"""Trend pullback / reverse-dip family (04-M3B-策略库.md §6.3).

While the background trend stays valid the MA20 and a ±0.5*ATR pullback
zone freeze. Long: a completed close enters the zone, then a later
completed close reclaims the frozen MA20 while exceeding the previous close
— the short side is the exact mirror. The setup expires after 5 bars of
this frequency; execution happens only at the next legal open. Exits are
the frozen 1*ATR stop and background-qualification failure; no future
"the low is already in" information is ever consulted.
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
from ak_trading.v2.strategies.registry import (
    atr_of,
    can_open_now,
    episode_id_for,
    evaluate_common,
    evaluation_nano,
    evaluate_exits,
    factor_decimal,
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


class TrendPullback:
    """With-trend pullback strategy; pure candidate proposals only."""

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
        if "PULLBACK" not in context.regime.flags:
            reasons.append("PULLBACK_FLAG_REQUIRED")
        ma20 = factor_decimal(context, "ma20")
        atr = atr_of(context)
        if ma20 is None or ma20 is None or atr is None or atr <= 0:
            reasons.append("PULLBACK_FACTORS_MISSING")
        if reasons or not bars:
            return observation_wait(context, state, None, 0,
                                    tuple(reasons) or ("NO_COMPLETED_BARS",))
        side = 1 if context.regime.base == "UP_TREND" else -1
        distance = Decimal(str(spec.parameters["pullback_distance_atr"]))
        setup = self._advance_setup(context, state.setup, side, bars, ma20, atr, distance)
        if setup is None:
            return observation_wait(context, state, None, state.confirmation_count,
                                    ("PULLBACK_ZONE_NOT_ENTERED",))
        event = "PERSIST:UP_TREND" if side == 1 else "PERSIST:DOWN_TREND"
        gate = admission_decision(context, event=event, side=side, horizon=spec.max_holding,
            checked_at_nano=checked_at_nano)
        confirmation = state.confirmation_count + 1 if gate.ok else 0
        required = int(spec.parameters["entry_confirmation_bars"])
        confirmed = setup.consecutive_confirmations >= 1 and setup.armed
        if not confirmed or confirmation < required:
            return observation_wait(
                context, state, setup, confirmation,
                gate.reasons if not gate.ok else ("WAITING_PULLBACK_RECLAIM",))
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
            context, side=side, layer_index=0, trigger_price=setup.frozen_ma,
            budget=budget, forecast=gate.forecast, state=candidate,
            reasons=("PULLBACK_RECLAIM_CONFIRMED",))

    def _advance_setup(self, context: StrategyContext, previous: StrategySetup | None,
                       side: int, bars, ma20: Decimal, atr: Decimal,
                       distance: Decimal) -> StrategySetup | None:
        spec = self._spec
        bar = bars[-1]
        close = bar.close
        expiry_bars = int(spec.parameters["setup_expiry_bars"])
        if previous is None or previous.side != side \
                or context.now_nano > previous.expires_at_nano:
            frozen_ma, frozen_atr, started = ma20, atr, bar.end_nano
            observed, armed, confirmations, previous_close = 1, False, 0, close
            base_id = stable_id("setup", f"{spec.strategy_id}|{bar.bar_id}|{side}")
        elif previous.last_bar_id == bar.bar_id:
            return previous
        else:
            frozen_ma = previous.frozen_ma
            frozen_atr = previous.frozen_atr
            started = previous.started_at_nano
            observed = previous.observed_bars + 1
            armed = previous.armed
            confirmations = previous.consecutive_confirmations
            previous_close = previous.previous_close
            base_id = previous.setup_id
        zone_low = frozen_ma - distance * frozen_atr
        zone_high = frozen_ma + distance * frozen_atr
        reclaimed = (armed and previous_close is not None
                     and ((side == 1 and close > frozen_ma and close > previous_close)
                          or (side == -1 and close < frozen_ma and close < previous_close)))
        newly_armed = armed or (zone_low <= close <= zone_high)
        if reclaimed:
            confirmations += 1
        return StrategySetup(
            setup_id=base_id, strategy_id=spec.strategy_id,
            contract_id=bar.contract_id, side=side, started_at_nano=started,
            expires_at_nano=setup_expiry_nano(context, started, expiry_bars),
            observed_bars=observed, frozen_range=None, frozen_ma=frozen_ma,
            frozen_atr=frozen_atr, trigger_level=frozen_ma, armed=newly_armed,
            consecutive_confirmations=confirmations, previous_close=close,
            last_bar_id=bar.bar_id)

    def _manage(self, context: StrategyContext, state: StrategyState,
                position: PositionSnapshot, *,
                checked_at_nano: int | None = None) -> StrategyProposal:
        budget = state.budget
        extras: tuple[str, ...] = ()
        if budget is not None:
            expected = "UP_TREND" if budget.side == 1 else "DOWN_TREND"
            if context.regime.base != expected:
                extras = ("BACKGROUND_TREND_LOST",)
        if state.phase == "EXITING":
            return make_exit_proposal(context, state, ("EXIT_PENDING",))
        reasons = evaluate_exits(
            context, state, extras, checked_at_nano=checked_at_nano)
        if reasons:
            return make_exit_proposal(context, state, reasons)
        if hysteresis_hold(context, state, checked_at_nano=checked_at_nano):
            return make_hold_proposal(context, state, position.lots, ("HYSTERESIS_BAND_MANAGED",))
        return make_hold_proposal(context, state, position.lots, ("PULLBACK_HOLD",))
