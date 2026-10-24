"""M1-02 clock: sessions, night trading days, horizons, aggregation, exits."""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from ak_trading.v2 import contracts as c
from ak_trading.v2 import clock as k
from ak_trading.v2 import schemas as s

M = 60_000_000_000
H = 3_600_000_000_000
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "v2" / "shared.json"
CALENDAR = "synthetic-calendar-v2.1.0"


def unano(moment: datetime) -> int:
    delta = moment.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 10**9


def cst(day: int, hour: int, minute: int = 0, second: int = 0) -> int:
    return unano(datetime(2026, 1, day, hour, minute, second, tzinfo=timezone.utc)
                 - timedelta(hours=8))


def quality() -> c.DataQuality:
    return c.DataQuality(status="READY", reasons=(), temporal_basis="observed",
                         input_hash=hashlib.sha256(b"clock-test").hexdigest())


def load_fixture() -> dict:
    return s.decode_shared_fixture(FIXTURE.read_bytes())


def minute_bar(index: int, start: int, *, ohlc: tuple[int, int, int, int], volume: int,
               oi_open: int, oi_close: int, session_id: str = "D09-0") -> c.MarketBar:
    o, h, l, cl = ohlc
    return c.MarketBar(
        bar_id=f"bar-{index}", contract_id="SYN.rb2609", raw_symbol="SYN.rb2609",
        product="SYN.rb", duration_seconds=60, datetime_nano=start, start_nano=start,
        end_nano=start + M, trading_day=date(2026, 1, 9), session_id=session_id,
        available_at_nano=start + M + M // 60, imported_at_nano=start + M,
        open=Decimal(o), high=Decimal(h), low=Decimal(l), close=Decimal(cl),
        volume=Decimal(volume), open_oi=Decimal(oi_open), close_oi=Decimal(oi_close),
        complete=True, expected_minutes=1, actual_minutes=1, source="clock-test",
        quality=quality())


PATTERN = [(100, 102, 99, 101), (101, 104, 100, 103), (103, 103, 100, 101),
           (101, 105, 101, 104), (104, 106, 103, 105)]


def day_sessions(day: int, product: str = "SYN.rb", version: str = CALENDAR,
                 evidence: str = "SCENARIO") -> tuple[c.SessionWindow, ...]:
    spans = [(9, 0, 10, 15), (10, 30, 11, 30), (13, 30, 15, 0)]
    return tuple(
        c.SessionWindow(session_id=f"D{day:02d}-{idx}", product=product,
                        trading_day=date(2026, 1, day), start_nano=cst(day, a, b),
                        end_nano=cst(day, x, y), kind="DAY", calendar_version=version,
                        evidence_status=evidence)
        for idx, (a, b, x, y) in enumerate(spans))


def night_session(evening: int, target: int, product: str = "SYN.rb") -> c.SessionWindow:
    return c.SessionWindow(
        session_id=f"N{target:02d}", product=product, trading_day=date(2026, 1, target),
        start_nano=cst(evening, 21), end_nano=cst(evening, 23), kind="NIGHT",
        calendar_version=CALENDAR, evidence_status="SCENARIO")


@pytest.mark.parametrize("minutes,repeat", [(5, 1), (15, 3), (30, 6), (60, 12)])
def test_hand_aggregation_matches_spec(minutes: int, repeat: int) -> None:
    session = c.SessionWindow(
        session_id="AGG", product="SYN.rb", trading_day=date(2026, 1, 9),
        start_nano=cst(9, 9), end_nano=cst(9, 9) + 5 * repeat * M, kind="DAY",
        calendar_version=CALENDAR, evidence_status="SCENARIO")
    bars: list[c.MarketBar] = []
    for block in range(repeat):
        for i, (o, h, l, cl) in enumerate(PATTERN):
            start = cst(9, 9) + (block * 5 + i) * M
            bars.append(minute_bar(block * 5 + i, start, ohlc=(o, h, l, cl),
                                   volume=i + 1, oi_open=20 + i, oi_close=21 + i,
                                   session_id="AGG"))
    as_of = cst(9, 9) + 5 * repeat * M + 5 * M
    aggregated = k.aggregate_bars(tuple(bars), (session,), minutes * 60, as_of)
    assert len(aggregated) == 5 * repeat // minutes
    for bar in aggregated:
        assert bar.open == Decimal("100")
        assert bar.high == Decimal("106")
        assert bar.low == Decimal("99")
        assert bar.close == Decimal("105")
        assert bar.volume == Decimal(15 * repeat)
        assert bar.open_oi == Decimal("20")
        assert bar.close_oi == Decimal("25")
        assert bar.complete is True
        assert bar.expected_minutes == minutes
        assert bar.actual_minutes == minutes
        assert bar.available_at_nano == max(b.available_at_nano for b in bars)


