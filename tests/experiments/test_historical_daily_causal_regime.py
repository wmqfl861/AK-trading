"""Causal attribution checks; every generated price and oracle is SYNTHETIC."""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from fractions import Fraction
from hashlib import sha256
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from ak_trading.futures.io import (bars_rows_from_bars, canonical_json_bytes,
                                   publish_input_bundle, read_input_bundle)
from ak_trading.futures.scenario_backtest import ScenarioConfig, run_scenario_backtest
from ak_trading.futures.spec import ContractBar

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools/experiments/historical_daily_causal_regime.py"
BASE = dict(initial_cash="1000000", lots=1, multiplier="10", price_tick="1",
            fee_per_lot="0.01", fee_rate="0.0001", slippage_ticks="1",
            margin_rate="0.12", max_margin_ratio="0.4")
STRATEGIES = ("donchian_trend", "ma_trend", "zscore_reversion")
LABELS = ("UNKNOWN", "FLAT", "TREND_UP", "TREND_DOWN", "RANGE", "MIXED")


@pytest.fixture
def cli():
    spec = importlib.util.spec_from_file_location("causal_regime_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_input(tmp_path, histories=None, *, reverse=False):
    # real_export is only the legacy transport. Provenance explicitly labels
    # both the input metadata and each bar's price source as SYNTHETIC.
    histories = histories if histories is not None else {
        "SHFE.rb2401": [100] * 60,
        "SHFE.rb2402": list(range(100, 161)),
        "SHFE.rb2403": [100] * 40 + [110, 111, 115],
        "SHFE.rb2404": [100] * 65,
        "SHFE.rb2405": [],
    }
    declarations, bars = [], []
    origin = datetime(2023, 1, 1, tzinfo=timezone.utc)
    for symbol, history in histories.items():
        expiry = f"2024-{symbol[-2:]}-15T15:00:00+08:00"
        declarations.append(dict(raw_symbol=symbol, product="SHFE.RB", expiry_at=expiry,
                                 instance_id=f"{symbol}|{expiry}", identity_status="SYNTHETIC"))
        for index, value in enumerate(history):
            opening, closing = value if isinstance(value, tuple) else (value, value)
            opening, closing = Decimal(str(opening)), Decimal(str(closing))
            ts = origin + timedelta(days=index)
            bars.append(ContractBar(symbol=symbol, duration_seconds=86400, ts=ts,
                                    datetime_nano=int(ts.timestamp()) * 1_000_000_000,
                                    source="SYNTHETIC_generated_causal_regime_test_prices",
                                    open=opening, close=closing, high=max(opening, closing) + Decimal("0.1"),
                                    low=min(opening, closing) - Decimal("0.1"), volume=Decimal("1000")))
    if reverse:
        bars.reverse()
        declarations.reverse()
    source = tmp_path / "SYNTHETIC-input"
    publish_input_bundle(source, dataset_kind="real_export", created_at="2024-06-01T00:00:00+00:00",
                         bars_rows=bars_rows_from_bars(bars), contracts=declarations,
                         metadata=dict(evidence_level="SYNTHETIC", price_source="SYNTHETIC_hand_constructed_test_series"),
                         availability={}, request_info={}, source_hashes={"SYNTHETIC": "a" * 64},
                         read_only_transaction={"executed": False}, clock_policy={"d_status": "unavailable"},
                         notes={"price_source": "SYNTHETIC"})
    config = tmp_path / "SYNTHETIC-base.json"
    config.write_bytes(canonical_json_bytes(BASE))
    return source, config


def args(source, config, output):
    return ["--input-bundle", str(source), "--base-config", str(config), "--output", str(output)]


def doc(path):
    return json.loads(path.read_bytes())


def ledger(path):
    return list(csv.DictReader(io.StringIO(path.read_text("utf-8"), newline="")))


def tree(path):
    return {member.relative_to(path).as_posix(): member.read_bytes() for member in path.rglob("*") if member.is_file()}


def rewrite_input(source, name, payload):
    (source / name).write_bytes(payload)
    manifest = doc(source / "manifest.json")
    manifest["content_hashes"][name] = sha256(payload).hexdigest()
    (source / "manifest.json").write_bytes(canonical_json_bytes(manifest))


def rewrite_child(child, name, payload):
    (child / name).write_bytes(payload)
    manifest = doc(child / "manifest.json")
    manifest["content_hashes"][name] = sha256(payload).hexdigest()
    (child / "manifest.json").write_bytes(canonical_json_bytes(manifest))


def child(output, symbol="SHFE.rb2402", strategy="ma_trend"):
    return output / "cells" / symbol / strategy / "BASE"


def fraction_labels(values):
    # Independent exact-rational oracle; never calls owner classification.
    values = [Fraction(str(value)) for value in values]
    result = []
    for index in range(len(values)):
        if index < 21:
            result.append("UNKNOWN")
            continue
        changes = [values[k] - values[k - 1] for k in range(index - 20, index)]
        net, distance = sum(changes), sum(abs(change) for change in changes)
        if distance == 0:
            result.append("FLAT")
        elif abs(net) / distance >= Fraction(2, 5):
            result.append("TREND_UP" if net > 0 else "TREND_DOWN")
        elif abs(net) / distance <= Fraction(1, 5):
            result.append("RANGE")
        else:
            result.append("MIXED")
    return result


def prefix_from_steps(positive, *, sign=1, adjustment="0"):
    steps = [Decimal(1)] * positive + [Decimal(-1)] * (20 - positive)
    steps[-1] += Decimal(adjustment)
    result = [Decimal(100)]
    for step in steps:
        result.append(result[-1] + sign * step)
    return result + [Decimal(500)]


@pytest.mark.parametrize("prices,expected", [
    ([Decimal(100)] * 22, "FLAT"),
    (prefix_from_steps(14), "TREND_UP"),
    (prefix_from_steps(14, sign=-1), "TREND_DOWN"),
    (prefix_from_steps(12), "RANGE"),
    (prefix_from_steps(13), "MIXED"),
    (prefix_from_steps(10), "RANGE"),
    (prefix_from_steps(14, adjustment="-0.0001"), "MIXED"),
    (prefix_from_steps(14, adjustment="0.0001"), "TREND_UP"),
    (prefix_from_steps(12, adjustment="-0.0001"), "RANGE"),
    (prefix_from_steps(12, adjustment="0.0001"), "MIXED"),
])
def test_hand_computed_labels_exact_thresholds_and_short_prefix(cli, prices, expected):
    rows = cli.classify_prior_closes(prices)
    assert rows[20] == dict(regime="UNKNOWN", prefix_start_index=None, prefix_end_index=None,
                            signed_endpoint_change=None, absolute_path_change=None, efficiency_ratio=None)
    assert rows[21]["regime"] == expected
    assert (rows[21]["prefix_start_index"], rows[21]["prefix_end_index"]) == (0, 20)
    assert [row["regime"] for row in rows] == fraction_labels(prices)
    if expected == "FLAT":
        assert rows[21]["efficiency_ratio"] is None and rows[21]["absolute_path_change"] == "0"


def test_classifier_never_uses_current_close_or_future_and_is_context_independent(cli):
    prices = prefix_from_steps(14) + [Decimal(90), Decimal(101)]
    original = cli.classify_prior_closes(prices)
    changed = list(prices)
    changed[21] = Decimal("999999999")
    assert cli.classify_prior_closes(changed)[21] == original[21]
    assert cli.classify_prior_closes(prices + [Decimal(1)] * 30)[:len(prices)] == original
    with localcontext() as context:
        context.prec = 7
        assert cli.classify_prior_closes(prices) == original


@pytest.mark.parametrize("bad", [Decimal("NaN"), Decimal("Infinity"), Decimal(0), Decimal(-1), 100.0,
                               Decimal("1e13"), Decimal("1e-13"), Decimal("1.0000000000001")])
def test_invalid_close_never_becomes_unknown(cli, bad):
    with pytest.raises(ValueError):
        cli.classify_prior_closes([bad])


def test_full_population_primary_boundary_carried_position_empty_groups_and_overlap(cli, tmp_path):
    source, config = build_input(tmp_path)
    before = tree(source)
    output = tmp_path / "complete"
    assert cli.main(args(source, config, output)) == 0
    assert tree(source) == before
    coverage, summary = doc(output / "coverage.json"), doc(output / "summary.json")
    daily, groups = ledger(output / "daily_attribution.csv"), ledger(output / "regime_summary.csv")
    assert coverage["declared_contracts"] == 5 and coverage["contracts_with_bars"] == 4
    assert coverage["declared_without_interval_bars"] == 1
    assert len(summary["accounts"]) == 12 and len(daily) == 229 * 3 and len(groups) == 4 * 3 * 2 * 6
    assert coverage["primary_contract_observations"] == 6 and coverage["primary_attribution_rows"] == 18
    assert coverage["date_coverage"]["ALL"]["unique_raw_dates"] == 65
    assert coverage["date_coverage"]["ALL"]["maximum_contracts_same_raw_date"] == 4
    assert coverage["date_coverage"]["PRIMARY"]["unique_raw_dates"] == 5
    assert coverage["date_coverage"]["PRIMARY"]["maximum_contracts_same_raw_date"] == 2
    for symbol in ("SHFE.rb2401", "SHFE.rb2402", "SHFE.rb2403", "SHFE.rb2404"):
        masks = [[(row["observation_index"], row["regime"], row["primary"]) for row in daily if row["symbol"] == symbol and row["strategy"] == strategy] for strategy in STRATEGIES]
        assert masks[0] == masks[1] == masks[2]
    sixty = [row for row in daily if row["symbol"] == "SHFE.rb2401"]
    assert not any(row["primary"] == "true" for row in sixty)
    sixty_one = [row for row in daily if row["symbol"] == "SHFE.rb2402" and row["primary"] == "true"]
    assert len(sixty_one) == 3 and {row["observation_index"] for row in sixty_one} == {"60"}
    donchian = next(row for row in sixty_one if row["strategy"] == "donchian_trend")
    assert donchian["prior_close_position_lots"] == "1"  # PRIMARY did not reset it.
    short_report = doc(child(output, "SHFE.rb2403", "donchian_trend") / "report.json")
    assert Decimal(short_report["net_pnl"]) > 0  # Profit does not qualify a short account.
    short_groups = [row for row in groups if row["symbol"] == "SHFE.rb2403" and row["window"] == "PRIMARY"]
    assert len(short_groups) == 18 and {row["reason"] for row in short_groups} == {"NO_PRIMARY_OBSERVATIONS"}
    empty = [row for row in groups if row["observation_count"] == "0"]
    assert all(row["mean_net_delta"] == "" and row["net_pnl"] == "0" and row["contribution_max_drawdown"] == "0" for row in empty)
    flat_pairs = [row for row in summary["paired_primary_differences"] if row["regime"] == "FLAT"]
    assert len(flat_pairs) == 3 and all(row["contracts_with_rows"] == 1 and row["zero_differences"] == 1 for row in flat_pairs)
    flat_groups = [row for row in summary["groups"] if row["window"] == "PRIMARY" and row["regime"] == "FLAT"]
    assert all(row["no_active_contracts"] == 1 and row["contracts_with_rows"] == 1 for row in flat_groups)
    assert summary["groups"][0]["reason"] == "PREFIX_LT_21"
    assert coverage["calendar_status"] == "CALENDAR_UNVERIFIED"


def test_fraction_oracle_no_cross_contract_or_input_order_contamination(cli, tmp_path):
    histories = {"SHFE.rb2401": [100] * 21 + list(range(100, 140)), "SHFE.rb2402": [200 - i for i in range(65)]}
    paths = []
    for name, reverse in (("normal", False), ("reversed", True)):
        directory = tmp_path / name
        directory.mkdir()
        source, config = build_input(directory, histories, reverse=reverse)
        output = directory / "output"
        assert cli.main(args(source, config, output)) == 0
        paths.append(output)
        rows = ledger(output / "daily_attribution.csv")
        for symbol, prices in histories.items():
            for strategy in STRATEGIES:
                account = [row for row in rows if row["symbol"] == symbol and row["strategy"] == strategy]
                assert [row["regime"] for row in account] == fraction_labels(prices)
    for name in ("daily_attribution.csv", "regime_summary.csv", "coverage.json", "summary.json", "report.md"):
        assert (paths[0] / name).read_bytes() == (paths[1] / name).read_bytes()


def test_actual_public_engine_reversal_cross_regime_and_terminal_costs_reconcile(cli, tmp_path):
    source, config = build_input(tmp_path, {"SHFE.rb2401": list(range(100, 141)) + [(150, 10), (8, 6)]})
    output = tmp_path / "output"
    assert cli.main(args(source, config, output)) == 0
    daily = [row for row in ledger(output / "daily_attribution.csv") if row["strategy"] == "donchian_trend"]
    fills = ledger(child(output, "SHFE.rb2401", "donchian_trend") / "fills.csv")
    assert [(row["observation_index"], row["offset"], row["phase"]) for row in fills] == [("41", "open", "next_open"), ("42", "close", "next_open"), ("42", "open", "next_open"), ("42", "close", "terminal_close")]
    # Independent exact-rational oracle: ideal prices, both fee components and
    # one slippage charge per leg; effective prices never enter this calculation.
    fees = {41: Fraction(1, 100) + Fraction(1, 10000) * 150 * 10,
            42: 2 * (Fraction(1, 100) + Fraction(1, 10000) * 8 * 10) + Fraction(1, 100) + Fraction(1, 10000) * 6 * 10}
    assert Fraction(daily[41]["net_delta"]) == -1400 - fees[41] - 10
    assert Fraction(daily[42]["gross_delta"]) == 10 * (8 - 10) - 10 * (6 - 8) == 0
    assert Fraction(daily[42]["net_delta"]) == -fees[42] - 30
    assert (daily[41]["regime"], daily[42]["regime"]) == ("TREND_UP", "TREND_DOWN")
    assert daily[42]["terminal_close_fees"] == "0.016" and daily[42]["terminal_close_slippage"] == "10"
    assert daily[42]["fill_legs"] == "3" and daily[42]["active_observation"] == "true"
    groups = [row for row in ledger(output / "regime_summary.csv") if row["strategy"] == "donchian_trend" and row["window"] == "ALL"]
    totals = {row["regime"]: Fraction(row["net_pnl"]) for row in groups}
    assert totals["TREND_UP"] == Fraction(daily[41]["net_delta"]) and totals["TREND_DOWN"] == Fraction(daily[42]["net_delta"])
    report = doc(child(output, "SHFE.rb2401", "donchian_trend") / "report.json")
    assert sum(totals.values()) == Fraction(report["net_pnl"]) == Fraction("-1440.212")
    for strategy in STRATEGIES:
        account = [row for row in ledger(output / "daily_attribution.csv") if row["strategy"] == strategy]
        marks = ledger(child(output, "SHFE.rb2401", strategy) / "equity.csv")
        account_fills = ledger(child(output, "SHFE.rb2401", strategy) / "fills.csv")
        previous = Fraction(1000000)
        for row, mark in zip(account, marks):
            index = row["observation_index"]
            matching = [fill for fill in account_fills if fill["observation_index"] == index]
            assert Fraction(row["net_delta"]) == Fraction(mark["equity"]) - previous
            assert Fraction(row["fee_delta"]) == sum((Fraction(fill["fee_per_lot_component"]) + Fraction(fill["fee_rate_component"]) for fill in matching), Fraction(0))
            assert Fraction(row["slippage_delta"]) == 10 * len(matching)
            previous = Fraction(mark["equity"])


def test_terminal_open_and_close_n61_and_future_append_exception(cli, tmp_path):
    prices = list(range(100, 161))
    source, config = build_input(tmp_path, {"SHFE.rb2401": prices})
    output = tmp_path / "old"
    assert cli.main(args(source, config, output)) == 0
    ma_fills = ledger(child(output, "SHFE.rb2401", "ma_trend") / "fills.csv")
    assert [(row["observation_index"], row["phase"]) for row in ma_fills] == [("60", "next_open"), ("60", "terminal_close")]
    old_rows = ledger(output / "daily_attribution.csv")
    ma_last = next(row for row in old_rows if row["strategy"] == "ma_trend" and row["observation_index"] == "60")
    assert ma_last["active_observation"] == "true" and ma_last["prior_close_position_lots"] == "0"
    assert ma_last["net_delta"] == "-20.34" and ma_last["terminal_close_fees"] == "0.17"
    suffix = tmp_path / "suffix"
    suffix.mkdir()
    source2, config2 = build_input(suffix, {"SHFE.rb2401": prices + [161, 162]})
    newer = tmp_path / "new"
    assert cli.main(args(source2, config2, newer)) == 0
    new_rows = ledger(newer / "daily_attribution.csv")
    for strategy in STRATEGIES:
        old_account = [row for row in old_rows if row["strategy"] == strategy]
        new_account = [row for row in new_rows if row["strategy"] == strategy]
        assert [row["regime"] for row in old_account] == [row["regime"] for row in new_account[:61]]
        assert old_account[:60] == new_account[:60]
        assert old_account[60]["terminal"] == "true" and new_account[60]["terminal"] == "false"
    assert next(row for row in new_rows if row["strategy"] == "ma_trend" and row["observation_index"] == "60")["net_delta"] == "-10.17"
    assert len(old_rows) == 183  # The formal report retained the old terminal row.


def test_holding_without_fill_is_active_and_primary_sums_use_equity59(cli, tmp_path):
    source, config = build_input(tmp_path, {"SHFE.rb2401": list(range(100, 175))})
    output = tmp_path / "output"
    assert cli.main(args(source, config, output)) == 0
    rows = ledger(output / "daily_attribution.csv")
    groups = ledger(output / "regime_summary.csv")
    held = next(row for row in rows if row["strategy"] == "donchian_trend" and row["observation_index"] == "60")
    assert held["fill_legs"] == "0" and held["prior_close_position_lots"] == "1" and held["active_observation"] == "true"
    for strategy in STRATEGIES:
        marks = ledger(child(output, "SHFE.rb2401", strategy) / "equity.csv")
        primary = [row for row in groups if row["strategy"] == strategy and row["window"] == "PRIMARY"]
        assert sum((Fraction(row["net_pnl"]) for row in primary), Fraction(0)) == Fraction(marks[-1]["equity"]) - Fraction(marks[59]["equity"])
        all_rows = [row for row in groups if row["strategy"] == strategy and row["window"] == "ALL"]
        assert sum((Fraction(row["net_pnl"]) for row in all_rows), Fraction(0)) == Fraction(marks[-1]["equity"]) - 1000000


def test_contribution_curve_initial_loss_zero_steps_recovery_and_no_segment_reset(cli):
    assert cli.contribution_drawdown(map(Decimal, ("-3", "0", "5", "0", "-4"))) == Decimal(4)
    assert cli.contribution_drawdown(map(Decimal, ("-3", "0"))) == Decimal(3)
    rows, equity = [], Decimal(BASE["initial_cash"])
    for index, (delta, label) in enumerate((("-3", "RANGE"), ("0", "TREND_UP"), ("5", "RANGE"), ("0", "MIXED"), ("-4", "RANGE"))):
        equity += Decimal(delta)
        rows.append(dict(symbol="SHFE.rb2401", contract_instance_id="SYNTHETIC", strategy="donchian_trend",
                         observation_index=index, raw_date=f"2023-01-{index+1:02d}", regime=label, primary=False,
                         active_observation=True, fill_legs=0, terminal=index == 4, terminal_close_legs=0,
                         gross_delta=delta, fee_delta="0", slippage_delta="0", net_delta=delta,
                         terminal_close_fees="0", terminal_close_slippage="0", equity=str(equity)))
    groups = cli.summarize_account(rows)
    selected = next(row for row in groups if row["window"] == "ALL" and row["regime"] == "RANGE")
    assert selected["net_pnl"] == "-2" and selected["contribution_max_drawdown"] == "4"
    assert all(row["mean_net_delta"] is None and row["contribution_max_drawdown"] == "0" for row in groups if row["window"] == "PRIMARY")


def test_even_medians_and_fixed_paired_denominators_are_independent_ledger_differences(cli, tmp_path):
    histories = {"SHFE.rb2401": list(range(100, 175)), "SHFE.rb2402": list(range(200, 280))}
    source, config = build_input(tmp_path, histories)
    output = tmp_path / "output"
    assert cli.main(args(source, config, output)) == 0
    summary, groups = doc(output / "summary.json"), ledger(output / "regime_summary.csv")
    for item in summary["groups"]:
        population = [row for row in groups if row["strategy"] == item["strategy"] and row["window"] == item["window"] and row["regime"] == item["regime"] and int(row["observation_count"])]
        values = sorted(Fraction(row["net_pnl"]) for row in population)
        if values:
            expected = (values[0] + values[-1]) / 2 if len(values) == 2 else values[0]
            assert Fraction(item["net_contribution"]["median"]) == expected
        else:
            assert item["net_contribution"] == dict(min=None, median=None, max=None)
    lookup = {(row["symbol"], row["strategy"], row["window"], row["regime"]): row for row in groups}
    assert len(summary["paired_primary_differences"]) == 18
    for pair in summary["paired_primary_differences"]:
        independent = []
        for symbol in histories:
            left = lookup[(symbol, pair["left_strategy"], "PRIMARY", pair["regime"])]
            right = lookup[(symbol, pair["right_strategy"], "PRIMARY", pair["regime"])]
            if int(left["observation_count"]):
                independent.append(Fraction(left["net_pnl"]) - Fraction(right["net_pnl"]))
        assert pair["contracts_with_rows"] == len(independent)
        if independent:
            assert Fraction(pair["net_contribution_difference"]["median"]) == sum(independent) / len(independent)
    assert not any("portfolio" in key or "total_net" in key for key in summary)


@pytest.mark.parametrize("extra", [["--output", "duplicate"], ["--base-config", "duplicate"], ["--input-bundle", "duplicate"],
                                     ["--symbol", "SHFE.rb2401"], ["--strategy", "ma_trend"], ["--out", "alias"], ["--threshold", "0.3"]])
def test_unknown_duplicate_filter_and_abbreviated_arguments_reject(cli, tmp_path, extra, capsys):
    source, config = build_input(tmp_path, {"SHFE.rb2401": [100] * 22})
    output = tmp_path / "rejected"
    assert cli.main(args(source, config, output) + extra) == 2
    assert not output.exists()
    assert json.loads(capsys.readouterr().out)["status"] == "rejected"


@pytest.mark.parametrize("missing", ["--input-bundle", "--base-config", "--output"])
def test_missing_required_argument_is_machine_readable(cli, tmp_path, missing, capsys):
    source, config = build_input(tmp_path, {"SHFE.rb2401": [100] * 22})
    output = tmp_path / "rejected"
    arguments = args(source, config, output)
    index = arguments.index(missing)
    del arguments[index:index+2]
    assert cli.main(arguments) == 2 and not output.exists()
    assert json.loads(capsys.readouterr().out)["error_type"] == "ValueError"


@pytest.mark.parametrize("problem", ["duplicate", "nan", "infinity", "missing_field", "unknown_field", "array", "changed", "boolean_lots", "missing_file"])
def test_strict_fixed_base_config_precedes_root_creation(cli, tmp_path, problem):
    source, config = build_input(tmp_path, {"SHFE.rb2401": [100] * 22})
    values = dict(BASE)
    if problem == "duplicate":
        config.write_bytes(b'{"lots":1,' + canonical_json_bytes(values)[1:])
    elif problem in ("nan", "infinity"):
        config.write_bytes(canonical_json_bytes(values).replace(b'"1"', b'NaN' if problem == "nan" else b'1e9999', 1))
    elif problem == "array":
        config.write_bytes(b'[]\n')
    elif problem == "missing_file":
        config.unlink()
    else:
        if problem == "missing_field":
            del values["fee_rate"]
        elif problem == "unknown_field":
            values["threshold"] = "0.3"
        elif problem == "changed":
            values["initial_cash"] = "900000"
        else:
            values["lots"] = True
        config.write_bytes(canonical_json_bytes(values))
    output = tmp_path / "rejected"
    assert cli.main(args(source, config, output)) == 2 and not output.exists()


@pytest.mark.parametrize("problem", ["hash", "missing_payload", "duplicate_json", "ambiguous_instance", "bad_csv", "bad_ohlc", "nonfinite_ohlc", "duplicate_nano", "duration", "undeclared_bar"])
def test_input_hash_identity_schema_and_prices_reject_without_dropping(cli, tmp_path, problem):
    source, config = build_input(tmp_path, {"SHFE.rb2401": [100] * 22})
    if problem == "hash":
        (source / "metadata.json").write_bytes(b'{}\n')
    elif problem == "missing_payload":
        (source / "availability.json").unlink()
    elif problem == "duplicate_json":
        rewrite_input(source, "metadata.json", b'{"evidence_level":"SYNTHETIC","evidence_level":"SYNTHETIC"}\n')
    elif problem == "ambiguous_instance":
        declarations = doc(source / "contracts.json")
        declarations[0]["instance_id"] = "SYNTHETIC_wrong_instance"
        rewrite_input(source, "contracts.json", canonical_json_bytes(declarations))
    else:
        data = (source / "bars.csv").read_bytes()
        rows = list(csv.reader(io.StringIO(data.decode(), newline="")))
        columns = rows[0]
        if problem == "bad_csv":
            rows[0][0] = "wrong_header"
        elif problem == "bad_ohlc":
            rows[1][columns.index("low")] = "105"
        elif problem == "nonfinite_ohlc":
            rows[1][columns.index("close")] = "NaN"
        elif problem == "duplicate_nano":
            rows[2][columns.index("datetime_nano")] = rows[1][columns.index("datetime_nano")]
        elif problem == "duration":
            rows[1][columns.index("duration_seconds")] = "60"
        else:
            rows[1][columns.index("symbol")] = "SHFE.rb2402"
        stream = io.StringIO(newline="")
        csv.writer(stream, lineterminator="\n").writerows(rows)
        rewrite_input(source, "bars.csv", stream.getvalue().encode())
    output = tmp_path / "rejected"
    assert cli.main(args(source, config, output)) == 2 and not output.exists()


@pytest.mark.parametrize("kind", ["existing", "input_child", "input_parent", "source_tree", "docs_tree", "base_path", "device", "credential"])
def test_output_path_safety_and_collision_preserve_existing_bytes(cli, tmp_path, kind, monkeypatch):
    source, config = build_input(tmp_path, {"SHFE.rb2401": [100] * 22})
    if kind == "existing":
        output = tmp_path / "existing"
        output.mkdir()
        (output / "sentinel").write_bytes(b'SYNTHETIC_keep')
    else:
        output = {"input_child": source / "nested", "input_parent": source.parent,
                  "source_tree": ROOT / "tools/experiments/SYNTHETIC_should_not_create",
                  "docs_tree": ROOT / "docs/SYNTHETIC_should_not_create", "base_path": config,
                  "device": tmp_path / "NUL", "credential": tmp_path / ".env"}[kind]
    before = tree(source)
    opened = []
    if kind == "device":
        original = Path.open

        def guarded(path, *positional, **keywords):
            if path == output:
                opened.append(path)
                raise AssertionError("device must be rejected before opening")
            return original(path, *positional, **keywords)

        monkeypatch.setattr(Path, "open", guarded)
    assert cli.main(args(source, config, output)) == 2
    assert tree(source) == before
    if kind == "existing":
        assert (output / "sentinel").read_bytes() == b'SYNTHETIC_keep'
    elif kind == "device":
        assert not opened  # Windows device aliases need not have exists()==False.
    elif kind in ("source_tree", "docs_tree", "credential", "input_child"):
        assert not output.exists()


def test_symlink_component_is_rejected_without_writing_target(cli, tmp_path):
    source, config = build_input(tmp_path, {"SHFE.rb2401": [100] * 22})
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    assert cli.main(args(source, config, alias / "output")) == 2
    assert list(real.iterdir()) == []


@pytest.mark.parametrize("failure", ["second_account", "input_drift", "code_drift", "child_incomplete", "child_code", "child_identity", "report_count_type", "equity_missing", "decision_identity", "fill_fee", "fill_cash", "duplicate_report", "extra_child", "derived_config"])
def test_child_corruption_drift_and_midrun_failure_never_publish_complete(cli, tmp_path, monkeypatch, failure, capsys):
    source, config = build_input(tmp_path, {"SHFE.rb2401": [100] * 40 + [110, 90, 80]})
    output = tmp_path / "failed"
    original, original_hashes = cli.run_history_scenario, cli._code_hashes
    calls, drift = 0, False

    def current_hashes():
        hashes = original_hashes()
        if drift:
            hashes["tools/experiments/historical_daily_causal_regime.py"] = "f" * 64
        return hashes

    def injected(*positional, **keywords):
        nonlocal calls, drift
        calls += 1
        if failure == "second_account" and calls == 2:
            raise RuntimeError("SYNTHETIC second-account failure")
        result = original(*positional, **keywords)
        if calls != 1:
            return result
        directory = positional[1]
        if failure == "input_drift":
            rewrite_input(source, "metadata.json", canonical_json_bytes({"evidence_level": "SYNTHETIC", "revision": 2}))
        elif failure == "code_drift":
            drift = True  # SYNTHETIC hash-reader fault; real sources stay untouched.
        elif failure in ("child_incomplete", "child_code"):
            manifest = doc(directory / "manifest.json")
            if failure == "child_incomplete":
                manifest["status"] = "incomplete"
            else:
                manifest["code_hashes"]["scripts/backtest_history.py"] = "f" * 64
            (directory / "manifest.json").write_bytes(canonical_json_bytes(manifest))
        elif failure in ("child_identity", "report_count_type"):
            report = doc(directory / "report.json")
            if failure == "child_identity":
                report["strategy"] = "ma_trend"
            else:
                report["fills"] = True
            rewrite_child(directory, "report.json", canonical_json_bytes(report))
        elif failure == "duplicate_report":
            rewrite_child(directory, "report.json", b'{"status":"complete",' + (directory / "report.json").read_bytes()[1:])
        elif failure == "extra_child":
            (directory / "unexpected.txt").write_bytes(b'SYNTHETIC extra artifact')
        elif failure == "derived_config":
            (output / "configs/BASE.json").write_bytes(canonical_json_bytes({**BASE, "initial_cash": "900000"}))
        elif failure == "equity_missing":
            lines = (directory / "equity.csv").read_bytes().splitlines(keepends=True)
            rewrite_child(directory, "equity.csv", b''.join(lines[:-1]))
        else:
            name = "decisions.csv" if failure == "decision_identity" else "fills.csv"
            rows = ledger(directory / name)
            columns = tuple(rows[0])
            if failure == "decision_identity":
                rows[0]["datetime_nano"] = "1"
            elif failure == "fill_fee":
                rows[0]["fee_per_lot_component"] = "99"
            else:
                rows[0]["cash_after"] = "1"
            stream = io.StringIO(newline="")
            writer = csv.DictWriter(stream, columns, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
            rewrite_child(directory, name, stream.getvalue().encode())
        return result

    monkeypatch.setattr(cli, "run_history_scenario", injected)
    monkeypatch.setattr(cli, "_code_hashes", current_hashes)
    assert cli.main(args(source, config, output)) == 2
    assert doc(output / "manifest.json")["status"] == "incomplete"
    assert json.loads(capsys.readouterr().out)["status"] == "incomplete"


@pytest.mark.parametrize("failure", ["payload", "marker_collision", "partial_marker"])
def test_writer_failure_partial_marker_and_foreign_collision_keep_evidence(cli, tmp_path, monkeypatch, failure):
    source, config = build_input(tmp_path, {"SHFE.rb2401": [100] * 22})
    output, original = tmp_path / "failed", Path.open
    marker = output / "manifest.json"
    injected = False

    class FailOnce:
        def __init__(self, handle):
            self.handle, self.failed = handle, False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def write(self, payload):
            if not self.failed:
                self.failed = True
                self.handle.write(payload[:50])
                raise OSError("SYNTHETIC partial marker write")
            return self.handle.write(payload)

    def fail(path, mode="r", *positional, **keywords):
        nonlocal injected
        if failure == "payload" and path == output / "daily_attribution.csv" and "x" in mode:
            raise OSError("SYNTHETIC payload write failure")
        if path == marker and "x" in mode and not injected:
            injected = True
            if failure == "marker_collision":
                with original(path, "xb") as handle:
                    handle.write(b'SYNTHETIC_foreign_writer_sentinel')
            elif failure == "partial_marker":
                return FailOnce(original(path, mode, *positional, **keywords))
        return original(path, mode, *positional, **keywords)

    monkeypatch.setattr(Path, "open", fail)
    assert cli.main(args(source, config, output)) == 2
    if failure == "marker_collision":
        assert marker.read_bytes() == b'SYNTHETIC_foreign_writer_sentinel'
    else:
        assert doc(marker)["status"] == "incomplete"


def test_exact_hash_closure_and_fresh_offline_process_reproduce_every_byte(cli, tmp_path):
    source, config = build_input(tmp_path, {"SHFE.rb2401": list(range(100, 162)), "SHFE.rb2402": [100] * 22})
    first, second = tmp_path / "first", tmp_path / "second"
    assert cli.main(args(source, config, first)) == 0
    manifest = doc(first / "manifest.json")
    artifacts = tree(first)
    assert set(manifest["content_hashes"]) == set(artifacts) - {"manifest.json"}
    assert all(sha256(artifacts[name]).hexdigest() == digest for name, digest in manifest["content_hashes"].items())
    assert manifest["code_hashes"]["tools/experiments/historical_daily_causal_regime.py"] == sha256(SCRIPT.read_bytes()).hexdigest()
    bootstrap = """
import builtins, io, os, runpy, socket, sys
original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if any(name == p or name.startswith(p + '.') for p in ('dotenv','tqsdk','sqlalchemy','psycopg','requests','httpx','ak_trading.config','ak_trading.runtime','ak_trading.persistence')):
        raise AssertionError('offline forbidden import: ' + name)
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
def denied(*args, **kwargs):
    raise AssertionError('network attempted')
socket.socket.connect = socket.socket.connect_ex = socket.create_connection = socket.getaddrinfo = denied
original_open = io.open
def guarded_open(path, *args, **kwargs):
    if isinstance(path, (str, bytes, os.PathLike)) and os.fsdecode(path).replace('\\\\','/').rsplit('/',1)[-1].lower().startswith('.env'):
        raise AssertionError('credential path attempted')
    return original_open(path, *args, **kwargs)
io.open = guarded_open
script = sys.argv.pop(1)
runpy.run_path(script, run_name='__main__')
"""
    environment = {name: os.environ[name] for name in ("PATH", "SYSTEMROOT", "WINDIR") if name in os.environ}
    environment.update(PYTHONHASHSEED="3281", PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1", PYTHONIOENCODING="utf-8", TEMP=str(tmp_path), TMP=str(tmp_path))
    result = subprocess.run([sys.executable, "-B", "-c", bootstrap, str(SCRIPT), *args(source, config, second)],
                            cwd=tmp_path, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert tree(first) == tree(second)
