from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import time
import json
import queue
import threading
from types import SimpleNamespace

import pytest

from ak_trading.infiny_bridge.protocol import ProtocolViolation, normalize_payload, parse_json_body
from ak_trading.infiny_bridge.service import BridgeService
from test_infiny_adapter import adapter, configure, tick, wait_for
from test_infiny_bridge import FIXED_NOW, _payload, _request, running_bridge


class MutableClock:
    def __init__(self):
        self.wall = FIXED_NOW
        self.elapsed = 100.0

    def now(self):
        return self.wall

    def monotonic(self):
        return self.elapsed

    def advance(self, seconds):
        self.wall += timedelta(seconds=seconds)
        self.elapsed += seconds


class PolledQueue(queue.Queue):
    """Wait for real worker iterations without advancing the injected clock."""

    polls = 0

    def get(self, *args, **kwargs):
        self.polls += 1
        return super().get(*args, **kwargs)


def settle(bridge, cycles=40, expect=None):
    target = bridge._request_queue.polls + cycles
    # Real-time watchdog only; business time stays on the injected clock. It
    # scales with the demanded poll count so larger cycles keep a bounded yet
    # feasible window under loaded Windows scheduling. The default budget is
    # unchanged: max(5.0, 40 * 0.125) == 5.0.
    watchdog = max(5.0, cycles * 0.125)
    assert wait_for(lambda: bridge._request_queue.polls >= target, timeout=watchdog)
    if expect is not None:
        assert wait_for(expect, timeout=5)
    assert wait_for(lambda: bridge._request_queue.polls >= target, timeout=watchdog)
    if expect is not None:
        assert wait_for(expect, timeout=5)


@pytest.fixture
def harness(adapter, monkeypatch):
    clock = MutableClock()

    class AdapterDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.wall.astimezone(tz) if tz else clock.wall.replace(tzinfo=None)

    monkeypatch.setattr(adapter.module, "datetime", AdapterDateTime)
    monkeypatch.setattr(adapter.module, "time", SimpleNamespace(monotonic=clock.monotonic))
    sent = []

    def transport(url, payload, timeout):
        sent.append((url, deepcopy(payload), timeout, threading.current_thread()))
        return 200, b'{"signal":"hold","reason":"heartbeat"}'

    bridge = configure(adapter.module, transport=transport)
    bridge._request_queue = PolledQueue(maxsize=2)
    yield SimpleNamespace(bridge=bridge, clock=clock, sent=sent, adapter=adapter)
    bridge.on_stop()


def test_idle_worker_repeats_cached_heartbeat_every_five_seconds_without_native(harness):
    bridge, clock, sent = harness.bridge, harness.clock, harness.sent
    bridge.params_map.enable_trading = True
    bridge.on_start()
    settle(bridge, expect=lambda: len(sent) == 1)
    assert len(sent) == 1
    first = sent[0][1]
    native_before = deepcopy({key: len(value) for key, value in harness.adapter.events.items()})
    state_before = bridge.state_map.model_dump()
    clock.advance(4.9)
    settle(bridge)
    assert len(sent) == 1
    clock.advance(0.1)
    settle(bridge, expect=lambda: len(sent) == 2)
    clock.advance(5)
    settle(bridge, expect=lambda: len(sent) == 3)
    for url, payload, timeout, thread in sent:
        assert url.endswith("/heartbeat")
        assert timeout == harness.adapter.module.HEARTBEAT_HTTP_BUDGET <= 3.0
        assert thread is not threading.main_thread()
        assert payload["ticks"] == [] and "quote" not in payload
        assert payload["metadata"]["snapshot_observed_at"] == first["metadata"]["snapshot_observed_at"]
        assert payload["position"]["status"] == payload["account"]["status"] == "unknown"
        assert payload["active_orders"]["count"] is None
        assert payload["runtime"]["observer_only"] is True
        assert payload["runtime"]["enable_trading"] is False
        assert payload["runtime"]["order_submission_count"] == 0
        json.dumps(payload, allow_nan=False)
    assert len({entry[1]["batch_id"] for entry in sent}) == 3
    assert len({entry[1]["sequence"] for entry in sent}) == 3
    assert len({entry[1]["produced_at"] for entry in sent}) == 3
    assert {key: len(value) for key, value in harness.adapter.events.items()} == native_before
    assert bridge.state_map.model_dump() == state_before
    assert bridge.params_map.enable_trading is False and bridge.trading is False


def test_partial_tick_updates_cached_quote_without_refreshing_other_snapshot_times(harness):
    bridge, clock, sent = harness.bridge, harness.clock, harness.sent
    bridge.on_start()
    settle(bridge)
    observed = sent[-1][1]["metadata"]["snapshot_observed_at"]
    clock.advance(1)
    quote_time = clock.wall.isoformat()
    bridge.on_tick(tick(datetime=quote_time))
    assert len(bridge.tick_buffer) == 1  # No complete analyze batch yet.
    native_before = len(harness.adapter.events.get("reads", []))
    for seconds in (4, 5, 5):
        clock.advance(seconds)
        settle(bridge)
    assert wait_for(lambda: len(sent) == 4, timeout=5)
    for _, payload, _, _ in sent[1:]:
        assert datetime.fromisoformat(payload["quote"]["time"]) == datetime.fromisoformat(quote_time)
        assert payload["quote"]["last"] == 4000
        assert payload["metadata"]["snapshot_observed_at"] == observed
        assert payload["ticks"] == []
        assert payload["account"]["status"] == "unknown"
    assert len(harness.adapter.events.get("reads", [])) == native_before


