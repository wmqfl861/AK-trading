"""K14: independent pre-change oracles, mutable aliases and bounded ownership.

All inputs are SYNTHETIC. The reference algorithms/projection below come from
the frozen pre-K14 source; they do not call production cache preparation.
"""
from __future__ import annotations

import copy
import dataclasses
import gc
import hashlib
import json
import math
import random
import struct
import weakref
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import Mapping

import pytest

from ak_trading.v2.contracts import CalibrationEvidence, Horizon
from ak_trading.v2.probability import models as subject
from ak_trading.v2.probability.calibration import evidence_projection_without_back_references
from ak_trading.v2.schemas import SchemaError, canonical_json_bytes

LEVELS = ("preferred", "secondary", "fallback")
QUANTILES = [0.05, 0.25, 0.5, 0.75, 0.95]


def old_quantile(values, quantile):
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * quantile
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return values[int(position)]
    fraction = position - low
    return values[low] * (1 - fraction) + values[high] * fraction


def old_apply_quantile(model, levels, quantiles):
    samples, counts = model["samples"], model["counts"]
    if not isinstance(samples, Mapping) or not isinstance(counts, Mapping):
        return None
    minimum = counts.get("minimum_leaf_blocks")
    minimum = int(minimum[0]) if isinstance(minimum, list) and minimum else 10
    chosen = None
    for level in levels:
        entry = samples.get(level)
        if isinstance(entry, list) and len(entry) >= minimum:
            chosen = entry
            break
    if chosen is None:
        chosen = samples.get(levels[-1])
    if not isinstance(chosen, list) or not chosen:
        return None
    return [old_quantile([float(value) for value in chosen], q) for q in quantiles]


def old_apply_cdf(model, levels, horizon):
    samples = model["samples"]
    if not isinstance(samples, Mapping):
        return None
    chosen, total = None, 0
    for level in levels:
        entry = samples.get(level)
        if isinstance(entry, list) and entry:
            chosen = [float(value) for value in entry]
            total = len(chosen)
            break
    if chosen is None or total == 0:
        return None
    unknown = sum(1 for value in chosen if value < 0) / total
    return [(sum(1 for value in chosen if 0 < value <= step) / total, unknown)
            for step in range(1, horizon + 1)]


