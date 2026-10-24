from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import Enum
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
MONEY_QUANTUM = Decimal("0.01")
RATE_QUANTUM = Decimal("0.00000001")
FEE_ACTIONS = ("open", "close", "close_today")


class ReferenceDataError(ValueError):
    """Raised when market-reference source data is unsafe to use."""


class FeeBasis(str, Enum):
    CNY_PER_LOT = "cny_per_lot"
    RATE_PER_NOTIONAL = "rate_per_notional"


class FeeValueStatus(str, Enum):
    VALUE = "value"
    EXPLICIT_ZERO = "explicit_zero"
    UNKNOWN = "unknown"


class MarkupMode(str, Enum):
    ABSOLUTE_PER_LOT = "absolute_per_lot"
    RATE_ADD = "rate_add"
    EXCHANGE_MULTIPLIER = "exchange_multiplier"


def _decimal(value: Any, field_name: str, *, allow_zero: bool = True) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ReferenceDataError(
            f"{field_name} must be a decimal string, integer, or Decimal"
        )
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ReferenceDataError(f"{field_name} is not a decimal") from exc
    if not result.is_finite():
        raise ReferenceDataError(f"{field_name} must be finite")
    if result < 0 or (not allow_zero and result == 0):
        qualifier = "positive" if not allow_zero else "non-negative"
        raise ReferenceDataError(f"{field_name} must be {qualifier}")
    return result


def _date(value: Any, field_name: str) -> date:
    if not isinstance(value, str):
        raise ReferenceDataError(f"{field_name} must be an ISO date string")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ReferenceDataError(f"{field_name} must be YYYY-MM-DD") from exc


def _nonempty(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReferenceDataError(f"{field_name} must be a non-empty string")
    return value.strip()


def _source_host_matches_domain(source_url: str, official_domain: str) -> bool:
    hostname = (urlparse(source_url).hostname or "").lower().rstrip(".")
    wanted = official_domain.lower().lstrip(".").rstrip(".")
    return bool(hostname and wanted) and (
        hostname == wanted or hostname.endswith(f".{wanted}")
    )


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")


@dataclass(frozen=True)
class Provenance:
    source_url: str
    source_title: str
    publication_date: date
    transcribed_by: str
    transcribed_at: date
    access_method: str
    values_verified: bool

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        field_name: str,
        *,
        official_domain: str | None = None,
    ) -> "Provenance":
        if not isinstance(value, Mapping):
            raise ReferenceDataError(f"{field_name} must be an object")
        source_url = _nonempty(value.get("source_url"), f"{field_name}.source_url")
        if not source_url.startswith(("https://", "http://")):
            raise ReferenceDataError(f"{field_name}.source_url must be HTTP(S)")
        if official_domain is not None and not _source_host_matches_domain(
            source_url, official_domain
        ):
            raise ReferenceDataError(
                f"{field_name}.source_url host must match official domain "
                f"{official_domain!r}"
            )
        verified = value.get("values_verified")
        if not isinstance(verified, bool):
            raise ReferenceDataError(f"{field_name}.values_verified must be bool")
        return cls(
            source_url=source_url,
            source_title=_nonempty(
                value.get("source_title"), f"{field_name}.source_title"
            ),
            publication_date=_date(
                value.get("publication_date"), f"{field_name}.publication_date"
            ),
            transcribed_by=_nonempty(
                value.get("transcribed_by"), f"{field_name}.transcribed_by"
            ),
            transcribed_at=_date(
                value.get("transcribed_at"), f"{field_name}.transcribed_at"
            ),
            access_method=_nonempty(
                value.get("access_method"), f"{field_name}.access_method"
            ),
            values_verified=verified,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_url": self.source_url,
            "source_title": self.source_title,
            "publication_date": self.publication_date.isoformat(),
            "transcribed_by": self.transcribed_by,
            "transcribed_at": self.transcribed_at.isoformat(),
            "access_method": self.access_method,
            "values_verified": self.values_verified,
        }


@dataclass(frozen=True)
class FeeComponent:
    status: FeeValueStatus
    value: Decimal | None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], field_name: str) -> "FeeComponent":
        if not isinstance(value, Mapping):
            raise ReferenceDataError(f"{field_name} must be an object")
        try:
            status = FeeValueStatus(value.get("status"))
        except (TypeError, ValueError) as exc:
            raise ReferenceDataError(f"{field_name}.status is invalid") from exc

        raw = value.get("value")
        if status is FeeValueStatus.UNKNOWN:
            if raw is not None:
                raise ReferenceDataError(f"{field_name}.value must be null when unknown")
            return cls(status=status, value=None)

        amount = _decimal(raw, f"{field_name}.value")
        if status is FeeValueStatus.EXPLICIT_ZERO and amount != 0:
            raise ReferenceDataError(
                f"{field_name}.value must be zero when status is explicit_zero"
            )
        if status is FeeValueStatus.VALUE and amount == 0:
            raise ReferenceDataError(
                f"{field_name}.status must be explicit_zero for a zero value"
            )
        return cls(status=status, value=amount)

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status.value, "value": _decimal_text(self.value)}


