"""Pure, immutable contracts for the disabled-by-default Infiny execution domain.

No native imports, I/O, environment access, clocks, or implicit type coercion.
Wire data describes evidence; it never authenticates an external source.
"""

from dataclasses import dataclass, fields
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
import hashlib
import json
import re
from types import UnionType
from typing import TypeVar, Union, get_args, get_origin

SCHEMA_VERSION = "infiny_execution_v1"
V2_SCHEMA_VERSION = "infiny_execution_v2"
MAX_MESSAGE_BYTES = 1_048_576
MAX_NESTING = 32
NATIVE_FULL_ACCOUNT_RECONCILIATION = "unsupported"
NATIVE_SUBMIT_ENABLED = False
NATIVE_CANCEL_OWNED_ENABLED = False


class ContractError(ValueError):
    """Malformed or internally contradictory contract, never an execution result."""


class ExecutionMode(str, Enum):
    DISABLED = "disabled"
    SIMULATION = "simulation"
    NATIVE = "native"


class Source(str, Enum):
    SIMULATION = "simulation"
    PYTHONGO = "pythongo"


class Action(str, Enum):
    OPEN_LONG = "OPEN_LONG"
    OPEN_SHORT = "OPEN_SHORT"
    CLOSE_LONG = "CLOSE_LONG"
    CLOSE_SHORT = "CLOSE_SHORT"
    REVERSE_TO_LONG = "REVERSE_TO_LONG"
    REVERSE_TO_SHORT = "REVERSE_TO_SHORT"


class ClosePolicy(str, Enum):
    AUTO_YD_FIRST = "AUTO_YD_FIRST"
    TODAY_ONLY = "TODAY_ONLY"
    YESTERDAY_ONLY = "YESTERDAY_ONLY"


class OrderType(str, Enum):
    GFD = "GFD"
    FAK = "FAK"


class Direction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class Offset(str, Enum):
    OPEN = "OPEN"
    CLOSE = "CLOSE"
    CLOSE_TODAY = "CLOSE_TODAY"
    CLOSE_YESTERDAY = "CLOSE_YESTERDAY"


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"


class Bucket(str, Enum):
    TD = "TD"
    YD = "YD"
    ALL = "ALL"


class Phase(str, Enum):
    CLOSE = "CLOSE"
    OPEN = "OPEN"


class Completeness(str, Enum):
    UNKNOWN = "unknown"
    PARTIAL = "partial"
    COMPLETE = "complete"


class CoverageLevel(str, Enum):
    UNKNOWN = "unknown"
    CALLBACKS_SINCE_START = "callbacks_since_start"
    OWN_JOURNAL = "own_journal"
    FULL_ACCOUNT = "full_account"


class MarketStatus(str, Enum):
    CONTINUOUS = "continuous"
    AUCTION = "auction"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class AssetClass(str, Enum):
    FUTURE = "future"
    OPTION = "option"
    INDEX = "index"
    CONTINUOUS = "continuous"
    COMBINATION = "combination"
    UNKNOWN = "unknown"


class ConnectionState(str, Enum):
    OFFLINE = "offline"
    SERVICE_ONLY = "service_only"
    ROUNDTRIP = "roundtrip"
    STALE = "stale"


class ReadinessState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTED = "CONNECTED"
    OBSERVING = "OBSERVING"
    PLAN_READY = "PLAN_READY"
    EXECUTABLE = "EXECUTABLE"
    RECONCILE_REQUIRED = "RECONCILE_REQUIRED"


class OrderState(str, Enum):
    RESERVED = "RESERVED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED_LOCAL = "SUBMITTED_LOCAL"
    WORKING = "WORKING"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    LOCAL_REJECTED = "LOCAL_REJECTED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    PARTIAL_CANCELLED = "PARTIAL_CANCELLED"
    SUBMIT_UNKNOWN = "SUBMIT_UNKNOWN"
    EXPIRED = "EXPIRED"


class CancelState(str, Enum):
    NONE = "NONE"
    REQUESTED = "REQUESTED"
    CONFIRMED = "CONFIRMED"
    UNKNOWN = "UNKNOWN"


class ReservationState(str, Enum):
    HELD = "HELD"
    REFLECTED = "REFLECTED"
    RELEASED = "RELEASED"


class ReconcileReason(str, Enum):
    SUBMIT_UNKNOWN = "SUBMIT_UNKNOWN"
    OWNERSHIP_CONFLICT = "OWNERSHIP_CONFLICT"
    EVENT_GAP = "EVENT_GAP"
    LEDGER_FAILURE = "LEDGER_FAILURE"
    LEDGER_CORRUPT = "LEDGER_CORRUPT"
    RESTART_UNRESOLVED = "RESTART_UNRESOLVED"
    BASELINE_GAP = "BASELINE_GAP"
    TRADE_GAP = "TRADE_GAP"
    QUANTITY_CONFLICT = "QUANTITY_CONFLICT"
    CANCEL_UNKNOWN = "CANCEL_UNKNOWN"


class AuthorizationAction(str, Enum):
    OPEN_LONG = "OPEN_LONG"
    OPEN_SHORT = "OPEN_SHORT"
    CLOSE_LONG = "CLOSE_LONG"
    CLOSE_SHORT = "CLOSE_SHORT"
    REVERSE_TO_LONG = "REVERSE_TO_LONG"
    REVERSE_TO_SHORT = "REVERSE_TO_SHORT"
    CANCEL_OWNED = "CANCEL_OWNED"


class EventKind(str, Enum):
    ORDER = "order"
    TRADE = "trade"
    CANCEL = "cancel"
    ERROR = "error"


class Ownership(str, Enum):
    OWNED = "owned"
    FOREIGN = "foreign"
    UNKNOWN = "unknown"


class ResultCode(str, Enum):
    OK = "OK"
    IDEMPOTENT = "IDEMPOTENT"
    BLOCKED = "BLOCKED"
    INTENT_CONFLICT = "INTENT_CONFLICT"
    REVISION_CONFLICT = "REVISION_CONFLICT"
    NOT_FOUND = "NOT_FOUND"
    PERSISTENCE_ERROR = "PERSISTENCE_ERROR"
    QUARANTINED = "QUARANTINED"


TERMINAL_ORDER_STATES = frozenset({OrderState.FILLED, OrderState.LOCAL_REJECTED, OrderState.REJECTED,
                                  OrderState.CANCELLED, OrderState.PARTIAL_CANCELLED, OrderState.EXPIRED})
INFLIGHT_ORDER_STATES = frozenset({OrderState.SUBMITTING, OrderState.SUBMITTED_LOCAL, OrderState.WORKING,
                                  OrderState.PARTIAL, OrderState.SUBMIT_UNKNOWN})


