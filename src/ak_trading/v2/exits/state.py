"""ExitState pure transitions and recovery validation (08-M3C §4.2/§7.4).

Three strictly separated commit boundaries (OQ-3):

* :func:`commit_exit_observation` — advances observation facts only
  (peaks, the arm latch, the effective protection line). It never creates
  fills, releases reservations or marks ladder steps complete.
* :func:`commit_selected_exit` — latches ``no_more_add``, the original
  action/target and the intent commitments after M4 verified and selected
  the candidate and registered its :class:`ExitExecutionLink` entries.
* :func:`apply_exit_execution` — only real new fills advance cash-flow
  accumulators, ``closed_lots`` and completion progress.

The first real fill initializes an :class:`ExitState`
(:func:`initialize_exit_state`); an OPEN proposal or a cancelled order
never starts the age clock (08 §4.2). ``state_prefix_hash`` binds every
committed state to the hash chain of its ancestors: each advance is
``sha256(previous_hash ‖ event_id ‖ canonical core projection)``.

Side-free invariants are enforced here (the arm latch can never be
cleared, a set peak can never be erased, ``net_peak_r`` is monotone,
``no_more_add`` never resets); direction-aware monotonicity of the line
itself (long up / short down, b0 floor) is enforced by the side-aware
TRAILING_STOP policy and its tests — :class:`ExitState` deliberately
carries no direction field.
"""
from __future__ import annotations

import dataclasses
from decimal import Decimal
from typing import Iterable

from ak_trading.v2.contracts import (
    AccountSnapshot,
    ExitContext,
    ExitExecutionLink,
    ExitProposal,
    ExitState,
    Fill,
    LedgerCheckpoint,
    TradingRule,
)
from ak_trading.v2.exits.features import episode_cashflows, exit_digest

GENESIS_VERSION = "exit-state-genesis-v1"

_IMMUTABLE_OBSERVATION_FIELDS = (
    "account_id", "contract_id", "strategy_id", "episode_id",
    "first_fill_nano", "exit_binding_sha256", "realized_gross_cny",
    "fees_paid_cny", "no_more_add", "qbase", "closed_lots", "phase",
)


def _check_ladder_observation(previous: ExitState, state: ExitState) -> None:
    """P2: only the trigger latch may move in an observation.

    ``requested_lots`` is a SELECTION fact (D3: a candidate never pre-
    writes requests) and ``completed_lots`` advances only on real fills;
    both must be carried unchanged here. New steps may appear (first
    observation of a bound ladder) only with requested=completed=0.
    """
    previous_steps = {step.level_index: step for step in previous.ladder_progress}
    seen: list[int] = []
    for step in state.ladder_progress:
        seen.append(step.level_index)
        before = previous_steps.get(step.level_index)
        if before is None:
            if step.requested_lots != 0 or step.completed_lots != 0:
                raise ExitStateError(
                    "ladder_new_step_requires_zero_request_and_complete")
            continue
        if step.requested_lots != before.requested_lots                 or step.completed_lots != before.completed_lots:
            raise ExitStateError(
                "observation_must_not_change_ladder_request_or_complete")
        if before.triggered and not step.triggered:
            raise ExitStateError("ladder_trigger_latch_must_not_reset")
    if previous_steps and seen != sorted(previous_steps):
        raise ExitStateError("ladder_step_set_must_match_committed_steps")


class ExitStateError(ValueError):
    """Raised when a transition violates the frozen commit boundaries."""


def _core(state: ExitState) -> dict[str, object]:
    """Canonical JSON-safe projection bound into the prefix hash."""
    projection: dict[str, object] = {}
    for field in dataclasses.fields(state):
        if field.name in ("state_prefix_hash", "last_committed_event_id"):
            continue
        value = getattr(state, field.name)
        if field.name == "confirmations":
            value = [(item.exit_policy_id, item.rule_id, item.count,
                      item.last_observation_id) for item in value]
        elif field.name == "ladder_progress":
            value = [(item.level_index, item.triggered, item.requested_lots,
                      item.completed_lots) for item in value]
        projection[field.name] = value
    return projection  # type: ignore[return-value]


def _genesis_hash(state: ExitState) -> str:
    return exit_digest([GENESIS_VERSION, state.account_id, state.contract_id,
                        state.strategy_id, state.episode_id,
                        state.first_fill_nano, state.exit_binding_sha256])


def _advance(previous_hash: str, event_id: str, state: ExitState) -> str:
    return exit_digest([previous_hash, event_id, _core(state)])


