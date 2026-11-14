"""SYNTHETIC K45 contracts and unmodified full-production direction oracle."""
from __future__ import annotations

import copy
from dataclasses import fields, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("k45_skill_test", ROOT / "tools/experiments/k45_forecast_skill.py")
k = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = k
SPEC.loader.exec_module(k)

from ak_trading.v2.clock import horizon_window
from ak_trading.v2.contracts import FactorValue, FrozenRange
from ak_trading.v2.validation.splits import SplitAssignment

HASH = hashlib.sha256(b"SYNTHETIC:K45").hexdigest()
PRODUCT, CONTRACT = "SHFE.RB", "SHFE.RB2405"
NANO = 1_000_000_000
DAY = 86400 * NANO
BASE = int(datetime(2022, 1, 3, 1, tzinfo=timezone.utc).timestamp()) * NANO
DAYS = [date(2022, 1, 3) + timedelta(days=i) for i in range(370)]
SESSIONS = tuple(k.SessionWindow(f"SYNTHETIC-{i}", PRODUCT, day, BASE + i * DAY,
                                 BASE + i * DAY + 300 * k.MINUTE, "DAY", "SYNTHETIC", "SCENARIO")
                 for i, day in enumerate(DAYS))


def quality(status="READY", basis="observed"):
    return k.DataQuality(status, ("SYNTHETIC",), basis, HASH)


def make_segment(indices, mode, prefix):
    duration = 60 if mode == "MINUTE" else 86400
    keys = tuple(spec.factor_key for spec in k.factor_registry_v2(duration))
    features, requests, labels = [], [], []
    for i in indices:
        decision = SESSIONS[i].start_nano - 1
        signal = ((i * 7) % 19 - 9) / 9
        values = tuple(FactorValue(key, float(signal * ((j % 3) - 1) + j / 20), decision, quality())
                       for j, key in enumerate(keys))
        values += (FactorValue("META:price_tick:v2.1.0", Decimal("1"), decision, quality()),)
        snapshot = k.FeatureSnapshot(f"SYNTHETIC-{prefix}-{mode}-{i}", CONTRACT, "RB2405", PRODUCT,
            DAYS[i], SESSIONS[i].session_id, "DAY" if mode == "MINUTE" else "DAILY", decision,
            decision, duration, (), Decimal("100"), Decimal("2"), values, None, quality())
        features.append(snapshot)
        frozen = FrozenRange(f"SYNTHETIC-RANGE-{prefix}-{mode}-{i}", decision, Decimal("98"),
            Decimal("100"), Decimal("102"), Decimal("96"), Decimal("104"), snapshot.snapshot_id)
        direction = "UP" if prefix == "CAL" or signal > -.5 else "DOWN" if signal < -.75 else "NEUTRAL"
        up, down = direction == "UP", direction == "DOWN"
        batch = k.build_forecast_requests(snapshot, tuple(k.Horizon("TRADING_MINUTE" if mode == "MINUTE"
            else "TRADING_DAY", h) for h in k.HORIZONS[mode]), frozen, SESSIONS)
        requests.extend(batch)
        for request in batch:
            labels.append(k.PathLabel(request.request_id, CONTRACT, "path-label-v2.1.0", request.horizon,
                request.window_start_nano, request.window_end_nano, request.window_end_nano + 60 * NANO,
                "MATURE", ("SYNTHETIC",), direction, signal / 50, Decimal(str(signal * 2)), signal,
                "UP_FIRST" if up else "DOWN_FIRST" if down else "NEITHER", 1 if up else None,
                1 if down else None, 1 if up else None, 1 if down else None,
                "RETURN_FIRST" if up else "BREAK_FIRST" if down else "NEITHER",
                {event: i % 2 == 0 for event in k.models.PERSISTENCE_EVENTS},
                {"LONG": down, "SHORT": up}, frozen.range_id))
    return tuple(features), tuple(requests), tuple(labels), keys


def regimes(features, reference_id):
    return tuple(k.RegimeAssessment(f"SYNTHETIC-A-{f.snapshot_id}", f.snapshot_id, reference_id,
        f.decision_nano, "UNKNOWN", (), None, "UNKNOWN", 1, ("SYNTHETIC",), quality()) for f in features)


def synthetic_case(mode, count=230):
    trf, trr, trl, keys = make_segment(range(count), mode, "TRAIN")
    caf, car, cal, _ = make_segment(range(260, 300), mode, "CAL")
    train_end = max(label.available_at_nano for label in trl)
    cal_end = max(label.available_at_nano for label in cal)
    reference = k.fit_regime_reference(trf, train_end, train_end)
    config = k.models.default_fit_config(keys, train_end, cal_end, SESSIONS[365].end_nano)
    return k.Segment(trf, regimes(trf, "SYNTHETIC-PIT-REFERENCE"), trr, trl), k.Segment(
        caf, regimes(caf, reference.reference_id), car, cal), config, reference


def full_fit(train, cal, config, reference):
    return k.models.fit_forecast_models(train.features, train.regimes, train.requests, train.labels,
        cal.features, cal.regimes, cal.requests, cal.labels, config, reference, seasonality=None)


def float_leaves(value, prefix=()):
    if isinstance(value, float):
        return {prefix: value.hex()}
    if isinstance(value, dict) or isinstance(value, k.Mapping):
        return {path: item for key, child in value.items() for path, item in float_leaves(child, prefix + (key,)).items()}
    if isinstance(value, (tuple, list)):
        return {path: item for i, child in enumerate(value) for path, item in float_leaves(child, prefix + (i,)).items()}
    return {}


def roundtrip_segment(tmp_path, segment, mode, partition="TRAIN"):
    identity = {"source_collection_sha256": HASH, "scenario_input_hash": HASH}
    member = k.write_segment(tmp_path / f"{mode}-{partition}.jsonl.gz", segment, identity,
                             "SYNTHETIC", mode, partition)
    return k.read_segment(member, identity, "SYNTHETIC", mode, partition), member