def _require(condition, message):
    if not condition:
        raise ContractError(message)


def _identifier(value, name):
    _require(type(value) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}", value) is not None,
             f"{name}: expected an opaque nonempty identifier")


def _integer(value, name, minimum=0):
    _require(type(value) is int and minimum <= value <= 2**63 - 1,
             f"{name}: expected integer >= {minimum}, not bool")


def _decimal(value, name, positive=False):
    _require(type(value) is Decimal and value.is_finite(), f"{name}: expected finite Decimal")
    _require(value >= 0 and (not positive or value > 0), f"{name}: invalid amount")
    _require(len(value.as_tuple().digits) <= 64 and abs(value.as_tuple().exponent) <= 128,
             f"{name}: decimal magnitude/precision exceeds wire limits")


def _utc(value, name):
    _require(type(value) is datetime and value.tzinfo is not None and value.utcoffset() is not None,
             f"{name}: expected aware datetime")
    try:
        return value.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise ContractError(f"{name}: invalid UTC datetime") from exc


def _typed(value, annotation, name):
    """Check concrete annotations without evaluating strings or forward references."""
    origin = get_origin(annotation)
    if origin in (Union, UnionType):
        for choice in get_args(annotation):
            try:
                return _typed(value, choice, name)
            except ContractError:
                pass
        raise ContractError(f"{name}: value does not match the declared union")
    if origin is tuple:
        _require(type(value) is tuple, f"{name}: expected immutable tuple")
        subtype, repeat = get_args(annotation)
        _require(repeat is Ellipsis, f"{name}: unsupported tuple type")
        return tuple(_typed(item, subtype, name) for item in value)
    if annotation is datetime:
        return _utc(value, name)
    _require(type(value) is annotation, f"{name}: expected {annotation.__name__}")
    if annotation is Decimal:
        _require(value.is_finite(), f"{name}: expected finite Decimal")
        _require(len(value.as_tuple().digits) <= 64 and abs(value.as_tuple().exponent) <= 128,
                 f"{name}: decimal magnitude/precision exceeds wire limits")
    if annotation is int:
        _integer(value, name)
    if annotation is str:
        _require(len(value) <= 256 and not any(ord(ch) < 32 for ch in value), f"{name}: invalid text")
    return value


class ContractData:
    __slots__ = ()
    # Wire namespace of this contract family; v1 types inherit the default and
    # stay byte-identical. Sibling v2/transport modules override the attribute.
    schema = SCHEMA_VERSION

    def __post_init__(self):
        for field in fields(self):
            value = _typed(getattr(self, field.name), field.type, field.name)
            object.__setattr__(self, field.name, value)
        self._validate()

    def _validate(self):
        pass


@dataclass(frozen=True, slots=True, kw_only=True)
class Symbol(ContractData):
    exchange: str
    instrument_id: str

    def _validate(self):
        _identifier(self.exchange, "exchange")
        _identifier(self.instrument_id, "instrument_id")


@dataclass(frozen=True, slots=True, kw_only=True)
class Binding(ContractData):
    account_key: str
    strategy_key: str
    strategy_id: int
    instance_id: str
    session_epoch: str
    trading_day: date
    exchange: str
    instrument_id: str
    hedge_flag: str

    def _validate(self):
        for name in ("account_key", "strategy_key", "instance_id", "session_epoch", "exchange", "instrument_id", "hedge_flag"):
            _identifier(getattr(self, name), name)


@dataclass(frozen=True, slots=True, kw_only=True)
class TimingPolicy(ContractData):
    link_ttl: Decimal = Decimal("15")
    packet_ttl: Decimal = Decimal("10")
    quote_ttl: Decimal = Decimal("3")
    account_ttl: Decimal = Decimal("5")
    position_ttl: Decimal = Decimal("5")
    contract_ttl: Decimal = Decimal("3600")
    future_skew: Decimal = Decimal("2")

    def _validate(self):
        for field in fields(self):
            _decimal(getattr(self, field.name), field.name, positive=field.name != "future_skew")


@dataclass(frozen=True, slots=True, kw_only=True)
class Prices(ContractData):
    open: Decimal | None
    close: Decimal | None

    def _validate(self):
        for name in ("open", "close"):
            if getattr(self, name) is not None:
                _decimal(getattr(self, name), name, positive=True)


@dataclass(frozen=True, slots=True, kw_only=True)
class OrderIntent(ContractData):
    intent_id: str
    source_key: str
    binding: Binding
    action: Action
    volume: int
    prices: Prices
    close_policy: ClosePolicy
    order_type: OrderType
    created_at: datetime
    expires_at: datetime
    schema_version: str = SCHEMA_VERSION
    market: bool = False

    def _validate(self):
        _require(self.schema_version == SCHEMA_VERSION, "unsupported schema_version")
        _identifier(self.intent_id, "intent_id")
        _identifier(self.source_key, "source_key")
        _integer(self.volume, "volume", 1)
        _require(not self.market, "market orders are unsupported")
        _require(self.expires_at > self.created_at, "intent expiry must follow creation")
        if self.action not in (Action.CLOSE_LONG, Action.CLOSE_SHORT):
            _require(self.prices.open is not None, "action requires prices.open")
        if self.action not in (Action.OPEN_LONG, Action.OPEN_SHORT):
            _require(self.prices.close is not None, "action requires prices.close")


@dataclass(frozen=True, slots=True, kw_only=True)
class ValidationResult(ContractData):
    blockers: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.blockers

    def _validate(self):
        for code in self.blockers:
            _identifier(code, "blocker")


@dataclass(frozen=True, slots=True, kw_only=True)
class ComponentTiming(ContractData):
    sampled_at: datetime | None
    source_at: datetime | None
    received_at: datetime | None
    snapshot_id: str | None
    source: Source
    completeness: Completeness

    def _validate(self):
        if self.snapshot_id is not None:
            _identifier(self.snapshot_id, "snapshot_id")


@dataclass(frozen=True, slots=True, kw_only=True)
class SnapshotTimes(ContractData):
    sampled_at: datetime | None
    sent_at: datetime | None
    received_at: datetime | None
    quote: ComponentTiming
    contract: ComponentTiming
    account: ComponentTiming
    positions: ComponentTiming


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceWatermark(ContractData):
    source: Source
    stream_id: str
    value: str
    as_of: datetime

    def _validate(self):
        _identifier(self.stream_id, "stream_id")
        _identifier(self.value, "watermark value")


