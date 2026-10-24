"""Capital-stress comparison contracts over explicitly SYNTHETIC offline data.

These tests exercise the real legacy strategies, replay, bundle reader and
six-file writer.  Fraction arithmetic independently reconstructs admission,
accounting and path differences; it does not call the comparison implementation
as an oracle.  Fault injection wraps the real child writer only after it has
written its actual output.  No live broker, SDK, database or sibling experiment
is imported or used.
"""
from __future__ import annotations

import csv
import importlib.util
import io
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from fractions import Fraction
from hashlib import sha256
from pathlib import Path

import pytest

from ak_trading.futures.io import (
    BARS_CSV_COLUMNS,
    bars_rows_from_bars,
    publish_input_bundle,
    read_input_bundle,
)
from ak_trading.futures.scenario_backtest import ScenarioConfig, run_scenario_backtest
from ak_trading.futures.spec import ContractBar


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools/experiments/historical_daily_capital_stress.py"
BASE_FIXTURE = ROOT / "tests/fixtures/experiments/rb_daily_capital_stress/base.json"
BASE = {
    "initial_cash": "1000000", "lots": 1, "multiplier": "10", "price_tick": "1",
    "fee_per_lot": "0.01", "fee_rate": "0.0001", "slippage_ticks": "1",
    "margin_rate": "0.12", "max_margin_ratio": "0.4",
}
STRATEGIES = ("donchian_trend", "ma_trend", "zscore_reversion")
REQUIRED_BARS = {"donchian_trend": 41, "ma_trend": 60, "zscore_reversion": 21}
CAPITALS = {"C5000": "5000", "C15000": "15000", "C1000000": "1000000"}
SCENARIOS = {"BASE": "0.12", "MARGIN_X1_5": "0.18"}
CHILD_PAYLOADS = {"config.json", "fills.csv", "equity.csv", "decisions.csv", "report.json"}
CHILD_FILES = CHILD_PAYLOADS | {"manifest.json"}
TOP_PAYLOADS = {"coverage.json", "comparison.csv", "summary.json", "report.md"}
PAIRED_METRICS = (
    "net_pnl", "fees", "slippage", "max_drawdown", "max_drawdown_ratio",
    "opening_blocks", "fills", "closed_trades",
)
CANDIDATE_FIELDS = (
    "observation_index", "datetime_nano", "raw_ts", "strategy_status", "reason",
    "evaluator_candidate_direction", "candidate_direction", "target_lots",
    "logical_direction", "signal_age",
)
CLASSES = ("NO_OPENING_ATTEMPT", "ALL_ATTEMPTS_BLOCKED", "PARTLY_BLOCKED", "NO_OPENING_BLOCK")
DECIMAL_CONTEXT = Context(prec=128, rounding=ROUND_HALF_EVEN)


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                       separators=(",", ":")) + "\n").encode("utf-8")


def json_file(path):
    return json.loads(path.read_bytes())


def csv_file(path):
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        rows = list(reader)
        assert None not in (reader.fieldnames or [])
        assert all(None not in row for row in rows), (path, rows)
        return rows


def csv_bytes(rows, columns):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def tree(path):
    return {file.relative_to(path).as_posix(): file.read_bytes()
            for file in path.rglob("*") if file.is_file()}


def q(value):
    """Exact rational conversion of a ledger decimal, never through float."""
    return Fraction(Decimal(str(value)))


def decimal(value):
    with localcontext(DECIMAL_CONTEXT):
        return Decimal(value.numerator) / Decimal(value.denominator)


def number_equal(actual, expected):
    if expected is None:
        assert actual is None or actual == "", actual
    elif isinstance(expected, Fraction):
        assert q(actual) == expected, (actual, expected)
    elif isinstance(expected, Decimal):
        assert Decimal(str(actual)) == expected, (actual, expected)
    elif isinstance(expected, int):
        assert not isinstance(actual, bool)
        assert int(actual) == expected, (actual, expected)
    else:
        assert actual == expected, (actual, expected)


def make_base(root):
    path = root / "base.json"
    path.write_bytes(json_bytes(BASE))
    return path


def make_bundle(root, paths, *, empty_symbols=(), reverse_rows=False):
    """Publish valid transport schema; the generated observations are SYNTHETIC."""
    bars, contracts = [], []
    first = datetime(2023, 11, 1, tzinfo=timezone.utc)
    for symbol in sorted(set(paths) | set(empty_symbols)):
        expiry = f"2024-{int(symbol[-2:]):02d}-15T15:00:00+08:00"
        contracts.append({
            "raw_symbol": symbol, "product": "SHFE.RB", "expiry_at": expiry,
            "instance_id": f"{symbol}|{expiry}", "identity_status": "reconstructed_static",
            "fixture": "SYNTHETIC", "instrument_class": "FUTURE",
        })
        if symbol not in paths:
            continue
        closes, opens = paths[symbol]
        opens = closes if opens is None else opens
        assert len(closes) == len(opens)
        for index, (closing, opening) in enumerate(zip(closes, opens)):
            close, opened = Decimal(str(closing)), Decimal(str(opening))
            ts = first + timedelta(days=index)
            bars.append(ContractBar(
                symbol=symbol, duration_seconds=86400, datetime_nano=int(ts.timestamp()) * 10**9,
                ts=ts, source="SYNTHETIC_generated_capital_stress_prices", open=opened,
                high=max(opened, close) + Decimal("0.1"),
                low=min(opened, close) - Decimal("0.1"), close=close,
                volume=Decimal("1000"),
            ))
    rows = bars_rows_from_bars(bars)
    if reverse_rows:
        rows.reverse()
    source = root / "input"
    publish_input_bundle(
        source, dataset_kind="real_export", created_at="2024-01-01T00:00:00+00:00",
        bars_rows=rows, contracts=contracts,
        metadata={"fixture": "SYNTHETIC", "source": "generated offline test prices"},
        availability={"fixture": "SYNTHETIC", "historical_available_at": "NOT_VERIFIED"},
        request_info={"fixture": "SYNTHETIC"}, source_hashes={"SYNTHETIC_generator": "1" * 64},
        read_only_transaction={"executed": False},
        clock_policy={"fixture": "SYNTHETIC", "d_status": "unavailable"},
        notes={"fixture": "SYNTHETIC; never historical PIT or execution data"},
    )
    return source


def matrix_paths():
    return {
        "SHFE.rb2405": (list(range(3600, 3642)), None),
        "SHFE.rb2406": ([3100] * 60, None),
        "SHFE.rb2407": (list(range(3400, 3460)) + [4000, 3500, 3450, 3460, 3470], None),
    }


def arguments(source, base, output):
    return ["--input-bundle", str(source), "--base-config", str(base), "--output", str(output)]


def invoke(cli, source, base, output, capsys, extra=()):
    try:
        code = cli.main(arguments(source, base, output) + list(extra))
    except SystemExit as error:
        code = error.code
    captured = capsys.readouterr()
    statuses = []
    for line in (captured.out + "\n" + captured.err).splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and "status" in value:
            statuses.append(value)
    assert statuses, (code, captured.out, captured.err)
    return code, statuses[-1]


def assert_failed_output(output):
    marker = output / "manifest.json"
    if marker.is_file():
        try:
            manifest = json_file(marker)
        except ValueError:
            return  # A partially written marker is never a complete publication.
        assert manifest.get("status") != "complete", manifest


