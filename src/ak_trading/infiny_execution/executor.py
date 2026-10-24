"""Serial execution over the N3 durable journal; only the offline fake can send."""
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from threading import RLock, get_ident
from typing import Callable

from . import contracts as c
from .ledger import Ledger, LedgerError, SubmitResult
from .native import NativeExecutionError, build_native_request
from .native_contracts import ExecutionOffer, ReadyChallenge, DispatchCommand, DispatchReceipt
from .planning import plan, validate_plan


@dataclass(frozen=True, slots=True, kw_only=True)
class PreparedExecution:
    plan: c.Plan
    context: c.ExecutionContext


class Executor:
    """A callback-thread assembler, not an automatic trading worker.

    The port is never resolved in disabled/native mode. Native capability is
    absent, rather than inferred from caller-supplied proof or authorization.
    SQLite transactions end before each external call; attempts survive process
    loss and are never retried by this class.
    """

    def __init__(self, ledger: Ledger, *, port: object = None,
                 mode: c.ExecutionMode = c.ExecutionMode.DISABLED,
                 binding: c.Binding | None = None, investor_handle: object = None,
                 clock: Callable[[], datetime] | None = None,
                 evidence_registry: object = None, control_state: object = None,
                 dispatch_journal: object = None):
        if type(ledger) is not Ledger:
            raise TypeError("Executor requires the persistent Ledger")
        self._ledger = ledger
        self._port = port
        self._mode = mode
        self._binding = binding
        self._investor_handle = investor_handle
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._owner_thread_id = get_ident()
        self._lock = RLock()
        # Native orchestration dependencies. Defaults keep everything blocked:
        # production evidence cannot be registered before the C3 trusted
        # assembler, and the N3 dispatch journal owns atomic latching.
        from .native_evidence import ProductionEvidenceRegistry
        self._evidence_registry = evidence_registry if evidence_registry is not None else ProductionEvidenceRegistry()
        self._control_state = control_state
        self._dispatch_journal = dispatch_journal
        self._offers: dict[str, "ExecutionOffer"] = {}

    def disable(self) -> None:
        """Stop new attempts; leave callbacks, journal and reservations intact."""
        with self._lock:
            self._mode = c.ExecutionMode.DISABLED

    def _gate(self, context: c.ExecutionContext) -> None:
        # These checks precede imports or any attribute/method lookup on port.
        if self._mode is c.ExecutionMode.DISABLED:
            raise NativeExecutionError("MODE_DISABLED")
        if self._mode is not c.ExecutionMode.SIMULATION:
            raise NativeExecutionError("NATIVE_DISABLED")
        if type(context) is not c.ExecutionContext:
            raise c.ContractError("expected ExecutionContext")
        if context.mode is not c.ExecutionMode.SIMULATION:
            raise NativeExecutionError("MODE_DISABLED")
        if (get_ident() != self._owner_thread_id or context.owner_thread_id != self._owner_thread_id or
                context.current_thread_id != get_ident()):
            raise NativeExecutionError("THREAD_INVALID")
        if context.binding != self._binding:
            raise NativeExecutionError("OWNERSHIP_CONFLICT")
        if context.snapshot.source is not c.Source.SIMULATION:
            raise NativeExecutionError("NATIVE_DISABLED")
        from .simulation import FakeNative
        if type(self._port) is not FakeNative:
            raise NativeExecutionError("NATIVE_DISABLED")
        if (self._port.binding != self._binding or
                self._port.investor_handle is not self._investor_handle):
            raise NativeExecutionError("ACCOUNT_MISMATCH")

    def persisted_context(self, context: c.ExecutionContext) -> c.ExecutionContext:
        """Read the durable control state for this exact binding.

        The scope index may already hold a different epoch/day; anything other
        than a full binding and control match rejects. An absent scope is a
        rejection, never initialization permission for an ordinary path.
        """
        current = self._ledger.context(context.binding)
        if current is None:
            raise NativeExecutionError("CONTEXT_REQUIRED")
        if (current.binding != context.binding or current.mode != context.mode or
                current.policy != context.policy or current.authorization != context.authorization or
                current.route_ownership != context.route_ownership):
            raise NativeExecutionError("AUTHORIZATION_REQUIRED")
        return current

    def latest_context(self, context: c.ExecutionContext, *,
                       receipt: c.ReservationReceipt | None = None,
                       intent: c.OrderIntent | None = None,
                       expected_context: c.ExecutionContext) -> c.ExecutionContext:
        """CAS-register a refreshed observation against persisted controls.

        The caller must supply the controls-equivalent expected value obtained
        from persisted_context() in the same operation. Registering without
        that comparison would let one connection roll back another's concurrent
        revocation, budget reduction, disable or epoch change.
        """
        if expected_context is None:
            raise NativeExecutionError("CONTEXT_REQUIRED")
        self._gate(context)
        previous = self._ledger.context(context.binding)
        if previous is not None and previous.snapshot.snapshot_id != context.snapshot.snapshot_id:
            prior_time = previous.snapshot.times.sampled_at
            incoming_time = context.snapshot.times.sampled_at
            if prior_time is not None and (incoming_time is None or incoming_time < prior_time):
                raise NativeExecutionError("SNAPSHOT_STALE")
        status = self._ledger.check()
        snapshot = replace(context.snapshot, ledger_revision=status.revision)
        ready = context.readiness
        if status.reconcile.required:
            ready = replace(ready, state=c.ReadinessState.RECONCILE_REQUIRED,
                            can_submit=False, blockers=tuple(dict.fromkeys(
                                (*ready.blockers, "RECONCILE_REQUIRED"))))
        refreshed = replace(context, snapshot=snapshot, ledger_revision=status.revision,
                            clock=replace(context.clock, now=self._clock()),
                            current_thread_id=get_ident(), reconcile=status.reconcile,
                            readiness=ready, reservation_view=None,
                            intent=intent if intent is not None else context.intent,
                            reservation_receipt=receipt if receipt is not None else context.reservation_receipt)
        self._ledger.compare_and_register_context(refreshed, expected_context)
        view = self._ledger.view(context.binding.account_key, snapshot_id=snapshot.snapshot_id)
        updated = replace(refreshed, reservation_view=view)
        self._ledger.compare_and_register_context(updated, refreshed)
        return updated

    def refresh(self, context: c.ExecutionContext, *,
                receipt: c.ReservationReceipt | None = None,
                intent: c.OrderIntent | None = None) -> c.ExecutionContext:
        """Ordinary refresh entry: compare persisted controls, then CAS."""
        expected = self.persisted_context(context)
        return self.latest_context(context, receipt=receipt, intent=intent, expected_context=expected)

    def readiness(self, context: c.ExecutionContext) -> c.Readiness:
        """Describe capabilities using the current journal, including foreign orders."""
        context = self.refresh(context)
        blockers = list(c.validate_context(context, self._clock()).blockers)
        proof = context.proof
        records = tuple(self._ledger.get_leg(r.leg_id) for r in context.reservation_view.reservations)
        if proof is None or context.snapshot.coverage.level is not c.CoverageLevel.FULL_ACCOUNT:
            blockers.append("PROOF_REQUIRED")
        else:
            for order in proof.order_inventory or ():
                if not order.active:
                    continue
                matches = [r for r in records if r.ownership_verified and
                           (r.intent.binding.account_key, r.intent.binding.trading_day,
                            r.intent.binding.exchange, r.intent.binding.instrument_id, r.intent.binding.hedge_flag,
                            r.leg.direction, r.leg.offset) ==
                           (order.account_key, order.trading_day, order.exchange, order.instrument_id,
                            order.hedge_flag, order.direction, order.offset) and
                           (order.order_id is not None and r.order_id == order.order_id or
                            order.order_sys_id is not None and r.order_sys_id == order.order_sys_id)]
                if len(matches) != 1:
                    blockers.append("BASELINE_GAP")
        pending = True
        if context.reservation_receipt is not None:
            owned = tuple(self._ledger.get_leg(identity) for identity in context.reservation_receipt.reservation_ids)
            unattempted = tuple(r for r in owned if r.attempt_no == 0 and r.state is c.OrderState.RESERVED)
            pending = bool(unattempted)
            if unattempted:
                blockers.extend(validate_plan(unattempted[0].plan, context, self._clock()).blockers)
        elif context.intent is not None:
            blockers.extend(plan(context.intent, context.snapshot, context.reservation_view,
                                 context.policy, self._clock()).blockers)
        else:
            blockers.append("INTENT_REQUIRED")
        # Readiness has a closed vocabulary; detailed timing codes remain in
        # ValidationResult, represented here by their component stale blocker.
        normalized = tuple(dict.fromkeys(code if code in c.BLOCKER_CODES else
                           "QUOTE_STALE" if code.startswith("QUOTE_") else "SNAPSHOT_STALE"
                           for code in blockers))
        required = context.reconcile.required or "BASELINE_GAP" in normalized
        state = c.ReadinessState.RECONCILE_REQUIRED if required else (
            c.ReadinessState.PLAN_READY if normalized or not pending else c.ReadinessState.EXECUTABLE)
        cancel_valid = c.validate_context(context, self._clock(), action=c.AuthorizationAction.CANCEL_OWNED).ok
        cancel_available = any(r.ownership_verified and r.order_id is not None and
                               r.intent.binding == context.binding and r.state not in c.TERMINAL_ORDER_STATES and
                               r.risk_filled < r.leg.volume and r.cancel_state is c.CancelState.NONE for r in records)
        return replace(context.readiness, state=state, can_submit=not normalized and pending,
                       can_cancel_owned=cancel_valid and cancel_available,
                       blockers=normalized, coverage=context.snapshot.coverage)

    def prepare(self, intent: c.OrderIntent, context: c.ExecutionContext, *,
                resources: tuple[c.ExecutionSnapshot, ...] = ()) -> PreparedExecution:
        """register -> persisted-control comparison -> trusted view -> plan -> reserve.

        A scope with no registered context is never initialized here: initial
        control registration belongs to the explicit assembler entry, so a
        concurrent registration from another connection cannot be rolled back
        by an ordinary planning call.
        """
        with self._lock:
            self._gate(context)
            if intent.binding != context.binding or (context.intent is not None and context.intent != intent):
                raise NativeExecutionError("OWNERSHIP_CONFLICT")
            expected = self.persisted_context(context)
            registered = self._ledger.register(intent)
            context = replace(context, intent=registered.intent, reservation_view=None)
            context = self.latest_context(context, intent=registered.intent, expected_context=expected)
            if resources:
                self._ledger.compare_and_register_context(context, context, resources=resources)
            if registered.code is c.ResultCode.IDEMPOTENT:
                for reservation in context.reservation_view.reservations:
                    existing = self._ledger.get_leg(reservation.leg_id)
                    if c.canonical_hash(existing.intent) == registered.intent_hash:
                        receipt = self._ledger.reserve(existing.plan, context.ledger_revision)
                        context = self.latest_context(context, receipt=receipt, intent=registered.intent,
                                                      expected_context=context)
                        return PreparedExecution(plan=existing.plan, context=context)
            proposed = plan(registered.intent, context.snapshot, context.reservation_view,
                            context.policy, self._clock())
            if proposed.blockers:
                raise LedgerError(c.ResultCode.BLOCKED, proposed.blockers)
            receipt = self._ledger.reserve(proposed, context.ledger_revision)
            context = self.latest_context(context, receipt=receipt, intent=registered.intent,
                                          expected_context=context)
            return PreparedExecution(plan=proposed, context=context)

    def _check_original(self, record, intent_id, plan_id, context):
        if (record.intent.intent_id != intent_id or record.plan.plan_id != plan_id or
                record.intent.binding != context.binding or context.intent is None or
                c.canonical_hash(context.intent) != c.canonical_hash(record.intent) or
                record.plan.intent_hash != c.canonical_hash(record.intent)):
            raise NativeExecutionError("OWNERSHIP_CONFLICT")
        receipt = context.reservation_receipt
        if (receipt is None or receipt.plan_id != plan_id or
                receipt.reservation_ids != tuple(leg.leg_id for leg in record.plan.legs)):
            raise NativeExecutionError("RESERVATION_VIEW_REQUIRED")

    def _dependencies(self, record, context):
        if not record.leg.depends_on:
            return
        for leg_id in record.leg.depends_on:
            previous = self._ledger.get_leg(leg_id)
            if (previous.state is not c.OrderState.FILLED or not previous.settled or
                    previous.risk_filled != previous.leg.volume or previous.trade_gap or
                    previous.reconcile_required or previous.cancel_state in
                    (c.CancelState.REQUESTED, c.CancelState.UNKNOWN)):
                raise NativeExecutionError("TRADE_GAP")
        original_side = (c.Side.LONG if record.intent.action is c.Action.REVERSE_TO_SHORT else c.Side.SHORT)
        positions = [p for p in context.snapshot.positions if p.side is original_side]
        proof = context.proof
        if (len(positions) != 1 or positions[0].Position != 0 or
                proof is None or not proof.verified or
                proof.position_snapshot_id != context.snapshot.snapshot_id or
                not set(record.leg.depends_on).issubset(proof.covered_leg_ids)):
            raise NativeExecutionError("PROOF_REQUIRED")

    def refresh_pending_open(self, leg_id: str, context: c.ExecutionContext) -> PreparedExecution:
        """Reprice the resource bound in N3's transaction; keep order terms frozen."""
        with self._lock:
            self._gate(context)
            record = self._ledger.get_leg(leg_id)
            self._check_original(record, record.intent.intent_id, record.plan.plan_id, context)
            expected = self.persisted_context(context)
            context = self.latest_context(context, intent=record.intent, expected_context=expected)
            self._dependencies(record, context)
            receipt = self._ledger.refresh_pending_open(leg_id, context)
            context = self.latest_context(context, receipt=receipt, intent=record.intent, expected_context=context)
            return PreparedExecution(plan=self._ledger.get_leg(leg_id).plan, context=context)

    def submit_once(self, intent_id: str, leg_id: str, plan_id: str,
                    context: c.ExecutionContext) -> SubmitResult:
        with self._lock:
            self._gate(context)
            record = self._ledger.get_leg(leg_id)
            self._check_original(record, intent_id, plan_id, context)
            if record.attempt_no:
                return SubmitResult(code=c.ResultCode.IDEMPOTENT, leg=record,
                                    revision=self._ledger.check().revision)
            record, context, request = self._preflight_leg(record, context)
            self._gate(context)
            attempt = self._ledger.begin_attempt(leg_id, context)
            if not attempt.send_permitted:
                return SubmitResult(code=c.ResultCode.IDEMPOTENT, leg=attempt.leg, revision=attempt.revision)
            # The journal has committed SUBMITTING before resolving the method.
            # BaseException deliberately propagates as process-loss simulation:
            # the durable latch, not a Python boolean, prevents a later resend.
            try:
                result = self._port.send_order(request)
            except Exception:
                return self._ledger.record_submit_result(leg_id, error_code="PORT_EXCEPTION")
            return self._ledger.record_submit_result(leg_id, result)

    def _preflight_leg(self, record, context):
        """Shared submission preflight: persisted controls -> CAS refresh ->
        dependency barrier -> plan revalidation -> pure request mapping.

        This is the single precheck reused by the direct simulation port path
        and the two-phase native offer path; it never touches a port.
        """
        expected = self.persisted_context(context)
        context = self.latest_context(context, intent=record.intent, expected_context=expected)
        self._dependencies(record, context)
        validation = validate_plan(record.plan, context, self._clock())
        if not validation.ok:
            raise LedgerError(c.ResultCode.BLOCKED, validation.blockers)
        contract = context.snapshot.contract
        request = build_native_request(
            record.leg, context.binding, self._investor_handle,
            order_type=record.intent.order_type,
            price_tick=contract.price_tick if contract is not None else None,
            verified_hedge_flags=self._port.verified_hedge_flags)
        return record, context, request

    def request_cancel(self, cancel_id: str, intent_id: str, leg_id: str,
                       context: c.ExecutionContext) -> SubmitResult:
        with self._lock:
            attempt = self._cancel_preflight(cancel_id, intent_id, leg_id, context)
            if not attempt.cancel_permitted:
                return SubmitResult(code=c.ResultCode.IDEMPOTENT, leg=attempt.leg, revision=attempt.revision)
            try:
                result = self._port.cancel_order(attempt.order_id)
            except Exception:
                return self._ledger.record_cancel_result(cancel_id, error_code="PORT_EXCEPTION")
            return self._ledger.record_cancel_result(cancel_id, result)

    def _cancel_preflight(self, cancel_id, intent_id, leg_id, context):
        """Shared cancel preflight: controls, route and the durable REQUESTED
        latch, without any port call (reused by the two-phase cancel offer)."""
        with self._lock:
            self._gate(context)
            record = self._ledger.get_leg(leg_id)
            if record.intent.intent_id != intent_id or record.intent.binding != context.binding:
                raise NativeExecutionError("OWNERSHIP_CONFLICT")
            expected = self.persisted_context(context)
            context = self.latest_context(context, intent=record.intent, expected_context=expected)
            # Cancel validation is deliberately independent of quote/policy TTL,
            # full-account proof and submit readiness; N3 verifies the route.
            validation = c.validate_context(context, self._clock(), action=c.AuthorizationAction.CANCEL_OWNED)
            if not validation.ok:
                raise LedgerError(c.ResultCode.BLOCKED, validation.blockers)
            return self._ledger.request_cancel(cancel_id, intent_id, leg_id, context)

    # ------------------------------------------------------------------
    # Two-phase native orchestration (NE1 plan §5.2). This Executor never
    # touches a native port: the client performs the single call inside a
    # verified dispatch admission. Capability originates from the trusted
    # assembler's ControlState/permit and never from a mode string; atomic
    # attempt latching, permit consumption and the durable dispatch ticket
    # belong to N3's DispatchJournal inside one BEGIN IMMEDIATE.
    # ------------------------------------------------------------------

    def _require_control_state(self, context: c.ExecutionContext):
        state = self._control_state
        if state is None:
            raise NativeExecutionError("CONTROL_STATE_REQUIRED")
        authorization_id = context.authorization.authorization_id if context.authorization is not None else None
        if (state.binding != context.binding or state.mode != context.mode or
                state.policy_id != context.policy.policy_id or
                state.authorization_id != authorization_id):
            raise NativeExecutionError("CONTROL_STATE_MISMATCH")
        return state

    def _require_permit(self, state):
        if state.mode is not c.ExecutionMode.NATIVE:
            return None
        if state.native_permit_id is None:
            raise NativeExecutionError("NATIVE_PERMIT_UNREGISTERED")
        result = self._evidence_registry.verify_permit_active(state.native_permit_id, now=self._clock())
        if not result.ok:
            raise NativeExecutionError(*result.blockers)
        return state.native_permit_id

    def _permit_deadline(self, action: c.AuthorizationAction):
        """C1.3 RD-01..03: the pilot permit/evidence applicability bound.

        Consumption is fail-closed: an unavailable/unverifiable registry
        deadline blocks the offer instead of guessing one. Simulation scopes
        carry no permit and therefore no registry deadline.
        """
        permit_id = getattr(self._control_state, "native_permit_id", None)
        if permit_id is None:
            return None
        result = self._evidence_registry.applicable_deadline(
            permit_id, action=action, now=self._clock())
        if not result.validation.ok:
            raise NativeExecutionError(*result.validation.blockers)
        return result.deadline

    def prepare_native_offer(self, intent: c.OrderIntent, context: c.ExecutionContext, *,
                             resources: tuple = ()) -> ExecutionOffer:
        """Shared preflight through a durable plan; an offer has no call right.

        C1.4 OFD Option A: the first offer's absolute deadline is durable
        journal state. Recovery rebuilds read the persisted bytes through
        get_offer — zero recomputation, so no anchor can ever drift (F2);
        first issuance computes E_first once from t_offer + the applicable
        deadlines and persists it atomically via record_offer insert-or-read.
        """
        from .native_contracts import CommandKind, compute_offer_deadline
        with self._lock:
            self._gate(context)
            state = self._require_control_state(context)
            self._require_permit(state)
            if self._dispatch_journal is None:
                raise NativeExecutionError("DISPATCH_JOURNAL_REQUIRED")
            # C1.5 AP-01: the six separate preparation writes (intent register,
            # four context CAS commits, reservation) are replaced by exactly
            # one N3-owned transaction; N4 only supplies the same-operation
            # pure read of the persisted context plus resources and consumes
            # the returned (Plan, final ExecutionContext).
            expected = self.persisted_context(context)
            plan_result, final_context = self._dispatch_journal.prepare_native_submission(
                intent, context, expected_control_revision=state.control_revision,
                expected_context=expected, resources=resources)
            prepared = PreparedExecution(plan=plan_result, context=final_context)
            unattempted = [leg for leg in prepared.plan.legs
                           if self._ledger.get_leg(leg.leg_id).attempt_no == 0]
            if not unattempted:
                raise NativeExecutionError("ATTEMPT_EXISTS")
            leg = unattempted[0]
            record = self._ledger.get_leg(leg.leg_id)
            offer_id = c.canonical_hash(prepared.plan)[:16] + ":" + leg.leg_id
            persisted = self._dispatch_journal.get_offer(offer_id)
            cached = self._offers.get(offer_id)
            authoritative = persisted.offer if persisted is not None else cached
            if authoritative is not None:
                # OFD: the durable first deadline is immutable; a rebuild
                # reproduces the persisted bytes without any recomputation.
                self._offers[offer_id] = authoritative
                return authoritative
            t_offer = self._clock()
            offer = ExecutionOffer(
                offer_id=offer_id, intent_id=record.intent.intent_id, leg_id=leg.leg_id,
                plan_id=prepared.plan.plan_id, intent_hash=prepared.plan.intent_hash,
                leg_hash=c.canonical_hash(record.leg), binding=context.binding,
                snapshot_id=prepared.context.snapshot.snapshot_id,
                policy_id=prepared.context.policy.policy_id,
                params_sha256=state.params_sha256, control_revision=state.control_revision,
                expires_at=self._first_deadline(prepared, final_context, t_offer))
            # AP-03: the C1.4 offer record stays a separate, unchanged
            # transaction after the preparation commit.
            stored = self._dispatch_journal.record_offer(
                offer, final_context, first_issued_at=t_offer, kind=CommandKind.SUBMIT,
                expected_control_revision=state.control_revision,
                expected_context=self.persisted_context(final_context))
            # insert-or-read: the journal's record is authoritative
            self._offers[offer_id] = stored.offer
            return stored.offer

    def _first_deadline(self, prepared, context: c.ExecutionContext, t_offer):
        """E_first at first issuance only (SUBMIT).

        Ceiling terms: the frozen 1500 ms budget from t_offer plus the data
        anchor budget min(sampled_at, t_first) + 1500 ms retained from
        R3-P15 — a snapshot packet older than first issuance by more than the
        budget cannot obtain the full window — over D_original: the plan
        deadline, the public authorization deadline and the C1.3
        applicable_deadline permit bound. Immutability across recovery comes
        from record_offer persistence; recovery never re-runs this.
        """
        from .native_contracts import DISPATCH_TTL_MS, compute_offer_deadline
        sampled = prepared.context.snapshot.times.sampled_at
        anchor = min(sampled, t_offer) if sampled is not None else t_offer
        anchor_budget = anchor + timedelta(milliseconds=DISPATCH_TTL_MS)
        deadlines = [prepared.plan.expires_at]
        if context.authorization is not None:
            deadlines.append(context.authorization.expires_at)
        permit_deadline = self._permit_deadline(
            c.AuthorizationAction(context.intent.action.value))
        if permit_deadline is not None:
            deadlines.append(permit_deadline)
        return min(compute_offer_deadline(t_offer, deadlines), anchor_budget)

    def _first_cancel_deadline(self, context: c.ExecutionContext, record, t_offer):
        """E_first at first issuance only (owned CANCEL).

        CANCEL-D-01: the plan deadline never bounds a CANCEL offer — the plan
        may legitimately be long expired by the time an owned order needs
        cancelling. Applicable bounds: the intent deadline, the public
        authorization deadline, the C1.3 Q1 permit bound and the frozen
        1500 ms budget from t_offer.
        """
        from .native_contracts import compute_offer_deadline
        deadlines = [record.intent.expires_at]
        if context.authorization is not None:
            deadlines.append(context.authorization.expires_at)
        permit_deadline = self._permit_deadline(c.AuthorizationAction.CANCEL_OWNED)
        if permit_deadline is not None:
            deadlines.append(permit_deadline)
        return compute_offer_deadline(t_offer, deadlines)

    def commit_native_offer(self, offer_id: str, challenge: ReadyChallenge,
                            context: c.ExecutionContext) -> DispatchCommand:
        """Re-verify controls and the client challenge, then atomically latch.

        Rejection happens before any journal call: a concurrent control change
        fails the persisted comparison with zero writes and zero attempts.
        The single transaction that latches the attempt, consumes the permit
        and writes the ticket is N3's begin_dispatch; no SQLite transaction
        is ever held across a wait here.
        """
        from .client_dispatch import verify_ready_challenge
        with self._lock:
            self._gate(context)
            offer = self._offers.get(offer_id)
            if offer is None:
                raise NativeExecutionError("OFFER_UNKNOWN")
            state = self._require_control_state(context)
            if state.control_revision != offer.control_revision:
                raise NativeExecutionError("CONTROL_REVISION_MISMATCH")
            self._require_permit(state)
            # C1.2 DTTL-04: no N4 context-refresh write on this critical path.
            # The complete persisted context is read in this same operation and
            # handed to N3's single BEGIN IMMEDIATE, which compares it before
            # any refresh and atomically commits observation, attempt, permit
            # debit and the unique ticket. Every rejection below stays at
            # zero writes and zero attempts.
            expected = self.persisted_context(context)
            check = verify_ready_challenge(challenge, offer, control_state=state, now=self._clock())
            if not check.ok:
                raise LedgerError(c.ResultCode.BLOCKED, check.blockers)
            if self._dispatch_journal is None:
                # The N3 dispatch journal owns atomic latching; without it no
                # attempt, permit consumption or ticket is ever written.
                raise NativeExecutionError("DISPATCH_JOURNAL_REQUIRED")
            return self._dispatch_journal.begin_dispatch(
                offer, context, expected_control_revision=state.control_revision,
                expected_context=expected)

    def prepare_native_cancel(self, cancel_id: str, intent_id: str, leg_id: str,
                              context: c.ExecutionContext) -> ExecutionOffer:
        """Validate and durably latch REQUESTED, then offer the cancel dispatch.

        C1.4 OFD Option A applies identically: recovery reads the persisted
        offer bytes via get_offer; first issuance persists E_first through
        record_offer with the journal cancel identity.
        """
        from .native_contracts import CommandKind, compute_offer_deadline
        with self._lock:
            self._gate(context)
            state = self._require_control_state(context)
            self._require_permit(state)
            self._cancel_preflight(cancel_id, intent_id, leg_id, context)
            record = self._ledger.get_leg(leg_id)
            offer_id = "cancel:" + c.canonical_hash(record.intent)[:16] + ":" + leg_id
            persisted = self._dispatch_journal.get_offer(offer_id) if self._dispatch_journal else None
            cached = self._offers.get(offer_id)
            authoritative = persisted.offer if persisted is not None else cached
            if authoritative is not None:
                self._offers[offer_id] = authoritative
                return authoritative
            t_offer = self._clock()
            deadline = self._first_cancel_deadline(context, record, t_offer)
            offer = ExecutionOffer(
                offer_id=offer_id, intent_id=intent_id, leg_id=leg_id,
                plan_id=record.plan.plan_id, intent_hash=c.canonical_hash(record.intent),
                leg_hash=c.canonical_hash(record.leg), binding=context.binding,
                snapshot_id=context.snapshot.snapshot_id,
                policy_id=context.policy.policy_id,
                params_sha256=state.params_sha256, control_revision=state.control_revision,
                expires_at=deadline)
            if self._dispatch_journal is None:
                raise NativeExecutionError("DISPATCH_JOURNAL_REQUIRED")
            expected = self.persisted_context(context)
            stored = self._dispatch_journal.record_offer(
                offer, context, first_issued_at=t_offer, kind=CommandKind.CANCEL,
                expected_control_revision=state.control_revision,
                expected_context=expected, cancel_id=cancel_id)
            self._offers[offer_id] = stored.offer
            return stored.offer

    def complete_native_dispatch(self, receipt: DispatchReceipt) -> SubmitResult:
        """Project a call-boundary receipt through N3; NO_CALL is never -1.

        Stage/result consistency is enforced by the frozen DTO itself; chain
        contiguity is verified by the journal from the full receipt history.
        """
        with self._lock:
            if self._dispatch_journal is None:
                raise NativeExecutionError("DISPATCH_JOURNAL_REQUIRED")
            if type(receipt) is not DispatchReceipt:
                raise c.ContractError("DispatchReceipt required")
            return self._dispatch_journal.record_receipt(receipt)
