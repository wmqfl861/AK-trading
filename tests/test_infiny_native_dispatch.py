"""NE1-H13/H14/H15/H26 (N4 layer): offer orchestration, CAS rejections, receipt logic.

Commit-phase atomicity (H16/H17 full assertions) additionally requires N3's
frozen DispatchJournal; until that file lands, the N4-side guarantee under
test is that every rejection path leaves zero attempts and zero writes and
that no dispatch is ever produced without the journal. All fixtures are
SYNTHETIC.
"""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ak_trading.infiny_execution import contracts as c
from ak_trading.infiny_execution.client_dispatch import (
    SingleFlightGuard,
    build_receipt,
    cancel_digest,
    dispatch_deadline,
    recovery_snapshot,
    request_digest,
    validate_receipt_chain,
)
from ak_trading.infiny_execution.dispatch_journal import DispatchJournal
from ak_trading.infiny_execution.executor import Executor
from ak_trading.infiny_execution.ledger import Ledger, LedgerError
from ak_trading.infiny_execution.native import NativeExecutionError
from ak_trading.infiny_execution.native_contracts import (
    CommandKind,
    ContractLimit,
    DispatchCommand,
    ReadyChallenge,
    ReceiptStage,
    ResultKind,
)
from ak_trading.infiny_execution.reconcile import CallbackContext, normalize_event
from ak_trading.infiny_execution.simulation import FakeClock, FakeNative, simulation_context
from infiny_native_fakes import FakeEvidenceRegistry, synthetic_control_state

D = Decimal


class Pilot:
    """SYNTHETIC offline assembly: real v2 ledger, fake port, optional journal."""

    def __init__(self, path, *, journal=True):
        self.clock = FakeClock()
        self.context = simulation_context(clock=self.clock)
        self.ledger = Ledger(Path(path), clock=self.clock, schema="v2")
        self._initialize_or_adopt()
        self.native = FakeNative(self.context.binding)
        self.control = synthetic_control_state(self.context)
        self.registry = FakeEvidenceRegistry()
        self._register_control_once()
        self.executor = Executor(
            self.ledger, port=self.native, mode=c.ExecutionMode.SIMULATION,
            binding=self.context.binding, investor_handle=self.native.investor_handle,
            clock=self.clock, evidence_registry=self.registry,
            control_state=self.control,
            dispatch_journal=(DispatchJournal(self.ledger, evidence_registry=self.registry)
                              if journal else None))

    def _initialize_or_adopt(self):
        """Absent-only initialization; a reopened journal is adopted when its
        durable controls match, mirroring the production assembler stance."""
        try:
            self.ledger.initialize_context(self.context)
        except LedgerError:
            persisted = self.ledger.context(self.context.binding)
            if persisted is None:
                raise
            for field in ("mode", "binding", "policy", "authorization", "route_ownership"):
                if getattr(persisted, field) != getattr(self.context, field):
                    raise
            self.context = persisted

    def _register_control_once(self):
        existing = self.ledger.control_revision(self.context.binding)
        if existing is None:
            self.ledger.register_control_update(self.control)
        elif existing != self.control.control_revision:
            raise ValueError("durable control revision differs from the assembled state")

    def close(self):
        self.ledger.close()

    def offer(self, intent=None):
        offer = self.executor.prepare_native_offer(intent or self.context.intent, self.context)
        # adopt the CAS-refreshed persisted context (receipt + view attached)
        self.context = self.ledger.context(self.context.binding)
        return offer

    def challenge(self, offer, *, enable_trading=True):
        return ReadyChallenge(
            offer_id=offer.offer_id, capture_id="SIM_CAPTURE_1",
            thread_evidence_id="SIM_THREAD_TEST",
            latest_params_sha256=self.control.params_sha256,
            enable_trading=enable_trading, unconfirmed_event_watermark=None,
            generation=self.control.generation)


@pytest.fixture
def pilot(tmp_path):
    item = Pilot(tmp_path / "pilot.sqlite")
    try:
        yield item
    finally:
        item.close()


def make_leg():
    return c.Leg(leg_id="SIM_INTENT:OPEN:-:0", index=0, phase=c.Phase.OPEN, depends_on=(),
                 direction=c.Direction.BUY, offset=c.Offset.OPEN, volume=2, price=D("3500"),
                 position_bucket=None, cash_reserve=D("2000"))


def submit_command(context, *, command_id="SIM_CMD_1", issued_at=None):
    issued = issued_at if issued_at is not None else context.clock.now
    return DispatchCommand(
        command_id=command_id, kind=CommandKind.SUBMIT, attempt_id="a:" + command_id,
        offer_id="SIM_OFFER_1", intent_id="SIM_INTENT", leg_id="SIM_INTENT:OPEN:-:0",
        plan_id="f" * 64, binding=context.binding, leg=make_leg(), order_type=c.OrderType.GFD,
        price_tick=D("1"), contract_limits=ContractLimit(min_volume=1, max_volume=5, volume_step=1),
        control_revision=1, permit_id="SIM_PERMIT_1", params_sha256="ab" * 32,
        issued_at=issued, expires_at=dispatch_deadline(issued))