@pytest.fixture(scope="module", params=("MINUTE", "DAILY"))
def oracle_case(request, tmp_path_factory):
    train, cal, config, reference = synthetic_case(request.param)
    full = full_fit(train, cal, config, reference)
    directory = tmp_path_factory.mktemp("k45-oracle-" + request.param)
    train, _ = roundtrip_segment(directory, train, request.param, "TRAIN")
    cal, _ = roundtrip_segment(directory, cal, request.param, "CALIBRATION")
    restored_reference = k._fit_regime_reference_from_view(train.features,
        reference.training_end_nano, reference.available_at_nano)
    assert k.canonical_json_bytes(k.reference_payload(restored_reference)) == k.canonical_json_bytes(k.reference_payload(reference))
    research = k.fit_direction(train, cal, config, reference)
    # The legacy stream comparison stays in place; the returned result now also
    # traverses the resident tuple path used by the effective v3 experiment.
    tuple_train = k.resident_segment(tuple(train.requests), {a.snapshot_id: a for a in train.regimes}, train.labels)
    tuple_cal = k.resident_segment(tuple(cal.requests), {a.snapshot_id: a for a in cal.regimes}, cal.labels)
    assert isinstance(k.segment_access(tuple_train), k.models._TupleSegmentAccess)
    assert isinstance(k.segment_access(tuple_cal), k.models._TupleSegmentAccess)
    actual = k.fit_direction(tuple_train, tuple_cal, config, reference)
    assert k.canonical_json_bytes(actual.payload()) == k.canonical_json_bytes(research.payload())
    return request.param, tuple_train, tuple_cal, config, reference, full, actual


def test_all_eight_horizons_all_four_candidates_bit_exact_unmodified_full_oracle(oracle_case):
    mode, train, cal, config, reference, full, research = oracle_case
    expected = {key: value for key, value in full.models.items() if "|DIRECTION|" in key}
    assert len(expected) == len(k.HORIZONS[mode]) * 4
    assert set(research.models) == set(expected)
    assert k.canonical_json_bytes(research.models) == k.canonical_json_bytes(expected)
    assert float_leaves(research.models) == float_leaves(expected)
    assert research.inner["sufficient"]
    assert config["max_iterations"] == 500 and config["tree_count"] == 50
    assert config["inner_minimum_train_days"] == 120 and config["inner_minimum_validation_days"] == 30
    features, requests, _, _ = make_segment([330], mode, "PREDICT")
    assessment = regimes(features, reference.reference_id)[0]
    for request in requests:
        expected_direction = k.models.apply_model_set(full.models, tuple(config["feature_keys"]),
            config["minimum_leaf_blocks"], request, assessment.base)["direction"]
        actual = k.direction_probability(research, request, assessment)
        assert tuple(float(expected_direction[c]).hex() for c in k.models.DIRECTION_CLASSES) == tuple(v.hex() for v in actual)


def mutable_ids(value):
    result = {id(value)} if isinstance(value, (dict, list, set)) else set()
    children = value.values() if isinstance(value, k.Mapping) else value if isinstance(value, (list, tuple)) else ()
    for child in children:
        result |= mutable_ids(child)
    return result


def test_a01_full_train_controls_selected_actual_nonunit_temperature_deep_isolation(oracle_case):
    mode, train, cal, config, reference, full, fitted = oracle_case
    _, joined, _ = k.joined_samples(train, cal, config, reference)
    raw_bytes = k.canonical_json_bytes(fitted.raw_control)
    assert not mutable_ids(fitted.raw_control) & mutable_ids(fitted.calibrated_control)
    assert all(m["temperature"] == 1. for m in fitted.raw_control.values())
    assert any(m["temperature"] != 1. for m in fitted.calibrated_control.values())
    features, requests, _, _ = make_segment([330], mode, "CONTROL")
    assessment = regimes(features, reference.reference_id)[0]
    for horizon in k.HORIZONS[mode]:
        group = sorted([s for s in joined if s.horizon.value == horizon], key=lambda s: (s.decision_nano, s.request_id))
        common = k.models._usable_samples(k.models.TASK_DIRECTION, k.models._common_samples(group))
        assert all(s.barrier == "ATR_1" for s in common)
        assert len(common) * 3 == len(group)
        key = k.models.model_key(PRODUCT, mode, group[0].horizon, k.models.COMMON_BARRIER,
                                k.models.TASK_DIRECTION, "UNCONDITIONAL_FREQUENCY")
        assert fitted.sample_ids[key] == sorted(s.request_id for s in common)
        center, scale = k.models._standardize_stats([s.vector for s in common])
        direct = k.models._fit_candidate(k.models.TASK_DIRECTION, "UNCONDITIONAL_FREQUENCY", common, fitted.config, center, scale)
        direct["fit_status"] = "SELECTED:RESEARCH_BASELINE_FULL_TRAIN"
        direct["temperature"] = 1.0
        assert k.canonical_json_bytes(direct) == k.canonical_json_bytes(fitted.raw_control[key])
    assert all(k.direction_probability(fitted, request, assessment, name) is not None
               for name in ("raw_control", "calibrated_control") for request in requests)
    assert k.canonical_json_bytes(fitted.raw_control) == raw_bytes


