from __future__ import annotations

import threading

import pytest

from test_infiny_adapter import adapter, configure
from test_infiny_bridge import _request, running_bridge


def test_restored_blank_values_do_not_erase_confirmed_subscription(adapter):
    bridge = adapter.module.AKObserverBridge()
    bridge.set_params({"exchange": "", "instrument_id": "  ", "api_url": "", "enable_trading": True})
    assert bridge.params_map.exchange == "SHFE"
    assert bridge.params_map.instrument_id == "rb2610"
    assert bridge.params_map.api_url == "http://127.0.0.1:17778/analyze"
    assert bridge.params_map.enable_trading is False


def test_explicit_other_contract_is_preserved_across_blank_restore(adapter):
    bridge = configure(adapter.module)
    bridge.set_params({"exchange": "DCE", "instrument_id": "m2701"})
    bridge.set_params({"exchange": "", "instrument_id": ""})
    assert (bridge.params_map.exchange, bridge.params_map.instrument_id) == ("DCE", "m2701")


def test_framework_direct_empty_assignment_is_recovered_before_subscription_and_heartbeat(adapter, running_bridge):
    address, service, calls, audit = running_bridge
    completed = threading.Event()
    response = []
    def transport(url, payload, timeout):
        status, body, _ = _request(address, "POST", "/heartbeat", payload)
        response.append((status, body))
        completed.set()
        return status, body
    bridge = adapter.module.AKObserverBridge(transport=transport)
    bridge.strategy_name = "test"
    bridge.params_map.exchange = ""
    bridge.params_map.instrument_id = ""
    bridge.params_map.api_url = ""
    try:
        bridge.on_start()
        assert completed.wait(2)
        assert adapter.events["sub"][0][:2] == ("SHFE", "rb2610")
        assert response == [(200, {"signal": "hold", "reason": "heartbeat"})]
        receipt = service.status()["recent_receipt"]
        assert receipt["exchange"] == "SHFE" and receipt["instrument"] == "rb2610"
        assert receipt["snapshot"]["runtime"]["enable_trading"] is False
        assert any(adapter.module.ADAPTER_VERSION in text for _, text in adapter.events["output"])
    finally:
        bridge.on_stop()


def test_partial_unknown_subscription_does_not_guess_an_exchange(adapter):
    bridge = configure(adapter.module)
    bridge.params_map.exchange = ""
    bridge.params_map.instrument_id = "m2701"
    bridge.on_start()
    assert adapter.events["sub"] == []
    assert bridge.worker_thread is None
    assert bridge.state_map.connection_status == "invalid"


def test_invalid_nonempty_destination_is_not_replaced_with_defaults(adapter):
    bridge = configure(adapter.module)
    with pytest.raises(ValueError):
        bridge.set_params({"api_url": "https://example.invalid/analyze"})
    assert bridge.params_map.api_url == "http://127.0.0.1:17778/analyze"