def working_event(pilot, leg_id, order_id, event_id):
    binding = pilot.context.binding
    return normalize_event(
        {"OrderID": order_id, "OrderSysID": f"SIM_SYS_{order_id}", "FrontID": 1, "SessionID": 1,
         "Exchange": binding.exchange, "InstrumentID": binding.instrument_id, "Hedgeflag": "1",
         "Direction": "0", "Offset": "0", "Memo": leg_id, "TradingDay": "20260914",
         "Status": "未成交", "TotalVolume": 2, "TradedVolume": 0, "CancelVolume": 0,
         "Price": "3500"},
        CallbackContext(event_id=event_id, kind=c.EventKind.ORDER, received_at=pilot.clock(),
                        session_epoch=binding.session_epoch, source=c.Source.SIMULATION,
                        account_key=binding.account_key, strategy_key=binding.strategy_key,
                        simulation_scope=True))


# --- NE1-H13: stale control at every orchestration entry, zero writes ---


@pytest.mark.parametrize("mutate", ["authorization", "policy", "disabled", "epoch"])
def test_H13_offer_rejects_concurrent_control_change(tmp_path, mutate):
    item = Pilot(tmp_path / "race.sqlite")
    other = Ledger(tmp_path / "race.sqlite", clock=item.clock)
    try:
        first = item.offer()
        base = item.context
        if mutate == "authorization":
            changed = replace(base, authorization=None)
        elif mutate == "policy":
            changed = replace(base, policy=replace(base.policy, max_cash_reserve=D("1")))
        elif mutate == "disabled":
            changed = replace(base, mode=c.ExecutionMode.DISABLED, authorization=None,
                              readiness=replace(base.readiness, mode=c.ExecutionMode.DISABLED,
                                                can_submit=False, can_cancel_owned=False,
                                                state=c.ReadinessState.PLAN_READY))
        else:
            binding = replace(base.binding, session_epoch="SIM_EPOCH_NEXT")
            coverage = replace(base.snapshot.coverage, session_epoch=binding.session_epoch)
            snapshot = replace(base.snapshot, snapshot_id="SIM_EPOCH_SNAPSHOT", binding=binding,
                               coverage=coverage, quote=replace(base.snapshot.quote, binding=binding),
                               account=replace(base.snapshot.account, binding=binding),
                               positions=tuple(replace(p, binding=binding) for p in base.snapshot.positions))
            changed = replace(base, binding=binding, snapshot=snapshot, proof=None,
                              authorization=None, intent=replace(base.intent, binding=binding),
                              reservation_view=None,
                              readiness=replace(base.readiness, coverage=coverage, can_submit=False,
                                                state=c.ReadinessState.PLAN_READY))
        other.register_context(changed)
        replacement_intent = replace(base.intent, intent_id="SIM_INTENT_NEXT",
                                     source_key="SIM_SOURCE_NEXT")
        with pytest.raises((NativeExecutionError, LedgerError)):
            item.executor.prepare_native_offer(replacement_intent, base)
        assert item.native.sent_requests == []
        assert item.ledger.get_leg(first.leg_id).attempt_no == 0
        persisted = other.context(base.binding)
        assert persisted.authorization is (None if mutate in ("authorization", "disabled", "epoch")
                                           else persisted.authorization)
        if mutate == "policy":
            assert persisted.policy.max_cash_reserve == D("1")
        if mutate == "epoch":
            assert persisted.binding.session_epoch == "SIM_EPOCH_NEXT"
    finally:
        other.close()
        item.close()


@pytest.mark.parametrize("mutate", ["authorization", "policy"])
def test_H13_commit_rejects_stale_control_zero_attempt(pilot, tmp_path, mutate):
    offer = pilot.offer()
    journal = Ledger(tmp_path / "pilot.sqlite", clock=pilot.clock)
    try:
        base = pilot.context
        changed = (replace(base, authorization=None) if mutate == "authorization"
                   else replace(base, policy=replace(base.policy, max_cash_reserve=D("1"))))
        journal.register_context(changed)
    finally:
        journal.close()
    with pytest.raises((NativeExecutionError, LedgerError)):
        pilot.executor.commit_native_offer(offer.offer_id, pilot.challenge(offer), pilot.context)
    record = pilot.ledger.get_leg(offer.leg_id)
    assert record.attempt_no == 0 and record.state is c.OrderState.RESERVED
    assert pilot.native.sent_requests == []


def test_H13_cancel_offer_rejects_revoked_authorization_without_latch(pilot):
    offer = pilot.offer()
    pilot.executor.submit_once(offer.intent_id, offer.leg_id, offer.plan_id, pilot.context)
    revoked = replace(pilot.context, authorization=None)
    with pytest.raises((NativeExecutionError, LedgerError)):
        pilot.executor.prepare_native_cancel("SIM_CANCEL_1", offer.intent_id, offer.leg_id, revoked)
    assert pilot.ledger.get_leg(offer.leg_id).cancel_state is c.CancelState.NONE
    assert pilot.native.cancelled_ids == []


# --- NE1-H14: offer identity, revision layering and forgery rejection ---