@dataclass(frozen=True)
class FeeSchedule:
    basis: FeeBasis
    components: Mapping[str, FeeComponent]
    applicable_contracts: tuple[str, ...]
    effective_from: date
    must_reverify_before: date
    provenance: Provenance

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        field_name: str,
        *,
        official_domain: str | None = None,
    ) -> "FeeSchedule":
        if not isinstance(value, Mapping):
            raise ReferenceDataError(f"{field_name} must be an object")
        try:
            basis = FeeBasis(value.get("basis"))
        except (TypeError, ValueError) as exc:
            raise ReferenceDataError(f"{field_name}.basis is invalid") from exc
        raw_components = value.get("components")
        if not isinstance(raw_components, Mapping):
            raise ReferenceDataError(f"{field_name}.components must be an object")
        if set(raw_components) != set(FEE_ACTIONS):
            raise ReferenceDataError(
                f"{field_name}.components must contain exactly {FEE_ACTIONS}"
            )
        components = {
            action: FeeComponent.from_mapping(
                raw_components[action], f"{field_name}.components.{action}"
            )
            for action in FEE_ACTIONS
        }
        if basis is FeeBasis.RATE_PER_NOTIONAL:
            for action, component in components.items():
                if component.value is not None and component.value > 1:
                    raise ReferenceDataError(
                        f"{field_name}.components.{action}.value must be a ratio"
                    )
        raw_contracts = value.get("applicable_contracts")
        if not isinstance(raw_contracts, list) or not raw_contracts or any(
            not isinstance(contract, str) or not contract.strip()
            for contract in raw_contracts
        ):
            raise ReferenceDataError(
                f"{field_name}.applicable_contracts must be a non-empty string list"
            )
        applicable_contracts = tuple(contract.strip() for contract in raw_contracts)
        if len(set(applicable_contracts)) != len(applicable_contracts):
            raise ReferenceDataError(
                f"{field_name}.applicable_contracts must be unique"
            )
        effective = _date(value.get("effective_from"), f"{field_name}.effective_from")
        reverify = _date(
            value.get("must_reverify_before"),
            f"{field_name}.must_reverify_before",
        )
        if reverify < effective:
            raise ReferenceDataError(
                f"{field_name}.must_reverify_before precedes effective_from"
            )
        return cls(
            basis=basis,
            components=components,
            applicable_contracts=applicable_contracts,
            effective_from=effective,
            must_reverify_before=reverify,
            provenance=Provenance.from_mapping(
                value.get("provenance", {}),
                f"{field_name}.provenance",
                official_domain=official_domain,
            ),
        )


@dataclass(frozen=True)
class MarginRule:
    rate_of_notional: Decimal
    price_basis: str
    applicable_contracts: tuple[str, ...]
    effective_from: date
    must_reverify_before: date
    provenance: Provenance
    note: str

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        field_name: str,
        *,
        official_domain: str | None = None,
    ) -> "MarginRule":
        if not isinstance(value, Mapping):
            raise ReferenceDataError(f"{field_name} must be an object")
        price_basis = _nonempty(value.get("price_basis"), f"{field_name}.price_basis")
        if price_basis != "settlement_price":
            raise ReferenceDataError(
                f"{field_name}.price_basis must be settlement_price"
            )
        rate = _decimal(
            value.get("rate_of_notional"),
            f"{field_name}.rate_of_notional",
            allow_zero=False,
        )
        if rate > 1:
            raise ReferenceDataError(
                f"{field_name}.rate_of_notional must be a ratio, not a percent"
            )
        raw_contracts = value.get("applicable_contracts")
        if not isinstance(raw_contracts, list) or not raw_contracts or any(
            not isinstance(contract, str) or not contract.strip()
            for contract in raw_contracts
        ):
            raise ReferenceDataError(
                f"{field_name}.applicable_contracts must be a non-empty string list"
            )
        applicable_contracts = tuple(contract.strip() for contract in raw_contracts)
        if len(set(applicable_contracts)) != len(applicable_contracts):
            raise ReferenceDataError(
                f"{field_name}.applicable_contracts must be unique"
            )
        effective = _date(value.get("effective_from"), f"{field_name}.effective_from")
        reverify = _date(
            value.get("must_reverify_before"),
            f"{field_name}.must_reverify_before",
        )
        if reverify < effective:
            raise ReferenceDataError(
                f"{field_name}.must_reverify_before precedes effective_from"
            )
        return cls(
            rate_of_notional=rate,
            price_basis=price_basis,
            applicable_contracts=applicable_contracts,
            effective_from=effective,
            must_reverify_before=reverify,
            provenance=Provenance.from_mapping(
                value.get("provenance", {}),
                f"{field_name}.provenance",
                official_domain=official_domain,
            ),
            note=_nonempty(value.get("note"), f"{field_name}.note"),
        )