@pytest.mark.parametrize("case", ("duplicate", "late", "reference", "overlap"))
def test_join_guards_same_error_type_and_reason_as_full_entry(case, tmp_path):
    train, cal, config, reference = synthetic_case("MINUTE", count=4)
    if case == "duplicate":
        train = replace(train, requests=train.requests + train.requests[:1])
    elif case == "late":
        train = replace(train, labels=(replace(train.labels[0], available_at_nano=config["training_end_nano"] + 1),) + train.labels[1:])
    elif case == "reference":
        cal = replace(cal, regimes=regimes(cal.features, "SYNTHETIC-WRONG-REFERENCE"))
    else:
        cal = train
    restored_train, _ = roundtrip_segment(tmp_path, train, "MINUTE", "TRAIN")
    restored_cal, _ = roundtrip_segment(tmp_path, cal, "MINUTE", "CALIBRATION")
    with pytest.raises((k.models.FitError, ValueError, TypeError)) as actual:
        k.fit_direction(restored_train, restored_cal, config, reference)
    with pytest.raises(type(actual.value)) as expected:
        full_fit(train, cal, config, reference)
    assert str(expected.value) == str(actual.value)


def test_insufficient_inner_split_matches_failed_production_direction_models():
    train, cal, config, reference = synthetic_case("DAILY", count=40)
    actual = k.fit_direction(train, cal, config, reference)
    full = full_fit(train, cal, config, reference)
    expected = {key: value for key, value in full.models.items() if "|DIRECTION|" in key}
    assert not actual.inner["sufficient"]
    assert k.canonical_json_bytes(actual.models) == k.canonical_json_bytes(expected)


def bar(identity="SYNTHETIC-BAR", *, duration=60, status="READY", basis="reconstructed", offset=0):
    start = BASE + offset * k.MINUTE
    end = start + duration * NANO
    return k.MarketBar(identity, CONTRACT, "RB2405", PRODUCT, duration, start, start, end,
        DAYS[0], SESSIONS[0].session_id, end + 60 * NANO, None, *(Decimal(x) for x in
        ("100", "102", "99", "101", "10", "1", "2")), True, 1, 1, "SYNTHETIC", quality(status, basis))


def test_scenario_changes_only_matching_availability_no_source_mutation(tmp_path):
    original = k.FrozenInput(tmp_path, HASH, (), SESSIONS, (bar(), bar("daily", duration=86400),
        bar("degraded", status="DEGRADED"), bar("observed", basis="observed")), (), ())
    before = k.canonical_json_bytes([k.encode_record(b) for b in original.bars])
    transformed, check = k.scenario_view(original)
    assert check["changed_count"] == 1 and check["unchanged_count"] == 3
    assert transformed.bars[0].available_at_nano == transformed.bars[0].end_nano + 5 * NANO
    assert all(getattr(original.bars[0], f.name) == getattr(transformed.bars[0], f.name)
               for f in fields(k.MarketBar) if f.name != "available_at_nano")
    assert original.bars[1:] == transformed.bars[1:]
    assert k.canonical_json_bytes([k.encode_record(b) for b in original.bars]) == before
    with pytest.raises(k.ResearchError, match="SOURCE_MINUTE_AVAILABILITY_NOT_60_SECONDS"):
        k.scenario_view(replace(original, bars=(replace(bar(), available_at_nano=bar().end_nano + NANO),)))


def test_original_v212_envelope_precise_keys_and_version():
    payload = {"schema_version": k.SESSIONS_SCHEMA_ID, "record_type": "SessionWindow",
               "contract_version": k.SOURCE_VERSION, "records": [k.encode_record(SESSIONS[0])]}
    assert k.original_record_envelope(k.canonical_json_bytes(payload), k.SessionWindow, k.SESSIONS_SCHEMA_ID) == SESSIONS[:1]
    for changed, reason in ((dict(payload, extra=1), "RECORD_ENVELOPE_FIELDS"),
                            (dict(payload, contract_version=k.CONTRACT_VERSION), "RECORD_ENVELOPE_VERSION"),
                            (dict(payload, record_type="MarketBar"), "RECORD_ENVELOPE_SCHEMA")):
        with pytest.raises(k.ResearchError, match=reason):
            k.original_record_envelope(k.canonical_json_bytes(changed), k.SessionWindow, k.SESSIONS_SCHEMA_ID)


def test_time_projection_never_exposes_price_fields(tmp_path):
    class TimeOnly:
        bar_id, contract_id, duration_seconds = "SYNTHETIC-TIME", CONTRACT, 60
        start_nano, end_nano, available_at_nano, trading_day = BASE, BASE + k.MINUTE, BASE + 2 * k.MINUTE, DAYS[0]
        def __getattr__(self, name):
            raise AssertionError("PRICE_FIELD_ACCESSED:" + name)
    index = k.time_index(SimpleNamespace(bars=(TimeOnly(),)))
    request = make_segment([0], "MINUTE", "TIME")[1][0]
    interval = index.interval(request, request.window_end_nano)
    assert interval.available_at_nano == BASE + 2 * k.MINUTE


def test_missing_direction_not_replaced_with_uniform_distribution():
    train, cal, config, reference = synthetic_case("MINUTE", count=2)
    fitted = k.DirectionFit(config, reference.reference_id, {}, {}, {}, {}, {})
    features, requests, _, _ = make_segment([330], "MINUTE", "NO-DIRECTION")
    assessment = regimes(features, reference.reference_id)[0]
    assert k.direction_probability(fitted, requests[0], assessment) is None
    with pytest.raises(k.ResearchError, match="PREDICTION_MODEL_TIME_INVALID"):
        k.direction_probability(fitted, cal.requests[0], cal.regimes[0])


def test_generated_output_serialized_before_any_final_path_is_created(tmp_path):
    output = tmp_path / "result.json"
    with pytest.raises((TypeError, ValueError)):
        k.dump_new(output, {"invalid": math.nan})
    assert not output.exists()
    result = k.dump_new(output, {"schema": "SYNTHETIC"})
    k.verify_pin(result)
    with pytest.raises(k.ResearchError, match="OUTPUT_ALREADY_EXISTS"):
        k.dump_new(output, {})


