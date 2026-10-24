"""NE1-H01/H05/H06/H07/H24/H34 client gates for AKExecutionBridge (TG).

All identities are SYNTHETIC; the client file is loaded isolated from the
repository with a fake INFINIGO sentinel (no real native attribute resolves).
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from infiny_native_fakes import (
    ArmingClock,
    FakeExecutionService,
    install_fake_native,
    load_client_from_temp,
    set_synthetic_gate_capture,
    stop_started_bridge,
    synthetic_binding,
    synthetic_dispatch,
    synthetic_offer,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / "tests/fixtures/infiny_native_execution/synthetic_cases.json")
                     .read_text(encoding="utf-8"))

# ---------------------------------------------------------------------------
# NE1-ARM-ARMING SYNTHETIC fixtures (decision NE1-ARB-ARMING-001, C row of
# RULING 06). Every value below is SYNTHETIC: the operator proposal, the user
# credential, the service ACK and the service identity facts are all test
# roles acting through the client's public file/processing surface.
# ---------------------------------------------------------------------------
SYNTHETIC_ARMING_SERVICE_INSTANCE = "execution-0123456789abcdef"
SYNTHETIC_ARMING_SERVICE_EPOCH = 5
SYNTHETIC_CONTRACT_LOCK = "ee" * 32
ARMING_PROPOSAL_SCHEMA = "ne1_arming_proposal_v1"
CLIENT_ARMING_SCHEMA = "ne1_client_arming_v1"
CLIENT_DISARM_SCHEMA = "ne1_client_disarm_v1"


def _utcnow_iso(value=None):
    moment = value or datetime.now(timezone.utc)
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _control_ack_body(module, packet, confirmation_id, *, kind="RECORDED",
                      report_kind="PARAMS_CONFIRMED",
                      epoch=SYNTHETIC_ARMING_SERVICE_EPOCH):
    """SYNTHETIC service-role ControlResponse: the exact ACK echo the client
    mirror decodes (RECORDED by default; BLOCKED for negative probes)."""
    now = _utcnow_iso()
    return module.canonical_dumps({
        "schema_version": module.TRANSPORT_SCHEMA_VERSION,
        "type": "ControlResponse",
        "data": {**{key: packet["data"][key] for key in
                    ("request_id", "instance_id", "session_epoch", "generation",
                     "sequence", "params_sha256")},
                 "service_epoch": epoch, "request_service_epoch": None,
                 "sent_at": now, "received_at": now, "binding": None,
                 "ledger_revision": 1, "control_revision": 1, "kind": kind,
                 "report_kind": report_kind, "confirmation_id": confirmation_id,
                 "control_phase": "ARMED", "readiness": {"mode": "NATIVE"},
                 "blockers": []}}).encode()


def _arming_transport(module, arm_box, *, epoch=SYNTHETIC_ARMING_SERVICE_EPOCH,
                      paths=None):
    """SYNTHETIC transport double: IDLE polls + the exact RECORDED ACK for
    every PARAMS_CONFIRMED (service role). ``arm_box["control_mode"]`` switches
    the control reply at call time: "ok" (default), "fail" (http 500) or
    "blocked" (exact-shape BLOCKED ACK)."""

    def transport(url, payload, timeout, headers=None):
        if paths is not None:
            paths.append(url)
        if url.endswith("/execution/v1/control"):
            data = payload["data"]
            if data.get("kind") == "PARAMS_CONFIRMED":
                confirmation = data["confirmation"]
                arm_box.setdefault("confirmations", []).append(confirmation)
                mode = arm_box.get("control_mode", "ok")
                if mode == "fail":
                    return 500, b"{}"
                if mode == "blocked":
                    return 200, _control_ack_body(
                        module, payload, confirmation["confirmation_id"], kind="BLOCKED")
                return 200, _control_ack_body(
                    module, payload, confirmation["confirmation_id"], epoch=epoch)
            return 200, _control_ack_body(module, payload, None,
                                          report_kind=data.get("kind"), epoch=epoch)
        return 200, _epoch_idle_body(module, payload, epoch)

    return transport


def _install_arming_proposal(bridge, *, sends=1, cancels=1,
                             risk_policy_id="SIM_POLICY_ARM",
                             authorization_id="SIM_AUTH_ARM",
                             permit_id="SIM_PERMIT_ARM", mutate=None):
    """OPERATOR-role prepare-arm output (SYNTHETIC): the non-secret proposal
    the reviewed tool publishes into the store."""
    store = Path(bridge._store_path)
    request_raw = (store / "pairing-request.json").read_bytes()
    request = json.loads(request_raw.decode("utf-8"))
    record = {
        "schema_version": ARMING_PROPOSAL_SCHEMA,
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
        "authorization_id": authorization_id,
        "permit_id": permit_id,
        "execution_mode": "native",
        "risk_policy_id": risk_policy_id,
        "max_native_sends": sends,
        "max_native_cancels": cancels,
        "created_at": _utcnow_iso(),
    }
    if mutate is not None:
        record = mutate(record)
    payload = json.dumps(record, sort_keys=True).encode("utf-8")
    temporary = store / ".arming-proposal.json.probe-tmp"
    temporary.write_bytes(payload)
    _apply_protected_dacl(temporary)
    os.replace(temporary, store / "arming-proposal.json")
    return record


def _install_arm_credential(bridge, *, challenge=None, mutate=None):
    """USER-role arming.json delivery (SYNTHETIC): reads the private request
    and challenge slots exactly like the reviewed interactive tool does."""
    store = Path(bridge._store_path)
    request_raw = (store / "arming-request.json").read_bytes()
    request = json.loads(request_raw.decode("utf-8"))
    plan = json.loads((store / "arming-plan.json").read_text(encoding="utf-8"))
    challenge_record = json.loads((store / "arming-challenge.json")
                                  .read_text(encoding="utf-8"))
    challenge_value = challenge_record["challenge"] if challenge is None else challenge
    expires = min(datetime.fromisoformat(request["expires_at"].replace("Z", "+00:00")),
                  datetime.fromisoformat(challenge_record["expires_at"].replace("Z", "+00:00")))
    record = {
        "schema_version": CLIENT_ARMING_SCHEMA,
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
        "challenge": challenge_value,
        "user_sid": _current_user_sid_text(),
        "action_source": "USER_LOCAL_CONSOLE",
        "explicit_user_action": True,
        "confirmed_at": _utcnow_iso(),
        "expires_at": _utcnow_iso(expires),
    }
    if mutate is not None:
        record = mutate(record)
    payload = json.dumps(record, sort_keys=True).encode("utf-8")
    temporary = store / ".arming.json.probe-tmp"
    temporary.write_bytes(payload)
    _apply_protected_dacl(temporary)
    os.replace(temporary, store / "arming.json")
    return record


def _install_disarm(bridge, scope=None, *, mutate=None):
    """USER-role disarm.json (SYNTHETIC): the scoped revocation request.
    arm_request_id is null when no request slot exists yet (AR4.2)."""
    store = Path(bridge._store_path)
    if scope is None:
        scope = json.loads((store / "arming-plan.json").read_text(encoding="utf-8"))["scope"]
    request_path = store / "arming-request.json"
    arm_request_id = None
    if request_path.exists():
        arm_request_id = json.loads(request_path.read_text(encoding="utf-8"))["request_id"]
    record = {
        "schema_version": CLIENT_DISARM_SCHEMA,
        "disarm_id": uuid.uuid4().hex,
        "scope": scope,
        "arm_request_id": arm_request_id,
        "arm_id": None,
        "requested_at": _utcnow_iso(),
        "user_sid": _current_user_sid_text(),
        "reason": "USER_DISARM",
    }
    if mutate is not None:
        record = mutate(record)
    payload = json.dumps(record, sort_keys=True).encode("utf-8")
    temporary = store / ".disarm.json.probe-tmp"
    temporary.write_bytes(payload)
    _apply_protected_dacl(temporary)
    os.replace(temporary, store / "disarm.json")
    return record


def _prepare_arming(module, fake, tmp_path, arm_box, *, paths=None,
                    store_name="store", **param_overrides):
    """The shared arming chain prefix: started + paired (injection seam) +
    SYNTHETIC service facts + operator proposal processed to a frozen plan."""
    bridge = module.AKExecutionBridge(
        native=fake, transport=_arming_transport(module, arm_box, paths=paths),
        store_directory=tmp_path / store_name, pairing_token="ab" * 32)
    bridge.strategy_id = 23
    bridge.strategy_name = "SYNTHETIC"
    bridge.set_params({"exchange": "SHFE", "instrument_id": "rb2610", "hedge_flag": "1",
                       "investor_id": "SYNTHETIC-ACCOUNT-A", "order_type": "GFD",
                       **param_overrides})
    bridge.on_init()
    bridge.on_start()
    # SYNTHETIC service identity facts for the injection seam (the real flow
    # learns them from the authenticated pairing handshake)
    bridge._service_instance_id = SYNTHETIC_ARMING_SERVICE_INSTANCE
    with bridge._service_epoch_lock:
        bridge._service_epoch = SYNTHETIC_ARMING_SERVICE_EPOCH
    return bridge


def armed_bridge(module, fake, tmp_path, **param_overrides):
    """Full SYNTHETIC positive arming chain (ARM-M07 client side): pairing
    seam -> proposal -> plan -> one-time request/challenge -> USER credential
    -> consumption + exact PARAMS_CONFIRMED ACK -> legal-callback ARMED.
    Params stay enable_trading=False/disabled/zero the whole time."""
    arm_box = {}
    bridge = _prepare_arming(module, fake, tmp_path, arm_box, **param_overrides)
    _install_arming_proposal(bridge)
    bridge._process_arming_files()   # DISARMED -> PREPARED_DISABLED
    bridge._process_arming_files()   # PREPARED_DISABLED -> ARM_READY
    _install_arm_credential(bridge)
    bridge._process_arming_files()   # ARM_READY -> ARM_PENDING (+ exact ACK)
    bridge._pump_once()              # legal framework callback -> ARMED
    assert bridge._arm_state == module.ARM_STATE_ARMED
    assert bridge._arming_effective_now() is True
    assert bridge.params_map.enable_trading is False
    assert bridge.params_map.execution_mode == "disabled"
    set_synthetic_gate_capture(bridge, quote=True, account=True, position=True, contract=True)
    return bridge


def armed_dispatch(bridge, *, kind="SUBMIT", command_id="SIM_CMD_1", ttl_ms=400,
                   order_id=None, binding_overrides=None, issued=None):
    """A dispatch that matches the ARMED round: target hash, frozen permit,
    expected control revision and the live binding identity."""
    binding = synthetic_binding()
    binding.update({"instance_id": bridge._instance_id,
                    "session_epoch": bridge._session_epoch})
    binding.update(binding_overrides or {})
    dispatch = synthetic_dispatch(
        binding=binding,
        kind=kind, params_sha256=bridge._effective_params_sha256,
        command_id=command_id, ttl_ms=ttl_ms, order_id=order_id, issued=issued)
    dispatch["permit_id"] = bridge._arm_context.permit_id
    dispatch["control_revision"] = bridge._arm_context.control_revision
    return dispatch


def test_h01_disabled_load_resolves_no_native_methods(monkeypatch, tmp_path):
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    bridge = module.AKExecutionBridge(native=fake)
    assert bridge.submit_enabled is False and bridge.cancel_owned_enabled is False
    assert fake.calls == [] and fake.attribute_access == []


def test_h01_public_bypass_entries_reject_even_armed(monkeypatch, tmp_path):
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    bridge = armed_bridge(module, fake, tmp_path)
    try:
        for method, args in ((bridge.send_order, ({"Volume": 1},)),
                             (bridge.cancel_order, (7,)),
                             (bridge.submit_once, ("i", "l", "p", None)),
                             (bridge.request_cancel, ("c", "i", "l", None))):
            with pytest.raises(Exception, match="MODE_DISABLED"):
                method(*args)
        assert fake.send_calls() == []
    finally:
        stop_started_bridge(bridge)


def test_h01_bare_request_cannot_bypass_via_pump(monkeypatch, tmp_path):
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    bridge = armed_bridge(module, fake, tmp_path)
    try:
        dispatch = armed_dispatch(bridge)
        bridge._receive_dispatch(dispatch)
        bridge._pump_once()
        assert fake.send_calls() != []  # valid path works (baseline for negatives)
        # a second, foreign-bare command with no permit is refused
        foreign = armed_dispatch(bridge, command_id="SIM_CMD_2")
        foreign["permit_id"] = ""
        bridge._receive_dispatch(foreign)
        before = len(fake.send_calls())
        bridge._pump_once()
        assert len(fake.send_calls()) == before
    finally:
        stop_started_bridge(bridge)


def test_h05_foreign_binding_scope_zero_calls(monkeypatch, tmp_path):
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    bridge = armed_bridge(module, fake, tmp_path)
    try:
        for index, overrides in enumerate(({"instance_id": "OTHER_INSTANCE"},
                                           {"session_epoch": "OTHER_EPOCH"},
                                           {"exchange": "DCE"}, {"instrument_id": "m2701"})):
            dispatch = armed_dispatch(bridge, command_id=f"SIM_CMD_F_{index}",
                                      binding_overrides=overrides)
            bridge._receive_dispatch(dispatch)
        bridge._pump_once()
        assert fake.send_calls() == []
        assert bridge.counters["native_send_attempts"] == 0
    finally:
        stop_started_bridge(bridge)


def test_h06_blank_restore_recovers_connection_not_authorization(monkeypatch, tmp_path):
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    bridge = module.AKExecutionBridge(native=fake, store_directory=None)
    bridge.set_params({"exchange": "SHFE", "instrument_id": "rb2610",
                       "execution_api_url": "http://127.0.0.1:17779/execution/v1/poll",
                       "enable_trading": True, "parameter_confirmation": "SIM_CHALLENGE",
                       "authorization_id": "SIM_AUTH"})
    # saved-true was forced false; authorization fields not restored by blanks
    assert bridge.params_map.enable_trading is False
    bridge.set_params({"exchange": "", "instrument_id": "", "execution_api_url": ""})
    assert bridge.params_map.exchange == "SHFE"
    assert bridge.params_map.instrument_id == "rb2610"
    assert bridge.params_map.execution_api_url.startswith("http://127.0.0.1:17779")
    with pytest.raises(ValueError):
        bridge.set_params({"execution_api_url": "https://example.invalid/execution/v1/poll"})
    assert bridge.params_map.execution_api_url.startswith("http://127.0.0.1:17779")


def test_h06_volume_alias_disagreement_rejected(monkeypatch, tmp_path):
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    bridge = module.AKExecutionBridge(native=fake, store_directory=None)
    with pytest.raises(ValueError):
        bridge.set_params({"volume": 2, "order_volume": 3})


def test_h07_freshness_gates_and_expiry(monkeypatch, tmp_path):
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    bridge = armed_bridge(module, fake, tmp_path)
    try:
        fresh = armed_dispatch(bridge, ttl_ms=1500)
        bridge._receive_dispatch(fresh)
        bridge._pump_once()
        assert len(fake.send_calls()) == 1

        stale = armed_dispatch(bridge, command_id="SIM_CMD_STALE", ttl_ms=100)
        bridge._receive_dispatch(stale)
        time.sleep(0.25)  # exceed the 100 ms window; pump after expiry
        bridge._pump_once()
        assert len(fake.send_calls()) == 1  # expired ticket: zero calls, NO_CALL receipt

        aged_quote = armed_dispatch(bridge, command_id="SIM_CMD_AGE", ttl_ms=1500)
        bridge._latest_capture["quote_at"] = datetime.now(timezone.utc) - timedelta(seconds=4)
        bridge._receive_dispatch(aged_quote)
        bridge._pump_once()
        assert len(fake.send_calls()) == 1  # stale quote blocks
    finally:
        stop_started_bridge(bridge)


def test_h24_cancel_admission_independent_of_quote(monkeypatch, tmp_path):
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    bridge = armed_bridge(module, fake, tmp_path)
    try:
        bridge._latest_capture["quote_at"] = datetime.now(timezone.utc) - timedelta(seconds=30)
        cancel = armed_dispatch(bridge, kind="CANCEL", command_id="SIM_CMD_CX",
                                order_id=42, ttl_ms=1500)
        bridge._receive_dispatch(cancel)
        bridge._pump_once()
        assert len([c for c in fake.calls if c[0] == "cancelOrder"]) == 1  # stale quote does not block owned cancel
        # a revoked round still never cancels even with fresh data
        cancel2 = armed_dispatch(bridge, kind="CANCEL", command_id="SIM_CMD_CX2",
                                 order_id=43, ttl_ms=1500)
        bridge._revoke_arming("synthetic_user_disarm")
        assert bridge._arming_effective_now() is False
        set_synthetic_gate_capture(bridge, quote=True)
        bridge._receive_dispatch(cancel2)
        calls = [c for c in fake.calls if c[0] == "cancelOrder"]
        bridge._pump_once()
        assert [c for c in fake.calls if c[0] == "cancelOrder"] == calls
    finally:
        stop_started_bridge(bridge)


def test_h34_production_path_rejects_synthetic_shapes(monkeypatch, tmp_path):
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    bridge = armed_bridge(module, fake, tmp_path)
    try:
        dispatch = armed_dispatch(bridge)
        # a command claiming no permit (synthetic/self-reported capability) is refused
        dispatch["permit_id"] = ""
        bridge._receive_dispatch(dispatch)
        bridge._pump_once()
        assert fake.send_calls() == []
    finally:
        stop_started_bridge(bridge)


def test_golden_vectors_mapping_mirror(monkeypatch, tmp_path):
    module, _ = load_client_from_temp(monkeypatch, tmp_path)
    binding = synthetic_binding()
    leg = {"leg_id": "SIM_LEG_1", "direction": "BUY", "offset": "OPEN",
           "volume": 1, "price": "3500"}
    request = module.build_client_request(leg, binding, "SYNTHETIC-HANDLE",
                                          strategy_id=23, order_type="GFD")
    assert tuple(request) == module.NATIVE_REQUEST_KEYS
    assert request["Direction"] == "0" and request["Offset"] == "0"
    assert request["Market"] is False and request["OrderType"] == "0"
    assert request["Memo"] == "SIM_LEG_1" and request["Price"] == 3500.0
    close_yd = {**leg, "direction": "SELL", "offset": "CLOSE_YESTERDAY"}
    request2 = module.build_client_request(close_yd, binding, "h",
                                            strategy_id=23, order_type="FAK")
    assert request2["Direction"] == "1" and request2["Offset"] == "1"
    assert request2["OrderType"] == "1"
    dce = {**binding, "exchange": "DCE"}
    with pytest.raises(module.ContractError):
        module.build_client_request(close_yd, dce, "h", strategy_id=23, order_type="GFD")
    with pytest.raises(module.ContractError):
        module.build_client_request({**leg, "price": "3500.1", "price_tick": "1"}, binding,
                                    "h", strategy_id=23, order_type="GFD")
    with pytest.raises(module.ContractError):
        module.build_client_request({**leg, "order_type_extra": True}, binding, "h",
                                    strategy_id=23, order_type="FOK")


# ---------------------------------------------------------------------------
# C1.2 dispatch TTL window: DTTL-01/02/03 client consumption semantics
# (NE1-ARB-C1.2-OVF06-001; client-c1.2-ttl-prep.md test plan rows 1-4)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ttl_ms,accepted", [
    (500, True),          # old 500 ms ticket remains valid under the 1500 cap
    (500.001, True),      # the 500-era boundary no longer rejects
    (1499.999, True),
    (1500, True),         # exactly the frozen cap is valid
    (1500.001, False),    # above the cap cannot even be accepted
])
def test_dttl01_mirror_encoded_lifetime_boundaries(monkeypatch, tmp_path, ttl_ms, accepted):
    module, _ = load_client_from_temp(monkeypatch, tmp_path)
    assert module.DISPATCH_TTL_MS == 1500  # DTTL-01: frozen mirror, no override
    command = synthetic_dispatch(ttl_ms=ttl_ms)
    if accepted:
        assert module.validate_dispatch_command(command)["command_id"] == "SIM_CMD_1"
    else:
        with pytest.raises(module.ContractError):
            module.validate_dispatch_command(command)


def test_dttl01_now_equals_expires_is_expired(monkeypatch, tmp_path):
    """DTTL-01: issued <= now < expires with equality-at-expiry expired."""
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    bridge = armed_bridge(module, fake, tmp_path)
    try:
        frozen = datetime.now(timezone.utc)
        monkeypatch.setattr(module, "_now_utc", lambda: frozen)
        command = armed_dispatch(bridge, command_id="SIM_CMD_EQ",
                                 issued=frozen - timedelta(milliseconds=400), ttl_ms=400)
        # expires == frozen == now exactly
        assert command["expires_at"] == (frozen + timedelta(milliseconds=0)) \
            .isoformat(timespec="microseconds").replace("+00:00", "Z")
        bridge._receive_dispatch(command)
        bridge._pump_once()
        assert fake.send_calls() == []
        rows = bridge._stores.receipt_for_command(command["command_id"])
        assert [row[0] for row in rows] == ["NO_CALL"]  # zero call + NO_CALL receipt
    finally:
        stop_started_bridge(bridge)


def test_dttl02_command_outliving_offer_is_quarantined(monkeypatch, tmp_path):
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    bridge = armed_bridge(module, fake, tmp_path)
    try:
        offer = synthetic_offer(binding=synthetic_binding(
            instance_id=bridge._instance_id, session_epoch=bridge._session_epoch),
            params_sha256=bridge._effective_params_sha256, offer_id="SIM_OFFER_WIN",
            expires_in_ms=300)
        bridge._remember_offer_deadline(offer)
        # the command's own window (400 ms) extends past the offer deadline (300 ms)
        command = armed_dispatch(bridge, command_id="SIM_CMD_WIN", ttl_ms=400)
        command["offer_id"] = "SIM_OFFER_WIN"
        bridge._receive_dispatch(command)  # quarantined at receipt: never enters the slot
        assert bridge._dispatch_slot is None
        assert fake.send_calls() == []
        assert bridge.counters["dispatch_window_conflicts"] == 1
        assert bridge._window_conflict_latched is True
        rows = bridge._stores.receipt_for_command(command["command_id"])
        assert [row[0] for row in rows] == ["NO_CALL"]
        # the latch also blocks a later, perfectly valid dispatch (zero call)
        valid = armed_dispatch(bridge, command_id="SIM_CMD_OK", ttl_ms=400)
        bridge._receive_dispatch(valid)
        bridge._pump_once()
        assert fake.send_calls() == []
    finally:
        stop_started_bridge(bridge)


def test_dttl02_offer_deadline_first_value_immutable(monkeypatch, tmp_path):
    """Repeated poll/ready with the same offer id never moves the recorded
    deadline; a mutated expiry is refused, not adopted."""
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    bridge = armed_bridge(module, fake, tmp_path)
    try:
        binding = synthetic_binding(instance_id=bridge._instance_id,
                                    session_epoch=bridge._session_epoch)
        first = synthetic_offer(binding=binding, params_sha256=bridge._effective_params_sha256,
                                offer_id="SIM_OFFER_FIX", expires_in_ms=900)
        bridge._remember_offer_deadline(first)
        bridge._remember_offer_deadline(dict(first))            # identical: no-op
        assert bridge._offer_deadlines["SIM_OFFER_FIX"] == first["expires_at"]
        extended = synthetic_offer(binding=binding, params_sha256=bridge._effective_params_sha256,
                                   offer_id="SIM_OFFER_FIX", expires_in_ms=1500)
        bridge._remember_offer_deadline(extended)               # later expiry refused
        assert bridge._offer_deadlines["SIM_OFFER_FIX"] == first["expires_at"]  # first kept
        assert bridge._window_conflict_latched is True
        assert bridge.counters["dispatch_window_conflicts"] == 1
    finally:
        stop_started_bridge(bridge)


# ---------------------------------------------------------------------------
# C1.2 joint-integration cross-layer fix: 429 POLL_TOO_FAST poll backoff
# (TransportErrorResponse.retry_after_ms; zero polls during the window)
# ---------------------------------------------------------------------------

def _error_body(module, retry_after_ms, *, error_code="POLL_TOO_FAST", http_status=429):
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    return module.canonical_dumps({
        "schema_version": module.TRANSPORT_SCHEMA_VERSION,
        "type": "TransportErrorResponse",
        "data": {"request_id": "req" + "0" * 29, "instance_id": None, "session_epoch": None,
                 "generation": None, "sequence": None, "params_sha256": None,
                 "service_epoch": 1, "request_service_epoch": None,
                 "sent_at": now, "received_at": now, "binding": None,
                 "ledger_revision": None, "control_revision": None,
                 "error_code": error_code, "http_status": http_status,
                 "retry_after_ms": retry_after_ms}}).encode()


def _idle_body(module, packet):
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    return module.canonical_dumps({
        "schema_version": module.TRANSPORT_SCHEMA_VERSION,
        "type": "PollResponse",
        "data": {**{key: packet["data"][key] for key in
                    ("request_id", "instance_id", "session_epoch", "generation",
                     "service_epoch", "sequence", "sent_at", "params_sha256")},
                 "received_at": now, "ledger_revision": 1, "control_revision": 1,
                 "kind": "IDLE", "offer": None, "blockers": []}}).encode()


def test_c12_429_poll_too_fast_backs_off_and_recovers(monkeypatch, tmp_path):
    """429 -> back off exactly retry_after_ms -> zero polls while the window
    is open -> polling resumes at expiry (no storm, no latch)."""
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    calls = {"n": 0}

    def transport(url, payload, timeout, headers=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return 429, _error_body(module, 2000)
        return 200, _idle_body(module, payload)

    bridge = module.AKExecutionBridge(native=fake, transport=transport,
                                      store_directory=tmp_path / "store",
                                      pairing_token="ab" * 32)
    bridge.strategy_id = 23
    bridge.set_params({"exchange": "SHFE", "instrument_id": "rb2610", "hedge_flag": "1"})
    bridge.on_init()
    bridge.on_start()

    try:
        bridge._control_cycle()  # first poll throttled
        assert calls["n"] == 1
        assert bridge.counters["poll_backoffs"] == 1
        assert bridge.counters["poll_requests"] == 0  # the throttled poll never counted
        assert bridge.state_map.control_status == "poll_backoff"
        window = bridge._poll_backoff_until - time.monotonic()
        assert 1.5 < window <= 2.0  # exactly the contract's retry_after_ms

        bridge._control_cycle()  # inside the window: zero requests
        bridge._control_cycle()
        assert calls["n"] == 1  # no poll was sent during the backoff window
        assert bridge._sequence == 1  # no envelope/sequence burned while backing off

        bridge._poll_backoff_until = time.monotonic() - 0.001  # window expires
        bridge._control_cycle()
        assert calls["n"] == 2  # polling resumed at expiry
        assert bridge.counters["poll_requests"] == 1
        assert bridge.state_map.control_status == "ok"
        assert not bridge._in_poll_backoff()
        bridge.on_stop()
    finally:
        stop_started_bridge(bridge)


def test_c12_429_without_contract_body_still_backs_off_conservatively(monkeypatch, tmp_path):
    """A 429 with an undecodable body must not fall back to storming; other
    error codes keep the plain hard-error semantics with no backoff."""
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    bridge = module.AKExecutionBridge(native=fake,
                                      transport=lambda *a: (429, b"not-json"),
                                      store_directory=tmp_path / "store",
                                      pairing_token="ab" * 32)
    bridge.strategy_id = 23
    bridge.set_params({"exchange": "SHFE", "instrument_id": "rb2610", "hedge_flag": "1"})
    bridge.on_init()
    bridge.on_start()
    try:
        bridge._control_cycle()
        assert bridge.counters["poll_backoffs"] == 1
        assert bridge._poll_backoff_until - time.monotonic() > 4.0  # conservative default
        assert bridge._control_cycle() is None  # suppressed, zero requests
        bridge.on_stop()
    finally:
        stop_started_bridge(bridge)

    # a non-429 failure stays a hard error: no backoff state is entered
    other = module.AKExecutionBridge(native=fake,
                                     transport=lambda *a: (500, b"{}"),
                                     store_directory=tmp_path / "store2",
                                     pairing_token="ab" * 32)
    other.strategy_id = 23
    other.set_params({"exchange": "SHFE", "instrument_id": "rb2610", "hedge_flag": "1"})
    other.on_init()
    other.on_start()
    try:
        packet = other._envelope({"snapshot": None, "snapshot_id": None,
                                  "client_state_digest": "ab" * 32,
                                  "ack_watermark": None}, "PollRequest")
        with pytest.raises(module.ContractError, match="http_500"):
            other._post("/execution/v1/poll", packet)
        assert other.counters["poll_backoffs"] == 0
        assert other._poll_backoff_until == 0.0
        assert not other._in_poll_backoff()
        other.on_stop()
    finally:
        stop_started_bridge(other)


# ---------------------------------------------------------------------------
# C1.2 joint-gate defect probe: CONSUMED receipts must survive the REAL
# /results validator (the frozen engine DispatchReceipt decode); a CONSUMED
# record written before the native outcome carries the N4 build_receipt
# OUTCOME_PENDING marker, never result_kind=None.
# ---------------------------------------------------------------------------

def test_c12_consumed_receipt_roundtrips_real_validator(monkeypatch, tmp_path):
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    bridge = armed_bridge(module, fake, tmp_path)
    try:
        dispatch = armed_dispatch(bridge, command_id="SIM_CMD_RCPT", ttl_ms=1500)
        bridge._receive_dispatch(dispatch)
        bridge._pump_once()
        assert len(fake.send_calls()) == 1
        rows = bridge._stores.receipts.execute(
            "SELECT payload FROM receipts WHERE command_id=? ORDER BY local_sequence",
            (dispatch["command_id"],)).fetchall()
        records = [json.loads(row[0]) for row in rows]
        assert [record["stage"] for record in records] == ["CONSUMED", "RETURNED"]
        from ak_trading.infiny_execution import native_contracts as nc
        decoded = [nc.decode(json.dumps({
            "schema_version": nc.TRANSPORT_SCHEMA_VERSION,
            "type": "DispatchReceipt", "data": record}), nc.DispatchReceipt)
            for record in records]
        consumed, returned = decoded
        assert consumed.stage is nc.ReceiptStage.CONSUMED
        assert consumed.result_kind is nc.ResultKind.EXCEPTION  # OUTCOME_PENDING marker
        assert consumed.error_code == "OUTCOME_PENDING"
        assert consumed.result_value is None
        assert returned.stage is nc.ReceiptStage.RETURNED
        assert returned.result_kind is nc.ResultKind.LOCAL_ACK
        assert returned.result_value >= 0
    finally:
        stop_started_bridge(bridge)


# ---------------------------------------------------------------------------
# NE1-LM-R4 M06 (decision NE1-ARB-PAIRING-LASTMILE-001 §5 M06 + RULING 02/03
# client side): token success, service recovery and params-hash changes never
# restore trading permission; old sessions/credentials/dispatches never enter
# native. All tokens SYNTHETIC; every transport here is an injected double.
# ---------------------------------------------------------------------------

SYNTHETIC_PAIRING_TOKEN = "cd" * 32
SYNTHETIC_SERVICE_EPOCH = 5
# X01-01 (NE1-ARB-LM-X01-001): the ONLY legal service-instance-id syntax is
# 'execution-' + exactly 16 lowercase hex; this is a SYNTHETIC test vector.
SYNTHETIC_SERVICE_INSTANCE_ID = "execution-0123456789abcdef"
CLIENT_PAIRING_SCHEMA = "ne1_client_pairing_v1"
SLOT_FIELDS = {"schema_version", "request_id", "request_sha256", "instance_id",
               "session_epoch", "generation", "deployment_key", "strategy_id",
               "params_sha256", "client_file_sha256", "execution_origin",
               "service_instance_id", "service_epoch", "contract_lock_sha256",
               "token"}


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


def _apply_protected_dacl(path, *, extra_trustee_sid=None):
    """Test-side mirror of the handoff tool's discipline: a PROTECTED DACL
    granting only the current user (plus optionally one extra trustee, used
    to build the deliberately-loose negative)."""
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
    trustees = [_current_user_sid_text()]
    if extra_trustee_sid:
        trustees.append(extra_trustee_sid)
    sids = []
    try:
        for text in trustees:
            pointer = ctypes.c_void_p()
            assert adv.ConvertStringSidToSidW(text, ctypes.byref(pointer))
            sids.append(pointer)
        total = 16 + sum(8 + adv.GetLengthSid(pointer) for pointer in sids)
        buffer = ctypes.create_string_buffer(total)
        assert adv.InitializeAcl(buffer, total, 2)  # ACL_REVISION
        for pointer in sids:
            assert adv.AddAccessAllowedAce(buffer, 2, 0x1F01FF, pointer)
        # DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION
        result = adv.SetNamedSecurityInfoW(str(path), 1, 0x4 | 0x80000000,
                                           None, None, buffer, None)
        assert result == 0, f"SetNamedSecurityInfoW failed: {result}"
    finally:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.LocalFree.argtypes = [wintypes.HANDLE]
        for pointer in sids:
            kernel32.LocalFree(pointer)


def _install_pairing_slot(store, token=SYNTHETIC_PAIRING_TOKEN, *,
                          service_epoch=SYNTHETIC_SERVICE_EPOCH,
                          service_instance_id=None,
                          contract_lock_sha256="ce" * 32,
                          extra_trustee_sid=None, mutate=None):
    """USER-role installation of the SYNTHETIC credential slot with the
    tool's own discipline (transfer-serialized write, protected DACL, atomic
    replace). Returns the slot record."""
    store = Path(store)
    request_raw = (store / "pairing-request.json").read_bytes()
    request = json.loads(request_raw.decode("utf-8"))
    origin_port = urlsplit(request["execution_api_url"]).port
    record = {
        "schema_version": CLIENT_PAIRING_SCHEMA,
        "request_id": request["request_id"],
        "request_sha256": hashlib.sha256(request_raw).hexdigest(),
        "instance_id": request["instance_id"],
        "session_epoch": request["session_epoch"],
        "generation": request["generation"],
        "deployment_key": request["deployment_key"],
        "strategy_id": request["strategy_id"],
        "params_sha256": request["params_sha256"],
        "client_file_sha256": request["client_file_sha256"],
        "execution_origin": f"http://127.0.0.1:{origin_port}",
        # X01-01: default is the SYNTHETIC legal 'execution-<16hex>' label;
        # an explicitly passed value (even a falsy negative) is honored.
        "service_instance_id": (SYNTHETIC_SERVICE_INSTANCE_ID
                                if service_instance_id is None
                                else service_instance_id),
        "service_epoch": service_epoch,
        "contract_lock_sha256": contract_lock_sha256,
        "token": token,
    }
    if mutate is not None:
        record = mutate(record)
    payload = json.dumps(record, sort_keys=True).encode("utf-8")
    temporary = store / ".pairing.json.probe-tmp"
    temporary.write_bytes(payload)
    _apply_protected_dacl(temporary, extra_trustee_sid=extra_trustee_sid)
    os.replace(temporary, store / "pairing.json")
    return record


def _epoch_idle_body(module, packet, service_epoch):
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    return module.canonical_dumps({
        "schema_version": module.TRANSPORT_SCHEMA_VERSION,
        "type": "PollResponse",
        "data": {**{key: packet["data"][key] for key in
                    ("request_id", "instance_id", "session_epoch", "generation",
                     "sequence", "sent_at", "params_sha256")},
                 "service_epoch": service_epoch, "received_at": now,
                 "ledger_revision": 1, "control_revision": 1, "kind": "IDLE",
                 "offer": None, "blockers": []}}).encode()


def _waiting_bridge(module, fake, tmp_path, transport):
    """Unpaired explicit-store client (no token injection): valid start into
    WAIT_PAIRING with an injected transport double."""
    bridge = module.AKExecutionBridge(native=fake, transport=transport,
                                      store_directory=tmp_path / "store")
    bridge.strategy_id = 23
    bridge.set_params({"exchange": "SHFE", "instrument_id": "rb2610", "hedge_flag": "1"})
    bridge.on_init()
    bridge.on_start()
    assert bridge._pairing_state == "WAIT_PAIRING"
    assert bridge.state_map.execution_status == "wait_pairing"
    return bridge


def test_m06_activation_success_keeps_trading_disabled_and_token_private(monkeypatch, tmp_path):
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    capture = []

    def transport(url, payload, timeout, headers=None):
        capture.append({"url": url, "payload": payload, "headers": dict(headers or {})})
        return 200, _epoch_idle_body(module, payload, SYNTHETIC_SERVICE_EPOCH)

    bridge = _waiting_bridge(module, fake, tmp_path, transport)
    try:
        assert capture == []  # WAIT_PAIRING: zero HTTP before any credential
        _install_pairing_slot(bridge._store_path)
        assert bridge._try_activate_pairing() is True
        assert bridge._pairing_state == "PAIRED_DISABLED"
        assert bridge.state_map.execution_status == "paired_disabled"
        assert bridge._service_epoch == SYNTHETIC_SERVICE_EPOCH
        assert bridge.counters["pairing_activations"] == 1
        # M06 core: the token granted communication, never trading permission
        assert bridge.params_map.enable_trading is False
        assert bridge.counters["native_send_attempts"] == 0
        assert bridge.counters["native_cancel_attempts"] == 0
        assert fake.send_calls() == [] and fake.cancel_calls() == []
        assert fake.attribute_access == []
        # the single null-epoch handshake poll shape + authenticated header
        first = capture[0]
        assert first["payload"]["type"] == "PollRequest"
        assert first["payload"]["data"]["service_epoch"] is None
        assert first["payload"]["data"]["snapshot"] is None
        assert first["payload"]["data"]["snapshot_id"] is None
        assert first["payload"]["data"]["ack_watermark"] is None
        assert first["headers"].get("X-Execution-Token") == SYNTHETIC_PAIRING_TOKEN
        # token hygiene: never in params, UI params, logs or store meta
        dumps = json.dumps(bridge.get_params()) + json.dumps(bridge.params_map.model_dump())
        dumps += json.dumps(bridge._pairing_request_record or {})
        assert SYNTHETIC_PAIRING_TOKEN not in dumps
        logs = [json.dumps({"args": call["args"], "kwargs": call["kwargs"]}, ensure_ascii=False)
                for name, call, _ in fake.calls if name == "writeLog"]
        assert all(SYNTHETIC_PAIRING_TOKEN not in entry for entry in logs)
        assert bridge._stores.read_meta("pairing_token") is None
        bridge.on_stop()
    finally:
        stop_started_bridge(bridge)


def test_m06_service_epoch_mismatch_requires_restart_without_storm(monkeypatch, tmp_path):
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    capture = []

    def transport(url, payload, timeout, headers=None):
        capture.append({"payload": payload, "headers": dict(headers or {})})
        # the live service runs a DIFFERENT epoch than the credential slot
        return 200, _epoch_idle_body(module, payload, SYNTHETIC_SERVICE_EPOCH + 1)

    bridge = _waiting_bridge(module, fake, tmp_path, transport)
    try:
        _install_pairing_slot(bridge._store_path)
        assert bridge._try_activate_pairing() is False
        assert bridge._pairing_state == "RESTART_REQUIRED"
        assert bridge._last_pairing_reason == "PAIRING_SERVICE_MISMATCH"
        assert bridge._pairing_token is None  # candidate never promoted
        storm = len(capture)
        for _ in range(3):
            bridge._control_cycle()
        assert len(capture) == storm  # no invalid-request storm from the dead pairing
        assert bridge.params_map.enable_trading is False
        assert bridge.counters["native_send_attempts"] == 0
        bridge.on_stop()
    finally:
        stop_started_bridge(bridge)


def test_m06_service_recovery_after_auth_rejection_stays_disabled(monkeypatch, tmp_path):
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    calls = {"n": 0}

    def transport(url, payload, timeout, headers=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return 401, b"{}"  # the token is dead (service rotated it)
        return 200, _epoch_idle_body(module, payload, SYNTHETIC_SERVICE_EPOCH)

    bridge = _waiting_bridge(module, fake, tmp_path, transport)
    try:
        _install_pairing_slot(bridge._store_path)
        assert bridge._try_activate_pairing() is False
        assert bridge._pairing_state == "RESTART_REQUIRED"
        assert bridge._last_pairing_reason == "service_auth_rejected"
        # the service "recovers" on later calls: no automatic re-auth, no storm
        for _ in range(3):
            bridge._control_cycle()
        assert calls["n"] == 1
        assert bridge._try_activate_pairing() is False  # RESTART_REQUIRED is terminal
        assert bridge.params_map.enable_trading is False
        assert fake.send_calls() == []
        bridge.on_stop()
    finally:
        stop_started_bridge(bridge)


def test_m06_stale_credential_after_restart_refused_zero_http(monkeypatch, tmp_path):
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    capture = []

    def transport(url, payload, timeout, headers=None):
        capture.append({"payload": payload, "headers": dict(headers or {})})
        return 200, _epoch_idle_body(module, payload, SYNTHETIC_SERVICE_EPOCH)

    bridge = _waiting_bridge(module, fake, tmp_path, transport)
    try:
        store_path = Path(bridge._store_path)
        _install_pairing_slot(store_path)  # credential for request R1
        assert bridge._try_activate_pairing() is True
        bridge.on_stop()  # request R1 inactive; session gone
        assert json.loads((store_path / "pairing-request.json")
                          .read_text(encoding="utf-8"))["active"] is False

        used = len(capture)
        bridge.on_start()  # same object, new session, NEW request R2
        assert bridge._pairing_state == "WAIT_PAIRING"
        assert bridge._session_epoch is not None
        # the OLD pairing.json cannot activate against the new request context
        assert bridge._try_activate_pairing() is False
        assert bridge._last_pairing_reason == "PAIRING_CONTEXT_MISMATCH"
        assert len(capture) == used  # zero anonymous HTTP while refused
        assert bridge._pairing_token is None
        assert bridge.state_map.execution_status == "wait_pairing"
        # a credential for the NEW request activates and STILL grants no trading
        _install_pairing_slot(store_path)
        assert bridge._try_activate_pairing() is True
        assert bridge.params_map.enable_trading is False
        assert fake.send_calls() == []
        bridge.on_stop()
    finally:
        stop_started_bridge(bridge)


def test_m06_params_change_while_waiting_requires_restart(monkeypatch, tmp_path):
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    transport = lambda *a, **k: (200, b"{}")  # never reached while unpaired
    bridge = _waiting_bridge(module, fake, tmp_path, transport)
    try:
        first_hash = bridge._params_sha256
        bridge.set_params({"order_volume": 2})  # a params_sha256-affecting change
        assert bridge._pairing_state == "RESTART_REQUIRED"
        assert bridge._last_pairing_reason == "params_changed"
        request = json.loads((Path(bridge._store_path) / "pairing-request.json")
                             .read_text(encoding="utf-8"))
        assert request["active"] is False  # old description voided
        bridge._control_cycle()  # no anonymous HTTP from a restart-required life
        # the R3.2 recovery: a fresh stop/start republishes an active request
        # with the new parameter hash (RESTART_REQUIRED never re-arms in place)
        bridge.on_stop()
        bridge.on_start()
        assert bridge._pairing_state == "WAIT_PAIRING"
        request2 = json.loads((Path(bridge._store_path) / "pairing-request.json")
                              .read_text(encoding="utf-8"))
        assert request2["active"] is True
        assert request2["params_sha256"] == bridge._params_sha256 != first_hash
        assert request2["request_id"] != request["request_id"]
        assert bridge.params_map.enable_trading is False
        bridge.on_stop()
    finally:
        stop_started_bridge(bridge)


def test_m06_params_hash_stable_across_lives_and_sensitive_to_version(monkeypatch, tmp_path):
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    transport = lambda *a, **k: (200, b"{}")

    def hash_for(store_name):
        bridge = module.AKExecutionBridge(native=fake, transport=transport,
                                          store_directory=tmp_path / store_name,
                                          pairing_token="ab" * 32)
        bridge.strategy_id = 23
        bridge.set_params({"exchange": "SHFE", "instrument_id": "rb2610",
                           "hedge_flag": "1", "order_volume": 1})
        bridge.on_init()
        try:
            bridge.on_start()
            value = bridge._params_sha256
            assert bridge._pairing_state == "PAIRED_DISABLED"  # token seam changes nothing
            bridge.on_stop()
        finally:
            stop_started_bridge(bridge)
        return value

    first = hash_for("s1")
    second = hash_for("s2")  # different store, different instance/session/token life
    assert first == second  # same adapter version + same params -> same hash
    monkeypatch.setattr(module, "ADAPTER_VERSION", "ne1-execution-PROBE-VERSION")
    assert hash_for("s3") != first  # a version bump legitimately voids old hashes
    monkeypatch.undo()
    assert module.ADAPTER_VERSION == "ne1-execution-20260918-arming-v1-snapshot-20260930-r1"


@pytest.fixture
def controlled_dispatch_clock(monkeypatch):
    """SYNTHETIC UTC + monotonic input, independent of pairing/ACL latency.

    Patch only the isolated client's time reference, never the shared time
    module used by pytest/thread joins. All local arming documents use the
    same UTC source; no production TTL, gate or identity is overridden.
    """
    clock = ArmingClock()
    monkeypatch.setitem(_utcnow_iso.__globals__, "_utcnow_iso",
                        lambda value=None: (clock.now() if value is None else value)
                        .isoformat(timespec="microseconds").replace("+00:00", "Z"))

    def install(module):
        monkeypatch.setattr(module, "_now_utc", clock.now)
        monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=clock.monotonic))
        return clock

    return install


def _dispatch_receipt_bodies(bridge, command):
    """Read only this SYNTHETIC command's persisted receipt evidence."""
    with bridge._stores._lock:
        rows = bridge._stores.receipts.execute(
            "SELECT payload FROM receipts WHERE command_id=? ORDER BY local_sequence",
            (command["command_id"],)).fetchall()
    return [json.loads(row[0]) for row in rows]