def test_H14_offer_carries_persisted_control_revision_and_hashes(pilot):
    control = synthetic_control_state(pilot.context, control_revision=7)
    pilot.ledger.register_control_update(control)  # durable revision bump
    pilot.executor._control_state = control
    offer = pilot.offer()
    assert offer.control_revision == 7
    record = pilot.ledger.get_leg(offer.leg_id)
    assert offer.leg_hash == c.canonical_hash(record.leg)
    assert offer.intent_hash == c.canonical_hash(record.intent)
    assert offer.plan_id == record.plan.plan_id
    assert record.attempt_no == 0
    assert pilot.native.sent_requests == []


def test_H14_commit_rejects_unknown_offer_and_revision_mismatch(pilot):
    real = pilot.offer()
    with pytest.raises((NativeExecutionError, LedgerError)):
        pilot.executor.commit_native_offer("SIM_UNKNOWN_OFFER", pilot.challenge(real), pilot.context)
    bumped = synthetic_control_state(pilot.context, control_revision=real.control_revision + 1)
    pilot.executor._control_state = bumped
    with pytest.raises((NativeExecutionError, LedgerError)):
        pilot.executor.commit_native_offer(real.offer_id, pilot.challenge(real), pilot.context)
    assert pilot.ledger.get_leg(real.leg_id).attempt_no == 0
    assert pilot.native.sent_requests == []


def test_H14_commit_rejects_closed_gate_and_stale_params(pilot):
    offer = pilot.offer()
    closed = pilot.challenge(offer, enable_trading=False)
    with pytest.raises((NativeExecutionError, LedgerError), match="GATE_CLOSED"):
        pilot.executor.commit_native_offer(offer.offer_id, closed, pilot.context)
    stale = replace(pilot.challenge(offer), latest_params_sha256="cd" * 32)
    with pytest.raises((NativeExecutionError, LedgerError), match="PARAMS_CHANGED"):
        pilot.executor.commit_native_offer(offer.offer_id, stale, pilot.context)
    assert pilot.ledger.get_leg(offer.leg_id).attempt_no == 0


# --- NE1-H15: idempotency; an offer is never a call ---


def test_H15_repeated_offer_is_idempotent_and_poll_creates_nothing(pilot):
    first = pilot.offer()
    again = pilot.offer()
    assert again.plan_id == first.plan_id and again.leg_id == first.leg_id
    view = pilot.ledger.view(pilot.context.binding.account_key)
    intent_legs = pilot.ledger.get_leg(first.leg_id).plan.legs
    assert {item.leg_id for item in view.reservations} == {leg.leg_id for leg in intent_legs}
    assert pilot.native.sent_requests == [] and pilot.native.cancelled_ids == []
    for leg in intent_legs:
        assert pilot.ledger.get_leg(leg.leg_id).attempt_no == 0


def test_H15_commit_without_journal_writes_nothing(tmp_path):
    """C1.4: without the dispatch journal even offer creation is refused, so
    no attempt, ticket or native call can ever appear."""
    item = Pilot(tmp_path / "nojournal.sqlite", journal=False)
    try:
        with pytest.raises(NativeExecutionError, match="DISPATCH_JOURNAL_REQUIRED"):
            item.offer()
        assert item.native.sent_requests == []
        # C1.5: without the journal the single preparation seam never runs, so
        # nothing at all is committed — no intent, plan, reservation, attempt
        # or ticket can exist
        view = item.ledger.view(item.context.binding.account_key)
        assert view.reservations == ()
        assert view.inflight_legs == 0
    finally:
        item.close()


# --- NE1-H16/H17/H18: atomic dispatch, at-most-once consume, return projection ---


def consumed_receipt(command, pilot, *, sequence=1):
    return build_receipt(
        command=command, stage=ReceiptStage.CONSUMED, generation="SIM_GEN_1",
        local_sequence=sequence, recorded_at=pilot.clock(),
        request={"StrategyID": 23}, receipt_id="SIM_RECEIPT_CONSUMED")


def test_H16_commit_latches_attempt_and_ticket_atomically(pilot):
    offer = pilot.offer()
    command = pilot.executor.commit_native_offer(offer.offer_id, pilot.challenge(offer), pilot.context)
    record = pilot.ledger.get_leg(offer.leg_id)
    assert record.attempt_no == 1 and record.state is c.OrderState.SUBMITTING
    pending = DispatchJournal(pilot.ledger).pending_commands(pilot.context.binding)
    assert [item.command_id for item in pending] == [command.command_id]
    assert pilot.native.sent_requests == []  # a ticket is not a call


def test_H16_crash_after_commit_returns_same_ticket_without_reexecution(pilot):
    offer = pilot.offer()
    first = pilot.executor.commit_native_offer(offer.offer_id, pilot.challenge(offer), pilot.context)
    again = pilot.executor.commit_native_offer(offer.offer_id, pilot.challenge(offer), pilot.context)
    assert again == first
    assert pilot.ledger.get_leg(offer.leg_id).attempt_no == 1


def test_H17_consume_at_most_once_and_retransmit_only(pilot):
    offer = pilot.offer()
    command = pilot.executor.commit_native_offer(offer.offer_id, pilot.challenge(offer), pilot.context)
    consumed = consumed_receipt(command, pilot)
    result = pilot.executor.complete_native_dispatch(consumed)
    assert result.code is c.ResultCode.OK
    duplicate = pilot.executor.complete_native_dispatch(consumed)
    assert duplicate.code is c.ResultCode.IDEMPOTENT
    record = pilot.ledger.get_leg(offer.leg_id)
    assert record.attempt_no == 1 and record.state is c.OrderState.SUBMITTING
    assert pilot.native.sent_requests == []


