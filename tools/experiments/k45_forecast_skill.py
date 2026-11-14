"""K45 fixed direction-only research orchestration (SCENARIO / NOT_ASSESSED).

Production functions own feature, label, fit and temperature mathematics.
Stage one seals TEST requests; stage two accepts TRAIN/CAL artifacts only.
Stage three requires the independent review and is never called implicitly.
"""
from __future__ import annotations

import argparse
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from collections.abc import Mapping
import copy
import csv
from contextlib import contextmanager
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal
import gzip
import gc
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from types import MappingProxyType, SimpleNamespace
from zoneinfo import ZoneInfo

REPOSITORY = Path(__file__).resolve().parents[2]
if str(REPOSITORY / "src") not in sys.path:
    sys.path.insert(0, str(REPOSITORY / "src"))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from ak_trading.v2 import CONTRACT_VERSION, INPUT_SCHEMA_VERSION
from ak_trading.v2._fold_records import FoldRecords, RecordView, _SeqNode
from ak_trading.v2.artifacts import canonical_json_bytes, sha256_hex
from ak_trading.v2.clock import ClockError
from ak_trading.v2.contracts import (
    DataQuality, FeatureSnapshot, ForecastRequest, FrozenInput, Horizon,
    Instrument, MacroContext, MacroItem, MarketBar, PathLabel, RegimeAssessment,
    SessionWindow, TradingRule,
)
from ak_trading.v2.data import (
    _AVAILABILITY_KEYS, _MANIFEST_KEYS, _REQUIRED_FILES, _check_no_input_mixing,
    _safe_package_path, _validate_availability_semantics,
    _validate_input_manifest_semantics, visible_rule,
)
from ak_trading.v2.decision.pipeline import _features
from ak_trading.v2.decision.execution import fee_amount
from ak_trading.v2.factor_specs import factor_registry_v2
from ak_trading.v2.factors import FactorsError
from ak_trading.v2.macro import MacroCache
from ak_trading.v2.probability import models
from ak_trading.v2.probability.forecast import ForecastError, build_forecast_requests
from ak_trading.v2.probability.labels import label_forecast, price_tick_from_features
from ak_trading.v2.probability.regimes import (
    RegimeReference, assess_regime, fit_regime_reference, freeze_range,
    _fit_regime_reference_from_view,
)
from ak_trading.v2.schemas import (
    AVAILABILITY_SCHEMA_ID, INSTRUMENTS_SCHEMA_ID, MACRO_ITEMS_SCHEMA_ID,
    RULES_SCHEMA_ID, SESSIONS_SCHEMA_ID, decode_csv_bytes, decode_record,
    encode_record, parse_json_bytes,
)
from ak_trading.v2.validation.experiments import _WindowIndex
from ak_trading.v2.validation.splits import SampleInterval, SplitAssignment, SplitSpec, assign_splits

NANO = 1_000_000_000
MINUTE = 60 * NANO
TZ = ZoneInfo("Asia/Shanghai")
SOURCE_VERSION = "ak_probability_strategy_contract_v2.1.2"
SOURCE_COLLECTION_SHA = "09e7353c3094d42e777ce87bcd54a90a3384d2050005cd422fc073db32951e8e"
PLAN_SHA = "cc753d6175a21deb513857e66d8d3271befa7989138d13bda8afb169e977f09a"
PRODUCTION_MODELS_SHA = "ce91e507d4acae6566b21bbf5f56caf858746cfe86a995a54a8511e49b1ade2f"
HOLDOUT = date(2026, 9, 5)
SCENARIO_SECONDS = 5
HORIZONS = {"MINUTE": (1, 5, 15, 30, 60), "DAILY": (1, 5, 20)}
FORMAL_DATES = {"TRAIN": (date(2023, 1, 3), date(2024, 2, 29)),
                "CALIBRATION": (date(2024, 3, 1), date(2024, 5, 31)),
                "TEST": (date(2024, 6, 3), date(2024, 10, 18))}
DRY_DATES = {"TRAIN": (date(2023, 1, 3), date(2023, 10, 31)),
             "CALIBRATION": (date(2023, 11, 1), date(2023, 12, 26)),
             "TEST": (date(2023, 12, 27), date(2024, 2, 29))}
MATURITY_END = date(2024, 11, 15)
SAFETY = {"enable_trading": False, "observer": "hold", "broker": "NOT_RUN",
          "real_send": 0, "real_cancel": 0, "holdout_read": False, "M5_entry_used": False}
CALL_COUNTS = Counter()
CACHE_RECORDS = 2
CACHE_BYTES = 32 * 1024 ** 2
STREAM_SCHEMA = "k45-record-stream-v1"
FIT_INPUTS_CLOSED = False


def actual_test_counts():
    return {"test_label_calls": CALL_COUNTS["R001:TEST:label"],
            "test_probability_calls": CALL_COUNTS["R001:TEST:probability"],
            "test_scoring_run_count": CALL_COUNTS["R001:TEST:scoring_run"]}


class ResearchError(RuntimeError):
    """Only fixed codes are exposed by the CLI; never repr a data-bearing object."""


def require(condition, code: str):
    if not condition:
        raise ResearchError(code)


