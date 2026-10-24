"""NE1-H23/H28/H29/H30/H31/H32/H33 client lifecycle for AKExecutionBridge (TL).

Thread sentinels, bounded lanes, stop barriers with one shared deadline,
revocation/close-gate linearization and idle behavior. All SYNTHETIC.

NE1-LM-R4 additions: M01 (no side effects, deterministic default store,
same-slot exclusivity) and M02 (persistent identity/facts, refusal matrix
with no in-memory fallback), plus the M04/M07 hook surface.

NE1-ARM-ARMING additions (decision NE1-ARB-ARMING-001): the armed fixture
now walks the real local arming chain (proposal -> plan -> request/
challenge -> one-time credential -> PARAMS_CONFIRMED ACK -> legal-callback
ARMED) instead of flipping a Params switch, and the lifecycle matrix gains
the arming rows (stop/set_params/event-gap revocation, control-lane drive).
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from infiny_native_fakes import (
    DEFAULT_STORE_ROOT,
    deploy_self_strategy,
    expected_default_store,
    load_client_from_temp,
    set_synthetic_gate_capture,
    stop_started_bridge,
    synthetic_binding,
    synthetic_dispatch,
    tree_snapshot,
)

ARMING_VERSION_V1 = "ne1-execution-20260918-arming-v1"
SYNTHETIC_ARMING_SERVICE_INSTANCE = "execution-0123456789abcdef"
SYNTHETIC_ARMING_SERVICE_EPOCH = 5
SYNTHETIC_CONTRACT_LOCK = "ee" * 32


def _nudge_control(bridge):
    """Wake the control lane for one prompt cycle: the lane samples its
    interval at the start of each cycle, so an armed-after-start round
    otherwise waits out the current 5 s disabled window. Only scheduling is
    advanced, never identity."""
    with bridge._state_lock:
        lane = bridge._lanes.get("control")
    if lane is not None:
        lane.wake.set()


def _utcnow_iso(value=None):
    moment = value or datetime.now(timezone.utc)
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _current_user_sid_text():
    """Current Windows user SID via the process token (test-side only)."""
    import ctypes
    from ctypes import wintypes
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    ker = ctypes.WinDLL("kernel32", use_last_error=True)
    ker.GetCurrentProcess.restype = wintypes.HANDLE
    ker.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD,
                                     ctypes.POINTER(wintypes.HANDLE))
    ker.OpenProcessToken.restype = wintypes.BOOL
    ker.LocalFree.argtypes = (wintypes.HANDLE,)
    ker.LocalFree.restype = wintypes.HANDLE
    adv.GetTokenInformation.argtypes = (wintypes.HANDLE, wintypes.INT, wintypes.LPVOID,
                                        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    adv.GetTokenInformation.restype = wintypes.BOOL
    adv.ConvertSidToStringSidW.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR))
    adv.ConvertSidToStringSidW.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    assert ker.OpenProcessToken(ker.GetCurrentProcess(), 0x0008, ctypes.byref(token))
    try:
        size = wintypes.DWORD(0)
        adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(max(size.value, 1))
        assert adv.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size))
        pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        text = wintypes.LPWSTR()
        assert adv.ConvertSidToStringSidW(ctypes.c_void_p(pointer), ctypes.byref(text))
        try:
            return text.value
        finally:
            ker.LocalFree(ctypes.cast(text, wintypes.HANDLE))
    finally:
        ker.CloseHandle(token)


def _apply_protected_dacl(path):
    """Test-side mirror of the reviewed tool discipline: a PROTECTED DACL
    granting only the current user."""
    import ctypes
    from ctypes import wintypes
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    adv.ConvertStringSidToSidW.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p))
    adv.ConvertStringSidToSidW.restype = wintypes.BOOL
    adv.GetLengthSid.argtypes = (ctypes.c_void_p,)
    adv.GetLengthSid.restype = wintypes.DWORD
    adv.InitializeAcl.argtypes = (ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD)
    adv.InitializeAcl.restype = wintypes.BOOL
    adv.AddAccessAllowedAce.argtypes = (ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                         ctypes.c_void_p)
    adv.AddAccessAllowedAce.restype = wintypes.BOOL
    adv.SetNamedSecurityInfoW.argtypes = (wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD,
                                           ctypes.c_void_p, ctypes.c_void_p,
                                           ctypes.c_void_p, ctypes.c_void_p)
    adv.SetNamedSecurityInfoW.restype = ctypes.c_ulong
    sid = ctypes.c_void_p()
    assert adv.ConvertStringSidToSidW(_current_user_sid_text(), ctypes.byref(sid))
    try:
        total = 16 + 8 + adv.GetLengthSid(sid)
        buffer = ctypes.create_string_buffer(total)
        assert adv.InitializeAcl(buffer, total, 2)
        assert adv.AddAccessAllowedAce(buffer, 2, 0x1F01FF, sid)
        result = adv.SetNamedSecurityInfoW(str(path), 1, 0x4 | 0x80000000,
                                           None, None, buffer, None)
        assert result == 0, f"SetNamedSecurityInfoW failed: {result}"
    finally:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.LocalFree.argtypes = [wintypes.HANDLE]
        kernel32.LocalFree(sid)


def _arming_idle_body(module, packet, service_epoch=SYNTHETIC_ARMING_SERVICE_EPOCH):
    now = _utcnow_iso()
    return module.canonical_dumps({
        "schema_version": module.TRANSPORT_SCHEMA_VERSION,
        "type": "PollResponse",
        "data": {**{key: packet["data"][key] for key in
                    ("request_id", "instance_id", "session_epoch", "generation",
                     "sequence", "sent_at", "params_sha256")},
                 "service_epoch": service_epoch, "received_at": now,
                 "request_service_epoch": packet["data"]["service_epoch"],
                 "binding": synthetic_binding(instance_id=packet["data"]["instance_id"],
                                               session_epoch=packet["data"]["session_epoch"]),
                 "ledger_revision": 1, "control_revision": 1, "kind": "IDLE",
                 "offer": None, "blockers": []}}).encode()


def _arming_control_ack(module, packet, confirmation_id, *, kind="RECORDED",
                        report_kind="PARAMS_CONFIRMED"):
    now = _utcnow_iso()
    return module.canonical_dumps({
        "schema_version": module.TRANSPORT_SCHEMA_VERSION,
        "type": "ControlResponse",
        "data": {**{key: packet["data"][key] for key in
                    ("request_id", "instance_id", "session_epoch", "generation",
                     "sequence", "params_sha256")},
                 "service_epoch": SYNTHETIC_ARMING_SERVICE_EPOCH,
                 "request_service_epoch": None, "sent_at": now, "received_at": now,
                 "binding": None, "ledger_revision": 1, "control_revision": 1,
                 "kind": kind, "report_kind": report_kind,
                 "confirmation_id": confirmation_id, "control_phase": "ARMED",
                 "readiness": {"mode": "NATIVE"}, "blockers": []}}).encode()


def _arming_transport(module, *, paths=None):
    """SYNTHETIC transport double: IDLE polls + exact RECORDED ACKs (service
    role). Real-socket lifecycle coverage stays with the LM-I integration
    suite; here the double keeps the arming ACK shape exact."""

    def transport(url, payload, timeout, headers=None):
        if paths is not None:
            paths.append(url)
        if url.endswith("/execution/v1/control"):
            data = payload["data"]
            confirmation = data.get("confirmation")
            cid = confirmation["confirmation_id"] if confirmation else None
            return 200, _arming_control_ack(module, payload, cid,
                                            report_kind=data.get("kind"))
        return 200, _arming_idle_body(module, payload)

    return transport


def _install_arming_proposal(bridge):
    """OPERATOR-role prepare-arm output (SYNTHETIC)."""
    store = Path(bridge._store_path)
    request_raw = (store / "pairing-request.json").read_bytes()
    request = json.loads(request_raw.decode("utf-8"))
    record = {
        "schema_version": "ne1_arming_proposal_v1",
        "proposal_id": uuid.uuid4().hex,
        "scope": {
            "pairing_request_id": request["request_id"],
            "pairing_request_sha256": hashlib.sha256(request_raw).hexdigest(),
            "instance_id": request["instance_id"],
            "session_epoch": request["session_epoch"],
            "generation": request["generation"],
            "process_instance_id": request["process_instance_id"],
            "deployment_key": request["deployment_key"],
            "strategy_id": request["strategy_id"],
            "client_file_sha256": request["client_file_sha256"],
            "adapter_version": request["adapter_version"],
            "contract_lock_sha256": SYNTHETIC_CONTRACT_LOCK,
            "execution_origin": f"http://127.0.0.1:{urlsplit(request['execution_api_url']).port}",
            "pairing_params_sha256": request["params_sha256"],
        },
        "binding": synthetic_binding(instance_id=bridge._instance_id,
                                     session_epoch=bridge._session_epoch),
        "operator_action_id": "SIM_ACTION_ARM_1",
        "authorization_id": "SIM_AUTH_ARM",
        "permit_id": "SIM_PERMIT_ARM",
        "execution_mode": "native",
        "risk_policy_id": "SIM_POLICY_ARM",
        "max_native_sends": 1,
        "max_native_cancels": 1,
        "created_at": _utcnow_iso(),
    }
    payload = json.dumps(record, sort_keys=True).encode("utf-8")
    temporary = store / ".arming-proposal.json.probe-tmp"
    temporary.write_bytes(payload)
    _apply_protected_dacl(temporary)
    os.replace(temporary, store / "arming-proposal.json")
    return record


def _install_arm_credential(bridge):
    """USER-role arming.json delivery (SYNTHETIC)."""
    store = Path(bridge._store_path)
    request_raw = (store / "arming-request.json").read_bytes()
    request = json.loads(request_raw.decode("utf-8"))
    plan = json.loads((store / "arming-plan.json").read_text(encoding="utf-8"))
    challenge = json.loads((store / "arming-challenge.json")
                           .read_text(encoding="utf-8"))
    expires = min(datetime.fromisoformat(request["expires_at"].replace("Z", "+00:00")),
                  datetime.fromisoformat(challenge["expires_at"].replace("Z", "+00:00")))
    record = {
        "schema_version": "ne1_client_arming_v1",
        "arm_id": uuid.uuid4().hex,
        "request_id": request["request_id"],
        "request_sha256": hashlib.sha256(request_raw).hexdigest(),
        "plan_id": plan["plan_id"],
        "scope": request["scope"],
        "binding": request["binding"],
        "target_params_sha256": request["target_params_sha256"],
        "authorization_id": request["authorization_id"],
        "authorization_record_sha256": "af" * 32,
        "permit_id": request["permit_id"],
        "permit_record_sha256": "bf" * 32,
        "service_instance_id": request["service_instance_id"],
        "service_epoch": request["service_epoch"],
        "expected_control_revision": 1,
        "prerequisites_id": "SIM_PREREQ_1",
        "prerequisites_sha256": "cf" * 32,
        "challenge": challenge["challenge"],
        "user_sid": _current_user_sid_text(),
        "action_source": "USER_LOCAL_CONSOLE",
        "explicit_user_action": True,
        "confirmed_at": _utcnow_iso(),
        "expires_at": _utcnow_iso(expires),
    }
    payload = json.dumps(record, sort_keys=True).encode("utf-8")
    temporary = store / ".arming.json.probe-tmp"
    temporary.write_bytes(payload)
    _apply_protected_dacl(temporary)
    os.replace(temporary, store / "arming.json")
    return record


def _armed(module, fake, tmp_path, *, paths=None, **overrides):
    """Full SYNTHETIC arming chain (R1.2 seam for pairing only): explicit
    store + injected native double + SYNTHETIC token; the round is armed by
    the local arming mechanism (proposal -> plan -> request/challenge ->
    USER credential -> exact ACK -> legal-callback ARMED), never by Params."""
    bridge = module.AKExecutionBridge(
        native=fake, transport=_arming_transport(module, paths=paths),
        store_directory=tmp_path / "store", pairing_token="ab" * 32)
    context_ready = threading.Event()
    original_accept = bridge._accept_snapshot_context

    def observe_context(data, packet, request_life):
        with bridge._state_lock:
            accepted = original_accept(data, packet, request_life)
            if accepted and bridge._arm_state == module.ARM_STATE_ARMED \
                    and bridge._snapshot_context_current():
                context_ready.set()
            return accepted

    bridge._accept_snapshot_context = observe_context
    bridge.strategy_id = 23
    bridge.strategy_name = "SYNTHETIC"
    bridge.set_params({"exchange": "SHFE", "instrument_id": "rb2610", "hedge_flag": "1",
                       "investor_id": "SYNTHETIC-ACCOUNT-A",
                       "execution_api_url": "http://127.0.0.1:17779/execution/v1/poll",
                       **overrides})
    bridge.on_init()
    bridge.on_start()
    # SYNTHETIC service identity facts for the injection seam (the real flow
    # learns them from the authenticated pairing handshake)
    bridge._service_instance_id = SYNTHETIC_ARMING_SERVICE_INSTANCE
    with bridge._service_epoch_lock:
        bridge._service_epoch = SYNTHETIC_ARMING_SERVICE_EPOCH
    _install_arming_proposal(bridge)
    bridge._process_arming_files()   # -> PREPARED_DISABLED
    bridge._process_arming_files()   # -> ARM_READY
    _install_arm_credential(bridge)
    bridge._process_arming_files()   # -> ARM_PENDING (+ exact ACK)
    bridge._pump_once()              # legal callback -> ARMED
    assert bridge._arm_state == module.ARM_STATE_ARMED
    assert bridge._arming_effective_now() is True
    assert bridge.params_map.enable_trading is False  # Params never armed anything
    _nudge_control(bridge)
    # A pre-arm context must retire before the independent gate precondition.
    try:
        assert context_ready.wait(3.0), "SYNTHETIC post-arm response context was not accepted"
        assert bridge._snapshot_context_current()
    except AssertionError:
        stop_started_bridge(bridge)
        raise
    set_synthetic_gate_capture(bridge, quote=True, account=True, position=True, contract=True)
    return bridge


def _armed_dispatch(bridge, *, kind="SUBMIT", command_id="SIM_CMD_1", ttl_ms=400,
                    order_id=None, issued=None):
    dispatch = synthetic_dispatch(binding=synthetic_binding(
        instance_id=bridge._instance_id, session_epoch=bridge._session_epoch),
        kind=kind, params_sha256=bridge._effective_params_sha256,
        command_id=command_id, ttl_ms=ttl_ms, order_id=order_id,
        issued=issued)
    dispatch["permit_id"] = bridge._arm_context.permit_id
    dispatch["control_revision"] = bridge._arm_context.control_revision
    return dispatch


def test_h28_worker_native_and_gui_all_rejected(monkeypatch, tmp_path):
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    bridge = _armed(module, fake, tmp_path)
    try:
        native_calls = [name for name, _, _ in fake.calls]
        assert set(native_calls) <= {"updateState", "updateParam", "writeLog", "getInvestorList", "subMarketData"}
        subscriptions = [(call, owner) for name, call, owner in fake.calls if name == "subMarketData"]
        assert subscriptions == [({"args": (), "kwargs": {"StrategyObj": bridge,
                                   "ExchangeID": "SHFE", "InstrumentID": "rb2610"}},
                                  threading.current_thread())]

        # any non-callback thread using the client's internal native-facing entry
        errors = []
        def wrong_thread():
            try:
                bridge._pump_once()
            except RuntimeError as exc:
                errors.append(str(exc))
        thread = threading.Thread(target=wrong_thread)
        thread.start(); thread.join(timeout=2)
        assert errors == ["legal_framework_callback_thread_required"]

        # constructor-vs-callback mismatch: a fresh bridge bound on this thread
        # rejects a different callback thread
        other = module.AKExecutionBridge(native=fake, store_directory=None)
        other.on_init()
        before = len(fake.calls)
        def other_thread():
            try:
                other.update_status_bar()
            except RuntimeError as exc:
                errors.append(str(exc))
        thread = threading.Thread(target=other_thread)
        thread.start(); thread.join(timeout=2)
        assert "legal_framework_callback_thread_required" in errors[-1]
        assert len(fake.calls) == before

        # updateState stays exactly two positional args
        state_calls = [call for name, call, _ in fake.calls if name == "updateState"]
        assert state_calls and all(len(call["args"]) == 2 and not call["kwargs"]
                                   for call in state_calls)
    finally:
        stop_started_bridge(bridge)


def test_h29_bounded_lanes_and_no_replacement_sender(monkeypatch, tmp_path):
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    fake.block_send.clear()  # one lane blocked long-term
    paths = []
    bridge = None
    try:
        bridge = _armed(module, fake, tmp_path, paths=paths)
        threads_before = {thread.ident for thread in threading.enumerate()
                          if thread.name.startswith("ne1-")}
        deadline = time.monotonic() + 1.2
        bridge._receive_dispatch(_armed_dispatch(bridge, ttl_ms=1500))
        bridge._pump_once()  # send lane blocks inside fake native (callback thread)
        while time.monotonic() < deadline:
            pass
        threads_after = {thread.ident for thread in threading.enumerate()
                         if thread.name.startswith("ne1-")}
        assert len(threads_after) <= len(threads_before) + 1  # blocked send thread
        # control lane keeps progressing while the send is blocked (SYNTHETIC
        # transport double; real-socket coverage belongs to the LM-I suite)
        assert any(path.endswith("/poll") for path in paths)
    finally:
        fake.block_send.set()
        stop_started_bridge(bridge)


def test_h30_stop_single_deadline_and_no_native_entry(monkeypatch, tmp_path):
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    bridge = _armed(module, fake, tmp_path)
    try:
        bridge._receive_dispatch(_armed_dispatch(bridge, ttl_ms=1500))
        # the measured window is the stop barrier itself (lane joins under the one
        # shared 3.5 s deadline); the best-effort OV-F05 control uploads have their
        # own http-timeout bound and are covered by the wire f05 tests, and their
        # socket-level latency is host-dependent (a refused connect can cost >2 s
        # under some network filters) — decoupled here so the deadline claim stays
        # the thing under test.
        monkeypatch.setattr(bridge, "_report_control", lambda *a, **k: False)
        started = time.monotonic()
        bridge.on_stop()
        elapsed = time.monotonic() - started
        assert elapsed <= 3.5 + 0.5, "stop must share one bounded deadline"
        assert fake.send_calls() == []  # stop before pump: no native entry
        # a stop-time pump (via a late callback) is refused by the thread/stop gates
        sends = len(fake.send_calls())
        assert sends == 0
        # stop closed and latched the armed round terminally
        assert bridge._arm_state == module.ARM_STATE_DISARMED_TERMINAL
        assert bridge._arming_effective_now() is False
        # repeated start/stop keeps a single lane set
        bridge.on_start()
        lanes_one = {thread.name for thread in threading.enumerate()
                     if thread.name.startswith("ne1-")}
        bridge.on_stop()
        bridge.on_start()
        lanes_two = {thread.name for thread in threading.enumerate()
                     if thread.name.startswith("ne1-")}
        assert len(lanes_two) <= len(lanes_one) + 3
    finally:
        stop_started_bridge(bridge)


def test_h31_close_gate_blocks_before_native(monkeypatch, tmp_path):
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    bridge = _armed(module, fake, tmp_path)
    try:
        dispatch = _armed_dispatch(bridge, ttl_ms=1500)
        # the user revokes the round just before the consuming callback
        bridge._revoke_arming("user_disarm")
        bridge._receive_dispatch(dispatch)
        bridge._pump_once()
        assert fake.send_calls() == []
        assert bridge._arm_state == module.ARM_STATE_DISARMED_TERMINAL
        assert bridge._arming_effective_now() is False
    finally:
        stop_started_bridge(bridge)


def test_h32_no_rearm_with_unresolved_dispatch(monkeypatch, tmp_path):
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    bridge = _armed(module, fake, tmp_path)
    try:
        bridge._receive_dispatch(_armed_dispatch(bridge, ttl_ms=1500))
        bridge.on_stop()
        bridge.set_params({"enable_trading": True})  # saved-true cannot rearm via the UI path
        assert bridge.params_map.enable_trading is False
        assert bridge._arm_state == module.ARM_STATE_DISARMED_TERMINAL  # latched by stop
    finally:
        stop_started_bridge(bridge)


def test_h33_idle_network_active_gui_frozen(monkeypatch, tmp_path):
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    paths = []
    bridge = None
    try:
        bridge = _armed(module, fake, tmp_path, paths=paths)
        state_calls = [call for name, call, _ in fake.calls if name == "updateState"]
        polled = len([path for path in paths if path.endswith("/poll")])
        time.sleep(0.6)  # no ticks at all
        state_after = [call for name, call, _ in fake.calls if name == "updateState"]
        polls_now = len([path for path in paths if path.endswith("/poll")])
        assert polls_now > polled                                  # control lane keeps polling
        assert len(state_after) == len(state_calls)                # GUI untouched by workers
        assert bridge.state_map.ui_idle_refresh == "UNSUPPORTED"
    finally:
        stop_started_bridge(bridge)


def test_h23_outbox_threshold_latches_event_gap(monkeypatch, tmp_path):
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    bridge = _armed(module, fake, tmp_path)
    try:
        for index in range(3):
            bridge.on_order({"OrderID": index, "Status": "未成交"})
        assert bridge.state_map.event_gap is False
        # the post-latch probe ticket is built while the round is still armed
        dispatch = _armed_dispatch(bridge, command_id="SIM_CMD_GAP", ttl_ms=1500)
        # simulate the store's unacked counter crossing the item cap (10 001)
        original_counts = bridge._stores.unacked_counts
        bridge._stores.unacked_counts = lambda: (module.OUTBOX_MAX_ITEMS + 1, 0)
        try:
            bridge.on_order({"OrderID": 99, "Status": "未成交"})
        finally:
            bridge._stores.unacked_counts = original_counts
        assert bridge.state_map.event_gap is True
        # AR2.2: an event gap is also an arming revocation trigger (latched)
        assert bridge._arm_state == module.ARM_STATE_DISARMED_TERMINAL
        assert bridge._arming_effective_now() is False
        bridge._receive_dispatch(dispatch)
        bridge._pump_once()
        assert fake.send_calls() == []  # latch closes the native gate
        # heartbeat/new ticks never clear the latch
        bridge.on_tick({"last_price": 1})
        assert bridge.state_map.event_gap is True
    finally:
        stop_started_bridge(bridge)


# ---------------------------------------------------------------------------
# C1.2 dispatch TTL window: DTTL-05 terminal fence + cross-process dedup
# (client-c1.2-ttl-prep.md test plan rows 5-7)
# ---------------------------------------------------------------------------

def test_dttl05_post_fsync_terminal_fence(monkeypatch, tmp_path):
    """Expiry crossing during the CONSUMED receipt write: zero call, terminal
    NO_CALL chain, committed CONSUMED fact retained, replay never retries."""
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    bridge = _armed(module, fake, tmp_path)
    try:
        frozen = datetime.now(timezone.utc)
        clock = {"now": frozen}
        monkeypatch.setattr(module, "_now_utc", lambda: clock["now"])
        # single time source: the dispatch is issued from the frozen clock
        # sample itself, so the fake never takes a second real-clock sample
        # (DTTL-05 straddle fix, E2 evidence 2026-09-20)
        command = _armed_dispatch(bridge, command_id="SIM_CMD_FENCE",
                                  ttl_ms=600, issued=frozen)
        original_record = bridge._record_receipt

        def record_and_advance(cmd, stage, *args, **kwargs):
            record = original_record(cmd, stage, *args, **kwargs)
            if stage == "CONSUMED":
                # the window closes while the CONSUMED write is in flight
                clock["now"] = frozen + timedelta(milliseconds=800)
            return record

        bridge._record_receipt = record_and_advance
        bridge._receive_dispatch(command)
        bridge._pump_once()
        assert fake.send_calls() == []  # zero native call
        stages = [row[0] for row in bridge._stores.receipt_for_command(command["command_id"])]
        assert stages == ["CONSUMED", "NO_CALL"]  # committed fact kept + terminal chain
        assert command["command_id"] in bridge._terminal_no_call
        assert bridge.counters["native_send_attempts"] == 0
        # replay of the same command: receipts only, never retried
        bridge._receive_dispatch(dict(command))
        bridge._pump_once()
        assert fake.send_calls() == []
        stages = [row[0] for row in bridge._stores.receipt_for_command(command["command_id"])]
        assert stages == ["CONSUMED", "NO_CALL", "NO_CALL"]
    finally:
        stop_started_bridge(bridge)


def test_c12_new_process_persisted_consumed_old_ticket_zero_call(monkeypatch, tmp_path):
    """H17/H32/OV-O05: the persistent receipt store is the dedup authority
    across processes — a new client life replays only receipts, zero calls."""
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    # process A consumes the command
    bridge_a = _armed(module, fake, tmp_path)
    try:
        command = _armed_dispatch(bridge_a, command_id="SIM_CMD_CROSS", ttl_ms=900)
        bridge_a._receive_dispatch(command)
        bridge_a._pump_once()
        assert len(fake.send_calls()) == 1
        bridge_a.on_stop()  # closes the persistent stores
    finally:
        stop_started_bridge(bridge_a)

    # process B: fresh instance/session/generation/arming round, same store
    bridge_b = _armed(module, fake, tmp_path)
    try:
        assert bridge_b._session_epoch != bridge_a._session_epoch
        bridge_b._receive_dispatch(dict(command))  # the old ticket replays
        bridge_b._pump_once()
        assert len(fake.send_calls()) == 1  # zero new native calls
        stages = [row[0] for row in bridge_b._stores.receipt_for_command(command["command_id"])]
        assert stages == ["CONSUMED", "RETURNED", "NO_CALL"]  # receipts-only re-upload
        assert command["command_id"] in bridge_b._terminal_no_call
        bridge_b.on_stop()
    finally:
        stop_started_bridge(bridge_b)


def test_c12_generation_epoch_roll_old_ticket_disabled(monkeypatch, tmp_path):
    """Decision line 60: after a session roll the old ticket is disabled with
    zero calls and leaves no consumption facts."""
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    bridge = _armed(module, fake, tmp_path)
    try:
        command = _armed_dispatch(bridge, command_id="SIM_CMD_ROLL", ttl_ms=1500)
        # the session rolls (restart-like) without the ticket being re-issued
        bridge._session_epoch = "a" + uuid.uuid4().hex
        bridge._generation = "b" + uuid.uuid4().hex
        bridge._receive_dispatch(command)
        bridge._pump_once()
        assert fake.send_calls() == []
        assert bridge.counters["native_call_blocked"] >= 1
        assert bridge._stores.receipt_for_command(command["command_id"]) == []
    finally:
        stop_started_bridge(bridge)


# ===========================================================================
# NE1-LM-R4 M01/M02 + M04/M07 hooks (decision NE1-ARB-PAIRING-LASTMILE-001
# §5 rows M01/M02; RULING 01). All deployments are temp self_strategy copies
# with the fake INFINIGO installed BEFORE the import; every identity here is
# SYNTHETIC and no real service, token or account exists.
# ===========================================================================

ADAPTER_VERSION_V2 = "ne1-execution-20260918-arming-v1-snapshot-20260930-r1"
_NOARG_PARAMS = {"exchange": "SHFE", "instrument_id": "rb2610", "hedge_flag": "1"}


class _NoHttp:
    """Any instantiation proves an anonymous network attempt."""

    def __init__(self, *args, **kwargs):
        raise AssertionError("network connection attempted by the no-arg client")


def _noarg_started(deployment, *, strategy_id=23, params=None):
    """No-form deployment mainline: the REAL framework build exposes no
    parameter form, so the default is NO set_params call at all — the Params
    Field defaults (the deployment constants) alone must carry a valid start."""
    bridge = deployment.no_arg(strategy_id=strategy_id, strategy_name="SYNTHETIC",
                               params=params)
    bridge.on_init()
    bridge.on_start()
    return bridge


def test_m01_import_construct_invalid_start_zero_side_effects(monkeypatch, tmp_path):
    deployment = deploy_self_strategy(monkeypatch, tmp_path)
    fake = deployment.fake
    monkeypatch.setattr(deployment.module, "HTTPConnection", _NoHttp)
    monkeypatch.setattr(deployment.module, "HTTPSConnection", _NoHttp)
    before = tree_snapshot(deployment.self_strategy_dir)
    threads_before = threading.active_count()

    # 2026-09-18 defaults fix, direction B: the deployment-value defaults never
    # legalize an EXPLICITLY blanked parameter — set_params with empty strings
    # still refuses to start ("parameters incomplete" safety semantics kept).
    bridge = deployment.no_arg(strategy_id=23, strategy_name="SYNTHETIC")
    bridge.set_params({"exchange": "SHFE", "instrument_id": "rb2610", "hedge_flag": ""})
    bridge.on_init()
    bridge.on_start()
    assert bridge.state_map.execution_status == "invalid"
    assert bridge._running is False
    assert bridge._instance_id is None  # identity never obtained

    # blank exchange/instrument on a never-confirmed bridge: the blank-restore
    # fallback finds no confirmed connection, so the explicit blanks stay empty
    blanks = deployment.no_arg(strategy_id=23, strategy_name="SYNTHETIC")
    blanks.set_params({"exchange": "", "instrument_id": "", "hedge_flag": "1"})
    blanks.on_init()
    blanks.on_start()
    assert blanks.state_map.execution_status == "invalid"
    assert blanks._running is False
    assert blanks._instance_id is None

    # framework assigned a non-integer strategy_id: refused, no store (the
    # valid defaults alone must not bypass the identity checks)
    assigned = deployment.no_arg()
    assigned.strategy_id = "23"
    assigned.on_init()
    assigned.on_start()
    assert assigned._last_refusal["code"] == "STRATEGY_ID_INVALID"

    # never assigned at all, zero set_params: refused, no store
    unassigned = deployment.no_arg()
    unassigned.on_init()
    unassigned.on_start()
    assert unassigned._last_refusal["code"] == "STRATEGY_ID_UNASSIGNED"

    assert tree_snapshot(deployment.self_strategy_dir) == before  # zero files
    assert threading.active_count() == threads_before             # zero threads
    assert fake.attribute_access == []                            # zero native attrs
    assert fake.call_counts().get("sendOrder", 0) == 0
    assert fake.call_counts().get("cancelOrder", 0) == 0
    assert fake.call_counts().get("getInvestorList", 0) == 0


def test_m01_valid_start_deterministic_store_wait_pairing_zero_http(monkeypatch, tmp_path):
    deployment = deploy_self_strategy(monkeypatch, tmp_path)
    module, fake = deployment.module, deployment.fake
    monkeypatch.setattr(module, "HTTPConnection", _NoHttp)
    monkeypatch.setattr(module, "HTTPSConnection", _NoHttp)
    bridge = _noarg_started(deployment)
    try:
        store = expected_default_store(deployment.self_strategy_dir, 23)
        assert store.is_dir(), "the deterministic default store must exist"
        assert bridge._resolve_store_directory() == str(store)
        assert bridge._store_path == str(store)
        assert bridge._pairing_state == "WAIT_PAIRING"
        assert bridge.state_map.execution_status == "wait_pairing"
        assert bridge.params_map.enable_trading is False  # start force-disables

        # 2026-09-18 defaults fix, direction A: with no parameter form and NO
        # set_params call, the Params Field defaults ARE the deployment values and
        # alone carry a valid start; every safety default stays untouched.
        assert bridge.params_map.exchange == "SHFE" == module.DEFAULT_EXCHANGE
        assert bridge.params_map.instrument_id == "rb2610" == module.DEFAULT_INSTRUMENT_ID
        assert bridge.params_map.hedge_flag == "1" == module.DEFAULT_HEDGE_FLAG
        assert bridge.params_map.investor_id == module.DEFAULT_INVESTOR_ID
        assert bridge.params_map.execution_mode == "disabled"
        assert bridge.params_map.max_native_sends == 0
        assert bridge.params_map.max_native_cancels == 0

        identity = json.loads((store / "identity.json").read_text(encoding="utf-8"))
        assert identity["schema_version"] == "ne1_client_identity_v1"
        assert identity["strategy_id"] == 23
        assert len(identity["instance_id"]) == 32
        assert all(char in "0123456789abcdef" for char in identity["instance_id"])
        request = json.loads((store / "pairing-request.json").read_text(encoding="utf-8"))
        assert request["schema_version"] == "ne1_pairing_request_v1"
        assert request["active"] is True
        assert request["store_layout_version"] == 2
        assert request["store_directory"] == str(store)
        assert request["adapter_version"] == module.ADAPTER_VERSION == ADAPTER_VERSION_V2
        assert request["client_file_sha256"] == deployment.client_sha256
        assert request["instance_id"] == identity["instance_id"] == bridge._instance_id
        assert request["session_epoch"] == bridge._session_epoch
        assert request["generation"] == bridge._generation
        assert request["params_sha256"] == bridge._params_sha256
        # the published description carries the deployment default connection
        assert request["exchange"] == "SHFE"
        assert request["instrument_id"] == "rb2610"
        assert request["hedge_flag"] == "1"

        time.sleep(0.35)  # lanes cycle while unpaired: still zero HTTP, zero native
        assert fake.attribute_access == []
        assert fake.call_counts().get("sendOrder", 0) == 0
        assert fake.call_counts().get("cancelOrder", 0) == 0
        assert fake.call_counts().get("getInvestorList", 0) == 0

        # CWD independence: the slot never depends on the working directory
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        probe = deployment.no_arg()
        probe.strategy_id = 23
        assert probe._resolve_store_directory() == str(store)

        bridge.on_stop()
        stopped = json.loads((store / "pairing-request.json").read_text(encoding="utf-8"))
        assert stopped["active"] is False  # stop atomically marks the request inactive
    finally:
        stop_started_bridge(bridge)


def test_m01_second_object_same_slot_refused_until_clean_stop(monkeypatch, tmp_path):
    deployment = deploy_self_strategy(monkeypatch, tmp_path)
    first = _noarg_started(deployment)
    second = deployment.no_arg(strategy_id=23, strategy_name="SYNTHETIC",
                               params=dict(_NOARG_PARAMS))
    second.on_init()
    try:
        second.on_start()
        assert second._last_refusal["code"] == "STORE_LOCK_HELD"
        assert second._running is False
        assert second._instance_id is None
        root = deployment.self_strategy_dir / DEFAULT_STORE_ROOT
        slots = sorted(item.name for item in root.iterdir())
        assert slots == ["strategy-23"]  # no alternate directory was invented
        assert first._pairing_state == "WAIT_PAIRING"  # first life unaffected

        first.on_stop()
        second.on_start()  # lock released by the clean stop
        assert second._running is True
        assert second._instance_id == first._instance_id  # same slot, same identity
        second.on_stop()
    finally:
        stop_started_bridge(first)
        stop_started_bridge(second)


def test_m01_cross_process_lock_holds_the_slot(monkeypatch, tmp_path):
    deployment = deploy_self_strategy(monkeypatch, tmp_path)
    warm = _noarg_started(deployment)
    try:
        store = Path(warm._store_path)
        warm.on_stop()  # the store exists; the OS lock is free again
    finally:
        stop_started_bridge(warm)
    script = (
        "import msvcrt, os, sys, time\n"
        "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR)\n"
        "msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)\n"
        "print('LOCKED', flush=True)\n"
        "time.sleep(30)\n"
    )
    holder = subprocess.Popen([sys.executable, "-B", "-X", "utf8", "-c", script,
                               str(store / "client.lock")],
                              stdout=subprocess.PIPE)
    try:
        assert b"LOCKED" in holder.stdout.readline()
        contender = deployment.no_arg(strategy_id=23, strategy_name="SYNTHETIC",
                                      params=dict(_NOARG_PARAMS))
        contender.on_init()
        contender.on_start()
        assert contender._last_refusal["code"] == "STORE_LOCK_HELD"
        assert contender._running is False
        # the file's existence is not the lock: only the OS lock counts
        assert (store / "client.lock").exists()
    finally:
        holder.kill()
        holder.wait(timeout=5)
    # the OS released the lock when the holder died
    try:
        contender.on_start()
        assert contender._running is True
        contender.on_stop()
    finally:
        stop_started_bridge(contender)


def test_m01_strategy_id_zero_is_a_legal_slot(monkeypatch, tmp_path):
    """R1.1: a framework-assigned 0 is a legal slot, distinct from the
    unassigned constructor default."""
    deployment = deploy_self_strategy(monkeypatch, tmp_path)
    bridge = _noarg_started(deployment, strategy_id=0)
    try:
        store = expected_default_store(deployment.self_strategy_dir, 0)
        assert store.is_dir()
        assert bridge._resolve_store_directory() == str(store)
        identity = json.loads((store / "identity.json").read_text(encoding="utf-8"))
        assert identity["strategy_id"] == 0
        assert bridge._running is True
        bridge.on_stop()
    finally:
        stop_started_bridge(bridge)


def test_m02_identity_and_business_facts_persist_across_lives(monkeypatch, tmp_path):
    deployment = deploy_self_strategy(monkeypatch, tmp_path)
    first = _noarg_started(deployment)
    try:
        first.on_order({"OrderID": "P-1", "Status": "未成交"})
        first.on_order({"OrderID": "P-2", "Status": "未成交"})
        store = Path(first._store_path)
        identity_bytes = (store / "identity.json").read_bytes()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            rows_before = first._stores.pending_events(16)
            if len(rows_before) >= 2:
                break
            time.sleep(0.02)
        assert len(rows_before) == 2
        streams_before = {json.loads(body)["data"]["event_stream_id"] for _, body in rows_before}
        first_session, first_generation = first._session_epoch, first._generation
        first_request = dict(first._pairing_request_record)
        first.on_stop()
    finally:
        stop_started_bridge(first)

    second = _noarg_started(deployment)
    try:
        assert second._instance_id == first._instance_id            # persistent identity
        assert second._session_epoch != first_session                # new session
        assert second._generation != first_generation                # new generation
        assert second._process_instance_id != first._process_instance_id
        assert second._pairing_request_record["request_id"] != first_request["request_id"]
        assert (store / "identity.json").read_bytes() == identity_bytes  # identity frozen
        rows_after = second._stores.pending_events(16)
        assert [row[0] for row in rows_after] == [row[0] for row in rows_before]  # M07: ids
        assert [row[1] for row in rows_after] == [row[1] for row in rows_before]  # M07: bytes
        # new-generation events get a NEW stream id; numbering never renumbers old rows
        second.on_order({"OrderID": "P-3", "Status": "未成交"})
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            rows_now = second._stores.pending_events(16)
            if len(rows_now) >= 3:
                break
            time.sleep(0.02)
        parsed = [json.loads(body)["data"] for _, body in rows_now]
        assert parsed[2]["event_stream_id"] not in streams_before  # new generation stream
        assert parsed[0]["event_stream_id"] in streams_before      # old rows untouched
        # per-stream numbering: the old stream keeps 1,2; the new stream starts
        # fresh at 1 — old rows are never renumbered into the new stream
        assert [item["event_sequence"] for item in parsed[:2]] == [1, 2]
        assert parsed[2]["event_sequence"] == 1
        second.on_stop()
    finally:
        stop_started_bridge(second)


def test_m02_repeated_start_on_running_object_is_idempotent(monkeypatch, tmp_path):
    deployment = deploy_self_strategy(monkeypatch, tmp_path)
    bridge = _noarg_started(deployment)
    try:
        request_path = Path(bridge._store_path) / "pairing-request.json"
        request_bytes = request_path.read_bytes()
        session, generation = bridge._session_epoch, bridge._generation
        request_id = bridge._pairing_request_record["request_id"]
        bridge.on_start()  # repeated start: idempotent no-op
        assert bridge._running is True
        assert bridge._session_epoch == session
        assert bridge._generation == generation
        assert bridge._pairing_request_record["request_id"] == request_id
        assert request_path.read_bytes() == request_bytes
        identity = json.loads((Path(bridge._store_path) / "identity.json")
                              .read_text(encoding="utf-8"))
        assert identity["instance_id"] == bridge._instance_id  # identity not regenerated
        bridge.on_stop()
    finally:
        stop_started_bridge(bridge)


@pytest.mark.parametrize("mutation,expected_code", [
    ("corrupt", "STORE_IDENTITY_INVALID"),
    ("schema", "STORE_IDENTITY_INVALID"),
    ("instance", "STORE_IDENTITY_INVALID"),
    ("deployment", "STORE_SLOT_MISMATCH"),
    ("strategy", "STORE_IDENTITY_INVALID"),
    ("truncated", "STORE_IDENTITY_INVALID"),
])
def test_m02_bad_identity_refused_without_regeneration(monkeypatch, tmp_path,
                                                       mutation, expected_code):
    deployment = deploy_self_strategy(monkeypatch, tmp_path)
    warm = _noarg_started(deployment)
    try:
        identity_path = Path(warm._store_path) / "identity.json"
        good = json.loads(identity_path.read_text(encoding="utf-8"))
        warm.on_stop()
    finally:
        stop_started_bridge(warm)
    if mutation == "corrupt":
        payload = b"{not json at all"
    elif mutation == "truncated":
        payload = json.dumps(good).encode("utf-8")[:30]
    else:
        broken = dict(good)
        if mutation == "schema":
            broken["schema_version"] = "ne1_client_identity_v0"
        elif mutation == "instance":
            broken["instance_id"] = "NOT-HEX-32"
        elif mutation == "deployment":
            broken["deployment_key"] = "ee" * 32
        elif mutation == "strategy":
            broken["strategy_id"] = 99
        payload = (json.dumps(broken, sort_keys=True) + "\n").encode("utf-8")
    identity_path.write_bytes(payload)

    contender = deployment.no_arg(strategy_id=23, strategy_name="SYNTHETIC",
                                  params=dict(_NOARG_PARAMS))
    contender.on_init()
    contender.on_start()
    assert contender._last_refusal["code"] == expected_code
    assert contender._running is False
    assert contender._instance_id is None
    assert contender._stores is None  # no in-memory fallback path exists
    # the broken bytes are preserved verbatim; no second UUID was generated
    assert identity_path.read_bytes() == payload


def test_m02_legacy_store_refused_and_bytes_preserved(monkeypatch, tmp_path):
    deployment = deploy_self_strategy(monkeypatch, tmp_path)
    # an r3-era store: receipts/outbox without identity.json
    legacy = deployment.self_strategy_dir / DEFAULT_STORE_ROOT / "strategy-41"
    legacy.mkdir(parents=True)
    for name in ("receipts.db", "outbox.db"):
        handle = sqlite3.connect(legacy / name)
        handle.execute("CREATE TABLE t (x)")
        handle.commit()
        handle.close()
    digests = {name: hashlib.sha256((legacy / name).read_bytes()).hexdigest()
               for name in ("receipts.db", "outbox.db")}
    bridge = deployment.no_arg(strategy_id=41, params=dict(_NOARG_PARAMS))
    bridge.on_init()
    bridge.on_start()
    assert bridge._last_refusal["code"] == "LEGACY_STORE_MIGRATION_REQUIRED"
    assert bridge._running is False
    assert not (legacy / "identity.json").exists()  # never claimed silently
    assert {name: hashlib.sha256((legacy / name).read_bytes()).hexdigest()
            for name in ("receipts.db", "outbox.db")} == digests  # bytes preserved

    # a layout-2 store carrying the legacy token meta is equally refused
    warm = _noarg_started(deployment)
    try:
        store = Path(warm._store_path)
        warm.on_stop()
    finally:
        stop_started_bridge(warm)
    outbox = sqlite3.connect(store / "outbox.db")
    with outbox:
        outbox.execute("INSERT OR REPLACE INTO meta VALUES ('pairing_token', 'legacy')")
    outbox.close()
    again = deployment.no_arg(strategy_id=23, params=dict(_NOARG_PARAMS))
    again.on_init()
    again.on_start()
    assert again._last_refusal["code"] == "LEGACY_STORE_MIGRATION_REQUIRED"
    assert again._running is False
    assert again._pairing_token is None  # the old token is never restored


def test_m02_readonly_store_objects_refuse_restart(monkeypatch, tmp_path):
    deployment = deploy_self_strategy(monkeypatch, tmp_path)
    warm = _noarg_started(deployment)
    try:
        store = Path(warm._store_path)
        warm.on_stop()
    finally:
        stop_started_bridge(warm)

    request_path = store / "pairing-request.json"
    request_bytes = request_path.read_bytes()
    os.chmod(request_path, 0o444)  # Windows read-only attribute
    try:
        blocked = deployment.no_arg(strategy_id=23, params=dict(_NOARG_PARAMS))
        blocked.on_init()
        blocked.on_start()
        assert blocked._last_refusal["code"] == "STORE_PUBLISH_FAILED"
        assert blocked._running is False
        assert request_path.read_bytes() == request_bytes  # old bytes intact
    finally:
        os.chmod(request_path, 0o666)

    # an already-checkpointed WAL database opens read-only without writing;
    # a 0-byte readonly database forces the first write (header) and refuses
    receipts = store / "receipts.db"
    for sidecar in (store / "receipts.db-wal", store / "receipts.db-shm"):
        sidecar.unlink(missing_ok=True)
    receipts.write_bytes(b"")
    os.chmod(receipts, 0o444)
    try:
        denied = deployment.no_arg(strategy_id=23, params=dict(_NOARG_PARAMS))
        denied.on_init()
        denied.on_start()
        assert denied._last_refusal["code"] == "STORE_DB_INVALID"
        assert denied._running is False
    finally:
        os.chmod(receipts, 0o666)


def test_m02_persist_failure_refused_no_memory_fallback(monkeypatch, tmp_path):
    module, fake = load_client_from_temp(monkeypatch, tmp_path)

    def failing_fsync(fd):
        raise OSError("synthetic full disk")

    monkeypatch.setattr(os, "fsync", failing_fsync)
    bridge = module.AKExecutionBridge(native=fake, store_directory=tmp_path / "s1")
    bridge.strategy_id = 23
    bridge.set_params(dict(_NOARG_PARAMS))
    bridge.on_init()
    try:
        bridge.on_start()
        assert bridge._last_refusal["code"] == "STORE_IDENTITY_WRITE_FAILED"
        assert bridge._running is False
        assert bridge._stores is None            # no in-memory substitute store
        assert not list((tmp_path / "s1").glob("*.db"))  # no business DB without identity
        monkeypatch.undo()
        # recovery on a fresh directory works after the failure is gone
        bridge.on_start()
        assert bridge._running is True
        assert bridge._pairing_state == "WAIT_PAIRING"
        bridge.on_stop()
    finally:
        stop_started_bridge(bridge)


def test_m02_atomic_publish_failure_refuses_start(monkeypatch, tmp_path):
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    warm = module.AKExecutionBridge(native=fake, store_directory=tmp_path / "s1")
    warm.strategy_id = 23
    warm.set_params(dict(_NOARG_PARAMS))
    warm.on_init()
    try:
        warm.on_start()
        warm.on_stop()
    finally:
        stop_started_bridge(warm)

    def failing_replace(src, dst):
        raise OSError("synthetic publish failure")

    monkeypatch.setattr(os, "replace", failing_replace)
    bridge = module.AKExecutionBridge(native=fake, store_directory=tmp_path / "s1")
    bridge.strategy_id = 23
    bridge.set_params(dict(_NOARG_PARAMS))
    bridge.on_init()
    try:
        bridge.on_start()
        assert bridge._last_refusal["code"] == "STORE_PUBLISH_FAILED"
        assert bridge._running is False
        monkeypatch.undo()
        # identity survived; a later start republishes cleanly
        bridge.on_start()
        assert bridge._running is True
        bridge.on_stop()
    finally:
        stop_started_bridge(bridge)


def test_m02_path_replacement_and_links_refused(monkeypatch, tmp_path):
    deployment = deploy_self_strategy(monkeypatch, tmp_path)
    root = deployment.self_strategy_dir / DEFAULT_STORE_ROOT
    root.mkdir()
    # a FILE where the store directory should be
    (root / "strategy-23").write_text("not a directory")
    bridge = deployment.no_arg(strategy_id=23, params=dict(_NOARG_PARAMS))
    bridge.on_init()
    bridge.on_start()
    assert bridge._last_refusal["code"] == "STORE_PATH_INVALID"
    assert bridge._running is False

    # a symlinked store directory (reparse point) is rejected even though
    # the target is a perfectly valid directory
    real = tmp_path / "real-store"
    real.mkdir()
    os.symlink(real, root / "strategy-24")
    linked = deployment.no_arg(strategy_id=24, params=dict(_NOARG_PARAMS))
    linked.on_init()
    linked.on_start()
    assert linked._last_refusal["code"] == "STORE_PATH_INVALID"
    assert linked._running is False


def test_m02_stop_incomplete_keeps_store_busy(monkeypatch, tmp_path):
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    bridge = module.AKExecutionBridge(native=fake, store_directory=tmp_path / "s1",
                                      pairing_token="ab" * 32)
    bridge.strategy_id = 23
    bridge.set_params(dict(_NOARG_PARAMS))
    bridge.on_init()
    bridge.on_start()
    # a lane worker that misses the bounded join deadline: block INSIDE the
    # cycle (a lane merely waiting would exit cleanly on the retire wake)
    entered = threading.Event()
    release = threading.Event()

    def stuck_cycle():
        entered.set()
        release.wait(30)

    monkeypatch.setattr(bridge, "_events_cycle", stuck_cycle)
    assert entered.wait(timeout=2), "the events lane never entered the stuck cycle"
    try:
        bridge.on_stop()
        assert bridge.state_map.execution_status == "stop_incomplete"
        assert bridge._stores is not None           # store/lock NOT handed over
        assert bridge._client_lock is not None
        # the same object cannot restart over the stuck lanes
        bridge.on_start()
        assert bridge._last_refusal["code"] == "STORE_BUSY"
        assert bridge._running is False
        # and another object stays blocked by the still-held OS lock
        contender = module.AKExecutionBridge(native=fake, store_directory=tmp_path / "s1",
                                             pairing_token="ab" * 32)
        contender.strategy_id = 23
        contender.set_params(dict(_NOARG_PARAMS))
        contender.on_init()
        contender.on_start()
        assert contender._last_refusal["code"] == "STORE_LOCK_HELD"
        assert contender._running is False
    finally:
        release.set()  # let the stuck lane exit after the assertions


def test_m04_hook_surface_shapes(monkeypatch, tmp_path):
    """M04/M07 hook surface for the integration owner: the documented
    R1.2 methods exist with the frozen shapes on an isolated client."""
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    bridge = module.AKExecutionBridge(native=fake, store_directory=tmp_path / "hook")
    bridge.strategy_id = 23
    assert bridge._resolve_store_directory() == str(tmp_path / "hook")
    state = bridge._open_client_state()
    assert {"instance_id", "store_directory", "store_layout_version",
            "deployment_key", "client_file_sha256"} <= set(state)
    assert state["store_layout_version"] == 2
    bridge._release_store()  # hand the probe store back before the real start
    bridge.set_params(dict(_NOARG_PARAMS))
    bridge.on_init()
    bridge.on_start()
    try:
        record = bridge._publish_pairing_request()
        assert set(record) == {"schema_version", "request_id", "active", "created_at",
                               "instance_id", "session_epoch", "generation",
                               "process_instance_id", "deployment_key", "strategy_id",
                               "adapter_version", "client_file_sha256",
                               "store_layout_version", "store_directory",
                               "exchange", "instrument_id", "hedge_flag",
                               "execution_api_url", "params_sha256"}
        published = json.loads((Path(bridge._store_path) / "pairing-request.json")
                               .read_text(encoding="utf-8"))
        assert published == record
        assert bridge._try_activate_pairing() is False  # no credential: waits
        assert bridge._last_pairing_reason == "PAIRING_MISSING"
        bridge.on_stop()
    finally:
        stop_started_bridge(bridge)


# ===========================================================================
# NE1-ARM-ARMING lifecycle rows (decision NE1-ARB-ARMING-001 RULING 06 C
# side): set_params invalidation, control-lane drive of the whole chain, and
# the stop latch with an honest state file. All SYNTHETIC.
# ===========================================================================

def test_arm_set_params_invalidates_the_round(monkeypatch, tmp_path):
    """AR2.1: a successful set_params application on a running armed life
    voids the candidates, confirmation and active ArmContext; re-arming in
    this session is impossible."""
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    bridge = _armed(module, fake, tmp_path)
    try:
        assert bridge._arming_effective_now() is True
        pairing_before = bridge._params_sha256
        probe = _armed_dispatch(bridge, command_id="SIM_CMD_SET", ttl_ms=1500)
        bridge.set_params({"order_volume": 2})  # ANY successful application
        assert bridge._arm_state == module.ARM_STATE_DISARMED_TERMINAL
        assert bridge._last_arming_reason == "params_set"
        assert bridge._arm_context is None
        # the effective hash fell back to the pairing hash of the armed round
        assert bridge._effective_params_sha256 == pairing_before
        set_synthetic_gate_capture(bridge, quote=True, account=True, position=True, contract=True)
        bridge._receive_dispatch(probe)
        bridge._pump_once()
        assert fake.send_calls() == []
        # validation failure leaves everything untouched (no half update)
        with pytest.raises(ValueError):
            bridge.set_params({"order_volume": 0})
        assert bridge.params_map.order_volume == 2
        bridge.on_stop()
    finally:
        stop_started_bridge(bridge)


def test_arm_control_lane_drives_the_whole_chain(monkeypatch, tmp_path):
    """AR2.3: the background control lane alone carries the round from files
    to ARM_PENDING (files, persistence, confirmation — no native, no GUI);
    only the legal callback finishes ARMED."""
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    bridge = module.AKExecutionBridge(
        native=fake, transport=_arming_transport(module),
        store_directory=tmp_path / "store", pairing_token="ab" * 32)
    bridge.strategy_id = 23
    bridge.set_params({"exchange": "SHFE", "instrument_id": "rb2610", "hedge_flag": "1",
                       "investor_id": "SYNTHETIC-ACCOUNT-A"})
    bridge.on_init()
    bridge.on_start()
    try:
        bridge._service_instance_id = SYNTHETIC_ARMING_SERVICE_INSTANCE
        with bridge._service_epoch_lock:
            bridge._service_epoch = SYNTHETIC_ARMING_SERVICE_EPOCH
        _install_arming_proposal(bridge)
        calls_before = len(fake.calls)
        for _ in range(2):  # one admission step per lane cycle: PREPARED, READY
            bridge._control_cycle()
        _install_arm_credential(bridge)
        bridge._control_cycle()  # consumes the credential + sends the confirmation
        assert calls_before == len(fake.calls)          # zero native/GUI
        assert bridge._arm_state == module.ARM_STATE_ARM_PENDING
        assert bridge._arming_ack_received is True      # exact ACK recorded
        assert bridge._arming_effective_now() is False  # lane never arms
        assert bridge.counters["poll_requests"] >= 1     # the paired polls ran too
        bridge._pump_once()  # the legal callback finishes the round
        assert bridge._arm_state == module.ARM_STATE_ARMED
        assert bridge._arming_effective_now() is True
        bridge.on_stop()
    finally:
        stop_started_bridge(bridge)


def test_arm_stop_latches_terminal_with_honest_state_file(monkeypatch, tmp_path):
    """AR5.1/AR5.2: stop closes and latches the round; the non-secret state
    file reports the closure without inventing service-side facts; the next
    life of the same object starts DISARMED and cannot consume old slots."""
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    bridge = _armed(module, fake, tmp_path)
    store = Path(bridge._store_path)
    try:
        bridge.on_stop()
        assert bridge._arm_state == module.ARM_STATE_DISARMED_TERMINAL
        state = json.loads((store / "arming-state.json").read_text(encoding="utf-8"))
        assert state["state"] == "DISARMED_TERMINAL"
        assert state["local_gate_closed"] is True
        assert state["revoked"] is True
        assert state["effective_enable_trading"] is False
        # the service-side barrier is never claimed by a local-only stop
        assert state["service_gate_closed"] is False
        # a fresh life of the same object never consumes the old request
        bridge.on_start()
        assert bridge._arm_state == module.ARM_STATE_DISARMED
        assert bridge._arming_effective_now() is False
        bridge._process_arming_files()
        assert bridge._arm_state == module.ARM_STATE_DISARMED
        assert fake.send_calls() == []
        bridge.on_stop()
    finally:
        stop_started_bridge(bridge)