@pytest.mark.parametrize("kind,value,error,want", [
    (ResultKind.LOCAL_ACK, 7, None, c.OrderState.SUBMITTED_LOCAL),
    (ResultKind.LOCAL_REJECTED, None, None, c.OrderState.LOCAL_REJECTED),
    (ResultKind.INVALID_RETURN, None, "INVALID_RETURN", c.OrderState.SUBMIT_UNKNOWN),
    (ResultKind.EXCEPTION, None, "PORT_EXCEPTION", c.OrderState.SUBMIT_UNKNOWN),
])
def test_H18_return_projection_matrix(pilot, kind, value, error, want):
    offer = pilot.offer()
    command = pilot.executor.commit_native_offer(offer.offer_id, pilot.challenge(offer), pilot.context)
    consumed = consumed_receipt(command, pilot)
    pilot.executor.complete_native_dispatch(consumed)
    returned = build_receipt(
        command=command, stage=ReceiptStage.RETURNED, generation="SIM_GEN_1",
        local_sequence=2, recorded_at=pilot.clock(), request={"StrategyID": 23},
        result_kind=kind, result_value=value, error_code=error,
        previous=consumed, receipt_id="SIM_RECEIPT_RETURNED")
    result = pilot.executor.complete_native_dispatch(returned)
    assert result.leg.state is want
    assert len(pilot.native.sent_requests) == 0  # projection never calls the port


# --- NE1-H17/H31/H32 layer logic: deadline, receipts, single flight, recovery ---


def test_H17_receipt_chain_rules_and_no_call_never_fakes_rejection():
    context = simulation_context()
    command = submit_command(context)
    consumed = build_receipt(
        command=command, stage=ReceiptStage.CONSUMED, generation="SIM_GEN_1", local_sequence=1,
        recorded_at=context.clock.now, request={"StrategyID": 23}, receipt_id="SIM_R1")
    returned = build_receipt(
        command=command, stage=ReceiptStage.RETURNED, generation="SIM_GEN_1", local_sequence=2,
        recorded_at=context.clock.now, request={"StrategyID": 23},
        result_kind=ResultKind.LOCAL_ACK, result_value=7, previous=consumed, receipt_id="SIM_R2")
    assert validate_receipt_chain((consumed, returned)).ok
    assert validate_receipt_chain((returned,)).blockers
    assert validate_receipt_chain((consumed, consumed)).blockers
    with pytest.raises(c.ContractError):
        build_receipt(command=command, stage=ReceiptStage.NO_CALL, generation="SIM_GEN_1",
                      local_sequence=1, recorded_at=context.clock.now, request={"StrategyID": 23},
                      result_kind=ResultKind.LOCAL_REJECTED, receipt_id="SIM_R3")
    assert request_digest({"StrategyID": 23, "Investor": object()}) == request_digest({"StrategyID": 23})
    assert len(cancel_digest(7)) == 64


def test_H31_deadline_is_earliest_and_never_refreshed():
    now = simulation_context().clock.now
    earlier = now + timedelta(milliseconds=200)
    assert dispatch_deadline(now) == now + timedelta(milliseconds=1500)
    assert dispatch_deadline(now, earlier) == earlier
    assert dispatch_deadline(now, now + timedelta(milliseconds=2900)) == now + timedelta(milliseconds=1500)


def test_H31_single_flight_guard_and_H32_recovery_snapshot():
    context = simulation_context()
    guard = SingleFlightGuard()
    first = submit_command(context, command_id="SIM_CMD_A")
    assert guard.admit(first)
    assert not guard.admit(submit_command(context, command_id="SIM_CMD_B"))
    guard.release("SIM_CMD_A")
    assert guard.admit(submit_command(context, command_id="SIM_CMD_B"))
    now = context.clock.now
    live = submit_command(context, command_id="SIM_CMD_L", issued_at=now)
    expired = submit_command(context, command_id="SIM_CMD_E",
                             issued_at=now - timedelta(milliseconds=1600))
    assert recovery_snapshot((live, expired), now=now) == (live,)


# --- NE1-H26: replacement cannot be planned while the old leg is unknown ---


def test_H26_unknown_cancel_blocks_replacement_offer(pilot):
    offer = pilot.offer()
    pilot.executor.submit_once(offer.intent_id, offer.leg_id, offer.plan_id, pilot.context)
    record = pilot.ledger.get_leg(offer.leg_id)
    pilot.ledger.ingest_event(working_event(pilot, offer.leg_id, record.order_id, "SIM_EVT_WORK"))
    pilot.native.cancel_result = TimeoutError("simulated cancel timeout")
    pilot.executor.request_cancel("SIM_CANCEL_1", offer.intent_id, offer.leg_id, pilot.context)
    assert pilot.ledger.get_leg(offer.leg_id).cancel_state is c.CancelState.UNKNOWN
    replacement = replace(pilot.context.intent, intent_id="SIM_INTENT_REPLACE",
                          source_key="SIM_SOURCE_REPLACE")
    with pytest.raises((NativeExecutionError, LedgerError)):
        pilot.offer(replacement)
    with pytest.raises((NativeExecutionError, LedgerError)):
        pilot.executor.prepare_native_cancel("SIM_CANCEL_2", offer.intent_id, offer.leg_id,
                                             pilot.context)
    assert len(pilot.native.sent_requests) == 1
    # exactly one legitimate cancel attempt happened and its outcome is
    # unknown; no second cancel and no replacement send may follow
    assert pilot.native.cancelled_ids == [record.order_id]


