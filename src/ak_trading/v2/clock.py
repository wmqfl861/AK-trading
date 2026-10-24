"""Trading clock: session grids, aggregation, horizon windows and holding exits.

All functions are pure and take an explicit product calendar (frozen
``SessionWindow`` tuples) plus an injected decision/assessment time in UTC
nanoseconds (devplan-v2.1.0 M1 §5). Lunch breaks, exchange sub-session
breaks, weekends and holidays are calendar gaps, never missing bars; only a
missing 1-minute slot inside a continuous fragment is ``MISSING``. Planned
grids come from the frozen calendar; natural dates are never used to guess
trading days.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Mapping

from ak_trading.v2.artifacts import sha256_hex, stable_id
from ak_trading.v2.contracts import (
    BAR_DURATIONS,
    DataQuality,
    Horizon,
    HoldingPolicy,
    MarketBar,
    SessionWindow,
)

MINUTE_NANO = 60_000_000_000
_AGGREGATE_SOURCE = "ak_trading.v2:aggregate"


class ClockError(ValueError):
    """Fixed-reason calendar/clock violation."""


def _check_sessions(sessions: tuple[SessionWindow, ...]) -> tuple[SessionWindow, ...]:
    if not isinstance(sessions, tuple) or any(not isinstance(s, SessionWindow) for s in sessions):
        raise TypeError("sessions_must_be_tuple_of_SessionWindow")
    ordered = tuple(sorted(sessions, key=lambda s: (s.product, s.start_nano, s.end_nano)))
    for previous, current in zip(ordered, ordered[1:]):
        if previous.product == current.product and current.start_nano < previous.end_nano:
            raise ClockError("overlapping_sessions_for_product")
    return ordered


def _product_sessions(sessions: tuple[SessionWindow, ...], product: str) -> tuple[SessionWindow, ...]:
    return tuple(s for s in sessions if s.product == product)


def _minute_slots(session: SessionWindow) -> tuple[tuple[int, int], ...]:
    span = session.end_nano - session.start_nano
    if span % MINUTE_NANO != 0:
        raise ClockError("session_not_minute_aligned")
    count = span // MINUTE_NANO
    return tuple(
        (session.start_nano + index * MINUTE_NANO, session.start_nano + (index + 1) * MINUTE_NANO)
        for index in range(count)
    )


def _all_minute_slots(sessions: tuple[SessionWindow, ...], product: str) -> tuple[tuple[int, int], ...]:
    slots: list[tuple[int, int]] = []
    for session in _product_sessions(sessions, product):
        slots.extend(_minute_slots(session))
    return tuple(sorted(slots))


def _trading_days(sessions: tuple[SessionWindow, ...], product: str) -> tuple[date, ...]:
    days: dict[date, int] = {}
    for session in sorted(_product_sessions(sessions, product), key=lambda s: s.start_nano):
        days.setdefault(session.trading_day, session.start_nano)
    return tuple(sorted(days.keys(), key=lambda day: days[day]))


def _containing_session(sessions: tuple[SessionWindow, ...], moment: int) -> SessionWindow | None:
    for session in sessions:
        if session.start_nano <= moment < session.end_nano:
            return session
    return None


def aggregate_bars(bars: tuple[MarketBar, ...], sessions: tuple[SessionWindow, ...],
                   duration_seconds: int, as_of_nano: int) -> tuple[MarketBar, ...]:
    """Aggregate visible 1-minute bars into ``duration_seconds`` bars.

    Anchored at each continuous session start; groups never span fragments
    and never merge with a following session. Any missing required minute
    slot inside a full-size group keeps null OHLC values with a ``MISSING``
    status; a session tail shorter than ``n`` minutes keeps its values with
    ``complete=False``. Volume/OI gaps only invalidate those fields. The
    aggregate availability time is the latest input availability time.
    """
    if not isinstance(bars, tuple) or any(not isinstance(bar, MarketBar) for bar in bars):
        raise TypeError("bars_must_be_tuple_of_MarketBar")
    if isinstance(duration_seconds, bool) or not isinstance(duration_seconds, int):
        raise TypeError("duration_seconds_must_be_int")
    if duration_seconds not in BAR_DURATIONS:
        raise ClockError("duration_seconds_must_be_60_300_900_1800_3600_86400")
    if isinstance(as_of_nano, bool) or not isinstance(as_of_nano, int):
        raise TypeError("as_of_nano_must_be_int")
    sessions = _check_sessions(sessions)
    if any(bar.duration_seconds != 60 for bar in bars):
        raise ClockError("aggregation_requires_60_second_inputs")

    # PIT first (F06): only bars visible at this as-of participate in content,
    # conflict or hash decisions; a same-identity revision that becomes visible
    # only later must not turn an earlier successful aggregation into an error.
    by_product: Mapping[str, tuple[SessionWindow, ...]] = {}
    for session in sessions:
        accumulated = by_product.get(session.product, ())
        by_product = {**by_product, session.product: accumulated + (session,)}
    visible_candidates: list[MarketBar] = []
    for bar in bars:
        if not bar.complete:
            continue
        if bar.end_nano > as_of_nano:
            continue
        if bar.available_at_nano is None or bar.available_at_nano > as_of_nano:
            continue
        # F21(r3): 60-second bars must align exactly to the planned minute
        # grid (session.start + k*60s). A bar starting off the grid is a
        # malformed source row — REJECTED, not silently skipped.
        if bar.duration_seconds == 60:
            product_sessions = by_product.get(bar.product, ())
            containing = None
            for session in product_sessions:
                if session.start_nano <= bar.start_nano < session.end_nano:
                    containing = session
                    break
            if containing is not None:
                offset = bar.start_nano - containing.start_nano
                if offset % MINUTE_NANO != 0:
                    raise ClockError("bar_off_declared_minute_grid")
            visible_candidates.append(bar)

    deduplicated: dict[tuple[str, int], MarketBar] = {}
    for bar in sorted(visible_candidates,
                       key=lambda item: (item.contract_id, item.datetime_nano)):
        key = (bar.contract_id, bar.datetime_nano)
        if key in deduplicated:
            if deduplicated[key] != bar:
                raise ClockError("conflicting_duplicate_bar")
            continue
        deduplicated[key] = bar

    visible: list[MarketBar] = []
    for bar in deduplicated.values():
        product_sessions = by_product.get(bar.product)
        if not product_sessions:
            raise ClockError("bar_product_has_no_calendar")
        if _containing_session(product_sessions, bar.start_nano) is None:
            raise ClockError("bar_outside_session_calendar")
        visible.append(bar)

    if duration_seconds == 60:
        return tuple(sorted(visible, key=lambda b: (b.contract_id, b.datetime_nano)))

    outputs: list[MarketBar] = []
    by_contract: dict[str, list[MarketBar]] = {}
    for bar in visible:
        by_contract.setdefault(bar.contract_id, []).append(bar)
    for contract_id in sorted(by_contract):
        contract_bars = sorted(by_contract[contract_id], key=lambda b: b.start_nano)
        reference = contract_bars[0]
        product_sessions = sorted(
            (s for s in sessions if s.product == reference.product), key=lambda s: s.start_nano)
        # The planned grid comes from the full declared calendar (F05): a
        # session with zero observed rows keeps its MISSING aggregates instead
        # of disappearing, and daily aggregates span every declared session of
        # the trading day, staying forming until the last one ends.
        if duration_seconds == 86400:
            for day in _trading_days(sessions, reference.product):
                day_sessions = sorted(
                    (s for s in product_sessions if s.trading_day == day),
                    key=lambda s: s.start_nano)
                if day_sessions:
                    outputs.extend(_aggregate_daily(reference, contract_bars, day_sessions, as_of_nano))
        else:
            for session in product_sessions:
                outputs.extend(_aggregate_minute_groups(
                    reference, contract_bars, session, duration_seconds, as_of_nano))
    return tuple(sorted(outputs, key=lambda b: (b.contract_id, b.start_nano)))


_QUALITY_SEVERITY = {"READY": 0, "WARMUP": 1, "DEGRADED": 2, "STALE": 3,
                     "MISSING": 4, "INVALID": 5}


def _bar_quality(bars: list[MarketBar], reasons: tuple[str, ...], status: str,
                 calendar_version: str = "", slot_positions: str = "") -> DataQuality:
    if all(bar.quality.temporal_basis == "observed" for bar in bars):
        basis = "observed"
    elif any(bar.quality.temporal_basis == "reconstructed" for bar in bars):
        basis = "reconstructed"
    else:
        basis = "unknown"
    # F18: the aggregate quality can never be better than the worst child;
    # a DEGRADED sub-bar never aggregates into a READY bar.
    child_reasons = [reason for bar in bars for reason in bar.quality.reasons
                     if reason != "missing_minute_slots"]
    effective_status = status
    if bars:
        worst_child = max(bars, key=lambda b: _QUALITY_SEVERITY.get(b.quality.status, 5))
        if _QUALITY_SEVERITY.get(worst_child.quality.status, 5) > _QUALITY_SEVERITY.get(status, 5):
            effective_status = worst_child.quality.status
    combined_reasons = tuple(dict.fromkeys((*reasons, *child_reasons))) if effective_status != "READY" else reasons
    # F19(r3): input_hash binds the full canonical content AND dependency
    # fingerprint of all consumed sub-bars — not just prices/volumes but
    # also each bar's quality hash, availability, temporal basis, imported
    # time, the session calendar version, and the planned slot positions.
    content_parts = []
    for bar in sorted(bars, key=lambda b: b.bar_id):
        content_parts.append(
            f"{bar.bar_id}|{bar.open}|{bar.high}|{bar.low}|{bar.close}|"
            f"{bar.volume}|{bar.open_oi}|{bar.close_oi}|"
            f"{bar.quality.input_hash}|{bar.available_at_nano}|"
            f"{bar.quality.temporal_basis}|{bar.imported_at_nano}|"
            f"{bar.start_nano}")
    content = "|".join(content_parts) or "no-inputs"
    if calendar_version:
        content = f"cal:{calendar_version}|{content}"
    if slot_positions:
        content = f"{content}|slots:{slot_positions}"
    return DataQuality(
        status=effective_status,
        reasons=combined_reasons,
        temporal_basis=basis,
        input_hash=sha256_hex(content.encode("utf-8")),
    )


def _combine_values(bars: list[MarketBar]) -> tuple:
    open_ = bars[0].open
    high = max((bar.high for bar in bars if bar.high is not None), default=None)
    low = min((bar.low for bar in bars if bar.low is not None), default=None)
    close = bars[-1].close
    volume = sum((bar.volume for bar in bars if bar.volume is not None), Decimal(0)) \
        if all(bar.volume is not None for bar in bars) else None
    open_oi = bars[0].open_oi if bars[0].open_oi is not None else None
    close_oi = bars[-1].close_oi
    return open_, high, low, close, volume, open_oi, close_oi


def _build_aggregate(reference: MarketBar, trading_day: date, session_id: str | None,
                     group_start: int, group_end: int, present: list[MarketBar],
                     duration_seconds: int, expected_minutes: int, group_size: int,
                     missing: bool, reasons: tuple[str, ...],
                     calendar_version: str = "",
                     slot_positions: str = "") -> MarketBar:
    all_children_ready = all(bar.quality.status == 'READY' for bar in present)
    complete = (not missing) and group_size == expected_minutes and len(present) == expected_minutes and all_children_ready
    if missing:
        open_ = high = low = close = volume = open_oi = close_oi = None
        status = "MISSING"
        quality_reasons = ("missing_minute_slots", *reasons)
    else:
        if present:
            open_, high, low, close, volume, open_oi, close_oi = _combine_values(present)
        else:
            open_ = high = low = close = volume = open_oi = close_oi = None
        status = "READY"
        quality_reasons = reasons
    available = max((bar.available_at_nano for bar in present if bar.available_at_nano is not None),
                    default=None)
    identity = (f"{reference.contract_id}|{reference.raw_symbol}|{duration_seconds}"
                f"|{group_start}|{group_end}|{session_id}")
    return MarketBar(
        bar_id=stable_id("bar", identity),
        contract_id=reference.contract_id,
        raw_symbol=reference.raw_symbol,
        product=reference.product,
        duration_seconds=duration_seconds,
        datetime_nano=group_start,
        start_nano=group_start,
        end_nano=group_end,
        trading_day=trading_day,
        session_id=session_id,
        available_at_nano=available,
        imported_at_nano=None,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        open_oi=open_oi,
        close_oi=close_oi,
        complete=complete,
        expected_minutes=expected_minutes,
        actual_minutes=len(present),
        source=_AGGREGATE_SOURCE,
        quality=_bar_quality(present, quality_reasons, status,
                             calendar_version=calendar_version,
                             slot_positions=slot_positions),
    )


def _aggregate_minute_groups(reference: MarketBar, contract_bars: list[MarketBar],
                             session: SessionWindow, duration_seconds: int,
                             as_of_nano: int) -> list[MarketBar]:
    n_minutes = duration_seconds // 60
    bars_by_start = {bar.start_nano: bar for bar in contract_bars}
    outputs: list[MarketBar] = []
    slots = _minute_slots(session)
    for group_index in range(0, len(slots), n_minutes):
        group = slots[group_index:group_index + n_minutes]
        elapsed = [slot for slot in group if slot[1] <= as_of_nano]
        if not elapsed:
            continue
        present = [bars_by_start[slot[0]] for slot in elapsed if slot[0] in bars_by_start]
        missing = any(slot[0] not in bars_by_start for slot in elapsed)
        reasons: tuple[str, ...] = ()
        if len(group) < n_minutes:
            reasons = ("session_tail_partial",)
        elif any(slot[1] > as_of_nano for slot in group):
            reasons = ("group_not_yet_elapsed",)
        outputs.append(_build_aggregate(
            reference, session.trading_day, session.session_id, group[0][0], group[-1][1],
            present, duration_seconds, n_minutes, len(group), missing, reasons,
            calendar_version=session.calendar_version,
            slot_positions=",".join(f"{s[0]}-{s[1]}" for s in group),
        ))
    return outputs


def _aggregate_daily(reference: MarketBar, contract_bars: list[MarketBar],
                     day_sessions: list[SessionWindow], as_of_nano: int) -> list[MarketBar]:
    day = day_sessions[0].trading_day
    bars = sorted((bar for bar in contract_bars
                   if (bar.trading_day if bar.trading_day is not None else day) == day),
                  key=lambda b: b.start_nano)
    slots: list[tuple[int, int]] = []
    calendar_versions = sorted({s.calendar_version for s in day_sessions})
    for fragment in day_sessions:
        slots.extend(_minute_slots(fragment))
    elapsed = [slot for slot in slots if slot[1] <= as_of_nano]
    if not elapsed:
        return []
    bars_by_start = {bar.start_nano: bar for bar in bars}
    present = [bars_by_start[slot[0]] for slot in elapsed if slot[0] in bars_by_start]
    missing = any(slot[0] not in bars_by_start for slot in elapsed)
    reasons: tuple[str, ...] = ("day_not_yet_elapsed",) if len(elapsed) < len(slots) else ()
    cal_ver = "|".join(calendar_versions)
    slot_pos = ",".join(f"{s[0]}-{s[1]}" for s in slots)
    if not present:
        # F05: zero-row declared elapsed trading day still emits its MISSING
        # daily aggregate (null prices), preserving the plan denominator.
        reasons = (*reasons, "no_inputs")
        return [_build_aggregate(
            reference, day, None, slots[0][0], slots[-1][1], present, 86400,
            len(slots), len(slots), True, reasons,
            calendar_version=cal_ver, slot_positions=slot_pos,
        )]
    return [_build_aggregate(
        reference, day, None, slots[0][0], slots[-1][1], present, 86400,
        len(slots), len(slots), missing, reasons,
        calendar_version=cal_ver, slot_positions=slot_pos,
    )]


def horizon_window(decision_nano: int, product: str, horizon: Horizon,
                   sessions: tuple[SessionWindow, ...]) -> tuple[int, int]:
    """Window of the next ``h`` not-yet-started planned trading minutes/days.

    The first slot starts strictly after ``decision_nano``; non-trading gaps
    are skipped; missing bars still occupy planned slots. Daily horizons run
    from the first session start of the next trading day, never from row
    counts.
    """
    if not isinstance(horizon, Horizon):
        raise TypeError("horizon_must_be_Horizon")
    sessions = _check_sessions(sessions)
    if horizon.unit == "TRADING_MINUTE":
        slots = _all_minute_slots(sessions, product)
        upcoming = [slot for slot in slots if slot[0] > decision_nano]
        if not upcoming:
            raise ClockError("no_future_trading_minutes")
        if len(upcoming) < horizon.value:
            raise ClockError("horizon_exceeds_calendar")
        return upcoming[0][0], upcoming[horizon.value - 1][1]
    days = _trading_days(sessions, product)
    if not days:
        raise ClockError("no_trading_days_in_calendar")
    containing = _containing_session(_product_sessions(sessions, product), decision_nano)
    if containing is not None:
        current_day = containing.trading_day
    else:
        started = [day for day in days
                   if any(s.start_nano <= decision_nano for s in _product_sessions(sessions, product)
                          if s.trading_day == day)]
        current_day = started[-1] if started else None
    if current_day is None:
        upcoming_days = [day for day in days
                         if any(s.start_nano > decision_nano
                                for s in _product_sessions(sessions, product) if s.trading_day == day)]
    else:
        upcoming_days = days[days.index(current_day) + 1:]
    if len(upcoming_days) < horizon.value:
        raise ClockError("horizon_exceeds_calendar")
    first_day, last_day = upcoming_days[0], upcoming_days[horizon.value - 1]
    first_sessions = [s for s in _product_sessions(sessions, product) if s.trading_day == first_day]
    last_sessions = [s for s in _product_sessions(sessions, product) if s.trading_day == last_day]
    window_start = min(s.start_nano for s in first_sessions)
    window_end = max(s.end_nano for s in last_sessions)
    return window_start, window_end


def holding_deadline(decision_nano: int, product: str, policy: HoldingPolicy,
                     sessions: tuple[SessionWindow, ...]) -> int:
    """Planned exit point of the allowed holding segment for ``product``.

    Minute policy: same trading day; the lunch/intra-day break only extends
    holding when ``allow_lunch``; NIGHT to DAY continuation requires
    ``allow_night_to_day``. Daily policy: the end of the last frozen session.
    """
    if not isinstance(policy, HoldingPolicy):
        raise TypeError("policy_must_be_HoldingPolicy")
    sessions = _check_sessions(sessions)
    product_sessions = sorted(_product_sessions(sessions, product), key=lambda s: s.start_nano)
    if not product_sessions:
        raise ClockError("no_sessions_for_product")
    if not policy.intraday:
        return max(s.end_nano for s in product_sessions)

    containing = _containing_session(tuple(product_sessions), decision_nano)
    if containing is not None:
        context_day = containing.trading_day
    else:
        preceding = [s for s in product_sessions if s.end_nano <= decision_nano]
        following = [s for s in product_sessions if s.start_nano > decision_nano]
        if preceding:
            context_day = max(preceding, key=lambda s: s.end_nano).trading_day
        elif following:
            context_day = following[0].trading_day
        else:
            raise ClockError("no_sessions_for_product")
    day_sessions = [s for s in product_sessions if s.trading_day == context_day]
    if containing is not None:
        anchor = containing
    else:
        later = [s for s in day_sessions if s.start_nano > decision_nano]
        earlier = [s for s in day_sessions if s.end_nano <= decision_nano]
        anchor = earlier[-1] if earlier else (later[0] if later else day_sessions[0])
    deadline = anchor.end_nano
    ordered = [s for s in day_sessions if s.start_nano >= anchor.start_nano and s is not anchor]
    previous = anchor
    for session in sorted(ordered, key=lambda s: s.start_nano):
        if previous.kind == "NIGHT" and session.kind == "DAY" and not policy.allow_night_to_day:
            break
        if not policy.allow_lunch and session.start_nano > previous.end_nano:
            break
        deadline = session.end_nano
        previous = session
    return deadline


def can_open_before_close(decision_nano: int, product: str, policy: HoldingPolicy,
                          sessions: tuple[SessionWindow, ...]) -> bool:
    """True when more than ``stop_open_before_minutes`` trading minutes remain
    between ``decision_nano`` and the allowed holding segment end."""
    deadline = holding_deadline(decision_nano, product, policy, sessions)
    remaining = sum(
        1 for slot in _all_minute_slots(_check_sessions(sessions), product)
        if decision_nano < slot[1] <= deadline
    )
    return remaining > policy.stop_open_before_minutes
