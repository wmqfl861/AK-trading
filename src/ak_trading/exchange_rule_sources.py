"""Official exchange-source discovery, retrieval, and conservative rule parsing.

Search snippets discover official URLs only. Numeric values are never copied from
those snippets. The collector retrieves pages through the local ``websearch``
stack, archives its normalized content, and emits review-required fields when an
exchange document cannot be deterministically parsed.
"""

from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlparse

from ak_trading.exchange_rule_extraction import (
    AUTO_EXTRACTION_NOTE,
    ExtractedRuleValue,
    extract_rule_values,
)
from ak_trading.exchange_rules import (
    ExchangeRuleError,
    FeeBasis,
    FetchedDocument,
    RuleCategory,
    RuleItem,
    RuleStatus,
    RuleValueKind,
    SourceDocument,
    rule_identity_sort_key,
)
from ak_trading.futures_catalog import (
    FuturesCatalogError,
    load_futures_catalog,
    validate_source_pairs,
)


class ExchangeRuleSourceError(RuntimeError):
    """Raised when an official source cannot be discovered or retrieved safely."""


class DomainNotAllowedError(ExchangeRuleSourceError):
    """Raised when a source URL is not on the configured official allowlist."""


class DocumentFetchError(ExchangeRuleSourceError):
    """Raised when local-stack retrieval cannot produce retained evidence."""

    def __init__(
        self,
        message: str,
        *,
        observability: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.observability = dict(observability or {})
        self.humanlike_retry_attempted = self.observability.get(
            "humanlike_retry_attempted", False
        )
        self.humanlike_retry_error = self.observability.get(
            "humanlike_retry_error"
        )


@dataclass(frozen=True)
class ProductSource:
    """Configured futures product and optional contract-specific rule scope."""

    code: str
    contract: str | None
    query: str
    seed_urls: tuple[str, ...] = ()
    name_zh: str | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.code, "product source code"),
            (self.query, "product source query"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ExchangeRuleSourceError(f"{name} must be non-empty")
        if self.contract is not None and (
            not isinstance(self.contract, str) or not self.contract.strip()
        ):
            raise ExchangeRuleSourceError(
                "product source contract must be a non-empty string or null"
            )
        if any(not isinstance(url, str) or not url.strip() for url in self.seed_urls):
            raise ExchangeRuleSourceError("product source seed URLs must be non-empty")
        if len(set(self.seed_urls)) != len(self.seed_urls):
            raise ExchangeRuleSourceError("product source seed URLs must be unique")


@dataclass(frozen=True)
class ExchangeSource:
    """One exchange's official-domain allowlist and tracked product sources."""

    code: str
    official_domains: tuple[str, ...]
    products: tuple[ProductSource, ...]
    homepage_url: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code.strip():
            raise ExchangeRuleSourceError("exchange source code must be non-empty")
        if not self.official_domains:
            raise ExchangeRuleSourceError("exchange source must list official domains")
        if not self.products:
            raise ExchangeRuleSourceError("exchange source must list products")
        normalized_domains = tuple(
            domain.lower().lstrip(".").rstrip(".")
            for domain in self.official_domains
            if isinstance(domain, str)
        )
        if (
            len(normalized_domains) != len(self.official_domains)
            or any(not domain for domain in normalized_domains)
            or len(set(normalized_domains)) != len(normalized_domains)
        ):
            raise ExchangeRuleSourceError(
                "official domains must be non-empty and unique"
            )
        product_codes = tuple(product.code.upper() for product in self.products)
        if len(set(product_codes)) != len(product_codes):
            raise ExchangeRuleSourceError("exchange product codes must be unique")
        homepage_url = self.homepage_url
        if homepage_url is None:
            homepage_url = f"https://{normalized_domains[0]}/"
            object.__setattr__(self, "homepage_url", homepage_url)
        elif not isinstance(homepage_url, str) or not homepage_url.strip():
            raise ExchangeRuleSourceError(
                "exchange homepage_url must be a non-empty string or null"
            )
        if not is_official_url(homepage_url, normalized_domains):
            raise DomainNotAllowedError(
                f"exchange homepage is not on an official domain: {homepage_url}"
            )


@dataclass(frozen=True)
class DiscoveredDocument:
    """An allowlisted official document found by configuration or discovery."""

    exchange: str
    product: str
    contract: str | None
    url: str
    title: str
    description: str
    discovery_method: str


@dataclass(frozen=True)
class CollectionEvidence:
    """Retained source documents and conservative parsed fields for one exchange."""

    documents: tuple[SourceDocument, ...]
    items: tuple[RuleItem, ...]
    errors: tuple[str, ...]
    review_reasons: tuple[str, ...]


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data.strip())

    @property
    def text(self) -> str:
        return "\n".join(self.parts)


_CHARSET_RE = re.compile(
    r"charset\s*=\s*[\"']?\s*([A-Za-z0-9._:-]+)", re.IGNORECASE
)
_FALLBACK_TEXT_FIELDS = ("text", "content", "body")
_FALLBACK_HTML_FIELDS = ("html", "rawHtml", "raw_html", "rawHTML")

# RuiShu-style WAFs answer blocked fetches with HTTP 200 plus an obfuscated
# challenge script; such payloads are evidence-free and must be treated as
# empty so the tiered fallbacks (and the human-like browser tier) can run.
_WAF_CHALLENGE_MARKERS = ("$_ts=window['$_ts']", '$_ts=window["$_ts"]')
# The same WAF sometimes lets a fetch through only as far as the rendered
# human-verification interstitial (slider puzzle); its normalized markdown is
# non-empty, so the markers must be checked before the content is accepted.
_RENDERED_CHALLENGE_MARKERS = (
    "人机识别检测",
    "向右滑动填充拼图",
    "请开启JavaScript",
    "安全验证",
    "WEB 应用防火墙",
    "#js-challenge",
    "#captcha",
    "#slider",
)
# RuiShu variants also serve an obfuscated bootstrap script whose markdown
# starts with randomly-named window assignments (``window._aBc…=function``).
_JS_OBFUSCATION_RE = re.compile(r"window\._[A-Za-z0-9]{8,}\s*=\s*function")
_TEMPLATE_PLACEHOLDER_MIN_SPANS = 3


def _is_waf_challenge_text(text: str) -> bool:
    head = text.lstrip()[:512]
    return any(marker in head for marker in _WAF_CHALLENGE_MARKERS)


def _is_rendered_challenge_text(text: str) -> bool:
    head = text.lstrip()[:512]
    return any(marker in head for marker in _RENDERED_CHALLENGE_MARKERS) or bool(
        _JS_OBFUSCATION_RE.search(head)
    )


def _is_template_placeholder_text(text: str) -> bool:
    """Detect JS-template parameter pages whose values never rendered.

    Exchange product pages (for example INE contract pages) ship an HTML
    skeleton whose table cells hold ``{{ pageList[0].Field }}`` bindings; the
    concrete values arrive only after client-side rendering.  A scrape that
    stops before that rendering is evidence-free for rule extraction, so the
    tiered fallback (ending in the human-like browser) must retry it.
    """

    spans = re.findall(r"\{\{[^{}\n]{1,120}\}\}", text)
    return len(spans) >= _TEMPLATE_PLACEHOLDER_MIN_SPANS


def _is_evidence_free_text(text: str) -> bool:
    if not isinstance(text, str) or not text.strip():
        return True
    return (
        _is_waf_challenge_text(text)
        or _is_rendered_challenge_text(text)
        or _is_template_placeholder_text(text)
    )


@dataclass(frozen=True)
class _RichScrapeResult:
    data: Mapping[str, Any] | None
    attempted: bool
    error: str | None


@dataclass(frozen=True)
class _HumanlikeRetryResult:
    markdown: str | None
    attempted: bool
    error: str | None


_SCRAPLING_SITE_PACKAGES = Path(
    r"C:\Users\Administrator\.scrapling\Lib\site-packages"
)


def _load_scrapling_session() -> object | None:
    """Load Scrapling only when its optional local installation is available."""

    if _SCRAPLING_SITE_PACKAGES.is_dir():
        candidate = str(_SCRAPLING_SITE_PACKAGES)
        if candidate not in sys.path:
            sys.path.insert(0, candidate)
    try:
        from scrapling.fetchers import StealthySession
    except Exception:
        return None
    return StealthySession


# Scrapling's compiled dependencies belong to its own virtual environment, so
# the reliable fallback is executing the session inside that interpreter.
_SCRAPLING_PYTHON = Path.home() / ".scrapling" / "Scripts" / "python.exe"

_HUMANLIKE_SCRIPT = r"""
import json, sys, time
url, homepage, profile = sys.argv[1], sys.argv[2], sys.argv[3]
from scrapling.fetchers import StealthySession
def scroll(page):
    try:
        page.mouse.wheel(0, 650)
        page.wait_for_timeout(250)
        page.mouse.wheel(0, 650)
    except Exception:
        pass
with StealthySession(headless=True, real_chrome=True, user_data_dir=profile,
                     block_webrtc=True, hide_canvas=True, locale="zh-CN",
                     timeout=90000, google_search=False) as session:
    # Cookie warming only: a homepage that answers with an HTTP error must
    # not kill the run before the target page is attempted.
    home = home_error = None
    try:
        home = session.fetch(homepage, network_idle=True, wait=3000)
        time.sleep(2.5)
    except Exception as exc:
        home_error = str(exc)[:300]
    target = session.fetch(url, network_idle=True, wait=2000, page_action=scroll)
    text = target.get_all_text(strip=True) or ""
    print(json.dumps({"home_status": getattr(home, "status", None),
                      "home_error": home_error,
                      "status": getattr(target, "status", None),
                      "text": text}, ensure_ascii=False))
"""


