"""Observer-only PythonGO adapter for a local InfiniTrader bridge.

This file is deployable as a PythonGO strategy.  It keeps the legacy parameter
names and lifecycle shape, but all external work is bounded loopback HTTP in a
single daemon worker.  It has no production order sender.
"""

from __future__ import annotations

import hashlib
import json
import math
import queue
import re
import threading
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from http.client import HTTPConnection, HTTPSConnection
import socket

import INFINIGO as _native


class Field:
    """Small validated field used without the optional chart/numerical stack."""

    def __init__(self, default=None, *, title="", ge=None):
        self.default, self.title, self.ge = default, title, ge

    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, instance, owner=None):
        return self if instance is None else instance.__dict__.get(self.name, self.default)

    def __set__(self, instance, value):
        expected = type(self.default)
        if expected is bool:
            if isinstance(value, str):
                if value.strip().lower() not in {"true", "false", "0", "1"}:
                    raise ValueError("invalid boolean parameter")
                value = value.strip().lower() in {"true", "1"}
            if not isinstance(value, bool):
                raise ValueError("invalid boolean parameter")
        elif expected is int:
            if isinstance(value, bool):
                raise ValueError("invalid integer parameter")
            parsed = int(value)
            if isinstance(value, float) and parsed != value:
                raise ValueError("invalid integer parameter")
            value = parsed
        elif expected is str and not isinstance(value, str):
            raise ValueError("invalid string parameter")
        if self.ge is not None and value < self.ge:
            raise ValueError("parameter below minimum")
        instance.__dict__[self.name] = value


class BaseParams:
    @classmethod
    def fields(cls):
        result = {}
        for parent in reversed(cls.__mro__):
            result.update({key: value for key, value in vars(parent).items() if isinstance(value, Field)})
        return result

    def model_dump(self):
        return {key: getattr(self, key) for key in self.fields()}

    def __iter__(self):
        return iter(self.model_dump().items())


class BaseState(BaseParams):
    pass


class _NativeReadOnly:
    @staticmethod
    def get_investor_list():
        return _native.getInvestorList()

    @staticmethod
    def get_investor_account(investor):
        return _native.getInvestorAccount(investor)

    @staticmethod
    def get_investor_position(investor, simple=False):
        return _native.getInvestorPosition(investor, Simple=simple)

    @staticmethod
    def get_instrument(exchange, instrument_id):
        return _native.getInstrument(Exchange=exchange, InstrumentID=instrument_id)


infini = _NativeReadOnly()


class BaseStrategy:
    """PythonGO's documented callback contract, with no execution methods."""

    def __init__(self):
        self.strategy_id = 0
        self.strategy_name = ""
        self.trading = False

    @property
    def class_name(self):
        return type(self).__name__

    def get_params(self):
        return [{"name": key, "value": str(getattr(self.params_map, key)), "cn_name": field.title}
                for key, field in self.params_map.fields().items()]

    def set_params(self, data):
        for key, value in data.items():
            if key not in self.params_map.fields():
                raise ValueError("unknown strategy parameter")
            setattr(self.params_map, key, value)
        _native.updateParam(StrategyID=self.strategy_id, Data=self.get_params())
        self.update_status_bar()

    def update_status_bar(self):
        self._assert_callback_thread()
        _native.updateState(self.strategy_id, {
            field.title: str(getattr(self.state_map, key)) for key, field in self.state_map.fields().items()
        })

    def output(self, *messages):
        self._assert_callback_thread()
        _native.writeLog(msg="[" + self.strategy_name + "] " + " ".join(map(str, messages)))

    def on_init(self):
        self.trading = False
        self.update_status_bar()
        self.output("原生观察桥接已加载；报单禁用")

    def sub_market_data(self, exchange=None, instrument_id=None):
        self._assert_callback_thread()
        _native.subMarketData(StrategyObj=self, ExchangeID=exchange, InstrumentID=instrument_id)

    def unsub_market_data(self, exchange=None, instrument_id=None):
        self._assert_callback_thread()
        _native.unsubMarketData(StrategyObj=self, ExchangeID=exchange, InstrumentID=instrument_id)

    def on_order(self, order):
        pass

    def on_trade(self, trade, log=True):
        pass

    def on_error(self, error):
        self.trading = False
        self.output("收到错误回报；保持观察模式")

    def on_order_cancel(self, order):
        pass

    def on_order_trade(self, order):
        pass


CHINA_TZ = timezone(timedelta(hours=8))
ADAPTER_VERSION = "observer-20260914-reconnect-v5"
HEARTBEAT_INTERVAL_SECONDS = 5.0
HEARTBEAT_HTTP_BUDGET = HEARTBEAT_INTERVAL_SECONDS / 2
LINK_TTL_SECONDS = 15.0
PACKET_TTL_SECONDS = 10.0
DEFAULT_EXCHANGE = "SHFE"
DEFAULT_INSTRUMENT_ID = "rb2610"
DEFAULT_API_URL = "http://127.0.0.1:17778/analyze"
DEFAULT_BATCH_SIZE = 10
DEFAULT_ORDER_VOLUME = 1
DEFAULT_QUEUE_SIZE = 2
MAX_HTTP_TIMEOUT_SECONDS = 3.0
MAX_RESPONSE_BYTES = 64 * 1024
MAX_REASON_LENGTH = 20
STOP_JOIN_TIMEOUT_SECONDS = 3.5
MAX_SYMBOLS = 16
MAX_CALLBACK_RECORDS = 512
MAX_CALLBACK_EVENTS = 20
INVESTOR_RETRY_SECONDS = 5.0
NATIVE_UNAVAILABLE = 1.79769e308
CALLBACK_COVERAGE = "callbacks_since_start"
RATE_SOURCE = "account_or_terminal_reference_unverified"
# These CTP instrument reference keys are copied only if actually returned.
# Account Fee and Margin are amounts and must never be substituted for rates.
RATE_FIELDS = (
    "LongMarginRatio", "ShortMarginRatio", "LongMarginRatioByMoney",
    "LongMarginRatioByVolume", "ShortMarginRatioByMoney", "ShortMarginRatioByVolume",
    "OpenRatioByMoney", "OpenRatioByVolume", "CloseRatioByMoney", "CloseRatioByVolume",
    "CloseTodayRatioByMoney", "CloseTodayRatioByVolume",
)


class Params(BaseParams):
    """Legacy-compatible PythonGO parameter model."""

    exchange: str = Field(default=DEFAULT_EXCHANGE, title="交易所代码")
    instrument_id: str = Field(default=DEFAULT_INSTRUMENT_ID, title="合约代码")
    additional_symbols: str = Field(default="", title="附加观察合约(交易所.合约;分隔)")
    api_url: str = Field(default=DEFAULT_API_URL, title="桥接服务地址")
    batch_size: int = Field(default=DEFAULT_BATCH_SIZE, title="打包tick数量", ge=1)
    account_tag: str = Field(default="", title="账户备注(仅本地标签)")
    enable_trading: bool = Field(default=False, title="是否按AI信号下单")
    order_volume: int = Field(default=DEFAULT_ORDER_VOLUME, title="下单手数", ge=1)

    @property
    def volume(self) -> int:
        """Short-name compatibility view; the persisted field is order_volume."""

        return self.order_volume


class State(BaseState):
    """Small status surface suitable for the PythonGO display panel."""

    tick_count: int = Field(default=0, title="累计tick数", ge=0)
    ai_calls: int = Field(default=0, title="AI调用次数", ge=0)
    last_signal: str = Field(default="hold", title="最近AI信号")
    last_reason: str = Field(default="startup", title="最近AI理由")
    last_error: str = Field(default="", title="最近错误")
    dropped_batches: int = Field(default=0, title="丢弃批次数", ge=0)
    dropped_responses: int = Field(default=0, title="丢弃响应数", ge=0)
    blocked_candidates: int = Field(default=0, title="阻断候选数", ge=0)
    order_submissions: int = Field(default=0, title="报单提交数", ge=0)
    connection_status: str = Field(default="starting", title="桥接连接")
    market_status: str = Field(default="unknown", title="合约状态")
    framework_trading: bool = Field(default=False, title="框架交易状态")


@dataclass(frozen=True)
class _RequestJob:
    kind: str
    url: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class _WorkerResponse:
    kind: str
    status: int | None
    body: bytes | Mapping[str, Any] | None
    error: str | None = None
    connection: Mapping[str, Any] | None = None
    received_mono: float | None = None
    received_at: str | None = None


@dataclass
class _SymbolContext:
    ticks: list[dict[str, Any]] = field(default_factory=list)
    last_tick: dict[str, Any] | None = None
    quote_sample: dict[str, Any] | None = None
    market_status: str | None = None
    contract: dict[str, Any] = field(default_factory=dict)
    rates: dict[str, Any] = field(default_factory=dict)
    orders: OrderedDict = field(default_factory=OrderedDict)
    trades: OrderedDict = field(default_factory=OrderedDict)
    errors: deque = field(default_factory=lambda: deque(maxlen=MAX_CALLBACK_EVENTS))
    orders_truncated: bool = False
    trades_truncated: bool = False


Transport = Callable[[str, Mapping[str, Any], float], Any]


class _CaptureFault(Exception):
    """A component query failed or returned a foreign identity mid-capture.

    Account-scoped facts are all-or-nothing: the outer capture transaction
    reacts by rotating the observation epoch, discarding every partial fact
    and recapturing once; a persistent fault publishes unknowns."""


def _finite_number(value: Any, *, positive: bool = False) -> int | float | None:
    """Return a JSON-safe finite number without turning missing data into zero."""

    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or abs(number) >= NATIVE_UNAVAILABLE or (positive and number <= 0):
        return None
    return value if isinstance(value, int) else number


def _quantity(value: Any) -> int | None:
    number = _finite_number(value)
    if number is None or number < 0 or not float(number).is_integer():
        return None
    return int(number)


def _nonnegative(value: Any) -> int | float | None:
    number = _finite_number(value)
    return number if number is not None and number >= 0 else None


def _identifier(value: Any) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value) if 0 <= value < 10 ** 80 else None
    if isinstance(value, str):
        value = value.strip()
        if re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,79}", value):
            return value
    return None


def _safe_text(value: Any, *, maximum: int = 120) -> str:
    if not isinstance(value, str):
        value = str(value)
    value = " ".join(value.split())
    value = "".join(char for char in value if ord(char) >= 32)
    return value[:maximum]


