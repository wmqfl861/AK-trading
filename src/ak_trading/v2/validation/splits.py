"""Global trading-day splits with closed influence intervals and calendar embargo."""
from __future__ import annotations

from dataclasses import dataclass
from bisect import bisect_left,bisect_right
from datetime import date
from typing import Literal

from ak_trading.v2.contracts import Horizon, SessionWindow

MINUTE = 60_000_000_000


def _integer(value: object, name: str) -> None:
    if type(value) is not int:
        raise TypeError(f"{name}_must_be_int")


@dataclass(frozen=True)
class SampleInterval:
    sample_id: str
    product: str
    trading_day: date
    start_nano: int
    label_end_nano: int
    holding_end_nano: int
    available_at_nano: int
    horizon: Horizon

    def __post_init__(self) -> None:
        if not self.sample_id or not self.product or type(self.trading_day) is not date:
            raise ValueError("invalid_sample_identity")
        if not isinstance(self.horizon, Horizon):
            raise TypeError("horizon_required")
        for name in ("start_nano", "label_end_nano", "holding_end_nano", "available_at_nano"):
            _integer(getattr(self, name), name)
        if min(self.label_end_nano, self.holding_end_nano, self.available_at_nano) < self.start_nano:
            raise ValueError("influence_precedes_start")

    @property
    def influence_end(self) -> int:
        return max(self.label_end_nano, self.holding_end_nano, self.available_at_nano)


@dataclass(frozen=True)
class SplitSpec:
    split_id: str
    train_start_nano: int
    train_end_nano: int
    calibration_start_nano: int
    calibration_end_nano: int
    test_start_nano: int
    test_end_nano: int
    embargo_minutes: int
    embargo_days: int
    final_holdout: bool

    def __post_init__(self) -> None:
        bounds = (self.train_start_nano, self.train_end_nano, self.calibration_start_nano,
                  self.calibration_end_nano, self.test_start_nano, self.test_end_nano)
        for value in (*bounds, self.embargo_minutes, self.embargo_days):
            _integer(value, "split_value")
        if (not self.split_id or not bounds[0] < bounds[1] <= bounds[2] < bounds[3] <= bounds[4] < bounds[5]
                or min(self.embargo_minutes, self.embargo_days) < 0):
            raise ValueError("invalid_split_bounds")
        if type(self.final_holdout) is not bool:
            raise TypeError("final_holdout_must_be_bool")


@dataclass(frozen=True)
class SplitAssignment:
    sample_id: str
    split_id: str
    partition: Literal["TRAIN", "CALIBRATION", "TEST", "PURGED", "EMBARGOED", "UNMATURED"]
    reasons: tuple[str, ...]


def calendar_days(sessions: tuple[SessionWindow, ...]) -> tuple[tuple[date, int, int], ...]:
    """A global date has the union of every product's explicitly assigned sessions."""
    by_product: dict[str, list[SessionWindow]] = {}
    bounds: dict[date, tuple[int, int]] = {}
    ids = set()
    for session in sessions:
        if not isinstance(session, SessionWindow) or session.evidence_status == "UNKNOWN":
            raise ValueError("calendar_unknown")
        if (session.product, session.session_id) in ids:
            raise ValueError("duplicate_session")
        ids.add((session.product, session.session_id))
        if (session.end_nano - session.start_nano) % MINUTE:
            raise ValueError("session_not_minute_aligned")
        by_product.setdefault(session.product, []).append(session)
        lo, hi = bounds.get(session.trading_day, (session.start_nano, session.end_nano))
        bounds[session.trading_day] = min(lo, session.start_nano), max(hi, session.end_nano)
    for group in by_product.values():
        ordered = sorted(group, key=lambda s: s.start_nano)
        if any(a.end_nano > b.start_nano for a, b in zip(ordered, ordered[1:])):
            raise ValueError("overlapping_sessions")
    days = tuple(sorted(((day, lo, hi) for day, (lo, hi) in bounds.items()), key=lambda r: r[1]))
    if any(a[0] >= b[0] or a[2] > b[1] for a, b in zip(days, days[1:])):
        raise ValueError("ambiguous_global_trading_days")
    return days


class _CalendarIndex:
    def __init__(self,sessions):
        self.sessions=tuple(sorted(sessions,key=lambda s:s.start_nano))
        self.starts=tuple(s.start_nano for s in self.sessions)
        self.ends=tuple(s.end_nano for s in self.sessions)
        cumulative=[0]
        for s in self.sessions:cumulative.append(cumulative[-1]+s.end_nano-s.start_nano)
        self.cumulative=tuple(cumulative)

    def elapsed(self,point):
        i=bisect_right(self.ends,point)
        total=self.cumulative[i]
        if i<len(self.sessions):total+=max(0,point-self.starts[i])
        return total

    def minutes_between(self,start,end):
        return (self.elapsed(end)-self.elapsed(start)+MINUTE-1)//MINUTE


