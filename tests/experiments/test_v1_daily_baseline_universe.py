"""K44 SYNTHETIC regressions. No database, secret or real bar inputs are used."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, Inexact, localcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from ak_trading.futures import io as engine_io
from ak_trading.futures import scenario_backtest as engine


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools/experiments/v1_daily_baseline_universe.py"
SPEC = importlib.util.spec_from_file_location("universe_under_test", SCRIPT)
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)
B, D = m.load_helpers(ROOT)
DEC = Decimal
SYMBOLS = {"INE.SC": "INE.sc2405", "DCE.M": "DCE.m2405", "CZCE.TA": "CZCE.TA405",
           "CFFEX.IF": "CFFEX.IF2405", "GFEX.SI": "GFEX.si2405"}


@pytest.fixture(autouse=True)
def exact_math():
    with localcontext() as ctx:
        ctx.prec = 128
        ctx.traps[Inexact] = True
        yield


def declaration(product, *, symbol=None, year=2024):
    symbol = symbol or SYMBOLS[product]
    expiry = f"{year}-05-15T15:00:00+08:00"
    return dict(product=product, raw_symbol=symbol, expiry_at=expiry, instance_id=symbol + "|" + expiry,
                identity_status="SYNTHETIC")


def raw_input(product="DCE.M", closes=(100, 110, 105), *, oi=None, dates=None, utc=False, declarations=None):
    declarations = declarations or [declaration(product)]
    rows = []
    dates = dates or [datetime(2024, 1, 2, tzinfo=m.SHANGHAI) + timedelta(days=j) for j in range(len(closes))]
    for j, (close, ts) in enumerate(zip(closes, dates)):
        original = ts.astimezone(timezone.utc) if utc else ts
        values = dict.fromkeys(engine_io.BARS_CSV_COLUMNS, "")
        values.update(symbol=SYMBOLS[product], duration_seconds=86400, datetime_nano=m.nano(ts.isoformat()), ts=original.isoformat(),
                      trading_day=ts.date().isoformat(), trading_day_source="SYNTHETIC", open=str(close), high=str(DEC(str(close)) + 1),
                      low=str(DEC(str(close)) - 1), close=str(close), volume="100", open_oi="10",
                      close_oi="10" if oi is None else oi[j], source="SYNTHETIC", available_at="2026-09-01T00:00:00+08:00")
        rows.append(values)
    payloads = {"bars.csv": m.csv_bytes(rows, list(engine_io.BARS_CSV_COLUMNS)), "contracts.json": m.json_bytes(declarations),
                "metadata.json": m.json_bytes({"test_label": "SYNTHETIC"}), "availability.json": m.json_bytes({"test_label": "SYNTHETIC"})}
    manifest = dict(schema_version="futures_bundle_v1", status="complete", dataset_kind="real_export", test_label="SYNTHETIC",
                    request=dict(products=[product], start=m.START, end=m.END, start_nano=m.nano(m.START), end_nano=m.nano(m.END), duration_seconds=86400),
                    read_only_transaction=dict(verified=True, executed=True, observed_read_only="on", observed_isolation="repeatable read"),
                    row_counts=dict(bars=len(rows), contracts=len(declarations)), content_hashes={k: m.pin(v)["sha256"] for k, v in payloads.items()})
    return manifest, payloads


def prepared(product="DCE.M", closes=(100, 110, 105), **kwargs):
    manifest, payloads = raw_input(product, closes, **kwargs)
    return m.inspect_input(product, manifest, payloads)


def synthetic_rb():
    # Accepted-table shape with explicitly SYNTHETIC contents. No RB replay.
    groups = defaultdict(list)
    summary, years, control = B.aggregate_matrix(groups)
    for row in summary:
        row["net_sum"] = DEC(0)
    dominant = []
    for method in reversed(m.METHODS):
        dominant.append(dict(product="SHFE.RB", method=method, method_name=m.NAMES[method], status="COVERED", net=DEC(0),
                            positive_years=0, covered_years=1, worst_year=2024, worst_year_net=DEC(0), max_drawdown=DEC(0),
                            longest_drawdown_observations=0, open_drawdown_at_end=False, covered_observations=2,
                            total_observations=3, coverage_percent="66.666667", all_accounts=1, all_accounts_net=DEC(0),
                            all_accounts_mean_numerator=DEC(0), all_accounts_mean_denominator=1, all_accounts_mean_display="0.000"))
    year = [dict(product="SHFE.RB", method=method, year=2024, year_status="OBSERVED_RAW_YEAR", status="COVERED", net=DEC(0),
                 covered_observations=2, total_observations=3) for method in m.METHODS]
    scope = [dict(metric=k, value=v) for k, v in dict(input_window_start="2024-01-02", input_window_end="2024-01-04",
        covered_observations=2, observations=3, coverage_percent="66.666667", switches=0).items()]
    return {name: B.csv_bytes(records) for name, records in zip(m.TABLES, (summary, years, control, dominant, year, scope))}


def context(tmp_path, name, *, active=m.NEW_PRODUCTS, no_oi=False):
    specimens = {product: prepared(product, oi=("", "", "") if no_oi else None) for product in active}
    entries = {product: dict(status="READY" if product in active else "NO_DATA", reason_code="VALIDATED" if product in active else "no_rows_in_requested_range",
        export_raw_exit=0 if product in active else 3, read_only_snapshot_verified=True, validated_export_bar_rows=0,
        bundle=dict(manifest=dict(sha256="a" * 64)) if product in active else None) for product in m.NEW_PRODUCTS}
    specs = {product: dict(multiplier=m.ECON[product][0], price_tick=m.ECON[product][1], source=dict(bytes=1, sha256=m.ECON[product][2]),
        source_url="https://synthetic.invalid/contract", author_verification="SYNTHETIC") for product in m.NEW_PRODUCTS}
    rb_available = dict(product="SHFE.RB", status="REUSED_ACCEPTED", reason_code="REUSED_ACCEPTED_RB", declared_contracts=1,
        contracts_with_bars=1, contracts_without_bars=0, bar_rows=3, start_raw_date="2024-01-02", end_raw_date="2024-01-04",
        export_raw_exit=None, read_only_snapshot_verified=True, input_manifest_sha256="b" * 64, economic_specs_status="SYNTHETIC", new_simulation=False)
    fake_build = {name: m.pin(("SYNTHETIC:" + name).encode()) for name in m.BUILD_FILES}
    result = SimpleNamespace(repo=ROOT, output=tmp_path / name, sources=m.Sources(), config=m.fixed_config(), b=B, dominant=D,
        rb=synthetic_rb(), rb_availability=rb_available, prepared=specimens, inputs=dict(products=entries), specs=dict(products=specs),
        source_hash="1" * 64, input_hash="2" * 64, build_hash="3" * 64, logical_sources={"SYNTHETIC": m.pin(b"SYNTHETIC")}, build_pins=fake_build)
    return result


def receipt_for(context, tmp_path):
    logs = tmp_path / (context.output.name + "-logs")
    logs.mkdir()
    streams = {}
    for name in ("stdout", "stderr"):
        filename = logs / (name + ".txt")
        filename.write_bytes(b"SYNTHETIC\n")
        streams[name] = dict(path=str(filename), **m.pin(filename.read_bytes()))
    argv = [sys.executable, "-B", "-X", "utf8", str(SCRIPT), "matrix", "--source-repo", str(ROOT), "--output-dir", str(context.output)]
    for name in ("source", "input", "build"):
        filename = tmp_path / (name + ".json")
        if not filename.exists():
            filename.write_bytes(m.json_bytes(dict(test_label="SYNTHETIC", kind=name)))
        digest = m.pin(filename.read_bytes())["sha256"]
        setattr(context, name + "_hash", digest)
        argv += ["--" + name + "-bindings", str(filename), "--" + name + "-bindings-sha256", digest]
    return dict(schema="k44-matrix-terminal/v1", raw_exit_subject="matrix_child", raw_exit=0, terminated=True, streams_closed=True,
        timed_out=False, pid=123, task_id="SYNTHETIC-not-a-real-process", started_utc="2026-11-13T00:00:00+00:00", ended_utc="2026-11-13T00:00:01+00:00",
        argv=argv, cwd=str(ROOT), output_dir=str(context.output), source_bindings_sha256=context.source_hash,
        input_bindings_sha256=context.input_hash, build_bindings_sha256=context.build_hash, closed_streams=streams)


@pytest.mark.parametrize("product", m.NEW_PRODUCTS)
def test_five_products_exact_identity_and_empty_declaration_support(product):
    second = declaration(product, symbol=SYMBOLS[product][:-2] + "06")
    data = prepared(product, declarations=[declaration(product), second])
    assert data.stats == dict(declared_contracts=2, contracts_with_bars=1, contracts_without_bars=1, bar_rows=3,
                             start_raw_date="2024-01-02", end_raw_date="2024-01-04")
    assert all(row["contract_instance_id"] == declaration(product)["instance_id"] for row in data.bars[SYMBOLS[product]])


@pytest.mark.parametrize("product", m.NEW_PRODUCTS)
def test_fixed_single_factor_matrix(product):
    configs = list(m.matrix_configs(product, m.fixed_config()))
    assert len(configs) == 16
    for capital, scenario, values in configs:
        assert values["initial_cash"] == capital and (values["multiplier"], values["price_tick"]) == m.ECON[product][:2]
        changed = {key for key in m.BASE if key != "initial_cash" and values[key] != m.BASE[key]}
        assert changed == {"BASE": set(), "FEE_X2": {"fee_per_lot", "fee_rate"}, "SLIPPAGE_X2": {"slippage_ticks"}, "MARGIN_X1_5": {"margin_rate"}}[scenario]


@pytest.mark.parametrize("mutation", ["unknown", "order", "missing", "economic", "risk"])
def test_config_rejects_every_nonfixed_input(mutation):
    value = copy.deepcopy(m.fixed_config())
    if mutation == "unknown":
        value["extra"] = True
    elif mutation == "order":
        value["new_product_order"].reverse()
    elif mutation == "missing":
        del value["views"]
    elif mutation == "economic":
        value["economics"]["GFEX.SI"]["price_tick"] = "1"
    else:
        value["base_assumptions"]["max_margin_ratio"] = "1"
    with pytest.raises(m.ValidationError, match="fixed_configuration"):
        list(m.matrix_configs("DCE.M", value))


def test_config_on_disk_is_exact_and_strict_json():
    assert m.loads((ROOT / m.CONFIG).read_bytes()) == m.fixed_config()
    with pytest.raises(m.ValidationError, match="duplicate_json_key"):
        m.loads(b'{"a":1,"a":2}')
    with pytest.raises(m.ValidationError, match="nonfinite_json"):
        m.loads(b'{"a":NaN}')


@pytest.mark.parametrize("declarations,reason", [
    ([declaration("CZCE.TA"), declaration("CZCE.TA", year=2034)], "ambiguous_symbol_instances"),
    ([declaration("CZCE.TA"), declaration("CZCE.TA")], "duplicate_instance"),
    ([declaration("CZCE.TA", symbol="CZCE.TA2405")], "delivery_code"),
])
def test_ambiguous_or_wrong_instance_is_rejected(declarations, reason):
    with pytest.raises(m.InputError, match=reason):
        prepared("CZCE.TA", declarations=declarations)


def test_timezone_normalization_preserves_input_and_instant():
    manifest, payloads = raw_input(utc=True)
    before = copy.deepcopy(payloads)
    data = m.inspect_input("DCE.M", manifest, payloads)
    local = prepared()
    assert payloads == before
    assert data.bars == local.bars
    assert all(row["original_ts"].endswith("+00:00") and row["normalized_raw_ts"].endswith("+08:00") for row in data.mapping)
    assert [row["datetime_nano"] for row in data.mapping] == [row["datetime_nano"] for row in local.mapping]


@pytest.mark.parametrize("fault,code", [("naive", "BLOCKED_INPUT_VALIDATION"), ("late", "BLOCKED_INPUT_DATE_BOUNDARY"),
    ("early", "BLOCKED_INPUT_DATE_BOUNDARY"), ("trading_day", "BLOCKED_INPUT_DATE_BOUNDARY"),
    ("duplicate", "BLOCKED_INPUT_VALIDATION"), ("negative_oi", "BLOCKED_INPUT_VALIDATION"),
    ("infinite_oi", "BLOCKED_INPUT_VALIDATION"), ("wrong_nano", "BLOCKED_INPUT_VALIDATION"), ("bad_ohlc", "BLOCKED_INPUT_VALIDATION")])
def test_input_boundaries_fail_without_dropping_rows(fault, code):
    manifest, payloads = raw_input()
    records = m.rows(payloads["bars.csv"])
    if fault == "naive":
        records[0]["ts"] = "2024-01-02T00:00:00"
    elif fault in ("late", "early"):
        records[0]["ts"] = "2026-09-04T16:00:00+00:00" if fault == "late" else "2020-07-19T15:59:00+00:00"
        records[0]["close"] = "not-a-price"  # The date guard must win before any numerical read.
    elif fault == "trading_day":
        records[0]["trading_day"] = "2026-09-05"
    elif fault == "duplicate":
        records.append(records[0])
    elif fault in ("negative_oi", "infinite_oi"):
        records[0]["close_oi"] = "-1" if fault == "negative_oi" else "Infinity"
    elif fault == "wrong_nano":
        records[0]["datetime_nano"] = str(int(records[0]["datetime_nano"]) + 1)
    else:
        records[0]["low"] = "10000"
    payloads["bars.csv"] = m.csv_bytes(records, list(engine_io.BARS_CSV_COLUMNS))
    with pytest.raises(m.InputError) as failure:
        m.inspect_input("DCE.M", manifest, payloads)
    assert failure.value.code == code


@pytest.mark.parametrize("product,price,expected_slip,expected_fee", [("CFFEX.IF", 4000, "60", "120.01"), ("INE.SC", 400, "100", "40.01")])
def test_fractional_ticks_large_multipliers_exact_costs(product, price, expected_slip, expected_fee):
    data = prepared(product, (price, price))
    view = m.bundle_views(data, engine_io, B)[SYMBOLS[product]]
    values = next(m.matrix_configs(product, m.fixed_config()))[2]
    result = B.control_account(view.bars, declaration(product), "buy_hold_control", values)
    ledger = B.verify_account(view.bars, declaration(product), "buy_hold_control", "1000000", "BASE", values, result)
    assert result["fills"][0]["slippage"] == DEC(expected_slip)
    assert result["fills"][0]["fee"] == DEC(expected_fee)
    assert ledger[-1]["net"] == -(DEC(expected_slip) + DEC(expected_fee)) * 2


@pytest.mark.parametrize("capital,opened", [("1000000", True), ("300000", False), ("100000", False), ("50000", False)])
def test_if_capital_admission_and_first_open_never_retries(capital, opened):
    data = prepared("CFFEX.IF", (4000, 10, 10))
    view = m.bundle_views(data, engine_io, B)[SYMBOLS["CFFEX.IF"]]
    values = next(values for c, s, values in m.matrix_configs("CFFEX.IF", m.fixed_config()) if (c, s) == (capital, "BASE"))
    result = B.control_account(view.bars, declaration("CFFEX.IF"), "buy_hold_control", values)
    ledger = B.verify_account(view.bars, declaration("CFFEX.IF"), "buy_hold_control", capital, "BASE", values, result)
    assert len(result["fills"]) == (2 if opened else 0)
    assert sum(row["opening_attempts"] for row in ledger) == 1
    if not opened:
        assert all(row["net"] == 0 and row["position"] == 0 for row in ledger)
        assert ledger[0]["opening_margin_blocked"] == 1
    flat = B.control_account(view.bars, declaration("CFFEX.IF"), "flat_control", values)
    assert flat["fills"] == [] and all(row["equity"] == DEC(capital) for row in flat["equity"])


def test_selector_prior_oi_missing_zero_tie_and_future_independence():
    first = m.selected_bars(prepared(oi=("0", "", "100")))
    second_symbol = "DCE.m2406"
    second = copy.deepcopy(first[SYMBOLS["DCE.M"]])
    for row in second:
        row["symbol"] = second_symbol
        row["contract_instance_id"] = second_symbol + "|2024-06-15T15:00:00+08:00"
    bars = dict(first, **{second_symbol: second})
    selected = D.select_dominant(bars)
    assert selected[0]["selected_key"] is None
    assert selected[1]["selected_contract"] == SYMBOLS["DCE.M"] and selected[1]["max_oi_ties"] == 2
    assert selected[2]["selected_key"] is None
    bars[SYMBOLS["DCE.M"]][1]["close_oi"] = DEC(999999)
    changed = D.select_dominant(bars)
    assert changed[1] == selected[1]


def test_dominant_cross_year_drawdown_gap_recovery_and_tie_year():
    dates = [datetime(2023, 12, 30, tzinfo=m.SHANGHAI) + timedelta(days=j) for j in range(5)]
    data = prepared(closes=(100,) * 5, dates=dates)
    bars = m.selected_bars(data)
    selection = D.select_dominant(bars)
    # Keep an underwater missing observation across a year boundary.
    selection[2]["selected_key"] = None
    selection[2]["selected_contract"] = selection[2]["selected_instance"] = None
    selection[2]["status"] = "NO_ELIGIBLE_PREVIOUS_OI"
    keys = [m.ledger_key(row) for row in bars[SYMBOLS["DCE.M"]]]
    nets = dict(zip(keys, map(DEC, ("100", "-10", "0", "10", "-3"))))
    result, years, curves = m.dominant_cell("DCE.M", "50000", "BASE", "ma_trend", selection, nets, 1, DEC(97))
    assert result["net"] == -3 and result["max_drawdown"] == 10
    assert result["longest_drawdown_observations"] == 2
    assert curves[2]["selected_net"] is None and curves[2]["drawdown"] == 10 and curves[2]["underwater_observations"] == 2
    assert curves[3]["drawdown"] == 0 and curves[3]["underwater_observations"] == 0
    assert sum(row["net"] or 0 for row in years) == -3
    scope = m.dominant_scope("DCE.M", data, selection, m.selection_csv(selection))
    assert scope["switches"] == 0


def test_no_coverage_is_null_not_zero_and_wrong_support_rejects():
    data = prepared(oi=("", "", ""))
    bars = m.selected_bars(data)
    selection = D.select_dominant(bars)
    nets = {m.ledger_key(row): DEC(0) for row in bars[SYMBOLS["DCE.M"]]}
    result, annual, curves = m.dominant_cell("DCE.M", "50000", "BASE", "flat_control", selection, nets, 1, DEC(0))
    assert result["net"] is None and result["max_drawdown"] is None and result["all_accounts_net"] == 0
    assert all(row["net"] is None for row in annual)
    assert all(row["cumulative_net"] is None for row in curves)
    good = D.select_dominant(m.selected_bars(prepared()))
    with pytest.raises(m.ValidationError, match="selected_support_mismatch"):
        m.dominant_cell("DCE.M", "50000", "BASE", "flat_control", good, {}, 1, DEC(0))


def test_source_mutation_and_path_rejection(tmp_path):
    filename = tmp_path / "source.json"
    filename.write_bytes(b"one")
    sources = m.Sources()
    sources.read("source", filename, m.pin(b"one"))
    filename.write_bytes(b"two")
    with pytest.raises(m.ValidationError, match="source_mismatch"):
        sources.verify()
    for value in (42, tmp_path / ".." / "escape", tmp_path / ".env", tmp_path / "final-holdout"):
        with pytest.raises(m.ValidationError):
            m.path(value)


@pytest.fixture(scope="module")
def complete(tmp_path_factory):
    base = tmp_path_factory.mktemp("k44-complete-SYNTHETIC")
    contexts, manifests, receipts, finals = [], [], [], []
    calls = []
    class EngineProxy:
        ScenarioConfig = engine.ScenarioConfig
        ScenarioFill = engine.ScenarioFill
        ScenarioEquity = engine.ScenarioEquity
        ScenarioDecision = engine.ScenarioDecision

        @staticmethod
        def run_scenario_backtest(view, **kwargs):
            calls.append((kwargs["symbol"], kwargs["strategy"]))
            return engine.run_scenario_backtest(view, **kwargs)
    with localcontext() as ctx:
        ctx.prec = 128
        ctx.traps[Inexact] = True
        for attempt in ("first", "second"):
            current = context(base, attempt)
            receipt = receipt_for(current, base)
            manifest = m.run_matrix(current, EngineProxy, engine_io)
            receipt["manifest"] = m.pin((current.output / "manifest.json").read_bytes())
            m.validate_inventory(current, manifest)
            assert not any((current.output / name).exists() for name in m.final_checks())
            final = m.seal(current, receipt)
            contexts.append(current)
            manifests.append(manifest)
            receipts.append(receipt)
            finals.append(final)
    return SimpleNamespace(base=base, contexts=contexts, manifests=manifests, receipts=receipts, finals=finals, calls=calls)


def test_two_complete_matrices_and_checks_byte_equal(complete):
    first, second = complete.contexts
    a = {p.relative_to(first.output).as_posix(): p.read_bytes() for p in first.output.rglob("*") if p.is_file()}
    b = {p.relative_to(second.output).as_posix(): p.read_bytes() for p in second.output.rglob("*") if p.is_file()}
    assert a == b
    assert len([name for name in a if name.startswith("publish/")]) == 46
    assert complete.finals[0] == complete.finals[1]
    assert complete.finals[0]["simulated_products"] == list(m.NEW_PRODUCTS)


def test_full_matrix_80_cells_shared_selection_and_no_rb_engine_calls(complete):
    assert len(complete.calls) == 2 * 5 * 48
    assert not any(symbol.startswith("SHFE.") for symbol, _ in complete.calls)
    current = complete.contexts[0]
    for product in m.NEW_PRODUCTS:
        public = current.output / "publish" / m.PUBLIC / product
        assert [len(m.rows((public / filename).read_bytes())) for filename in m.TABLES] == [160, 1120, 1536, 80, 560, 1]
        curves = m.rows((current.output / "products" / product / "dominant-curves.csv").read_bytes())
        selections = defaultdict(set)
        for row in curves:
            selections[row["raw_date"]].add((row["selected_contract"], row["selected_instance"]))
        assert all(len(values) == 1 for values in selections.values())
        assert len(curves) == 80 * 3
    for filename, raw in current.rb.items():
        assert (current.output / "publish" / m.PUBLIC / "SHFE.RB" / filename).read_bytes() == raw


def test_report_numeric_refs_and_fixed_thirty_rows(complete):
    current = complete.contexts[0]
    records = m.rows((current.output / "publish" / m.PUBLIC / "summary.csv").read_bytes())
    assert len(records) == 30
    assert [(r["product"], r["method"]) for r in records] == [(p, method) for p in m.PRODUCTS for method in m.METHODS]
    assert all(r["capital"] == "1000000" and r["scenario"] == "BASE" for r in records)
    for ref in complete.finals[0]["report_value_refs"]:
        table = m.rows((current.output / "publish" / m.PUBLIC / ref["csv"]).read_bytes())
        assert table[ref["row"] - 2][ref["column"]] == ref["value"]
    assert complete.finals[0]["report_source_refs"]
    assert not any(r["product"] == "TOTAL" for r in records)
    assert complete.finals[0]["new_training"] is False and complete.finals[0]["holdout_read"] is False


def test_blocked_product_does_not_block_others_and_all_blocked_is_honest(tmp_path):
    for label, active in (("partial", ("DCE.M",)), ("blocked", ())):
        current = context(tmp_path, label, active=active)
        receipt = receipt_for(current, tmp_path)
        result = m.run_matrix(current, engine, engine_io)
        receipt["manifest"] = m.pin((current.output / "manifest.json").read_bytes())
        final = m.seal(current, receipt)
        assert final["new_simulation"] == bool(active)
        assert final["coverage_status"] == ("PARTIAL_NEW_PRODUCT_COVERAGE" if active else "BLOCKED")
        summary = m.rows((current.output / "publish" / m.PUBLIC / "summary.csv").read_bytes())
        unavailable = [row for row in summary if row["product"] in set(m.NEW_PRODUCTS) - set(active)]
        assert all(row["dominant_net"] == row["all_accounts_net"] == "" for row in unavailable)
        for product in set(m.NEW_PRODUCTS) - set(active):
            assert result["counts"][product]["accounts"] == 0


@pytest.mark.parametrize("fault", ["nonzero", "not_terminal", "open_stream", "wrong_subject", "wrong_output", "wrong_manifest", "wrong_mode", "wrong_binding", "stream_tamper"])
def test_receipt_counterexamples_preserve_failure(complete, tmp_path, fault):
    current = complete.contexts[0]
    receipt = copy.deepcopy(complete.receipts[0])
    if fault == "nonzero":
        receipt["raw_exit"] = 7
    elif fault == "not_terminal":
        receipt["terminated"] = False
    elif fault == "open_stream":
        receipt["streams_closed"] = False
    elif fault == "wrong_subject":
        receipt["raw_exit_subject"] = "supervisor"
    elif fault == "wrong_output":
        receipt["output_dir"] = str(tmp_path)
    elif fault == "wrong_manifest":
        receipt["manifest"]["sha256"] = "0" * 64
    elif fault == "wrong_mode":
        receipt["argv"][receipt["argv"].index("matrix")] = "seal"
    elif fault == "wrong_binding":
        receipt["input_bindings_sha256"] = "0" * 64
    else:
        filename = tmp_path / "changed.txt"
        filename.write_bytes(b"changed")
        receipt["closed_streams"]["stdout"]["path"] = str(filename)
    with pytest.raises((m.ValidationError, SystemExit)):
        m.verify_matrix_receipt(current, receipt, (current.output / "manifest.json").read_bytes())


def test_existing_output_and_second_seal_are_rejected(complete):
    current = complete.contexts[0]
    with pytest.raises(m.ValidationError, match="output_already_exists"):
        m.run_matrix(current, engine, engine_io)
    with pytest.raises(m.ValidationError, match="actual_payload_set_or_existing_check"):
        m.seal(current, complete.receipts[0])


def test_inventory_missing_extra_or_tampered_payload_rejected(tmp_path):
    current = context(tmp_path, "inventory", active=())
    receipt_for(current, tmp_path)
    manifest = m.run_matrix(current, engine, engine_io)
    target = current.output / "config.json"
    original = target.read_bytes()
    target.write_bytes(b"{}\n")
    with pytest.raises(m.ValidationError, match="payload_hash"):
        m.validate_inventory(current, manifest)
    target.write_bytes(original)
    extra = current.output / ".CHECK.json-incomplete"
    extra.write_bytes(b"SYNTHETIC failure retained")
    with pytest.raises(m.ValidationError, match="actual_payload_set_or_existing_check"):
        m.validate_inventory(current, manifest)
    bad = copy.deepcopy(manifest)
    del bad["payloads"]["config.json"]
    with pytest.raises(m.ValidationError, match="complete_expected_payload_inventory"):
        m.validate_inventory(current, bad)


@pytest.mark.parametrize("argv", [["matrix", "--source-bind", "x"], ["matrix", "--source-repo", "x", "--source-repo", "y"],
    ["matrix", "--product", "SHFE.RB"], ["seal", "--skip-hash"]])
def test_cli_invalid_options_native_exit_two(argv):
    result = subprocess.run([sys.executable, "-B", "-X", "utf8", str(SCRIPT), *argv], capture_output=True, text=True)
    assert result.returncode == 2
    assert "error:" in result.stderr


def test_no_numpy_or_database_import_side_effect():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "import numpy" not in source and "import sqlalchemy" not in source and "import dotenv" not in source


@pytest.mark.parametrize("value,raw_exit,status", [
    (dict(status="published", reasons=[], row_counts=dict(bars=3, contracts=1)), 0, "published"),
    (dict(status="empty_real_export", reasons=["no_rows_in_requested_range"], row_counts=dict(valid_rows=0)), 3, "empty_real_export"),
    (dict(status="export_blocked", reason="database_connection_failed"), 3, "export_blocked"),
    (dict(status="validation_rejected", reasons=["duplicate_keys=2", "invalid_ohlc=0"]), 3, "validation_rejected"),
    (dict(status="scope_limit", reasons=["max_contracts_exceeded:601>600"]), 3, "scope_limit"),
    (dict(status="published", reasons=[]), 3, "UNCLASSIFIED_EXPORT_FAILURE"),
    (dict(status="export_blocked", reason="postgresql://synthetic-user:synthetic-secret@invalid"), 3, "UNCLASSIFIED_EXPORT_FAILURE"),
    (dict(status="unexpected", reason="never print this synthetic text"), 1, "UNCLASSIFIED_EXPORT_FAILURE"),
])
def test_export_status_allowlist_never_relabels_failure_or_exposes_unknown_text(value, raw_exit, status):
    value["read_only_transaction"] = dict(verified=True, observed_read_only="on", observed_isolation="repeatable read")
    value["unrelated_sensitive_exception"] = "synthetic-secret-must-not-be-reported"
    result = m.export_outcome(m.json_bytes(value), raw_exit)
    assert result["status"] == status
    assert "synthetic-secret" not in json.dumps(result)
    assert "never print" not in json.dumps(result)


def test_csv_schema_failure_is_product_input_validation():
    manifest, payloads = raw_input()
    payloads["bars.csv"] = b"symbol,symbol\nDCE.m2405,DCE.m2405\n"
    with pytest.raises(m.InputError, match="BLOCKED_INPUT_VALIDATION:invalid_bar_csv"):
        m.inspect_input("DCE.M", manifest, payloads)


def test_computation_failure_is_not_downgraded_to_unavailable(tmp_path, monkeypatch):
    current = context(tmp_path, "failed-computation", active=("DCE.M",))
    def fail(*args, **kwargs):
        raise ArithmeticError("SYNTHETIC accounting failure")
    monkeypatch.setattr(m, "run_product", fail)
    with pytest.raises(ArithmeticError, match="accounting failure"):
        m.run_matrix(current, engine, engine_io)
    assert not (current.output / "manifest.json").exists()
    assert not any((current.output / relative).exists() for relative in m.final_checks())


def bundle_locations(root):
    return dict(root=str(root), manifest=dict(path=str(root / "manifest.json")),
                payloads={name: dict(path=str(root / name)) for name in m.PAYLOAD_MEMBERS})


@pytest.mark.parametrize("product", m.NEW_PRODUCTS)
@pytest.mark.parametrize("status", ["READY", "BLOCKED_INPUT_VALIDATION", "BLOCKED_INPUT_DATE_BOUNDARY", "BLOCKED_ECONOMIC_SPECS"])
def test_frozen_bundle_descendant_output_rejected_for_every_product_and_status(tmp_path, product, status):
    root = tmp_path / "frozen" / product
    products = {key: dict(status="NO_DATA", bundle=None) for key in m.NEW_PRODUCTS}
    products[product] = dict(status=status, bundle=bundle_locations(root))
    output = root / "new-matrix"
    with pytest.raises(m.ValidationError, match="output_input_bundle_overlap"):
        m.guard_bundle_output(output, products)
    assert not output.exists()


@pytest.mark.parametrize("relation", ["equal", "ancestor"])
def test_bundle_root_equal_or_below_output_rejected(tmp_path, relation):
    root = tmp_path / "frozen" / "DCE.M"
    products = {key: dict(bundle=None) for key in m.NEW_PRODUCTS}
    products["DCE.M"] = dict(bundle=bundle_locations(root))
    output = root if relation == "equal" else root.parent
    with pytest.raises(m.ValidationError, match="output_input_bundle_overlap"):
        m.guard_bundle_output(output, products)


@pytest.mark.parametrize("fault", ["root", "manifest", "bars", "payload_name"])
def test_declared_bundle_root_cannot_hide_actual_payload_location(tmp_path, fault):
    root = tmp_path / "frozen"
    bundle = bundle_locations(root)
    if fault == "root":
        bundle["root"] = str(tmp_path / "decoy")
    elif fault == "manifest":
        bundle["manifest"]["path"] = str(tmp_path / "elsewhere" / "manifest.json")
    elif fault == "bars":
        bundle["payloads"]["bars.csv"]["path"] = str(tmp_path / "elsewhere" / "bars.csv")
    else:
        bundle["payloads"]["bars.csv"]["path"] = str(root / "renamed.csv")
    products = {key: dict(bundle=None) for key in m.NEW_PRODUCTS}
    products["DCE.M"] = dict(bundle=bundle)
    with pytest.raises(m.ValidationError, match="bundle_(manifest|payload)_root_identity"):
        m.guard_bundle_output(tmp_path / "safe-output", products)


def test_separate_output_and_canonical_equivalent_root_are_accepted(tmp_path):
    root = tmp_path / "frozen"
    products = {key: dict(bundle=None) for key in m.NEW_PRODUCTS}
    products["DCE.M"] = dict(bundle=bundle_locations(root))
    products["DCE.M"]["bundle"]["root"] = str(root / ".")
    output = tmp_path / "frozen-other" / "matrix"
    m.guard_bundle_output(output, products)
    assert not output.exists()


@pytest.mark.parametrize("mode", ["matrix", "seal"])
def test_complete_synthetic_preflight_reaches_guard_before_any_execution(tmp_path, monkeypatch, capsys, mode):
    """Mock only provenance reads; execute main and the complete preflight path.

    Every mocked record is explicitly SYNTHETIC. Real input, engine, config
    changes and new report files are neither prerequisites nor accessed here.
    """
    repo, frozen = tmp_path / "repo", tmp_path / "frozen-input"
    config = repo / m.CONFIG
    config.parent.mkdir(parents=True)
    config.write_bytes(m.json_bytes(m.fixed_config()))
    frozen.mkdir()
    for name in ("manifest.json", *sorted(m.PAYLOAD_MEMBERS)):
        (frozen / name).write_bytes(b"SYNTHETIC source bytes must stay unchanged\n")
    output = frozen / "deep" / "nested" / "matrix"
    if mode == "seal":
        output.mkdir(parents=True)
    original_sources = {p.relative_to(frozen).as_posix(): p.read_bytes() for p in frozen.rglob("*") if p.is_file()}
    source_file, input_file, build_file = (tmp_path / (name + ".json") for name in ("source", "input", "build"))
    engine_root = tmp_path / "SYNTHETIC-engine-never-imported"
    engine_sources = {"engine.file" + str(i) + ".py": dict(sha256="e" * 64, bytes=0) for i in range(10)}
    physical = {name: str(engine_root / name.removeprefix("engine.")) for name in engine_sources}
    original = dict(source_pins={**engine_sources, "input.manifest": dict(sha256="b" * 64, bytes=0)},
                    physical_locations={**physical, "input.manifest": str(tmp_path / "SYNTHETIC-RB-manifest.json")},
                    accepted_counts=dict(rb_declarations=1, rb_bars=3))
    authority_raw = {"K40B_source_freeze": m.json_bytes(dict(source_bindings=original)),
        "K40B_verdict": m.json_bytes(dict(verdict="PASS", conditional=False, unconditional=True, reviewed_files={})),
        "K43_verdict": m.json_bytes(dict(verdict="PASS", conditional=False, unconditional=True, reviewed_files={}))}
    authority_raw.update({m.RB_PUBLIC + "/" + name: raw for name, raw in synthetic_rb().items()})
    authority = dict(schema="k44-author-source-bindings/v1", engine_source_root=str(engine_root),
        sources={name: dict(path=str(tmp_path / "SYNTHETIC-authority" / (str(i) + ".json")), **m.pin(raw))
                 for i, (name, raw) in enumerate(authority_raw.items())},
        economic_specs=dict(path=str(tmp_path / "SYNTHETIC-economics.json"), sha256=m.ECON_HASH, bytes=0))
    build = dict(schema="k44-build-bindings/v1", status="FROZEN_AFTER_COMPLETE_SYNTHETIC_TESTS",
        plan=dict(path=str(tmp_path / "SYNTHETIC-plan.md"), sha256=m.PLAN_HASH, bytes=0),
        files={name: m.pin((ROOT / name).read_bytes()) for name in m.BUILD_FILES},
        targeted_receipt=dict(path=str(tmp_path / "SYNTHETIC-tests-receipt.json"), sha256="f" * 64, bytes=0),
        synthetic_determinism="TWO_COMPLETE_PAYLOADS_AND_CHECKS_BYTE_EQUAL")
    products = {product: dict(status="NO_DATA", bundle=None) for product in m.NEW_PRODUCTS}
    products["DCE.M"] = dict(status="READY", bundle=bundle_locations(frozen))
    fake_reads = {"source-bindings": m.json_bytes(authority), "build-bindings": m.json_bytes(build),
        "economic-specs": m.json_bytes(dict(products={product: {} for product in m.NEW_PRODUCTS})),
        "build-targeted-receipt": m.json_bytes(dict(raw_exit=0, terminated=True, streams_closed=True)),
        "RB.input.manifest": m.json_bytes(dict(read_only_transaction=dict(verified=True))),
        "input-bindings": m.json_bytes(dict(schema="k44-input-bindings/v1", source_bindings_sha256=m.SOURCE_HASH, products=products)),
        "effective-plan": b"SYNTHETIC\n"}
    fake_reads.update({"authority." + name: raw for name, raw in authority_raw.items()})
    fake_reads.update({name: b"SYNTHETIC\n" for name in engine_sources})
    fake_reads.update({"build." + name: (ROOT / name).read_bytes() for name in m.BUILD_FILES})
    calls = dict(guard=0, engine=0, writes=0)
    original_guard = m.guard_bundle_output
    def read_synthetic(_sources, name, location, expected):
        return fake_reads[name]
    def traced_guard(*args):
        calls["guard"] += 1
        return original_guard(*args)
    def forbidden(*args, **kwargs):
        calls["engine"] += 1
        raise AssertionError("execution must not precede the directory guard")
    monkeypatch.setattr(m.Sources, "read", read_synthetic)
    monkeypatch.setattr(m, "guard_bundle_output", traced_guard)
    monkeypatch.setattr(m, "load_helpers", forbidden)
    monkeypatch.setattr(m, "run_matrix", forbidden)
    monkeypatch.setattr(m, "seal", forbidden)
    argv = [mode, "--source-bindings", str(source_file), "--source-bindings-sha256", m.SOURCE_HASH,
            "--input-bindings", str(input_file), "--input-bindings-sha256", "1" * 64,
            "--build-bindings", str(build_file), "--build-bindings-sha256", "2" * 64]
    if mode == "matrix":
        argv += ["--source-repo", str(repo), "--output-dir", str(output)]
    else:
        receipt = tmp_path / "SYNTHETIC-matrix-receipt.json"
        receipt.write_bytes(m.json_bytes(dict(argv=["SYNTHETIC-python", "matrix", "--source-repo", str(repo)])))
        argv += ["--matrix-dir", str(output), "--matrix-receipt", str(receipt)]
    assert m.main(argv) == 2
    assert json.loads(capsys.readouterr().err)["reason"] == "output_input_bundle_overlap"
    assert calls == dict(guard=1, engine=0, writes=0)
    assert output.exists() is (mode == "seal")
    assert {p.relative_to(frozen).as_posix(): p.read_bytes() for p in frozen.rglob("*") if p.is_file()} == original_sources
