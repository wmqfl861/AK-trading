"""SYNTHETIC K43 accounting/selection/custody tests; no external real inputs."""
from __future__ import annotations

import ast
import copy
import importlib.util
import json
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, Inexact, localcontext
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools/experiments/v1_daily_baseline_dominant.py"
spec = importlib.util.spec_from_file_location("k43_dominant_test_module", TOOL)
k = importlib.util.module_from_spec(spec)
spec.loader.exec_module(k)


@pytest.fixture(autouse=True)
def exact_arithmetic():
    with localcontext() as context:
        context.prec = 160
        context.traps[Inexact] = True
        yield


def make_context(tmp_path, days=None, oi=None, deltas=None, output="run"):
    """Construct small labeled SYNTHETIC accounts, not a real acceptance claim."""
    days = days or ["2020-12-30", "2020-12-31", "2021-01-04", "2021-01-05"]
    oi = oi or [["10", "2", "0", "4"], ["2", "10", "0", "4"]]
    symbols = {f"SHFE.rb20{n + 1:02d}": f"SHFE.rb20{n + 1:02d}|SYNTHETIC" for n in range(len(oi))}
    bar_rows, declarations, a_rows, control_raw, summary = [], [], [], {}, []
    totals = {m: Decimal(0) for m in k.METHODS}
    for n, (symbol, instance) in enumerate(symbols.items()):
        declarations.append(dict(raw_symbol=symbol, instance_id=instance))
        for j, day in enumerate(days):
            bar_rows.append(dict(symbol=symbol, duration_seconds="86400", datetime_nano=str(k.nano(day + "T00:00:00+08:00")),
                                 ts=day + "T00:00:00+08:00", trading_day=day, close_oi=oi[n][j], available_at="2026-09-06T18:16:03+08:00"))
        for m in k.METHODS:
            records = []
            equity, gross, fee, slip = k.INITIAL, Decimal(0), Decimal(0), Decimal(0)
            for j, day in enumerate(days):
                net_delta = Decimal(str(deltas[n][j] if deltas else (-3, 2, -1, 4)[j % 4])) if m != "flat_control" else Decimal(0)
                fee_delta = Decimal("0.1") if j == len(days) - 1 and m != "flat_control" else Decimal(0)
                prior = equity
                equity += net_delta
                gross += net_delta + fee_delta
                fee += fee_delta
                common = dict(symbol=symbol, contract_instance_id=instance, j=str(j), datetime_nano=str(k.nano(day + "T00:00:00+08:00")),
                              raw_ts=day + "T00:00:00+08:00", raw_date=day, raw_year=day[:4], terminal=k.cell(j == len(days) - 1),
                              previous_equity=k.cell(prior), equity=k.cell(equity), net_delta=k.cell(net_delta),
                              gross_delta=k.cell(net_delta + fee_delta), fee_delta=k.cell(fee_delta), slippage_delta="0",
                              slippage="0", cash=k.cell(equity), realized_pnl=k.cell(gross), unrealized_pnl="0",
                              terminal_close_fills="1" if fee_delta else "0")
                if m in k.STRATEGIES:
                    common.update(strategy=m, primary_support=k.cell(j >= 60), gross_pnl=k.cell(gross), fees=k.cell(fee),
                                  net_pnl=k.cell(equity - k.INITIAL), equity_delta=k.cell(net_delta))
                    a_rows.append(common)
                else:
                    common.update(method=m, capital="1000000", scenario="BASE", primary=k.cell(j >= 60), gross=k.cell(gross), fee=k.cell(fee),
                                  net=k.cell(equity - k.INITIAL), terminal_close_fee_delta=k.cell(fee_delta), terminal_close_slippage_delta="0")
                    records.append(common)
                totals[m] += net_delta
            if m in k.CONTROLS:
                control_raw[f"accounts/C1000000/BASE/{symbol}/{m}/ledger.csv"] = k.csv_bytes(records)
    for m in k.METHODS:
        summary.append(dict(method=m, capital="1000000", scenario="BASE", view="ALL", net_sum=k.cell(totals[m])))
    prior = {name: k.csv_bytes([dict(label="SYNTHETIC", value="0")]) for name in k.OLD_CSV}
    prior["sensitivity-summary.csv"] = k.csv_bytes(summary)
    prior[k.REPORT] = b"# SYNTHETIC prior report\n\nPrior accepted-style synthetic table.\n"
    sources = k.Sources()
    marker = tmp_path / "SYNTHETIC-source.json"
    if not marker.exists():
        marker.write_bytes(b'{"synthetic":true}\n')
    record = k.pin(marker.read_bytes())
    for name in ("K40A_verdict", "K40B_verdict", "K40B_manifest", "K40B_CHECK", "B.publish." + k.REPORT):
        sources.read(name, marker, record)
    bindings = {name: record for name in ("input_manifest", "chronological_manifest", "K40A_verdict", "K40B_verdict", "K40B_manifest")}
    return SimpleNamespace(repo=tmp_path, output=tmp_path / output, symbols=symbols, sources=sources,
                           daily_raw=k.csv_bytes(a_rows), input_raw={"bars.csv": k.csv_bytes(bar_rows), "contracts.json": k.json_bytes(declarations)},
                           control_raw=control_raw, prior=prior, prior_check={"report_value_refs": []}, bindings=bindings,
                           bindings_sha256="a" * 64)


