"""Officially scoped catalog of mainland Chinese futures products.

The catalog is deliberately separate from the legacy commodity reference book. It
records the exchange directory boundaries, futures-only product universe, explicit
option exclusions, and per-exchange calendar coverage used by rule collection.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse


SUPPORTED_EXCHANGES = ("SHFE", "INE", "DCE", "CZCE", "CFFEX", "GFEX")


class FuturesCatalogError(ValueError):
    """Raised when the official futures catalog is malformed or incomplete."""


class InstrumentKind(str, Enum):
    FUTURE = "future"
    OPTION = "option"
    INDEX = "index"
    UNKNOWN = "unknown"


class CatalogProductStatus(str, Enum):
    LISTED = "listed"
    ACTIVE = "active"
    INACTIVE = "inactive"
    REVIEW_REQUIRED = "review_required"


class CatalogExcludedStatus(str, Enum):
    EXCLUDED_OPTION = "excluded_option"


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FuturesCatalogError(f"{field} must be a non-empty string")
    return value.strip()


def _url(value: object, field: str) -> str:
    result = _nonempty(value, field)
    parsed = urlparse(result)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise FuturesCatalogError(f"{field} must be an HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise FuturesCatalogError(f"{field} must not contain credentials")
    return result


def _domain(value: object, field: str) -> str:
    result = _nonempty(value, field).lower().strip(".")
    if (
        not result
        or any(character.isspace() for character in result)
        or any(character in result for character in "/:@")
    ):
        raise FuturesCatalogError(f"{field} must be a hostname")
    return result


def _official_url(value: object, domains: Iterable[str], field: str) -> str:
    result = _url(value, field)
    hostname = (urlparse(result).hostname or "").lower().strip(".")
    allowed = tuple(_domain(domain, f"{field}.official_domain") for domain in domains)
    if not allowed or not any(
        hostname == domain or hostname.endswith(f".{domain}") for domain in allowed
    ):
        raise FuturesCatalogError(f"{field} must belong to an official exchange domain")
    return result


def _date(value: object, field: str) -> date:
    if not isinstance(value, str):
        raise FuturesCatalogError(f"{field} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise FuturesCatalogError(f"{field} must be YYYY-MM-DD") from exc


def _datetime(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise FuturesCatalogError(f"{field} must be an ISO datetime")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise FuturesCatalogError(f"{field} must be an ISO datetime") from exc
    if result.tzinfo is None:
        raise FuturesCatalogError(f"{field} must include a timezone offset")
    return result


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise FuturesCatalogError("catalog payload is not JSON serializable") from exc


def _sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _parse_spec_urls(value: object, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    # An empty list round-trips from the canonical dump of products without
    # additional spec pages; treat it the same as an absent field.
    if not isinstance(value, list):
        raise FuturesCatalogError(f"{field} must be a list when present")
    return tuple(
        _url(item, f"{field}[{index}]") for index, item in enumerate(value)
    )


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        char not in "0123456789abcdef" for char in value
    ):
        raise FuturesCatalogError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _domains(values: object, field: str) -> tuple[str, ...]:
    if not isinstance(values, list) or not values:
        raise FuturesCatalogError(f"{field} must be a non-empty list")
    normalized = tuple(
        _domain(item, f"{field}[{index}]") for index, item in enumerate(values)
    )
    if len(set(normalized)) != len(normalized):
        raise FuturesCatalogError(f"{field} must be unique")
    return normalized


@dataclass(frozen=True)
class CatalogSource:
    """One exchange's official product-directory and calendar boundaries."""

    exchange: str
    official_domains: tuple[str, ...]
    catalog_url: str
    calendar_url: str
    rule_query_template: str

    def __post_init__(self) -> None:
        exchange = _nonempty(self.exchange, "catalog_source.exchange").upper()
        if exchange not in SUPPORTED_EXCHANGES:
            raise FuturesCatalogError(f"unsupported mainland futures exchange: {exchange}")
        domains = tuple(
            _domain(domain, "catalog_source.official_domain")
            for domain in self.official_domains
        )
        if not domains or len(set(domains)) != len(domains):
            raise FuturesCatalogError("catalog_source.official_domains must be unique")
        _official_url(
            self.catalog_url, domains, "catalog_source.catalog_url"
        )
        _official_url(
            self.calendar_url, domains, "catalog_source.calendar_url"
        )
        _nonempty(self.rule_query_template, "catalog_source.rule_query_template")

    def as_dict(self) -> dict[str, Any]:
        return {
            "exchange": self.exchange.upper(),
            "official_domains": list(self.official_domains),
            "catalog_url": self.catalog_url,
            "calendar_url": self.calendar_url,
            "rule_query_template": self.rule_query_template,
        }