def reject(cli, source, base, output, capsys, *, extra=(), preflight=True):
    code, status = invoke(cli, source, base, output, capsys, extra)
    assert code == 2, status
    assert status["status"] == ("rejected" if preflight else "incomplete"), status
    assert isinstance(status.get("reason"), str) and status["reason"]
    assert isinstance(status.get("error_type"), str) and status["error_type"]
    if preflight:
        assert not output.exists()
    else:
        assert_failed_output(output)
    return status


@pytest.fixture(scope="module")
def cli():
    name = "_ak_historical_daily_capital_stress_tests"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(name, None)


@pytest.fixture(scope="module")
def matrix(cli, tmp_path_factory):
    root = tmp_path_factory.mktemp("SYNTHETIC_capital_stress")
    source = make_bundle(root, matrix_paths(), empty_symbols=("SHFE.rb2408",))
    base = make_base(root)
    output = root / "one"
    before = tree(source)
    result = cli.run_capital_stress(source, base, output)
    assert result["status"] == "complete"
    assert tree(source) == before
    return source, base, output


@pytest.fixture
def inputs(tmp_path):
    return make_bundle(tmp_path, {"SHFE.rb2405": ([3100], None)}), make_base(tmp_path)


def cell_key(row):
    return row["symbol"], row["strategy"], row["capital_id"], row["scenario"]


def cell_directory(output, row):
    expected = output / "cells" / row["symbol"] / row["strategy"] / row["capital_id"] / row["scenario"]
    assert (output / row["child_path"]).resolve() == expected.resolve()
    return expected


def accounting_oracle(source, child):
    """Reconstruct the account independently from pending targets and fill legs."""
    config = json_file(child / "config.json")
    report = json_file(child / "report.json")
    assumptions = config["assumptions"]
    costs = {key: q(value) for key, value in assumptions.items()}
    initial = cash = peak = costs["initial_cash"]
    multiplier, margin, ratio = (costs[key] for key in ("multiplier", "margin_rate", "max_margin_ratio"))
    movement = costs["price_tick"] * costs["slippage_ticks"]
    marks, decisions, fills = (csv_file(child / name) for name in ("equity.csv", "decisions.csv", "fills.csv"))
    bars = sorted((row for row in csv_file(source / "bars.csv") if row["symbol"] == config["symbol"]),
                  key=lambda row: int(row["datetime_nano"]))
    assert len(bars) == len(marks) == len(decisions) == report["observations"]
    by_index = defaultdict(list)
    for fill in fills:
        by_index[int(fill["observation_index"])].append(fill)
    assert set(by_index) <= set(range(len(bars)))
    position, entry, pending = 0, Fraction(0), None
    realized = fees = slippage = gross = Fraction(0)
    min_cash = min_equity = initial
    close_available, close_headroom, opening_headroom = [], [], []
    max_drawdown, max_ratio = Fraction(0), Fraction(0)
    attempts = blocks = exhausted = margin_blocks = breaches = nonpositive = 0
    opening_fills = closed_trades = 0

    def leg(fill, index, offset, side, price, phase):
        nonlocal cash, realized, fees, slippage, position, entry, min_cash, opening_fills, closed_trades
        lots = abs(position) if offset == "close" else int(assumptions["lots"])
        flat = costs["fee_per_lot"] * lots
        rate = costs["fee_rate"] * price * multiplier * lots
        slip = movement * multiplier * lots
        pnl = (price - entry) * position * multiplier if offset == "close" else Fraction(0)
        assert (int(fill["observation_index"]), fill["offset"], int(fill["side"]), int(fill["lots"]), fill["phase"]) == (
            index, offset, side, lots, phase,
        )
        number_equal(fill["ideal_price"], price)
        number_equal(fill["effective_price"], price + side * movement)
        number_equal(fill["fee_per_lot_component"], flat)
        number_equal(fill["fee_rate_component"], rate)
        number_equal(fill["fee"], flat + rate)
        number_equal(fill["slippage"], slip)
        number_equal(fill["realized_pnl"], pnl)
        assert fill["signal_index"] == ("" if phase == "terminal_close" else str(index - 1))
        cash += pnl - flat - rate - slip
        realized += pnl
        fees += flat + rate
        slippage += slip
        position = 0 if offset == "close" else side * lots
        entry = Fraction(0) if offset == "close" else price
        opening_fills += offset == "open"
        closed_trades += offset == "close"
        number_equal(fill["cash_after"], cash)
        number_equal(fill["position_after"], position)
        min_cash = min(min_cash, cash)

    for index, (bar, mark, decision) in enumerate(zip(bars, marks, decisions)):
        opened, close = q(bar["open"]), q(bar["close"])
        for ledger_row in (mark, decision, *by_index[index]):
            assert int(ledger_row["observation_index"]) == index
            assert ledger_row["datetime_nano"] == bar["datetime_nano"]
            assert ledger_row["raw_ts"] == bar["ts"]
        before_position = position
        daily = Fraction(0) if index == 0 else position * multiplier * (opened - q(bars[index - 1]["close"]))
        current = iter(by_index[index])
        status = "no_preceding_target"
        attempted_after_cost = required_opening_margin = None
        if pending is not None:
            if pending == position:
                status = "target_unchanged"
            else:
                status = "closed" if position else "flat"
                if position:
                    leg(next(current), index, "close", -1 if position > 0 else 1, opened, "next_open")
                if pending:
                    lots = abs(pending)
                    charge = costs["fee_per_lot"] * lots + costs["fee_rate"] * opened * multiplier * lots + movement * multiplier * lots
                    attempted_after_cost = cash - charge
                    required_opening_margin = lots * opened * multiplier * margin
                    attempts += 1
                    opening_headroom.append(attempted_after_cost * ratio - required_opening_margin)
                    if attempted_after_cost <= 0:
                        status = "opening_costs_exhaust_equity"
                        exhausted += 1
                        blocks += 1
                    elif required_opening_margin > attempted_after_cost * ratio:
                        status = "opening_margin_blocked"
                        margin_blocks += 1
                        blocks += 1
                    else:
                        leg(next(current), index, "open", 1 if pending > 0 else -1, opened, "next_open")
                        status = "reversed" if before_position else "opened"
        assert decision["execution_status"] == status
        number_equal(decision["opening_equity_after_costs"], attempted_after_cost)
        number_equal(decision["opening_required_margin"], required_opening_margin)
        daily += position * multiplier * (close - opened)
        if index == len(bars) - 1 and position:
            leg(next(current), index, "close", -1 if position > 0 else 1, close, "terminal_close")
        assert next(current, None) is None, (index, by_index[index])
        unrealized = position * multiplier * (close - entry)
        equity = cash + unrealized
        gross += daily
        required_margin = abs(position) * close * multiplier * margin
        available = equity - required_margin
        headroom = equity * ratio - required_margin
        breached = bool(position and (equity <= 0 or required_margin > equity * ratio))
        peak = max(peak, equity)
        drawdown = peak - equity
        drawdown_ratio = drawdown / peak
        max_drawdown = max(max_drawdown, drawdown)
        max_ratio = max(max_ratio, drawdown_ratio)
        expected_mark = dict(
            close=close, position_lots=position, cash=cash, realized_pnl=realized,
            unrealized_pnl=unrealized, equity=equity, daily_gross_pnl=daily, gross_pnl=gross,
            fees=fees, slippage=slippage, net_pnl=equity - initial,
            required_margin=required_margin, available_equity=available,
            drawdown=drawdown, drawdown_ratio=decimal(drawdown_ratio),
        )
        for name, value in expected_mark.items():
            number_equal(mark[name], value)
        assert mark["margin_breached"] == str(breached).lower()
        assert mark["terminal"] == str(index == len(bars) - 1).lower()
        assert cash == initial + realized - fees - slippage
        assert gross == realized + unrealized
        min_equity = min(min_equity, equity)
        close_available.append(available)
        close_headroom.append(headroom)
        breaches += breached
        nonpositive += equity <= 0
        pending = None if decision["target_lots"] == "" else int(decision["target_lots"])

    assert position == 0 and gross == realized
    execution_class = "NO_OPENING_ATTEMPT" if not attempts else "ALL_ATTEMPTS_BLOCKED" if not opening_fills else "PARTLY_BLOCKED" if blocks else "NO_OPENING_BLOCK"
    metrics = dict(
        fills=len(fills), opening_fills=opening_fills, closed_trades=closed_trades,
        turnover_lots=sum(int(fill["lots"]) for fill in fills),
        turnover_notional=sum((q(fill["ideal_price"]) * int(fill["lots"]) * multiplier for fill in fills), Fraction(0)),
        opening_attempts=attempts, opening_blocks=blocks, opening_margin_blocked=margin_blocks,
        opening_costs_exhaust_equity=exhausted,
        opening_blocked_rate=decimal(Fraction(blocks, attempts)) if attempts else None,
        execution_class=execution_class, gross_pnl=gross, fees=fees, slippage=slippage,
        net_pnl=equity - initial, total_return=decimal((equity - initial) / initial),
        ending_cash=cash, ending_equity=equity, ending_position_lots=0,
        max_drawdown=max_drawdown, max_drawdown_ratio=decimal(max_ratio),
        minimum_recorded_cash=min_cash, minimum_closing_equity=min_equity,
        peak_close_required_margin=max(q(mark["required_margin"]) for mark in marks),
        minimum_close_available_equity=min(close_available),
        minimum_close_margin_budget_headroom=min(close_headroom),
        margin_breach_observations=breaches, nonpositive_equity_observations=nonpositive,
        minimum_opening_margin_budget_headroom=min(opening_headroom) if opening_headroom else None,
    )
    assert attempts == opening_fills + blocks
    for name in ("fills", "closed_trades", "opening_blocks", "gross_pnl", "fees", "slippage",
                 "net_pnl", "ending_cash", "ending_equity", "ending_position_lots", "total_return",
                 "max_drawdown", "max_drawdown_ratio", "turnover_lots", "turnover_notional",
                 "margin_breach_observations", "nonpositive_equity_observations"):
        number_equal(report[name], metrics[name])
    assert report["reconciliation"] and all(value is True for value in report["reconciliation"].values())
    return metrics, marks, decisions, fills


