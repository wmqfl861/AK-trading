"""Attribute unchanged offline RB daily accounts to prior-close causal labels.

Labels describe daily account changes; they never enter a strategy or execution
decision. PRIMARY is a reporting mask over the full naturally warmed trajectory.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
import io
import json
from pathlib import Path
import re
import stat
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "src"))

from ak_trading.futures.io import (  # noqa: E402
    OutputDirectoryError, assert_output_directory_is_new, canonical_json_bytes,
    read_input_bundle, sha256_file,
)
from ak_trading.futures.scenario_backtest import (  # noqa: E402
    ScenarioConfig, run_history_scenario,
)

ARITHMETIC = Context(prec=128, rounding=ROUND_HALF_EVEN)
LABELS = ("UNKNOWN", "FLAT", "TREND_UP", "TREND_DOWN", "RANGE", "MIXED")
WINDOWS = ("ALL", "PRIMARY")
RULE_ID = "prior21_er20_v1"
STRATEGIES = (
    ("donchian_trend", 41, {"entry_window": 40, "exit_window": 20}),
    ("ma_trend", 60, {"fast_window": 20, "slow_window": 60}),
    ("zscore_reversion", 21, {"lookback": 20, "entry_z": 2.0, "exit_z": 0.5,
                              "stop_z": 4.0, "max_signal_age": 20}),
)
PAIRS = (("donchian_trend", "ma_trend"), ("donchian_trend", "zscore_reversion"),
         ("ma_trend", "zscore_reversion"))
BASE = dict(initial_cash="1000000", lots=1, multiplier="10", price_tick="1",
            fee_per_lot="0.01", fee_rate="0.0001", slippage_ticks="1",
            margin_rate="0.12", max_margin_ratio="0.4")
ENGINE_CODE_PATHS = (
    "src/ak_trading/__init__.py", "src/ak_trading/futures/__init__.py",
    "src/ak_trading/futures/scenario_backtest.py", "src/ak_trading/futures/spec.py",
    "src/ak_trading/futures/strategies.py", "src/ak_trading/futures/structures.py",
    "src/ak_trading/futures/io.py", "src/ak_trading/futures/data.py",
    "src/ak_trading/strategy.py", "scripts/backtest_history.py",
)
CODE_PATHS = (*ENGINE_CODE_PATHS, "tools/experiments/historical_daily_causal_regime.py")
CORE_INPUTS = ("bars.csv", "contracts.json", "metadata.json", "availability.json")
CHILD_PAYLOADS = ("config.json", "fills.csv", "equity.csv", "decisions.csv", "report.json")
SAFETY = dict(evidence_level="SCENARIO", historical_strict_execution=False,
              assumptions_verification="not_historical_verified",
              statistical_qualification="NOT_ASSESSED", broker="NOT_RUN",
              enable_trading=False, real_order_send_calls=0, real_order_cancel_calls=0)
FILL_COLUMNS = tuple("fill_id observation_index signal_index datetime_nano raw_ts phase offset side lots ideal_price effective_price fee_per_lot_component fee_rate_component fee slippage realized_pnl cash_after position_after".split())
EQUITY_COLUMNS = tuple("observation_index datetime_nano raw_ts close position_lots cash realized_pnl unrealized_pnl equity daily_gross_pnl gross_pnl fees slippage net_pnl required_margin available_equity margin_breached drawdown drawdown_ratio terminal".split())
DECISION_COLUMNS = tuple("observation_index datetime_nano raw_ts decision_at clock_status strategy_status reason evaluator_candidate_direction candidate_direction target_lots logical_direction signal_age preceding_signal_index attempted_target_lots execution_status opening_required_margin opening_equity_after_costs final_observation".split())
DAILY_COLUMNS = tuple("symbol contract_instance_id strategy observation_index datetime_nano raw_ts raw_date rule_id regime prefix_start_index prefix_end_index prefix_start_datetime_nano prefix_end_datetime_nano signed_endpoint_change absolute_path_change efficiency_ratio primary terminal prior_close_position_lots position_lots active_observation fill_legs terminal_close_legs gross_delta fee_delta slippage_delta net_delta terminal_close_fees terminal_close_slippage equity gross_pnl fees slippage".split())
GROUP_COLUMNS = tuple("symbol contract_instance_id strategy window regime coverage_status reason statistical_qualification observation_count raw_date_count active_observations fill_legs terminal_observations terminal_close_legs gross_pnl fees slippage net_pnl terminal_close_fees terminal_close_slippage contribution_max_drawdown mean_net_delta".split())
SUM_FIELDS = ("gross_pnl", "fees", "slippage", "net_pnl", "terminal_close_fees", "terminal_close_slippage")
REAL_MANIFEST_SHA256 = "f995ba8f425622ba16e1e9be34ad66c016071007fa7e7bae29056e254cc62faa"
REAL_COUNTS = dict(zip(
    ("SHFE.rb" + value for value in ("2009", "2010", "2011", "2012", "2101", "2102", "2103", "2104", "2105", "2106", "2107", "2108", "2109", "2110", "2111", "2112", "2201")),
    (172, 188, 210, 231, 243, 236, 216, 195, 176, 155, 135, 112, 91, 75, 53, 32, 10),
))
REAL_LABEL_COUNTS = {"ALL": (346, 0, 507, 89, 688, 900), "PRIMARY": (0, 0, 370, 67, 468, 690)}


def _text(number: Decimal) -> str:
    if not number.is_finite():
        raise ValueError("nonfinite attribution amount")
    if number == 0:
        return "0"
    result = format(number, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def _decimal(value: object) -> Decimal:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError("ledger amount must be a finite Decimal string")
    number = Decimal(value)
    if not number.is_finite() or len(number.as_tuple().digits) > 128 or not -256 <= number.as_tuple().exponent <= 30:
        raise ValueError("ledger amount exceeds finite accounting bounds")
    return number


def _integer(value: str) -> int:
    if not isinstance(value, str) or re.fullmatch(r"-?(?:0|[1-9][0-9]*)", value) is None or len(value) > 30:
        raise ValueError("ledger integer has invalid representation")
    return int(value)


def _boolean(value: str) -> bool:
    if value not in ("true", "false"):
        raise ValueError("ledger boolean must be true or false")
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


def _json(payload: bytes):
    return json.loads(payload.decode("utf-8"), object_pairs_hook=_unique,
                      parse_float=Decimal, parse_constant=_nonfinite)


def _object(payload: bytes) -> dict:
    document = _json(payload)
    if not isinstance(document, dict):
        raise ValueError("JSON document must be an object")
    return document


def _digest(payload: bytes) -> str:
    # Hash captured bytes, without reading a path a second time.
    from hashlib import sha256
    return sha256(payload).hexdigest()


def _path(path: Path, *, output=False) -> Path:
    path = Path(path)
    if str(path).startswith(("\\\\?\\", "\\\\.\\")):
        raise ValueError("device namespaces are unsupported")
    for part in path.parts:
        stem = part.rstrip(" .").split(".", 1)[0].upper()
        if stem in {"NUL", "CON", "PRN", "AUX", "CONIN$", "CONOUT$"} or re.fullmatch(r"(?:COM|LPT)[1-9]", stem):
            raise ValueError("reserved device paths are unsupported")
        if part != path.anchor and ":" in part:
            raise ValueError("alternate stream paths are unsupported")
        if part.lower() == ".env" or part.lower().startswith(".env."):
            raise ValueError("credential paths are outside this offline contract")
    if output:
        absolute = path.absolute()
        for component in (absolute, *absolute.parents):
            try:
                attributes = component.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(attributes.st_mode) or getattr(attributes, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise OutputDirectoryError("output components may not be symlinks or reparse aliases")
    return path.resolve()


def _code_hashes() -> dict:
    return {name: sha256_file(ROOT / name) for name in CODE_PATHS}


def _unchanged(source, snapshot, base_file, base_hash, code_hashes):
    if {name: sha256_file(source / name) for name in snapshot} != snapshot:
        raise ValueError("input bytes changed during attribution")
    if sha256_file(base_file) != base_hash:
        raise ValueError("BASE bytes changed during attribution")
    if _code_hashes() != code_hashes:
        raise ValueError("attribution code changed during publication")


def _read_bundle(source):
    if not source.is_dir():
        raise ValueError("required input bundle directory does not exist")
    for name in ("manifest.json", *CORE_INPUTS):
        if not (source / name).is_file():
            raise ValueError(f"required input file does not exist: {name}")
    payload = (source / "manifest.json").read_bytes()
    manifest = _object(payload)
    hashes = manifest.get("content_hashes")
    if not isinstance(hashes, dict) or not set(CORE_INPUTS) <= set(hashes):
        raise ValueError("input manifest must bind all four core payloads")
    snapshot = {"manifest.json": _digest(payload)}
    for name, expected in hashes.items():
        if (not isinstance(name, str) or name in ("", ".", "..", "manifest.json")
                or any(character in name for character in "/\\:")
                or not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None):
            raise ValueError("invalid input payload hash declaration")
        member = _path(source / name)
        if member.parent != source or not member.is_file():
            raise ValueError("input payload is absent or outside the bundle")
        captured = member.read_bytes()
        if _digest(captured) != expected:
            raise ValueError(f"input content hash mismatch: {name}")
        snapshot[name] = expected
        if name == "contracts.json":
            declarations = _json(captured)
            if not isinstance(declarations, list) or any(not isinstance(row, dict) for row in declarations):
                raise ValueError("contracts.json must contain only contract objects")
        elif name in ("metadata.json", "availability.json"):
            _object(captured)
    bundle = read_input_bundle(source)
    if bundle.manifest != manifest or {name: sha256_file(source / name) for name in snapshot} != snapshot:
        raise ValueError("input changed while reading")
    if manifest.get("dataset_kind") != "real_export":
        raise ValueError("existing engine requires real_export transport")
    for name, actual in (("bars", len(bundle.bars)), ("contracts", len(bundle.contracts))):
        declared = manifest.get("row_counts", {}).get(name)
        if isinstance(declared, bool) or not isinstance(declared, int) or declared != actual:
            raise ValueError("input row_counts do not match consumed payloads")
    return bundle, snapshot


def _base(base_file):
    if not base_file.is_file():
        raise ValueError("required BASE config file does not exist")
    if base_file.stat().st_size > 16_384:
        raise ValueError("BASE config exceeds 16 KiB")
    payload = base_file.read_bytes()
    values = _object(payload)
    config = ScenarioConfig.from_mapping(values)
    if values != BASE or config.to_dict() != BASE:
        raise ValueError("BASE must contain the nine fixed precommitted values")
    return payload, config


def classify_prior_closes(closes) -> list[dict]:
    """Label observation j exclusively from its 21 preceding stored closes."""
    prices = tuple(closes)
    if any(not isinstance(price, Decimal) or not price.is_finite()
           or not Decimal("0.000000000001") <= price <= Decimal("1000000000000")
           or price.as_tuple().exponent < -12 or len(price.as_tuple().digits) > 28 for price in prices):
        raise ValueError("classification requires closes within the public engine's finite Decimal price bounds")
    result = []
    with localcontext(ARITHMETIC):
        for index in range(len(prices)):
            if index < 21:
                result.append(dict(regime="UNKNOWN", prefix_start_index=None, prefix_end_index=None,
                                   signed_endpoint_change=None, absolute_path_change=None, efficiency_ratio=None))
                continue
            first, last = index - 21, index - 1
            net = prices[last] - prices[first]
            path = sum((abs(prices[k] - prices[k - 1]) for k in range(first + 1, index)), Decimal(0))
            if path == 0:
                label = "FLAT"
            elif 5 * abs(net) >= 2 * path:
                label = "TREND_UP" if net > 0 else "TREND_DOWN"
            elif 5 * abs(net) <= path:
                label = "RANGE"
            else:
                label = "MIXED"
            result.append(dict(regime=label, prefix_start_index=first, prefix_end_index=last,
                               signed_endpoint_change=_text(net), absolute_path_change=_text(path),
                               efficiency_ratio=_text(abs(net) / path) if path else None))
    return result


def _population(bundle):
    declarations, instances = {}, set()
    for record in bundle.contracts:
        symbol = record.get("raw_symbol")
        if (not isinstance(symbol, str) or re.fullmatch(r"SHFE\.(?i:rb)[0-9]{2}(?:0[1-9]|1[0-2])", symbol) is None
                or record.get("product") != "SHFE.RB"):
            raise ValueError("population requires only concrete SHFE.RB futures")
        if symbol in declarations:
            raise ValueError("ambiguous raw contract symbol")
        try:
            expiry = datetime.fromisoformat(record["expiry_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("contract requires timezone-aware expiry_at") from exc
        instance = record.get("instance_id")
        if expiry.utcoffset() is None or instance != f"{symbol}|{expiry.isoformat()}" or instance in instances:
            raise ValueError("contract full instance is inconsistent or ambiguous")
        if any(name in record and record[name] != "FUTURE" for name in ("asset_class", "instrument_class", "ins_class")):
            raise ValueError("non-future declaration")
        declarations[symbol] = record
        instances.add(instance)
    histories = {symbol: [] for symbol in declarations}
    for bar in bundle.bars:
        if bar.symbol not in histories:
            raise ValueError("bar has no unique declared RB instance")
        if (isinstance(bar.datetime_nano, bool) or not isinstance(bar.datetime_nano, int) or bar.datetime_nano <= 0
                or bar.duration_seconds != 86400 or bar.ts.utcoffset() is None):
            raise ValueError("bar requires positive integer nano and timezone-aware daily identity")
        prices = (bar.open, bar.high, bar.low, bar.close)
        if any(not isinstance(price, Decimal) or not price.is_finite() or price <= 0 for price in prices):
            raise ValueError("bar OHLC must be finite positive Decimal values")
        if not bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high:
            raise ValueError("invalid OHLC envelope")
        histories[bar.symbol].append(bar)
    if not bundle.bars:
        raise ValueError("attribution needs at least one daily observation")
    for symbol, history in histories.items():
        history.sort(key=lambda bar: bar.datetime_nano)
        if len(history) > 200_000 or any(a.datetime_nano == b.datetime_nano for a, b in zip(history, history[1:])):
            raise ValueError("duplicate or excessive daily observations")
    return declarations, histories


def _coverage(bundle, declarations, histories, labels):
    dates = {window: defaultdict(list) for window in WINDOWS}
    total_labels = {window: Counter() for window in WINDOWS}
    gap_counts, gaps, records = Counter(), [], []
    for symbol in sorted(declarations):
        bars = histories[symbol]
        label_rows = labels[symbol]
        counts = {window: Counter(row["regime"] for j, row in enumerate(label_rows) if window == "ALL" or j >= 60) for window in WINDOWS}
        for window in WINDOWS:
            total_labels[window].update(counts[window])
        for index, bar in enumerate(bars):
            for window in WINDOWS:
                if window == "ALL" or index >= 60:
                    dates[window][bar.ts.date().isoformat()].append((symbol, label_rows[index]["regime"]))
        for previous, current in zip(bars, bars[1:]):
            days = (current.ts.date() - previous.ts.date()).days
            gap_counts[days] += 1
            if days > 1:
                gaps.append(dict(symbol=symbol, previous_raw_ts=previous.ts.isoformat(), current_raw_ts=current.ts.isoformat(), raw_date_gap_days=days))
        records.append(dict(symbol=symbol, contract_instance_id=declarations[symbol]["instance_id"],
                            observations=len(bars), primary_observations=max(0, len(bars) - 60),
                            interval_status="HAS_INTERVAL_OBSERVATIONS" if bars else "NO_INTERVAL_OBSERVATIONS",
                            primary_status="OBSERVED_DESCRIPTIVE_ONLY" if len(bars) > 60 else "INSUFFICIENT",
                            first_raw_ts=bars[0].ts.isoformat() if bars else None,
                            last_raw_ts=bars[-1].ts.isoformat() if bars else None,
                            raw_date_count=len({bar.ts.date() for bar in bars}),
                            regime_counts={window: {label: counts[window][label] for label in LABELS} for window in WINDOWS},
                            strategy_coverage={strategy: dict(required_bars=required, potential_next_open_slots=max(0, len(bars) - required)) for strategy, required, _ in STRATEGIES}))
    date_coverage = {}
    for window in WINDOWS:
        rows = []
        for day, values in sorted(dates[window].items()):
            symbols = sorted({symbol for symbol, _ in values})
            rows.append(dict(raw_date=day, symbols=symbols, contract_count=len(symbols),
                             regime_counts={label: sum(regime == label for _, regime in values) for label in LABELS}))
        date_coverage[window] = dict(unique_raw_dates=len(rows), maximum_contracts_same_raw_date=max((row["contract_count"] for row in rows), default=0), dates=rows)
    observed = sum(bool(bars) for bars in histories.values())
    primary = sum(len(bars) > 60 for bars in histories.values())
    return {**SAFETY, "schema_version": "rb_daily_causal_regime_coverage_v1", "rule_id": RULE_ID,
            "label_order": list(LABELS), "bar_count": len(bundle.bars), "declared_contracts": len(declarations),
            "contracts_with_bars": observed, "declared_without_interval_bars": len(declarations) - observed,
            "accounts": observed * 3, "attribution_rows": len(bundle.bars) * 3,
            "primary_contracts": primary, "observed_contracts_without_primary": observed - primary,
            "primary_contract_observations": sum(max(0, len(bars) - 60) for bars in histories.values()),
            "primary_attribution_rows": sum(max(0, len(bars) - 60) for bars in histories.values()) * 3,
            "short_prefix_observations": sum(min(21, len(bars)) for bars in histories.values()),
            "contracts_at_most_60_observations": [symbol for symbol in sorted(histories) if 0 < len(histories[symbol]) <= 60],
            "regime_counts": {window: {label: total_labels[window][label] for label in LABELS} for window in WINDOWS},
            "date_coverage": date_coverage, "contracts": records,
            "raw_date_gap_distribution": {str(gap): gap_counts[gap] for gap in sorted(gap_counts)},
            "raw_date_gaps_over_one_day": gaps, "calendar_status": "CALENDAR_UNVERIFIED",
            "gap_interpretation": "raw_stored_date_intervals_only_not_missing_trading_sessions",
            "primary_mask": "j>=60_without_account_reset", "window_unit": "stored_observations"}


def _verify_real_population(coverage, snapshot):
    if snapshot["manifest.json"] != REAL_MANIFEST_SHA256:
        return
    counts = {row["symbol"]: row["observations"] for row in coverage["contracts"] if row["observations"]}
    checks = [counts == REAL_COUNTS, coverage["declared_contracts"] == 84, coverage["contracts_with_bars"] == 17,
              coverage["declared_without_interval_bars"] == 67, coverage["bar_count"] == 2530,
              coverage["accounts"] == 51, coverage["attribution_rows"] == 7590,
              coverage["primary_attribution_rows"] == 4785, coverage["primary_contracts"] == 14,
              coverage["observed_contracts_without_primary"] == 3,
              coverage["date_coverage"]["ALL"]["unique_raw_dates"] == 263,
              coverage["date_coverage"]["ALL"]["maximum_contracts_same_raw_date"] == 12,
              coverage["date_coverage"]["PRIMARY"]["unique_raw_dates"] == 203,
              coverage["date_coverage"]["PRIMARY"]["maximum_contracts_same_raw_date"] == 10]
    for window in WINDOWS:
        checks.append(tuple(coverage["regime_counts"][window][label] for label in LABELS) == REAL_LABEL_COUNTS[window])
    if not all(checks):
        raise ValueError("real input population or precommitted price-only coverage mismatch")


def _csv_bytes(rows, columns):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow("" if row[name] is None else "true" if row[name] is True else "false" if row[name] is False else str(row[name]) for name in columns)
    return stream.getvalue().encode("utf-8")


def _ledger(payload, columns):
    records = list(csv.reader(io.StringIO(payload.decode("utf-8"), newline=""), strict=True))
    if not records or tuple(records[0]) != columns or any(len(row) != len(columns) for row in records[1:]):
        raise ValueError("child ledger has invalid exact schema or row width")
    rows = [dict(zip(columns, row)) for row in records[1:]]
    if _csv_bytes(rows, columns) != payload:
        raise ValueError("child ledger is not canonical UTF-8 CSV")
    return rows


def attribute_account(bars, label_rows, equity, decisions, fills, *, symbol, instance, strategy, config, report):
    """Validate published ledger identities and attribute their exact changes."""
    with localcontext(ARITHMETIC):
        return _attribute_account(bars, label_rows, equity, decisions, fills, symbol=symbol, instance=instance,
                                  strategy=strategy, config=config, report=report)


def _attribute_account(bars, label_rows, equity, decisions, fills, *, symbol, instance, strategy, config, report):
    if len(bars) != len(label_rows) or len(equity) != len(bars) or len(decisions) != len(bars):
        raise ValueError("account has missing or extra observation rows")
    by_index, fill_ids = defaultdict(list), set()
    last_index = -1
    for fill in fills:
        index = _integer(fill["observation_index"])
        if not 0 <= index < len(bars) or index < last_index or fill["fill_id"] in fill_ids:
            raise ValueError("fill identity is duplicate, unordered or outside account")
        last_index = index
        fill_ids.add(fill["fill_id"])
        bar = bars[index]
        if _integer(fill["datetime_nano"]) != bar.datetime_nano or fill["raw_ts"] != bar.ts.isoformat():
            raise ValueError("fill raw identity does not match account input")
        if fill["phase"] == "terminal_close":
            if index != len(bars) - 1 or fill["offset"] != "close" or fill["signal_index"] != "":
                raise ValueError("invalid terminal close identity")
            ideal = bar.close
        elif fill["phase"] == "next_open":
            if index == 0 or _integer(fill["signal_index"]) != index - 1:
                raise ValueError("fill does not follow preceding completed observation")
            ideal = bar.open
        else:
            raise ValueError("unsupported fill phase")
        side, lots = _integer(fill["side"]), _integer(fill["lots"])
        if side not in (-1, 1) or lots != config.lots or fill["offset"] not in ("open", "close"):
            raise ValueError("fill direction, quantity or offset mismatch")
        fee = _decimal(fill["fee"])
        if (fee != _decimal(fill["fee_per_lot_component"]) + _decimal(fill["fee_rate_component"])
                or _decimal(fill["fee_per_lot_component"]) != config.fee_per_lot * lots
                or _decimal(fill["fee_rate_component"]) != config.fee_rate * ideal * config.multiplier * lots
                or _decimal(fill["ideal_price"]) != ideal
                or _decimal(fill["effective_price"]) != ideal + side * config.price_tick * config.slippage_ticks
                or _decimal(fill["slippage"]) != config.price_tick * config.slippage_ticks * config.multiplier * lots):
            raise ArithmeticError("fill fees, ideal prices or single-counted slippage do not reconcile")
        by_index[index].append(fill)
    previous = dict(equity=config.initial_cash, cash=config.initial_cash, realized_pnl=Decimal(0),
                    gross_pnl=Decimal(0), fees=Decimal(0), slippage=Decimal(0), position_lots=0)
    result = []
    for index, (bar, mark, decision, label) in enumerate(zip(bars, equity, decisions, label_rows)):
        for row in (mark, decision):
            if (_integer(row["observation_index"]) != index or _integer(row["datetime_nano"]) != bar.datetime_nano
                    or row["raw_ts"] != bar.ts.isoformat()):
                raise ValueError("equity/decision identity is missing, duplicate or cross-account")
        terminal = index == len(bars) - 1
        if (_boolean(mark["terminal"]) != terminal or _boolean(decision["final_observation"]) != terminal
                or _decimal(mark["close"]) != bar.close or decision["clock_status"] != "observation_only"):
            raise ValueError("account mark/decision does not match the complete trajectory")
        amounts = {name: _decimal(mark[name]) for name in ("equity", "gross_pnl", "fees", "slippage")}
        net = amounts["equity"] - previous["equity"]
        gross = amounts["gross_pnl"] - previous["gross_pnl"]
        fees = amounts["fees"] - previous["fees"]
        slip = amounts["slippage"] - previous["slippage"]
        legs = by_index[index]
        if (gross != _decimal(mark["daily_gross_pnl"]) or net != gross - fees - slip
                or fees != sum((_decimal(fill["fee"]) for fill in legs), Decimal(0))
                or slip != sum((_decimal(fill["slippage"]) for fill in legs), Decimal(0))
                or amounts["equity"] != _decimal(mark["cash"]) + _decimal(mark["unrealized_pnl"])
                or amounts["gross_pnl"] != _decimal(mark["realized_pnl"]) + _decimal(mark["unrealized_pnl"])
                or _decimal(mark["cash"]) != config.initial_cash + _decimal(mark["realized_pnl"]) - amounts["fees"] - amounts["slippage"]
                or _decimal(mark["net_pnl"]) != amounts["equity"] - config.initial_cash):
            raise ArithmeticError("exact daily equity, fill-cost or cash reconciliation failed")
        position = previous["position_lots"]
        running_cash, realized = previous["cash"], previous["realized_pnl"]
        for fill in legs:
            side, quantity = _integer(fill["side"]), _integer(fill["lots"])
            if fill["offset"] == "open" and position != 0 or fill["offset"] == "close" and position != -side * quantity:
                raise ArithmeticError("fill position flow does not reconcile")
            position += side * quantity
            if position != _integer(fill["position_after"]):
                raise ArithmeticError("fill position_after does not reconcile")
            change = _decimal(fill["realized_pnl"])
            if fill["offset"] == "open" and change != 0:
                raise ArithmeticError("opening fill cannot realize PnL")
            realized += change
            running_cash += change - _decimal(fill["fee"]) - _decimal(fill["slippage"])
            if running_cash != _decimal(fill["cash_after"]):
                raise ArithmeticError("fill cash_after does not reconcile")
        if position != _integer(mark["position_lots"]) or terminal and position != 0:
            raise ArithmeticError("mark position or terminal flat identity failed")
        if running_cash != _decimal(mark["cash"]) or realized != _decimal(mark["realized_pnl"]):
            raise ArithmeticError("fill realized PnL/cash does not match closing mark")
        terminal_legs = [fill for fill in legs if fill["phase"] == "terminal_close"]
        first, last = label["prefix_start_index"], label["prefix_end_index"]
        result.append(dict(symbol=symbol, contract_instance_id=instance, strategy=strategy, observation_index=index,
                           datetime_nano=bar.datetime_nano, raw_ts=bar.ts.isoformat(), raw_date=bar.ts.date().isoformat(),
                           rule_id=RULE_ID, **label, prefix_start_datetime_nano=bars[first].datetime_nano if first is not None else None,
                           prefix_end_datetime_nano=bars[last].datetime_nano if last is not None else None,
                           primary=index >= 60, terminal=terminal, prior_close_position_lots=previous["position_lots"],
                           position_lots=position, active_observation=bool(previous["position_lots"] or legs),
                           fill_legs=len(legs), terminal_close_legs=len(terminal_legs),
                           gross_delta=_text(gross), fee_delta=_text(fees), slippage_delta=_text(slip), net_delta=_text(net),
                           terminal_close_fees=_text(sum((_decimal(fill["fee"]) for fill in terminal_legs), Decimal(0))),
                           terminal_close_slippage=_text(sum((_decimal(fill["slippage"]) for fill in terminal_legs), Decimal(0))),
                           **{name: _text(number) for name, number in amounts.items()}))
        previous = dict(amounts, position_lots=position, cash=running_cash, realized_pnl=realized)
    for name, expected in (("gross_pnl", previous["gross_pnl"]), ("fees", previous["fees"]),
                           ("slippage", previous["slippage"]), ("net_pnl", previous["equity"] - config.initial_cash),
                           ("ending_equity", previous["equity"]), ("ending_cash", previous["equity"])):
        if _decimal(report[name]) != expected:
            raise ArithmeticError("account daily totals do not match child report")
    if report["ending_position_lots"] != 0 or report["fills"] != len(fills):
        raise ArithmeticError("account report position/fill total mismatch")
    return result


def _child(child, relative, bars, label_rows, record, strategy, parameters, config, snapshot, bundle, config_hash, code_hashes):
    if {member.name for member in child.iterdir()} != {*CHILD_PAYLOADS, "manifest.json"}:
        raise ValueError("child must contain exactly the six public engine artifacts")
    payloads = {}
    for name in (*CHILD_PAYLOADS, "manifest.json"):
        path = child / name
        if _path(path, output=True) != path or not path.is_file():
            raise ValueError("unsafe child artifact")
        payloads[name] = path.read_bytes()
    manifest, report, published_config = (_object(payloads[name]) for name in ("manifest.json", "report.json", "config.json"))
    if (manifest.get("status") != "complete" or manifest.get("content_hashes") != {name: _digest(payloads[name]) for name in CHILD_PAYLOADS}
            or manifest.get("input_manifest_sha256") != snapshot["manifest.json"]
            or manifest.get("input_content_hashes") != bundle.manifest["content_hashes"]
            or manifest.get("input_config_sha256") != config_hash
            or manifest.get("source_hashes") != bundle.manifest.get("source_hashes", {})
            or manifest.get("code_hashes") != {name: code_hashes[name] for name in ENGINE_CODE_PATHS}):
        raise ValueError("child manifest pins, payload hashes or completion mismatch")
    for document in (manifest, report):
        if (document.get("schema_version") != "futures_daily_scenario_v1"
                or document.get("historical_strict_execution") is not False
                or document.get("assumptions_verification") != "not_historical_verified"
                or document.get("dataset_kind") != "historical_single_contract_daily_scenario"):
            raise ValueError("child is outside the SCENARIO evidence boundary")
    for field in ("observations", "fills", "closed_trades", "turnover_lots", "opening_blocks",
                  "margin_breach_observations", "nonpositive_equity_observations", "ending_position_lots"):
        value = report.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("child report count must be a nonnegative integer")
    for document in (published_config, report):
        if (document.get("symbol") != record["symbol"] or document.get("contract_instance_id") != record["contract_instance_id"]
                or document.get("strategy") != strategy):
            raise ValueError("cross-account child identity mismatch")
    if (published_config.get("assumptions") != config.to_dict() or published_config.get("strategy_parameters") != parameters
            or report.get("status") != "complete" or report.get("observations") != len(bars)
            or report.get("first_datetime_nano") != bars[0].datetime_nano or report.get("last_datetime_nano") != bars[-1].datetime_nano):
        raise ValueError("child config/report differs from fixed full account")
    reconciliation = report.get("reconciliation")
    if not isinstance(reconciliation, dict) or not reconciliation or any(value is not True for value in reconciliation.values()):
        raise ArithmeticError("child reconciliation is not all true")
    with localcontext(ARITHMETIC):
        daily = attribute_account(bars, label_rows, _ledger(payloads["equity.csv"], EQUITY_COLUMNS),
                                  _ledger(payloads["decisions.csv"], DECISION_COLUMNS), _ledger(payloads["fills.csv"], FILL_COLUMNS),
                                  symbol=record["symbol"], instance=record["contract_instance_id"], strategy=strategy, config=config, report=report)
    account = dict(symbol=record["symbol"], contract_instance_id=record["contract_instance_id"], strategy=strategy,
                   observations=len(bars), child_directory=relative, report=report,
                   account_max_drawdown=report["max_drawdown"], account_max_drawdown_ratio=report["max_drawdown_ratio"])
    return daily, account, {f"{relative}/{name}": _digest(payload) for name, payload in payloads.items()}


def contribution_drawdown(deltas) -> Decimal:
    """Maximum decline of a monetary contribution curve anchored at zero."""
    with localcontext(ARITHMETIC):
        cumulative = peak = worst = Decimal(0)
        for delta in deltas:
            if not isinstance(delta, Decimal) or not delta.is_finite():
                raise ValueError("contribution steps must be finite Decimal amounts")
            cumulative += delta
            peak = max(peak, cumulative)
            worst = max(worst, peak - cumulative)
        return worst


def summarize_account(rows) -> list[dict]:
    if not rows:
        raise ValueError("cannot summarize an absent account")
    result = []
    with localcontext(ARITHMETIC):
        for window in WINDOWS:
            population = [row for row in rows if window == "ALL" or row["primary"]]
            for label in LABELS:
                selected = [row for row in population if row["regime"] == label]
                total = {name: sum((_decimal(row[source]) for row in selected), Decimal(0)) for name, source in (
                    ("gross_pnl", "gross_delta"), ("fees", "fee_delta"), ("slippage", "slippage_delta"), ("net_pnl", "net_delta"),
                    ("terminal_close_fees", "terminal_close_fees"), ("terminal_close_slippage", "terminal_close_slippage"))}
                if total["net_pnl"] != total["gross_pnl"] - total["fees"] - total["slippage"]:
                    raise ArithmeticError("regime group money does not reconcile")
                reason = "PREFIX_LT_21" if label == "UNKNOWN" and selected else "NO_PRIMARY_OBSERVATIONS" if not population and window == "PRIMARY" else "NO_ROWS_IN_REGIME" if not selected else None
                result.append(dict(symbol=rows[0]["symbol"], contract_instance_id=rows[0]["contract_instance_id"], strategy=rows[0]["strategy"],
                                   window=window, regime=label, coverage_status="OBSERVED_DESCRIPTIVE_ONLY" if selected else "INSUFFICIENT",
                                   reason=reason, statistical_qualification="NOT_ASSESSED", observation_count=len(selected),
                                   raw_date_count=len({row["raw_date"] for row in selected}), active_observations=sum(row["active_observation"] for row in selected),
                                   fill_legs=sum(row["fill_legs"] for row in selected), terminal_observations=sum(row["terminal"] for row in selected),
                                   terminal_close_legs=sum(row["terminal_close_legs"] for row in selected), **{name: _text(amount) for name, amount in total.items()},
                                   contribution_max_drawdown=_text(contribution_drawdown(_decimal(row["net_delta"]) if row["regime"] == label else Decimal(0) for row in population)),
                                   mean_net_delta=_text(total["net_pnl"] / len(selected)) if selected else None))
            groups = result[-len(LABELS):]
            if sum(group["observation_count"] for group in groups) != len(population):
                raise ArithmeticError("regime groups do not partition window observations")
            for field, source in (("gross_pnl", "gross_delta"), ("fees", "fee_delta"), ("slippage", "slippage_delta"), ("net_pnl", "net_delta")):
                if sum((_decimal(group[field]) for group in groups), Decimal(0)) != sum((_decimal(row[source]) for row in population), Decimal(0)):
                    raise ArithmeticError("regime group amounts do not partition window")
            expected = _decimal(rows[-1]["equity"]) - (_decimal(rows[59]["equity"]) if window == "PRIMARY" and len(rows) > 60 else Decimal(BASE["initial_cash"]))
            if population and sum((_decimal(row["net_delta"]) for row in population), Decimal(0)) != expected:
                raise ArithmeticError("window net amount does not telescope to equity anchor")
    return result


def _distribution(values):
    if not values:
        return dict(min=None, median=None, max=None)
    values = sorted(values)
    middle = len(values) // 2
    median = values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2
    return dict(min=_text(values[0]), median=_text(median), max=_text(values[-1]))


def _summary(daily, group_rows, accounts, coverage):
    descriptions, comparisons = [], []
    contract_count = coverage["contracts_with_bars"]
    group_lookup = {(row["symbol"], row["strategy"], row["window"], row["regime"]): row for row in group_rows}
    with localcontext(ARITHMETIC):
        for strategy, _, _ in STRATEGIES:
            for window in WINDOWS:
                for label in LABELS:
                    population = [row for row in group_rows if row["strategy"] == strategy and row["window"] == window and row["regime"] == label and row["observation_count"]]
                    nets = [_decimal(row["net_pnl"]) for row in population]
                    raw_dates = {row["raw_date"] for row in daily if row["strategy"] == strategy and row["regime"] == label and (window == "ALL" or row["primary"])}
                    descriptions.append(dict(strategy=strategy, window=window, regime=label,
                                             coverage_status="OBSERVED_DESCRIPTIVE_ONLY" if population else "INSUFFICIENT",
                                             reason="PREFIX_LT_21" if label == "UNKNOWN" and population else "NO_ROWS_IN_REGIME" if not population else None,
                                             statistical_qualification="NOT_ASSESSED", contracts_with_rows=len(population), total_contracts=contract_count,
                                             contracts_without_rows=contract_count - len(population), observation_count=sum(row["observation_count"] for row in population),
                                             raw_date_count=len(raw_dates), positive_net_contracts=sum(net > 0 for net in nets), zero_net_contracts=sum(net == 0 for net in nets),
                                             negative_net_contracts=sum(net < 0 for net in nets), no_active_contracts=sum(row["active_observations"] == 0 for row in population),
                                             net_contribution=_distribution(nets), contribution_max_drawdown=_distribution([_decimal(row["contribution_max_drawdown"]) for row in population]),
                                             contract_contributions=[{name: row[name] for name in ("symbol", "net_pnl", "contribution_max_drawdown", "observation_count", "active_observations")} for row in population]))
        for left, right in PAIRS:
            for label in LABELS:
                population = []
                for record in coverage["contracts"]:
                    symbol = record["symbol"]
                    if not record["observations"]:
                        continue
                    a, b = (group_lookup[(symbol, strategy, "PRIMARY", label)] for strategy in (left, right))
                    if a["observation_count"] != b["observation_count"] or a["raw_date_count"] != b["raw_date_count"]:
                        raise ArithmeticError("paired PRIMARY masks differ between strategies")
                    if a["observation_count"]:
                        population.append(dict(symbol=symbol, difference=_text(_decimal(a["net_pnl"]) - _decimal(b["net_pnl"]))))
                differences = [_decimal(row["difference"]) for row in population]
                raw_dates = {row["raw_date"] for row in daily if row["strategy"] == left and row["primary"] and row["regime"] == label}
                comparisons.append(dict(left_strategy=left, right_strategy=right, window="PRIMARY", regime=label,
                                        coverage_status="OBSERVED_DESCRIPTIVE_ONLY" if population else "INSUFFICIENT", statistical_qualification="NOT_ASSESSED",
                                        contracts_with_rows=len(population), total_contracts=contract_count, raw_date_count=len(raw_dates),
                                        net_contribution_difference=_distribution(differences), positive_differences=sum(value > 0 for value in differences),
                                        zero_differences=sum(value == 0 for value in differences), negative_differences=sum(value < 0 for value in differences),
                                        contract_differences=population))
    return {**SAFETY, "schema_version": "rb_daily_causal_regime_summary_v1", "rule_id": RULE_ID,
            "strategy_order": [strategy for strategy, _, _ in STRATEGIES], "window_order": list(WINDOWS), "label_order": list(LABELS),
            "accounts": accounts, "account_count": len(accounts), "attribution_rows": len(daily), "regime_summary_rows": len(group_rows),
            "primary_attribution_rows": sum(row["primary"] for row in daily), "groups": descriptions, "paired_primary_differences": comparisons,
            "decimal_precision": 128, "ratio_rounding": "ROUND_HALF_EVEN", "primary_mask": "j>=60_without_account_reset",
            "distribution_population": "contracts_with_rows_in_the_same_price_mask_including_no_activity",
            "drawdown_basis": "separate_zero_anchored_state_contribution_curves_not_account_or_portfolio_risk",
            "cross_contract_population": "correlated_overlapping_contracts_not_independent_opportunities"}


def _report(summary, coverage, *, synthetic):
    def show(distribution):
        return " / ".join("null" if distribution[key] is None else distribution[key] for key in ("min", "median", "max"))
    source = "明确标记 SYNTHETIC 的测试价格" if synthetic else "冻结 RB 真实导出观测"
    lines = [f"{source}已实际完成 {summary['account_count']} 个独立 BASE 账户的因果状态归因：{summary['attribution_rows']} 行 ALL，其中 {summary['primary_attribution_rows']} 行 PRIMARY。",
             f"{coverage['primary_contracts']} 个合约有 j≥60 的 PRIMARY 观测，{coverage['observed_contracts_without_primary']} 个有行情短合约无 PRIMARY；全部账户及空状态组仍保留。",
             "", "全部结果是 SCENARIO 回顾性描述：historical_strict_execution=false、assumptions_verification=not_historical_verified、statistical_qualification=NOT_ASSESSED；broker=NOT_RUN、enable_trading=false，真实下单/撤单调用均为0。" ,
             "这一区间和既有 BASE 结果已看过，不构成样本外、最终留出、历史 PIT、独立统计证据或 v2 策略资格。" if not synthetic else "所有 SYNTHETIC 价格仅验证实现契约；real_export 是旧引擎所需的运输标签，不赋予这些价格真实历史身份。",
             "", "状态规则 prior21_er20_v1 只看本观测之前21个收盘价，以20段价格路径的方向效率固定分组：≥0.4为上/下趋势、≤0.2为区间、其余混合；平价为FLAT，前缀不足为UNKNOWN。它与策略内部行情启发式分开，标签从不驱动交易。",
             "PRIMARY 是完整自然暖机轨迹的报告掩码，j=59收盘以后开始累计，存量持仓照实结转。每合约账户从100万/固定1手独立开始；自然暖机为41/60/21观测，不是统一风险预算。",
             "", "PRIMARY 的价格覆盖（价格观测不按三策略重复计数；raw日期不是已核验交易日日历）：", "",
             "| 状态 | 合约观测 | 去重raw日期 | 有行合约 |", "| --- | ---: | ---: | ---: |"]
    for label in LABELS:
        dates = [row for row in coverage["date_coverage"]["PRIMARY"]["dates"] if row["regime_counts"][label]]
        count = sum(record["regime_counts"]["PRIMARY"][label] > 0 for record in coverage["contracts"])
        lines.append(f"| {label} | {coverage['regime_counts']['PRIMARY'][label]} | {len(dates)} | {count} |")
    lines += ["", f"PRIMARY 共{coverage['date_coverage']['PRIMARY']['unique_raw_dates']}个raw日期，同日最多{coverage['date_coverage']['PRIMARY']['maximum_contracts_same_raw_date']}合约；ALL共{coverage['date_coverage']['ALL']['unique_raw_dates']}个raw日期，同日最多{coverage['date_coverage']['ALL']['maximum_contracts_same_raw_date']}合约。详见[coverage.json](coverage.json)的逐日symbol集合、原始日期间隔及CALENDAR_UNVERIFIED；不能把周末/假日自动认定为缺失或补齐。",
              "", "PRIMARY 状态净损益贡献与贡献回撤（金额列为各合约 min / median / max；n包含有状态而无成交的账户）：", "",
              "| 策略 | 状态 | n | 净贡献 min / median / max | 贡献回撤 min / median / max | 正/零/负合约 | 无活动合约 |",
              "| --- | --- | ---: | --- | --- | --- | ---: |"]
    for group in summary["groups"]:
        if group["window"] == "PRIMARY":
            lines.append(f"| {group['strategy']} | {group['regime']} | {group['contracts_with_rows']} | {show(group['net_contribution'])} | {show(group['contribution_max_drawdown'])} | {group['positive_net_contracts']}/{group['zero_net_contracts']}/{group['negative_net_contracts']} | {group['no_active_contracts']} |")
    lines += ["", "PRIMARY 固定配对净贡献差（左策略−右策略，同一symbol与价格标签；描述相关且区间重叠合约）：", "",
              "| 固定配对 | 状态 | n | 去重raw日期 | 差值 min / median / max | 正/零/负差值 |", "| --- | --- | ---: | ---: | --- | --- |"]
    for pair in summary["paired_primary_differences"]:
        lines.append(f"| {pair['left_strategy']}−{pair['right_strategy']} | {pair['regime']} | {pair['contracts_with_rows']} | {pair['raw_date_count']} | {show(pair['net_contribution_difference'])} | {pair['positive_differences']}/{pair['zero_differences']}/{pair['negative_differences']} |")
    lines += ["", "UNKNOWN保存前缀不足的未分类金额；FLAT或其他无行组仍以INSUFFICIENT保留，加法金额和贡献回撤为0、均值/分布为null。n=60无PRIMARY，n=61才有一行；没有样本数门槛授予统计充分性。",
              "每行净增量严格等于毛增量减本观测手续费与一次滑点支出。跨状态持仓按每日增量分摊，不把整笔交易归给入场/出场标签。本观测所有开盘和终点成交腿均归给其事前标签；terminal_close_fees/slippage仅记录真正终点平仓腿，不把整根末观测损益称为强平损益。",
              "贡献回撤按零起点、全部时间顺序累计该状态净增量，其余状态步长为0且不重置峰值。六状态净贡献可相加，六状态最大回撤不可相加，也不是只在该状态开仓的新回测风险。完整单账户max_drawdown/max_drawdown_ratio单独保存在summary账户条目。",
              "价格相关、日期重叠，合约不是独立机会；不生成组合收益/拼接NAV、跨合约回撤、年化、Sharpe、显著性、胜率资格、排名选优或资金建议。合约身份为静态重建，上市历史、会话、available_at、经济参数及细粒度可成交性未验证。",
              "", "[daily_attribution.csv](daily_attribution.csv)保存逐观测前缀、金额和终点成本；[regime_summary.csv](regime_summary.csv)保存每账户ALL/PRIMARY的完整六组；[summary.json](summary.json)保存单账户原报告和固定配对分母；[manifest.json](manifest.json)绑定全部原始产物、输入和源码。", "", "单账户原始账本：", ""]
    for account in summary["accounts"]:
        directory = account["child_directory"]
        lines.append(f"- {account['symbol']} / {account['strategy']} / BASE: [report]({directory}/report.json), [fills]({directory}/fills.csv), [equity]({directory}/equity.csv), [decisions]({directory}/decisions.csv)")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _write_new(root, name, payload, hashes):
    target = _path(root / name, output=True)
    with target.open("xb") as handle:
        handle.write(payload)
    expected = _digest(payload)
    if sha256_file(target) != expected:
        raise OSError(f"output hash verification failed: {name}")
    hashes[name] = expected


def _verify_tree(root, hashes, *, marker=False):
    found = set()
    for member in root.rglob("*"):
        _path(member, output=True)
        if member.is_file():
            found.add(member.relative_to(root).as_posix())
        elif not member.is_dir():
            raise ValueError("non-file artifact in output tree")
    if found != set(hashes) | ({"manifest.json"} if marker else set()):
        raise ValueError("output tree does not have exact manifest closure")
    if any(sha256_file(root / name) != digest for name, digest in hashes.items()):
        raise ValueError("published output bytes drifted")


class _Incomplete(RuntimeError):
    def __init__(self, cause):
        super().__init__(str(cause))
        self.cause = cause


def run_causal_regime(input_bundle: Path, base_config: Path, output: Path) -> dict:
    """Publish every BASE account and its descriptive causal attribution."""
    source, base_file = _path(input_bundle), _path(base_config)
    destination = _path(output, output=True)
    forbidden = (source, base_file, *(ROOT / name for name in ("src", "scripts", "tests", "config", "tools", "docs")))
    assert_output_directory_is_new(destination, forbidden)
    code_hashes = _code_hashes()
    base_bytes, config = _base(base_file)
    bundle, snapshot = _read_bundle(source)
    declarations, histories = _population(bundle)
    labels = {symbol: classify_prior_closes(bar.close for bar in histories[symbol]) for symbol in sorted(histories)}
    coverage = _coverage(bundle, declarations, histories, labels)
    _verify_real_population(coverage, snapshot)
    base_hash = _digest(base_bytes)
    config_bytes = canonical_json_bytes(config.to_dict())
    config_hash = _digest(config_bytes)
    _unchanged(source, snapshot, base_file, base_hash, code_hashes)
    provenance = {**SAFETY, "schema_version": "rb_daily_causal_regime_v1", "dataset_kind": "rb_daily_prefix_regime_attribution",
                  "rule_id": RULE_ID, "input_manifest_sha256": snapshot["manifest.json"], "input_content_hashes": bundle.manifest["content_hashes"],
                  "input_file_hashes": snapshot, "input_base_config_sha256": base_hash, "derived_config_hashes": {"BASE": config_hash},
                  "code_hashes": code_hashes, "base_assumptions": config.to_dict(), "accounts": coverage["accounts"],
                  "attribution_rows": coverage["attribution_rows"], "primary_attribution_rows": coverage["primary_attribution_rows"],
                  "calendar_status": "CALENDAR_UNVERIFIED"}
    _path(destination, output=True)
    assert_output_directory_is_new(destination, forbidden)
    destination.mkdir(parents=True, exist_ok=False)
    hashes = {}

    def incomplete(exc):
        return canonical_json_bytes({**provenance, "status": "incomplete", "content_hashes": dict(sorted(hashes.items())),
                                     "error_type": type(exc).__name__, "reason": str(exc)[:500]})

    try:
        (destination / "configs").mkdir(exist_ok=False)
        _write_new(destination, "configs/BASE.json", config_bytes, hashes)
        daily, groups, accounts = [], [], []
        with localcontext(ARITHMETIC):
            for record in coverage["contracts"]:
                symbol = record["symbol"]
                if not record["observations"]:
                    continue
                bars = histories[symbol]
                paired_labels = None
                for strategy, _, parameters in STRATEGIES:
                    _unchanged(source, snapshot, base_file, base_hash, code_hashes)
                    relative = f"cells/{symbol}/{strategy}/BASE"
                    child = _path(destination / relative, output=True)
                    run_history_scenario(source, child, symbol=symbol, strategy=strategy, config_path=destination / "configs/BASE.json")
                    rows, account, child_hashes = _child(child, relative, bars, labels[symbol], record, strategy, parameters,
                                                        config, snapshot, bundle, config_hash, code_hashes)
                    price_mask = [(row["observation_index"], row["regime"], row["primary"]) for row in rows]
                    if paired_labels is not None and price_mask != paired_labels:
                        raise ArithmeticError("three strategies do not share the same causal price mask")
                    paired_labels = price_mask
                    daily.extend(rows)
                    groups.extend(summarize_account(rows))
                    accounts.append(account)
                    hashes.update(child_hashes)
                    _unchanged(source, snapshot, base_file, base_hash, code_hashes)
            keys = {(row["symbol"], row["contract_instance_id"], row["strategy"], row["observation_index"]) for row in daily}
            if (len(keys) != len(daily) or len(daily) != coverage["attribution_rows"] or len(accounts) != coverage["accounts"]
                    or len(groups) != coverage["accounts"] * 12 or sum(row["primary"] for row in daily) != coverage["primary_attribution_rows"]):
                raise ValueError("account or attribution population has missing/duplicate rows")
            summary = _summary(daily, groups, accounts, coverage)
        payloads = {"coverage.json": canonical_json_bytes(coverage), "daily_attribution.csv": _csv_bytes(daily, DAILY_COLUMNS),
                    "regime_summary.csv": _csv_bytes(groups, GROUP_COLUMNS), "summary.json": canonical_json_bytes(summary),
                    "report.md": _report(summary, coverage, synthetic=bundle.metadata.get("evidence_level") == "SYNTHETIC")}
        for name, payload in payloads.items():
            _write_new(destination, name, payload, hashes)
        _verify_tree(destination, hashes)
        _unchanged(source, snapshot, base_file, base_hash, code_hashes)
        marker = destination / "manifest.json"
        complete = canonical_json_bytes({**provenance, "status": "complete", "content_hashes": dict(sorted(hashes.items()))})
        with marker.open("xb") as handle:
            try:
                handle.write(complete)
                handle.flush()
                if marker.read_bytes() != complete:
                    raise OSError("complete manifest read-back failed")
                _verify_tree(destination, hashes, marker=True)
                _unchanged(source, snapshot, base_file, base_hash, code_hashes)
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
    return {**SAFETY, "status": "complete", "accounts": len(accounts), "attribution_rows": len(daily),
            "primary_attribution_rows": summary["primary_attribution_rows"], "regime_summary_rows": len(groups), "output": str(destination)}


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


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
        result = run_causal_regime(args.input_bundle, args.base_config, args.output)
    except Exception as exc:
        cause = exc.cause if isinstance(exc, _Incomplete) else exc
        print(json.dumps({**SAFETY, "status": "incomplete" if isinstance(exc, _Incomplete) else "rejected",
                          "error_type": type(cause).__name__, "reason": str(cause)[:500]}, ensure_ascii=False, sort_keys=True, allow_nan=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
