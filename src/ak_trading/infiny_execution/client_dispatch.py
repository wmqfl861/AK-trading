"""Pure two-phase dispatch orchestration helpers (NE1 plan §5.2/§5.3).

No ports, HTTP, threads or journal writes live here: the service actor
composes these helpers around N3's atomic DispatchJournal. Dispatch validity
is the frozen 1500 ms window (C1.2) and is never refreshed; receipts chain by
canonical hash and NO_CALL never fabricates a native result.
"""
from collections.abc import Mapping
from datetime import datetime, timedelta
import hashlib
import json

from . import contracts as c
from .native_contracts import (
    DISPATCH_TTL_MS,
    CommandKind,
    DispatchCommand,
    DispatchReceipt,
    ExecutionOffer,
    ReceiptStage,
    ReadyChallenge,
    ResultKind,
    compute_command_deadline,
    compute_offer_deadline,
    validate_dispatch_admission,
)

__all__ = [
    "DISPATCH_TTL_MS", "CommandKind", "DispatchCommand", "DispatchReceipt", "ExecutionOffer",
    "ReceiptStage", "ReadyChallenge", "ResultKind",
    "compute_offer_deadline", "compute_command_deadline", "validate_dispatch_admission",
    "dispatch_deadline", "verify_ready_challenge", "request_digest", "cancel_digest",
    "build_receipt", "validate_receipt_chain", "SingleFlightGuard", "recovery_snapshot",
]


def dispatch_deadline(issued_at: datetime, *earliest: datetime | None) -> datetime:
    """Convenience alias over the frozen DTTL-02 calculator; a ticket never renews."""
    candidates = [item for item in earliest if item is not None]
    if not candidates:
        return issued_at + timedelta(milliseconds=DISPATCH_TTL_MS)
    return compute_offer_deadline(issued_at, candidates)


def verify_ready_challenge(challenge: ReadyChallenge, offer: ExecutionOffer, *,
                           control_state, now: datetime) -> c.ValidationResult:
    """Structural re-verification of a client readiness claim.

    The challenge is a claim, never proof of right: only fields that must
    match persisted control state are accepted, and a claimed closed gate
    blocks while a claimed open gate still grants nothing by itself.
    """
    blockers = []
    if challenge.offer_id != offer.offer_id:
        blockers.append("OFFER_MISMATCH")
    if challenge.latest_params_sha256 != control_state.params_sha256:
        blockers.append("PARAMS_CHANGED")
    if challenge.generation != control_state.generation:
        blockers.append("GENERATION_CHANGED")
    if not challenge.enable_trading:
        blockers.append("GATE_CLOSED")
    if offer.expires_at <= now:
        blockers.append("DISPATCH_EXPIRED")
    if offer.control_revision != control_state.control_revision:
        blockers.append("CONTROL_REVISION_MISMATCH")
    return c.ValidationResult(blockers=tuple(dict.fromkeys(blockers)))


def request_digest(request: Mapping) -> str:
    """sha256 over the canonical JSON of a pure native request mapping.

    The in-memory Investor handle is excluded: identity stays client-side and
    never crosses the wire, so both ends derive the same digest from the
    remaining eleven keys.
    """
    safe = {key: value for key, value in dict(request).items() if key != "Investor"}
    payload = json.dumps(safe, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def cancel_digest(order_id: int) -> str:
    """Digest of the one-key cancel call boundary."""
    payload = json.dumps({"cancelOrder": order_id}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_receipt(*, command: DispatchCommand, stage: ReceiptStage, generation: str,
                  local_sequence: int, recorded_at: datetime, request: Mapping | None = None,
                  order_id: int | None = None, result_kind=None, result_value=None,
                  error_code=None, previous: DispatchReceipt | None = None,
                  receipt_id: str) -> DispatchReceipt:
    """Assemble a chained call-boundary receipt (CONSUMED -> RETURNED/NO_CALL).

    A CONSUMED record is fsynced before the native call, so its result is not
    yet known: it carries the frozen OUTCOME_PENDING marker rather than a
    fabricated native outcome.
    """
    if stage is ReceiptStage.CONSUMED and result_kind is None:
        result_kind, error_code = ResultKind.EXCEPTION, "OUTCOME_PENDING"
    if request is not None:
        digest = request_digest(request)
    elif order_id is not None and command.kind is CommandKind.CANCEL:
        digest = cancel_digest(order_id)
    else:
        raise ValueError("a receipt requires the request digest source")
    return DispatchReceipt(
        receipt_id=receipt_id, command_id=command.command_id, attempt_id=command.attempt_id,
        binding=command.binding, generation=generation, stage=stage,
        local_sequence=local_sequence, recorded_at=recorded_at, result_kind=result_kind,
        result_value=result_value, error_code=error_code, request_sha256=digest,
        previous_receipt_sha256=(None if previous is None else c.canonical_hash(previous)))


def validate_receipt_chain(receipts: tuple[DispatchReceipt, ...]) -> c.ValidationResult:
    """Contiguity, hash chaining, single CONSUMED and one terminal stage."""
    blockers = []
    if not receipts:
        return c.ValidationResult(blockers=("RECEIPT_CHAIN_EMPTY",))
    first = receipts[0]
    if receipts[0].local_sequence != 1:
        blockers.append("RECEIPT_CHAIN_START")
    previous = None
    consumed = 0
    terminal = 0
    for position, receipt in enumerate(receipts):
        if (receipt.command_id != first.command_id or receipt.attempt_id != first.attempt_id):
            blockers.append("RECEIPT_CHAIN_COMMAND_MIXED")
        if receipt.local_sequence != position + 1:
            blockers.append("RECEIPT_SEQUENCE_GAP")
        expected_link = None if previous is None else c.canonical_hash(previous)
        if receipt.previous_receipt_sha256 != expected_link:
            blockers.append("RECEIPT_CHAIN_BROKEN")
        if receipt.stage is ReceiptStage.CONSUMED:
            consumed += 1
        else:
            terminal += 1
        previous = receipt
    if consumed != 1:
        blockers.append("RECEIPT_CONSUME_NOT_ONCE")
    if terminal > 1:
        blockers.append("RECEIPT_DOUBLE_TERMINAL")
    return c.ValidationResult(blockers=tuple(dict.fromkeys(blockers)))


class SingleFlightGuard:
    """At most one in-flight command per execution instance scope."""

    def __init__(self):
        self._inflight: dict[tuple[str, str], str] = {}

    def admit(self, command: DispatchCommand) -> bool:
        key = (command.binding.account_key, command.binding.strategy_key)
        if key in self._inflight:
            return False
        self._inflight[key] = command.command_id
        return True

    def release(self, command_id: str) -> None:
        for key, value in list(self._inflight.items()):
            if value == command_id:
                del self._inflight[key]

    def inflight(self, binding) -> str | None:
        return self._inflight.get((binding.account_key, binding.strategy_key))


def recovery_snapshot(commands, *, now: datetime) -> tuple:
    """Read-only restart view: unexpired commands remain reconciled items.

    Recovery never re-executes: an unexpired ticket is reported, not handed
    to a client again; expired tickets are merely dropped from the view.
    """
    unexpired = []
    for command in commands:
        if command.issued_at <= now < command.expires_at:
            unexpired.append(command)
    return tuple(unexpired)
