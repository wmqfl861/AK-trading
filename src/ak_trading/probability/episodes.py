"""Independent, flat-to-flat probability research opportunities.

An episode covers stored observations i+1..i+h with fresh initial cash. Raw
bar identities order assumed fills; no session, live order or historical fee
certification is inferred. Callers own partitioning and must not score the
zero placeholders of invalid/unmatured outcomes as realized cash returns.

Eligibility is frozen at i: breakout requires close strictly above prior_high20
and MA20>MA60 (long), or strictly below prior_low20 and MA20<MA60 (short).
Mean reversion requires current regime exactly range/transition and z<=-2
(long) or z>=2 (short). These regime names are the factor producer's ER/MA
heuristic, never a future label. Pullback requires uptrend/downtrend and the
first future completed close crossing toward a frozen level: MA20 when on the
retracement side, otherwise close-direction*pullback_atr*ATR. It enters only at
the following open. Grid is eligible for any ready row; its frozen center and
ATR spacing map completed-close distance to signed, capped next-open lots.

Ordinary brackets use actual entry and frozen ATR, gap-open precedence,
stop-first ambiguous OHLC, and target-price-only favorable gaps. Grid's global
limits disable all later entries; a same-bar dual limit closes at the adverse
limit for actual inventory. Final-close liquidation is precommitted. Fees use
flat plus ideal-notional rate on every leg; slippage is a separate cash charge.
Margin admission includes estimated opening costs and current marked equity,
reducing new lots as needed. max_margin samples held inventory at opens,
completed closes while held, and fills, not an inferred intrabar peak.
Nonpositive observed equity is liquidated at that open/close; negative cash is
retained, without an invented maintenance-margin threshold or cash top-up.

A fully priced opportunity is complete even if no signal, margin-blocked, or
insolvent. Its reason distinguishes cash, no_trade:*, margin_blocked, target,
stop, target_gap, stop_gap, grid_stop[_gap], terminal_close and insolvency exits.
Traded margin reductions append :margin_limited; observed insolvency prefixes
insolvent:. terminal_nano always denotes i+h, even for earlier exits. Grid has
direction=0; ordinary direction records the eligible side even without a fill.
"""
from __future__ import annotations

from collections.abc import Sequence
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext

from ak_trading.futures.scenario_backtest import ScenarioConfig
from ak_trading.futures.spec import ContractBar
from ak_trading.probability.contracts import (
    ACTIONS, ActionOutcome, ActionSpec, FactorRow, validate_bar,
)

__all__ = ["action_specs", "simulate_action"]

D = Decimal
_ARITHMETIC = Context(prec=128, rounding=ROUND_HALF_EVEN)
_ZERO = D(0)


def action_specs(horizon: int = 5) -> tuple[ActionSpec, ...]:
    """The five actions in stable order with the shared fixed defaults."""
    return tuple(ActionSpec(name=name, horizon=horizon) for name in ACTIONS)


def simulate_action(
    observation: FactorRow,
    bars: Sequence[ContractBar],
    spec: ActionSpec,
    economics: ScenarioConfig,
) -> ActionOutcome:
    """Simulate one opportunity using only current factors for eligibility."""
    if not isinstance(economics, ScenarioConfig):
        raise ValueError("economics must be a validated ScenarioConfig")
    with localcontext(_ARITHMETIC):
        problem = _window_problem(observation, bars, spec)
        if problem is not None:
            return _empty(observation, spec, *problem)
        terminal = bars[observation.index + spec.horizon].datetime_nano
        if spec.name == "cash":
            return _empty(observation, spec, "complete", "cash", terminal)
        values, problem = _factors(observation, spec)
        if problem:
            return _empty(observation, spec, "invalid", problem)
        if spec.name == "grid":
            return _grid_episode(observation, bars, spec, economics, values, terminal)
        direction, reason = _eligibility(observation, spec, values)
        if not direction:
            return _empty(observation, spec, "complete", reason, terminal)
        book = _Book(economics, observation.index)
        end = observation.index + spec.horizon
        entry_index = observation.index + 1
        if spec.name == "pullback":
            level = _pullback_level(observation, spec, values, direction)
            previous = values["close"]
            for signal_index in range(observation.index + 1, end + 1):
                close = bars[signal_index].close
                crossed = (direction * (previous - level) > 0
                           and direction * (close - level) <= 0)
                if crossed:
                    if signal_index == end:
                        return _empty(observation, spec, "complete",
                                      "no_trade:pullback_confirmed_at_terminal", terminal, direction)
                    entry_index = signal_index + 1
                    break
                previous = close
            else:
                return _empty(observation, spec, "complete",
                              "no_trade:pullback_not_reached", terminal, direction)
        reason = "terminal_close"
        entry = bars[entry_index].open
        target = entry + direction * D(str(spec.target_atr)) * values["atr14"]
        stop = entry - direction * D(str(spec.stop_atr)) * values["atr14"]
        for index in range(entry_index, end + 1):
            bar = bars[index]
            if index == entry_index:
                admitted = book.admit(bar.open, economics.lots)
                if not admitted:
                    return _empty(observation, spec, "complete", "margin_blocked", terminal, direction)
                book.fill(index, bar, direction, admitted, bar.open,
                          "open", "next_open", entry_index - 1)
            equity_at_open = book.mark(bar.open)
            hit = _bracket(bar, direction, target, stop)
            if hit is not None and hit[0].endswith("_gap"):
                reason, price, ambiguous = hit
            elif equity_at_open <= 0:
                reason, price = "insolvency_open", bar.open
            elif hit is not None:
                reason, price, ambiguous = hit
                book.ambiguous += ambiguous
            elif book.mark(bar.close) <= 0:
                reason, price = "insolvency_close", bar.close
            elif index == end:
                reason, price = "terminal_close", bar.close
            else:
                continue
            book.fill(index, bar, -direction, abs(book.position), price, "close", reason)
            break
        return book.outcome(observation, spec, terminal, direction, reason)


