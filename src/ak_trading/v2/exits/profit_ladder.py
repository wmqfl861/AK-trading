"""PROFIT_LADDER staged profit-taking trigger (08-M3C §5.2/§5.3, P2).

Rulings implemented:

* The staging variable is ``Nt / R0`` (``episode_net_liquidation_r``) —
  the whole-round projected net liquidation including realized gross,
  every paid fee, the remaining mark value and the FUTURE exit cost
  Kclose. Never a price level, never ``unrealized_pnl_r`` (floating P&L
  omits closed legs and fees and would mis-stage after a partial close),
  never a single profitable lot. ``R0`` is the entry-frozen round
  ``planned_loss_cny`` and is never re-based by reduces/adds.
* Stage i triggers when ``n_t >= profit_levels_r[i]`` — equality counts.
  The trigger latch is monotone per stage: profit falling back never
  unlatches a stage and never blocks a later stage.
* Three separate books (D3): the ``triggered`` latch is an observation
  fact; ``requested_lots`` is latched only when the stage's request is
  SELECTED (:func:`state.commit_selected_exit` — a candidate never
  pre-writes requests); ``completed_lots`` advances only on first-booked
  real fills. Cancels, rejects and restarts never count as completes.
* One jump across several thresholds selects the HIGHEST cumulative
  target and issues one deficit — never a re-multiplication of the
  current remainder ("two 50%s never become 75%"). Zero-lot stages keep
  their latch and a reason; with Qbase=1 intermediate stages are zero
  until the final stage closes everything.
* Preconditions: voluntary partial closes require the frozen ``Qbase``
  (the M4-managed OPEN/ADD resolution protocol); without it the policy
  only emits KEEP + reasons. Hard-risk exits are never blocked by this.
* The final stage (f == 1) is action=EXIT / target 0; intermediate stages
  are REDUCE with signed target ``s*(Qbase - D_i)``. The original T0
  keeps its ex-ante protective channel — first hit wins.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ak_trading.v2.contracts import (
    ExitContext, ExitLadderStep, ExitProposal, ExitSpec, ExitState,
)
from ak_trading.v2.exits.features import (
    collect_exit_facts, exit_digest, next_window_start,
)
from ak_trading.v2.exits.sizing import (
    closing_lot_step, cumulative_target_lots, reduce_target_lots,
    stage_is_final,
)
from ak_trading.v2.exits.state import advance_state, base_state_for

LADDER_REASON_VOCABULARY = frozenset({
    "LADDER_LEVEL_TRIGGERED", "LADDER_DEFICIT_ISSUED",
    "LADDER_NO_EXECUTABLE_LOTS", "LADDER_QBASE_NOT_FROZEN",
    "LADDER_FINAL_STAGE_EXIT", "LADDER_TARGET_ALREADY_MET",
    "BELOW_FIRST_LEVEL",
    "SIZING_PENDING_ADD_RESOLUTION", "NLI_UNVALUED", "RULE_MISSING",
    "MARK_UNVALUED", "MARK_MISSING", "MARK_STALE",
    "CLOSE_FEE_UNKNOWN", "CLOSE_TODAY_FEE_UNKNOWN", "VALUATION_INCOMPLETE",
    "PLANNED_LOSS_NON_POSITIVE", "NO_REAL_FILL", "OBSERVATION_GAP",
})


def _parse_levels(parameters) -> tuple[tuple[Decimal, ...], tuple[Decimal, ...], int]:
    levels = tuple(Decimal(item) for item in parameters["profit_levels_r"])
    fractions = tuple(Decimal(item)
                      for item in parameters["cumulative_close_fractions"])
    bars = parameters["confirmation_bars"]
    if not isinstance(bars, int) or isinstance(bars, bool) or bars < 1:
        raise ValueError("ladder_confirmation_bars_must_be_int_at_least_1")
    return levels, fractions, bars


def _ladder_steps(previous: ExitState, count: int,
                  triggered_mask: tuple[bool, ...]) -> tuple[ExitLadderStep, ...]:
    """Carry the committed three-book state; latch only `triggered`."""
    steps = list(previous.ladder_progress)
    if steps and len(steps) != count:
        raise ValueError("ladder_step_count_must_match_bound_parameters")
    result: list[ExitLadderStep] = []
    for index in range(count):
        triggered = triggered_mask[index]
        if index < len(steps):
            step = steps[index]
            if triggered and not step.triggered:
                step = ExitLadderStep(level_index=step.level_index,
                                      triggered=True,
                                      requested_lots=step.requested_lots,
                                      completed_lots=step.completed_lots)
            result.append(step)
        else:
            result.append(ExitLadderStep(level_index=index, triggered=triggered,
                                         requested_lots=0, completed_lots=0))
    return tuple(result)


@dataclass(frozen=True)
class ProfitLadderPolicy:
    """The P2 staged profit-taking ExitPolicy implementation."""

    _spec: ExitSpec

    def __post_init__(self) -> None:
        if self._spec.family != "PROFIT_LADDER":
            raise ValueError("spec_family_must_be_PROFIT_LADDER")

    @property
    def spec(self) -> ExitSpec:
        return self._spec

    def evaluate(self, context: ExitContext,
                 previous: ExitState | None) -> ExitProposal:
        base = base_state_for(context, previous)
        facts = collect_exit_facts(context, base)
        levels, fractions, _bars = _parse_levels(self._spec.parameters)
        now = facts.now_nano
        side = budget_side = context.budget.side

        n_t = facts.net_liquidation_r
        # Shared observation layer: the net peak moves forward whenever the
        # current Nt observation qualifies (merge keeps one submission).
        peak_changes: dict[str, object] = {}
        if facts.net_liquidation_r is not None:
            candidate = max(Decimal("0"), facts.net_liquidation_r)
            if base.net_peak_r is None or candidate > base.net_peak_r:
                peak_changes = {
                    "net_peak_r": candidate, "net_peak_source_nano": now,
                    "net_peak_source_hash": exit_digest(
                        ["nli", now, facts.net_liquidation_r])}

        if n_t is None:
            return self._keep(context, base, peak_changes,
                              tuple(facts.nli_reasons) + ("NLI_UNVALUED",), now)

        previously = {step.level_index: step.triggered
                      for step in base.ladder_progress}
        triggered_mask = tuple(
            previously.get(index, False) or n_t >= levels[index]
            for index in range(len(levels)))
        newly_triggered = any(
            triggered_mask[index] and not previously.get(index, False)
            for index in range(len(levels)))
        highest = max(index for index in range(len(levels))
                      if triggered_mask[index]) if any(triggered_mask) else None
        changes: dict[str, object] = dict(peak_changes)
        if base.qbase is not None:
            # M1 couples ladder steps to a frozen qbase; before the freeze
            # the observation carries peaks only and the latch forms on the
            # first observation after the Qbase protocol completes.
            changes["ladder_progress"] = _ladder_steps(base, len(levels),
                                                       triggered_mask)

        if highest is None:
            return self._keep(context, base, changes,
                              ("BELOW_FIRST_LEVEL",), now)

        qbase = base.qbase
        if qbase is None:
            if context.entry_state.pending_layer_index is not None:
                reason = "SIZING_PENDING_ADD_RESOLUTION"
            else:
                reason = "LADDER_QBASE_NOT_FROZEN"
            return self._keep(context, base, changes, (reason,), now)
        try:
            lot_step = closing_lot_step(facts.rule)
        except ValueError:
            return self._keep(context, base, changes, ("RULE_MISSING",), now)

        final = stage_is_final(fractions, highest)
        cumulative = cumulative_target_lots(qbase, fractions[highest], lot_step,
                                            final_step=final)
        reasons: list[str] = []
        if newly_triggered:
            reasons.append("LADDER_LEVEL_TRIGGERED")

        if final:
            reasons.append("LADDER_FINAL_STAGE_EXIT")
            return self._proposal(context, base, changes, "EXIT", 0, reasons, now)

        remaining = facts.remaining_lots
        if cumulative >= qbase:
            # Fraction == 1 cannot occur on a non-final index (whitelist);
            # guard anyway — never request more than the round.
            reasons.append("LADDER_FINAL_STAGE_EXIT")
            return self._proposal(context, base, changes, "EXIT", 0, reasons, now)
        if cumulative <= base.closed_lots:
            reasons.append("LADDER_NO_EXECUTABLE_LOTS")
            return self._keep(context, base, changes, tuple(reasons), now)
        target = reduce_target_lots(side, qbase, cumulative)
        if abs(target) >= remaining and remaining > 0:
            # r_i >= q: the rule asks for no NEW reduction this round; the
            # latch and the standing obligation stay (D3).
            reasons.append("LADDER_TARGET_ALREADY_MET")
            return self._keep(context, base, changes, tuple(reasons), now)
        if target == 0:
            reasons.append("LADDER_NO_EXECUTABLE_LOTS")
            return self._keep(context, base, changes, tuple(reasons), now)
        reasons.append("LADDER_DEFICIT_ISSUED")
        return self._proposal(context, base, changes, "REDUCE", target,
                              reasons, now)

    # -- helpers -----------------------------------------------------------

    def _input_hash(self, context: ExitContext, base: ExitState,
                    payload: list) -> str:
        return exit_digest(["ladder-input", [
            context.account_id, context.episode_id,
            context.exit_binding.binding_sha256, self._spec.exit_policy_id,
            base.state_prefix_hash] + payload])

    def _proposal_id(self, context: ExitContext, input_sha256: str) -> str:
        return exit_digest(["exit-proposal", context.exit_binding.binding_sha256,
                            self._spec.exit_policy_id, context.now_nano,
                            input_sha256])

    def _keep(self, context: ExitContext, base: ExitState,
              changes: dict[str, object], reasons: tuple[str, ...],
              now: int) -> ExitProposal:
        input_sha256 = self._input_hash(context, base, ["KEEP", sorted(reasons)])
        proposal_id = self._proposal_id(context, input_sha256)
        next_state = base if not changes else advance_state(
            base, proposal_id, **changes)
        remaining = sum(lot.lots for lot in context.lots
                        if lot.episode_id == context.episode_id
                        and lot.contract_id == context.contract_id
                        and lot.strategy_id == context.strategy_id)
        earliest = next_window_start(context.sessions, now,
                                     self._spec.observation_duration_seconds)
        return ExitProposal(
            proposal_id=proposal_id,
            strategy_id=context.strategy_id, episode_id=context.episode_id,
            contract_id=context.contract_id,
            exit_binding_sha256=context.exit_binding.binding_sha256,
            action="KEEP", target_lots=context.budget.side * remaining,
            triggering_policy_id=self._spec.exit_policy_id,
            input_sha256=input_sha256, observed_at_nano=now,
            earliest_execution_nano=max(earliest, now), protection=None,
            next_state=next_state,
            reasons=tuple(r for r in reasons
                          if r in LADDER_REASON_VOCABULARY))

    def _proposal(self, context: ExitContext, base: ExitState,
                  changes: dict[str, object], action: str, target: int,
                  reasons: list[str], now: int) -> ExitProposal:
        input_sha256 = self._input_hash(context, base, [action, target,
                                                        sorted(reasons)])
        proposal_id = self._proposal_id(context, input_sha256)
        next_state = advance_state(base, proposal_id, **changes)
        earliest = next_window_start(context.sessions, now,
                                     self._spec.observation_duration_seconds)
        protection = None
        return ExitProposal(
            proposal_id=proposal_id,
            strategy_id=context.strategy_id, episode_id=context.episode_id,
            contract_id=context.contract_id,
            exit_binding_sha256=context.exit_binding.binding_sha256,
            action=action, target_lots=target,
            triggering_policy_id=self._spec.exit_policy_id,
            input_sha256=input_sha256, observed_at_nano=now,
            earliest_execution_nano=max(earliest, now), protection=protection,
            next_state=next_state,
            reasons=tuple(r for r in reasons
                          if r in LADDER_REASON_VOCABULARY))