def path_differences(first, second):
    _, first_marks, _, first_fills = first
    _, second_marks, _, second_fills = second
    assert len(first_marks) == len(second_marks)
    positions = sum(a["position_lots"] != b["position_lots"] for a, b in zip(first_marks, second_marks))

    def events(fills):
        grouped = defaultdict(list)
        for fill in fills:
            grouped[int(fill["observation_index"])].append((
                fill["phase"], fill["offset"], int(fill["side"]), int(fill["lots"]),
                None if fill["signal_index"] == "" else int(fill["signal_index"]), q(fill["ideal_price"]),
            ))
        return grouped

    first_events, second_events = events(first_fills), events(second_fills)
    fill_differences = sum(first_events[index] != second_events[index] for index in range(len(first_marks)))
    return positions, fill_differences


def distribution(values):
    if not values:
        return {"min": None, "median": None, "max": None}
    values = sorted(Decimal(str(value)) for value in values)
    middle = len(values) // 2
    with localcontext(DECIMAL_CONTEXT):
        median = values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / Decimal(2)
    return {"min": values[0], "median": median, "max": values[-1]}


def test_registered_base_fixture_has_only_the_precommitted_nine_exact_values():
    assert BASE_FIXTURE.read_bytes() == json_bytes(BASE)
    assert ScenarioConfig.from_mapping(BASE).to_dict() == BASE


def test_matrix_population_natural_warmup_and_no_candidate_accounts_are_retained(matrix):
    source, base, output = matrix
    rows = csv_file(output / "comparison.csv")
    expected_keys = [(symbol, strategy, capital, scenario)
                     for symbol in sorted(matrix_paths()) for strategy in STRATEGIES
                     for capital in CAPITALS for scenario in SCENARIOS]
    assert [cell_key(row) for row in rows] == expected_keys
    assert len(set(cell_key(row) for row in rows)) == len(rows) == 54
    configs = {name.relative_to(output).as_posix(): json_file(name) for name in (output / "configs").iterdir()}
    assert set(configs) == {f"configs/{capital}__{scenario}.json" for capital in CAPITALS for scenario in SCENARIOS}
    for capital, cash in CAPITALS.items():
        for scenario, margin in SCENARIOS.items():
            assert configs[f"configs/{capital}__{scenario}.json"] == dict(BASE, initial_cash=cash, margin_rate=margin)
    coverage = json_file(output / "coverage.json")
    expected_counts = dict(bar_count=167, declared_contracts=4, contracts_with_bars=3,
                           declared_without_interval_bars=1, common_coverage_contracts=2,
                           insufficient_common_coverage_contracts=1, matrix_cells=54,
                           common_matrix_cells=36, insufficient_matrix_cells=18)
    for name, value in expected_counts.items():
        assert coverage[name] == value
    contracts = coverage["contracts"]
    assert [row["symbol"] for row in contracts] == ["SHFE.rb2405", "SHFE.rb2406", "SHFE.rb2407", "SHFE.rb2408"]
    assert [row["observations"] for row in contracts] == [42, 60, 65, 0]
    declarations = {row["raw_symbol"]: row for row in json_file(source / "contracts.json")}
    input_rows = csv_file(source / "bars.csv")
    for contract in contracts:
        bars = sorted((bar for bar in input_rows if bar["symbol"] == contract["symbol"]),
                      key=lambda bar: int(bar["datetime_nano"]))
        assert contract["contract_instance_id"] == declarations[contract["symbol"]]["instance_id"]
        assert contract["first_raw_ts"] == (bars[0]["ts"] if bars else None)
        assert contract["last_raw_ts"] == (bars[-1]["ts"] if bars else None)
        assert contract["first_datetime_nano"] == (int(bars[0]["datetime_nano"]) if bars else None)
        assert contract["last_datetime_nano"] == (int(bars[-1]["datetime_nano"]) if bars else None)
        assert contract["potential_next_open_slots"] == {
            strategy: max(0, len(bars) - needed) for strategy, needed in REQUIRED_BARS.items()
        }
    for row in rows:
        n = {"SHFE.rb2405": 42, "SHFE.rb2406": 60, "SHFE.rb2407": 65}[row["symbol"]]
        assert int(row["observations"]) == n
        assert int(row["required_bars"]) == REQUIRED_BARS[row["strategy"]]
        assert int(row["potential_next_open_slots"]) == max(0, n - REQUIRED_BARS[row["strategy"]])
        assert row["coverage_status"] == ("MEETS_60_OBSERVATIONS" if n >= 60 else "INSUFFICIENT_COMMON_COVERAGE")
        assert row["capital_reference"] == "C1000000" and row["margin_reference"] == "BASE"
        assert row["evidence_level"] == "SCENARIO"
        assert row["contract_instance_id"] == declarations[row["symbol"]]["instance_id"]
    short = next(row for row in rows if cell_key(row) == ("SHFE.rb2405", "donchian_trend", "C15000", "BASE"))
    assert int(short["opening_fills"]) == 1  # No artificial common warmup to bar 60.
    no_candidate = [row for row in rows if row["symbol"] == "SHFE.rb2406"]
    assert all(row["execution_class"] == "NO_OPENING_ATTEMPT" for row in no_candidate)
    assert all(row["opening_blocked_rate"] == "" and int(row["fills"]) == 0 for row in no_candidate)
    ma_equal_60 = next(row for row in no_candidate if row["strategy"] == "ma_trend")
    assert int(ma_equal_60["potential_next_open_slots"]) == 0