@dataclass(frozen=True)
class MarkupPolicy:
    mode: MarkupMode
    value: Decimal
    applies_to: frozenset[str]
    confirmed_with_broker: bool
    assumption: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "MarkupPolicy":
        if not isinstance(value, Mapping):
            raise ReferenceDataError("broker_markup must be an object")
        try:
            mode = MarkupMode(value.get("mode"))
        except (TypeError, ValueError) as exc:
            raise ReferenceDataError("broker_markup.mode is invalid") from exc
        amount = _decimal(value.get("value"), "broker_markup.value")
        if mode is MarkupMode.EXCHANGE_MULTIPLIER and amount < 1:
            raise ReferenceDataError(
                "broker_markup.value must be at least 1 for exchange_multiplier"
            )
        if mode is MarkupMode.RATE_ADD and amount > 1:
            raise ReferenceDataError("broker_markup.value must be a ratio for rate_add")
        raw_actions = value.get("applies_to")
        if not isinstance(raw_actions, list) or any(
            not isinstance(action, str) for action in raw_actions
        ):
            raise ReferenceDataError("broker_markup.applies_to must be a string list")
        actions = frozenset(raw_actions)
        if len(actions) != len(raw_actions):
            raise ReferenceDataError("broker_markup.applies_to must be unique")
        if not actions or not actions.issubset(FEE_ACTIONS):
            raise ReferenceDataError("broker_markup.applies_to has invalid actions")
        confirmed = value.get("confirmed_with_broker")
        if not isinstance(confirmed, bool):
            raise ReferenceDataError(
                "broker_markup.confirmed_with_broker must be bool"
            )
        return cls(
            mode=mode,
            value=amount,
            applies_to=actions,
            confirmed_with_broker=confirmed,
            assumption=_nonempty(value.get("assumption"), "broker_markup.assumption"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "value": _decimal_text(self.value),
            "applies_to": sorted(self.applies_to),
            "confirmed_with_broker": self.confirmed_with_broker,
            "assumption": self.assumption,
        }


@dataclass(frozen=True)
class ProductSpec:
    code: str
    name_zh: str
    exchange: str
    official_domain: str
    main_symbol: str
    research_contract: str
    last_trading_day: date
    contract_size: Decimal
    price_tick: Decimal
    day_sessions: tuple[str, ...]
    night_sessions: tuple[str, ...]
    notice_query: str
    last_reviewed_date: date
    static_provenance: Provenance
    fees: FeeSchedule
    margin: MarginRule

    @property
    def tick_value(self) -> Decimal:
        return _money(self.contract_size * self.price_tick)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], field_name: str) -> "ProductSpec":
        if not isinstance(value, Mapping):
            raise ReferenceDataError(f"{field_name} must be an object")
        code = _nonempty(value.get("code"), f"{field_name}.code").upper()
        raw_sessions = value.get("sessions")
        if not isinstance(raw_sessions, Mapping):
            raise ReferenceDataError(f"{field_name}.sessions must be an object")

        def sessions(key: str) -> tuple[str, ...]:
            raw = raw_sessions.get(key)
            if not isinstance(raw, list) or not raw or any(
                not isinstance(item, str) or "-" not in item for item in raw
            ):
                raise ReferenceDataError(
                    f"{field_name}.sessions.{key} must be a non-empty time range list"
                )
            return tuple(raw)

        contract_size = _decimal(
            value.get("contract_size_tons"),
            f"{field_name}.contract_size_tons",
            allow_zero=False,
        )
        tick = _decimal(
            value.get("price_tick_cny_per_ton"),
            f"{field_name}.price_tick_cny_per_ton",
            allow_zero=False,
        )
        official_domain = _nonempty(
            value.get("official_domain"), f"{field_name}.official_domain"
        ).lower()
        product = cls(
            code=code,
            name_zh=_nonempty(value.get("name_zh"), f"{field_name}.name_zh"),
            exchange=_nonempty(value.get("exchange"), f"{field_name}.exchange").upper(),
            official_domain=official_domain,
            main_symbol=_nonempty(
                value.get("main_symbol"), f"{field_name}.main_symbol"
            ),
            research_contract=_nonempty(
                value.get("research_contract"), f"{field_name}.research_contract"
            ),
            last_trading_day=_date(
                value.get("last_trading_day"), f"{field_name}.last_trading_day"
            ),
            contract_size=contract_size,
            price_tick=tick,
            day_sessions=sessions("day"),
            night_sessions=sessions("night"),
            notice_query=_nonempty(
                value.get("notice_query"), f"{field_name}.notice_query"
            ),
            last_reviewed_date=_date(
                value.get("last_reviewed_date"), f"{field_name}.last_reviewed_date"
            ),
            static_provenance=Provenance.from_mapping(
                value.get("static_provenance", {}),
                f"{field_name}.static_provenance",
                official_domain=official_domain,
            ),
            fees=FeeSchedule.from_mapping(
                value.get("fees", {}),
                f"{field_name}.fees",
                official_domain=official_domain,
            ),
            margin=MarginRule.from_mapping(
                value.get("margin", {}),
                f"{field_name}.margin",
                official_domain=official_domain,
            ),
        )
        if product.research_contract not in product.fees.applicable_contracts:
            raise ReferenceDataError(
                f"{field_name}.fees do not apply to research_contract"
            )
        if product.research_contract not in product.margin.applicable_contracts:
            raise ReferenceDataError(
                f"{field_name}.margin does not apply to research_contract"
            )
        return product


