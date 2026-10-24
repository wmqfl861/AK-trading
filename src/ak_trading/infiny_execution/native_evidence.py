"""NE1-C1 native scope evidence and capability-origin verifiers (frozen DTOs).

Evidence objects are immutable records; their ARTIFACTS (sha256 references) are
verified by the trusted service assembler in C3, never by HTTP payloads.
Construction/validation here is purely structural and temporal: it never marks
anything as platform-verified. The production registry refuses every
registration in C1; the SYNTHETIC test factory exists only for in-process test
injection and no production CLI/HTTP path can instantiate it.

EvidenceSourceKind.SIMULATION_TERMINAL_QUERY_LOG (BS-1,
NE1-ARB-BASELINE-SOURCE-001, 2026-09-18) is admitted structurally by this
module only; codecs frozen before BS-1 reject the value, which is the required
compatibility behavior. Its skeleton validator fails closed with explicit
BASELINE_INSUFFICIENT_* blockers until BS-2/BS-3 verify real frozen
query-log originals against the ruling's section-4 field mapping.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum

from .contracts import (
    Binding,
    ContractData,
    ContractError,
    OrderInventoryItem,
    PositionBucket,
    TradeInventoryItem,
    validate_identifier,
    validate_integer,
    validate_utc_datetime,
    ValidationResult,
    V2_SCHEMA_VERSION,
)
from .native_contracts import AuthorizationAction, NativePilotPermit, Origin


class EvidenceSourceKind(str, Enum):
    TERMINAL_EXPORT = "TERMINAL_EXPORT"
    BROKER_EXPORT = "BROKER_EXPORT"
    # NE1-ARB-BASELINE-SOURCE-001 (2026-09-18), BS-1: the only newly admitted
    # baseline source for the confirmed CTP *simulation* account — frozen real
    # terminal query-log originals (funds/orders/trades/positions). Terminal or
    # broker exports must never be relabeled to it, and codecs frozen before
    # BS-1 reject this value by construction (required compatibility stance).
    SIMULATION_TERMINAL_QUERY_LOG = "SIMULATION_TERMINAL_QUERY_LOG"


class CallbackKind(str, Enum):
    ON_INIT = "ON_INIT"
    ON_START = "ON_START"
    ON_TICK = "ON_TICK"
    ON_CONTRACT_STATUS = "ON_CONTRACT_STATUS"
    ON_ORDER = "ON_ORDER"
    ON_ORDER_TRADE = "ON_ORDER_TRADE"
    ON_ORDER_CANCEL = "ON_ORDER_CANCEL"
    ON_CANCEL = "ON_CANCEL"
    ON_TRADE = "ON_TRADE"
    ON_ERROR = "ON_ERROR"
    ON_STOP = "ON_STOP"
    SET_PARAMS = "SET_PARAMS"


_HEX = frozenset("0123456789abcdef")


def _hash64(value, name):
    if not (type(value) is str and len(value) == 64 and all(ch in _HEX for ch in value)):
        raise ContractError(f"{name}: expected lowercase sha256 hex")
    return value


def _basis_text(value, name):
    if not (type(value) is str and 0 < len(value) <= 256 and
            not any(ord(ch) < 32 for ch in value)):
        raise ContractError(f"{name}: expected a concrete completion basis, not a slogan")
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeBaselineEvidence(ContractData):
    """Takeover baseline: source_kind only admits verified exports; a manual
    'no open orders' declaration is not representable in this type."""
    schema = V2_SCHEMA_VERSION
    evidence_id: str
    source_kind: EvidenceSourceKind
    artifact_sha256: str
    account_key: str
    trading_day: date
    session_epoch: str
    as_of: datetime
    order_inventory: tuple[OrderInventoryItem, ...]
    trade_inventory: tuple[TradeInventoryItem, ...]
    positions: tuple[PositionBucket, ...]
    coverage_description: str
    completion_basis: str
    isolation_evidence_id: str
    approved_scope_id: str
    expires_at: datetime
    origin: Origin = Origin.PRODUCTION

    def _validate(self):
        validate_identifier(self.evidence_id, "evidence_id")
        _hash64(self.artifact_sha256, "artifact_sha256")
        validate_identifier(self.account_key, "account_key")
        validate_identifier(self.session_epoch, "session_epoch")
        validate_utc_datetime(self.as_of, "as_of")
        _basis_text(self.coverage_description, "coverage_description")
        _basis_text(self.completion_basis, "completion_basis")
        validate_identifier(self.isolation_evidence_id, "isolation_evidence_id")
        validate_identifier(self.approved_scope_id, "approved_scope_id")
        validate_utc_datetime(self.expires_at, "expires_at")
        if self.expires_at <= self.as_of:
            raise ContractError("baseline expiry must follow its as_of")


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeRouteEvidence(ContractData):
    """Callback routing/ownership evidence; native-missing fields may be null,
    but the combination must still prove account+strategy scope and an alias
    domain. Memo round-trip is recorded, never assumed."""
    schema = V2_SCHEMA_VERSION
    evidence_id: str
    framework_sha256: str
    sdk_sha256: str
    binding: Binding
    callback_kinds: tuple[CallbackKind, ...]
    account_scope_basis: str
    strategy_scope_basis: str
    local_alias_scope: str
    front_id: int | None
    session_id: int | None
    memo_limit: int
    memo_roundtrip_verified: bool
    validated_at: datetime
    expires_at: datetime
    origin: Origin = Origin.PRODUCTION

    def _validate(self):
        validate_identifier(self.evidence_id, "evidence_id")
        _hash64(self.framework_sha256, "framework_sha256")
        _hash64(self.sdk_sha256, "sdk_sha256")
        if not self.callback_kinds:
            raise ContractError("route evidence must list callback kinds")
        _basis_text(self.account_scope_basis, "account_scope_basis")
        _basis_text(self.strategy_scope_basis, "strategy_scope_basis")
        if not ((self.front_id is not None and self.session_id is not None) or
                self.local_alias_scope):
            raise ContractError("alias domain needs front/session or an explicit local scope")
        if self.front_id is not None:
            validate_integer(self.front_id, "front_id")
        if self.session_id is not None:
            validate_integer(self.session_id, "session_id")
        validate_integer(self.memo_limit, "memo_limit", 1)
        validate_utc_datetime(self.validated_at, "validated_at")
        validate_utc_datetime(self.expires_at, "expires_at")


@dataclass(frozen=True, slots=True, kw_only=True)
class ClientThreadEvidence(ContractData):
    """Client callback-thread record; thread integers never cross processes,
    so the process domain is mandatory."""
    schema = V2_SCHEMA_VERSION
    evidence_id: str
    instance_id: str
    session_epoch: str
    generation: str
    process_instance_id: str
    callback_thread_id: int
    callback_kind: CallbackKind
    observed_at: datetime
    origin: Origin = Origin.PRODUCTION

    def _validate(self):
        for name in ("evidence_id", "instance_id", "session_epoch", "generation",
                     "process_instance_id"):
            validate_identifier(getattr(self, name), name)
        validate_integer(self.callback_thread_id, "callback_thread_id")
        validate_utc_datetime(self.observed_at, "observed_at")


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeSettlementEvidence(ContractData):
    """Scoped settlement proof covering exactly the listed legs/events; it
    never promotes own_journal to FULL_ACCOUNT and never releases resources
    outside its explicit scope."""
    schema = V2_SCHEMA_VERSION
    evidence_id: str
    baseline_evidence_id: str
    binding: Binding
    as_of: datetime
    artifact_sha256: str
    covered_leg_ids: tuple[str, ...]
    covered_event_ids: tuple[str, ...]
    order_inventory: tuple[OrderInventoryItem, ...]
    trade_inventory: tuple[TradeInventoryItem, ...]
    position_snapshot_id: str
    completion_basis: str
    isolation_evidence_id: str
    origin: Origin = Origin.PRODUCTION

    def _validate(self):
        validate_identifier(self.evidence_id, "evidence_id")
        validate_identifier(self.baseline_evidence_id, "baseline_evidence_id")
        _hash64(self.artifact_sha256, "artifact_sha256")
        validate_utc_datetime(self.as_of, "as_of")
        if not self.covered_leg_ids:
            raise ContractError("settlement evidence must list its covered legs")
        if not self.covered_event_ids:
            raise ContractError("settlement evidence must list its covered events")
        if len(set(self.covered_leg_ids)) != len(self.covered_leg_ids):
            raise ContractError("covered leg ids must be unique")
        if len(set(self.covered_event_ids)) != len(self.covered_event_ids):
            raise ContractError("covered event ids must be unique")
        for identity in self.covered_leg_ids + self.covered_event_ids:
            validate_identifier(identity, "covered identity")
        validate_identifier(self.position_snapshot_id, "position_snapshot_id")
        _basis_text(self.completion_basis, "completion_basis")
        validate_identifier(self.isolation_evidence_id, "isolation_evidence_id")


# ---------------------------------------------------------------------------
# temporal validators (structural + expiry only; artifact checks belong to C3)
# ---------------------------------------------------------------------------

# BS-1 (NE1-ARB-BASELINE-SOURCE-001 §4): frozen environment marker that a
# SIMULATION_TERMINAL_QUERY_LOG coverage_description must carry literally.
SIMULATION_QUERY_LOG_ENVIRONMENT_MARKER = "CTP_SIMULATION;NON_LIVE"

_SHA256_RUN = re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")


def validate_simulation_terminal_query_log_baseline(
        evidence: NativeBaselineEvidence, *, now: datetime) -> ValidationResult:
    """BS-1 skeleton validator for SIMULATION_TERMINAL_QUERY_LOG baselines.

    Implements the locally decidable invariants of the ruling's §4 field
    mapping. Fail-closed: any missing qualifying-original condition is an
    explicit BASELINE_INSUFFICIENT_* blocker, never a default pass.

    Checked here:
    * coverage_description carries the frozen environment marker
      ``CTP_SIMULATION;NON_LIVE`` and binds ``account=<account_key>``;
    * completion_basis references an actual sha256 (64 lowercase hex) of the
      completion-basis receipt — slogans ("user confirmed", "all zero") or a
      ruling number are not a basis;
    * inventories are completed mappings of the proven query domain: every
      order/trade item and position bucket belongs to the baseline's
      account_key and trading_day. An empty tuple encodes a zero-row claim;
      zero-row completion cannot be certified at this layer (the real query-log
      formats are BS-2/BS-3 work, blocked on real terminal samples), so empty
      inventories are explicit INSUFFICIENT placeholders and are rejected;
    * expires_at stays a real approval window: strictly after as_of and after
      the supplied trusted ``now``. The now-as_of<=300s economy gate remains
      in planning; this validator never relaxes or duplicates it.

    Deliberately NOT done here: parsing order/trade/position query-log formats
    (BS-2/BS-3 own the frozen parsers; §5 forbids implementing them against
    imagined formats), verifying that artifact_sha256 matches a frozen
    original's actual bytes, and verifying the isolation/approved-scope
    records exist and cover the window — those belong to the trusted service
    assembler's environment gate. Their presence and shape remain structural
    construction requirements of the DTO itself.
    """
    if evidence.source_kind is not EvidenceSourceKind.SIMULATION_TERMINAL_QUERY_LOG:
        raise ContractError(
            "skeleton validator only accepts SIMULATION_TERMINAL_QUERY_LOG baselines")
    now = validate_utc_datetime(now, "now")
    blockers = []
    if (SIMULATION_QUERY_LOG_ENVIRONMENT_MARKER not in evidence.coverage_description or
            f"account={evidence.account_key}" not in evidence.coverage_description):
        blockers.append("BASELINE_INSUFFICIENT_COVERAGE_ENV")
    if _SHA256_RUN.search(evidence.completion_basis) is None:
        blockers.append("BASELINE_INSUFFICIENT_COMPLETION_BASIS")
    if not (evidence.order_inventory and evidence.trade_inventory and evidence.positions):
        blockers.append("BASELINE_INSUFFICIENT_INVENTORY")
    scope_mismatch = any(
        item.account_key != evidence.account_key or item.trading_day != evidence.trading_day
        for item in evidence.order_inventory + evidence.trade_inventory)
    scope_mismatch = scope_mismatch or any(
        bucket.binding.account_key != evidence.account_key or
        bucket.binding.trading_day != evidence.trading_day
        for bucket in evidence.positions)
    if scope_mismatch:
        blockers.append("BASELINE_INSUFFICIENT_INVENTORY_SCOPE")
    if evidence.expires_at <= evidence.as_of:  # defensive: construction already rejects it
        blockers.append("BASELINE_INSUFFICIENT_EXPIRY_WINDOW")
    if evidence.expires_at <= now:
        blockers.append("BASELINE_EXPIRED")
    return ValidationResult(blockers=tuple(blockers))


def validate_baseline(evidence: NativeBaselineEvidence, *, now: datetime) -> ValidationResult:
    if evidence.source_kind is EvidenceSourceKind.SIMULATION_TERMINAL_QUERY_LOG:
        # BS-1: the query-log source kind never falls back to the expiry-only
        # check; the §4 skeleton runs and fails closed on its own blockers.
        return validate_simulation_terminal_query_log_baseline(evidence, now=now)
    if evidence.expires_at <= now:
        return ValidationResult(blockers=("BASELINE_EXPIRED",))
    return ValidationResult(blockers=())


def validate_route(evidence: NativeRouteEvidence, *, now: datetime) -> ValidationResult:
    if evidence.expires_at <= now:
        return ValidationResult(blockers=("ROUTE_EVIDENCE_EXPIRED",))
    return ValidationResult(blockers=())


def validate_thread(evidence: ClientThreadEvidence, *, now: datetime) -> ValidationResult:
    # Thread evidence has no TTL: it binds a generation, not a freshness window.
    del now
    return ValidationResult(blockers=())


def validate_settlement(evidence: NativeSettlementEvidence, *, now: datetime) -> ValidationResult:
    if evidence.as_of > now:
        return ValidationResult(blockers=("SETTLEMENT_FUTURE",))
    return ValidationResult(blockers=())


# ---------------------------------------------------------------------------
# capability origin: registries
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True, kw_only=True)
class EvidenceDeadlineResult(ContractData):
    """C1.3 / RD-01: local immutable registry-deadline answer (not a transport
    DTO). validation.ok is true if and only if deadline is not None; None
    never means infinity. A success deadline is timezone-aware UTC and lies
    strictly beyond the trusted `now` supplied with the query."""
    schema = V2_SCHEMA_VERSION
    validation: ValidationResult
    deadline: datetime | None

    def _validate(self):
        if not isinstance(self.validation, ValidationResult):
            raise ContractError("expected a ValidationResult")
        if self.deadline is not None:
            validate_utc_datetime(self.deadline, "deadline")
        if self.validation.ok != (self.deadline is not None):
            raise ContractError("validation.ok must correspond to the presence of a deadline")
        for code in self.validation.blockers:
            if not (type(code) is str and code):
                raise ContractError("blocker codes must be nonempty strings")


class EvidenceRegistry:
    """Trusted-assembler interface (C3 implements artifact verification).

    C1.3 / RD-01..RD-03: `applicable_deadline` exposes the permit/evidence
    applicability bound so N3 derives D_original/D_current through this public
    interface instead of inspecting registry internals or manufacturing an
    evidence expiry. The base implementation is fail-closed:
    NATIVE_DEADLINE_UNAVAILABLE. Concrete registries compute, per action:
    D_registry(SUBMIT) = min(P.expires_at, A.expires_at, B.expires_at,
                             R.expires_at, C.applicable_deadline)
    D_registry(CANCEL_OWNED) = min(P.expires_at, A.expires_at, R.expires_at)
    Success requires every required dependency deadline to be verifiable and
    strictly beyond the supplied trusted now; equality blocks
    (NATIVE_DEADLINE_EXPIRED)."""

    def applicable_deadline(self, permit_id: str, *, action: AuthorizationAction,
                            now: datetime) -> EvidenceDeadlineResult:
        validate_identifier(permit_id, "permit_id")
        if not isinstance(action, AuthorizationAction):
            raise ContractError("expected an AuthorizationAction")
        validate_utc_datetime(now, "now")
        return EvidenceDeadlineResult(
            validation=ValidationResult(blockers=("NATIVE_DEADLINE_UNAVAILABLE",)),
            deadline=None)

    def register_baseline(self, evidence: NativeBaselineEvidence) -> None:
        raise NotImplementedError

    def register_route(self, evidence: NativeRouteEvidence) -> None:
        raise NotImplementedError

    def register_thread(self, evidence: ClientThreadEvidence) -> None:
        raise NotImplementedError

    def register_settlement(self, evidence: NativeSettlementEvidence) -> None:
        raise NotImplementedError

    def register_permit(self, permit: NativePilotPermit) -> None:
        raise NotImplementedError

    def verify_permit_active(self, permit_id: str, *, now: datetime) -> ValidationResult:
        raise NotImplementedError


class ProductionEvidenceRegistry(EvidenceRegistry):
    """C1 production stance: the trusted assembler does not exist yet, so no
    production evidence or permit can be registered. SYNTHETIC origins are
    refused outright and can never become broker evidence."""

    def _refuse(self, value):
        origin = getattr(value, "origin", None)
        if origin is Origin.SYNTHETIC:
            raise ContractError("SYNTHETIC evidence can never be registered as production")
        raise ContractError("production evidence registration is not available before C3")

    def register_baseline(self, evidence):
        self._refuse(evidence)

    def register_route(self, evidence):
        self._refuse(evidence)

    def register_thread(self, evidence):
        self._refuse(evidence)

    def register_settlement(self, evidence):
        self._refuse(evidence)

    def register_permit(self, permit):
        self._refuse(permit)

    def applicable_deadline(self, permit_id, *, action, now):
        validate_identifier(permit_id, "permit_id")
        if not isinstance(action, AuthorizationAction):
            raise ContractError("expected an AuthorizationAction")
        validate_utc_datetime(now, "now")
        return EvidenceDeadlineResult(
            validation=ValidationResult(blockers=("NATIVE_PERMIT_UNREGISTERED",)),
            deadline=None)

    def verify_permit_active(self, permit_id, *, now):
        del permit_id, now
        return ValidationResult(blockers=("NATIVE_PERMIT_UNREGISTERED",))


class TestEvidenceFactory:
    """SYNTHETIC-only issuance for offline tests (fixture manifest marks every
    case SYNTHETIC). No production CLI/HTTP path constructs this factory."""

    def __init__(self, *, purpose: str):
        if purpose != "SYNTHETIC_TEST_ONLY":
            raise ContractError("test evidence factory requires the SYNTHETIC test purpose")
        self._purpose = purpose

    def issue_permit(self, *, permit_id: str, authorization_id: str, binding: Binding,
                     control_revision: int, baseline_evidence_id: str, route_evidence_id: str,
                     cash_bound_evidence_id: str, source_key: str,
                     actions: tuple[AuthorizationAction, ...], max_volume: int,
                     expires_at: datetime, params_sha256: str) -> NativePilotPermit:
        return NativePilotPermit(
            permit_id=permit_id, authorization_id=authorization_id, binding=binding,
            control_revision=control_revision, baseline_evidence_id=baseline_evidence_id,
            route_evidence_id=route_evidence_id, cash_bound_evidence_id=cash_bound_evidence_id,
            source_key=source_key, actions=actions, max_send_attempts=1,
            max_cancel_attempts=0, max_volume=max_volume, price_bounds=(),
            expires_at=expires_at, params_sha256=params_sha256, origin=Origin.SYNTHETIC)


# ---------------------------------------------------------------------------
# operator registration manifest (RULING 05, N1b-owned shared contract)
# ---------------------------------------------------------------------------

OPERATOR_REGISTRATION_SCHEMA_VERSION = "infiny_native_operator_registration_v1"


class OperatorRecordType(str, Enum):
    ACTOR_REGISTRATION = "ActorRegistration"
    NATIVE_AUTHORIZATION = "NativeAuthorization"
    NATIVE_PILOT_PERMIT = "NativePilotPermit"
    NATIVE_BASELINE_EVIDENCE = "NativeBaselineEvidence"
    NATIVE_ROUTE_EVIDENCE = "NativeRouteEvidence"
    NATIVE_SETTLEMENT_EVIDENCE = "NativeSettlementEvidence"
    CLIENT_THREAD_EVIDENCE = "ClientThreadEvidence"


def _local_file_path(value, name):
    if not (type(value) is str and 0 < len(value) <= 512):
        raise ContractError(f"{name}: expected an explicit local file path")
    if "://" in value or value.startswith("//") or value.startswith("\\\\"):
        raise ContractError(f"{name}: URLs and UNC paths are not explicit local files")
    if "${" in value or "%" in value:
        raise ContractError(f"{name}: environment expansion is not a explicit path")
    if value.endswith((".exe", ".dll", ".pyd", ".bat", ".cmd")):
        raise ContractError(f"{name}: executables are not registration records")
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class OperatorArtifact(ContractData):
    schema = V2_SCHEMA_VERSION
    path: str
    sha256: str

    def _validate(self):
        _local_file_path(self.path, "artifact path")
        _hash64(self.sha256, "artifact sha256")


@dataclass(frozen=True, slots=True, kw_only=True)
class OperatorRegistrationManifest(ContractData):
    """Operator-side registration manifest (RULING 05). A manifest is an
    operator INPUT consumed by explicit stopped-service CLI subcommands; it is
    never read at runtime, never auto-imported from HTTP, and never carries
    credentials, pairing files or unfiltered account secrets."""
    schema = V2_SCHEMA_VERSION
    registration_id: str
    record_type: OperatorRecordType
    record_path: str
    record_sha256: str
    contract_lock_sha256: str
    operator_action_id: str
    authorization_artifact_path: str
    authorization_artifact_sha256: str
    artifacts: tuple[OperatorArtifact, ...]

    def _validate(self):
        validate_identifier(self.registration_id, "registration_id")
        validate_identifier(self.operator_action_id, "operator_action_id")
        _local_file_path(self.record_path, "record path")
        _local_file_path(self.authorization_artifact_path, "authorization artifact path")
        _hash64(self.record_sha256, "record sha256")
        _hash64(self.contract_lock_sha256, "contract lock sha256")
        _hash64(self.authorization_artifact_sha256, "authorization artifact sha256")
        if not self.artifacts:
            raise ContractError("a manifest lists its referenced artifacts")
        paths = [item.path for item in self.artifacts]
        if len(set(paths)) != len(paths):
            raise ContractError("artifact paths must be unique")


__all__ = [
    "EvidenceSourceKind", "CallbackKind", "NativeBaselineEvidence", "NativeRouteEvidence",
    "ClientThreadEvidence", "NativeSettlementEvidence", "validate_baseline", "validate_route",
    "validate_thread", "validate_settlement", "validate_simulation_terminal_query_log_baseline",
    "SIMULATION_QUERY_LOG_ENVIRONMENT_MARKER", "EvidenceRegistry", "ProductionEvidenceRegistry",
    "TestEvidenceFactory", "EvidenceDeadlineResult",
    "OPERATOR_REGISTRATION_SCHEMA_VERSION", "OperatorRecordType",
    "OperatorArtifact", "OperatorRegistrationManifest",
]