@dataclass(frozen=True, slots=True, kw_only=True)
class Coverage(ContractData):
    level: CoverageLevel
    account_key: str
    strategy_key: str | None
    session_epoch: str
    trading_day: date
    source: Source
    as_of: datetime | None
    complete_through: SourceWatermark | None
    completion_evidence_id: str | None
    own_active_count: int | None
    account_active_count: int | None

    def _validate(self):
        for name in ("account_key", "strategy_key", "session_epoch", "completion_evidence_id"):
            if getattr(self, name) is not None:
                _identifier(getattr(self, name), name)
        if self.complete_through is not None:
            _require(self.complete_through.source is self.source, "coverage watermark source mismatch")
            _require(self.as_of is not None and self.complete_through.as_of <= self.as_of, "coverage watermark is ahead of evidence")
        if self.level is CoverageLevel.FULL_ACCOUNT:
            _require(self.source is Source.SIMULATION and self.strategy_key is None and
                     self.complete_through is not None and self.completion_evidence_id is not None,
                     "full account coverage requires a simulation completion barrier")
        else:
            _require(self.account_active_count is None, "partial coverage cannot report account active count")
        if self.own_active_count is not None and self.account_active_count is not None:
            _require(self.own_active_count <= self.account_active_count, "own count exceeds account count")


@dataclass(frozen=True, slots=True, kw_only=True)
class OrderInventoryItem(ContractData):
    account_key: str
    trading_day: date
    exchange: str
    instrument_id: str
    hedge_flag: str
    direction: Direction
    offset: Offset
    order_id: int | None
    order_sys_id: str | None
    total_volume: int
    traded_volume: int
    cancel_volume: int
    active: bool

    def _validate(self):
        for name in ("account_key", "exchange", "instrument_id", "hedge_flag", "order_sys_id"):
            if getattr(self, name) is not None:
                _identifier(getattr(self, name), name)
        _integer(self.total_volume, "total_volume", 1)
        _require(self.traded_volume + self.cancel_volume <= self.total_volume, "inventory order volume contradiction")
        _require(self.order_id is not None or self.order_sys_id is not None, "inventory order requires an alias")


@dataclass(frozen=True, slots=True, kw_only=True)
class TradeInventoryItem(ContractData):
    account_key: str
    trading_day: date
    exchange: str
    instrument_id: str
    hedge_flag: str
    trade_id: str
    order_sys_id: str | None
    order_id: int | None
    direction: Direction
    offset: Offset
    price: Decimal
    volume: int

    def _validate(self):
        for name in ("account_key", "exchange", "instrument_id", "hedge_flag", "trade_id", "order_sys_id"):
            if getattr(self, name) is not None:
                _identifier(getattr(self, name), name)
        _decimal(self.price, "price", positive=True)
        _integer(self.volume, "volume", 1)


@dataclass(frozen=True, slots=True, kw_only=True)
class ReconciliationProof(ContractData):
    proof_id: str
    source: Source
    account_key: str
    trading_day: date
    session_epoch: str
    as_of: datetime
    order_inventory: tuple[OrderInventoryItem, ...] | None
    trade_inventory: tuple[TradeInventoryItem, ...] | None
    position_snapshot_id: str | None
    complete_through: SourceWatermark | None
    exclusive_writer_verified: bool
    verified: bool
    covered_event_ids: tuple[str, ...] = ()
    covered_leg_ids: tuple[str, ...] = ()

    def _validate(self):
        for name in ("proof_id", "account_key", "session_epoch", "position_snapshot_id"):
            if getattr(self, name) is not None:
                _identifier(getattr(self, name), name)
        for identity in self.covered_event_ids + self.covered_leg_ids:
            _identifier(identity, "covered identity")
        if self.source is Source.PYTHONGO:
            _require(not self.verified and self.complete_through is None, "native completion barrier is unsupported")
        if self.complete_through is not None:
            _require(self.complete_through.source is self.source and self.complete_through.as_of <= self.as_of,
                     "proof watermark source/time mismatch")
        if self.verified:
            _require(self.source is Source.SIMULATION and self.complete_through is not None and
                     self.exclusive_writer_verified and self.order_inventory is not None and
                     self.trade_inventory is not None and self.position_snapshot_id is not None,
                     "verified proof requires complete simulation inventories and exclusive writer evidence")
        for inventory in (self.order_inventory, self.trade_inventory):
            for item in inventory or ():
                _require(item.account_key == self.account_key and item.trading_day == self.trading_day,
                         "inventory identity differs from proof")
        trade_keys = [(x.exchange, x.instrument_id, x.trade_id) for x in self.trade_inventory or ()]
        _require(len(set(trade_keys)) == len(trade_keys), "duplicate proof trade inventory")


@dataclass(frozen=True, slots=True, kw_only=True)
class Quote(ContractData):
    binding: Binding
    time: datetime | None
    last: Decimal | None
    bid1: Decimal | None
    ask1: Decimal | None
    bid_volume1: int | None
    ask_volume1: int | None
    upper_limit: Decimal | None
    lower_limit: Decimal | None
    market_status: MarketStatus | None

    def _validate(self):
        for name in ("last", "bid1", "ask1", "upper_limit", "lower_limit"):
            if getattr(self, name) is not None:
                _decimal(getattr(self, name), name, positive=True)


@dataclass(frozen=True, slots=True, kw_only=True)
class VolumeLimit(ContractData):
    evidence_id: str
    min_volume: int
    max_volume: int
    volume_step: int
    verified: bool

    def _validate(self):
        _identifier(self.evidence_id, "volume limit evidence")
        for name in ("min_volume", "max_volume", "volume_step"):
            _integer(getattr(self, name), name, 1)
        _require(self.min_volume <= self.max_volume, "inverted volume limit")


@dataclass(frozen=True, slots=True, kw_only=True)
class Contract(ContractData):
    exchange: str | None
    instrument_id: str | None
    asset_class: AssetClass | None
    expiry: date | None
    price_tick: Decimal | None
    multiplier: Decimal | None
    min_limit_order_volume: int | None
    max_limit_order_volume: int | None
    additional_volume_limits: tuple[VolumeLimit, ...] = ()

    def _validate(self):
        for name in ("exchange", "instrument_id"):
            if getattr(self, name) is not None:
                _identifier(getattr(self, name), name)
        for name in ("price_tick", "multiplier"):
            if getattr(self, name) is not None:
                _decimal(getattr(self, name), name, positive=True)
        for name in ("min_limit_order_volume", "max_limit_order_volume"):
            if getattr(self, name) is not None:
                _integer(getattr(self, name), name, 1)
        if self.min_limit_order_volume is not None and self.max_limit_order_volume is not None:
            _require(self.min_limit_order_volume <= self.max_limit_order_volume, "inverted contract volume limits")


@dataclass(frozen=True, slots=True, kw_only=True)
class Account(ContractData):
    binding: Binding
    available: Decimal | None
    currency: str | None

    def _validate(self):
        if self.currency is not None:
            _require(re.fullmatch(r"[A-Z]{3}", self.currency) is not None, "invalid currency")