def old_hash(model_set):
    evidence = [evidence_projection_without_back_references(row)
                for row in model_set.evidence]
    evidence.sort(key=canonical_json_bytes)
    fingerprints = [canonical_json_bytes(row) for row in evidence]
    if len(set(fingerprints)) != len(fingerprints):
        raise subject.FitError("duplicate_evidence_identity_in_projection")
    payload = {
        "regime_reference_id": model_set.regime_reference_id,
        "feature_keys": list(model_set.feature_keys),
        "training_end_nano": model_set.training_end_nano,
        "calibration_end_nano": model_set.calibration_end_nano,
        "valid_until_nano": model_set.valid_until_nano,
        "fit_config": dict(model_set.fit_config),
        "models": {key: dict(value) for key, value in sorted(model_set.models.items())},
        "source_hashes": dict(sorted(model_set.source_hashes.items())),
        "evidence": evidence,
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def bundle(model, *, config=None, evidence=(), sources=None):
    return subject.ForecastModelSet("synthetic-model", "synthetic-regime", ("x",),
        1, 2, 3, config if config is not None else {"values": [1, 2]},
        {"model": model}, evidence, sources if sources is not None else {"SYNTHETIC": "0" * 64})


def evidence_row():
    return CalibrationEvidence("evidence", "model", "policy", "SYN.rb", "DAY", 60,
        "UP_TREND", "COMMON", "NONE", "DIRECTION", Horizon("TRADING_MINUTE", 1),
        "label", "ALL", 600, 80, 0.01, 0.01, 1, 2, 3, "VALID")


def cdf_fit(values):
    return subject._fit_empirical_cdf(values, [LEVELS] * len(values), ["x"], [0.0], [1.0])


def quantile_fit(values, minimum=1):
    return subject._fit_empirical_quantile(values, [LEVELS] * len(values), QUANTILES,
                                            ["x"], [0.0], [1.0], minimum)


def plain_fitted(model):
    # The approved internal change is tuple -> JSON array; translate just
    # fitted samples to the original list representation for the old oracle.
    return {**model, "samples": {key: list(value) for key, value in model["samples"].items()}}


def bits(value):
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return tuple(bits(item) for item in value)
    return struct.pack("!d", value)


def outcome(call):
    try:
        return ("value", bits(call()))
    except Exception as exc:
        return (type(exc), str(exc))


@pytest.mark.parametrize("size", [0, 1, 3, 17, 300])
@pytest.mark.parametrize("horizon", [-3, 0, 1, 20, 60, 130])
def test_cdf_randomized_owned_and_plain_are_bitwise_old(size, horizon):
    rng = random.Random(701 + size)
    population = [-8, -0.0, 0.0, 0.1, 1, 1.01, 2, 20, 61, 150]
    fitted = cdf_fit([rng.choice(population) for _ in range(size)])
    plain = plain_fitted(fitted)
    expected = bits(old_apply_cdf(plain, LEVELS, horizon))
    for model in (fitted, plain):
        assert bits(subject._apply_empirical_cdf(model, LEVELS, horizon)) == expected
        assert bits(subject._apply_empirical_cdf(model, LEVELS, horizon)) == expected


@pytest.mark.parametrize("size", [0, 1, 3, 17, 300])
@pytest.mark.parametrize("quantiles", [[], QUANTILES, [1.0, 0.0, 0.5, 0.5], [-0.1, 0.5], [1.01], [float("nan")]])
def test_quantile_randomized_owned_and_plain_are_bitwise_old(size, quantiles):
    rng = random.Random(904 + size)
    fitted = quantile_fit([rng.uniform(-8, 8) for _ in range(size)])
    plain = plain_fitted(fitted)
    expected = outcome(lambda: old_apply_quantile(plain, LEVELS, quantiles))
    for model in (fitted, plain):
        assert outcome(lambda: subject._apply_empirical_quantile(model, LEVELS, quantiles)) == expected


@pytest.mark.parametrize("values", [[-0.0], [0.0], [-math.inf, -0.0, 0.0, 0.1, math.inf, math.nan]])
def test_signed_zero_and_nonfinite_cdf_conversion(values):
    fitted = cdf_fit(values)
    assert bits(subject._apply_empirical_cdf(fitted, LEVELS, 3)) == bits(old_apply_cdf(plain_fitted(fitted), LEVELS, 3))
    if len(values) == 1:
        fitted = quantile_fit(values)
        assert bits(subject._apply_empirical_quantile(fitted, LEVELS, QUANTILES)) == bits(old_apply_quantile(plain_fitted(fitted), LEVELS, QUANTILES))


@pytest.mark.parametrize("kind", ["cdf", "quantile"])
def test_level_selection_and_count_changes_are_rechecked(kind):
    fitted = cdf_fit([1, 2, 4]) if kind == "cdf" else quantile_fit([1, 2, 4])
    apply = subject._apply_empirical_cdf if kind == "cdf" else subject._apply_empirical_quantile
    oracle = old_apply_cdf if kind == "cdf" else old_apply_quantile
    query = 5 if kind == "cdf" else QUANTILES
    for preferred, secondary, fallback, minimum in [
            ([], [4, 5], [8, 9], 2), ([], [], [1], 10), ([], [], [], 10),
            ([1, 2, 3], [9], [7], 1), ([1, 2, 3], [9], [7], 4)]:
        fitted["samples"] = dict(zip(LEVELS, (preferred, secondary, fallback)))
        fitted["counts"] = {"minimum_leaf_blocks": [minimum]}
        assert outcome(lambda: apply(fitted, LEVELS, query)) == outcome(lambda: oracle(fitted, LEVELS, query))


def test_giant_int_remains_lazy_and_failed_conversion_is_not_cached():
    model = quantile_fit([10 ** 400])
    assert subject._apply_empirical_quantile(model, LEVELS, []) == []
    for _ in range(2):
        assert outcome(lambda: subject._apply_empirical_quantile(model, LEVELS, [0.5])) == outcome(
            lambda: old_apply_quantile(plain_fitted(model), LEVELS, [0.5]))
        assert model["samples"][LEVELS[0]]._float_values is None


@pytest.mark.parametrize("horizon", [0, -1, 1.5])
def test_cdf_preserves_conversion_before_horizon_failure(horizon):
    for values in [[10 ** 400], [1.0]]:
        model = {"samples": {LEVELS[0]: values}}
        assert outcome(lambda: subject._apply_empirical_cdf(model, LEVELS, horizon)) == outcome(
            lambda: old_apply_cdf(model, LEVELS, horizon))


def test_only_exact_owned_samples_receive_the_immutable_certificate():
    class Number(float):
        pass
    class Values(list):
        pass
    fitted = quantile_fit([Number(1), Number(2)])
    assert type(fitted["samples"][LEVELS[0]]) is list
    assert subject._apply_empirical_quantile(fitted, LEVELS, [0.5]) == [1.5]
    with pytest.raises(TypeError, match="builtin_numeric"):
        subject._OwnedSamples([Number(1)])
    for values in [(1.0, 2.0), ([1.0], [2.0])]:
        model = {"samples": {level: values for level in LEVELS}, "counts": {}}
        assert subject._apply_empirical_cdf(model, LEVELS, 3) is None
        assert subject._apply_empirical_quantile(model, LEVELS, [0.5]) is None
    mutable = Values([1.0, 2.0, 3.0])
    model = {"samples": {level: mutable for level in LEVELS}, "counts": {"minimum_leaf_blocks": [1]}}
    subject._apply_empirical_cdf(model, LEVELS, 3)
    list.__setitem__(mutable, 1, 20.0)
    assert bits(subject._apply_empirical_cdf(model, LEVELS, 3)) == bits(old_apply_cdf(model, LEVELS, 3))
    assert bits(subject._apply_empirical_quantile(model, LEVELS, [0.5])) == bits(old_apply_quantile(model, LEVELS, [0.5]))


def test_plain_lists_mutate_through_shared_models_and_model_sets():
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    model = {"samples": {level: values for level in LEVELS}, "counts": {"minimum_leaf_blocks": [1]}}
    other_model = {**model}
    sets = (bundle(model), bundle(other_model))
    changes = [lambda: values.__setitem__(2, 17.0), lambda: values.__setitem__(slice(1, 3), [8.0, 9.0]),
               lambda: values.append(-1.0), lambda: values.pop(0), values.reverse, values.sort, values.clear,
               lambda: values.extend([6.0, 7.0])]
    for mutate in [lambda: None, *changes]:
        mutate()
        for current in (model, other_model):
            assert bits(subject._apply_empirical_cdf(current, LEVELS, 10)) == bits(old_apply_cdf(current, LEVELS, 10))
            assert bits(subject._apply_empirical_quantile(current, LEVELS, QUANTILES)) == bits(old_apply_quantile(current, LEVELS, QUANTILES))
        for current in sets:
            assert current.content_hash() == old_hash(current)


def test_model_dictionary_update_delete_clear_and_sample_replacement():
    model = quantile_fit([1, 2, 3])
    current = bundle(model)
    current.content_hash()
    for operation in [lambda: model["samples"].__setitem__(LEVELS[0], [8.0, 9.0]),
                      lambda: model["samples"].__delitem__(LEVELS[0]),
                      lambda: model.update(samples={LEVELS[-1]: [10.0]}),
                      lambda: model["counts"].update(minimum_leaf_blocks=[2]),
                      lambda: model["samples"].clear()]:
        operation()
        assert current.content_hash() == old_hash(current)
        assert outcome(lambda: subject._apply_empirical_quantile(model, LEVELS, QUANTILES)) == outcome(lambda: old_apply_quantile(plain_fitted(model), LEVELS, QUANTILES))
    del model["samples"]
    assert outcome(lambda: subject._apply_empirical_quantile(model, LEVELS, QUANTILES)) == outcome(lambda: old_apply_quantile(model, LEVELS, QUANTILES))
    assert current.content_hash() == old_hash(current)
    model.clear()
    assert current.content_hash() == old_hash(current)


def test_owned_samples_reject_item_writes_and_return_fresh_outputs():
    model = cdf_fit([1, 2, 3])
    samples = model["samples"][LEVELS[0]]
    with pytest.raises(TypeError):
        samples[1] = 100
    first = subject._apply_empirical_cdf(model, LEVELS, 3)
    first[0] = (999.0, 999.0)
    assert bits(subject._apply_empirical_cdf(model, LEVELS, 3)) == bits(old_apply_cdf(plain_fitted(model), LEVELS, 3))
    quantile = quantile_fit([1, 2, 3])
    first = subject._apply_empirical_quantile(quantile, LEVELS, QUANTILES)
    first[:] = [999.0]
    assert bits(subject._apply_empirical_quantile(quantile, LEVELS, QUANTILES)) == bits(old_apply_quantile(plain_fitted(quantile), LEVELS, QUANTILES))


def test_json_roundtrip_preserves_hash_and_restores_mutable_aliases():
    model = quantile_fit([-0.0, 1, 2, 3])
    restored = json.loads(json.dumps(model))
    assert bundle(model).content_hash() == bundle(restored).content_hash()
    assert type(restored["samples"][LEVELS[0]]) is list
    values = restored["samples"][LEVELS[0]]
    subject._apply_empirical_quantile(restored, LEVELS, QUANTILES)
    values[1] = 50.0
    assert bits(subject._apply_empirical_quantile(restored, LEVELS, QUANTILES)) == bits(old_apply_quantile(restored, LEVELS, QUANTILES))


def test_constructor_preserves_shallow_copy_and_inner_aliases():
    inner = {"samples": {"x": [1, 2]}, "coefficients": [[0.0]]}
    outer = {"model": inner}
    config = {"nested": [1]}
    source = {"SYNTHETIC": "0" * 64}
    current = subject.ForecastModelSet("id", "ref", (), 1, 2, 3, config, outer, (), source)
    initial = current.content_hash()
    outer["model"] = {}
    config["nested"] = [99]
    source["SYNTHETIC"] = "1" * 64
    assert current.content_hash() == initial == old_hash(current)
    inner["coefficients"][0][0] = -0.0
    assert current.content_hash() == old_hash(current) != initial


@pytest.mark.parametrize("before,after", [(True, 1), (1, 1.0), (0.0, -0.0), (False, 0)])
def test_hash_signature_distinguishes_json_values_python_calls_equal(before, after):
    model = {"nested": {"value": [before]}}
    current = bundle(model)
    initial = current.content_hash()
    model["nested"]["value"][0] = after
    assert current.content_hash() == old_hash(current) != initial


def test_hash_covers_tuple_mutable_descendants_and_all_public_content():
    model = {"tuple": ([1, 2],), "coefficients": [[0.0]], "trees": [{"leaf": [0.5]}]}
    config = {"nested": {"rates": [0.1]}}
    row = evidence_row()
    current = bundle(model, config=config, evidence=(row,))
    changes = [lambda: model["tuple"][0].__setitem__(1, 7),
               lambda: model["coefficients"][0].__setitem__(0, 4.0),
               lambda: model["trees"][0]["leaf"].__setitem__(0, 0.9),
               lambda: config["nested"]["rates"].append(0.2),
               lambda: object.__setattr__(row, "sample_count", 601),
               lambda: object.__setattr__(row.horizon, "value", 2),
               lambda: object.__setattr__(current, "source_hashes", {"SYNTHETIC": "1" * 64}),
               lambda: object.__setattr__(current, "feature_keys", ("other",)),
               lambda: object.__setattr__(current, "regime_reference_id", "different")]
    for mutate in changes:
        previous = current.content_hash()
        mutate()
        assert current.content_hash() == old_hash(current) != previous


def test_hash_removes_only_backrefs_rechecks_duplicates_and_replace_is_uncached():
    row = evidence_row()
    current = bundle(quantile_fit([1, 2]), evidence=(row,))
    initial = current.content_hash()
    object.__setattr__(row, "evidence_id", "new-evidence")
    object.__setattr__(row, "model_id", "new-model")
    object.__setattr__(current, "model_set_id", "new-set")
    assert current.content_hash() == initial == old_hash(current)
    replacement = dataclasses.replace(current)
    assert not hasattr(replacement, "_content_hash_cache")
    assert replacement.content_hash() == initial
    object.__setattr__(current, "evidence", (row, dataclasses.replace(row, evidence_id="other")))
    for _ in range(2):
        with pytest.raises(subject.FitError, match="duplicate_evidence_identity"):
            current.content_hash()


@pytest.mark.parametrize("value", [Decimal("1.2"), object()])
def test_unsupported_hash_values_use_original_serializer_and_do_not_cache(value):
    current = bundle({"unsupported": value})
    def encoded(call):
        try:
            return ("value", call())
        except Exception as exc:
            return (type(exc), str(exc))
    assert encoded(current.content_hash) == encoded(lambda: old_hash(current))
    assert not hasattr(current, "_content_hash_cache")


def test_cyclic_inputs_keep_original_failure_and_can_be_repaired():
    cyclic = []
    cyclic.append(cyclic)
    current = bundle({"cycle": cyclic})
    with pytest.raises(SchemaError, match="canonical_json_encode_failed"):
        old_hash(current)
    with pytest.raises(SchemaError, match="canonical_json_encode_failed"):
        current.content_hash()
    assert not hasattr(current, "_content_hash_cache")
    cyclic[:] = [1]
    assert current.content_hash() == old_hash(current)


def test_owned_preparation_and_hash_reuse_have_deterministic_counts(monkeypatch):
    model = cdf_fit(list(range(1000)))
    sample = model["samples"][LEVELS[0]]
    calls = {"float": 0, "cdf": 0, "canonical": 0}
    original_count = subject._count_cdf
    def counted_float(value):
        calls["float"] += 1
        return float(value)
    # Patch float only while preparing: certification has already completed.
    monkeypatch.setattr(subject, "float", counted_float, raising=False)
    def counted_cdf(values):
        calls["cdf"] += 1
        return original_count(values)
    monkeypatch.setattr(subject, "_count_cdf", counted_cdf)
    for horizon in [1, 20, 60, 130] * 3:
        subject._apply_empirical_cdf(model, LEVELS, horizon)
    assert calls["float"] == len(sample) and calls["cdf"] == 1
    monkeypatch.delattr(subject, "float")
    quantile = quantile_fit(list(range(1000)))
    monkeypatch.setattr(subject, "float", counted_float, raising=False)
    before = calls["float"]
    for _ in range(5):
        subject._apply_empirical_quantile(quantile, LEVELS, QUANTILES)
    assert calls["float"] - before == 1000
    monkeypatch.delattr(subject, "float")
    current = bundle(quantile)
    def counted_canonical(value):
        calls["canonical"] += 1
        return canonical_json_bytes(value)
    monkeypatch.setattr(subject, "canonical_json_bytes", counted_canonical)
    expected = old_hash(current)
    for _ in range(10):
        assert current.content_hash() == expected
    assert calls["canonical"] == 1


def test_concurrent_owned_preparation_occurs_once(monkeypatch):
    model = cdf_fit(list(range(1000)))
    calls = []
    original = subject._count_cdf
    def count(values):
        calls.append(1)
        return original(values)
    monkeypatch.setattr(subject, "_count_cdf", count)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: subject._apply_empirical_cdf(model, LEVELS, 60), range(24)))
    assert len(calls) == 1
    assert all(bits(result) == bits(results[0]) for result in results)


