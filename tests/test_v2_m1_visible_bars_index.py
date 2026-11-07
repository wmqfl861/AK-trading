"""M1-03b H4: visible-bars static index, per-query cutoffs and cache identity.

Differential standard: every assertion compares the product implementation
(``visible_bars`` / ``visible_bars_recent`` backed by the cached index)
against an independent scan written out in this file from the pre-H4
semantics — per-bar filters, per-query current-trading-day derivation and
the stable ``(start_nano, datetime_nano)`` ordering with original ordinal
ties. Randomized bundles cover arbitrary availability/start ordering,
duplicate keys, shuffled bundle order, overlapping/missing sessions,
unknown contracts and decreasing query times (r3-001 H4).
"""
from __future__ import annotations

import hashlib
import random
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ak_trading.v2 import data as d
from ak_trading.v2.contracts import (
    DataQuality, FrozenInput, Instrument, MarketBar, SessionWindow,
)

M = 60_000_000_000
BASE = 1_760_000_000 * 10**9  # fixed synthetic epoch, no wall clock


def _quality(status: str = "READY", basis: str = "reconstructed") -> DataQuality:
    return DataQuality(status=status, reasons=(), temporal_basis=basis,
                       input_hash=hashlib.sha256(b"index-test").hexdigest())


def _instrument(contract_id: str, product: str) -> Instrument:
    return Instrument(contract_id=contract_id, raw_symbol=contract_id, product=product,
                      sector="synthetic", risk_groups=("SYN",), listing_nano=0,
                      expiry_nano=None, last_trade_nano=None, delivery_cutoff_nano=None,
                      quality=_quality())


def _bar(index: int, *, contract_id: str, product: str, duration: int, start: int,
         end: int | None = None, trading_day: date | None, available_at: int | None,
         complete: bool = True, status: str = "READY", basis: str = "reconstructed",
         volume: Decimal | None = Decimal(1000), session_id: str | None = "s0",
         datetime_nano: int | None = None) -> MarketBar:
    return MarketBar(
        bar_id=f"{contract_id}-{duration}-{index}", contract_id=contract_id,
        raw_symbol=contract_id, product=product, duration_seconds=duration,
        datetime_nano=start if datetime_nano is None else datetime_nano,
        start_nano=start, end_nano=start + duration if end is None else end,
        trading_day=trading_day, session_id=session_id, available_at_nano=available_at,
        imported_at_nano=available_at, open=Decimal("99"), high=Decimal("101"),
        low=Decimal("98"), close=Decimal("100"), volume=volume, open_oi=Decimal("0"),
        close_oi=Decimal("0"), complete=complete, expected_minutes=1, actual_minutes=1,
        source="index-test", quality=_quality(status, basis))


def _reference_current_trading_day(bundle: FrozenInput, contract_id: str, decision: int):
    """Original pre-H4 derivation, verbatim semantics."""
    products = {i.product for i in bundle.instruments if i.contract_id == contract_id}
    products.update(b.product for b in bundle.bars if b.contract_id == contract_id)
    scoped = [s for s in bundle.sessions if s.product in products]
    for session in sorted(scoped, key=lambda s: s.start_nano):
        if session.start_nano <= decision < session.end_nano:
            return session.trading_day
    preceding = [s for s in scoped if s.end_nano <= decision]
    if preceding:
        return max(preceding, key=lambda s: s.end_nano).trading_day
    return None


def _reference_visible_bars(bundle: FrozenInput, contract_id: str, duration: int,
                            decision: int, intraday: bool):
    """Independent scan of the pre-H4 per-query semantics."""
    current_day = (_reference_current_trading_day(bundle, contract_id, decision)
                   if intraday and duration == 86400 else None)
    products = {b.product for b in bundle.bars if b.contract_id == contract_id}
    sessions = [s for s in bundle.sessions if s.product in products]

    def on_minute_grid(bar: MarketBar) -> bool:
        if not isinstance(bar, MarketBar) or bar.duration_seconds != 60:
            return True
        for session in sessions:
            if session.start_nano <= bar.start_nano < session.end_nano:
                return (bar.start_nano - session.start_nano) % M == 0
        return True

    visible = [
        bar for bar in bundle.bars
        if bar.contract_id == contract_id
        and bar.duration_seconds == duration
        and bar.complete
        and bar.end_nano <= decision
        and bar.available_at_nano is not None
        and bar.available_at_nano <= decision
        and bar.quality.status == "READY"
        and bar.quality.temporal_basis != "unknown"
        and on_minute_grid(bar)
        and not (current_day is not None and bar.trading_day == current_day)
    ]
    return tuple(sorted(visible, key=lambda b: (b.start_nano, b.datetime_nano)))


