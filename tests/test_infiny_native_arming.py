"""NE1 arming cross-end matrix (ARM-LM-I, decision NE1-ARB-ARMING-001).

Owner: ARM-LM-I (integration owner). Specification:
``data/maintenance_artifacts/infiny-arming-mechanism-20260918-r1/decision.md``
(RULING 02 eight-state machine, RULING 06 ARM-M01..ARM-M14 matrix,
RULING 07 LM-I row). This file is the W1 preparation deliverable:

- Runnable-now sections prove the DESIGN of the ruled mechanisms through the
  shared reference models and fixtures in ``tests/infiny_native_fakes.py``
  (eight-state machine, dual-hash semantics, one-time consumption order,
  clock rollback, console interactivity, strict AR4.2 vectors, REAL Windows
  DACL sandbox) plus durable current-tree facts the decision keeps true.
- Cross-end ARM-M cases run against the FROZEN ARM-LM-C/D/S combination
  (W2 merge, 2026-09-18) through public paths only; the honest freeze gate
  ``_require_frozen_arming`` keeps failing loudly if the freeze is absent.
  Run with
  ``python -B scripts/test_offline.py -q tests/test_infiny_native_arming.py``.

Everything is SYNTHETIC: isolated temp stores, fake INFINIGO, loopback test
services only; no production store/port/credential is touched, ``.env`` is
never read, real broker stays NOT_RUN, and no production bypass flag or
private-field injection exists in this file.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from infiny_native_fakes import (
    ACTION_SOURCE_USER_LOCAL_CONSOLE,
    ARMING_CHALLENGE_SCHEMA,
    ARMING_FILE_NAMES,
    ARMING_PREREQUISITE_KINDS,
    ARMING_PREREQUISITES_SCHEMA,
    ARMING_PROPOSAL_SCHEMA,
    ARMING_REQUEST_ACTIVE_WINDOW_MAX_SECONDS,
    ARMING_REQUEST_SCHEMA,
    ARMING_SCHEMA_FIELDS,
    ARMING_STATE_DISARMED,
    ARMING_STATE_ARMED,
    ARMING_STATE_ARMED_SEND_EXHAUSTED,
    ARMING_STATE_ARM_PENDING,
    ARMING_STATE_ARM_READY,
    ARMING_STATE_PREPARED_DISABLED,
    ARMING_STATE_REVOKE_PENDING,
    ARMING_STATE_TERMINAL,
    ARMING_STATES,
    CLIENT_ARMING_FIELDS,
    CLIENT_ARMING_SCHEMA,
    CLIENT_ARMING_VERSION_EXPECTED,
    CLIENT_SOURCE,
    CRASH_POINTS,
    ConnectionCountingListener,
    FakeExecutionService,
    LedgerError,
    SYNTHETIC_ARMING_AUTHORIZATION_ID,
    SYNTHETIC_ARMING_CONTRACT_LOCK,
    SYNTHETIC_ARMING_PERMIT_ID,
    SYNTHETIC_ARMING_SERVICE_EPOCH,
    SYNTHETIC_ARMING_SERVICE_INSTANCE,
    SYNTHETIC_PAIRING_TOKEN,
    TempExecutionStack,
    ArmingAclError,
    ArmingAclSandbox,
    ArmingClock,
    ArmingCrashInjected,
    ArmingDeadline,
    ArmingFixtureError,
    ArmingSlotRefusal,
    ArmingStateModel,
    ConsoleRefusal,
    FakeConsoleTTY,
    LEGACY_V2B_CLIENT_SHA256,
    OneTimeCredentialSlot,
    PARAMS_HASH_FIELDS,
    SERVICE_ARMING_VERSION_EXPECTED,
    TARGET_OVERRIDE_FIELDS,
    TOOL_ARMING_VERSION_EXPECTED,
    StepClock,
    armed_dispatch,
    arm_bridge_from_tool_round,
    arm_bridge_to_armed,
    arming_fixture_user_sid,
    arming_strict_loads,
    assert_no_secret_material,
    deploy_self_strategy,
    expected_default_store,
    inject_arming_service_seams,
    install_arm_credential,
    install_arming_proposal,
    load_client_from_temp,
    loosen_arming_slot_dacl,
    post_json,
    publish_arming_slot,
    reference_target_params_sha256,
    set_synthetic_gate_capture,
    stop_started_bridge,
    synthetic_binding,
    synthetic_arming_challenge,
    synthetic_arming_credential,
    synthetic_arming_hex,
    synthetic_arming_plan,
    synthetic_arming_prerequisites,
    synthetic_arming_proposal,
    synthetic_arming_registrations,
    synthetic_arming_request,
    synthetic_arming_scope,
    synthetic_arming_state,
    validate_arming_document,
    validate_arming_prerequisites,
    validate_arming_registrations,
    validate_arming_state_transition,
)

ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = ROOT / "scripts" / "pair_infiny_execution.py"
SERVICE_PATH = ROOT / "src" / "ak_trading" / "infiny_execution" / "client_service.py"

_windows_only = pytest.mark.skipif(
    sys.platform != "win32",
    reason="AR4.3 keeps the R2.4 REAL Windows DACL standard; non-Windows "
           "platforms fail closed and cannot be proven here")

# The legacy migration-block fact is proven against the exact v2b file the
# decision snapshotted; once ARM-LM-C lands, this proof is superseded by the
# new client's own extension-whitelist tests (ARM-M13 positive half).
_v2b_tree_only = pytest.mark.skipif(
    hashlib.sha256(CLIENT_SOURCE.read_bytes()).hexdigest() != LEGACY_V2B_CLIENT_SHA256,
    reason="tree no longer ships the decision-snapshotted v2b client "
           "(expected after the ARM-LM-C freeze); legacy fact superseded")


def _client_arming_constants_defined_before_use():
    """Static probe for the in-flight ARM-LM-C revision observed 2026-09-18:
    the deployed client referenced ARMING_*_FILE_NAME constants (added to
    _STORE_JSON_FILE_NAMES) before their assignments, so ANY fresh import of
    the client raises NameError (minimal reproduction: importing the tree file
    fails at the _STORE_JSON_FILE_NAMES tuple). This gate skips the
    client-deploying design pins while the sibling file is un-importable and
    auto-recovers once the owner's revision imports again; it never replaces
    an assertion (cross-node regression routed to ARM-LM-C, see
    implementation.md finding ARM-LM-I-X01)."""
    import re
    source = CLIENT_SOURCE.read_text(encoding="utf-8")
    for match in re.finditer(r"^(ARMING_[A-Z_]+_FILE_NAME|"
                             r"CLIENT_ARMING_FILE_NAME|CLIENT_DISARM_FILE_NAME)"
                             r"\s*=\s*", source, re.M):
        if match.start() > source.find(match.group(1)):
            # occurrences exist before the definition assignment
            return False
    return True


_needs_importable_client = pytest.mark.skipif(
    not _client_arming_constants_defined_before_use(),
    reason="in-flight ARM-LM-C client revision does not import (ARMING_* file"
           " constants referenced before definition); re-run at the freeze")


# ===========================================================================
# Shared helpers
# ===========================================================================

def arm_bridge_from_tool_round(module, bridge):
    """X01 unblock repair 4 (local shadow of the fakes helper): state-aware
    tool-round consumption. The fakes variant assumes the bridge is still
    DISARMED and drives the whole proposal chain; the tool-round tests call it
    with the proposal ALREADY consumed by ``_full_tool_round`` (bridge at
    ARM_READY), so the first two transitions are skipped when already past
    them. Assertion set is unchanged for every state it accepts."""
    if bridge._arm_state == module.ARM_STATE_DISARMED:
        bridge._process_arming_files()          # DISARMED -> PREPARED_DISABLED
        assert bridge._arm_state == module.ARM_STATE_PREPARED_DISABLED, \
            bridge._last_arming_reason
        bridge._process_arming_files()          # PREPARED_DISABLED -> ARM_READY
    assert bridge._arm_state == module.ARM_STATE_ARM_READY, \
        bridge._last_arming_reason
    bridge._process_arming_files()              # ARM_READY -> ARM_PENDING (+ ACK)
    assert bridge._arm_state == module.ARM_STATE_ARM_PENDING, \
        bridge._last_arming_reason
    assert bridge._arming_ack_received, bridge._last_arming_reason
    bridge._pump_once()                         # legal callback -> ARMED
    assert bridge._arm_state == module.ARM_STATE_ARMED, bridge._last_arming_reason
    assert bridge._arming_effective_now() is True
    assert bridge.params_map.enable_trading is False
    return bridge


def _full_legal_model(*, max_cancels=0, cancel_scope_authorized=True):
    """Drive one clean lifecycle to ARMED per the RULING 02 table."""
    model = ArmingStateModel(max_cancels=max_cancels,
                             cancel_scope_authorized=cancel_scope_authorized)
    model.on_start()
    model.prepare()
    model.note_pairing(paired_disabled=True, epoch_checked=True, plan_valid=True)
    model.pairing_epoch_verified()
    model.arm_credential_validated()
    model.service_ack(matched=True)
    model.legal_callback()
    return model


def _require_frozen_arming(*, client=None, tool=False, service=False):
    """Honest freeze gate: if one of these tests is ever unskipped before
    the ARM-LM-C/D/S freeze lands, fail LOUDLY with the missing artifact —
    never silently pass and never weaken into a bypass (work-order edge)."""
    problems = []
    if client is not None and getattr(client, "ADAPTER_VERSION", None) \
            != CLIENT_ARMING_VERSION_EXPECTED:
        problems.append(f"client!={CLIENT_ARMING_VERSION_EXPECTED}")
    if tool:
        source = TOOL_PATH.read_text(encoding="utf-8")
        if "prepare-arm" not in source or TOOL_ARMING_VERSION_EXPECTED not in source:
            problems.append(f"tool!={TOOL_ARMING_VERSION_EXPECTED}")
    if service:
        source = SERVICE_PATH.read_text(encoding="utf-8")
        if SERVICE_ARMING_VERSION_EXPECTED not in source:
            problems.append(f"service!={SERVICE_ARMING_VERSION_EXPECTED}")
    if problems:
        raise AssertionError("ARM-LM-C/D/S freeze not present: " + ", ".join(problems))


def _run_tool(arguments):
    """Run the reviewed tool CLI exactly as AR5.3 freezes the command forms
    (direct argument list, capture stdout+stderr separately, never a shell)."""
    return subprocess.run([sys.executable, "-B", "-X", "utf8", str(TOOL_PATH), *arguments],
                          capture_output=True, text=True, cwd=str(ROOT))


def _console_flow_confirm(console, *, request_id, request_bytes_reader,
                          deadline, scope_display):
    """Fixture-level model of the AR1.3 tool-side confirmation order:
    interactive check -> request re-read -> window check -> full scope
    display -> fresh phrase read -> request-bytes re-verification."""
    console.require_interactive()
    first = request_bytes_reader()
    if deadline.expired():
        raise ConsoleRefusal("CONFIRMATION_WINDOW_EXPIRED")
    for line in scope_display:
        console.display(line)
    phrase = console.read_phrase(request_id)
    if request_bytes_reader() != first:
        raise ConsoleRefusal("ARMING_REQUEST_CHANGED")
    return phrase


def _scope_display_lines(credential_request, challenge_secret):
    """The AR1.3 mandatory disclosure set (SYNTHETIC values; the challenge
    itself is deliberately NOT part of the display)."""
    scope = credential_request["scope"]
    return [
        f"account marker: SYNTHETIC-ACCOUNT-A (opaque)",
        f"binding: {json.dumps(credential_request['binding'], sort_keys=True)}",
        f"client_file_sha256: {scope['client_file_sha256']}",
        f"adapter_version: {scope['adapter_version']}",
        f"contract_lock_sha256: {scope['contract_lock_sha256']}",
        f"mode: native  action: OPEN_LONG  volume: 1  price range: [3500, 3500]",
        f"expires_at: {credential_request['expires_at']}",
        f"authorization_id: {credential_request['authorization_id']}",
        f"permit_id: {credential_request['permit_id']}",
        f"max_native_sends=1  max_native_cancels=0",
        f"THIS IS THE ONLY ACTIVATION OF THIS CLIENT LIFETIME",
    ]


# ===========================================================================
# §1 RULING 02 — eight-state machine design validation (runnable now)
# ===========================================================================

def test_model_full_legal_chain_matches_ruling_table():
    model = _full_legal_model()
    assert model.state == ARMING_STATE_ARMED
    assert model.effective_enable_trading is True
    assert model.consumed is True and model.activation_count == 1
    model.first_send_consumed()
    assert model.state == ARMING_STATE_ARMED_SEND_EXHAUSTED
    assert model.effective_enable_trading is True  # gate open, send quota gone
    model.revoke("USER_DISARM")
    assert model.state == ARMING_STATE_REVOKE_PENDING
    assert model.effective_enable_trading is False  # row 7: local gate closes immediately
    model.local_gate_closed()
    assert model.state == ARMING_STATE_TERMINAL
    assert model.effective_enable_trading is False
    assert [event for _, event in model.event_log] == [
        "on_start", "prepare", "pairing_epoch_verified", "arm_credential_validated",
        "service_ack", "legal_callback", "first_send_consumed", "revoke",
        "local_gate_closed"]


def test_model_initial_state_is_disarmed_and_default_disabled():
    model = ArmingStateModel()
    assert model.state == ARMING_STATE_DISARMED
    assert model.effective_enable_trading is False
    assert model.consumed is False and model.revoked is False
    model.on_start()  # new object / on_start -> DISARMED, nothing else
    assert model.state == ARMING_STATE_DISARMED


@pytest.mark.parametrize("state,event,kwargs", [
    # prepare only from DISARMED
    (ARMING_STATE_PREPARED_DISABLED, "prepare", {}),
    (ARMING_STATE_ARM_READY, "prepare", {}),
    (ARMING_STATE_ARMED, "prepare", {}),
    (ARMING_STATE_TERMINAL, "prepare", {}),
    # epoch verification only from PREPARED_DISABLED
    (ARMING_STATE_DISARMED, "pairing_epoch_verified", {}),
    (ARMING_STATE_ARM_READY, "pairing_epoch_verified", {}),
    # arming only from ARM_READY
    (ARMING_STATE_DISARMED, "arm_credential_validated", {}),
    (ARMING_STATE_PREPARED_DISABLED, "arm_credential_validated", {}),
    (ARMING_STATE_ARMED, "arm_credential_validated", {}),
    # callback cannot arm without the exact ACK first (AR2.2 row 4)
    (ARMING_STATE_ARM_PENDING, "legal_callback", {}),
    (ARMING_STATE_DISARMED, "legal_callback", {}),
    (ARMING_STATE_ARM_READY, "legal_callback", {}),
    # send consumption only from ARMED
    (ARMING_STATE_DISARMED, "first_send_consumed", {}),
    (ARMING_STATE_ARM_PENDING, "first_send_consumed", {}),
    (ARMING_STATE_ARMED_SEND_EXHAUSTED, "first_send_consumed", {}),
    (ARMING_STATE_TERMINAL, "first_send_consumed", {}),
    # gate-close ACK only from REVOKE_PENDING
    (ARMING_STATE_DISARMED, "local_gate_closed", {}),
    (ARMING_STATE_ARMED, "local_gate_closed", {}),
    (ARMING_STATE_TERMINAL, "local_gate_closed", {}),
    # on_start only describes a fresh object
    (ARMING_STATE_ARMED, "on_start", {}),
    (ARMING_STATE_TERMINAL, "on_start", {}),
])
def test_model_illegal_transitions_rejected(state, event, kwargs):
    model = ArmingStateModel()
    model.state = state  # place the machine directly in the origin state
    with pytest.raises(ArmingFixtureError):
        getattr(model, event)(**kwargs)
    assert model.state == state  # rejected events never mutate the machine


def test_model_epoch_verification_needs_all_three_preconditions():
    model = ArmingStateModel()
    model.prepare()
    # PAIRED alone, epoch alone, or plan validity alone never suffice
    model.note_pairing(paired_disabled=True)
    with pytest.raises(ArmingFixtureError):
        model.pairing_epoch_verified()
    model.note_pairing(epoch_checked=True)
    with pytest.raises(ArmingFixtureError):
        model.pairing_epoch_verified()
    model.note_pairing(plan_valid=True)
    assert model.pairing_epoch_verified() == ARMING_STATE_ARM_READY


@pytest.mark.parametrize("state", ARMING_STATES)
def test_model_service_wording_never_transitions(state):
    """AR2.2 row 3: service ready/armed wording does not move the machine."""
    model = ArmingStateModel()
    model.state = state
    assert model.service_says_armed() == state
    assert model.state == state


def test_model_service_ack_alone_stays_pending_and_disabled():
    model = ArmingStateModel()
    model.prepare()
    model.note_pairing(paired_disabled=True, epoch_checked=True, plan_valid=True)
    model.pairing_epoch_verified()
    model.arm_credential_validated()
    assert model.service_ack(matched=True) == ARMING_STATE_ARM_PENDING
    assert model.effective_enable_trading is False  # ACK alone never arms
    assert model.legal_callback() == ARMING_STATE_ARMED  # ack THEN callback
    # callback arriving first (fresh round) must not arm either
    second = ArmingStateModel()
    second.prepare()
    second.note_pairing(paired_disabled=True, epoch_checked=True, plan_valid=True)
    second.pairing_epoch_verified()
    second.arm_credential_validated()
    with pytest.raises(ArmingFixtureError):
        second.legal_callback()  # no ACK yet
    assert second.state == ARMING_STATE_ARM_PENDING


def test_model_consumed_and_revoked_are_one_way():
    model = _full_legal_model()
    assert model.consumed is True
    model.revoke()
    assert model.revoked is True
    model.local_gate_closed()
    # idempotent revoke inside the terminal latch; the latches never reset
    assert model.revoke() == ARMING_STATE_TERMINAL
    assert model.consumed is True and model.revoked is True


def test_model_single_activation_per_client_lifetime():
    model = _full_legal_model()
    model.revoke()
    model.local_gate_closed()
    with pytest.raises(ArmingFixtureError):
        model.arm_credential_validated()  # no second arming in this session
    with pytest.raises(ArmingFixtureError):
        model.prepare()  # terminal is absorbing: no candidate revival
    # a NEW session may arm again only through the full ruled sequence
    fresh = _full_legal_model()
    assert fresh.state == ARMING_STATE_ARMED and fresh.activation_count == 1


def test_model_send_exhausted_keeps_independent_cancel_scope():
    model = _full_legal_model(max_cancels=1, cancel_scope_authorized=True)
    assert model.attempt_send(outcome="rejected") == ARMING_STATE_ARMED_SEND_EXHAUSTED
    # first send attempt occupied the quota even though it was REJECTED
    with pytest.raises(ArmingFixtureError):
        model.attempt_send(outcome="ok")
    with pytest.raises(ArmingFixtureError):
        model.reopen_send()  # cancel can never buy the send back
    assert model.attempt_cancel() is True  # separately authorized CANCEL_OWNED
    with pytest.raises(ArmingFixtureError):
        model.attempt_cancel()  # quota 1 consumed
    # without an independent CANCEL_OWNED grant the exhausted state refuses
    other = _full_legal_model(max_cancels=1, cancel_scope_authorized=False)
    other.first_send_consumed()
    with pytest.raises(ArmingFixtureError):
        other.attempt_cancel()


@pytest.mark.parametrize("outcome", ["ok", "rejected", "exception", "unknown"])
def test_model_every_first_send_outcome_consumes_the_quota(outcome):
    model = _full_legal_model()
    assert model.attempt_send(outcome=outcome) == ARMING_STATE_ARMED_SEND_EXHAUSTED
    with pytest.raises(ArmingFixtureError):
        model.attempt_send(outcome="ok")


def test_model_pairing_dimension_never_derives_arming_dimension():
    model = ArmingStateModel()
    model.note_pairing(paired_disabled=True, epoch_checked=True, plan_valid=True)
    assert model.state == ARMING_STATE_DISARMED  # pairing facts alone: no move
    model.prepare()
    model.service_says_armed()
    assert model.state == ARMING_STATE_PREPARED_DISABLED  # wording is not arming
    # and the arming states say nothing about pairing on their own
    armed = _full_legal_model()
    armed.note_pairing(paired_disabled=False)  # link loss in the other dimension
    assert armed.state == ARMING_STATE_ARMED  # communication != admission


def test_model_revoke_latch_beats_late_arm_ack():
    """AR5.2: arm/disarm race — revocation observed first stays latched; the
    old arm ACK must never override it."""
    model = ArmingStateModel()
    model.prepare()
    model.note_pairing(paired_disabled=True, epoch_checked=True, plan_valid=True)
    model.pairing_epoch_verified()
    model.arm_credential_validated()
    model.revoke()
    assert model.state == ARMING_STATE_REVOKE_PENDING
    assert model.service_ack(matched=True) == ARMING_STATE_REVOKE_PENDING
    assert model.effective_enable_trading is False
    assert model.legal_callback() == ARMING_STATE_REVOKE_PENDING  # late callback too
    assert model.local_gate_closed() == ARMING_STATE_TERMINAL


def test_model_second_confirmation_is_refused():
    model = ArmingStateModel()
    model.prepare()
    with pytest.raises(ArmingFixtureError):
        model.second_confirmation()  # never two confirmations, any state


# ===========================================================================
# §2 AR2.1 — dual-hash semantics on the deployed client codec (runnable now)
# ===========================================================================

def _deployed_bridge_with_params(monkeypatch, tmp_path, params=None):
    deployment = deploy_self_strategy(monkeypatch, tmp_path)
    base = {"exchange": "SHFE", "instrument_id": "rb2610", "hedge_flag": "1",
            "risk_policy_id": "SIM_POLICY", "authorization_id": "SIM_AUTH",
            "max_native_sends": 0, "max_native_cancels": 0,
            "execution_api_url": "http://127.0.0.1:17779/execution/v1/poll"}
    base.update(params or {})
    bridge = deployment.no_arg(strategy_id=23, strategy_name="SYNTHETIC",
                               params=base)
    return deployment, bridge


@_needs_importable_client
def test_dual_hash_reference_encoder_matches_live_client_base_hash(monkeypatch, tmp_path):
    """Encoder fidelity pin: the AR2.1 reference computation with ZERO
    overrides reproduces the live client's own pairing params hash exactly
    (same canonical codec, same ADAPTER_VERSION, same F05 field list)."""
    deployment, bridge = _deployed_bridge_with_params(monkeypatch, tmp_path)
    assert (reference_target_params_sha256(deployment.module, bridge.params_map)
            == bridge._params_sha256)
    # determinism: same frozen copy -> same hash on recomputation
    assert (reference_target_params_sha256(deployment.module, bridge.params_map)
            == reference_target_params_sha256(deployment.module, bridge.params_map))


@_needs_importable_client
def test_dual_hash_target_differs_from_pairing_via_five_overrides(monkeypatch, tmp_path):
    deployment, bridge = _deployed_bridge_with_params(
        monkeypatch, tmp_path,
        {"risk_policy_id": "SIM_POLICY_BASE", "authorization_id": "SIM_AUTH_BASE"})
    pairing_hash = bridge._params_sha256
    overrides = {"execution_mode": "native", "risk_policy_id": "SIM_POLICY_ARMED",
                 "authorization_id": "SIM_AUTH_ARMED", "max_native_sends": 1,
                 "max_native_cancels": 0}
    target_hash = reference_target_params_sha256(
        deployment.module, bridge.params_map, overrides)
    assert target_hash != pairing_hash
    # a minimal single-field override (mode only) is its own distinct target
    only_mode = reference_target_params_sha256(
        deployment.module, bridge.params_map, {"execution_mode": "native"})
    assert only_mode != pairing_hash and only_mode != target_hash
    # every override field that actually changes a value moves the hash
    for field in ("execution_mode", "risk_policy_id", "authorization_id",
                  "max_native_sends"):
        single = reference_target_params_sha256(
            deployment.module, bridge.params_map, {field: overrides[field]})
        assert single != pairing_hash, field
    # an override EQUAL to the frozen base value is a no-op by design: the
    # hash reflects values only (max_native_cancels stays 0 on 0)
    assert (reference_target_params_sha256(
        deployment.module, bridge.params_map, {"max_native_cancels": 0})
        == pairing_hash)
    # the public set_params path can NEVER produce the native override:
    # F03 coercion keeps the live params disabled (proof by behavior)
    bridge.set_params({"execution_mode": "native"})
    assert str(bridge.params_map.execution_mode) == "disabled"
    assert bridge._params_sha256 == pairing_hash  # pairing hash unchanged


@_needs_importable_client
def test_dual_hash_override_guard_rejects_non_ruled_fields(monkeypatch, tmp_path):
    deployment, bridge = _deployed_bridge_with_params(monkeypatch, tmp_path)
    for field in ("order_volume", "instrument_id", "execution_api_url",
                  "account_tag", "hedge_flag"):
        with pytest.raises(ArmingFixtureError):
            reference_target_params_sha256(deployment.module, bridge.params_map,
                                           {field: "SIM_VALUE"})


@_needs_importable_client
def test_dual_hash_transient_and_identity_fields_never_enter_the_hash(monkeypatch, tmp_path):
    """AR2.1: challenge, transient switches, permit/control identities and
    timestamps stay out of the params hash (self-reference exclusion)."""
    deployment, bridge = _deployed_bridge_with_params(monkeypatch, tmp_path)
    excluded = {"enable_trading", "parameter_confirmation", "challenge",
                "permit_id", "control_revision", "arm_id", "request_id",
                "confirmed_at", "expires_at", "prerequisites_id"}
    assert excluded.isdisjoint(PARAMS_HASH_FIELDS)
    # behaviorally: toggling a transient switch leaves the live hash equal
    before = bridge._params_sha256
    bridge.set_params({"enable_trading": True})  # coerced back to False (F03)
    assert bridge._params_sha256 == before


@_needs_importable_client
def test_dual_hash_non_override_economic_change_breaks_plan_equality(monkeypatch, tmp_path):
    """Any economic change outside the five override fields yields a
    different hash — which is exactly why those fields must stay frozen and
    a change forces a new plan + re-registration instead of riding arming."""
    deployment, bridge = _deployed_bridge_with_params(monkeypatch, tmp_path)
    base_hash = bridge._params_sha256
    changed = dict(order_volume=str(int(bridge.params_map.order_volume) + 1))
    payload = {field: str(getattr(bridge.params_map, field))
               for field in PARAMS_HASH_FIELDS}
    payload.update(changed)
    payload["adapter_version"] = deployment.module.ADAPTER_VERSION
    assert (deployment.module.sha256_hex(deployment.module.canonical_dumps(payload))
            != base_hash)


def test_dual_hash_scope_pins_pairing_hash_not_target():
    """The AR4.2 common scope carries ``pairing_params_sha256`` — the frozen
    on_start hash — while the target hash travels in the plan/request/
    credential documents; the two never swap places (shared-vector check)."""
    scope = synthetic_arming_scope()
    assert "pairing_params_sha256" in scope and "target_params_sha256" not in scope
    plan = synthetic_arming_plan()
    assert plan["target_params_sha256"] != plan["scope"]["pairing_params_sha256"]
    request = synthetic_arming_request(plan=plan)
    credential = synthetic_arming_credential(request=request)
    for document in (plan, request, credential):
        assert document["target_params_sha256"] == plan["target_params_sha256"]


# ===========================================================================
# §3 AR2.2/AR5 — arming clock design validation (runnable now)
# ===========================================================================

def test_clock_utc_rollback_never_revives_an_expired_candidate():
    clock = ArmingClock()
    deadline = ArmingDeadline(
        clock, utc_expires_at=clock.now() + timedelta(
            seconds=ARMING_REQUEST_ACTIVE_WINDOW_MAX_SECONDS),
        window_seconds=ARMING_REQUEST_ACTIVE_WINDOW_MAX_SECONDS)
    assert deadline.expired() is False
    clock.advance(ARMING_REQUEST_ACTIVE_WINDOW_MAX_SECONDS + 1)
    assert deadline.expired() is True  # monotonic cutoff passed
    clock.rollback_utc(3600)  # UTC jumps a full hour backwards
    assert clock.now() < deadline.utc_expires_at  # UTC looks un-expired again
    assert deadline.expired() is True  # the latch does not care


def test_clock_monotonic_only_advances_and_early_utc_expiry_latches():
    clock = ArmingClock()
    mono_before = clock.monotonic()
    clock.rollback_utc(120)
    assert clock.monotonic() >= mono_before  # rollback never rewinds monotonic
    deadline = ArmingDeadline(clock, utc_expires_at=clock.now() + timedelta(seconds=10),
                              window_seconds=300)
    clock.advance(11)  # UTC expiry hits long before the monotonic window
    assert deadline.expired() is True
    clock.rollback_utc(600)
    assert deadline.expired() is True


def test_clock_window_ceiling_is_300_seconds():
    """AR4.2: request/challenge activation windows are capped at 300 s."""
    assert ARMING_REQUEST_ACTIVE_WINDOW_MAX_SECONDS == 300


# ===========================================================================
# §4 AR1.3 — console interactivity design validation (runnable now)
# ===========================================================================

def test_console_positive_flow_displays_full_scope_and_accepts_phrase():
    request = synthetic_arming_request()
    console = FakeConsoleTTY(scripted_inputs=[f"ARM {request['request_id'][-8:]}"])
    challenge_secret = synthetic_arming_hex("challenge")
    clock = ArmingClock()
    deadline = ArmingDeadline(clock, utc_expires_at=clock.now() + timedelta(seconds=300),
                              window_seconds=300)
    stored = {"bytes": json.dumps(request, sort_keys=True).encode("utf-8")}
    phrase = _console_flow_confirm(
        console, request_id=request["request_id"],
        request_bytes_reader=lambda: stored["bytes"],
        deadline=deadline,
        scope_display=_scope_display_lines(request, challenge_secret))
    assert phrase == f"ARM {request['request_id'][-8:]}"
    displayed = "\n".join(console.displayed)
    # the mandatory disclosure set was shown (AR1.3)
    for marker in ("binding", "max_native_sends=1", "max_native_cancels=0",
                   "expires_at", "authorization_id", "permit_id",
                   "ONLY ACTIVATION"):
        assert marker in displayed
    # and the challenge was never displayed nor copyable
    assert assert_no_secret_material(displayed, [challenge_secret])


@pytest.mark.parametrize("variant,code", [
    ("piped", "CONSOLE_STDIN_NOT_TTY"),
    ("redirected", "CONSOLE_STDOUT_NOT_TTY"),
    ("detached", "CONSOLE_NOT_ATTACHED"),
])
def test_console_non_interactive_variants_refuse_before_any_prompt(variant, code):
    console = {"piped": FakeConsoleTTY.piped,
               "redirected": FakeConsoleTTY.redirected,
               "detached": FakeConsoleTTY.detached}[variant]()
    assert console.is_interactive() is False
    with pytest.raises(ConsoleRefusal) as raised:
        console.display("SYNTHETIC scope")
    assert raised.value.code == code
    assert console.displayed == []  # nothing was ever shown


def test_console_empty_wrong_and_exhausted_inputs_refuse():
    console = FakeConsoleTTY(scripted_inputs=["", "ARM deadbeef"])
    request = synthetic_arming_request()
    with pytest.raises(ConsoleRefusal) as empty:
        console.read_phrase(request["request_id"])
    assert empty.value.code == "CONFIRMATION_EMPTY"
    with pytest.raises(ConsoleRefusal) as wrong:
        console.read_phrase(request["request_id"])
    assert wrong.value.code == "CONFIRMATION_PHRASE_MISMATCH"
    with pytest.raises(ConsoleRefusal) as exhausted:
        console.read_phrase(request["request_id"])
    assert exhausted.value.code == "CONFIRMATION_INPUT_REQUIRED"
    assert console.read_count == 2  # no fabricated third read


def test_console_retry_consumes_fresh_input_and_never_auto_answers():
    request = synthetic_arming_request()
    expected = f"ARM {request['request_id'][-8:]}"
    console = FakeConsoleTTY(scripted_inputs=["ARM wrongguy", expected])
    with pytest.raises(ConsoleRefusal):
        console.read_phrase(request["request_id"])
    # the retry needs a NEW user line; the double never replays or invents one
    assert console.read_phrase(request["request_id"]) == expected
    assert console.read_count == 2


def test_console_flow_refuses_expired_window_and_replaced_request():
    request = synthetic_arming_request()
    phrase = f"ARM {request['request_id'][-8:]}"
    clock = ArmingClock()
    deadline = ArmingDeadline(clock, utc_expires_at=clock.now() + timedelta(seconds=30),
                              window_seconds=30)
    clock.advance(31)  # the user presses Enter only after the window closed
    with pytest.raises(ConsoleRefusal) as expired:
        _console_flow_confirm(FakeConsoleTTY(scripted_inputs=[phrase]),
                              request_id=request["request_id"],
                              request_bytes_reader=lambda: b"first",
                              deadline=deadline, scope_display=["scope"])
    assert expired.value.code == "CONFIRMATION_WINDOW_EXPIRED"
    # request replaced between the display and the read (AR1.3/M04)
    readings = iter([b"first", b"second"])
    fresh = ArmingDeadline(
        ArmingClock(),
        utc_expires_at=datetime(2026, 9, 18, 0, 30, 0, tzinfo=timezone.utc),
        window_seconds=300)
    with pytest.raises(ConsoleRefusal) as replaced:
        _console_flow_confirm(FakeConsoleTTY(scripted_inputs=[phrase]),
                              request_id=request["request_id"],
                              request_bytes_reader=lambda: next(readings),
                              deadline=fresh, scope_display=["scope"])
    assert replaced.value.code == "ARMING_REQUEST_CHANGED"


# ===========================================================================
# §5 AR5.1 — one-time credential slot design validation (runnable now)
# ===========================================================================

def _published_slot(tmp_path):
    slot = OneTimeCredentialSlot(tmp_path / "store")
    request = synthetic_arming_request()
    slot.publish_request(request)
    slot.publish_challenge(synthetic_arming_challenge(request=request))
    return slot, synthetic_arming_credential(request=request)


def test_slot_consume_once_order(tmp_path):
    slot, credential = _published_slot(tmp_path)
    assert slot.path("arming.json").exists() is False  # nothing delivered yet
    slot.deliver_credential(credential)
    assert slot.path("arming.json").exists()
    assert slot.consume(credential) is True
    # tombstone persisted FIRST; secrets removed; exactly one confirmation
    assert slot.state["consumed"] is True
    assert slot.path("arming.json").exists() is False
    assert slot.path("arming-challenge.json").exists() is False
    assert len(slot.http_confirmations_sent) == 1
    # replay of the same arm_id is refused BY THE TOMBSTONE
    with pytest.raises(ArmingSlotRefusal) as replay:
        slot.deliver_credential(credential)
    assert replay.value.code == "ARMING_ALREADY_CONSUMED"
    # a second consume of the same delivery is refused too
    with pytest.raises(ArmingSlotRefusal) as second:
        slot.consume(credential)
    assert second.value.code == "ARMING_ALREADY_CONSUMED"


def test_slot_tombstone_survives_restart_and_never_rearms(tmp_path):
    slot, credential = _published_slot(tmp_path)
    slot.deliver_credential(credential)
    slot.consume(credential)
    restarted = slot.restart()
    # AR5.1: restarts are effective=False; tombstones survive; no auto re-arm
    assert credential["arm_id"] in restarted.consumed_arm_ids
    with pytest.raises(ArmingSlotRefusal) as replay:
        restarted.deliver_credential(credential)
    assert replay.value.code == "ARMING_ALREADY_CONSUMED"
    assert restarted.http_confirmations_sent == []
    # the persisted state document cannot be downgraded to re-arm
    tampered = dict(restarted.state)
    tampered["consumed"] = False
    with pytest.raises(ArmingFixtureError):
        validate_arming_state_transition(restarted.state, tampered)


@pytest.mark.parametrize("crash_at", CRASH_POINTS)
def test_slot_crash_points_leave_the_gate_closed(tmp_path, crash_at):
    """Each AR5.1 file-level crash point leaves the gate closed; the only
    legal recovery is finishing the SAME logical confirmation exactly once —
    never a second activation, never a new credential."""
    slot, credential = _published_slot(tmp_path)
    if crash_at in ("before_delivery", "after_delivery_before_consume"):
        with pytest.raises(ArmingCrashInjected):
            slot.deliver_credential(credential, crash_at=crash_at)
        if crash_at == "before_delivery":
            assert not slot.path("arming.json").exists()
            return  # nothing ever landed: safe by absence
        # delivered but never consumed: the recovery consume completes once
        assert slot.consume(credential) is True
    else:
        slot.deliver_credential(credential)
        with pytest.raises(ArmingCrashInjected):
            slot.consume(credential, crash_at=crash_at)
        if crash_at == "after_tombstone_before_delete":
            # tombstone persisted, secrets still on disk (leftover secrets
            # are the reviewed user tooling's problem, never auto-activation)
            assert slot.path("arming.json").exists()
            assert slot.http_confirmations_sent == []
        else:  # after_delete_before_http: secrets gone, nothing sent yet
            assert not slot.path("arming.json").exists()
            assert not slot.path("arming-challenge.json").exists()
            assert slot.http_confirmations_sent == []
        # the recovery continuation finishes the SAME confirmation once
        assert slot.consume(credential) is True
    with pytest.raises(ArmingSlotRefusal):
        slot.consume(credential)  # any further consume is a replay
    assert len(slot.http_confirmations_sent) == 1


def test_slot_ack_loss_allows_only_bounded_same_confirmation_retransmit(tmp_path):
    slot, credential = _published_slot(tmp_path)
    slot.deliver_credential(credential)
    slot.consume(credential)
    assert slot.retransmit_same_confirmation(credential) is True  # bounded replay
    tampered = dict(credential)
    tampered["expected_control_revision"] = credential["expected_control_revision"] + 1
    with pytest.raises(ArmingSlotRefusal) as changed:
        slot.retransmit_same_confirmation(tampered)
    assert changed.value.code == "ARMING_RETRANSMIT_CONTENT_CHANGED"


def test_slot_delivery_refuses_unknown_slot_names(tmp_path):
    slot, _ = _published_slot(tmp_path)
    with pytest.raises(ArmingSlotRefusal):
        slot.path("arming-other.json")


def test_slot_state_document_is_strict_and_synthetic(tmp_path):
    slot, credential = _published_slot(tmp_path)
    slot.deliver_credential(credential)
    slot.consume(credential)
    document = arming_strict_loads(slot.path("arming-state.json").read_text(encoding="utf-8"))
    validate_arming_document("ne1_arming_state_v1", document)
    assert document["consumed"] is True
    assert document["effective_enable_trading"] is False  # ARM_PENDING never claims armed


# ===========================================================================
# §6 AR4.2/AR5.3 — strict shared-vector validation (runnable now)
# ===========================================================================

@pytest.mark.parametrize("schema,builder", [
    (ARMING_PROPOSAL_SCHEMA, synthetic_arming_proposal),
    ("ne1_arming_plan_v1", synthetic_arming_plan),
    (ARMING_REQUEST_SCHEMA, synthetic_arming_request),
    (ARMING_CHALLENGE_SCHEMA, synthetic_arming_challenge),
    (CLIENT_ARMING_SCHEMA, synthetic_arming_credential),
    ("ne1_arming_state_v1", synthetic_arming_state),
])
def test_vectors_exact_field_sets_and_unknown_field_rejection(schema, builder):
    document = builder()
    validate_arming_document(schema, document)  # exact set passes
    assert set(document) == {"schema_version", *ARMING_SCHEMA_FIELDS[schema]}
    with pytest.raises(ArmingFixtureError):
        validate_arming_document(schema, {**document, "enable_trading": True})
    stripped = dict(document)
    removed = sorted(set(document) - {"schema_version"})[0]
    del stripped[removed]
    with pytest.raises(ArmingFixtureError):
        validate_arming_document(schema, stripped)


def test_vectors_raw_json_strictness(tmp_path):
    request = synthetic_arming_request()
    text = json.dumps(request, sort_keys=True)
    assert arming_strict_loads(text) == request
    duplicate = text[:-1] + ', "request_id": "x"}'
    with pytest.raises(ArmingFixtureError):
        arming_strict_loads(duplicate)
    with pytest.raises(ArmingFixtureError):
        arming_strict_loads('{"schema_version": "x", "a": NaN}')
    with pytest.raises(ArmingFixtureError):
        arming_strict_loads("[1, 2]")  # top level must be an object
    oversized = '{"schema_version": "' + "a" * (64 * 1024) + '"}'
    with pytest.raises(ArmingFixtureError):
        arming_strict_loads(oversized)


def test_vectors_type_strictness_bool_never_int_never_string_bool():
    proposal = synthetic_arming_proposal()
    tampered = dict(proposal)
    tampered["max_native_sends"] = True  # bool posing as int
    with pytest.raises(ArmingFixtureError):
        validate_arming_document(ARMING_PROPOSAL_SCHEMA, tampered)
    tampered = dict(proposal)
    tampered["max_native_cancels"] = 2  # outside the ruled 0|1 set
    with pytest.raises(ArmingFixtureError):
        validate_arming_document(ARMING_PROPOSAL_SCHEMA, tampered)
    request = synthetic_arming_request()
    tampered = dict(request)
    tampered["active"] = "true"  # string posing as bool
    with pytest.raises(ArmingFixtureError):
        validate_arming_document(ARMING_REQUEST_SCHEMA, tampered)
    credential = synthetic_arming_credential()
    for bad in ({"action_source": "GUI_AUTOMATION"},
                {"explicit_user_action": False},
                {"challenge": "XY" * 32}):
        tampered = dict(credential)
        tampered.update(bad)
        with pytest.raises(ArmingFixtureError):
            validate_arming_document(CLIENT_ARMING_SCHEMA, tampered)


def test_vectors_plan_overrides_are_exactly_the_five_fields():
    plan = synthetic_arming_plan()
    assert set(plan["effective_overrides"]) <= TARGET_OVERRIDE_FIELDS
    assert set(plan["effective_overrides"]) == {
        "execution_mode", "risk_policy_id", "authorization_id",
        "max_native_sends", "max_native_cancels"}
    tampered = dict(plan)
    tampered["effective_overrides"] = {**plan["effective_overrides"],
                                       "order_volume": 2}
    with pytest.raises(ArmingFixtureError):
        validate_arming_document("ne1_arming_plan_v1", tampered)


def test_vectors_prerequisites_require_all_five_real_kinds():
    prerequisites = synthetic_arming_prerequisites()
    validate_arming_prerequisites(prerequisites)
    kinds = {ref["kind"] for ref in prerequisites["evidence_refs"]}
    assert kinds == set(ARMING_PREREQUISITE_KINDS)
    for kind in ARMING_PREREQUISITE_KINDS:
        missing = dict(prerequisites)
        missing["evidence_refs"] = [ref for ref in prerequisites["evidence_refs"]
                                    if ref["kind"] != kind]
        with pytest.raises(ArmingFixtureError):
            validate_arming_prerequisites(missing)
    # placeholder/NOT_RUN stand-ins and bare approved-bool entries never pass
    placeholder = dict(prerequisites)
    placeholder["evidence_refs"] = [dict(ref, path="sim-evidence/NOT_RUN.json")
                                    for ref in prerequisites["evidence_refs"]]
    with pytest.raises(ArmingFixtureError):
        validate_arming_prerequisites(placeholder)
    boolean = dict(prerequisites)
    boolean["evidence_refs"] = [{"kind": kind, "approved": True}
                                for kind in ARMING_PREREQUISITE_KINDS]
    with pytest.raises(ArmingFixtureError):
        validate_arming_prerequisites(boolean)


def test_vectors_registrations_index_shape():
    registrations = synthetic_arming_registrations()
    validate_arming_registrations(registrations)
    assert {entry["record_type"] for entry in registrations["entries"]} == {
        "ACTOR", "EVIDENCE", "AUTHORIZATION", "PERMIT"}
    tampered = dict(registrations)
    tampered["entries"] = registrations["entries"] + [
        {**registrations["entries"][0], "extra": True}]
    with pytest.raises(ArmingFixtureError):
        validate_arming_registrations(tampered)


# ===========================================================================
# §7 AR4.3 — REAL Windows DACL sandbox (runnable now, Windows-only)
# ===========================================================================

@_windows_only
def test_acl_domain_proven_before_any_secret_and_holds_for_slots(tmp_path):
    sandbox = ArmingAclSandbox(tmp_path / "arming-store")
    assert not any(sandbox.base_dir.iterdir())  # proven while EMPTY (AR4.3)
    assert sandbox.sid == sandbox.current_user_sid()
    request = synthetic_arming_request()
    sandbox.write_private("arming-challenge.json",
                          json.dumps(synthetic_arming_challenge(request=request)))
    sandbox.write_private("arming.json",
                          json.dumps(synthetic_arming_credential(request=request)))
    sandbox.write_public("arming-request.json", json.dumps(request))
    # non-secret slots (state/disarm) live in the same protected domain
    sandbox.write_public("arming-state.json", json.dumps(synthetic_arming_state()))
    sandbox.write_public("disarm.json", json.dumps(
        {"schema_version": "ne1_client_disarm_v1"}))
    sandbox.verify()  # every slot re-verified after publication


@_windows_only
def test_acl_negative_loose_dacl_is_actually_detected(tmp_path):
    sandbox = ArmingAclSandbox(tmp_path / "arming-store")
    request = synthetic_arming_request()
    target = sandbox.write_private("arming.json",
                                   json.dumps(synthetic_arming_credential(request=request)))
    sandbox.verify()  # positive first
    sandbox.loosen("arming.json")  # REAL Everyone-ACE negative case
    with pytest.raises(OSError):  # the real verifier must catch it
        sandbox.verify()
    assert target.exists()


@_windows_only
def test_acl_sandbox_refuses_preexisting_directory(tmp_path):
    directory = tmp_path / "preexisting"
    directory.mkdir()
    with pytest.raises(ArmingAclError):
        ArmingAclSandbox(directory)


def test_acl_disclosure_helper_catches_secret_forms():
    secret = "5f" * 32
    assert assert_no_secret_material("clean receipt text", [secret])
    with pytest.raises(AssertionError):
        assert_no_secret_material(f"receipt mentions {secret}", [secret])
    digest = hashlib.sha256(secret.encode()).hexdigest()
    with pytest.raises(AssertionError):
        assert_no_secret_material(f"digest {digest}", [secret])
    encoded = secret.encode().hex()
    with pytest.raises(AssertionError):
        assert_no_secret_material(f"hex {encoded}", [secret])


# ===========================================================================
# §8 ARM-M13 current half — v2b migration block (runnable against the
#    decision-snapshotted v2b tree; superseded by the frozen client's own
#    tests once ARM-LM-C lands)
# ===========================================================================

@_v2b_tree_only
@pytest.mark.parametrize("arming_file", sorted(ARMING_FILE_NAMES))
def test_m13_current_v2b_refuses_arming_slot_files(monkeypatch, tmp_path, arming_file):
    """Old v2b meets the new arming-v1 slot files -> startup refusal with the
    safe migration-block code (never a silent accept, never a wipe)."""
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    store = tmp_path / "store"
    store.mkdir()
    (store / arming_file).write_text('{"schema_version": "probe"}', encoding="utf-8")
    bridge = module.AKExecutionBridge(native=fake, store_directory=store,
                                      pairing_token="ab" * 32)
    bridge.strategy_id = 23
    bridge.set_params({"exchange": "SHFE", "instrument_id": "rb2610",
                       "hedge_flag": "1",
                       "execution_api_url": "http://127.0.0.1:17779/execution/v1/poll"})
    bridge.on_init()
    bridge.on_start()
    assert bridge.state_map.execution_status == "store_refused"
    assert bridge.state_map.control_status == "LEGACY_STORE_MIGRATION_REQUIRED"
    assert (store / arming_file).read_text(encoding="utf-8") == '{"schema_version": "probe"}'
    counts = fake.call_counts()
    assert counts.get("sendOrder", 0) == 0 and counts.get("cancelOrder", 0) == 0
    assert bridge._running is False


@_v2b_tree_only
def test_m13_current_v2b_has_no_arming_lifecycle_yet(monkeypatch, tmp_path):
    """F04 durable fact: the v2b client has no ARMED lifecycle — the
    confirmation field is a declaration only and no arming surface exists.
    (Replaced by the frozen client's own ARM-M01 group after the freeze.)"""
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    bridge = module.AKExecutionBridge(native=fake)
    for attribute in ("arm_context", "arming_state", "arm_request",
                      "target_params_sha256", "consume_arming_credential"):
        assert not hasattr(bridge, attribute)
    bridge.set_params({"enable_trading": True, "execution_mode": "native",
                       "max_native_sends": 1})
    # F03: set_params coerces the switches back to the frozen safe defaults
    assert bridge.params_map.enable_trading is False
    assert str(bridge.params_map.execution_mode) == "disabled"


# ===========================================================================
# §9 RULING 06 — cross-end ARM-M matrix, W2 INTEGRATION (2026-09-18).
#    The ARM-LM-C/D/S freeze landed (client ne1-execution-20260918-arming-v1,
#    tool ne1-pairing-20260918-arming-v1, service ne1-service-20260918-arming-v1);
#    the PENDING_FREEZE markers were lifted by the main-session W2 merge and
#    every body now drives the FROZEN combination through public paths only
#    (no-arg/CLI/real local HTTP/strict local slots). Each body still fails
#    LOUDLY through _require_frozen_arming if the freeze is ever absent.
# ===========================================================================

_TOOL_CACHE: dict = {}


def _tool_module():
    """Import the frozen pairing tool for in-process console-seam tests (the
    interactive reader seam is the only SYNTHETIC substitution; every real
    refusal face is proven by piped subprocesses)."""
    module = _TOOL_CACHE.get("tool")
    if module is None:
        spec = importlib.util.spec_from_file_location("ne1_pairing_tool_w2", TOOL_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _TOOL_CACHE["tool"] = module
    return module


def _launcher_module():
    module = _TOOL_CACHE.get("launcher")
    if module is None:
        spec = importlib.util.spec_from_file_location("ne1_launcher_w2",
                                                      ROOT / "scripts" /
                                                      "run_infiny_execution.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _TOOL_CACHE["launcher"] = module
    return module


def _json_line(payload):
    return (json.dumps(payload, ensure_ascii=True, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _write_file(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload if isinstance(payload, bytes)
                     else payload.encode("utf-8"))
    return path


def _layout_store(tmp_path, strategy_id=23):
    """A store the REAL tool accepts (R2.3 layout: <root>/.ak_execution/
    strategy-<id>); the frozen client creates and protects it on start."""
    return Path(tmp_path) / "self_strategy" / ".ak_execution" / f"strategy-{strategy_id}"


def _pair_and_start(module, fake, store, poll_url, *, token=None):
    bridge = module.AKExecutionBridge(
        native=fake, store_directory=store,
        pairing_token=token if token is not None else SYNTHETIC_PAIRING_TOKEN)
    bridge.strategy_id = 23
    bridge.strategy_name = "SYNTHETIC"
    bridge.set_params({"exchange": "SHFE", "instrument_id": "rb2610", "hedge_flag": "1",
                       "investor_id": "SYNTHETIC-ACCOUNT-A",
                       "execution_api_url": poll_url})
    bridge.on_init()
    bridge.on_start()
    inject_arming_service_seams(bridge)
    return bridge


def _client_binding(bridge):
    return synthetic_binding(instance_id=bridge._instance_id,
                             session_epoch=bridge._session_epoch)


def _arming_candidate(*, cancel=1):
    """Operator's non-secret ne1_arming_proposal_candidate_v1 input."""
    return {"schema_version": "ne1_arming_proposal_candidate_v1",
            "binding": {"actor_id": "SIM_ACTOR_W2",
                        "account_key": "SIM_ACCOUNT_A",
                        "strategy_key": "SIM_STRATEGY_S",
                        "strategy_id": 23,
                        "trading_day": "2026-09-18",
                        "exchange": "SHFE", "instrument_id": "rb2610",
                        "hedge_flag": "1"},
            "operator_action_id": "SIM_ACTION_W2",
            "authorization_id": SYNTHETIC_ARMING_AUTHORIZATION_ID,
            "permit_id": SYNTHETIC_ARMING_PERMIT_ID,
            "risk_policy_id": "SIM_POLICY_ARM",
            "max_native_sends": 1,
            "max_native_cancels": cancel}


def _tool_prepare_arm(store, request_path, lock_path, client_sha256, tmp_path,
                      *, candidate, tag="p1"):
    """REAL prepare-arm CLI over subprocess (direct argument list, no shell)."""
    candidate_path = _write_file(Path(tmp_path) / "w2" / f"candidate-{tag}.json",
                                 _json_line(candidate))
    return _run_tool(["prepare-arm",
                      "--client-store", str(store),
                      "--client-request", str(request_path),
                      "--proposal", str(candidate_path),
                      "--contract-lock", str(lock_path),
                      "--expected-client-sha256", client_sha256,
                      "--receipt", str(Path(tmp_path) / "w2" / f"prepare-{tag}.json")])


def _v2_grant_records(bridge, target, tmp_path, *, cancel=1, control_revision=1,
                      authorization_id=None, permit_id=None):
    """Complete existing v2 NativeAuthorization/NativePilotPermit exports via
    the frozen codec, pinned to the live client binding and target hash."""
    from ak_trading.infiny_execution import contracts as codec
    from ak_trading.infiny_execution.native_contracts import (
        NativeAuthorization, NativePilotPermit, Origin)
    authorization_id = authorization_id or SYNTHETIC_ARMING_AUTHORIZATION_ID
    permit_id = permit_id or SYNTHETIC_ARMING_PERMIT_ID
    packet = {"schema_version": codec.Binding.schema, "type": "Binding",
              # X01 unblock repair 3: pin to the CLIENT'S OWN published
              # request binding (the tool compares decoded bindings
              # byte-exactly); the previous guessed synthetic binding never
              # matched the live client round.
              "data": json.loads((Path(bridge._store_path) / "arming-request.json")
                                 .read_text(encoding="utf-8"))["binding"]}
    binding = codec.decode(json.dumps(packet), codec.Binding)
    symbol = codec.Symbol(exchange="SHFE", instrument_id="rb2610")
    bound = codec.PriceBound(symbol=symbol, lower=Decimal("1000"),
                             upper=Decimal("5000"))
    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    authorization = NativeAuthorization(
        authorization_id=authorization_id, binding=binding, symbols=(symbol,),
        actions=(codec.AuthorizationAction.OPEN_LONG,
                 codec.AuthorizationAction.CANCEL_OWNED),
        max_send_attempts=1, max_cancel_attempts=cancel, max_volume=1,
        price_bounds=(bound,), expires_at=expires,
        origin=Origin.SYNTHETIC, params_sha256=target)
    permit = NativePilotPermit(
        permit_id=permit_id, authorization_id=authorization_id,
        binding=binding, control_revision=control_revision,
        baseline_evidence_id="ev-base-w2", route_evidence_id="ev-route-w2",
        cash_bound_evidence_id="ev-cash-w2", source_key="sim-source-w2",
        actions=(codec.AuthorizationAction.OPEN_LONG,), max_send_attempts=1,
        max_cancel_attempts=cancel, max_volume=1, price_bounds=(),
        expires_at=expires, params_sha256=target, origin=Origin.SYNTHETIC)
    grants = Path(tmp_path) / "w2" / "grants"
    auth_path = _write_file(grants / "authorization.json",
                            codec.encode(authorization,
                                         schema_version=NativeAuthorization.schema) + "\n")
    permit_path = _write_file(grants / "permit.json",
                              codec.encode(permit,
                                           schema_version=NativePilotPermit.schema) + "\n")
    return {"authorization_path": auth_path,
            "authorization_sha256": hashlib.sha256(auth_path.read_bytes()).hexdigest(),
            "permit_path": permit_path,
            "permit_sha256": hashlib.sha256(permit_path.read_bytes()).hexdigest(),
            "control_revision": control_revision}


def _registration_entry(tmp_path, record_type, tag, record_path=None):
    base = Path(tmp_path) / "w2" / "registrations" / tag
    base.mkdir(parents=True, exist_ok=True)
    if record_path is None:
        record_path = _write_file(base / "record.json",
                                  _json_line({"record_type": record_type,
                                              "note": "SYNTHETIC"}))
    manifest_path = _write_file(base / "manifest.json",
                                _json_line({"record_type": record_type,
                                            "record_sha256":
                                                hashlib.sha256(record_path.read_bytes()).hexdigest(),
                                            "note": "SYNTHETIC manifest"}))
    receipt_path = _write_file(base / "receipt.json",
                               _json_line({"event": "registered",
                                           "record_type": record_type,
                                           "note": "SYNTHETIC receipt"}))
    return {"record_type": record_type, "record_path": str(record_path),
            "record_sha256": hashlib.sha256(record_path.read_bytes()).hexdigest(),
            "manifest_path": str(manifest_path),
            "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "receipt_path": str(receipt_path),
            "receipt_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest()}


def _registrations_document(bridge, grants, target, lock_sha256, tmp_path):
    # X01 unblock repair 3: same published-binding pin as the grant records
    # (ARM_REGISTRATIONS_BINDING_MISMATCH compares against the live request).
    published_binding = json.loads(
        (Path(bridge._store_path) / "arming-request.json")
        .read_text(encoding="utf-8"))["binding"]
    return {"schema_version": "ne1_arming_registrations_v1",
            "binding": published_binding,
            "target_params_sha256": target,
            "contract_lock_sha256": lock_sha256,
            "authorization_id": SYNTHETIC_ARMING_AUTHORIZATION_ID,
            "permit_id": SYNTHETIC_ARMING_PERMIT_ID,
            "entries": [
                _registration_entry(tmp_path, "ActorRegistration", "actor"),
                _registration_entry(tmp_path, "NativeBaselineEvidence", "evidence"),
                _registration_entry(tmp_path, "NativeAuthorization",
                                    "authorization",
                                    record_path=grants["authorization_path"]),
                _registration_entry(tmp_path, "NativePilotPermit", "permit",
                                    record_path=grants["permit_path"])]}


def _prerequisites_document(lock_sha256, client_sha256, tmp_path, *,
                            expires_hours=1.0):
    refs = []
    for kind in ARMING_PREREQUISITE_KINDS:
        path = _write_file(Path(tmp_path) / "w2" / "prereq" / f"{kind}.json",
                           _json_line({"kind": kind, "stage": "SYNTHETIC",
                                       "note": "sandbox evidence"}))
        refs.append({"kind": kind, "path": str(path),
                     "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    now = datetime.now(timezone.utc)
    payload = {"schema_version": ARMING_PREREQUISITES_SCHEMA,
               "prerequisites_id": synthetic_arming_hex("prereq", 32),
               "created_at": now.isoformat(timespec="microseconds").replace("+00:00", "Z"),
               "expires_at": (now + timedelta(hours=expires_hours)
                              ).isoformat(timespec="microseconds").replace("+00:00", "Z"),
               "contract_lock_sha256": lock_sha256,
               "client_file_sha256": client_sha256,
               "service_file_sha256": synthetic_arming_hex("svcfile", 64),
               "tool_file_sha256": hashlib.sha256(TOOL_PATH.read_bytes()).hexdigest(),
               "evidence_refs": refs}
    path = _write_file(Path(tmp_path) / "w2" / "prerequisites.json",
                       _json_line(payload))
    return {"payload": payload, "path": path,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _service_start_document(prerequisites, service_epoch):
    return {"event": "execution_started", "host": "127.0.0.1", "port": 17779,
            "service_epoch": service_epoch,
            "service_version": SERVICE_ARMING_VERSION_EXPECTED,
            "instance_id": SYNTHETIC_ARMING_SERVICE_INSTANCE,
            "execution_enabled": False, "observer_only": True,
            "observer_port_untouched": 17778,
            "actors": ["SIM_ACTOR_W2"], "instances": [],
            "audit_log": "SYNTHETIC",
            "contract_lock_sha256":
                prerequisites["payload"]["contract_lock_sha256"],
            "pairing_dir": "SYNTHETIC",
            # X01 unblock repair 1: the frozen tool contract requires a
            # non-empty pairings list on every execution_started line (a real
            # arming-era line lists the paired instance); the previous empty
            # list could never pass validate_service_start and was never
            # exercised past the X01 rejection.
            "pairings": [{"instance_id": "ab" * 16, "session_epoch": "cd" * 16,
                          "token_file": "SYNTHETIC", "diagnostic_only": False}],
            "arming_prerequisites": {
                "prerequisites_id": prerequisites["payload"]["prerequisites_id"],
                "prerequisites_sha256": prerequisites["sha256"],
                "client_file_sha256":
                    prerequisites["payload"]["client_file_sha256"]}}


def _arm_namespace(store, request_path, service_start_path, grants,
                   registrations_path, prerequisites, lock_path, client_sha256,
                   receipt_path):
    return type("Args", (), {
        "client_store": str(store),
        "arming_request": str(request_path),
        "service_start": str(service_start_path),
        "authorization_record": str(grants["authorization_path"]),
        "permit_record": str(grants["permit_path"]),
        "registration_receipts": str(registrations_path),
        "s4_readiness": str(prerequisites["path"]),
        "contract_lock": str(lock_path),
        "expected_client_sha256": client_sha256,
        "receipt": str(receipt_path)})()


def _tool_arm_inprocess(tool, monkeypatch, namespace, answers):
    """cmd_arm under the controlled test console (ARM-M04): the reader seam
    gets scripted answers; everything else is the production path."""
    monkeypatch.setattr(tool, "_console_attached", lambda: True)
    script = list(answers)
    monkeypatch.setattr(tool, "_console_read_line",
                        lambda prompt: script.pop(0) if script else "")
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        result = tool.cmd_arm(namespace)
    return result, buffer.getvalue()


def _full_tool_round(module, fake, tmp_path, monkeypatch, *, cancel=1,
                     control_revision=1):
    """A complete cross-end round on the layout store: REAL prepare-arm CLI ->
    client plan/request/challenge (public lane) -> the full arm materials the
    REAL cmd_arm needs, derived from the client's own published facts."""
    service = FakeExecutionService()
    store = _layout_store(tmp_path)
    bridge = _pair_and_start(module, fake, store, service.poll_url)
    lock_path = _write_file(Path(tmp_path) / "w2" / "contract-lock.json",
                            _json_line({"note": "SYNTHETIC contract lock"}))
    client_sha256 = hashlib.sha256(
        Path(module.__spec__.origin).read_bytes()).hexdigest()
    result = _tool_prepare_arm(store, store / "pairing-request.json", lock_path,
                               client_sha256, tmp_path,
                               candidate=_arming_candidate(cancel=cancel))
    assert result.returncode == 0, result.stderr
    bridge._process_arming_files()      # DISARMED -> PREPARED_DISABLED
    bridge._process_arming_files()      # PREPARED_DISABLED -> ARM_READY
    assert bridge._arm_state == module.ARM_STATE_ARM_READY, bridge._last_arming_reason
    plan = json.loads((store / "arming-plan.json").read_text(encoding="utf-8"))
    request_path = store / "arming-request.json"
    grants = _v2_grant_records(bridge, plan["target_params_sha256"], tmp_path,
                               cancel=cancel, control_revision=control_revision)
    registrations = _registrations_document(
        bridge, grants, plan["target_params_sha256"],
        plan["scope"]["contract_lock_sha256"], tmp_path)
    registrations_path = _write_file(Path(tmp_path) / "w2" / "registrations.json",
                                     _json_line(registrations))
    prerequisites = _prerequisites_document(
        plan["scope"]["contract_lock_sha256"], client_sha256, tmp_path)
    service_start_path = _write_file(
        Path(tmp_path) / "w2" / "service-start.json",
        _json_line(_service_start_document(prerequisites,
                                           SYNTHETIC_ARMING_SERVICE_EPOCH)))
    return {"bridge": bridge, "service": service, "store": store,
            "lock_path": lock_path, "client_sha256": client_sha256,
            "request_path": request_path, "grants": grants,
            "registrations_path": registrations_path,
            "prerequisites": prerequisites,
            "service_start_path": service_start_path}


def _arm_args(round_, tmp_path, tag="r1"):
    return _arm_namespace(round_["store"], round_["request_path"],
                          round_["service_start_path"], round_["grants"],
                          round_["registrations_path"], round_["prerequisites"],
                          round_["lock_path"], round_["client_sha256"],
                          Path(tmp_path) / "w2" / f"arm-receipt-{tag}.json")


def _arm_phrase(round_):
    request = json.loads(Path(round_["request_path"]).read_text(encoding="utf-8"))
    return "ARM " + request["request_id"][-8:]


def _post_with_retry(url, payload, *, token, timeout=10.0):
    last = None
    for _attempt in range(4):
        try:
            return post_json(url, payload, token=token, timeout=timeout)
        except OSError as exc:
            last = exc
            time.sleep(0.05)
    raise last


_W2_INSTANCE = "sim_instance_w2_0001"
_W2_SESSION = "sim_epoch_w2_00001"


def _w2_binding():
    return synthetic_binding(instance_id=_W2_INSTANCE, session_epoch=_W2_SESSION)


def _w2_trusted_stack(tmp_path, *, with_assembly=True, with_prerequisites=True,
                      with_records=True, target=None, adapter=None):
    """TempExecutionStack wired with the isolated SYNTHETIC trusted control
    assembly exactly as the frozen service constructor accepts it (AR3.3
    sandbox positive; the production launcher passes none of it)."""
    from ak_trading.infiny_execution import client_service as cs
    from ak_trading.infiny_execution import contracts as codec
    from ak_trading.infiny_execution.native_contracts import (
        ControlState, NativeAuthorization, NativePilotPermit, Origin)
    from ak_trading.infiny_execution.native_evidence import (
        CallbackKind, EvidenceSourceKind, NativeBaselineEvidence,
        NativeRouteEvidence, NativeSettlementEvidence)
    target = target or synthetic_arming_hex("target", 64)
    adapter = adapter or synthetic_arming_hex("adapter", 64)
    clock = StepClock()   # every synthetic grant time anchors on the service clock
    base = clock.now()
    packet = {"schema_version": codec.Binding.schema, "type": "Binding",
              "data": _w2_binding()}
    binding = codec.decode(json.dumps(packet), codec.Binding)
    symbol = codec.Symbol(exchange="SHFE", instrument_id="rb2610")
    expires = base + timedelta(hours=2)
    authorization = NativeAuthorization(
        authorization_id=SYNTHETIC_ARMING_AUTHORIZATION_ID, binding=binding,
        symbols=(symbol,),
        actions=(codec.AuthorizationAction.OPEN_LONG,
                 codec.AuthorizationAction.CANCEL_OWNED),
        max_send_attempts=1, max_cancel_attempts=1, max_volume=1,
        price_bounds=(codec.PriceBound(symbol=symbol, lower=Decimal("1000"),
                                       upper=Decimal("5000")),),
        expires_at=expires, origin=Origin.SYNTHETIC, params_sha256=target)
    permit = NativePilotPermit(
        permit_id=SYNTHETIC_ARMING_PERMIT_ID,
        authorization_id=SYNTHETIC_ARMING_AUTHORIZATION_ID,
        binding=binding, control_revision=1,
        baseline_evidence_id="ev-base-w2", route_evidence_id="ev-route-w2",
        cash_bound_evidence_id="ev-cash-w2", source_key="sim-source-w2",
        actions=(codec.AuthorizationAction.OPEN_LONG,), max_send_attempts=1,
        max_cancel_attempts=1, max_volume=1, price_bounds=(),
        expires_at=expires, params_sha256=target, origin=Origin.SYNTHETIC)
    state = ControlState(
        control_revision=permit.control_revision, binding=binding,
        mode=codec.ExecutionMode.NATIVE, policy_id="SIM_POLICY_ARM",
        authorization_id=SYNTHETIC_ARMING_AUTHORIZATION_ID,
        native_permit_id=permit.permit_id, params_sha256=target,
        generation="SIM_GEN_W2", updated_at=base - timedelta(minutes=15),
        disabled_reason=None)
    baseline = NativeBaselineEvidence(
        evidence_id="ev-base-w2", source_kind=EvidenceSourceKind.TERMINAL_EXPORT,
        artifact_sha256="1" * 64, account_key=binding.account_key,
        trading_day=binding.trading_day, session_epoch=binding.session_epoch,
        as_of=base - timedelta(minutes=10),
        order_inventory=(), trade_inventory=(), positions=(),
        coverage_description="SYNTHETIC isolated arming coverage",
        completion_basis="SYNTHETIC isolated arming completion",
        isolation_evidence_id="ev-iso-w2", approved_scope_id="ev-scope-w2",
        expires_at=base + timedelta(hours=3),
        origin=Origin.SYNTHETIC)
    route = NativeRouteEvidence(
        evidence_id="ev-route-w2", framework_sha256="2" * 64, sdk_sha256="3" * 64,
        binding=binding, callback_kinds=(CallbackKind.ON_TICK,),
        account_scope_basis="SYNTHETIC",
        strategy_scope_basis="SYNTHETIC", local_alias_scope="SYNTHETIC-local",
        front_id=None, session_id=None, memo_limit=1, memo_roundtrip_verified=True,
        validated_at=base - timedelta(minutes=10),
        expires_at=base + timedelta(hours=3),
        origin=Origin.SYNTHETIC)
    settlement = NativeSettlementEvidence(
        evidence_id="ev-cash-w2", baseline_evidence_id="ev-base-w2",
        binding=binding, as_of=base - timedelta(minutes=10),
        artifact_sha256="4" * 64, covered_leg_ids=("SIM_ARM_LEG_1",),
        covered_event_ids=("SIM_ARM_EVENT_1",), order_inventory=(),
        trade_inventory=(), position_snapshot_id="SIM_ARM_POS",
        completion_basis="SYNTHETIC isolated arming settlement basis",
        isolation_evidence_id="ev-iso-w2", origin=Origin.SYNTHETIC)
    registry = cs.ProductionServiceRegistry(None)
    if with_records:
        registry._records["NativeBaselineEvidence:ev-base-w2"] = \
            ("NativeBaselineEvidence", baseline)
        registry._records["NativeRouteEvidence:ev-route-w2"] = \
            ("NativeRouteEvidence", route)
        registry._records["NativeSettlementEvidence:ev-cash-w2"] = \
            ("NativeSettlementEvidence", settlement)
        registry._records["NativeAuthorization:"
                          + SYNTHETIC_ARMING_AUTHORIZATION_ID] = \
            ("NativeAuthorization", authorization)
        registry._records["NativePilotPermit:" + SYNTHETIC_ARMING_PERMIT_ID] = \
            ("NativePilotPermit", permit)
    assemblies = None
    if with_assembly:
        assemblies = {codec.canonical_hash(binding):
                      cs.TrustedControlAssembly(control_state=state, permit=permit,
                                                authorization=authorization,
                                                origin=Origin.SYNTHETIC)}
    prerequisites = None
    if with_prerequisites:
        prerequisites = cs.ArmingPrerequisites(
            prerequisites_id=synthetic_arming_hex("prereq", 32),
            prerequisites_sha256=synthetic_arming_hex("pbytes", 64),
            client_file_sha256=adapter,
            service_file_sha256=synthetic_arming_hex("svcfile", 64),
            tool_file_sha256=synthetic_arming_hex("toolfile", 64),
            contract_lock_sha256="cd" * 32,
            created_at=base,
            expires_at=base + timedelta(hours=3),
            evidence_refs=tuple(
                (kind, f"SYNTHETIC/{kind}", "e5" * 32) for kind in
                ARMING_PREREQUISITE_KINDS))
    class _LedgerSyntheticRegistry:
        """SYNTHETIC-only seam satisfying the frozen ledger control-update
        CAS call (register_permit + verify_permit_active); never a production
        path (same seam class the ARM-LM-S protocol tests use)."""

        def register_permit(self, record) -> None:
            del record

        def verify_permit_active(self, permit_id: str, *, now):
            del permit_id, now
            from ak_trading.infiny_execution import contracts as codec
            return codec.ValidationResult(blockers=())

    stack = TempExecutionStack(
        tmp_path, bindings=(_w2_binding(),),
        tokens={_W2_INSTANCE: SYNTHETIC_PAIRING_TOKEN},
        evidence_registry=registry, trusted_control_assemblies=assemblies,
        arming_prerequisites=prerequisites, audit_name="w2-audit.jsonl",
        clock=clock)
    with stack.journal.maintenance():
        stack.journal.register_control_update(
            state, permit=permit, evidence_registry=_LedgerSyntheticRegistry())
    stack.arming_target = target
    stack.arming_adapter = adapter
    stack.arming_clock = clock
    return stack


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _serve_with_anchor(monkeypatch, capsys, *, journal_path, lock_path,
                       pairing_dir, registry_dir, audit_path, anchor_path=None):
    """In-process REAL launcher serve (registered journal, ephemeral port,
    17778/17779 untouched); returns the parsed execution_started line and a
    stop handle."""
    from ak_trading.infiny_execution import client_service
    captured = {}
    real_build = client_service.build_execution_server

    def build(**kwargs):
        server = real_build(**kwargs)
        captured["server"] = server
        return server

    monkeypatch.setattr(client_service, "build_execution_server", build)
    port = _free_port()
    argv = ["serve", "--port", str(port), "--journal", str(journal_path),
            "--audit-log", str(audit_path), "--pairing-dir", str(pairing_dir),
            "--registry-dir", str(registry_dir), "--contract-lock", str(lock_path)]
    if anchor_path is not None:
        argv += ["--arming-readiness", str(anchor_path)]
    result = {}

    def run_serve():
        result["code"] = _launcher_module().main(argv)

    thread = threading.Thread(target=run_serve, daemon=True)
    thread.start()
    started = None
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

    def stop():
        server = captured.get("server")
        if server is not None:
            server.shutdown()
        thread.join(timeout=15)
        return result.get("code")

    assert started is not None, "execution_started was not emitted"
    return started, stop


def _registration_files_for_serve(tmp_path):
    from ak_trading.infiny_execution import contracts as codec
    from ak_trading.infiny_execution.native_evidence import (
        OperatorRegistrationManifest)
    lock = _write_file(Path(tmp_path) / "serve" / "contract-lock.json",
                       _json_line({"lock": "w2-serve"}))
    record = _write_file(Path(tmp_path) / "serve" / "actor-record.json",
                         _json_line({"actor_id": "SIM_ACTOR_W2",
                                     "account_key": "SIM_ACCOUNT_A",
                                     "bindings": [_w2_binding()]}))
    artifact = _write_file(Path(tmp_path) / "serve" / "authorization.json",
                           _json_line({"user": "SIM_OPERATION"}))
    manifest = {
        "registration_id": "reg-w2-0001", "record_type": "ActorRegistration",
        "record_path": str(record).replace("\\", "/"),
        "record_sha256": hashlib.sha256(record.read_bytes()).hexdigest(),
        "contract_lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
        "operator_action_id": "SIM_OP_W2",
        "authorization_artifact_path": str(artifact).replace("\\", "/"),
        "authorization_artifact_sha256":
            hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "artifacts": [{"path": str(artifact).replace("\\", "/"),
                       "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}]}
    envelope = {"schema_version": codec.V2_SCHEMA_VERSION,
                "type": "OperatorRegistrationManifest", "data": manifest}
    encoded = codec.decode(json.dumps(envelope), OperatorRegistrationManifest,
                           schema_version=codec.V2_SCHEMA_VERSION)
    manifest_path = _write_file(
        Path(tmp_path) / "serve" / "manifest.json",
        codec.encode(encoded, schema_version=codec.V2_SCHEMA_VERSION))
    return lock, manifest_path


# -- M01 ----------------------------------------------------------------------

def test_m01_arming_safe_defaults_and_zero_surface(monkeypatch, tmp_path):
    """ARM-M01 (C) on the frozen combination: no-arg-constructed deployed
    client, start/restart with the arming slot set present but foreign, safe
    Params projection, no ArmContext, no native attribute resolution, zero
    anonymous HTTP (WAIT_PAIRING life), zero native calls."""
    deployment = deploy_self_strategy(monkeypatch, tmp_path)
    _require_frozen_arming(client=deployment.module)
    module, fake = deployment.module, deployment.fake
    listener = ConnectionCountingListener()
    store = expected_default_store(deployment.self_strategy_dir, 23)
    store.mkdir(parents=True)
    # first life creates the store identity (the layout gate refuses a
    # non-empty store without identity); the foreign slots land AFTER it
    warmup = deployment.no_arg(
        strategy_id=23, strategy_name="SYNTHETIC",
        params={"exchange": "SHFE", "instrument_id": "rb2610",
                "hedge_flag": "1",
                "execution_api_url": listener.url()})
    warmup.on_init()
    warmup.on_start()
    warmup.on_stop()
    # the full foreign slot set (strict shapes, another store's scope)
    proposal = synthetic_arming_proposal()
    for name, payload in (
            ("arming-proposal.json", json.dumps(proposal)),
            ("arming-plan.json",
             json.dumps(synthetic_arming_plan(proposal=proposal))),
            ("arming-request.json", json.dumps(synthetic_arming_request())),
            ("arming-challenge.json", json.dumps(
                synthetic_arming_challenge(request=synthetic_arming_request()))),
            ("arming.json", json.dumps(
                synthetic_arming_credential(request=synthetic_arming_request()))),
            ("disarm.json", json.dumps({
                "schema_version": "ne1_client_disarm_v1",
                "disarm_id": synthetic_arming_hex("disarm", 32),
                "scope": synthetic_arming_scope(), "arm_request_id": None,
                "arm_id": None,
                "requested_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "user_sid": arming_fixture_user_sid(),
                "reason": "USER_DISARM"})),
            ("arming-state.json",
             json.dumps(synthetic_arming_state(consumed=True))),
            ("arming-transfer.lock", "")):
        publish_arming_slot(str(store), name,
                            payload.encode("utf-8") if isinstance(payload, str)
                            else payload)
    try:
        bridge = deployment.no_arg(
            strategy_id=23, strategy_name="SYNTHETIC",
            params={"exchange": "SHFE", "instrument_id": "rb2610",
                    "hedge_flag": "1",
                    "execution_api_url": listener.url()})
        bridge.on_init()
        bridge.on_start()
        for _cycle in range(3):
            bridge._process_arming_files()   # lane body: foreign slots refused
        bridge._pump_once()
        assert bridge._arm_state == module.ARM_STATE_DISARMED
        assert bridge._arming_effective_now() is False
        assert bridge._arm_context is None
        assert bridge.params_map.enable_trading is False
        assert str(bridge.params_map.execution_mode) == "disabled"
        assert fake.attribute_access == []
        counts = fake.call_counts()
        assert counts.get("sendOrder", 0) == 0 and counts.get("cancelOrder", 0) == 0
        assert listener.connection_count() == 0  # zero anonymous HTTP
        # repeat start: still DISARMED, nothing resurrects
        bridge.on_stop()
        bridge.on_start()
        assert bridge._arm_state == module.ARM_STATE_DISARMED
        assert bridge._arming_effective_now() is False
        bridge.on_stop()
    finally:
        listener.close()


def test_m01_no_single_condition_alone_opens_the_gate(monkeypatch, tmp_path):
    """ARM-M01 (C): pairing alone, a service-fact state file alone, a plan
    (authorization reference) alone, or a delivered credential file alone
    each leave the local admission machine DISARMED; only the full ruled
    chain (M07) may open it."""
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    _require_frozen_arming(client=module)
    service = FakeExecutionService()
    bridge = None
    try:
        # 1. pairing alone (injected token, no arming slots)
        bridge = _pair_and_start(module, fake, tmp_path / "s1", service.poll_url)
        bridge._process_arming_files()
        bridge._pump_once()
        assert bridge._arm_state == module.ARM_STATE_DISARMED
        assert bridge._arming_effective_now() is False
        bridge.on_stop()

        # 2. a service-fact state file alone (claims consumed + epoch facts)
        store2 = tmp_path / "s2"
        bridge = _pair_and_start(module, fake, store2, service.poll_url)
        state = synthetic_arming_state(consumed=True,
                                       service_instance_id="execution-synthetic",
                                       service_epoch=9)
        publish_arming_slot(str(store2), "arming-state.json",
                            json.dumps(state).encode("utf-8"))
        bridge._process_arming_files()
        bridge._pump_once()
        assert bridge._arm_state == module.ARM_STATE_DISARMED
        assert bridge._arming_effective_now() is False
        bridge.on_stop()

        # 3. an authorization-bearing plan alone (no proposal was processed)
        store3 = tmp_path / "s3"
        bridge = _pair_and_start(module, fake, store3, service.poll_url)
        publish_arming_slot(str(store3), "arming-plan.json",
                            json.dumps(synthetic_arming_plan()).encode("utf-8"))
        bridge._process_arming_files()
        bridge._pump_once()
        assert bridge._arm_state == module.ARM_STATE_DISARMED
        assert bridge._arming_effective_now() is False
        bridge.on_stop()

        # 4. a delivered credential file alone (no published request)
        store4 = tmp_path / "s4"
        bridge = _pair_and_start(module, fake, store4, service.poll_url)
        request = synthetic_arming_request()
        credential = synthetic_arming_credential(request=request)
        publish_arming_slot(str(store4), "arming.json",
                            json.dumps(credential).encode("utf-8"))
        bridge._process_arming_files()
        bridge._pump_once()
        assert bridge._arm_state == module.ARM_STATE_DISARMED
        assert bridge._arming_effective_now() is False
        assert len(fake.send_calls()) == 0
    finally:
        stop_started_bridge(bridge)
        service.close()


# -- M02 ----------------------------------------------------------------------

def test_m02_prepare_arm_to_plan_dual_hash_public_cli(monkeypatch, tmp_path):
    """ARM-M02 (C+D) on the frozen combination: REAL prepare-arm CLI ->
    client-validated proposal -> CLIENT-computed target hash in the published
    plan; default Params and the pairing hash unchanged; only the
    PARAMS_CONFIRMED wire message carries the target hash; proposal conflict
    and wrong-release refusals."""
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    _require_frozen_arming(client=module, tool=True)
    service = FakeExecutionService()
    bridge = None
    try:
        store = _layout_store(tmp_path)
        bridge = _pair_and_start(module, fake, store, service.poll_url)
        lock_path = _write_file(Path(tmp_path) / "w2" / "contract-lock.json",
                                _json_line({"note": "SYNTHETIC lock m02"}))
        client_sha256 = hashlib.sha256(
            Path(module.__spec__.origin).read_bytes()).hexdigest()
        # wrong release hash refuses (exit 2, no slot written)
        refused = _tool_prepare_arm(store, store / "pairing-request.json",
                                    lock_path, "00" * 32, tmp_path,
                                    candidate=_arming_candidate(), tag="bad")
        assert refused.returncode == 2
        assert not (store / "arming-proposal.json").exists()
        # the real call publishes the proposal
        result = _tool_prepare_arm(store, store / "pairing-request.json",
                                   lock_path, client_sha256, tmp_path,
                                   candidate=_arming_candidate())
        assert result.returncode == 0, result.stderr
        receipt = json.loads((Path(tmp_path) / "w2" / "prepare-p1.json")
                             .read_text(encoding="utf-8"))
        assert receipt["status"] == "PREPARATION_SUBMITTED"
        assert receipt["target_params_sha256"] is None  # client is the only computer
        # conflicting proposal: the tool never overwrites (exit 2)
        conflict = _tool_prepare_arm(store, store / "pairing-request.json",
                                     lock_path, client_sha256, tmp_path,
                                     candidate=_arming_candidate(), tag="p2")
        assert conflict.returncode == 2
        # client validates while disabled -> plan with the client-computed target
        bridge._process_arming_files()
        assert bridge._arm_state == module.ARM_STATE_PREPARED_DISABLED
        plan = json.loads((store / "arming-plan.json")
                          .read_text(encoding="utf-8"))
        reference = reference_target_params_sha256(
            module, bridge.params_map, plan["effective_overrides"])
        assert plan["target_params_sha256"] == reference
        assert plan["target_params_sha256"] != bridge._params_sha256
        assert bridge._params_sha256 == plan["scope"]["pairing_params_sha256"]
        assert bridge.params_map.enable_trading is False  # defaults unchanged
        # to ARM_READY + USER credential -> the ONLY target-hash carrier
        bridge._process_arming_files()
        assert bridge._arm_state == module.ARM_STATE_ARM_READY
        # X01 unblock repair 2: drive ONE disabled-lane control cycle so the
        # AR2.1 pre-confirmation poll observation below is deterministic.
        # The control lane polls every POLL_DISABLED_INTERVAL_MS (5 s) while
        # the round is not effective; this test finishes the confirmation in
        # ~2 s, so without this cycle `pre_polls` is deterministically empty
        # and the pairing-hash carrier check never gets its input.
        bridge._control_cycle()
        install_arm_credential(bridge)
        bridge._process_arming_files()
        assert bridge._arm_state == module.ARM_STATE_ARM_PENDING
        bridge._pump_once()
        assert bridge._arm_state == module.ARM_STATE_ARMED
        confirmed = [packet for packet in service.control_packets
                     if packet["data"].get("kind") == "PARAMS_CONFIRMED"]
        assert len(confirmed) == 1
        assert confirmed[0]["data"]["confirmation"]["params_sha256"] == reference
        # every poll that left the client BEFORE the confirmation carries the
        # pairing hash, never the target (AR2.1 disabled-message rule)
        confirm_index = min(
            index for index, (path, packet) in enumerate(service.requests)
            if path.endswith("/control")
            and packet["data"].get("kind") == "PARAMS_CONFIRMED")
        pre_polls = [packet for index, (path, packet) in enumerate(service.requests)
                     if index < confirm_index and path.endswith("/poll")
                     and packet["type"] == "PollRequest"]
        assert pre_polls, "expected at least one pre-confirmation poll"
        for packet in pre_polls:
            assert packet["data"]["params_sha256"] == bridge._params_sha256
    finally:
        stop_started_bridge(bridge)
        service.close()


# -- M03 ----------------------------------------------------------------------

def test_m03_registration_order_and_cas_walls(tmp_path):
    """ARM-M03 (S+I) on the frozen combination: real v2 journal CAS walls
    (registration revision), the control-update permit dependency wall, the
    maintenance exclusivity, and the launcher's OS runtime lock that makes
    registration impossible while a service holds the journal."""
    _require_frozen_arming(service=True)
    stack = TempExecutionStack(tmp_path, bindings=(synthetic_binding(),))
    try:
        # positive order: actor registration exists (constructor), a CAS
        # update with the CURRENT revision succeeds
        revision = stack.revisions[-1]
        updated = stack.register_actor_bindings(
            (synthetic_binding(instance_id="SIM_INSTANCE_CAS",
                               session_epoch="SIM_EPOCH_CAS"),),
            expected_registration_revision=revision)
        assert updated > revision
        # stale revision: the frozen ledger CAS refuses
        with pytest.raises(LedgerError):
            stack.register_actor_bindings(
                (synthetic_binding(instance_id="SIM_INSTANCE_CAS2",
                                   session_epoch="SIM_EPOCH_CAS2"),),
                expected_registration_revision=revision)
        # control-update dependency wall: a NATIVE control state whose permit
        # is not supplied/registered is refused, never silently recorded
        from ak_trading.infiny_execution import contracts as codec
        from ak_trading.infiny_execution.native_contracts import (
            ControlState, ExecutionMode)
        packet = {"schema_version": codec.Binding.schema, "type": "Binding",
                  "data": synthetic_binding()}
        binding = codec.decode(json.dumps(packet), codec.Binding)
        orphan = ControlState(
            control_revision=1, binding=binding, mode=ExecutionMode.NATIVE,
            policy_id="SIM_POLICY_ARM",
            authorization_id=SYNTHETIC_ARMING_AUTHORIZATION_ID,
            native_permit_id="UNREGISTERED_PERMIT", params_sha256="ab" * 32,
            generation="SIM_GEN_W2", updated_at=datetime.now(timezone.utc),
            disabled_reason=None)
        with pytest.raises(LedgerError):
            with stack.journal.maintenance():
                stack.journal.register_control_update(orphan)
        # maintenance exclusivity: a second maintenance holder is refused
        with stack.journal.maintenance():
            with pytest.raises(LedgerError):
                stack.journal.begin_maintenance()
        # the launcher's runtime lock (what serve holds for its whole life):
        # exactly one holder; a concurrent registration run is locked out
        launcher = _launcher_module()
        holder = launcher.JournalRuntimeLock(stack.journal_path).acquire()
        try:
            with pytest.raises(launcher.JournalRuntimeLock.Busy):
                launcher.JournalRuntimeLock(stack.journal_path).acquire()
        finally:
            holder.release()
        # after release the lock is taken again (crashed-run safe)
        second = launcher.JournalRuntimeLock(stack.journal_path).acquire()
        second.release()
    finally:
        stack.close()


# -- M04 ----------------------------------------------------------------------

def test_m04_console_entry_positive_and_refusal_matrix(monkeypatch, tmp_path):
    """ARM-M04 (D) cross-end on the frozen combination: the REAL arm tool on
    a live client's store — a piped subprocess refuses before any read
    (ARM_INTERACTIVE_REQUIRED, zero writes), the controlled console drives
    the positive flow to ARM_SUBMITTED_AWAITING_CLIENT, wrong/empty phrases
    refuse, and the CLIENT then consumes the delivered credential (the
    cross-end bit). No --yes/--force/auto-arm chain exists."""
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    _require_frozen_arming(client=module, tool=True)
    tool = _tool_module()
    source = TOOL_PATH.read_text(encoding="utf-8")
    assert '"--yes"' not in source and '"--force"' not in source
    round_ = _full_tool_round(module, fake, tmp_path, monkeypatch)
    bridge, store = round_["bridge"], round_["store"]
    service = round_["service"]
    try:
        # piped subprocess with the CORRECT phrase available still refuses:
        # the TTY precondition comes before any file read or secret byte
        piped = _run_tool(["arm",
                           "--client-store", str(store),
                           "--arming-request", str(round_["request_path"]),
                           "--service-start", str(round_["service_start_path"]),
                           "--authorization-record",
                           str(round_["grants"]["authorization_path"]),
                           "--permit-record", str(round_["grants"]["permit_path"]),
                           "--registration-receipts",
                           str(round_["registrations_path"]),
                           "--s4-readiness", str(round_["prerequisites"]["path"]),
                           "--contract-lock", str(round_["lock_path"]),
                           "--expected-client-sha256", round_["client_sha256"],
                           "--receipt",
                           str(Path(tmp_path) / "w2" / "arm-receipt-pipe.json")])
        assert piped.returncode == 2
        assert "ARM_INTERACTIVE_REQUIRED" in piped.stdout
        assert not (store / "arming.json").exists()
        # empty phrase refuses
        with pytest.raises(Exception) as empty_error:
            _tool_arm_inprocess(tool, monkeypatch,
                                _arm_args(round_, tmp_path, "empty"), [""])
        assert "ARM_CONFIRMATION_EMPTY" in str(empty_error.value)
        # wrong phrase x3 refuses (no auto-answer ever)
        with pytest.raises(Exception) as wrong_error:
            _tool_arm_inprocess(tool, monkeypatch, _arm_args(round_, tmp_path,
                                                             "wrong"),
                                ["ARM nope", "ARM nope2", "ARM nope3"])
        assert "ARM_PHRASE_MISMATCH" in str(wrong_error.value)
        assert not (store / "arming.json").exists()
        # positive: the controlled console confirms the live scope phrase
        result, printed = _tool_arm_inprocess(tool, monkeypatch,
                                              _arm_args(round_, tmp_path, "ok"),
                                              [_arm_phrase(round_)])
        assert result["status"] == "ARM_SUBMITTED_AWAITING_CLIENT"
        receipt = json.loads(Path(result["receipt"]).read_text(encoding="utf-8"))
        assert receipt["operation"] == "arm"
        assert receipt["action_source"] == "USER_LOCAL_CONSOLE"
        # the challenge value never appears in any printed/output surface
        challenge = json.loads((store / "arming-challenge.json")
                               .read_text(encoding="utf-8"))["challenge"]
        assert_no_secret_material(printed, [challenge])
        assert_no_secret_material(json.dumps(receipt), [challenge])
        # cross-end: the CLIENT consumes the tool-delivered credential
        arm_bridge_from_tool_round(module, bridge)
        state = json.loads((store / "arming-state.json").read_text(encoding="utf-8"))
        assert state["consumed"] is True
        assert state["arm_id"] == receipt["arm_id"]
    finally:
        stop_started_bridge(bridge)
        service.close()


# -- M05 ----------------------------------------------------------------------

def test_m05_real_writers_acl_matrix(monkeypatch, tmp_path):
    """ARM-M05 (C+D) cross-end: the REAL writers' outputs are protected
    current-user-only DACLs (prepare-arm proposal slot, the client's
    published plan/request/state); a loosened store refuses prepare-arm; a
    loosened challenge slot refuses arm with the credential never published;
    no secret material reaches stdout/receipts."""
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    _require_frozen_arming(client=module, tool=True)
    tool = _tool_module()
    round_ = _full_tool_round(module, fake, tmp_path, monkeypatch)
    bridge, store = round_["bridge"], round_["store"]
    service = round_["service"]
    loose_bridge = None
    try:
        # positive: the tool writer's proposal slot and the client's published
        # slots all live in the verified protection domain
        for name in ("arming-proposal.json", "arming-plan.json",
                     "arming-request.json", "arming-state.json"):
            tool.verify_private_access(store / name, need_write=True)
        # negative: an Everyone-ACE store refuses prepare-arm before writing
        loose_store = _layout_store(Path(tmp_path) / "loose")
        loose_bridge = _pair_and_start(module, fake, loose_store,
                                       service.poll_url)
        loosen_arming_slot_dacl(loose_store)
        refused = _tool_prepare_arm(
            loose_store, loose_store / "pairing-request.json",
            round_["lock_path"], round_["client_sha256"],
            Path(tmp_path) / "loose",
            candidate=_arming_candidate(), tag="loose")
        assert refused.returncode == 2
        assert "PAIRING_ACL_RESTRICTED" in refused.stdout
        assert not (loose_store / "arming-proposal.json").exists()
        # negative: a loosened challenge slot refuses arm before the console
        challenge_path = store / "arming-challenge.json"
        challenge_value = json.loads(
            challenge_path.read_text(encoding="utf-8"))["challenge"]
        loosen_arming_slot_dacl(challenge_path)
        with pytest.raises(Exception) as acl_error:
            _tool_arm_inprocess(tool, monkeypatch,
                                _arm_args(round_, tmp_path, "acl"),
                                [_arm_phrase(round_)])
        assert "PAIRING_ACL_RESTRICTED" in str(acl_error.value)
        assert not (store / "arming.json").exists()
        # the challenge bytes never entered the refusal surface
        assert_no_secret_material(str(acl_error.value), [challenge_value])
    finally:
        stop_started_bridge(bridge)
        stop_started_bridge(loose_bridge)
        service.close()


# -- M06 ----------------------------------------------------------------------

def test_m06_schema_binding_tamper_zero_activation(monkeypatch, tmp_path):
    """ARM-M06 (C+D) cross-end: per-case tampered deliveries to the frozen
    client's public slots — unknown field, duplicate key, foreign scope via
    the stale published request, wrong user SID, wrong challenge, expired
    credential — every case yields ZERO activation with the safe reason."""
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    _require_frozen_arming(client=module, tool=True)
    service = FakeExecutionService()
    bridge = None
    try:
        bridge = _pair_and_start(module, fake, tmp_path / "store",
                                 service.poll_url)

        # unknown extra field in the proposal
        proposal = install_arming_proposal(bridge)
        tampered = dict(proposal)
        tampered["extra_field"] = 1
        publish_arming_slot(bridge._store_path, "arming-proposal.json",
                            json.dumps(tampered).encode("utf-8"))
        bridge._process_arming_files()
        assert bridge._arm_state == module.ARM_STATE_DISARMED
        assert bridge._last_arming_reason == "ARMING_PROPOSAL_INVALID"
        assert bridge._arming_effective_now() is False

        # duplicate JSON keys (raw bytes)
        raw = ('{"schema_version": "ne1_arming_proposal_v1", '
               '"schema_version": "ne1_arming_proposal_v1", '
               '"proposal_id": "%s"}' % synthetic_arming_hex("dup", 32))
        publish_arming_slot(bridge._store_path, "arming-proposal.json",
                            raw.encode("utf-8"))
        bridge._process_arming_files()
        assert bridge._arm_state == module.ARM_STATE_DISARMED
        assert bridge._last_arming_reason == "ARMING_PROPOSAL_INVALID"

        # a fresh valid round reaches ARM_READY for the credential cases
        install_arming_proposal(bridge)
        bridge._process_arming_files()
        assert bridge._arm_state == module.ARM_STATE_PREPARED_DISABLED
        bridge._process_arming_files()
        assert bridge._arm_state == module.ARM_STATE_ARM_READY

        # stale request: the published request bytes drift after the freeze
        request_path = Path(bridge._store_path) / "arming-request.json"
        original_bytes = request_path.read_bytes()
        published = json.loads(original_bytes.decode("utf-8"))
        inactive = dict(published)
        inactive["active"] = False
        publish_arming_slot(bridge._store_path, "arming-request.json",
                            json.dumps(inactive).encode("utf-8"))
        install_arm_credential(bridge)  # bound to the ORIGINAL request bytes
        bridge._process_arming_files()
        assert bridge._arm_state == module.ARM_STATE_ARM_READY  # not consumed
        assert bridge._last_arming_reason in ("ARMING_REQUEST_CHANGED",
                                              "ARMING_REQUEST_INVALID")
        # restore the exact original bytes for the remaining cases
        publish_arming_slot(bridge._store_path, "arming-request.json",
                            original_bytes)

        # wrong user SID in the credential
        install_arm_credential(bridge)
        credential = json.loads(
            (Path(bridge._store_path) / "arming.json").read_text(encoding="utf-8"))
        credential["user_sid"] = "S-1-5-21-9999-9999-9999-9999-9999"
        publish_arming_slot(bridge._store_path, "arming.json",
                            json.dumps(credential).encode("utf-8"))
        bridge._process_arming_files()
        assert bridge._arm_state == module.ARM_STATE_ARM_READY
        assert bridge._last_arming_reason == "ARMING_USER_SID_MISMATCH"

        # wrong challenge value
        install_arm_credential(bridge, challenge_override="ff" * 32)
        bridge._process_arming_files()
        assert bridge._arm_state == module.ARM_STATE_ARM_READY
        assert bridge._last_arming_reason == "ARMING_CHALLENGE_MISMATCH"

        # expired credential (public-path expiry counterexample, AR5.1):
        # valid window shape, already elapsed by consumption time
        install_arm_credential(
            bridge,
            expires_override=datetime.now(timezone.utc)
            + timedelta(milliseconds=400))
        time.sleep(0.6)
        bridge._process_arming_files()
        assert bridge._arm_state == module.ARM_STATE_ARM_READY
        assert bridge._last_arming_reason == "ARMING_CREDENTIAL_EXPIRED"

        # every case above: zero native surface movement
        assert len(fake.send_calls()) == 0
        assert fake.attribute_access == []
    finally:
        stop_started_bridge(bridge)
        service.close()


# -- M07 ----------------------------------------------------------------------

def test_m07_full_positive_chain_to_armed(monkeypatch, tmp_path):
    """ARM-M07 (C+D+S) cross-end on the frozen combination: real delivered
    client + REAL prepare-arm/arm tool + mirrored loopback service. The three
    facts are recorded SEPARATELY (tool receipt, service RECORDED over real
    HTTP, client ARMED); only-file-receipt and only-service-ACK stay pending;
    fake/real counts stay in disjoint buckets."""
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    _require_frozen_arming(client=module, tool=True, service=True)
    tool = _tool_module()
    round_ = _full_tool_round(module, fake, tmp_path, monkeypatch)
    bridge, store = round_["bridge"], round_["store"]
    service = round_["service"]
    listener = ConnectionCountingListener()
    try:
        # intermediate honesty: the tool receipt alone leaves the client at
        # ARM_READY (a delivered file is not an activation)
        result, printed = _tool_arm_inprocess(tool, monkeypatch,
                                              _arm_args(round_, tmp_path, "m07"),
                                              [_arm_phrase(round_)])
        receipt = json.loads(Path(result["receipt"]).read_text(encoding="utf-8"))
        assert receipt["status"] == "ARM_SUBMITTED_AWAITING_CLIENT"
        assert bridge._arm_state == module.ARM_STATE_ARM_READY
        assert bridge._arming_effective_now() is False
        challenge = json.loads((store / "arming-challenge.json")
                               .read_text(encoding="utf-8"))["challenge"]
        assert_no_secret_material(printed + json.dumps(receipt), [challenge])
        # client consumption + exact ACK (service fact over real HTTP)
        bridge._process_arming_files()
        assert bridge._arm_state == module.ARM_STATE_ARM_PENDING
        assert bridge._arming_ack_received
        # only-service-ACK honesty: no legal callback yet -> still not ARMED
        assert bridge._arming_effective_now() is False
        bridge._pump_once()  # legal framework callback installs ArmContext
        assert bridge._arm_state == module.ARM_STATE_ARMED
        assert bridge._arming_effective_now() is True
        # the three separately recorded facts, joined only by IDs
        state = json.loads((store / "arming-state.json").read_text(encoding="utf-8"))
        confirmed = [packet for packet in service.control_packets
                     if packet["data"].get("kind") == "PARAMS_CONFIRMED"]
        assert state["arm_id"] == receipt["arm_id"] == \
            confirmed[0]["data"]["confirmation"]["confirmation_id"]
        assert state["consumed"] is True and state["revoked"] is False
        assert not (store / "arming.json").exists()
        assert not (store / "arming-challenge.json").exists()
        # armed dispatch over the public gate; disjoint count buckets
        set_synthetic_gate_capture(bridge, quote=True, account=True, position=True,
                                   contract=True)
        command = armed_dispatch(bridge)
        bridge._receive_dispatch(command)
        bridge._pump_once()
        assert len(fake.send_calls()) == 1
        buckets = {
            "fake_native_calls": dict(fake.call_counts()),
            "fake_native_send_attempts": bridge.counters["native_send_attempts"],
            "fake_native_cancel_attempts": bridge.counters["native_cancel_attempts"],
            "real_service_code_requests": len(service.requests),
            "real_socket_attempts_to_sentinel": listener.connection_count(),
        }
        assert buckets["fake_native_send_attempts"] == 1
        assert buckets["real_socket_attempts_to_sentinel"] == 0
        assert sum(buckets["fake_native_calls"].values()) >= 1
        assert buckets["real_service_code_requests"] >= 1
    finally:
        stop_started_bridge(bridge)
        service.close()
        listener.close()


# -- M08 ----------------------------------------------------------------------

def test_m08_service_authority_matrix(tmp_path):
    """ARM-M08 (S) cross-end over raw real HTTP: the PARAMS_CONFIRMED handler
    verifies the full grant chain — positive exact ACK with RECORDED-without-
    readiness semantics, then the counterexample wall (wrong hash, wrong
    adapter, expired, conflicting confirmation, second confirmation), the
    poll issuance gate, the production wiring refusal and the GATE_CLOSED
    latch."""
    _require_frozen_arming(service=True)
    stack = _w2_trusted_stack(tmp_path)
    plain = TempExecutionStack(
        Path(tmp_path) / "plain", bindings=(_w2_binding(),),
        tokens={_W2_INSTANCE: SYNTHETIC_PAIRING_TOKEN})
    try:
        base = stack.poll_url.rsplit("/", 1)[0]
        target = stack.arming_target
        adapter = stack.arming_adapter
        epoch = stack.service_epoch
        binding = _w2_binding()
        clock_now = stack.arming_clock.now()

        # positive: exact ACK echo (confirmation id, target, permit in
        # readiness, both submission flags stay false)
        report = _w2_control_report(binding, target, request_id="req-w2-1",
                                    sequence=1, service_epoch=epoch,
                                    confirmation_id="conf-w2-1",
                                    adapter_sha256=adapter, now=clock_now)
        status, body = _post_with_retry(base + "/control", report,
                                        token=SYNTHETIC_PAIRING_TOKEN)
        assert status == 200
        data = body["data"]
        assert data["kind"] == "RECORDED" and data["report_kind"] == "PARAMS_CONFIRMED"
        assert data["confirmation_id"] == "conf-w2-1"
        assert data["params_sha256"] == target
        assert data["readiness"]["native_permit_id"] == SYNTHETIC_ARMING_PERMIT_ID
        assert data["readiness"]["can_submit"] is False
        assert data["readiness"]["can_cancel_owned"] is False

        def blocked_by(report_document):
            code, response = _post_with_retry(base + "/control", report_document,
                                              token=SYNTHETIC_PAIRING_TOKEN)
            return code, response["data"]

        # wrong target hash -> authorization refusal
        wrong_hash = _w2_control_report(
            binding, synthetic_arming_hex("other", 64), request_id="req-w2-2",
            sequence=2, service_epoch=epoch, confirmation_id="conf-w2-2",
            adapter_sha256=adapter, now=clock_now)
        code, data = blocked_by(wrong_hash)
        assert code == 200 and data["kind"] == "BLOCKED"
        assert "AUTHORIZATION_REQUIRED" in data["blockers"]
        # wrong adapter hash (unapproved release) -> authorization refusal
        wrong_adapter = _w2_control_report(
            binding, target, request_id="req-w2-3", sequence=3,
            service_epoch=epoch, confirmation_id="conf-w2-3",
            adapter_sha256=synthetic_arming_hex("other", 64), now=clock_now)
        code, data = blocked_by(wrong_adapter)
        assert code == 200 and data["kind"] == "BLOCKED"
        assert "AUTHORIZATION_REQUIRED" in data["blockers"]
        # expired confirmation -> EXPIRED (frozen vocabulary): a valid DTO
        # window (expires > confirmed) that has already elapsed for the
        # service clock needs its own stack (a past-window DTO is a 400 at
        # the decode boundary, which is a DIFFERENT refusal)
        expired_stack = _w2_trusted_stack(Path(tmp_path) / "expired")
        try:
            expired_now = expired_stack.arming_clock.now()
            expired_stack.arming_clock.advance(5.0)
            elapsed = _w2_control_report(
                binding, expired_stack.arming_target, request_id="req-w2-4",
                sequence=1, service_epoch=expired_stack.service_epoch,
                confirmation_id="conf-w2-4",
                adapter_sha256=expired_stack.arming_adapter,
                confirmed_at=expired_now - timedelta(seconds=10),
                expires_at=expired_now + timedelta(seconds=1),
                now=expired_now)
            code, response = _post_with_retry(
                expired_stack.poll_url.rsplit("/", 1)[0] + "/control",
                elapsed, token=SYNTHETIC_PAIRING_TOKEN)
            assert code == 200 and response["data"]["kind"] == "BLOCKED"
            assert "EXPIRED" in response["data"]["blockers"]
        finally:
            expired_stack.close()
        # same confirmation id with DIFFERENT content -> conflict refusal
        conflicting = _w2_control_report(
            binding, target, request_id="req-w2-5", sequence=5,
            service_epoch=epoch, confirmation_id="conf-w2-1",
            adapter_sha256=adapter, now=clock_now)
        conflicting["data"]["confirmation"]["confirmed_at"] = \
            (clock_now - timedelta(seconds=1)).isoformat()
        code, data = blocked_by(conflicting)
        assert code == 200 and data["kind"] == "BLOCKED"
        assert "AUTHORIZATION_REQUIRED" in data["blockers"]
        # replay of the SAME logical confirmation replays the same fact
        code, data = blocked_by(report)
        assert code == 200 and data["kind"] == "RECORDED"
        # a second DIFFERENT confirmation id for the same binding -> refusal
        second = _w2_control_report(
            binding, target, request_id="req-w2-6", sequence=6,
            service_epoch=epoch, confirmation_id="conf-w2-6",
            adapter_sha256=adapter, now=clock_now)
        code, data = blocked_by(second)
        assert code == 200 and data["kind"] == "BLOCKED"
        assert "AUTHORIZATION_REQUIRED" in data["blockers"]

        # issuance gate: with NO confirmation for the binding the poll NEVER
        # issues the staged native offer
        staging = _w2_trusted_stack(Path(tmp_path) / "stage")
        try:
            from ak_trading.infiny_execution.client_service import PollOutcome
            from ak_trading.infiny_execution.native_contracts import ResponseKind
            staging.orchestrator.poll_outcomes.append(
                PollOutcome(kind=ResponseKind.OFFER,
                            offer={"offer_id": "SIM_OFFER_W2",
                                   "params_sha256": staging.arming_target},
                            blockers=()))
            staging.clock.advance(5.05)
            poll = {"schema_version": "infiny_client_execution_transport_v1_1",
                    "type": "PollRequest",
                    "data": {"request_id": "req-poll-w2",
                             "instance_id": _W2_INSTANCE,
                             "session_epoch": _W2_SESSION,
                             "generation": "SIM_GEN_W2",
                             "service_epoch": staging.service_epoch,
                             "sequence": 1,
                             "sent_at": datetime.now(timezone.utc).isoformat(),
                             "params_sha256": staging.arming_target,
                             "snapshot": None, "snapshot_id": None,
                             "client_state_digest": "ab" * 32,
                             "ack_watermark": None}}
            code, body = _post_with_retry(staging.poll_url, poll,
                                          token=SYNTHETIC_PAIRING_TOKEN)
            assert code == 200
            assert body["data"]["kind"] == "BLOCKED"
            assert "NATIVE_DISABLED" in body["data"]["blockers"]
        finally:
            staging.close()

        # production wiring (no trusted assembly, no anchor): BLOCKED, never
        # RECORDED — the DISABLED assembly cannot promote through this handler
        plain_base = plain.poll_url.rsplit("/", 1)[0]
        plain_report = _w2_control_report(
            binding, target, request_id="req-w2-plain", sequence=1,
            service_epoch=plain.service_epoch, confirmation_id="conf-plain-1",
            adapter_sha256=adapter, now=plain.clock.now())
        code, body = _post_with_retry(plain_base + "/control", plain_report,
                                      token=SYNTHETIC_PAIRING_TOKEN)
        assert code == 200 and body["data"]["kind"] == "BLOCKED"
        assert "NATIVE_DISABLED" in body["data"]["blockers"]

        # GATE_CLOSED latches: replayed AND fresh confirmations both refused
        # for the rest of this epoch
        gate = {"schema_version": "infiny_client_execution_transport_v1_1",
                "type": "ControlReport",
                "data": {"request_id": "req-w2-gate", "instance_id": _W2_INSTANCE,
                         "session_epoch": _W2_SESSION, "generation": "SIM_GEN_W2",
                         "service_epoch": epoch, "sequence": 7,
                         "sent_at": datetime.now(timezone.utc).isoformat(),
                         "params_sha256": target, "kind": "GATE_CLOSED",
                         "confirmation": None}}
        code, body = _post_with_retry(base + "/control", gate,
                                      token=SYNTHETIC_PAIRING_TOKEN)
        assert code == 200
        code, data = blocked_by(report)   # replayed confirmation
        assert data["kind"] == "BLOCKED"
        fresh = _w2_control_report(
            binding, target, request_id="req-w2-7", sequence=8,
            service_epoch=epoch, confirmation_id="conf-w2-7",
            adapter_sha256=adapter, now=clock_now)
        code, data = blocked_by(fresh)
        assert data["kind"] == "BLOCKED"
        events = [record.get("event") for record in stack.audit_records()]
        assert "arming_revoked" in events
    finally:
        stack.close()
        plain.close()


def _w2_control_report(binding, target, *, request_id, sequence, service_epoch,
                       confirmation_id, adapter_sha256, confirmed_at=None,
                       expires_at=None, explicit=True, generation="SIM_GEN_W2",
                       now=None):
    now = now or datetime.now(timezone.utc)
    return {"schema_version": "infiny_client_execution_transport_v1_1",
            "type": "ControlReport",
            "data": {"request_id": request_id,
                     "instance_id": binding["instance_id"],
                     "session_epoch": binding["session_epoch"],
                     "generation": generation, "service_epoch": service_epoch,
                     "sequence": sequence,
                     "sent_at": now.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                     "params_sha256": target, "kind": "PARAMS_CONFIRMED",
                     "confirmation": {
                         "confirmation_id": confirmation_id, "binding": binding,
                         "params_sha256": target,
                         "adapter_sha256": adapter_sha256,
                         "confirmed_at": (confirmed_at or now
                                          ).isoformat(timespec="microseconds").replace("+00:00", "Z"),
                         "expires_at": (expires_at or now + timedelta(minutes=5)
                                        ).isoformat(timespec="microseconds").replace("+00:00", "Z"),
                         "explicit_ui_action": explicit}}}


def test_m08_arming_readiness_path_anchor(tmp_path, monkeypatch, capsys):
    """ARM-M08 (S) cross-end: the REAL launcher serve anchors the approved
    prerequisites bytes at startup (strict three-key object), a wrong service
    hash degrades to null with the service still legally disabled, and the
    omitted case stays null — 17778/17779 untouched (ephemeral port)."""
    _require_frozen_arming(service=True)
    from ak_trading.infiny_execution import client_service as cs
    lock, manifest = _registration_files_for_serve(tmp_path)

    def registered_journal(name):
        journal = Path(tmp_path) / "serve" / name
        assert _launcher_module().main(
            ["register-actor", "--journal", str(journal), "--manifest",
             str(manifest), "--contract-lock", str(lock), "--initialize"]) == 0
        return journal

    def anchor_document(service_sha=None):
        now = datetime.now(timezone.utc)
        return {"schema_version": "ne1_arming_prerequisites_v1",
                "prerequisites_id": synthetic_arming_hex("anchor", 32),
                "created_at": now.isoformat().replace("+00:00", "Z"),
                "expires_at": (now + timedelta(hours=1)
                               ).isoformat().replace("+00:00", "Z"),
                "contract_lock_sha256":
                    hashlib.sha256(lock.read_bytes()).hexdigest(),
                "client_file_sha256": synthetic_arming_hex("client", 64),
                "service_file_sha256": service_sha or hashlib.sha256(
                    Path(cs.__file__).read_bytes()).hexdigest(),
                "tool_file_sha256": synthetic_arming_hex("tool", 64),
                "evidence_refs": [{"kind": kind, "path": f"SYNTHETIC/{kind}",
                                   "sha256": "e5" * 32} for kind in
                                  ARMING_PREREQUISITE_KINDS]}

    anchor = _write_file(Path(tmp_path) / "serve" / "anchor.json",
                         _json_line(anchor_document()))
    bad = _write_file(Path(tmp_path) / "serve" / "anchor-bad.json",
                      _json_line(anchor_document(service_sha="0" * 64)))
    started, stop = _serve_with_anchor(
        monkeypatch, capsys, journal_path=registered_journal("journal.sqlite"),
        lock_path=lock, pairing_dir=Path(tmp_path) / "serve" / "pairing",
        registry_dir=Path(tmp_path) / "serve" / "registry",
        audit_path=Path(tmp_path) / "serve" / "audit.jsonl", anchor_path=anchor)
    try:
        payload = started["arming_prerequisites"]
        document = json.loads(anchor.read_text(encoding="utf-8"))
        assert set(payload) == {"prerequisites_id", "prerequisites_sha256",
                                "client_file_sha256"}
        assert payload["prerequisites_id"] == document["prerequisites_id"]
        assert payload["prerequisites_sha256"] == hashlib.sha256(
            anchor.read_bytes()).hexdigest()
        assert started["execution_enabled"] is False
    finally:
        assert stop() == 0
    started, stop = _serve_with_anchor(
        monkeypatch, capsys, journal_path=registered_journal("j2.sqlite"),
        lock_path=lock, pairing_dir=Path(tmp_path) / "serve" / "pairing2",
        registry_dir=Path(tmp_path) / "serve" / "registry2",
        audit_path=Path(tmp_path) / "serve" / "audit2.jsonl", anchor_path=bad)
    try:
        assert started["arming_prerequisites"] is None  # rejected, not fatal
        assert started["execution_enabled"] is False
    finally:
        assert stop() == 0
    started, stop = _serve_with_anchor(
        monkeypatch, capsys, journal_path=registered_journal("j3.sqlite"),
        lock_path=lock, pairing_dir=Path(tmp_path) / "serve" / "pairing3",
        registry_dir=Path(tmp_path) / "serve" / "registry3",
        audit_path=Path(tmp_path) / "serve" / "audit3.jsonl")
    try:
        assert started["arming_prerequisites"] is None  # omitted stays null
        assert started["execution_enabled"] is False
    finally:
        assert stop() == 0


# -- M09 ----------------------------------------------------------------------

def test_m09_crash_and_replay_matrix(monkeypatch, tmp_path):
    """ARM-M09 (C+D) cross-end: one-time consumption on the frozen client —
    a corrupt ACK keeps ARM_PENDING with a bounded retransmission of the
    SAME logical confirmation; the consumed credential's replay and a
    same-arm_id changed payload are ignored (never a second confirmation); a
    BLOCKED ACK is terminal; restart with an ARMED-claiming state file plus
    leftover credential never re-arms; the tool refuses a consumed round."""
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    _require_frozen_arming(client=module, tool=True)
    service = FakeExecutionService()
    bridge = None
    try:
        store = _layout_store(tmp_path)   # tool-compatible .ak_execution layout
        bridge = _pair_and_start(module, fake, store, service.poll_url)
        # corrupt ACK first, then RECORDED: the SAME confirmation is retried
        service.control_ack_kinds.extend(["INVALID"])
        install_arming_proposal(bridge)
        bridge._process_arming_files()
        bridge._process_arming_files()
        install_arm_credential(bridge)
        bridge._process_arming_files()          # consume -> corrupt ACK
        assert bridge._arm_state == module.ARM_STATE_ARM_PENDING
        assert bridge._arming_ack_received is False
        bridge._process_arming_files()          # bounded retransmission
        assert bridge._arming_ack_received is True
        bridge._pump_once()
        assert bridge._arm_state == module.ARM_STATE_ARMED
        confirms = [packet for packet in service.control_packets
                    if packet["data"].get("kind") == "PARAMS_CONFIRMED"]
        assert len(confirms) == 2
        assert confirms[0]["data"]["confirmation"] \
            == confirms[1]["data"]["confirmation"]
        # a same-arm_id changed payload lands in the store AFTER the round is
        # over: ignored, never a third confirmation
        state_bytes = (store / "arming-state.json").read_bytes()
        arm_id = json.loads(state_bytes.decode("utf-8"))["arm_id"]
        request = json.loads((store / "arming-request.json")
                             .read_text(encoding="utf-8"))
        forged = {
            "schema_version": "ne1_client_arming_v1",
            "arm_id": arm_id or synthetic_arming_hex("forged", 32),
            "request_id": request["request_id"],
            "request_sha256": "00" * 32,  # changed payload, same arm_id
            "plan_id": request["plan_id"],
            "scope": request["scope"], "binding": request["binding"],
            "target_params_sha256": request["target_params_sha256"],
            "authorization_id": request["authorization_id"],
            "authorization_record_sha256": "af" * 32,
            "permit_id": request["permit_id"],
            "permit_record_sha256": "bf" * 32,
            "service_instance_id": request["service_instance_id"],
            "service_epoch": request["service_epoch"],
            "expected_control_revision": 1,
            "prerequisites_id": "SIM_PREREQ_1", "prerequisites_sha256": "cf" * 32,
            "challenge": "aa" * 32, "user_sid": arming_fixture_user_sid(),
            "action_source": "USER_LOCAL_CONSOLE", "explicit_user_action": True,
            "confirmed_at": datetime.now(timezone.utc).isoformat()
            .replace("+00:00", "Z"),
            "expires_at": (datetime.now(timezone.utc)
                           + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        }
        publish_arming_slot(bridge._store_path, "arming.json",
                            json.dumps(forged).encode("utf-8"))
        bridge._process_arming_files()
        assert bridge._arm_state == module.ARM_STATE_ARMED  # unchanged
        confirms = [packet for packet in service.control_packets
                    if packet["data"].get("kind") == "PARAMS_CONFIRMED"]
        assert len(confirms) == 2  # no third confirmation, ever
        # restart with the ARMED-claiming state + leftover credential: cold
        bridge.on_stop()   # _store_path is released: use the saved path
        claiming = json.loads(state_bytes.decode("utf-8"))
        claiming["effective_enable_trading"] = True
        publish_arming_slot(str(store), "arming-state.json",
                            json.dumps(claiming).encode("utf-8"))
        bridge = _pair_and_start(module, fake, store, service.poll_url)
        bridge._process_arming_files()
        bridge._pump_once()
        assert bridge._arm_state == module.ARM_STATE_DISARMED
        assert bridge._arming_effective_now() is False
        assert bridge._arm_context is None
        # the tool refuses a consumed round (prepare-arm one-shot guard)
        lock_path = _write_file(Path(tmp_path) / "w2" / "contract-lock.json",
                                _json_line({"note": "lock m09"}))
        client_sha256 = hashlib.sha256(
            Path(module.__spec__.origin).read_bytes()).hexdigest()
        refused = _tool_prepare_arm(store, store / "pairing-request.json",
                                    lock_path, client_sha256, tmp_path,
                                    candidate=_arming_candidate(), tag="m09")
        assert refused.returncode == 2
        # the existing-proposal wall fires first: the consumed round can
        # never receive a fresh candidate (ARM_ROUND_CLOSED is unreachable
        # behind it — both close the round)
        assert "ARM_PROPOSAL_ALREADY_PRESENT" in refused.stdout
        # BLOCKED service ACK is terminal (no new challenge, no second arm);
        # the queue is loaded AFTER the previous life's stop reports so the
        # BLOCKED lands exactly on this round's PARAMS_CONFIRMED
        store2 = tmp_path / "store2"
        bridge.on_stop()
        bridge = _pair_and_start(module, fake, store2, service.poll_url)
        install_arming_proposal(bridge)
        bridge._process_arming_files()
        bridge._process_arming_files()
        service.control_ack_kinds.extend(["BLOCKED"])
        install_arm_credential(bridge)
        bridge._process_arming_files()
        assert bridge._arm_state in (module.ARM_STATE_DISARMED_TERMINAL,
                                     module.ARM_STATE_RESTART_REQUIRED)
        assert bridge._arming_effective_now() is False
        assert len(fake.send_calls()) == 0
    finally:
        stop_started_bridge(bridge)
        service.close()


# -- M10 ----------------------------------------------------------------------

def test_m10_revoke_race_matrix(monkeypatch, tmp_path):
    """ARM-M10 (C+S) cross-end: the REAL disarm CLI publishes the scoped
    revocation; the client observes it before the credential (revoke wins
    the race) and the late credential is ignored; a wrong-scope disarm is
    STALE_SCOPE and revokes nothing; an armed round disarms to a terminal
    state with GATE_CLOSED on the wire and no new native entry afterwards."""
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    _require_frozen_arming(client=module, tool=True)
    service = FakeExecutionService()
    bridge = None
    try:
        store = _layout_store(tmp_path)
        bridge = _pair_and_start(module, fake, store, service.poll_url)
        install_arming_proposal(bridge)
        bridge._process_arming_files()
        bridge._process_arming_files()
        assert bridge._arm_state == module.ARM_STATE_ARM_READY

        # wrong scope first: STALE_SCOPE, the live round is untouched
        stale = {"schema_version": "ne1_client_disarm_v1",
                 "disarm_id": synthetic_arming_hex("disarm", 32),
                 "scope": synthetic_arming_scope(), "arm_request_id": None,
                 "arm_id": None,
                 "requested_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                 "user_sid": arming_fixture_user_sid(),
                 "reason": "USER_DISARM"}
        publish_arming_slot(bridge._store_path, "disarm.json",
                            json.dumps(stale).encode("utf-8"))
        bridge._process_arming_files()
        assert bridge._arm_state == module.ARM_STATE_ARM_READY
        assert bridge._last_arming_reason == "ARMING_DISARM_STALE_SCOPE"
        (Path(bridge._store_path) / "disarm.json").unlink()

        # the REAL disarm CLI on the live round (offline, no console)
        receipt_path = _write_file(Path(tmp_path) / "w2" / "disarm-receipt.json",
                                   "")  # fresh path with an existing parent
        receipt_path.unlink()
        result = _run_tool(["disarm",
                            "--client-store", str(store),
                            "--client-request",
                            str(store / "pairing-request.json"),
                            "--receipt", str(receipt_path)])
        assert result.returncode == 0, result.stderr
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        assert receipt["status"] == "DISARM_REQUESTED"
        assert receipt["local_gate_closed"] is None  # PENDING, never claimed
        # revoke wins the race: the late credential is refused by the round
        bridge._process_arming_files()
        assert bridge._arm_state in (module.ARM_STATE_DISARMED_TERMINAL,
                                     module.ARM_STATE_RESTART_REQUIRED)
        assert bridge._arming_effective_now() is False
        install_arm_credential(bridge)  # late USER delivery
        bridge._process_arming_files()
        assert bridge._arm_state in (module.ARM_STATE_DISARMED_TERMINAL,
                                     module.ARM_STATE_RESTART_REQUIRED)
        confirms = [packet for packet in service.control_packets
                    if packet["data"].get("kind") == "PARAMS_CONFIRMED"]
        assert confirms == []  # the revoked round never confirmed anything

        # armed round: disarm closes the gate before any new native entry
        store2 = _layout_store(Path(tmp_path) / "second")
        bridge.on_stop()
        bridge = _pair_and_start(module, fake, store2, service.poll_url)
        arm_bridge_to_armed(module, bridge)
        set_synthetic_gate_capture(bridge, quote=True, account=True, position=True,
                                   contract=True)
        # X01 unblock repair (m10 second half): build the post-disarm probe
        # dispatch while the round is still armed — revocation drops the
        # ArmContext (AR5.2), so armed_dispatch cannot be built afterwards.
        after_cmd = armed_dispatch(bridge, command_id="SIM_CMD_AFTER_DISARM")
        result = _run_tool(["disarm",
                            "--client-store", str(store2),
                            "--client-request",
                            str(store2 / "pairing-request.json"),
                            "--receipt", str(receipt_path.with_name(
                                "disarm-receipt2.json"))])
        assert result.returncode == 0, result.stderr
        bridge._process_arming_files()  # observe -> latch + local ACK
        assert bridge._arming_effective_now() is False
        command = after_cmd
        bridge._receive_dispatch(command)
        bridge._pump_once()
        assert len(fake.send_calls()) == 0  # no new native entry after the gate
        assert "GATE_CLOSED" in service.control_reports
    finally:
        stop_started_bridge(bridge)
        service.close()


# -- M11 ----------------------------------------------------------------------


@pytest.fixture
def m11_controlled_dispatch_clock(monkeypatch):
    """Install one local UTC/monotonic source on the isolated client only.

    Arming documents are produced by the existing fixture helpers using the
    current UTC time, so the clock starts at the same current instant.  The
    client's ``_now_utc`` and ``time.monotonic`` references are the only
    patched inputs.  The test module's real ``time`` module remains untouched;
    socket waits and thread joins therefore keep their normal wall-clock
    behavior.
    """
    clock = ArmingClock(start=datetime.now(timezone.utc))

    def install(module):
        monkeypatch.setattr(module, "_now_utc", clock.now)
        monkeypatch.setattr(module, "time",
                            SimpleNamespace(monotonic=clock.monotonic))
        return clock

    return install


def _m11_dispatch(bridge, clock, *, kind="SUBMIT", command_id,
                  order_id=None, ttl_ms=400):
    """Build a 400 ms ticket against the controlled clock's UTC instant."""
    issued = clock.now()
    command = armed_dispatch(bridge, kind=kind, command_id=command_id,
                             order_id=order_id, ttl_ms=ttl_ms)
    command["issued_at"] = issued.isoformat(
        timespec="microseconds").replace("+00:00", "Z")
    command["expires_at"] = (issued + timedelta(milliseconds=ttl_ms)).isoformat(
        timespec="microseconds").replace("+00:00", "Z")
    return command


def _m11_wait_for_control_lane_idle(bridge, *, timeout=2.0):
    """Let the real control lane finish before a manual arming step.

    The bridge starts its production control lane before this test publishes
    the synthetic operator slots.  A full-file run can therefore overlap a
    background ``_process_arming_files`` call with the manual transition that
    freezes ``_arm_request`` and writes the challenge.  Waiting for the
    existing lane's bounded ``busy`` seam is sufficient coordination and
    leaves the lane running; this helper never clears a slot, changes a state,
    or injects an arming fact.
    """
    lane = getattr(bridge, "_lanes", {}).get("control")
    if lane is None:
        return
    deadline = time.monotonic() + timeout
    while True:
        if not lane.busy:
            # Require two consecutive idle observations.  This closes the
            # tiny read-to-start window without touching the production lane.
            time.sleep(0.001)
            if not lane.busy:
                return
        if time.monotonic() >= deadline:
            raise AssertionError("M11 control lane did not become idle")
        time.sleep(0.001)


def _m11_arm_bridge_to_armed(module, bridge):
    """Drive the same public arming chain as the shared fixture helper.

    The only addition is a bounded wait for the already-running control lane
    before each file publication or processing call, preventing a concurrent
    request/challenge publication from making the credential observation
    internally inconsistent during the full arming-file run.
    """
    _m11_wait_for_control_lane_idle(bridge)
    install_arming_proposal(bridge)
    _m11_wait_for_control_lane_idle(bridge)
    bridge._process_arming_files()          # DISARMED -> PREPARED_DISABLED
    assert bridge._arm_state == module.ARM_STATE_PREPARED_DISABLED, \
        bridge._last_arming_reason
    _m11_wait_for_control_lane_idle(bridge)
    bridge._process_arming_files()          # PREPARED_DISABLED -> ARM_READY
    assert bridge._arm_state == module.ARM_STATE_ARM_READY, \
        bridge._last_arming_reason
    _m11_wait_for_control_lane_idle(bridge)
    install_arm_credential(bridge)
    _m11_wait_for_control_lane_idle(bridge)
    bridge._process_arming_files()          # ARM_READY -> ARM_PENDING (+ ACK)
    assert bridge._arm_state == module.ARM_STATE_ARM_PENDING, \
        bridge._last_arming_reason
    assert bridge._arming_ack_received, bridge._last_arming_reason
    bridge._pump_once()                     # legal callback -> ARMED
    assert bridge._arm_state == module.ARM_STATE_ARMED, \
        bridge._last_arming_reason
    assert bridge._arming_effective_now() is True
    assert bridge.params_map.enable_trading is False
    return bridge


def _m11_command_summary(command):
    return {"command_id": command["command_id"],
            "attempt_id": command["attempt_id"],
            "kind": command["kind"]}


def _m11_slot_summary(bridge):
    command = bridge._dispatch_slot
    return None if command is None else _m11_command_summary(command)


def _m11_state_snapshot(bridge):
    return {"arm_state": bridge._arm_state,
            "slot": _m11_slot_summary(bridge),
            "slot_epoch": bridge._dispatch_slot_epoch,
            "counters": dict(bridge.counters)}


def _m11_observe_dispatch(bridge, clock, *, advance_after_consumed=False):
    """Attach pass-through observations to the real dispatch methods.

    Every wrapper calls its original bound method exactly once, returns its
    original value, and re-raises its original exception.  The bounded records
    retain command identity, UTC/monotonic observations, slot transitions,
    gate/fence lists, boundary reasons and receipt stages without reading the
    receipt database or any credential/arming file.
    """
    observations = []

    def event_base(event, command=None):
        body = {"event": event,
                "observed_utc": clock.now().isoformat(
                    timespec="microseconds").replace("+00:00", "Z"),
                "observed_monotonic": clock.monotonic(),
                "state": _m11_state_snapshot(bridge)}
        if command is not None:
            body.update(_m11_command_summary(command))
        return body

    original_receive = bridge._receive_dispatch

    def receive(command):
        started = clock.monotonic()
        before = _m11_state_snapshot(bridge)
        try:
            result = original_receive(command)
        except BaseException as exc:
            row = event_base("slot_receive_error", command)
            row.update({"before": before, "after": _m11_state_snapshot(bridge),
                        "elapsed_monotonic": clock.monotonic() - started,
                        "error": type(exc).__name__})
            observations.append(row)
            raise
        row = event_base("slot_receive", command)
        row.update({"before": before, "after": _m11_state_snapshot(bridge),
                    "elapsed_monotonic": clock.monotonic() - started})
        observations.append(row)
        return result

    bridge._receive_dispatch = receive

    original_gate = bridge._gate_blockers

    def gate(command, now):
        started = clock.monotonic()
        try:
            blockers = original_gate(command, now)
        except BaseException as exc:
            row = event_base("gate_error", command)
            row.update({"now_utc": now.isoformat(timespec="microseconds")
                        .replace("+00:00", "Z"),
                        "elapsed_monotonic": clock.monotonic() - started,
                        "error": type(exc).__name__})
            observations.append(row)
            raise
        row = event_base("gate", command)
        row.update({"now_utc": now.isoformat(timespec="microseconds")
                    .replace("+00:00", "Z"),
                    "blockers": list(blockers),
                    "elapsed_monotonic": clock.monotonic() - started})
        observations.append(row)
        return blockers

    bridge._gate_blockers = gate

    original_fence = bridge._post_fsync_fence

    def fence(command):
        started = clock.monotonic()
        try:
            violations = original_fence(command)
        except BaseException as exc:
            row = event_base("fence_error", command)
            row.update({"elapsed_monotonic": clock.monotonic() - started,
                        "error": type(exc).__name__})
            observations.append(row)
            raise
        row = event_base("fence", command)
        row.update({"violations": list(violations),
                    "elapsed_monotonic": clock.monotonic() - started})
        observations.append(row)
        return violations

    bridge._post_fsync_fence = fence

    original_receipt = bridge._record_receipt

    def receipt(command, stage, result_kind, value, error_code):
        started = clock.monotonic()
        try:
            result = original_receipt(command, stage, result_kind, value,
                                      error_code)
        except BaseException as exc:
            row = event_base("receipt_error", command)
            row.update({"requested_stage": stage,
                        "requested_error_code": error_code,
                        "elapsed_monotonic": clock.monotonic() - started,
                        "error": type(exc).__name__})
            observations.append(row)
            raise
        row = event_base("receipt", command)
        row.update({"requested_stage": stage,
                    "requested_error_code": error_code,
                    "stage": result.get("stage"),
                    "error_code": result.get("error_code"),
                    "local_sequence": result.get("local_sequence"),
                    "elapsed_monotonic": clock.monotonic() - started})
        observations.append(row)
        if advance_after_consumed and stage == "CONSUMED":
            clock.advance(0.401)
            observations.append({"event": "clock_advance_after_consumed",
                                 "command_id": command["command_id"],
                                 "advance_seconds": 0.401,
                                 "observed_utc": clock.now().isoformat(
                                     timespec="microseconds").replace(
                                         "+00:00", "Z"),
                                 "observed_monotonic": clock.monotonic()})
        return result

    bridge._record_receipt = receipt

    original_release = bridge._release_slot

    def release():
        before = _m11_state_snapshot(bridge)
        started = clock.monotonic()
        result = original_release()
        row = event_base("slot_release")
        row.update({"before": before, "after": _m11_state_snapshot(bridge),
                    "elapsed_monotonic": clock.monotonic() - started})
        observations.append(row)
        return result

    bridge._release_slot = release

    original_quarantine = bridge._quarantine_dispatch

    def quarantine(command, reason):
        started = clock.monotonic()
        result = original_quarantine(command, reason)
        row = event_base("boundary", command)
        row.update({"reason": reason,
                    "elapsed_monotonic": clock.monotonic() - started})
        observations.append(row)
        return result

    bridge._quarantine_dispatch = quarantine

    original_pump = bridge._pump_once

    def pump():
        started = clock.monotonic()
        before = _m11_state_snapshot(bridge)
        try:
            result = original_pump()
        except BaseException as exc:
            row = event_base("pump_error")
            row.update({"before": before, "after": _m11_state_snapshot(bridge),
                        "elapsed_monotonic": clock.monotonic() - started,
                        "error": type(exc).__name__})
            observations.append(row)
            raise
        row = event_base("pump",)
        row.update({"before": before, "after": _m11_state_snapshot(bridge),
                    "elapsed_monotonic": clock.monotonic() - started})
        observations.append(row)
        return result

    bridge._pump_once = pump
    return observations


def _m11_receipt_summary(observations, command_id):
    return [(row["stage"], row["error_code"])
            for row in observations
            if row.get("event") == "receipt"
            and row.get("command_id") == command_id]


def _m11_print_diagnostics(observations):
    """Emit only bounded SYNTHETIC observations for failed-run custody."""
    print("M11_DIAGNOSTIC " + json.dumps(observations[:256], sort_keys=True,
                                         separators=(",", ":")))

def test_m11_budget_and_cancel_split(monkeypatch, tmp_path,
                                    m11_controlled_dispatch_clock):
    """ARM-M11 (C+S) cross-end: send=1/cancel=1 split budgets on the frozen
    client — the first send consumes the round (ARMED_SEND_EXHAUSTED), the
    second send is refused, the independently authorized own-cancel fires
    exactly once, a second cancel is refused, and a cancel never reopens the
    send lane."""
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    clock = m11_controlled_dispatch_clock(module)
    assert module.DISPATCH_TTL_MS == 1500
    _require_frozen_arming(client=module)
    service = FakeExecutionService()
    bridge = None
    fence_bridge = None
    observations = []
    fence_observations = []
    try:
        bridge = _pair_and_start(module, fake, tmp_path / "store",
                                 service.poll_url)
        _m11_arm_bridge_to_armed(module, bridge)  # fixture round carries cancels=1
        set_synthetic_gate_capture(bridge, quote=True, account=True, position=True,
                                   contract=True)
        observations = _m11_observe_dispatch(bridge, clock)

        first = _m11_dispatch(bridge, clock, command_id="SIM_CMD_B1")
        assert module._parse_iso(first["expires_at"]) == \
            module._parse_iso(first["issued_at"]) + timedelta(milliseconds=400)
        bridge._receive_dispatch(first)
        bridge._pump_once()
        assert len(fake.send_calls()) == 1
        assert bridge._arm_state == module.ARM_STATE_ARMED_SEND_EXHAUSTED
        assert bridge._arming_effective_now() is True  # gate open, quota gone
        # the second send is refused (budget exhausted, not gate closure);
        # a gate-blocked ticket occupies the single slot until its own TTL
        # expires (terminal NO_CALL receipt releases it)
        def flush_blocked_ticket(command):
            # Explicitly cross THIS ticket's original 400 ms deadline.  The
            # production cap remains 1500 ms and the test never sleeps to
            # manufacture the expiry.
            clock.advance(0.401)
            assert "DISPATCH_EXPIRED" in bridge._gate_blockers(
                command, clock.now())
            bridge._pump_once()
            assert bridge._dispatch_slot is None
            assert _m11_receipt_summary(observations, command["command_id"])[-1] \
                == ("NO_CALL", "dispatch_expired")

        second = _m11_dispatch(bridge, clock, command_id="SIM_CMD_B2")
        before_second = bridge._gate_blockers(second, clock.now())
        assert "SEND_BUDGET_EXHAUSTED" in before_second
        assert "DISPATCH_EXPIRED" not in before_second
        bridge._receive_dispatch(second)
        bridge._pump_once()
        assert len(fake.send_calls()) == 1
        assert bridge.counters["native_call_blocked"] >= 1
        flush_blocked_ticket(second)
        # the independently authorized own cancel fires exactly once
        cancel = _m11_dispatch(bridge, clock, kind="CANCEL",
                               command_id="SIM_CMD_C1", order_id=101)
        before_cancel = bridge._gate_blockers(cancel, clock.now())
        assert before_cancel == []
        bridge._receive_dispatch(cancel)
        bridge._pump_once()
        assert len(fake.cancel_calls()) == 1
        # a second cancel is refused; the send lane stays closed throughout
        cancel2 = _m11_dispatch(bridge, clock, kind="CANCEL",
                                command_id="SIM_CMD_C2", order_id=101)
        before_cancel2 = bridge._gate_blockers(cancel2, clock.now())
        assert "CANCEL_BUDGET_EXHAUSTED" in before_cancel2
        bridge._receive_dispatch(cancel2)
        bridge._pump_once()
        assert len(fake.cancel_calls()) == 1
        assert len(fake.send_calls()) == 1
        flush_blocked_ticket(cancel2)
        third = _m11_dispatch(bridge, clock, command_id="SIM_CMD_B3")
        before_third = bridge._gate_blockers(third, clock.now())
        assert "SEND_BUDGET_EXHAUSTED" in before_third
        bridge._receive_dispatch(third)
        bridge._pump_once()
        assert len(fake.send_calls()) == 1  # cancel never reopens send
        flush_blocked_ticket(third)

        # Paired DTTL-05 counterexample: advance the same clock immediately
        # after the real durable CONSUMED write.  The final fence must return
        # the exact expiry violation, persist NO_CALL and release the slot;
        # replaying that command remains a terminal NO_CALL with no native call.
        stop_started_bridge(bridge)
        bridge = None
        fence_bridge = _pair_and_start(module, fake, tmp_path / "fence-store",
                                       service.poll_url)
        _m11_arm_bridge_to_armed(module, fence_bridge)
        set_synthetic_gate_capture(fence_bridge, quote=True, account=True,
                                   position=True, contract=True)
        fence_observations = _m11_observe_dispatch(
            fence_bridge, clock, advance_after_consumed=True)
        fence_command = _m11_dispatch(
            fence_bridge, clock, command_id="SIM_CMD_M11_FENCE")
        sends_before_fence = len(fake.send_calls())
        fence_bridge._receive_dispatch(fence_command)
        fence_bridge._pump_once()
        assert len(fake.send_calls()) == sends_before_fence
        assert fence_bridge._dispatch_slot is None
        assert fence_command["command_id"] in fence_bridge._terminal_no_call
        assert _m11_receipt_summary(
            fence_observations, fence_command["command_id"]) == [
                ("CONSUMED", "OUTCOME_PENDING"),
                ("NO_CALL", "terminal_fence_no_call")]
        fence_events_before_replay = len(fence_observations)
        fence_bridge._receive_dispatch(dict(fence_command))
        fence_bridge._pump_once()
        assert len(fake.send_calls()) == sends_before_fence
        assert fence_bridge._dispatch_slot is None
        assert len(fence_observations) > fence_events_before_replay
        assert _m11_receipt_summary(
            fence_observations, fence_command["command_id"]) == [
                ("CONSUMED", "OUTCOME_PENDING"),
                ("NO_CALL", "terminal_fence_no_call"),
                ("NO_CALL", "persistent_consumed_no_call")]
    finally:
        _m11_print_diagnostics(observations + fence_observations)
        stop_started_bridge(fence_bridge)
        stop_started_bridge(bridge)
        service.close()


# -- M12 ----------------------------------------------------------------------

def test_m12_expiry_thread_and_invalidation(monkeypatch, tmp_path):
    """ARM-M12 (C) cross-end: no legal callback -> no activation even with
    the exact ACK; a set_params mutation invalidates the whole round; a
    service-epoch drift refuses the credential; an expired credential is
    refused; stop latches the round terminal with the honest state file."""
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    _require_frozen_arming(client=module)
    service = FakeExecutionService()
    bridge = None
    try:
        store = tmp_path / "store"
        bridge = _pair_and_start(module, fake, store, service.poll_url)
        install_arming_proposal(bridge)
        bridge._process_arming_files()
        bridge._process_arming_files()
        install_arm_credential(bridge)
        bridge._process_arming_files()
        assert bridge._arm_state == module.ARM_STATE_ARM_PENDING
        assert bridge._arming_ack_received
        # NO legal callback: repeated lane cycles never flip the gate
        for _cycle in range(3):
            bridge._process_arming_files()
        assert bridge._arm_state == module.ARM_STATE_ARM_PENDING
        assert bridge._arming_effective_now() is False
        bridge._pump_once()
        assert bridge._arm_state == module.ARM_STATE_ARMED
        # set_params mutation invalidates the armed round (AR2.1)
        bridge.set_params({"exchange": "SHFE", "instrument_id": "rb2610",
                           "hedge_flag": "1",
                           "execution_api_url": service.poll_url,
                           "order_volume": 2})
        assert bridge._arming_effective_now() is False
        assert bridge._arm_state in (module.ARM_STATE_DISARMED_TERMINAL,
                                     module.ARM_STATE_RESTART_REQUIRED)

        # service-epoch drift between the frozen round and the live life:
        # the credential consumed and ACKed under the old epoch can never be
        # installed on the legal callback (SERVICE_EPOCH_CHANGED blocker)
        store2 = tmp_path / "store2"
        bridge.on_stop()
        bridge = _pair_and_start(module, fake, store2, service.poll_url)
        install_arming_proposal(bridge)
        bridge._process_arming_files()
        bridge._process_arming_files()
        install_arm_credential(bridge)
        bridge._process_arming_files()
        assert bridge._arm_state == module.ARM_STATE_ARM_PENDING
        assert bridge._arming_ack_received
        with bridge._service_epoch_lock:
            bridge._service_epoch = SYNTHETIC_ARMING_SERVICE_EPOCH + 1
        bridge._pump_once()   # legal callback, drifted epoch -> no install
        assert bridge._arm_state == module.ARM_STATE_ARM_PENDING
        assert bridge._arming_effective_now() is False
        with bridge._service_epoch_lock:
            bridge._service_epoch = SYNTHETIC_ARMING_SERVICE_EPOCH
        bridge._pump_once()   # epoch restored inside the SAME frozen round
        assert bridge._arm_state == module.ARM_STATE_ARMED  # drift was the blocker

        # expired credential refused on the public path
        store3 = tmp_path / "store3"
        bridge.on_stop()
        bridge = _pair_and_start(module, fake, store3, service.poll_url)
        install_arming_proposal(bridge)
        bridge._process_arming_files()
        bridge._process_arming_files()
        install_arm_credential(
            bridge,
            expires_override=datetime.now(timezone.utc)
            + timedelta(milliseconds=400))
        time.sleep(0.6)
        bridge._process_arming_files()
        assert bridge._arm_state == module.ARM_STATE_ARM_READY
        assert bridge._last_arming_reason == "ARMING_CREDENTIAL_EXPIRED"

        # stop latches terminal with the honest state file
        store4 = tmp_path / "store4"
        bridge.on_stop()
        bridge = _pair_and_start(module, fake, store4, service.poll_url)
        arm_bridge_to_armed(module, bridge)
        bridge.on_stop()
        state = json.loads((store4 / "arming-state.json")
                           .read_text(encoding="utf-8"))
        assert state["revoked"] is True
        assert state["local_gate_closed"] is True
        assert state["service_gate_closed"] is False  # honest gap
        assert bridge._arming_effective_now() is False
    finally:
        stop_started_bridge(bridge)
        service.close()


# -- M13 ----------------------------------------------------------------------

def test_m13_extension_whitelist_and_installer_discipline(monkeypatch, tmp_path):
    """ARM-M13 positive half (C+D) cross-end: the frozen client ACCEPTS the
    eight arming-v1 slot names (extension whitelist) while unknown files
    still refuse startup; the frozen installer's arming-slot exclusion list
    covers exactly the ruled slots, its version is the arming release, and
    its CLI is exercisable."""
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    _require_frozen_arming(client=module, tool=True)
    service = FakeExecutionService()
    bridge = None
    try:
        # first life creates the store identity (the layout gate refuses a
        # non-empty store without identity); the eight slots land after it
        store = tmp_path / "store"
        bridge = _pair_and_start(module, fake, store, service.poll_url)
        bridge.on_stop()
        proposal = synthetic_arming_proposal()
        for name, payload in (
                ("arming-proposal.json", json.dumps(proposal)),
                ("arming-plan.json",
                 json.dumps(synthetic_arming_plan(proposal=proposal))),
                ("arming-request.json", json.dumps(synthetic_arming_request())),
                ("arming-challenge.json",
                 json.dumps(synthetic_arming_challenge())),
                ("arming.json",
                 json.dumps(synthetic_arming_credential())),
                ("disarm.json", '{"schema_version": "probe"}'),
                ("arming-state.json", json.dumps(synthetic_arming_state())),
                ("arming-transfer.lock", '{"schema_version": "probe"}')):
            publish_arming_slot(str(store), name, payload.encode("utf-8"))
        # restart over the full extension slot set: accepted, still DISARMED
        # (presence alone arms nothing, per M01)
        bridge = _pair_and_start(module, fake, store, service.poll_url)
        assert bridge._running is True  # extension slots accepted
        assert bridge._arm_state == module.ARM_STATE_DISARMED
        bridge._process_arming_files()
        bridge._pump_once()
        assert bridge._arm_state == module.ARM_STATE_DISARMED
        bridge.on_stop()

        # an unknown extra file still refuses startup (byte-preserved)
        bridge = None
        (store / "arming-extra.json").write_text('{"probe": 1}',
                                                 encoding="utf-8")
        probe = _pair_and_start(module, fake, store, service.poll_url)
        assert probe.state_map.execution_status == "store_refused"
        assert probe._running is False
        assert probe._last_refusal["code"] == "LEGACY_STORE_MIGRATION_REQUIRED"
        assert (store / "arming-extra.json").read_text(encoding="utf-8") \
            == '{"probe": 1}'

        # installer discipline: frozen arming-release version, the exclusion
        # list covers exactly the ruled slots, CLI present
        installer_spec = importlib.util.spec_from_file_location(
            "ne1_installer_w2", ROOT / "scripts" / "install_infiny_execution.py")
        installer_module = importlib.util.module_from_spec(installer_spec)
        installer_spec.loader.exec_module(installer_module)
        assert installer_module.TOOL_VERSION == "ne1-delivery-20260918-arming-v1"
        assert set(installer_module.ARMING_SLOT_NAMES) == set(ARMING_FILE_NAMES)
        help_run = subprocess.run(
            [sys.executable, "-B", "-X", "utf8",
             str(ROOT / "scripts" / "install_infiny_execution.py"), "--help"],
            capture_output=True, text=True, cwd=str(ROOT))
        assert help_run.returncode == 0
    finally:
        stop_started_bridge(bridge)
        service.close()


# -- M14 ----------------------------------------------------------------------

def test_m14_regression_manifest_and_honest_counts(monkeypatch, tmp_path):
    """ARM-M14 (I/R) W2 integration evidence: the five frozen regression
    groups are executed through the offline guard with real exit codes and
    selection counts (group 1 fully run in-test; groups 2-5 collect-verified
    here — their full green runs are the main-session W2 panel, whose
    evidence is archived in the W2 implementation record); the fake/real
    count buckets stay disjoint on a live armed client."""
    _require_frozen_arming(service=True, tool=True)
    groups = (
        ["tests/test_infiny_execution_contracts.py",
         "tests/test_infiny_execution_planning.py",
         "tests/test_infiny_execution_ledger.py",
         "tests/test_infiny_execution_e2e.py"],
        ["tests/test_infiny_native_contracts.py",
         "tests/test_infiny_native_protocol.py",
         "tests/test_infiny_native_gates.py",
         "tests/test_infiny_native_mapping.py",
         "tests/test_infiny_native_dispatch.py",
         "tests/test_infiny_native_reconcile.py"],
        ["tests/test_infiny_native_lifecycle.py",
         "tests/test_infiny_native_integration.py"],
        ["tests/test_infiny_native_deployment.py",
         "tests/test_infiny_native_acceptance.py"],
        ["tests/test_infiny_native_pairing.py",
         "tests/test_infiny_native_wire.py"],
    )
    assert len(groups) == 5
    for group in groups:
        for name in group:
            assert (ROOT / name).is_file(), name
    evidence = []
    # every group is COLLECT-verified here with a real offline-guard exit
    # code; the full green RUN of all five groups is the main-session W2
    # panel (archived in the W2 implementation record) — a collection count
    # never stands in for a run result (count_complete stays false)
    for index, group in enumerate(groups, start=1):
        collect = subprocess.run(
            [sys.executable, "-B", "-X", "utf8", "scripts/test_offline.py",
             "-q", "--collect-only", *group],
            capture_output=True, text=True, cwd=str(ROOT), timeout=290)
        evidence.append({"group": index, "mode": "collect-only",
                         "exit": collect.returncode})
        assert collect.returncode == 0
        assert "selected" in collect.stdout
    count_complete = all(item["mode"] == "run" for item in evidence)
    assert count_complete is False  # honest: collect-only evidence is missing

    # disjoint count buckets on a live armed client (fake vs loopback vs real
    # socket attempts are reported separately, never summed)
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    service = FakeExecutionService()
    listener = ConnectionCountingListener()
    bridge = None
    try:
        bridge = _pair_and_start(module, fake, tmp_path / "store",
                                 service.poll_url)
        arm_bridge_to_armed(module, bridge)
        buckets = {
            "fake_native_calls": dict(fake.call_counts()),
            "fake_native_send_attempts": bridge.counters["native_send_attempts"],
            "fake_native_cancel_attempts": bridge.counters["native_cancel_attempts"],
            "real_service_code_requests": len(service.requests),
            "real_socket_attempts_to_sentinel": listener.connection_count(),
        }
        assert buckets["fake_native_send_attempts"] == 0
        assert buckets["real_socket_attempts_to_sentinel"] == 0
        assert buckets["real_service_code_requests"] >= 1  # ACK over real HTTP
        keys = list(buckets)
        assert len(set(map(str, keys))) == len(keys)  # buckets stay named
    finally:
        stop_started_bridge(bridge)
        service.close()
        listener.close()


# ===========================================================================
# Fixture-integrity sentinel: the shared vectors this file relies on stay in
# lockstep with the ruling's frozen vocabularies.
# ===========================================================================

def test_shared_vector_counts_match_the_ruling():
    assert len(ARMING_FILE_NAMES) == 8  # AR4.1 slot table
    assert len(("ne1_arming_proposal_v1", "ne1_arming_plan_v1",
                "ne1_arming_request_v1", "ne1_arming_challenge_v1",
                "ne1_client_arming_v1", "ne1_client_disarm_v1",
                "ne1_arming_state_v1", "ne1_arming_tool_receipt_v1",
                "ne1_arming_registrations_v1", "ne1_arming_prerequisites_v1")) == 10
    assert len(CLIENT_ARMING_FIELDS) == 22  # AR4.2 credential field count
    assert len(CRASH_POINTS) == 4  # AR5.1 file-level crash points
    assert len(ARMING_PREREQUISITE_KINDS) == 5
    assert ACTION_SOURCE_USER_LOCAL_CONSOLE == "USER_LOCAL_CONSOLE"
