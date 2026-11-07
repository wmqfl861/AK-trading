"""M4 H4: raw recent-20 capacity window equivalence at the execution boundary.

The raw execution paths (``replay``'s ``previous=visible_bars_recent(...)``
and the payoff-path equivalent) must reproduce exactly what
``capacity_before`` computes over the FULL raw visible population:
capacity's own eligibility, ``(end_nano, bar_id)`` ordering and tie
stability, the last-20 window, 20 distinct starts, non-null volumes,
READY/DEGRADED allowance and the Decimal median/FLOOR arithmetic — never
"the final 20 by a different sort" (r3-001 H4).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ak_trading.v2 import data as d
from ak_trading.v2.contracts import FeeLeg, FrozenInput, TradingRule
from ak_trading.v2.decision.execution import capacity_before
from ak_trading.v2.factors import _deduplicate_visible
from ak_trading.v2.factors import FactorsError
from test_v2_m1_visible_bars_index import (
    BASE, M, _bar, _instrument, _capacity_window, _query_grid, _random_bundle,
    _reference_visible_bars,
)

import random


def _rule(contract_id: str = "rb", participation: str = "0.1") -> TradingRule:
    leg = FeeLeg(per_lot_cny=Decimal("0"), notional_rate=Decimal("0.0001"),
                 explicit_zero=False)
    return TradingRule(
        rule_id=f"rule-{contract_id}", contract_id=contract_id,
        effective_from_nano=0, effective_until_nano=BASE + 400 * 86_400 * 10**9,
        available_at_nano=0, multiplier=Decimal("10"), price_tick=Decimal("1"),
        lot_step=1, min_open_lots=1, open_fee=leg, close_fee=leg, close_today_fee=leg,
        long_margin_rate=Decimal("0.09"), short_margin_rate=Decimal("0.09"),
        limit_up=None, limit_down=None, slippage_ticks=Decimal("1"),
        impact_ticks=Decimal("0"), participation_rate=Decimal(participation),
        evidence="SCENARIO", source_sha256="a" * 64)


@pytest.fixture(autouse=True)
def _isolated_cache():
    d.clear_visible_index_cache()
    yield
    d.clear_visible_index_cache()


def test_randomized_recent_window_equals_full_population_capacity():
    rule = _rule()
    for seed in range(20, 30):
        bundle = _random_bundle(seed)
        rng = random.Random(2000 + seed)
        for decision in _query_grid(bundle, rng):
            full = _reference_visible_bars(bundle, "rb", 60, decision, False)
            recent = d.visible_bars_recent(bundle, "rb", 60, decision, False, 20)
            assert recent == _capacity_window(full)
            assert capacity_before(recent, "rb", 60, decision, rule) == \
                capacity_before(full, "rb", 60, decision, rule)
            if len(full) >= 20:
                # The exact window capacity sorts itself, tie order included.
                assert tuple(recent) == tuple(
                    sorted(full, key=lambda b: (b.end_nano, b.bar_id))[-20:])


def test_recent_window_is_not_the_final_twenty_of_a_different_sort():
    # 21 bars: starts ascend with i, but bar 20 closes earliest — capacity's
    # (end_nano, bar_id) window drops bar 20, while a naive suffix of the
    # (start_nano, datetime_nano) visible order would drop bar 0 instead.
    bars = tuple(
        _bar(i, contract_id="rb", product="RB", duration=60, start=BASE + i * M,
             end=BASE + (50 if i == 20 else 100 + i) * M,
             trading_day=date(2026, 3, 2), available_at=BASE + (100 + i) * M + 10**9,
             volume=Decimal(1000 + i))
        for i in range(21))
    bundle = FrozenInput(Path("SYNTHETIC"), "0" * 64, (_instrument("rb", "RB"),),
                         (), bars, (), ())
    decision = BASE + 130 * M
    full = d.visible_bars(bundle, "rb", 60, decision, False)
    recent = d.visible_bars_recent(bundle, "rb", 60, decision, False, 20)
    assert [b.bar_id for b in recent] == [f"rb-60-{i}" for i in range(20)]
    assert f"rb-60-20" not in {b.bar_id for b in recent}
    naive_suffix = full[-20:]
    assert {b.bar_id for b in naive_suffix} != {b.bar_id for b in recent}
    rule = _rule()
    assert capacity_before(recent, "rb", 60, decision, rule) == \
        capacity_before(full, "rb", 60, decision, rule)


def test_capacity_before_keeps_direct_input_semantics_untouched():
    # Direct capacity inputs are NOT filtered by visible_bars' stricter
    # READY predicate: DEGRADED observations remain usable, unknown rule
    # evidence still yields None, and the distinct-start/volume checks hold.
    rule = _rule()
    degraded = tuple(
        _bar(i, contract_id="rb", product="RB", duration=60, start=BASE + i * M,
             trading_day=date(2026, 3, 2), available_at=BASE + (i + 1) * M,
             status="DEGRADED", volume=Decimal(900))
        for i in range(20))
    assert capacity_before(degraded, "rb", 60, BASE + 25 * M, rule) == 90
    unknown = TradingRule(**{**rule.__dict__, "evidence": "UNKNOWN"})
    assert capacity_before(degraded, "rb", 60, BASE + 25 * M, unknown) is None
    no_volume = tuple(
        _bar(i, contract_id="rb", product="RB", duration=60, start=BASE + i * M,
             trading_day=date(2026, 3, 2), available_at=BASE + (i + 1) * M,
             volume=Decimal(1))
        for i in range(19)) + (
        _bar(19, contract_id="rb", product="RB", duration=60, start=BASE + 19 * M,
             trading_day=date(2026, 3, 2), available_at=BASE + 20 * M, volume=None),)
    assert capacity_before(no_volume, "rb", 60, BASE + 25 * M, rule) is None
    repeated_start = tuple(
        _bar(i, contract_id="rb", product="RB", duration=60,
             start=BASE + (i // 2) * M, trading_day=date(2026, 3, 2),
             available_at=BASE + (i + 1) * M, volume=Decimal(900))
        for i in range(20))
    assert capacity_before(repeated_start, "rb", 60, BASE + 25 * M, rule) is None
    short = degraded[:19]
    assert capacity_before(short, "rb", 60, BASE + 25 * M, rule) is None


def test_conflicting_duplicates_outside_and_inside_the_window():
    clean = tuple(
        _bar(i, contract_id="rb", product="RB", duration=60, start=BASE + (i + 2) * M,
             trading_day=date(2026, 3, 2), available_at=BASE + (i + 3) * M,
             volume=Decimal(1000 + i))
        for i in range(25))

    def conflict(volume: Decimal, start: int) -> tuple:
        return (
            _bar(90, contract_id="rb", product="RB", duration=60, start=start,
                 trading_day=date(2026, 3, 2), available_at=start + M,
                 volume=volume),
            _bar(91, contract_id="rb", product="RB", duration=60, start=start,
                 trading_day=date(2026, 3, 2), available_at=start + M,
                 volume=volume + Decimal(7)),
        )

    rule = _rule()
    decision = BASE + 40 * M
    # Outside the window: the conflicting pair sits earliest and the last-20
    # capacity window never sees it; the raw population keeps both bars and
    # the capacity value matches the clean bundle exactly.
    early_pair = conflict(Decimal("5"), BASE)
    with_pair = FrozenInput(Path("SYNTHETIC"), "0" * 64, (_instrument("rb", "RB"),),
                            (), early_pair + clean, (), ())
    clean_bundle = FrozenInput(Path("SYNTHETIC"), "0" * 64, (_instrument("rb", "RB"),),
                               (), clean, (), ())
    full = d.visible_bars(with_pair, "rb", 60, decision, False)
    assert len(full) == 27  # raw population: no dedup, no error
    recent = d.visible_bars_recent(with_pair, "rb", 60, decision, False, 20)
    # Capacity's own window over 25 clean bars is the LAST 20 by
    # (end_nano, bar_id) — clean[5:25]; the early conflicting pair is
    # outside it, so the capacity value matches the clean bundle exactly.
    assert {b.bar_id for b in recent} == {f"rb-60-{i}" for i in range(5, 25)}
    assert capacity_before(recent, "rb", 60, decision, rule) == \
        capacity_before(d.visible_bars(clean_bundle, "rb", 60, decision, False),
                        "rb", 60, decision, rule)
    # The context-path DAILY contract still rejects the conflict over the
    # complete population (factors layer, unchanged).
    with pytest.raises(FactorsError, match="conflicting_duplicate_bar"):
        _deduplicate_visible(list(full))
    # Inside the window: the conflicting pair is newest, the window contains
    # both duplicates, the 20-distinct-starts check fails, and both the
    # recent and full-population paths agree on None.
    late_pair = conflict(Decimal("5"), BASE + 40 * M)
    inside = FrozenInput(Path("SYNTHETIC"), "0" * 64, (_instrument("rb", "RB"),),
                         (), clean + late_pair, (), ())
    late_decision = BASE + 80 * M
    assert capacity_before(
        d.visible_bars_recent(inside, "rb", 60, late_decision, False, 20),
        "rb", 60, late_decision, rule) is None
    assert capacity_before(
        d.visible_bars(inside, "rb", 60, late_decision, False),
        "rb", 60, late_decision, rule) is None


def test_capacity_stable_across_eviction_multiple_bundles_and_decreasing_queries():
    rule = _rule()
    decision = BASE + 3 * 86_400 * 10**9
    first, second = _random_bundle(31), _random_bundle(32)
    before = capacity_before(
        d.visible_bars_recent(first, "rb", 60, decision, False, 20),
        "rb", 60, decision, rule)
    original_budget = d._INDEX_CACHE_MAX_RETAINED_BYTES
    try:
        # Force every query to rebuild (zero-entry budget) and interleave a
        # second bundle identity: values must not drift.
        d._INDEX_CACHE_MAX_RETAINED_BYTES = 1
        rebuilt = capacity_before(
            d.visible_bars_recent(first, "rb", 60, decision, False, 20),
            "rb", 60, decision, rule)
        other = capacity_before(
            d.visible_bars_recent(second, "rb", 60, decision, False, 20),
            "rb", 60, decision, rule)
        again = capacity_before(
            d.visible_bars_recent(first, "rb", 60, decision, False, 20),
            "rb", 60, decision, rule)
    finally:
        d._INDEX_CACHE_MAX_RETAINED_BYTES = original_budget
    assert before == rebuilt == again
    assert other == capacity_before(
        _reference_visible_bars(second, "rb", 60, decision, False),
        "rb", 60, decision, rule)
    # Decreasing decision times recompute the window, never reuse it.
    later = decision + 86_400 * 10**9
    at_later = capacity_before(
        d.visible_bars_recent(first, "rb", 60, later, False, 20),
        "rb", 60, later, rule)
    at_earlier = capacity_before(
        d.visible_bars_recent(first, "rb", 60, decision, False, 20),
        "rb", 60, decision, rule)
    assert (at_later, at_earlier) == (
        capacity_before(_reference_visible_bars(first, "rb", 60, later, False),
                        "rb", 60, later, rule),
        capacity_before(_reference_visible_bars(first, "rb", 60, decision, False),
                        "rb", 60, decision, rule))


def test_count_larger_than_population_returns_everything_in_capacity_order():
    bundle = _random_bundle(33)
    decision = BASE + 86_400 * 10**9  # early: only a few bars are visible
    full = d.visible_bars(bundle, "rb", 60, decision, False)
    recent = d.visible_bars_recent(bundle, "rb", 60, decision, False, 20)
    assert tuple(recent) == _capacity_window(full)
    assert len(recent) == len(full)
    assert d.visible_bars_recent(bundle, "rb", 60, decision, False, 0) == ()
