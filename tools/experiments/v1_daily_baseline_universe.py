"""Fixed K44 daily research over frozen exports; standard-library orchestration.

Production loads the accepted historical engine only after all common, build,
input and economic provenance checks.  Nothing in this module opens a database.
``matrix`` leaves CHECKs reserved; ``seal`` consumes a terminated child receipt.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, Inexact, ROUND_HALF_EVEN, localcontext
from pathlib import Path
from types import SimpleNamespace


NEW_PRODUCTS = ("INE.SC", "DCE.M", "CZCE.TA", "CFFEX.IF", "GFEX.SI")
PRODUCTS = ("SHFE.RB",) + NEW_PRODUCTS
STRATEGIES = ("donchian_trend", "ma_trend", "zscore_reversion")
METHODS = STRATEGIES + ("flat_control", "buy_hold_control")
CAPITALS = ("1000000", "300000", "100000", "50000")
SCENARIOS = ("BASE", "FEE_X2", "SLIPPAGE_X2", "MARGIN_X1_5")
YEARS = tuple(range(2020, 2027))
TABLES = ("sensitivity-summary.csv", "sensitivity-year.csv", "control-comparison.csv",
          "dominant-summary.csv", "dominant-year.csv", "dominant-scope.csv")
TOOL = "tools/experiments/v1_daily_baseline_universe.py"
CONFIG = "config/research/v1_daily_baseline_universe.json"
TEST = "tests/experiments/test_v1_daily_baseline_universe.py"
REPORT = "docs/research/v1-daily-baseline-universe.md"
PUBLIC = "docs/research/v1-daily-baseline-universe"
RB_PUBLIC = "docs/research/v1-daily-baseline-rb"
BUILD_FILES = (TOOL, CONFIG, TEST)
B_TOOL = "tools/experiments/v1_daily_baseline_sensitivity.py"
D_TOOL = "tools/experiments/v1_daily_baseline_dominant.py"
HELPER_HASHES = {
    B_TOOL: "aacced182ab148e4868f65104adc332b6d697c2aba57efd93ccb166b2afadb37",
    D_TOOL: "9152d4ea91f3bb7af7dc5596d6631db0ec1535f74664498809d6bbb16db2bbd2",
}
SOURCE_HASH = "07f5e571682ace02c1abcfcd7264a523ac1f840e4ec9c60cc8f63de4a45a9968"
ECON_HASH = "8bdcd7abe6c9c4ab330c173574eba6412d1372bcc6ff1d3dde292cd5900e7692"
PLAN_HASH = "c83ec85ba0e3aef44b7d912b264039e93e0bf985da0cf09dfa1b6cec422a8bce"
START = "2020-07-20T00:00:00+08:00"
END = "2026-09-05T00:00:00+08:00"
SHANGHAI = timezone(timedelta(hours=8))
ZERO = Decimal(0)
SAFETY = dict(enable_trading=False, observer="hold", broker="NOT_RUN",
              real_order_send_calls=0, real_order_cancel_calls=0)
MODE = dict(evidence_level="SCENARIO", statistical_qualification="NOT_ASSESSED",
            new_training=False, holdout_read=False, timing_verified=False,
            historical_known_at=None, historical_schedule_verified=False)
RULE = "PREVIOUS_STORED_COMPLETED_BAR_SCENARIO"
NAMES = dict(donchian_trend="突破趋势", ma_trend="均线趋势", zscore_reversion="均值回归",
             flat_control="始终空仓", buy_hold_control="首开一次买入持有")
ECON = {
    "INE.SC": ("1000", "0.1", "1db0d3280d3f53e6ffe5a04c5aaf824a1beef38ceaadd7962a8df54178d2dfea"),
    "DCE.M": ("10", "1", "e4d13ae3602d1aa9ce60c9f7c9e1da2fab2bae2a70b6e27fb7e8a02a37ae8591"),
    "CZCE.TA": ("5", "2", "376bb73c55da12065dc662f771103eda72ffb8dff9ce41ecc11003c6bcc79744"),
    "CFFEX.IF": ("300", "0.2", "ef9617375e11e7d151fce261b8c03b643097725f14155744a7f0bc0af101d720"),
    "GFEX.SI": ("5", "5", "98dc4eba4b911b8b37d9f40cc872fd6839d60cd959bec16697afe65927c48bc4"),
}
BASE = dict(initial_cash="1000000", lots=1, fee_per_lot="0.01", fee_rate="0.0001",
            slippage_ticks="1", margin_rate="0.12", max_margin_ratio="0.4")
SUMMARY_COLUMNS = "product method method_name capital scenario product_status dominant_status dominant_net all_accounts_net dominant_positive_years dominant_covered_years dominant_worst_year dominant_worst_year_net dominant_max_drawdown".split()
AVAIL_COLUMNS = "product status reason_code declared_contracts contracts_with_bars contracts_without_bars bar_rows start_raw_date end_raw_date export_raw_exit read_only_snapshot_verified input_manifest_sha256 economic_specs_status new_simulation".split()
DOM_COLUMNS = "product capital scenario method method_name status net positive_years covered_years worst_year worst_year_net max_drawdown longest_drawdown_observations open_drawdown_at_end covered_observations total_observations coverage_percent all_accounts all_accounts_net all_accounts_mean_numerator all_accounts_mean_denominator all_accounts_mean_display".split()
YEAR_COLUMNS = "product capital scenario method year year_status status net covered_observations total_observations".split()
SCOPE_COLUMNS = "product status selection_rule selection_sha256 input_window_start input_window_end declared_contracts contracts_with_bars contracts_without_bars input_bar_rows method_count capital_count scenario_count observations covered_observations uncovered_observations coverage_numerator coverage_denominator coverage_percent switches uncovered_segments maximum_oi_tie_days first_bar_exclusions missing_previous_oi_exclusions zero_previous_oi_candidates maximum_previous_bar_calendar_gap_days timing_verified historical_known_at information_time_basis".split()
CURVE_COLUMNS = "product capital scenario method raw_date status selected_contract selected_instance selected_net cumulative_net running_peak drawdown underwater_observations".split()
SELECTION_COLUMNS = "raw_date selected_contract selected_instance selected_j selected_datetime_nano selected_raw_ts status eligible_candidates max_prior_oi max_oi_ties selected_by_tie_rule".split()
UNAVAILABLE = {"NO_DATA", "BLOCKED_EXPORT", "BLOCKED_ECONOMIC_SPECS", "BLOCKED_INPUT_DATE_BOUNDARY", "BLOCKED_INPUT_VALIDATION"}
PAYLOAD_MEMBERS = {"bars.csv", "contracts.json", "metadata.json", "availability.json"}


class ValidationError(ValueError):
    pass


class InputError(ValidationError):
    def __init__(self, code, reason):
        self.code, self.reason = code, reason
        super().__init__(code + ":" + reason)


def need(condition, reason):
    if not condition:
        raise ValidationError(reason)


def equal(actual, expected, reason):
    need(actual == expected, reason)


def path(value):
    need(isinstance(value, (str, Path)), "path_type")
    result = Path(value)
    need(".." not in result.parts and not any(p.lower().startswith(".env") or "holdout" in p.lower() for p in result.parts), "forbidden_path")
    return result.resolve()


def child(root, relative):
    part = Path(relative)
    need(not part.is_absolute() and not part.drive and ".." not in part.parts, "relative_path")
    value = path(path(root) / part)
    need(value.is_relative_to(path(root)), "escaped_root")
    return value


def pin(raw):
    return dict(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())


def unique(items):
    result = {}
    for key, value in items:
        need(key not in result, "duplicate_json_key")
        result[key] = value
    return result


def loads(raw):
    def invalid(_):
        raise ValidationError("nonfinite_json")
    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid)


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")


def amount(value):
    need(isinstance(value, (str, int, Decimal)) and not isinstance(value, bool), "decimal_type")
    need(str(value) and str(value).strip() == str(value), "decimal_whitespace")
    value = Decimal(value)
    need(value.is_finite(), "nonfinite_amount")
    return value


def integer(value):
    need(not isinstance(value, bool) and re.fullmatch(r"-?(0|[1-9][0-9]*)", str(value)) is not None, "integer_type")
    return int(value)


def cell(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        text = format(value, "f")
        return "0" if value == 0 else text.rstrip("0").rstrip(".") if "." in text else text
    return str(value)


def csv_bytes(records, columns):
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(columns)
    writer.writerows([cell(record[key]) for key in columns] for record in records)
    return out.getvalue().encode("utf-8")


def rows(raw):
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8"), newline=""))
    need(reader.fieldnames and len(reader.fieldnames) == len(set(reader.fieldnames)), "csv_header")
    records = list(reader)
    need(all(None not in record and None not in record.values() for record in records), "csv_shape")
    return records


def write_new(filename, raw):
    filename = Path(filename)
    filename.parent.mkdir(parents=True, exist_ok=True)
    with filename.open("xb") as stream:
        stream.write(raw)
    equal(pin(filename.read_bytes()), pin(raw), "write_readback")


def atomic_new(filename, raw):
    filename = Path(filename)
    loads(raw)
    need(not filename.exists(), "existing_final")
    filename.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + filename.name + "-", dir=filename.parent)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
    Path(temporary).rename(filename)


class Sources:
    def __init__(self):
        self.records, self.locations = {}, {}

    def read(self, name, location, expected):
        filename = path(location)
        need(filename.is_file(), "missing_source:" + name)
        raw = filename.read_bytes()
        expected = {"sha256": expected} if isinstance(expected, str) else expected
        need(re.fullmatch(r"[0-9a-f]{64}", expected.get("sha256", "")) is not None, "invalid_source_hash")
        actual = pin(raw)
        for field in ("bytes", "sha256"):
            if field in expected:
                equal(actual[field], expected[field], "source_mismatch:" + name)
        if name in self.records:
            equal(actual, self.records[name], "source_drift:" + name)
            equal(str(filename), self.locations[name], "source_location_drift")
        self.records[name], self.locations[name] = actual, str(filename)
        return raw

    def verify(self):
        for name, expected in list(self.records.items()):
            self.read(name, self.locations[name], expected)


def load_helpers(repo):
    modules = []
    for index, relative in enumerate((B_TOOL, D_TOOL)):
        filename = child(path(repo), relative)
        equal(pin(filename.read_bytes())["sha256"], HELPER_HASHES[relative], "accepted_helper")
        spec = importlib.util.spec_from_file_location("k44_accepted_helper_" + str(index), filename)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        modules.append(module)
    return tuple(modules)


def fixed_config():
    return dict(schema="k44-universe-config/v1", new_product_order=list(NEW_PRODUCTS), reuse_product="SHFE.RB",
                method_order=list(METHODS), capital_order=list(CAPITALS), scenario_order=list(SCENARIOS),
                raw_years=list(YEARS), views=["ALL", "PRIMARY"], primary_j_min=60,
                export_start=START, export_end_exclusive=END, duration_seconds=86400,
                base_assumptions=BASE, strategy_parameters={
                    "donchian_trend": dict(entry_window=40, exit_window=20),
                    "ma_trend": dict(fast_window=20, slow_window=60),
                    "zscore_reversion": dict(lookback=20, entry_z="2.0", exit_z="0.5", stop_z="4.0", max_signal_age=20)},
                economics={p: dict(multiplier=v[0], price_tick=v[1], official_source_sha256=v[2],
                                  historical_schedule_verified=False) for p, v in ECON.items()},
                source_bindings_sha256=SOURCE_HASH, economic_specs_sha256=ECON_HASH,
                selection_rule=RULE, safety=SAFETY)


def matrix_configs(product, config):
    equal(config, fixed_config(), "fixed_configuration")
    need(product in NEW_PRODUCTS, "new_product_only")
    economics = config["economics"][product]
    for capital in CAPITALS:
        for scenario in SCENARIOS:
            values = dict(BASE, initial_cash=capital, multiplier=economics["multiplier"], price_tick=economics["price_tick"])
            if scenario == "FEE_X2":
                values.update(fee_per_lot="0.02", fee_rate="0.0002")
            elif scenario == "SLIPPAGE_X2":
                values["slippage_ticks"] = "2"
            elif scenario == "MARGIN_X1_5":
                values["margin_rate"] = "0.18"
            yield capital, scenario, values


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    need(parsed.utcoffset() is not None, "naive_timestamp")
    return parsed.astimezone(SHANGHAI)


def nano(value):
    delta = timestamp(value).astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 10**9 + delta.microseconds * 1000


def guard_dates(records):
    """Dates only, over every row, before numerical validation or statistics."""
    for record in records:
        try:
            raw_day = timestamp(record["ts"]).date()
            days = [raw_day]
            if record.get("trading_day"):
                days.append(date.fromisoformat(record["trading_day"]))
        except (ValueError, TypeError, KeyError) as exc:
            raise InputError("BLOCKED_INPUT_VALIDATION", "invalid_date_or_timezone") from exc
        if any(not date(2020, 7, 20) <= value < date(2026, 9, 5) for value in days):
            raise InputError("BLOCKED_INPUT_DATE_BOUNDARY", "market_date_outside_fixed_window")


def inspect_input(product, manifest, payloads):
    """Validate one frozen export without importing an engine or computing PnL."""
    try:
        records = rows(payloads["bars.csv"])
    except (ValueError, KeyError, UnicodeError) as exc:
        raise InputError("BLOCKED_INPUT_VALIDATION", "invalid_bar_csv") from exc
    guard_dates(records)
    try:
        equal(manifest["schema_version"], "futures_bundle_v1", "input_schema")
        equal((manifest["status"], manifest["dataset_kind"]), ("complete", "real_export"), "input_complete_real")
        request = manifest["request"]
        equal(request["products"], [product], "single_product_request")
        equal((timestamp(request["start"]), timestamp(request["end"])), (timestamp(START), timestamp(END)), "input_window")
        equal(integer(request["duration_seconds"]), 86400, "input_duration")
        equal((integer(request["start_nano"]), integer(request["end_nano"])), (nano(START), nano(END)), "request_nanos")
        evidence = manifest["read_only_transaction"]
        need(evidence["verified"] is True and evidence["executed"] is True and evidence["observed_read_only"] == "on"
             and evidence["observed_isolation"] == "repeatable read", "unverified_read_only_snapshot")
        declarations = loads(payloads["contracts.json"])
        need(isinstance(declarations, list), "declarations_type")
        by_symbol, instances = {}, set()
        for declaration in declarations:
            symbol, instance = declaration["raw_symbol"], declaration["instance_id"]
            equal(declaration["product"], product, "declaration_product")
            match = re.fullmatch(r"(INE|DCE|CZCE|CFFEX|GFEX)\.([A-Za-z]+)([0-9]{3,4})", symbol)
            need(match is not None, "concrete_symbol")
            exchange, code, delivery = match.groups()
            equal(exchange + "." + code.upper(), product, "symbol_product")
            need(len(delivery) == (3 if exchange == "CZCE" else 4) and 1 <= int(delivery[-2:]) <= 12, "delivery_code")
            expiry = datetime.fromisoformat(declaration["expiry_at"])
            need(expiry.utcoffset() is not None, "expiry_timezone")
            equal(instance, symbol + "|" + expiry.isoformat(), "full_instance_identity")
            need(instance not in instances, "duplicate_instance")
            need(symbol not in by_symbol, "ambiguous_symbol_instances")
            instances.add(instance)
            by_symbol[symbol] = declaration
        bars, mapping, seen = defaultdict(list), [], set()
        for record in records:
            symbol = record["symbol"]
            need(symbol in by_symbol, "undeclared_bar")
            ns = integer(record["datetime_nano"])
            equal(ns, nano(record["ts"]), "timestamp_nano_identity")
            equal(integer(record["duration_seconds"]), 86400, "bar_duration")
            normalized = timestamp(record["ts"]).isoformat()
            raw_day = timestamp(record["ts"]).date().isoformat()
            identity = (by_symbol[symbol]["instance_id"], raw_day)
            need(identity not in seen, "duplicate_instance_date")
            seen.add(identity)
            prices = {name: amount(record[name]) for name in ("open", "high", "low", "close")}
            need(all(value > 0 for value in prices.values()), "nonpositive_ohlc")
            need(prices["low"] <= min(prices["open"], prices["close"]) <= max(prices["open"], prices["close"]) <= prices["high"], "ohlc_envelope")
            for name in ("volume", "open_oi", "close_oi"):
                if record[name] not in ("", None):
                    need(amount(record[name]) >= 0, "negative_volume_oi")
            row = dict(record, ts=normalized, datetime_nano=ns,
                       contract_instance_id=by_symbol[symbol]["instance_id"], raw_date=raw_day)
            bars[symbol].append(row)
            mapping.append(dict(symbol=symbol, contract_instance_id=row["contract_instance_id"], datetime_nano=ns,
                                original_ts=record["ts"], normalized_raw_ts=normalized, original_trading_day=record["trading_day"]))
        equal(manifest["row_counts"], dict(bars=len(records), contracts=len(declarations)), "manifest_counts")
        for name in ("metadata.json", "availability.json"):
            need(isinstance(loads(payloads[name]), dict), "input_sidecar_type")
        for symbol, values in bars.items():
            values.sort(key=lambda r: r["datetime_nano"])
            for j, row in enumerate(values):
                row["j"] = j
        days = sorted({row["raw_date"] for row in records_normalized(bars)})
        stats = dict(declared_contracts=len(declarations), contracts_with_bars=len(bars),
                     contracts_without_bars=len(declarations) - len(bars), bar_rows=len(records),
                     start_raw_date=days[0] if days else None, end_raw_date=days[-1] if days else None)
        return SimpleNamespace(bars=dict(bars), declarations=by_symbol, mapping=sorted(mapping, key=lambda r: (r["symbol"], r["datetime_nano"])), stats=stats)
    except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
        reason = str(exc) if isinstance(exc, ValidationError) else "invalid_input_schema_or_value"
        raise InputError("BLOCKED_INPUT_VALIDATION", reason) from exc


def records_normalized(bars):
    return [row for symbol in sorted(bars) for row in bars[symbol]]


def selected_bars(prepared):
    return {symbol: [dict(symbol=symbol, contract_instance_id=r["contract_instance_id"], j=r["j"],
                         datetime_nano=r["datetime_nano"], raw_ts=r["ts"], raw_date=r["raw_date"],
                         close_oi=None if not r["close_oi"] else amount(r["close_oi"]), available_at=r["available_at"])
                     for r in records] for symbol, records in prepared.bars.items()}


def selection_csv(selection):
    output = []
    for record in selection:
        key = record["selected_key"]
        output.append({name: record[name] for name in SELECTION_COLUMNS if name not in ("selected_j", "selected_datetime_nano", "selected_raw_ts")}
                      | dict(selected_j=key[5] if key else None, selected_datetime_nano=key[2] if key else None,
                             selected_raw_ts=key[3] if key else None))
    return csv_bytes(output, SELECTION_COLUMNS)


def ratio(numerator, denominator, places=3, multiplier=1):
    need(denominator > 0, "ratio_denominator")
    with localcontext() as ctx:
        ctx.prec = 160
        ctx.traps[Inexact] = False
        value = numerator * Decimal(multiplier) / Decimal(denominator)
        return format(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN), "f")


def ledger_key(record):
    return tuple(record[name] for name in ("symbol", "contract_instance_id", "datetime_nano", "raw_ts", "raw_date", "j"))


def dominant_cell(product, capital, scenario, method, selection, nets, account_count, all_net):
    cumulative = peak = max_dd = ZERO
    duration = max_duration = covered = 0
    annual = {year: ZERO for year in YEARS}
    annual_count = {year: 0 for year in YEARS}
    curves = []
    for item in selection:
        key = tuple(item["selected_key"]) if item["selected_key"] is not None else None
        need(key is None or key in nets, "selected_support_mismatch")
        value = nets[key] if key else None
        year = date.fromisoformat(item["raw_date"]).year
        if value is not None:
            cumulative += value
            annual[year] += value
            annual_count[year] += 1
            covered += 1
        peak = max(peak, cumulative)
        dd = peak - cumulative
        duration = duration + 1 if dd > 0 else 0
        max_duration, max_dd = max(max_duration, duration), max(max_dd, dd)
        curves.append(dict(product=product, capital=capital, scenario=scenario, method=method, raw_date=item["raw_date"],
                           status=item["status"], selected_contract=item["selected_contract"], selected_instance=item["selected_instance"],
                           selected_net=value, cumulative_net=cumulative, running_peak=peak, drawdown=dd,
                           underwater_observations=duration))
    equal(sum(annual.values(), ZERO), cumulative, "dominant_annual_conservation")
    equal(sum(nets.values(), ZERO), all_net, "dominant_full_account_anchor")
    observed = [year for year in YEARS if annual_count[year]]
    worst = min(observed, key=lambda year: (annual[year], year)) if observed else None
    summary = dict(product=product, capital=capital, scenario=scenario, method=method, method_name=NAMES[method],
                   status="COVERED" if covered else "NO_COVERAGE", net=cumulative if covered else None,
                   positive_years=sum(annual[y] > 0 for y in observed), covered_years=len(observed),
                   worst_year=worst, worst_year_net=annual[worst] if worst is not None else None,
                   max_drawdown=max_dd if covered else None, longest_drawdown_observations=max_duration if covered else None,
                   open_drawdown_at_end=duration > 0 if covered else None, covered_observations=covered,
                   total_observations=len(selection), coverage_percent=ratio(Decimal(covered), len(selection), 6, 100),
                   all_accounts=account_count, all_accounts_net=all_net, all_accounts_mean_numerator=all_net,
                   all_accounts_mean_denominator=account_count, all_accounts_mean_display=ratio(all_net, account_count))
    years = [dict(product=product, capital=capital, scenario=scenario, method=method, year=year,
                  year_status="PARTIAL_INPUT_WINDOW" if year in (2020, 2026) else "OBSERVED_RAW_YEAR",
                  status="COVERED" if annual_count[year] else "NO_COVERAGE", net=annual[year] if annual_count[year] else None,
                  covered_observations=annual_count[year], total_observations=sum(date.fromisoformat(x["raw_date"]).year == year for x in selection)) for year in YEARS]
    if not covered:
        for row in curves:
            for field in ("cumulative_net", "running_peak", "drawdown", "underwater_observations"):
                row[field] = None
    return summary, years, curves


def dominant_scope(product, prepared, selection, selection_raw):
    previous, switches, gaps, in_gap = None, 0, 0, False
    for item in selection:
        current = (item["selected_contract"], item["selected_instance"]) if item["selected_key"] else None
        if current is None:
            gaps += int(not in_gap)
            in_gap = True
        else:
            switches += int(previous is not None and previous != current)
            previous, in_gap = current, False
    candidates = [r for item in selection for r in item["candidates"]]
    covered = sum(x["selected_key"] is not None for x in selection)
    stats = prepared.stats
    return dict(product=product, status="COVERED" if covered else "NO_COVERAGE", selection_rule=RULE,
                selection_sha256=pin(selection_raw)["sha256"], input_window_start=stats["start_raw_date"], input_window_end=stats["end_raw_date"],
                declared_contracts=stats["declared_contracts"], contracts_with_bars=stats["contracts_with_bars"], contracts_without_bars=stats["contracts_without_bars"],
                input_bar_rows=stats["bar_rows"], method_count=5, capital_count=4, scenario_count=4, observations=len(selection),
                covered_observations=covered, uncovered_observations=len(selection) - covered,
                coverage_numerator=covered, coverage_denominator=len(selection), coverage_percent=ratio(Decimal(covered), len(selection), 6, 100),
                switches=switches, uncovered_segments=gaps, maximum_oi_tie_days=sum(x["selected_by_tie_rule"] for x in selection),
                first_bar_exclusions=sum(r["status"] == "FIRST_STORED_BAR" for r in candidates),
                missing_previous_oi_exclusions=sum(r["status"] == "MISSING_PREVIOUS_OI" for r in candidates),
                zero_previous_oi_candidates=sum(r["status"] == "ELIGIBLE" and amount(r["prior_close_oi"]) == 0 for r in candidates),
                maximum_previous_bar_calendar_gap_days=max(((date.fromisoformat(r["raw_date"]) - date.fromisoformat(r["prior_raw_date"])).days for r in candidates if r["prior_raw_date"]), default=0),
                timing_verified=False, historical_known_at=None, information_time_basis=RULE)


def binding(sources, name, location, digest):
    return loads(sources.read(name, location, digest))


def export_outcome(raw, raw_exit):
    """Return only fixed, credential-safe fields from the pinned exporter schema."""
    unknown = dict(status="UNCLASSIFIED_EXPORT_FAILURE", reasons=["UNCLASSIFIED_EXPORT_FAILURE"],
                   row_counts={}, read_only_snapshot_verified=False)
    try:
        value = loads(raw)
        status = value["status"]
        allowed = {"published", "empty_real_export", "validation_rejected", "scope_limit", "export_blocked", "rejected"}
        if status not in allowed or raw_exit not in (0, 2, 3) or (raw_exit == 0) != (status == "published"):
            return unknown
        reason_values = value.get("reasons", [value["reason"]] if "reason" in value else [])
        fixed = {"no_rows_in_requested_range", "database_connection_failed", "database_read_failed",
                 "database_connection_cleanup_failed", "invalid_database_url_format", "database_engine_failed",
                 "export_operation_failed", "database_cleanup_failed", "database_dependencies_unavailable",
                 "AK_DATABASE_URL_missing_or_empty", "AK_DATABASE_URL_not_present_in_process_environment",
                 "env_file_unreadable", "invalid_product_token", "invalid_or_unsupported_products",
                 "duration_must_be_positive_integer", "output_directory_exists"}
        patterns = (r"(?:duplicate_keys|invalid_ohlc|ordering_errors|negative_volume_oi|conflicting_identity)=[0-9]+",
                    r"max_(?:contracts|bar_rows|bundle_bytes|products)_exceeded:[0-9]+>[0-9]+",
                    r"read_only_snapshot_unverified:(?:unsupported_dialect|verification_failed|not_executed|read_only_verification_failed)")
        if not isinstance(reason_values, list) or any(not isinstance(reason, str) or
            (reason not in fixed and not any(re.fullmatch(pattern, reason) for pattern in patterns)) for reason in reason_values):
            return unknown
        counts = value.get("row_counts", {})
        if not isinstance(counts, dict) or set(counts) - {"bars", "contracts", "valid_rows"}:
            return unknown
        if any(type(count) is not int or count < 0 for count in counts.values()):
            return unknown
        read_only = value.get("read_only_transaction", {})
        verified = read_only.get("verified") is True and read_only.get("observed_read_only") == "on" and read_only.get("observed_isolation") == "repeatable read"
        return dict(status=status, reasons=reason_values, row_counts=counts, read_only_snapshot_verified=verified)
    except (ValueError, KeyError, TypeError, UnicodeError):
        return unknown


def verify_export_receipt(sources, product, entry):
    receipt_ref = entry["export_receipt"]
    receipt = loads(sources.read("export." + product, receipt_ref["path"], receipt_ref))
    need(receipt["terminated"] is True and receipt["streams_closed"] is True and receipt["timed_out"] is False,
         "export_not_terminated")
    equal(receipt["raw_exit_subject"], "export_child", "export_subject")
    equal(receipt["raw_exit"], entry["export_raw_exit"], "export_raw_exit")
    argv = receipt["argv"]
    need(isinstance(argv, list) and argv.count("--products") == 1, "export_command")
    equal(argv[argv.index("--products") + 1], product, "export_product")
    for option, value in (("--start", START), ("--end", END), ("--duration", "86400")):
        need(argv.count(option) == 1 and argv[argv.index(option) + 1] == value, "export_fixed_option")
    need(any(Path(token).name == "export_futures_input.py" for token in argv), "export_script")
    equal(set(receipt["closed_streams"]), {"stdout", "stderr"}, "export_closed_stream_set")
    streams = {}
    for name, record in receipt["closed_streams"].items():
        # Verify raw log bytes without displaying or interpreting their content.
        streams[name] = sources.read("export-stream." + product + "." + name, record["path"], record)
    outcome = export_outcome(streams["stdout"], receipt["raw_exit"])
    equal(entry["export_outcome"], outcome, "actual_export_outcome")
    equal(entry["read_only_snapshot_verified"], outcome["read_only_snapshot_verified"], "actual_export_snapshot")
    if entry["status"] in {"READY", "BLOCKED_INPUT_VALIDATION", "BLOCKED_INPUT_DATE_BOUNDARY"}:
        equal(receipt["raw_exit"], 0, "successful_export_required")
        equal(outcome["status"], "published", "published_export_required")
        need(outcome["read_only_snapshot_verified"], "successful_read_only_export_required")
    elif entry["status"] == "NO_DATA":
        equal((receipt["raw_exit"], entry["reason_code"]), (3, "no_rows_in_requested_range"), "empty_export_evidence")
        equal(entry["validated_export_bar_rows"], 0, "empty_export_count")
        equal((outcome["status"], outcome["reasons"], outcome["row_counts"].get("valid_rows")),
              ("empty_real_export", ["no_rows_in_requested_range"], 0), "actual_empty_export")
    elif entry["status"] == "BLOCKED_EXPORT":
        need(receipt["raw_exit"] != 0, "failed_export_requires_nonzero")
        equal(entry["reason_code"], ";".join(outcome["reasons"]) or outcome["status"], "actual_export_failure_reason")
    return receipt


def guard_bundle_output(output, products):
    """Freeze each real bundle directory, including unavailable input bundles.

    The declared root is not trusted independently: the manifest and all four
    exact-named payload paths must resolve directly beneath that same root.
    """
    output = path(output)
    for product in NEW_PRODUCTS:
        bundle = products[product].get("bundle")
        if bundle is None:
            continue
        root = path(bundle["root"])
        equal(path(bundle["manifest"]["path"]), child(root, "manifest.json"), "bundle_manifest_root_identity")
        equal(set(bundle["payloads"]), PAYLOAD_MEMBERS, "bundle_payload_members")
        for name, record in bundle["payloads"].items():
            equal(path(record["path"]), child(root, name), "bundle_payload_root_identity")
        need(not output.is_relative_to(root) and not root.is_relative_to(output), "output_input_bundle_overlap")


def preflight(repo, source_file, source_hash, input_file, input_hash, build_file, build_hash, output, *, sealing=False):
    repo, output = path(repo), path(output)
    need(repo.is_dir(), "source_repo_missing")
    need(output.is_dir() if sealing else not output.exists(), "output_state")
    sources = Sources()
    equal(source_hash, SOURCE_HASH, "effective_source_binding")
    authority = binding(sources, "source-bindings", source_file, source_hash)
    equal(authority["schema"], "k44-author-source-bindings/v1", "source_schema")
    source_values = {}
    for name, record in sorted(authority["sources"].items()):
        source_values[name.replace("\\", "/")] = sources.read("authority." + name, record["path"], record)
        if name.replace("\\", "/") in (B_TOOL, D_TOOL):
            sources.read("local." + name, child(repo, name), record)
    specs = loads(sources.read("economic-specs", authority["economic_specs"]["path"], authority["economic_specs"]))
    equal(authority["economic_specs"]["sha256"], ECON_HASH, "economic_binding")
    equal(set(specs["products"]), set(NEW_PRODUCTS), "economic_products")
    build = binding(sources, "build-bindings", build_file, build_hash)
    equal(build["schema"], "k44-build-bindings/v1", "build_schema")
    equal(build["status"], "FROZEN_AFTER_COMPLETE_SYNTHETIC_TESTS", "build_status")
    equal(build["plan"]["sha256"], PLAN_HASH, "effective_plan")
    sources.read("effective-plan", build["plan"]["path"], build["plan"])
    equal(set(build["files"]), set(BUILD_FILES), "build_write_set")
    for relative, record in build["files"].items():
        sources.read("build." + relative, child(repo, relative), record)
    equal(pin(Path(__file__).read_bytes()), {k: build["files"][TOOL][k] for k in ("bytes", "sha256")}, "running_driver_pin")
    config = loads(child(repo, CONFIG).read_bytes())
    equal(config, fixed_config(), "fixed_configuration")
    test_ref = build["targeted_receipt"]
    test_receipt = loads(sources.read("build-targeted-receipt", test_ref["path"], test_ref))
    need(test_receipt["raw_exit"] == 0 and test_receipt["terminated"] is True and test_receipt["streams_closed"] is True,
         "targeted_terminal_success")
    equal(build["synthetic_determinism"], "TWO_COMPLETE_PAYLOADS_AND_CHECKS_BYTE_EQUAL", "complete_synthetic_test")
    original = loads(source_values["K40B_source_freeze"])["source_bindings"]
    engine_root = path(authority["engine_source_root"])
    engine_hashes = {}
    for name, record in original["source_pins"].items():
        if name.startswith("engine."):
            relative = name.removeprefix("engine.")
            equal(path(original["physical_locations"][name]), child(engine_root, relative), "original_engine_locator")
            sources.read(name, original["physical_locations"][name], record)
            engine_hashes[relative] = record["sha256"]
    equal(len(engine_hashes), 10, "original_engine_source_count")
    for key in ("K40B_verdict", "K43_verdict"):
        verdict = loads(source_values[key])
        need(verdict["verdict"] == "PASS" and verdict["conditional"] is False and verdict["unconditional"] is True, "prior_unconditional_pass")
        for relative, record in verdict["reviewed_files"].items():
            if relative.endswith(".py") or relative.startswith("config/"):
                sources.read("preserved." + relative, child(repo, relative), record)
    rb = {name: source_values[RB_PUBLIC + "/" + name] for name in TABLES}
    original_manifest = loads(sources.read("RB.input.manifest", original["physical_locations"]["input.manifest"], original["source_pins"]["input.manifest"]))
    rb_count = original["accepted_counts"]
    rb_scope = {r["metric"]: r["value"] for r in rows(rb["dominant-scope.csv"])}
    rb_availability = dict(product="SHFE.RB", status="REUSED_ACCEPTED", reason_code="REUSED_ACCEPTED_RB",
        declared_contracts=rb_count["rb_declarations"], contracts_with_bars=rb_count["rb_declarations"], contracts_without_bars=0,
        bar_rows=rb_count["rb_bars"], start_raw_date=rb_scope["input_window_start"], end_raw_date=rb_scope["input_window_end"],
        export_raw_exit=None, read_only_snapshot_verified=original_manifest["read_only_transaction"]["verified"],
        input_manifest_sha256=original["source_pins"]["input.manifest"]["sha256"], economic_specs_status="REUSED_ACCEPTED_RB", new_simulation=False)
    inputs = binding(sources, "input-bindings", input_file, input_hash)
    equal(inputs["schema"], "k44-input-bindings/v1", "input_binding_schema")
    equal(inputs["source_bindings_sha256"], source_hash, "input_source_identity")
    equal(set(inputs["products"]), set(NEW_PRODUCTS), "five_input_products")
    guard_bundle_output(output, inputs["products"])
    prepared = {}
    for product in NEW_PRODUCTS:
        entry = inputs["products"][product]
        need(entry["status"] == "READY" or entry["status"] in UNAVAILABLE, "product_status")
        verify_export_receipt(sources, product, entry)
        spec = specs["products"][product]
        equal((spec["multiplier"], spec["price_tick"], spec["source"]["sha256"]), ECON[product], "fixed_economics")
        if entry["status"] != "BLOCKED_ECONOMIC_SPECS":
            sources.read("economic." + product, spec["source"]["path"], spec["source"])
        else:
            filename = path(spec["source"]["path"])
            observed = pin(filename.read_bytes()) if filename.is_file() else None
            equal(entry["economic_observed_pin"], observed, "frozen_unavailable_economic_source")
            need(observed != {k: spec["source"][k] for k in ("bytes", "sha256")}, "economic_source_not_actually_blocked")
        if entry.get("bundle") is not None:
            bundle = entry["bundle"]
            raw_manifest = sources.read("input." + product + ".manifest", bundle["manifest"]["path"], bundle["manifest"])
            manifest = loads(raw_manifest)
            equal(set(bundle["payloads"]), PAYLOAD_MEMBERS, "four_input_payloads")
            payloads = {name: sources.read("input." + product + "." + name, record["path"], record) for name, record in bundle["payloads"].items()}
            equal(manifest["content_hashes"], {name: pin(raw)["sha256"] for name, raw in payloads.items()}, "manifest_payload_hashes")
            equal(manifest["source_hashes"], {
                "catalog": authority["sources"]["config/china_futures_catalog.json"]["sha256"],
                "universe_cache": authority["sources"]["data/downloads/backfill_contract_universe.json"]["sha256"]}, "export_source_hashes")
            if entry["status"] in ("READY", "BLOCKED_INPUT_DATE_BOUNDARY", "BLOCKED_INPUT_VALIDATION"):
                try:
                    value = inspect_input(product, manifest, payloads)
                except InputError as exc:
                    equal(entry["status"], exc.code, "frozen_validation_status")
                    equal(entry["reason_code"], exc.reason, "frozen_validation_reason")
                else:
                    equal(entry["status"], "READY", "unexpected_valid_frozen_input")
                    need(value.stats["contracts_with_bars"] > 0, "ready_input_empty")
                    equal(entry["support"], value.stats, "frozen_support")
                    prepared[product] = value
        else:
            need(entry["status"] != "READY", "ready_bundle_missing")
    for location in sources.locations.values():
        other = path(location)
        need(not other.is_relative_to(output) and not output.is_relative_to(other), "output_source_overlap")
    for protected in (repo, engine_root):
        need(not output.is_relative_to(protected) and not protected.is_relative_to(output), "output_protected_overlap")
    b, dominant = load_helpers(repo)
    sources.verify()
    logical_sources = {name: record for name, record in sources.records.items()
                       if not name.startswith(("export.", "export-stream.", "build-targeted-receipt"))}
    return SimpleNamespace(repo=repo, output=output, sources=sources, config=config, authority=authority,
        specs=specs, build=build, inputs=inputs, prepared=prepared, engine_root=engine_root, engine_hashes=engine_hashes,
        b=b, dominant=dominant, rb=rb, rb_availability=rb_availability, source_hash=source_hash, input_hash=input_hash,
        build_hash=build_hash, logical_sources=logical_sources, build_pins={name: {k: record[k] for k in ("bytes", "sha256")} for name, record in build["files"].items()})


class PayloadWriter:
    def __init__(self, root):
        self.root, self.inventory = path(root), {}

    def write(self, relative, raw):
        need(relative not in self.inventory and not relative.endswith("CHECK.json"), "payload_reserved_or_duplicate")
        write_new(child(self.root, relative), raw)
        self.inventory[relative] = pin(raw)


def bundle_views(prepared, engine_io, b):
    views = {}
    for symbol, records in prepared.bars.items():
        parsed = tuple(engine_io.bars_from_rows(records))
        views[symbol] = engine_io.InputBundle(Path("FROZEN_VERIFIED_K44_INPUT"),
            b.frozen(dict(schema_version="futures_bundle_v1", status="complete", dataset_kind="real_export")),
            parsed, (b.frozen(prepared.declarations[symbol]),), b.frozen({}), b.frozen({}))
    return views


def run_product(context, product, writer, engine, engine_io):
    prepared, b = context.prepared[product], context.b
    views = bundle_views(prepared, engine_io, b)
    bar_records = selected_bars(prepared)
    selection = context.dominant.select_dominant(bar_records)
    selection_raw = selection_csv(selection)
    prefix = "products/" + product + "/"
    writer.write(prefix + "selection.csv", selection_raw)
    writer.write(prefix + "selection-candidates.json", json_bytes(selection))
    writer.write(prefix + "timestamp-map.csv", csv_bytes(prepared.mapping, ["symbol", "contract_instance_id", "datetime_nano", "original_ts", "normalized_raw_ts", "original_trading_day"]))
    base_keys = {ledger_key(r) for values in bar_records.values() for r in values}
    groups, signal_pins, accounts, contract_year = defaultdict(list), {}, [], []
    dominant_rows, year_rows, curves = [], [], []
    counts = dict(accounts=0, strategy_accounts=0, control_accounts=0, ALL_observations=0,
                  PRIMARY_observations=0, contract_year_cells=0)
    for capital, scenario, values in matrix_configs(product, context.config):
        nets = {method: {} for method in METHODS}
        for symbol, view in sorted(views.items()):
            declaration = dict(view.contracts[0])
            instance = declaration["instance_id"]
            slug = symbol + "-" + hashlib.sha256(instance.encode()).hexdigest()[:16]
            for method in METHODS:
                if method in STRATEGIES:
                    result = b.native_result(engine.run_scenario_backtest(view, symbol=symbol, strategy=method,
                                                                        config=engine.ScenarioConfig.from_mapping(values)))
                    signature = pin(json_bytes([{k: r[k] for k in ("candidate_direction", "logical_direction", "signal_age", "strategy_status", "reason")} for r in result["decisions"]]))["sha256"]
                    key = instance, method
                    equal(signature, signal_pins.setdefault(key, signature), "logical_signals_changed_with_funding")
                else:
                    result = b.control_account(view.bars, declaration, method, values)
                ledger = b.verify_account(view.bars, declaration, method, capital, scenario, values, result)
                for row in ledger:
                    key = ledger_key(row)
                    need(key not in nets[method], "duplicate_ledger_support")
                    nets[method][key] = row["net_delta"]
                account_path = prefix + "accounts/C" + capital + "/" + scenario + "/" + slug + "/" + method
                if method in STRATEGIES:
                    native = b.native_payloads(result, engine)
                else:
                    from dataclasses import fields
                    native = {"config.json": json_bytes(result["config"]), "report.json": json_bytes(result["report"]),
                              "fills.csv": b.csv_bytes(result["fills"], [f.name for f in fields(engine.ScenarioFill)]),
                              "equity.csv": b.csv_bytes(result["equity"]), "decisions.csv": b.csv_bytes(result["decisions"])}
                for name, raw in native.items():
                    writer.write(account_path + "/" + name, raw)
                writer.write(account_path + "/ledger.csv", b.csv_bytes(ledger))
                accounts.append(dict(product=product, capital=capital, scenario=scenario, method=method,
                                     symbol=symbol, contract_instance_id=instance, account_path=account_path,
                                     observations=len(ledger), terminal_net=cell(ledger[-1]["net"])))
                counts["accounts"] += 1
                counts["strategy_accounts" if method in STRATEGIES else "control_accounts"] += 1
                counts["ALL_observations"] += len(ledger)
                counts["PRIMARY_observations"] += max(0, len(ledger) - 60)
                for view_name in ("ALL", "PRIMARY"):
                    for scope in ("ALL_YEARS", *YEARS):
                        value = b.window_cell(ledger, view_name, scope)
                        groups[b.group_key(capital, scenario, method, view_name, scope)].append(value)
                        if scope != "ALL_YEARS":
                            contract_year.append(dict(product=product, **{k: v for k, v in value.items() if not k.startswith("_")}))
                            counts["contract_year_cells"] += 1
        for method in METHODS:
            equal(set(nets[method]), base_keys, "method_support_mismatch")
            all_net = sum((x["net_sum"] for x in groups[b.group_key(capital, scenario, method, "ALL")]), ZERO)
            summary, annual, curve = dominant_cell(product, capital, scenario, method, selection, nets[method], len(views), all_net)
            dominant_rows.append(summary)
            year_rows.extend(annual)
            curves.extend(curve)
    equal(counts["accounts"], len(views) * 80, "all_accounts_present")
    equal((len(dominant_rows), len(year_rows)), (80, 560), "dominant_matrix_size")
    summaries, years, controls = b.aggregate_matrix(groups)
    tables = {name: [dict(product=product, **row) for row in records] for name, records in zip(TABLES[:3], (summaries, years, controls))}
    tables.update({TABLES[3]: dominant_rows, TABLES[4]: year_rows, TABLES[5]: [dominant_scope(product, prepared, selection, selection_raw)]})
    writer.write(prefix + "account-index.csv", csv_bytes(accounts, list(accounts[0])))
    writer.write(prefix + "contract-year.csv", csv_bytes(contract_year, list(contract_year[0])))
    writer.write(prefix + "dominant-curves.csv", csv_bytes(curves, CURVE_COLUMNS))
    return tables, counts


def public_path(name):
    return "publish/" + PUBLIC + "/" + name


def final_checks():
    return [public_path(product + "/CHECK.json") for product in PRODUCTS] + [public_path("CHECK.json")]


def expected_public():
    return {"publish/" + REPORT, public_path("summary.csv"), public_path("availability.csv")} | {
        public_path(product + "/" + name) for product in PRODUCTS for name in TABLES}


def headers(context):
    result = {name: ["product"] + next(csv.reader(io.StringIO(context.rb[name].decode("utf-8")))) for name in TABLES[:3]}
    result.update({TABLES[3]: DOM_COLUMNS, TABLES[4]: YEAR_COLUMNS, TABLES[5]: SCOPE_COLUMNS})
    return result


def availability_row(product, entry, prepared, specs):
    status = "SIMULATED" if entry["status"] == "READY" else entry["status"]
    stats = prepared.stats if prepared is not None else {name: None for name in ("declared_contracts", "contracts_with_bars", "contracts_without_bars", "bar_rows", "start_raw_date", "end_raw_date")}
    if status == "NO_DATA":
        stats["bar_rows"] = entry["validated_export_bar_rows"]
    return dict(product=product, status=status, reason_code="FIXED_SCENARIO_MATRIX" if status == "SIMULATED" else entry["reason_code"],
                **stats, export_raw_exit=entry["export_raw_exit"], read_only_snapshot_verified=entry["read_only_snapshot_verified"],
                input_manifest_sha256=entry["bundle"]["manifest"]["sha256"] if entry.get("bundle") else None,
                economic_specs_status="BLOCKED_ECONOMIC_SPECS" if status == "BLOCKED_ECONOMIC_SPECS" else specs[product]["author_verification"],
                new_simulation=status == "SIMULATED")


def summary_table(tables, availability):
    results = []
    for product, available in zip(PRODUCTS, availability):
        for method in METHODS:
            record = dict.fromkeys(SUMMARY_COLUMNS)
            record.update(product=product, method=method, method_name=NAMES[method], capital="1000000", scenario="BASE",
                          product_status=available["status"], dominant_status=available["status"])
            if available["status"] in ("REUSED_ACCEPTED", "SIMULATED"):
                candidates = [row for row in tables[product]["dominant-summary.csv"] if row["method"] == method
                              and row.get("capital", "1000000") == "1000000" and row.get("scenario", "BASE") == "BASE"]
                need(len(candidates) == 1, "first_screen_dominant_identity")
                selected = candidates[0]
                full = [row for row in tables[product]["sensitivity-summary.csv"] if (row["capital"], row["scenario"], row["method"], row["view"]) == ("1000000", "BASE", method, "ALL")]
                need(len(full) == 1, "first_screen_full_identity")
                equal(cell(selected["all_accounts_net"]), cell(full[0]["net_sum"]), "summary_full_anchor")
                record.update(dominant_status=selected["status"], dominant_net=selected["net"], all_accounts_net=full[0]["net_sum"],
                              dominant_positive_years=selected["positive_years"], dominant_covered_years=selected["covered_years"],
                              dominant_worst_year=selected["worst_year"], dominant_worst_year_net=selected["worst_year_net"],
                              dominant_max_drawdown=selected["max_drawdown"])
            results.append(record)
    equal(len(results), 30, "fixed_summary_size")
    return results


class ReportValues:
    def __init__(self, tables):
        self.tables, self.refs = tables, []

    def value(self, filename, row, column):
        value = cell(self.tables[filename][row][column])
        self.refs.append(dict(csv=filename, row=row + 2, column=column, value=value))
        return value or "空"


def render_report(tables, summary, availability, specs):
    all_tables = {product + "/" + name: records for product, product_tables in tables.items() for name, records in product_tables.items()}
    all_tables.update({"summary.csv": summary, "availability.csv": availability})
    values = ReportValues(all_tables)
    available_count = sum(row["new_simulation"] for row in availability)
    if available_count == len(NEW_PRODUCTS):
        opening = "五个新增品种都完成了固定条件下的日线模拟，螺纹钢沿用此前接受的结果。"
    elif available_count:
        opening = "新增品种中有已完成模拟和不可用两类，具体状态与原因为下表所列。"
    else:
        opening = "新增品种均未形成可运行的冻结输入，本轮没有完成新增研究矩阵。"
    lines = ["# 固定品种日线基准", "", opening,
        "首表使用相同初始资金和基准成本，分别列出主力归集与全部独立合约的净额。",
        "规格采用已核实的静态合约值；前根持仓量的历史可知时点仍未得到验证。",
        "主力列拼接的是各原账户当日盈亏，不是一笔资本连续执行的换月回测。",
        "结果为情景描述，统计资格未评定，也没有据此调参或建议交易。", "",
        "| 品种 | 做法 | 初始资金 | 成本 | 主力净额 | 全合约净额 | 正年 | 覆盖年 | 最差年 | 该年净额 | 最大回撤 | 状态 |",
        "| --- | --- | ---: | --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | --- |"]
    columns = ("product", "method_name", "capital", "scenario", "dominant_net", "all_accounts_net", "dominant_positive_years",
               "dominant_covered_years", "dominant_worst_year", "dominant_worst_year_net", "dominant_max_drawdown", "dominant_status")
    for i in range(len(summary)):
        lines.append("| " + " | ".join(values.value("summary.csv", i, column) for column in columns) + " |")
    lines += ["", "## 输入可用性", "", "| 品种 | 声明合约 | 有日线合约 | 无日线合约 | 日线数 | 首日 | 末日 | 状态 | 原因 |", "| --- | ---: | ---: | ---: | ---: | --- | --- | --- | --- |"]
    for i in range(len(availability)):
        lines.append("| " + " | ".join(values.value("availability.csv", i, column) for column in
            ("product", "declared_contracts", "contracts_with_bars", "contracts_without_bars", "bar_rows", "start_raw_date", "end_raw_date", "status", "reason_code")) + " |")
    lines += ["", "## 资金与成本情景", "", "下表按品种、资金、成本和方法分别列示；金额没有跨品种相加。PRIMARY 只是原账户观察索引达到预热边界后的报告遮罩，账户不会重新启动。", "",
              "| 品种 | 资金 | 成本 | 做法 | 主力净额 | 全合约净额 | 最长水下观察数 |", "| --- | ---: | --- | --- | ---: | ---: | ---: |"]
    for product in PRODUCTS:
        name = product + "/dominant-summary.csv"
        for i, row in enumerate(all_tables[name]):
            if product == "SHFE.RB" or (row["capital"], row["scenario"]) == ("1000000", "BASE"):
                continue
            lines.append("| " + " | ".join(values.value(name, i, column) for column in
                ("product", "capital", "scenario", "method_name", "net", "all_accounts_net", "longest_drawdown_observations")) + " |")
    lines += ["", "螺纹钢仅复用原有主力资金与成本单元，其他单元未重放；其全部合约敏感性保留原接受表。", "", "## 主力覆盖与限制", "",
              "| 品种 | 覆盖观察数 | 全部观察数 | 覆盖百分比 | 切换次数 |", "| --- | ---: | ---: | ---: | ---: |"]
    for product in PRODUCTS:
        name = product + "/dominant-scope.csv"
        if not all_tables[name]:
            continue
        fields = ("covered_observations", "observations", "coverage_percent", "switches")
        if product == "SHFE.RB":
            cells = [values.value(name, next(i for i, row in enumerate(all_tables[name]) if row["metric"] == field), "value") for field in fields]
        else:
            cells = [values.value(name, 0, field) for field in fields]
        lines.append("| " + product + " | " + " | ".join(cells) + " |")
    lines += ["", "观察日为输入中上海日期的并集。没有候选的观察日不填零收益，曲线仅延续已有累计值；水下缺日仍计入回撤持续观察数。选择依据同合约前一条已存日线的持仓量，今天持仓量和今天收益不参与选约。原账户终点费用保留，历史参数日程和实际可知时点均未核实。", "",
              "## 静态规格来源", "", "| 品种 | 官方来源 | 乘数 | 最小变动价位 | 本地原文 SHA-256 |", "| --- | --- | ---: | ---: | --- |"]
    source_refs = []
    for product in NEW_PRODUCTS:
        spec = specs[product]
        lines.append(f"| {product} | [官方合约]({spec['source_url']}) | {spec['multiplier']} | {spec['price_tick']} | `{spec['source']['sha256']}` |")
        source_refs.extend(dict(product=product, source="ECONOMIC-SPECS", field=field, value=spec[field]) for field in ("multiplier", "price_tick"))
    lines += ["", "这些来源是已存官方文本的精确副本，不代表本次重新联网抓取或已核实完整历史规则日程。工业硅取期货条款的价位，原抓取失败与成功回退记录保留在外部来源绑定。", ""]
    return "\n".join(lines).encode("utf-8"), values.refs, source_refs


def run_matrix(context, engine=None, engine_io=None):
    if engine is None:
        engine, engine_io, _ = context.b.load_engine(context.engine_root, context.engine_hashes, context.output)
    need(not context.output.exists(), "output_already_exists")
    context.sources.verify()
    context.output.mkdir(parents=True)
    writer = PayloadWriter(context.output)
    writer.write("config.json", json_bytes(context.config))
    tables = {"SHFE.RB": {name: rows(raw) for name, raw in context.rb.items()}}
    counts = {"SHFE.RB": dict(accounts=0, strategy_accounts=0, control_accounts=0, new_simulation=False)}
    availability = [context.rb_availability]
    columns = headers(context)
    for name, raw in context.rb.items():
        writer.write(public_path("SHFE.RB/" + name), raw)
    for product in NEW_PRODUCTS:
        if product in context.prepared:
            product_tables, product_counts = run_product(context, product, writer, engine, engine_io)
            counts[product] = dict(product_counts, new_simulation=True)
        else:
            product_tables = {name: [] for name in TABLES}
            counts[product] = dict(accounts=0, strategy_accounts=0, control_accounts=0, new_simulation=False)
        tables[product] = product_tables
        for name in TABLES:
            writer.write(public_path(product + "/" + name), csv_bytes(product_tables[name], columns[name]))
        availability.append(availability_row(product, context.inputs["products"][product], context.prepared.get(product), context.specs["products"]))
    summary = summary_table(tables, availability)
    writer.write(public_path("summary.csv"), csv_bytes(summary, SUMMARY_COLUMNS))
    writer.write(public_path("availability.csv"), csv_bytes(availability, AVAIL_COLUMNS))
    report, refs, source_refs = render_report(tables, summary, availability, context.specs["products"])
    writer.write("publish/" + REPORT, report)
    equal({name for name in writer.inventory if name.startswith("publish/")}, expected_public(), "39_pending_publish_files")
    context.sources.verify()
    manifest = dict(schema="k44-matrix-manifest/v1", status="PENDING_SEAL", payloads=writer.inventory,
        reserved_final_check_paths=final_checks(), source_bindings_sha256=context.source_hash,
        input_bindings_sha256=context.input_hash, build_bindings_sha256=context.build_hash,
        sources=context.logical_sources, build_files=context.build_pins, counts=counts,
        product_status={r["product"]: r["status"] for r in availability},
        report_value_refs=refs, report_source_refs=source_refs, **MODE, safety=SAFETY)
    write_new(context.output / "manifest.json", json_bytes(manifest))
    return manifest


def expected_inventory(context):
    expected = expected_public() | {"config.json"}
    for product, prepared in context.prepared.items():
        prefix = "products/" + product + "/"
        expected.update(prefix + name for name in ("selection.csv", "selection-candidates.json", "timestamp-map.csv",
                                                   "account-index.csv", "contract-year.csv", "dominant-curves.csv"))
        for symbol in prepared.bars:
            instance = prepared.declarations[symbol]["instance_id"]
            slug = symbol + "-" + hashlib.sha256(instance.encode()).hexdigest()[:16]
            for capital in CAPITALS:
                for scenario in SCENARIOS:
                    for method in METHODS:
                        account = prefix + "accounts/C" + capital + "/" + scenario + "/" + slug + "/" + method + "/"
                        expected.update(account + name for name in ("config.json", "report.json", "fills.csv", "equity.csv", "decisions.csv", "ledger.csv"))
    return expected


def validate_inventory(context, manifest):
    equal(manifest["schema"], "k44-matrix-manifest/v1", "matrix_manifest_schema")
    equal(manifest["status"], "PENDING_SEAL", "matrix_manifest_pending")
    equal(manifest["reserved_final_check_paths"], final_checks(), "reserved_final_checks")
    expected = expected_inventory(context)
    equal(set(manifest["payloads"]), expected, "complete_expected_payload_inventory")
    actual = {p.relative_to(context.output).as_posix() for p in context.output.rglob("*") if p.is_file()}
    equal(actual, expected | {"manifest.json"}, "actual_payload_set_or_existing_check")
    for relative, record in manifest["payloads"].items():
        equal(pin(child(context.output, relative).read_bytes()), record, "payload_hash:" + relative)
    for name, expected_hash in (("source", context.source_hash), ("input", context.input_hash), ("build", context.build_hash)):
        equal(manifest[name + "_bindings_sha256"], expected_hash, "manifest_binding_hash")
    equal(manifest["build_files"], context.build_pins, "manifest_build_pins")
    equal(manifest["sources"], context.logical_sources, "manifest_sources")
    for product in NEW_PRODUCTS:
        if product in context.prepared:
            support = context.prepared[product].stats
            counts = manifest["counts"][product]
            equal(counts["accounts"], support["contracts_with_bars"] * 80, "manifest_expected_accounts")
            equal(counts["strategy_accounts"], support["contracts_with_bars"] * 48, "manifest_strategy_accounts")
            equal(counts["control_accounts"], support["contracts_with_bars"] * 32, "manifest_control_accounts")
            equal(counts["ALL_observations"], support["bar_rows"] * 80, "manifest_all_observations")
            equal(counts["contract_year_cells"], counts["accounts"] * 14, "manifest_year_cells")
            for filename, count in zip(TABLES, (160, 1120, 1536, 80, 560, 1)):
                equal(len(rows(child(context.output, public_path(product + "/" + filename)).read_bytes())), count, "public_product_row_count")
        else:
            equal(manifest["counts"][product]["accounts"], 0, "unavailable_account_count")
            for filename in TABLES:
                equal(rows(child(context.output, public_path(product + "/" + filename)).read_bytes()), [], "unavailable_empty_table")
    for filename in TABLES:
        equal(child(context.output, public_path("SHFE.RB/" + filename)).read_bytes(), context.rb[filename], "RB_reuse_bytes")
    equal(len(rows(child(context.output, public_path("summary.csv")).read_bytes())), 30, "summary_rows")
    equal(len(rows(child(context.output, public_path("availability.csv")).read_bytes())), 6, "availability_rows")
    for ref in manifest["report_value_refs"]:
        records = rows(child(context.output, public_path(ref["csv"])).read_bytes())
        equal(records[ref["row"] - 2][ref["column"]], ref["value"], "report_cell_mapping")


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    sub = parser.add_subparsers(dest="mode", required=True)
    for mode in ("matrix", "seal"):
        command = sub.add_parser(mode, allow_abbrev=False)
        for name in ("source", "input", "build"):
            command.add_argument("--" + name + "-bindings", required=True)
            command.add_argument("--" + name + "-bindings-sha256", required=True)
        if mode == "matrix":
            command.add_argument("--source-repo", required=True)
            command.add_argument("--output-dir", required=True)
        else:
            command.add_argument("--matrix-dir", required=True)
            command.add_argument("--matrix-receipt", required=True)
    tokens = list(argv)
    options = [token.partition("=")[0] for token in tokens if token.startswith("--")]
    if len(options) != len(set(options)):
        parser.error("duplicate options are forbidden")
    return parser.parse_args(tokens)


def verify_matrix_receipt(context, receipt, manifest_raw):
    equal(receipt["schema"], "k44-matrix-terminal/v1", "matrix_receipt_schema")
    equal(receipt["raw_exit_subject"], "matrix_child", "matrix_receipt_subject")
    need(receipt["raw_exit"] == 0 and receipt["terminated"] is True and receipt["streams_closed"] is True
         and receipt["timed_out"] is False, "matrix_child_not_successful_terminal")
    need(integer(receipt["pid"]) > 0 and isinstance(receipt["task_id"], str) and receipt["task_id"], "matrix_process_identity")
    started, ended = datetime.fromisoformat(receipt["started_utc"]), datetime.fromisoformat(receipt["ended_utc"])
    need(started.utcoffset() is not None and ended.utcoffset() is not None and ended >= started, "matrix_terminal_times")
    equal(path(receipt["output_dir"]), context.output, "matrix_receipt_output")
    equal(receipt["manifest"], pin(manifest_raw), "matrix_receipt_manifest")
    for name, digest in (("source", context.source_hash), ("input", context.input_hash), ("build", context.build_hash)):
        equal(receipt[name + "_bindings_sha256"], digest, "receipt_binding")
    argv = receipt["argv"]
    need(isinstance(argv, list) and len(argv) > 5, "receipt_command_argv")
    positions = [i for i, token in enumerate(argv) if Path(token).name == Path(TOOL).name]
    need(len(positions) == 1, "receipt_driver_identity")
    index = positions[0]
    cwd = path(receipt["cwd"])
    command_path = Path(argv[index])
    command_path = command_path if command_path.is_absolute() else cwd / command_path
    equal(path(command_path), child(context.repo, TOOL), "receipt_driver_location")
    args = parse_args(argv[index + 1:])
    equal(args.mode, "matrix", "receipt_command_mode")
    equal(path(args.source_repo), context.repo, "receipt_source_repo")
    equal(path(args.output_dir), context.output, "receipt_command_output")
    for name, digest in (("source", context.source_hash), ("input", context.input_hash), ("build", context.build_hash)):
        equal(getattr(args, name + "_bindings_sha256"), digest, "receipt_command_binding_hash")
        filename = path(getattr(args, name + "_bindings"))
        equal(pin(filename.read_bytes())["sha256"], digest, "receipt_command_binding_bytes")
    need(set(receipt["closed_streams"]) == {"stdout", "stderr"}, "closed_stream_set")
    for record in receipt["closed_streams"].values():
        filename = path(record["path"])
        need(not filename.is_relative_to(context.output), "stream_overlaps_payload")
        equal(pin(filename.read_bytes()), {k: record[k] for k in ("bytes", "sha256")}, "closed_stream_hash")
    return dict(schema="k44-verified-matrix-terminal-projection/v1", raw_exit_subject="matrix_child", raw_exit=0,
                terminated=True, streams_closed=True, matrix_manifest_sha256=pin(manifest_raw)["sha256"],
                source_bindings_sha256=context.source_hash, input_bindings_sha256=context.input_hash,
                build_bindings_sha256=context.build_hash, sources=context.logical_sources, build_files=context.build_pins)


def seal(context, receipt):
    raw_manifest = (context.output / "manifest.json").read_bytes()
    manifest = loads(raw_manifest)
    context.sources.verify()
    validate_inventory(context, manifest)
    logical_receipt = verify_matrix_receipt(context, receipt, raw_manifest)
    checks = {}
    for product in PRODUCTS:
        new_simulation = manifest["counts"][product]["new_simulation"]
        payloads = {name: record for name, record in manifest["payloads"].items()
                    if name.startswith(public_path(product + "/")) or name.startswith("products/" + product + "/")}
        value = dict(schema="k44-product-check/v1", product=product, status="SEALED_MATRIX_EXIT_0",
                     product_status=manifest["product_status"][product], new_simulation=new_simulation,
                     counts=manifest["counts"][product], payloads=payloads, source_bindings_sha256=context.source_hash,
                     input_bindings_sha256=context.input_hash, build_bindings_sha256=context.build_hash,
                     build_files=context.build_pins, engine_sources={k: v for k, v in context.logical_sources.items() if k.startswith("engine.")},
                     matrix_receipt=logical_receipt, **MODE, safety=SAFETY)
        if product != "SHFE.RB":
            spec = context.specs["products"][product]
            value["economic_source"] = {k: spec["source"][k] for k in ("bytes", "sha256")}
            value["economic_assumptions"] = context.config["economics"][product]
            entry = context.inputs["products"][product]
            value["input_status"] = entry["status"]
            value["input_reason_code"] = entry["reason_code"]
            value["export_raw_exit"] = entry["export_raw_exit"]
        else:
            value["reuse_only"] = True
            value["engine_calls"] = 0
        checks[public_path(product + "/CHECK.json")] = json_bytes(value)
    simulated = [p for p in NEW_PRODUCTS if manifest["counts"][p]["new_simulation"]]
    root = dict(schema="k44-universe-check/v1", status="SEALED_MATRIX_EXIT_0",
        coverage_status="COMPLETE_NEW_PRODUCT_COVERAGE" if len(simulated) == 5 else "PARTIAL_NEW_PRODUCT_COVERAGE" if simulated else "BLOCKED",
        new_simulation=bool(simulated), simulated_products=simulated, reused_products=["SHFE.RB"],
        product_status=manifest["product_status"], counts=manifest["counts"], matrix_receipt=logical_receipt,
        source_bindings_sha256=context.source_hash, input_bindings_sha256=context.input_hash, build_bindings_sha256=context.build_hash,
        build_files=context.build_pins, report_value_refs=manifest["report_value_refs"], report_source_refs=manifest["report_source_refs"],
        payloads={name: record for name, record in manifest["payloads"].items() if name.startswith("publish/")}
                 | {name: pin(raw) for name, raw in checks.items()}, **MODE, safety=SAFETY)
    checks[public_path("CHECK.json")] = json_bytes(root)
    equal(list(checks), final_checks(), "final_check_publish_order")
    for raw in checks.values():
        loads(raw)
    context.sources.verify()
    for relative, raw in checks.items():
        atomic_new(child(context.output, relative), raw)
    return root


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        with localcontext() as ctx:
            ctx.prec = 128
            ctx.traps[Inexact] = True
            if args.mode == "matrix":
                repo, output = args.source_repo, args.output_dir
                receipt = None
            else:
                receipt = loads(path(args.matrix_receipt).read_bytes())
                positions = [i for i, token in enumerate(receipt["argv"]) if token == "--source-repo"]
                need(len(positions) == 1, "receipt_source_repo_argument")
                repo, output = receipt["argv"][positions[0] + 1], args.matrix_dir
            context = preflight(repo, args.source_bindings, args.source_bindings_sha256,
                                args.input_bindings, args.input_bindings_sha256,
                                args.build_bindings, args.build_bindings_sha256, output, sealing=args.mode == "seal")
            result = run_matrix(context) if args.mode == "matrix" else seal(context, receipt)
            print(json.dumps(dict(status=result["status"], mode=args.mode), sort_keys=True), flush=True)
        return 0
    except (ValidationError, ValueError, KeyError, OSError, ArithmeticError, TypeError) as exc:
        # Inputs have no connection strings; still avoid interpolating arbitrary exception text.
        reason = str(exc) if isinstance(exc, ValidationError) else type(exc).__name__
        print(json.dumps(dict(status="FAILED", mode=args.mode, reason=reason), sort_keys=True), file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