@dataclass(frozen=True)
class FuturesProduct:
    """A listed futures product from one official exchange directory."""

    exchange: str
    code: str
    name_zh: str
    instrument_kind: InstrumentKind
    status: CatalogProductStatus
    source_url: str
    spec_urls: tuple[str, ...] = ()
    representative_contract: str | None = None
    contract_pattern: str | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        exchange = _nonempty(self.exchange, "catalog_product.exchange").upper()
        if exchange not in SUPPORTED_EXCHANGES:
            raise FuturesCatalogError(f"unsupported product exchange: {exchange}")
        code = _nonempty(self.code, "catalog_product.code").upper()
        if len(code) > 32:
            raise FuturesCatalogError("catalog_product.code exceeds the persistence limit")
        _nonempty(self.name_zh, "catalog_product.name_zh")
        if self.instrument_kind is not InstrumentKind.FUTURE:
            raise FuturesCatalogError(
                f"only future products may enter the rule universe: {exchange}/{code}"
            )
        if not isinstance(self.status, CatalogProductStatus):
            raise FuturesCatalogError("catalog_product.status is invalid")
        _url(self.source_url, "catalog_product.source_url")
        for spec_index, spec_url in enumerate(self.spec_urls):
            _url(spec_url, f"catalog_product.spec_urls[{spec_index}]")
        if len(set(self.spec_urls)) != len(self.spec_urls) or (
            self.source_url in self.spec_urls
        ):
            raise FuturesCatalogError(
                "catalog_product.spec_urls must be unique and exclude source_url"
            )
        if self.representative_contract is not None:
            _nonempty(self.representative_contract, "catalog_product.representative_contract")
        if self.contract_pattern is not None:
            _nonempty(self.contract_pattern, "catalog_product.contract_pattern")

    @property
    def identity(self) -> tuple[str, str]:
        return self.exchange.upper(), self.code.upper()

    def as_dict(self) -> dict[str, Any]:
        return {
            "exchange": self.exchange.upper(),
            "code": self.code.upper(),
            "name_zh": self.name_zh,
            "instrument_kind": self.instrument_kind.value,
            "status": self.status.value,
            "source_url": self.source_url,
            "spec_urls": list(self.spec_urls),
            "representative_contract": self.representative_contract,
            "contract_pattern": self.contract_pattern,
            "note": self.note,
        }


