"""Pure SDK mapping and a permanently disabled native adapter for this phase.

There is deliberately no INFINIGO import or platform discovery here. Possessing
an SDK object, a mode string, or simulated proof never grants native capability.
"""
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation, localcontext
import math
from types import MappingProxyType
from typing import Protocol

from . import contracts as c


class NativeExecutionError(ValueError):
    """Safe, machine-readable rejection before any native attribute access."""

    def __init__(self, *blockers: str):
        self.blockers = tuple(blockers)
        super().__init__("|".join(self.blockers))


class NativePort(Protocol):
    def send_order(self, request: Mapping[str, object]) -> object:
        raise NotImplementedError

    def cancel_order(self, order_id: int) -> object:
        raise NotImplementedError


def build_native_request(leg: c.Leg, binding: c.Binding, investor_handle: object, *,
                         order_type: c.OrderType,
                         price_tick: Decimal,
                         verified_hedge_flags: tuple[str, ...]) -> Mapping[str, object]:
    """Map a frozen leg; absent order/tick/hedge evidence fails closed.

    Investor is an explicitly bound in-memory handle, never a label selected by
    this function. The keyword-only evidence is supplied from the original
    intent and latest contract by the independent assembler.
    """
    if type(leg) is not c.Leg or type(binding) is not c.Binding:
        raise c.ContractError("expected typed Leg and Binding")
    if type(order_type) is not c.OrderType:
        raise NativeExecutionError("ORDER_TYPE_UNSUPPORTED")
    if investor_handle is None or type(investor_handle) is bool:
        raise NativeExecutionError("ACCOUNT_MISMATCH")
    if (type(verified_hedge_flags) is not tuple or
            any(type(flag) is not str for flag in verified_hedge_flags) or
            binding.hedge_flag not in verified_hedge_flags):
        raise NativeExecutionError("HEDGE_UNVERIFIED")
    split_exchange = binding.exchange in ("SHFE", "INE")
    if leg.offset in (c.Offset.CLOSE_TODAY, c.Offset.CLOSE_YESTERDAY) and not split_exchange:
        raise NativeExecutionError("UNSUPPORTED_CLOSE_POLICY")
    if leg.offset is c.Offset.CLOSE and split_exchange:
        raise NativeExecutionError("UNSUPPORTED_CLOSE_POLICY")
    if (type(price_tick) is not Decimal or not price_tick.is_finite() or price_tick <= 0):
        raise NativeExecutionError("CONTRACT_MISSING")
    try:
        with localcontext() as arithmetic:
            arithmetic.prec = 512
            converted = float(leg.price)
            if (not math.isfinite(converted) or leg.price % price_tick != 0 or
                    Decimal(str(converted)) != leg.price or
                    Decimal(str(converted)) % price_tick != 0):
                raise NativeExecutionError("TICK_MALIGNED")
    except (OverflowError, InvalidOperation) as exc:
        raise NativeExecutionError("TICK_MALIGNED") from exc
    return MappingProxyType({
        "StrategyID": binding.strategy_id,
        "Exchange": binding.exchange,
        "InstrumentID": binding.instrument_id,
        "Volume": leg.volume,
        "Price": converted,
        "Direction": {c.Direction.BUY: "0", c.Direction.SELL: "1"}[leg.direction],
        "OrderType": {c.OrderType.GFD: "0", c.OrderType.FAK: "1"}[order_type],
        "Investor": investor_handle,
        "HedgeFlag": binding.hedge_flag,
        "Offset": {c.Offset.OPEN: "0", c.Offset.CLOSE: "1",
                   c.Offset.CLOSE_YESTERDAY: "1", c.Offset.CLOSE_TODAY: "3"}[leg.offset],
        "Market": False,
        "Memo": leg.leg_id,
    })


class NativeAdapter:
    """Dependency-injected, inert SDK boundary. No native mode is enabled.

    Storing an opaque reference does not inspect it. Both operations reject
    before looking up sendOrder/cancelOrder, including when simulation is named.
    """
    full_account_reconciliation = c.NATIVE_FULL_ACCOUNT_RECONCILIATION
    submit_enabled = c.NATIVE_SUBMIT_ENABLED
    cancel_owned_enabled = c.NATIVE_CANCEL_OWNED_ENABLED

    def __init__(self, native: object = None, *, mode: c.ExecutionMode = c.ExecutionMode.DISABLED):
        self._native = native
        self._mode = mode

    def _reject(self):
        if self._mode is c.ExecutionMode.DISABLED:
            raise NativeExecutionError("MODE_DISABLED")
        raise NativeExecutionError("NATIVE_DISABLED", "ACCOUNT_RECONCILIATION_UNSUPPORTED")

    def send_order(self, request: Mapping[str, object]) -> object:
        self._reject()

    def cancel_order(self, order_id: int) -> object:
        self._reject()