# --- C1.2 (NE1-ARB-C1.2-OVF06-001): single-write seam, immutable offers ---


def test_C12_single_write_critical_path(pilot, monkeypatch):
    """DTTL-04: the ready path performs no N4 context-refresh write commits
    and exactly one dispatch transaction; every N4 step is a pure check."""
    from ak_trading.infiny_execution.executor import Executor as ExecutorType
    offer = pilot.offer()
    violations = []
    monkeypatch.setattr(ExecutorType, "latest_context",
                        lambda *args, **kwargs: violations.append("latest_context") or (_ for _ in ()).throw(
                            AssertionError("N4 context refresh on dispatch critical path")))
    monkeypatch.setattr(pilot.ledger, "compare_and_register_context",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            AssertionError("N4 CAS write on dispatch critical path")))
    calls = []
    real_begin = pilot.ledger.begin_dispatch
    monkeypatch.setattr(pilot.ledger, "begin_dispatch",
                        lambda *args, **kwargs: calls.append(1) or real_begin(*args, **kwargs))
    command = pilot.executor.commit_native_offer(offer.offer_id, pilot.challenge(offer), pilot.context)
    assert len(calls) == 1
    assert pilot.ledger.get_leg(offer.leg_id).attempt_no == 1
    assert command.leg_id == offer.leg_id


def test_C12_offer_expiry_immutable_across_poll_ready(pilot):
    """DTTL-02: repeated issuance of the same offer id keeps the first
    absolute deadline; a later clock never extends it, and /ready inherits."""
    first = pilot.offer()
    pilot.clock.advance(D("0.400"))
    again = pilot.offer()
    assert again == first  # identical immutable persisted server state
    command = pilot.executor.commit_native_offer(first.offer_id, pilot.challenge(first), pilot.context)
    assert command.expires_at <= first.expires_at  # inherits, never extends


def test_C12_command_deadline_inherits_offer_not_issue_time(pilot):
    """DTTL-03: the command deadline is min(offer, t_issue+1500, D_current):
    issuing 400 ms after the offer must not move the deadline later."""
    from ak_trading.infiny_execution.native_contracts import DISPATCH_TTL_MS
    offer = pilot.offer()
    pilot.clock.advance(D("0.400"))
    command = pilot.executor.commit_native_offer(offer.offer_id, pilot.challenge(offer), pilot.context)
    assert command.expires_at == offer.expires_at
    assert (command.expires_at - command.issued_at).total_seconds() * 1000 < DISPATCH_TTL_MS


@pytest.mark.parametrize("lifetime_ms,constructs,admission", [
    (500, True, True),            # old 500 ms ticket remains valid
    (500.001, True, True),
    (1499.999, True, True),
    (1500, True, True),           # exactly the frozen cap is valid
    (1500.001, False, False),     # larger than the cap cannot even encode
])
def test_C12_boundary_lifetimes(lifetime_ms, constructs, admission):
    from ak_trading.infiny_execution.native_contracts import validate_dispatch_admission
    context = simulation_context()
    now = context.clock.now
    issued = submit_command(context, issued_at=now - timedelta(milliseconds=10))
    expires = now - timedelta(milliseconds=10) + timedelta(milliseconds=lifetime_ms)
    if constructs:
        command = replace(issued, expires_at=expires)
        result = validate_dispatch_admission(command, expires, now=now)
        assert result.ok is admission
        assert validate_dispatch_admission(
            command, expires, now=expires).blockers == ("EXPIRED",)  # equality is expired
    else:
        with pytest.raises(c.ContractError):
            replace(issued, expires_at=expires)


def test_C12_calculator_semantics():
    """Frozen DTTL-02/03 calculators: earliest wins, never renewed, a command
    never outlives its offer, and fresh observations only tighten."""
    from ak_trading.infiny_execution.native_contracts import (
        compute_command_deadline, compute_offer_deadline, validate_dispatch_admission)
    now = simulation_context().clock.now
    tight = now + timedelta(milliseconds=200)
    loose = now + timedelta(seconds=30)
    assert compute_offer_deadline(now, (loose, tight)) == tight
    assert compute_offer_deadline(now, (loose,)) == now + timedelta(milliseconds=1500)
    offer_at = now + timedelta(milliseconds=600)
    assert compute_command_deadline(offer_at, now, (loose,)) == offer_at  # inherits offer cap
    assert compute_command_deadline(offer_at, now, (tight,)) == tight     # D_current tightens
    command = replace(submit_command(simulation_context(), issued_at=now),
                      expires_at=now + timedelta(milliseconds=1000))
    assert validate_dispatch_admission(command, now + timedelta(milliseconds=1000), now=now).ok
    late = replace(command, expires_at=now + timedelta(milliseconds=1200))
    assert "COMMAND_OUTLIVES_OFFER" in validate_dispatch_admission(
        late, now + timedelta(milliseconds=1000), now=now).blockers


