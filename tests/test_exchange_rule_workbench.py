import json
from datetime import date, datetime, timedelta
from html import unescape
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

import pytest

from ak_trading.exchange_rule_artifacts import (
    ExchangeRuleArtifactStore,
    build_catalog_collection_run,
)
from ak_trading.exchange_rule_collection import (
    collect_exchange_rules,
    collect_exchange_rules_from_catalog,
)
from ak_trading.exchange_rule_workbench import (
    WorkbenchError,
    WorkbenchRequestError,
    build_workbench_resource,
    build_workbench_state,
    make_workbench_handler,
    render_workbench_html,
)
from ak_trading.exchange_rules import RuleScope, RuleScopeKind, RuleStatus
from ak_trading.futures_catalog import load_futures_catalog
from ak_trading.market_reference import SHANGHAI_TZ


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "config" / "china_futures_catalog.json"
NOW = datetime(2026, 7, 28, 20, 30, tzinfo=SHANGHAI_TZ)


def _fixture_result(tmp_path: Path):
    store = ExchangeRuleArtifactStore(tmp_path / "exchange_rules")
    result = collect_exchange_rules(
        store=store,
        source_registry_path=ROOT / "config" / "exchange_rule_sources.json",
        reference_path=ROOT / "config" / "market_reference.json",
        now=NOW,
        trading_day=date(2026, 7, 29),
        fixture_paths={
            "SHFE": ROOT / "tests" / "fixtures_exchange_rules_shfe.json",
            "CZCE": ROOT / "tests" / "fixtures_exchange_rules_czce.json",
            "DCE": ROOT / "tests" / "fixtures_exchange_rules_dce.json",
        },
    )
    return store, result


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


def _catalog_result_with_scope_current(tmp_path: Path):
    store = ExchangeRuleArtifactStore(tmp_path / "catalog_exchange_rules")
    result = collect_exchange_rules_from_catalog(
        store=store,
        catalog_path=CATALOG,
        now=NOW,
        trading_day=date(2026, 7, 29),
        fixture_paths=_catalog_fixtures(tmp_path),
    )
    assert result.snapshot is not None
    assert result.catalog_run is not None
    candidate = next(
        item
        for item in result.snapshot.items
        if item.status is RuleStatus.CANDIDATE and item.exchange == "SHFE"
    )
    scope = RuleScope(RuleScopeKind.PRODUCT, candidate.exchange, candidate.product)
    record, _ = store.approve_scope_fields(
        result.snapshot,
        scope=scope,
        field_keys=(candidate.identity_key,),
        reviewer="test reviewer",
        reason="offline candidate field reviewed",
        decided_at=datetime(2026, 7, 28, 21, 0, tzinfo=SHANGHAI_TZ),
        catalog_id=str(result.catalog_run["catalog_id"]),
    )
    return store, result, candidate, record


