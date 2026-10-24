"""Daily quantitative candidates for a strictly non-executing observer bridge."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import math
import re
from pathlib import Path
from typing import Mapping, Sequence

from ak_trading.futures.spec import ClockStatus, StrategyContext
from ak_trading.futures.strategies import default_registry
from ak_trading.futures.structures import build_structure_frame
from ak_trading.probability.artifacts import load_frozen_input

CST = timezone(timedelta(hours=8))


def _finite(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _day(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").date() if len(value) == 8 else date.fromisoformat(value)
    except ValueError:
        return None


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.utcoffset() is not None else None
    except ValueError:
        return None


def _historical_day(bar):
    return bar.trading_day if bar.trading_day is not None else bar.ts.astimezone(CST).date()


class DailyObserver:
    def __init__(self, history_bundle: Path | None, allowed_symbols: Sequence[str], strategy: str = "ma_trend",
                 expected_history_date: date | None = None):
        if strategy not in {"ma_trend", "donchian_trend", "zscore_reversion"}:
            raise ValueError("unsupported single-contract observation strategy")
        self.allowed = frozenset(allowed_symbols)
        if not self.allowed:
            raise ValueError("at least one allowed concrete symbol is required")
        self.strategy = default_registry()[strategy]
        self.expected_history_date = expected_history_date
        self.bundle = None
        self.history_hash = None
        self.bars = {}
        self.identities = {}
        self.source_hashes = {}
        self.observation_only_symbols = frozenset(symbol for symbol in self.allowed if not re.fullmatch(r"[A-Z]+\.[A-Za-z]+[0-9]{3,4}", symbol))
        if history_bundle is not None:
            self.bundle, self.history_hash = load_frozen_input(Path(history_bundle))
            self.source_hashes = self.bundle.manifest.get("source_hashes") or {}
            from ak_trading.futures.scenario_backtest import _select_contract
            for symbol in sorted(self.allowed - self.observation_only_symbols):
                _, product, instance, bars = _select_contract(self.bundle, symbol)
                self.identities[symbol] = (product, instance)
                self.bars[symbol] = bars

    def status(self) -> dict:
        return {
            "strategy": self.strategy.spec.strategy_id,
            "order_execution_enabled": False,
            "history_loaded": self.bundle is not None,
            "history_manifest_sha256": self.history_hash,
            "history_dataset_kind": self.bundle.manifest.get("dataset_kind") if self.bundle else None,
            "history_provenance": "hashed_export_claims_not_broker_certification" if self.source_hashes else "missing",
            "source_hashes": dict(self.source_hashes),
            "history_through": self.expected_history_date.isoformat() if self.expected_history_date else None,
            "required_completed_bars": self.strategy.spec.required_bars,
            "timeframe_seconds": 86400,
            "allowed_symbols": sorted(self.allowed),
            "observation_only_symbols": sorted(self.observation_only_symbols),
        }

    def evaluate(self, payload: Mapping) -> dict:
        def hold(reason, **details):
            return {"candidate_signal": "hold", "reason": reason, "observation_only": True,
                    "execution_authorized": False, **details}
        if not isinstance(payload, Mapping):
            return hold("payload_invalid")
        symbol = f"{payload.get('exchange')}.{payload.get('instrument_id')}"
        if symbol not in self.allowed:
            return hold("symbol_not_allowed")
        if symbol in self.observation_only_symbols:
            return hold("option_observation_only")
        if self.bundle is None:
            return hold("history_not_loaded")
        if not self.source_hashes:
            return hold("history_provenance_missing")
        if self.expected_history_date is None:
            return hold("history_reference_date_missing")
        ticks = payload.get("ticks")
        if not isinstance(ticks, (list, tuple)) or not ticks or not all(isinstance(t, Mapping) for t in ticks):
            return hold("ticks_missing")
        tick = ticks[-1]
        as_of = _timestamp(tick.get("time"))
        if as_of is None:
            return hold("tick_timestamp_unknown")
        trading_day = _day(payload.get("trading_day")) or _day(tick.get("trading_day"))
        if trading_day is None or self.expected_history_date >= trading_day:
            return hold("trading_day_or_history_cutoff_unknown")
        runtime = payload.get("runtime")
        if not isinstance(runtime, Mapping) or runtime.get("enable_trading") is not False or runtime.get("observer_only") is not True:
            return hold("runtime_observer_not_verified")
        position = payload.get("position")
        if not isinstance(position, Mapping) or position.get("status") != "ready":
            return hold("position_unknown")
        long_side, short_side = position.get("long"), position.get("short")
        if not isinstance(long_side, Mapping) or not isinstance(short_side, Mapping):
            return hold("position_unknown")
        long_count, short_count, net = (_finite(long_side.get("total")), _finite(short_side.get("total")), _finite(position.get("net")))
        if any(v is None or not v.is_integer() or v < 0 for v in (long_count, short_count)) or net is None or net != long_count-short_count:
            return hold("position_invalid")
        if long_count or short_count:
            return hold("existing_position_requires_exit_policy")
        orders = payload.get("active_orders")
        if not isinstance(orders, Mapping) or orders.get("status") != "ready" or type(orders.get("count")) is not int or orders["count"] != 0:
            return hold("active_orders_unknown_or_present")
        account = payload.get("account")
        if not isinstance(account, Mapping) or account.get("status") != "ready":
            return hold("account_unknown")
        available = _finite(account.get("available"))
        if available is None:
            return hold("account_available_unknown")
        if available <= 0:
            return hold("no_available_funds")
        contract = payload.get("contract") or {}
        quote = payload.get("quote") or {}
        if not isinstance(contract, Mapping) or not isinstance(quote, Mapping):
            return hold("quote_invalid")
        for part in (payload, quote, tick):
            for flag in ("auction", "is_auction", "in_auction", "at_auction", "limit_up", "limit_down", "at_limit", "limit_state"):
                if part.get(flag) is True:
                    return hold("market_flag_blocked")
        price, bid, ask = (_finite(tick.get(key)) for key in ("last", "bid1", "ask1"))
        upper, lower, step = (_finite(tick.get(key) if tick.get(key) is not None else contract.get(key)) for key in ("upper_limit", "lower_limit", "price_tick"))
        if any(v is None or v <= 0 for v in (price, bid, ask, upper, lower, step)):
            return hold("quote_limits_or_tick_unknown")
        if not lower < price < upper or bid > ask or not lower <= bid <= upper or not lower <= ask <= upper:
            return hold("quote_at_limit_or_invalid")
        if ask-bid > 10*step:
            return hold("spread_excessive")
        if payload.get("market_status") not in {"continuous", "trading", None}:
            return hold("market_not_continuous")
        if payload.get("market_status") is None:
            return hold("market_status_unknown")
        completed = tuple(bar for bar in self.bars[symbol] if _historical_day(bar) <= self.expected_history_date)
        if not completed or _historical_day(completed[-1]) != self.expected_history_date:
            return hold("historical_bars_stale")
        if len(completed) < self.strategy.spec.required_bars:
            return hold("completed_history_insufficient")
        for bar in completed:
            if bar.available_at is None or bar.available_at.utcoffset() is None or bar.available_at > as_of:
                return hold("historical_availability_unknown")
        product, instance = self.identities[symbol]
        declaration = next(row for row in self.bundle.contracts if row.get("instance_id") == instance)
        expiry = datetime.fromisoformat(declaration["expiry_at"])
        if expiry.astimezone(CST).date() <= trading_day:
            return hold("contract_expired_or_delivery_day")
        window = completed[-max(61, self.strategy.spec.required_bars+1):]
        frame = build_structure_frame(window)
        context = StrategyContext(
            self.strategy.spec.strategy_id, product, instance, symbol, None, ClockStatus.OBSERVATION_ONLY,
            str(window[-1].datetime_nano), tuple(window), frame, {}, expiry_at=expiry,
        )
        result = self.strategy.evaluate(context, None)
        direction = result.diagnostics.get("candidate_direction")
        if direction not in (-1, 0, 1) or isinstance(direction, bool):
            return hold("quantitative_signal_unavailable", strategy=self.strategy.spec.strategy_id)
        return {
            "candidate_signal": "buy" if direction == 1 else "sell" if direction == -1 else "hold",
            "reason": "daily_quantitative_candidate", "strategy": self.strategy.spec.strategy_id,
            "signal_key": f"{instance}|{self.strategy.spec.strategy_id}|{window[-1].datetime_nano}",
            "history_manifest_sha256": self.history_hash, "completed_bars": len(completed),
            "history_through": self.expected_history_date.isoformat(), "observation_only": True,
            "execution_authorized": False, "historical_clock_status": "observation_only",
            "history_provenance": "hashed_export_claims_not_broker_certification",
            "candidate_as_of": as_of.isoformat(),
        }
