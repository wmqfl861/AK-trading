"""Limited martingale mean-reversion family (04-M3B-策略库.md §6.6).

Strict q / 2q / 4q template (three layers, seven base units, the M1
seven-lot research cap forces q=1). Admission: ORDERED_RANGE only, no
EVENT_SHOCK/ILLIQUID/BREAKOUT, range persistence >= 0.85, the matching
frozen stop-touch probability <= 0.10 with VALID evidence, RETURN_BEFORE_BREAK
valid with terminal competing unknown <= 0.02. The first layer uses the
same auditable price trigger as the fixed grid (close at or below
``L + 0.25W`` arms; a later close strictly re-crossing while below M
confirms; first touch and re-cross are different bars; qualification must
hold for three consecutive observations). The whole ``RoundBudget`` (all
three layers, worst allowed entries, fees, stress via the M1 pure formula,
peak margin over every layer path, day loss, equity base) freezes before
the first layer and never expands. ADDs re-check everything; floating loss
alone is never a trigger; one gap bar crossing two layers proposes at most
the next single layer; stops never move outward and losses are never
chased across episodes.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Literal, Mapping
from types import MappingProxyType

from ak_trading.v2 import policy as m1_policy
from ak_trading.v2.contracts import (
    LayerSpec,
    PositionSnapshot,
    StrategyContext,
    StrategyProposal,
    StrategySpec,
    StrategyState,
)
from ak_trading.v2.strategies.registry import (
    RESEARCH_MAXIMUM_ROUND_LOTS,
    advance_confirmation_count,
    can_open_now,
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
    StateTransitionError,
    add_candidate_state,
    freeze_round_budget,
    open_candidate_state,
    round_loss_cny,
)

MARTINGALE_MULTIPLIERS = (1, 2, 4)


class LimitedMartingale:
    """Limited 1/2/4 martingale; pure candidate proposals only."""

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
            required_range=frozen, require_stop_touch=True,
            require_return_before_break=True, require_competing_unknown=True)
        created = setup is not None and (
            state.setup is None or setup.setup_id != state.setup.setup_id)
        confirmation = advance_confirmation_count(
            context, state, gate_ok=gate.ok, setup=setup, created=created)
        required = int(spec.parameters["entry_confirmation_bars"])
        if setup.consecutive_confirmations < 1 or confirmation < required:
            return observation_wait(
                context, state, setup, confirmation,
                gate.reasons if not gate.ok else ("WAITING_MARTINGALE_RECROSS",))
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
            reasons=("MARTINGALE_RECROSS_CONFIRMED",))

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
            return make_hold_proposal(
                context, state, position.lots,
                ("HYSTERESIS_BAND_MANAGED", "MARTINGALE_ADD_SUSPENDED"))
        return make_hold_proposal(
            context, state, position.lots, ("MARTINGALE_HOLD",))

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
            # Floating loss itself is never an add trigger.
            return None
        allowed_open, _ = can_open_now(context)
        if not allowed_open:
            return None  # new risk is forbidden near the holding-segment end
        horizon = select_add_horizon(context, state.active_exit_nano)
        if horizon is None:
            return None
        gate = admission_decision(
            context, event="PERSIST:ORDERED_RANGE", side=budget.side, horizon=horizon,
            required_range=frozen, require_stop_touch=True,
            require_return_before_break=True, require_competing_unknown=True)
        if not gate.ok or gate.window_end_nano is None:
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
            reasons=(f"MARTINGALE_LAYER_{next_index}_CROSSED",))


def comparison_round_plans(rule, stress_policy, *, side: Literal[-1, 1],
                           stop_price: Decimal, initial_atr_points: Decimal,
                           layer_reference_prices: tuple[Decimal, ...],
                           budget_cap_cny: Decimal) -> Mapping[str, tuple[LayerSpec, ...] | None]:
    """Fair contrasts under one identical planned-loss cap (§6.6 / §10-6).

    Schemes: ``MARTINGALE_1_2_4`` (q,2q,4q), ``EQUAL_BATCHES`` (q,q,q) and
    ``NO_ADD`` (single layer). For each, the largest integer base unit whose
    planned round loss fits the same cap is chosen descending; infeasible
    schemes return ``None``. The martingale plan never receives a larger cap
    than the contrasts, and unused budget (cap minus planned) stays visible.
    """
    schemes: dict[str, tuple[int, ...]] = {
        "MARTINGALE_1_2_4": MARTINGALE_MULTIPLIERS,
        "EQUAL_BATCHES": (1, 1, 1),
        "NO_ADD": (1,),
    }
    plans: dict[str, tuple[LayerSpec, ...] | None] = {}
    for name, multipliers in schemes.items():
        total_units = sum(multipliers)
        base_cap = RESEARCH_MAXIMUM_ROUND_LOTS // total_units
        chosen: tuple[LayerSpec, ...] | None = None
        minimum_lots = max(rule.lot_step, rule.min_open_lots)
        for base_lots in range(base_cap, 0, -1):
            if base_lots < minimum_lots or base_lots % rule.lot_step != 0:
                continue
            references = (layer_reference_prices
                          if len(multipliers) == len(layer_reference_prices)
                          else layer_reference_prices[:1] * len(multipliers))
            layers = tuple(
                LayerSpec(index=index, trigger_price=price, reference_entry_price=price,
                          lots=multiplier * base_lots)
                for index, (price, multiplier) in enumerate(zip(references, multipliers)))
            try:
                planned = round_loss_cny(layers, side, stop_price, rule, initial_atr_points)
            except StateTransitionError:
                chosen = None
                break
            if planned <= budget_cap_cny:
                chosen = layers
                break
        plans[name] = chosen
    return MappingProxyType(plans)
