"""Range mean-reversion family (04-M3B-策略库.md §6.4).

Freezes L/M/U. Long: first observe a completed close at or below
``L + 0.20*W``, then a later completed close re-crossing that level while
still below M — only then a candidate. Short mirrors at ``U - 0.20*W``.
One direction per round, never fills in the middle. Target M with the
frozen range stops; the expected spread from the worst allowed entry to M
must cover at least twice the full round-trip cost, positive conservative
EV stays with M4. No ADD in the first version.
"""
from __future__ import annotations

from decimal import Decimal

from ak_trading.v2.strategies.registry import (
    can_open_now,
    cost_coverage_ok,
    episode_id_for,
    evaluate_common,
    evaluate_exits,
    hysteresis_hold,
    make_exit_proposal,
    make_hold_proposal,
    make_open_proposal,
    next_open_nano,
    observation_wait,
    admission_decision,
    open_window_fits,
    range_entry_setup,
)
from ak_trading.v2.contracts import (
    PositionSnapshot,
    StrategyContext,
    StrategyProposal,
    StrategySpec,
    StrategyState,
)
from ak_trading.v2.strategies.state import freeze_round_budget, open_candidate_state


class RangeReversion:
    """Range high-sell/low-buy strategy; pure candidate proposals only."""

    def __init__(self, spec: StrategySpec) -> None:
        self._spec = spec

    @property
    def spec(self) -> StrategySpec:
        return self._spec

    def evaluate(self, context: StrategyContext,
                 previous: StrategyState | None) -> StrategyProposal:
        return evaluate_common(self, context, previous,
                               observe=self._observe, manage=self._manage)

    def _observe(self, context: StrategyContext, state: StrategyState) -> StrategyProposal:
        spec = self._spec
        reasons: list[str] = []
        if context.regime.base not in spec.allowed_regimes:
            reasons.append("REGIME_NOT_ALLOWED")
        unknown_flags = [flag for flag in context.regime.flags if flag not in spec.allowed_flags]
        if unknown_flags:
            reasons.append("FLAG_NOT_ALLOWED")
        frozen = context.regime.frozen_range
        if frozen is None:
            reasons.append("FROZEN_RANGE_MISSING")
        if context.features.atr_points is None or context.features.atr_points <= 0:
            reasons.append("ATR_MISSING")
        if context.rule is None:
            reasons.append("RULE_MISSING")
        if reasons:
            return observation_wait(context, state, None, 0, tuple(reasons))

        fraction = Decimal(str(spec.parameters["entry_fraction"]))
        setup, side, wait_reason = range_entry_setup(context, state, fraction, 5)
        if setup is None or side == 0:
            return observation_wait(context, state, None, state.confirmation_count,
                                    (wait_reason or "RANGE_MIDDLE_WAIT",))
        event = "PERSIST:ORDERED_RANGE"
        gate = admission_decision(
            context, event=event, side=side, horizon=spec.max_holding,
            required_range=frozen, require_return_before_break=True,
            require_competing_unknown=True)
        confirmation = state.confirmation_count + 1 if gate.ok else 0
        required = int(spec.parameters["entry_confirmation_bars"])
        if setup.consecutive_confirmations < 1 or confirmation < required:
            return observation_wait(
                context, state, setup, confirmation,
                gate.reasons if not gate.ok else ("WAITING_RANGE_RECROSS",))
        if not gate.ok:
            return observation_wait(context, state, setup, confirmation, gate.reasons)

        allowed, blocked = can_open_now(context)
        if not allowed:
            return observation_wait(context, state, setup, confirmation, (blocked,))
        if gate.window_end_nano is None or not open_window_fits(context, gate.window_end_nano):
            return observation_wait(context, state, setup, confirmation,
                                    ("OPEN_WINDOW_EXCEEDS_HOLDING_DEADLINE",))
        reference = context.features.reference_price
        if reference is None:
            return observation_wait(context, state, setup, confirmation,
                                    ("REFERENCE_PRICE_MISSING",))
        atr = context.features.atr_points
        ratio = Decimal(str(spec.parameters["minimum_round_trip_cost_ratio"]))
        covered, cost_reason = cost_coverage_ok(
            context.rule, side, reference, frozen.center, atr, ratio)
        if not covered:
            return observation_wait(context, state, setup, confirmation,
                                    (cost_reason or "COST_COVERAGE_INSUFFICIENT",))
        episode_id = episode_id_for(spec, context.features.contract_id, context.now_nano)
        budget = freeze_round_budget(spec, context, side, episode_id)
        if budget is None:
            return observation_wait(context, state, setup, confirmation, ("BUDGET_INFEASIBLE",))
        candidate = open_candidate_state(
            spec, context, episode_id, budget, gate.window_end_nano,
            setup=setup, confirmation_count=confirmation)
        valid_until = min(gate.forecast.expires_at_nano, gate.forecast.window_end_nano)
        if next_open_nano(context) >= valid_until:
            return observation_wait(context, state, setup, confirmation,
                                    ("EXECUTION_WINDOW_EMPTY",))
        return make_open_proposal(
            context, side=side, layer_index=0, trigger_price=setup.trigger_level,
            budget=budget, forecast=gate.forecast, state=candidate,
            reasons=("RANGE_RECROSS_CONFIRMED",))

    def _manage(self, context: StrategyContext, state: StrategyState,
                position: PositionSnapshot) -> StrategyProposal:
        if state.phase == "EXITING":
            return make_exit_proposal(context, state, ("EXIT_PENDING",))
        reasons = evaluate_exits(context, state)
        if reasons:
            return make_exit_proposal(context, state, reasons)
        if hysteresis_hold(context, state):
            return make_hold_proposal(context, state, position.lots, ("HYSTERESIS_BAND_MANAGED",))
        return make_hold_proposal(context, state, position.lots, ("REVERSION_HOLD",))
