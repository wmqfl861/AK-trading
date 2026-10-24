"""OV-F02..OV-F05 repair verification: REAL C1.1 wire formats (not fakes).

Every test encodes/decodes with the in-repo frozen native_contracts (C1.1)
and drives the standalone client file over real loopback HTTP. SYNTHETIC
identities only; no production port, no credentials, no native platform.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import sys
import threading
import time
import types
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from ak_trading.infiny_execution import contracts as c
from ak_trading.infiny_execution import native_contracts as nc
from ak_trading.infiny_bridge.protocol import normalize_payload  # engine wire helpers

from infiny_native_fakes import (
    SYNTHETIC_PAIRING_TOKEN,
    FakeExecutionService,
    TempExecutionStack,
    arm_bridge_to_armed,
    get_status,
    inject_arming_service_seams,
    install_fake_native,
    load_client_from_temp,
    post_json,
    set_synthetic_gate_capture,
    stop_started_bridge,
    synthetic_binding,
    synthetic_dispatch,
    synthetic_offer,
)
from test_infiny_bridge import FIXED_NOW, _payload

CLIENT_PATH = ROOT / "integrations/infiny/AIBridge.py"  # never touched
EXEC_PATH = ROOT / "integrations/infiny/AKExecutionBridge.py"


def load_client(monkeypatch, tmp_path):
    module, fake = load_client_from_temp(monkeypatch, tmp_path)
    return module, fake


def make_binding(module):
    return {
        "account_key": "SIM_ACCOUNT_A", "strategy_key": "SIM_STRATEGY_S",
        "strategy_id": 23, "instance_id": "SIM_INSTANCE", "session_epoch": "SIM_EPOCH",
        "trading_day": "2026-09-14", "exchange": "SHFE", "instrument_id": "rb2610",
        "hedge_flag": "1"}


def _nudge_control_lane(bridge):
    """Advance the client control lane past its 5 s disabled cadence (only
    scheduling; never identity or params)."""
    with bridge._state_lock:
        lane = bridge._lanes.get("control")
    if lane is not None:
        lane.wake.set()


# ---------------------------------------------------------------------------
# OV-F02: pairing token header + PollRequest declared snapshot fields
# ---------------------------------------------------------------------------

def test_f02_poll_request_decodes_under_frozen_engine(monkeypatch, tmp_path):
    """The client-built PollRequest body must decode with nc.PollRequest
    (every declared field present, including explicit nulls)."""
    module, fake = load_client(monkeypatch, tmp_path)
    bridge = module.AKExecutionBridge(native=fake, store_directory=tmp_path / "store")
    bridge.strategy_id = 23
    bridge.set_params({"exchange": "SHFE", "instrument_id": "rb2610", "hedge_flag": "1"})
    bridge.on_init()
    bridge.on_start()  # identity exists only after a valid start (LM-C R1.2)
    try:
        packet = bridge._envelope({
            "snapshot": None, "snapshot_id": None,
            "client_state_digest": "ab" * 32, "ack_watermark": None}, "PollRequest")
        # engine decode requires the exact declared set including snapshot
        decoded = nc.decode(json.dumps(packet), nc.PollRequest)
        assert decoded.snapshot is None and decoded.snapshot_id is None
    finally:
        bridge.on_stop()


def test_f02_snapshot_by_value_leaves_received_at_null(monkeypatch, tmp_path):
    module, fake = load_client(monkeypatch, tmp_path)
    bridge = module.AKExecutionBridge(native=fake, store_directory=tmp_path / "store")
    bridge.strategy_id = 23
    bridge.set_params({"exchange": "SHFE", "instrument_id": "rb2610", "hedge_flag": "1"})
    bridge.on_init()
    bridge.on_start()
    try:
        binding = make_binding(module)
        stamp = datetime.now(timezone.utc)

        def timing():
            return c.ComponentTiming(sampled_at=stamp, source_at=stamp, received_at=None,
                                     snapshot_id=None, source=c.Source.PYTHONGO,
                                     completeness=c.Completeness.UNKNOWN)
        engine_snapshot = c.ExecutionSnapshot(
            snapshot_id="snap-1", binding=c.Binding(**{**binding, "trading_day": date(2026, 9, 14)}),
            source=c.Source.PYTHONGO,
            times=c.SnapshotTimes(sampled_at=stamp, sent_at=stamp, received_at=None,
                                  quote=timing(), contract=timing(),
                                  account=timing(), positions=timing()),
            quote=None, contract=None, account=None, positions=(),
            coverage=c.Coverage(level=c.CoverageLevel.OWN_JOURNAL,
                                account_key="SIM_ACCOUNT_A", strategy_key=None,
                                session_epoch="SIM_EPOCH", trading_day=date(2026, 9, 14),
                                source=c.Source.PYTHONGO, as_of=None, complete_through=None,
                                completion_evidence_id=None, own_active_count=None,
                                account_active_count=None),
            proof_id=None, ledger_revision=0)
        wire = json.loads(nc.encode(engine_snapshot))
        packet = bridge._envelope({
            "snapshot": wire["data"], "snapshot_id": engine_snapshot.snapshot_id,
            "client_state_digest": "ab" * 32, "ack_watermark": None}, "PollRequest")
        decoded = nc.decode(json.dumps(packet), nc.PollRequest)
        assert decoded.snapshot is not None
        assert decoded.snapshot.times.received_at is None  # service stamps receipt
    finally:
        bridge.on_stop()


def test_f02_token_header_on_wire_and_persisted(monkeypatch, tmp_path):
    module, fake = load_client(monkeypatch, tmp_path)
    captured = {}

    def transport(url, payload, timeout, headers=None):
        captured["headers"] = dict(headers or {})
        captured["url"] = url
        return 200, module.canonical_dumps({
            "schema_version": "infiny_client_execution_transport_v1",
            "type": "PollResponse",
            "data": dict(payload, received_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                         ledger_revision=1, control_revision=1, kind="IDLE",
                         offer=None, blockers=[], readiness={})}).encode()

    bridge = module.AKExecutionBridge(native=fake, transport=transport,
                                      store_directory=tmp_path / "store",
                                      pairing_token=SYNTHETIC_PAIRING_TOKEN)
    bridge.strategy_id = 23
    bridge.set_params({"exchange": "SHFE", "instrument_id": "rb2610", "hedge_flag": "1"})
    bridge.on_init()
    bridge.on_start()
    try:
        _nudge_control_lane(bridge)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and "headers" not in captured:
            time.sleep(0.02)
    finally:
        stop_started_bridge(bridge)
    assert captured.get("headers", {}).get("X-Execution-Token") == SYNTHETIC_PAIRING_TOKEN
    # persisted for restart continuity (client-runtime storage)
    assert (tmp_path / "store").exists()


# ---------------------------------------------------------------------------
# OV-F03: ReadyResponse wire type acceptance
# ---------------------------------------------------------------------------

def test_f03_real_engine_ready_response_dispatch_is_accepted(monkeypatch, tmp_path):
    module, fake = load_client(monkeypatch, tmp_path)
    bridge = module.AKExecutionBridge(native=fake, store_directory=None)
    bridge.strategy_id = 23
    bridge.set_params({"exchange": "SHFE", "instrument_id": "rb2610"})
    binding = make_binding(module)
    issued = datetime.now(timezone.utc)
    dispatch = module.validate_dispatch_command({
        "command_id": "SIM_CMD_1", "kind": "SUBMIT", "attempt_id": "SIM_ATTEMPT_1",
        "offer_id": "SIM_OFFER_1", "intent_id": "SIM_INTENT_1", "leg_id": "SIM_LEG_1",
        "plan_id": "cd" * 32, "binding": binding,
        "leg": {"leg_id": "SIM_LEG_1", "index": 0, "phase": "OPEN", "depends_on": [],
                "direction": "BUY", "offset": "OPEN", "volume": 1, "price": "3500",
                "position_bucket": None, "cash_reserve": "1000"},
        "order_type": "GFD", "price_tick": "1",
        "contract_limits": {"min_volume": 1, "max_volume": 5, "volume_step": 1},
        "control_revision": 1, "permit_id": "SIM_PERMIT_TEST",
        "params_sha256": "ab" * 32,
        "issued_at": issued.isoformat().replace("+00:00", "Z"),
        "expires_at": (issued + timedelta(milliseconds=400)).isoformat().replace("+00:00", "Z"),
        "order_id": None, "route_evidence_id": None, "cancel_id": None})
    ready = {
        "schema_version": "infiny_client_execution_transport_v1",
        "type": "ReadyResponse",
        "data": {"request_id": "req", "instance_id": binding["instance_id"],
                 "session_epoch": binding["session_epoch"], "generation": "SIM_GEN",
                 "sequence": 1, "params_sha256": "ab" * 32, "service_epoch": 1,
                 "sent_at": issued.isoformat(), "received_at": issued.isoformat(),
                 "kind": "DISPATCH", "dispatch": dispatch, "readiness": {}, "blockers": []}}
    command = bridge._extract_dispatch(ready)
    assert command is not None and command["command_id"] == "SIM_CMD_1"
    # BLOCKED variant carries no command
    blocked = {**ready, "data": {**ready["data"], "kind": "BLOCKED",
                                 "dispatch": None, "blockers": ["SERVICE_BLOCK"]}}
    assert bridge._extract_dispatch(blocked) is None
    with pytest.raises(module.ContractError):
        bridge._extract_dispatch({"type": "DispatchResponse", "data": {}})


def test_f03_engine_wire_type_is_readyresponse_golden():
    from test_infiny_native_gates import FIXTURE  # frozen golden fixture
    cases = FIXTURE.get("ready_response", [])
    if not cases:
        pytest.skip("fixture lacks ready_response vectors")
    for case in cases:
        assert nc.decode(json.dumps(case["wire"]), nc.ReadyResponse).kind.value == case["kind"]


# ---------------------------------------------------------------------------
# OV-F04: event stream identity + EventBatch assembly
# ---------------------------------------------------------------------------

def test_f04_engine_accepts_client_event_batch(monkeypatch, tmp_path):
    module, fake = load_client(monkeypatch, tmp_path)
    bridge = module.AKExecutionBridge(native=fake, store_directory=tmp_path / "store")
    bridge.strategy_id = 23
    bridge.set_params({"exchange": "SHFE", "instrument_id": "rb2610", "hedge_flag": "1"})
    bridge.on_init()
    bridge.on_start()
    try:
        bridge.on_order({"OrderID": "7", "Status": "未成交"})
        bridge.on_trade({"TradeID": "t-1", "Volume": 1})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            rows = bridge._stores.pending_events(16)
            if len(rows) >= 2:
                break
            time.sleep(0.02)
        events = []
        for event_id, body in rows:
            parsed = module.strict_loads(body, limit=module.MAX_REQUEST_BYTES)
            events.append(parsed["data"])
        packet = bridge._envelope({"events": events}, "EventBatch")
        stream = {item["event_stream_id"] for item in events}
        seqs = sorted(item["event_sequence"] for item in events)
        assert len(stream) == 1 and seqs == [1, 2]  # one stream, contiguous
        # the frozen engine decodes the client-built batch
        decoded = nc.decode(json.dumps(packet), nc.EventBatch)
        assert len(decoded.events) == 2
    finally:
        stop_started_bridge(bridge)


def test_f04_stream_reset_per_generation(monkeypatch, tmp_path):
    module, fake = load_client(monkeypatch, tmp_path)
    bridge = module.AKExecutionBridge(native=fake, store_directory=tmp_path / "store")
    bridge.strategy_id = 23
    bridge.set_params({"exchange": "SHFE", "instrument_id": "rb2610", "hedge_flag": "1"})
    bridge.on_init()
    bridge.on_start()
    try:
        first = bridge._event_stream_id()
        seq1 = bridge._next_event_sequence()
        bridge.on_stop()
        bridge.on_start()
        second = bridge._event_stream_id()
        assert first != second  # new generation -> new stream id
    finally:
        stop_started_bridge(bridge)


# ---------------------------------------------------------------------------
# OV-F05: control report upload on stop / gate closure
# ---------------------------------------------------------------------------

def test_f05_control_reports_uploaded_over_real_http(monkeypatch, tmp_path):
    module, fake = load_client(monkeypatch, tmp_path)
    service = FakeExecutionService()
    bridge = None
    try:
        bridge = module.AKExecutionBridge(native=fake, store_directory=tmp_path / "store",
                                          pairing_token=SYNTHETIC_PAIRING_TOKEN)
        bridge.strategy_id = 23
        bridge.set_params({"exchange": "SHFE", "instrument_id": "rb2610", "hedge_flag": "1",
                          "enable_trading": True,
                          "execution_api_url": service.poll_url})
        bridge.params_map.enable_trading = True  # inert on the frozen client
        bridge.on_init()
        bridge.on_start()
        # W2 arming-gate alignment: params alone never armed this life — the
        # stop chain below runs for a PAIRED-but-DISARMED session and every
        # report is still uploaded over real HTTP.
        assert bridge._arming_effective_now() is False
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not service.requests:
            time.sleep(0.02)
        bridge.on_stop()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(service.control_reports) < 3:
            time.sleep(0.02)
        # §5.3 two-party stop chain, W2 semantics (NE1-ARB-ARMING-001): the
        # gate closure sends GATE_CLOSED, the armed-round revocation latch
        # publishes its own local-gate-closed GATE_CLOSED (AR5.2), then the
        # stop chain closes with STOPPED so the service can confirm DISABLED.
        assert service.control_reports[:3] == ["GATE_CLOSED", "GATE_CLOSED", "STOPPED"]
        assert service.control_reports[-1] == "STOPPED"
        # every upload is a ControlReport envelope with no confirmation payload
        assert all(packet["type"] == "ControlReport"
                   and packet["data"]["confirmation"] is None
                   for packet in service.control_packets[:3])
        paths = {path for path, _ in service.requests}
        assert "/execution/v1/control" in paths
    finally:
        stop_started_bridge(bridge)
        service.close()


def test_f05_gate_closure_sends_gate_closed_control(monkeypatch, tmp_path):
    module, fake = load_client(monkeypatch, tmp_path)
    service = FakeExecutionService()
    bridge = None
    try:
        bridge = module.AKExecutionBridge(native=fake, store_directory=tmp_path / "store",
                                          pairing_token=SYNTHETIC_PAIRING_TOKEN)
        bridge.strategy_id = 23
        bridge.set_params({"exchange": "SHFE", "instrument_id": "rb2610", "hedge_flag": "1",
                          "execution_api_url": service.poll_url})
        bridge.on_init()
        bridge.on_start()
        bridge._close_gate("synthetic_revocation")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not service.control_reports:
            time.sleep(0.02)
        assert service.control_reports[0] == "GATE_CLOSED"
    finally:
        stop_started_bridge(bridge)
        service.close()


# ---------------------------------------------------------------------------
# Integrated disabled handshake end-to-end (real HTTP round trip)
# ---------------------------------------------------------------------------

def test_end_to_end_disabled_handshake_poll_round_trip(monkeypatch, tmp_path):
    """Real client over real loopback HTTP: poll accepted (200), service
    epoch learned, kind=IDLE; no native call, observer untouched."""
    module, fake = load_client(monkeypatch, tmp_path)
    service = FakeExecutionService()
    bridge = None
    try:
        bridge = module.AKExecutionBridge(native=fake, store_directory=tmp_path / "store",
                                          pairing_token=SYNTHETIC_PAIRING_TOKEN)
        bridge.strategy_id = 23
        bridge.set_params({"exchange": "SHFE", "instrument_id": "rb2610", "hedge_flag": "1",
                          "enable_trading": True,
                          "execution_api_url": service.poll_url})
        bridge.on_init()
        bridge.on_start()
        _nudge_control_lane(bridge)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not service.requests:
            time.sleep(0.02)
        assert service.requests, "disabled handshake never reached the service"
        path, packet = service.requests[0]
        assert path.endswith("/execution/v1/poll")
        assert packet["type"] == "PollRequest"
        assert "snapshot" in packet["data"]  # declared field on the wire
        nc.decode(json.dumps(packet), nc.PollRequest)  # engine-acceptable
        # no native send/cancel/attribute-bomb resolution occurred
        assert all(name not in ("sendOrder", "cancelOrder") for name, _, _ in fake.calls)
        assert fake.attribute_access == []
        bridge.on_stop()
    finally:
        stop_started_bridge(bridge)
        service.close()


# ---------------------------------------------------------------------------
# C1.2 dispatch TTL window: engine/mirror boundary agreement + a real HTTP
# round trip that consumes a dispatch inside the 1500 ms window
# (client-c1.2-ttl-prep.md test plan rows 1 and 9)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("lifetime_ms,accepted", [
    (500, True), (500.001, True), (1499.999, True), (1500, True),
    (1500.001, False),
])
def test_dttl01_engine_and_mirror_agree_on_ttl_boundaries(monkeypatch, tmp_path,
                                                          lifetime_ms, accepted):
    """Both sides of the contract accept the same lifetimes: everything up to
    the frozen 1500 ms cap, nothing beyond it (the 500-era boundary is gone)."""
    module, _ = load_client(monkeypatch, tmp_path)
    from dataclasses import replace
    from ak_trading.infiny_execution import contracts as c
    from ak_trading.infiny_execution.simulation import simulation_context
    from test_infiny_native_dispatch import submit_command
    context = simulation_context()
    now = context.clock.now
    issued = submit_command(context, issued_at=now - timedelta(milliseconds=10))
    expires = now - timedelta(milliseconds=10) + timedelta(milliseconds=lifetime_ms)
    mirrored = synthetic_dispatch(ttl_ms=lifetime_ms)
    if accepted:
        engine_command = replace(issued, expires_at=expires)
        assert module.validate_dispatch_command(mirrored) is not None
        assert (engine_command.expires_at - engine_command.issued_at) \
            .total_seconds() * 1000 <= module.DISPATCH_TTL_MS
    else:
        with pytest.raises(c.ContractError):
            replace(issued, expires_at=expires)
        with pytest.raises(module.ContractError):
            module.validate_dispatch_command(mirrored)


def test_dttl07_real_http_dispatch_consumed_within_1500ms_window(monkeypatch, tmp_path):
    """Real loopback HTTP poll -> OFFER -> /ready -> DISPATCH: the command
    inherits the offer's absolute deadline (DTTL-03) and is consumed with a
    single native call inside the 1500 ms window. W2 arming-gate alignment
    (NE1-ARB-ARMING-001): with NO arming-effective round the same offer is
    correctly ignored (reversed direction — no /ready, no dispatch); after the
    ruled local-slot arming the identical wire path flows end to end."""
    module, fake = load_client(monkeypatch, tmp_path)
    service = FakeExecutionService()
    synthetic_service_epoch = 5
    original_idle, original_offer = service._idle, service._offer

    def complete_poll_response(packet, response):
        header = packet["data"]
        response["data"].update({
            **{name: header[name] for name in ("request_id", "instance_id", "session_epoch",
                                              "generation", "sequence", "params_sha256")},
            "request_service_epoch": header["service_epoch"],
            "service_epoch": synthetic_service_epoch,
            "binding": synthetic_binding(instance_id=header["instance_id"],
                                           session_epoch=header["session_epoch"]),
        })
        return response

    service._idle = lambda packet: complete_poll_response(packet, original_idle(packet))
    service._offer = lambda packet, offer: complete_poll_response(packet, original_offer(packet, offer))
    bridge = None
    try:
        bridge = module.AKExecutionBridge(native=fake, store_directory=tmp_path / "store",
                                          pairing_token=SYNTHETIC_PAIRING_TOKEN)
        context_checks = []
        original_accept = bridge._accept_snapshot_context

        def observe_context(data, packet, request_life):
            header = packet["data"]
            binding = data.get("binding", {})
            expected_binding = {"instance_id": bridge._instance_id,
                                "session_epoch": bridge._session_epoch,
                                "strategy_id": bridge.strategy_id,
                                "exchange": bridge.params_map.exchange,
                                "instrument_id": bridge.params_map.instrument_id,
                                "hedge_flag": bridge.params_map.hedge_flag}
            check = {
                "echoes_match": all(data.get(name) == header[name] for name in
                                    ("request_id", "instance_id", "session_epoch",
                                     "generation", "sequence", "params_sha256")),
                "epoch_echo_matches": data.get("request_service_epoch") == header["service_epoch"],
                "service_epoch_matches": data.get("service_epoch") == bridge._service_epoch,
                "life_current": request_life == bridge._snapshot_life(),
                "pairing_hash_equals_raw_hash": request_life[3] == request_life[5],
                "wire_hash_equals_effective_hash": header["params_sha256"] == request_life[4],
                "binding_matches": all(binding.get(name) == value for name, value in expected_binding.items()),
                "pin_matches": bridge._snapshot_binding_pin in
                               (None, (request_life, module.canonical_dumps(binding))),
            }
            accepted = original_accept(data, packet, request_life)
            context_checks.append({**check, "accepted": accepted,
                                   "context_error": bridge._snapshot_errors.get("context")})
            return accepted

        monkeypatch.setattr(bridge, "_accept_snapshot_context", observe_context)
        bridge.strategy_id = 23
        bridge.set_params({"exchange": "SHFE", "instrument_id": "rb2610", "hedge_flag": "1",
                           "investor_id": "SYNTHETIC-ACCOUNT-A", "enable_trading": True,
                           "authorization_id": "SIM_AUTH", "risk_policy_id": "SIM_POLICY",
                           "max_native_sends": 1, "max_native_cancels": 1,
                           "execution_api_url": service.poll_url})
        bridge.on_init()
        bridge.on_start()
        bridge.params_map.enable_trading = True  # legacy re-arm — inert now
        inject_arming_service_seams(bridge)
        _nudge_control_lane(bridge)
        set_synthetic_gate_capture(bridge, quote=True, account=True, position=True, contract=True)
        binding = synthetic_binding(instance_id=bridge._instance_id,
                                    session_epoch=bridge._session_epoch)

        # -- reversed direction: unarmed, the offer is ignored (no /ready) --
        ignored_offer = synthetic_offer(binding=binding,
                                        params_sha256=bridge._params_sha256,
                                        offer_id="SIM_OFFER_IGNORED", expires_in_ms=1500)
        service.offers.append(ignored_offer)
        _nudge_control_lane(bridge)
        unarmed_deadline = time.monotonic() + 3
        while time.monotonic() < unarmed_deadline \
                and not any(path.endswith("/ready") for path, _ in service.requests):
            time.sleep(0.02)
        assert not any(path.endswith("/ready") for path, _ in service.requests), \
            "unarmed client must never pursue an execution offer"
        assert bridge._dispatch_slot is None
        assert len(fake.send_calls()) == 0
        assert bridge.state_map.control_status == "not_armed_offer_ignored"

        # -- armed positive path: the ruled chain, then the identical flow --
        # the inert legacy re-arm is rolled back to the safe projection so the
        # armed round is proven to need nothing but the ruled local slots
        bridge.params_map.enable_trading = False
        arm_bridge_to_armed(module, bridge)
        assert bridge._arming_effective_now() is True
        _nudge_control_lane(bridge)  # armed cadence starts on the next wake
        context_deadline = time.monotonic() + 3
        while time.monotonic() < context_deadline and not bridge._snapshot_context_current():
            time.sleep(0.02)
        assert bridge._snapshot_context_current(), {"SYNTHETIC_context_scope": context_checks[-8:]}
        set_synthetic_gate_capture(bridge, quote=True, account=True, position=True, contract=True)
        offer = synthetic_offer(binding=binding, params_sha256=bridge._effective_params_sha256,
                                offer_id="SIM_OFFER_E2E", expires_in_ms=1500)
        service.offers.append(offer)
        offer_expires = datetime.fromisoformat(offer["expires_at"].replace("Z", "+00:00"))

        def command_factory(packet):
            # DTTL-03 server rule mirrored by the fake: the command deadline is
            # min(offer.expires_at, t_issue + 1500 ms, D_current)
            t_issue = datetime.now(timezone.utc)
            command = synthetic_dispatch(binding=binding,
                                         params_sha256=bridge._effective_params_sha256,
                                         command_id="SIM_CMD_E2E", ttl_ms=1500)
            command["offer_id"] = "SIM_OFFER_E2E"
            command["permit_id"] = bridge._arm_context.permit_id
            command["control_revision"] = bridge._arm_context.control_revision
            command["expires_at"] = min(offer_expires,
                                        t_issue + timedelta(milliseconds=1500)) \
                .isoformat(timespec="microseconds").replace("+00:00", "Z")
            return command

        service.ready_commands.append(command_factory)
        started = time.monotonic()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and bridge._dispatch_slot is None:
            time.sleep(0.02)
        assert bridge._dispatch_slot is not None, "dispatch never arrived over real HTTP"
        assert time.monotonic() - started < 1.5  # still inside the 1500 ms window
        assert bridge._offer_deadlines.get("SIM_OFFER_E2E") == offer["expires_at"]
        bridge._pump_once()
        assert len(fake.send_calls()) == 1
        # the receipt chain CONSUMED -> RETURNED reaches the real /results lane
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len(service.receipts) < 2:
            time.sleep(0.02)
        assert [record["stage"] for record in service.receipts] == ["CONSUMED", "RETURNED"]
    finally:
        if bridge is not None:
            bridge.on_stop()
        service.close()


# ---------------------------------------------------------------------------
# NE1-LM-R4 / LM-I raw-wire additions (decision NE1-ARB-PAIRING-LASTMILE-001
# §5 M05 wire facets, §6 LM-I row). Unlike the FakeExecutionService above,
# these drive the REAL service stack — real Ledger (v2 journal) + real
# ExecutionService + real loopback HTTP — with raw frozen-codec packets and
# no client object: the wire boundary an operator probe or a foreign
# implementation would face. SYNTHETIC identities only.
# ---------------------------------------------------------------------------

def _wire_header(instance, session, service_epoch, *, generation="SIM_GEN_W",
                 sequence=1):
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    return {"request_id": uuid.uuid4().hex, "instance_id": instance,
            "session_epoch": session, "generation": generation,
            "service_epoch": service_epoch, "sequence": sequence,
            "sent_at": now, "params_sha256": "ab" * 32}


def _wire_poll(instance, session, *, service_epoch):
    return {"schema_version": "infiny_client_execution_transport_v1_1",
            "type": "PollRequest",
            "data": {**_wire_header(instance, session, service_epoch),
                     "snapshot": None, "snapshot_id": None,
                     "client_state_digest": "cd" * 32, "ack_watermark": None}}


def _wire_control(instance, session, *, service_epoch, kind="GATE_CLOSED"):
    return {"schema_version": "infiny_client_execution_transport_v1_1",
            "type": "ControlReport",
            "data": {**_wire_header(instance, session, service_epoch),
                     "kind": kind, "confirmation": None}}


def _wire_event(session, *, sequence, stream="SIM_STREAM_WIRE_1",
                generation="SIM_GEN_W", event_id=None, kind="order"):
    return {"event_id": event_id or f"SIM_EVT_WIRE_{sequence}", "kind": kind,
            "event_stream_id": stream, "event_sequence": sequence,
            "captured_at": datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
            "callback_generation": generation,
            "callback_session_epoch": session, "route_evidence_id": None,
            "raw_fields": [{"key": "OrderID", "value": f"WIRE-{sequence}"},
                           {"key": "Status", "value": "active"}]}


def _wire_stack(tmp_path):
    return TempExecutionStack(
        tmp_path, bindings=(synthetic_binding(instance_id="SIM_INSTANCE_A",
                                              session_epoch="SIM_EPOCH_A"),),
        tokens={"SIM_INSTANCE_A": SYNTHETIC_PAIRING_TOKEN})


def test_lm_wire_authenticated_routes_same_session(tmp_path):
    """M05/M04 wire facet over the REAL service: one registered binding +
    token serves the full authenticated business surface in one session —
    null-epoch poll handshake (BLOCKED), learned-epoch poll (IDLE), control
    report, event upload with durable ack, and GET status with the ten
    frozen headers."""
    stack = _wire_stack(tmp_path)
    try:
        base = stack.poll_url.rsplit("/", 1)[0]
        # initial disabled handshake: null epoch poll is metadata-only (BLOCKED)
        stack.clock.advance(5.05)
        status, body = post_json(stack.poll_url, _wire_poll(
            "SIM_INSTANCE_A", "SIM_EPOCH_A", service_epoch=None),
            token=SYNTHETIC_PAIRING_TOKEN, timeout=10.0)
        assert status == 200 and body["data"]["kind"] == "BLOCKED"
        epoch = body["data"]["service_epoch"]
        assert epoch == stack.service_epoch

        # learned epoch poll: ordinary IDLE cycle in the same session
        stack.clock.advance(5.05)
        status, body = post_json(stack.poll_url, _wire_poll(
            "SIM_INSTANCE_A", "SIM_EPOCH_A", service_epoch=epoch),
            token=SYNTHETIC_PAIRING_TOKEN, timeout=10.0)
        assert status == 200 and body["data"]["kind"] == "IDLE"

        # authenticated control report on the same credentials
        status, body = post_json(base + "/control", _wire_control(
            "SIM_INSTANCE_A", "SIM_EPOCH_A", service_epoch=epoch),
            token=SYNTHETIC_PAIRING_TOKEN, timeout=10.0)
        assert status == 200 and body["type"] == "ControlResponse"

        # event upload: durable ack with the ORIGINAL event ids preserved
        events = [_wire_event("SIM_EPOCH_A", sequence=1),
                  _wire_event("SIM_EPOCH_A", sequence=2)]
        batch = {"schema_version": "infiny_client_execution_transport_v1_1",
                 "type": "EventBatch",
                 "data": {**_wire_header("SIM_INSTANCE_A", "SIM_EPOCH_A", epoch),
                          "events": events}}
        status, body = post_json(base + "/events", batch,
                                 token=SYNTHETIC_PAIRING_TOKEN, timeout=10.0)
        assert status == 200 and body["data"]["persisted"] is True
        assert list(body["data"]["accepted_event_ids"]) == \
            [event["event_id"] for event in events]

        # redelivery under a fresh request id: no renumbering, no duplicate
        # persistence — the ids come back marked duplicate (M07 wire facet)
        replay = {"schema_version": "infiny_client_execution_transport_v1_1",
                  "type": "EventBatch",
                  "data": {**_wire_header("SIM_INSTANCE_A", "SIM_EPOCH_A", epoch,
                                          sequence=2),
                           "events": events}}
        status, body = post_json(base + "/events", replay,
                                 token=SYNTHETIC_PAIRING_TOKEN, timeout=10.0)
        assert status == 200
        assert list(body["data"]["accepted_event_ids"]) == \
            [event["event_id"] for event in events]
        assert list(body["data"]["duplicate_event_ids"]) == \
            [event["event_id"] for event in events]

        # GET status with the ten frozen headers closes the same-session sweep
        status, body = get_status(stack.poll_url, token=SYNTHETIC_PAIRING_TOKEN,
                                  instance_id="SIM_INSTANCE_A",
                                  session_epoch="SIM_EPOCH_A",
                                  generation="SIM_GEN_W", service_epoch=epoch,
                                  params_sha256="ab" * 32, timeout=10.0)
        assert status == 200 and body["type"] == "StatusResponse"
    finally:
        stack.close()


def test_lm_wire_token_and_identity_walls(tmp_path):
    """M05 wire counter-examples: no token header is a 401 before any body
    decode; a well-formed token worn by the wrong packet identity is a 403
    BINDING_MISMATCH; the registered token with an unregistered session is a
    403 BINDING_MISMATCH; a stale epoch on a business route is a 409
    EPOCH_MISMATCH; null epoch on a non-poll route is 409 HANDSHAKE_REQUIRED.
    X02 (X01 decision §5): an illegal service_epoch=0 value is 400
    INVALID_REQUEST at the decode boundary, decodable and never echoed."""
    stack = _wire_stack(tmp_path)
    try:
        base = stack.poll_url.rsplit("/", 1)[0]
        epoch = stack.service_epoch

        # missing token: authentication precedes everything (F03/F08 wire)
        status, body = post_json(stack.poll_url, _wire_poll(
            "SIM_INSTANCE_A", "SIM_EPOCH_A", service_epoch=None), token=None,
            timeout=10.0)
        assert (status, body["data"]["error_code"]) == (401, "AUTHENTICATION_FAILED")

        stack.clock.advance(5.05)
        # valid token, packet claims ANOTHER instance identity (F06)
        status, body = post_json(stack.poll_url, _wire_poll(
            "SIM_INSTANCE_B", "SIM_EPOCH_A", service_epoch=None),
            token=SYNTHETIC_PAIRING_TOKEN, timeout=10.0)
        assert (status, body["data"]["error_code"]) == (403, "BINDING_MISMATCH")

        stack.clock.advance(5.05)
        # registered token+instance but a session the binding never registered
        status, body = post_json(stack.poll_url, _wire_poll(
            "SIM_INSTANCE_A", "SIM_EPOCH_OTHER", service_epoch=None),
            token=SYNTHETIC_PAIRING_TOKEN, timeout=10.0)
        assert (status, body["data"]["error_code"]) == (403, "BINDING_MISMATCH")

        # stale epoch on the control route (any business route): 409 with a
        # POSITIVE stale integer (the original pre-X02 assertion, kept)
        status, body = post_json(base + "/control", _wire_control(
            "SIM_INSTANCE_A", "SIM_EPOCH_A", service_epoch=epoch + 1),
            token=SYNTHETIC_PAIRING_TOKEN, timeout=10.0)
        assert (status, body["data"]["error_code"]) == (409, "EPOCH_MISMATCH")

        # X02 raw-wire assertion (NE1-ARB-LM-X01-001 §5, fixed by X02-S):
        # service_epoch=0 is an illegal VALUE — not a stale integer — and is
        # rejected at the decode boundary with 400 INVALID_REQUEST on both
        # the poll route and a business route (the pre-fix 500 is gone); the
        # frozen codec still decodes the error body and the illegal zero is
        # never echoed back into request_service_epoch.
        for route, packet in (
                (stack.poll_url, _wire_poll("SIM_INSTANCE_A", "SIM_EPOCH_A",
                                            service_epoch=0)),
                (base + "/control", _wire_control("SIM_INSTANCE_A", "SIM_EPOCH_A",
                                                  service_epoch=0))):
            status, body = post_json(route, packet,
                                     token=SYNTHETIC_PAIRING_TOKEN, timeout=10.0)
            assert status == 400
            assert body["data"]["error_code"] == "INVALID_REQUEST"
            error = nc.decode(json.dumps(body), nc.TransportErrorResponse)
            assert error.request_service_epoch is None
            assert error.service_epoch == epoch

        # null epoch on a non-poll route: handshake refusal, not business
        status, body = post_json(base + "/control", _wire_control(
            "SIM_INSTANCE_A", "SIM_EPOCH_A", service_epoch=None),
            token=SYNTHETIC_PAIRING_TOKEN, timeout=10.0)
        assert (status, body["data"]["error_code"]) == (409, "HANDSHAKE_REQUIRED")
    finally:
        stack.close()