def formal_sessions():
    first, last = date(2023, 1, 3), date(2024, 11, 15)
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    return tuple(k.SessionWindow(f"SYNTHETIC-FORMAL-{day}", PRODUCT, day,
        int(datetime(day.year, day.month, day.day, 1, tzinfo=timezone.utc).timestamp()) * NANO,
        int(datetime(day.year, day.month, day.day, 6, tzinfo=timezone.utc).timestamp()) * NANO,
        "DAY", "SYNTHETIC", "SCENARIO") for day in days)


def formal_request(day, horizon=1, mode="MINUTE"):
    sessions = formal_sessions()
    session = next(s for s in sessions if s.trading_day == day)
    old = make_segment([0], mode, "STAGE-GUARD")[0][0]
    f = replace(old, snapshot_id=f"SYNTHETIC-{day}-{mode}", trading_day=day,
                session_id=session.session_id, decision_nano=session.start_nano + 5 * k.MINUTE + 5 * NANO,
                available_at_nano=session.start_nano + 5 * k.MINUTE + 5 * NANO)
    request = k.build_forecast_requests(f, (k.Horizon("TRADING_MINUTE" if mode == "MINUTE" else "TRADING_DAY", horizon),), None, sessions)[1]
    interval = k.SampleInterval(request.request_id, PRODUCT, day, f.decision_nano, request.window_end_nano,
                               request.window_end_nano, request.window_end_nano + 5 * NANO, request.horizon)
    return request, interval, sessions


def test_phase_one_test_labels_rejected_even_if_renamed_or_dates_forged():
    request, interval, sessions = formal_request(date(2024, 7, 5))
    split = k.split_for(sessions, "MINUTE")
    assignment = SplitAssignment(request.request_id, split.split_id, "TEST", ())
    with pytest.raises(k.ResearchError, match="TEST_LABEL_FORBIDDEN_BEFORE_STAGE3"):
        k.guarded_label(request, interval, assignment, split, (), sessions,
                        evaluation_as_of=k.evaluation_time(sessions))
    with pytest.raises(k.ResearchError, match="LABEL_PARTITION_MISMATCH"):
        k.guarded_label(request, interval, replace(assignment, partition="TRAIN"), split, (), sessions)
    fake = replace(request, features=replace(request.features, trading_day=date(2023, 7, 5)))
    with pytest.raises(k.ResearchError, match="LABEL_DECISION_PARTITION_MISMATCH"):
        k.guarded_label(fake, replace(interval, trading_day=fake.features.trading_day),
                        replace(assignment, partition="TRAIN"), split, (), sessions)
    with pytest.raises(k.ResearchError, match="LABEL_FIXED_SPLIT_REQUIRED"):
        k.guarded_label(request, interval, assignment, replace(split, train_end_nano=split.train_end_nano - 1), (), sessions)


def test_cal_window_crossing_test_fails_before_labeling_and_terminal_maturity_is_not_extended():
    request, interval, sessions = formal_request(date(2024, 5, 31), horizon=20, mode="DAILY")
    split = k.split_for(sessions, "DAILY")
    assignment = SplitAssignment(request.request_id, split.split_id, "CALIBRATION", ())
    with pytest.raises(k.ResearchError, match="LABEL_CROSSES_PARTITION"):
        k.guarded_label(request, interval, assignment, split, (), sessions)
    assert k.assign_splits((interval,), split, sessions)[0].partition == "PURGED"
    _, test_interval, _ = formal_request(date(2024, 10, 18), horizon=20, mode="DAILY")
    terminal = replace(test_interval, label_end_nano=sessions[-1].end_nano, holding_end_nano=sessions[-1].end_nano,
                       available_at_nano=sessions[-1].end_nano + NANO)
    assert k.assign_splits((terminal,), split, sessions)[0].partition == "UNMATURED"
    assert k.evaluation_time(sessions) == sessions[-1].end_nano + 61 * NANO


def test_stage_two_ticket_rejects_test_member_and_disguised_date_before_fit(tmp_path):
    train, cal, config, reference = synthetic_case("MINUTE", count=2)
    path = tmp_path / "ticket.json"
    ticket = {"schema": "k45-fit-ticket-v2", "purpose": "R001", "mode": "MINUTE", "identity": {
        "source_collection_sha256": HASH, "scenario_input_hash": HASH}, "code_pins": [], "segments": {"TEST": {}},
        "reference": k.reference_payload(reference), "config": config, "status": "READY_FOR_STAGE2"}
    path.write_bytes(k.canonical_json_bytes(ticket))
    with pytest.raises(k.ResearchError, match="FIT_INTERFACE_TRAIN_CAL_ONLY"):
        k.read_fit_ticket(path)
    request, _, _ = formal_request(date(2024, 7, 5))
    segment = k.make_segment([request], {request.features.snapshot_id: regimes((request.features,), reference.reference_id)[0]}, ())
    train_file = tmp_path / "train.json.gz"
    member = k.write_segment(train_file, segment, ticket["identity"], "R001", "MINUTE", "TRAIN")
    empty = k.write_segment(tmp_path / "cal.json.gz", k.Segment((), (), (), ()), ticket["identity"],
                           "R001", "MINUTE", "CALIBRATION")
    ticket["segments"] = {"TRAIN": member, "CALIBRATION": empty}
    path.write_bytes(k.canonical_json_bytes(ticket))
    with pytest.raises(k.ResearchError, match="FIT_INPUT_PARTITION_BOUNDARY"):
        k.read_fit_ticket(path)


