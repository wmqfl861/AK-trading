"""The archived research catalog remains intact and cannot become a calculator."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sqlite3

import pytest

from ak_trading.factor_library import (
    DEFAULT_REGISTRY, ROOT, FactorLibrary, FactorLibraryError,
)


@pytest.fixture(scope="module")
def library():
    return FactorLibrary()


def test_package_integrity_counts_and_status(library):
    result = library.verify()
    assert result["validation"] == "structure_and_integrity_pass"
    assert result["source_files_verified"] == 19
    assert result["counts"]["factors"] == 605
    assert result["counts"]["products"] == 91
    assert result["counts"]["product_factor_map"] == 18153
    assert result["counts"]["raw_observations"] == 0
    assert result["counts"]["factor_values"] == 0
    assert result["calculator_status"] == "not_integrated"
    assert result["profitability_validated"] is False


def test_one_factor_preserves_source_definition(library):
    result = library.factor("mom001")
    factor = result["factor"]
    assert factor["factor_id"] == "MOM001"
    assert factor["formula"] == "sum(r,n)"
    assert "未回测" in factor["backtest"]
    assert "同一合约" in factor["caveats"]
    assert len(factor) == 23
    assert {row["source_id"] for row in result["sources"]} == set(factor["source_ids"].split(","))


def test_product_filter_retains_conditional_applicability(library):
    result = library.search(product="shfe.cu", family="库存", limit=20)
    assert result["row_count"] == 12
    assert not result["truncated"]
    assert {row["product_id"] for row in result["rows"]} == {"SHFE.CU"}
    assert all(row["condition"] for row in result["rows"])
    assert all(row["factor_id"].startswith("INV") for row in result["rows"])
    assert library.search(product="CFFEX.T", family="库存")["row_count"] == 0


def test_no_product_filter_does_not_multiply_factor_rows(library):
    result = library.search(limit=1000)
    assert result["row_count"] == 605
    assert len({row["factor_id"] for row in result["rows"]}) == 605
    assert not result["truncated"]


def test_pagination_and_literal_text_filters(library):
    assert library.search(limit=2)["truncated"]
    assert library.products(limit=91)["truncated"] is False
    assert library.products(limit=90)["truncated"] is True
    assert library.search(keyword="MOM001")["row_count"] == 1
    assert library.search(keyword="% OR 1=1 --")["row_count"] == 0
    assert library.search(family="%")["row_count"] == 0


def test_monthly_average_is_research_id(library):
    rows = library.products()["rows"]
    monthly = [row for row in rows if row["product_id"].endswith("_AVG")]
    assert len(monthly) == 3
    assert library.search(product="DCE.L_AVG", family="月均价")["row_count"] == 8


def test_starters_intersect_product_applicability(library):
    result = library.starters(product="DCE.M")
    ids = {row["factor_id"] for row in result["rows"]}
    assert "MOM001" in ids and "XSP015" in ids
    assert not {"BND003", "AVG004", "SHIP008"} & ids
    assert all(row["product_id"] == "DCE.M" and row["application"] and row["condition"]
               for row in result["rows"])
    spread = next(row for row in result["rows"] if row["factor_id"] == "XSP015")
    assert "单腿不独立获得组合信号" in spread["condition"]
    assert library.starters()["row_count"] == 30


@pytest.mark.parametrize("kwargs", [
    {"product": "SHFE.rb2610"}, {"product": "DCE.M'; DELETE FROM factors;--"},
    {"priority": "HIGH"}, {"limit": 1001}, {"limit": 0}, {"limit": True},
    {"keyword": "x" * 121}, {"family": "库存\n"},
])
def test_bad_filters_rejected(library, kwargs):
    with pytest.raises(FactorLibraryError):
        library.search(**kwargs)


def test_unknown_factor_rejected(library):
    with pytest.raises(FactorLibraryError, match="unknown factor"):
        library.factor("NOT_A_FACTOR")


def test_read_only_connection_rejects_writes(library):
    with library._connection() as conn:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        assert conn.execute("PRAGMA trusted_schema").fetchone()[0] == 0
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("UPDATE factors SET name=name WHERE 0")


def test_tampered_copy_fails_checksum_without_touching_source(tmp_path):
    registry = json.loads(DEFAULT_REGISTRY.read_text(encoding="utf-8"))
    entry = registry["catalogs"][0]
    for key in ("archive_path", "sqlite_path", "checksums_path"):
        source = ROOT / entry[key]
        target = tmp_path / source.name
        shutil.copyfile(source, target)
        entry[key] = target.name
    db = tmp_path / entry["sqlite_path"]
    with db.open("ab") as handle:
        handle.write(b"tampered")
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(registry), encoding="utf-8")
    with pytest.raises(FactorLibraryError, match="integrity mismatch"):
        FactorLibrary(path, project_root=tmp_path)


def test_registry_cannot_escape_project(tmp_path):
    registry = json.loads(DEFAULT_REGISTRY.read_text(encoding="utf-8"))
    registry["catalogs"][0]["archive_path"] = "../outside.zip"
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(registry), encoding="utf-8")
    with pytest.raises(FactorLibraryError, match="leaves"):
        FactorLibrary(path, project_root=tmp_path)


def test_existing_calculators_are_unchanged():
    from ak_trading.probability.factors import factor_registry

    registered = factor_registry()
    assert len(registered) == 14
    assert {factor.name for factor in registered} >= {"er20", "atr14", "zscore20", "volume_ratio20"}
    assert all(factor.version == "1" for factor in registered)
