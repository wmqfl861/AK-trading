"""LEGACY contrast-policy adapter (08-M3C §3/§5.4, M3C-H02).

The legacy exit policy wraps the *existing* exit machinery — the frozen
S0/T0 barrier matching, the risk timer / lifecycle / holding-deadline
exits and the valuation-failure exits — which keep running in their
original M3B/M4 channels under ``LEGACY_PROTECTIVE`` timing
(``STOP``/``TARGET``/``TIME_EXIT``/``ROLL_EXIT`` intent purposes). It is a
对照 policy: it adds **zero** new exit decisions, and that is exactly how
equivalence (M3C-H02: identical actions, fills, budgets, fees and
recovery facts versus the pre-M3C path) is guaranteed — no second,
divergent copy of the original checks is created here (08 §3 red line).

Consequently ``evaluate`` always emits a KEEP candidate whose next_state
is the unchanged committed state. The reason vocabulary deliberately
avoids the ``STOP``/``TARGET``/``ROLL`` substrings so no downstream
reason-sniffing router can misroute this candidate into the legacy
protective matching path (08 §7.1).
"""
from __future__ import annotations

from dataclasses import dataclass

from ak_trading.v2.contracts import ExitContext, ExitProposal, ExitSpec, ExitState
from ak_trading.v2.exits.features import exit_digest, next_window_start
from ak_trading.v2.exits.state import base_state_for

LEGACY_REASON_VOCABULARY = frozenset({
    "LEGACY_DEFERS_TO_ORIGINAL_CHANNELS", "S0_T0_BARRIER_MATCHING_RETAINED",
    "RISK_TIMER_AND_LIFECYCLE_RETAINED", "VALUATION_FAILURE_EXIT_RETAINED",
})


@dataclass(frozen=True)
class LegacyExitPolicy:
    """The LEGACY contrast ExitPolicy: delegate, never duplicate."""

    _spec: ExitSpec

    def __post_init__(self) -> None:
        if self._spec.family != "LEGACY":
            raise ValueError("spec_family_must_be_LEGACY")
        if dict(self._spec.parameters) != {}:
            raise ValueError("legacy_parameters_must_be_empty")

    @property
    def spec(self) -> ExitSpec:
        return self._spec

    def evaluate(self, context: ExitContext,
                 previous: ExitState | None) -> ExitProposal:
        base = base_state_for(context, previous)
        now = context.now_nano
        remaining = sum(lot.lots for lot in context.lots
                        if lot.episode_id == context.episode_id
                        and lot.contract_id == context.contract_id
                        and lot.strategy_id == context.strategy_id)
        input_sha256 = exit_digest([
            "legacy-input", context.account_id, context.episode_id,
            context.exit_binding.binding_sha256, self._spec.exit_policy_id,
            now, remaining, base.state_prefix_hash])
        proposal_id = exit_digest([
            "exit-proposal", context.exit_binding.binding_sha256,
            self._spec.exit_policy_id, now, input_sha256])
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
            earliest_execution_nano=max(earliest, now),
            protection=None, next_state=base,
            reasons=tuple(LEGACY_REASON_VOCABULARY))