@dataclass(frozen=True)
class TradingCalendar:
    valid_from: date
    valid_through: date
    closed_dates: frozenset[date]
    source_url: str
    last_reviewed_date: date

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "TradingCalendar":
        if not isinstance(value, Mapping):
            raise ReferenceDataError("calendar must be an object")
        valid_from = _date(value.get("valid_from"), "calendar.valid_from")
        valid_through = _date(value.get("valid_through"), "calendar.valid_through")
        if valid_through < valid_from:
            raise ReferenceDataError("calendar.valid_through precedes valid_from")
        raw_closed = value.get("closed_dates")
        if not isinstance(raw_closed, list):
            raise ReferenceDataError("calendar.closed_dates must be a list")
        closed = frozenset(
            _date(item, f"calendar.closed_dates[{index}]")
            for index, item in enumerate(raw_closed)
        )
        if any(item < valid_from or item > valid_through for item in closed):
            raise ReferenceDataError("calendar.closed_dates must be within coverage")
        source_url = _nonempty(value.get("source_url"), "calendar.source_url")
        if not source_url.startswith(("https://", "http://")):
            raise ReferenceDataError("calendar.source_url must be HTTP(S)")
        return cls(
            valid_from=valid_from,
            valid_through=valid_through,
            closed_dates=closed,
            source_url=source_url,
            last_reviewed_date=_date(
                value.get("last_reviewed_date"), "calendar.last_reviewed_date"
            ),
        )

    def is_trading_day(self, value: date) -> bool:
        self.require_covered(value)
        return value.weekday() < 5 and value not in self.closed_dates

    def require_covered(self, value: date) -> None:
        if value < self.valid_from or value > self.valid_through:
            raise ReferenceDataError(
                f"trading day {value} is outside calendar coverage "
                f"{self.valid_from}..{self.valid_through}"
            )

    def next_trading_day(self, value: date, *, include_current: bool = False) -> date:
        candidate = value if include_current else value + timedelta(days=1)
        while True:
            self.require_covered(candidate)
            if self.is_trading_day(candidate):
                return candidate
            candidate += timedelta(days=1)

    def target_trading_day(self, now: datetime) -> date:
        if now.tzinfo is None:
            raise ReferenceDataError("now must be timezone-aware")
        local = now.astimezone(SHANGHAI_TZ)
        candidate = local.date()
        self.require_covered(candidate)
        if local.time() < time(15, 0) and self.is_trading_day(candidate):
            return candidate
        return self.next_trading_day(candidate)


@dataclass(frozen=True)
class ReferenceBook:
    schema_version: int
    expected_products: tuple[str, ...]
    calendar: TradingCalendar
    broker_markup: MarkupPolicy
    products: tuple[ProductSpec, ...]

    def product(self, code: str) -> ProductSpec:
        wanted = code.upper()
        for product in self.products:
            if product.code == wanted:
                return product
        raise ReferenceDataError(f"unknown product {code!r}")


def load_reference_book(path: Path) -> ReferenceBook:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReferenceDataError(f"cannot load reference data: {path}") from exc
    return reference_book_from_mapping(raw)


def reference_book_from_mapping(raw: Mapping[str, Any]) -> ReferenceBook:
    """Validate decoded market-reference data without requiring a file round trip."""
    if not isinstance(raw, Mapping):
        raise ReferenceDataError("reference data root must be an object")
    if raw.get("schema_version") != 1:
        raise ReferenceDataError("unsupported market-reference schema_version")
    raw_expected = raw.get("expected_products")
    if not isinstance(raw_expected, list) or any(
        not isinstance(item, str) or not item.strip() for item in raw_expected
    ):
        raise ReferenceDataError("expected_products must be a non-empty string list")
    expected = tuple(item.upper() for item in raw_expected)
    if not expected or len(set(expected)) != len(expected):
        raise ReferenceDataError("expected_products must be non-empty and unique")
    raw_products = raw.get("products")
    if not isinstance(raw_products, list):
        raise ReferenceDataError("products must be a list")
    products = tuple(
        ProductSpec.from_mapping(item, f"products[{index}]")
        for index, item in enumerate(raw_products)
    )
    codes = tuple(product.code for product in products)
    if codes != expected:
        raise ReferenceDataError(
            f"products must match expected_products order; expected {expected}, got {codes}"
        )
    return ReferenceBook(
        schema_version=1,
        expected_products=expected,
        calendar=TradingCalendar.from_mapping(raw.get("calendar", {})),
        broker_markup=MarkupPolicy.from_mapping(raw.get("broker_markup", {})),
        products=products,
    )