def _attr(value: Any, *names: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        for name in names:
            if name in value and value[name] is not None:
                return value[name]
    for name in names:
        try:
            result = getattr(value, name)
        except Exception:
            # Some native properties raise while a field is unavailable.
            continue
        if result is not None:
            return result
    return default


def _parse_day(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) == 8 and text.isascii() and text.isdigit():
        try:
            datetime.strptime(text, "%Y%m%d")
            return text
        except ValueError:
            return None
    if len(text) == 10:
        try:
            return datetime.strptime(text, "%Y-%m-%d").strftime("%Y%m%d")
        except ValueError:
            return None
    return None


def _aware_iso(value: Any, *, trading_day: Any = None, update_time: Any = None) -> str | None:
    """Convert SDK time fields to an aware ISO-8601 timestamp in China time."""

    parsed: datetime | None = None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            for format_value in ("%Y%m%d %H:%M:%S.%f", "%Y%m%d %H:%M:%S"):
                try:
                    parsed = datetime.strptime(text, format_value)
                    break
                except ValueError:
                    continue

    # TradingDay can name the following session's date during night trading;
    # it is not a calendar date and cannot reconstruct a missing Datetime.
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CHINA_TZ)
    return parsed.astimezone(CHINA_TZ).isoformat()


def validate_loopback_url(value: str) -> str:
    """Validate an HTTP(S) loopback URL and strip query/fragment metadata."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError("api_url must be a loopback URL")
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        raise ValueError("api_url must be a loopback HTTP URL")
    if parsed.query or parsed.fragment or not parsed.hostname:
        raise ValueError("api_url must not contain query or fragment")
    host = parsed.hostname.casefold().rstrip(".")
    is_loopback = host == "localhost"
    if not is_loopback:
        try:
            is_loopback = ip_address(host).is_loopback
        except ValueError as exc:
            raise ValueError("api_url host must be loopback") from exc
    if not is_loopback:
        raise ValueError("api_url host must be loopback")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("api_url port is invalid") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("api_url port is invalid")
    path = parsed.path or "/analyze"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _endpoint(url: str, path: str) -> str:
    parsed = urlsplit(validate_loopback_url(url))
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _default_transport(url: str, payload: Mapping[str, Any], timeout: float) -> tuple[int, bytes]:
    """Direct loopback sockets: no proxies, redirects, or unbounded body reads."""
    endpoint = urlsplit(validate_loopback_url(url))
    if isinstance(timeout, bool) or not 0 < float(timeout) <= MAX_HTTP_TIMEOUT_SECONDS:
        raise ValueError("invalid_transport_timeout")
    body = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(body) > 256 * 1024:
        raise ValueError("request_too_large")
    connection_type = HTTPSConnection if endpoint.scheme == "https" else HTTPConnection
    host = endpoint.hostname
    if host.casefold().rstrip(".") == "localhost":
        host = "127.0.0.1"
    connection = connection_type(host, endpoint.port, timeout=float(timeout))
    deadline = time.monotonic() + float(timeout)
    live_socket = [None]
    expired = threading.Event()

    def expire():
        expired.set()
        sock = live_socket[0]
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    timer = threading.Timer(float(timeout), expire)
    timer.daemon = True
    response = None
    timer.start()
    try:
        connection.connect()
        live_socket[0] = connection.sock
        remaining = deadline - time.monotonic()
        if expired.is_set() or remaining <= 0:
            raise TimeoutError("request_deadline_exceeded")
        connection.sock.settimeout(remaining)
        connection.request("POST", endpoint.path, body=body, headers={
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json", "Connection": "close", "X-Infiny-Source": "AKObserverBridge",
        })
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise ValueError("redirect_not_allowed")
        chunks = []
        received = 0
        while True:
            remaining = deadline - time.monotonic()
            if expired.is_set() or remaining <= 0:
                raise TimeoutError("response_deadline_exceeded")
            if callable(getattr(response, "isclosed", None)) and response.isclosed():
                break
            live_socket[0].settimeout(remaining)
            chunk = response.read1(min(8192, MAX_RESPONSE_BYTES + 1 - received))
            if expired.is_set() or time.monotonic() >= deadline:
                raise TimeoutError("response_deadline_exceeded")
            if not chunk:
                break
            received += len(chunk)
            if received > MAX_RESPONSE_BYTES:
                raise ValueError("response_too_large")
            chunks.append(chunk)
        return response.status, b"".join(chunks)
    finally:
        timer.cancel()
        if response is not None:
            response.close()
        connection.close()


def _strict_object(value: bytes | bytearray | str | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (bytes, bytearray)):
        if len(value) > MAX_RESPONSE_BYTES:
            raise ValueError("response_too_large")
        text = bytes(value).decode("utf-8", errors="strict")
    elif isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise ValueError("response_too_large")
        text = value
    else:
        raise ValueError("response_not_object")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate_response_key")
            result[key] = item
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError("non_finite_response")

    result = json.loads(
        text,
        object_pairs_hook=reject_duplicates,
        parse_constant=reject_constant,
    )
    if not isinstance(result, dict):
        raise ValueError("response_not_object")
    return result


def _masked_account_tag(value: Any) -> str | None:
    """Never place the configured account tag itself on the wire."""

    if not isinstance(value, str) or not value.strip():
        return None
    digest = hashlib.sha256(value.strip().encode("utf-8")).hexdigest()[:16]
    return f"tag_{digest}"


def _param_value(params: Any, name: str, default: Any = None) -> Any:
    if isinstance(params, Mapping):
        return params.get(name, default)
    return getattr(params, name, default)


def build_order_preview(
    signal: str,
    price: Any,
    params: Any,
    *,
    volume: Any = None,
    exchange: str | None = None,
    instrument_id: str | None = None,
) -> dict[str, Any]:
    """Build a buy/sell OPEN FAK mapping without dispatching it.

    The mapping mirrors the named arguments of the installed
    ``BaseStrategy.send_order`` API.  It is deliberately detached from any
    strategy instance, framework handle, order API, or network transport.
    """

    direction = signal.strip().casefold() if isinstance(signal, str) else ""
    if direction not in {"buy", "sell"}:
        raise ValueError("preview signal must be buy or sell")
    numeric_price = _finite_number(price, positive=True)
    if numeric_price is None:
        raise ValueError("preview price must be finite and positive")
    raw_volume = volume
    if raw_volume is None:
        raw_volume = _param_value(params, "order_volume", _param_value(params, "volume", None))
    numeric_volume = _finite_number(raw_volume, positive=True)
    if numeric_volume is None or not float(numeric_volume).is_integer():
        raise ValueError("preview volume must be a positive integer")
    selected_exchange = exchange if exchange is not None else _param_value(params, "exchange", "")
    selected_instrument = (
        instrument_id if instrument_id is not None else _param_value(params, "instrument_id", "")
    )
    if not isinstance(selected_exchange, str) or not selected_exchange.strip():
        raise ValueError("preview exchange is required")
    if not isinstance(selected_instrument, str) or not selected_instrument.strip():
        raise ValueError("preview instrument_id is required")
    return {
        "exchange": selected_exchange.strip(),
        "instrument_id": selected_instrument.strip(),
        "volume": int(numeric_volume),
        "price": numeric_price,
        "order_direction": direction,
        "order_type": "FAK",
        "offset": "OPEN",
    }


class AIBridge(BaseStrategy):
    """PythonGO strategy that observes ticks and never submits orders."""

    def __init__(
        self,
        transport: Transport | None = None,
        *,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        http_timeout: float = MAX_HTTP_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__()
        if not isinstance(queue_size, int) or not 1 <= queue_size <= 100:
            raise ValueError("queue_size must be between 1 and 100")
        if isinstance(http_timeout, bool) or not isinstance(http_timeout, (int, float)):
            raise ValueError("http_timeout must be positive")
        if not math.isfinite(float(http_timeout)) or not 0 < float(http_timeout) <= MAX_HTTP_TIMEOUT_SECONDS:
            raise ValueError("http_timeout must be between 0 and 3 seconds")

        self.params_map = Params()
        self.state_map = State()
        self._confirmed_connection = {
            "exchange": DEFAULT_EXCHANGE,
            "instrument_id": DEFAULT_INSTRUMENT_ID,
            "api_url": DEFAULT_API_URL,
        }
        self._confirmed_additional_symbols = ""
        self._contexts: dict[tuple[str, str], _SymbolContext] = {}
        self._subscriptions: set[tuple[str, str]] = set()
        self._callback_started_at = datetime.now(timezone.utc).isoformat()
        self._strategy_errors: deque = deque(maxlen=MAX_CALLBACK_EVENTS)
        self._context_lock = threading.RLock()
        self._callback_thread_id = threading.get_ident()
        self._snapshot_lock = threading.RLock()
        self._heartbeat_cache: OrderedDict[tuple[str, str], _RequestJob] = OrderedDict()
        self._heartbeat_templates: dict[tuple[str, str], _RequestJob] = {}
        self._heartbeat_due: dict[tuple[str, str], float] = {}
        self._trading_day: str | None = None
        self._lanes: dict[tuple[str, str], dict[str, Any]] = {}
        self._lane_generation = 0
        self._ready_jobs: deque = deque(maxlen=64)
        self._scheduler_wake = threading.Event()
        self._session_epoch = uuid.uuid4().hex
        self._last_response_sequence = 0
        self._last_roundtrip: tuple[float, str] | None = None
        self._sent_requests: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.tick_buffer: list[dict[str, Any]] = []
        self._transport = transport or _default_transport
        self._http_timeout = float(http_timeout)
        self._request_queue: queue.Queue[_RequestJob] = queue.Queue(maxsize=queue_size)
        self._response_queue: queue.Queue[_WorkerResponse] = queue.Queue(maxsize=queue_size * 2)
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._running = False
        self._sequence = 0
        self._instance_id = uuid.uuid4().hex
        self._market_status: str | None = None
        self._investor_id: str | None = None
        self._investor_scope_checked = False
        self._investor_retry_at = 0.0
        self._contract_cache: dict[str, Any] = {}
        self._last_tick: dict[str, Any] | None = None
        self._unsafe_enable_requested = False
        self._dropped_batches = 0
        self._dropped_responses = 0
        self._blocked_candidates = 0
        self._order_submissions = 0

    @property
    def instance_id(self) -> str:
        return self._instance_id

    @property
    def http_timeout(self) -> float:
        return self._http_timeout

    @property
    def worker_thread(self) -> threading.Thread | None:
        return self._worker

    def _assert_callback_thread(self) -> None:
        if threading.get_ident() != self._callback_thread_id:
            raise RuntimeError("native_callback_thread_required")

    def connection_status(self) -> dict[str, Any]:
        """Read-only transport status; GUI refresh still requires a callback."""
        with self._snapshot_lock:
            success = self._last_roundtrip
        age = None
        if success is not None:
            try:
                mono_age = time.monotonic() - success[0]
                wall_age = (datetime.now(timezone.utc) - datetime.fromisoformat(success[1])).total_seconds()
                if math.isfinite(mono_age) and min(mono_age, wall_age) >= 0:
                    age = max(mono_age, wall_age)
            except (ValueError, TypeError, OverflowError):
                pass
        status = "offline" if not self._running else "service_only" if success is None else "roundtrip" if age is not None and age <= LINK_TTL_SECONDS else "stale"
        return {"connection": status, "age_seconds": age, "stale_after_seconds": LINK_TTL_SECONDS,
                "client_verified": False, "execution_enabled": False, "ui_idle_refresh": "unsupported"}

    def _set_state(self, name: str, value: Any) -> None:
        try:
            setattr(self.state_map, name, value)
        except (AttributeError, TypeError, ValueError):
            pass

    def _output_main(self, message: str) -> None:
        try:
            self.output(message)
        except Exception as exc:
            self._set_state("last_error", _safe_text(f"output: {exc}"))

    def _publish_state(self) -> None:
        try:
            self.update_status_bar()
        except Exception as exc:
            self._set_state("last_error", _safe_text(f"status: {exc}"))

    @staticmethod
    def _truthy(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().casefold() in {"1", "true", "yes", "on", "y", "是"}
        return bool(value)

    def _force_observer_mode(self, *, announce: bool = False) -> None:
        if self._truthy(getattr(self.params_map, "enable_trading", False)):
            self._unsafe_enable_requested = True
            try:
                self.params_map.enable_trading = False
            except (AttributeError, TypeError, ValueError):
                pass
            if announce:
                self._output_main("观察模式已阻断 enable_trading=True；不会报单")
        self.trading = False
        self._set_state("framework_trading", False)
        self._set_state("order_submissions", 0)

    @staticmethod
    def _validate_symbol(exchange: str, instrument: str) -> None:
        if exchange not in {"SHFE", "INE", "DCE", "CZCE", "CFFEX", "GFEX"}:
            raise ValueError("invalid_exchange")
        # Explicit futures and C/P options only: no wildcard, expression or URL.
        if not re.fullmatch(
            r"[A-Za-z]{1,12}[0-9]{3,4}(?:[CPcp][0-9]{1,10}|-[CPcp]-[0-9]{1,10}|_[CPcp]_[0-9]{1,10})?",
            instrument,
        ):
            raise ValueError("invalid_contract")

    def _configured_symbols(self, params: Mapping[str, Any] | None = None) -> tuple[tuple[str, str], ...]:
        values = self.params_map.model_dump() if params is None else params
        primary = (values["exchange"], values["instrument_id"])
        self._validate_symbol(*primary)
        extra = values.get("additional_symbols", "")
        if not isinstance(extra, str) or len(extra) > MAX_SYMBOLS * 81:
            raise ValueError("invalid_additional_symbols")
        symbols = [primary]
        if extra.strip():
            entries = extra.split(";")
            if len(entries) >= MAX_SYMBOLS:
                raise ValueError("too_many_symbols")
            for entry in entries:
                parts = entry.strip().split(".")
                if len(parts) != 2:
                    raise ValueError("invalid_additional_symbol")
                symbol = tuple(parts)
                self._validate_symbol(*symbol)
                if symbol in symbols:
                    raise ValueError("duplicate_symbol")
                symbols.append(symbol)
        return tuple(symbols)

    def _primary_symbol(self) -> tuple[str, str]:
        return self.params_map.exchange, self.params_map.instrument_id

    def _context(self, symbol: tuple[str, str] | None = None) -> _SymbolContext:
        symbol = self._primary_symbol() if symbol is None else symbol
        if symbol not in self._contexts:
            self._contexts[symbol] = _SymbolContext()
        context = self._contexts[symbol]
        if symbol == self._primary_symbol():
            self.tick_buffer = context.ticks
            self._last_tick = context.last_tick
            self._contract_cache = context.contract
            self._market_status = context.market_status
        return context

    def _callback_symbol(self, value: Any) -> tuple[str, str] | None:
        exchange = _attr(value, "exchange", "Exchange", "ExchangeID")
        instrument = _attr(value, "instrument_id", "InstrumentID")
        if not isinstance(exchange, str) or not isinstance(instrument, str):
            return None
        symbol = (exchange.strip(), instrument.strip())
        try:
            allowed = self._subscriptions if self._running else self._configured_symbols()
        except (TypeError, ValueError):
            return None
        return symbol if symbol in allowed else None

    def _restore_connection(self, candidate: Mapping[str, Any]) -> dict[str, str]:
        connection = {key: candidate.get(key) for key in self._confirmed_connection}
        blank = {key for key, value in connection.items() if value is None or (isinstance(value, str) and not value.strip())}
        pair = ("exchange", "instrument_id")
        if any(key in blank for key in pair):
            for key in pair:
                if key not in blank and connection[key] != self._confirmed_connection[key]:
                    raise ValueError("incomplete_contract_identity")
            for key in pair:
                if key in blank:
                    connection[key] = self._confirmed_connection[key]
        if "api_url" in blank:
            connection["api_url"] = self._confirmed_connection["api_url"]
        if any(not isinstance(value, str) for value in connection.values()):
            raise ValueError("invalid_connection_parameter")
        connection = {key: value.strip() for key, value in connection.items()}
        self._validate_symbol(connection["exchange"], connection["instrument_id"])
        connection["api_url"] = validate_loopback_url(connection["api_url"])
        if self._running and connection != self._confirmed_connection:
            raise ValueError("stop_observer_before_changing_subscription")
        return connection

    def _prepare_connection(self) -> None:
        previous = {key: getattr(self.params_map, key) for key in self._confirmed_connection}
        resolved = self._restore_connection(previous)
        self._configured_symbols({**self.params_map.model_dump(), **resolved})
        if self._running and self.params_map.additional_symbols != self._confirmed_additional_symbols:
            raise ValueError("stop_observer_before_changing_subscription")
        for key, value in resolved.items():
            setattr(self.params_map, key, value)
        self._confirmed_connection = resolved
        self._confirmed_additional_symbols = self.params_map.additional_symbols
        if previous != resolved:
            _native.updateParam(StrategyID=self.strategy_id, Data=self.get_params())
            self._output_main("已恢复观察实例连接参数；交易仍关闭")

    def set_params(self, data: Mapping[str, Any]) -> None:
        """Preserve confirmed connection values when the client restores blanks."""

        self._assert_callback_thread()
        normalized: dict[str, Any] = {}
        unsafe = False
        for key, value in dict(data).items():
            target = "order_volume" if key == "volume" else key
            if target == "enable_trading" and self._truthy(value):
                normalized[target] = False
                unsafe = True
            else:
                normalized[target] = value
        candidate = self.params_map.model_dump()
        candidate.update(normalized)
        resolved = self._restore_connection(candidate)
        self._configured_symbols({**candidate, **resolved})
        if self._running and candidate["additional_symbols"] != self._confirmed_additional_symbols:
            raise ValueError("stop_observer_before_changing_subscription")
        normalized.update(resolved)
        # Validate every field before updating any live parameter.
        validated = Params()
        for key, value in {**candidate, **resolved}.items():
            if key not in validated.fields():
                raise ValueError("unknown strategy parameter")
            setattr(validated, key, value)
        super().set_params(normalized)
        self._confirmed_connection = resolved
        self._confirmed_additional_symbols = self.params_map.additional_symbols
        if unsafe:
            self._unsafe_enable_requested = True
            self._output_main("UI交易开关被观察模式重置为 False；不会报单")
        self._force_observer_mode()

    def on_init(self) -> None:
        self._assert_callback_thread()
        self._force_observer_mode(announce=True)
        self._prepare_connection()
        super().on_init()
        self._publish_state()

    def on_start(self) -> None:
        self._assert_callback_thread()
        self._force_observer_mode()
        if self._running:
            return
        if self._worker is not None and self._worker.is_alive():
            self._record_error("previous_worker_still_stopping")
            return
        if self._subscriptions:
            self._record_error("previous_subscriptions_not_stopped")
            return
        try:
            self._prepare_connection()
            symbols = self._configured_symbols()
        except (ValueError, TypeError):
            self._record_error("invalid_connection_parameters")
            self._set_state("connection_status", "invalid")
            self._output_main("观察参数不完整；未订阅行情")
            self._publish_state()
            return
        with self._snapshot_lock:
            self._heartbeat_cache.clear()
            self._heartbeat_templates.clear()
            self._heartbeat_due.clear()
            self._ready_jobs.clear()
            self._trading_day = None
            self._lane_generation += 1
            self._sent_requests.clear()
            self._session_epoch = uuid.uuid4().hex
            self._last_response_sequence = 0
            self._last_roundtrip = None
        self._discard_responses()
        with self._context_lock:
            self._contexts.clear()
            self._strategy_errors.clear()
            self._callback_started_at = datetime.now(timezone.utc).isoformat()
            self._investor_id = None
            self._investor_scope_checked = False
            self._investor_retry_at = 0.0
            for symbol in symbols:
                self._context(symbol)
        self._discard_pending_requests()
        # Keep room for each startup heartbeat, still bounded by MAX_SYMBOLS.
        self._request_queue.maxsize = max(self._request_queue.maxsize, len(symbols) * 2)
        self._resolve_investor()
        self._output_main(f"{ADAPTER_VERSION} {self.class_name} {self.params_map.exchange}.{self.params_map.instrument_id} observer_only=True")
        self._running = True
        for symbol in symbols:
            self._subscriptions.add(symbol)
            try:
                self.sub_market_data(exchange=symbol[0], instrument_id=symbol[1])
            except Exception:
                self._subscriptions.discard(symbol)
                self._record_error("subscription_failed")
        self._force_observer_mode(announce=True)
        if not self._subscriptions:
            self._running = False
            self._set_state("connection_status", "invalid")
            self._publish_state()
            return
        for symbol in symbols:
            if symbol in self._subscriptions:
                template = self._connection_only_envelope(symbol)
                with self._snapshot_lock:
                    self._heartbeat_templates[symbol] = _RequestJob(
                        "heartbeat", _endpoint(self.params_map.api_url, "/heartbeat"), template)
                self._enqueue_heartbeat(symbol)
        self._start_worker()
        self._set_state("connection_status", "connecting")
        self._publish_state()
        self._output_main(
            f"观察桥接启动 {self.params_map.exchange}.{self.params_map.instrument_id} "
            f"symbols={len(self._subscriptions)} batch={self.params_map.batch_size}；报单提交数=0"
        )

    def on_stop(self) -> None:
        """Stop only this adapter and subscriptions it actually owns."""

        self._assert_callback_thread()
        self._running = False
        self._force_observer_mode()
        self._stop_event.set()
        # One deadline governs the whole stop: worker and lane joins share it
        # instead of each receiving a fresh budget.
        stop_deadline = time.monotonic() + min(STOP_JOIN_TIMEOUT_SECONDS, self._http_timeout + 0.5)
        # Concurrent stop notification: every lane wakes at once, is retired
        # with its generation, and can never re-enter scheduling afterwards.
        with self._snapshot_lock:
            self._heartbeat_cache.clear()
            self._heartbeat_templates.clear()
            self._heartbeat_due.clear()
            self._ready_jobs.clear()
            self._sent_requests.clear()
            self._session_epoch = uuid.uuid4().hex
            self._lane_generation += 1
            retired = list(self._lanes.values())
            for lane in retired:
                lane["retired"] = True
                lane["inflight"] = lane.get("inflight")
                lane["wake"].set()
            self._last_roundtrip = None
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=max(0.0, stop_deadline - time.monotonic()))
        if worker is not None and worker.is_alive():
            self._record_error("worker_stop_timeout")
        else:
            self._worker = None
        for lane in retired:
            thread = lane.get("thread")
            if thread is not None:
                thread.join(timeout=max(0.0, stop_deadline - time.monotonic()))
        with self._snapshot_lock:
            for lane in retired:
                lane["job"] = None
                lane["next"] = None
                lane["deferred"] = None
            self._lanes.clear()
        self._discard_pending_requests()
        self._discard_responses()
        for symbol in tuple(self._subscriptions):
            try:
                self.unsub_market_data(exchange=symbol[0], instrument_id=symbol[1])
            except Exception:
                self._record_error("unsubscribe_failed")
            else:
                self._subscriptions.discard(symbol)
        self._force_observer_mode()
        self._set_state("connection_status", "offline")
        self._publish_state()
        self._output_main(
            f"观察桥接停止 tick={getattr(self.state_map, 'tick_count', 0)} "
            f"AI调用={getattr(self.state_map, 'ai_calls', 0)} 报单提交数=0"
        )

    def _rotate_observation_epoch(self) -> None:
        """Invalidate observations on a binding/day change, without execution I/O."""
        self._assert_callback_thread()
        now = time.monotonic()
        self._set_state("connection_status", "connecting")
        with self._context_lock, self._snapshot_lock:
            self._session_epoch = uuid.uuid4().hex
            self._last_roundtrip = None
            self._last_response_sequence = 0
            self._sent_requests.clear()
            self._heartbeat_cache.clear()
            self._heartbeat_due.clear()
            self._ready_jobs.clear()
            self._contexts.clear()
            self._strategy_errors.clear()
            self._callback_started_at = datetime.now(timezone.utc).isoformat()
            for symbol in sorted(self._subscriptions):
                self._context(symbol)
                self._heartbeat_templates[symbol] = _RequestJob(
                    "heartbeat", _endpoint(self.params_map.api_url, "/heartbeat"),
                    self._connection_only_envelope(symbol))
                self._heartbeat_due[symbol] = now + HEARTBEAT_INTERVAL_SECONDS
        self._discard_pending_requests()
        self._discard_responses()

    def _connection_only_envelope(self, symbol: tuple[str, str]) -> dict[str, Any]:
        """Known transport identity only; no invented account/quote snapshot."""
        return {"schema_version": 2, "exchange": symbol[0], "instrument_id": symbol[1],
                "instance_id": self._instance_id, "ticks": [], "heartbeat": True,
                "metadata": {"adapter_version": ADAPTER_VERSION, "adapter_class": self.class_name},
                "connection": {"instance_id": self._instance_id, "session_epoch": self._session_epoch,
                               "ui_idle_refresh": "unsupported", "snapshots": {}},
                "runtime": {"observer_only": True, "enable_trading": False,
                            "framework_trading": False, "order_submission_count": 0}}

    def _cache_snapshot(self, symbol: tuple[str, str], payload: dict[str, Any]) -> None:
        self._assert_callback_thread()
        if not self._running or symbol not in self._subscriptions:
            return
        snapshot = deepcopy(payload)
        snapshot["ticks"] = []
        snapshot["heartbeat"] = True
        with self._snapshot_lock:
            self._heartbeat_cache[symbol] = _RequestJob(
                "heartbeat", _endpoint(self.params_map.api_url, "/heartbeat"), snapshot)
            self._heartbeat_cache.move_to_end(symbol)
            while len(self._heartbeat_cache) > MAX_SYMBOLS:
                self._heartbeat_cache.popitem(last=False)

    def _enqueue_heartbeat(self, symbol: tuple[str, str]) -> None:
        self._assert_callback_thread()
        if not self._running or symbol not in self._subscriptions:
            return
        try:
            payload = self._build_envelope([], heartbeat=True, symbol=symbol)
            self._cache_snapshot(symbol, payload)
        except Exception:
            self._record_error("heartbeat_snapshot_failed")
            with self._snapshot_lock:
                self._heartbeat_cache.pop(symbol, None)
            payload = self._connection_only_envelope(symbol)
        self._enqueue("heartbeat", payload, _endpoint(self.params_map.api_url, "/heartbeat"))

    def _update_cached_quote(self, symbol: tuple[str, str], tick_data: dict[str, Any]) -> None:
        """A partial tick batch updates only quote evidence, never account age."""
        with self._snapshot_lock:
            job = self._heartbeat_cache.get(symbol)
            if job is None:
                return
            payload = deepcopy(job.payload)
            payload["quote"] = {"status": "available", **tick_data}
            payload["trading_day"] = tick_data.get("trading_day")
            payload["connection"]["snapshots"]["quote"] = deepcopy(self._contexts[symbol].quote_sample)
            self._heartbeat_cache[symbol] = _RequestJob(job.kind, job.url, payload)

    @staticmethod
    def _sample_metadata(sampled_at: str, source_at: str | None = None) -> dict[str, Any]:
        return {"sampled_at": sampled_at, "source_at": source_at, "received_at": sampled_at,
                "snapshot_id": uuid.uuid4().hex, "source": "pythongo",
                "completeness": "unverified"}

    def _discard_responses(self) -> None:
        while True:
            try:
                self._response_queue.get_nowait()
            except queue.Empty:
                return
            self._response_queue.task_done()

    def _discard_pending_requests(self) -> None:
        """Drop queued requests so a later start cannot send stale ticks."""

        while True:
            try:
                self._request_queue.get_nowait()
            except queue.Empty:
                return
            else:
                self._request_queue.task_done()

    def on_contract_status(self, status: Any) -> None:
        self._assert_callback_thread()
        with self._context_lock:
            self._drain_responses()
            self._force_observer_mode()
            symbol = self._callback_symbol(status)
            if symbol is None:
                return
            context = self._context(symbol)
            context.market_status = self._map_contract_status(_attr(status, "status", "Status"))
            if symbol == self._primary_symbol():
                self._market_status = context.market_status
                self._set_state("market_status", context.market_status or "unknown")
            self._enqueue_heartbeat(symbol)
            self._publish_state()

    @staticmethod
    def _map_contract_status(value: Any) -> str | None:
        """Map known textual callback values; never infer status from the clock."""

        if not isinstance(value, str):
            return None
        normalized = value.strip().casefold()
        mapping = {
            "continuous": "continuous",
            "continous": "continuous",
            "trading": "trading",
            "连续交易": "continuous",
            "auction": "auction",
            "call": "auction",
            "call_auction": "auction",
            "集合竞价报单": "auction",
            "集合竞价平衡": "auction",
            "集合竞价撮合": "auction",
            "beforetrading": "pre_open",
            "pre_open": "pre_open",
            "开盘前": "pre_open",
            "notrading": "closed",
            "closed": "closed",
            "非交易": "closed",
            "收盘": "closed",
        }
        return mapping.get(normalized)

    def on_tick(self, tick: Any) -> None:
        with self._context_lock:
            self._drain_responses()
            self._force_observer_mode()
            self._set_state("tick_count", int(getattr(self.state_map, "tick_count", 0) or 0) + 1)
            if not self._running:
                self._publish_state()
                return
            symbol = self._callback_symbol(tick)
            if symbol is None:
                return
            # Discovery can recover after login, even before a batch fills.
            self._resolve_investor()
            tick_data = self._tick_to_dict(tick)
            if tick_data is None:
                self._record_error("invalid_quote_or_time")
                self._publish_state()
                return
            context = self._context(symbol)
            day = tick_data.get("trading_day")
            with self._snapshot_lock:
                known_day = self._trading_day
                # A missing TradingDay never overwrites the last known boundary.
                if day:
                    self._trading_day = day
            day_changed = bool(known_day and day and day != known_day)
            if day_changed:
                self._rotate_observation_epoch()
                context = self._context(symbol)
            context.last_tick = tick_data
            context.quote_sample = self._sample_metadata(datetime.now(timezone.utc).isoformat(), tick_data.get("time"))
            self._update_cached_quote(symbol, tick_data)
            if day_changed:
                self._enqueue_heartbeat(symbol)
            if symbol == self._primary_symbol():
                self._last_tick = tick_data
            context.ticks.append(tick_data)
            if len(context.ticks) >= max(1, min(1000, int(self.params_map.batch_size))):
                ticks = context.ticks.copy()
                context.ticks.clear()
                try:
                    payload = self._build_envelope(ticks, symbol=symbol)
                    self._cache_snapshot(symbol, payload)
                    self._enqueue("analyze", payload, validate_loopback_url(self.params_map.api_url))
                except Exception:
                    self._record_error("quote_snapshot_failed")
            self._drain_responses()
            self._publish_state()

    def _tick_to_dict(self, tick: Any) -> dict[str, Any] | None:
        symbol = self._callback_symbol(tick)
        if symbol is None:
            return None
        last = _finite_number(_attr(tick, "last_price", "LastPrice"), positive=True)
        if last is None:
            return None
        timestamp = _aware_iso(_attr(tick, "datetime", "Datetime"))
        if timestamp is None:
            return None
        context = self._context(symbol)
        result: dict[str, Any] = {
            "time": timestamp,
            "last": last,
            "volume": _quantity(_attr(tick, "volume", "Volume")),
            "open_interest": _quantity(_attr(tick, "open_interest", "OpenInterest")),
            "turnover": _nonnegative(_attr(tick, "turnover", "Turnover")),
            "price_tick": _finite_number(context.contract.get("price_tick"), positive=True),
        }
        for output, property_name, raw_name in (
            ("open", "open_price", "OpenPrice"),
            ("high", "high_price", "HighPrice"),
            ("low", "low_price", "LowPrice"),
            ("pre_close", "pre_close_price", "PreClosePrice"),
            ("pre_settlement", "pre_settlement_price", "PreSettlementPrice"),
            ("upper_limit", "upper_limit_price", "UpperLimitPrice"),
            ("lower_limit", "lower_limit_price", "LowerLimitPrice"),
        ):
            result[output] = _finite_number(_attr(tick, property_name, raw_name), positive=True)
        for side in ("bid", "ask"):
            for level in range(1, 6):
                result[f"{side}{level}"] = _finite_number(
                    _attr(tick, f"{side}_price{level}", f"{side.title()}Price{level}"), positive=True,
                )
                result[f"{side}_volume{level}"] = _quantity(
                    _attr(tick, f"{side}_volume{level}", f"{side.title()}Volume{level}"),
                )
        if result["bid1"] is not None and result["ask1"] is not None and result["bid1"] > result["ask1"]:
            return None
        trading_day = _parse_day(_attr(tick, "trading_day", "TradingDay"))
        if trading_day is not None:
            result["trading_day"] = trading_day
        return result

    @staticmethod
    def _order_status(value: Any) -> tuple[str, bool | None]:
        # Installed pythongo.types.TypeOrderStatus; unknown codes stay unknown.
        mapping = {
            "未成交": ("pending", True),
            "部分成交": ("partially_filled", True),
            "已报入未应答": ("submitted", True),
            "部分撤单还在队列中": ("partially_cancelled_pending", True),
            "部成部撤还在队列中": ("partially_filled_cancelled_pending", True),
            "待报入": ("pending_submission", True),
            "交易员已报入": ("submitted", True),
            "全部成交": ("filled", False),
            "部成部撤": ("partially_filled_cancelled", False),
            "已撤销": ("cancelled", False),
            "投资经理驳回": ("rejected", False),
            "交易员驳回": ("rejected", False),
        }
        return mapping.get(value, ("unknown", None)) if isinstance(value, str) else ("unknown", None)

    @staticmethod
    def _event_sides(value: Any) -> dict[str, Any]:
        direction = _attr(value, "direction", "Direction")
        offset = _attr(value, "offset", "Offset")
        return {
            "direction": {"0": "buy", "1": "sell"}.get(direction) if isinstance(direction, str) else None,
            "offset": {"0": "open", "1": "close", "3": "close_today"}.get(offset) if isinstance(offset, str) else None,
        }

    @staticmethod
    def _remember(records: OrderedDict, key: str, event: dict[str, Any]) -> bool:
        previous = records.pop(key, {})
        # Partial native callbacks must not erase already observed fields.
        merged = {**previous, **{name: value for name, value in event.items() if value is not None}}
        for name, value in event.items():
            merged.setdefault(name, value)
        if "status" in event:
            # Unknown status must also clear the previous pending classification.
            merged["pending"] = event.get("pending")
        records[key] = merged
        if len(records) > MAX_CALLBACK_RECORDS:
            records.popitem(last=False)
            return True
        return False

    def _observe_order(self, order: Any, callback: str) -> None:
        with self._context_lock:
            self._force_observer_mode()
            self._drain_responses()
            symbol = self._callback_symbol(order)
            if not self._running or symbol is None:
                return
            context = self._context(symbol)
            order_id = _identifier(_attr(order, "order_id", "OrderID"))
            system_id = _identifier(_attr(order, "order_sys_id", "OrderSysID"))
            key = "local:" + order_id if order_id is not None else "exchange:" + system_id if system_id else None
            if key is not None:
                # Merge an earlier callback that knew only the exchange ID.
                previous_key = next((name for name, item in context.orders.items()
                                     if system_id and item.get("order_sys_id") == system_id), None)
                if previous_key is not None and previous_key != key:
                    previous = context.orders.pop(previous_key)
                    if order_id is None:
                        key = previous_key
                    context.orders.setdefault(key, previous)
                raw_status = _attr(order, "status", "Status")
                status, pending = self._order_status(raw_status)
                if raw_status in (None, "") and callback == "on_order_cancel":
                    status, pending = "cancelled", False
                elif raw_status in (None, "") and key in context.orders:
                    status = context.orders[key].get("status", "unknown")
                    pending = context.orders[key].get("pending")
                event = {
                    "callback": callback, "observed_at": datetime.now(timezone.utc).isoformat(),
                    "exchange": symbol[0], "instrument_id": symbol[1],
                    "order_id": order_id, "order_sys_id": system_id,
                    "price": _finite_number(_attr(order, "price", "Price"), positive=True),
                    **self._event_sides(order),
                    "total_volume": _quantity(_attr(order, "total_volume", "TotalVolume")),
                    "traded_volume": _quantity(_attr(order, "traded_volume", "TradedVolume")),
                    "cancel_volume": _quantity(_attr(order, "cancel_volume", "CancelVolume")),
                    "status": status, "pending": pending,
                    "order_time": _aware_iso(_attr(order, "order_time", "OrderTime")),
                    "cancel_time": _aware_iso(_attr(order, "cancel_time", "CancelTime")),
                }
                context.orders_truncated |= self._remember(context.orders, key, event)
            self._enqueue_heartbeat(symbol)
            self._publish_state()

    def on_order(self, order: Any) -> None:
        self._observe_order(order, "on_order")

    def on_order_trade(self, order: Any) -> None:
        self._observe_order(order, "on_order_trade")

    def on_order_cancel(self, order: Any) -> None:
        self._observe_order(order, "on_order_cancel")

    def on_cancel(self, order: Any) -> None:
        # CancelOrderData confirms a cancel callback, not a complete order query.
        self._observe_order(order, "on_cancel")

    def on_trade(self, trade: Any, log: bool = True) -> None:
        with self._context_lock:
            self._force_observer_mode()
            self._drain_responses()
            symbol = self._callback_symbol(trade)
            if not self._running or symbol is None:
                return
            context = self._context(symbol)
            trade_id = _identifier(_attr(trade, "trade_id", "TradeID"))
            if trade_id is not None:
                event = {
                    "callback": "on_trade", "observed_at": datetime.now(timezone.utc).isoformat(),
                    "exchange": symbol[0], "instrument_id": symbol[1], "trade_id": trade_id,
                    "order_id": _identifier(_attr(trade, "order_id", "OrderID")),
                    "order_sys_id": _identifier(_attr(trade, "order_sys_id", "OrderSysID")),
                    "trade_time": _aware_iso(_attr(trade, "trade_time", "TradeTime")),
                    "price": _finite_number(_attr(trade, "price", "Price"), positive=True),
                    "volume": _quantity(_attr(trade, "volume", "Volume")),
                    **self._event_sides(trade),
                }
                context.trades_truncated |= self._remember(context.trades, trade_id, event)
            self._enqueue_heartbeat(symbol)
            self._publish_state()

    def on_error(self, error: Any) -> None:
        with self._context_lock:
            self._force_observer_mode()
            self._drain_responses()
            # Installed raw callbacks expose errCode/errMsg. Messages may carry
            # account IDs or memos, so only the code is retained or published.
            code = _identifier(_attr(error, "errCode", "ErrorID", "error_code"))
            self._record_error("native_error" + ("_" + code if code else ""))
            if not self._running:
                return
            symbol = self._callback_symbol(error)
            has_identity = any(_attr(error, name) not in (None, "")
                               for name in ("exchange", "Exchange", "ExchangeID", "instrument_id", "InstrumentID"))
            if has_identity and symbol is None:
                return
            event = {
                "callback": "on_error", "observed_at": datetime.now(timezone.utc).isoformat(),
                "error_code": code, "scope": "contract" if symbol else "strategy",
                "exchange": symbol[0] if symbol else None,
                "instrument_id": symbol[1] if symbol else None,
            }
            if symbol is not None:
                self._context(symbol).errors.append(event)
                self._enqueue_heartbeat(symbol)
            else:
                self._strategy_errors.append(event)
                for owned in sorted(self._subscriptions):
                    self._enqueue_heartbeat(owned)
            self._publish_state()

    def _start_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._stop_event.clear()
        self._worker = threading.Thread(
            target=self._worker_loop,
            name=f"infiny-observer-{self._instance_id[:8]}",
            daemon=True,
        )
        self._worker.start()

    def _enqueue(self, kind: str, payload: dict[str, Any], url: str) -> bool:
        job = _RequestJob(kind=kind, url=validate_loopback_url(url), payload=payload)
        try:
            self._request_queue.put_nowait(job)
            return True
        except queue.Full:
            try:
                self._request_queue.get_nowait()
                self._request_queue.task_done()
            except queue.Empty:
                pass
            try:
                self._request_queue.put_nowait(job)
            except queue.Full:
                self._dropped_batches += 1
                self._set_state("dropped_batches", self._dropped_batches)
                return False
            self._dropped_batches += 1
            self._set_state("dropped_batches", self._dropped_batches)
            return True

    def _stamp_transport(self, payload: dict[str, Any]) -> None:
        payload["batch_id"] = str(uuid.uuid4())
        payload["sequence"] = self._next_sequence()
        payload["produced_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        payload["connection"].update({"instance_id": self._instance_id, "session_epoch": self._session_epoch,
                                      "sequence": payload["sequence"], "request_id": payload["batch_id"],
                                      "sent_at": payload["produced_at"]})

    @staticmethod
    def _payload_symbol(payload: Mapping[str, Any]) -> tuple[str, str] | None:
        exchange, instrument = payload.get("exchange"), payload.get("instrument_id")
        if isinstance(exchange, str) and isinstance(instrument, str):
            return exchange, instrument
        return None

    def _send_job(self, job: _RequestJob) -> None:
        if self._stop_event.is_set():
            return
        payload = deepcopy(job.payload)
        is_heartbeat = payload.get("heartbeat") is True
        budget = HEARTBEAT_HTTP_BUDGET if is_heartbeat else self._http_timeout
        with self._snapshot_lock:
            connection = payload.setdefault("connection", {})
            if connection.get("session_epoch", self._session_epoch) != self._session_epoch:
                return
            self._stamp_transport(payload)
            connection = deepcopy(payload["connection"])
            self._sent_requests[connection["request_id"]] = connection
            while len(self._sent_requests) > 64:
                self._sent_requests.popitem(last=False)
        if self._stop_event.is_set():
            return
        sent_mono = time.monotonic()
        # Last-moment admission fence: a stop or epoch change that happened
        # after stamping (including while this send was paused) must never
        # let the transport start. This is the final check before the wire.
        if self._stop_event.is_set() or self._session_epoch != connection.get("session_epoch"):
            return
        cost = budget
        try:
            result = self._transport(job.url, payload, budget)
            status, body = self._transport_result(result)
            received_mono = time.monotonic()
            cost = max(0.0, received_mono - sent_mono)
            received_at = datetime.now(timezone.utc).isoformat()
            elapsed = received_mono - sent_mono
            wall_age = (datetime.fromisoformat(received_at) - datetime.fromisoformat(connection["sent_at"].replace("Z", "+00:00"))).total_seconds()
            if (not math.isfinite(elapsed) or not 0 <= elapsed <= budget
                    or not 0 <= wall_age <= PACKET_TTL_SECONDS):
                raise TimeoutError("response_deadline_exceeded")
            response = _WorkerResponse(job.kind, status, body, connection=connection,
                                       received_mono=received_mono, received_at=received_at)
        except Exception as exc:
            response = _WorkerResponse(job.kind, None, None, _safe_text(exc), connection=connection)
        finally:
            # Each heartbeat owns its own cadence; a slow symbol cannot silence
            # healthy ones and a queued batch cannot delay the next beat.
            symbol = self._payload_symbol(payload)
            if symbol is not None:
                with self._snapshot_lock:
                    lane = self._lanes.get(symbol)
                    if lane is not None:
                        lane["last_cost"] = cost if math.isfinite(cost) else math.inf
                    if (is_heartbeat and symbol in self._heartbeat_templates
                            and connection.get("session_epoch") == self._session_epoch):
                        # Cadence anchors at send start: a concurrent lane's
                        # clock effect must not extend this symbol's interval.
                        self._heartbeat_due[symbol] = sent_mono + HEARTBEAT_INTERVAL_SECONDS
        if not self._stop_event.is_set():
            # Memory only: link evidence can advance while the GUI awaits a callback.
            if response.error is None and response.status is not None and 200 <= response.status < 300:
                try:
                    body = _strict_object(response.body)
                    if (set(body) == {"signal", "reason"} and body["signal"] == "hold"
                            and isinstance(body["reason"], str) and 0 < len(body["reason"]) <= MAX_REASON_LENGTH
                            and not any(ord(c) < 32 for c in body["reason"])):
                        with self._snapshot_lock:
                            if connection["session_epoch"] == self._session_epoch:
                                self._last_roundtrip = (response.received_mono, response.received_at)
                except (ValueError, TypeError, UnicodeError):
                    pass
            self._put_response(response)

    def _new_lane(self) -> dict[str, Any]:
        # Unknown transport cost sorts last: a symbol that has not yet proven
        # expensive cannot let backlog claims preempt a known-cheap beat.
        return {"wake": threading.Event(), "busy": False, "job": None, "next": None,
                "deferred": None, "inflight": False, "retired": False,
                "generation": self._lane_generation, "last_cost": math.inf, "thread": None}

    def _lane_loop(self, symbol: tuple[str, str], lane: dict[str, Any]) -> None:
        """One bounded transport lane per symbol; network/clock/memory only.

        The thread is bound to exactly this lane object for its whole life:
        after a stop or restart the registry maps the symbol to a new
        generation object, so a retiring thread can never resolve, clear or
        modify a replacement lane by symbol."""
        while not self._stop_event.is_set() and not lane.get("retired"):
            lane["wake"].wait(0.25)
            lane["wake"].clear()
            if self._stop_event.is_set() or lane.get("retired"):
                return
            while not self._stop_event.is_set() and not lane.get("retired"):
                # Drain every accepted slot before sleeping again: chained,
                # deferred and reserved work can never be silently lost.
                job = None
                with self._snapshot_lock:
                    if lane.get("retired"):
                        return
                    for source in ("next", "deferred", "job"):
                        candidate = lane.get(source)
                        if candidate is not None:
                            lane[source] = None
                            job = candidate
                            break
                    if job is not None:
                        lane["busy"] = False
                        lane["inflight"] = True
                if job is None:
                    break
                self._scheduler_wake.set()
                try:
                    self._send_job(job)
                except Exception:
                    # Lanes never touch native, GUI or published state; a lane
                    # bug must not leak tracebacks or block the other lanes.
                    pass
                finally:
                    with self._snapshot_lock:
                        lane["inflight"] = False
                    self._scheduler_wake.set()


    def _wake_all_lanes(self) -> None:
        with self._snapshot_lock:
            lanes = list(self._lanes.values())
        for lane in lanes:
            lane["wake"].set()
        self._scheduler_wake.set()

    def _spawn_lane(self, symbol: tuple[str, str]) -> None:
        if symbol in self._lanes:
            return
        lane = self._new_lane()
        self._lanes[symbol] = lane
        thread = threading.Thread(
            target=self._lane_loop, args=(symbol, lane),
            name=f"infiny-lane-{symbol[1][:12]}-{self._instance_id[:6]}", daemon=True)
        lane["thread"] = thread
        thread.start()

    def _worker_loop(self) -> None:
        """Central scheduler: assigns at most one send per cycle, oldest due
        beat first, with a bounded fairness share for queued one-shot jobs.
        Slow symbols are isolated in their own lanes, so a healthy symbol's
        cadence never waits behind another symbol's transport."""
        beat_streak = 0
        try:
            while not self._stop_event.is_set():
                # Pre-decision barrier: let in-flight sends finish (bounded
                # grace) so arbitration sees a settled clock. Transports that
                # outlast the grace never block the other lanes in practice.
                settle = time.monotonic() + 0.015
                while not self._stop_event.is_set() and math.isfinite(settle) and time.monotonic() < settle:
                    with self._snapshot_lock:
                        unsettled = any(
                            lane.get("inflight") or lane.get("job") is not None
                            or lane.get("next") is not None or lane.get("deferred") is not None
                            for lane in self._lanes.values())
                    if not unsettled:
                        break
                    self._scheduler_wake.wait(0.001)
                if self._stop_event.is_set():
                    break
                pulled = None
                try:
                    pulled = self._request_queue.get(timeout=0.002)
                except queue.Empty:
                    pass
                if pulled is not None:
                    self._request_queue.task_done()
                assigned_symbol = None
                with self._snapshot_lock:
                    now = time.monotonic()
                    if pulled is not None:
                        self._ready_jobs.append((pulled, now))
                    for symbol in self._heartbeat_templates:
                        self._spawn_lane(symbol)
                    due_symbol = None
                    due_is_cheap = False
                    if self._running and math.isfinite(now):
                        # Selection and job lookup share one atomic snapshot:
                        # a concurrent stop can never leave a dangling lookup.
                        # Order: proven-cheap confirmations first so a backlog
                        # of expensive transports cannot delay them past their
                        # cadence; never-served symbols before proven-expensive
                        # ones so every subscription gets its first beat.
                        def _rank(item_symbol: tuple[str, str], item_at: float) -> tuple[int, float, tuple[str, str]]:
                            lane_cost = self._lanes.get(item_symbol, {}).get("last_cost", math.inf)
                            cost_class = 0 if lane_cost == 0 else 1 if math.isinf(lane_cost) else 2
                            return cost_class, item_at, item_symbol

                        candidates = sorted(
                            _rank(symbol, at)
                            for symbol, at in self._heartbeat_due.items()
                            if at <= now and symbol in self._heartbeat_templates
                            and not self._lanes.get(symbol, {}).get("inflight")
                            and self._lanes.get(symbol, {}).get("job") is None
                            and self._lanes.get(symbol, {}).get("deferred") is None)
                        if candidates:
                            due_symbol = candidates[0][2]
                        due_is_cheap = bool(candidates) and candidates[0][0] == 0
                    ready = None
                    for entry in self._ready_jobs:
                        job, queued_at = entry
                        symbol = self._payload_symbol(job.payload)
                        if symbol is None:
                            continue
                        lane = self._lanes.get(symbol)
                        if lane is None:
                            # A one-shot may arrive before any template (direct
                            # enqueue without on_start): spawn its lane here.
                            self._spawn_lane(symbol)
                            lane = self._lanes[symbol]
                        if (not lane.get("inflight")
                                and (lane.get("job") is None or lane.get("deferred") is None)):
                            ready = (symbol, entry, (now - queued_at) > HEARTBEAT_INTERVAL_SECONDS)
                            break
                    # A due cheap confirmation is never preempted by the
                    # fairness share; only expensive/backlog dues yield slots.
                    # An aged one-shot may displace a due beat at most once
                    # per beat (alternation), and against a cheap beat only
                    # as a same-lane swap (the beat is deferred, not lost).
                    displace_aged = (ready is not None and ready[2]
                                     and beat_streak == 0 and due_symbol is not None
                                     and (not due_is_cheap or due_symbol == ready[0]))
                    if ready is not None and (due_symbol is None
                                              or displace_aged
                                              or (beat_streak >= 3 and not due_is_cheap)):
                        self._ready_jobs.remove(ready[1])
                        self._spawn_lane(ready[0])
                        lane = self._lanes[ready[0]]
                        # Handoff is atomic with the selection: a displaced
                        # pending job moves to the deferred slot, it is never
                        # overwritten or dropped.
                        if lane.get("job") is not None:
                            lane["deferred"] = lane["job"]
                        lane["job"] = ready[1][0]
                        lane["busy"] = True
                        assigned_symbol = ready[0]
                        beat_streak = 1 if displace_aged else 0
                    elif due_symbol is not None:
                        job = self._heartbeat_cache.get(due_symbol) or self._heartbeat_templates.get(due_symbol)
                        if job is not None:
                            self._spawn_lane(due_symbol)
                            lane = self._lanes[due_symbol]
                            lane["job"] = job
                            lane["busy"] = True
                            assigned_symbol = due_symbol
                            beat_streak += 1
                    if ready is None:
                        # An aged one-shot whose lane is still in flight chains
                        # onto that lane: it runs right after the in-flight
                        # send without taking an arbitration slot from a beat.
                        for entry in list(self._ready_jobs):
                            job, queued_at = entry
                            symbol = self._payload_symbol(job.payload)
                            lane = self._lanes.get(symbol) if symbol is not None else None
                            if (lane is not None and lane.get("inflight")
                                    and lane.get("next") is None
                                    and (now - queued_at) > HEARTBEAT_INTERVAL_SECONDS):
                                self._ready_jobs.remove(entry)
                                lane["next"] = job
                                break
                if self._stop_event.is_set():
                    break
                if assigned_symbol is not None:
                    self._lanes[assigned_symbol]["wake"].set()
        finally:
            # Retirement joins belong to on_stop's single shared deadline;
            # the scheduler itself only wakes lanes and exits promptly so a
            # stopped worker can never consume the stop budget twice.
            self._wake_all_lanes()

    @staticmethod
    def _transport_result(result: Any) -> tuple[int, bytes | Mapping[str, Any]]:
        if isinstance(result, tuple) and len(result) == 2:
            status, body = result
            try:
                status_int = int(status)
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid_http_status") from exc
        else:
            status_int, body = 200, result
        if isinstance(body, Mapping):
            return status_int, dict(body)
        if isinstance(body, str):
            body = body.encode("utf-8")
        if not isinstance(body, (bytes, bytearray)):
            raise ValueError("response_body_invalid")
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("response_too_large")
        return status_int, bytes(body)

    def _put_response(self, response: _WorkerResponse) -> None:
        try:
            self._response_queue.put_nowait(response)
            return
        except queue.Full:
            try:
                self._response_queue.get_nowait()
                self._response_queue.task_done()
            except queue.Empty:
                pass
            try:
                self._response_queue.put_nowait(response)
            except queue.Full:
                pass
            self._dropped_responses += 1

    def _drain_responses(self) -> None:
        self._assert_callback_thread()
        while True:
            try:
                response = self._response_queue.get_nowait()
            except queue.Empty:
                break
            try:
                self._handle_response(response)
            finally:
                self._response_queue.task_done()
        self._set_state("dropped_responses", self._dropped_responses)
        self._set_state("dropped_batches", self._dropped_batches)
        self._set_state("blocked_candidates", self._blocked_candidates)
        self._set_state("order_submissions", 0)
        if self.connection_status()["connection"] == "stale":
            self._set_state("connection_status", "stale")

    def _handle_response(self, response: _WorkerResponse) -> None:
        self._assert_callback_thread()
        if response.connection is not None:
            with self._snapshot_lock:
                meta = response.connection
                expected = self._sent_requests.pop(meta.get("request_id"), None)
                if (self._stop_event.is_set() or expected != meta
                        or meta.get("instance_id") != self._instance_id
                        or meta.get("session_epoch") != self._session_epoch
                        or meta.get("sequence", 0) <= self._last_response_sequence):
                    return
                self._last_response_sequence = meta["sequence"]
        if response.error:
            self._record_error(response.error)
            self._set_state("connection_status", "error")
            self._output_main(f"桥接请求失败：{_safe_text(response.error, maximum=60)}")
            return
        if response.status is None or not 200 <= response.status < 300:
            self._record_error(f"http_{response.status or 'unknown'}")
            self._set_state("connection_status", "error")
            return
        try:
            body = _strict_object(response.body or b"")
            if set(body) != {"signal", "reason"}:
                raise ValueError("unexpected_response_keys")
            signal = body["signal"]
            reason = body["reason"]
            if not isinstance(signal, str) or signal.casefold() not in {"hold", "buy", "sell"}:
                raise ValueError("unexpected_signal")
            signal = signal.casefold()
            if not isinstance(reason, str) or not reason or len(reason) > MAX_REASON_LENGTH:
                raise ValueError("invalid_reason")
            if any(ord(char) < 32 for char in reason):
                raise ValueError("invalid_reason")
        except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self._record_error(exc)
            self._set_state("connection_status", "protocol_error")
            self._set_state("last_signal", "hold")
            self._set_state("last_reason", "invalid_response")
            self._output_main("桥接响应无效；保持 hold")
            return

        if response.kind == "analyze":
            self._set_state("ai_calls", int(getattr(self.state_map, "ai_calls", 0) or 0) + 1)
        self._set_state("last_signal", "hold")
        if signal in {"buy", "sell"}:
            # Only a current legal hold round trip proves the UI link; a
            # blocked candidate is not a connection success.
            self._blocked_candidates += 1
            self._set_state("last_reason", f"blocked_{signal}")
            self._output_main(
                f"外部候选 {signal} 已阻断（观察模式）；不会报单，原因为 {_safe_text(reason)}"
            )
        else:
            self._set_state("connection_status", "connected")
            self._set_state("last_reason", reason[:MAX_REASON_LENGTH])
            self._output_main(f"AI hold：{reason[:MAX_REASON_LENGTH]}")

    def _record_error(self, error: Any) -> None:
        self._set_state("last_error", _safe_text(error))

    def _next_sequence(self) -> int:
        with self._snapshot_lock:
            self._sequence += 1
            return self._sequence

    def _build_envelope(
        self, ticks: list[dict[str, Any]], *, heartbeat: bool = False,
        symbol: tuple[str, str] | None = None,
    ) -> dict[str, Any]:
        self._assert_callback_thread()
        with self._context_lock:
            self._force_observer_mode()
            sampled_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            symbol = self._primary_symbol() if symbol is None else symbol
            # One capture binds one account scope and epoch: the scope is
            # resolved once and re-validated after the reads; a mid-capture
            # change or a component fault rotates the epoch, discards every
            # partial account-scoped fact and recaptures exactly once.
            investor = self._resolve_investor()
            for capture_attempt in (0, 1):
                with self._snapshot_lock:
                    capture_epoch = self._session_epoch
                context = self._context(symbol)
                try:
                    contract = self._read_contract(symbol)
                    position = self._read_position_snapshot(symbol, investor=investor)
                    account = self._read_account_snapshot(investor=investor)
                except _CaptureFault:
                    self._record_error("capture_component_fault")
                    if capture_attempt == 0:
                        self._rotate_observation_epoch()
                        investor = self._resolve_investor()
                        continue
                    context = self._context(symbol)
                    contract = self._unknown_contract()
                    position, account = self._unknown_position(), self._unknown_account()
                    break
                resolved = self._resolve_investor()
                with self._snapshot_lock:
                    epoch_intact = self._session_epoch == capture_epoch and resolved == investor
                if not epoch_intact:
                    context = self._context(symbol)
                    contract = self._unknown_contract()
                    position, account = self._unknown_position(), self._unknown_account()
                break
            ticks = [{**item, "price_tick": item.get("price_tick") or contract.get("price_tick")} for item in ticks]
            latest = ticks[-1] if ticks else context.last_tick
            orders = list(context.orders.values())
            trades = list(context.trades.values())
            payload: dict[str, Any] = {
                "schema_version": 2,
                "metadata": {"adapter_version": ADAPTER_VERSION, "adapter_class": self.class_name,
                             "realtime_extensions": 1, "snapshot_observed_at": sampled_at},
                "connection": {"instance_id": self._instance_id, "session_epoch": self._session_epoch,
                               "ui_idle_refresh": "unsupported", "snapshots": {
                                   name: self._sample_metadata(sampled_at)
                                   for name in ("account", "position", "contract")}},
                "batch_id": str(uuid.uuid4()),
                "sequence": self._next_sequence(),
                "instance_id": self._instance_id,
                "exchange": symbol[0],
                "instrument_id": symbol[1],
                "account_tag": _masked_account_tag(self.params_map.account_tag),
                "produced_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "ticks": ticks,
                "trading_day": latest.get("trading_day") if latest else None,
                "market_status": context.market_status or "unknown",
                "position": position,
                "account": account,
                "contract": contract,
                "active_orders": {"status": "unknown", "count": None, "observed_count": len(orders),
                                  "coverage": CALLBACK_COVERAGE},
                "observed_orders": {
                    "status": "observing", "count": len(orders),
                    "pending_count": sum(item.get("pending") is True for item in orders),
                    "unknown_status_count": sum(item.get("pending") is None for item in orders),
                    "coverage": CALLBACK_COVERAGE, "capacity": MAX_CALLBACK_RECORDS,
                    "truncated": context.orders_truncated, "window_started_at": self._callback_started_at,
                },
                "observed_trades": {
                    "status": "observing", "count": len(trades), "coverage": CALLBACK_COVERAGE,
                    "capacity": MAX_CALLBACK_RECORDS, "truncated": context.trades_truncated,
                    "window_started_at": self._callback_started_at,
                },
                "orders": [dict(item) for item in orders[-MAX_CALLBACK_EVENTS:]],
                "trades": [dict(item) for item in trades[-MAX_CALLBACK_EVENTS:]],
                "errors": [dict(item) for item in sorted(
                    [*self._strategy_errors, *context.errors], key=lambda item: item["observed_at"],
                )[-MAX_CALLBACK_EVENTS:]],
                "capabilities": self._read_capabilities(context),
                "runtime": {
                    "enable_trading": False,
                    "observer_only": True,
                    "framework_trading": False,
                    "order_submission_count": 0,
                    "instance_id": self._instance_id,
                },
            }
            if latest:
                payload["quote"] = {"status": "available", **latest}
                # Tick source time survives non-market callbacks and timer pings.
                payload["connection"]["snapshots"]["quote"] = deepcopy(context.quote_sample) if context.quote_sample else self._sample_metadata(sampled_at, latest.get("time"))
            if heartbeat:
                payload["heartbeat"] = True
            return payload

    @staticmethod
    def _read_capabilities(context: _SymbolContext) -> dict[str, Any]:
        try:
            names = sorted(name for name in dir(_native)
                           if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", name)
                           and any(part in name.casefold() for part in ("margin", "commission", "fee")))[:32]
        except Exception:
            names = []
        return {
            "rates_query": "not_exposed_in_documented_api",
            "native_candidate_names": names,
            "source": RATE_SOURCE,
            "available_values": dict(context.rates),
        }

    def _resolve_investor(self) -> str | None:
        """Validate the bound account scope before every sampling.

        Discovery retries stay throttled while no account is bound, but a
        known binding must be re-verified on each callback sample: a changed
        or ambiguous member list invalidates the old epoch instead of
        refreshing the previous account's evidence.
        """
        self._assert_callback_thread()
        now = time.monotonic()
        cached = self._investor_id
        if cached is None and self._investor_scope_checked and now < self._investor_retry_at:
            return None
        self._investor_scope_checked = True
        self._investor_retry_at = now + INVESTOR_RETRY_SECONDS
        try:
            investors = infini.get_investor_list()
        except Exception:
            self._record_error("investor_scope_unavailable")
            if cached is not None:
                self._investor_id = None
                if self._running:
                    self._rotate_observation_epoch()
            return None
        if isinstance(investors, Mapping):
            # A record or keyed records must contain actual InvestorID values;
            # never use a mapping key or a list index as an account identifier.
            investors = [investors] if "InvestorID" in investors or "investor_id" in investors else list(investors.values())
        identifier: str | None = None
        if isinstance(investors, (list, tuple)) and len(investors) == 1:
            value = _attr(investors[0], "InvestorID", "investor_id")
            if isinstance(value, str) and value.strip() and len(value) <= 80:
                identifier = value.strip()
        if cached is not None and identifier != cached and self._running:
            self._rotate_observation_epoch()
        self._investor_id = identifier
        return identifier

    def _unknown_account(self) -> dict[str, Any]:
        result = {"status": "unknown", "available": None, "balance": None, "risk": None}
        result.update({name: None for name in (
            "commission", "position_profit", "close_profit", "margin",
            "frozen_margin", "frozen_commission", "frozen_cash")})
        return result

    def _read_account_snapshot(self, investor: str | None = None) -> dict[str, Any]:
        result = self._unknown_account()
        fields = {
            "commission": ("Fee", "Commission", "commission"),
            "position_profit": ("PositionProfit", "position_profit"),
            "close_profit": ("CloseProfit", "close_profit"),
            "margin": ("Margin", "margin"),
            "frozen_margin": ("FrozenMargin", "frozen_margin"),
            "frozen_commission": ("FrozenCommission", "frozen_commission"),
            "frozen_cash": ("FrozenCash", "frozen_cash"),
        }
        result.update({name: None for name in fields})
        if investor is None:
            investor = self._resolve_investor()
        if investor is None:
            return result
        try:
            raw = infini.get_investor_account(investor)
        except Exception:
            # A failed component query aborts the account-scoped capture.
            self._record_error("account_snapshot_unavailable")
            raise _CaptureFault("account_query_failed") from None
        raw_investor = _attr(raw, "InvestorID", "investor", "investor_id")
        if raw_investor is not None and raw_investor != investor:
            raise _CaptureFault("account_foreign_identity")
        result["available"] = _finite_number(_attr(raw, "Available", "available"))
        result["balance"] = _finite_number(_attr(raw, "Balance", "balance"))
        for name, aliases in fields.items():
            result[name] = _finite_number(_attr(raw, *aliases))
        rights = _finite_number(_attr(raw, "DynamicRights", "dynamic_rights"), positive=True)
        margin = result["margin"]
        result["risk"] = _finite_number(margin / rights) if margin is not None and rights else None
        if result["available"] is not None or result["balance"] is not None:
            result["status"] = "ready"
        return result

    def _read_position_snapshot(self, symbol: tuple[str, str] | None = None,
                                investor: str | None = None) -> dict[str, Any]:
        if investor is None:
            investor = self._resolve_investor()
        target_exchange, target_instrument = self._primary_symbol() if symbol is None else symbol
        if not isinstance(target_instrument, str) or not target_instrument.strip():
            return self._unknown_position()
        if not isinstance(target_exchange, str) or not target_exchange.strip():
            return self._unknown_position()
        if investor is None:
            return self._unknown_position()
        try:
            rows = infini.get_investor_position(investor, simple=True)
        except Exception:
            # A failed component query aborts the account-scoped capture.
            self._record_error("position_snapshot_unavailable")
            raise _CaptureFault("position_query_failed") from None
        if not isinstance(rows, list):
            raise _CaptureFault("position_query_invalid")
        filtered: list[Mapping[str, Any]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise _CaptureFault("position_row_invalid")
            instrument = row.get("InstrumentID")
            if not isinstance(instrument, str) or not instrument.strip():
                raise _CaptureFault("position_row_invalid")
            instrument = instrument.strip()
            if instrument != target_instrument:
                continue
            row_investor = _attr(row, "InvestorID", "investor_id")
            if row_investor is not None and row_investor != investor:
                raise _CaptureFault("position_foreign_identity")
            exchange = _attr(row, "Exchange", "ExchangeID")
            if exchange is not None:
                if not isinstance(exchange, str) or not exchange.strip():
                    raise _CaptureFault("position_row_invalid")
                if exchange.strip() != target_exchange:
                    raise _CaptureFault("position_foreign_exchange")
            if exchange is None:
                try:
                    identities = self._subscriptions or self._configured_symbols()
                except (TypeError, ValueError):
                    return self._unknown_position()
                if sum(item[1] == target_instrument for item in identities) > 1:
                    return self._unknown_position()
            filtered.append(row)
        return self._position_rows_to_plain(filtered)

    def _read_contract(self, symbol: tuple[str, str] | None = None) -> dict[str, Any]:
        self._assert_callback_thread()
        symbol = self._primary_symbol() if symbol is None else symbol
        context = self._context(symbol)
        try:
            raw = infini.get_instrument(*symbol)
        except Exception:
            # A failed component query aborts the account-scoped capture.
            self._record_error("contract_snapshot_unavailable")
            raise _CaptureFault("contract_query_failed") from None
        if isinstance(raw, Mapping):
            for names, expected in ((("Exchange", "ExchangeID"), symbol[0]), (("InstrumentID",), symbol[1])):
                identity = _attr(raw, *names)
                if identity is not None and identity != expected:
                    raw = None
                    break
        raw = raw if isinstance(raw, Mapping) else {}
        product_class = raw.get("ProductClass")
        option_type = raw.get("OptionsType")
        asset_class = {"1": "future", "2": "option"}.get(str(product_class))
        contract = {
            "status": "unknown",
            "price_tick": _finite_number(raw.get("PriceTick"), positive=True),
            "upper_limit": _finite_number(raw.get("UpperLimitPrice"), positive=True),
            "lower_limit": _finite_number(raw.get("LowerLimitPrice"), positive=True),
            "expiry": _parse_day(raw.get("ExpireDate")),
            "asset_class": asset_class,
            "strike": _finite_number(raw.get("StrikePrice"), positive=True),
            "underlying": _identifier(raw.get("UnderlyingInstrID")),
            "option_type": {"1": "call", "2": "put"}.get(str(option_type)),
            "multiplier": _quantity(raw.get("VolumeMultiple")),
            "product_id": _identifier(raw.get("ProductID")),
            "min_limit_order_volume": _quantity(raw.get("MinLimitOrderVolume")),
            "max_limit_order_volume": _quantity(raw.get("MaxLimitOrderVolume")),
        }
        for name in ("upper_limit", "lower_limit"):
            tick_limit = _finite_number((context.last_tick or {}).get(name), positive=True)
            if tick_limit is not None:
                contract[name] = tick_limit
        if any(value is not None for name, value in contract.items() if name != "status"):
            contract["status"] = "ready"
        context.contract = dict(contract)
        context.rates = {name: value for name in RATE_FIELDS
                         if (value := _nonnegative(raw.get(name))) is not None}
        if symbol == self._primary_symbol():
            self._contract_cache = context.contract
        return contract

    @staticmethod
    def _unknown_position() -> dict[str, Any]:
        return {
            "status": "unknown",
            "long": {"total": None, "today": None, "yesterday": None},
            "short": {"total": None, "today": None, "yesterday": None},
            "net": None,
        }

    @classmethod
    def _side_plain(cls, side: Any) -> dict[str, Any] | None:
        if side is None:
            return None
        if isinstance(side, Mapping) and not side:
            return {"total": 0, "today": 0, "yesterday": 0}
        total = _quantity(_attr(side, "total", "position", "Position", default=None))
        yesterday = _quantity(
            _attr(side, "yesterday", "yd_position_close", "YdPositionClose", default=None)
        )
        today = _quantity(_attr(side, "today", "td_position_close", "TodayPosition", default=None))
        if total is None:
            return None
        if yesterday is None and today is None:
            return None
        if yesterday is None:
            yesterday = total - today
        if today is None:
            today = total - yesterday
        if any(value < 0 for value in (total, today, yesterday)):
            return None
        if today + yesterday > total:
            return None
        return {"total": total, "today": today, "yesterday": yesterday}

    @classmethod
    def _position_to_plain(cls, position: Any) -> dict[str, Any]:
        long_side = cls._side_plain(_attr(position, "long", default=None))
        short_side = cls._side_plain(_attr(position, "short", default=None))
        if long_side is None or short_side is None:
            return cls._unknown_position()
        net = long_side["total"] - short_side["total"]
        return {
            "status": "ready",
            "long": long_side,
            "short": short_side,
            "net": net,
        }

    @classmethod
    def _position_rows_to_plain(cls, rows: list[Mapping[str, Any]]) -> dict[str, Any]:
        # The documented query has no completion barrier. Missing sides or a
        # missing breakdown never prove zero; known per-side totals stay
        # visible as observations and completeness/net remain unknown.
        if not rows:
            return cls._unknown_position()
        totals = {"long": [0, 0, True], "short": [0, 0, True]}  # total, yesterday, complete
        observed_sides = set()
        for row in rows:
            direction = row.get("Direction")
            side = {"多": "long", "空": "short"}.get(direction) if isinstance(direction, str) else None
            total = _quantity(row.get("Position"))
            yesterday = _quantity(row.get("YdPositionClose"))
            if side is None or total is None:
                return cls._unknown_position()
            observed_sides.add(side)
            totals[side][0] += total
            if yesterday is None or yesterday < 0 or yesterday > total:
                totals[side][2] = False
                continue
            totals[side][1] += yesterday
        complete = all(totals[side][2] for side in observed_sides)
        result: dict[str, Any] = {"status": "observed" if complete else "unknown"}
        for side in ("long", "short"):
            if side not in observed_sides:
                result[side] = {"total": None, "today": None, "yesterday": None}
                continue
            total, yesterday, side_complete = totals[side]
            if side_complete and _quantity(total) is not None and _quantity(yesterday) is not None:
                result[side] = {"total": total, "today": total - yesterday, "yesterday": yesterday}
            else:
                result[side] = {"total": total, "today": None, "yesterday": None}
        result["net"] = (result["long"]["total"] - result["short"]["total"]
                         if complete and len(observed_sides) == 2 else None)
        return result

    def _unknown_contract(self) -> dict[str, Any]:
        return {
            "status": "unknown", "price_tick": None, "upper_limit": None,
            "lower_limit": None, "expiry": None, "asset_class": None, "strike": None,
            "underlying": None, "option_type": None, "multiplier": None,
            "product_id": None, "min_limit_order_volume": None, "max_limit_order_volume": None,
        }

    def _safe_position(self) -> dict[str, Any]:
        """Compatibility helper for old strategy snippets; it remains read-only."""

        try:
            return self._read_position_snapshot()
        except _CaptureFault:
            return self._unknown_position()

    def send_order(self, *args: Any, **kwargs: Any) -> None:
        """Defense-in-depth override; no inherited order API can be reached."""

        self._force_observer_mode()
        self._output_main("观察模式阻断 send_order；报单提交数=0")
        return None

    def make_order_req(self, *args: Any, **kwargs: Any) -> None:
        """Block the lower-level inherited request path as well."""

        self._force_observer_mode()
        self._output_main("观察模式阻断 make_order_req；报单提交数=0")
        return None

    def auto_close_position(self, *args: Any, **kwargs: Any) -> None:
        """Block inherited close/flatten helpers; no live execution exists here."""

        self._force_observer_mode()
        self._output_main("观察模式阻断 auto_close_position；报单提交数=0")
        return None

    def cancel_order(self, *args: Any, **kwargs: Any) -> None:
        """Defense-in-depth override for cancellation as well."""

        self._force_observer_mode()
        self._output_main("观察模式阻断 cancel_order")
        return None


class AKObserverBridge(AIBridge):
    """Deployment-safe class name for a new, separate PythonGO instance."""

    pass


__all__ = [
    "AKObserverBridge",
    "AIBridge",
    "Params",
    "State",
    "build_order_preview",
    "validate_loopback_url",
]
