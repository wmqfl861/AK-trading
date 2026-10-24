"""Type/schema registry and strict JSON/CSV codecs for the v2.1 contract.

The dataclass table in ``ak_trading.v2.contracts`` is the authoritative
schema (devplan-v2.1.0 M1 §7). This module registers every record type and
string enumeration, provides strict JSON/CSV encoding and decoding, and
computes the versioned calibration geometry key. JSON output is UTF-8
without BOM, key-sorted, compact, terminated by exactly one LF; duplicate
keys and non-finite numbers are rejected on input.
"""
from __future__ import annotations

import collections.abc as _abc
import csv
import io
import json
import re
from dataclasses import dataclass, fields, is_dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType, NoneType, UnionType
from typing import Any, Literal, Mapping, Sequence, Union, get_args, get_origin, get_type_hints
from typing import TypeAliasType

from ak_trading.v2 import CONTRACT_VERSION, SHARED_FIXTURE_SCHEMA_VERSION
from ak_trading.v2.contracts import (
    JsonValue,
    PERSISTENCE_EVENTS,
    AccountSnapshot,
    CalibrationEvidence,
    ConfidencePolicy,
    ContinuationEvidence,
    DataQuality,
    DecisionRecord,
    ExecutionResult,
    ExitConfirmation,
    ExitContext,
    ExitExecutionLink,
    ExitFeatureSpec,
    ExitFeatureSnapshot,
    ExitFeatureValue,
    ExitLadderStep,
    ExitPolicyBinding,
    ExitProtectionRecord,
    ExitProposal,
    ExitSpec,
    ExitState,
    FactorSpec,
    FactorValue,
    FeatureSnapshot,
    FeeLeg,
    Fill,
    ForecastRequest,
    FrozenInput,
    FrozenRange,
    GateDecision,
    HoldingPolicy,
    Horizon,
    Instrument,
    LayerSpec,
    LedgerCheckpoint,
    MacroContext,
    MacroItem,
    MarketBar,
    MarketForecast,
    OrderIntent,
    PathLabel,
    PayoffEstimate,
    PayoffPathCheckpoint,
    PayoffPathDecision,
    PayoffPathOrder,
    PayoffPathResult,
    PayoffReceiptCommit,
    PayoffTrainingPlan,
    PositionLot,
    PositionSnapshot,
    RegimeAssessment,
    RoundBudget,
    RunPolicy,
    SessionWindow,
    StressPolicy,
    StrategyContext,
    StrategyProposal,
    StrategyQualification,
    StrategySetup,
    StrategySpec,
    StrategyState,
    TimeProbability,
    TradingRule,
    ValuationMark,
)

BARS_SCHEMA_ID = "ak_probability_bars_v2.1.0"
INSTRUMENTS_SCHEMA_ID = "ak_probability_instruments_v2.1.0"
SESSIONS_SCHEMA_ID = "ak_probability_sessions_v2.1.0"
MACRO_ITEMS_SCHEMA_ID = "ak_probability_macro_items_v2.1.0"
RULES_SCHEMA_ID = "ak_probability_rules_v2.1.0"
AVAILABILITY_SCHEMA_ID = "ak_probability_availability_v2.1.0"
MANIFEST_SCHEMA_ID = "ak_probability_input_v2.1.0"
SHARED_FIXTURE_SCHEMA_ID = SHARED_FIXTURE_SCHEMA_VERSION
CONTRACT_LOCK_SCHEMA_ID = "ak_probability_contract_lock_v2.1.0"
CHECKPOINT_SCHEMA_ID = "ak_probability_checkpoints_v2.1.3"
SHADOW_CHECKPOINT_SCHEMA_ID = "ak_probability_shadow_checkpoints_v2.1.3"
METRICS_SCHEMA_ID = "ak_probability_metrics_v2.1.4"
SHADOW_EVENT_SCHEMA_ID = "ak_probability_shadow_event_v2.1.3"
MODEL_ACTIVATION_REQUEST_SCHEMA_ID = "ak_probability_model_activation_request_v2.1.3"
MODEL_ACTIVATIONS_SCHEMA_ID = "ak_probability_model_activations_v2.1.3"
SEASONALITY_SCHEMA_ID = "ak_probability_seasonality_v2.1.1"
INPUT_COLLECTION_SCHEMA_ID = "ak_probability_input_collection_v2.1.0"
FACTOR_REGISTRY_SCHEMA_ID = "ak_probability_factor_registry_v2.1.1"
# 08-M3C §12 / 007 §3: the ten v1.0.0 exit product schemas (structure
# unchanged from the target table; envelope follows M1 §7).
EXIT_SPECS_SCHEMA_ID = "ak_probability_exit_specs_v1.0.0"
EXIT_FEATURE_SPECS_SCHEMA_ID = "ak_probability_exit_feature_specs_v1.0.0"
EXIT_BINDINGS_SCHEMA_ID = "ak_probability_exit_bindings_v1.0.0"
EXIT_FEATURES_SCHEMA_ID = "ak_probability_exit_features_v1.0.0"
EXIT_STATES_SCHEMA_ID = "ak_probability_exit_states_v1.0.0"
EXIT_PROPOSALS_SCHEMA_ID = "ak_probability_exit_proposals_v1.0.0"
EXIT_EXECUTION_LINKS_SCHEMA_ID = "ak_probability_exit_execution_links_v1.0.0"
EXIT_AUDIT_SCHEMA_ID = "ak_probability_exit_audit_v1.0.0"
EXIT_LOGICAL_REPLAY_SCHEMA_ID = "ak_probability_exit_logical_replay_v1.0.0"
EXIT_RUN_SCHEMA_ID = "ak_probability_exit_run_v1.0.0"
# E-P1 (v2-ep1-training-path-20260917-r1 RULING 2.2/2.3): the offline
# fixed-candidate path-measurement source schemas. Fresh v1 identities, no
# version collision with the experiment/research_config lineage
# (v2.1.0 current, v2.1.1 allocated to 007 exit registration, v2.1.2
# allocated to the C1 condition protocol in contracts-2.1.4).
PAYOFF_TRAINING_PLAN_SCHEMA_ID = "ak_probability_payoff_training_plan_v1"
PAYOFF_PATH_SCHEMA_ID = "ak_probability_payoff_path_v1"