def test_segment_and_reference_roundtrip_is_exact_and_shared_features_restored(tmp_path):
    train, _, _, reference = synthetic_case("DAILY", count=3)
    restored, _ = roundtrip_segment(tmp_path, train, "DAILY")
    for name in ("features", "regimes", "requests", "labels"):
        expected = tuple(getattr(train, name))
        actual = tuple(getattr(restored, name))
        assert actual == expected
        assert [k.canonical_json_bytes(k.encode_record(r)) for r in actual] == [k.canonical_json_bytes(k.encode_record(r)) for r in expected]
    assert restored.features.store is restored.requests.store
    assert k.reference_payload(k.decode_reference(k.reference_payload(reference))) == k.reference_payload(reference)


def test_stream_sources_preserve_growth_nonprefix_order_duplicates_and_decimal_encoding(tmp_path):
    train, _, _, _ = synthetic_case("MINUTE", count=4)
    sequences = (("A", "B"), ("A", "B", "C"), ("X", "A", "A"), ("B", "A"))
    features = tuple(replace(f, source_bar_ids=ids) for f, ids in zip(train.features, sequences))
    by_id = {f.snapshot_id: f for f in features}
    requests = tuple(replace(r, features=by_id[r.features.snapshot_id]) for r in train.requests)
    represented = replace(requests[0].features, reference_price=Decimal("100.00"))
    assert represented == features[0]
    assert k.encode_record(represented) != k.encode_record(features[0])
    requests = (replace(requests[0], features=represented),) + requests[1:]
    original = replace(train, features=features[::-1] + features[:1], requests=requests + requests[:1])
    restored, member = roundtrip_segment(tmp_path, original, "MINUTE")
    for field in ("features", "requests", "regimes", "labels"):
        expected = [k.canonical_json_bytes(k.encode_record(row)) for row in getattr(original, field)]
        actual = [k.canonical_json_bytes(k.encode_record(row)) for row in getattr(restored, field)]
        assert actual == expected
    assert member["counts"]["FeatureSnapshot"] == 5
    assert member["counts"]["ForecastRequest"] == len(requests) + 1
    assert k.encode_record(restored.requests[0].features)["reference_price"] == "100.00"


@pytest.mark.parametrize("fault", ("pin", "parent", "length", "reference", "footer", "footer_type", "truncated", "trailing", "unreachable"))
def test_stream_rejects_pins_closure_reference_and_terminal_corruption(tmp_path, fault):
    train, _, _, _ = synthetic_case("DAILY", count=2)
    _, member = roundtrip_segment(tmp_path, train, "DAILY")
    identity = {"source_collection_sha256": HASH, "scenario_input_hash": HASH}
    path = Path(member["path"])
    rows = [json.loads(line) for line in k.gzip.decompress(path.read_bytes()).splitlines()]
    source = next(row for row in rows if row.get("type") == "source")
    snapshot = next(row for row in rows if row.get("type") == "FeatureSnapshot")
    if fault == "pin":
        member = dict(member, sha256="0" * 64)
    elif fault == "parent": source["parent"] = source["id"]
    elif fault == "length": source["length"] += 1
    elif fault == "reference": snapshot["value"]["source_bar_ids"]["source_node"] = 9999
    elif fault == "footer": rows = rows[:-1]
    elif fault == "footer_type": rows[-1]["counts"]["FeatureSnapshot"] = True
    elif fault == "trailing": rows.append({"type": "source"})
    elif fault == "unreachable":
        count = rows[-1]["source_nodes"]
        rows.insert(-1, {"type": "source", "id": count, "parent": None, "length": 1, "delta": ["UNREACHABLE"]})
    if fault != "pin":
        raw = k.gzip.compress(b"".join(k.canonical_json_bytes(row) for row in rows), mtime=0)
        if fault == "truncated": raw = raw[:-8]
        path.write_bytes(raw)
        member = dict(member, **k.pin(path))
    with pytest.raises((k.ResearchError, EOFError, OSError)):
        k.read_segment(member, identity, "SYNTHETIC", "DAILY", "TRAIN")


def test_stream_partition_stores_and_getters_cannot_reach_other_segments(tmp_path):
    train, cal, _, _ = synthetic_case("MINUTE", count=2)
    train, _ = roundtrip_segment(tmp_path, train, "MINUTE", "TRAIN")
    cal, _ = roundtrip_segment(tmp_path, cal, "MINUTE", "CALIBRATION")
    assert train.features.store is not cal.features.store
    assert k.segment_access(train).request_getter()(cal.requests[0].request_id) is None
    assert k.segment_access(cal).request_getter()(train.requests[0].request_id) is None
    assert set(train.features.store._snapshot_slots_by_id).isdisjoint(cal.features.store._snapshot_slots_by_id)


@pytest.mark.parametrize("fault", ("duplicate_snapshot", "nested_mismatch"))
def test_stream_retains_original_production_join_errors(tmp_path, fault):
    train, cal, config, reference = synthetic_case("DAILY", count=3)
    if fault == "duplicate_snapshot":
        train = replace(train, features=train.features + train.features[:1])
    else:
        changed = replace(train.requests[0].features, reference_price=Decimal("101"))
        train = replace(train, requests=(replace(train.requests[0], features=changed),) + train.requests[1:])
    restored, _ = roundtrip_segment(tmp_path, train, "DAILY", "TRAIN")
    restored_cal, _ = roundtrip_segment(tmp_path, cal, "DAILY", "CALIBRATION")
    with pytest.raises((k.models.FitError, ValueError, TypeError)) as original:
        full_fit(train, cal, config, reference)
    with pytest.raises(type(original.value)) as actual:
        k.fit_direction(restored, restored_cal, config, reference)
    assert str(original.value) == str(actual.value)