def test_minute_and_daily_horizon_windows() -> None:
    data = load_fixture()
    sessions = data["sessions"]
    # Lunch break: 11:29:59 -> first not-yet-started slot is 13:30, 5m end 13:35.
    assert k.horizon_window(cst(9, 11, 29, 59), "SYN.rb",
                            c.Horizon(unit="TRADING_MINUTE", value=5), sessions) \
        == (cst(9, 13, 30), cst(9, 13, 35))
    # The lunch gap is calendar, never counted as 120 missing minute bars.
    one = k.horizon_window(cst(9, 11, 29, 59), "SYN.rb",
                           c.Horizon(unit="TRADING_MINUTE", value=1), sessions)
    assert one == (cst(9, 13, 30), cst(9, 13, 31))
    # All five minute horizons from a normal morning decision point.
    # Hand-computed across the 10:15-10:30 exchange break: h=1/5 stay inside
    # the first fragment (ends 10:02/10:06); h=15 needs 14 slots to 10:15 plus
    # the 10:30 slot (ends 10:31); h=30 ends at the 10:45-10:46 slot; h=60 ends
    # at the 11:15-11:16 slot.
    for h, expected_end in ((1, cst(9, 10, 2)), (5, cst(9, 10, 6)), (15, cst(9, 10, 31)),
                            (30, cst(9, 10, 46)), (60, cst(9, 11, 16))):
        start, end = k.horizon_window(cst(9, 10, 0), "SYN.rb",
                                      c.Horizon(unit="TRADING_MINUTE", value=h), sessions)
        assert start == cst(9, 10, 1)
        assert end == expected_end
    # Daily horizons: next trading day full sessions, from the calendar not row counts.
    start1, end1 = k.horizon_window(cst(9, 10, 0), "SYN.rb",
                                    c.Horizon(unit="TRADING_DAY", value=1), sessions)
    assert start1 == cst(9, 21)  # the 2026-01-12 trading day begins with its night session
    assert end1 == cst(12, 15)
    start5, end5 = k.horizon_window(cst(9, 10, 0), "SYN.rb",
                                    c.Horizon(unit="TRADING_DAY", value=5), sessions)
    assert start5 == cst(9, 21)
    assert end5 == cst(16, 15)
    with pytest.raises(k.ClockError):
        k.horizon_window(cst(9, 10, 0), "SYN.rb",
                         c.Horizon(unit="TRADING_DAY", value=20), sessions)
    # 20-day horizon over an extended local calendar (next ~30 weekdays).
    def at(day_date: date, hour: int, minute: int = 0) -> int:
        moment = datetime(day_date.year, day_date.month, day_date.day, hour, minute,
                          tzinfo=timezone.utc) - timedelta(hours=8)
        return unano(moment)

    extended = list(sessions)
    cursor = date(2026, 1, 19)
    added: list[date] = []
    while len(added) < 30:
        if cursor.weekday() < 5:
            added.append(cursor)
        cursor += timedelta(days=1)
    for index, day_date in enumerate(added):
        for fragment_index, (a, b, x, y) in enumerate(
                [(9, 0, 10, 15), (10, 30, 11, 30), (13, 30, 15, 0)]):
            extended.append(c.SessionWindow(
                session_id=f"X{day_date.isoformat()}-{fragment_index}", product="SYN.rb",
                trading_day=day_date, start_nano=at(day_date, a, b), end_nano=at(day_date, x, y),
                kind="DAY", calendar_version=CALENDAR, evidence_status="SCENARIO"))
        if index > 0:
            previous = added[index - 1]
            extended.append(c.SessionWindow(
                session_id=f"XN{day_date.isoformat()}", product="SYN.rb",
                trading_day=day_date, start_nano=at(previous, 21), end_nano=at(previous, 23),
                kind="NIGHT", calendar_version=CALENDAR, evidence_status="SCENARIO"))
    extended_days = sorted({w.trading_day for w in extended})
    start20, end20 = k.horizon_window(cst(9, 10, 0), "SYN.rb",
                                      c.Horizon(unit="TRADING_DAY", value=20),
                                      tuple(extended))
    assert start20 == cst(9, 21)
    twentieth = extended_days[extended_days.index(date(2026, 1, 9)) + 20]
    assert end20 == max(w.end_nano for w in extended if w.trading_day == twentieth)


