"""PIT capacity and explicitly hypothetical OHLC execution; never an order API."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, ROUND_FLOOR
from statistics import median
from typing import Mapping

from ak_trading.v2.artifacts import canonical_json_bytes, stable_id
from ak_trading.v2.contracts import (
    AccountSnapshot, ExecutionResult, FeeLeg, Fill, MarketBar, OrderIntent,
    PositionSnapshot, RoundBudget, SessionWindow, TradingRule,
)

ZERO = Decimal("0")


def fee_amount(leg: FeeLeg | None, price: Decimal, lots: int,
               multiplier: Decimal) -> Decimal:
    if leg is None:
        raise ValueError("COST_UNKNOWN")
    if type(lots) is not int or lots <= 0:
        raise ValueError("positive_integer_lots_required")
    return lots * (leg.per_lot_cny + price * multiplier * leg.notional_rate)


def rule_at(rules: tuple[TradingRule, ...], contract_id: str,
            as_of_nano: int) -> TradingRule | None:
    matched = [r for r in rules if r.contract_id == contract_id
               and r.available_at_nano <= as_of_nano
               and r.effective_from_nano <= as_of_nano < r.effective_until_nano]
    if len(matched) > 1:
        raise ValueError("overlapping_visible_rules")
    return matched[0] if matched else None


def capacity_before(bars: tuple[MarketBar, ...], contract_id: str,
                    duration_seconds: int, as_of_nano: int,
                    rule: TradingRule | None) -> int | None:
    """Twenty complete observations of this frequency; current volume is never read."""
    if rule is None or rule.contract_id != contract_id or rule.evidence == "UNKNOWN":
        return None
    visible = sorted((b for b in bars if b.contract_id == contract_id
                      and b.duration_seconds == duration_seconds and b.complete
                      and b.end_nano <= as_of_nano and b.available_at_nano is not None
                      and b.available_at_nano <= as_of_nano),
                     key=lambda b: (b.end_nano, b.bar_id))[-20:]
    if len(visible) < 20 or len({b.start_nano for b in visible}) != 20:
        return None
    if any(b.volume is None or b.quality.status not in ("READY", "DEGRADED") for b in visible):
        return None
    return int((median(b.volume for b in visible) * rule.participation_rate)
               .to_integral_value(rounding=ROUND_FLOOR))


def position_for(account: AccountSnapshot, contract_id: str) -> PositionSnapshot | None:
    lots = [p for p in account.lots if p.contract_id == contract_id]
    if not lots:
        return None
    owners = {(p.strategy_id, p.episode_id, p.side) for p in lots}
    if len(owners) != 1:
        raise ValueError("multiple_owners_or_directions_for_contract")
    strategy, episode, side = next(iter(owners))
    quantity = sum(p.lots for p in lots)
    return PositionSnapshot(contract_id, strategy, episode, side * quantity,
                            sum(p.entry_price * p.lots for p in lots) / quantity, False)


def simulate_execution(intent: OrderIntent, execution_bar: MarketBar,
                       rule: TradingRule, capacity_remaining: int,
                       position: PositionSnapshot | None) -> ExecutionResult:
    """Fill at the next legal opening, using capacity fixed before that opening.

    The caller splits today/yesterday closes from actual lots. The intent's
    entry limit is the frozen worst permitted execution price, including slip.
    No H/L/C or final volume decides an opening fill or its size.
    """
    if type(capacity_remaining) is not int or capacity_remaining < 0:
        raise ValueError("capacity_must_be_nonnegative_integer")
    side = 1 if intent.delta_lots > 0 else -1
    when = execution_bar.start_nano
    reasons: list[str] = []
    status = "BLOCKED"
    if intent.contract_id != execution_bar.contract_id or intent.contract_id != rule.contract_id:
        raise ValueError("execution_contract_mismatch")
    if when >= intent.expires_at_nano:
        status = "EXPIRED"
        reasons.append("INTENT_EXPIRED")
    if when <= intent.created_at_nano or when < intent.not_before_nano:
        reasons.append("EXECUTION_NOT_YET_LEGAL")
    if not (rule.available_at_nano <= when
            and rule.effective_from_nano <= when < rule.effective_until_nano):
        reasons.append("COST_UNKNOWN")
    if rule.evidence == "UNKNOWN":
        reasons.append("COST_UNKNOWN")
    if execution_bar.trading_day is None or execution_bar.session_id is None:
        reasons.append("CLOCK_UNKNOWN")
    if execution_bar.open is None or execution_bar.open <= 0:
        reasons.append("DATA_INVALID")
    if execution_bar.quality.status in ("INVALID", "MISSING", "STALE"):
        reasons.append("DATA_INVALID")
    leg = {"OPEN": rule.open_fee, "CLOSE": rule.close_fee,
           "CLOSE_TODAY": rule.close_today_fee}[intent.offset]
    if leg is None:
        reasons.append("COST_UNKNOWN")
    if intent.offset == "OPEN":
        if (rule.long_margin_rate if side == 1 else rule.short_margin_rate) is None:
            reasons.append("COST_UNKNOWN")
        if position is not None and (position.strategy_id != intent.strategy_id
                or position.episode_id != intent.episode_id or position.lots * side <= 0):
            reasons.append("POSITION_OWNERSHIP_MISMATCH")
    elif position is None or position.strategy_id != intent.strategy_id \
            or position.episode_id != intent.episode_id or position.lots * side >= 0:
        reasons.append("POSITION_OWNERSHIP_MISMATCH")
    elif abs(intent.delta_lots) > abs(position.lots):
        reasons.append("CLOSE_EXCEEDS_POSITION")
    reference = execution_bar.open
    price = None if reference is None else reference + side * rule.price_tick * (
        rule.slippage_ticks + rule.impact_ticks)
    if price is not None:
        if price <= 0 or price % rule.price_tick != 0:
            reasons.append("PRICE_TICK_INVALID")
        if rule.limit_up is None or rule.limit_down is None:
            reasons.append("LIMIT_RULE_UNKNOWN")
        else:
            if price > rule.limit_up or price < rule.limit_down:
                reasons.append("PRICE_OUTSIDE_LIMIT")
            if (side == 1 and reference >= rule.limit_up) or (side == -1 and reference <= rule.limit_down):
                reasons.append("LIMIT_LOCKED")
        if intent.limit_price is not None and side * (price - intent.limit_price) > 0:
            reasons.append("ENTRY_GAP_EXCEEDED" if intent.offset == "OPEN" else "LIMIT_PRICE_NOT_MET")
    if abs(intent.delta_lots) % rule.lot_step:
        reasons.append("LOT_STEP_INVALID")
    quantity = min(abs(intent.delta_lots), capacity_remaining)
    quantity -= quantity % rule.lot_step
    if intent.offset == "OPEN" and quantity < rule.min_open_lots:
        quantity = 0
    if quantity == 0:
        reasons.append("CAPACITY_EXHAUSTED")
    if reasons:
        if intent.offset != "OPEN":
            reasons.append("EXIT_PENDING")
        return ExecutionResult(intent.intent_id, (), intent.delta_lots, status,
                               tuple(sorted(set(reasons))))
    payload = [intent.intent_id, execution_bar.bar_id, when, str(price), quantity,
               intent.offset, rule.rule_id]
    fill = Fill(stable_id("fill", canonical_json_bytes(payload)), intent.intent_id,
                intent.contract_id, intent.strategy_id, intent.episode_id, when,
                execution_bar.trading_day, side, quantity, intent.offset, reference,
                price, fee_amount(leg, price, quantity, rule.multiplier),
                abs(price - reference) * quantity * rule.multiplier, rule.rule_id, "SCENARIO")
    remaining = intent.delta_lots - side * quantity
    return ExecutionResult(intent.intent_id, (fill,), remaining,
                           "PARTIAL" if remaining else "FILLED",
                           ("SCENARIO", "EXIT_PENDING") if remaining and intent.offset != "OPEN" else ("SCENARIO",))


@dataclass(frozen=True)
class StressPathResult:
    scenario: str
    trading_days: tuple[date, ...]
    reference_prices: tuple[Decimal, ...]
    entry_prices: tuple[Decimal, ...]
    exit_price: Decimal
    gross_loss_cny: Decimal
    fees_cny: Decimal
    loss_cny: Decimal
    peak_margin_cny: Decimal
    minimum_available_cny: Decimal
    margin_shortfall_cny: Decimal
    evidence: str


def simulate_stress_paths(budget: RoundBudget, rule: TradingRule,
                          sessions: tuple[SessionWindow, ...]) -> tuple[StressPathResult, ...]:
    """Independent finite STOP/GAP/NO_STOP cash paths, never edits market bars.

    Empty trading_days explicitly means calendar-unverified SCENARIO. When a
    calendar is supplied, NO_STOP consumes three real trading dates and exits
    on the fourth; lunch, midnight and weekends do not count as extra days.
    """
    if rule.contract_id != budget.contract_id or rule.evidence == "UNKNOWN":
        raise ValueError("COST_UNKNOWN")
    if any(leg is None for leg in (rule.open_fee, rule.close_fee, rule.close_today_fee)):
        raise ValueError("COST_UNKNOWN")
    policy = budget.stress_policy
    side, atr = budget.side, budget.initial_atr_points
    slip = rule.price_tick * (rule.slippage_ticks + rule.impact_ticks)
    entries = tuple(l.reference_entry_price + side * (Decimal("0.1") * atr + slip)
                    for l in budget.layers)
    if any(p <= 0 for p in entries):
        raise ValueError("stress_entry_price_nonpositive")
    rate = rule.long_margin_rate if side == 1 else rule.short_margin_rate
    if rate is None:
        raise ValueError("COST_UNKNOWN")
    rate = min(Decimal("1"), rate * policy.margin_multiplier)
    days = tuple(sorted({s.trading_day for s in sessions if s.end_nano > budget.frozen_at_nano}))
    if sessions and len(days) < policy.locked_trading_days + 1:
        raise ValueError("stress_calendar_insufficient")
    legs = (rule.open_fee, rule.close_fee, rule.close_today_fee)
    def charge(price: Decimal, count: int) -> Decimal:
        return policy.fee_multiplier * max(fee_amount(leg, price, count, rule.multiplier) for leg in legs)
    total = sum(l.lots for l in budget.layers)
    opening = sum((charge(price, l.lots) for price, l in zip(entries, budget.layers)), ZERO)
    results = []
    paths = {
        "STOP": (budget.stop_price,),
        "GAP": (budget.stop_price - side * policy.gap_atr_multiple * atr,),
        "NO_STOP": tuple(budget.stop_price - side * atr * policy.adverse_atr_per_day * d
                         for d in range(1, policy.locked_trading_days + 1)) + (
            budget.stop_price - side * atr * (policy.locked_trading_days * policy.adverse_atr_per_day
                                             + policy.release_extra_atr),),
    }
    for name, path in paths.items():
        exit_price = path[-1] - side * policy.slippage_multiplier * slip
        if exit_price <= 0 or any(p <= 0 for p in path):
            raise ValueError("stress_execution_price_nonpositive")
        gross = sum((l.lots * side * (entry - exit_price) * rule.multiplier
                     for l, entry in zip(budget.layers, entries)), ZERO)
        fees = opening + charge(exit_price, total)
        entry_margin = sum((entry * l.lots * rule.multiplier * rate
                            for entry, l in zip(entries, budget.layers)), ZERO)
        margins = [entry_margin]
        available = [budget.equity_at_start_cny - opening - entry_margin]
        for price in path:
            pnl = sum((side * l.lots * (price - entry) * rule.multiplier
                       for entry, l in zip(entries, budget.layers)), ZERO)
            margin = price * total * rule.multiplier * rate
            margins.append(margin)
            available.append(budget.equity_at_start_cny - opening + pnl - margin)
        available.append(budget.equity_at_start_cny - gross - fees)
        minimum = min(available)
        results.append(StressPathResult(name, days[:len(path)], path, entries, exit_price,
            gross, fees, max(ZERO, gross + fees), max(margins), minimum,
            max(ZERO, -minimum), "SCENARIO"))
    return tuple(results)


def simulate_protective_exit(intent: OrderIntent, execution_bar: MarketBar,
                             rule: TradingRule, capacity_remaining: int,
                             position: PositionSnapshot, budget: RoundBudget) -> ExecutionResult:
    """A protective order registered before this bar: stop wins OHLC ambiguity.

    Only this execution boundary reads H/L. Entry/selection never sees them.
    A gap past the stop fills at the opening price, and locked boards retain
    the signed outstanding quantity. The synthetic reference is explicit.
    """
    if intent.offset=="OPEN" or intent.created_at_nano>=execution_bar.start_nano:
        raise ValueError("protective_exit_must_be_registered_before_execution_bar")
    if intent.episode_id!=budget.episode_id or position.episode_id!=budget.episode_id:
        raise ValueError("protective_exit_episode_mismatch")
    b=execution_bar
    if b.open is None or b.high is None or b.low is None:
        return ExecutionResult(intent.intent_id,(),intent.delta_lots,"BLOCKED",("DATA_INVALID","EXIT_PENDING"))
    side=budget.side
    stop=(b.low<=budget.stop_price if side==1 else b.high>=budget.stop_price)
    target=(b.high>=budget.target_price if side==1 else b.low<=budget.target_price)
    if not stop and not target:
        return ExecutionResult(intent.intent_id,(),intent.delta_lots,"BLOCKED",("PROTECTIVE_NOT_TRIGGERED",))
    reference=(min(b.open,budget.stop_price) if side==1 else max(b.open,budget.stop_price)) if stop else (
        max(b.open,budget.target_price) if side==1 else min(b.open,budget.target_price))
    scenario_bar=replace(b,open=reference,high=max(reference,b.high),low=min(reference,b.low))
    result=simulate_execution(intent,scenario_bar,rule,capacity_remaining,position)
    reason="STOP_FIRST_AMBIGUITY" if stop and target else "STOP_TRIGGERED" if stop else "TARGET_TRIGGERED"
    return replace(result,reasons=tuple(sorted(set(result.reasons)|{reason})))