@dataclass(frozen=True)
class ExcludedOption:
    """An option family explicitly observed and excluded from this workflow."""

    exchange: str
    code: str
    name_zh: str
    source_url: str
    reason: str
    status: CatalogExcludedStatus = CatalogExcludedStatus.EXCLUDED_OPTION

    def __post_init__(self) -> None:
        exchange = _nonempty(self.exchange, "excluded_option.exchange").upper()
        if exchange not in SUPPORTED_EXCHANGES:
            raise FuturesCatalogError(f"unsupported excluded-option exchange: {exchange}")
        _nonempty(self.code, "excluded_option.code")
        _nonempty(self.name_zh, "excluded_option.name_zh")
        _url(self.source_url, "excluded_option.source_url")
        _nonempty(self.reason, "excluded_option.reason")

    def as_dict(self) -> dict[str, Any]:
        return {
            "exchange": self.exchange.upper(),
            "code": self.code.upper(),
            "name_zh": self.name_zh,
            "instrument_kind": InstrumentKind.OPTION.value,
            "status": self.status.value,
            "source_url": self.source_url,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ExchangeCalendar:
    """Calendar coverage owned by one exchange source."""

    exchange: str
    valid_from: date
    valid_through: date
    closed_dates: frozenset[date]
    source_url: str
    status: str = "review_required"

    def __post_init__(self) -> None:
        exchange = _nonempty(self.exchange, "calendar.exchange").upper()
        if exchange not in SUPPORTED_EXCHANGES:
            raise FuturesCatalogError(f"unsupported calendar exchange: {exchange}")
        if self.valid_through < self.valid_from:
            raise FuturesCatalogError("calendar.valid_through precedes valid_from")
        if any(day < self.valid_from or day > self.valid_through for day in self.closed_dates):
            raise FuturesCatalogError("calendar.closed_dates must be within coverage")
        _url(self.source_url, "calendar.source_url")
        if self.status not in {"current", "review_required"}:
            raise FuturesCatalogError("calendar.status is invalid")

    def is_trading_day(self, value: date) -> bool:
        self.require_covered(value)
        return value.weekday() < 5 and value not in self.closed_dates

    def require_covered(self, value: date) -> None:
        if value < self.valid_from or value > self.valid_through:
            raise FuturesCatalogError(
                f"{self.exchange} trading day {value} is outside calendar coverage "
                f"{self.valid_from}..{self.valid_through}"
            )

    def next_trading_day(self, value: date, *, include_current: bool = False) -> date:
        candidate = value if include_current else value + timedelta(days=1)
        while True:
            self.require_covered(candidate)
            if self.is_trading_day(candidate):
                return candidate
            candidate += timedelta(days=1)

    def target_trading_day(self, local_now: datetime) -> date:
        if local_now.tzinfo is None:
            raise FuturesCatalogError("catalog clock must be timezone-aware")
        candidate = local_now.date()
        self.require_covered(candidate)
        if local_now.time() < time(15, 0) and self.is_trading_day(candidate):
            return candidate
        return self.next_trading_day(candidate)

    def as_dict(self) -> dict[str, Any]:
        return {
            "exchange": self.exchange.upper(),
            "valid_from": self.valid_from.isoformat(),
            "valid_through": self.valid_through.isoformat(),
            "closed_dates": sorted(day.isoformat() for day in self.closed_dates),
            "source_url": self.source_url,
            "status": self.status,
        }


@dataclass(frozen=True)
class FuturesCatalog:
    """Immutable, hash-identified futures universe and source manifest."""

    observed_at: datetime
    sources: tuple[CatalogSource, ...]
    calendars: tuple[ExchangeCalendar, ...]
    products: tuple[FuturesProduct, ...]
    excluded_options: tuple[ExcludedOption, ...]
    catalog_id: str
    review_reason: str | None = None

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise FuturesCatalogError("catalog.observed_at must be timezone-aware")
        source_codes = tuple(source.exchange.upper() for source in self.sources)
        if not source_codes:
            raise FuturesCatalogError("catalog must contain at least one exchange source")
        if len(set(source_codes)) != len(source_codes):
            raise FuturesCatalogError("catalog sources must have unique exchanges")
        unsupported_sources = set(source_codes) - set(SUPPORTED_EXCHANGES)
        if unsupported_sources:
            raise FuturesCatalogError(
                f"unsupported catalog exchanges: {sorted(unsupported_sources)}"
            )
        calendar_codes = tuple(calendar.exchange.upper() for calendar in self.calendars)
        if set(calendar_codes) != set(source_codes) or len(calendar_codes) != len(source_codes):
            raise FuturesCatalogError(
                "catalog must contain exactly one calendar per exchange source"
            )
        product_keys = [product.identity for product in self.products]
        if len(set(product_keys)) != len(product_keys):
            raise FuturesCatalogError("futures catalog product identities must be unique")
        source_set = set(source_codes)
        if any(product.exchange.upper() not in source_set for product in self.products):
            raise FuturesCatalogError("every futures product must have an exchange source")
        source_by_exchange = {
            source.exchange.upper(): source for source in self.sources
        }
        calendar_by_exchange = {
            calendar.exchange.upper(): calendar for calendar in self.calendars
        }
        for exchange, source in source_by_exchange.items():
            _official_url(
                source.catalog_url,
                source.official_domains,
                f"catalog source {exchange} catalog_url",
            )
            _official_url(
                source.calendar_url,
                source.official_domains,
                f"catalog source {exchange} calendar_url",
            )
            _official_url(
                calendar_by_exchange[exchange].source_url,
                source.official_domains,
                f"calendar {exchange} source_url",
            )
        for product in self.products:
            domains = source_by_exchange[
                product.exchange.upper()
            ].official_domains
            _official_url(
                product.source_url,
                domains,
                f"catalog product {product.exchange}/{product.code} source_url",
            )
            for spec_index, spec_url in enumerate(product.spec_urls):
                _official_url(
                    spec_url,
                    domains,
                    f"catalog product {product.exchange}/{product.code} "
                    f"spec_urls[{spec_index}]",
                )
        excluded_keys = [
            (item.exchange.upper(), item.code.upper()) for item in self.excluded_options
        ]
        if len(set(excluded_keys)) != len(excluded_keys):
            raise FuturesCatalogError("excluded option identities must be unique")
        for item in self.excluded_options:
            _official_url(
                item.source_url,
                source_by_exchange[item.exchange.upper()].official_domains,
                f"excluded option {item.exchange}/{item.code} source_url",
            )
        _digest(self.catalog_id, "catalog_id")
        if self.review_reason is not None:
            _nonempty(self.review_reason, "catalog.review_reason")

    @property
    def future_products(self) -> tuple[FuturesProduct, ...]:
        return tuple(sorted(self.products, key=lambda item: item.identity))

    @property
    def exchanges(self) -> tuple[str, ...]:
        configured = {source.exchange.upper() for source in self.sources}
        return tuple(exchange for exchange in SUPPORTED_EXCHANGES if exchange in configured)

    def products_for_exchange(self, exchange: str) -> tuple[FuturesProduct, ...]:
        wanted = _nonempty(exchange, "exchange").upper()
        return tuple(product for product in self.future_products if product.exchange.upper() == wanted)

    def source_for(self, exchange: str) -> CatalogSource:
        wanted = _nonempty(exchange, "exchange").upper()
        for source in self.sources:
            if source.exchange.upper() == wanted:
                return source
        raise FuturesCatalogError(f"unknown catalog exchange: {wanted}")

    def calendar_for(self, exchange: str) -> ExchangeCalendar:
        wanted = _nonempty(exchange, "exchange").upper()
        for calendar in self.calendars:
            if calendar.exchange.upper() == wanted:
                return calendar
        raise FuturesCatalogError(f"unknown calendar exchange: {wanted}")

    def target_trading_day(self, local_now: datetime) -> date:
        targets = {calendar.target_trading_day(local_now) for calendar in self.calendars}
        if len(targets) != 1:
            raise FuturesCatalogError(
                "exchange calendars derive different target trading days; pass --trading-day explicitly"
            )
        return next(iter(targets))

    def coverage_manifest(self) -> dict[str, Any]:
        return {
            "catalog_id": self.catalog_id,
            "observed_at": self.observed_at.isoformat(),
            "expected_futures": [
                {"exchange": exchange, "product": product}
                for exchange, product in (item.identity for item in self.future_products)
            ],
            "excluded_options": [item.as_dict() for item in self.excluded_options],
            "exchange_count": len(self.exchanges),
            "future_product_count": len(self.products),
            "excluded_option_count": len(self.excluded_options),
            "review_reason": self.review_reason,
        }

    def content_without_id(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "observed_at": self.observed_at.isoformat(),
            "sources": [source.as_dict() for source in sorted(self.sources, key=lambda item: item.exchange)],
            "calendars": [calendar.as_dict() for calendar in sorted(self.calendars, key=lambda item: item.exchange)],
            "products": [product.as_dict() for product in self.future_products],
            "excluded_options": [item.as_dict() for item in sorted(self.excluded_options, key=lambda item: (item.exchange, item.code))],
            "review_reason": self.review_reason,
        }

    def as_dict(self) -> dict[str, Any]:
        result = self.content_without_id()
        result["catalog_id"] = self.catalog_id
        return result

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any],
        *,
        require_all_exchanges: bool = False,
    ) -> "FuturesCatalog":
        if not isinstance(raw, Mapping) or raw.get("schema_version") != 1:
            raise FuturesCatalogError("unsupported futures catalog schema_version")
        observed_at = _datetime(raw.get("observed_at"), "catalog.observed_at")
        raw_sources = raw.get("sources")
        raw_calendars = raw.get("calendars")
        raw_products = raw.get("products")
        raw_excluded = raw.get("excluded_options", [])
        if not isinstance(raw_sources, list) or not raw_sources:
            raise FuturesCatalogError("catalog.sources must be a non-empty list")
        if not isinstance(raw_calendars, list) or not raw_calendars:
            raise FuturesCatalogError("catalog.calendars must be a non-empty list")
        if not isinstance(raw_products, list):
            raise FuturesCatalogError("catalog.products must be a list")
        if not isinstance(raw_excluded, list):
            raise FuturesCatalogError("catalog.excluded_options must be a list")

        sources = tuple(
            CatalogSource(
                exchange=_nonempty(item.get("exchange"), f"sources[{index}].exchange").upper(),
                official_domains=_domains(item.get("official_domains"), f"sources[{index}].official_domains"),
                catalog_url=_url(item.get("catalog_url"), f"sources[{index}].catalog_url"),
                calendar_url=_url(item.get("calendar_url"), f"sources[{index}].calendar_url"),
                rule_query_template=_nonempty(item.get("rule_query_template"), f"sources[{index}].rule_query_template"),
            )
            for index, item in enumerate(raw_sources)
            if isinstance(item, Mapping)
        )
        if len(sources) != len(raw_sources):
            raise FuturesCatalogError("each catalog source must be an object")
        calendars: list[ExchangeCalendar] = []
        for index, item in enumerate(raw_calendars):
            if not isinstance(item, Mapping):
                raise FuturesCatalogError(f"calendars[{index}] must be an object")
            raw_closed = item.get("closed_dates", [])
            if not isinstance(raw_closed, list):
                raise FuturesCatalogError(f"calendars[{index}].closed_dates must be a list")
            calendars.append(
                ExchangeCalendar(
                    exchange=_nonempty(item.get("exchange"), f"calendars[{index}].exchange").upper(),
                    valid_from=_date(item.get("valid_from"), f"calendars[{index}].valid_from"),
                    valid_through=_date(item.get("valid_through"), f"calendars[{index}].valid_through"),
                    closed_dates=frozenset(_date(value, f"calendars[{index}].closed_dates") for value in raw_closed),
                    source_url=_url(item.get("source_url"), f"calendars[{index}].source_url"),
                    status=str(item.get("status", "review_required")),
                )
            )
        products: list[FuturesProduct] = []
        for index, item in enumerate(raw_products):
            if not isinstance(item, Mapping):
                raise FuturesCatalogError(f"products[{index}] must be an object")
            try:
                kind = InstrumentKind(item.get("instrument_kind", InstrumentKind.FUTURE.value))
                status = CatalogProductStatus(item.get("status", CatalogProductStatus.LISTED.value))
            except (TypeError, ValueError) as exc:
                raise FuturesCatalogError(f"products[{index}] has an invalid instrument/status") from exc
            products.append(
                FuturesProduct(
                    exchange=_nonempty(item.get("exchange"), f"products[{index}].exchange").upper(),
                    code=_nonempty(item.get("code"), f"products[{index}].code").upper(),
                    name_zh=_nonempty(item.get("name_zh"), f"products[{index}].name_zh"),
                    instrument_kind=kind,
                    status=status,
                    source_url=_url(item.get("source_url"), f"products[{index}].source_url"),
                    spec_urls=_parse_spec_urls(
                        item.get("spec_urls"), f"products[{index}].spec_urls"
                    ),
                    representative_contract=(
                        item.get("representative_contract")
                        if isinstance(item.get("representative_contract"), str)
                        else None
                    ),
                    contract_pattern=(
                        item.get("contract_pattern")
                        if isinstance(item.get("contract_pattern"), str)
                        else None
                    ),
                    note=item.get("note") if isinstance(item.get("note"), str) else None,
                )
            )
        excluded: list[ExcludedOption] = []
        for index, item in enumerate(raw_excluded):
            if not isinstance(item, Mapping):
                raise FuturesCatalogError(f"excluded_options[{index}] must be an object")
            excluded.append(
                ExcludedOption(
                    exchange=_nonempty(item.get("exchange"), f"excluded_options[{index}].exchange").upper(),
                    code=_nonempty(item.get("code"), f"excluded_options[{index}].code").upper(),
                    name_zh=_nonempty(item.get("name_zh"), f"excluded_options[{index}].name_zh"),
                    source_url=_url(item.get("source_url"), f"excluded_options[{index}].source_url"),
                    reason=_nonempty(item.get("reason"), f"excluded_options[{index}].reason"),
                )
            )
        if require_all_exchanges and set(item.exchange.upper() for item in sources) != set(SUPPORTED_EXCHANGES):
            raise FuturesCatalogError("production futures catalog must cover all six domestic exchanges")
        catalog_payload = {
            "schema_version": 1,
            "observed_at": observed_at.isoformat(),
            "sources": [source.as_dict() for source in sorted(sources, key=lambda item: item.exchange)],
            "calendars": [calendar.as_dict() for calendar in sorted(calendars, key=lambda item: item.exchange)],
            "products": [product.as_dict() for product in sorted(products, key=lambda item: item.identity)],
            "excluded_options": [item.as_dict() for item in sorted(excluded, key=lambda item: (item.exchange, item.code))],
            "review_reason": raw.get("review_reason") if isinstance(raw.get("review_reason"), str) else None,
        }
        expected_id = _sha256(catalog_payload)
        supplied_id = raw.get("catalog_id")
        if supplied_id is not None and _digest(supplied_id, "catalog_id") != expected_id:
            raise FuturesCatalogError("catalog_id does not match canonical catalog content")
        return cls(
            observed_at=observed_at,
            sources=sources,
            calendars=tuple(calendars),
            products=tuple(products),
            excluded_options=tuple(excluded),
            catalog_id=expected_id,
            review_reason=catalog_payload["review_reason"],
        )


def load_futures_catalog(path: str | Path, *, require_all_exchanges: bool = False) -> FuturesCatalog:
    source = Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FuturesCatalogError(f"cannot load futures catalog: {source}") from exc
    return FuturesCatalog.from_mapping(raw, require_all_exchanges=require_all_exchanges)


def validate_source_pairs(
    catalog: FuturesCatalog,
    source_products: Iterable[tuple[str, str]],
) -> None:
    expected = {(exchange.upper(), product.upper()) for exchange, product in (item.identity for item in catalog.future_products)}
    configured = {(exchange.upper(), product.upper()) for exchange, product in source_products}
    if expected != configured:
        missing = sorted(expected - configured)
        extra = sorted(configured - expected)
        raise FuturesCatalogError(
            f"source registry futures coverage mismatch; missing={missing[:20]}, extra={extra[:20]}"
        )
