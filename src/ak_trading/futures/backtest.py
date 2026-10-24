"""Pure in-memory daily event replay ledger for contract-level futures.

Finalized-plan §十: ideal-price accounting, next-open execution, known-rule
fail-closed semantics, FIFO lots with today/history offset management,
capacity/limit/margin constraints, kill latch and terminal schedules. No
database, no network, no future reads. PnL flows exclusively through the
daily gross identity; fills only move fees, slippage and lots.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from statistics import median
from typing import Callable, Mapping, Sequence

from ak_trading.futures.spec import (
    ContractBar,
    FeeAmount,
    FeeSpec,
    Fill,
    MarginSpec,
    ProductKey,
    ResearchPolicy,
    Trade,
)

__all__ = [
    "ContractRuleSet",
    "SessionSchedule",
    "OpenIntent",
    "Lot",
    "ContractLedger",
    "EquityMark",
    "ReplayPlan",
    "ReplayOutcome",
    "LedgerEngine",
    "run_replay",
    "fee_components_for_fill",
    "slippage_cost",
    "compute_cap_lots",
]


@dataclass(frozen=True)
class ContractRuleSet:
    """Frozen per-contract execution rules; unknowns block fail-closed."""

    multiplier: Decimal
    price_tick: Decimal
    lot_step: int = 1
    min_open_lots: int = 1
    fee: FeeSpec | None = None
    margin: MarginSpec | None = None
    # date.isoformat() -> (upper, lower); a missing day blocks fills.
    limit_bands: Mapping[str, tuple[Decimal, Decimal]] = field(default_factory=dict)
    median_volume20: float | None = None  # overrides bar-derived capacity


@dataclass(frozen=True)
class SessionSchedule:
    trading_day: date
    session_open: datetime
    session_close: datetime


@dataclass(frozen=True)
class OpenIntent:
    """Execution intent submitted after a completed-bar decision."""

    decision_id: str
    product: ProductKey
    contract_instance_id: str
    raw_symbol: str
    target_lots: int
    kind: str  # open | reduce | close
    used_bar_id: str | None = None
    expires_after_attempt: bool = True  # open intents expire; exits persist
    # §十.7 roll linkage: when set, this open may only execute if the old
    # contract's position has been fully closed (no new leg on a stuck old
    # leg; the old leg keeps consuming its margin until actually closed).
    requires_full_exit_of: str | None = None
    is_roll: bool = False


@dataclass
class Lot:
    lots: int  # signed
    opened_trading_day: date
    entry_ideal_price: Decimal
    opening_fee_cny: Decimal
    opening_slip_cny: Decimal


@dataclass
class ContractLedger:
    instance_id: str
    raw_symbol: str
    product: ProductKey
    multiplier: Decimal
    lots: int = 0
    lots_before_open: int | None = None
    entry_lots: list[Lot] = field(default_factory=list)
    cumulative_gross: Decimal = Decimal("0")  # mark-based (equity identity)
    realized_gross: Decimal = Decimal("0")  # fill-based (cash identity)
    fees_paid: Decimal = Decimal("0")
    slip_paid: Decimal = Decimal("0")
    last_mark_close: Decimal | None = None
    last_mark_day: date | None = None
    today_open: Decimal | None = None
    incomplete_reasons: list[str] = field(default_factory=list)

    def margin_rate_for(self, lots: int, margin: MarginSpec | None) -> Decimal | None:
        if margin is None:
            return None
        rate = margin.long.rate if lots > 0 else margin.short.rate
        return rate

    def margin_required(self, price: Decimal, margin: MarginSpec | None) -> Decimal | None:
        """Rate margin at ``price``; None when the rule is unknown."""
        if self.lots == 0:
            return Decimal("0")
        rate = self.margin_rate_for(self.lots, margin)
        if rate is None:
            return None
        return abs(Decimal(self.lots)) * price * self.multiplier * rate


@dataclass(frozen=True)
class EquityMark:
    trading_day: date
    cash: Decimal
    equity: Decimal
    required_margin: Decimal | None
    status: str  # active | killed | insolvent | incomplete | terminal
    unrealized_pnl: Decimal = Decimal("0")
    gross_notional: Decimal = Decimal("0")
    invested: bool = False


def fee_components_for_fill(
    *,
    offset: str,
    lots: int,
    ideal_price: Decimal,
    multiplier: Decimal,
    fee: FeeSpec | None,
    policy: ResearchPolicy,
    scenario: str,
) -> tuple[tuple[FeeAmount, ...], str | None]:
    """Fee components for one fill leg; unknown rules return a blocker.

    base = confirmed fee + CNY 0.01/lot account addition per charged side;
    stress = every component x2 (§九.11). ``explicit_zero`` legs are legal;
    absent legs are unknown, never zero.
    """
    if fee is None:
        return (), "fee_rule_unknown"
    component = fee.leg(offset)
    if component is None:
        return (), f"fee_leg_unknown:{offset}"
    stress_multiplier = Decimal(
        policy.stress_fee_multiplier if scenario == "stress" else 1
    )
    parts: list[FeeAmount] = []
    if component.cny_per_lot is not None:
        parts.append(
            FeeAmount(
                name=f"{offset}:cny_per_lot",
                cny=component.cny_per_lot * Decimal(abs(lots)) * stress_multiplier,
                basis=component.source_id or "fee_spec",
            )
        )
    elif component.rate_per_notional is not None:
        notional = Decimal(abs(lots)) * ideal_price * multiplier
        parts.append(
            FeeAmount(
                name=f"{offset}:rate_per_notional",
                cny=notional * component.rate_per_notional * stress_multiplier,
                basis=component.source_id or "fee_spec",
            )
        )
    parts.append(
        FeeAmount(
            name=f"{offset}:account_addition",
            cny=policy.account_fee_addition_cny_per_lot
            * Decimal(abs(lots))
            * stress_multiplier,
            basis="account_estimate_exchange_rules_constant",
        )
    )
    return tuple(parts), None


def slippage_cost(
    *,
    delta_lots: int,
    tick: Decimal,
    multiplier: Decimal,
    policy: ResearchPolicy,
    scenario: str,
) -> Decimal:
    ticks = Decimal(
        policy.stress_slippage_ticks if scenario == "stress" else policy.base_slippage_ticks
    )
    return Decimal(abs(delta_lots)) * tick * multiplier * ticks


def compute_cap_lots(
    *,
    policy: ResearchPolicy,
    rule_median_volume20: float | None,
    prior_bars: Sequence[ContractBar],
) -> int | None:
    """floor(0.001 * median_volume20) over the 20 sessions before the open."""
    if rule_median_volume20 is not None:
        if not math.isfinite(rule_median_volume20) or rule_median_volume20 < 0:
            return None
        return int(math.floor(policy.participation_cap * rule_median_volume20))
    if len(prior_bars) < 20:
        return None
    window = [float(bar.volume) for bar in prior_bars[-20:] if bar.volume is not None]
    if len(window) < 20:
        return None
    return int(math.floor(policy.participation_cap * median(window)))


class LedgerEngine:
    """Deterministic per-fold ledger; one instance per fold/baseline/scenario."""

    def __init__(
        self,
        *,
        initial_cash: Decimal,
        policy: ResearchPolicy,
        scenario: str = "base",
        contracts: Mapping[str, tuple[ProductKey, str, ContractRuleSet]],
    ) -> None:
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.policy = policy
        self.scenario = scenario
        self.ledgers: dict[str, ContractLedger] = {
            instance_id: ContractLedger(
                instance_id=instance_id,
                raw_symbol=raw_symbol,
                product=product,
                multiplier=rules.multiplier,
            )
            for instance_id, (product, raw_symbol, rules) in contracts.items()
        }
        self.rules = {
            instance_id: entry[2] for instance_id, entry in contracts.items()
        }
        self.fills: list[Fill] = []
        self.roll_fill_ids: set[str] = set()
        self.marks: list[EquityMark] = []
        self.blocked: list[dict[str, str]] = []
        self.capacity_used: dict[tuple[str, date], int] = {}
        self.killed = False
        self.insolvent = False
        self.peak_equity = initial_cash
        self._fill_seq = 0

    # -- execution ---------------------------------------------------------

    def _next_fill_id(self) -> str:
        self._fill_seq += 1
        return f"F{self._fill_seq:06d}"

    def execute_intent(
        self,
        intent: OpenIntent,
        *,
        session: SessionSchedule,
        open_price: Decimal,
        day_bars_before: Sequence[ContractBar],
        known_equity: Decimal,
    ) -> list[Fill]:
        """Attempt one intent at this session open; returns executed fills.

        Exits run before opens by caller ordering; a reversal inside one
        intent closes first, then opens. Unknown rules block the affected
        action fail-closed and are recorded, never guessed (§十.8-9).
        """
        ledger = self.ledgers.get(intent.contract_instance_id)
        rules = self.rules.get(intent.contract_instance_id)
        if ledger is None or rules is None:
            self.blocked.append(
                {"decision_id": intent.decision_id, "reason": "unknown_contract"}
            )
            return []
        day = session.trading_day
        if ledger.today_open is None:
            # First activity of the day for this contract.
            ledger.today_open = open_price
            ledger.lots_before_open = ledger.lots

        bands = rules.limit_bands.get(day.isoformat())
        if bands is None:
            self.blocked.append(
                {"decision_id": intent.decision_id, "reason": "limit_bands_unknown"}
            )
            return []
        upper, lower = bands
        if open_price > upper or open_price < lower:
            self.blocked.append(
                {"decision_id": intent.decision_id, "reason": "open_outside_limit_bands"}
            )
            ledger.incomplete_reasons.append("open_outside_limit_bands")
            return []
        if open_price == upper or open_price == lower:
            # Limit-touch opens do not fill by default, exits included (§十.8).
            self.blocked.append(
                {"decision_id": intent.decision_id, "reason": "limit_open_no_fill"}
            )
            return []

        cap = compute_cap_lots(
            policy=self.policy,
            rule_median_volume20=rules.median_volume20,
            prior_bars=day_bars_before,
        )
        if cap is None:
            self.blocked.append(
                {"decision_id": intent.decision_id, "reason": "capacity_unknown"}
            )
            return []

        target = intent.target_lots
        current = ledger.lots
        fills: list[Fill] = []
        if target != current:
            delta = target - current
            reducing = current != 0 and (
                (delta > 0) != (current > 0) or abs(target) < abs(current)
            )
            if reducing:
                close_lots = min(abs(delta), abs(current))
                fills.extend(
                    self._execute_close(
                        ledger, rules, intent, session, open_price, close_lots, cap,
                        upper, lower,
                    )
                )
            remainder = target - ledger.lots
            if remainder != 0 and (
                ledger.lots == 0 or (remainder > 0) == (ledger.lots > 0)
            ):
                if intent.kind == "open" and (self.killed or self.insolvent):
                    self.blocked.append(
                        {"decision_id": intent.decision_id,
                         "reason": "kill_switch_blocks_new_risk"}
                    )
                    return fills
                fills.extend(
                    self._execute_open(
                        ledger, rules, intent, session, open_price,
                        remainder, cap, known_equity, upper, lower,
                    )
                )
        return fills

    def _capacity_remaining(self, instance_id: str, day: date, cap: int) -> int:
        return cap - self.capacity_used.get((instance_id, day), 0)

    def _consume_capacity(self, instance_id: str, day: date, lots: int) -> None:
        self.capacity_used[(instance_id, day)] = (
            self.capacity_used.get((instance_id, day), 0) + lots
        )

    def _execute_close(
        self,
        ledger: ContractLedger,
        rules: ContractRuleSet,
        intent: OpenIntent,
        session: SessionSchedule,
        open_price: Decimal,
        lots_requested: int,
        cap: int,
        upper: Decimal,
        lower: Decimal,
    ) -> list[Fill]:
        day = session.trading_day
        available = self._capacity_remaining(ledger.instance_id, day, cap)
        if available <= 0:
            self.blocked.append(
                {"decision_id": intent.decision_id,
                 "reason": "capacity_exhausted_no_close"}
            )
            return []
        fills: list[Fill] = []
        to_close = min(lots_requested, available)
        while to_close > 0 and ledger.entry_lots:
            lot = ledger.entry_lots[0]
            lot_is_today = lot.opened_trading_day == day
            closed_here = min(to_close, abs(lot.lots))
            offset = "close_today" if lot_is_today else "close"
            fee_parts, fee_block = fee_components_for_fill(
                offset=offset,
                lots=closed_here,
                ideal_price=open_price,
                multiplier=ledger.multiplier,
                fee=rules.fee,
                policy=self.policy,
                scenario=self.scenario,
            )
            if fee_block is not None:
                self.blocked.append(
                    {"decision_id": intent.decision_id, "reason": fee_block}
                )
                break
            direction = 1 if lot.lots > 0 else -1
            slip = slippage_cost(
                delta_lots=closed_here,
                tick=rules.price_tick,
                multiplier=ledger.multiplier,
                policy=self.policy,
                scenario=self.scenario,
            )
            fee_total = sum(part.cny for part in fee_parts)
            # Cash advances only by realized PnL, fees and slippage (§十.4);
            # the open position's PnL stays unrealized until closed.
            realized = (
                Decimal(closed_here)
                * ledger.multiplier
                * (open_price - lot.entry_ideal_price)
                * direction
            )
            effective = open_price - direction * (
                rules.price_tick
                * Decimal(
                    self.policy.stress_slippage_ticks
                    if self.scenario == "stress"
                    else self.policy.base_slippage_ticks
                )
            )
            if effective > upper or effective < lower:
                # Conservative display price outside the known band: the fill
                # is not executed and the slippage is never trimmed (§十.5).
                self.blocked.append(
                    {"decision_id": intent.decision_id,
                     "reason": "modeled_slippage_outside_band"}
                )
                break
            fill = Fill(
                fill_id=self._next_fill_id(),
                decision_id=intent.decision_id,
                product=ledger.product,
                contract_instance_id=ledger.instance_id,
                raw_symbol=ledger.raw_symbol,
                event_time=session.session_open,
                trading_day=day,
                side=-direction,
                offset=offset,
                lots=closed_here,
                ideal_open=open_price,
                effective_price=effective,
                multiplier=ledger.multiplier,
                fee_components=fee_parts,
                fee_cny=fee_total,
                slippage_cost=slip,
                rule_evidence_ids=(f"{ledger.instance_id}:{offset}",),
                used_bar_id=intent.used_bar_id,
            )
            # Commit this FIFO batch only after the fill is fully validated.
            # A later batch may be blocked without undoing earlier fills.
            ledger.realized_gross += realized
            self.cash += realized - fee_total - slip
            fills.append(fill)
            self.fills.append(fill)
            if intent.is_roll:
                self.roll_fill_ids.add(fill.fill_id)
            self._consume_capacity(ledger.instance_id, day, closed_here)
            ledger.lots -= direction * closed_here
            if abs(lot.lots) == closed_here:
                ledger.entry_lots.pop(0)
            else:
                lot.lots -= direction * closed_here
            ledger.fees_paid += fee_total
            ledger.slip_paid += slip
            to_close -= closed_here
        return fills

    def _execute_open(
        self,
        ledger: ContractLedger,
        rules: ContractRuleSet,
        intent: OpenIntent,
        session: SessionSchedule,
        open_price: Decimal,
        lots: int,
        cap: int,
        known_equity: Decimal,
        upper: Decimal,
        lower: Decimal,
    ) -> list[Fill]:
        day = session.trading_day
        direction = 1 if lots > 0 else -1
        available = self._capacity_remaining(ledger.instance_id, day, cap)
        magnitude = min(abs(lots), max(available, 0))
        magnitude = (magnitude // rules.lot_step) * rules.lot_step
        if magnitude <= 0:
            self.blocked.append(
                {"decision_id": intent.decision_id,
                 "reason": "capacity_exhausted_no_open" if available <= 0 else "below_lot_step"}
            )
            return []
        if magnitude < rules.min_open_lots:
            self.blocked.append(
                {"decision_id": intent.decision_id, "reason": "below_minimum_open_lots"}
            )
            return []
        fee_parts, fee_block = fee_components_for_fill(
            offset="open",
            lots=magnitude,
            ideal_price=open_price,
            multiplier=ledger.multiplier,
            fee=rules.fee,
            policy=self.policy,
            scenario=self.scenario,
        )
        if fee_block is not None:
            self.blocked.append({"decision_id": intent.decision_id, "reason": fee_block})
            return []
        rate = ledger.margin_rate_for(direction, rules.margin)
        # Margin is occupancy checked against the POST-TRADE global
        # requirement (§九.10): every open ledger at its latest price plus
        # this trade's resulting total position, never just the added lots.
        if rate is None:
            self.blocked.append(
                {"decision_id": intent.decision_id,
                 "reason": "margin_rule_unknown_blocks_new_risk"}
            )
            return []
        others_margin = Decimal("0")
        for other in self.ledgers.values():
            if other is ledger or other.lots == 0:
                continue
            other_price = other.last_mark_close or open_price
            required = other.margin_required(other_price, self.rules[other.instance_id].margin)
            if required is None:
                self.blocked.append(
                    {"decision_id": intent.decision_id,
                     "reason": "margin_rule_unknown_blocks_new_risk"}
                )
                return []
            others_margin += required
        resulting_lots = ledger.lots + direction * magnitude
        this_total_margin = (
            abs(Decimal(resulting_lots)) * open_price * ledger.multiplier * rate
        )
        total_after = others_margin + this_total_margin
        if total_after > Decimal(str(self.policy.max_margin_ratio)) * known_equity:
            self.blocked.append(
                {"decision_id": intent.decision_id, "reason": "margin_ratio_exceeded"}
            )
            return []
        slip = slippage_cost(
            delta_lots=magnitude,
            tick=rules.price_tick,
            multiplier=ledger.multiplier,
            policy=self.policy,
            scenario=self.scenario,
        )
        fee_total = sum(part.cny for part in fee_parts)
        effective = open_price + direction * (
            rules.price_tick
            * Decimal(
                self.policy.stress_slippage_ticks
                if self.scenario == "stress"
                else self.policy.base_slippage_ticks
            )
        )
        if effective > upper or effective < lower:
            self.blocked.append(
                {"decision_id": intent.decision_id,
                 "reason": "modeled_slippage_outside_band"}
            )
            return []
        self.cash -= fee_total + slip
        fill = Fill(
            fill_id=self._next_fill_id(),
            decision_id=intent.decision_id,
            product=ledger.product,
            contract_instance_id=ledger.instance_id,
            raw_symbol=ledger.raw_symbol,
            event_time=session.session_open,
            trading_day=day,
            side=direction,
            offset="open",
            lots=magnitude,
            ideal_open=open_price,
            effective_price=effective,
            multiplier=ledger.multiplier,
            fee_components=fee_parts,
            fee_cny=fee_total,
            slippage_cost=slip,
            rule_evidence_ids=(f"{ledger.instance_id}:open",),
            used_bar_id=intent.used_bar_id,
        )
        self.fills.append(fill)
        if intent.is_roll or intent.requires_full_exit_of is not None:
            self.roll_fill_ids.add(fill.fill_id)
        self._consume_capacity(ledger.instance_id, day, magnitude)
        ledger.lots += direction * magnitude
        ledger.entry_lots.append(
            Lot(
                lots=direction * magnitude,
                opened_trading_day=day,
                entry_ideal_price=open_price,
                opening_fee_cny=fee_total,
                opening_slip_cny=slip,
            )
        )
        ledger.fees_paid += fee_total
        ledger.slip_paid += slip
        return [fill]

    # -- marking -----------------------------------------------------------

    def mark_day(self, day: date, closes: Mapping[str, Decimal | None]) -> EquityMark:
        """Mark one day under the section 10.4 two-quantity identities.

        cash   = initial + sum(realized_gross from fills) - fees - slippage
        equity = cash + sum(unrealized on open lots at this day's close)
        The daily gross identity (q_prev/q_after) still explains the equity
        change and is tracked per ledger; it never touches cash until a
        close fill realizes it. A None close for a held contract marks the
        ledger incomplete (valuation_unavailable), never a fabricated price.
        """
        margin_total = Decimal("0")
        margin_known = True
        gross_notional = Decimal("0")
        invested = False
        for instance_id, close in sorted(closes.items()):
            ledger = self.ledgers[instance_id]
            rules = self.rules[instance_id]
            if close is None:
                if ledger.lots != 0:
                    ledger.incomplete_reasons.append(
                        f"valuation_unavailable:{day.isoformat()}"
                    )
                    margin_known = False
                    invested = True
                continue
            q_prev = (
                ledger.lots_before_open
                if ledger.lots_before_open is not None
                else ledger.lots
            )
            q_after = ledger.lots
            open_price = ledger.today_open if ledger.today_open is not None else close
            if q_prev != 0 or q_after != 0:
                if ledger.last_mark_close is not None:
                    gross = Decimal(q_prev) * ledger.multiplier * (
                        open_price - ledger.last_mark_close
                    ) + Decimal(q_after) * ledger.multiplier * (close - open_price)
                else:
                    gross = Decimal(q_after) * ledger.multiplier * (close - open_price)
            else:
                gross = Decimal("0")
            ledger.cumulative_gross += gross
            ledger.last_mark_close = close
            ledger.last_mark_day = day
            ledger.today_open = None
            ledger.lots_before_open = None
            if ledger.lots != 0:
                invested = True
                gross_notional += (
                    abs(Decimal(ledger.lots)) * close * ledger.multiplier
                )
                required = ledger.margin_required(close, rules.margin)
                if required is None:
                    margin_known = False
                else:
                    margin_total += required
        # Cash advances only by realized PnL, fees and slippage.
        realized_total = sum(
            (ledger.realized_gross for ledger in self.ledgers.values()), Decimal("0")
        )
        fee_total = sum((fill.fee_cny for fill in self.fills), Decimal("0"))
        slip_total = sum((fill.slippage_cost for fill in self.fills), Decimal("0"))
        self.cash = self.initial_cash + realized_total - fee_total - slip_total
        # Unrealized uses each FIFO lot's entry price at the fresh close.
        unrealized = Decimal("0")
        for ledger in self.ledgers.values():
            if ledger.lots == 0 or ledger.last_mark_close is None:
                continue
            for lot in ledger.entry_lots:
                unrealized += (
                    Decimal(lot.lots)
                    * ledger.multiplier
                    * (ledger.last_mark_close - lot.entry_ideal_price)
                )
        equity = self.cash + unrealized
        if equity <= 0:
            self.insolvent = True
        self.peak_equity = max(self.peak_equity, equity)
        if not self.killed and self.peak_equity > 0:
            if (
                self.peak_equity - equity
            ) / self.peak_equity >= self.policy.drawdown_kill:
                self.killed = True
        status = (
            "insolvent" if self.insolvent
            else "killed" if self.killed
            else ("incomplete" if not margin_known else "active")
        )
        mark = EquityMark(
            trading_day=day,
            cash=self.cash,
            equity=equity,
            required_margin=margin_total if margin_known else None,
            status=status,
            unrealized_pnl=unrealized,
            gross_notional=gross_notional,
            invested=invested,
        )
        self.marks.append(mark)
        return mark

    # -- trades & audit ----------------------------------------------------

    def build_trades(self) -> list[Trade]:
        """FIFO closed trades with exact proportional fee/slip attribution."""
        trades: list[Trade] = []
        open_batches: dict[str, list[Fill]] = {}
        close_fills: dict[str, list[Fill]] = {}
        for fill in self.fills:
            bucket = open_batches if fill.offset == "open" else close_fills
            bucket.setdefault(fill.contract_instance_id, []).append(fill)
        for instance_id in sorted(open_batches.keys() | close_fills.keys()):
            pending: list[tuple[Fill, int]] = [
                (fill, fill.lots) for fill in open_batches.get(instance_id, [])
            ]
            for close_fill in close_fills.get(instance_id, []):
                remaining = close_fill.lots
                direction = close_fill.side * -1
                while remaining > 0 and pending:
                    open_fill, open_remaining = pending[0]
                    matched = min(remaining, open_remaining)
                    close_share = Decimal(matched) / Decimal(close_fill.lots)
                    open_share = Decimal(matched) / Decimal(open_fill.lots)
                    closing_fees = close_fill.fee_cny * close_share
                    closing_slip = close_fill.slippage_cost * close_share
                    opening_fees = open_fill.fee_cny * open_share
                    opening_slip = open_fill.slippage_cost * open_share
                    gross = (
                        Decimal(matched)
                        * close_fill.multiplier
                        * (close_fill.ideal_open - open_fill.ideal_open)
                        * direction
                    )
                    trades.append(
                        Trade(
                            trade_id=f"T{len(trades) + 1:06d}",
                            strategy_id="portfolio",
                            product=close_fill.product,
                            contract_instance_id=instance_id,
                            raw_symbol=close_fill.raw_symbol,
                            opened_event_time=open_fill.event_time,
                            closed_event_time=close_fill.event_time,
                            direction=direction,
                            lots=matched,
                            entry_ideal_price=open_fill.ideal_open,
                            exit_ideal_price=close_fill.ideal_open,
                            multiplier=close_fill.multiplier,
                            opening_fees=opening_fees,
                            closing_fees=closing_fees,
                            opening_slippage=opening_slip,
                            closing_slippage=closing_slip,
                            gross_pnl=gross,
                            net_pnl=(
                                gross
                                - opening_fees
                                - closing_fees
                                - opening_slip
                                - closing_slip
                            ),
                        )
                    )
                    remaining -= matched
                    if matched >= open_remaining:
                        pending.pop(0)
                    else:
                        pending[0] = (open_fill, open_remaining - matched)
        return trades

    def turnover_notional(self) -> Decimal:
        return sum(
            (Decimal(fill.lots) * fill.ideal_open * fill.multiplier for fill in self.fills),
            Decimal("0"),
        )

    def reconciliation(self) -> dict[str, object]:
        """Independent identity audit: cash from FILL records only.

        The expected cash is recomputed by matching close fills against open
        fills FIFO (realized PnL, fees, slippage) rather than from the
        engine's own cash accumulator, and the equity identity is checked
        against the per-day gross-identity totals, so the two quantities
        cannot silently collapse into one.
        """
        total_fees = sum((fill.fee_cny for fill in self.fills), Decimal("0"))
        total_slip = sum((fill.slippage_cost for fill in self.fills), Decimal("0"))
        realized_from_fills = Decimal("0")
        open_batches: dict[str, list[Fill]] = {}
        close_fills: dict[str, list[Fill]] = {}
        for fill in self.fills:
            bucket = open_batches if fill.offset == "open" else close_fills
            bucket.setdefault(fill.contract_instance_id, []).append(fill)
        for instance_id in sorted(close_fills):
            pending = [(f, f.lots) for f in open_batches.get(instance_id, [])]
            for close_fill in close_fills[instance_id]:
                remaining = close_fill.lots
                direction = close_fill.side * -1
                while remaining > 0 and pending:
                    open_fill, open_remaining = pending[0]
                    matched = min(remaining, open_remaining)
                    realized_from_fills += (
                        Decimal(matched)
                        * close_fill.multiplier
                        * (close_fill.ideal_open - open_fill.ideal_open)
                        * direction
                    )
                    remaining -= matched
                    if matched >= open_remaining:
                        pending.pop(0)
                    else:
                        pending[0] = (open_fill, open_remaining - matched)
        expected_cash = self.initial_cash + realized_from_fills - total_fees - total_slip
        ledger_gross = sum(
            (ledger.cumulative_gross for ledger in self.ledgers.values()), Decimal("0")
        )
        final_equity = self.marks[-1].equity if self.marks else self.cash
        expected_equity = self.initial_cash + ledger_gross - total_fees - total_slip
        return {
            "cash_matches_fill_identity": self.cash == expected_cash,
            "cash": self.cash,
            "expected_cash_from_fills": expected_cash,
            "equity_matches_gross_identity": final_equity == expected_equity,
            "equity": final_equity,
            "expected_equity_from_daily_gross": expected_equity,
            "final_unrealized": (
                self.marks[-1].unrealized_pnl if self.marks else Decimal("0")
            ),
            "cumulative_gross": ledger_gross,
            "realized_gross": realized_from_fills,
            "fees": total_fees,
            "slippage": total_slip,
            "ledger_fees_match": sum(
                (ledger.fees_paid for ledger in self.ledgers.values()), Decimal("0")
            ) == total_fees,
            "ledger_slip_match": sum(
                (ledger.slip_paid for ledger in self.ledgers.values()), Decimal("0")
            ) == total_slip,
        }


# ---------------------------------------------------------------------------
# Daily replay orchestration (§十.2-3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayPlan:
    """Deterministic replay inputs: sessions, bars, decisions, terminals.

    ``decisions[(trading_day, instance_id)]`` are intents decided AFTER that
    day's completed bar; they execute at the next session open. Open intents
    expire after one eligible attempt; exit intents persist until filled or
    the ledger goes incomplete.
    """

    days: Sequence[date]
    sessions: Mapping[str, Mapping[date, SessionSchedule]]
    bars: Mapping[str, Mapping[date, ContractBar]]
    decisions: Mapping[tuple[date, str], Sequence[OpenIntent]]
    terminal_days: Mapping[str, date]
    # Confirmed-closed days per instance extend marks silently (§五.6.f);
    # a session simply absent while not confirmed closed is a data gap.
    confirmed_closed: frozenset[tuple[str, date]] = frozenset()


@dataclass(frozen=True)
class ReplayOutcome:
    fills: tuple[Fill, ...]
    trades: tuple[Trade, ...]
    marks: tuple[EquityMark, ...]
    blocked: tuple[Mapping[str, str], ...]
    turnover_notional: Decimal
    reconciliation: Mapping[str, object]
    killed: bool
    insolvent: bool
    incomplete_contracts: tuple[str, ...]
    open_positions: Mapping[str, int]
    incomplete_reasons: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    final_unrealized_pnl: Decimal = Decimal("0")
    # complete | incomplete | killed | insolvent (§十.12 / §十一.9, B3 fix)
    status: str = "complete"
    open_trade_count: int = 0
    roll_fill_ids: tuple[str, ...] = ()
    roll_costs_cny: Decimal = Decimal("0")


def _require_fresh_warmup_engine(engine: LedgerEngine) -> None:
    """Warmup may carry strategy state, never a training account."""
    if (
        engine.cash != engine.initial_cash
        or engine.peak_equity != engine.initial_cash
        or engine.fills
        or engine.roll_fill_ids
        or engine.marks
        or engine.blocked
        or engine.capacity_used
        or engine.killed
        or engine.insolvent
        or engine._fill_seq != 0
        or any(
            ledger != ContractLedger(
                instance_id=ledger.instance_id,
                raw_symbol=ledger.raw_symbol,
                product=ledger.product,
                multiplier=ledger.multiplier,
            )
            for ledger in engine.ledgers.values()
        )
    ):
        raise ValueError("warmup requires a fresh engine without account or ledger residuals")


def _completed_bar_available_at(bar: ContractBar) -> datetime | None:
    """Unknown, naive or pre-close publication clocks are not usable history."""
    if (
        bar.available_at is None
        or bar.session_close is None
        or bar.available_at.utcoffset() is None
        or bar.session_close.utcoffset() is None
        or bar.available_at < bar.session_close
    ):
        return None
    return bar.available_at


def run_replay(
    engine: LedgerEngine,
    plan: ReplayPlan,
    *,
    decision_provider: Callable[[LedgerEngine, date], Sequence[OpenIntent]] | None = None,
    warmup_days: Sequence[date] = (),
    enforce_availability: bool = False,
) -> ReplayOutcome:
    """Replay the plan day by day in causal event order (§十.2-3).

    Scored and warmup dates must each be strictly ascending and unique, and
    warmup dates must all precede the first scored date. An empty scored
    smoke plan remains valid when no warmup is requested.

    ``decision_provider`` is invoked after each scored day's marks. With
    ``warmup_days``, it is first called once per warmup date, with a fresh,
    unchanged account and no execution or marking. Only the final warmup
    date's provider/map intents survive into the first scored session.

    The host maintains completed-bar strategy history in its closure. Warmup
    bars require known, aware publication/close clocks in callback order and
    strictly before the earliest scored session open; invalid clocks raise
    before any callback. Other pre-scored bars seed capacity only when those
    same clock checks pass. Unknown/late prehistory is omitted, never dated
    by guesswork. In warmup replays, each scored attempt also filters history
    and defers decisions until strictly after their publication clocks.
    Empty warmup preserves the existing replay/smoke behavior.
    """
    days = tuple(plan.days)
    warmup_days = tuple(warmup_days)
    if any(left >= right for left, right in zip(days, days[1:])):
        raise ValueError("scored plan.days must be strictly ascending and unique")
    if any(left >= right for left, right in zip(warmup_days, warmup_days[1:])):
        raise ValueError("warmup_days must be strictly ascending and unique")
    if warmup_days and (not days or warmup_days[-1] >= days[0]):
        raise ValueError("warmup_days must precede a nonempty scored plan.days")

    causal_checks = bool(warmup_days) or enforce_availability
    decision_dependency_clock: datetime | None = None
    dependency_clock_unknown = False
    carry: dict[str, list[OpenIntent]] = {}
    historical_bars: dict[str, list[ContractBar]] = {
        instance_id: [] for instance_id in plan.bars
    }
    decision_available_at: dict[int, datetime | None] = {}
    cited_bars: dict[tuple[str, str], ContractBar] = {}
    warmup_clocks: dict[date, datetime] = {}
    if causal_checks:
        _require_fresh_warmup_engine(engine)
        scored_day_set = set(days)
        scored_opens = [
            session.session_open
            for sessions in plan.sessions.values()
            for day, session in sessions.items()
            if day in scored_day_set
        ]
        if not scored_opens or any(t.utcoffset() is None for t in scored_opens):
            raise ValueError("warmup requires a known, aware scored session open")
        first_open = min(scored_opens)
        previous_clock: datetime | None = None
        # Validate the whole prefix before a provider can mutate closure state.
        for day in warmup_days:
            clocks = []
            for instance_id, series in plan.bars.items():
                bar = series.get(day)
                if bar is None:
                    continue
                available_at = _completed_bar_available_at(bar)
                if available_at is None or bar.trading_day != day:
                    raise ValueError(f"invalid warmup bar clock: {instance_id}:{day}")
                if available_at >= first_open:
                    raise ValueError(
                        f"warmup bar must be available before first scored open: {instance_id}:{day}"
                    )
                clocks.append(available_at)
            if clocks:
                clock = max(clocks)
                if previous_clock is not None and clock < previous_clock:
                    raise ValueError("warmup publication clocks must follow callback day order")
                warmup_clocks[day] = clock
                previous_clock = clock
        for instance_id, series in plan.bars.items():
            for day, bar in sorted(series.items()):
                cited_bars[(instance_id, f"{instance_id}:{bar.datetime_nano}")] = bar
                cited_bars[(instance_id, f"{bar.symbol}:{bar.datetime_nano}")] = bar
                available_at = _completed_bar_available_at(bar)
                if (
                    day < days[0]
                    and bar.trading_day == day
                    and available_at is not None
                    and available_at < first_open
                ):
                    historical_bars[instance_id].append(bar)

    def _queue_decisions(day: date) -> None:
        nonlocal decision_dependency_clock, dependency_clock_unknown
        queued: list[tuple[str, OpenIntent]] = []
        if decision_provider is not None:
            queued.extend(
                (intent.contract_instance_id, intent)
                for intent in decision_provider(engine, day)
            )
        for (decision_day, instance_id), intents in sorted(
            plan.decisions.items(), key=lambda item: (item[0][0], item[0][1])
        ):
            if decision_day == day:
                queued.extend((instance_id, intent) for intent in intents)
        if causal_checks:
            # Logical state may retain earlier observations, so a later
            # decision inherits every prior publication dependency.
            clocks = [
                session.session_close
                for sessions in plan.sessions.values()
                if (session := sessions.get(day)) is not None
            ]
            clocks.extend(
                bar.available_at
                for series in plan.bars.values()
                if (bar := series.get(day)) is not None and bar.available_at is not None
            )
            if day in warmup_clocks:
                clocks.append(warmup_clocks[day])
            clock = max(clocks) if clocks and all(t.utcoffset() is not None for t in clocks) else None
            if clock is None or any(
                _completed_bar_available_at(bar) is None
                for series in plan.bars.values() if (bar := series.get(day)) is not None
            ):
                dependency_clock_unknown = True
            if clock is not None:
                decision_dependency_clock = max(clock, decision_dependency_clock) if decision_dependency_clock else clock
            clock = None if dependency_clock_unknown else decision_dependency_clock
            for instance_id, intent in queued:
                available_at = clock
                if intent.used_bar_id is not None:
                    used_bar = cited_bars.get((instance_id, intent.used_bar_id))
                    bar_clock = _completed_bar_available_at(used_bar) if used_bar is not None else None
                    available_at = (
                        max(clock, bar_clock)
                        if clock is not None and bar_clock is not None else None
                    )
                decision_available_at[id(intent)] = available_at
        for instance_id, intent in queued:
            carry.setdefault(instance_id, []).append(intent)

    initial_cash = engine.initial_cash
    for day in warmup_days:
        carry.clear()
        decision_available_at.clear()
        _queue_decisions(day)
        if engine.initial_cash != initial_cash:
            raise ValueError("warmup provider must not change account capital")
        _require_fresh_warmup_engine(engine)

    for day in days:
        # 1) Session opens. Explicit intra-day precedence (§十.3 / A1):
        #    pass 1 executes every contract's EXIT intents (earlier-submitted
        #    risk reductions first), pass 2 executes OPEN intents, both in a
        #    stable (product, raw symbol) order, so capacity and margin are
        #    consumed exits-first across contracts — never by an open that
        #    could sneak ahead of another product's stuck exit.
        todays_actions: list[tuple[str, SessionSchedule, object, list[OpenIntent], list[OpenIntent], bool]] = []
        for instance_id in sorted(
            plan.sessions,
            key=lambda i: (str(engine.ledgers[i].product), engine.ledgers[i].raw_symbol),
        ):
            session = plan.sessions.get(instance_id, {}).get(day)
            if session is None:
                continue
            bar = plan.bars.get(instance_id, {}).get(day)
            if bar is None or bar.open is None:
                # Session exists but the bar is missing: no fills today; if a
                # position is held the day ends valuation_unavailable.
                if engine.ledgers[instance_id].lots != 0:
                    engine.ledgers[instance_id].incomplete_reasons.append(
                        f"data_gap:{day.isoformat()}"
                    )
                continue
            intents = carry.pop(instance_id, [])
            if causal_checks:
                eligible = []
                for intent in intents:
                    available_at = decision_available_at.get(id(intent))
                    if available_at is None:
                        engine.blocked.append(
                            {"decision_id": intent.decision_id, "reason": "decision_clock_unknown"}
                        )
                    elif available_at >= session.session_open:
                        # Publication at the open is too late. This is not an
                        # eligible attempt, so even one-shot opens survive.
                        engine.blocked.append(
                            {"decision_id": intent.decision_id, "reason": "decision_not_available_before_open"}
                        )
                        carry.setdefault(instance_id, []).append(intent)
                    else:
                        eligible.append(intent)
                intents = eligible
            exits = [i for i in intents if i.kind in ("close", "reduce")]
            opens = [i for i in intents if i.kind == "open"]
            is_terminal = plan.terminal_days.get(instance_id) == day
            if is_terminal and engine.ledgers[instance_id].lots != 0:
                # §十.12: the terminal open only closes positions; the exit
                # intent is engine-generated and identifies itself.
                exits = [
                    OpenIntent(
                        decision_id=f"terminal|{day.isoformat()}|{instance_id}",
                        product=engine.ledgers[instance_id].product,
                        contract_instance_id=instance_id,
                        raw_symbol=engine.ledgers[instance_id].raw_symbol,
                        target_lots=0,
                        kind="close",
                        used_bar_id=None,
                    )
                ] + exits
            todays_actions.append((instance_id, session, bar, exits, opens, is_terminal))

        known_equity = engine.marks[-1].equity if engine.marks else engine.initial_cash

        def _attempt(instance_id: str, session: SessionSchedule, bar, intent: OpenIntent) -> None:
            # §十.7 roll linkage: a successor open requires the old leg to be
            # fully closed; a stuck old leg also keeps its margin consumed.
            if intent.requires_full_exit_of is not None:
                old = engine.ledgers.get(intent.requires_full_exit_of)
                if old is not None and old.lots != 0:
                    engine.blocked.append(
                        {
                            "decision_id": intent.decision_id,
                            "reason": "roll_old_position_not_closed",
                            "old_contract": intent.requires_full_exit_of,
                        }
                    )
                    engine.ledgers[instance_id].incomplete_reasons.append(
                        f"roll_new_leg_blocked_old_open:{day.isoformat()}"
                    )
                    return
            before_fills = len(engine.fills)
            before_blocked = len(engine.blocked)
            prior_bars = historical_bars.get(instance_id, [])
            if causal_checks:
                prior_bars = [
                    prior for prior in prior_bars
                    if (available_at := _completed_bar_available_at(prior)) is not None
                    and available_at < session.session_open
                    and prior.trading_day is not None
                    and prior.trading_day < day
                ]
            engine.execute_intent(
                intent,
                session=session,
                open_price=bar.open,
                day_bars_before=prior_bars,
                known_equity=known_equity,
            )
            blocked_here = engine.blocked[before_blocked:]
            if (
                intent.requires_full_exit_of is not None
                and len(engine.fills) == before_fills
                and blocked_here
            ):
                # Old leg fully closed but the new leg could not open: the
                # roll completes partially into cash (§十.7).
                engine.blocked.append(
                    {
                        "decision_id": intent.decision_id,
                        "reason": "roll_partial_to_cash",
                        "new_contract": instance_id,
                    }
                )

        for instance_id, session, bar, exits, opens, is_terminal in todays_actions:
            for intent in exits:
                _attempt(instance_id, session, bar, intent)
        for instance_id, session, bar, exits, opens, is_terminal in todays_actions:
            for intent in opens:
                if is_terminal:
                    # Recorded, not silently dropped (§十二.16 honesty).
                    engine.blocked.append(
                        {"decision_id": intent.decision_id, "reason": "terminal_close_only"}
                    )
                    continue
                _attempt(instance_id, session, bar, intent)

        for instance_id, session, bar, exits, opens, is_terminal in todays_actions:
            intents = exits + opens
            # Terminal exits that did not fully close leave a residue that is
            # reported, never silently flattened (§十.12 / A3).
            if is_terminal and engine.ledgers[instance_id].lots != 0:
                engine.ledgers[instance_id].incomplete_reasons.append(
                    f"terminal_close_incomplete:{day.isoformat()}"
                )
            remaining: list[OpenIntent] = []
            for intent in intents:
                if intent.kind == "open" and not is_terminal:
                    continue  # attempted once at this eligible open
                if intent.kind == "close" and intent.decision_id.startswith("terminal|"):
                    continue  # engine-generated, never carried
                if engine.ledgers[instance_id].lots == 0 and intent.kind in ("close", "reduce"):
                    continue  # fully closed
                remaining.append(intent)
            if remaining:
                carry.setdefault(instance_id, []).extend(remaining)
            historical_bars.setdefault(instance_id, []).append(bar)

        # 2) Marks at each session close (value uses that day's close).
        closes: dict[str, Decimal | None] = {}
        for instance_id in sorted(plan.sessions):
            session = plan.sessions.get(instance_id, {}).get(day)
            ledger = engine.ledgers.get(instance_id)
            if ledger is None:
                continue
            if session is None:
                if (instance_id, day) in plan.confirmed_closed:
                    # Known closed market: positions extend the prior mark
                    # with zero increment (§五.6.f).
                    closes[instance_id] = ledger.last_mark_close
                elif ledger.lots != 0 or ledger.last_mark_close is not None:
                    closes[instance_id] = None
                continue
            bar = plan.bars.get(instance_id, {}).get(day)
            closes[instance_id] = bar.close if bar is not None else None
        mark = engine.mark_day(day, closes)
        if mark.status == "active" and any(
            terminal_day == day for terminal_day in plan.terminal_days.values()
        ):
            # Label the terminal row as terminal rather than active (B3).
            mark = EquityMark(
                trading_day=mark.trading_day,
                cash=mark.cash,
                equity=mark.equity,
                required_margin=mark.required_margin,
                status="terminal",
                unrealized_pnl=mark.unrealized_pnl,
                gross_notional=mark.gross_notional,
                invested=mark.invested,
            )
            engine.marks[-1] = mark

        # 3) Completed-bar decisions become intents for the next open.
        _queue_decisions(day)

    trades = engine.build_trades()
    incomplete = tuple(
        sorted(
            instance_id
            for instance_id, ledger in engine.ledgers.items()
            if ledger.incomplete_reasons
        )
    )
    open_positions = {
        instance_id: ledger.lots
        for instance_id, ledger in sorted(engine.ledgers.items())
        if ledger.lots != 0
    }
    incomplete_reasons = {
        instance_id: tuple(ledger.incomplete_reasons)
        for instance_id, ledger in sorted(engine.ledgers.items())
        if ledger.incomplete_reasons
    }
    reconciliation = engine.reconciliation()
    failed_identities = [
        name for name in (
            "cash_matches_fill_identity", "equity_matches_gross_identity",
            "ledger_fees_match", "ledger_slip_match",
        )
        if reconciliation.get(name) is not True
    ]
    if failed_identities:
        incomplete_reasons["account"] = tuple(
            f"reconciliation_failed:{name}" for name in failed_identities
        )
    final_unrealized = engine.marks[-1].unrealized_pnl if engine.marks else Decimal("0")
    if engine.insolvent:
        run_status = "insolvent"
    elif incomplete_reasons or open_positions:
        # A terminal or data residue makes the run incomplete; it is
        # reported with open_positions/unrealized, never silently completed.
        run_status = "incomplete"
    elif engine.killed:
        run_status = "killed"
    else:
        run_status = "complete"
    return ReplayOutcome(
        status=run_status,
        incomplete_reasons=incomplete_reasons,
        final_unrealized_pnl=final_unrealized,
        fills=tuple(engine.fills),
        trades=tuple(trades),
        marks=tuple(engine.marks),
        blocked=tuple(engine.blocked),
        turnover_notional=engine.turnover_notional(),
        reconciliation=reconciliation,
        killed=engine.killed,
        insolvent=engine.insolvent,
        incomplete_contracts=incomplete,
        open_positions=open_positions,
        open_trade_count=sum(
            sum(lot.lots != 0 for lot in ledger.entry_lots)
            for ledger in engine.ledgers.values()
        ),
        roll_fill_ids=tuple(sorted(engine.roll_fill_ids)),
        roll_costs_cny=sum(
            (fill.fee_cny + fill.slippage_cost for fill in engine.fills
             if fill.fill_id in engine.roll_fill_ids), Decimal("0")
        ),
    )
