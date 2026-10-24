"""Bounded live probe for catalog-backed official exchange rule sources.

The probe deliberately does not persist anything.  It samples product URLs from
the futures catalog, sends one Firecrawl-compatible scrape request per URL to
the local web-search stack, and emits one ASCII-safe JSON document on stdout.
"""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


STACK_URL = "http://127.0.0.1:3002/v1/scrape"
STACK_API_KEY = "fc-local"
DEFAULT_TIMEOUT = 45.0


def _text_length(value: object) -> int:
    return len(value.strip()) if isinstance(value, str) else 0


def _first_text(mapping: Mapping[str, Any], names: tuple[str, ...]) -> str:
    for name in names:
        value = mapping.get(name)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _first_text_or_formats(mapping: Mapping[str, Any], names: tuple[str, ...]) -> str:
    value = _first_text(mapping, names)
    if value:
        return value
    for name in names:
        candidate = mapping.get(name)
        if isinstance(candidate, (list, tuple)):
            values = [item.strip() for item in candidate if isinstance(item, str) and item.strip()]
            if values:
                return ",".join(values)
    return ""


def analyze_scrape_payload(payload: object) -> dict[str, Any]:
    """Analyze a decoded scrape response without performing I/O.

    ``normalized_content_length`` is based on Markdown (the fetcher's retained
    normalized representation).  HTML/raw fields are reported separately so a
    response with useful raw content but an empty normalized body remains
    visible as ``empty-normalized`` rather than being treated as successful.
    The function also accepts raw JSON text/bytes to make malformed responses
    testable without involving the network layer.
    """

    if isinstance(payload, (str, bytes, bytearray)):
        try:
            payload = json.loads(payload)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
            return {
                "status": "error",
                "error": "malformed-json",
                "normalized_content_length": 0,
                "raw_content_length": 0,
                "charset": None,
                "format": None,
                "flags": {},
            }
    if not isinstance(payload, Mapping):
        return {
            "status": "error",
            "error": "response-not-object",
            "normalized_content_length": 0,
            "raw_content_length": 0,
            "charset": None,
            "format": None,
            "flags": {},
        }
    if "success" in payload and payload.get("success") is not True:
        return {
            "status": "error",
            "error": "response-not-success",
            "normalized_content_length": 0,
            "raw_content_length": 0,
            "charset": None,
            "format": None,
            "flags": {},
        }

    data = payload.get("data")
    if not isinstance(data, Mapping):
        data = payload
    metadata = data.get("metadata")
    if not isinstance(metadata, Mapping):
        metadata = {}

    normalized = _first_text(data, ("markdown", "normalized", "normalized_content"))
    raw = _first_text(
        data,
        ("raw_content", "rawContent", "raw", "raw_html", "rawHtml", "html", "content"),
    )
    charset = _first_text(
        metadata,
        ("charset", "encoding", "contentCharset", "content_charset"),
    ) or _first_text(data, ("charset", "encoding"))
    content_type = _first_text(
        metadata,
        ("contentType", "content_type", "mimeType", "mime_type"),
    ) or _first_text(data, ("contentType", "content_type", "mimeType", "mime_type"))
    response_format = _first_text_or_formats(metadata, ("format", "formats")) or _first_text_or_formats(
        data, ("format", "formats")
    )
    fetcher = _first_text(metadata, ("fetcher",)) or _first_text(data, ("fetcher",))
    cached = metadata.get("cached") if "cached" in metadata else data.get("cached")
    fetch_errors = data.get("fetchErrors", data.get("fetch_errors", []))
    if not isinstance(fetch_errors, list):
        fetch_errors = []

    normalized_length = _text_length(normalized)
    raw_length = _text_length(raw)
    status = "ok" if normalized_length else "empty-normalized"
    result: dict[str, Any] = {
        "status": status,
        "error": None,
        "normalized_content_length": normalized_length,
        "raw_content_length": raw_length,
        "charset": charset or None,
        "format": response_format or None,
        "flags": {
            "has_normalized_content": bool(normalized_length),
            "has_raw_content": bool(raw_length),
            "content_type": content_type or None,
            "fetcher": fetcher or None,
            "cached": cached if isinstance(cached, bool) else None,
            "fetch_errors": [str(item)[:200] for item in fetch_errors[:20]],
        },
    }
    status_code = metadata.get("statusCode", metadata.get("status_code"))
    if isinstance(status_code, (int, float)) and not isinstance(status_code, bool):
        result["source_http_status"] = int(status_code)
    return result


