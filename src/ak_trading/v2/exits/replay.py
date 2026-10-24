"""Logic-only exit replay (08-M3C §3 P1 file nine; P1 review finding #2).

Replays a context sequence through the pure M3C pipeline — feature
snapshot, per-policy candidates, D3 arbitration, observation commit — and
produces the ``ak_probability_exit_logical_replay_v1.0.0`` envelope.
``financial_result`` is fixed ``null``: a logical replay is engineering
evidence about the decision chain, NEVER a financial NAV, a backtest
result or a shadow-run substitute (08 §1/§12).

No resources are touched: no orders, no ledger writes, no reservation
changes; the observation commit inside the loop advances only the pure
candidate chain. Contexts must share one scope and binding; the same
inputs always produce byte-identical canonical output.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from ak_trading.v2 import CONTRACT_VERSION
from ak_trading.v2.artifacts import canonical_json_bytes
from ak_trading.v2.contracts import (
    ExitContext, ExitFeatureSnapshot, ExitProposal, ExitState,
)
from ak_trading.v2.exits.arbitration import arbitrate_exit
from ak_trading.v2.exits.features import (
    build_exit_feature_snapshot, exit_jsonable,
)
from ak_trading.v2.exits.registry import evaluate_exit
from ak_trading.v2.exits.state import commit_exit_observation

LOGICAL_EXIT_REPLAY_SCHEMA_VERSION = "ak_probability_exit_logical_replay_v1.0.0"


class ReplayError(ValueError):
    """Raised when a context sequence is not a legal single-scope replay."""


@dataclass(frozen=True)
class LogicalExitReplay:
    """The full logic-replay record; financial_result is always None."""
    schema_version: str
    contract_version: str
    binding_sha256: str
    initial_state: ExitState | None
    snapshots: tuple[ExitFeatureSnapshot, ...]
    proposals: tuple[ExitProposal, ...]
    final_state: ExitState
    financial_result: None


def replay_exit_policy(
        contexts: Sequence[ExitContext],
        previous: ExitState | None,
        evaluate: Callable[[ExitContext, ExitState | None],
                           tuple[ExitProposal, ...]] = evaluate_exit,
        arbitrate: Callable[[tuple[ExitProposal, ...], ExitContext],
                            ExitProposal] = arbitrate_exit,
        commit: Callable[[ExitState, ExitProposal], ExitState]
        = commit_exit_observation) -> LogicalExitReplay:
    """Replay one scope's context sequence through the pure exit pipeline."""
    if not contexts:
        raise ReplayError("replay_requires_at_least_one_context")
    scope = (contexts[0].account_id, contexts[0].contract_id,
             contexts[0].strategy_id, contexts[0].episode_id,
             contexts[0].exit_binding.binding_sha256)
    for context in contexts:
        current = (context.account_id, context.contract_id,
                   context.strategy_id, context.episode_id,
                   context.exit_binding.binding_sha256)
        if current != scope:
            raise ReplayError("replay_contexts_must_share_one_scope_and_binding")
    initial = previous if previous is not None else \
        contexts[0].previous_exit_state
    state = initial
    snapshots: list[ExitFeatureSnapshot] = []
    proposals: list[ExitProposal] = []
    for context in contexts:
        snapshots.append(build_exit_feature_snapshot(context, state))
        candidates = evaluate(context, state)
        winner = arbitrate(candidates, context)
        proposals.append(winner)
        state = commit(state, winner)
    return LogicalExitReplay(
        schema_version=LOGICAL_EXIT_REPLAY_SCHEMA_VERSION,
        contract_version=CONTRACT_VERSION,
        binding_sha256=scope[4],
        initial_state=initial,
        snapshots=tuple(snapshots),
        proposals=tuple(proposals),
        final_state=state,
        financial_result=None)


def logical_exit_replay_bytes(replay: LogicalExitReplay) -> bytes:
    """Canonical JSON envelope (schema_version, contract_version, ...)."""
    payload = {
        "schema_version": replay.schema_version,
        "contract_version": replay.contract_version,
        "binding_sha256": replay.binding_sha256,
        "initial_state": None if replay.initial_state is None
        else exit_jsonable(_state_projection(replay.initial_state)),
        "snapshots": [
            {"snapshot_id": snapshot.snapshot_id,
             "observed_at_nano": snapshot.observed_at_nano,
             "available_at_nano": snapshot.available_at_nano,
             "values": [[value.feature_key,
                         str(value.value) if hasattr(value.value, "is_finite")
                         else value.value,
                         value.quality.status] for value in snapshot.values]}
            for snapshot in replay.snapshots],
        "proposals": [_proposal_projection(proposal)
                      for proposal in replay.proposals],
        "final_state": exit_jsonable(_state_projection(replay.final_state)),
        "financial_result": None,
    }
    return canonical_json_bytes(payload)


def _state_projection(state: ExitState) -> dict:
    return {
        "state_prefix_hash": state.state_prefix_hash,
        "phase": state.phase,
        "armed": state.armed,
        "protection_stop_price": state.protection_stop_price,
        "protection_line_version": state.protection_line_version,
        "net_peak_r": state.net_peak_r,
        "price_peak": state.price_peak,
        "qbase": state.qbase,
        "closed_lots": state.closed_lots,
        "no_more_add": state.no_more_add,
        "ladder_progress": [[step.level_index, step.triggered,
                             step.requested_lots, step.completed_lots]
                            for step in state.ladder_progress],
    }


def _proposal_projection(proposal: ExitProposal) -> dict:
    return {
        "proposal_id": proposal.proposal_id,
        "action": proposal.action,
        "target_lots": proposal.target_lots,
        "triggering_policy_id": proposal.triggering_policy_id,
        "observed_at_nano": proposal.observed_at_nano,
        "earliest_execution_nano": proposal.earliest_execution_nano,
        "reasons": list(proposal.reasons),
        "next_state_prefix_hash": proposal.next_state.state_prefix_hash,
    }
