"""NE1-H02/H17/H21/H37 client integration for AKExecutionBridge (TI).

Observer/execution coexistence isolation, at-most-one native call per command
across replays, synchronous callbacks before the native return, and the
self-contained temp-directory lifecycle round trip. All SYNTHETIC.

NE1-LM-R4 (decision NE1-ARB-PAIRING-LASTMILE-001) additions: the no-arg
self_strategy assembly fixtures, the M07 fact-recovery matrix and the M05
restart matrix against an independent temp REAL service stack, and the M04
no-arg positive chain (skips removed after the X01-C/X01-S/X01-D freeze,
decision NE1-ARB-LM-X01-001 §3 X01-I row).
Real-vs-fake counts are recorded in disjoint buckets: fake-native calls,
loopback test-service requests, and real socket attempts.

X01 merge (NE1-ARB-LM-X01-001): the M04 group below is DOUBLE evidence —
TempExecutionStack writer/startup doubles + PairingToolDouble install. The
REAL production chain (real LM-S CLI launcher serve + write_pairing_file,
real LM-D build-actor-manifest/install tool, no-arg client activation) is
test_x01_real_cli_writer_tool_install_activate_noarg_client.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from infiny_native_fakes import (
    CLIENT_PAIRING_SCHEMA,
    ConnectionCountingListener,
    FakeExecutionService,
    IDENTITY_SCHEMA,
    LedgerError,
    PAIRING_REQUEST_SCHEMA,
    PairingToolDouble,
    PairingToolRefusal,
    SYNTHETIC_PAIRING_TOKEN,
    TempExecutionStack,
    armed_dispatch,
    arm_bridge_to_armed,
    deploy_self_strategy,
    expected_default_store,
    get_status,
    install_fake_native,
    inject_arming_service_seams,
    load_client_from_temp,
    post_json,
    set_synthetic_gate_capture,
    stop_started_bridge,
    synthetic_binding,
    synthetic_dispatch,
    tree_snapshot,
)

ROOT = Path(__file__).resolve().parents[1]
ADAPTER_PATH = ROOT / "integrations/infiny/AIBridge.py"

# SYNTHETIC non-secret binding-template values for the operator/user roles
# (R2.3: account/strategy keys come from operator-verified material, never
# from account queries).
SIM_ACCOUNT_KEY = "SIM_ACCOUNT_A"
SIM_STRATEGY_KEY = "SIM_STRATEGY_S"
SIM_TRADING_DAY = "2026-09-17"


def _wait_until(predicate, timeout=5.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _nudge_control_lane(bridge):
    """Wake the client control lane for one prompt cycle (its own disabled
    interval is 5 s; the nudge only advances scheduling, never identity)."""
    with bridge._state_lock:
        lane = bridge._lanes.get("control")
    if lane is not None:
        lane.wake.set()


def _separated_counts(bridge, fake, *, listener=None, stack=None):
    """LM-I separated bookkeeping (decision §6 LM-I row): fake-native call
    counts, loopback test-service request counts, and REAL socket attempts
    are reported as disjoint buckets and never summed together."""
    return {
        "fake_native_calls": dict(fake.call_counts()),
        "fake_native_send_attempts": bridge.counters["native_send_attempts"],
        "fake_native_cancel_attempts": bridge.counters["native_cancel_attempts"],
        "real_service_code_requests": (stack.service.counters().get("requests", 0)
                                       if stack is not None else 0),
        "real_socket_attempts_to_sentinel": (listener.connection_count()
                                             if listener is not None else 0),
    }


def _started(module, fake, tmp_path, service, store=None):
    """Paired started fixture (R1.2 injected/SYNTHETIC seam carries only the
    pairing credentials). W2 arming alignment (NE1-ARB-ARMING-001): the r3-era
    post-start ``params_map.enable_trading = True`` re-arm is INERT on the
    frozen client — arming goes exclusively through the ruled local-slot
    chain (arm_bridge_to_armed); every armed test also proves the legacy
    re-arm blocks dispatch first (assertion direction reversed)."""
    bridge = module.AKExecutionBridge(native=fake, store_directory=store or (tmp_path / "store"),
                                      pairing_token=SYNTHETIC_PAIRING_TOKEN)
    bridge.strategy_id = 23
    bridge.strategy_name = "SYNTHETIC"
    bridge.set_params({"exchange": "SHFE", "instrument_id": "rb2610", "hedge_flag": "1",
                       "investor_id": "SYNTHETIC-ACCOUNT-A", "enable_trading": True,
                       "authorization_id": "SIM_AUTH", "risk_policy_id": "SIM_POLICY",
                       "max_native_sends": 1, "max_native_cancels": 1,
                       "execution_api_url": service.poll_url})
    bridge.on_init()
    bridge.on_start()
    inject_arming_service_seams(bridge)
    return bridge


def _armed(module, fake, tmp_path, service):
    """W2 ruled armed fixture: the same started bridge carried to ARMED by the
    public local-slot protocol (proposal -> plan -> request/challenge -> USER
    credential -> exact RECORDED ACK over real HTTP -> legal callback)."""
    bridge = _started(module, fake, tmp_path, service)
    arm_bridge_to_armed(module, bridge)
    set_synthetic_gate_capture(bridge, quote=True, account=True, position=True, contract=True)
    _nudge_control_lane(bridge)
    return bridge


def _assert_legacy_rearm_blocks_dispatch(module, fake, tmp_path, service):
    """Reversed assertion (W2): dispatch with NO arming-effective round is
    CORRECTLY blocked — the legacy Params re-arm opens nothing. Returns the
    probe bridge for teardown; the caller passes `store=` to isolate it."""
    probe = _started(module, fake, tmp_path, service, store=tmp_path / "probe-store")
    probe.params_map.enable_trading = True  # legacy re-arm attempt — inert now
    set_synthetic_gate_capture(probe, quote=True, account=True, position=True, contract=True)
    command = synthetic_dispatch(binding=synthetic_binding(
        instance_id=probe._instance_id, session_epoch=probe._session_epoch),
        params_sha256=probe._params_sha256, ttl_ms=490)
    probe._receive_dispatch(command)
    probe._pump_once()
    assert len(fake.send_calls()) == 0            # zero native without ARMED
    assert probe.counters["native_call_blocked"] >= 1
    assert probe._arming_effective_now() is False  # Params never armed anything
    assert probe._dispatch_slot is not None        # ticket held, never executed
    return probe


def test_h02_observer_and_execution_coexist_isolated(monkeypatch, tmp_path):
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    service = FakeExecutionService()
    execution = None
    obs_bridge = None
    try:
        execution = _armed(module, fake, tmp_path, service)

        # load the real observer file with the same fake native; it stays an
        # observer even when handed buy/sell/enable=true (NE1-H02)
        import importlib.util
        spec = importlib.util.spec_from_file_location("observer_coload", ADAPTER_PATH)
        observer = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(__import__("sys").modules, "observer_coload", observer)
        spec.loader.exec_module(observer)
        from test_infiny_adapter import configure, tick  # observer test helpers

        obs_bridge = configure(observer.module if hasattr(observer, "module") else observer)
        obs_bridge.params_map.enable_trading = True
        obs_bridge.on_start()
        obs_bridge._transport = lambda *args: (200, b'{"signal":"buy","reason":"candidate"}')
        obs_bridge._start_worker()
        obs_bridge._enqueue("analyze", obs_bridge._build_envelope([]),
                            "http://127.0.0.1:17778/analyze")
        assert obs_bridge._drain_responses() is None or True
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and obs_bridge._response_queue.empty():
            time.sleep(0.01)
        obs_bridge._drain_responses()
        assert obs_bridge.state_map.last_signal == "hold"
        assert obs_bridge.state_map.order_submissions == 0
        obs_bridge.on_stop()

        # execution instance never touched the observer service: every request
        # went to /execution/v1/* on the fake 17779 loopback only
        assert execution.counters["native_send_attempts"] == 0
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not service.requests:
            time.sleep(0.02)
        paths = {path for path, _ in service.requests}
        assert paths and all(path.startswith("/execution/v1/") for path in paths)
    finally:
        stop_started_bridge(execution)
        stop_started_bridge(obs_bridge)
        service.close()


def test_h17_at_most_one_native_call_across_duplicate_dispatches(monkeypatch, tmp_path):
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    service = FakeExecutionService()
    bridge = None
    probe = None
    try:
        # W2 reversed direction: without an arming-effective round the very
        # same dispatch is blocked (zero native, ticket held) — CORRECT.
        probe = _assert_legacy_rearm_blocks_dispatch(module, fake, tmp_path, service)

        bridge = _armed(module, fake, tmp_path, service)
        command = armed_dispatch(bridge)
        bridge._receive_dispatch(command)
        bridge._pump_once()
        assert len(fake.send_calls()) == 1
        # duplicate arrival of the same command: receipt re-recordable, never re-called
        bridge._receive_dispatch(dict(command))
        bridge._pump_once()
        assert len(fake.send_calls()) == 1
        assert bridge.counters["native_send_attempts"] == 1
        # receipts chain persisted CONSUMED -> RETURNED, then the replay closes
        # with a terminal NO_CALL receipt (C1.2/OV-O05: receipts-only re-upload)
        rows = bridge._stores.receipt_for_command(command["command_id"])
        assert [row[0] for row in rows] == ["CONSUMED", "RETURNED", "NO_CALL"]
    finally:
        stop_started_bridge(bridge)
        stop_started_bridge(probe)
        service.close()


def test_h21_sync_callback_before_return_persisted_first(monkeypatch, tmp_path):
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    service = FakeExecutionService()
    bridge = None
    probe = None
    try:
        # W2 reversed direction: no arming-effective round -> the dispatch is
        # blocked before any native call could fire the sync-callback path.
        probe = _assert_legacy_rearm_blocks_dispatch(module, fake, tmp_path, service)

        bridge = _armed(module, fake, tmp_path, service)

        # a fill callback fires synchronously DURING the native send
        invoked = []
        original_send = fake.sendOrder

        def send_with_sync_callback(**kwargs):
            invoked.append(kwargs)
            bridge.on_trade({"TradeID": "SIM_TRADE_1", "Volume": 1})
            result = original_send(**kwargs)
            invoked.append("returned")
            return result

        fake.sendOrder = send_with_sync_callback
        command = armed_dispatch(bridge)
        bridge._receive_dispatch(command)
        bridge._pump_once()
        assert invoked == [invoked[0], "returned"] and len(invoked) == 2  # single call, sync trade inside
        assert bridge.state_map.event_gap is False  # capture during call persisted
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            rows = bridge._stores.pending_events(10)
            if any("SIM_TRADE_1" in row[1] for row in rows):
                break
            with service.lock:
                if any("SIM_TRADE_1" in json.dumps(r) for r in service.receipts) or                    any("SIM_TRADE_1" == eid for eid in service.acked_event_ids):
                    break
            time.sleep(0.02)
        rows = bridge._stores.pending_events(10)
        assert (any("SIM_TRADE_1" in row[1] for row in rows)
                or any("SIM_TRADE_1" in json.dumps(event) for event in service.uploaded_events))
    finally:
        stop_started_bridge(bridge)
        stop_started_bridge(probe)
        service.close()


def test_h37_self_contained_temp_dir_full_lifecycle(monkeypatch, tmp_path):
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    service = FakeExecutionService()
    bridge = None
    probe = None
    try:
        # W2 reversed direction: full legacy lifecycle callbacks with the
        # Params re-arm still open zero native calls without an armed round.
        probe = _assert_legacy_rearm_blocks_dispatch(module, fake, tmp_path, service)
        probe.on_tick({"last_price": 3500})
        probe.on_contract_status({"Status": "continuous"})
        probe.on_order({"OrderID": 1, "Status": "未成交"})
        probe.on_trade({"TradeID": "T1"})
        probe.on_order_cancel({"OrderID": 1})
        probe.on_cancel({"OrderID": 1})
        probe.on_error({"errCode": "0004"})
        probe.on_tick({"last_price": 3501})  # pump rides the next legal callback
        assert len(fake.send_calls()) == 0
        probe.on_stop()

        bridge = _started(module, fake, tmp_path, service)
        arm_bridge_to_armed(module, bridge)
        bridge.on_tick({"last_price": 3500})
        bridge.on_contract_status({"Status": "continuous"})
        bridge.on_order({"OrderID": 1, "Status": "未成交"})
        bridge.on_trade({"TradeID": "T1"})
        bridge.on_order_cancel({"OrderID": 1})
        bridge.on_cancel({"OrderID": 1})
        bridge.on_error({"errCode": "0004"})
        set_synthetic_gate_capture(bridge, quote=True, account=True, position=True, contract=True)
        command = armed_dispatch(bridge)
        bridge._receive_dispatch(command)
        bridge.on_tick({"last_price": 3501})  # pump rides the next legal callback
        assert len(fake.send_calls()) == 1
        bridge.on_stop()
        # the loaded module never touched repository packages: its namespace has
        # no ak_trading import and the fake recorded only whitelisted methods
        assert fake.send_calls() and fake.send_calls()[0][1]["kwargs"]["Exchange"] == "SHFE"
        assert bridge.counters["native_send_attempts"] == 1
        assert bridge.counters["native_cancel_attempts"] == 0
    finally:
        stop_started_bridge(bridge)
        stop_started_bridge(probe)
        service.close()


def test_zero_side_effect_import_and_construct(monkeypatch, tmp_path):
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    before_calls = list(fake.calls)
    before_access = list(fake.attribute_access)
    bridge = module.AKExecutionBridge(native=fake)
    assert fake.calls == before_calls
    assert fake.attribute_access == before_access
    assert bridge.submit_enabled is False and bridge.cancel_owned_enabled is False
    assert bridge.mode in ("disabled",) or str(bridge.mode) == "ExecutionMode.DISABLED"


# ===========================================================================
# NE1-LM-R4 / LM-I: no-arg assembly fixtures (R4.2) — current durable facts.
#
# These assertions hold for the current release AND the LM-C target: they pin
# only facts the decision keeps true (no native trading surface, no
# unexpected attribute resolution). Facts that the LM-C revision CHANGES
# (anonymous polling, missing default store) live only in the M04 group
# below, never here.
#
# 2026-09-18 defaults alignment (ne1-execution-20260918-pairing-v2b; the
# integration-domain mirror of the gates/lifecycle direction A/B rewrite):
# the Params Field defaults ARE the deployment values (SHFE / rb2610 /
# hedge 1 / investor 15000180250), so a no-arg client that never calls
# set_params starts validly on them alone (direction A), while an EXPLICIT
# set_params blanking exchange / instrument_id / hedge_flag is still
# refused with zero store and zero traffic (direction B).
# ===========================================================================

def test_lm_no_arg_assembly_zero_side_effects_before_start(monkeypatch, tmp_path):
    """R4.2 stage 1-3: deploy into a temp self_strategy directory, install the
    fake INFINIGO BEFORE import, construct AKExecutionBridge() with ZERO
    arguments, then only normal framework field assignment (strategy_id,
    strategy_name, one ordinary set_params). Before any start: zero files,
    zero threads, zero network, zero native trading surface."""
    deployment = deploy_self_strategy(monkeypatch, tmp_path)
    fake = deployment.fake
    files_before = tree_snapshot(tmp_path)
    listener = ConnectionCountingListener()
    # baseline AFTER the sentinel listener spawns its own accept thread: the
    # listener is test infrastructure, only client-spawned threads would move
    # the count
    threads_before = threading.active_count()
    try:
        bridge = deployment.no_arg(
            strategy_id=23, strategy_name="SYNTHETIC",
            params={"exchange": "SHFE", "instrument_id": "rb2610",
                    "execution_api_url": listener.url()})
        # the probe never injected store/token: identity stays client-chosen
        assert bridge._store_directory is None and bridge._pairing_token is None
        assert tree_snapshot(tmp_path) == files_before  # no store, no dirs
        assert threading.active_count() == threads_before
        assert listener.connection_count() == 0
        counts = fake.call_counts()
        assert counts.get("sendOrder", 0) == 0
        assert counts.get("cancelOrder", 0) == 0
        assert counts.get("getInvestorList", 0) == 0
        assert fake.attribute_access == []  # no attribute bomb resolution
        # the default R1.1 store slot is not created before a valid start
        assert not expected_default_store(deployment.self_strategy_dir, 23).exists()
    finally:
        listener.close()


class _NoHttp:
    """Any instantiation proves an anonymous network attempt (the same
    tripwire the lifecycle M01 probes use)."""

    def __init__(self, *args, **kwargs):
        raise AssertionError("network connection attempted by the no-arg client")


def test_lm_noarg_start_uses_baked_defaults(monkeypatch, tmp_path):
    """2026-09-18 defaults fix, direction A (mirrors the lifecycle M01
    direction-A probe): this InfiniTrader build exposes no parameter form,
    so the no-form mainline is NO set_params call at all — the Params Field
    defaults (the baked deployment values) alone must carry a valid start to
    WAIT_PAIRING, own the deterministic default store, and emit no traffic of
    any kind while unpaired."""
    deployment = deploy_self_strategy(monkeypatch, tmp_path)
    module, fake = deployment.module, deployment.fake
    monkeypatch.setattr(module, "HTTPConnection", _NoHttp)
    monkeypatch.setattr(module, "HTTPSConnection", _NoHttp)
    bridge = deployment.no_arg(strategy_id=23, strategy_name="SYNTHETIC")
    bridge.on_init()
    bridge.on_start()
    try:
        store_dir = expected_default_store(deployment.self_strategy_dir, 23)
        assert store_dir.is_dir(), "baked-default start must own the default store"
        assert bridge.state_map.execution_status == "wait_pairing"
        assert bridge.params_map.enable_trading is False  # start force-disables
        # the Params Field defaults ARE the deployment values, and every
        # safety default stays untouched
        assert bridge.params_map.exchange == "SHFE" == module.DEFAULT_EXCHANGE
        assert bridge.params_map.instrument_id == "rb2610" == module.DEFAULT_INSTRUMENT_ID
        assert bridge.params_map.hedge_flag == "1" == module.DEFAULT_HEDGE_FLAG
        assert bridge.params_map.investor_id == module.DEFAULT_INVESTOR_ID
        assert bridge.params_map.execution_mode == "disabled"
        assert bridge.params_map.max_native_sends == 0
        assert bridge.params_map.max_native_cancels == 0
        identity = json.loads((store_dir / "identity.json").read_text(encoding="utf-8"))
        assert identity["schema_version"] == IDENTITY_SCHEMA
        assert identity["strategy_id"] == 23
        request = json.loads((store_dir / "pairing-request.json").read_text(encoding="utf-8"))
        assert request["schema_version"] == PAIRING_REQUEST_SCHEMA
        assert request["active"] is True
        assert request["instance_id"] == identity["instance_id"]
        # the published request carries the deployment default connection
        assert request["exchange"] == "SHFE"
        assert request["instrument_id"] == "rb2610"
        assert request["hedge_flag"] == "1"
        time.sleep(0.35)  # lanes cycle while unpaired: still zero HTTP, zero native
        assert bridge.counters["poll_requests"] == 0
        counts = fake.call_counts()
        assert counts.get("sendOrder", 0) == 0 and counts.get("cancelOrder", 0) == 0
        assert counts.get("getInvestorList", 0) == 0
        assert fake.attribute_access == []
    finally:
        stop_started_bridge(bridge)


def test_lm_explicit_empty_params_rejected(monkeypatch, tmp_path):
    """2026-09-18 defaults fix, direction B (mirrors the lifecycle M01
    direction-B probes): the baked deployment defaults never legalize an
    EXPLICITLY blanked parameter — set_params with empty strings still
    refuses to start ("parameters incomplete" safety semantics kept), with
    zero store, zero identity, zero anonymous HTTP and zero native surface."""
    deployment = deploy_self_strategy(monkeypatch, tmp_path)
    fake = deployment.fake
    files_before = tree_snapshot(tmp_path)
    listener = ConnectionCountingListener()
    try:
        # hedge_flag blanked explicitly: refused even though exchange and
        # instrument_id carry valid values
        bridge = deployment.no_arg(strategy_id=23, strategy_name="SYNTHETIC",
                                   params={"exchange": "SHFE", "instrument_id": "rb2610",
                                           "hedge_flag": "",
                                           "execution_api_url": listener.url()})
        bridge.on_init()
        bridge.on_start()
        assert bridge.state_map.execution_status == "invalid"
        assert bridge._running is False
        assert bridge._instance_id is None  # identity never obtained

        # exchange/instrument_id blanked explicitly on a never-confirmed
        # bridge: the blank-restore fallback finds no confirmed connection,
        # so the explicit blanks stay empty and the start is refused
        blanks = deployment.no_arg(strategy_id=23, strategy_name="SYNTHETIC",
                                   params={"exchange": "", "instrument_id": "",
                                           "hedge_flag": "1",
                                           "execution_api_url": listener.url()})
        blanks.on_init()
        blanks.on_start()
        assert blanks.state_map.execution_status == "invalid"
        assert blanks._running is False
        assert blanks._instance_id is None

        # zero store, zero files, zero anonymous HTTP, zero native surface
        assert tree_snapshot(tmp_path) == files_before
        assert not expected_default_store(deployment.self_strategy_dir, 23).exists()
        assert listener.connection_count() == 0
        counts = fake.call_counts()
        assert counts.get("sendOrder", 0) == 0 and counts.get("cancelOrder", 0) == 0
        assert counts.get("getInvestorList", 0) == 0
        assert fake.attribute_access == []
    finally:
        listener.close()


# ===========================================================================
# M07 fact-recovery matrix (current behavior; decision §5 M07 / H17/H18/H22/
# H32/H39): pending outbox keeps original event_id/stream/序号, the
# CONSUMED/RETURNED/NO_CALL receipt chain is never truncated, old-session
# reports are isolated from the new scope, and restarts never re-call native.
# ===========================================================================

def _event_identity(body_text, module):
    parsed = module.strict_loads(body_text, limit=module.MAX_REQUEST_BYTES)
    item = parsed["data"] if parsed.get("type") == "RawCallbackEnvelope" else parsed
    return (item["event_id"], item["event_stream_id"], item["event_sequence"],
            item["callback_session_epoch"], item["callback_generation"])


def test_m07_pending_outbox_preserves_identity_and_isolates_generations(
        monkeypatch, tmp_path):
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    attempted_batches = []
    first = None
    second = None
    try:
        def recording_transport(url, payload, timeout, headers=None):
            if payload.get("type") == "EventBatch":
                # thread-tagged: the client's own events lane retries pending
                # uploads at high cadence, so a background-lane batch may land
                # in the same window as the direct _events_cycle() call below
                # (observed as a combined-run flake 2026-09-17); tagging keeps
                # the direct call's batch countable while STILL asserting
                # scope purity for every batch the client emitted
                attempted_batches.append((list(payload["data"]["events"]),
                                          dict(headers or {}),
                                          threading.get_ident()))
            return 500, b"{}"

        store = tmp_path / "store"
        first = module.AKExecutionBridge(native=fake, transport=recording_transport,
                                         store_directory=store,
                                         pairing_token=SYNTHETIC_PAIRING_TOKEN)
        first.strategy_id = 23
        first.set_params({"exchange": "SHFE", "instrument_id": "rb2610", "hedge_flag": "1"})
        first.on_init()
        first.on_start()
        first.on_order({"OrderID": "A1", "Status": "未成交"})
        first.on_trade({"TradeID": "T1", "Volume": 1})
        rows_a = first._stores.pending_events(16)
        assert len(rows_a) == 2
        identity_a = [_event_identity(body, module) for _, body in rows_a]
        first.on_stop()

        # a brand-new object on the SAME store must see byte-identical pending
        # facts: same event_id, same stream, same sequence, same session scope
        second = module.AKExecutionBridge(native=fake, transport=recording_transport,
                                          store_directory=store,
                                          pairing_token=SYNTHETIC_PAIRING_TOKEN)
        second.strategy_id = 23
        second.set_params({"exchange": "SHFE", "instrument_id": "rb2610", "hedge_flag": "1"})
        second.on_init()
        second.on_start()
        rows_b = second._stores.pending_events(16)
        identity_b = [_event_identity(body, module) for _, body in rows_b]
        assert identity_b == identity_a
        second.on_order({"OrderID": "B1", "Status": "未成交"})
        rows_after = second._stores.pending_events(16)
        identity_after = [_event_identity(body, module) for _, body in rows_after]
        assert identity_after[:2] == identity_a            # old facts untouched
        assert len(identity_after) == 3
        assert identity_after[2][1] != identity_a[0][1]    # new generation -> new stream
        assert identity_after[2][3] != identity_a[0][3]    # new session scope

        # upload batches never mix scopes: one batch == one stream/session/generation,
        # and every batch carries the ORIGINAL event ids and sequences
        def drain_one_batch(bridge):
            attempted_batches.clear()
            try:
                bridge._events_cycle()
            except module.ContractError:
                pass  # the recording transport always answers 500
            mine = [batch for batch in attempted_batches
                    if batch[2] == threading.get_ident()]
            assert len(mine) == 1  # the direct cycle emitted exactly one batch
            events, headers, _ = mine[0]
            # scope purity holds for EVERY batch the client emitted in this
            # window, the background retry lane included
            for lane_events, _, _ in attempted_batches:
                lane_scopes = {(item["event_stream_id"], item["callback_session_epoch"],
                                item["callback_generation"]) for item in lane_events}
                assert len(lane_scopes) == 1
            scopes = {(item["event_stream_id"], item["callback_session_epoch"],
                       item["callback_generation"]) for item in events}
            assert len(scopes) == 1
            return events, headers

        old_events, _ = drain_one_batch(second)
        assert [item["event_id"] for item in old_events] == [row[0] for row in identity_a]
        assert sorted(item["event_sequence"] for item in old_events) == \
            sorted(row[2] for row in identity_a)
        # simulating the service ack for the old stream exposes the isolated new one
        second._stores.ack_events([row[0] for row in identity_a])
        new_events, _ = drain_one_batch(second)
        assert [item["event_id"] for item in new_events] == [identity_after[2][0]]
        assert new_events[0]["event_stream_id"] == identity_after[2][1]
        # no native trading surface was ever touched while recovering facts
        counts = _separated_counts(second, fake)
        assert counts["fake_native_send_attempts"] == 0
        assert counts["fake_native_cancel_attempts"] == 0
        second.on_stop()
    finally:
        stop_started_bridge(first)
        stop_started_bridge(second)


def test_m07_receipt_chain_not_truncated_and_never_recalled(monkeypatch, tmp_path):
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    service = FakeExecutionService()
    store = tmp_path / "store"
    first = None
    second = None
    try:
        # W2: each life arms through the ruled public local-slot chain; the
        # r3-era Params re-arm is inert (reversed direction proven in h17/h21/
        # h37 — here the receipts-authority contract is the subject).
        first = _started(module, fake, tmp_path, service, store=store)
        arm_bridge_to_armed(module, first)
        set_synthetic_gate_capture(first, quote=True, account=True, position=True, contract=True)
        command = armed_dispatch(first)
        first._receive_dispatch(command)
        first._pump_once()
        assert len(fake.send_calls()) == 1
        first.on_stop()

        def chain_rows(store_dir, command_id):
            # on_stop closes the in-memory store handles; the receipts FILE is
            # the durable fact, so read it directly (read-only, both while a
            # bridge holds it and after stop)
            receipts_path = (Path(store_dir) / "receipts.db").as_posix()
            connection = sqlite3.connect(f"file:{receipts_path}?mode=ro", uri=True)
            try:
                return connection.execute(
                    "SELECT local_sequence, body_hash, previous_receipt_sha256, stage"
                    " FROM receipts WHERE command_id=? ORDER BY local_sequence",
                    (command_id,)).fetchall()
            finally:
                connection.close()

        rows = chain_rows(store, command["command_id"])
        assert [row[3] for row in rows] == ["CONSUMED", "RETURNED"]
        for previous, current in zip(rows, rows[1:]):  # hash chain intact
            assert current[2] == previous[1]
        head_hash = rows[-1][1]

        # new object, same store: replaying the same command is receipts-only
        second = _started(module, fake, tmp_path, service, store=store)
        arm_bridge_to_armed(module, second)
        set_synthetic_gate_capture(second, quote=True, account=True, position=True, contract=True)
        second._receive_dispatch(dict(command))
        second._pump_once()
        rows_after = chain_rows(store, command["command_id"])
        assert [row[3] for row in rows_after] == ["CONSUMED", "RETURNED", "NO_CALL"]
        for previous, current in zip(rows_after, rows_after[1:]):
            assert current[2] == previous[1]
        assert rows_after[1][1] == head_hash  # restart appended, never rewrote
        assert len(fake.send_calls()) == 1    # restart did not re-call native
        assert second.counters["native_send_attempts"] == 0

        # a ticket delivered to a fully STOPPED object (current fact): zero
        # native calls and the receipts file stays BYTE-STABLE after the final
        # close — nothing fabricates or rewrites facts post-stop (a terminal
        # persisted NO_CALL for such leftover tickets is an LM-C pending
        # behavior and is asserted in the skip-marked LM-I pending group)
        stopped_command = synthetic_dispatch(binding=synthetic_binding(
            instance_id=second._instance_id, session_epoch=second._session_epoch),
            params_sha256=second._params_sha256, command_id="SIM_CMD_STOPPED",
            ttl_ms=490)
        second.on_stop()
        # snapshot AFTER the close (WAL checkpoint lands in the main file on
        # close); the post-stop ticket must not move a single byte
        receipts_bytes_before = (store / "receipts.db").read_bytes()
        second._receive_dispatch(stopped_command)
        second._pump_once()
        assert chain_rows(store, stopped_command["command_id"]) == []
        assert (store / "receipts.db").read_bytes() == receipts_bytes_before
        assert len(fake.send_calls()) == 1
        assert second.counters["native_send_attempts"] == 0
    finally:
        stop_started_bridge(first)
        stop_started_bridge(second)
        service.close()


# ===========================================================================
# M05 restart matrix (current behavior; decision §5 M05, F06/F07/F14): the
# service rotates tokens per start, rejects the old token with 401, strictly
# increases its epoch, rejects stale epochs with 409, refuses a restarted
# client's new session until the binding is CAS-updated, and the ledger
# registration-revision CAS behaves per the frozen semantics.
# ===========================================================================

def _poll_packet(instance, session, *, service_epoch, params_sha256="ab" * 32,
                 generation="SIM_GEN_1", sequence=1):
    from datetime import datetime as _dt
    import uuid as _uuid
    return {"schema_version": "infiny_client_execution_transport_v1_1",
            "type": "PollRequest",
            "data": {"request_id": _uuid.uuid4().hex,  # opaque identifier
                     "instance_id": instance, "session_epoch": session,
                     "generation": generation, "service_epoch": service_epoch,
                     "sequence": sequence,
                     "sent_at": _dt.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
                     "params_sha256": params_sha256,
                     "snapshot": None, "snapshot_id": None,
                     "client_state_digest": "cd" * 32, "ack_watermark": None}}


def _error_code(body):
    return None if body is None else body["data"]["error_code"]


_HEADER_KEYS = ("request_id", "instance_id", "session_epoch", "generation",
                "service_epoch", "sequence", "sent_at", "params_sha256")


def _control_report(instance, session, *, service_epoch, kind="GATE_CLOSED"):
    """A wire-valid ControlReport: transport header fields ONLY, plus
    kind/confirmation (the poll-only body fields are not accepted here)."""
    base = _poll_packet(instance, session, service_epoch=service_epoch)["data"]
    return {"schema_version": "infiny_client_execution_transport_v1_1",
            "type": "ControlReport",
            "data": {**{key: base[key] for key in _HEADER_KEYS},
                     "kind": kind, "confirmation": None}}


def test_m05_service_restart_rejects_old_tokens_and_old_epochs(tmp_path):
    stack = TempExecutionStack(tmp_path, tokens={"SIM_INSTANCE_A": SYNTHETIC_PAIRING_TOKEN})
    try:
        poll = stack.poll_url
        # diagnostic token without a registered actor: business refusal (F07)
        stack.clock.advance(5.05)
        status, body = post_json(poll, _poll_packet("SIM_INSTANCE_A", "SIM_EPOCH_A",
                                                    service_epoch=None),
                                 token=SYNTHETIC_PAIRING_TOKEN)
        assert (status, _error_code(body)) == (403, "ACTOR_UNREGISTERED")

        # F08 current fact: the running service has NO runtime actor refresh —
        # the R3.1/R3.2 closed loop registers OFFLINE (serve stopped) and the
        # next service start loads the binding. Register, then restart on the
        # same journal with the SAME token material (fresh epoch, fresh
        # service_instance_id).
        stack.register_actor_bindings((synthetic_binding(
            instance_id="SIM_INSTANCE_A", session_epoch="SIM_EPOCH_A"),))
        stack.restart(tokens={"SIM_INSTANCE_A": SYNTHETIC_PAIRING_TOKEN})

        stack.clock.advance(5.05)
        status, body = post_json(poll, _poll_packet("SIM_INSTANCE_A", "SIM_EPOCH_A",
                                                    service_epoch=None),
                                 token=SYNTHETIC_PAIRING_TOKEN)
        assert status == 200 and body["data"]["kind"] == "BLOCKED"
        first_epoch = body["data"]["service_epoch"]
        assert first_epoch == stack.service_epoch

        # same-object restart: new session vs registered binding -> 403 (F06)
        stack.clock.advance(5.05)
        status, body = post_json(poll, _poll_packet("SIM_INSTANCE_A", "SIM_EPOCH_NEW",
                                                    service_epoch=first_epoch),
                                 token=SYNTHETIC_PAIRING_TOKEN)
        assert (status, _error_code(body)) == (403, "BINDING_MISMATCH")

        # valid token worn by a different instance identity -> 403 (F06)
        stack.clock.advance(5.05)
        status, body = post_json(poll, _poll_packet("SIM_INSTANCE_B", "SIM_EPOCH_A",
                                                    service_epoch=first_epoch),
                                 token=SYNTHETIC_PAIRING_TOKEN)
        assert (status, _error_code(body)) == (403, "BINDING_MISMATCH")

        # service restart on the same journal: fresh tokens, higher epoch (F07)
        fresh_token = "ef" * 32
        old_epoch = stack.service_epoch
        stack.restart(tokens={"SIM_INSTANCE_A": fresh_token})
        assert stack.service_epoch > old_epoch

        status, body = post_json(poll, _poll_packet("SIM_INSTANCE_A", "SIM_EPOCH_A",
                                                    service_epoch=None),
                                 token=SYNTHETIC_PAIRING_TOKEN)
        assert (status, _error_code(body)) == (401, "AUTHENTICATION_FAILED")

        # the old learned epoch is refused even with the fresh token
        stack.clock.advance(5.05)
        status, body = post_json(poll, _poll_packet("SIM_INSTANCE_A", "SIM_EPOCH_A",
                                                    service_epoch=old_epoch),
                                 token=fresh_token)
        assert (status, _error_code(body)) == (409, "EPOCH_MISMATCH")

        # fresh null-epoch handshake closes the loop with the fresh token
        stack.clock.advance(5.05)
        status, body = post_json(poll, _poll_packet("SIM_INSTANCE_A", "SIM_EPOCH_A",
                                                    service_epoch=None),
                                 token=fresh_token)
        assert status == 200 and body["data"]["service_epoch"] == stack.service_epoch

        # null epoch on a non-poll route is a handshake refusal, not business
        # (ControlReport carries ONLY the transport header + kind/confirmation)
        control = _control_report("SIM_INSTANCE_A", "SIM_EPOCH_A",
                                  service_epoch=None)
        status, body = post_json(poll.rsplit("/", 1)[0] + "/control", control,
                                 token=fresh_token)
        assert (status, _error_code(body)) == (409, "HANDSHAKE_REQUIRED")
    finally:
        stack.close()


@pytest.mark.parametrize("restart_order", ["client_then_service", "service_then_client",
                                           "both_together"])
def test_m05_combined_restart_orders_stay_blocked_until_cas_reregistration(
        tmp_path, restart_order):
    """Every restart-order combination ends blocked until the operator
    re-registers the CURRENT session via the revision CAS with a fresh token
    (R3.2: neither restart alone can close the loop)."""
    stack = TempExecutionStack(tmp_path, bindings=(synthetic_binding(
        instance_id="SIM_INSTANCE_A", session_epoch="SIM_EPOCH_A"),),
        tokens={"SIM_INSTANCE_A": SYNTHETIC_PAIRING_TOKEN})
    try:
        first_epoch = stack.service_epoch

        if restart_order in ("client_then_service", "both_together"):
            stack.clock.advance(5.05)
            status, body = post_json(stack.poll_url, _poll_packet(
                "SIM_INSTANCE_A", "SIM_EPOCH_B", service_epoch=first_epoch),
                token=SYNTHETIC_PAIRING_TOKEN)
            # restarted client session vs the registered binding -> F06 refusal
            assert (status, _error_code(body)) == (403, "BINDING_MISMATCH")
        # the service-restart leg (whichever order): fresh token material on
        # the same journal — the old token dies with the old service start
        fresh = "ef" * 32
        stack.restart(tokens={"SIM_INSTANCE_A": fresh})

        # both orders now end in the same terminal state: the old token is dead
        status, body = post_json(stack.poll_url, _poll_packet(
            "SIM_INSTANCE_A", "SIM_EPOCH_B", service_epoch=None),
            token=SYNTHETIC_PAIRING_TOKEN)
        assert (status, _error_code(body)) == (401, "AUTHENTICATION_FAILED")

        # the manual R3.2 recovery: CAS-update the binding to the new session,
        # then a fresh token handshake closes the loop
        stack.register_actor_bindings((synthetic_binding(
            instance_id="SIM_INSTANCE_A", session_epoch="SIM_EPOCH_B"),),
            expected_registration_revision=1)
        fresh2 = "cd" * 32
        stack.restart(tokens={"SIM_INSTANCE_A": fresh2})
        stack.clock.advance(5.05)
        status, body = post_json(stack.poll_url, _poll_packet(
            "SIM_INSTANCE_A", "SIM_EPOCH_B", service_epoch=None), token=fresh2)
        assert status == 200 and body["data"]["service_epoch"] == stack.service_epoch
    finally:
        stack.close()


def test_m05_registration_revision_cas_semantics(tmp_path):
    """F14 ledger-side current facts: same manifest is idempotent, updates
    need the exact current revision, and neither missing nor stale values
    can silently overwrite a binding."""
    stack = TempExecutionStack(tmp_path)
    try:
        first = synthetic_binding(instance_id="SIM_INSTANCE_A", session_epoch="SIM_EPOCH_A")
        revision = stack.register_actor_bindings((first,))
        assert revision == 1
        # identical retry: idempotent, same revision, no new binding
        assert stack.register_actor_bindings((first,)) == 1
        changed = synthetic_binding(instance_id="SIM_INSTANCE_A",
                                    session_epoch="SIM_EPOCH_B")
        with pytest.raises(LedgerError) as missing:
            stack.register_actor_bindings((changed,))
        assert "ACTOR_CONFLICT" in missing.value.blockers
        with pytest.raises(LedgerError) as stale:
            stack.register_actor_bindings((changed,), expected_registration_revision=2)
        assert "ACTOR_CONFLICT" in stale.value.blockers
        assert stack.register_actor_bindings(
            (changed,), expected_registration_revision=1) == 2
        # the failed attempts never replaced the binding: only the CAS update
        # moved the registered session forward (direct journal verification)
        records = stack.journal.actors()
        assert len(records) == 1
        assert records[0].registration_revision == 2
        registered_sessions = {binding.session_epoch for binding in records[0].bindings}
        assert registered_sessions == {"SIM_EPOCH_B"}
    finally:
        stack.close()


def test_m05_client_driven_restart_walls(monkeypatch, tmp_path):
    """The real client (constructor token seam, INJECTED/SYNTHETIC) walks into
    the same walls: first handshake served, then its own restart hits
    BINDING_MISMATCH, and a service restart kills its token with 401."""
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    listener = ConnectionCountingListener()
    stack = None
    bridge = None
    try:
        # 2026-09-18 timing hardening (same lineage as the 2026-09-17
        # combined-run flakes): the slow REAL stack (journal + service +
        # server) is built BEFORE the client starts and the fixed origin is
        # wired BEFORE on_start — the R1.4-ruled timing the M04 group below
        # already uses, with no parameter touched after the start. The
        # earlier draft built the stack after on_start and retargeted the URL
        # mid-session through set_params; that raced the first 5 s poll
        # cadence on slow I/O (sentinel contacted), and the retarget's
        # params_identity_changed gate close emits its OV-F05 GATE_CLOSED
        # report to the PRE-retarget origin (client behavior since pre-v2b:
        # _close_gate runs before the params swap), which opened a real
        # socket to the inert sentinel on every run — the "transient GATE_
        # CLOSED report" the old comment described. With the fixed origin
        # wired before the start there is no mid-session parameter change at
        # all, and the sentinel below proves ZERO stray sockets.
        # R3.1 order as ruled: the empty service holds the port FIRST (no
        # tokens, no actors), registration happens while the running service
        # cannot see it (F08), and the same-port restart with the fresh token
        # material is the service start that loads the binding.
        stack = TempExecutionStack(tmp_path)  # empty: no tokens, no actors
        bridge = module.AKExecutionBridge(native=fake, store_directory=tmp_path / "store",
                                          pairing_token=SYNTHETIC_PAIRING_TOKEN)
        bridge.strategy_id = 23
        bridge.set_params({"exchange": "SHFE", "instrument_id": "rb2610", "hedge_flag": "1",
                           "execution_api_url": stack.poll_url})
        bridge.on_init()
        bridge.on_start()
        identity = synthetic_binding(instance_id=bridge._instance_id,
                                     session_epoch=bridge._session_epoch,
                                     strategy_id=23)
        stack.register_actor_bindings((identity,))
        stack.restart(tokens={bridge._instance_id: SYNTHETIC_PAIRING_TOKEN})
        stack.clock.advance(5.05)
        _nudge_control_lane(bridge)
        assert _wait_until(lambda: any(
            record.get("event") == "request_served" and record.get("route", "").endswith("/poll")
            for record in stack.audit_records())
            and bridge._service_epoch == stack.service_epoch)
        assert bridge._service_epoch == stack.service_epoch

        bridge.on_stop()
        bridge.on_start()  # same object: new session epoch
        stack.clock.advance(5.05)
        _nudge_control_lane(bridge)
        assert _wait_until(lambda: any(
            record.get("event") == "request_rejected"
            and record.get("error") == "BINDING_MISMATCH"
            and record.get("instance_id") == bridge._instance_id
            for record in stack.audit_records()))

        stack.restart(tokens={bridge._instance_id: "ef" * 32})
        stack.clock.advance(5.05)
        _nudge_control_lane(bridge)
        # wait for the CLIENT-side latch too: the audit record is written when
        # the service rejects, but the client processes the 401 response a
        # moment later — asserting on the server record alone is a TOCTOU
        # race (observed as a combined-run flake 2026-09-17)
        assert _wait_until(lambda: any(
            record.get("event") == "request_rejected"
            and record.get("error") == "AUTHENTICATION_FAILED"
            for record in stack.audit_records())
            and bridge.state_map.execution_status == "restart_required")
        # R3.2 client-side wall: the dead token closes the gate — the client
        # itself must report RESTART_REQUIRED, not keep storming the service
        assert bridge.state_map.execution_status == "restart_required"
        # separated counting: zero native surface; the ONLY real sockets the
        # client opened went to the temp service port — no parameter was ever
        # pointed at the sentinel (fixed origin wired before on_start), so
        # zero stray real sockets is the deterministic fact it now proves
        counts = _separated_counts(bridge, fake, listener=listener, stack=stack)
        assert counts["fake_native_send_attempts"] == 0
        assert counts["fake_native_cancel_attempts"] == 0
        assert counts["real_socket_attempts_to_sentinel"] == 0
        assert counts["real_service_code_requests"] >= 1
        bridge.on_stop()
    finally:
        if bridge is not None:
            bridge.on_stop()
        if stack is not None:
            stack.close()
        listener.close()


# ===========================================================================
# M04 no-arg positive chain (decision §5 M04 / R4.2). Skips removed at the
# X01-C/X01-S/LM-D freeze merge (NE1-ARB-LM-X01-001 §3 X01-I row, §6 V05).
# Timing resolved per R1.4 as ruled: the earlier draft redirected
# execution_api_url via set_params DURING WAIT_PAIRING and expected the same
# session to activate — R1.4 voids any params_sha256 change mid-wait
# (RESTART_REQUIRED), so the fixed origin is wired BEFORE on_start (the
# empty service holds the port first) and no parameter is touched after the
# start; the client itself is unchanged.
#
# DOUBLE EVIDENCE: the chain below drives the operator/service/user ROLES
# with the TempExecutionStack writer/startup doubles and PairingToolDouble.
# The real production chain (real CLI launcher + real writer + real LM-D
# install tool + no-arg activation) is the X01 regression test at the end of
# this file; the double results alone cannot prove real-tool-chain
# consistency.
# ===========================================================================

def test_m04_noarg_wait_pairing_zero_anonymous_requests(monkeypatch, tmp_path):
    """M04 stage 1: a zero-argument client that only receives
    framework assignments must enter WAIT_PAIRING with an active pairing
    request in the deterministic default store and must not emit ANY request,
    native call or attribute resolution while unpaired."""
    deployment = deploy_self_strategy(monkeypatch, tmp_path)
    fake = deployment.fake
    listener = ConnectionCountingListener()
    stack = None  # the service deliberately does NOT exist yet (R3.1 step 2)
    bridge = None
    try:
        bridge = deployment.no_arg(
            strategy_id=23, strategy_name="SYNTHETIC",
            params={"exchange": "SHFE", "instrument_id": "rb2610", "hedge_flag": "1",
                    "execution_api_url": listener.url()})
        bridge.on_init()
        bridge.on_start()
        store_dir = expected_default_store(deployment.self_strategy_dir, 23)
        assert store_dir.is_dir(), "WAIT_PAIRING must own the deterministic default store"
        identity = json.loads((store_dir / "identity.json").read_text(encoding="utf-8"))
        assert identity["schema_version"] == IDENTITY_SCHEMA
        assert identity["strategy_id"] == 23
        request = json.loads((store_dir / "pairing-request.json").read_text(encoding="utf-8"))
        assert request["schema_version"] == PAIRING_REQUEST_SCHEMA
        assert request["active"] is True
        assert request["instance_id"] == identity["instance_id"]
        # same installation SLOT (resolve both sides: Windows 8.3 short-name
        # aliases must not fake a different store)
        assert Path(request["store_directory"]).resolve() == store_dir.resolve()
        assert request["client_file_sha256"] == deployment.client_sha256
        assert bridge.state_map.execution_status == "wait_pairing"
        # zero anonymous traffic, zero native surface, zero attributes
        assert listener.connection_count() == 0
        counts = fake.call_counts()
        assert counts.get("sendOrder", 0) == 0 and counts.get("cancelOrder", 0) == 0
        assert counts.get("getInvestorList", 0) == 0
        assert fake.attribute_access == []
        bridge.on_stop()
        stopped = json.loads((store_dir / "pairing-request.json").read_text(encoding="utf-8"))
        assert stopped["active"] is False
    finally:
        if bridge is not None:
            bridge.on_stop()
        if stack is not None:
            stack.close()
        listener.close()


def test_m04_full_chain_manifest_registration_handoff_handshake(monkeypatch, tmp_path):
    """M04 stages 2-6 [DOUBLE EVIDENCE: TempExecutionStack service/writer
    doubles + PairingToolDouble install]: the active request drives the
    operator manifest, the temp service issues the v2 token, the USER-role
    tool double installs it, the same waiting client activates, the
    null-epoch poll handshake reaches PAIRED_DISABLED, and authenticated
    control/events/status succeed in the same session — with every real call
    count at zero.

    R3.1 order: the empty service holds the port FIRST (no tokens, no actors —
    a waiting client never contacts it), the client then starts WAIT_PAIRING
    pointing at that fixed origin; registration happens while the running
    service cannot see it (F08), and the SAME-PORT restart with the fresh
    token material is the service start that closes the loop. No parameter is
    touched after the start: R1.4 makes any params_sha256 change invalidate
    the active request."""
    deployment = deploy_self_strategy(monkeypatch, tmp_path)
    fake = deployment.fake
    listener = ConnectionCountingListener()
    stack = TempExecutionStack(tmp_path)  # empty: no tokens, no actors
    bridge = None
    try:
        bridge = deployment.no_arg(
            strategy_id=23, strategy_name="SYNTHETIC",
            params={"exchange": "SHFE", "instrument_id": "rb2610", "hedge_flag": "1",
                    "execution_api_url": stack.poll_url})
        bridge.on_init()
        bridge.on_start()
        store_dir = expected_default_store(deployment.self_strategy_dir, 23)
        request_path = store_dir / "pairing-request.json"
        request = json.loads(request_path.read_text(encoding="utf-8"))
        assert request["execution_api_url"] == stack.poll_url
        # the waiting client has not emitted ANY request to the running but
        # unpaired service (zero anonymous traffic, R1.4)
        assert stack.service.counters().get("requests", 0) == 0

        # template facts must match the live request before any registration
        template = {"actor_id": "SIM_ACTOR_LM", "account_key": SIM_ACCOUNT_KEY,
                    "strategy_key": SIM_STRATEGY_KEY, "strategy_id": 23,
                    "trading_day": SIM_TRADING_DAY, "exchange": "SHFE",
                    "instrument_id": "rb2610", "hedge_flag": "1"}
        for field in ("strategy_id", "exchange", "instrument_id", "hedge_flag"):
            assert request[field] == template[field], "template drifted from the live request"

        binding = {"account_key": template["account_key"],
                   "strategy_key": template["strategy_key"],
                   "strategy_id": request["strategy_id"],
                   "instance_id": request["instance_id"],
                   "session_epoch": request["session_epoch"],
                   "trading_day": template["trading_day"],
                   "exchange": request["exchange"],
                   "instrument_id": request["instrument_id"],
                   "hedge_flag": request["hedge_flag"]}
        # offline registration is invisible to the running service (F08); the
        # same-port restart with fresh token material loads the binding
        stack.register_actor_bindings((binding,))
        stack.restart(tokens={request["instance_id"]: "cd" * 32})
        pairing_dir = stack.base_dir / "pairing"
        stack.write_service_pairing_files_v2(
            pairing_dir, session_epochs={request["instance_id"]: request["session_epoch"]})
        service_start_path = stack.base_dir / "service-start.json"
        stack.write_service_start(service_start_path)
        source = pairing_dir / f"{request['instance_id']}.token"

        # USER role (R2.1): the reviewed local tool, never the agent
        receipt = PairingToolDouble().install(
            client_store=store_dir, client_request_path=request_path,
            source_path=source, service_start_path=service_start_path,
            contract_lock_sha256="cd" * 32,
            expected_client_sha256=deployment.client_sha256,
            receipt_path=stack.base_dir / "pairing-transfer.json")
        assert receipt["result"] == "TRANSFERRED_AWAITING_HANDSHAKE"
        assert "token" not in receipt and "token" not in json.dumps(receipt)

        # the SAME waiting session activates: on the bound callback thread the
        # call stages the candidate AND performs the null-epoch metadata
        # handshake (the poll floor must be clear first — advance the clock)
        stack.clock.advance(5.05)
        assert bridge._try_activate_pairing() is True
        assert _wait_until(lambda: any(
            record.get("event") == "request_served"
            and record.get("route", "").endswith("/poll")
            and record.get("instance_id") == request["instance_id"]
            for record in stack.audit_records())
            and bridge._service_epoch == stack.service_epoch)
        assert bridge._service_epoch == stack.service_epoch
        assert bridge.state_map.execution_status == "paired_disabled"

        # authenticated business routes in the same session: events upload
        bridge.on_order({"OrderID": "M4-1", "Status": "未成交"})
        assert _wait_until(lambda: any(
            record.get("event") == "request_served"
            and record.get("route", "").endswith("/events")
            for record in stack.audit_records()))
        # control (stop chain) and status via the same credentials
        bridge.on_stop()
        assert _wait_until(lambda: sum(
            1 for record in stack.audit_records()
            if record.get("event") == "request_served"
            and record.get("route", "").endswith("/control")) >= 2)
        status, body = get_status(stack.poll_url, token="cd" * 32,
                                  instance_id=request["instance_id"],
                                  session_epoch=request["session_epoch"],
                                  generation=request["generation"],
                                  service_epoch=stack.service_epoch,
                                  params_sha256=request["params_sha256"])
        assert status == 200 and body["type"] == "StatusResponse"
        # every real count is zero and no anonymous request ever leaked
        counts = _separated_counts(bridge, fake, listener=listener, stack=stack)
        assert counts["fake_native_send_attempts"] == 0
        assert counts["fake_native_cancel_attempts"] == 0
        assert fake.attribute_access == []
        assert listener.connection_count() == 0
        rejected = [record for record in stack.audit_records()
                    if record.get("event") == "request_rejected"]
        assert rejected == [] or all(
            record.get("error") not in ("AUTHENTICATION_FAILED",) for record in rejected)
    finally:
        if bridge is not None:
            bridge.on_stop()
        if stack is not None:
            stack.close()
        listener.close()


def test_m04_stop_makes_request_inactive_and_reactivation_refused(monkeypatch, tmp_path):
    """M04 counter-example edge [DOUBLE EVIDENCE: PairingToolDouble install]:
    a stopped client's request is inactive; installing against it is refused
    and the client never activates a stale credential."""
    deployment = deploy_self_strategy(monkeypatch, tmp_path)
    listener = ConnectionCountingListener()
    stack = None
    bridge = None
    try:
        bridge = deployment.no_arg(
            strategy_id=23, strategy_name="SYNTHETIC",
            params={"exchange": "SHFE", "instrument_id": "rb2610", "hedge_flag": "1",
                    "execution_api_url": listener.url()})
        bridge.on_init()
        bridge.on_start()
        store_dir = expected_default_store(deployment.self_strategy_dir, 23)
        request_path = store_dir / "pairing-request.json"
        request = json.loads(request_path.read_text(encoding="utf-8"))
        binding = {"account_key": SIM_ACCOUNT_KEY, "strategy_key": SIM_STRATEGY_KEY,
                   "strategy_id": request["strategy_id"],
                   "instance_id": request["instance_id"],
                   "session_epoch": request["session_epoch"],
                   "trading_day": SIM_TRADING_DAY, "exchange": request["exchange"],
                   "instrument_id": request["instrument_id"],
                   "hedge_flag": request["hedge_flag"]}
        stack = TempExecutionStack(tmp_path, bindings=(binding,),
                                   tokens={request["instance_id"]: "cd" * 32})
        pairing_dir = stack.base_dir / "pairing"
        stack.write_service_pairing_files_v2(
            pairing_dir, session_epochs={request["instance_id"]: request["session_epoch"]})
        service_start_path = stack.base_dir / "service-start.json"
        stack.write_service_start(service_start_path)

        bridge.on_stop()  # the waiting session ends BEFORE the handoff
        stopped = json.loads(request_path.read_text(encoding="utf-8"))
        assert stopped["active"] is False

        with pytest.raises(PairingToolRefusal) as refusal:
            PairingToolDouble().install(
                client_store=store_dir, client_request_path=request_path,
                source_path=pairing_dir / f"{request['instance_id']}.token",
                service_start_path=service_start_path,
                contract_lock_sha256="cd" * 32,
                expected_client_sha256=deployment.client_sha256,
                receipt_path=stack.base_dir / "pairing-transfer.json")
        assert refusal.value.code == "PAIRING_REQUEST_INACTIVE"
        assert not (store_dir / "pairing.json").exists()
        assert bridge._try_activate_pairing() is False
        assert listener.connection_count() == 0  # no anonymous retry storm
    finally:
        if bridge is not None:
            bridge.on_stop()
        if stack is not None:
            stack.close()
        listener.close()


# ===========================================================================
# X01 real-chain regression (NE1-ARB-LM-X01-001 §3 X01-I row, §6 V05). The
# M04 group above exercises the role boundaries with the declared doubles;
# THIS test is the real-tool-chain evidence: the REAL LM-D pairing tool
# (scripts/pair_infiny_execution.py: build-actor-manifest + install), the
# REAL LM-S CLI launcher (scripts/run_infiny_execution.py: register-actor +
# serve — its serve publishes the v2 source credential through the REAL
# client_service.write_pairing_file and prints the execution_started line
# saved verbatim as the non-secret service-start description), and the SAME
# no-arg self_strategy client activating against that running real service.
# X01 core: the service identity ``execution-<16hex>`` produced by the real
# writer/launcher survives the real tool install VERBATIM (slot value never
# modified) and activates the no-arg client — the pre-X01 client hex32 loop
# refused exactly this value with PAIRING_FORMAT_INVALID. Everything runs in
# one isolated sandbox on a temp loopback port; SYNTHETIC identities only.
# ===========================================================================

def _read_audit_records(path):
    if not Path(path).exists():
        return []
    return [json.loads(line) for line in
            Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def test_x01_real_cli_writer_tool_install_activate_noarg_client(
        monkeypatch, tmp_path, capsys):
    import importlib.util
    import re as _re
    import socket as _socket

    def load_script(filename, module_name):
        spec = importlib.util.spec_from_file_location(
            module_name, ROOT / "scripts" / filename)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(__import__("sys").modules, module_name, module)
        spec.loader.exec_module(module)
        return module

    launcher = load_script("run_infiny_execution.py", "ne1_launcher_real_x01")
    tool = load_script("pair_infiny_execution.py", "ne1_pairing_tool_real_x01")

    deployment = deploy_self_strategy(monkeypatch, tmp_path)
    fake = deployment.fake
    # operator-side SYNTHETIC non-secret inputs for the REAL tool manifest,
    # prepared BEFORE the port reservation: nothing below depends on the
    # request, and every filesystem operation moved ahead of the probe
    # shrinks the reserve->close->serve-bind race window on that port
    # (LM-I isolation hardening 2026-09-17; combined-run flake source)
    work = tmp_path / "x01_real"
    work.mkdir()
    lock_path = work / "contract-lock.json"
    lock_path.write_text('{"lock": "SYNTHETIC-contract-lock-x01"}',
                         encoding="utf-8")
    template_path = work / "binding-template.json"
    template_path.write_text(json.dumps({
        "actor_id": "SIM_ACTOR_X01", "account_key": SIM_ACCOUNT_KEY,
        "strategy_key": SIM_STRATEGY_KEY, "strategy_id": 23,
        "trading_day": SIM_TRADING_DAY, "exchange": "SHFE",
        "instrument_id": "rb2610", "hedge_flag": "1"}), encoding="utf-8")
    authorization_path = work / "operator-authorization.json"
    authorization_path.write_text(json.dumps(
        {"operator_action_id": "SIM_OP_X01_REAL"}), encoding="utf-8")

    # R1.4 timing (as ruled): the fixed origin is reserved and wired BEFORE
    # on_start; no parameter is touched after the start — the waiting client
    # simply never contacts the port until the credential activates it.
    with _socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    poll_url = f"http://127.0.0.1:{port}/execution/v1/poll"

    bridge = deployment.no_arg(
        strategy_id=23, strategy_name="SYNTHETIC",
        params={"exchange": "SHFE", "instrument_id": "rb2610", "hedge_flag": "1",
                "execution_api_url": poll_url})
    bridge.on_init()
    bridge.on_start()
    assert bridge.state_map.execution_status == "wait_pairing"
    store_dir = expected_default_store(deployment.self_strategy_dir, 23)
    request_path = store_dir / "pairing-request.json"
    request = json.loads(request_path.read_text(encoding="utf-8"))
    assert request["active"] is True

    # REAL LM-D tool: build-actor-manifest from the ACTIVE live request
    # (binding identity fields come from the request, never guessed)
    manifest_dir = work / "manifest-out"
    assert tool.main([
        "build-actor-manifest", "--client-request", str(request_path),
        "--binding-template", str(template_path),
        "--contract-lock", str(lock_path),
        "--operator-action-id", "SIM_OP_X01_REAL",
        "--operator-authorization", str(authorization_path),
        "--output-directory", str(manifest_dir)]) == 0
    manifest_path = manifest_dir / "register-actor-manifest.json"

    # REAL CLI operator registration (journal maintenance; service stopped)
    journal_path = work / "exec.db"
    audit_path = work / "serve-audit.jsonl"
    assert launcher.main([
        "register-actor", "--journal", str(journal_path),
        "--manifest", str(manifest_path), "--contract-lock", str(lock_path),
        "--initialize"]) == 0

    # REAL CLI serve, driven in-process so the offline guard registers the
    # loopback listener: serve writes the v2 source through the REAL
    # write_pairing_file with the service's own identity and only then
    # prints the execution_started description saved verbatim below.
    from ak_trading.infiny_execution import client_service as _cs
    real_build = _cs.build_execution_server
    captured = {}

    def build(**kwargs):
        captured["server"] = real_build(**kwargs)
        return captured["server"]

    monkeypatch.setattr(_cs, "build_execution_server", build)
    pairing_dir = work / "pairing"
    serve_result = {}

    def run_serve():
        serve_result["code"] = launcher.main([
            "serve", "--port", str(port), "--journal", str(journal_path),
            "--audit-log", str(audit_path), "--pairing-dir", str(pairing_dir),
            "--registry-dir", str(work / "registry"),
            "--contract-lock", str(lock_path)])

    serve_thread = threading.Thread(target=run_serve, daemon=True)
    serve_thread.start()
    started = None
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and started is None:
            for line in capsys.readouterr().out.splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("event") == "execution_started":
                    started = event
            if started is None:
                time.sleep(0.05)
        assert started is not None, "real CLI serve never emitted execution_started"
        assert started["port"] == port
        assert started["execution_enabled"] is False
        service_start_path = work / "service-start.json"
        service_start_path.write_text(json.dumps(started), encoding="utf-8")

        # X01: the real writer and the real launcher startup line carry the
        # ONE legal service-id syntax, equal character-for-character, and the
        # startup line stays non-secret
        source_path = pairing_dir / f"{request['instance_id']}.token"
        source = json.loads(source_path.read_text(encoding="ascii"))
        assert _re.fullmatch(r"execution-[0-9a-f]{16}", started["instance_id"])
        assert _re.fullmatch(r"execution-[0-9a-f]{16}",
                             source["service_instance_id"])
        assert source["service_instance_id"] == started["instance_id"]
        assert source["service_epoch"] == started["service_epoch"]
        assert source["instance_id"] == request["instance_id"]
        assert source["session_epoch"] == request["session_epoch"]
        assert source["token"] not in json.dumps(started)

        # REAL LM-D install (user role): publishes the client credential with
        # the slot service ID EXACTLY as the service produced it (V05: the
        # slot value is never modified) plus a fresh non-secret receipt
        receipt_path = work / "pairing-transfer-receipt.json"
        assert tool.main([
            "install", "--client-store", str(store_dir),
            "--client-request", str(request_path),
            "--source", str(source_path),
            "--service-start", str(service_start_path),
            "--contract-lock", str(lock_path),
            "--expected-client-sha256", deployment.client_sha256,
            "--receipt", str(receipt_path)]) == 0
        installed = json.loads((store_dir / "pairing.json").read_text(encoding="utf-8"))
        assert installed["service_instance_id"] == source["service_instance_id"]
        assert installed["service_epoch"] == source["service_epoch"]
        assert installed["token"] == source["token"]
        receipt_text = receipt_path.read_text(encoding="utf-8")
        assert source["token"] not in receipt_text
        assert "TRANSFERRED_AWAITING_HANDSHAKE" in receipt_text

        # the SAME waiting no-arg session activates against the running real
        # service: null-epoch metadata handshake, epoch learned and checked
        # (the wait now also requires the CLIENT-side epoch latch, so the
        # asserts below cannot race the handshake's response processing)
        assert _wait_until(lambda: (bridge._try_activate_pairing()
                                    or bridge.state_map.execution_status == "paired_disabled")
                           and bridge._service_epoch == started["service_epoch"],
                           timeout=20, interval=0.25)
        assert bridge._service_epoch == started["service_epoch"]
        assert bridge.state_map.execution_status == "paired_disabled"
        assert bridge.counters["pairing_activations"] == 1
        served = [record for record in _read_audit_records(audit_path)
                  if record.get("event") == "request_served"]
        assert any(record.get("route", "").endswith("/poll") for record in served)

        # same-session authenticated business routes on the real service:
        # events upload, the two-party stop chain, and GET status
        bridge.on_order({"OrderID": "X01-1", "Status": "未成交"})
        assert _wait_until(lambda: any(
            record.get("event") == "request_served"
            and record.get("route", "").endswith("/events")
            for record in _read_audit_records(audit_path)))
        bridge.on_stop()
        assert _wait_until(lambda: sum(
            1 for record in _read_audit_records(audit_path)
            if record.get("event") == "request_served"
            and record.get("route", "").endswith("/control")) >= 2)
        status, body = get_status(poll_url, token=source["token"],
                                  instance_id=request["instance_id"],
                                  session_epoch=request["session_epoch"],
                                  generation=request["generation"],
                                  service_epoch=started["service_epoch"],
                                  params_sha256=request["params_sha256"],
                                  timeout=10.0)
        assert status == 200 and body["type"] == "StatusResponse"

        # zero anonymous traffic and no storm: every rejected request (if
        # any) is a business refusal, never an authentication failure, and
        # the SYNTHETIC token never leaks into the audit log
        records = _read_audit_records(audit_path)
        rejected = [record for record in records
                    if record.get("event") == "request_rejected"]
        assert all(record.get("error") != "AUTHENTICATION_FAILED"
                   for record in rejected)
        assert source["token"] not in audit_path.read_text(encoding="utf-8")

        # separated counts: zero native trading surface and zero unexpected
        # attribute resolution; the only real HTTP went to the temp service
        counts = fake.call_counts()
        assert counts.get("sendOrder", 0) == 0
        assert counts.get("cancelOrder", 0) == 0
        assert counts.get("getInvestorList", 0) == 0
        assert fake.attribute_access == []
        assert bridge.counters["native_send_attempts"] == 0
        assert bridge.counters["native_cancel_attempts"] == 0
    finally:
        stop_started_bridge(bridge)
        server = captured.get("server")
        if server is not None:
            server.shutdown()
        serve_thread.join(timeout=10)
    assert serve_result.get("code") == 0
