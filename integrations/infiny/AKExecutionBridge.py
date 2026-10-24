"""NE1 client execution strategy: self-contained, disabled by default.

Independent PythonGO execution entry (docs/infiny-native-execution-plan.md
§4/§5/§7). Shares nothing with the observer strategies: it never imports them,
never calls the observer service, and only talks to the local execution
service under ``/execution/v1/`` (default http://127.0.0.1:17779). Importing
or constructing this module has zero side effects: no thread starts, no native
attribute resolves, no account query, no network connection (NE1-H01/H37).

NE1-LM-R4 pairing last-mile (decision NE1-ARB-PAIRING-LASTMILE-001): a
zero-argument framework construction is directly usable. The first valid
``on_start`` opens the deterministic default store
``<this file's directory>/.ak_execution/strategy-<strategy_id>/`` (layout 2),
persists the instance identity, publishes the non-secret pairing request and
enters ``WAIT_PAIRING`` with zero anonymous HTTP. The credential written by
the user-operated handoff tool (``pairing.json``) is activated only against
the current active request, then a single authenticated null-service-epoch
disabled poll learns and verifies the service epoch before any other business
transport opens (RULING 01/02/03).

The transport codec and DTO rules mirror frozen NE1-C1
src/ak_trading/infiny_execution/native_contracts.py
(sha256 d9e47f30ff25fdaace1a541124cfa007e7bd042bc7c5b849996193573d5d4e3b)
constant-for-constant; tests/test_infiny_native_gates.py keeps them in sync
through golden vectors. The deployed file runs without the repository on
sys.path; repository types surface only through a lazy import on already
rejected bypass paths for legacy compatibility.

NE1-ARM-ARMING (decision NE1-ARB-ARMING-001, 2026-09-18; this file's
ARM-LM-C row of RULING 07): the parameter-form confirmation is replaced by
the interactive local arming mechanism. A frozen safe Params projection
(enable_trading=False, execution_mode="disabled", empty confirmation, zero
default budgets) stays permanent; admission now runs through a separate
eight-state local machine (DISARMED -> PREPARED_DISABLED -> ARM_READY ->
ARM_PENDING -> ARMED -> ARMED(send exhausted) -> REVOKE_PENDING ->
DISARMED_TERMINAL/RESTART_REQUIRED), a non-serializable in-memory ArmContext
and a restricted dual parameter hash: the pairing hash stays frozen at
on_start, while the target hash covers exactly the five override fields
execution_mode/risk_policy_id/authorization_id/max_native_sends/
max_native_cancels on a frozen copy of the same canonical encoding. The
user-operated tool delivers a one-time local credential; the client
consumes it exactly once (persistent tombstone first, then secret-slot
destruction, then the PARAMS_CONFIRMED control report), and only a legal
framework callback may install the context that flips the in-memory
``enable_trading_effective`` boolean. Bare Params switches, State text or
file booleans never authorize a native call.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import stat
import sys
import threading
import time
import uuid
import ctypes  # stdlib only; WinDLL bindings happen lazily, never at import
from collections import OrderedDict, deque
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from http.client import HTTPConnection, HTTPSConnection
from ipaddress import ip_address
from urllib.parse import urlsplit, urlunsplit

import INFINIGO as _native

ADAPTER_VERSION = "ne1-execution-20260918-arming-v1-snapshot-20260930-r1"

# ---------------------------------------------------------------------------
# frozen mirror of NE1-C1 native_contracts.py constants
# ---------------------------------------------------------------------------
TRANSPORT_SCHEMA_VERSION = "infiny_client_execution_transport_v1_1"
MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
MAX_NESTING = 32
MAX_EVENTS_PER_BATCH = 128
MAX_RAW_FIELDS_PER_EVENT = 64
DISPATCH_TTL_MS = 1500  # NE1-ARB-C1.2-OVF06-001: fixed maximum, no override exists
POLL_ARMED_MIN_INTERVAL_MS = 250
POLL_DISABLED_INTERVAL_MS = 5000
# POLL_TOO_FAST (429) with an undecodable body still backs off conservatively;
# the typed TransportErrorResponse retry_after_ms is authoritative when present.
POLL_BACKOFF_DEFAULT_MS = POLL_DISABLED_INTERVAL_MS

DEFAULT_EXECUTION_API_URL = "http://127.0.0.1:17779/execution/v1/poll"
# Deployment defaults (2026-09-18 urgent fix; observer-bridge pattern, cf.
# AIBridge.py): this InfiniTrader build exposes NO strategy parameter form,
# so the deployment values must live in the Params Field defaults or a real
# instance can never leave "parameters incomplete". Plain identifiers and a
# simulation account label only — never credentials. Every safety default
# stays untouched (enable_trading=False, execution_mode="disabled",
# max_native_sends/cancels=0); explicitly empty set_params values are still
# refused at on_start.
DEFAULT_EXCHANGE = "SHFE"
DEFAULT_INSTRUMENT_ID = "rb2610"
DEFAULT_HEDGE_FLAG = "1"
DEFAULT_INVESTOR_ID = "15000180250"  # user simulation account id, not a credential
MAX_HTTP_TIMEOUT_SECONDS = 3.0
STOP_JOIN_TIMEOUT_SECONDS = 3.5
MAX_SYMBOLS = 16
OUTBOX_MAX_ITEMS = 10000
OUTBOX_MAX_BYTES = 16 * 1024 * 1024
NOTIFY_QUEUE_MAX = 1024
SQLITE_BUSY_TIMEOUT_MS = 200

QUOTE_TTL_SECONDS = 3.0
ACCOUNT_TTL_SECONDS = 5.0
POSITION_TTL_SECONDS = 5.0
CONTRACT_TTL_SECONDS = 3600.0
PACKET_TTL_SECONDS = 10.0  # H07 data-packet freshness; never a ticket-lifetime cap (DTTL)
LINK_TTL_SECONDS = 15.0
FUTURE_SKEW_SECONDS = 2.0
SNAPSHOT_ACCOUNT_INTERVAL_SECONDS = 5.0
SNAPSHOT_CONTRACT_INTERVAL_SECONDS = 60.0
_CHINA_TIMEZONE = timezone(timedelta(hours=8))

DIRECTION_MAP = {"BUY": "0", "SELL": "1"}
OFFSET_MAP = {"OPEN": "0", "CLOSE": "1", "CLOSE_TODAY": "3", "CLOSE_YESTERDAY": "1"}
ORDER_TYPE_MAP = {"GFD": "0", "FAK": "1"}
YD_ONLY_EXCHANGES = frozenset({"SHFE", "INE"})
NATIVE_REQUEST_KEYS = ("StrategyID", "Exchange", "InstrumentID", "Volume", "Price",
                       "Direction", "OrderType", "Investor", "HedgeFlag", "Offset",
                       "Market", "Memo")

LEG_DIRECTIONS = frozenset({"BUY", "SELL"})
LEG_OFFSETS = frozenset({"OPEN", "CLOSE", "CLOSE_TODAY", "CLOSE_YESTERDAY"})
COMMAND_KINDS = frozenset({"SUBMIT", "CANCEL"})
RECEIPT_STAGES = frozenset({"CONSUMED", "RETURNED", "NO_CALL"})
RESULT_KINDS = frozenset({"LOCAL_ACK", "LOCAL_REJECTED", "EXCEPTION", "TIMEOUT", "INVALID_RETURN"})
CONTROL_KINDS = frozenset({"PARAMS_CONFIRMED", "GATE_CLOSED", "STOPPED", "GENERATION_REPORT"})
RESPONSE_KINDS = frozenset({"IDLE", "BLOCKED", "OFFER"})

CALLBACK_KIND_MAP = {
    "on_init": "ON_INIT", "on_start": "ON_START", "on_tick": "ON_TICK",
    "on_contract_status": "ON_CONTRACT_STATUS", "on_order": "ON_ORDER",
    "on_order_trade": "ON_ORDER_TRADE", "on_order_cancel": "ON_ORDER_CANCEL",
    "on_cancel": "ON_CANCEL", "on_trade": "ON_TRADE", "on_error": "ON_ERROR",
    "on_stop": "ON_STOP", "set_params": "SET_PARAMS",
}

_HEX = frozenset("0123456789abcdef")
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}")


class ContractError(ValueError):
    """Local mirror of the frozen contract error; never carries secrets."""


class PollBackoffRequest(Exception):
    """429 POLL_TOO_FAST: the C1.1 frozen TransportErrorResponse asks the
    client to withhold polling for retry_after_ms (Retry-After header mirror).
    Not a hard error: the control lane backs off instead of retrying."""

    def __init__(self, retry_after_ms):
        super().__init__(f"poll_too_fast_retry_after_{retry_after_ms}ms")
        self.retry_after_ms = retry_after_ms


def _require(condition, message):
    if not condition:
        raise ContractError(message)


def _identifier(value, name):
    _require(type(value) is str and _IDENTIFIER_RE.fullmatch(value) is not None,
             f"{name}: expected an opaque nonempty identifier")


def _hash64(value, name):
    _require(type(value) is str and len(value) == 64 and all(ch in _HEX for ch in value),
             f"{name}: expected lowercase sha256 hex")


def _integer(value, name, minimum=0):
    _require(type(value) is int and minimum <= value <= 2 ** 63 - 1,
             f"{name}: expected integer >= {minimum}, not bool")


def _utc_text(value, name):
    _require(type(value) is str and value.endswith("Z"), f"{name}: expected aware UTC ISO text")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ContractError(f"{name}: unparsable datetime") from exc
    _require(parsed.tzinfo is not None, f"{name}: naive time rejected")
    return parsed


def _decimal_text(value, name, *, positive=False):
    _require(type(value) is str and 0 < len(value) <= 64, f"{name}: expected decimal text")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ContractError(f"{name}: unparsable decimal") from exc
    _require(number.is_finite(), f"{name}: nonfinite decimal")
    if positive:
        _require(number > 0, f"{name}: must be positive")
    return number


def _now_utc():
    return datetime.now(timezone.utc)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_iso(value):
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    return datetime.fromisoformat(text)


# ---------------------------------------------------------------------------
# strict JSON codec (mirror of the engine's canonical rules)
# ---------------------------------------------------------------------------

def _reject_constant(_value):
    raise ContractError("nonfinite JSON number")


def _reject_duplicate(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def _depth_check(value, depth=0):
    _require(depth <= MAX_NESTING, "message nesting exceeds limit")
    if type(value) is dict:
        for item in value.values():
            _depth_check(item, depth + 1)
    elif type(value) is list:
        for item in value:
            _depth_check(item, depth + 1)


def canonical_dumps(payload):
    text = json.dumps(payload, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":"))
    encoded = text.encode("utf-8")
    _require(len(encoded) <= MAX_REQUEST_BYTES, "message too large")
    return text


def strict_loads(text, *, limit):
    _require(isinstance(text, (str, bytes, bytearray)), "invalid body")
    if isinstance(text, (bytes, bytearray)):
        _require(len(text) <= limit, "body too large")
        text = bytes(text).decode("utf-8", errors="strict")
    else:
        _require(len(text.encode("utf-8")) <= limit, "body too large")
    value = json.loads(text, object_pairs_hook=_reject_duplicate,
                       parse_constant=_reject_constant)
    _depth_check(value)
    return value


def wire_packet(type_name, data):
    return {"schema_version": TRANSPORT_SCHEMA_VERSION, "type": type_name, "data": data}


def sha256_hex(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _decimal_wire(value):
    if value == 0:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


# ---------------------------------------------------------------------------
# thin transport DTO mirrors (construction + strict decode of what the client
# sends or receives; rule-for-rule identical to NE1-C1)
# ---------------------------------------------------------------------------

def _validate_envelope_header(data):
    _require(type(data) is dict and set(data) >= {
        "request_id", "instance_id", "session_epoch", "generation",
        "service_epoch", "sequence", "sent_at", "params_sha256"},
        "transport envelope: missing header fields")
    for name in ("request_id", "instance_id", "session_epoch", "generation"):
        _identifier(data[name], name)
    if data["service_epoch"] is not None:
        _integer(data["service_epoch"], "service_epoch")
    _integer(data["sequence"], "sequence")
    _utc_text(data["sent_at"], "sent_at")
    _hash64(data["params_sha256"], "params_sha256")


def _validate_binding(data):
    _require(type(data) is dict and set(data) == {
        "account_key", "strategy_key", "strategy_id", "instance_id", "session_epoch",
        "trading_day", "exchange", "instrument_id", "hedge_flag"}, "binding: exact fields")
    for name in ("account_key", "strategy_key", "instance_id", "session_epoch",
                 "exchange", "instrument_id", "hedge_flag"):
        _identifier(data[name], name)
    _integer(data["strategy_id"], "strategy_id")
    _require(type(data["trading_day"]) is str and re.fullmatch(r"\d{4}-\d{2}-\d{2}", data["trading_day"]),
             "trading_day: expected ISO date")


def _validate_leg(data):
    _require(type(data) is dict and set(data) == {
        "leg_id", "index", "phase", "depends_on", "direction", "offset", "volume",
        "price", "position_bucket", "cash_reserve"}, "leg: exact fields")
    _identifier(data["leg_id"], "leg_id")
    _integer(data["index"], "index")
    _require(data["phase"] in {"OPEN", "CLOSE"}, "leg: unknown phase")
    _require(type(data["depends_on"]) is list, "leg.depends_on: expected array")
    for dep in data["depends_on"]:
        _identifier(dep, "depends_on")
    _require(data["direction"] in LEG_DIRECTIONS, "leg: unknown direction")
    _require(data["offset"] in LEG_OFFSETS, "leg: unknown offset")
    _integer(data["volume"], "volume", 1)
    _decimal_text(data["price"], "leg.price", positive=True)
    _require(data["position_bucket"] is None or type(data["position_bucket"]) is str,
             "leg.position_bucket")
    _decimal_text(data["cash_reserve"], "leg.cash_reserve")


def validate_dispatch_command(data):
    """Strict decode mirror of DispatchCommand; returns a normalized dict."""
    _require(type(data) is dict and set(data) == {
        "command_id", "kind", "attempt_id", "offer_id", "intent_id", "leg_id",
        "plan_id", "binding", "leg", "order_type", "price_tick", "contract_limits",
        "control_revision", "permit_id", "params_sha256", "issued_at", "expires_at",
        "order_id", "route_evidence_id", "cancel_id"}, "dispatch: exact fields")
    for name in ("command_id", "attempt_id", "offer_id", "intent_id", "leg_id", "permit_id"):
        _identifier(data[name], name)
    _require(data["kind"] in COMMAND_KINDS, "dispatch: unknown kind")
    _hash64(data["plan_id"], "plan_id")
    _hash64(data["params_sha256"], "params_sha256")
    _integer(data["control_revision"], "control_revision")
    _validate_binding(data["binding"])
    _validate_leg(data["leg"])
    _require(data["order_type"] in ORDER_TYPE_MAP, "dispatch: unsupported order type")
    tick = _decimal_text(data["price_tick"], "price_tick", positive=True)
    price = _decimal_text(data["leg"]["price"], "leg.price", positive=True)
    _require(price % tick == 0, "leg price is not on the tick grid")
    limits = data["contract_limits"]
    _require(type(limits) is dict and set(limits) == {"min_volume", "max_volume", "volume_step"},
             "contract_limits: exact fields")
    for name in ("min_volume", "max_volume", "volume_step"):
        _integer(limits[name], name, 1)
    _require(limits["min_volume"] <= limits["max_volume"], "inverted contract limits")
    issued = _utc_text(data["issued_at"], "issued_at")
    expires = _utc_text(data["expires_at"], "expires_at")
    _require(expires > issued, "dispatch validity must be a positive window")
    _require((expires - issued).total_seconds() * 1000 <= DISPATCH_TTL_MS,
             "dispatch validity exceeds the frozen TTL cap")
    if data["kind"] == "CANCEL":
        _require(data["order_id"] is not None and data["route_evidence_id"] is not None
                 and data["cancel_id"] is not None,
                 "cancel command requires journal order_id, route evidence and cancel_id")
        _integer(data["order_id"], "order_id")
        _identifier(data["route_evidence_id"], "route_evidence_id")
        _identifier(data["cancel_id"], "cancel_id")
    else:
        _require(data["order_id"] is None and data["route_evidence_id"] is None
                 and data["cancel_id"] is None, "submit command cannot carry cancel keys")
    return data


def validate_poll_response(packet):
    """Strict decode mirror of PollResponse."""
    _require(type(packet) is dict and set(packet) == {"schema_version", "type", "data"},
             "poll response: envelope shape")
    _require(packet["schema_version"] == TRANSPORT_SCHEMA_VERSION, "unknown schema version")
    _require(packet["type"] == "PollResponse", "unexpected response type")
    data = packet["data"]
    _validate_envelope_header(data)
    _require(type(data) is dict and set(data) >= {
        "received_at", "ledger_revision", "control_revision", "kind"},
        "poll response: missing fields")
    _utc_text(data["received_at"], "received_at")
    _integer(data["ledger_revision"], "ledger_revision")
    _integer(data["control_revision"], "control_revision")
    _require(data["kind"] in RESPONSE_KINDS, "poll response: unknown kind")
    _require(type(data.get("blockers", [])) is list, "blockers: expected array")
    for code in data.get("blockers", ()):
        _identifier(code, "blocker")
    offer = data.get("offer")
    if data["kind"] == "OFFER":
        _require(offer is not None, "OFFER response must carry the offer")
        validate_execution_offer(offer)
    else:
        _require(offer is None, "only OFFER responses carry an offer")
    return data


def validate_execution_offer(data):
    _require(type(data) is dict and set(data) == {
        "offer_id", "intent_id", "leg_id", "plan_id", "intent_hash", "leg_hash",
        "binding", "snapshot_id", "policy_id", "params_sha256", "control_revision",
        "expires_at"}, "offer: exact fields")
    for name in ("offer_id", "intent_id", "leg_id", "snapshot_id", "policy_id"):
        _identifier(data[name], name)
    for name in ("plan_id", "intent_hash", "leg_hash", "params_sha256"):
        _hash64(data[name], name)
    _integer(data["control_revision"], "control_revision")
    _validate_binding(data["binding"])
    _utc_text(data["expires_at"], "offer.expires_at")


def validate_transport_error_response(packet):
    """Decode mirror of TransportErrorResponse (RULING 03) for the 429
    backoff path; only the fields the client acts on are typed-checked.
    POLL_TOO_FAST maps one-to-one to http 429 in the frozen error table."""
    _require(type(packet) is dict and set(packet) == {"schema_version", "type", "data"},
             "transport error: envelope shape")
    _require(packet["schema_version"] == TRANSPORT_SCHEMA_VERSION, "unknown schema version")
    _require(packet["type"] == "TransportErrorResponse", "unexpected response type")
    data = packet["data"]
    _require(type(data) is dict, "transport error: data shape")
    _require(type(data.get("error_code")) is str, "error_code must be a string")
    _require(type(data.get("http_status")) is int and not isinstance(data.get("http_status"), bool),
             "http_status must be an integer")
    retry = data.get("retry_after_ms")
    _require(retry is None or (type(retry) is int and not isinstance(retry, bool) and retry >= 1),
             "retry_after_ms must be a positive integer when present")
    return data


# ---------------------------------------------------------------------------
# thin native request mapping mirror (plan §6.1; golden vectors in TG tests)
# ---------------------------------------------------------------------------

def build_client_request(leg, binding, investor_handle, *, strategy_id, order_type):
    """Mirror of N4 build_native_request: pure field/enum/decimal checks."""
    _require(type(strategy_id) is int and strategy_id >= 0, "strategy_id")
    _require(investor_handle is not None, "investor handle required")
    direction = leg["direction"]
    offset = leg["offset"]
    _require(direction in DIRECTION_MAP and offset in OFFSET_MAP, "unmapped direction/offset")
    if offset == "CLOSE_YESTERDAY":
        _require(binding["exchange"] in YD_ONLY_EXCHANGES, "CLOSE_YESTERDAY is SHFE/INE only")
    if offset == "CLOSE_TODAY":
        _require(binding["exchange"] in YD_ONLY_EXCHANGES, "CLOSE_TODAY is SHFE/INE only")
    _require(order_type in ORDER_TYPE_MAP, "unsupported order type")
    volume = leg["volume"]
    _require(type(volume) is int and not isinstance(volume, bool) and volume >= 1,
             "volume must be a positive integer")
    price = _decimal_text(leg["price"], "price", positive=True)
    tick = _decimal_text(leg.get("price_tick", "1"), "price_tick", positive=True)
    _require(price % tick == 0, "price is not on the tick grid")
    request = {
        "StrategyID": strategy_id,
        "Exchange": binding["exchange"],
        "InstrumentID": binding["instrument_id"],
        "Volume": volume,
        "Price": float(price),
        "Direction": DIRECTION_MAP[direction],
        "OrderType": ORDER_TYPE_MAP[order_type],
        "Investor": investor_handle,
        "HedgeFlag": binding["hedge_flag"],
        "Offset": OFFSET_MAP[offset],
        "Market": False,
        "Memo": leg["leg_id"],
    }
    _require(Decimal(str(request["Price"])) == price,
             "float conversion must preserve the exact tick value")
    _require(tuple(request) == NATIVE_REQUEST_KEYS, "request keys must be exact")
    return request


# ---------------------------------------------------------------------------
# PythonGO field protocol (self-contained, same shape as the observer file)
# ---------------------------------------------------------------------------

class Field:
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


class Params(BaseParams):
    exchange: str = Field(DEFAULT_EXCHANGE, title="交易所代码")
    instrument_id: str = Field(DEFAULT_INSTRUMENT_ID, title="合约代码")
    additional_symbols: str = Field("", title="附加观察合约(交易所.合约;分隔)")
    api_url: str = Field("http://127.0.0.1:17778/analyze", title="观察服务地址(兼容保留)")
    execution_api_url: str = Field(DEFAULT_EXECUTION_API_URL, title="执行服务地址")
    batch_size: int = Field(10, title="打包tick数量", ge=1)
    account_tag: str = Field("", title="账户备注(仅本地标签)")
    investor_id: str = Field(DEFAULT_INVESTOR_ID, title="用户选定账户(仅本地)")
    hedge_flag: str = Field(DEFAULT_HEDGE_FLAG, title="套保标志(须已核实白名单)")
    enable_trading: bool = Field(False, title="是否允许执行调用")
    order_volume: int = Field(1, title="下单手数", ge=1)
    order_type: str = Field("GFD", title="订单类型")
    execution_mode: str = Field("disabled", title="执行模式")
    risk_policy_id: str = Field("", title="风险政策引用")
    authorization_id: str = Field("", title="授权引用")
    parameter_confirmation: str = Field("", title="一次性参数确认challenge")
    max_native_sends: int = Field(0, title="真实发送次数上限", ge=0)
    max_native_cancels: int = Field(0, title="真实撤单次数上限", ge=0)

    @property
    def volume(self):
        return self.order_volume


class State(BaseState):
    native_send_attempts: int = Field(0, title="真实发送尝试数", ge=0)
    native_cancel_attempts: int = Field(0, title="真实撤单尝试数", ge=0)
    unresolved_dispatch_count: int = Field(0, title="未决票据数", ge=0)
    outbox_depth: int = Field(0, title="待上传事件数", ge=0)
    execution_status: str = Field("disabled", title="执行状态")
    control_status: str = Field("unknown", title="控制状态")
    snapshot_status: str = Field("awaiting_context", title="Snapshot capture")
    last_receipt_at: str = Field("", title="最近收据时间")
    event_gap: bool = Field(False, title="事件缺口锁存")
    client_verified: bool = Field(False, title="客户端已验证(恒假)")
    ui_idle_refresh: str = Field("UNSUPPORTED", title="无tick刷新")
    # AR1.4: display-only projections of the arming admission machine. They
    # mirror the in-memory effective view and never grant anything.
    arming_state: str = Field("DISARMED", title="本地武装状态")
    enable_trading_effective: bool = Field(False, title="有效执行开关(内存布尔)")


def validate_loopback_url(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("execution url must be a loopback URL")
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        raise ValueError("execution url must be a loopback HTTP URL")
    if parsed.query or parsed.fragment or not parsed.hostname:
        raise ValueError("execution url must not contain query or fragment")
    host = parsed.hostname.casefold().rstrip(".")
    loopback = host == "localhost"
    if not loopback:
        try:
            loopback = ip_address(host).is_loopback
        except ValueError as exc:
            raise ValueError("execution url host must be loopback") from exc
    if not loopback:
        raise ValueError("execution url host must be loopback")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("execution url port is invalid") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("execution url port is invalid")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/execution/v1/poll", "", ""))


def _endpoint(url, path):
    parsed = urlsplit(validate_loopback_url(url))
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


# ---------------------------------------------------------------------------
# NE1-LM-R4 pairing last-mile infrastructure (RULING 01/02/03): deterministic
# default store, persistent instance identity, OS file locks, strict pairing
# record validation and the Windows DACL contract vectors. Everything here is
# pure computation or fails closed; nothing runs at import time.
# ---------------------------------------------------------------------------

STORE_LAYOUT_VERSION = 2
STORE_ROOT_NAME = ".ak_execution"
IDENTITY_SCHEMA_VERSION = "ne1_client_identity_v1"
PAIRING_REQUEST_SCHEMA_VERSION = "ne1_pairing_request_v1"
CLIENT_PAIRING_SCHEMA_VERSION = "ne1_client_pairing_v1"
PAIRING_TOKEN_HEX_LENGTH = 64

IDENTITY_FILE_NAME = "identity.json"
PAIRING_REQUEST_FILE_NAME = "pairing-request.json"
CLIENT_PAIRING_FILE_NAME = "pairing.json"
CLIENT_LOCK_FILE_NAME = "client.lock"
TRANSFER_LOCK_FILE_NAME = "pairing-transfer.lock"
# NE1-ARM-ARMING local slots (AR4.1): exact file names, added verbatim to
# the layout-2 whitelist below; nothing else is accepted.
ARMING_PROPOSAL_FILE_NAME = "arming-proposal.json"
ARMING_PLAN_FILE_NAME = "arming-plan.json"
ARMING_REQUEST_FILE_NAME = "arming-request.json"
ARMING_CHALLENGE_FILE_NAME = "arming-challenge.json"
CLIENT_ARMING_FILE_NAME = "arming.json"
CLIENT_DISARM_FILE_NAME = "disarm.json"
ARMING_STATE_FILE_NAME = "arming-state.json"
ARMING_TRANSFER_LOCK_FILE_NAME = "arming-transfer.lock"

_STORE_JSON_FILE_NAMES = (
    IDENTITY_FILE_NAME, PAIRING_REQUEST_FILE_NAME, CLIENT_PAIRING_FILE_NAME,
    ARMING_PROPOSAL_FILE_NAME, ARMING_PLAN_FILE_NAME, ARMING_REQUEST_FILE_NAME,
    ARMING_CHALLENGE_FILE_NAME, CLIENT_ARMING_FILE_NAME, CLIENT_DISARM_FILE_NAME,
    ARMING_STATE_FILE_NAME)

_STORE_FILE_NAMES = frozenset(
    _STORE_JSON_FILE_NAMES + (CLIENT_LOCK_FILE_NAME, TRANSFER_LOCK_FILE_NAME,
                              ARMING_TRANSFER_LOCK_FILE_NAME, "receipts.db", "outbox.db",
                              "receipts.db-wal", "receipts.db-shm",
                              "outbox.db-wal", "outbox.db-shm"))

_IDENTITY_FIELDS = frozenset({"schema_version", "instance_id", "deployment_key",
                               "strategy_id", "created_at"})
_PAIRING_REQUEST_FIELDS = frozenset({
    "schema_version", "request_id", "active", "created_at",
    "instance_id", "session_epoch", "generation", "process_instance_id",
    "deployment_key", "strategy_id", "adapter_version", "client_file_sha256",
    "store_layout_version", "store_directory",
    "exchange", "instrument_id", "hedge_flag", "execution_api_url", "params_sha256"})
_CLIENT_PAIRING_FIELDS = frozenset({
    "schema_version", "request_id", "request_sha256", "instance_id",
    "session_epoch", "generation", "deployment_key", "strategy_id",
    "params_sha256", "client_file_sha256", "execution_origin",
    "service_instance_id", "service_epoch", "contract_lock_sha256", "token"})

PAIRING_STATE_UNPAIRED = "UNPAIRED"
PAIRING_STATE_WAIT = "WAIT_PAIRING"
PAIRING_STATE_PAIRED = "PAIRED_DISABLED"
PAIRING_STATE_RESTART = "RESTART_REQUIRED"

_MAX_PAIRING_FILE_BYTES = 32 * 1024

# ---------------------------------------------------------------------------
# NE1-ARM-ARMING local slots, schemas and the eight-state admission machine
# (decision NE1-ARB-ARMING-001 RULING 02/04/05, client side). Everything here
# is data or pure validation; nothing runs at import time.
# ---------------------------------------------------------------------------

ARMING_PROPOSAL_SCHEMA_VERSION = "ne1_arming_proposal_v1"
ARMING_PLAN_SCHEMA_VERSION = "ne1_arming_plan_v1"
ARMING_REQUEST_SCHEMA_VERSION = "ne1_arming_request_v1"
ARMING_CHALLENGE_SCHEMA_VERSION = "ne1_arming_challenge_v1"
CLIENT_ARMING_SCHEMA_VERSION = "ne1_client_arming_v1"
CLIENT_DISARM_SCHEMA_VERSION = "ne1_client_disarm_v1"
ARMING_STATE_SCHEMA_VERSION = "ne1_arming_state_v1"

_MAX_ARMING_FILE_BYTES = 64 * 1024
_ARMING_WINDOW_SECONDS = 300.0  # AR4.2: request/challenge activation window cap

# AR2.1: the ONLY fields a target plan may override on the frozen params copy.
ARMING_OVERRIDE_FIELDS = ("execution_mode", "risk_policy_id", "authorization_id",
                          "max_native_sends", "max_native_cancels")

ARM_STATE_DISARMED = "DISARMED"
ARM_STATE_PREPARED_DISABLED = "PREPARED_DISABLED"
ARM_STATE_ARM_READY = "ARM_READY"
ARM_STATE_ARM_PENDING = "ARM_PENDING"
ARM_STATE_ARMED = "ARMED"
ARM_STATE_ARMED_SEND_EXHAUSTED = "ARMED_SEND_EXHAUSTED"
ARM_STATE_REVOKE_PENDING = "REVOKE_PENDING"
ARM_STATE_DISARMED_TERMINAL = "DISARMED_TERMINAL"
ARM_STATE_RESTART_REQUIRED = "RESTART_REQUIRED"

ARM_STATES = frozenset({ARM_STATE_DISARMED, ARM_STATE_PREPARED_DISABLED,
                        ARM_STATE_ARM_READY, ARM_STATE_ARM_PENDING, ARM_STATE_ARMED,
                        ARM_STATE_ARMED_SEND_EXHAUSTED, ARM_STATE_REVOKE_PENDING,
                        ARM_STATE_DISARMED_TERMINAL, ARM_STATE_RESTART_REQUIRED})

# AR2.2 transition table: every legal edge of the local admission machine.
# The two terminal labels share the eighth table row; both are absorbing.
_ARM_TRANSITIONS = {
    ARM_STATE_DISARMED: frozenset({ARM_STATE_PREPARED_DISABLED, ARM_STATE_REVOKE_PENDING,
                                   ARM_STATE_DISARMED_TERMINAL, ARM_STATE_RESTART_REQUIRED}),
    ARM_STATE_PREPARED_DISABLED: frozenset({ARM_STATE_ARM_READY, ARM_STATE_REVOKE_PENDING,
                                            ARM_STATE_DISARMED_TERMINAL,
                                            ARM_STATE_RESTART_REQUIRED}),
    ARM_STATE_ARM_READY: frozenset({ARM_STATE_ARM_PENDING, ARM_STATE_REVOKE_PENDING,
                                    ARM_STATE_DISARMED_TERMINAL, ARM_STATE_RESTART_REQUIRED}),
    ARM_STATE_ARM_PENDING: frozenset({ARM_STATE_ARMED, ARM_STATE_REVOKE_PENDING,
                                      ARM_STATE_DISARMED_TERMINAL, ARM_STATE_RESTART_REQUIRED}),
    ARM_STATE_ARMED: frozenset({ARM_STATE_ARMED_SEND_EXHAUSTED, ARM_STATE_REVOKE_PENDING,
                                ARM_STATE_DISARMED_TERMINAL, ARM_STATE_RESTART_REQUIRED}),
    ARM_STATE_ARMED_SEND_EXHAUSTED: frozenset({ARM_STATE_REVOKE_PENDING,
                                               ARM_STATE_DISARMED_TERMINAL,
                                               ARM_STATE_RESTART_REQUIRED}),
    ARM_STATE_REVOKE_PENDING: frozenset({ARM_STATE_DISARMED_TERMINAL,
                                         ARM_STATE_RESTART_REQUIRED}),
    ARM_STATE_DISARMED_TERMINAL: frozenset(),
    ARM_STATE_RESTART_REQUIRED: frozenset(),
}

_SCOPE_FIELDS = frozenset({
    "pairing_request_id", "pairing_request_sha256", "instance_id", "session_epoch",
    "generation", "process_instance_id", "deployment_key", "strategy_id",
    "client_file_sha256", "adapter_version", "contract_lock_sha256",
    "execution_origin", "pairing_params_sha256"})
_ARMING_PROPOSAL_FIELDS = frozenset({
    "schema_version", "proposal_id", "scope", "binding", "operator_action_id",
    "authorization_id", "permit_id", "execution_mode", "risk_policy_id",
    "max_native_sends", "max_native_cancels", "created_at"})
_ARMING_PLAN_FIELDS = frozenset({
    "schema_version", "plan_id", "proposal_id", "proposal_sha256", "scope",
    "binding", "authorization_id", "permit_id", "target_params_sha256",
    "effective_overrides", "created_at"})
_ARMING_REQUEST_FIELDS = frozenset({
    "schema_version", "request_id", "plan_id", "plan_sha256", "scope", "binding",
    "authorization_id", "permit_id", "target_params_sha256", "service_instance_id",
    "service_epoch", "created_at", "expires_at", "active"})
_ARMING_CHALLENGE_FIELDS = frozenset({
    "schema_version", "request_id", "request_sha256", "challenge", "issued_at",
    "expires_at", "writer_sid"})
_CLIENT_ARMING_FIELDS = frozenset({
    "schema_version", "arm_id", "request_id", "request_sha256", "plan_id", "scope",
    "binding", "target_params_sha256", "authorization_id",
    "authorization_record_sha256", "permit_id", "permit_record_sha256",
    "service_instance_id", "service_epoch", "expected_control_revision",
    "prerequisites_id", "prerequisites_sha256", "challenge", "user_sid",
    "action_source", "explicit_user_action", "confirmed_at", "expires_at"})
_CLIENT_DISARM_FIELDS = frozenset({
    "schema_version", "disarm_id", "scope", "arm_request_id", "arm_id",
    "requested_at", "user_sid", "reason"})
_ARMING_STATE_FIELDS = frozenset({
    "schema_version", "scope", "plan_id", "arm_request_id", "arm_id", "state",
    "consumed", "revoked", "effective_enable_trading", "target_params_sha256",
    "service_instance_id", "service_epoch", "control_revision", "expires_at",
    "local_gate_closed", "service_gate_closed", "unresolved_dispatch_count",
    "count_complete", "updated_at", "reason_code"})

_SID_RE = re.compile(r"S-1-\d+(?:-\d+)+")
_CONTROL_RESPONSE_KINDS = frozenset({"RECORDED", "BLOCKED"})
_CONTROL_PHASES = frozenset({"DISABLED", "ARMED", "REVOKE_PENDING", "DISABLED_CONFIRMED"})

# R2.4 Windows ACL contract vectors (shared with the service/tool sides by
# value, never by import): the only trustees ever allowed on a private
# pairing object, plus the write preflight mask.
_SID_SYSTEM = "S-1-5-18"
_SID_ADMINISTRATORS = "S-1-5-32-544"
_SID_OWNER_RIGHTS = "S-1-3-4"
_ACE_TYPE_ALLOW = 0
_ACL_WRITE_MASK = (0x40000000 | 0x00000002 | 0x00000004 | 0x00000100
                   | 0x00040000 | 0x00080000)
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _hex32(value):
    return type(value) is str and len(value) == 32 and all(ch in _HEX for ch in value)


def _hex64(value):
    return type(value) is str and len(value) == 64 and all(ch in _HEX for ch in value)


def _service_instance_id(value):
    """X01-01 (NE1-ARB-LM-X01-001): the ONLY legal service-instance-id form is
    ``execution-`` followed by exactly 16 lowercase hex characters (26 ASCII
    characters, case-sensitive, leading zeros kept). Not a client identity:
    no bare hex32, no ``execution-<32hex>``, no other prefix, no trim or
    casefold, no compat conversion, no non-string value is ever accepted."""
    return (type(value) is str
            and re.fullmatch(r"execution-[0-9a-f]{16}", value) is not None)


def _reject_code(condition, code):
    if not condition:
        raise ContractError(code)


_LONG_PATH_CACHE = {}


def _windows_long_path(path):
    """Best-effort 8.3 short-name expansion (GetLongPathNameW) WITHOUT
    following links or resolving environment variables. Falls back to the
    input on any failure; used so the published store path matches the
    operator-visible deployment path."""
    if sys.platform != "win32":
        return path
    cached = _LONG_PATH_CACHE.get(path)
    if cached is not None:
        return cached
    expanded = path
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetLongPathNameW.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR,
                                              wintypes.DWORD)
        kernel32.GetLongPathNameW.restype = wintypes.DWORD
        size = kernel32.GetLongPathNameW(path, None, 0)
        if size > 0:
            buffer = ctypes.create_unicode_buffer(size + 1)
            if kernel32.GetLongPathNameW(path, buffer, size + 1) > 0:
                expanded = buffer.value
    except Exception:
        expanded = path
    if len(_LONG_PATH_CACHE) < 1024:
        _LONG_PATH_CACHE[path] = expanded
    return expanded


def _validated_absolute_path(path_text, code):
    """Local absolute path only (R1.1): a drive-letter absolute path with no
    UNC/device prefix, no drive-relative form, no ADS colon; no expandvars or
    expanduser, and no resolve() (links are never followed here)."""
    _reject_code(isinstance(path_text, str) and path_text.strip() != "", code)
    absolute = os.path.abspath(path_text)
    drive, rest = os.path.splitdrive(absolute)
    _reject_code(len(drive) == 2 and drive[1] == ":" and drive[0].isalpha(), code)
    _reject_code(rest[:1] in ("/", "\\"), code)
    _reject_code(":" not in rest, code)
    return absolute


def _is_reparse(st):
    if hasattr(st, "st_file_attributes"):
        return bool(st.st_file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT)
    return stat.S_ISLNK(st.st_mode)


def _assert_regular_file(path, code):
    try:
        st = os.lstat(path)
    except OSError:
        raise ContractError(code) from None
    _reject_code(stat.S_ISREG(st.st_mode) and not _is_reparse(st)
                 and not stat.S_ISLNK(st.st_mode), code)


def _assert_real_directory(path, code):
    try:
        st = os.lstat(path)
    except OSError:
        raise ContractError(code) from None
    _reject_code(stat.S_ISDIR(st.st_mode) and not _is_reparse(st)
                 and not stat.S_ISLNK(st.st_mode), code)


def _reject_linked_ancestors(path, code):
    """Reject any symlink/junction/reparse point among the existing path
    components (checked without following links, before anything opens)."""
    current = path
    while True:
        parent = os.path.dirname(current)
        if parent == current:
            return
        try:
            st = os.lstat(current)
        except OSError:
            current = parent  # a missing component cannot redirect anything
            continue
        _reject_code(not _is_reparse(st) and not stat.S_ISLNK(st.st_mode), code)
        current = parent


def _atomic_replace_write(path, payload):
    """Exclusive temp file in the target directory + fsync + atomic replace.
    The temp name stays recognizable so an interrupted publish never turns a
    layout-2 store into an unknown-layout store."""
    temporary = os.path.join(os.path.dirname(path),
                             "." + os.path.basename(path) + "." + uuid.uuid4().hex)
    handle = open(temporary, "xb")
    try:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    try:
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


class _FileLock:
    """OS-exclusive byte-range lock (exclusive across handles AND processes).
    The lock file's existence proves nothing; only the held OS lock counts,
    and the OS releases it when a process dies (R1.3)."""

    def __init__(self, path):
        self._path = path
        self._fd = None

    def acquire(self):
        fd = None
        try:
            fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o600)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            return False
        self._fd = fd
        return True

    def release(self):
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass


class _WindowsSecurity:
    """Lazy ctypes DACL probe (R2.4). Returns non-secret facts only; bind or
    probe failure raises so callers fail closed instead of downgrading."""

    def __init__(self):
        self._advapi32 = None
        self._kernel32 = None
        self._current_user_sid = None

    def _bind(self):
        if self._advapi32 is not None:
            return
        if os.name != "nt" or sys.platform != "win32":
            raise ContractError("PLATFORM_ACL_UNSUPPORTED")
        import ctypes
        from ctypes import wintypes
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.OpenProcessToken.argtypes = (
            wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
        kernel32.OpenProcessToken.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.LocalFree.argtypes = (wintypes.HANDLE,)
        kernel32.LocalFree.restype = wintypes.HANDLE
        advapi32.GetTokenInformation.argtypes = (
            wintypes.HANDLE, wintypes.INT, wintypes.LPVOID, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD))
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.ConvertSidToStringSidW.argtypes = (
            ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR))
        advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
        advapi32.GetNamedSecurityInfoW.argtypes = (
            wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p))
        advapi32.GetNamedSecurityInfoW.restype = ctypes.c_ulong
        advapi32.SetNamedSecurityInfoW.argtypes = (
            wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
        advapi32.SetNamedSecurityInfoW.restype = ctypes.c_ulong
        advapi32.GetAce.argtypes = (
            ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))
        advapi32.GetAce.restype = wintypes.BOOL
        # AR4.3 write discipline: PROTECTED DACL publication (argument lists
        # only — paths and SIDs, never secrets, no shell, no window).
        advapi32.ConvertStringSidToSidW.argtypes = (
            wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p))
        advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
        advapi32.GetLengthSid.argtypes = (ctypes.c_void_p,)
        advapi32.GetLengthSid.restype = wintypes.DWORD
        advapi32.InitializeAcl.argtypes = (ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD)
        advapi32.InitializeAcl.restype = wintypes.BOOL
        advapi32.AddAccessAllowedAce.argtypes = (ctypes.c_void_p, wintypes.DWORD,
                                                  wintypes.DWORD, ctypes.c_void_p)
        advapi32.AddAccessAllowedAce.restype = wintypes.BOOL
        self._advapi32 = advapi32
        self._kernel32 = kernel32
        self._wintypes = wintypes
        import struct as _struct
        self._struct = _struct

    def _sid_string(self, sid_pointer):
        buffer = self._wintypes.LPWSTR()
        if not self._advapi32.ConvertSidToStringSidW(
                ctypes.c_void_p(sid_pointer), ctypes.byref(buffer)):
            raise ContractError("PLATFORM_ACL_UNSUPPORTED")
        try:
            return buffer.value or ""
        finally:
            self._kernel32.LocalFree(ctypes.cast(buffer, self._wintypes.HANDLE))

    def current_user_sid(self):
        if self._current_user_sid is not None:
            return self._current_user_sid
        self._bind()
        import ctypes
        token = self._wintypes.HANDLE()
        if not self._kernel32.OpenProcessToken(
                self._kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            raise ContractError("PLATFORM_ACL_UNSUPPORTED")
        try:
            size = self._wintypes.DWORD(0)
            self._advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
            buffer = ctypes.create_string_buffer(max(size.value, 1))
            if not self._advapi32.GetTokenInformation(
                    token, 1, buffer, size, ctypes.byref(size)):
                raise ContractError("PLATFORM_ACL_UNSUPPORTED")
            sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
            if not sid_pointer:
                raise ContractError("PLATFORM_ACL_UNSUPPORTED")
            self._current_user_sid = self._sid_string(sid_pointer)
            return self._current_user_sid
        finally:
            self._kernel32.CloseHandle(token)

    def describe(self, path):
        """Owner SID + allow-ACE trustees (+ write grant) for one object."""
        import ctypes
        self._bind()
        owner = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        result = self._advapi32.GetNamedSecurityInfoW(
            str(path), 1, 0x1 | 0x4, ctypes.byref(owner), None,
            ctypes.byref(dacl), None, None)
        if result != 0:
            raise ContractError("PLATFORM_ACL_UNSUPPORTED")
        entry = {"owner_sid": None, "allow_trustees": [], "write_granted": False,
                 "dacl_present": bool(dacl.value)}
        if owner.value:
            entry["owner_sid"] = self._sid_string(owner.value)
        if dacl.value:
            header = (ctypes.c_ubyte * 8).from_address(dacl.value)
            ace_count = self._struct.unpack_from("<H", header, 4)[0]
            for index in range(ace_count):
                ace_pointer = ctypes.c_void_p()
                if not self._advapi32.GetAce(dacl.value, index,
                                             ctypes.byref(ace_pointer)):
                    raise ContractError("PLATFORM_ACL_UNSUPPORTED")
                raw = ctypes.string_at(ace_pointer.value, 8)
                ace_type, mask = raw[0], self._struct.unpack_from("<I", raw, 4)[0]
                if ace_type != _ACE_TYPE_ALLOW:
                    continue  # deny ACEs only restrict access
                entry["allow_trustees"].append(self._sid_string(ace_pointer.value + 8))
                if mask & _ACL_WRITE_MASK:
                    entry["write_granted"] = True
        return entry

    def apply_protected_dacl(self, path):
        """R2.4/AR4.3 publication discipline: replace the object's DACL with a
        PROTECTED one granting only the current Windows user full control.
        Direct argument list, no shell, no hidden window; only the path and
        the caller's own SID ever cross the boundary. Any failure raises so
        callers fail closed instead of downgrading to chmod."""
        import ctypes
        self._bind()
        sid = ctypes.c_void_p()
        if not self._advapi32.ConvertStringSidToSidW(
                self.current_user_sid(), ctypes.byref(sid)):
            raise ContractError("PLATFORM_ACL_UNSUPPORTED")
        try:
            total = 16 + 8 + self._advapi32.GetLengthSid(sid)
            buffer = ctypes.create_string_buffer(total)
            if not self._advapi32.InitializeAcl(buffer, total, 2):  # ACL_REVISION
                raise ContractError("ARMING_ACL_RESTRICTED")
            if not self._advapi32.AddAccessAllowedAce(buffer, 2, 0x1F01FF, sid):
                raise ContractError("ARMING_ACL_RESTRICTED")
            # DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION
            result = self._advapi32.SetNamedSecurityInfoW(
                str(path), 1, 0x4 | 0x80000000, None, None, buffer, None)
            if result != 0:
                raise ContractError("ARMING_ACL_RESTRICTED")
        finally:
            self._kernel32.LocalFree(sid)


_SECURITY = _WindowsSecurity()


def _verify_private_access(path, *, need_write):
    """R2.4 hard precondition for reading the credential slot: DACL present,
    owner is the current user (or Administrators), every allow-ACE trustee
    within {user, OWNER_RIGHTS, SYSTEM, Administrators}. POSIX mode bits are
    never consulted; an unprovable platform refuses."""
    current = _SECURITY.current_user_sid()
    described = _SECURITY.describe(path)
    allowed = {current, _SID_SYSTEM, _SID_ADMINISTRATORS, _SID_OWNER_RIGHTS}
    owner_ok = described["owner_sid"] in (current, _SID_ADMINISTRATORS)
    trustees_ok = set(described["allow_trustees"]) <= allowed
    write_ok = (not need_write) or described["write_granted"]
    record = {"owner_ok": bool(owner_ok), "trustees_ok": bool(trustees_ok),
              "write_granted": bool(write_ok), "dacl_present": described["dacl_present"]}
    _reject_code(record["dacl_present"] and owner_ok and trustees_ok and write_ok,
                 "PAIRING_ACL_RESTRICTED")
    return record


def _strict_execution_origin(url):
    """Normalized origin (scheme/host/explicit port) of the already validated
    execution URL; None when this URL cannot carry a pairing origin (R2.2:
    explicit loopback http, no userinfo/query/fragment)."""
    if not isinstance(url, str) or not url.strip():
        return None
    parsed = urlsplit(url.strip())
    try:
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme != "http" or parsed.username or parsed.password:
        return None
    if parsed.query or parsed.fragment or parsed.hostname != "127.0.0.1":
        return None
    if port is None or not 1 <= port <= 65535:
        return None
    return f"http://127.0.0.1:{port}"


def _validate_identity_record(record):
    _reject_code(type(record) is dict and set(record) == _IDENTITY_FIELDS,
                 "STORE_IDENTITY_INVALID")
    _reject_code(record.get("schema_version") == IDENTITY_SCHEMA_VERSION,
                 "STORE_IDENTITY_INVALID")
    _reject_code(_hex32(record.get("instance_id")), "STORE_IDENTITY_INVALID")
    _reject_code(_hex64(record.get("deployment_key")), "STORE_IDENTITY_INVALID")
    strategy = record.get("strategy_id")
    _reject_code(type(strategy) is int and not isinstance(strategy, bool)
                 and strategy >= 0, "STORE_IDENTITY_INVALID")
    _reject_code(type(record.get("created_at")) is str and record["created_at"].strip() != "",
                 "STORE_IDENTITY_INVALID")
    return record


def _validate_pairing_request_record(record):
    _reject_code(type(record) is dict and set(record) == _PAIRING_REQUEST_FIELDS,
                 "PAIRING_REQUEST_INVALID")
    _reject_code(record.get("schema_version") == PAIRING_REQUEST_SCHEMA_VERSION,
                 "PAIRING_REQUEST_INVALID")
    _reject_code(type(record.get("active")) is bool and record["active"] is True,
                 "PAIRING_REQUEST_INVALID")
    for name in ("request_id", "instance_id", "session_epoch", "generation",
                 "process_instance_id"):
        _reject_code(_hex32(record.get(name)), "PAIRING_REQUEST_INVALID")
    for name in ("deployment_key", "client_file_sha256", "params_sha256"):
        _reject_code(_hex64(record.get(name)), "PAIRING_REQUEST_INVALID")
    strategy = record.get("strategy_id")
    _reject_code(type(strategy) is int and not isinstance(strategy, bool)
                 and strategy >= 0, "PAIRING_REQUEST_INVALID")
    _reject_code(type(record.get("adapter_version")) is str
                 and record["adapter_version"].strip() != "", "PAIRING_REQUEST_INVALID")
    _reject_code(type(record.get("store_layout_version")) is int
                 and not isinstance(record.get("store_layout_version"), bool)
                 and record["store_layout_version"] == STORE_LAYOUT_VERSION,
                 "PAIRING_REQUEST_INVALID")
    _utc_text(record.get("created_at"), "created_at")
    _reject_code(type(record.get("store_directory")) is str
                 and record["store_directory"].strip() != "", "PAIRING_REQUEST_INVALID")
    for name in ("exchange", "instrument_id", "hedge_flag"):
        _reject_code(type(record.get(name)) is str
                     and _IDENTIFIER_RE.fullmatch(record[name]) is not None,
                     "PAIRING_REQUEST_INVALID")
    _reject_code(type(record.get("execution_api_url")) is str
                 and record["execution_api_url"].strip() != "", "PAIRING_REQUEST_INVALID")
    return record


def _validate_client_pairing_record(record):
    _reject_code(type(record) is dict and set(record) == _CLIENT_PAIRING_FIELDS,
                 "PAIRING_FORMAT_INVALID")
    _reject_code(record.get("schema_version") == CLIENT_PAIRING_SCHEMA_VERSION,
                 "PAIRING_FORMAT_INVALID")
    for name in ("request_id", "instance_id", "session_epoch", "generation"):
        _reject_code(_hex32(record.get(name)), "PAIRING_FORMAT_INVALID")
    # X01-01 (NE1-ARB-LM-X01-001): service_instance_id is a service-process
    # label, NOT a client identity — it left the hex32 loop above and is
    # checked by its own exact rule; failure stays PAIRING_FORMAT_INVALID.
    _reject_code(_service_instance_id(record.get("service_instance_id")),
                 "PAIRING_FORMAT_INVALID")
    for name in ("request_sha256", "deployment_key", "params_sha256",
                 "client_file_sha256", "contract_lock_sha256", "token"):
        _reject_code(_hex64(record.get(name)), "PAIRING_FORMAT_INVALID")
    strategy = record.get("strategy_id")
    _reject_code(type(strategy) is int and not isinstance(strategy, bool)
                 and strategy >= 0, "PAIRING_FORMAT_INVALID")
    epoch = record.get("service_epoch")
    _reject_code(type(epoch) is int and not isinstance(epoch, bool) and epoch >= 1,
                 "PAIRING_FORMAT_INVALID")
    _reject_code(type(record.get("execution_origin")) is str
                 and record["execution_origin"].strip() != "", "PAIRING_FORMAT_INVALID")
    return record


# ---------------------------------------------------------------------------
# NE1-ARM-ARMING strict local codecs (AR4.1/AR4.2). Exact field sets, strict
# UTF-8 objects, no duplicate keys, strict bools, integers that reject bool,
# UTC ISO times, 64-hex hashes, 32-hex random ids; unknown schemas reject.
# ---------------------------------------------------------------------------

def _strict_sid(value):
    return type(value) is str and _SID_RE.fullmatch(value) is not None


def _strict_int(value, minimum=0):
    return type(value) is int and minimum <= value <= 2 ** 63 - 1


def _validate_scope_record(record, code):
    _reject_code(type(record) is dict and set(record) == _SCOPE_FIELDS, code)
    for name in ("pairing_request_id", "instance_id", "session_epoch", "generation",
                 "process_instance_id"):
        _reject_code(_hex32(record.get(name)), code)
    for name in ("pairing_request_sha256", "deployment_key", "client_file_sha256",
                 "contract_lock_sha256", "pairing_params_sha256"):
        _reject_code(_hex64(record.get(name)), code)
    _reject_code(_strict_int(record.get("strategy_id")), code)
    _reject_code(type(record.get("adapter_version")) is str
                 and record["adapter_version"].strip() != "", code)
    _reject_code(type(record.get("execution_origin")) is str
                 and _strict_execution_origin(record["execution_origin"]) is not None,
                 code)
    return record


def _validate_arming_proposal_record(record):
    code = "ARMING_PROPOSAL_INVALID"
    _reject_code(type(record) is dict and set(record) == _ARMING_PROPOSAL_FIELDS, code)
    _reject_code(record.get("schema_version") == ARMING_PROPOSAL_SCHEMA_VERSION, code)
    _reject_code(_hex32(record.get("proposal_id")), code)
    _validate_scope_record(record.get("scope"), code)
    _validate_binding(record.get("binding"))
    for name in ("operator_action_id", "authorization_id", "permit_id", "risk_policy_id"):
        _reject_code(type(record.get(name)) is str
                     and _IDENTIFIER_RE.fullmatch(record[name]) is not None, code)
    _reject_code(record.get("execution_mode") == "native", code)
    _reject_code(record.get("max_native_sends") == 1
                 and _strict_int(record.get("max_native_sends")), code)
    _reject_code(record.get("max_native_cancels") in (0, 1)
                 and _strict_int(record.get("max_native_cancels")), code)
    _utc_text(record.get("created_at"), "created_at")
    return record


def _validate_effective_overrides(value):
    _reject_code(type(value) is dict and set(value) == set(ARMING_OVERRIDE_FIELDS),
                 "ARMING_PLAN_INVALID")
    _reject_code(value.get("execution_mode") == "native", "ARMING_PLAN_INVALID")
    _reject_code(type(value.get("risk_policy_id")) is str
                 and _IDENTIFIER_RE.fullmatch(value["risk_policy_id"]) is not None,
                 "ARMING_PLAN_INVALID")
    _reject_code(type(value.get("authorization_id")) is str
                 and _IDENTIFIER_RE.fullmatch(value["authorization_id"]) is not None,
                 "ARMING_PLAN_INVALID")
    _reject_code(value.get("max_native_sends") == 1
                 and _strict_int(value.get("max_native_sends")), "ARMING_PLAN_INVALID")
    _reject_code(value.get("max_native_cancels") in (0, 1)
                 and _strict_int(value.get("max_native_cancels")), "ARMING_PLAN_INVALID")
    return value


def _validate_arming_plan_record(record):
    code = "ARMING_PLAN_INVALID"
    _reject_code(type(record) is dict and set(record) == _ARMING_PLAN_FIELDS, code)
    _reject_code(record.get("schema_version") == ARMING_PLAN_SCHEMA_VERSION, code)
    _reject_code(_hex64(record.get("plan_id")), code)  # hash-style plan identifier
    _reject_code(_hex32(record.get("proposal_id")), code)
    _reject_code(_hex64(record.get("proposal_sha256")), code)
    _reject_code(_hex64(record.get("target_params_sha256")), code)
    _validate_scope_record(record.get("scope"), code)
    _validate_binding(record.get("binding"))
    for name in ("authorization_id", "permit_id"):
        _reject_code(type(record.get(name)) is str
                     and _IDENTIFIER_RE.fullmatch(record[name]) is not None, code)
    _validate_effective_overrides(record.get("effective_overrides"))
    _utc_text(record.get("created_at"), "created_at")
    return record


def _validate_arming_request_record(record):
    code = "ARMING_REQUEST_INVALID"
    _reject_code(type(record) is dict and set(record) == _ARMING_REQUEST_FIELDS, code)
    _reject_code(record.get("schema_version") == ARMING_REQUEST_SCHEMA_VERSION, code)
    _reject_code(_hex32(record.get("request_id")), code)
    _reject_code(_hex64(record.get("plan_id")), code)
    _reject_code(_hex64(record.get("plan_sha256")), code)
    _reject_code(_hex64(record.get("target_params_sha256")), code)
    _validate_scope_record(record.get("scope"), code)
    _validate_binding(record.get("binding"))
    for name in ("authorization_id", "permit_id"):
        _reject_code(type(record.get(name)) is str
                     and _IDENTIFIER_RE.fullmatch(record[name]) is not None, code)
    _reject_code(_service_instance_id(record.get("service_instance_id")), code)
    _reject_code(_strict_int(record.get("service_epoch"), 1), code)
    created = _utc_text(record.get("created_at"), "created_at")
    expires = _utc_text(record.get("expires_at"), "expires_at")
    _reject_code(expires > created, code)
    _reject_code((expires - created).total_seconds() <= _ARMING_WINDOW_SECONDS, code)
    _reject_code(type(record.get("active")) is bool, code)
    return record


def _validate_arming_challenge_record(record):
    code = "ARMING_CHALLENGE_INVALID"
    _reject_code(type(record) is dict and set(record) == _ARMING_CHALLENGE_FIELDS, code)
    _reject_code(record.get("schema_version") == ARMING_CHALLENGE_SCHEMA_VERSION, code)
    _reject_code(_hex32(record.get("request_id")), code)
    _reject_code(_hex64(record.get("request_sha256")), code)
    _reject_code(_hex64(record.get("challenge")), code)
    issued = _utc_text(record.get("issued_at"), "issued_at")
    expires = _utc_text(record.get("expires_at"), "expires_at")
    _reject_code(expires > issued, code)
    _reject_code((expires - issued).total_seconds() <= _ARMING_WINDOW_SECONDS, code)
    _reject_code(_strict_sid(record.get("writer_sid")), code)
    return record


def _validate_client_arming_record(record):
    code = "ARMING_CREDENTIAL_INVALID"
    _reject_code(type(record) is dict and set(record) == _CLIENT_ARMING_FIELDS, code)
    _reject_code(record.get("schema_version") == CLIENT_ARMING_SCHEMA_VERSION, code)
    _reject_code(_hex32(record.get("arm_id")), code)
    _reject_code(_hex32(record.get("request_id")), code)
    for name in ("request_sha256", "plan_id", "target_params_sha256",
                 "authorization_record_sha256", "permit_record_sha256",
                 "prerequisites_sha256", "challenge"):
        _reject_code(_hex64(record.get(name)), code)
    _validate_scope_record(record.get("scope"), code)
    _validate_binding(record.get("binding"))
    for name in ("authorization_id", "permit_id", "prerequisites_id"):
        _reject_code(type(record.get(name)) is str
                     and _IDENTIFIER_RE.fullmatch(record[name]) is not None, code)
    _reject_code(_service_instance_id(record.get("service_instance_id")), code)
    _reject_code(_strict_int(record.get("service_epoch"), 1), code)
    _reject_code(_strict_int(record.get("expected_control_revision"), 1), code)
    _reject_code(_strict_sid(record.get("user_sid")), code)
    _reject_code(record.get("action_source") == "USER_LOCAL_CONSOLE", code)
    _reject_code(record.get("explicit_user_action") is True, code)
    confirmed = _utc_text(record.get("confirmed_at"), "confirmed_at")
    expires = _utc_text(record.get("expires_at"), "expires_at")
    _reject_code(expires > confirmed, code)
    return record


def _validate_client_disarm_record(record):
    code = "ARMING_DISARM_INVALID"
    _reject_code(type(record) is dict and set(record) == _CLIENT_DISARM_FIELDS, code)
    _reject_code(record.get("schema_version") == CLIENT_DISARM_SCHEMA_VERSION, code)
    _reject_code(_hex32(record.get("disarm_id")), code)
    _validate_scope_record(record.get("scope"), code)
    _reject_code(record.get("arm_request_id") is None or _hex32(record["arm_request_id"]),
                 code)
    _reject_code(record.get("arm_id") is None or _hex32(record["arm_id"]), code)
    _utc_text(record.get("requested_at"), "requested_at")
    _reject_code(_strict_sid(record.get("user_sid")), code)
    _reject_code(record.get("reason") == "USER_DISARM", code)
    return record


def _validate_arming_state_record(record):
    code = "ARMING_STATE_INVALID"
    _reject_code(type(record) is dict and set(record) == _ARMING_STATE_FIELDS, code)
    _reject_code(record.get("schema_version") == ARMING_STATE_SCHEMA_VERSION, code)
    _validate_scope_record(record.get("scope"), code)
    _reject_code(record.get("plan_id") is None or _hex64(record["plan_id"]), code)
    _reject_code(record.get("arm_request_id") is None or _hex32(record["arm_request_id"]),
                 code)
    _reject_code(record.get("arm_id") is None or _hex32(record["arm_id"]), code)
    _reject_code(record.get("state") in ARM_STATES, code)
    for name in ("consumed", "revoked", "effective_enable_trading", "local_gate_closed",
                 "service_gate_closed", "count_complete"):
        _reject_code(type(record.get(name)) is bool, code)
    _reject_code(record.get("target_params_sha256") is None
                 or _hex64(record["target_params_sha256"]), code)
    _reject_code(record.get("service_instance_id") is None
                 or _service_instance_id(record["service_instance_id"]), code)
    _reject_code(record.get("service_epoch") is None
                 or _strict_int(record["service_epoch"], 1), code)
    _reject_code(record.get("control_revision") is None
                 or _strict_int(record["control_revision"], 1), code)
    _reject_code(record.get("expires_at") is None
                 or type(record["expires_at"]) is str, code)
    _reject_code(record.get("unresolved_dispatch_count") is None
                 or _strict_int(record["unresolved_dispatch_count"]), code)
    _utc_text(record.get("updated_at"), "updated_at")
    _reject_code(record.get("reason_code") is None
                 or (type(record["reason_code"]) is str
                     and record["reason_code"].strip() != ""), code)
    return record


def validate_control_response(packet):
    """Strict-enough decode mirror of ControlResponse for the PARAMS_CONFIRMED
    ACK path: only the fields the client acts on are typed-checked (same
    precedent as validate_transport_error_response)."""
    _require(type(packet) is dict and set(packet) == {"schema_version", "type", "data"},
             "control response: envelope shape")
    _require(packet["schema_version"] == TRANSPORT_SCHEMA_VERSION, "unknown schema version")
    _require(packet["type"] == "ControlResponse", "unexpected response type")
    data = packet["data"]
    _require(type(data) is dict, "control response: data shape")
    _require(data.get("kind") in _CONTROL_RESPONSE_KINDS, "control response: unknown kind")
    _require(data.get("report_kind") in CONTROL_KINDS, "control response: unknown report kind")
    confirmation_id = data.get("confirmation_id")
    _require(confirmation_id is None
             or (type(confirmation_id) is str
                 and _IDENTIFIER_RE.fullmatch(confirmation_id) is not None),
             "control response: confirmation_id")
    _require(data.get("control_phase") in _CONTROL_PHASES,
             "control response: unknown control phase")
    _require(type(data.get("blockers", [])) is list, "control response: blockers")
    for item in data.get("blockers", ()):
        _require(type(item) is str, "control response: blocker")
    _require(type(data.get("readiness")) is dict, "control response: readiness")
    if data.get("service_epoch") is not None:
        _require(_strict_int(data["service_epoch"], 1), "control response: service_epoch")
    return data


class ArmContext:
    """AR1.4: the non-serializable in-memory admission context. It carries the
    effective mode, authorization/permit references, budget ceilings and the
    validity window of ONE armed round; ``enable_trading_effective`` is the
    boolean this context yields only after full re-validation. It can never
    be pickled, dumped or restored from any file."""

    __slots__ = ("risk_policy_id", "authorization_id", "permit_id", "max_native_sends",
                 "max_native_cancels", "target_params_sha256", "pairing_params_sha256",
                 "binding", "service_instance_id", "service_epoch", "control_revision",
                 "arm_id", "request_id", "plan_id", "expires_at", "monotonic_deadline",
                 "installed_at")

    def __init__(self, **fields):
        for name in self.__slots__:
            setattr(self, name, fields[name])

    def __reduce__(self):
        raise TypeError("ArmContext is not serializable")

    def expired(self, now, monotonic_now):
        return (now >= self.expires_at) or (monotonic_now >= self.monotonic_deadline)


def _atomic_protected_write(path, payload):
    """AR4.3 publication: exclusive temp file, PROTECTED user-only DACL
    applied BEFORE any payload bytes are written, then write + fsync + atomic
    replace, then a DACL post-verification. Used for every client-published
    arming slot (private challenge included); any failure raises so callers
    fail closed."""
    temporary = os.path.join(os.path.dirname(path),
                             "." + os.path.basename(path) + "." + uuid.uuid4().hex)
    handle = open(temporary, "xb")
    try:
        _SECURITY.apply_protected_dacl(temporary)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    except BaseException:
        handle.close()
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    handle.close()
    try:
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    _verify_private_access(path, need_write=True)


def _protect_created_directories(directory):
    """AR4.3: apply the protected user-only DACL to every directory level
    this creation actually produced (win32 only; any failure fails closed so
    a private slot is never published under an unprovable parent)."""
    if sys.platform != "win32":
        return
    created = []
    probe = directory
    while not os.path.exists(probe):
        created.append(probe)
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    for path in reversed(created):
        _SECURITY.apply_protected_dacl(path)
        _verify_private_access(path, need_write=True)


def _read_arming_slot(path, code):
    """Read + strictly decode one arming slot (bounded, exact UTF-8)."""
    _assert_regular_file(path, code)
    try:
        raw = open(path, "rb").read(_MAX_ARMING_FILE_BYTES + 1)
    except OSError:
        raise ContractError(code) from None
    _reject_code(len(raw) <= _MAX_ARMING_FILE_BYTES, code)
    try:
        record = strict_loads(raw.decode("utf-8", errors="strict"),
                              limit=_MAX_ARMING_FILE_BYTES)
    except (ContractError, UnicodeDecodeError, json.JSONDecodeError):
        raise ContractError(code) from None
    _reject_code(type(record) is dict, code)
    return record, raw


# ---------------------------------------------------------------------------
# local persistence: receipt chain + raw callback outbox (synchronous=FULL)
# ---------------------------------------------------------------------------

class _LocalStores:
    def __init__(self, directory):
        self.path = str(directory)
        os.makedirs(self.path, exist_ok=True)
        self._lock = threading.Lock()
        self.receipts = sqlite3.connect(os.path.join(self.path, "receipts.db"),
                                        timeout=SQLITE_BUSY_TIMEOUT_MS / 1000.0, check_same_thread=False)
        self.outbox = sqlite3.connect(os.path.join(self.path, "outbox.db"),
                                      timeout=SQLITE_BUSY_TIMEOUT_MS / 1000.0, check_same_thread=False)
        for handle in (self.receipts, self.outbox):
            handle.execute("PRAGMA journal_mode=WAL")
            handle.execute("PRAGMA synchronous=FULL")
            handle.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        with self.receipts:
            self.receipts.execute(
                "CREATE TABLE IF NOT EXISTS receipts ("
                "receipt_id TEXT PRIMARY KEY, command_id TEXT, attempt_id TEXT, stage TEXT,"
                "generation TEXT, local_sequence INTEGER, recorded_at TEXT, body_hash TEXT,"
                "previous_receipt_sha256 TEXT, payload TEXT)")
        with self.outbox:
            self.outbox.execute(
                "CREATE TABLE IF NOT EXISTS events ("
                "event_id TEXT PRIMARY KEY, kind TEXT, captured_at TEXT, body TEXT, acked INTEGER DEFAULT 0)")
            self.outbox.execute(
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        self._receipt_count = self.receipts.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
        self._event_bytes = self.outbox.execute(
            "SELECT COALESCE(SUM(LENGTH(body)),0) FROM events WHERE acked=0").fetchone()[0]

    def read_meta(self, key):
        with self._lock:
            row = self.outbox.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def write_meta(self, key, value):
        with self._lock:
            with self.outbox:
                self.outbox.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(value)))

    def next_event_sequence(self, count=1):
        """Atomically allocate the next contiguous event sequence numbers."""
        with self._lock:
            with self.outbox:
                row = self.outbox.execute(
                    "SELECT value FROM meta WHERE key='event_sequence'").fetchone()
                current = int(row[0]) if row else 0
                self.outbox.execute(
                    "INSERT OR REPLACE INTO meta VALUES ('event_sequence', ?)",
                    (str(current + count),))
        return list(range(current + 1, current + count + 1))

    def append_receipt(self, record):
        body = canonical_dumps(record)
        body_hash = sha256_hex(body)
        with self._lock:
            previous = self.receipts.execute(
                "SELECT body_hash FROM receipts ORDER BY local_sequence DESC LIMIT 1").fetchone()
            with self.receipts:
                self.receipts.execute(
                    "INSERT INTO receipts VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (record["receipt_id"], record["command_id"], record["attempt_id"],
                     record["stage"], record["generation"], record["local_sequence"],
                     record["recorded_at"], body_hash,
                     previous[0] if previous else None, body))
            self._receipt_count += 1
            return previous[0] if previous else None, body_hash

    def receipt_for_command(self, command_id):
        with self._lock:
            rows = self.receipts.execute(
                "SELECT stage, attempt_id FROM receipts WHERE command_id=? ORDER BY local_sequence",
                (command_id,)).fetchall()
        return rows

    def append_event(self, body_text):
        parsed = strict_loads(body_text, limit=MAX_REQUEST_BYTES)
        event = parsed["data"] if parsed.get("type") == "RawCallbackEnvelope" else parsed
        event_id = event["event_id"]
        encoded = len(body_text.encode("utf-8"))
        with self._lock:
            with self.outbox:
                self.outbox.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?,0)",
                                    (event_id, event["kind"], event["captured_at"], body_text))
            self._event_bytes += encoded

    def pending_events(self, limit):
        with self._lock:
            rows = self.outbox.execute(
                "SELECT event_id, body FROM events WHERE acked=0 ORDER BY rowid LIMIT ?",
                (limit,)).fetchall()
        return rows

    def ack_events(self, watermark_event_ids):
        with self._lock:
            for event_id in watermark_event_ids:
                row = self.outbox.execute("SELECT body FROM events WHERE event_id=? AND acked=0",
                                          (event_id,)).fetchone()
                if row:
                    self._event_bytes -= len(row[0].encode("utf-8"))
                with self.outbox:
                    self.outbox.execute("UPDATE events SET acked=1 WHERE event_id=?", (event_id,))

    def unacked_counts(self):
        with self._lock:
            row = self.outbox.execute(
                "SELECT COUNT(*), COALESCE(SUM(LENGTH(body)),0) FROM events WHERE acked=0").fetchone()
        return row[0], row[1]

    def close(self):
        with self._lock:
            try:
                self.receipts.close()
            finally:
                self.outbox.close()


# ---------------------------------------------------------------------------
# the execution strategy
# ---------------------------------------------------------------------------

class _Lane:
    __slots__ = ("wake", "busy", "job", "retired", "thread", "kind")

    def __init__(self, kind):
        self.wake = threading.Event()
        self.busy = False
        self.job = None
        self.retired = False
        self.thread = None
        self.kind = kind


class AKExecutionBridge:
    """Disabled-by-default NE1 execution strategy (plan §5.1 thread model).

    Native calls happen only on the legal framework callback thread, after the
    fixed gate order, at most one per callback, behind a CONSUMED receipt.
    """

    def __init__(self, *, native=None, transport=None, store_directory=None,
                 http_timeout=MAX_HTTP_TIMEOUT_SECONDS,
                 pairing_token=None):
        self._native_recorder = native  # stored only; attributes never resolved here
        # OV-F02/R1.2: the pairing token is NEVER a production construction
        # input. A non-empty value is only the INJECTED test-seam candidate;
        # the production token source is exclusively the RULING 02 protected
        # pairing.json slot consumed via _try_activate_pairing. It never
        # enters logs, params, or ordinary store meta.
        self._injected_pairing_token = (pairing_token
                                        if isinstance(pairing_token, str) and pairing_token.strip()
                                        else None)
        self._pairing_token = None  # only the ACTIVE token: injected fixture or activated
        self._transport = transport or self._default_transport
        if isinstance(http_timeout, bool) or not isinstance(http_timeout, (int, float)) \
                or not 0 < float(http_timeout) <= MAX_HTTP_TIMEOUT_SECONDS:
            raise ValueError("http_timeout must be within (0, 3] seconds")
        self._http_timeout = float(http_timeout)
        self.params_map = Params()
        self.state_map = State()
        # R1.1: the public assignment surface with an internal assigned flag;
        # the constructor default 0 is NOT a framework assignment (legal 0 is).
        self._strategy_id = 0
        self._strategy_id_assigned = False
        self.strategy_name = ""
        self.trading = False
        # R1.2: construction only saves parameters and builds in-memory
        # objects and locks. The instance identity is "not yet obtained from
        # the store" (None) until the first valid on_start; session and
        # generation exist only inside a started life.
        self._instance_id = None
        self._process_instance_id = uuid.uuid4().hex
        self._session_epoch = None
        self._generation = None
        self._sequence = 0
        self._service_epoch = None
        self._service_epoch_lock = threading.Lock()
        self._callback_thread_id = None
        self._admission = threading.RLock()
        self._state_lock = threading.RLock()
        self._running = False
        self._stop_event = threading.Event()
        self._lanes = {}
        self._dispatch_slot = None            # single received DispatchCommand
        self._dispatch_slot_epoch = None
        self._offer_deadlines: OrderedDict[str, str] = OrderedDict()
        self._terminal_no_call: set[str] = set()
        self._consumed_commands = OrderedDict()  # command_id -> True (bounded)
        self._notify_queue = deque()          # bounded notification queue
        self._result_uploads = deque()        # bounded receipt upload queue
        self._stores = None
        self._store_directory = store_directory
        self._store_path = None               # resolved store directory (started life)
        self._client_lock = None              # OS exclusive client.lock handle
        self._deployment_key = None
        self._client_file_sha256 = None
        self._pairing_request_record = None
        self._pairing_state = PAIRING_STATE_UNPAIRED
        self._pairing_injected = False
        self._activation_candidate = None
        self._last_pairing_reason = None
        self._last_refusal = None
        self._stop_incomplete = False
        self._params_sha256 = None
        self._confirmed_connection = {"exchange": "", "instrument_id": "",
                                      "execution_api_url": DEFAULT_EXECUTION_API_URL}
        self._latest_capture = {"quote_at": None, "account_at": None,
                                "position_at": None, "contract_at": None,
                                "capture_id": None}
        self._snapshot_context = None
        self._snapshot_binding_pin = None
        self._snapshot_facts = {}
        self._snapshot_frozen = None
        self._snapshot_queries = {}
        self._snapshot_subscription = None
        self._snapshot_errors = {}
        self._thread_evidence_id = None
        self._event_gap_latched = False
        # DTTL-02/03: capture_incomplete-class latch — a dispatch-window
        # contract violation (command outliving its offer, or a mutated offer
        # deadline) quarantines consumption until manual re-arm.
        self._window_conflict_latched = False
        self._memory_receipt_sequence = 0
        self._current_event_stream_id = None
        self._memory_event_sequence = 0
        self._last_link_at = None
        # POLL_TOO_FAST (429) backoff: no poll is sent before this monotonic
        # deadline; set from the frozen TransportErrorResponse retry_after_ms.
        self._poll_backoff_until = 0.0
        # NE1-ARM-ARMING local admission machine (AR1.4/AR2.2). All of this is
        # per-life in-memory state; NOTHING here is ever restored from a file.
        self._arm_state = ARM_STATE_DISARMED
        self._arm_plan = None            # frozen plan context (the single per-session plan)
        self._arm_request = None         # frozen one-time request/challenge context
        self._arm_confirmation = None    # frozen PARAMS_CONFIRMED payload (same logical retransmit)
        self._arm_context = None         # installed ArmContext or None
        self._effective_params_sha256 = None  # pairing hash until the atomic ARMED switch
        self._arming_revoked = False
        self._arming_ack_received = False
        self._arming_consumed = False
        self._arming_service_gate_closed = None
        self._arming_deadline_monotonic = None
        self._last_arming_reason = None
        self._service_instance_id = None
        self.counters = {"native_send_attempts": 0, "native_cancel_attempts": 0,
                         "local_order_ids": [], "unresolved_dispatch_count": 0,
                         "dispatch_received": 0, "dispatch_consumed": 0,
                         "dispatch_backpressured": 0, "poll_requests": 0,
                         "event_batches_sent": 0, "native_call_blocked": 0,
                         "dispatch_window_conflicts": 0,
                         "terminal_no_call": 0, "poll_backoffs": 0,
                         "pairing_activations": 0, "params_confirmations_sent": 0}

    # -- public framework assignment surface (R1.1) ---------------------------

    @property
    def strategy_id(self):
        return self._strategy_id

    @strategy_id.setter
    def strategy_id(self, value):
        if self._running and value != self._strategy_id:
            # R1.4: an actual strategy_id change voids the current pairing
            # request and demands a fresh start with a new description.
            self._deactivate_for_restart("strategy_id_changed")
        self._strategy_id = value
        self._strategy_id_assigned = True

    # -- legacy disabled-compat surface (N4 E21; lazy repo types) ----------

    @property
    def mode(self):
        try:
            from ak_trading.infiny_execution.contracts import ExecutionMode
            return ExecutionMode.DISABLED
        except Exception:
            return "disabled"

    @property
    def submit_enabled(self):
        return False

    @property
    def cancel_owned_enabled(self):
        return False

    @property
    def capabilities(self):
        return ("engineering:offline", "platform:PARTIAL_UNVERIFIED", "broker:NOT_RUN",
                "full_account_reconciliation:unsupported")

    def _disabled_error(self):
        try:
            from ak_trading.infiny_execution.native import NativeExecutionError
            return NativeExecutionError("MODE_DISABLED: client bypass entry")
        except Exception:
            return ContractError("MODE_DISABLED: client bypass entry")

    def send_order(self, request):
        raise self._disabled_error()

    def cancel_order(self, order_id):
        raise self._disabled_error()

    def submit_once(self, intent_id, leg_id, plan_id, context):
        raise self._disabled_error()

    def request_cancel(self, cancel_id, intent_id, leg_id, context):
        raise self._disabled_error()

    # -- framework protocol -------------------------------------------------

    def _assert_callback_thread(self):
        if self._callback_thread_id is None or threading.get_ident() != self._callback_thread_id:
            raise RuntimeError("legal_framework_callback_thread_required")

    def get_params(self):
        return [{"name": key, "value": str(getattr(self.params_map, key)), "cn_name": field.title}
                for key, field in self.params_map.fields().items()]

    def set_params(self, data):
        self._bind_callback_thread("set_params")
        normalized = dict(data)
        if "volume" in normalized:
            if "order_volume" in normalized and normalized["volume"] != normalized["order_volume"]:
                raise ValueError("volume alias disagrees with order_volume")
            normalized["order_volume"] = normalized.pop("volume")
        candidate = self.params_map.model_dump()
        for key in ("exchange", "instrument_id", "execution_api_url"):
            value = normalized.get(key)
            if value is None or (isinstance(value, str) and not value.strip()):
                candidate[key] = self._confirmed_connection[key]
            elif key == "execution_api_url":
                candidate[key] = validate_loopback_url(value)
            else:
                candidate[key] = value
        for key, value in normalized.items():
            if key in ("exchange", "instrument_id", "execution_api_url"):
                continue
            if key not in self.params_map.fields():
                raise ValueError("unknown strategy parameter")
            candidate[key] = value
        if candidate["enable_trading"]:
            candidate["enable_trading"] = False  # never restorable via saved params
        if candidate["execution_mode"] != "disabled":
            candidate["execution_mode"] = "disabled"  # mode string grants nothing
        # AR1.4: parameter_confirmation no longer carries secrets or any
        # activation function; every legal application clears it.
        candidate["parameter_confirmation"] = ""
        validated = Params()
        for key, value in candidate.items():
            setattr(validated, key, value)
        # runtime identity change closes the gate and voids confirmations
        identity_changed = self._running and (
            validated.exchange != self.params_map.exchange
            or validated.instrument_id != self.params_map.instrument_id
            or validated.execution_api_url != self.params_map.execution_api_url)
        if identity_changed:
            self._close_gate("params_identity_changed")
        self.params_map = validated
        if self._running and self._arm_state not in (ARM_STATE_DISARMED_TERMINAL,
                                                     ARM_STATE_RESTART_REQUIRED):
            # AR2.1: a successful set_params application voids every arming
            # candidate, confirmation and active ArmContext of this life;
            # re-arming needs a new plan and a new user action.
            self._revoke_arming("params_set")
        self._confirmed_connection = {
            "exchange": validated.exchange, "instrument_id": validated.instrument_id,
            "execution_api_url": validated.execution_api_url}
        previous_hash = self._params_sha256
        self._params_sha256 = self._compute_params_hash()
        if previous_hash != self._params_sha256:
            self._retire_snapshot()
        if (self._running and self._pairing_state == PAIRING_STATE_WAIT
                and previous_hash is not None and previous_hash != self._params_sha256):
            # R1.4: any successful change of a params_sha256 field during the
            # pairing wait voids the current request; the old description's
            # parameter hash is never continued. A fresh on_start republishes.
            self._deactivate_for_restart("params_changed")
        self._record_thread_evidence("SET_PARAMS")
        self._pump_once()
        _native.updateParam(StrategyID=self.strategy_id, Data=self.get_params())
        self.update_status_bar()

    def _compute_params_hash(self):
        fields = ("exchange", "instrument_id", "execution_api_url", "order_volume",
                  "order_type", "risk_policy_id", "authorization_id",
                  "max_native_sends", "max_native_cancels", "hedge_flag", "investor_id",
                  "execution_mode", "account_tag", "additional_symbols")
        payload = {key: str(getattr(self.params_map, key)) for key in fields}
        payload["adapter_version"] = ADAPTER_VERSION
        return sha256_hex(canonical_dumps(payload))

    def _compute_target_params_hash(self, overrides):
        """AR2.1 dual hash: the TARGET hash is computed by this client alone,
        on a frozen copy of the current canonical parameter encoding, with
        EXACTLY the five restricted override fields applied. Challenge,
        transient switches, permit_id, record hashes and timestamps never
        enter it (no authorization/permit self-reference)."""
        _validate_effective_overrides(overrides)
        fields = ("exchange", "instrument_id", "execution_api_url", "order_volume",
                  "order_type", "risk_policy_id", "authorization_id",
                  "max_native_sends", "max_native_cancels", "hedge_flag", "investor_id",
                  "execution_mode", "account_tag", "additional_symbols")
        payload = {key: str(getattr(self.params_map, key)) for key in fields}
        payload["adapter_version"] = ADAPTER_VERSION
        for name in ARMING_OVERRIDE_FIELDS:
            payload[name] = str(overrides[name])
        return sha256_hex(canonical_dumps(payload))

    def update_status_bar(self):
        self._assert_callback_thread()
        _native.updateState(self.strategy_id, {
            field.title: str(getattr(self.state_map, key)) for key, field in self.state_map.fields().items()})

    def output(self, *messages):
        self._assert_callback_thread()
        _native.writeLog(msg="[" + self.strategy_name + "] " + " ".join(map(str, messages)))

    def _bind_callback_thread(self, kind):
        if self._callback_thread_id is None:
            self._callback_thread_id = threading.get_ident()
        self._record_thread_evidence(kind)

    def _record_thread_evidence(self, kind):
        callback = CALLBACK_KIND_MAP.get(kind)
        if callback is None:
            return
        if self._instance_id is None:
            return  # R1.2: no thread evidence encoded for a not-yet-obtained instance
        evidence = {
            "evidence_id": "cte-" + uuid.uuid4().hex[:16], "instance_id": self._instance_id,
            "session_epoch": self._session_epoch, "generation": self._generation,
            "process_instance_id": self._process_instance_id,
            "callback_thread_id": threading.get_ident(), "callback_kind": callback,
            "observed_at": _iso(_now_utc()), "origin": "PRODUCTION"}
        self._thread_evidence_id = evidence["evidence_id"]
        self._latest_evidence_body = canonical_dumps(
            wire_packet("ClientThreadEvidence", evidence))

    def on_init(self):
        self._bind_callback_thread("on_init")
        self._params_sha256 = self._compute_params_hash()
        self.update_status_bar()
        self.output(f"{ADAPTER_VERSION} execution client loaded; native calls disabled")

    def on_start(self):
        self._bind_callback_thread("on_start")
        self._assert_callback_thread()
        if self._running:
            return  # repeated start on a running object is an idempotent no-op
        if self._stop_incomplete:
            # R1.4: an old lane that never exited must not hand the store lock
            # to a new life of this object.
            self._refuse_start("STORE_BUSY")
            return
        if not self.params_map.exchange.strip() or not self.params_map.instrument_id.strip() \
                or _IDENTIFIER_RE.fullmatch(self.params_map.hedge_flag or "") is None:
            # R1.2: missing contract or registration-required hedge means an
            # invalid start; no half-initialized store is created.
            self._set_state("execution_status", "invalid")
            self.update_status_bar()
            self.output("execution parameters incomplete; not started")
            return
        injected = self._injected_pairing_token
        if injected is not None:
            # R1.2 injection seam: explicit store AND an injected native or
            # transport double; otherwise the token is refused outright.
            fixture = (self._store_directory is not None
                       and (self._native_recorder is not None
                            or self._transport is not self._default_transport))
            if not fixture:
                self._refuse_start("PAIRING_INJECTION_NOT_ALLOWED")
                return
            if not _hex64(injected):
                self._refuse_start("PAIRING_TOKEN_FORMAT_INVALID")
                return
        try:
            state = self._open_client_state()
        except ContractError as exc:
            self._refuse_start(str(exc))
            return
        except OSError:
            self._refuse_start("STORE_OPEN_FAILED")
            return
        self._instance_id = state["instance_id"]
        self._deployment_key = state["deployment_key"]
        self._client_file_sha256 = state["client_file_sha256"]
        # R1.4: force-disable, clear in-memory pairing state and any old
        # service epoch; parameter confirmations stay void.
        if self.params_map.enable_trading:
            try:
                self.params_map.enable_trading = False
            except (AttributeError, TypeError, ValueError):
                pass
        self._pairing_token = None
        self._service_epoch = None
        self._activation_candidate = None
        self._pairing_injected = False
        # NE1-ARM-ARMING: every restart enters DISARMED with effective=False;
        # arming-state.json is NEVER read back for recovery (AR5.1) — any
        # historical ARMED text or leftover credential is dead evidence.
        self._arm_state = ARM_STATE_DISARMED
        self._arm_plan = None
        self._arm_request = None
        self._arm_confirmation = None
        self._arm_context = None
        self._effective_params_sha256 = self._params_sha256
        self._arming_revoked = False
        self._arming_ack_received = False
        self._arming_consumed = False
        self._arming_service_gate_closed = None
        self._arming_deadline_monotonic = None
        self._last_arming_reason = None
        self._service_instance_id = None
        self._set_state("arming_state", ARM_STATE_DISARMED)
        self._set_state("enable_trading_effective", False)
        with self._state_lock:
            self._session_epoch = uuid.uuid4().hex
            self._generation = uuid.uuid4().hex
            self._current_event_stream_id = None  # new generation -> new stream
            self._retire_snapshot()
            self._running = True
        try:
            self._publish_pairing_request()
        except ContractError as exc:
            self._running = False
            self._refuse_start(str(exc))
            return
        except OSError:
            self._running = False
            self._refuse_start("STORE_PUBLISH_FAILED")
            return
        if injected is not None:
            # INJECTED/SYNTHETIC fixture branch (R1.2): after clearing old
            # runtime state, the explicitly injected candidate is used for
            # this life; never restored from any historical token store.
            self._pairing_token = injected
            self._pairing_state = PAIRING_STATE_PAIRED
            self._pairing_injected = True
            self._set_state("execution_status", "paired_disabled")
        else:
            self._pairing_state = PAIRING_STATE_WAIT
            self._set_state("execution_status", "wait_pairing")
        self._stop_event.clear()
        self._last_link_at = _now_utc()
        self._subscribe_snapshot()
        self._spawn_lanes()
        self.update_status_bar()
        self.output(f"execution client started instance={self._instance_id[:8]} "
                    f"generation={self._generation[:8]} pairing={self._pairing_state} "
                    f"sends=0 cancels=0")

    def on_stop(self):
        self._bind_callback_thread("on_stop")
        self._assert_callback_thread()
        with self._admission:
            self._running = False
            self._retire_snapshot()
            self._close_gate("stopped")
            # AR2.2 REVOKE_PENDING row: stop closes and latches the armed
            # round; this session can never arm again.
            self._revoke_arming("stopped")
        self._unsubscribe_snapshot()
        self._stop_event.set()
        # OV-F05: the two-party stop chain needs the STOPPED report so the
        # service can confirm DISABLED after its REVOKE_PENDING phase. Only a
        # paired life can send it (zero anonymous control in WAIT_PAIRING).
        self._report_control("STOPPED")
        deadline = time.monotonic() + min(STOP_JOIN_TIMEOUT_SECONDS, self._http_timeout + 0.5)
        with self._state_lock:
            lanes = list(self._lanes.values())
            for lane in lanes:
                lane.retired = True
                lane.wake.set()
            self._lanes.clear()
            self._generation = uuid.uuid4().hex  # retired generation marker
        for lane in lanes:
            if lane.thread is not None:
                lane.thread.join(timeout=max(0.0, deadline - time.monotonic()))
        # R1.4: mark the current request inactive; the session/request context
        # is gone, so a leftover pairing.json can never activate again.
        self._mark_request_inactive()
        with self._admission:
            self._pairing_token = None
            self._activation_candidate = None
            self._service_epoch = None
            self._pairing_state = PAIRING_STATE_UNPAIRED
        stuck = any(lane.thread is not None and lane.thread.is_alive() for lane in lanes)
        if stuck:
            # STORE_BUSY/STOP_INCOMPLETE: the store lock is NOT handed over
            # while an old worker may still write; evidence is preserved.
            self._stop_incomplete = True
            self._set_state("execution_status", "stop_incomplete")
        else:
            self._release_store()
            self._set_state("execution_status", "stopped")
        self.update_status_bar()
        self.output(f"execution client stopped sends={self.counters['native_send_attempts']} "
                    f"cancels={self.counters['native_cancel_attempts']}")

    # -- R1 deterministic store + persistent identity --------------------------

    def _resolve_store_directory(self) -> str:
        """Compute (never create) the deployment-state store directory.

        R1.1: ``<this module's directory>/.ak_execution/strategy-<id>/`` by
        default; an explicit store_directory must be a local absolute path.
        The path never depends on CWD, date, PID, UUID, contract, account,
        strategy_name, session or generation."""
        strategy_id = self._strategy_id
        if not self._strategy_id_assigned:
            raise ContractError("STRATEGY_ID_UNASSIGNED")
        if isinstance(strategy_id, bool) or not isinstance(strategy_id, int) \
                or strategy_id < 0:
            raise ContractError("STRATEGY_ID_INVALID")
        if self._store_directory is not None:
            directory = _validated_absolute_path(str(self._store_directory), "STORE_PATH_INVALID")
        else:
            module_file = _validated_absolute_path(os.path.abspath(__file__),
                                                   "STORE_PATH_INVALID")
            directory = os.path.join(os.path.dirname(module_file), STORE_ROOT_NAME,
                                     f"strategy-{strategy_id}")
        return _windows_long_path(os.path.normpath(directory))

    def _deployment_context(self):
        """Validated module path + deployment key + self-file hash (computed
        only at a valid on_start; links are rejected without following)."""
        module_file = _validated_absolute_path(os.path.abspath(__file__),
                                               "STORE_PATH_INVALID")
        _assert_regular_file(module_file, "STORE_PATH_INVALID")
        _reject_linked_ancestors(module_file, "STORE_PATH_INVALID")
        try:
            with open(module_file, "rb") as handle:
                payload = handle.read()
        except OSError:
            raise ContractError("STORE_PATH_INVALID") from None
        normalized = os.path.normcase(os.path.normpath(os.path.abspath(module_file)))
        return {"deployment_key": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
                "client_file_sha256": hashlib.sha256(payload).hexdigest()}

    def _open_client_state(self) -> dict:
        """Open (first-create) the layout-2 store under the exclusive
        client.lock; returns the persistent instance identity context.

        Every failure mode — bad identity, deployment-slot mismatch, corrupt
        DB, occupied slot, unprovable persistence — raises ContractError with
        a safe local reason code and leaves the object disabled; there is NO
        in-memory fallback."""
        directory = self._resolve_store_directory()
        deployment = self._deployment_context()
        _reject_linked_ancestors(directory, "STORE_PATH_INVALID")
        if os.path.exists(directory):
            _assert_real_directory(directory, "STORE_PATH_INVALID")
            self._check_store_layout(directory)
        else:
            try:
                os.makedirs(directory)
            except OSError:
                raise ContractError("STORE_CREATE_FAILED") from None
            # post-open identity re-verification (guard against replacement
            # between the pre-checks and the creation)
            _assert_real_directory(directory, "STORE_PATH_INVALID")
            # AR4.3: the store tree this client created is protected from the
            # first moment it exists (never "write then tighten").
            try:
                _protect_created_directories(directory)
            except (ContractError, OSError):
                raise ContractError("STORE_ACL_RESTRICTED") from None
        lock = _FileLock(os.path.join(directory, CLIENT_LOCK_FILE_NAME))
        if not lock.acquire():
            raise ContractError("STORE_LOCK_HELD")
        try:
            _assert_real_directory(directory, "STORE_PATH_INVALID")
            identity = self._load_or_create_identity(directory, deployment)
            try:
                stores = _LocalStores(directory)
            except sqlite3.Error:
                raise ContractError("STORE_DB_INVALID") from None
            if stores.read_meta("pairing_token") is not None:
                # R1.3: a legacy r3 store carrying the old token meta is never
                # silently claimed; original bytes stay untouched.
                stores.close()
                raise ContractError("LEGACY_STORE_MIGRATION_REQUIRED")
        except BaseException:
            lock.release()
            raise
        self._client_lock = lock
        self._stores = stores
        self._store_path = directory
        return {"instance_id": identity["instance_id"],
                "store_directory": directory,
                "store_layout_version": STORE_LAYOUT_VERSION,
                "deployment_key": deployment["deployment_key"],
                "client_file_sha256": deployment["client_file_sha256"],
                "identity_created_at": identity["created_at"]}

    def _check_store_layout(self, directory):
        try:
            entries = set(os.listdir(directory))
        except OSError:
            raise ContractError("STORE_OPEN_FAILED") from None
        recognized_temp = tuple("." + name + "." for name in _STORE_JSON_FILE_NAMES)
        unknown = {name for name in entries - _STORE_FILE_NAMES
                   if not name.startswith(recognized_temp)}
        if unknown:
            raise ContractError("LEGACY_STORE_MIGRATION_REQUIRED")
        meaningful = entries - {CLIENT_LOCK_FILE_NAME}
        if meaningful and IDENTITY_FILE_NAME not in meaningful:
            # non-empty old store without identity: r3-era layout
            raise ContractError("LEGACY_STORE_MIGRATION_REQUIRED")

    def _load_or_create_identity(self, directory, deployment):
        path = os.path.join(directory, IDENTITY_FILE_NAME)
        if os.path.exists(path):
            _assert_regular_file(path, "STORE_IDENTITY_INVALID")
            try:
                raw = open(path, "rb").read(_MAX_PAIRING_FILE_BYTES + 1)
            except OSError:
                raise ContractError("STORE_IDENTITY_INVALID") from None
            _reject_code(len(raw) <= _MAX_PAIRING_FILE_BYTES, "STORE_IDENTITY_INVALID")
            try:
                record = strict_loads(raw.decode("utf-8", errors="strict"),
                                      limit=_MAX_PAIRING_FILE_BYTES)
            except (ContractError, UnicodeDecodeError, json.JSONDecodeError):
                raise ContractError("STORE_IDENTITY_INVALID") from None
            _validate_identity_record(record)
            if record["deployment_key"] != deployment["deployment_key"]:
                raise ContractError("STORE_SLOT_MISMATCH")
            if record["strategy_id"] != self._strategy_id:
                raise ContractError("STORE_IDENTITY_INVALID")
            return record
        record = {
            "schema_version": IDENTITY_SCHEMA_VERSION,
            "instance_id": uuid.uuid4().hex,
            "deployment_key": deployment["deployment_key"],
            "strategy_id": int(self._strategy_id),
            "created_at": _iso(_now_utc()),
        }
        payload = (canonical_dumps(record) + "\n").encode("utf-8")
        try:
            handle = open(path, "x", encoding="ascii", newline="\n")
            try:
                handle.write(payload.decode("ascii"))
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                handle.close()
        except FileExistsError:
            return self._load_or_create_identity(directory, deployment)
        except OSError:
            # full disk / failed fsync / permission: refuse, never generate a
            # second UUID to mask an incomplete identity (R1.3)
            raise ContractError("STORE_IDENTITY_WRITE_FAILED") from None
        try:
            with open(path, "rb") as handle:
                if handle.read(len(payload) + 1) != payload:
                    raise ContractError("STORE_IDENTITY_WRITE_FAILED")
        except OSError:
            raise ContractError("STORE_IDENTITY_WRITE_FAILED") from None
        return record

    def _release_store(self):
        stores, self._stores = self._stores, None
        if stores is not None:
            try:
                stores.close()
            except Exception:
                pass
        lock, self._client_lock = self._client_lock, None
        if lock is not None:
            lock.release()
        self._pairing_request_record = None
        self._store_path = None

    def _refuse_start(self, code):
        """Record the safe local refusal code, unwind any partial store state
        and stay disabled. Local codes never enter the frozen HTTP enum."""
        self._last_refusal = {"code": code, "at": _iso(_now_utc())}
        self._last_pairing_reason = code
        self._pairing_state = PAIRING_STATE_UNPAIRED
        self._pairing_token = None
        self._activation_candidate = None
        if not self._stop_incomplete:
            # a stop-incomplete life keeps its store/lock exactly so the old
            # workers cannot be joined by a new writer (R1.4 STORE_BUSY)
            self._release_store()
        self._set_state("execution_status", "store_refused")
        self._set_state("control_status", code)
        self.update_status_bar()
        self.output(f"start refused: {code}")

    # -- R1.4 pairing request publication --------------------------------------

    def _publish_pairing_request(self) -> dict:
        """Publish the non-secret current-start description (one JSON object,
        strict fields, canonical bytes) as pairing-request.json."""
        record = {
            "schema_version": PAIRING_REQUEST_SCHEMA_VERSION,
            "request_id": uuid.uuid4().hex,
            "active": True,
            "created_at": _iso(_now_utc()),
            "instance_id": self._instance_id,
            "session_epoch": self._session_epoch,
            "generation": self._generation,
            "process_instance_id": self._process_instance_id,
            "deployment_key": self._deployment_key,
            "strategy_id": int(self._strategy_id),
            "adapter_version": ADAPTER_VERSION,
            "client_file_sha256": self._client_file_sha256,
            "store_layout_version": STORE_LAYOUT_VERSION,
            "store_directory": self._store_path,
            "exchange": self.params_map.exchange,
            "instrument_id": self.params_map.instrument_id,
            "hedge_flag": self.params_map.hedge_flag,
            "execution_api_url": self.params_map.execution_api_url,
            "params_sha256": self._params_sha256,
        }
        _validate_pairing_request_record(record)
        payload = (canonical_dumps(record) + "\n").encode("utf-8")
        _atomic_replace_write(os.path.join(self._store_path, PAIRING_REQUEST_FILE_NAME),
                              payload)
        self._pairing_request_record = record
        return dict(record)

    def _read_published_request(self):
        """Read + strictly validate the current pairing-request.json bytes."""
        path = os.path.join(self._store_path, PAIRING_REQUEST_FILE_NAME)
        try:
            raw = open(path, "rb").read(_MAX_PAIRING_FILE_BYTES + 1)
        except OSError:
            raise ContractError("PAIRING_REQUEST_UNREADABLE") from None
        _reject_code(len(raw) <= _MAX_PAIRING_FILE_BYTES, "PAIRING_REQUEST_UNREADABLE")
        try:
            record = strict_loads(raw.decode("utf-8", errors="strict"),
                                  limit=_MAX_PAIRING_FILE_BYTES)
        except (ContractError, UnicodeDecodeError, json.JSONDecodeError):
            raise ContractError("PAIRING_REQUEST_UNREADABLE") from None
        _reject_code(type(record) is dict and set(record) == _PAIRING_REQUEST_FIELDS,
                     "PAIRING_REQUEST_UNREADABLE")
        _reject_code(record.get("schema_version") == PAIRING_REQUEST_SCHEMA_VERSION,
                     "PAIRING_REQUEST_UNREADABLE")
        return record, raw

    def _mark_request_inactive(self):
        """Atomically mark the current request inactive (stop path)."""
        record = self._pairing_request_record
        if record is None or self._store_path is None:
            return
        stopped = dict(record)
        stopped["active"] = False
        payload = (canonical_dumps(stopped) + "\n").encode("utf-8")
        try:
            _atomic_replace_write(os.path.join(self._store_path, PAIRING_REQUEST_FILE_NAME),
                                  payload)
            self._pairing_request_record = stopped
        except OSError:
            # keep the evidence and the refusal code; the request context is
            # gone with the session either way
            self._last_refusal = {"code": "STORE_PUBLISH_FAILED", "at": _iso(_now_utc())}

    def _deactivate_for_restart(self, reason):
        """R1.4: void the current request and enter RESTART_REQUIRED (used
        for params/strategy changes while waiting for pairing)."""
        with self._admission:
            self._retire_snapshot()
            self._mark_request_inactive()
            self._pairing_state = PAIRING_STATE_RESTART
            self._activation_candidate = None
            self._last_pairing_reason = reason
            if self.params_map.enable_trading:
                try:
                    self.params_map.enable_trading = False
                except (AttributeError, TypeError, ValueError):
                    pass
            # NE1-ARM-ARMING: a restart-class failure voids every arming
            # candidate of this life (AR2.2 terminal row).
            self._revoke_arming(reason, restart=True)
            self._set_state("execution_status", "restart_required")
            self._set_state("control_status", reason)

    def _enter_restart_required(self, reason):
        """R3.2: service-side failure (auth rejected / epoch changed / service
        mismatch) closes the gate and stops this pairing's request storm; the
        request file itself stays as evidence for the operator."""
        with self._admission:
            self._retire_snapshot()
            self._pairing_state = PAIRING_STATE_RESTART
            self._activation_candidate = None
            self._last_pairing_reason = reason
            if self.params_map.enable_trading:
                try:
                    self.params_map.enable_trading = False
                except (AttributeError, TypeError, ValueError):
                    pass
            # NE1-ARM-ARMING: service identity loss is a restart-class
            # revocation; a reconnect can never revive the armed round.
            self._revoke_arming(reason, restart=True)
            self._set_state("execution_status", "restart_required")
            self._set_state("control_status", reason)

    # -- RULING 02 client credential activation --------------------------------

    def _stage_pairing_candidate(self):
        """Under pairing-transfer.lock (bounded, non-blocking): read the
        credential slot, verify the Windows ACL contract and match the FULL
        current active request context. No HTTP is performed while the file
        lock or admission is held. Returns (staged, reason_code)."""
        if self._pairing_state != PAIRING_STATE_WAIT or not self._running:
            return False, "NOT_WAIT_PAIRING"
        slot_path = os.path.join(self._store_path, CLIENT_PAIRING_FILE_NAME)
        if not os.path.exists(slot_path):
            return False, "PAIRING_MISSING"
        lock = _FileLock(os.path.join(self._store_path, TRANSFER_LOCK_FILE_NAME))
        if not lock.acquire():
            return False, "PAIRING_TRANSFER_LOCK_BUSY"
        try:
            _assert_regular_file(slot_path, "PAIRING_TARGET_INVALID")
            _verify_private_access(slot_path, need_write=False)
            try:
                raw = open(slot_path, "rb").read(_MAX_PAIRING_FILE_BYTES + 1)
            except OSError:
                raise ContractError("PAIRING_MISSING") from None
            _reject_code(len(raw) <= _MAX_PAIRING_FILE_BYTES, "PAIRING_FORMAT_INVALID")
            try:
                record = strict_loads(raw.decode("utf-8", errors="strict"),
                                      limit=_MAX_PAIRING_FILE_BYTES)
            except (ContractError, UnicodeDecodeError, json.JSONDecodeError):
                raise ContractError("PAIRING_FORMAT_INVALID") from None
            _validate_client_pairing_record(record)
            request, request_raw = self._read_published_request()
            _reject_code(type(request.get("active")) is bool and request["active"] is True,
                         "PAIRING_REQUEST_INACTIVE")
            _reject_code(request.get("schema_version") is not None
                         and request.get("request_id") is not None,
                         "PAIRING_REQUEST_UNREADABLE")
            request_sha256 = hashlib.sha256(request_raw).hexdigest()
            _reject_code(record["request_sha256"] == request_sha256,
                         "PAIRING_CONTEXT_MISMATCH")
            for field, current in (
                    ("request_id", request.get("request_id")),
                    ("instance_id", self._instance_id),
                    ("session_epoch", self._session_epoch),
                    ("generation", self._generation),
                    ("deployment_key", self._deployment_key),
                    ("strategy_id", int(self._strategy_id)),
                    ("params_sha256", self._params_sha256),
                    ("client_file_sha256", self._client_file_sha256)):
                _reject_code(record[field] == current, "PAIRING_CONTEXT_MISMATCH")
            for field in ("instance_id", "session_epoch", "generation",
                          "deployment_key", "strategy_id", "params_sha256",
                          "client_file_sha256"):
                _reject_code(record[field] == request.get(field),
                             "PAIRING_CONTEXT_MISMATCH")
            origin = _strict_execution_origin(self.params_map.execution_api_url)
            _reject_code(origin is not None, "PAIRING_ORIGIN_INVALID")
            _reject_code(record["execution_origin"] == origin, "PAIRING_CONTEXT_MISMATCH")
            self._activation_candidate = {
                "token": record["token"],
                "expected_service_epoch": record["service_epoch"],
                "service_instance_id": record["service_instance_id"],
                "request_id": record["request_id"],
                "request_sha256": record["request_sha256"],
                "generation": record["generation"],
            }
            return True, None
        finally:
            lock.release()

    def _activation_handshake(self):
        """The single authenticated null-service-epoch disabled poll; only a
        response whose service epoch equals the expected epoch from the
        credential slot promotes the client to PAIRED_DISABLED, after
        re-verifying the same active request and generation (R1.4)."""
        candidate = self._activation_candidate
        if candidate is None or self._pairing_state != PAIRING_STATE_WAIT \
                or not self._running:
            return False
        request_life = self._snapshot_life()
        packet = self._envelope({
            "snapshot": None,  # first request: snapshot/snapshot_id/ack all empty
            "snapshot_id": None,
            "client_state_digest": sha256_hex(canonical_dumps({
                "sends": self.counters["native_send_attempts"],
                "cancels": self.counters["native_cancel_attempts"],
                "generation": self._generation})),
            "ack_watermark": None}, "PollRequest")
        packet["data"]["service_epoch"] = None  # service_epoch=null on the wire
        try:
            response = self._post("/execution/v1/poll", packet,
                                  token=candidate["token"])
        except PollBackoffRequest as backoff:
            self._enter_poll_backoff(backoff.retry_after_ms)
            return False
        except ContractError as exc:
            if str(exc) == "http_401":
                self._enter_restart_required("service_auth_rejected")
            else:
                # I/O failure keeps the client disabled; bounded retry only
                self._last_pairing_reason = "PAIRING_IO_FAILED"
            return False
        self.counters["poll_requests"] += 1
        try:
            data = validate_poll_response(response)
        except ContractError:
            self._last_pairing_reason = "PAIRING_RESPONSE_INVALID"
            return False
        epoch = data.get("service_epoch")
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
            self._enter_restart_required("service_epoch_invalid")
            return False
        with self._admission:
            # re-verify the same active request and generation before opening
            # any other business transport
            if not self._running or self._pairing_state != PAIRING_STATE_WAIT \
                    or self._activation_candidate is not candidate \
                    or self._generation != candidate["generation"]:
                return False
            try:
                _, request_raw = self._read_published_request()
            except ContractError:
                return False
            if hashlib.sha256(request_raw).hexdigest() != candidate["request_sha256"]:
                self._enter_restart_required("request_changed")
                return False
            if epoch != candidate["expected_service_epoch"]:
                self._enter_restart_required("PAIRING_SERVICE_MISMATCH")
                return False
            with self._service_epoch_lock:
                self._service_epoch = epoch
            # NE1-ARM-ARMING: remember the verified service identity for the
            # arming request scope (never trusted from any file alone).
            self._service_instance_id = candidate["service_instance_id"]
            self._pairing_token = candidate["token"]
            self._pairing_state = PAIRING_STATE_PAIRED
            self._activation_candidate = None
            self.counters["pairing_activations"] = \
                self.counters.get("pairing_activations", 0) + 1
            self._set_state("execution_status", "paired_disabled")
            self._set_state("control_status", "ok")
            self._last_link_at = _now_utc()
            self._accept_snapshot_context(data, packet, request_life)
            return True

    def _try_activate_pairing(self) -> bool:
        """True only once activation has COMPLETED (PAIRED_DISABLED). Stages
        the credential candidate under the transfer lock; when called on the
        bound legal callback thread it also performs the bounded handshake.
        Waiting, invalid or missing credentials return False with a safe
        reason code in _last_pairing_reason (never the token)."""
        if self._pairing_state == PAIRING_STATE_PAIRED:
            return True
        if not self._running:
            self._last_pairing_reason = "NOT_RUNNING"
            return False
        if self._pairing_state != PAIRING_STATE_WAIT:
            self._last_pairing_reason = self._pairing_state
            return False
        try:
            staged, code = self._stage_pairing_candidate()
        except ContractError as exc:
            staged, code = False, str(exc)
        if not staged:
            self._last_pairing_reason = code or "PAIRING_MISSING"
            return False
        if self._callback_thread_id is not None \
                and threading.get_ident() == self._callback_thread_id:
            return self._activation_handshake()
        return False  # staged; the bounded control-lane schedule handshakes

    # -- NE1-ARM-ARMING local admission machine (AR2/AR4/AR5 client side) ----

    def _expected_scope(self, contract_lock_sha256):
        """The scope object this life must see in every arming record: built
        ONLY from the currently published pairing request, the live identity
        and the approved contract-lock reference carried by the proposal."""
        request, request_raw = self._read_published_request()
        origin = _strict_execution_origin(self.params_map.execution_api_url)
        _reject_code(origin is not None, "ARMING_ORIGIN_INVALID")
        return {
            "pairing_request_id": request["request_id"],
            "pairing_request_sha256": hashlib.sha256(request_raw).hexdigest(),
            "instance_id": self._instance_id,
            "session_epoch": self._session_epoch,
            "generation": self._generation,
            "process_instance_id": self._process_instance_id,
            "deployment_key": self._deployment_key,
            "strategy_id": int(self._strategy_id),
            "client_file_sha256": self._client_file_sha256,
            "adapter_version": ADAPTER_VERSION,
            "contract_lock_sha256": contract_lock_sha256,
            "execution_origin": origin,
            "pairing_params_sha256": self._params_sha256,
        }

    def _scope_matches(self, scope, contract_lock_sha256):
        expected = self._expected_scope(contract_lock_sha256)
        _reject_code(type(scope) is dict and scope == expected, "ARMING_SCOPE_MISMATCH")
        return True

    def _binding_matches_life(self, binding):
        _validate_binding(binding)
        _reject_code(binding["instance_id"] == self._instance_id
                     and binding["session_epoch"] == self._session_epoch
                     and binding["strategy_id"] == int(self._strategy_id)
                     and binding["exchange"] == self.params_map.exchange
                     and binding["instrument_id"] == self.params_map.instrument_id
                     and binding["hedge_flag"] == self.params_map.hedge_flag,
                     "ARMING_BINDING_MISMATCH")
        return True

    def _transition_arm_state(self, new_state, reason, **state_overrides):
        """AR2.2: strictly table-checked local transition + non-secret state
        publication. Publication failures propagate: the load-bearing
        tombstone path must fail closed."""
        current = self._arm_state
        _reject_code(new_state in _ARM_TRANSITIONS.get(current, frozenset()),
                     f"ARMING_TRANSITION_INVALID:{current}->{new_state}")
        self._arm_state = new_state
        self._last_arming_reason = reason
        self._set_state("arming_state", new_state)
        if new_state in (ARM_STATE_DISARMED_TERMINAL, ARM_STATE_RESTART_REQUIRED,
                         ARM_STATE_REVOKE_PENDING):
            self._set_state("enable_trading_effective", False)
        if new_state == ARM_STATE_ARMED:
            self._set_state("enable_trading_effective", True)
        if self._arm_plan is not None:
            self._publish_arming_state(**state_overrides)

    def _arming_effective_now(self, now=None):
        """The AR1.4 in-memory boolean: True only while a fully validated
        ArmContext is installed, unrevoked, unexpired (UTC and monotonic) on
        a paired life. Bare Params/State/file values never participate."""
        context = self._arm_context
        if context is None or self._arming_revoked or not self._running:
            return False
        if now is None:
            now = _now_utc()
        if context.expired(now, time.monotonic()):
            return False
        if self._pairing_state != PAIRING_STATE_PAIRED:
            return False
        with self._service_epoch_lock:
            if self._service_epoch != context.service_epoch:
                return False
        return self._effective_params_sha256 == context.target_params_sha256

    def _publish_arming_state(self, **overrides):
        """Write the non-secret ne1_arming_state_v1 slot (tombstone and ACK
        surface). Any ``true`` here is a report, never a restart input."""
        plan = self._arm_plan or {}
        request = self._arm_request or {}
        confirmation = self._arm_confirmation
        record = {
            "schema_version": ARMING_STATE_SCHEMA_VERSION,
            "scope": plan.get("scope"),
            "plan_id": plan.get("plan", {}).get("plan_id"),
            "arm_request_id": request.get("request_id"),
            "arm_id": confirmation["confirmation_id"] if confirmation else overrides.get("arm_id"),
            "state": self._arm_state,
            "consumed": self._arming_consumed,
            "revoked": self._arming_revoked,
            "effective_enable_trading": self._arming_effective_now(),
            "target_params_sha256": plan.get("target"),
            "service_instance_id": (request.get("service_instance_id")
                                    or self._service_instance_id),
            "service_epoch": (request.get("service_epoch")
                              if request else self._service_epoch),
            "control_revision": (confirmation or {}).get("expected_control_revision"),
            "expires_at": (confirmation or {}).get("expires_at")
                          or request.get("expires_at"),
            "local_gate_closed": self._arm_state in (ARM_STATE_REVOKE_PENDING,
                                                     ARM_STATE_DISARMED_TERMINAL,
                                                     ARM_STATE_RESTART_REQUIRED),
            "service_gate_closed": bool(self._arming_service_gate_closed),
            "unresolved_dispatch_count": self.counters["unresolved_dispatch_count"],
            "count_complete": (self._arm_state in (ARM_STATE_DISARMED_TERMINAL,
                                                   ARM_STATE_RESTART_REQUIRED)
                               and self.counters["unresolved_dispatch_count"] == 0),
            "updated_at": _iso(_now_utc()),
            "reason_code": self._last_arming_reason,
        }
        for key, value in overrides.items():
            if key in record and value is not None:
                record[key] = value
        record["arm_id"] = record["arm_id"] if record["arm_id"] else None
        _validate_arming_state_record(record)
        payload = (canonical_dumps(record) + "\n").encode("utf-8")
        _atomic_protected_write(os.path.join(self._store_path, ARMING_STATE_FILE_NAME),
                                payload)
        return record

    def _process_arming_files(self):
        """Background-lane arming pump (AR2.3): standard-library file and
        network validation plus pending-state persistence only — no native,
        no account query, no GUI, and no effective=True flip (that is the
        legal-callback-only _install_arm_context_if_pending). One admission
        step per call; repeated lane cycles walk the chain."""
        if not self._running or self._store_path is None:
            return
        try:
            if self._observe_disarm_request():
                return
            self._check_arming_expiry()
            state = self._arm_state
            if state == ARM_STATE_DISARMED:
                self._try_process_arming_proposal()
            elif state == ARM_STATE_PREPARED_DISABLED:
                self._maybe_publish_arming_request()
            elif state == ARM_STATE_ARM_READY:
                self._try_consume_arming_credential()
            elif state == ARM_STATE_ARM_PENDING and not self._arming_ack_received:
                self._retransmit_confirmation_if_pending()
        except ContractError as exc:
            self._last_arming_reason = str(exc)

    def _check_arming_expiry(self):
        """AR4.2/AR5: the request/challenge window is capped by BOTH a
        monotonic deadline and UTC re-verification; a clock rollback can
        never revive an expired candidate."""
        if self._arm_state not in (ARM_STATE_ARM_READY, ARM_STATE_ARM_PENDING):
            return
        request = self._arm_request or {}
        deadline = self._arming_deadline_monotonic
        now = _now_utc()
        expired = deadline is not None and time.monotonic() >= deadline
        if not expired and request.get("expires_at") is not None:
            expired = now >= _parse_iso(request["expires_at"])
        if expired:
            self._revoke_arming("arming_expired")

    def _try_process_arming_proposal(self):
        """DISARMED -> PREPARED_DISABLED (AR2.1/AR2.2): validate the
        prepare-arm proposal against the full current scope, compute the
        target hash on the frozen copy and publish the single plan."""
        if self._arm_state != ARM_STATE_DISARMED or not self._running:
            return False
        path = os.path.join(self._store_path, ARMING_PROPOSAL_FILE_NAME)
        if not os.path.exists(path):
            return False
        try:
            _verify_private_access(path, need_write=False)
            record, raw = _read_arming_slot(path, "ARMING_PROPOSAL_INVALID")
            _validate_arming_proposal_record(record)
            scope = record["scope"]
            contract_lock = scope["contract_lock_sha256"]
            self._scope_matches(scope, contract_lock)
            self._binding_matches_life(record["binding"])
            overrides = {name: record[name] for name in ARMING_OVERRIDE_FIELDS}
            target = self._compute_target_params_hash(overrides)
            plan = {
                "schema_version": ARMING_PLAN_SCHEMA_VERSION,
                "plan_id": sha256_hex(canonical_dumps(
                    {"proposal_sha256": hashlib.sha256(raw).hexdigest(),
                     "scope": scope, "target": target})),
                "proposal_id": record["proposal_id"],
                "proposal_sha256": hashlib.sha256(raw).hexdigest(),
                "scope": scope,
                "binding": record["binding"],
                "authorization_id": record["authorization_id"],
                "permit_id": record["permit_id"],
                "target_params_sha256": target,
                "effective_overrides": overrides,
                "created_at": _iso(_now_utc()),
            }
            _validate_arming_plan_record(plan)
            payload = (canonical_dumps(plan) + "\n").encode("utf-8")
            _atomic_protected_write(os.path.join(self._store_path, ARMING_PLAN_FILE_NAME),
                                    payload)
            with self._admission:
                self._arm_plan = {"plan": plan, "plan_bytes": payload, "scope": scope,
                                  "binding": record["binding"], "overrides": overrides,
                                  "target": target,
                                  "contract_lock_sha256": contract_lock,
                                  "generation": self._generation}
                self._transition_arm_state(ARM_STATE_PREPARED_DISABLED, "plan_published")
            return True
        except (ContractError, OSError) as exc:
            self._last_arming_reason = str(exc) if str(exc) else "ARMING_PROPOSAL_REJECTED"
            return False

    def _maybe_publish_arming_request(self):
        """PREPARED_DISABLED -> ARM_READY (AR2.2): only once this life is
        PAIRED_DISABLED with a verified current service epoch; publish the
        one-time non-secret request and the private challenge."""
        if self._arm_state != ARM_STATE_PREPARED_DISABLED or not self._running:
            return False
        if self._pairing_state != PAIRING_STATE_PAIRED:
            return False
        with self._service_epoch_lock:
            service_epoch = self._service_epoch
        if service_epoch is None or not _service_instance_id(self._service_instance_id):
            return False
        plan = self._arm_plan
        if self._params_sha256 != plan["scope"]["pairing_params_sha256"]:
            self._revoke_arming("params_changed")
            return False
        now = _now_utc()
        expires = now + timedelta(seconds=_ARMING_WINDOW_SECONDS)
        plan_record = plan["plan"]
        request = {
            "schema_version": ARMING_REQUEST_SCHEMA_VERSION,
            "request_id": uuid.uuid4().hex,
            "plan_id": plan_record["plan_id"],
            "plan_sha256": hashlib.sha256(plan["plan_bytes"]).hexdigest(),
            "scope": plan["scope"],
            "binding": plan["binding"],
            "authorization_id": plan_record["authorization_id"],
            "permit_id": plan_record["permit_id"],
            "target_params_sha256": plan["target"],
            "service_instance_id": self._service_instance_id,
            "service_epoch": service_epoch,
            "created_at": _iso(now),
            "expires_at": _iso(expires),
            "active": True,
        }
        _validate_arming_request_record(request)
        request_payload = (canonical_dumps(request) + "\n").encode("utf-8")
        challenge_value = secrets.token_hex(32)
        challenge = {
            "schema_version": ARMING_CHALLENGE_SCHEMA_VERSION,
            "request_id": request["request_id"],
            "request_sha256": hashlib.sha256(request_payload).hexdigest(),
            "challenge": challenge_value,
            "issued_at": _iso(now),
            "expires_at": _iso(expires),
            "writer_sid": _SECURITY.current_user_sid(),
        }
        _validate_arming_challenge_record(challenge)
        challenge_payload = (canonical_dumps(challenge) + "\n").encode("utf-8")
        try:
            _atomic_protected_write(
                os.path.join(self._store_path, ARMING_REQUEST_FILE_NAME), request_payload)
            _atomic_protected_write(
                os.path.join(self._store_path, ARMING_CHALLENGE_FILE_NAME),
                challenge_payload)
        except (ContractError, OSError):
            # partial publication never yields a live one-time request
            self._revoke_arming("arming_publish_failed")
            return False
        with self._admission:
            self._arm_request = {
                "request_id": request["request_id"],
                "request_bytes": request_payload,
                "request_sha256": hashlib.sha256(request_payload).hexdigest(),
                "expires_at": _iso(expires),
                "challenge": challenge_value,
                "challenge_expires_at": _iso(expires),
                "monotonic_deadline": time.monotonic() + _ARMING_WINDOW_SECONDS,
                "scope": plan["scope"], "binding": plan["binding"],
                "target": plan["target"], "plan_id": plan_record["plan_id"],
                "authorization_id": plan_record["authorization_id"],
                "permit_id": plan_record["permit_id"],
                "service_instance_id": self._service_instance_id,
                "service_epoch": service_epoch,
            }
            self._arming_deadline_monotonic = self._arm_request["monotonic_deadline"]
            self._transition_arm_state(ARM_STATE_ARM_READY, "request_published")
        return True

    def _try_consume_arming_credential(self):
        """ARM_READY -> ARM_PENDING (AR5.1): verify the user's one-time local
        credential under the transfer lock, persist the consumed tombstone
        FIRST, then deactivate the request and destroy both secret slots,
        and only then send this confirmation. One activation per life."""
        if self._arm_state != ARM_STATE_ARM_READY or not self._running:
            return False
        slot_path = os.path.join(self._store_path, CLIENT_ARMING_FILE_NAME)
        if not os.path.exists(slot_path):
            return False
        lock = _FileLock(os.path.join(self._store_path, ARMING_TRANSFER_LOCK_FILE_NAME))
        if not lock.acquire():
            self._last_arming_reason = "ARMING_TRANSFER_LOCK_BUSY"
            return False
        try:
            frozen = self._arm_request
            now = _now_utc()
            if time.monotonic() >= frozen["monotonic_deadline"] \
                    or now >= _parse_iso(frozen["expires_at"]):
                self._revoke_arming("arming_expired")
                return False
            request_path = os.path.join(self._store_path, ARMING_REQUEST_FILE_NAME)
            published, published_raw = _read_arming_slot(request_path,
                                                         "ARMING_REQUEST_INVALID")
            _validate_arming_request_record(published)
            _reject_code(published["request_id"] == frozen["request_id"]
                         and published["active"] is True
                         and hashlib.sha256(published_raw).hexdigest()
                         == frozen["request_sha256"],
                         "ARMING_REQUEST_CHANGED")
            _verify_private_access(slot_path, need_write=False)
            record, _raw = _read_arming_slot(slot_path, "ARMING_CREDENTIAL_INVALID")
            _validate_client_arming_record(record)
            _reject_code(record["request_id"] == frozen["request_id"]
                         and record["request_sha256"] == frozen["request_sha256"],
                         "ARMING_CREDENTIAL_MISMATCH")
            _reject_code(record["plan_id"] == frozen["plan_id"]
                         and record["scope"] == frozen["scope"]
                         and record["binding"] == frozen["binding"]
                         and record["target_params_sha256"] == frozen["target"]
                         and record["authorization_id"] == frozen["authorization_id"]
                         and record["permit_id"] == frozen["permit_id"],
                         "ARMING_CREDENTIAL_MISMATCH")
            _reject_code(record["service_instance_id"] == frozen["service_instance_id"]
                         and record["service_epoch"] == frozen["service_epoch"],
                         "ARMING_CREDENTIAL_MISMATCH")
            # the random challenge is displayed to no one and copied by no
            # one: only the exact private-slot value ever matches
            _reject_code(hmac.compare_digest(record["challenge"], frozen["challenge"]),
                         "ARMING_CHALLENGE_MISMATCH")
            _reject_code(record["user_sid"] == _SECURITY.current_user_sid(),
                         "ARMING_USER_SID_MISMATCH")
            expires = _parse_iso(record["expires_at"])
            _reject_code(now < expires, "ARMING_CREDENTIAL_EXPIRED")
            _reject_code(expires <= _parse_iso(frozen["expires_at"])
                         and expires <= _parse_iso(frozen["challenge_expires_at"]),
                         "ARMING_CREDENTIAL_WINDOW_INVALID")
            confirmation = {
                "confirmation_id": record["arm_id"],
                "binding": frozen["binding"],
                "params_sha256": frozen["target"],
                "adapter_sha256": self._client_file_sha256,
                "confirmed_at": record["confirmed_at"],
                "expires_at": record["expires_at"],
                "explicit_ui_action": True,
            }
            # AR5.1 order: tombstone -> request inactive -> destroy secrets.
            # Any failure after the tombstone is terminal (leftover secrets
            # are disposed by the user's reviewed tool, never auto-activated).
            self._arming_consumed = True
            self._arm_confirmation = {**confirmation,
                                      "expected_control_revision":
                                          record["expected_control_revision"]}
            self._transition_arm_state(ARM_STATE_ARM_PENDING, "credential_consumed",
                                       arm_id=record["arm_id"],
                                       control_revision=record["expected_control_revision"])
            inactive = dict(published)
            inactive["active"] = False
            inactive_payload = (canonical_dumps(inactive) + "\n").encode("utf-8")
            _atomic_protected_write(request_path, inactive_payload)
            os.unlink(slot_path)
            challenge_path = os.path.join(self._store_path, ARMING_CHALLENGE_FILE_NAME)
            if os.path.exists(challenge_path):
                os.unlink(challenge_path)
            _reject_code(not os.path.exists(slot_path)
                         and not os.path.exists(challenge_path),
                         "ARMING_SECRET_DESTROY_FAILED")
            # the in-memory secret dies with the slot
            self._arm_request = {**frozen, "challenge": None}
            self._last_arming_reason = "credential_consumed"
        except (ContractError, OSError) as exc:
            code = str(exc) or "ARMING_CONSUME_FAILED"
            self._last_arming_reason = code
            if self._arming_consumed:
                self._revoke_arming(code)
            return False
        finally:
            lock.release()
        # the confirmation is sent only after the lock is gone (no HTTP
        # under the file lock or the admission lock)
        self._send_params_confirmed()
        return True

    def _send_params_confirmed(self):
        """ARM_PENDING carrier (AR2.1/AR2.3): the ONLY message that carries
        the target hash before the gate opens; the same logical confirmation
        (same arm_id, same original expiry) is retransmitted, never re-signed."""
        confirmation = self._arm_confirmation
        if confirmation is None or self._arm_state != ARM_STATE_ARM_PENDING:
            return False
        packet = self._envelope({"kind": "PARAMS_CONFIRMED",
                                 "confirmation": {key: confirmation[key] for key in
                                                  ("confirmation_id", "binding",
                                                   "params_sha256", "adapter_sha256",
                                                   "confirmed_at", "expires_at",
                                                   "explicit_ui_action")}},
                                "ControlReport")
        try:
            response = self._post("/execution/v1/control", packet)
        except ContractError as exc:
            if str(exc) == "http_401":
                self._enter_restart_required("service_auth_rejected")
            else:
                self._last_arming_reason = "ARMING_CONFIRMATION_IO_FAILED"
            return False
        self.counters["params_confirmations_sent"] += 1
        try:
            data = validate_control_response(response)
        except ContractError:
            self._last_arming_reason = "ARMING_ACK_INVALID"
            return False
        if data.get("report_kind") != "PARAMS_CONFIRMED" \
                or data.get("confirmation_id") != confirmation["confirmation_id"]:
            self._last_arming_reason = "ARMING_ACK_MISMATCH"
            return False
        if data.get("kind") == "RECORDED":
            with self._state_lock:
                self._arming_ack_received = True
            self._last_arming_reason = "service_recorded"
            if self._arm_plan is not None:
                try:
                    self._publish_arming_state()
                except (ContractError, OSError):
                    pass
            return True
        # BLOCKED: the service refused this confirmation — terminal, no new
        # challenge, no second arm
        self._revoke_arming("service_blocked_confirmation")
        return False

    def _retransmit_confirmation_if_pending(self):
        """AR5.1: bounded retransmission of the SAME logical confirmation
        while the exact ACK is missing; the original arm_id and expiry never
        move."""
        if self._arm_state != ARM_STATE_ARM_PENDING or self._arming_ack_received:
            return False
        return self._send_params_confirmed()

    def _observe_disarm_request(self):
        """AR5.2: observe the user's scoped disarm.json. Revocation priority
        over arm; idempotent; a foreign scope is STALE_SCOPE and revokes
        nothing here."""
        path = os.path.join(self._store_path, CLIENT_DISARM_FILE_NAME)
        if not os.path.exists(path):
            return False
        try:
            _verify_private_access(path, need_write=False)
            record, _raw = _read_arming_slot(path, "ARMING_DISARM_INVALID")
            _validate_client_disarm_record(record)
            plan = self._arm_plan
            if plan is None or record["scope"] != plan["scope"]:
                self._last_arming_reason = "ARMING_DISARM_STALE_SCOPE"
                return False
            self._revoke_arming("user_disarm")
            return True
        except (ContractError, OSError) as exc:
            self._last_arming_reason = str(exc) or "ARMING_DISARM_INVALID"
            return False

    def _revoke_arming(self, reason, *, restart=False):
        """AR5.2 revocation: latch the intent, refuse every new admission and
        post-fsync entry, drop the effective context (hash returns to the
        pairing hash), publish local_gate_closed and finish in a terminal
        state. Existing calls are never rolled back or implicitly cancelled;
        the service barrier stays a separately displayed fact."""
        if self._arm_state in (ARM_STATE_DISARMED_TERMINAL, ARM_STATE_RESTART_REQUIRED):
            return
        terminal = ARM_STATE_RESTART_REQUIRED if restart else ARM_STATE_DISARMED_TERMINAL
        with self._admission:
            self._arming_revoked = True
            self._arm_context = None
            self._effective_params_sha256 = self._params_sha256
            self._set_state("enable_trading_effective", False)
            if self._arm_state != ARM_STATE_REVOKE_PENDING:
                self._transition_arm_state(ARM_STATE_REVOKE_PENDING, reason)
            else:
                self._last_arming_reason = reason
            self._transition_arm_state(terminal, reason)
        # best-effort service notification outside the admission lock
        self._report_control("GATE_CLOSED")

    def _arming_activation_blockers(self, now):
        """Full re-verification list for the ARM_PENDING -> ARMED install
        (AR2.2/AR2.3): every condition is re-checked inside the admission
        critical section on the legal callback thread."""
        blockers = []
        frozen = self._arm_request or {}
        plan = self._arm_plan or {}
        if not self._arming_ack_received:
            blockers.append("NO_SERVICE_ACK")
        if self._arming_revoked:
            blockers.append("REVOKED")
        if self._pairing_state != PAIRING_STATE_PAIRED:
            blockers.append("NOT_PAIRED")
        if plan.get("generation") != self._generation:
            blockers.append("GENERATION_CHANGED")
        if self._params_sha256 != plan.get("scope", {}).get("pairing_params_sha256"):
            blockers.append("PAIRING_HASH_CHANGED")
        confirmation = self._arm_confirmation or {}
        expires_text = confirmation.get("expires_at") or frozen.get("expires_at")
        if expires_text is None:
            blockers.append("NO_EXPIRY")
        else:
            if now >= _parse_iso(expires_text):
                blockers.append("ARMING_EXPIRED")
        deadline = self._arming_deadline_monotonic
        if deadline is not None and time.monotonic() >= deadline:
            blockers.append("ARMING_EXPIRED_MONOTONIC")
        with self._service_epoch_lock:
            if self._service_epoch != frozen.get("service_epoch"):
                blockers.append("SERVICE_EPOCH_CHANGED")
        if not self._arming_consumed:
            blockers.append("NOT_CONSUMED")
        return blockers

    def _install_arm_context_if_pending(self):
        """AR2.2 ARMED row: ONLY a legal framework callback may run this (the
        sole False->True flip of enable_trading_effective); every condition
        is re-verified inside the caller's admission critical section and
        the effective hash plus ArmContext switch atomically."""
        if self._arm_state != ARM_STATE_ARM_PENDING:
            return False
        now = _now_utc()
        blockers = self._arming_activation_blockers(now)
        if blockers:
            if "ARMING_EXPIRED" in blockers or "ARMING_EXPIRED_MONOTONIC" in blockers:
                self._revoke_arming("arming_expired")
            return False
        confirmation = self._arm_confirmation
        frozen = self._arm_request
        plan = self._arm_plan
        context = ArmContext(
            risk_policy_id=plan["overrides"]["risk_policy_id"],
            authorization_id=plan["plan"]["authorization_id"],
            permit_id=plan["plan"]["permit_id"],
            max_native_sends=plan["overrides"]["max_native_sends"],
            max_native_cancels=plan["overrides"]["max_native_cancels"],
            target_params_sha256=plan["target"],
            pairing_params_sha256=self._params_sha256,
            binding=plan["binding"],
            service_instance_id=frozen["service_instance_id"],
            service_epoch=frozen["service_epoch"],
            control_revision=confirmation["expected_control_revision"],
            arm_id=confirmation["confirmation_id"],
            request_id=frozen["request_id"],
            plan_id=plan["plan"]["plan_id"],
            expires_at=_parse_iso(confirmation["expires_at"]),
            monotonic_deadline=frozen["monotonic_deadline"],
            installed_at=now)
        with self._state_lock:
            self._arm_context = context
            # AR2.1: the atomic activation switch of the effective hash
            self._effective_params_sha256 = context.target_params_sha256
        self._transition_arm_state(ARM_STATE_ARMED, "context_installed")
        return True

    # -- lanes (control / events; generation-bound, bounded) ----------------

    def _spawn_lanes(self):
        for kind in ("control", "events"):
            lane = _Lane(kind)
            lane.thread = threading.Thread(target=self._lane_loop, args=(lane,),
                                           name=f"ne1-{lane.kind}-{self._instance_id[:6]}",
                                           daemon=True)
            with self._state_lock:
                self._lanes[kind] = lane
            lane.thread.start()

    def _lane_loop(self, lane):
        """Generation-bound periodic lane; at most one in-flight cycle each."""
        while not self._stop_event.is_set() and not lane.retired:
            if lane.kind == "control":
                interval = (POLL_ARMED_MIN_INTERVAL_MS / 1000.0
                            if self._arming_effective_now()
                            else POLL_DISABLED_INTERVAL_MS / 1000.0)
            else:
                interval = 0.05
            lane.wake.wait(interval)
            lane.wake.clear()
            if self._stop_event.is_set() or lane.retired:
                return
            lane.busy = True
            try:
                if lane.kind == "control":
                    self._control_cycle()
                else:
                    self._events_cycle()
            except Exception:
                pass  # lanes never touch native/GUI; bounded retry next cycle
            finally:
                lane.busy = False

    def _wake_events(self):
        with self._state_lock:
            lane = self._lanes.get("events")
        if lane is not None:
            lane.wake.set()

    # -- network transport ---------------------------------------------------

    @staticmethod
    def _default_transport(url, payload, timeout, headers=None, *, admission=None):
        endpoint = urlsplit(validate_loopback_url(url))
        if isinstance(timeout, bool) or not 0 < float(timeout) <= MAX_HTTP_TIMEOUT_SECONDS:
            raise ValueError("invalid_transport_timeout")
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            raise ValueError("request_too_large")
        connection_type = HTTPSConnection if endpoint.scheme == "https" else HTTPConnection
        host = "127.0.0.1" if endpoint.hostname.casefold().rstrip(".") == "localhost" else endpoint.hostname
        connection = connection_type(host, endpoint.port, timeout=float(timeout))
        try:
            request = connection.request
            request_path = endpoint.path
            request_headers = {
                "Content-Type": "application/json; charset=utf-8", "Accept": "application/json",
                "Connection": "close", **(headers or {})}
            # Final validation admits the prepared request; no lock spans I/O.
            if admission is not None:
                admission()
            request("POST", request_path, body=body, headers=request_headers)
            response = connection.getresponse()
            if 300 <= response.status < 400:
                raise ValueError("redirect_not_allowed")
            data = response.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                raise ValueError("response_too_large")
            return response.status, bytes(data)
        finally:
            connection.close()

    def _auth_headers(self):
        """Pairing-token header; the token never enters logs or params."""
        token = self._pairing_token
        if not token:
            return {}
        return {"X-Execution-Token": token}

    @staticmethod
    def _poll_backoff_error(body):
        """Build the PollBackoffRequest from a 429 response: the typed
        TransportErrorResponse retry_after_ms is authoritative; a body that
        does not decode still backs off with the conservative default (a 429
        must never produce a poll storm whatever its body looks like)."""
        retry_ms = POLL_BACKOFF_DEFAULT_MS
        try:
            data = validate_transport_error_response(
                strict_loads(body, limit=MAX_RESPONSE_BYTES))
            if data["error_code"] == "POLL_TOO_FAST" and data["http_status"] == 429 \
                    and data.get("retry_after_ms") is not None:
                retry_ms = data["retry_after_ms"]
        except Exception:
            pass
        return PollBackoffRequest(retry_ms)

    def _enter_poll_backoff(self, retry_after_ms):
        """Withhold every poll until the requested window expires."""
        with self._state_lock:
            self._poll_backoff_until = time.monotonic() + retry_after_ms / 1000.0
        self.counters["poll_backoffs"] = self.counters.get("poll_backoffs", 0) + 1
        self._set_state("control_status", "poll_backoff")
        self.output(f"poll throttled (429 POLL_TOO_FAST); backing off {retry_after_ms} ms")

    def _in_poll_backoff(self):
        with self._state_lock:
            return time.monotonic() < self._poll_backoff_until

    def _post(self, path, packet, *, token=None):
        url = _endpoint(self.params_map.execution_api_url, path)
        headers = {"X-Execution-Token": token} if token else self._auth_headers()
        transport = self._transport
        transport_args = (url, packet, self._http_timeout, dict(headers))
        default_transport = transport is self._default_transport
        admission = None
        if path == "/execution/v1/poll" and packet["data"].get("snapshot") is not None:
            check_snapshot_request = self._check_snapshot_request
            admission = lambda: check_snapshot_request(packet)
        if default_transport:
            status, body = transport(*transport_args, admission=admission)
        else:
            legacy_args = transport_args[:3]
            if admission is not None:
                admission()
            try:
                status, body = transport(*transport_args)
            except TypeError:
                # C2-era injected test doubles take (url, payload, timeout).
                if admission is not None:
                    admission()
                status, body = transport(*legacy_args)
        if status is None or not 200 <= status < 300:
            if status == 429:
                # POLL_TOO_FAST maps one-to-one to 429 in the C1.1 frozen
                # error table; back off per retry_after_ms instead of the
                # fixed lane interval (boundary races must not storm the poll).
                raise self._poll_backoff_error(body)
            raise ContractError(f"http_{status or 'unknown'}")
        decoded = strict_loads(body, limit=MAX_RESPONSE_BYTES)
        _require(type(decoded) is dict and decoded.get("schema_version") == TRANSPORT_SCHEMA_VERSION,
                 "unknown schema version")
        return decoded

    def _envelope(self, body_extra, body_type):
        with self._state_lock:
            self._sequence += 1
            # AR2.1: the effective hash is the pairing hash until the atomic
            # ARMED switch; disabled messages never carry the target hash
            # (only the PARAMS_CONFIRMED confirmation itself does).
            effective = self._effective_params_sha256 or self._params_sha256
        return wire_packet(body_type, {
            "request_id": uuid.uuid4().hex, "instance_id": self._instance_id,
            "session_epoch": self._session_epoch, "generation": self._generation,
            "service_epoch": self._service_epoch, "sequence": self._sequence,
            "sent_at": _iso(_now_utc()), "params_sha256": effective,
            **body_extra})

    # -- control lane: poll + ready -----------------------------------------

    def _control_cycle(self):
        # AR2.3: background-lane arming work first — file/network validation
        # and pending-state persistence only (never native/GUI/account, and
        # never an effective flip).
        self._process_arming_files()
        if not self._running:
            return
        if self._in_poll_backoff():
            return  # 429 backoff: zero poll requests, no sequence consumed
        state = self._pairing_state
        if state == PAIRING_STATE_RESTART or state == PAIRING_STATE_UNPAIRED:
            return  # closed gate: no request storm from a broken pairing
        if state == PAIRING_STATE_WAIT:
            # bounded local credential-slot check (no new worker, no storm);
            # zero anonymous HTTP while unpaired
            if self._activation_candidate is None:
                slot_path = os.path.join(self._store_path or "", CLIENT_PAIRING_FILE_NAME)
                if not self._store_path or not os.path.exists(slot_path):
                    return
                try:
                    staged, code = self._stage_pairing_candidate()
                except ContractError as exc:
                    staged, code = False, str(exc)
                if not staged:
                    self._last_pairing_reason = code
                    return
            self._activation_handshake()
            return
        with self._service_epoch_lock:
            service_epoch = self._service_epoch
        with self._state_lock:
            request_life = self._snapshot_life()
            snapshot = self._snapshot_for_poll()
            packet = self._envelope({
                "snapshot": snapshot,
                "snapshot_id": snapshot["snapshot_id"] if snapshot is not None else None,
                "client_state_digest": sha256_hex(canonical_dumps({
                    "sends": self.counters["native_send_attempts"],
                    "cancels": self.counters["native_cancel_attempts"],
                    "generation": self._generation})),
                "ack_watermark": (self._latest_ack_watermark()
                                  if service_epoch is not None else None)}, "PollRequest")
        if request_life != self._snapshot_life():
            return
        try:
            response = self._post("/execution/v1/poll", packet)
        except PollBackoffRequest as backoff:
            self._enter_poll_backoff(backoff.retry_after_ms)
            return
        except ContractError as exc:
            if str(exc) == "http_401":
                # R3.2: the token is dead (service restart/rotation): close
                # the gate and stop this pairing's invalid-request storm
                self._enter_restart_required("service_auth_rejected")
            return
        self.counters["poll_requests"] += 1
        if request_life != self._snapshot_life():
            return  # an old in-flight request cannot establish a successor life
        try:
            data = validate_poll_response(response)
        except ContractError:
            self._reject_snapshot_context("response_invalid")
            return
        self._set_state("control_status", "ok")  # recovered from any backoff
        if data.get("service_epoch") is not None:
            if service_epoch is None:
                with self._service_epoch_lock:
                    self._service_epoch = data["service_epoch"]
            elif data["service_epoch"] != service_epoch:
                # R3.2: the service restarted under us; the old active session
                # must not silently ride the new epoch
                self._enter_restart_required("service_epoch_changed")
                return
        self._accept_snapshot_context(data, packet, request_life)
        self._last_link_at = _now_utc()
        kind = data["kind"]
        if kind == "OFFER":
            # AR2.1: execution offers are pursued ONLY while an installed
            # armed round is effective (ARM_PENDING and earlier states stop
            # taking offers/ready); a service "armed" wording alone never
            # promotes anything.
            if self._arm_context is None or not self._arming_effective_now():
                self._set_state("control_status", "not_armed_offer_ignored")
                return
            offer = data["offer"]
            if offer["params_sha256"] != self._effective_params_sha256:
                self._set_state("control_status", "params_mismatch")
                return
            if offer["binding"]["instance_id"] != self._instance_id:
                self._set_state("control_status", "binding_mismatch")
                return
            # DTTL-02: the offer's first absolute deadline is immutable server
            # state; repeated poll/ready/recovery never yields a later expiry.
            self._remember_offer_deadline(offer)
            challenge = {
                "offer_id": offer["offer_id"],
                "capture_id": self._latest_capture.get("capture_id") or "none",
                "thread_evidence_id": self._thread_evidence_id or "none",
                "latest_params_sha256": self._effective_params_sha256,
                "enable_trading": self._arming_effective_now(),
                "unconfirmed_event_watermark": self._latest_ack_watermark(),
                "generation": self._generation}
            ready = self._envelope({"offer_id": offer["offer_id"], "challenge": challenge},
                                   "ReadyRequest")
            reply = self._post("/execution/v1/ready", ready)
            command = self._extract_dispatch(reply)
            if command is not None:
                self._receive_dispatch(command)

    def _extract_dispatch(self, reply):
        """Accept the C1.1-frozen server wire type 'ReadyResponse'.

        kind=DISPATCH carries exactly one committed command and no blockers;
        kind=BLOCKED carries no command. The legacy C2 test-double name
        'DispatchResponse' is no longer accepted on the wire.
        """
        _require(type(reply) is dict and reply.get("type") == "ReadyResponse",
                 "unexpected ready reply type")
        data = reply.get("data") or {}
        kind = data.get("kind")
        _require(kind in {"DISPATCH", "BLOCKED"}, "unknown ready response kind")
        command = data.get("dispatch")
        if kind == "BLOCKED":
            _require(command is None, "blocked ready reply carries no command")
            return None
        if command is None:
            return None
        validate_dispatch_command(command)
        return command

    def _remember_offer_deadline(self, offer):
        offer_id = offer["offer_id"]
        expires = offer["expires_at"]
        with self._state_lock:
            previous = self._offer_deadlines.get(offer_id)
            if previous is not None and previous != expires:
                # DTTL-02: the offer deadline is computed once on the server;
                # an identical offer ID never carries a different expiry. Any
                # change is a dispatch-window violation -> quarantine latch.
                self._window_conflict_latched = True
                self.counters["dispatch_window_conflicts"] = \
                    self.counters.get("dispatch_window_conflicts", 0) + 1
                self._set_state("execution_status",
                                "quarantined:offer_deadline_changed")
                return
            self._offer_deadlines[offer_id] = expires
            while len(self._offer_deadlines) > 64:
                self._offer_deadlines.popitem(last=False)

    def _quarantine_dispatch(self, command, reason):
        """DTTL-02/03 quarantine: capture_incomplete-class latch +
        DISPATCH_WINDOW_CONFLICT diagnostic receipt, zero native call."""
        self._window_conflict_latched = True
        self.counters["dispatch_window_conflicts"] = \
            self.counters.get("dispatch_window_conflicts", 0) + 1
        self._set_state("execution_status", f"quarantined:{reason}")
        self._record_receipt(command, "NO_CALL", None, None, reason)

    def _offer_deadline_for(self, command):
        """Earliest known offer deadline for this command's offer (C1.2)."""
        with self._state_lock:
            return self._offer_deadlines.get(command["offer_id"])

    def _validate_admission_mirror(self, command, now):
        """Mirror of C1.2 validate_dispatch_admission (DTTL-01/03/05):
        issued <= now < expires (equality is expired), encoded lifetime within
        the frozen 1500 ms cap, and a command that never outlives its offer.
        Returns a blocker list; COMMAND_OUTLIVES_OFFER also quarantines."""
        blockers = []
        issued = _parse_iso(command["issued_at"])
        expires = _parse_iso(command["expires_at"])
        lifetime_ms = (expires - issued).total_seconds() * 1000
        offer_expires = self._offer_deadline_for(command)
        if offer_expires is not None:
            offer_dt = _parse_iso(offer_expires)
            if expires > offer_dt:
                blockers.append("COMMAND_OUTLIVES_OFFER")
        if lifetime_ms <= 0:
            blockers.append("NONPOSITIVE_WINDOW")
        elif lifetime_ms > DISPATCH_TTL_MS:
            blockers.append("TTL_EXCEEDED")
        if now < issued:
            blockers.append("NOT_YET_ISSUED")
        if now >= expires:
            blockers.append("EXPIRED")
        return blockers, offer_expires

    def _receive_dispatch(self, command):
        # DTTL-02: a command may never outlive its offer's first absolute
        # deadline. Violation -> quarantine at receipt: the ticket never
        # enters the slot, zero native call, NO_CALL diagnostic receipt.
        offer_expires = self._offer_deadline_for(command)
        if offer_expires is not None and _parse_iso(command["expires_at"]) > _parse_iso(offer_expires):
            self._quarantine_dispatch(command, "dispatch_window_conflict")
            return
        with self._state_lock:
            if self._dispatch_slot is not None:
                self.counters["dispatch_backpressured"] += 1
                return  # slot=1: never overwrite an accepted ticket
            self._dispatch_slot = command
            self._dispatch_slot_epoch = self._generation
            self.counters["dispatch_received"] += 1
            self.counters["unresolved_dispatch_count"] = 1

    # -- event lane: outbox upload + receipt upload ---------------------------

    def _latest_ack_watermark(self):
        return None

    def _events_cycle(self):
        if self._pairing_state != PAIRING_STATE_PAIRED:
            return  # R1.4: no anonymous events/results while unpaired
        while True:
            try:
                record = self._result_uploads.popleft()
            except IndexError:
                break
            try:
                self._upload_receipt(record)
            except ContractError as exc:
                self._result_uploads.appendleft(record)
                if str(exc) == "http_401":
                    self._enter_restart_required("service_auth_rejected")
                break
            except Exception:
                self._result_uploads.appendleft(record)
                break
        if self._stores is None:
            return
        rows = self._stores.pending_events(MAX_EVENTS_PER_BATCH)
        if not rows:
            return
        events = []
        kept_ids = []
        stream_key = None
        for event_id, body in rows:
            parsed = strict_loads(body, limit=MAX_REQUEST_BYTES)
            item = parsed["data"] if parsed.get("type") == "RawCallbackEnvelope" else parsed
            key = (item.get("event_stream_id"), item.get("callback_session_epoch"),
                   item.get("callback_generation"))
            if stream_key is None:
                stream_key = key
            if key != stream_key:
                break  # one batch carries exactly one stream/session/generation
            events.append(item)
            kept_ids.append(event_id)
        if not events:
            return
        packet = self._envelope({"events": events}, "EventBatch")
        self._post("/execution/v1/events", packet)
        self.counters["event_batches_sent"] += 1
        self._stores.ack_events(kept_ids)

    # -- capture: raw callback whitelist -> outbox ----------------------------

    _CALLBACK_WHITELIST = (
        "OrderID", "OrderSysID", "TradeID", "TradingDay", "Exchange", "ExchangeID",
        "InstrumentID", "Direction", "Offset", "Hedgeflag", "HedgeFlag", "Price",
        "Volume", "TotalVolume", "TradedVolume", "CancelVolume", "Status",
        "OrderTime", "CancelTime", "TradeTime", "FrontID", "SessionID", "Memo",
        "errCode", "exchange", "instrument_id", "order_id", "order_sys_id",
        "trade_id", "trading_day", "direction", "offset", "price", "volume",
        "total_volume", "traded_volume", "cancel_volume", "status",
        "order_time", "cancel_time", "trade_time", "front_id", "session_id",
        "error_code")

    def _event_stream_id(self):
        """One stream per (instance, session, generation); persisted (RULING 09).

        The persisted key includes the generation, so a restart with a new
        generation allocates a NEW stream id instead of renumbering the old
        one; sequence allocation continues from the per-generation counter.
        """
        if self._current_event_stream_id is not None:
            return self._current_event_stream_id
        meta_key = f"event_stream_id:{self._generation}"
        candidate = "stream-" + uuid.uuid4().hex[:20]
        if self._stores is not None:
            persisted = self._stores.read_meta(meta_key)
            if persisted:
                candidate = persisted
            else:
                self._stores.write_meta(meta_key, candidate)
                self._stores.write_meta("event_sequence", "0")
        self._current_event_stream_id = candidate
        return candidate

    def _next_event_sequence(self):
        if self._stores is not None:
            return self._stores.next_event_sequence(1)[0]
        with self._state_lock:
            self._memory_event_sequence += 1
            return self._memory_event_sequence

    def _capture_callback(self, kind, raw):
        if self._stores is None:
            return
        fields = []
        source = raw if isinstance(raw, dict) else {name: getattr(raw, name, None)
                                                    for name in self._CALLBACK_WHITELIST}
        for key in self._CALLBACK_WHITELIST:
            value = source.get(key) if isinstance(source, dict) else source.get(key, None)
            if value is None:
                continue
            text = str(value).strip()
            if not text or len(text) > 256 or any(ord(ch) < 32 for ch in text):
                continue
            fields.append({"key": key, "value": text})
            if len(fields) >= MAX_RAW_FIELDS_PER_EVENT:
                break
        if not fields:
            return
        stream_id = self._event_stream_id()
        sequence = self._next_event_sequence()
        envelope = {
            "event_id": "evt-" + uuid.uuid4().hex[:16], "kind": kind,
            "event_stream_id": stream_id, "event_sequence": sequence,
            "captured_at": _iso(_now_utc()), "callback_generation": self._generation,
            "callback_session_epoch": self._session_epoch, "route_evidence_id": None,
            "raw_fields": fields}
        body = canonical_dumps({"schema_version": TRANSPORT_SCHEMA_VERSION,
                                 "type": "RawCallbackEnvelope", "data": envelope})
        try:
            self._stores.append_event(body)
        except Exception:
            self._latch_event_gap("outbox_write_failed")
            return
        items, size = self._stores.unacked_counts()
        self._set_state("outbox_depth", items)
        if items > OUTBOX_MAX_ITEMS or size > OUTBOX_MAX_BYTES:
            self._latch_event_gap("outbox_threshold")

    def _report_control(self, kind):
        """Best-effort /execution/v1/control upload (plan §5.3).

        Never blocks or raises into the caller: a failed upload is retried by
        the control lane on the next cycle. GATE_CLOSED/STOPPED/GENERATION_
        REPORT are sent here; PARAMS_CONFIRMED goes exclusively through
        _send_params_confirmed, which carries the frozen confirmation payload
        and decodes the exact ACK. Only a paired (authenticated) life sends
        — zero anonymous control requests while waiting for pairing.
        """
        _require(kind in CONTROL_KINDS, "unknown control kind")
        if self._pairing_state != PAIRING_STATE_PAIRED:
            return False
        try:
            packet = self._envelope({"kind": kind, "confirmation": None}, "ControlReport")
            self._post("/execution/v1/control", packet)
            self.counters["control_reports_sent"] = self.counters.get("control_reports_sent", 0) + 1
            return True
        except Exception:
            return False

    def _latch_event_gap(self, reason):
        self._event_gap_latched = True
        self._set_state("event_gap", True)
        self._set_state("execution_status", "event_gap")
        self._close_gate(reason)
        # AR2.2 REVOKE_PENDING row: an event gap is a revocation trigger for
        # the armed round (latched; recovery never re-arms it).
        self._revoke_arming(reason)

    # -- disabled native snapshot capture (legal callback only) --------------

    def _snapshot_life(self):
        request = self._pairing_request_record or {}
        return (self._instance_id, self._session_epoch, self._generation,
                self._params_sha256,
                getattr(self, "_effective_params_sha256", None) or self._params_sha256,
                self._compute_params_hash(), self.strategy_id,
                self.params_map.exchange, self.params_map.instrument_id,
                self.params_map.hedge_flag, self.params_map.investor_id,
                request.get("request_id"), request.get("active"), self._running)

    def _retire_snapshot(self):
        with self._state_lock:
            self._snapshot_context = None
            self._snapshot_binding_pin = None
            self._snapshot_facts = {}
            self._snapshot_frozen = None
            self._snapshot_queries = {}
            self._latest_capture = {"quote_at": None, "account_at": None,
                                    "position_at": None, "contract_at": None,
                                    "capture_id": None}
            self._set_state("snapshot_status", "awaiting_context")

    def _reject_snapshot_context(self, code):
        with self._state_lock:
            self._snapshot_context = None
            self._snapshot_facts = {}
            self._snapshot_frozen = None
            self._latest_capture = {"quote_at": None, "account_at": None,
                                    "position_at": None, "contract_at": None,
                                    "capture_id": None}
            self._snapshot_fault("context", code)

    def _accept_snapshot_context(self, data, packet, request_life):
        with self._state_lock:
            if request_life != self._snapshot_life() or not self._running:
                return False
            try:
                header = packet["data"]
                for name in ("request_id", "instance_id", "session_epoch", "generation",
                             "sequence", "params_sha256"):
                    _require(type(data.get(name)) is type(header[name])
                             and data[name] == header[name], "snapshot_response_identity")
                _require("request_service_epoch" in data
                         and type(data["request_service_epoch"]) is type(header["service_epoch"])
                         and data["request_service_epoch"] == header["service_epoch"],
                         "snapshot_response_epoch_echo")
                _integer(data.get("service_epoch"), "snapshot service_epoch", 1)
                _require(data["service_epoch"] == self._service_epoch,
                         "snapshot_service_epoch")
                _integer(data.get("ledger_revision"), "snapshot ledger_revision")
                _require(request_life[3] == request_life[5], "snapshot_parameters_changed")
                binding = data.get("binding")
                _validate_binding(binding)
                date.fromisoformat(binding["trading_day"])
                for name, value in (
                        ("instance_id", self._instance_id), ("session_epoch", self._session_epoch),
                        ("strategy_id", self.strategy_id), ("exchange", self.params_map.exchange),
                        ("instrument_id", self.params_map.instrument_id),
                        ("hedge_flag", self.params_map.hedge_flag)):
                    _require(type(binding[name]) is type(value) and binding[name] == value,
                             "snapshot_binding_identity")
                binding_text = canonical_dumps(binding)
                pin = self._snapshot_binding_pin
                _require(pin is None or pin == (request_life, binding_text),
                         "snapshot_binding_changed")
            except (ContractError, TypeError, ValueError):
                self._reject_snapshot_context("invalid_response_binding")
                return False
            self._snapshot_binding_pin = (request_life, binding_text)
            self._snapshot_context = {
                "life": request_life, "binding": binding_text,
                "service_epoch": data["service_epoch"], "ledger_revision": data["ledger_revision"]}
            self._snapshot_errors.pop("context", None)
            if self._snapshot_frozen is None:
                self._set_state("snapshot_status", "awaiting_callback")
            return True

    def _snapshot_context_current(self):
        context = self._snapshot_context
        return (context is not None and self._running
                and self._pairing_state == PAIRING_STATE_PAIRED
                and context["life"] == self._snapshot_life()
                and context["service_epoch"] == self._service_epoch)

    def _snapshot_for_poll(self):
        if not self._snapshot_context_current():
            if self._snapshot_context is not None:
                self._retire_snapshot()
            return None
        frozen = self._snapshot_frozen
        if frozen is None or frozen[0] != self._snapshot_context["life"]:
            return None
        return strict_loads(frozen[1], limit=MAX_REQUEST_BYTES)

    def _check_snapshot_request(self, packet):
        with self._state_lock:
            snapshot = self._snapshot_for_poll()
            header = packet["data"]
            _require(snapshot is not None and header["snapshot"] == snapshot,
                     "snapshot_lifecycle_changed")
            for name, value in (("instance_id", self._instance_id),
                                ("session_epoch", self._session_epoch),
                                ("generation", self._generation),
                                ("params_sha256", self._snapshot_context["life"][4]),
                                ("service_epoch", self._service_epoch)):
                _require(header.get(name) == value, "snapshot_lifecycle_changed")

    def _snapshot_fault(self, component, code):
        self._snapshot_errors[component] = code
        self._set_state("snapshot_status", component + ":" + code)

    def _subscribe_snapshot(self):
        self._assert_callback_thread()
        if self._snapshot_subscription is not None or not self._running:
            return
        symbol = (self.params_map.exchange, self.params_map.instrument_id)
        try:
            _native.subMarketData(StrategyObj=self, ExchangeID=symbol[0], InstrumentID=symbol[1])
        except Exception:
            self._snapshot_fault("subscription", "subscribe_failed")
            self.output("snapshot subscribe_failed")
            return
        self._snapshot_subscription = symbol
        self._snapshot_errors.pop("subscription", None)

    def _unsubscribe_snapshot(self):
        self._assert_callback_thread()
        symbol = self._snapshot_subscription
        self._snapshot_subscription = None
        if symbol is None:
            return
        try:
            _native.unsubMarketData(StrategyObj=self, ExchangeID=symbol[0], InstrumentID=symbol[1])
        except Exception:
            self._snapshot_fault("subscription", "unsubscribe_failed")
            self.output("snapshot unsubscribe_failed")

    def _snapshot_fields(self, raw, names):
        self._assert_callback_thread()
        if isinstance(raw, Mapping):
            return {name: raw[name] for name in names if name in raw}
        if raw is None or type(raw) in (bool, int, float, str):
            return {}
        result = {}
        missing = object()
        for name in names:
            value = getattr(raw, name, missing)
            if value is not missing:
                result[name] = value
        return result

    @staticmethod
    def _snapshot_value(fields, *names):
        for name in names:
            if name in fields:
                return fields[name]
        return None

    @staticmethod
    def _snapshot_day(value):
        if type(value) is date:
            return value.isoformat()
        if type(value) is not str:
            return None
        text = value.strip()
        if re.fullmatch(r"\d{8}", text):
            text = text[:4] + "-" + text[4:6] + "-" + text[6:]
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            return None

    @staticmethod
    def _snapshot_time(value):
        if type(value) is str:
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
        if not isinstance(value, datetime):
            return None
        try:
            if value.tzinfo is None:
                value = value.replace(tzinfo=_CHINA_TIMEZONE)
            return _iso(value)
        except (OverflowError, ValueError):
            return None

    @staticmethod
    def _snapshot_number(value, *, integer=False, positive=False, object_zero=False):
        if type(value) not in (int, float, str, Decimal):
            return None
        try:
            number = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
        if not number.is_finite() or (object_zero and number == 0):
            return None
        if positive and number <= 0:
            return None
        if integer:
            if number < (1 if positive else 0) or number > 2 ** 63 - 1 \
                    or number != number.to_integral_value():
                return None
            return int(number)
        if number != 0 and not -60 <= number.adjusted() <= 60:
            return None
        text = _decimal_wire(number)
        return text if len(text) <= 64 else None

    def _snapshot_identities(self, fields, binding, *, investor=None, required=False):
        groups = (("exchange", ("Exchange", "ExchangeID", "exchange")),
                  ("instrument_id", ("InstrumentID", "instrument_id")),
                  ("hedge_flag", ("HedgeFlag", "Hedgeflag", "hedge_flag")))
        for target, names in groups:
            values = [fields[name] for name in names if name in fields]
            if required and not values:
                return False
            if any(type(value) is not str or value != binding[target] for value in values):
                return False
        for name in ("TradingDay", "trading_day"):
            if name in fields and self._snapshot_day(fields[name]) != binding["trading_day"]:
                return False
        if investor is not None:
            for name in ("InvestorID", "investor_id", "investor"):
                if name in fields and (type(fields[name]) is not str or fields[name] != investor):
                    return False
        return True

    _SNAPSHOT_IDENTITIES = ("Exchange", "ExchangeID", "exchange", "InstrumentID", "instrument_id",
                            "HedgeFlag", "Hedgeflag", "hedge_flag", "InvestorID", "investor_id",
                            "investor", "TradingDay", "trading_day", "Datetime", "datetime")
    _SNAPSHOT_QUANTITIES = ("Position", "PositionClose", "FrozenClosing",
                            "YdPositionClose", "YdFrozenClosing")

    def _snapshot_investor(self):
        self._assert_callback_thread()
        rows = _native.getInvestorList()
        if isinstance(rows, Mapping):
            rows = [rows] if "InvestorID" in rows or "investor_id" in rows else list(rows.values())
        if not isinstance(rows, (list, tuple)) or not rows:
            raise ContractError("investor_unavailable")
        identities = []
        for row in rows:
            fields = self._snapshot_fields(row, ("InvestorID", "investor_id"))
            values = [fields[name] for name in ("InvestorID", "investor_id") if name in fields]
            if not values or any(type(value) is not str or not value.strip() or value != values[0]
                                 for value in values):
                raise ContractError("investor_identity_invalid")
            identities.append(values[0])
        selected = self.params_map.investor_id
        if selected:
            if type(selected) is not str or identities.count(selected) != 1:
                raise ContractError("investor_selection_mismatch")
            return selected
        if len(identities) != 1:
            raise ContractError("investor_ambiguous")
        return identities[0]

    def _snapshot_record(self, value, fields, sampled_at):
        return {"value": value, "sampled_at": _iso(sampled_at),
                "source_at": self._snapshot_time(self._snapshot_value(fields, "Datetime", "datetime"))}

    def _snapshot_account(self, investor, binding):
        self._assert_callback_thread()
        raw = _native.getInvestorAccount(investor)
        fields = self._snapshot_fields(raw, self._SNAPSHOT_IDENTITIES +
                                        ("Available", "available", "CurrencyID", "currency"))
        if not self._snapshot_identities(fields, binding, investor=investor):
            raise ContractError("account_identity_invalid")
        available = self._snapshot_number(self._snapshot_value(fields, "Available", "available"))
        currency = self._snapshot_value(fields, "CurrencyID", "currency")
        if type(currency) is not str or re.fullmatch(r"[A-Z]{3}", currency) is None:
            currency = None
        value = {"available": available, "currency": currency} \
            if available is not None or currency is not None else None
        return self._snapshot_record(value, fields, _now_utc())

    def _snapshot_positions(self, investor, binding):
        self._assert_callback_thread()
        raw = _native.getInvestorPosition(investor, Simple=True)
        if not isinstance(raw, (list, tuple)):
            raise ContractError("positions_unavailable")
        buckets = []
        seen = set()
        for row in raw:
            fields = self._snapshot_fields(row, self._SNAPSHOT_IDENTITIES +
                                            self._SNAPSHOT_QUANTITIES + ("Direction",))
            for names in (("InvestorID", "investor_id", "investor"),
                          ("Exchange", "ExchangeID", "exchange"),
                          ("InstrumentID", "instrument_id"),
                          ("HedgeFlag", "Hedgeflag", "hedge_flag")):
                values = [fields[name] for name in names if name in fields]
                if any(type(value) is not str or value != values[0] for value in values):
                    raise ContractError("position_identity_invalid")
            for name in ("InvestorID", "investor_id", "investor"):
                if name in fields and fields[name] != investor:
                    raise ContractError("position_investor_mismatch")
            if not self._snapshot_identities(fields, binding, investor=investor, required=True):
                identities = (self._snapshot_value(fields, "Exchange", "ExchangeID", "exchange"),
                              self._snapshot_value(fields, "InstrumentID", "instrument_id"),
                              self._snapshot_value(fields, "HedgeFlag", "Hedgeflag", "hedge_flag"))
                if all(type(value) is str and value for value in identities) \
                        and identities != (binding["exchange"], binding["instrument_id"], binding["hedge_flag"]):
                    continue
                raise ContractError("position_scope_unavailable")
            side = {"\u591a": "long", "\u7a7a": "short"}.get(fields.get("Direction"))
            if side is None or side in seen:
                raise ContractError("position_bucket_invalid")
            seen.add(side)
            value = {name: self._snapshot_number(fields.get(name), integer=True)
                     for name in self._SNAPSHOT_QUANTITIES}
            pairs = (("PositionClose", "Position"), ("YdPositionClose", "PositionClose"),
                     ("FrozenClosing", "PositionClose"), ("YdFrozenClosing", "FrozenClosing"),
                     ("YdFrozenClosing", "YdPositionClose"))
            if any(value[small] is not None and value[large] is not None
                   and value[small] > value[large] for small, large in pairs):
                raise ContractError("position_hierarchy_invalid")
            if all(value[name] is not None for name in self._SNAPSHOT_QUANTITIES[1:]) \
                    and value["FrozenClosing"] - value["YdFrozenClosing"] \
                    > value["PositionClose"] - value["YdPositionClose"]:
                raise ContractError("position_hierarchy_invalid")
            value["side"] = side
            buckets.append(self._snapshot_record(value, fields, _now_utc()))
        return self._snapshot_record(buckets, {}, _now_utc())

    def _snapshot_contract(self, binding):
        self._assert_callback_thread()
        raw = _native.getInstrument(Exchange=binding["exchange"], InstrumentID=binding["instrument_id"])
        fields = self._snapshot_fields(raw, self._SNAPSHOT_IDENTITIES +
                                        ("ProductClass", "ExpireDate", "PriceTick", "VolumeMultiple",
                                         "MinLimitOrderVolume", "MaxLimitOrderVolume"))
        if not self._snapshot_identities(fields, binding):
            raise ContractError("contract_identity_invalid")
        value = {
            "exchange": self._snapshot_value(fields, "Exchange", "ExchangeID", "exchange"),
            "instrument_id": self._snapshot_value(fields, "InstrumentID", "instrument_id"),
            "asset_class": {"1": "future", "2": "option"}.get(str(fields.get("ProductClass"))),
            "expiry": self._snapshot_day(fields.get("ExpireDate")),
            "price_tick": self._snapshot_number(fields.get("PriceTick"), positive=True),
            "multiplier": self._snapshot_number(fields.get("VolumeMultiple"), positive=True),
            "min_limit_order_volume": self._snapshot_number(fields.get("MinLimitOrderVolume"),
                                                             integer=True, positive=True),
            "max_limit_order_volume": self._snapshot_number(fields.get("MaxLimitOrderVolume"),
                                                             integer=True, positive=True),
            "additional_volume_limits": []}
        low, high = value["min_limit_order_volume"], value["max_limit_order_volume"]
        if low is not None and high is not None and low > high:
            raise ContractError("contract_volume_invalid")
        if not any(item is not None for key, item in value.items() if key != "additional_volume_limits"):
            value = None
        return self._snapshot_record(value, fields, _now_utc())

    def _snapshot_quote(self, raw, binding):
        names = self._SNAPSHOT_IDENTITIES + (
            "LastPrice", "last_price", "BidPrice1", "bid_price1", "AskPrice1", "ask_price1",
            "BidVolume1", "bid_volume1", "AskVolume1", "ask_volume1", "UpperLimitPrice",
            "upper_limit_price", "LowerLimitPrice", "lower_limit_price")
        fields = self._snapshot_fields(raw, names)
        if not self._snapshot_identities(fields, binding):
            raise ContractError("quote_identity_invalid")
        value = {}
        for target, aliases in (("last", ("LastPrice", "last_price")),
                                ("bid1", ("BidPrice1", "bid_price1")),
                                ("ask1", ("AskPrice1", "ask_price1")),
                                ("upper_limit", ("UpperLimitPrice", "upper_limit_price")),
                                ("lower_limit", ("LowerLimitPrice", "lower_limit_price"))):
            value[target] = self._snapshot_number(self._snapshot_value(fields, *aliases), positive=True)
        for target, aliases in (("bid_volume1", ("BidVolume1", "bid_volume1")),
                                ("ask_volume1", ("AskVolume1", "ask_volume1"))):
            value[target] = self._snapshot_number(self._snapshot_value(fields, *aliases), integer=True,
                                                   object_zero=not isinstance(raw, Mapping))
        value["market_status"] = self._snapshot_facts.get("status")
        value["time"] = self._snapshot_time(self._snapshot_value(fields, "Datetime", "datetime"))
        if not any(item is not None for item in value.values()):
            value = None
        return self._snapshot_record(value, fields, _now_utc())

    def _snapshot_timing(self, record, snapshot_id, *, known=None):
        if known is None:
            known = record is not None and record["value"] is not None
        return {"sampled_at": record["sampled_at"] if record is not None else None,
                "source_at": record["source_at"] if record is not None else None,
                "received_at": None, "snapshot_id": snapshot_id if record is not None else None,
                "source": "pythongo", "completeness": "partial" if known else "unknown"}

    def _freeze_snapshot(self, context):
        facts = self._snapshot_facts
        quote = facts.get("quote")
        account = facts.get("account")
        contract = facts.get("contract")
        positions = facts.get("positions")
        if not any(record is not None and record["value"] is not None
                   for record in (quote, account, contract)) \
                and not (positions and any(any(bucket["value"][name] is not None
                                               for name in self._SNAPSHOT_QUANTITIES)
                                           for bucket in positions["value"])):
            self._snapshot_frozen = None
            self._set_state("snapshot_status", "facts_unavailable")
            return
        binding = strict_loads(context["binding"], limit=MAX_REQUEST_BYTES)
        snapshot_id = "cap-" + uuid.uuid4().hex
        value = {"snapshot_id": snapshot_id, "binding": binding, "source": "pythongo",
                 "times": {"sampled_at": _iso(_now_utc()), "sent_at": None, "received_at": None},
                 "quote": None, "contract": contract["value"] if contract else None,
                 "account": None, "positions": [], "proof_id": None,
                 "ledger_revision": context["ledger_revision"],
                 "coverage": {"level": "unknown", "account_key": binding["account_key"],
                              "strategy_key": binding["strategy_key"],
                              "session_epoch": binding["session_epoch"],
                              "trading_day": binding["trading_day"], "source": "pythongo",
                              "as_of": None, "complete_through": None,
                              "completion_evidence_id": None, "own_active_count": None,
                              "account_active_count": None}}
        for name in ("quote", "contract", "account", "positions"):
            value["times"][name] = self._snapshot_timing(facts.get(name), snapshot_id)
        for name, record in (("quote", quote), ("account", account)):
            if record is not None and record["value"] is not None:
                value[name] = {"binding": binding, **record["value"]}
        for bucket in positions["value"] if positions is not None else ():
            value["positions"].append({"binding": binding, **bucket["value"],
                                       "timing": self._snapshot_timing(bucket, snapshot_id)})
        self._snapshot_frozen = (context["life"], canonical_dumps(value))
        capture = {"capture_id": snapshot_id}
        for name in ("quote", "account", "positions", "contract"):
            record = facts.get(name)
            known = record is not None and record["value"] is not None
            key = "position_at" if name == "positions" else name + "_at"
            capture[key] = _parse_iso(record["sampled_at"]) if known else None
        self._latest_capture = capture
        self._set_state("snapshot_status", "captured_partial")

    def _refresh_capture(self, *, quote=None, account=False, position=False, contract=False, status=None):
        self._assert_callback_thread()
        with self._state_lock:
            if not self._snapshot_context_current():
                if self._snapshot_context is not None:
                    self._retire_snapshot()
                return
            context = self._snapshot_context
            binding = strict_loads(context["binding"], limit=MAX_REQUEST_BYTES)
            changed = False
            if quote is not None and type(quote) is not bool:
                try:
                    self._snapshot_facts["quote"] = self._snapshot_quote(quote, binding)
                    self._snapshot_errors.pop("quote", None)
                except Exception:
                    self._snapshot_facts.pop("quote", None)
                    self._snapshot_fault("quote", "quote_invalid")
                changed = True
            if status is not None:
                try:
                    fields = self._snapshot_fields(status, self._SNAPSHOT_IDENTITIES + ("Status", "status"))
                    if not self._snapshot_identities(fields, binding):
                        raise ContractError("status_identity_invalid")
                    raw_status = self._snapshot_value(fields, "Status", "status")
                    market_status = raw_status if raw_status in ("continuous", "auction", "closed", "unknown") else None
                    if market_status != self._snapshot_facts.get("status"):
                        self._snapshot_facts["status"] = market_status
                        prior = self._snapshot_facts.get("quote")
                        if prior is not None and prior["value"] is not None:
                            prior["value"] = {**prior["value"], "market_status": market_status}
                        changed = True
                except Exception:
                    self._snapshot_fault("status", "status_invalid")
            attempt = self._snapshot_queries.get("account_positions")
            if attempt is None or time.monotonic() - attempt >= SNAPSHOT_ACCOUNT_INTERVAL_SECONDS:
                try:
                    investor = self._snapshot_investor()
                except Exception:
                    self._snapshot_facts.pop("account", None)
                    self._snapshot_facts.pop("positions", None)
                    self._snapshot_fault("investor", "investor_scope_unavailable")
                    changed = True
                else:
                    for name, reader in (("account", self._snapshot_account),
                                         ("positions", self._snapshot_positions)):
                        try:
                            self._snapshot_facts[name] = reader(investor, binding)
                            self._snapshot_errors.pop(name, None)
                            changed = True
                        except ContractError:
                            self._snapshot_facts.pop(name, None)
                            self._snapshot_fault(name, name + "_invalid")
                            changed = True
                        except Exception:
                            self._snapshot_fault(name, name + "_query_failed")
                self._snapshot_queries["account_positions"] = time.monotonic()
            attempt = self._snapshot_queries.get("contract")
            if attempt is None or time.monotonic() - attempt >= SNAPSHOT_CONTRACT_INTERVAL_SECONDS:
                try:
                    self._snapshot_facts["contract"] = self._snapshot_contract(binding)
                    self._snapshot_errors.pop("contract", None)
                    changed = True
                except ContractError:
                    self._snapshot_facts.pop("contract", None)
                    self._snapshot_fault("contract", "contract_invalid")
                    changed = True
                except Exception:
                    self._snapshot_fault("contract", "contract_query_failed")
                self._snapshot_queries["contract"] = time.monotonic()
            if not self._snapshot_context_current() or self._snapshot_context["binding"] != context["binding"]:
                return
            if changed:
                self._freeze_snapshot(context)

    def _fresh(self, key, ttl, now):
        stamp = self._latest_capture.get(key)
        if stamp is None:
            return False, "missing"
        age = (now - stamp).total_seconds()
        if age > ttl:
            return False, "stale"
        if age < -FUTURE_SKEW_SECONDS:
            return False, "future"
        return True, age

    # -- gate order + single native call (plan §7.2) --------------------------

    def _gate_blockers(self, command, now):
        """NE1-ARM-ARMING gate (AR2.3): no bare Params switch is trusted any
        more. Admission requires the installed ArmContext, the full Binding,
        the current service/session/generation/process identity, the target
        hash, the exact permit, the authorization reference, the control
        revision, the validity window and the per-round budget ceilings."""
        blockers = []
        context = self._arm_context
        if context is None:
            blockers.append("NOT_ARMED")
        else:
            if not self._arming_effective_now(now):
                blockers.append("ARMING_NOT_EFFECTIVE")
            if command["params_sha256"] != context.target_params_sha256:
                blockers.append("PARAMS_HASH_MISMATCH")
            if command["permit_id"] != context.permit_id:
                blockers.append("PERMIT_MISMATCH")
            if command["control_revision"] != context.control_revision:
                blockers.append("CONTROL_REVISION_MISMATCH")
            if not context.authorization_id:
                blockers.append("NO_AUTHORIZATION_REFERENCE")
        if not self._running:
            blockers.append("NOT_RUNNING")
        if self._event_gap_latched:
            blockers.append("EVENT_GAP")
        if command["params_sha256"] != self._effective_params_sha256:
            blockers.append("PARAMS_HASH_MISMATCH")
        if command["binding"]["instance_id"] != self._instance_id:
            blockers.append("BINDING_INSTANCE_MISMATCH")
        if command["binding"]["session_epoch"] != self._session_epoch:
            blockers.append("BINDING_EPOCH_MISMATCH")
        if command["binding"]["exchange"] != self.params_map.exchange \
                or command["binding"]["instrument_id"] != self.params_map.instrument_id:
            blockers.append("BINDING_SYMBOL_MISMATCH")
        if self._window_conflict_latched:
            blockers.append("WINDOW_CONFLICT_LATCHED")
        # DTTL-02/03: absolute offer-deadline inheritance replaces any
        # client-side earliest-value derivation. Expiry uses only the
        # command's own expires_at (the server already computed
        # min(offer.expires_at, t_issue+1500ms, D_current)); /ready grants
        # nothing and the client never re-arms or re-derives.
        mirror_blockers, _ = self._validate_admission_mirror(command, now)
        if "COMMAND_OUTLIVES_OFFER" in mirror_blockers:
            blockers.append("DISPATCH_WINDOW_CONFLICT")
        if "EXPIRED" in mirror_blockers:
            blockers.append("DISPATCH_EXPIRED")
        if "NOT_YET_ISSUED" in mirror_blockers:
            blockers.append("NOT_YET_ISSUED")
        if "NONPOSITIVE_WINDOW" in mirror_blockers or "TTL_EXCEEDED" in mirror_blockers:
            blockers.append("DISPATCH_TTL_INVALID")
        if command["kind"] == "SUBMIT":
            # Market-data freshness gates apply to submits; an owned cancel is
            # independent of stale quotes (plan §7.4).
            for key, ttl in (("quote_at", QUOTE_TTL_SECONDS), ("account_at", ACCOUNT_TTL_SECONDS),
                             ("position_at", POSITION_TTL_SECONDS),
                             ("contract_at", CONTRACT_TTL_SECONDS)):
                ok, _ = self._fresh(key, ttl, now)
                if not ok:
                    blockers.append(f"{key.upper()}_NOT_FRESH")
        if self._last_link_at is None or (now - self._last_link_at).total_seconds() > LINK_TTL_SECONDS:
            blockers.append("LINK_NOT_FRESH")
        if context is not None and command["permit_id"] in ("", None):
            blockers.append("NO_PERMIT")
        if command["command_id"] in self._consumed_commands:
            blockers.append("ALREADY_CONSUMED")
        if context is not None:
            # AR2.2 budget row: the ceilings come from the armed round's
            # ArmContext (send=1, cancel explicitly 0 or 1), never Params.
            if command["kind"] == "SUBMIT" \
                    and self.counters["native_send_attempts"] >= max(0, context.max_native_sends):
                blockers.append("SEND_BUDGET_EXHAUSTED")
            if command["kind"] == "CANCEL" \
                    and self.counters["native_cancel_attempts"] >= max(0, context.max_native_cancels):
                blockers.append("CANCEL_BUDGET_EXHAUSTED")
        return blockers

    def _pump_once(self):
        """Consume at most one pending dispatch on the legal callback thread."""
        self._assert_callback_thread()
        with self._admission:
            if self._stop_event.is_set() or not self._running:
                if self._dispatch_slot is not None:
                    self._record_receipt(self._dispatch_slot, "NO_CALL", None, None,
                                         "gate_closed_before_call")
                    self._release_slot()
                return
            # AR2.2: the ONLY False->True flip of the admission boolean runs
            # here — a legal framework callback, inside the critical section,
            # after the exact service ACK, re-verifying every condition.
            self._install_arm_context_if_pending()
            command = self._dispatch_slot
            if command is None:
                return
            now = _now_utc()
            # OV-O05/H17/H32: the persistent receipt store is the dedup
            # authority across processes. A command with an existing CONSUMED
            # receipt (from an earlier life of this client or another
            # process) is never consumed again: receipts-only re-upload,
            # zero native call, terminal NO_CALL chain.
            if command["command_id"] in self._terminal_no_call:
                self._record_receipt(command, "NO_CALL", None, None,
                                     "persistent_consumed_no_call")
                self._release_slot()
                return
            if self._stores is not None:
                prior_stages = {row[0] for row in
                                self._stores.receipt_for_command(command["command_id"])}
                if "CONSUMED" in prior_stages:
                    self._terminal_no_call.add(command["command_id"])
                    self.counters["terminal_no_call"] = \
                        self.counters.get("terminal_no_call", 0) + 1
                    self._record_receipt(command, "NO_CALL", None, None,
                                         "persistent_consumed_no_call")
                    self._release_slot()
                    return
            blockers = self._gate_blockers(command, now)
            if blockers:
                if "DISPATCH_WINDOW_CONFLICT" in blockers:
                    self._quarantine_dispatch(command, "dispatch_window_conflict")
                    self._release_slot()
                elif "ALREADY_CONSUMED" not in blockers and "DISPATCH_EXPIRED" in blockers:
                    self._record_receipt(command, "NO_CALL", None, None, "dispatch_expired")
                    self._release_slot()
                self.counters["native_call_blocked"] += 1
                self._set_state("control_status", "blocked")
                return
            self._consumed_commands[command["command_id"]] = True
            if len(self._consumed_commands) > 256:
                self._consumed_commands.popitem(last=False)
            self._record_receipt(command, "CONSUMED", None, None, None)
            # DTTL-05: terminal fence after the CONSUMED receipt is durable
            # and before any native attribute resolution, in the same
            # _admission critical section. A window that closed during the
            # CONSUMED write means zero call; committed facts (the CONSUMED
            # receipt itself) are retained and reconciled conservatively
            # (H16/H17). A terminal NO_CALL receipt chain follows and the
            # command is never retried on replay.
            fence_violations = self._post_fsync_fence(command)
            if fence_violations:
                self._terminal_no_call.add(command["command_id"])
                self.counters["terminal_no_call"] = \
                    self.counters.get("terminal_no_call", 0) + 1
                self._record_receipt(command, "NO_CALL", None, None,
                                     "terminal_fence_no_call")
                self._release_slot()
                self.counters["native_call_blocked"] += 1
                self._set_state("control_status", "terminal_no_call")
                self.output("dispatch window closed after CONSUMED; zero call")
                return
            self._release_slot()
            try:
                result_kind, value, error_code = self._invoke_native(command)
            except Exception as exc:
                result_kind, value, error_code = "EXCEPTION", None, "native_exception"
                self.output(f"native invocation failed: {type(exc).__name__}")
            if command["kind"] == "SUBMIT" \
                    and self.counters["native_send_attempts"] >= 1:
                # AR2.2 budget row: the round's FIRST send attempt — rejected,
                # exceptional or unknown outcome included — persistently
                # occupies the quota; sends stay closed, an independently
                # authorized CANCEL_OWNED keeps its remaining ceiling, and a
                # cancel can never reopen the send lane.
                if self._arm_state == ARM_STATE_ARMED:
                    self._transition_arm_state(ARM_STATE_ARMED_SEND_EXHAUSTED,
                                               "send_exhausted")
            self._record_receipt(command, "RETURNED", result_kind, value, error_code)
            self._set_state("last_receipt_at", _iso(_now_utc()))
            self._set_state("control_status", "returned")

    def _post_fsync_fence(self, command):
        """DTTL-05 final revalidation plus the AR2.3 arming fence: after the
        CONSUMED fsync the still-required preconditions are re-read from the
        verified in-memory view only — installed context, revocation latch,
        validity window, identity and the already-validated clock conditions.
        No file parse, ACL or new persistent write ever happens here."""
        now = _now_utc()
        mirror_blockers, _ = self._validate_admission_mirror(command, now)
        violations = list(mirror_blockers)
        if self._stop_event.is_set() or not self._running:
            violations.append("GATE_CLOSED")
        context = self._arm_context
        if context is None or not self._arming_effective_now(now):
            violations.append("GATE_CLOSED")
        else:
            if context.expired(now, time.monotonic()):
                violations.append("ARMING_EXPIRED")
            if self._effective_params_sha256 != context.target_params_sha256:
                violations.append("EFFECTIVE_HASH_SWITCHED")
        if command["binding"]["session_epoch"] != self._session_epoch \
                or self._dispatch_slot_epoch != self._generation:
            violations.append("GENERATION_EPOCH_CHANGED")
        if self._event_gap_latched or self._window_conflict_latched \
                or self._arming_revoked:
            violations.append("LATCHED_STATE")
        return violations

    def _release_slot(self):
        with self._state_lock:
            self._dispatch_slot = None
            self.counters["unresolved_dispatch_count"] = 0

    def _invoke_native(self, command):
        # last-moment admission fence (v5 lesson): stop/epoch re-checked at the wire
        if self._stop_event.is_set():
            raise ContractError("stopped_before_call")
        if command["kind"] == "SUBMIT":
            request = build_client_request(
                {**command["leg"], "price_tick": command["price_tick"]},
                command["binding"], self._resolve_investor_handle(),
                strategy_id=self.strategy_id, order_type=command["order_type"])
            self.counters["native_send_attempts"] += 1
            self._set_state("native_send_attempts", self.counters["native_send_attempts"])
            result = _native.sendOrder(**request)
            return self._classify(result)
        self.counters["native_cancel_attempts"] += 1
        self._set_state("native_cancel_attempts", self.counters["native_cancel_attempts"])
        result = _native.cancelOrder(OrderID=command["order_id"])
        return self._classify(result)

    @staticmethod
    def _classify(result):
        if type(result) is int:
            if result >= 0:
                return "LOCAL_ACK", result, None
            if result == -1:
                return "LOCAL_REJECTED", None, None
            return "INVALID_RETURN", None, "negative_non_minus_one"
        if isinstance(result, bool) or result is None:
            return "INVALID_RETURN", None, "invalid_native_return"
        return "INVALID_RETURN", None, "undecodable_native_return"

    def _resolve_investor_handle(self):
        selected = self.params_map.investor_id.strip()
        _require(selected != "", "investor selection missing")
        investors = _native.getInvestorList()
        if isinstance(investors, dict):
            investors = [investors] if "InvestorID" in investors or "investor_id" in investors \
                else list(investors.values())
        if not isinstance(investors, (list, tuple)) or len(investors) != 1:
            raise ContractError("investor_scope_ambiguous")
        identity = investors[0]
        for name in ("InvestorID", "investor_id"):
            value = identity.get(name) if isinstance(identity, dict) else getattr(identity, name, None)
            if isinstance(value, str) and value.strip():
                _require(value.strip() == selected, "investor_selection_mismatch")
                return identity  # in-memory framework handle; never serialized
        raise ContractError("investor_handle_unavailable")

    # -- receipts --------------------------------------------------------------

    def _record_receipt(self, command, stage, result_kind, value, error_code):
        # N4 client_dispatch.build_receipt semantics (frozen): a CONSUMED
        # record is fsynced before the native call, so its result is not yet
        # known — it carries the OUTCOME_PENDING marker instead of a null
        # result kind ("called stages require a result kind" on /results).
        if stage == "CONSUMED" and result_kind is None:
            result_kind, error_code = "EXCEPTION", "OUTCOME_PENDING"
        record = {
            "receipt_id": "rcp-" + uuid.uuid4().hex[:16],
            "command_id": command["command_id"], "attempt_id": command["attempt_id"],
            "binding": command["binding"], "generation": self._generation,
            "stage": stage, "local_sequence": 0, "recorded_at": _iso(_now_utc()),
            "result_kind": result_kind, "result_value": value, "error_code": error_code,
            "request_sha256": sha256_hex(canonical_dumps(command)),
            "previous_receipt_sha256": None}
        if self._stores is not None:
            with self._state_lock:
                counter = self._stores.receipts.execute(
                    "SELECT COALESCE(MAX(local_sequence),0)+1 FROM receipts").fetchone()[0]
            record["local_sequence"] = counter
            previous, body_hash = self._stores.append_receipt(record)
            record["previous_receipt_sha256"] = previous
        else:
            self._memory_receipt_sequence += 1
            record["local_sequence"] = self._memory_receipt_sequence
        if len(self._result_uploads) < NOTIFY_QUEUE_MAX:
            self._result_uploads.append(dict(record))
        return record

    def _upload_receipt(self, record):
        packet = self._envelope({"receipt": record}, "ResultsRequest")
        self._post("/execution/v1/results", packet)

    # -- framework callbacks ----------------------------------------------------

    def on_tick(self, tick):
        self._bind_callback_thread("on_tick")
        self._assert_callback_thread()
        self._refresh_capture(quote=tick)
        self._pump_once()
        self.update_status_bar()

    def on_contract_status(self, status):
        self._bind_callback_thread("on_contract_status")
        self._assert_callback_thread()
        self._refresh_capture(status=status)
        self._pump_once()
        self.update_status_bar()

    def on_order(self, order):
        self._bind_callback_thread("on_order")
        self._capture_callback("order", order)
        self._pump_once()

    def on_order_trade(self, order):
        self._bind_callback_thread("on_order_trade")
        self._capture_callback("order", order)
        self._pump_once()

    def on_order_cancel(self, order):
        self._bind_callback_thread("on_order_cancel")
        self._capture_callback("cancel", order)
        self._pump_once()

    def on_cancel(self, order):
        self._bind_callback_thread("on_cancel")
        self._capture_callback("cancel", order)
        self._pump_once()

    def on_trade(self, trade):
        self._bind_callback_thread("on_trade")
        self._capture_callback("trade", trade)
        self._pump_once()

    def on_error(self, error):
        self._bind_callback_thread("on_error")
        self._capture_callback("error", error)

    # -- helpers ----------------------------------------------------------------

    def _set_state(self, name, value):
        try:
            setattr(self.state_map, name, value)
        except (AttributeError, TypeError, ValueError):
            pass

    def _close_gate(self, reason):
        with self._admission:
            if self.params_map.enable_trading:
                try:
                    self.params_map.enable_trading = False
                except (AttributeError, TypeError, ValueError):
                    pass
            # AR2.3: closing the gate also drops any installed ArmContext and
            # returns the effective hash to the pairing hash; the state
            # machine transition itself is driven by _revoke_arming.
            self._arm_context = None
            self._effective_params_sha256 = self._params_sha256
            self._set_state("enable_trading_effective", False)
            self._set_state("execution_status", f"gate_closed:{reason}")
        # OV-F05 (plan §5.3): best-effort GATE_CLOSED control report so the
        # service can move REVOKE_PENDING -> DISABLED_CONFIRMED on evidence.
        self._report_control("GATE_CLOSED")


__all__ = ["AKExecutionBridge", "ADAPTER_VERSION", "ContractError", "PollBackoffRequest",
           "validate_loopback_url", "build_client_request", "validate_dispatch_command",
           "validate_control_response", "ArmContext", "STORE_LAYOUT_VERSION",
           "STORE_ROOT_NAME", "ARM_STATE_DISARMED", "ARM_STATE_PREPARED_DISABLED",
           "ARM_STATE_ARM_READY", "ARM_STATE_ARM_PENDING", "ARM_STATE_ARMED",
           "ARM_STATE_ARMED_SEND_EXHAUSTED", "ARM_STATE_REVOKE_PENDING",
           "ARM_STATE_DISARMED_TERMINAL", "ARM_STATE_RESTART_REQUIRED",
           "ARMING_OVERRIDE_FIELDS"]
