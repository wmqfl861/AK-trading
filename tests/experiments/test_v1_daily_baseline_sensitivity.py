"""Small SYNTHETIC accounts; no real research input or credentials are read."""
from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, Inexact, localcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from ak_trading.futures import io as engine_io
from ak_trading.futures import scenario_backtest as engine
from ak_trading.futures.spec import ContractBar


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools/experiments/v1_daily_baseline_sensitivity.py"
SPEC = importlib.util.spec_from_file_location("sensitivity_under_test", SCRIPT)
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)
D = Decimal
SYMBOL = "SHFE.rb2405"
DECLARATION = {"raw_symbol": SYMBOL, "product": "SHFE.RB", "expiry_at": "2024-05-15T15:00:00+08:00",
               "instance_id": SYMBOL + "|2024-05-15T15:00:00+08:00", "identity_status": "SYNTHETIC"}


@pytest.fixture(autouse=True)
def exact_context():
    with localcontext() as ctx:
        ctx.prec = 128
        ctx.traps[Inexact] = True
        yield


def bars(closes, opens=None):
    first = datetime(2020, 11, 2, tzinfo=timezone(timedelta(hours=8)))
    result = []
    for j, close in enumerate(closes):
        opening, close = D(str(opens[j] if opens is not None else close)), D(str(close))
        ts = first + timedelta(days=j)
        result.append(ContractBar(symbol=SYMBOL, duration_seconds=86400, datetime_nano=int(ts.timestamp()) * 10**9,
                                  ts=ts, source="SYNTHETIC", open=opening, high=max(opening, close) + 1,
                                  low=min(opening, close) - 1, close=close, volume=D(1000)))
    return tuple(result)


def bundle(values):
    return engine_io.InputBundle(Path("SYNTHETIC-not-a-disk-input"),
         {"schema_version": "futures_bundle_v1", "dataset_kind": "real_export", "status": "complete", "test_label": "SYNTHETIC"},
         values, (DECLARATION,), {}, {})


def config(**changes):
    return dict(m.BASE, **changes)


def control(values, method="buy_hold_control", **changes):
    cfg = config(**changes)
    result = m.control_account(values, DECLARATION, method, cfg)
    ledger = m.verify_account(values, DECLARATION, method, str(cfg["initial_cash"]), "SYNTHETIC", cfg, result)
    return result, ledger


def strategy(values, method="donchian_trend", **changes):
    cfg = config(**changes)
    result = m.native_result(engine.run_scenario_backtest(bundle(values), symbol=SYMBOL, strategy=method,
                                                         config=engine.ScenarioConfig.from_mapping(cfg)))
    ledger = m.verify_account(values, DECLARATION, method, str(cfg["initial_cash"]), "SYNTHETIC", cfg, result)
    return result, ledger


def test_matrix_exact_cross_product_and_single_stresses():
    matrix = list(m.matrix_configs(m.fixed_config()))
    assert len(matrix) == 16 and len({(a, b) for a, b, _ in matrix}) == 16
    for capital, scenario, values in matrix:
        assert values["initial_cash"] == capital
        changed = {k for k in m.BASE if k != "initial_cash" and values[k] != m.BASE[k]}
        assert changed == {"BASE": set(), "FEE_X2": {"fee_per_lot", "fee_rate"},
                           "SLIPPAGE_X2": {"slippage_ticks"}, "MARGIN_X1_5": {"margin_rate"}}[scenario]
    bad = m.fixed_config()
    bad["capital_order"] = ["5000"]
    with pytest.raises(m.ValidationError):
        list(m.matrix_configs(bad))


def test_buy_hold_one_bar_round_trip_exact_cost_and_no_double_slip():
    result, ledger = control(bars([110], [100]))
    assert len(result["fills"]) == 2
    assert [f["phase"] for f in result["fills"]] == ["first_open", "terminal_close"]
    assert result["report"]["gross_pnl"] == "100"
    assert result["report"]["fees"] == "0.23"
    assert result["report"]["slippage"] == "20"
    assert result["report"]["net_pnl"] == "79.77"
    assert ledger[0]["terminal_close_fee_delta"] == D("0.12")
    assert ledger[0]["terminal_close_slippage_delta"] == 10
    assert ledger[0]["position"] == 0 and ledger[0]["unrealized_pnl"] == 0