def prepared(context):
    bars, nets, accounts, count = k.prepare_data(context)
    return bars, nets, k.select_dominant(bars)


def calculate(context):
    bars, nets, selection = prepared(context)
    tables, curves = k.aggregate_tables(bars, nets, selection, k.rows(context.prior["sensitivity-summary.csv"]))
    return tables, curves, selection


def change_csv(raw, change):
    data = k.rows(raw)
    change(data)
    return k.csv_bytes(data)


def test_previous_oi_not_current_oi_or_future_profit(tmp_path):
    context = make_context(tmp_path)
    bars, nets, selection = prepared(context)
    assert selection[1]["selected_contract"] == sorted(bars)[0]
    altered = copy.deepcopy(bars)
    altered[sorted(bars)[1]][1]["close_oi"] = Decimal("999999999")
    assert k.select_dominant(altered)[1] == selection[1]
    for method in nets:
        for key in list(nets[method]):
            if key[4] > selection[1]["raw_date"]:
                nets[method][key] += Decimal("999999")
    assert k.select_dominant(bars) == selection


def test_ties_zero_first_bar_missing_no_backfill_and_permutation(tmp_path):
    context = make_context(tmp_path, oi=[["0", "", "3", "2"], ["0", "", "3", "2"]])
    bars, nets, selection = prepared(context)
    assert selection[0]["selected_contract"] is None
    assert selection[1]["eligible_candidates"] == 2 and selection[1]["max_oi_ties"] == 2
    assert selection[1]["selected_contract"] == sorted(bars)[0]
    assert selection[2]["selected_contract"] is None
    assert {c["status"] for c in selection[2]["candidates"]} == {"MISSING_PREVIOUS_OI"}
    context.input_raw["bars.csv"] = change_csv(context.input_raw["bars.csv"], lambda r: r.reverse())
    assert prepared(context)[2] == selection


def test_missing_calendar_dates_use_only_immediate_stored_bar(tmp_path):
    context = make_context(tmp_path, days=["2020-12-01", "2020-12-20", "2021-02-01", "2021-02-02"])
    tables, curves, selection = calculate(context)
    scope = {r["metric"]: r["value"] for r in tables[k.NEW_CSV[2]]}
    assert scope["maximum_previous_bar_calendar_gap_days"] == "43"
    assert selection[2]["candidates"][0]["prior_j"] == 1


@pytest.mark.parametrize("value", ["-1", "NaN", "Infinity", "-Infinity", "nonsense"])
def test_invalid_oi_rejected(tmp_path, value):
    context = make_context(tmp_path)
    context.input_raw["bars.csv"] = change_csv(context.input_raw["bars.csv"], lambda r: r[0].update(close_oi=value))
    with pytest.raises(k.ValidationError):
        prepared(context)


