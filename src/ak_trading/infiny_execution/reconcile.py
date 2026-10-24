"""Strict SDK-field normalization; source claims never establish native ownership."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import re
from typing import TYPE_CHECKING

from . import contracts as c

if TYPE_CHECKING:
    from .ledger import IngestResult, Ledger


@dataclass(frozen=True, slots=True, kw_only=True)
class CallbackContext:
    event_id: str
    kind: c.EventKind
    received_at: datetime
    session_epoch: str
    source: c.Source
    account_key: str | None = None
    strategy_key: str | None = None
    simulation_scope: bool = False
    native_route_evidence_id: str | None = None

    def __post_init__(self):
        if type(self.source) is not c.Source or type(self.kind) is not c.EventKind:
            raise TypeError("typed callback source and kind required")
        if type(self.simulation_scope) is not bool:
            raise TypeError("simulation_scope requires bool")
        if self.simulation_scope and (self.source is not c.Source.SIMULATION or not self.account_key or not self.strategy_key):
            raise ValueError("simulation scope requires explicit simulation account and strategy")
        if self.native_route_evidence_id is not None:
            if self.source is not c.Source.PYTHONGO or self.simulation_scope:
                raise ValueError("native route evidence applies only to unsimulated pythongo callbacks")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}", self.native_route_evidence_id):
                raise ValueError("native route evidence id must be an opaque identifier")


# Only keys explicitly listed here may be read, including attribute-backed SDK objects.
_FIELDS = {
    "exchange": ("Exchange", "exchange"), "instrument_id": ("InstrumentID", "instrument_id"),
    "hedge_flag": ("Hedgeflag", "hedgeflag", "hedge_flag"),
    "order_id": ("OrderID", "order_id"), "order_sys_id": ("OrderSysID", "order_sys_id"),
    "front_id": ("FrontID", "front_id"), "session_id": ("SessionID", "session_id"),
    "memo": ("Memo", "memo"), "trade_id": ("TradeID", "trade_id"),
    "order_time": ("OrderTime", "order_time"), "trade_time": ("TradeTime", "trade_time"),
    "trading_day": ("TradingDay", "trading_day"), "status": ("Status", "status"),
    "total_volume": ("TotalVolume", "total_volume"), "traded_volume": ("TradedVolume", "traded_volume"),
    "cancel_volume": ("CancelVolume", "cancel_volume"), "price": ("Price", "price"),
    "volume": ("Volume", "volume"), "error_code": ("ErrorCode", "error_code"),
    "direction": ("Direction", "direction"), "offset": ("Offset", "offset"),
}
_STATUS = {
    "已报入未应答": c.OrderState.SUBMITTED_LOCAL, "交易员已报入": c.OrderState.SUBMITTED_LOCAL,
    "待报入": c.OrderState.SUBMITTED_LOCAL, "未成交": c.OrderState.WORKING,
    "部分成交": c.OrderState.PARTIAL, "全部成交": c.OrderState.FILLED,
    "已撤销": c.OrderState.CANCELLED, "部成部撤": c.OrderState.PARTIAL_CANCELLED,
    "投资经理驳回": c.OrderState.REJECTED, "交易员驳回": c.OrderState.REJECTED,
    "部分撤单": c.OrderState.WORKING, "部成部撤还在队列中": c.OrderState.WORKING,
}
_IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z")
_MEMO = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]*:(?:OPEN|CLOSE):(?:TD|YD|ALL|-):[0-9]+\Z")


def simulation_evidence_id(strategy_key: str) -> str:
    return "simulation:" + hashlib.sha256(strategy_key.encode("utf-8")).hexdigest()


def _read(raw, aliases):
    for key in aliases:
        try:
            if isinstance(raw, Mapping):
                if key in raw:
                    return raw[key]
            else:
                value = getattr(raw, key, None)
                if value is not None:
                    return value
        except (AttributeError, KeyError, TypeError, ValueError):
            # A broken preferred alias must not hide a readable lower-case one.
            continue
    return None


def _identifier(value, *, compact=False):
    if type(value) is not str:
        return None
    value = "".join(value.split()) if compact else value.strip()
    return value if _IDENT.fullmatch(value) else None


def _integer(value, minimum=0):
    if type(value) is str and re.fullmatch(r"[0-9]{1,19}", value.strip()):
        value = int(value)
    return value if type(value) is int and minimum <= value <= 2**63 - 1 else None


def _decimal(value):
    if type(value) not in (str, int, float, Decimal):
        return None
    try:
        value = Decimal(str(value))
        return value if value.is_finite() and value > 0 and len(value.as_tuple().digits) <= 64 and abs(value.as_tuple().exponent) <= 128 else None
    except (InvalidOperation, ValueError):
        return None


def _datetime(value):
    if type(value) is str:
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return value if type(value) is datetime and value.tzinfo is not None else None


def _date(value):
    if type(value) is str:
        try:
            value = date.fromisoformat(value)
        except ValueError:
            return None
    return value if type(value) is date else None


def normalize_event(raw: Mapping[str, object] | object, callback_context: CallbackContext) -> c.ExecutionEvent:
    if type(callback_context) is not CallbackContext:
        raise TypeError("CallbackContext required")
    ctx = callback_context
    values = {key: _read(raw, aliases) for key, aliases in _FIELDS.items()}
    for key in ("exchange", "instrument_id", "hedge_flag", "trade_id", "error_code"):
        values[key] = _identifier(values[key])
    values["order_sys_id"] = _identifier(values["order_sys_id"], compact=True)
    memo = _identifier(values["memo"])
    values["memo"] = memo if memo and _MEMO.fullmatch(memo) else None
    for key in ("order_id", "front_id", "session_id", "traded_volume", "cancel_volume"):
        values[key] = _integer(values[key])
    # Non-negative raw quantities, including legal zeros, are preserved verbatim;
    # business floors are enforced by the ledger gate, never by dropping evidence.
    for key in ("total_volume", "volume"):
        values[key] = _integer(values[key], 0)
    values["price"] = _decimal(values["price"])
    values["order_time"], values["trade_time"] = _datetime(values["order_time"]), _datetime(values["trade_time"])
    values["trading_day"] = _date(values["trading_day"])
    status = values["status"]
    values["status"] = status if type(status) is c.OrderState else _STATUS.get(status) if type(status) is str else None
    direction, offset = values["direction"], values["offset"]
    values["direction"] = direction if type(direction) is c.Direction else {"0": c.Direction.BUY, "1": c.Direction.SELL, "BUY": c.Direction.BUY, "SELL": c.Direction.SELL}.get(direction) if type(direction) is str else None
    values["offset"] = offset if type(offset) is c.Offset else {"0": c.Offset.OPEN, "1": c.Offset.CLOSE, "3": c.Offset.CLOSE_TODAY, **{x.value: x for x in c.Offset}}.get(offset) if type(offset) is str else None
    # SHFE/INE '1' closes yesterday. The exchange comes from the callback itself.
    if values["offset"] is c.Offset.CLOSE and values["exchange"] in ("SHFE", "INE"):
        values["offset"] = c.Offset.CLOSE_YESTERDAY
    evidence = None
    if ctx.simulation_scope:
        evidence = c.OwnershipEvidence(journal_leg_id=values["memo"] or "unassociated", matched_alias="exchange" if values["order_sys_id"] else "local",
                                       callback_account_verified=True, strategy_scope_verified=True,
                                       evidence_id=simulation_evidence_id(ctx.strategy_key))
    elif ctx.native_route_evidence_id is not None:
        # A claim to a registered native route; the ledger verifies the registry.
        evidence = c.OwnershipEvidence(journal_leg_id=values["memo"] or "unassociated", matched_alias="exchange" if values["order_sys_id"] else "local",
                                       callback_account_verified=True, strategy_scope_verified=True,
                                       evidence_id="native-route:" + ctx.native_route_evidence_id)
    # UNKNOWN preserves contradictory quantities and missing error codes verbatim;
    # the journal quarantines them atomically instead of inventing valid fields.
    return c.ExecutionEvent(event_id=ctx.event_id, kind=ctx.kind, received_at=ctx.received_at,
                            callback_session_epoch=ctx.session_epoch, account_key=ctx.account_key,
                            ownership=c.Ownership.UNKNOWN, ownership_evidence=evidence, **values)


class Reconciler:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger

    def ingest(self, event: c.ExecutionEvent) -> IngestResult:
        return self.ledger.ingest_event(event)