RECORD_TYPES: dict[str, type] = {
    record_type.__name__: record_type
    for record_type in (
        Horizon, DataQuality, Instrument, SessionWindow, MarketBar, MacroItem, MacroContext,
        FeeLeg, TradingRule, FrozenInput, FactorSpec, FactorValue, FeatureSnapshot, FrozenRange,
        ForecastRequest, PathLabel, CalibrationEvidence, TimeProbability, MarketForecast,
        RegimeAssessment, HoldingPolicy, ConfidencePolicy,
        StressPolicy, RunPolicy, StrategyQualification, StrategySpec, LayerSpec, RoundBudget,
        PositionLot, PositionSnapshot, AccountSnapshot, StrategySetup, StrategyState,
        StrategyContext, PayoffEstimate, StrategyProposal, GateDecision, DecisionRecord,
        OrderIntent, Fill, ExecutionResult, ValuationMark,
        ContinuationEvidence, ExitSpec, ExitFeatureSpec, ExitFeatureValue,
        ExitFeatureSnapshot, ExitPolicyBinding, ExitConfirmation, ExitProtectionRecord,
        ExitLadderStep, ExitState, ExitProposal, ExitContext, ExitExecutionLink,
        LedgerCheckpoint,
    )
}

# E-P1: the offline fixed-candidate path-measurement family keeps its OWN
# registry, deliberately separate from the normal trading RECORD_TYPES. The
# codec helpers below accept the union, but the normal file/schema ids and
# this measurement domain never accept each other's envelopes (mutual
# rejection); normal decoders still reject path records on exact field sets
# (path_id / path_decision_id) and vice versa.
PAYOFF_PATH_RECORD_TYPES: dict[str, type] = {
    record_type.__name__: record_type
    for record_type in (
        PayoffTrainingPlan, PayoffPathDecision, PayoffPathOrder, PayoffPathCheckpoint,
        PayoffReceiptCommit, PayoffPathResult,
    )
}

_ALL_RECORD_TYPES: dict[str, type] = {**RECORD_TYPES, **PAYOFF_PATH_RECORD_TYPES}

ENUM_REGISTRY: dict[str, frozenset[str]] = {
    "HorizonUnit": frozenset({"TRADING_MINUTE", "TRADING_DAY"}),
    "QualityStatus": frozenset({"READY", "WARMUP", "MISSING", "INVALID", "STALE", "DEGRADED"}),
    "BaseRegime": frozenset({"UP_TREND", "DOWN_TREND", "ORDERED_RANGE", "WIDE_RANGE", "COMPRESSION", "UNKNOWN"}),
    "RegimeFlag": frozenset({"PULLBACK", "BREAKOUT", "EVENT_SHOCK", "ILLIQUID"}),
    "StrategyFamily": frozenset({"TREND", "BREAKOUT", "PULLBACK", "REVERSION", "GRID", "MARTINGALE", "WAIT"}),
    "StrategyPhase": frozenset({"OBSERVE", "WAIT_TRIGGER", "HOLDING", "EXITING", "PAUSED"}),
    "ProposalAction": frozenset({"WAIT", "OPEN", "ADD", "HOLD", "REDUCE", "EXIT"}),
    "ExitFamily": frozenset({"LEGACY", "PROFIT_LADDER", "TRAILING_STOP", "VOLATILITY_STOP",
                             "TIME_DECAY", "SIGNAL_REVERSAL", "DRAWDOWN_EXIT",
                             "OPPORTUNITY_COST", "PARTIAL_LIQUIDATION"}),
    "ExitAction": frozenset({"KEEP", "REDUCE", "EXIT"}),
    "ExitPhase": frozenset({"MANAGING", "REDUCING", "LIQUIDATING", "CLOSED"}),
    "ExitMode": frozenset({"MINUTE", "DAILY"}),
    "ExitExecutionKind": frozenset({"NEXT_WINDOW_CLOSE", "LEGACY_PROTECTIVE"}),
    "ExitTiming": frozenset({"NEXT_LEGAL_WINDOW", "LEGACY_PROTECTIVE"}),
    "ExitWindowKind": frozenset({"CURRENT_STATE", "EPISODE_PREFIX",
                                 "EPISODE_PREFIX_CALENDAR", "ACCOUNT_PREFIX",
                                 "LIQUIDATION_PREFIX"}),
    "ExitSourceScope": frozenset({"EPISODE", "ACCOUNT"}),
    "ExitClockUnit": frozenset({"NONE", "TRADING_MINUTE", "TRADING_DAY"}),
    "PayoffPathAction": frozenset({"MEASURE_ENTRY", "MANAGE_EXIT", "CANCEL_ENTRY"}),
    "PayoffPathStatus": frozenset({"NO_CANDIDATE", "COMPLETE", "INCOMPLETE"}),
    "PayoffPathSourceKind": frozenset({"P1_ACTION_CONDITIONAL_REPLAY"}),
}

_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_GEOMETRY_VERSION = "geometry-v2.1.1"
META_TICK_FACTOR_KEY = "META:price_tick:v2.1.0"
RANGE_FREE_EVENTS = frozenset({
    "UP_TREND", "DOWN_TREND", "COMPRESSION", "DIRECTION", "PATH", "UP_TOUCH", "DOWN_TOUCH",
})


class SchemaError(ValueError):
    """Fixed-reason schema/codec violation."""


_RESOLVED_HINTS: dict[type, dict[str, Any]] = {}