def test_lunch_break_and_tail_segments() -> None:
    data = load_fixture()
    sessions = data["sessions"]
    # 11:28:59 + 5 slots spans the lunch break; the window crosses it...
    start, end = k.horizon_window(cst(9, 11, 28, 59), "SYN.rb",
                                  c.Horizon(unit="TRADING_MINUTE", value=5), sessions)
    assert start == cst(9, 11, 29)
    assert end == cst(9, 13, 34)
    # ...but aggregated bars never cross a fragment boundary (10:15 or lunch).
    day9 = tuple(w for w in sessions if w.trading_day == date(2026, 1, 9) and w.kind == "DAY")
    assert len(day9) == 3
    bars: list[c.MarketBar] = []
    index = 0
    for fragment in day9:
        span = (fragment.end_nano - fragment.start_nano) // M
        for i in range(span):
            bars.append(minute_bar(index, fragment.start_nano + i * M,
                                   ohlc=(100, 101, 99, 100), volume=1, oi_open=20,
                                   oi_close=21, session_id=fragment.session_id))
            index += 1
    aggregated = k.aggregate_bars(tuple(bars), day9, 3600, cst(9, 15) + M)
    by_start = {bar.start_nano: bar for bar in aggregated}
    # fragment 09:00-10:15: one complete 60m bar plus a 15m tail.
    assert by_start[cst(9, 9)].complete is True
    assert (by_start[cst(9, 10)].complete, by_start[cst(9, 10)].actual_minutes) == (False, 15)
    # fragment 10:30-11:30: exactly one complete bar that stops at the lunch start.
    lunch_bar = by_start[cst(9, 10, 30)]
    assert lunch_bar.complete is True and lunch_bar.end_nano == cst(9, 11, 30)
    # fragment 13:30-15:00: complete bar plus a 30m tail.
    assert by_start[cst(9, 13, 30)].complete is True
    assert by_start[cst(9, 14, 30)].actual_minutes == 30
    # No aggregated bar spans the lunch gap.
    for bar in aggregated:
        assert not (bar.start_nano < cst(9, 11, 30) < bar.end_nano)
        assert not (bar.start_nano < cst(9, 10, 15) < bar.end_nano)
    # A 75-minute fragment: full 60-minute bar + 15-minute tail that never merges.
    short = c.SessionWindow(
        session_id="S75", product="SYN.rb", trading_day=date(2026, 1, 9),
        start_nano=cst(9, 9), end_nano=cst(9, 9) + 75 * M, kind="DAY",
        calendar_version=CALENDAR, evidence_status="SCENARIO")
    bars75 = [minute_bar(i, cst(9, 9) + i * M, ohlc=(100, 101, 99, 100), volume=1,
                         oi_open=20, oi_close=21, session_id="S75") for i in range(75)]
    tail_alone = k.aggregate_bars(tuple(bars75), (short,), 3600, cst(9, 9) + 80 * M)
    assert [(b.complete, b.expected_minutes, b.actual_minutes) for b in tail_alone] == [
        (True, 60, 60), (False, 60, 15)]
    following = c.SessionWindow(
        session_id="NEXT", product="SYN.rb", trading_day=date(2026, 1, 12),
        start_nano=cst(12, 21), end_nano=cst(12, 23), kind="NIGHT",
        calendar_version=CALENDAR, evidence_status="SCENARIO")
    bars_next = [minute_bar(100 + i, cst(12, 21) + i * M, ohlc=(100, 101, 99, 100),
                            volume=1, oi_open=20, oi_close=21, session_id="NEXT")
                 for i in range(120)]
    combined = k.aggregate_bars(tuple(bars75) + tuple(bars_next), (short, following), 3600,
                                cst(12, 23) + M)
    tail = [b for b in combined if b.start_nano == cst(9, 9) + 60 * M][0]
    assert tail.complete is False and tail.actual_minutes == 15
    assert tail.end_nano == cst(9, 9) + 75 * M


