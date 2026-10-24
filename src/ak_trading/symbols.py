from __future__ import annotations

from typing import Any

_MAIN_INS_CLASSES = frozenset({"CONT", "CONTINUOUS"})
_OPTION_INS_CLASSES = frozenset({"OPTION", "FUTURE_OPTION", "FUTURE_COMBINE"})


def require_concrete_symbol(symbol: str) -> str:
    """Reject continuous-symbol syntax where a concrete contract is required."""
    value = (symbol or "").strip()
    if not value:
        raise ValueError("symbol is empty")
    if value.startswith("KQ.") or "@" in value:
        raise ValueError("a concrete delivery-month contract is required")
    exchange, separator, instrument = value.partition(".")
    if not separator or not exchange or not instrument:
        raise ValueError("symbol must use EXCHANGE.instrument format")
    return value


def resolve_trade_symbol(quote: Any, configured_symbol: str) -> str:
    """Map a quoteable symbol to a concrete futures delivery-month contract.

    - Main/continuous quotes (KQ.m@... or CONT): use non-empty underlying_symbol
    - FUTURE quotes: use the concrete symbol itself when not expired
    - Options and other types: rejected (never silently trade the underlying)
    """
    symbol = (configured_symbol or "").strip()
    if not symbol:
        raise ValueError("configured_symbol is empty")

    ins_class = str(getattr(quote, "ins_class", "") or "").upper()
    expired = bool(getattr(quote, "expired", False))
    underlying = str(getattr(quote, "underlying_symbol", "") or "").strip()
    instrument_id = str(getattr(quote, "instrument_id", "") or symbol).strip()

    if expired:
        raise ValueError(f"contract expired: {instrument_id or symbol}")

    is_main = symbol.startswith("KQ.m@") or ins_class in _MAIN_INS_CLASSES
    if is_main:
        if not underlying:
            raise ValueError(
                f"main-contract quote has no underlying_symbol yet: {symbol}"
            )
        return underlying

    if ins_class == "FUTURE":
        return instrument_id or symbol

    if ins_class in _OPTION_INS_CLASSES or "OPTION" in ins_class:
        raise ValueError(
            f"options are not supported for trade resolution: "
            f"ins_class={ins_class!r} symbol={symbol!r}"
        )

    raise ValueError(
        f"unsupported trade symbol type ins_class={ins_class!r} symbol={symbol!r}; "
        "use KQ.m@EXCHANGE.product or a non-expired FUTURE contract"
    )