@dataclass(frozen=True, slots=True, kw_only=True)
class PositionBucket(ContractData):
    binding: Binding
    side: Side
    timing: ComponentTiming
    Position: int | None
    PositionClose: int | None
    FrozenClosing: int | None
    YdPositionClose: int | None
    YdFrozenClosing: int | None

    def _validate(self):
        pairs = ((self.PositionClose, self.Position), (self.YdPositionClose, self.PositionClose),
                 (self.FrozenClosing, self.PositionClose), (self.YdFrozenClosing, self.FrozenClosing),
                 (self.YdFrozenClosing, self.YdPositionClose))
        for smaller, larger in pairs:
            if smaller is not None and larger is not None:
                _require(smaller <= larger, "position hierarchy contradiction")
        if all(x is not None for x in (self.PositionClose, self.YdPositionClose, self.FrozenClosing, self.YdFrozenClosing)):
            _require(self.FrozenClosing - self.YdFrozenClosing <= self.PositionClose - self.YdPositionClose,
                     "today frozen exceeds today closeable")


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionSnapshot(ContractData):
    snapshot_id: str
    binding: Binding
    source: Source
    times: SnapshotTimes
    quote: Quote | None
    contract: Contract | None
    account: Account | None
    positions: tuple[PositionBucket, ...]
    coverage: Coverage
    proof_id: str | None
    ledger_revision: int

    def _validate(self):
        _identifier(self.snapshot_id, "snapshot_id")
        if self.proof_id is not None:
            _identifier(self.proof_id, "proof_id")
        b = self.binding
        cov = self.coverage
        _require((cov.account_key, cov.session_epoch, cov.trading_day, cov.source) ==
                 (b.account_key, b.session_epoch, b.trading_day, self.source), "snapshot coverage scope mismatch")
        _require(cov.strategy_key in (None, b.strategy_key), "coverage strategy mismatch")
        for item in (self.quote, self.account, *self.positions):
            if item is not None:
                _require(item.binding == b, "snapshot component binding mismatch")
        if self.contract is not None:
            _require(self.contract.exchange in (None, b.exchange) and self.contract.instrument_id in (None, b.instrument_id),
                     "snapshot contract identity mismatch")
        for item in (self.times.quote, self.times.contract, self.times.account, self.times.positions,
                     *(x.timing for x in self.positions)):
            _require(item.source is self.source, "snapshot component source mismatch")
        if self.quote is not None:
            _require(self.quote.time == self.times.quote.source_at, "quote.time must be the real source_at")
        keys = [(x.side, x.binding.hedge_flag) for x in self.positions]
        _require(len(keys) == len(set(keys)), "duplicate position bucket")


def _seconds(delta):
    return Decimal(delta.days * 86400 + delta.seconds) + Decimal(delta.microseconds) / Decimal(1_000_000)


def validate_timing(timing: ComponentTiming, now: datetime, ttl: Decimal, *,
                    require_source: bool = False, policy: TimingPolicy = TimingPolicy()) -> ValidationResult:
    _typed(timing, ComponentTiming, "timing")
    now = _utc(now, "now")
    _decimal(ttl, "ttl", positive=True)
    _typed(require_source, bool, "require_source")
    _typed(policy, TimingPolicy, "policy")
    blockers = []
    values = {"sampled_at": timing.sampled_at, "received_at": timing.received_at}
    if require_source or timing.source_at is not None:
        values["source_at"] = timing.source_at
    for name, stamp in values.items():
        if stamp is None:
            blockers.append(f"MISSING_{name.upper()}")
        elif _seconds(now - stamp) > ttl:
            blockers.append(f"STALE_{name.upper()}")
        elif _seconds(stamp - now) > policy.future_skew:
            blockers.append(f"FUTURE_{name.upper()}")
    if timing.source_at is not None and timing.sampled_at is not None:
        if _seconds(timing.source_at - timing.sampled_at) > policy.future_skew:
            blockers.append("SOURCE_AFTER_SAMPLE")
    if timing.sampled_at is not None and timing.received_at is not None:
        if _seconds(timing.sampled_at - timing.received_at) > policy.future_skew:
            blockers.append("SAMPLE_AFTER_RECEIPT")
    return ValidationResult(blockers=tuple(blockers))


BLOCKER_CODES = frozenset({
    "ACCOUNT_MISMATCH", "ACCOUNT_RECONCILIATION_UNSUPPORTED", "AUCTION",
    "AUTHORIZATION_ACTION", "AUTHORIZATION_EXPIRED", "AUTHORIZATION_REQUIRED",
    "BASELINE_GAP", "BOOK_CROSSED", "BOOK_EMPTY", "BUCKET_UNAVAILABLE",
    "CANCEL_NOT_AVAILABLE", "CASH_BOUND_MISSING", "CASH_BOUND_UNVERIFIED", "CANCEL_ROUTE_UNVERIFIED",
    "CANCEL_UNKNOWN", "CONTRACT_EXPIRED", "CONTRACT_MISSING", "EVENT_GAP",
    "EXPIRED", "FEE_AS_RATE_INVALID", "GROSS_POSITION_LIMIT", "HEDGE_UNVERIFIED",
    "INFLIGHT_LIMIT", "INSUFFICIENT_CASH", "INSUFFICIENT_POSITION",
    "INTENT_REQUIRED", "INSTRUMENT_MISMATCH", "INSTRUMENT_UNSUPPORTED", "LEDGER_CORRUPT",
    "LEDGER_FAILURE", "LEDGER_REVISION_MISMATCH", "LINK_STALE", "LIMIT_LOCKED",
    "MARKET_CLOSED", "MARKET_STATUS_UNKNOWN", "MISSING_POSITION_BUCKET",
    "MODE_DISABLED", "NATIVE_DISABLED", "OWNERSHIP_CONFLICT", "POLICY_EXPIRED",
    "POSITION_DATA_INVALID", "PRICE_OUT_OF_BOUNDS", "PROOF_REQUIRED", "QUANTITY_CONFLICT",
    "QUOTE_MISSING", "QUOTE_STALE", "RECONCILE_REQUIRED", "RESERVATION_VIEW_REQUIRED", "RESTART_UNRESOLVED",
    "SESSION_CHANGED", "SNAPSHOT_STALE", "SPREAD_TOO_WIDE", "SUBMIT_UNKNOWN",
    "THREAD_INVALID", "TICK_MALIGNED", "TRADING_DAY_CHANGED",
    "TRADE_GAP", "UNSUPPORTED_CLOSE_POLICY", "VOLUME_INVALID",
    "VOLUME_SPLIT_IMPOSSIBLE", "VOLUME_TOO_SMALL",
})