def pin(path) -> dict:
    path = Path(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def verify_pin(item):
    actual = pin(item["path"])
    require(actual["sha256"] == item["sha256"], "INPUT_HASH_MISMATCH")
    if "bytes" in item:
        require(actual["bytes"] == item["bytes"], "INPUT_SIZE_MISMATCH")


def dump_new(path, value, *, compressed=False):
    """Serialize completely before creating a generated artifact."""
    path = Path(path)
    raw = canonical_json_bytes(value) + b"\n"
    json.loads(raw)
    require(not path.exists(), "OUTPUT_ALREADY_EXISTS")
    path.parent.mkdir(parents=True, exist_ok=True)
    if compressed:
        raw = gzip.compress(raw, mtime=0)
    temporary = path.with_name(path.name + ".writing")
    with temporary.open("xb") as stream:
        stream.write(raw)
    temporary.replace(path)
    return pin(path)


def load_json(path, *, compressed=False):
    raw = Path(path).read_bytes()
    return parse_json_bytes(gzip.decompress(raw) if compressed else raw)


@dataclass(frozen=True)
class Segment:
    features: tuple[FeatureSnapshot, ...] | RecordView
    regimes: tuple[RegimeAssessment, ...]
    requests: tuple[ForecastRequest, ...] | RecordView
    labels: tuple[PathLabel, ...]


def new_store():
    return FoldRecords(cache_max_records=CACHE_RECORDS, cache_max_bytes=CACHE_BYTES, page_dir=None)


def ordered_view(store, kind):
    count = store.logical_snapshot_count if kind == "FeatureSnapshot" else store.logical_request_count
    return RecordView(store, kind, tuple(range(count)))


class RecordRows:
    """Only ordered handles across already isolated production views; no payload storage."""
    def __init__(self, views=(), *, members=None):
        self.members = tuple(members) if members is not None else tuple(
            (view, index) for view in views for index in range(len(view)))

    def __len__(self):
        return len(self.members)

    def __getitem__(self, index):
        view, slot = self.members[index]
        return view[slot]

    def __iter__(self):
        for view, slot in self.members:
            yield view[slot]

    def info(self, index):
        view, slot = self.members[index]
        return record_info(view, slot)


def record_info(records, index):
    """Metadata/compact fields only; never materialize a source prefix for selection."""
    if isinstance(records, RecordRows):
        return records.info(index)
    if isinstance(records, RecordView):
        slot = records._indices[index]
        return (records.store._snapshots if records.record_kind == "FeatureSnapshot"
                else records.store._requests)[slot]
    return records[index]


def record_infos(records):
    return (record_info(records, index) for index in range(len(records)))


def select_records(records, predicate):
    indices = [index for index in range(len(records)) if predicate(record_info(records, index))]
    if isinstance(records, RecordView):
        return records.select(indices)
    if isinstance(records, RecordRows):
        return RecordRows(members=(records.members[index] for index in indices))
    return tuple(records[index] for index in indices)


def compact_segment(segment):
    """Fresh segment-local store, preserving every logical record and order."""
    store = new_store()
    for snapshot in segment.features:
        store.append_snapshot(snapshot)
    for request in segment.requests:
        store.append_request(request)
    store.seal()
    return Segment(ordered_view(store, "FeatureSnapshot"), segment.regimes,
                   ordered_view(store, "ForecastRequest"), segment.labels)


def segment_access(segment):
    if isinstance(segment.features, RecordView):
        return models._ViewSegmentAccess(segment.features, segment.requests)
    return models._TupleSegmentAccess(segment.features, segment.requests)


@dataclass(frozen=True)
class DirectionFit:
    config: dict
    reference_id: str
    models: dict
    raw_control: dict
    calibrated_control: dict
    sample_ids: dict
    inner: dict

    def payload(self):
        return {"schema": "k45-direction-fit-v2", "config": self.config,
                "reference_id": self.reference_id, "models": self.models,
                "raw_control": self.raw_control, "calibrated_control": self.calibrated_control,
                "sample_ids": self.sample_ids, "inner": self.inner,
                "production_calibration_evidence": "NOT_BUILT_DIRECTION_ONLY",
                "production_publishable": False}


def joined_samples(training: Segment, calibration: Segment, fit_config, reference):
    """The production guards, in production order; no numerical fit is done here."""
    config = models._validate_fit_config(fit_config)
    keys = tuple(config["feature_keys"])
    train_end, cal_end, valid_until = (int(config[k]) for k in
        ("training_end_nano", "calibration_end_nano", "model_valid_until_nano"))
    if not isinstance(reference, RegimeReference):
        raise TypeError("reference_must_be_RegimeReference")
    if reference.available_at_nano > train_end:
        raise models.FitError("regime_reference_available_after_training_end")
    train_access = segment_access(training)
    cal_access = segment_access(calibration)
    models._bind_seasonality(train_access, cal_access, keys, None, train_end)
    train = models._validate_join_access(train_access, train_access, training.regimes,
        training.labels, keys, str(config["label_version"]), train_end, "label_late_for_training")
    cal = models._validate_join_access(cal_access, cal_access, calibration.regimes,
        calibration.labels, keys, str(config["label_version"]), cal_end, "label_late_for_calibration")
    if {s.request_id for s in train} & {s.request_id for s in cal}:
        raise models.FitError("training_calibration_overlap")
    if any(s.decision_nano > train_end for s in train):
        raise models.FitError("training_decision_after_training_end")
    if any(s.decision_nano <= train_end for s in cal):
        raise models.FitError("calibration_decision_not_after_training_end")
    for assessment in calibration.regimes:
        if assessment.reference_id != reference.reference_id:
            raise models.FitError("calibration_regime_reference_mismatch")
    for assessment in training.regimes:
        if assessment.reference_id == reference.reference_id and assessment.decision_nano < reference.available_at_nano:
            raise models.FitError("training_regime_reference_not_yet_available")
    latest = max([s.label_available_at for s in train] + [s.label_available_at for s in cal]
                 + [s.decision_nano for s in cal])
    if valid_until <= latest:
        raise models.FitError("model_valid_until_not_after_calibration_inputs")
    require(len({s.mode for s in train + cal}) <= 1, "FIT_MODES_MUST_BE_SEPARATE")
    return config, train, cal


def inner_partition(samples, config):
    """Date/control orchestration specified in v2 §10.3, before cell filtering."""
    days = sorted({sample.trading_day for sample in samples})
    boundary_index = math.floor(float(config["inner_split_fraction"]) * len(days))
    fit_days = set(days[:boundary_index])
    dev_days = days[boundary_index:]
    fit = [s for s in samples if s.trading_day in fit_days]
    dev = [s for s in samples if s.trading_day not in fit_days]
    if fit and dev:
        boundary = min(s.decision_nano for s in dev)
        fit = [s for s in fit if s.label.window_end_nano < boundary]
    if fit and dev:
        last = max(s.label.window_end_nano for s in fit)
        dev = [s for s in dev if s.window_start > last]
    embargo_days = set(dev_days[:int(config["inner_embargo_days"])])
    dev = [s for s in dev if s.trading_day not in embargo_days]
    nfit, ndev = len({s.trading_day for s in fit}), len({s.trading_day for s in dev})
    return fit, dev, {"fit_days": nfit, "development_days": ndev,
                      "fit_samples": len(fit), "development_samples": len(dev),
                      "sufficient": nfit >= config["inner_minimum_train_days"] and ndev >= config["inner_minimum_validation_days"],
                      "split_date": dev_days[0].isoformat() if dev_days else None,
                      "embargo_dates": [d.isoformat() for d in sorted(embargo_days)]}


def fit_direction(training: Segment, calibration: Segment, fit_config, reference) -> DirectionFit:
    config, train, cal = joined_samples(training, calibration, fit_config, reference)
    fit, dev, inner = inner_partition(train, config)
    fit_ids, dev_ids = {s.request_id for s in fit}, {s.request_id for s in dev}
    cells = defaultdict(list)
    for sample in train:
        cells[models._cell_key(sample)].append(sample)
    selected, raw_control, calibrated_control, sample_ids = {}, {}, {}, {}
    for cell in sorted(cells):
        ordered = sorted(cells[cell], key=lambda s: (s.decision_nano, s.request_id))
        common = models._common_samples(ordered)
        usable = models._usable_samples(models.TASK_DIRECTION, common)
        product, mode, _, horizon_value = cell
        horizon = ordered[0].horizon

        def register(task, barrier, algorithm, model, score, chosen, reason):
            record = dict(model)
            record["samples"] = dict(record.get("samples", {}))
            record["samples"]["development_score"] = [score] if score is not None else []
            if chosen:
                record["fit_status"] = f"SELECTED:{reason}"
            elif reason.startswith(("FAILED", "EXCLUDED")):
                record["fit_status"] = reason
            selected[models.model_key(product, mode, horizon, barrier, task, algorithm)] = record

        if not inner["sufficient"] or not usable:
            reason = "FAILED:insufficient_inner_split" if not inner["sufficient"] else "FAILED:no_samples"
            register(models.TASK_DIRECTION, models.COMMON_BARRIER, "UNCONDITIONAL_FREQUENCY",
                     models._failed_model("UNCONDITIONAL_FREQUENCY", config["feature_keys"], reason), None, False, reason)
            continue
        models._run_task_selection(models.TASK_DIRECTION, models.COMMON_BARRIER, usable,
                                   fit_ids, dev_ids, cell, config, register, horizon_value)
        key = models.model_key(product, mode, horizon, models.COMMON_BARRIER,
                               models.TASK_DIRECTION, "UNCONDITIONAL_FREQUENCY")
        center, scale = models._standardize_stats([sample.vector for sample in usable])
        control = models._fit_candidate(models.TASK_DIRECTION, "UNCONDITIONAL_FREQUENCY", usable, config, center, scale)
        sample_ids[key] = sorted(sample.request_id for sample in usable)
        if control is None:
            failed = models._failed_model("UNCONDITIONAL_FREQUENCY", config["feature_keys"], "FAILED:control_fit_failed")
            raw_control[key], calibrated_control[key] = copy.deepcopy(failed), copy.deepcopy(failed)
            continue
        control["fit_status"] = "SELECTED:RESEARCH_BASELINE_FULL_TRAIN"
        control["temperature"] = 1.0
        raw_control[key], calibrated_control[key] = copy.deepcopy(control), copy.deepcopy(control)
        require(models._selected_model(raw_control, product, mode, horizon, models.COMMON_BARRIER, models.TASK_DIRECTION)
                is raw_control[key], "RAW_CONTROL_SELECTION_MISSING")
        require(models._selected_model(calibrated_control, product, mode, horizon, models.COMMON_BARRIER, models.TASK_DIRECTION)
                is calibrated_control[key], "CALIBRATED_CONTROL_SELECTION_MISSING")
    raw_before = canonical_json_bytes(raw_control)
    if cal and inner["sufficient"]:
        models._apply_calibration_temperatures(selected, config, cal)
        models._apply_calibration_temperatures(calibrated_control, config, cal)
    require(canonical_json_bytes(raw_control) == raw_before, "RAW_CONTROL_MUTATED")
    require(all(float(model.get("temperature", 1.0)) == 1.0 for model in raw_control.values()), "RAW_CONTROL_TEMPERATURE_CHANGED")
    return DirectionFit(config, reference.reference_id, selected, raw_control, calibrated_control, sample_ids, inner)


def direction_probability(fitted: DirectionFit, request: ForecastRequest, assessment: RegimeAssessment,
                          component="models", *, purpose="R001"):
    f = request.features
    require(component in ("models", "raw_control", "calibrated_control"), "COMPONENT_INVALID")
    require(assessment.snapshot_id == f.snapshot_id and assessment.decision_nano == f.decision_nano
            and assessment.reference_id == fitted.reference_id, "PREDICTION_REFERENCE_MISMATCH")
    config = fitted.config
    require(config["training_end_nano"] < f.decision_nano and
            config["calibration_end_nano"] < f.decision_nano < config["model_valid_until_nano"], "PREDICTION_MODEL_TIME_INVALID")
    require(f.available_at_nano <= f.decision_nano < request.window_start_nano < request.window_end_nano, "PREDICTION_REQUEST_TIME_INVALID")
    dates = FORMAL_DATES if purpose == "R001" else DRY_DATES
    if partition_day(f.trading_day, dates) == "TEST": CALL_COUNTS[purpose + ":TEST:probability"] += 1
    values = models.apply_model_set(getattr(fitted, component), tuple(config["feature_keys"]),
                                   config["minimum_leaf_blocks"], request, assessment.base)["direction"]
    if values is None:
        return None
    result = tuple(values[name] for name in models.DIRECTION_CLASSES)
    require(all(isinstance(v, (float, int)) and not isinstance(v, bool) and math.isfinite(v) and 0 <= v <= 1 for v in result)
            and abs(math.fsum(result) - 1) <= 1e-9, "PREDICTION_PROBABILITY_INVALID")
    return result


def scenario_view(bundle: FrozenInput, seconds=SCENARIO_SECONDS):
    require(type(seconds) is int and 1 <= seconds <= 59, "SCENARIO_SECONDS_INVALID")
    changed = []
    bars = []
    counts = Counter()
    for bar in bundle.bars:
        target = bar.duration_seconds == 60 and bar.quality.status == "READY" and bar.quality.temporal_basis == "reconstructed"
        if target:
            require(bar.available_at_nano == bar.end_nano + 60 * NANO, "SOURCE_MINUTE_AVAILABILITY_NOT_60_SECONDS")
            bars.append(replace(bar, available_at_nano=bar.end_nano + seconds * NANO))
            changed.append(bar.bar_id)
            counts[partition_day(bar.trading_day) or "OUTSIDE"] += 1
        else:
            bars.append(bar)
    identity = {"source_manifest_sha256": bundle.manifest_sha256, "seconds": seconds,
                "predicate": "MINUTE_READY_RECONSTRUCTED", "changed_bar_ids": sorted(changed),
                "tool_sha256": pin(__file__)["sha256"]}
    return replace(bundle, bars=tuple(bars)), {"scenario_input_hash": sha256_hex(canonical_json_bytes(identity)),
                "changed_count": len(changed), "unchanged_count": len(bars) - len(changed), "changed_by_partition": dict(counts),
                "original_seconds": 60, "scenario_seconds": seconds, "source_mutations": 0,
                "quality_reasons_describe_original_evidence": True}


def partition_day(day, dates=FORMAL_DATES):
    if day is None:
        return None
    for name, (first, last) in dates.items():
        if first <= day <= last:
            return name
    if dates == FORMAL_DATES and date(2024, 10, 21) <= day <= MATURITY_END:
        return "MATURITY_ONLY"
    return None


def original_record_envelope(payload, record_type, schema):
    root = parse_json_bytes(payload)
    require(isinstance(root, dict) and set(root) == {"schema_version", "record_type", "contract_version", "records"}, "RECORD_ENVELOPE_FIELDS")
    require(root["schema_version"] == schema and root["record_type"] == record_type.__name__, "RECORD_ENVELOPE_SCHEMA")
    require(root["contract_version"] == SOURCE_VERSION and isinstance(root["records"], list), "RECORD_ENVELOPE_VERSION")
    return tuple(decode_record(record_type, row) for row in root["records"])


def read_original_shard(path, expected_hash):
    """K45-only v2.1.2 envelope adapter; current dataclasses remain authoritative."""
    require(not FIT_INPUTS_CLOSED, "FIT_SOURCE_REOPEN_FORBIDDEN")
    path = Path(path)
    require(path.is_dir() and not path.is_symlink(), "SHARD_PATH_INVALID")
    raw = (path / "manifest.json").read_bytes()
    require(sha256_hex(raw) == expected_hash, "SHARD_MANIFEST_HASH")
    manifest = parse_json_bytes(raw)
    require(isinstance(manifest, dict) and set(manifest) == _MANIFEST_KEYS, "SHARD_MANIFEST_FIELDS")
    require(manifest["contract_version"] == SOURCE_VERSION and CONTRACT_VERSION == "ak_probability_strategy_contract_v2.1.3", "ADAPTER_VERSION")
    require(manifest["schema_version"] == INPUT_SCHEMA_VERSION, "SHARD_MANIFEST_SCHEMA")
    _validate_input_manifest_semantics(manifest)
    members = manifest["files"]
    require(isinstance(members, dict) and "manifest.json" not in members and _REQUIRED_FILES <= set(members), "SHARD_MEMBERS")
    payloads = {}
    for name, expected in sorted(members.items()):
        target = _safe_package_path(path, name)
        content = target.read_bytes()
        require(sha256_hex(content) == expected, "SHARD_MEMBER_HASH")
        payloads[name] = content
    availability = parse_json_bytes(payloads["availability.json"])
    require(isinstance(availability, dict) and set(availability) == _AVAILABILITY_KEYS and
            availability["schema_version"] == AVAILABILITY_SCHEMA_ID, "AVAILABILITY_SCHEMA")
    _validate_availability_semantics(availability)
    records = {}
    for name, cls, schema in (("instruments.json", Instrument, INSTRUMENTS_SCHEMA_ID),
                             ("sessions.json", SessionWindow, SESSIONS_SCHEMA_ID),
                             ("macro_items.json", MacroItem, MACRO_ITEMS_SCHEMA_ID),
                             ("rules.json", TradingRule, RULES_SCHEMA_ID)):
        records[name] = original_record_envelope(payloads[name], cls, schema)
    # Date fields are checked before decoding/using any bar price field.
    from io import StringIO
    for row in csv.DictReader(StringIO(payloads["bars.csv"].decode("utf-8"))):
        day = date.fromisoformat(row["trading_day"]) if row["trading_day"] else None
        clock_day = datetime.fromtimestamp(int(row["datetime_nano"]) // NANO, TZ).date()
        require((day is None or day < HOLDOUT) and clock_day < HOLDOUT, "STOP_HOLDOUT_BOUNDARY")
    before = decode_csv_bytes(payloads["bars.csv"], MarketBar)
    bars = _check_no_input_mixing(records["instruments.json"], records["sessions.json"], before, records["rules.json"])
    bundle = FrozenInput(path, expected_hash, records["instruments.json"], records["sessions.json"],
                         bars, records["macro_items.json"], records["rules.json"])
    return bundle, {"source_manifest_sha256": expected_hash, "bars_before_dedup": len(before), "bars_after_dedup": len(bars),
                    "source_contract_version": SOURCE_VERSION, "consumer_contract_version": CONTRACT_VERSION,
                    "official_loader_status": "REJECTED_VERSION", "official_loader_accepted": False,
                    "adapter_scope": "K45_RESEARCH_ONLY", "source_mutations": 0}


@dataclass(frozen=True)
class TimeBar:
    bar_id: str
    contract_id: str
    duration_seconds: int
    start_nano: int
    end_nano: int
    available_at_nano: int | None
    trading_day: date | None


def time_index(bundle):
    """Production interval logic receives a projection containing no prices."""
    bars = tuple(TimeBar(bar.bar_id, bar.contract_id, bar.duration_seconds, bar.start_nano,
                        bar.end_nano, bar.available_at_nano, bar.trading_day) for bar in bundle.bars)
    return _WindowIndex(SimpleNamespace(bars=bars))


def split_for(sessions, mode, *, purpose="R001"):
    require(purpose in ("R001", "DRY_RUN"), "PURPOSE_INVALID")
    require(mode in HORIZONS, "MODE_INVALID")
    dates = FORMAL_DATES if purpose == "R001" else DRY_DATES
    ranges = {}
    for name, (first, last) in dates.items():
        starts = [s.start_nano for s in sessions if s.trading_day == first]
        ends = [s.end_nano for s in sessions if s.trading_day == last]
        require(bool(starts) and bool(ends), "SPLIT_CALENDAR_BOUNDARY_MISSING")
        ranges[name] = min(starts), max(ends) + 61 * NANO
    return SplitSpec(split_id=f"K45-{purpose}-{mode}",
        train_start_nano=ranges["TRAIN"][0], train_end_nano=ranges["TRAIN"][1],
        calibration_start_nano=ranges["CALIBRATION"][0], calibration_end_nano=ranges["CALIBRATION"][1],
        test_start_nano=ranges["TEST"][0], test_end_nano=ranges["TEST"][1],
        embargo_minutes=60, embargo_days=20, final_holdout=False)


def label_allowed(request, interval, assignment, split, future_bars, *, purpose="R001", stage=1,
                  evaluation_as_of=None):
    """Reject renamed partitions and cross-TEST windows before label_forecast."""
    require(purpose in ("R001", "DRY_RUN"), "PURPOSE_INVALID")
    require(interval.sample_id == request.request_id and assignment.sample_id == request.request_id
            and interval.start_nano == request.features.decision_nano
            and interval.label_end_nano == request.window_end_nano and interval.holding_end_nano == request.window_end_nano
            and interval.trading_day == request.features.trading_day and interval.horizon == request.horizon
            and interval.product == request.features.product, "LABEL_REQUEST_BINDING")
    partition = assignment.partition
    require(assignment.split_id == split.split_id, "LABEL_SPLIT_IDENTITY")
    dates = FORMAL_DATES if purpose == "R001" else DRY_DATES
    require(partition in dates and partition_day(request.features.trading_day, dates) == partition, "LABEL_PARTITION_MISMATCH")
    require(stage in (1, 3), "LABEL_STAGE_INVALID")
    require(stage == 3 or partition != "TEST", "TEST_LABEL_FORBIDDEN_BEFORE_STAGE3")
    require(purpose != "DRY_RUN" or request.features.trading_day <= FORMAL_DATES["TRAIN"][1], "DRY_RUN_OUTSIDE_FORMAL_TRAIN")
    cutoff = {"TRAIN": split.train_end_nano, "CALIBRATION": split.calibration_end_nano,
              "TEST": evaluation_as_of}[partition]
    require(type(cutoff) is int, "LABEL_EVALUATION_CUTOFF_REQUIRED")
    lower, upper = {"TRAIN": (split.train_start_nano, split.train_end_nano),
                    "CALIBRATION": (split.calibration_start_nano, split.calibration_end_nano),
                    "TEST": (split.test_start_nano, split.test_end_nano)}[partition]
    require(lower <= request.features.decision_nano < upper, "LABEL_DECISION_PARTITION_MISMATCH")
    if partition != "TEST":
        require(interval.influence_end < cutoff and request.window_end_nano < cutoff, "LABEL_CROSSES_PARTITION")
    for bar in future_bars:
        require(bar.trading_day is not None and bar.trading_day < HOLDOUT, "LABEL_HOLDOUT_BOUNDARY")
        require(bar.contract_id == request.features.contract_id and bar.duration_seconds == request.features.duration_seconds
                and request.window_start_nano <= bar.start_nano < request.window_end_nano, "LABEL_WINDOW_INPUT_IDENTITY")
        if partition != "TEST":
            require(bar.trading_day <= dates[partition][1] and bar.end_nano < cutoff
                    and bar.available_at_nano is not None and bar.available_at_nano < cutoff, "LABEL_INPUT_CROSSES_PARTITION")
        if purpose == "DRY_RUN":
            require(bar.trading_day <= FORMAL_DATES["TRAIN"][1], "DRY_RUN_FUTURE_INPUT")
    return cutoff


def guarded_label(request, interval, assignment, split, future_bars, sessions, *, purpose="R001", stage=1,
                  evaluation_as_of=None):
    mode = "DAILY" if request.horizon.unit == "TRADING_DAY" else "MINUTE"
    require(split == split_for(sessions, mode, purpose=purpose), "LABEL_FIXED_SPLIT_REQUIRED")
    if assignment.partition == "TEST":
        require(evaluation_as_of == evaluation_time(sessions, purpose), "LABEL_FIXED_EVALUATION_REQUIRED")
    cutoff = label_allowed(request, interval, assignment, split, future_bars, purpose=purpose, stage=stage,
                           evaluation_as_of=evaluation_as_of)
    CALL_COUNTS[purpose + ":" + assignment.partition + ":label"] += 1
    return label_forecast(request, tuple(future_bars), sessions, (), cutoff)


SOURCE_METADATA = Path("C:/AKH71113/measurements/source-metadata.json")
SOURCE_METADATA_SHA = "efc8b35c75583962c9a3b5dff75051703e34ce0bb9a1bd8b40e62365fa8ef82f"
SELECTION_SHA = "b6b09ae43f9cf1f26906094dacfcbf854cd520caaea65fcad398e2ce3c4663f7"
SESSIONS_SHA = "8450e193eea2a57a6c39362b110bfee4761a53e320663aabb089740034067e4d"
HISTORY_BINS = (1_000, 10_000, 30_000, 60_000, math.inf)


def history_bin(count):
    return next(i for i, ceiling in enumerate(HISTORY_BINS) if count <= ceiling)


def source_identity():
    """Locate the already pinned r1 collection without accessing any data row."""
    verify_pin({"path": str(SOURCE_METADATA), "sha256": SOURCE_METADATA_SHA})
    candidates = [item for item in load_json(SOURCE_METADATA)["candidates"] if item["revision"] == "r1"]
    require(len(candidates) == 1, "R1_SOURCE_IDENTITY_AMBIGUOUS")
    candidate = candidates[0]
    path = Path(candidate["path"])
    require(candidate["manifest_sha256"] == SOURCE_COLLECTION_SHA, "R1_HISTORICAL_IDENTITY")
    verify_pin({"path": str(path / "manifest.json"), "sha256": SOURCE_COLLECTION_SHA})
    manifest = load_json(path / "manifest.json")
    require(set(manifest) == {"schema_version", "contract_version", "dataset_kind", "files", "shards", "limitations"}, "COLLECTION_FIELDS")
    require(manifest["schema_version"] == "ak_probability_input_collection_v2.1.0" and
            manifest["contract_version"] == SOURCE_VERSION and
            manifest["dataset_kind"] == "HISTORICAL_RECONSTRUCTED", "COLLECTION_SCHEMA")
    require(set(manifest["limitations"]) == {"collection_merged_read_only_from_accepted_shards",
        "database_not_requeried", "identical_identities_deduplicated_bar_keys_neither_conflict_nor_drop"}, "COLLECTION_LIMITATIONS")
    require(isinstance(manifest["files"], dict) and isinstance(manifest["shards"], list)
            and len(manifest["shards"]) == 30, "COLLECTION_MEMBERS")
    old = {item["path"]: item for item in candidate["shards"]}
    shards, union = {}, {}
    for item in manifest["shards"]:
        require(set(item) == {"path", "manifest_sha256"}, "COLLECTION_SHARD_FIELDS")
        member = item["path"] + "/manifest.json"
        manifest_path = _safe_package_path(path, member)
        require(old[item["path"]]["manifest_sha256"] == item["manifest_sha256"], "HISTORICAL_SHARD_PIN")
        verify_pin({"path": str(manifest_path), "sha256": item["manifest_sha256"]})
        shard = load_json(manifest_path)
        require(set(shard) == _MANIFEST_KEYS, "SHARD_MANIFEST_FIELDS")
        _validate_input_manifest_semantics(shard)
        contracts = shard["request"]["contract_ids"]
        require(len(contracts) == 1 and contracts[0] not in shards, "CONTRACT_SHARD_IDENTITY")
        require(shard["files"]["bars.csv"] == old[item["path"]]["bars_declared_sha256"], "HISTORICAL_BAR_PIN")
        require(shard["files"]["selection-evidence.json"] == SELECTION_SHA and
                shard["files"]["sessions.json"] == SESSIONS_SHA, "SHARED_CALENDAR_SELECTION_PIN")
        union[member] = item["manifest_sha256"]
        union.update({item["path"] + "/" + name: digest for name, digest in shard["files"].items()})
        shards[contracts[0]] = {"path": str(manifest_path.parent), "manifest_sha256": item["manifest_sha256"]}
    require(len(shards) == 30 and union == manifest["files"], "COLLECTION_EXACT_FILE_SET")
    return path, shards


def select_contracts(payload, sessions, contracts):
    root_keys = {"availability_policy_version", "counting_calendar", "entries", "parameters",
                 "schema_version", "selection_policy_version"}
    entry_keys = {"bootstrap", "candidates", "cutoff_before_first_session", "duration_seconds",
                  "previous_trading_day", "ranked_eligible", "reason", "selected_contract_id",
                  "selection_cutoff_nano", "status", "trading_day"}
    require(isinstance(payload, dict) and set(payload) == root_keys and
            payload["schema_version"] == "rb-selection-evidence-v1.0.0" and isinstance(payload["entries"], list), "SELECTION_SCHEMA")
    by_day = defaultdict(list)
    for session in sessions:
        by_day[session.trading_day].append(session)
    days = sorted(by_day)
    previous = {day: days[i - 1] if i else None for i, day in enumerate(days)}
    groups = defaultdict(list)
    for row in payload["entries"]:
        require(isinstance(row, dict) and set(row) == entry_keys and
                type(row["bootstrap"]) is bool and type(row["cutoff_before_first_session"]) is bool and
                type(row["duration_seconds"]) is int and row["duration_seconds"] in (60, 86400), "SELECTION_ENTRY_SCHEMA")
        day = date.fromisoformat(row["trading_day"])
        require(day < HOLDOUT, "SELECTION_HOLDOUT_BOUNDARY")
        groups[day, row["duration_seconds"]].append(row)
    selected, excluded = {}, {}
    for day in days:
        for duration in (60, 86400):
            rows = groups.get((day, duration), [])
            reasons = []
            if not rows:
                reasons.append("SELECTION_MISSING")
            elif len(rows) != 1:
                reasons.append("SELECTION_CONFLICT")
            else:
                row = rows[0]
                if row["status"] != "SELECTED": reasons.append("SELECTION_NOT_SELECTED")
                if row["bootstrap"]: reasons.append("SELECTION_BOOTSTRAP")
                if previous[day] is None: reasons.append("SELECTION_NO_PREVIOUS_DAY")
                elif row["previous_trading_day"] != previous[day].isoformat(): reasons.append("SELECTION_PREVIOUS_DAY_MISMATCH")
                if (row["cutoff_before_first_session"] is not True or type(row["selection_cutoff_nano"]) is not int
                        or row["selection_cutoff_nano"] >= min(s.start_nano for s in by_day[day])):
                    reasons.append("SELECTION_CUTOFF_INVALID")
                if row["selected_contract_id"] not in contracts: reasons.append("SELECTION_CONTRACT_UNKNOWN")
                if not reasons: selected[day, duration] = row["selected_contract_id"]
            if reasons: excluded[day, duration] = tuple(reasons)
    require(set(groups) <= {(day, duration) for day in days for duration in (60, 86400)}, "SELECTION_DAY_OUTSIDE_CALENDAR")
    return selected, excluded


@dataclass(frozen=True)
class Anchor:
    contract: str
    day: date
    duration: int
    grid_end: int
    decision: int | None
    reasons: tuple[str, ...]

    @property
    def mode(self):
        return "DAILY" if self.duration == 86400 else "MINUTE"


def anchors(bundle, selected, *, purpose="R001", step=5, mode=None, partitions=None):
    """Original session grid; missing or delayed bars never renumber it."""
    require(type(step) is int and step in (5, 15), "SAMPLING_STEP_INVALID")
    require(mode is None or mode in HORIZONS, "MODE_INVALID")
    dates = FORMAL_DATES if purpose == "R001" else DRY_DATES
    partitions = set(dates) if partitions is None else set(partitions)
    minutes, daily = defaultdict(list), defaultdict(list)
    for bar in bundle.bars:
        if bar.duration_seconds == 60:
            minutes[bar.contract_id, bar.session_id, bar.end_nano].append(bar)
        elif bar.duration_seconds == 86400:
            daily[bar.contract_id, bar.trading_day].append(bar)
    contracts = {instrument.contract_id for instrument in bundle.instruments}
    result = []
    for session in sorted(bundle.sessions, key=lambda s: s.start_nano):
        day = session.trading_day
        if mode == "DAILY" or partition_day(day, dates) not in partitions:
            continue
        contract = selected.get((day, 60))
        if contract not in contracts:
            continue
        for end in range(session.start_nano + step * MINUTE, session.end_nano + 1, step * MINUTE):
            candidates = minutes.get((contract, session.session_id, end), [])
            require(len(candidates) <= 1, "ANCHOR_BAR_IDENTITY_CONFLICT")
            reason, decision = [], None
            if not candidates: reason.append("GRID_BAR_MISSING")
            else:
                bar = candidates[0]
                require(bar.trading_day == day, "ANCHOR_TRADING_DAY_MISMATCH")
                if not bar.complete: reason.append("ANCHOR_INCOMPLETE")
                if bar.available_at_nano is None: reason.append("ANCHOR_AVAILABILITY_UNKNOWN")
                if not reason:
                    decision = max(end, bar.available_at_nano)
                    if decision > session.end_nano:
                        reason.append("ANCHOR_AVAILABLE_AFTER_SESSION")
                        decision = None
            result.append(Anchor(contract, day, 60, end, decision, tuple(reason)))
    for day in sorted({s.trading_day for s in bundle.sessions}):
        contract = selected.get((day, 86400))
        if mode == "MINUTE" or contract not in contracts or partition_day(day, dates) not in partitions:
            continue
        candidates = daily.get((contract, day), [])
        require(len(candidates) <= 1, "DAILY_ANCHOR_IDENTITY_CONFLICT")
        reason, decision = [], None
        if not candidates: reason.append("DAILY_BAR_MISSING")
        else:
            bar = candidates[0]
            if not bar.complete: reason.append("ANCHOR_INCOMPLETE")
            if bar.available_at_nano is None: reason.append("ANCHOR_AVAILABILITY_UNKNOWN")
            if not reason: decision = max(bar.end_nano, bar.available_at_nano)
        result.append(Anchor(contract, day, 86400, candidates[0].end_nano if candidates else 0, decision, tuple(reason)))
    return tuple(sorted(result, key=lambda a: (a.decision if a.decision is not None else a.grid_end, a.duration)))


class Timings:
    def __init__(self):
        self.values = defaultdict(list)

    def call(self, key, function, *args, **kwargs):
        before = time.perf_counter()
        try:
            return function(*args, **kwargs)
        finally:
            self.values[tuple(str(x) for x in key)].append(time.perf_counter() - before)

    def report(self):
        from k45_forecast_statistics import percentile
        return [{"component": key[0], "dimensions": list(key[1:]), "calls": len(values),
                 "total_seconds": math.fsum(values), "mean_seconds": math.fsum(values) / len(values),
                 "median_seconds": percentile(values, .5), "p95_seconds": percentile(values, .95)}
                for key, values in sorted(self.values.items())]


def usable_feature(snapshot):
    keys = tuple(spec.factor_key for spec in factor_registry_v2(snapshot.duration_seconds))
    reasons = []
    if models.feature_vector(snapshot, keys) is None: reasons.append("FEATURE_VECTOR_INCOMPLETE")
    if snapshot.reference_price is None or snapshot.reference_price <= 0: reasons.append("REFERENCE_PRICE_MISSING")
    if snapshot.atr_points is None or snapshot.atr_points <= 0: reasons.append("ATR_MISSING")
    probe = ForecastRequest("K45-TICK-PROBE", snapshot, Horizon("TRADING_MINUTE", 1),
        snapshot.decision_nano + 1, snapshot.decision_nano + 2, "ATR_1", Decimal(1), Decimal(1), None)
    if price_tick_from_features(probe) is None: reasons.append("PRICE_TICK_MISSING")
    if snapshot.available_at_nano > snapshot.decision_nano: reasons.append("FEATURE_NOT_AVAILABLE")
    return tuple(reasons)


def feature_rows(bundle, selected, timing, *, purpose="R001", stores=None):
    stores = stores if stores is not None else {(mode, partition): new_store()
        for mode in HORIZONS for partition in FORMAL_DATES}
    audits, usable_count = [], 0
    dates = FORMAL_DATES if purpose == "R001" else DRY_DATES
    for anchor in anchors(bundle, selected, purpose=purpose):
        partition = partition_day(anchor.day, dates)
        meta = {"mode": anchor.mode, "day": anchor.day.isoformat(), "partition": partition,
                "contract": anchor.contract, "grid_end": anchor.grid_end,
                "decision": anchor.decision, "reasons": list(anchor.reasons), "snapshot_id": None,
                "history_count": None, "usable": False}
        if anchor.decision is not None:
            before = time.perf_counter()
            try:
                snapshot, bars, _ = _features(bundle, anchor.contract, anchor.decision, anchor.duration,
                    None, None, bundle.sessions, canonical_daily=anchor.duration == 86400)
            except FactorsError as exc:
                if str(exc) != "no_visible_bars_for_snapshot": raise
                meta["reasons"].append("NO_VISIBLE_BARS_FOR_SNAPSHOT")
            else:
                elapsed = time.perf_counter() - before
                meta["history_count"] = len(bars)
                if partition == "TRAIN":
                    timing.values["features", anchor.mode, anchor.contract, str(history_bin(len(bars)))].append(elapsed)
                require(snapshot.trading_day == anchor.day and snapshot.contract_id == anchor.contract,
                        "FEATURE_ANCHOR_IDENTITY_MISMATCH")
                meta["reasons"].extend(usable_feature(snapshot))
                meta["snapshot_id"] = snapshot.snapshot_id
                meta["usable"] = not meta["reasons"]
                if meta["usable"]:
                    stores[anchor.mode, partition].append_snapshot(snapshot)
                    usable_count += 1
        audits.append(meta)
        if len(audits) % 100 == 0:
            print(json.dumps({"phase": "PIT_FEATURE_PROGRESS", "purpose": purpose,
                "contract": anchor.contract, "processed_anchors": len(audits), "usable_snapshots": usable_count,
                "last_trading_day": anchor.day.isoformat(), "test_labels": 0}), flush=True)
    return stores, audits


def assessment_for(snapshot, rules, reference, previous):
    rule_bundle = FrozenInput(REPOSITORY, SOURCE_COLLECTION_SHA, (), (), (), (), tuple(rules))
    rule = visible_rule(rule_bundle, snapshot.contract_id, snapshot.decision_nano)
    macro = MacroCache(()).snapshot(snapshot.decision_nano, snapshot.product, False, True)
    known = rule is not None and all(leg is not None for leg in (rule.open_fee, rule.close_fee, rule.close_today_fee))
    if known:
        cost = (fee_amount(rule.open_fee, snapshot.reference_price, 1, rule.multiplier) + max(
            fee_amount(leg, snapshot.reference_price, 1, rule.multiplier) for leg in (rule.close_fee, rule.close_today_fee))) / rule.multiplier
        cost += 2 * rule.price_tick * (rule.slippage_ticks + rule.impact_ticks)
        assessed = snapshot
    else:
        cost = Decimal(0)
        assessed = replace(snapshot, quality=replace(snapshot.quality, status="MISSING",
            reasons=tuple(sorted(set((*snapshot.quality.reasons, "COST_UNKNOWN"))))))
    return assess_regime(assessed, macro, None, freeze_range(snapshot), cost, reference, previous)


def build_research_requests(features, rules, sessions, mode, timing, *, purpose="R001"):
    """Shared formal/rehearsal orchestration, with immutable production math."""
    dates = FORMAL_DATES if purpose == "R001" else DRY_DATES
    split = split_for(sessions, mode, purpose=purpose)
    if isinstance(features, dict):
        views = features
    else:
        stores = {partition: new_store() for partition in dates}
        for snapshot in features:
            partition = partition_day(snapshot.trading_day, dates)
            require(partition in dates, "REQUEST_DECISION_OUTSIDE_DESIGN")
            stores[partition].append_snapshot(snapshot)
        views = {partition: ordered_view(store, "FeatureSnapshot") for partition, store in stores.items()}
    require(set(views) == set(dates) and len({id(view.store) for view in views.values()}) == len(dates),
            "REQUEST_PARTITION_STORES_MUST_BE_DISTINCT")
    members = [(partition, index) for partition, view in views.items() for index in range(len(view))]
    members.sort(key=lambda item: (record_info(views[item[0]], item[1]).decision_nano,
                                   record_info(views[item[0]], item[1]).snapshot_id))
    train = views["TRAIN"].select([index for partition, index in members if partition == "TRAIN"])
    reference = timing.call(("reference_final", mode), _fit_regime_reference_from_view, train,
                            split.train_end_nano, split.train_end_nano)
    by_day, regimes, reasons, previous = {}, {}, [], {}
    for stored_partition, index in members:
        snapshot = views[stored_partition][index]
        partition = partition_day(snapshot.trading_day, dates)
        require(partition in dates and partition == stored_partition, "REQUEST_DECISION_OUTSIDE_DESIGN")
        if partition == "TRAIN":
            if snapshot.trading_day not in by_day:
                prefix = select_records(train, lambda f: f.trading_day < snapshot.trading_day)
                cutoff = max((f.decision_nano for f in record_infos(prefix)), default=snapshot.decision_nano - 1)
                by_day[snapshot.trading_day] = timing.call(("reference_prefix", mode), _fit_regime_reference_from_view,
                                                          prefix, cutoff, cutoff)
            current = by_day[snapshot.trading_day]
        else:
            current = reference
        key = snapshot.contract_id, snapshot.duration_seconds
        before = time.perf_counter()
        assessment = assessment_for(snapshot, rules, current, previous.get(key))
        if partition == "TRAIN": timing.values["assessment", mode].append(time.perf_counter() - before)
        previous[key], regimes[snapshot.snapshot_id] = assessment, assessment
        for value in HORIZONS[mode]:
            before = time.perf_counter()
            try:
                batch = build_forecast_requests(snapshot, (Horizon("TRADING_DAY" if mode == "DAILY"
                    else "TRADING_MINUTE", value),), assessment.frozen_range, sessions)
            except ClockError:
                reasons.append({"snapshot_id": snapshot.snapshot_id, "day": snapshot.trading_day.isoformat(),
                    "partition": partition, "horizon": value, "reason": "HORIZON_CALENDAR_INSUFFICIENT"})
            else:
                for request in batch:
                    views[partition].store.append_request(request)
            finally:
                if partition == "TRAIN": timing.values["requests", mode, str(value)].append(time.perf_counter() - before)
    return reference, RecordRows(ordered_view(views[partition].store, "ForecastRequest")
        for partition in dates), regimes, reasons


def reference_payload(reference):
    return {"reference_id": reference.reference_id, "training_end_nano": reference.training_end_nano,
            "available_at_nano": reference.available_at_nano,
            "volatility_samples": {key: list(values) for key, values in reference.volatility_samples.items()},
            "source_hashes": dict(reference.source_hashes)}


def decode_reference(payload):
    require(set(payload) == {"reference_id", "training_end_nano", "available_at_nano", "volatility_samples", "source_hashes"}, "REFERENCE_FIELDS")
    reference = RegimeReference(payload["reference_id"], payload["training_end_nano"], payload["available_at_nano"],
        {key: tuple(values) for key, values in payload["volatility_samples"].items()}, payload["source_hashes"])
    require(reference.reference_id == "regime_reference:" + reference.content_hash(), "REFERENCE_IDENTITY")
    return reference


def stream_header(identity, purpose, mode, partition):
    return {"schema": STREAM_SCHEMA, "purpose": purpose, "mode": mode, "partition": partition,
            "source_collection_sha256": identity["source_collection_sha256"],
            "scenario_input_hash": identity["scenario_input_hash"]}


def write_segment(path, segment, identity, purpose, mode, partition):
    """Lossless stream of existing production source nodes and canonical records.

    Nested features are always preserved inline (apart from their source-node
    reference). Equal IDs or Decimal values never substitute another encoding.
    Only one complete record is materialized for its canonical logical hash.
    """
    path = Path(path)
    require(not path.exists(), "OUTPUT_ALREADY_EXISTS")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not isinstance(segment.features, RecordView):
        segment = compact_segment(segment)
    require(isinstance(segment.requests, RecordView) and
            segment.features.store is segment.requests.store, "SEGMENT_STORE_IDENTITY")
    header = stream_header(identity, purpose, mode, partition)
    temporary = path.with_name(path.name + ".writing")
    nodes, counts = {}, Counter()
    logical = hashlib.sha256()
    logical_bytes = delta_count = delta_bytes = largest_record = 0
    started = time.perf_counter()
    with temporary.open("xb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as target:
        def emit(value):
            target.write(canonical_json_bytes(value))

        def source_ref(node):
            nonlocal delta_count, delta_bytes
            pending, visiting = [], set()
            while node is not None and node not in nodes:
                require(node not in visiting, "SOURCE_NODE_CYCLE")
                visiting.add(node)
                pending.append(node)
                node = node.parent
            for current in reversed(pending):
                parent = nodes[current.parent] if current.parent is not None else None
                require(current.length == (current.parent.length if current.parent is not None else 0)
                        + len(current.delta), "SOURCE_NODE_LENGTH")
                index = len(nodes)
                nodes[current] = index
                delta_count += len(current.delta)
                delta_bytes += len(canonical_json_bytes(list(current.delta)))
                emit({"type": "source", "id": index, "parent": parent,
                      "length": current.length, "delta": list(current.delta)})
            return nodes[pending[0]] if pending else nodes[node]

        emit(header)
        groups = (("FeatureSnapshot", segment.features), ("RegimeAssessment", segment.regimes),
                  ("ForecastRequest", segment.requests), ("PathLabel", segment.labels))
        for kind, rows in groups:
            for index, record in enumerate(rows):
                value = encode_record(record)
                encoded = canonical_json_bytes({"type": kind, "value": value})
                logical.update(encoded)
                logical_bytes += len(encoded)
                largest_record = max(largest_record, len(encoded))
                if kind in ("FeatureSnapshot", "ForecastRequest"):
                    compact = record_info(rows, index)
                    snapshot = compact if kind == "FeatureSnapshot" else compact.features
                    nested = value if kind == "FeatureSnapshot" else value["features"]
                    nested["source_bar_ids"] = {"source_node": source_ref(snapshot.source)}
                emit({"type": kind, "value": value})
                counts[kind] += 1
        footer = {"type": "end", "counts": {name: counts[name] for name, _ in groups},
                  "source_nodes": len(nodes), "logical_sha256": logical.hexdigest(), "logical_bytes": logical_bytes}
        emit(footer)
    require(not path.exists(), "OUTPUT_ALREADY_EXISTS")
    temporary.rename(path)
    return {**pin(path), "schema": STREAM_SCHEMA, "scope": header,
            **{key: value for key, value in footer.items() if key != "type"},
            "source_delta_count": delta_count, "source_delta_json_bytes": delta_bytes,
            "largest_logical_record_bytes": largest_record, "write_seconds": time.perf_counter() - started}


def read_segment(member, identity, purpose, mode, partition):
    """Strict pins, closure, order, full source restoration and production decode."""
    verify_pin(member)
    expected_header = stream_header(identity, purpose, mode, partition)
    require(member.get("schema") == STREAM_SCHEMA and member.get("scope") == expected_header, "FIT_SEGMENT_SCOPE")
    store, nodes, parents, referenced = new_store(), [], [], set()
    regimes, labels, counts = [], [], Counter()
    logical, logical_bytes, previous_group = hashlib.sha256(), 0, 0
    kinds = ("FeatureSnapshot", "RegimeAssessment", "ForecastRequest", "PathLabel")
    classes = (FeatureSnapshot, RegimeAssessment, ForecastRequest, PathLabel)
    dates = FORMAL_DATES if purpose == "R001" else DRY_DATES

    def restore_sources(value):
        require(isinstance(value, dict), "STREAM_SNAPSHOT_FIELDS")
        if purpose in ("R001", "DRY_RUN"):
            day = date.fromisoformat(value["trading_day"])
            require(day < HOLDOUT, "FIT_INPUT_HOLDOUT_BOUNDARY")
            require(partition_day(day, dates) == partition, "FIT_INPUT_PARTITION_BOUNDARY")
            if purpose == "DRY_RUN":
                require(day <= FORMAL_DATES["TRAIN"][1], "DRY_FIT_INPUT_BOUNDARY")
            require((value["duration_seconds"] == 86400) == (mode == "DAILY"), "FIT_INPUT_MODE_MISMATCH")
        ref = value.get("source_bar_ids")
        require(isinstance(ref, dict) and set(ref) == {"source_node"}, "SOURCE_REFERENCE_FIELDS")
        node_id = ref["source_node"]
        require(type(node_id) is int and 0 <= node_id < len(nodes), "SOURCE_REFERENCE_INVALID")
        referenced.add(node_id)
        return dict(value, source_bar_ids=list(nodes[node_id].materialize()))

    with gzip.open(member["path"], "rb") as stream:
        header_line = stream.readline()
        require(bool(header_line) and parse_json_bytes(header_line) == expected_header, "FIT_SEGMENT_IDENTITY")
        footer = None
        for line in stream:
            row = parse_json_bytes(line)
            require(isinstance(row, dict) and "type" in row, "STREAM_RECORD_SCHEMA")
            kind = row["type"]
            if kind == "source":
                require(set(row) == {"type", "id", "parent", "length", "delta"}, "SOURCE_NODE_FIELDS")
                require(type(row["id"]) is int and row["id"] == len(nodes), "SOURCE_NODE_ORDER")
                parent = row["parent"]
                require(parent is None or type(parent) is int and 0 <= parent < len(nodes), "SOURCE_PARENT_INVALID")
                require(type(row["length"]) is int and isinstance(row["delta"], list)
                        and all(isinstance(value, str) for value in row["delta"]), "SOURCE_DELTA_INVALID")
                node = _SeqNode(None if parent is None else nodes[parent], tuple(row["delta"]))
                require(node.length == row["length"], "SOURCE_NODE_LENGTH")
                nodes.append(node)
                parents.append(parent)
                continue
            if kind == "end":
                require(set(row) == {"type", "counts", "source_nodes", "logical_sha256", "logical_bytes"}, "STREAM_FOOTER_FIELDS")
                require(isinstance(row["counts"], dict) and set(row["counts"]) == set(kinds)
                        and all(type(value) is int and value >= 0 for value in row["counts"].values())
                        and type(row["source_nodes"]) is int and row["source_nodes"] >= 0
                        and type(row["logical_bytes"]) is int and row["logical_bytes"] >= 0
                        and isinstance(row["logical_sha256"], str), "STREAM_FOOTER_TYPES")
                footer = row
                require(stream.read(1) == b"", "STREAM_TRAILING_DATA")
                break
            require(set(row) == {"type", "value"} and kind in kinds, "STREAM_RECORD_FIELDS")
            group = kinds.index(kind)
            require(group >= previous_group, "STREAM_RECORD_ORDER")
            previous_group = group
            value = row["value"]
            if kind == "FeatureSnapshot":
                value = restore_sources(value)
            elif kind == "ForecastRequest":
                value = dict(value, features=restore_sources(value["features"]))
            record = decode_record(classes[group], value)
            encoded = canonical_json_bytes({"type": kind, "value": encode_record(record)})
            logical.update(encoded)
            logical_bytes += len(encoded)
            counts[kind] += 1
            if kind == "FeatureSnapshot": store.append_snapshot(record)
            elif kind == "ForecastRequest": store.append_request(record)
            elif kind == "RegimeAssessment": regimes.append(record)
            else: labels.append(record)
        require(footer is not None, "STREAM_FOOTER_MISSING")
    reached = set()
    for index in referenced:
        while index is not None and index not in reached:
            reached.add(index)
            index = parents[index]
    require(reached == set(range(len(nodes))), "SOURCE_CLOSURE_HAS_UNREFERENCED_NODES")
    expected = {"counts": {kind: counts[kind] for kind in kinds}, "source_nodes": len(nodes),
                "logical_sha256": logical.hexdigest(), "logical_bytes": logical_bytes}
    require(all(footer[key] == value and member.get(key) == value for key, value in expected.items()), "STREAM_LOGICAL_PIN_MISMATCH")
    store.seal()
    return Segment(ordered_view(store, "FeatureSnapshot"), tuple(regimes),
                   ordered_view(store, "ForecastRequest"), tuple(labels))


def make_segment(requests, regimes, labels):
    # Match the original dict's first-key order/last-value behavior using only
    # scalar indices, and preserve every request (including duplicate IDs).
    features = {record.features.snapshot_id: index for index, record in enumerate(record_infos(requests))}
    store = new_store()
    for index in features.values():
        store.append_snapshot(requests[index].features)
    for request in requests:
        store.append_request(request)
    store.seal()
    return Segment(ordered_view(store, "FeatureSnapshot"), tuple(regimes[key] for key in features),
                   ordered_view(store, "ForecastRequest"), tuple(labels))


def evaluation_time(sessions, purpose="R001"):
    last = MATURITY_END if purpose == "R001" else FORMAL_DATES["TRAIN"][1]
    ends = [session.end_nano for session in sessions if session.trading_day == last]
    require(bool(ends), "EVALUATION_CALENDAR_END_MISSING")
    return max(ends) + 61 * NANO


def code_pins():
    paths = [REPOSITORY / relative for relative in (
        "docs/research/k45-forecast-skill-design.md", "tools/experiments/k45_forecast_skill.py",
        "tools/experiments/k45_forecast_statistics.py", "tests/experiments/test_k45_forecast_skill.py",
        "tests/experiments/test_k45_forecast_statistics.py", "src/ak_trading/v2/probability/models.py",
        "src/ak_trading/v2/probability/forecast.py", "src/ak_trading/v2/probability/labels.py",
        "src/ak_trading/v2/probability/regimes.py", "src/ak_trading/v2/validation/splits.py",
        "src/ak_trading/v2/validation/experiments.py", "src/ak_trading/v2/decision/pipeline.py",
        "src/ak_trading/v2/data.py", "src/ak_trading/v2/factors.py", "src/ak_trading/v2/factor_specs.py",
        "src/ak_trading/v2/contracts.py", "src/ak_trading/v2/schemas.py", "src/ak_trading/v2/clock.py",
        "src/ak_trading/v2/_fold_records.py")]
    result = [pin(path) for path in paths]
    require(result[0]["sha256"] == PLAN_SHA, "EFFECTIVE_PLAN_PIN_MISMATCH")
    require(result[5]["sha256"] == PRODUCTION_MODELS_SHA, "PRODUCTION_MODELS_PIN_MISMATCH")
    return result


def count_layer(rows, reasons=(), *, potential=None):
    reason_counts = Counter(reasons)
    return {"count": len(rows), "days": len({row for row in rows}),
            "loss_from_previous": None if potential is None else potential - len(rows),
            "top3_reasons_nonexclusive": [[reason, count] for reason, count in reason_counts.most_common(3)]}


def funnel(mode, audits, requests, assignments, labels, train_samples, cal_samples, request_reasons,
           *, purpose="R001"):
    dates = FORMAL_DATES if purpose == "R001" else DRY_DATES
    assignment_by_id = {a.sample_id: a for a in assignments}
    label_by_id = {label.request_id: label for label in labels}
    result = []
    for partition in dates:
        candidates = [row for row in audits if row["mode"] == mode and row["partition"] == partition]
        usable = [row for row in candidates if row["usable"]]
        for horizon in (None, *HORIZONS[mode]):
            generated = [r for r in record_infos(requests) if partition_day(r.features.trading_day, dates) == partition
                         and (horizon is None or r.horizon.value == horizon)]
            legal = [r for r in generated if assignment_by_id[r.request_id].partition == partition]
            mature = [r for r in legal if r.request_id in label_by_id and label_by_id[r.request_id].status == "MATURE"]
            samples = train_samples if partition == "TRAIN" else cal_samples if partition == "CALIBRATION" else []
            samples = [s for s in samples if horizon is None or s.horizon.value == horizon]
            cells = defaultdict(list)
            for sample in samples: cells[models._cell_key(sample)].append(sample)
            canonical = [s for cell in sorted(cells) for s in models._usable_samples(models.TASK_DIRECTION,
                models._common_samples(sorted(cells[cell], key=lambda s: (s.decision_nano, s.request_id))))]
            excluded = [reason for row in candidates for reason in row["reasons"]]
            excluded_requests = [item["reason"] for item in request_reasons if item["partition"] == partition
                                 and (horizon is None or item["horizon"] == horizon)]
            excluded_requests += [reason for r in generated for reason in assignment_by_id[r.request_id].reasons]
            label_reasons = [reason for r in legal if r.request_id in label_by_id
                             for reason in label_by_id[r.request_id].reasons if label_by_id[r.request_id].status != "MATURE"]
            multiplier = len(HORIZONS[mode]) if horizon is None else 1
            result.append({"mode": mode, "partition": partition, "horizon": horizon,
                "candidate_anchors": count_layer([r["day"] for r in candidates]),
                "usable_features": count_layer([r["day"] for r in usable], excluded, potential=len(candidates)),
                "generated_requests": count_layer([r.features.trading_day.isoformat() for r in generated], excluded_requests,
                                                   potential=len(usable) * multiplier * 3),
                "legal_requests": count_layer([r.features.trading_day.isoformat() for r in legal], excluded_requests,
                                               potential=len(generated)),
                "generated_direction_units": len({(r.features.snapshot_id, r.horizon) for r in generated}),
                "legal_direction_units": len({(r.features.snapshot_id, r.horizon) for r in legal}),
                "label_mature": "NOT_RUN:TEST_SEALED" if partition == "TEST" else
                    count_layer([r.features.trading_day.isoformat() for r in mature], label_reasons, potential=len(legal)),
                "direction_samples": "NOT_RUN:TEST_SEALED" if partition == "TEST" else
                    count_layer([s.trading_day.isoformat() for s in canonical], potential=len(mature) // 3),
                "reason_counts_are_nonexclusive": True, "loss_totals_are_unique_in_the_layer_unit": True})
    return result


def read_fit_ticket(path):
    ticket = load_json(path)
    require(set(ticket) == {"schema", "purpose", "mode", "identity", "code_pins", "segments", "reference", "config", "status"}
            and ticket["schema"] == "k45-fit-ticket-v2", "FIT_TICKET_SCHEMA")
    require(ticket["purpose"] in ("R001", "DRY_RUN") and ticket["mode"] in HORIZONS, "FIT_TICKET_SCOPE")
    require(set(ticket["segments"]) == {"TRAIN", "CALIBRATION"}, "FIT_INTERFACE_TRAIN_CAL_ONLY")
    for source in ticket["code_pins"]: verify_pin(source)
    mode, purpose = ticket["mode"], ticket["purpose"]
    dates = FORMAL_DATES if purpose == "R001" else DRY_DATES
    segments = {}
    for partition, member in ticket["segments"].items():
        segment = read_segment(member, ticket["identity"], purpose, mode, partition)
        cutoff = ticket["config"]["training_end_nano" if partition == "TRAIN" else "calibration_end_nano"]
        require(all(label.available_at_nano is not None and label.available_at_nano < cutoff and
                    label.window_end_nano < cutoff for label in segment.labels), "FIT_INPUT_INFLUENCE_BOUNDARY")
        require(all((f.duration_seconds == 86400) == (mode == "DAILY") for f in record_infos(segment.features)), "FIT_INPUT_MODE_MISMATCH")
        segments[partition] = segment
    reference = decode_reference(ticket["reference"])
    return ticket, segments, reference


def fit_only(ticket_path, output):
    """CLI subprocess boundary: no source collection, TEST, sessions or tail argument."""
    ticket, segments, reference = read_fit_ticket(ticket_path)
    require(ticket["status"] == "READY_FOR_STAGE2", "FIT_MODE_INSUFFICIENT")
    began = time.perf_counter()
    fitted = fit_direction(segments["TRAIN"], segments["CALIBRATION"], ticket["config"], reference)
    require(fitted.inner["sufficient"], "FIT_INNER_INSUFFICIENT")
    result = {"schema": "k45-frozen-models-v2", "purpose": ticket["purpose"], "mode": ticket["mode"],
              "identity": ticket["identity"], "ticket": pin(ticket_path), "code_pins": ticket["code_pins"],
              "fit": fitted.payload(), "elapsed_seconds": time.perf_counter() - began,
              "calibration_request_ids_sha256": sha256_hex(canonical_json_bytes(sorted(r.request_id for r in record_infos(segments["CALIBRATION"].requests)))),
              "train_days": len({r.features.trading_day for r in record_infos(segments["TRAIN"].requests)}),
              "calibration_days": len({r.features.trading_day for r in record_infos(segments["CALIBRATION"].requests)}),
              **actual_test_counts(),
              "safety": SAFETY}
    dump_new(output, result, compressed=True)
    return {"status": "FIT_FROZEN", "mode": ticket["mode"], "artifact": pin(output),
            "elapsed_seconds": result["elapsed_seconds"]}


def decode_fit(payload):
    require(payload["schema"] == "k45-direction-fit-v2" and payload["production_publishable"] is False and
            payload["production_calibration_evidence"] == "NOT_BUILT_DIRECTION_ONLY", "DIRECTION_MODEL_SCHEMA")
    return DirectionFit(payload["config"], payload["reference_id"], payload["models"], payload["raw_control"],
                        payload["calibrated_control"], payload["sample_ids"], payload["inner"])


def selected_algorithms(fitted, mode, product):
    result = {}
    for horizon in HORIZONS[mode]:
        h = Horizon("TRADING_DAY" if mode == "DAILY" else "TRADING_MINUTE", horizon)
        record = models._selected_model(fitted.models, product, mode, h, models.COMMON_BARRIER, models.TASK_DIRECTION)
        result[horizon] = None if record is None else record["algorithm"]
    return result


def canonical_requests(requests):
    by_unit = defaultdict(list)
    for index, request in enumerate(record_infos(requests)):
        by_unit[request.features.snapshot_id, request.horizon].append(index)
    result = []
    for _, group in sorted(by_unit.items(), key=lambda item: (item[0][0], item[0][1].unit, item[0][1].value)):
        require(len(group) == 3 and {record_info(requests, index).barrier_key for index in group}
                == {"ATR_0.5", "ATR_1", "ATR_2"}, "DIRECTION_THREE_BARRIER_IDENTITY")
        result.append(next(index for index in group if record_info(requests, index).barrier_key == "ATR_1"))
    if isinstance(requests, RecordView): return requests.select(result)
    if isinstance(requests, RecordRows): return RecordRows(members=(requests.members[index] for index in result))
    return tuple(requests[index] for index in result)


def predict_rows(fitted, segment, *, purpose="R001"):
    assessments = {assessment.snapshot_id: assessment for assessment in segment.regimes}
    output = []
    for request in canonical_requests(segment.requests):
        assessment = assessments[request.features.snapshot_id]
        output.append({"request_id": request.request_id, "day": request.features.trading_day.isoformat(),
            "mode": "DAILY" if request.features.duration_seconds == 86400 else "MINUTE", "horizon": request.horizon.value,
            **{name: direction_probability(fitted, request, assessment, component, purpose=purpose) for name, component in (
                ("model", "models"), ("raw", "raw_control"), ("calibrated", "calibrated_control"))}})
    return output


def score_predictions(predictions, labels, sessions, mode, fitted, *, purpose="R001"):
    from k45_forecast_statistics import Observation, score_mode
    dates = FORMAL_DATES if purpose == "R001" else DRY_DATES
    calendar = sorted({s.trading_day.isoformat() for s in sessions if dates["TEST"][0] <= s.trading_day <= dates["TEST"][1]})
    by_id = {label.request_id: label for label in labels}
    require(len(by_id) == len(labels), "SCORING_LABEL_IDENTITY")
    rows = [Observation(row["request_id"], row["day"], mode, row["horizon"],
            by_id[row["request_id"]].direction if row["request_id"] in by_id and by_id[row["request_id"]].status == "MATURE" else None,
            row["model"], row["raw"], row["calibrated"]) for row in predictions]
    product = next((s.product for s in sessions), "SHFE.RB")
    return score_mode(rows, calendar, mode, selected_algorithms(fitted, mode, product), fitted.inner["sufficient"])


def stage_one(output, capacity_path):
    """All real TEST numerical payloads go only to sealed external files."""
    capacity = load_json(capacity_path)
    require(capacity.get("schema") == "k45-capacity-check-v1" and capacity.get("status") == "READY"
            and capacity.get("source_collection_sha256") == SOURCE_COLLECTION_SHA
            and capacity.get("tool_sha256") == pin(__file__)["sha256"], "CAPACITY_PREFLIGHT_REQUIRED")
    for source in capacity["evidence_pins"]: verify_pin(source)
    output = Path(output)
    require(not output.exists(), "RUN_OUTPUT_ALREADY_EXISTS")
    output.mkdir(parents=True)
    started = time.perf_counter()
    sources = code_pins()
    collection, shards = source_identity()
    timing = Timings()
    records, sessions, selection = [], None, None
    for contract, item in sorted(shards.items()):
        bundle, check = timing.call(("input_validation", contract), read_original_shard,
                                    item["path"], item["manifest_sha256"])
        require({i.contract_id for i in bundle.instruments} == {contract}, "SHARD_INSTRUMENT_IDENTITY")
        if sessions is None:
            sessions = bundle.sessions
            selection = load_json(Path(item["path"]) / "selection-evidence.json")
        require(bundle.sessions == sessions, "SHARED_CALENDAR_CONTENT_MISMATCH")
        records.append(dict(check, contract=contract))
        del bundle
    require(len({s.trading_day for s in sessions}) == 452 and
            min(s.trading_day for s in sessions) == FORMAL_DATES["TRAIN"][0] and
            max(s.trading_day for s in sessions) == MATURITY_END, "FIXED_SOURCE_CALENDAR")
    selected, selection_exclusions = select_contracts(selection, sessions, set(shards))
    feature_stores = {(mode, partition): new_store() for mode in HORIZONS for partition in FORMAL_DATES}
    audits, rules, scenarios, dry_inputs = [], {}, [], []
    for contract, item in sorted(shards.items()):
        bundle, _ = timing.call(("input_revalidation", contract), read_original_shard, item["path"], item["manifest_sha256"])
        scenario, check = scenario_view(bundle)
        scenarios.append(dict(check, contract=contract, source_manifest_sha256=item["manifest_sha256"]))
        for rule in scenario.rules:
            old = rules.setdefault(rule.rule_id, rule)
            require(old == rule, "RULE_IDENTITY_CONFLICT")
        _, anchor_audits = feature_rows(scenario, selected, timing, stores=feature_stores)
        audits.extend(anchor_audits)
        if any(row["partition"] == "TRAIN" and row["usable"] for row in anchor_audits):
            dry_sessions = tuple(s for s in sessions if s.trading_day <= FORMAL_DATES["TRAIN"][1])
            dry_end = evaluation_time(dry_sessions, "DRY_RUN")
            dry_bars = tuple(b for b in scenario.bars if b.trading_day is not None and
                b.trading_day <= FORMAL_DATES["TRAIN"][1] and b.end_nano < dry_end and
                (b.available_at_nano is None or b.available_at_nano < dry_end))
            dry_inputs.append(dump_new(output / "dry-input" / f"{contract}.json.gz", {
                "schema": "k45-dry-input-v2", "purpose": "DRY_RUN", "contract": contract,
                "source_manifest_sha256": item["manifest_sha256"], "scenario_input_hash": check["scenario_input_hash"],
                "bars": [encode_record(b) for b in dry_bars],
                "instruments": [encode_record(i) for i in scenario.instruments],
                "selection": [{"day": day.isoformat(), "duration": duration, "contract": chosen}
                    for (day, duration), chosen in selected.items() if chosen == contract and day <= FORMAL_DATES["TRAIN"][1]],
                "rules": [encode_record(r) for r in scenario.rules],
                "sessions": [encode_record(s) for s in dry_sessions]}, compressed=True))
        print(json.dumps({"phase": "STAGE1_FEATURES", "validated_shards": len(scenarios),
                          "feature_count": sum(store.logical_snapshot_count for store in feature_stores.values()),
                          "actual_test_calls": actual_test_counts()}), flush=True)
        del bundle, scenario
    require(sum(item["changed_count"] for item in scenarios) == 1_783_377, "SCENARIO_TRANSFORM_COUNT_MISMATCH")
    identity = {"source_collection_sha256": SOURCE_COLLECTION_SHA,
        "scenario_input_hash": sha256_hex(canonical_json_bytes({"source_collection_sha256": SOURCE_COLLECTION_SHA,
            "shard_scenarios": scenarios, "seconds": SCENARIO_SECONDS, "tool_sha256": sources[1]["sha256"]}))}
    references, requests, regimes, request_reasons = {}, {}, {}, {}
    for mode in HORIZONS:
        group = {partition: ordered_view(feature_stores[mode, partition], "FeatureSnapshot") for partition in FORMAL_DATES}
        references[mode], requests[mode], regimes[mode], request_reasons[mode] = build_research_requests(
            group, tuple(rules.values()), sessions, mode, timing)
    intervals = {mode: [] for mode in HORIZONS}
    for contract, item in sorted(shards.items()):
        # Future influence is projected onto timestamps before any label call.
        bundle, _ = timing.call(("input_time_projection", contract), read_original_shard, item["path"], item["manifest_sha256"])
        scenario, _ = scenario_view(bundle)
        index = time_index(scenario)
        for mode in HORIZONS:
            for request in select_records(requests[mode], lambda r: r.features.contract_id == contract):
                before = time.perf_counter()
                interval = index.interval(request, request.window_end_nano)
                intervals[mode].append(interval)
                if partition_day(request.features.trading_day) == "TRAIN":
                    timing.values["time_interval", mode, str(request.horizon.value)].append(time.perf_counter() - before)
        del bundle, scenario, index
    assignments = {mode: timing.call(("split", mode), assign_splits, tuple(intervals[mode]),
                                     split_for(sessions, mode), sessions) for mode in HORIZONS}
    by_assignment = {mode: {a.sample_id: a for a in values} for mode, values in assignments.items()}
    by_interval = {mode: {a.sample_id: a for a in values} for mode, values in intervals.items()}
    labels = {mode: [] for mode in HORIZONS}
    for contract, item in sorted(shards.items()):
        relevant = {mode: select_records(requests[mode], lambda r: r.features.contract_id == contract and
            by_assignment[mode][r.request_id].partition in ("TRAIN", "CALIBRATION")) for mode in HORIZONS}
        if not any(relevant.values()): continue
        bundle, _ = timing.call(("input_labels", contract), read_original_shard, item["path"], item["manifest_sha256"])
        scenario, _ = scenario_view(bundle)
        index = _WindowIndex(scenario)
        for mode in HORIZONS:
            split = split_for(sessions, mode)
            for request in relevant[mode]:
                before = time.perf_counter()
                label = guarded_label(request, by_interval[mode][request.request_id], by_assignment[mode][request.request_id],
                                      split, index.window(request), sessions)
                labels[mode].append(label)
                if by_assignment[mode][request.request_id].partition == "TRAIN":
                    timing.values["labels", mode, str(request.horizon.value)].append(time.perf_counter() - before)
        del bundle, scenario, index
    tickets, mode_checks, funnels, sealed = {}, {}, [], []
    for mode in HORIZONS:
        split = split_for(sessions, mode)
        keys = tuple(spec.factor_key for spec in factor_registry_v2(86400 if mode == "DAILY" else 60))
        config = models.default_fit_config(keys, split.train_end_nano, split.calibration_end_nano, evaluation_time(sessions) + 1)
        label_by_id = {label.request_id: label for label in labels[mode]}
        segments = {}
        for partition in ("TRAIN", "CALIBRATION", "TEST"):
            kept = select_records(requests[mode], lambda r: by_assignment[mode][r.request_id].partition == partition and
                    (partition == "TEST" or label_by_id[r.request_id].status == "MATURE"))
            segments[partition] = make_segment(kept, regimes[mode], [] if partition == "TEST" else
                                              [label_by_id[r.request_id] for r in record_infos(kept)])
        if segments["TRAIN"].requests or segments["CALIBRATION"].requests:
            checked_config, train_samples, cal_samples = joined_samples(segments["TRAIN"], segments["CALIBRATION"], config, references[mode])
            _, _, inner = inner_partition(train_samples, checked_config)
        else:
            train_samples, cal_samples = [], []
            inner = {"sufficient": False, "fit_days": 0, "development_days": 0, "reason": "NO_LEGAL_INPUTS"}
        test_days = {str(h): len({r.features.trading_day for r in record_infos(segments["TEST"].requests)
                                if r.horizon.value == h}) for h in HORIZONS[mode]}
        sufficient = inner["sufficient"] and any(count >= 60 for count in test_days.values())
        mode_checks[mode] = {"status": "READY_FOR_STAGE2" if sufficient else "INSUFFICIENT",
                             "inner": inner, "legal_test_request_days": test_days,
                             "unknown_regime_count": sum(a.base == "UNKNOWN" for a in regimes[mode].values()),
                             "regime_count": len(regimes[mode])}
        funnels.extend(funnel(mode, audits, requests[mode], assignments[mode], labels[mode], train_samples,
                              cal_samples, request_reasons[mode]))
        paths = {}
        for partition in ("TRAIN", "CALIBRATION", "TEST"):
            item = timing.call(("record_stream_write", mode, partition), write_segment,
                output / ("sealed-test" if partition == "TEST" else "fit-input") / f"{mode}-{partition}.jsonl.gz",
                segments[partition], identity, "R001", mode, partition)
            if partition == "TEST": sealed.append(item)
            else: paths[partition] = item
        ticket = {"schema": "k45-fit-ticket-v2", "purpose": "R001", "mode": mode, "identity": identity,
                  "code_pins": sources, "segments": paths, "reference": reference_payload(references[mode]),
                  "config": config, "status": mode_checks[mode]["status"]}
        tickets[mode] = dump_new(output / "fit-input" / f"{mode}-ticket.json", ticket)
    timing_payload = timing.report()
    elapsed = time.perf_counter() - started
    check = {"schema": "k45-stage1-check-v2", "identity": identity, "code_pins": sources,
        "input_validation": records, "scenario_shards": scenarios, "mode_checks": mode_checks,
        "capacity_preflight": pin(capacity_path),
        "fit_tickets": tickets, "sealed_test": sealed, "dry_inputs": dry_inputs,
        "source_shards": shards, "sessions": [encode_record(s) for s in sessions],
        "selection_exclusions": [{"day": day.isoformat(), "duration": duration, "reasons": list(reasons)}
            for (day, duration), reasons in sorted(selection_exclusions.items())],
        "funnel": funnels, "timings": timing_payload, "elapsed_stage1_seconds": elapsed,
        "anchor_history_metadata": audits,
        **actual_test_counts(),
        "safety": SAFETY, "classification": "SCENARIO / NOT_ASSESSED",
        "minute_assumption": "假设分钟线在收盘后5秒可用。"}
    dump_new(output / "stage1-measurements.json", check)
    return check


def synthetic_costs(output):
    """Default-parameter synthetic measurements only; no source data argument."""
    from k45_forecast_statistics import Observation, score_mode
    path = REPOSITORY / "tests/experiments/test_k45_forecast_skill.py"
    spec = importlib.util.spec_from_file_location("k45_synthetic_cost_fixture", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    timings = []
    for mode in HORIZONS:
        training, calibration, config, reference = module.synthetic_case(mode)
        checked, train, cal = joined_samples(training, calibration, config, reference)
        inner, development, _ = inner_partition(train, checked)
        fit_ids, dev_ids = {s.request_id for s in inner}, {s.request_id for s in development}
        for horizon in HORIZONS[mode]:
            full = models._usable_samples(models.TASK_DIRECTION, models._common_samples(sorted(
                [s for s in train if s.horizon.value == horizon], key=lambda s: (s.decision_nano, s.request_id))))
            inner_cell = [s for s in full if s.request_id in fit_ids]
            dev_cell = [s for s in full if s.request_id in dev_ids]
            for stage, samples in (("inner_fit", inner_cell), ("full_refit", full)):
                center, scale = models._standardize_stats([s.vector for s in samples])
                for algorithm in models._task_algorithms(models.TASK_DIRECTION):
                    before = time.perf_counter()
                    model = models._fit_candidate(models.TASK_DIRECTION, algorithm, samples, checked, center, scale)
                    elapsed = time.perf_counter() - before
                    require(model is not None, "SYNTHETIC_COST_CANDIDATE_FAILED")
                    timings.append({"component": stage, "mode": mode, "horizon": horizon, "algorithm": algorithm,
                                    "n": len(samples), "seconds": elapsed})
                    if stage == "inner_fit":
                        before = time.perf_counter()
                        predictions = [models._apply_candidate(models.TASK_DIRECTION, model, sample, center, scale,
                            checked["minimum_leaf_blocks"], horizon) for sample in dev_cell]
                        models._candidate_dev_loss(models.TASK_DIRECTION, dev_cell, predictions, checked["quantiles"], horizon)
                        timings.append({"component": "development_loss", "mode": mode, "horizon": horizon,
                                        "algorithm": algorithm, "n": len(dev_cell), "seconds": time.perf_counter() - before})
        before = time.perf_counter()
        fitted = fit_direction(training, calibration, config, reference)
        timings.append({"component": "full_direction_chain", "mode": mode, "n": len(train),
                        "seconds": time.perf_counter() - before})
        before = time.perf_counter()
        model_copy = copy.deepcopy(fitted.models)
        control_copy = copy.deepcopy(fitted.raw_control)
        models._apply_calibration_temperatures(model_copy, checked, cal)
        models._apply_calibration_temperatures(control_copy, checked, cal)
        timings.append({"component": "calibration_two_components", "mode": mode, "n": len(cal),
                        "seconds": time.perf_counter() - before})
        features, requests, _, _ = module.make_segment([330], mode, "COST-PREDICT")
        prediction_segment = Segment(features, module.regimes(features, reference.reference_id), requests, ())
        before = time.perf_counter()
        predictions = predict_rows(fitted, prediction_segment)
        timings.append({"component": "prediction_three_components", "mode": mode, "n": len(predictions),
                        "seconds": time.perf_counter() - before})
        calendar = [f"SYNTHETIC-{i:03}" for i in range(92)]
        rows = [Observation(f"SYNTHETIC-{h}-{day}", day, mode, h, "UP", (.6, .2, .2),
                (.4, .3, .3), (.5, .25, .25)) for h in HORIZONS[mode] for day in calendar]
        before = time.perf_counter()
        result = score_mode(rows, calendar, mode, {h: "SYNTHETIC" for h in HORIZONS[mode]}, True)
        raw = canonical_json_bytes(result)
        timings.append({"component": "bootstrap_and_summary_output", "mode": mode, "n": len(rows),
                        "seconds": time.perf_counter() - before, "summary_bytes": len(raw)})
        before = time.perf_counter()
        identity = {"source_collection_sha256": "0" * 64, "scenario_input_hash": "0" * 64}
        member = write_segment(Path(output).with_name(Path(output).stem + f"-{mode}-stream.jsonl.gz"),
                               training, identity, "SYNTHETIC", mode, "TRAIN")
        restored = read_segment(member, identity, "SYNTHETIC", mode, "TRAIN")
        require(len(restored.requests) == len(training.requests), "SYNTHETIC_STREAM_COUNT")
        timings.append({"component": "row_output_roundtrip", "mode": mode, "n": len(training.requests),
                        "seconds": time.perf_counter() - before, "bytes": member["bytes"],
                        "logical_bytes": member["logical_bytes"], "artifact": member})
        # Measure complete source expansion, canonical hashing, gzip, strict
        # restoration and production re-append at realistic prefix lengths.
        # This is SYNTHETIC; no real factor or price payload is inspected.
        from ak_trading.v2._fold_records import _deep_size_estimate
        synthetic_ids = tuple("SYNTHETIC:" + hashlib.sha256(str(index).encode()).hexdigest() for index in range(80000))
        original_features, original_requests, _, _ = module.make_segment(range(4), mode, "STREAM-COST")
        long_features = {f.snapshot_id: replace(f, source_bar_ids=synthetic_ids[:count])
            for f, count in zip(original_features, (10000, 30000, 50000, 80000))}
        source_segment = compact_segment(Segment(tuple(long_features.values()), (),
            tuple(replace(r, features=long_features[r.features.snapshot_id]) for r in original_requests), ()))
        before = time.perf_counter()
        source_member = write_segment(Path(output).with_name(Path(output).stem + f"-{mode}-sources.jsonl.gz"),
                                     source_segment, identity, "SYNTHETIC", mode, "TRAIN")
        restored = read_segment(source_member, identity, "SYNTHETIC", mode, "TRAIN")
        source_elapsed = time.perf_counter() - before
        timings.append({"component": "source_stream_roundtrip", "mode": mode,
            "n": source_member["logical_bytes"], "seconds": source_elapsed,
            "source_nodes": source_member["source_nodes"], "source_delta_count": source_member["source_delta_count"],
            "logical_bytes": source_member["logical_bytes"], "file_bytes": source_member["bytes"],
            "resident_store_bytes": _deep_size_estimate(restored.features.store), "artifact": source_member,
            "components_included": ["node_closure_write", "full_record_encoding", "logical_hash", "gzip_write",
                                    "file_hash", "gzip_read", "node_restore", "production_decode", "production_append"]})
    value = {"schema": "k45-synthetic-costs-v2", "classification": "SYNTHETIC", "code_pins": code_pins(),
             "seed": 7, "max_iterations": 500, "tree_count": 50, "feature_columns": 10, "timings": timings,
             "source_test_values_read": False, "test_label_calls": 0, "test_probability_calls": 0}
    dump_new(output, value)
    return {"status": "SYNTHETIC_COSTS_MEASURED", "artifact": pin(output), "components": len(timings)}


def planning_budget(stage1_path, costs_path, output):
    started = time.perf_counter()
    check, costs = load_json(stage1_path), load_json(costs_path)
    require(costs["schema"] == "k45-synthetic-costs-v2" and costs["classification"] == "SYNTHETIC" and
            costs["max_iterations"] == 500 and costs["tree_count"] == 50, "COST_EVIDENCE_SCHEMA")
    for source in (*check["code_pins"], *costs["code_pins"]): verify_pin(source)
    entries, missing = [], []
    components = costs["timings"]
    for mode in HORIZONS:
        if check["mode_checks"][mode]["status"] != "READY_FOR_STAGE2": continue
        ticket_pin = check["fit_tickets"][mode]
        verify_pin(ticket_pin)
        ticket, segments, reference = read_fit_ticket(ticket_pin["path"])
        config, train, cal = joined_samples(segments["TRAIN"], segments["CALIBRATION"], ticket["config"], reference)
        inner, dev, _ = inner_partition(train, config)
        fit_ids, dev_ids = {s.request_id for s in inner}, {s.request_id for s in dev}
        for horizon in HORIZONS[mode]:
            common = models._usable_samples(models.TASK_DIRECTION, models._common_samples(sorted(
                [s for s in train if s.horizon.value == horizon], key=lambda s: (s.decision_nano, s.request_id))))
            sizes = {"inner_fit": sum(s.request_id in fit_ids for s in common),
                     "development_loss": sum(s.request_id in dev_ids for s in common), "full_refit": len(common)}
            for component, count in sizes.items():
                for algorithm in models._task_algorithms(models.TASK_DIRECTION):
                    matches = [row for row in components if row["component"] == component and row["mode"] == mode
                               and row.get("horizon") == horizon and row.get("algorithm") == algorithm]
                    if len(matches) != 1 or matches[0]["n"] <= 0:
                        missing.append(f"{component}:{mode}:{horizon}:{algorithm}")
                        continue
                    source = matches[0]
                    factor = count / source["n"]
                    # Linear sample extrapolation is disclosed, not a runtime bound.
                    entries.append({"component": component, "mode": mode, "horizon": horizon,
                        "algorithm": algorithm, "n": count, "unit_seconds": source["seconds"] / source["n"],
                        "estimated_seconds": source["seconds"] * factor,
                        "source": "SYNTHETIC_DEFAULT_PARAMETERS_LINEAR_SAMPLE_EXTRAPOLATION"})
                    if component == "full_refit" and algorithm == "UNCONDITIONAL_FREQUENCY":
                        entries.append(dict(entries[-1], component="full_train_controls", estimated_seconds=2 * source["seconds"] * factor))
        test_rows = next(row["legal_direction_units"] for row in check["funnel"] if row["mode"] == mode and row["partition"] == "TEST" and row["horizon"] is None)
        for component, count in (("calibration_two_components", len(cal)), ("prediction_three_components", test_rows),
                                 ("row_output_roundtrip", len(train) + len(cal) + 3 * test_rows)):
            source = next((row for row in components if row["mode"] == mode and row["component"] == component), None)
            if source is None or source["n"] <= 0: missing.append(f"{component}:{mode}")
            else: entries.append({"component": component, "mode": mode, "n": count,
                "unit_seconds": source["seconds"] / source["n"], "estimated_seconds": count * source["seconds"] / source["n"],
                "source": "SYNTHETIC_LINEAR_SAMPLE_EXTRAPOLATION"})
        source = next((row for row in components if row["mode"] == mode and row["component"] == "bootstrap_and_summary_output"), None)
        if source is None: missing.append(f"bootstrap_and_summary_output:{mode}")
        else: entries.append({"component": "bootstrap_and_summary_output", "mode": mode, "n": 20_000,
                              "estimated_seconds": source["seconds"], "source": "SYNTHETIC_SAME_92_DAY_SHAPE"})
        streams = [member for member in (*ticket["segments"].values(), *check["sealed_test"])
                   if member.get("scope", {}).get("mode") == mode]
        logical_bytes = sum(member["logical_bytes"] for member in streams)
        source_cost = next((row for row in components if row["component"] == "source_stream_roundtrip"
                            and row["mode"] == mode), None)
        if source_cost is None or source_cost["logical_bytes"] <= 0:
            missing.append(f"source_stream_roundtrip:{mode}")
        else:
            entries.append({"component": "source_stream_full_io_and_restore", "mode": mode,
                "n": logical_bytes, "full_roundtrip_allowances": 3,
                "estimated_seconds": 3 * logical_bytes * source_cost["seconds"] / source_cost["logical_bytes"],
                "source": "SYNTHETIC_LONG_PREFIX_FULL_ENCODING_HASH_GZIP_DECODER_APPEND_PER_BYTE",
                "covers": "Budget/fit/TEST restored streams; rehearsal copies repeated by the full-workload rehearsal allowance"})
        for horizon in HORIZONS[mode]:
            source = [row for row in check["timings"] if row["component"] == "labels" and row["dimensions"] == [mode, str(horizon)]]
            count = next(row["legal_direction_units"] for row in check["funnel"] if row["mode"] == mode
                         and row["partition"] == "TEST" and row["horizon"] == horizon)
            if len(source) != 1 or source[0]["calls"] <= 0: missing.append(f"labels:{mode}:{horizon}")
            else: entries.append({"component": "test_labels", "mode": mode, "horizon": horizon, "n": count,
                "unit_seconds": source[0]["mean_seconds"], "estimated_seconds": count * source[0]["mean_seconds"],
                "source": "ACTUAL_TRAIN_UNIT_COST"})
    history_probe = Path("C:/AKR71114/reproductions/r7-six-probes-20261114T044158Z-e973a58a/BATCH-RESULT.json")
    verify_pin({"path": str(history_probe), "sha256": "b6fdcf598801df5bf429020ed3c30fcad78b151d4846ecd413a031049e3835a7"})
    history_record = next(p for p in load_json(history_probe)["probes"] if p["appendix"] == 7)
    # Fixed, already observed same-machine values, parsed from the sealed
    # summary. This does not repeat the historical probe or read TEST data.
    synthetic_history = []
    for line in history_record["summary_lines"][:5]:
        columns = line.split()
        synthetic_history.append((int(columns[0].replace(",", "")), float(columns[1]) / 1000))
    history, dry_feature_seconds = [], 0.0
    for mode in HORIZONS:
        for bin_index in range(5):
            train = [row for row in check["anchor_history_metadata"] if row["mode"] == mode and row["partition"] == "TRAIN"
                     and row["history_count"] is not None and history_bin(row["history_count"]) == bin_index]
            later = [row for row in check["anchor_history_metadata"] if row["mode"] == mode and row["partition"] != "TRAIN"
                     and row["history_count"] is not None and history_bin(row["history_count"]) == bin_index]
            observations = [row for row in check["timings"] if row["component"] == "features" and
                            row["dimensions"][0] == mode and row["dimensions"][2] == str(bin_index)]
            unit = math.fsum(row["total_seconds"] for row in observations) / sum(row["calls"] for row in observations) if observations else None
            source = "ACTUAL_TRAIN"
            if unit is None and (train or later):
                largest = max(row["history_count"] for row in train + later)
                matching = [cost for count, cost in synthetic_history if count >= largest] if mode == "MINUTE" else []
                if matching: unit, source = matching[0], "SYNTHETIC_SAME_MACHINE_LONGER_HISTORY"
                else: missing.append(f"feature_history:{mode}:{bin_index}")
            dry_feature_seconds += len(train) * unit if unit is not None else 0
            history.append({"mode": mode, "bin": bin_index, "train_anchors": len(train), "later_anchors": len(later),
                            "unit_seconds": unit, "source": source, "later_feature_extrapolation_seconds":
                            len(later) * unit if unit is not None else None, "later_actual_cost_already_in_stage1": True})
    # Rehearsal repeats fits, requests, references, labels and scoring. Using the
    # complete formal workload is a deliberately larger planning workload.
    base_remaining = math.fsum(row["estimated_seconds"] for row in entries)
    measured_request_reference = math.fsum(row["total_seconds"] for row in check["timings"]
        if row["component"] in ("requests", "reference_final", "reference_prefix", "assessment", "time_interval", "split", "labels"))
    input_measured = math.fsum(row["total_seconds"] for row in check["timings"] if row["component"].startswith("input"))
    entries.extend([
        {"component": "rehearsal", "n": "FULL_FORMAL_WORKLOAD_UPPER_SIZE_ASSUMPTION",
         "estimated_seconds": base_remaining + measured_request_reference, "source": "EXTRAPOLATED_ENGINEERING_CHAIN"},
        {"component": "remaining_input_verification_and_decode", "n": 30,
         "estimated_seconds": input_measured, "source": "ALL_MEASURED_STAGE1_INPUT_PASSES_AS_PLANNING_ALLOWANCE"},
        {"component": "rehearsal_features", "n": sum(row["partition"] == "TRAIN" for row in check["anchor_history_metadata"]),
         "estimated_seconds": dry_feature_seconds, "source": "ACTUAL_TRAIN_HISTORY_BIN_UNIT_COSTS"},
    ])
    estimated = math.fsum(row["estimated_seconds"] for row in entries)
    elapsed_budget = time.perf_counter() - started
    total = check["elapsed_stage1_seconds"] + elapsed_budget + 2 * estimated
    status = "STOP:NOT_ESTIMABLE" if missing else "STOP:PLANNING_EXCEEDS_72H" if total > 72 * 3600 else (
        "READY_FOR_STAGE2" if any(row["status"] == "READY_FOR_STAGE2" for row in check["mode_checks"].values()) else "STOP:BOTH_MODES_INSUFFICIENT")
    result = {"schema": "k45-planning-budget-v2", "stage1": pin(stage1_path), "synthetic_costs": pin(costs_path),
        "components": entries, "history_bins": history, "missing_components": missing,
        "elapsed_stage1_seconds": check["elapsed_stage1_seconds"], "estimated_remaining_seconds": estimated,
        "elapsed_budget_read_restore_seconds": elapsed_budget,
        "planning_total_seconds": total, "fixed_margin": 2, "status": status,
        "limitation": "Planning approximation; sample scaling and larger rehearsal workload are not mathematical upper bounds."}
    dump_new(output, result)
    return result


def run_fit_subprocess(ticket, output, capture):
    """Child inherits the supervising job; the fit CLI receives only its ticket."""
    capture = Path(capture)
    capture.mkdir(parents=True, exist_ok=False)
    argv = [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "fit", "--ticket", str(ticket), "--output", str(output)]
    dump_new(capture / "request.json", {"argv": argv, "cwd": str(REPOSITORY), "ticket": pin(ticket)})
    with (capture / "stdout.txt").open("xb") as stdout, (capture / "stderr.txt").open("xb") as stderr:
        result = subprocess.run(argv, cwd=REPOSITORY, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, check=False)
        native = result.returncode
    streams = []
    for name in ("stdout.txt", "stderr.txt"):
        path = capture / name
        compressed = path.with_suffix(path.suffix + ".gz")
        with compressed.open("xb") as stream: stream.write(gzip.compress(path.read_bytes(), mtime=0))
        streams.append(pin(compressed))
    dump_new(capture / "result.json", {"raw_exit": native, "streams": streams,
                                     "outcome": "FIT_FROZEN" if native == 0 else "FIT_FAILED"})
    require(native == 0, "FIT_SUBPROCESS_FAILED")
    return pin(output)


def read_dry_bundle(member):
    verify_pin(member)
    value = load_json(member["path"], compressed=True)
    require(set(value) == {"schema", "purpose", "contract", "source_manifest_sha256", "scenario_input_hash",
                          "bars", "instruments", "selection", "rules", "sessions"} and value["schema"] == "k45-dry-input-v2"
            and value["purpose"] == "DRY_RUN", "DRY_INPUT_SCHEMA")
    # This bounded shard input is formal TRAIN only; never open a real TEST
    # segment or accumulate all decoded shards in the rehearsal process.
    for key in ("bars", "sessions"):
        for row in value[key]:
            require(row["trading_day"] is not None and date.fromisoformat(row["trading_day"]) <= FORMAL_DATES["TRAIN"][1],
                    "DRY_INPUT_FORMAL_TRAIN_ONLY")
    sessions = tuple(decode_record(SessionWindow, row) for row in value["sessions"])
    bars = tuple(decode_record(MarketBar, row) for row in value["bars"])
    rules = tuple(decode_record(TradingRule, row) for row in value["rules"])
    instruments = tuple(decode_record(Instrument, row) for row in value["instruments"])
    selected = {(date.fromisoformat(row["day"]), row["duration"]): row["contract"] for row in value["selection"]}
    require(all(day <= FORMAL_DATES["TRAIN"][1] for day, _ in selected), "DRY_SELECTION_FORMAL_TRAIN_ONLY")
    require({b.contract_id for b in bars} <= {value["contract"]}, "DRY_INPUT_CONTRACT_IDENTITY")
    return FrozenInput(Path(member["path"]).parent, value["source_manifest_sha256"], instruments,
                       sessions, bars, (), rules), selected, value["contract"]


def read_dry_inputs(pins, timing):
    stores = {(mode, partition): new_store() for mode in HORIZONS for partition in DRY_DATES}
    rules, sessions = {}, None
    for member in pins:
        bundle, selected, _ = timing.call(("input_dry_features",), read_dry_bundle, member)
        if sessions is None: sessions = bundle.sessions
        require(sessions == bundle.sessions, "DRY_SHARED_SESSIONS")
        feature_rows(bundle, selected, timing, purpose="DRY_RUN", stores=stores)
        for rule in bundle.rules:
            require(rules.setdefault(rule.rule_id, rule) == rule, "DRY_RULE_IDENTITY")
        del bundle
    require(sessions is not None, "DRY_INPUT_MISSING")
    return stores, tuple(rules.values()), sessions


def rehearsal(stage1, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    timing = Timings()
    feature_stores, rules, sessions = read_dry_inputs(stage1["dry_inputs"], timing)
    diagnostics, artifacts = {}, []
    for mode in HORIZONS:
        selected_features = {partition: ordered_view(feature_stores[mode, partition], "FeatureSnapshot") for partition in DRY_DATES}
        reference, requests, assessments, request_reasons = build_research_requests(selected_features, rules,
            sessions, mode, timing, purpose="DRY_RUN")
        intervals = []
        for member in stage1["dry_inputs"]:
            bundle, _, contract = timing.call(("input_dry_intervals", mode), read_dry_bundle, member)
            metadata = time_index(bundle)
            intervals.extend(metadata.interval(request, request.window_end_nano)
                for request in select_records(requests, lambda r: r.features.contract_id == contract))
            del bundle, metadata
        split = split_for(sessions, mode, purpose="DRY_RUN")
        assignments = assign_splits(tuple(intervals), split, sessions)
        by_interval, by_assignment = {r.sample_id: r for r in intervals}, {r.sample_id: r for r in assignments}
        labels, segments = {}, {}
        all_train_cal_labels = []
        for member in stage1["dry_inputs"]:
            bundle, _, contract = timing.call(("input_dry_labels", mode), read_dry_bundle, member)
            index = _WindowIndex(bundle)
            for request in select_records(requests, lambda r: r.features.contract_id == contract
                    and by_assignment[r.request_id].partition in ("TRAIN", "CALIBRATION")):
                all_train_cal_labels.append(guarded_label(request, by_interval[request.request_id],
                    by_assignment[request.request_id], split, index.window(request), sessions, purpose="DRY_RUN"))
            del bundle, index
        by_label = {label.request_id: label for label in all_train_cal_labels}
        for partition in ("TRAIN", "CALIBRATION"):
            group = select_records(requests, lambda r: by_assignment[r.request_id].partition == partition)
            all_labels = [by_label[r.request_id] for r in record_infos(group)]
            mature = {label.request_id: label for label in all_labels if label.status == "MATURE"}
            kept = select_records(group, lambda r: r.request_id in mature)
            segments[partition] = make_segment(kept, assessments, [mature[r.request_id] for r in record_infos(kept)])
            labels[partition] = all_labels
        test = select_records(requests, lambda r: by_assignment[r.request_id].partition == "TEST")
        segments["TEST"] = make_segment(test, assessments, ())
        keys = tuple(s.factor_key for s in factor_registry_v2(86400 if mode == "DAILY" else 60))
        config = models.default_fit_config(keys, split.train_end_nano, split.calibration_end_nano,
                                           evaluation_time(sessions, "DRY_RUN") + 1)
        if segments["TRAIN"].requests or segments["CALIBRATION"].requests:
            checked, train_samples, _ = joined_samples(segments["TRAIN"], segments["CALIBRATION"], config, reference)
            _, _, inner = inner_partition(train_samples, checked)
        else:
            inner = {"sufficient": False, "fit_days": 0, "development_days": 0}
        if not inner["sufficient"]:
            diagnostics[mode] = {"status": "DRY_RUN_NOT_RUN:INNER_INSUFFICIENT", "inner": inner,
                                 "test_probability_calls": 0, "test_label_calls": 0, "scoring_runs": 0}
            continue
        segment_pins = {}
        for partition in ("TRAIN", "CALIBRATION", "TEST"):
            member = timing.call(("record_stream_write", mode, partition), write_segment,
                output / ("sealed-diagnostics" if partition == "TEST" else "fit-input") / f"{mode}-{partition}.jsonl.gz",
                segments[partition], stage1["identity"], "DRY_RUN", mode, partition)
            if partition == "TEST":
                segments[partition] = timing.call(("record_stream_read", mode, partition), read_segment,
                    member, stage1["identity"], "DRY_RUN", mode, partition)
            else:
                # The isolated fit child restores TRAIN/CAL. The parent has
                # finished with these records; retaining another decoded copy
                # would add a second full factor population to the tree peak.
                segment_pins[partition] = member
                del segments[partition]
        ticket_path = output / "fit-input" / f"{mode}-ticket.json"
        dump_new(ticket_path, {"schema": "k45-fit-ticket-v2", "purpose": "DRY_RUN", "mode": mode,
            "identity": stage1["identity"], "code_pins": stage1["code_pins"], "segments": segment_pins,
            "reference": reference_payload(reference), "config": config, "status": "READY_FOR_STAGE2"})
        fit_pin = run_fit_subprocess(ticket_path, output / f"{mode}-models.json.gz", output / f"{mode}-capture")
        fitted = decode_fit(load_json(fit_pin["path"], compressed=True)["fit"])
        predictions = predict_rows(fitted, segments["TEST"], purpose="DRY_RUN")
        prediction_pin = dump_new(output / "sealed-diagnostics" / f"{mode}-predictions.json.gz", predictions, compressed=True)
        canonical = canonical_requests(segments["TEST"].requests)
        by_test_label = {}
        for member in stage1["dry_inputs"]:
            bundle, _, contract = timing.call(("input_dry_test_labels", mode), read_dry_bundle, member)
            index = _WindowIndex(bundle)
            for r in select_records(canonical, lambda r: r.features.contract_id == contract):
                by_test_label[r.request_id] = guarded_label(r, by_interval[r.request_id], by_assignment[r.request_id], split,
                    index.window(r), sessions, purpose="DRY_RUN", stage=3, evaluation_as_of=evaluation_time(sessions, "DRY_RUN"))
            del bundle, index
        test_labels = [by_test_label[r.request_id] for r in record_infos(canonical)]
        label_pin = dump_new(output / "sealed-diagnostics" / f"{mode}-labels.json.gz",
                             [encode_record(row) for row in test_labels], compressed=True)
        scores = score_predictions(predictions, test_labels, sessions, mode, fitted, purpose="DRY_RUN")
        CALL_COUNTS["DRY_RUN:TEST:scoring_run"] += 1
        require(all(cell["status"] == "INSUFFICIENT" for cell in scores["cells"]), "DRY_RUN_MUST_NOT_GAIN_60_DAY_QUALIFICATION")
        score_pin = dump_new(output / "sealed-diagnostics" / f"{mode}-statistics.json.gz", scores, compressed=True)
        artifacts.extend((fit_pin, prediction_pin, label_pin, score_pin))
        diagnostics[mode] = {"status": "DRY_RUN_CHAIN_COMPLETED", "inner": inner, "test_request_count": len(predictions),
            "test_probability_calls": 3 * len(predictions), "test_label_calls": len(test_labels), "scoring_runs": 1,
            "calendar_days": scores["calendar_days"], "nonfinite_or_na_metrics": sum(
                metric["skill"] is None for cell in scores["cells"] for metric in cell["metrics"].values()),
            "research_conclusions_published": False, "actual_test_access": False}
    status = "DRY_RUN_COMPLETED" if diagnostics["MINUTE"]["status"] == "DRY_RUN_CHAIN_COMPLETED" else "STOP:MINUTE_REHEARSAL_INSUFFICIENT"
    result = {"schema": "k45-rehearsal-check-v2", "purpose": "DRY_RUN", "status": status,
              "diagnostics": diagnostics, "sealed_artifacts": artifacts, "timings": timing.report(),
              "actual_test_calls": actual_test_counts(), "dry_call_counts": dict(CALL_COUNTS)}
    dump_new(output / "rehearsal-check.json", result)
    return result


def stage_two(stage1_path, budget_path, output):
    output = Path(output)
    require(not output.exists(), "STAGE2_OUTPUT_ALREADY_EXISTS")
    output.mkdir(parents=True)
    stage1, budget = load_json(stage1_path), load_json(budget_path)
    verify_pin(budget["stage1"])
    require(Path(budget["stage1"]["path"]).resolve() == Path(stage1_path).resolve(), "BUDGET_STAGE1_IDENTITY")
    require(budget["status"] == "READY_FOR_STAGE2", "STAGE1_STOP_RULE")
    for item in stage1["code_pins"]: verify_pin(item)
    models_by_mode = {}
    for mode in HORIZONS:
        if stage1["mode_checks"][mode]["status"] != "READY_FOR_STAGE2": continue
        ticket = stage1["fit_tickets"][mode]
        verify_pin(ticket)
        models_by_mode[mode] = run_fit_subprocess(ticket["path"], output / f"{mode}-models.json.gz",
                                                output / f"{mode}-fit-capture")
        print(json.dumps({"phase": "STAGE2_MODEL_FROZEN", "mode": mode, "actual_test_calls": 0}), flush=True)
    dry = rehearsal(stage1, output / "rehearsal")
    freeze = {"schema": "k45-stage2-freeze-v2", "stage1": pin(stage1_path), "budget": pin(budget_path),
              "models": models_by_mode, "rehearsal": pin(output / "rehearsal" / "rehearsal-check.json"),
              "code_pins": stage1["code_pins"], "identity": stage1["identity"],
              "status": "FROZEN_AWAITING_INDEPENDENT_HARD_REVIEW" if dry["status"] == "DRY_RUN_COMPLETED" else dry["status"],
              **actual_test_counts(), "safety": SAFETY}
    dump_new(output / "freeze.json", freeze)
    return freeze


def validate_release(release):
    """Root binds the independent review's exact bytes to the frozen revision."""
    require(set(release) == {"schema", "purpose", "freeze", "review", "reviewer", "independent", "one_time_run"}
            and release["schema"] == "k45-stage3-release-v2" and release["purpose"] == "R001"
            and release["one_time_run"] == "R001" and release["independent"] is True,
            "STAGE3_RELEASE_SCHEMA")
    require(isinstance(release["reviewer"], str) and bool(release["reviewer"]), "INDEPENDENT_REVIEWER_REQUIRED")
    verify_pin(release["review"])
    review = load_json(release["review"]["path"])
    require(review.get("verdict") == "PASS" and review.get("conditional") is False,
            "INDEPENDENT_UNCONDITIONAL_PASS_REQUIRED")
    verify_pin(release["freeze"])
    freeze = load_json(release["freeze"]["path"])
    require(freeze["schema"] == "k45-stage2-freeze-v2" and
            freeze["status"] == "FROZEN_AWAITING_INDEPENDENT_HARD_REVIEW", "STAGE2_FREEZE_REQUIRED")
    for member in (*freeze["code_pins"], freeze["stage1"], freeze["budget"], freeze["rehearsal"], *freeze["models"].values()):
        verify_pin(member)
    require(load_json(freeze["rehearsal"]["path"])["status"] == "DRY_RUN_COMPLETED", "REHEARSAL_REQUIRED")
    return freeze


def stage_three(release_path, output):
    """Never called by stage one/two; consume one R001 after frozen PASS only."""
    release = load_json(release_path)
    freeze = validate_release(release)
    stage1 = load_json(freeze["stage1"]["path"])
    output = Path(output)
    require(not output.exists(), "R001_OUTPUT_ALREADY_EXISTS")
    # An exclusive marker beside the frozen revision prevents changing output
    # directories from turning a consumed research evaluation into another one.
    marker = Path(release["freeze"]["path"]).parent / "R001.started.json"
    require(not marker.exists(), "R001_ALREADY_STARTED")
    dump_new(marker, {"schema": "k45-one-time-r001-v2", "release": pin(release_path), "output": str(output),
                      "recovery_policy": "STOP_AND_PRESERVE; DO_NOT_REGENERATE_EXPOSED_PREDICTIONS"})
    output.mkdir(parents=True)
    sessions = tuple(decode_record(SessionWindow, row) for row in stage1["sessions"])
    segments, fitted, predictions, prediction_pins = {}, {}, {}, {}
    for mode, member in freeze["models"].items():
        verify_pin(member)
        fitted[mode] = decode_fit(load_json(member["path"], compressed=True)["fit"])
        candidates = [p for p in stage1["sealed_test"] if Path(p["path"]).name == f"{mode}-TEST.jsonl.gz"]
        require(len(candidates) == 1, "SEALED_TEST_IDENTITY")
        segments[mode] = read_segment(candidates[0], freeze["identity"], "R001", mode, "TEST")
        require(not segments[mode].labels, "SEALED_TEST_LABELS_FORBIDDEN")
        require(all(partition_day(f.trading_day) == "TEST" for f in record_infos(segments[mode].features)), "SEALED_TEST_DATE_BOUNDARY")
        predictions[mode] = predict_rows(fitted[mode], segments[mode])
        prediction_pins[mode] = dump_new(output / f"{mode}-probabilities.json.gz", predictions[mode], compressed=True)
    # All modes' probabilities are durably frozen before the first label call.
    dump_new(output / "probabilities-frozen.json", {"predictions": prediction_pins, "freeze": release["freeze"]})
    labels, counts = {mode: [] for mode in fitted}, {"test_label_calls": 0,
        "test_probability_calls": 3 * sum(len(rows) for rows in predictions.values()), "test_scoring_run_count": 0}
    intervals = {mode: [] for mode in fitted}
    future = {mode: {} for mode in fitted}
    # Stage three may open future prices. Time split is recomputed and must
    # retain every frozen legal TEST request; altered inputs are a hard failure.
    for contract, item in sorted(stage1["source_shards"].items()):
        group = {mode: select_records(canonical_requests(segments[mode].requests), lambda r: r.features.contract_id == contract)
                 for mode in fitted}
        if not any(group.values()): continue
        bundle, _ = read_original_shard(item["path"], item["manifest_sha256"])
        scenario, _ = scenario_view(bundle)
        index = _WindowIndex(scenario)
        metadata = time_index(scenario)
        for mode in fitted:
            for request in group[mode]:
                intervals[mode].append(metadata.interval(request, request.window_end_nano))
                future[mode][request.request_id] = index.window(request)
    for mode in fitted:
        split = split_for(sessions, mode)
        # The original split used the complete request population. Recomputing
        # embargo on just TEST would change its maximum influence. Use the
        # sealed legal assignments and validate the original interval/segment
        # guard instead of inventing a narrower split population here.
        by_interval = {row.sample_id: row for row in intervals[mode]}
        for request in canonical_requests(segments[mode].requests):
            interval = by_interval[request.request_id]
            require(interval.influence_end <= max(s.end_nano for s in sessions), "TEST_MATURITY_CHANGED")
            assignment = SplitAssignment(request.request_id, split.split_id, "TEST", ())
            labels[mode].append(guarded_label(request, interval, assignment, split, future[mode][request.request_id],
                sessions, stage=3, evaluation_as_of=evaluation_time(sessions)))
            counts["test_label_calls"] += 1
    label_pins, scores = {}, {}
    for mode in fitted:
        label_pins[mode] = dump_new(output / f"{mode}-labels.json.gz", [encode_record(row) for row in labels[mode]], compressed=True)
    counts["test_scoring_run_count"] = 1
    CALL_COUNTS["R001:TEST:scoring_run"] += 1
    for mode in fitted:
        scores[mode] = score_predictions(predictions[mode], labels[mode], sessions, mode, fitted[mode])
    score_pin = dump_new(output / "scores.json", scores)
    result = {"schema": "k45-r001-check-v2", "classification": "SCENARIO / NOT_ASSESSED",
        "minute_assumption": "假设分钟线在收盘后5秒可用。", "freeze": release["freeze"], "review": release["review"],
        "probabilities": prediction_pins, "labels": label_pins, "scores": score_pin, **actual_test_counts(),
        "verification_recomputations": 0, "safety": SAFETY}
    dump_new(output / "CHECK.json", result)
    return result


@dataclass(frozen=True)
class ResidentInputs:
    """Validated, already bounded inputs. No source loader or source path is retained."""
    bundles: Mapping
    sessions: tuple[SessionWindow, ...]
    rules: tuple[TradingRule, ...]
    selected: Mapping
    mode: str
    fit_cutoff: int | None
    validation: tuple[dict, ...]

    def check_fit_boundary(self):
        require(self.fit_cutoff is not None, "FIT_TRUNCATED_INPUT_REQUIRED")
        for bundle in self.bundles.values():
            require(all(bar.trading_day is not None and bar.trading_day <= FORMAL_DATES["CALIBRATION"][1]
                        and bar.end_nano < self.fit_cutoff for bar in bundle.bars), "FIT_FUTURE_BAR_REACHABLE")


def truncate_fit_bundle(bundle, calibration_end):
    """Called immediately after full shard validation, before indexes or scenarios."""
    kept = tuple(bar for bar in bundle.bars if bar.trading_day is not None
                 and bar.trading_day <= FORMAL_DATES["CALIBRATION"][1] and bar.end_nano < calibration_end)
    return replace(bundle, root=Path("K45_CAL_TRUNCATED_NO_SOURCE_INTERFACE"), bars=kept)


@contextmanager
def resident_fit_scope(inputs):
    """The actual fit/rehearsal path cannot fall back to the full-input adapter."""
    global FIT_INPUTS_CLOSED
    inputs.check_fit_boundary()
    previous = FIT_INPUTS_CLOSED
    FIT_INPUTS_CLOSED = True
    try:
        yield inputs
    finally:
        FIT_INPUTS_CLOSED = previous


def resident_inputs(mode, timing, *, for_fit, extra_contracts=()):
    require(mode in HORIZONS, "MODE_INVALID")
    _, shards = source_identity()
    bundles, checks, rules = {}, [], {}
    sessions, selected, cutoff = None, None, None
    duration = 86400 if mode == "DAILY" else 60
    for contract, source in sorted(shards.items()):
        bundle, check = timing.call(("input_validation", mode), read_original_shard,
                                   source["path"], source["manifest_sha256"])
        require({item.contract_id for item in bundle.instruments} == {contract}, "SHARD_INSTRUMENT_IDENTITY")
        if sessions is None:
            sessions = bundle.sessions
            selected, _ = select_contracts(load_json(Path(source["path"]) / "selection-evidence.json"), sessions, set(shards))
            cutoff = split_for(sessions, mode).calibration_end_nano
        require(bundle.sessions == sessions, "SHARED_CALENDAR_CONTENT_MISMATCH")
        # This validation statistic reads only declared timestamps and quality.
        transformed = sum(bar.duration_seconds == 60 and bar.quality.status == "READY"
                          and bar.quality.temporal_basis == "reconstructed" for bar in bundle.bars)
        kept = truncate_fit_bundle(bundle, cutoff) if for_fit else replace(
            bundle, root=Path("K45_TEST_RECONSTRUCTION_NO_SOURCE_INTERFACE"))
        del bundle
        chosen = {value for (day, frequency), value in selected.items() if frequency == duration
                  and partition_day(day) in (("TRAIN", "CALIBRATION") if for_fit else ("TEST",))}
        if not for_fit:
            chosen.update(extra_contracts)
        checks.append(dict(check, contract=contract, scenario_target_count=transformed,
                           retained_bars=len(kept.bars) if contract in chosen else 0))
        if contract in chosen:
            bundles[contract] = kept
            for rule in kept.rules:
                require(rules.setdefault(rule.rule_id, rule) == rule, "RULE_IDENTITY_CONFLICT")
        del kept
        print(json.dumps({"phase": "INPUT_VALIDATION", "mode": mode, "shards": len(checks)}), flush=True)
    require(sum(item["scenario_target_count"] for item in checks) == 1_783_377, "SCENARIO_TRANSFORM_COUNT_MISMATCH")
    require(len({s.trading_day for s in sessions}) == 452 and min(s.trading_day for s in sessions)
            == FORMAL_DATES["TRAIN"][0] and max(s.trading_day for s in sessions) == MATURITY_END, "FIXED_SOURCE_CALENDAR")
    # All original-file checks finish and all future fit objects are gone before
    # scenario construction, production cache creation, or any feature call.
    gc.collect()
    for contract in tuple(bundles):
        bundles[contract], _ = timing.call(("scenario", mode), scenario_view, bundles[contract])
    result = ResidentInputs(MappingProxyType(bundles), sessions, tuple(rules.values()),
                            MappingProxyType(selected), mode, cutoff if for_fit else None, tuple(checks))
    if for_fit:
        result.check_fit_boundary()
    return result


def resident_segment(requests, assessments, labels=()):
    """Keep each request's original FeatureSnapshot object and tuple production route."""
    requests = tuple(requests)
    features = {request.features.snapshot_id: request.features for request in requests}
    return Segment(tuple(features.values()), tuple(assessments[key] for key in features), requests, tuple(labels))


def resident_features(inputs, timing, *, step=5, partitions=("TRAIN", "CALIBRATION"), days=None, reuse=None):
    if inputs.fit_cutoff is not None:
        inputs.check_fit_boundary()
        require(set(partitions) <= {"TRAIN", "CALIBRATION"}, "FIT_TEST_FEATURE_FORBIDDEN")
    features, audits, reusable = [], [], {} if reuse is None else reuse
    candidates = sorted((anchor for bundle in inputs.bundles.values()
        for anchor in anchors(bundle, inputs.selected, step=step, mode=inputs.mode, partitions=partitions)
        if days is None or anchor.day in days), key=lambda a: (a.decision if a.decision is not None else a.grid_end,
                                                             a.contract, a.duration))
    for anchor in candidates:
        key = anchor.contract, anchor.duration, anchor.grid_end
        if key in reusable:
            snapshot, audit = reusable[key]
            audit = dict(audit)
        else:
            audit = {"mode": anchor.mode, "day": anchor.day.isoformat(), "partition": partition_day(anchor.day),
                     "reasons": list(anchor.reasons), "usable": False, "history_count": None,
                     "grid_end": anchor.grid_end, "contract": anchor.contract, "feature_seconds": None}
            snapshot = None
            if anchor.decision is not None:
                started = time.perf_counter()
                try:
                    candidate, visible, _ = _features(inputs.bundles[anchor.contract], anchor.contract,
                        anchor.decision, anchor.duration, None, None, inputs.sessions, canonical_daily=anchor.duration == 86400)
                except FactorsError as error:
                    if str(error) != "no_visible_bars_for_snapshot":
                        raise
                    audit["reasons"].append("NO_VISIBLE_BARS_FOR_SNAPSHOT")
                else:
                    audit["feature_seconds"] = time.perf_counter() - started
                    audit["history_count"] = len(visible)
                    require(candidate.trading_day == anchor.day and candidate.contract_id == anchor.contract,
                            "FEATURE_ANCHOR_IDENTITY_MISMATCH")
                    audit["reasons"].extend(usable_feature(candidate))
                    audit["usable"] = not audit["reasons"]
                    timing.values["resident_features", anchor.mode, str(history_bin(len(visible)))].append(audit["feature_seconds"])
                    if audit["usable"]:
                        snapshot = candidate
                    del candidate, visible
            reusable[key] = snapshot, dict(audit)
        audits.append(audit)
        if snapshot is not None:
            features.append(snapshot)
        if len(audits) % 100 == 0:
            print(json.dumps({"phase": "RESIDENT_FEATURES", "mode": inputs.mode, "candidates": len(audits),
                              "usable": len(features), **actual_test_counts()}), flush=True)
    return tuple(sorted(features, key=lambda f: (f.decision_nano, f.snapshot_id))), audits, reusable


@dataclass(frozen=True)
class ResidentChain:
    features: tuple[FeatureSnapshot, ...]
    requests: tuple[ForecastRequest, ...]
    assessments: Mapping
    reference: RegimeReference
    previous: Mapping
    reasons: tuple[dict, ...]
    reference_populations: tuple[dict, ...]


def resident_chain(features, rules, sessions, mode, timing, *, purpose="R001", reference=None,
                   previous=None, state_keys=(), reuse_requests=None):
    require(isinstance(features, tuple), "RESIDENT_FEATURE_TUPLE_REQUIRED")
    dates = FORMAL_DATES if purpose == "R001" else DRY_DATES
    ordered = tuple(sorted(features, key=lambda f: (f.decision_nano, f.snapshot_id)))
    require(all(partition_day(f.trading_day, dates) in dates for f in ordered), "REQUEST_DECISION_OUTSIDE_DESIGN")
    split = split_for(sessions, mode, purpose=purpose)
    training = tuple(f for f in ordered if partition_day(f.trading_day, dates) == "TRAIN")
    populations = []
    if reference is None:
        reference = timing.call(("resident_reference_final", mode, purpose), fit_regime_reference,
                                training, split.train_end_nano, split.train_end_nano)
        populations.append({"kind": "final", "count": len(training), "cutoff": split.train_end_nano})
    else:
        require(not training and reference.training_end_nano == split.train_end_nano
                and reference.available_at_nano == split.train_end_nano, "TEST_FROZEN_REFERENCE_MISMATCH")
    state = {key: None for key in state_keys}
    state.update({} if previous is None else previous)
    assessments, requests, reasons, by_day = {}, [], [], {}
    reused = None
    if reuse_requests is not None:
        reused = defaultdict(list)
        for request in reuse_requests:
            reused[request.features.snapshot_id].append(request)
        require(set(reused) <= {f.snapshot_id for f in ordered}, "REHEARSAL_REQUEST_OUTSIDE_TRAIN")
    for snapshot in ordered:
        partition = partition_day(snapshot.trading_day, dates)
        current = reference
        if partition == "TRAIN":
            if snapshot.trading_day not in by_day:
                prefix = tuple(f for f in training if f.trading_day < snapshot.trading_day)
                cutoff = max((f.decision_nano for f in prefix), default=snapshot.decision_nano - 1)
                by_day[snapshot.trading_day] = timing.call(("resident_reference_prefix", mode, purpose),
                                                          fit_regime_reference, prefix, cutoff, cutoff)
                populations.append({"kind": "prefix", "day": snapshot.trading_day.isoformat(),
                                    "count": len(prefix), "cutoff": cutoff})
            current = by_day[snapshot.trading_day]
        key = snapshot.contract_id, snapshot.duration_seconds
        assessment = timing.call(("resident_assessment", mode, purpose), assessment_for,
                                 snapshot, rules, current, state.get(key))
        state[key] = assessments[snapshot.snapshot_id] = assessment
        if reused is not None:
            group = reused.get(snapshot.snapshot_id, ())
            require(all(request.features is snapshot and request.frozen_range == assessment.frozen_range for request in group),
                    "REHEARSAL_REQUEST_BINDING_CHANGED")
            requests.extend(group)
            continue
        for horizon in HORIZONS[mode]:
            try:
                batch = timing.call(("resident_requests", mode, str(horizon)), build_forecast_requests,
                    snapshot, (Horizon("TRADING_DAY" if mode == "DAILY" else "TRADING_MINUTE", horizon),),
                    assessment.frozen_range, sessions)
            except ClockError:
                reasons.append({"snapshot_id": snapshot.snapshot_id, "partition": partition, "horizon": horizon,
                                "reason": "HORIZON_CALENDAR_INSUFFICIENT"})
            else:
                requests.extend(batch)
    return ResidentChain(ordered, tuple(requests), MappingProxyType(assessments), reference,
                         MappingProxyType(state), tuple(reasons), tuple(populations))


def previous_payload(previous):
    return [{"contract_id": contract, "duration_seconds": duration,
             "assessment": None if value is None else encode_record(value)}
            for (contract, duration), value in sorted(previous.items())]


def decode_previous(rows):
    require(isinstance(rows, list), "PREVIOUS_STATE_SCHEMA")
    result = {}
    for row in rows:
        require(set(row) == {"contract_id", "duration_seconds", "assessment"}, "PREVIOUS_STATE_SCHEMA")
        key = row["contract_id"], row["duration_seconds"]
        require(key not in result and isinstance(key[0], str) and type(key[1]) is int, "PREVIOUS_STATE_IDENTITY")
        result[key] = None if row["assessment"] is None else decode_record(RegimeAssessment, row["assessment"])
    return result


def id_sequence(purpose, mode, partition, step, kind, ids):
    ids = list(ids)
    require(all(isinstance(value, str) for value in ids), "ID_SEQUENCE_TYPE")
    return {"schema": "k45-id-sequence-v3", "purpose": purpose, "mode": mode, "partition": partition,
            "step": step, "kind": kind, "ids": ids}


def identity_sequences(chain, assignments, fitted_requests, mode, step, *, purpose="R001", partitions=None):
    dates = FORMAL_DATES if purpose == "R001" else DRY_DATES
    partitions = tuple(dates) if partitions is None else tuple(partitions)
    by_id = {row.sample_id: row for row in assignments}
    fit_ids = {request.request_id for request in fitted_requests}
    result = []
    for partition in partitions:
        features = tuple(f for f in chain.features if partition_day(f.trading_day, dates) == partition)
        requests = tuple(r for r in chain.requests if partition_day(r.features.trading_day, dates) == partition)
        groups = {"snapshots": (f.snapshot_id for f in features),
                  "assessments": (chain.assessments[f.snapshot_id].assessment_id for f in features),
                  "generated_requests": (r.request_id for r in requests),
                  "legal_requests": (r.request_id for r in requests if by_id[r.request_id].partition == partition)}
        if partition != "TEST":
            groups["fit_requests"] = (r.request_id for r in requests if r.request_id in fit_ids)
        result.extend(id_sequence(purpose, mode, partition, step, kind, ids) for kind, ids in groups.items())
    return tuple(result)


def seal_identity_sequences(directory, sequences, bindings):
    result = []
    for sequence in sequences:
        digest = sha256_hex(canonical_json_bytes(sequence))
        path = Path(directory) / f'{sequence["purpose"]}-{sequence["mode"]}-{sequence["partition"]}-{sequence["kind"]}.json'
        member = dump_new(path, {"sequence": sequence, "bindings": bindings})
        result.append({"partition": sequence["partition"], "kind": sequence["kind"], "count": len(sequence["ids"]),
                       "sequence_sha256": digest, "file": member})
    return result


def verify_identity_sequences(expected, actual, bindings):
    """This gate precedes every real TEST prediction; never repair or reorder IDs."""
    require(len(expected) == len(actual), "RECONSTRUCTED_ID_SEQUENCE_MISMATCH")
    for member, sequence in zip(expected, actual):
        verify_pin(member["file"])
        frozen = load_json(member["file"]["path"])
        require(frozen["bindings"] == bindings and frozen["sequence"] == sequence
                and member["sequence_sha256"] == sha256_hex(canonical_json_bytes(sequence))
                and member["count"] == len(sequence["ids"]), "RECONSTRUCTED_ID_SEQUENCE_MISMATCH")


def predict_verified(fitted, segment, expected, actual, bindings, *, purpose="R001"):
    verify_identity_sequences(expected, actual, bindings)
    return predict_rows(fitted, segment, purpose=purpose)


def request_time_payload(request):
    return {"request_id": request.request_id, "contract_id": request.features.contract_id,
            "product": request.features.product, "day": request.features.trading_day.isoformat(),
            "decision_nano": request.features.decision_nano, "window_start_nano": request.window_start_nano,
            "window_end_nano": request.window_end_nano, "horizon_unit": request.horizon.unit,
            "horizon_value": request.horizon.value}


def decode_time_request(row):
    require(set(row) == {"request_id", "contract_id", "product", "day", "decision_nano", "window_start_nano",
                         "window_end_nano", "horizon_unit", "horizon_value"}, "TIME_REQUEST_SCHEMA")
    return SimpleNamespace(request_id=row["request_id"], features=SimpleNamespace(
        contract_id=row["contract_id"], product=row["product"], trading_day=date.fromisoformat(row["day"]),
        decision_nano=row["decision_nano"]), window_start_nano=row["window_start_nano"],
        window_end_nano=row["window_end_nano"], horizon=Horizon(row["horizon_unit"], row["horizon_value"]))


def assignment_hash(assignments):
    return sha256_hex(canonical_json_bytes([{"id": a.sample_id, "partition": a.partition,
                                           "split_id": a.split_id, "reasons": list(a.reasons)} for a in assignments]))


def resident_split(inputs, chain, timing, *, purpose="R001", context_requests=()):
    split = split_for(inputs.sessions, inputs.mode, purpose=purpose)
    sessions = inputs.sessions if purpose == "R001" else tuple(
        s for s in inputs.sessions if s.trading_day <= FORMAL_DATES["TRAIN"][1])
    indexes = {contract: time_index(bundle) for contract, bundle in inputs.bundles.items()}
    intervals = tuple(timing.call(("resident_interval", inputs.mode, str(r.horizon.value)),
        indexes[r.features.contract_id].interval, r, r.window_end_nano) for r in (*context_requests, *chain.requests))
    assignments = timing.call(("resident_split", inputs.mode, purpose), assign_splits, intervals, split, sessions)
    return intervals, assignments


def resident_labels(inputs, chain, intervals, assignments, timing, *, purpose="R001", partitions=("TRAIN", "CALIBRATION")):
    split = split_for(inputs.sessions, inputs.mode, purpose=purpose)
    by_interval = {row.sample_id: row for row in intervals}
    by_assignment = {row.sample_id: row for row in assignments}
    indexes = {contract: _WindowIndex(bundle) for contract, bundle in inputs.bundles.items()}
    labels = []
    for request in chain.requests:
        assigned = by_assignment[request.request_id]
        if assigned.partition not in partitions:
            continue
        window = indexes[request.features.contract_id].window(request)
        if purpose == "DRY_RUN":
            cutoff = evaluation_time(inputs.sessions, purpose)
            window = tuple(bar for bar in window if bar.trading_day is not None
                           and bar.trading_day <= FORMAL_DATES["TRAIN"][1] and bar.end_nano < cutoff)
        labels.append(timing.call(("resident_label", inputs.mode, str(request.horizon.value), purpose), guarded_label,
            request, by_interval[request.request_id], assigned, split, window, inputs.sessions,
            purpose=purpose, stage=3 if assigned.partition == "TEST" else 1,
            evaluation_as_of=evaluation_time(inputs.sessions, purpose) if assigned.partition == "TEST" else None))
    return tuple(labels)


def resident_segments(chain, assignments, labels):
    by_assignment = {row.sample_id: row for row in assignments}
    by_label = {row.request_id: row for row in labels}
    segments = {}
    for partition in FORMAL_DATES:
        group = tuple(r for r in chain.requests if by_assignment[r.request_id].partition == partition
                      and (partition == "TEST" or by_label[r.request_id].status == "MATURE"))
        segments[partition] = resident_segment(group, chain.assessments,
                                               () if partition == "TEST" else (by_label[r.request_id] for r in group))
    return segments


def resident_config(sessions, mode, *, purpose="R001"):
    split = split_for(sessions, mode, purpose=purpose)
    keys = tuple(spec.factor_key for spec in factor_registry_v2(86400 if mode == "DAILY" else 60))
    return models.default_fit_config(keys, split.train_end_nano, split.calibration_end_nano,
                                    evaluation_time(sessions, purpose) + 1)


def resident_inner(segments, config, reference):
    if not segments["TRAIN"].requests and not segments["CALIBRATION"].requests:
        return {"sufficient": False, "fit_days": 0, "development_days": 0, "reason": "NO_LEGAL_INPUTS"}, (), ()
    checked, train, cal = joined_samples(segments["TRAIN"], segments["CALIBRATION"], config, reference)
    _, _, inner = inner_partition(train, checked)
    return inner, train, cal


def resident_rehearsal(inputs, formal_chain, timing, directory, bindings, step):
    features = tuple(f for f in formal_chain.features if partition_day(f.trading_day) == "TRAIN")
    requests = tuple(r for r in formal_chain.requests if partition_day(r.features.trading_day) == "TRAIN")
    chain = resident_chain(features, inputs.rules, inputs.sessions, inputs.mode, timing,
                           purpose="DRY_RUN", reuse_requests=requests)
    require(all(f is original for f, original in zip(chain.features, features)) and
            all(r is original for r, original in zip(chain.requests, requests)), "REHEARSAL_OBJECT_REUSE_FAILED")
    intervals, assignments = resident_split(inputs, chain, timing, purpose="DRY_RUN")
    labels = resident_labels(inputs, chain, intervals, assignments, timing, purpose="DRY_RUN")
    segments = resident_segments(chain, assignments, labels)
    config = resident_config(inputs.sessions, inputs.mode, purpose="DRY_RUN")
    inner, _, _ = resident_inner(segments, config, chain.reference)
    ids = seal_identity_sequences(Path(directory) / "ids", identity_sequences(chain, assignments,
        (*segments["TRAIN"].requests, *segments["CALIBRATION"].requests), inputs.mode, step, purpose="DRY_RUN"), bindings)
    result = {"schema": "k45-rehearsal-check-v3", "purpose": "DRY_RUN", "mode": inputs.mode,
              "status": "DRY_RUN_NOT_RUN:INNER_INSUFFICIENT", "inner": inner, "identities": ids,
              "objects_reused": True, "reference": reference_payload(chain.reference),
              "reference_populations": list(chain.reference_populations), "test_probability_calls": 0,
              "test_label_calls": 0, "scoring_runs": 0, "research_conclusions_published": False}
    if inner["sufficient"]:
        fitted = timing.call(("resident_fit", inputs.mode, "DRY_RUN"), fit_direction,
                              segments["TRAIN"], segments["CALIBRATION"], config, chain.reference)
        model_pin = dump_new(Path(directory) / "models.json.gz", {"fit": fitted.payload()}, compressed=True)
        predictions = timing.call(("resident_prediction", inputs.mode, "DRY_RUN"), predict_rows,
                                   fitted, segments["TEST"], purpose="DRY_RUN")
        probability_pin = dump_new(Path(directory) / "sealed-diagnostics/probabilities.json.gz", predictions, compressed=True)
        canonical = canonical_requests(segments["TEST"].requests)
        test_chain = replace(chain, requests=canonical)
        test_labels = resident_labels(inputs, test_chain, intervals, assignments, timing,
                                      purpose="DRY_RUN", partitions=("TEST",))
        label_pin = dump_new(Path(directory) / "sealed-diagnostics/labels.json.gz",
                             [encode_record(row) for row in test_labels], compressed=True)
        scores = timing.call(("resident_statistics", inputs.mode, "DRY_RUN"), score_predictions,
                              predictions, test_labels, inputs.sessions, inputs.mode, fitted, purpose="DRY_RUN")
        require(all(cell["status"] == "INSUFFICIENT" for cell in scores["cells"]), "DRY_RUN_MUST_NOT_GAIN_60_DAY_QUALIFICATION")
        score_pin = dump_new(Path(directory) / "sealed-diagnostics/statistics.json.gz", scores, compressed=True)
        result.update(status="DRY_RUN_CHAIN_COMPLETED", artifacts=[model_pin, probability_pin, label_pin, score_pin],
                      test_probability_calls=3 * len(predictions), test_label_calls=len(test_labels), scoring_runs=1,
                      calendar_days=scores["calendar_days"])
    result["actual_test_calls"] = actual_test_counts()
    dump_new(Path(directory) / "rehearsal-check.json", result)
    return result


def candidate_metadata(mode, output):
    """Separate process: inspect time/quality fields, never decode a bar price."""
    started = time.perf_counter()
    _, shards = source_identity()
    first = Path(next(iter(shards.values()))["path"])
    sessions = original_record_envelope((first / "sessions.json").read_bytes(), SessionWindow, SESSIONS_SCHEMA_ID)
    selected, _ = select_contracts(load_json(first / "selection-evidence.json"), sessions, set(shards))
    duration = 86400 if mode == "DAILY" else 60
    chosen = {contract for (day, frequency), contract in selected.items()
              if frequency == duration and partition_day(day) in FORMAL_DATES}
    grouped, input_rows = {}, 0
    for contract, source in sorted(shards.items()):
        path = Path(source["path"])
        manifest = load_json(path / "manifest.json")
        verify_pin({"path": str(path / "bars.csv"), "sha256": manifest["files"]["bars.csv"]})
        if contract not in chosen:
            continue
        rows = []
        with (path / "bars.csv").open(encoding="utf-8", newline="") as stream:
            for raw in csv.DictReader(stream):
                input_rows += 1
                frequency = int(raw["duration_seconds"])
                if frequency != duration:
                    continue
                day = date.fromisoformat(raw["trading_day"]) if raw["trading_day"] else None
                require((day is None or day < HOLDOUT) and
                        datetime.fromtimestamp(int(raw["datetime_nano"]) // NANO, TZ).date() < HOLDOUT, "STOP_HOLDOUT_BOUNDARY")
                quality = json.loads(raw["quality"])
                end = int(raw["end_nano"])
                available = int(raw["available_at_nano"]) if raw["available_at_nano"] else None
                if frequency == 60 and quality["status"] == "READY" and quality["temporal_basis"] == "reconstructed":
                    require(available == end + 60 * NANO, "SOURCE_MINUTE_AVAILABILITY_NOT_60_SECONDS")
                    available = end + SCENARIO_SECONDS * NANO
                require(raw["complete"] in ("true", "false"), "METADATA_COMPLETE_ENCODING")
                rows.append(SimpleNamespace(contract_id=contract, product=raw["product"], duration_seconds=frequency,
                    start_nano=int(raw["start_nano"]), end_nano=end, available_at_nano=available,
                    trading_day=day, session_id=raw["session_id"] or None, complete=raw["complete"] == "true",
                    quality=SimpleNamespace(status=quality["status"], temporal_basis=quality["temporal_basis"])))
        scoped = sorted((s for s in sessions if s.product in {r.product for r in rows}), key=lambda s: s.start_nano)
        starts = [s.start_nano for s in scoped]
        def eligible(row):
            if not row.complete or row.available_at_nano is None or row.quality.status != "READY" or row.quality.temporal_basis == "unknown":
                return False
            slot = bisect_right(starts, row.start_nano) - 1
            return duration != 60 or slot < 0 or row.start_nano >= scoped[slot].end_nano or (row.start_nano - starts[slot]) % MINUTE == 0
        cutoffs = sorted(max(row.end_nano, row.available_at_nano) for row in rows if eligible(row))
        projection = SimpleNamespace(bars=rows, sessions=sessions, instruments=(SimpleNamespace(contract_id=contract),))
        for step in ((5, 15) if mode == "MINUTE" else (5,)):
            for anchor in anchors(projection, selected, step=step, mode=mode):
                count = bisect_right(cutoffs, anchor.decision) if anchor.decision is not None else 0
                bucket = history_bin(count)
                key = step, anchor.day.isoformat(), bucket
                row = grouped.setdefault(key, {"step": step, "day": anchor.day.isoformat(), "bucket": bucket,
                    "partition": partition_day(anchor.day), "candidates": 0, "callable_candidates": 0,
                    "history_total": 0, "history_min": None, "history_max": None})
                row["candidates"] += 1
                if anchor.decision is not None:
                    row["callable_candidates"] += 1
                    row["history_total"] += count
                    row["history_min"] = count if row["history_min"] is None else min(count, row["history_min"])
                    row["history_max"] = count if row["history_max"] is None else max(count, row["history_max"])
        del rows, projection, cutoffs
    result = {"schema": "k45-candidate-metadata-v3", "mode": mode, "source_collection_sha256": SOURCE_COLLECTION_SHA,
              "sessions_sha256": SESSIONS_SHA, "selection_sha256": SELECTION_SHA,
              "price_fields_decoded": False, "input_rows_inspected": input_rows,
              "rows": [grouped[key] for key in sorted(grouped)], "elapsed_seconds": time.perf_counter() - started}
    dump_new(output, result)
    return {"status": "METADATA_COUNTS_FROZEN", "mode": mode, "artifact": pin(output)}


def tree_memory():
    import psutil
    root = psutil.Process()
    rss = private = 0
    identities = []
    for process in (root, *root.children(recursive=True)):
        try:
            memory = process.memory_info()
            rss += memory.rss
            private += getattr(memory, "private", memory.rss)
            identities.append({"pid": process.pid, "created": process.create_time()})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return {"rss_bytes": rss, "private_bytes": private, "processes": identities}


def measurement_days(sessions):
    days = sorted({s.trading_day for s in sessions if FORMAL_DATES["TRAIN"][0] <= s.trading_day <= FORMAL_DATES["TRAIN"][1]})
    require(len(days) >= 20, "MEASUREMENT_CALENDAR_TOO_SHORT")
    return tuple(days[:10] + days[-10:])


def measured_twenty_days(inputs, timing, *, step=5):
    days = measurement_days(inputs.sessions)
    started = time.perf_counter()
    before = tree_memory()
    features, audits, reuse = resident_features(inputs, timing, step=step, partitions=("TRAIN",), days=set(days))
    chain = resident_chain(features, inputs.rules, inputs.sessions, inputs.mode, timing)
    intervals, assignments = resident_split(inputs, chain, timing)
    labels = resident_labels(inputs, chain, intervals, assignments, timing)
    segments = resident_segments(chain, assignments, labels)
    config = resident_config(inputs.sessions, inputs.mode)
    _, train, cal = resident_inner(segments, config, chain.reference)
    after = tree_memory()
    groups = []
    for day in days:
        for bucket in range(len(HISTORY_BINS)):
            rows = [r for r in audits if r["day"] == day.isoformat()
                    and history_bin(r["history_count"] or 0) == bucket]
            groups.append({"day": day.isoformat(), "bucket": bucket, "candidates": len(rows),
                           "usable": sum(r["usable"] for r in rows),
                           "feature_seconds": math.fsum(r["feature_seconds"] for r in rows if r["feature_seconds"] is not None)})
    payload = {"schema": "k45-real-twenty-day-measurement-v3", "mode": inputs.mode, "step": step,
        "days": [day.isoformat() for day in days], "no_selection_days": [day.isoformat() for day in days
            if (day, 86400 if inputs.mode == "DAILY" else 60) not in inputs.selected],
        "cost_only": True, "research_predictions": 0, "elapsed_seconds": time.perf_counter() - started,
        "reference_population_scope": "MEASURED_DAYS_ONLY; NOT_COMPLETE_FORMAL_PREFIX",
        "reference_populations": list(chain.reference_populations), "timings": timing.report(), "buckets_by_day": groups,
        "memory_before": before, "memory_after": after,
        "features": len(features), "requests": len(chain.requests), "labels": len(labels),
        "source_tuple_bytes": sum(sys.getsizeof(f.source_bar_ids) for f in features),
        "feature_object_bytes_without_sources": sum(resident_object_bytes(replace(f, source_bar_ids=())) for f in features),
        "request_shallow_bytes": sum(sys.getsizeof(r) + sys.getsizeof(r.__dict__) for r in chain.requests),
        "label_object_bytes": sum(resident_object_bytes(label) for label in labels),
        "funnel": funnel(inputs.mode, audits, chain.requests, assignments, labels, train, cal, chain.reasons),
        "actual_test_calls": actual_test_counts()}
    return payload, reuse, audits


def resident_object_bytes(value, seen=None):
    seen = set() if seen is None else seen
    if id(value) in seen:
        return 0
    seen.add(id(value))
    size = sys.getsizeof(value)
    if is_dataclass(value):
        size += sys.getsizeof(getattr(value, "__dict__", {}))
        size += sum(resident_object_bytes(getattr(value, item.name), seen) for item in fields(value))
    elif isinstance(value, Mapping):
        size += sum(resident_object_bytes(k, seen) + resident_object_bytes(v, seen) for k, v in value.items())
    elif isinstance(value, (list, tuple, set, frozenset)):
        size += sum(resident_object_bytes(item, seen) for item in value)
    return size


def validate_resource_record(path, mode):
    resource = load_json(path)
    require(resource.get("schema") == "k45-mode-resource-v3" and resource.get("status") == "READY"
            and resource.get("mode") == mode and resource.get("tool_sha256") == pin(__file__)["sha256"], "MODE_RESOURCE_RECORD_REQUIRED")
    limit = min(24 * 1024 ** 3, resource["available_physical_bytes"] * 3 // 4,
                resource["available_commit_bytes"] * 3 // 4)
    require(type(resource.get("process_tree_limit_bytes")) is int and resource["process_tree_limit_bytes"] == limit
            and limit > 0 and resource["wall_seconds"] == 259200, "MODE_RESOURCE_LIMIT_MISMATCH")
    return resource


def measure_mode(mode, resource_path, output):
    """Real required 20-day chain; no estimated-cost gate can run before it."""
    resource = validate_resource_record(resource_path, mode)
    output = Path(output)
    require(not output.exists(), "RUN_OUTPUT_ALREADY_EXISTS")
    output.mkdir(parents=True)
    sources = code_pins()
    timing = Timings()
    inputs = resident_inputs(mode, timing, for_fit=True)
    with resident_fit_scope(inputs):
        result, _, _ = measured_twenty_days(inputs, timing)
    result.update(resource=pin(resource_path), code_pins=sources, source_collection_sha256=SOURCE_COLLECTION_SHA,
                  input_validation=list(inputs.validation), status="TWENTY_DAY_MEASUREMENT_COMPLETED",
                  process_tree_limit_bytes=resource["process_tree_limit_bytes"], safety=SAFETY)
    dump_new(output / "measurement.json", result)
    return {"status": result["status"], "mode": mode, "artifact": pin(output / "measurement.json")}


def public_error(error):
    """No exception message or object repr from data-bearing production code."""
    import traceback
    return {"status": "FAILED", "error_type": type(error).__name__,
            "code": str(error) if isinstance(error, ResearchError) else "PRODUCTION_OR_IO_ERROR",
            "locations": [{"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
                          for frame in traceback.extract_tb(error.__traceback__)], "actual_test_calls": actual_test_counts()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    measured = commands.add_parser("measure-mode")
    measured.add_argument("--mode", choices=tuple(HORIZONS), required=True)
    measured.add_argument("--resource", required=True)
    measured.add_argument("--output", required=True)
    metadata = commands.add_parser("candidate-metadata")
    metadata.add_argument("--mode", choices=tuple(HORIZONS), required=True)
    metadata.add_argument("--output", required=True)
    stage1 = commands.add_parser("stage1")
    stage1.add_argument("--output", required=True)
    stage1.add_argument("--capacity", required=True)
    costs = commands.add_parser("synthetic-costs")
    costs.add_argument("--output", required=True)
    budget = commands.add_parser("budget")
    budget.add_argument("--stage1", required=True)
    budget.add_argument("--costs", required=True)
    budget.add_argument("--output", required=True)
    fit = commands.add_parser("fit")
    fit.add_argument("--ticket", required=True)
    fit.add_argument("--output", required=True)
    stage2 = commands.add_parser("stage2")
    stage2.add_argument("--stage1", required=True)
    stage2.add_argument("--budget", required=True)
    stage2.add_argument("--output", required=True)
    stage3 = commands.add_parser("stage3")
    stage3.add_argument("--release", required=True)
    stage3.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    CALL_COUNTS.clear()
    try:
        output = Path(args.output).resolve()
        require(output.is_relative_to(Path("C:/AKR81114/k45/runs").resolve()) or
                output.is_relative_to(Path("C:/AKR81114/k45/owner").resolve()), "OUTPUT_OUTSIDE_AUTHORIZED_SCOPE")
        if args.command == "measure-mode": result = measure_mode(args.mode, args.resource, output)
        elif args.command == "candidate-metadata": result = candidate_metadata(args.mode, output)
        else:
            raise ResearchError("LEGACY_RECORD_STREAM_ROUTE_RETIRED")
        print(json.dumps({"status": result.get("status", "COMPLETED"), "stage": args.command,
                          "output": str(output), "safety": SAFETY}, ensure_ascii=False), flush=True)
        return 0 if not result.get("status", "").startswith("STOP") else 3
    except Exception as error:
        print(json.dumps(public_error(error), ensure_ascii=False), flush=True)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