def _field_types(record_type: type) -> dict[str, Any]:
    """Resolve dataclass field annotations (contracts uses future annotations)."""
    if record_type not in _RESOLVED_HINTS:
        try:
            _RESOLVED_HINTS[record_type] = get_type_hints(record_type)
        except Exception as exc:  # pragma: no cover - malformed registry type
            raise SchemaError(f"type_hints_resolution_failed:{record_type.__name__}") from exc
    return _RESOLVED_HINTS[record_type]


_BUCKET_PATTERN = re.compile(r"([\[(])(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)([\])])\Z")


def probability_in_bucket(probability: float, bucket: str) -> bool:
    """Left-closed/right-open binary bucket membership for evidence checks.

    Buckets are written like ``[0.85,0.90)`` or ``[0.05,0.10]``; the special
    multi-class bucket is exactly ``ALL``. This freezes the admission rule
    that 0.85 only enters ``[0.85,0.90)`` and the 0.10 stop-touch bound only
    enters ``[0.05,0.10]`` (devplan-v2.1.0 M1 §4.1).
    """
    if isinstance(probability, bool) or not isinstance(probability, (int, float)):
        raise TypeError("probability_must_be_number")
    if not isinstance(bucket, str):
        raise TypeError("bucket_must_be_str")
    if bucket == "ALL":
        return True
    match = _BUCKET_PATTERN.fullmatch(bucket)
    if match is None:
        raise SchemaError(f"invalid_bucket_format:{bucket}")
    low_open, low_text, high_text, high_closed = match.groups()
    low, high = float(low_text), float(high_text)
    if low > high:
        raise SchemaError(f"invalid_bucket_range:{bucket}")
    value = float(probability)
    if low_open == "(" and value <= low:
        return False
    if value < low:
        return False
    if high_closed == "]" :
        return value <= high
    return value < high


def _unwrap(typ: Any) -> Any:
    while isinstance(typ, TypeAliasType):
        typ = typ.__value__
    return typ


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SchemaError("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(value: str) -> float:
    raise SchemaError(f"non_finite_number:{value}")


def parse_json_bytes(payload: bytes) -> Any:
    """Strict UTF-8 (no BOM) JSON parse rejecting duplicate keys and non-finite numbers."""
    if not isinstance(payload, bytes):
        raise TypeError("payload_must_be_bytes")
    if payload.startswith(b"\xef\xbb\xbf"):
        raise SchemaError("bom_rejected")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise SchemaError("invalid_utf8") from None
    try:
        return json.loads(text, object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant)
    except SchemaError:
        raise
    except (ValueError, RecursionError):
        raise SchemaError("invalid_json") from None


def canonical_json_bytes(payload: JsonValue) -> bytes:
    """Canonical bytes: sorted keys, compact separators, UTF-8, one trailing LF."""
    try:
        text = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
        )
    except (TypeError, ValueError):
        raise SchemaError("canonical_json_encode_failed") from None
    return (text + "\n").encode("utf-8")