def _blockers(codes, name):
    for code in codes:
        _require(code in BLOCKER_CODES, f"{name}: undeclared blocker code {code!r}")


@dataclass(frozen=True, slots=True, kw_only=True)
class CashBoundSource(ContractData):
    source: Source
    evidence_id: str
    verified: bool

    def _validate(self):
        _identifier(self.evidence_id, "cash bound evidence")
        if self.verified:
            _require(self.source is Source.SIMULATION, "verified cash bound requires simulation source")


@dataclass(frozen=True, slots=True, kw_only=True)
class RiskPolicy(ContractData):
    policy_id: str
    valid_until: datetime
    max_leg_qty: int | None
    max_gross_position: int | None
    max_inflight_legs: int | None
    max_cash_reserve: Decimal | None
    cash_per_open_lot: Decimal | None
    fee_per_close_lot: Decimal | None
    cash_bound_source: CashBoundSource | None
    timing: TimingPolicy = TimingPolicy()

    def _validate(self):
        _identifier(self.policy_id, "policy_id")
        for name in ("max_leg_qty", "max_gross_position", "max_inflight_legs"):
            if getattr(self, name) is not None:
                _integer(getattr(self, name), name, 1)
        for name in ("max_cash_reserve", "fee_per_close_lot"):
            if getattr(self, name) is not None:
                _decimal(getattr(self, name), name)
        if self.cash_per_open_lot is not None:
            _decimal(self.cash_per_open_lot, "cash_per_open_lot", positive=True)
            _require(self.cash_bound_source is not None and self.cash_bound_source.verified,
                     "cash_per_open_lot requires a verified cash bound source")
        if self.cash_bound_source is not None:
            _require(self.cash_bound_source.source is Source.SIMULATION or not self.cash_bound_source.verified,
                     "native cash bounds are never verified this phase")


@dataclass(frozen=True, slots=True, kw_only=True)
class Leg(ContractData):
    leg_id: str
    index: int
    phase: Phase
    depends_on: tuple[str, ...]
    direction: Direction
    offset: Offset
    volume: int
    price: Decimal
    position_bucket: Bucket | None
    cash_reserve: Decimal

    _DIRECTION_OFFSET = frozenset({
        (Direction.BUY, Offset.OPEN), (Direction.SELL, Offset.OPEN),
        (Direction.SELL, Offset.CLOSE), (Direction.SELL, Offset.CLOSE_TODAY),
        (Direction.SELL, Offset.CLOSE_YESTERDAY), (Direction.BUY, Offset.CLOSE),
        (Direction.BUY, Offset.CLOSE_TODAY), (Direction.BUY, Offset.CLOSE_YESTERDAY),
    })

    def _validate(self):
        _identifier(self.leg_id, "leg_id")
        _integer(self.index, "index")
        _integer(self.volume, "volume", 1)
        _decimal(self.price, "price", positive=True)
        _decimal(self.cash_reserve, "cash_reserve")
        for dep in self.depends_on:
            _identifier(dep, "depends_on")
        _require((self.direction, self.offset) in self._DIRECTION_OFFSET, "direction/offset pair is not executable")
        if self.phase is Phase.OPEN:
            _require(self.offset is Offset.OPEN and self.position_bucket is None, "open leg cannot close a bucket")
        else:
            _require(self.offset is not Offset.OPEN and self.position_bucket is not None, "close leg requires a position bucket")


@dataclass(frozen=True, slots=True, kw_only=True)
class Plan(ContractData):
    plan_id: str
    intent_hash: str
    snapshot_id: str
    ledger_revision: int
    policy_id: str
    expires_at: datetime
    legs: tuple[Leg, ...]
    blockers: tuple[str, ...]

    def _validate(self):
        for name in ("plan_id", "intent_hash"):
            _require(re.fullmatch(r"[0-9a-f]{64}", getattr(self, name)) is not None, f"{name}: expected sha256 hex")
        for name in ("snapshot_id", "policy_id"):
            _identifier(getattr(self, name), name)
        _integer(self.ledger_revision, "ledger_revision")
        if not self.blockers:
            _require(len(self.legs) > 0, "accepted plan requires at least one leg")
        _require([leg.index for leg in self.legs] == list(range(len(self.legs))), "leg indices must be dense and ordered")
        index_of = {leg.leg_id: leg.index for leg in self.legs}
        _require(len(index_of) == len(self.legs), "duplicate leg_id")
        for leg in self.legs:
            for dep in leg.depends_on:
                _require(dep in index_of and index_of[dep] < leg.index, "dependency must reference an earlier leg")
        _blockers(self.blockers, "plan blockers")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReservationKey(ContractData):
    account_key: str
    exchange: str
    instrument_id: str
    hedge_flag: str
    side: Side
    bucket: Bucket

    def _validate(self):
        for name in ("account_key", "exchange", "instrument_id", "hedge_flag"):
            _identifier(getattr(self, name), name)


@dataclass(frozen=True, slots=True, kw_only=True)
class Reservation(ContractData):
    resource_key: ReservationKey
    leg_id: str
    state: ReservationState
    volume: int
    cash_reserve: Decimal
    not_proven_reflected: int = 0
    reflected_evidence_id: str | None = None

    def _validate(self):
        _identifier(self.leg_id, "leg_id")
        if self.reflected_evidence_id is not None:
            _identifier(self.reflected_evidence_id, "reflected evidence")
        _integer(self.volume, "volume", 1)
        _decimal(self.cash_reserve, "cash_reserve")
        _integer(self.not_proven_reflected, "not_proven_reflected")
        _require(self.not_proven_reflected <= self.volume,
                 "unproven reflection cannot exceed the reserved volume")
        if self.state is ReservationState.REFLECTED:
            _require(self.reflected_evidence_id is not None, "reflected reservation requires barrier evidence")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReservationReceipt(ContractData):
    plan_id: str
    reservation_ids: tuple[str, ...]
    revision: int

    def _validate(self):
        _require(re.fullmatch(r"[0-9a-f]{64}", self.plan_id) is not None, "plan_id: expected sha256 hex")
        _require(len(self.reservation_ids) == len(set(self.reservation_ids)) and self.reservation_ids,
                 "reservation ids must be nonempty and unique")
        for item in self.reservation_ids:
            _identifier(item, "reservation_id")
        _integer(self.revision, "revision")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReconcileState(ContractData):
    required: bool = False
    reasons: tuple[ReconcileReason, ...] = ()
    since: datetime | None = None
    evidence_id: str | None = None

    def _validate(self):
        if self.evidence_id is not None:
            _identifier(self.evidence_id, "reconcile evidence")
        if self.required:
            _require(len(self.reasons) > 0 and self.since is not None, "latched reconcile requires reasons and a timestamp")
        else:
            _require(not self.reasons and self.since is None and self.evidence_id is None,
                     "clear reconcile state carries no reasons or evidence")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReservationView(ContractData):
    account_key: str
    snapshot_id: str | None
    ledger_revision: int
    reservations: tuple[Reservation, ...]
    cash_reserved: Decimal
    gross_position: int
    inflight_legs: int
    reconcile: ReconcileState

    def _validate(self):
        _identifier(self.account_key, "account_key")
        if self.snapshot_id is not None:
            _identifier(self.snapshot_id, "snapshot_id")
        _decimal(self.cash_reserved, "cash_reserved")
        _integer(self.gross_position, "gross_position")
        _integer(self.inflight_legs, "inflight_legs")
        _require(len({item.leg_id for item in self.reservations if item.state is not ReservationState.RELEASED}) ==
                 len([item for item in self.reservations if item.state is not ReservationState.RELEASED]),
                 "a leg can hold at most one active reservation")


