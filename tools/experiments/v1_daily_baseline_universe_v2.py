"""K46: aggregate accepted daily accounts; never import or run a trading engine.

All money is exact rational arithmetic. Normalized daily observations have a
precommitted integer representation of 10**-24; no floating-point tolerance is
used. The public CHECK describes this build, while the old research generation
and its old CHECK files remain immutable.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path


PRODUCTS = ("SHFE.RB", "INE.SC", "DCE.M", "CZCE.TA", "CFFEX.IF", "GFEX.SI")
METHODS = ("donchian_trend", "ma_trend", "zscore_reversion", "flat_control", "buy_hold_control")
STRATEGIES = METHODS[:3]
CAPITALS = ("1000000", "300000", "100000", "50000")
YEARS = tuple(range(2020, 2027))
MULTIPLIERS = dict(zip(PRODUCTS, (10, 1000, 10, 5, 300, 5)))
EXPECTED_SUPPORT = dict(zip(PRODUCTS, ((84, 17847), (92, 29738), (55, 11851), (83, 17773), (76, 5931), (49, 9325))))
SCALE = 10**24
SHANGHAI = timezone(timedelta(hours=8))
START, END = date(2020, 7, 20), date(2026, 9, 5)
REPORT = "docs/research/v1-daily-baseline-universe.md"
PUBLIC = "docs/research/v1-daily-baseline-universe/v2/"
TOOL = "tools/experiments/v1_daily_baseline_universe_v2.py"
CONFIG = "config/research/v1_daily_baseline_universe_v2.json"
TEST = "tests/experiments/test_v1_daily_baseline_universe_v2.py"
BUILD_FILES = (TOOL, CONFIG, TEST)
TABLES = ("summary.csv", "annual.csv", "readiness.csv", "combined-daily.csv", "combined-summary.csv", "combined-annual.csv", "capital-events.csv", "facts.csv")
EVENTS = ("opening_attempts", "opening_fills", "opening_blocks", "opening_margin_blocked", "opening_costs_exhaust_equity")
CLASSES = ("EVALUATOR_READY", "WARMUP", "ZERO_VARIANCE", "OTHER_UNAVAILABLE")
PLAN_HASH = "8a7dfac461f2fbf594f45cdaf1be07b20bc6530895d972a24d78b359db78fbb9"
AUTHORITIES = {
    "plan": ("C:/AKR71114/plans/k46/PLAN.md", PLAN_HASH),
    "k44_final": ("C:/AKH81113/k44/owner/FINAL-WRITESET-r2.json", "d4dee3c72815142b395f0276f04544696f13bd96f8c36f29c3d96d0869f8dcad"),
    "k44_verdict": ("C:/AKH81113/reviews/k44/hard-review-final-r1/VERDICT.json", "ca2f2aaba95a38be28ac898134134a66faa014cfb77116f9b8af81784f1e78e1"),
    "k44_manifest": ("C:/AKH81113/k44/runs/matrix-owner-r1/manifest.json", "bda5a7422384edc1936d671b1280eafdeaa78abadbdbb94bbabfc559b6fe4b9a"),
    "k44_inputs": ("C:/AKH81113/k44/owner/INPUT-BINDINGS-r1.json", "bb4624154d79b9701de6e577ed8b65427a28b9b376246dbd0dd4670f35eefdbc"),
    "k44_sources": ("C:/AKH81113/plans/k44/SOURCE-BINDINGS.json", "07f5e571682ace02c1abcfcd7264a523ac1f840e4ec9c60cc8f63de4a45a9968"),
    "economics": ("C:/AKH81113/plans/k44/ECONOMIC-SPECS.json", "8bdcd7abe6c9c4ab330c173574eba6412d1372bcc6ff1d3dde292cd5900e7692"),
    "rb_sources": ("C:/AKH81113/plans/k43/SOURCE-BINDINGS.json", "da5651cb309cb7d5e6beddb20e8b14ee3ecb2745ee0a644271cc89dedf02d62f"),
    "b_manifest": ("C:/AKH71113/k40-b/runs/matrix-owner-r1/manifest.json", "636a50b9a9bc80cbf8c28afabd3d1367880380b688e6697712b619eed67bf061"),
    "k43_freeze": ("C:/AKH81113/k43/owner/freeze-r1/FREEZE.json", "09cf167ecbe307d9211760ec8f0df61ec8fe0aa6010d09ebd48db4a80ee4a2dc"),
    "k43_manifest": ("C:/AKH81113/k43/runs/aggregate-owner-r1/manifest.json", "3ff1664e39b4bceab34ce17d62cd69718000a3abe890ceaa7d05ed20d2b4c85a"),
}
SAFETY = dict(new_simulation=False, new_training=False, holdout_read=False, enable_trading=False,
              broker="NOT_RUN", observer="hold", real_order_send_calls=0, real_order_cancel_calls=0,
              credentials_read=False, K45_input_used=False)
PRODUCT_NAMES = dict(zip(PRODUCTS, ("螺纹钢", "原油", "豆粕", "PTA", "沪深300股指", "工业硅")))
METHOD_NAMES = dict(zip(METHODS, ("突破", "均线", "回归", "始终空仓", "一直持有")))


class Rejected(ValueError):
    """A declared input, provenance, or integrity precondition failed."""


def need(condition, reason):
    if not condition:
        raise Rejected(reason)


def equal(actual, expected, reason):
    need(actual == expected, reason)


def pin(raw):
    return dict(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def loads(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            need(key not in result, "duplicate_json_key")
            result[key] = value
        return result
    def constant(_value):
        raise Rejected("nonfinite_json")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def rows(raw):
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8"), newline=""))
    fields = reader.fieldnames
    need(fields and len(fields) == len(set(fields)) and all(fields), "invalid_csv_header")
    output = list(reader)
    need(all(None not in r and all(v is not None for v in r.values()) for r in output), "invalid_csv_width")
    return output


def integer(value):
    need(type(value) is int or isinstance(value, str) and re.fullmatch(r"-?(0|[1-9][0-9]*)", value) is not None,
         "invalid_integer")
    return int(value)


def amount(value):
    need(isinstance(value, (str, int)) and not isinstance(value, bool), "amount_must_be_decimal_text")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise Rejected("invalid_decimal") from exc
    need(number.is_finite(), "nonfinite_amount")
    return Fraction(number)


def half_even(value):
    value = Fraction(value)
    sign = -1 if value < 0 else 1
    quotient, remainder = divmod(abs(value.numerator), value.denominator)
    if 2 * remainder > value.denominator or (2 * remainder == value.denominator and quotient % 2):
        quotient += 1
    return sign * quotient


def finite_text(value):
    value = Fraction(value)
    denominator, twos, fives = value.denominator, 0, 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    equal(denominator, 1, "nonterminating_decimal_serialization")
    places = max(twos, fives)
    digits = str(abs(value.numerator) * 2**(places - twos) * 5**(places - fives)).zfill(places + 1)
    if places:
        digits = (digits[:-places] + "." + digits[-places:]).rstrip("0").rstrip(".")
    return ("-" if value < 0 else "") + digits


def cell(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Fraction):
        return finite_text(value)
    return str(value)


def csv_bytes(records):
    need(bool(records), "empty_output_table")
    fields = list(records[0])
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for record in records:
        equal(list(record), fields, "output_column_order")
        writer.writerow({k: cell(v) for k, v in record.items()})
    return output.getvalue().encode("utf-8")


def fixed_config():
    return dict(schema="k46-universe-v2-config/v1", products=list(PRODUCTS), methods=list(METHODS),
                capitals=list(CAPITALS), scenario="BASE", years=list(YEARS), multipliers=MULTIPLIERS,
                start=START.isoformat(), end_exclusive=END.isoformat(), normalized_scale=str(SCALE),
                rounding="ROUND_HALF_EVEN_INTEGER_QUOTIENT", coverage_threshold="1/2",
                readiness_percent_decimal_places=24, readiness_percent_rounding="ROUND_HALF_EVEN_INTEGER_QUOTIENT",
                denominator="same_instance_previous_stored_close_times_multiplier",
                readiness="raw_evaluator_candidate_direction_in_minus1_zero_plus1",
                aggregation="daily_equal_weight_then_sum_without_compounding",
                source_hashes={k: value[1] for k, value in AUTHORITIES.items()}, safety=SAFETY)


def within(path, relative):
    base = Path(path).resolve()
    relative = Path(relative)
    need(not relative.is_absolute() and ".." not in relative.parts, "unsafe_relative_path")
    target = (base / relative).resolve()
    need(target.is_relative_to(base) and target != base, "path_escapes_root")
    return target


def overlap(left, right):
    left, right = Path(left).resolve(), Path(right).resolve()
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def guard_output(output, protected):
    output = Path(output).resolve()
    need(not any(part.lower() in ("review", "reviews") for part in output.parts), "output_in_review_directory")
    for source in protected:
        need(not overlap(output, source), "output_source_overlap")


def stamp(value):
    parsed = datetime.fromisoformat(value)
    need(parsed.utcoffset() is not None, "naive_timestamp")
    return parsed.astimezone(SHANGHAI)


def guard_dates(records, timestamp_key="raw_ts"):
    """Complete date pass before interpreting monetary or candidate fields."""
    for record in records:
        observed = stamp(record[timestamp_key]).date()
        days = [observed]
        for key in ("raw_date", "trading_day"):
            if record.get(key):
                day = date.fromisoformat(record[key])
                days.append(day)
                if key == "raw_date":
                    equal(day, observed, "raw_date_timestamp_mismatch")
        need(all(START <= day < END for day in days), "observation_outside_fixed_window")


def timestamp_nano(value):
    delta = stamp(value).astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 10**9 + delta.microseconds * 1000


def identity(row):
    return (row["symbol"], row["contract_instance_id"], integer(row["datetime_nano"]),
            row["raw_ts"], row["raw_date"], integer(row["j"]))


class Sources:
    """Verify exact bytes when consumed; keep locations outside public evidence."""

    def __init__(self):
        self.inventory = {}
        self.locations = {}

    def read(self, logical, filename, expected):
        filename = Path(filename).resolve()
        need(filename.is_file(), "required_source_missing:" + logical)
        raw = filename.read_bytes()
        actual = pin(raw)
        equal(actual["sha256"], expected["sha256"], "source_hash_mismatch:" + logical)
        if "bytes" in expected:
            equal(actual["bytes"], expected["bytes"], "source_size_mismatch:" + logical)
        if logical in self.inventory:
            equal(self.inventory[logical], actual, "source_identity_changed")
            equal(self.locations[logical], str(filename), "logical_source_location_changed")
        self.inventory[logical], self.locations[logical] = actual, str(filename)
        return raw

    def record(self, logical, record):
        return self.read(logical, record["path"], record)

    def member(self, logical_root, root, manifest, relative):
        need(relative in manifest["payloads"], "source_member_not_pinned:" + relative)
        return self.read(logical_root + "/" + relative, within(root, relative), manifest["payloads"][relative])

    def verify(self):
        for logical, expected in self.inventory.items():
            equal(pin(Path(self.locations[logical]).read_bytes()), expected, "source_changed:" + logical)


def prepare_bars(product, declarations, raw_rows, mapping=None):
    selected_declarations = [r for r in declarations if r["product"] == product]
    by_symbol = {}
    for record in selected_declarations:
        symbol, instance = record["raw_symbol"], record["instance_id"]
        need(symbol not in by_symbol and instance not in by_symbol.values(), "ambiguous_contract_instance")
        need(instance.startswith(symbol + "|"), "invalid_contract_instance")
        by_symbol[symbol] = instance
    observations = [r for r in raw_rows if r["symbol"] in by_symbol]
    guard_dates(observations, "ts")
    bars, seen = defaultdict(list), set()
    for record in observations:
        symbol = record["symbol"]
        equal(integer(record["duration_seconds"]), 86400, "not_daily_bar")
        ns = integer(record["datetime_nano"])
        equal(ns, timestamp_nano(record["ts"]), "bar_nano_mismatch")
        normalized = stamp(record["ts"]).isoformat()
        day = stamp(record["ts"]).date().isoformat()
        instance = by_symbol[symbol]
        need((instance, day) not in seen, "duplicate_instance_day")
        seen.add((instance, day))
        close = amount(record["close"])
        need(close > 0, "nonpositive_close")
        bars[instance].append(dict(symbol=symbol, contract_instance_id=instance, datetime_nano=ns,
                                   raw_ts=normalized, raw_date=day, close=close,
                                   original_ts=record["ts"], original_trading_day=record["trading_day"]))
    need(bool(bars), "empty_product_bars")
    for values in bars.values():
        values.sort(key=lambda r: r["datetime_nano"])
        for j, row in enumerate(values):
            row["j"] = j
    if mapping is not None:
        expected = {(r["symbol"], instance, str(r["datetime_nano"]), r["original_ts"], r["raw_ts"], r["original_trading_day"])
                    for instance, values in bars.items() for r in values}
        fields = ("symbol", "contract_instance_id", "datetime_nano", "original_ts", "normalized_raw_ts", "original_trading_day")
        actual = [tuple(row[k] for k in fields) for row in mapping]
        equal(len(actual), len(set(actual)), "duplicate_timestamp_mapping")
        equal(set(actual), expected, "timestamp_mapping_mismatch")
    return dict(bars)


def selected_rows(raw_selection, bars, rb=False):
    days = sorted({r["raw_date"] for values in bars.values() for r in values})
    equal([r["raw_date"] for r in raw_selection], days, "selection_date_support")
    by_identity = {identity(r): r for values in bars.values() for r in values}
    selection = []
    for item in raw_selection:
        day = item["raw_date"]
        need(START <= date.fromisoformat(day) < END, "selection_outside_fixed_window")
        if rb:
            key = tuple(item["selected_key"]) if item["selected_key"] is not None else None
        else:
            names = ("selected_contract", "selected_instance", "selected_datetime_nano", "selected_raw_ts", "selected_j")
            populated = [bool(item[name]) for name in names]
            need(all(populated) or not any(populated), "partial_selected_identity")
            key = (item[names[0]], item[names[1]], integer(item[names[2]]), item[names[3]], day, integer(item[names[4]])) if all(populated) else None
        if key is None:
            equal(item["status"], "NO_ELIGIBLE_PREVIOUS_OI", "missing_selection_status")
            need(not item["selected_contract"] and not item["selected_instance"], "missing_selection_identity")
            selection.append(dict(raw_date=day, key=None, denominator=None, prior=None))
            continue
        equal(item["status"], "SELECTED", "selected_status")
        need(key in by_identity and key[4] == day, "selected_identity_not_in_bars")
        equal((item["selected_contract"], item["selected_instance"]), key[:2], "selected_contract_mismatch")
        row = by_identity[key]
        need(row["j"] > 0, "selected_without_previous_close")
        prior = bars[key[1]][row["j"] - 1]
        selection.append(dict(raw_date=day, key=key, denominator=prior["close"] * MULTIPLIERS[row["symbol"].split(".")[0] + "." + re.sub(r"[0-9]", "", row["symbol"].split(".")[1]).upper()], prior=prior))
    return selection


def readiness(decision):
    candidate, status, reason = decision["evaluator_candidate_direction"], decision["strategy_status"], decision["reason"]
    need(isinstance(status, str) and isinstance(reason, str) and status and reason, "missing_decision_status")
    if candidate not in (None, ""):
        need(integer(candidate) in (-1, 0, 1), "invalid_evaluator_candidate")
        need(status != "warmup" and reason != "zero_variance_window", "contradictory_ready_decision")
        return "EVALUATOR_READY"
    if status == "warmup":
        need(reason != "zero_variance_window", "contradictory_warmup_decision")
        return "WARMUP"
    if reason == "zero_variance_window":
        equal(status, "unavailable", "contradictory_zero_variance_decision")
        return "ZERO_VARIANCE"
    need(status != "ready", "ready_without_evaluator_candidate")
    return "OTHER_UNAVAILABLE"


def event_counts(row):
    values = {name: integer(row[name]) for name in EVENTS}
    need(all(value >= 0 for value in values.values()), "negative_event_count")
    equal(values["opening_attempts"], values["opening_fills"] + values["opening_blocks"], "opening_attempt_identity")
    equal(values["opening_blocks"], values["opening_margin_blocked"] + values["opening_costs_exhaust_equity"], "opening_block_identity")
    return values


def validate_account(product, method, instance, bars, ledger, decisions, fills):
    for records in (ledger, decisions, fills):
        guard_dates(records)
    equal((len(ledger), len(decisions)), (len(bars), len(bars)), "account_observation_support")
    grouped, ids = defaultdict(list), set()
    for fill in fills:
        need(fill["fill_id"] not in ids, "duplicate_fill_id")
        ids.add(fill["fill_id"])
        j = integer(fill["observation_index"])
        need(0 <= j < len(bars), "fill_index_outside_account")
        bar = bars[j]
        equal((integer(fill["datetime_nano"]), fill["raw_ts"]), (bar["datetime_nano"], bar["raw_ts"]), "fill_timestamp_identity")
        need(fill["offset"] in ("open", "close"), "unknown_fill_offset")
        equal(integer(fill["lots"]), 1, "not_fixed_one_lot")
        need(integer(fill["side"]) in (-1, 1), "fill_side")
        phase, signal = fill["phase"], fill["signal_index"]
        if phase == "terminal_close":
            need(j == len(bars) - 1 and fill["offset"] == "close" and signal == "", "terminal_fill_identity")
        elif method in STRATEGIES:
            need(phase == "next_open" and j > 0 and integer(signal) == j - 1, "preceding_fill_signal_identity")
        else:
            need(method == "buy_hold_control" and phase == "first_open" and j == 0 and signal == "", "control_fill_identity")
        for key in ("ideal_price", "effective_price", "fee_per_lot_component", "fee_rate_component", "fee", "slippage", "realized_pnl", "cash_after"):
            amount(fill[key])
        grouped[j].append(fill)
    output = []
    for j, (row, decision, bar) in enumerate(zip(ledger, decisions, bars)):
        equal((row["capital"], row["scenario"], row["method"], row["contract_instance_id"]), ("1000000", "BASE", method, instance), "account_metadata")
        equal(identity(row), identity(bar), "ledger_bar_identity")
        equal((integer(decision["observation_index"]), integer(decision["datetime_nano"]), decision["raw_ts"]), (j, bar["datetime_nano"], bar["raw_ts"]), "decision_bar_identity")
        equal(integer(row["raw_year"]), date.fromisoformat(bar["raw_date"]).year, "ledger_year")
        events = event_counts(row)
        equal(events["opening_fills"], sum(f["offset"] == "open" for f in grouped[j]), "ledger_opening_fill_reconciliation")
        equal(integer(row["fills"]), len(grouped[j]), "ledger_fill_reconciliation")
        equal(row["execution_status"], decision["execution_status"], "execution_status_identity")
        if method in STRATEGIES:
            preceding = decision["preceding_signal_index"]
            equal(preceding, str(j - 1) if j else "", "preceding_decision_signal_identity")
            state = readiness(decision)
        else:
            state = None
        if method == "buy_hold_control":
            equal(events["opening_attempts"], int(j == 0), "buy_hold_first_open_only")
        if method == "flat_control":
            equal(events["opening_attempts"], 0, "flat_control_opening")
        for key in ("cash", "realized_pnl", "unrealized_pnl", "previous_equity", "equity", "gross", "fee", "slippage", "net", "gross_delta", "fee_delta", "slippage_delta", "net_delta", "terminal_close_fee_delta", "terminal_close_slippage_delta"):
            amount(row[key])
        for key in ("opening_required_margin", "opening_equity_after_costs"):
            if decision.get(key):
                amount(decision[key])
        output.append(dict(key=identity(bar), net=amount(row["net_delta"]), events=events, readiness=state,
                           status=decision.get("strategy_status"), reason=decision.get("reason")))
    return output


def curve(values):
    cumulative, peak, maximum, duration, longest, covered = 0, 0, 0, 0, 0, 0
    daily = []
    for value in values:
        if value is not None:
            cumulative += value
            covered += 1
        peak = max(peak, cumulative)
        drawdown = peak - cumulative
        duration = duration + 1 if drawdown > 0 else 0
        maximum, longest = max(maximum, drawdown), max(longest, duration)
        daily.append(dict(cumulative=cumulative, drawdown=drawdown, duration=duration))
    return dict(total=cumulative if covered else None, maximum=maximum if covered else None,
                longest=longest if covered else None, open_at_end=bool(duration) if covered else None,
                covered=covered, daily=daily)


def eligible(ready, denominator, strategy=True):
    if denominator == 0:
        return "NO_COVERAGE"
    return "EXCLUDED_COVERAGE_LT_50" if strategy and 2 * ready < denominator else "INCLUDED"


def percent_units(units):
    return Fraction(units, 10**22) if units is not None else None


def coverage_percent(ready, count):
    return Fraction(half_even(Fraction(100 * SCALE * ready, count)), SCALE) if count else None


def year_status(year, covered, product=None):
    if not covered:
        return "NO_COVERAGE"
    return "PARTIAL_YEAR" if year in (2020, 2026) or product == "GFEX.SI" and year == 2022 else "FULL_OBSERVED_YEAR"


def product_tables(product, selection, accounts):
    """Aggregate persisted rows; accounts already passed identity/date validation."""
    selected = {r["key"]: r for r in selection if r["key"] is not None}
    selected_instances = {key[1] for key in selected}
    summaries, annuals, readiness_rows, daily, joins = [], [], [], {}, []
    for method in METHODS:
        selected_values, classes, reasons = {}, Counter(), defaultdict(Counter)
        all_events, chosen_events = Counter(), Counter()
        never_all = never_selected = never_selected_day = 0
        method_accounts = accounts[method]
        for instance, records in sorted(method_accounts.items()):
            opens_all = opens_selected = 0
            for record in records:
                all_events.update(record["events"])
                opens_all += record["events"]["opening_fills"]
                if record["key"] not in selected:
                    continue
                key = record["key"]
                need(key not in selected_values, "duplicate_selected_account_row")
                chosen = selected[key]
                need(chosen["denominator"] > 0, "invalid_nominal_denominator")
                units = half_even(SCALE * record["net"] / chosen["denominator"])
                selected_values[key] = dict(net=record["net"], units=units)
                chosen_events.update(record["events"])
                opens_selected += record["events"]["opening_fills"]
                if method in STRATEGIES:
                    classes[record["readiness"]] += 1
                    reasons[record["readiness"]][record["status"] + "/" + record["reason"]] += 1
                prior = chosen["prior"]
                joins.append(dict(product=product, method=method, raw_date=key[4], symbol=key[0], contract_instance_id=instance,
                                  j=key[5], datetime_nano=key[2], raw_ts=key[3], prior_j=prior["j"], prior_raw_ts=prior["raw_ts"],
                                  prior_raw_date=prior["raw_date"], prior_gap_days=(date.fromisoformat(key[4])-date.fromisoformat(prior["raw_date"])).days,
                                  prior_close=prior["close"], multiplier=MULTIPLIERS[product], denominator=chosen["denominator"],
                                  net_yuan=record["net"], normalized_units=units, readiness=record["readiness"],
                                  **record["events"]))
            never_all += int(opens_all == 0)
            if instance in selected_instances:
                never_selected += int(opens_all == 0)
                never_selected_day += int(opens_selected == 0)
        equal(set(selected_values), set(selected), "complete_selected_account_support")
        count, ready = len(selected), classes["EVALUATOR_READY"]
        if method in STRATEGIES:
            equal(sum(classes.values()), count, "readiness_partition")
        exclusion = eligible(ready, count, method in STRATEGIES)
        days = [r["raw_date"] for r in selection]
        daily[method] = {r["raw_date"]: selected_values[r["key"]] if r["key"] is not None else None for r in selection}
        normalized = curve([daily[method][day]["units"] if daily[method][day] is not None else None for day in days])
        money = curve([daily[method][day]["net"] if daily[method][day] is not None else None for day in days])
        years = []
        for year in YEARS:
            values = [daily[method][day] for day in days if day.startswith(str(year) + "-")]
            normalized_year = curve([v["units"] if v is not None else None for v in values])
            net_year = sum((v["net"] for v in values if v is not None), Fraction()) if normalized_year["covered"] else None
            years.append(dict(product=product, method=method, year=year, year_status=year_status(year, normalized_year["covered"], product),
                              covered_days=normalized_year["covered"], net_yuan=net_year, normalized_units=normalized_year["total"],
                              normalized_percent=percent_units(normalized_year["total"]), max_drawdown_pp=percent_units(normalized_year["maximum"]),
                              longest_drawdown_observations=normalized_year["longest"]))
        if count:
            equal(sum((r["net_yuan"] for r in years if r["net_yuan"] is not None), Fraction()), money["total"], "annual_money_conservation")
            equal(sum(r["normalized_units"] for r in years if r["normalized_units"] is not None), normalized["total"], "annual_units_conservation")
        covered_years = [r for r in years if r["covered_days"]]
        worst = min(covered_years, key=lambda r: (r["net_yuan"], r["year"])) if covered_years else None
        summaries.append(dict(product=product, method=method, capital="1000000", scenario="BASE", status="COVERED" if count else "NO_COVERAGE",
                              selected_days=count, net_yuan=money["total"], normalized_units=normalized["total"], normalized_percent=percent_units(normalized["total"]),
                              positive_years=sum(r["net_yuan"] > 0 for r in covered_years), covered_years=len(covered_years),
                              worst_year=worst["year"] if worst else None, worst_year_net_yuan=worst["net_yuan"] if worst else None,
                              max_drawdown_pp=percent_units(normalized["maximum"]), longest_drawdown_observations=normalized["longest"],
                              open_drawdown_at_end=normalized["open_at_end"], ready_days=ready if method in STRATEGIES else None,
                              readiness_denominator=count if method in STRATEGIES else None,
                              readiness_percent=coverage_percent(ready, count) if method in STRATEGIES else None,
                              eligibility=exclusion, selected_opening_fills=chosen_events["opening_fills"],
                              max_drawdown_yuan=money["maximum"]))
        readiness_rows.append(dict(product=product, method=method, **{label.lower()+"_days": classes[label] if method in STRATEGIES else None for label in CLASSES},
                                   reason_counts_json=json.dumps({k: dict(sorted(v.items())) for k,v in sorted(reasons.items())}, ensure_ascii=False, sort_keys=True, separators=(",", ":")) if method in STRATEGIES else None,
                                   ready_days=ready if method in STRATEGIES else None, readiness_denominator=count if method in STRATEGIES else None,
                                   readiness_percent=coverage_percent(ready, count) if method in STRATEGIES else None,
                                   readiness_status="APPLICABLE" if method in STRATEGIES else "NOT_APPLICABLE", eligibility=exclusion,
                                   **{"selected_"+k: chosen_events[k] for k in EVENTS}, all_contracts=len(method_accounts),
                                   selected_contracts=len(selected_instances), all_contracts_never_opened=never_all,
                                   selected_contracts_never_opened=never_selected, selected_contracts_without_selected_day_open=never_selected_day,
                                   **{"all_"+k: all_events[k] for k in EVENTS}))
        annuals.extend(years)
    return summaries, annuals, readiness_rows, daily, joins


def combined_tables(summaries, product_daily):
    eligible_by_method = {method: [p for p in PRODUCTS if next(r for r in summaries if (r["product"], r["method"]) == (p, method))["eligibility"] == "INCLUDED"] for method in METHODS}
    days = sorted({day for methods in product_daily.values() for daily in methods.values() for day in daily})
    daily_rows, total_rows, annual_rows = [], [], []
    for method in METHODS:
        records = []
        for day in days:
            participants = [p for p in eligible_by_method[method] if product_daily[p][method].get(day) is not None]
            n = len(participants)
            total = sum(product_daily[p][method][day]["units"] for p in participants) if n else None
            units = half_even(Fraction(total, n)) if n else None
            records.append(dict(method=method, raw_date=day, status="COVERED" if n else "NO_PARTICIPANTS",
                                participant_count=n, participants="|".join(participants), sum_product_units=total,
                                normalized_units=units, normalized_percent=percent_units(units)))
        overall = curve([r["normalized_units"] for r in records])
        for row, state in zip(records, overall["daily"]):
            row.update(cumulative_units=state["cumulative"] if overall["covered"] else None,
                       cumulative_percent=percent_units(state["cumulative"]) if overall["covered"] else None,
                       drawdown_pp=percent_units(state["drawdown"]) if overall["covered"] else None,
                       drawdown_observations=state["duration"] if overall["covered"] else None)
        nvalues = [r["participant_count"] for r in records]
        total_rows.append(dict(method=method, status="COVERED" if overall["covered"] else "NO_COVERAGE",
                               contributing_days=overall["covered"], total_observation_days=len(days),
                               normalized_units=overall["total"], normalized_percent=percent_units(overall["total"]),
                               max_drawdown_pp=percent_units(overall["maximum"]), longest_drawdown_observations=overall["longest"],
                               open_drawdown_at_end=overall["open_at_end"], min_participants=min(nvalues) if nvalues else None,
                               max_participants=max(nvalues) if nvalues else None, eligible_products="|".join(eligible_by_method[method]),
                               excluded_products="|".join(p for p in PRODUCTS if p not in eligible_by_method[method])))
        by_year = []
        for year in YEARS:
            subset = [r for r in records if r["raw_date"].startswith(str(year) + "-")]
            annual = curve([r["normalized_units"] for r in subset])
            participants = [r["participant_count"] for r in subset]
            by_year.append(dict(method=method, year=year, year_status=year_status(year, annual["covered"]),
                                contributing_days=annual["covered"], normalized_units=annual["total"], normalized_percent=percent_units(annual["total"]),
                                max_drawdown_pp=percent_units(annual["maximum"]), longest_drawdown_observations=annual["longest"],
                                min_participants=min(participants) if participants else None, max_participants=max(participants) if participants else None))
        if overall["covered"]:
            equal(sum(r["normalized_units"] for r in by_year if r["normalized_units"] is not None), overall["total"], "combined_annual_conservation")
        daily_rows.extend(records)
        annual_rows.extend(by_year)
    return daily_rows, total_rows, annual_rows


def capital_events(product, source_rows, readiness_rows):
    selected = {}
    for row in source_rows:
        if row["scenario"] != "BASE" or row["view"] != "ALL":
            continue
        key = row["capital"], row["method"]
        need(key not in selected, "duplicate_capital_cell")
        if "product" in row:
            equal(row["product"], product, "capital_product")
        selected[key] = row
    equal(set(selected), {(c, m) for c in CAPITALS for m in METHODS}, "capital_cell_support")
    result = []
    for capital in CAPITALS:
        for method in METHODS:
            source = selected[(capital, method)]
            counts = event_counts(source)
            status = source["status"]
            equal(status, "OBSERVED_DESCRIPTIVE_ONLY", "capital_source_status")
            accounts = integer(source["n"])
            actual = next(r for r in readiness_rows if r["method"] == method)
            equal(accounts, actual["all_contracts"], "capital_account_count")
            if capital == "1000000":
                equal(counts, {key: actual["all_"+key] for key in EVENTS}, "capital_base_ledger_events")
                equal(integer(source["never_opened_accounts"]), actual["all_contracts_never_opened"], "capital_never_opened_accounts")
            result.append(dict(product=product, capital=capital, method=method, scenario="BASE", view="ALL", accounts=accounts, **counts))
    return result


def compare_old_tables(summaries, annuals, old_summary, old_annual):
    summary = {(r["method"]): r for r in old_summary if r.get("capital", "1000000") == "1000000" and r.get("scenario", "BASE") == "BASE"}
    years = {(r["method"], integer(r["year"])): r for r in old_annual if r.get("capital", "1000000") == "1000000" and r.get("scenario", "BASE") == "BASE"}
    equal(set(summary), set(METHODS), "old_summary_method_support")
    equal(set(years), {(m, y) for m in METHODS for y in YEARS}, "old_annual_support")
    for row in summaries:
        old = summary[row["method"]]
        equal(row["net_yuan"], amount(old["net"]) if old["net"] else None, "old_dominant_net")
        equal(row["selected_days"], integer(old["covered_observations"]), "old_selected_days")
        equal(row["max_drawdown_yuan"], amount(old["max_drawdown"]) if old["max_drawdown"] else None, "old_money_drawdown")
        equal((row["positive_years"], row["covered_years"]), (integer(old["positive_years"]), integer(old["covered_years"])), "old_positive_years")
    for row in annuals:
        old = years[(row["method"], row["year"])]
        equal(row["net_yuan"], amount(old["net"]) if old["net"] else None, "old_annual_net")
        equal(row["covered_days"], integer(old["covered_observations"]), "old_annual_coverage")


@dataclass
class Context:
    repo: Path
    output: Path
    build: dict
    build_pin: dict
    sources: Sources
    authorities: dict
    protected: list


def preflight(repo, output, binding_path, binding_hash, sealing=False):
    repo, output, binding_path = Path(repo).resolve(), Path(output).resolve(), Path(binding_path).resolve()
    need(repo.is_dir() and binding_path.is_file(), "required_build_inputs_missing")
    raw = binding_path.read_bytes()
    equal(pin(raw)["sha256"], binding_hash, "build_binding_hash")
    build = loads(raw)
    equal(build["schema"], "k46-build-bindings/v1", "build_binding_schema")
    equal(build["plan_sha256"], PLAN_HASH, "build_plan_hash")
    equal(Path(build["source_repo"]).resolve(), repo, "build_repository")
    equal(set(build["files"]), set(BUILD_FILES), "build_file_set")
    equal(within(repo, TOOL), Path(__file__).resolve(), "running_tool_repository")
    for relative in BUILD_FILES:
        equal(pin(within(repo, relative).read_bytes()), build["files"][relative], "build_file_hash:" + relative)
    config = loads(within(repo, CONFIG).read_bytes())
    equal(config, fixed_config(), "fixed_configuration")
    protected = [repo, binding_path.parent]
    protected.extend(Path(p).parent for p, _ in AUTHORITIES.values())
    guard_output(output, protected)
    need(output.is_dir() if sealing else not output.exists(), "aggregate_directory_state")
    sources, authorities = Sources(), {}
    for key, (path, sha) in AUTHORITIES.items():
        raw = sources.read("authority/" + key, path, {"sha256": sha})
        authorities[key] = loads(raw) if key != "plan" else raw
    for key in ("K43_verdict", "K40B_verdict"):
        record = authorities["k44_sources"]["sources"][key]
        verdict = loads(sources.record("authority/" + key, record))
        need(verdict["verdict"] == "PASS" and verdict["unconditional"] is True and verdict["conditional"] is False, "prior_review_not_unconditional")
    verdict = authorities["k44_verdict"]
    need(verdict["verdict"] == "PASS" and verdict["unconditional"] is True and verdict["conditional"] is False, "k44_review_not_unconditional")
    equal((authorities["k44_final"]["final_tool_build"], authorities["k44_final"]["research_generation_build"]), ("r2", "r1"), "old_generation_identity")
    for relative, record in authorities["k44_final"]["files"].items():
        if relative != REPORT:
            sources.read("preserved/" + relative, within(repo, relative), record)
    rb_report = authorities["k44_sources"]["sources"]["docs/research/v1-daily-baseline-rb.md"]
    sources.read("preserved/docs/research/v1-daily-baseline-rb.md", within(repo, "docs/research/v1-daily-baseline-rb.md"), rb_report)
    for product in PRODUCTS[1:]:
        entry = authorities["k44_inputs"]["products"][product]
        equal(entry["status"], "READY", "old_product_not_ready")
        equal(integer(authorities["economics"]["products"][product]["multiplier"]), MULTIPLIERS[product], "economic_multiplier")
        bundle = entry["bundle"]
        root = Path(bundle["root"]).resolve()
        for name, record in {"manifest.json": bundle["manifest"], **bundle["payloads"]}.items():
            equal(Path(record["path"]).resolve(), within(root, name), "input_bundle_member_location")
        protected.append(root)
    protected.append(Path(authorities["rb_sources"]["input_manifest"]["path"]).parent)
    guard_output(output, protected)
    rb_config_record = authorities["k44_sources"]["sources"]["config/research/v1_daily_baseline_rb_b.json"]
    rb_config = loads(sources.record("authority/rb_config", rb_config_record))
    equal(integer(rb_config["base_assumptions"]["multiplier"]), MULTIPLIERS["SHFE.RB"], "rb_multiplier")
    return Context(repo, output, build, pin(binding_path.read_bytes()), sources, authorities, protected)


def load_product(context, product):
    store, authority = context.sources, context.authorities
    if product == "SHFE.RB":
        binding = authority["rb_sources"]
        store.record("input/SHFE.RB/manifest.json", binding["input_manifest"])
        payload = {name: store.record("input/SHFE.RB/" + name, item) for name, item in binding["input_payloads"].items()}
        root = Path(AUTHORITIES["b_manifest"][0]).parent
        manifest = authority["b_manifest"]
        k43_root = Path(AUTHORITIES["k43_manifest"][0]).parent
        selection_raw = loads(store.member("k43", k43_root, authority["k43_manifest"], "selection.json"))
        bars = prepare_bars(product, loads(payload["contracts.json"]), rows(payload["bars.csv"]))
        selection = selected_rows(selection_raw, bars, rb=True)
        account_paths = {(instance, method): "accounts/C1000000/BASE/" + values[0]["symbol"] + "/" + method
                         for instance, values in bars.items() for method in METHODS}
        logical = "b"
    else:
        bundle = authority["k44_inputs"]["products"][product]["bundle"]
        store.record("input/"+product+"/manifest.json", bundle["manifest"])
        payload = {name: store.record("input/"+product+"/"+name, item) for name, item in bundle["payloads"].items()}
        root = Path(AUTHORITIES["k44_manifest"][0]).parent
        manifest = authority["k44_manifest"]
        prefix = "products/"+product+"/"
        mapping = rows(store.member("k44", root, manifest, prefix+"timestamp-map.csv"))
        bars = prepare_bars(product, loads(payload["contracts.json"]), rows(payload["bars.csv"]), mapping)
        selection = selected_rows(rows(store.member("k44", root, manifest, prefix+"selection.csv")), bars)
        index = rows(store.member("k44", root, manifest, prefix+"account-index.csv"))
        account_paths, observed = {}, {}
        for row in index:
            if row["capital"] != "1000000" or row["scenario"] != "BASE":
                continue
            equal(row["product"], product, "index_product")
            instance, method = row["contract_instance_id"], row["method"]
            need(instance in bars and method in METHODS, "account_index_identity")
            equal(row["symbol"], bars[instance][0]["symbol"], "index_symbol")
            key = instance, method
            need(key not in account_paths, "duplicate_account_index")
            relative = row["account_path"]
            need(relative.startswith(prefix+"accounts/C1000000/BASE/") and relative.endswith("/"+method), "account_path_scope")
            account_paths[key], observed[key] = relative, integer(row["observations"])
            equal(observed[key], len(bars[instance]), "index_observation_count")
        equal(set(account_paths), {(instance, m) for instance in bars for m in METHODS}, "account_index_coverage")
        logical = "k44"
    equal((len(bars), sum(map(len, bars.values()))), EXPECTED_SUPPORT[product], "accepted_product_support")
    accounts = {method: {} for method in METHODS}
    for instance in sorted(bars):
        for method in METHODS:
            account_path = account_paths[(instance, method)]
            ledger, decisions, fills = (rows(store.member(logical, root, manifest, account_path+"/"+name))
                                       for name in ("ledger.csv", "decisions.csv", "fills.csv"))
            accounts[method][instance] = validate_account(product, method, instance, bars[instance], ledger, decisions, fills)
    return selection, accounts


def prior_table(context, product, filename):
    relative = "docs/research/v1-daily-baseline-universe/"+product+"/"+filename
    record = context.authorities["k44_final"]["files"][relative]
    return rows(context.sources.read("accepted-public/"+relative, record["source"], record))


def build_tables(context):
    summaries, annuals, ready, funding, product_daily, audit = [], [], [], [], {}, []
    for product in PRODUCTS:
        selection, accounts = load_product(context, product)
        summary, annual, readiness_rows, daily, joins = product_tables(product, selection, accounts)
        compare_old_tables(summary, annual, prior_table(context, product, "dominant-summary.csv"), prior_table(context, product, "dominant-year.csv"))
        if product == "SHFE.RB":
            old = loads(context.sources.member("k43", Path(AUTHORITIES["k43_manifest"][0]).parent, context.authorities["k43_manifest"], "daily-curves.json"))
            expected = {(method, day): value["net"] if value is not None else None for method, records in daily.items() for day, value in records.items()}
            actual = {(r["method"], r["raw_date"]): amount(r["selected_net"]) if r["selected_net"] is not None else None for r in old}
            equal(len(actual), len(old), "rb_curve_duplicate")
            equal(actual, expected, "rb_original_daily_net")
        summaries.extend(summary)
        annuals.extend(annual)
        ready.extend(readiness_rows)
        funding.extend(capital_events(product, prior_table(context, product, "sensitivity-summary.csv"), readiness_rows))
        product_daily[product] = daily
        audit.extend(joins)
        print(json.dumps({"product": product, "accepted_accounts_read": sum(len(v) for v in accounts.values()), "status": "AGGREGATED_NOT_SEALED"}), flush=True)
    combined_daily, combined_summary, combined_annual = combined_tables(summaries, product_daily)
    tables = dict(zip(TABLES[:-1], (summaries, annuals, ready, combined_daily, combined_summary, combined_annual, funding)))
    tables["facts.csv"] = facts(summaries, combined_summary, product_daily)
    return tables, audit


def facts(summaries, combined, daily):
    rules = [r for r in summaries if r["method"] in STRATEGIES and r["eligibility"] == "INCLUDED"]
    net = {(r["product"],r["method"]):r["net_yuan"] for r in summaries}
    days = sorted({day for methods in daily.values() for values in methods.values() for day in values})
    items = [("product_count",len(PRODUCTS),"个","fixed products"), ("rule_count",len(STRATEGIES),"个","fixed methods"),
             ("lots_per_account",1,"手","accepted fixed one-lot accounts"), ("first_observation",days[0],"日期","original selection dates"),
             ("last_observation",days[-1],"日期","original selection dates"), ("included_rule_cells",len(rules),"个","summary.csv eligibility=INCLUDED; strategies only"),
             ("positive_rule_cells",sum(r["net_yuan"]>0 for r in rules),"个","included strategy net_yuan > 0"),
             ("negative_rule_cells",sum(r["net_yuan"]<0 for r in rules),"个","included strategy net_yuan < 0"),
             ("zero_rule_cells",sum(r["net_yuan"]==0 for r in rules),"个","included strategy net_yuan = 0"),
             ("better_than_hold_cells",sum(r["net_yuan"]>net[(r["product"],"buy_hold_control")] for r in rules),"个","strict same-product net_yuan comparison")]
    for row in combined:
        items.append(("combined_percent/"+row["method"],row["normalized_percent"],"%","combined-summary.csv "+row["method"]))
    for row in summaries:
        if row["method"] in STRATEGIES and row["eligibility"] != "INCLUDED":
            items.append(("excluded_coverage/"+row["product"]+"/"+row["method"],row["readiness_percent"],"%","summary.csv "+row["product"]+" "+row["method"]+" "+row["eligibility"]))
    return [dict(fact_id=k,value=v,unit=u,basis=b) for k,v,u,b in items]


def display(value, kind="text"):
    if value is None or value == "":
        return "—"
    if kind == "money":
        return format(half_even(Fraction(value)), ",d")
    if kind == "percent":
        units = half_even(Fraction(value)*100)
        return ("-" if units < 0 else "") + str(abs(units)//100) + "." + str(abs(units)%100).zfill(2)
    if kind == "boolean":
        return "是" if value else "否"
    return str(value)


def render_report(tables):
    fact = {r["fact_id"]:r["value"] for r in tables["facts.csv"]}
    refs = []
    def fact_value(key, kind="text"):
        text = display(fact[key],kind)
        refs.append(dict(kind="fact",fact_id=key,display=text))
        return text
    lines = ["# 六品种日线基准：第二版", "",
             f"{fact_value('product_count')}个品种、{fact_value('rule_count')}个规则，{fact_value('first_observation')} 至 {fact_value('last_observation')}，每个合约固定 {fact_value('lots_per_account')} 手，规则和参数没有调整。",
             f"按当天主力合约算，排除能计算的天数不足一半的组合后，{fact_value('included_rule_cells')} 个“品种×规则”中 {fact_value('positive_rule_cells')} 个赚、{fact_value('negative_rule_cells')} 个亏、{fact_value('zero_rule_cells')} 个持平，好过一直持有同品种主力合约的有 {fact_value('better_than_hold_cells')} 个。",
             "把各品种折成同样的名义金额、每天等权平均后相加：" + "，".join(METHOD_NAMES[m]+" "+fact_value("combined_percent/"+m,"percent")+"%" for m in (*STRATEGIES,"buy_hold_control")) + "；这不是年化收益率。"]
    excluded = [r for r in tables["summary.csv"] if r["method"] in STRATEGIES and r["eligibility"] != "INCLUDED"]
    if excluded:
        lines.append("这些组合能计算的主力日不足一半："+"；".join(PRODUCT_NAMES[r["product"]]+"的"+METHOD_NAMES[r["method"]]+" "+fact_value("excluded_coverage/"+r["product"]+"/"+r["method"],"percent")+"%" for r in excluded)+"，未计入上面两句。")
    else:
        lines.append("各品种的三个规则都达到至少一半主力日可计算的门槛，合并时仍按每天实际有主力合约的品种数平均。")
    lines.append("数字没有扣换月成本，指标只用单个合约自己的历史，也没有做统计检验；它们只作为今后研究的基准线。")
    lines.append("")

    def table(filename, records, columns):
        need(len(records) <= 50, "report_table_exceeds_50_rows")
        lines.append("| "+" | ".join(label for label,_,_ in columns)+" |")
        lines.append("| "+" | ".join("---" for _ in columns)+" |")
        for index,row in records:
            cells = []
            for label,key,kind in columns:
                value = row[key]
                if kind == "product":
                    rendered = PRODUCT_NAMES[value]
                elif kind == "method":
                    rendered = METHOD_NAMES[value]
                elif kind == "eligibility":
                    rendered = {"INCLUDED":"计入","EXCLUDED_COVERAGE_LT_50":"低覆盖，排除","NO_COVERAGE":"无覆盖"}[value]
                elif kind == "year_status":
                    rendered = {"PARTIAL_YEAR":"部分年度","FULL_OBSERVED_YEAR":"已有全年观察","NO_COVERAGE":"无覆盖"}[value]
                elif kind == "events":
                    rendered = "/".join(str(v) for v in value)
                else:
                    rendered = display(value,kind)
                need("|" not in rendered and "\n" not in rendered, "unsafe_report_cell")
                refs.append(dict(kind="table",file=filename,row=index,column=key,display=rendered))
                cells.append(rendered)
            lines.append("| "+" | ".join(cells)+" |")
        lines.append("")
    table("summary.csv",list(enumerate(tables["summary.csv"])),[("品种","product","product"),("做法","method","method"),("主力净额（元）","net_yuan","money"),("名义折算（%）","normalized_percent","percent"),("正年数","positive_years","text"),("最差年","worst_year","text"),("该年净额（元）","worst_year_net_yuan","money"),("最大回撤（百分点）","max_drawdown_pp","percent"),("可计算日（%）","readiness_percent","percent"),("选中日开仓","selected_opening_fills","text"),("合并资格","eligibility","eligibility")])
    lines.extend(["金额按元取整；折算百分比是逐日净额除以前一根收盘价乘合约乘数，再按天相加，不复利。最大回撤用百分点表示。", ""])
    lines.extend(["## 每天等权后的结果", "", "各规则按全期覆盖门槛排除对应品种；两项对照不按规则覆盖排除，因此它们的参与品种集合可能不同。工业硅上市前不参加平均，上市后仍须当天有原选约赢家。每日实际品种数见 [combined-daily.csv](v1-daily-baseline-universe/v2/combined-daily.csv)。", ""])
    table("combined-summary.csv",list(enumerate(tables["combined-summary.csv"])),[("做法","method","method"),("全期（%）","normalized_percent","percent"),("最大回撤（百分点）","max_drawdown_pp","percent"),("最长回撤观察日数","longest_drawdown_observations","text"),("末尾尚未收复","open_drawdown_at_end","boolean"),("最少参与品种","min_participants","text"),("最多参与品种","max_participants","text")])
    table("combined-annual.csv",list(enumerate(tables["combined-annual.csv"])),[("做法","method","method"),("年份","year","text"),("范围","year_status","year_status"),("年内（%）","normalized_percent","percent"),("年内回撤（百分点）","max_drawdown_pp","percent"),("最长回撤观察日数","longest_drawdown_observations","text")])
    lines.extend(["## 规则有多少天能计算", "", "这里的“能计算”包含算出不持仓方向的日子。规则状态写为观察模式不等于尚在预热；口径取原评价器记录的候选方向。当天收盘的候选与当天开盘执行是两个时点，开仓次数只取原账本当日的开仓事件。一直持有在合约首根开仓，首根未入选时，选中日开仓次数可以为零。", ""])
    table("readiness.csv",[(i,r) for i,r in enumerate(tables["readiness.csv"]) if r["method"] in STRATEGIES],[("品种","product","product"),("规则","method","method"),("可计算日","ready_days","text"),("主力日","readiness_denominator","text"),("占比（%）","readiness_percent","percent"),("选中日开仓","selected_opening_fills","text"),("全部合同实例","all_contracts","text"),("全账户历史从未开仓","all_contracts_never_opened","text")])
    lines.extend(["## 四档资金下的开仓事件", "", "每格依次是尝试／成交／被保证金拦下的次数，范围为所有独立合约账户，采用原基准成本；它不是主力日统计，也不是共享资金池。两项对照和其他阻断原因详见 [capital-events.csv](v1-daily-baseline-universe/v2/capital-events.csv)。", ""])
    capital_records = []
    for product in PRODUCTS:
        for capital in CAPITALS:
            record = dict(product=product,capital=capital)
            for method in STRATEGIES:
                i,source = next((i,r) for i,r in enumerate(tables["capital-events.csv"]) if (r["product"],r["capital"],r["method"]) == (product,capital,method))
                record[method] = tuple(source[k] for k in ("opening_attempts","opening_fills","opening_margin_blocked"))
                refs.append(dict(kind="capital_events",product=product,capital=capital,method=method,row=i,columns=["opening_attempts","opening_fills","opening_margin_blocked"]))
            capital_records.append((len(capital_records),record))
    table("capital-events.csv:grouped",capital_records,[("品种","product","product"),("初始资金（元）","capital","money"),("突破","donchian_trend","events"),("均线","ma_trend","events"),("回归","zscore_reversion","events")])
    costs = [r for r in tables["capital-events.csv"] if r["opening_costs_exhaust_equity"]]
    if costs:
        lines.append("另有开仓费用耗尽权益的阻断："+"；".join(PRODUCT_NAMES[r["product"]]+"、"+display(r["capital"],"money")+"元、"+METHOD_NAMES[r["method"]]+" "+str(r["opening_costs_exhaust_equity"])+"次" for r in costs)+"；这些次数没有列入保证金阻断。")
        refs.extend(dict(kind="costs_block",file="capital-events.csv",row=tables["capital-events.csv"].index(r),column="opening_costs_exhaust_equity",display=str(r["opening_costs_exhaust_equity"])) for r in costs)
    lines.extend(["", "## 计算口径与限制", "",
                  "每个选中日的分母使用同一合同实例紧邻的上一根已存日线收盘价乘固定乘数，允许跨周末或历史缺口；缺少合法前收盘则拒绝运行。原来没有选约赢家的日子留空，不用零补齐，也不计入当日平均。",
                  "比例先以小数点后24位半偶舍入为整数单位；每日合并再半偶舍入一次，然后按日精确相加。分年之和严格等于全期。回撤从零起算，年内回撤年初归零；持续时间数观察日，未选约日若仍低于前高则继续计数。",
                  "低覆盖筛选按全期可计算日比例固定：低于50%排除，正好50%保留；保留的组合不会再逐日删除预热期。规则可计算只表示收盘时可得候选，不代表可实盘执行。",
                  "这条等权线拼接原账户的当日盈亏，不是一笔资金连续运行的结果。换约可能继承原账户持仓，不能当成新开仓；没有加入换月成本。前根持仓量的历史可知时点与静态合约规格的历史逐日适用性未经验证，结果仍为 SCENARIO / NOT_ASSESSED。",
                  "旧 CSV、旧 CHECK 和螺纹钢独立报告保留原生成历史；新结果由 [v2 CHECK.json](v1-daily-baseline-universe/v2/CHECK.json) 约束，并绑定同次提交的新工具、配置和测试。旧资金成本敏感性表继续保留在各品种旧 CSV 中。", "",
                  "全部数值表："+"、".join("["+name+"](v1-daily-baseline-universe/v2/"+name+")" for name in TABLES)+"。", ""])
    return "\n".join(lines).encode("utf-8"), refs


def write_new(path, raw):
    path = Path(path)
    need(not path.exists(), "output_file_already_exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".json":
        loads(raw)
    temporary = path.with_name(path.name+".tmp")
    with temporary.open("xb") as stream:
        stream.write(raw)
    temporary.replace(path)


def aggregate(context):
    need(not context.output.exists(), "output_already_exists")
    guard_output(context.output, context.protected)
    context.output.mkdir(parents=True, exist_ok=False)
    tables, audit = build_tables(context)
    report, refs = render_report(tables)
    context.sources.verify()
    # Stable logical manifests exclude location, PID, run identifier and clock.
    input_manifest = json_bytes(context.sources.inventory)
    audit_raw = csv_bytes(audit)
    ref_raw = json_bytes(refs)
    semantics = {k:v for k,v in fixed_config().items() if k != "source_hashes"}
    public = {PUBLIC+name: csv_bytes(tables[name]) for name in TABLES}
    public[REPORT] = report
    check = dict(schema="k46-universe-v2-check/v1", status="SEALED_AGGREGATE_EXIT_0", semantics=semantics,
                 safety=SAFETY, evidence_level="SCENARIO", statistical_qualification="NOT_ASSESSED",
                 historical_known_at=None, timing_verified=False, historical_schedule_verified=False,
                 prior_research_generation_build="r1", prior_final_tool_build="r2",
                 build_files=context.build["files"], published_files={k:pin(v) for k,v in sorted(public.items())},
                 tables={name:dict(rows=len(tables[name]),columns=list(tables[name][0])) for name in TABLES},
                 external_manifests=dict(inputs=dict(sha256=pin(input_manifest)["sha256"],file_count=len(context.sources.inventory)),
                                         daily_join=dict(sha256=pin(audit_raw)["sha256"],file_count=1),
                                         report_references=dict(sha256=pin(ref_raw)["sha256"],file_count=1)),
                 reconciliation=dict(annual_net_exact=True,annual_normalized_units_exact=True,combined_annual_units_exact=True,
                                     original_dominant_net_exact=True,original_capital_events_exact=True,readiness_partition_exact=True,
                                     selected_identity_exact=True,opening_fills_exact=True,old_public_files_unchanged=True))
    payloads = {"pending/"+k:v for k,v in public.items()}
    payloads.update({"inputs.json":input_manifest,"daily-join.csv":audit_raw,"report-references.json":ref_raw,"check-draft.json":json_bytes(check)})
    for relative, raw in payloads.items():
        write_new(within(context.output,relative),raw)
    manifest = dict(schema="k46-aggregate-manifest/v1",status="AGGREGATED_AWAITING_TERMINAL_RECEIPT",
                    build_binding_sha256=context.build_pin["sha256"],build_files=context.build["files"],
                    payloads={k:pin(v) for k,v in sorted(payloads.items())},public_files=sorted([*public,PUBLIC+"CHECK.json"]))
    write_new(context.output/"manifest.json",json_bytes(manifest))
    write_new(context.output/"locations.json",json_bytes(context.sources.locations))
    print(json.dumps(dict(status=manifest["status"],payload_files=len(payloads),consumed_source_files=len(context.sources.inventory))),flush=True)


def verify_receipt(receipt, context, manifest_pin):
    output, binding_pin = context.output, context.build_pin
    equal(receipt["schema"],"k46-aggregate-terminal/v1","receipt_schema")
    need(receipt["terminated"] is True and receipt["streams_closed"] is True and receipt["timed_out"] is False,"aggregate_not_terminal")
    need(type(receipt["raw_exit"]) is int and receipt["raw_exit"] == 0,"aggregate_nonzero_exit")
    need(integer(receipt["child_pid"]) > 0,"missing_child_identity")
    equal(Path(receipt["output_dir"]).resolve(),Path(output).resolve(),"receipt_output_identity")
    equal(receipt["manifest"],manifest_pin,"receipt_manifest_identity")
    equal(receipt["locations"],pin((output/"locations.json").read_bytes()),"receipt_source_locations_identity")
    equal(receipt["build_bindings_sha256"],binding_pin["sha256"],"receipt_build_identity")
    equal(Path(receipt["cwd"]).resolve(),context.repo,"receipt_working_directory")
    started, ended = datetime.fromisoformat(receipt["started_utc"]),datetime.fromisoformat(receipt["ended_utc"])
    need(started.utcoffset() is not None and ended.utcoffset() is not None and ended >= started,"receipt_time_order")
    command = receipt["command"]
    need(isinstance(command,list) and all(isinstance(arg,str) for arg in command) and command.count("aggregate")==1,"receipt_command_mode")
    mode_index=command.index("aggregate")
    need(mode_index>0,"receipt_command_tool_missing")
    equal(Path(command[mode_index-1]).resolve(),within(context.repo,TOOL),"receipt_command_tool")
    def argument(flag):
        need(command.count(flag)==1 and command.index(flag)>mode_index and command.index(flag)+1<len(command),"receipt_command_identity")
        return command[command.index(flag)+1]
    for flag, expected in (("--output-dir",output),("--source-repo",context.repo)):
        equal(Path(argument(flag)).resolve(),Path(expected).resolve(),"receipt_command_path:"+flag)
    equal(argument("--build-bindings-sha256"),binding_pin["sha256"],"receipt_command_build_hash")
    equal(pin(Path(argument("--build-bindings")).read_bytes()),binding_pin,"receipt_command_build_file")
    for name in ("stdout","stderr"):
        record=receipt[name]
        equal(pin(Path(record["path"]).read_bytes()),{k:record[k] for k in ("bytes","sha256")},"receipt_stream_changed")


def seal(context, receipt_path):
    output=context.output
    need(not (output/"publish").exists(),"already_sealed")
    manifest_raw=(output/"manifest.json").read_bytes()
    manifest=loads(manifest_raw)
    equal(manifest["schema"],"k46-aggregate-manifest/v1","aggregate_manifest_schema")
    equal(manifest["build_binding_sha256"],context.build_pin["sha256"],"aggregate_build_binding")
    equal(manifest["build_files"],context.build["files"],"aggregate_build_files")
    receipt=loads(Path(receipt_path).read_bytes())
    verify_receipt(receipt,context,pin(manifest_raw))
    actual={p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file()}
    equal(actual,set(manifest["payloads"])|{"manifest.json","locations.json"},"aggregate_inventory")
    for relative, expected in manifest["payloads"].items():
        equal(pin(within(output,relative).read_bytes()),expected,"aggregate_payload_changed:"+relative)
    sources=loads((output/"inputs.json").read_bytes())
    locations=loads((output/"locations.json").read_bytes())
    equal(set(sources),set(locations),"input_location_support")
    for logical,expected in sources.items():
        equal(pin(Path(locations[logical]).read_bytes()),expected,"consumed_source_changed:"+logical)
    for relative in BUILD_FILES:
        equal(pin(within(context.repo,relative).read_bytes()),context.build["files"][relative],"seal_build_changed")
    expected_public={REPORT,*[PUBLIC+name for name in TABLES],PUBLIC+"CHECK.json"}
    equal(set(manifest["public_files"]),expected_public,"public_write_set")
    draft=loads((output/"check-draft.json").read_bytes())
    equal(draft["build_files"],context.build["files"],"check_build_files")
    equal(set(draft["published_files"]),expected_public-{PUBLIC+"CHECK.json"},"check_public_set")
    for relative,expected in draft["published_files"].items():
        equal(pin(within(output,"pending/"+relative).read_bytes()),expected,"check_public_pin")
    # Publish only after all checks succeed. Partial/failed evidence is preserved.
    for relative in sorted(expected_public):
        raw=(output/"check-draft.json").read_bytes() if relative==PUBLIC+"CHECK.json" else within(output,"pending/"+relative).read_bytes()
        write_new(within(output,"publish/"+relative),raw)
    print(json.dumps(dict(status="SEALED_AGGREGATE_EXIT_0",published_files=len(expected_public))),flush=True)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest="mode",required=True)
    run=sub.add_parser("aggregate")
    run.add_argument("--source-repo",required=True)
    run.add_argument("--output-dir",required=True)
    finish=sub.add_parser("seal")
    finish.add_argument("--aggregate-dir",required=True)
    finish.add_argument("--aggregate-receipt",required=True)
    for command in (run,finish):
        command.add_argument("--build-bindings",required=True)
        command.add_argument("--build-bindings-sha256",required=True)
    args=parser.parse_args(argv)
    try:
        if args.mode=="aggregate":
            context=preflight(args.source_repo,args.output_dir,args.build_bindings,args.build_bindings_sha256)
            aggregate(context)
        else:
            raw=Path(args.build_bindings).read_bytes()
            equal(pin(raw)["sha256"],args.build_bindings_sha256,"build_binding_hash")
            context=preflight(loads(raw)["source_repo"],args.aggregate_dir,args.build_bindings,args.build_bindings_sha256,sealing=True)
            seal(context,args.aggregate_receipt)
    except (Rejected,ValueError,KeyError,TypeError,OSError,csv.Error) as exc:
        print(json.dumps(dict(status="REJECTED",reason=str(exc)),ensure_ascii=False),file=sys.stderr)
        return 2
    return 0


if __name__=="__main__":
    raise SystemExit(main())
