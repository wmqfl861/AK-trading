from __future__ import annotations

import json
import subprocess
from datetime import date, datetime
from pathlib import Path

import pytest

import ak_trading.exchange_rule_collection as collection_module
from ak_trading.exchange_rule_artifacts import ExchangeRuleArtifactStore
from ak_trading.exchange_rule_collection import (
    collect_exchange_rules,
    collect_exchange_rules_from_catalog,
)
from ak_trading.exchange_rule_sources import discover_official_documents
from ak_trading.exchange_rules import FetchedDocument, RuleStatus, RuleValueKind
from ak_trading.futures_catalog import load_futures_catalog
from ak_trading.market_reference import SHANGHAI_TZ


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "config" / "china_futures_catalog.json"
REFERENCE = ROOT / "config" / "market_reference.json"
REGISTRY = ROOT / "config" / "exchange_rule_sources.json"
NOW = datetime(2026, 7, 28, 20, 30, tzinfo=SHANGHAI_TZ)


class _OfflineDocumentFetcher:
    def __init__(self, *, now, **kwargs):
        self.now = now

    def fetch(self, url: str) -> FetchedDocument:
        return FetchedDocument(
            url=url,
            title="Offline official evidence",
            content=b"Official rules require manual extraction.",
            content_type="text/plain",
            retrieved_at=self.now(),
        )


def _runner_backed_discovery(
    failed_pairs: set[tuple[str, str]],
    calls: list[tuple[str, str]],
):
    def discover(source, **kwargs):
        products_by_query = {product.query: product for product in source.products}

        def runner(command, timeout):
            product = products_by_query[command[2]]
            identity = (source.code.upper(), product.code.upper())
            calls.append(identity)
            if identity in failed_pairs:
                return subprocess.CompletedProcess(
                    args=command,
                    returncode=1,
                    stdout="",
                    stderr="search failed",
                )
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout=json.dumps(
                    {
                        "success": True,
                        "data": {
                            "web": [
                                {
                                    "url": (
                                        f"https://{source.official_domains[0]}/"
                                        f"search-{product.code.lower()}.html"
                                    ),
                                    "title": f"{source.code} {product.code}",
                                }
                            ]
                        },
                    }
                ),
                stderr="",
            )

        return discover_official_documents(
            source,
            executable="websearch",
            timeout=kwargs.get("timeout", 45.0),
            runner=runner,
        )

    return discover


def _catalog_fixtures(tmp_path: Path) -> dict[str, Path]:
    catalog = load_futures_catalog(CATALOG, require_all_exchanges=True)
    fixtures: dict[str, Path] = {}
    for source in catalog.sources:
        product = catalog.products_for_exchange(source.exchange)[0]
        path = tmp_path / f"{source.exchange.lower()}.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "documents": [
                        {
                            "product": product.code,
                            "contract": None,
                            "url": product.source_url,
                            "title": f"{source.exchange} {product.code} catalog fixture",
                            "content_type": "application/json",
                            "content": {
                                "rules": [
                                    {
                                        "product": product.code,
                                        "contract": None,
                                        "category": "fee",
                                        "field": "open",
                                        "value": "1",
                                        "unit": "cny_per_lot",
                                        "status": "candidate",
                                        "excerpt": "Offline official-format fixture.",
                                    }
                                ]
                            },
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        fixtures[source.exchange] = path
    return fixtures


def test_catalog_collection_covers_all_futures_and_excludes_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    def unexpected_discovery(*args, **kwargs):
        raise AssertionError("fixture collection must not run official discovery")

    monkeypatch.setattr(
        collection_module,
        "discover_official_documents",
        unexpected_discovery,
    )
    store = ExchangeRuleArtifactStore(tmp_path / "exchange_rules")
    result = collect_exchange_rules_from_catalog(
        store=store,
        catalog_path=CATALOG,
        now=NOW,
        trading_day=date(2026, 7, 29),
        fixture_paths=_catalog_fixtures(tmp_path),
    )

    assert result.snapshot is not None
    assert result.snapshot.status is RuleStatus.REVIEW_REQUIRED
    assert result.run["status"] == "review_required"
    assert result.catalog_run is not None
    assert result.catalog_run["status"] == "review_required"
    assert len(result.catalog_run["expected_futures"]) == 81
    assert len(result.catalog_run["processed_futures"]) == 81
    assert result.catalog_run["closed_futures"] == []
    assert result.catalog_run["unresolved_futures"] == []
    assert len(result.catalog_run["excluded_options"]) == 6
    assert set(result.catalog_run["exchange_statuses"]) == {
        "SHFE", "INE", "DCE", "CZCE", "CFFEX", "GFEX"
    }
    assert set(result.catalog_run["exchange_statuses"].values()) == {"review_required"}
    assert {item.exchange for item in result.snapshot.items} == {
        "SHFE", "INE", "DCE", "CZCE", "CFFEX", "GFEX"
    }
    assert all(item.contract is None for item in result.snapshot.items)
    assert store.current_snapshot_id() is None


def test_catalog_collection_keeps_other_products_after_one_search_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        collection_module,
        "discover_official_documents",
        _runner_backed_discovery({("SHFE", "AU")}, calls),
    )
    monkeypatch.setattr(
        collection_module,
        "LocalWebSearchDocumentFetcher",
        _OfflineDocumentFetcher,
    )

    result = collect_exchange_rules_from_catalog(
        store=ExchangeRuleArtifactStore(tmp_path / "exchange_rules"),
        catalog_path=CATALOG,
        now=NOW,
        trading_day=date(2026, 7, 29),
        websearch_executable="websearch",
    )

    assert result.snapshot is not None
    assert result.catalog_run is not None
    assert result.catalog_run["exchange_statuses"]["SHFE"] == "partial"
    assert result.catalog_run["status"] == "review_required"
    assert len(result.catalog_run["processed_futures"]) == 81
    assert result.catalog_run["unresolved_futures"] == []
    assert result.catalog_run["errors"] == [
        "official search exited 1 for SHFE/AU"
    ]
    assert result.run["errors"] == result.catalog_run["errors"]
    shfe_calls = [product for exchange, product in calls if exchange == "SHFE"]
    au_index = shfe_calls.index("AU")
    assert au_index < len(shfe_calls) - 1
    following_product = shfe_calls[au_index + 1]
    assert any(
        document.url.endswith(f"/search-{following_product.lower()}.html")
        for document in result.snapshot.documents
    )
    assert any(
        document.url.endswith("/products/futures/metal/preciousmetal/au_f/")
        for document in result.snapshot.documents
    )