def _workbench_get(
    store: ExchangeRuleArtifactStore,
    path: str,
) -> tuple[int, str, str]:
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        make_workbench_handler(store, catalog_path=CATALOG),
    )
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.daemon = True
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}{path}"
        try:
            with urlopen(url, timeout=5) as response:
                return (
                    response.status,
                    response.headers.get("Content-Type", ""),
                    response.read().decode("utf-8"),
                )
        except HTTPError as exc:
            return (
                exc.code,
                exc.headers.get("Content-Type", ""),
                exc.read().decode("utf-8"),
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_workbench_legacy_state_is_compact_and_rules_are_paginated(tmp_path: Path):
    store, result = _fixture_result(tmp_path)
    state = build_workbench_state(store)

    assert result.snapshot is not None
    assert state["read_only"] is True
    assert state["current"] is None
    assert state["candidate"]["snapshot_id"] == result.snapshot.snapshot_id
    assert "items" not in state
    assert "documents" not in state
    assert state["catalog"]["latest_run"] is None

    rules = build_workbench_resource(store, "rules", {"limit": ["10"]})
    assert rules["total"] == 27
    assert len(rules["items"]) == 10
    assert {item["exchange"] for item in rules["items"]} <= {"SHFE", "CZCE", "DCE"}

    documents = build_workbench_resource(store, "documents", {"limit": ["10"]})
    assert documents["total"] == 3
    assert all("content" not in document for document in documents["items"])


def test_workbench_catalog_state_shows_coverage_and_scoped_provenance(tmp_path: Path):
    store, result, candidate, record = _catalog_result_with_scope_current(tmp_path)
    state = build_workbench_state(store)

    assert result.catalog_run is not None
    assert state["catalog"]["latest_run"]["expected_futures"] == 81
    assert state["catalog"]["latest_run"]["processed_futures"] == 81
    assert state["catalog"]["latest_run"]["excluded_options"] == 6
    assert {row["exchange"] for row in state["catalog"]["coverage"]} == {
        "SHFE",
        "INE",
        "DCE",
        "CZCE",
        "CFFEX",
        "GFEX",
    }
    assert sum(row["expected_futures"] for row in state["catalog"]["coverage"]) == 81
    assert sum(row["processed_futures"] for row in state["catalog"]["coverage"]) == 81
    assert sum(row["excluded_options"] for row in state["catalog"]["coverage"]) == 6
    assert state["scope"]["current_pointer_count"] == 1
    assert state["scope"]["current_field_count"] == 1
    assert state["scope"]["approval_count"] == 1
    assert state["scope"]["review_queue_count"] == 81
    assert "expected_products" not in state["catalog"]["coverage"][0]

    products = build_workbench_resource(
        store,
        "catalog-products",
        {"offset": ["17"], "limit": ["20"]},
    )
    assert products["total"] == 81
    assert products["offset"] == 17
    assert len(products["items"]) == 20
    assert {item["coverage"] for item in products["items"]} == {"processed"}

    options = build_workbench_resource(store, "excluded-options", {"limit": ["10"]})
    assert options["total"] == 6
    assert all(item["instrument_kind"] == "option" for item in options["items"])
    assert all(item["status"] == "excluded_option" for item in options["items"])

    rules = build_workbench_resource(
        store,
        "rules",
        {
            "exchange": [candidate.exchange],
            "product": [candidate.product],
            "limit": ["100"],
        },
    )
    scoped = next(item for item in rules["items"] if item["key"] == "|".join((
        candidate.exchange,
        candidate.product,
        candidate.contract or "",
        candidate.category.value,
        candidate.field,
    )))
    assert scoped["scope_current"] is True
    assert scoped["scope_approvals"][0]["approval_record_id"] == record.record_id
    assert scoped["scope_approvals"][0]["catalog_id"] == result.catalog_run["catalog_id"]
    assert scoped["scope_approvals"][0]["value"] == "1"


def test_workbench_resources_reject_unbounded_or_invalid_requests(tmp_path: Path):
    store, _ = _fixture_result(tmp_path)

    with pytest.raises(WorkbenchRequestError, match="between 1 and 100"):
        build_workbench_resource(store, "rules", {"limit": ["101"]})
    with pytest.raises(WorkbenchRequestError, match="offset must be non-negative"):
        build_workbench_resource(store, "documents", {"offset": ["-1"]})
    with pytest.raises(WorkbenchRequestError, match="unsupported"):
        build_workbench_resource(store, "not-a-resource", {})
    with pytest.raises(WorkbenchRequestError, match="unsupported"):
        build_workbench_resource(store, "history", {"kind": ["not-a-kind"]})


def test_workbench_landing_page_shows_only_exchange_product_matrix(tmp_path: Path):
    store, _, candidate, _ = _catalog_result_with_scope_current(tmp_path)
    catalog = load_futures_catalog(CATALOG, require_all_exchanges=True)
    candidate_name = next(
        product.name_zh
        for product in catalog.future_products
        if product.identity == (candidate.exchange, candidate.product)
    )
    html = render_workbench_html(build_workbench_state(store))
    visible_html = unescape(html)

    assert "\u4ea4\u6613\u6240\u89c4\u5219\u5de5\u4f5c\u53f0" in visible_html
    for exchange in ("SHFE", "INE", "DCE", "CZCE", "CFFEX", "GFEX"):
        assert f'data-exchange="{exchange}"' in html
    assert "CU" in visible_html
    assert "\u94dc" in visible_html
    assert candidate_name in visible_html
    assert "\u5df2\u63d0\u53d6 1 \u9879" in visible_html or "\u65e0\u6570\u503c 8 \u9879" in visible_html
    assert "/product?exchange=SHFE&amp;product=CU" in html
    assert (
        f"/product?exchange={candidate.exchange}&amp;product={candidate.product}"
        in html
    )
    assert "<table" not in html
    assert "<script" not in html
    assert "Rule fields" not in html
    assert "Official source evidence" not in html
    assert "approve_scope_fields" not in html
    assert "reject_scope_fields" not in html
    assert "Offline official-format fixture." not in html

    status, content_type, served_html = _workbench_get(store, "/")
    assert status == 200
    assert content_type == "text/html; charset=utf-8"
    assert "\u94dc" in unescape(served_html)


def test_workbench_product_detail_shows_nine_fields_and_documents(tmp_path: Path):
    store, result, candidate, _ = _catalog_result_with_scope_current(tmp_path)
    assert result.catalog_run is not None
    catalog = load_futures_catalog(CATALOG, require_all_exchanges=True)
    candidate_name = next(
        product.name_zh
        for product in catalog.future_products
        if product.identity == (candidate.exchange, candidate.product)
    )
    run = result.catalog_run
    later = build_catalog_collection_run(
        target_trading_day=date.fromisoformat(run["target_trading_day"]),
        started_at=NOW + timedelta(hours=2),
        completed_at=NOW + timedelta(hours=2, minutes=1),
        catalog_id=run["catalog_id"],
        exchange_statuses=run["exchange_statuses"],
        expected_futures=(
            (item["exchange"], item["product"])
            for item in run["expected_futures"]
        ),
        processed_futures=(
            (item["exchange"], item["product"])
            for item in run["processed_futures"]
        ),
        closed_futures=(
            (item["exchange"], item["product"])
            for item in run["closed_futures"]
        ),
        excluded_options=run["excluded_options"],
        snapshot_id=run["snapshot_id"],
        errors=(
            f"{candidate.exchange}/{candidate.product}: isolated fixture failure",
            "DCE/M: unrelated fixture failure",
        ),
        review_reasons=(
            f"context {candidate.product}: manual fixture review",
            "DCE: unrelated exchange review",
        ),
        document_ids=run["document_ids"],
        previous_snapshot_id=run["previous_snapshot_id"],
    )
    store.store_catalog_run(later)

    query = urlencode(
        {"exchange": candidate.exchange, "product": candidate.product}
    )
    status, content_type, html = _workbench_get(store, f"/product?{query}")
    visible_html = unescape(html)

    assert status == 200
    assert content_type == "text/html; charset=utf-8"
    assert candidate.exchange in visible_html
    assert candidate.product in visible_html
    assert candidate_name in visible_html
    assert visible_html.count("<tr>") == 10  # one header plus nine canonical fields
    assert "fee / open" in visible_html
    assert "risk / risk_controls_and_forced_liquidation" in visible_html
    assert "\u672a\u83b7\u53d6\u5230\u6570\u503c" in visible_html
    assert f"SHFE {candidate.product} catalog fixture" in visible_html
    assert candidate.source_url in visible_html
    assert "\u83b7\u53d6\u65b9\u5f0f" in visible_html
    assert "\u672a\u8bb0\u5f55" in visible_html
    assert "\u5185\u5bb9\u957f\u5ea6" in visible_html
    assert " bytes" in visible_html
    assert "isolated fixture failure" in visible_html
    assert "manual fixture review" in visible_html
    assert "unrelated fixture failure" not in visible_html
    assert "unrelated exchange review" not in visible_html
    assert "<button" not in visible_html.lower()
    assert "<form" not in visible_html.lower()
    assert "approve_scope_fields" not in visible_html
    assert "reject_scope_fields" not in visible_html


def test_workbench_product_detail_preserves_explicit_zero_but_not_unknown(
    tmp_path: Path,
):
    store, _ = _fixture_result(tmp_path)
    query = urlencode({"exchange": "CZCE", "product": "TA"})
    status, _, html = _workbench_get(store, f"/product?{query}")
    visible_html = unescape(html)

    assert status == 200
    close_today_row = visible_html.split("fee / close_today", 1)[1].split(
        "</tr>", 1
    )[0]
    lock_margin_row = visible_html.split("lock_margin / same_contract", 1)[1].split(
        "</tr>", 1
    )[0]
    assert '<span class="rule-value">0</span>' in close_today_row
    assert '<span class="rule-value">\u672a\u83b7\u53d6\u5230\u6570\u503c</span>' in lock_margin_row
    assert '<span class="rule-value">0</span>' not in lock_margin_row


def test_workbench_product_detail_returns_clean_404_for_unknown_product(
    tmp_path: Path,
):
    store, _, _, _ = _catalog_result_with_scope_current(tmp_path)

    status, content_type, body = _workbench_get(
        store, "/product?exchange=SHFE&product=NOPE"
    )
    assert status == 404
    assert content_type == "text/plain; charset=utf-8"
    assert body == "product not found\n"

    malformed_status, _, malformed_body = _workbench_get(
        store, "/product?exchange=SHFE"
    )
    assert malformed_status == 400
    assert "exchange and product are required" in malformed_body


def test_workbench_request_error_response_carries_no_raw_markup(tmp_path: Path):
    store, _, _, _ = _catalog_result_with_scope_current(tmp_path)

    status, content_type, body = _workbench_get(
        store,
        "/product?exchange=%3Cscript%3Ealert%281%29%3C%2Fscript%3E&product=X",
    )

    assert status == 400
    assert content_type == "text/plain; charset=utf-8"
    assert "<script>" not in body


def test_workbench_rejects_non_local_bind():
    handler = make_workbench_handler(ExchangeRuleArtifactStore("data/exchange_rules"))
    assert handler.server_version.startswith("AKExchangeRulesWorkbench/")
    try:
        from ak_trading.exchange_rule_workbench import serve_workbench

        serve_workbench("data/exchange_rules", host="0.0.0.0", port=8765)
    except WorkbenchError as exc:
        assert "local-only" in str(exc)