def test_m06_old_session_dispatch_blocked_after_persistent_restart(
        monkeypatch, tmp_path, controlled_dispatch_clock):
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    clock = controlled_dispatch_clock(module)
    assert module.DISPATCH_TTL_MS == 1500
    bridge_a = armed_bridge(module, fake, tmp_path)
    try:
        old_command = armed_dispatch(bridge_a, command_id="SIM_CMD_OLD_SESSION",
                                     ttl_ms=1500, issued=clock.now())
        a_target_hash = bridge_a._effective_params_sha256
        a_session, a_generation = bridge_a._session_epoch, bridge_a._generation
        assert bridge_a._gate_blockers(old_command, clock.now()) == []
        assert bridge_a._stores.receipt_for_command(old_command["command_id"]) == []
        bridge_a.on_stop()  # session A ends before any consumption
    finally:
        stop_started_bridge(bridge_a)

    bridge_b = armed_bridge(module, fake, tmp_path)
    try:
        assert bridge_b._instance_id == bridge_a._instance_id  # same persistent identity
        assert bridge_b._session_epoch != a_session
        assert bridge_b._generation != a_generation
        # The same target hash legitimately repeats across lives; the old
        # binding alone must reject this still-live ticket. W3 F-01: do not
        # substitute the legal TTL NO_CALL branch for this empty-receipt check.
        assert bridge_b._effective_params_sha256 == a_target_hash
        assert module._parse_iso(old_command["issued_at"]) <= clock.now() \
            < module._parse_iso(old_command["expires_at"])
        assert bridge_b._gate_blockers(old_command, clock.now()) == ["BINDING_EPOCH_MISMATCH"]
        blocked_before = bridge_b.counters["native_call_blocked"]
        bridge_b._receive_dispatch(dict(old_command))
        bridge_b._pump_once()
        assert fake.send_calls() == [] and fake.cancel_calls() == []
        assert bridge_b.counters["native_send_attempts"] == 0
        assert bridge_b.counters["native_cancel_attempts"] == 0
        assert bridge_b.counters["native_call_blocked"] == blocked_before + 1
        assert bridge_b._stores.receipt_for_command(old_command["command_id"]) == []
        bridge_b.on_stop()
    finally:
        stop_started_bridge(bridge_b)