def _grid_episode(observation, bars, spec, economics, values, terminal):
    book = _Book(economics, observation.index)
    center = values["close"]
    spacing = D(str(spec.grid_step_atr)) * values["atr14"]
    stop_distance = D(str(spec.grid_stop_atr)) * values["atr14"]
    lower, upper = center - stop_distance, center + stop_distance
    end = observation.index + spec.horizon
    pending = 0  # The decision close is exactly the frozen center.
    reason = "terminal_close"
    for index in range(observation.index + 1, end + 1):
        bar = bars[index]
        equity = book.mark(bar.open)
        # A known gap stop cancels pending openings and reduces existing lots
        # immediately. Future high/low is inspected only after open execution.
        if bar.open <= lower or bar.open >= upper:
            reason = "grid_stop_gap"
            _flatten(book, index, bar, bar.open, reason)
            break
        if equity <= 0:
            reason = "insolvency_open"
            _flatten(book, index, bar, bar.open, reason)
            break
        _rebalance(book, index, bar, pending)
        if book.insolvent:
            reason = "insolvency_open"
            _flatten(book, index, bar, bar.open, reason)
            break
        lower_hit, upper_hit = bar.low <= lower, bar.high >= upper
        if lower_hit or upper_hit:
            if lower_hit and upper_hit:
                # OHLC cannot order the two global limits. Choose the adverse
                # one for the actual post-open inventory; never add inventory.
                price = upper if book.position < 0 else lower
                book.ambiguous += int(bool(book.position))
            else:
                price = lower if lower_hit else upper
            reason = "grid_stop"
            _flatten(book, index, bar, price, reason)
            break
        equity = book.mark(bar.close)
        if equity <= 0:
            reason = "insolvency_close"
            _flatten(book, index, bar, bar.close, reason)
            break
        distance = center - bar.close
        # Floor absolute distance before attaching direction: truncating a
        # signed negative quotient would otherwise create asymmetric grids.
        levels = min(spec.grid_levels, int(abs(distance) // spacing))
        if index == end:
            _flatten(book, index, bar, bar.close, "terminal_close")
            if not book.fills:
                reason = "margin_blocked" if book.margin_limited else (
                    "no_trade:grid_confirmed_at_terminal" if levels
                    else "no_trade:grid_level_not_reached"
                )
            break
        pending = (1 if distance > 0 else -1) * levels * economics.lots
    if not book.fills and reason.startswith("grid_stop"):
        reason = "no_trade:" + reason
    return book.outcome(observation, spec, terminal, 0, reason)


def _flatten(book, index, bar, price, phase):
    if book.position:
        side = -1 if book.position > 0 else 1
        book.fill(index, bar, side, abs(book.position), price, "close", phase)


def _rebalance(book, index, bar, target):
    position = book.position
    if position and (target * position <= 0 or abs(target) < abs(position)):
        reducing = abs(position) if target * position <= 0 else abs(position) - abs(target)
        side = -1 if position > 0 else 1
        book.fill(index, bar, side, reducing, bar.open, "close", "next_open", index - 1)
    additional = abs(target) - abs(book.position)
    if additional > 0:
        admitted = book.admit(bar.open, additional)
        if admitted:
            book.fill(index, bar, 1 if target > 0 else -1, admitted, bar.open,
                      "open", "next_open", index - 1)


def _factors(observation, spec):
    names = ("close", "atr14") + {
        "breakout": ("prior_high20", "prior_low20", "ma20", "ma60"),
        "mean_reversion": ("zscore20",),
        "pullback": (), "grid": (),
    }[spec.name]
    values = {}
    for name in names:
        value = observation.values.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float, D)):
            return {}, f"invalid_factor:{name}"
        value = D(str(value))
        if not value.is_finite() or (name != "zscore20" and value <= 0):
            return {}, f"invalid_factor:{name}"
        values[name] = value
    if (spec.name in ("mean_reversion", "pullback")
            and observation.regime not in ("range", "transition", "uptrend", "downtrend")):
        return {}, "invalid_factor:regime"
    return values, None


