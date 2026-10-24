"""DRAWDOWN_EXIT whole-round profit giveback (08-M3C §5.2/§5.3, P2; D1).

Rulings implemented:

* ``arm`` is a DERIVED predicate — ``peak_net_liquidation_r >=
  arm_peak_profit_r`` (equality counts) over the shared, committed net
  peak — never a new persistent bool and never ``ExitState.armed``. A
  drawdown-only binding reaches 1R with ``ExitState.armed == False`` and
  the protection triple all None (D1 acceptance anchor). The peak is
  monotone and the threshold is frozen per binding, so the derived arm
  can never disarm; profit falling back below the threshold is precisely
  the giveback scenario being armed for.
* Trigger: ``giveback_t = peak_net_liquidation_r - Nt/R0 >=
  max_giveback_r`` — equality counts (contrast TIME_DECAY's strictly
  below). EXIT / target 0, earliest next legal window. No
  confirmation_bars parameter for this family.
* The peak is the whole-round ``max(0, max_u Nu)/R0`` (EPISODE_PREFIX):
  realized RGt stays inside Nt, so ladder partial closes change only the
  remaining Ut/Kclose — the peak never resets and a partial close never
  fabricates a peak collapse. ``closed_fraction`` never enters the
  formula.
* The peak only advances through the shared observation layer and
  ``commit_exit_observation``; equal peaks keep the original source (no
  fake refresh). STALE/UNVALUED/unknown-cost observations never refresh
  the peak and never trigger a new giveback EXIT; later backfills never
  rewrite committed decisions.
* Account drawdown (``EXITSTATE:account_drawdown_ratio``) is NOT
  consumed: the 10% account kill switch is the M4 hard channel; the 5%
  soft overlay stays unregistered.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ak_trading.v2.contracts import ExitContext, ExitProposal, ExitSpec, ExitState
from ak_trading.v2.exits.features import (
    collect_exit_facts, exit_digest, next_window_start,
)
from ak_trading.v2.exits.state import advance_state, base_state_for

DRAWDOWN_REASON_VOCABULARY = frozenset({
    "DRAWDOWN_ARMED", "DRAWDOWN_GIVEBACK_BREACHED", "DRAWDOWN_PEAK_WARMUP",
    "DRAWDOWN_NOT_ARMED", "DRAWDOWN_KEEP", "NLI_UNVALUED",
    "MARK_UNVALUED", "MARK_MISSING", "MARK_STALE", "RULE_MISSING",
    "CLOSE_FEE_UNKNOWN", "CLOSE_TODAY_FEE_UNKNOWN", "VALUATION_INCOMPLETE",
    "PLANNED_LOSS_NON_POSITIVE", "NO_REAL_FILL", "OBSERVATION_GAP",
    "NO_QUALIFIED_NET_OBSERVATION",
})


@dataclass(frozen=True)
class DrawdownExitPolicy:
    """The P2 whole-round giveback ExitPolicy implementation."""

    _spec: ExitSpec

    def __post_init__(self) -> None:
        if self._spec.family != "DRAWDOWN_EXIT":
            raise ValueError("spec_family_must_be_DRAWDOWN_EXIT")

    @property
    def spec(self) -> ExitSpec:
        return self._spec

    def evaluate(self, context: ExitContext,
                 previous: ExitState | None) -> ExitProposal:
        base = base_state_for(context, previous)
        facts = collect_exit_facts(context, base)
        arm_threshold = Decimal(str(self._spec.parameters["arm_peak_profit_r"]))
        giveback_threshold = Decimal(
            str(self._spec.parameters["max_giveback_r"]))
        now = facts.now_nano
        side = context.budget.side

        # Shared observation layer: advance the net peak when this
        # observation's Nt qualifies; equal peaks keep the original source.
        changes: dict[str, object] = {}
        if facts.net_liquidation_r is not None:
            candidate = max(Decimal("0"), facts.net_liquidation_r)
            if base.net_peak_r is None or candidate > base.net_peak_r:
                changes = {
                    "net_peak_r": candidate, "net_peak_source_nano": now,
                    "net_peak_source_hash": exit_digest(
                        ["nli", now, facts.net_liquidation_r])}

        n_t = facts.net_liquidation_r
        p_t = facts.net_peak_r  # merged peak incl. this qualifying observation
        reasons: list[str] = []
        if p_t is None:
            reasons.extend(facts.net_peak_reasons or ("DRAWDOWN_PEAK_WARMUP",))
            return self._proposal(context, base, changes, "KEEP", now, reasons,
                                  side, facts.remaining_lots, p_t, n_t)
        previously_armed = base.net_peak_r is not None \
            and base.net_peak_r >= arm_threshold
        drawdown_armed = p_t >= arm_threshold
        if drawdown_armed and not previously_armed:
            reasons.append("DRAWDOWN_ARMED")
        if n_t is None:
            reasons.extend(facts.nli_reasons or ("NLI_UNVALUED",))
            return self._proposal(context, base, changes, "KEEP", now, reasons,
                                  side, facts.remaining_lots, p_t, n_t)
        if not drawdown_armed:
            reasons.append("DRAWDOWN_NOT_ARMED")
            return self._proposal(context, base, changes, "KEEP", now, reasons,
                                  side, facts.remaining_lots, p_t, n_t)
        giveback = p_t - n_t
        if giveback >= giveback_threshold:
            reasons.append("DRAWDOWN_GIVEBACK_BREACHED")
            return self._proposal(context, base, changes, "EXIT", now, reasons,
                                  side, 0, p_t, n_t)
        reasons.append("DRAWDOWN_KEEP")
        return self._proposal(context, base, changes, "KEEP", now, reasons,
                              side, facts.remaining_lots, p_t, n_t)

    def _proposal(self, context: ExitContext, base: ExitState,
                  changes: dict[str, object], action: str, now: int,
                  reasons: list[str], side: int, remaining: int,
                  peak_value, nli_value) -> ExitProposal:
        input_core = [
            context.account_id, context.episode_id,
            context.exit_binding.binding_sha256, self._spec.exit_policy_id,
            now, action,
            str(peak_value) if peak_value is not None else None,
            str(nli_value) if nli_value is not None else None,
            str(Decimal(str(self._spec.parameters["arm_peak_profit_r"]))),
            str(Decimal(str(self._spec.parameters["max_giveback_r"]))),
            base.state_prefix_hash]
        input_sha256 = exit_digest(["drawdown-input", input_core])
        proposal_id = exit_digest([
            "exit-proposal", context.exit_binding.binding_sha256,
            self._spec.exit_policy_id, now, input_sha256])
        next_state = advance_state(base, proposal_id, **changes) if changes \
            else base
        target = 0 if action == "EXIT" else side * remaining
        earliest = next_window_start(context.sessions, now,
                                     self._spec.observation_duration_seconds)
        return ExitProposal(
            proposal_id=proposal_id,
            strategy_id=context.strategy_id, episode_id=context.episode_id,
            contract_id=context.contract_id,
            exit_binding_sha256=context.exit_binding.binding_sha256,
            action=action, target_lots=target,
            triggering_policy_id=self._spec.exit_policy_id,
            input_sha256=input_sha256, observed_at_nano=now,
            earliest_execution_nano=max(earliest, now), protection=None,
            next_state=next_state,
            reasons=tuple(reason for reason in dict.fromkeys(reasons)
                          if reason in DRAWDOWN_REASON_VOCABULARY))