def test_empty_cache_never_fabricates_a_snapshot(harness, monkeypatch):
    bridge = harness.bridge
    monkeypatch.setattr(bridge, "_build_envelope", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("unavailable")))
    bridge.on_start()
    settle(bridge, expect=lambda: len(harness.sent) == 1)
    for _ in range(3):
        harness.clock.advance(5)
        settle(bridge)
    assert wait_for(lambda: len(harness.sent) == 4, timeout=5)
    for _, payload, _, _ in harness.sent:
        assert "snapshot_observed_at" not in payload.get("metadata", {})
        assert payload["connection"]["snapshots"] == {}
        assert payload.get("account", {}).get("status", "unknown") == "unknown"
        assert "quote" not in payload
    assert bridge.state_map.order_submissions == 0


def test_empty_position_without_query_completion_does_not_prove_flat(adapter):
    bridge = configure(adapter.module)
    adapter.infini.investors = [{"InvestorID": "only"}]
    adapter.infini.positions["only"] = []
    for account in ({}, {"Available": 0, "Balance": 0}):
        adapter.infini.accounts["only"] = account
        payload = bridge._build_envelope([], heartbeat=True)
        assert payload["position"]["status"] == "unknown"
        assert payload["position"]["net"] is None
        assert payload["position"]["long"]["total"] is None
        assert payload["active_orders"]["count"] is None


def test_all_cached_symbols_are_repeated_and_response_queue_stays_bounded(harness):
    bridge, clock, sent = harness.bridge, harness.clock, harness.sent
    extra = [f"SHFE.rb2610C{3000 + index}" for index in range(15)]
    bridge.set_params({"additional_symbols": ";".join(extra)})
    bridge.on_start()
    settle(bridge, cycles=80, expect=lambda: len({(e[1]["exchange"], e[1]["instrument_id"]) for e in sent}) == 16)
    symbols = {(entry[1]["exchange"], entry[1]["instrument_id"]) for entry in sent}
    assert len(symbols) == 16
    for round_number in range(3):
        clock.advance(5)
        settle(bridge, cycles=120,
               expect=lambda n=round_number: len(sent) >= 16 * (n + 2))
        batch = sent[16 * (round_number + 1):16 * (round_number + 2)]
        assert {(entry[1]["exchange"], entry[1]["instrument_id"]) for entry in batch} == symbols
    assert bridge._response_queue.qsize() <= bridge._response_queue.maxsize == 4
    assert bridge._request_queue.qsize() <= bridge._request_queue.maxsize <= 100
    assert bridge._dropped_responses > 0


def test_stop_restart_discards_old_epoch_quotes_and_cached_account(harness):
    bridge, clock, sent = harness.bridge, harness.clock, harness.sent
    harness.adapter.infini.investors = [{"InvestorID": "fixture-only"}]
    harness.adapter.infini.accounts["fixture-only"] = {"Available": 10}
    bridge.on_start()
    settle(bridge)
    bridge.on_tick(tick(datetime=clock.wall.isoformat()))
    first_epoch = sent[0][1]["connection"]["session_epoch"]
    bridge.on_stop()
    count = len(sent)
    clock.advance(45)
    assert bridge.worker_thread is None
    assert len(sent) == count
    harness.adapter.infini.investors = []
    bridge.set_params({"instrument_id": "ag2610"})
    bridge.on_start()
    settle(bridge)
    clock.advance(5)
    settle(bridge, expect=lambda: len(sent) == count + 2)
    for _, payload, _, _ in sent[count:]:
        assert payload["connection"]["session_epoch"] != first_epoch
        assert payload["instrument_id"] == "ag2610"
        assert "quote" not in payload and payload["ticks"] == []
        assert payload["account"]["status"] == payload["position"]["status"] == "unknown"
        assert payload["orders"] == payload["trades"] == []


def test_stop_discards_queued_jobs_and_late_inflight_reply(harness):
    bridge = harness.bridge
    entered, release = threading.Event(), threading.Event()
    calls = []

    def transport(*args):
        calls.append(args)
        entered.set()
        assert release.wait(2)
        return 200, b'{"signal":"buy","reason":"late"}'

    bridge._transport = transport
    bridge.set_params({"additional_symbols": "SHFE.rb2610C3100"})
    bridge.on_start()
    assert entered.wait(1)
    unblock = threading.Thread(target=lambda: (bridge._stop_event.wait(1), release.set()))
    unblock.start()
    try:
        bridge.on_stop()
    finally:
        release.set()
        unblock.join(timeout=2)
    # With true per-symbol lanes the second startup beat may legitimately
    # begin within milliseconds (before on_stop runs); the stop fence must
    # still ensure none of them produces a state effect afterwards.
    assert 1 <= len(calls) <= 2
    assert bridge._response_queue.empty() and bridge._request_queue.empty()
    assert bridge.state_map.blocked_candidates == 0, "a reply arriving after stop must not update UI state"
    assert bridge.state_map.order_submissions == 0


