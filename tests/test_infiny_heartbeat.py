from __future__ import annotations

import json
import threading

import pytest

from test_infiny_adapter import adapter
from test_infiny_bridge import FIXED_NOW, _payload, _request, _tick, running_bridge
from ak_trading.infiny_bridge.decision import DailyObserver
from ak_trading.infiny_bridge.protocol import ProtocolViolation, normalize_payload


def unknown_position():
    return {"status": "unknown", "long": {"total": None, "today": None, "yesterday": None},
            "short": {"total": None, "today": None, "yesterday": None}, "net": None}


def test_actual_adapter_start_heartbeat_preserves_available_account_with_unknown_position(adapter, running_bridge):
    address, service, calls, _audit = running_bridge
    adapter.infini.investors = [{"InvestorID": "fixture-account"}]
    adapter.infini.accounts["fixture-account"] = {"Available": 0, "Balance": 0}
    adapter.infini.positions["fixture-account"] = [{"InstrumentID": "rb2610", "Direction": "多"}]
    finished = threading.Event()
    responses = []
    def transport(url, payload, timeout):
        status, body, _raw = _request(address, "POST", "/heartbeat", payload)
        responses.append((status, body))
        finished.set()
        return status, body
    bridge = adapter.module.AIBridge(transport=transport)
    bridge.params_map.exchange = "SHFE"
    bridge.params_map.instrument_id = "rb2610"
    try:
        bridge.on_start()
        assert finished.wait(2)
        assert responses == [(200, {"signal": "hold", "reason": "heartbeat"})]
        receipt = service.status()["recent_receipt"]
        assert receipt["snapshot"]["account"]["available"] == 0
        assert receipt["snapshot"]["position"] == unknown_position()
        assert service.status()["counters"]["protocol_errors"] == 0
        assert calls == []
    finally:
        bridge.on_stop()


def test_ready_position_with_null_totals_is_downgraded_not_assumed_flat():
    raw = unknown_position()
    raw["status"] = "ready"
    request = _payload(position=raw)
    result = normalize_payload(request, allowed_symbols=["SHFE.rb2610"], now=FIXED_NOW)
    assert result.payload["position"]["status"] == "unknown"
    assert result.payload["position"]["net"] is None


@pytest.mark.parametrize("invalid", [-1, 0.5, True, "0"])
def test_malformed_position_values_are_still_rejected(invalid):
    position = unknown_position()
    position["long"]["total"] = invalid
    with pytest.raises(ProtocolViolation, match="invalid_position"):
        normalize_payload(_payload(position=position), allowed_symbols=["SHFE.rb2610"], now=FIXED_NOW)


def test_inconsistent_ready_position_is_rejected():
    raw = _payload()
    raw["position"]["net"] = 1
    with pytest.raises(ProtocolViolation, match="invalid_position"):
        normalize_payload(raw, allowed_symbols=["SHFE.rb2610"], now=FIXED_NOW)


def test_optional_unknown_tick_context_does_not_erase_valid_market_telemetry():
    raw = _payload(position=unknown_position(), ticks=[_tick(price_tick=None, upper_limit=None, lower_limit=None, volume=None)])
    normalized = normalize_payload(raw, allowed_symbols=["SHFE.rb2610"], now=FIXED_NOW)
    assert normalized.payload["ticks"][0]["last"] == 4000
    assert normalized.payload["ticks"][0]["price_tick"] is None
    assert normalized.payload["position"]["status"] == "unknown"


def test_rejected_heartbeat_records_safe_reason_and_identity_without_raw_secrets(running_bridge):
    address, service, calls, audit = running_bridge
    raw = _payload(ticks=[], position={"status": "ready", "net": "private-secret"}, account_tag="private-account")
    status, body, _ = _request(address, "POST", "/heartbeat", raw)
    assert status == 400 and body == {"signal": "hold", "reason": "invalid_position"}
    rejection = service.status()["recent_rejections"][-1]
    assert rejection["reason"] == "invalid_position"
    assert rejection["endpoint"] == "/heartbeat"
    assert rejection["instrument"] == "rb2610"
    assert rejection["payload_hash"]
    assert "private-secret" not in json.dumps(rejection)
    assert "private-account" not in audit.read_text(encoding="utf-8")
    assert calls == []


def test_unknown_position_stays_blocked_at_the_decision_boundary(tmp_path):
    from test_infiny_decision import make_history, payload
    history = tmp_path / "history"
    make_history(history)
    from datetime import date
    observer = DailyObserver(history, ["SHFE.rb2610"], expected_history_date=date(2026, 9, 8))
    raw = payload()
    raw["position"] = unknown_position()
    raw["account"]["available"] = 100000
    result = observer.evaluate(normalize_payload(raw, allowed_symbols=["SHFE.rb2610"]).payload)
    assert result["candidate_signal"] == "hold"
    assert result["reason"] == "position_unknown"
    assert result["execution_authorized"] is False
