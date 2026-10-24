from __future__ import annotations

from typing import Any, Iterable, Iterator


def order_full_symbol(order: Any) -> str:
    """Build EXCHANGE.instrument from a TqSdk Order/Trade-like object.

    Note: Order.instrument_id is exchange-local (e.g. rb2610), not full symbol.
    """
    exchange_id = str(getattr(order, "exchange_id", "") or "").strip()
    instrument_id = str(getattr(order, "instrument_id", "") or "").strip()
    if exchange_id and instrument_id:
        return f"{exchange_id}.{instrument_id}"
    return instrument_id or exchange_id


def iter_orders_for_symbol(orders: Any, symbol: str) -> Iterator[tuple[str, Any]]:
    target = (symbol or "").strip()
    if not target:
        return
    items: Iterable[tuple[str, Any]]
    if hasattr(orders, "items"):
        items = orders.items()
    else:
        return
    for order_id, order in items:
        if str(order_id).startswith("_"):
            continue
        if order_full_symbol(order) == target:
            yield str(order_id), order


def dump_orders_for_symbol(api: Any, symbol: str, *, printer=print) -> int:
    """Print matching orders; return how many were printed."""
    orders = api.get_order()
    count = 0
    for order_id, order in iter_orders_for_symbol(orders, symbol):
        printer(
            "order",
            order_id,
            "status",
            order.status,
            "direction",
            order.direction,
            "offset",
            order.offset,
            "volume_left",
            order.volume_left,
            "last_msg",
            order.last_msg,
            "symbol",
            order_full_symbol(order),
        )
        count += 1
    if count == 0:
        printer("order", "none_for_symbol", symbol)
    return count