def _capacity_window(population, count=20):
    ordered = sorted(population, key=lambda b: (b.end_nano, b.bar_id))
    return tuple(ordered[-count:]) if count else ()


def _random_bundle(seed: int) -> FrozenInput:
    """Seeded adversarial bundle: shuffled order, arbitrary availability,
    duplicate (start, datetime) keys, mixed durations and quality, an
    unknown contract, overlapping and missing sessions."""
    rng = random.Random(seed)
    contracts = {"rb": "RB", "hc": "RB", "cu": "CU"}
    days = [date(2026, 3, day) for day in range(1, 6)]
    sessions: list[SessionWindow] = []
    for day_index, day in enumerate(days):
        day_start = BASE + day_index * 86_400 * 10**9
        sessions.append(SessionWindow(f"d{day_index}-am", "RB", day, day_start + 9 * 3600 * 10**9,
                                      day_start + 10 * 3600 * 10**9, "DAY", "cal", "SCENARIO"))
        # Overlapping RB session with a half-minute offset: bundle order decides.
        if day_index % 2 == 0:
            sessions.append(SessionWindow(f"d{day_index}-am2", "RB", day,
                                          day_start + 9 * 3600 * 10**9 + 30 * 10**9,
                                          day_start + 10 * 3600 * 10**9 + 30 * 10**9,
                                          "DAY", "cal", "SCENARIO"))
        sessions.append(SessionWindow(f"d{day_index}-pm", "CU", day,
                                      day_start + 13 * 3600 * 10**9,
                                      day_start + 15 * 3600 * 10**9, "DAY", "cal", "SCENARIO"))
    bars: list[MarketBar] = []
    counter = 0
    for day_index, day in enumerate(days):
        day_start = BASE + day_index * 86_400 * 10**9
        for minute in range(rng.randint(6, 14)):
            contract = rng.choice(tuple(contracts))
            duration = rng.choice((60, 300, 86400))
            # Off-grid starts on even days exercise the minute-grid filter.
            offset = (minute * M if day_index % 2 == 0
                      else minute * M + 30 * 10**9)
            start = day_start + 9 * 3600 * 10**9 + offset
            available = rng.choice((
                start + M,                       # visible one minute later
                start + 3 * M,                   # late availability
                start + 10 * 3600 * 10**9,       # far-future availability
                None,                            # unknown availability
            ))
            bar = _bar(counter, contract_id=contract, product=contracts[contract],
                       duration=duration, start=start, trading_day=day,
                       available_at=available,
                       complete=rng.random() > 0.1,
                       status=rng.choice(("READY", "READY", "READY", "DEGRADED", "INVALID")),
                       basis=rng.choice(("observed", "reconstructed", "unknown")),
                       volume=rng.choice((Decimal(500), Decimal(900), None)),
                       session_id=None if rng.random() < 0.3 else "d0-am")
            bars.append(bar)
            counter += 1
            if rng.random() < 0.25:
                # Duplicate (start, datetime) key with a different bar_id:
                # raw population keeps both; ordering must keep bundle order.
                bars.append(_bar(counter, contract_id=contract, product=contracts[contract],
                                 duration=duration, start=start, trading_day=day,
                                 available_at=available, volume=Decimal(700),
                                 session_id=bar.session_id))
                counter += 1
        # A daily bar per contract per day, sometimes missing.
        if rng.random() < 0.8:
            bars.append(_bar(counter, contract_id="rb", product="RB", duration=86400,
                             start=day_start + 9 * 3600 * 10**9,
                             end=day_start + 15 * 3600 * 10**9, trading_day=day,
                             available_at=day_start + 15 * 3600 * 10**9 + 10**9))
            counter += 1
    rng.shuffle(bars)
    instruments = tuple(_instrument(contract, product)
                        for contract, product in contracts.items())
    return FrozenInput(Path("SYNTHETIC"), "0" * 64, instruments, tuple(sessions),
                       tuple(bars), (), ())