def test_cache_lifetime_and_identity_isolation():
    class Marker:
        pass
    first, second = quantile_fit([1, 2, 3]), quantile_fit([1, 2, 3])
    sets = [bundle(first), bundle(second)]
    assert sets[0].content_hash() == sets[1].content_hash()
    first_token = subject._SampleIdentity(first["samples"][LEVELS[0]])
    second_token = subject._SampleIdentity(second["samples"][LEVELS[0]])
    assert first_token != second_token
    assert first_token == subject._SampleIdentity(first_token.source)
    sample = first["samples"][LEVELS[0]]
    sample.marker = Marker()
    marker_ref = weakref.ref(sample.marker)
    set_ref = weakref.ref(sets[0])
    subject._apply_empirical_quantile(first, LEVELS, QUANTILES)
    assert copy.deepcopy(sample) is sample
    del sample, first, second, first_token, second_token, sets
    gc.collect()
    assert marker_ref() is None and set_ref() is None


def test_owned_cache_does_not_accumulate_per_horizon_state():
    model = cdf_fit([0, 1, 2, 3, 8])
    sample = model["samples"][LEVELS[0]]
    subject._apply_empirical_cdf(model, LEVELS, 1)
    keys, counts, floats = set(sample.__dict__), sample._cdf_counts, sample._float_values
    for horizon in range(1, 101):
        subject._apply_empirical_cdf(model, LEVELS, horizon)
    assert set(sample.__dict__) == keys
    assert sample._cdf_counts is counts and sample._float_values is floats