def _eligibility(observation, spec, values):
    if spec.name == "breakout":
        if values["close"] > values["prior_high20"] and values["ma20"] > values["ma60"]:
            return 1, "breakout_long"
        if values["close"] < values["prior_low20"] and values["ma20"] < values["ma60"]:
            return -1, "breakout_short"
        return 0, "no_trade:breakout_not_confirmed"
    if spec.name == "mean_reversion":
        if observation.regime not in ("range", "transition"):
            return 0, "no_trade:regime_not_range_or_transition"
        if values["zscore20"] <= -2:
            return 1, "mean_reversion_long"
        if values["zscore20"] >= 2:
            return -1, "mean_reversion_short"
        return 0, "no_trade:zscore_inside_threshold"
    if spec.name == "pullback":
        if observation.regime == "uptrend":
            return 1, "pullback_long"
        if observation.regime == "downtrend":
            return -1, "pullback_short"
    return 0, "no_trade:regime_not_trend"


def _pullback_level(observation, spec, values, direction):
    # A current MA must lie strictly on the retracement side. If unavailable
    # or already behind the price in the wrong direction, use the fixed ATR
    # offset instead. Neither threshold is updated from future observations.
    raw = observation.values.get("ma20")
    if not isinstance(raw, bool) and isinstance(raw, (int, float, D)):
        ma = D(str(raw))
        if ma.is_finite() and ma > 0 and direction * (values["close"] - ma) > 0:
            return ma
    return values["close"] - direction * D(str(spec.pullback_atr)) * values["atr14"]


def _bracket(bar, direction, target, stop):
    stop_open = bar.open <= stop if direction == 1 else bar.open >= stop
    target_open = bar.open >= target if direction == 1 else bar.open <= target
    if stop_open:
        return "stop_gap", bar.open, 0
    if target_open:
        # Favorable gaps get only the target, an explicitly conservative fill.
        return "target_gap", target, 0
    stop_hit = bar.low <= stop if direction == 1 else bar.high >= stop
    target_hit = bar.high >= target if direction == 1 else bar.low <= target
    if stop_hit:
        return "stop", stop, int(target_hit)
    if target_hit:
        return "target", target, 0
    return None