@pytest.mark.parametrize("mutation", ["duplicate_bar", "wrong_identity", "missing_method", "duplicate_ledger", "wrong_capital", "wrong_scenario", "missing_ledger", "bad_delta", "bad_terminal", "bad_j60", "nonfinite_money"])
def test_support_and_accounting_fail_closed(tmp_path, mutation):
    context = make_context(tmp_path)
    if mutation == "duplicate_bar":
        context.input_raw["bars.csv"] = change_csv(context.input_raw["bars.csv"], lambda r: r.append(dict(r[0])))
    elif mutation == "wrong_identity":
        context.daily_raw = change_csv(context.daily_raw, lambda r: r[0].update(contract_instance_id="wrong"))
    elif mutation == "missing_method":
        context.daily_raw = k.csv_bytes([r for r in k.rows(context.daily_raw) if r["strategy"] != k.STRATEGIES[0]])
    elif mutation == "duplicate_ledger":
        context.daily_raw = change_csv(context.daily_raw, lambda r: r.append(dict(r[0])))
    elif mutation in ("wrong_capital", "wrong_scenario"):
        key = next(iter(context.control_raw))
        field, value = ("capital", "50000") if mutation == "wrong_capital" else ("scenario", "FEE_X2")
        context.control_raw[key] = change_csv(context.control_raw[key], lambda r: r[0].update({field: value}))
    elif mutation == "missing_ledger":
        del context.control_raw[next(iter(context.control_raw))]
    else:
        field, value = {"bad_delta": ("net_delta", "123"), "bad_terminal": ("terminal", "true"),
                        "bad_j60": ("primary_support", "true"), "nonfinite_money": ("cash", "NaN")}[mutation]
        context.daily_raw = change_csv(context.daily_raw, lambda r: r[0].update({field: value}))
    with pytest.raises(k.ValidationError):
        prepared(context)


@pytest.mark.parametrize("field", ["ts", "trading_day"])
def test_entire_bundle_holdout_guard_runs_before_selection(tmp_path, monkeypatch, field):
    context = make_context(tmp_path)
    data = k.rows(context.input_raw["bars.csv"])
    other = dict(data[0], symbol="OTHER.not_selected")
    other[field] = "2026-09-05T00:00:00+08:00" if field == "ts" else "2026-09-05"
    data.append(other)
    context.input_raw["bars.csv"] = k.csv_bytes(data)
    monkeypatch.setattr(k, "select_dominant", lambda _: pytest.fail("selection must not run"))
    with pytest.raises(k.ValidationError, match="HOLDOUT_BOUNDARY"):
        k.aggregate(context)
    assert not context.output.exists()


def test_ledger_holdout_boundary_and_import_time_distinction(tmp_path):
    context = make_context(tmp_path)
    assert prepared(context)[2]
    context.daily_raw = change_csv(context.daily_raw, lambda r: r[0].update(raw_ts="2026-09-05T00:00:00+08:00"))
    with pytest.raises(k.ValidationError, match="HOLDOUT_BOUNDARY"):
        prepared(context)


def test_switch_uses_net_delta_not_equity_and_preserves_terminal_cost(tmp_path):
    context = make_context(tmp_path, deltas=[[1000, 2, 3, -5], [-2000, 10, 20, -50]])
    tables, curves, selection = calculate(context)
    result = tables[k.NEW_CSV[0]][0]
    assert [s["selected_contract"] for s in selection] == [None, "SHFE.rb2001", "SHFE.rb2002", "SHFE.rb2001"]
    assert result["net"] == Decimal("17")
    assert result["all_accounts_net"] == Decimal("-1020")
    assert result["all_accounts"] == 2
    assert [r["selected_net"] for r in curves if r["method"] == k.METHODS[0]] == [None, Decimal(2), Decimal(20), Decimal(-5)]