def test_m06_generation_mismatch_before_native_has_terminal_no_call(
        monkeypatch, tmp_path, controlled_dispatch_clock):
    """An unexpired admitted ticket loses its generation before the final fence."""
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    clock = controlled_dispatch_clock(module)
    assert module.DISPATCH_TTL_MS == 1500
    bridge = armed_bridge(module, fake, tmp_path)
    try:
        command = armed_dispatch(bridge, command_id="SIM_CMD_GENERATION_FENCE",
                                 ttl_ms=1500, issued=clock.now())
        assert bridge._gate_blockers(command, clock.now()) == []
        bridge._receive_dispatch(command)
        admitted_generation = bridge._dispatch_slot_epoch
        original_record = bridge._record_receipt
        observed_fence = []

        def record_and_roll_generation(cmd, stage, *args, **kwargs):
            record = original_record(cmd, stage, *args, **kwargs)
            if stage == "CONSUMED":
                # Fault injection after the real durable write; the receipt
                # and the installed ArmContext are never fabricated.
                bridge._generation = "0" * 32 if admitted_generation != "0" * 32 else "1" * 32
                observed_fence.extend(bridge._post_fsync_fence(cmd))
            return record

        monkeypatch.setattr(bridge, "_record_receipt", record_and_roll_generation)
        bridge._pump_once()
        assert bridge._generation != admitted_generation
        assert observed_fence == ["GENERATION_EPOCH_CHANGED"]
        assert clock.now() < module._parse_iso(command["expires_at"])
        assert bridge._stores.receipt_for_command(command["command_id"]) == [
            ("CONSUMED", command["attempt_id"]), ("NO_CALL", command["attempt_id"])]
        receipts = _dispatch_receipt_bodies(bridge, command)
        assert [row["error_code"] for row in receipts] == [
            "OUTCOME_PENDING", "terminal_fence_no_call"]
        assert command["command_id"] in bridge._terminal_no_call
        assert bridge._dispatch_slot is None
        assert fake.send_calls() == [] and fake.cancel_calls() == []
        assert bridge.counters["native_send_attempts"] == 0
        assert bridge.counters["native_cancel_attempts"] == 0
    finally:
        stop_started_bridge(bridge)