def test_all_metrics_pairings_candidates_and_group_descriptions_recompute_from_ledgers(matrix):
    source, _, output = matrix
    rows = csv_file(output / "comparison.csv")
    summary = json_file(output / "summary.json")
    assert [cell_key(row) for row in summary["cells"]] == [cell_key(row) for row in rows]
    summaries = {cell_key(row): row for row in summary["cells"]}
    oracles = {cell_key(row): accounting_oracle(source, cell_directory(output, row)) for row in rows}
    for row in rows:
        key = cell_key(row)
        metrics, _, decisions, _ = oracles[key]
        for name, expected in metrics.items():
            number_equal(row[name], expected)
            number_equal(summaries[key][name], expected)
        for field, ledger_field in (("candidate_counts", "candidate_direction"), ("reason_counts", "reason"),
                                    ("execution_status_counts", "execution_status"), ("strategy_status_counts", "strategy_status")):
            expected_counts = Counter(record[ledger_field] or "null" for record in decisions)
            assert summaries[key][field] == dict(expected_counts)
        expected_first = next((index for index, record in enumerate(decisions) if record["candidate_direction"] != ""), None)
        assert summaries[key]["first_nonnull_candidate_index"] == expected_first
        for prefix, reference_key in (
            ("capital_reference", (key[0], key[1], "C1000000", key[3])),
            ("margin_reference", (key[0], key[1], key[2], "BASE")),
        ):
            reference = oracles[reference_key]
            for name in PAIRED_METRICS:
                with localcontext(DECIMAL_CONTEXT):
                    current_value, reference_value = metrics[name], reference[0][name]
                    delta = current_value - reference_value
                number_equal(row[f"{prefix}_delta_{name}"], delta)
            position_diff, fill_diff = path_differences(oracles[key], reference)
            assert int(row[f"{prefix}_position_difference_observations"]) == position_diff
            assert int(row[f"{prefix}_fill_event_difference_observations"]) == fill_diff
            reference_row = next(value for value in rows if cell_key(value) == reference_key)
            assert row[f"{prefix}_child_path"] == reference_row["child_path"]
    for symbol in matrix_paths():
        for strategy in STRATEGIES:
            siblings = [oracles[(symbol, strategy, capital, scenario)][2] for capital in CAPITALS for scenario in SCENARIOS]
            expected = [tuple(record[field] for field in CANDIDATE_FIELDS) for record in siblings[0]]
            assert all([tuple(record[field] for field in CANDIDATE_FIELDS) for record in candidate] == expected
                       for candidate in siblings[1:])
    invariance = summary["candidate_invariance"]
    assert len(invariance) == 9
    assert all(record["status"] == "IDENTICAL_ACROSS_SIX_CELLS" and record["cells"] == 6 for record in invariance)
    groups = summary["groups"]
    assert [(group["strategy"], group["capital_id"], group["scenario"]) for group in groups] == [
        (strategy, capital, scenario) for strategy in STRATEGIES for capital in CAPITALS for scenario in SCENARIOS
    ]
    for group in groups:
        selected = [row for row in rows if row["coverage_status"] == "MEETS_60_OBSERVATIONS"
                    and (row["strategy"], row["capital_id"], row["scenario"]) == (group["strategy"], group["capital_id"], group["scenario"])]
        assert group["n"] == len(selected) == 2 and group["status"] == "DESCRIPTIVE"
        expected_classes = {name: sum(row["execution_class"] == name for row in selected) for name in CLASSES}
        assert group["execution_class_counts"] == expected_classes
        for name, metric in (("opening_contracts", "opening_fills"), ("block_contracts", "opening_blocks"),
                             ("breach_contracts", "margin_breach_observations"), ("nonpositive_equity_contracts", "nonpositive_equity_observations")):
            assert group[name] == sum(int(row[metric]) > 0 for row in selected)
        assert group["no_opening_contracts"] == sum(int(row["opening_fills"]) == 0 for row in selected)
        assert group["net_pnl_counts"] == {
            "positive": sum(q(row["net_pnl"]) > 0 for row in selected),
            "zero": sum(q(row["net_pnl"]) == 0 for row in selected),
            "negative": sum(q(row["net_pnl"]) < 0 for row in selected),
        }
        for name in ("net_pnl", "total_return"):
            for statistic, expected in distribution([row[name] for row in selected]).items():
                number_equal(group[name][statistic], expected)
        number_equal(group["worst_max_drawdown"], max(q(row["max_drawdown"]) for row in selected))
        number_equal(group["worst_max_drawdown_ratio"], max(Decimal(row["max_drawdown_ratio"]) for row in selected))
        for prefix in ("capital_reference", "margin_reference"):
            for kind in ("position", "fill_event"):
                assert group[f"{prefix}_{kind}_difference_contracts"] == sum(int(row[f"{prefix}_{kind}_difference_observations"]) > 0 for row in selected)


def test_static_affordability_counts_quotes_without_claiming_opening_attempts(matrix):
    source, _, output = matrix
    coverage = json_file(output / "coverage.json")
    quotes = [q(row["open"]) for row in csv_file(source / "bars.csv")]
    for record in coverage["static_affordability"]:
        capital, scenario = record["capital_id"], record["scenario"]
        margin = q(SCENARIOS[scenario])
        thresholds = [price * q(BASE["multiplier"]) * margin / q(BASE["max_margin_ratio"])
                      + q(BASE["fee_per_lot"]) + q(BASE["fee_rate"]) * price * q(BASE["multiplier"])
                      + q(BASE["slippage_ticks"]) * q(BASE["price_tick"]) * q(BASE["multiplier"])
                      for price in quotes]
        assert record["label"] == "fresh-empty-account stored-open quote affordability"
        assert record["stored_open_quotes"] == len(quotes) == 167
        assert record["affordable_stored_open_quotes"] == sum(q(CAPITALS[capital]) >= threshold for threshold in thresholds)
        number_equal(record["threshold_min"], min(thresholds))
        number_equal(record["threshold_max"], max(thresholds))
    rows = csv_file(output / "comparison.csv")
    assert all(int(row["opening_fills"]) == 0 for row in rows if row["capital_id"] == "C5000")
    assert any(row["execution_class"] == "ALL_ATTEMPTS_BLOCKED" for row in rows if row["capital_id"] == "C5000")
    # Every 3100 quote is affordable at C15000/.18, yet the constant-price
    # contract has no strategy attempt at any margin or capital level.
    assert q("15000") > q("3100") * q("10") * q("0.18") / q("0.4") + q("13.11")
    assert all(row["execution_class"] == "NO_OPENING_ATTEMPT" for row in rows if row["symbol"] == "SHFE.rb2406")


