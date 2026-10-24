"""Flat / wait family (04-M3B-策略库.md §6.7).

Always answers: ``action=WAIT`` with ``target_lots=0`` and concrete unmet
conditions. It never tampers with an episode owned by another strategy and
never claims an existing position is flat: when the account holds lots the
reasons say so, the owning strategy's EXIT flows through the unified
position-management channel and M4 executes it. ``cancel_new_risk`` is the
family's registered behaviour — no new risk is ever proposed. NO_TRADE
means "no new risk"; the account is only flat when actual lots are zero
and nothing is pending.
"""
from __future__ import annotations

from ak_trading.v2.contracts import (
    StrategyContext,
    StrategyProposal,
    StrategySpec,
    StrategyState,
)
from ak_trading.v2.strategies.registry import (
    fresh_state,
    next_open_nano,
    observation_span_nano,
)
from ak_trading.v2.artifacts import stable_id


class WaitStrategy:
    """Waiting strategy; never proposes new risk."""

    def __init__(self, spec: StrategySpec) -> None:
        self._spec = spec

    @property
    def spec(self) -> StrategySpec:
        return self._spec

    def evaluate(self, context: StrategyContext,
                 previous: StrategyState | None) -> StrategyProposal:
        spec = self._spec
        state = previous if previous is not None else fresh_state(
            spec.strategy_id, context.features.contract_id)
        reasons: list[str] = ["NO_NEW_RISK"]
        position = context.position
        if position is not None and position.lots > 0:
            if position.strategy_id != spec.strategy_id:
                reasons.append("POSITION_MANAGED_BY_OWNER_STRATEGY")
            else:
                reasons.append("OWN_POSITION_PENDING_MANAGEMENT")
            if position.pending_exit:
                reasons.append("EXIT_PENDING")
        if context.account.killed:
            reasons.append("ACCOUNT_KILLED")
        from ak_trading.v2.contracts import StrategyState as _State
        nxt = _State(
            strategy_id=spec.strategy_id, episode_id=None,
            contract_id=state.contract_id, phase="OBSERVE", setup=None,
            last_regime=context.regime, direction=0, filled_layers=(),
            layer_filled_lots=(), pending_layer_index=None, active_exit_nano=None,
            pending_exit_nano=None, pending_intent_ids=(), budget=None,
            confirmation_count=0, cooldown_until_nano=state.cooldown_until_nano,
            last_event_id=state.last_event_id)
        earliest = next_open_nano(context)
        return StrategyProposal(
            proposal_id=stable_id(
                "proposal",
                f"{spec.strategy_id}|{state.contract_id}|{context.now_nano}|WAIT|0"),
            strategy_id=spec.strategy_id, episode_id=None,
            contract_id=state.contract_id, decision_nano=context.now_nano,
            action="WAIT", target_lots=0, layer_index=None,
            trigger_price=None, target_price=None, stop_price=None,
            earliest_execution_nano=earliest,
            valid_until_nano=earliest + observation_span_nano(spec.mode),
            forecast_id=None, assessment_id=context.regime.assessment_id,
            budget=None, payoff=None, next_state=nxt,
            reasons=tuple(dict.fromkeys(reasons)),
        )