def test_warm_hash_never_iterates_owned_training_samples(monkeypatch):
    current = bundle(quantile_fit(list(range(1000))))
    expected = current.content_hash()
    def reject_iteration(self):
        raise AssertionError("warm hash scanned an owned training sequence")
    monkeypatch.setattr(subject._OwnedSamples, "__iter__", reject_iteration)
    for _ in range(5):
        assert current.content_hash() == expected


def test_mutable_numeric_subclass_falls_back_and_observes_value_changes():
    class MutableNumber(float):
        multiplier = 1
        def __float__(self):
            return super().__float__() * self.multiplier
    value = MutableNumber(2)
    model = quantile_fit([value])
    assert type(model["samples"][LEVELS[0]]) is list
    assert subject._apply_empirical_quantile(model, LEVELS, [0.5]) == [2.0]
    value.multiplier = 4
    assert subject._apply_empirical_quantile(model, LEVELS, [0.5]) == [8.0]
    assert bits(subject._apply_empirical_quantile(model, LEVELS, QUANTILES)) == bits(old_apply_quantile(model, LEVELS, QUANTILES))


def test_list_subclass_empty_iteration_preserves_original_cdf_result():
    class EmptyIteration(list):
        def __iter__(self):
            return iter(())
    model = {"samples": {LEVELS[0]: EmptyIteration([1])}}
    assert subject._apply_empirical_cdf(model, LEVELS, 5) is old_apply_cdf(model, LEVELS, 5) is None


def test_nested_source_alias_and_mapping_keys_are_in_hash_signature():
    inner = [1]
    current = bundle({"nested": {"x": 1}}, sources={"SYNTHETIC": inner})
    original = current.content_hash()
    inner[0] = 2
    assert current.content_hash() == old_hash(current) != original
    original = current.content_hash()
    nested = current.models["model"]["nested"]
    nested["y"] = nested.pop("x")
    assert current.content_hash() == old_hash(current) != original