@dataclass(frozen=True, slots=True, kw_only=True)
class PriceBound(ContractData):
    symbol: Symbol
    lower: Decimal
    upper: Decimal

    def _validate(self):
        _decimal(self.lower, "lower", positive=True)
        _decimal(self.upper, "upper", positive=True)
        _require(self.lower <= self.upper, "inverted price bound")


@dataclass(frozen=True, slots=True, kw_only=True)
class Authorization(ContractData):
    authorization_id: str
    mode: ExecutionMode
    account_key: str
    strategy_key: str
    session_epoch: str
    symbols: tuple[Symbol, ...]
    actions: tuple[AuthorizationAction, ...]
    max_volume: int
    price_bounds: tuple[PriceBound, ...]
    expires_at: datetime

    def _validate(self):
        _require(self.mode is not ExecutionMode.NATIVE, "native authorization is not grantable this phase")
        for name in ("authorization_id", "account_key", "strategy_key", "session_epoch"):
            _identifier(getattr(self, name), name)
        _require(len(self.symbols) > 0, "authorization requires at least one symbol")
        _require(len(self.actions) > 0, "authorization requires at least one action")
        _integer(self.max_volume, "max_volume", 1)
        known = {(item.exchange, item.instrument_id) for item in self.symbols}
        _require(len(known) == len(self.symbols), "duplicate symbol in authorization")
        for bound in self.price_bounds:
            _require((bound.symbol.exchange, bound.symbol.instrument_id) in known,
                     "price bound outside authorized symbols")


@dataclass(frozen=True, slots=True, kw_only=True)
class Readiness(ContractData):
    connection: ConnectionState
    state: ReadinessState
    can_plan: bool
    can_submit: bool
    can_cancel_owned: bool
    blockers: tuple[str, ...]
    mode: ExecutionMode
    coverage: Coverage
    evidence_id: str | None

    def _validate(self):
        _blockers(self.blockers, "readiness blockers")
        _require(self.mode is not ExecutionMode.NATIVE, "native readiness is not unlockable this phase")
        if self.evidence_id is not None:
            _identifier(self.evidence_id, "evidence_id")
        if self.can_submit:
            _require(self.mode is ExecutionMode.SIMULATION and self.state is ReadinessState.EXECUTABLE and
                     not self.blockers and self.coverage.level is CoverageLevel.FULL_ACCOUNT,
                     "can_submit requires simulation mode, EXECUTABLE state, no blockers and full account coverage")
        if self.state is ReadinessState.EXECUTABLE:
            _require(self.mode is ExecutionMode.SIMULATION, "EXECUTABLE is simulation-only this phase")
        if self.state is ReadinessState.RECONCILE_REQUIRED:
            _require(not self.can_submit, "reconcile required blocks submission")


@dataclass(frozen=True, slots=True, kw_only=True)
class ClockReading(ContractData):
    now: datetime
    monotonic_ns: int
    domain_id: str

    def _validate(self):
        _integer(self.monotonic_ns, "monotonic_ns")
        _identifier(self.domain_id, "clock domain")


@dataclass(frozen=True, slots=True, kw_only=True)
class RouteOwnership(ContractData):
    route: str
    evidence_id: str
    verified: bool

    def _validate(self):
        _require(self.route in ("submit", "cancel"), "unknown route")
        _identifier(self.evidence_id, "route evidence")
        if self.verified:
            _require(self.route == "cancel", "only the cancel route can be verified this phase")