@pytest.mark.parametrize("callback", ["on_contract_status", "on_order", "on_order_trade", "on_order_cancel", "on_cancel", "on_trade", "on_error"])
def test_native_context_callbacks_drain_replies_and_keep_execution_disabled(adapter, monkeypatch, callback):
    bridge = configure(adapter.module)
    monkeypatch.setattr(bridge, "_start_worker", lambda: None)
    bridge.on_start()
    bridge._put_response(adapter.module._WorkerResponse("heartbeat", 200, b'{"signal":"buy","reason":"candidate"}'))
    bridge.params_map.enable_trading = True
    bridge.trading = True
    try:
        getattr(bridge, callback)({"Exchange": "SHFE", "InstrumentID": "rb2610", "Status": "continuous", "errCode": "4"})
        assert bridge._response_queue.empty()
        assert bridge.state_map.connection_status != "connected"
        assert bridge.state_map.blocked_candidates == 1
        assert bridge.state_map.blocked_candidates == 1
        assert bridge.state_map.last_signal == "hold"
        assert bridge.params_map.enable_trading is False and bridge.trading is False
        assert all(thread is threading.main_thread() for thread in adapter.events["status"])
        assert all(thread is threading.main_thread() for thread, _ in adapter.events["output"])
    finally:
        bridge.on_stop()


@pytest.mark.parametrize("endpoint", ["heartbeat", "analyze"])
def test_service_receipt_expires_and_reads_never_change_reception_counters(endpoint):
    clock = MutableClock()
    service = BridgeService(clock=clock, allowed_symbols=["SHFE.rb2610"])
    try:
        assert service.status()["client"]["status"] == "awaiting_client"
        assert service.health()["client"]["last_received_at"] is None
        getattr(service, endpoint)(_payload())
        client = service.status()["client"]
        assert client["status"] == "client_seen" and client["age_seconds"] == 0
        assert client["last_received_at"] == FIXED_NOW.isoformat().replace("+00:00", "Z")
        assert client["stale_after_seconds"] == 15
        before = service.status()["counters"]
        clock.advance(31)
        for _ in range(3):
            status, health = service.status(), service.health()
            assert status["client"]["status"] == health["client_status"] == "client_stale"
            assert status["client"]["age_seconds"] == health["client"]["age_seconds"] == 31
            assert status["counters"] == before
        assert status["contracts"]["SHFE.rb2610"]["transport"]["status"] == "client_stale"
    finally:
        service.close()


@pytest.mark.parametrize("fault", ["backward_wall", "backward_mono", "nan", "inf", "naive", "missing", "raises"])
@pytest.mark.parametrize("at_receipt", [False, True])
def test_clock_anomalies_never_report_fresh_liveness_or_snapshots(fault, at_receipt):
    clock = MutableClock()
    service = BridgeService(clock=clock, allowed_symbols=["SHFE.rb2610"])
    payload = _payload(ticks=[], quote={"time": FIXED_NOW.isoformat(), "last": 4000},
                       metadata={"snapshot_observed_at": FIXED_NOW.isoformat()})
    try:
        if not at_receipt:
            service.heartbeat(payload)
        if fault == "backward_wall":
            clock.wall -= timedelta(seconds=3)
        elif fault == "backward_mono":
            clock.elapsed = -1
        elif fault in {"nan", "inf"}:
            clock.elapsed = float(fault)
        elif fault == "naive":
            clock.wall = clock.wall.replace(tzinfo=None)
        elif fault == "missing":
            clock.wall = None
        else:
            clock.now = lambda: (_ for _ in ()).throw(ValueError("clock unavailable"))
        if at_receipt:
            service.heartbeat(payload)
        snapshot = service.status()
        # A backwards wall value on first receipt is only detectable relative to
        # the future observation timestamp, not as a transport clock reversal.
        if not (at_receipt and fault == "backward_wall"):
            assert snapshot["client"]["status"] != "client_seen"
            assert service.health()["client_status"] != "client_seen"
        assert snapshot["recent_receipt"]["freshness"]["snapshot"]["status"] != "fresh"
        assert snapshot["recent_receipt"]["freshness"]["quote"]["status"] != "fresh"
        json.dumps(snapshot, allow_nan=False)
    finally:
        service.close()


def test_real_json_heartbeat_separates_live_transport_from_old_observations(harness, running_bridge):
    address, service, calls, _audit = running_bridge
    bridge, clock = harness.bridge, harness.clock
    service.clock = clock
    delivered = []

    def transport(url, payload, timeout):
        result = harness.adapter.module._default_transport(url, payload, timeout)
        delivered.append(deepcopy(payload))
        return result

    bridge._transport = transport
    bridge.set_params({"api_url": f"http://{address[0]}:{address[1]}/analyze"})
    bridge.on_start()
    settle(bridge)
    bridge.on_tick(tick(datetime=clock.wall.isoformat()))
    clock.advance(45)
    settle(bridge)
    assert len(delivered) == 2
    assert parse_json_body(json.dumps(delivered[-1], allow_nan=False).encode())["heartbeat"] is True
    before = service.status()["counters"]
    for path in ("/status", "/health"):
        code, body, _ = _request(address, "GET", path)
        assert code == 200 and body["client"]["status"] == "client_seen"
        assert body["client"]["age_seconds"] == 0
        receipt = body["recent_receipt"]
        assert receipt["freshness"]["snapshot"]["status"] == "stale"
        assert receipt["freshness"]["snapshot"]["age_seconds"] == 45
        assert receipt["freshness"]["quote"]["status"] == "stale"
        assert receipt["snapshot"]["account"]["status"] == "unknown"
        assert receipt["snapshot"]["position"]["status"] == "unknown"
        assert receipt["source_verified"] is False
        assert body["observer_only"] is True and body["execution_enabled"] is False
    assert service.status()["counters"] == before
    assert before["protocol_errors"] == 0 and before["heartbeat_requests"] == 2
    assert service.status()["telemetry"]["order_submissions_verified"] is False
    assert service.status()["telemetry"]["native_order_submission_count"] is None
    assert service.status()["live_client_verified"] is False
    assert calls == []


