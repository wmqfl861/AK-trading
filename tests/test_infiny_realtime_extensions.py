from __future__ import annotations

import ast
import json
import sys
import threading
from datetime import datetime
from types import SimpleNamespace

import pytest

from test_infiny_adapter import ADAPTER_PATH, adapter, configure, tick, wait_for


PRIMARY = ("SHFE", "rb2610")
OPTION = ("SHFE", "rb2610C3100")
SENTINEL = 1.7976931348623157e308


@pytest.fixture
def observer(adapter, monkeypatch):
    """Public callbacks with fake native APIs and a captured network boundary."""
    bridge = configure(adapter.module, batch_size=1)
    sent = []
    monkeypatch.setattr(bridge, "_start_worker", lambda: None)
    monkeypatch.setattr(bridge, "_enqueue", lambda kind, payload, url: sent.append((kind, payload, url)) or True)
    bridge.on_start()
    sent.clear()
    yield SimpleNamespace(bridge=bridge, sent=sent, adapter=adapter)
    bridge.on_stop()


def order(**overrides):
    values = {
        "Exchange": "SHFE", "InstrumentID": "rb2610", "OrderID": 7, "OrderSysID": "sys-7",
        "Price": 4000, "Direction": "0", "Offset": "0", "TotalVolume": 4,
        "TradedVolume": 1, "CancelVolume": 0, "Status": "部分成交",
        "OrderTime": "20260909 09:10:00", "Memo": "private-memo", "AccountID": "private-account",
    }
    values.update(overrides)
    return values


def trade(**overrides):
    values = {
        "Exchange": "SHFE", "InstrumentID": "rb2610", "TradeID": "trade-1", "OrderID": 7,
        "OrderSysID": "sys-7", "Price": 4000, "Volume": 1, "Direction": "1", "Offset": "3",
        "TradeTime": "20260909 09:10:01", "Memo": "private-memo", "InvestorID": "private-investor",
    }
    values.update(overrides)
    return values


def last_payload(observer):
    return observer.sent[-1][1]


def add_option(observer):
    observer.bridge.on_stop()
    observer.bridge.set_params({"additional_symbols": "SHFE.rb2610C3100"})
    observer.bridge.on_start()
    observer.sent.clear()


