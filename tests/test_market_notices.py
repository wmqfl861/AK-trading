import json
import subprocess
import threading
from datetime import datetime
from pathlib import Path

from ak_trading.market_notices import check_official_notices
from ak_trading.market_reference import SHANGHAI_TZ, load_reference_book


ROOT = Path(__file__).resolve().parents[1]


def _book():
    return load_reference_book(ROOT / "config" / "market_reference.json")


def _success_payload(domain: str, date_text: str = "2026-07-20") -> str:
    return json.dumps(
        {
            "success": True,
            "data": {
                "web": [
                    {
                        "title": "Official notice",
                        "url": f"https://www.{domain}/notice/1.html",
                        "description": f"Published {date_text}",
                    },
                    {
                        "title": "Untrusted mirror",
                        "url": "https://example.com/not-official",
                        "description": "2026-12-31",
                    },
                ]
            },
        }
    )


def test_three_official_checks_run_in_parallel_and_ignore_other_domains():
    book = _book()
    barrier = threading.Barrier(3)

    def runner(command: list[str], timeout: float):
        barrier.wait(timeout=2)
        query = command[2]
        domain = next(
            product.official_domain for product in book.products if product.notice_query == query
        )
        return subprocess.CompletedProcess(command, 0, _success_payload(domain), "")

    result = check_official_notices(
        book,
        checked_at=datetime(2026, 7, 28, 8, 0, tzinfo=SHANGHAI_TZ),
        executable="websearch-test",
        runner=runner,
    )

    assert result["status"] == "checked"
    assert [item["code"] for item in result["products"]] == ["RB", "TA", "M"]
    assert all(len(item["official_results"]) == 1 for item in result["products"])
    assert all("example.com" not in str(item) for item in result["products"])


def test_newer_dated_official_result_requires_review():
    book = _book()

    def runner(command: list[str], timeout: float):
        query = command[2]
        domain = next(
            product.official_domain for product in book.products if product.notice_query == query
        )
        return subprocess.CompletedProcess(
            command, 0, _success_payload(domain, "2026-07-29"), ""
        )

    result = check_official_notices(
        book,
        checked_at=datetime(2026, 7, 29, 8, 0, tzinfo=SHANGHAI_TZ),
        executable="websearch-test",
        runner=runner,
    )

    assert result["status"] == "review_required"
    assert all(item["newer_dated_results"] for item in result["products"])


def test_newer_full_month_official_result_requires_review():
    book = _book()

    def runner(command: list[str], timeout: float):
        product = next(
            product for product in book.products if product.notice_query == command[2]
        )
        return subprocess.CompletedProcess(
            command,
            0,
            _success_payload(product.official_domain, "August 3, 2026"),
            "",
        )

    result = check_official_notices(
        book,
        checked_at=datetime(2026, 7, 29, 8, 0, tzinfo=SHANGHAI_TZ),
        executable="websearch-test",
        runner=runner,
    )

    assert result["status"] == "review_required"
    assert all(item["newer_dated_results"] for item in result["products"])


def test_one_search_failure_is_not_reported_as_no_change():
    book = _book()

    def runner(command: list[str], timeout: float):
        if "PTA" in command[2]:
            return subprocess.CompletedProcess(command, 2, "", "failed")
        product = next(
            product for product in book.products if product.notice_query == command[2]
        )
        return subprocess.CompletedProcess(
            command, 0, _success_payload(product.official_domain), ""
        )

    result = check_official_notices(
        book,
        checked_at=datetime(2026, 7, 28, 8, 0, tzinfo=SHANGHAI_TZ),
        executable="websearch-test",
        runner=runner,
    )

    assert result["status"] == "error"
    ta = next(item for item in result["products"] if item["code"] == "TA")
    assert ta["status"] == "error"
    assert "code 2" in ta["error"]
    assert next(item for item in result["products"] if item["code"] == "RB")[
        "status"
    ] == "checked"


def test_missing_websearch_is_explicit_error(monkeypatch):
    monkeypatch.setattr("ak_trading.market_notices.shutil.which", lambda _: None)
    result = check_official_notices(
        _book(),
        checked_at=datetime(2026, 7, 28, 8, 0, tzinfo=SHANGHAI_TZ),
        executable=None,
        runner=lambda *_: (_ for _ in ()).throw(AssertionError("must not run")),
    )

    assert result["status"] == "error"
    assert result["error"] == "websearch executable not found"