def test_test_marker_and_claimed_source_never_verify_native_connection():
    service = BridgeService(clock=MutableClock())
    try:
        service.heartbeat(_payload(exchange="TEST", ticks=[]), source_header="AKObserverBridge")
        status = service.status()
        assert status["client"]["source_header_trust"] == "claimed_unverified"
        assert status["recent_receipt"]["synthetic"] is True
        assert status["recent_receipt"]["source_verified"] is False
        assert status["live_client_verified"] is False
        assert status["execution_enabled"] is False
        assert status["telemetry"]["order_submissions_verified"] is False
    finally:
        service.close()


@pytest.mark.parametrize("value", ["x" * 1000, 123, True, [], {}, "not-a-time", "2026-09-09T01:00:00"])
def test_observation_time_metadata_is_bounded_and_invalid_values_are_unknown(value):
    request = normalize_payload(_payload(metadata={"snapshot_observed_at": value}), now=FIXED_NOW)
    assert request.payload["metadata"]["snapshot_observed_at"] is None


def test_metadata_width_and_numeric_values_are_bounded():
    with pytest.raises(ProtocolViolation, match="invalid_metadata"):
        normalize_payload(_payload(metadata={f"key_{i}": i for i in range(33)}))
    result = normalize_payload(_payload(metadata={"oversized": 10 ** 200}))
    assert result.payload["metadata"]["oversized"] is None


def test_idle_link_uses_reply_time_and_expires_without_new_success(harness):
    bridge, clock = harness.bridge, harness.clock
    bridge.on_start()
    settle(bridge)
    for _ in range(2):
        clock.advance(5)
        settle(bridge)
    assert len(harness.sent) == 3
    assert bridge.connection_status()["connection"] == "roundtrip"
    bridge._transport = lambda *args: (_ for _ in ()).throw(TimeoutError("offline"))
    clock.advance(15)
    settle(bridge)
    assert bridge.connection_status()["connection"] == "roundtrip"
    clock.advance(0.001)
    settle(bridge)
    assert bridge.connection_status()["connection"] == "stale"
    assert bridge.connection_status()["execution_enabled"] is False


def test_adapter_rejects_old_response_sequence_and_epoch(harness):
    bridge = harness.bridge
    bridge.on_start()
    settle(bridge)
    old = bridge._response_queue.get_nowait()
    bridge._response_queue.task_done()
    harness.clock.advance(5)
    settle(bridge)
    new = bridge._response_queue.get_nowait()
    bridge._response_queue.task_done()
    bridge._handle_response(new)
    state = bridge.state_map.model_dump()
    bridge._handle_response(old)
    assert bridge.state_map.model_dump() == state
    bridge.on_stop()
    bridge.on_start()
    settle(bridge)
    bridge._drain_responses()
    state = bridge.state_map.model_dump()
    bridge._handle_response(new)
    assert bridge.state_map.model_dump() == state
    assert len(harness.adapter.events["sub"]) == 2
    worker = bridge.worker_thread
    bridge.on_start()
    assert bridge.worker_thread is worker and len(harness.adapter.events["sub"]) == 2


def test_nonmarket_callback_keeps_quote_sampling_and_source_time(harness):
    bridge, clock = harness.bridge, harness.clock
    bridge.on_start()
    settle(bridge)
    bridge.on_tick(tick(datetime=clock.wall.isoformat()))
    clock.advance(5)
    settle(bridge)
    quoted = deepcopy(harness.sent[-1][1]["connection"]["snapshots"]["quote"])
    bridge.on_contract_status({"Exchange": "SHFE", "InstrumentID": "rb2610", "Status": "continuous"})
    settle(bridge)
    assert harness.sent[-1][1]["connection"]["snapshots"]["quote"] == quoted
    assert harness.sent[-1][1]["connection"]["snapshots"]["account"]["source_at"] is None


@pytest.mark.parametrize("method,args", [("on_start", ()), ("on_tick", ({},)), ("on_contract_status", ({},)),
                                        ("on_order", ({},)), ("on_trade", ({},)), ("on_error", ({},)),
                                        ("_build_envelope", ([],)), ("update_status_bar", ()),
                                        ("_read_contract", ()), ("_read_account_snapshot", ())])