def test_night_session_trading_day_and_holiday_gap() -> None:
    data = load_fixture()
    sessions = data["sessions"]
    night = [w for w in sessions if w.session_id == "N12"][0]
    # The fixture states explicitly: the 2026-01-09 night session belongs to 2026-01-12.
    assert night.start_nano == cst(9, 21)
    assert night.trading_day == date(2026, 1, 12)
    # Holding through midnight keeps the same trading day for loss resets.
    policy = c.HoldingPolicy(True, True, False, 30, 5)
    assert k.holding_deadline(cst(9, 21, 30), "SYN.rb", policy, sessions) == cst(9, 23)
    assert k.holding_deadline(cst(9, 22, 30), "SYN.rb", policy, sessions) == cst(9, 23)
    # Calendar hole: a bar outside any session is rejected, never guessed.
    orphan = minute_bar(0, cst(9, 20), ohlc=(100, 101, 99, 100), volume=1, oi_open=20,
                        oi_close=21, session_id=None)
    with pytest.raises(k.ClockError):
        k.aggregate_bars((orphan,), sessions, 300, cst(9, 20) + 5 * M)
    # No-night product: a calendar without any NIGHT windows still works.
    no_night = tuple(w for w in sessions if w.kind == "DAY")
    assert k.holding_deadline(cst(9, 10), "SYN.rb", policy, no_night) == cst(9, 15)
    # Pre-holiday night-stop: Friday evening has no further night fragment after 23:00.
    holiday_gap = [w for w in sessions if w.trading_day == date(2026, 1, 12)
                   and w.kind == "NIGHT"]
    assert holiday_gap[0].end_nano == cst(9, 23)
    # Cross-year session crossing midnight in one fragment.
    new_year = c.SessionWindow(
        session_id="NY", product="SYN.rb", trading_day=date(2025, 12, 31),
        start_nano=unano(datetime(2025, 12, 31, 21, tzinfo=timezone.utc) - timedelta(hours=8)),
        end_nano=unano(datetime(2026, 1, 1, 1, tzinfo=timezone.utc) - timedelta(hours=8)),
        kind="NIGHT", calendar_version=CALENDAR, evidence_status="SCENARIO")
    slots = k._minute_slots(new_year)
    assert len(slots) == 240
    assert slots[0][0] < slots[-1][1]
    # Identical duplicate bars are idempotent; conflicting duplicates are INVALID.
    session = day_sessions(9)[0]
    base = minute_bar(0, cst(9, 9), ohlc=PATTERN[0], volume=1, oi_open=20, oi_close=21,
                      session_id="D09-0")
    same = k.aggregate_bars((base, base), day_sessions(9), 60, cst(9, 9) + 5 * M)
    assert same == (base,)
    conflicting = minute_bar(0, cst(9, 9), ohlc=PATTERN[1], volume=1, oi_open=20,
                             oi_close=21, session_id="D09-0")
    with pytest.raises(k.ClockError):
        k.aggregate_bars((base, conflicting), day_sessions(9), 60, cst(9, 9) + 5 * M)


def test_holding_deadline_and_open_cutoff() -> None:
    data = load_fixture()
    sessions = data["sessions"]
    minute = c.HoldingPolicy(True, True, False, 30, 5)
    daily = c.HoldingPolicy(False, True, True, 0, 0)
    # Default minute policy: same trading day, lunch allowed, night-to-day forbidden.
    assert k.holding_deadline(cst(9, 10), "SYN.rb", minute, sessions) == cst(9, 15)
    assert k.holding_deadline(cst(9, 11, 45), "SYN.rb", minute, sessions) == cst(9, 15)
    assert k.holding_deadline(cst(9, 22), "SYN.rb", minute, sessions) == cst(9, 23)
    # allow_night_to_day extends through the day session of the same trading day.
    bridging = c.HoldingPolicy(True, True, True, 30, 5)
    assert k.holding_deadline(cst(9, 22), "SYN.rb", bridging, sessions) == cst(12, 15)
    # No lunch: holding stops at the end of the current fragment.
    no_lunch = c.HoldingPolicy(True, False, False, 30, 5)
    assert k.holding_deadline(cst(9, 11), "SYN.rb", no_lunch, sessions) == cst(9, 11, 30)
    # Daily policy: planned exit at the end of the frozen calendar.
    assert k.holding_deadline(cst(9, 10), "SYN.rb", daily, sessions) == \
        max(w.end_nano for w in sessions)
    # Stop-open cutoff: 30 trading minutes before the holding segment end.
    assert k.can_open_before_close(cst(9, 14, 20), "SYN.rb", minute, sessions) is True
    assert k.can_open_before_close(cst(9, 14, 31), "SYN.rb", minute, sessions) is False
    assert k.can_open_before_close(cst(9, 22, 40), "SYN.rb", minute, sessions) is False
    # Pure calendar function: the exit clock fires without waiting for new bars.
    assert k.holding_deadline(cst(9, 14, 59, 30), "SYN.rb", minute, sessions) == cst(9, 15)