def test_template_imports_only_stdlib_and_native_first(adapter):
    tree = ast.parse(ADAPTER_PATH.read_text(encoding="utf-8"))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(item.name.split(".")[0] for item in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    assert imports <= sys.stdlib_module_names | {"INFINIGO"}
    assert "INFINIGO" in imports
    assert adapter.module._native is adapter.native
    assert adapter.module.Params().additional_symbols == ""


def test_raw_sdk_depth_and_market_fields_serialize_through_on_tick(observer):
    data = {
        "Exchange": "SHFE", "InstrumentID": "rb2610", "LastPrice": 4000,
        "Datetime": datetime(2026, 9, 9, 9, 10), "TradingDay": "20260909", "Volume": 12,
        "OpenPrice": 3980, "HighPrice": 4020, "LowPrice": 3970, "PreClosePrice": 3990,
        "PreSettlementPrice": 3985, "Turnover": 480000.5, "OpenInterest": 500,
    }
    for level in range(1, 6):
        data.update({f"BidPrice{level}": 4000 - level, f"AskPrice{level}": 4000 + level,
                     f"BidVolume{level}": level * 2, f"AskVolume{level}": level * 3})
    observer.bridge.on_tick(data)
    encoded = last_payload(observer)["ticks"][0]
    assert encoded["time"] == "2026-09-09T09:10:00+08:00"
    assert encoded["trading_day"] == "20260909"
    for level in range(1, 6):
        assert encoded[f"bid{level}"] == 4000 - level
        assert encoded[f"ask{level}"] == 4000 + level
        assert encoded[f"bid_volume{level}"] == level * 2
        assert encoded[f"ask_volume{level}"] == level * 3
    assert {key: encoded[key] for key in ("open", "high", "low", "pre_close", "pre_settlement", "turnover", "open_interest")} == {
        "open": 3980, "high": 4020, "low": 3970, "pre_close": 3990, "pre_settlement": 3985,
        "turnover": 480000.5, "open_interest": 500,
    }
    json.dumps(last_payload(observer), allow_nan=False)


def test_installed_tick_properties_are_supported_and_missing_getters_stay_null(observer):
    # Mirror installed TickData's read-only property surface without importing SDK.
    class InstalledTick:
        exchange = property(lambda self: "SHFE")
        instrument_id = property(lambda self: "rb2610")
        datetime = property(lambda self: datetime(2026, 9, 9, 9, 10))
        last_price = property(lambda self: 4000)
        high_price = property(lambda self: 4020)
        bid_price5 = property(lambda self: 3995)
        ask_volume5 = property(lambda self: 8)

        @property
        def bid_price1(self):
            raise RuntimeError("not exposed")

    observer.bridge.on_tick(InstalledTick())
    encoded = last_payload(observer)["ticks"][0]
    assert encoded["last"] == 4000 and encoded["high"] == 4020
    assert encoded["bid5"] == 3995 and encoded["ask_volume5"] == 8
    assert encoded["bid1"] is None and encoded["ask1"] is None
    assert encoded["volume"] is None and encoded["open_interest"] is None


@pytest.mark.parametrize("unavailable", [None, 0, -1, float("inf"), SENTINEL, 1.79769e308])
def test_illiquid_option_last_survives_missing_or_sentinel_depth(observer, unavailable):
    add_option(observer)
    observer.bridge.on_tick(tick(instrument_id=OPTION[1], last_price=12, bid_price1=unavailable, ask_price1=unavailable))
    payload = last_payload(observer)
    assert (payload["exchange"], payload["instrument_id"]) == OPTION
    assert payload["ticks"][0]["last"] == 12
    assert payload["ticks"][0]["bid1"] is None and payload["ticks"][0]["ask1"] is None
    assert payload["quote"]["last"] == 12
    json.dumps(payload, allow_nan=False)


def test_invalid_last_or_calendar_time_skips_entire_tick(observer):
    for value in (None, 0, -2, SENTINEL, 1.79769e308, float("nan"), float("inf")):
        observer.bridge.on_tick(tick(last_price=value))
    observer.bridge.on_tick(tick(datetime=None, update_time="21:00:00", trading_day="20260910"))
    assert observer.sent == []


def test_all_quantities_are_integer_nonnegative_and_missing_is_not_zero(observer):
    for invalid in (-1, 1.5, True, SENTINEL, float("nan"), float("inf"), None):
        observer.bridge.on_tick(tick(volume=invalid, open_interest=invalid, bid_volume1=invalid, ask_volume5=invalid))
        encoded = last_payload(observer)["ticks"][0]
        assert all(encoded[name] is None for name in ("volume", "open_interest", "bid_volume1", "ask_volume5"))
    observer.bridge.on_tick(tick(volume=0.0, open_interest=2.0, bid_volume1=0, turnover=-1))
    encoded = last_payload(observer)["ticks"][0]
    assert type(encoded["volume"]) is int and encoded["volume"] == 0
    assert type(encoded["open_interest"]) is int and encoded["open_interest"] == 2
    assert encoded["bid_volume1"] == 0 and encoded["turnover"] is None


def test_optional_price_sentinels_do_not_contaminate_valid_tick(observer):
    observer.bridge.on_tick(tick(open_price=SENTINEL, high_price=0, low_price=-1,
                                 pre_close_price=float("nan"), pre_settlement_price=SENTINEL,
                                 upper_limit_price=SENTINEL, turnover=SENTINEL))
    encoded = last_payload(observer)["ticks"][0]
    assert encoded["last"] == 4000
    assert all(encoded[name] is None for name in ("open", "high", "low", "pre_close", "pre_settlement", "upper_limit", "turnover"))
    json.dumps(last_payload(observer), allow_nan=False)


def test_option_identifier_styles_restore_without_guessing_or_overriding(adapter):
    bridge = configure(adapter.module)
    for symbol in ("SHFE.rb2610C3100", "DCE.m2701-C-3000", "CZCE.TA701C5000",
                   "CFFEX.IO2609-P-4200", "INE.sc2610C600", "GFEX.si2610_C_10000", "SHFE.rb2610"):
        exchange, instrument = symbol.split(".")
        bridge.set_params({"exchange": exchange, "instrument_id": instrument})
        bridge.set_params({"exchange": "", "instrument_id": ""})
        assert (bridge.params_map.exchange, bridge.params_map.instrument_id) == (exchange, instrument)
        assert bridge.params_map.enable_trading is False


def test_invalid_subscription_expressions_are_rejected_before_any_subscription(adapter):
    bridge = configure(adapter.module)
    for invalid in ("rb*", "rb2610;ag2610", "rb2610$(id)", "rb2610\nC3100", "rb２６１０C3100",
                    "../rb2610", "rb2610-C3100", "SHFE.rb2610", "rb2610C3100?x", "rb2610-C-3000.5"):
        with pytest.raises(ValueError):
            bridge.set_params({"instrument_id": invalid})
    for invalid in ("*.rb2610", "SHFE.*", "SHFE.rb2610;", "DCE.m2701;;SHFE.rb2610C3100", "rb2610C3100", "SHFE.rb2610"):
        with pytest.raises(ValueError):
            bridge.set_params({"additional_symbols": invalid})
    assert adapter.events["sub"] == []
    assert bridge.params_map.instrument_id == "rb2610"


def test_subscription_limit_is_sixteen_including_primary(adapter, monkeypatch):
    bridge = configure(adapter.module)
    monkeypatch.setattr(bridge, "_start_worker", lambda: None)
    extra = [f"SHFE.rb2610C{3000 + index}" for index in range(16)]
    with pytest.raises(ValueError, match="too_many_symbols"):
        bridge.set_params({"additional_symbols": ";".join(extra)})
    bridge.set_params({"additional_symbols": ";".join(extra[:15])})
    bridge.on_start()
    assert len(adapter.events["sub"]) == 16
    assert bridge._request_queue.qsize() == 16
    assert bridge._request_queue.maxsize <= 100
    bridge.on_stop()
    assert len(adapter.events["unsub"]) == 16


def test_additional_params_are_preserved_and_cannot_change_while_running(observer):
    add_option(observer)
    observer.bridge.set_params({"batch_size": 3})
    assert observer.bridge.params_map.additional_symbols == "SHFE.rb2610C3100"
    with pytest.raises(ValueError, match="stop_observer"):
        observer.bridge.set_params({"additional_symbols": "DCE.m2701-C-3000"})
    assert observer.bridge.params_map.additional_symbols == "SHFE.rb2610C3100"


def test_invalid_nonempty_additional_symbols_prevent_start(adapter):
    bridge = configure(adapter.module)
    bridge.params_map.additional_symbols = "SHFE.*"
    bridge.on_start()
    assert bridge.state_map.connection_status == "invalid"
    assert adapter.events["sub"] == [] and bridge.worker_thread is None


def test_multisymbol_batches_positions_metadata_and_status_are_isolated(observer):
    data = observer.adapter.infini
    data.investors = [{"InvestorID": "only"}]
    data.positions["only"] = [
        {"InstrumentID": PRIMARY[1], "Exchange": "SHFE", "Direction": "多", "Position": 4, "YdPositionClose": 1},
        {"InstrumentID": OPTION[1], "Exchange": "SHFE", "Direction": "空", "Position": 2, "YdPositionClose": 1},
    ]
    data.instruments[PRIMARY] = {"ProductClass": "1", "PriceTick": 1}
    data.instruments[OPTION] = {"ProductClass": "2", "PriceTick": 0.5, "StrikePrice": 3100}
    add_option(observer)
    observer.bridge.set_params({"batch_size": 2})
    observer.bridge.on_contract_status({"Exchange": "SHFE", "InstrumentID": OPTION[1], "Status": "集合竞价撮合"})
    observer.sent.clear()
    observer.bridge.on_tick(tick(last_price=4000))
    observer.bridge.on_tick(tick(instrument_id=OPTION[1], last_price=21, bid_price1=20, ask_price1=22))
    assert observer.sent == []
    observer.bridge.on_tick(tick(last_price=4001))
    observer.bridge.on_tick(tick(instrument_id=OPTION[1], last_price=22, bid_price1=21, ask_price1=23))
    future, option = [entry[1] for entry in observer.sent]
    assert [value["last"] for value in future["ticks"]] == [4000, 4001]
    assert [value["last"] for value in option["ticks"]] == [21, 22]
    assert future["position"]["long"]["total"] == 4 and option["position"]["short"]["total"] == 2
    assert future["position"]["short"]["total"] is None and option["position"]["long"]["total"] is None
    assert future["position"]["net"] is None and option["position"]["net"] is None
    assert future["position"]["status"] == option["position"]["status"] == "observed"
    assert future["contract"]["asset_class"] == "future" and option["contract"]["asset_class"] == "option"
    assert future["ticks"][0]["price_tick"] == 1 and option["ticks"][0]["price_tick"] == 0.5
    assert future["market_status"] == "unknown" and option["market_status"] == "auction"
    assert future["sequence"] != option["sequence"]


def test_unknown_or_missing_callback_identity_is_never_assigned_to_primary(observer):
    for change in ({"instrument_id": "rb2610P3100"}, {"exchange": "DCE"}, {"exchange": ""}, {"instrument_id": ""}):
        observer.bridge.on_tick(tick(**change))
    observer.bridge.on_order(order(Exchange="DCE"))
    observer.bridge.on_trade(trade(InstrumentID=""))
    observer.bridge.on_error({"Exchange": "SHFE", "InstrumentID": "unsubscribed", "errCode": "4"})
    assert observer.sent == []


def test_stop_only_unsubscribes_owned_symbols_even_when_params_directly_change(observer):
    add_option(observer)
    observer.adapter.events["unsub"].clear()
    observer.bridge.params_map.instrument_id = "ag2610"
    observer.bridge.on_stop()
    assert {item[:2] for item in observer.adapter.events["unsub"]} == {PRIMARY, OPTION}
    observer.bridge.on_stop()
    assert len(observer.adapter.events["unsub"]) == 2


def test_failed_subscription_does_not_unsubscribe_unowned_contract(observer, monkeypatch):
    observer.bridge.on_stop()
    observer.bridge.set_params({"additional_symbols": "SHFE.rb2610C3100"})
    subscribe = observer.adapter.native.subMarketData

    def selective(**kwargs):
        if kwargs["InstrumentID"] == OPTION[1]:
            raise RuntimeError("fake failure")
        subscribe(**kwargs)

    monkeypatch.setattr(observer.adapter.native, "subMarketData", selective)
    observer.adapter.events["unsub"].clear()
    observer.sent.clear()
    observer.bridge.on_start()
    assert [item[1]["instrument_id"] for item in observer.sent] == [PRIMARY[1]]
    observer.bridge.on_stop()
    assert [item[:2] for item in observer.adapter.events["unsub"]] == [PRIMARY]


def test_contract_native_option_metadata_and_tick_limits_take_precedence(observer):
    observer.adapter.infini.instruments[PRIMARY] = {
        "ProductClass": "2", "VolumeMultiple": 10, "StrikePrice": 3100, "UnderlyingInstrID": "rb2610",
        "OptionsType": "2", "ProductID": "rb_o", "ExpireDate": "20260924", "PriceTick": 0.5,
        "MinLimitOrderVolume": 1, "MaxLimitOrderVolume": 100,
        "UpperLimitPrice": 5000, "LowerLimitPrice": 1000, "Memo": "private-memo",
    }
    observer.bridge.on_tick(tick(upper_limit_price=4500, lower_limit_price=3500))
    contract = last_payload(observer)["contract"]
    assert contract == {
        "status": "ready", "price_tick": 0.5, "upper_limit": 4500, "lower_limit": 3500, "expiry": "20260924",
        "asset_class": "option", "strike": 3100, "underlying": "rb2610", "option_type": "put",
        "multiplier": 10, "product_id": "rb_o", "min_limit_order_volume": 1, "max_limit_order_volume": 100,
    }
    assert "Memo" not in json.dumps(last_payload(observer))


def test_missing_contract_product_class_does_not_guess_option_from_identifier(observer):
    add_option(observer)
    observer.adapter.infini.instruments[OPTION] = {"PriceTick": SENTINEL, "VolumeMultiple": SENTINEL,
                                                "ExpireDate": "20269999", "StrikePrice": SENTINEL}
    observer.bridge.on_tick(tick(instrument_id=OPTION[1], upper_limit_price=SENTINEL, lower_limit_price=0))
    contract = last_payload(observer)["contract"]
    assert contract["asset_class"] is None and contract["status"] == "unknown"
    assert contract["expiry"] is None and contract["strike"] is None and contract["multiplier"] is None


def test_contract_identity_mismatch_cannot_poison_option_cache(observer):
    add_option(observer)
    observer.adapter.infini.instruments[OPTION] = {"Exchange": "SHFE", "InstrumentID": PRIMARY[1], "PriceTick": 10}
    observer.bridge.on_tick(tick(instrument_id=OPTION[1]))
    assert last_payload(observer)["contract"]["price_tick"] is None


def test_order_callbacks_merge_deduplicate_and_report_observed_window(observer):
    observer.bridge.on_order(order())
    first = last_payload(observer)
    observer.bridge.on_order_trade(order(TradedVolume=4, Status="全部成交"))
    observer.bridge.on_order_trade(order(TradedVolume=4, Status="全部成交"))
    payload = last_payload(observer)
    assert len(payload["orders"]) == 1 and payload["observed_orders"]["count"] == 1
    assert payload["orders"][0]["status"] == "filled" and payload["orders"][0]["pending"] is False
    assert first["orders"][0]["status"] == "partially_filled"  # queued snapshot remains immutable
    assert payload["orders"][0]["order_time"] == "2026-09-09T09:10:00+08:00"
    assert payload["active_orders"] == {"status": "unknown", "count": None, "observed_count": 1, "coverage": "callbacks_since_start"}
    assert all("private-" not in json.dumps(item[1]) for item in observer.sent)


def test_order_cancel_merges_partial_installed_properties_without_erasing_quantity(observer):
    observer.bridge.on_order(order())
    callback = SimpleNamespace(exchange="SHFE", instrument_id="rb2610", order_id=7,
                               order_sys_id="sys-7", cancel_volume=3, cancel_time="20260909 09:11:00")
    observer.bridge.on_order_cancel(callback)
    event = last_payload(observer)["orders"][0]
    assert event["total_volume"] == 4 and event["traded_volume"] == 1
    assert event["cancel_volume"] == 3 and event["status"] == "cancelled" and event["pending"] is False
    assert event["cancel_time"] == "2026-09-09T09:11:00+08:00"


def test_installed_order_statuses_distinguish_pending_terminal_and_unknown(observer):
    pending = ("未成交", "部分成交", "已报入未应答", "部分撤单还在队列中", "部成部撤还在队列中", "待报入", "交易员已报入")
    terminal = ("全部成交", "部成部撤", "已撤销", "投资经理驳回", "交易员驳回")
    for index, status in enumerate((*pending, *terminal, "投顾报单", "投资经理通过", "invented", "未知")):
        observer.bridge.on_order(order(OrderID=index, OrderSysID=f"sys-{index}", Status=status))
        event = last_payload(observer)["orders"][-1]
        expected = True if status in pending else False if status in terminal else None
        assert event["pending"] is expected
    assert last_payload(observer)["active_orders"]["count"] is None
    observer.bridge.on_order(order(OrderID=90, OrderSysID="sys-90", Status="未成交"))
    observer.bridge.on_order(order(OrderID=90, OrderSysID="sys-90", Status="unrecognized"))
    assert last_payload(observer)["orders"][-1]["status"] == "unknown"
    assert last_payload(observer)["orders"][-1]["pending"] is None


def test_callback_only_history_is_bounded_deduplicated_and_returns_last_twenty(observer):
    for index in range(520):
        observer.bridge.on_order(order(OrderID=index, OrderSysID=f"sys-{index}"))
        observer.bridge.on_trade(trade(TradeID=f"trade-{index}"))
    observer.bridge.on_trade(trade(TradeID="trade-519", Volume=3))
    payload = last_payload(observer)
    assert len(payload["orders"]) == len(payload["trades"]) == 20
    assert payload["orders"][0]["order_id"] == "500"
    assert payload["trades"][-1]["volume"] == 3
    for name in ("observed_orders", "observed_trades"):
        assert payload[name]["count"] == 512 and payload[name]["capacity"] == 512
        assert payload[name]["truncated"] is True and payload[name]["coverage"] == "callbacks_since_start"
    assert payload["active_orders"]["status"] == "unknown"


def test_same_event_ids_are_separate_between_symbols_and_unscoped_ids_are_not_invented(observer):
    add_option(observer)
    for instrument in (PRIMARY[1], OPTION[1]):
        observer.bridge.on_order(order(InstrumentID=instrument))
        observer.bridge.on_trade(trade(InstrumentID=instrument))
    observer.bridge.on_order(order(OrderID=-1, OrderSysID=""))
    primary = last_payload(observer)
    assert primary["observed_orders"]["count"] == primary["observed_trades"]["count"] == 1
    assert all(item["instrument_id"] == PRIMARY[1] for item in primary["orders"] + primary["trades"])
    observer.bridge.on_trade(trade(InstrumentID=OPTION[1], TradeID=""))
    option = last_payload(observer)
    assert option["observed_orders"]["count"] == option["observed_trades"]["count"] == 1
    assert all(item["instrument_id"] == OPTION[1] for item in option["orders"] + option["trades"])


def test_exchange_only_order_id_is_merged_when_local_id_arrives(observer):
    observer.bridge.on_order(order(OrderID=-1))
    observer.bridge.on_order(order(TradedVolume=2))
    payload = last_payload(observer)
    assert payload["observed_orders"]["count"] == 1
    assert payload["orders"][0]["order_id"] == "7" and payload["orders"][0]["traded_volume"] == 2


def test_trade_event_allowlist_times_amounts_and_sentinels(observer):
    observer.bridge.on_trade(trade(Price=SENTINEL, Volume=-1, TradeTime="09:10:01", SessionID=999, Password="secret"))
    event = last_payload(observer)["trades"][0]
    assert event["price"] is None and event["volume"] is None and event["trade_time"] is None
    assert event["direction"] == "sell" and event["offset"] == "close_today"
    assert set(event) == {"callback", "observed_at", "exchange", "instrument_id", "trade_id", "order_id",
                          "order_sys_id", "trade_time", "price", "volume", "direction", "offset"}
    assert "secret" not in json.dumps(last_payload(observer), allow_nan=False)


def test_error_callbacks_publish_only_codes_and_do_not_turn_on_trading(observer):
    add_option(observer)
    observer.bridge.params_map.enable_trading = True
    observer.bridge.trading = True
    observer.bridge.on_error({"errCode": "0004", "errMsg": "private-account password secret", "Memo": "private-memo"})
    assert len(observer.sent) == 2
    for _, payload, _ in observer.sent:
        assert payload["errors"][0]["error_code"] == "0004" and payload["errors"][0]["scope"] == "strategy"
        assert "secret" not in json.dumps(payload)
        assert payload["runtime"]["enable_trading"] is False
    assert observer.bridge.params_map.enable_trading is False and observer.bridge.trading is False
    assert all("secret" not in text for _, text in observer.adapter.events["output"])


def test_callback_after_stop_and_restart_does_not_claim_preexisting_order_snapshot(observer):
    observer.bridge.on_order(order())
    previous_start = last_payload(observer)["observed_orders"]["window_started_at"]
    observer.bridge.on_stop()
    observer.sent.clear()
    observer.bridge.on_trade(trade())
    assert observer.sent == []
    observer.bridge.on_start()
    payload = last_payload(observer)
    assert payload["orders"] == [] and payload["trades"] == []
    assert payload["observed_orders"]["window_started_at"] != previous_start
    assert payload["active_orders"]["status"] == "unknown" and payload["active_orders"]["count"] is None


def test_account_uses_installed_fee_primary_and_allows_only_numeric_observation_fields(observer):
    observer.bridge.on_stop()
    data = observer.adapter.infini
    data.investors = [{"InvestorID": "only"}]
    data.accounts["only"] = {"Fee": 7, "Commission": 999, "Available": 0, "Balance": 0,
                            "PositionProfit": -2, "CloseProfit": 3, "Margin": 10, "DynamicRights": 100,
                            "FrozenMargin": 4, "FrozenCommission": 0, "FrozenCash": 5,
                            "AccountID": "private-account", "Password": "secret"}
    observer.bridge.on_start()
    account = last_payload(observer)["account"]
    assert account["status"] == "ready" and account["available"] == account["balance"] == 0
    assert account["commission"] == 7 and account["margin"] == 10 and account["risk"] == 0.1
    assert account["position_profit"] == -2 and account["frozen_commission"] == 0 and account["frozen_cash"] == 5
    assert last_payload(observer)["capabilities"]["available_values"] == {}
    assert "private-account" not in json.dumps(last_payload(observer))


def test_optional_account_alias_and_missing_fields_never_fabricate_zero(observer):
    observer.bridge.on_stop()
    observer.adapter.infini.investors = [{"InvestorID": "only"}]
    observer.adapter.infini.accounts["only"] = {"Commission": 2, "Available": 10, "FrozenMargin": SENTINEL}
    observer.bridge.on_start()
    account = last_payload(observer)["account"]
    assert account["commission"] == 2
    assert account["frozen_margin"] is None and account["frozen_commission"] is None
    assert account["margin"] is None and account["risk"] is None


def test_native_rate_discovery_never_invokes_candidates_or_labels_account_amounts_as_rates(observer, monkeypatch):
    called = []
    monkeypatch.setattr(observer.adapter.native, "getCommissionRate", lambda *args: called.append(args), raising=False)
    monkeypatch.setattr(observer.adapter.native, "getMargin", "diagnostic", raising=False)
    observer.adapter.infini.instruments[PRIMARY] = {
        "LongMarginRatio": 0.12, "OpenRatioByVolume": 3, "CloseTodayRatioByMoney": 0,
        "ShortMarginRatio": SENTINEL, "CloseRatioByMoney": -2, "Fee": 50, "Margin": 700,
        "InventedExchangeOfficialRate": 999,
    }
    observer.bridge.on_tick(tick())
    capabilities = last_payload(observer)["capabilities"]
    assert capabilities == {
        "rates_query": "not_exposed_in_documented_api", "native_candidate_names": ["getCommissionRate", "getMargin"],
        "source": "account_or_terminal_reference_unverified",
        "available_values": {"LongMarginRatio": 0.12, "OpenRatioByVolume": 3, "CloseTodayRatioByMoney": 0},
    }
    assert called == []


def test_empty_native_capability_surface_is_explicitly_unavailable(observer):
    observer.bridge.on_tick(tick())
    assert last_payload(observer)["capabilities"] == {
        "rates_query": "not_exposed_in_documented_api", "native_candidate_names": [],
        "source": "account_or_terminal_reference_unverified", "available_values": {},
    }


def test_unknown_investor_retries_on_callback_after_cooldown(observer, monkeypatch):
    now = [10.0]
    observer.bridge._investor_scope_checked = False
    monkeypatch.setattr(observer.adapter.module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    observer.bridge.on_order(order())
    assert last_payload(observer)["account"]["status"] == "unknown"
    before = len([item for item in observer.adapter.events["reads"] if item[0] == "investor"])
    observer.adapter.infini.investors = {"terminal-slot": {"InvestorID": "only"}}
    observer.adapter.infini.accounts["only"] = {"Available": 0, "Balance": 0}
    observer.bridge.on_trade(trade())
    assert last_payload(observer)["account"]["status"] == "unknown"
    assert len([item for item in observer.adapter.events["reads"] if item[0] == "investor"]) == before
    now[0] = 15.0
    observer.bridge.on_order_cancel(order(Status="已撤销"))
    assert last_payload(observer)["account"]["status"] == "ready"
    assert last_payload(observer)["position"]["status"] == "unknown"
    assert last_payload(observer)["position"]["net"] is None


def test_investor_exceptions_retry_and_mapping_keys_are_never_used_as_accounts(observer, monkeypatch):
    now = [20.0]
    observer.bridge._investor_scope_checked = False
    monkeypatch.setattr(observer.adapter.module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    original = observer.adapter.native.getInvestorList
    monkeypatch.setattr(observer.adapter.native, "getInvestorList", lambda: (_ for _ in ()).throw(RuntimeError("private-secret")))
    observer.bridge.on_trade(trade())
    assert last_payload(observer)["account"]["status"] == "unknown"
    monkeypatch.setattr(observer.adapter.native, "getInvestorList", original)
    for response in ({"wrong-key": {}}, [{"AccountID": "wrong"}], [{"InvestorID": "a"}, {"InvestorID": "b"}], ["only"]):
        observer.adapter.infini.investors = response
        now[0] += 5
        observer.bridge.on_trade(trade())
        assert last_payload(observer)["account"]["status"] == "unknown"
    observer.adapter.infini.investors = {"InvestorID": "only"}
    observer.adapter.infini.accounts["only"] = {"Available": 0}
    now[0] += 5
    observer.bridge.on_trade(trade())
    assert last_payload(observer)["account"]["status"] == "ready"
    assert "private-secret" not in str(observer.adapter.events["output"])


def test_multiple_accounts_becoming_available_clears_previous_single_scope(observer, monkeypatch):
    now = [20.0]
    observer.bridge._investor_scope_checked = False
    monkeypatch.setattr(observer.adapter.module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    observer.adapter.infini.investors = [{"InvestorID": "a"}]
    observer.adapter.infini.accounts["a"] = {"Available": 100}
    observer.bridge.on_order(order())
    assert last_payload(observer)["account"]["status"] == "ready"
    now[0] += 5
    observer.adapter.infini.investors = [{"InvestorID": "a"}, {"InvestorID": "b"}]
    observer.bridge.on_order(order())
    assert last_payload(observer)["account"]["status"] == "unknown"
    assert last_payload(observer)["position"]["status"] == "unknown"


def test_mismatched_native_account_or_position_identity_stays_unknown(observer):
    observer.bridge.on_stop()
    observer.adapter.infini.investors = [{"InvestorID": "only"}]
    observer.adapter.infini.accounts["only"] = {"InvestorID": "other", "Available": 100}
    observer.adapter.infini.positions["only"] = [{"InvestorID": "other", "InstrumentID": PRIMARY[1],
                                                "Direction": "多", "Position": 1, "YdPositionClose": 0}]
    observer.bridge.on_start()
    assert last_payload(observer)["account"]["status"] == "unknown"
    assert last_payload(observer)["position"]["status"] == "unknown"


def test_position_without_exchange_is_unknown_when_contract_id_is_ambiguous(observer):
    observer.bridge.on_stop()
    observer.adapter.infini.investors = [{"InvestorID": "only"}]
    observer.adapter.infini.positions["only"] = [{"InstrumentID": PRIMARY[1], "Direction": "多", "Position": 3, "YdPositionClose": 1}]
    observer.bridge.set_params({"additional_symbols": "DCE.rb2610"})
    observer.sent.clear()
    observer.bridge.on_start()
    assert len(observer.sent) == 2
    assert all(item[1]["position"]["status"] == "unknown" for item in observer.sent)
    observer.adapter.infini.positions["only"][0]["ExchangeID"] = "DCE"
    observer.bridge.on_tick(tick(exchange="DCE"))
    assert last_payload(observer)["position"]["long"]["total"] == 3
    assert last_payload(observer)["position"]["short"]["total"] is None
    assert last_payload(observer)["position"]["net"] is None
    assert last_payload(observer)["position"]["status"] == "observed"


def test_repeated_start_does_not_duplicate_subscriptions_or_reset_observation_window(observer):
    observer.bridge.on_order(order())
    started_at = last_payload(observer)["observed_orders"]["window_started_at"]
    count = len(observer.adapter.events["sub"])
    observer.bridge.on_start()
    observer.bridge.on_order(order())
    assert len(observer.adapter.events["sub"]) == count
    assert last_payload(observer)["observed_orders"]["window_started_at"] == started_at
    assert last_payload(observer)["observed_orders"]["count"] == 1


def test_callbacks_enqueue_on_callback_thread_and_worker_never_invokes_native(adapter):
    network = []
    callback_threads = []

    def transport(url, payload, timeout):
        json.dumps(payload, allow_nan=False)
        network.append((payload, threading.current_thread()))
        return 200, b'{"signal":"buy","reason":"candidate"}'

    bridge = configure(adapter.module, transport=transport)
    original_enqueue = bridge._enqueue

    def enqueue(*args, **kwargs):
        callback_threads.append(threading.current_thread())
        return original_enqueue(*args, **kwargs)

    bridge._enqueue = enqueue
    bridge.on_start()
    try:
        assert wait_for(lambda: len(network) >= 1)
        bridge.on_order(order())
        assert wait_for(lambda: any(payload["orders"] for payload, _ in network))
        bridge.on_trade(trade())
        assert wait_for(lambda: any(payload["trades"] for payload, _ in network))
        bridge.on_error({"errCode": "0004"})
        assert wait_for(lambda: any(payload["errors"] for payload, _ in network))
        bridge._drain_responses()
        assert all(thread is threading.main_thread() for thread in callback_threads)
        assert all(thread is threading.main_thread() for _, thread in adapter.events["reads"])
        assert all(thread is not threading.main_thread() for _, thread in network)
        assert bridge.state_map.order_submissions == 0 and bridge.state_map.last_signal == "hold"
    finally:
        bridge.on_stop()


def test_every_order_cancel_close_entrypoint_stays_inert_and_forces_observer_callbacks(observer, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("native execution is forbidden")

    for name in ("sendOrder", "cancelOrder", "closePosition", "getInvestorOrders", "getInvestorTrades"):
        monkeypatch.setattr(observer.adapter.native, name, forbidden, raising=False)
    for method in ("send_order", "make_order_req", "auto_close_position", "cancel_order"):
        observer.bridge.params_map.enable_trading = True
        observer.bridge.trading = True
        assert getattr(observer.bridge, method)("ignored") is None
        assert observer.bridge.params_map.enable_trading is False and observer.bridge.trading is False
    for callback, value in (("on_order", order()), ("on_order_trade", order(Status="全部成交")),
                            ("on_order_cancel", order(Status="已撤销")), ("on_trade", trade()),
                            ("on_error", {"errCode": "0004"})):
        observer.bridge.params_map.enable_trading = True
        observer.bridge.trading = True
        getattr(observer.bridge, callback)(value)
        assert observer.bridge.params_map.enable_trading is False and observer.bridge.trading is False
        assert last_payload(observer)["runtime"]["order_submission_count"] == 0
