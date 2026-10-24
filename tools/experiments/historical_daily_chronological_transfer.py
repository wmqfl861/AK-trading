"""Describe raw-year increments of independent, fixed RB daily SCENARIO accounts.

Every account is generated once by the unchanged single-contract engine. Years
and the j >= 60 support view attribute its validated ledger; neither changes a
trajectory. Calendar, execution and economic assumptions remain unverified.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict
import csv
from datetime import datetime, timedelta
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
import io
import json
import math
from pathlib import Path
import re
import stat
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "src"))

from ak_trading.futures.io import (  # noqa: E402
    OutputDirectoryError, assert_output_directory_is_new, canonical_json_bytes,
    read_input_bundle, sha256_bytes, sha256_file,
)
from ak_trading.futures.scenario_backtest import ScenarioConfig, run_history_scenario  # noqa: E402
from ak_trading.futures.strategies import DonchianTrend, MATrend, ZScoreReversion  # noqa: E402

ARITHMETIC = Context(prec=128, rounding=ROUND_HALF_EVEN)
D = Decimal
YEARS = tuple(range(2020, 2027))
VIEWS = ("ALL", "PRIMARY")
STRATEGIES = ("donchian_trend", "ma_trend", "zscore_reversion")
PARAMETERS = (
    {"entry_window": 40, "exit_window": 20},
    {"fast_window": 20, "slow_window": 60},
    {"lookback": 20, "entry_z": 2.0, "exit_z": 0.5, "stop_z": 4.0, "max_signal_age": 20},
)
REQUIRED = (41, 60, 21)
PAIRS = ((STRATEGIES[0], STRATEGIES[1]), (STRATEGIES[0], STRATEGIES[2]),
         (STRATEGIES[1], STRATEGIES[2]))
BASE = {"initial_cash": "1000000", "lots": 1, "multiplier": "10", "price_tick": "1",
        "fee_per_lot": "0.01", "fee_rate": "0.0001", "slippage_ticks": "1",
        "margin_rate": "0.12", "max_margin_ratio": "0.4"}
PLAN_DIRECTORY = ROOT / "docs/strategy-research-blueprint-20261010-r2/implementation-clarifications/rb-daily-chronological-transfer-20261014-r1"
PLAN_FILE = PLAN_DIRECTORY / "PLAN-F01-20261015-r2.md"
PLAN_HASH = "a1c51e2bf8b94b6a5e4859c9e74e101ac1aa2669e2f1d207b23a1c7cd6b042e7"
INPUTS_FILE = PLAN_DIRECTORY / "INPUTS.json"
INPUTS_HASH = "9d331675c899e7b04e915e6731ebef694741491d2c0460b3dbfee180ecc16a70"
CLARIFICATION_FILE = PLAN_DIRECTORY / "CLARIFICATION-IMPLEMENTATION-20261015-r3.md"
CLARIFICATION_HASH = "0ec3ddb50edd712f0629ff472062e4a49c3340b3cac60a9aa192bb0b3098ec8c"
DISPOSITION_FILE = PLAN_DIRECTORY / "DISPOSITION-IMPLEMENTATION-20261015-r3.md"
DISPOSITION_HASH = "7421a9b15d7bfbdf2a9cb468635e6025595a2d07dbe3d37892a71621bc014507"
LEGACY_MANIFEST = ROOT / "data/research_runs/project-review-20260908-rb2101-ma-final/manifest.json"
SELF = "tools/experiments/historical_daily_chronological_transfer.py"
SIBLING_REFERENCE = "tools/experiments/historical_daily_scenario_compare.py"
CORE = ("bars.csv", "contracts.json", "metadata.json", "availability.json")
CHILD_FILES = ("config.json", "fills.csv", "equity.csv", "decisions.csv", "report.json")
CALENDAR = "UNAVAILABLE_VERIFIED_TRADING_CALENDAR"
SAFETY = {"evidence_level": "SCENARIO", "historical_strict_execution": False,
          "assumptions_verification": "not_historical_verified", "broker": "NOT_RUN",
          "enable_trading": False, "real_order_send_calls": 0, "real_order_cancel_calls": 0}
CHILD_SAFETY = {"evidence_level": "SCENARIO", "historical_strict_execution": False,
                "assumptions_verification": "not_historical_verified", "broker": "NOT_RUN",
                "enable_trading": False, "real_send_count": 0, "real_cancel_count": 0}
CHRONOLOGY = {
    "raw_years": list(YEARS), "calendar_status": CALENDAR,
    "raw_date_field": "bars.csv.ts", "trading_day_field": "ignored_for_blocking",
    "available_at_field": "provenance_only", "year_assignment": "local_raw_ts_year",
    "raw_timestamp_semantics": "timezone-aware local raw_ts with explicit +08:00 offset",
    "year_boundaries": "inclusive January 1 through December 31 of the raw local year",
    "boundary_rule": "each stored row belongs to its raw_ts year; no drop, duplication, shift, synthesis, boundary close or cash/state reset",
    "primary_mask": "zero_based_observation_index >= 60",
    "replay_mode": "one_full_contract_replay_per_strategy",
    "year_status": {str(y): "PARTIAL_INPUT_WINDOW" if y in (2020, 2026) else "OBSERVED_RAW_YEAR" for y in YEARS},
}
DAILY_COLUMNS = (
    "symbol", "contract_instance_id", "strategy", "j", "datetime_nano", "raw_ts", "raw_date",
    "raw_year", "year_status", "partial_year", "primary_support", "terminal", "position_lots",
    "cash", "realized_pnl", "unrealized_pnl", "equity", "previous_equity", "equity_delta",
    "gross_pnl", "gross_delta", "fees", "fee_delta", "slippage", "slippage_delta", "net_pnl",
    "net_delta", "fills_at_j", "terminal_close_fills", "candidate_direction", "strategy_status",
    "reason", "execution_status", "margin_breached", "child_directory",
)
YEAR_COLUMNS = (
    "symbol", "contract_instance_id", "strategy", "raw_year", "view", "status", "temporal_class",
    "year_status", "calendar_status", "partial_year", "observations", "raw_dates", "fills",
    "terminal_close_fills", "first_j", "last_j", "first_raw_ts", "last_raw_ts", "previous_equity",
    "end_equity", "year_net_delta", "year_gross_delta", "year_fee_delta", "year_slippage_delta",
    "carry_status", "position_before_first_row", "undefined_reason", "child_directory",
)
SUM_COLUMNS = (
    "raw_year", "strategy", "view", "status", "year_status", "calendar_status", "partial_year",
    "contract_year_cells", "distinct_contracts", "observations", "raw_dates", "fills",
    "terminal_close_fills", "matched_pair_cells", "independent_account_descriptive_net_sum",
    "independent_account_descriptive_gross_sum", "independent_account_descriptive_fee_sum",
    "independent_account_descriptive_slippage_sum", "contract_year_net_min", "contract_year_net_median",
    "contract_year_net_max", "positive_contract_years", "zero_contract_years", "negative_contract_years",
    "undefined_reason",
)
PAIR_COLUMNS = (
    "symbol", "contract_instance_id", "j", "datetime_nano", "raw_ts", "raw_date", "raw_year", "view",
    "strategy_a", "strategy_b", "status", "net_delta_a", "net_delta_b", "net_delta_a_minus_b",
    "gross_delta_a", "gross_delta_b", "gross_delta_a_minus_b", "fee_delta_a", "fee_delta_b",
    "fee_delta_a_minus_b", "slippage_delta_a", "slippage_delta_b", "slippage_delta_a_minus_b",
    "equity_a", "equity_b", "equity_a_minus_b",
)
PAIR_SUM_COLUMNS = (
    "raw_year", "view", "strategy_a", "strategy_b", "status", "year_status", "calendar_status",
    "matched_observations", "matched_contract_years", "distinct_contracts", "raw_dates",
    "unmatched_observations", "independent_account_descriptive_net_difference_sum",
    "independent_account_descriptive_gross_difference_sum", "independent_account_descriptive_fee_difference_sum",
    "independent_account_descriptive_slippage_difference_sum", "contract_year_difference_min",
    "contract_year_difference_median", "contract_year_difference_max", "positive_contract_years",
    "zero_contract_years", "negative_contract_years", "undefined_reason",
)
FILL_COLUMNS = ("fill_id", "observation_index", "signal_index", "datetime_nano", "raw_ts", "phase",
                "offset", "side", "lots", "ideal_price", "effective_price", "fee_per_lot_component",
                "fee_rate_component", "fee", "slippage", "realized_pnl", "cash_after", "position_after")
EQUITY_COLUMNS = ("observation_index", "datetime_nano", "raw_ts", "close", "position_lots", "cash",
                  "realized_pnl", "unrealized_pnl", "equity", "daily_gross_pnl", "gross_pnl", "fees",
                  "slippage", "net_pnl", "required_margin", "available_equity", "margin_breached",
                  "drawdown", "drawdown_ratio", "terminal")
DECISION_COLUMNS = ("observation_index", "datetime_nano", "raw_ts", "decision_at", "clock_status",
                    "strategy_status", "reason", "evaluator_candidate_direction", "candidate_direction",
                    "target_lots", "logical_direction", "signal_age", "preceding_signal_index",
                    "attempted_target_lots", "execution_status", "opening_required_margin",
                    "opening_equity_after_costs", "final_observation")


def _text(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("nonfinite accounting value")
    if value == 0:
        return "0"
    result = format(value, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def _decimal(value: object) -> Decimal:
    if not isinstance(value, (str, Decimal)) or isinstance(value, str) and len(value) > 512:
        raise ValueError("accounting amounts must be finite Decimal strings")
    number = D(value)
    if not number.is_finite() or abs(number.adjusted()) > 256:
        raise ValueError("nonfinite or unbounded accounting amount")
    return number


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)) or re.fullmatch(r"-?(?:0|[1-9][0-9]*)", str(value)) is None:
        raise ValueError("invalid integer ledger field")
    return int(value)


def _boolean(value: str) -> bool:
    if value not in ("true", "false"):
        raise ValueError("invalid boolean ledger field")
    return value == "true"


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _nonfinite(value):
    raise ValueError(f"nonfinite JSON number: {value}")


def _finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        _nonfinite(value)
    return result


def _json(payload: bytes, *, decimal_floats=False):
    return json.loads(payload.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_nonfinite,
                      parse_float=D if decimal_floats else _finite_float)


def _object(payload: bytes, *, decimal_floats=False) -> dict:
    result = _json(payload, decimal_floats=decimal_floats)
    if not isinstance(result, dict):
        raise ValueError("JSON document must be an object")
    return result


def _safe_path(path: Path, *, output=False) -> Path:
    path = Path(path)
    if str(path).startswith(("\\\\?\\", "\\\\.\\")):
        raise ValueError("device namespaces are unsupported")
    for part in path.parts:
        stem = part.rstrip(" .").split(".", 1)[0].upper()
        if stem in {"CON", "NUL", "AUX", "PRN", "CONIN$", "CONOUT$"} or re.fullmatch(r"(?:COM|LPT)[1-9]", stem):
            raise ValueError("reserved device paths are unsupported")
        if part != path.anchor and ":" in part:
            raise ValueError("alternate stream paths are unsupported")
        if part.lower() == ".env" or part.lower().startswith(".env."):
            raise ValueError("credential paths are outside the offline contract")
    if output:
        for component in (path.absolute(), *path.absolute().parents):
            try:
                info = component.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise OutputDirectoryError("output components may not be symlinks or reparse points")
    return path.resolve()


def _equal(actual, expected, name):
    if actual != expected:
        raise ValueError(f"{name} mismatch")


def _typed_equal(actual, expected, name):
    if type(actual) is not type(expected):
        raise ValueError(f"{name} type mismatch")
    if isinstance(expected, dict):
        _equal(set(actual), set(expected), f"{name} fields")
        for key, value in expected.items():
            _typed_equal(actual[key], value, f"{name}.{key}")
    elif isinstance(expected, list):
        _equal(len(actual), len(expected), f"{name} length")
        for index, value in enumerate(expected):
            _typed_equal(actual[index], value, f"{name}[{index}]")
    else:
        _equal(actual, expected, name)


def _source_pins():
    for path, expected in ((PLAN_FILE, PLAN_HASH), (INPUTS_FILE, INPUTS_HASH),
                           (CLARIFICATION_FILE, CLARIFICATION_HASH), (DISPOSITION_FILE, DISPOSITION_HASH)):
        if not path.is_file() or sha256_file(path) != expected:
            raise ValueError(f"effective plan/input inventory pin mismatch: {path.name}")
    inventory = _object(INPUTS_FILE.read_bytes())
    legacy_pin = next(row["sha256"] for row in inventory["required_input_pins"]
                      if row["path"] == LEGACY_MANIFEST.as_posix())
    _equal(sha256_file(LEGACY_MANIFEST), legacy_pin, "legacy source manifest hash")
    legacy = _object(LEGACY_MANIFEST.read_bytes())["code_hashes"]
    _equal(legacy, inventory["engine_interface"]["legacy_code_hashes"], "legacy source inventory")
    _equal(len(legacy), 10, "legacy source count")
    for name, expected in legacy.items():
        _equal(sha256_file(ROOT / name), expected, f"legacy source {name}")
    sibling_pin = next(row["sha256"] for row in inventory["required_input_pins"]
                       if row["path"] == (ROOT / SIBLING_REFERENCE).as_posix())
    _equal(sha256_file(ROOT / SIBLING_REFERENCE), sibling_pin, "read-only sibling reference")
    for cls, parameters, required in zip((DonchianTrend, MATrend, ZScoreReversion), PARAMETERS, REQUIRED):
        strategy = cls()
        _equal(asdict(strategy), parameters, "fixed strategy parameters")
        _equal(strategy.spec.required_bars, required, "fixed strategy warmup")
    snapshot = {PLAN_FILE: PLAN_HASH, INPUTS_FILE: INPUTS_HASH, CLARIFICATION_FILE: CLARIFICATION_HASH,
                DISPOSITION_FILE: DISPOSITION_HASH, LEGACY_MANIFEST: legacy_pin,
                ROOT / SIBLING_REFERENCE: sibling_pin, ROOT / SELF: sha256_file(ROOT / SELF)}
    snapshot.update({ROOT / name: digest for name, digest in legacy.items()})
    return inventory, legacy, snapshot


def _capture(source: Path, base_file: Path):
    if not source.is_dir() or not base_file.is_file():
        raise ValueError("required bundle directory or BASE file does not exist")
    if base_file.stat().st_size > 16_384:
        raise ValueError("BASE config exceeds 16 KiB")
    base_bytes = base_file.read_bytes()
    config = ScenarioConfig.from_mapping(_object(base_bytes, decimal_floats=True))
    _equal(config.to_dict(), BASE, "registered fixed BASE assumptions")
    for name in ("manifest.json", *CORE):
        if not (source / name).is_file():
            raise ValueError(f"required input missing: {name}")
    manifest_bytes = (source / "manifest.json").read_bytes()
    manifest = _object(manifest_bytes)
    if (manifest.get("schema_version") != "futures_bundle_v1" or manifest.get("status") != "complete"
            or manifest.get("dataset_kind") != "real_export"):
        raise ValueError("requires a complete real_export futures_bundle_v1 schema")
    declared = manifest.get("content_hashes")
    if not isinstance(declared, dict) or not set(CORE) <= set(declared):
        raise ValueError("manifest must declare all four core content hashes")
    snapshot = {source / "manifest.json": sha256_bytes(manifest_bytes), base_file: sha256_bytes(base_bytes)}
    for name, expected in declared.items():
        if (not isinstance(name, str) or name in ("", ".", "..", "manifest.json")
                or any(c in name for c in "/\\:") or not isinstance(expected, str)
                or re.fullmatch("[0-9a-f]{64}", expected) is None):
            raise ValueError("invalid input payload hash declaration")
        path = _safe_path(source / name)
        if path.parent != source or not path.is_file():
            raise ValueError("required payload absent or outside bundle")
        payload = path.read_bytes()
        _equal(sha256_bytes(payload), expected, f"input hash {name}")
        snapshot[path] = expected
        if name == "contracts.json":
            rows = _json(payload)
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError("contracts.json must contain contract objects")
        elif name in ("metadata.json", "availability.json"):
            _object(payload)
    bundle = read_input_bundle(source)
    _equal(bundle.manifest, manifest, "captured input manifest")
    _check_snapshot(snapshot)
    return bundle, config, base_bytes, snapshot


def _check_snapshot(snapshot):
    for path, expected in snapshot.items():
        _equal(sha256_file(path), expected, f"frozen input/source {path.name}")


def _audit_population(bundle, config):
    declarations, instances = {}, set()
    for row in bundle.contracts:
        symbol = row.get("raw_symbol")
        if row.get("product") != "SHFE.RB":
            if isinstance(symbol, str) and re.fullmatch(r"SHFE\.rb[0-9]{4}", symbol):
                raise ValueError("RB raw symbol has a conflicting product declaration")
            continue
        if not isinstance(symbol, str) or re.fullmatch(r"SHFE\.rb[0-9]{2}(?:0[1-9]|1[0-2])", symbol) is None:
            raise ValueError("RB declaration must name a concrete SHFE.rb future")
        try:
            expiry = datetime.fromisoformat(row["expiry_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("RB requires timezone-aware expiry_at") from exc
        if expiry.utcoffset() is None or row.get("instance_id") != f"{symbol}|{expiry.isoformat()}":
            raise ValueError("RB full-instance identity mismatch")
        if symbol in declarations or row["instance_id"] in instances:
            raise ValueError("ambiguous RB symbol or full instance")
        if sum(other.get("raw_symbol") == symbol or other.get("instance_id") == row["instance_id"]
               for other in bundle.contracts) != 1:
            raise ValueError("RB identity is duplicated in the mixed bundle")
        if any(key in row and row[key] != "FUTURE" for key in ("asset_class", "instrument_class", "ins_class")):
            raise ValueError("RB declaration is not a future")
        declarations[symbol] = row
        instances.add(row["instance_id"])
    if not declarations:
        raise ValueError("bundle has no concrete RB population")
    bars = {symbol: [] for symbol in declarations}
    all_counts = Counter()
    for bar in bundle.bars:
        all_counts[bar.symbol] += 1
        if bar.symbol not in bars:
            if bar.symbol.startswith("SHFE.rb"):
                raise ValueError("RB bar has no declared full instance")
            continue
        if (isinstance(bar.datetime_nano, bool) or not isinstance(bar.datetime_nano, int)
                or bar.datetime_nano <= 0 or bar.duration_seconds != 86400
                or bar.ts.utcoffset() != timedelta(hours=8) or bar.ts.year not in YEARS):
            raise ValueError("RB rows require positive raw identity, daily duration and +08:00 raw year 2020..2026")
        for field in ("open", "high", "low", "close"):
            value = getattr(bar, field)
            if (not isinstance(value, D) or not value.is_finite() or not D("0.000000000001") <= value <= D("1000000000000")
                    or value.as_tuple().exponent < -12 or len(value.as_tuple().digits) > 28):
                raise ValueError("invalid RB Decimal OHLC")
        if not bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high:
            raise ValueError("invalid RB OHLC envelope")
        if min(bar.open, bar.close) <= config.price_tick * config.slippage_ticks:
            raise ValueError("slippage permits a nonpositive effective sell price")
        bars[bar.symbol].append(bar)
    dates, year_counts, year_dates = set(), Counter(), defaultdict(set)
    records = []
    for symbol in sorted(declarations):
        ordered = tuple(sorted(bars[symbol], key=lambda bar: bar.datetime_nano))
        if not ordered or len(ordered) > 200_000:
            raise ValueError("each declared RB instance requires stored bars")
        if any(a.datetime_nano >= b.datetime_nano or a.ts >= b.ts for a, b in zip(ordered, ordered[1:])):
            raise ValueError("duplicate or contradictory RB observation ordering")
        bars[symbol] = ordered
        for bar in ordered:
            dates.add(bar.ts.date().isoformat())
            year_counts[bar.ts.year] += 1
            year_dates[bar.ts.year].add(bar.ts.date().isoformat())
        records.append({"symbol": symbol, "contract_instance_id": declarations[symbol]["instance_id"],
                        "identity_status": declarations[symbol].get("identity_status", "unverified"),
                        "observations": len(ordered), "primary_observations": max(0, len(ordered) - 60),
                        "coverage_status": "GE_60_OBSERVATIONS" if len(ordered) >= 60 else "SHORT_LT_60",
                        "first_raw_ts": ordered[0].ts.isoformat(), "last_raw_ts": ordered[-1].ts.isoformat(),
                        "year_observations": {str(y): sum(b.ts.year == y for b in ordered) for y in YEARS}})
    observed = {"all_contract_declarations": len(bundle.contracts), "all_bar_rows": len(bundle.bars),
                "rb_declarations": len(records), "rb_symbols_with_bars": len(records),
                "rb_bars": sum(r["observations"] for r in records), "rb_distinct_raw_dates": len(dates),
                "rb_common_coverage_contracts_ge_60": sum(r["observations"] >= 60 for r in records),
                "rb_short_contracts_lt_60": sum(r["observations"] < 60 for r in records),
                "rb_observations_max": max(r["observations"] for r in records),
                "rb_observations_min": min(r["observations"] for r in records),
                "non_rb_declarations_excluded": len(bundle.contracts) - len(records),
                "non_rb_bars_excluded": len(bundle.bars) - sum(r["observations"] for r in records),
                "rb_raw_ts_min": min(r["first_raw_ts"] for r in records),
                "rb_raw_ts_max": max(r["last_raw_ts"] for r in records),
                "rb_year_bar_counts": {str(y): year_counts[y] for y in YEARS},
                "rb_year_raw_date_counts": {str(y): len(year_dates[y]) for y in YEARS},
                "short_contracts": [{"symbol": r["symbol"], "observations": r["observations"]} for r in records if r["observations"] < 60]}
    synthetic = bundle.metadata.get("evidence_level") == "SYNTHETIC"
    if synthetic:
        if any("SYNTHETIC" not in bar.source for rows in bars.values() for bar in rows):
            raise ValueError("SYNTHETIC fixtures require explicitly labelled price sources")
    expected = {"child_calls": len(records) * 3, "daily_rows": observed["rb_bars"] * 3,
                "primary_rows": sum(r["primary_observations"] for r in records) * 3,
                "contract_year_cells": len(records) * 3 * 7 * 2, "year_summary_rows": 42,
                "paired_summary_rows": 42,
                "paired_observation_rows": (observed["rb_bars"] + sum(r["primary_observations"] for r in records)) * 3}
    universe = [{"symbol": r["symbol"], "contract_instance_id": r["contract_instance_id"]} for r in records]
    classes = [{"declaration": dict(row), "classification": "INCLUDED_RB" if row.get("raw_symbol") in bars else "EXCLUDED_NON_RB",
                "stored_observations": all_counts[row.get("raw_symbol")]} for row in bundle.contracts]
    coverage = {**SAFETY, "schema_version": "rb_chronological_coverage_v1", "calendar_status": CALENDAR,
                "data_kind": "SYNTHETIC" if synthetic else "FROZEN_REAL_EXPORT",
                "base_parameter_fixture": "SYNTHETIC_NOT_HISTORICAL_VERIFICATION", **observed,
                "contracts": records, "declaration_classifications": classes,
                "symbol_classifications": [{"symbol": symbol, "observations": count,
                                            "classification": "INCLUDED_RB" if symbol in bars else "EXCLUDED_NON_RB"}
                                           for symbol, count in sorted(all_counts.items())],
                "expected_counts": expected, "universe_sha256": sha256_bytes(canonical_json_bytes(universe)),
                "chronology": CHRONOLOGY, "strategy_order": list(STRATEGIES),
                "required_bars": dict(zip(STRATEGIES, REQUIRED)),
                "raw_gaps": "not_filled_or_inferred_as_trading_sessions"}
    return coverage, bars


def _require_frozen_real_input(bundle, coverage, inventory):
    # The production entry has no fixture mode or count override. Small
    # SYNTHETIC cases exercise the internal audit/attribution/publisher seams.
    if coverage["data_kind"] != "FROZEN_REAL_EXPORT":
        raise ValueError("production requires the pinned real-input-02; SYNTHETIC is test-only")
    _equal(bundle.root, Path(inventory["bundle"]["path"]).resolve(), "original mixed input directory")
    for key, expected in inventory["observed_coverage"].items():
        if key in coverage:
            _equal(coverage[key], expected, f"real population {key}")
    _equal(sha256_file(bundle.root / "manifest.json"), inventory["bundle"]["manifest_sha256"], "real input manifest pin")


def _publication_scope(source, base_file, bundle, config, base_bytes, coverage, bars, snapshot):
    """Recompute captured facts; private fixture cases never gain real acceptance."""
    _check_snapshot(snapshot)
    fresh_bundle, fresh_config, fresh_bytes, fresh_snapshot = _capture(source, base_file)
    fresh_coverage, fresh_bars = _audit_population(fresh_bundle, fresh_config)
    for actual, expected, name in ((bundle, fresh_bundle, "bundle"), (config, fresh_config, "BASE"),
                                   (base_bytes, fresh_bytes, "BASE bytes"), (bars, fresh_bars, "bars"),
                                   ({k: v for k, v in coverage.items() if k not in
                                     ("evaluation_scope", "production_acceptance", "child_provenance")},
                                    fresh_coverage, "coverage")):
        _equal(actual, expected, f"recomputed publication {name}")
    for path, expected in fresh_snapshot.items():
        _equal(snapshot.get(path), expected, "captured publication input pin")
    inventory = _object(INPUTS_FILE.read_bytes())
    if source == Path(inventory["bundle"]["path"]).resolve():
        _require_frozen_real_input(fresh_bundle, fresh_coverage, inventory)
        return {"evaluation_scope": "FROZEN_REAL_INPUT_SCENARIO", "production_acceptance": True}
    if fresh_bundle.metadata.get("evidence_level") != "SYNTHETIC" or fresh_coverage["data_kind"] != "SYNTHETIC":
        raise ValueError("private publication requires an explicitly SYNTHETIC test case")
    return {"evaluation_scope": "SYNTHETIC_TEST_ONLY", "production_acceptance": False}


def _csv_rows(payload, columns):
    reader = csv.reader(io.StringIO(payload.decode("utf-8"), newline=""))
    if next(reader, None) != list(columns):
        raise ValueError("unexpected child CSV header")
    result = []
    for row in reader:
        if len(row) != len(columns):
            raise ValueError("malformed child CSV row")
        result.append(dict(zip(columns, row)))
    return result


def _child_record(relative, record, strategy, payloads, input_hash, config_hash, input_content_hashes,
                  legacy_hashes, wrapper_hash):
    return {"raw_symbol": record["symbol"], "contract_instance_id": record["contract_instance_id"],
            "strategy": strategy, "child_directory": f"{relative}/",
            "child_artifact_sha256": {name: sha256_bytes(payload) for name, payload in sorted(payloads.items())},
            "input_manifest_sha256": input_hash, "input_config_sha256": config_hash,
            "input_content_hashes": input_content_hashes, "code_hashes": legacy_hashes,
            "wrapper_source_sha256": wrapper_hash, **CHILD_SAFETY,
            "safety_basis": "ENGINE_BYTES_AND_WRAPPER_OFFLINE_EXECUTION_SCOPE",
            "external_broker_verification": "NOT_RUN",
            "real_order_count_scope": "THIS_WRAPPER_INVOCATION_AND_CHILD"}


def _native_optional_assertions(objects, child_provenance):
    expected = {**child_provenance, **SAFETY, "symbol": child_provenance["raw_symbol"],
                "dataset_kind": "historical_single_contract_daily_scenario", "status": "complete",
                "strategy_parameters": PARAMETERS[STRATEGIES.index(child_provenance["strategy"])],
                "source_hashes_status": "copied_frozen_export_provenance_not_external_reverified"}
    for obj in objects:
        for key, value in expected.items():
            if key in obj:
                _typed_equal(obj[key], value, f"native child optional assertion {key}")


def _child(directory, relative, record, strategy, bars, config, input_manifest, input_hash,
           config_hash, legacy_hashes, wrapper_hash):
    names = {*CHILD_FILES, "manifest.json"}
    _equal({p.name for p in directory.iterdir()}, names, "exact six-file child layout")
    payloads = {}
    for name in sorted(names):
        path = directory / name
        if _safe_path(path, output=True) != path or not path.is_file():
            raise ValueError("unsafe child output")
        payloads[name] = path.read_bytes()
    manifest, report, published = (_object(payloads[name]) for name in ("manifest.json", "report.json", "config.json"))
    _typed_equal(manifest.get("status"), "complete", "child manifest status")
    _typed_equal(manifest.get("content_hashes"), {n: sha256_bytes(payloads[n]) for n in CHILD_FILES}, "child hash closure")
    for key, value in {"input_manifest_sha256": input_hash, "input_config_sha256": config_hash,
                       "input_content_hashes": input_manifest["content_hashes"], "code_hashes": legacy_hashes,
                       "source_hashes": input_manifest.get("source_hashes", {})}.items():
        _typed_equal(manifest.get(key), value, f"child provenance {key}")
    _typed_equal(manifest.get("source_hashes_status"), "copied_frozen_export_provenance_not_external_reverified",
                 "child source hash verification boundary")
    for obj in (manifest, report):
        _typed_equal(obj.get("dataset_kind"), "historical_single_contract_daily_scenario", "child SCENARIO dataset")
    for obj in (manifest, report, published):
        _typed_equal(obj.get("historical_strict_execution"), False, "child historical strict boundary")
        _typed_equal(obj.get("assumptions_verification"), "not_historical_verified", "child assumption boundary")
    for obj in (published, report):
        for key, value in {"symbol": record["symbol"], "contract_instance_id": record["contract_instance_id"],
                           "strategy": strategy}.items():
            _typed_equal(obj.get(key), value, f"child identity {key}")
    _equal(published.get("assumptions"), config.to_dict(), "child BASE assumptions")
    _typed_equal(published.get("strategy_parameters"), PARAMETERS[STRATEGIES.index(strategy)], "child fixed strategy")
    _equal(published.get("parameter_selection"), "fixed_no_optimization", "child parameter selection")
    _equal(published.get("terminal_policy"), "precommitted_final_observation_close", "child terminal policy")
    _equal(report.get("status"), "complete", "child report status")
    _equal(report.get("observations"), len(bars), "child observations")
    _equal(report.get("first_datetime_nano"), bars[0].datetime_nano, "child first identity")
    _equal(report.get("last_datetime_nano"), bars[-1].datetime_nano, "child last identity")
    reconciliation = report.get("reconciliation")
    if not isinstance(reconciliation, dict) or not reconciliation or any(v is not True for v in reconciliation.values()):
        raise ValueError("child accounting reconciliation flags failed")
    _equal(reconciliation.get("terminal_flat"), True, "child terminal-flat reconciliation")
    if not isinstance(report.get("event_order"), list) or not report["event_order"] or not isinstance(report.get("limitations"), list) or not report["limitations"]:
        raise ValueError("child event order and limitations are required")
    fills = _csv_rows(payloads["fills.csv"], FILL_COLUMNS)
    equity = _csv_rows(payloads["equity.csv"], EQUITY_COLUMNS)
    decisions = _csv_rows(payloads["decisions.csv"], DECISION_COLUMNS)
    _equal(len(equity), len(bars), "child equity row count")
    _equal(len(decisions), len(bars), "child decision row count")
    _equal(len(fills), report.get("fills"), "child fill count")
    by_j = defaultdict(list)
    last_fill_j = -1
    for i, fill in enumerate(fills):
        j = _integer(fill["observation_index"])
        if not 0 <= j < len(bars) or j < last_fill_j:
            raise ValueError("invalid fill observation index/order")
        _equal(fill["fill_id"], f"fill-{i:06d}", "unique ordered fill id")
        last_fill_j = j
        by_j[j].append(fill)
    rows, position, entry = [], 0, D(0)
    cash = previous_equity = peak = config.initial_cash
    realized = cumulative_gross = cumulative_fees = cumulative_slip = D(0)
    previous_gross = previous_fees = previous_slip = D(0)
    max_dd = max_dd_ratio = D(0)
    turnover = D(0)
    for j, (bar, mark, decision) in enumerate(zip(bars, equity, decisions)):
        identity = {"observation_index": str(j), "datetime_nano": str(bar.datetime_nano), "raw_ts": bar.ts.isoformat()}
        for obj in (mark, decision, *by_j[j]):
            for key, expected in identity.items():
                _equal(obj[key], expected, f"ledger {key}")
        terminal = j == len(bars) - 1
        _equal(_boolean(mark["terminal"]), terminal, "terminal equity row")
        _equal(_boolean(decision["final_observation"]), terminal, "terminal decision row")
        _equal(decision["decision_at"], "", "observation-only decision clock")
        _equal(decision["clock_status"], "observation_only", "observation-only status")
        _equal(decision["preceding_signal_index"], str(j - 1) if j else "", "preceding signal")
        _equal(decision["attempted_target_lots"], decisions[j - 1]["target_lots"] if j else "", "previous completed target")
        candidate = decision["candidate_direction"]
        if candidate not in ("", "-1", "0", "1") or decision["evaluator_candidate_direction"] not in ("", "-1", "0", "1"):
            raise ValueError("invalid candidate direction")
        _equal(decision["target_lots"], str(int(candidate) * config.lots) if candidate else "", "fixed target lots")
        _integer(decision["logical_direction"])
        if decision["signal_age"]:
            if _integer(decision["signal_age"]) < 0:
                raise ValueError("negative logical signal age")
        for field in ("opening_required_margin", "opening_equity_after_costs"):
            if decision[field]:
                _decimal(decision[field])
        before_position = position
        daily_gross = D(0) if not j else before_position * config.multiplier * (bar.open - bars[j - 1].close)
        terminal_legs = [f for f in by_j[j] if f["phase"] == "terminal_close"]
        seen_terminal = False
        for fill in by_j[j]:
            phase, offset = fill["phase"], fill["offset"]
            side, lots = _integer(fill["side"]), _integer(fill["lots"])
            if side not in (-1, 1) or lots <= 0 or offset not in ("open", "close"):
                raise ValueError("invalid fill direction/quantity/offset")
            if phase == "terminal_close":
                if not terminal or seen_terminal or offset != "close" or fill["signal_index"]:
                    raise ValueError("invalid terminal close")
                seen_terminal = True
                daily_gross += position * config.multiplier * (bar.close - bar.open)
                ideal = bar.close
            elif phase == "next_open" and j and not seen_terminal:
                _equal(fill["signal_index"], str(j - 1), "fill preceding signal")
                ideal = bar.open
            else:
                raise ValueError("invalid fill execution phase")
            _equal(_decimal(fill["ideal_price"]), ideal, "ideal fill price")
            _equal(_decimal(fill["effective_price"]), ideal + side * config.price_tick * config.slippage_ticks, "informational effective price")
            flat, rate = config.fee_per_lot * lots, config.fee_rate * ideal * config.multiplier * lots
            slip = config.price_tick * config.slippage_ticks * config.multiplier * lots
            _equal(_decimal(fill["fee_per_lot_component"]), flat, "flat fee component")
            _equal(_decimal(fill["fee_rate_component"]), rate, "notional fee component")
            _equal(_decimal(fill["fee"]), flat + rate, "fill fee sum")
            _equal(_decimal(fill["slippage"]), slip, "single cash slippage charge")
            if offset == "close":
                if not position or side != (-1 if position > 0 else 1) or lots != abs(position):
                    raise ValueError("closing fill does not match actual position")
                pnl = (ideal - entry) * position * config.multiplier
                position, entry = 0, D(0)
            else:
                if position or lots != config.lots or phase != "next_open":
                    raise ValueError("opening fill does not follow flat fixed-lot state")
                pnl = D(0)
                position, entry = side * lots, ideal
            cash += pnl - flat - rate - slip
            realized += pnl
            cumulative_fees += flat + rate
            cumulative_slip += slip
            turnover += ideal * config.multiplier * lots
            _equal(_decimal(fill["realized_pnl"]), pnl, "fill ideal-price realized PnL")
            _equal(_decimal(fill["cash_after"]), cash, "fill cash chain")
            _equal(_integer(fill["position_after"]), position, "fill position chain")
        if not seen_terminal:
            daily_gross += position * config.multiplier * (bar.close - bar.open)
        cumulative_gross += daily_gross
        unrealized = position * config.multiplier * (bar.close - entry)
        value = cash + unrealized
        margin = abs(position) * bar.close * config.multiplier * config.margin_rate
        breached = bool(position and (value <= 0 or margin > value * config.max_margin_ratio))
        peak = max(peak, value)
        dd, dd_ratio = peak - value, (peak - value) / peak
        max_dd, max_dd_ratio = max(max_dd, dd), max(max_dd_ratio, dd_ratio)
        expected_values = {"close": bar.close, "cash": cash, "realized_pnl": realized,
                           "unrealized_pnl": unrealized, "equity": value, "daily_gross_pnl": daily_gross,
                           "gross_pnl": cumulative_gross, "fees": cumulative_fees, "slippage": cumulative_slip,
                           "net_pnl": value - config.initial_cash, "required_margin": margin,
                           "available_equity": value - margin, "drawdown": dd, "drawdown_ratio": dd_ratio}
        for field, expected in expected_values.items():
            _equal(_decimal(mark[field]), expected, f"equity ledger {field}")
        _equal(_integer(mark["position_lots"]), position, "equity actual position")
        _equal(_boolean(mark["margin_breached"]), breached, "margin breach observation")
        gross_delta, fee_delta, slip_delta = cumulative_gross - previous_gross, cumulative_fees - previous_fees, cumulative_slip - previous_slip
        net_delta = gross_delta - fee_delta - slip_delta
        _equal(value - previous_equity, net_delta, "exact daily equity conservation")
        _equal(cumulative_gross, realized + unrealized, "gross realized/unrealized conservation")
        row = {"symbol": record["symbol"], "contract_instance_id": record["contract_instance_id"], "strategy": strategy,
               "j": j, "datetime_nano": bar.datetime_nano, "raw_ts": bar.ts.isoformat(), "raw_date": bar.ts.date().isoformat(),
               "raw_year": bar.ts.year, "year_status": CHRONOLOGY["year_status"][str(bar.ts.year)],
               "partial_year": bar.ts.year in (2020, 2026), "primary_support": j >= 60,
               "terminal": terminal, "position_lots": position, "cash": cash, "realized_pnl": realized,
               "unrealized_pnl": unrealized, "equity": value, "previous_equity": previous_equity,
               "equity_delta": value - previous_equity, "gross_pnl": cumulative_gross, "gross_delta": gross_delta,
               "fees": cumulative_fees, "fee_delta": fee_delta, "slippage": cumulative_slip,
               "slippage_delta": slip_delta, "net_pnl": value - config.initial_cash, "net_delta": net_delta,
               "fills_at_j": len(by_j[j]), "terminal_close_fills": len(terminal_legs), "candidate_direction": candidate,
               "strategy_status": decision["strategy_status"], "reason": decision["reason"],
               "execution_status": decision["execution_status"], "margin_breached": breached, "child_directory": relative}
        rows.append(row)
        previous_equity, previous_gross, previous_fees, previous_slip = value, cumulative_gross, cumulative_fees, cumulative_slip
    _equal(position, 0, "terminal actual position")
    amounts = {"initial_cash": config.initial_cash, "ending_cash": cash, "ending_equity": previous_equity,
               "gross_pnl": cumulative_gross, "realized_pnl": realized, "unrealized_pnl": D(0),
               "fees": cumulative_fees, "slippage": cumulative_slip, "net_pnl": previous_equity - config.initial_cash,
               "total_return": (previous_equity - config.initial_cash) / config.initial_cash,
               "max_drawdown": max_dd, "max_drawdown_ratio": max_dd_ratio, "turnover_notional": turnover}
    for field, expected in amounts.items():
        _equal(_decimal(report[field]), expected, f"report {field}")
    counts = {"ending_position_lots": 0, "closed_trades": sum(f["offset"] == "close" for f in fills),
              "turnover_lots": sum(_integer(f["lots"]) for f in fills),
              "opening_blocks": sum(d["execution_status"] in ("opening_costs_exhaust_equity", "opening_margin_blocked") for d in decisions),
              "margin_breach_observations": sum(r["margin_breached"] for r in rows),
              "nonpositive_equity_observations": sum(r["equity"] <= 0 for r in rows)}
    for field, expected in counts.items():
        if isinstance(report.get(field), bool) or not isinstance(report.get(field), int):
            raise ValueError("report counts must be integers")
        _equal(report[field], expected, f"report {field}")
    _equal(report["candidate_counts"], dict(sorted(Counter(d["candidate_direction"] or "null" for d in decisions).items())), "candidate counts")
    for field, expected in (("net_delta", amounts["net_pnl"]), ("gross_delta", cumulative_gross),
                            ("fee_delta", cumulative_fees), ("slippage_delta", cumulative_slip)):
        _equal(sum((r[field] for r in rows), D(0)), expected, f"full child sum {field}")
    provenance = _child_record(relative, record, strategy, payloads, input_hash, config_hash,
                               input_manifest["content_hashes"], legacy_hashes, wrapper_hash)
    _native_optional_assertions((manifest, report, published), provenance)
    return rows, provenance, {f"{relative}/{name}": sha256_bytes(payload) for name, payload in payloads.items()}


def _validate_child_provenance(destination, coverage, provenance):
    entries = coverage.get("child_provenance")
    if not isinstance(entries, list):
        raise ValueError("child provenance array is required")
    expected_count = coverage["expected_counts"]["child_calls"]
    _typed_equal(len(entries), expected_count, "child provenance count")
    accounts = [(record, strategy) for record in coverage["contracts"] for strategy in STRATEGIES]
    _typed_equal(len(accounts), expected_count, "child provenance account closure")
    for actual, (record, strategy) in zip(entries, accounts):
        relative = f"children/{record['symbol']}/{strategy}"
        directory = _safe_path(destination / relative, output=True)
        _equal({p.name for p in directory.iterdir()}, {*CHILD_FILES, "manifest.json"}, "child provenance six-file closure")
        payloads = {name: (directory / name).read_bytes() for name in (*CHILD_FILES, "manifest.json")}
        native = [_object(payloads[name]) for name in ("manifest.json", "report.json", "config.json")]
        _typed_equal(native[0].get("content_hashes"), {n: sha256_bytes(payloads[n]) for n in CHILD_FILES},
                     "child provenance native payload hashes")
        expected = _child_record(relative, record, strategy, payloads, provenance["input_manifest_sha256"],
                                 provenance["input_base_config_sha256"], provenance["input_content_hashes"],
                                 provenance["legacy_code_hashes"], provenance["wrapper_sha256"])
        _typed_equal(actual, expected, "child provenance record")
        _native_optional_assertions(native, expected)


def _provenance_report_line(reference):
    return (f"Child provenance: path=`{reference['path']}`; json_pointer=`{reference['json_pointer']}`; "
            f"entry_count={reference['entry_count']}; sha256=`{reference['sha256']}`.")


def _validate_provenance_binding(destination, coverage, provenance, parent):
    payload = (destination / "coverage.json").read_bytes()
    actual = _object(payload)
    _typed_equal(actual, coverage, "child provenance coverage read-back")
    _validate_child_provenance(destination, actual, provenance)
    reference = {"path": "coverage.json", "json_pointer": "/child_provenance",
                 "entry_count": coverage["expected_counts"]["child_calls"], "sha256": sha256_bytes(payload)}
    for obj in (_object((destination / "summary.json").read_bytes()), parent):
        _typed_equal(obj.get("child_provenance_ref"), reference, "child provenance reference")
        for field in ("evaluation_scope", "production_acceptance"):
            _typed_equal(obj.get(field), provenance[field], f"child provenance {field}")
        _typed_equal(obj.get("status"), "complete", "child provenance parent status")
    if _provenance_report_line(reference) not in (destination / "report.md").read_text(encoding="utf-8").splitlines():
        raise ValueError("child provenance report reference mismatch")


def _distribution(values):
    if not values:
        return (None, None, None)
    values = sorted(values)
    n = len(values)
    return values[0], values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2, values[-1]


def _contract_year(daily, records):
    grouped = defaultdict(list)
    for row in daily:
        grouped[row["symbol"], row["strategy"]].append(row)
    result = []
    for record in records:
        for strategy in STRATEGIES:
            trajectory = grouped[record["symbol"], strategy]
            for year in YEARS:
                for view in VIEWS:
                    rows = [r for r in trajectory if r["raw_year"] == year and (view == "ALL" or r["primary_support"])]
                    temporal = "FUTURE_ONLY" if year < int(record["first_raw_ts"][:4]) else "AFTER_LAST_OBSERVATION" if year > int(record["last_raw_ts"][:4]) else "WITHIN_STORED_SPAN"
                    status = ("PARTIAL_INPUT_WINDOW" if year in (2020, 2026) else "OBSERVED") if rows else "PRIMARY_EMPTY" if view == "PRIMARY" else "NO_ROWS"
                    first, last = (rows[0], rows[-1]) if rows else (None, None)
                    result.append({"symbol": record["symbol"], "contract_instance_id": record["contract_instance_id"],
                                   "strategy": strategy, "raw_year": year, "view": view, "status": status,
                                   "temporal_class": temporal, "year_status": CHRONOLOGY["year_status"][str(year)],
                                   "calendar_status": CALENDAR, "partial_year": year in (2020, 2026),
                                   "observations": len(rows), "raw_dates": len({r["raw_date"] for r in rows}),
                                   "fills": sum(r["fills_at_j"] for r in rows), "terminal_close_fills": sum(r["terminal_close_fills"] for r in rows),
                                   "first_j": first["j"] if rows else None, "last_j": last["j"] if rows else None,
                                   "first_raw_ts": first["raw_ts"] if rows else None, "last_raw_ts": last["raw_ts"] if rows else None,
                                   "previous_equity": first["previous_equity"] if rows else None, "end_equity": last["equity"] if rows else None,
                                   **{f"year_{name}": sum((r[name] for r in rows), D(0)) if rows else None
                                      for name in ("net_delta", "gross_delta", "fee_delta", "slippage_delta")},
                                   "carry_status": "NO_ROWS" if not rows else "INITIAL_OBSERVATION" if first["j"] == 0 else "CARRIED_FROM_PREVIOUS_RAW_YEAR" if trajectory[first["j"] - 1]["raw_year"] != year else "CARRIED_FROM_PREVIOUS_OBSERVATION",
                                   "position_before_first_row": (trajectory[first["j"] - 1]["position_lots"] if first["j"] else 0) if rows else None,
                                   "undefined_reason": None if rows else "no_stored_observation_in_view_and_raw_year",
                                   "child_directory": trajectory[0]["child_directory"]})
            for field in ("net_delta", "gross_delta", "fee_delta", "slippage_delta"):
                attributed = [r for r in result if r["symbol"] == record["symbol"] and r["strategy"] == strategy and r["view"] == "ALL"]
                _equal(sum((r[f"year_{field}"] or D(0) for r in attributed), D(0)),
                       sum((r[field] for r in trajectory), D(0)), f"seven-year conservation {field}")
    return result


def pair_observations(daily):
    """Classify unmatched evidence explicitly; production children are validated first."""
    grouped = defaultdict(dict)
    for row in daily:
        key = (row["symbol"], row["j"])
        if row["strategy"] in grouped[key]:
            raise ValueError("duplicate strategy observation for pairing")
        grouped[key][row["strategy"]] = row
    matched, excluded = [], []
    identity = ("symbol", "contract_instance_id", "j", "datetime_nano", "raw_ts", "raw_year", "raw_date")
    money = ("net_delta", "gross_delta", "fee_delta", "slippage_delta", "equity")
    for (symbol, j), strategies in sorted(grouped.items()):
        for a, b in PAIRS:
            left, right = strategies.get(a), strategies.get(b)
            for view in VIEWS:
                anchor = left or right
                if anchor is None or view == "PRIMARY" and j < 60:
                    continue
                classification = None
                if left is None or right is None:
                    classification = "MISSING_A" if left is None else "MISSING_B"
                elif any(left[k] != right[k] for k in identity):
                    classification = "IDENTITY_MISMATCH"
                elif view == "PRIMARY" and (not left["primary_support"] or not right["primary_support"]):
                    classification = "SUPPORT_MISMATCH"
                else:
                    try:
                        for row in (left, right):
                            for field in money:
                                _decimal(row[field])
                    except (ValueError, ArithmeticError):
                        classification = "NONFINITE_OR_INVALID_AMOUNT"
                if classification:
                    excluded.append({"symbol": symbol, "j": j, "raw_year": anchor["raw_year"], "view": view,
                                     "strategy_a": a, "strategy_b": b, "status": classification})
                    continue
                row = {**{k: left[k] for k in identity}, "view": view, "strategy_a": a, "strategy_b": b, "status": "MATCHED"}
                for field in money:
                    x, y = _decimal(left[field]), _decimal(right[field])
                    row.update({f"{field}_a": x, f"{field}_b": y, f"{field}_a_minus_b": x - y})
                matched.append(row)
    pair_index = {p: i for i, p in enumerate(PAIRS)}
    matched.sort(key=lambda r: (r["symbol"], pair_index[r["strategy_a"], r["strategy_b"]], r["j"], VIEWS.index(r["view"])))
    return matched, excluded


def _summaries(daily, contract_year, pairs, excluded):
    pair_summary = []
    for year in YEARS:
        for view in VIEWS:
            for a, b in PAIRS:
                rows = [r for r in pairs if (r["raw_year"], r["view"], r["strategy_a"], r["strategy_b"]) == (year, view, a, b)]
                by_contract = defaultdict(lambda: D(0))
                for row in rows:
                    by_contract[row["contract_instance_id"]] += row["net_delta_a_minus_b"]
                values = list(by_contract.values())
                low, middle, high = _distribution(values)
                pair_summary.append({"raw_year": year, "view": view, "strategy_a": a, "strategy_b": b,
                                     "status": "MATCHED" if rows else "PRIMARY_EMPTY" if view == "PRIMARY" else "NO_ROWS",
                                     "year_status": CHRONOLOGY["year_status"][str(year)], "calendar_status": CALENDAR,
                                     "matched_observations": len(rows), "matched_contract_years": len(values),
                                     "distinct_contracts": len(by_contract), "raw_dates": len({r["raw_date"] for r in rows}),
                                     "unmatched_observations": sum((r["raw_year"], r["view"], r["strategy_a"], r["strategy_b"]) == (year, view, a, b) for r in excluded),
                                     **{f"independent_account_descriptive_{name}_difference_sum": sum((r[f"{name}_delta_a_minus_b"] for r in rows), D(0)) if rows else None
                                        for name in ("net", "gross", "fee", "slippage")},
                                     "contract_year_difference_min": low, "contract_year_difference_median": middle,
                                     "contract_year_difference_max": high, "positive_contract_years": sum(v > 0 for v in values),
                                     "zero_contract_years": sum(v == 0 for v in values), "negative_contract_years": sum(v < 0 for v in values),
                                     "undefined_reason": None if rows else "no_matched_observations_in_view_and_raw_year"})
    year_summary = []
    for year in YEARS:
        for strategy in STRATEGIES:
            for view in VIEWS:
                cells = [r for r in contract_year if (r["raw_year"], r["strategy"], r["view"]) == (year, strategy, view) and r["observations"]]
                rows = [r for r in daily if r["raw_year"] == year and r["strategy"] == strategy and (view == "ALL" or r["primary_support"])]
                values = [r["year_net_delta"] for r in cells]
                low, middle, high = _distribution(values)
                year_summary.append({"raw_year": year, "strategy": strategy, "view": view,
                                     "status": ("PARTIAL_INPUT_WINDOW" if year in (2020, 2026) else "OBSERVED") if cells else "PRIMARY_EMPTY" if view == "PRIMARY" else "NO_ROWS",
                                     "year_status": CHRONOLOGY["year_status"][str(year)], "calendar_status": CALENDAR,
                                     "partial_year": year in (2020, 2026), "contract_year_cells": len(cells),
                                     "distinct_contracts": len({r["contract_instance_id"] for r in cells}),
                                     "observations": len(rows), "raw_dates": len({r["raw_date"] for r in rows}),
                                     "fills": sum(r["fills_at_j"] for r in rows), "terminal_close_fills": sum(r["terminal_close_fills"] for r in rows),
                                     "matched_pair_cells": sum(r["matched_contract_years"] for r in pair_summary if r["raw_year"] == year and r["view"] == view and strategy in (r["strategy_a"], r["strategy_b"])),
                                     **{f"independent_account_descriptive_{name}_sum": sum((r[f"year_{name}_delta"] for r in cells), D(0)) if cells else None
                                        for name in ("net", "gross", "fee", "slippage")},
                                     "contract_year_net_min": low, "contract_year_net_median": middle, "contract_year_net_max": high,
                                     "positive_contract_years": sum(v > 0 for v in values), "zero_contract_years": sum(v == 0 for v in values),
                                     "negative_contract_years": sum(v < 0 for v in values),
                                     "undefined_reason": None if cells else "no_observed_contract_years_in_view"})
    return year_summary, pair_summary


def _csv(columns, rows):
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_text(value) if isinstance(value, D) else "" if value is None else str(value).lower() if isinstance(value, bool) else value
                         for value in (row[c] for c in columns)])
    return buffer.getvalue().encode("utf-8")


def _json_ready(value):
    if isinstance(value, D):
        return _text(value)
    if isinstance(value, dict):
        return {k: _json_ready(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(v) for v in value]
    return value


def _report(coverage, year_summary, pair_summary, child_provenance_ref):
    lines = ["SCENARIO only: historical execution, economic assumptions and trading calendar are unverified.", "",
             f"The {'SYNTHETIC test' if coverage['data_kind'] == 'SYNTHETIC' else 'frozen real-export'} input supplies {coverage['rb_declarations']} concrete RB accounts per strategy; {coverage['expected_counts']['child_calls']} independent full-contract BASE replays retain all natural warmups and positions. The BASE parameter fixture is SYNTHETIC and is not historical parameter evidence.", "",
             f"Evaluation scope: {coverage['evaluation_scope']}; production_acceptance={str(coverage['production_acceptance']).lower()}. Production acceptance denotes only the fixed-input structural matrix; product hard review remains NOT_RUN.", "",
             _provenance_report_line(child_provenance_ref), "",
             "Native config/report/manifest fields supply their emitted identity, assumption and hash facts. The complete typed child records in coverage.json add wrapper-scoped safety declarations with ENGINE_BYTES_AND_WRAPPER_OFFLINE_EXECUTION_SCOPE. Real send/cancel counts concern only this wrapper invocation and child; simulated fills do not increment them. External broker verification remains NOT_RUN. Every original six-file child is preserved, including each report's event_order and limitations.", "",
             "historical_strict_execution=false; assumptions_verification=not_historical_verified; broker=NOT_RUN; enable_trading=false; real send/cancel calls=0. Calendar status: UNAVAILABLE_VERIFIED_TRADING_CALENDAR. Raw timestamp years use explicit +08:00 local time. 2020 and 2026 are PARTIAL_INPUT_WINDOW years; no missing session is inferred or filled.", "",
             "Cohort composition is confounded across years: contracts differ and overlap incompletely. A cross-year difference is not same-contract temporal transfer unless that exact contract is paired in both years. All pairings below compare strategies on the same concrete contract and raw observation within a year. Accounts and their initial cash are never pooled or stitched.", "",
             "| Concrete RB contract | Stored observations | j >= 60 observations | First raw timestamp | Last raw timestamp |",
             "| --- | ---: | ---: | --- | --- |"]
    for row in coverage["contracts"]:
        lines.append(f"| {row['symbol']} | {row['observations']} | {row['primary_observations']} | {row['first_raw_ts']} | {row['last_raw_ts']} |")
    lines += ["", "| Raw year | Status | RB observations before strategy replication | Distinct raw dates |", "| ---: | --- | ---: | ---: |"]
    for year in YEARS:
        lines.append(f"| {year} | {CHRONOLOGY['year_status'][str(year)]} | {coverage['rb_year_bar_counts'][str(year)]} | {coverage['rb_year_raw_date_counts'][str(year)]} |")
    lines += ["", "Year descriptions use observed contract-year cells as the distribution denominator. The sum column is a descriptive sum of independent account increments, not a portfolio NAV or pooled return.", "",
              "| Raw year | Strategy | View | Observed contract-years | Observations | Independent-account descriptive net sum | Contract-year net min / median / max |",
              "| ---: | --- | --- | ---: | ---: | ---: | --- |"]
    def display(value):
        return "null" if value is None else _text(value) if isinstance(value, D) else str(value)
    for row in year_summary:
        dist = " / ".join(display(row[f"contract_year_net_{k}"]) for k in ("min", "median", "max"))
        lines.append(f"| {row['raw_year']} | {row['strategy']} | {row['view']} | {row['contract_year_cells']} | {row['observations']} | {display(row['independent_account_descriptive_net_sum'])} | {dist} |")
    lines += ["", "Paired differences are A minus B, matched only on the same full contract, raw year, observation index and timestamp. They have no cross-contract imputation.", "",
              "| Raw year | View | Strategy A | Strategy B | Matched observations | Matched contract-years | Descriptive net difference sum | Contract-year difference min / median / max |",
              "| ---: | --- | --- | --- | ---: | ---: | ---: | --- |"]
    for row in pair_summary:
        dist = " / ".join(display(row[f"contract_year_difference_{k}"]) for k in ("min", "median", "max"))
        lines.append(f"| {row['raw_year']} | {row['view']} | {row['strategy_a']} | {row['strategy_b']} | {row['matched_observations']} | {row['matched_contract_years']} | {display(row['independent_account_descriptive_net_difference_sum'])} | {dist} |")
    lines += ["", "The PRIMARY view is the j >= 60 attribution mask over the same full account ledger. Short contracts, zero candidates, zero fills and all-cash paths remain present. Empty cells retain NO_ROWS/PRIMARY_EMPTY with null undefined metrics; future-only cohorts do not contribute earlier denominators. A year boundary creates no new initial cash, reset or close. The unchanged engine's terminal close belongs only to its final raw observation's year.", "",
              "No historical PIT/OOS, verified trading-calendar, continuous-roll, pooled account, fit, optimization, strategy selection, ranking, winner, significance, annualization, Sharpe or qualification claim is made. This descriptive experiment is not an M5 A/B mandatory gate. Fees, tick, multiplier, margin, listing history, available_at, settlement, capacity and executable next-open assumptions are unverified. Broker and real-order operations remain NOT_RUN.", "",
              "[coverage](coverage.json), [daily attribution](daily_attribution.csv), [contract-years](contract_year.csv), [year summaries](year_summary.csv), [paired observations](paired_observations.csv), [paired summaries](paired_summary.csv), [summary](summary.json), [chronology precommit](chronology-precommit.json), [manifest](manifest.json). Each daily row names its unchanged six-file child directory."]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _write_new(root, name, payload, hashes):
    path = root / name
    _safe_path(path, output=True)
    with path.open("xb") as handle:
        handle.write(payload)
    _equal(path.read_bytes(), payload, f"payload read-back {name}")
    _equal(sha256_file(path), sha256_bytes(payload), f"payload hash {name}")
    hashes[name] = sha256_bytes(payload)


def _verify_tree(root, hashes, *, marker=False):
    actual = set()
    for path in root.rglob("*"):
        _safe_path(path, output=True)
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
        elif not path.is_dir():
            raise ValueError("non-file output artifact")
    _equal(actual, set(hashes) | ({"manifest.json"} if marker else set()), "exact output tree closure")
    for name, expected in hashes.items():
        _equal(sha256_file(root / name), expected, f"published output drift {name}")


class _Incomplete(RuntimeError):
    def __init__(self, cause):
        super().__init__(str(cause))
        self.cause = cause


def run_transfer(input_bundle: Path, base_config: Path, output: Path) -> dict:
    source, base_file = _safe_path(input_bundle), _safe_path(base_config)
    destination = _safe_path(output, output=True)
    forbidden = (source, base_file, *(ROOT / n for n in ("src", "scripts", "tests", "config", "tools", "docs")))
    assert_output_directory_is_new(destination, forbidden)
    inventory, legacy_hashes, snapshot = _source_pins()
    with localcontext(ARITHMETIC):
        bundle, config, base_bytes, input_snapshot = _capture(source, base_file)
        snapshot.update(input_snapshot)
        coverage, bars = _audit_population(bundle, config)
        _require_frozen_real_input(bundle, coverage, inventory)
    _check_snapshot(snapshot)
    return _publish(source, base_file, destination, bundle, config, base_bytes,
                    coverage, bars, legacy_hashes, snapshot)


def _publish(source, base_file, destination, bundle, config, base_bytes, coverage, bars,
             legacy_hashes, snapshot):
    """Publish already captured evidence; production always audits real pins first."""
    forbidden = (source, base_file, *(ROOT / n for n in ("src", "scripts", "tests", "config", "tools", "docs")))
    destination = _safe_path(destination, output=True)
    assert_output_directory_is_new(destination, forbidden)
    _check_snapshot(snapshot)
    with localcontext(ARITHMETIC):
        scope = _publication_scope(source, base_file, bundle, config, base_bytes, coverage, bars, snapshot)
    coverage = {**coverage, **scope}
    expected = coverage["expected_counts"]
    provenance = {**SAFETY, **scope, "schema_version": "rb_daily_chronological_transfer_v1", "calendar_status": CALENDAR,
                  "data_kind": coverage["data_kind"], "effective_plan": PLAN_FILE.relative_to(ROOT).as_posix(),
                  "effective_plan_sha256": PLAN_HASH, "input_inventory_sha256": INPUTS_HASH,
                  "implementation_clarification": {"path": CLARIFICATION_FILE.relative_to(ROOT).as_posix(),
                                                     "sha256": CLARIFICATION_HASH},
                  "implementation_disposition": {"path": DISPOSITION_FILE.relative_to(ROOT).as_posix(),
                                                   "sha256": DISPOSITION_HASH},
                  "input_manifest_sha256": snapshot[source / "manifest.json"],
                  "input_content_hashes": dict(bundle.manifest["content_hashes"]),
                  "input_base_config_sha256": sha256_bytes(base_bytes),
                  "base_parameter_fixture": "SYNTHETIC_NOT_HISTORICAL_VERIFICATION",
                  "legacy_code_hashes": legacy_hashes, "wrapper_sha256": snapshot[ROOT / SELF],
                  "read_only_sibling_reference": {SIBLING_REFERENCE: snapshot[ROOT / SIBLING_REFERENCE]},
                  "expected_counts": expected, "chronology": CHRONOLOGY}
    precommit = {**provenance, "universe_sha256": coverage["universe_sha256"],
                 "universe": [{"symbol": r["symbol"], "contract_instance_id": r["contract_instance_id"]} for r in coverage["contracts"]],
                 "pnl_inspection_started": False, "production_acceptance": False}
    _safe_path(destination, output=True)
    assert_output_directory_is_new(destination, forbidden)
    destination.mkdir(parents=True, exist_ok=False)
    hashes = {}
    def incomplete(exc):
        return canonical_json_bytes({**provenance, "status": "incomplete", "content_hashes": dict(sorted(hashes.items())),
                                     "production_acceptance": False, "error_type": type(exc).__name__,
                                     "reason": "chronological_transfer_publication_failed"})
    try:
        _write_new(destination, "chronology-precommit.json", canonical_json_bytes(precommit), hashes)
        _equal(_object((destination / "chronology-precommit.json").read_bytes())["pnl_inspection_started"], False, "precommit before economic inspection")
        _write_new(destination, "base_config.json", canonical_json_bytes(config.to_dict()), hashes)
        daily, children = [], []
        calls = 0
        with localcontext(ARITHMETIC):
            for record in coverage["contracts"]:
                for strategy in STRATEGIES:
                    _check_snapshot(snapshot)
                    relative = f"children/{record['symbol']}/{strategy}"
                    directory = _safe_path(destination / relative, output=True)
                    run_history_scenario(source, directory, symbol=record["symbol"], strategy=strategy, config_path=base_file)
                    calls += 1
                    rows, child_provenance, child_hashes = _child(directory, relative, record, strategy,
                        bars[record["symbol"]], config, bundle.manifest, provenance["input_manifest_sha256"],
                        provenance["input_base_config_sha256"], legacy_hashes, provenance["wrapper_sha256"])
                    _check_snapshot(snapshot)
                    daily.extend(rows)
                    children.append(child_provenance)
                    hashes.update(child_hashes)
            contract_year = _contract_year(daily, coverage["contracts"])
            pairs, excluded = pair_observations(daily)
            if excluded:
                raise ValueError("validated child matrix contains unmatched paired observations")
            year_summary, pair_summary = _summaries(daily, contract_year, pairs, excluded)
            actual = {"child_calls": calls, "daily_rows": len(daily), "primary_rows": sum(r["primary_support"] for r in daily),
                      "contract_year_cells": len(contract_year), "year_summary_rows": len(year_summary),
                      "paired_summary_rows": len(pair_summary), "paired_observation_rows": len(pairs)}
            _equal(actual, expected, "complete matrix/attribution counts")
            coverage = {**coverage, "child_provenance": children}
            _write_new(destination, "coverage.json", canonical_json_bytes(coverage), hashes)
            child_provenance_ref = {"path": "coverage.json", "json_pointer": "/child_provenance",
                                    "entry_count": expected["child_calls"], "sha256": hashes["coverage.json"]}
            summary = {**provenance, "status": "complete", "actual_counts": actual,
                       "strategy_order": list(STRATEGIES), "view_order": list(VIEWS), "pair_order": [list(p) for p in PAIRS],
                       "denominators": {"contract_year": "observed rows in the same concrete account/year/view",
                                        "year_distribution": "observed contract-year cells; independent accounts; changing cohorts",
                                        "paired": "same concrete contract, raw year, j, datetime_nano and raw_ts only",
                                        "descriptive_sum": "sum of independent account increments; no pooled capital or return"},
                       "undefined_metrics": "null with explicit NO_ROWS/PRIMARY_EMPTY reason",
                       "pair_classifications": {"MATCHED": len(pairs), "MISSING_A": 0, "MISSING_B": 0,
                                                "IDENTITY_MISMATCH": 0, "NONFINITE_OR_INVALID_AMOUNT": 0, "SUPPORT_MISMATCH": 0},
                       "contract_year_status_counts": dict(sorted(Counter(r["status"] for r in contract_year).items())),
                       "child_provenance_ref": child_provenance_ref,
                       "year_summary": year_summary, "paired_summary": pair_summary,
                       "decimal_precision": 128, "ratio_rounding": "ROUND_HALF_EVEN",
                       "composition_confounding": "cross-year cohorts differ and overlap incompletely; no general same-contract temporal-transfer inference",
                       "product_hard_review": "NOT_RUN", "m5_ab_mandatory_gate": False}
            payloads = {"daily_attribution.csv": _csv(DAILY_COLUMNS, daily),
                        "contract_year.csv": _csv(YEAR_COLUMNS, contract_year), "year_summary.csv": _csv(SUM_COLUMNS, year_summary),
                        "paired_observations.csv": _csv(PAIR_COLUMNS, pairs), "paired_summary.csv": _csv(PAIR_SUM_COLUMNS, pair_summary),
                        "summary.json": canonical_json_bytes(_json_ready(summary)),
                        "report.md": _report(coverage, year_summary, pair_summary, child_provenance_ref)}
        for name, payload in payloads.items():
            _write_new(destination, name, payload, hashes)
        _verify_tree(destination, hashes)
        _check_snapshot(snapshot)
        with localcontext(ARITHMETIC):
            _equal(_publication_scope(source, base_file, bundle, config, base_bytes, coverage, bars, snapshot),
                   scope, "complete publication scope")
        complete = canonical_json_bytes({**provenance, "status": "complete", "actual_counts": actual,
                                         "child_provenance_ref": child_provenance_ref,
                                         "content_hashes": dict(sorted(hashes.items()))})
        _validate_provenance_binding(destination, coverage, provenance, _object(complete))
        marker = destination / "manifest.json"
        with marker.open("xb") as handle:
            try:
                handle.write(complete)
                handle.flush()
                _equal(marker.read_bytes(), complete, "complete marker read-back")
                _verify_tree(destination, hashes, marker=True)
                _validate_provenance_binding(destination, coverage, provenance, _object(marker.read_bytes()))
                _check_snapshot(snapshot)
            except BaseException as exc:
                try:
                    handle.seek(0)
                    handle.truncate()
                    handle.write(incomplete(exc))
                    handle.flush()
                except OSError:
                    pass
                raise
    except BaseException as exc:
        try:
            with (destination / "manifest.json").open("xb") as handle:
                handle.write(incomplete(exc))
        except OSError:
            pass
        if isinstance(exc, Exception):
            raise _Incomplete(exc) from exc
        raise
    return {**SAFETY, "status": "complete", "calendar_status": CALENDAR, "actual_counts": actual, "output": str(destination)}


class _ArgumentError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise _ArgumentError(message)


class _Once(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        if getattr(namespace, self.dest, None) is not None:
            parser.error(f"{option_string} must be provided exactly once")
        setattr(namespace, self.dest, values)


def main(argv=None):
    parser = _Parser(description=__doc__, allow_abbrev=False)
    for option in ("--input-bundle", "--base-config", "--output"):
        parser.add_argument(option, type=Path, required=True, action=_Once)
    try:
        args = parser.parse_args(argv)
        result = run_transfer(args.input_bundle, args.base_config, args.output)
    except Exception as exc:
        cause = exc.cause if isinstance(exc, _Incomplete) else exc
        print(json.dumps({**SAFETY, "status": "incomplete" if isinstance(exc, _Incomplete) else "rejected",
                          "error_type": type(cause).__name__, "reason": str(cause)[:500]},
                         ensure_ascii=False, sort_keys=True, allow_nan=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
