"""Fixed-inventory grid family (04-M3B-策略库.md §6.5).

One side per round, three layers of the same base unit q triggering at
``L + 0.25W / L + 0.15W / L + 0.05W`` (short mirrors from U). The initial
layer needs the same auditable boundary-return confirmation as the
martingale first layer; later layers are completed closes crossing the
predetermined level in the adverse direction, each re-checking range,
confidence, cost and budget. Same q per layer, full 3q inventory, one
frozen stop and target M; q and layers never change on floating loss.
WIDE_RANGE needs an independently validated version (default off).
"""
from __future__ import annotations

from decimal import Decimal

from ak_trading.v2 import policy as m1_policy
from ak_trading.v2.contracts import (
    PositionSnapshot,
    StrategyContext,
    StrategyProposal,
    StrategySpec,
    StrategyState,
)
from ak_trading.v2.strategies.registry import (
    advance_confirmation_count,
    can_open_now,
    cost_coverage_ok,
    episode_id_for,
    evaluate_common,
    evaluate_exits,
    hysteresis_hold,
    make_add_proposal,
    make_exit_proposal,
    make_hold_proposal,
    make_open_proposal,
    next_open_nano,
    observation_bars,
    observation_wait,
    admission_decision,
    open_window_fits,
    range_entry_setup,
    select_add_horizon,
)
from ak_trading.v2.strategies.state import (
    add_candidate_state,
    freeze_round_budget,
    open_candidate_state,
)


class FixedInventoryGrid:
    """Fixed-inventory grid strategy; pure candidate proposals only."""

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

        fractions = tuple(Decimal(str(value)) for value in spec.parameters["layer_fractions"])
        setup, side, wait_reason = range_entry_setup(context, state, fractions[0], 5)
        if setup is None or side == 0:
            # §7/phase0-F1: an expired observation period leaves no credit.
            hold = 0 if (state.setup is not None
                         and context.now_nano > state.setup.expires_at_nano) \
                else state.confirmation_count
            return observation_wait(context, state, None, hold,
                                    (wait_reason or "RANGE_MIDDLE_WAIT",))
        gate = admission_decision(
            context, event="PERSIST:ORDERED_RANGE", side=side, horizon=spec.max_holding,
            required_range=frozen, require_return_before_break=True,
            require_competing_unknown=True)
        created = setup is not None and (
            state.setup is None or setup.setup_id != state.setup.setup_id)
        confirmation = advance_confirmation_count(
            context, state, gate_ok=gate.ok, setup=setup, created=created)
        required = int(spec.parameters["entry_confirmation_bars"])
        if setup.consecutive_confirmations < 1 or confirmation < required:
            return observation_wait(
                context, state, setup, confirmation,
                gate.reasons if not gate.ok else ("WAITING_GRID_RECROSS",))
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
        covered, cost_reason = cost_coverage_ok(
            context.rule, side, reference, frozen.center,
            context.features.atr_points or Decimal("1"), Decimal("1"))
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
            reasons=("GRID_RECROSS_CONFIRMED",))

    def _manage(self, context: StrategyContext, state: StrategyState,
                position: PositionSnapshot) -> StrategyProposal:
        if state.phase == "EXITING":
            return make_exit_proposal(context, state, ("EXIT_PENDING",))
        reasons = evaluate_exits(context, state)
        if reasons:
            return make_exit_proposal(context, state, reasons)
        add = self._try_add(context, state, position)
        if add is not None:
            return add
        if hysteresis_hold(context, state):
            return make_hold_proposal(context, state, position.lots, ("HYSTERESIS_BAND_MANAGED",))
        return make_hold_proposal(context, state, position.lots, ("GRID_HOLD",))

    def _try_add(self, context: StrategyContext, state: StrategyState,
                 position: PositionSnapshot) -> StrategyProposal | None:
        spec = self._spec
        budget = state.budget
        if budget is None or state.active_exit_nano is None:
            return None
        if state.pending_layer_index is not None:
            return None
        frozen = budget.frozen_range
        current = context.regime.frozen_range
        if frozen is None or current is None or current.range_id != frozen.range_id:
            return None
        next_index = len(state.filled_layers)
        if next_index >= len(budget.layers) or next_index >= spec.max_layers:
            return None
        bars = observation_bars(context)
        if not bars:
            return None
        layer = budget.layers[next_index]
        close = bars[-1].close
        adverse_cross = (close <= layer.trigger_price if budget.side == 1
                         else close >= layer.trigger_price)
        if not adverse_cross:
            # Floating loss alone is never an add trigger.
            return None
        allowed_open, _ = can_open_now(context)
        if not allowed_open:
            return None  # new risk is forbidden near the holding-segment end
        horizon = select_add_horizon(context, state.active_exit_nano)
        if horizon is None:
            return None
        gate = admission_decision(
            context, event="PERSIST:ORDERED_RANGE", side=budget.side, horizon=horizon,
            required_range=frozen, require_return_before_break=True,
            require_competing_unknown=True)
        if not gate.ok or gate.window_end_nano is None:
            return None
        rule = context.rule
        atr = context.features.atr_points
        if rule is None or atr is None:
            return None
        covered, _ = cost_coverage_ok(
            rule, budget.side, layer.reference_entry_price, budget.target_price, atr,
            Decimal("1"))
        if not covered:
            return None
        research = m1_policy.research_policy()
        equity = context.account.equity_cny
        stress_cap = equity * Decimal(str(research["maximum_stress_loss_to_equity"]))
        margin_cap = equity * Decimal(str(research["maximum_strategy_margin_ratio"]))
        if budget.stress_loss_cny > stress_cap or budget.reserved_margin_cny > margin_cap:
            return None
        candidate = add_candidate_state(state, next_index, gate.window_end_nano)
        valid_until = min(gate.forecast.expires_at_nano, gate.forecast.window_end_nano,
                          gate.window_end_nano)
        if next_open_nano(context) >= valid_until:
            return None
        return make_add_proposal(
            context, layer_index=next_index, current_lots=position.lots,
            forecast=gate.forecast, state=candidate,
            reasons=(f"GRID_LAYER_{next_index}_CROSSED",))
