from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
import types
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest


ADAPTER_PATH = Path(__file__).resolve().parents[1] / "integrations" / "infiny" / "AIBridge.py"


@pytest.fixture
def adapter(monkeypatch):
    events: dict[str, list] = {
        "base_init": [],
        "base_start": [],
        "base_stop": [],
        "sub": [],
        "unsub": [],
        "status": [],
        "output": [],
    }

    infini = types.ModuleType("fake_native_storage")
    infini.investors = []
    infini.accounts = {}
    infini.positions = {}
    infini.instruments = {}

    def get_investor_list():
        events.setdefault("reads", []).append(("investor", threading.current_thread()))
        return infini.investors

    def get_investor_account(investor):
        events.setdefault("reads", []).append(("account", threading.current_thread()))
        return infini.accounts.get(investor, {})

    def get_investor_position(investor, simple=False):
        events.setdefault("reads", []).append(("position", threading.current_thread()))
        return infini.positions.get(investor, [])

    def get_instrument(exchange, instrument_id):
        events.setdefault("reads", []).append(("instrument", threading.current_thread()))
        return infini.instruments.get((exchange, instrument_id), {})

    # The deployable adapter must load against native INFINIGO alone.
    native = types.ModuleType("INFINIGO")
    native.getInvestorList = get_investor_list
    native.getInvestorAccount = get_investor_account
    native.getInvestorPosition = lambda investor, Simple=False: get_investor_position(investor, Simple)
    native.getInstrument = lambda Exchange, InstrumentID: get_instrument(Exchange, InstrumentID)
    native.updateState = lambda strategy_id, data, /: events["status"].append(threading.current_thread())
    native.updateParam = lambda **kwargs: None
    native.writeLog = lambda msg: events["output"].append((threading.current_thread(), msg))
    native.subMarketData = lambda **kwargs: events["sub"].append((kwargs["ExchangeID"], kwargs["InstrumentID"], threading.current_thread()))
    native.unsubMarketData = lambda **kwargs: events["unsub"].append((kwargs["ExchangeID"], kwargs["InstrumentID"], threading.current_thread()))
    monkeypatch.setitem(sys.modules, "INFINIGO", native)

    module_name = f"_fake_infiny_adapter_{id(events)}"
    spec = importlib.util.spec_from_file_location(module_name, ADAPTER_PATH)
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, loaded)
    spec.loader.exec_module(loaded)
    return SimpleNamespace(module=loaded, infini=infini, native=native, events=events)


def configure(loaded, *, batch_size=10, transport=None):
    if transport is None:
        transport = lambda _url, _payload, _timeout: (200, b'{"signal":"hold","reason":"ok"}')
    bridge = loaded.AIBridge(transport=transport)
    bridge.params_map.exchange = "SHFE"
    bridge.params_map.instrument_id = "rb2610"
    bridge.params_map.batch_size = batch_size
    bridge.params_map.account_tag = "private-account-tag"
    return bridge