@pytest.mark.parametrize("mutate", ["control_revision", "authorization"])
def test_C12_competing_writer_leaves_no_partial_state(pilot, tmp_path, mutate):
    """A competing durable control change before commit rejects atomically:
    zero attempts, zero tickets, the reserved leg untouched."""
    offer = pilot.offer()
    journal = Ledger(tmp_path / "pilot.sqlite", clock=pilot.clock)
    try:
        if mutate == "control_revision":
            journal.register_control_update(
                synthetic_control_state(pilot.context, control_revision=offer.control_revision + 1))
        else:
            journal.register_context(replace(pilot.context, authorization=None))
    finally:
        journal.close()
    before = pilot.ledger.check().revision
    with pytest.raises((NativeExecutionError, LedgerError)):
        pilot.executor.commit_native_offer(offer.offer_id, pilot.challenge(offer), pilot.context)
    record = pilot.ledger.get_leg(offer.leg_id)
    assert record.attempt_no == 0 and record.state is c.OrderState.RESERVED
    assert DispatchJournal(pilot.ledger).pending_commands(pilot.context.binding) == ()
    assert not pilot.ledger.check().reconcile.required


def test_C12_post_commit_expiry_suppresses_reissue_and_refunds_nothing(pilot):
    """DTTL-05/06: after the committed command expires, idempotent re-commit
    returns the same ticket (never renewed), recovery drops it from the live
    view, and a NO_CALL receipt projects conservatively without rearming."""
    from ak_trading.infiny_execution.client_dispatch import build_receipt, recovery_snapshot
    offer = pilot.offer()
    command = pilot.executor.commit_native_offer(offer.offer_id, pilot.challenge(offer), pilot.context)
    pilot.clock.advance(D("1.600"))  # beyond the frozen window
    # a replayed /ready after expiry must fail closed: no renewal, no reissue,
    # no second transaction; same-ticket idempotence holds only inside the
    # validity window (covered by the crash-window counterexample)
    with pytest.raises((NativeExecutionError, LedgerError), match="DISPATCH_EXPIRED"):
        pilot.executor.commit_native_offer(offer.offer_id, pilot.challenge(offer), pilot.context)
    assert pilot.ledger.get_leg(offer.leg_id).attempt_no == 1
    assert [item.command_id for item in
            DispatchJournal(pilot.ledger).pending_commands(pilot.context.binding)] == [command.command_id]
    assert recovery_snapshot((command,), now=pilot.clock()) == ()
    consumed = build_receipt(
        command=command, stage=ReceiptStage.CONSUMED, generation="SIM_GEN_1",
        local_sequence=1, recorded_at=pilot.clock(), request={"StrategyID": 23},
        receipt_id="SIM_RECEIPT_LATE")
    assert pilot.executor.complete_native_dispatch(consumed).code is c.ResultCode.OK
    no_call = build_receipt(
        command=command, stage=ReceiptStage.NO_CALL, generation="SIM_GEN_1",
        local_sequence=2, recorded_at=pilot.clock(), request={"StrategyID": 23},
        previous=consumed, receipt_id="SIM_RECEIPT_NOCALL")
    result = pilot.executor.complete_native_dispatch(no_call)
    assert result.leg.attempt_no == 1  # committed facts stay; expiry refunds nothing
    assert pilot.native.sent_requests == []


# --- n4-r1 review blockers B1/B2 (DTTL-02) — reviewer probes P7/P8 ---


def test_B1_P8_authorization_deadline_bounds_offer(pilot):
    """P8: an authorization expiring at t0+800ms must bound the offer to
    t0+800ms; the full 1500ms budget is never granted past it."""
    expire_at = pilot.clock() + timedelta(milliseconds=800)
    tighter = replace(pilot.context,
                      authorization=replace(pilot.context.authorization, expires_at=expire_at))
    pilot.ledger.register_context(tighter)
    pilot.context = pilot.ledger.context(pilot.context.binding)  # adopt receipt-bearing state
    offer = pilot.offer()
    assert offer.expires_at == expire_at
    command = pilot.executor.commit_native_offer(offer.offer_id, pilot.challenge(offer), pilot.context)
    assert command.expires_at <= expire_at


def test_B2_P7_recovery_rebuild_never_extends_first_deadline(tmp_path):
    """P7: a fresh executor over the same journal 400ms later must rebuild the
    identical deterministic deadline; the committed command never outlives the
    first offer's immutable expiry."""
    path = tmp_path / "rebuild.sqlite"
    first = Pilot(path)
    original = first.offer()          # first issuance; deliberately not committed
    first_deadline = original.expires_at
    first.close()
    recovered = Pilot(path)
    recovered.clock.advance(D("0.400"))
    try:
        rebuilt = recovered.offer()
        assert rebuilt.offer_id == original.offer_id
        assert rebuilt.expires_at == first_deadline          # deterministic, never later
        replay = recovered.executor.commit_native_offer(
            rebuilt.offer_id, recovered.challenge(rebuilt), recovered.context)
        assert replay.expires_at <= first_deadline           # inherits, never extends
    finally:
        recovered.close()


