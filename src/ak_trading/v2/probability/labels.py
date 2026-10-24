"""Path/range/competing/stop labels for M3A (devplan-v2.1.0 03-M3A §4.2).

``label_forecast`` is the single explicit research-scoring boundary through
which future data enters a label. It anchors every event on the decision-time
information frozen inside the ``ForecastRequest`` (reference price C, ATR,
symmetric barrier distances, frozen range, price tick metadata) and never
re-anchors on later prices. Ambiguity (same-bar double touch that finer data
cannot order) is a legal outcome category, never silently resolved in the
prediction-favourable direction.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from ak_trading.v2.contracts import (
    PERSISTENCE_EVENTS,
    ForecastRequest,
    MarketBar,
    PathLabel,
    SessionWindow,
)
from ak_trading.v2.schemas import META_TICK_FACTOR_KEY

LABEL_VERSION = "path-label-v2.1.0"
MINUTE_NANO = 60_000_000_000

PERSIST_UP_TREND = "UP_TREND"
PERSIST_DOWN_TREND = "DOWN_TREND"
PERSIST_ORDERED = "ORDERED_RANGE"
PERSIST_WIDE = "WIDE_RANGE"
PERSIST_COMPRESSION = "COMPRESSION"
PERSIST_UP_BREAKOUT = "UP_BREAKOUT"
PERSIST_DOWN_BREAKOUT = "DOWN_BREAKOUT"


class LabelError(ValueError):
    """Fixed-reason label construction violation."""


def price_tick_from_features(request: ForecastRequest) -> Decimal | None:
    """Decision-time ``META:price_tick:v2.1.0`` factor value, if published."""
    for factor in request.features.values:
        if factor.factor_key == META_TICK_FACTOR_KEY:
            value = factor.value
            if isinstance(value, Decimal) and value > 0:
                return value
            if isinstance(value, float) and value > 0:
                return Decimal(str(value))
            return None
    return None


_SESSION_STARTS_CACHE: dict[tuple[int, str], tuple[tuple[int, ...], tuple[SessionWindow, ...]]] = {}


def _product_sessions_ordered(sessions: tuple[SessionWindow, ...],
                              product: str) -> tuple[tuple[int, ...], tuple[SessionWindow, ...]]:
    """Sorted product sessions plus their start-nano bisect index (pure cache)."""
    key = (id(sessions), product)
    cached = _SESSION_STARTS_CACHE.get(key)
    if cached is None or len(cached[1]) != sum(1 for s in sessions if s.product == product):
        ordered = tuple(sorted((s for s in sessions if s.product == product),
                               key=lambda s: s.start_nano))
        cached = (tuple(s.start_nano for s in ordered), ordered)
        _SESSION_STARTS_CACHE[key] = cached
    return cached


def _expected_minute_slots(sessions: tuple[SessionWindow, ...], product: str,
                           window_start: int, window_end: int) -> tuple[tuple[int, int, int], ...]:
    """Slots as ``(primary, start_nano, end_nano)`` with primary = start_nano.

    Only sessions that can overlap ``[window_start, window_end)`` are expanded
    (bisect over sorted session starts); semantics identical to the full scan.
    """
    import bisect as _bisect

    starts, ordered = _product_sessions_ordered(sessions, product)
    import bisect as _bisect
    if not ordered:
        return ()
    span_bound = max(s.end_nano - s.start_nano for s in ordered)
    first = _bisect.bisect_left(starts, window_start - span_bound)
    slots: list[tuple[int, int, int]] = []
    for session in ordered[first:]:
        if session.start_nano >= window_end:
            break
        if session.end_nano <= window_start:
            continue
        span = session.end_nano - session.start_nano
        if span <= 0 or span % MINUTE_NANO != 0:
            raise LabelError("session_not_minute_aligned")
        count = span // MINUTE_NANO
        first_index = max(0, (window_start - session.start_nano + MINUTE_NANO - 1) // MINUTE_NANO)
        for index in range(first_index, count):
            slot_start = session.start_nano + index * MINUTE_NANO
            slot_end = slot_start + MINUTE_NANO
            if slot_start >= window_start and slot_end <= window_end:
                slots.append((slot_start, slot_start, slot_end))
            elif slot_start >= window_end:
                break
    return tuple(sorted(slots))


def _expected_day_slots(sessions: tuple[SessionWindow, ...], product: str,
                        window_start: int, window_end: int) -> tuple[tuple[date, int, int], ...]:
    """Slots as ``(primary, start_nano, end_nano)`` with primary = trading day."""
    by_day: dict[date, tuple[int, int]] = {}
    for session in sorted((s for s in sessions if s.product == product), key=lambda s: s.start_nano):
        low, high = by_day.get(session.trading_day, (session.start_nano, session.end_nano))
        by_day[session.trading_day] = (min(low, session.start_nano), max(high, session.end_nano))
    slots = [
        (day, start, end) for day, (start, end) in sorted(by_day.items())
        if start >= window_start and end <= window_end
    ]
    return tuple(slots)


def _finer_evidence(bars: tuple[MarketBar, ...], contract_id: str, base_duration: int,
                    interval: tuple[int, int], as_of_nano: int) -> tuple[MarketBar, ...] | None:
    """Valid same-contract finer historical bars covering ``interval``.

    Finer bars must be complete, visible by ``as_of_nano``, of strictly finer
    duration, and contained in the interval; 1-minute OHLC can never serve as
    finer evidence below the 1-minute base. Bars outside the interval are
    irrelevant, not violations.
    """
    usable: list[MarketBar] = []
    for bar in bars:
        if bar.contract_id != contract_id or bar.duration_seconds >= base_duration:
            continue
        if not bar.complete or bar.end_nano > as_of_nano:
            continue
        if bar.available_at_nano is None or bar.available_at_nano > as_of_nano:
            continue
        if bar.start_nano < interval[0] or bar.end_nano > interval[1]:
            continue
        if any(price is None for price in (bar.open, bar.high, bar.low, bar.close)):
            continue
        usable.append(bar)
    if not usable:
        return None
    return tuple(sorted(usable, key=lambda b: b.start_nano))


def _first_barrier_touch_in_finer(finer: tuple[MarketBar, ...], upper: Decimal,
                                  lower: Decimal) -> str | None:
    """``UP``/``DOWN`` if finer data orders a same-bar double touch, else None."""
    for bar in finer:
        open_, high, low = bar.open, bar.high, bar.low
        open_up = open_ is not None and open_ >= upper
        open_down = open_ is not None and open_ <= lower
        touch_up = open_up or (high is not None and high >= upper)
        touch_down = open_down or (low is not None and low <= lower)
        if not touch_up and not touch_down:
            continue
        if open_up and not open_down:
            return "UP"
        if open_down and not open_up:
            return "DOWN"
        if open_up and open_down:
            return None
        if touch_up and not touch_down:
            return "UP"
        if touch_down and not touch_up:
            return "DOWN"
        return None
    return None


def _first_competing_touch_in_finer(finer: tuple[MarketBar, ...], center: Decimal,
                                    outer: Decimal, upper_half: bool) -> str | None:
    """``RETURN``/``BREAK`` ordering from finer data, else None."""
    for bar in finer:
        open_, high, low = bar.open, bar.high, bar.low
        if upper_half:
            open_break = open_ is not None and open_ > outer
            open_return = open_ is not None and open_ <= center
            touch_break = open_break or (high is not None and high > outer)
            touch_return = open_return or (low is not None and low <= center)
        else:
            open_break = open_ is not None and open_ < outer
            open_return = open_ is not None and open_ >= center
            touch_break = open_break or (low is not None and low < outer)
            touch_return = open_return or (high is not None and high >= center)
        if not touch_break and not touch_return:
            continue
        if open_return and not open_break:
            return "RETURN"
        if open_break and not open_return:
            return "BREAK"
        if open_break and open_return:
            return None
        if touch_return and not touch_break:
            return "RETURN"
        if touch_break and not touch_return:
            return "BREAK"
        return None
    return None


def _build_label(request: ForecastRequest, steps: tuple[MarketBar, ...],
                 finer_bars: tuple[MarketBar, ...], available_at: int,
                 reasons: tuple[str, ...]) -> PathLabel:
    features = request.features
    reference = features.reference_price
    atr = features.atr_points
    tick = price_tick_from_features(request)
    frozen = request.frozen_range
    upper_barrier = reference + request.upper_distance
    lower_barrier = reference - request.lower_distance
    consumed_available = [available_at]

    closes = [bar.close for bar in steps]
    highs = [bar.high for bar in steps]
    lows = [bar.low for bar in steps]
    opens = [bar.open for bar in steps]
    terminal = closes[-1]
    terminal_points = terminal - reference
    terminal_return = float(terminal_points / reference)
    terminal_atr = float(terminal_points / atr)
    theta = max(Decimal(2) * tick, Decimal("0.1") * atr)
    if terminal_points > theta:
        direction = "UP"
    elif terminal_points < -theta:
        direction = "DOWN"
    else:
        direction = "NEUTRAL"

    def finer_for(index: int) -> tuple[MarketBar, ...] | None:
        return _finer_evidence(
            finer_bars, request.features.contract_id, steps[index].duration_seconds,
            (steps[index].start_nano, steps[index].end_nano), available_at)

    # --- first-touch path outcome (opening gap judged first, per 4.2.2) ---
    first_up_step: int | None = None
    first_down_step: int | None = None
    path_outcome = "NEITHER"
    for index, bar in enumerate(steps):
        step = index + 1
        open_, high, low = opens[index], highs[index], lows[index]
        open_up = open_ is not None and open_ >= upper_barrier
        open_down = open_ is not None and open_ <= lower_barrier
        touch_up = open_up or (high is not None and high >= upper_barrier)
        touch_down = open_down or (low is not None and low <= lower_barrier)
        if first_up_step is None and touch_up:
            first_up_step = step
        if first_down_step is None and touch_down:
            first_down_step = step
        if path_outcome == "NEITHER" and (touch_up or touch_down):
            if open_up and not open_down:
                path_outcome = "UP_FIRST"
            elif open_down and not open_up:
                path_outcome = "DOWN_FIRST"
            elif touch_up and not touch_down:
                path_outcome = "UP_FIRST"
            elif touch_down and not touch_up:
                path_outcome = "DOWN_FIRST"
            else:
                finer = finer_for(index)
                if finer is not None:
                    consumed_available.extend(b.available_at_nano for b in finer)
                ordered = _first_barrier_touch_in_finer(finer, upper_barrier, lower_barrier) \
                    if finer is not None else None
                path_outcome = "UP_FIRST" if ordered == "UP" else (
                    "DOWN_FIRST" if ordered == "DOWN" else "AMBIGUOUS")

    # --- competing return/break event (per 4.2.7) ---
    return_step: int | None = None
    break_step: int | None = None
    competing_outcome: str | None = None
    if frozen is None or reference == frozen.center:
        competing_outcome = None
    else:
        upper_half = reference > frozen.center
        center = frozen.center
        outer = frozen.upper if upper_half else frozen.lower
        competing_outcome = "NEITHER"
        for index, bar in enumerate(steps):
            step = index + 1
            open_, high, low = opens[index], highs[index], lows[index]
            if upper_half:
                open_break = open_ is not None and open_ > outer
                open_return = open_ is not None and open_ <= center
                touch_break = open_break or (high is not None and high > outer)
                touch_return = open_return or (low is not None and low <= center)
            else:
                open_break = open_ is not None and open_ < outer
                open_return = open_ is not None and open_ >= center
                touch_break = open_break or (low is not None and low < outer)
                touch_return = open_return or (high is not None and high >= center)
            if return_step is None and touch_return:
                return_step = step
            if break_step is None and touch_break:
                break_step = step
            if competing_outcome == "NEITHER" and (touch_return or touch_break):
                if open_return and not open_break:
                    competing_outcome = "RETURN_FIRST"
                elif open_break and not open_return:
                    competing_outcome = "BREAK_FIRST"
                elif touch_return and not touch_break:
                    competing_outcome = "RETURN_FIRST"
                elif touch_break and not touch_return:
                    competing_outcome = "BREAK_FIRST"
                else:
                    finer = finer_for(index)
                    if finer is not None:
                        consumed_available.extend(b.available_at_nano for b in finer)
                    ordered = _first_competing_touch_in_finer(
                        finer, center, outer, upper_half) if finer is not None else None
                    competing_outcome = "RETURN_FIRST" if ordered == "RETURN" else (
                        "BREAK_FIRST" if ordered == "BREAK" else "AMBIGUOUS")

    # --- persistence events (per 4.2.4/4.2.5/4.2.6/4.2.11) ---
    persistence: dict[str, bool | None] = {}
    half_atr = Decimal("0.5") * atr
    persistence[PERSIST_UP_TREND] = (
        terminal >= reference + Decimal("0.5") * atr
        and all(low is not None and low >= reference - atr for low in lows))
    persistence[PERSIST_DOWN_TREND] = (
        terminal <= reference - Decimal("0.5") * atr
        and all(high is not None and high <= reference + atr for high in highs))
    in_range = frozen is not None and frozen.lower <= reference <= frozen.upper
    if in_range:
        persistence[PERSIST_ORDERED] = all(
            low is not None and low >= frozen.lower and high is not None and high <= frozen.upper
            for low, high in zip(lows, highs))
        persistence[PERSIST_WIDE] = persistence[PERSIST_ORDERED]
    else:
        persistence[PERSIST_ORDERED] = None
        persistence[PERSIST_WIDE] = None
    compression_low, compression_high = reference - half_atr, reference + half_atr
    persistence[PERSIST_COMPRESSION] = (
        all(low is not None and low >= compression_low
            and high is not None and high <= compression_high
            for low, high in zip(lows, highs))
        and compression_low <= terminal <= compression_high)
    if frozen is not None and tick is not None:
        up_breakout_threshold = frozen.upper + tick
        down_breakout_threshold = frozen.lower - tick
        if reference > up_breakout_threshold:
            persistence[PERSIST_UP_BREAKOUT] = all(
                close is not None and close > up_breakout_threshold for close in closes)
            persistence[PERSIST_DOWN_BREAKOUT] = None
        elif reference < down_breakout_threshold:
            persistence[PERSIST_DOWN_BREAKOUT] = all(
                close is not None and close < down_breakout_threshold for close in closes)
            persistence[PERSIST_UP_BREAKOUT] = None
        else:
            persistence[PERSIST_UP_BREAKOUT] = None
            persistence[PERSIST_DOWN_BREAKOUT] = None
    else:
        persistence[PERSIST_UP_BREAKOUT] = None
        persistence[PERSIST_DOWN_BREAKOUT] = None

    # --- martingale stop-touch events (per 4.2.8) ---
    if frozen is not None:
        stop_touched: dict[str, bool | None] = {
            "LONG": any(low is not None and low <= frozen.stop_lower for low in lows),
            "SHORT": any(high is not None and high >= frozen.stop_upper for high in highs),
        }
        range_id: str | None = frozen.range_id
    else:
        stop_touched = {}
        range_id = None

    outcome_reasons = list(reasons)
    if path_outcome == "AMBIGUOUS":
        outcome_reasons.append("same_bar_double_touch_unresolved")
    if competing_outcome == "AMBIGUOUS":
        outcome_reasons.append("competing_event_unresolved")

    return PathLabel(
        request_id=request.request_id,
        contract_id=request.features.contract_id,
        label_version=LABEL_VERSION,
        horizon=request.horizon,
        window_start_nano=request.window_start_nano,
        window_end_nano=request.window_end_nano,
        available_at_nano=max(consumed_available),
        status="MATURE",
        reasons=tuple(outcome_reasons),
        direction=direction,
        terminal_return=terminal_return,
        terminal_points=terminal_points,
        terminal_atr=terminal_atr,
        path_outcome=path_outcome,
        first_up_step=first_up_step,
        first_down_step=first_down_step,
        return_step=return_step,
        break_step=break_step,
        competing_outcome=competing_outcome,
        persistence={key: persistence[key] for key in PERSISTENCE_EVENTS},
        stop_touched=stop_touched,
        range_id=range_id,
    )


def _degenerate_label(request: ForecastRequest, status: str,
                      reasons: tuple[str, ...]) -> PathLabel:
    return PathLabel(
        request_id=request.request_id,
        contract_id=request.features.contract_id,
        label_version=LABEL_VERSION,
        horizon=request.horizon,
        window_start_nano=request.window_start_nano,
        window_end_nano=request.window_end_nano,
        available_at_nano=None,
        status=status,
        reasons=reasons,
        direction=None,
        terminal_return=None,
        terminal_points=None,
        terminal_atr=None,
        path_outcome=None,
        first_up_step=None,
        first_down_step=None,
        return_step=None,
        break_step=None,
        competing_outcome=None,
        persistence={key: None for key in PERSISTENCE_EVENTS},
        stop_touched={},
        range_id=request.frozen_range.range_id if request.frozen_range is not None else None,
    )


def label_forecast(request: ForecastRequest, future_bars: tuple[MarketBar, ...],
                   sessions: tuple[SessionWindow, ...],
                   finer_bars: tuple[MarketBar, ...], as_of_nano: int) -> PathLabel:
    """Score one frozen request against its future window as of ``as_of_nano``.

    Maturity (per 4.2.9): MATURE only when every expected slot bar, the
    terminal value and any needed finer data are visible; UNMATURED while the
    window has not fully elapsed; MISSING when time has passed but expected
    bars are absent; INVALID on identity/order/price violations. Ambiguity is
    a legal MATURE outcome. Window-external appended data never changes an
    already matured label (per 4.2.10).
    """
    if not isinstance(request, ForecastRequest):
        raise TypeError("request_must_be_ForecastRequest")
    if not isinstance(future_bars, tuple) or any(not isinstance(b, MarketBar) for b in future_bars):
        raise TypeError("future_bars_must_be_tuple_of_MarketBar")
    if not isinstance(sessions, tuple) or any(not isinstance(s, SessionWindow) for s in sessions):
        raise TypeError("sessions_must_be_tuple_of_SessionWindow")
    if not isinstance(finer_bars, tuple) or any(not isinstance(b, MarketBar) for b in finer_bars):
        raise TypeError("finer_bars_must_be_tuple_of_MarketBar")
    if isinstance(as_of_nano, bool) or not isinstance(as_of_nano, int):
        raise TypeError("as_of_nano_must_be_int")

    features = request.features
    if features.reference_price is None or features.atr_points is None \
            or price_tick_from_features(request) is None:
        return _degenerate_label(request, "INVALID", ("missing_decision_anchor_metadata",))
    if features.atr_points <= 0:
        return _degenerate_label(request, "INVALID", ("atr_points_not_positive",))

    if request.horizon.unit == "TRADING_MINUTE":
        base_duration = 60
        slots: tuple[tuple[object, int, int], ...] = _expected_minute_slots(
            sessions, features.product, request.window_start_nano, request.window_end_nano)
    else:
        base_duration = 86400
        slots = _expected_day_slots(
            sessions, features.product, request.window_start_nano, request.window_end_nano)
    if len(slots) != request.horizon.value:
        return _degenerate_label(request, "INVALID", ("window_slot_count_mismatch",))

    slot_starts = {slot[1] for slot in slots}
    slot_primary = {slot[1]: slot[0] for slot in slots}
    for bar in future_bars:
        if bar.contract_id != request.features.contract_id:
            return _degenerate_label(request, "INVALID", ("bar_contract_identity_violation",))
        if bar.duration_seconds != base_duration:
            return _degenerate_label(request, "INVALID", ("future_bar_duration_mismatch",))
        if bar.start_nano < request.window_end_nano and bar.start_nano not in slot_starts:
            return _degenerate_label(request, "INVALID", ("bar_not_slot_aligned",))
    for bar in finer_bars:
        if bar.contract_id != request.features.contract_id:
            return _degenerate_label(request, "INVALID", ("bar_contract_identity_violation",))

    if request.window_end_nano > as_of_nano:
        return _degenerate_label(request, "UNMATURED", ("window_not_elapsed",))

    by_slot: dict[object, MarketBar] = {}
    for bar in sorted(future_bars, key=lambda b: b.start_nano):
        key = slot_primary.get(bar.start_nano)
        if key is None:
            continue
        if key in by_slot:
            if by_slot[key] != bar:
                return _degenerate_label(request, "INVALID", ("conflicting_duplicate_bar",))
            continue
        by_slot[key] = bar

    step_bars: list[MarketBar] = []
    missing = 0
    for slot in slots:
        bar = by_slot.get(slot[0])
        if bar is None or not bar.complete or bar.end_nano > as_of_nano \
                or bar.available_at_nano is None or bar.available_at_nano > as_of_nano \
                or any(getattr(bar, name) is None for name in ("open", "high", "low", "close")):
            missing += 1
            continue
        step_bars.append(bar)
    if missing:
        return _degenerate_label(request, "MISSING", (f"missing_future_bars:{missing}",))

    available_at = max(bar.available_at_nano for bar in step_bars)
    return _build_label(request, tuple(step_bars), finer_bars, available_at, ())