def test_wrong_thread_cannot_reach_native_or_gui(adapter, method, args):
    bridge = configure(adapter.module)
    before = {key: len(value) for key, value in adapter.events.items()}
    errors = []

    def invoke():
        try:
            getattr(bridge, method)(*args)
        except RuntimeError as exc:
            errors.append(str(exc))

    thread = threading.Thread(target=invoke)
    thread.start()
    thread.join(timeout=2)
    assert errors == ["native_callback_thread_required"]
    assert {key: len(value) for key, value in adapter.events.items()} == before


@pytest.mark.parametrize("field", ["sampled_at", "source_at", "received_at"])
@pytest.mark.parametrize("ahead,expected", [(2, "fresh"), (2.001, "future"), (-3, "fresh")])
def test_component_clock_fields_keep_own_future_boundary(field, ahead, expected):
    clock = MutableClock()
    service = BridgeService(clock=clock)
    stamp = (clock.wall + timedelta(seconds=ahead)).isoformat()
    component = {"sampled_at": clock.wall.isoformat(), "source_at": clock.wall.isoformat(),
                 "received_at": clock.wall.isoformat(), "source": "pythongo", "completeness": "unverified"}
    component[field] = stamp
    try:
        service.heartbeat({"schema_version": 2, "connection": {"snapshots": {"account": component}}})
        entry = service.status()["recent_receipt"]
        key = {"sampled_at": "sampling", "source_at": "source", "received_at": "received"}[field]
        assert entry["freshness"]["components"]["account"][key]["status"] == expected
        assert entry["transport"]["age_seconds"] == 0
        assert entry["snapshot"]["account"]["status"] == "unknown"
    finally:
        service.close()


def test_protocol_retains_only_bounded_connection_claims_and_does_not_verify():
    raw = {"instance_id": "instance", "session_epoch": "epoch", "sequence": 7, "request_id": "req",
           "sent_at": FIXED_NOW.isoformat(), "verified": True,
           "snapshots": {"account": {"sampled_at": FIXED_NOW.isoformat(), "source_at": None,
                                      "received_at": FIXED_NOW.isoformat(), "snapshot_id": "sample",
                                      "source": "pythongo", "completeness": "full_account"}}}
    normalized = normalize_payload(_payload(connection=raw))
    value = normalized.payload["connection"]
    assert value["instance_id"] == "instance" and value["session_epoch"] == "epoch"
    assert value["sequence"] == 7 and value["request_id"] == "req"
    assert "verified" not in value
    assert value["snapshots"]["account"]["source_at"] is None
    assert value["snapshots"]["account"]["completeness"] == "unverified"
    for invalid in (-1, True, 1.5, 2 ** 60, "1"):
        value = normalize_payload(_payload(connection={**raw, "sequence": invalid})).payload["connection"]
        assert value["sequence"] is None
    with pytest.raises(ProtocolViolation, match="invalid_connection"):
        normalize_payload(_payload(connection={str(index): None for index in range(13)}))


