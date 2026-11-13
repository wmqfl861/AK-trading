"""SYNTHETIC K10b calendars: identity, context work, labels and bounded retention."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date
import gc
import json
import threading
import weakref

import pytest

from ak_trading.v2 import clock
from ak_trading.v2.artifacts import canonical_json_bytes
from ak_trading.v2.contracts import Horizon, SessionWindow
from ak_trading.v2.decision import pipeline
from ak_trading.v2.probability import labels
from ak_trading.v2.probability.forecast import predict_market
from ak_trading.v2.schemas import encode_record
from ak_trading.v2.strategies.registry import build_strategy_spec
from test_v2_m4_ledger import NS, account, run_policy
from test_v2_m4_replay import replay_inputs


PRODUCT = "SHFE.RB"
DAY = date(2026, 1, 12)
BASE = 1_770_000_000_000_000_000


def session(number=0, *, product=PRODUCT, start=BASE, minutes=2,
            kind="DAY", evidence="SCENARIO"):
    return SessionWindow(f"SYNTHETIC-{number}", product, DAY, start,
                         start + minutes * NS, kind, "SYNTHETIC-K10B", evidence)


@pytest.fixture(autouse=True)
def clear_caches():
    pipeline._clear_product_sessions_cache()
    labels._clear_session_starts_cache()
    clock._clear_calendar_index_cache()
    yield
    pipeline._clear_product_sessions_cache()
    labels._clear_session_starts_cache()
    clock._clear_calendar_index_cache()


def cache_api(kind):
    if kind == "pipeline":
        return (pipeline, pipeline._cached_product_sessions,
                pipeline._PRODUCT_SESSIONS_CACHE,
                pipeline._clear_product_sessions_cache,
                "_PRODUCT_SESSIONS_MAX_ENTRIES", "_PRODUCT_SESSIONS_MAX_REFS")
    return (labels, labels._product_sessions_ordered, labels._SESSION_STARTS_CACHE,
            labels._clear_session_starts_cache,
            "_SESSION_STARTS_MAX_ENTRIES", "_SESSION_STARTS_MAX_REFS")


def values(kind, result):
    return result if kind == "pipeline" else result[1]


def retained_refs(kind, cache):
    if kind == "pipeline":
        return sum(len(entry.source) + len(entry.filtered) for entry in cache.values())
    return sum(len(entry.source) + 2 * len(entry.result[1]) for entry in cache.values())


def context(bundle, models, payoff, reference, at=70 * NS, contract="rb"):
    spec = build_strategy_spec("TREND", "MINUTE")
    return pipeline.build_strategy_context(
        bundle, contract, at, account("100000"), None, models, payoff, spec, None,
        reference, run_policy(), (), exit_binding=pipeline.legacy_exit_binding(spec),
        forecast_provider=lambda model, request, regime: predict_market(
            model, request, regime, request.features.decision_nano))


def test_real_contexts_reuse_sessions_without_revalidating_or_expanding_minutes():
    bundle, models, payoff, reference = replay_inputs()
    before = clock._index_stats()
    first = context(bundle, models, payoff, reference)
    first_stats = clock._index_stats()
    assert first_stats["session_validations"] > before["session_validations"]
    assert first_stats["minute_slot_materializations"] > before["minute_slot_materializations"]
    for at in [70 * NS, 70 * NS, *(i * NS for i in range(71, 92))]:
        current = context(bundle, models, payoff, reference, at)
        assert current.sessions is first.sessions
        if at == 70 * NS:
            assert current == first
    last_stats = clock._index_stats()
    assert last_stats == first_stats
    print("K10B_CONTEXT_COUNTERS " + json.dumps(
        {"before": before, "first": first_stats, "last": last_stats}, sort_keys=True))
    # A separate bundle and another contract may share the same calendar object.
    shared_bundle = replace(bundle, manifest_sha256="b" * 64)
    assert context(shared_bundle, models, payoff, reference).sessions is first.sessions
    second = replace(bundle.instruments[0], contract_id="rb-other")
    other_contract = replace(bundle, instruments=bundle.instruments + (second,),
        rules=bundle.rules + (replace(bundle.rules[0], contract_id="rb-other"),),
        bars=bundle.bars + tuple(replace(bar, contract_id="rb-other") for bar in bundle.bars))
    assert context(other_contract, models, payoff, reference,
                   contract="rb-other").sessions is first.sessions
    distinct = replace(bundle, sessions=tuple(list(bundle.sessions)))
    rebuilt = context(distinct, models, payoff, reference)
    assert rebuilt.sessions == first.sessions and rebuilt.sessions is not first.sessions
    pipeline._clear_product_sessions_cache()
    clock._clear_calendar_index_cache()
    assert context(bundle, models, payoff, reference) == first


def test_filter_preserves_order_duplicates_objects_and_unknown_evidence():
    later = session(1, start=BASE + 4 * NS)
    first = session(2)
    tied = session(3, evidence="UNKNOWN")
    other = session(4, product="SHFE.CU")
    source = (later, other, first, tied, first)
    filtered = pipeline._cached_product_sessions(source, PRODUCT)
    assert filtered == (later, first, tied, first)
    assert all(a is b for a, b in zip(filtered, (later, first, tied, first)))
    starts, ordered = labels._product_sessions_ordered(source, PRODUCT)
    assert ordered == (first, tied, first, later)
    assert ordered[0] is first and ordered[1] is tied and ordered[2] is first
    assert starts == (BASE, BASE, BASE, BASE + 4 * NS)
    assert pipeline._cached_product_sessions(source, "SHFE.CU") == (other,)
    assert labels._product_sessions_ordered(source, "SHFE.CU") == ((BASE,), (other,))


@pytest.mark.parametrize("kind", ["pipeline", "labels"])
def test_identity_product_and_empty_results_are_separate(kind):
    _, call, cache, _, _, _ = cache_api(kind)
    source = (session(), session(1, product="SHFE.CU"))
    first = call(source, PRODUCT)
    assert call(source, PRODUCT) is first
    equal_source = tuple(list(source))
    equal = call(equal_source, PRODUCT)
    assert equal == first and equal is not first
    other = call(source, "SHFE.CU")
    assert values(kind, other) == (source[1],)
    missing = call(source, "MISSING")
    assert values(kind, missing) == ()
    assert (id(source), "MISSING") not in cache
    assert values(kind, call((), PRODUCT)) == ()
    assert (id(()), PRODUCT) not in cache


@pytest.mark.parametrize("kind", ["pipeline", "labels"])
def test_wrong_source_at_same_identity_key_is_rebuilt_deterministically(kind):
    _, call, cache, _, _, _ = cache_api(kind)
    old = (session(), session(1, start=BASE + 4 * NS))
    new = tuple(replace(s, start_nano=s.start_nano + 60 * NS,
                        end_nano=s.end_nano + 60 * NS) for s in old)
    prior = call(old, PRODUCT)
    cache[(id(new), PRODUCT)] = cache.pop((id(old), PRODUCT))
    result = call(new, PRODUCT)
    assert result is not prior and values(kind, result) == new
    assert cache[(id(new), PRODUCT)].source is new
    if kind == "labels":
        slots = labels._expected_minute_slots(new, PRODUCT, BASE, BASE + 70 * NS)
        assert slots[0] == (BASE + 60 * NS, BASE + 60 * NS, BASE + 61 * NS)
        assert all(row[0] >= BASE + 60 * NS for row in slots)


@pytest.mark.parametrize("kind", ["pipeline", "labels"])
def test_source_lifetime_is_retained_and_clear_releases_unselected_sessions(kind):
    _, call, _, clear, _, _ = cache_api(kind)
    source = (session(product="SHFE.CU"), session(1))
    unselected = weakref.ref(source[0])
    result = call(source, PRODUCT)
    del source
    gc.collect()
    assert unselected() is not None
    clear()
    gc.collect()
    assert unselected() is None
    assert values(kind, result)[0].session_id == "SYNTHETIC-1"


class MutableTuple(tuple):
    def __new__(cls, items):
        result = super().__new__(cls, items)
        result.current = list(items)
        return result

    def __iter__(self):
        return iter(self.current)


class DerivedSession(SessionWindow):
    pass


@pytest.mark.parametrize("kind", ["pipeline", "labels"])
@pytest.mark.parametrize("form", ["list", "tuple_subclass", "session_subclass"])
def test_noncacheable_inputs_recompute_without_new_rejections(kind, form):
    _, call, cache, _, _, _ = cache_api(kind)
    original = session()
    shifted = session(1, start=BASE + 60 * NS)
    if form == "list":
        source = [original]
    elif form == "tuple_subclass":
        source = MutableTuple([original])
    else:
        source = (DerivedSession(**vars(original)),)
    first = call(source, PRODUCT)
    if form == "list":
        source[0] = shifted
    elif form == "tuple_subclass":
        source.current[:] = [shifted]
    second = call(source, PRODUCT)
    assert first is not second and not cache
    assert values(kind, second)[0] is (source[0] if form == "session_subclass" else shifted)


@pytest.mark.parametrize("kind", ["pipeline", "labels"])
def test_failed_construction_keeps_original_error_and_publishes_nothing(kind):
    _, call, cache, _, _, _ = cache_api(kind)

    class Broken:
        @property
        def product(self):
            raise LookupError("SYNTHETIC-product-failure", 7)

    for _ in range(2):
        with pytest.raises(LookupError) as error:
            call((Broken(),), PRODUCT)
        assert error.value.args == ("SYNTHETIC-product-failure", 7)
        assert not cache


def test_label_slots_keep_night_lunch_and_half_open_boundaries():
    night = session(0, minutes=3, kind="NIGHT")
    morning = session(1, start=BASE + 12 * 60 * NS)
    afternoon = session(2, start=BASE + 16 * 60 * NS)
    source = (afternoon, session(3, product="SHFE.CU"), night, morning)
    starts = (BASE + NS, BASE + 2 * NS, morning.start_nano,
              morning.start_nano + NS, afternoon.start_nano)
    expected = tuple((t, t, t + NS) for t in starts)
    for clear in (False, False, True):
        if clear:
            labels._clear_session_starts_cache()
        actual = labels._expected_minute_slots(
            source, PRODUCT, BASE + NS, afternoon.start_nano + NS)
        assert actual == expected
    assert labels._expected_minute_slots(source, PRODUCT, BASE + 1, BASE + 2 * NS) == (
        (BASE + NS, BASE + NS, BASE + 2 * NS),)
    assert labels._expected_minute_slots(source, PRODUCT, BASE + NS, BASE + NS) == ()


def test_label_alignment_error_is_identical_cold_warm_and_after_clear():
    broken = (replace(session(), end_nano=BASE + NS + 1),)
    for clear in (False, False, True):
        if clear:
            labels._clear_session_starts_cache()
        with pytest.raises(labels.LabelError) as error:
            labels._expected_minute_slots(broken, PRODUCT, BASE, BASE + 3 * NS)
        assert error.value.args == ("session_not_minute_aligned",)


def test_real_labels_keep_hand_computed_outcome_and_cold_warm_clear_bytes():
    from test_v2_m3a_labels import (
        BASE as label_base, MINUTE, as_of_after, make_request, make_session, minute_bar)
    request = make_request(label_base, label_base + 5 * MINUTE,
                           Horizon("TRADING_MINUTE", 5))
    bars = (
        minute_bar(label_base, 103, 103, 103, 103),
        minute_bar(label_base + MINUTE, 102, 102, 97, 97),
        minute_bar(label_base + 2 * MINUTE, 97, 98, 96.5, 97.5),
        minute_bar(label_base + 3 * MINUTE, 97.5, 98, 97, 97.5),
        minute_bar(label_base + 4 * MINUTE, 97.5, 98, 97, 98),
    )
    source = (make_session(label_base, 240),)
    results = []
    for clear in (False, False, True):
        if clear:
            labels._clear_session_starts_cache()
        label = labels.label_forecast(request, bars, source, (),
                                      as_of_after(label_base + 5 * MINUTE))
        assert label.status == "MATURE" and label.path_outcome == "UP_FIRST"
        assert label.first_up_step == 1 and label.first_down_step == 2
        results.append(canonical_json_bytes(encode_record(label)))
    assert results[0] == results[1] == results[2]


@pytest.mark.parametrize("kind", ["pipeline", "labels"])
def test_lru_entry_limit_reuses_hot_key_and_rebuilds_evicted_source(kind, monkeypatch):
    module, call, cache, _, entry_limit, _ = cache_api(kind)
    monkeypatch.setattr(module, entry_limit, 2)
    sources = [(session(i, start=BASE + i * 3 * NS),) for i in range(3)]
    a, b = (call(source, PRODUCT) for source in sources[:2])
    assert call(sources[0], PRODUCT) is a
    call(sources[2], PRODUCT)
    assert list(cache) == [(id(sources[0]), PRODUCT), (id(sources[2]), PRODUCT)]
    rebuilt = call(sources[1], PRODUCT)
    assert rebuilt == b and rebuilt is not b
    assert len(cache) == 2


@pytest.mark.parametrize("kind,unit_refs", [("pipeline", 3), ("labels", 4)])
def test_reference_budget_and_oversized_inputs_preserve_complete_results(kind, unit_refs, monkeypatch):
    module, call, cache, clear, _, ref_limit = cache_api(kind)
    monkeypatch.setattr(module, ref_limit, 2 * unit_refs)
    sources = [(session(i), session(10 + i, product="SHFE.CU")) for i in range(3)]
    first = call(sources[0], PRODUCT)
    second = call(sources[1], PRODUCT)
    assert retained_refs(kind, cache) == 2 * unit_refs and len(cache) == 2
    call(sources[2], PRODUCT)
    assert (id(sources[0]), PRODUCT) not in cache
    assert retained_refs(kind, cache) == 2 * unit_refs and len(cache) == 2
    oversized = tuple(session(20 + i, start=BASE + i * 3 * NS) for i in range(4))
    large = call(oversized, PRODUCT)
    assert values(kind, large) == oversized
    assert call(oversized, PRODUCT) == large and call(oversized, PRODUCT) is not large
    assert (id(oversized), PRODUCT) not in cache and len(cache) == 2
    clear()
    assert not cache and retained_refs(kind, cache) == 0
    assert values(kind, first) == (sources[0][0],)
    assert values(kind, second) == (sources[1][0],)


def test_labels_direct_container_clear_has_no_ghost_occupancy(monkeypatch):
    monkeypatch.setattr(labels, "_SESSION_STARTS_MAX_REFS", 6)
    sources = [(session(i, start=BASE + i * 3 * NS),) for i in range(5)]
    for source in sources[:2]:
        labels._product_sessions_ordered(source, PRODUCT)
    assert retained_refs("labels", labels._SESSION_STARTS_CACHE) == 6
    labels._SESSION_STARTS_CACHE.clear()  # The unchanged Z09 probe's operation.
    assert not labels._SESSION_STARTS_CACHE
    assert retained_refs("labels", labels._SESSION_STARTS_CACHE) == 0
    for source in sources[2:4]:
        result = labels._product_sessions_ordered(source, PRODUCT)
        assert labels._product_sessions_ordered(source, PRODUCT) is result
    assert retained_refs("labels", labels._SESSION_STARTS_CACHE) == 6
    labels._product_sessions_ordered(sources[4], PRODUCT)
    assert list(labels._SESSION_STARTS_CACHE) == [
        (id(sources[3]), PRODUCT), (id(sources[4]), PRODUCT)]
    assert retained_refs("labels", labels._SESSION_STARTS_CACHE) == 6


@pytest.mark.parametrize("kind", ["pipeline", "labels"])
def test_concurrent_first_miss_publishes_one_result(kind):
    _, call, cache, _, _, _ = cache_api(kind)
    source = (session(), session(1, start=BASE + 3 * NS))
    barrier = threading.Barrier(8)

    def work(_):
        barrier.wait()
        return call(source, PRODUCT)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(work, range(8)))
    assert all(result is results[0] for result in results)
    assert len(cache) == 1 and cache[(id(source), PRODUCT)].source is source


@pytest.mark.parametrize("kind", ["pipeline", "labels"])
def test_concurrent_calls_clear_and_eviction_keep_calendars_and_bounds(kind, monkeypatch):
    module, call, cache, clear, entry_limit, ref_limit = cache_api(kind)
    monkeypatch.setattr(module, entry_limit, 2)
    monkeypatch.setattr(module, ref_limit, 6)
    sources = [(session(i, start=BASE + i * 60 * NS),) for i in range(8)]
    barrier = threading.Barrier(8)

    def work(worker):
        barrier.wait()
        for index in range(30):
            if (worker + index) % 7 == 0:
                clear()
            source = sources[(worker + index) % len(sources)]
            result = call(source, PRODUCT)
            assert values(kind, result) == source
        return True

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(pool.map(work, range(8)))
    assert 0 <= retained_refs(kind, cache) <= 6 and len(cache) <= 2
    for key, entry in cache.items():
        assert key == (id(entry.source), PRODUCT)
    clear()
    assert not cache and retained_refs(kind, cache) == 0
