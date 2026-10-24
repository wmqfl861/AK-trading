from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from ak_trading.futures.io import bars_rows_from_bars, publish_input_bundle
from ak_trading.futures.spec import ContractBar
from ak_trading.infiny_bridge.decision import DailyObserver


def payload():
    return {"exchange": "SHFE", "instrument_id": "rb2610", "trading_day": "20260909", "ticks": [
        {"time": "2026-09-09T09:10:00+08:00", "last": 4000, "bid1": 3999, "ask1": 4001, "volume": 10,
         "upper_limit": 4500, "lower_limit": 3500, "price_tick": 1}],
        "position": {"status": "ready", "long": {"total": 0}, "short": {"total": 0}, "net": 0},
        "account": {"status": "ready", "available": 0, "balance": 0},
        "active_orders": {"status": "ready", "count": 0},
        "runtime": {"observer_only": True, "enable_trading": False}, "market_status": "continuous"}


def make_history(path):
    bars = []
    start = datetime(2026, 6, 21, tzinfo=timezone(timedelta(hours=8)))
    for i in range(80):
        ts = start + timedelta(days=i)
        price = Decimal(3000 + i * 4)
        bars.append(ContractBar("SHFE.rb2610", 86400, int(ts.timestamp()) * 10**9, ts, "test_history",
            trading_day=ts.date(), available_at=datetime(2026,9,8,8,tzinfo=timezone(timedelta(hours=8))), open=price, high=price+2, low=price-2, close=price+1, volume=Decimal(1000)))
    publish_input_bundle(path, dataset_kind="real_export", created_at="2026-09-08T16:00:00+08:00",
        bars_rows=bars_rows_from_bars(bars), contracts=[{"raw_symbol":"SHFE.rb2610","product":"SHFE.RB",
        "instance_id":"SHFE.rb2610|2026-10-15T15:00:00+08:00","expiry_at":"2026-10-15T15:00:00+08:00"}],
        metadata={}, availability={}, request_info={}, source_hashes={"fixture": "fixture-hash"}, read_only_transaction={}, clock_policy={})


def test_tick_batch_never_substitutes_for_missing_daily_history():
    observer = DailyObserver(None, ["SHFE.rb2610"])
    result = observer.evaluate(payload())
    assert result["candidate_signal"] == "hold"
    assert result["reason"] == "history_not_loaded"
    assert observer.status()["order_execution_enabled"] is False


def test_unfunded_account_is_a_blocker_not_an_order_test(tmp_path):
    history = tmp_path / "history"
    make_history(history)
    observer = DailyObserver(history, ["SHFE.rb2610"], expected_history_date=date(2026, 9, 8))
    result = observer.evaluate(payload())
    assert result["candidate_signal"] == "hold"
    assert result["reason"] == "no_available_funds"


def test_completed_history_uses_existing_quant_strategy_without_order_execution(tmp_path):
    history = tmp_path / "history"
    make_history(history)
    observer = DailyObserver(history, ["SHFE.rb2610"], expected_history_date=date(2026, 9, 8))
    request = payload()
    request["account"]["available"] = 100000
    result = observer.evaluate(request)
    assert result["candidate_signal"] == "buy"
    assert result["strategy"] == "ma_trend"
    assert result["observation_only"] is True
    assert result["execution_authorized"] is False


def test_position_or_pending_orders_never_become_opening_signals(tmp_path):
    history = tmp_path / "history"
    make_history(history)
    observer = DailyObserver(history, ["SHFE.rb2610"], expected_history_date=date(2026, 9, 8))
    request = payload()
    request["position"]["long"]["total"] = 1
    request["position"]["net"] = 1
    assert observer.evaluate(request)["reason"] == "existing_position_requires_exit_policy"
    request = payload()
    request["active_orders"]["count"] = 1
    assert observer.evaluate(request)["candidate_signal"] == "hold"


def test_unknown_auction_and_daily_history_stale_are_blocked(tmp_path):
    history = tmp_path / "history"
    make_history(history)
    observer = DailyObserver(history, ["SHFE.rb2610"], expected_history_date=date(2026, 9, 8))
    request = payload()
    request["account"]["available"] = 100000
    request["market_status"] = "auction"
    assert observer.evaluate(request)["reason"] == "market_not_continuous"
    request["market_status"] = None
    assert observer.evaluate(request)["reason"] == "market_status_unknown"
    request["market_status"] = "continuous"
    observer.expected_history_date = date(2026, 9, 9)
    assert observer.evaluate(request)["reason"] == "trading_day_or_history_cutoff_unknown"


def test_same_day_later_publication_and_boolean_order_count_are_blocked(tmp_path):
    from dataclasses import replace
    history = tmp_path / "history"
    make_history(history)
    observer = DailyObserver(history, ["SHFE.rb2610"], expected_history_date=date(2026, 9, 8))
    request = payload()
    request["account"]["available"] = 100000
    for value in (False, 0.0):
        request["active_orders"]["count"] = value
        assert observer.evaluate(request)["reason"] == "active_orders_unknown_or_present"
    request["active_orders"]["count"] = 0
    bars = observer.bars["SHFE.rb2610"]
    observer.bars["SHFE.rb2610"] = (*bars[:-1], replace(bars[-1], available_at=datetime.fromisoformat("2026-09-09T16:00:00+08:00")))
    assert observer.evaluate(request)["reason"] == "historical_availability_unknown"


def test_market_flags_are_checked_inside_decision_boundary(tmp_path):
    history = tmp_path / "history"
    make_history(history)
    observer = DailyObserver(history, ["SHFE.rb2610"], expected_history_date=date(2026, 9, 8))
    for flag in ("auction", "limit_up", "limit_down", "at_limit"):
        request = payload()
        request["account"]["available"] = 100000
        request[flag] = True
        assert observer.evaluate(request)["reason"] == "market_flag_blocked"


def test_missing_source_metadata_is_not_broker_history_proof(tmp_path):
    history = tmp_path / "history"
    make_history(history)
    observer = DailyObserver(history, ["SHFE.rb2610"], expected_history_date=date(2026, 9, 8))
    observer.source_hashes = {}
    assert observer.evaluate(payload())["reason"] == "history_provenance_missing"