def tick(**overrides):
    values = {
        "exchange": "SHFE",
        "instrument_id": "rb2610",
        "last_price": 4000.0,
        "bid_price1": 3999.0,
        "ask_price1": 4001.0,
        "volume": 12,
        "upper_limit_price": 4500.0,
        "lower_limit_price": 3500.0,
        "trading_day": "20260909",
        "datetime": datetime(2026, 9, 9, 9, 10, 0),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def wait_for(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def stop_quietly(bridge):
    bridge.on_stop()


def test_adapter_loads_with_fake_sdk_and_legacy_defaults(adapter):
    params = adapter.module.Params()
    assert params.exchange == "SHFE"
    assert params.instrument_id == "rb2610"
    assert params.api_url == "http://127.0.0.1:17778/analyze"
    assert params.batch_size == 10
    assert params.enable_trading is False
    assert params.order_volume == 1
    assert params.volume == 1
    assert issubclass(adapter.module.AKObserverBridge, adapter.module.AIBridge)


def test_loopback_url_and_client_timeout_are_bounded(adapter):
    validate = adapter.module.validate_loopback_url
    assert validate("http://127.0.0.1:17778/analyze") == "http://127.0.0.1:17778/analyze"
    assert validate("http://localhost:17778") == "http://localhost:17778/analyze"
    with pytest.raises(ValueError):
        validate("https://example.invalid/analyze")
    with pytest.raises(ValueError):
        validate("http://127.0.0.1:17778/analyze?token=secret")
    with pytest.raises(ValueError):
        adapter.module.AIBridge(http_timeout=3.01)


def test_preview_maps_buy_and_sell_open_fak_through_test_sender_only(adapter):
    params = SimpleNamespace(exchange="SHFE", instrument_id="rb2610", order_volume=2)
    sent = []

    def fake_sender(**kwargs):
        sent.append(kwargs)
        return "fake-only"

    for signal in ("buy", "sell"):
        preview = adapter.module.build_order_preview(signal, 4000, params)
        assert preview == {
            "exchange": "SHFE",
            "instrument_id": "rb2610",
            "volume": 2,
            "price": 4000,
            "order_direction": signal,
            "order_type": "FAK",
            "offset": "OPEN",
        }
        assert fake_sender(**preview) == "fake-only"
    assert [item["order_direction"] for item in sent] == ["buy", "sell"]


def test_preview_rejects_invalid_signal_price_and_volume(adapter):
    params = SimpleNamespace(exchange="SHFE", instrument_id="rb2610", order_volume=1)
    with pytest.raises(ValueError):
        adapter.module.build_order_preview("hold", 4000, params)
    with pytest.raises(ValueError):
        adapter.module.build_order_preview("buy", float("nan"), params)
    with pytest.raises(ValueError):
        adapter.module.build_order_preview("buy", 4000, params, volume=1.5)


def test_actual_local_http_response_completes_after_server_closes_socket(adapter):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = b'{"signal":"hold","reason":"observing"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, raw = adapter.module._default_transport(f"http://127.0.0.1:{server.server_port}/analyze", {"test": True}, 2)
        assert status == 200
        assert json.loads(raw) == {"signal": "hold", "reason": "observing"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def direct_http(adapter, monkeypatch):
    """Exercise the real transport using fake connections, clock, and timer."""

    state = SimpleNamespace(
        now=10.0, status=200, body=b"{}", chunk_size=1,
        connect_delay=0.2, request_delay=0.3, read_delay=0.2,
        on_headers=None, on_read=None, connections=[], timers=[],
    )

    class FakeTimer:
        def __init__(self, interval, callback):
            self.interval, self.callback = interval, callback
            self.started = self.cancelled = self.daemon = False
            state.timers.append(self)

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

        def fire(self):
            self.callback()

    class FakeSocket:
        def __init__(self):
            self.timeouts, self.shutdown_modes = [], []

        def settimeout(self, value):
            self.timeouts.append(value)

        def shutdown(self, how):
            self.shutdown_modes.append(how)

    class FakeResponse:
        def __init__(self):
            self.status = state.status
            self.body = bytearray(state.body)
            self.read_sizes = []
            self.closed = False

        def read1(self, size):
            self.read_sizes.append(size)
            state.now += state.read_delay
            if state.on_read:
                state.on_read()
            count = min(size, state.chunk_size, len(self.body))
            chunk = bytes(self.body[:count])
            del self.body[:count]
            return chunk

        def close(self):
            self.closed = True

    class FakeConnection:
        scheme = "http"

        def __init__(self, host, port, timeout):
            self.host, self.port, self.timeout = host, port, timeout
            self.sock = FakeSocket()
            self.response = FakeResponse()
            self.requests = []
            self.closed = False
            state.connections.append(self)

        def connect(self):
            state.now += state.connect_delay

        def request(self, method, path, body, headers):
            state.now += state.request_delay
            self.requests.append((method, path, body, headers))

        def getresponse(self):
            if state.on_headers:
                state.on_headers()
            return self.response

        def close(self):
            self.closed = True

    class FakeTLSConnection(FakeConnection):
        scheme = "https"

    monkeypatch.setattr(adapter.module, "HTTPConnection", FakeConnection)
    monkeypatch.setattr(adapter.module, "HTTPSConnection", FakeTLSConnection)
    monkeypatch.setattr(adapter.module, "time", SimpleNamespace(monotonic=lambda: state.now))
    monkeypatch.setattr(
        adapter.module, "threading", SimpleNamespace(Event=threading.Event, Timer=FakeTimer)
    )
    return state


def test_default_transport_connects_directly_without_proxy_discovery(adapter, direct_http, monkeypatch):
    import urllib.request

    def forbidden(*_args, **_kwargs):
        pytest.fail("direct transport must not discover proxies or use an opener")

    monkeypatch.setattr(urllib.request, "getproxies", forbidden)
    monkeypatch.setattr(urllib.request, "build_opener", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    for scheme in ("http", "https"):
        assert adapter.module._default_transport(
            f"{scheme}://127.0.0.1:17778/analyze", {"ok": True}, 2.0
        ) == (200, b"{}")
        connection = direct_http.connections[-1]
        assert (connection.scheme, connection.host, connection.port) == (scheme, "127.0.0.1", 17778)
        method, path, body, headers = connection.requests[0]
        assert (method, path, json.loads(body)) == ("POST", "/analyze", {"ok": True})
        assert "Proxy-Authorization" not in headers
        assert connection.closed and connection.response.closed
        assert direct_http.timers[-1].cancelled
    assert len(direct_http.connections) == 2


def test_default_transport_localhost_uses_numeric_loopback(adapter, direct_http):
    for hostname in ("localhost", "LOCALHOST."):
        adapter.module._default_transport(f"http://{hostname}:17778/analyze", {}, 2.0)
        assert direct_http.connections[-1].host == "127.0.0.1"


def test_default_transport_rejects_redirect_without_a_second_request(adapter, direct_http):
    for status in (301, 302, 303, 307, 308):
        direct_http.status = status
        with pytest.raises(ValueError, match="redirect_not_allowed"):
            adapter.module._default_transport("http://127.0.0.1:17778/analyze", {}, 2.0)
        connection = direct_http.connections[-1]
        assert len(connection.requests) == 1
        assert connection.response.read_sizes == []
        assert connection.closed and connection.response.closed
        assert direct_http.timers[-1].cancelled
    assert len(direct_http.connections) == 5


def test_default_transport_uses_one_monotonic_deadline_and_closes_response(adapter, direct_http):
    assert adapter.module._default_transport(
        "http://127.0.0.1:17778/analyze", {"ok": True}, 2.0
    ) == (200, b"{}")
    connection = direct_http.connections[0]
    # Connect and request time consume the same budget as the body reads.
    assert connection.sock.timeouts == pytest.approx([1.8, 1.5, 1.3, 1.1])
    timer = direct_http.timers[0]
    assert timer.interval == 2.0
    assert timer.started and timer.daemon and timer.cancelled
    assert connection.closed and connection.response.closed


def test_default_transport_rejects_late_eof_even_if_timer_has_not_run(adapter, direct_http):
    direct_http.body = b""
    direct_http.read_delay = 2.0
    with pytest.raises(TimeoutError, match="response_deadline_exceeded"):
        adapter.module._default_transport("http://127.0.0.1:17778/analyze", {}, 2.0)
    connection = direct_http.connections[0]
    assert connection.closed and connection.response.closed
    assert direct_http.timers[0].cancelled


def test_default_transport_timer_interrupts_headers_and_closes_socket(adapter, direct_http):
    direct_http.on_headers = lambda: direct_http.timers[0].fire()
    with pytest.raises(TimeoutError, match="response_deadline_exceeded"):
        adapter.module._default_transport("http://127.0.0.1:17778/analyze", {}, 2.0)
    connection = direct_http.connections[0]
    assert connection.sock.shutdown_modes == [adapter.module.socket.SHUT_RDWR]
    assert connection.response.read_sizes == []
    assert connection.closed and connection.response.closed
    assert direct_http.timers[0].cancelled


def test_default_transport_limits_body_to_64k_and_closes_on_read_failure(adapter, direct_http):
    direct_http.body = b"x" * (adapter.module.MAX_RESPONSE_BYTES + 1)
    direct_http.chunk_size = 8192
    direct_http.read_delay = 0
    with pytest.raises(ValueError, match="response_too_large"):
        adapter.module._default_transport("http://127.0.0.1:17778/analyze", {}, 2.0)
    connection = direct_http.connections[0]
    assert sum(connection.response.read_sizes) == adapter.module.MAX_RESPONSE_BYTES + 1
    assert connection.closed and connection.response.closed
    assert direct_http.timers[0].cancelled

    def read_failure():
        raise OSError("fake_read_failure")

    direct_http.on_read = read_failure
    with pytest.raises(OSError, match="fake_read_failure"):
        adapter.module._default_transport("http://127.0.0.1:17778/analyze", {}, 2.0)
    connection = direct_http.connections[-1]
    assert connection.closed and connection.response.closed
    assert direct_http.timers[-1].cancelled


def test_position_missing_identity_fails_closed_without_aggregation(adapter):
    bridge = configure(adapter.module)
    adapter.infini.investors = [{"InvestorID": "investor-1"}]
    known = {"InstrumentID": "rb2610", "Direction": "空", "Position": 3, "YdPositionClose": 1}
    missing = {"Direction": "多", "Position": 4, "YdPositionClose": 2}
    invalid_rows = [missing] + [
        {**missing, "InstrumentID": identity} for identity in (None, "", "  ", 123, [])
    ]
    for row in invalid_rows:
        adapter.infini.positions["investor-1"] = [known, row]
        # Malformed identity aborts the capture outright (no partial rows).
        with pytest.raises(adapter.module._CaptureFault):
            bridge._read_position_snapshot()
        safe = bridge._safe_position()
        assert safe["status"] == "unknown"
        assert safe["net"] is None and safe["long"]["total"] is None and safe["short"]["total"] is None


def test_position_target_exchange_mismatch_fails_closed(adapter):
    bridge = configure(adapter.module)
    adapter.infini.investors = [{"InvestorID": "investor-1"}]
    for exchange in ("DCE", "", " ", 12):
        adapter.infini.positions["investor-1"] = [{
            "InstrumentID": "rb2610",
            "Exchange": exchange,
            "Direction": "多",
            "Position": 4,
            "YdPositionClose": 2,
        }]
        # A row claiming a different exchange is a capture fault, not data.
        with pytest.raises(adapter.module._CaptureFault):
            bridge._read_position_snapshot()
        position = bridge._safe_position()
        assert position["status"] == "unknown"
        assert position["net"] is None


def test_position_other_identified_instrument_is_not_aggregated(adapter):
    bridge = configure(adapter.module)
    adapter.infini.investors = [{"InvestorID": "investor-1"}]
    adapter.infini.positions["investor-1"] = [
        {
            "InstrumentID": "other-contract",
            "Exchange": "SHFE",
            "Direction": "多",
            "Position": 99,
            "YdPositionClose": 99,
        },
        {
            "InstrumentID": "rb2610",
            "Exchange": "SHFE",
            "Direction": "空",
            "Position": 3,
            "YdPositionClose": 1,
        },
    ]
    position = bridge._read_position_snapshot()
    assert position["status"] == "observed"
    assert position["long"] == {"total": None, "today": None, "yesterday": None}
    assert position["short"] == {"total": 3, "today": 2, "yesterday": 1}
    assert position["net"] is None


def test_empty_position_without_query_completion_serializes_as_unknown(adapter):
    bridge = configure(adapter.module)
    adapter.infini.investors = [{"InvestorID": "investor-1"}]
    adapter.infini.accounts["investor-1"] = {"Available": 100, "Balance": 200}
    adapter.infini.positions["investor-1"] = []
    envelope = bridge._build_envelope([])
    assert envelope["position"] == {
        "status": "unknown",
        "long": {"total": None, "today": None, "yesterday": None},
        "short": {"total": None, "today": None, "yesterday": None},
        "net": None,
    }


def test_both_position_sides_serialize_plain_today_yesterday_fields(adapter):
    bridge = configure(adapter.module)
    adapter.infini.investors = [{"InvestorID": "investor-1"}]
    adapter.infini.positions["investor-1"] = [
        {"InstrumentID": "rb2610", "Direction": "多", "Position": 4, "YdPositionClose": 2},
        {"InstrumentID": "rb2610", "Direction": "空", "Position": 3, "YdPositionClose": 1},
    ]
    position = bridge._read_position_snapshot()
    assert position == {
        "status": "observed",
        "long": {"total": 4, "today": 2, "yesterday": 2},
        "short": {"total": 3, "today": 2, "yesterday": 1},
        "net": 1,
    }
    assert "Direction" not in json.dumps(position)


def test_invalid_position_does_not_fabricate_zero(adapter):
    bridge = configure(adapter.module)
    adapter.infini.investors = [{"InvestorID": "investor-1"}]
    adapter.infini.positions["investor-1"] = [
        {"InstrumentID": "rb2610", "Direction": "多", "Position": 4}
    ]
    assert bridge._read_position_snapshot()["status"] == "unknown"


def test_partial_position_row_preserves_known_nonzero_total(adapter):
    bridge = configure(adapter.module)
    adapter.infini.investors = [{"InvestorID": "investor-1"}]
    adapter.infini.positions["investor-1"] = [
        {"InstrumentID": "rb2610", "Exchange": "SHFE", "Direction": "多", "Position": 4}
    ]
    position = bridge._read_position_snapshot()
    assert position["status"] == "unknown" and position["net"] is None
    assert position["long"]["total"] == 4
    assert position["long"]["today"] is None and position["long"]["yesterday"] is None
    assert position["short"] == {"total": None, "today": None, "yesterday": None}


def test_account_contract_are_allowlisted_and_account_tag_is_masked(adapter):
    bridge = configure(adapter.module)
    adapter.infini.investors = [{"InvestorID": "investor-1", "Password": "secret"}]
    adapter.infini.accounts["investor-1"] = {
        "InvestorID": "investor-1",
        "AccountID": "full-account-id",
        "Available": 123.5,
        "Balance": 456.5,
        "DynamicRights": 500,
        "Margin": 50,
        "Password": "secret",
    }
    adapter.infini.instruments[("SHFE", "rb2610")] = {
        "PriceTick": 1,
        "UpperLimitPrice": 4500,
        "LowerLimitPrice": 3500,
        "ExpireDate": "20261015",
        "InvestorID": "should-not-leak",
    }
    envelope = bridge._build_envelope([])
    assert envelope["account"] == {
        "status": "ready",
        "available": 123.5,
        "balance": 456.5,
        "risk": 0.1,
        "commission": None,
        "position_profit": None,
        "close_profit": None,
        "margin": 50,
        "frozen_margin": None,
        "frozen_commission": None,
        "frozen_cash": None,
    }
    assert envelope["contract"] == {
        "status": "ready",
        "price_tick": 1,
        "upper_limit": 4500,
        "lower_limit": 3500,
        "expiry": "20261015",
        "asset_class": None,
        "strike": None,
        "underlying": None,
        "option_type": None,
        "multiplier": None,
        "product_id": None,
        "min_limit_order_volume": None,
        "max_limit_order_volume": None,
    }
    assert envelope["account_tag"] != "private-account-tag"
    assert "full-account-id" not in json.dumps(envelope)
    assert "secret" not in json.dumps(envelope)


def test_tick_time_is_aware_and_invalid_quote_is_skipped(adapter):
    bridge = configure(adapter.module)
    encoded = bridge._tick_to_dict(tick())
    assert encoded is not None
    assert encoded["time"].endswith("+08:00")
    assert encoded["trading_day"] == "20260909"
    assert bridge._tick_to_dict(tick(last_price=float("nan"))) is None
    assert bridge._tick_to_dict(tick(bid_price1=4002)) is None


def test_tick_time_requires_calendar_datetime_not_night_trading_day(adapter):
    bridge = configure(adapter.module)
    assert bridge._tick_to_dict(
        tick(datetime=None, trading_day="20260910", update_time="21:00:00")
    ) is None
    encoded = bridge._tick_to_dict(
        tick(datetime=datetime(2026, 9, 9, 21), trading_day="20260910")
    )
    assert encoded["time"] == "2026-09-09T21:00:00+08:00"
    assert encoded["trading_day"] == "20260910"


def test_contract_status_maps_known_values_and_unknown_stays_unknown(adapter):
    bridge = configure(adapter.module)
    bridge.on_contract_status(SimpleNamespace(exchange="SHFE", instrument_id="rb2610", status="trading"))
    assert bridge.state_map.market_status == "trading"
    bridge.on_contract_status(SimpleNamespace(exchange="SHFE", instrument_id="rb2610", status="mystery"))
    assert bridge.state_map.market_status == "unknown"
    bridge.on_contract_status(SimpleNamespace(exchange="SHFE", instrument_id="other", status="continuous"))
    assert bridge.state_map.market_status == "unknown"


def test_v2_envelope_has_sequence_runtime_unknown_orders_and_utc_produced_at(adapter):
    bridge = configure(adapter.module)
    envelope = bridge._build_envelope([], heartbeat=True)
    assert envelope["schema_version"] == 2
    assert envelope["sequence"] == 1
    assert envelope["batch_id"]
    assert envelope["instance_id"] == bridge.instance_id
    assert envelope["produced_at"].endswith("Z")
    assert envelope["heartbeat"] is True
    assert envelope["active_orders"] == {
        "status": "unknown", "count": None, "observed_count": 0, "coverage": "callbacks_since_start",
    }
    assert envelope["runtime"] == {
        "enable_trading": False,
        "observer_only": True,
        "framework_trading": False,
        "order_submission_count": 0,
        "instance_id": bridge.instance_id,
    }


def test_on_start_subscribes_without_base_start_and_sends_heartbeat(adapter):
    received = []
    ready = threading.Event()

    def transport(url, payload, timeout):
        received.append((url, payload, timeout, threading.current_thread()))
        ready.set()
        return 200, b'{"signal":"hold","reason":"heartbeat"}'

    bridge = adapter.module.AIBridge(transport=transport)
    bridge.params_map.exchange = "SHFE"
    bridge.params_map.instrument_id = "rb2610"
    bridge.on_start()
    assert bridge.trading is False
    assert adapter.events["base_start"] == []
    assert adapter.events["sub"]
    assert ready.wait(1)
    assert received[0][0].endswith("/heartbeat")
    assert received[0][1]["ticks"] == []
    assert received[0][1]["schema_version"] == 2
    assert received[0][2] <= 3
    stop_quietly(bridge)


def test_worker_only_calls_transport_and_main_thread_drains_output(adapter):
    received_threads = []
    response_ready = threading.Event()

    def transport(_url, _payload, _timeout):
        received_threads.append(threading.current_thread())
        response_ready.set()
        return 200, b'{"signal":"hold","reason":"ok"}'

    bridge = configure(adapter.module, batch_size=1, transport=transport)
    bridge._start_worker()
    bridge._enqueue("analyze", bridge._build_envelope([bridge._tick_to_dict(tick())]), "http://127.0.0.1:17778/analyze")
    assert response_ready.wait(1)
    assert received_threads and all(item is not threading.main_thread() for item in received_threads)
    before = len(adapter.events["output"])
    bridge._drain_responses()
    assert len(adapter.events["output"]) > before
    assert all(item[0] is threading.main_thread() for item in adapter.events["output"])
    stop_quietly(bridge)


def test_buy_response_is_logged_as_blocked_hold_without_order_submission(adapter):
    transport = lambda _url, _payload, _timeout: (200, b'{"signal":"buy","reason":"candidate"}')
    bridge = configure(adapter.module, transport=transport)
    bridge._start_worker()
    bridge._enqueue(
        "analyze",
        bridge._build_envelope([]),
        "http://127.0.0.1:17778/analyze",
    )
    assert wait_for(lambda: not bridge._response_queue.empty())
    bridge._drain_responses()
    assert bridge.state_map.last_signal == "hold"
    assert bridge.state_map.blocked_candidates == 1
    assert bridge.state_map.order_submissions == 0
    assert any("阻断" in message for _thread, message in adapter.events["output"])
    stop_quietly(bridge)


def test_unexpected_response_keys_force_hold(adapter):
    bridge = configure(adapter.module)
    bridge._handle_response(
        adapter.module._WorkerResponse("analyze", 200, b'{"signal":"sell","reason":"x","extra":1}')
    )
    assert bridge.state_map.last_signal == "hold"
    assert bridge.state_map.last_reason == "invalid_response"
    assert bridge.state_map.blocked_candidates == 0


def test_queue_is_bounded_drops_oldest_and_reports_count(adapter):
    bridge = adapter.module.AIBridge(queue_size=1)
    first = bridge._build_envelope([])
    second = bridge._build_envelope([])
    assert bridge._enqueue("analyze", first, "http://127.0.0.1:17778/analyze")
    assert bridge._enqueue("analyze", second, "http://127.0.0.1:17778/analyze")
    assert bridge._dropped_batches == 1
    retained = bridge._request_queue.get_nowait()
    bridge._request_queue.task_done()
    assert retained.payload["sequence"] == second["sequence"]
    assert bridge._request_queue.maxsize == 1


def test_invalid_quote_does_not_enqueue_or_call_transport(adapter):
    called = []
    bridge = configure(adapter.module, batch_size=1)
    bridge._transport = lambda *args: called.append(args)
    bridge._running = True
    bridge.on_tick(tick(ask_price1=3998))
    assert called == []
    assert bridge.state_map.last_signal == "hold"
    assert bridge.state_map.tick_count == 1


def test_enable_trading_true_is_reset_and_send_order_is_inert(adapter):
    bridge = configure(adapter.module)
    bridge.params_map.enable_trading = True
    bridge.on_start()
    assert bridge.params_map.enable_trading is False
    assert bridge.trading is False
    assert bridge.send_order(
        exchange="SHFE",
        instrument_id="rb2610",
        volume=1,
        price=4000,
        order_direction="buy",
        order_type="FAK",
    ) is None
    assert bridge.make_order_req("SHFE", "rb2610", 1, 4000, "buy", "0", "FAK", "") is None
    assert bridge.auto_close_position("SHFE", "rb2610", 1, 4000, "sell") is None
    assert bridge.state_map.order_submissions == 0
    stop_quietly(bridge)


def test_set_params_true_is_reset_before_framework_update(adapter):
    bridge = configure(adapter.module)
    bridge.set_params({"enable_trading": True, "batch_size": 4})
    assert bridge.params_map.enable_trading is False
    assert bridge.params_map.batch_size == 4
    assert bridge.trading is False


def test_on_stop_unsubscribes_without_base_stop_or_instance_save(adapter):
    bridge = configure(adapter.module)
    bridge.on_start()
    bridge.on_stop()
    assert adapter.events["base_stop"] == []
    assert adapter.events["unsub"]
    assert bridge.worker_thread is None or not bridge.worker_thread.is_alive()


def test_worker_response_size_and_status_are_bounded(adapter):
    bridge = configure(adapter.module)
    bridge._handle_response(
        adapter.module._WorkerResponse("analyze", 500, b"{}")
    )
    assert bridge.state_map.last_signal == "hold"
    bridge._handle_response(
        adapter.module._WorkerResponse("analyze", 200, b"{" + b"x" * (64 * 1024) + b"}")
    )
    assert bridge.state_map.last_signal == "hold"
    assert bridge.state_map.last_reason == "invalid_response"


def test_unknown_investor_scope_keeps_account_and_position_unknown(adapter):
    bridge = configure(adapter.module)
    adapter.infini.investors = [{"InvestorID": "a"}, {"InvestorID": "b"}]
    envelope = bridge._build_envelope([])
    assert envelope["account"]["status"] == "unknown"
    assert envelope["position"]["status"] == "unknown"
    assert envelope["account"]["available"] is None
    assert envelope["position"]["net"] is None