def parse_price(value: Any, field_name: str = "price") -> Decimal:
    return _decimal(value, field_name, allow_zero=False)


def _markup_amount(
    policy: MarkupPolicy,
    action: str,
    *,
    lots: int,
    exchange_fee: Decimal | None,
    trade_price: Decimal | None,
    contract_size: Decimal,
) -> tuple[Decimal | None, str | None]:
    if action not in policy.applies_to:
        return Decimal("0.00"), None
    if policy.mode is MarkupMode.ABSOLUTE_PER_LOT:
        return _money(policy.value * lots), None
    if policy.mode is MarkupMode.RATE_ADD:
        if trade_price is None:
            return None, "trade_price_required_for_rate_markup"
        return _money(trade_price * contract_size * policy.value * lots), None
    if exchange_fee is None:
        return None, "exchange_fee_required_for_multiplier_markup"
    return _money(exchange_fee * (policy.value - 1)), None


def fee_for_action(
    product: ProductSpec,
    policy: MarkupPolicy,
    action: str,
    *,
    trade_price: Decimal | None = None,
    lots: int = 1,
) -> dict[str, Any]:
    if action not in FEE_ACTIONS:
        raise ReferenceDataError(f"invalid fee action {action!r}")
    if isinstance(lots, bool) or not isinstance(lots, int) or lots <= 0:
        raise ReferenceDataError("lots must be a positive integer")
    if trade_price is not None:
        trade_price = parse_price(trade_price, "trade_price")
    component = product.fees.components[action]
    exchange_fee: Decimal | None = None
    unavailable_reason: str | None = None
    if component.status is FeeValueStatus.UNKNOWN:
        unavailable_reason = "exchange_fee_unknown"
    elif product.fees.basis is FeeBasis.CNY_PER_LOT:
        assert component.value is not None
        exchange_fee = _money(component.value * lots)
    elif trade_price is None:
        unavailable_reason = "trade_price_required_for_ad_valorem_fee"
    else:
        assert component.value is not None
        exchange_fee = _money(
            trade_price * product.contract_size * component.value * lots
        )

    markup, markup_reason = _markup_amount(
        policy,
        action,
        lots=lots,
        exchange_fee=exchange_fee,
        trade_price=trade_price,
        contract_size=product.contract_size,
    )
    total = None
    if exchange_fee is not None and markup is not None:
        total = _money(exchange_fee + markup)
    reason = unavailable_reason or markup_reason
    return {
        "action": action,
        "lots": lots,
        "basis": product.fees.basis.value,
        "component_status": component.status.value,
        "component_value": _decimal_text(component.value),
        "trade_price": _decimal_text(trade_price),
        "exchange_fee_cny": _decimal_text(exchange_fee),
        "broker_markup_cny": _decimal_text(markup),
        "total_cny": _decimal_text(total),
        "available": total is not None,
        "unavailable_reason": reason,
    }


def _round_trip(
    name: str,
    first: Mapping[str, Any],
    second: Mapping[str, Any],
    *,
    tick_value: Decimal,
    trade_price: Decimal | None,
    contract_size: Decimal,
    lots: int,
) -> dict[str, Any]:
    def sum_field(field: str) -> Decimal | None:
        left = first.get(field)
        right = second.get(field)
        if left is None or right is None:
            return None
        return _money(Decimal(left) + Decimal(right))

    exchange = sum_field("exchange_fee_cny")
    markup = sum_field("broker_markup_cny")
    total = sum_field("total_cny")
    break_even = None
    cost_bps = None
    if total is not None:
        break_even = (total / (tick_value * lots)).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        )
        if trade_price is not None:
            notional = trade_price * contract_size * lots
            cost_bps = (total / notional * Decimal("10000")).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )
    reasons = [
        value
        for value in (first.get("unavailable_reason"), second.get("unavailable_reason"))
        if value
    ]
    return {
        "name": name,
        "exchange_fee_cny": _decimal_text(exchange),
        "broker_markup_cny": _decimal_text(markup),
        "total_cny": _decimal_text(total),
        "breakeven_ticks": _decimal_text(break_even),
        "cost_bps_of_notional": _decimal_text(cost_bps),
        "available": total is not None,
        "unavailable_reasons": sorted(set(reasons)),
    }


