"""M2-01: registry identity, parameters, formulas and definition mapping.

Covers the frozen factor_key format, per-duration parameter versions,
VOI009's instant registration, the REV001 ``k_equals_n`` alias, the eight
mandatory parameter keys, minute/daily annualization, the fourteen v1
baseline specs (checked read-only against the live v1 registry), registry
artifact round-trips and the absence of formula execution (M2-H02/H04).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ak_trading.probability.factors import factor_registry as v1_factor_registry
from ak_trading.v2.contracts import FactorSpec
from ak_trading.v2.factor_specs import (
    BASELINE_DEFINITIONS,
    LEGACY_COMPARISON_NAMES,
    BASELINE_NAMES,
    DAILY_DURATION,
    DAILY_INSTANT_VERSION,
    DAILY_PARAMETER_VERSION,
    DURATIONS,
    MANDATORY_PARAMETER_KEYS,
    MINUTE_INSTANT_VERSIONS,
    MINUTE_PARAMETER_VERSIONS,
    NOT_APPLICABLE,
    OPERATOR_VERSION,
    SOURCE_CATALOG_ID,
    SOURCE_FACTOR_IDS,
    SOURCE_FORMULAS,
    baseline_definition_sha256,
    baseline_factor_key,
    baseline_registry_v2,
    factor_registry_v2,
    instant_parameter_version,
    parameter_version,
    source_definition_sha256,
)
from ak_trading.v2.schemas import decode_record_file, encode_record_file

FACTORY_REGISTRY_SCHEMA = "ak_probability_factor_registry_v2.1.0"
MODULES = (
    "factor_specs.py", "factors.py", "seasonality.py", "baseline.py",
)


def test_registry_covers_ten_factors_for_all_six_durations() -> None:
    for duration in DURATIONS:
        registry = factor_registry_v2(duration)
        assert len(registry) == 10
        assert [spec.factor_key for spec in registry] == sorted(
            spec.factor_key for spec in registry)
        assert {spec.source_factor_id for spec in registry} == set(SOURCE_FACTOR_IDS)
        assert all(spec.duration_seconds == duration for spec in registry)
        assert all(spec.operator_version == OPERATOR_VERSION for spec in registry)
        assert all(len(spec.source_sha256) == 64 for spec in registry)
    with pytest.raises(ValueError):
        factor_registry_v2(120)
    with pytest.raises(TypeError):
        factor_registry_v2(True)


def test_factor_key_format_and_minute_daily_parameter_versions() -> None:
    minute = factor_registry_v2(60)
    daily = factor_registry_v2(DAILY_DURATION)
    by_id = {spec.source_factor_id: spec for spec in minute}
    assert by_id["MOM010"].factor_key == "MOM010:v2.1.0:d60:m1-n60"
    assert by_id["MOM010"].parameter_version == "m1-n60"
    daily_by_id = {spec.source_factor_id: spec for spec in daily}
    assert daily_by_id["MOM010"].factor_key == "MOM010:v2.1.0:d86400:d1-n20"
    assert daily_by_id["MOM010"].parameter_version == "d1-n20"
    # Minute and daily parameter identities never collide (M2-H02).
    minute_versions = {spec.parameter_version for spec in minute}
    daily_versions = {spec.parameter_version for spec in daily}
    assert not (minute_versions & daily_versions)
    for duration, expected in MINUTE_PARAMETER_VERSIONS.items():
        assert parameter_version(duration) == expected
    assert parameter_version(DAILY_DURATION) == DAILY_PARAMETER_VERSION
    # Window n is complete bars of the frequency: 60 minute bars / 20 days.
    for spec in minute:
        if spec.source_factor_id != "VOI009":
            assert spec.parameters["n"] == 60
            assert spec.lookback == 61
    for spec in daily:
        if spec.source_factor_id != "VOI009":
            assert spec.parameters["n"] == 20
            assert spec.lookback == 21


def test_voi009_registers_instant_versions_without_window() -> None:
    for duration in DURATIONS:
        registry = {spec.source_factor_id: spec for spec in factor_registry_v2(duration)}
        spec = registry["VOI009"]
        expected = (MINUTE_INSTANT_VERSIONS.get(duration) or DAILY_INSTANT_VERSION)
        assert spec.parameter_version == expected == instant_parameter_version(duration)
        assert spec.factor_key == f"VOI009:v2.1.0:d{duration}:{expected}"
        assert spec.lookback == 1
        assert "n" not in spec.parameters
        assert spec.parameters["window_unit"] == NOT_APPLICABLE
        assert spec.parameters["session_boundary"] == NOT_APPLICABLE


def test_rev001_registers_k_equals_n_alias() -> None:
    for duration in DURATIONS:
        spec = {s.source_factor_id: s for s in factor_registry_v2(duration)}["REV001"]
        expected_n = 60 if duration != DAILY_DURATION else 20
        assert spec.formula == "-sum(r,k)"
        assert spec.parameters["n"] == expected_n
        assert spec.parameters["k"] == expected_n
        assert spec.parameters["alias"] == "k_equals_n"


def test_mandatory_parameter_keys_present_on_every_spec() -> None:
    for duration in DURATIONS:
        for spec in (*factor_registry_v2(duration), *baseline_registry_v2(duration)):
            for key in MANDATORY_PARAMETER_KEYS:
                assert key in spec.parameters, (spec.factor_key, key)
            assert spec.parameters["window_unit"] in ("BARS", "SESSION_PAIR", NOT_APPLICABLE)


def test_annualization_frozen_per_bar_minute_and_nominal_252_daily() -> None:
    def parameters(duration: int, factor_id: str) -> dict:
        return {s.source_factor_id: s for s in factor_registry_v2(duration)}[factor_id].parameters
    assert parameters(60, "VOL001")["annualization"] == "PER_BAR"
    assert parameters(60, "VOL001")["annualization_factor"] == 1
    assert parameters(60, "VOL005")["annualization"] == "PER_BAR"
    assert parameters(60, "VOL005")["annualization_factor"] == 1
    assert parameters(DAILY_DURATION, "VOL001")["annualization"] == "NOMINAL_252"
    assert parameters(DAILY_DURATION, "VOL001")["annualization_factor"] == 252
    assert parameters(DAILY_DURATION, "VOL005")["annualization_factor"] == 252
    # Factors without a scale never carry an annualization value.
    assert parameters(60, "MOM001")["annualization"] == NOT_APPLICABLE
    assert parameters(60, "MOM001")["annualization_factor"] == NOT_APPLICABLE
    # The legacy vol20 baseline is explicitly unannualized.
    vol20 = {s.source_factor_id: s for s in baseline_registry_v2(60)}["vol20"]
    assert vol20.parameters["annualization"] == "NOT_ANNUALIZED"
    assert vol20.parameters["annualization_factor"] == NOT_APPLICABLE


def test_source_formulas_and_definition_hashes_match_frozen_catalog() -> None:
    assert set(SOURCE_FORMULAS) == set(SOURCE_FACTOR_IDS)
    expected_formulas = {
        "MOM001": "sum(r,n)",
        "MOM003": "sum(r,n)/(std(r,n)*sqrt(n))",
        "MOM010": "(P-lag(P,n))/sum(abs(diff(P)),n)",
        "REV001": "-sum(r,k)",
        "VOL001": "std(r,n)*sqrt(A)",
        "VOL004": "ATR(n)/abs(P)",
        "VOL005": "mean(min(r,0)^2,n)*A",
        "VOI001": "V/lag(mean(V,n),1)-1",
        "VOI007": "OI/lag(OI,n)-1",
        "VOI009": "V/OI",
    }
    assert dict(SOURCE_FORMULAS) == expected_formulas
    for duration in DURATIONS:
        for spec in factor_registry_v2(duration):
            assert spec.formula == expected_formulas[spec.source_factor_id]
            assert spec.source_sha256 == source_definition_sha256(spec.source_factor_id)
    assert SOURCE_CATALOG_ID == "china_futures_v1_20260913"


def test_required_fields_and_units_declared() -> None:
    expected_fields = {
        "MOM001": ("close",), "MOM003": ("close",), "MOM010": ("close",),
        "REV001": ("close",), "VOL001": ("close",), "VOL005": ("close",),
        "VOL004": ("high", "low", "close"),
        "VOI001": ("volume",), "VOI007": ("close_oi",),
        "VOI009": ("volume", "close_oi"),
    }
    for duration in DURATIONS:
        for spec in factor_registry_v2(duration):
            assert spec.required_fields == expected_fields[spec.source_factor_id]
            assert spec.unit and isinstance(spec.unit, str)


def test_baseline_registry_matches_v1_definitions_read_only() -> None:
    v1 = {spec.name: spec for spec in v1_factor_registry()}
    assert set(v1) == set(LEGACY_COMPARISON_NAMES)  # v1 stays at fourteen (006)
    for duration in DURATIONS:
        registry = baseline_registry_v2(duration)
        assert len(registry) == 15  # 006 §2: fourteen v1 windows + opening gap
        assert [spec.factor_key for spec in registry] == sorted(
            spec.factor_key for spec in registry)
        for spec in registry:
            name = spec.source_factor_id
            if name == "BASE:opening_gap_points":
                continue
            assert spec.factor_key == baseline_factor_key(name, duration) \
                == f"BASE:{name}:v2.1.0:d{duration}"
            assert spec.formula == BASELINE_DEFINITIONS[name] == v1[name].definition
            assert spec.lookback == v1[name].lookback
            assert spec.unit == v1[name].unit
            assert spec.parameters["reference_label"] == "legacy-v1"
            assert spec.source_sha256 == baseline_definition_sha256(name)
            # Old windows keep their include/exclude semantics visibly.
            if name in ("prior_high20", "prior_low20", "zscore20", "volume_ratio20"):
                assert spec.parameters["include_current"] is False
            if name in ("ma20", "ma60", "vol20", "atr14", "er20"):
                assert spec.parameters["include_current"] is True
            # ER keeps the absolute value; MOM010 is the signed variant.
            if name == "er20":
                assert spec.parameters["signed"] is False
            if name == "volume_ratio20":
                assert spec.parameters["minus_one"] is False
            if name == "oi_change5":
                assert spec.parameters["lag"] == 5
                assert spec.parameters["minus_one"] is True


def test_factor_registry_json_roundtrip_sorted_by_key() -> None:
    records = factor_registry_v2(300)
    payload = encode_record_file("FactorSpec", FACTORY_REGISTRY_SCHEMA, records)
    decoded = decode_record_file(payload, FactorSpec, FACTORY_REGISTRY_SCHEMA)
    assert decoded == records
    import json
    root = json.loads(payload.decode("utf-8"))
    keys = [record["factor_key"] for record in root["records"]]
    assert keys == sorted(keys)


def test_no_formula_execution_in_m2_modules() -> None:
    base = Path(__file__).resolve().parents[1] / "src" / "ak_trading" / "v2"
    for name in MODULES:
        source = (base / name).read_text(encoding="utf-8")
        for forbidden in ("eval(", "exec(", "compile("):
            assert forbidden not in source, (name, forbidden)