def test_cost_changes_actual_admission_and_both_fee_components():
    values = bars([100, 101])
    admitted, _ = control(values, initial_cash="310.2")
    rejected, ledger = control(values, initial_cash="310.2", fee_per_lot="0.02", fee_rate="0.0002")
    assert len(admitted["fills"]) == 2 and rejected["fills"] == []
    assert ledger[0]["opening_margin_blocked"] == 1
    cheap, _ = control(values, fee_per_lot="0.02", fee_rate="0.0002")
    assert cheap["fills"][0]["fee_per_lot_component"] == D("0.02")
    assert cheap["fills"][0]["fee_rate_component"] == D("0.2")


def test_first_open_blocked_never_retries_on_cheaper_days():
    result, ledger = control(bars([100, 5, 5], [100, 5, 5]), initial_cash="100")
    assert result["fills"] == [] and result["report"]["no_entry"] is True
    assert sum(r["opening_attempts"] for r in ledger) == 1
    assert sum(r["opening_blocks"] for r in ledger) == 1
    assert all(r["equity"] == 100 for r in ledger)


def test_margin_equality_is_admitted_and_cost_exhaustion_takes_precedence():
    result, _ = control(bars([100]), initial_cash="300", fee_per_lot="0", fee_rate="0", slippage_ticks="0")
    assert len(result["fills"]) == 2
    result, ledger = control(bars([100]), initial_cash="10.11")
    assert not result["fills"] and ledger[0]["opening_costs_exhaust_equity"] == 1
    assert ledger[0]["opening_margin_blocked"] == 0


def test_holding_breach_is_recorded_without_forced_exit():
    result, ledger = control(bars([100, 5, 5]), initial_cash="400", fee_per_lot="0", fee_rate="0", slippage_ticks="0")
    assert ledger[1]["position"] == 1 and ledger[1]["equity"] == -550
    assert ledger[1]["margin_breach_observations"] == 1 and ledger[1]["nonpositive_equity_observations"] == 1
    assert len(result["fills"]) == 2 and result["fills"][-1]["observation_index"] == 2


def test_primary_counts_inherit_position_even_when_terminal_row_closes_it():
    _, ledger = control(bars(list(range(100, 161))))
    cell = m.window_cell(ledger, "PRIMARY", 2021)
    assert cell["observations"] == 1 and cell["first_j"] == 60
    assert ledger[59]["raw_year"] == 2020 and ledger[60]["raw_year"] == 2021
    assert ledger[60]["position"] == 0 and ledger[60]["previous_position"] == 1
    assert (cell["opening_accounts_in_view"], cell["no_new_open_accounts_in_view"], cell["never_opened_accounts"],
            cell["held_at_view_start_accounts"]) == (0, 1, 0, 1)
    assert cell["fee_sum"] == D("0.17") and cell["slippage_sum"] == 10
    assert ledger[60]["previous_equity"] == ledger[59]["equity"]


def test_flat_zero_and_empty_primary_are_distinct_and_years_conserve():
    _, ledger = control(bars([100] * 60), "flat_control")
    all_cell, primary = m.window_cell(ledger, "ALL", "ALL_YEARS"), m.window_cell(ledger, "PRIMARY", "ALL_YEARS")
    assert m.aggregate([all_cell])["zero"] == 1
    assert m.aggregate([primary])["n"] == 0 and m.aggregate([primary])["net_sum"] is None
    assert primary["never_opened_accounts"] == 0 and primary["no_new_open_accounts_in_view"] == 0
    _, held = control(bars(list(range(100, 165))))
    all_total = m.window_cell(held, "ALL", "ALL_YEARS")["net_sum"]
    annual = [m.window_cell(held, "ALL", y)["net_sum"] for y in m.YEARS]
    assert sum(x for x in annual if x is not None) == all_total


