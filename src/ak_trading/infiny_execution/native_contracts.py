"""NE1-C1 v2 core and client-transport contracts (frozen DTOs only).

Pure, immutable, standard-library only. `infiny_execution_v2` core types may be
CONSTRUCTED in native mode to express a blocked state; construction never
grants execution. Real capability flows only from registered evidence
(native_evidence) inside a trusted assembler. No IO, clocks, natives or env.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum

from .contracts import (
    AuthorizationAction,
    Binding,
    BLOCKER_CODES,
    CancelState,
    ContractData,
    ContractError,
    ConnectionState,
    Coverage,
    CoverageLevel,
    EventKind,
    ExecutionMode,
    ExecutionSnapshot,
    Leg,
    OrderState,
    OrderType,
    PriceBound,
    ReadinessState,
    ResultCode,
    Symbol,
    V2_SCHEMA_VERSION,
    canonical_hash,
    decode as _engine_decode,
    encode as _engine_encode,
    validate_decimal,
    validate_identifier,
    validate_integer,
    validate_utc_datetime,
    ValidationResult,
)
import hashlib

TRANSPORT_SCHEMA_VERSION = "infiny_client_execution_transport_v1_1"
LOCAL_SCHEMA_VERSION = "infiny_execution_local_v1"  # C1.4: journal-local records, never wire
MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
MAX_NESTING = 32
MAX_EVENTS_PER_BATCH = 128
MAX_RAW_FIELDS_PER_EVENT = 64
DISPATCH_TTL_MS = 1500  # NE1-ARB-C1.2-OVF06-001: fixed maximum, no override exists
POLL_ARMED_MIN_INTERVAL_MS = 250
POLL_DISABLED_INTERVAL_MS = 5000

# C1.2 frozen dispatch-window policy fragment (mirrors the c1.2-r1 lock).
DISPATCH_WINDOW_RULES = {
    "dispatch_ttl_ms": 1500,
    "original_offer_deadline_immutable": True,
    "command_inherits_offer_deadline": True,
    "ready_write_transactions": 1,
    "post_consumed_fsync_expiry_check": True,
    "refresh_or_reissue": False,
}

# C1.4 (NE1-ARB-C1.4-N4R3-F2-001) frozen offer-deadline diagnostics. They map
# onto the existing blocked/conflict result transport; no new HTTP schema.
OFFER_DEADLINE_REQUIRED = "OFFER_DEADLINE_REQUIRED"
OFFER_DEADLINE_CONFLICT = "OFFER_DEADLINE_CONFLICT"
OFFER_DEADLINE_UNPROVEN = "OFFER_DEADLINE_UNPROVEN"
OFFER_DEADLINE_CLOCK_INVALID = "OFFER_DEADLINE_CLOCK_INVALID"
OFFER_DEADLINE_BLOCKERS = frozenset({
    OFFER_DEADLINE_REQUIRED, OFFER_DEADLINE_CONFLICT,
    OFFER_DEADLINE_UNPROVEN, OFFER_DEADLINE_CLOCK_INVALID,
})

# NE1-ARB-C1.1-001 frozen shared constants (RULINGS 02/04/07/09/10).
HTTP_REQUEST_FINGERPRINT_VERSION = "infiny_http_request_fingerprint_v1"
INITIAL_SERVICE_EPOCH = 1
EVENT_SEQUENCE_START = 1
STATUS_MAX_HEADER_BYTES = 8192
MAX_HTTP_WORKERS = 8
MAX_ACCOUNT_ACTORS = 8
MAX_ARMED_NATIVE_ACCOUNTS = 1
EXECUTION_TOKEN_HEADER = "X-Execution-Token"
PAIRING_TOKEN_BYTES = 32
PAIRING_TOKEN_HEX_LENGTH = 64

# RULING 02: exact GET /status field-to-header mapping; the pairing token is
# authentication only and is deliberately absent from this DTO mapping.
STATUS_REQUEST_HEADERS = {
    "schema_version": "X-Execution-Schema-Version",
    "type": "X-Execution-Type",
    "request_id": "X-Execution-Request-Id",
    "instance_id": "X-Execution-Instance-Id",
    "session_epoch": "X-Execution-Session-Epoch",
    "generation": "X-Execution-Generation",
    "service_epoch": "X-Execution-Service-Epoch",
    "sequence": "X-Execution-Sequence",
    "sent_at": "X-Execution-Sent-At",
    "params_sha256": "X-Execution-Params-Sha256",
}


class ExecutionScope(str, Enum):
    NONE = "none"
    SIMULATION_FULL_ACCOUNT = "simulation_full_account"
    NATIVE_PILOT = "native_pilot"


class Origin(str, Enum):
    PRODUCTION = "production"
    SYNTHETIC = "synthetic"


class CommandKind(str, Enum):
    SUBMIT = "SUBMIT"
    CANCEL = "CANCEL"


class ReceiptStage(str, Enum):
    CONSUMED = "CONSUMED"
    RETURNED = "RETURNED"
    NO_CALL = "NO_CALL"


class ResultKind(str, Enum):
    LOCAL_ACK = "LOCAL_ACK"            # native returned a non-negative int (bool excluded)
    LOCAL_REJECTED = "LOCAL_REJECTED"  # native returned -1
    EXCEPTION = "EXCEPTION"
    TIMEOUT = "TIMEOUT"
    INVALID_RETURN = "INVALID_RETURN"  # None/bool/other negative/undecodable


class ControlKind(str, Enum):
    PARAMS_CONFIRMED = "PARAMS_CONFIRMED"
    GATE_CLOSED = "GATE_CLOSED"
    STOPPED = "STOPPED"
    GENERATION_REPORT = "GENERATION_REPORT"


class ResponseKind(str, Enum):
    IDLE = "IDLE"
    BLOCKED = "BLOCKED"
    OFFER = "OFFER"


class ReadyResponseKind(str, Enum):
    DISPATCH = "DISPATCH"
    BLOCKED = "BLOCKED"


class ControlResponseKind(str, Enum):
    RECORDED = "RECORDED"
    BLOCKED = "BLOCKED"


class ControlPhase(str, Enum):
    DISABLED = "DISABLED"
    ARMED = "ARMED"
    REVOKE_PENDING = "REVOKE_PENDING"
    DISABLED_CONFIRMED = "DISABLED_CONFIRMED"


class TransportErrorCode(str, Enum):
    INVALID_REQUEST = "INVALID_REQUEST"
    UNSUPPORTED_SCHEMA = "UNSUPPORTED_SCHEMA"
    TYPE_MISMATCH = "TYPE_MISMATCH"
    UNSUPPORTED_ACTION = "UNSUPPORTED_ACTION"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    ORIGIN_REJECTED = "ORIGIN_REJECTED"
    HOST_REJECTED = "HOST_REJECTED"
    BINDING_MISMATCH = "BINDING_MISMATCH"
    ACTOR_UNREGISTERED = "ACTOR_UNREGISTERED"
    UNKNOWN_ROUTE = "UNKNOWN_ROUTE"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    REQUEST_TIMEOUT = "REQUEST_TIMEOUT"
    REQUEST_ID_CONFLICT = "REQUEST_ID_CONFLICT"
    SEQUENCE_CONFLICT = "SEQUENCE_CONFLICT"
    EPOCH_MISMATCH = "EPOCH_MISMATCH"
    HANDSHAKE_REQUIRED = "HANDSHAKE_REQUIRED"
    SNAPSHOT_CONFLICT = "SNAPSHOT_CONFLICT"
    ACK_WATERMARK_INVALID = "ACK_WATERMARK_INVALID"
    CONTEXT_CONFLICT = "CONTEXT_CONFLICT"
    RECEIPT_CONFLICT = "RECEIPT_CONFLICT"
    REQUEST_TOO_LARGE = "REQUEST_TOO_LARGE"
    UNSUPPORTED_MEDIA_TYPE = "UNSUPPORTED_MEDIA_TYPE"
    POLL_TOO_FAST = "POLL_TOO_FAST"
    RESPONSE_TOO_LARGE = "RESPONSE_TOO_LARGE"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    BACKPRESSURE = "BACKPRESSURE"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    PERSISTENCE_UNAVAILABLE = "PERSISTENCE_UNAVAILABLE"


# RULING 03: immutable frozen code -> HTTP status mapping.
TRANSPORT_ERROR_HTTP_STATUS = {
    TransportErrorCode.INVALID_REQUEST: 400,
    TransportErrorCode.UNSUPPORTED_SCHEMA: 400,
    TransportErrorCode.TYPE_MISMATCH: 400,
    TransportErrorCode.UNSUPPORTED_ACTION: 400,
    TransportErrorCode.AUTHENTICATION_FAILED: 401,
    TransportErrorCode.ORIGIN_REJECTED: 403,
    TransportErrorCode.HOST_REJECTED: 403,
    TransportErrorCode.BINDING_MISMATCH: 403,
    TransportErrorCode.ACTOR_UNREGISTERED: 403,
    TransportErrorCode.UNKNOWN_ROUTE: 404,
    TransportErrorCode.METHOD_NOT_ALLOWED: 405,
    TransportErrorCode.REQUEST_TIMEOUT: 408,
    TransportErrorCode.REQUEST_ID_CONFLICT: 409,
    TransportErrorCode.SEQUENCE_CONFLICT: 409,
    TransportErrorCode.EPOCH_MISMATCH: 409,
    TransportErrorCode.HANDSHAKE_REQUIRED: 409,
    TransportErrorCode.SNAPSHOT_CONFLICT: 409,
    TransportErrorCode.ACK_WATERMARK_INVALID: 409,
    TransportErrorCode.CONTEXT_CONFLICT: 409,
    TransportErrorCode.RECEIPT_CONFLICT: 409,
    TransportErrorCode.REQUEST_TOO_LARGE: 413,
    TransportErrorCode.UNSUPPORTED_MEDIA_TYPE: 415,
    TransportErrorCode.POLL_TOO_FAST: 429,
    TransportErrorCode.RESPONSE_TOO_LARGE: 500,
    TransportErrorCode.INTERNAL_ERROR: 500,
    TransportErrorCode.BACKPRESSURE: 503,
    TransportErrorCode.SERVICE_UNAVAILABLE: 503,
    TransportErrorCode.PERSISTENCE_UNAVAILABLE: 503,
}


_HEX = frozenset("0123456789abcdef")


def _hash64(value, name):
    if not (type(value) is str and len(value) == 64 and all(ch in _HEX for ch in value)):
        raise ContractError(f"{name}: expected lowercase sha256 hex")
    return value


def _text(value, name, limit=256):
    if not (type(value) is str and 0 < len(value) <= limit and
            not any(ord(ch) < 32 for ch in value)):
        raise ContractError(f"{name}: expected nonempty safe text")
    return value


# ---------------------------------------------------------------------------
# v2 core contracts (schema namespace: infiny_execution_v2)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True, kw_only=True)
class ControlState(ContractData):
    """Persistent CAS record: compare_and_register_context compares the
    canonical_hash of the complete value; control_revision is monotonic per
    control actor and never reordered by client timestamps."""
    schema = V2_SCHEMA_VERSION
    control_revision: int
    binding: Binding
    mode: ExecutionMode
    policy_id: str
    authorization_id: str | None
    native_permit_id: str | None
    params_sha256: str
    generation: str
    updated_at: datetime
    disabled_reason: str | None

    def _validate(self):
        validate_integer(self.control_revision, "control_revision")
        validate_identifier(self.policy_id, "policy_id")
        _hash64(self.params_sha256, "params_sha256")
        validate_identifier(self.generation, "generation")
        validate_utc_datetime(self.updated_at, "updated_at")
        for name in ("authorization_id", "native_permit_id", "disabled_reason"):
            if getattr(self, name) is not None:
                validate_identifier(getattr(self, name), name)
        if self.mode is ExecutionMode.NATIVE:
            if self.native_permit_id is None:
                raise ContractError("native control state must reference a native permit")
        elif self.native_permit_id is not None:
            raise ContractError("non-native control state cannot carry a native permit")


@dataclass(frozen=True, slots=True, kw_only=True)
class V2Readiness(ContractData):
    """v2 readiness: native states may be EXPRESSED, but can_submit derives
    only from a registered active pilot permit. Pilot never upgrades the
    coverage level to FULL_ACCOUNT."""
    schema = V2_SCHEMA_VERSION
    connection: ConnectionState
    state: ReadinessState
    can_plan: bool
    can_submit: bool
    can_cancel_owned: bool
    blockers: tuple[str, ...]
    mode: ExecutionMode
    coverage: Coverage | None
    evidence_id: str | None
    execution_scope: ExecutionScope
    native_permit_id: str | None = None

    def _validate(self):
        for code in self.blockers:
            if code not in BLOCKER_CODES:
                raise ContractError(f"undeclared blocker code {code!r}")
        if self.evidence_id is not None:
            validate_identifier(self.evidence_id, "evidence_id")
        if self.native_permit_id is not None:
            validate_identifier(self.native_permit_id, "native_permit_id")
        if self.execution_scope is ExecutionScope.NATIVE_PILOT:
            if self.mode is not ExecutionMode.NATIVE:
                raise ContractError("native pilot scope requires native mode")
            if self.can_submit:
                if self.native_permit_id is None:
                    raise ContractError("native can_submit derives only from a registered permit")
                if self.state is not ReadinessState.EXECUTABLE or self.blockers:
                    raise ContractError("armed native pilot must be EXECUTABLE with zero blockers")
                if self.coverage is None or self.coverage.level is CoverageLevel.FULL_ACCOUNT:
                    raise ContractError("pilot readiness never claims full account coverage")
        elif self.execution_scope is ExecutionScope.SIMULATION_FULL_ACCOUNT:
            if self.mode is not ExecutionMode.SIMULATION:
                raise ContractError("simulation scope requires simulation mode")
            if self.native_permit_id is not None:
                raise ContractError("simulation readiness cannot carry a native permit")
            if self.can_submit and (self.state is not ReadinessState.EXECUTABLE or self.blockers or
                                    self.coverage is None or
                                    self.coverage.level is not CoverageLevel.FULL_ACCOUNT):
                raise ContractError("simulation can_submit keeps the v1 full-account gate")
        else:
            if self.native_permit_id is not None:
                raise ContractError("scope none cannot carry a native permit")
            if self.can_submit:
                raise ContractError("scope none can never submit")


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeAuthorization(ContractData):
    """v2 native authorization record; send and cancel attempts are counted
    separately and neither is granted implicitly by the other."""
    schema = V2_SCHEMA_VERSION
    authorization_id: str
    binding: Binding
    symbols: tuple[Symbol, ...]
    actions: tuple[AuthorizationAction, ...]
    max_send_attempts: int
    max_cancel_attempts: int
    max_volume: int
    price_bounds: tuple[PriceBound, ...]
    expires_at: datetime
    origin: Origin
    params_sha256: str

    def _validate(self):
        validate_identifier(self.authorization_id, "authorization_id")
        if not self.symbols:
            raise ContractError("authorization requires at least one symbol")
        if not self.actions:
            raise ContractError("authorization requires at least one action")
        validate_integer(self.max_send_attempts, "max_send_attempts", 1)
        validate_integer(self.max_cancel_attempts, "max_cancel_attempts")
        validate_integer(self.max_volume, "max_volume", 1)
        known = {(item.exchange, item.instrument_id) for item in self.symbols}
        if len(known) != len(self.symbols):
            raise ContractError("duplicate symbol in authorization")
        for bound in self.price_bounds:
            if (bound.symbol.exchange, bound.symbol.instrument_id) not in known:
                raise ContractError("price bound outside authorized symbols")
        _hash64(self.params_sha256, "params_sha256")


# ---------------------------------------------------------------------------
# transport bodies (schema namespace: infiny_client_execution_transport_v1)
# Only wire messages inherit TransportMessage; nested bodies stay standalone.
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True, kw_only=True)
class TransportMessage(ContractData):
    """Every transport wire message carries the full envelope header set."""
    schema = TRANSPORT_SCHEMA_VERSION
    request_id: str
    instance_id: str
    session_epoch: str
    generation: str
    service_epoch: int | None
    sequence: int
    sent_at: datetime
    params_sha256: str

    def _validate(self):
        for name in ("request_id", "instance_id", "session_epoch", "generation"):
            validate_identifier(getattr(self, name), name)
        if self.service_epoch is not None:
            validate_integer(self.service_epoch, "service_epoch")
        validate_integer(self.sequence, "sequence")
        validate_utc_datetime(self.sent_at, "sent_at")
        _hash64(self.params_sha256, "params_sha256")


@dataclass(frozen=True, slots=True, kw_only=True)
class _TransportBody(ContractData):
    schema = TRANSPORT_SCHEMA_VERSION


@dataclass(frozen=True, slots=True, kw_only=True)
class EventCursor(_TransportBody):
    """Contiguous event stream position (RULING 09). Sequence 0 is the empty
    prefix (both id and hash null); a positive sequence requires the exact
    event id and its canonical hash. It cannot encode gaps or interval sets."""
    event_stream_id: str
    instance_id: str
    session_epoch: str
    generation: str
    sequence: int
    event_id: str | None
    event_sha256: str | None

    def _validate(self):
        for name in ("event_stream_id", "instance_id", "session_epoch", "generation"):
            validate_identifier(getattr(self, name), name)
        validate_integer(self.sequence, "sequence")
        if self.sequence == 0:
            if self.event_id is not None or self.event_sha256 is not None:
                raise ContractError("the empty prefix cursor carries no event identity")
        elif self.sequence < EVENT_SEQUENCE_START:
            raise ContractError("event sequence cannot be negative")
        else:
            validate_identifier(self.event_id, "event_id")
            _hash64(self.event_sha256, "event_sha256")


@dataclass(frozen=True, slots=True, kw_only=True)
class AckWatermark(EventCursor):
    """Largest contiguous durably-saved event prefix, inclusive (RULING 09)."""
    pass


@dataclass(frozen=True, slots=True, kw_only=True)
class ParameterConfirmation(_TransportBody):
    confirmation_id: str
    binding: Binding
    params_sha256: str
    adapter_sha256: str
    confirmed_at: datetime
    expires_at: datetime
    explicit_ui_action: bool

    def _validate(self):
        validate_identifier(self.confirmation_id, "confirmation_id")
        _hash64(self.params_sha256, "params_sha256")
        _hash64(self.adapter_sha256, "adapter_sha256")
        validate_utc_datetime(self.confirmed_at, "confirmed_at")
        validate_utc_datetime(self.expires_at, "expires_at")
        if self.expires_at <= self.confirmed_at:
            raise ContractError("confirmation expiry must follow confirmation")
        if self.explicit_ui_action is not True:
            raise ContractError("restored parameters never issue a confirmation")


@dataclass(frozen=True, slots=True, kw_only=True)
class NativePilotPermit(_TransportBody):
    """Strictly bounded manual-takeover permit; production instances can only
    be registered by the trusted evidence verifier, never by HTTP payloads."""
    permit_id: str
    authorization_id: str
    binding: Binding
    control_revision: int
    baseline_evidence_id: str
    route_evidence_id: str
    cash_bound_evidence_id: str
    source_key: str
    actions: tuple[AuthorizationAction, ...]
    max_send_attempts: int
    max_cancel_attempts: int
    max_volume: int
    price_bounds: tuple[PriceBound, ...]
    expires_at: datetime
    params_sha256: str
    origin: Origin = Origin.PRODUCTION

    def _validate(self):
        validate_identifier(self.permit_id, "permit_id")
        validate_identifier(self.authorization_id, "authorization_id")
        validate_integer(self.control_revision, "control_revision")
        for name in ("baseline_evidence_id", "route_evidence_id", "cash_bound_evidence_id",
                     "source_key"):
            validate_identifier(getattr(self, name), name)
        if not self.actions:
            raise ContractError("permit requires at least one action")
        validate_integer(self.max_send_attempts, "max_send_attempts", 1)
        validate_integer(self.max_cancel_attempts, "max_cancel_attempts")
        validate_integer(self.max_volume, "max_volume", 1)
        permitted = {(self.binding.exchange, self.binding.instrument_id)}
        for bound in self.price_bounds:
            if (bound.symbol.exchange, bound.symbol.instrument_id) not in permitted:
                raise ContractError("price bound outside the permitted binding")
        validate_utc_datetime(self.expires_at, "expires_at")
        _hash64(self.params_sha256, "params_sha256")


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionOffer(_TransportBody):
    """Plan/leg pointer WITHOUT any native call right."""
    offer_id: str
    intent_id: str
    leg_id: str
    plan_id: str
    intent_hash: str
    leg_hash: str
    binding: Binding
    snapshot_id: str
    policy_id: str
    params_sha256: str
    control_revision: int
    expires_at: datetime

    def _validate(self):
        for name in ("offer_id", "intent_id", "leg_id", "snapshot_id", "policy_id"):
            validate_identifier(getattr(self, name), name)
        for name in ("plan_id", "intent_hash", "leg_hash"):
            _hash64(getattr(self, name), name)
        _hash64(self.params_sha256, "params_sha256")
        validate_integer(self.control_revision, "control_revision")
        validate_utc_datetime(self.expires_at, "expires_at")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReadyChallenge(_TransportBody):
    """Client-side re-verification of an offer; a claim, never proof of right."""
    offer_id: str
    capture_id: str
    thread_evidence_id: str
    latest_params_sha256: str
    enable_trading: bool
    unconfirmed_event_watermark: EventCursor | None
    generation: str

    def _validate(self):
        for name in ("offer_id", "capture_id", "thread_evidence_id"):
            validate_identifier(getattr(self, name), name)
        _hash64(self.latest_params_sha256, "latest_params_sha256")
        validate_identifier(self.generation, "challenge generation")


@dataclass(frozen=True, slots=True, kw_only=True)
class ContractLimit(_TransportBody):
    min_volume: int
    max_volume: int
    volume_step: int

    def _validate(self):
        for name in ("min_volume", "max_volume", "volume_step"):
            validate_integer(getattr(self, name), name, 1)
        if self.min_volume > self.max_volume:
            raise ContractError("inverted contract limits")


@dataclass(frozen=True, slots=True, kw_only=True)
class DispatchCommand(_TransportBody):
    """The single atomically latched delivery ticket; validity never exceeds
    the frozen 500 ms TTL and is never refreshed."""
    command_id: str
    kind: CommandKind
    attempt_id: str
    offer_id: str
    intent_id: str
    leg_id: str
    plan_id: str
    binding: Binding
    leg: Leg
    order_type: OrderType
    price_tick: Decimal
    contract_limits: ContractLimit
    control_revision: int
    permit_id: str
    params_sha256: str
    issued_at: datetime
    expires_at: datetime
    order_id: int | None = None
    route_evidence_id: str | None = None
    cancel_id: str | None = None

    def _validate(self):
        for name in ("command_id", "attempt_id", "offer_id", "intent_id", "leg_id", "permit_id"):
            validate_identifier(getattr(self, name), name)
        _hash64(self.plan_id, "plan_id")
        _hash64(self.params_sha256, "params_sha256")
        validate_integer(self.control_revision, "control_revision")
        validate_utc_datetime(self.issued_at, "issued_at")
        validate_utc_datetime(self.expires_at, "expires_at")
        if self.expires_at <= self.issued_at:
            raise ContractError("dispatch validity must be a positive window")
        validate_decimal(self.price_tick, "price_tick", positive=True)
        if (self.expires_at - self.issued_at).total_seconds() * 1000 > DISPATCH_TTL_MS:
            raise ContractError("dispatch validity exceeds the frozen 500 ms TTL")
        if self.leg.price % self.price_tick != 0:
            raise ContractError("leg price is not on the tick grid")
        if self.kind is CommandKind.CANCEL:
            if self.order_id is None or self.route_evidence_id is None or self.cancel_id is None:
                raise ContractError(
                    "cancel command requires journal order_id, route evidence and cancel_id")
            if self.order_id is not None:
                validate_integer(self.order_id, "order_id")
        else:
            if self.order_id is not None or self.route_evidence_id is not None or self.cancel_id is not None:
                raise ContractError("submit command cannot carry cancel keys")


@dataclass(frozen=True, slots=True, kw_only=True)
class DispatchReceipt(_TransportBody):
    """Local call-boundary receipt; NO_CALL never fabricates a native -1."""
    receipt_id: str
    command_id: str
    attempt_id: str
    binding: Binding
    generation: str
    stage: ReceiptStage
    local_sequence: int
    recorded_at: datetime
    result_kind: ResultKind | None
    result_value: int | None
    error_code: str | None
    request_sha256: str
    previous_receipt_sha256: str | None

    def _validate(self):
        for name in ("receipt_id", "command_id", "attempt_id", "generation"):
            validate_identifier(getattr(self, name), name)
        validate_integer(self.local_sequence, "local_sequence", 1)
        validate_utc_datetime(self.recorded_at, "recorded_at")
        _hash64(self.request_sha256, "request_sha256")
        if self.previous_receipt_sha256 is not None:
            _hash64(self.previous_receipt_sha256, "previous_receipt_sha256")
        if self.error_code is not None:
            validate_identifier(self.error_code, "error_code")
        if self.stage is ReceiptStage.NO_CALL:
            if self.result_kind is not None or self.result_value is not None:
                raise ContractError("NO_CALL carries no native result")
        elif self.result_kind is None:
            raise ContractError("called stages require a result kind")
        if self.result_kind is ResultKind.LOCAL_ACK:
            if self.result_value is None or self.result_value < 0:
                raise ContractError("LOCAL_ACK requires a non-negative int (bool excluded)")
        elif self.result_kind is ResultKind.LOCAL_REJECTED:
            if self.result_value is not None:
                raise ContractError("LOCAL_REJECTED never fabricates -1 as a value")
        elif self.result_kind is not None and self.error_code is None:
            raise ContractError(f"{self.result_kind.value} requires a safe error code")


@dataclass(frozen=True, slots=True, kw_only=True)
class RawCallbackField(_TransportBody):
    """One whitelisted raw callback key/value; the value stays verbatim text."""
    key: str
    value: str

    def _validate(self):
        validate_identifier(self.key, "raw field key")
        if not (type(self.value) is str and 0 < len(self.value) <= 256 and
                not any(ord(ch) < 32 for ch in self.value)):
            raise ContractError("raw field value: expected nonempty safe text")


@dataclass(frozen=True, slots=True, kw_only=True)
class RawCallbackEnvelope(_TransportBody):
    """Raw callback copy; only reconcile whitelist keys survive (N3), values
    are kept verbatim as text. The stream id is allocated and persisted in the
    client outbox, uniquely bound to (instance, session, generation); restarts
    never renumber it. Sequence allocation and persistence are atomic."""
    event_id: str
    kind: EventKind
    event_stream_id: str
    event_sequence: int
    captured_at: datetime
    callback_generation: str
    callback_session_epoch: str
    route_evidence_id: str | None
    raw_fields: tuple[RawCallbackField, ...]

    def _validate(self):
        validate_identifier(self.event_id, "event_id")
        validate_identifier(self.event_stream_id, "event_stream_id")
        validate_integer(self.event_sequence, "event_sequence", EVENT_SEQUENCE_START)
        validate_utc_datetime(self.captured_at, "captured_at")
        validate_identifier(self.callback_generation, "callback_generation")
        validate_identifier(self.callback_session_epoch, "callback_session_epoch")
        if self.route_evidence_id is not None:
            validate_identifier(self.route_evidence_id, "route_evidence_id")
        if not 1 <= len(self.raw_fields) <= MAX_RAW_FIELDS_PER_EVENT:
            raise ContractError("raw fields per event are bounded")


# ---------------------------------------------------------------------------
# transport wire messages (header + body)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True, kw_only=True)
class PollRequest(TransportMessage):
    snapshot: ExecutionSnapshot | None = None
    snapshot_id: str | None = None
    client_state_digest: str | None = None
    ack_watermark: AckWatermark | None = None

    def _validate(self):
        TransportMessage._validate(self)
        if self.client_state_digest is not None:
            _hash64(self.client_state_digest, "client_state_digest")
        if self.snapshot is None:
            if self.snapshot_id is not None:
                raise ContractError("snapshot_id requires the by-value snapshot (RULING 06)")
        else:
            if self.snapshot_id != self.snapshot.snapshot_id:
                raise ContractError("snapshot_id must equal the embedded snapshot id")
            times = self.snapshot.times
            components = (times, times.quote, times.contract, times.account, times.positions,
                          *(bucket.timing for bucket in self.snapshot.positions))
            for component in components:
                if component.received_at is not None:
                    raise ContractError(
                        "client captures must leave received_at null; the service stamps receipt")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReadyRequest(TransportMessage):
    offer_id: str
    challenge: ReadyChallenge

    def _validate(self):
        TransportMessage._validate(self)
        validate_identifier(self.offer_id, "offer_id")
        if self.challenge.offer_id != self.offer_id:
            raise ContractError("challenge must reference the same offer")


@dataclass(frozen=True, slots=True, kw_only=True)
class ResultsRequest(TransportMessage):
    receipt: DispatchReceipt


@dataclass(frozen=True, slots=True, kw_only=True)
class EventBatch(TransportMessage):
    events: tuple[RawCallbackEnvelope, ...]

    def _validate(self):
        TransportMessage._validate(self)
        if not 1 <= len(self.events) <= MAX_EVENTS_PER_BATCH:
            raise ContractError("event batch size is bounded")
        seen_ids = set()
        seen_sequences = set()
        stream = self.events[0].event_stream_id
        session = self.events[0].callback_session_epoch
        generation = self.events[0].callback_generation
        for item in self.events:
            if item.event_id in seen_ids:
                raise ContractError("duplicate event id inside one batch")
            if item.event_sequence in seen_sequences:
                raise ContractError("duplicate event sequence inside one batch")
            if (item.event_stream_id != stream or item.callback_session_epoch != session or
                    item.callback_generation != generation):
                raise ContractError("one batch carries exactly one stream, session and generation")
            seen_ids.add(item.event_id)
            seen_sequences.add(item.event_sequence)


@dataclass(frozen=True, slots=True, kw_only=True)
class ControlReport(TransportMessage):
    kind: ControlKind
    confirmation: ParameterConfirmation | None = None

    def _validate(self):
        TransportMessage._validate(self)
        if self.kind is ControlKind.PARAMS_CONFIRMED and self.confirmation is None:
            raise ContractError("PARAMS_CONFIRMED must carry the confirmation")
        if self.kind is not ControlKind.PARAMS_CONFIRMED and self.confirmation is not None:
            raise ContractError("only PARAMS_CONFIRMED carries a confirmation")


@dataclass(frozen=True, slots=True, kw_only=True)
class StatusRequest(TransportMessage):
    pass


@dataclass(frozen=True, slots=True, kw_only=True)
class CallCount(_TransportBody):
    """Exact or bounded count (RULING 01). Exact: value == lower_bound == a
    present upper_bound. Uncertain: value=null with a justified lower bound;
    upper_bound=null when no bound can be established."""
    value: int | None
    lower_bound: int
    upper_bound: int | None

    def _validate(self):
        validate_integer(self.lower_bound, "lower_bound")
        if self.value is not None:
            validate_integer(self.value, "value")
            if self.value != self.lower_bound:
                raise ContractError("an exact count requires value == lower_bound")
            if self.upper_bound is not None and self.upper_bound != self.value:
                raise ContractError("an exact count requires value == upper_bound")
        if self.upper_bound is not None:
            validate_integer(self.upper_bound, "upper_bound")
            if self.upper_bound < self.lower_bound:
                raise ContractError("count bounds must satisfy lower <= upper")


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionCounters(_TransportBody):
    """Cumulative scope counters (RULING 01 / plan 13.3 / H39). Receipt counts
    are service facts, not native-entry counts; real and fake are never added;
    count_complete requires exact real counters plus reconciled evidence."""
    committed_dispatch_count: int
    consumed_receipt_count: int
    returned_receipt_count: int
    no_call_receipt_count: int
    native_send_attempts: CallCount
    native_cancel_attempts: CallCount
    local_order_ids: tuple[int, ...]
    broker_order_acks: int | None
    unique_trade_count: int | None
    fake_send_count: int | None
    fake_cancel_count: int | None
    unresolved_dispatch_count: int
    count_complete: bool

    def _validate(self):
        for name in ("committed_dispatch_count", "consumed_receipt_count",
                     "returned_receipt_count", "no_call_receipt_count",
                     "unresolved_dispatch_count"):
            validate_integer(getattr(self, name), name)
        if len(set(self.local_order_ids)) != len(self.local_order_ids):
            raise ContractError("local order ids must be unique")
        if list(self.local_order_ids) != sorted(self.local_order_ids):
            raise ContractError("local order ids must be sorted")
        for order_id in self.local_order_ids:
            validate_integer(order_id, "local order id")
        for name in ("broker_order_acks", "unique_trade_count",
                     "fake_send_count", "fake_cancel_count"):
            if getattr(self, name) is not None:
                validate_integer(getattr(self, name), name)
        if self.count_complete:
            if (self.native_send_attempts.value is None or
                    self.native_cancel_attempts.value is None):
                raise ContractError("count_complete requires exact attempt counts")
            for name in ("broker_order_acks", "unique_trade_count"):
                if getattr(self, name) is None:
                    raise ContractError(f"count_complete requires {name}")


@dataclass(frozen=True, slots=True, kw_only=True)
class SubmitProjection(_TransportBody):
    """Serializable projection of the N3 SubmitResult {code, leg, revision}
    seam (RULING 01); N3 converts its LegRecord into this transport view."""
    code: ResultCode
    leg_id: str
    leg_state: OrderState
    attempt_no: int
    order_id: int | None
    reported_filled: int
    trade_filled: int
    risk_filled: int
    trade_gap: int
    cancel_state: CancelState
    revision: int

    def _validate(self):
        validate_identifier(self.leg_id, "leg_id")
        validate_integer(self.attempt_no, "attempt_no", 1)
        if self.order_id is not None:
            validate_integer(self.order_id, "order_id")
        for name in ("reported_filled", "trade_filled", "risk_filled", "trade_gap"):
            validate_integer(getattr(self, name), name)
        validate_integer(self.revision, "revision")


@dataclass(frozen=True, slots=True, kw_only=True)
class TransportResponse(ContractData):
    """Common fields of all normal responses (RULING 01). Correlation values
    copy the validated request exactly; sent_at/received_at are server times;
    the binding is the authenticated registered scope."""
    schema = TRANSPORT_SCHEMA_VERSION
    request_id: str
    instance_id: str
    session_epoch: str
    generation: str
    sequence: int
    params_sha256: str
    service_epoch: int
    request_service_epoch: int | None
    sent_at: datetime
    received_at: datetime
    binding: Binding
    ledger_revision: int
    control_revision: int

    def _validate(self):
        for name in ("request_id", "instance_id", "session_epoch", "generation"):
            validate_identifier(getattr(self, name), name)
        validate_integer(self.sequence, "sequence")
        _hash64(self.params_sha256, "params_sha256")
        validate_integer(self.service_epoch, "service_epoch", INITIAL_SERVICE_EPOCH)
        if self.request_service_epoch is not None:
            validate_integer(self.request_service_epoch, "request_service_epoch",
                             INITIAL_SERVICE_EPOCH)
        validate_utc_datetime(self.sent_at, "sent_at")
        validate_utc_datetime(self.received_at, "received_at")
        validate_integer(self.ledger_revision, "ledger_revision")
        validate_integer(self.control_revision, "control_revision")


def _check_readiness(blockers, readiness):
    if not isinstance(readiness, V2Readiness):
        raise ContractError("expected V2Readiness")
    for code in blockers:
        validate_identifier(code, "blocker")
    if tuple(readiness.blockers) != tuple(blockers):
        raise ContractError("blockers must equal readiness.blockers")
    return readiness


@dataclass(frozen=True, slots=True, kw_only=True)
class PollResponse(TransportResponse):
    kind: ResponseKind
    offer: ExecutionOffer | None
    readiness: V2Readiness
    blockers: tuple[str, ...]

    def _validate(self):
        TransportResponse._validate(self)
        _check_readiness(self.blockers, self.readiness)
        if self.kind is ResponseKind.OFFER:
            if self.offer is None or self.blockers:
                raise ContractError("OFFER carries exactly one offer and no blockers")
            if self.offer.binding != self.binding:
                raise ContractError("offer binding differs from the response scope")
        elif self.kind is ResponseKind.IDLE:
            if self.offer is not None or self.blockers:
                raise ContractError("IDLE carries no offer and no blockers")
        else:
            if self.offer is not None or not self.blockers:
                raise ContractError("BLOCKED carries no offer and nonempty blockers")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReadyResponse(TransportResponse):
    kind: ReadyResponseKind
    dispatch: DispatchCommand | None
    readiness: V2Readiness
    blockers: tuple[str, ...]

    def _validate(self):
        TransportResponse._validate(self)
        _check_readiness(self.blockers, self.readiness)
        if self.kind is ReadyResponseKind.DISPATCH:
            if self.dispatch is None or self.blockers:
                raise ContractError("DISPATCH carries exactly one committed command and no blockers")
            if self.dispatch.binding != self.binding:
                raise ContractError("dispatch binding differs from the response scope")
        else:
            if self.dispatch is not None or not self.blockers:
                raise ContractError("BLOCKED carries no command and nonempty blockers")


@dataclass(frozen=True, slots=True, kw_only=True)
class ResultsResponse(TransportResponse):
    receipt_id: str
    command_id: str
    attempt_id: str
    receipt_sha256: str
    persisted: bool
    duplicate: bool
    submit_result: SubmitProjection

    def _validate(self):
        TransportResponse._validate(self)
        for name in ("receipt_id", "command_id", "attempt_id"):
            validate_identifier(getattr(self, name), name)
        _hash64(self.receipt_sha256, "receipt_sha256")
        if self.persisted is not True:
            raise ContractError("a 200 result response certifies durable receipt handling")


@dataclass(frozen=True, slots=True, kw_only=True)
class EventsResponse(TransportResponse):
    event_stream_id: str
    accepted_event_ids: tuple[str, ...]
    duplicate_event_ids: tuple[str, ...]
    persisted: bool
    ack_watermark: AckWatermark

    def _validate(self):
        TransportResponse._validate(self)
        validate_identifier(self.event_stream_id, "event_stream_id")
        if not self.accepted_event_ids:
            raise ContractError("accepted ids mirror the batch in request order")
        if len(set(self.accepted_event_ids)) != len(self.accepted_event_ids):
            raise ContractError("accepted ids are unique")
        for event_id in self.accepted_event_ids:
            validate_identifier(event_id, "accepted event id")
        if len(set(self.duplicate_event_ids)) != len(self.duplicate_event_ids):
            raise ContractError("duplicate ids are unique")
        for event_id in self.duplicate_event_ids:
            validate_identifier(event_id, "duplicate event id")
            if event_id not in self.accepted_event_ids:
                raise ContractError("duplicates are a subset of accepted ids")
        if self.persisted is not True:
            raise ContractError("an ACK is only emitted after N3 durability")
        if self.ack_watermark.event_stream_id != self.event_stream_id:
            raise ContractError("ACK watermark names a different stream")


@dataclass(frozen=True, slots=True, kw_only=True)
class ControlResponse(TransportResponse):
    kind: ControlResponseKind
    report_kind: ControlKind
    confirmation_id: str | None
    control_phase: ControlPhase
    readiness: V2Readiness
    blockers: tuple[str, ...]

    def _validate(self):
        TransportResponse._validate(self)
        _check_readiness(self.blockers, self.readiness)
        if self.report_kind is ControlKind.PARAMS_CONFIRMED:
            if self.confirmation_id is None:
                raise ContractError("PARAMS_CONFIRMED echoes its confirmation id")
            validate_identifier(self.confirmation_id, "confirmation_id")
        elif self.confirmation_id is not None:
            raise ContractError("only PARAMS_CONFIRMED echoes a confirmation id")
        if self.kind is ControlResponseKind.BLOCKED and not self.blockers:
            raise ContractError("a blocked transition carries nonempty blockers")


@dataclass(frozen=True, slots=True, kw_only=True)
class StatusResponse(TransportResponse):
    control_phase: ControlPhase
    readiness: V2Readiness
    blockers: tuple[str, ...]
    counters: ExecutionCounters
    pending_reconcile_count: int
    ack_watermark: AckWatermark | None

    def _validate(self):
        TransportResponse._validate(self)
        _check_readiness(self.blockers, self.readiness)
        validate_integer(self.pending_reconcile_count, "pending_reconcile_count")


@dataclass(frozen=True, slots=True, kw_only=True)
class TransportErrorResponse(ContractData):
    """Typed transport error (RULING 03). Correlation/scope fields and
    revisions are nullable until authenticated validated resolution; the
    server service_epoch and both timestamps are always required. No message,
    URL or raw exception text exists in this contract."""
    schema = TRANSPORT_SCHEMA_VERSION
    request_id: str | None
    instance_id: str | None
    session_epoch: str | None
    generation: str | None
    sequence: int | None
    params_sha256: str | None
    service_epoch: int
    request_service_epoch: int | None
    sent_at: datetime
    received_at: datetime
    binding: Binding | None
    ledger_revision: int | None
    control_revision: int | None
    error_code: TransportErrorCode
    http_status: int
    retry_after_ms: int | None

    def _validate(self):
        for name in ("request_id", "instance_id", "session_epoch", "generation"):
            if getattr(self, name) is not None:
                validate_identifier(getattr(self, name), name)
        if self.sequence is not None:
            validate_integer(self.sequence, "sequence")
        if self.params_sha256 is not None:
            _hash64(self.params_sha256, "params_sha256")
        validate_integer(self.service_epoch, "service_epoch", INITIAL_SERVICE_EPOCH)
        if self.request_service_epoch is not None:
            validate_integer(self.request_service_epoch, "request_service_epoch",
                             INITIAL_SERVICE_EPOCH)
        validate_utc_datetime(self.sent_at, "sent_at")
        validate_utc_datetime(self.received_at, "received_at")
        if self.ledger_revision is not None:
            validate_integer(self.ledger_revision, "ledger_revision")
        if self.control_revision is not None:
            validate_integer(self.control_revision, "control_revision")
        if TRANSPORT_ERROR_HTTP_STATUS.get(self.error_code) != self.http_status:
            raise ContractError("http_status must equal the frozen mapping for the code")
        if self.retry_after_ms is not None:
            validate_integer(self.retry_after_ms, "retry_after_ms", 1)


def request_fingerprint(method: str, route_path: str, request: ContractData) -> str:
    """HTTP replay fingerprint (RULING 01): SHA-256 over UTF-8 of
    version + "\\n" + METHOD + "\\n" + route + "\\n" + encode(request).
    Covers every typed header/payload field; excludes token/conn headers."""
    if not (type(method) is str and method.isalpha() and method.isupper() and method):
        raise ContractError("method must be an uppercase HTTP token")
    if not (type(route_path) is str and route_path.startswith("/") and
            "?" not in route_path and "#" not in route_path and "//" not in route_path[:2]):
        raise ContractError("route_path must be an exact path without query or fragment")
    if not isinstance(request, ContractData):
        raise ContractError("request must be a contract record")
    payload = (HTTP_REQUEST_FINGERPRINT_VERSION + "\n" + method + "\n" + route_path +
               "\n" + encode(request))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# C1.2 dispatch-window policy (NE1-ARB-C1.2-OVF06-001, DTTL-01..08)
# ---------------------------------------------------------------------------

def _milliseconds(delta: timedelta) -> Decimal:
    return (Decimal(delta.days * 86400 + delta.seconds) * 1000 +
            Decimal(delta.microseconds) / Decimal(1000))


def compute_offer_deadline(t_offer: datetime, original_deadlines) -> datetime:
    """DTTL-02: offer.expires_at = min(t_offer + DISPATCH_TTL_MS, D_original),
    computed once; a repeated poll/ready never yields a later expiry."""
    t_offer = validate_utc_datetime(t_offer, "t_offer")
    deadlines = list(original_deadlines)
    if not deadlines:
        raise ContractError("at least one original deadline (authorization, permit, "
                            "intent, policy or data) is required")
    deadline = min(validate_utc_datetime(item, "original deadline") for item in deadlines)
    cap = t_offer + timedelta(milliseconds=DISPATCH_TTL_MS)
    return min(cap, deadline)


def compute_command_deadline(offer_expires_at: datetime, t_issue: datetime,
                             current_deadlines) -> datetime:
    """DTTL-03: command.expires_at = min(offer.expires_at, t_issue + TTL, D_current).
    The command inherits the offer's absolute deadline; /ready grants nothing."""
    offer_expires_at = validate_utc_datetime(offer_expires_at, "offer expires_at")
    t_issue = validate_utc_datetime(t_issue, "t_issue")
    deadlines = list(current_deadlines)
    if not deadlines:
        raise ContractError("at least one current deadline is required")
    deadline = min(validate_utc_datetime(item, "current deadline") for item in deadlines)
    return min(offer_expires_at, t_issue + timedelta(milliseconds=DISPATCH_TTL_MS), deadline)