def _jsonify(value: Any) -> Any:
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise SchemaError("non_finite_float")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise SchemaError("non_finite_decimal")
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonify(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonify(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return encode_record(value)
    raise SchemaError(f"unencodable_value_type:{type(value).__name__}")


def encode_record(record: object) -> dict[str, JsonValue]:
    """Encode a registered record to its exact-field JSON object form."""
    if not is_dataclass(record) or isinstance(record, type):
        raise SchemaError("record_must_be_dataclass_instance")
    record_type = type(record)
    if record_type.__name__ not in _ALL_RECORD_TYPES:
        raise SchemaError(f"record_type_not_registered:{record_type.__name__}")
    result: dict[str, Any] = {}
    for field in fields(record_type):
        result[field.name] = _jsonify(getattr(record, field.name))
    return result


def _convert(value: Any, typ: Any, name: str) -> Any:
    typ = _unwrap(typ)
    if typ is NoneType:
        if value is None:
            return None
        raise SchemaError(f"{name}_must_be_null")
    if typ is bool:
        if isinstance(value, bool):
            return value
        raise SchemaError(f"{name}_must_be_bool")
    if typ is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise SchemaError(f"{name}_must_be_int_not_bool")
        return value
    if typ is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SchemaError(f"{name}_must_be_number")
        number = float(value)
        if number != number or number in (float("inf"), float("-inf")):
            raise SchemaError(f"{name}_must_be_finite")
        return number
    if typ is str:
        if not isinstance(value, str):
            raise SchemaError(f"{name}_must_be_str")
        return value
    if typ is Decimal:
        if isinstance(value, Decimal):
            if not value.is_finite():
                raise SchemaError(f"{name}_must_be_finite")
            return value
        # F17: Decimal-annotated JSON fields accept ONLY decimal strings;
        # JSON numbers (int/float) are rejected at the product type boundary.
        # The float|int union path (e.g. FactorValue.value) never reaches here.
        if isinstance(value, bool) or not isinstance(value, str):
            raise SchemaError(f"{name}_must_be_decimal_string")
        try:
            number = Decimal(value)
        except (InvalidOperation, ValueError):
            raise SchemaError(f"{name}_invalid_decimal") from None
        if not number.is_finite():
            raise SchemaError(f"{name}_must_be_finite")
        return number
    if typ is date:
        if isinstance(value, date):
            return value
        if not isinstance(value, str) or not _DATE_PATTERN.fullmatch(value):
            raise SchemaError(f"{name}_must_be_YYYY-MM-DD")
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise SchemaError(f"{name}_invalid_date") from None
    if typ is Path:
        if isinstance(value, Path):
            return value
        if not isinstance(value, str):
            raise SchemaError(f"{name}_must_be_str_path")
        return Path(value)
    origin = get_origin(typ)
    if origin is Literal:
        options = get_args(typ)
        for option in options:
            if isinstance(option, bool):
                if isinstance(value, bool) and value is option:
                    return value
            elif isinstance(option, int):
                if isinstance(value, bool):
                    continue
                if isinstance(value, int) and value == option:
                    return value
            elif isinstance(value, str) and value == option:
                return value
        raise SchemaError(f"{name}_not_in_literal")
    if isinstance(typ, UnionType) or get_origin(typ) is Union:
        arguments = get_args(typ)
        if value is None:
            if NoneType in arguments:
                return None
            raise SchemaError(f"{name}_must_not_be_null")
        # JSON numbers decode to float when the union offers float: the
        # documented Decimal-string / float-number discriminator (e.g.
        # FactorValue.value) must survive integer JSON literals.
        numeric_json = isinstance(value, (int, float)) and not isinstance(value, bool)
        has_float = any(_unwrap(argument) is float for argument in arguments)
        if numeric_json and has_float:
            ordered = [argument for argument in arguments
                       if _unwrap(argument) is not NoneType
                       and _unwrap(argument) is not Decimal]
            ordered += [argument for argument in arguments
                        if _unwrap(argument) is Decimal]
        else:
            ordered = [argument for argument in arguments
                       if _unwrap(argument) is not NoneType]
        for argument in ordered:
            try:
                return _convert(value, argument, name)
            except SchemaError:
                continue
        raise SchemaError(f"{name}_matches_no_union_branch")
    if origin is tuple:
        if not isinstance(value, (list, tuple)):
            raise SchemaError(f"{name}_must_be_array")
        arguments = get_args(typ)
        if len(arguments) != 2 or arguments[1] is not Ellipsis:
            raise SchemaError(f"{name}_unsupported_tuple_shape")
        return tuple(_convert(item, arguments[0], f"{name}[]") for item in value)
    if origin is list:
        # JsonValue contains list[JsonValue]: decode JSON arrays as lists.
        if not isinstance(value, list):
            raise SchemaError(f"{name}_must_be_array")
        arguments = get_args(typ)
        element_type = arguments[0] if arguments else Any
        return [_convert(item, element_type, f"{name}[]") for item in value]
    if origin is _abc.Mapping or origin is dict:
        if not isinstance(value, dict) and not isinstance(value, _abc.Mapping):
            raise SchemaError(f"{name}_must_be_object")
        arguments = get_args(typ)
        key_type = arguments[0] if arguments else str
        value_type = arguments[1] if len(arguments) > 1 else Any
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key_type is str and not isinstance(key, str):
                raise SchemaError(f"{name}_keys_must_be_str")
            if value_type is Any or (
                isinstance(_unwrap(value_type), TypeAliasType)
                and _unwrap(value_type).__name__ == "JsonValue"
            ):
                _validate_json_value(item, f"{name}[{key!r}]")
                result[key] = item
            else:
                result[key] = _convert(item, value_type, f"{name}[{key!r}]")
        return result
    if is_dataclass(typ) and typ.__name__ in _ALL_RECORD_TYPES:
        if isinstance(value, typ):
            return value
        if not isinstance(value, _abc.Mapping):
            raise SchemaError(f"{name}_must_be_object")
        return decode_record(typ, value)
    raise SchemaError(f"{name}_unsupported_type:{typ!r}")


def _raise(name: str) -> Any:
    raise SchemaError(f"{name}_must_be_object")


def _validate_json_value(value: Any, name: str) -> None:
    if value is None or isinstance(value, (bool, str, int)):
        return
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise SchemaError(f"{name}_non_finite")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item, f"{name}[]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise SchemaError(f"{name}_object_keys_must_be_str")
            _validate_json_value(item, f"{name}[{key!r}]")
        return
    raise SchemaError(f"{name}_not_json_value")


def decode_record[T](record_type: type[T], payload: Mapping[str, JsonValue]) -> T:
    """Decode a JSON object into ``record_type``; construction revalidates."""
    if record_type not in _ALL_RECORD_TYPES.values():
        raise SchemaError(f"record_type_not_registered:{getattr(record_type, '__name__', record_type)}")
    if not isinstance(payload, Mapping):
        raise SchemaError("payload_must_be_object")
    expected = {field.name for field in fields(record_type)}
    provided = set(payload.keys())
    if provided != expected:
        missing = sorted(expected - provided)
        extra = sorted(provided - expected)
        raise SchemaError(f"field_mismatch:missing={missing}:extra={extra}")
    converted: dict[str, Any] = {}
    resolved = _field_types(record_type)
    for field in fields(record_type):
        converted[field.name] = _convert(payload[field.name], resolved[field.name], field.name)
    try:
        return record_type(**converted)
    except (TypeError, ValueError) as exc:
        raise SchemaError(f"record_construction_failed:{exc}") from exc


def validate_record(kind: str, payload: Mapping[str, JsonValue]) -> None:
    """Validate a payload against registered record type ``kind``."""
    if kind not in _ALL_RECORD_TYPES:
        raise SchemaError(f"unknown_record_kind:{kind}")
    decode_record(_ALL_RECORD_TYPES[kind], payload)


def encode_record_file(record_kind: str, schema_version: str, records: Sequence[object]) -> bytes:
    if record_kind not in _ALL_RECORD_TYPES:
        raise SchemaError(f"unknown_record_kind:{record_kind}")
    root = {
        "schema_version": schema_version,
        "record_type": record_kind,
        "contract_version": CONTRACT_VERSION,
        "records": [encode_record(record) for record in records],
    }
    return canonical_json_bytes(root)


def decode_record_file[T](payload: bytes, record_type: type[T], expected_schema_version: str) -> tuple[T, ...]:
    root = parse_json_bytes(payload)
    if not isinstance(root, dict):
        raise SchemaError("record_file_root_must_be_object")
    expected_keys = {"schema_version", "record_type", "contract_version", "records"}
    if set(root.keys()) != expected_keys:
        raise SchemaError(f"record_file_root_fields_must_be_exactly_{sorted(expected_keys)}")
    if root["schema_version"] != expected_schema_version:
        raise SchemaError(f"schema_version_mismatch:{root['schema_version']}")
    if root["record_type"] != record_type.__name__:
        raise SchemaError(f"record_type_mismatch:{root['record_type']}")
    if root["contract_version"] != CONTRACT_VERSION:
        raise SchemaError(f"contract_version_mismatch:{root['contract_version']}")
    if not isinstance(root["records"], list):
        raise SchemaError("records_must_be_array")
    return tuple(decode_record(record_type, record) for record in root["records"])


# E-P1 RULING 2.3: normal and measurement schema mutual rejection. The public
# hooks below are the ONLY decode entries for the payoff-path domain; they
# reject every normal ledger/exit/checkpoint envelope (exact schema_version
# equality) and the normal decode paths (decode_record_file with
# CHECKPOINT_SCHEMA_ID / decode_record(LedgerCheckpoint|OrderIntent, ...))
# reject payoff-path payloads in return: PayoffPathCheckpoint carries the
# extra path_id, PayoffPathOrder replaces decision_id with path_decision_id,
# so the exact-field-set rule fails on "ignore-unknown-fields compatibility".

def decode_payoff_training_plan_file(payload: bytes) -> tuple[PayoffTrainingPlan, ...]:
    """Strict decode of an ak_probability_payoff_training_plan_v1 envelope."""
    return decode_record_file(payload, PayoffTrainingPlan, PAYOFF_TRAINING_PLAN_SCHEMA_ID)


def decode_payoff_path_file[T](payload: bytes, record_type: type[T]) -> tuple[T, ...]:
    """Strict decode of an ak_probability_payoff_path_v1 envelope.

    ``record_type`` must be one of PayoffPathResult, PayoffPathCheckpoint,
    PayoffPathDecision, PayoffPathOrder or PayoffReceiptCommit; normal
    trading record types are rejected here and path types are rejected by
    the normal decoders, so a measurement record can never be restored as a
    production checkpoint/intent and vice versa.
    """
    allowed = (PayoffPathResult, PayoffPathCheckpoint, PayoffPathDecision,
               PayoffPathOrder, PayoffReceiptCommit)
    if record_type not in allowed:
        raise SchemaError(f"payoff_path_record_type_not_in_domain:{record_type}")
    return decode_record_file(payload, record_type, PAYOFF_PATH_SCHEMA_ID)


def encode_csv_bytes(record_type: type, records: Sequence[object]) -> bytes:
    """CSV projection: UTF-8, LF, RFC4180, first row is the fixed field order."""
    if record_type not in _ALL_RECORD_TYPES.values():
        raise SchemaError(f"record_type_not_registered:{record_type.__name__}")
    header = [field.name for field in fields(record_type)]
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    for record in records:
        if not isinstance(record, record_type):
            raise SchemaError("record_type_mismatch_in_rows")
        writer.writerow([_csv_cell(_jsonify(getattr(record, name)), name) for name in header])
    return buffer.getvalue().encode("utf-8")


def _csv_cell(value: Any, name: str) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, (int, str)):
        return str(value)
    if isinstance(value, (dict, list)):
        return canonical_json_bytes(value).decode("utf-8").rstrip("\n")
    raise SchemaError(f"{name}_unsupported_csv_cell")


def decode_csv_bytes[T](payload: bytes, record_type: type[T]) -> tuple[T, ...]:
    if record_type not in _ALL_RECORD_TYPES.values():
        raise SchemaError(f"record_type_not_registered:{record_type.__name__}")
    if payload.startswith(b"\xef\xbb\xbf"):
        raise SchemaError("bom_rejected")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise SchemaError("invalid_utf8") from None
    if "\r" in text:
        raise SchemaError("crlf_rejected")
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    rows = list(reader)
    if not rows:
        raise SchemaError("csv_header_required")
    header = rows[0]
    expected_header = [field.name for field in fields(record_type)]
    if header != expected_header:
        raise SchemaError("csv_header_order_mismatch")
    field_types = _field_types(record_type)
    decoded: list[T] = []
    for row in rows[1:]:
        if not row:
            raise SchemaError("csv_blank_row")
        if len(row) != len(header):
            raise SchemaError("csv_row_width_mismatch")
        converted: dict[str, Any] = {}
        for name, cell in zip(header, row):
            converted[name] = _csv_cell_to_value(cell, field_types[name], name)
        decoded.append(decode_record(record_type, converted))
    return tuple(decoded)


def _csv_cell_to_value(cell: str, typ: Any, name: str) -> Any:
    if cell == "":
        return None
    typ = _unwrap(typ)
    if isinstance(typ, UnionType) or get_origin(typ) is Union:
        arguments = get_args(typ)
        if NoneType in arguments:
            for argument in arguments:
                if _unwrap(argument) is NoneType:
                    continue
                try:
                    return _csv_cell_to_value(cell, argument, name)
                except SchemaError:
                    continue
            raise SchemaError(f"{name}_csv_cell_matches_no_union_branch")
    if typ is bool:
        if cell == "true":
            return True
        if cell == "false":
            return False
        raise SchemaError(f"{name}_csv_bool_must_be_true_false")
    if typ is str:
        return cell
    if typ is Decimal or typ is date or typ is Path or typ is str:
        return _convert(cell, typ, name)
    if typ is float or typ is int:
        return _convert(_json_number(cell, name), typ, name)
    if get_origin(typ) is Literal:
        return _convert(cell, typ, name)
    parsed = parse_json_bytes(cell.encode("utf-8"))
    return _convert(parsed, typ, name)


def _json_number(cell: str, name: str) -> Any:
    try:
        return json.loads(cell, parse_constant=_reject_constant)
    except (ValueError, SchemaError):
        raise SchemaError(f"{name}_csv_numeric_cell_invalid") from None


def _bucket_of(value: Decimal, boundaries: tuple[Decimal | None, ...]) -> int:
    """0-based left-closed/right-open bucket index; extremes fall into the last bucket."""
    for index in range(len(boundaries) - 1):
        low = boundaries[index]
        high = boundaries[index + 1]
        if (low is None or value >= low) and (high is None or value < high):
            return index
    return len(boundaries) - 2


def calibration_geometry_key(features: FeatureSnapshot, frozen_range: FrozenRange | None,
                             event: str) -> str | None:
    """Versioned geometry-bucket support key (geometry-v2.1.1, 变更申请-002 §3).

    ``C`` is uniquely the current decision snapshot's ``reference_price``;
    ``FrozenRange.center`` stays the frozen regression target M and may
    deviate from C without redrawing or clamping. ``ATR`` is the same
    snapshot's ``atr_points``; L/U/stops come from the passed range and
    W = U - L. An old range may be reused (``source_snapshot_id`` may differ
    from the current snapshot) but must have been frozen no later than the
    decision. The tick is the snapshot's unique, valid, decision-visible
    ``META:price_tick:v2.1.0`` positive finite Decimal; duplicates error and
    missing/invalid values never guess. Event names map ``PERSIST:<E>`` to E
    and ``STOP_TOUCH:LONG/SHORT`` to ``STOP_TOUCH``; range-free events keep
    the fixed ``ANCHOR_ATR`` tail. Missing ingredients return ``None``.
    """
    if not isinstance(event, str):
        raise TypeError("event_must_be_str")
    if not isinstance(features, FeatureSnapshot):
        raise TypeError("features_must_be_FeatureSnapshot")
    if frozen_range is not None and not isinstance(frozen_range, FrozenRange):
        raise TypeError("frozen_range_must_be_FrozenRange_or_None")
    mapped_event = event.split(":", 1)[1] if event.startswith("PERSIST:") else event
    if mapped_event.startswith("STOP_TOUCH:"):
        mapped_event = "STOP_TOUCH"
    if mapped_event in RANGE_FREE_EVENTS:
        return f"{_GEOMETRY_VERSION}|{mapped_event}|ANCHOR_ATR"
    if frozen_range is None:
        return None
    if features.reference_price is None:
        return None
    anchor = features.reference_price
    atr = features.atr_points
    if atr is None or atr <= 0:
        return None
    if frozen_range.frozen_at_nano > features.decision_nano:
        return None
    tick: Decimal | None = None
    tick_entries = 0
    for factor in features.values:
        if factor.factor_key != META_TICK_FACTOR_KEY:
            continue
        tick_entries += 1
        if tick_entries > 1:
            raise SchemaError("duplicate_meta_price_tick_entries")
        if isinstance(factor.value, Decimal) and factor.value.is_finite() \
                and factor.value > 0 and factor.quality.status == "READY":
            visible = (factor.available_at_nano is not None
                       and factor.available_at_nano <= features.decision_nano)
            if visible:
                tick = factor.value
    if tick is None:
        return None
    width = frozen_range.upper - frozen_range.lower
    if width <= 0:
        return None
    bucket_width = _bucket_of(width / atr, (Decimal(0), Decimal(2), Decimal(4), Decimal(6), None))
    bucket_position = _bucket_of(
        (anchor - frozen_range.lower) / width,
        (None, Decimal(0), Decimal("0.25"), Decimal("0.5"), Decimal("0.75"), Decimal(1), None),
    )
    bucket_lower_stop = _bucket_of(
        (anchor - frozen_range.stop_lower) / atr,
        (None, Decimal(0), Decimal(1), Decimal(2), Decimal(4), None),
    )
    bucket_upper_stop = _bucket_of(
        (frozen_range.stop_upper - anchor) / atr,
        (None, Decimal(0), Decimal(1), Decimal(2), Decimal(4), None),
    )
    bucket_tick = _bucket_of(tick / atr, (Decimal(0), Decimal("0.01"), Decimal("0.05"), Decimal("0.1"), None))
    return (f"{_GEOMETRY_VERSION}|{mapped_event}|{bucket_width}-{bucket_position}"
            f"-{bucket_lower_stop}-{bucket_upper_stop}-{bucket_tick}")


def type_to_str(typ: Any) -> str:
    """Deterministic display form used in canonical signatures and lock hashing."""
    if isinstance(typ, TypeAliasType):
        return typ.__name__
    typ = _unwrap(typ)
    if typ is NoneType:
        return "None"
    if typ is Literal or get_origin(typ) is Literal:
        options = get_args(typ)
        return "Literal[" + ", ".join(repr(option) for option in options) + "]"
    if isinstance(typ, UnionType) or get_origin(typ) is Union:
        return " | ".join(type_to_str(argument) for argument in get_args(typ))
    origin = get_origin(typ)
    if origin is tuple:
        arguments = get_args(typ)
        if len(arguments) == 2 and arguments[1] is Ellipsis:
            return f"tuple[{type_to_str(arguments[0])}, ...]"
        return "tuple[" + ", ".join(type_to_str(a) for a in arguments) + "]"
    if origin is not None and isinstance(origin, type) and origin.__name__ in {"Mapping", "dict", "list"}:
        arguments = get_args(typ)
        name = origin.__name__
        if not arguments:
            return name
        return f"{name}[" + ", ".join(type_to_str(a) for a in arguments) + "]"
    if isinstance(typ, type):
        return typ.__name__
    return str(typ)


def canonical_type_signature(record_type: type) -> str:
    if not is_dataclass(record_type):
        raise SchemaError(f"record_type_must_be_dataclass:{getattr(record_type, '__name__', record_type)}")
    resolved = _field_types(record_type)
    parts = [f"{field.name}: {type_to_str(resolved[field.name])}" for field in fields(record_type)]
    return f"{record_type.__name__}(" + ", ".join(parts) + ")"


def canonical_schema_descriptor(kind: str) -> str:
    """Canonical textual schema description for a named schema ID (doc §7)."""
    descriptors = {
        BARS_SCHEMA_ID: f"{BARS_SCHEMA_ID}|columns=" + ",".join(f.name for f in fields(MarketBar))
                        + "|quality=canonical_json_cell",
        INSTRUMENTS_SCHEMA_ID: f"{INSTRUMENTS_SCHEMA_ID}|root=schema_version,record_type,contract_version,"
                               f"records|record_type=Instrument|fields=" + ",".join(f.name for f in fields(Instrument)),
        SESSIONS_SCHEMA_ID: f"{SESSIONS_SCHEMA_ID}|root=schema_version,record_type,contract_version,"
                            f"records|record_type=SessionWindow|fields=" + ",".join(f.name for f in fields(SessionWindow)),
        MACRO_ITEMS_SCHEMA_ID: f"{MACRO_ITEMS_SCHEMA_ID}|root=schema_version,record_type,contract_version,"
                               f"records|record_type=MacroItem|fields=" + ",".join(f.name for f in fields(MacroItem))
                               + "|records_may_be_empty",
        RULES_SCHEMA_ID: f"{RULES_SCHEMA_ID}|root=schema_version,record_type,contract_version,"
                         f"records|record_type=TradingRule|fields=" + ",".join(f.name for f in fields(TradingRule))
                         + "|unknown_rules_must_not_masquerade_as_zero_cost",
        AVAILABILITY_SCHEMA_ID: f"{AVAILABILITY_SCHEMA_ID}|root=schema_version:str,input_manifest_sha256:"
                                "str|null,contract_status:dict[str,str],minute_history_status:str,"
                                "realtime_status:str,reasons:list[str]|initial_realtime=NOT_ACCEPTED",
        MANIFEST_SCHEMA_ID: f"{MANIFEST_SCHEMA_ID}|root=schema_version:str,contract_version:str,"
                            "dataset_kind:str,created_at_nano:int,request:object,"
                            "read_only_transaction:object,files:dict[str,str],"
                            "source_hashes:dict[str,str],limitations:list[str]"
                            "|read_only_transaction=isolation:str,read_only:bool,verified:bool"
                            "|synthetic_input_isolation=NOT_APPLICABLE",
        SHARED_FIXTURE_SCHEMA_ID: f"{SHARED_FIXTURE_SCHEMA_ID}|root=schema_version,contract_version,"
                                  "instruments,sessions,bars,macro_items,rules,features,forecasts,"
                                  "regimes,accounts,strategy_specs",
        CHECKPOINT_SCHEMA_ID: f"{CHECKPOINT_SCHEMA_ID}|root=schema_version,record_type,contract_version,"
                                "records|record_type=LedgerCheckpoint|fields="
                                + ",".join(f.name for f in fields(LedgerCheckpoint)),
        SHADOW_CHECKPOINT_SCHEMA_ID: f"{SHADOW_CHECKPOINT_SCHEMA_ID}|shadow_checkpoint_envelope"
                                     "|checkpoint_payload=LedgerCheckpoint",
        METRICS_SCHEMA_ID: f"{METRICS_SCHEMA_ID}|root=schema_version,research_policy_version,"
                            "macro_policy_version,cost_scenarios,required_group_keys,"
                            "model_bundles,forecast_groups,strategy_groups,replay_summaries,"
                            "exclusions,bootstrap",
        SHADOW_EVENT_SCHEMA_ID: f"{SHADOW_EVENT_SCHEMA_ID}|envelope=schema_version,contract_version,"
                                "sequence,event_id,event(FeedEvent,kind_includes_MODEL_ACTIVATION)"
                                ",runner_received_at_nano,validated_at_nano,"
                                "clock_evidence_sha256,decision_ids,checkpoint_hash,"
                                "previous_hash,row_hash",
        MODEL_ACTIVATION_REQUEST_SCHEMA_ID: f"{MODEL_ACTIVATION_REQUEST_SCHEMA_ID}"
                                            "|root=schema_version,request:ModelActivationRequest",
        MODEL_ACTIVATIONS_SCHEMA_ID: f"{MODEL_ACTIVATIONS_SCHEMA_ID}|root=schema_version,"
                                     "records:list[request_fields_plus_effective_at_nano"
                                     "_and_old_qualification_hashes,no_self_hash]",
        SEASONALITY_SCHEMA_ID: (
            f"{SEASONALITY_SCHEMA_ID}|model_file=SeasonalityModel_six_fields"
            "|bucket_key=(product,session_kind,slot_index,duration_seconds,calendar_version)"
            "|columns=SEASONAL:volume_relative:v2.1.1:d<duration_seconds>,"
            "SEASONAL:abs_return_relative:v2.1.1:d<duration_seconds>"
            "|record_fields_unchanged"),
        CONTRACT_LOCK_SCHEMA_ID: f"{CONTRACT_LOCK_SCHEMA_ID}|root=schema_version:str,contract_version:str,"
                                 "document_version:str,public_files:dict[str,str],"
                                 "public_signatures:dict[str,str],schemas:dict[str,str],"
                                 "fixture_files:dict[str,str],ownership:dict[str,list[str]],"
                                 "required_tests:dict[str,list[str]],"
                                 "required_check_ids:dict[str,list[str]],policy:object,"
                                 "reviewed_revision:str,reviewer_report_sha256:str",
        FACTOR_REGISTRY_SCHEMA_ID: (
            f"{FACTOR_REGISTRY_SCHEMA_ID}|envelope=schema_version,record_type,"
            "contract_version,records|version_marks_frozen_registration_set"
            "|includes_v2_baseline_opening_gap_item"),
        EXIT_SPECS_SCHEMA_ID: f"{EXIT_SPECS_SCHEMA_ID}|root=schema_version,record_type,"
                               f"contract_version,records|record_type=ExitSpec|fields="
                               + ",".join(f.name for f in fields(ExitSpec)),
        EXIT_FEATURE_SPECS_SCHEMA_ID: f"{EXIT_FEATURE_SPECS_SCHEMA_ID}|root=schema_version,"
                                       f"record_type,contract_version,records|record_type="
                                       f"ExitFeatureSpec|fields="
                                       + ",".join(f.name for f in fields(ExitFeatureSpec)),
        EXIT_BINDINGS_SCHEMA_ID: f"{EXIT_BINDINGS_SCHEMA_ID}|root=schema_version,record_type,"
                                  f"contract_version,records|record_type=ExitPolicyBinding"
                                  f"|fields=" + ",".join(f.name for f in fields(ExitPolicyBinding))
                                  + "|binding_sha256_excludes_self",
        EXIT_FEATURES_SCHEMA_ID: f"{EXIT_FEATURES_SCHEMA_ID}|root=schema_version,record_type,"
                                  f"contract_version,records|record_type=ExitFeatureSnapshot"
                                  f"|fields=" + ",".join(f.name for f in fields(ExitFeatureSnapshot)),
        EXIT_STATES_SCHEMA_ID: f"{EXIT_STATES_SCHEMA_ID}|root=schema_version,record_type,"
                                f"contract_version,records|record_type=ExitState|fields="
                                + ",".join(f.name for f in fields(ExitState))
                                + "|committed_snapshots_only",
        EXIT_PROPOSALS_SCHEMA_ID: f"{EXIT_PROPOSALS_SCHEMA_ID}|root=schema_version,record_type,"
                                   f"contract_version,records|record_type=ExitProposal|fields="
                                   + ",".join(f.name for f in fields(ExitProposal))
                                   + "|next_state_embedded_candidate",
        EXIT_EXECUTION_LINKS_SCHEMA_ID: f"{EXIT_EXECUTION_LINKS_SCHEMA_ID}|root=schema_version,"
                                         f"record_type,contract_version,records|record_type="
                                         f"ExitExecutionLink|fields="
                                         + ",".join(f.name for f in fields(ExitExecutionLink)),
        EXIT_AUDIT_SCHEMA_ID: f"{EXIT_AUDIT_SCHEMA_ID}|root=schema_version,contract_version,"
                               "records|records=arbitration_audits_with_candidate_ids_and"
                               "_selected_target_and_decision_intent_references",
        EXIT_LOGICAL_REPLAY_SCHEMA_ID: f"{EXIT_LOGICAL_REPLAY_SCHEMA_ID}|root=schema_version,"
                                         "contract_version,binding_sha256,initial_state,"
                                         "snapshots,proposals,final_state,financial_result"
                                         "|financial_result_always_null",
        EXIT_RUN_SCHEMA_ID: f"{EXIT_RUN_SCHEMA_ID}|root=schema_version,contract_version"
                             "|manifest_envelope_binding_lock_both_registries_full_specs"
                             "_binding_fixture_input_prefix_and_code",
        INPUT_COLLECTION_SCHEMA_ID: f"{INPUT_COLLECTION_SCHEMA_ID}|root=schema_version,"
                                     f"contract_version,dataset_kind,"
                                     "shards:list[object],files:dict[str,str],"
                                     "limitations:list[str]"
                                     "|shard_item={path:str,manifest_sha256:str}",
        PAYOFF_TRAINING_PLAN_SCHEMA_ID: (
            f"{PAYOFF_TRAINING_PLAN_SCHEMA_ID}|root=schema_version,record_type,"
            f"contract_version,records|record_type=PayoffTrainingPlan|fields="
            + ",".join(f.name for f in fields(PayoffTrainingPlan))
            + "|no_self_hash_field|plan_sha256=sha256_canonical_record"
            + "|timing=inner_training_end<inner_calibration_end<warmup_start"
            "<=window_start<window_end<=training_end"),
        PAYOFF_PATH_SCHEMA_ID: (
            f"{PAYOFF_PATH_SCHEMA_ID}|root=schema_version,record_type,"
            f"contract_version,records|record_types=PayoffPathResult,"
            f"PayoffPathCheckpoint,PayoffPathDecision,PayoffPathOrder,"
            f"PayoffReceiptCommit|PayoffPathResult_fields="
            + ",".join(f.name for f in fields(PayoffPathResult))
            + "|PayoffPathCheckpoint_fields="
            + ",".join(f.name for f in fields(PayoffPathCheckpoint))
            + "|checkpoint_domain_separate_from_ak_probability_checkpoints_v2.1.3"
            "|mutual_rejection_with_normal_decoder_and_restore"),
    }
    if kind not in descriptors:
        raise SchemaError(f"unknown_schema_id:{kind}")
    return descriptors[kind]


def encode_shared_fixture(root_fields: Mapping[str, Any]) -> bytes:
    """Encode the shared fixture canonical bytes from its root field values."""
    expected = {
        "schema_version", "contract_version", "instruments", "sessions", "bars", "macro_items",
        "rules", "features", "forecasts", "regimes", "accounts", "strategy_specs",
    }
    if set(root_fields.keys()) != expected:
        raise SchemaError(f"shared_fixture_root_fields_must_be_exactly_{sorted(expected)}")
    return canonical_json_bytes(dict(root_fields))


def decode_shared_fixture(payload: bytes) -> dict[str, Any]:
    root = parse_json_bytes(payload)
    if not isinstance(root, dict):
        raise SchemaError("shared_fixture_root_must_be_object")
    if root.get("schema_version") != SHARED_FIXTURE_SCHEMA_ID:
        raise SchemaError(f"schema_version_mismatch:{root.get('schema_version')}")
    if root.get("contract_version") != CONTRACT_VERSION:
        raise SchemaError("contract_version_mismatch")
    sections = {
        "instruments": Instrument, "sessions": SessionWindow, "bars": MarketBar,
        "macro_items": MacroItem, "rules": TradingRule, "features": FeatureSnapshot,
        "forecasts": MarketForecast, "regimes": RegimeAssessment, "accounts": AccountSnapshot,
        "strategy_specs": StrategySpec,
    }
    decoded: dict[str, Any] = {
        "schema_version": root["schema_version"],
        "contract_version": root["contract_version"],
    }
    for section, record_type in sections.items():
        records = root.get(section)
        if not isinstance(records, list):
            raise SchemaError(f"{section}_must_be_array")
        decoded[section] = tuple(decode_record(record_type, record) for record in records)
    return decoded
