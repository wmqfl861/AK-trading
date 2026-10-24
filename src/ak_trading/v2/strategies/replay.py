"""Independent logical replay (04-M3B-策略库.md §3, §8).

Replays a frozen context sequence through one strategy with the M3B state
machine (``commit_observation`` every bar, ``commit_selected_proposal`` for
the logically selected candidate, ``apply_execution`` for injected feedback)
and produces proposals only. It never computes net PnL, NAV or win rates —
``financial_result`` is structurally ``None``; economic replay belongs to
the M4 fills-and-ledger implementation.
"""
from __future__ import annotations

from typing import Mapping

from ak_trading.v2.artifacts import stable_id
from ak_trading.v2.contracts import (
    ExecutionResult,
    OrderIntent,
    StrategyContext,
    StrategyProposal,
    StrategyState,
)
from ak_trading.v2.schemas import encode_record
from ak_trading.v2.strategies.registry import LOGICAL_REPLAY_SCHEMA_VERSION, Strategy
from ak_trading.v2.strategies.state import (
    apply_execution,
    commit_observation,
    commit_selected_proposal,
)

_SELECTABLE_ACTIONS = frozenset({"OPEN", "ADD", "REDUCE", "EXIT"})


def replay_intent_id(strategy_id: str, decision_nano: int, action: str) -> str:
    """Deterministic synthetic intent id used to key replay feedback."""
    return stable_id("intent", f"replay:{strategy_id}:{decision_nano}:{action}")


def _synthetic_intent(strategy_id: str, proposal: StrategyProposal,
                      context: StrategyContext) -> OrderIntent:
    if proposal.action == "EXIT":
        purpose = "STOP"
        offset = "CLOSE"
        held = 0
        if context.position is not None and context.position.strategy_id == strategy_id:
            held = context.position.lots
        delta = -held if held > 0 else -1
    elif proposal.action == "REDUCE":
        purpose, offset = "REDUCE", "CLOSE"
        delta = -1
    else:
        purpose = "ENTRY" if proposal.action == "OPEN" else "ADD"
        offset = "OPEN"
        side = 0
        if proposal.budget is not None:
            side = proposal.budget.side
        elif proposal.next_state.direction != 0:
            side = proposal.next_state.direction
        lots = 1
        if proposal.budget is not None and proposal.layer_index is not None:
            lots = proposal.budget.layers[proposal.layer_index].lots
        delta = side * lots if side != 0 else lots
    return OrderIntent(
        intent_id=replay_intent_id(strategy_id, proposal.decision_nano, proposal.action),
        decision_id=f"replay:{strategy_id}",
        strategy_id=strategy_id,
        episode_id=proposal.episode_id or "",
        contract_id=proposal.contract_id,
        created_at_nano=proposal.decision_nano,
        not_before_nano=proposal.earliest_execution_nano,
        expires_at_nano=max(proposal.valid_until_nano, proposal.earliest_execution_nano + 1),
        delta_lots=delta,
        offset=offset,
        purpose=purpose,
        layer_index=proposal.layer_index,
        limit_price=proposal.trigger_price,
    )


def replay_strategy(strategy: Strategy, contexts: tuple[StrategyContext, ...],
                    feedback: Mapping[str, ExecutionResult],
                    initial_state: StrategyState | None) -> tuple[StrategyProposal, ...]:
    """Sequential logical replay; appending future contexts never changes the
    proposals of an earlier frozen prefix."""
    state = initial_state
    proposals: list[StrategyProposal] = []
    strategy_id = strategy.spec.strategy_id
    for context in contexts:
        proposal = strategy.evaluate(context, state)
        proposals.append(proposal)
        state = commit_observation(state, proposal)
        if proposal.action in _SELECTABLE_ACTIONS and state is not None:
            intent = _synthetic_intent(strategy_id, proposal, context)
            try:
                state = commit_selected_proposal(state, proposal, (intent,))
            except ValueError:
                state = state
            result = feedback.get(intent.intent_id)
            if result is not None:
                state = apply_execution(state, result, context.account, context.now_nano)
    return tuple(proposals)


def logical_replay_payload(strategy_id: str, initial_state: StrategyState | None,
                           proposals: tuple[StrategyProposal, ...],
                           final_state: StrategyState | None) -> dict:
    """§8 ``logical_replay.json`` root object; financial result is None."""
    return {
        "schema_version": LOGICAL_REPLAY_SCHEMA_VERSION,
        "strategy_id": strategy_id,
        "initial_state": None if initial_state is None else encode_record(initial_state),
        "proposals": [encode_record(proposal) for proposal in proposals],
        "final_state": None if final_state is None else encode_record(final_state),
        "financial_result": None,
    }