def advance_state(previous: ExitState, event_id: str, **changes: object) -> ExitState:
    """Public pure helper: rebuild ``previous`` with changes + chain advance.

    Used by the policies to build OQ-3 candidate ``next_state`` values;
    ``commit_exit_observation`` later re-derives the same chain advance to
    reject replayed or tampered candidates.
    """
    return _with(previous, event_id, **changes)


def base_state_for(context: ExitContext,
                   previous: ExitState | None) -> ExitState:
    """The evaluation base: the committed state, or genesis from first fill.

    Exit policies never advance state from an unexecuted proposal or a
    cancelled order (08 §4.2): without a committed state a real OPEN fill
    must exist in the episode ledger, otherwise evaluation is refused.
    """
    if previous is not None:
        return previous
    fills = [fill for fill in context.episode_fills
             if fill.episode_id == context.episode_id
             and fill.contract_id == context.contract_id
             and fill.strategy_id == context.strategy_id
             and fill.offset == "OPEN"]
    if not fills:
        raise ExitStateError("evaluate_requires_real_fill_state")
    first = min(fills, key=lambda fill: (fill.execution_nano, fill.fill_id))
    return initialize_exit_state(context, first)


def _with(previous: ExitState, event_id: str, **changes: object) -> ExitState:
    """Rebuild ``previous`` with allowed field changes and a fresh prefix."""
    values = {field.name: getattr(previous, field.name)
              for field in dataclasses.fields(previous)}
    values.update(changes)
    candidate = ExitState(**values)  # type: ignore[arg-type]
    object.__setattr__(
        candidate, "state_prefix_hash",
        _advance(previous.state_prefix_hash, event_id, candidate))
    return candidate


def initialize_exit_state(context: ExitContext, first_fill: Fill) -> ExitState:
    """First real OPEN fill initializes the episode exit state.

    ``qbase`` freezes the cumulative original lots from the committed
    episode fill ledger (P1: single layer, no ADD policy in scope). The
    genesis prefix hash binds the scope, the first fill instant and the
    binding identity.
    """
    if first_fill.offset != "OPEN":
        raise ExitStateError("initialize_requires_open_fill")
    if first_fill.episode_id != context.episode_id \
            or first_fill.contract_id != context.contract_id \
            or first_fill.strategy_id != context.strategy_id:
        raise ExitStateError("first_fill_scope_must_match_context")
    fills = tuple(fill for fill in context.episode_fills
                  if fill.episode_id == context.episode_id
                  and fill.contract_id == context.contract_id
                  and fill.strategy_id == context.strategy_id)
    rule = next((item for item in context.rules
                 if item.contract_id == context.contract_id
                 and item.effective_from_nano <= context.now_nano
                 < item.effective_until_nano
                 and item.available_at_nano <= context.now_nano), None)
    multiplier = rule.multiplier if rule is not None else None
    gross, fees, _, _ = ((episode_cashflows(fills, first_fill.side, multiplier)
                          if multiplier is not None and fills
                          else (Decimal("0"),
                                sum((fill.fee_cny for fill in fills), Decimal("0")),
                                0, None)))
    qbase = sum(fill.lots for fill in fills if fill.offset == "OPEN")
    if qbase < 1:
        raise ExitStateError("qbase_requires_positive_open_lots")
    state = ExitState(
        account_id=context.account_id, contract_id=context.contract_id,
        strategy_id=context.strategy_id, episode_id=context.episode_id,
        first_fill_nano=first_fill.execution_nano,
        exit_binding_sha256=context.exit_binding.binding_sha256,
        realized_gross_cny=gross, fees_paid_cny=fees,
        price_peak=None, price_peak_source_nano=None, price_peak_source_hash=None,
        net_peak_r=None, net_peak_source_nano=None, net_peak_source_hash=None,
        armed=False, protection_stop_price=None, protection_line_version=None,
        protection_effective_from_nano=None, confirmations=(),
        no_more_add=False, qbase=qbase, closed_lots=0, ladder_progress=(),
        phase="MANAGING", last_committed_event_id=None,
        state_prefix_hash="0" * 64)
    object.__setattr__(state, "state_prefix_hash",
                       _advance(_genesis_hash(state),
                                f"genesis:{first_fill.fill_id}", state))
    object.__setattr__(state, "last_committed_event_id", first_fill.fill_id)
    return state