def test_reversal_close_survives_when_new_open_is_blocked():
    # The breakout must exceed the prior high (140), not merely equal it.
    closes = list(range(100, 140)) + [150, 10, 6]
    opens = list(range(100, 141)) + [150, 8]
    result, ledger = strategy(bars(closes, opens), initial_cash="500", fee_per_lot="0", fee_rate="0", slippage_ticks="0")
    assert [(f["observation_index"], f["offset"]) for f in result["fills"]] == [(41, "open"), (42, "close")]
    assert ledger[42]["opening_costs_exhaust_equity"] == 1 and ledger[42]["fills"] == 1
    assert ledger[42]["realized_pnl"] == -1420 and ledger[42]["position"] == 0


def test_whole_bundle_and_immutable_single_contract_view_have_identical_native_bytes():
    values = bars(list(range(100, 163)))
    full = bundle(values + tuple(replace(b, symbol="DCE.m2405") for b in values))
    full = replace(full, contracts=full.contracts + ({"raw_symbol": "DCE.m2405", "instance_id": "SYNTHETIC-other"},))
    view = engine_io.InputBundle(full.root, m.frozen(dict(full.manifest)), values, (m.frozen(DECLARATION),), m.frozen({}), m.frozen({}))
    for method in m.STRATEGIES:
        original = engine.run_scenario_backtest(full, symbol=SYMBOL, strategy=method, config=engine.ScenarioConfig.from_mapping(config()))
        selected = engine.run_scenario_backtest(view, symbol=SYMBOL, strategy=method, config=engine.ScenarioConfig.from_mapping(config()))
        assert m.native_payloads(m.native_result(original), engine) == m.native_payloads(m.native_result(selected), engine)


def test_paired_median_uses_paired_differences_and_rejects_missing_support():
    _, ledger = control(bars([100]), "flat_control")
    template = m.window_cell(ledger, "ALL", "ALL_YEARS")
    left = [dict(template, contract_instance_id=str(i), net_sum=D(x)) for i, x in enumerate([0, 100, 101, 102])]
    right = [dict(template, contract_instance_id=str(i), net_sum=D(x)) for i, x in enumerate([0, 1, 2, 100])]
    assert m.paired(left, right)["net_median"] == D("50.5")
    assert m.aggregate(left)["net_median"] - m.aggregate(right)["net_median"] == 99
    with pytest.raises(m.ValidationError):
        m.paired(left, right[:-1])
    right[0]["support_sha256"] = "changed"
    with pytest.raises(m.ValidationError):
        m.paired(left, right)


@pytest.mark.parametrize("defect", ["duplicate_fill", "duplicate_mark", "nonfinite", "cash", "cost"])
def test_invalid_ledger_is_rejected(defect):
    values = bars([100, 110])
    result = m.control_account(values, DECLARATION, "buy_hold_control", config())
    if defect == "duplicate_fill":
        result["fills"].append(dict(result["fills"][0]))
    elif defect == "duplicate_mark":
        result["equity"][1]["observation_index"] = 0
    elif defect == "nonfinite":
        result["equity"][0]["net_pnl"] = D("NaN")
    elif defect == "cash":
        result["equity"][0]["cash"] += 1
    else:
        result["fills"][0]["fee"] += 1
    with pytest.raises(m.ValidationError):
        m.verify_account(values, DECLARATION, "buy_hold_control", "1000000", "BASE", config(), result)


def test_pin_drift_preloaded_engine_and_nonzero_receipt_rejected(tmp_path):
    source = tmp_path / "SYNTHETIC.json"
    source.write_bytes(b"original")
    pins = m.Pins()
    pins.read("source", source, m.pin(b"original"))
    source.write_bytes(b"changed")
    with pytest.raises(m.ValidationError, match="source changed"):
        pins.verify()
    with pytest.raises(m.ValidationError, match="preloaded"):
        m.load_engine(tmp_path, {}, tmp_path / "output")
    receipt = tmp_path / "receipt.json"
    receipt.write_bytes(m.json_bytes({"stage": "matrix_child", "terminated": True, "raw_exit": 7}))
    with pytest.raises(m.ValidationError, match="did not terminate"):
        m.seal_check(tmp_path / "output", receipt)
    assert not (tmp_path / "output/publish/CHECK.json").exists()


