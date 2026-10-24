"""Offline comparison contracts; every generated price is SYNTHETIC."""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from ak_trading.futures.io import (BARS_CSV_COLUMNS, bars_rows_from_bars,
                                   canonical_json_bytes, publish_input_bundle,
                                   read_input_bundle, write_canonical_csv)
from ak_trading.futures.scenario_backtest import ScenarioConfig, run_scenario_backtest
from ak_trading.futures.spec import ContractBar

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools/experiments/historical_daily_scenario_compare.py"
ARITHMETIC = Context(prec=128, rounding=ROUND_HALF_EVEN)
BASE = dict(initial_cash="1000000", lots=1, multiplier="10", price_tick="1",
            fee_per_lot="0.01", fee_rate="0.0001", slippage_ticks="1",
            margin_rate="0.12", max_margin_ratio="0.4")
STRATEGIES = ("donchian_trend", "ma_trend", "zscore_reversion")
SCENARIOS = ("BASE", "FEE_X2", "SLIPPAGE_X2", "MARGIN_X1_5")


@pytest.fixture
def cli():
    spec = importlib.util.spec_from_file_location("rb_daily_compare_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_input(tmp_path, histories=None, *, reverse=False, assumptions=None):
    # The real_export schema wrapper is required by the existing engine. Both
    # metadata and every bar source state that these prices are SYNTHETIC.
    histories = histories or {
        "SHFE.rb2401": list(range(100, 143)),
        "SHFE.rb2402": [100] * 60,
        "SHFE.rb2403": list(range(100, 165)),
        "SHFE.rb2404": [],
    }
    declarations, bars = [], []
    first = datetime(2023, 1, 1, tzinfo=timezone.utc)
    for symbol, closes in histories.items():
        expiry = f"2024-{symbol[-2:]}-15T15:00:00+08:00"
        declarations.append({"raw_symbol": symbol, "product": "SHFE.RB", "expiry_at": expiry,
                             "instance_id": f"{symbol}|{expiry}", "identity_status": "SYNTHETIC"})
        for index, close in enumerate(closes):
            ts = first + timedelta(days=index)
            value = Decimal(str(close))
            bars.append(ContractBar(symbol=symbol, duration_seconds=86400, ts=ts,
                                    datetime_nano=int(ts.timestamp()) * 1_000_000_000,
                                    source="SYNTHETIC_generated_comparison_test_prices",
                                    open=value, close=value, high=value + Decimal("0.1"),
                                    low=value - Decimal("0.1"), volume=Decimal("1000")))
    if reverse:
        bars.reverse()
        declarations.reverse()
    source = tmp_path / "SYNTHETIC-input"
    publish_input_bundle(
        source, dataset_kind="real_export", created_at="2024-06-01T00:00:00+00:00",
        bars_rows=bars_rows_from_bars(bars), contracts=declarations,
        metadata={"evidence_level": "SYNTHETIC", "role": "schema_fixture_not_real_export_evidence"},
        availability={}, request_info={}, source_hashes={"SYNTHETIC": "a" * 64},
        read_only_transaction={"executed": False}, clock_policy={"d_status": "unavailable"},
        notes={"price_source": "SYNTHETIC"},
    )
    config = tmp_path / "SYNTHETIC-base.json"
    config.write_bytes(canonical_json_bytes(assumptions or BASE))
    return source, config


@pytest.fixture
def inputs(tmp_path):
    return build_input(tmp_path)


def args(source, config, output):
    return ["--input-bundle", str(source), "--base-config", str(config), "--output", str(output)]


def tree(path):
    return {p.relative_to(path).as_posix(): p.read_bytes() for p in path.rglob("*") if p.is_file()}


def document(path):
    return json.loads(path.read_bytes())


def ledger(path):
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def rewrite_payload(source, name, payload):
    (source / name).write_bytes(payload)
    manifest = document(source / "manifest.json")
    manifest["content_hashes"][name] = sha256(payload).hexdigest()
    (source / "manifest.json").write_bytes(canonical_json_bytes(manifest))


def child(root, symbol="SHFE.rb2403", strategy="donchian_trend", scenario="BASE"):
    return root / "cells" / symbol / strategy / scenario


def test_population_gate_keeps_every_natural_trajectory_and_zero_result(cli, inputs, tmp_path):
    source, config = inputs
    output = tmp_path / "complete"
    before = tree(source)
    assert cli.main(args(source, config, output)) == 0
    assert tree(source) == before
    coverage = document(output / "coverage.json")
    summary = document(output / "summary.json")
    rows = ledger(output / "comparison.csv")
    assert len(rows) == len(summary["cells"]) == 36
    assert coverage["declared_contracts"] == 4
    assert coverage["contracts_with_bars"] == 3 and coverage["declared_without_interval_bars"] == 1
    assert coverage["common_matrix_cells"] == 24 and coverage["insufficient_matrix_cells"] == 12
    assert len({(r["symbol"], r["contract_instance_id"], r["strategy"], r["scenario"]) for r in rows}) == 36
    assert [(r["symbol"], r["strategy"], r["scenario"]) for r in rows] == [
        (s, strategy, scenario) for s in ("SHFE.rb2401", "SHFE.rb2402", "SHFE.rb2403")
        for strategy in STRATEGIES for scenario in SCENARIOS]
    assert all(group["n"] == 2 for group in summary["groups"])
    records = {r["symbol"]: r for r in coverage["contracts"]}
    assert records["SHFE.rb2401"]["common_coverage_status"] == "INSUFFICIENT_COMMON_COVERAGE"
    assert records["SHFE.rb2402"]["strategy_coverage"]["ma_trend"]["potential_next_open_slots"] == 0
    assert records["SHFE.rb2404"]["first_raw_ts"] is None
    bundle = read_input_bundle(source)
    base = ScenarioConfig.from_mapping(BASE)
    for symbol in ("SHFE.rb2401", "SHFE.rb2402", "SHFE.rb2403"):
        for strategy in STRATEGIES:
            result = run_scenario_backtest(bundle, symbol=symbol, strategy=strategy, config=base)
            directory = child(output, symbol, strategy)
            assert document(directory / "report.json") == dict(result.report)
            decisions = ledger(directory / "decisions.csv")
            assert [r["candidate_direction"] for r in decisions] == [
                "" if d.candidate_direction is None else str(d.candidate_direction) for d in result.decisions]
            assert [Decimal(r["cash"]) for r in ledger(directory / "equity.csv")] == [m.cash for m in result.equity]
    assert document(child(output, "SHFE.rb2401", "donchian_trend") / "report.json")["fills"] == 2
    boundary = next(d for d in summary["cells"] if d["symbol"] == "SHFE.rb2402" and d["strategy"] == "ma_trend" and d["scenario"] == "BASE")
    assert boundary["fills"] == 0 and boundary["first_nonnull_candidate_index"] == 59
    assert boundary["potential_next_open_slots"] == 0
    assert boundary["child_report"]["candidate_counts"] == {"0": 1, "null": 59}
    zero = next(d for d in summary["cells"] if d["symbol"] == "SHFE.rb2401" and d["strategy"] == "ma_trend" and d["scenario"] == "BASE")
    assert zero["fills"] == 0 and zero["first_nonnull_candidate_index"] is None
    assert zero["child_report"]["candidate_counts"] == {"null": 43}
    assert zero["child_report"]["reconciliation"]["terminal_flat"] is True
    assert "SYNTHETIC" in (output / "report.md").read_text(encoding="utf-8")


def test_deterministic_two_directory_outputs_and_exact_hash_closure(cli, inputs, tmp_path, monkeypatch):
    source, config = inputs
    first, second = tmp_path / "one", tmp_path / "nested" / "two"
    original = Path.open
    writes = []

    def record(path, mode="r", *a, **kw):
        if first in path.parents and "x" in mode:
            writes.append(path.relative_to(first).as_posix())
        return original(path, mode, *a, **kw)

    monkeypatch.setattr(Path, "open", record)
    assert cli.main(args(source, config, first)) == 0
    assert writes[-1] == "manifest.json"
    assert cli.main(args(source, config, second)) == 0
    payloads = tree(first)
    assert payloads == tree(second)
    manifest = document(first / "manifest.json")
    assert manifest["content_hashes"] == {name: sha256(payload).hexdigest() for name, payload in payloads.items() if name != "manifest.json"}
    assert manifest["input_base_config_sha256"] == sha256(config.read_bytes()).hexdigest()
    assert manifest["code_hashes"]["tools/experiments/historical_daily_scenario_compare.py"] == sha256(SCRIPT.read_bytes()).hexdigest()
    assert len(payloads) == 36 * 6 + 4 + 5
    assert manifest["enable_trading"] is False and manifest["broker"] == "NOT_RUN"
    assert manifest["real_order_send_calls"] == manifest["real_order_cancel_calls"] == 0
    assert str(first).encode() not in b"".join(payloads.values())


def test_population_does_not_follow_pnl_direction_or_input_row_order(cli, tmp_path):
    populations = []
    for index, (reverse, declining) in enumerate(((False, False), (True, False), (True, True))):
        work = tmp_path / str(index)
        work.mkdir()
        histories = {"SHFE.rb2401": list(range(100, 143)), "SHFE.rb2402": [100] * 60,
                     "SHFE.rb2403": [300 - i if declining else 100 + i for i in range(65)]}
        source, config = build_input(work, histories, reverse=reverse)
        output = work / "out"
        assert cli.main(args(source, config, output)) == 0
        coverage = document(output / "coverage.json")
        populations.append([(r["symbol"], r["observations"], r["common_coverage_status"]) for r in coverage["contracts"]])
    assert populations[0] == populations[1] == populations[2]


def accounting_oracle(directory, config):
    """Independent Decimal accounting from raw fill/equity rows, no owner helper."""
    fills, equity = ledger(directory / "fills.csv"), ledger(directory / "equity.csv")
    cash = Decimal(config["initial_cash"])
    fees = slip = gross = Decimal(0)
    position, entry = 0, Decimal(0)
    multiplier = Decimal(config["multiplier"])
    with localcontext(ARITHMETIC):
        for fill in fills:
            lots, side, price = int(fill["lots"]), int(fill["side"]), Decimal(fill["ideal_price"])
            flat = Decimal(config["fee_per_lot"]) * lots
            rate = Decimal(config["fee_rate"]) * price * multiplier * lots
            charge = Decimal(config["slippage_ticks"]) * Decimal(config["price_tick"]) * multiplier * lots
            realized = (price - entry) * position * multiplier if fill["offset"] == "close" else Decimal(0)
            assert Decimal(fill["fee_per_lot_component"]) == flat
            assert Decimal(fill["fee_rate_component"]) == rate
            assert Decimal(fill["fee"]) == flat + rate
            assert Decimal(fill["slippage"]) == charge
            assert Decimal(fill["realized_pnl"]) == realized
            cash += realized - flat - rate - charge
            assert Decimal(fill["cash_after"]) == cash
            gross += realized
            fees += flat + rate
            slip += charge
            position = 0 if fill["offset"] == "close" else side * lots
            entry = Decimal(0) if not position else price
        peak, worst = Decimal(config["initial_cash"]), Decimal(0)
        for mark in equity:
            value = Decimal(mark["equity"])
            peak = max(peak, value)
            worst = max(worst, peak - value)
            assert Decimal(mark["drawdown"]) == peak - value
            assert Decimal(mark["available_equity"]) == value - Decimal(mark["required_margin"])
        report = document(directory / "report.json")
        assert position == 0
        assert cash == Decimal(equity[-1]["equity"]) == Decimal(report["ending_cash"]) == Decimal(report["ending_equity"])
        assert gross == Decimal(report["gross_pnl"])
        assert fees == Decimal(report["fees"]) and slip == Decimal(report["slippage"])
        assert gross - fees - slip == cash - Decimal(config["initial_cash"]) == Decimal(report["net_pnl"])
        assert worst == Decimal(report["max_drawdown"])
    return fills, report


def test_both_fee_components_all_reversal_legs_and_only_registered_scenario_deltas(cli, tmp_path):
    assumptions = dict(BASE, lots=2, fee_per_lot="3", fee_rate="0.001", price_tick="0.5",
                       initial_cash="100000", margin_rate="0.1")
    source, config = build_input(tmp_path, {"SHFE.rb2401": list(range(100, 141)) + [10, 6]}, assumptions=assumptions)
    output = tmp_path / "costs"
    assert cli.main(args(source, config, output)) == 0
    fills = {}
    for scenario in SCENARIOS:
        derived = document(output / "configs" / f"{scenario}.json")
        altered = {k for k in assumptions if derived[k] != assumptions[k]}
        assert altered == {"BASE": set(), "FEE_X2": {"fee_per_lot", "fee_rate"},
                           "SLIPPAGE_X2": {"slippage_ticks"}, "MARGIN_X1_5": {"margin_rate"}}[scenario]
        fills[scenario], _ = accounting_oracle(child(output, "SHFE.rb2401", scenario=scenario), derived)
    assert [r["offset"] for r in fills["BASE"]] == ["open", "close", "open", "close"]
    assert [r["side"] for r in fills["BASE"]] == ["1", "-1", "-1", "1"]
    for index, base in enumerate(fills["BASE"]):
        fee, slippage, margin = (fills[name][index] for name in SCENARIOS[1:])
        assert Decimal(fee["fee_per_lot_component"]) == 2 * Decimal(base["fee_per_lot_component"]) > 0
        assert Decimal(fee["fee_rate_component"]) == 2 * Decimal(base["fee_rate_component"]) > 0
        assert fee["slippage"] == base["slippage"]
        assert slippage["fee"] == base["fee"] and Decimal(slippage["slippage"]) == 2 * Decimal(base["slippage"])
        assert margin["fee"] == base["fee"] and margin["slippage"] == base["slippage"]
    rows = ledger(output / "comparison.csv")
    for strategy in STRATEGIES:
        group = [row for row in rows if row["strategy"] == strategy]
        base = group[0]
        for row in group:
            for key in ("net_pnl", "fees", "slippage", "max_drawdown", "fills", "opening_blocks"):
                assert Decimal(row[f"delta_{key}_vs_BASE"]) == Decimal(row[key]) - Decimal(base[key])


def test_scenarios_replay_admission_and_can_change_actual_fills(cli, tmp_path):
    assumptions = dict(BASE, initial_cash="420", fee_per_lot="10", fee_rate="0.01",
                       price_tick="0.5", slippage_ticks="5", margin_rate="0.1")
    source, config = build_input(tmp_path, {"SHFE.rb2401": list(range(100, 150))}, assumptions=assumptions)
    output = tmp_path / "admission"
    assert cli.main(args(source, config, output)) == 0
    reports = {scenario: document(child(output, "SHFE.rb2401", scenario=scenario) / "report.json") for scenario in SCENARIOS}
    assert reports["BASE"]["fills"] == 2 and reports["BASE"]["opening_blocks"] == 0
    for scenario in SCENARIOS[1:]:
        assert reports[scenario]["fills"] == 0 and reports[scenario]["opening_blocks"] == 9
        assert reports[scenario]["net_pnl"] == "0"
        accounting_oracle(child(output, "SHFE.rb2401", scenario=scenario), document(output / "configs" / f"{scenario}.json"))
    accounting_oracle(child(output, "SHFE.rb2401"), document(output / "configs" / "BASE.json"))
    assert reports["BASE"]["net_pnl"] == "-19"
    # Blocking this losing round trip makes stress PnL greater than BASE. This
    # real engine counterexample also prevents a false monotonic-cost claim.
    assert Decimal(reports["FEE_X2"]["net_pnl"]) > Decimal(reports["BASE"]["net_pnl"])


def test_all_short_population_keeps_null_descriptions_and_full_zero_cells(cli, tmp_path):
    source, config = build_input(tmp_path, {"SHFE.rb2401": [100] * 10})
    output = tmp_path / "short"
    assert cli.main(args(source, config, output)) == 0
    summary = document(output / "summary.json")
    assert len(summary["cells"]) == 12 and summary["common_matrix_cells"] == 0
    assert len(summary["insufficient_common_coverage_cells"]) == 12
    for group in summary["groups"]:
        assert group["n"] == 0 and group["status"] == "INSUFFICIENT"
        assert group["net_pnl"] == group["total_return"] == {"min": None, "median": None, "max": None}
        assert group["worst_single_contract_max_drawdown"] is None
    assert all(cell["fills"] == 0 and cell["first_nonnull_candidate_index"] is None for cell in summary["cells"])


def test_summary_even_median_denominators_and_cell_links_are_ledger_derived(cli, inputs, tmp_path):
    source, config = inputs
    output = tmp_path / "descriptions"
    assert cli.main(args(source, config, output)) == 0
    summary = document(output / "summary.json")
    with localcontext(ARITHMETIC):
        for group in summary["groups"]:
            cells = [c for c in summary["cells"] if c["coverage_status"] == "MEETS_60_OBSERVATIONS" and c["strategy"] == group["strategy"] and c["scenario"] == group["scenario"]]
            assert group["n"] == len(cells) == 2
            nets = sorted(Decimal(c["net_pnl"]) for c in cells)
            assert Decimal(group["net_pnl"]["median"]) == (nets[0] + nets[1]) / 2
            assert group["positive_net_pnl_contracts"] == sum(n > 0 for n in nets)
            assert group["zero_net_pnl_contracts"] == sum(n == 0 for n in nets)
            assert group["negative_net_pnl_contracts"] == sum(n < 0 for n in nets)
            for cell in cells:
                directory = output / cell["child_directory"]
                assert cell["child_report"] == document(directory / "report.json")
                equity = ledger(directory / "equity.csv")
                assert Decimal(cell["peak_required_margin"]) == max(Decimal(mark["required_margin"]) for mark in equity)
                assert Decimal(cell["minimum_available_equity"]) == min(Decimal(mark["available_equity"]) for mark in equity)
                accounting_oracle(directory, document(output / "configs" / f"{cell['scenario']}.json"))


@pytest.mark.parametrize("missing", ["--input-bundle", "--base-config", "--output"])
def test_required_arguments_reject_machine_readably_without_output(cli, inputs, tmp_path, missing, capsys):
    source, config = inputs
    output = tmp_path / "out"
    arguments = args(source, config, output)
    index = arguments.index(missing)
    del arguments[index:index + 2]
    assert cli.main(arguments) == 2 and not output.exists()
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "rejected" and result["error_type"] == "_ArgumentError"


@pytest.mark.parametrize("extra", [["--output", "other"], ["--base-config", "other"],
                                  ["--input-bundle", "other"], ["--strategy", "ma_trend"],
                                  ["--out", "other"], ["--symbol", "SHFE.rb2401"]])
def test_repeat_unknown_filter_or_abbreviation_reject(cli, inputs, tmp_path, extra):
    source, config = inputs
    output = tmp_path / "out"
    assert cli.main(args(source, config, output) + extra) == 2 and not output.exists()


@pytest.mark.parametrize("problem", ["missing", "duplicate", "NaN", "infinity", "unknown", "absent_field", "array", "fractional_lots"])
def test_strict_nine_parameter_config_preflight(cli, inputs, tmp_path, problem):
    source, config = inputs
    if problem == "missing":
        config = tmp_path / "absent.json"
    elif problem == "duplicate":
        config.write_bytes(config.read_bytes().replace(b'"lots":1', b'"lots":2,"lots":1'))
    elif problem == "NaN":
        config.write_bytes(config.read_bytes().replace(b'"1000000"', b'NaN'))
    elif problem == "infinity":
        config.write_bytes(config.read_bytes().replace(b'"1000000"', b'1e999'))
    elif problem == "array":
        config.write_bytes(b"[]\n")
    else:
        values = dict(BASE)
        if problem == "unknown":
            values["fee"] = "1"
        elif problem == "absent_field":
            del values["fee_rate"]
        else:
            values["lots"] = 1.5
        config.write_bytes(canonical_json_bytes(values))
    output = tmp_path / "out"
    assert cli.main(args(source, config, output)) == 2 and not output.exists()


@pytest.mark.parametrize("problem", ["hash", "missing_payload", "duplicate_json", "ambiguity", "duplicate_instance",
                                     "wrong_product", "duration", "invalid_ohlc", "nonfinite_price", "undeclared_bar"])
def test_input_population_payload_and_price_failures_precede_output(cli, inputs, tmp_path, problem):
    source, config = inputs
    if problem == "hash":
        (source / "bars.csv").write_bytes(b"corrupted")
    elif problem == "missing_payload":
        (source / "metadata.json").unlink()
    elif problem == "duplicate_json":
        rewrite_payload(source, "metadata.json", b'{"evidence_level":"REAL","evidence_level":"SYNTHETIC"}\n')
    elif problem in {"ambiguity", "duplicate_instance", "wrong_product"}:
        contracts = document(source / "contracts.json")
        if problem == "ambiguity":
            contracts.append(dict(contracts[0], expiry_at="2034-01-15T15:00:00+08:00",
                                  instance_id="SHFE.rb2401|2034-01-15T15:00:00+08:00"))
        elif problem == "duplicate_instance":
            contracts[-1]["instance_id"] = contracts[0]["instance_id"]
        else:
            contracts[-1]["product"] = "DCE.M"
        rewrite_payload(source, "contracts.json", canonical_json_bytes(contracts))
    else:
        rows = ledger(source / "bars.csv")
        key, value = {"duration": ("duration_seconds", "60"), "invalid_ohlc": ("high", "1"),
                      "nonfinite_price": ("close", "NaN"), "undeclared_bar": ("symbol", "SHFE.rb2499")}[problem]
        rows[1][key] = value
        digest = write_canonical_csv(source / "bars.csv", BARS_CSV_COLUMNS, rows)
        manifest = document(source / "manifest.json")
        manifest["content_hashes"]["bars.csv"] = digest
        (source / "manifest.json").write_bytes(canonical_json_bytes(manifest))
    before = tree(source)
    output = tmp_path / "out"
    assert cli.main(args(source, config, output)) == 2 and not output.exists()
    assert tree(source) == before


@pytest.mark.parametrize("kind", ["existing_empty", "existing_file", "existing_marker", "input_child", "input_parent",
                                  "source_src", "source_scripts", "source_tests", "source_config", "source_tools", "source_docs", "source_ancestor"])
def test_output_preflight_protects_inputs_sources_and_collision_bytes(cli, inputs, tmp_path, kind):
    source, config = inputs
    output = tmp_path / "out"
    if kind == "existing_empty":
        output.mkdir()
    elif kind == "existing_file":
        output.write_bytes(b"preserve")
    elif kind == "existing_marker":
        output.mkdir()
        (output / "manifest.json").write_bytes(b"preserve")
    elif kind == "input_child":
        output = source / "child"
    elif kind == "input_parent":
        output = source.parent
    elif kind == "source_ancestor":
        output = ROOT
    else:
        output = ROOT / kind.removeprefix("source_") / "rb-daily-compare-forbidden-output"
    before = tree(tmp_path)
    assert cli.main(args(source, config, output)) == 2
    assert tree(tmp_path) == before
    if kind.startswith("source_") and kind != "source_ancestor":
        assert not output.exists()


@pytest.mark.parametrize("alias", ["NUL", "CON", "COM1.json", "LPT9", "AUX"])
def test_device_alias_output_is_rejected_without_open(cli, inputs, tmp_path, alias):
    source, config = inputs
    output = tmp_path / alias
    assert cli.main(args(source, config, output)) == 2


def test_output_symlink_component_is_rejected(cli, inputs, tmp_path):
    source, config = inputs
    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    assert cli.main(args(source, config, alias / "out")) == 2
    assert not (target / "out").exists() and alias.is_symlink()


@pytest.mark.parametrize("failure", ["cell_exception", "input_drift", "code_drift", "child_incomplete", "child_code_mismatch", "derived_config_drift"])
def test_mid_matrix_failures_never_publish_complete(cli, inputs, tmp_path, monkeypatch, failure, capsys):
    source, config = inputs
    output = tmp_path / "failed"
    original = cli.run_history_scenario
    original_hashes = cli._code_hashes
    calls, drift = 0, False

    def hashes():
        current = original_hashes()
        if drift:
            current["tools/experiments/historical_daily_scenario_compare.py"] = "f" * 64
        return current

    def injected(*a, **kw):
        nonlocal calls, drift
        calls += 1
        if failure == "cell_exception" and calls == 2:
            raise RuntimeError("SYNTHETIC injected second-cell failure")
        result = original(*a, **kw)
        if calls == 1:
            directory = a[1]
            if failure == "input_drift":
                rewrite_payload(source, "metadata.json", canonical_json_bytes({"evidence_level": "SYNTHETIC", "revision": 2}))
            elif failure == "code_drift":
                drift = True  # SYNTHETIC hash-reader drift; repository sources stay read-only.
            elif failure in {"child_incomplete", "child_code_mismatch"}:
                manifest = document(directory / "manifest.json")
                if failure == "child_incomplete":
                    manifest["status"] = "incomplete"
                else:
                    manifest["code_hashes"]["scripts/backtest_history.py"] = "f" * 64
                (directory / "manifest.json").write_bytes(canonical_json_bytes(manifest))
            elif failure == "derived_config_drift":
                (output / "configs" / "FEE_X2.json").write_bytes(canonical_json_bytes(BASE))
        return result

    monkeypatch.setattr(cli, "run_history_scenario", injected)
    monkeypatch.setattr(cli, "_code_hashes", hashes)
    assert cli.main(args(source, config, output)) == 2
    assert document(output / "manifest.json")["status"] == "incomplete"
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "incomplete" and result["error_type"] in {"ValueError", "RuntimeError"}


@pytest.mark.parametrize("failure", ["payload", "marker_collision", "partial_marker"])
def test_write_failures_and_manifest_collision_preserve_evidence(cli, inputs, tmp_path, monkeypatch, failure):
    source, config = inputs
    output = tmp_path / "failed"
    marker = output / "manifest.json"
    original = Path.open
    injected = False

    class FailOnce:
        def __init__(self, handle):
            self.handle, self.failed = handle, False

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return self.handle.__exit__(*a)

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def write(self, payload):
            if not self.failed:
                self.failed = True
                self.handle.write(payload[:50])
                raise OSError("SYNTHETIC partial complete-marker write")
            return self.handle.write(payload)

    def fail(path, mode="r", *a, **kw):
        nonlocal injected
        if failure == "payload" and path == output / "comparison.csv" and "x" in mode:
            raise OSError("SYNTHETIC disk failure")
        if path == marker and "x" in mode and not injected:
            injected = True
            if failure == "marker_collision":
                with original(marker, "xb") as handle:
                    handle.write(b"other-writer-sentinel")
            elif failure == "partial_marker":
                return FailOnce(original(path, mode, *a, **kw))
        return original(path, mode, *a, **kw)

    monkeypatch.setattr(Path, "open", fail)
    assert cli.main(args(source, config, output)) == 2
    if failure == "marker_collision":
        assert marker.read_bytes() == b"other-writer-sentinel"
    else:
        manifest = document(marker)
        assert manifest["status"] == "incomplete" and manifest["error_type"] == "OSError"


def test_fresh_process_stays_offline_and_reproduces_bytes(cli, inputs, tmp_path):
    source, config = inputs
    first, second = tmp_path / "first", tmp_path / "second"
    assert cli.main(args(source, config, first)) == 0
    bootstrap = """
import builtins, importlib, io, os, socket, sys, runpy
original_import = builtins.__import__
def checked_import(name, *a, **kw):
    if any(name == p or name.startswith(p + '.') for p in ('dotenv','tqsdk','sqlalchemy','psycopg','requests','httpx','ak_trading.config','ak_trading.runtime','ak_trading.persistence')):
        raise AssertionError('offline forbidden import: ' + name)
    return original_import(name, *a, **kw)
builtins.__import__ = checked_import
def denied(*a, **kw):
    raise AssertionError('network attempted')
socket.socket.connect = socket.socket.connect_ex = socket.create_connection = socket.getaddrinfo = denied
original_open = io.open
def checked_open(path, *a, **kw):
    if isinstance(path, (str, bytes, os.PathLike)) and os.fsdecode(path).replace('\\\\','/').rsplit('/',1)[-1].lower().startswith('.env'):
        raise AssertionError('credential file attempted')
    return original_open(path, *a, **kw)
io.open = checked_open
script = sys.argv.pop(1)
runpy.run_path(script, run_name='__main__')
"""
    environment = {name: os.environ[name] for name in ("PATH", "SYSTEMROOT", "WINDIR") if name in os.environ}
    environment.update(PYTHONHASHSEED="728", PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1",
                       TEMP=str(tmp_path), TMP=str(tmp_path))
    result = subprocess.run([sys.executable, "-B", "-c", bootstrap, str(SCRIPT), *args(source, config, second)],
                            cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert tree(first) == tree(second)