def test_terminal_round_trip_detects_fill_divergence_when_all_closing_positions_are_flat(matrix):
    _, _, output = matrix
    rows = {cell_key(row): row for row in csv_file(output / "comparison.csv")}
    low = rows[("SHFE.rb2405", "donchian_trend", "C15000", "MARGIN_X1_5")]
    base = rows[("SHFE.rb2405", "donchian_trend", "C15000", "BASE")]
    assert int(base["fills"]) == 2 and int(low["fills"]) == 0
    assert int(low["margin_reference_position_difference_observations"]) == 0
    assert int(low["margin_reference_fill_event_difference_observations"]) == 1
    assert int(low["ending_position_lots"]) == int(base["ending_position_lots"]) == 0
    fills = csv_file(cell_directory(output, base) / "fills.csv")
    assert [(int(row["observation_index"]), row["phase"], row["offset"]) for row in fills] == [
        (41, "next_open", "open"), (41, "terminal_close", "close"),
    ]
    high = rows[("SHFE.rb2405", "donchian_trend", "C1000000", "BASE")]
    assert q(base["net_pnl"]) == q(high["net_pnl"]) < 0
    assert q(base["ending_cash"]) != q(high["ending_cash"])
    assert int(base["capital_reference_fill_event_difference_observations"]) == 0
    assert int(base["capital_reference_position_difference_observations"]) == 0
    assert Decimal(base["total_return"]) != Decimal(high["total_return"])


def test_complete_publication_binds_all_children_and_is_byte_deterministic_under_low_caller_precision(cli, matrix, tmp_path):
    source, base, output = matrix
    second = tmp_path / "second"
    with localcontext() as context:
        context.prec = 6
        result = cli.run_capital_stress(source, base, second)
    assert result["status"] == "complete"
    assert tree(output) == tree(second)
    published = tree(output)
    manifest = json.loads(published["manifest.json"])
    assert manifest["schema_version"] == "rb_daily_capital_stress_v1"
    assert manifest["status"] == "complete" and manifest["evidence_level"] == "SCENARIO"
    assert manifest["historical_strict_execution"] is False and manifest["enable_trading"] is False
    assert manifest["broker"] == "NOT_RUN"
    assert manifest["input_manifest_sha256"] == sha256((source / "manifest.json").read_bytes()).hexdigest()
    assert manifest["input_content_hashes"] == json_file(source / "manifest.json")["content_hashes"]
    assert manifest["content_hashes"] == {name: sha256(payload).hexdigest() for name, payload in published.items() if name != "manifest.json"}
    assert len(published) == 6 + 54 * 6 + 5
    assert all(not Path(name).is_absolute() and ".." not in Path(name).parts for name in manifest["content_hashes"])
    assert str(output).encode() not in published["manifest.json"] and str(second).encode() not in published["manifest.json"]
    for row in csv_file(output / "comparison.csv"):
        child = cell_directory(output, row)
        child_tree = tree(child)
        assert set(child_tree) == CHILD_FILES
        child_manifest = json.loads(child_tree["manifest.json"])
        assert child_manifest["status"] == "complete"
        assert child_manifest["content_hashes"] == {name: sha256(child_tree[name]).hexdigest() for name in CHILD_PAYLOADS}
        config_path = output / "configs" / f"{row['capital_id']}__{row['scenario']}.json"
        assert child_manifest["input_config_sha256"] == sha256(config_path.read_bytes()).hexdigest()
        assert child_manifest["input_manifest_sha256"] == manifest["input_manifest_sha256"]
        assert child_manifest["input_content_hashes"] == manifest["input_content_hashes"]
        assert child_manifest["source_hashes"] == json_file(source / "manifest.json")["source_hashes"]
        assert len(child_manifest["code_hashes"]) == 10
        for name, digest in child_manifest["code_hashes"].items():
            assert digest == sha256((ROOT / name).read_bytes()).hexdigest()
            assert manifest["code_hashes"][name] == digest
    report = published["report.md"].decode("utf-8")
    assert "synthetic" in report.lower() and "SCENARIO" in report and "NOT_RUN" in report
    assert json_file(output / "coverage.json")["input_metadata"]["fixture"] == "SYNTHETIC"
    assert json_file(output / "summary.json")["input_metadata"]["fixture"] == "SYNTHETIC"
    for name in TOP_PAYLOADS - {"report.md"}:
        assert name in report


def test_bar_permutation_preserves_sorted_child_accounting_and_candidates(cli, matrix, tmp_path):
    _, _, original = matrix
    source = make_bundle(tmp_path, matrix_paths(), empty_symbols=("SHFE.rb2408",), reverse_rows=True)
    base = make_base(tmp_path)
    output = tmp_path / "permuted"
    assert cli.run_capital_stress(source, base, output)["status"] == "complete"
    assert csv_file(output / "comparison.csv") == csv_file(original / "comparison.csv")
    for row in csv_file(output / "comparison.csv"):
        first, second = cell_directory(original, row), cell_directory(output, row)
        for name in CHILD_PAYLOADS:
            assert (first / name).read_bytes() == (second / name).read_bytes()


def test_all_short_contracts_publish_null_insufficient_descriptive_groups(cli, tmp_path):
    source = make_bundle(tmp_path, {"SHFE.rb2405": (list(range(3600, 3642)), None)})
    output = tmp_path / "short"
    assert cli.run_capital_stress(source, make_base(tmp_path), output)["status"] == "complete"
    summary = json_file(output / "summary.json")
    assert len(summary["cells"]) == len(summary["groups"]) == 18
    assert all(cell["coverage_status"] == "INSUFFICIENT_COMMON_COVERAGE" for cell in summary["cells"])
    for group in summary["groups"]:
        assert group["n"] == 0 and group["status"] == "INSUFFICIENT"
        assert group["net_pnl"] == group["total_return"] == {"min": None, "median": None, "max": None}
        assert group["worst_max_drawdown"] is None and group["worst_max_drawdown_ratio"] is None
        assert group["execution_class_counts"] == dict.fromkeys(CLASSES, 0)


def test_profit_direction_does_not_select_population_or_reset_coverage(cli, tmp_path):
    losing_closes = list(range(3600, 3641)) + [3300] * 20
    losing_opens = losing_closes.copy()
    losing_opens[41] = 4000
    paths = {"SHFE.rb2405": (list(range(3600, 3661)), None),
             "SHFE.rb2406": (losing_closes, losing_opens)}
    source = make_bundle(tmp_path, paths)
    output = tmp_path / "both_directions"
    assert cli.run_capital_stress(source, make_base(tmp_path), output)["status"] == "complete"
    coverage = json_file(output / "coverage.json")
    assert coverage["common_coverage_contracts"] == 2 and coverage["matrix_cells"] == 36
    assert all(row["coverage_status"] == "MEETS_60_OBSERVATIONS" for row in coverage["contracts"])
    rows = csv_file(output / "comparison.csv")
    assert Counter(row["symbol"] for row in rows) == {symbol: 18 for symbol in paths}
    assert all(group["n"] == 2 for group in json_file(output / "summary.json")["groups"])
    pnl = {row["symbol"]: q(row["net_pnl"]) for row in rows
           if (row["strategy"], row["capital_id"], row["scenario"]) == ("donchian_trend", "C1000000", "BASE")}
    assert pnl["SHFE.rb2405"] > 0 and pnl["SHFE.rb2406"] < 0