def test_stream_long_prefixes_are_complete_with_bounded_materialization_and_compact_disk(tmp_path):
    import tracemalloc
    from ak_trading.v2._fold_records import _deep_size_estimate
    template = make_segment([0], "MINUTE", "PREFIX")[0][0]
    source_ids = tuple(f"SYNTHETIC-SOURCE-{index:06d}" for index in range(20000))
    store = k.new_store()
    for index in range(48):
        store.append_snapshot(replace(template, snapshot_id=f"SYNTHETIC-LONG-{index}",
            source_bar_ids=source_ids[:(index + 1) * 400]))
    store.seal()
    segment = k.Segment(k.ordered_view(store, "FeatureSnapshot"), (), k.ordered_view(store, "ForecastRequest"), ())
    tracemalloc.start()
    try:
        restored, member = roundtrip_segment(tmp_path, segment, "MINUTE")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(restored.features) == 48
    assert restored.features[47].source_bar_ids == source_ids[:19200]
    assert member["source_delta_count"] == 19200
    assert member["logical_bytes"] > 10 * member["source_delta_json_bytes"]
    assert member["bytes"] < member["source_delta_json_bytes"] + 48 * 16000
    assert _deep_size_estimate(restored.features.store) < 16 * 1024 ** 2
    assert peak < 32 * 1024 ** 2
    stats = restored.features.store.stats()
    assert stats["cache_entries"] <= k.CACHE_RECORDS and stats["cache_bytes_estimate"] <= k.CACHE_BYTES