@dataclass(frozen=True, slots=True, kw_only=True)
class OwnershipEvidence(ContractData):
    journal_leg_id: str
    matched_alias: str
    callback_account_verified: bool
    strategy_scope_verified: bool
    evidence_id: str

    def _validate(self):
        _require(self.matched_alias in ("local", "exchange"), "unknown alias kind")
        for name in ("journal_leg_id", "evidence_id"):
            _identifier(getattr(self, name), name)


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionEvent(ContractData):
    event_id: str
    kind: EventKind
    received_at: datetime
    callback_session_epoch: str
    account_key: str | None
    ownership: Ownership
    exchange: str | None
    instrument_id: str | None
    hedge_flag: str | None
    order_id: int | None
    order_sys_id: str | None
    front_id: int | None
    session_id: int | None
    memo: str | None
    trade_id: str | None
    order_time: datetime | None
    trade_time: datetime | None
    trading_day: date | None
    status: OrderState | None
    total_volume: int | None
    traded_volume: int | None
    cancel_volume: int | None
    price: Decimal | None
    volume: int | None
    error_code: str | None
    direction: Direction | None
    offset: Offset | None
    ownership_evidence: OwnershipEvidence | None

    def __post_init__(self):
        if self.order_sys_id is not None:
            object.__setattr__(self, "order_sys_id", "".join(self.order_sys_id.split()))
        for name, value in (("trade_id", self.trade_id), ("memo", self.memo), ("error_code", self.error_code)):
            if value is not None:
                object.__setattr__(self, name, value.strip())
        ContractData.__post_init__(self)

    def _validate(self):
        for name in ("event_id", "callback_session_epoch", "account_key", "exchange",
                     "instrument_id", "hedge_flag", "order_sys_id", "trade_id", "memo", "error_code"):
            if getattr(self, name) is not None:
                _identifier(getattr(self, name), name)
        for name in ("order_id", "front_id", "session_id"):
            if getattr(self, name) is not None:
                _integer(getattr(self, name), name)
        # OWNED quantities stay strictly positive; UNKNOWN/FOREIGN keep raw
        # non-negative evidence (including zeros) verbatim for quarantine.
        minimum = 1 if self.ownership is Ownership.OWNED else 0
        for name in ("total_volume", "volume"):
            if getattr(self, name) is not None:
                _integer(getattr(self, name), name, minimum)
        for name in ("traded_volume", "cancel_volume"):
            if getattr(self, name) is not None:
                _integer(getattr(self, name), name)
        if self.price is not None:
            _decimal(self.price, "price", positive=True)
        if self.ownership is Ownership.OWNED:
            _require(self.account_key is not None and self.ownership_evidence is not None and
                     self.ownership_evidence.callback_account_verified and
                     self.ownership_evidence.strategy_scope_verified,
                     "owned event requires verified ownership evidence, not a memo")
            _require(self.trading_day is not None and self.exchange is not None and
                     self.instrument_id is not None,
                     "owned event requires trading_day/exchange/instrument_id identity")
            if None not in (self.total_volume, self.traded_volume, self.cancel_volume):
                _require(self.traded_volume + self.cancel_volume <= self.total_volume,
                         "owned event volume contradiction")
            if self.kind is EventKind.TRADE:
                _require(self.trade_id is not None and self.volume is not None and
                         self.price is not None and self.direction is not None and
                         self.offset is not None, "owned trade event missing trade fields")
            if self.kind in (EventKind.ORDER, EventKind.CANCEL):
                _require(self.order_id is not None or self.order_sys_id is not None,
                         "owned order event requires an order alias")
            if self.kind is EventKind.ERROR:
                _require(self.error_code is not None, "owned error event requires an error code")


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionContext(ContractData):
    mode: ExecutionMode
    binding: Binding
    snapshot: ExecutionSnapshot
    proof: ReconciliationProof | None
    policy: RiskPolicy
    authorization: Authorization | None
    reservation_receipt: ReservationReceipt | None
    owner_thread_id: int
    current_thread_id: int
    clock: ClockReading
    ledger_revision: int
    route_ownership: RouteOwnership | None
    readiness: Readiness
    reconcile: ReconcileState
    intent: OrderIntent | None = None
    reservation_view: ReservationView | None = None

    def _validate(self):
        _require(self.mode is not ExecutionMode.NATIVE,
                 "native mode cannot be unlocked by a payload this phase")
        _integer(self.owner_thread_id, "owner_thread_id")
        _integer(self.current_thread_id, "current_thread_id")
        _require(self.readiness.mode is self.mode, "readiness mode differs from context mode")
        if self.proof is not None:
            _require(self.proof.source is self.snapshot.source, "proof source differs from snapshot source")
            _require(self.proof.account_key == self.binding.account_key, "proof account differs from binding")
        if self.authorization is not None:
            _require(self.authorization.mode is self.mode, "authorization mode differs from context mode")
        if self.reconcile.required:
            _require(self.readiness.state is ReadinessState.RECONCILE_REQUIRED and not self.readiness.can_submit,
                     "latched reconcile must be reflected in readiness")
        if self.intent is not None:
            _require(self.intent.binding == self.binding,
                     "context intent binding differs from the execution scope")
        if self.reservation_view is not None:
            _require(self.reservation_view.account_key == self.binding.account_key,
                     "reservation view account differs from the execution scope")
            _require(self.reservation_view.ledger_revision == self.ledger_revision,
                     "reservation view ledger revision differs from the context")
            if self.reservation_view.snapshot_id is not None:
                _require(self.reservation_view.snapshot_id == self.snapshot.snapshot_id,
                         "reservation view snapshot differs from the context snapshot")


def validate_snapshot_timing(snapshot: ExecutionSnapshot, now: datetime,
                             policy: TimingPolicy = TimingPolicy()) -> ValidationResult:
    """Component freshness; heartbeats never renew quote source_at."""
    _typed(snapshot, ExecutionSnapshot, "snapshot")
    now = _utc(now, "now")
    _typed(policy, TimingPolicy, "policy")
    blockers = []
    times = snapshot.times
    for label, component, ttl in (("QUOTE", times.quote, policy.quote_ttl),
                                  ("ACCOUNT", times.account, policy.account_ttl),
                                  ("POSITION", times.positions, policy.position_ttl),
                                  ("CONTRACT", times.contract, policy.contract_ttl)):
        for blocker in validate_timing(component, now, ttl, require_source=label == "QUOTE",
                                       policy=policy).blockers:
            blockers.append(f"{label}_{blocker}")
    for label, stamp in (("PACKET_SAMPLED", times.sampled_at), ("PACKET_RECEIVED", times.received_at)):
        if stamp is None:
            blockers.append(f"{label}_MISSING")
        elif _seconds(now - stamp) > policy.packet_ttl:
            blockers.append(f"{label}_STALE")
        elif _seconds(stamp - now) > policy.future_skew:
            blockers.append(f"{label}_FUTURE")
    return ValidationResult(blockers=tuple(blockers))


def validate_context(context: ExecutionContext, now: datetime, *,
                     action: AuthorizationAction | None = None,
                     require_attempt: bool = False) -> ValidationResult:
    """Gate checks; owned-cancel is judged on its own route, never on quote TTL,
    full-account proof or can_submit. No payload can unlock native execution."""
    _typed(context, ExecutionContext, "context")
    now = _utc(now, "now")
    if action is not None:
        _typed(action, AuthorizationAction, "action")
    _typed(require_attempt, bool, "require_attempt")
    blockers = []
    if context.mode is ExecutionMode.DISABLED:
        blockers.append("MODE_DISABLED")
    if context.owner_thread_id != context.current_thread_id:
        blockers.append("THREAD_INVALID")
    auth = context.authorization
    if auth is None:
        blockers.append("AUTHORIZATION_REQUIRED")
    else:
        if auth.expires_at <= now:
            blockers.append("AUTHORIZATION_EXPIRED")
        binding = context.binding
        if auth.account_key != binding.account_key:
            blockers.append("ACCOUNT_MISMATCH")
        if auth.session_epoch != binding.session_epoch:
            blockers.append("SESSION_CHANGED")
        if auth.strategy_key != binding.strategy_key:
            blockers.append("OWNERSHIP_CONFLICT")
        if (binding.exchange, binding.instrument_id) not in \
                {(item.exchange, item.instrument_id) for item in auth.symbols}:
            blockers.append("INSTRUMENT_MISMATCH")
        if action is not None and action not in auth.actions:
            blockers.append("AUTHORIZATION_ACTION")
    if action is AuthorizationAction.CANCEL_OWNED:
        if not context.readiness.can_cancel_owned:
            blockers.append("CANCEL_NOT_AVAILABLE")
        route = context.route_ownership
        if route is None or route.route != "cancel" or not route.verified:
            blockers.append("CANCEL_ROUTE_UNVERIFIED")
        if context.reconcile.required and {ReconcileReason.LEDGER_FAILURE, ReconcileReason.LEDGER_CORRUPT} & \
                set(context.reconcile.reasons):
            blockers.append("LEDGER_FAILURE")
        if context.snapshot.ledger_revision != context.ledger_revision:
            blockers.append("LEDGER_REVISION_MISMATCH")
        return ValidationResult(blockers=tuple(dict.fromkeys(blockers)))
    if context.reconcile.required:
        blockers.append("RECONCILE_REQUIRED")
    if context.policy.valid_until <= now:
        blockers.append("POLICY_EXPIRED")
    snapshot = context.snapshot
    if snapshot.ledger_revision != context.ledger_revision:
        blockers.append("LEDGER_REVISION_MISMATCH")
    blockers.extend(validate_snapshot_timing(snapshot, now, context.policy.timing).blockers)
    if require_attempt:
        if context.intent is None:
            blockers.append("INTENT_REQUIRED")
        else:
            if context.intent.expires_at <= now:
                blockers.append("EXPIRED")
            if context.intent.binding != context.binding:
                blockers.append("OWNERSHIP_CONFLICT")
        if context.reservation_view is None:
            blockers.append("RESERVATION_VIEW_REQUIRED")
        elif context.reservation_view.reconcile.required:
            blockers.append("RECONCILE_REQUIRED")
    proof = context.proof
    if proof is None:
        blockers.append("PROOF_REQUIRED")
    elif context.mode is ExecutionMode.SIMULATION and not proof.verified:
        blockers.append("ACCOUNT_RECONCILIATION_UNSUPPORTED")
    if not context.readiness.can_submit:
        blockers.append("MODE_DISABLED" if context.mode is ExecutionMode.DISABLED else "RECONCILE_REQUIRED")
    blockers.extend(context.readiness.blockers)
    return ValidationResult(blockers=tuple(dict.fromkeys(blockers)))


