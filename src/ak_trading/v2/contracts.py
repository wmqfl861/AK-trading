"""Frozen public records for the v2.1 probability/strategy library.

This module is the complete cross-node type contract of devplan-v2.1.0 M1
(01-M1-共用基础与时钟契约.md §4). Every dataclass is ``frozen=True``;
``Mapping`` fields are defensively copied into immutable mappings; ordered
collections are tuples; fields carry no implicit defaults. Business I/O is
forbidden here. String enumerations are additionally restricted by the
schema registry in ``ak_trading.v2.schemas``.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from math import isfinite
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Mapping

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
type HorizonUnit = Literal["TRADING_MINUTE", "TRADING_DAY"]
type QualityStatus = Literal["READY", "WARMUP", "MISSING", "INVALID", "STALE", "DEGRADED"]
type BaseRegime = Literal["UP_TREND", "DOWN_TREND", "ORDERED_RANGE", "WIDE_RANGE", "COMPRESSION", "UNKNOWN"]
type RegimeFlag = Literal["PULLBACK", "BREAKOUT", "EVENT_SHOCK", "ILLIQUID"]
type StrategyFamily = Literal["TREND", "BREAKOUT", "PULLBACK", "REVERSION", "GRID", "MARTINGALE", "WAIT"]
type StrategyPhase = Literal["OBSERVE", "WAIT_TRIGGER", "HOLDING", "EXITING", "PAUSED"]
type ProposalAction = Literal["WAIT", "OPEN", "ADD", "HOLD", "REDUCE", "EXIT"]
# 08-M3C §4.1 / 变更申请-007 §5.1: independent exit vocabulary (M3C).
type ExitFamily = Literal["LEGACY", "PROFIT_LADDER", "TRAILING_STOP", "VOLATILITY_STOP",
                          "TIME_DECAY", "SIGNAL_REVERSAL", "DRAWDOWN_EXIT",
                          "OPPORTUNITY_COST", "PARTIAL_LIQUIDATION"]
type ExitAction = Literal["KEEP", "REDUCE", "EXIT"]
type ExitPhase = Literal["MANAGING", "REDUCING", "LIQUIDATING", "CLOSED"]
type ExitMode = Literal["MINUTE", "DAILY"]
type ExitExecutionKind = Literal["NEXT_WINDOW_CLOSE", "LEGACY_PROTECTIVE"]
type ExitTiming = Literal["NEXT_LEGAL_WINDOW", "LEGACY_PROTECTIVE"]
type ExitWindowKind = Literal["CURRENT_STATE", "EPISODE_PREFIX",
                              "EPISODE_PREFIX_CALENDAR", "ACCOUNT_PREFIX", "LIQUIDATION_PREFIX"]
type ExitSourceScope = Literal["EPISODE", "ACCOUNT"]
type ExitClockUnit = Literal["NONE", "TRADING_MINUTE", "TRADING_DAY"]

HORIZON_UNITS = frozenset({"TRADING_MINUTE", "TRADING_DAY"})
MINUTE_HORIZON_VALUES = frozenset({1, 5, 15, 30, 60})
DAY_HORIZON_VALUES = frozenset({1, 5, 20})
QUALITY_STATUSES = frozenset({"READY", "WARMUP", "MISSING", "INVALID", "STALE", "DEGRADED"})
TEMPORAL_BASES = frozenset({"observed", "reconstructed", "unknown"})
BASE_REGIMES = frozenset({"UP_TREND", "DOWN_TREND", "ORDERED_RANGE", "WIDE_RANGE", "COMPRESSION", "UNKNOWN"})
REGIME_FLAGS = frozenset({"PULLBACK", "BREAKOUT", "EVENT_SHOCK", "ILLIQUID"})
STRATEGY_FAMILIES = frozenset({"TREND", "BREAKOUT", "PULLBACK", "REVERSION", "GRID", "MARTINGALE", "WAIT"})
STRATEGY_PHASES = frozenset({"OBSERVE", "WAIT_TRIGGER", "HOLDING", "EXITING", "PAUSED"})
PROPOSAL_ACTIONS = frozenset({"WAIT", "OPEN", "ADD", "HOLD", "REDUCE", "EXIT"})
SESSION_KINDS = frozenset({"DAY", "NIGHT"})
FEATURE_SESSION_KINDS = frozenset({"DAY", "NIGHT", "DAILY"})
EVIDENCE_STATUSES = frozenset({"VERIFIED", "SCENARIO", "UNKNOWN"})
FILL_EVIDENCE_STATUSES = frozenset({"VERIFIED", "SCENARIO"})
BARRIER_KEYS = frozenset({"ATR_0.5", "ATR_1", "ATR_2"})
BAR_DURATIONS = frozenset({60, 300, 900, 1800, 3600, 86400})
PERSISTENCE_EVENTS = (
    "UP_TREND", "DOWN_TREND", "ORDERED_RANGE", "WIDE_RANGE", "COMPRESSION", "UP_BREAKOUT", "DOWN_BREAKOUT",
)
DIRECTION_KEYS = ("UP", "DOWN", "NEUTRAL")
PATH_KEYS = ("UP_FIRST", "DOWN_FIRST", "NEITHER", "AMBIGUOUS")
QUANTILE_KEYS = ("Q05", "Q25", "Q50", "Q75", "Q95")
STOP_TOUCH_KEYS = ("LONG", "SHORT")
TIME_PROBABILITY_EVENTS = frozenset({"UP_TOUCH", "DOWN_TOUCH", "RETURN_FIRST", "BREAK_FIRST"})
PUBLICATION_STATES = frozenset({"published", "unpublished", "unknown"})
DEGRADATION_STATES = frozenset({"normal", "degraded"})
MACRO_KINDS = frozenset({"FACT", "ECON_OPINION", "DERIVED_TAG"})
MACRO_MODES = frozenset({"PRICE_ONLY", "WITH_MACRO", "BLOCKED"})
RUN_PURPOSES = frozenset({"RESEARCH_EVALUATION", "SHADOW_OPERATIONAL"})
ROUTING_MODES = frozenset({"BEST_EV", "PROBABILITY_SAMPLE", "FIXED", "CASH"})
COST_SCENARIOS = frozenset({"BASE", "FEE_X2", "SLIPPAGE_X2", "MARGIN_X1_5", "GAP", "NO_STOP"})
STATISTICS_STATUSES = frozenset({"PASS", "FAIL", "INSUFFICIENT"})
VALIDATION_STAGES = frozenset({"RESEARCH", "VALIDATED", "SHADOW"})
LABEL_STATUSES = frozenset({"MATURE", "UNMATURED", "MISSING", "INVALID"})
PATH_OUTCOMES = frozenset({"UP_FIRST", "DOWN_FIRST", "NEITHER", "AMBIGUOUS"})
COMPETING_OUTCOMES = frozenset({"RETURN_FIRST", "BREAK_FIRST", "NEITHER", "AMBIGUOUS"})
LABEL_DIRECTIONS = frozenset({"UP", "DOWN", "NEUTRAL"})
EVIDENCE_STATUSES_4 = frozenset({"VALID", "INSUFFICIENT", "FAILED", "EXPIRED"})
OFFSET_KINDS = frozenset({"OPEN", "CLOSE", "CLOSE_TODAY"})
INTENT_PURPOSES = frozenset({"ENTRY", "ADD", "REDUCE", "STOP", "TARGET", "TIME_EXIT", "ROLL_EXIT"})
EXECUTION_STATUSES = frozenset({"FILLED", "PARTIAL", "BLOCKED", "EXPIRED", "CANCELLED"})
PAYOFF_STATUSES = frozenset({"READY", "INSUFFICIENT", "INVALID"})
DECISION_OUTCOMES = frozenset({"SELECTED", "NO_TRADE", "MANAGE_POSITION"})
CROSS_TIMEFRAME_VALUES = frozenset({"ALIGNED", "COUNTERTREND", "CONFLICT", "UNKNOWN"})
VALUATION_STATUSES = frozenset({"CURRENT", "STALE", "UNVALUED"})
VALUATION_STALE_REASONS = frozenset({"PRICE_MISSING", "PRICE_STALE", "RULE_MISSING"})
# 01 L767 / 变更申请-003 §3-4: STALE and UNVALUED share the same uppercase
# triple subset PRICE_MISSING/PRICE_STALE/RULE_MISSING (same constant).
VALUATION_UNVALUED_REASONS = VALUATION_STALE_REASONS
QUALIFICATION_MODEL_HASH_KEYS = ("forecast", "payoff", "regime_reference")
QUALIFICATION_COST_SCENARIOS = ("BASE", "FEE_X2", "SLIPPAGE_X2", "MARGIN_X1_5", "GAP", "NO_STOP")
_HEX = frozenset("0123456789abcdef")
PROBABILITY_SUM_TOLERANCE = 1e-12
QUANTILE_UNIT_TOLERANCE = Decimal("1e-9")

# ─── 08-M3C / 007 §5.1: exit-contract vocabulary and frozen key sets ───

EXIT_FAMILIES = frozenset({"LEGACY", "PROFIT_LADDER", "TRAILING_STOP", "VOLATILITY_STOP",
                           "TIME_DECAY", "SIGNAL_REVERSAL", "DRAWDOWN_EXIT",
                           "OPPORTUNITY_COST", "PARTIAL_LIQUIDATION"})
EXIT_ACTIONS = frozenset({"KEEP", "REDUCE", "EXIT"})
EXIT_PHASES = frozenset({"MANAGING", "REDUCING", "LIQUIDATING", "CLOSED"})
EXIT_MODES = frozenset({"MINUTE", "DAILY"})
EXIT_MODE_DURATIONS = {"MINUTE": 60, "DAILY": 86400}
EXIT_EXECUTION_KINDS = frozenset({"NEXT_WINDOW_CLOSE", "LEGACY_PROTECTIVE"})
EXIT_TIMINGS = frozenset({"NEXT_LEGAL_WINDOW", "LEGACY_PROTECTIVE"})
EXIT_WINDOW_KINDS = frozenset({"CURRENT_STATE", "EPISODE_PREFIX",
                               "EPISODE_PREFIX_CALENDAR", "ACCOUNT_PREFIX",
                               "LIQUIDATION_PREFIX"})
EXIT_SOURCE_SCOPES = frozenset({"EPISODE", "ACCOUNT"})
EXIT_CLOCK_UNITS = frozenset({"NONE", "TRADING_MINUTE", "TRADING_DAY"})

# 08 §6.2/§6.5: exactly 30 precise EXITSTATE keys (14 dual-mode definitions
# expanded over d60/d86400 plus the two single-mode age keys). Runtime keys
# never contain a literal "{d}" brace — the expansion below IS the registry.
EXIT_STATE_KEYS: tuple[str, ...] = tuple(sorted(
    f"EXITSTATE:{name}:v1.0.0:d{duration}"
    for name, durations in (
        ("unrealized_pnl_cny", (60, 86400)),
        ("unrealized_pnl_r", (60, 86400)),
        ("entry_distance_atr", (60, 86400)),
        ("episode_net_liquidation_cny", (60, 86400)),
        ("episode_net_liquidation_r", (60, 86400)),
        ("peak_net_liquidation_r", (60, 86400)),
        ("profit_giveback_r", (60, 86400)),
        ("peak_favorable_price", (60, 86400)),
        ("trailing_atr_distance", (60, 86400)),
        ("effective_stop_distance_atr", (60, 86400)),
        ("age_trading_minutes", (60,)),
        ("age_trading_days", (86400,)),
        ("remaining_steps", (60, 86400)),
        ("elapsed_fraction", (60, 86400)),
        ("account_drawdown_ratio", (60, 86400)),
        ("closed_fraction", (60, 86400)),
    )
    for duration in durations
))
# Reserved namespaces: P0 validators reject these keys anywhere an
# EXITSTATE key is accepted; unlocking requires a formal change (08 §6.3/§6.4).
EXITCTX_RESERVED_KEYS: tuple[str, ...] = tuple(sorted(
    f"EXITCTX:{name}:v1.0.0:d{duration}"
    for name in ("continue_value_cny", "next_best_value_cny", "switch_advantage_lcb_r")
    for duration in (60, 86400)))
EXITMKT_RESERVED_KEYS: tuple[str, ...] = tuple(
    f"EXITMKT:signed_volume_pressure:v1.0.0:d{duration}:body-vr20"
    for duration in (60, 300, 900, 1800, 3600, 86400))

# 08 §5.4: exact parameter whitelists per family (key sets must match
# exactly — no missing keys, no extra keys; values validated per family).
EXIT_PARAMETER_WHITELISTS: Mapping[str, frozenset[str]] = MappingProxyType({
    "LEGACY": frozenset(),
    "PROFIT_LADDER": frozenset({"profit_levels_r", "cumulative_close_fractions",
                                "confirmation_bars"}),
    "TRAILING_STOP": frozenset({"arm_profit_r", "trail_atr_multiple", "atr_basis",
                                "peak_source"}),
    "VOLATILITY_STOP": frozenset({"atr_basis", "atr_multiple", "confirmation_bars"}),
    "TIME_DECAY": frozenset({"soft_time_fractions", "minimum_progress_r",
                             "cumulative_close_fractions"}),
    "SIGNAL_REVERSAL": frozenset({"confirmation_bars"}),
    "DRAWDOWN_EXIT": frozenset({"arm_peak_profit_r", "max_giveback_r"}),
    "OPPORTUNITY_COST": frozenset({"minimum_improvement_r", "confirmation_bars"}),
    "PARTIAL_LIQUIDATION": frozenset({"sizing_mode", "lot_rounding", "final_step"}),
})
EXIT_FEATURE_FORMULA_VERSION = "v1.0.0"


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_int(value: object, name: str) -> None:
    if not _is_int(value):
        raise TypeError(f"{name}_must_be_int_not_bool")


def _check_str(value: object, name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name}_must_be_str")


def _check_optional_str(value: object, name: str) -> None:
    if value is not None:
        _check_str(value, name)


def _check_decimal(value: object, name: str) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name}_must_be_decimal")
    if not value.is_finite():
        raise ValueError(f"{name}_must_be_finite")


def _check_optional_decimal(value: object, name: str) -> None:
    if value is not None:
        _check_decimal(value, name)


def _check_float(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, float):
        raise TypeError(f"{name}_must_be_float")
    if not isfinite(value):
        raise ValueError(f"{name}_must_be_finite")


def _check_optional_float(value: object, name: str) -> None:
    if value is not None:
        _check_float(value, name)


def _check_probability(value: object, name: str) -> None:
    _check_float(value, name)
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name}_must_be_within_0_1")


def _check_optional_probability(value: object, name: str) -> None:
    if value is not None:
        _check_probability(value, name)


def _check_bool(value: object, name: str) -> None:
    if not isinstance(value, bool):
        raise TypeError(f"{name}_must_be_bool")


def _check_nano(value: object, name: str) -> None:
    _check_int(value, name)


def _check_optional_nano(value: object, name: str) -> None:
    if value is not None:
        _check_nano(value, name)


def _check_hash(value: object, name: str) -> None:
    _check_str(value, name)
    if len(value) != 64 or any(char not in _HEX for char in value):
        raise ValueError(f"{name}_must_be_64_lowercase_hex_sha256")


def _check_enum(value: object, allowed: frozenset[str], name: str) -> None:
    _check_str(value, name)
    if value not in allowed:
        raise ValueError(f"{name}_not_in_allowed_enum")


def _check_str_tuple(value: object, name: str) -> None:
    if not isinstance(value, tuple):
        raise TypeError(f"{name}_must_be_tuple")
    for item in value:
        _check_str(item, f"{name}_item")


def _check_date(value: object, name: str) -> None:
    if not isinstance(value, date):
        raise TypeError(f"{name}_must_be_date")


def _check_side(value: object, name: str) -> None:
    if not _is_int(value) or value not in (-1, 1):
        raise ValueError(f"{name}_must_be_minus1_or_1")


def _check_json_scalar(value: object, name: str) -> None:
    if value is None or isinstance(value, (bool, str)):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError(f"{name}_must_be_finite")
        return
    raise TypeError(f"{name}_must_be_json_scalar")


def _freeze_json(value: object, name: str) -> object:
    if isinstance(value, Mapping) and not isinstance(value, (str, bytes)):
        return MappingProxyType({key: _freeze_json(item, f"{name}[{key!r}]") for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, f"{name}[]") for item in value)
    _check_json_scalar(value, name)
    return value


def _freeze_mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name}_must_be_mapping")
    return MappingProxyType({key: _freeze_json(item, f"{name}[{key!r}]") for key, item in value.items()})


def _freeze_typed_mapping(value: object, name: str) -> Mapping[str, object]:
    """Shallow defensive copy for typed mappings whose values are not JSON."""
    if not isinstance(value, Mapping):
        raise TypeError(f"{name}_must_be_mapping")
    return MappingProxyType(dict(value))


def _check_typed_mapping(value: object, name: str, value_check) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name}_must_be_mapping")
    for key, item in value.items():
        _check_str(key, f"{name}_key")
        value_check(item, f"{name}_value")
    return MappingProxyType(dict(value))


def _check_probability_mapping(value: object, name: str) -> None:
    if isinstance(value, Mapping):
        for item in value.values():
            _check_probability(item, f"{name}_value")


def _mapping_keys(value: Mapping[str, object]) -> frozenset[str]:
    return frozenset(value.keys())


def _check_probability_dict(value: object, name: str, expected_keys: tuple[str, ...]) -> None:
    frozen = _freeze_typed_mapping(value, name)
    if _mapping_keys(frozen) != frozenset(expected_keys):
        raise ValueError(f"{name}_keys_must_be_exactly_{expected_keys}")
    for key in expected_keys:
        _check_probability(frozen[key], f"{name}[{key}]")
    if abs(sum(float(frozen[key]) for key in expected_keys) - 1.0) > PROBABILITY_SUM_TOLERANCE:
        raise ValueError(f"{name}_must_sum_to_1_within_1e-12")


def _check_quantile_floats(value: object, name: str) -> None:
    frozen = _freeze_typed_mapping(value, name)
    if _mapping_keys(frozen) != frozenset(QUANTILE_KEYS):
        raise ValueError(f"{name}_keys_must_be_exactly_{QUANTILE_KEYS}")
    ordered = [frozen[key] for key in QUANTILE_KEYS]
    for item in ordered:
        _check_float(item, f"{name}_value")
    if not all(ordered[i] <= ordered[i + 1] for i in range(len(ordered) - 1)):
        raise ValueError(f"{name}_must_be_nondecreasing")


def _check_quantile_decimals(value: object, name: str) -> None:
    frozen = _freeze_typed_mapping(value, name)
    if _mapping_keys(frozen) != frozenset(QUANTILE_KEYS):
        raise ValueError(f"{name}_keys_must_be_exactly_{QUANTILE_KEYS}")
    ordered = [frozen[key] for key in QUANTILE_KEYS]
    for item in ordered:
        _check_decimal(item, f"{name}_value")
    if not all(ordered[i] <= ordered[i + 1] for i in range(len(ordered) - 1)):
        raise ValueError(f"{name}_must_be_nondecreasing")


@dataclass(frozen=True)
class Horizon:
    unit: HorizonUnit
    value: int

    def __post_init__(self) -> None:
        _check_enum(self.unit, HORIZON_UNITS, "unit")
        _check_int(self.value, "value")
        if self.unit == "TRADING_MINUTE" and self.value not in MINUTE_HORIZON_VALUES:
            raise ValueError("value_must_be_one_of_1_5_15_30_60_for_TRADING_MINUTE")
        if self.unit == "TRADING_DAY" and self.value not in DAY_HORIZON_VALUES:
            raise ValueError("value_must_be_one_of_1_5_20_for_TRADING_DAY")


@dataclass(frozen=True)
class DataQuality:
    status: QualityStatus
    reasons: tuple[str, ...]
    temporal_basis: Literal["observed", "reconstructed", "unknown"]
    input_hash: str

    def __post_init__(self) -> None:
        _check_enum(self.status, QUALITY_STATUSES, "status")
        _check_str_tuple(self.reasons, "reasons")
        _check_enum(self.temporal_basis, TEMPORAL_BASES, "temporal_basis")
        _check_hash(self.input_hash, "input_hash")


@dataclass(frozen=True)
class Instrument:
    contract_id: str
    raw_symbol: str
    product: str
    sector: str
    risk_groups: tuple[str, ...]
    listing_nano: int | None
    expiry_nano: int | None
    last_trade_nano: int | None
    delivery_cutoff_nano: int | None
    quality: DataQuality

    def __post_init__(self) -> None:
        for name in ("contract_id", "raw_symbol", "product", "sector"):
            _check_str(getattr(self, name), name)
        _check_str_tuple(self.risk_groups, "risk_groups")
        for name in ("listing_nano", "expiry_nano", "last_trade_nano", "delivery_cutoff_nano"):
            _check_optional_nano(getattr(self, name), name)
        if not isinstance(self.quality, DataQuality):
            raise TypeError("quality_must_be_DataQuality")


@dataclass(frozen=True)
class SessionWindow:
    session_id: str
    product: str
    trading_day: date
    start_nano: int
    end_nano: int
    kind: Literal["DAY", "NIGHT"]
    calendar_version: str
    evidence_status: Literal["VERIFIED", "SCENARIO", "UNKNOWN"]

    def __post_init__(self) -> None:
        _check_str(self.session_id, "session_id")
        _check_str(self.product, "product")
        _check_date(self.trading_day, "trading_day")
        _check_nano(self.start_nano, "start_nano")
        _check_nano(self.end_nano, "end_nano")
        _check_enum(self.kind, SESSION_KINDS, "kind")
        _check_str(self.calendar_version, "calendar_version")
        _check_enum(self.evidence_status, EVIDENCE_STATUSES, "evidence_status")
        if self.start_nano >= self.end_nano:
            raise ValueError("start_nano_must_precede_end_nano")


@dataclass(frozen=True)
class MarketBar:
    bar_id: str
    contract_id: str
    raw_symbol: str
    product: str
    duration_seconds: int
    datetime_nano: int
    start_nano: int
    end_nano: int
    trading_day: date | None
    session_id: str | None
    available_at_nano: int | None
    imported_at_nano: int | None
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    volume: Decimal | None
    open_oi: Decimal | None
    close_oi: Decimal | None
    complete: bool
    expected_minutes: int
    actual_minutes: int
    source: str
    quality: DataQuality

    def __post_init__(self) -> None:
        for name in ("bar_id", "contract_id", "raw_symbol", "product", "source"):
            _check_str(getattr(self, name), name)
        _check_int(self.duration_seconds, "duration_seconds")
        if self.duration_seconds not in BAR_DURATIONS:
            raise ValueError("duration_seconds_must_be_60_300_900_1800_3600_86400")
        for name in ("datetime_nano", "start_nano", "end_nano"):
            _check_nano(getattr(self, name), name)
        if self.start_nano >= self.end_nano:
            raise ValueError("start_nano_must_precede_end_nano")
        if self.trading_day is not None:
            _check_date(self.trading_day, "trading_day")
        _check_optional_str(self.session_id, "session_id")
        _check_optional_nano(self.available_at_nano, "available_at_nano")
        _check_optional_nano(self.imported_at_nano, "imported_at_nano")
        for name in ("open", "high", "low", "close", "volume", "open_oi", "close_oi"):
            _check_optional_decimal(getattr(self, name), name)
        for name in ("volume", "open_oi", "close_oi"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name}_must_be_non_negative")
        _check_bool(self.complete, "complete")
        _check_int(self.expected_minutes, "expected_minutes")
        _check_int(self.actual_minutes, "actual_minutes")
        if self.expected_minutes < 0 or self.actual_minutes < 0 or self.actual_minutes > self.expected_minutes:
            raise ValueError("expected_actual_minutes_must_satisfy_0<=actual<=expected")
        if not isinstance(self.quality, DataQuality):
            raise TypeError("quality_must_be_DataQuality")
        prices = (self.open, self.high, self.low, self.close)
        if all(price is not None for price in prices):
            open_, high, low, close = prices
            if not (low <= min(open_, close) and max(open_, close) <= high and low <= high):
                raise ValueError("ohlc_ordering_violated")


@dataclass(frozen=True)
class MacroItem:
    item_id: str
    meeting_id: str | None
    revision: str
    kind: Literal["FACT", "ECON_OPINION", "DERIVED_TAG"]
    derived_tag: str | None
    derivation_version: str | None
    published_at_nano: int | None
    first_seen_nano: int
    retrieved_at_nano: int
    available_at_nano: int
    valid_from_nano: int
    valid_until_nano: int
    publication_state: str
    degradation_state: str
    assets: tuple[str, ...]
    products: tuple[str, ...]
    unit: str | None
    source_urls: tuple[str, ...]
    text: str
    parent_ids: tuple[str, ...]
    raw_sha256: str
    quality: DataQuality

    def __post_init__(self) -> None:
        _check_str(self.item_id, "item_id")
        _check_optional_str(self.meeting_id, "meeting_id")
        _check_str(self.revision, "revision")
        _check_enum(self.kind, MACRO_KINDS, "kind")
        _check_optional_str(self.derived_tag, "derived_tag")
        _check_optional_str(self.derivation_version, "derivation_version")
        if (self.derived_tag is None) != (self.kind != "DERIVED_TAG"):
            raise ValueError("derived_tag_must_be_set_only_for_DERIVED_TAG")
        if (self.derivation_version is None) != (self.kind != "DERIVED_TAG"):
            raise ValueError("derivation_version_must_be_set_only_for_DERIVED_TAG")
        _check_optional_nano(self.published_at_nano, "published_at_nano")
        for name in ("first_seen_nano", "retrieved_at_nano", "available_at_nano",
                     "valid_from_nano", "valid_until_nano"):
            _check_nano(getattr(self, name), name)
        if self.available_at_nano < self.retrieved_at_nano:
            raise ValueError("available_at_nano_must_not_precede_retrieved_at_nano")
        if self.valid_from_nano > self.valid_until_nano:
            raise ValueError("valid_from_must_not_exceed_valid_until")
        _check_enum(self.publication_state, PUBLICATION_STATES, "publication_state")
        _check_enum(self.degradation_state, DEGRADATION_STATES, "degradation_state")
        _check_str_tuple(self.assets, "assets")
        _check_str_tuple(self.products, "products")
        _check_optional_str(self.unit, "unit")
        _check_str_tuple(self.source_urls, "source_urls")
        _check_str(self.text, "text")
        _check_str_tuple(self.parent_ids, "parent_ids")
        if self.kind == "DERIVED_TAG" and not self.parent_ids:
            raise ValueError("DERIVED_TAG_requires_parent_ids")
        _check_hash(self.raw_sha256, "raw_sha256")
        if not isinstance(self.quality, DataQuality):
            raise TypeError("quality_must_be_DataQuality")


@dataclass(frozen=True)
class MacroContext:
    context_id: str
    decision_nano: int
    items: tuple[MacroItem, ...]
    policy_version: str
    risk_tags: tuple[str, ...]
    mode: Literal["PRICE_ONLY", "WITH_MACRO", "BLOCKED"]
    quality: DataQuality

    def __post_init__(self) -> None:
        _check_str(self.context_id, "context_id")
        _check_nano(self.decision_nano, "decision_nano")
        if not isinstance(self.items, tuple) or any(not isinstance(item, MacroItem) for item in self.items):
            raise TypeError("items_must_be_tuple_of_MacroItem")
        _check_str(self.policy_version, "policy_version")
        _check_str_tuple(self.risk_tags, "risk_tags")
        _check_enum(self.mode, MACRO_MODES, "mode")
        if not isinstance(self.quality, DataQuality):
            raise TypeError("quality_must_be_DataQuality")


@dataclass(frozen=True)
class FeeLeg:
    per_lot_cny: Decimal
    notional_rate: Decimal
    explicit_zero: bool

    def __post_init__(self) -> None:
        _check_decimal(self.per_lot_cny, "per_lot_cny")
        _check_decimal(self.notional_rate, "notional_rate")
        _check_bool(self.explicit_zero, "explicit_zero")
        if self.per_lot_cny < 0 or self.notional_rate < 0:
            raise ValueError("fee_leg_must_be_non_negative")
        if self.per_lot_cny == 0 and self.notional_rate == 0 and not self.explicit_zero:
            raise ValueError("zero_fee_requires_explicit_zero")


@dataclass(frozen=True)
class TradingRule:
    rule_id: str
    contract_id: str
    effective_from_nano: int
    effective_until_nano: int
    available_at_nano: int
    multiplier: Decimal
    price_tick: Decimal
    lot_step: int
    min_open_lots: int
    open_fee: FeeLeg | None
    close_fee: FeeLeg | None
    close_today_fee: FeeLeg | None
    long_margin_rate: Decimal | None
    short_margin_rate: Decimal | None
    limit_up: Decimal | None
    limit_down: Decimal | None
    slippage_ticks: Decimal
    impact_ticks: Decimal
    participation_rate: Decimal
    evidence: Literal["VERIFIED", "SCENARIO", "UNKNOWN"]
    source_sha256: str

    def __post_init__(self) -> None:
        _check_str(self.rule_id, "rule_id")
        _check_str(self.contract_id, "contract_id")
        for name in ("effective_from_nano", "effective_until_nano", "available_at_nano"):
            _check_nano(getattr(self, name), name)
        if self.effective_from_nano >= self.effective_until_nano:
            raise ValueError("effective_from_must_precede_effective_until")
        _check_decimal(self.multiplier, "multiplier")
        _check_decimal(self.price_tick, "price_tick")
        if self.multiplier <= 0 or self.price_tick <= 0:
            raise ValueError("multiplier_and_price_tick_must_be_positive")
        _check_int(self.lot_step, "lot_step")
        _check_int(self.min_open_lots, "min_open_lots")
        if self.lot_step < 1 or self.min_open_lots < 1:
            raise ValueError("lot_step_and_min_open_lots_must_be_at_least_1")
        for name in ("open_fee", "close_fee", "close_today_fee"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, FeeLeg):
                raise TypeError(f"{name}_must_be_FeeLeg_or_None")
        for name in ("long_margin_rate", "short_margin_rate"):
            value = getattr(self, name)
            _check_optional_decimal(value, name)
            if value is not None and not 0 <= value <= 1:
                raise ValueError(f"{name}_must_be_within_0_1")
        for name in ("limit_up", "limit_down"):
            value = getattr(self, name)
            _check_optional_decimal(value, name)
            if value is not None and value <= 0:
                raise ValueError(f"{name}_must_be_positive")
        _check_decimal(self.slippage_ticks, "slippage_ticks")
        _check_decimal(self.impact_ticks, "impact_ticks")
        _check_decimal(self.participation_rate, "participation_rate")
        if self.slippage_ticks < 0 or self.impact_ticks < 0:
            raise ValueError("slippage_and_impact_ticks_must_be_non_negative")
        if not 0 <= self.participation_rate <= 1:
            raise ValueError("participation_rate_must_be_within_0_1")
        _check_enum(self.evidence, EVIDENCE_STATUSES, "evidence")
        _check_hash(self.source_sha256, "source_sha256")


@dataclass(frozen=True)
class FrozenInput:
    root: Path
    manifest_sha256: str
    instruments: tuple[Instrument, ...]
    sessions: tuple[SessionWindow, ...]
    bars: tuple[MarketBar, ...]
    macro_items: tuple[MacroItem, ...]
    rules: tuple[TradingRule, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.root, Path):
            raise TypeError("root_must_be_Path")
        _check_hash(self.manifest_sha256, "manifest_sha256")
        for name, record_type in (
            ("instruments", Instrument), ("sessions", SessionWindow), ("bars", MarketBar),
            ("macro_items", MacroItem), ("rules", TradingRule),
        ):
            value = getattr(self, name)
            if not isinstance(value, tuple) or any(not isinstance(item, record_type) for item in value):
                raise TypeError(f"{name}_must_be_tuple_of_{record_type.__name__}")


@dataclass(frozen=True)
class FactorSpec:
    factor_key: str
    source_factor_id: str
    operator_version: str
    parameter_version: str
    duration_seconds: int
    lookback: int
    parameters: Mapping[str, JsonValue]
    unit: str
    required_fields: tuple[str, ...]
    formula: str
    source_sha256: str

    def __post_init__(self) -> None:
        for name in ("factor_key", "source_factor_id", "operator_version", "parameter_version", "unit", "formula"):
            _check_str(getattr(self, name), name)
        _check_int(self.duration_seconds, "duration_seconds")
        if self.duration_seconds not in BAR_DURATIONS:
            raise ValueError("duration_seconds_must_be_60_300_900_1800_3600_86400")
        _check_int(self.lookback, "lookback")
        if self.lookback < 1:
            raise ValueError("lookback_must_be_at_least_1")
        object.__setattr__(self, "parameters", _freeze_mapping(self.parameters, "parameters"))
        _check_str_tuple(self.required_fields, "required_fields")
        _check_hash(self.source_sha256, "source_sha256")


@dataclass(frozen=True)
class FactorValue:
    factor_key: str
    value: Decimal | float | None
    available_at_nano: int | None
    quality: DataQuality

    def __post_init__(self) -> None:
        _check_str(self.factor_key, "factor_key")
        if self.value is not None:
            if isinstance(self.value, Decimal):
                _check_decimal(self.value, "value")
            else:
                _check_float(self.value, "value")
        _check_optional_nano(self.available_at_nano, "available_at_nano")
        if not isinstance(self.quality, DataQuality):
            raise TypeError("quality_must_be_DataQuality")


@dataclass(frozen=True)
class FeatureSnapshot:
    snapshot_id: str
    contract_id: str
    raw_symbol: str
    product: str
    trading_day: date
    session_id: str
    session_kind: Literal["DAY", "NIGHT", "DAILY"]
    decision_nano: int
    available_at_nano: int
    duration_seconds: int
    source_bar_ids: tuple[str, ...]
    reference_price: Decimal | None
    atr_points: Decimal | None
    values: tuple[FactorValue, ...]
    daily_background_id: str | None
    quality: DataQuality

    def __post_init__(self) -> None:
        for name in ("snapshot_id", "contract_id", "raw_symbol", "product", "session_id"):
            _check_str(getattr(self, name), name)
        _check_date(self.trading_day, "trading_day")
        _check_enum(self.session_kind, FEATURE_SESSION_KINDS, "session_kind")
        _check_nano(self.decision_nano, "decision_nano")
        _check_nano(self.available_at_nano, "available_at_nano")
        if self.available_at_nano > self.decision_nano:
            raise ValueError("available_at_must_not_be_later_than_decision_nano")
        _check_int(self.duration_seconds, "duration_seconds")
        if self.duration_seconds not in BAR_DURATIONS:
            raise ValueError("duration_seconds_must_be_60_300_900_1800_3600_86400")
        _check_str_tuple(self.source_bar_ids, "source_bar_ids")
        _check_optional_decimal(self.reference_price, "reference_price")
        if self.reference_price is not None and self.reference_price <= 0:
            raise ValueError("reference_price_must_be_positive")
        _check_optional_decimal(self.atr_points, "atr_points")
        if self.atr_points is not None and self.atr_points < 0:
            raise ValueError("atr_points_must_be_non_negative")
        if not isinstance(self.values, tuple) or any(not isinstance(item, FactorValue) for item in self.values):
            raise TypeError("values_must_be_tuple_of_FactorValue")
        _check_optional_str(self.daily_background_id, "daily_background_id")
        if not isinstance(self.quality, DataQuality):
            raise TypeError("quality_must_be_DataQuality")


@dataclass(frozen=True)
class FrozenRange:
    range_id: str
    frozen_at_nano: int
    lower: Decimal
    center: Decimal
    upper: Decimal
    stop_lower: Decimal
    stop_upper: Decimal
    source_snapshot_id: str

    def __post_init__(self) -> None:
        _check_str(self.range_id, "range_id")
        _check_nano(self.frozen_at_nano, "frozen_at_nano")
        for name in ("lower", "center", "upper", "stop_lower", "stop_upper"):
            _check_decimal(getattr(self, name), name)
        _check_str(self.source_snapshot_id, "source_snapshot_id")
        if not (self.stop_lower < self.lower < self.center < self.upper < self.stop_upper):
            raise ValueError("boundaries_must_satisfy_stop_lower<lower<center<upper<stop_upper")


@dataclass(frozen=True)
class ForecastRequest:
    request_id: str
    features: FeatureSnapshot
    horizon: Horizon
    window_start_nano: int
    window_end_nano: int
    barrier_key: str
    upper_distance: Decimal
    lower_distance: Decimal
    frozen_range: FrozenRange | None

    def __post_init__(self) -> None:
        _check_str(self.request_id, "request_id")
        if not isinstance(self.features, FeatureSnapshot):
            raise TypeError("features_must_be_FeatureSnapshot")
        if not isinstance(self.horizon, Horizon):
            raise TypeError("horizon_must_be_Horizon")
        _check_nano(self.window_start_nano, "window_start_nano")
        _check_nano(self.window_end_nano, "window_end_nano")
        if self.window_start_nano >= self.window_end_nano:
            raise ValueError("window_start_must_precede_window_end")
        _check_enum(self.barrier_key, BARRIER_KEYS, "barrier_key")
        _check_decimal(self.upper_distance, "upper_distance")
        _check_decimal(self.lower_distance, "lower_distance")
        if self.upper_distance <= 0 or self.lower_distance <= 0:
            raise ValueError("barrier_distances_must_be_positive")
        if self.frozen_range is not None and not isinstance(self.frozen_range, FrozenRange):
            raise TypeError("frozen_range_must_be_FrozenRange_or_None")


@dataclass(frozen=True)
class PathLabel:
    request_id: str
    contract_id: str
    label_version: str
    horizon: Horizon
    window_start_nano: int
    window_end_nano: int
    available_at_nano: int | None
    status: Literal["MATURE", "UNMATURED", "MISSING", "INVALID"]
    reasons: tuple[str, ...]
    direction: Literal["UP", "DOWN", "NEUTRAL"] | None
    terminal_return: float | None
    terminal_points: Decimal | None
    terminal_atr: float | None
    path_outcome: Literal["UP_FIRST", "DOWN_FIRST", "NEITHER", "AMBIGUOUS"] | None
    first_up_step: int | None
    first_down_step: int | None
    return_step: int | None
    break_step: int | None
    competing_outcome: Literal["RETURN_FIRST", "BREAK_FIRST", "NEITHER", "AMBIGUOUS"] | None
    persistence: Mapping[str, bool | None]
    stop_touched: Mapping[str, bool | None]
    range_id: str | None

    def __post_init__(self) -> None:
        _check_str(self.request_id, "request_id")
        _check_str(self.contract_id, "contract_id")
        _check_str(self.label_version, "label_version")
        if not isinstance(self.horizon, Horizon):
            raise TypeError("horizon_must_be_Horizon")
        _check_nano(self.window_start_nano, "window_start_nano")
        _check_nano(self.window_end_nano, "window_end_nano")
        _check_optional_nano(self.available_at_nano, "available_at_nano")
        _check_enum(self.status, LABEL_STATUSES, "status")
        _check_str_tuple(self.reasons, "reasons")
        if self.direction is not None:
            _check_enum(self.direction, LABEL_DIRECTIONS, "direction")
        _check_optional_float(self.terminal_return, "terminal_return")
        _check_optional_decimal(self.terminal_points, "terminal_points")
        _check_optional_float(self.terminal_atr, "terminal_atr")
        if self.path_outcome is not None:
            _check_enum(self.path_outcome, PATH_OUTCOMES, "path_outcome")
        for name in ("first_up_step", "first_down_step", "return_step", "break_step"):
            value = getattr(self, name)
            if value is not None:
                _check_int(value, name)
                if value < 1:
                    raise ValueError(f"{name}_must_be_at_least_1")
        if self.competing_outcome is not None:
            _check_enum(self.competing_outcome, COMPETING_OUTCOMES, "competing_outcome")
        persistence = _check_typed_mapping(
            self.persistence, "persistence",
            lambda value, name: (_check_bool(value, name) if value is not None else None),
        )
        if _mapping_keys(persistence) != frozenset(PERSISTENCE_EVENTS):
            raise ValueError("persistence_keys_must_list_all_events")
        object.__setattr__(self, "persistence", persistence)
        stop = _check_typed_mapping(
            self.stop_touched, "stop_touched",
            lambda value, name: (_check_bool(value, name) if value is not None else None),
        )
        if _mapping_keys(stop) not in (frozenset(), frozenset(STOP_TOUCH_KEYS)):
            raise ValueError("stop_touched_keys_must_be_exactly_LONG_SHORT_or_empty")
        if self.range_id is None and _mapping_keys(stop):
            raise ValueError("stop_touched_requires_range")
        object.__setattr__(self, "stop_touched", stop)
        _check_optional_str(self.range_id, "range_id")


@dataclass(frozen=True)
class CalibrationEvidence:
    evidence_id: str
    model_id: str
    policy_id: str
    product: str
    session_kind: Literal["DAY", "NIGHT", "DAILY"]
    duration_seconds: int
    base_regime: BaseRegime
    barrier_key: str
    geometry_key: str
    event: str
    horizon: Horizon
    label_version: str
    bucket: str
    sample_count: int
    effective_blocks: int
    calibration_error: float | None
    brier: float | None
    training_end_nano: int
    calibration_end_nano: int
    valid_until_nano: int
    status: Literal["VALID", "INSUFFICIENT", "FAILED", "EXPIRED"]

    def __post_init__(self) -> None:
        for name in ("evidence_id", "model_id", "policy_id", "product", "barrier_key",
                     "geometry_key", "event", "label_version", "bucket"):
            _check_str(getattr(self, name), name)
        _check_enum(self.session_kind, FEATURE_SESSION_KINDS, "session_kind")
        _check_int(self.duration_seconds, "duration_seconds")
        if self.duration_seconds not in BAR_DURATIONS:
            raise ValueError("duration_seconds_must_be_60_300_900_1800_3600_86400")
        _check_enum(self.base_regime, BASE_REGIMES, "base_regime")
        if not isinstance(self.horizon, Horizon):
            raise TypeError("horizon_must_be_Horizon")
        _check_int(self.sample_count, "sample_count")
        _check_int(self.effective_blocks, "effective_blocks")
        if self.sample_count < 0 or self.effective_blocks < 0:
            raise ValueError("sample_count_and_effective_blocks_must_be_non_negative")
        _check_optional_float(self.calibration_error, "calibration_error")
        _check_optional_float(self.brier, "brier")
        for name in ("training_end_nano", "calibration_end_nano", "valid_until_nano"):
            _check_nano(getattr(self, name), name)
        if not self.training_end_nano <= self.calibration_end_nano <= self.valid_until_nano:
            raise ValueError("training_calibration_valid_nanos_must_be_ordered")
        _check_enum(self.status, EVIDENCE_STATUSES_4, "status")


@dataclass(frozen=True)
class TimeProbability:
    event: str
    step: int
    probability: float
    unknown_probability: float

    def __post_init__(self) -> None:
        _check_enum(self.event, TIME_PROBABILITY_EVENTS, "event")
        _check_int(self.step, "step")
        if self.step < 1:
            raise ValueError("step_must_be_at_least_1")
        _check_probability(self.probability, "probability")
        _check_probability(self.unknown_probability, "unknown_probability")
        if self.probability + self.unknown_probability > 1.0 + PROBABILITY_SUM_TOLERANCE:
            raise ValueError("probability_plus_unknown_must_not_exceed_1")


@dataclass(frozen=True)
class MarketForecast:
    forecast_id: str
    request_id: str
    snapshot_id: str
    contract_id: str
    product: str
    trading_day: date
    session_id: str
    session_kind: Literal["DAY", "NIGHT", "DAILY"]
    duration_seconds: int
    assessment_id: str
    base_regime: BaseRegime
    regime_reference_id: str
    decision_nano: int
    published_at_nano: int
    expires_at_nano: int
    horizon: Horizon
    window_start_nano: int
    window_end_nano: int
    barrier_key: str
    reference_price: Decimal
    atr_points: Decimal
    upper_distance: Decimal
    lower_distance: Decimal
    frozen_range: FrozenRange | None
    label_version: str
    model_id: str
    direction_probabilities: Mapping[str, float]
    return_quantiles: Mapping[str, float]
    point_quantiles: Mapping[str, Decimal]
    atr_quantiles: Mapping[str, float]
    path_probabilities: Mapping[str, float]
    time_cdf: tuple[TimeProbability, ...]
    persistence_probabilities: Mapping[str, float]
    stop_touch_probabilities: Mapping[str, float]
    return_before_break_probability: float | None
    evidence: tuple[CalibrationEvidence, ...]
    out_of_distribution: bool
    quality: DataQuality

    def __post_init__(self) -> None:
        for name in ("forecast_id", "request_id", "snapshot_id", "contract_id", "product",
                     "session_id", "assessment_id", "regime_reference_id", "label_version", "model_id"):
            _check_str(getattr(self, name), name)
        _check_date(self.trading_day, "trading_day")
        _check_enum(self.session_kind, FEATURE_SESSION_KINDS, "session_kind")
        _check_int(self.duration_seconds, "duration_seconds")
        if self.duration_seconds not in BAR_DURATIONS:
            raise ValueError("duration_seconds_must_be_60_300_900_1800_3600_86400")
        _check_enum(self.base_regime, BASE_REGIMES, "base_regime")
        if not isinstance(self.horizon, Horizon):
            raise TypeError("horizon_must_be_Horizon")
        for name in ("decision_nano", "published_at_nano", "expires_at_nano",
                     "window_start_nano", "window_end_nano"):
            _check_nano(getattr(self, name), name)
        if self.decision_nano > self.published_at_nano:
            raise ValueError("decision_must_not_be_later_than_published")
        if not self.decision_nano < self.window_start_nano < self.window_end_nano:
            raise ValueError("decision_lt_window_start_lt_window_end_required")
        _check_enum(self.barrier_key, BARRIER_KEYS, "barrier_key")
        _check_decimal(self.reference_price, "reference_price")
        _check_decimal(self.atr_points, "atr_points")
        _check_decimal(self.upper_distance, "upper_distance")
        _check_decimal(self.lower_distance, "lower_distance")
        if self.reference_price <= 0:
            raise ValueError("reference_price_must_be_positive")
        if self.atr_points <= 0:
            raise ValueError("atr_points_must_be_positive")
        if self.upper_distance <= 0 or self.lower_distance <= 0:
            raise ValueError("barrier_distances_must_be_positive")
        if self.frozen_range is not None and not isinstance(self.frozen_range, FrozenRange):
            raise TypeError("frozen_range_must_be_FrozenRange_or_None")
        _check_probability_dict(self.direction_probabilities, "direction_probabilities", DIRECTION_KEYS)
        object.__setattr__(self, "direction_probabilities",
                           _freeze_typed_mapping(self.direction_probabilities, "direction_probabilities"))
        _check_quantile_floats(self.return_quantiles, "return_quantiles")
        object.__setattr__(self, "return_quantiles",
                           _freeze_typed_mapping(self.return_quantiles, "return_quantiles"))
        _check_quantile_decimals(self.point_quantiles, "point_quantiles")
        object.__setattr__(self, "point_quantiles",
                           _freeze_typed_mapping(self.point_quantiles, "point_quantiles"))
        _check_quantile_floats(self.atr_quantiles, "atr_quantiles")
        object.__setattr__(self, "atr_quantiles",
                           _freeze_typed_mapping(self.atr_quantiles, "atr_quantiles"))
        _check_probability_dict(self.path_probabilities, "path_probabilities", PATH_KEYS)
        object.__setattr__(self, "path_probabilities",
                           _freeze_typed_mapping(self.path_probabilities, "path_probabilities"))
        # F16 (006 §4 / 03 §5.3): quantiles of different units must be
        # approximately consistent under the SAME positive reference price C
        # and ATR: point[i] ≈ return[i]×C and point[i] ≈ atr[i]×ATR.
        # float statistics are converted via Decimal(str(value)) per the
        # shared numeric policy (00 §4.3) — no implicit float*Decimal mixing.
        # The comparison uses the public QUANTILE_UNIT_TOLERANCE (relative
        # to max(|point|,1)), generous enough for downstream float→Decimal
        # rounding but tight enough to reject genuinely conflicting sets
        # (e.g. return/atr all-zero with non-zero point).
        for key in QUANTILE_KEYS:
            point = self.point_quantiles[key]  # Decimal
            ret_dec = Decimal(str(self.return_quantiles[key]))
            atr_dec = Decimal(str(self.atr_quantiles[key]))
            derived_return = ret_dec * self.reference_price
            derived_atr = atr_dec * self.atr_points
            if abs(derived_return - point) > QUANTILE_UNIT_TOLERANCE:
                raise ValueError(f"quantile_unit_inconsistency_point_vs_return:{key}")
            if abs(derived_atr - point) > QUANTILE_UNIT_TOLERANCE:
                raise ValueError(f"quantile_unit_inconsistency_point_vs_atr:{key}")
        if not isinstance(self.time_cdf, tuple) or any(not isinstance(item, TimeProbability) for item in self.time_cdf):
            raise TypeError("time_cdf_must_be_tuple_of_TimeProbability")
        persistence = _freeze_typed_mapping(self.persistence_probabilities, "persistence_probabilities")
        if not _mapping_keys(persistence) <= frozenset(PERSISTENCE_EVENTS):
            raise ValueError("persistence_probabilities_keys_not_in_event_set")
        _check_probability_mapping(persistence, "persistence_probabilities")
        object.__setattr__(self, "persistence_probabilities", persistence)
        stop = _freeze_typed_mapping(self.stop_touch_probabilities, "stop_touch_probabilities")
        if not _mapping_keys(stop) <= frozenset(STOP_TOUCH_KEYS):
            raise ValueError("stop_touch_probabilities_keys_must_be_LONG_SHORT_subset")
        if self.frozen_range is not None and _mapping_keys(stop) != frozenset(STOP_TOUCH_KEYS):
            raise ValueError("range_forecast_requires_both_stop_probabilities")
        if self.frozen_range is None and _mapping_keys(stop):
            raise ValueError("stop_touch_probabilities_require_frozen_range")
        _check_probability_mapping(stop, "stop_touch_probabilities")
        object.__setattr__(self, "stop_touch_probabilities", stop)
        _check_optional_probability(self.return_before_break_probability, "return_before_break_probability")
        if not isinstance(self.evidence, tuple) or any(not isinstance(item, CalibrationEvidence) for item in self.evidence):
            raise TypeError("evidence_must_be_tuple_of_CalibrationEvidence")
        _check_bool(self.out_of_distribution, "out_of_distribution")
        if not isinstance(self.quality, DataQuality):
            raise TypeError("quality_must_be_DataQuality")
        # Publication/expiry branches (变更申请-002 §4): a late record keeps
        # the original request/window and is only constructible as
        # quality=INVALID with reason FORECAST_LATE; its expiry stays the
        # original window_end and may precede published. Only the late branch
        # is exempt from expires > published; a plain INVALID record is not,
        # and on-time records must satisfy published < expires <= window_end.
        late = self.published_at_nano >= self.window_start_nano
        if late and not (self.quality.status == "INVALID"
                         and "FORECAST_LATE" in self.quality.reasons):
            raise ValueError("late_publication_requires_INVALID_and_FORECAST_LATE")
        if not late and "FORECAST_LATE" in self.quality.reasons:
            raise ValueError("FORECAST_LATE_marker_requires_actual_lateness")
        if late:
            if self.expires_at_nano != self.window_end_nano:
                raise ValueError("late_publication_expiry_must_equal_original_window_end")
        elif not self.published_at_nano < self.expires_at_nano <= self.window_end_nano:
            raise ValueError(
                "on_time_publication_requires_published_lt_expires_lte_window_end")


@dataclass(frozen=True)
class RegimeAssessment:
    assessment_id: str
    snapshot_id: str
    reference_id: str
    decision_nano: int
    base: BaseRegime
    flags: tuple[RegimeFlag, ...]
    frozen_range: FrozenRange | None
    cross_timeframe: Literal["ALIGNED", "COUNTERTREND", "CONFLICT", "UNKNOWN"]
    confirmed_observations: int
    reasons: tuple[str, ...]
    quality: DataQuality

    def __post_init__(self) -> None:
        for name in ("assessment_id", "snapshot_id", "reference_id"):
            _check_str(getattr(self, name), name)
        _check_nano(self.decision_nano, "decision_nano")
        _check_enum(self.base, BASE_REGIMES, "base")
        _check_str_tuple(self.flags, "flags")
        if any(flag not in REGIME_FLAGS for flag in self.flags):
            raise ValueError("flags_not_in_allowed_enum")
        if self.frozen_range is not None and not isinstance(self.frozen_range, FrozenRange):
            raise TypeError("frozen_range_must_be_FrozenRange_or_None")
        _check_enum(self.cross_timeframe, CROSS_TIMEFRAME_VALUES, "cross_timeframe")
        _check_int(self.confirmed_observations, "confirmed_observations")
        if self.confirmed_observations < 0:
            raise ValueError("confirmed_observations_must_be_non_negative")
        _check_str_tuple(self.reasons, "reasons")
        if not isinstance(self.quality, DataQuality):
            raise TypeError("quality_must_be_DataQuality")


@dataclass(frozen=True)
class HoldingPolicy:
    intraday: bool
    allow_lunch: bool
    allow_night_to_day: bool
    stop_open_before_minutes: int
    flatten_before_minutes: int

    def __post_init__(self) -> None:
        for name in ("intraday", "allow_lunch", "allow_night_to_day"):
            _check_bool(getattr(self, name), name)
        for name in ("stop_open_before_minutes", "flatten_before_minutes"):
            _check_int(getattr(self, name), name)
            if getattr(self, name) < 0:
                raise ValueError(f"{name}_must_be_non_negative")


@dataclass(frozen=True)
class ConfidencePolicy:
    policy_id: str
    minimum_persistence: float
    maximum_stop_touch: float | None
    minimum_samples: int
    minimum_blocks: int
    maximum_calibration_error: float
    maximum_ambiguity: float
    confirmation_observations: int
    exit_persistence: float
    cooldown_minutes: int
    minimum_conservative_ev_cny: Decimal

    def __post_init__(self) -> None:
        _check_str(self.policy_id, "policy_id")
        _check_probability(self.minimum_persistence, "minimum_persistence")
        _check_optional_probability(self.maximum_stop_touch, "maximum_stop_touch")
        for name in ("minimum_samples", "minimum_blocks"):
            _check_int(getattr(self, name), name)
            if getattr(self, name) < 1:
                raise ValueError(f"{name}_must_be_at_least_1")
        _check_float(self.maximum_calibration_error, "maximum_calibration_error")
        if self.maximum_calibration_error < 0:
            raise ValueError("maximum_calibration_error_must_be_non_negative")
        _check_float(self.maximum_ambiguity, "maximum_ambiguity")
        if self.maximum_ambiguity < 0:
            raise ValueError("maximum_ambiguity_must_be_non_negative")
        _check_int(self.confirmation_observations, "confirmation_observations")
        if self.confirmation_observations < 1:
            raise ValueError("confirmation_observations_must_be_at_least_1")
        _check_probability(self.exit_persistence, "exit_persistence")
        _check_int(self.cooldown_minutes, "cooldown_minutes")
        if self.cooldown_minutes < 0:
            raise ValueError("cooldown_minutes_must_be_non_negative")
        _check_decimal(self.minimum_conservative_ev_cny, "minimum_conservative_ev_cny")


@dataclass(frozen=True)
class StressPolicy:
    policy_id: str
    gap_atr_multiple: Decimal
    locked_trading_days: int
    adverse_atr_per_day: Decimal
    release_extra_atr: Decimal
    fee_multiplier: Decimal
    slippage_multiplier: Decimal
    margin_multiplier: Decimal

    def __post_init__(self) -> None:
        _check_str(self.policy_id, "policy_id")
        for name in ("gap_atr_multiple", "adverse_atr_per_day", "release_extra_atr",
                     "fee_multiplier", "slippage_multiplier", "margin_multiplier"):
            _check_decimal(getattr(self, name), name)
            if getattr(self, name) <= 0:
                raise ValueError(f"{name}_must_be_positive")
        _check_int(self.locked_trading_days, "locked_trading_days")
        if self.locked_trading_days < 1:
            raise ValueError("locked_trading_days_must_be_at_least_1")


@dataclass(frozen=True)
class RunPolicy:
    purpose: Literal["RESEARCH_EVALUATION", "SHADOW_OPERATIONAL"]
    routing_mode: Literal["BEST_EV", "PROBABILITY_SAMPLE", "FIXED", "CASH"]
    seed: int
    fixed_strategy_id: str | None
    fixed_exit_binding_sha256: str | None
    research_policy_version: str
    macro_policy_version: str
    macro_mode: Literal["PRICE_ONLY", "WITH_MACRO"]
    cost_scenario: Literal["BASE", "FEE_X2", "SLIPPAGE_X2", "MARGIN_X1_5", "GAP", "NO_STOP"]

    def __post_init__(self) -> None:
        _check_enum(self.purpose, RUN_PURPOSES, "purpose")
        _check_enum(self.routing_mode, ROUTING_MODES, "routing_mode")
        _check_int(self.seed, "seed")
        if (self.routing_mode == "FIXED") != (self.fixed_strategy_id is not None):
            raise ValueError("fixed_strategy_id_required_exactly_for_FIXED_mode")
        _check_optional_str(self.fixed_strategy_id, "fixed_strategy_id")
        # 007 §5.3: FIXED uniquely binds strategy_id + exit binding; every
        # other routing mode must pass None explicitly.
        if self.routing_mode == "FIXED":
            if self.fixed_exit_binding_sha256 is None:
                raise ValueError("FIXED_mode_requires_fixed_exit_binding_sha256")
            _check_hash(self.fixed_exit_binding_sha256, "fixed_exit_binding_sha256")
        elif self.fixed_exit_binding_sha256 is not None:
            raise ValueError("fixed_exit_binding_sha256_only_for_FIXED_mode")
        _check_str(self.research_policy_version, "research_policy_version")
        _check_str(self.macro_policy_version, "macro_policy_version")
        _check_enum(self.macro_mode, frozenset({"PRICE_ONLY", "WITH_MACRO"}), "macro_mode")
        _check_enum(self.cost_scenario, COST_SCENARIOS, "cost_scenario")


@dataclass(frozen=True)
class StrategyQualification:
    qualification_id: str
    strategy_id: str
    strategy_version: str
    parameter_version: str
    code_sha256: str
    exit_registry_sha256: str
    exit_binding_sha256: str
    exit_spec_sha256s: tuple[str, ...]
    product: str
    contract_ids: tuple[str, ...]
    mode: Literal["MINUTE", "DAILY"]
    horizons: tuple[Horizon, ...]
    macro_mode: Literal["PRICE_ONLY", "WITH_MACRO"]
    research_policy_version: str
    macro_policy_version: str
    cost_scenarios: tuple[str, ...]
    model_hashes: Mapping[str, str]
    experiment_sha256: str
    statistics: Literal["PASS", "FAIL", "INSUFFICIENT"]
    report_sha256: str
    review_sha256: str
    issued_at_nano: int
    valid_until_nano: int
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("qualification_id", "strategy_id", "strategy_version", "parameter_version",
                     "product", "research_policy_version", "macro_policy_version"):
            _check_str(getattr(self, name), name)
        for name in ("code_sha256", "experiment_sha256", "report_sha256", "review_sha256",
                     "exit_registry_sha256", "exit_binding_sha256"):
            _check_hash(getattr(self, name), name)
        if not isinstance(self.exit_spec_sha256s, tuple) or not self.exit_spec_sha256s:
            raise ValueError("exit_spec_sha256s_must_be_non_empty_tuple")
        for item in self.exit_spec_sha256s:
            _check_hash(item, "exit_spec_sha256s_item")
        if list(self.exit_spec_sha256s) != sorted(set(self.exit_spec_sha256s)):
            raise ValueError("exit_spec_sha256s_must_be_sorted_unique")
        _check_str_tuple(self.contract_ids, "contract_ids")
        if not self.contract_ids:
            raise ValueError("contract_ids_must_be_pre_registered_non_empty")
        _check_enum(self.mode, frozenset({"MINUTE", "DAILY"}), "mode")
        if not self.horizons or any(not isinstance(h, Horizon) for h in self.horizons):
            raise TypeError("horizons_must_be_non_empty_tuple_of_Horizon")
        _check_enum(self.macro_mode, frozenset({"PRICE_ONLY", "WITH_MACRO"}), "macro_mode")
        if tuple(self.cost_scenarios) != QUALIFICATION_COST_SCENARIOS:
            raise ValueError("cost_scenarios_must_be_fixed_six_in_order")
        model_hashes = _freeze_mapping(self.model_hashes, "model_hashes")
        if not set(model_hashes) <= {*QUALIFICATION_MODEL_HASH_KEYS, "seasonality"}:
            raise ValueError("model_hashes_keys_must_be_forecast_payoff_regime_reference_seasonality")
        if not set(QUALIFICATION_MODEL_HASH_KEYS) <= set(model_hashes):
            raise ValueError("model_hashes_requires_forecast_payoff_regime_reference")
        for value in model_hashes.values():
            _check_hash(value, "model_hashes_value")
        object.__setattr__(self, "model_hashes", model_hashes)
        _check_enum(self.statistics, STATISTICS_STATUSES, "statistics")
        _check_nano(self.issued_at_nano, "issued_at_nano")
        _check_nano(self.valid_until_nano, "valid_until_nano")
        if self.valid_until_nano <= self.issued_at_nano:
            raise ValueError("valid_until_must_be_after_issued")
        _check_str_tuple(self.reasons, "reasons")


@dataclass(frozen=True)
class StrategySpec:
    strategy_id: str
    family: StrategyFamily
    version: str
    parameter_version: str
    code_sha256: str
    mode: Literal["MINUTE", "DAILY"]
    parameters: Mapping[str, JsonValue]
    required_factor_keys: tuple[str, ...]
    allowed_regimes: tuple[BaseRegime, ...]
    allowed_flags: tuple[RegimeFlag, ...]
    confidence_policy_id: str
    path_barrier_key: str
    max_holding: Horizon
    holding_policy: HoldingPolicy
    max_layers: int
    max_total_lots: int
    validation_stage: Literal["RESEARCH", "VALIDATED", "SHADOW"]

    def __post_init__(self) -> None:
        _check_str(self.strategy_id, "strategy_id")
        _check_enum(self.family, STRATEGY_FAMILIES, "family")
        for name in ("version", "parameter_version", "confidence_policy_id"):
            _check_str(getattr(self, name), name)
        _check_hash(self.code_sha256, "code_sha256")
        _check_enum(self.mode, frozenset({"MINUTE", "DAILY"}), "mode")
        object.__setattr__(self, "parameters", _freeze_mapping(self.parameters, "parameters"))
        _check_str_tuple(self.required_factor_keys, "required_factor_keys")
        _check_str_tuple(self.allowed_regimes, "allowed_regimes")
        if any(regime not in BASE_REGIMES for regime in self.allowed_regimes):
            raise ValueError("allowed_regimes_not_in_allowed_enum")
        _check_str_tuple(self.allowed_flags, "allowed_flags")
        if any(flag not in REGIME_FLAGS for flag in self.allowed_flags):
            raise ValueError("allowed_flags_not_in_allowed_enum")
        _check_enum(self.path_barrier_key, BARRIER_KEYS, "path_barrier_key")
        if self.path_barrier_key != "ATR_1":
            raise ValueError("path_barrier_key_fixed_to_ATR_1_in_v2_1_0")
        if not isinstance(self.max_holding, Horizon):
            raise TypeError("max_holding_must_be_Horizon")
        if not isinstance(self.holding_policy, HoldingPolicy):
            raise TypeError("holding_policy_must_be_HoldingPolicy")
        _check_int(self.max_layers, "max_layers")
        _check_int(self.max_total_lots, "max_total_lots")
        if self.family == "WAIT":
            # WAIT never opens risk (04-M3B §4.1): exactly 0/0, never a 1/1 placeholder.
            if self.max_layers != 0 or self.max_total_lots != 0:
                raise ValueError("WAIT_max_layers_and_max_total_lots_must_be_exactly_0")
        elif self.max_layers < 1 or self.max_total_lots < 1:
            raise ValueError("trading_families_require_max_layers_and_max_total_lots_at_least_1")
        _check_enum(self.validation_stage, VALIDATION_STAGES, "validation_stage")


@dataclass(frozen=True)
class LayerSpec:
    index: int
    trigger_price: Decimal
    reference_entry_price: Decimal
    lots: int

    def __post_init__(self) -> None:
        _check_int(self.index, "index")
        if self.index < 0:
            raise ValueError("index_must_be_0_based_non_negative")
        _check_decimal(self.trigger_price, "trigger_price")
        if self.trigger_price <= 0:
            raise ValueError("trigger_price_must_be_positive")
        _check_decimal(self.reference_entry_price, "reference_entry_price")
        if self.reference_entry_price <= 0:
            raise ValueError("reference_entry_price_must_be_positive")
        _check_int(self.lots, "lots")
        if self.lots < 1:
            raise ValueError("lots_must_be_positive_integer")


@dataclass(frozen=True)
class RoundBudget:
    budget_id: str
    episode_id: str
    contract_id: str
    frozen_at_nano: int
    range_id: str | None
    frozen_range: FrozenRange | None
    stress_policy: StressPolicy
    initial_atr_points: Decimal
    side: Literal[-1, 1]
    layers: tuple[LayerSpec, ...]
    stop_price: Decimal
    target_price: Decimal
    latest_exit_nano: int
    maximum_lots: int
    planned_loss_cny: Decimal
    stress_loss_cny: Decimal
    reserved_margin_cny: Decimal
    day_loss_limit_cny: Decimal
    equity_at_start_cny: Decimal

    def __post_init__(self) -> None:
        for name in ("budget_id", "episode_id", "contract_id"):
            _check_str(getattr(self, name), name)
        _check_nano(self.frozen_at_nano, "frozen_at_nano")
        _check_optional_str(self.range_id, "range_id")
        if self.frozen_range is not None:
            if not isinstance(self.frozen_range, FrozenRange):
                raise TypeError("frozen_range_must_be_FrozenRange_or_None")
            if self.range_id is None or self.frozen_range.range_id != self.range_id:
                raise ValueError("frozen_range_range_id_must_match_budget_range_id")
        if not isinstance(self.stress_policy, StressPolicy):
            raise TypeError("stress_policy_must_be_StressPolicy")
        _check_decimal(self.initial_atr_points, "initial_atr_points")
        if self.initial_atr_points <= 0:
            raise ValueError("initial_atr_points_must_be_positive")
        _check_side(self.side, "side")
        if not self.layers or any(not isinstance(layer, LayerSpec) for layer in self.layers):
            raise TypeError("layers_must_be_non_empty_tuple_of_LayerSpec")
        if [layer.index for layer in self.layers] != list(range(len(self.layers))):
            raise ValueError("layer_indexes_must_be_consecutive_0_based")
        _check_decimal(self.stop_price, "stop_price")
        _check_decimal(self.target_price, "target_price")
        if self.stop_price <= 0 or self.target_price <= 0:
            raise ValueError("stop_and_target_prices_must_be_positive")
        _check_nano(self.latest_exit_nano, "latest_exit_nano")
        _check_int(self.maximum_lots, "maximum_lots")
        if sum(layer.lots for layer in self.layers) > self.maximum_lots:
            raise ValueError("maximum_lots_must_cover_all_layers")
        for name in ("planned_loss_cny", "stress_loss_cny"):
            _check_decimal(getattr(self, name), name)
            if getattr(self, name) < 0:
                raise ValueError(f"{name}_must_be_non_negative")
        for name in ("reserved_margin_cny", "day_loss_limit_cny", "equity_at_start_cny"):
            _check_decimal(getattr(self, name), name)
            if getattr(self, name) < 0:
                raise ValueError(f"{name}_must_be_non_negative")


@dataclass(frozen=True)
class PositionLot:
    lot_id: str
    contract_id: str
    strategy_id: str
    episode_id: str
    side: Literal[-1, 1]
    lots: int
    opened_at_nano: int
    opened_trading_day: date
    entry_price: Decimal
    opening_fee_cny: Decimal

    def __post_init__(self) -> None:
        for name in ("lot_id", "contract_id", "strategy_id", "episode_id"):
            _check_str(getattr(self, name), name)
        _check_side(self.side, "side")
        _check_int(self.lots, "lots")
        if self.lots < 1:
            raise ValueError("lots_must_be_positive_integer")
        _check_nano(self.opened_at_nano, "opened_at_nano")
        _check_date(self.opened_trading_day, "opened_trading_day")
        _check_decimal(self.entry_price, "entry_price")
        if self.entry_price <= 0:
            raise ValueError("entry_price_must_be_positive")
        _check_decimal(self.opening_fee_cny, "opening_fee_cny")
        if self.opening_fee_cny < 0:
            raise ValueError("opening_fee_cny_must_be_non_negative")


@dataclass(frozen=True)
class PositionSnapshot:
    contract_id: str
    strategy_id: str
    episode_id: str
    lots: int
    average_entry_price: Decimal | None
    pending_exit: bool

    def __post_init__(self) -> None:
        for name in ("contract_id", "strategy_id", "episode_id"):
            _check_str(getattr(self, name), name)
        # Signed net lots: the snapshot has no side field, so the sign of the
        # fill-derived net position is the only direction carrier (01 §4.1,
        # 05 §7.1 signed-lot semantics; shorts constructible without abs()).
        _check_int(self.lots, "lots")
        _check_optional_decimal(self.average_entry_price, "average_entry_price")
        if self.average_entry_price is not None and self.average_entry_price <= 0:
            raise ValueError("average_entry_price_must_be_positive")
        _check_bool(self.pending_exit, "pending_exit")


@dataclass(frozen=True)
class AccountSnapshot:
    account_id: str
    as_of_nano: int
    trading_day: date
    initial_cash_cny: Decimal
    cash_cny: Decimal
    realized_gross_cny: Decimal
    fees_cny: Decimal
    slippage_cny: Decimal
    unrealized_cny: Decimal
    equity_cny: Decimal
    margin_cny: Decimal
    reserved_margin_cny: Decimal
    day_start_equity_cny: Decimal
    day_net_cny: Decimal
    peak_equity_cny: Decimal
    lots: tuple[PositionLot, ...]
    killed: bool
    quality: DataQuality

    def __post_init__(self) -> None:
        _check_str(self.account_id, "account_id")
        _check_nano(self.as_of_nano, "as_of_nano")
        _check_date(self.trading_day, "trading_day")
        for name in ("initial_cash_cny", "cash_cny", "realized_gross_cny", "fees_cny",
                     "slippage_cny", "unrealized_cny", "equity_cny", "margin_cny",
                     "reserved_margin_cny", "day_start_equity_cny", "day_net_cny", "peak_equity_cny"):
            _check_decimal(getattr(self, name), name)
        if self.cash_cny != self.initial_cash_cny + self.realized_gross_cny - self.fees_cny:
            raise ValueError("cash_must_equal_initial_plus_realized_minus_fees")
        if self.equity_cny != self.cash_cny + self.unrealized_cny:
            raise ValueError("equity_must_equal_cash_plus_unrealized")
        if self.day_net_cny != self.equity_cny - self.day_start_equity_cny:
            raise ValueError("day_net_must_equal_equity_minus_day_start_equity")
        if not isinstance(self.lots, tuple) or any(not isinstance(lot, PositionLot) for lot in self.lots):
            raise TypeError("lots_must_be_tuple_of_PositionLot")
        _check_bool(self.killed, "killed")
        if not isinstance(self.quality, DataQuality):
            raise TypeError("quality_must_be_DataQuality")


@dataclass(frozen=True)
class StrategySetup:
    setup_id: str
    strategy_id: str
    contract_id: str
    side: Literal[-1, 1]
    started_at_nano: int
    expires_at_nano: int
    observed_bars: int
    frozen_range: FrozenRange | None
    frozen_ma: Decimal | None
    frozen_atr: Decimal
    trigger_level: Decimal
    armed: bool
    consecutive_confirmations: int
    previous_close: Decimal | None
    last_bar_id: str

    def __post_init__(self) -> None:
        for name in ("setup_id", "strategy_id", "contract_id", "last_bar_id"):
            _check_str(getattr(self, name), name)
        _check_side(self.side, "side")
        _check_nano(self.started_at_nano, "started_at_nano")
        _check_nano(self.expires_at_nano, "expires_at_nano")
        if self.expires_at_nano <= self.started_at_nano:
            raise ValueError("expires_must_be_after_started")
        _check_int(self.observed_bars, "observed_bars")
        if self.observed_bars < 0:
            raise ValueError("observed_bars_must_be_non_negative")
        if self.frozen_range is not None and not isinstance(self.frozen_range, FrozenRange):
            raise TypeError("frozen_range_must_be_FrozenRange_or_None")
        _check_optional_decimal(self.frozen_ma, "frozen_ma")
        if self.frozen_ma is not None and self.frozen_ma <= 0:
            raise ValueError("frozen_ma_must_be_positive")
        _check_decimal(self.frozen_atr, "frozen_atr")
        if self.frozen_atr <= 0:
            raise ValueError("frozen_atr_must_be_positive")
        _check_decimal(self.trigger_level, "trigger_level")
        if self.trigger_level <= 0:
            raise ValueError("trigger_level_must_be_positive")
        _check_bool(self.armed, "armed")
        _check_int(self.consecutive_confirmations, "consecutive_confirmations")
        if self.consecutive_confirmations < 0:
            raise ValueError("consecutive_confirmations_must_be_non_negative")
        _check_optional_decimal(self.previous_close, "previous_close")
        if self.previous_close is not None and self.previous_close <= 0:
            raise ValueError("previous_close_must_be_positive")


@dataclass(frozen=True)
class StrategyState:
    strategy_id: str
    episode_id: str | None
    contract_id: str
    phase: StrategyPhase
    setup: StrategySetup | None
    last_regime: RegimeAssessment | None
    direction: Literal[-1, 0, 1]
    filled_layers: tuple[int, ...]
    layer_filled_lots: tuple[int, ...]
    pending_layer_index: int | None
    active_exit_nano: int | None
    pending_exit_nano: int | None
    pending_intent_ids: tuple[str, ...]
    budget: RoundBudget | None
    confirmation_count: int
    cooldown_until_nano: int | None
    last_event_id: str | None

    def __post_init__(self) -> None:
        _check_str(self.strategy_id, "strategy_id")
        _check_optional_str(self.episode_id, "episode_id")
        _check_str(self.contract_id, "contract_id")
        _check_enum(self.phase, STRATEGY_PHASES, "phase")
        if self.setup is not None and not isinstance(self.setup, StrategySetup):
            raise TypeError("setup_must_be_StrategySetup_or_None")
        if self.last_regime is not None and not isinstance(self.last_regime, RegimeAssessment):
            raise TypeError("last_regime_must_be_RegimeAssessment_or_None")
        if not _is_int(self.direction) or self.direction not in (-1, 0, 1):
            raise ValueError("direction_must_be_minus1_0_or_1")
        for name in ("filled_layers", "layer_filled_lots", "pending_intent_ids"):
            value = getattr(self, name)
            if not isinstance(value, tuple):
                raise TypeError(f"{name}_must_be_tuple")
        for index in self.filled_layers:
            _check_int(index, "filled_layers_item")
            if index < 0:
                raise ValueError("filled_layers_item_must_be_0_based")
        if list(self.filled_layers) != sorted(set(self.filled_layers)):
            raise ValueError("filled_layers_must_be_strictly_increasing")
        for lots in self.layer_filled_lots:
            _check_int(lots, "layer_filled_lots_item")
            if lots < 0:
                raise ValueError("layer_filled_lots_item_must_be_non_negative")
        if self.episode_id is None:
            if (self.budget is not None or self.pending_layer_index is not None
                    or self.active_exit_nano is not None or self.pending_exit_nano is not None
                    or self.pending_intent_ids or self.filled_layers or self.layer_filled_lots):
                raise ValueError("episode_fields_must_be_empty_without_selected_open")
        else:
            if self.budget is None:
                raise ValueError("episode_requires_budget")
            if len(self.layer_filled_lots) != len(self.budget.layers):
                raise ValueError("layer_filled_lots_must_match_budget_layers")
        if self.pending_layer_index is not None:
            _check_int(self.pending_layer_index, "pending_layer_index")
            if self.pending_layer_index < 0:
                raise ValueError("pending_layer_index_must_be_0_based")
            if self.active_exit_nano is None:
                raise ValueError("pending_add_requires_active_exit_nano")
        _check_optional_nano(self.active_exit_nano, "active_exit_nano")
        _check_optional_nano(self.pending_exit_nano, "pending_exit_nano")
        if (self.active_exit_nano is not None and self.pending_exit_nano is not None
                and self.pending_exit_nano > self.active_exit_nano):
            raise ValueError("pending_exit_must_not_be_later_than_active_exit")
        if self.active_exit_nano is not None:
            if self.budget is None or self.active_exit_nano > self.budget.latest_exit_nano:
                raise ValueError("active_exit_must_not_exceed_budget_latest_exit")
        if self.budget is not None and not isinstance(self.budget, RoundBudget):
            raise TypeError("budget_must_be_RoundBudget_or_None")
        _check_int(self.confirmation_count, "confirmation_count")
        if self.confirmation_count < 0:
            raise ValueError("confirmation_count_must_be_non_negative")
        _check_optional_nano(self.cooldown_until_nano, "cooldown_until_nano")
        _check_optional_str(self.last_event_id, "last_event_id")


@dataclass(frozen=True)
class StrategyContext:
    now_nano: int
    spec: StrategySpec
    exit_binding: ExitPolicyBinding
    features: FeatureSnapshot
    forecasts: tuple[MarketForecast, ...]
    regime: RegimeAssessment
    macro: MacroContext
    bars: tuple[MarketBar, ...]
    sessions: tuple[SessionWindow, ...]
    account: AccountSnapshot
    position: PositionSnapshot | None
    rule: TradingRule | None
    confidence_policy: ConfidencePolicy
    stress_policy: StressPolicy
    run_policy: RunPolicy
    qualification: StrategyQualification | None
    model_hashes: Mapping[str, str]
    holding_deadline_nano: int

    def __post_init__(self) -> None:
        _check_nano(self.now_nano, "now_nano")
        if not isinstance(self.spec, StrategySpec):
            raise TypeError("spec_must_be_StrategySpec")
        if not isinstance(self.exit_binding, ExitPolicyBinding):
            raise TypeError("exit_binding_must_be_ExitPolicyBinding")
        if self.exit_binding.strategy_id != self.spec.strategy_id:
            raise ValueError("exit_binding_strategy_must_match_spec")
        if not isinstance(self.features, FeatureSnapshot):
            raise TypeError("features_must_be_FeatureSnapshot")
        if not isinstance(self.forecasts, tuple) or any(not isinstance(f, MarketForecast) for f in self.forecasts):
            raise TypeError("forecasts_must_be_tuple_of_MarketForecast")
        if not isinstance(self.regime, RegimeAssessment):
            raise TypeError("regime_must_be_RegimeAssessment")
        if not isinstance(self.macro, MacroContext):
            raise TypeError("macro_must_be_MacroContext")
        if not isinstance(self.bars, tuple) or any(not isinstance(bar, MarketBar) for bar in self.bars):
            raise TypeError("bars_must_be_tuple_of_MarketBar")
        if not isinstance(self.sessions, tuple) or any(not isinstance(s, SessionWindow) for s in self.sessions):
            raise TypeError("sessions_must_be_tuple_of_SessionWindow")
        if not isinstance(self.account, AccountSnapshot):
            raise TypeError("account_must_be_AccountSnapshot")
        if self.position is not None and not isinstance(self.position, PositionSnapshot):
            raise TypeError("position_must_be_PositionSnapshot_or_None")
        if self.rule is not None and not isinstance(self.rule, TradingRule):
            raise TypeError("rule_must_be_TradingRule_or_None")
        if not isinstance(self.confidence_policy, ConfidencePolicy):
            raise TypeError("confidence_policy_must_be_ConfidencePolicy")
        if not isinstance(self.stress_policy, StressPolicy):
            raise TypeError("stress_policy_must_be_StressPolicy")
        if not isinstance(self.run_policy, RunPolicy):
            raise TypeError("run_policy_must_be_RunPolicy")
        model_hashes = _freeze_mapping(self.model_hashes, "model_hashes")
        for value in model_hashes.values():
            _check_hash(value, "model_hashes_value")
        object.__setattr__(self, "model_hashes", model_hashes)
        if self.qualification is not None and not isinstance(self.qualification,
                                                             StrategyQualification):
            raise TypeError("qualification_must_be_StrategyQualification_or_None")
        # Qualification/spec version, policy-version and model-hash consistency is
        # enforced by the M4 gate (QUALIFICATION_MISMATCH / QUALIFICATION_EXPIRED,
        # OPEN/ADD weight 0); structurally valid but stale or mismatched
        # qualifications must not block constructing exit contexts for existing
        # positions (REDUCE/EXIT require no qualification).
        if self.confidence_policy.policy_id != self.spec.confidence_policy_id:
            raise ValueError("confidence_policy_id_must_match_spec")
        if any(session.product != self.features.product for session in self.sessions):
            raise ValueError("sessions_must_contain_only_this_product_calendar")
        _check_nano(self.holding_deadline_nano, "holding_deadline_nano")


@dataclass(frozen=True)
class PayoffEstimate:
    estimate_id: str
    proposal_id: str
    model_id: str
    horizon: Horizon
    barrier_key: str
    geometry_key: str
    opportunity_set_id: str
    exit_binding_sha256: str
    exit_state_condition: str
    training_end_nano: int
    sample_count: int
    effective_blocks: int
    expected_net_cny: Decimal | None
    conservative_ev_cny: Decimal | None
    net_profit_probability: float | None
    loss_cvar95_cny: Decimal | None
    peak_margin_cny: Decimal | None
    return_before_stop_probability: float | None
    missing_probability_mass: float
    status: Literal["READY", "INSUFFICIENT", "INVALID"]
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("estimate_id", "proposal_id", "model_id", "barrier_key",
                     "geometry_key", "opportunity_set_id", "exit_state_condition"):
            _check_str(getattr(self, name), name)
        _check_hash(self.exit_binding_sha256, "exit_binding_sha256")
        if not isinstance(self.horizon, Horizon):
            raise TypeError("horizon_must_be_Horizon")
        for name in ("training_end_nano", "sample_count", "effective_blocks"):
            _check_int(getattr(self, name), name)
        if self.sample_count < 0 or self.effective_blocks < 0:
            raise ValueError("sample_count_and_effective_blocks_must_be_non_negative")
        for name in ("expected_net_cny", "conservative_ev_cny", "loss_cvar95_cny", "peak_margin_cny"):
            _check_optional_decimal(getattr(self, name), name)
        if self.peak_margin_cny is not None and self.peak_margin_cny < 0:
            raise ValueError("peak_margin_cny_must_be_non_negative")
        _check_optional_probability(self.net_profit_probability, "net_profit_probability")
        _check_optional_probability(self.return_before_stop_probability, "return_before_stop_probability")
        _check_probability(self.missing_probability_mass, "missing_probability_mass")
        _check_enum(self.status, PAYOFF_STATUSES, "status")
        _check_str_tuple(self.reasons, "reasons")


@dataclass(frozen=True)
class StrategyProposal:
    proposal_id: str
    strategy_id: str
    episode_id: str | None
    contract_id: str
    decision_nano: int
    action: ProposalAction
    target_lots: int
    layer_index: int | None
    trigger_price: Decimal | None
    target_price: Decimal | None
    stop_price: Decimal | None
    earliest_execution_nano: int
    valid_until_nano: int
    forecast_id: str | None
    assessment_id: str
    budget: RoundBudget | None
    payoff: PayoffEstimate | None
    next_state: StrategyState
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        _check_str(self.proposal_id, "proposal_id")
        _check_str(self.strategy_id, "strategy_id")
        _check_optional_str(self.episode_id, "episode_id")
        _check_str(self.contract_id, "contract_id")
        _check_nano(self.decision_nano, "decision_nano")
        _check_enum(self.action, PROPOSAL_ACTIONS, "action")
        # Signed absolute target net lots (01 §4.1: "签名后的绝对目标净手数");
        # negative targets express short goals without any abs() workaround.
        _check_int(self.target_lots, "target_lots")
        if self.layer_index is not None:
            _check_int(self.layer_index, "layer_index")
            if self.layer_index < 0:
                raise ValueError("layer_index_must_be_0_based")
        for name in ("trigger_price", "target_price", "stop_price"):
            value = getattr(self, name)
            _check_optional_decimal(value, name)
            if value is not None and value <= 0:
                raise ValueError(f"{name}_must_be_positive")
        _check_nano(self.earliest_execution_nano, "earliest_execution_nano")
        _check_nano(self.valid_until_nano, "valid_until_nano")
        if self.earliest_execution_nano >= self.valid_until_nano:
            raise ValueError("earliest_execution_must_precede_valid_until")
        _check_optional_str(self.forecast_id, "forecast_id")
        _check_str(self.assessment_id, "assessment_id")
        if self.budget is not None and not isinstance(self.budget, RoundBudget):
            raise TypeError("budget_must_be_RoundBudget_or_None")
        if self.payoff is not None and not isinstance(self.payoff, PayoffEstimate):
            raise TypeError("payoff_must_be_PayoffEstimate_or_None")
        if not isinstance(self.next_state, StrategyState):
            raise TypeError("next_state_must_be_StrategyState")
        _check_str_tuple(self.reasons, "reasons")


@dataclass(frozen=True)
class GateDecision:
    proposal_id: str
    eligible: bool
    reasons: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    checked_at_nano: int

    def __post_init__(self) -> None:
        _check_str(self.proposal_id, "proposal_id")
        _check_bool(self.eligible, "eligible")
        _check_str_tuple(self.reasons, "reasons")
        _check_str_tuple(self.evidence_ids, "evidence_ids")
        _check_nano(self.checked_at_nano, "checked_at_nano")


@dataclass(frozen=True)
class DecisionRecord:
    decision_id: str
    opportunity_set_id: str
    decision_nano: int
    policy_version: str
    run_policy_sha256: str
    purpose: Literal["RESEARCH_EVALUATION", "SHADOW_OPERATIONAL"]
    qualification_ids: tuple[str, ...]
    mode: Literal["BEST_EV", "PROBABILITY_SAMPLE", "FIXED", "CASH"]
    proposals: tuple[StrategyProposal, ...]
    gates: tuple[GateDecision, ...]
    weights: Mapping[str, float]
    seed: int
    random_u: float | None
    selected_proposal_id: str | None
    outcome: Literal["SELECTED", "NO_TRADE", "MANAGE_POSITION"]
    reasons: tuple[str, ...]
    account_before_hash: str

    def __post_init__(self) -> None:
        _check_str(self.decision_id, "decision_id")
        _check_str(self.opportunity_set_id, "opportunity_set_id")
        _check_nano(self.decision_nano, "decision_nano")
        _check_str(self.policy_version, "policy_version")
        _check_hash(self.run_policy_sha256, "run_policy_sha256")
        _check_enum(self.purpose, RUN_PURPOSES, "purpose")
        _check_str_tuple(self.qualification_ids, "qualification_ids")
        _check_enum(self.mode, ROUTING_MODES, "mode")
        if not isinstance(self.proposals, tuple) or any(not isinstance(p, StrategyProposal) for p in self.proposals):
            raise TypeError("proposals_must_be_tuple_of_StrategyProposal")
        if not isinstance(self.gates, tuple) or any(not isinstance(g, GateDecision) for g in self.gates):
            raise TypeError("gates_must_be_tuple_of_GateDecision")
        weights = _freeze_typed_mapping(self.weights, "weights")
        for item in weights.values():
            _check_float(item, "weights_value")
        object.__setattr__(self, "weights", weights)
        _check_int(self.seed, "seed")
        _check_optional_probability(self.random_u, "random_u")
        _check_optional_str(self.selected_proposal_id, "selected_proposal_id")
        _check_enum(self.outcome, DECISION_OUTCOMES, "outcome")
        _check_str_tuple(self.reasons, "reasons")
        _check_hash(self.account_before_hash, "account_before_hash")


@dataclass(frozen=True)
class OrderIntent:
    intent_id: str
    decision_id: str
    strategy_id: str
    episode_id: str
    contract_id: str
    created_at_nano: int
    not_before_nano: int
    expires_at_nano: int
    delta_lots: int
    offset: Literal["OPEN", "CLOSE", "CLOSE_TODAY"]
    purpose: Literal["ENTRY", "ADD", "REDUCE", "STOP", "TARGET", "TIME_EXIT", "ROLL_EXIT"]
    layer_index: int | None
    limit_price: Decimal | None

    def __post_init__(self) -> None:
        for name in ("intent_id", "decision_id", "strategy_id", "episode_id", "contract_id"):
            _check_str(getattr(self, name), name)
        for name in ("created_at_nano", "not_before_nano", "expires_at_nano"):
            _check_nano(getattr(self, name), name)
        if self.created_at_nano > self.not_before_nano or self.not_before_nano >= self.expires_at_nano:
            raise ValueError("created<=not_before<expires_required")
        _check_int(self.delta_lots, "delta_lots")
        if self.delta_lots == 0:
            raise ValueError("delta_lots_must_be_signed_non_zero")
        _check_enum(self.offset, OFFSET_KINDS, "offset")
        _check_enum(self.purpose, INTENT_PURPOSES, "purpose")
        if self.layer_index is not None:
            _check_int(self.layer_index, "layer_index")
            if self.layer_index < 0:
                raise ValueError("layer_index_must_be_0_based")
        _check_optional_decimal(self.limit_price, "limit_price")
        if self.limit_price is not None and self.limit_price <= 0:
            raise ValueError("limit_price_must_be_positive")


@dataclass(frozen=True)
class Fill:
    fill_id: str
    intent_id: str
    contract_id: str
    strategy_id: str
    episode_id: str
    execution_nano: int
    trading_day: date
    side: Literal[-1, 1]
    lots: int
    offset: Literal["OPEN", "CLOSE", "CLOSE_TODAY"]
    reference_price: Decimal
    execution_price: Decimal
    fee_cny: Decimal
    slippage_cny: Decimal
    rule_id: str
    evidence: Literal["VERIFIED", "SCENARIO"]

    def __post_init__(self) -> None:
        for name in ("fill_id", "intent_id", "contract_id", "strategy_id", "episode_id", "rule_id"):
            _check_str(getattr(self, name), name)
        _check_nano(self.execution_nano, "execution_nano")
        _check_date(self.trading_day, "trading_day")
        _check_side(self.side, "side")
        _check_int(self.lots, "lots")
        if self.lots < 1:
            raise ValueError("lots_must_be_positive_integer")
        _check_enum(self.offset, OFFSET_KINDS, "offset")
        _check_decimal(self.reference_price, "reference_price")
        _check_decimal(self.execution_price, "execution_price")
        if self.reference_price <= 0 or self.execution_price <= 0:
            raise ValueError("prices_must_be_positive")
        _check_decimal(self.fee_cny, "fee_cny")
        _check_decimal(self.slippage_cny, "slippage_cny")
        if self.fee_cny < 0 or self.slippage_cny < 0:
            raise ValueError("fee_and_slippage_must_be_non_negative")
        _check_enum(self.evidence, FILL_EVIDENCE_STATUSES, "evidence")


@dataclass(frozen=True)
class ExecutionResult:
    intent_id: str
    fills: tuple[Fill, ...]
    remaining_lots: int
    status: Literal["FILLED", "PARTIAL", "BLOCKED", "EXPIRED", "CANCELLED"]
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        _check_str(self.intent_id, "intent_id")
        if not isinstance(self.fills, tuple) or any(not isinstance(fill, Fill) for fill in self.fills):
            raise TypeError("fills_must_be_tuple_of_Fill")
        # Signed unfinished quantity after partial fills (05 §7.1).
        _check_int(self.remaining_lots, "remaining_lots")
        _check_enum(self.status, EXECUTION_STATUSES, "status")
        _check_str_tuple(self.reasons, "reasons")


@dataclass(frozen=True)
class ValuationMark:
    contract_id: str
    price: Decimal | None
    as_of_nano: int | None
    valued_at_nano: int | None
    source_bar_id: str | None
    source_bar_sha256: str | None
    rule_id: str | None
    status: Literal["CURRENT", "STALE", "UNVALUED"]
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        _check_str(self.contract_id, "contract_id")
        _check_enum(self.status, VALUATION_STATUSES, "status")
        _check_str_tuple(self.reasons, "reasons")
        facts = (self.price, self.as_of_nano, self.valued_at_nano, self.source_bar_id,
                 self.source_bar_sha256, self.rule_id)
        if self.status in ("CURRENT", "STALE"):
            if any(value is None for value in facts):
                raise ValueError("current_stale_marks_require_all_fact_fields")
            _check_decimal(self.price, "price")
            if self.price is not None and self.price <= 0:
                raise ValueError("price_must_be_positive")
            _check_nano(self.as_of_nano, "as_of_nano")
            _check_nano(self.valued_at_nano, "valued_at_nano")
            if self.as_of_nano > self.valued_at_nano:
                raise ValueError("as_of_must_not_exceed_valued_at")
            _check_str(self.source_bar_id, "source_bar_id")
            _check_hash(self.source_bar_sha256, "source_bar_sha256")
            _check_str(self.rule_id, "rule_id")
            if self.status == "CURRENT":
                if self.reasons:
                    raise ValueError("current_mark_requires_empty_reasons")
            else:
                if not self.reasons:
                    raise ValueError("stale_mark_requires_non_empty_reasons")
                if not set(self.reasons) <= VALUATION_STALE_REASONS:
                    raise ValueError("stale_reasons_subset_violated")
                if tuple(sorted(set(self.reasons))) != self.reasons:
                    raise ValueError("stale_reasons_must_be_sorted_deduplicated")
        else:
            if any(value is not None for value in facts):
                raise ValueError("unvalued_mark_requires_all_fact_fields_none")
            if not self.reasons:
                raise ValueError("unvalued_mark_requires_reasons")
            if not set(self.reasons) <= VALUATION_UNVALUED_REASONS:
                raise ValueError("unvalued_reasons_subset_violated")
            if tuple(sorted(set(self.reasons))) != self.reasons:
                raise ValueError("unvalued_reasons_must_be_sorted_deduplicated")


# ─── 08-M3C §4.1/§4.2, 007 §5.1, OQ-1/3/4/6 rulings: public exit types ───


def _exit_decimal_of(value: object, name: str) -> Decimal:
    if not isinstance(value, str):
        raise ValueError(f"{name}_must_be_decimal_string")
    try:
        number = Decimal(value)
    except Exception:
        raise ValueError(f"{name}_must_be_decimal_string") from None
    if not number.is_finite():
        raise ValueError(f"{name}_must_be_finite")
    return number


def _exit_decimal_list(value: object, name: str) -> tuple[Decimal, ...]:
    # JSON lists arrive as lists; the defensive freeze stores tuples.
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(f"{name}_must_be_non_empty_list")
    return tuple(_exit_decimal_of(item, f"{name}_item") for item in value)


def _check_exit_parameters(family: str, parameters: Mapping[str, object]) -> None:
    """08 §5.4 exact whitelist plus per-family value constraints."""
    expected = EXIT_PARAMETER_WHITELISTS[family]
    if set(parameters) != set(expected):
        raise ValueError("exit_parameters_keys_must_exactly_match_family_whitelist")
    for key, item in parameters.items():
        if isinstance(item, bool) or not isinstance(item, (str, int, list, tuple)):
            raise ValueError(f"exit_parameter_{key}_invalid_json_value_type")
        if isinstance(item, (list, tuple)) and any(
                not isinstance(entry, str) for entry in item):
            raise ValueError(f"exit_parameter_{key}_items_must_be_str")
        if isinstance(item, int) and not _is_int(item):
            raise ValueError(f"exit_parameter_{key}_must_not_be_bool")

    def positive(key: str) -> Decimal:
        number = _exit_decimal_of(parameters[key], f"exit_parameter_{key}")
        if number <= 0:
            raise ValueError(f"exit_parameter_{key}_must_be_positive")
        return number

    def confirmation_bars(key: str = "confirmation_bars") -> None:
        value = parameters[key]
        if not _is_int(value) or value < 1:
            raise ValueError(f"exit_parameter_{key}_must_be_int_at_least_1")

    def enum_value(key: str, allowed: frozenset[str]) -> None:
        if parameters[key] not in allowed:
            raise ValueError(f"exit_parameter_{key}_enum_violated")

    def fraction_list(key: str, *, last_must_be_one: bool) -> tuple[Decimal, ...]:
        values = _exit_decimal_list(parameters[key], f"exit_parameter_{key}")
        for number in values:
            if number <= 0 or number > 1:
                raise ValueError(f"exit_parameter_{key}_must_be_within_0_1")
        if any(b <= a for a, b in zip(values, values[1:])):
            raise ValueError(f"exit_parameter_{key}_must_be_strictly_increasing")
        if last_must_be_one and values[-1] != 1:
            raise ValueError(f"exit_parameter_{key}_last_must_be_one")
        return values

    if family == "LEGACY":
        return
    if family == "PROFIT_LADDER":
        levels = _exit_decimal_list(parameters["profit_levels_r"],
                                    "exit_parameter_profit_levels_r")
        if any(b <= a for a, b in zip(levels, levels[1:])):
            raise ValueError("exit_parameter_profit_levels_r_must_be_strictly_increasing")
        if any(number <= 0 for number in levels):
            raise ValueError("exit_parameter_profit_levels_r_must_be_positive")
        fractions = fraction_list("cumulative_close_fractions", last_must_be_one=True)
        if len(levels) != len(fractions):
            raise ValueError("exit_parameter_ladder_arrays_must_have_equal_length")
        confirmation_bars()
    elif family == "TRAILING_STOP":
        positive("arm_profit_r")
        positive("trail_atr_multiple")
        enum_value("atr_basis", frozenset({"FROZEN_ATR"}))
        enum_value("peak_source", frozenset({"COMPLETED_CLOSE"}))
    elif family == "VOLATILITY_STOP":
        enum_value("atr_basis", frozenset({"BASE_ATR14"}))
        positive("atr_multiple")
        confirmation_bars()
    elif family == "TIME_DECAY":
        times = fraction_list("soft_time_fractions", last_must_be_one=False)
        progress = _exit_decimal_list(parameters["minimum_progress_r"],
                                      "exit_parameter_minimum_progress_r")
        if any(number <= 0 for number in progress):
            raise ValueError("exit_parameter_minimum_progress_r_must_be_positive")
        fractions = fraction_list("cumulative_close_fractions", last_must_be_one=True)
        if not (len(times) == len(progress) == len(fractions)):
            raise ValueError("exit_parameter_time_decay_arrays_must_have_equal_length")
    elif family == "SIGNAL_REVERSAL":
        confirmation_bars()
    elif family == "DRAWDOWN_EXIT":
        positive("arm_peak_profit_r")
        positive("max_giveback_r")
    elif family == "OPPORTUNITY_COST":
        improvement = _exit_decimal_of(parameters["minimum_improvement_r"],
                                       "exit_parameter_minimum_improvement_r")
        if improvement < 0:
            raise ValueError("exit_parameter_minimum_improvement_r_must_not_be_negative")
        confirmation_bars()
    elif family == "PARTIAL_LIQUIDATION":
        enum_value("sizing_mode", frozenset({"CUMULATIVE_ORIGINAL_LOTS"}))
        enum_value("lot_rounding", frozenset({"FLOOR_CLOSE"}))
        enum_value("final_step", frozenset({"ALL_REMAINDER"}))


def _check_exit_state_key(key: object, name: str, duration_seconds: int | None = None) -> None:
    if not isinstance(key, str):
        raise ValueError(f"{name}_must_be_str")
    if key in EXITCTX_RESERVED_KEYS or key in EXITMKT_RESERVED_KEYS:
        raise ValueError(f"{name}_reserved_namespace_requires_formal_change")
    if key not in EXIT_STATE_KEYS:
        raise ValueError(f"{name}_must_be_registered_EXITSTATE_key")
    if duration_seconds is not None and not key.endswith(f":d{duration_seconds}"):
        raise ValueError(f"{name}_duration_suffix_mismatch")


def exit_observation_slot_id(contract_id: str, duration_seconds: int,
                             observed_at_nano: int) -> str:
    """OQ-6 canonical observation slot ID: compact JSON array
    [contract_id, duration_seconds, observed_at_nano]; no trailing LF."""
    return json.dumps([contract_id, duration_seconds, observed_at_nano],
                      ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _validate_exit_observation_slot_id(text: object, parent_contract_id: str) -> None:
    if not isinstance(text, str):
        raise ValueError("last_observation_id_must_be_str_or_none")
    try:
        parsed = json.loads(text)
    except Exception:
        raise ValueError("last_observation_id_must_be_canonical_json_array") from None
    if not (isinstance(parsed, list) and len(parsed) == 3):
        raise ValueError("last_observation_id_must_be_three_element_array")
    contract_id, duration, observed = parsed
    if not isinstance(contract_id, str) or not contract_id:
        raise ValueError("last_observation_id_contract_must_be_non_empty_str")
    if not _is_int(duration) or duration not in (60, 86400):
        raise ValueError("last_observation_id_duration_must_be_60_or_86400")
    if not _is_int(observed) or observed <= 0:
        raise ValueError("last_observation_id_observed_at_must_be_positive_int")
    if contract_id != parent_contract_id:
        raise ValueError("last_observation_id_contract_must_match_parent_state")
    canonical = exit_observation_slot_id(contract_id, duration, observed)
    if text != canonical:
        raise ValueError("last_observation_id_must_be_canonical_compact_form")


def exit_binding_content_hash(strategy_id: str, entry_spec_sha256: str,
                              exit_spec_sha256s: tuple[str, ...],
                              sizing_spec_sha256: str | None,
                              priority: tuple[str, ...],
                              legacy_rules_version: str) -> str:
    """OQ-4: the single authoritative binding identity formula.

    sha256 over the canonical JSON projection of exactly six identity
    fields (sorted keys, compact separators, UTF-8, ensure_ascii=False,
    exactly one trailing LF). Uses only the standard library; the byte
    contract equals schemas.canonical_json_bytes. ``exit_spec_sha256s`` is
    sorted for hashing (order-insensitive identity) but duplicates are
    rejected; ``priority`` keeps its semantic order and rejects duplicates.
    """
    if not isinstance(strategy_id, str) or not strategy_id:
        raise ValueError("strategy_id_must_be_non_empty_str")
    if not isinstance(legacy_rules_version, str) or not legacy_rules_version:
        raise ValueError("legacy_rules_version_must_be_non_empty_str")
    _check_hash(entry_spec_sha256, "entry_spec_sha256")
    if not isinstance(exit_spec_sha256s, tuple) or not exit_spec_sha256s:
        raise ValueError("exit_spec_sha256s_must_be_non_empty_tuple")
    for item in exit_spec_sha256s:
        _check_hash(item, "exit_spec_sha256s_item")
    if len(set(exit_spec_sha256s)) != len(exit_spec_sha256s):
        raise ValueError("exit_spec_sha256s_must_not_contain_duplicates")
    if sizing_spec_sha256 is not None:
        _check_hash(sizing_spec_sha256, "sizing_spec_sha256")
    if not isinstance(priority, tuple) or not priority:
        raise ValueError("priority_must_be_non_empty_tuple")
    for item in priority:
        if not isinstance(item, str) or not item:
            raise ValueError("priority_items_must_be_non_empty_str")
    if len(set(priority)) != len(priority):
        raise ValueError("priority_must_not_contain_duplicates")
    payload = {
        "strategy_id": strategy_id,
        "entry_spec_sha256": entry_spec_sha256,
        "exit_spec_sha256s": list(sorted(exit_spec_sha256s)),
        "sizing_spec_sha256": sizing_spec_sha256,
        "priority": list(priority),
        "legacy_rules_version": legacy_rules_version,
    }
    canonical = (
        json.dumps(payload, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=False, allow_nan=False) + "\n"
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True)
class ContinuationEvidence:
    """OQ-1: opaque P4-reserved continuation reference. Carries no economic
    meaning; a non-None ExitContext.continuation is rejected before P4."""
    evidence_sha256: str
    version: Literal["1.0.0"]
    kind: Literal["P4_RESERVED"]

    def __post_init__(self) -> None:
        _check_hash(self.evidence_sha256, "evidence_sha256")
        if self.version != "1.0.0":
            raise ValueError("continuation_version_must_be_1.0.0")
        if self.kind != "P4_RESERVED":
            raise ValueError("continuation_kind_must_be_P4_RESERVED")


@dataclass(frozen=True)
class ExitSpec:
    exit_policy_id: str
    family: ExitFamily
    mode: ExitMode
    version: str
    parameter_version: str
    code_sha256: str
    parameters: Mapping[str, JsonValue]
    required_market_keys: tuple[str, ...]
    required_state_keys: tuple[str, ...]
    applicable_entry_families: tuple[StrategyFamily, ...]
    observation_duration_seconds: int
    execution_timing: ExitTiming
    validation_stage: Literal["RESEARCH", "VALIDATED", "SHADOW"]

    def __post_init__(self) -> None:
        for name in ("exit_policy_id", "version", "parameter_version"):
            _check_str(getattr(self, name), name)
        _check_enum(self.family, EXIT_FAMILIES, "family")
        _check_enum(self.mode, EXIT_MODES, "mode")
        _check_hash(self.code_sha256, "code_sha256")
        duration = EXIT_MODE_DURATIONS[self.mode]
        _check_int(self.observation_duration_seconds, "observation_duration_seconds")
        if self.observation_duration_seconds != duration:
            raise ValueError("observation_duration_seconds_must_match_mode")
        _check_enum(self.execution_timing, EXIT_TIMINGS, "execution_timing")
        _check_enum(self.validation_stage, VALIDATION_STAGES, "validation_stage")
        parameters = _freeze_mapping(self.parameters, "parameters")
        _check_exit_parameters(self.family, parameters)
        object.__setattr__(self, "parameters", parameters)
        if not isinstance(self.required_market_keys, tuple):
            raise TypeError("required_market_keys_must_be_tuple")
        if list(self.required_market_keys) != sorted(set(self.required_market_keys)):
            raise ValueError("required_market_keys_must_be_sorted_unique")
        for key in self.required_market_keys:
            _check_str(key, "required_market_keys_item")
            if not (key.endswith(f":d{duration}")
                    or (duration == 60 and key.endswith("-instant"))):
                raise ValueError("required_market_keys_duration_suffix_mismatch")
        if not isinstance(self.required_state_keys, tuple):
            raise TypeError("required_state_keys_must_be_tuple")
        if list(self.required_state_keys) != sorted(set(self.required_state_keys)):
            raise ValueError("required_state_keys_must_be_sorted_unique")
        for key in self.required_state_keys:
            _check_exit_state_key(key, "required_state_keys_item", duration)
        if not isinstance(self.applicable_entry_families, tuple) \
                or not self.applicable_entry_families:
            raise ValueError("applicable_entry_families_must_be_non_empty_tuple")
        for family in self.applicable_entry_families:
            _check_enum(family, STRATEGY_FAMILIES, "applicable_entry_families_item")
            if family == "WAIT":
                raise ValueError("applicable_entry_families_must_not_contain_WAIT")
        if len(set(self.applicable_entry_families)) != len(self.applicable_entry_families):
            raise ValueError("applicable_entry_families_must_not_contain_duplicates")


@dataclass(frozen=True)
class ExitFeatureSpec:
    feature_key: str
    formula_version: str
    unit: str
    window_kind: ExitWindowKind
    minimum_source_facts: int
    source_scope: ExitSourceScope
    clock_unit: ExitClockUnit
    required_source_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        _check_exit_state_key(self.feature_key, "feature_key")
        _check_str(self.formula_version, "formula_version")
        if self.formula_version != EXIT_FEATURE_FORMULA_VERSION:
            raise ValueError("formula_version_must_be_v1.0.0")
        if f":{EXIT_FEATURE_FORMULA_VERSION}:" not in self.feature_key:
            raise ValueError("feature_key_version_segment_mismatch")
        _check_str(self.unit, "unit")
        _check_enum(self.window_kind, EXIT_WINDOW_KINDS, "window_kind")
        _check_int(self.minimum_source_facts, "minimum_source_facts")
        if self.minimum_source_facts < 1:
            raise ValueError("minimum_source_facts_must_be_at_least_1")
        _check_enum(self.source_scope, EXIT_SOURCE_SCOPES, "source_scope")
        _check_enum(self.clock_unit, EXIT_CLOCK_UNITS, "clock_unit")
        _check_str_tuple(self.required_source_keys, "required_source_keys")
        if list(self.required_source_keys) != sorted(set(self.required_source_keys)):
            raise ValueError("required_source_keys_must_be_sorted_unique")


@dataclass(frozen=True)
class ExitFeatureValue:
    feature_key: str
    value: Decimal | int | None
    observed_at_nano: int | None
    available_at_nano: int | None
    quality: DataQuality
    source_hashes: Mapping[str, str]

    def __post_init__(self) -> None:
        _check_exit_state_key(self.feature_key, "feature_key")
        if isinstance(self.value, Decimal):
            if not self.value.is_finite():
                raise ValueError("value_must_be_finite_decimal")
        elif self.value is not None and not _is_int(self.value):
            raise ValueError("value_must_be_decimal_int_or_none")
        _check_optional_nano(self.observed_at_nano, "observed_at_nano")
        _check_optional_nano(self.available_at_nano, "available_at_nano")
        if not isinstance(self.quality, DataQuality):
            raise TypeError("quality_must_be_DataQuality")
        if self.value is None and not self.quality.reasons:
            raise ValueError("missing_value_requires_quality_reasons")
        hashes = _freeze_mapping(self.source_hashes, "source_hashes")
        for item in hashes.values():
            _check_hash(item, "source_hashes_value")
        object.__setattr__(self, "source_hashes", hashes)


@dataclass(frozen=True)
class ExitFeatureSnapshot:
    snapshot_id: str
    account_id: str
    contract_id: str
    strategy_id: str
    episode_id: str
    exit_binding_sha256: str
    duration_seconds: int
    observed_at_nano: int
    available_at_nano: int
    values: tuple[ExitFeatureValue, ...]
    source_state_sha256: str

    def __post_init__(self) -> None:
        for name in ("snapshot_id", "account_id", "contract_id", "strategy_id",
                     "episode_id"):
            _check_str(getattr(self, name), name)
        _check_hash(self.exit_binding_sha256, "exit_binding_sha256")
        _check_hash(self.source_state_sha256, "source_state_sha256")
        if self.duration_seconds not in (60, 86400):
            raise ValueError("duration_seconds_must_be_60_or_86400")
        _check_nano(self.observed_at_nano, "observed_at_nano")
        _check_nano(self.available_at_nano, "available_at_nano")
        if self.available_at_nano < self.observed_at_nano:
            raise ValueError("available_at_must_not_precede_observed_at")
        if not isinstance(self.values, tuple):
            raise TypeError("values_must_be_tuple_of_ExitFeatureValue")
        keys = [value.feature_key for value in self.values]
        if keys != sorted(set(keys)):
            raise ValueError("values_must_be_sorted_unique_by_feature_key")
        for value in self.values:
            if not isinstance(value, ExitFeatureValue):
                raise TypeError("values_must_be_tuple_of_ExitFeatureValue")
            _check_exit_state_key(value.feature_key, "values_key", self.duration_seconds)
            if value.available_at_nano is not None \
                    and value.available_at_nano > self.available_at_nano:
                raise ValueError("value_available_at_must_not_exceed_snapshot")


@dataclass(frozen=True)
class ExitPolicyBinding:
    binding_sha256: str
    strategy_id: str
    entry_spec_sha256: str
    exit_spec_sha256s: tuple[str, ...]
    sizing_spec_sha256: str | None
    priority: tuple[str, ...]
    legacy_rules_version: str

    def __post_init__(self) -> None:
        _check_str(self.strategy_id, "strategy_id")
        _check_str(self.legacy_rules_version, "legacy_rules_version")
        _check_hash(self.entry_spec_sha256, "entry_spec_sha256")
        if not isinstance(self.exit_spec_sha256s, tuple) or not self.exit_spec_sha256s:
            raise ValueError("exit_spec_sha256s_must_be_non_empty_tuple")
        for item in self.exit_spec_sha256s:
            _check_hash(item, "exit_spec_sha256s_item")
        if len(set(self.exit_spec_sha256s)) != len(self.exit_spec_sha256s):
            raise ValueError("exit_spec_sha256s_must_not_contain_duplicates")
        if list(self.exit_spec_sha256s) != sorted(self.exit_spec_sha256s):
            raise ValueError("exit_spec_sha256s_must_be_stored_sorted")
        if self.sizing_spec_sha256 is not None:
            _check_hash(self.sizing_spec_sha256, "sizing_spec_sha256")
        if not isinstance(self.priority, tuple) or not self.priority:
            raise ValueError("priority_must_be_non_empty_tuple")
        for item in self.priority:
            _check_str(item, "priority_item")
        if len(set(self.priority)) != len(self.priority):
            raise ValueError("priority_must_not_contain_duplicates")
        expected = exit_binding_content_hash(
            self.strategy_id, self.entry_spec_sha256, self.exit_spec_sha256s,
            self.sizing_spec_sha256, self.priority, self.legacy_rules_version)
        _check_hash(self.binding_sha256, "binding_sha256")
        if self.binding_sha256 != expected:
            raise ValueError("binding_sha256_must_equal_content_hash")


@dataclass(frozen=True)
class ExitConfirmation:
    """OQ-6: one soft-confirmation counter per (exit_policy_id, rule_id)."""
    exit_policy_id: str
    rule_id: str
    count: int
    last_observation_id: str | None

    def __post_init__(self) -> None:
        _check_str(self.exit_policy_id, "exit_policy_id")
        _check_str(self.rule_id, "rule_id")
        _check_int(self.count, "count")
        if self.count < 0:
            raise ValueError("count_must_be_non_negative")
        _check_optional_str(self.last_observation_id, "last_observation_id")
        if self.count > 0 and self.last_observation_id is None:
            raise ValueError("positive_count_requires_last_observation_id")


@dataclass(frozen=True)
class ExitProtectionRecord:
    policy_id: str
    stop_price: Decimal
    line_version: str
    effective_from_nano: int

    def __post_init__(self) -> None:
        _check_str(self.policy_id, "policy_id")
        _check_decimal(self.stop_price, "stop_price")
        if self.stop_price <= 0:
            raise ValueError("stop_price_must_be_positive")
        _check_str(self.line_version, "line_version")
        _check_nano(self.effective_from_nano, "effective_from_nano")


@dataclass(frozen=True)
class ExitLadderStep:
    level_index: int
    triggered: bool
    requested_lots: int
    completed_lots: int

    def __post_init__(self) -> None:
        _check_int(self.level_index, "level_index")
        if self.level_index < 0:
            raise ValueError("level_index_must_be_non_negative")
        _check_bool(self.triggered, "triggered")
        _check_int(self.requested_lots, "requested_lots")
        _check_int(self.completed_lots, "completed_lots")
        if self.requested_lots < 0 or self.completed_lots < 0:
            raise ValueError("ladder_lots_must_be_non_negative")
        if self.completed_lots > self.requested_lots:
            raise ValueError("completed_lots_must_not_exceed_requested_lots")


@dataclass(frozen=True)
class ExitState:
    account_id: str
    contract_id: str
    strategy_id: str
    episode_id: str
    first_fill_nano: int
    exit_binding_sha256: str
    realized_gross_cny: Decimal
    fees_paid_cny: Decimal
    price_peak: Decimal | None
    price_peak_source_nano: int | None
    price_peak_source_hash: str | None
    net_peak_r: Decimal | None
    net_peak_source_nano: int | None
    net_peak_source_hash: str | None
    armed: bool
    protection_stop_price: Decimal | None
    protection_line_version: str | None
    protection_effective_from_nano: int | None
    confirmations: tuple[ExitConfirmation, ...]
    no_more_add: bool
    qbase: int | None
    closed_lots: int
    ladder_progress: tuple[ExitLadderStep, ...]
    phase: ExitPhase
    last_committed_event_id: str | None
    state_prefix_hash: str

    def __post_init__(self) -> None:
        for name in ("account_id", "contract_id", "strategy_id", "episode_id"):
            _check_str(getattr(self, name), name)
        _check_nano(self.first_fill_nano, "first_fill_nano")
        _check_hash(self.exit_binding_sha256, "exit_binding_sha256")
        _check_decimal(self.realized_gross_cny, "realized_gross_cny")
        _check_decimal(self.fees_paid_cny, "fees_paid_cny")
        if self.price_peak is None:
            if any(value is not None for value in
                   (self.price_peak_source_nano, self.price_peak_source_hash)):
                raise ValueError("price_peak_none_requires_none_source_fields")
        else:
            _check_decimal(self.price_peak, "price_peak")
            _check_nano(self.price_peak_source_nano, "price_peak_source_nano")
            if self.price_peak_source_nano < self.first_fill_nano:
                raise ValueError("price_peak_source_must_not_precede_first_fill")
            _check_hash(self.price_peak_source_hash, "price_peak_source_hash")
        if self.net_peak_r is None:
            if any(value is not None for value in
                   (self.net_peak_source_nano, self.net_peak_source_hash)):
                raise ValueError("net_peak_none_requires_none_source_fields")
        else:
            _check_decimal(self.net_peak_r, "net_peak_r")
            if self.net_peak_r < 0:
                raise ValueError("net_peak_r_must_be_non_negative")
            _check_nano(self.net_peak_source_nano, "net_peak_source_nano")
            if self.net_peak_source_nano < self.first_fill_nano:
                raise ValueError("net_peak_source_must_not_precede_first_fill")
            _check_hash(self.net_peak_source_hash, "net_peak_source_hash")
        _check_bool(self.armed, "armed")
        protection = (self.protection_stop_price, self.protection_line_version,
                      self.protection_effective_from_nano)
        if self.armed:
            if any(value is None for value in protection):
                raise ValueError("armed_requires_protection_fields")
            _check_decimal(self.protection_stop_price, "protection_stop_price")
            if self.protection_stop_price <= 0:
                raise ValueError("protection_stop_price_must_be_positive")
            _check_str(self.protection_line_version, "protection_line_version")
            _check_nano(self.protection_effective_from_nano, "protection_effective_from_nano")
        else:
            if any(value is not None for value in protection):
                raise ValueError("unarmed_requires_protection_fields_none")
        if not isinstance(self.confirmations, tuple) \
                or any(not isinstance(item, ExitConfirmation) for item in self.confirmations):
            raise TypeError("confirmations_must_be_tuple_of_ExitConfirmation")
        keys = [(item.exit_policy_id, item.rule_id) for item in self.confirmations]
        if keys != sorted(set(keys)):
            raise ValueError("confirmations_must_be_sorted_unique_by_policy_and_rule")
        for item in self.confirmations:
            if item.last_observation_id is not None:
                _validate_exit_observation_slot_id(item.last_observation_id,
                                                   self.contract_id)
        _check_bool(self.no_more_add, "no_more_add")
        if self.qbase is None:
            if self.closed_lots != 0 or self.ladder_progress:
                raise ValueError("qbase_none_requires_zero_closed_and_no_ladder")
        else:
            _check_int(self.qbase, "qbase")
            if self.qbase < 1:
                raise ValueError("qbase_must_be_at_least_1")
        _check_int(self.closed_lots, "closed_lots")
        if self.closed_lots < 0:
            raise ValueError("closed_lots_must_be_non_negative")
        if self.qbase is not None and self.closed_lots > self.qbase:
            raise ValueError("closed_lots_must_not_exceed_qbase")
        if not isinstance(self.ladder_progress, tuple) \
                or any(not isinstance(item, ExitLadderStep) for item in self.ladder_progress):
            raise TypeError("ladder_progress_must_be_tuple_of_ExitLadderStep")
        indices = [item.level_index for item in self.ladder_progress]
        if indices != sorted(set(indices)):
            raise ValueError("ladder_progress_must_be_sorted_unique_by_level_index")
        _check_enum(self.phase, EXIT_PHASES, "phase")
        if self.phase == "CLOSED" and self.qbase is None:
            raise ValueError("closed_phase_requires_frozen_qbase")
        _check_optional_str(self.last_committed_event_id, "last_committed_event_id")
        _check_hash(self.state_prefix_hash, "state_prefix_hash")


@dataclass(frozen=True)
class ExitProposal:
    """OQ-3: fully embedded candidate next_state (observation candidate only;
    never a substitute for a selected/committed/filled state)."""
    proposal_id: str
    strategy_id: str
    episode_id: str
    contract_id: str
    exit_binding_sha256: str
    action: ExitAction
    target_lots: int
    triggering_policy_id: str | None
    input_sha256: str
    observed_at_nano: int
    earliest_execution_nano: int
    protection: ExitProtectionRecord | None
    next_state: ExitState
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("proposal_id", "strategy_id", "episode_id", "contract_id"):
            _check_str(getattr(self, name), name)
        _check_hash(self.exit_binding_sha256, "exit_binding_sha256")
        _check_hash(self.input_sha256, "input_sha256")
        _check_enum(self.action, EXIT_ACTIONS, "action")
        _check_int(self.target_lots, "target_lots")
        if self.action == "EXIT" and self.target_lots != 0:
            raise ValueError("exit_action_requires_zero_target_lots")
        if self.action == "REDUCE" and self.target_lots == 0:
            raise ValueError("reduce_action_requires_non_zero_target_lots")
        _check_optional_str(self.triggering_policy_id, "triggering_policy_id")
        _check_nano(self.observed_at_nano, "observed_at_nano")
        _check_nano(self.earliest_execution_nano, "earliest_execution_nano")
        if self.earliest_execution_nano < self.observed_at_nano:
            raise ValueError("earliest_execution_must_not_precede_observation")
        if self.protection is not None \
                and not isinstance(self.protection, ExitProtectionRecord):
            raise TypeError("protection_must_be_ExitProtectionRecord_or_none")
        if not isinstance(self.next_state, ExitState):
            raise TypeError("next_state_must_be_ExitState")
        if (self.next_state.strategy_id != self.strategy_id
                or self.next_state.episode_id != self.episode_id
                or self.next_state.contract_id != self.contract_id
                or self.next_state.exit_binding_sha256 != self.exit_binding_sha256):
            raise ValueError("next_state_scope_must_match_proposal")
        _check_str_tuple(self.reasons, "reasons")


@dataclass(frozen=True)
class ExitContext:
    now_nano: int
    account_id: str
    contract_id: str
    strategy_id: str
    episode_id: str
    exit_binding: ExitPolicyBinding
    exit_specs: tuple[ExitSpec, ...]
    market_features: tuple[ExitFeatureSnapshot, ...]
    forecasts: tuple[MarketForecast, ...]
    entry_spec: StrategySpec
    entry_state: StrategyState
    budget: RoundBudget
    account: AccountSnapshot
    lots: tuple[PositionLot, ...]
    marks: tuple[ValuationMark, ...]
    episode_fills: tuple[Fill, ...]
    rules: tuple[TradingRule, ...]
    sessions: tuple[SessionWindow, ...]
    previous_exit_state: ExitState | None
    continuation: ContinuationEvidence | None

    def __post_init__(self) -> None:
        _check_nano(self.now_nano, "now_nano")
        for name in ("account_id", "contract_id", "strategy_id", "episode_id"):
            _check_str(getattr(self, name), name)
        if not isinstance(self.exit_binding, ExitPolicyBinding):
            raise TypeError("exit_binding_must_be_ExitPolicyBinding")
        if self.exit_binding.strategy_id != self.strategy_id:
            raise ValueError("exit_binding_strategy_must_match_context")
        if not isinstance(self.exit_specs, tuple) or not self.exit_specs \
                or any(not isinstance(item, ExitSpec) for item in self.exit_specs):
            raise TypeError("exit_specs_must_be_non_empty_tuple_of_ExitSpec")
        for collection, kind in (
                (self.market_features, ExitFeatureSnapshot),
                (self.forecasts, MarketForecast),
                (self.lots, PositionLot),
                (self.marks, ValuationMark),
                (self.episode_fills, Fill),
                (self.rules, TradingRule),
                (self.sessions, SessionWindow)):
            if not isinstance(collection, tuple) \
                    or any(not isinstance(item, kind) for item in collection):
                raise TypeError(f"{kind.__name__.lower()}s_must_be_tuple_of_{kind.__name__}")
        if not isinstance(self.entry_spec, StrategySpec):
            raise TypeError("entry_spec_must_be_StrategySpec")
        if not isinstance(self.entry_state, StrategyState):
            raise TypeError("entry_state_must_be_StrategyState")
        if not isinstance(self.budget, RoundBudget):
            raise TypeError("budget_must_be_RoundBudget")
        if not isinstance(self.account, AccountSnapshot):
            raise TypeError("account_must_be_AccountSnapshot")
        if self.previous_exit_state is not None:
            if not isinstance(self.previous_exit_state, ExitState):
                raise TypeError("previous_exit_state_must_be_ExitState_or_none")
            if (self.previous_exit_state.account_id != self.account_id
                    or self.previous_exit_state.contract_id != self.contract_id
                    or self.previous_exit_state.strategy_id != self.strategy_id
                    or self.previous_exit_state.episode_id != self.episode_id):
                raise ValueError("previous_exit_state_scope_must_match_context")
        # OQ-1: P0—P3 contexts must pass continuation=None explicitly.
        if self.continuation is not None:
            raise ValueError("exit_context_continuation_must_be_none_before_p4")


@dataclass(frozen=True)
class ExitExecutionLink:
    link_id: str
    exit_proposal_id: str | None
    strategy_proposal_id: str
    decision_id: str
    intent_id: str
    exit_binding_sha256: str
    action: ExitAction
    target_lots: int
    requested_lots: int
    execution_kind: ExitExecutionKind

    def __post_init__(self) -> None:
        for name in ("link_id", "strategy_proposal_id", "decision_id", "intent_id"):
            _check_str(getattr(self, name), name)
        _check_optional_str(self.exit_proposal_id, "exit_proposal_id")
        _check_hash(self.exit_binding_sha256, "exit_binding_sha256")
        _check_enum(self.action, EXIT_ACTIONS, "action")
        if self.action not in ("REDUCE", "EXIT"):
            raise ValueError("execution_link_action_must_be_reduce_or_exit")
        _check_int(self.target_lots, "target_lots")
        if self.action == "EXIT" and self.target_lots != 0:
            raise ValueError("exit_action_requires_zero_target_lots")
        if self.action == "REDUCE" and self.target_lots == 0:
            raise ValueError("reduce_action_requires_non_zero_target_lots")
        _check_int(self.requested_lots, "requested_lots")
        if self.requested_lots < 1:
            raise ValueError("requested_lots_must_be_at_least_1")
        _check_enum(self.execution_kind, EXIT_EXECUTION_KINDS, "execution_kind")


@dataclass(frozen=True)
class LedgerCheckpoint:
    sequence: int
    contract_lock_sha256: str
    registry_sha256: str
    exit_registry_sha256: str
    run_policy: RunPolicy
    rule_hashes: Mapping[str, str]
    model_hashes: Mapping[str, str]
    qualification_hashes: Mapping[str, str]
    account: AccountSnapshot
    valuation_marks: tuple[ValuationMark, ...]
    states: tuple[StrategyState, ...]
    exit_bindings: tuple[ExitPolicyBinding, ...]
    exit_states: tuple[ExitState, ...]
    exit_execution_links: tuple[ExitExecutionLink, ...]
    pending_intents: tuple[OrderIntent, ...]
    pending_remaining_lots: Mapping[str, int]
    processed_event_ids: tuple[str, ...]
    processed_fill_hashes: Mapping[str, str]
    capacity_used: Mapping[str, int]
    previous_hash: str
    checkpoint_hash: str

    def __post_init__(self) -> None:
        _check_int(self.sequence, "sequence")
        if self.sequence < 0:
            raise ValueError("sequence_must_be_non_negative")
        _check_hash(self.contract_lock_sha256, "contract_lock_sha256")
        _check_hash(self.registry_sha256, "registry_sha256")
        _check_hash(self.exit_registry_sha256, "exit_registry_sha256")
        if not isinstance(self.run_policy, RunPolicy):
            raise TypeError("run_policy_must_be_RunPolicy")
        for name in ("rule_hashes", "model_hashes", "qualification_hashes"):
            mapping = _freeze_mapping(getattr(self, name), name)
            for item in mapping.values():
                _check_hash(item, f"{name}_value")
            object.__setattr__(self, name, mapping)
        model_keys = set(self.model_hashes)
        if not set(QUALIFICATION_MODEL_HASH_KEYS) <= model_keys \
                or not model_keys <= {*QUALIFICATION_MODEL_HASH_KEYS, "seasonality"}:
            raise ValueError("checkpoint_model_hash_keys_must_be_complete_fixed_set")
        if not isinstance(self.account, AccountSnapshot):
            raise TypeError("account_must_be_AccountSnapshot")
        if not isinstance(self.states, tuple) or any(not isinstance(s, StrategyState) for s in self.states):
            raise TypeError("states_must_be_tuple_of_StrategyState")
        # 007 §5.2: exit registry/binding/state/link collections are strictly
        # required, participate in checkpoint_hash, and are only empty when no
        # corresponding facts exist. Sorted and unique by identity; every
        # reference resolves inside this checkpoint.
        if not isinstance(self.exit_bindings, tuple) or any(
                not isinstance(item, ExitPolicyBinding) for item in self.exit_bindings):
            raise TypeError("exit_bindings_must_be_tuple_of_ExitPolicyBinding")
        binding_hashes = [item.binding_sha256 for item in self.exit_bindings]
        if binding_hashes != sorted(set(binding_hashes)):
            raise ValueError("exit_bindings_must_be_sorted_unique_by_binding_hash")
        if not isinstance(self.exit_states, tuple) or any(
                not isinstance(item, ExitState) for item in self.exit_states):
            raise TypeError("exit_states_must_be_tuple_of_ExitState")
        state_keys = [(item.account_id, item.contract_id, item.strategy_id,
                       item.episode_id) for item in self.exit_states]
        if state_keys != sorted(set(state_keys)):
            raise ValueError("exit_states_must_be_sorted_unique_by_scope")
        if not isinstance(self.exit_execution_links, tuple) or any(
                not isinstance(item, ExitExecutionLink)
                for item in self.exit_execution_links):
            raise TypeError("exit_execution_links_must_be_tuple_of_ExitExecutionLink")
        link_ids = [item.link_id for item in self.exit_execution_links]
        if link_ids != sorted(set(link_ids)):
            raise ValueError("exit_execution_links_must_be_sorted_unique_by_link_id")
        declared_bindings = set(binding_hashes)
        for item in self.exit_states:
            if item.exit_binding_sha256 not in declared_bindings:
                raise ValueError("exit_state_binding_must_be_declared")
        for item in self.exit_execution_links:
            if item.exit_binding_sha256 not in declared_bindings:
                raise ValueError("exit_execution_link_binding_must_be_declared")
        held_lots = [lot for lot in self.account.lots if lot.lots > 0]
        held_scopes = {(self.account.account_id, lot.contract_id, lot.strategy_id,
                        lot.episode_id) for lot in held_lots}
        if not held_scopes <= set(state_keys):
            raise ValueError("held_lots_require_matching_exit_states")
        binding_strategies = {item.strategy_id for item in self.exit_bindings}
        for intent in self.pending_intents:
            if intent.offset in ("OPEN", "ADD") \
                    and intent.strategy_id not in binding_strategies:
                raise ValueError("pending_open_add_requires_declared_exit_binding")
        if not isinstance(self.pending_intents, tuple) or any(not isinstance(i, OrderIntent) for i in self.pending_intents):
            raise TypeError("pending_intents_must_be_tuple_of_OrderIntent")
        _check_str_tuple(self.processed_event_ids, "processed_event_ids")
        capacity = _freeze_mapping(self.capacity_used, "capacity_used")
        for item in capacity.values():
            _check_int(item, "capacity_used_value")
            if item < 0:
                raise ValueError("capacity_used_value_must_be_non_negative")
        object.__setattr__(self, "capacity_used", capacity)
        # 变更申请-003: valuation marks cover exactly the held contracts, sorted
        # and unique; mark times respect the account clock.
        if not isinstance(self.valuation_marks, tuple) or any(
                not isinstance(mark, ValuationMark) for mark in self.valuation_marks):
            raise TypeError("valuation_marks_must_be_tuple_of_ValuationMark")
        mark_contracts = tuple(mark.contract_id for mark in self.valuation_marks)
        if list(mark_contracts) != sorted(set(mark_contracts)):
            raise ValueError("valuation_marks_must_be_sorted_unique_by_contract")
        held_contracts = {lot.contract_id for lot in self.account.lots}
        if set(mark_contracts) != held_contracts:
            raise ValueError("valuation_marks_must_match_held_contracts")
        for mark in self.valuation_marks:
            if mark.status in ("CURRENT", "STALE"):
                if mark.valued_at_nano > self.account.as_of_nano:
                    raise ValueError("mark_valued_at_must_not_exceed_account_as_of")
                if mark.rule_id not in self.rule_hashes:
                    raise ValueError("mark_rule_must_reference_consumed_rule_hashes")
        # pending_remaining_lots: the intent, state-pending and remaining key
        # sets must be STRICTLY equal (003: every pending intent is owned by
        # exactly one matching state; no orphans on either side, no duplicates).
        remaining = _freeze_typed_mapping(self.pending_remaining_lots,
                                          "pending_remaining_lots")
        intent_deltas = {intent.intent_id: intent.delta_lots
                         for intent in self.pending_intents}
        if len(intent_deltas) != len(self.pending_intents):
            raise ValueError("duplicate_pending_intent_ids")
        state_pending: dict[str, list[StrategyState]] = {}
        for state in self.states:
            for intent_id in state.pending_intent_ids:
                state_pending.setdefault(intent_id, []).append(state)
        expected_keys = set(intent_deltas) | set(state_pending)
        if set(remaining) != expected_keys:
            raise ValueError("pending_remaining_lots_keyset_mismatch")
        if set(intent_deltas) != set(state_pending):
            raise ValueError("pending_intent_and_state_keysets_must_be_equal")
        for intent in self.pending_intents:
            owners = state_pending.get(intent.intent_id, ())
            if len(owners) != 1:
                raise ValueError("pending_intent_requires_exactly_one_owning_state")
            if (owners[0].strategy_id != intent.strategy_id
                    or owners[0].contract_id != intent.contract_id
                    or owners[0].episode_id != intent.episode_id):
                raise ValueError("pending_intent_state_ownership_mismatch")
        for intent_id, value in remaining.items():
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError("pending_remaining_value_must_be_int_not_bool")
            if value == 0:
                raise ValueError("pending_remaining_value_must_be_non_zero")
            delta = intent_deltas.get(intent_id)
            if delta is not None:
                if (value > 0) != (delta > 0):
                    raise ValueError("pending_remaining_sign_must_match_delta")
                if abs(value) > abs(delta):
                    raise ValueError("pending_remaining_must_not_exceed_delta")
        object.__setattr__(self, "pending_remaining_lots", remaining)
        # processed_fill_hashes: complete canonical Fill content hashes.
        fill_hashes = _freeze_typed_mapping(self.processed_fill_hashes,
                                            "processed_fill_hashes")
        for value in fill_hashes.values():
            _check_hash(value, "processed_fill_hashes_value")
        object.__setattr__(self, "processed_fill_hashes", fill_hashes)
        _check_hash(self.previous_hash, "previous_hash")
        _check_hash(self.checkpoint_hash, "checkpoint_hash")


# ─── E-P1 (v2-ep1-training-path-20260917-r1 RULING 2): fixed-candidate
# offline path-measurement source contracts. These types are a SEPARATE
# measurement source family: they never carry GateDecision(eligible=True),
# never produce DecisionRecord.selected_proposal_id, and never fake a READY
# PayoffEstimate. Normal replay/shadow/qualification/broker consumers must
# not accept them, and no type here subclasses the normal trading records
# (duck-typing disguise is impossible by construction).

type PayoffPathAction = Literal["MEASURE_ENTRY", "MANAGE_EXIT", "CANCEL_ENTRY"]
type PayoffPathStatus = Literal["NO_CANDIDATE", "COMPLETE", "INCOMPLETE"]
type PayoffPathSourceKind = Literal["P1_ACTION_CONDITIONAL_REPLAY"]

PAYOFF_PATH_ACTIONS = frozenset({"MEASURE_ENTRY", "MANAGE_EXIT", "CANCEL_ENTRY"})
PAYOFF_PATH_STATUSES = frozenset({"NO_CANDIDATE", "COMPLETE", "INCOMPLETE"})
PAYOFF_PATH_SOURCE_KINDS = frozenset({"P1_ACTION_CONDITIONAL_REPLAY"})
# §2.2: plan.protocol_version / selection_rule are frozen literals.
PAYOFF_TRAINING_PLAN_PROTOCOL_VERSION = "payoff-training-p1-v1"
PAYOFF_TRAINING_PLAN_SELECTION_RULE = "FIRST_FEASIBLE_P1_OPEN_PER_WINDOW"
# §2.3: isolated initial-account id namespace ("payoff-path:...").
PAYOFF_PATH_ACCOUNT_ID_PREFIX = "payoff-path:"
# path_id uses the established stable_id algorithm with this category.
PAYOFF_PATH_ID_CATEGORY = "payoff_path"
PAYOFF_PATH_DECISION_ID_CATEGORY = "payoff_path_decision"
# §3.1.3: the seven fixed domain-check keys of the measurement protocol.
PAYOFF_PATH_CHECK_DOMAINS: tuple[str, ...] = (
    "IDENTITY_TIME", "DATA_CLOCK", "REGIME_MACRO", "FORECAST_CONFIDENCE",
    "TRIGGER_DOMAIN", "COST_BUDGET", "ACCOUNT_CAPACITY",
)
# §2.4: source producer_version of the PayoffPathResult branch AND the
# explicit multi-rule branch. The legacy seven-argument single-rule branch
# keeps "observation-from-replay-p1-v1" (frozen in decision/payoffs.py).
OBSERVATION_FROM_REPLAY_P1_V2 = "observation-from-replay-p1-v2"
# RULING 3.4: the eleven NEW blocking reason suffixes, all in the
# "PAYOFF_INTEGRITY:" namespace, registered by M1 for the measurement path.
# They are disjoint from E1's frozen sets (9x PAYOFF_SAMPLE_UNAVAILABLE,
# 23x PAYOFF_INTEGRITY, 1x PAYOFF_INTEGRITY_PROGRAM) which live in
# ak_trading.v2.decision.payoffs; unknown suffixes never auto-become legal
# exclusions even when they share the prefix.
PAYOFF_PATH_INTEGRITY_SUFFIXES: tuple[str, ...] = (
    "SOURCE_KIND_MISMATCH", "SAMPLING_PLAN_MISMATCH", "CANDIDATE_SOURCE_MISMATCH",
    "PATH_DECISION_CHAIN_MISSING", "PATH_ORDER_CHAIN_MISSING", "RECEIPT_COMMIT_MISMATCH",
    "INITIAL_ACCOUNT_MISMATCH", "RULE_ORIGINAL_CONFLICT", "RULE_CONSUMPTION_MISMATCH",
    "RULE_PIT_MISMATCH", "SOURCE_BYTES_MISMATCH",
)


def _stable_hash_id(category: str, content: str | bytes) -> str:
    """``<category>:<sha256>`` — same algorithm as artifacts.stable_id, kept
    local so the contract layer never imports the publication layer."""
    if not isinstance(category, str) or not category:
        raise TypeError("category_must_be_non_empty_str")
    payload = content.encode("utf-8") if isinstance(content, str) else bytes(content)
    return f"{category}:{hashlib.sha256(payload).hexdigest()}"


def _check_path_id(value: object, name: str) -> None:
    _check_str(value, name)
    text: str = value  # type: ignore[assignment]
    if not text.startswith(f"{PAYOFF_PATH_ID_CATEGORY}:") or len(text) != \
            len(PAYOFF_PATH_ID_CATEGORY) + 1 + 64:
        raise ValueError(f"{name}_must_be_payoff_path_stable_id")
    if any(char not in _HEX for char in text[len(PAYOFF_PATH_ID_CATEGORY) + 1:]):
        raise ValueError(f"{name}_must_be_payoff_path_stable_id")


@dataclass(frozen=True)
class PayoffTrainingPlan:
    """§2.2 pre-frozen fixed sampling plan; carries no self-referential hash
    field (plan_sha256 is derived via payoff_training_plan_sha256)."""

    protocol_version: str
    outer_split_id: str
    inner_split_id: str
    inner_training_end_nano: int
    inner_calibration_end_nano: int
    warmup_start_nano: int
    window_start_nano: int          # candidate-selection window start (inclusive)
    window_end_nano: int            # candidate-selection window end (exclusive)
    training_end_nano: int          # outer training cutoff; execution/maturity never pass it
    contract_ids: tuple[str, ...]   # non-empty, unique, lexicographic
    strategy_id: str
    exit_binding_sha256: str
    initial_cash_cny: Decimal
    calendar_sha256: str
    selection_rule: str

    def __post_init__(self) -> None:
        if self.protocol_version != PAYOFF_TRAINING_PLAN_PROTOCOL_VERSION:
            raise ValueError("protocol_version_must_be_payoff_training_p1_v1")
        for name in ("outer_split_id", "inner_split_id", "strategy_id"):
            _check_str(getattr(self, name), name)
        for name in ("inner_training_end_nano", "inner_calibration_end_nano",
                     "warmup_start_nano", "window_start_nano", "window_end_nano",
                     "training_end_nano"):
            _check_nano(getattr(self, name), name)
        # §2.2 strict chain:
        # inner_training_end < inner_calibration_end < warmup_start
        #   <= window_start < window_end <= training_end
        if not (self.inner_training_end_nano < self.inner_calibration_end_nano
                < self.warmup_start_nano <= self.window_start_nano
                < self.window_end_nano <= self.training_end_nano):
            raise ValueError("plan_timing_chain_violated")
        _check_str_tuple(self.contract_ids, "contract_ids")
        if not self.contract_ids:
            raise ValueError("contract_ids_must_be_non_empty")
        if len(set(self.contract_ids)) != len(self.contract_ids):
            raise ValueError("contract_ids_must_not_contain_duplicates")
        if list(self.contract_ids) != sorted(self.contract_ids):
            raise ValueError("contract_ids_must_be_sorted_unique")
        _check_hash(self.exit_binding_sha256, "exit_binding_sha256")
        _check_decimal(self.initial_cash_cny, "initial_cash_cny")
        if self.initial_cash_cny <= 0:
            raise ValueError("initial_cash_cny_must_be_positive")
        _check_hash(self.calendar_sha256, "calendar_sha256")
        if self.selection_rule != PAYOFF_TRAINING_PLAN_SELECTION_RULE:
            raise ValueError("selection_rule_must_be_first_feasible_p1_open_per_window")


def payoff_training_plan_sha256(plan: PayoffTrainingPlan) -> str:
    """§2.2 ``plan_sha256 = SHA256(canonical(plan))`` over the full encoded
    record; the plan stores no self-referential hash field, so the canonical
    projection can never include its own digest."""
    from ak_trading.v2.schemas import canonical_json_bytes, encode_record
    if not isinstance(plan, PayoffTrainingPlan):
        raise TypeError("plan_must_be_PayoffTrainingPlan")
    return hashlib.sha256(canonical_json_bytes(encode_record(plan))).hexdigest()


def payoff_path_id(plan_sha256: str, input_manifest_sha256: str,
                   initial_account: AccountSnapshot, contract_lock_sha256: str,
                   strategy_registry_sha256: str, exit_registry_sha256: str,
                   model_hashes: Mapping[str, str]) -> str:
    """§2.3 ``path_id = stable_id('payoff_path', canonical([plan_sha256,
    input_manifest_sha256, initial_account, lock, strategy_registry,
    exit_registry, model_hashes]))``. Input paths, wall clock, randomness and
    file mtimes never enter the identity; M4 must build it from the same
    frozen lock/registry identities it executes under."""
    from ak_trading.v2.schemas import canonical_json_bytes, encode_record
    _check_hash(plan_sha256, "plan_sha256")
    _check_hash(input_manifest_sha256, "input_manifest_sha256")
    _check_hash(contract_lock_sha256, "contract_lock_sha256")
    _check_hash(strategy_registry_sha256, "strategy_registry_sha256")
    _check_hash(exit_registry_sha256, "exit_registry_sha256")
    if not isinstance(initial_account, AccountSnapshot):
        raise TypeError("initial_account_must_be_AccountSnapshot")
    frozen_hashes = _freeze_mapping(model_hashes, "model_hashes")
    for value in frozen_hashes.values():
        _check_hash(value, "model_hashes_value")
    payload = [
        plan_sha256, input_manifest_sha256, encode_record(initial_account),
        contract_lock_sha256, strategy_registry_sha256, exit_registry_sha256,
        {key: frozen_hashes[key] for key in sorted(frozen_hashes)},
    ]
    return _stable_hash_id(PAYOFF_PATH_ID_CATEGORY,
                           canonical_json_bytes(payload))  # type: ignore[arg-type]


def payoff_path_decision_source_sha256(
    plan_sha256: str, proposal: StrategyProposal, context: StrategyContext,
    request: "ForecastRequest | None", account_before_sha256: str,
    checks: Mapping[str, tuple[str, ...]], action: PayoffPathAction,
) -> str:
    """§2.3 causal source hash of one path decision: the prior plan, the
    then-visible context/request/proposal, account_before and the domain
    checks/action. Excludes the decision's own id/hash and every future path
    input (input manifest, path_id, final checkpoint); changing future inputs
    may change the final path_id/source but never these bytes."""
    from ak_trading.v2.schemas import canonical_json_bytes, encode_record
    _check_hash(plan_sha256, "plan_sha256")
    if not isinstance(proposal, StrategyProposal):
        raise TypeError("proposal_must_be_StrategyProposal")
    if not isinstance(context, StrategyContext):
        raise TypeError("context_must_be_StrategyContext")
    if request is not None and not isinstance(request, ForecastRequest):
        raise TypeError("request_must_be_ForecastRequest_or_none")
    _check_hash(account_before_sha256, "account_before_sha256")
    frozen_checks = _check_typed_mapping(checks, "checks", _check_str_tuple)
    _check_enum(action, PAYOFF_PATH_ACTIONS, "action")
    payload = {
        "plan_sha256": plan_sha256,
        "proposal": encode_record(proposal),
        "context": encode_record(context),
        "request": None if request is None else encode_record(request),
        "account_before_sha256": account_before_sha256,
        "checks": {key: list(frozen_checks[key]) for key in sorted(frozen_checks)},
        "action": action,
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()  # type: ignore[arg-type]


def payoff_path_decision_id(source_sha256: str) -> str:
    """§2.3 path_decision_id is derived from (only) its source_sha256 via the
    established stable_id algorithm."""
    _check_hash(source_sha256, "source_sha256")
    return _stable_hash_id(PAYOFF_PATH_DECISION_ID_CATEGORY, source_sha256)


@dataclass(frozen=True)
class PayoffPathDecision:
    """One measurement decision; no selected/eligible/weight fields exist."""

    path_decision_id: str
    plan_sha256: str
    observed_at_nano: int
    proposal: StrategyProposal
    context: StrategyContext
    request: ForecastRequest | None
    account_before_sha256: str
    checks: Mapping[str, tuple[str, ...]]
    action: PayoffPathAction
    source_sha256: str

    def __post_init__(self) -> None:
        _check_str(self.path_decision_id, "path_decision_id")
        _check_hash(self.plan_sha256, "plan_sha256")
        _check_nano(self.observed_at_nano, "observed_at_nano")
        if not isinstance(self.proposal, StrategyProposal):
            raise TypeError("proposal_must_be_StrategyProposal")
        if not isinstance(self.context, StrategyContext):
            raise TypeError("context_must_be_StrategyContext")
        if self.request is not None and not isinstance(self.request, ForecastRequest):
            raise TypeError("request_must_be_ForecastRequest_or_none")
        _check_hash(self.account_before_sha256, "account_before_sha256")
        _check_enum(self.action, PAYOFF_PATH_ACTIONS, "action")
        frozen = _check_typed_mapping(self.checks, "checks", _check_str_tuple)
        if set(frozen) != set(PAYOFF_PATH_CHECK_DOMAINS):
            raise ValueError("checks_keys_must_be_exactly_the_seven_domains")
        object.__setattr__(self, "checks", frozen)
        if self.action == "MEASURE_ENTRY":
            # 原生 M3B 单层首次 OPEN、payoff=None、request 必填；成功 ENTRY 的
            # 必要输入/资源检查全部为空。
            if self.request is None:
                raise ValueError("measure_entry_requires_request")
            if self.proposal.action != "OPEN":
                raise ValueError("measure_entry_requires_open_proposal")
            if self.proposal.payoff is not None:
                raise ValueError("measure_entry_requires_payoff_none")
            if self.proposal.budget is None or len(self.proposal.budget.layers) != 1:
                raise ValueError("measure_entry_requires_single_layer_budget")
            if any(frozen[domain] for domain in PAYOFF_PATH_CHECK_DOMAINS):
                raise ValueError("measure_entry_requires_empty_check_reasons")
        expected_source = payoff_path_decision_source_sha256(
            self.plan_sha256, self.proposal, self.context, self.request,
            self.account_before_sha256, frozen, self.action)
        _check_hash(self.source_sha256, "source_sha256")
        if self.source_sha256 != expected_source:
            raise ValueError("source_sha256_must_equal_causal_projection")
        if self.path_decision_id != payoff_path_decision_id(self.source_sha256):
            raise ValueError("path_decision_id_must_derive_from_source_sha256")


@dataclass(frozen=True)
class PayoffPathOrder:
    """OrderIntent's complete economic/clock fields with the single
    replacement decision_id -> path_decision_id; resolves ONLY to a
    PayoffPathDecision in this package, never to a production DecisionRecord."""

    intent_id: str
    path_decision_id: str
    strategy_id: str
    episode_id: str
    contract_id: str
    created_at_nano: int
    not_before_nano: int
    expires_at_nano: int
    delta_lots: int
    offset: Literal["OPEN", "CLOSE", "CLOSE_TODAY"]
    purpose: Literal["ENTRY", "ADD", "REDUCE", "STOP", "TARGET", "TIME_EXIT", "ROLL_EXIT"]
    layer_index: int | None
    limit_price: Decimal | None

    def __post_init__(self) -> None:
        for name in ("intent_id", "path_decision_id", "strategy_id", "episode_id",
                     "contract_id"):
            _check_str(getattr(self, name), name)
        for name in ("created_at_nano", "not_before_nano", "expires_at_nano"):
            _check_nano(getattr(self, name), name)
        if self.created_at_nano > self.not_before_nano or self.not_before_nano >= self.expires_at_nano:
            raise ValueError("created<=not_before<expires_required")
        _check_int(self.delta_lots, "delta_lots")
        if self.delta_lots == 0:
            raise ValueError("delta_lots_must_be_signed_non_zero")
        _check_enum(self.offset, OFFSET_KINDS, "offset")
        _check_enum(self.purpose, INTENT_PURPOSES, "purpose")
        if self.layer_index is not None:
            _check_int(self.layer_index, "layer_index")
            if self.layer_index < 0:
                raise ValueError("layer_index_must_be_0_based")
        _check_optional_decimal(self.limit_price, "limit_price")
        if self.limit_price is not None and self.limit_price <= 0:
            raise ValueError("limit_price_must_be_positive")


@dataclass(frozen=True)
class PayoffPathCheckpoint:
    """LedgerCheckpoint's complete field list plus ``path_id``; the
    ``pending_intents`` element type is strictly PayoffPathOrder. The
    checkpoint_hash is recomputed inside the ak_probability_payoff_path_v1
    schema domain: it never restores as a normal checkpoint and a stripped
    path_id never becomes an old checkpoint (strict decoder mutual
    rejection, schemas.py)."""

    sequence: int
    contract_lock_sha256: str
    registry_sha256: str
    exit_registry_sha256: str
    run_policy: RunPolicy
    rule_hashes: Mapping[str, str]
    model_hashes: Mapping[str, str]
    qualification_hashes: Mapping[str, str]
    account: AccountSnapshot
    valuation_marks: tuple[ValuationMark, ...]
    states: tuple[StrategyState, ...]
    exit_bindings: tuple[ExitPolicyBinding, ...]
    exit_states: tuple[ExitState, ...]
    exit_execution_links: tuple[ExitExecutionLink, ...]
    pending_intents: tuple[PayoffPathOrder, ...]
    pending_remaining_lots: Mapping[str, int]
    processed_event_ids: tuple[str, ...]
    processed_fill_hashes: Mapping[str, str]
    capacity_used: Mapping[str, int]
    previous_hash: str
    checkpoint_hash: str
    path_id: str

    def __post_init__(self) -> None:
        _check_int(self.sequence, "sequence")
        if self.sequence < 0:
            raise ValueError("sequence_must_be_non_negative")
        _check_hash(self.contract_lock_sha256, "contract_lock_sha256")
        _check_hash(self.registry_sha256, "registry_sha256")
        _check_hash(self.exit_registry_sha256, "exit_registry_sha256")
        _check_path_id(self.path_id, "path_id")
        if not isinstance(self.run_policy, RunPolicy):
            raise TypeError("run_policy_must_be_RunPolicy")
        for name in ("rule_hashes", "model_hashes", "qualification_hashes"):
            mapping = _freeze_mapping(getattr(self, name), name)
            for item in mapping.values():
                _check_hash(item, f"{name}_value")
            object.__setattr__(self, name, mapping)
        model_keys = set(self.model_hashes)
        if not set(QUALIFICATION_MODEL_HASH_KEYS) <= model_keys \
                or not model_keys <= {*QUALIFICATION_MODEL_HASH_KEYS, "seasonality"}:
            raise ValueError("checkpoint_model_hash_keys_must_be_complete_fixed_set")
        if not isinstance(self.account, AccountSnapshot):
            raise TypeError("account_must_be_AccountSnapshot")
        if not isinstance(self.states, tuple) or any(not isinstance(s, StrategyState) for s in self.states):
            raise TypeError("states_must_be_tuple_of_StrategyState")
        if not isinstance(self.exit_bindings, tuple) or any(
                not isinstance(item, ExitPolicyBinding) for item in self.exit_bindings):
            raise TypeError("exit_bindings_must_be_tuple_of_ExitPolicyBinding")
        binding_hashes = [item.binding_sha256 for item in self.exit_bindings]
        if binding_hashes != sorted(set(binding_hashes)):
            raise ValueError("exit_bindings_must_be_sorted_unique_by_binding_hash")
        if not isinstance(self.exit_states, tuple) or any(
                not isinstance(item, ExitState) for item in self.exit_states):
            raise TypeError("exit_states_must_be_tuple_of_ExitState")
        state_keys = [(item.account_id, item.contract_id, item.strategy_id,
                       item.episode_id) for item in self.exit_states]
        if state_keys != sorted(set(state_keys)):
            raise ValueError("exit_states_must_be_sorted_unique_by_scope")
        if not isinstance(self.exit_execution_links, tuple) or any(
                not isinstance(item, ExitExecutionLink)
                for item in self.exit_execution_links):
            raise TypeError("exit_execution_links_must_be_tuple_of_ExitExecutionLink")
        link_ids = [item.link_id for item in self.exit_execution_links]
        if link_ids != sorted(set(link_ids)):
            raise ValueError("exit_execution_links_must_be_sorted_unique_by_link_id")
        declared_bindings = set(binding_hashes)
        for item in self.exit_states:
            if item.exit_binding_sha256 not in declared_bindings:
                raise ValueError("exit_state_binding_must_be_declared")
        for item in self.exit_execution_links:
            if item.exit_binding_sha256 not in declared_bindings:
                raise ValueError("exit_execution_link_binding_must_be_declared")
        held_lots = [lot for lot in self.account.lots if lot.lots > 0]
        held_scopes = {(self.account.account_id, lot.contract_id, lot.strategy_id,
                        lot.episode_id) for lot in held_lots}
        if not held_scopes <= set(state_keys):
            raise ValueError("held_lots_require_matching_exit_states")
        binding_strategies = {item.strategy_id for item in self.exit_bindings}
        if not isinstance(self.pending_intents, tuple) or any(
                not isinstance(item, PayoffPathOrder) for item in self.pending_intents):
            raise TypeError("pending_intents_must_be_tuple_of_PayoffPathOrder")
        for intent in self.pending_intents:
            if intent.offset in ("OPEN", "ADD") \
                    and intent.strategy_id not in binding_strategies:
                raise ValueError("pending_open_add_requires_declared_exit_binding")
        _check_str_tuple(self.processed_event_ids, "processed_event_ids")
        capacity = _freeze_mapping(self.capacity_used, "capacity_used")
        for item in capacity.values():
            _check_int(item, "capacity_used_value")
            if item < 0:
                raise ValueError("capacity_used_value_must_be_non_negative")
        object.__setattr__(self, "capacity_used", capacity)
        if not isinstance(self.valuation_marks, tuple) or any(
                not isinstance(mark, ValuationMark) for mark in self.valuation_marks):
            raise TypeError("valuation_marks_must_be_tuple_of_ValuationMark")
        mark_contracts = tuple(mark.contract_id for mark in self.valuation_marks)
        if list(mark_contracts) != sorted(set(mark_contracts)):
            raise ValueError("valuation_marks_must_be_sorted_unique_by_contract")
        held_contracts = {lot.contract_id for lot in self.account.lots}
        if set(mark_contracts) != held_contracts:
            raise ValueError("valuation_marks_must_match_held_contracts")
        for mark in self.valuation_marks:
            if mark.status in ("CURRENT", "STALE"):
                if mark.valued_at_nano > self.account.as_of_nano:
                    raise ValueError("mark_valued_at_must_not_exceed_account_as_of")
                if mark.rule_id not in self.rule_hashes:
                    raise ValueError("mark_rule_must_reference_consumed_rule_hashes")
        remaining = _freeze_typed_mapping(self.pending_remaining_lots,
                                          "pending_remaining_lots")
        intent_deltas = {intent.intent_id: intent.delta_lots
                         for intent in self.pending_intents}
        if len(intent_deltas) != len(self.pending_intents):
            raise ValueError("duplicate_pending_intent_ids")
        state_pending: dict[str, list[StrategyState]] = {}
        for state in self.states:
            for intent_id in state.pending_intent_ids:
                state_pending.setdefault(intent_id, []).append(state)
        expected_keys = set(intent_deltas) | set(state_pending)
        if set(remaining) != expected_keys:
            raise ValueError("pending_remaining_lots_keyset_mismatch")
        if set(intent_deltas) != set(state_pending):
            raise ValueError("pending_intent_and_state_keysets_must_be_equal")
        for intent in self.pending_intents:
            owners = state_pending.get(intent.intent_id, ())
            if len(owners) != 1:
                raise ValueError("pending_intent_requires_exactly_one_owning_state")
            if (owners[0].strategy_id != intent.strategy_id
                    or owners[0].contract_id != intent.contract_id
                    or owners[0].episode_id != intent.episode_id):
                raise ValueError("pending_intent_state_ownership_mismatch")
        for intent_id, value in remaining.items():
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError("pending_remaining_value_must_be_int_not_bool")
            if value == 0:
                raise ValueError("pending_remaining_value_must_be_non_zero")
            delta = intent_deltas.get(intent_id)
            if delta is not None:
                if (value > 0) != (delta > 0):
                    raise ValueError("pending_remaining_sign_must_match_delta")
                if abs(value) > abs(delta):
                    raise ValueError("pending_remaining_must_not_exceed_delta")
        object.__setattr__(self, "pending_remaining_lots", remaining)
        fill_hashes = _freeze_typed_mapping(self.processed_fill_hashes,
                                            "processed_fill_hashes")
        for value in fill_hashes.values():
            _check_hash(value, "processed_fill_hashes_value")
        object.__setattr__(self, "processed_fill_hashes", fill_hashes)
        _check_hash(self.previous_hash, "previous_hash")
        _check_hash(self.checkpoint_hash, "checkpoint_hash")


@dataclass(frozen=True)
class PayoffReceiptCommit:
    """Proof that one execution receipt (including the no-Fill terminal
    states) was actually processed and committed by the simulated scheduler;
    observed_at is the simulated processing time, never a broker time."""

    execution_index: int
    observed_at_nano: int
    checkpoint_sha256: str

    def __post_init__(self) -> None:
        _check_int(self.execution_index, "execution_index")
        if self.execution_index < 0:
            raise ValueError("execution_index_must_be_non_negative")
        _check_nano(self.observed_at_nano, "observed_at_nano")
        _check_hash(self.checkpoint_sha256, "checkpoint_sha256")


@dataclass(frozen=True)
class PayoffPathResult:
    """Complete closed measurement path over one frozen plan window.
    COMPLETE only means the path finished; label maturity is verified
    separately. Integrity failures raise; there is no "legally missing
    sample" return value."""

    source_kind: PayoffPathSourceKind
    path_id: str
    plan: PayoffTrainingPlan
    input_manifest_sha256: str
    initial_account: AccountSnapshot
    features: tuple[FeatureSnapshot, ...]
    forecasts: tuple[MarketForecast, ...]
    regimes: tuple[RegimeAssessment, ...]
    proposals: tuple[StrategyProposal, ...]
    candidate_checks: Mapping[str, Mapping[str, tuple[str, ...]]]
    exit_specs: tuple[ExitSpec, ...]
    exit_binding: ExitPolicyBinding
    exit_features: tuple[ExitFeatureSnapshot, ...]
    exit_proposals: tuple[ExitProposal, ...]
    path_decisions: tuple[PayoffPathDecision, ...]
    orders: tuple[PayoffPathOrder, ...]
    fills: tuple[Fill, ...]
    equity: tuple[AccountSnapshot, ...]
    checkpoints: tuple[PayoffPathCheckpoint, ...]
    execution_results: tuple[ExecutionResult, ...]
    receipt_commits: tuple[PayoffReceiptCommit, ...]
    rule_originals: tuple[TradingRule, ...]
    bar_originals: tuple[MarketBar, ...]
    sessions: tuple[SessionWindow, ...]
    exclusions: tuple[str, ...]
    reconciliation: Mapping[str, Decimal | bool | str]
    status: PayoffPathStatus

    def __post_init__(self) -> None:
        _check_enum(self.source_kind, PAYOFF_PATH_SOURCE_KINDS, "source_kind")
        _check_path_id(self.path_id, "path_id")
        if not isinstance(self.plan, PayoffTrainingPlan):
            raise TypeError("plan_must_be_PayoffTrainingPlan")
        _check_hash(self.input_manifest_sha256, "input_manifest_sha256")
        if not isinstance(self.initial_account, AccountSnapshot):
            raise TypeError("initial_account_must_be_AccountSnapshot")
        if not isinstance(self.exit_binding, ExitPolicyBinding):
            raise TypeError("exit_binding_must_be_ExitPolicyBinding")
        if self.plan.strategy_id != self.exit_binding.strategy_id:
            raise ValueError("plan_strategy_must_match_exit_binding")
        if self.plan.exit_binding_sha256 != self.exit_binding.binding_sha256:
            raise ValueError("plan_exit_binding_hash_must_match_binding")
        # §2.2 isolated payoff-path: namespace + flat start, cash equals plan.
        account = self.initial_account
        if not account.account_id.startswith(PAYOFF_PATH_ACCOUNT_ID_PREFIX):
            raise ValueError("initial_account_id_must_use_payoff_path_namespace")
        if account.initial_cash_cny != self.plan.initial_cash_cny:
            raise ValueError("initial_account_cash_must_equal_plan")
        if (account.cash_cny != self.plan.initial_cash_cny
                or account.equity_cny != self.plan.initial_cash_cny
                or account.day_start_equity_cny != self.plan.initial_cash_cny
                or account.peak_equity_cny != self.plan.initial_cash_cny
                or account.realized_gross_cny != 0 or account.fees_cny != 0
                or account.slippage_cny != 0 or account.unrealized_cny != 0
                or account.day_net_cny != 0 or account.margin_cny != 0
                or account.reserved_margin_cny != 0 or account.lots
                or account.killed):
            raise ValueError("initial_account_must_be_flat_no_residues")
        if account.as_of_nano < self.plan.warmup_start_nano:
            raise ValueError("initial_account_clock_must_not_precede_warmup_start")
        for collection, kind in (
                (self.features, FeatureSnapshot),
                (self.forecasts, MarketForecast),
                (self.regimes, RegimeAssessment),
                (self.proposals, StrategyProposal),
                (self.exit_specs, ExitSpec),
                (self.exit_features, ExitFeatureSnapshot),
                (self.exit_proposals, ExitProposal),
                (self.path_decisions, PayoffPathDecision),
                (self.orders, PayoffPathOrder),
                (self.fills, Fill),
                (self.equity, AccountSnapshot),
                (self.checkpoints, PayoffPathCheckpoint),
                (self.execution_results, ExecutionResult),
                (self.receipt_commits, PayoffReceiptCommit),
                (self.rule_originals, TradingRule),
                (self.bar_originals, MarketBar),
                (self.sessions, SessionWindow)):
            if not isinstance(collection, tuple) \
                    or any(not isinstance(item, kind) for item in collection):
                raise TypeError(f"{kind.__name__.lower()}s_must_be_tuple_of_{kind.__name__}")
        for snapshot in self.equity:
            if snapshot.account_id != account.account_id:
                raise ValueError("equity_snapshots_must_share_initial_account")
        # candidate_checks: exact per-proposal coverage, seven domains each.
        frozen_checks: dict[str, Mapping[str, tuple[str, ...]]] = {}
        raw_checks = self.candidate_checks
        if not isinstance(raw_checks, Mapping):
            raise TypeError("candidate_checks_must_be_mapping")
        for proposal_id, domains in raw_checks.items():
            _check_str(proposal_id, "candidate_checks_key")
            frozen_checks[proposal_id] = _check_typed_mapping(
                domains, f"candidate_checks[{proposal_id!r}]", _check_str_tuple)
        object.__setattr__(self, "candidate_checks", MappingProxyType(frozen_checks))
        proposal_ids = {item.proposal_id for item in self.proposals}
        if set(frozen_checks) != proposal_ids:
            raise ValueError("candidate_checks_must_cover_exactly_checked_proposals")
        for domains in frozen_checks.values():
            if set(domains) != set(PAYOFF_PATH_CHECK_DOMAINS):
                raise ValueError("candidate_checks_keys_must_be_exactly_the_seven_domains")
        # decisions: same frozen plan hash, unique ids, at most one ENTRY.
        plan_hash = payoff_training_plan_sha256(self.plan)
        decision_ids = [item.path_decision_id for item in self.path_decisions]
        if len(set(decision_ids)) != len(decision_ids):
            raise ValueError("path_decision_ids_must_be_unique")
        for item in self.path_decisions:
            if item.plan_sha256 != plan_hash:
                raise ValueError("path_decision_plan_hash_must_match_result_plan")
            if item.proposal.contract_id not in self.plan.contract_ids:
                raise ValueError("path_decision_contract_must_be_in_plan_scope")
            if item.proposal.strategy_id != self.plan.strategy_id:
                raise ValueError("path_decision_strategy_must_match_plan")
        entry_count = sum(1 for item in self.path_decisions
                          if item.action == "MEASURE_ENTRY")
        if entry_count > 1:
            raise ValueError("at_most_one_measure_entry_per_path")
        # orders: resolve only into this package's decisions.
        order_intent_ids = [order.intent_id for order in self.orders]
        if len(set(order_intent_ids)) != len(order_intent_ids):
            raise ValueError("order_intent_ids_must_be_unique")
        for order in self.orders:
            if order.path_decision_id not in decision_ids:
                raise ValueError("path_order_must_resolve_to_path_decision")
            if order.strategy_id != self.plan.strategy_id:
                raise ValueError("path_order_strategy_must_match_plan")
            if order.contract_id not in self.plan.contract_ids:
                raise ValueError("path_order_contract_must_be_in_plan_scope")
        if self.orders and entry_count != 1:
            raise ValueError("orders_require_exactly_one_measure_entry")
        # fills and execution results resolve into this package's orders.
        fill_ids = [fill.fill_id for fill in self.fills]
        if len(set(fill_ids)) != len(fill_ids):
            raise ValueError("fill_ids_must_be_unique")
        for fill in self.fills:
            if fill.intent_id not in set(order_intent_ids):
                raise ValueError("path_fill_must_resolve_to_path_order")
            if fill.contract_id not in self.plan.contract_ids:
                raise ValueError("path_fill_contract_must_be_in_plan_scope")
        for result in self.execution_results:
            if result.intent_id not in set(order_intent_ids):
                raise ValueError("execution_result_must_resolve_to_path_order")
        # checkpoints: same path identity, unique hashes, linked chain.
        checkpoint_hashes = [item.checkpoint_hash for item in self.checkpoints]
        if len(set(checkpoint_hashes)) != len(checkpoint_hashes):
            raise ValueError("checkpoint_hashes_must_be_unique")
        sequences = [item.sequence for item in self.checkpoints]
        if sequences != sorted(sequences) or len(set(sequences)) != len(sequences):
            raise ValueError("checkpoints_must_be_sorted_unique_by_sequence")
        for index, item in enumerate(self.checkpoints):
            if item.path_id != self.path_id:
                raise ValueError("checkpoint_path_id_must_match_result")
            if item.account.account_id != account.account_id:
                raise ValueError("checkpoint_account_must_share_initial_account")
            expected_previous = "0" * 64 if index == 0 \
                else checkpoint_hashes[index - 1]
            if item.previous_hash != expected_previous:
                raise ValueError("checkpoint_previous_hash_chain_broken")
            for link in item.exit_execution_links:
                if link.decision_id not in decision_ids:
                    raise ValueError(
                        "checkpoint_exit_link_must_resolve_to_path_decision")
                if link.intent_id not in set(order_intent_ids):
                    raise ValueError(
                        "checkpoint_exit_link_must_resolve_to_path_order")
        # receipt commits: index, checkpoint and fill-clock discipline.
        for commit in self.receipt_commits:
            if commit.execution_index >= len(self.execution_results):
                raise ValueError("receipt_commit_execution_index_out_of_range")
            if commit.checkpoint_sha256 not in set(checkpoint_hashes):
                raise ValueError("receipt_commit_checkpoint_must_be_in_path")
            result = self.execution_results[commit.execution_index]
            for fill in result.fills:
                if fill.execution_nano > commit.observed_at_nano:
                    raise ValueError("receipt_commit_must_not_precede_fill")
        # exit originals only empty when no such facts exist.
        if (self.exit_proposals or self.exit_features) and not self.exit_specs:
            raise ValueError("exit_facts_require_exit_specs_originals")
        _check_str_tuple(self.exclusions, "exclusions")
        reconciliation = _check_typed_mapping(
            self.reconciliation, "reconciliation",
            lambda value, name: (_check_decimal(value, name) if isinstance(value, Decimal)
                                 else _check_bool(value, name) if isinstance(value, bool)
                                 else _check_str(value, name)))
        object.__setattr__(self, "reconciliation", reconciliation)
        _check_enum(self.status, PAYOFF_PATH_STATUSES, "status")
        if self.status == "NO_CANDIDATE":
            if entry_count or self.orders or self.fills \
                    or self.execution_results or self.receipt_commits:
                raise ValueError("no_candidate_path_must_have_no_entry_orders_or_fills")