def test_grid_missing_slot_does_not_renumber_and_each_session_resets(tmp_path):
    day = date(2023, 1, 4)
    first = replace(SESSIONS[0], trading_day=day, end_nano=BASE + 15 * k.MINUTE)
    second = replace(first, session_id="SYNTHETIC-SECOND", start_nano=BASE + 60 * k.MINUTE, end_nano=BASE + 75 * k.MINUTE)
    inst = k.Instrument(CONTRACT, "RB2405", PRODUCT, "SYNTHETIC", (), None, None, None, None, quality())
    bars = []
    for session in (first, second):
        for i in range(15):
            if session == first and i == 4: continue
            b = bar(f"{session.session_id}-{i}", offset=(session.start_nano - BASE) // k.MINUTE + i)
            bars.append(replace(b, trading_day=day, session_id=session.session_id, available_at_nano=b.end_nano + 5 * NANO))
    bundle = k.FrozenInput(tmp_path, HASH, (inst,), (first, second), tuple(bars), (), ())
    grid = k.anchors(bundle, {(day, 60): CONTRACT})
    assert len(grid) == 6
    assert grid[0].reasons == ("GRID_BAR_MISSING",)
    assert grid[1].grid_end == BASE + 10 * k.MINUTE
    assert grid[2].reasons == ("ANCHOR_AVAILABLE_AFTER_SESSION",)
    assert grid[3].grid_end == second.start_nano + 5 * k.MINUTE


def test_public_errors_do_not_expose_data_and_stage_three_requires_unconditional_pass(tmp_path):
    secret_marker = "SYNTHETIC-TEST-PRICE-DO-NOT-LOG-12345"
    error = ValueError(secret_marker)
    public = json.dumps(k.public_error(error))
    assert secret_marker not in public and "ValueError" in public
    review_file = tmp_path / "review.json"
    for verdict, conditional in (("FAIL", False), ("PASS", True)):
        review_file.write_text(json.dumps({"verdict": verdict, "conditional": conditional}), encoding="utf-8")
        release = {"schema": "k45-stage3-release-v2", "purpose": "R001", "freeze": {},
                   "review": k.pin(review_file), "reviewer": "SYNTHETIC-INDEPENDENT", "independent": True, "one_time_run": "R001"}
        with pytest.raises(k.ResearchError, match="INDEPENDENT_UNCONDITIONAL_PASS_REQUIRED"):
            k.validate_release(release)


def test_unknown_cost_preserves_original_snapshot_and_unknown_assessment():
    train, _, _, reference = synthetic_case("MINUTE", count=1)
    snapshot = train.features[0]
    original = k.canonical_json_bytes(k.encode_record(snapshot))
    assessment = k.assessment_for(snapshot, (), replace(reference, available_at_nano=snapshot.decision_nano - 1,
                                  training_end_nano=snapshot.decision_nano - 1), None)
    assert assessment.base == "UNKNOWN"
    assert k.canonical_json_bytes(k.encode_record(snapshot)) == original


def test_pit_training_references_are_prior_day_and_rehearsal_uses_prefix_only(monkeypatch):
    sessions = formal_sessions()
    features = tuple(formal_request(day)[0].features for day in (date(2023, 1, 4), date(2023, 1, 5), date(2024, 3, 5)))
    original = k._fit_regime_reference_from_view
    checked = []
    def exact_reference(view, cutoff, available):
        actual = original(view, cutoff, available)
        expected = k.fit_regime_reference(tuple(view), cutoff, available)
        assert k.canonical_json_bytes(k.reference_payload(actual)) == k.canonical_json_bytes(k.reference_payload(expected))
        assert actual.reference_id == expected.reference_id
        checked.append((len(view), cutoff))
        return actual
    monkeypatch.setattr(k, "_fit_regime_reference_from_view", exact_reference)
    reference, requests, assessments, _, = k.build_research_requests(features, (), sessions, "MINUTE", k.Timings())
    assert [count for count, _ in checked] == [2, 0, 1]
    assert assessments[features[0].snapshot_id].reference_id != reference.reference_id
    assert assessments[features[1].snapshot_id].reference_id != assessments[features[0].snapshot_id].reference_id
    assert assessments[features[2].snapshot_id].reference_id == reference.reference_id
    prefix = tuple(s for s in sessions if s.trading_day <= date(2024, 2, 29))
    assert k.split_for(prefix, "MINUTE", purpose="DRY_RUN").test_end_nano == k.evaluation_time(prefix, "DRY_RUN")
    with pytest.raises(k.ResearchError, match="REQUEST_DECISION_OUTSIDE_DESIGN"):
        k.build_research_requests(features, (), prefix, "MINUTE", k.Timings(), purpose="DRY_RUN")


def test_stage_one_requires_current_capacity_evidence_before_output_or_input_decode(tmp_path, monkeypatch):
    path = tmp_path / "capacity.json"
    path.write_bytes(k.canonical_json_bytes({"schema": "k45-capacity-check-v1", "status": "STOP"}))
    def forbidden(*args, **kwargs):
        pytest.fail("Input decoder must not run before capacity evidence is accepted")
    monkeypatch.setattr(k, "read_original_shard", forbidden)
    with pytest.raises(k.ResearchError, match="CAPACITY_PREFLIGHT_REQUIRED"):
        k.stage_one(tmp_path / "run", path)
    assert not (tmp_path / "run").exists()


def test_n1_n5_n30_n59_same_anchor_window_but_distinct_identity_and_rule_first_seen_counterexample(tmp_path):
    import random
    rng = random.Random(20261113)
    original = []
    value = 1000.
    for i in range(260):
        value += rng.uniform(-2, 2)
        b = bar(f"SYNTHETIC-CONTINUOUS-{i}", offset=i)
        original.append(replace(b, open=Decimal(str(value)), close=Decimal(str(value + .2)),
            high=Decimal(str(value + 2)), low=Decimal(str(value - 2)),
            volume=Decimal(i % 11 + 10), open_oi=Decimal(i + 100), close_oi=Decimal(i + 101)))
    rule = k.TradingRule("SYNTHETIC-RULE", CONTRACT, BASE - DAY, BASE + DAY, BASE - DAY,
        Decimal(10), Decimal(1), 1, 1, None, None, None, None, None, None, None,
        Decimal(0), Decimal(0), Decimal(".1"), "SCENARIO", HASH)
    bundle = k.FrozenInput(tmp_path, HASH, (), SESSIONS[:1], tuple(original), (), (rule,))
    price_anchors, windows, identities = [], [], []
    for seconds in (1, 5, 30, 59):
        transformed, _ = k.scenario_view(bundle, seconds)
        decision = original[199].end_nano + seconds * NANO
        snapshot, _, _ = k._features(transformed, CONTRACT, decision, 60, None, None, SESSIONS[:1])
        assert not k.usable_feature(snapshot)
        request = k.build_forecast_requests(snapshot, (k.Horizon("TRADING_MINUTE", 5),), None, SESSIONS[:1])[1]
        label = k.label_forecast(request, k._WindowIndex(transformed).window(request), SESSIONS[:1], (), BASE + DAY)
        assert label.status == "MATURE" and label.direction is not None
        price_anchors.append(snapshot.reference_price)
        windows.append((request.window_start_nano, request.window_end_nano))
        identities.append((snapshot.snapshot_id, request.request_id))
    assert len(set(price_anchors)) == 1 and len(set(windows)) == 1 and len(set(identities)) == 4
    between = original[199].end_nano + 20 * NANO
    delayed_rule = replace(rule, available_at_nano=between)
    with_rule = replace(bundle, rules=(delayed_rule,))
    assert k.visible_rule(with_rule, CONTRACT, original[199].end_nano + 5 * NANO) is None
    assert k.visible_rule(with_rule, CONTRACT, original[199].end_nano + 30 * NANO) == delayed_rule


def resident_identity_case(price_change=False):
    """SYNTHETIC real feature/production request chain for subprocess identity tests."""
    import random
    rng = random.Random(20261114)
    rows, value = [], 1000.
    for i in range(260):
        value += rng.uniform(-2, 2)
        delta = Decimal('.1') if price_change and i == 190 else Decimal(0)
        base = Decimal(str(value))
        rows.append(replace(bar(f'SYNTHETIC-V3-{i}', offset=i), open=base, close=base + Decimal('.2') + delta,
            high=base + 2, low=base - 2, volume=Decimal(i % 11 + 10), open_oi=Decimal(i + 100), close_oi=Decimal(i + 101)))
    rule = k.TradingRule('SYNTHETIC-V3-RULE', CONTRACT, BASE - DAY, BASE + DAY, BASE - DAY,
        Decimal(10), Decimal(1), 1, 1, None, None, None, None, None, None, None,
        Decimal(0), Decimal(0), Decimal('.1'), 'SCENARIO', HASH)
    original = k.FrozenInput(Path('SYNTHETIC'), HASH, (), SESSIONS[:1], tuple(rows), (), (rule,))
    bundle, _ = k.scenario_view(original)
    features, requests = [], []
    for i in (199, 204, 209):
        snapshot, _, _ = k._features(bundle, CONTRACT, rows[i].end_nano + 5 * NANO, 60, None, None, SESSIONS[:1])
        assert not k.usable_feature(snapshot)
        features.append(snapshot)
        requests.extend(k.build_forecast_requests(snapshot, (k.Horizon('TRADING_MINUTE', 5),), k.freeze_range(snapshot), SESSIONS[:1]))
    return tuple(features), tuple(requests)


def test_resident_cross_process_hashseed_identity_sequences_are_exact(tmp_path):
    import subprocess
    program = (
        "import runpy,json; m=runpy.run_path('tests/experiments/test_k45_forecast_skill.py'); "
        "f,r=m['resident_identity_case'](); "
        "print(json.dumps({'snapshots':[x.snapshot_id for x in f],'requests':[x.request_id for x in r]}))"
    )
    results = []
    for seed in ('1', '57931'):
        result = subprocess.run([sys.executable, '-X', 'utf8', '-c', program], cwd=ROOT,
            env={'SystemRoot': 'C:/Windows', 'WINDIR': 'C:/Windows', 'TEMP': str(tmp_path), 'TMP': str(tmp_path),
                 'PYTHONHASHSEED': seed, 'PYTHONNOUSERSITE': '1'},
            capture_output=True, text=True, encoding='utf-8', timeout=120)
        assert result.returncode == 0, {'raw_exit': result.returncode, 'stderr': result.stderr}
        assert not result.stderr
        results.append(json.loads(result.stdout))
    assert results[0] == results[1]
    assert len(results[0]['snapshots']) == 3 and len(results[0]['requests']) == 9


def test_resident_fit_boundary_discards_future_and_blocks_source_reopen(tmp_path):
    sessions = formal_sessions()
    cutoff = k.split_for(sessions, 'MINUTE').calibration_end_nano
    kept = replace(bar(), trading_day=date(2024, 5, 31), end_nano=cutoff - 1)
    future = replace(bar('SYNTHETIC-FUTURE'), trading_day=date(2024, 6, 3))
    unknown = replace(bar('SYNTHETIC-UNKNOWN'), trading_day=None)
    full = k.FrozenInput(tmp_path, HASH, (), sessions, (kept, future, unknown), (), ())
    truncated = k.truncate_fit_bundle(full, cutoff)
    assert truncated.bars == (kept,)
    assert truncated.sessions is sessions and truncated.root != tmp_path
    inputs = k.ResidentInputs({CONTRACT: truncated}, sessions, (), {}, 'MINUTE', cutoff, ())
    with k.resident_fit_scope(inputs):
        assert all(b is kept for rows in k._WindowIndex(truncated).groups.values() for b in rows)
        with pytest.raises(k.ResearchError, match='FIT_SOURCE_REOPEN_FORBIDDEN'):
            k.read_original_shard(tmp_path, HASH)
    poisoned = replace(inputs, bundles={CONTRACT: replace(truncated, bars=(kept, future))})
    with pytest.raises(k.ResearchError, match='FIT_FUTURE_BAR_REACHABLE'):
        with k.resident_fit_scope(poisoned):
            pytest.fail('A future bar reached the fit body')


def test_resident_changed_synthetic_price_stops_before_prediction(tmp_path, monkeypatch):
    features, requests = resident_identity_case()
    changed_features, changed_requests = resident_identity_case(price_change=True)
    bindings = {'source_collection_sha256': HASH, 'scenario': 'SYNTHETIC-N5'}
    def sequences(fs, rs):
        return (k.id_sequence('R001', 'MINUTE', 'TEST', 5, 'snapshots', (f.snapshot_id for f in fs)),
                k.id_sequence('R001', 'MINUTE', 'TEST', 5, 'generated_requests', (r.request_id for r in rs)))
    frozen = k.seal_identity_sequences(tmp_path / 'sealed', sequences(features, requests), bindings)
    called = []
    monkeypatch.setattr(k, 'predict_rows', lambda *args, **kwargs: called.append('PREDICT'))
    with pytest.raises(k.ResearchError, match='RECONSTRUCTED_ID_SEQUENCE_MISMATCH'):
        k.predict_verified(None, None, frozen, sequences(changed_features, changed_requests), bindings)
    assert called == []
    k.predict_verified(None, None, frozen, sequences(features, requests), bindings)
    assert called == ['PREDICT']


def test_resident_identity_preserves_order_duplicates_and_assessment_gate(tmp_path):
    binding = {'classification': 'SYNTHETIC'}
    sequence = k.id_sequence('R001', 'MINUTE', 'TEST', 5, 'assessments', ('a', 'b', 'a'))
    frozen = k.seal_identity_sequences(tmp_path, (sequence,), binding)
    assert frozen[0]['count'] == 3
    for altered in (('a', 'a', 'b'), ('a', 'b'), ('a', 'changed', 'a')):
        with pytest.raises(k.ResearchError, match='RECONSTRUCTED_ID_SEQUENCE_MISMATCH'):
            k.verify_identity_sequences(frozen, (dict(sequence, ids=list(altered)),), binding)


def test_resident_rehearsal_reuses_objects_rebuilds_reference_and_previous():
    sessions = formal_sessions()
    dates = (date(2023, 1, 4), date(2023, 10, 31), date(2023, 11, 2), date(2023, 12, 28),
             date(2024, 2, 29), date(2024, 3, 5))
    features = tuple(formal_request(day)[0].features for day in dates)
    formal = k.resident_chain(features, (), sessions, 'MINUTE', k.Timings(), state_keys=((CONTRACT, 60), ('SYNTHETIC-NONE', 60)))
    training = tuple(f for f in features if f.trading_day <= date(2024, 2, 29))
    requests = tuple(r for r in formal.requests if r.features.trading_day <= date(2024, 2, 29))
    rehearsal = k.resident_chain(training, (), sessions, 'MINUTE', k.Timings(), purpose='DRY_RUN', reuse_requests=requests)
    assert all(a is b for a, b in zip(training, rehearsal.features))
    assert all(a is b for a, b in zip(requests, rehearsal.requests))
    assert formal.reference.reference_id != rehearsal.reference.reference_id
    for f in rehearsal.features:
        if f.trading_day > date(2023, 10, 31):
            assert rehearsal.assessments[f.snapshot_id].reference_id == rehearsal.reference.reference_id
    assert formal.previous[CONTRACT, 60] is formal.assessments[features[-1].snapshot_id]
    assert formal.previous['SYNTHETIC-NONE', 60] is None
    restored = k.decode_previous(k.previous_payload(formal.previous))
    assert restored == formal.previous
    test_feature = formal_request(date(2024, 6, 4))[0].features
    test = k.resident_chain((test_feature,), (), sessions, 'MINUTE', k.Timings(),
        reference=formal.reference, previous=restored)
    assert restored == formal.previous
    assert test.previous[CONTRACT, 60] is test.assessments[test_feature.snapshot_id]