def _query_grid(bundle: FrozenInput, rng: random.Random) -> list[int]:
    starts = [b.start_nano for b in bundle.bars] or [BASE]
    ends = [b.end_nano for b in bundle.bars] or [BASE + M]
    grid = {min(starts), max(ends), BASE, BASE + 5 * 86_400 * 10**9}
    grid.update(rng.randint(min(starts), max(ends)) for _ in range(24))
    grid.update(rng.randint(min(starts), max(ends) - 1) + 1 for _ in range(6))
    return sorted(grid)


@pytest.fixture(autouse=True)
def _isolated_cache():
    d.clear_visible_index_cache()
    yield
    d.clear_visible_index_cache()


def test_randomized_differential_matches_pre_h4_scan():
    for seed in range(12):
        bundle = _random_bundle(seed)
        rng = random.Random(1000 + seed)
        for decision in _query_grid(bundle, rng):
            for duration in (60, 300, 86400):
                for intraday in (False, True):
                    produced = d.visible_bars(bundle, "rb", duration, decision, intraday)
                    expected = _reference_visible_bars(bundle, "rb", duration, decision,
                                                      intraday)
                    assert produced == expected, (seed, decision, duration, intraday)
                    for count in (0, 1, 5, 20):
                        recent = d.visible_bars_recent(bundle, "rb", duration, decision,
                                                       intraday, count)
                        assert recent == _capacity_window(expected, count), \
                            (seed, decision, duration, intraday, count)


def test_unknown_contract_and_missing_duration_match_reference():
    bundle = _random_bundle(3)
    assert d.visible_bars(bundle, "missing-contract", 60, BASE + M, False) == ()
    assert d.visible_bars_recent(bundle, "missing-contract", 60, BASE + M, False, 20) == ()
    assert d.visible_bars(bundle, "rb", 900, BASE + M, True) == \
        _reference_visible_bars(bundle, "rb", 900, BASE + M, True)


def test_decreasing_and_day_changing_queries_never_reuse_a_cached_day():
    bars = tuple(
        _bar(i, contract_id="rb", product="RB", duration=86400,
             start=BASE + i * 86_400 * 10**9, end=BASE + (i + 1) * 86_400 * 10**9,
             trading_day=date(2026, 3, 1 + i),
             available_at=BASE + (i + 1) * 86_400 * 10**9 + 10**9)
        for i in range(4))
    sessions = tuple(
        SessionWindow(f"d{i}", "RB", date(2026, 3, 1 + i), BASE + i * 86_400 * 10**9,
                      BASE + (i + 1) * 86_400 * 10**9, "DAY", "cal", "SCENARIO")
        for i in range(4))
    bundle = FrozenInput(Path("SYNTHETIC"), "0" * 64, (_instrument("rb", "RB"),),
                         sessions, bars, (), ())
    day3_late = BASE + 4 * 86_400 * 10**9 - M          # inside day 4's session
    day2_mid = BASE + 2 * 86_400 * 10**9 + M           # inside day 3's session
    day1_mid = BASE + 1 * 86_400 * 10**9 + M           # inside day 2's session
    # Warm the cache, then query backwards: each day must be excluded fresh.
    d.visible_bars(bundle, "rb", 86400, day3_late, True)
    at_day2 = d.visible_bars(bundle, "rb", 86400, day2_mid, True)
    assert [b.trading_day for b in at_day2] == [date(2026, 3, 1), date(2026, 3, 2)]
    at_day1 = d.visible_bars(bundle, "rb", 86400, day1_mid, True)
    assert [b.trading_day for b in at_day1] == [date(2026, 3, 1)]
    # Decreasing repeat equals the first increasing result and the reference.
    again_day2 = d.visible_bars(bundle, "rb", 86400, day2_mid, True)
    assert again_day2 == at_day2
    for when in (day1_mid, day2_mid, day3_late):
        assert d.visible_bars(bundle, "rb", 86400, when, True) == \
            _reference_visible_bars(bundle, "rb", 86400, when, True)
        assert d.visible_bars_recent(bundle, "rb", 86400, when, True, 20) == \
            _capacity_window(_reference_visible_bars(bundle, "rb", 86400, when, True))