class _Book:
    """Small FIFO lot book; ideal PnL and separately charged costs."""

    def __init__(self, economics: ScenarioConfig, start: int):
        self.economics = economics
        self.start = start
        self.cash = economics.initial_cash
        self.gross = self.fees = self.slip = self.max_margin = _ZERO
        self.position = 0
        self.entries: list[tuple[Decimal, int]] = []
        self.fills: list[dict[str, object]] = []
        self.ambiguous = 0
        self.margin_limited = False
        self.insolvent = False

    def admit(self, price, requested):
        # Integer binary search avoids fractional lots and remains bounded even
        # at the shared million-lot maximum. Include unrealized equity of held
        # lots and all estimated costs of only the new opening leg.
        cfg = self.economics
        equity = self.mark(price)
        low, high = 0, requested
        while low < high:
            quantity = (low + high + 1) // 2
            flat, rate, slip = self.costs(price, quantity)
            after_cost = equity - flat - rate - slip
            required = (abs(self.position) + quantity) * price * cfg.multiplier * cfg.margin_rate
            if after_cost > 0 and required <= after_cost * cfg.max_margin_ratio:
                low = quantity
            else:
                high = quantity - 1
        self.margin_limited |= low < requested
        return low

    def costs(self, price, lots):
        cfg = self.economics
        flat = cfg.fee_per_lot * lots
        rate = cfg.fee_rate * abs(price * cfg.multiplier * lots)
        slip = cfg.slippage_ticks * cfg.price_tick * cfg.multiplier * lots
        return flat, rate, slip

    def mark(self, price):
        cfg = self.economics
        margin = abs(self.position) * price * cfg.multiplier * cfg.margin_rate
        self.max_margin = max(self.max_margin, margin)
        direction = 1 if self.position > 0 else -1
        unrealized = sum(((price - entry) * lots * direction * cfg.multiplier
                          for entry, lots in self.entries), _ZERO)
        equity = self.cash + unrealized
        self.insolvent |= equity <= 0
        return equity

    def fill(self, index, bar, side, lots, price, offset, phase, signal_index=None):
        self.mark(price)
        gross = _ZERO
        if offset == "close":
            remaining = lots
            while remaining:
                entry, quantity = self.entries[0]
                closing = min(remaining, quantity)
                gross += (price - entry) * (-side) * closing * self.economics.multiplier
                remaining -= closing
                if closing == quantity:
                    self.entries.pop(0)
                else:
                    self.entries[0] = entry, quantity - closing
        else:
            self.entries.append((price, lots))
        self.position += side * lots
        flat, rate, slip = self.costs(price, lots)
        self.gross += gross
        self.fees += flat + rate
        self.slip += slip
        self.cash = self.economics.initial_cash + self.gross - self.fees - self.slip
        self.mark(price)
        self.fills.append({
            "step": index - self.start, "index": index,
            "datetime_nano": bar.datetime_nano, "raw_ts": bar.ts.isoformat(),
            "signal_index": signal_index, "phase": phase, "offset": offset,
            "side": side, "lots": lots, "ideal_price": price,
            "fee_per_lot_component": flat, "fee_rate_component": rate,
            "fee": flat + rate, "slippage": slip, "gross_pnl": gross,
            "cash_after": self.cash, "position_after": self.position,
        })

    def outcome(self, observation, spec, terminal, direction, reason):
        if self.position or self.entries:
            raise ArithmeticError("episode must finish flat")
        if self.margin_limited and self.fills:
            reason += ":margin_limited"
        if self.insolvent:
            reason = "insolvent:" + reason
        return ActionOutcome(
            event_id=observation.event_id, action_id=spec.action_id,
            contract_id=observation.contract_id, index=observation.index,
            terminal_nano=terminal, status="complete", reason=reason,
            traded=bool(self.fills), direction=direction,
            gross_pnl=self.gross, fees=self.fees, slippage=self.slip,
            net_pnl=self.gross - self.fees - self.slip,
            max_margin=self.max_margin, ambiguous_bars=self.ambiguous,
            fills=tuple(self.fills),
        )


def _empty(observation, spec, status, reason, terminal_nano=None, direction=0):
    return ActionOutcome(
        event_id=observation.event_id, action_id=spec.action_id,
        contract_id=observation.contract_id, index=observation.index,
        terminal_nano=terminal_nano, status=status, reason=reason,
        traded=False, direction=direction, gross_pnl=_ZERO, fees=_ZERO,
        slippage=_ZERO, net_pnl=_ZERO, max_margin=_ZERO, ambiguous_bars=0,
    )


def _window_problem(observation, bars, spec):
    start = observation.index
    if isinstance(start, bool) or not isinstance(start, int) or not 0 <= start < len(bars):
        return "invalid", "invalid_observation_index"
    if observation.status != "ready":
        return "invalid", f"observation_not_ready:{observation.reason or observation.status}"
    if bars[start].datetime_nano != observation.timestamp_nano:
        return "invalid", "observation_identity_mismatch"
    end = start + spec.horizon
    previous = None
    # Preserve row offsets and inspect even trailing rows after an early exit.
    # Invalid known rows take precedence over an otherwise unmatured tail.
    for index in range(start, min(end + 1, len(bars))):
        bar = bars[index]
        if bar.symbol != observation.symbol:
            return "invalid", f"contract_mismatch:{index}"
        nano = bar.datetime_nano
        if (isinstance(nano, bool) or not isinstance(nano, int) or nano <= 0
                or (previous is not None and nano <= previous)):
            return "invalid", f"invalid_timestamp:{index}"
        previous = nano
        if not validate_bar(bar):
            return "invalid", f"invalid_bar:{index}"
    if end >= len(bars):
        return "unmatured", "insufficient_future_bars"
    return None