def test_cross_year_and_j60_do_not_reset_account(tmp_path):
    days = [(date(2020, 11, 15) + timedelta(days=i)).isoformat() for i in range(65)]
    context = make_context(tmp_path, days=days, oi=[["1"] * 65], deltas=[["0.01"] * 65])
    tables, curves, selection = calculate(context)
    assert tables[k.NEW_CSV[0]][0]["net"] == Decimal("0.64")
    assert sum(r["net"] or 0 for r in tables[k.NEW_CSV[1]] if r["method"] == k.METHODS[0]) == Decimal("0.64")


def test_switches_across_uncovered_days_and_segments(tmp_path):
    for second, expected in (("A", 0), ("B", 1)):
        context = make_context(tmp_path)
        bars, nets, original = prepared(context)
        selection = copy.deepcopy(original)
        for i in (0, 2):
            selection[i].update(selected_contract=None, selected_instance=None, selected_key=None)
        symbols = sorted(bars)
        selected_symbols = [symbols[0], symbols[0] if second == "A" else symbols[1]]
        for i, symbol in zip((1, 3), selected_symbols):
            selection[i].update(selected_contract=symbol, selected_instance=bars[symbol][i]["contract_instance_id"], selected_key=list(k.identity(bars[symbol][i])))
        tables, _ = k.aggregate_tables(bars, nets, selection, k.rows(context.prior["sensitivity-summary.csv"]))
        scope = {r["metric"]: r["value"] for r in tables[k.NEW_CSV[2]]}
        assert scope["switches"] == str(expected)
        assert scope["uncovered_segments"] == "2"


def test_drawdown_initial_loss_equal_peak_open_end_and_water_gap(tmp_path):
    days = ["2020-12-28", "2020-12-29", "2020-12-30", "2020-12-31", "2021-01-04", "2021-01-05"]
    context = make_context(tmp_path, days=days, oi=[["1", "", "1", "1", "1", "1"]], deltas=[[0, -5, 99, 2, 3, -1]])
    tables, curves, selection = calculate(context)
    result = tables[k.NEW_CSV[0]][0]
    assert result["max_drawdown"] == Decimal(5)
    assert result["longest_drawdown_observations"] == 3
    assert result["open_drawdown_at_end"] is True
    curve = [r for r in curves if r["method"] == k.METHODS[0]]
    assert curve[2]["selected_net"] is None and curve[2]["drawdown"] == 5
    assert curve[4]["drawdown"] == 0 and curve[4]["underwater_observations"] == 0
    assert result["worst_year"] == 2020 and result["positive_years"] == 1


def test_covered_zero_no_coverage_and_worst_tie_are_distinct(tmp_path):
    context = make_context(tmp_path, deltas=[[0] * 4, [0] * 4])
    tables, curves, selection = calculate(context)
    result = tables[k.NEW_CSV[0]][0]
    assert result["max_drawdown"] == 0 and result["longest_drawdown_observations"] == 0
    assert result["worst_year"] == 2020 and result["positive_years"] == 0
    assert next(r for r in tables[k.NEW_CSV[1]] if r["method"] == k.METHODS[0] and r["year"] == 2022)["net"] is None
    context = make_context(tmp_path, oi=[[""] * 4, [""] * 4])
    tables, curves, selection = calculate(context)
    assert tables[k.NEW_CSV[0]][0]["net"] is None
    assert tables[k.NEW_CSV[0]][0]["max_drawdown"] is None
    assert tables[k.NEW_CSV[0]][0]["longest_drawdown_observations"] is None
    assert all(r["selected_net"] is None for r in curves)


def test_exact_ratios_half_even_and_no_float_amounts():
    assert k.rounded_ratio(Decimal(1), 6, 3) == "0.167"
    assert k.rounded_ratio(Decimal("1.2345"), 1, 3) == "1.234"
    assert k.rounded_ratio(Decimal("1.2355"), 1, 3) == "1.236"
    assert k.rounded_ratio(Decimal(1), 3, 6, 100) == "33.333333"
    with pytest.raises(k.ValidationError):
        k.amount(1.5)
    with pytest.raises(Inexact):
        _ = k.amount("1" + "0" * 200) + Decimal(1)