def test_continuous_losing_account_records_breach_then_cost_exhaustion_without_forced_liquidation(cli, tmp_path):
    closes = list(range(3900, 3941)) + [1000, 800] + [850] * 22
    opens = closes.copy()
    opens[41] = 4000
    opens[42] = 1100  # A recovery gap distinguishes fill cash from worst closing equity.
    source = make_bundle(tmp_path, {"SHFE.rb2405": (closes, opens)})
    output = tmp_path / "loss"
    assert cli.run_capital_stress(source, make_base(tmp_path), output)["status"] == "complete"
    rows = csv_file(output / "comparison.csv")
    for row in rows:
        metrics, _, _, _ = accounting_oracle(source, cell_directory(output, row))
        for name, expected in metrics.items():
            number_equal(row[name], expected)
    target = next(row for row in rows if cell_key(row) == ("SHFE.rb2405", "donchian_trend", "C15000", "BASE"))
    child = cell_directory(output, target)
    marks, decisions, fills = (csv_file(child / name) for name in ("equity.csv", "decisions.csv", "fills.csv"))
    assert marks[41]["position_lots"] == "1" and marks[41]["margin_breached"] == "true"
    assert q(marks[41]["equity"]) < 0
    assert decisions[42]["execution_status"] == "opening_costs_exhaust_equity"
    assert [(int(row["observation_index"]), row["phase"], row["offset"]) for row in fills] == [
        (41, "next_open", "open"), (42, "next_open", "close"),
    ]
    assert q(target["ending_equity"]) < 0 and int(target["nonpositive_equity_observations"]) > 0
    # Free equity and the 40% opening budget are different quantities.
    assert q(target["minimum_close_available_equity"]) != q(target["minimum_close_margin_budget_headroom"])
    assert q(target["minimum_recorded_cash"]) != q(target["minimum_closing_equity"])


def test_reversal_closing_cost_changes_the_next_opening_admission_using_public_pure_api(tmp_path):
    closes = list(range(100, 141)) + [10, 6]
    opens = closes.copy()
    opens[41:43] = [150, 8]
    source = make_bundle(tmp_path, {"SHFE.rb2405": (closes, opens)})
    explicit_synthetic = dict(BASE, initial_cash="1465", fee_per_lot="10", fee_rate="0",
                              slippage_ticks="0", margin_rate="0.1")
    result = run_scenario_backtest(read_input_bundle(source), symbol="SHFE.rb2405",
                                  strategy="donchian_trend", config=ScenarioConfig.from_mapping(explicit_synthetic))
    assert [(fill.observation_index, fill.offset) for fill in result.fills] == [(41, "open"), (42, "close")]
    decision = result.decisions[42]
    assert decision.execution_status == "opening_margin_blocked"
    assert q(decision.opening_equity_after_costs) == Fraction(15)
    assert q(decision.opening_required_margin) == Fraction(8)
    assert Fraction(8) > Fraction(15) * q("0.4")
    assert Fraction(8) <= (Fraction(15) + Fraction(10)) * q("0.4")  # Ignoring the close fee would wrongly admit.
    assert result.fills[-1].cash_after == Decimal("25")


@pytest.mark.parametrize("boundary", ["equality", "exhausted", "negative_after_cost"])
def test_exact_admission_equality_and_cost_exhaustion_use_public_pure_api(tmp_path, boundary):
    source = make_bundle(tmp_path, {"SHFE.rb2405": ([100] * 40 + [101, 101], None)})
    price = Fraction(101)
    opening_cost = q("0.01") + q("0.0001") * price * 10 + 10
    required_margin = price * 10 * q("0.12")
    initial = required_margin / q("0.4") + opening_cost if boundary == "equality" else opening_cost if boundary == "exhausted" else opening_cost / 2
    config = ScenarioConfig.from_mapping(dict(BASE, initial_cash=str(decimal(initial))))
    result = run_scenario_backtest(read_input_bundle(source), symbol="SHFE.rb2405", strategy="donchian_trend", config=config)
    decision = result.decisions[41]
    if boundary == "equality":
        assert len(result.fills) == 2 and decision.execution_status == "opened"
        assert q(decision.opening_required_margin) == q(decision.opening_equity_after_costs) * q("0.4")
    else:
        assert not result.fills and decision.execution_status == "opening_costs_exhaust_equity"
        assert q(decision.opening_equity_after_costs) <= 0
        assert result.equity[-1].cash == config.initial_cash


@pytest.mark.parametrize("missing", ["--input-bundle", "--base-config", "--output"])
def test_cli_requires_each_parameter_without_creating_output(cli, inputs, tmp_path, capsys, missing):
    source, base = inputs
    output = tmp_path / "out"
    argv = arguments(source, base, output)
    index = argv.index(missing)
    del argv[index:index + 2]
    try:
        code = cli.main(argv)
    except SystemExit as error:
        code = error.code
    assert code == 2
    assert not output.exists()


@pytest.mark.parametrize("extra", [["--capital", "C15000"], ["--strategy", "ma_trend"], ["--base-conf", "base.json"],
                                   ["--output", "other"], ["--base-config", "other"], ["--input-bundle", "other"]])
def test_cli_rejects_unknown_abbreviated_and_repeated_arguments(cli, inputs, tmp_path, capsys, extra):
    source, base = inputs
    reject(cli, source, base, tmp_path / "out", capsys, extra=extra)


@pytest.mark.parametrize("problem", ["duplicate", "nonfinite_constant", "nonfinite_string", "overflow_number", "unknown", "missing",
                                     "not_object", "cash_drift", "margin_drift", "fee_drift", "lots_drift", "boolean_lots"])
def test_invalid_or_unregistered_base_is_preflight_rejection(cli, inputs, tmp_path, capsys, problem):
    source, base = inputs
    values = dict(BASE)
    if problem == "duplicate":
        payload = json_bytes(values).replace(b'"lots":1', b'"lots":1,"lots":1')
    elif problem == "nonfinite_constant":
        payload = json_bytes(values).replace(b'"initial_cash":"1000000"', b'"initial_cash":NaN')
    elif problem == "nonfinite_string":
        payload = json_bytes(dict(values, fee_rate="Infinity"))
    elif problem == "overflow_number":
        payload = json_bytes(values).replace(b'"initial_cash":"1000000"', b'"initial_cash":1e9999')
    elif problem == "unknown":
        payload = json_bytes(dict(values, optimize=True))
    elif problem == "missing":
        del values["multiplier"]
        payload = json_bytes(values)
    elif problem == "not_object":
        payload = b"[]\n"
    else:
        key, value = {"cash_drift": ("initial_cash", "5000"), "margin_drift": ("margin_rate", "0.18"),
                      "fee_drift": ("fee_rate", "0.0002"), "lots_drift": ("lots", 2), "boolean_lots": ("lots", True)}[problem]
        payload = json_bytes(dict(values, **{key: value}))
    base.write_bytes(payload)
    reject(cli, source, base, tmp_path / "out", capsys)


def repin_input(source, name, payload):
    (source / name).write_bytes(payload)
    if name != "manifest.json":
        manifest = json_file(source / "manifest.json")
        manifest["content_hashes"][name] = sha256(payload).hexdigest()
        (source / "manifest.json").write_bytes(json_bytes(manifest))


def test_payload_corruption_is_rejected_before_output_creation(cli, inputs, tmp_path, capsys):
    source, base = inputs
    (source / "bars.csv").write_bytes((source / "bars.csv").read_bytes() + b"\n")
    reject(cli, source, base, tmp_path / "out", capsys)


@pytest.mark.parametrize("missing", ["bundle", "base", "manifest.json", "bars.csv", "metadata.json"])
def test_missing_required_input_is_preflight_failure(cli, inputs, tmp_path, capsys, missing):
    source, base = inputs
    if missing == "bundle":
        source = tmp_path / "required_but_missing_bundle"
    elif missing == "base":
        base = tmp_path / "required_but_missing_base.json"
    else:
        (source / missing).unlink()
    reject(cli, source, base, tmp_path / "out", capsys)