@pytest.mark.parametrize("elapsed_ms", [1500, 1501], ids=["at-expiry", "after-expiry"])
def test_m06_dispatch_ttl_expiry_has_exact_no_call(
        monkeypatch, tmp_path, controlled_dispatch_clock, elapsed_ms):
    """A matching ticket expires by real gate semantics, with an exact NO_CALL."""
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    clock = controlled_dispatch_clock(module)
    assert module.DISPATCH_TTL_MS == 1500
    bridge = armed_bridge(module, fake, tmp_path)
    try:
        issued = clock.now()
        command = armed_dispatch(bridge, command_id="SIM_CMD_TTL_EXPIRED",
                                 ttl_ms=1500, issued=issued)
        assert bridge._gate_blockers(command, clock.now()) == []
        assert module._parse_iso(command["expires_at"]) == issued + timedelta(milliseconds=1500)
        bridge._receive_dispatch(command)
        assert bridge._stores.receipt_for_command(command["command_id"]) == []
        mono_before = clock.monotonic()
        clock.advance(elapsed_ms / 1000)
        assert clock.monotonic() == mono_before + elapsed_ms / 1000
        assert clock.now() == issued + timedelta(milliseconds=elapsed_ms)
        assert bridge._gate_blockers(command, clock.now()) == ["DISPATCH_EXPIRED"]
        blocked_before = bridge.counters["native_call_blocked"]
        bridge._pump_once()
        assert bridge._stores.receipt_for_command(command["command_id"]) == [
            ("NO_CALL", command["attempt_id"])]
        receipts = _dispatch_receipt_bodies(bridge, command)
        assert len(receipts) == 1
        assert receipts[0]["error_code"] == "dispatch_expired"
        assert receipts[0]["result_kind"] is None and receipts[0]["result_value"] is None
        assert receipts[0]["binding"] == command["binding"]
        assert receipts[0]["request_sha256"] == module.sha256_hex(module.canonical_dumps(command))
        assert bridge._dispatch_slot is None
        assert command["command_id"] not in bridge._consumed_commands
        assert fake.send_calls() == [] and fake.cancel_calls() == []
        assert bridge.counters["native_send_attempts"] == 0
        assert bridge.counters["native_cancel_attempts"] == 0
        assert bridge.counters["native_call_blocked"] == blocked_before + 1
    finally:
        stop_started_bridge(bridge)


