"""M1-02 clock: sessions, night trading days, horizons, aggregation, exits."""
from __future__ import annotations

import hashlib
import importlib.util
import random
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


# --- K10-core: differential tests against the frozen legacy oracle ----------

_ORACLE_SPEC = importlib.util.spec_from_file_location(
    "v2_clock_legacy_oracle", Path(__file__).resolve().parent / "v2_clock_legacy_oracle.py")
oracle = importlib.util.module_from_spec(_ORACLE_SPEC)
_ORACLE_SPEC.loader.exec_module(oracle)

K10_MINUTE_HORIZONS = tuple(c.Horizon("TRADING_MINUTE", value) for value in (1, 5, 15, 30, 60))
K10_DAY_HORIZONS = tuple(c.Horizon("TRADING_DAY", value) for value in (1, 5, 20))
K10_POLICIES = (
    c.HoldingPolicy(True, True, False, 30, 5),
    c.HoldingPolicy(True, False, False, 30, 5),
    c.HoldingPolicy(True, True, True, 30, 5),
    c.HoldingPolicy(True, False, True, 0, 0),
    c.HoldingPolicy(False, True, True, 0, 0),
)


def _run(function, *args):
    try:
        return function(*args)
    except BaseException as error:  # noqa: BLE001 - differential capture
        return error


def _assert_same_result(reference, actual, label: str) -> None:
    """Both values or both exceptions with identical type and args."""
    if isinstance(reference, BaseException):
        assert isinstance(actual, BaseException), label
        assert type(actual) is type(reference), (label, actual, reference)
        assert actual.args == reference.args, (label, actual, reference)
    else:
        assert not isinstance(actual, BaseException), (label, actual)
        assert type(actual) is type(reference), (label, actual, reference)
        assert actual == reference, (label, actual, reference)


