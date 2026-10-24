import json
from datetime import datetime, timezone
from pathlib import Path
import subprocess

import pytest

from ak_trading.exchange_rule_sources import (
    DomainNotAllowedError,
    ExchangeRuleSourceError,
    ExchangeSource,
    LocalWebSearchDocumentFetcher,
    ProductSource,
    collect_evidence,
    discover_official_documents,
    is_official_url,
    parse_structured_rule_document,
)
from ak_trading.exchange_rules import (
    FetchedDocument,
    RuleCategory,
    RuleStatus,
    RuleValueKind,
    SourceDocument,
)


NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
SOURCE = ExchangeSource(
    code="SHFE",
    official_domains=("shfe.com.cn",),
    products=(
        ProductSource(
            code="RB",
            contract="SHFE.rb2610",
            query="site:shfe.com.cn rb rules",
        ),
    ),
)


def _completed(payload: object, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["websearch"],
        returncode=returncode,
        stdout=json.dumps(payload),
        stderr="",
    )


def _source_document(content: bytes, content_type: str = "application/json") -> SourceDocument:
    fetched = FetchedDocument(
        url="https://www.shfe.com.cn/rules.json",
        title="SHFE rules",
        content=content,
        content_type=content_type,
        retrieved_at=NOW,
    )
    return SourceDocument.from_fetched(
        fetched,
        exchange="SHFE",
        content_path="documents/aa/evidence.json",
    )


def test_official_url_allowlist_rejects_lookalikes_and_credentials():
    assert is_official_url("https://www.shfe.com.cn/rules", SOURCE.official_domains)
    assert not is_official_url("https://shfe.com.cn.attacker.example/rules", SOURCE.official_domains)
    assert not is_official_url("https://user:pass@shfe.com.cn/rules", SOURCE.official_domains)
    with pytest.raises(DomainNotAllowedError):
        discover_official_documents(
            ExchangeSource(
                code="SHFE",
                official_domains=("shfe.com.cn",),
                products=(
                    ProductSource(
                        code="RB",
                        contract="SHFE.rb2610",
                        query="rules",
                        seed_urls=("https://attacker.example/rules",),
                    ),
                ),
            ),
            executable="websearch",
            runner=lambda command, timeout: _completed({"success": True, "data": {}}),
        )


def test_discovery_uses_search_for_urls_and_discards_non_official_results():
    calls = []

    def runner(command, timeout):
        calls.append((command, timeout))
        return _completed(
            {
                "success": True,
                "data": {
                    "web": [
                        {"url": "https://www.shfe.com.cn/official.html", "title": "Official"},
                        {"url": "https://search.example/result", "title": "Untrusted"},
                    ]
                },
            }
        )

    documents, errors = discover_official_documents(
        SOURCE,
        executable="websearch",
        timeout=4,
        limit=5,
        runner=runner,
    )

    assert [document.url for document in documents] == [
        "https://www.shfe.com.cn/official.html"
    ]
    assert errors == ()
    assert calls[0][0] == ["websearch", "search", "site:shfe.com.cn rb rules", "--limit", "5"]


def test_discovery_isolates_nonzero_failure_and_preserves_seed_before_continuing():
    source = ExchangeSource(
        code="SHFE",
        official_domains=("shfe.com.cn",),
        products=(
            ProductSource(code="RB", contract=None, query="first"),
            ProductSource(
                code="AU",
                contract=None,
                query="failing",
                seed_urls=("https://www.shfe.com.cn/seed-au.html",),
            ),
            ProductSource(code="AG", contract=None, query="last"),
        ),
    )
    calls = []

    def runner(command, timeout):
        calls.append(command[2])
        if command[2] == "failing":
            return _completed({"success": False}, returncode=7)
        return _completed(
            {
                "success": True,
                "data": {
                    "web": [
                        {
                            "url": f"https://www.shfe.com.cn/{command[2]}.html",
                            "title": command[2],
                        }
                    ]
                },
            }
        )

    documents, errors = discover_official_documents(
        source,
        executable="websearch",
        runner=runner,
    )

    assert calls == ["first", "failing", "last"]
    assert [document.url for document in documents] == [
        "https://www.shfe.com.cn/first.html",
        "https://www.shfe.com.cn/seed-au.html",
        "https://www.shfe.com.cn/last.html",
    ]
    assert errors == ("official search exited 7 for SHFE/AU",)


@pytest.mark.parametrize(
    ("failure", "expected_error"),
    (
        ("timeout", "official search timed out for SHFE/RB"),
        ("oserror", "official search could not start for SHFE/RB"),
        ("invalid_json", "websearch returned invalid JSON for SHFE/RB"),
        (
            "not_success",
            "websearch response did not report success for SHFE/RB",
        ),
    ),
)
def test_discovery_isolates_other_runtime_failures(
    failure: str,
    expected_error: str,
):
    source = ExchangeSource(
        code="SHFE",
        official_domains=("shfe.com.cn",),
        products=(
            ProductSource(code="RB", contract=None, query="failing"),
            ProductSource(code="AG", contract=None, query="succeeding"),
        ),
    )
    calls = []

    def runner(command, timeout):
        calls.append(command[2])
        if command[2] == "succeeding":
            return _completed(
                {
                    "success": True,
                    "data": {
                        "web": [
                            {
                                "url": "https://www.shfe.com.cn/ag.html",
                                "title": "AG",
                            }
                        ]
                    },
                }
            )
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, timeout)
        if failure == "oserror":
            raise OSError("could not launch")
        if failure == "invalid_json":
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout="not-json",
                stderr="",
            )
        return _completed({"success": False, "data": {}})

    documents, errors = discover_official_documents(
        source,
        executable="websearch",
        runner=runner,
    )

    assert calls == ["failing", "succeeding"]
    assert [document.product for document in documents] == ["AG"]
    assert errors == (expected_error,)