def test_original_order_and_ties_preserved_in_full_and_capacity_orders():
    # Equal (start, datetime) pairs keep bundle order in full results; the
    # capacity order applies (end_nano, bar_id) on top with that tie order.
    def tied(bar_id: str, volume: Decimal) -> MarketBar:
        return MarketBar(bar_id=bar_id, contract_id="rb", raw_symbol="rb", product="RB",
                         duration_seconds=60, datetime_nano=BASE + 5 * M,
                         start_nano=BASE + 5 * M, end_nano=BASE + 6 * M,
                         trading_day=date(2026, 3, 2), session_id="s",
                         available_at_nano=BASE + 6 * M, imported_at_nano=BASE + 6 * M,
                         open=Decimal("99"), high=Decimal("101"), low=Decimal("98"),
                         close=Decimal("100"), volume=volume, open_oi=Decimal("0"),
                         close_oi=Decimal("0"), complete=True, expected_minutes=1,
                         actual_minutes=1, source="index-test", quality=_quality())

    bars = (tied("z-first", Decimal(10)), tied("a-second", Decimal(20)))
    bundle = FrozenInput(Path("SYNTHETIC"), "0" * 64, (_instrument("rb", "RB"),), (),
                         tuple(bars), (), ())
    full = d.visible_bars(bundle, "rb", 60, BASE + 7 * M, False)
    assert [b.bar_id for b in full] == ["z-first", "a-second"]
    # Capacity order re-breaks the (end_nano, bar_id) tie by bar_id, while
    # the full result keeps the original bundle order.
    recent = d.visible_bars_recent(bundle, "rb", 60, BASE + 7 * M, False, 20)
    assert [b.bar_id for b in recent] == ["a-second", "z-first"]


def test_overlapping_sessions_keep_bundle_order_precedence():
    grid = BASE + 9 * 3600 * 10**9
    # A bar one minute after the aligned session start but 30s off the
    # overlapping one: aligned when the aligned session comes first in the
    # bundle, misaligned when the offset session comes first.
    aligned_first = SessionWindow("aligned", "RB", date(2026, 3, 2), grid, grid + 3600 * 10**9,
                                  "DAY", "cal", "SCENARIO")
    offset = SessionWindow("offset", "RB", date(2026, 3, 2), grid + 30 * 10**9,
                           grid + 3600 * 10**9 + 30 * 10**9, "DAY", "cal", "SCENARIO")
    bar = _bar(0, contract_id="rb", product="RB", duration=60, start=grid + M,
               trading_day=date(2026, 3, 2), available_at=grid + 2 * M, session_id=None)
    for order, visible_count in (((aligned_first, offset), 1), ((offset, aligned_first), 0)):
        bundle = FrozenInput(Path("SYNTHETIC"), "0" * 64, (_instrument("rb", "RB"),),
                             order, (bar,), (), ())
        assert len(d.visible_bars(bundle, "rb", 60, grid + 3 * M, False)) == visible_count


def test_bars_outside_any_session_pass_the_grid_check():
    # No calendar for the product at all: minute bars stay eligible.
    bar = _bar(0, contract_id="cu", product="CU", duration=60, start=BASE + 30 * 10**9,
               trading_day=date(2026, 3, 2), available_at=BASE + M, session_id=None)
    bundle = FrozenInput(Path("SYNTHETIC"), "0" * 64, (), (), (bar,), (), ())
    assert d.visible_bars(bundle, "cu", 60, BASE + 2 * M, False) == (bar,)


def test_invalid_arguments_keep_original_type_error_priority():
    bundle = FrozenInput(Path("SYNTHETIC"), "0" * 64, (), (), (), (), ())
    cases = [
        ({}, "rb", 60, BASE, False, "bundle_must_be_FrozenInput"),
        (bundle, 7, 60, BASE, False, "contract_id_must_be_str"),
        (bundle, "rb", True, BASE, False, "duration_seconds_must_be_int"),
        (bundle, "rb", 60, True, False, "decision_nano_must_be_int"),
        (bundle, "rb", 60, BASE, 1, "intraday_must_be_bool"),
    ]
    for bad_bundle, contract_id, duration, decision, intraday, message in cases:
        with pytest.raises(TypeError, match=message):
            d.visible_bars(bad_bundle, contract_id, duration, decision, intraday)
        with pytest.raises(TypeError, match=message):
            d.visible_bars_recent(bad_bundle, contract_id, duration, decision,
                                  intraday, 20)
    with pytest.raises(TypeError, match="count_must_be_int"):
        d.visible_bars_recent(bundle, "rb", 60, BASE, False, True)
    with pytest.raises(TypeError, match="count_must_be_int"):
        d.visible_bars_recent(bundle, "rb", 60, BASE, False, "20")
    with pytest.raises(ValueError, match="count_must_be_nonnegative"):
        d.visible_bars_recent(bundle, "rb", 60, BASE, False, -1)
    # Argument priority: an invalid intraday outranks an invalid count.
    with pytest.raises(TypeError, match="intraday_must_be_bool"):
        d.visible_bars_recent(bundle, "rb", 60, BASE, 1, -1)