def _trading_minutes(start: int, end: int, sessions: tuple[SessionWindow, ...]) -> int:
    total = sum(max(0, min(end, s.end_nano) - max(start, s.start_nano)) for s in sessions)
    return (total + MINUTE - 1) // MINUTE


def _advance_minutes(boundary: int, count: int, sessions: tuple[SessionWindow, ...]) -> int | None:
    remaining = count * MINUTE
    for s in sorted(sessions, key=lambda s: s.start_nano):
        start = max(boundary, s.start_nano)
        span = max(0, s.end_nano - start)
        if span >= remaining and span:
            return start + remaining
        remaining -= span
    return None


def embargo_end(boundary: int, samples: tuple[SampleInterval, ...], split: SplitSpec,
                sessions: tuple[SessionWindow, ...]) -> int | None:
    """Use the longest mapped isolation across products and frequencies, never wall minutes."""
    ends = [boundary]
    for product in sorted({s.product for s in samples}):
        calendar = tuple(s for s in sessions if s.product == product)
        group = tuple(s for s in samples if s.product == product)
        minutes = tuple(s for s in group if s.horizon.unit == "TRADING_MINUTE")
        daily = tuple(s for s in group if s.horizon.unit == "TRADING_DAY")
        calendar_index=_CalendarIndex(calendar)
        if minutes:
            count = max(60, split.embargo_minutes,
                        *(calendar_index.minutes_between(s.start_nano, s.influence_end) for s in minutes))
            end = _advance_minutes(boundary, count, calendar)
            if end is None:
                return None
            ends.append(end)
        if daily:
            days = calendar_days(calendar)
            count = max(20, split.embargo_days, *(s.horizon.value for s in daily),
                        *(len({d for d, lo, hi in days if hi > s.start_nano and lo < s.influence_end})
                          for s in daily))
            future = [(lo, hi) for _, lo, hi in days if hi > boundary]
            if len(future) < count:
                return None
            ends.append(future[count - 1][1])
    return max(ends)


def assign_splits(samples: tuple[SampleInterval, ...], split: SplitSpec,
                   sessions: tuple[SessionWindow, ...]) -> tuple[SplitAssignment, ...]:
    if not isinstance(samples, tuple) or any(not isinstance(s, SampleInterval) for s in samples):
        raise TypeError("samples_must_be_tuple_of_SampleInterval")
    if len({s.sample_id for s in samples}) != len(samples):
        raise ValueError("duplicate_sample_id")
    days = {d: (lo, hi) for d, lo, hi in calendar_days(sessions)}
    ranges = (("TRAIN", split.train_start_nano, split.train_end_nano),
              ("CALIBRATION", split.calibration_start_nano, split.calibration_end_nano),
              ("TEST", split.test_start_nano, split.test_end_nano))
    cutoffs = {name: embargo_end(lo, samples, split, sessions)
               for name, lo, _ in ranges if name != "TRAIN"}
    maturity_end = max((s.end_nano for s in sessions), default=-1)
    result = []
    calendars={p:_CalendarIndex(tuple(s for s in sessions if s.product==p)) for p in {s.product for s in samples}}
    for sample in sorted(samples, key=lambda s: (s.start_nano, s.product, s.sample_id)):
        reasons: tuple[str, ...] = ()
        partition = "PURGED"
        calendar=calendars[sample.product]
        i=bisect_right(calendar.starts,sample.start_nano)-1
        # Publication/decision may occur after a session's last close. Map it
        # to the most recently started explicit trading day, never UTC date.
        valid_session=i>=0 and calendar.sessions[i].trading_day==sample.trading_day
        if sample.trading_day not in days or not valid_session:
            raise ValueError("sample_trading_day_not_in_calendar")
        day_lo, day_hi = days[sample.trading_day]
        memberships = [(n, lo, hi) for n, lo, hi in ranges if lo <= sample.start_nano < hi]
        day_partitions = [n for n, lo, hi in ranges if day_lo < hi and day_hi > lo]
        if not memberships:
            reasons = ("OUTSIDE_DECISION_PARTITIONS",)
        elif len(day_partitions) > 1:
            reasons = ("GLOBAL_TRADING_DAY_CROSSES_PARTITIONS",)
        else:
            name, lo, hi = memberships[0]
            if name != "TEST" and sample.influence_end >= hi:
                reasons = ("CLOSED_INFLUENCE_OVERLAPS_BOUNDARY",)
            elif name != "TRAIN" and (cutoffs[name] is None or day_lo < cutoffs[name]):
                partition, reasons = "EMBARGOED", ("GLOBAL_TRADING_DAY_EMBARGO",)
            elif sample.influence_end > maturity_end:
                partition, reasons = "UNMATURED", ("MATURITY_TAIL_NOT_COVERED",)
            else:
                partition = name
        result.append(SplitAssignment(sample.sample_id, split.split_id, partition, reasons))
    return tuple(result)
