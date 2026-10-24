"""Confirmed breakout family (04-M3B-策略库.md §6.2).

Consumes M3A's complete frozen prior 20-bar high/low boundary; requires three completed
closes beyond ``U + 1*tick`` (or below ``L - 1*tick``) all relative to the
same frozen boundary plus a qualifying ``PERSIST:UP/DOWN_BREAKOUT`` forecast
whose frozen range matches the setup. False-breakout exit fires when a
completed close re-enters the boundary; stop is 1 ATR / target 2 ATR from
the entry reference and the maximum holding horizon applies in parallel.
"""
from __future__ import annotations

from decimal import Decimal

from ak_trading.v2.artifacts import stable_id
from ak_trading.v2.contracts import (
    PositionSnapshot,
    StrategyContext,
    StrategyProposal,
    StrategySpec,
    StrategyState,
    StrategySetup,
)
from ak_trading.v2.probability.regimes import freeze_range
from ak_trading.v2.strategies.registry import (
    atr_of,
    can_open_now,
    cost_coverage_ok,
    episode_id_for,
    evaluate_common,
    evaluation_nano,
    evaluate_exits,
    hysteresis_hold,
    make_exit_proposal,
    make_hold_proposal,
    make_open_proposal,
    next_open_nano,
    observation_bars,
    observation_wait,
    admission_decision,
    open_window_fits,
    setup_expiry_nano,
)
from ak_trading.v2.strategies.state import freeze_round_budget, open_candidate_state

PRIOR_WINDOW = 20


def _prior_boundary(bars) -> tuple[Decimal, Decimal] | None:
    window = bars[-(PRIOR_WINDOW + 1):-1]
    if len(window) < PRIOR_WINDOW:
        return None
    highs = [bar.high for bar in window if bar.high is not None]
    lows = [bar.low for bar in window if bar.low is not None]
    if len(highs) < PRIOR_WINDOW or len(lows) < PRIOR_WINDOW:
        return None
    return min(lows), max(highs)