def test_multiple_bundle_identities_and_future_appends_stay_independent():
    first = _random_bundle(5)
    second = _random_bundle(6)
    assert first.bars != second.bars
    decision = BASE + 3 * 86_400 * 10**9
    for _ in range(3):  # alternating queries never cross-contaminate
        assert d.visible_bars(first, "rb", 60, decision, True) == \
            _reference_visible_bars(first, "rb", 60, decision, True)
        assert d.visible_bars(second, "rb", 60, decision, True) == \
            _reference_visible_bars(second, "rb", 60, decision, True)
    appended = _random_bundle(5)
    extra = _bar(9_999, contract_id="rb", product="RB", duration=60,
                 start=BASE + 4 * 86_400 * 10**9 + 9 * 3600 * 10**9,
                 trading_day=date(2026, 3, 5), available_at=None)
    from dataclasses import replace
    grown = replace(appended, bars=appended.bars + (extra,))
    assert d.visible_bars(grown, "rb", 60, decision, False) == \
        _reference_visible_bars(grown, "rb", 60, decision, False)
    # The original bundle keeps its own results after the append created a
    # new bundle object (future append = new identity, never a mutation).
    assert d.visible_bars(appended, "rb", 60, decision, False) == \
        _reference_visible_bars(appended, "rb", 60, decision, False)
    stats = d.visible_index_cache_stats()
    assert stats["entries"] >= 3 and stats["retained_bytes"] <= stats["budget_bytes"]


def test_cache_eviction_is_lru_and_budgeted(monkeypatch):
    def bundle_with(index: int) -> FrozenInput:
        bars = tuple(
            _bar(i, contract_id=f"c{index}", product=f"P{index}", duration=60,
                 start=BASE + i * M, trading_day=date(2026, 3, 2),
                 available_at=BASE + (i + 1) * M)
            for i in range(50))
        return FrozenInput(Path("SYNTHETIC"), "0" * 64, (), (), bars, (), ())

    first, second, third = bundle_with(1), bundle_with(2), bundle_with(3)
    decision = BASE + 60 * M
    d.visible_bars(first, "c1", 60, decision, False)
    one = d.visible_index_cache_stats()
    assert one["entries"] == 1
    # A budget admitting exactly one more entry evicts the least recently
    # used entry when a third arrives.
    monkeypatch.setattr(d, "_INDEX_CACHE_MAX_RETAINED_BYTES",
                        one["retained_bytes"] * 2 + 64)
    d.visible_bars(second, "c2", 60, decision, False)
    assert d.visible_index_cache_stats()["entries"] == 2
    d.visible_bars(first, "c1", 60, decision, False)  # first is now most recent
    d.visible_bars(third, "c3", 60, decision, False)
    stats = d.visible_index_cache_stats()
    assert stats["entries"] == 2 and stats["retained_bytes"] <= stats["budget_bytes"]
    # The evicted bundle (second) still answers correctly after a rebuild.
    assert d.visible_bars(second, "c2", 60, decision, False) == \
        _reference_visible_bars(second, "c2", 60, decision, False)
    assert d.visible_index_cache_stats()["entries"] == 2


def test_oversized_index_bypasses_retention_but_still_answers(monkeypatch):
    bundle = _random_bundle(7)
    d.visible_bars(bundle, "rb", 60, BASE + 3 * 86_400 * 10**9, False)
    built = d.visible_index_cache_stats()["retained_bytes"]
    d.clear_visible_index_cache()
    monkeypatch.setattr(d, "_INDEX_CACHE_MAX_RETAINED_BYTES", max(built - 1, 1))
    decision = BASE + 3 * 86_400 * 10**9
    assert d.visible_bars(bundle, "rb", 60, decision, False) == \
        _reference_visible_bars(bundle, "rb", 60, decision, False)
    assert d.visible_bars_recent(bundle, "rb", 60, decision, False, 20) == \
        _capacity_window(_reference_visible_bars(bundle, "rb", 60, decision, False))
    stats = d.visible_index_cache_stats()
    assert stats["entries"] == 0 and stats["retained_bytes"] == 0
    # The same oversized population stays correct on repeated uncached calls.
    assert d.visible_bars(bundle, "rb", 60, decision, False) == \
        _reference_visible_bars(bundle, "rb", 60, decision, False)
