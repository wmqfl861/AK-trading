"""Offline single-contract daily scenarios under explicit, unverified assumptions.

This host uses the existing pure observation-only strategy evaluators. Raw bar
identities order observations; no session, calendar or publication clock is
inferred. Scenario fills are accounting assumptions, not historical execution
certificates. All monetary arithmetic uses bounded Decimal inputs and a private
128-digit context; ratios alone can require rounding.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from decimal import Context, Decimal, DecimalException, ROUND_HALF_EVEN, localcontext
import csv
import io
import json
from pathlib import Path
import re

from ak_trading.futures.io import (
    BUNDLE_SCHEMA_VERSION, InputBundle, OutputDirectoryError,
    assert_output_directory_is_new, canonical_json_bytes, read_input_bundle,
    sha256_bytes, sha256_file,
)
from ak_trading.futures.spec import (
    ClockStatus, ContractBar, StrategyContext, StrategyState, parse_product_token,
)
from ak_trading.futures.strategies import DonchianTrend, MATrend, ZScoreReversion
from ak_trading.futures.structures import build_structure_frame

__all__ = ["ScenarioConfig", "ScenarioResult", "run_scenario_backtest", "run_history_scenario"]

D = Decimal
_ARITHMETIC = Context(prec=128, rounding=ROUND_HALF_EVEN)
STRATEGIES = ("donchian_trend", "ma_trend", "zscore_reversion")
SCHEMA_VERSION = "futures_daily_scenario_v1"
MAX_OBSERVATIONS = 200_000


def _number(name: str, value: object, lower: str, upper: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError(f"{name} must be a finite decimal number")
    if isinstance(value, str) and len(value) > 100:
        raise ValueError(f"{name} decimal representation is too long")
    try:
        number = D(str(value))
        if not number.is_finite() or not D(lower) <= number <= D(upper):
            raise ValueError(f"{name} must be finite and in {lower}..{upper}")
        # Bound precision as well as magnitude so every monetary product and
        # sum fits the private context exactly, even over MAX_OBSERVATIONS.
        if number.as_tuple().exponent < -12 or len(number.as_tuple().digits) > 28:
            raise ValueError(f"{name} supports at most 12 decimal places and 28 digits")
        return number
    except DecimalException as exc:
        raise ValueError(f"{name} must be a finite decimal number") from exc


def _text(number: Decimal) -> str:
    if number == 0:
        return "0"
    text = format(number, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


@dataclass(frozen=True)
class ScenarioConfig:
    """Nine required economic inputs; no defaults or strategy tuning knobs."""

    initial_cash: Decimal
    lots: int
    multiplier: Decimal
    price_tick: Decimal
    fee_per_lot: Decimal
    fee_rate: Decimal
    slippage_ticks: Decimal
    margin_rate: Decimal
    max_margin_ratio: Decimal

    def __post_init__(self) -> None:
        if isinstance(self.lots, bool) or not isinstance(self.lots, int) or not 1 <= self.lots <= 1_000_000:
            raise ValueError("lots must be an integer in 1..1000000")
        bounds = {
            "initial_cash": ("0.000000000001", "1000000000000000"),
            "multiplier": ("0.000000000001", "1000000000"),
            "price_tick": ("0.000000000001", "1000000000"),
            "fee_per_lot": ("0", "1000000000"),
            "fee_rate": ("0", "1"), "slippage_ticks": ("0", "1000000"),
            "margin_rate": ("0", "1"), "max_margin_ratio": ("0", "1"),
        }
        for name, (lower, upper) in bounds.items():
            object.__setattr__(self, name, _number(name, getattr(self, name), lower, upper))

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> ScenarioConfig:
        if not isinstance(values, Mapping):
            raise ValueError("config must be a JSON object")
        required = {field.name for field in fields(cls)}
        missing, unknown = required - set(values), set(values) - required
        if missing or unknown:
            raise ValueError(f"config missing keys: {sorted(missing)}; unknown keys: {sorted(map(str, unknown))}")
        return cls(**dict(values))

    def to_dict(self) -> dict[str, object]:
        return {key: _text(value) if isinstance(value, D) else value for key, value in asdict(self).items()}


@dataclass(frozen=True)
class ScenarioFill:
    fill_id: str
    observation_index: int
    signal_index: int | None
    datetime_nano: int
    raw_ts: str
    phase: str
    offset: str
    side: int
    lots: int
    ideal_price: Decimal
    effective_price: Decimal
    fee_per_lot_component: Decimal
    fee_rate_component: Decimal
    fee: Decimal
    slippage: Decimal
    realized_pnl: Decimal
    cash_after: Decimal
    position_after: int


@dataclass(frozen=True)
class ScenarioEquity:
    observation_index: int
    datetime_nano: int
    raw_ts: str
    close: Decimal
    position_lots: int
    cash: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    equity: Decimal
    daily_gross_pnl: Decimal
    gross_pnl: Decimal
    fees: Decimal
    slippage: Decimal
    net_pnl: Decimal
    required_margin: Decimal
    available_equity: Decimal
    margin_breached: bool
    drawdown: Decimal
    drawdown_ratio: Decimal
    terminal: bool


@dataclass(frozen=True)
class ScenarioDecision:
    observation_index: int
    datetime_nano: int
    raw_ts: str
    decision_at: None
    clock_status: str
    strategy_status: str
    reason: str
    evaluator_candidate_direction: int | None
    candidate_direction: int | None
    target_lots: int | None
    logical_direction: int
    signal_age: int | None
    preceding_signal_index: int | None
    attempted_target_lots: int | None
    execution_status: str
    opening_required_margin: Decimal | None
    opening_equity_after_costs: Decimal | None
    final_observation: bool


@dataclass(frozen=True)
class ScenarioResult:
    config: Mapping[str, object]
    fills: tuple[ScenarioFill, ...]
    equity: tuple[ScenarioEquity, ...]
    decisions: tuple[ScenarioDecision, ...]
    report: Mapping[str, object]


EVENT_ORDER = (
    "Sort selected contract daily rows by raw integer datetime_nano; reject duplicate identities and invalid OHLC, never drop rows.",
    "At each observation open, execute only the preceding completed observation's scenario target; a reversal closes first, then checks margin for its opening leg.",
    "Charge fee_per_lot plus fee_rate times ideal-price notional, and adverse slippage_ticks times price_tick times multiplier, per lot on every fill leg.",
    "Mark the held position to the observation close; no futures settlement cash transfer or inferred trading session is applied.",
    "At the precommitted final observation close, close any remaining position at that close with the same costs, including a position opened at that observation's open.",
    "Evaluate the now-completed observation using an observation-only structure prefix and logical StrategyState; its candidate sets the following observation's signed fixed-lot target.",
    "The final observation's candidate is recorded but has no following execution. Null candidates preserve holdings and logical direction while held signal age advances; zero candidates request a close.",
)
LIMITATIONS = (
    "All economic assumptions are user_configured and not_historical_verified; no official fee, multiplier, tick or margin schedule is inferred.",
    "This is a retrospective single concrete contract scenario with possible coverage and selection bias, not a portfolio or a continuous-contract roll backtest.",
    "Raw timestamps order stored daily observations only. Session windows, exchange calendar, missing sessions and point-in-time available_at are unverified; imported_at is not historical availability.",
    "Next-observation-open and final-observation-close fills are assumed; intrabar path, price limits, volume capacity, queue priority, tick-grid executability and actual tradability are not certified.",
    "The same frozen additive fee formula applies to open and every close. close_today versus close_history is not historically resolved, even for a terminal same-observation round trip.",
    "Margin uses ideal open notional for admission and close notional for daily monitoring, not historical settlement prices. Breaches are reported; no forced liquidation, cash top-up or maintenance rule is invented.",
    "Gross PnL uses ideal prices; slippage is deducted once as cash expense. Effective prices are informational and are never also used for PnL or fee notional.",
    "Existing strategy parameters and regime heuristics are fixed without optimization. Observation-only candidates are promoted to scenario targets, never to certified actionable signals.",
    "The host carries logical direction and completed-observation age independently of actual fills. It applies the existing fixed z-score max_signal_age exit omitted by that evaluator's observation-only candidate path.",
    "Volume and open interest do not constrain fills. Missing, negative or nonfinite values are counted as unusable, never replaced by a fabricated capacity.",
    "Return is total net PnL divided by initial cash; drawdown uses initial cash and subsequent closing equity marks. No annualized return, Sharpe or statistical significance is claimed.",
)


def _select_contract(bundle: InputBundle, symbol: str):
    if bundle.manifest.get("schema_version") != BUNDLE_SCHEMA_VERSION or bundle.manifest.get("status") != "complete":
        raise ValueError("scenario requires a complete futures_bundle_v1 input")
    if bundle.manifest.get("dataset_kind") != "real_export":
        raise ValueError("scenario requires dataset_kind=real_export")
    if not isinstance(symbol, str) or len(symbol) > 80:
        raise ValueError("symbol must be one explicit concrete futures symbol")
    match = re.fullmatch(r"(SHFE|INE|DCE|CZCE|CFFEX|GFEX)\.([A-Za-z]+)([0-9]{3,4})", symbol)
    if match is None:
        raise ValueError("symbol must be one concrete future; options and KQ continuous symbols are unsupported")
    exchange, product_code, delivery = match.groups()
    if len(delivery) != (3 if exchange == "CZCE" else 4) or not 1 <= int(delivery[-2:]) <= 12:
        raise ValueError("symbol has an invalid concrete delivery code")
    product = parse_product_token(f"{exchange}.{product_code}")
    declarations = [row for row in bundle.contracts if isinstance(row, Mapping) and row.get("raw_symbol") == symbol]
    if len(declarations) != 1:
        raise ValueError(f"symbol requires exactly one full contract instance; found {len(declarations)} (missing or ambiguous)")
    declaration = declarations[0]
    if declaration.get("product") != str(product):
        raise ValueError("contract product does not match symbol")
    for name in ("asset_class", "instrument_class", "ins_class"):
        if name in declaration and declaration[name] != "FUTURE":
            raise ValueError("options or non-future contract metadata are unsupported")
    try:
        expiry = datetime.fromisoformat(declaration["expiry_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("contract requires a full instance with expiry_at") from exc
    if expiry.utcoffset() is None:
        raise ValueError("contract expiry_at must be timezone-aware")
    instance = declaration.get("instance_id")
    if instance != f"{symbol}|{expiry.isoformat()}":
        raise ValueError("full contract instance_id does not match symbol and expiry_at")
    if sum(isinstance(row, Mapping) and row.get("instance_id") == instance for row in bundle.contracts) != 1:
        raise ValueError("duplicate full contract instance_id")
    bars = tuple(bar for bar in bundle.bars if bar.symbol == symbol)
    if not bars or len(bars) > MAX_OBSERVATIONS:
        raise ValueError(f"selected symbol needs 1..{MAX_OBSERVATIONS} daily observations")
    for bar in bars:
        if isinstance(bar.datetime_nano, bool) or not isinstance(bar.datetime_nano, int) or bar.datetime_nano <= 0:
            raise ValueError("datetime_nano must be a positive integer")
        if bar.duration_seconds != 86400 or bar.ts.utcoffset() is None:
            raise ValueError("only timezone-aware raw daily observations are supported")
    ordered = tuple(sorted(bars, key=lambda bar: bar.datetime_nano))
    if any(a.datetime_nano == b.datetime_nano for a, b in zip(ordered, ordered[1:])):
        raise ValueError("duplicate datetime_nano in selected contract")
    return declaration, product, instance, ordered


def _validate_prices(bars: tuple[ContractBar, ...], config: ScenarioConfig) -> dict[str, int]:
    unusable = Counter({"volume": 0, "open_oi": 0, "close_oi": 0})
    adverse_move = config.price_tick * config.slippage_ticks
    for index, bar in enumerate(bars):
        if any(not isinstance(getattr(bar, name), D) for name in ("open", "high", "low", "close")):
            raise ValueError(f"OHLC prices must be finite Decimal values at observation {index}")
        prices = {name: _number(f"OHLC {name} at observation {index}", getattr(bar, name), "0.000000000001", "1000000000000")
                  for name in ("open", "high", "low", "close")}
        if not prices["low"] <= min(prices["open"], prices["close"]) <= max(prices["open"], prices["close"]) <= prices["high"]:
            raise ValueError(f"invalid OHLC envelope at observation {index}")
        if min(prices["open"], prices["close"]) <= adverse_move:
            raise ValueError(f"slippage permits a non-positive effective sell price at observation {index}")
        for name in unusable:
            value = getattr(bar, name)
            if value is None or not isinstance(value, D) or not value.is_finite() or value < 0:
                unusable[name] += 1
    return dict(unusable)


def run_scenario_backtest(
    bundle: InputBundle, *, symbol: str, strategy: str, config: ScenarioConfig,
) -> ScenarioResult:
    """Pure replay over one verified bundle view; publication is a separate API.

    The caller must verify disk bytes with read_input_bundle. No settings,
    database, broker, clock or network operations occur in this function.
    """
    if strategy not in STRATEGIES:
        raise ValueError("unsupported strategy; carry requires far legs; choose " + ", ".join(STRATEGIES))
    if not isinstance(config, ScenarioConfig):
        raise ValueError("config must be an explicit validated ScenarioConfig")
    with localcontext(_ARITHMETIC):
        return _replay(bundle, symbol, strategy, config)


def _replay(bundle, symbol, strategy_id, config):
    declaration, product, instance, bars = _select_contract(bundle, symbol)
    unusable = _validate_prices(bars, config)
    strategy = {"donchian_trend": DonchianTrend, "ma_trend": MATrend,
                "zscore_reversion": ZScoreReversion}[strategy_id]()
    strategy_parameters = asdict(strategy)
    state = StrategyState(strategy_id, contract_instance_id=instance)
    fills, marks, decisions = [], [], []
    cash = peak = previous_equity = config.initial_cash
    realized = fees = slip = gross = D(0)
    position, entry, pending = 0, D(0), None
    max_drawdown = max_drawdown_ratio = D(0)
    opening_blocks = 0
    adverse_move = config.price_tick * config.slippage_ticks

    def costs(price, lots):
        flat = config.fee_per_lot * lots
        rate = config.fee_rate * price * config.multiplier * lots
        slippage = adverse_move * config.multiplier * lots
        return flat, rate, slippage

    def fill(index, side, offset, price, signal_index, phase):
        nonlocal cash, realized, fees, slip, position, entry
        quantity = abs(position) if offset == "close" else config.lots
        flat, rate, charge = costs(price, quantity)
        pnl = (price - entry) * position * config.multiplier if offset == "close" else D(0)
        realized += pnl
        fees += flat + rate
        slip += charge
        cash += pnl - flat - rate - charge
        position = 0 if offset == "close" else side * quantity
        entry = D(0) if offset == "close" else price
        bar = bars[index]
        fills.append(ScenarioFill(
            f"fill-{len(fills):06d}", index, signal_index, bar.datetime_nano,
            bar.ts.isoformat(), phase, offset, side, quantity, price,
            price + side * adverse_move, flat, rate, flat + rate, charge,
            pnl, cash, position,
        ))

    for index, bar in enumerate(bars):
        before_position = position
        daily_gross = D(0) if index == 0 else before_position * config.multiplier * (bar.open - bars[index - 1].close)
        prior_fees, prior_slip = fees, slip
        execution_status = "no_preceding_target"
        margin_needed = after_cost = None
        if pending is not None:
            if pending == position:
                execution_status = "target_unchanged"
            else:
                execution_status = "closed" if position else "flat"
                if position:
                    fill(index, -1 if position > 0 else 1, "close", bar.open, index - 1, "next_open")
                if pending:
                    flat, rate, charge = costs(bar.open, abs(pending))
                    after_cost = cash - flat - rate - charge
                    margin_needed = abs(pending) * bar.open * config.multiplier * config.margin_rate
                    if after_cost <= 0:
                        execution_status = "opening_costs_exhaust_equity"
                        opening_blocks += 1
                    elif margin_needed > after_cost * config.max_margin_ratio:
                        execution_status = "opening_margin_blocked"
                        opening_blocks += 1
                    else:
                        fill(index, 1 if pending > 0 else -1, "open", bar.open, index - 1, "next_open")
                        execution_status = "reversed" if before_position else "opened"
        daily_gross += position * config.multiplier * (bar.close - bar.open)
        terminal = index == len(bars) - 1
        if terminal and position:
            fill(index, -1 if position > 0 else 1, "close", bar.close, None, "terminal_close")
        unrealized = position * config.multiplier * (bar.close - entry)
        equity = cash + unrealized
        gross += daily_gross
        required_margin = abs(position) * bar.close * config.multiplier * config.margin_rate
        breached = bool(position and (equity <= 0 or required_margin > equity * config.max_margin_ratio))
        peak = max(peak, equity)
        drawdown = peak - equity
        drawdown_ratio = drawdown / peak
        max_drawdown = max(max_drawdown, drawdown)
        max_drawdown_ratio = max(max_drawdown_ratio, drawdown_ratio)
        # Independent daily mark identity catches sign, gap and reversal errors.
        if equity != previous_equity + daily_gross - (fees - prior_fees) - (slip - prior_slip):
            raise ArithmeticError("daily equity reconciliation failed")
        if cash != config.initial_cash + realized - fees - slip or gross != realized + unrealized:
            raise ArithmeticError("cash or gross PnL reconciliation failed")
        marks.append(ScenarioEquity(
            index, bar.datetime_nano, bar.ts.isoformat(), bar.close, position,
            cash, realized, unrealized, equity, daily_gross, gross, fees, slip,
            equity - config.initial_cash, required_margin, equity - required_margin,
            breached, drawdown, drawdown_ratio, terminal,
        ))
        previous_equity = equity
        # Every fixed feature is local to at most 60 observations. Keeping that
        # bounded prefix avoids quadratic scans for long stored histories.
        prefix = bars[max(0, index + 1 - max(60, strategy.spec.required_bars)):index + 1]
        context = StrategyContext(
            strategy_id, product, instance, symbol, None, ClockStatus.OBSERVATION_ONLY,
            str(bar.datetime_nano), prefix, build_structure_frame(prefix), {},
        )
        evaluation = strategy.evaluate(context, state)
        raw_candidate = evaluation.diagnostics.get("candidate_direction")
        candidate = raw_candidate
        reason = evaluation.diagnostics.get("underlying_reason", evaluation.reason)
        if isinstance(strategy, ZScoreReversion) and state.direction and state.signal_age is not None and state.signal_age >= strategy.max_signal_age:
            candidate, reason = 0, "max_signal_age_reached"
        if candidate is not None and (isinstance(candidate, bool) or candidate not in (-1, 0, 1)):
            raise ValueError("evaluator emitted an invalid candidate direction")
        direction = state.direction if candidate is None else candidate
        age = None if direction == 0 else (0 if direction != state.direction or state.signal_age is None else state.signal_age + 1)
        state = StrategyState(strategy_id, direction, age, instance)
        target = candidate * config.lots if candidate is not None else None
        decisions.append(ScenarioDecision(
            index, bar.datetime_nano, bar.ts.isoformat(), None, "observation_only",
            evaluation.status, str(reason), raw_candidate, candidate, target,
            state.direction, state.signal_age, index - 1 if index else None,
            pending, execution_status, margin_needed, after_cost, terminal,
        ))
        pending = target

    net = marks[-1].equity - config.initial_cash
    config_payload = {
        "schema_version": SCHEMA_VERSION, "symbol": symbol, "contract_instance_id": instance,
        "strategy": strategy_id, "strategy_parameters": strategy_parameters,
        "parameter_selection": "fixed_no_optimization", "assumptions": config.to_dict(),
        "assumptions_source": "user_configured", "assumptions_verification": "not_historical_verified",
        "historical_strict_execution": False, "terminal_policy": "precommitted_final_observation_close",
        "fee_basis": "fee_per_lot_plus_fee_rate_times_ideal_notional_all_legs",
        "slippage_basis": "cash_expense_on_ideal_prices_once_per_fill_leg",
        "decimal_precision": _ARITHMETIC.prec, "ratio_rounding": "ROUND_HALF_EVEN",
    }
    report = {
        "schema_version": SCHEMA_VERSION, "status": "complete",
        "dataset_kind": "historical_single_contract_daily_scenario",
        "historical_strict_execution": False, "assumptions_verification": "not_historical_verified",
        "symbol": symbol, "contract_instance_id": instance, "strategy": strategy_id,
        "contract_identity_status": declaration.get("identity_status", "unverified"),
        "observations": len(bars), "first_datetime_nano": bars[0].datetime_nano,
        "last_datetime_nano": bars[-1].datetime_nano, "fills": len(fills),
        "closed_trades": sum(fill.offset == "close" for fill in fills),
        "opening_blocks": opening_blocks, "margin_breach_observations": sum(mark.margin_breached for mark in marks),
        "nonpositive_equity_observations": sum(mark.equity <= 0 for mark in marks),
        "candidate_counts": dict(sorted(Counter("null" if row.candidate_direction is None else str(row.candidate_direction) for row in decisions).items())),
        "unusable_nonpricing_fields": unusable, "initial_cash": _text(config.initial_cash),
        "ending_cash": _text(cash), "ending_equity": _text(marks[-1].equity),
        "ending_position_lots": position, "realized_pnl": _text(realized),
        "unrealized_pnl": _text(marks[-1].unrealized_pnl), "gross_pnl": _text(gross),
        "fees": _text(fees), "slippage": _text(slip), "net_pnl": _text(net),
        "total_return": _text(net / config.initial_cash),
        "max_drawdown": _text(max_drawdown), "max_drawdown_ratio": _text(max_drawdown_ratio),
        "turnover_lots": sum(fill.lots for fill in fills),
        "turnover_notional": _text(sum((fill.lots * fill.ideal_price * config.multiplier for fill in fills), D(0))),
        "reconciliation": {
            "cash_equals_initial_plus_realized_less_costs": cash == config.initial_cash + realized - fees - slip,
            "equity_equals_cash_plus_unrealized": marks[-1].equity == cash + marks[-1].unrealized_pnl,
            "gross_equals_realized_plus_unrealized": gross == realized + marks[-1].unrealized_pnl,
            "net_equals_gross_less_costs": net == gross - fees - slip,
            "daily_equity_identity": True, "terminal_flat": position == 0,
        },
        "event_order": list(EVENT_ORDER), "limitations": list(LIMITATIONS),
    }
    return ScenarioResult(config_payload, tuple(fills), tuple(marks), tuple(decisions), report)


def _reject_json_constant(value: str):
    raise ValueError(f"nonfinite JSON constant is unsupported: {value}")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _strict_json(payload: bytes, *, decimal_floats: bool = False):
    return json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object,
                      parse_float=D if decimal_floats else float,
                      parse_constant=_reject_json_constant)


def _json_object(payload: bytes, *, decimal_floats: bool = False):
    result = _strict_json(payload, decimal_floats=decimal_floats)
    if not isinstance(result, dict):
        raise ValueError("JSON document must be an object")
    return result


def _csv_bytes(record_type, rows) -> bytes:
    buffer = io.StringIO(newline="")
    names = [field.name for field in fields(record_type)]
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(names)
    for row in rows:
        cells = []
        for name in names:
            value = getattr(row, name)
            cells.append("" if value is None else "true" if value is True else "false" if value is False
                         else _text(value) if isinstance(value, D) else str(value))
        writer.writerow(cells)
    return buffer.getvalue().encode("utf-8")


# Bind every local behavior dependency, including transitive imports. Only
# these explicit source files are read: never repository .env/settings/data DBs.
_CODE_PATHS = (
    "src/ak_trading/__init__.py", "src/ak_trading/futures/__init__.py",
    "src/ak_trading/futures/scenario_backtest.py", "src/ak_trading/futures/spec.py",
    "src/ak_trading/futures/strategies.py", "src/ak_trading/futures/structures.py",
    "src/ak_trading/futures/io.py", "src/ak_trading/futures/data.py",
    "src/ak_trading/strategy.py", "scripts/backtest_history.py",
)
_ROOT = Path(__file__).resolve().parents[3]


def _safe_path(path: Path) -> Path:
    path = Path(path)
    for part in path.parts:
        # Windows device aliases can act as streams even when Path.exists is
        # false. Do not read or create them on any platform.
        stem = part.rstrip(" .").split(".", 1)[0].upper()
        if stem in {"NUL", "CON", "PRN", "AUX", "CONIN$", "CONOUT$"} or re.fullmatch(r"(?:COM|LPT)[1-9]", stem):
            raise ValueError("reserved device paths are unsupported")
    return path.resolve()


def run_history_scenario(
    input_bundle: Path, output: Path, *, symbol: str, strategy: str, config_path: Path,
) -> dict[str, object]:
    """Verify a frozen bundle, run a scenario, publish a new deterministic dir.

    Invalid input or config never creates an output directory. After successful
    validation/replay, a write failure leaves an incomplete manifest if the
    filesystem still permits writing one. The complete manifest is written last.
    """
    if Path(output).is_symlink():
        raise OutputDirectoryError("output may not be an existing symlink")
    source, destination, config_file = map(_safe_path, (input_bundle, output, config_path))
    forbidden = (source, config_file, _ROOT / "src", _ROOT / "scripts", _ROOT / "tests", _ROOT / "config")
    assert_output_directory_is_new(destination, forbidden)
    if destination.is_symlink():
        raise OutputDirectoryError("output may not be a symlink")
    if not source.is_dir():
        raise ValueError("input bundle directory does not exist")
    if not config_file.is_file():
        raise ValueError("explicit config file does not exist")
    if config_file.stat().st_size > 16_384:
        raise ValueError("config file exceeds 16 KiB")
    config_bytes = config_file.read_bytes()
    config = ScenarioConfig.from_mapping(_json_object(config_bytes, decimal_floats=True))
    manifest_path = source / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = _json_object(manifest_bytes)
    declared_hashes = manifest.get("content_hashes")
    if not isinstance(declared_hashes, dict):
        raise ValueError("input content_hashes must be an object")
    # The existing reader verifies all four core payloads from their captured
    # bytes. Reject unsafe sidecar paths and bind all optional sidecars as well.
    for name, digest in declared_hashes.items():
        if (not isinstance(name, str) or name in ("", ".", "..", "manifest.json")
                or any(character in name for character in ("/", "\\", ":"))
                or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None):
            raise ValueError("invalid input content hash declaration")
        target = _safe_path(source / name)
        if target.parent != source:
            raise ValueError("input payload resolves outside bundle")
    for name in ("contracts.json", "metadata.json", "availability.json"):
        captured = (source / name).read_bytes()
        if sha256_bytes(captured) != declared_hashes.get(name):
            raise ValueError(f"content hash mismatch for {name}")
        parsed = _strict_json(captured)
        if name == "contracts.json":
            if not isinstance(parsed, list) or any(not isinstance(row, dict) for row in parsed):
                raise ValueError("contracts.json must be a list of contract objects")
        elif not isinstance(parsed, dict):
            raise ValueError(f"{name} must be an object")
    # The reader's independently captured payloads must match these same hashes,
    # binding strict JSON validation to its consumed bytes without an unverified
    # second parse. No existing io.py behavior is changed for other callers.
    bundle = read_input_bundle(source)
    if bundle.manifest != manifest or manifest_path.read_bytes() != manifest_bytes:
        raise ValueError("input manifest changed while reading")
    for name, expected in declared_hashes.items():
        if name not in {"bars.csv", "contracts.json", "metadata.json", "availability.json"}:
            if sha256_file(source / name) != expected:
                raise ValueError(f"content hash mismatch for {name}")
    code_hashes = {name: sha256_file(_ROOT / name) for name in _CODE_PATHS}
    result = run_scenario_backtest(bundle, symbol=symbol, strategy=strategy, config=config)
    # Detect changes during replay rather than publishing a code digest from a
    # moving working tree. No source files are modified by this check.
    if code_hashes != {name: sha256_file(_ROOT / name) for name in _CODE_PATHS}:
        raise ValueError("scenario source code changed during replay")
    payloads = {
        "config.json": canonical_json_bytes(dict(result.config)),
        "fills.csv": _csv_bytes(ScenarioFill, result.fills),
        "equity.csv": _csv_bytes(ScenarioEquity, result.equity),
        "decisions.csv": _csv_bytes(ScenarioDecision, result.decisions),
        "report.json": canonical_json_bytes(dict(result.report)),
    }
    provenance = {
        "schema_version": SCHEMA_VERSION,
        "dataset_kind": "historical_single_contract_daily_scenario",
        "historical_strict_execution": False,
        "assumptions_verification": "not_historical_verified",
        "input_manifest_sha256": sha256_bytes(manifest_bytes),
        "input_config_sha256": sha256_bytes(config_bytes),
        "input_content_hashes": declared_hashes,
        "source_hashes": manifest.get("source_hashes", {}),
        "source_hashes_status": "copied_frozen_export_provenance_not_external_reverified",
        "code_hashes": code_hashes,
    }
    assert_output_directory_is_new(destination, forbidden)
    destination.mkdir(parents=True, exist_ok=False)
    complete_manifest = dict(provenance, status="complete",
                             content_hashes={name: sha256_bytes(payload) for name, payload in payloads.items()})
    written = {}

    def incomplete_bytes(exc):
        return canonical_json_bytes(dict(
            provenance, status="incomplete", content_hashes=dict(written),
            error_type=type(exc).__name__, reason="artifact_publication_failed",
        ))

    try:
        for name, payload in payloads.items():
            path = destination / name
            with path.open("xb") as handle:
                handle.write(payload)
            expected = complete_manifest["content_hashes"][name]
            if sha256_file(path) != expected:
                raise OSError(f"output hash verification failed: {name}")
            written[name] = expected
        with (destination / "manifest.json").open("xb") as handle:
            try:
                handle.write(canonical_json_bytes(complete_manifest))
                handle.flush()
            except BaseException as exc:
                # This descriptor belongs to us. Repair a partial write on
                # that descriptor, never by reopening a possibly replaced path.
                try:
                    handle.seek(0)
                    handle.truncate()
                    handle.write(incomplete_bytes(exc))
                    handle.flush()
                except OSError:
                    pass
                raise
    except BaseException as exc:
        try:
            # A collision may be another writer's marker or a symlink. Exclusive
            # creation preserves both, including a partial marker we own.
            with (destination / "manifest.json").open("xb") as handle:
                handle.write(incomplete_bytes(exc))
        except OSError:
            pass  # Absence of a readable complete marker remains incomplete.
        raise
    return {"status": "complete", "historical_strict_execution": False,
            "symbol": symbol, "strategy": strategy, "observations": result.report["observations"],
            "fills": len(result.fills), "net_pnl": result.report["net_pnl"],
            "output": str(destination)}
