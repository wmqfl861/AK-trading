"""Trend following family (04-M3B-策略库.md §6.1).

Long: ``UP_TREND`` with ``MA20 > MA60``, ``(MA20 - MA60) / ATR >= 0.25`` and
the last three completed closes each at or above their own then-visible
MA20; short is the exact mirror. Targets/stops freeze at the known decision
reference ``C`` (``C + 2*ATR`` / ``C - ATR``) and never move with the actual
entry price. An existing position is never re-opened minute by minute, and
the first version has no ADD.
"""
from __future__ import annotations

from decimal import Decimal

from ak_trading.v2.contracts import (
    PositionSnapshot,
    StrategyContext,
    StrategyProposal,
    StrategySpec,
    StrategyState,
    StrategySetup,
)
from ak_trading.v2.strategies.registry import (
    advance_confirmation_count,
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

TREND_MA_WINDOW = 20
TREND_CLOSE_LOOKBACK = 3


def _ma_at(closes: list[Decimal], end_index: int) -> Decimal:
    window = closes[end_index - TREND_MA_WINDOW + 1:end_index + 1]
    return sum(window) / Decimal(len(window))


class TrendFollowing:
    """Trend-following strategy; pure candidate proposals only."""

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

    # -- observation ---------------------------------------------------------

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
        ma20 = factor_decimal(context, "ma20")
        ma60 = factor_decimal(context, "ma60")
        atr = atr_of(context)
        if ma20 is None or ma60 is None or atr is None or atr <= 0:
            reasons.append("TREND_FACTORS_MISSING")
        if reasons or not bars:
            return self._wait(context, state, None, 0, tuple(reasons) or ("NO_COMPLETED_BARS",))

        spread = Decimal(str(spec.parameters["entry_spread_atr"]))
        side = 0
        if ma20 > ma60 and (ma20 - ma60) / atr >= spread:
            side = 1
        elif ma60 > ma20 and (ma60 - ma20) / atr >= spread:
            side = -1
        closes = [bar.close for bar in bars if bar.close is not None]
        needed = TREND_MA_WINDOW + TREND_CLOSE_LOOKBACK - 1
        if side == 0:
            return self._wait(context, state, None, 0, ("TREND_SPREAD_NOT_QUALIFIED",))
        if len(closes) < needed:
            return self._wait(context, state, state.setup, state.confirmation_count,
                              ("INSUFFICIENT_BARS_FOR_MA",))

        structure_count = 0
        for index in range(len(closes) - TREND_CLOSE_LOOKBACK, len(closes)):
            ma_at = _ma_at(closes, index)
            if side == 1 and closes[index] >= ma_at:
                structure_count += 1
            elif side == -1 and closes[index] <= ma_at:
                structure_count += 1
            else:
                structure_count = 0
        setup = self._advance_setup(context, state.setup, side, bars, structure_count,
                                    ma20, atr)
        event = "PERSIST:UP_TREND" if side == 1 else "PERSIST:DOWN_TREND"
        gate = admission_decision(
            context, event=event, side=side, horizon=spec.max_holding,
            checked_at_nano=checked_at_nano)
        # §7/§10-11 (shared counter): advances once per distinct completed
        # bar; a re-delivered decision context holds the counter, and an
        # expired observation period never lends credit to a rebuilt one.
        confirmation = advance_confirmation_count(
            context, state, gate_ok=gate.ok, setup=setup)
        required = int(spec.parameters["entry_confirmation_bars"])
        if structure_count < required or confirmation < required:
            return self._wait(context, state, setup, confirmation,
                              gate.reasons if not gate.ok else ("WAITING_ENTRY_CONFIRMATION",))
        if not gate.ok:
            return self._wait(context, state, setup, confirmation, gate.reasons)

        allowed, blocked = can_open_now(context, checked_at_nano=checked_at_nano)
        if not allowed:
            return self._wait(context, state, setup, confirmation, (blocked,))
        if gate.window_end_nano is None or not open_window_fits(context, gate.window_end_nano):
            return self._wait(context, state, setup, confirmation,
                              ("OPEN_WINDOW_EXCEEDS_HOLDING_DEADLINE",))
        reference = context.features.reference_price
        if reference is None:
            return self._wait(context, state, setup, confirmation, ("REFERENCE_PRICE_MISSING",))
        episode_id = episode_id_for(spec, context.features.contract_id, context.now_nano)
        budget = freeze_round_budget(spec, context, side, episode_id)
        if budget is None:
            return self._wait(context, state, setup, confirmation, ("BUDGET_INFEASIBLE",))
        candidate = open_candidate_state(
            spec, context, episode_id, budget, gate.window_end_nano,
            setup=setup, confirmation_count=confirmation)
        valid_until = min(gate.forecast.expires_at_nano, gate.forecast.window_end_nano)
        if next_open_nano(context) >= valid_until:
            return self._wait(context, state, setup, confirmation, ("EXECUTION_WINDOW_EMPTY",))
        return make_open_proposal(
            context, side=side, layer_index=0, trigger_price=reference, budget=budget,
            forecast=gate.forecast, state=candidate, reasons=("TREND_ENTRY_CONFIRMED",))

    def _advance_setup(self, context: StrategyContext, previous: StrategySetup | None,
                       side: int, bars, structure_count: int, ma20: Decimal,
                       atr: Decimal) -> StrategySetup | None:
        if structure_count <= 0:
            return None
        from ak_trading.v2.artifacts import stable_id
        bar = bars[-1]
        expiry_bars = 3
        if (previous is not None and previous.side == side
                and context.now_nano < previous.expires_at_nano
                and previous.last_bar_id != bar.bar_id):
            return StrategySetup(
                setup_id=previous.setup_id, strategy_id=previous.strategy_id,
                contract_id=previous.contract_id, side=side,
                started_at_nano=previous.started_at_nano,
                expires_at_nano=previous.expires_at_nano,
                observed_bars=previous.observed_bars + 1,
                frozen_range=None, frozen_ma=previous.frozen_ma,
                frozen_atr=previous.frozen_atr, trigger_level=previous.trigger_level,
                armed=previous.armed,
                consecutive_confirmations=structure_count,
                previous_close=bar.close, last_bar_id=bar.bar_id)
        if previous is not None and previous.last_bar_id == bar.bar_id:
            return previous
        started = bar.end_nano
        return StrategySetup(
            setup_id=stable_id("setup", f"{self._spec.strategy_id}|{bar.bar_id}|{side}"),
            strategy_id=self._spec.strategy_id,
            contract_id=bar.contract_id, side=side, started_at_nano=started,
            expires_at_nano=setup_expiry_nano(context, started, expiry_bars),
            observed_bars=1, frozen_range=None, frozen_ma=ma20, frozen_atr=atr,
            trigger_level=bar.close, armed=False,
            consecutive_confirmations=structure_count, previous_close=bar.close,
            last_bar_id=bar.bar_id)

    # -- management ------------------------------------------------------------

    def _manage(self, context: StrategyContext, state: StrategyState,
                position: PositionSnapshot, *,
                checked_at_nano: int | None = None) -> StrategyProposal:
        spec = self._spec
        extras: tuple[str, ...] = ()
        ma20 = factor_decimal(context, "ma20")
        ma60 = factor_decimal(context, "ma60")
        budget = state.budget
        if budget is not None and ma20 is not None and ma60 is not None:
            if (budget.side == 1 and ma20 <= ma60) or (budget.side == -1 and ma60 <= ma20):
                extras = ("TREND_CONDITION_REVERSED",)
        if state.phase == "EXITING":
            return make_exit_proposal(context, state, ("EXIT_PENDING",))
        reasons = evaluate_exits(
            context, state, extras, checked_at_nano=checked_at_nano)
        if reasons:
            return make_exit_proposal(context, state, reasons)
        if hysteresis_hold(context, state, checked_at_nano=checked_at_nano):
            return make_hold_proposal(
                context, state, position.lots, ("HYSTERESIS_BAND_MANAGED",))
        return make_hold_proposal(context, state, position.lots, ("TREND_HOLD",))

    def _wait(self, context: StrategyContext, state: StrategyState, setup,
              confirmation: int, reasons: tuple[str, ...]) -> StrategyProposal:
        return observation_wait(context, state, setup, confirmation, reasons)
