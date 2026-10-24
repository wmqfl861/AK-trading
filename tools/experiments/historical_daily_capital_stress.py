"""Fixed-capital RB daily scenarios using the unchanged offline legacy engine.

This retrospective SCENARIO comparison is not a historical execution certificate,
strategy qualification, or recommendation for a safe account balance. No service,
credentials, broker, or network is used. Every account is replayed independently.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict, fields
from datetime import datetime
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
import csv
import io
import json
import math
import os
from pathlib import Path
import re
import stat
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from ak_trading.futures.io import canonical_json_bytes, read_input_bundle, sha256_bytes, sha256_file
from ak_trading.futures.scenario_backtest import (
    ScenarioConfig, ScenarioDecision, ScenarioEquity, ScenarioFill, run_history_scenario,
)
from ak_trading.futures.strategies import DonchianTrend, MATrend, ZScoreReversion

D = Decimal
ARITHMETIC = Context(prec=128, rounding=ROUND_HALF_EVEN)
SCHEMA_VERSION = "rb_daily_capital_stress_v1"
STRATEGIES = ("donchian_trend", "ma_trend", "zscore_reversion")
CAPITALS = (("C5000", "5000"), ("C15000", "15000"), ("C1000000", "1000000"))
SCENARIOS = ("BASE", "MARGIN_X1_5")
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
OWN_CODE_PATH = "tools/experiments/historical_daily_capital_stress.py"
CHILD_FILES = {"config.json", "fills.csv", "equity.csv", "decisions.csv", "report.json", "manifest.json"}
PAYLOAD_FILES = CHILD_FILES - {"manifest.json"}
EXECUTION_CLASSES = ("NO_OPENING_ATTEMPT", "ALL_ATTEMPTS_BLOCKED", "PARTLY_BLOCKED", "NO_OPENING_BLOCK")
PAIRED_METRICS = ("net_pnl", "fees", "slippage", "max_drawdown", "max_drawdown_ratio",
                  "opening_blocks", "fills", "closed_trades")
COUNT_METRICS = {"opening_blocks", "fills", "closed_trades"}
MONEY_METRICS = ("gross_pnl", "fees", "slippage", "net_pnl", "total_return", "ending_cash",
                 "ending_equity", "max_drawdown", "max_drawdown_ratio", "turnover_notional")
INVARIANT_FIELDS = ("observation_index", "datetime_nano", "raw_ts", "strategy_status", "reason",
                    "evaluator_candidate_direction", "candidate_direction", "target_lots",
                    "logical_direction", "signal_age")
BOUNDARY = dict(evidence_level="SCENARIO", historical_strict_execution=False,
                assumptions_verification="not_historical_verified", broker="NOT_RUN",
                enable_trading=False, real_send_count=0, real_cancel_count=0)


def _text(value: Decimal) -> str:
    if value == 0:
        return "0"
    result = format(value, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def _number(value, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, D)) or value == "":
        raise ValueError(f"{name} must be a finite decimal, never bool/null")
    try:
        result = D(value)
    except (ArithmeticError, ValueError) as exc:
        raise ValueError(f"invalid decimal for {name}") from exc
    if not result.is_finite():
        raise ValueError(f"nonfinite decimal for {name}")
    return result


def _integer(value, name: str, *, nullable=False) -> int | None:
    if value == "" and nullable:
        return None
    if isinstance(value, bool) or (isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value) is None):
        raise ValueError(f"invalid integer for {name}")
    if not isinstance(value, (str, int)):
        raise ValueError(f"invalid integer for {name}")
    return int(value)


def _boolean(value: str, name: str) -> bool:
    if value not in ("true", "false"):
        raise ValueError(f"invalid boolean for {name}")
    return value == "true"


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(f"nonfinite JSON constant: {value}")


def _finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("nonfinite JSON number")
    return result


def _json(payload: bytes, *, decimal=False):
    return json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object,
                      parse_constant=_reject_constant, parse_float=D if decimal else _finite_float)


def _object(payload: bytes, *, decimal=False):
    result = _json(payload, decimal=decimal)
    if not isinstance(result, dict):
        raise ValueError("JSON document must be an object")
    return result


def _safe_path(path: Path) -> Path:
    """Reject lexical aliases and every existing reparse component before resolve."""
    path = Path(path).absolute()
    current = Path(path.anchor)
    for part in path.parts[1:]:
        if part not in (".", ".."):
            stem = part.rstrip(" .").split(".", 1)[0].upper()
            if (stem in {"NUL", "CON", "PRN", "AUX", "CONIN$", "CONOUT$"}
                    or re.fullmatch(r"(?:COM|LPT)[1-9]", stem)
                    or part.rstrip(" .") != part or ":" in part):
                raise ValueError("device or aliased path component is unsupported")
        current /= part
        if os.path.lexists(current):
            info = current.lstat()
            if (stat.S_ISLNK(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024)):
                raise ValueError("symlink/junction/reparse path component is unsupported")
    return path.resolve()


def _overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _destination(source: Path, base: Path, output: Path) -> Path:
    destination = _safe_path(output)
    if os.path.lexists(destination):
        raise ValueError("output must be a new directory")
    protected = (source, base, *(ROOT / name for name in ("src", "scripts", "tests", "config", "tools", "docs")))
    if any(_overlap(destination, item) for item in protected):
        raise ValueError("output overlaps input, BASE or protected repository paths")
    return destination


def _code_hashes() -> dict[str, str]:
    return {name: sha256_file(_safe_path(ROOT / name)) for name in (*ENGINE_CODE_PATHS, OWN_CODE_PATH)}


def _capture_input(source: Path):
    manifest_bytes = _safe_path(source / "manifest.json").read_bytes()
    manifest = _object(manifest_bytes)
    if (manifest.get("schema_version") != "futures_bundle_v1" or manifest.get("status") != "complete"
            or manifest.get("dataset_kind") != "real_export"):
        raise ValueError("requires complete futures_bundle_v1 / real_export input")
    declarations = manifest.get("content_hashes")
    if not isinstance(declarations, dict):
        raise ValueError("input content_hashes must be an object")
    core = {"bars.csv", "contracts.json", "metadata.json", "availability.json"}
    if not core <= set(declarations):
        raise ValueError("missing required input payload hash")
    captured = {"manifest.json": manifest_bytes}
    for name, expected in declarations.items():
        if (not isinstance(name, str) or name in ("", ".", "..", "manifest.json")
                or re.fullmatch(r"[A-Za-z0-9_.-]+", name) is None
                or not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None):
            raise ValueError("unsafe input payload/hash declaration")
        target = _safe_path(source / name)
        if target.parent != source or not target.is_file():
            raise ValueError("required input payload is absent or outside bundle")
        captured[name] = target.read_bytes()
        if sha256_bytes(captured[name]) != expected:
            raise ValueError(f"input content hash mismatch: {name}")
    for name in ("contracts.json", "metadata.json", "availability.json"):
        value = _json(captured[name])
        if name == "contracts.json":
            if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
                raise ValueError("contracts must be a list of objects")
        elif not isinstance(value, dict):
            raise ValueError(f"{name} must be an object")
    return manifest, captured


def _population(bundle):
    declarations, grouped = {}, defaultdict(list)
    instances = set()
    for item in bundle.contracts:
        symbol = item.get("raw_symbol")
        if not isinstance(symbol, str) or re.fullmatch(r"SHFE\.rb[0-9]{4}", symbol) is None:
            raise ValueError("population requires concrete raw SHFE.rb contracts")
        if not 1 <= int(symbol[-2:]) <= 12 or item.get("product") != "SHFE.RB":
            raise ValueError("invalid RB product or delivery code")
        try:
            expiry = datetime.fromisoformat(item["expiry_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("contract requires expiry_at") from exc
        instance = item.get("instance_id")
        if expiry.utcoffset() is None or instance != f"{symbol}|{expiry.isoformat()}":
            raise ValueError("contract requires one full timezone-aware instance")
        if symbol in declarations or instance in instances:
            raise ValueError("ambiguous duplicate contract instance")
        if any(item.get(name, "FUTURE") != "FUTURE" for name in ("asset_class", "instrument_class", "ins_class")):
            raise ValueError("non-future declaration is unsupported")
        declarations[symbol] = item
        instances.add(instance)
    for bar in bundle.bars:
        if bar.symbol not in declarations:
            raise ValueError("bar symbol has no unique RB declaration")
        if (bar.duration_seconds != 86400 or bar.ts.utcoffset() is None
                or isinstance(bar.datetime_nano, bool) or not isinstance(bar.datetime_nano, int)
                or bar.datetime_nano <= 0):
            raise ValueError("only timezone-aware positive-identity daily observations are supported")
        prices = [_number(getattr(bar, name), name) for name in ("open", "high", "low", "close")]
        if any(not D("0.000000000001") <= price <= D("1000000000000")
               or price.as_tuple().exponent < -12 or len(price.as_tuple().digits) > 28 for price in prices):
            raise ValueError("OHLC outside legacy bounded Decimal constraints")
        opening, high, low, close = prices
        if not low <= min(opening, close) <= max(opening, close) <= high:
            raise ValueError("invalid OHLC envelope")
        if min(opening, close) <= D(BASE["price_tick"]) * D(BASE["slippage_ticks"]):
            raise ValueError("slippage permits a non-positive effective sell price")
        grouped[bar.symbol].append(bar)
    for symbol, rows in grouped.items():
        rows.sort(key=lambda row: row.datetime_nano)
        if len(rows) > 200_000 or len({row.datetime_nano for row in rows}) != len(rows):
            raise ValueError(f"invalid daily observation population: {symbol}")
    return declarations, dict(grouped)


def _strategy_specs():
    return {name: {"parameters": asdict(cls()), "required_bars": cls().spec.required_bars}
            for name, cls in zip(STRATEGIES, (DonchianTrend, MATrend, ZScoreReversion))}


def _coverage(bundle, declarations, grouped, specs):
    rows = []
    for symbol in sorted(declarations):
        bars = grouped.get(symbol, [])
        n = len(bars)
        row = dict(symbol=symbol, contract_instance_id=declarations[symbol]["instance_id"], observations=n,
                   first_raw_ts=bars[0].ts.isoformat() if n else None,
                   last_raw_ts=bars[-1].ts.isoformat() if n else None,
                   first_datetime_nano=bars[0].datetime_nano if n else None,
                   last_datetime_nano=bars[-1].datetime_nano if n else None,
                   coverage_status="MEETS_60_OBSERVATIONS" if n >= 60 else
                   "INSUFFICIENT_COMMON_COVERAGE" if n else "NO_INTERVAL_OBSERVATIONS")
        row["potential_next_open_slots"] = {name: max(0, n - value["required_bars"]) for name, value in specs.items()}
        rows.append(row)
    opens = [bar.open for bars in grouped.values() for bar in bars]
    affordability = []
    for capital_id, cash in CAPITALS:
        for scenario in SCENARIOS:
            rate = D("0.12" if scenario == "BASE" else "0.18")
            thresholds = [D(10) * price * rate / D("0.4") + D("10.01") + D("0.001") * price for price in opens]
            affordability.append(dict(capital_id=capital_id, scenario=scenario, margin_rate=_text(rate),
                                      stored_open_quotes=len(opens), affordable_stored_open_quotes=sum(D(cash) >= value for value in thresholds),
                                      threshold_min=_text(min(thresholds)) if thresholds else None,
                                      threshold_max=_text(max(thresholds)) if thresholds else None,
                                      label="fresh-empty-account stored-open quote affordability"))
    common = sum(len(bars) >= 60 for bars in grouped.values())
    return dict(BOUNDARY, schema_version=SCHEMA_VERSION, contracts=rows, strategies=specs,
                bar_count=len(bundle.bars), declared_contracts=len(declarations), contracts_with_bars=len(grouped),
                declared_without_interval_bars=len(declarations) - len(grouped), common_coverage_contracts=common,
                insufficient_common_coverage_contracts=len(grouped) - common, matrix_cells=len(grouped) * 18,
                common_matrix_cells=common * 18, insufficient_matrix_cells=(len(grouped) - common) * 18,
                open_min=_text(min(opens)) if opens else None, open_max=_text(max(opens)) if opens else None,
                static_affordability=affordability,
                static_count_population="All stored opens, including warmup/final observations and overlapping contracts; fresh empty account, not actual opportunities.",
                input_metadata=dict(bundle.metadata), input_sources=sorted({bar.source for bar in bundle.bars}))


def _records(payload, record_type):
    names = [field.name for field in fields(record_type)]
    reader = csv.reader(io.StringIO(payload.decode("utf-8"), newline=""), strict=True)
    if next(reader, None) != names:
        raise ValueError(f"unexpected {record_type.__name__} CSV header")
    integers = {"observation_index", "signal_index", "datetime_nano", "side", "lots", "position_after",
                "position_lots", "evaluator_candidate_direction", "candidate_direction", "target_lots",
                "logical_direction", "signal_age", "preceding_signal_index", "attempted_target_lots"}
    nullable_ints = {"signal_index", "evaluator_candidate_direction", "candidate_direction", "target_lots",
                     "signal_age", "preceding_signal_index", "attempted_target_lots"}
    bools = {"margin_breached", "terminal", "final_observation"}
    strings = {"fill_id", "raw_ts", "phase", "offset", "clock_status", "strategy_status", "reason", "execution_status"}
    nullable_numbers = {"opening_required_margin", "opening_equity_after_costs"}
    result = []
    for values in reader:
        if len(values) != len(names):
            raise ValueError("malformed child CSV row")
        row = {}
        for name, value in zip(names, values):
            if name in integers:
                row[name] = _integer(value, name, nullable=name in nullable_ints)
            elif name in bools:
                row[name] = _boolean(value, name)
            elif name in strings:
                if not value:
                    raise ValueError(f"missing child field: {name}")
                row[name] = value
            elif name == "decision_at":
                if value:
                    raise ValueError("scenario decision_at must be null")
                row[name] = None
            else:
                row[name] = None if value == "" and name in nullable_numbers else _number(value, name)
        result.append(row)
    return result


def _require_equal(actual, expected, name):
    if (isinstance(expected, bool) and actual is not expected) or actual != expected:
        raise ValueError(f"child consistency mismatch: {name}")


def _child(path, symbol, strategy, config, bars, declaration, specs, snapshot):
    _safe_path(path)
    if {file.name for file in path.iterdir()} != CHILD_FILES:
        raise ValueError("child must contain exactly six files")
    payloads = {}
    for name in sorted(CHILD_FILES):
        file = _safe_path(path / name)
        if not file.is_file():
            raise ValueError("child payload must be a regular file")
        payloads[name] = file.read_bytes()
    manifest = _object(payloads["manifest.json"])
    for name, expected in dict(status="complete", schema_version="futures_daily_scenario_v1",
                               historical_strict_execution=False, assumptions_verification="not_historical_verified",
                               input_manifest_sha256=snapshot["input_manifest_sha256"],
                               input_content_hashes=snapshot["input_content_hashes"], source_hashes=snapshot["source_hashes"],
                               code_hashes={key: snapshot["code_hashes"][key] for key in ENGINE_CODE_PATHS},
                               input_config_sha256=sha256_bytes(canonical_json_bytes(config.to_dict()))).items():
        _require_equal(manifest.get(name), expected, name)
    _require_equal(manifest.get("content_hashes"), {name: sha256_bytes(payloads[name]) for name in sorted(PAYLOAD_FILES)}, "child hash closure")
    published = _object(payloads["config.json"])
    report = _object(payloads["report.json"])
    identity = dict(symbol=symbol, contract_instance_id=declaration["instance_id"], strategy=strategy,
                    schema_version="futures_daily_scenario_v1", historical_strict_execution=False,
                    assumptions_verification="not_historical_verified")
    for record in (published, report):
        for name, expected in identity.items():
            _require_equal(record.get(name), expected, name)
    _require_equal(published.get("assumptions"), config.to_dict(), "assumptions")
    _require_equal(published.get("strategy_parameters"), specs[strategy]["parameters"], "strategy parameters")
    _require_equal(published.get("terminal_policy"), "precommitted_final_observation_close", "terminal policy")
    _require_equal(report.get("status"), "complete", "report status")
    reconciliation = report.get("reconciliation")
    reconciliation_keys = {"cash_equals_initial_plus_realized_less_costs", "equity_equals_cash_plus_unrealized",
                           "gross_equals_realized_plus_unrealized", "net_equals_gross_less_costs",
                           "daily_equity_identity", "terminal_flat"}
    if not isinstance(reconciliation, dict) or set(reconciliation) != reconciliation_keys or any(value is not True for value in reconciliation.values()):
        raise ValueError("child reconciliation is not fully true")
    fills = _records(payloads["fills.csv"], ScenarioFill)
    marks = _records(payloads["equity.csv"], ScenarioEquity)
    decisions = _records(payloads["decisions.csv"], ScenarioDecision)
    _require_equal(len(marks), len(bars), "equity row count")
    _require_equal(len(decisions), len(bars), "decision row count")
    for name in MONEY_METRICS + ("initial_cash", "realized_pnl", "unrealized_pnl"):
        _number(report.get(name), f"report.{name}")
    for name in ("observations", "fills", "closed_trades", "opening_blocks", "margin_breach_observations",
                 "nonpositive_equity_observations", "turnover_lots", "ending_position_lots"):
        if not isinstance(report.get(name), int) or isinstance(report[name], bool):
            raise ValueError(f"invalid report count: {name}")
    _require_equal(report["observations"], len(bars), "observations")
    _require_equal(report["initial_cash"], _text(config.initial_cash), "initial cash")
    by_index = defaultdict(list)
    last_index = -1
    for index, fill in enumerate(fills):
        observation = fill["observation_index"]
        if not 0 <= observation < len(bars) or observation < last_index:
            raise ValueError("invalid fill observation ordering")
        last_index = observation
        _require_equal(fill["fill_id"], f"fill-{index:06d}", "fill identity")
        _require_equal(fill["datetime_nano"], bars[observation].datetime_nano, "fill nano")
        _require_equal(fill["raw_ts"], bars[observation].ts.isoformat(), "fill raw ts")
        if fill["side"] not in (-1, 1) or fill["lots"] != config.lots or fill["offset"] not in ("open", "close"):
            raise ValueError("invalid fill side/quantity/offset")
        if fill["phase"] == "next_open":
            _require_equal(fill["signal_index"], observation - 1, "fill signal index")
            _require_equal(fill["ideal_price"], bars[observation].open, "opening price")
        elif fill["phase"] == "terminal_close" and observation == len(bars) - 1 and fill["offset"] == "close":
            _require_equal(fill["signal_index"], None, "terminal signal index")
            _require_equal(fill["ideal_price"], bars[observation].close, "terminal price")
        else:
            raise ValueError("unexpected fill phase")
        _require_equal(fill["fee_per_lot_component"], config.fee_per_lot * fill["lots"], "flat fee")
        _require_equal(fill["fee_rate_component"], config.fee_rate * fill["ideal_price"] * config.multiplier * fill["lots"], "rate fee")
        _require_equal(fill["fee"], fill["fee_per_lot_component"] + fill["fee_rate_component"], "fill fee")
        _require_equal(fill["slippage"], config.price_tick * config.slippage_ticks * config.multiplier * fill["lots"], "slippage")
        by_index[observation].append(fill)
    cash = previous_equity = peak = config.initial_cash
    realized = fees = slip = D(0)
    position = attempts = margin_blocks = exhausted_blocks = 0
    headrooms = []
    for index, (bar, mark, decision) in enumerate(zip(bars, marks, decisions)):
        for record in (mark, decision):
            for name, expected in dict(observation_index=index, datetime_nano=bar.datetime_nano, raw_ts=bar.ts.isoformat()).items():
                _require_equal(record[name], expected, name)
        _require_equal(mark["close"], bar.close, "close price")
        _require_equal(mark["terminal"], index == len(bars) - 1, "terminal mark")
        _require_equal(decision["final_observation"], index == len(bars) - 1, "final decision")
        _require_equal(decision["clock_status"], "observation_only", "clock status")
        _require_equal(decision["preceding_signal_index"], index - 1 if index else None, "preceding signal index")
        _require_equal(decision["attempted_target_lots"], decisions[index - 1]["target_lots"] if index else None, "preceding target")
        for name in ("evaluator_candidate_direction", "candidate_direction", "logical_direction"):
            if decision[name] not in ((-1, 0, 1) if name == "logical_direction" else (None, -1, 0, 1)):
                raise ValueError("invalid child candidate/logical direction")
        _require_equal(decision["target_lots"], None if decision["candidate_direction"] is None else decision["candidate_direction"] * config.lots, "candidate target")
        required, after = decision["opening_required_margin"], decision["opening_equity_after_costs"]
        if (required is None) != (after is None):
            raise ValueError("opening budget fields must be jointly null")
        if required is not None:
            target = decision["attempted_target_lots"]
            if target not in (-config.lots, config.lots):
                raise ValueError("opening attempt requires fixed nonzero target")
            closed_cash = cash + sum((fill["realized_pnl"] - fill["fee"] - fill["slippage"]
                                     for fill in by_index[index] if fill["phase"] == "next_open" and fill["offset"] == "close"), D(0))
            opening_cost = abs(target) * (config.fee_per_lot + config.fee_rate * bar.open * config.multiplier
                                          + config.price_tick * config.slippage_ticks * config.multiplier)
            _require_equal(required, abs(target) * bar.open * config.multiplier * config.margin_rate, "opening required margin")
            _require_equal(after, closed_cash - opening_cost, "opening after-cost equity")
        old_fees, old_slip = fees, slip
        for fill in by_index[index]:
            if fill["offset"] == "open":
                _require_equal(position, 0, "open from flat")
                position = fill["side"] * fill["lots"]
            else:
                _require_equal(position, -fill["side"] * fill["lots"], "closing position")
                position = 0
            realized += fill["realized_pnl"]
            fees += fill["fee"]
            slip += fill["slippage"]
            cash += fill["realized_pnl"] - fill["fee"] - fill["slippage"]
            _require_equal(fill["cash_after"], cash, "fill cash")
            _require_equal(fill["position_after"], position, "fill position")
        _require_equal(mark["position_lots"], position, "marked position")
        for name, expected in dict(cash=cash, realized_pnl=realized, fees=fees, slippage=slip,
                                   equity=cash + mark["unrealized_pnl"],
                                   net_pnl=mark["equity"] - config.initial_cash,
                                   gross_pnl=realized + mark["unrealized_pnl"],
                                   required_margin=abs(position) * bar.close * config.multiplier * config.margin_rate,
                                   available_equity=mark["equity"] - mark["required_margin"]).items():
            _require_equal(mark[name], expected, name)
        _require_equal(mark["equity"], previous_equity + mark["daily_gross_pnl"] - (fees - old_fees) - (slip - old_slip), "daily equity")
        peak = max(peak, mark["equity"])
        _require_equal(mark["drawdown"], peak - mark["equity"], "drawdown")
        _require_equal(mark["drawdown_ratio"], (peak - mark["equity"]) / peak, "drawdown ratio")
        _require_equal(mark["margin_breached"], bool(position and (mark["equity"] <= 0 or mark["required_margin"] > mark["equity"] * config.max_margin_ratio)), "margin breach")
        previous_equity = mark["equity"]
        opening_count = sum(fill["offset"] == "open" for fill in by_index[index])
        status = decision["execution_status"]
        if required is not None:
            attempts += 1
            headrooms.append(after * config.max_margin_ratio - required)
            if status == "opening_costs_exhaust_equity":
                if after > 0 or opening_count:
                    raise ValueError("exhausted opening is inconsistent")
                exhausted_blocks += 1
            elif status == "opening_margin_blocked":
                if after <= 0 or required <= after * config.max_margin_ratio or opening_count:
                    raise ValueError("blocked opening is inconsistent")
                margin_blocks += 1
            elif status not in ("opened", "reversed") or opening_count != 1 or after <= 0 or required > after * config.max_margin_ratio:
                raise ValueError("accepted opening is inconsistent")
        elif opening_count or status in ("opening_margin_blocked", "opening_costs_exhaust_equity", "opened", "reversed"):
            raise ValueError("opening execution has no budget attempt")
    opening_fills = sum(fill["offset"] == "open" for fill in fills)
    blocks = margin_blocks + exhausted_blocks
    _require_equal(attempts, opening_fills + blocks, "opening attempts")
    _require_equal(report["fills"], len(fills), "fills")
    _require_equal(report["closed_trades"], len(fills) - opening_fills, "closed trades")
    _require_equal(report["opening_blocks"], blocks, "opening blocks")
    _require_equal(position, 0, "terminal flat")
    _require_equal(marks[-1]["unrealized_pnl"], D(0), "terminal unrealized PnL")
    _require_equal(marks[-1]["equity"], cash, "terminal cash/equity")
    _require_equal(report["ending_position_lots"], 0, "report terminal flat")
    for name, value in dict(ending_cash=cash, ending_equity=marks[-1]["equity"], realized_pnl=realized,
                            unrealized_pnl=marks[-1]["unrealized_pnl"], fees=fees, slippage=slip,
                            gross_pnl=marks[-1]["gross_pnl"], net_pnl=cash - config.initial_cash,
                            total_return=(cash - config.initial_cash) / config.initial_cash,
                            max_drawdown=max(mark["drawdown"] for mark in marks),
                            max_drawdown_ratio=max(mark["drawdown_ratio"] for mark in marks),
                            turnover_notional=sum((fill["lots"] * fill["ideal_price"] * config.multiplier for fill in fills), D(0))).items():
        _require_equal(_number(report[name], name), value, name)
    for name, value in dict(turnover_lots=sum(fill["lots"] for fill in fills),
                            margin_breach_observations=sum(mark["margin_breached"] for mark in marks),
                            nonpositive_equity_observations=sum(mark["equity"] <= 0 for mark in marks)).items():
        _require_equal(report[name], value, name)
    candidates = dict(sorted(Counter("null" if row["candidate_direction"] is None else str(row["candidate_direction"]) for row in decisions).items()))
    _require_equal(report.get("candidate_counts"), candidates, "candidate counts")
    if not isinstance(report.get("limitations"), list) or not isinstance(report.get("unusable_nonpricing_fields"), dict):
        raise ValueError("missing child limitations/field diagnostics")
    execution_class = ("NO_OPENING_ATTEMPT" if not attempts else "ALL_ATTEMPTS_BLOCKED" if not opening_fills
                       else "PARTLY_BLOCKED" if blocks else "NO_OPENING_BLOCK")
    return dict(report=report, decisions=decisions, marks=marks, fills=fills,
                payload_hashes={name: sha256_bytes(payload) for name, payload in payloads.items()},
                opening_fills=opening_fills,
                opening_attempts=attempts, opening_blocks=blocks, opening_margin_blocked=margin_blocks,
                opening_costs_exhaust_equity=exhausted_blocks, execution_class=execution_class,
                opening_blocked_rate=_text(D(blocks) / D(attempts)) if attempts else None,
                minimum_recorded_cash=_text(min([config.initial_cash, *(fill["cash_after"] for fill in fills)])),
                minimum_closing_equity=_text(min([config.initial_cash, *(mark["equity"] for mark in marks)])),
                peak_close_required_margin=_text(max(mark["required_margin"] for mark in marks)),
                minimum_close_available_equity=_text(min(mark["available_equity"] for mark in marks)),
                minimum_close_margin_budget_headroom=_text(min(mark["equity"] * config.max_margin_ratio - mark["required_margin"] for mark in marks)),
                minimum_opening_margin_budget_headroom=_text(min(headrooms)) if headrooms else None,
                candidate_signature=[tuple(row[name] for name in INVARIANT_FIELDS) for row in decisions],
                positions=[mark["position_lots"] for mark in marks],
                fill_events=[[(fill["phase"], fill["offset"], fill["side"], fill["lots"], fill["signal_index"], fill["ideal_price"])
                              for fill in by_index[index]] for index in range(len(bars))])


def _comparison_row(cell, symbol, strategy, capital_id, scenario, config, bars, declaration, specs, child_path):
    report = cell["report"]
    row = dict(symbol=symbol, contract_instance_id=declaration["instance_id"], strategy=strategy,
               capital_id=capital_id, scenario=scenario, child_path=child_path, evidence_level="SCENARIO",
               historical_strict_execution=False, assumptions_verification="not_historical_verified",
               coverage_status="MEETS_60_OBSERVATIONS" if len(bars) >= 60 else "INSUFFICIENT_COMMON_COVERAGE",
               observations=len(bars), first_raw_ts=bars[0].ts.isoformat(), last_raw_ts=bars[-1].ts.isoformat(),
               required_bars=specs[strategy]["required_bars"], potential_next_open_slots=max(0, len(bars) - specs[strategy]["required_bars"]),
               initial_cash=_text(config.initial_cash), margin_rate=_text(config.margin_rate), max_margin_ratio=_text(config.max_margin_ratio))
    for name in MONEY_METRICS + ("fills", "closed_trades", "turnover_lots", "opening_blocks",
                                "margin_breach_observations", "nonpositive_equity_observations", "ending_position_lots"):
        row[name] = report[name]
    for name in ("opening_fills", "opening_attempts", "opening_margin_blocked", "opening_costs_exhaust_equity",
                 "execution_class", "opening_blocked_rate", "minimum_recorded_cash", "minimum_closing_equity",
                 "peak_close_required_margin", "minimum_close_available_equity", "minimum_close_margin_budget_headroom",
                 "minimum_opening_margin_budget_headroom"):
        row[name] = cell[name]
    return row


def _pair_rows(rows, cells):
    lookup = {(row["symbol"], row["strategy"], row["capital_id"], row["scenario"]): row for row in rows}
    invariance = []
    for symbol in sorted({row["symbol"] for row in rows}):
        for strategy in STRATEGIES:
            group = [cells[(symbol, strategy, capital_id, scenario)] for capital_id, _ in CAPITALS for scenario in SCENARIOS]
            if any(cell["candidate_signature"] != group[0]["candidate_signature"] for cell in group[1:]):
                raise ValueError("candidate/logical-state invariance failed across six cells")
            invariance.append(dict(symbol=symbol, strategy=strategy, status="IDENTICAL_ACROSS_SIX_CELLS",
                                   compared_observations=len(group[0]["candidate_signature"]), cells=6))
    for row in rows:
        key = (row["symbol"], row["strategy"], row["capital_id"], row["scenario"])
        for prefix, reference_key, reference_id in (
            ("capital_reference", (key[0], key[1], "C1000000", key[3]), "C1000000"),
            ("margin_reference", (key[0], key[1], key[2], "BASE"), "BASE"),
        ):
            reference = lookup[reference_key]
            row[prefix] = reference_id
            row[prefix + "_child_path"] = reference["child_path"]
            for name in PAIRED_METRICS:
                row[prefix + "_delta_" + name] = row[name] - reference[name] if name in COUNT_METRICS else _text(D(row[name]) - D(reference[name]))
            row[prefix + "_position_difference_observations"] = sum(a != b for a, b in zip(cells[key]["positions"], cells[reference_key]["positions"]))
            row[prefix + "_fill_event_difference_observations"] = sum(a != b for a, b in zip(cells[key]["fill_events"], cells[reference_key]["fill_events"]))
    return invariance


def _distribution(values):
    if not values:
        return dict(min=None, median=None, max=None)
    ordered = sorted(D(value) for value in values)
    n = len(ordered)
    median = ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / D(2)
    return dict(min=_text(ordered[0]), median=_text(median), max=_text(ordered[-1]))


def _summary(rows, cells, invariance, coverage):
    indexed, groups = [], []
    for row in rows:
        cell = cells[(row["symbol"], row["strategy"], row["capital_id"], row["scenario"])]
        entry = dict(row)
        for name in ("candidate_counts", "reconciliation", "unusable_nonpricing_fields", "limitations"):
            entry[name] = cell["report"][name]
        for field, key in (("reason", "reason_counts"), ("execution_status", "execution_status_counts"), ("strategy_status", "strategy_status_counts")):
            entry[key] = dict(sorted(Counter(decision[field] for decision in cell["decisions"]).items()))
        entry["first_nonnull_candidate_index"] = next((decision["observation_index"] for decision in cell["decisions"] if decision["candidate_direction"] is not None), None)
        indexed.append(entry)
    for strategy in STRATEGIES:
        for capital_id, _ in CAPITALS:
            for scenario in SCENARIOS:
                population = [row for row in rows if (row["strategy"], row["capital_id"], row["scenario"]) == (strategy, capital_id, scenario)
                              and row["coverage_status"] == "MEETS_60_OBSERVATIONS"]
                group = dict(strategy=strategy, capital_id=capital_id, scenario=scenario, n=len(population),
                             status="DESCRIPTIVE" if population else "INSUFFICIENT",
                             opening_contracts=sum(row["opening_fills"] > 0 for row in population),
                             no_opening_contracts=sum(row["opening_fills"] == 0 for row in population),
                             execution_class_counts={name: sum(row["execution_class"] == name for row in population) for name in EXECUTION_CLASSES},
                             block_contracts=sum(row["opening_blocks"] > 0 for row in population),
                             breach_contracts=sum(row["margin_breach_observations"] > 0 for row in population),
                             nonpositive_equity_contracts=sum(row["nonpositive_equity_observations"] > 0 for row in population),
                             net_pnl_counts={name: sum((D(row["net_pnl"]) > 0 if name == "positive" else D(row["net_pnl"]) < 0 if name == "negative" else D(row["net_pnl"]) == 0) for row in population) for name in ("positive", "zero", "negative")},
                             net_pnl=_distribution([row["net_pnl"] for row in population]),
                             total_return=_distribution([row["total_return"] for row in population]),
                             worst_max_drawdown=_text(max(D(row["max_drawdown"]) for row in population)) if population else None,
                             worst_max_drawdown_ratio=_text(max(D(row["max_drawdown_ratio"]) for row in population)) if population else None)
                for prefix in ("capital_reference", "margin_reference"):
                    for kind in ("position", "fill_event"):
                        group[prefix + "_" + kind + "_difference_contracts"] = sum(row[prefix + "_" + kind + "_difference_observations"] > 0 for row in population)
                groups.append(group)
    return dict(BOUNDARY, schema_version=SCHEMA_VERSION, cells=indexed, groups=groups,
                candidate_invariance=invariance,
                short_contract_cells=[entry for entry in indexed if entry["coverage_status"] == "INSUFFICIENT_COMMON_COVERAGE"],
                null_reasons={"opening_blocked_rate": "No actual opening attempt; denominator is zero.",
                             "minimum_opening_margin_budget_headroom": "No opening attempt budget snapshot.",
                             "group_distributions": "No contract meets the 60-observation common coverage gate."},
                sampling={"minimum_recorded_cash": "initial cash and every fill.cash_after",
                          "minimum_closing_equity": "initial equity and every closing observation mark",
                          "margin_metrics": "closing observations, after terminal close; not intrabar worst risk"},
                input_metadata=coverage["input_metadata"], input_sources=coverage["input_sources"])


def _csv(rows):
    names = list(rows[0]) if rows else ["symbol", "contract_instance_id", "strategy", "capital_id", "scenario"]
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(names)
    for row in rows:
        writer.writerow("" if row[name] is None else "true" if row[name] is True else "false" if row[name] is False else str(row[name]) for name in names)
    return buffer.getvalue().encode("utf-8")


def _report(rows, summary, coverage):
    opening_cells = sum(row["opening_fills"] > 0 for row in rows)
    blocked_cells = sum(row["opening_blocks"] > 0 for row in rows)
    breached_cells = sum(row["margin_breach_observations"] > 0 for row in rows)
    lines = ["# RB daily fixed-capital and margin scenarios", "",
             f"This run completed {len(rows)} independent contract accounts: {opening_cells} had an opening fill, {blocked_cells} had an actual blocked opening attempt, and {breached_cells} recorded a closing-observation margin breach.", "",
             "Evidence level: SCENARIO; historical assumptions and execution are unverified. Broker NOT_RUN, trading disabled, real send/cancel counts 0. These results do not establish PIT/OOS evidence, strategy qualification, or a safe minimum balance.", "",
             "Each account uses fixed one-lot positions and continuous cash within its own contract interval. Capitals and margin assumptions were fixed from frozen prices before these strategy replays. The legacy IDs are donchian_trend, ma_trend and zscore_reversion; they are not v2 TREND/BREAKOUT/PULLBACK.", "",
             "## Fresh empty-account quote affordability", "",
             "Threshold = 10 × stored open × margin rate / 0.4 + 10.01 + 0.001 × stored open. Counts include warmup/final observations and overlapping contract dates. They describe fresh-empty-account stored-open quote affordability, not candidates, actual attempts, fills or future feasibility. Prior gains, losses and closing costs change actual cash.", "",
             "| Capital | Margin scenario | Threshold min | Threshold max | Affordable stored opens / all |", "| --- | --- | ---: | ---: | ---: |"]
    for row in coverage["static_affordability"]:
        lines.append(f"| {row['capital_id']} | {row['scenario']} | {row['threshold_min']} | {row['threshold_max']} | {row['affordable_stored_open_quotes']} / {row['stored_open_quotes']} |")
    lines += ["", "## Common-coverage account descriptions", "",
              "The gate is at least 60 stored observations and only changes the description denominator. Strategies retain natural warmups 41/60/21 and their full, different contract intervals. Contracts overlap and are correlated; these counts are not independent episodes or date blocks.", "",
              "| Strategy | Capital | Margin | n | Open / no open | Classes: no attempt / all blocked / partial / no block | Block / breach / nonpositive contracts | Net PnL min / median / max | Return min / median / max | Worst drawdown / ratio | Capital fill / position divergence | Margin fill / position divergence |",
              "| --- | --- | --- | ---: | ---: | --- | --- | --- | --- | --- | --- | --- |"]
    for group in summary["groups"]:
        fmt = lambda values: " / ".join(str(values[name]) for name in ("min", "median", "max"))
        classes = " / ".join(str(group["execution_class_counts"][name]) for name in EXECUTION_CLASSES)
        lines.append(f"| {group['strategy']} | {group['capital_id']} | {group['scenario']} | {group['n']} | {group['opening_contracts']} / {group['no_opening_contracts']} | {classes} | {group['block_contracts']} / {group['breach_contracts']} / {group['nonpositive_equity_contracts']} | {fmt(group['net_pnl'])} | {fmt(group['total_return'])} | {group['worst_max_drawdown']} / {group['worst_max_drawdown_ratio']} | {group['capital_reference_fill_event_difference_contracts']} / {group['capital_reference_position_difference_contracts']} | {group['margin_reference_fill_event_difference_contracts']} / {group['margin_reference_position_difference_contracts']} |")
    for strategy in STRATEGIES:
        lines += ["", f"## {strategy}: C15000 against C1000000", "",
                  "Fill/position divergence counts compare the same contract and strategy under the same margin scenario. Closing-flat endpoints do not determine path equality.", "",
                  "| Contract | BASE net PnL | Open / blocked | Drawdown / ratio | Capital fill / position differences | Margin stress net PnL | Open / blocked | Drawdown / ratio | Capital fill / position differences |", "| --- | ---: | ---: | --- | ---: | ---: | ---: | --- | ---: |"]
        selected = [row for row in rows if row["strategy"] == strategy and row["capital_id"] == "C15000" and row["coverage_status"] == "MEETS_60_OBSERVATIONS"]
        for first, second in zip(selected[::2], selected[1::2]):
            part = lambda row: f"{row['net_pnl']} | {row['opening_fills']} / {row['opening_blocks']} | {row['max_drawdown']} / {row['max_drawdown_ratio']} | {row['capital_reference_fill_event_difference_observations']} / {row['capital_reference_position_difference_observations']}"
            lines.append(f"| {first['symbol']} | {part(first)} | {part(second)} |")
    lines += ["", "## Short contracts", "", "All short-contract cells were replayed and retained; they do not enter the 18 description denominators.", "",
              "| Contract | Observations | Coverage | Complete cells | Cells with opening fills |", "| --- | ---: | --- | ---: | ---: |"]
    for contract in coverage["contracts"]:
        if 0 < contract["observations"] < 60:
            short = [row for row in rows if row["symbol"] == contract["symbol"]]
            lines.append(f"| {contract['symbol']} | {contract['observations']} | {contract['coverage_status']} | {len(short)} | {sum(row['opening_fills'] > 0 for row in short)} |")
    lines += ["", "## Reading these results", "",
              "Identical trades and PnL can have a larger return percentage solely because the initial-cash denominator is smaller. Fewer trades can improve or worsen PnL; margin-stress net returns need not be monotonic. All-cash C5000 results are not profitability or risk qualification. NO_OPENING_ATTEMPT is distinct from ALL_ATTEMPTS_BLOCKED.", "",
              "Available equity = equity − required margin. Margin-budget headroom = equity × 0.4 − required margin. Minimum recorded cash samples initial cash and every fill; minimum closing equity samples initial equity and closes. Neither certifies continuous intrabar worst risk. Closing margin observations follow the final terminal close, so a final zero margin can conceal a position opened and closed within that last observation.", "",
              "No historical PIT metadata, verified calendar/session/availability, actual tradability, daily settlement, forced liquidation, cash top-up, intrabar path, price-limit or execution-capacity model is supplied. Historical costs, multiplier and margin assumptions are unverified. Independent contract accounts cannot be added or stitched into portfolio NAV, and fixed one-lot exposure is not a common whole-round risk budget. No annualized return, Sharpe, significance, trading-win qualification or minimum-safe-capital recommendation is produced.", "",
              "A complete replay is distinct from independent product hard review, statistical qualification, full v2 delivery and trading permission. Synthetic fixture source/metadata marks remain in coverage and summary.", "",
              "[Comparison CSV](comparison.csv) · [Summary JSON](summary.json) · [Coverage JSON](coverage.json)", "",
              "## Raw accounts", "", "| Contract | Strategy | Capital | Scenario | Raw ledgers and report |", "| --- | --- | --- | --- | --- |"]
    for row in rows:
        path = row["child_path"]
        lines.append(f"| {row['symbol']} | {row['strategy']} | {row['capital_id']} | {row['scenario']} | [fills]({path}/fills.csv) [equity]({path}/equity.csv) [decisions]({path}/decisions.csv) [report]({path}/report.json) |")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _write_new(path: Path, payload: bytes):
    _safe_path(path)
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
    if path.read_bytes() != payload:
        raise OSError("published payload read-back mismatch")


def _tree_hashes(destination):
    result = {}
    for path in sorted(destination.rglob("*")):
        _safe_path(path)
        if path.is_file() and path != destination / "manifest.json":
            result[path.relative_to(destination).as_posix()] = sha256_file(path)
    return result


def _check_snapshot(source, input_bytes, base, base_bytes, configs, destination, code):
    for name, payload in input_bytes.items():
        if _safe_path(source / name).read_bytes() != payload:
            raise ValueError(f"input drift: {name}")
    if base.read_bytes() != base_bytes:
        raise ValueError("BASE input drift")
    if _code_hashes() != code:
        raise ValueError("code drift")
    if destination is not None:
        for name, payload in configs.items():
            if _safe_path(destination / "configs" / name).read_bytes() != payload:
                raise ValueError("derived config drift")


def _incomplete(exc, snapshot):
    return canonical_json_bytes(dict(BOUNDARY, schema_version=SCHEMA_VERSION, status="incomplete",
                                     error_type=type(exc).__name__, reason=str(exc)[:500], **snapshot))


def run_capital_stress(input_bundle: Path, base_config: Path, output: Path) -> dict[str, object]:
    """Validate, replay the fixed population, and publish one new complete tree."""
    source, base = _safe_path(input_bundle), _safe_path(base_config)
    destination = _destination(source, base, output)
    if not source.is_dir() or not base.is_file() or not (source / "manifest.json").is_file():
        raise ValueError("required input bundle, manifest or BASE is absent")
    if base.stat().st_size > 16_384:
        raise ValueError("BASE exceeds 16 KiB")
    base_bytes = base.read_bytes()
    config = ScenarioConfig.from_mapping(_object(base_bytes, decimal=True))
    if config.to_dict() != BASE:
        raise ValueError("BASE differs from preregistered nine economic values")
    manifest, input_bytes = _capture_input(source)
    bundle = read_input_bundle(source)
    declarations, grouped = _population(bundle)
    specs = _strategy_specs()
    code = _code_hashes()
    snapshot = dict(input_manifest_sha256=sha256_bytes(input_bytes["manifest.json"]),
                    input_content_hashes=manifest["content_hashes"], source_hashes=manifest.get("source_hashes", {}),
                    source_hashes_status="copied_frozen_export_provenance_not_external_reverified",
                    input_base_config_sha256=sha256_bytes(base_bytes), code_hashes=code)
    with localcontext(ARITHMETIC):
        configs, values = {}, {}
        for capital_id, cash in CAPITALS:
            for scenario in SCENARIOS:
                current = dict(BASE, initial_cash=cash, margin_rate="0.12" if scenario == "BASE" else "0.18")
                values[(capital_id, scenario)] = ScenarioConfig.from_mapping(current)
                configs[f"{capital_id}__{scenario}.json"] = canonical_json_bytes(values[(capital_id, scenario)].to_dict())
        _check_snapshot(source, input_bytes, base, base_bytes, configs, None, code)
        coverage = _coverage(bundle, declarations, grouped, specs)
        _destination(source, base, output)
        destination.mkdir(parents=True, exist_ok=False)
        try:
            (destination / "configs").mkdir()
            for name, payload in configs.items():
                _write_new(destination / "configs" / name, payload)
            rows, cells = [], {}
            expected_hashes = {"configs/" + name: sha256_bytes(payload) for name, payload in configs.items()}
            for symbol in sorted(grouped):
                bars = grouped[symbol]
                for strategy in STRATEGIES:
                    for capital_id, _ in CAPITALS:
                        for scenario in SCENARIOS:
                            _check_snapshot(source, input_bytes, base, base_bytes, configs, destination, code)
                            relative = f"cells/{symbol}/{strategy}/{capital_id}/{scenario}"
                            child_path = _safe_path(destination / relative)
                            if destination not in child_path.parents:
                                raise ValueError("child escapes its owned output root")
                            derived = destination / "configs" / f"{capital_id}__{scenario}.json"
                            run_history_scenario(source, child_path, symbol=symbol, strategy=strategy, config_path=derived)
                            _check_snapshot(source, input_bytes, base, base_bytes, configs, destination, code)
                            current = values[(capital_id, scenario)]
                            cell = _child(child_path, symbol, strategy, current, bars, declarations[symbol], specs, snapshot)
                            cells[(symbol, strategy, capital_id, scenario)] = cell
                            rows.append(_comparison_row(cell, symbol, strategy, capital_id, scenario, current, bars, declarations[symbol], specs, relative))
                            expected_hashes.update({relative + "/" + name: digest for name, digest in cell["payload_hashes"].items()})
            invariance = _pair_rows(rows, cells)
            summary = _summary(rows, cells, invariance, coverage)
            payloads = {"coverage.json": canonical_json_bytes(coverage), "comparison.csv": _csv(rows),
                        "summary.json": canonical_json_bytes(summary), "report.md": _report(rows, summary, coverage)}
            for name, payload in payloads.items():
                _write_new(destination / name, payload)
            expected_hashes.update({name: sha256_bytes(payload) for name, payload in payloads.items()})
            _check_snapshot(source, input_bytes, base, base_bytes, configs, destination, code)
            hashes = _tree_hashes(destination)
            if hashes != expected_hashes:
                raise ValueError("published output closure changed after validation")
            complete = dict(BOUNDARY, schema_version=SCHEMA_VERSION, status="complete", **snapshot,
                            strategy_order=list(STRATEGIES), capital_order=[dict(capital_id=name, initial_cash=cash) for name, cash in CAPITALS],
                            scenario_order=list(SCENARIOS), matrix_cells=len(rows), common_matrix_cells=coverage["common_matrix_cells"],
                            insufficient_matrix_cells=coverage["insufficient_matrix_cells"], candidate_invariance="IDENTICAL_ACROSS_SIX_CELLS",
                            content_hashes=hashes)
            marker = destination / "manifest.json"
            with marker.open("xb") as handle:
                try:
                    complete_bytes = canonical_json_bytes(complete)
                    handle.write(complete_bytes)
                    handle.flush()
                    if marker.read_bytes() != complete_bytes or _tree_hashes(destination) != hashes:
                        raise OSError("complete tree read-back mismatch")
                    _check_snapshot(source, input_bytes, base, base_bytes, configs, destination, code)
                except BaseException as exc:
                    # Only repair the descriptor we exclusively created; never reopen a competing path.
                    try:
                        handle.seek(0)
                        handle.truncate()
                        handle.write(_incomplete(exc, snapshot))
                        handle.flush()
                    except OSError:
                        pass
                    raise
        except BaseException as exc:
            # Tell the CLI whether THIS invocation acquired the root; existing paths are preflight rejections.
            setattr(exc, "_capital_stress_output_created", True)
            try:
                _write_new(destination / "manifest.json", _incomplete(exc, snapshot))
            except (OSError, ValueError):
                pass  # A collision preserves the other writer; absent/invalid marker is incomplete.
            raise
    return dict(BOUNDARY, status="complete", matrix_cells=len(rows), output=str(destination),
                manifest_sha256=sha256_file(destination / "manifest.json"))


class _Once(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        if getattr(namespace, self.dest, None) is not None:
            parser.error(f"{option_string} must be provided exactly once")
        setattr(namespace, self.dest, values)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def main(argv=None) -> int:
    parser = _Parser(description=__doc__, allow_abbrev=False)
    for name in ("--input-bundle", "--base-config", "--output"):
        parser.add_argument(name, type=Path, required=True, action=_Once)
    args = None
    try:
        args = parser.parse_args(argv)
        result = run_capital_stress(args.input_bundle, args.base_config, args.output)
    except (ValueError, OSError, RuntimeError, ArithmeticError, TypeError, KeyError, csv.Error) as exc:
        output_created = getattr(exc, "_capital_stress_output_created", False)
        print(json.dumps(dict(BOUNDARY, status="incomplete" if output_created else "rejected",
                              error_type=type(exc).__name__, reason=str(exc)[:500]),
                         sort_keys=True, allow_nan=False, ensure_ascii=False))
        return 2
    print(json.dumps(result, sort_keys=True, allow_nan=False, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