def _differential_battery(sessions, product, moments, horizons, policies) -> None:
    for moment in moments:
        for horizon in horizons:
            _assert_same_result(
                _run(oracle.horizon_window, moment, product, horizon, sessions),
                _run(k.horizon_window, moment, product, horizon, sessions),
                f"horizon {horizon.unit}/{horizon.value} @ {moment}")
        for policy in policies:
            _assert_same_result(
                _run(oracle.holding_deadline, moment, product, policy, sessions),
                _run(k.holding_deadline, moment, product, policy, sessions),
                f"holding {policy} @ {moment}")
    for policy in policies:
        for moment in moments[:: max(1, len(moments) // 8)]:
            _assert_same_result(
                _run(oracle.can_open_before_close, moment, product, policy, sessions),
                _run(k.can_open_before_close, moment, product, policy, sessions),
                f"can_open {policy} @ {moment}")


def _probe_moments(sessions) -> list[int]:
    moments = set()
    for window in sessions:
        moments.update((window.start_nano - 1, window.start_nano,
                        window.end_nano - 1, window.end_nano, window.end_nano + 1))
    moments.add(min(w.start_nano for w in sessions) - 86_400_000_000_000)
    moments.add(max(w.end_nano for w in sessions) + 86_400_000_000_000)
    return sorted(moments)


def at(day_date: date, hour: int, minute: int = 0) -> int:
    moment = datetime(day_date.year, day_date.month, day_date.day, hour, minute,
                      tzinfo=timezone.utc) - timedelta(hours=8)
    return unano(moment)


def _random_calendar(seed: int, *, two_products: bool, misaligned: bool, shuffle: bool):
    rng = random.Random(seed)
    names = ["SYN.rb", "SYN.hc"] if two_products else ["SYN.rb"]
    weekdays: list[date] = []
    cursor = date(2026, 3, 2)
    while len(weekdays) < rng.randint(2, 5):
        if cursor.weekday() < 5:
            weekdays.append(cursor)
        cursor += timedelta(days=1)
    sessions: list[c.SessionWindow] = []
    for name in names:
        for day_index, day_date in enumerate(weekdays):
            spans = [(9, 0, 10, 15), (10, 30, 11, 30), (13, 30, 15, 0)]
            if rng.random() < 0.35:
                spans.pop(rng.randrange(len(spans)))
            for index, (a, b, x, y) in enumerate(spans):
                sessions.append(c.SessionWindow(
                    session_id=f"R{day_date.isoformat()}-{name}-{index}", product=name,
                    trading_day=day_date, start_nano=at(day_date, a, b),
                    end_nano=at(day_date, x, y), kind="DAY",
                    calendar_version=CALENDAR, evidence_status="SCENARIO"))
            if day_index + 1 < len(weekdays) and rng.random() < 0.5:
                following = weekdays[day_index + 1]
                sessions.append(c.SessionWindow(
                    session_id=f"RN{following.isoformat()}-{name}", product=name,
                    trading_day=following, start_nano=at(day_date, 21), end_nano=at(day_date, 23),
                    kind="NIGHT", calendar_version=CALENDAR, evidence_status="SCENARIO"))
    if misaligned:
        victim = max((w for w in sessions if w.product == "SYN.rb"), key=lambda w: w.end_nano)
        sessions = [w if w is not victim else c.SessionWindow(
            session_id=victim.session_id + "-odd", product=victim.product,
            trading_day=victim.trading_day, start_nano=victim.start_nano,
            end_nano=victim.end_nano + 1, kind=victim.kind,
            calendar_version=CALENDAR, evidence_status="SCENARIO") for w in sessions]
    if shuffle:
        rng.shuffle(sessions)
    return tuple(sessions)


@pytest.mark.parametrize("seed", [pytest.param(11, id="seed-11"), pytest.param(23, id="seed-23"),
                                  pytest.param(37, id="seed-37"), pytest.param(53, id="seed-53")])
def test_k10_differential_random_calendars(seed: int) -> None:
    k._clear_calendar_index_cache()
    variants = ((False, False, True), (True, False, False),
                (True, True, False), (False, False, False))
    for index, (two_products, misaligned, shuffle) in enumerate(variants):
        sessions = _random_calendar(seed + 97 * index, two_products=two_products,
                                    misaligned=misaligned, shuffle=shuffle)
        moments = _probe_moments(sessions)
        horizons = K10_MINUTE_HORIZONS + K10_DAY_HORIZONS
        # Cold pass then warm pass over the same tuple: both must match the oracle.
        _differential_battery(sessions, "SYN.rb", moments, horizons, K10_POLICIES)
        _differential_battery(sessions, "SYN.rb", moments, horizons, K10_POLICIES)
        if two_products:
            _differential_battery(sessions, "SYN.hc", moments, horizons, K10_POLICIES)


def test_k10_differential_structural_edges() -> None:
    k._clear_calendar_index_cache()
    sessions = load_fixture()["sessions"]
    moments = _probe_moments(sessions)
    moments += [cst(9, 14, 30), cst(9, 14, 30) - 1, cst(9, 16), cst(9, 23) + 1]
    horizons = K10_MINUTE_HORIZONS + K10_DAY_HORIZONS
    _differential_battery(sessions, "SYN.rb", moments, horizons, K10_POLICIES)
    _differential_battery(sessions, "SYN.rb", moments, horizons, K10_POLICIES)

    # Empty calendar and absent product.
    _differential_battery((), "SYN.rb", [cst(9, 9)],
                          K10_MINUTE_HORIZONS[:1] + K10_DAY_HORIZONS[:1], K10_POLICIES[:2])
    _differential_battery(sessions, "SYN.absent", [cst(9, 9), cst(9, 11, 45)],
                          K10_MINUTE_HORIZONS[:1] + K10_DAY_HORIZONS[:1], K10_POLICIES[:2])

    # Unordered tuple of the same windows.
    reversed_sessions = tuple(sorted(sessions, key=lambda w: (w.product, -w.start_nano)))
    _differential_battery(reversed_sessions, "SYN.rb", moments, horizons, K10_POLICIES)

    # Duplicate same-product session and cross-product overlap still error even
    # when the queried calendar windows belong to another product.
    duplicated = day_sessions(9) + day_sessions(9)[:1]
    _assert_same_result(
        _run(oracle.horizon_window, cst(9, 10), "SYN.rb", K10_MINUTE_HORIZONS[0], duplicated),
        _run(k.horizon_window, cst(9, 10), "SYN.rb", K10_MINUTE_HORIZONS[0], duplicated),
        "duplicate-overlap")
    other_overlap = day_sessions(9, product="SYN.hc") + day_sessions(9, product="SYN.hc")[:1]
    mixed = day_sessions(9) + other_overlap
    _assert_same_result(
        _run(oracle.holding_deadline, cst(9, 10), "SYN.rb", K10_POLICIES[0], mixed),
        _run(k.holding_deadline, cst(9, 10), "SYN.rb", K10_POLICIES[0], mixed),
        "cross-product-overlap")

    # Interleaved trading-day labels: first-start order differs from label order.
    inter = (c.SessionWindow(session_id="I0", product="SYN.ix", trading_day=date(2026, 1, 6),
                             start_nano=cst(6, 9), end_nano=cst(6, 10), kind="DAY",
                             calendar_version=CALENDAR, evidence_status="SCENARIO"),
             c.SessionWindow(session_id="I1", product="SYN.ix", trading_day=date(2026, 1, 5),
                             start_nano=cst(6, 10, 30), end_nano=cst(6, 11, 30), kind="DAY",
                             calendar_version=CALENDAR, evidence_status="SCENARIO"),
             c.SessionWindow(session_id="I2", product="SYN.ix", trading_day=date(2026, 1, 5),
                             start_nano=cst(6, 13, 30), end_nano=cst(6, 15), kind="DAY",
                             calendar_version=CALENDAR, evidence_status="SCENARIO"),
             c.SessionWindow(session_id="I3", product="SYN.ix", trading_day=date(2026, 1, 7),
                             start_nano=cst(7, 9), end_nano=cst(7, 10), kind="DAY",
                             calendar_version=CALENDAR, evidence_status="SCENARIO"))
    _differential_battery(inter, "SYN.ix", _probe_moments(inter), horizons, K10_POLICIES)

    # Only the session span is checked for minute alignment, not the absolute start.
    odd = (c.SessionWindow(session_id="ODD", product="SYN.odd", trading_day=date(2026, 1, 9),
                           start_nano=cst(9, 9) + 30 * 10**9,
                           end_nano=cst(9, 9) + 30 * 10**9 + 5 * M, kind="DAY",
                           calendar_version=CALENDAR, evidence_status="SCENARIO"),)
    _differential_battery(odd, "SYN.odd", _probe_moments(odd), horizons, K10_POLICIES)

    # Non-minute span: minute grids error while daily/holding queries still work.
    misspan = (c.SessionWindow(session_id="MS0", product="SYN.ms", trading_day=date(2026, 1, 9),
                               start_nano=cst(9, 9), end_nano=cst(9, 9) + 90 * M + 1, kind="DAY",
                               calendar_version=CALENDAR, evidence_status="SCENARIO"),
               night_session(9, 12, product="SYN.ms"))
    _differential_battery(misspan, "SYN.ms", _probe_moments(misspan), horizons, K10_POLICIES)


def test_k10_error_priority_matches_oracle() -> None:
    k._clear_calendar_index_cache()
    sessions = load_fixture()["sessions"]
    overlapping = sessions + (sessions[0],)
    bad_policy = ("policy",)
    # Invalid policy / horizon beats invalid calendar in both implementations.
    _assert_same_result(
        _run(oracle.holding_deadline, cst(9, 10), "SYN.rb", bad_policy, overlapping),
        _run(k.holding_deadline, cst(9, 10), "SYN.rb", bad_policy, overlapping), "policy-first")
    _assert_same_result(
        _run(oracle.can_open_before_close, cst(9, 10), "SYN.rb", bad_policy, overlapping),
        _run(k.can_open_before_close, cst(9, 10), "SYN.rb", bad_policy, overlapping), "policy-first")
    _assert_same_result(
        _run(oracle.horizon_window, cst(9, 10), "SYN.rb", "horizon", overlapping),
        _run(k.horizon_window, cst(9, 10), "SYN.rb", "horizon", overlapping), "horizon-first")
    # Valid policy / horizon with an invalid calendar.
    _assert_same_result(
        _run(oracle.holding_deadline, cst(9, 10), "SYN.rb", K10_POLICIES[0], overlapping),
        _run(k.holding_deadline, cst(9, 10), "SYN.rb", K10_POLICIES[0], overlapping), "overlap")
    _assert_same_result(
        _run(oracle.horizon_window, cst(9, 10), "SYN.rb", K10_MINUTE_HORIZONS[0], overlapping),
        _run(k.horizon_window, cst(9, 10), "SYN.rb", K10_MINUTE_HORIZONS[0], overlapping), "overlap")
    # Holding succeeds while the minute grid is misaligned: can_open errors after holding.
    day9 = sorted((w for w in sessions if w.trading_day == date(2026, 1, 9) and w.kind == "DAY"),
                  key=lambda w: w.end_nano)
    last = day9[-1]
    misaligned = tuple(w if w is not last else c.SessionWindow(
        session_id=last.session_id + "-odd", product=last.product, trading_day=last.trading_day,
        start_nano=last.start_nano, end_nano=last.end_nano + 1, kind=last.kind,
        calendar_version=last.calendar_version, evidence_status=last.evidence_status)
        for w in sessions)
    _assert_same_result(
        _run(oracle.holding_deadline, cst(9, 10), "SYN.rb", K10_POLICIES[0], misaligned),
        _run(k.holding_deadline, cst(9, 10), "SYN.rb", K10_POLICIES[0], misaligned), "mis-holding")
    _assert_same_result(
        _run(oracle.can_open_before_close, cst(9, 10), "SYN.rb", K10_POLICIES[0], misaligned),
        _run(k.can_open_before_close, cst(9, 10), "SYN.rb", K10_POLICIES[0], misaligned),
        "mis-can-open")
    _assert_same_result(
        _run(oracle.horizon_window, cst(9, 10), "SYN.rb", K10_DAY_HORIZONS[0], misaligned),
        _run(k.horizon_window, cst(9, 10), "SYN.rb", K10_DAY_HORIZONS[0], misaligned), "mis-daily")


def test_k10_cache_warm_reuse_zero_rebuild() -> None:
    k._clear_calendar_index_cache()
    sessions = load_fixture()["sessions"]
    minute5 = c.Horizon("TRADING_MINUTE", 5)
    minute15 = c.Horizon("TRADING_MINUTE", 15)
    day1 = c.Horizon("TRADING_DAY", 1)
    day5 = c.Horizon("TRADING_DAY", 5)
    policy = c.HoldingPolicy(True, True, False, 30, 5)
    bridging = c.HoldingPolicy(True, True, True, 30, 5)
    before = k._index_stats()
    k.horizon_window(cst(9, 10), "SYN.rb", minute5, sessions)
    k.horizon_window(cst(9, 10), "SYN.rb", day1, sessions)
    k.holding_deadline(cst(9, 10), "SYN.rb", policy, sessions)
    k.can_open_before_close(cst(9, 10), "SYN.rb", policy, sessions)
    cold = {name: k._index_stats()[name] - before[name] for name in before}
    assert cold["session_validations"] >= 1
    assert cold["product_index_builds"] >= 1
    assert cold["minute_slot_materializations"] >= 1
    assert cold["trading_day_group_builds"] >= 1
    warm_before = k._index_stats()
    for _ in range(3):
        k.horizon_window(cst(9, 10), "SYN.rb", minute5, sessions)
        k.horizon_window(cst(9, 10, 3), "SYN.rb", minute15, sessions)
        k.horizon_window(cst(9, 10), "SYN.rb", day5, sessions)
        k.holding_deadline(cst(9, 22), "SYN.rb", bridging, sessions)
        k.can_open_before_close(cst(9, 14, 20), "SYN.rb", policy, sessions)
    warm = {name: k._index_stats()[name] - warm_before[name] for name in warm_before}
    assert warm == {name: 0 for name in warm_before}


def test_k10_cache_identity_eviction_and_clear() -> None:
    k._clear_calendar_index_cache()
    sessions = load_fixture()["sessions"]
    minute5 = c.Horizon("TRADING_MINUTE", 5)
    policy = c.HoldingPolicy(True, True, False, 30, 5)
    # Same-value fresh tuple: an identity miss that still yields oracle results.
    # ``tuple(sessions)`` and even ``sessions + ()`` return the same object on
    # this CPython, so the equal-value copy is built through a list.
    fresh = tuple(list(sessions))
    assert fresh == sessions and fresh is not sessions
    start = k._index_stats()
    _assert_same_result(
        _run(oracle.horizon_window, cst(9, 10), "SYN.rb", minute5, fresh),
        _run(k.horizon_window, cst(9, 10), "SYN.rb", minute5, fresh), "fresh-tuple")
    assert k._index_stats()["session_validations"] - start["session_validations"] == 1

    # LRU cap: one more calendar than the cap evicts the least recently used.
    k._clear_calendar_index_cache()
    calendars = []
    for offset in range(k._INDEX_MAX_CALENDARS + 1):
        product = f"SYN.g{offset}"
        calendars.append(tuple(
            c.SessionWindow(session_id=w.session_id, product=product,
                            trading_day=w.trading_day, start_nano=w.start_nano,
                            end_nano=w.end_nano, kind=w.kind,
                            calendar_version=w.calendar_version,
                            evidence_status=w.evidence_status) for w in sessions))
    first, *others = calendars
    k.holding_deadline(cst(9, 10), "SYN.g0", policy, first)
    for index, other in enumerate(others, start=1):
        k.holding_deadline(cst(9, 10), f"SYN.g{index}", policy, other)
    assert k._index_cache_state()["entries"] == k._INDEX_MAX_CALENDARS
    start = k._index_stats()
    replay = k.holding_deadline(cst(9, 10), "SYN.g0", policy, first)
    assert replay == oracle.holding_deadline(cst(9, 10), "SYN.g0", policy, first)
    assert k._index_stats()["session_validations"] - start["session_validations"] >= 1

    # Clearing forces a real rebuild.
    k._clear_calendar_index_cache()
    assert k._index_cache_state() == {"entries": 0, "session_refs": 0, "minute_slots": 0}
    start = k._index_stats()
    k.holding_deadline(cst(9, 10), "SYN.g0", policy, first)
    assert k._index_stats()["session_validations"] - start["session_validations"] >= 1

    # Unknown products never accumulate cache entries.
    entries = k._index_cache_state()["entries"]
    for _ in range(3):
        with pytest.raises(k.ClockError):
            k.holding_deadline(cst(9, 10), "SYN.absent", policy, first)
    assert k._index_cache_state()["entries"] == entries


def test_k10_cache_capacity_limits() -> None:
    k._clear_calendar_index_cache()
    # A single product grid beyond the slot cap is used transiently, never retained.
    span = (k._INDEX_MAX_MINUTE_SLOTS + 1) * M
    huge = (c.SessionWindow(session_id="BIG", product="SYN.big", trading_day=date(2026, 1, 5),
                            start_nano=cst(5, 9), end_nano=cst(5, 9) + span, kind="DAY",
                            calendar_version=CALENDAR, evidence_status="SCENARIO"),)
    minute5 = c.Horizon("TRADING_MINUTE", 5)
    start = k._index_stats()
    first_result = k.horizon_window(cst(5, 9, 30), "SYN.big", minute5, huge)
    second_result = k.horizon_window(cst(5, 9, 30), "SYN.big", minute5, huge)
    delta = {name: k._index_stats()[name] - start[name] for name in start}
    assert delta["minute_slot_materializations"] == 2
    assert k._index_cache_state()["minute_slots"] == 0
    assert first_result == second_result
    _assert_same_result(
        _run(oracle.horizon_window, cst(5, 9, 30), "SYN.big", minute5, huge), first_result, "huge")

    # A calendar beyond the session-reference cap is never published at all.
    count = k._INDEX_MAX_SESSION_REFS // 2 + 1
    many = tuple(c.SessionWindow(session_id=f"M{i}", product="SYN.many",
                                 trading_day=date(2026, 1, 5), start_nano=cst(5, 9) + i * M,
                                 end_nano=cst(5, 9) + (i + 1) * M, kind="DAY",
                                 calendar_version=CALENDAR, evidence_status="SCENARIO")
                 for i in range(count))
    flat = c.HoldingPolicy(False, True, True, 0, 0)
    start = k._index_stats()
    deadline = k.holding_deadline(cst(5, 9), "SYN.many", flat, many)
    k.holding_deadline(cst(5, 9), "SYN.many", flat, many)
    delta = {name: k._index_stats()[name] - start[name] for name in start}
    assert delta["session_validations"] == 2
    assert deadline == oracle.holding_deadline(cst(5, 9), "SYN.many", flat, many)
    assert k._index_cache_state()["entries"] == 1  # only the huge-grid entry above


def test_k10_uncached_inputs_fall_back() -> None:
    k._clear_calendar_index_cache()
    sessions = load_fixture()["sessions"]
    minute5 = c.Horizon("TRADING_MINUTE", 5)
    policy = c.HoldingPolicy(True, True, False, 30, 5)

    class _SessionsTuple(tuple):
        pass

    subclass_input = _SessionsTuple(sessions)
    start = k._index_stats()
    for _ in range(2):
        _assert_same_result(
            _run(oracle.horizon_window, cst(9, 10), "SYN.rb", minute5, subclass_input),
            _run(k.horizon_window, cst(9, 10), "SYN.rb", minute5, subclass_input), "tuple-subclass")
        _assert_same_result(
            _run(oracle.holding_deadline, cst(9, 10), "SYN.rb", policy, subclass_input),
            _run(k.holding_deadline, cst(9, 10), "SYN.rb", policy, subclass_input), "tuple-subclass")
        _assert_same_result(
            _run(oracle.can_open_before_close, cst(9, 14, 20), "SYN.rb", policy, subclass_input),
            _run(k.can_open_before_close, cst(9, 14, 20), "SYN.rb", policy, subclass_input),
            "tuple-subclass")
    # Per round: horizon 1 + holding 1 + can_open (holding + own) 2 validations.
    assert k._index_stats()["session_validations"] - start["session_validations"] == 2 * 4
    assert k._index_cache_state()["entries"] == 0

    class _WindowSub(c.SessionWindow):
        pass

    substituted = tuple(_WindowSub(
        session_id=w.session_id, product=w.product, trading_day=w.trading_day,
        start_nano=w.start_nano, end_nano=w.end_nano, kind=w.kind,
        calendar_version=w.calendar_version, evidence_status=w.evidence_status)
        for w in sessions)
    start = k._index_stats()
    _assert_same_result(
        _run(oracle.horizon_window, cst(9, 10), "SYN.rb", minute5, substituted),
        _run(k.horizon_window, cst(9, 10), "SYN.rb", minute5, substituted), "window-subclass")
    _assert_same_result(
        _run(oracle.holding_deadline, cst(9, 10), "SYN.rb", policy, substituted),
        _run(k.holding_deadline, cst(9, 10), "SYN.rb", policy, substituted), "window-subclass")
    assert k._index_stats()["session_validations"] - start["session_validations"] == 2
    assert k._index_cache_state()["entries"] == 0

    _assert_same_result(
        _run(oracle.holding_deadline, cst(9, 10), "SYN.rb", policy, list(sessions)),
        _run(k.holding_deadline, cst(9, 10), "SYN.rb", policy, list(sessions)), "list-input")
