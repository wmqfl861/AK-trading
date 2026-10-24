"""Strict input normalization for the loopback observation bridge."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from .connection import utc_time


CST = timezone(timedelta(hours=8))
MAX_BODY_BYTES = 256 * 1024
MIN_TICKS = 1
MAX_TICKS = 1000
MAX_REASON_LENGTH = 20
MAX_BATCH_ID_LENGTH = 128
MAX_SYMBOL_LENGTH = 80
DEFAULT_STALE_AFTER_SECONDS = 30.0

_SENSITIVE_NAMES = {
    "accounttag", "accountid", "fullid", "full_id", "clientid", "client_id",
    "userid", "user_id", "apikey", "api_key", "access_token", "refreshtoken",
}
_SENSITIVE_PARTS = ("password", "passwd", "secret", "credential", "authorization", "token")
_PRICE_FIELDS = {
    "last", "bid1", "ask1", "open", "high", "low", "close",
    "upper_limit", "lower_limit", "price_tick", "pre_close", "pre_settlement",
} | {f"{side}{level}" for side in ("bid", "ask") for level in range(2, 6)}
_QUANTITY_FIELDS = {"volume", "open_interest"} | {f"{side}_volume{level}" for side in ("bid", "ask") for level in range(1, 6)}
_NUMERIC_TICK_FIELDS = _PRICE_FIELDS | _QUANTITY_FIELDS | {"turnover"}
_TIME_FIELDS = ("time", "datetime", "timestamp", "ts", "update_time")


class DecisionEvaluator(Protocol):
    """Callable decision boundary used after normalization and safety checks."""

    def __call__(self, normalized_payload: Mapping[str, Any]) -> Mapping[str, Any]:
        ...


class ProtocolViolation(ValueError):
    """A request cannot safely enter the evaluator boundary."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


ProtocolError = ProtocolViolation


@dataclass(frozen=True)
class NormalizedRequest(Mapping[str, Any]):
    """Validated request metadata plus the evaluator-facing mapping."""

    payload: dict[str, Any]
    exchange: str | None
    instrument: str | None
    batch_id: str | None
    synthetic: bool
    tick_count: int
    payload_hash: str
    blocked_reason: str | None = None

    def __getitem__(self, key: str) -> Any:
        return self.payload[key]

    def __iter__(self):
        return iter(self.payload)

    def __len__(self) -> int:
        return len(self.payload)


def safe_reason(value: Any, fallback: str = "observing") -> str:
    """Return a short, single-line reason suitable for a JSON response."""

    if not isinstance(value, str):
        value = fallback
    value = " ".join(value.split())
    value = "".join(char for char in value if ord(char) >= 32)
    return (value or fallback)[:MAX_REASON_LENGTH]


def hold_response(reason: str = "observing") -> dict[str, str]:
    """The only response shape exposed by `/analyze` and `/heartbeat`."""

    return {"signal": "hold", "reason": safe_reason(reason)}


def _reject_constant(_value: str) -> None:
    raise ProtocolViolation("non_finite_number")


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolViolation("duplicate_key")
        result[key] = value
    return result