def test_slow_symbols_cannot_starve_healthy_heartbeat(harness):
    """MH03: healthy beats stay due-driven under sustained slow-lane pressure.

    The shared virtual clock advances only in controlled test phases (to the
    next due instant, one heartbeat interval at a time); slow transports
    occupy their lanes with bounded real blocks and never add shared virtual
    time, so every healthy sent-to-sent gap is exactly one interval regardless
    of real thread scheduling. Completion is bound to observed healthy rounds
    and observed slow-lane service, never to a global call count, and every
    wait is a real-time watchdog that fails with a bounded scheduling trace
    instead of relaxing the cadence assertion.
    """
    bridge, clock = harness.bridge, harness.clock
    interval = harness.adapter.module.HEARTBEAT_INTERVAL_SECONDS
    bridge.set_params({"additional_symbols": ";".join(f"SHFE.rb2610C{3000 + index}" for index in range(15))})
    calls = []
    guard = threading.Lock()
    healthy_beat = threading.Event()
    slow = {"entered": 0, "inflight": 0}
    gate = threading.Event()          # holds one designated slow request mid-transport
    gated_seen = threading.Event()    # that request is provably in flight
    gate_target = [None]              # entry index that must block (test-armed)
    trace: list[str] = []
    slow_block_seconds = 0.04         # real per-call lane occupation, bounded
    total_deadline = time.monotonic() + 30.0  # watchdog for the whole scenario

    def note(message):
        # Bounded replayable scheduling trace; symbols/rounds only, no payloads.
        if len(trace) < 256:
            with guard:
                trace.append(f"{message} virtual={clock.elapsed - 100:.3f} "
                             f"healthy={sum(1 for symbol, _ in calls if symbol == 'rb2610')} "
                             f"slow_entered={slow['entered']} slow_inflight={slow['inflight']}")

    def healthy_lane_busy():
        lane = bridge._lanes.get(("SHFE", "rb2610"))
        return bool(lane) and (lane.get("inflight") or lane.get("job") is not None
                               or lane.get("next") is not None or lane.get("deferred") is not None)

    def transport(url, payload, timeout):
        symbol = payload["instrument_id"]
        with guard:
            index = slow["entered"]
            calls.append((symbol, clock.elapsed - 100))
        if symbol == "rb2610":
            healthy_beat.set()
            return 200, b'{"signal":"hold","reason":"ok"}'
        with guard:
            slow["entered"] += 1
            slow["inflight"] += 1
        gated = gate_target[0] is not None and index == gate_target[0]
        try:
            if gated:
                gated_seen.set()
                # Forced interleave: this request stays genuinely in flight
                # across the round's clock advance (bounded so a lost release
                # can never hang the lane or the stop join).
                gate.wait(2.0)
            else:
                # Ordinary in-flight pressure: the request really blocks its
                # lane for a bounded real window instead of costing zero time,
                # and it never advances the shared virtual clock.
                deadline = time.monotonic() + slow_block_seconds
                while time.monotonic() < deadline and not bridge._stop_event.is_set():
                    time.sleep(0.005)
        finally:
            with guard:
                slow["inflight"] -= 1
            if gated:
                gate_target[0] = None
        raise TimeoutError("synthetic network timeout")

    def entered():
        with guard:
            return slow["entered"]

    bridge._transport = transport
    lane_threads = []
    bridge.on_start()
    try:
        note("startup")
        # Startup either serves the primary symbol from its queued one-shot, or
        # (when startup sampling rotated the epoch and re-armed every due time)
        # the first beat waits for the due instant; advance exactly to it.
        startup_due = None
        with bridge._snapshot_lock:
            startup_due = bridge._heartbeat_due.get(("SHFE", "rb2610"))
        if startup_due is not None and startup_due > clock.elapsed:
            clock.advance(startup_due - clock.elapsed)
            note("startup-advanced-to-due")
        assert healthy_beat.wait(10), "no healthy startup beat; " + "; ".join(trace)
        healthy_beat.clear()
        for round_number in range(5):
            remaining = total_deadline - time.monotonic()
            assert remaining > 0, f"watchdog exhausted before round {round_number}; " + "; ".join(trace)
            budget = min(10, remaining)
            # Phase 1 - real slow-lane service between beats: with the clock
            # parked on the healthy due instant, the scheduler keeps serving
            # due slow symbols one lane at a time.
            baseline = entered()
            assert wait_for(lambda: entered() >= baseline + 3, timeout=budget), \
                f"round {round_number}: slow lanes not served between beats; " + "; ".join(trace)
            # Phase 2 - force the starvation-relevant interleave: arm the next
            # slow entry to hold mid-transport across the advance.
            with guard:
                gate_target[0] = slow["entered"]
            gate.clear()
            gated_seen.clear()
            assert gated_seen.wait(budget), \
                f"round {round_number}: no slow request reached in-flight hold; " + "; ".join(trace)
            assert wait_for(lambda: not healthy_lane_busy(), timeout=budget), \
                f"round {round_number}: healthy lane never settled; " + "; ".join(trace)
            # Phase 3 - advance exactly one heartbeat interval to the healthy
            # due instant while a slow request is provably in flight, then
            # release it; nothing else moves the clock, so the healthy
            # sent_mono lands exactly on the due instant.
            clock.advance(interval)
            gate.set()
            note(f"round-{round_number}-advanced")
            assert healthy_beat.wait(budget), \
                f"round {round_number}: healthy heartbeat starved at the due instant; " + "; ".join(trace)
            healthy_beat.clear()
            note(f"round-{round_number}-beat")
        final_entered = entered()
        # Sustained blocking/timeout pressure across the whole scenario.
        assert final_entered >= 15, f"slow lanes under-served: {final_entered}; " + "; ".join(trace)
        lane_threads = [lane.get("thread") for lane in bridge._lanes.values()]
    finally:
        # Release everything the test holds, then take the real stop path so
        # worker and lanes join inside the production stop budget.
        gate.set()
        bridge._stop_event.set()  # unblock any slow transport still looping
        bridge.on_stop()
    healthy = [at for symbol, at in calls if symbol == "rb2610"]
    gaps = [later - earlier for earlier, later in zip(healthy, healthy[1:])]
    native_threads = [item for items in harness.adapter.events.values() for item in items
                      if isinstance(item, tuple) and any(isinstance(part, threading.Thread) and part is not threading.main_thread() for part in item)]
    assert not native_threads
    assert len(healthy) >= 2
    assert max(gaps) <= 5.05, f"healthy cadence gaps {gaps}; " + "; ".join(trace)
    # No lane/worker thread of this bridge may outlive the stop (no leaks).
    assert all(thread is None or not thread.is_alive() for thread in lane_threads), \
        "lane threads leaked past stop; " + "; ".join(trace)
    assert bridge.worker_thread is None or not bridge.worker_thread.is_alive()


def test_healthy_lane_blockage_is_detected_not_masked(harness):
    """MH03 counterexample: a healthy lane forbidden to progress must yield
    zero completed beats in the observation window, so the paced test above
    cannot pass vacuously through its beat handshake."""
    bridge, clock = harness.bridge, harness.clock
    bridge.set_params({"additional_symbols": ";".join(f"SHFE.rb2610C{3000 + index}" for index in range(15))})
    entered = []
    completed = []
    guard = threading.Lock()
    blocked = threading.Event()

    def transport(url, payload, timeout):
        symbol = payload["instrument_id"]
        if symbol == "rb2610":
            with guard:
                entered.append(symbol)
            blocked.wait(5.0)  # healthy lane deliberately cannot progress
            with guard:
                completed.append(symbol)
            return 200, b'{"signal":"hold","reason":"ok"}'
        return 200, b'{"signal":"hold","reason":"ok"}'

    bridge._transport = transport
    bridge.on_start()
    try:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            clock.advance(5.0)
            time.sleep(0.05)
        with guard:
            entries, beats = list(entered), list(completed)
        # The lane may be engaged (entered) and blocked, but no beat may
        # complete: the paced test's completion handshake can only pass when
        # the healthy lane genuinely progresses.
        assert beats == [], \
            f"blocked healthy lane still completed beats: {beats} (entries={entries})"
    finally:
        blocked.set()
        bridge.on_stop()


