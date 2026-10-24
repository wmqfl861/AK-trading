"""Trading-clock no-progress full-close exit (08-M3C §5.2/§5.4, P1).

P1 registers the all-close single-stage version only
(``soft_time_fractions=["0.75"], minimum_progress_r=["0.50"],
cumulative_close_fractions=["1"]``); the P2 two-stage parameters ship as
``:params-2`` and never overwrite an in-flight binding.

Clock semantics (08 §6.2/§10-6):

* ``elapsed_fraction = age/(age + remaining_steps)`` with both clocks in
  the same mode; the deadline is
  ``min(active_exit_nano, RoundBudget.latest_exit_nano)`` — the M3B
  holding deadline is encoded upstream into ``active_exit_nano`` at fill
  time. The mapping is convex in calendar time near the deadline, so any
  deadline tightening raises the fraction and can only trigger the soft
  exit earlier, never later.
* Trading minutes intersect wall time with legal session windows: lunch
  breaks do not age the episode and missing bars never pause the clock.
  Daily ages count completed trading days only (entry-day close = step
  1; the running day does not count).
* Boundary semantics: the time threshold triggers on equality; the
  progress threshold requires ``Nt/R0`` **strictly below** the minimum —
  ``0.50R`` exactly does not trigger "insufficient progress".
* ``Nt`` unknown (unvalued mark, unknown exit cost) means the progress
  condition is undecidable: KEEP with reasons, never a guessed value.
  The hard deadline channel keeps running in M4 regardless (M3C-H03).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ak_trading.v2.contracts import ExitContext, ExitProposal, ExitSpec, ExitState
from ak_trading.v2.exits.features import collect_exit_facts, exit_digest, next_window_start
from ak_trading.v2.exits.sizing import (
    closing_lot_step, cumulative_target_lots, reduce_target_lots,
    stage_is_final,
)
from ak_trading.v2.exits.state import base_state_for

TIME_DECAY_REASON_VOCABULARY = frozenset({
    "TIME_DECAY_NO_PROGRESS", "TIME_DECAY_KEEP", "BELOW_TIME_THRESHOLD",
    "PROGRESS_AT_OR_ABOVE_THRESHOLD", "CLOCK_UNAVAILABLE", "NLI_UNVALUED",
    "TIME_DECAY_STAGE_REDUCE", "TIME_DECAY_QBASE_NOT_FROZEN",
    "SIZING_PENDING_ADD_RESOLUTION",
    # facts-origin quality words that may surface verbatim (08 §6.5):
    "MARK_UNVALUED", "MARK_MISSING", "MARK_STALE", "RULE_MISSING",
    "CLOSE_FEE_UNKNOWN", "CLOSE_TODAY_FEE_UNKNOWN", "VALUATION_INCOMPLETE",
    "PLANNED_LOSS_NON_POSITIVE", "NO_REAL_FILL", "CALENDAR_UNKNOWN",
    "DEADLINE_UNKNOWN", "NO_VALID_HOLDING_WINDOW", "OBSERVATION_GAP",
})


@dataclass(frozen=True)
class TimeDecayPolicy:
    """The P1 full-close time-decay ExitPolicy implementation."""

    _spec: ExitSpec

    def __post_init__(self) -> None:
        if self._spec.family != "TIME_DECAY":
            raise ValueError("spec_family_must_be_TIME_DECAY")

    @property
    def spec(self) -> ExitSpec:
        return self._spec

    def evaluate(self, context: ExitContext,
                 previous: ExitState | None) -> ExitProposal:
        base = base_state_for(context, previous)
        facts = collect_exit_facts(context, base)
        parameters = self._spec.parameters
        time_thresholds = tuple(Decimal(item) for item in parameters["soft_time_fractions"])
        progress_thresholds = tuple(Decimal(item)
                                    for item in parameters["minimum_progress_r"])
        close_fractions = tuple(Decimal(item)
                                for item in parameters["cumulative_close_fractions"])
        now = facts.now_nano
        elapsed = facts.elapsed_fraction
        nli_r = facts.net_liquidation_r

        reasons: list[str] = []
        triggered_fraction: Decimal | None = None
        reduce_target: int | None = None
        if elapsed is None:
            reasons.extend(facts.elapsed_reasons or ("CLOCK_UNAVAILABLE",))
        elif nli_r is None:
            reasons.extend(facts.nli_reasons or ("NLI_UNVALUED",))
        else:
            for time_threshold, progress_threshold, fraction in zip(
                    time_thresholds, progress_thresholds, close_fractions):
                if elapsed >= time_threshold and nli_r < progress_threshold:
                    triggered_fraction = (max(triggered_fraction, fraction)
                                          if triggered_fraction is not None
                                          else fraction)

        full_close = triggered_fraction is not None and triggered_fraction == Decimal("1")
        if full_close:
            reasons.append("TIME_DECAY_NO_PROGRESS")
        elif triggered_fraction is not None:
            # P2 two-stage params-2: an intermediate stage is a voluntary
            # REDUCE sized off the frozen Qbase (D3); without Qbase or a
            # known closing step it degrades to KEEP with explicit reasons.
            qbase = base.qbase
            if qbase is None:
                if context.entry_state.pending_layer_index is not None:
                    reasons.append("SIZING_PENDING_ADD_RESOLUTION")
                else:
                    reasons.append("TIME_DECAY_QBASE_NOT_FROZEN")
                triggered_fraction = None
            else:
                try:
                    lot_step = closing_lot_step(facts.rule)
                except ValueError:
                    reasons.append("RULE_MISSING")
                    triggered_fraction = None
                else:
                    stage = max(index for index in range(len(time_thresholds))
                                if elapsed >= time_thresholds[index]
                                and nli_r < progress_thresholds[index])
                    final = stage_is_final(close_fractions, stage)
                    cumulative = cumulative_target_lots(
                        qbase, close_fractions[stage], lot_step, final)
                    if cumulative <= base.closed_lots or cumulative == 0:
                        reasons.append("TIME_DECAY_KEEP")
                        triggered_fraction = None
                    else:
                        reduce_target = reduce_target_lots(
                            facts.side, qbase, cumulative)
                        if abs(reduce_target) >= facts.remaining_lots > 0:
                            reasons.append("TIME_DECAY_KEEP")
                            triggered_fraction = None
                        else:
                            reasons.append("TIME_DECAY_STAGE_REDUCE")
        elif not reasons:
            if elapsed is not None and elapsed < time_thresholds[0]:
                reasons.append("BELOW_TIME_THRESHOLD")
            elif nli_r is not None:
                reasons.append("PROGRESS_AT_OR_ABOVE_THRESHOLD")
            else:
                reasons.append("TIME_DECAY_KEEP")

        input_core = [
            context.account_id, context.episode_id,
            context.exit_binding.binding_sha256, self._spec.exit_policy_id,
            now, str(elapsed) if elapsed is not None else None,
            str(nli_r) if nli_r is not None else None,
            [str(item) for item in time_thresholds],
            [str(item) for item in progress_thresholds],
            base.state_prefix_hash]
        input_sha256 = exit_digest(["time-decay-input", input_core])
        proposal_id = exit_digest([
            "exit-proposal", context.exit_binding.binding_sha256,
            self._spec.exit_policy_id, now, input_sha256])

        # Time-decay stages carry no observation facts of their own (OQ-6:
        # no confirmation counters for trailing/time policies): the candidate
        # next_state is the unchanged committed state.
        if full_close:
            action, target = "EXIT", 0
        elif triggered_fraction is not None and reduce_target is not None:
            action, target = "REDUCE", reduce_target
        else:
            action, target = "KEEP", facts.side * facts.remaining_lots
        earliest = next_window_start(context.sessions, now, facts.duration_seconds)
        return ExitProposal(
            proposal_id=proposal_id,
            strategy_id=context.strategy_id, episode_id=context.episode_id,
            contract_id=context.contract_id,
            exit_binding_sha256=context.exit_binding.binding_sha256,
            action=action, target_lots=target,
            triggering_policy_id=self._spec.exit_policy_id,
            input_sha256=input_sha256, observed_at_nano=now,
            earliest_execution_nano=max(earliest, now),
            protection=None, next_state=base,
            reasons=tuple(reason for reason in reasons
                          if reason in TIME_DECAY_REASON_VOCABULARY))