def validate_dispatch_admission(command: DispatchCommand, offer_expires_at: datetime,
                                *, now: datetime) -> ValidationResult:
    """DTTL-01/03/05 admission diagnostics: requires
    issued_at <= now < expires_at (equality with expiry is expired), the
    encoded lifetime within the frozen cap, and a command that never outlives
    its offer. Pure check; the client repeats it after CONSUMED fsync."""
    if not isinstance(command, DispatchCommand):
        raise ContractError("expected DispatchCommand")
    offer_expires_at = validate_utc_datetime(offer_expires_at, "offer expires_at")
    now = validate_utc_datetime(now, "now")
    blockers = []
    if command.expires_at > offer_expires_at:
        blockers.append("COMMAND_OUTLIVES_OFFER")
    lifetime = _milliseconds(command.expires_at - command.issued_at)
    if lifetime <= 0:
        blockers.append("NONPOSITIVE_WINDOW")
    elif lifetime > DISPATCH_TTL_MS:
        blockers.append("TTL_EXCEEDED")
    if now < command.issued_at:
        blockers.append("NOT_YET_ISSUED")
    if now >= command.expires_at:
        blockers.append("EXPIRED")
    return ValidationResult(blockers=tuple(blockers))


# ---------------------------------------------------------------------------
# C1.4 durable first-offer deadline (NE1-ARB-C1.4-N4R3-F2-001, OFD-01..07)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True, kw_only=True)
class OfferDeadlineRecord(ContractData):
    """OFD-02: immutable first-offer deadline record, local journal data only.
    Lives in its own schema namespace so no transport decoder accepts it as an
    HTTP payload and no v1/v2 envelope re-encodes it."""
    schema = LOCAL_SCHEMA_VERSION
    offer: ExecutionOffer
    first_issued_at: datetime
    kind: CommandKind
    cancel_id: str | None = None

    def _validate(self):
        if not isinstance(self.offer, ExecutionOffer):
            raise ContractError("expected an ExecutionOffer")
        validate_utc_datetime(self.first_issued_at, "first_issued_at")
        if not isinstance(self.kind, CommandKind):
            raise ContractError("expected a CommandKind")
        lifetime = _milliseconds(self.offer.expires_at - self.first_issued_at)
        if lifetime <= 0:
            raise ContractError("offer must expire strictly after first issuance")
        if lifetime > DISPATCH_TTL_MS:
            raise ContractError("first-offer deadline exceeds the frozen 1500 ms budget")
        if self.kind is CommandKind.CANCEL:
            if self.cancel_id is None:
                raise ContractError("owned CANCEL requires its journal cancel identity")
            validate_identifier(self.cancel_id, "cancel_id")
        elif self.cancel_id is not None:
            raise ContractError("SUBMIT records carry no cancel identity")