def cli(args):
    return subprocess.run([sys.executable, "-B", "-X", "utf8", str(SCRIPT), *args], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8", timeout=60)


def cli_args(tmp_path, output, cfg=None):
    return ["--source-repo", str(tmp_path / "source"), "--authority-dir", str(tmp_path / "authority"),
            "--engine-root", str(tmp_path / "engine"), "--config", str(cfg or ROOT / "config/research/v1_daily_baseline_rb_b.json"),
            "--output-dir", str(output)]


@pytest.mark.parametrize("defect", ["missing", "existing", "config", "repeat", "abbreviation", "unknown"])
def test_actual_cli_fail_closed_without_real_sources(tmp_path, defect):
    output = tmp_path / "output"
    args = cli_args(tmp_path, output)
    if defect == "existing":
        output.mkdir()
        (output / "keep").write_bytes(b"SYNTHETIC")
    elif defect == "config":
        cfg = m.fixed_config()
        cfg["base_assumptions"] = dict(cfg["base_assumptions"], lots=2)
        config_path = tmp_path / "config.json"
        config_path.write_bytes(m.json_bytes(cfg))
        args = cli_args(tmp_path, output, config_path)
    elif defect == "repeat":
        args += ["--output-dir", str(output)]
    elif defect == "abbreviation":
        args[0] = "--source"
    elif defect == "unknown":
        args += ["--skip-pins"]
    result = cli(args)
    assert result.returncode == 2, result.stderr
    assert not (output / "manifest.json").exists() and not (output / "publish/CHECK.json").exists()
    if defect == "existing":
        assert (output / "keep").read_bytes() == b"SYNTHETIC"


def mini_context(tmp_path):
    values = bars(list(range(100, 161)))
    full = bundle(values)
    coverage = {"contracts": [{"symbol": SYMBOL, "contract_instance_id": DECLARATION["instance_id"],
                                "observations": 61, "primary_observations": 1, "first_raw_ts": values[0].ts.isoformat(),
                                "last_raw_ts": values[-1].ts.isoformat(), "year_observations": {str(y): sum(b.ts.year == y for b in values) for y in m.YEARS}}],
                "rb_raw_ts_min": values[0].ts.isoformat(), "rb_raw_ts_max": values[-1].ts.isoformat()}
    groups, accepted_payloads = defaultdict(list), {}
    for method in m.STRATEGIES:
        result, ledger = strategy(values, method)
        for filename, raw in m.native_payloads(result, engine).items():
            accepted_payloads[f"children/{SYMBOL}/{method}/{filename}"] = m.pin(raw)["sha256"]
        for view in m.VIEWS:
            for scope in ("ALL_YEARS", *m.YEARS):
                groups[m.group_key("1000000", "BASE", method, view, scope)].append(m.window_cell(ledger, view, scope))
    prior = {m.REPORT: "# RB 日线既有结果\n\nSYNTHETIC old first\nold second\nold third\n\nSYNTHETIC original A body\n".encode(),
             "CHECK.json": m.json_bytes({"report_value_refs": []})}
    for filename, view in (("year-all.csv", "ALL"), ("year-primary.csv", "PRIMARY")):
        rows = []
        for year in m.YEARS:
            for method in m.STRATEGIES:
                r = m.aggregate(groups[m.group_key("1000000", "BASE", method, view, year)])
                rows.append({"raw_year": year, "strategy": method, **r, "median": r["net_median"], "min": r["net_min"], "max": r["net_max"]})
        prior[filename] = m.csv_bytes(rows)
    pairs = []
    for year in m.YEARS:
        for view in m.VIEWS:
            for a, b in ((m.STRATEGIES[0], m.STRATEGIES[1]), (m.STRATEGIES[0], m.STRATEGIES[2]), (m.STRATEGIES[1], m.STRATEGIES[2])):
                r = m.paired(groups[m.group_key("1000000", "BASE", a, view, year)], groups[m.group_key("1000000", "BASE", b, view, year)])
                pairs.append({"raw_year": year, "view": view, "strategy_a": a, "strategy_b": b, **r,
                              "median": r["net_median"], "min": r["net_min"], "max": r["net_max"]})
    prior["paired-years.csv"] = m.csv_bytes(pairs)
    prior["conclusions.csv"] = m.csv_bytes([{"strategy": method, "view": view, "calendar_year_slots": 7,
         "positive_years": sum(m.aggregate(groups[m.group_key("1000000", "BASE", method, view, y)])["net_sum"] > 0
                               for y in m.YEARS if m.aggregate(groups[m.group_key("1000000", "BASE", method, view, y)])["n"])}
         for method in m.STRATEGIES for view in m.VIEWS])
    return full, SimpleNamespace(output=tmp_path / "first", engine_root=ROOT, engine_hashes={}, bundle_root=tmp_path,
         input_manifest=full.manifest, config=copy.deepcopy(m.fixed_config()), pins=m.Pins(), coverage=coverage,
         prior=prior, manifest={"content_hashes": accepted_payloads}, accepted_counts={"rb_declarations": 1, "rb_bars": 61, "rb_distinct_raw_dates": 61})


def test_two_small_matrices_are_byte_identical_and_complete(tmp_path, monkeypatch):
    full, context = mini_context(tmp_path)
    monkeypatch.setattr(m, "load_engine", lambda *_: (engine, engine_io, {}))
    monkeypatch.setattr(engine_io, "read_input_bundle", lambda _: full)
    first = m.run_matrix(context)
    assert first["accounts"] == 80 and first["contract_year_cells"] == 1120
    context.output = tmp_path / "second"
    second = m.run_matrix(context)
    assert first == second
    def payloads(root):
        return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file() and "runtime" not in p.parts}
    assert payloads(tmp_path / "first") == payloads(tmp_path / "second")
    assert not (tmp_path / "first/publish/CHECK.json").exists()


