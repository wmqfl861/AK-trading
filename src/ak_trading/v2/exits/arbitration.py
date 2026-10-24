"""Exit arbitration: one executable target, merged observation facts (D3).

This is the SINGLE authoritative M3C arbitrator (D3 implementation order
3): ``registry.arbitrate_exit`` delegates here so no two divergent
arbitrators can coexist. Pure — no resource side effects; M4 merges the
result with its hard-risk channel, the original M3B management and any
still-owed selected targets before the management gates.

Ordering (D3 §take-min, 08 §5.3/§7.1):

1. Any legal ``EXIT`` candidate wins outright with target 0 — hard EXITs
   are never blocked by soft signals and multiple EXIT candidates all
   share target 0 (nothing is summed).
2. Otherwise the ``REDUCE`` candidate with the SMALLEST absolute
   remaining target wins — the most aggressive legal reduction; shorts
   compare absolute values, never signed min. Two 50% rules never add up
   to 75% and are never averaged.
3. Otherwise the first ``KEEP`` in binding priority order. KEEP never
   cancels an owed exit obligation.

Ties (equal targets) resolve by ``ExitPolicyBinding.priority`` order —
there is deliberately NO runtime family fallback: the priority is part of
the binding identity and must cover every triggerable policy.

Observation-fact merging (D1 order 2 / D3): the returned winner carries a
next_state that merges EVERY candidate's legal observation facts —
forward peaks with their sources, the shared protection line as the
side-aware tightest candidate, per-(policy, rule) confirmation counters
and ladder trigger latches. Copying only the winner's next_state would
silently drop the losers' peaks/lines; evaluating policies sequentially
against each other's candidates would double-advance observations. All
candidates descend from the same committed base, so the merge is a pure
fold over their deltas.
"""
from __future__ import annotations

import dataclasses
from typing import Iterable, Mapping, Sequence

from ak_trading.v2.contracts import (
    ExitConfirmation, ExitContext, ExitProposal, ExitState,
)
from ak_trading.v2.exits.state import advance_state, base_state_for

_ACTION_RANK: Mapping[str, int] = {"EXIT": 0, "REDUCE": 1, "KEEP": 2}


class ArbitrationError(ValueError):
    """Raised on empty candidate sets or out-of-priority triggers."""


def _priority_position(context: ExitContext, policy_id: str | None) -> int:
    priority = tuple(context.exit_binding.priority)
    if policy_id is None:
        return len(priority)
    if policy_id not in priority:
        # D3: the binding priority must cover every triggerable policy id.
        raise ArbitrationError(
            f"arbitration_priority_missing_policy:{policy_id}")
    return priority.index(policy_id)


def merge_observation_facts(context: ExitContext,
                            proposals: Sequence[ExitProposal]) -> tuple[dict, ExitState]:
    """Fold every candidate's observation deltas over their common base.

    Returns ``(changes, base)`` — the merged field changes relative to the
    common base and the base itself, ready for a single
    :func:`state.advance_state` under the winner's proposal id. The base
    is re-derived deterministically (the same
    :func:`state.base_state_for` every policy evaluated against), so the
    merged state and every individual candidate share one ancestor; the
    merge never treats one candidate as another's committed state.
    """
    if not proposals:
        raise ArbitrationError("merge_requires_candidates")
    base = base_state_for(context, context.previous_exit_state)
    side = context.budget.side

    def _best_price_peak(current: tuple, candidate: ExitState) -> tuple:
        value, nano, mark = current
        if candidate.price_peak is None:
            return current
        if value is None:
            return (candidate.price_peak, candidate.price_peak_source_nano,
                    candidate.price_peak_source_hash)
        better = (candidate.price_peak > value if side == 1
                  else candidate.price_peak < value)
        if better:
            return (candidate.price_peak, candidate.price_peak_source_nano,
                    candidate.price_peak_source_hash)
        return current

    def _best_net_peak(current: tuple, candidate: ExitState) -> tuple:
        value, nano, mark = current
        if candidate.net_peak_r is None:
            return current
        if value is None or candidate.net_peak_r > value:
            return (candidate.net_peak_r, candidate.net_peak_source_nano,
                candidate.net_peak_source_hash)
        return current

    def _best_line(current: tuple, candidate: ExitState) -> tuple:
        price, version, effective, armed = current
        if not candidate.armed or candidate.protection_stop_price is None:
            return current
        value = candidate.protection_stop_price
        if not armed or price is None:
            return (value, candidate.protection_line_version,
                    candidate.protection_effective_from_nano, True)
        tighter = (value > price if side == 1 else value < price)
        if tighter:
            return (value, candidate.protection_line_version,
                    candidate.protection_effective_from_nano, True)
        return current

    price_peak: tuple = (base.price_peak, base.price_peak_source_nano,
                         base.price_peak_source_hash)
    net_peak: tuple = (base.net_peak_r, base.net_peak_source_nano,
                       base.net_peak_source_hash)
    line: tuple = (base.protection_stop_price, base.protection_line_version,
                   base.protection_effective_from_nano, base.armed)
    confirmations: dict[tuple[str, str], ExitConfirmation] = {
        (item.exit_policy_id, item.rule_id): item
        for item in base.confirmations}
    ladder: dict[int, object] = {step.level_index: step
                                 for step in base.ladder_progress}

    for proposal in proposals:
        state = proposal.next_state
        price_peak = _best_price_peak(price_peak, state)
        net_peak = _best_net_peak(net_peak, state)
        line = _best_line(line, state)
        for item in state.confirmations:
            key = (item.exit_policy_id, item.rule_id)
            existing = confirmations.get(key)
            if existing is None or item.count >= existing.count:
                confirmations[key] = item
        for step in state.ladder_progress:
            previous = ladder.get(step.level_index)
            if previous is None:
                ladder[step.level_index] = step
            elif step.triggered and not previous.triggered:
                ladder[step.level_index] = dataclasses.replace(
                    previous, triggered=True)

    changes: dict[str, object] = {
        "price_peak": price_peak[0],
        "price_peak_source_nano": price_peak[1],
        "price_peak_source_hash": price_peak[2],
        "net_peak_r": net_peak[0],
        "net_peak_source_nano": net_peak[1],
        "net_peak_source_hash": net_peak[2],
    }
    if line[3] and line[0] is not None:
        changes.update({"armed": True,
                        "protection_stop_price": line[0],
                        "protection_line_version": line[1],
                        "protection_effective_from_nano": line[2]})
    if confirmations:
        changes["confirmations"] = tuple(sorted(
            confirmations.values(),
            key=lambda item: (item.exit_policy_id, item.rule_id)))
    if ladder:
        changes["ladder_progress"] = tuple(
            ladder[index] for index in sorted(ladder))
    return changes, base


def arbitrate_exit(proposals: tuple[ExitProposal, ...],
                   context: ExitContext) -> ExitProposal:
    """Fold the candidates into ONE proposal under D3 take-min ordering."""
    if not proposals:
        raise ArbitrationError("arbitration_requires_candidates")

    def rank(proposal: ExitProposal) -> tuple[int, int, int]:
        return (_ACTION_RANK[proposal.action],
                abs(proposal.target_lots),
                _priority_position(context, proposal.triggering_policy_id))

    winner = min(proposals, key=rank)
    changes, base = merge_observation_facts(context, proposals)
    merged = advance_state(base, winner.proposal_id, **changes) if changes \
        else base
    return dataclasses.replace(winner, next_state=merged)