def encode_local(value: ContractData) -> str:
    return _engine_encode(value, LOCAL_SCHEMA_VERSION)


def decode_local(payload, expected_type: type) -> object:
    return _engine_decode(payload, expected_type, schema_version=LOCAL_SCHEMA_VERSION)


# Namespace-aware wrappers (C1.4-r2/F1): dispatch on each type's own schema
# attribute so v2 records (ControlState, evidence, manifests) keep their
# established encoding paths byte-for-byte. Only the journal-local namespace
# is deliberately routed through its dedicated encode_local/decode_local pair.
def encode(value: ContractData) -> str:
    schema = getattr(type(value), "schema", TRANSPORT_SCHEMA_VERSION)
    if schema is LOCAL_SCHEMA_VERSION:
        raise ContractError("journal-local records use encode_local")
    return _engine_encode(value, schema)


def decode(payload, expected_type):
    schema = getattr(expected_type, "schema", TRANSPORT_SCHEMA_VERSION)
    if schema is LOCAL_SCHEMA_VERSION:
        raise ContractError("journal-local records use decode_local")
    return _engine_decode(payload, expected_type, schema_version=schema)


__all__ = [
    "TRANSPORT_SCHEMA_VERSION", "MAX_REQUEST_BYTES", "MAX_RESPONSE_BYTES", "MAX_NESTING",
    "MAX_EVENTS_PER_BATCH", "MAX_RAW_FIELDS_PER_EVENT", "DISPATCH_TTL_MS",
    "POLL_ARMED_MIN_INTERVAL_MS", "POLL_DISABLED_INTERVAL_MS",
    "HTTP_REQUEST_FINGERPRINT_VERSION", "INITIAL_SERVICE_EPOCH", "EVENT_SEQUENCE_START",
    "STATUS_MAX_HEADER_BYTES", "MAX_HTTP_WORKERS", "MAX_ACCOUNT_ACTORS",
    "MAX_ARMED_NATIVE_ACCOUNTS", "EXECUTION_TOKEN_HEADER", "PAIRING_TOKEN_BYTES",
    "PAIRING_TOKEN_HEX_LENGTH", "STATUS_REQUEST_HEADERS",
    "ExecutionScope", "Origin", "CommandKind", "ReceiptStage", "ResultKind", "ControlKind",
    "ResponseKind", "ReadyResponseKind", "ControlResponseKind", "ControlPhase",
    "TransportErrorCode", "TRANSPORT_ERROR_HTTP_STATUS",
    "ControlState", "V2Readiness", "NativeAuthorization",
    "TransportMessage", "EventCursor", "AckWatermark", "ParameterConfirmation",
    "NativePilotPermit", "ExecutionOffer", "ReadyChallenge", "ContractLimit",
    "DispatchCommand", "DispatchReceipt", "RawCallbackField", "RawCallbackEnvelope",
    "PollRequest", "ReadyRequest", "ResultsRequest", "EventBatch", "ControlReport",
    "StatusRequest", "TransportResponse", "PollResponse", "ReadyResponse",
    "ResultsResponse", "EventsResponse", "ControlResponse", "StatusResponse",
    "TransportErrorResponse", "CallCount", "ExecutionCounters", "SubmitProjection",
    "encode", "decode", "canonical_hash", "request_fingerprint",
    "DISPATCH_WINDOW_RULES", "compute_offer_deadline", "compute_command_deadline",
    "validate_dispatch_admission",
    "LOCAL_SCHEMA_VERSION", "OFFER_DEADLINE_REQUIRED", "OFFER_DEADLINE_CONFLICT",
    "OFFER_DEADLINE_UNPROVEN", "OFFER_DEADLINE_CLOCK_INVALID",
    "OFFER_DEADLINE_BLOCKERS", "OfferDeadlineRecord", "encode_local", "decode_local",
]