@pytest.mark.parametrize("name", ["manifest.json", "contracts.json", "metadata.json", "availability.json"])
@pytest.mark.parametrize("problem", ["duplicate", "nonfinite"])
def test_strict_json_in_all_input_metadata_rejects_even_when_payload_hashes_match(cli, inputs, tmp_path, capsys, name, problem):
    source, base = inputs
    payload = (source / name).read_bytes()
    insertion = b'"SYNTHETIC_probe":1,"SYNTHETIC_probe":1,' if problem == "duplicate" else b'"SYNTHETIC_probe":NaN,'
    payload = payload.replace(b"{", b"{" + insertion, 1)
    repin_input(source, name, payload)
    reject(cli, source, base, tmp_path / "out", capsys)


@pytest.mark.parametrize("problem", ["ambiguous", "duplicate_instance", "not_daily", "duplicate_nano", "undeclared_bar", "non_rb", "bad_symbol"])
def test_invalid_population_or_daily_identity_is_not_filtered_away(cli, inputs, tmp_path, capsys, problem):
    source, base = inputs
    if problem in {"ambiguous", "duplicate_instance"}:
        contracts = json_file(source / "contracts.json")
        duplicate = dict(contracts[0])
        if problem == "ambiguous":
            duplicate["expiry_at"] = "2034-05-15T15:00:00+08:00"
            duplicate["instance_id"] = f"{duplicate['raw_symbol']}|{duplicate['expiry_at']}"
        contracts.append(duplicate)
        repin_input(source, "contracts.json", json_bytes(contracts))
    else:
        rows = csv_file(source / "bars.csv")
        if problem == "not_daily":
            rows[0]["duration_seconds"] = "60"
        elif problem == "duplicate_nano":
            rows.append(dict(rows[0]))
        elif problem == "undeclared_bar":
            rows[0]["symbol"] = "SHFE.rb2410"
        elif problem == "non_rb":
            rows[0]["symbol"] = "SHFE.hc2405"
        else:
            rows[0]["symbol"] = "SHFE.rb2405/../outside"
        repin_input(source, "bars.csv", csv_bytes(rows, BARS_CSV_COLUMNS))
    before = tree(source)
    reject(cli, source, base, tmp_path / "out", capsys)
    assert tree(source) == before


@pytest.mark.parametrize("name,value", [("open", "0"), ("close", "NaN"), ("high", "1"), ("close", "")])
def test_invalid_ohlc_cannot_be_dropped_or_completed(cli, inputs, tmp_path, capsys, name, value):
    source, base = inputs
    rows = csv_file(source / "bars.csv")
    rows[0][name] = value
    repin_input(source, "bars.csv", csv_bytes(rows, BARS_CSV_COLUMNS))
    output = tmp_path / "out"
    code, status = invoke(cli, source, base, output, capsys)
    assert code == 2 and status["status"] in {"rejected", "incomplete"}
    assert status.get("reason") and status.get("error_type")
    if status["status"] == "rejected":
        assert not output.exists()
    assert_failed_output(output)


@pytest.mark.parametrize("kind", ["existing_directory", "existing_file", "foreign_marker", "input_child", "input_alias", "base_child",
                                  "src", "scripts", "tests", "tools", "docs", "device"])
def test_unsafe_outputs_preserve_existing_bytes_and_create_nothing(cli, inputs, tmp_path, capsys, kind):
    source, base = inputs
    output = tmp_path / "out"
    if kind == "existing_directory":
        output.mkdir()
    elif kind == "existing_file":
        output.write_bytes(b"SYNTHETIC existing file belongs to another writer")
    elif kind == "foreign_marker":
        output.mkdir()
        (output / "manifest.json").write_bytes(b"SYNTHETIC other writer marker")
    elif kind == "input_child":
        output = source / "out"
    elif kind == "input_alias":
        output = source / ".." / source.name / "out"
    elif kind == "base_child":
        output = base / "out"
    elif kind == "device":
        output = tmp_path / "NUL"
    else:
        output = ROOT / kind / "SYNTHETIC_capital_stress_forbidden_output"
        assert not output.exists()
    before = tree(tmp_path)
    code, status = invoke(cli, source, base, output, capsys)
    assert code == 2 and status["status"] == "rejected"
    assert tree(tmp_path) == before
    if kind in {"src", "scripts", "tests", "tools", "docs"}:
        assert not output.exists()


@pytest.mark.parametrize("kind", ["dangling_output", "parent_alias", "input_alias"])
def test_symlink_output_and_ancestor_alias_are_rejected_without_following(cli, inputs, tmp_path, capsys, kind):
    source, base = inputs
    link = tmp_path / "link"
    target = tmp_path / "absent" if kind == "dangling_output" else source if kind == "input_alias" else tmp_path / "real_parent"
    if kind == "parent_alias":
        target.mkdir()
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:
            pytest.skip("Windows test host does not grant symbolic-link creation")
        raise
    output = link if kind == "dangling_output" else link / "out"
    original_target = os.readlink(link)
    code, status = invoke(cli, source, base, output, capsys)
    assert code == 2 and status["status"] == "rejected"
    assert os.readlink(link) == original_target
    assert not (target / "out").exists()
    if kind == "dangling_output":
        assert not target.exists()


def rewrite_child_payload(child, name, mutator):
    path = child / name
    if name.endswith(".json"):
        payload = json_file(path)
        mutator(payload)
        data = json_bytes(payload)
    else:
        rows = csv_file(path)
        columns = list(rows[0])
        mutator(rows)
        data = csv_bytes(rows, columns)
    path.write_bytes(data)
    manifest = json_file(child / "manifest.json")
    manifest["content_hashes"][name] = sha256(data).hexdigest()
    (child / "manifest.json").write_bytes(json_bytes(manifest))


@pytest.mark.parametrize("fault", ["child_exception", "incomplete", "identity", "assumptions", "reconciliation", "nonfinite_equity",
                                   "wrong_observation", "unpaired_attempt", "missing_payload", "extra_member", "code_pin", "bad_csv_header"])
def test_bad_bound_child_artifacts_never_receive_complete_publication(cli, inputs, tmp_path, capsys, monkeypatch, fault):
    source, base = inputs
    output = tmp_path / "out"
    real_writer = cli.run_history_scenario
    calls = []

    def corrupt_after_real_replay(*args, **kwargs):
        result = real_writer(*args, **kwargs)
        child = Path(args[1])
        calls.append(child)
        if fault == "child_exception":
            raise RuntimeError("SYNTHETIC child failure after actual replay")
        if fault == "incomplete":
            manifest = json_file(child / "manifest.json")
            manifest["status"] = "incomplete"
            (child / "manifest.json").write_bytes(json_bytes(manifest))
        elif fault == "identity":
            rewrite_child_payload(child, "report.json", lambda payload: payload.update(symbol="SHFE.rb2999"))
        elif fault == "assumptions":
            rewrite_child_payload(child, "config.json", lambda payload: payload["assumptions"].update(lots=2))
        elif fault == "reconciliation":
            rewrite_child_payload(child, "report.json", lambda payload: payload["reconciliation"].update(terminal_flat=False))
        elif fault == "nonfinite_equity":
            rewrite_child_payload(child, "equity.csv", lambda rows: rows[0].update(equity="NaN"))
        elif fault == "wrong_observation":
            rewrite_child_payload(child, "equity.csv", lambda rows: rows[0].update(datetime_nano="1"))
        elif fault == "unpaired_attempt":
            rewrite_child_payload(child, "decisions.csv", lambda rows: rows[0].update(opening_required_margin="1"))
        elif fault == "missing_payload":
            (child / "fills.csv").unlink()
        elif fault == "extra_member":
            (child / "SYNTHETIC_foreign_member").write_bytes(b"keep this other writer file")
        elif fault == "bad_csv_header":
            path = child / "equity.csv"
            payload = path.read_bytes().replace(b",close,", b",SYNTHETIC_unexpected_close,", 1)
            assert payload != path.read_bytes()
            path.write_bytes(payload)
            manifest = json_file(child / "manifest.json")
            manifest["content_hashes"]["equity.csv"] = sha256(payload).hexdigest()
            (child / "manifest.json").write_bytes(json_bytes(manifest))
        else:
            manifest = json_file(child / "manifest.json")
            first_pin = next(iter(manifest["code_hashes"]))
            manifest["code_hashes"][first_pin] = "0" * 64
            (child / "manifest.json").write_bytes(json_bytes(manifest))
        return result

    monkeypatch.setattr(cli, "run_history_scenario", corrupt_after_real_replay)
    reject(cli, source, base, output, capsys, preflight=False)
    assert len(calls) == 1
    if fault == "extra_member":
        assert (calls[0] / "SYNTHETIC_foreign_member").read_bytes() == b"keep this other writer file"