def test_m06_loose_acl_on_credential_refused_before_any_read(monkeypatch, tmp_path):
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    capture = []

    def transport(url, payload, timeout, headers=None):
        capture.append(payload)
        return 200, _epoch_idle_body(module, payload, SYNTHETIC_SERVICE_EPOCH)

    bridge = _waiting_bridge(module, fake, tmp_path, transport)
    try:
        # deliberately loose: Everyone is granted access on the credential slot
        _install_pairing_slot(bridge._store_path, extra_trustee_sid="S-1-1-0")
        assert bridge._try_activate_pairing() is False
        assert bridge._last_pairing_reason == "PAIRING_ACL_RESTRICTED"
        assert bridge._pairing_state == "WAIT_PAIRING"
        assert bridge._pairing_token is None
        assert capture == []  # no HTTP from a refused credential
        assert fake.send_calls() == []
        bridge.on_stop()
    finally:
        stop_started_bridge(bridge)


def test_m06_injection_seam_refusal_matrix(monkeypatch, tmp_path):
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    cases = [
        # token but NO explicit store (default-store production path)
        (dict(native=fake, pairing_token="ab" * 32), None, "PAIRING_INJECTION_NOT_ALLOWED"),
        # token + explicit store but NO injected native/transport
        (dict(store_directory=tmp_path / "s1", pairing_token="ab" * 32), None,
         "PAIRING_INJECTION_NOT_ALLOWED"),
        # full fixture but a non-hex token
        (dict(native=fake, store_directory=tmp_path / "s2", pairing_token="SYNTHETIC-TOKEN"),
         None, "PAIRING_TOKEN_FORMAT_INVALID"),
    ]
    for kwargs, _, expected in cases:
        bridge = module.AKExecutionBridge(**kwargs)
        bridge.strategy_id = 23
        bridge.set_params({"exchange": "SHFE", "instrument_id": "rb2610", "hedge_flag": "1"})
        bridge.on_init()
        bridge.on_start()
        assert bridge._running is False
        assert bridge._last_refusal["code"] == expected
        assert bridge._pairing_token is None
    # no store was created by any refused injection
    assert not (tmp_path / "s1").exists()
    assert not (tmp_path / "s2").exists()