# --- C1.3 RD-01..03: permit deadline joins D_original via the registry ---


class _DeadlineRegistry(FakeEvidenceRegistry):
    """Local SYNTHETIC registry with a fixed applicable deadline."""

    def __init__(self, deadline):
        super().__init__()
        self._deadline = deadline

    def applicable_deadline(self, permit_id, *, action, now):
        from ak_trading.infiny_execution.native_evidence import EvidenceDeadlineResult
        return EvidenceDeadlineResult(validation=c.ValidationResult(blockers=()),
                                      deadline=self._deadline)


class _UnavailableRegistry(FakeEvidenceRegistry):
    """Keeps the C1.3 base fail-closed answer."""

    def applicable_deadline(self, permit_id, *, action, now):
        from ak_trading.infiny_execution.native_evidence import EvidenceDeadlineResult
        return EvidenceDeadlineResult(
            validation=c.ValidationResult(blockers=("NATIVE_DEADLINE_UNAVAILABLE",)),
            deadline=None)


def test_C13_permit_deadline_joins_D_original_and_fails_closed(pilot):
    from ak_trading.infiny_execution.native_contracts import ControlState, ExecutionMode
    prepared = pilot.executor.prepare(pilot.context.intent, pilot.context)
    tight = pilot.clock() + timedelta(milliseconds=600)
    pilot.executor._evidence_registry = _DeadlineRegistry(tight)
    # probe-level pilot state: mode/context consistency is enforced upstream in
    # _require_control_state, not inside the deadline helper under test
    pilot.executor._control_state = ControlState(
        control_revision=pilot.control.control_revision, binding=pilot.control.binding,
        mode=ExecutionMode.NATIVE, policy_id=pilot.control.policy_id,
        authorization_id=pilot.control.authorization_id, native_permit_id="SIM_PERMIT_TIGHT",
        params_sha256=pilot.control.params_sha256, generation=pilot.control.generation,
        updated_at=pilot.control.updated_at, disabled_reason=None)
    assert pilot.executor._first_deadline(prepared, pilot.context, pilot.clock()) == tight
    pilot.executor._evidence_registry = _UnavailableRegistry()
    with pytest.raises(NativeExecutionError, match="NATIVE_DEADLINE_UNAVAILABLE"):
        pilot.executor._first_deadline(prepared, pilot.context, pilot.clock())


# --- n4-r2 review F1 / R2-P13: H07 future-skew cannot lift the offer cap ---


def test_F1_P13_future_skew_boundary_keeps_offer_within_cap(pilot):
    """R2-P13: with sampled_at exactly at the tolerated future_skew (2 s),
    the offer deadline stays within t_offer + 1500 ms instead of riding the
    skewed anchor up to t_offer + 3500 ms; zero-skew keeps the exact budget."""
    from ak_trading.infiny_execution.native_contracts import DISPATCH_TTL_MS
    t0 = pilot.clock()
    skew = timedelta(seconds=2)
    snapshot = pilot.context.snapshot
    shifted = replace(snapshot, snapshot_id="SIM_SNAPSHOT_SKEWED",
                      times=replace(snapshot.times,
                                    sampled_at=snapshot.times.sampled_at + skew,
                                    sent_at=snapshot.times.sent_at + skew,
                                    received_at=snapshot.times.received_at + skew))
    skewed = replace(pilot.context, snapshot=shifted)
    pilot.ledger.register_context(skewed)
    pilot.context = skewed
    offer = pilot.offer()
    assert offer.expires_at <= t0 + timedelta(milliseconds=DISPATCH_TTL_MS)
    assert (offer.expires_at - t0).total_seconds() * 1000 <= DISPATCH_TTL_MS + 0.001


# --- n4-r3 review F2 / C1.4 OFD Option A: durable first-offer deadline ---


def test_F2_P14_recovery_rebuild_reads_persisted_bytes(tmp_path):
    """R3-P14: a fresh executor over the same journal 3 s later rebuilds the
    offer from the persisted record — the first deadline returns
    byte-identical with zero recomputation, whatever the recovery offset."""
    path = tmp_path / "ofd-rebuild.sqlite"
    first = Pilot(path)
    original = first.offer()
    first_deadline = original.expires_at
    stored = first.executor._dispatch_journal.get_offer(original.offer_id)
    assert stored is not None and stored.offer == original
    first.close()
    recovered = Pilot(path)
    recovered.clock.advance(D("3.000"))
    try:
        rebuilt = recovered.offer()
        assert rebuilt.offer_id == original.offer_id
        assert rebuilt == original                    # persisted bytes, not recomputed
        assert rebuilt.expires_at == first_deadline   # immovable across any offset
    finally:
        recovered.close()


# --- n4-r4 review F3/F4 (OFD-01): submit data-anchor cap, cancel bounds ---


