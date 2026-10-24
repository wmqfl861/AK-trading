"""Bounded source inventory is observable provenance, never historical approval."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")

from ak_trading.futures.io import read_input_bundle
from ak_trading.futures.readiness import inspect_readiness
from ak_trading.persistence.futures_research import export_futures_bundle
from test_futures_export_connections import FakeEngine, make_request


OPENCTP = (
    "交易所,合约代码,品种代码,合约乘数,最小跳动,开仓费率,平今费用/手,做多保证金率,更新时间,API_KEY,备注\r\n"
    "SHFE,rb2609,rb,10,1,0.0001,0,0.07,2026-09-07 15:30:00,hidden_secret,postgresql://user:password@host/db\r\n"
).encode("utf-8-sig")


def export_with_source(tmp_path, payload=OPENCTP, filename="openctp.csv"):
    request = make_request(tmp_path)
    source = request.catalog_path.parent / filename
    source.write_bytes(payload)
    request = replace(request, local_metadata_paths=(source,))
    output = tmp_path / "bundle"
    outcome = export_futures_bundle(FakeEngine(), request, output)
    assert outcome.status == "published", outcome.reasons
    return source, read_input_bundle(output)


def test_openctp_file_bytes_are_hashed_and_inventory_is_frozen_without_secrets(tmp_path):
    source, bundle = export_with_source(tmp_path)
    inventory = bundle.metadata["source_inventory"]
    assert inventory["schema_version"] == "futures_local_metadata_inventory_v1"
    assert inventory["status"] == "loaded"
    assert inventory["scope"] == "explicit_local_files_only"
    record, = inventory["sources"]
    digest = hashlib.sha256(OPENCTP).hexdigest()
    assert record["source_id"] == "local_metadata:" + digest
    assert record["sha256"] == digest
    assert record["size_bytes"] == len(OPENCTP)
    assert record["format"] == "csv" and record["shape"] == "openctp_raw"
    assert record["row_count"] == 1
    assert "合约乘数" in record["fields"]
    assert record["detected_dates"]["更新时间"] == ["2026-09-07T15:30:00"]
    assert record["contract_presence"]["SHFE.rb2609"]["row_count"] == 1
    assert "multiplier" in record["contract_presence"]["SHFE.rb2609"]["semantic_fields"]
    assert record["approval_status"] == "unapproved"
    assert record["historical_validity"] == "not_assessed"
    assert bundle.metadata["fields"] == {}
    assert bundle.manifest["source_hashes"][record["source_id"]] == digest
    assert bundle.availability["source_hashes"][record["source_id"]] == digest
    encoded = b"".join(path.read_bytes() for path in bundle.root.iterdir())
    for secret in (b"API_KEY", b"hidden_secret", b"postgresql://", b"password@host", str(source).encode()):
        assert secret not in encoded
    source.unlink()
    assert read_input_bundle(bundle.root).metadata == bundle.metadata
    metadata_path = bundle.root / "metadata.json"
    metadata_path.write_bytes(metadata_path.read_bytes().replace(digest.encode(), b"0" * 64))
    with pytest.raises(ValueError, match="content hash mismatch for metadata.json"):
        read_input_bundle(bundle.root)


@pytest.mark.parametrize("document,shape,row_count,symbol", [
    ({"calendar": {"valid_from": "2026-01-01", "valid_through": "2026-12-31"}, "products": [{
        "exchange": "SHFE", "code": "RB", "research_contract": "SHFE.rb2609",
        "contract_size_tons": "10", "price_tick_cny_per_ton": "1",
        "sessions": {"day": ["09:00-10:15"], "night": ["21:00-23:00"]},
        "fees": {"applicable_contracts": ["SHFE.rb2609"], "effective_from": "2026-02-12",
                 "components": {"close_today": {"value": "0.0001"}},
                 "provenance": {"source_url": "https://user:SECRET@host", "publication_date": "2026-02-10"}},
    }], "API_KEY": "SECRET"}, "reference_products", 1, "SHFE.rb2609"),
    ({"day": "2026-09-07", "official": {"SHFE.rb": {"spec_long": 0.07, "ct_fee_unit": 0}},
      "reference": {"SHFE.rb": {"fee_open_ratio": 0.0001, "updated_at": "2026-09-07 15:30:00"}}},
     "settlement_snapshot", 2, None),
    ([{"ExchangeID": "SHFE", "InstrumentID": "rb2609", "VolumeMultiple": 10,
       "PriceTick": 1, "TradingDay": "20260907", "Password": "SECRET"}], "json_records", 1, "SHFE.rb2609"),
    ({"o_cursor": [{"INSTRUMENTID": "rb2609", "PRODUCTID": "rb_f", "SPECLONGMARGINRATIO": 0.07}]},
     "o_cursor_records", 1, None),
])
def test_json_reference_and_snapshot_shapes_preserve_only_observed_inventory(tmp_path, document, shape, row_count, symbol):
    raw = json.dumps(document, ensure_ascii=False).encode()
    _, bundle = export_with_source(tmp_path, raw, "snapshot.json")
    record, = bundle.metadata["source_inventory"]["sources"]
    assert record["format"] == "json" and record["shape"] == shape
    assert record["row_count"] == row_count
    assert record["sha256"] == hashlib.sha256(raw).hexdigest()
    assert record["historical_validity"] == "not_assessed"
    assert record["approval_status"] == "unapproved"
    if symbol:
        assert record["contract_presence"][symbol]["row_count"] == 1
        assert {"multiplier", "price_tick"} <= set(record["contract_presence"][symbol]["semantic_fields"])
    else:
        assert record["contract_presence"] == {}
    if shape == "reference_products":
        assert record["detected_dates"]["fees.effective_from"] == ["2026-02-12"]
        assert record["detected_dates"]["calendar.valid_from"] == ["2026-01-01"]
        assert "historical_session_windows" not in bundle.metadata["fields"]
    if shape == "settlement_snapshot":
        assert record["product_presence"]["SHFE.RB"]["row_count"] == 2
        assert record["detected_dates"]["day"] == ["2026-09-07"]
    encoded = json.dumps(bundle.metadata)
    assert "SECRET" not in encoded and "source_url" not in encoded


def test_provided_source_presence_is_contract_scoped_and_cannot_unlock_execution(tmp_path):
    _, bundle = export_with_source(tmp_path)
    product = bundle.availability["products"]["SHFE.RB"]
    assert product["local_source_presence"]["status"] == "present_reference_only"
    presence = product["contract_source_presence"]
    assert presence["SHFE.rb2609"]["status"] == "present_reference_only"
    assert presence["SHFE.rb2610"]["status"] == "not_observed_in_loaded_sources"
    report = inspect_readiness(bundle)
    assert report["implementation_limitations"]["historical_metadata_adapter"] == "not_implemented"
    assessment = report["metadata_assessment"]
    assert assessment["source_inventory_status"] == "loaded"
    assert assessment["source_presence"] == "present_reference_only"
    assert assessment["database_metadata_status"] == "not_assessed"
    assert assessment["bundle_fields_status"] == "absent"
    rows = {row["raw_symbol"]: row for row in report["products"]["SHFE.RB"]["contracts"]}
    present = rows["SHFE.rb2609"]
    other = rows["SHFE.rb2610"]
    assert present["local_source_presence"]["status"] == "present_reference_only"
    assert present["field_evidence"]["multiplier"]["status"] == "present_reference_only"
    interpretation = present["field_interpretation"]["multiplier"]
    assert interpretation["source_presence"] == "present_reference_only"
    assert interpretation["historical_validity"] == "not_assessed"
    assert interpretation["approval_status"] == "unapproved"
    assert other["field_evidence"]["multiplier"]["status"] == "missing"
    assert other["field_interpretation"]["multiplier"]["source_presence"] == "not_observed_in_loaded_sources"
    assert report["strict_execution_enabled"] is False
    assert all(row["actions"][action]["status"] == "blocked" for row in rows.values() for action in report["strict_actions"])


@pytest.mark.parametrize("legacy_paths", [[], ["never-read-source.json"]])
def test_old_bundle_reports_unloaded_sources_without_claiming_database_absence(tmp_path, legacy_paths):
    request = make_request(tmp_path)
    output = tmp_path / "old-bundle"
    assert export_futures_bundle(FakeEngine(), request, output).status == "published"
    bundle = read_input_bundle(output)
    before = {path.name: path.read_bytes() for path in output.iterdir()}
    bundle.metadata["local_metadata_paths"] = legacy_paths
    report = inspect_readiness(bundle)
    assert report["metadata_status"] == "no_historical_metadata_fields"
    assessment = report["metadata_assessment"]
    assert assessment["source_inventory_status"] == ("declared_not_loaded" if legacy_paths else "not_loaded")
    assert assessment["source_presence"] == "unassessed_not_loaded"
    assert assessment["database_metadata_status"] == "not_assessed"
    row = report["products"]["SHFE.RB"]["contracts"][0]
    assert row["field_evidence"]["currency"]["status"] == "missing"
    assert row["field_interpretation"]["currency"]["category"] == "implementation_limitation"
    assert row["field_interpretation"]["multiplier"]["category"] == "observed_missing_bundle_field"
    assert row["field_interpretation"]["multiplier"]["source_presence"] == "unassessed_not_loaded"
    assert before == {path.name: path.read_bytes() for path in output.iterdir()}


def test_reference_economic_fields_follow_explicit_applicability_only(tmp_path):
    document = {"products": [{
        "exchange": "SHFE", "code": "RB", "research_contract": "SHFE.rb2609",
        "contract_size_tons": "10", "price_tick_cny_per_ton": "1",
        "fees": {"applicable_contracts": ["SHFE.rb2610"],
                 "components": {"close_today": {"value": "0.0001"}}},
        "margin": {"applicable_contracts": ["SHFE.rb2610"], "rate_of_notional": "0.07"},
    }]}
    _, bundle = export_with_source(tmp_path, json.dumps(document).encode(), "reference.json")
    presence = bundle.metadata["source_inventory"]["sources"][0]["contract_presence"]
    assert set(presence["SHFE.rb2609"]["semantic_fields"]) == {"multiplier", "price_tick"}
    assert set(presence["SHFE.rb2610"]["semantic_fields"]) == {"fees", "margin"}
    assert "fees.components.close_today.value" not in presence["SHFE.rb2609"]["fields"]
    assert "contract_size_tons" not in presence["SHFE.rb2610"]["fields"]
    report = inspect_readiness(bundle)
    rows = {row["raw_symbol"]: row for row in report["products"]["SHFE.RB"]["contracts"]}
    assert rows["SHFE.rb2609"]["field_evidence"]["fees"]["status"] == "missing"
    assert rows["SHFE.rb2610"]["field_evidence"]["fees"]["status"] == "present_reference_only"


@pytest.mark.parametrize("fault", ["missing", "directory", "unsupported", "bad_csv", "bad_json", "unknown_json", "too_large", "too_deep", "too_many_rows"])
def test_bad_or_over_limit_metadata_fails_before_connecting(tmp_path, fault):
    request = make_request(tmp_path)
    source = request.catalog_path.parent / "source.json"
    if fault == "directory":
        source.mkdir()
    elif fault == "unsupported":
        source = source.with_suffix(".txt")
        source.write_text("source", encoding="utf-8")
    elif fault == "bad_csv":
        source = source.with_suffix(".csv")
        source.write_text("symbol,multiplier\nSHFE.rb2609\n", encoding="utf-8")
    elif fault == "bad_json":
        source.write_text('{"password":"SECRET",', encoding="utf-8")
    elif fault == "unknown_json":
        source.write_text('{"password":"SECRET"}', encoding="utf-8")
    elif fault == "too_large":
        source.write_bytes(b" " * (8 * 1024 * 1024 + 1))
    elif fault == "too_deep":
        source.write_text("[" * 14 + "0" + "]" * 14, encoding="utf-8")
    elif fault == "too_many_rows":
        source = source.with_suffix(".csv")
        source.write_text("symbol,multiplier\n" + "SHFE.rb2609,10\n" * 100_001, encoding="utf-8")
    engine = FakeEngine()
    outcome = export_futures_bundle(engine, replace(request, local_metadata_paths=(source,)), tmp_path / "out")
    assert outcome.status in ("export_blocked", "scope_limit")
    assert engine.connect_count == 0
    assert not (tmp_path / "out").exists()
    assert "SECRET" not in repr(outcome)