def test_non_hold_reply_and_stop_do_not_show_connected(harness):
    bridge = harness.bridge
    bridge._transport = lambda *args: (200, b'{"signal":"buy","reason":"candidate"}')
    bridge.on_start()
    assert wait_for(lambda: not bridge._response_queue.empty())
    bridge._drain_responses()
    assert bridge.state_map.last_signal == "hold"
    assert bridge.state_map.blocked_candidates == 1
    assert bridge.state_map.connection_status != "connected"
    assert bridge.connection_status()["connection"] == "service_only"
    bridge._transport = lambda *args: (200, b'{"signal":"hold","reason":"ok"}')
    bridge._enqueue_heartbeat(("SHFE", "rb2610"))
    assert wait_for(lambda: bridge._response_queue.qsize() >= 1)
    bridge._drain_responses()
    assert bridge.state_map.connection_status == "connected"
    bridge.on_stop()
    assert bridge.state_map.connection_status != "connected"
    assert bridge.connection_status()["connection"] == "offline"


@pytest.mark.parametrize("quote_time", [None, "invalid-time"])
def test_quote_without_valid_time_cannot_retain_ready_status(quote_time):
    clock = MutableClock()
    quote = {"status": "ready", "last": 4000}
    if quote_time is not None:
        quote["time"] = quote_time
    service = BridgeService(clock=clock, allowed_symbols=["SHFE.rb2610"])
    try:
        reply = service.heartbeat(_payload(ticks=[], quote=quote))
        assert reply["signal"] == "hold"
        receipt = service.status()["recent_receipt"]
        assert receipt["snapshot"]["quote"]["status"] != "ready"
        assert receipt["snapshot"]["quote"]["last"] == 4000
        assert receipt["freshness"]["quote"]["status"] == "unknown"
    finally:
        service.close()


def test_evaluator_reference_cannot_rewrite_published_receipt():
    clock = MutableClock()
    retained = []

    def evaluator(payload):
        retained.append(payload)
        return {"candidate_signal": "hold", "reason": "ok"}

    service = BridgeService(evaluator, clock=clock, allowed_symbols=["SHFE.rb2610"])
    try:
        service.analyze(_payload(quote={"time": FIXED_NOW.isoformat(), "last": 4000, "bid1": 3999, "ask1": 4001}))
        assert len(retained) == 1
        before = deepcopy(service.status()["recent_receipt"]["snapshot"]["quote"])
        baseline = deepcopy(service.status())
        clock.advance(31)
        retained[0]["quote"]["time"] = clock.wall.isoformat()
        retained[0]["quote"]["last"] = 7777
        after = service.status()
        assert after["recent_receipt"]["snapshot"]["quote"] == before
        assert after["recent_receipt"]["freshness"]["quote"]["status"] != "fresh"
        assert after["counters"] == baseline["counters"]
    finally:
        service.close()


@pytest.mark.parametrize("method", ["status", "health"])
def test_status_view_cannot_mutate_stored_snapshot(method):
    clock = MutableClock()
    service = BridgeService(clock=clock, allowed_symbols=["SHFE.rb2610"])
    try:
        service.heartbeat(_payload(ticks=[]))
        view = getattr(service, method)()
        view["recent_receipt"]["snapshot"]["account"]["available"] = 987654321
        assert service.status()["recent_receipt"]["snapshot"]["account"]["available"] == 100000
    finally:
        service.close()


@pytest.mark.parametrize("scope", ["replacement", "ambiguous"])
def test_account_scope_change_before_retry_does_not_refresh_old_account(harness, monkeypatch, scope):
    bridge, clock = harness.bridge, harness.clock
    monkeypatch.setattr(bridge, "_start_worker", lambda: None)
    bridge.params_map.batch_size = 1
    harness.adapter.infini.investors = [{"InvestorID": "review-A"}]
    harness.adapter.infini.accounts["review-A"] = {"Available": 123}
    harness.adapter.infini.accounts["review-B"] = {"Available": 456}
    bridge.on_start()
    old_epoch = bridge._session_epoch
    clock.advance(1)
    harness.adapter.infini.investors = [{"InvestorID": "review-B"}]
    if scope == "ambiguous":
        harness.adapter.infini.investors.append({"InvestorID": "review-A"})
    bridge.on_tick(tick(datetime=clock.wall.isoformat(), trading_day="20260914"))
    snapshot = bridge._heartbeat_cache[("SHFE", "rb2610")].payload
    assert bridge._session_epoch != old_epoch
    assert snapshot["account"]["available"] != 123
    assert snapshot["connection"]["session_epoch"] == bridge._session_epoch