def test_m06_unpairable_origin_url_refuses_activation(monkeypatch, tmp_path):
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    capture = []

    def transport(url, payload, timeout, headers=None):
        capture.append(payload)
        return 200, _epoch_idle_body(module, payload, SYNTHETIC_SERVICE_EPOCH)

    bridge = module.AKExecutionBridge(native=fake, transport=transport,
                                      store_directory=tmp_path / "store")
    bridge.strategy_id = 23
    bridge.set_params({"exchange": "SHFE", "instrument_id": "rb2610", "hedge_flag": "1",
                       # loopback-valid but not an explicit 127.0.0.1 http origin
                       "execution_api_url": "http://localhost:17779/execution/v1/poll"})
    bridge.on_init()
    bridge.on_start()
    try:
        assert bridge._pairing_state == "WAIT_PAIRING"
        _install_pairing_slot(bridge._store_path)
        assert bridge._try_activate_pairing() is False
        assert bridge._last_pairing_reason == "PAIRING_ORIGIN_INVALID"
        assert capture == []  # zero HTTP from an origin-unpairable URL
        bridge.on_stop()
    finally:
        stop_started_bridge(bridge)


# ---------------------------------------------------------------------------
# X01-C (decision NE1-ARB-LM-X01-001 §2 RULING X01-01, §6 V01): the ONLY
# legal pairing-slot service id is 'execution-' + exactly 16 lowercase hex
# (26 ASCII chars, case-sensitive, leading zeros kept). The four client
# identity fields stay hex32; a service id never substitutes for a client
# instance and vice versa. All values here are SYNTHETIC.
# ---------------------------------------------------------------------------

def _synthetic_slot_record(**overrides):
    """Structurally valid SYNTHETIC pairing record for the format matrix."""
    base = {
        "schema_version": CLIENT_PAIRING_SCHEMA,
        "request_id": "0a" * 16,
        "request_sha256": "0b" * 32,
        "instance_id": "0c" * 16,
        "session_epoch": "0d" * 16,
        "generation": "0e" * 16,
        "deployment_key": "0f" * 32,
        "strategy_id": 23,
        "params_sha256": "1a" * 32,
        "client_file_sha256": "1b" * 32,
        "execution_origin": "http://127.0.0.1:17779",
        "service_instance_id": SYNTHETIC_SERVICE_INSTANCE_ID,
        "service_epoch": SYNTHETIC_SERVICE_EPOCH,
        "contract_lock_sha256": "1c" * 32,
        "token": SYNTHETIC_PAIRING_TOKEN,
    }
    base.update(overrides)
    return base


def test_x01_service_instance_id_exact_syntax_matrix(monkeypatch, tmp_path):
    """V01: exact-syntax positives (incl. leading zeros) pass the client
    validator; every other shape — bare hex32, execution-<32hex>, 15/17-hex
    suffixes, case variants, wrong prefix, whitespace/newline, non-hex, path
    characters, empty string and non-strings — is PAIRING_FORMAT_INVALID."""
    module, _ = load_client_from_temp(monkeypatch, tmp_path)
    for value in ("execution-0123456789abcdef", "execution-0000000000000000",
                  "execution-ffffffffffffffff"):
        assert module._service_instance_id(value) is True
        module._validate_client_pairing_record(
            _synthetic_slot_record(service_instance_id=value))
    bad_values = [
        uuid.uuid4().hex,                      # bare hex32 (client identity form)
        "execution-" + uuid.uuid4().hex,       # execution-<32hex>
        "execution-0123456789abcde",           # 15-char suffix
        "execution-0123456789abcdef0",         # 17-char suffix
        "EXECUTION-0123456789abcdef",          # prefix is case-sensitive
        "execution-0123456789ABCDEF",          # hex must be lowercase
        "observer-0123456789abcdef",           # wrong prefix
        " execution-0123456789abcdef",         # leading whitespace
        "execution-0123456789abcdef\n",        # trailing newline
        "execution-0123456789abcdeg",          # non-hex suffix character
        "execution-/../abcdef",                # path characters
        "",                                    # empty string
        None, 123, True, ["execution-0123456789abcdef"],  # non-strings
    ]
    for value in bad_values:
        assert module._service_instance_id(value) is False
        with pytest.raises(module.ContractError, match="PAIRING_FORMAT_INVALID"):
            module._validate_client_pairing_record(
                _synthetic_slot_record(service_instance_id=value))
    # the four client identity fields remain hex32: a service id cannot stand
    # in for a client instance (V01 anti-confusion boundary)
    for field in ("request_id", "instance_id", "session_epoch", "generation"):
        with pytest.raises(module.ContractError, match="PAIRING_FORMAT_INVALID"):
            module._validate_client_pairing_record(
                _synthetic_slot_record(**{field: SYNTHETIC_SERVICE_INSTANCE_ID}))


@pytest.mark.parametrize("bad_service_id", [
    uuid.uuid4().hex,                    # bare hex32 client identity in the service field
    "execution-" + uuid.uuid4().hex,     # execution-<32hex>
    "observer-0123456789abcdef",         # wrong prefix
    "EXECUTION-0123456789ABCDEF",        # case-sensitive prefix/hex
    "",                                  # empty string
    123,                                 # non-string: number
    True,                                # non-string: bool
])
def test_x01_bad_service_id_refuses_activation_zero_http(monkeypatch, tmp_path,
                                                         bad_service_id):
    """V01 activation negatives: a syntactically illegal service id in an
    otherwise fully matching credential slot keeps the client WAIT_PAIRING
    (disabled), promotes no token, sends zero HTTP and preserves the slot
    bytes verbatim."""
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    capture = []

    def transport(url, payload, timeout, headers=None):
        capture.append(payload)
        return 200, _epoch_idle_body(module, payload, SYNTHETIC_SERVICE_EPOCH)

    bridge = _waiting_bridge(module, fake, tmp_path, transport)
    try:
        _install_pairing_slot(bridge._store_path, service_instance_id=bad_service_id)
        slot = Path(bridge._store_path) / "pairing.json"
        before = slot.read_bytes()
        assert bridge._try_activate_pairing() is False
        assert bridge._last_pairing_reason == "PAIRING_FORMAT_INVALID"
        assert bridge._pairing_state == "WAIT_PAIRING"
        assert bridge._pairing_token is None            # candidate never promoted
        assert bridge.counters["pairing_activations"] == 0
        assert capture == []                            # zero HTTP on a bad format
        assert fake.send_calls() == [] and fake.cancel_calls() == []
        assert slot.read_bytes() == before              # original bytes preserved
        # still waiting: a legal slot for the SAME request activates normally
        _install_pairing_slot(bridge._store_path)
        assert bridge._try_activate_pairing() is True
        assert bridge._pairing_state == "PAIRED_DISABLED"
        assert bridge.params_map.enable_trading is False
        bridge.on_stop()
    finally:
        stop_started_bridge(bridge)


# ---------------------------------------------------------------------------
# NE1-ARM-ARMING client matrix (decision NE1-ARB-ARMING-001 RULING 06, C
# rows: ARM-M01/M02/M06/M09/M10/M11/M12 client parts). All identities,
# credentials and service ACKs are SYNTHETIC; the fake native never resolves
# a real attribute; every transport here is an injected double.
# ---------------------------------------------------------------------------

def test_arm_states_eight_state_positive_chain(monkeypatch, tmp_path):
    """ARM-M07 chain walked explicitly: DISARMED -> PREPARED_DISABLED ->
    ARM_READY -> ARM_PENDING -> ARMED -> ARMED(send exhausted) ->
    REVOKE_PENDING -> DISARMED_TERMINAL, each transition asserted in place."""
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    arm_box = {}
    bridge = _prepare_arming(module, fake, tmp_path, arm_box)
    try:
        assert bridge._arm_state == module.ARM_STATE_DISARMED
        assert bridge._arming_effective_now() is False

        _install_arming_proposal(bridge)
        bridge._process_arming_files()
        assert bridge._arm_state == module.ARM_STATE_PREPARED_DISABLED
        assert bridge._arming_effective_now() is False
        assert (Path(bridge._store_path) / "arming-plan.json").exists()

        bridge._process_arming_files()  # already paired + epoch known -> ARM_READY
        assert bridge._arm_state == module.ARM_STATE_ARM_READY
        assert bridge._arming_effective_now() is False
        request = json.loads((Path(bridge._store_path) / "arming-request.json")
                             .read_text(encoding="utf-8"))
        assert request["active"] is True
        assert (Path(bridge._store_path) / "arming-challenge.json").exists()

        _install_arm_credential(bridge)
        bridge._process_arming_files()
        assert bridge._arm_state == module.ARM_STATE_ARM_PENDING
        assert bridge._arming_ack_received is True  # exact ACK already returned
        assert bridge._arming_effective_now() is False  # no legal callback yet
        assert not (Path(bridge._store_path) / "arming.json").exists()
        assert not (Path(bridge._store_path) / "arming-challenge.json").exists()
        state = json.loads((Path(bridge._store_path) / "arming-state.json")
                           .read_text(encoding="utf-8"))
        assert state["consumed"] is True and state["arm_id"] is not None

        bridge._pump_once()  # the legal framework callback
        assert bridge._arm_state == module.ARM_STATE_ARMED
        assert bridge._arming_effective_now() is True
        assert bridge._effective_params_sha256 == bridge._arm_plan["target"]

        set_synthetic_gate_capture(bridge, quote=True, account=True, position=True, contract=True)
        bridge._receive_dispatch(armed_dispatch(bridge, ttl_ms=1500))
        bridge._pump_once()
        assert len(fake.send_calls()) == 1
        assert bridge._arm_state == module.ARM_STATE_ARMED_SEND_EXHAUSTED

        # a second send is permanently refused in this round
        bridge._receive_dispatch(armed_dispatch(bridge, command_id="SIM_CMD_SECOND",
                                                ttl_ms=1500))
        bridge._pump_once()
        assert len(fake.send_calls()) == 1

        # user disarm: revoke -> terminal, idempotent, same-session re-arm dead
        _install_disarm(bridge)
        bridge._process_arming_files()
        assert bridge._arm_state == module.ARM_STATE_DISARMED_TERMINAL
        assert bridge._arming_effective_now() is False
        state = json.loads((Path(bridge._store_path) / "arming-state.json")
                           .read_text(encoding="utf-8"))
        assert state["revoked"] is True and state["local_gate_closed"] is True
        assert state["effective_enable_trading"] is False
        _install_arming_proposal(bridge)  # a new proposal cannot revive this life
        bridge._process_arming_files()
        assert bridge._arm_state == module.ARM_STATE_DISARMED_TERMINAL
        bridge.on_stop()
    finally:
        stop_started_bridge(bridge)