def parse_json_body(body: bytes) -> dict[str, Any]:
    """Parse bounded strict JSON, rejecting duplicate keys and NaN/Infinity."""

    if not isinstance(body, (bytes, bytearray)):
        raise ProtocolViolation("invalid_body")
    if len(body) > MAX_BODY_BYTES:
        raise ProtocolViolation("payload_too_large")
    try:
        value = json.loads(
            bytes(body).decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except ProtocolViolation:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise ProtocolViolation("invalid_json")
    if not isinstance(value, dict):
        raise ProtocolViolation("payload_object_required")
    _ensure_finite(value)
    return value


def payload_hash(payload: Any) -> str:
    """Hash a receipt without retaining raw payload data."""

    try:
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"),
            ensure_ascii=True, allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        encoded = repr(payload).encode("utf-8", errors="replace")
    return hashlib.sha256(encoded).hexdigest()


def _ensure_finite(value: Any, *, depth: int = 0) -> None:
    if depth > 8:
        raise ProtocolViolation("payload_depth")
    if value is None or isinstance(value, (bool, str)):
        return
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ProtocolViolation("non_finite_number")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ProtocolViolation("invalid_json_key")
            _ensure_finite(item, depth=depth + 1)
        return
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        for item in value:
            _ensure_finite(item, depth=depth + 1)
        return
    raise ProtocolViolation("invalid_json_value")


def _sensitive(key: str) -> bool:
    lowered = key.casefold().replace("-", "_")
    compact = lowered.replace("_", "")
    return compact in {name.replace("_", "") for name in _SENSITIVE_NAMES} or any(
        part in lowered for part in _SENSITIVE_PARTS
    )


def _text(value: Any, *, maximum: int = 120) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    value = "".join(char for char in value if ord(char) >= 32)
    return value[:maximum] or None


def _number(value: Any, *, positive: bool = False, integer: bool = False) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or abs(number) >= 1e100 or (positive and number <= 0):
        return None
    if integer and not number.is_integer():
        return None
    return int(number) if integer else value


def _safe_mapping(value: Any, *, depth: int = 0) -> dict[str, Any]:
    """Copy non-sensitive informational metadata with a small depth bound."""

    if not isinstance(value, Mapping) or len(value) > 32:
        raise ProtocolViolation("invalid_metadata")
    if depth > 4:
        raise ProtocolViolation("payload_depth")
    result: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or _sensitive(key):
            continue
        if key == "snapshot_observed_at":
            parsed = utc_time(item)
            result[key] = parsed.isoformat().replace("+00:00", "Z") if parsed else None
        elif isinstance(item, Mapping):
            result[key[:64]] = _safe_mapping(item, depth=depth + 1)
        elif isinstance(item, list):
            result[key[:64]] = [_text(entry, maximum=256) if isinstance(entry, str) else _number(entry)
                               for entry in item[:20]]
        elif isinstance(item, str):
            result[key[:64]] = _text(item, maximum=256)
        elif isinstance(item, bool) or item is None:
            result[key[:64]] = item
        elif isinstance(item, (int, float)):
            result[key[:64]] = _number(item)
    return result


def _clean_connection(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping) or len(value) > 12:
        raise ProtocolViolation("invalid_connection")
    result = {}
    for key in ("instance_id", "session_epoch", "request_id"):
        item = value.get(key)
        result[key] = item if isinstance(item, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", item) else None
    sequence = value.get("sequence")
    result["sequence"] = sequence if type(sequence) is int and 1 <= sequence <= 2 ** 53 - 1 else None
    sent_at = utc_time(value.get("sent_at"))
    result["sent_at"] = sent_at.isoformat().replace("+00:00", "Z") if sent_at else None
    result["ui_idle_refresh"] = "unsupported"
    snapshots = value.get("snapshots", {})
    if not isinstance(snapshots, Mapping) or len(snapshots) > 8:
        raise ProtocolViolation("invalid_connection")
    result["snapshots"] = {}
    for name in ("quote", "account", "position", "contract"):
        item = snapshots.get(name)
        if item is None:
            continue
        if not isinstance(item, Mapping) or len(item) > 8:
            raise ProtocolViolation("invalid_connection")
        record = {}
        for key in ("sampled_at", "source_at", "received_at"):
            parsed = utc_time(item.get(key))
            record[key] = parsed.isoformat().replace("+00:00", "Z") if parsed else None
        record["snapshot_id"] = _text(item.get("snapshot_id"), maximum=128)
        record["source"] = item.get("source") if item.get("source") in ("pythongo", "simulation") else "unknown"
        # Wire claims never prove native query completion or full-account scope.
        record["completeness"] = "unverified"
        result["snapshots"][name] = record
    return result


def _clean_account(value: Any) -> dict[str, Any]:
    if value is None:
        return {"status": "unknown", "available": None, "balance": None, "risk": None}
    if not isinstance(value, Mapping):
        raise ProtocolViolation("invalid_account")
    status = _text(value.get("status"), maximum=24) or "unknown"
    result = {
        "status": status,
        "available": _number(value.get("available", value.get("Available"))),
        "balance": _number(value.get("balance", value.get("Balance"))),
        "risk": _number(value.get("risk")),
    }
    for key in ("commission", "position_profit", "close_profit", "margin", "frozen_margin", "frozen_commission", "frozen_cash"):
        if key in value:
            result[key] = _number(value[key])
    return result


def _clean_side(value: Any) -> dict[str, Any]:
    if value is None:
        return {"total": None, "today": None, "yesterday": None}
    if not isinstance(value, Mapping):
        raise ProtocolViolation("invalid_position")
    result: dict[str, Any] = {}
    for output, names in (
        ("total", ("total", "position", "Position")),
        ("today", ("today", "TodayPosition")),
        ("yesterday", ("yesterday", "YdPositionClose")),
    ):
        present = next((name for name in names if name in value), None)
        if present is None or value[present] is None:
            result[output] = None
            continue
        result[output] = _number(value[present], integer=True)
        if result[output] is None or result[output] < 0:
            raise ProtocolViolation("invalid_position")
    total, today, yesterday = (result[key] for key in ("total", "today", "yesterday"))
    if total is not None and any(number is not None and number > total for number in (today, yesterday)):
        raise ProtocolViolation("invalid_position")
    if all(number is not None for number in (total, today, yesterday)) and today + yesterday > total:
        raise ProtocolViolation("invalid_position")
    return result


def _clean_position(value: Any) -> dict[str, Any]:
    if value is None:
        return {
            "status": "unknown",
            "long": _clean_side(None), "short": _clean_side(None), "net": None,
        }
    if not isinstance(value, Mapping):
        raise ProtocolViolation("invalid_position")
    net_present = "net" if "net" in value else "net_position" if "net_position" in value else None
    raw_net = value.get(net_present) if net_present is not None else None
    net = _number(raw_net, integer=True) if raw_net is not None else None
    if raw_net is not None and net is None:
        raise ProtocolViolation("invalid_position")
    long_side, short_side = _clean_side(value.get("long")), _clean_side(value.get("short"))
    long_total, short_total = long_side["total"], short_side["total"]
    if all(number is not None for number in (long_total, short_total, net)) and long_total - short_total != net:
        raise ProtocolViolation("invalid_position")
    complete = all(number is not None for number in (long_total, short_total, net))
    status = "ready" if value.get("status") == "ready" and complete else "observed" if value.get("status") == "observed" and (long_total is not None or short_total is not None) else "unknown"
    return {"status": status, "long": long_side, "short": short_side, "net": net}


def _clean_runtime(value: Any) -> dict[str, Any]:
    if value is None:
        return {
            "status": "unknown", "observer_only": None,
            "enable_trading": None, "framework_trading": None,
            "order_submission_count": None,
        }
    if not isinstance(value, Mapping):
        raise ProtocolViolation("invalid_runtime")
    count = _number(value.get("order_submission_count"), integer=True)
    return {
        "status": _text(value.get("status"), maximum=24) or "available",
        "observer_only": value.get("observer_only") if isinstance(value.get("observer_only"), bool) else None,
        "enable_trading": value.get("enable_trading") if isinstance(value.get("enable_trading"), bool) else None,
        "framework_trading": value.get("framework_trading") if isinstance(value.get("framework_trading"), bool) else None,
        "order_submission_count": count if count is not None and count >= 0 else None,
    }


def _clean_active_orders(value: Any) -> dict[str, Any]:
    if value is None:
        return {"status": "unknown", "count": None}
    if not isinstance(value, Mapping):
        raise ProtocolViolation("invalid_active_orders")
    count = value.get("count") if type(value.get("count")) is int else None
    result = {
        "status": _text(value.get("status"), maximum=24) or "unknown",
        "count": count if count is not None and count >= 0 else None,
    }
    if "observed_count" in value:
        result["observed_count"] = _number(value["observed_count"], integer=True)
        result["coverage"] = "callbacks_since_start"
    return result


def _clean_events(value: Any, kind: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 20:
        raise ProtocolViolation("invalid_event_window")
    text_fields = {"callback", "observed_at", "exchange", "instrument_id", "order_id", "order_sys_id", "trade_id", "direction", "offset", "status", "order_time", "cancel_time", "trade_time", "error_code", "scope"}
    number_fields = {"price", "volume", "total_volume", "traded_volume", "cancel_volume"}
    rows = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ProtocolViolation("invalid_event_record")
        row = {key: _text(item[key], maximum=80) for key in text_fields if key in item}
        row.update({key: _number(item[key], positive=key == "price", integer=key != "price") for key in number_fields if key in item})
        if "pending" in item:
            row["pending"] = item["pending"] if isinstance(item["pending"], bool) else None
        rows.append(row)
    return rows


def _clean_event_window(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"status": "unknown", "coverage": "callbacks_since_start"}
    result = {"status": _text(value.get("status"), maximum=24), "coverage": "callbacks_since_start",
              "window_started_at": _text(value.get("window_started_at"), maximum=64), "truncated": value.get("truncated") is True}
    for key in ("count", "pending_count", "unknown_status_count", "capacity"):
        result[key] = _number(value.get(key), integer=True)
    return result


def _clean_capabilities(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"rates_query": "not_exposed_in_documented_api"}
    result = {"rates_query": "not_exposed_in_documented_api", "source": "account_or_terminal_reference_unverified"}
    names = value.get("native_candidate_names", [])
    result["native_candidate_names"] = [name for name in names[:20] if isinstance(name, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", name)] if isinstance(names, list) else []
    values = value.get("available_values", {})
    fields = ("LongMarginRatioByMoney", "ShortMarginRatioByMoney", "LongMarginRatioByVolume", "ShortMarginRatioByVolume", "OpenRatioByMoney", "OpenRatioByVolume", "CloseRatioByMoney", "CloseRatioByVolume", "CloseTodayRatioByMoney", "CloseTodayRatioByVolume")
    result["available_values"] = {key: _number(values[key]) for key in fields if isinstance(values, Mapping) and key in values}
    return result


def _clean_contract(value: Any) -> dict[str, Any]:
    if value is None:
        return {
            "status": "unknown", "price_tick": None,
            "upper_limit": None, "lower_limit": None, "expiry": None,
        }
    if not isinstance(value, Mapping):
        raise ProtocolViolation("invalid_contract")
    result = {
        "status": _text(value.get("status"), maximum=24) or "unknown",
        "price_tick": _number(value.get("price_tick", value.get("tick_size")), positive=True),
        "upper_limit": _number(value.get("upper_limit"), positive=True),
        "lower_limit": _number(value.get("lower_limit"), positive=True),
        "expiry": _text(value.get("expiry", value.get("expiry_at")), maximum=64),
    }
    for key in ("asset_class", "underlying", "option_type", "product_id"):
        if key in value:
            result[key] = _text(value[key], maximum=64)
    for key in ("strike", "multiplier", "min_limit_order_volume", "max_limit_order_volume"):
        if key in value:
            result[key] = _number(value[key], positive=True)
    return result


def _clean_price_map(value: Any, *, reason: str) -> dict[str, Any]:
    if value is None:
        return {"status": "unknown"}
    if not isinstance(value, Mapping):
        raise ProtocolViolation(reason)
    result = {**{key: value for key, value in value.items() if key == "time"}, "status": _text(value.get("status"), maximum=24) or "available"}
    aliases = {"last_price": "last", "bid_price1": "bid1", "ask_price1": "ask1"}
    for source, target in aliases.items():
        if source in value and target not in value:
            value = dict(value)
            value[target] = value[source]
    for key in _NUMERIC_TICK_FIELDS:
        if key in value:
            number = _number(value[key], positive=key in _PRICE_FIELDS, integer=key in _QUANTITY_FIELDS)
            if number is None and key == "last":
                raise ProtocolViolation(reason)
            result[key] = number
    for key in ("is_auction", "in_auction", "at_auction", "at_limit", "limit_state"):
        if key in value and isinstance(value[key], bool):
            result[key] = value[key]
    for key in ("market_status", "trading_status"):
        text = _text(value.get(key), maximum=32)
        if text is not None:
            result[key] = text
    bid, ask = result.get("bid1"), result.get("ask1")
    if bid is not None and ask is not None and bid > ask:
        raise ProtocolViolation(reason)
    if result.get("status") == "ready" and utc_time(result.get("time")) is None:
        # Quote readiness shares the strict freshness time contract: without
        # an explicit-calendar, timezone-aware timestamp it cannot be ready.
        result["status"] = "unknown"
    return result


def _clean_tick(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ProtocolViolation("invalid_tick")
    result: dict[str, Any] = {}
    aliases = {"last_price": "last", "bid_price1": "bid1", "ask_price1": "ask1"}
    for key, item in value.items():
        if _sensitive(key):
            continue
        target = aliases.get(key, key)
        if target == "update_time":
            target = "time"
        if target in _TIME_FIELDS:
            if "time" not in result:
                result["time"] = item
        elif target in _NUMERIC_TICK_FIELDS:
            if item is None and target != "last":
                result[target] = None
                continue
            number = _number(item, positive=target in _PRICE_FIELDS, integer=target in _QUANTITY_FIELDS)
            if number is None and target == "last":
                raise ProtocolViolation("invalid_tick")
            if target in _QUANTITY_FIELDS and number is not None and number < 0:
                raise ProtocolViolation("invalid_tick")
            result[target] = number
        elif target == "trading_day":
            text = _text(item, maximum=16)
            if text is not None:
                result[target] = text
        elif target in {"bid_volume1", "ask_volume1"}:
            number = _number(item, integer=True)
            if number is not None and number >= 0:
                result[target] = number
    if not any(key in result for key in ("last", "bid1", "ask1")):
        raise ProtocolViolation("invalid_tick")
    bid, ask = result.get("bid1"), result.get("ask1")
    if bid is not None and ask is not None and bid > ask:
        raise ProtocolViolation("invalid_tick")
    return result


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value if value.tzinfo is not None else value.replace(tzinfo=CST)
        return parsed.astimezone(timezone.utc)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = _number(value)
        if number is None:
            return None
        seconds = float(number)
        if abs(seconds) >= 1e17:
            seconds /= 1e9
        elif abs(seconds) >= 1e11:
            seconds /= 1e3
        try:
            return datetime.fromtimestamp(seconds, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or re.fullmatch(r"\d{4}-?\d{2}-?\d{2}", text):
        return None
    normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = None
        for fmt in ("%Y%m%d %H:%M:%S.%f", "%Y%m%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CST)
    return parsed.astimezone(timezone.utc)


def _tick_time(tick: Mapping[str, Any]) -> datetime | None:
    return _parse_timestamp(tick.get("time"))


def canonical_symbol(exchange: str, instrument: str) -> str:
    """Return the stable whitelist spelling used by the bridge."""

    return f"{exchange.strip().upper()}.{instrument.strip()}"


def _identity(payload: Mapping[str, Any], *, required: bool) -> tuple[str | None, str | None]:
    raw_exchange, raw_instrument = payload.get("exchange"), payload.get("instrument_id", payload.get("symbol"))
    if raw_exchange is None and not required:
        exchange = None
    else:
        exchange = _text(raw_exchange, maximum=32)
        if not exchange:
            raise ProtocolViolation("unknown_exchange")
        exchange = exchange.upper()
    if raw_instrument is None and not required:
        instrument = None
    else:
        instrument = _text(raw_instrument, maximum=MAX_SYMBOL_LENGTH)
        if not instrument:
            raise ProtocolViolation("symbol_unknown")
        if exchange and instrument.casefold().startswith(exchange.casefold() + "."):
            instrument = instrument.split(".", 1)[1]
    return exchange, instrument


def _version(payload: Mapping[str, Any]) -> int:
    raw = payload.get("schema_version")
    if raw is None:
        raw = payload.get("version", payload.get("protocol_version", 1))
    if isinstance(raw, bool):
        raise ProtocolViolation("unknown_version")
    if raw in (1, "1", "v1"):
        return 1
    if raw in (2, "2", "v2"):
        return 2
    raise ProtocolViolation("unknown_version")


def _block_market(payload: Mapping[str, Any], quote: Mapping[str, Any]) -> str | None:
    for key in ("auction", "is_auction", "in_auction", "at_auction"):
        if payload.get(key) is True or quote.get(key) is True:
            return "auction_state"
    for key in ("limit_up", "limit_down", "at_limit", "limit_state"):
        if payload.get(key) is True or quote.get(key) is True:
            return "limit_state"
    for value in (payload.get("market_status"), payload.get("trading_status"), quote.get("market_status")):
        if isinstance(value, str):
            lowered = value.casefold()
            if "auction" in lowered or lowered in {"call", "call_auction"}:
                return "auction_state"
            if "limit" in lowered:
                return "limit_state"
    return None


def normalize_payload(
    payload: Mapping[str, Any],
    *,
    allowed_symbols: Sequence[str] | None = None,
    allow_test: bool = False,
    now: datetime | None = None,
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
    require_ticks: bool = True,
    require_identity: bool = True,
) -> NormalizedRequest:
    """Validate a v1/v2 envelope and produce a safe evaluator payload."""

    if not isinstance(payload, Mapping):
        raise ProtocolViolation("payload_object_required")
    _ensure_finite(payload)
    schema_version = _version(payload)
    if not math.isfinite(float(stale_after_seconds)) or stale_after_seconds <= 0:
        raise ValueError("stale_after_seconds must be positive")

    exchange, instrument = _identity(payload, required=require_identity)
    synthetic = bool(exchange and exchange.casefold() == "test")
    blocked: str | None = "test_disabled" if synthetic and not allow_test else None
    configured = {str(item).strip().casefold() for item in (allowed_symbols or ()) if str(item).strip()}
    if exchange and instrument and not synthetic and configured:
        full = canonical_symbol(exchange, instrument).casefold()
        if full not in configured and instrument.casefold() not in configured:
            blocked = blocked or "symbol_unknown"

    raw_ticks = payload.get("ticks")
    ticks: list[dict[str, Any]] = []
    if require_ticks:
        if not isinstance(raw_ticks, (list, tuple)):
            raise ProtocolViolation("ticks_required")
        if not MIN_TICKS <= len(raw_ticks) <= MAX_TICKS:
            raise ProtocolViolation("ticks_limit")
        ticks = [_clean_tick(item) for item in raw_ticks]
    elif raw_ticks is not None:
        if not isinstance(raw_ticks, (list, tuple)) or len(raw_ticks) > MAX_TICKS:
            raise ProtocolViolation("ticks_limit")
        ticks = [_clean_tick(item) for item in raw_ticks]

    times = [_tick_time(tick) for tick in ticks]
    if times and any(item is None for item in times):
        blocked = blocked or "missing_full_context"
    if times and all(item is not None for item in times):
        if any(times[index] < times[index - 1] for index in range(1, len(times))):
            blocked = blocked or "tick_chronology"
        if now is not None:
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
            age = (now.astimezone(timezone.utc) - max(times)).total_seconds()
            if age > stale_after_seconds:
                blocked = blocked or "stale_tick"
            elif age < -stale_after_seconds:
                blocked = blocked or "future_tick"

    quote = _clean_price_map(payload.get("quote"), reason="invalid_quote")
    quote_time = _tick_time(quote) if quote.get("time") is not None else None
    if quote_time is not None and now is not None:
        age = (now.astimezone(timezone.utc) - quote_time).total_seconds()
        if age > stale_after_seconds:
            blocked = blocked or "stale_quote"
    position = _clean_position(payload.get("position"))
    account = _clean_account(payload.get("account"))
    runtime = _clean_runtime(payload.get("runtime"))
    contract = _clean_contract(payload.get("contract"))
    active_orders = _clean_active_orders(payload.get("active_orders"))
    market_reason = _block_market(payload, payload.get("quote") or {})
    if market_reason is None:
        for raw_tick in raw_ticks or []:
            market_reason = _block_market(raw_tick, {})
            if market_reason is not None:
                break
    blocked = blocked or market_reason

    trading_day = _text(payload.get("trading_day"), maximum=16)
    normalized: dict[str, Any] = {
        "version": f"v{schema_version}",
        "schema_version": schema_version,
        "exchange": exchange,
        "instrument_id": instrument,
        "batch_id": _text(payload.get("batch_id"), maximum=MAX_BATCH_ID_LENGTH),
        "ticks": ticks,
        "quote": quote,
        "position": position,
        "account": account,
        "runtime": runtime,
        "contract": contract,
        "active_orders": active_orders,
        "metadata": _safe_mapping(payload.get("metadata", {})),
        "connection": _clean_connection(payload.get("connection")),
        "orders": _clean_events(payload.get("orders"), "orders"),
        "trades": _clean_events(payload.get("trades"), "trades"),
        "errors": _clean_events(payload.get("errors"), "errors"),
        "observed_orders": _clean_event_window(payload.get("observed_orders")),
        "observed_trades": _clean_event_window(payload.get("observed_trades")),
        "capabilities": _clean_capabilities(payload.get("capabilities")),
    }
    if trading_day is not None:
        normalized["trading_day"] = trading_day
    for key in ("market_status", "trading_status", "session_status"):
        text = _text(payload.get(key), maximum=32)
        if text is not None:
            normalized[key] = text
    if payload.get("heartbeat") is True:
        normalized["heartbeat"] = True

    return NormalizedRequest(
        payload=normalized,
        exchange=exchange,
        instrument=instrument,
        batch_id=normalized["batch_id"],
        synthetic=synthetic,
        tick_count=len(ticks),
        payload_hash=payload_hash(payload),
        blocked_reason=blocked,
    )


normalize_request = normalize_payload
parse_payload = parse_json_body

__all__ = [
    "CST", "DEFAULT_STALE_AFTER_SECONDS", "DecisionEvaluator",
    "MAX_BATCH_ID_LENGTH", "MAX_BODY_BYTES", "MAX_REASON_LENGTH", "MAX_TICKS", "MIN_TICKS",
    "NormalizedRequest", "ProtocolError", "ProtocolViolation", "canonical_symbol",
    "hold_response", "normalize_payload", "normalize_request", "parse_json_body", "parse_payload",
    "payload_hash", "safe_reason",
]