def test_F3_P15_stale_snapshot_packet_cannot_get_full_budget(tmp_path):
    """R3-P15: when the snapshot packet is older than first issuance by more
    than the frozen budget, the submit offer's deadline tightens to
    min(sampled_at, t_first) + 1500 ms instead of t_first + 1500 ms."""
    from ak_trading.infiny_execution.native_contracts import DISPATCH_TTL_MS
    item = Pilot(tmp_path / "anchor.sqlite")
    try:
        item.clock.advance(D("1.000"))  # first issuance 1 s after the packet
        offer = item.offer()
        t_first = item.clock()
        sampled = item.context.snapshot.times.sampled_at
        expected_cap = min(sampled, t_first) + timedelta(milliseconds=DISPATCH_TTL_MS)
        assert offer.expires_at == expected_cap
        assert offer.expires_at < t_first + timedelta(milliseconds=DISPATCH_TTL_MS)
    finally:
        item.close()


def test_F4_cancel_offer_never_bounded_by_expired_plan(pilot):
    """CANCEL-D-01: an owned-cancel offer is still issued and persisted even
    when the underlying plan expired long ago; its bounds are the intent /
    authorization / permit deadlines plus the frozen budget, never the plan."""
    from ak_trading.infiny_execution.native_contracts import DISPATCH_TTL_MS
    offer = pilot.offer()
    pilot.executor.submit_once(offer.intent_id, offer.leg_id, offer.plan_id, pilot.context)
    record = pilot.ledger.get_leg(offer.leg_id)
    pilot.ledger.ingest_event(working_event(pilot, offer.leg_id, record.order_id, "SIM_EVT_F4"))
    # age far past any plan deadline (packet TTL bounded it near issuance)
    pilot.clock.advance(D("60"))
    cancel_offer = pilot.executor.prepare_native_cancel(
        "SIM_CANCEL_F4", offer.intent_id, offer.leg_id, pilot.context)
    t_first = pilot.clock()
    assert t_first < cancel_offer.expires_at <= t_first + timedelta(milliseconds=DISPATCH_TTL_MS)
    stored = pilot.executor._dispatch_journal.get_offer(cancel_offer.offer_id)
    assert stored is not None and stored.offer == cancel_offer


# --- C1.5 AP-01: single preparation transaction on the offer path ---


def test_C15_prepare_path_is_exactly_two_transactions(pilot, monkeypatch):
    """AP-01/AP-03: a fresh first offer performs exactly one N3 preparation
    transaction plus one independent C1.4 record_offer transaction — no
    separate N4 register/CAS/reserve commits."""
    from ak_trading.infiny_execution.executor import Executor as ExecutorType

    def forbidden(*args, **kwargs):
        raise AssertionError("six-write preparation path used on the seam route")

    monkeypatch.setattr(pilot.executor, "prepare", forbidden)
    monkeypatch.setattr(pilot.ledger, "register", forbidden)
    monkeypatch.setattr(pilot.ledger, "register_context", forbidden)
    monkeypatch.setattr(pilot.ledger, "compare_and_register_context", forbidden)
    monkeypatch.setattr(pilot.ledger, "reserve", forbidden)
    calls = {"prepare": 0, "record": 0}
    real_prepare = pilot.ledger.prepare_native_submission
    real_record = pilot.ledger.record_offer

    def counting_prepare(*args, **kwargs):
        calls["prepare"] += 1
        return real_prepare(*args, **kwargs)

    def counting_record(*args, **kwargs):
        calls["record"] += 1
        return real_record(*args, **kwargs)

    monkeypatch.setattr(pilot.ledger, "prepare_native_submission", counting_prepare)
    monkeypatch.setattr(pilot.ledger, "record_offer", counting_record)
    offer = pilot.offer()
    assert calls == {"prepare": 1, "record": 1}
    assert offer.expires_at > pilot.clock()
    # the idempotent second issuance only reads persisted offer bytes: no
    further = pilot.offer()
    assert calls == {"prepare": 2, "record": 1}  # seam re-derives idempotently
    assert further == offer


def test_C15_two_writes_hold_when_adopting_new_observation(tmp_path, monkeypatch):
    """n3-r7 B1-r6 repair: when the caller carries a NEWER observation, the
    seam adopts it inside the same single transaction — the first offer over
    a fresh observation still costs exactly two writes (seam + record)."""
    item = Pilot(tmp_path / "adopt.sqlite")
    try:
        item.clock.advance(D("0.250"))
        newer = item.ledger.context(item.context.binding)
        from ak_trading.infiny_execution.simulation import FakeClock
        # a fresher market observation registered as an explicit trusted update
        from dataclasses import replace as _replace
        fresh = _replace(newer.snapshot, snapshot_id="SIM_SNAPSHOT_ADOPT")
        adopted = _replace(newer, snapshot=fresh)
        item.ledger.register_context(adopted)
        item.context = item.ledger.context(item.context.binding)
        calls = {"prepare": 0, "record": 0}
        real_prepare = item.ledger.prepare_native_submission
        real_record = item.ledger.record_offer

        def counting_prepare(*args, **kwargs):
            calls["prepare"] += 1
            return real_prepare(*args, **kwargs)

        def counting_record(*args, **kwargs):
            calls["record"] += 1
            return real_record(*args, **kwargs)

        monkeypatch.setattr(item.ledger, "prepare_native_submission", counting_prepare)
        monkeypatch.setattr(item.ledger, "record_offer", counting_record)
        offer = item.executor.prepare_native_offer(item.context.intent, item.context)
        assert calls == {"prepare": 1, "record": 1}
        assert offer.snapshot_id == "SIM_SNAPSHOT_ADOPT"
    finally:
        item.close()