def _validate_observation(previous: ExitState, proposal: ExitProposal) -> ExitState:
    """OQ-3 observation-only legality; returns the candidate next_state."""
    state = proposal.next_state
    if (state.account_id != previous.account_id
            or state.contract_id != previous.contract_id
            or state.strategy_id != previous.strategy_id
            or state.episode_id != previous.episode_id
            or state.exit_binding_sha256 != previous.exit_binding_sha256):
        raise ExitStateError("observation_scope_must_match_previous")
    for name in _IMMUTABLE_OBSERVATION_FIELDS:
        if getattr(state, name) != getattr(previous, name):
            raise ExitStateError(f"observation_must_not_change_{name}")
    # peaks: never erased; net peak monotone (side-free)
    if previous.price_peak is not None and state.price_peak is None:
        raise ExitStateError("price_peak_must_not_be_erased")
    if previous.net_peak_r is not None:
        if state.net_peak_r is None:
            raise ExitStateError("net_peak_must_not_be_erased")
        if state.net_peak_r < previous.net_peak_r:
            raise ExitStateError("net_peak_must_not_decrease")
    for prefix in ("price_peak", "net_peak"):
        value = getattr(state, f"{prefix}_r" if prefix == "net_peak" else prefix)
        nano = getattr(state, f"{prefix}_source_nano")
        mark = getattr(state, f"{prefix}_source_hash")
        if value is None and (nano is not None or mark is not None):
            raise ExitStateError(f"{prefix}_none_requires_none_sources")
        if value is not None and (nano is None or mark is None):
            raise ExitStateError(f"{prefix}_set_requires_sources")
        if nano is not None and nano < previous.first_fill_nano:
            raise ExitStateError(f"{prefix}_source_precedes_first_fill")
    # arm latch: never cleared; an armed state always carries the line triple
    if previous.armed and not state.armed:
        raise ExitStateError("armed_latch_must_not_be_cleared")
    if state.armed and state.protection_stop_price is None:
        raise ExitStateError("armed_requires_protection_line")
    if not state.armed and state.protection_stop_price is not None:
        raise ExitStateError("unarmed_state_must_not_carry_protection_line")
    if previous.armed and state.armed:
        if state.protection_effective_from_nano is None \
                or previous.protection_effective_from_nano is None:
            raise ExitStateError("armed_line_timing_required")
        if state.protection_effective_from_nano \
                < previous.protection_effective_from_nano:
            raise ExitStateError("protection_effective_from_must_not_rewind")
    _check_ladder_observation(previous, state)
    return state


def commit_exit_observation(previous: ExitState,
                            proposal: ExitProposal) -> ExitState:
    """Commit one candidate's observation facts (no resource side effects).

    The candidate must be a legal observation-only advance of ``previous``
    (OQ-3) and its prefix hash must equal the recomputed chain advance
    with ``proposal.proposal_id`` as the event — a replayed or tampered
    candidate cannot slip through under a different event identity.
    """
    if proposal.exit_binding_sha256 != previous.exit_binding_sha256:
        raise ExitStateError("proposal_binding_must_match_previous_state")
    if proposal.strategy_id != previous.strategy_id \
            or proposal.episode_id != previous.episode_id \
            or proposal.contract_id != previous.contract_id:
        raise ExitStateError("proposal_scope_must_match_previous_state")
    state = _validate_observation(previous, proposal)
    if _core(state) == _core(previous):
        # Nothing observable changed (e.g. a KEEP candidate): the chain does
        # not advance and no event identity is consumed.
        return previous
    expected = _advance(previous.state_prefix_hash, proposal.proposal_id, state)
    if state.state_prefix_hash != expected:
        raise ExitStateError("candidate_prefix_hash_must_match_chain_advance")
    object.__setattr__(state, "last_committed_event_id", proposal.proposal_id)
    return state