def test_missing_trading_day_does_not_hide_next_day_transition(harness, monkeypatch):
    bridge, clock = harness.bridge, harness.clock
    monkeypatch.setattr(bridge, "_start_worker", lambda: None)
    bridge.params_map.batch_size = 1
    bridge.on_start()
    bridge.on_tick(tick(datetime=clock.wall.isoformat(), trading_day="20260914"))
    bridge.on_order({"Exchange": "SHFE", "InstrumentID": "rb2610", "OrderID": "review-old-day", "Status": "未成交"})
    epoch = bridge._session_epoch
    assert bridge._contexts[("SHFE", "rb2610")].orders
    clock.advance(86400)
    bridge.on_tick(tick(datetime=clock.wall.isoformat(), trading_day=None))
    bridge.on_tick(tick(datetime=clock.wall.isoformat(), trading_day="20260915"))
    snapshot = bridge._heartbeat_cache[("SHFE", "rb2610")].payload
    assert bridge._session_epoch != epoch
    assert snapshot["orders"] == []
    assert snapshot["trading_day"] == "20260915"


def test_real_lanes_isolate_slow_symbols_and_keep_healthy_cadence(adapter, monkeypatch):
    """Real threads, real clock, event-paced slow transports (beyond the fake
    40-call probes): startup breadth, tail-symbol cadence, analyze progress,
    bounded true concurrency, epoch re-arm and bounded concurrent stop."""
    monkeypatch.setattr(adapter.module, "HEARTBEAT_INTERVAL_SECONDS", 0.5)
    bridge = configure(adapter.module)
    bridge.set_params({"additional_symbols": ";".join(f"SHFE.rb2610C{3000 + index}" for index in range(15))})
    bridge.params_map.batch_size = 1
    guard = threading.Lock()
    state = {"active": 0, "peak": 0}
    calls = []

    def transport(url, payload, timeout):
        with guard:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            calls.append((payload["instrument_id"], payload.get("heartbeat", False), time.monotonic()))
        try:
            if payload["instrument_id"] != "rb2610C3014":
                bridge._stop_event.wait(0.35)
                raise TimeoutError("synthetic slow offline failure")
            return 200, b'{"signal":"hold","reason":"ok"}'
        finally:
            with guard:
                state["active"] -= 1

    bridge._transport = transport
    bridge.on_start()
    bridge.on_tick(tick(datetime=datetime.now(timezone.utc).isoformat(), trading_day="20260914"))
    time.sleep(1.4)
    bridge._rotate_observation_epoch()
    time.sleep(1.4)
    stop_started = time.monotonic()
    bridge.on_stop()
    stop_elapsed = time.monotonic() - stop_started
    healthy = [at for symbol, _, at in calls if symbol == "rb2610C3014"]
    gaps = [later - earlier for earlier, later in zip(healthy, healthy[1:])]
    symbols = {symbol for symbol, _, _ in calls}
    analyzed = sum(1 for _, heartbeat, _ in calls if not heartbeat)
    post_stop = [call for call in calls if call[2] > stop_started + 0.25]
    assert len(symbols) == 16, "startup breadth lost"
    assert analyzed >= 1, "queued analyze never progressed"
    assert len(healthy) >= 4 and max(gaps) <= 1.2, f"tail cadence broken: {gaps}"
    assert sorted(gaps)[len(gaps) // 2] <= 0.7, f"median cadence broken: {gaps}"
    assert sum(g > 0.85 for g in gaps) <= 1, f"more than one starved beat: {gaps}"
    assert 4 <= state["peak"] <= 16, f"concurrency out of bounds: {state['peak']}"
    assert stop_elapsed < 2.5, f"stop waited serially: {stop_elapsed}"
    assert post_stop == [], "transport ran after stop completed"


def test_response_drained_after_link_ttl_cannot_show_connected(harness):
    bridge, clock = harness.bridge, harness.clock
    bridge.on_start()
    settle(bridge)
    clock.advance(16)
    bridge._drain_responses()
    assert bridge.state_map.connection_status != "connected"
    assert bridge.connection_status()["connection"] == "stale"


def test_delayed_or_backward_transport_reply_cannot_establish_roundtrip(harness):
    bridge, clock = harness.bridge, harness.clock

    def transport(*args):
        clock.advance(11)
        return 200, b'{"signal":"hold","reason":"late"}'

    bridge._transport = transport
    # Run only one fake request so its 11s clock advance cannot create a loop.
    bridge._enqueue("heartbeat", bridge._build_envelope([], heartbeat=True), bridge.params_map.api_url)
    bridge._start_worker()
    settle(bridge)
    assert bridge._last_roundtrip is None


def test_trading_day_change_expires_cached_account_and_old_session(harness):
    bridge, clock = harness.bridge, harness.clock
    bridge.on_start()
    settle(bridge)
    bridge.on_tick(tick(datetime=clock.wall.isoformat(), trading_day="20260909"))
    clock.advance(5)
    settle(bridge)
    previous = harness.sent[-1][1]["connection"]["session_epoch"]
    bridge.on_tick(tick(datetime=clock.wall.isoformat(), trading_day="20260910"))
    clock.advance(5)
    settle(bridge)
    payload = harness.sent[-1][1]
    assert payload["connection"]["session_epoch"] != previous
    assert payload["trading_day"] == "20260910"
    assert payload["connection"]["snapshots"]["account"]["source_at"] is None
    assert payload["position"]["status"] == "unknown"