def _request_scrape(url: str, timeout: float) -> tuple[int | None, object | None, str | None]:
    request = Request(
        STACK_URL,
        data=json.dumps(
            {"url": url, "formats": ["markdown", "html"], "stealth": True},
            separators=(",", ":"),
        ).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {STACK_API_KEY}",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed loopback URL
            status = getattr(response, "status", None)
            body = response.read()
    except HTTPError as exc:
        return exc.code, None, None
    except (TimeoutError, URLError, OSError) as exc:
        return None, None, f"transport-{type(exc).__name__}"
    try:
        return status if isinstance(status, int) else None, json.loads(body), None
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return status if isinstance(status, int) else None, body, None


def _probe_one(exchange: str, product: str, url: str, timeout: float) -> dict[str, Any]:
    result: dict[str, Any] = {
        "exchange": exchange,
        "product": product,
        "url": url,
        "http_status": None,
        "transport_error": None,
        "normalized_content_length": 0,
        "raw_content_length": 0,
        "charset": None,
        "format": None,
        "flags": {},
    }
    try:
        http_status, payload, transport_error = _request_scrape(url, timeout)
    except Exception as exc:
        result["status"] = "error"
        result["error"] = f"transport-{type(exc).__name__}"
        result["transport_error"] = result["error"]
        return result
    result["http_status"] = http_status
    result["transport_error"] = transport_error
    if transport_error is not None:
        result["status"] = "error"
        result["error"] = transport_error
        return result
    if payload is None and http_status is not None and http_status >= 400:
        result["status"] = "error"
        result["error"] = f"http-{http_status}"
        return result
    analysis = analyze_scrape_payload(payload)
    result.update(analysis)
    if http_status is not None:
        result["http_status"] = http_status
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Probe a bounded sample of official futures exchange rule URLs."
    )
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--sample-per-exchange", type=int, default=2)
    parser.add_argument("--exchange", action="append", nargs="+", dest="exchanges")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    return parser


def _sample_products(catalog_path: Path, requested: list[str] | None, sample: int):
    from ak_trading.futures_catalog import load_futures_catalog

    catalog = load_futures_catalog(catalog_path, require_all_exchanges=False)
    wanted = [item.upper() for item in requested] if requested else list(catalog.exchanges)
    unknown = sorted(set(wanted) - set(catalog.exchanges))
    if unknown:
        raise ValueError(f"unknown exchange: {unknown[0]}")
    return [
        (exchange, product.code, product.source_url)
        for exchange in wanted
        for product in catalog.products_for_exchange(exchange)[:sample]
    ], wanted


def run(arguments: list[str] | None = None) -> tuple[int, dict[str, Any]]:
    parser = _parser()
    args = parser.parse_args(arguments)
    if not 1 <= args.sample_per_exchange <= 10:
        parser.error("--sample-per-exchange must be between 1 and 10")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be positive")
    requested = [exchange for group in (args.exchanges or []) for exchange in group]
    try:
        samples, exchanges = _sample_products(args.catalog, requested, args.sample_per_exchange)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))

    results = [_probe_one(exchange, product, url, args.timeout) for exchange, product, url in samples]
    by_exchange: dict[str, dict[str, int]] = {}
    for exchange in exchanges:
        rows = [item for item in results if item["exchange"] == exchange]
        by_exchange[exchange] = {
            "sampled": len(rows),
            "ok": sum(item.get("status") == "ok" for item in rows),
            "empty-normalized": sum(item.get("status") == "empty-normalized" for item in rows),
            "error": sum(item.get("status") == "error" for item in rows),
        }
    total = len(results)
    empty_count = sum(item.get("status") == "empty-normalized" for item in results)
    summary = {
        "per_exchange": by_exchange,
        "overall_empty_normalized_rate": (empty_count / total) if total else 0.0,
    }
    output = {"results": results, "summary": summary}
    transport_failures = sum(
        item.get("status") == "error" and str(item.get("error", "")).startswith("transport-")
        for item in results
    )
    if total and transport_failures == total:
        return 2, output
    return (
        0
        if all(
            by_exchange[item]["sampled"] > 0 and by_exchange[item]["ok"] > 0
            for item in exchanges
        )
        else 1
    ), output


def main(arguments: list[str] | None = None) -> int:
    try:
        code, output = run(arguments)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    except Exception as exc:  # keep stdout JSON-only while making unexpected failures explicit
        print(json.dumps({"error": type(exc).__name__}, ensure_ascii=True, sort_keys=True))
        return 2
    print(json.dumps(output, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