def test_arm_m02_dual_hash_restricted_five_fields(monkeypatch, tmp_path):
    """ARM-M02: the pairing hash stays frozen while the target hash covers
    EXACTLY the five override fields on the frozen canonical copy; only the
    PARAMS_CONFIRMED confirmation carries the target hash pre-activation and
    the envelope switches atomically at ARMED."""
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    arm_box = {}
    bridge = _prepare_arming(module, fake, tmp_path, arm_box)
    try:
        pairing_hash = bridge._params_sha256
        _install_arming_proposal(bridge, risk_policy_id="SIM_POLICY_ARM",
                                 authorization_id="SIM_AUTH_ARM")
        bridge._process_arming_files()
        plan = json.loads((Path(bridge._store_path) / "arming-plan.json")
                          .read_text(encoding="utf-8"))
        target = plan["target_params_sha256"]
        assert target != pairing_hash
        assert set(plan["effective_overrides"]) == set(module.ARMING_OVERRIDE_FIELDS)
        assert plan["effective_overrides"]["execution_mode"] == "native"
        assert plan["effective_overrides"]["max_native_sends"] == 1
        # the frozen safe params and the published pairing request never moved
        assert bridge._params_sha256 == pairing_hash
        request = json.loads((Path(bridge._store_path) / "pairing-request.json")
                             .read_text(encoding="utf-8"))
        assert request["params_sha256"] == pairing_hash
        assert bridge.params_map.execution_mode == "disabled"
        assert bridge.params_map.enable_trading is False
        # the client recomputes the same target from the plan alone
        assert bridge._compute_target_params_hash(plan["effective_overrides"]) == target
        # a different override value yields a different target (five fields matter)
        other = dict(plan["effective_overrides"], risk_policy_id="SIM_POLICY_OTHER")
        assert bridge._compute_target_params_hash(other) != target

        bridge._process_arming_files()  # ARM_READY
        _install_arm_credential(bridge)
        bridge._process_arming_files()  # ARM_PENDING; confirmation captured
        confirmation = arm_box["confirmations"][0]
        assert confirmation["params_sha256"] == target  # the ONLY target carrier
        assert confirmation["adapter_sha256"] == bridge._client_file_sha256
        assert confirmation["explicit_ui_action"] is True
        assert bridge._effective_params_sha256 == pairing_hash  # not switched yet
        bridge._pump_once()
        assert bridge._arm_state == module.ARM_STATE_ARMED
        assert bridge._effective_params_sha256 == target  # atomic switch
        bridge.on_stop()
    finally:
        stop_started_bridge(bridge)


def _scope_of(bridge):
    request_raw = (Path(bridge._store_path) / "pairing-request.json").read_bytes()
    request = json.loads(request_raw.decode("utf-8"))
    return {
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
    }


def test_arm_m02_proposal_negative_matrix(monkeypatch, tmp_path):
    """ARM-M02/M06 negatives: wrong mode, send=2, cancel=2, bool-for-int,
    foreign scope, foreign binding and a stale pairing-request reference each
    keep the client DISARMED with the slot bytes preserved."""
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    arm_box = {}
    bridge = _prepare_arming(module, fake, tmp_path, arm_box)
    try:
        def mutate_field(key, value):
            return lambda record: {**record, key: value}

        cases = [
            ("mode", mutate_field("execution_mode", "disabled")),
            ("sends", mutate_field("max_native_sends", 2)),
            ("cancels", mutate_field("max_native_cancels", 2)),
            ("bool_sends", mutate_field("max_native_sends", True)),
            ("scope", mutate_field("scope", {**_scope_of(bridge),
                                             "session_epoch": "0f" * 16})),
            ("binding", mutate_field("binding", synthetic_binding(
                instance_id=bridge._instance_id, session_epoch="0e" * 16))),
            ("stale_request", mutate_field("scope", {**_scope_of(bridge),
                                                     "pairing_request_sha256": "0d" * 32})),
        ]
        for label, mutate in cases:
            _install_arming_proposal(bridge, mutate=mutate)
            before = (Path(bridge._store_path) / "arming-proposal.json").read_bytes()
            bridge._process_arming_files()
            assert bridge._arm_state == module.ARM_STATE_DISARMED, label
            assert bridge._last_arming_reason is not None, label
            assert (Path(bridge._store_path) / "arming-proposal.json").read_bytes() == before
            assert not (Path(bridge._store_path) / "arming-plan.json").exists(), label
        # a well-formed proposal for the SAME request still works afterwards
        _install_arming_proposal(bridge)
        bridge._process_arming_files()
        assert bridge._arm_state == module.ARM_STATE_PREPARED_DISABLED
        bridge.on_stop()
    finally:
        stop_started_bridge(bridge)


def test_arm_m02_one_plan_per_session_conflict_refused(monkeypatch, tmp_path):
    """ARM-M02: one frozen target plan per session; a second different
    proposal while PREPARED_DISABLED changes nothing."""
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    arm_box = {}
    bridge = _prepare_arming(module, fake, tmp_path, arm_box)
    try:
        _install_arming_proposal(bridge, authorization_id="SIM_AUTH_ARM")
        bridge._process_arming_files()
        plan_bytes = (Path(bridge._store_path) / "arming-plan.json").read_bytes()
        _install_arming_proposal(bridge, authorization_id="SIM_AUTH_OTHER")
        bridge._process_arming_files()  # one step: publishes the FIRST plan's request
        # the conflicting second proposal changed nothing: the frozen plan and
        # its target stay the first one's
        assert bridge._arm_plan["plan"]["authorization_id"] == "SIM_AUTH_ARM"
        assert (Path(bridge._store_path) / "arming-plan.json").read_bytes() == plan_bytes
        request = json.loads((Path(bridge._store_path) / "arming-request.json")
                             .read_text(encoding="utf-8"))
        assert request["authorization_id"] == "SIM_AUTH_ARM"
        bridge.on_stop()
    finally:
        stop_started_bridge(bridge)


def test_arm_m01_params_and_files_alone_never_open_gate(monkeypatch, tmp_path):
    """ARM-M01: a bare Params/UI switch, a proposal alone, pairing alone, a
    service-armed wording alone and an ARMED-looking state file each leave
    the effective gate closed with zero native entry."""
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    arm_box = {}
    bridge = _prepare_arming(module, fake, tmp_path, arm_box)
    try:
        bridge.params_map.enable_trading = True  # bare UI flip grants nothing
        bridge.params_map.execution_mode = "native"  # mode string grants nothing
        _install_arming_proposal(bridge)
        bridge._process_arming_files()  # PREPARED_DISABLED only
        set_synthetic_gate_capture(bridge, quote=True, account=True, position=True, contract=True)
        dispatch = synthetic_dispatch(binding=synthetic_binding(
            instance_id=bridge._instance_id, session_epoch=bridge._session_epoch),
            params_sha256=bridge._arm_plan["target"], command_id="SIM_CMD_EARLY",
            ttl_ms=1500)
        dispatch["permit_id"] = "SIM_PERMIT_ARM"
        bridge._receive_dispatch(dispatch)
        bridge._pump_once()
        assert fake.send_calls() == []
        assert "NOT_ARMED" in bridge._gate_blockers(dispatch, datetime.now(timezone.utc))
        # a forged ARMED state file cannot arm a fresh life either
        forged = json.loads((Path(bridge._store_path) / "arming-state.json")
                            .read_text(encoding="utf-8"))
        forged["state"] = "ARMED"
        forged["effective_enable_trading"] = True
        (Path(bridge._store_path) / "arming-state.json").write_text(
            json.dumps(forged, sort_keys=True), encoding="utf-8")
        bridge.on_stop()
        bridge.on_start()
        assert bridge._arm_state == module.ARM_STATE_DISARMED
        assert bridge._arming_effective_now() is False
        assert bridge._arm_context is None
        bridge.on_stop()
    finally:
        stop_started_bridge(bridge)


def test_arm_m09_one_time_consumption_replay_and_restart(monkeypatch, tmp_path):
    """ARM-M09: the credential burns exactly once — tombstone first, secrets
    destroyed, one confirmation; replays and restarts never re-arm."""
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    arm_box = {}
    bridge = _prepare_arming(module, fake, tmp_path, arm_box)
    try:
        _install_arming_proposal(bridge)
        bridge._process_arming_files()  # -> PREPARED_DISABLED
        bridge._process_arming_files()  # -> ARM_READY
        credential = _install_arm_credential(bridge)
        bridge._process_arming_files()
        bridge._pump_once()
        assert bridge._arm_state == module.ARM_STATE_ARMED
        assert len(arm_box["confirmations"]) == 1
        assert arm_box["confirmations"][0]["confirmation_id"] == credential["arm_id"]

        # replaying the same delivered credential bytes does nothing (the
        # state machine has left ARM_READY; the round is consumed)
        replay_tmp = Path(bridge._store_path) / ".arming.json.probe-tmp"
        replay_tmp.write_bytes(json.dumps(credential, sort_keys=True).encode("utf-8"))
        _apply_protected_dacl(replay_tmp)
        os.replace(replay_tmp, Path(bridge._store_path) / "arming.json")
        bridge._process_arming_files()
        assert len(arm_box["confirmations"]) == 1
        assert bridge._arm_state == module.ARM_STATE_ARMED

        # restart: the persisted state never restores anything, and this new
        # life has no request/challenge slots to consume against
        bridge.on_stop()
        bridge.on_start()
        assert bridge._arm_state == module.ARM_STATE_DISARMED
        assert bridge._arming_effective_now() is False
        bridge._process_arming_files()  # the leftover old-scope proposal is refused
        assert bridge._arm_state == module.ARM_STATE_DISARMED
        assert len(arm_box["confirmations"]) == 1
        bridge.on_stop()
    finally:
        stop_started_bridge(bridge)


def test_arm_m09_ack_loss_bounded_retransmission_same_logic(monkeypatch, tmp_path):
    """ARM-M09: ACK loss retries the SAME logical confirmation (same arm_id,
    same expiry); a service BLOCKED answer is terminal; no second arm."""
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    arm_box = {"control_mode": "fail"}
    bridge = _prepare_arming(module, fake, tmp_path, arm_box)
    try:
        _install_arming_proposal(bridge)
        bridge._process_arming_files()  # -> PREPARED_DISABLED
        bridge._process_arming_files()  # -> ARM_READY
        credential = _install_arm_credential(bridge)
        bridge._process_arming_files()  # consumed + confirmation sent, ACK lost
        assert bridge._arm_state == module.ARM_STATE_ARM_PENDING
        assert bridge._arming_ack_received is False
        first_expiry = bridge._arm_confirmation["expires_at"]
        bridge._retransmit_confirmation_if_pending()  # bounded same-logic retry
        assert len(arm_box["confirmations"]) == 2
        assert arm_box["confirmations"][0] == arm_box["confirmations"][1]
        assert arm_box["confirmations"][0]["confirmation_id"] == credential["arm_id"]
        assert bridge._arm_confirmation["expires_at"] == first_expiry

        arm_box["control_mode"] = "ok"  # the service recovers
        assert bridge._retransmit_confirmation_if_pending() is True
        assert bridge._arming_ack_received is True
        bridge._pump_once()
        assert bridge._arm_state == module.ARM_STATE_ARMED
        bridge.on_stop()
    finally:
        stop_started_bridge(bridge)

    # BLOCKED variant: terminal, never a second challenge or arm (same client
    # module/fake, a second isolated store)
    arm_box2 = {"control_mode": "blocked"}
    bridge2 = _prepare_arming(module, fake, tmp_path, arm_box2, store_name="store2")
    try:
        _install_arming_proposal(bridge2)
        bridge2._process_arming_files()  # -> PREPARED_DISABLED
        bridge2._process_arming_files()  # -> ARM_READY
        _install_arm_credential(bridge2)
        bridge2._process_arming_files()
        assert bridge2._arm_state == module.ARM_STATE_DISARMED_TERMINAL
        assert bridge2._last_arming_reason == "service_blocked_confirmation"
        assert bridge2._arming_effective_now() is False
        assert fake.send_calls() == []  # no half ever reached native
        bridge2.on_stop()
    finally:
        stop_started_bridge(bridge2)


