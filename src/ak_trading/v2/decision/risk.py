"""Gross portfolio commitments are filtered before any selection policy."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Mapping

from ak_trading.v2.contracts import (
    AccountSnapshot, GateDecision, Instrument, JsonValue, OrderIntent,
    StrategyProposal, StrategyState, TradingRule, ValuationMark,
)
from ak_trading.v2.decision.execution import rule_at, simulate_stress_paths

ZERO = Decimal("0")


def enforce_account_risk(proposals: tuple[StrategyProposal, ...],
                         gates: tuple[GateDecision, ...], account: AccountSnapshot,
                         instruments: tuple[Instrument, ...], states: tuple[StrategyState, ...],
                         pending_intents: tuple[OrderIntent, ...], rules: tuple[TradingRule, ...],
                         capacity_remaining: Mapping[str, int],
                         policy: Mapping[str, JsonValue], *,
                         valuations: tuple[ValuationMark, ...]) -> tuple[GateDecision, ...]:
    """Compare alternatives independently against the same committed account.

    The replay host reserves the chosen episode before calling this function
    for the next contract. Pending intentions already covered by a whole-round
    budget never add that budget a second time.
    """
    by_id = {g.proposal_id: g for g in gates}
    if len(by_id) != len(gates) or set(by_id) != {p.proposal_id for p in proposals}:
        raise ValueError("gate_proposal_join_mismatch")
    instruments_by_id = {i.contract_id: i for i in instruments}
    def ratio(name: str) -> Decimal:
        value = policy[name]
        if not isinstance(value, str):
            raise ValueError("risk_ratio_requires_decimal_string")
        return Decimal(value)
    episodes = {s.episode_id: s for s in states if s.episode_id is not None}
    if len(episodes) != sum(s.episode_id is not None for s in states):
        raise ValueError("duplicate_episode")
    unknown = any(i.episode_id not in episodes for i in pending_intents)
    unknown |= any(l.episode_id not in episodes for l in account.lots)
    marks = {m.contract_id:m for m in valuations}
    if len(marks)!=len(valuations) or set(marks)!={l.contract_id for l in account.lots}:
        raise ValueError("valuation_position_set_mismatch")
    rule_ids={}
    for rule in rules:
        if rule.rule_id in rule_ids and rule_ids[rule.rule_id]!=rule:
            raise ValueError("conflicting_rule_identity")
        rule_ids[rule.rule_id]=rule
    valuation_reasons=set()
    actual_by_episode={}
    unrealized=ZERO
    for lot in account.lots:
        mark=marks[lot.contract_id]
        if mark.status!="CURRENT":
            valuation_reasons.update(("DATA_STALE",*mark.reasons))
        if mark.status=="UNVALUED":
            valuation_reasons.add("COST_UNKNOWN")
            continue
        rule=rule_ids.get(mark.rule_id)
        if (rule is None or rule.contract_id!=lot.contract_id or mark.valued_at_nano>account.as_of_nano
                or mark.as_of_nano>mark.valued_at_nano):
            raise ValueError("valuation_rule_or_time_mismatch")
        rate=rule.long_margin_rate if lot.side==1 else rule.short_margin_rate
        if rate is None or rule.evidence=="UNKNOWN" or not (
                rule.available_at_nano<=mark.valued_at_nano and
                rule.effective_from_nano<=mark.valued_at_nano<rule.effective_until_nano):
            raise ValueError("valuation_rule_unusable")
        if mark.status=="CURRENT" and not (rule.available_at_nano<=account.as_of_nano
                and rule.effective_from_nano<=account.as_of_nano<rule.effective_until_nano):
            valuation_reasons.add("COST_UNKNOWN")
        state=episodes.get(lot.episode_id)
        if state is None or state.strategy_id!=lot.strategy_id or state.contract_id!=lot.contract_id:
            raise ValueError("lot_state_owner_mismatch")
        if mark.status=="CURRENT" and account.as_of_nano-mark.as_of_nano>120_000_000_000:
            valuation_reasons.add("DATA_STALE")
        amount=mark.price*lot.lots*rule.multiplier*rate
        actual_by_episode[lot.episode_id]=actual_by_episode.get(lot.episode_id,ZERO)+amount
        unrealized+=lot.side*lot.lots*(mark.price-lot.entry_price)*rule.multiplier
    if sum(actual_by_episode.values(),ZERO)!=account.margin_cny or unrealized!=account.unrealized_cny:
        raise ValueError("valuation_account_reconciliation_mismatch")
    committed: dict[str, tuple[str, str, Decimal]] = {}
    for episode, state in episodes.items():
        if state.budget is None:
            unknown = True
            continue
        actual=actual_by_episode.get(episode,ZERO)
        future=(state.phase not in ("EXITING","PAUSED") and
                (sum(state.layer_filled_lots)<state.budget.maximum_lots or state.pending_intent_ids))
        amount=max(actual,state.budget.reserved_margin_cny) if future else actual
        committed[episode]=(state.strategy_id,state.contract_id,amount)
    result = []
    for proposal in proposals:
        gate = by_id[proposal.proposal_id]
        reasons = list(gate.reasons)
        if proposal.action not in ("OPEN", "ADD"):
            result.append(gate)
            continue
        reasons.extend(valuation_reasons)
        if unknown:
            reasons.append("MARGIN_LIMIT")
        if account.killed:
            reasons.extend(("DAY_LOSS_LIMIT", "DRAWDOWN_LIMIT"))
        if account.day_net_cny <= -account.day_start_equity_cny * ratio("maximum_day_loss_ratio"):
            reasons.append("DAY_LOSS_LIMIT")
        if account.equity_cny <= account.peak_equity_cny * (Decimal(1)-ratio("maximum_drawdown_ratio")):
            reasons.append("DRAWDOWN_LIMIT")
        if proposal.contract_id not in capacity_remaining:
            reasons.append("CAPACITY_UNKNOWN")
        elif capacity_remaining[proposal.contract_id] <= 0:
            reasons.append("CAPACITY_EXHAUSTED")
        rule = rule_at(rules, proposal.contract_id, proposal.decision_nano)
        if rule is None or rule.evidence == "UNKNOWN":
            reasons.append("COST_UNKNOWN")
        elif proposal.contract_id in capacity_remaining and capacity_remaining[proposal.contract_id] < rule.min_open_lots:
            reasons.append("CAPACITY_EXHAUSTED")
        budget = proposal.budget
        if budget is None:
            reasons.append("ROUND_BUDGET_EXCEEDED")
        else:
            old = episodes.get(budget.episode_id)
            if old is not None and old.budget != budget:
                reasons.append("ROUND_BUDGET_EXCEEDED")
            if (budget.planned_loss_cny > budget.equity_at_start_cny * ratio("maximum_round_loss_ratio")
                    or budget.maximum_lots > policy["maximum_round_lots"]
                    or budget.frozen_at_nano > proposal.decision_nano):
                reasons.append("ROUND_BUDGET_EXCEEDED")
            if budget.stress_loss_cny > budget.equity_at_start_cny * ratio("maximum_stress_loss_to_equity"):
                reasons.append("TAIL_LOSS_LIMIT")
            if rule is not None:
                try:
                    stress = simulate_stress_paths(budget, rule, ())
                    if max(s.loss_cny for s in stress) > budget.stress_loss_cny:
                        reasons.append("ROUND_BUDGET_EXCEEDED")
                    if max(s.margin_shortfall_cny for s in stress) > 0:
                        reasons.append("MARGIN_LIMIT")
                except ValueError:
                    reasons.append("COST_UNKNOWN")
            candidate = dict(committed)
            candidate[budget.episode_id] = (proposal.strategy_id, proposal.contract_id,
                max(budget.reserved_margin_cny,actual_by_episode.get(budget.episode_id,ZERO)))
            gross = sum((v[2] for v in candidate.values()), ZERO)
            incremental = ZERO if budget.episode_id in committed else budget.reserved_margin_cny
            gross = max(gross, account.margin_cny + account.reserved_margin_cny + incremental)
            equity = account.equity_cny
            total_planned=sum((s.budget.planned_loss_cny for episode,s in episodes.items()
                               if s.budget is not None and episode!=budget.episode_id),ZERO)+budget.planned_loss_cny
            if total_planned-account.day_net_cny>account.day_start_equity_cny*ratio("maximum_day_loss_ratio"):
                reasons.append("DAY_LOSS_LIMIT")
            if equity-budget.stress_loss_cny-gross<0:
                reasons.append("MARGIN_LIMIT")
            if gross > equity * ratio("maximum_margin_ratio") or gross > equity:
                reasons.append("MARGIN_LIMIT")
            strategy_sums: dict[str, Decimal] = {}
            product_sums: dict[str, Decimal] = {}
            group_sums: dict[str, Decimal] = {}
            for strategy, contract, amount in candidate.values():
                instrument = instruments_by_id.get(contract)
                if instrument is None:
                    reasons.append("CONCENTRATION_LIMIT")
                    continue
                strategy_sums[strategy] = strategy_sums.get(strategy, ZERO) + amount
                product_sums[instrument.product] = product_sums.get(instrument.product, ZERO) + amount
                for group in instrument.risk_groups:
                    group_sums[group] = group_sums.get(group, ZERO) + amount
            if (any(v > equity * ratio("maximum_strategy_margin_ratio") for v in strategy_sums.values())
                    or any(v > equity * ratio("maximum_product_margin_ratio") for v in product_sums.values())
                    or any(v > equity * ratio("maximum_group_margin_ratio") for v in group_sums.values())):
                reasons.append("CONCENTRATION_LIMIT")
        added = set(reasons) - set(gate.reasons)
        result.append(replace(gate, eligible=gate.eligible and not added,
                              reasons=tuple(sorted(set(reasons)))))
    return tuple(result)