def test_bound_candidate_reason_difference_across_six_cells_is_publication_failure(cli, inputs, tmp_path, capsys, monkeypatch):
    source, base = inputs
    output = tmp_path / "out"
    real_writer = cli.run_history_scenario
    calls = []

    def change_only_one_candidate_signature(*args, **kwargs):
        result = real_writer(*args, **kwargs)
        child = Path(args[1])
        calls.append(child)
        if len(calls) == 1:
            rewrite_child_payload(child, "decisions.csv", lambda rows: rows[0].update(reason="SYNTHETIC_changed_candidate_reason"))
        return result

    monkeypatch.setattr(cli, "run_history_scenario", change_only_one_candidate_signature)
    status = reject(cli, source, base, output, capsys, preflight=False)
    assert len(calls) >= 6 and "invariance" in status["reason"].lower()


def test_late_mutation_of_an_already_validated_child_payload_cannot_be_resealed(cli, inputs, tmp_path, capsys, monkeypatch):
    source, base = inputs
    output = tmp_path / "out"
    real_writer = cli.run_history_scenario
    calls = []
    original = changed = None

    def mutate_prior_child_after_second_real_replay(*args, **kwargs):
        nonlocal original, changed
        result = real_writer(*args, **kwargs)
        calls.append(Path(args[1]))
        if len(calls) == 2:
            prior = calls[0] / "report.json"
            original = prior.read_bytes()
            changed = original + b"\n"
            prior.write_bytes(changed)
        return result

    monkeypatch.setattr(cli, "run_history_scenario", mutate_prior_child_after_second_real_replay)
    status = reject(cli, source, base, output, capsys, preflight=False)
    assert original is not None and original != changed and len(calls) >= 2
    assert (calls[0] / "report.json").read_bytes() == changed
    assert "closure" in status["reason"].lower() or "hash" in status["reason"].lower()


@pytest.mark.parametrize("fault", ["input", "base", "derived_config", "source"])
def test_midrun_captured_input_config_or_source_drift_cannot_mix_revisions(cli, inputs, tmp_path, capsys, monkeypatch, fault):
    source, base = inputs
    output = tmp_path / "out"
    real_writer = cli.run_history_scenario
    real_read = Path.read_bytes
    changed = False
    calls = []

    def source_read(path):
        payload = real_read(path)
        if changed and fault == "source" and path.resolve() == SCRIPT.resolve():
            return payload + b"\n# SYNTHETIC simulated source drift; disk source is unchanged.\n"
        return payload

    def drift_after_real_replay(*args, **kwargs):
        nonlocal changed
        result = real_writer(*args, **kwargs)
        calls.append(Path(args[1]))
        if not changed:
            if fault == "input":
                repin_input(source, "availability.json", json_bytes({"fixture": "SYNTHETIC", "revision": "changed"}))
            elif fault == "base":
                base.write_bytes(base.read_bytes() + b"\n")
            elif fault == "derived_config":
                config = Path(kwargs["config_path"])
                config.write_bytes(config.read_bytes() + b"\n")
            changed = True
        return result

    monkeypatch.setattr(Path, "read_bytes", source_read)
    monkeypatch.setattr(cli, "run_history_scenario", drift_after_real_replay)
    reject(cli, source, base, output, capsys, preflight=False)
    assert changed and calls
    if fault == "source":
        assert real_read(SCRIPT) != source_read(SCRIPT)


def test_competing_top_manifest_is_preserved_and_cannot_be_overwritten_by_complete(cli, inputs, tmp_path, capsys, monkeypatch):
    source, base = inputs
    output = tmp_path / "out"
    foreign = json_bytes({"status": "incomplete", "fixture": "SYNTHETIC_OTHER_WRITER"})
    real_writer = cli.run_history_scenario
    inserted = False

    def insert_after_real_replay(*args, **kwargs):
        nonlocal inserted
        result = real_writer(*args, **kwargs)
        if not inserted:
            with (output / "manifest.json").open("xb") as stream:
                stream.write(foreign)
            inserted = True
        return result

    monkeypatch.setattr(cli, "run_history_scenario", insert_after_real_replay)
    reject(cli, source, base, output, capsys, preflight=False)
    assert inserted and (output / "manifest.json").read_bytes() == foreign


def test_top_payload_write_failure_returns_native_error_and_keeps_input_unchanged(cli, inputs, tmp_path, capsys, monkeypatch):
    source, base = inputs
    output = tmp_path / "out"
    before = tree(source)
    real_open = Path.open
    injected = False

    def fail_one_top_payload(path, mode="r", *args, **kwargs):
        nonlocal injected
        if path == output / "summary.json" and any(letter in mode for letter in "xwa"):
            injected = True
            raise OSError("SYNTHETIC top payload write failure")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_one_top_payload)
    reject(cli, source, base, output, capsys, preflight=False)
    assert injected
    assert tree(source) == before


def test_partial_complete_marker_write_is_repaired_only_on_its_owned_descriptor(cli, inputs, tmp_path, capsys, monkeypatch):
    source, base = inputs
    output = tmp_path / "out"
    real_open = Path.open
    injected = False

    class InterruptedMarker:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def write(self, payload):
            nonlocal injected
            if not injected and b'"status":"complete"' in payload:
                injected = True
                self.stream.write(payload[:len(payload) // 2])
                self.stream.flush()
                raise OSError("SYNTHETIC partial complete marker write")
            return self.stream.write(payload)

    def interrupt_owned_marker(path, mode="r", *args, **kwargs):
        stream = real_open(path, mode, *args, **kwargs)
        return InterruptedMarker(stream) if path == output / "manifest.json" and mode == "xb" else stream

    monkeypatch.setattr(Path, "open", interrupt_owned_marker)
    reject(cli, source, base, output, capsys, preflight=False)
    assert injected and json_file(output / "manifest.json")["status"] == "incomplete"


def test_native_cli_exit_and_machine_status_are_separate_verified_facts(inputs, tmp_path):
    source, base = inputs
    output = tmp_path / "native_success"
    success = subprocess.run([sys.executable, str(SCRIPT), *arguments(source, base, output)], cwd=ROOT,
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    assert success.returncode == 0, (success.returncode, success.stdout, success.stderr)
    assert json.loads(success.stdout.splitlines()[-1])["status"] == "complete"
    failure_output = tmp_path / "native_rejected"
    negative = subprocess.run([sys.executable, str(SCRIPT), *arguments(source, base, failure_output), "--capital", "C5000"],
                              cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    assert negative.returncode == 2, (negative.returncode, negative.stdout, negative.stderr)
    assert json.loads(negative.stdout.splitlines()[-1])["status"] == "rejected"
    assert not failure_output.exists()