def product_report(
    product: ProductSpec,
    policy: MarkupPolicy,
    trading_day: date,
    *,
    trade_price: Decimal | None = None,
    settlement_price: Decimal | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    if trading_day >= product.last_trading_day:
        errors.append(
            f"research contract is at or past last trading day "
            f"{product.last_trading_day}"
        )
    if trading_day < product.fees.effective_from:
        errors.append("fee schedule is not yet effective")
    if trading_day > product.fees.must_reverify_before:
        errors.append(
            f"fee schedule must be reverified after "
            f"{product.fees.must_reverify_before}"
        )
    if trading_day < product.margin.effective_from:
        errors.append("margin rule is not yet effective")
    if trading_day > product.margin.must_reverify_before:
        errors.append(
            f"margin rule must be reverified after "
            f"{product.margin.must_reverify_before}"
        )
    for action, component in product.fees.components.items():
        if component.status is FeeValueStatus.UNKNOWN:
            errors.append(f"{action} exchange fee is unknown")
    for label, provenance in (
        ("static contract data", product.static_provenance),
        ("fee schedule", product.fees.provenance),
        ("margin rule", product.margin.provenance),
    ):
        if not provenance.values_verified:
            warnings.append(f"{label} is transcribed and not human-signed")
    if not policy.confirmed_with_broker:
        warnings.append("broker markup has not been reconciled to a settlement statement")

    if trade_price is not None:
        trade_price = parse_price(trade_price, f"{product.code}.trade_price")
    if settlement_price is not None:
        settlement_price = parse_price(
            settlement_price, f"{product.code}.settlement_price"
        )
    action_fees = {
        action: fee_for_action(
            product, policy, action, trade_price=trade_price, lots=1
        )
        for action in FEE_ACTIONS
    }
    unavailable_reason_text = {
        "trade_price_required_for_ad_valorem_fee": (
            "trade price is required for an ad valorem exchange fee"
        ),
        "trade_price_required_for_rate_markup": (
            "trade price is required for the broker rate markup"
        ),
        "exchange_fee_required_for_multiplier_markup": (
            "exchange fee is required for the broker multiplier markup"
        ),
        "exchange_fee_unknown": "exchange fee is unknown",
    }
    for action, fee in action_fees.items():
        if fee["available"]:
            continue
        reason = fee.get("unavailable_reason")
        detail = unavailable_reason_text.get(
            reason,
            str(reason or "a required input is missing").replace("_", " "),
        )
        warnings.append(f"{action} fee total unavailable: {detail}")
    round_trips = {
        "regular": _round_trip(
            "open_plus_close",
            action_fees["open"],
            action_fees["close"],
            tick_value=product.tick_value,
            trade_price=trade_price,
            contract_size=product.contract_size,
            lots=1,
        ),
        "same_day": _round_trip(
            "open_plus_close_today",
            action_fees["open"],
            action_fees["close_today"],
            tick_value=product.tick_value,
            trade_price=trade_price,
            contract_size=product.contract_size,
            lots=1,
        ),
    }
    margin_amount = None
    if settlement_price is not None:
        margin_amount = _money(
            settlement_price
            * product.contract_size
            * product.margin.rate_of_notional
        )
    else:
        warnings.append("exchange margin unavailable: settlement price is required")

    status = "error" if errors else ("warning" if warnings else "ok")
    return {
        "code": product.code,
        "name_zh": product.name_zh,
        "exchange": product.exchange,
        "status": status,
        "errors": errors,
        "warnings": warnings,
        "main_symbol": product.main_symbol,
        "research_contract": product.research_contract,
        "last_trading_day": product.last_trading_day.isoformat(),
        "contract_size": {
            "value": _decimal_text(product.contract_size),
            "unit": "ton_per_lot",
        },
        "price_tick": {
            "value": _decimal_text(product.price_tick),
            "unit": "cny_per_ton",
        },
        "tick_value_cny_per_lot": _decimal_text(product.tick_value),
        "sessions": {
            "day": list(product.day_sessions),
            "night": list(product.night_sessions),
            "calendar_warning": "Night sessions and holiday exceptions follow exchange notices.",
        },
        "prices": {
            "trade_price_cny_per_ton": _decimal_text(trade_price),
            "settlement_price_cny_per_ton": _decimal_text(settlement_price),
        },
        "fees": {
            "basis": product.fees.basis.value,
            "applicable_contracts": list(product.fees.applicable_contracts),
            "effective_from": product.fees.effective_from.isoformat(),
            "must_reverify_before": product.fees.must_reverify_before.isoformat(),
            "rounding": "round exchange fee to CNY 0.01, then add separately rounded markup",
            "actions": action_fees,
            "round_trips": round_trips,
            "provenance": product.fees.provenance.as_dict(),
        },
        "margin": {
            "exchange_rate_of_notional": _decimal_text(
                product.margin.rate_of_notional
            ),
            "applicable_contracts": list(product.margin.applicable_contracts),
            "price_basis": product.margin.price_basis,
            "settlement_price_cny_per_ton": _decimal_text(settlement_price),
            "estimated_exchange_margin_cny_per_lot": _decimal_text(margin_amount),
            "effective_from": product.margin.effective_from.isoformat(),
            "must_reverify_before": product.margin.must_reverify_before.isoformat(),
            "note": product.margin.note,
            "broker_actual": "unknown_and_may_be_higher",
            "provenance": product.margin.provenance.as_dict(),
        },
        "static_provenance": product.static_provenance.as_dict(),
        "reconciled_with_broker_statement": False,
    }


def build_preopen_report(
    book: ReferenceBook,
    trading_day: date,
    generated_at: datetime,
    *,
    trade_prices: Mapping[str, Decimal] | None = None,
    settlement_prices: Mapping[str, Decimal] | None = None,
    notice_check: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if generated_at.tzinfo is None:
        raise ReferenceDataError("generated_at must be timezone-aware")
    book.calendar.require_covered(trading_day)
    if not book.calendar.is_trading_day(trading_day):
        raise ReferenceDataError(f"{trading_day} is not a configured trading day")
    trade_prices = {key.upper(): value for key, value in (trade_prices or {}).items()}
    settlement_prices = {
        key.upper(): value for key, value in (settlement_prices or {}).items()
    }
    unknown_price_codes = (
        set(trade_prices) | set(settlement_prices)
    ) - set(book.expected_products)
    if unknown_price_codes:
        raise ReferenceDataError(
            f"prices supplied for unknown products: {sorted(unknown_price_codes)}"
        )
    products = [
        product_report(
            product,
            book.broker_markup,
            trading_day,
            trade_price=trade_prices.get(product.code),
            settlement_price=settlement_prices.get(product.code),
        )
        for product in book.products
    ]
    notice_status = (notice_check or {}).get("status", "offline_not_checked")
    if notice_status not in {
        "offline_not_checked",
        "checked",
        "review_required",
        "error",
    }:
        raise ReferenceDataError(f"unsupported notice-check status {notice_status!r}")
    if any(product["status"] == "error" for product in products) or notice_status == "error":
        overall = "error"
    elif notice_status == "review_required":
        overall = "review_required"
    elif notice_status == "offline_not_checked" or any(
        product["status"] == "warning" for product in products
    ):
        overall = "warning"
    else:
        overall = "ok"
    report: dict[str, Any] = {
        "schema_version": 1,
        "trading_day": trading_day.isoformat(),
        "generated_at": generated_at.astimezone(SHANGHAI_TZ).isoformat(),
        "timezone": "Asia/Shanghai",
        "status": overall,
        "expected_products": list(book.expected_products),
        "broker_markup": book.broker_markup.as_dict(),
        "calendar": {
            "valid_from": book.calendar.valid_from.isoformat(),
            "valid_through": book.calendar.valid_through.isoformat(),
            "source_url": book.calendar.source_url,
            "last_reviewed_date": book.calendar.last_reviewed_date.isoformat(),
        },
        "notice_check": dict(notice_check or {"status": "offline_not_checked"}),
        "products": products,
        "out_of_scope_costs": [
            "slippage",
            "bid_ask_spread",
            "order_or_cancellation_fees",
            "delivery_fees",
            "funding_cost",
            "broker_minimum_order_fee",
        ],
        "disclaimers": [
            "Exchange parameters are transcribed snapshots, not account-specific broker terms.",
            "Broker margin may be higher than the exchange margin shown here.",
            "The CNY 0.01 markup is a user-specified assumption until statement reconciliation.",
            "A successful indexed notice check is not proof that an exchange made no change.",
        ],
    }
    canonical = json.dumps(
        report, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    report["report_id"] = hashlib.sha256(canonical).hexdigest()
    return report


def _display_money(value: Any) -> str:
    return "N/A" if value is None else f"{value} CNY"


def _fee_rule_text(fee: Mapping[str, Any]) -> str:
    value = fee.get("component_value")
    if fee.get("component_status") == "unknown":
        return "unknown"
    if fee.get("basis") == "rate_per_notional":
        return f"{value} of notional"
    return f"{value} CNY/lot"


def render_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# 开盘前市场参数报告",
        "",
        f"- 交易日: {report['trading_day']}",
        f"- 生成时间: {report['generated_at']}",
        f"- 状态: {report['status']}",
        f"- 报告 ID: {report['report_id']}",
        "",
        "经纪商加收假设: 每个收费方向、每手另加 "
        f"{report['broker_markup']['value']} CNY；开仓和平仓分别计算。",
        "",
    ]
    for product in report["products"]:
        lines.extend(
            [
                f"## {product['code']} {product['name_zh']}",
                "",
                f"- 交易所: {product['exchange']}",
                f"- 当前研究合约: {product['research_contract']}",
                f"- 最后交易日保护线: {product['last_trading_day']}",
                f"- 合约乘数: {product['contract_size']['value']} 吨/手",
                f"- 最小变动: {product['price_tick']['value']} 元/吨",
                f"- 每跳价值: {product['tick_value_cny_per_lot']} 元/手",
                f"- 日盘: {', '.join(product['sessions']['day'])}",
                f"- 夜盘: {', '.join(product['sessions']['night'])}",
                f"- 手续费有效期: {product['fees']['effective_from']} 至复核截止 "
                f"{product['fees']['must_reverify_before']}",
                "",
                "| 方向 | 交易所规则值 | 交易所手续费估算 | 加收 | 合计 |",
                "| --- | ---: | ---: | ---: | ---: |",
            ]
        )
        labels = {"open": "开仓", "close": "平昨", "close_today": "平今"}
        for action in FEE_ACTIONS:
            fee = product["fees"]["actions"][action]
            lines.append(
                f"| {labels[action]} | {_fee_rule_text(fee)} | "
                f"{_display_money(fee['exchange_fee_cny'])} | "
                f"{_display_money(fee['broker_markup_cny'])} | "
                f"{_display_money(fee['total_cny'])} |"
            )
        lines.extend(
            [
                "",
                "| 往返口径 | 交易所手续费 | 加收 | 合计 | 盈亏平衡跳数 |",
                "| --- | ---: | ---: | ---: | ---: |",
            ]
        )
        for key, label in (("regular", "开仓+平昨"), ("same_day", "开仓+平今")):
            item = product["fees"]["round_trips"][key]
            lines.append(
                f"| {label} | {_display_money(item['exchange_fee_cny'])} | "
                f"{_display_money(item['broker_markup_cny'])} | "
                f"{_display_money(item['total_cny'])} | "
                f"{item['breakeven_ticks'] or 'N/A'} |"
            )
        margin = product["margin"]
        lines.extend(
            [
                "",
                f"- 交易所保证金率快照: "
                f"{Decimal(margin['exchange_rate_of_notional']) * 100}%",
                f"- 保证金有效期: {margin['effective_from']} 至复核截止 "
                f"{margin['must_reverify_before']}",
                f"- 估算每手交易所保证金: "
                f"{_display_money(margin['estimated_exchange_margin_cny_per_lot'])}",
                "- 期货公司实际保证金: 未知，通常可能高于交易所标准。",
                f"- 手续费来源: {product['fees']['provenance']['source_url']}",
                f"- 保证金来源: {margin['provenance']['source_url']}",
            ]
        )
        if product["errors"]:
            lines.append(f"- 错误: {'; '.join(product['errors'])}")
        if product["warnings"]:
            lines.append(f"- 提醒: {'; '.join(product['warnings'])}")
        lines.append("")
    notice = report["notice_check"]
    lines.extend(
        [
            "## 公告检查",
            "",
            f"- 状态: {notice.get('status', 'unknown')}",
            "- 说明: 公告检索只用于发现待复核内容，不会自动修改手续费或保证金。",
        ]
    )
    for item in notice.get("products", []):
        lines.append(f"- {item.get('code', '?')}: {item.get('status', 'unknown')}")
        if item.get("error"):
            lines.append(f"  - error: {item['error']}")
        for result in item.get("newer_dated_results", []):
            lines.append(
                f"  - review: [{result.get('title') or result.get('url')}]"
                f"({result.get('url')})"
            )
    lines.extend(
        [
            "",
            "## 未计入成本",
            "",
            ", ".join(report["out_of_scope_costs"]),
            "",
        ]
    )
    return "\n".join(lines)


def _stage_text(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            if not content.endswith("\n"):
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return temp_path


def write_report_artifacts(
    report: Mapping[str, Any], output_root: Path
) -> tuple[Path, Path]:
    day_dir = output_root / str(report["trading_day"])
    json_path = day_dir / "preopen.json"
    markdown_path = day_dir / "preopen.md"
    json_text = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
    markdown_text = render_markdown(report)
    staged_json: Path | None = None
    staged_markdown: Path | None = None
    try:
        staged_json = _stage_text(json_path, json_text)
        staged_markdown = _stage_text(markdown_path, markdown_text)
        os.replace(staged_markdown, markdown_path)
        os.replace(staged_json, json_path)
    finally:
        if staged_json is not None:
            staged_json.unlink(missing_ok=True)
        if staged_markdown is not None:
            staged_markdown.unlink(missing_ok=True)
    return json_path, markdown_path