class ConfirmedBreakout:
    """Breakout-confirmation strategy; pure candidate proposals only."""

    def __init__(self, spec: StrategySpec) -> None:
        self._spec = spec

    @property
    def spec(self) -> StrategySpec:
        return self._spec

    def evaluate(self, context: StrategyContext,
                 previous: StrategyState | None, *,
                 checked_at_nano: int | None = None) -> StrategyProposal:
        evaluation_nano(context, checked_at_nano)
        return evaluate_common(
            self, context, previous,
            observe=lambda ctx, state: self._observe(
                ctx, state, checked_at_nano=checked_at_nano),
            manage=lambda ctx, state, position: self._manage(
                ctx, state, position, checked_at_nano=checked_at_nano))

    def _observe(self, context: StrategyContext, state: StrategyState, *,
                 checked_at_nano: int | None = None) -> StrategyProposal:
        spec = self._spec
        bars = observation_bars(context)
        reasons: list[str] = []
        if context.regime.base not in spec.allowed_regimes:
            reasons.append("REGIME_NOT_ALLOWED")
        unknown_flags = [flag for flag in context.regime.flags if flag not in spec.allowed_flags]
        if unknown_flags:
            reasons.append("FLAG_NOT_ALLOWED")
        if "BREAKOUT" not in context.regime.flags:
            reasons.append("BREAKOUT_FLAG_REQUIRED")
        atr = atr_of(context)
        rule = context.rule
        if atr is None or atr <= 0:
            reasons.append("ATR_MISSING")
        if rule is None:
            reasons.append("RULE_MISSING")
        if reasons or not bars:
            return observation_wait(context, state, None, 0,
                                    tuple(reasons) or ("NO_COMPLETED_BARS",))
        close = bars[-1].close
        buffer_ticks = Decimal(str(spec.parameters["breakout_buffer_ticks"]))
        tick = rule.price_tick
        buffer = buffer_ticks * tick

        setup = state.setup
        if setup is not None and context.now_nano > setup.expires_at_nano:
            setup = None
        same_bar = setup is not None and setup.last_bar_id == bars[-1].bar_id
        created = False
        side = 0
        trigger: Decimal | None = None
        if setup is not None:
            frozen = setup.frozen_range
            original_atr = setup.frozen_atr
            if (frozen is None or context.regime.frozen_range != frozen
                    or original_atr is None or original_atr <= 0
                    or frozen.center != (frozen.lower + frozen.upper) / Decimal(2)
                    or frozen.stop_lower != frozen.lower - original_atr
                    or frozen.stop_upper != frozen.upper + original_atr
                    or setup.trigger_level != (
                        frozen.upper + buffer if setup.side == 1 else frozen.lower - buffer)):
                return observation_wait(context, state, setup, 0,
                                        ("SETUP_RANGE_MISMATCH",))
            side = setup.side
            trigger = setup.trigger_level
            if not same_bar:
                beyond = close > trigger if side == 1 else close < trigger
                if not beyond:
                    setup = None
                    side = 0
        if setup is None:
            boundary = _prior_boundary(bars)
            if boundary is None:
                return observation_wait(context, state, None, 0,
                                        ("INSUFFICIENT_BARS_FOR_BOUNDARY",))
            low, high = boundary
            frozen = context.regime.frozen_range
            if frozen is None:
                return observation_wait(context, state, None, 0,
                                        ("FROZEN_RANGE_REQUIRED",))
            # M3A alone owns new range identity. Recompute through that producer
            # only to validate the complete context original; never substitute
            # the result for a missing, stale or inconsistent supplied object.
            if (frozen != freeze_range(context.features)
                    or frozen.lower != low or frozen.upper != high
                    or frozen.frozen_at_nano != context.now_nano):
                return observation_wait(context, state, None, 0,
                                        ("FROZEN_RANGE_MISMATCH",))
            if close > high + buffer:
                side, trigger = 1, high + buffer
            elif close < low - buffer:
                side, trigger = -1, low - buffer
            else:
                return observation_wait(context, state, None, 0,
                                        ("NO_BOUNDARY_CROSS",))
            started = bars[-1].end_nano
            setup = StrategySetup(
                setup_id=stable_id("setup", f"{spec.strategy_id}|{bars[-1].bar_id}|{side}"),
                strategy_id=spec.strategy_id, contract_id=bars[-1].contract_id,
                side=side, started_at_nano=started,
                expires_at_nano=setup_expiry_nano(context, started, 3),
                observed_bars=1, frozen_range=frozen, frozen_ma=None, frozen_atr=atr,
                trigger_level=trigger, armed=True, consecutive_confirmations=1,
                previous_close=close, last_bar_id=bars[-1].bar_id)
            created = True
        elif not same_bar:
            from ak_trading.v2.contracts import StrategySetup as _Setup
            setup = _Setup(
                setup_id=setup.setup_id, strategy_id=setup.strategy_id,
                contract_id=setup.contract_id, side=setup.side,
                started_at_nano=setup.started_at_nano,
                expires_at_nano=setup.expires_at_nano,
                observed_bars=setup.observed_bars + 1,
                frozen_range=setup.frozen_range, frozen_ma=setup.frozen_ma,
                frozen_atr=setup.frozen_atr, trigger_level=setup.trigger_level,
                armed=setup.armed, consecutive_confirmations=setup.consecutive_confirmations + 1,
                previous_close=close, last_bar_id=bars[-1].bar_id)

        event = "PERSIST:UP_BREAKOUT" if side == 1 else "PERSIST:DOWN_BREAKOUT"
        gate = admission_decision(
            context, event=event, side=side, horizon=spec.max_holding,
            required_range=setup.frozen_range, checked_at_nano=checked_at_nano)
        if not gate.ok:
            confirmation = 0
        elif created:
            confirmation = 1
        elif same_bar:
            confirmation = state.confirmation_count
        else:
            confirmation = state.confirmation_count + 1
        required = int(spec.parameters["entry_confirmation_bars"])
        if setup.consecutive_confirmations < required or confirmation < required:
            return observation_wait(
                context, state, setup, confirmation,
                gate.reasons if not gate.ok else ("WAITING_BREAKOUT_CONFIRMATION",))
        if not gate.ok:
            return observation_wait(context, state, setup, confirmation, gate.reasons)

        allowed, blocked = can_open_now(context, checked_at_nano=checked_at_nano)
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
            rule, side, reference, reference + side * Decimal(2) * atr, atr, Decimal("2"))
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
            reasons=("BREAKOUT_CONFIRMED",))

    def _manage(self, context: StrategyContext, state: StrategyState,
                position: PositionSnapshot, *,
                checked_at_nano: int | None = None) -> StrategyProposal:
        bars = observation_bars(context)
        extras: tuple[str, ...] = ()
        budget = state.budget
        setup = state.setup
        if budget is not None and setup is not None and bars:
            close = bars[-1].close
            boundary = setup.trigger_level - budget.side * Decimal(
                str(self._spec.parameters["breakout_buffer_ticks"])) * (
                    context.rule.price_tick if context.rule else Decimal("0"))
            if (budget.side == 1 and close <= boundary) or \
                    (budget.side == -1 and close >= boundary):
                extras = ("FALSE_BREAKOUT",)
        if state.phase == "EXITING":
            return make_exit_proposal(context, state, ("EXIT_PENDING",))
        reasons = evaluate_exits(
            context, state, extras, checked_at_nano=checked_at_nano)
        if reasons:
            return make_exit_proposal(context, state, reasons)
        if hysteresis_hold(context, state, checked_at_nano=checked_at_nano):
            return make_hold_proposal(context, state, position.lots, ("HYSTERESIS_BAND_MANAGED",))
        return make_hold_proposal(context, state, position.lots, ("BREAKOUT_HOLD",))