def commit_selected_exit(previous: ExitState, proposal: ExitProposal,
                         links: tuple[ExitExecutionLink, ...]) -> ExitState:
    """Latch no_more_add / original action / intent commitments (M4 step)."""
    if not links:
        raise ExitStateError("selection_requires_execution_links")
    if proposal.action == "KEEP":
        raise ExitStateError("keep_candidates_are_not_selected")
    for link in links:
        if link.exit_binding_sha256 != previous.exit_binding_sha256:
            raise ExitStateError("link_binding_must_match_state")
        if link.action != proposal.action:
            raise ExitStateError("link_action_must_match_proposal")
        if link.exit_proposal_id not in (None, proposal.proposal_id):
            raise ExitStateError("link_proposal_reference_mismatch")
    state = _validate_observation(previous, proposal)
    if _core(state) == _core(previous):
        # Nothing observable changed (e.g. a pure drawdown EXIT candidate):
        # anchor the selection on the unchanged committed state.
        state = previous
    else:
        expected = _advance(previous.state_prefix_hash, proposal.proposal_id,
                            state)
        if state.state_prefix_hash != expected:
            raise ExitStateError(
                "candidate_prefix_hash_must_match_chain_advance")
    # P2 phase continuity (design §6 / D3): LIQUIDATING is terminal except
    # for CLOSED-by-fill; a REDUCE can never pull it back to holding, and a
    # hard EXIT upgrades REDUCING irreversibly.
    if previous.phase == "CLOSED":
        raise ExitStateError("closed_state_admits_no_selection")
    if previous.phase == "LIQUIDATING" and proposal.action != "EXIT":
        raise ExitStateError("liquidating_cannot_return_to_partial")
    phase = "LIQUIDATING" if proposal.action == "EXIT" else "REDUCING"
    changes: dict[str, object] = {"no_more_add": True, "phase": phase}
    # Ladder request latching (D3): the highest triggered stage without a
    # recorded request gets its cumulative target locked at SELECTION time
    # — candidates never pre-write requests.
    unrequested = [step for step in state.ladder_progress
                   if step.triggered and step.requested_lots == 0]
    if unrequested:
        qbase = state.qbase
        if qbase is None:
            raise ExitStateError("ladder_selection_requires_frozen_qbase")
        stage = max(unrequested, key=lambda step: step.level_index)
        request = qbase if proposal.action == "EXIT"             else qbase - abs(proposal.target_lots)
        if request < 0 or request > qbase:
            raise ExitStateError("ladder_request_out_of_range")
        steps = list(state.ladder_progress)
        steps[steps.index(stage)] = dataclasses.replace(
            stage, requested_lots=request)
        changes["ladder_progress"] = tuple(steps)
    selected = _with(state, f"selected:{links[0].link_id}", **changes)
    return selected


def _episode_lots_of(account: AccountSnapshot, state: ExitState) -> tuple:
    return tuple(lot for lot in account.lots
                 if lot.episode_id == state.episode_id
                 and lot.contract_id == state.contract_id
                 and lot.strategy_id == state.strategy_id)


def apply_exit_execution(previous: ExitState, result, link: ExitExecutionLink,
                         account: AccountSnapshot, now_nano: int, *,
                         multiplier: Decimal) -> ExitState:
    """Advance from one execution receipt; only real fills move cashflows.

    Contract notes (P1, frozen positional signature kept verbatim):

    * ``account`` is the snapshot at ``now_nano`` **before** this receipt's
      fills are applied — the closing lots' entry basis is derived from
      the episode lots still visible there (mirrors ledger.py's
      ``lot.side * closed * (fill.execution_price - lot.entry_price)
      * rule.multiplier``). Post-receipt snapshots would erase the basis.
    * ``multiplier`` is the effective contract multiplier of the fill's
      rule; M4 holds the rule at the receipt site. It is a REQUIRED
      keyword-only argument (007 no-implicit-defaults principle, P1
      review finding): the transition never guesses a CNY amount.
    * An empty terminal replay of an already-committed intent is
      idempotent; per-``fill_id`` delivery dedup stays with the M4
      checkpoint's processed-event set (same split as M3B apply_execution).
    """
    if result.intent_id != link.intent_id:
        raise ExitStateError("result_intent_must_match_link")
    if link.exit_binding_sha256 != previous.exit_binding_sha256:
        raise ExitStateError("link_binding_must_match_state")
    for fill in result.fills:
        if fill.contract_id != previous.contract_id \
                or fill.strategy_id != previous.strategy_id \
                or fill.episode_id != previous.episode_id:
            raise ExitStateError("fill_scope_must_match_state")
        if fill.offset == "OPEN":
            raise ExitStateError("exit_link_cannot_carry_open_fills")
    if not result.fills:
        # No real fill: nothing advances (BLOCKED keeps the exit owed; an
        # empty terminal replay is a pure no-op).
        return previous

    close_lots = sum(fill.lots for fill in result.fills)
    lots = _episode_lots_of(account, previous)
    held = sum(lot.lots for lot in lots)
    qbase = previous.qbase
    if qbase is None:
        qbase = previous.closed_lots + held + close_lots
        if qbase < 1:
            raise ExitStateError("qbase_freeze_requires_positive_lots")
    if previous.closed_lots + close_lots > qbase:
        raise ExitStateError("closed_lots_must_not_exceed_qbase")

    gross_increment = Decimal("0")
    fee_increment = Decimal("0")
    if not isinstance(multiplier, Decimal) or not multiplier.is_finite()             or multiplier <= 0:
        raise ExitStateError("multiplier_must_be_positive_decimal")
    remaining_basis = [[lot.lots, lot.entry_price, lot.side] for lot in lots]
    for fill in sorted(result.fills, key=lambda item: (item.execution_nano,
                                                       item.fill_id)):
        fee_increment += fill.fee_cny
        remaining = fill.lots
        while remaining > 0 and remaining_basis:
            head = remaining_basis[0]
            take = min(int(head[0]), remaining)
            gross_increment += (int(head[2]) * take
                                * (fill.execution_price - head[1]) * multiplier)
            remaining -= take
            if take == int(head[0]):
                remaining_basis.pop(0)
            else:
                head[0] = int(head[0]) - take
        if remaining > 0:
            raise ExitStateError("closing_fills_exceed_pre_receipt_lots")

    closed = previous.closed_lots + close_lots
    phase = previous.phase
    if closed >= qbase and result.remaining_lots == 0:
        phase = "CLOSED"
    elif phase == "REDUCING" and link.target_lots != 0:
        # A voluntary REDUCE reaches its selected target: back to MANAGING
        # (entry state returns to HOLDING on the M3B side); no_more_add
        # stays latched (08 §4.2 P2 semantics).
        remaining = qbase - closed
        if remaining == abs(link.target_lots):
            phase = "MANAGING"
    event = f"fill:{result.intent_id}"
    steps = tuple(
        dataclasses.replace(step, completed_lots=min(closed, step.requested_lots))
        for step in previous.ladder_progress)
    updated = _with(
        previous, event,
        realized_gross_cny=previous.realized_gross_cny + gross_increment,
        fees_paid_cny=previous.fees_paid_cny + fee_increment,
        qbase=qbase, closed_lots=closed, phase=phase,
        ladder_progress=steps)
    return updated