def _humanlike_subprocess_result(
    homepage_url: str,
    url: str,
    profile_directory: Path,
    timeout: float,
) -> _HumanlikeRetryResult | None:
    """Run the human-like session inside Scrapling's own interpreter.

    Returns None when that interpreter is absent or the subprocess cannot be
    executed, so callers can degrade to the ordinary error path.
    """

    if not isinstance(_SCRAPLING_PYTHON, Path) or not _SCRAPLING_PYTHON.is_file():
        return None
    command = [
        str(_SCRAPLING_PYTHON),
        "-c",
        _HUMANLIKE_SCRIPT,
        url,
        homepage_url,
        str(profile_directory),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout + 120.0,
        )
    except subprocess.TimeoutExpired:
        return _HumanlikeRetryResult(
            markdown=None,
            attempted=True,
            error="subprocess-timeout",
        )
    except OSError:
        return _HumanlikeRetryResult(
            markdown=None,
            attempted=True,
            error="subprocess-launch-failed",
        )
    if completed.returncode != 0:
        return _HumanlikeRetryResult(
            markdown=None,
            attempted=True,
            error=f"subprocess-exit-{completed.returncode}",
        )
    lines = [line for line in (completed.stdout or "").splitlines() if line.strip()]
    if not lines:
        return _HumanlikeRetryResult(
            markdown=None,
            attempted=True,
            error="subprocess-empty-output",
        )
    try:
        payload = json.loads(lines[-1])
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return _HumanlikeRetryResult(
            markdown=None,
            attempted=True,
            error="subprocess-invalid-json",
        )
    if not isinstance(payload, Mapping):
        return _HumanlikeRetryResult(
            markdown=None,
            attempted=True,
            error="subprocess-invalid-json",
        )
    home_status = payload.get("home_status")
    if isinstance(home_status, int) and home_status >= 400:
        return _HumanlikeRetryResult(
            markdown=None,
            attempted=True,
            error=f"homepage-status-{home_status}",
        )
    target_status = payload.get("status")
    if isinstance(target_status, int) and target_status >= 400:
        return _HumanlikeRetryResult(
            markdown=None,
            attempted=True,
            error=f"target-status-{target_status}",
        )
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        return _HumanlikeRetryResult(
            markdown=None,
            attempted=True,
            error="empty-content",
        )
    return _HumanlikeRetryResult(markdown=text, attempted=True, error=None)


def human_scroll(page: object) -> None:
    """Perform a small human-like scroll after the target document loads."""

    try:
        mouse = getattr(page, "mouse")
        mouse.wheel(0, 650)
        wait_for_timeout = getattr(page, "wait_for_timeout", None)
        if callable(wait_for_timeout):
            wait_for_timeout(250)
        mouse.wheel(0, 650)
    except Exception:
        # Scrapling itself logs page-action failures; scrolling is only a WAF aid.
        return None


def _response_status(response: object) -> int | None:
    for name in ("status", "status_code"):
        value = getattr(response, name, None)
        if callable(value):
            try:
                value = value()
            except Exception:
                continue
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _response_text(response: object) -> str:
    """Extract visible response text without depending on Scrapling internals."""

    get_all_text = getattr(response, "get_all_text", None)
    if callable(get_all_text):
        try:
            value = get_all_text(separator="\n", strip=True)
        except Exception:
            value = None
        if value is not None and str(value).strip():
            return str(value).strip()

    for name in ("text", "content", "html", "body"):
        value = getattr(response, name, None)
        if callable(value):
            try:
                value = value()
            except Exception:
                continue
        if isinstance(value, (bytes, bytearray)):
            try:
                value = bytes(value).decode("utf-8", errors="replace")
            except Exception:
                continue
        if isinstance(value, str) and value.strip():
            return _html_text(value) if "<" in value and ">" in value else value.strip()
    return ""


def _nonempty_response_value(value: object) -> str | bytes | None:
    """Return a response text/bytes value only when it has non-whitespace data."""

    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
        return raw if raw.strip() else None
    # JSON fixtures cannot carry bytes, but accepting an integer byte array keeps
    # the adapter useful for response shims that preserve the raw body explicitly.
    if isinstance(value, list) and value and all(
        isinstance(item, int) and 0 <= item <= 255 for item in value
    ):
        raw = bytes(value)
        return raw if raw.strip() else None
    return None


def _response_charset(metadata: Mapping[str, Any]) -> str | None:
    """Extract a declared charset without trusting it for already-decoded JSON text."""

    direct = metadata.get("charset")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    candidates: list[object] = [
        metadata.get("contentType"),
        metadata.get("content-type"),
        metadata.get("mimeType"),
        metadata.get("mime_type"),
    ]
    headers = metadata.get("headers")
    if isinstance(headers, Mapping):
        candidates.extend(headers.get(name) for name in ("content-type", "Content-Type"))
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        match = _CHARSET_RE.search(candidate)
        if match:
            return match.group(1)
    return None


def _html_charset(raw: bytes) -> str | None:
    """Read a charset declaration from the small HTML preamble, if present."""

    probe = raw[:8192].decode("ascii", errors="ignore")
    match = _CHARSET_RE.search(probe)
    return match.group(1) if match else None


def _decode_response_value(
    value: str | bytes,
    *,
    metadata: Mapping[str, Any],
) -> tuple[str, str, str, bytes | None]:
    """Decode fallback content while keeping the original bytes when available."""

    if isinstance(value, str):
        return value.strip(), "utf-8", "default", None

    raw = bytes(value)
    encodings: list[tuple[str, str]] = []
    if raw.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        encodings.append(("utf-32", "bom"))
    elif raw.startswith(b"\xef\xbb\xbf"):
        encodings.append(("utf-8-sig", "bom"))
    elif raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings.append(("utf-16", "bom"))
    candidates = (
        (_response_charset(metadata), "metadata"),
        (_html_charset(raw), "html-meta"),
        ("utf-8", "default"),
        ("gb18030", "default"),
    )
    for encoding, source in candidates:
        if encoding and encoding.lower() not in {
            item_encoding.lower() for item_encoding, _ in encodings
        }:
            encodings.append((encoding, source))
    for encoding, source in encodings:
        try:
            decoded = raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
        if decoded.strip():
            return decoded.strip(), encoding, source, raw
    # Latin-1 is a lossless final decode for evidence. It is intentionally not
    # treated as a successful charset claim; the original bytes remain attached.
    return raw.decode("latin-1").strip(), "latin-1", "fallback", raw