def synthetic_receipt(context, tmp_path):
    stdout, stderr = tmp_path / (context.output.name + ".stdout"), tmp_path / (context.output.name + ".stderr")
    stdout.write_bytes(b'SYNTHETIC terminal stream\n'); stderr.write_bytes(b'')
    binding = tmp_path / "SYNTHETIC-bindings.json"
    binding.write_bytes(b'{}\n')
    receipt = dict(schema="k43-aggregation-terminal/v1", raw_exit_subject="aggregation_child", raw_exit=0,
                   terminated=True, streams_closed=True, pid=123, task_id="SYNTHETIC-test", started_utc="2026-11-13T00:00:00+00:00",
                   ended_utc="2026-11-13T00:00:01+00:00", cwd=str(ROOT), output_dir=str(context.output),
                   source_bindings_sha256=context.bindings_sha256, manifest=k.pin((context.output / "manifest.json").read_bytes()),
                   argv=[sys.executable, "-B", "-X", "utf8", str(TOOL), "aggregate", "--source-repo", str(tmp_path),
                         "--source-bindings", str(binding), "--source-bindings-sha256", context.bindings_sha256, "--output-dir", str(context.output)],
                   closed_streams={name: dict(path=str(p), **k.pin(p.read_bytes())) for name, p in (("stdout", stdout), ("stderr", stderr))})
    filename = tmp_path / (context.output.name + ".receipt.json")
    filename.write_bytes(k.json_bytes(receipt))
    return binding, filename, receipt


def test_small_complete_runs_are_deterministic_and_only_seal_creates_check(tmp_path, monkeypatch):
    first = make_context(tmp_path, output="first")
    second = make_context(tmp_path, output="second")
    assert k.aggregate(first) == k.aggregate(second)
    assert not (first.output / "publish/CHECK.json").exists()
    payloads = k.loads((first.output / "manifest.json").read_bytes())["payloads"]
    for name in payloads:
        assert (first.output / name).read_bytes() == (second.output / name).read_bytes()
    binding, receipt, _ = synthetic_receipt(first, tmp_path)
    monkeypatch.setattr(k, "load_sources", lambda *a, **kw: first)
    assert k.seal(first.output, receipt, binding, first.bindings_sha256)["status"] == "SEALED_AGGREGATION_EXIT_0"
    check = k.loads((first.output / "publish/CHECK.json").read_bytes())
    assert check["raw_exit"] == 0 and check["new_simulation"] is False
    assert check["historical_versions"]["K40B"]["new_simulation"] is True
    with pytest.raises(k.ValidationError, match="final CHECK"):
        k.seal(first.output, receipt, binding, first.bindings_sha256)


@pytest.mark.parametrize("mutation", ["nonzero", "unterminated", "streams_open", "wrong_output", "wrong_hash", "wrong_argv", "stream_tamper", "payload_tamper", "extra_file", "temp_file"])
def test_seal_rejects_incomplete_or_contradictory_custody(tmp_path, monkeypatch, mutation):
    context = make_context(tmp_path)
    k.aggregate(context)
    binding, receipt_path, receipt = synthetic_receipt(context, tmp_path)
    monkeypatch.setattr(k, "load_sources", lambda *a, **kw: context)
    if mutation == "nonzero": receipt["raw_exit"] = 3
    elif mutation == "unterminated": receipt["terminated"] = False
    elif mutation == "streams_open": receipt["streams_closed"] = False
    elif mutation == "wrong_output": receipt["output_dir"] = str(tmp_path / "different")
    elif mutation == "wrong_hash": receipt["source_bindings_sha256"] = "b" * 64
    elif mutation == "wrong_argv": receipt["argv"][5] = "seal"
    elif mutation == "stream_tamper": Path(receipt["closed_streams"]["stdout"]["path"]).write_bytes(b'changed')
    elif mutation == "payload_tamper": (context.output / "publish/dominant-summary.csv").write_bytes(b'changed')
    elif mutation == "extra_file": (context.output / "extra").write_bytes(b'changed')
    elif mutation == "temp_file": (context.output / "publish/CHECK.json.tmp").write_bytes(b'partial')
    receipt_path.write_bytes(k.json_bytes(receipt))
    with pytest.raises(k.ValidationError):
        k.seal(context.output, receipt_path, binding, context.bindings_sha256)
    assert not (context.output / "publish/CHECK.json").exists()


