from __future__ import annotations

import json
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from ak_trading.market_reference import ProductSpec, ReferenceBook, SHANGHAI_TZ


_ISO_DATE = re.compile(r"\b(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})日?\b")
_MONTH_ALIASES = (
    ("January", "Jan"),
    ("February", "Feb"),
    ("March", "Mar"),
    ("April", "Apr"),
    ("May",),
    ("June", "Jun"),
    ("July", "Jul"),
    ("August", "Aug"),
    ("September", "Sep"),
    ("October", "Oct"),
    ("November", "Nov"),
    ("December", "Dec"),
)
_ENGLISH_DATE = re.compile(
    rf"\b({'|'.join(alias for aliases in _MONTH_ALIASES for alias in aliases)})\.?\s+"
    r"(\d{1,2}),\s+(20\d{2})\b",
    re.IGNORECASE,
)
_MONTHS = {
    month.lower(): index
    for index, aliases in enumerate(_MONTH_ALIASES, start=1)
    for month in aliases
}


def _result_date(value: str) -> date | None:
    match = _ISO_DATE.search(value)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    match = _ENGLISH_DATE.search(value)
    if match:
        try:
            return date(
                int(match.group(3)),
                _MONTHS[match.group(1).lower()],
                int(match.group(2)),
            )
        except ValueError:
            return None
    return None


def _is_official_url(url: str, domain: str) -> bool:
    hostname = (urlparse(url).hostname or "").lower()
    wanted = domain.lower().lstrip(".")
    return hostname == wanted or hostname.endswith(f".{wanted}")


def _default_runner(
    command: list[str], timeout: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _check_product(
    product: ProductSpec,
    executable: str,
    timeout: float,
    runner: Callable[[list[str], float], subprocess.CompletedProcess[str]],
) -> dict[str, Any]:
    command = [
        executable,
        "search",
        product.notice_query,
        "--limit",
        "10",
    ]
    try:
        completed = runner(command, timeout)
    except subprocess.TimeoutExpired:
        return {
            "code": product.code,
            "status": "error",
            "error": f"websearch timed out after {timeout:g}s",
            "official_results": [],
            "newer_dated_results": [],
        }
    except OSError as exc:
        return {
            "code": product.code,
            "status": "error",
            "error": f"websearch launch failed: {type(exc).__name__}",
            "official_results": [],
            "newer_dated_results": [],
        }
    if completed.returncode != 0:
        return {
            "code": product.code,
            "status": "error",
            "error": f"websearch exited with code {completed.returncode}",
            "official_results": [],
            "newer_dated_results": [],
        }
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {
            "code": product.code,
            "status": "error",
            "error": "websearch returned invalid JSON",
            "official_results": [],
            "newer_dated_results": [],
        }
    if not isinstance(payload, Mapping) or payload.get("success") is not True:
        return {
            "code": product.code,
            "status": "error",
            "error": "websearch response did not report success",
            "official_results": [],
            "newer_dated_results": [],
        }
    data = payload.get("data")
    raw_results = data.get("web", []) if isinstance(data, Mapping) else []
    if not isinstance(raw_results, list):
        raw_results = []
    official: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_results:
        if not isinstance(raw, Mapping):
            continue
        url = str(raw.get("url") or "").strip()
        if not url or url in seen or not _is_official_url(url, product.official_domain):
            continue
        seen.add(url)
        title = str(raw.get("title") or "").strip()
        description = str(raw.get("description") or "").strip()
        published = _result_date(f"{title} {description}")
        official.append(
            {
                "title": title,
                "url": url,
                "description": description[:500],
                "indexed_date": published.isoformat() if published else None,
            }
        )
    if not official:
        return {
            "code": product.code,
            "status": "error",
            "error": "no official-domain results were returned",
            "official_results": [],
            "newer_dated_results": [],
        }
    newer = [
        item
        for item in official
        if item["indexed_date"] is not None
        and date.fromisoformat(item["indexed_date"]) > product.last_reviewed_date
    ]
    return {
        "code": product.code,
        "status": "review_required" if newer else "checked",
        "error": None,
        "last_reviewed_date": product.last_reviewed_date.isoformat(),
        "official_results": official[:5],
        "newer_dated_results": newer,
        "caveat": "Search-index evidence is not proof that the exchange made no change.",
    }


def check_official_notices(
    book: ReferenceBook,
    *,
    checked_at: datetime,
    timeout: float = 45.0,
    executable: str | Path | None = None,
    runner: Callable[
        [list[str], float], subprocess.CompletedProcess[str]
    ] = _default_runner,
) -> dict[str, Any]:
    if checked_at.tzinfo is None:
        raise ValueError("checked_at must be timezone-aware")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError("timeout must be positive")
    resolved = str(executable) if executable is not None else shutil.which("websearch")
    if not resolved:
        return {
            "status": "error",
            "checked_at": checked_at.astimezone(SHANGHAI_TZ).isoformat(),
            "error": "websearch executable not found",
            "products": [],
        }
    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=len(book.products)) as executor:
        futures = {
            executor.submit(
                _check_product,
                product,
                resolved,
                float(timeout),
                runner,
            ): product.code
            for product in book.products
        }
        for future in as_completed(futures):
            code = futures[future]
            try:
                results[code] = future.result()
            except Exception as exc:
                results[code] = {
                    "code": code,
                    "status": "error",
                    "error": f"unexpected notice-check failure: {type(exc).__name__}",
                    "official_results": [],
                    "newer_dated_results": [],
                }
    ordered = [results[code] for code in book.expected_products]
    if any(item["status"] == "error" for item in ordered):
        status = "error"
    elif any(item["status"] == "review_required" for item in ordered):
        status = "review_required"
    else:
        status = "checked"
    return {
        "status": status,
        "checked_at": checked_at.astimezone(SHANGHAI_TZ).isoformat(),
        "method": "local_websearch_official_domain_index",
        "products": ordered,
        "caveat": "Numeric parameters are never auto-updated from search snippets.",
    }