def test_midrun_failure_never_publishes_complete_manifest(tmp_path, monkeypatch):
    full, context = mini_context(tmp_path)
    monkeypatch.setattr(m, "load_engine", lambda *_: (engine, engine_io, {}))
    monkeypatch.setattr(engine_io, "read_input_bundle", lambda _: full)
    original = m.write_account
    calls = []
    def fail_second(*args):
        calls.append(True)
        if len(calls) == 2:
            raise OSError("SYNTHETIC disk failure")
        return original(*args)
    monkeypatch.setattr(m, "write_account", fail_second)
    with pytest.raises(OSError, match="SYNTHETIC"):
        m.run_matrix(context)
    assert (context.output / "accounts").is_dir()
    assert not (context.output / "manifest.json").exists() and not (context.output / "publish/CHECK.json").exists()


def test_source_drift_during_small_matrix_prevents_completion(tmp_path, monkeypatch):
    full, context = mini_context(tmp_path)
    source = tmp_path / "SYNTHETIC-source.txt"
    source.write_bytes(b"original")
    context.pins.read("SYNTHETIC.source", source)
    monkeypatch.setattr(m, "load_engine", lambda *_: (engine, engine_io, {}))
    monkeypatch.setattr(engine_io, "read_input_bundle", lambda _: full)
    original = m.write_account
    def mutate_source(*args):
        result = original(*args)
        source.write_bytes(b"changed during SYNTHETIC run")
        return result
    monkeypatch.setattr(m, "write_account", mutate_source)
    with pytest.raises(m.ValidationError, match="source changed"):
        m.run_matrix(context)
    assert not (context.output / "manifest.json").exists()
    assert not (context.output / "publish/CHECK.json").exists()