def _html_text(value: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(value)
        parser.close()
    except (AssertionError, ValueError, TypeError):
        # _markupbase raises AssertionError on malformed <![...]> marked
        # sections (e.g. conditional-comment remnants); the raw evidence
        # stays attached upstream, so an empty text here is safe.
        return ""
    return parser.text.strip()


def _fallback_content(
    data: Mapping[str, Any], metadata: Mapping[str, Any]
) -> tuple[str, dict[str, Any]] | None:
    """Choose the first usable tiered field after normalized markdown is empty."""

    mappings: list[tuple[str, Mapping[str, Any]]] = [("data", data)]
    for parent_name in ("result", "document"):
        parent = data.get(parent_name)
        if isinstance(parent, Mapping):
            mappings.append((parent_name, parent))

    for field_names, is_html in (
        (_FALLBACK_TEXT_FIELDS, False),
        (_FALLBACK_HTML_FIELDS, True),
    ):
        for parent_name, parent in mappings:
            for field in field_names:
                candidate = _nonempty_response_value(parent.get(field))
                if candidate is None:
                    continue
                decoded, charset, charset_source, raw_bytes = _decode_response_value(
                    candidate, metadata=metadata
                )
                if not decoded or _is_evidence_free_text(decoded):
                    continue
                normalized = _html_text(decoded) if is_html else decoded
                normalized = normalized or decoded
                # Script text extracted from a challenge page surfaces here
                # even when the raw HTML head does not match the markers.
                if not normalized or _is_evidence_free_text(normalized):
                    continue
                fallback: dict[str, Any] = {
                    "field": f"{parent_name}.{field}" if parent_name != "data" else field,
                    "kind": "html" if is_html else "text",
                    "charset": charset,
                    "charset_source": charset_source,
                }
                if is_html:
                    fallback["raw_html"] = decoded
                if raw_bytes is not None:
                    fallback["raw_bytes_base64"] = base64.b64encode(raw_bytes).decode(
                        "ascii"
                    )
                return normalized, fallback
    return None


def _rich_scrape_data(
    url: str,
    *,
    timeout: float,
    runner: Callable[[list[str], float], subprocess.CompletedProcess[str]],
) -> _RichScrapeResult:
    """Request rich formats from the local Firecrawl-compatible stack on demand."""

    executable = shutil.which("firecrawl")
    if not executable:
        return _RichScrapeResult(
            data=None,
            attempted=False,
            error="executable-not-found",
        )
    command = [
        executable,
        "--api-url",
        "http://127.0.0.1:3002",
        "-k",
        "fc-local",
        "scrape",
        url,
        "--format",
        "markdown",
        "--format",
        "html",
        "--format",
        "rawHtml",
        "--json",
    ]
    try:
        completed = runner(command, timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _RichScrapeResult(
            data=None,
            attempted=True,
            error=f"transport-error: {type(exc).__name__}",
        )
    if completed.returncode != 0:
        return _RichScrapeResult(
            data=None,
            attempted=True,
            error=f"exit-code-{completed.returncode}",
        )
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return _RichScrapeResult(data=None, attempted=True, error="invalid-json")
    if not isinstance(response, Mapping):
        return _RichScrapeResult(
            data=None,
            attempted=True,
            error="unusable-response-shape",
        )
    nested = response.get("data")
    if isinstance(nested, Mapping):
        return _RichScrapeResult(data=nested, attempted=True, error=None)
    # The Firecrawl CLI emits the data mapping directly for a single URL.
    if any(key in response for key in ("markdown", "html", "rawHtml")):
        return _RichScrapeResult(data=response, attempted=True, error=None)
    return _RichScrapeResult(
        data=None,
        attempted=True,
        error="unusable-response-shape",
    )


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExchangeRuleSourceError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_date(value: object, field: str) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ExchangeRuleSourceError(f"{field} must be an ISO date or null")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ExchangeRuleSourceError(f"{field} must be YYYY-MM-DD") from exc


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ExchangeRuleSourceError("local-stack evidence is not JSON serializable") from exc


def load_exchange_sources(path: str | Path) -> tuple[ExchangeSource, ...]:
    """Load the versioned official-source allowlist and discovery configuration."""

    source = Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExchangeRuleSourceError(
            f"cannot load exchange rule sources: {source}"
        ) from exc
    if not isinstance(raw, Mapping) or raw.get("schema_version") != 1:
        raise ExchangeRuleSourceError("unsupported exchange-rule source schema_version")
    raw_sources = raw.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ExchangeRuleSourceError("sources must be a non-empty list")

    sources: list[ExchangeSource] = []
    for source_index, raw_source in enumerate(raw_sources):
        if not isinstance(raw_source, Mapping):
            raise ExchangeRuleSourceError(f"sources[{source_index}] must be an object")
        raw_domains = raw_source.get("official_domains")
        raw_products = raw_source.get("products")
        if not isinstance(raw_domains, list) or not raw_domains:
            raise ExchangeRuleSourceError(
                f"sources[{source_index}].official_domains must be a non-empty list"
            )
        if not isinstance(raw_products, list) or not raw_products:
            raise ExchangeRuleSourceError(
                f"sources[{source_index}].products must be a non-empty list"
            )
        products: list[ProductSource] = []
        for product_index, raw_product in enumerate(raw_products):
            if not isinstance(raw_product, Mapping):
                raise ExchangeRuleSourceError(
                    f"sources[{source_index}].products[{product_index}] must be an object"
                )
            raw_seed_urls = raw_product.get("seed_urls", [])
            if not isinstance(raw_seed_urls, list):
                raise ExchangeRuleSourceError(
                    f"sources[{source_index}].products[{product_index}].seed_urls "
                    "must be a list"
                )
            raw_contract = raw_product.get("contract")
            if raw_contract is not None and not isinstance(raw_contract, str):
                raise ExchangeRuleSourceError(
                    f"sources[{source_index}].products[{product_index}].contract "
                    "must be a string or null"
                )
            products.append(
                ProductSource(
                    code=_nonempty(raw_product.get("code"), "product.code").upper(),
                    contract=raw_contract.strip() if isinstance(raw_contract, str) else None,
                    query=_nonempty(raw_product.get("query"), "product.query"),
                    seed_urls=tuple(
                        _nonempty(seed_url, "product.seed_url")
                        for seed_url in raw_seed_urls
                    ),
                )
            )
        exchange = ExchangeSource(
            code=_nonempty(raw_source.get("code"), "source.code").upper(),
            official_domains=tuple(
                _nonempty(domain, "source.official_domain") for domain in raw_domains
            ),
            products=tuple(products),
            homepage_url=raw_source.get("homepage_url"),
        )
        for product in exchange.products:
            for seed_url in product.seed_urls:
                require_official_url(seed_url, exchange.official_domains)
        sources.append(exchange)
    source_codes = tuple(item.code for item in sources)
    if len(set(source_codes)) != len(source_codes):
        raise ExchangeRuleSourceError("source codes must be unique")
    return tuple(sources)


def validate_sources_against_catalog(
    sources: Sequence[ExchangeSource],
    catalog_path: str | Path,
    *,
    require_all_exchanges: bool = True,
) -> None:
    """Require exact future-only `(exchange, product)` catalog coverage.

    Options never become `ProductSource` values. The catalog is the authority for
    the collection universe; this validation also prevents a product code from
    being accidentally assigned to the wrong exchange.
    """

    try:
        catalog = load_futures_catalog(
            catalog_path, require_all_exchanges=require_all_exchanges
        )
        validate_source_pairs(
            catalog,
            (
                (source.code, product.code)
                for source in sources
                for product in source.products
            ),
        )
    except FuturesCatalogError as exc:
        raise ExchangeRuleSourceError(str(exc)) from exc

    sources_by_exchange = {source.code.upper(): source for source in sources}
    for catalog_source in catalog.sources:
        source = sources_by_exchange.get(catalog_source.exchange.upper())
        if source is None:
            raise ExchangeRuleSourceError(
                f"source registry is missing catalog exchange {catalog_source.exchange}"
            )
        if set(domain.lower().strip(".") for domain in source.official_domains) != set(
            catalog_source.official_domains
        ):
            raise ExchangeRuleSourceError(
                f"source registry domains do not match official catalog for "
                f"{catalog_source.exchange}"
            )


def build_exchange_sources_from_catalog(
    catalog_path: str | Path,
) -> tuple[ExchangeSource, ...]:
    """Build product-level official rule sources from a futures-only catalog.

    Product pages are retained as evidence seeds. Rules are queried per listed
    futures product, while options remain catalog-only exclusions and cannot enter
    this source registry.
    """

    try:
        catalog = load_futures_catalog(catalog_path, require_all_exchanges=True)
    except FuturesCatalogError as exc:
        raise ExchangeRuleSourceError(str(exc)) from exc

    sources: list[ExchangeSource] = []
    for catalog_source in catalog.sources:
        products = tuple(
            ProductSource(
                code=product.code,
                contract=None,
                query=catalog_source.rule_query_template.format(
                    product=product.name_zh
                ),
                seed_urls=(product.source_url, *product.spec_urls),
                name_zh=product.name_zh,
            )
            for product in catalog.products_for_exchange(catalog_source.exchange)
        )
        if not products:
            raise ExchangeRuleSourceError(
                f"catalog has no futures products for {catalog_source.exchange}"
            )
        sources.append(
            ExchangeSource(
                code=catalog_source.exchange,
                official_domains=catalog_source.official_domains,
                products=products,
                homepage_url=catalog_source.catalog_url,
            )
        )
    validate_sources_against_catalog(sources, catalog_path)
    return tuple(sources)


def is_official_url(url: str, domains: Iterable[str]) -> bool:
    """Return whether an HTTP(S) URL belongs to an allowlisted exchange domain."""

    parsed = urlparse(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return False
    hostname = parsed.hostname.lower().rstrip(".")
    for domain in domains:
        normalized = domain.lower().lstrip(".").rstrip(".")
        if hostname == normalized or hostname.endswith(f".{normalized}"):
            return True
    return False


def require_official_url(url: str, domains: Iterable[str]) -> str:
    """Validate an official URL before it can enter the evidence pipeline."""

    if not is_official_url(url, domains):
        raise DomainNotAllowedError(
            "source URL is not on the configured official-domain allowlist"
        )
    return url


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


def _require_positive_timeout(value: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ExchangeRuleSourceError(f"{label} must be positive")
    return float(value)


def _websearch_executable(executable: str | Path | None) -> str:
    resolved = str(executable) if executable is not None else shutil.which("websearch")
    if not resolved:
        raise ExchangeRuleSourceError("websearch executable not found")
    return resolved


def discover_official_documents(
    source: ExchangeSource,
    *,
    executable: str | Path | None = None,
    timeout: float = 45.0,
    limit: int = 10,
    runner: Callable[[list[str], float], subprocess.CompletedProcess[str]] = _default_runner,
) -> tuple[tuple[DiscoveredDocument, ...], tuple[str, ...]]:
    """Discover allowlisted URLs and isolate runtime search failures by product."""

    timeout_value = _require_positive_timeout(timeout, "search timeout")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise ExchangeRuleSourceError("search limit must be between 1 and 50")
    resolved = _websearch_executable(executable)
    discovered: list[DiscoveredDocument] = []
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()

    def append(
        product: ProductSource,
        *,
        url: str,
        title: str,
        description: str,
        discovery_method: str,
    ) -> None:
        require_official_url(url, source.official_domains)
        key = (product.code.upper(), url)
        if key in seen:
            return
        seen.add(key)
        discovered.append(
            DiscoveredDocument(
                exchange=source.code,
                product=product.code.upper(),
                contract=product.contract,
                url=url,
                title=title.strip() or url,
                description=description.strip()[:1000],
                discovery_method=discovery_method,
            )
        )

    for product in source.products:
        for seed_url in product.seed_urls:
            append(
                product,
                url=seed_url,
                title=seed_url,
                description="Configured official seed URL.",
                discovery_method="configured_seed",
            )
        command = [resolved, "search", product.query, "--limit", str(limit)]
        try:
            completed = runner(command, timeout_value)
        except subprocess.TimeoutExpired:
            errors.append(
                f"official search timed out for {source.code}/{product.code}"
            )
            continue
        except OSError:
            errors.append(
                f"official search could not start for {source.code}/{product.code}"
            )
            continue
        if completed.returncode != 0:
            errors.append(
                f"official search exited {completed.returncode} for "
                f"{source.code}/{product.code}"
            )
            continue
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            errors.append(
                f"websearch returned invalid JSON for {source.code}/{product.code}"
            )
            continue
        if not isinstance(payload, Mapping) or payload.get("success") is not True:
            errors.append(
                "websearch response did not report success for "
                f"{source.code}/{product.code}"
            )
            continue
        data = payload.get("data")
        raw_results = data.get("web", []) if isinstance(data, Mapping) else []
        if not isinstance(raw_results, list):
            raw_results = []
        for raw_result in raw_results:
            if not isinstance(raw_result, Mapping):
                continue
            url = str(raw_result.get("url") or "").strip()
            if not url or not is_official_url(url, source.official_domains):
                continue
            append(
                product,
                url=url,
                title=str(raw_result.get("title") or url),
                description=str(raw_result.get("description") or ""),
                discovery_method="local_websearch",
            )
    return tuple(discovered), tuple(errors)


_STRUCTURED_FILE_MAX_BYTES = 20 * 1024 * 1024

# SHFE and INE publish the complete daily settlement parameters (margin and
# fee ratios for every listed contract) as plain JSON ``js{YYYYMMDD}.dat``
# files with no WAF in front of them.  The file for a trading day appears
# after that day's settlement, so reads walk back to the latest published
# file (weekends and holidays included).
_DAILY_PARAMETER_HOSTS: dict[str, str] = {
    "SHFE": "https://www.shfe.com.cn",
    "INE": "https://www.ine.cn",
}
_DAILY_PARAMETER_LOOKBACK_DAYS = 12
_DAILY_PARAMETER_REQUIRED_FIELD = "SPECLONGMARGINRATIO"


def _daily_parameter_url(exchange: str, day: date) -> str:
    host = _DAILY_PARAMETER_HOSTS[exchange.upper()]
    return f"{host}/data/tradedata/future/dailydata/js{day:%Y%m%d}.dat"


def fetch_exchange_daily_parameters(
    exchange: str,
    day: date,
    *,
    timeout: float,
    official_domains: Sequence[str] = (),
    lookback_days: int = _DAILY_PARAMETER_LOOKBACK_DAYS,
) -> "FetchedDocument | None":
    """Fetch the latest published SHFE/INE daily settlement-parameter JSON.

    The returned evidence records both the requested day and the final
    (actually published) day, and carries the published day as its explicit
    effective date so parameter-table supersession uses the real freshness.
    """

    exchange = exchange.upper()
    host = _DAILY_PARAMETER_HOSTS.get(exchange)
    if host is None:
        return None
    requested_url = _daily_parameter_url(exchange, day)
    for offset in range(lookback_days + 1):
        candidate_day = day - timedelta(days=offset)
        url = _daily_parameter_url(exchange, candidate_day)
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0 Safari/537.36"
                ),
                "Referer": f"{host}/",
                "Accept": "application/json, */*",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                final_url = str(response.geturl() or url)
                if official_domains and not is_official_url(
                    final_url, tuple(official_domains)
                ):
                    return None
                payload = response.read(_STRUCTURED_FILE_MAX_BYTES + 1)
                if len(payload) > _STRUCTURED_FILE_MAX_BYTES:
                    continue
        except (urllib.error.URLError, OSError, ValueError):
            continue
        if not payload:
            continue
        try:
            text = payload.decode("utf-8")
            data = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        rows = data.get("o_cursor") if isinstance(data, Mapping) else None
        if not isinstance(rows, list) or not rows:
            continue
        if not any(
            isinstance(row, Mapping)
            and _DAILY_PARAMETER_REQUIRED_FIELD in row
            for row in rows
        ):
            continue
        now = datetime.now(timezone.utc)
        content = json.dumps(
            {
                "schema_version": 1,
                "retrieval_method": "direct_structured_file",
                "source_url": final_url,
                "requested_url": requested_url,
                "published_trading_day": candidate_day.isoformat(),
                "title": f"{exchange} daily settlement parameters {candidate_day.isoformat()}",
                "markdown": text,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
        return FetchedDocument(
            url=final_url,
            title=f"{exchange} daily settlement parameters",
            content=content,
            content_type="application/vnd.ak-trading.websearch-scrape+json",
            retrieved_at=now,
            effective_date=candidate_day,
        )
    return None


def _fetch_structured_file(
    url: str,
    *,
    timeout: float,
    official_domains: Sequence[str] = (),
) -> "FetchedDocument | None":
    """Fetch official CSV/XLSX parameter files directly.

    The local websearch stack renders HTML and discards raw file downloads,
    while exchanges publish their settlement-parameter tables as plain files.
    CSV bytes pass through as text; XLSX sheets are flattened to one
    tab-separated line per row via openpyxl when it is importable. The
    response must stay on the official allowlist across redirects, the final
    URL is recorded as provenance, and downloads are size-capped.
    """

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return None
    suffix = parsed.path.lower().rsplit(".", 1)[-1] if "." in parsed.path else ""
    if suffix not in {"csv", "xlsx"}:
        return None
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            ),
            "Accept": "*/*",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            final_url = str(response.geturl() or url)
            if official_domains and not is_official_url(
                final_url, tuple(official_domains)
            ):
                return None
            payload = response.read(_STRUCTURED_FILE_MAX_BYTES + 1)
            if len(payload) > _STRUCTURED_FILE_MAX_BYTES:
                return None
            charset = response.headers.get_content_charset() or "gbk"
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not payload:
        return None
    title = urlparse(final_url).path.rsplit("/", 1)[-1] or parsed.path.rsplit("/", 1)[-1]
    if suffix == "csv":
        text = payload.decode(charset, errors="replace")
        if not text.strip() or "<html" in text[:400].lower():
            return None
    else:
        try:
            import io

            import openpyxl
        except ImportError:
            return None
        try:
            workbook = openpyxl.load_workbook(io.BytesIO(payload), read_only=True)
            sheet = workbook.active
            rows = []
            for row in sheet.iter_rows(values_only=True):
                cells = ["" if cell is None else str(cell) for cell in row]
                rows.append("\t".join(cells))
            text = "\n".join(rows)
            workbook.close()
        except Exception:
            return None
        if not text.strip():
            return None
    now = datetime.now(timezone.utc)
    content = json.dumps(
        {
            "schema_version": 1,
            "retrieval_method": "direct_structured_file",
            "source_url": final_url,
            "requested_url": url,
            "title": title,
            "markdown": text,
        },
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    return FetchedDocument(
        url=final_url,
        title=title,
        content=content,
        content_type="application/vnd.ak-trading.websearch-scrape+json",
        retrieved_at=now,
    )


class LocalWebSearchDocumentFetcher:
    """Retrieve official pages only through the mandatory local web-search stack."""

    def __init__(
        self,
        *,
        executable: str | Path | None = None,
        timeout: float = 120.0,
        stealth: bool = True,
        runner: Callable[[list[str], float], subprocess.CompletedProcess[str]] = _default_runner,
        now: Callable[[], datetime] | None = None,
        exchange: str | None = None,
        exchange_homepage: str | None = None,
        official_domains: Sequence[str] = (),
        humanlike_enabled: bool = True,
        profile_root: str | Path | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self.executable = _websearch_executable(executable)
        self.timeout = _require_positive_timeout(timeout, "fetch timeout")
        if not isinstance(stealth, bool):
            raise ExchangeRuleSourceError("stealth must be bool")
        self.stealth = stealth
        self.runner = runner
        self.now = now or (lambda: datetime.now(timezone.utc))
        if exchange is not None and (
            not isinstance(exchange, str) or not exchange.strip()
        ):
            raise ExchangeRuleSourceError("exchange must be a non-empty string or null")
        if exchange_homepage is not None and (
            not isinstance(exchange_homepage, str) or not exchange_homepage.strip()
        ):
            raise ExchangeRuleSourceError(
                "exchange_homepage must be a non-empty string or null"
            )
        if not isinstance(humanlike_enabled, bool):
            raise ExchangeRuleSourceError("humanlike_enabled must be bool")
        if sleep_fn is not None and not callable(sleep_fn):
            raise ExchangeRuleSourceError("sleep_fn must be callable")
        self.exchange = exchange.strip().upper() if exchange else None
        self.exchange_homepage = exchange_homepage.strip() if exchange_homepage else None
        self.official_domains = tuple(str(domain).lower().strip(".") for domain in official_domains)
        self.humanlike_enabled = humanlike_enabled
        self.profile_root = Path(profile_root) if profile_root is not None else (
            Path(__file__).resolve().parents[2]
            / "data"
            / "exchange_rules"
            / ".humanlike_profiles"
        )
        self.sleep_fn = sleep_fn or time.sleep
        self.last_observability: dict[str, object] | None = None
        # One URL is discovered for several products (shared notices, common
        # parameter files); a per-fetcher memo keeps the expensive WAF
        # fallbacks (and plain re-downloads) to one attempt per URL per run.
        self._fetched_by_url: dict[str, FetchedDocument] = {}

    def _humanlike_observability(
        self, result: _HumanlikeRetryResult
    ) -> dict[str, object]:
        observability = {
            "humanlike_retry_attempted": result.attempted,
            "humanlike_retry_error": result.error,
        }
        self.last_observability = observability
        return observability

    def _humanlike_error(
        self,
        message: str,
        result: _HumanlikeRetryResult,
    ) -> DocumentFetchError:
        return DocumentFetchError(
            message,
            observability=self._humanlike_observability(result),
        )

    def _profile_directory(self, homepage_url: str) -> Path:
        exchange = self.exchange or urlparse(homepage_url).hostname or "unknown"
        safe_exchange = re.sub(r"[^A-Za-z0-9._-]+", "_", exchange).strip("._")
        safe_exchange = safe_exchange or "unknown"
        profile = self.profile_root / safe_exchange
        profile.mkdir(parents=True, exist_ok=True)
        return profile

    def _validate_humanlike_homepage(self, homepage_url: str, target_url: str) -> bool:
        if self.official_domains:
            if not is_official_url(homepage_url, self.official_domains):
                return False
            if not is_official_url(target_url, self.official_domains):
                return False
        else:
            parsed_homepage = urlparse(homepage_url)
            if (
                parsed_homepage.scheme not in {"http", "https"}
                or not parsed_homepage.hostname
                or parsed_homepage.username is not None
                or parsed_homepage.password is not None
            ):
                return False
            parsed_target = urlparse(target_url)
            if (
                parsed_target.scheme not in {"http", "https"}
                or not parsed_target.hostname
                or parsed_target.username is not None
                or parsed_target.password is not None
            ):
                return False
        target_host = urlparse(target_url).hostname
        homepage_host = urlparse(homepage_url).hostname
        if not target_host or not homepage_host:
            return False
        target_host = target_host.lower().rstrip(".")
        homepage_host = homepage_host.lower().rstrip(".")
        # Hosts may live on different subdomains of the same official domain
        # (e.g. qhxy.dce.com.cn documents under the www.dce.com.cn homepage).
        for domain in self.official_domains:
            normalized = domain.lower().rstrip(".")
            if (
                target_host == normalized or target_host.endswith(f".{normalized}")
            ) and (
                homepage_host == normalized
                or homepage_host.endswith(f".{normalized}")
            ):
                return True
        return target_host == homepage_host or target_host.endswith(
            f".{homepage_host}"
        ) or homepage_host.endswith(f".{target_host}")

    def _run_humanlike_session(
        self,
        session_factory: Callable[..., object],
        *,
        homepage_url: str,
        target_url: str,
        profile_directory: Path,
    ) -> _HumanlikeRetryResult:
        """Drive one human-like browser session: warm the homepage, fetch the target.

        The homepage visit only warms WAF cookies, so a failing or non-200
        homepage is recorded but never aborts the target attempt.
        """

        try:
            session = session_factory(
                headless=True,
                real_chrome=True,
                user_data_dir=str(profile_directory),
                block_webrtc=True,
                hide_canvas=True,
                locale="zh-CN",
                timeout=90000,
                google_search=False,
            )
            starter = getattr(session, "start", None)
            closer = getattr(session, "close", None)
            context_enter = getattr(session, "__enter__", None)
            context_exit = getattr(session, "__exit__", None)
            active = session
            try:
                if callable(starter):
                    starter()
                elif callable(context_enter) and callable(context_exit):
                    active = context_enter()
                try:
                    try:
                        homepage_response = active.fetch(
                            homepage_url,
                            network_idle=True,
                            wait=4000,
                        )
                        homepage_status = _response_status(homepage_response)
                        if homepage_status is not None and homepage_status >= 400:
                            return _HumanlikeRetryResult(
                                markdown=None,
                                attempted=True,
                                error=f"homepage-status-{homepage_status}",
                            )
                    except Exception:
                        # Cookie warming failed; the target may still load.
                        pass
                    self.sleep_fn(3.5)
                    target_response = active.fetch(
                        target_url,
                        network_idle=True,
                        wait=2500,
                        page_action=human_scroll,
                    )
                finally:
                    if active is not session and callable(context_exit):
                        context_exit(None, None, None)
                    elif callable(closer):
                        closer()
            except Exception as exc:
                return _HumanlikeRetryResult(
                    markdown=None,
                    attempted=True,
                    error=f"transport-error:{type(exc).__name__}",
                )
            target_status = _response_status(target_response)
            if target_status is not None and target_status >= 400:
                return _HumanlikeRetryResult(
                    markdown=None,
                    attempted=True,
                    error=f"target-status-{target_status}",
                )
            markdown = _response_text(target_response)
            if not markdown:
                return _HumanlikeRetryResult(
                    markdown=None,
                    attempted=True,
                    error="empty-content",
                )
            return _HumanlikeRetryResult(
                markdown=markdown,
                attempted=True,
                error=None,
            )
        except Exception as exc:
            return _HumanlikeRetryResult(
                markdown=None,
                attempted=True,
                error=f"transport-error:{type(exc).__name__}",
            )

    def _humanlike_retry(
        self,
        url: str,
        *,
        exchange_homepage: str | None = None,
    ) -> _HumanlikeRetryResult:
        homepage_url = exchange_homepage or self.exchange_homepage
        if homepage_url is None:
            return _HumanlikeRetryResult(
                markdown=None,
                attempted=True,
                error="homepage-unavailable",
            )
        if not self._validate_humanlike_homepage(homepage_url, url):
            return _HumanlikeRetryResult(
                markdown=None,
                attempted=True,
                error="transport-error:InvalidHomepage",
            )
        try:
            profile_directory = self._profile_directory(homepage_url)
        except Exception as exc:
            return _HumanlikeRetryResult(
                markdown=None,
                attempted=True,
                error=f"transport-error:{type(exc).__name__}",
            )
        session_factory = _load_scrapling_session()
        if session_factory is not None:
            result = self._run_humanlike_session(
                session_factory,
                homepage_url=homepage_url,
                target_url=url,
                profile_directory=profile_directory,
            )
            return self._retry_transient_humanlike(
                result,
                session_factory=session_factory,
                homepage_url=homepage_url,
                url=url,
                profile_directory=profile_directory,
            )
        subprocess_result = _humanlike_subprocess_result(
            homepage_url,
            url,
            profile_directory,
            self.timeout,
        )
        if subprocess_result is not None:
            if subprocess_result.markdown is None:
                subprocess_result = self._retry_transient_humanlike(
                    subprocess_result,
                    homepage_url=homepage_url,
                    url=url,
                    profile_directory=profile_directory,
                )
            return subprocess_result
        return _HumanlikeRetryResult(
            markdown=None,
            attempted=True,
            error="scrapling-unavailable",
        )

    def _retry_transient_humanlike(
        self,
        result: _HumanlikeRetryResult,
        *,
        homepage_url: str,
        url: str,
        profile_directory: Path,
        session_factory: Callable[..., object] | None = None,
    ) -> _HumanlikeRetryResult:
        """Retry transient humanlike failures (crash, timeout, WAF 5xx).

        Long collection runs sporadically see the browser subprocess exit
        non-zero (profile lock from a zombie Chrome, a brief homepage 522,
        interference from other headless-browser jobs on the machine); two
        spaced retries recover those without masking real failures.
        """

        error = result.error or ""

        def _transient(value: str) -> bool:
            return (
                value.startswith("subprocess-exit-")
                or value == "subprocess-timeout"
                or value == "subprocess-launch-failed"
                or re.match(r"homepage-status-5\d\d$", value) is not None
            )

        for _ in range(2):
            if result.markdown is not None or not _transient(error):
                return result
            self.sleep_fn(8.0)
            if session_factory is not None:
                result = self._run_humanlike_session(
                    session_factory,
                    homepage_url=homepage_url,
                    target_url=url,
                    profile_directory=profile_directory,
                )
            else:
                retried = _humanlike_subprocess_result(
                    homepage_url,
                    url,
                    profile_directory,
                    self.timeout,
                )
                if retried is not None:
                    result = retried
            error = result.error or ""
        return result

    def fetch(
        self,
        url: str,
        *,
        exchange_homepage: str | None = None,
        humanlike_enabled: bool | None = None,
    ) -> FetchedDocument:
        cached = self._fetched_by_url.get(url)
        if cached is not None:
            return cached
        document = self._fetch_uncached(
            url,
            exchange_homepage=exchange_homepage,
            humanlike_enabled=humanlike_enabled,
        )
        self._fetched_by_url[url] = document
        return document

    def _fetch_uncached(
        self,
        url: str,
        *,
        exchange_homepage: str | None = None,
        humanlike_enabled: bool | None = None,
    ) -> FetchedDocument:
        direct = _fetch_structured_file(
            url,
            timeout=self.timeout,
            official_domains=self.official_domains,
        )
        if direct is not None:
            return direct
        command = [self.executable, "scrape", url]
        if self.stealth:
            command.append("--stealth")
        try:
            completed = self.runner(command, self.timeout)
        except subprocess.TimeoutExpired as exc:
            raise DocumentFetchError("local websearch scrape timed out") from exc
        except OSError as exc:
            raise DocumentFetchError("local websearch scrape could not start") from exc
        if completed.returncode != 0:
            raise DocumentFetchError(
                f"local websearch scrape exited {completed.returncode}"
            )
        try:
            response = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise DocumentFetchError("local websearch scrape returned invalid JSON") from exc
        if not isinstance(response, Mapping) or response.get("success") is not True:
            raise DocumentFetchError("local websearch scrape did not report success")
        data = response.get("data")
        if not isinstance(data, Mapping):
            raise DocumentFetchError("local websearch scrape returned no document payload")
        metadata = data.get("metadata")
        if not isinstance(metadata, Mapping):
            metadata = {}
        markdown = data.get("markdown")
        fallback: dict[str, Any] | None = None
        rich_retry: _RichScrapeResult | None = None
        humanlike_retry: _HumanlikeRetryResult | None = None
        # A WAF challenge means the site is actively filtering this client.
        # Proxy renderers (the rich local stack) then often return a degraded
        # or list-only rendering, so the real-browser humanlike tier must run
        # anyway and its result is preferred when it yields content.
        initial_was_challenge = (
            isinstance(markdown, str)
            and markdown.strip()
            and (
                _is_waf_challenge_text(markdown)
                or _is_rendered_challenge_text(markdown)
            )
        )
        if (
            not isinstance(markdown, str)
            or not markdown.strip()
            or _is_evidence_free_text(markdown)
        ):
            selected = _fallback_content(data, metadata)
            selected_from_rich = False
            if selected is None:
                rich_retry = _rich_scrape_data(
                    url,
                    timeout=self.timeout,
                    runner=self.runner,
                )
                rich_data = rich_retry.data
                if rich_data is not None:
                    rich_metadata = rich_data.get("metadata")
                    if isinstance(rich_metadata, Mapping):
                        metadata = {**metadata, **rich_metadata}
                    selected = _fallback_content(rich_data, metadata)
                    if selected is None:
                        rich_markdown = rich_data.get("markdown")
                        if (
                            isinstance(rich_markdown, str)
                            and rich_markdown.strip()
                            and not _is_evidence_free_text(rich_markdown)
                        ):
                            selected = (rich_markdown.strip(), None)
                    if selected is not None:
                        selected_from_rich = True
                        data = {**data, **rich_data}
            effective_humanlike_enabled = (
                self.humanlike_enabled
                if humanlike_enabled is None
                else humanlike_enabled
            )
            if not isinstance(effective_humanlike_enabled, bool):
                raise ExchangeRuleSourceError("humanlike_enabled must be bool")
            humanlike_succeeded = False
            if (
                selected is None
                or initial_was_challenge
                or selected_from_rich
            ) and effective_humanlike_enabled:
                humanlike_retry = self._humanlike_retry(
                    url,
                    exchange_homepage=exchange_homepage,
                )
                if (
                    humanlike_retry.markdown is not None
                    and not _is_evidence_free_text(humanlike_retry.markdown)
                ):
                    markdown = humanlike_retry.markdown
                    humanlike_succeeded = True
                    self._humanlike_observability(humanlike_retry)
                    data = {
                        **data,
                        "fetcher": "scrapling_humanlike",
                    }
                    metadata = {
                        **metadata,
                        "sourceURL": url,
                        "title": metadata.get("title") or url,
                        "statusCode": 200,
                    }
                else:
                    if (
                        humanlike_retry.markdown is not None
                        and _is_evidence_free_text(humanlike_retry.markdown)
                    ):
                        humanlike_retry = replace(
                            humanlike_retry,
                            error=humanlike_retry.error or "challenge-content",
                        )
                    if selected is None:
                        raise self._humanlike_error(
                            "local websearch scrape returned empty normalized "
                            "content; humanlike fallback failed: "
                            f"{humanlike_retry.error or 'empty-content'}",
                            humanlike_retry,
                        )
                    # Keep the rich-stack rendering; the failed humanlike
                    # attempt stays visible through the retry-observability
                    # fields written into the retained payload.
            elif selected is None:
                raise DocumentFetchError(
                    "local websearch scrape returned empty normalized content"
                )
            if selected is not None and not humanlike_succeeded:
                markdown, fallback = selected
        source_url = str(metadata.get("sourceURL") or metadata.get("url") or url).strip()
        if not source_url:
            raise DocumentFetchError("local websearch scrape returned no source URL")
        title = str(metadata.get("title") or source_url).strip() or source_url
        fetch_errors = data.get("fetchErrors")
        if not isinstance(fetch_errors, list):
            fetch_errors = []
        normalized = {
            "schema_version": 1,
            "retrieval_method": "local_websearch_scrape",
            "requested_url": url,
            "source_url": source_url,
            "title": title,
            "metadata": {
                "description": str(metadata.get("description") or ""),
                "status_code": metadata.get("statusCode"),
                "fetcher": str(data.get("fetcher") or "unknown"),
                "cached": data.get("cached") is True,
                "fetch_errors": [str(error)[:1000] for error in fetch_errors],
            },
            "markdown": markdown,
        }
        if fallback is not None:
            normalized["fallback"] = fallback
        if rich_retry is not None:
            normalized["rich_retry_attempted"] = rich_retry.attempted
            normalized["rich_retry_error"] = rich_retry.error
        if humanlike_retry is not None:
            normalized["humanlike_retry_attempted"] = humanlike_retry.attempted
            normalized["humanlike_retry_error"] = humanlike_retry.error
        retrieved_at = self.now()
        if retrieved_at.tzinfo is None:
            raise DocumentFetchError("local fetch clock returned a naive datetime")
        return FetchedDocument(
            url=source_url,
            title=title,
            content=_canonical_json_bytes(normalized),
            content_type="application/vnd.ak-trading.websearch-scrape+json",
            retrieved_at=retrieved_at,
        )


def document_suffix(content_type: str, url: str) -> str:
    """Choose a safe artifact suffix without trusting query-string filenames."""

    normalized = content_type.lower().split(";", 1)[0]
    if normalized == "application/pdf" or urlparse(url).path.lower().endswith(".pdf"):
        return ".pdf"
    if normalized in {"application/json", "text/json"} or normalized.endswith("+json"):
        return ".json"
    if "html" in normalized:
        return ".html"
    if normalized.startswith("text/"):
        return ".txt"
    return ".bin"


def document_text(document: SourceDocument) -> tuple[str | None, str | None]:
    """Return retained text for review without inferring numeric values from prose."""

    content_type = document.content_type.lower().split(";", 1)[0]
    if content_type == "application/vnd.ak-trading.websearch-scrape+json":
        try:
            payload = json.loads(document.content)
        except json.JSONDecodeError:
            return None, "normalized local-stack evidence is invalid JSON"
        markdown = payload.get("markdown") if isinstance(payload, Mapping) else None
        if not isinstance(markdown, str) or not markdown.strip():
            return None, "normalized local-stack evidence has no readable content"
        return markdown, None
    if "html" in content_type:
        parser = _TextExtractor()
        parser.feed(document.content)
        return parser.text or None, None if parser.text else "html contains no text"
    return document.content or None, None if document.content else "source contains no text"


def _rule_value_kind(raw_rule: Mapping[str, Any]) -> RuleValueKind:
    raw_value = raw_rule.get("value")
    raw_kind = raw_rule.get("value_kind")
    if raw_value is None:
        try:
            return RuleValueKind(raw_kind or RuleValueKind.UNKNOWN.value)
        except (TypeError, ValueError) as exc:
            raise ExchangeRuleSourceError("structured rule has an invalid value_kind") from exc
    try:
        numeric = Decimal(str(raw_value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ExchangeRuleSourceError("structured rule numeric value is invalid") from exc
    if not numeric.is_finite() or numeric < 0:
        raise ExchangeRuleSourceError("structured rule numeric value is invalid")
    default = (
        RuleValueKind.EXPLICIT_ZERO.value if numeric == 0 else RuleValueKind.DECIMAL.value
    )
    try:
        value_kind = RuleValueKind(raw_kind or default)
    except (TypeError, ValueError) as exc:
        raise ExchangeRuleSourceError("structured rule has an invalid value_kind") from exc
    if value_kind in {RuleValueKind.UNKNOWN, RuleValueKind.TEXT}:
        raise ExchangeRuleSourceError("numeric structured rule cannot be text or unknown")
    if value_kind is RuleValueKind.EXPLICIT_ZERO and numeric != 0:
        raise ExchangeRuleSourceError("explicit-zero structured rule must be zero")
    if value_kind is RuleValueKind.DECIMAL and numeric == 0:
        raise ExchangeRuleSourceError("zero structured rule must be explicit_zero")
    return value_kind


def parse_structured_rule_document(
    document: SourceDocument,
    *,
    product: str,
    contract: str | None,
) -> tuple[RuleItem, ...]:
    """Parse an explicit normalized rule JSON fixture or official data endpoint.

    Arbitrary HTML, PDF, spreadsheet, and notice prose remains evidence for a
    human reviewer. This parser deliberately does not use regular expressions to
    manufacture numeric exchange facts from Chinese text.
    """

    content_type = document.content_type.lower().split(";", 1)[0]
    if content_type not in {
        "application/json",
        "text/json",
        "application/vnd.ak-trading.exchange-rules+json",
    }:
        return ()
    try:
        payload = json.loads(document.content)
    except json.JSONDecodeError:
        return ()
    raw_rules = (
        payload.get("rules") or payload.get("exchange_rules")
        if isinstance(payload, Mapping)
        else payload
    )
    if not isinstance(raw_rules, list):
        return ()

    items: list[RuleItem] = []
    for raw_rule in raw_rules:
        if not isinstance(raw_rule, Mapping):
            continue
        raw_product = str(raw_rule.get("product") or product).upper()
        raw_contract_value = raw_rule.get("contract", contract)
        if raw_contract_value is not None and not isinstance(raw_contract_value, str):
            raise ExchangeRuleSourceError("structured rule contract must be a string or null")
        raw_contract = raw_contract_value.strip() if isinstance(raw_contract_value, str) else None
        if raw_product != product.upper() or raw_contract != contract:
            continue
        try:
            category = RuleCategory(raw_rule.get("category"))
            status = RuleStatus(raw_rule.get("status", RuleStatus.CANDIDATE.value))
        except (TypeError, ValueError) as exc:
            raise ExchangeRuleSourceError(
                "structured rule has an invalid category or status"
            ) from exc
        if status not in {RuleStatus.CANDIDATE, RuleStatus.REVIEW_REQUIRED}:
            raise ExchangeRuleSourceError(
                "retrieved rule data may only create candidate or review-required values"
            )
        value_kind = _rule_value_kind(raw_rule)
        field = _nonempty(raw_rule.get("field"), "structured rule field")
        unit = raw_rule.get("unit")
        if unit is not None and not isinstance(unit, str):
            raise ExchangeRuleSourceError("structured rule unit must be a string or null")
        if category is RuleCategory.FEE and value_kind in {
            RuleValueKind.DECIMAL,
            RuleValueKind.EXPLICIT_ZERO,
        }:
            try:
                FeeBasis(unit)
            except (TypeError, ValueError) as exc:
                raise ExchangeRuleSourceError(
                    "numeric fee rules require cny_per_lot or rate_per_notional"
                ) from exc
        if category in {RuleCategory.MARGIN, RuleCategory.LOCK_MARGIN} and value_kind in {
            RuleValueKind.DECIMAL,
            RuleValueKind.EXPLICIT_ZERO,
        }:
            numeric = Decimal(str(raw_rule.get("value")))
            if numeric > 1:
                raise ExchangeRuleSourceError(
                    "numeric margin rules must be ratios, not percentages"
                )
        effective_from = raw_rule.get("effective_from")
        if effective_from is None and document.effective_date is not None:
            effective_from = document.effective_date.isoformat()
        items.append(
            RuleItem.from_dict(
                {
                    "exchange": document.exchange,
                    "product": product.upper(),
                    "contract": contract,
                    "category": category.value,
                    "field": field,
                    "value_kind": value_kind.value,
                    "status": status.value,
                    "value": raw_rule.get("value"),
                    "unit": unit,
                    "applies_to": raw_rule.get("applies_to", contract or product.upper()),
                    "effective_from": effective_from,
                    "effective_through": raw_rule.get("effective_through"),
                    "source_document_id": document.document_id,
                    "source_url": document.url,
                    "excerpt": raw_rule.get("excerpt")
                    or json.dumps(raw_rule, ensure_ascii=False, sort_keys=True),
                    "note": raw_rule.get("note"),
                }
            )
        )
    return tuple(items)


_REQUIRED_FIELDS: tuple[tuple[RuleCategory, str], ...] = (
    (RuleCategory.FEE, "open"),
    (RuleCategory.FEE, "close"),
    (RuleCategory.FEE, "close_today"),
    (RuleCategory.MARGIN, "ordinary_rate"),
    (RuleCategory.LOCK_MARGIN, "same_contract"),
    (RuleCategory.LIMIT, "price_and_position_limits"),
    (RuleCategory.SESSION, "trading_sessions"),
    (RuleCategory.DELIVERY, "delivery_and_closeout"),
    (RuleCategory.RISK, "risk_controls_and_forced_liquidation"),
)


def fill_required_unknowns(
    *,
    exchange: str,
    product: str,
    contract: str | None,
    items: Iterable[RuleItem],
    document: SourceDocument | None = None,
    documents: Sequence[SourceDocument] = (),
    target_day: date | None = None,
    product_name: str | None = None,
    product_names: Sequence[str] | None = None,
    preferred_document_urls: frozenset[str] | None = None,
) -> tuple[RuleItem, ...]:
    """Extract safe missing fields, then fill the remainder as explicit unknowns."""

    result = list(items)
    existing = {(item.category, item.field) for item in result}
    retained_documents = list(documents)
    if document is not None and all(
        retained.document_id != document.document_id
        for retained in retained_documents
    ):
        retained_documents.append(document)
    retained_documents.sort(key=lambda retained: retained.document_id)
    document_by_id = {
        retained.document_id: retained for retained in retained_documents
    }
    extracted = {
        item.identity: item
        for item in extract_rule_values(
            retained_documents,
            product=product,
            target_day=target_day,
            product_name=product_name,
            product_names=product_names,
            preferred_document_urls=preferred_document_urls,
        )
    }

    for category, field in _REQUIRED_FIELDS:
        if (category, field) in existing:
            continue
        extracted_item = extracted.get((category, field))
        if extracted_item is not None:
            source_document = document_by_id[extracted_item.source_document_id]
            result.append(
                _rule_item_from_extraction(
                    extracted_item,
                    exchange=exchange,
                    product=product,
                    contract=contract,
                    effective_from=(
                        extracted_item.effective_date
                        or source_document.effective_date
                    ),
                )
            )
            continue
        retained_document = retained_documents[0] if retained_documents else None
        if retained_document is None:
            excerpt = "No official document was collected for this required rule category."
            document_id = None
            url = None
        else:
            excerpt = (
                "Official document was retained, but no deterministic structured "
                f"{category.value}/{field} value was parsed. Manual review required."
            )
            document_id = retained_document.document_id
            url = retained_document.url
        result.append(
            RuleItem(
                exchange=exchange,
                product=product,
                contract=contract,
                category=category,
                field=field,
                value_kind=RuleValueKind.UNKNOWN,
                status=RuleStatus.REVIEW_REQUIRED,
                value=None,
                unit=None,
                applies_to=contract or product,
                effective_from=None,
                effective_through=None,
                source_document_id=document_id,
                source_url=url,
                excerpt=excerpt,
            )
        )
    return tuple(
        sorted(result, key=lambda item: rule_identity_sort_key(item.identity_key))
    )


def _rule_item_from_extraction(
    extracted: ExtractedRuleValue,
    *,
    exchange: str,
    product: str,
    contract: str | None,
    effective_from: date | None,
) -> RuleItem:
    """Adapt text values to RuleItem's established TEXT/value-none convention."""

    numeric_value = extracted.value if isinstance(extracted.value, Decimal) else None
    return RuleItem(
        exchange=exchange,
        product=product,
        contract=contract,
        category=extracted.category,
        field=extracted.field,
        value_kind=extracted.value_kind,
        status=extracted.status,
        value=numeric_value,
        unit=extracted.unit,
        applies_to=contract or product,
        effective_from=effective_from,
        effective_through=None,
        source_document_id=extracted.source_document_id,
        source_url=extracted.source_url,
        excerpt=extracted.excerpt,
        note=extracted.note,
    )


def _item_signature(item: RuleItem) -> tuple[object, ...]:
    return (
        item.value_kind,
        item.status,
        item.value,
        item.unit,
        item.applies_to,
        item.effective_from,
        item.effective_through,
    )


def _consolidate_items(
    *,
    exchange: str,
    product: ProductSource,
    items: Iterable[RuleItem],
    review_reasons: list[str],
    target_day: date | None = None,
) -> tuple[RuleItem, ...]:
    grouped: dict[tuple[str, str, str | None, str, str], list[RuleItem]] = {}
    for item in items:
        grouped.setdefault(item.identity_key, []).append(item)
    result: list[RuleItem] = []
    for identity, candidates in sorted(grouped.items()):
        ordered = sorted(candidates, key=lambda item: item.source_document_id or "")
        signatures = {_item_signature(item) for item in ordered}
        if len(signatures) == 1:
            result.append(ordered[0])
            continue
        if target_day is not None and all(
            item.effective_from is not None for item in ordered
        ):
            eligible = [
                item
                for item in ordered
                if item.effective_from is not None
                and item.effective_from <= target_day
                and (
                    item.effective_through is None
                    or target_day <= item.effective_through
                )
            ]
            if eligible:
                latest_date = max(
                    item.effective_from for item in eligible if item.effective_from
                )
                latest = [
                    item for item in eligible if item.effective_from == latest_date
                ]
                if len({_item_signature(item) for item in latest}) == 1:
                    evidence = "\n".join(
                        f"[{item.source_document_id or 'unattributed'}; "
                        f"effective_on={item.effective_from.isoformat() if item.effective_from else 'unknown'}] "
                        f"{item.excerpt}"
                        for item in ordered
                    )
                    result.append(
                        replace(
                            sorted(
                                latest,
                                key=lambda item: (
                                    item.source_document_id or "",
                                    item.excerpt,
                                ),
                            )[0],
                            note=(
                                "date-aware conflict resolved; selected "
                                f"effective_on={latest_date.isoformat()}\n{evidence}"
                            ),
                        )
                    )
                    continue
        category = RuleCategory(identity[3])
        field = identity[4]
        sources = "\n".join(
            f"[{item.source_document_id or 'unattributed'}; effective_on="
            f"{item.effective_from.isoformat() if item.effective_from else 'unknown'}] "
            f"{item.excerpt}"
            for item in ordered
        )
        review_reasons.append(
            f"{product.code}: conflicting candidate values for {category.value}/{field}"
        )
        result.append(
            RuleItem(
                exchange=exchange,
                product=product.code,
                contract=product.contract,
                category=category,
                field=field,
                value_kind=RuleValueKind.UNKNOWN,
                status=RuleStatus.REVIEW_REQUIRED,
                value=None,
                unit=None,
                applies_to=product.contract or product.code,
                effective_from=None,
                effective_through=None,
                source_document_id=ordered[0].source_document_id,
                source_url=ordered[0].source_url,
                excerpt=(
                    "Conflicting deterministic candidate values were retained in "
                    f"official documents:\n{sources}\nManual review required."
                ),
                note=(
                    "conflict: deterministic extraction found differing retained "
                    f"values:\n{sources}"
                ),
            )
        )
    return tuple(result)


def fixture_documents(
    fixture_path: str | Path,
    *,
    source: ExchangeSource,
    collected_at: datetime,
) -> tuple[tuple[DiscoveredDocument, FetchedDocument], ...]:
    """Load deterministic official-document fixtures without network access."""

    if collected_at.tzinfo is None:
        raise ExchangeRuleSourceError("fixture collected_at must be timezone-aware")
    path = Path(fixture_path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExchangeRuleSourceError(f"cannot load source fixture: {path}") from exc
    raw_documents = raw.get("documents") if isinstance(raw, Mapping) else None
    if not isinstance(raw_documents, list):
        raise ExchangeRuleSourceError("source fixture must contain a documents list")
    products = {product.code.upper(): product for product in source.products}
    result: list[tuple[DiscoveredDocument, FetchedDocument]] = []
    for index, raw_document in enumerate(raw_documents):
        if not isinstance(raw_document, Mapping):
            raise ExchangeRuleSourceError(f"fixture documents[{index}] must be an object")
        product_code = _nonempty(raw_document.get("product"), "fixture.product").upper()
        product = products.get(product_code)
        if product is None:
            raise ExchangeRuleSourceError(
                f"fixture product {product_code!r} is not configured"
            )
        raw_contract = raw_document.get("contract", product.contract)
        if raw_contract is not None and not isinstance(raw_contract, str):
            raise ExchangeRuleSourceError("fixture.contract must be a string or null")
        contract = raw_contract.strip() if isinstance(raw_contract, str) else None
        if contract != product.contract:
            raise ExchangeRuleSourceError(
                f"fixture contract for {product_code!r} does not match configured scope"
            )
        url = _nonempty(raw_document.get("url"), "fixture.url")
        require_official_url(url, source.official_domains)
        final_url = _nonempty(raw_document.get("final_url", url), "fixture.final_url")
        require_official_url(final_url, source.official_domains)
        content_value = raw_document.get("content")
        if isinstance(content_value, (Mapping, list)):
            content = json.dumps(
                content_value, ensure_ascii=False, sort_keys=True
            ).encode("utf-8")
        elif isinstance(content_value, str):
            content = content_value.encode("utf-8")
        else:
            raise ExchangeRuleSourceError(
                "fixture content must be a string, object, or list"
            )
        content_type = _nonempty(
            raw_document.get("content_type", "application/json"),
            "fixture.content_type",
        )
        fetched = FetchedDocument(
            url=final_url,
            title=_nonempty(raw_document.get("title", final_url), "fixture.title"),
            content=content,
            content_type=content_type,
            retrieved_at=collected_at,
            publication_date=_optional_date(
                raw_document.get("publication_date"), "fixture.publication_date"
            ),
            effective_date=_optional_date(
                raw_document.get("effective_date"), "fixture.effective_date"
            ),
        )
        result.append(
            (
                DiscoveredDocument(
                    exchange=source.code,
                    product=product.code.upper(),
                    contract=contract,
                    url=url,
                    title=fetched.title,
                    description="fixture",
                    discovery_method="fixture",
                ),
                fetched,
            )
        )
    return tuple(result)


def collect_evidence(
    source: ExchangeSource,
    discovered: Sequence[DiscoveredDocument],
    *,
    fetcher: Callable[[str], FetchedDocument],
    content_path_builder: Callable[[str, str], str],
    discovery_errors: Iterable[str] = (),
    target_day: date | None = None,
    shared_documents: Sequence[SourceDocument] = (),
) -> CollectionEvidence:
    """Fetch, retain, and conservatively parse one exchange's official documents."""

    documents: dict[str, SourceDocument] = {}
    by_product: dict[str, list[SourceDocument]] = {
        product.code.upper(): [] for product in source.products
    }
    for shared in shared_documents:
        documents[shared.document_id] = shared
        for product_documents in by_product.values():
            if all(
                existing.document_id != shared.document_id
                for existing in product_documents
            ):
                product_documents.append(shared)
    errors = list(discovery_errors)
    for discovered_document in discovered:
        if discovered_document.exchange != source.code:
            continue
        try:
            require_official_url(discovered_document.url, source.official_domains)
            fetched = fetcher(discovered_document.url)
            require_official_url(fetched.url, source.official_domains)
            preliminary = SourceDocument.from_fetched(
                fetched,
                exchange=source.code,
                content_path="pending",
            )
            document = SourceDocument.from_fetched(
                fetched,
                exchange=source.code,
                content_path=content_path_builder(
                    preliminary.content_sha256,
                    document_suffix(fetched.content_type, fetched.url),
                ),
            )
        except (
            DocumentFetchError,
            DomainNotAllowedError,
            ExchangeRuleError,
            ExchangeRuleSourceError,
        ) as exc:
            errors.append(
                f"{discovered_document.product}: {type(exc).__name__}: {exc}"
            )
            continue
        documents[document.document_id] = document
        product_documents = by_product.setdefault(discovered_document.product.upper(), [])
        if all(existing.document_id != document.document_id for existing in product_documents):
            product_documents.append(document)

    parsed_items: list[RuleItem] = []
    review_reasons: list[str] = []
    for product in source.products:
        product_documents = sorted(
            by_product.get(product.code.upper(), []),
            key=lambda document: document.document_id,
        )
        product_items: list[RuleItem] = []
        for document in product_documents:
            try:
                parsed_for_document = parse_structured_rule_document(
                    document,
                    product=product.code,
                    contract=product.contract,
                )
            except ExchangeRuleSourceError as exc:
                parsed_for_document = ()
                review_reasons.append(
                    f"{product.code}: structured parser rejected document: {exc}"
                )
            text, text_error = document_text(document)
            if text_error:
                review_reasons.append(f"{product.code}: {text_error}")
            if not parsed_for_document:
                detail = "no readable text" if text is None else "manual rule extraction required"
                review_reasons.append(
                    f"{product.code}: official document retained but {detail}"
                )
            product_items.extend(parsed_for_document)
        consolidated = _consolidate_items(
            exchange=source.code,
            product=product,
            items=product_items,
            review_reasons=review_reasons,
            target_day=target_day,
        )
        completed_items = fill_required_unknowns(
            exchange=source.code,
            product=product.code,
            contract=product.contract,
            items=consolidated,
            documents=product_documents,
            target_day=target_day,
            product_name=product.name_zh,
            product_names=tuple(
                sorted(
                    {
                        item.name_zh
                        for item in source.products
                        if item.name_zh
                    }
                )
            ),
            preferred_document_urls=frozenset(product.seed_urls),
        )
        parsed_items.extend(completed_items)
        review_reasons.extend(
            f"{product.code}: {item.note}"
            for item in completed_items
            if item.status is RuleStatus.REVIEW_REQUIRED
            and item.note is not None
            and item.note.startswith("conflict: deterministic extraction")
        )
    extracted_count = sum(
        item.status is RuleStatus.CANDIDATE
        and isinstance(item.note, str)
        and item.note.startswith(AUTO_EXTRACTION_NOTE)
        for item in parsed_items
    )
    required_count = len(source.products) * len(_REQUIRED_FIELDS)
    review_reasons.append(
        f"{source.code}: deterministic extraction extracted {extracted_count} of "
        f"{required_count} required fields for this exchange "
        "(task-card catalog denominator: 729 fields)."
    )
    return CollectionEvidence(
        documents=tuple(sorted(documents.values(), key=lambda document: document.document_id)),
        items=tuple(
            sorted(parsed_items, key=lambda item: rule_identity_sort_key(item.identity_key))
        ),
        errors=tuple(errors),
        review_reasons=tuple(dict.fromkeys(review_reasons)),
    )