def test_arm_m10_disarm_races_and_stale_scope(monkeypatch, tmp_path):
    """ARM-M10: revocation wins over a pending arm; disarm before consumption
    keeps zero confirmations; a foreign-scope disarm is STALE_SCOPE and
    revokes nothing here."""
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    arm_box = {"control_mode": "fail"}  # keep the round ACK-pending
    bridge = _prepare_arming(module, fake, tmp_path, arm_box)
    try:
        _install_arming_proposal(bridge)
        bridge._process_arming_files()
        bridge._process_arming_files()  # ARM_READY
        _install_arm_credential(bridge)  # user delivers the credential
        _install_disarm(bridge)          # ...and disarms before consumption
        bridge._process_arming_files()
        assert bridge._arm_state == module.ARM_STATE_DISARMED_TERMINAL
        assert bridge._arming_consumed is False
        assert arm_box.get("confirmations") is None  # zero confirmations sent
        # the credential slot survives for the user's reviewed disposal
        assert (Path(bridge._store_path) / "arming.json").exists()
        bridge.on_stop()
    finally:
        stop_started_bridge(bridge)

    # stale scope: another instance's disarm must not revoke this one (same
    # module/fake, second isolated store)
    arm_box2 = {}
    bridge2 = _prepare_arming(module, fake, tmp_path, arm_box2, store_name="store2")
    try:
        _install_arming_proposal(bridge2)
        bridge2._process_arming_files()   # -> PREPARED_DISABLED
        foreign_scope = {**_scope_of(bridge2), "instance_id": "0a" * 16}
        _install_disarm(bridge2, scope=foreign_scope)
        with bridge2._service_epoch_lock:  # hold the round at PREPARED for the
            saved_epoch = bridge2._service_epoch  # observation itself
            bridge2._service_epoch = None
        bridge2._process_arming_files()
        assert bridge2._arm_state == module.ARM_STATE_PREPARED_DISABLED
        assert bridge2._last_arming_reason == "ARMING_DISARM_STALE_SCOPE"
        assert bridge2._arming_revoked is False
        with bridge2._service_epoch_lock:
            bridge2._service_epoch = saved_epoch
        bridge2._process_arming_files()   # the round still progresses normally
        assert bridge2._arm_state == module.ARM_STATE_ARM_READY
        assert bridge2._arming_revoked is False
        bridge2.on_stop()
    finally:
        stop_started_bridge(bridge2)


def test_arm_m10_revoked_round_admits_no_new_native(monkeypatch, tmp_path):
    """ARM-M10: after the local gate closes, no new native entry happens and
    unresolved facts are preserved (no implicit cancel, no zeroed counts)."""
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    bridge = armed_bridge(module, fake, tmp_path)
    try:
        # one send happened in the armed round; the ticket is unresolved
        bridge._receive_dispatch(armed_dispatch(bridge, command_id="SIM_CMD_R1",
                                                ttl_ms=1500))
        bridge._pump_once()
        assert len(fake.send_calls()) == 1
        # the post-revoke probe tickets are built while the context is live
        probe_cancel = armed_dispatch(bridge, kind="CANCEL", command_id="SIM_CMD_R2",
                                      order_id=77, ttl_ms=1500)
        bridge._revoke_arming("synthetic_user_disarm")
        assert bridge._arming_effective_now() is False
        state = json.loads((Path(bridge._store_path) / "arming-state.json")
                           .read_text(encoding="utf-8"))
        assert state["revoked"] is True
        # the local gate fact and the service barrier stay separately honest
        assert state["local_gate_closed"] is True
        assert state["service_gate_closed"] is False
        # cancels are NOT implicitly submitted on revocation
        assert [c for c in fake.calls if c[0] == "cancelOrder"] == []
        set_synthetic_gate_capture(bridge, quote=True)
        bridge._receive_dispatch(probe_cancel)
        bridge._pump_once()
        assert [c for c in fake.calls if c[0] == "cancelOrder"] == []
        assert len(fake.send_calls()) == 1
        bridge.on_stop()
    finally:
        stop_started_bridge(bridge)


def test_arm_m11_send_exhausted_keeps_independent_cancel(monkeypatch, tmp_path):
    """ARM-M11: send=1/cancel=1 — the first send occupies the quota even when
    rejected, a second send is refused, the separately authorized cancel
    still works exactly once, and a cancel can never reopen the send lane."""
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    arm_box = {}
    bridge = _prepare_arming(module, fake, tmp_path, arm_box)
    try:
        _install_arming_proposal(bridge, sends=1, cancels=1)
        bridge._process_arming_files()  # -> PREPARED_DISABLED
        bridge._process_arming_files()  # -> ARM_READY
        _install_arm_credential(bridge)
        bridge._process_arming_files()
        bridge._pump_once()
        assert bridge._arm_state == module.ARM_STATE_ARMED
        set_synthetic_gate_capture(bridge, quote=True, account=True, position=True, contract=True)

        fake.fail_send = True  # the first send attempt FAILS — quota still burns
        bridge._receive_dispatch(armed_dispatch(bridge, command_id="SIM_CMD_FAIL",
                                                ttl_ms=1500))
        bridge._pump_once()
        assert len(fake.send_calls()) == 1  # the attempt happened
        assert bridge._arm_state == module.ARM_STATE_ARMED_SEND_EXHAUSTED
        fake.fail_send = False

        # the second send is refused at the gate (probe without occupying the
        # single dispatch slot, which a blocked ticket legitimately retains)
        second = armed_dispatch(bridge, command_id="SIM_CMD_SECOND", ttl_ms=1500)
        blockers = bridge._gate_blockers(second, datetime.now(timezone.utc))
        assert "SEND_BUDGET_EXHAUSTED" in blockers

        # the separately authorized owned cancel still works exactly once
        bridge._latest_capture["quote_at"] = datetime.now(timezone.utc) - timedelta(seconds=30)
        bridge._receive_dispatch(armed_dispatch(bridge, kind="CANCEL",
                                                command_id="SIM_CMD_CX1", order_id=42,
                                                ttl_ms=1500))
        bridge._pump_once()
        assert len([c for c in fake.calls if c[0] == "cancelOrder"]) == 1
        # the second cancel is out of budget, and the send lane stays closed
        second_cancel = armed_dispatch(bridge, kind="CANCEL", command_id="SIM_CMD_CX2",
                                       order_id=43, ttl_ms=1500)
        assert "CANCEL_BUDGET_EXHAUSTED" in bridge._gate_blockers(
            second_cancel, datetime.now(timezone.utc))
        third = armed_dispatch(bridge, command_id="SIM_CMD_THIRD", ttl_ms=1500)
        assert "SEND_BUDGET_EXHAUSTED" in bridge._gate_blockers(
            third, datetime.now(timezone.utc))
        assert len(fake.send_calls()) == 1
        bridge.on_stop()
    finally:
        stop_started_bridge(bridge)


def test_arm_m12_no_callback_no_activation_and_expiry(monkeypatch, tmp_path):
    """ARM-M12: without a legal framework callback the round stays ARM_PENDING
    and finally expires; a UTC clock rollback cannot revive a dead round."""
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    arm_box = {}
    bridge = _prepare_arming(module, fake, tmp_path, arm_box)
    try:
        _install_arming_proposal(bridge)
        bridge._process_arming_files()  # -> PREPARED_DISABLED
        bridge._process_arming_files()  # -> ARM_READY
        _install_arm_credential(bridge)
        bridge._process_arming_files()  # consumed + exact ACK received
        assert bridge._arming_ack_received is True
        assert bridge._arm_state == module.ARM_STATE_ARM_PENDING
        assert bridge._arming_effective_now() is False  # no callback: not armed
        # no _pump_once happens; the monotonic window lapses
        bridge._arming_deadline_monotonic = time.monotonic() - 0.001
        bridge._process_arming_files()
        assert bridge._arm_state == module.ARM_STATE_DISARMED_TERMINAL
        assert bridge._last_arming_reason == "arming_expired"
        # rolling the clock back cannot revive the round
        bridge._arming_deadline_monotonic = time.monotonic() + 300
        bridge._pump_once()
        assert bridge._arm_state == module.ARM_STATE_DISARMED_TERMINAL
        assert bridge._arming_effective_now() is False
        assert fake.send_calls() == []
        bridge.on_stop()
    finally:
        stop_started_bridge(bridge)


def test_arm_m12_background_lane_no_native_no_gui(monkeypatch, tmp_path):
    """ARM-M12: the background arming pump (any thread that is not the bound
    callback thread) consumes files and persists state but never resolves a
    native attribute, never touches the GUI and never flips effective."""
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    arm_box = {}
    bridge = _prepare_arming(module, fake, tmp_path, arm_box)
    try:
        _install_arming_proposal(bridge)
        bridge._process_arming_files()  # -> PREPARED_DISABLED
        bridge._process_arming_files()  # -> ARM_READY
        _install_arm_credential(bridge)  # deliver everything up front
        calls_before = len(fake.calls)
        attrs_before = len(fake.attribute_access)
        done = threading.Event()

        def background_worker():
            bridge._process_arming_files()
            done.set()

        worker = threading.Thread(target=background_worker)
        worker.start()
        assert done.wait(timeout=10)
        worker.join(timeout=5)
        assert bridge._arming_effective_now() is False  # only callbacks arm
        # zero NEW native/GUI surface from the whole background chain
        assert len(fake.calls) == calls_before
        assert len(fake.attribute_access) == attrs_before
        assert fake.send_calls() == [] and fake.cancel_calls() == []
        # the legal callback alone flips effective (ARM_PENDING -> ARMED)
        assert bridge._arm_state == module.ARM_STATE_ARM_PENDING
        bridge._pump_once()
        assert bridge._arm_state == module.ARM_STATE_ARMED
        assert bridge._arming_effective_now() is True
        bridge.on_stop()
    finally:
        stop_started_bridge(bridge)


def test_arm_m06_arming_slots_whitelisted_unknown_still_refused(monkeypatch, tmp_path):
    """ARM-M13 client part: the deterministic store layout accepts exactly
    the arming-v1 slot set; one unknown file still refuses the next start."""
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    arm_box = {}
    bridge = _prepare_arming(module, fake, tmp_path, arm_box)
    store = Path(bridge._store_path)
    try:
        _install_arming_proposal(bridge)
        bridge._process_arming_files()
        _install_disarm(bridge)
        expected = {"arming-proposal.json", "arming-plan.json", "arming-state.json",
                    "disarm.json", "identity.json", "pairing-request.json",
                    "client.lock", "receipts.db", "outbox.db"}
        assert expected <= set(os.listdir(store))
        bridge.on_stop()
        # restart over the same slots works (they are whitelisted)
        bridge.on_start()
        assert bridge._running is True
        bridge.on_stop()
        # one unknown file is still a hard layout refusal
        (store / "mystery-slot.json").write_text("{}", encoding="utf-8")
        bridge.on_start()
        assert bridge._running is False
        assert bridge._last_refusal["code"] == "LEGACY_STORE_MIGRATION_REQUIRED"
    finally:
        stop_started_bridge(bridge)


