"""Frozen contracts for the futures strategy library.

This module is the bottom layer of ``ak_trading.futures`` (finalized-plan
§三): data/structures/strategies/portfolio depend on these types; nothing in
this module imports them. It also declares the later-stage contracts
(``PortfolioTarget``, ``Fill``, ``Trade``, ``ResearchPolicy``) so S2 receives
stable interfaces instead of stubs.

Unknown-vs-zero discipline (§四.7): every nullable quantitative field is
``None`` plus an explicit reason elsewhere; a numeric ``0`` must come from a
source that explicitly states zero. Money, fees and multipliers use
``Decimal``; signals and statistics may use finite ``float``; booleans are
never used as numbers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Mapping, Sequence

__all__ = [
    "ASSET_CLASS_FUTURE",
    "POSITION_UNIT_STRATEGY_RISK",
    "EARLIEST_EXECUTION_NEXT_OPEN",
    "SUPPORTED_DURATION_SECONDS",
    "FUTURES_EXCHANGES",
    "REGISTRY_DONCHIAN_TREND",
    "REGISTRY_MA_TREND",
    "REGISTRY_ZSCORE_REVERSION",
    "REGISTRY_TERM_STRUCTURE_CARRY",
    "REGISTRY_LEGACY_DUAL_MA_REFERENCE",
    "ClockStatus",
    "ProductKey",
    "ContractBar",
    "MetadataField",
    "ContractMetadata",
    "FeeComponent",
    "FeeSpec",
    "MarginRate",
    "MarginSpec",
    "StructureFrame",
    "FactorValue",
    "PairSelectionState",
    "StrategyState",
    "StrategySpec",
    "StrategyContext",
    "StrategyResult",
    "ReadinessAction",
    "FieldEvidence",
    "GateResult",
    "PortfolioTarget",
    "Fill",
    "FeeAmount",
    "Trade",
    "ResearchPolicy",
    "parse_product_token",
]

ASSET_CLASS_FUTURE = "FUTURE"
POSITION_UNIT_STRATEGY_RISK = "strategy_risk_unit"
EARLIEST_EXECUTION_NEXT_OPEN = "next_eligible_bar_open"
SUPPORTED_DURATION_SECONDS = (86400,)

#: Exchange tokens accepted by the CLI grammar (ASCII upper, §五.2).
FUTURES_EXCHANGES = frozenset({"SHFE", "INE", "DCE", "CZCE", "CFFEX", "GFEX"})

REGISTRY_DONCHIAN_TREND = "donchian_trend"
REGISTRY_MA_TREND = "ma_trend"
REGISTRY_ZSCORE_REVERSION = "zscore_reversion"
REGISTRY_TERM_STRUCTURE_CARRY = "term_structure_carry"
#: Compatibility reference only; never counts as a fifth strategy (§三.6).
REGISTRY_LEGACY_DUAL_MA_REFERENCE = "legacy_dual_ma_reference"

_FUTURES_FAMILIES = frozenset({"trend", "mean_reversion", "term_structure_carry"})

#: Actions recognized by the normalized gate table (§七).
READINESS_ACTIONS = (
    "observe",
    "signal",
    "open_long",
    "open_short",
    "close_today",
    "close_history",
    "value",
)


class ClockStatus(str, Enum):
    """How trustworthy a bar/decision clock is (§四.2, §五.7)."""

    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    OBSERVATION_ONLY = "observation_only"


def parse_product_token(token: str) -> ProductKey:
    """Parse and normalize an ``exchange.product`` CLI token (§五.2).

    The exchange part is ASCII-uppercased and must be one of the six futures
    exchanges; the product part must be ASCII letters only. Catalog membership
    is checked by the data layer, not here.
    """
    value = (token or "").strip()
    exchange, separator, product = value.partition(".")
    if not separator or not exchange or not product:
        raise ValueError(
            f"product token must use EXCHANGE.product format: {token!r}"
        )
    exchange_upper = exchange.upper()
    if exchange_upper not in FUTURES_EXCHANGES:
        raise ValueError(
            f"unknown exchange {exchange!r}; expected one of "
            f"{sorted(FUTURES_EXCHANGES)}"
        )
    if not product.isascii() or not product.isalpha():
        raise ValueError(
            f"product part must be ASCII letters only: {token!r}"
        )
    return ProductKey(exchange=exchange_upper, product=product.upper())


@dataclass(frozen=True)
class ProductKey:
    """Normalized exchange.product identity; case-insensitive on input."""

    exchange: str
    product: str

    def __post_init__(self) -> None:
        if self.exchange not in FUTURES_EXCHANGES:
            raise ValueError(f"invalid exchange: {self.exchange!r}")
        if not self.product.isascii() or not self.product.isalpha():
            raise ValueError(f"invalid product code: {self.product!r}")

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.exchange}.{self.product}"


@dataclass(frozen=True)
class ContractBar:
    """One completed contract-local daily bar with raw identity intact.

    ``datetime_nano`` (nanoseconds) is the authoritative ordering identity;
    ``ts`` keeps the raw microsecond-precision timestamp. Nullable timing
    fields carry ``None`` when unknown — never fabricated values.
    """

    symbol: str
    duration_seconds: int
    datetime_nano: int
    ts: datetime
    source: str
    trading_day: date | None = None
    trading_day_source: str = ""
    session_open: datetime | None = None
    session_close: datetime | None = None
    available_at: datetime | None = None
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    close: Decimal | None = None
    volume: Decimal | None = None  # NULL volume/OI stays NULL (§六.4)
    open_oi: Decimal | None = None
    close_oi: Decimal | None = None

    def __post_init__(self) -> None:
        if self.duration_seconds not in SUPPORTED_DURATION_SECONDS:
            raise ValueError(
                f"unsupported duration_seconds: {self.duration_seconds}"
            )
        if self.ts.tzinfo is None:
            raise ValueError("bar ts must be timezone-aware")


@dataclass(frozen=True)
class MetadataField:
    """One sourced metadata value with contract/product scope and interval.

    Effective intervals are left-closed right-open:
    ``effective_from <= event_time < effective_through`` (§四.6).
    """

    name: str
    value: object | None
    scope: str  # "product" | "contract"
    contract: str | None
    effective_from: datetime
    effective_through: datetime
    available_at: datetime
    source_id: str
    source_sha256: str | None = None
    unit: str | None = None
    basis: str | None = None
    approval_status: str = "unapproved"  # approved | reference_only | reconstructed_static | review_required | unapproved
    temporal_quality: str = "reconstructed_static"

    def covers(self, event_time: datetime) -> bool:
        return self.effective_from <= event_time < self.effective_through

    def visible_at(self, decision_time: datetime) -> bool:
        return self.available_at <= decision_time


@dataclass(frozen=True)
class ContractMetadata:
    """Read-only metadata bag for one contract instance."""

    contract_instance_id: str
    raw_symbol: str
    product: ProductKey
    expiry_at: datetime | None
    fields: Mapping[str, MetadataField] = field(default_factory=dict)
    identity_status: str = "reconstructed_static"
    listing_at: datetime | None = None
    last_trade_at: datetime | None = None
    delivery_cutoff_at: datetime | None = None


@dataclass(frozen=True)
class FeeComponent:
    """One fee leg; ``cny_per_lot`` and ``rate_per_notional`` never mix."""

    cny_per_lot: Decimal | None = None
    rate_per_notional: Decimal | None = None
    source_id: str = ""
    explicit_zero: bool = False

    def __post_init__(self) -> None:
        if (self.cny_per_lot is None) == (self.rate_per_notional is None):
            raise ValueError(
                "exactly one of cny_per_lot / rate_per_notional is required"
            )
        if self.cny_per_lot is not None and self.cny_per_lot < 0:
            raise ValueError("cny_per_lot must be non-negative")
        if self.rate_per_notional is not None and self.rate_per_notional < 0:
            raise ValueError("rate_per_notional must be non-negative")


@dataclass(frozen=True)
class FeeSpec:
    """open/close/close_today fee schedule (§四.7)."""

    open: FeeComponent | None = None
    close: FeeComponent | None = None
    close_today: FeeComponent | None = None
    basis: str | None = None
    source_id: str = ""

    def leg(self, offset: str) -> FeeComponent | None:
        if offset == "open":
            return self.open
        if offset == "close":
            return self.close
        if offset == "close_today":
            return self.close_today
        raise ValueError(f"unknown fee offset: {offset!r}")


@dataclass(frozen=True)
class MarginRate:
    rate: Decimal
    benchmark: str = "settlement"  # settlement | close_price


@dataclass(frozen=True)
class MarginSpec:
    """Long/short margin with basis separated from account estimates."""

    long: MarginRate | None = None
    short: MarginRate | None = None
    basis: str = "exchange_only"  # exchange_only | account_estimate
    source_id: str = ""


@dataclass(frozen=True)
class StructureFrame:
    """Causal structure snapshot as of one completed observation.

    Computed by ``ak_trading.futures.structures``; consumed through
    ``StrategyContext``. ``None`` fields mean "not computable from the
    declared observation prefix" (insufficient history, zero variance, ...).
    """

    close: float | None = None
    upper_entry: float | None = None
    lower_entry: float | None = None
    upper_exit: float | None = None
    lower_exit: float | None = None
    fast_ma: float | None = None
    slow_ma: float | None = None
    lagged_mean: float | None = None
    lagged_std: float | None = None
    zscore: float | None = None
    er20: float | None = None
    er_status: str | None = None  # "no_movement" when the ER denominator is 0
    regime: str = "unavailable"  # trend | range | transition | unavailable
    daily_vol20: float | None = None
    window_length: int = 0
    status: str = "unavailable"
    reason: str | None = None


@dataclass(frozen=True)
class FactorValue:
    """A named factor with explicit unknown semantics (§四.4)."""

    name: str
    value: float | None
    status: str  # ready | unavailable
    reason: str | None = None
    non_actionable: bool = False


@dataclass(frozen=True)
class PairSelectionState:
    """Carry near/far selection state (§五.12).

    ``confirmation_count`` counts consecutive synchronized observations in
    which the pending candidate far was selected; switching requires two.
    """

    near_symbol: str = ""
    far_symbol: str | None = None
    far_expiry_at: datetime | None = None
    pending_far_symbol: str | None = None
    pending_far_expiry_at: datetime | None = None
    confirmation_count: int = 0

    def __post_init__(self) -> None:
        if self.confirmation_count < 0:
            raise ValueError("confirmation_count must be non-negative")


@dataclass(frozen=True)
class StrategyState:
    """Logical (non-account) strategy state carried across decisions (§四.5)."""

    strategy_id: str
    direction: int = 0  # logical -1 / 0 / +1; never a lots count
    signal_age: int | None = None  # completed observations inside the signal
    contract_instance_id: str | None = None
    episode_id: str | None = None
    pair: PairSelectionState | None = None

    def __post_init__(self) -> None:
        if self.direction not in (-1, 0, 1):
            raise ValueError("direction must be -1, 0 or +1")
        if self.signal_age is not None and self.signal_age < 0:
            raise ValueError("signal_age must be non-negative")


@dataclass(frozen=True)
class StrategySpec:
    """Static declaration of one strategy implementation (§四.1)."""

    strategy_id: str
    family: str
    version: str
    parameters: Mapping[str, object]
    required_bars: int
    required_features: tuple[str, ...]
    required_factors: tuple[str, ...]
    asset_class: str = ASSET_CLASS_FUTURE
    supported_durations: tuple[int, ...] = SUPPORTED_DURATION_SECONDS

    def __post_init__(self) -> None:
        if self.asset_class != ASSET_CLASS_FUTURE:
            raise ValueError("futures library only accepts asset_class=FUTURE")
        if self.family not in _FUTURES_FAMILIES and self.strategy_id != REGISTRY_LEGACY_DUAL_MA_REFERENCE:
            raise ValueError(f"unknown family: {self.family!r}")
        if self.required_bars <= 0:
            raise ValueError("required_bars must be positive")
        if self.supported_durations != SUPPORTED_DURATION_SECONDS:
            raise ValueError("futures strategies only support daily bars")


@dataclass(frozen=True)
class StrategyContext:
    """Everything a strategy may see at one decision point (§四.2).

    ``bars`` is the as-of truncated, contract-local completed history. The
    host performs every truncation; strategies never see future rows.
    """

    strategy_id: str
    product: ProductKey
    contract_instance_id: str
    raw_symbol: str
    decision_at: datetime | None  # None => observation_only prefix
    clock_status: ClockStatus
    completed_bar_id: str | None
    bars: tuple[ContractBar, ...]
    structure: StructureFrame | None
    factors: Mapping[str, FactorValue | None]
    expiry_at: datetime | None = None
    roll_selector_state: object | None = None  # RollSelectionState from data.py
    pair_candidates: tuple[ContractMetadata, ...] = ()  # carry far candidates
    pair_bars: Mapping[str, tuple[ContractBar, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class StrategyResult:
    """Unified strategy output (§四.3).

    ``target_position`` is a direction risk unit in {-1, 0, +1, None}; lots
    live in ``PortfolioTarget.target_lots`` and only fills change holdings.
    """

    strategy_id: str
    family: str
    product: ProductKey
    concrete_symbol: str
    decision_at: datetime | None
    target_position: int | None
    status: str  # ready | flat | warmup | unavailable
    reason: str
    diagnostics: Mapping[str, object] = field(default_factory=dict)
    position_unit: str = POSITION_UNIT_STRATEGY_RISK
    earliest_execution: str = EARLIEST_EXECUTION_NEXT_OPEN
    next_state: StrategyState | None = None

    def __post_init__(self) -> None:
        if self.target_position not in (-1, 0, 1, None):
            raise ValueError("target_position must be -1, 0, +1 or None")
        if self.position_unit != POSITION_UNIT_STRATEGY_RISK:
            raise ValueError("futures results use strategy_risk_unit")
        if self.status in ("ready", "flat") and self.target_position is None:
            raise ValueError("ready/flat results need a concrete direction")
        if self.status in ("warmup", "unavailable") and self.target_position is not None:
            raise ValueError("warmup/unavailable results must have null target")
        if self.status == "ready" and self.target_position == 0:
            raise ValueError("ready corresponds to a non-zero direction")


class ReadinessAction(str, Enum):
    """Normalized gate actions (§七.1). Roll decomposes into close+open."""

    OBSERVE = "observe"
    SIGNAL = "signal"
    OPEN_LONG = "open_long"
    OPEN_SHORT = "open_short"
    CLOSE_TODAY = "close_today"
    CLOSE_HISTORY = "close_history"
    VALUE = "value"


@dataclass(frozen=True)
class FieldEvidence:
    """Field-level evidence fed into the gate table (§七.3).

    ``status`` is one of: ``present_approved``, ``present_reference_only``,
    ``present_unapproved``, ``missing``, ``unknown``, ``invalid``,
    ``not_applicable`` (with basis), ``not_requested``. Presence without
    adequate source/unit/range/approval blocks exactly like missing.
    """

    name: str
    status: str
    reason: str | None = None
    evidence_id: str | None = None

    def blocks(self) -> bool:
        return self.status in (
            "missing",
            "unknown",
            "invalid",
            "present_reference_only",
            "present_unapproved",
        )


@dataclass(frozen=True)
class GateResult:
    """Leaf execution-readiness conclusion; only ready or blocked (§七.3)."""

    action: ReadinessAction
    product: ProductKey
    contract_instance_id: str
    trading_day: date | None
    event_time: datetime
    status: str  # ready | blocked
    missing_fields: tuple[str, ...] = ()
    invalid_fields: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    checked_at_event_time: datetime | None = None


@dataclass(frozen=True)
class PortfolioTarget:
    """Portfolio-level execution intent in integer lots (S2 contract)."""

    decision_id: str
    strategy_ids: tuple[str, ...]
    product: ProductKey
    contract_instance_id: str
    raw_symbol: str
    target_lots: int
    status: str  # active | netted_flat | slot_unallocated | risk_refusal | below_minimum_lot | close_intent
    reason: str
    requested_exposure: float | None = None
    post_risk_exposure: float | None = None
    q_star: Decimal | None = None
    raw_requested_exposure: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.target_lots, int) or isinstance(self.target_lots, bool):
            raise ValueError("target_lots must be an int")


@dataclass(frozen=True)
class FeeAmount:
    """One realized fee component in CNY."""

    name: str
    cny: Decimal
    basis: str


@dataclass(frozen=True)
class Fill:
    """Ideal-price ledger fill; PnL always uses ``ideal_open`` (§十.5)."""

    fill_id: str
    decision_id: str
    product: ProductKey
    contract_instance_id: str
    raw_symbol: str
    event_time: datetime
    trading_day: date | None
    side: int  # +1 buy, -1 sell
    offset: str  # open | close | close_today
    lots: int
    ideal_open: Decimal
    effective_price: Decimal
    multiplier: Decimal
    fee_components: tuple[FeeAmount, ...] = ()
    fee_cny: Decimal = Decimal("0")
    slippage_cost: Decimal = Decimal("0")
    rule_evidence_ids: tuple[str, ...] = ()
    used_bar_id: str | None = None

    def __post_init__(self) -> None:
        if self.side not in (-1, 1):
            raise ValueError("side must be +1 or -1")
        if self.offset not in ("open", "close", "close_today"):
            raise ValueError("offset must be open/close/close_today")
        if self.lots <= 0:
            raise ValueError("lots must be positive")


@dataclass(frozen=True)
class Trade:
    """FIFO closed trade on one concrete contract (§十一.10)."""

    trade_id: str
    strategy_id: str
    product: ProductKey
    contract_instance_id: str
    raw_symbol: str
    opened_event_time: datetime
    closed_event_time: datetime
    direction: int
    lots: int
    entry_ideal_price: Decimal
    exit_ideal_price: Decimal
    multiplier: Decimal
    opening_fees: Decimal
    closing_fees: Decimal
    opening_slippage: Decimal
    closing_slippage: Decimal
    gross_pnl: Decimal
    net_pnl: Decimal
    episode_id: str | None = None
    holding_days: int | None = None


@dataclass(frozen=True)
class ResearchPolicy:
    """Frozen risk/capital/cost policy shared by portfolio and engine (§九).

    Values mirror finalized-plan §九; changing any of them produces a new
    config digest rather than silently altering an observed run.
    """

    target_daily: float = 0.10 / (252 ** 0.5)
    slot_share_base: float = 1.0 / 18.0  # 3 families * 6 products
    sizing_vol_floor: float = 0.005
    instrument_cap: float = 0.20
    gross_cap: float = 0.80
    cluster_cap: float = 0.50
    vol_floor_for_risk: float = 1e-12
    max_margin_ratio: float = 0.40
    drawdown_kill: float = 0.15
    participation_cap: float = 0.001
    initial_nav_cny: Decimal = Decimal("400000000")
    # Cost scenarios (§九.11): base = confirmed fees + 0.01 CNY/lot/side
    # account addition + 1 tick/side; stress = all components x2 + 2 ticks.
    account_fee_addition_cny_per_lot: Decimal = Decimal("0.01")
    base_slippage_ticks: int = 1
    stress_slippage_ticks: int = 2
    stress_fee_multiplier: int = 2
    expiry_buffer_days: int = 20
    exit_session_buffer: int = 5
    pair_gap_days_min: int = 60
    pair_gap_days_max: int = 180
    pair_gap_days_target: int = 90
    roll_volume_ratio: float = 1.2
    roll_window_days: int = 20

    def __post_init__(self) -> None:
        if self.slot_share_base <= 0:
            raise ValueError("slot_share_base must be positive")


UTC = timezone.utc