def test_midrun_source_change_prevents_complete_publication(tmp_path, monkeypatch):
    context = make_context(tmp_path)
    original_verify = context.sources.verify
    calls = []
    def changed():
        calls.append(1)
        if len(calls) == 2:
            Path(next(iter(context.sources.locations.values()))).write_bytes(b'SYNTHETIC source changed')
        original_verify()
    monkeypatch.setattr(context.sources, "verify", changed)
    with pytest.raises(k.ValidationError, match="source changed"):
        k.aggregate(context)
    assert not context.output.exists()
    assert not (context.output.with_name(context.output.name + ".pending") / "manifest.json").exists()


def test_existing_output_and_path_types_refused(tmp_path):
    context = make_context(tmp_path)
    context.output.mkdir()
    with pytest.raises(k.ValidationError, match="already exists"):
        k.aggregate(context)
    for value in (None, 2, "", tmp_path / ".." / "escape"):
        with pytest.raises(k.ValidationError):
            k.path(value)


@pytest.mark.parametrize("tail", [[], ["--source-repo", "duplicate"], ["--source-r", "short"], ["--skip-hash", "true"]])
def test_actual_cli_tampered_binding_and_bad_options_fail_two(tmp_path, tail):
    source = tmp_path / "bindings.json"
    source.write_bytes(b'{}\n')
    output = tmp_path / "absent"
    command = [sys.executable, "-B", "-X", "utf8", str(TOOL), "aggregate", "--source-repo", str(tmp_path),
               "--source-bindings", str(source), "--source-bindings-sha256", "0" * 64, "--output-dir", str(output)] + tail
    run = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert run.returncode == 2, (run.returncode, run.stdout, run.stderr)
    assert run.stderr and not output.exists()


def test_driver_is_standard_library_only_and_has_no_execution_imports():
    tree = ast.parse(TOOL.read_text(encoding="utf-8"))
    imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = {alias.name.split(".")[0] for n in imports if isinstance(n, ast.Import) for alias in n.names}
    names |= {n.module.split(".")[0] for n in imports if isinstance(n, ast.ImportFrom)}
    assert names <= sys.stdlib_module_names | {"__future__"}
    assert not names & {"subprocess", "socket", "urllib", "http", "importlib", "tqsdk", "numpy", "ak_trading"}
    source = TOOL.read_text(encoding="utf-8")
    assert "os.environ" not in source and "getenv(" not in source and "run_scenario_backtest(" not in source


def test_published_report_every_number_maps_to_unchanged_csv():
    report = (ROOT / "docs/research/v1-daily-baseline-rb.md").read_bytes()
    check = k.loads((ROOT / k.PUBLIC / "CHECK.json").read_bytes())
    assert check["schema"] == k.SCHEMA and check["latest_operation"] == "K43"
    tables = {name: k.rows((ROOT / k.PUBLIC / name).read_bytes()) for name in k.OLD_CSV + k.NEW_CSV}
    assert k.numeric_mapping(report.decode("utf-8"), {name: tables[name] for name in k.NEW_CSV + k.OLD_CSV}) == check["report_numeric_tokens"]
    for ref in check["report_value_refs"]:
        assert tables[ref["csv"]][ref["row"] - 2][ref["column"]] == ref["value"]
    for name in k.OLD_CSV + k.NEW_CSV:
        assert k.pin((ROOT / k.PUBLIC / name).read_bytes()) == check["output_hashes"]["publish/" + name]
    assert k.pin(report) == check["output_hashes"]["publish/" + k.REPORT]
    assert len(tables[k.NEW_CSV[0]]) == 5 and len(tables[k.NEW_CSV[1]]) == 35
    first_screen = report.decode("utf-8").split("| 品种与做法", 1)[0]
    assert first_screen.count("。") == 5
    assert check["new_simulation"] is False and check["timing_verified"] is False