def test_discovery_all_searches_failed_returns_empty_documents_and_all_errors():
    source = ExchangeSource(
        code="SHFE",
        official_domains=("shfe.com.cn",),
        products=(
            ProductSource(code="RB", contract=None, query="rb"),
            ProductSource(code="AU", contract=None, query="au"),
        ),
    )

    documents, errors = discover_official_documents(
        source,
        executable="websearch",
        runner=lambda command, timeout: _completed({}, returncode=1),
    )

    assert documents == ()
    assert errors == (
        "official search exited 1 for SHFE/RB",
        "official search exited 1 for SHFE/AU",
    )


def test_discovery_does_not_swallow_unexpected_runner_errors():
    def runner(command, timeout):
        raise RuntimeError("programmer error")

    with pytest.raises(RuntimeError, match="programmer error"):
        discover_official_documents(
            SOURCE,
            executable="websearch",
            runner=runner,
        )


def test_local_fetcher_retains_normalized_scrape_evidence_without_parsing_snippets():
    calls = []

    def runner(command, timeout):
        calls.append((command, timeout))
        return _completed(
            {
                "success": True,
                "data": {
                    "markdown": "# Official rules\nFee: review this document.",
                    "metadata": {
                        "sourceURL": "https://www.shfe.com.cn/rules.html",
                        "title": "Official rules",
                        "statusCode": 200,
                    },
                    "fetcher": "scrapling",
                },
            }
        )

    document = LocalWebSearchDocumentFetcher(
        executable="websearch",
        timeout=8,
        runner=runner,
        now=lambda: NOW,
    ).fetch("https://www.shfe.com.cn/rules.html")

    assert calls[0][0] == ["websearch", "scrape", "https://www.shfe.com.cn/rules.html", "--stealth"]
    assert document.url == "https://www.shfe.com.cn/rules.html"
    assert document.content_type.endswith("scrape+json")
    assert b"Official rules" in document.content


def test_structured_parser_preserves_explicit_zero_and_rejects_unknown_status():
    document = _source_document(
        json.dumps(
            {
                "rules": [
                    {
                        "product": "RB",
                        "contract": "SHFE.rb2610",
                        "category": "fee",
                        "field": "close_today",
                        "value": "0",
                        "value_kind": "explicit_zero",
                        "unit": "cny_per_lot",
                        "status": "candidate",
                        "excerpt": "Close today is explicitly zero.",
                    }
                ]
            }
        ).encode()
    )
    items = parse_structured_rule_document(
        document,
        product="RB",
        contract="SHFE.rb2610",
    )
    assert len(items) == 1
    assert items[0].value_kind is RuleValueKind.EXPLICIT_ZERO
    assert items[0].status is RuleStatus.CANDIDATE
    assert items[0].category is RuleCategory.FEE

    invalid = _source_document(
        json.dumps(
            {
                "rules": [
                    {
                        "product": "RB",
                        "contract": "SHFE.rb2610",
                        "category": "fee",
                        "field": "open",
                        "value": "1",
                        "unit": "cny_per_lot",
                        "status": "current_approved",
                    }
                ]
            }
        ).encode()
    )
    with pytest.raises(ExchangeRuleSourceError, match="candidate or review-required"):
        parse_structured_rule_document(
            invalid,
            product="RB",
            contract="SHFE.rb2610",
        )


def test_collect_evidence_marks_missing_fields_unknown_and_conflicts_review_required():
    first = _source_document(
        json.dumps(
            {
                "rules": [
                    {
                        "product": "RB",
                        "contract": "SHFE.rb2610",
                        "category": "fee",
                        "field": "open",
                        "value": "1",
                        "unit": "cny_per_lot",
                        "status": "candidate",
                    }
                ]
            }
        ).encode()
    )
    second = SourceDocument.from_fetched(
        FetchedDocument(
            url="https://www.shfe.com.cn/other.json",
            title="SHFE other rules",
            content=json.dumps(
                {
                    "rules": [
                        {
                            "product": "RB",
                            "contract": "SHFE.rb2610",
                            "category": "fee",
                            "field": "open",
                            "value": "2",
                            "unit": "cny_per_lot",
                            "status": "candidate",
                        }
                    ]
                }
            ).encode(),
            content_type="application/json",
            retrieved_at=NOW,
        ),
        exchange="SHFE",
        content_path="documents/bb/other.json",
    )
    fetched = {"https://www.shfe.com.cn/rules.json": FetchedDocument(
        url=first.url,
        title=first.title,
        content=first.raw_content,
        content_type=first.content_type,
        retrieved_at=NOW,
    ), "https://www.shfe.com.cn/other.json": FetchedDocument(
        url=second.url,
        title=second.title,
        content=second.raw_content,
        content_type=second.content_type,
        retrieved_at=NOW,
    )}
    discovered = tuple(
        type("Discovered", (), {
            "exchange": "SHFE",
            "product": "RB",
            "contract": "SHFE.rb2610",
            "url": url,
        })()
        for url in fetched
    )

    evidence = collect_evidence(
        SOURCE,
        discovered,
        fetcher=fetched.__getitem__,
        content_path_builder=lambda digest, suffix: f"documents/{digest[:2]}/{digest}{suffix}",
    )

    open_item = next(item for item in evidence.items if item.field == "open")
    assert open_item.status is RuleStatus.REVIEW_REQUIRED
    assert open_item.value_kind is RuleValueKind.UNKNOWN
    assert any("conflicting candidate values" in reason for reason in evidence.review_reasons)
    assert any(
        item.category is RuleCategory.LOCK_MARGIN and item.value_kind is RuleValueKind.UNKNOWN
        for item in evidence.items
    )