T = TypeVar("T", bound=ContractData)



def _wire(value):
    if isinstance(value, ContractData):
        return {field.name: _wire(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if type(value) is Decimal:
        if value == 0:
            return "0"
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    if type(value) is datetime:
        return _utc(value, "datetime").isoformat(timespec="microseconds").replace("+00:00", "Z")
    if type(value) is date:
        return value.isoformat()
    if type(value) is tuple:
        return [_wire(item) for item in value]
    return value


def encode(value: ContractData, schema_version: str = SCHEMA_VERSION) -> str:
    """Versioned, typed, canonical JSON; UTF-8 hashing uses these exact bytes."""
    _require(isinstance(value, ContractData), "unsupported contract type")
    _require(getattr(type(value), "schema", SCHEMA_VERSION) == schema_version,
             "contract type belongs to a different schema namespace")
    packet = {"schema_version": schema_version, "type": type(value).__name__, "data": _wire(value)}
    text = json.dumps(packet, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    _require(len(text.encode("utf-8")) <= MAX_MESSAGE_BYTES, "message too large")
    return text


def canonical_hash(value: ContractData) -> str:
    return hashlib.sha256(encode(value, getattr(type(value), "schema", SCHEMA_VERSION)).encode("utf-8")).hexdigest()


def _pairs(items):
    result = {}
    for key, value in items:
        _require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def _nonfinite(value):
    raise ContractError("nonfinite JSON number")


def _depth(value, depth=0):
    _require(depth <= MAX_NESTING, "message nesting exceeds limit")
    if type(value) is dict:
        for item in value.values():
            _depth(item, depth + 1)
    elif type(value) is list:
        for item in value:
            _depth(item, depth + 1)


def _from_wire(value, annotation, name):
    origin = get_origin(annotation)
    if origin in (Union, UnionType):
        for choice in get_args(annotation):
            try:
                return _from_wire(value, choice, name)
            except ContractError:
                pass
        raise ContractError(f"{name}: invalid wire union")
    if origin is tuple:
        _require(type(value) is list, f"{name}: expected JSON array")
        return tuple(_from_wire(item, get_args(annotation)[0], name) for item in value)
    if isinstance(annotation, type) and issubclass(annotation, ContractData):
        _require(type(value) is dict, f"{name}: expected JSON object")
        declared = {field.name: field.type for field in fields(annotation)}
        _require(value.keys() == declared.keys(), f"{name}: missing or unexpected fields")
        return annotation(**{key: _from_wire(value[key], kind, key) for key, kind in declared.items()})
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        _require(type(value) is str, f"{name}: expected enum string")
        try:
            return annotation(value)
        except ValueError as exc:
            raise ContractError(f"{name}: unsupported enum") from exc
    if annotation is Decimal:
        _require(type(value) is str and len(value) <= 256 and
                 re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?", value) is not None,
                 f"{name}: expected decimal string")
        try:
            return _typed(Decimal(value), Decimal, name)
        except InvalidOperation as exc:
            raise ContractError(f"{name}: invalid Decimal") from exc
    if annotation is datetime:
        _require(type(value) is str and re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})", value) is not None,
            f"{name}: expected timestamp with timezone")
        try:
            return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")), name)
        except ValueError as exc:
            raise ContractError(f"{name}: invalid datetime") from exc
    if annotation is date:
        _require(type(value) is str and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is not None,
                 f"{name}: expected calendar date")
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ContractError(f"{name}: invalid date") from exc
    return _typed(value, annotation, name)


def decode(payload: str | bytes, expected_type: type[T],
           *, schema_version: str = SCHEMA_VERSION) -> T:
    """Reject unknown fields/versions/types, duplicate keys, NaN and coercions."""
    _require(isinstance(expected_type, type) and issubclass(expected_type, ContractData) and
             expected_type is not ContractData, "unsupported expected type")
    _require(type(payload) in (str, bytes), "expected JSON text or UTF-8 bytes")
    try:
        raw = payload.encode("utf-8") if type(payload) is str else payload
        _require(len(raw) <= MAX_MESSAGE_BYTES, "message too large")
        packet = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_nonfinite)
        _depth(packet)
        _require(type(packet) is dict and packet.keys() == {"schema_version", "type", "data"}, "invalid envelope")
        _require(packet["schema_version"] == schema_version, "unsupported schema_version")
        _require(getattr(expected_type, "schema", SCHEMA_VERSION) == schema_version,
                 "expected type belongs to a different schema namespace")
        _require(packet["type"] == expected_type.__name__, "unexpected envelope type")
        return _from_wire(packet["data"], expected_type, expected_type.__name__)
    except (UnicodeError, ValueError, TypeError, RecursionError, OverflowError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError("invalid execution message") from exc


# Public validation helpers for sibling v2/transport contract modules; the
# private names stay authoritative so v1 behavior is byte-for-byte unchanged.
validate_identifier = _identifier
validate_integer = _integer
validate_decimal = _decimal
validate_utc_datetime = _utc
validate_requirement = _require