def freeze_qbase(previous: ExitState, total_open_lots: int,
                 event_id: str) -> ExitState:
    """Freeze Qbase once every OPEN/ADD promise reached a terminal state.

    D3 Qbase protocol: M4 stops new adds, cancels unfilled add risk,
    waits for terminal receipts and absorbs late fills; THEN this pure
    transform freezes the real original lots (including anything already
    closed by a hard exit) in one shot. Qbase never re-freezes and never
    re-bases after partial closes.
    """
    if previous.qbase is not None:
        raise ExitStateError("qbase_already_frozen")
    if not isinstance(total_open_lots, int) or isinstance(total_open_lots, bool)             or total_open_lots < 1:
        raise ExitStateError("qbase_freeze_requires_positive_lots")
    if total_open_lots < previous.closed_lots:
        raise ExitStateError("qbase_must_cover_closed_lots")
    return _with(previous, event_id, qbase=total_open_lots)


def validate_restored_exit_state(state: ExitState,
                                 checkpoint: LedgerCheckpoint) -> None:
    """Recovery check (08 §4.2/§7.4): membership, binding, chain shape.

    The checkpoint is the single source of truth for committed exit
    states; a restored state must be byte-identical to its committed
    instance, belong to a binding present in the same checkpoint, and —
    when already flat — satisfy the CLOSED invariants (qbase frozen,
    closed == qbase) that only real fills may establish.
    """
    scope = (state.account_id, state.contract_id, state.strategy_id,
             state.episode_id, state.exit_binding_sha256)
    stored = {((item.account_id, item.contract_id, item.strategy_id,
                item.episode_id, item.exit_binding_sha256)): item
              for item in checkpoint.exit_states}
    if scope not in stored:
        raise ExitStateError("restored_state_missing_from_checkpoint")
    if stored[scope] != state:
        raise ExitStateError("restored_state_must_equal_committed_instance")
    if not any(binding.binding_sha256 == state.exit_binding_sha256
               and binding.strategy_id == state.strategy_id
               for binding in checkpoint.exit_bindings):
        raise ExitStateError("restored_state_binding_missing_from_checkpoint")
    if state.phase == "CLOSED" and state.qbase is not None \
            and state.closed_lots != state.qbase:
        raise ExitStateError("closed_phase_requires_fully_closed_lots")


def visible_exit_rule(rules: Iterable[TradingRule], contract_id: str,
                      now_nano: int) -> TradingRule | None:
    """Latest rule visible for the contract at ``now_nano`` (shared helper)."""
    candidates = [rule for rule in rules
                  if rule.contract_id == contract_id
                  and rule.effective_from_nano <= now_nano
                  < rule.effective_until_nano
                  and rule.available_at_nano <= now_nano]
    if not candidates:
        return None
    return max(candidates, key=lambda rule: (rule.effective_from_nano,
                                             rule.rule_id))