def test_catalog_collection_all_searches_failed_is_partial_with_unknown_items(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    def all_failed(source, **kwargs):
        return (), tuple(
            f"official search exited 1 for {source.code}/{product.code}"
            for product in source.products
        )

    monkeypatch.setattr(
        collection_module,
        "discover_official_documents",
        all_failed,
    )
    monkeypatch.setattr(
        collection_module,
        "LocalWebSearchDocumentFetcher",
        _OfflineDocumentFetcher,
    )
    # The SHFE/INE daily settlement-parameter fast lane is a configured
    # structured source independent of search; this test pins the total
    # discovery-failure shape, so the direct file fetch is stubbed out too.
    monkeypatch.setattr(
        collection_module,
        "fetch_exchange_daily_parameters",
        lambda *args, **kwargs: None,
    )

    result = collect_exchange_rules_from_catalog(
        store=ExchangeRuleArtifactStore(tmp_path / "exchange_rules"),
        catalog_path=CATALOG,
        now=NOW,
        trading_day=date(2026, 7, 29),
        websearch_executable="websearch",
    )

    assert result.snapshot is not None
    assert result.catalog_run is not None
    assert result.snapshot.documents == ()
    assert result.snapshot.status is RuleStatus.REVIEW_REQUIRED
    assert result.snapshot.items
    assert all(item.status is RuleStatus.REVIEW_REQUIRED for item in result.snapshot.items)
    assert all(item.value_kind is RuleValueKind.UNKNOWN for item in result.snapshot.items)
    assert set(result.catalog_run["exchange_statuses"].values()) == {"partial"}
    assert len(result.catalog_run["processed_futures"]) == 81
    assert result.catalog_run["unresolved_futures"] == []
    assert len(result.catalog_run["errors"]) == 81
    assert result.catalog_run["status"] == "review_required"
    assert result.run["errors"] == result.catalog_run["errors"]


def test_legacy_three_product_collection_consumes_isolated_search_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        collection_module,
        "discover_official_documents",
        _runner_backed_discovery({("CZCE", "TA")}, calls),
    )
    monkeypatch.setattr(
        collection_module,
        "LocalWebSearchDocumentFetcher",
        _OfflineDocumentFetcher,
    )

    result = collect_exchange_rules(
        store=ExchangeRuleArtifactStore(tmp_path / "exchange_rules"),
        source_registry_path=REGISTRY,
        reference_path=REFERENCE,
        now=NOW,
        trading_day=date(2026, 7, 29),
        websearch_executable="websearch",
    )

    assert calls == [("SHFE", "RB"), ("CZCE", "TA"), ("DCE", "M")]
    assert result.snapshot is not None
    assert result.snapshot.status is RuleStatus.REVIEW_REQUIRED
    assert {item.product for item in result.snapshot.items} == {"RB", "TA", "M"}
    assert result.run["errors"] == ["official search exited 1 for CZCE/TA"]
    assert any(
        document.url.endswith("/search-m.html")
        for document in result.snapshot.documents
    )


def test_catalog_collection_marks_all_exchanges_closed_without_snapshot(tmp_path: Path):
    store = ExchangeRuleArtifactStore(tmp_path / "exchange_rules")
    result = collect_exchange_rules_from_catalog(
        store=store,
        catalog_path=CATALOG,
        now=datetime(2026, 9, 30, 20, 30, tzinfo=SHANGHAI_TZ),
        trading_day=date(2026, 10, 1),
        fixture_paths=_catalog_fixtures(tmp_path),
    )

    assert result.snapshot is None
    assert result.run["status"] == "skipped_closed_day"
    assert result.catalog_run is not None
    assert result.catalog_run["status"] == "skipped_closed_day"
    assert len(result.catalog_run["closed_futures"]) == 81
    assert result.catalog_run["processed_futures"] == []
    assert result.catalog_run["unresolved_futures"] == []
    assert set(result.catalog_run["exchange_statuses"].values()) == {"closed"}
