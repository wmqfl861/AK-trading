"""Legacy trading-clock oracle: frozen pre-K10 implementation bodies.

Verbatim copies of ``horizon_window`` / ``holding_deadline`` /
``can_open_before_close`` and the original private helpers they use, exactly
as they stood before the K10-core internal-calendar-index change. This module
is differential-test reference material only: product code must not import
it, and it must not borrow any new index or query helper from the changed
``ak_trading.v2.clock``.

Provenance: repository base ``c608d20791792a4838f6f3e001dd436ad2f8faba``,
``src/ak_trading/v2/clock.py`` SHA-256
``793771e4633a92398a513983ea3f62713ce6c9f2ab10a4e49df4000eda77660b``
(read-only copy at ``C:/AKR51111/before/src/ak_trading/v2/clock.py``).
Only the import block was adjusted: the frozen contract classes and the
unchanged legacy ``ClockError`` are imported from the live package so
differential comparisons see identical exception types.
"""
from __future__ import annotations

from ak_trading.v2.clock import ClockError
from ak_trading.v2.contracts import Horizon, HoldingPolicy, SessionWindow

MINUTE_NANO = 60_000_000_000


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
