from __future__ import annotations

from datetime import datetime, timezone

from ak_trading.infiny_bridge.protocol import normalize_payload
from ak_trading.infiny_bridge.service import BridgeService


def option_payload():
    return {
        "schema_version": 2, "exchange": "SHFE", "instrument_id": "rb2610C3100", "batch_id": "option",
        "ticks": [{"time": "2026-09-10T10:00:00+08:00", "last": 30, "bid1": None, "ask1": 31,
                   "bid2": 29, "ask2": 32, "bid_volume1": None, "ask_volume1": 2,
                   "bid5": 26, "ask5": None, "volume": 120, "open_interest": 800,
                   "turnover": 36000, "pre_settlement": 28, "upper_limit": 1.7976931348623157e308}],
        "quote": {"last": 30, "bid1": None, "ask1": 31},
        "contract": {"status": "ready", "asset_class": "option", "strike": 3100, "underlying": "rb2610",
                     "option_type": "call", "multiplier": 10, "price_tick": 1, "product_id": "rb",
                     "expiry": "20260924"},
        "account": {"status": "ready", "available": 0, "balance": 0, "commission": 7, "margin": 100},
        "active_orders": {"status": "unknown", "count": None, "observed_count": 1, "coverage": "callbacks_since_start"},
        "orders": [{"order_id": "7", "status": "pending", "total_volume": 1, "traded_volume": 0,
                    "instrument_id": "rb2610C3100", "exchange": "SHFE", "memo": "secret-account"}],
        "trades": [{"trade_id": "t1", "price": 30, "volume": 1, "account_id": "secret-account"}],
        "capabilities": {"rates_query": "not_exposed_in_documented_api", "available_values": {}},
    }


def test_option_sparse_depth_and_terminal_sentinels_preserve_real_values():
    result = normalize_payload(option_payload(), allowed_symbols=["SHFE.rb2610C3100"],
                               now=datetime(2026, 9, 10, 2, tzinfo=timezone.utc))
    tick = result.payload["ticks"][0]
    assert tick["bid1"] is None and tick["last"] == 30
    assert tick["bid5"] == 26 and tick["ask_volume1"] == 2
    assert tick["upper_limit"] is None
    assert tick["open_interest"] == 800
    assert result.payload["contract"]["asset_class"] == "option"
    assert result.payload["contract"]["strike"] == 3100
    assert result.payload["account"]["commission"] == 7
    assert result.payload["active_orders"]["count"] is None
    assert result.payload["orders"][0]["order_id"] == "7"
    assert "memo" not in result.payload["orders"][0]
    assert "account_id" not in result.payload["trades"][0]


def test_stale_cached_quote_on_callback_heartbeat_is_flagged_with_its_own_time():
    from datetime import datetime, timezone as tz
    from ak_trading.infiny_bridge.service import BridgeService
    service = BridgeService(allowed_symbols=["SHFE.rb2610C3100"],
                            clock=type("C", (), {"now": lambda self: datetime(2026, 9, 9, 1, tzinfo=tz.utc), "monotonic": lambda self: 0.0})())
    try:
        payload = option_payload()
        payload["ticks"] = []
        payload["quote"] = {"status": "available", "last": 30, "bid1": None, "ask1": 31, "time": "2026-09-09T08:00:00+08:00"}
        body = service.heartbeat(payload)
        assert body == {"signal": "hold", "reason": "stale_quote"}
        receipt = service.status()["recent_receipt"]
        assert receipt["snapshot"]["quote"]["time"] == "2026-09-09T08:00:00+08:00"
    finally:
        service.close()


def test_per_contract_status_keeps_futures_and_option_independent():
    service = BridgeService(allowed_symbols=["SHFE.rb2610", "SHFE.rb2610C3100"])
    try:
        option = option_payload()
        option["ticks"] = []
        service.heartbeat(option)
        future = {"schema_version": 2, "exchange": "SHFE", "instrument_id": "rb2610", "ticks": []}
        service.heartbeat(future)
        contracts = service.status()["contracts"]
        assert set(contracts) == {"SHFE.rb2610", "SHFE.rb2610C3100"}
        assert contracts["SHFE.rb2610C3100"]["snapshot"]["contract"]["option_type"] == "call"
        assert contracts["SHFE.rb2610C3100"]["snapshot"]["orders"][0]["order_id"] == "7"
    finally:
        service.close()
