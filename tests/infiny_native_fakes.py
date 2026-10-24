"""SYNTHETIC-only evidence registry and permit issuance for NE1 tests.

Everything issued here is marked SYNTHETIC and can never be registered by a
production registry; production CLI/HTTP paths cannot construct any of these
helpers. Fixtures built from this module must be flagged SYNTHETIC in their
manifests.
"""
from datetime import datetime, timedelta, timezone

from ak_trading.infiny_execution import contracts as c
from ak_trading.infiny_execution.native_contracts import NativePilotPermit, Origin
from ak_trading.infiny_execution.native_evidence import (
    CallbackKind,
    ClientThreadEvidence,
    EvidenceRegistry,
    NativeBaselineEvidence,
    NativeRouteEvidence,
    NativeSettlementEvidence,
    TestEvidenceFactory,
)

SYNTHETIC = "SYNTHETIC_TEST_ONLY"
# C1.1/C1.2 frozen transport schema the client mirror accepts on the wire.
TRANSPORT_SCHEMA = "infiny_client_execution_transport_v1_1"


class FakeEvidenceRegistry(EvidenceRegistry):
    """In-memory registry accepting only SYNTHETIC records for offline tests."""

    def __init__(self):
        self._permits: dict[str, NativePilotPermit] = {}
        self._baselines: dict[str, NativeBaselineEvidence] = {}
        self._routes: dict[str, NativeRouteEvidence] = {}
        self._threads: dict[str, ClientThreadEvidence] = {}
        self._settlements: dict[str, NativeSettlementEvidence] = {}

    def register_permit(self, permit: NativePilotPermit) -> None:
        if permit.origin is not Origin.SYNTHETIC:
            raise c.ContractError("test registry accepts SYNTHETIC permits only")
        self._permits[permit.permit_id] = permit

    def register_baseline(self, evidence: NativeBaselineEvidence) -> None:
        if evidence.origin is not Origin.SYNTHETIC:
            raise c.ContractError("test registry accepts SYNTHETIC evidence only")
        self._baselines[evidence.evidence_id] = evidence

    def register_route(self, evidence: NativeRouteEvidence) -> None:
        if evidence.origin is not Origin.SYNTHETIC:
            raise c.ContractError("test registry accepts SYNTHETIC evidence only")
        self._routes[evidence.evidence_id] = evidence

    def register_thread(self, evidence: ClientThreadEvidence) -> None:
        if evidence.origin is not Origin.SYNTHETIC:
            raise c.ContractError("test registry accepts SYNTHETIC evidence only")
        self._threads[evidence.evidence_id] = evidence

    def register_settlement(self, evidence: NativeSettlementEvidence) -> None:
        if evidence.origin is not Origin.SYNTHETIC:
            raise c.ContractError("test registry accepts SYNTHETIC evidence only")
        self._settlements[evidence.evidence_id] = evidence

    def verify_permit_active(self, permit_id: str, *, now: datetime) -> c.ValidationResult:
        permit = self._permits.get(permit_id)
        if permit is None:
            return c.ValidationResult(blockers=("NATIVE_PERMIT_UNREGISTERED",))
        if permit.expires_at <= now:
            return c.ValidationResult(blockers=("NATIVE_PERMIT_EXPIRED",))
        return c.ValidationResult(blockers=())


def issue_test_permit(*, binding: c.Binding, permit_id: str = "SIM_PERMIT_TEST",
                      authorization_id: str = "SIM_AUTHORIZATION", control_revision: int = 1,
                      max_volume: int = 1, valid_minutes: int = 5,
                      params_sha256: str) -> NativePilotPermit:
    """Wrap the frozen SYNTHETIC factory (send=1, cancel=0 by C1 design)."""
    factory = TestEvidenceFactory(purpose=SYNTHETIC)
    return factory.issue_permit(
        permit_id=permit_id, authorization_id=authorization_id, binding=binding,
        control_revision=control_revision, baseline_evidence_id="SIM_BASE_TEST",
        route_evidence_id="SIM_ROUTE_TEST", cash_bound_evidence_id="SIM_CASH_TEST",
        source_key="SIM_OPERATION_TEST", actions=(c.Action.OPEN_LONG,),
        max_volume=max_volume, expires_at=datetime.now(timezone.utc) + timedelta(minutes=valid_minutes),
        params_sha256=params_sha256)


def synthetic_control_state(context: c.ExecutionContext, *, control_revision: int = 1,
                            generation: str = "SIM_GEN_1", permit_id: str | None = None):
    """A ControlState matching an ExecutionContext's persisted controls."""
    from ak_trading.infiny_execution.native_contracts import ControlState, ExecutionMode
    authorization_id = (context.authorization.authorization_id
                        if context.authorization is not None else None)
    return ControlState(
        control_revision=control_revision, binding=context.binding, mode=context.mode,
        policy_id=context.policy.policy_id, authorization_id=authorization_id,
        native_permit_id=permit_id, params_sha256="ab" * 32, generation=generation,
        updated_at=context.clock.now, disabled_reason=None)


def synthetic_client_thread(binding: c.Binding, *, generation: str = "SIM_GEN_1",
                            evidence_id: str = "SIM_THREAD_TEST") -> ClientThreadEvidence:
    from ak_trading.infiny_execution.native_evidence import CallbackKind
    return ClientThreadEvidence(
        evidence_id=evidence_id, instance_id=binding.instance_id,
        session_epoch=binding.session_epoch, generation=generation,
        process_instance_id="SIM_PROCESS_TEST", callback_thread_id=1,
        callback_kind=CallbackKind.ON_TICK, observed_at=datetime.now(timezone.utc),
        origin=Origin.SYNTHETIC)


# ---------------------------------------------------------------------------
# NE1 client fake framework (added by the client owner; loader + fake native
# + loopback fake execution service; SYNTHETIC identities only)
# ---------------------------------------------------------------------------

import importlib.util
import json
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from os import getenv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIENT_SOURCE = Path(getenv("AK_INFINY_SNAPSHOT_CLIENT",
                            str(ROOT / "integrations/infiny/AKExecutionBridge.py"))).resolve()
if not CLIENT_SOURCE.is_file():
    raise FileNotFoundError(f"missing SYNTHETIC client source path: {CLIENT_SOURCE}")

_INVESTOR = {"InvestorID": "SYNTHETIC-ACCOUNT-A"}


class FakeNativeModule(types.ModuleType):
    """Records every attribute access; unexpected attributes raise."""

    def __init__(self):
        super().__init__("INFINIGO")
        self.calls = []
        self.attribute_access = []
        self.investors = [dict(_INVESTOR)]
        self.send_result = 7
        self.lock = threading.Lock()
        self.block_send = threading.Event()
        self.block_send.set()
        self.fail_send = False

        def record(name, impl):
            def wrapper(*args, **kwargs):
                with self.lock:
                    self.calls.append((name, {"args": args, "kwargs": kwargs},
                                       threading.current_thread()))
                return impl(*args, **kwargs)
            return wrapper

        self.updateState = record("updateState", lambda *a, **k: None)
        self.updateParam = record("updateParam", lambda *a, **k: None)
        self.writeLog = record("writeLog", lambda *a, **k: None)
        self.getInvestorList = record("getInvestorList", lambda: list(self.investors))
        self.subMarketData = record("subMarketData", lambda *, StrategyObj, ExchangeID, InstrumentID: None)
        self.unsubMarketData = record("unsubMarketData", lambda *, StrategyObj, ExchangeID, InstrumentID: None)
        self.sendOrder = record("sendOrder", self._send_order)
        self.cancelOrder = record("cancelOrder", lambda *, OrderID: self.send_result)

    def _send_order(self, **kwargs):
        if self.fail_send:
            raise RuntimeError("SYNTHETIC native send failure")
        if not self.block_send.wait(3):
            raise TimeoutError("SYNTHETIC native send blocked")
        return self.send_result

    def __getattr__(self, name):
        with self.lock:
            self.attribute_access.append((name, threading.current_thread()))
        raise AssertionError(f"unexpected INFINIGO attribute resolved: {name}")

    def send_calls(self):
        with self.lock:
            return [entry for entry in self.calls if entry[0] == "sendOrder"]

    def cancel_calls(self):
        with self.lock:
            return [entry for entry in self.calls if entry[0] == "cancelOrder"]

    def call_counts(self):
        """Per-method call counts (fake-native ledger side of the separated
        real-vs-fake bookkeeping: these are always SYNTHETIC fake calls and
        are never summed with real network/real service counts)."""
        with self.lock:
            counts = {}
            for name, _, _ in self.calls:
                counts[name] = counts.get(name, 0) + 1
            return counts


def install_fake_native(monkeypatch, name="INFINIGO"):
    fake = FakeNativeModule()
    monkeypatch.setitem(sys.modules, name, fake)
    return fake


def load_client_from_temp(monkeypatch, tmp_path, fake=None):
    """Deploy only the client file + fake INFINIGO into a temp dir and import
    it with the repository stripped from sys.path (NE1-H37)."""
    target_dir = Path(tmp_path) / "client_only"
    target_dir.mkdir()
    target = target_dir / "AKExecutionBridge.py"
    target.write_bytes(CLIENT_SOURCE.read_bytes())
    if fake is None:
        fake = install_fake_native(monkeypatch)
    saved = list(sys.path)
    # strip every repository path (client must not import the repo); stdlib stays
    sys.path[:] = [entry for entry in saved if "AK-trading" not in entry] + [str(target_dir)]
    try:
        spec = importlib.util.spec_from_file_location("ne1_client_isolated", target)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, "ne1_client_isolated", module)
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = saved
    return module, fake


def stop_started_bridge(bridge):
    """LM-I isolation helper (2026-09-17 combined-run hardening): stop a
    client instance iff a session is still live, so a failed assertion can
    never leak lane threads (the events lane cycles at 20 Hz and the control
    lane keeps polling every 5 s until process exit — leaked lanes poison
    every later test in one-process combined runs). Idempotent by the
    ``_running`` guard; never called for bridges that were never started."""
    if bridge is not None and getattr(bridge, "_running", False):
        bridge.on_stop()


def set_synthetic_gate_capture(bridge, *, quote=None, account=False,
                               position=False, contract=False):
    """Set SYNTHETIC freshness preconditions for existing gate unit tests.

    This fabricates only gate state, never native facts or an upload snapshot.
    Snapshot capture regressions must use actual callbacks and fake SDK reads.
    """
    # Preserve the isolated client's controlled-clock precondition.
    now = bridge._fresh.__func__.__globals__["_now_utc"]()
    with bridge._state_lock:
        capture = dict(bridge._latest_capture)
        if quote is not None:
            capture["quote_at"] = now
        if account:
            capture["account_at"] = now
        if position:
            capture["position_at"] = now
        if contract:
            capture["contract_at"] = now
        capture["capture_id"] = "SYNTHETIC-gate-" + uuid.uuid4().hex[:16]
        bridge._latest_capture = capture


def synthetic_binding(**overrides):
    base = {"account_key": "SIM_ACCOUNT_A", "strategy_key": "SIM_STRATEGY_S",
            "strategy_id": 23, "instance_id": "SIM_INSTANCE", "session_epoch": "SIM_EPOCH",
            "trading_day": "2026-09-14", "exchange": "SHFE", "instrument_id": "rb2610",
            "hedge_flag": "1"}
    base.update(overrides)
    return base


def synthetic_offer(binding=None, *, params_sha256="ab" * 32, offer_id="SIM_OFFER_1",
                    expires_in_ms=1500):
    """A structurally valid mirrored ExecutionOffer (C1.2 DTTL-02: the offer
    carries its first absolute deadline; callers may tighten it)."""
    binding = synthetic_binding() if binding is None else binding
    expires = datetime.now(timezone.utc) + timedelta(milliseconds=expires_in_ms)
    return {"offer_id": offer_id, "intent_id": "SIM_INTENT_1", "leg_id": "SIM_LEG_1",
            "plan_id": "cd" * 32, "intent_hash": "ab" * 32, "leg_hash": "cd" * 32,
            "binding": binding, "snapshot_id": "SIM_SNAPSHOT_1",
            "policy_id": "SIM_POLICY", "params_sha256": params_sha256,
            "control_revision": 1,
            "expires_at": expires.isoformat(timespec="microseconds").replace("+00:00", "Z")}


def synthetic_dispatch(binding=None, *, kind="SUBMIT", issued=None, params_sha256="ab" * 32,
                       command_id="SIM_CMD_1", ttl_ms=400, order_id=None):
    """A structurally valid mirrored DispatchCommand for client-side tests."""
    binding = synthetic_binding() if binding is None else binding
    issued_at = issued or datetime.now(timezone.utc)
    expires = issued_at + timedelta(milliseconds=ttl_ms)
    command = {
        "command_id": command_id, "kind": kind, "attempt_id": "SIM_ATTEMPT_1",
        "offer_id": "SIM_OFFER_1", "intent_id": "SIM_INTENT_1", "leg_id": "SIM_LEG_1",
        "plan_id": "cd" * 32, "binding": binding,
        "leg": {"leg_id": "SIM_LEG_1", "index": 0, "phase": "OPEN", "depends_on": [],
                "direction": "BUY", "offset": "OPEN", "volume": 1, "price": "3500",
                "position_bucket": None, "cash_reserve": "1000"},
        "order_type": "GFD", "price_tick": "1",
        "contract_limits": {"min_volume": 1, "max_volume": 5, "volume_step": 1},
        "control_revision": 1, "permit_id": "SIM_PERMIT_TEST",
        "params_sha256": params_sha256,
        "issued_at": issued_at.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "expires_at": expires.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "order_id": order_id, "route_evidence_id": None, "cancel_id": None}
    if kind == "CANCEL":
        command["order_id"] = order_id if order_id is not None else 42
        command["route_evidence_id"] = "SIM_ROUTE_TEST"
        command["cancel_id"] = "SIM_CANCEL_1"
    return command


class FakeExecutionService:
    """Loopback /execution/v1 service speaking the mirrored codec."""

    def __init__(self):
        self.requests = []
        self.lock = threading.Lock()
        self.poll_responses = []      # callables(packet) -> response
        self.offers = []              # offer dicts served as kind=OFFER polls
        self.ready_commands = []      # mirrored dispatch dicts or callables(packet) -> dict
        self.acked_event_ids = []
        self.uploaded_events = []
        self.receipts = []
        self.control_reports = []     # reported kinds in arrival order
        self.control_packets = []     # full ControlReport packets (W2 assertions)
        self.control_ack_kinds = []   # queued ACK kinds ("RECORDED"/"BLOCKED");
                                      # empty -> every report is RECORDED
        service = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self, status, payload):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                packet = json.loads(self.rfile.read(length))
                with service.lock:
                    service.requests.append((self.path, packet))
                if self.path.endswith("/poll"):
                    with service.lock:
                        responder = service.poll_responses.pop(0) if service.poll_responses else None
                        offer = service.offers.pop(0) if service.offers else None
                    if responder is not None:
                        self._reply(200, responder(packet))
                    elif offer is not None:
                        self._reply(200, service._offer(packet, offer))
                    else:
                        self._reply(200, service._idle(packet))
                elif self.path.endswith("/ready"):
                    with service.lock:
                        command = service.ready_commands.pop(0) if service.ready_commands else None
                    if command is None:
                        self._reply(200, service._blocked(packet))
                    else:
                        if callable(command):
                            command = command(packet)
                        self._reply(200, service._dispatch(packet, command))
                elif self.path.endswith("/events"):
                    with service.lock:
                        service.acked_event_ids.extend(
                            event["event_id"] for event in packet["data"]["events"])
                        service.uploaded_events.extend(packet["data"]["events"])
                    self._reply(200, service._ack(packet))
                elif self.path.endswith("/results"):
                    with service.lock:
                        service.receipts.append(packet["data"]["receipt"])
                    self._reply(200, service._ack(packet))
                elif self.path.endswith("/control"):
                    data = packet["data"]
                    with service.lock:
                        service.control_reports.append(data["kind"])
                        service.control_packets.append(packet)
                        ack_kind = (service.control_ack_kinds.pop(0)
                                    if service.control_ack_kinds else "RECORDED")
                    if ack_kind == "INVALID":
                        # corrupt-ACK injection: a PollResponse-shaped body on
                        # /control (the client must treat it as no ACK)
                        self._reply(200, service._ack(packet))
                    else:
                        self._reply(200, service._control(packet, ack_kind))
                else:
                    self._reply(404, {"error": "not_found"})

            def log_message(self, *args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    @staticmethod
    def _base(packet):
        return {"request_id": packet["data"]["request_id"],
                "instance_id": packet["data"]["instance_id"],
                "session_epoch": packet["data"]["session_epoch"],
                "generation": packet["data"]["generation"],
                "service_epoch": packet["data"]["service_epoch"],
                "sequence": packet["data"]["sequence"],
                "sent_at": packet["data"]["sent_at"],
                "params_sha256": packet["data"]["params_sha256"]}

    def _idle(self, packet):
        return {"schema_version": TRANSPORT_SCHEMA,
                "type": "PollResponse",
                "data": {**self._base(packet), "received_at": "2026-09-14T01:30:00.000000Z",
                         "ledger_revision": 1, "control_revision": 1, "kind": "IDLE",
                         "offer": None, "blockers": []}}

    def _offer(self, packet, offer):
        return {"schema_version": TRANSPORT_SCHEMA,
                "type": "PollResponse",
                "data": {**self._base(packet), "received_at": "2026-09-14T01:30:00.000000Z",
                         "ledger_revision": 1, "control_revision": 1, "kind": "OFFER",
                         "offer": offer, "blockers": []}}

    def _blocked(self, packet):
        return {"schema_version": TRANSPORT_SCHEMA,
                "type": "ReadyResponse",
                "data": {**self._base(packet), "kind": "BLOCKED", "dispatch": None,
                         "readiness": {}, "blockers": ["SERVICE_BLOCK"]}}

    def _dispatch(self, packet, command):
        return {"schema_version": TRANSPORT_SCHEMA,
                "type": "ReadyResponse",
                "data": {**self._base(packet), "kind": "DISPATCH", "dispatch": command,
                         "readiness": {}, "blockers": []}}

    def _ack(self, packet):
        return {"schema_version": TRANSPORT_SCHEMA,
                "type": "PollResponse",
                "data": {**self._base(packet), "received_at": "2026-09-14T01:30:00.000000Z",
                         "ledger_revision": 1, "control_revision": 1, "kind": "IDLE",
                         "offer": None, "blockers": []}}

    def _control(self, packet, ack_kind="RECORDED"):
        """W2 arming alignment: /control answers with a mirrored ControlResponse
        (the frozen client's PARAMS_CONFIRMED carrier decodes exactly this ACK
        shape via validate_control_response). Default RECORDED; queued BLOCKED
        entries exercise the terminal-refusal path."""
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds") \
            .replace("+00:00", "Z")
        data = packet["data"]
        confirmation = data.get("confirmation") or {}
        kind = data.get("kind")
        if ack_kind == "RECORDED" and kind == "PARAMS_CONFIRMED":
            phase = "ARMED"
        elif kind in ("GATE_CLOSED", "STOPPED"):
            phase = "DISABLED_CONFIRMED"
        else:
            phase = "DISABLED"
        return {"schema_version": TRANSPORT_SCHEMA,
                "type": "ControlResponse",
                "data": {**self._base(packet), "request_service_epoch": None,
                         "sent_at": now, "received_at": now, "binding": None,
                         "ledger_revision": 1, "control_revision": 1,
                         "kind": ack_kind, "report_kind": kind,
                         "confirmation_id": confirmation.get("confirmation_id"),
                         "control_phase": phase,
                         "readiness": {"mode": "NATIVE"} if phase == "ARMED" else {},
                         "blockers": [] if ack_kind == "RECORDED" else ["NATIVE_DISABLED"]}}

    @property
    def poll_url(self):
        return f"http://127.0.0.1:{self.port}/execution/v1/poll"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


# ---------------------------------------------------------------------------
# NE1-LM-R4 additions (decision NE1-ARB-PAIRING-LASTMILE-001 §6, LM-I row).
#
# Fixtures for the no-arg acceptance mainline (R4.2), the M04/M05/M07
# lifecycle matrices, and the separated real-vs-fake counting discipline.
# The no-arg assembly deploys the CURRENT integrations/infiny/AKExecutionBridge.py
# release file into a temp <...>/pyStrategy/self_strategy/ layout — when the
# LM-C revision lands it becomes the new release file automatically, because
# the deployment copies whatever the repository ships. Everything here is
# SYNTHETIC and never touches production stores, ports or credentials.
# ---------------------------------------------------------------------------

import hashlib
import os
import socket
import uuid
from datetime import date as _date
from http.client import HTTPConnection
from urllib.parse import urlsplit

from ak_trading.infiny_execution import native_contracts as _nc
from ak_trading.infiny_execution.client_service import (
    CompleteOutcome,
    ExecutionService,
    PollOutcome,
    ReadyOutcome,
    build_execution_server,
)
from ak_trading.infiny_execution.ledger import Ledger, LedgerError
from ak_trading.infiny_execution.native_contracts import STATUS_REQUEST_HEADERS

# A fixed SYNTHETIC 64-hex token (RULING 10 wire form). Test doubles choose
# this value up front and register it with the temp service; it is never a
# production credential and never enters logs/params in these fixtures.
SYNTHETIC_PAIRING_TOKEN = "ab" * 32

# Frozen target-contract identifiers the LM-C/LM-S/LM-D revisions implement
# (decision R1.1–R2.3). The doubles below speak exactly these formats so the
# skip-marked M04 chain runs unchanged once the freeze lands.
DEFAULT_STORE_ROOT = ".ak_execution"                      # R1.1 store root name
IDENTITY_SCHEMA = "ne1_client_identity_v1"                # R1.3 identity.json
PAIRING_REQUEST_SCHEMA = "ne1_pairing_request_v1"         # R1.4 active request
SERVICE_PAIRING_SCHEMA = "ne1_service_pairing_v2"         # R2.2 service writer
CLIENT_PAIRING_SCHEMA = "ne1_client_pairing_v1"           # R2.2 client slot
TARGET_STORE_LAYOUT = 2                                   # R1.3 layout version
SERVICE_VERSION_V2 = "ne1-service-20260917-pairing-v2"    # LM-S release id
TOOL_VERSION_V1 = "ne1-pairing-20260917-v1"               # LM-D tool version


def expected_default_store(self_strategy_dir, strategy_id):
    """R1.1 deployment-state default store path: the release file's directory
    plus ``.ak_execution/strategy-<decimal strategy_id>/``. CWD-, date-,
    PID- and UUID-free by construction."""
    if isinstance(strategy_id, bool) or not isinstance(strategy_id, int) or strategy_id < 0:
        raise ValueError("strategy_id must be a non-negative int, not bool")
    return (Path(self_strategy_dir) / DEFAULT_STORE_ROOT / f"strategy-{strategy_id}").resolve()


def tree_snapshot(root):
    """Relative-path snapshot of every file/dir under root (import/construct
    zero-side-effect sentinel for the no-arg probe)."""
    root = Path(root)
    if not root.exists():
        return frozenset()
    return frozenset(str(item.relative_to(root)) for item in root.rglob("*"))


def _strict_fields(payload, schema, fields):
    """Strict JSON-field gate shared by the pairing-format doubles."""
    if not isinstance(payload, dict) or set(payload) != set(fields):
        raise PairingToolRefusal("PAIRING_FORMAT_INVALID")
    if payload.get("schema_version") != schema:
        raise PairingToolRefusal("PAIRING_FORMAT_INVALID")
    return payload


def _is_hex64(value):
    return (type(value) is str and len(value) == 64
            and all(ch in "0123456789abcdef" for ch in value))


class PairingToolRefusal(Exception):
    """R2.3 rejection: carries only the safe reason code, never a secret."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class SelfStrategyDeployment:
    """A temp ``<tmp>/pyStrategy/self_strategy/`` deployment of the client
    release file with a fake INFINIGO installed BEFORE import (R4.2 stage 1).

    ``module`` is the imported self-contained client; ``client_sha256`` pins
    which release file the probe exercised."""

    def __init__(self, module, fake, self_strategy_dir, client_sha256):
        self.module = module
        self.fake = fake
        self.self_strategy_dir = Path(self_strategy_dir)
        self.client_sha256 = client_sha256

    def no_arg(self, *, strategy_id=None, strategy_name=None, params=None):
        """R4.2 no-arg construction: ``AKExecutionBridge()`` with ZERO
        arguments, then only the public framework surface — strategy_id /
        strategy_name attribute assignment and one ordinary ``set_params``.

        No store, no token, no transport/native injection, and no writes to
        private identity fields (``_instance_id``/``_session_epoch``/
        ``_generation`` stay exactly what the client itself generated)."""
        bridge = self.module.AKExecutionBridge()
        if strategy_id is not None:
            if isinstance(strategy_id, bool) or not isinstance(strategy_id, int):
                raise TypeError("framework strategy_id is a non-bool int")
            bridge.strategy_id = strategy_id  # normal framework assignment
        if strategy_name is not None:
            bridge.strategy_name = strategy_name
        if params:
            bridge.set_params(dict(params))  # ordinary parameter load only
        return bridge


def deploy_self_strategy(monkeypatch, tmp_path, fake=None):
    """Deploy the release file into a temp self_strategy directory, install
    the fake INFINIGO module BEFORE the import, and import the file with the
    repository stripped from sys.path (NE1-H37 self-containment)."""
    root = Path(tmp_path) / "pyStrategy"
    self_strategy = root / "self_strategy"
    self_strategy.mkdir(parents=True)
    target = self_strategy / "AKExecutionBridge.py"
    payload = CLIENT_SOURCE.read_bytes()
    target.write_bytes(payload)
    if fake is None:
        fake = install_fake_native(monkeypatch)  # in place before the import
    saved = list(sys.path)
    sys.path[:] = [entry for entry in saved if "AK-trading" not in entry] + [str(self_strategy)]
    try:
        spec = importlib.util.spec_from_file_location("ne1_client_self_strategy", target)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, "ne1_client_self_strategy", module)
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = saved
    return SelfStrategyDeployment(module, fake, self_strategy,
                                  hashlib.sha256(payload).hexdigest())


class ConnectionCountingListener:
    """Real-loopback network sentinel: counts REAL socket connections made by
    the production default transport (as opposed to counted JSON requests on
    a fake/temp service). Accepts and immediately closes — never answers —
    so any client cycle that reaches it fails fast and stays unpersisted."""

    def __init__(self):
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(16)
        self.port = self._socket.getsockname()[1]
        self._lock = threading.Lock()
        self._connections = 0
        self._closed = False
        listener = self

        def accept_loop():
            while True:
                try:
                    connection, _ = listener._socket.accept()
                except OSError:
                    return
                with listener._lock:
                    listener._connections += 1
                try:
                    connection.close()
                except OSError:
                    pass

        self.thread = threading.Thread(target=accept_loop, daemon=True)
        self.thread.start()

    def url(self, path="/execution/v1/poll"):
        return f"http://127.0.0.1:{self.port}{path}"

    def connection_count(self):
        with self._lock:
            return self._connections

    def close(self):
        if not self._closed:
            self._closed = True
            try:
                self._socket.close()
            except OSError:
                pass
            # deterministic accept-loop exit: without the join a racing
            # accept() call can outlive this close and land in the next
            # test's thread-count baselines
            if self.thread.is_alive():
                self.thread.join(timeout=2)


class StepClock:
    """Controllable wall+monotonic clock so the temp real service can step
    past the frozen 5 s disabled-poll floor deterministically."""

    def __init__(self):
        self._now = datetime(2026, 9, 17, 1, 0, 0, tzinfo=timezone.utc)
        self._mono = 5000.0

    def now(self):
        return self._now

    def monotonic(self):
        return self._mono

    def advance(self, seconds):
        self._mono += float(seconds)
        self._now = self._now + timedelta(seconds=seconds)


class StubOutcomeOrchestrator:
    """SYNTHETIC orchestrator double for the temp real service stack (same
    N4 seam the production service expects). Defaults keep every route in
    its legal disabled shape: IDLE polls, BLOCKED readies (RECONCILE_REQUIRED),
    not-found completions — no fabricated production commands (M04 rule)."""

    def __init__(self):
        self.poll_outcomes = []
        self.ready_outcomes = []
        self.completed = []

    def poll(self, binding, request):
        del binding, request
        if self.poll_outcomes:
            return self.poll_outcomes.pop(0)
        return PollOutcome(kind=_nc.ResponseKind.IDLE, offer=None, blockers=())

    def ready(self, binding, request):
        del binding, request
        if self.ready_outcomes:
            return self.ready_outcomes.pop(0)
        return ReadyOutcome(dispatch=None, blockers=("RECONCILE_REQUIRED",),
                            context_conflict=False)

    def complete(self, receipt):
        self.completed.append(receipt)
        return CompleteOutcome(submit_result=None, not_found=True)


class TempExecutionStack:
    """Independent temp loopback stack over the REAL service code: real
    Ledger (v2 journal) + real ExecutionService + real HTTP server, with
    SYNTHETIC identities only.

    Plays the operator/service roles of R3.1 for the M05 matrix: offline
    actor registration (with the ledger's registration-revision CAS), fresh
    per-start token issuance, and service restart on the same journal."""

    def __init__(self, tmp_path, *, bindings=(), tokens=None,
                 orchestrator=None, clock=None, audit_name="audit.jsonl",
                 evidence_registry=None, trusted_control_assemblies=None,
                 arming_prerequisites=None):
        base = Path(tmp_path) / "lm_service"
        base.mkdir(parents=True, exist_ok=True)
        self.base_dir = base
        self.journal_path = base / "exec.db"
        self.audit_path = base / audit_name
        self.clock = clock or StepClock()
        self.journal = Ledger(self.journal_path, clock=lambda: self.clock.now(),
                              schema="v2")
        self.tokens = dict(tokens or {})
        self.orchestrator = orchestrator or StubOutcomeOrchestrator()
        self.revisions = []
        # W2 arming rows (NE1-ARB-ARMING-001 ARM-M08/M10): optional isolated
        # SYNTHETIC trusted-assembly wiring handed to the REAL ExecutionService
        # exactly as the ARM-LM-S constructor accepts it; defaults keep the
        # production-disabled assembly.
        self.evidence_registry = evidence_registry
        self.trusted_control_assemblies = trusted_control_assemblies
        self.arming_prerequisites = arming_prerequisites
        if bindings:
            self.register_actor_bindings(bindings)
        self.service = None
        self.server = None
        self.thread = None
        self.port = None
        self._build()

    # -- operator role: offline registration (R3.1 step 4) -------------------

    def register_actor_bindings(self, bindings, *, actor_id="SIM_ACTOR_LM",
                                account_key="SIM_ACCOUNT_A",
                                expected_registration_revision=None):
        """Register bindings as dict fixtures; returns the ledger revision.
        ``expected_registration_revision`` exercises the existing ledger CAS
        (M05: idempotent retry, missing/stale revision refusal)."""
        prepared = tuple(
            c.Binding(**{**binding, "trading_day": _date.fromisoformat(binding["trading_day"])})
            for binding in bindings)
        with self.journal.maintenance():
            revision = self.journal.register_actor(
                actor_id, account_key, prepared,
                operator_action_id="SIM_OP_LM", manifest_sha256="ab" * 32,
                contract_lock_sha256="cd" * 32,
                expected_registration_revision=expected_registration_revision)
        self.revisions.append(revision)
        return revision

    # -- service role: build / restart on the same journal (F07/R3.2) --------

    def _build(self):
        self.service = ExecutionService(
            self.journal, tokens=dict(self.tokens),
            orchestrator=self.orchestrator, clock=self.clock,
            audit_log=self.audit_path,
            evidence_registry=self.evidence_registry,
            trusted_control_assemblies=self.trusted_control_assemblies,
            arming_prerequisites=self.arming_prerequisites)
        # A service restart rebinds the SAME port (a fixed production port is
        # part of the deployment identity); only the very first build lets the
        # OS pick an ephemeral one.
        port = self.port if self.port else 0
        self.server = build_execution_server(port=port, service=self.service)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def restart(self, *, tokens=None):
        """Service restart on the SAME journal: fresh service instance, strictly
        increasing service epoch, and caller-issued fresh tokens (current CLI
        behavior — old token material is never reused). The journal stays
        open so registered actors and their bindings persist."""
        self._stop_server()
        if tokens is not None:
            self.tokens = dict(tokens)
        self._build()
        return self.service

    def _stop_server(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=2)
        if self.service is not None:
            self.service.close()
        self.server = self.thread = None

    @property
    def poll_url(self):
        return f"http://127.0.0.1:{self.port}/execution/v1/poll"

    @property
    def service_epoch(self):
        return self.service.service_epoch

    def audit_records(self):
        if not self.audit_path.exists():
            return []
        return [json.loads(line) for line in
                self.audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    # -- service role: R2.2 non-secret start description + v2 pairing files --

    def write_service_start(self, path, *, contract_lock_sha256="cd" * 32,
                            service_version=SERVICE_VERSION_V2):
        """Non-secret service-start description (R2.2): no token, no secret
        digest — only identity, epoch, port and per-instance token paths."""
        payload = {"schema_version": "ne1_service_start_v1",
                   "origin": SYNTHETIC,
                   "service_version": service_version,
                   "service_instance_id": self.service.instance_id,
                   "service_epoch": self.service.service_epoch,
                   "host": "127.0.0.1", "port": self.port,
                   "contract_lock_sha256": contract_lock_sha256,
                   "pairing_dir": None,
                   "instances": {instance: {"session_epoch": None,
                                            "token_path": None}
                                 for instance in sorted(self.tokens)}}
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    def write_service_pairing_files_v2(self, pairing_dir, *, session_epochs=None,
                                       contract_lock_sha256="cd" * 32,
                                       diagnostic_only=False):
        """Service-role simulation of the R2.2 v2 pairing writer: one strict
        ``<instance_id>.token`` per registered instance, exclusive-create,
        refusing any existing source file (PAIRING_FILE_EXISTS)."""
        pairing_dir = Path(pairing_dir)
        pairing_dir.mkdir(parents=True, exist_ok=True)
        written = {}
        for instance_id in sorted(self.tokens):
            target = pairing_dir / f"{instance_id}.token"
            if target.exists():
                raise PairingToolRefusal("PAIRING_FILE_EXISTS")
            session = (session_epochs or {}).get(instance_id)
            payload = {"schema_version": SERVICE_PAIRING_SCHEMA,
                       "instance_id": instance_id,
                       "session_epoch": session,
                       "service_instance_id": self.service.instance_id,
                       "service_epoch": self.service.service_epoch,
                       "contract_lock_sha256": contract_lock_sha256,
                       "diagnostic_only": bool(diagnostic_only and instance_id == "unregistered"),
                       "token": self.tokens[instance_id]}
            handle = open(target, "x", encoding="ascii", newline="\n")
            try:
                handle.write(json.dumps(payload, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                handle.close()
            written[instance_id] = target
        return written

    def close(self):
        self._stop_server()
        self.journal.close()


class PairingToolDouble:
    """USER-role simulation of the reviewed local handoff tool
    (R2.1/R2.3 ``pair_infiny_execution.py install``): reads the active
    non-secret pairing request plus the service-side v2 pairing file and
    atomically publishes the client credential slot ``pairing.json``.

    Implements the checkable subset of the R2.3 rejection table with safe
    reason codes only; produces a non-secret receipt. SYNTHETIC evidence —
    this double exists so the M04 chain exercises the ROLE boundary before
    the LM-D tool freeze; it is never pointed at production stores."""

    REQUEST_FIELDS = ("schema_version", "request_id", "active", "created_at",
                      "instance_id", "session_epoch", "generation",
                      "process_instance_id", "deployment_key", "strategy_id",
                      "adapter_version", "client_file_sha256",
                      "store_layout_version", "store_directory",
                      "exchange", "instrument_id", "hedge_flag",
                      "execution_api_url", "params_sha256")
    SOURCE_FIELDS = ("schema_version", "instance_id", "session_epoch",
                     "service_instance_id", "service_epoch",
                     "contract_lock_sha256", "diagnostic_only", "token")
    SLOT_FIELDS = ("schema_version", "request_id", "request_sha256",
                   "instance_id", "session_epoch", "generation",
                   "deployment_key", "strategy_id", "params_sha256",
                   "client_file_sha256", "execution_origin",
                   "service_instance_id", "service_epoch",
                   "contract_lock_sha256", "token")

    def install(self, *, client_store, client_request_path, source_path,
                service_start_path, contract_lock_sha256, expected_client_sha256,
                receipt_path):
        client_store = Path(client_store)
        request_bytes = Path(client_request_path).read_bytes()
        request = _strict_fields(json.loads(request_bytes), PAIRING_REQUEST_SCHEMA,
                                 self.REQUEST_FIELDS)
        source = _strict_fields(json.loads(Path(source_path).read_text(encoding="ascii")),
                                SERVICE_PAIRING_SCHEMA, self.SOURCE_FIELDS)
        service_start = json.loads(Path(service_start_path).read_text(encoding="utf-8"))
        # -- R2.3 refusals --------------------------------------------------
        if request["active"] is not True:
            raise PairingToolRefusal("PAIRING_REQUEST_INACTIVE")
        if request["client_file_sha256"] != expected_client_sha256:
            raise PairingToolRefusal("PAIRING_CONTEXT_MISMATCH")
        if not _is_hex64(source["token"]):
            raise PairingToolRefusal("PAIRING_FORMAT_INVALID")
        if source.get("diagnostic_only") is True:
            raise PairingToolRefusal("PAIRING_DIAGNOSTIC_ONLY")
        if source["instance_id"] != request["instance_id"]:
            raise PairingToolRefusal("PAIRING_CONTEXT_MISMATCH")
        if source["session_epoch"] != request["session_epoch"]:
            raise PairingToolRefusal("PAIRING_CONTEXT_MISMATCH")
        if source["contract_lock_sha256"] != contract_lock_sha256 \
                or service_start.get("contract_lock_sha256") != contract_lock_sha256:
            raise PairingToolRefusal("PAIRING_CONTEXT_MISMATCH")
        if (source["service_instance_id"] != service_start.get("service_instance_id")
                or source["service_epoch"] != service_start.get("service_epoch")):
            raise PairingToolRefusal("PAIRING_SERVICE_MISMATCH")
        origin = self._origin(request["execution_api_url"])
        if origin != self._origin(f"http://127.0.0.1:{service_start['port']}/execution/v1/poll"):
            raise PairingToolRefusal("PAIRING_CONTEXT_MISMATCH")
        slot_path = client_store / "pairing.json"
        lock_path = client_store / "pairing-transfer.lock"
        client_store.mkdir(parents=True, exist_ok=True)
        try:
            lock_handle = open(lock_path, "x")
        except FileExistsError:
            raise PairingToolRefusal("PAIRING_TRANSFER_LOCK_BUSY") from None
        try:
            if slot_path.exists():
                # R2.3 client-slot rules: the immutable slot identity must
                # match; same-request reinstall must be byte-identical;
                # rotation is only legal under a NEW active request.
                try:
                    existing = json.loads(slot_path.read_text(encoding="utf-8"))
                except Exception:
                    raise PairingToolRefusal("PAIRING_SLOT_INVALID") from None
                if (not isinstance(existing, dict) or set(existing) != set(self.SLOT_FIELDS)
                        or existing.get("schema_version") != CLIENT_PAIRING_SCHEMA):
                    raise PairingToolRefusal("PAIRING_SLOT_INVALID")
                for field in ("instance_id", "deployment_key", "strategy_id"):
                    if existing.get(field) != request[field]:
                        raise PairingToolRefusal("PAIRING_SLOT_FOREIGN")
                if existing.get("request_id") == request["request_id"]:
                    if (existing.get("token") != source["token"]
                            or existing.get("request_sha256")
                            != hashlib.sha256(request_bytes).hexdigest()):
                        raise PairingToolRefusal("PAIRING_SLOT_CONFLICT")
            slot = {"schema_version": CLIENT_PAIRING_SCHEMA,
                    "request_id": request["request_id"],
                    "request_sha256": hashlib.sha256(request_bytes).hexdigest(),
                    "instance_id": request["instance_id"],
                    "session_epoch": request["session_epoch"],
                    "generation": request["generation"],
                    "deployment_key": request["deployment_key"],
                    "strategy_id": request["strategy_id"],
                    "params_sha256": request["params_sha256"],
                    "client_file_sha256": request["client_file_sha256"],
                    "execution_origin": origin,
                    "service_instance_id": source["service_instance_id"],
                    "service_epoch": source["service_epoch"],
                    "contract_lock_sha256": source["contract_lock_sha256"],
                    "token": source["token"]}
            # request changed while we held the lock -> refuse, never activate stale
            if Path(client_request_path).read_bytes() != request_bytes:
                raise PairingToolRefusal("PAIRING_REQUEST_CHANGED")
            temp = client_store / "pairing.json.tmp"
            temp.write_text(json.dumps(slot, sort_keys=True), encoding="utf-8")
            os.replace(temp, slot_path)
        finally:
            lock_handle.close()
            try:
                os.unlink(lock_path)
            except OSError:
                pass
        receipt = {"schema_version": "ne1_pairing_transfer_receipt_v1",
                   "origin": SYNTHETIC, "tool_version": TOOL_VERSION_V1,
                   "result": "TRANSFERRED_AWAITING_HANDSHAKE",
                   "client_store": str(client_store),
                   "request_id": request["request_id"],
                   "instance_id": request["instance_id"],
                   "session_epoch": request["session_epoch"],
                   "generation": request["generation"],
                   "strategy_id": request["strategy_id"],
                   "client_file_sha256": request["client_file_sha256"],
                   "service_instance_id": source["service_instance_id"],
                   "service_epoch": source["service_epoch"]}
        receipt_path = Path(receipt_path)
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
        return receipt

    @staticmethod
    def _origin(url):
        parsed = urlsplit(url)
        port = f":{parsed.port}" if parsed.port not in (None, 80) else ""
        return f"{parsed.scheme}://{parsed.hostname}{port}"


# ---------------------------------------------------------------------------
# raw loopback HTTP helpers against the temp real service (wire matrix)
# ---------------------------------------------------------------------------

def post_json(url, payload, *, token=None, timeout=3.0):
    """Direct loopback POST with the frozen token header; returns
    (status, decoded-body-or-None). Never follows redirects."""
    parsed = urlsplit(url)
    connection = HTTPConnection(parsed.hostname, parsed.port or 80, timeout=timeout)
    headers = {"Content-Type": "application/json; charset=utf-8",
               "Accept": "application/json", "Connection": "close"}
    if token is not None:
        headers["X-Execution-Token"] = token
    try:
        connection.request("POST", parsed.path or "/",
                           body=json.dumps(payload).encode("utf-8"), headers=headers)
        response = connection.getresponse()
        body = response.read()
        return response.status, (json.loads(body) if body else None)
    finally:
        connection.close()


def get_status(base_url, *, token=None, instance_id, session_epoch, generation,
               service_epoch, params_sha256, sequence=0, timeout=3.0):
    """Direct loopback GET /execution/v1/status with the ten frozen headers
    (service_epoch None is sent as the literal 'null')."""
    parsed = urlsplit(base_url)
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    headers = {
        STATUS_REQUEST_HEADERS["schema_version"]: TRANSPORT_SCHEMA,
        STATUS_REQUEST_HEADERS["type"]: "StatusRequest",
        STATUS_REQUEST_HEADERS["request_id"]: uuid.uuid4().hex,
        STATUS_REQUEST_HEADERS["instance_id"]: instance_id,
        STATUS_REQUEST_HEADERS["session_epoch"]: session_epoch,
        STATUS_REQUEST_HEADERS["generation"]: generation,
        STATUS_REQUEST_HEADERS["service_epoch"]: ("null" if service_epoch is None
                                                  else str(service_epoch)),
        STATUS_REQUEST_HEADERS["sequence"]: str(sequence),
        STATUS_REQUEST_HEADERS["sent_at"]: now,
        STATUS_REQUEST_HEADERS["params_sha256"]: params_sha256,
        "Connection": "close"}
    if token is not None:
        headers["X-Execution-Token"] = token
    connection = HTTPConnection(parsed.hostname, parsed.port or 80, timeout=timeout)
    try:
        connection.request("GET", "/execution/v1/status", headers=headers)
        response = connection.getresponse()
        body = response.read()
        return response.status, (json.loads(body) if body else None)
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# ARM-LM-I additions (decision NE1-ARB-ARMING-001, data/maintenance_artifacts/
# infiny-arming-mechanism-20260918-r1/decision.md: RULING 02 eight-state
# machine, RULING 04 slots/fields/ACL, RULING 05 one-time consumption,
# RULING 06 ARM-M matrix, RULING 07 LM-I row).
#
# W1 preparation fixtures only. Nothing here implements or patches the
# production client/tool/service; every artifact stays SYNTHETIC and lives
# under pytest temp directories. The frozen AR4.1/AR4.2 vocabularies below
# are shared test vectors: when ARM-LM-C/D/S freeze, their codecs must match
# these exact names/field sets (shared-vector rule, RULING 07 closing note).
# ---------------------------------------------------------------------------

ARMING_DECISION_ID = "NE1-ARB-ARMING-001"

# Frozen release ids the sibling owners will ship (RULING 07 C/D/S rows).
CLIENT_ARMING_VERSION_EXPECTED = "ne1-execution-20260918-arming-v1-snapshot-20260930-r1"
# W3 F-04: the X01-fix release is exactly v1b; source delimiters are not
# part of this value. The consuming freeze gate must compare parsed versions.
TOOL_ARMING_VERSION_EXPECTED = "ne1-pairing-20260918-arming-v1b"
SERVICE_ARMING_VERSION_EXPECTED = "ne1-service-20260918-arming-v1"

# Decision §一 snapshot of the pre-arming v2b client (F03/F04 tree). The
# legacy migration-block fact (ARM-M13 current half) is proven against THIS
# file only; once ARM-LM-C lands the tree hash differs and that proof is
# owned by the new client's own extension-whitelist tests.
LEGACY_V2B_CLIENT_SHA256 = "c0bbfee0b4f47e9e23a27469265cd45a5e3723623494f1a1e6f012d871a3aeca"

# AR4.1 fixed slot names (the exact arming-v1 extension whitelist).
ARMING_FILE_NAMES = frozenset({
    "arming-proposal.json",     # prepare-arm tool (non-secret candidates)
    "arming-plan.json",         # client (non-secret frozen target plan)
    "arming-request.json",      # client (non-secret active request digest)
    "arming-challenge.json",    # client (PRIVATE random challenge)
    "arming.json",              # user arm tool (PRIVATE one-time credential)
    "disarm.json",              # user disarm tool (non-secret scoped revoke)
    "arming-state.json",        # client (non-secret tombstones/state ACK)
    "arming-transfer.lock",     # tool/client short exclusive transfer lock
})

# AR4.2 schema ids.
ARMING_PROPOSAL_SCHEMA = "ne1_arming_proposal_v1"
ARMING_PLAN_SCHEMA = "ne1_arming_plan_v1"
ARMING_REQUEST_SCHEMA = "ne1_arming_request_v1"
ARMING_CHALLENGE_SCHEMA = "ne1_arming_challenge_v1"
CLIENT_ARMING_SCHEMA = "ne1_client_arming_v1"
CLIENT_DISARM_SCHEMA = "ne1_client_disarm_v1"
ARMING_STATE_SCHEMA = "ne1_arming_state_v1"
ARMING_TOOL_RECEIPT_SCHEMA = "ne1_arming_tool_receipt_v1"
ARMING_REGISTRATIONS_SCHEMA = "ne1_arming_registrations_v1"
ARMING_PREREQUISITES_SCHEMA = "ne1_arming_prerequisites_v1"

# AR4.2 frozen field sets (schema_version plus exactly these, in a `scope`
# object for the common scope, never flattened).
ARMING_SCOPE_FIELDS = (
    "pairing_request_id", "pairing_request_sha256", "instance_id",
    "session_epoch", "generation", "process_instance_id", "deployment_key",
    "strategy_id", "client_file_sha256", "adapter_version",
    "contract_lock_sha256", "execution_origin", "pairing_params_sha256")
ARMING_PROPOSAL_FIELDS = (
    "proposal_id", "scope", "binding", "operator_action_id", "authorization_id",
    "permit_id", "execution_mode", "risk_policy_id", "max_native_sends",
    "max_native_cancels", "created_at")
ARMING_PLAN_FIELDS = (
    "plan_id", "proposal_id", "proposal_sha256", "scope", "binding",
    "authorization_id", "permit_id", "target_params_sha256",
    "effective_overrides", "created_at")
ARMING_REQUEST_FIELDS = (
    "request_id", "plan_id", "plan_sha256", "scope", "binding",
    "authorization_id", "permit_id", "target_params_sha256",
    "service_instance_id", "service_epoch", "created_at", "expires_at",
    "active")
ARMING_CHALLENGE_FIELDS = (
    "request_id", "request_sha256", "challenge", "issued_at", "expires_at",
    "writer_sid")
CLIENT_ARMING_FIELDS = (
    "arm_id", "request_id", "request_sha256", "plan_id", "scope", "binding",
    "target_params_sha256", "authorization_id", "authorization_record_sha256",
    "permit_id", "permit_record_sha256", "service_instance_id",
    "service_epoch", "expected_control_revision", "prerequisites_id",
    "prerequisites_sha256", "challenge", "user_sid", "action_source",
    "explicit_user_action", "confirmed_at", "expires_at")
CLIENT_DISARM_FIELDS = (
    "disarm_id", "scope", "arm_request_id", "arm_id", "requested_at",
    "user_sid", "reason")
ARMING_STATE_FIELDS = (
    "scope", "plan_id", "arm_request_id", "arm_id", "state", "consumed",
    "revoked", "effective_enable_trading", "target_params_sha256",
    "service_instance_id", "service_epoch", "control_revision", "expires_at",
    "local_gate_closed", "service_gate_closed", "unresolved_dispatch_count",
    "count_complete", "updated_at", "reason_code")
ARMING_TOOL_RECEIPT_FIELDS = (
    "operation", "status", "action_source", "scope", "request_id", "arm_id",
    "disarm_id", "plan_id", "authorization_id", "permit_id",
    "target_params_sha256", "client_file_sha256", "contract_lock_sha256",
    "service_instance_id", "service_epoch", "control_revision",
    "prerequisites_id", "prerequisites_sha256", "user_sid",
    "local_gate_closed", "service_gate_closed", "recorded_at", "reason_code")
ARMING_REGISTRATIONS_FIELDS = (
    "binding", "target_params_sha256", "contract_lock_sha256",
    "authorization_id", "permit_id", "entries")
ARMING_REGISTRATION_ENTRY_FIELDS = (
    "record_type", "record_path", "record_sha256", "manifest_path",
    "manifest_sha256", "receipt_path", "receipt_sha256")
ARMING_PREREQUISITES_FIELDS = (
    "prerequisites_id", "created_at", "expires_at", "contract_lock_sha256",
    "client_file_sha256", "service_file_sha256", "tool_file_sha256",
    "evidence_refs")
ARMING_PREREQUISITE_KINDS = (
    "RELEASE_REVIEW", "S3_ZERO_NATIVE", "MANUAL_TAKEOVER_APPROVAL",
    "SINGLE_ORDER_APPROVAL", "NATIVE_ASSEMBLY_ACCEPTANCE")

# AR2.1: the ONLY fields a target plan may override on the frozen copy.
TARGET_OVERRIDE_FIELDS = frozenset({
    "execution_mode", "risk_policy_id", "authorization_id",
    "max_native_sends", "max_native_cancels"})
# F05 (decision-verified): the client's canonical params-hash field list.
# Transient switches, confirmations, challenges and permit/control
# identities never enter the params hash.
PARAMS_HASH_FIELDS = (
    "exchange", "instrument_id", "execution_api_url", "order_volume",
    "order_type", "risk_policy_id", "authorization_id", "max_native_sends",
    "max_native_cancels", "hedge_flag", "investor_id", "execution_mode",
    "account_tag", "additional_symbols")

# AR4.2: request/challenge activation window ceiling.
ARMING_REQUEST_ACTIVE_WINDOW_MAX_SECONDS = 300
# AR4.1: hard per-file JSON ceiling.
ARMING_MAX_JSON_BYTES = 64 * 1024

ACTION_SOURCE_USER_LOCAL_CONSOLE = "USER_LOCAL_CONSOLE"


class ArmingFixtureError(Exception):
    """Fixture-level design-rule violation (safe reason code only)."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class ArmingCrashInjected(Exception):
    """M09 crash-point injection at a named AR5.1 boundary."""


def _strict_bool(value, code):
    if type(value) is not bool:
        raise ArmingFixtureError(code)
    return value


def _strict_int(value, code):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ArmingFixtureError(code)
    return value


def _hex64(value, code):
    if (type(value) is not str or len(value) != 64
            or any(ch not in "0123456789abcdef" for ch in value)):
        raise ArmingFixtureError(code)
    return value


def arming_strict_loads(text):
    """Strict JSON reader for arming vectors: object top level, no duplicate
    keys, no NaN/Infinity, hard 64 KiB ceiling (AR4.1)."""
    if not isinstance(text, str):
        raise ArmingFixtureError("ARMING_FORMAT_NOT_TEXT")
    if len(text.encode("utf-8")) > ARMING_MAX_JSON_BYTES:
        raise ArmingFixtureError("ARMING_FORMAT_OVERSIZED")

    def pairs_hook(pairs):
        seen = set()
        for key, _ in pairs:
            if key in seen:
                raise ArmingFixtureError("ARMING_FORMAT_DUPLICATE_KEY")
            seen.add(key)
        return dict(pairs)

    def reject_constant(name):
        raise ArmingFixtureError("ARMING_FORMAT_NON_FINITE")

    try:
        document = json.loads(text, object_pairs_hook=pairs_hook,
                              parse_constant=reject_constant)
    except ArmingFixtureError:
        raise
    except (ValueError, UnicodeDecodeError):
        raise ArmingFixtureError("ARMING_FORMAT_INVALID") from None
    if not isinstance(document, dict):
        raise ArmingFixtureError("ARMING_FORMAT_NOT_OBJECT")
    return document


ARMING_SCHEMA_FIELDS = {
    ARMING_PROPOSAL_SCHEMA: ARMING_PROPOSAL_FIELDS,
    ARMING_PLAN_SCHEMA: ARMING_PLAN_FIELDS,
    ARMING_REQUEST_SCHEMA: ARMING_REQUEST_FIELDS,
    ARMING_CHALLENGE_SCHEMA: ARMING_CHALLENGE_FIELDS,
    CLIENT_ARMING_SCHEMA: CLIENT_ARMING_FIELDS,
    CLIENT_DISARM_SCHEMA: CLIENT_DISARM_FIELDS,
    ARMING_STATE_SCHEMA: ARMING_STATE_FIELDS,
    ARMING_TOOL_RECEIPT_SCHEMA: ARMING_TOOL_RECEIPT_FIELDS,
}


def validate_arming_document(schema, document):
    """Shared-vector strict validator (AR4.2): exact field set, exact schema
    id, strict bools, bool-never-int, hex64 where the ruling pins hashes."""
    fields = ARMING_SCHEMA_FIELDS.get(schema)
    if fields is None:
        raise ArmingFixtureError("ARMING_SCHEMA_UNKNOWN")
    if not isinstance(document, dict) or set(document) != {"schema_version", *fields}:
        raise ArmingFixtureError("ARMING_FIELDS_INVALID")
    if document["schema_version"] != schema:
        raise ArmingFixtureError("ARMING_SCHEMA_VERSION_INVALID")
    scope = document.get("scope")
    if "scope" in document:
        if not isinstance(scope, dict) or set(scope) != set(ARMING_SCOPE_FIELDS):
            raise ArmingFixtureError("ARMING_SCOPE_FIELDS_INVALID")
    for field in ("max_native_sends", "max_native_cancels", "service_epoch",
                  "expected_control_revision"):
        # AR4.2: references/revisions/windows not yet produced are null
        if field in document and document[field] is not None:
            _strict_int(document[field], "ARMING_FIELD_TYPE_INVALID")
    for field in ("active", "explicit_user_action", "consumed", "revoked",
                  "effective_enable_trading", "local_gate_closed",
                  "service_gate_closed", "count_complete"):
        if field in document:
            _strict_bool(document[field], "ARMING_FIELD_TYPE_INVALID")
    for field in ("pairing_request_sha256", "proposal_sha256", "plan_sha256",
                  "request_sha256", "target_params_sha256",
                  "authorization_record_sha256", "permit_record_sha256",
                  "prerequisites_sha256", "client_file_sha256",
                  "contract_lock_sha256", "challenge"):
        if field in document and document[field] is not None:
            _hex64(document[field], "ARMING_HASH_INVALID")
    if "action_source" in document and document["action_source"] != ACTION_SOURCE_USER_LOCAL_CONSOLE:
        raise ArmingFixtureError("ARMING_ACTION_SOURCE_INVALID")
    if "explicit_user_action" in document and document["explicit_user_action"] is not True:
        raise ArmingFixtureError("ARMING_EXPLICIT_ACTION_REQUIRED")
    if schema == ARMING_PROPOSAL_SCHEMA:
        if document["execution_mode"] != "native":
            raise ArmingFixtureError("ARMING_PROPOSAL_MODE_INVALID")
        if document["max_native_sends"] != 1:
            raise ArmingFixtureError("ARMING_PROPOSAL_SENDS_INVALID")
        if document["max_native_cancels"] not in (0, 1):
            raise ArmingFixtureError("ARMING_PROPOSAL_CANCELS_INVALID")
    if schema == ARMING_PLAN_SCHEMA:
        overrides = document["effective_overrides"]
        if not isinstance(overrides, dict) or not overrides \
                or set(overrides) - TARGET_OVERRIDE_FIELDS:
            raise ArmingFixtureError("ARMING_PLAN_OVERRIDES_INVALID")
    return document


# -- SYNTHETIC arming document builders (shared vectors) ---------------------

def synthetic_arming_hex(seed, length=64):
    import hashlib as _hashlib
    return _hashlib.sha256(seed.encode("utf-8")).hexdigest()[:length]


def synthetic_arming_scope(**overrides):
    base = {
        "pairing_request_id": synthetic_arming_hex("pairing-request", 32),
        "pairing_request_sha256": synthetic_arming_hex("pairing-request-bytes"),
        "instance_id": synthetic_arming_hex("instance", 32),
        "session_epoch": synthetic_arming_hex("session", 32),
        "generation": synthetic_arming_hex("generation", 32),
        "process_instance_id": synthetic_arming_hex("process", 32),
        "deployment_key": synthetic_arming_hex("deployment"),
        "strategy_id": 23,
        "client_file_sha256": synthetic_arming_hex("client-file"),
        "adapter_version": "ne1-execution-20260918-arming-v1-SYNTHETIC",
        "contract_lock_sha256": synthetic_arming_hex("contract-lock"),
        "execution_origin": "http://127.0.0.1:17779",
        "pairing_params_sha256": synthetic_arming_hex("pairing-params"),
    }
    base.update(overrides)
    return base


def synthetic_arming_proposal(scope=None, binding=None, **overrides):
    document = {
        "schema_version": ARMING_PROPOSAL_SCHEMA,
        "proposal_id": synthetic_arming_hex("proposal", 32),
        "scope": dict(scope or synthetic_arming_scope()),
        "binding": dict(binding or synthetic_binding()),
        "operator_action_id": "SIM_OPERATOR_ACTION_1",
        "authorization_id": "SIM_ARM_AUTH",
        "permit_id": "SIM_ARM_PERMIT",
        "execution_mode": "native",
        "risk_policy_id": "SIM_POLICY",
        "max_native_sends": 1,
        "max_native_cancels": 0,
        "created_at": "2026-09-18T00:00:00Z",
    }
    document.update(overrides)
    return validate_arming_document(ARMING_PROPOSAL_SCHEMA, document)


def synthetic_arming_plan(proposal=None, scope=None, binding=None, **overrides):
    proposal = proposal or synthetic_arming_proposal(scope=scope, binding=binding)
    document = {
        "schema_version": ARMING_PLAN_SCHEMA,
        "plan_id": synthetic_arming_hex("plan", 32),
        "proposal_id": proposal["proposal_id"],
        "proposal_sha256": synthetic_arming_hex("proposal-bytes"),
        "scope": proposal["scope"],
        "binding": proposal["binding"],
        "authorization_id": proposal["authorization_id"],
        "permit_id": proposal["permit_id"],
        "target_params_sha256": synthetic_arming_hex("target-params"),
        "effective_overrides": {
            "execution_mode": "native", "risk_policy_id": proposal["risk_policy_id"],
            "authorization_id": proposal["authorization_id"],
            "max_native_sends": 1, "max_native_cancels": 0},
        "created_at": proposal["created_at"],
    }
    document.update(overrides)
    return validate_arming_document(ARMING_PLAN_SCHEMA, document)


def synthetic_arming_request(plan=None, scope=None, binding=None, **overrides):
    plan = plan or synthetic_arming_plan(scope=scope, binding=binding)
    document = {
        "schema_version": ARMING_REQUEST_SCHEMA,
        "request_id": synthetic_arming_hex("arm-request", 32),
        "plan_id": plan["plan_id"],
        "plan_sha256": synthetic_arming_hex("plan-bytes"),
        "scope": plan["scope"],
        "binding": plan["binding"],
        "authorization_id": plan["authorization_id"],
        "permit_id": plan["permit_id"],
        "target_params_sha256": plan["target_params_sha256"],
        "service_instance_id": "execution-" + synthetic_arming_hex("service", 16),
        "service_epoch": 1,
        "created_at": "2026-09-18T00:00:00Z",
        "expires_at": "2026-09-18T00:05:00Z",
        "active": True,
    }
    document.update(overrides)
    return validate_arming_document(ARMING_REQUEST_SCHEMA, document)


def synthetic_arming_challenge(request=None, **overrides):
    request = request or synthetic_arming_request()
    document = {
        "schema_version": ARMING_CHALLENGE_SCHEMA,
        "request_id": request["request_id"],
        "request_sha256": synthetic_arming_hex("arm-request-bytes"),
        "challenge": synthetic_arming_hex("challenge"),
        "issued_at": "2026-09-18T00:00:00Z",
        "expires_at": "2026-09-18T00:05:00Z",
        "writer_sid": "S-1-5-21-1-2-3-4-1000",
    }
    document.update(overrides)
    return validate_arming_document(ARMING_CHALLENGE_SCHEMA, document)


def synthetic_arming_credential(request=None, plan=None, **overrides):
    request = request or synthetic_arming_request(plan=plan)
    document = {
        "schema_version": CLIENT_ARMING_SCHEMA,
        "arm_id": synthetic_arming_hex("arm", 32),
        "request_id": request["request_id"],
        "request_sha256": synthetic_arming_hex("arm-request-bytes"),
        "plan_id": request["plan_id"],
        "scope": request["scope"],
        "binding": request["binding"],
        "target_params_sha256": request["target_params_sha256"],
        "authorization_id": request["authorization_id"],
        "authorization_record_sha256": synthetic_arming_hex("auth-record"),
        "permit_id": request["permit_id"],
        "permit_record_sha256": synthetic_arming_hex("permit-record"),
        "service_instance_id": request["service_instance_id"],
        "service_epoch": request["service_epoch"],
        "expected_control_revision": 1,
        "prerequisites_id": synthetic_arming_hex("prerequisites", 32),
        "prerequisites_sha256": synthetic_arming_hex("prerequisites-bytes"),
        "challenge": synthetic_arming_hex("challenge"),
        "user_sid": "S-1-5-21-1-2-3-4-1000",
        "action_source": ACTION_SOURCE_USER_LOCAL_CONSOLE,
        "explicit_user_action": True,
        "confirmed_at": "2026-09-18T00:01:00Z",
        "expires_at": "2026-09-18T00:05:00Z",
    }
    document.update(overrides)
    return validate_arming_document(CLIENT_ARMING_SCHEMA, document)


def synthetic_arming_state(scope=None, **overrides):
    document = {
        "schema_version": ARMING_STATE_SCHEMA,
        "scope": dict(scope or synthetic_arming_scope()),
        "plan_id": None,
        "arm_request_id": None,
        "arm_id": None,
        "state": "DISARMED",
        "consumed": False,
        "revoked": False,
        "effective_enable_trading": False,
        "target_params_sha256": None,
        "service_instance_id": None,
        "service_epoch": None,
        "control_revision": None,
        "expires_at": None,
        "local_gate_closed": False,
        "service_gate_closed": False,
        "unresolved_dispatch_count": None,
        "count_complete": False,
        "updated_at": "2026-09-18T00:00:00Z",
        "reason_code": None,
    }
    document.update(overrides)
    return validate_arming_document(ARMING_STATE_SCHEMA, document)


def validate_arming_state_transition(previous, current):
    """AR4.2/AR5 one-way latch: consumed/revoked and closed gates never reset
    inside one scope (a state document may record history, but no value in
    it may ever be used to restore an armed gate after a restart)."""
    if previous is None:
        return current
    for field in ("consumed", "revoked", "local_gate_closed",
                  "service_gate_closed"):
        if previous.get(field) is True and current.get(field) is not True:
            raise ArmingFixtureError("ARMING_STATE_DOWNGRADE_FORBIDDEN")
    if previous.get("revoked") is True and current.get("effective_enable_trading") is True:
        # once the round is revoked, a later state document claiming an open
        # gate again in the same scope is a re-arm attempt from file (AR5.1)
        raise ArmingFixtureError("ARMING_STATE_REARM_FROM_FILE_FORBIDDEN")
    return current


def synthetic_arming_prerequisites(**overrides):
    """AR5.3 `ne1_arming_prerequisites_v1` SYNTHETIC vector: exactly the five
    evidence kinds, each an immutable non-secret path+hash (never a bare
    approved:true placeholder, never a NOT_RUN stand-in)."""
    evidence_refs = [
        {"kind": kind,
         "path": f"sim-evidence/{kind.lower()}.json",
         "sha256": synthetic_arming_hex(f"evidence-{kind}")}
        for kind in ARMING_PREREQUISITE_KINDS]
    document = {
        "schema_version": ARMING_PREREQUISITES_SCHEMA,
        "prerequisites_id": synthetic_arming_hex("prerequisites", 32),
        "created_at": "2026-09-18T00:00:00Z",
        "expires_at": "2026-09-25T00:00:00Z",
        "contract_lock_sha256": synthetic_arming_hex("contract-lock"),
        "client_file_sha256": synthetic_arming_hex("client-file"),
        "service_file_sha256": synthetic_arming_hex("service-file"),
        "tool_file_sha256": synthetic_arming_hex("tool-file"),
        "evidence_refs": evidence_refs,
    }
    document.update(overrides)
    return validate_arming_prerequisites(document)


def validate_arming_prerequisites(document):
    if not isinstance(document, dict) or set(document) != {
            "schema_version", *ARMING_PREREQUISITES_FIELDS}:
        raise ArmingFixtureError("ARMING_PREREQ_FIELDS_INVALID")
    if document["schema_version"] != ARMING_PREREQUISITES_SCHEMA:
        raise ArmingFixtureError("ARMING_PREREQ_SCHEMA_INVALID")
    refs = document["evidence_refs"]
    if not isinstance(refs, list):
        raise ArmingFixtureError("ARMING_PREREQ_REFS_INVALID")
    kinds = []
    for ref in refs:
        if not isinstance(ref, dict) or set(ref) != {"kind", "path", "sha256"}:
            raise ArmingFixtureError("ARMING_PREREQ_REF_FIELDS_INVALID")
        if ref["kind"] not in ARMING_PREREQUISITE_KINDS:
            raise ArmingFixtureError("ARMING_PREREQ_KIND_UNKNOWN")
        _hex64(ref["sha256"], "ARMING_HASH_INVALID")
        path = ref["path"]
        if not isinstance(path, str) or not path or path.strip() == "" \
                or "NOT_RUN" in path or path.endswith("placeholder"):
            raise ArmingFixtureError("ARMING_PREREQ_PLACEHOLDER_FORBIDDEN")
        kinds.append(ref["kind"])
    for kind in ARMING_PREREQUISITE_KINDS:
        if kind not in kinds:
            raise ArmingFixtureError("ARMING_PREREQ_KIND_MISSING")
    return document


def synthetic_arming_registrations(**overrides):
    """AR5.3 `ne1_arming_registrations_v1` SYNTHETIC vector: an index of the
    existing register-* receipts for this round (actor, evidence,
    authorization, permit) — an index, never the registration itself."""
    entries = [
        {"record_type": record_type,
         "record_path": f"sim-registry/{record_type.lower()}.json",
         "record_sha256": synthetic_arming_hex(f"record-{record_type}"),
         "manifest_path": f"sim-registry/{record_type.lower()}.manifest.json",
         "manifest_sha256": synthetic_arming_hex(f"manifest-{record_type}"),
         "receipt_path": f"sim-registry/{record_type.lower()}.receipt.json",
         "receipt_sha256": synthetic_arming_hex(f"receipt-{record_type}")}
        for record_type in ("ACTOR", "EVIDENCE", "AUTHORIZATION", "PERMIT")]
    document = {
        "schema_version": ARMING_REGISTRATIONS_SCHEMA,
        "binding": synthetic_binding(),
        "target_params_sha256": synthetic_arming_hex("target-params"),
        "contract_lock_sha256": synthetic_arming_hex("contract-lock"),
        "authorization_id": "SIM_ARM_AUTH",
        "permit_id": "SIM_ARM_PERMIT",
        "entries": entries,
    }
    document.update(overrides)
    return validate_arming_registrations(document)


def validate_arming_registrations(document):
    if not isinstance(document, dict) or set(document) != {
            "schema_version", *ARMING_REGISTRATIONS_FIELDS}:
        raise ArmingFixtureError("ARMING_REG_FIELDS_INVALID")
    if document["schema_version"] != ARMING_REGISTRATIONS_SCHEMA:
        raise ArmingFixtureError("ARMING_REG_SCHEMA_INVALID")
    _hex64(document["target_params_sha256"], "ARMING_HASH_INVALID")
    _hex64(document["contract_lock_sha256"], "ARMING_HASH_INVALID")
    for entry in document["entries"]:
        if not isinstance(entry, dict) or set(entry) != set(ARMING_REGISTRATION_ENTRY_FIELDS):
            raise ArmingFixtureError("ARMING_REG_ENTRY_FIELDS_INVALID")
        for field in ("record_sha256", "manifest_sha256", "receipt_sha256"):
            _hex64(entry[field], "ARMING_HASH_INVALID")
    return document


# -- AR2.1 dual-hash reference model -----------------------------------------

def reference_target_params_sha256(module, params_map, overrides=None):
    """AR2.1 reference computation of the TARGET params hash over a frozen
    copy: the deployed client module's own canonical codec (its
    sha256_hex + canonical_dumps + its ADAPTER_VERSION) applied to the F05
    hash field list, with ONLY the five allowed override fields replaced on
    the copy. The pairing hash is the same computation with no overrides;
    the arming tests assert encoder fidelity against the live client's own
    _params_sha256 before trusting this model. Overrides NEVER go through
    set_params (the public path coerces native/1 back to disabled/0 by F03).
    """
    overrides = dict(overrides or {})
    illegal = set(overrides) - TARGET_OVERRIDE_FIELDS
    if illegal:
        raise ArmingFixtureError("ARMING_OVERRIDE_FIELD_ILLEGAL")
    payload = {field: str(getattr(params_map, field)) for field in PARAMS_HASH_FIELDS}
    for field, value in overrides.items():
        payload[field] = str(value)
    payload["adapter_version"] = module.ADAPTER_VERSION
    return module.sha256_hex(module.canonical_dumps(payload))


# -- RULING 02 eight-state reference machine ----------------------------------

ARMING_STATE_DISARMED = "DISARMED"
ARMING_STATE_PREPARED_DISABLED = "PREPARED_DISABLED"
ARMING_STATE_ARM_READY = "ARM_READY"
ARMING_STATE_ARM_PENDING = "ARM_PENDING"
ARMING_STATE_ARMED = "ARMED"
ARMING_STATE_ARMED_SEND_EXHAUSTED = "ARMED_SEND_EXHAUSTED"
ARMING_STATE_REVOKE_PENDING = "REVOKE_PENDING"
ARMING_STATE_TERMINAL = "DISARMED_TERMINAL"

ARMING_STATES = (
    ARMING_STATE_DISARMED, ARMING_STATE_PREPARED_DISABLED, ARMING_STATE_ARM_READY,
    ARMING_STATE_ARM_PENDING, ARMING_STATE_ARMED, ARMING_STATE_ARMED_SEND_EXHAUSTED,
    ARMING_STATE_REVOKE_PENDING, ARMING_STATE_TERMINAL)

ARMING_MODEL_EVENTS = (
    "prepare", "pairing_epoch_verified", "service_says_armed",
    "arm_credential_validated", "service_ack", "legal_callback",
    "first_send_consumed", "attempt_send", "attempt_cancel", "reopen_send",
    "second_confirmation", "revoke", "local_gate_closed", "expire")


class ArmingStateModel:
    """Reference admission machine exactly per the RULING 02 AR2.2 table.

    Pairing state (WAIT_PAIRING/PAIRED/RESTART_REQUIRED) is a SEPARATE
    dimension: `pairing_epoch_verified` is only accepted when the caller has
    first established `note_pairing(paired_disabled=True, epoch_checked=True,
    plan_valid=True)`; no arming state can be derived from pairing facts
    alone and vice versa. This model is the owner-side design assertion; the
    independent reviewer designs their own counters (RULING 06).
    """

    def __init__(self, *, cancel_scope_authorized=True, max_cancels=0):
        self.state = ARMING_STATE_DISARMED
        self.consumed = False
        self.revoked = False
        self.activation_count = 0
        self.cancel_scope_authorized = bool(cancel_scope_authorized)
        self.cancels_used = 0
        self.max_cancels = _strict_int(max_cancels, "ARMING_MODEL_BUDGET_INVALID")
        self._ack_matched = False
        self._pairing_facts = {"paired_disabled": False, "epoch_checked": False,
                               "plan_valid": False}
        self.event_log = []

    # -- pairing dimension (communication) ---------------------------------

    def note_pairing(self, *, paired_disabled=None, epoch_checked=None,
                     plan_valid=None):
        for key, value in (("paired_disabled", paired_disabled),
                           ("epoch_checked", epoch_checked),
                           ("plan_valid", plan_valid)):
            if value is not None:
                self._pairing_facts[key] = _strict_bool(value, "ARMING_MODEL_TYPE")

    @property
    def effective_enable_trading(self):
        return self.state in (ARMING_STATE_ARMED, ARMING_STATE_ARMED_SEND_EXHAUSTED)

    def _require(self, condition, code):
        if not condition:
            raise ArmingFixtureError(code)

    def _record(self, event):
        self.event_log.append((self.state, event))

    # -- arming events ------------------------------------------------------

    def on_start(self):
        """New object / on_start -> DISARMED (AR2.2 row 1)."""
        self._require(self.state == ARMING_STATE_DISARMED,
                      "ARMING_MODEL_ON_START_INVALID")
        self._record("on_start")
        return self.state

    def prepare(self, proposal_valid=True):
        self._require(self.state == ARMING_STATE_DISARMED,
                      "ARMING_TRANSITION_ILLEGAL")
        self._require(proposal_valid, "ARMING_PROPOSAL_REJECTED")
        self._record("prepare")
        self.state = ARMING_STATE_PREPARED_DISABLED
        return self.state

    def pairing_epoch_verified(self):
        self._require(self.state == ARMING_STATE_PREPARED_DISABLED,
                      "ARMING_TRANSITION_ILLEGAL")
        facts = self._pairing_facts
        self._require(facts["paired_disabled"] and facts["epoch_checked"]
                      and facts["plan_valid"], "ARMING_PRECONDITIONS_MISSING")
        self._record("pairing_epoch_verified")
        self.state = ARMING_STATE_ARM_READY
        return self.state

    def service_says_armed(self):
        """AR2.2 row 3: service ready/armed WORDING never transitions."""
        self._record("service_says_armed")
        return self.state

    def arm_credential_validated(self):
        self._require(self.state == ARMING_STATE_ARM_READY,
                      "ARMING_TRANSITION_ILLEGAL")
        self._require(self.activation_count == 0,
                      "ARMING_SESSION_ALREADY_CONSUMED")
        self._record("arm_credential_validated")
        self.state = ARMING_STATE_ARM_PENDING
        return self.state

    def service_ack(self, matched):
        """ARM_PENDING stays disabled until the exact ACK AND a legal
        framework callback (AR2.2 row 4). Retransmit of the SAME logical
        confirmation is allowed; a second confirmation is not."""
        self._record("service_ack")
        if self.state == ARMING_STATE_REVOKE_PENDING:
            # AR5.2: a late arm ACK must never override the revoke latch.
            return self.state
        self._require(self.state == ARMING_STATE_ARM_PENDING,
                      "ARMING_TRANSITION_ILLEGAL")
        if matched:
            self._ack_matched = True
        return self.state

    def legal_callback(self):
        self._record("legal_callback")
        if self.state == ARMING_STATE_REVOKE_PENDING:
            return self.state
        self._require(self.state == ARMING_STATE_ARM_PENDING,
                      "ARMING_TRANSITION_ILLEGAL")
        self._require(self._ack_matched, "ARMING_ACK_REQUIRED_BEFORE_ARMED")
        self.state = ARMING_STATE_ARMED
        self.activation_count += 1
        self.consumed = True
        return self.state

    def first_send_consumed(self):
        """AR2.2 row 6: the FIRST send attempt (ok/rejected/exception/unknown)
        persistently occupies the quota."""
        self._require(self.state == ARMING_STATE_ARMED,
                      "ARMING_TRANSITION_ILLEGAL")
        self._record("first_send_consumed")
        self.state = ARMING_STATE_ARMED_SEND_EXHAUSTED
        return self.state

    def attempt_send(self, outcome="ok"):
        self._require(outcome in ("ok", "rejected", "exception", "unknown"),
                      "ARMING_MODEL_OUTCOME_INVALID")
        if self.state == ARMING_STATE_ARMED:
            return self.first_send_consumed()
        raise ArmingFixtureError("ARMING_SEND_BUDGET_EXHAUSTED")

    def attempt_cancel(self):
        """Own-order cancel only with an independent CANCEL_OWNED grant, at
        most the remaining cancel quota; never reopens send (AR2.2 row 6)."""
        self._require(self.state in (ARMING_STATE_ARMED,
                                     ARMING_STATE_ARMED_SEND_EXHAUSTED),
                      "ARMING_CANCEL_OUT_OF_SCOPE")
        self._require(self.cancel_scope_authorized, "ARMING_CANCEL_NOT_AUTHORIZED")
        self._require(self.cancels_used < self.max_cancels,
                      "ARMING_CANCEL_BUDGET_EXHAUSTED")
        self.cancels_used += 1
        self._record("attempt_cancel")
        return True

    def reopen_send(self):
        raise ArmingFixtureError("ARMING_SEND_REOPEN_FORBIDDEN")

    def second_confirmation(self):
        raise ArmingFixtureError("ARMING_SECOND_CONFIRMATION_FORBIDDEN")

    def revoke(self, reason="USER_DISARM"):
        del reason
        if self.state == ARMING_STATE_TERMINAL:
            # idempotent re-revocation inside the terminal latch (AR5.2)
            self._record("revoke")
            return self.state
        self._require(self.state in ARMING_STATES, "ARMING_TRANSITION_ILLEGAL")
        self._record("revoke")
        self.revoked = True
        self.state = ARMING_STATE_REVOKE_PENDING
        return self.state

    def local_gate_closed(self):
        self._require(self.state == ARMING_STATE_REVOKE_PENDING,
                      "ARMING_TRANSITION_ILLEGAL")
        self._record("local_gate_closed")
        self.state = ARMING_STATE_TERMINAL
        return self.state

    def expire(self):
        """Expiry closes and latches the round (AR5.2)."""
        self._require(self.state != ARMING_STATE_TERMINAL,
                      "ARMING_TRANSITION_ILLEGAL")
        self._record("expire")
        self.revoked = True
        self.state = ARMING_STATE_REVOKE_PENDING
        return self.state


# -- AR4.2 clock fixture ------------------------------------------------------

class ArmingClock:
    """Controllable UTC + monotonic clock for the arming windows. UTC may be
    rolled BACK deliberately (AR2.2/AR5: a clock rollback must never revive a
    candidate); the monotonic axis only ever advances, and deadlines latch
    once passed."""

    def __init__(self, *, start=None, monotonic_start=9000.0):
        self._now = start or datetime(2026, 9, 18, 0, 0, 0, tzinfo=timezone.utc)
        self._mono = float(monotonic_start)
        self._min_mono = float(monotonic_start)

    def now(self):
        return self._now

    def monotonic(self):
        return self._mono

    def advance(self, seconds):
        self._mono += float(seconds)
        self._now = self._now + timedelta(seconds=seconds)
        self._min_mono = max(self._min_mono, self._mono)

    def rollback_utc(self, seconds):
        """UTC moves back; the monotonic axis and every latched deadline
        stay exactly where they were."""
        self._now = self._now - timedelta(seconds=seconds)
        if self._mono < self._min_mono:
            self._mono = self._min_mono


class ArmingDeadline:
    """A candidate's own earliest-expiry deadline (AR2.2: the client keeps a
    monotonic cutoff AND re-checks UTC; rollback cannot restore a candidate).
    """

    def __init__(self, clock, *, utc_expires_at, window_seconds):
        self._clock = clock
        self.utc_expires_at = utc_expires_at
        self._monotonic_deadline = clock.monotonic() + float(window_seconds)
        self._latched = False

    def expired(self):
        if self._latched:
            return True
        if self._clock.monotonic() >= self._monotonic_deadline:
            self._latched = True
        elif self._clock.now() >= self.utc_expires_at:
            self._latched = True
        return self._latched


# -- AR1.3 fake interactive console -------------------------------------------

class ConsoleRefusal(Exception):
    """Console-double refusal carrying only a safe reason code."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class FakeConsoleTTY:
    """Interactive LOCAL console double for the arm flow (AR1.3).

    - both streams are TTYs and a console is attached in the positive shape;
      piped/redirected/detached variants refuse before any prompt;
    - phrase input is scripted per keystroke-submission: each read consumes
      one fresh scripted line and retries never auto-answer;
    - the console never receives the challenge value (AR1.3: the random
      challenge is not displayed and not copyable);
    - every displayed line is recorded so tests can assert the full scope
      disclosure (account marker, binding, versions/hashes, mode, action,
      volume, price range, expiry, authorization/permit ids, send=1 and the
      explicit cancel count, and the once-per-lifetime statement).
    """

    def __init__(self, *, stdin_tty=True, stdout_tty=True, attached=True,
                 scripted_inputs=None):
        self._stdin_tty = bool(stdin_tty)
        self._stdout_tty = bool(stdout_tty)
        self._attached = bool(attached)
        self.scripted_inputs = list(scripted_inputs or [])
        self.displayed = []
        self.read_count = 0

    @classmethod
    def piped(cls):
        return cls(stdin_tty=False)

    @classmethod
    def redirected(cls):
        return cls(stdout_tty=False)

    @classmethod
    def detached(cls):
        return cls(attached=False)

    def require_interactive(self):
        if not self._attached:
            raise ConsoleRefusal("CONSOLE_NOT_ATTACHED")
        if not self._stdin_tty:
            raise ConsoleRefusal("CONSOLE_STDIN_NOT_TTY")
        if not self._stdout_tty:
            raise ConsoleRefusal("CONSOLE_STDOUT_NOT_TTY")
        return True

    def is_interactive(self):
        try:
            self.require_interactive()
        except ConsoleRefusal:
            return False
        return True

    def display(self, text):
        self.require_interactive()
        self.displayed.append(str(text))
        return True

    def read_phrase(self, request_id):
        """Read the user's confirmation phrase `ARM <request_id last 8>`."""
        self.require_interactive()
        if not self.scripted_inputs:
            raise ConsoleRefusal("CONFIRMATION_INPUT_REQUIRED")
        phrase = self.scripted_inputs.pop(0)
        self.read_count += 1
        if not isinstance(phrase, str) or not phrase.strip():
            raise ConsoleRefusal("CONFIRMATION_EMPTY")
        expected = f"ARM {request_id[-8:]}"
        if phrase.strip() != expected:
            raise ConsoleRefusal("CONFIRMATION_PHRASE_MISMATCH")
        return phrase.strip()


def assert_no_secret_material(text, secrets):
    """AR4.3 disclosure discipline: the raw secret, its hex/base64 forms and
    its md5/sha256 digests must never appear in stdout/stderr/receipts/
    params/backups/exceptions/URLs."""
    import base64 as _base64
    import hashlib as _hashlib
    problems = []
    for secret in secrets:
        if not secret:
            continue
        raw = secret.encode("utf-8")
        candidates = {
            "raw": secret,
            "hex": raw.hex(),
            "hex_upper": raw.hex().upper(),
            "base64": _base64.b64encode(raw).decode("ascii"),
            "sha256": _hashlib.sha256(raw).hexdigest(),
            "md5": _hashlib.md5(raw).hexdigest(),
        }
        for form, value in candidates.items():
            if value and value in text:
                problems.append(form)
    if problems:
        raise AssertionError(f"secret material disclosed: {sorted(set(problems))}")
    return True


# -- AR5.1 one-time credential slot double ------------------------------------

class ArmingSlotRefusal(Exception):
    """Slot-double refusal carrying only a safe reason code."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


CRASH_POINTS = (
    "before_delivery",                 # before the credential file exists
    "after_delivery_before_consume",   # delivered, not yet consumed
    "after_tombstone_before_delete",   # tombstone persisted, secrets remain
    "after_delete_before_http",        # secrets removed, confirmation unsent
)


class OneTimeCredentialSlot:
    """Role double of the client-side arming slot lifecycle (AR5.1 order):
    transfer lock -> re-verify request/scope -> persist the consumed
    tombstone -> mark the request inactive -> remove arming.json and
    arming-challenge.json -> only then may the confirmation be sent. Every
    step failure leaves the gate closed; crash injection reproduces the
    AR5.1 file-level crash points for the M09 matrix. Replay resistance is
    tombstone-based, never deletion-based.
    """

    def __init__(self, store_dir, *, clock=None):
        self.store_dir = Path(store_dir)
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.clock = clock or ArmingClock()
        self.consumed_arm_ids = {}
        self.state = synthetic_arming_state()
        self.http_confirmations_sent = []

    def path(self, name):
        if name not in ARMING_FILE_NAMES:
            raise ArmingSlotRefusal("ARMING_SLOT_NAME_UNKNOWN")
        return self.store_dir / name

    def publish_request(self, request):
        validate_arming_document(ARMING_REQUEST_SCHEMA, request)
        target = self.path("arming-request.json")
        if target.exists():
            raise ArmingSlotRefusal("ARMING_REQUEST_ALREADY_PUBLISHED")
        target.write_text(json.dumps(request, sort_keys=True), encoding="utf-8")
        return target

    def publish_challenge(self, challenge):
        validate_arming_document(ARMING_CHALLENGE_SCHEMA, challenge)
        target = self.path("arming-challenge.json")
        if target.exists():
            raise ArmingSlotRefusal("ARMING_CHALLENGE_ALREADY_PUBLISHED")
        target.write_text(json.dumps(challenge, sort_keys=True), encoding="utf-8")
        return target

    def deliver_credential(self, credential, *, crash_at=None):
        """USER-role tool delivery of arming.json (one-time, exclusive)."""
        if crash_at is not None and crash_at not in CRASH_POINTS:
            raise ArmingSlotRefusal("ARMING_CRASH_POINT_UNKNOWN")
        if crash_at == "before_delivery":
            raise ArmingCrashInjected("before_delivery")
        validate_arming_document(CLIENT_ARMING_SCHEMA, credential)
        if credential["arm_id"] in self.consumed_arm_ids:
            raise ArmingSlotRefusal("ARMING_ALREADY_CONSUMED")
        target = self.path("arming.json")
        if target.exists():
            raise ArmingSlotRefusal("ARMING_DELIVERY_ALREADY_PRESENT")
        target.write_text(json.dumps(credential, sort_keys=True), encoding="utf-8")
        if crash_at == "after_delivery_before_consume":
            raise ArmingCrashInjected("after_delivery_before_consume")
        return target

    def consume(self, credential, *, crash_at=None):
        """Client-role consumption per the AR5.1 mandatory order. A repeated
        consume for an arm_id whose tombstone is persisted but whose single
        confirmation was never sent is the CRASH-RECOVERY continuation: it
        finishes the mandated order (remove secrets, then send exactly once)
        and never grants a second activation."""
        if crash_at is not None and crash_at not in CRASH_POINTS:
            raise ArmingSlotRefusal("ARMING_CRASH_POINT_UNKNOWN")
        validate_arming_document(CLIENT_ARMING_SCHEMA, credential)
        arm_id = credential["arm_id"]
        already = arm_id in self.consumed_arm_ids
        sent = any(entry["arm_id"] == arm_id
                   for entry in self.http_confirmations_sent)
        if already and sent:
            # tombstone + confirmation both recorded: any further consume is
            # a replay, refused by the persisted tombstone (not by absence)
            raise ArmingSlotRefusal("ARMING_ALREADY_CONSUMED")
        arming_path = self.path("arming.json")
        if not already:
            if not arming_path.exists():
                raise ArmingSlotRefusal("ARMING_CREDENTIAL_ABSENT")
            stored = arming_strict_loads(arming_path.read_text(encoding="utf-8"))
            if stored != credential:
                raise ArmingSlotRefusal("ARMING_CREDENTIAL_MISMATCH")
            # 1. persist the consumed tombstone BEFORE any secret removal
            self.consumed_arm_ids[arm_id] = self.clock.now().isoformat()
            self.state = validate_arming_state_transition(
                self.state, synthetic_arming_state(
                    scope=credential["scope"], plan_id=credential["plan_id"],
                    arm_request_id=credential["request_id"], arm_id=arm_id,
                    state="ARM_PENDING", consumed=True,
                    target_params_sha256=credential["target_params_sha256"],
                    service_instance_id=credential["service_instance_id"],
                    service_epoch=credential["service_epoch"],
                    control_revision=credential["expected_control_revision"],
                    expires_at=credential["expires_at"],
                    updated_at=self.clock.now().isoformat(
                        timespec="seconds").replace("+00:00", "Z")))
            self._write_state()
            if crash_at == "after_tombstone_before_delete":
                raise ArmingCrashInjected("after_tombstone_before_delete")
        # 2. request inactive + 3. remove both secret files (idempotent on
        # the recovery continuation)
        if arming_path.exists():
            arming_path.unlink()
        challenge_path = self.path("arming-challenge.json")
        if challenge_path.exists():
            challenge_path.unlink()
        if crash_at == "after_delete_before_http":
            raise ArmingCrashInjected("after_delete_before_http")
        # 4. only now may the confirmation be sent — exactly once per arm_id
        self.http_confirmations_sent.append(
            {"arm_id": arm_id,
             "target_params_sha256": credential["target_params_sha256"],
             "at": self.clock.now().isoformat(),
             "credential": dict(credential)})
        return True

    def retransmit_same_confirmation(self, credential):
        """ACK loss: bounded retransmission of the SAME logical confirmation,
        original arm_id and original deadline (AR5.1). Same confirmation id
        with different content is refused (AR3.3)."""
        validate_arming_document(CLIENT_ARMING_SCHEMA, credential)
        if credential["arm_id"] not in self.consumed_arm_ids:
            raise ArmingSlotRefusal("ARMING_NOT_CONSUMED_YET")
        if not self.http_confirmations_sent:
            raise ArmingSlotRefusal("ARMING_RETRANSMIT_OUT_OF_ORDER")
        previous = self.http_confirmations_sent[0]
        if previous["credential"] != credential:
            raise ArmingSlotRefusal("ARMING_RETRANSMIT_CONTENT_CHANGED")
        self.http_confirmations_sent.append(dict(previous))
        return True

    def _write_state(self):
        self.path("arming-state.json").write_text(
            json.dumps(self.state, sort_keys=True), encoding="utf-8")

    def restart(self):
        """Process restart: every restart is effective=False and old proofs
        are void (AR5.1); the persisted tombstones survive (that is their
        point), ARMED never restores, and the state document reloads from
        disk strictly."""
        survived = dict(self.consumed_arm_ids)
        persisted_state = None
        state_path = self.path("arming-state.json")
        if state_path.exists():
            persisted_state = validate_arming_document(
                ARMING_STATE_SCHEMA,
                arming_strict_loads(state_path.read_text(encoding="utf-8")))
        self.__init__(self.store_dir, clock=self.clock)
        self.consumed_arm_ids = survived
        if persisted_state is not None:
            # history is reloadable as FACTS; effective stays False in memory
            self.state = persisted_state
        return self


# -- AR4.3 Windows ACL sandbox (REAL DACL, never a chmod mock) ----------------

class ArmingAclError(Exception):
    """Real-DACL sandbox failure with a safe reason code."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _arming_windows_acl():
    """Reuse the repo's REAL Windows ACL toolkit (SetNamedSecurityInfoW /
    GetNamedSecurityInfoW via ctypes; the same R2.4 standard AR4.3 keeps)."""
    if sys.platform != "win32":
        raise ArmingAclError("ARMING_ACL_WINDOWS_ONLY")
    from ak_trading.infiny_execution.client_service import (
        _windows_acl, secure_pairing_directory)
    return _windows_acl(), secure_pairing_directory


def _loosen_dacl(path, *, add_everyone=True):
    """Negative-case builder: install a REAL protected DACL that ALSO grants
    Everyone full access (mirrors the pairing M03 negative builder; ctypes
    argument-list call, no shell, path only)."""
    if sys.platform != "win32":
        raise ArmingAclError("ARMING_ACL_WINDOWS_ONLY")
    import ctypes
    import ctypes.wintypes as wt
    acl_toolkit, _ = _arming_windows_acl()
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.ConvertStringSidToSidW.argtypes = (wt.LPCWSTR, ctypes.POINTER(ctypes.c_void_p))
    advapi32.ConvertStringSidToSidW.restype = wt.BOOL
    advapi32.InitializeAcl.argtypes = (ctypes.c_void_p, wt.DWORD, wt.DWORD)
    advapi32.InitializeAcl.restype = wt.BOOL
    advapi32.AddAccessAllowedAceEx.argtypes = (ctypes.c_void_p, wt.DWORD,
                                               wt.DWORD, wt.DWORD, ctypes.c_void_p)
    advapi32.AddAccessAllowedAceEx.restype = wt.BOOL
    advapi32.SetNamedSecurityInfoW.argtypes = (wt.LPCWSTR, ctypes.c_int, wt.DWORD,
                                               ctypes.c_void_p, ctypes.c_void_p,
                                               ctypes.c_void_p, ctypes.c_void_p)
    advapi32.SetNamedSecurityInfoW.restype = ctypes.c_ulong
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p
    current_sid = acl_toolkit.current_user_sid()
    trustees = [current_sid] + (["S-1-1-0"] if add_everyone else [])
    acl = ctypes.create_string_buffer(1024)
    if not advapi32.InitializeAcl(acl, 1024, 2):
        raise ArmingAclError("ARMING_ACL_NEGATIVE_BUILD_FAILED")
    pointers = []
    try:
        for sid in trustees:
            pointer = ctypes.c_void_p()
            if not advapi32.ConvertStringSidToSidW(sid, ctypes.byref(pointer)):
                raise ArmingAclError("ARMING_ACL_NEGATIVE_BUILD_FAILED")
            pointers.append(pointer)
            if not advapi32.AddAccessAllowedAceEx(
                    acl, 2, 0, 0x001F01FF, ctypes.c_void_p(pointer.value)):
                raise ArmingAclError("ARMING_ACL_NEGATIVE_BUILD_FAILED")
        code = advapi32.SetNamedSecurityInfoW(
            str(path), 1, 0x4 | 0x80000000, None, None, acl, None)
        if code != 0:
            raise ArmingAclError("ARMING_ACL_NEGATIVE_BUILD_FAILED")
    finally:
        for pointer in pointers:
            kernel32.LocalFree(ctypes.c_void_p(pointer.value))


class ArmingAclSandbox:
    """SYNTHETIC arming slot sandbox exercising the REAL Windows DACL surface:
    the protection domain is established and verified on the EMPTY parent
    directory before any secret byte exists; private files are created as
    exclusive temp entries, tightened, verified, fsynced and atomically
    published (AR4.3). Non-Windows platforms fail closed."""

    def __init__(self, base_dir):
        toolkit, secure = _arming_windows_acl()
        self._toolkit = toolkit
        self._secure = secure
        self.base_dir = Path(base_dir)
        if self.base_dir.exists():
            raise ArmingAclError("ARMING_SANDBOX_DIR_EXISTS")
        self.sid = secure(self.base_dir)   # protected BEFORE anything else
        self.verify()                      # domain proven while still empty

    def _create_tightened_temp(self, name):
        """Exclusive empty temp entry, REAL protected DACL applied and
        verified BEFORE any payload byte is written (AR4.3 order)."""
        temp = self.base_dir / ("." + name + ".tmp")
        if temp.exists():
            raise ArmingAclError("ARMING_TEMP_EXISTS")
        open(temp, "xb").close()
        self._toolkit.apply_protected_dacl(temp, sid=self.sid, directory=False)
        self._toolkit.verify_private(temp, sid=self.sid)
        return temp

    def write_private(self, name, payload):
        if name not in ARMING_FILE_NAMES:
            raise ArmingSlotRefusal("ARMING_SLOT_NAME_UNKNOWN")
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        temp = self._create_tightened_temp(name)
        handle = open(temp, "ab")
        try:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        final = self.base_dir / name
        os.replace(temp, final)
        self._toolkit.verify_private(final, sid=self.sid)
        return final

    def write_public(self, name, payload):
        """Non-secret slots live in the same protection domain (AR4.3 protects
        their integrity too): same tightened publication path."""
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        return self.write_private(name, payload)

    def verify(self):
        self._toolkit.verify_private(self.base_dir, sid=self.sid)
        for item in sorted(self.base_dir.iterdir()):
            if item.name.endswith(".tmp"):
                continue
            self._toolkit.verify_private(item, sid=self.sid)

    def loosen(self, name=None):
        """Install the REAL negative case (Everyone ACE) on one entry or the
        directory itself."""
        target = self.base_dir if name is None else self.base_dir / name
        _loosen_dacl(target)
        return target

    def current_user_sid(self):
        return self._toolkit.current_user_sid()


# ===========================================================================
# W2 cross-end arming drivers (NE1-ARB-ARMING-001 RULING 07 W2: the frozen
# ARM-LM-C client + ARM-LM-D tool roles + ARM-LM-S service run through their
# PUBLIC local-slot protocol only — no private-field injection, no Params
# re-arm, no production bypass. Everything below is SYNTHETIC.
# ===========================================================================

SYNTHETIC_ARMING_SERVICE_INSTANCE = "execution-0123456789abcdef"
SYNTHETIC_ARMING_SERVICE_EPOCH = 5
SYNTHETIC_ARMING_CONTRACT_LOCK = "ee" * 32
SYNTHETIC_ARMING_AUTHORIZATION_ID = "SIM_AUTH_ARM"
SYNTHETIC_ARMING_PERMIT_ID = "SIM_PERMIT_ARM"


def _fixture_current_user_sid():
    """Actual process-token SID (never an environment claim)."""
    import ctypes
    import ctypes.wintypes as wt
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.OpenProcessToken.argtypes = (wt.HANDLE, wt.DWORD,
                                          ctypes.POINTER(wt.HANDLE))
    advapi32.OpenProcessToken.restype = wt.BOOL
    advapi32.GetTokenInformation.argtypes = (wt.HANDLE, wt.DWORD, wt.LPVOID,
                                             wt.DWORD, ctypes.POINTER(wt.DWORD))
    advapi32.GetTokenInformation.restype = wt.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = (ctypes.c_void_p,
                                                ctypes.POINTER(wt.LPWSTR))
    advapi32.ConvertSidToStringSidW.restype = wt.BOOL
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = (wt.HANDLE,)
    token = wt.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008,
                                     ctypes.byref(token)):
        raise ArmingFixtureError("FIXTURE_SID_UNAVAILABLE")
    try:
        size = wt.DWORD(0)
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(max(size.value, 1))
        if not advapi32.GetTokenInformation(token, 1, buffer, size,
                                            ctypes.byref(size)):
            raise ArmingFixtureError("FIXTURE_SID_UNAVAILABLE")
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        text = wt.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise ArmingFixtureError("FIXTURE_SID_UNAVAILABLE")
        try:
            return text.value
        finally:
            kernel32.LocalFree(text)
    finally:
        kernel32.CloseHandle(token)


def _fixture_apply_protected_dacl(path):
    """Operator/user tool-role writer discipline (AR4.3): PROTECTED DACL
    granting only the current Windows user full control, applied on the
    exclusive temp entry before publication. Direct ctypes argument list —
    no shell, path only, mirroring the reviewed tool writers."""
    import ctypes
    import ctypes.wintypes as wt
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.ConvertStringSidToSidW.argtypes = (wt.LPCWSTR,
                                                ctypes.POINTER(ctypes.c_void_p))
    advapi32.ConvertStringSidToSidW.restype = wt.BOOL
    advapi32.InitializeAcl.argtypes = (ctypes.c_void_p, wt.DWORD, wt.DWORD)
    advapi32.InitializeAcl.restype = wt.BOOL
    advapi32.AddAccessAllowedAce.argtypes = (ctypes.c_void_p, wt.DWORD, wt.DWORD,
                                             ctypes.c_void_p)
    advapi32.AddAccessAllowedAce.restype = wt.BOOL
    advapi32.SetNamedSecurityInfoW.argtypes = (wt.LPCWSTR, ctypes.c_int, wt.DWORD,
                                               ctypes.c_void_p, ctypes.c_void_p,
                                               ctypes.c_void_p, ctypes.c_void_p)
    advapi32.SetNamedSecurityInfoW.restype = ctypes.c_ulong
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    sid = ctypes.c_void_p()
    if not advapi32.ConvertStringSidToSidW(_fixture_current_user_sid(),
                                           ctypes.byref(sid)):
        raise ArmingFixtureError("FIXTURE_ACL_UNAVAILABLE")
    try:
        total = 16 + 8 + advapi32.GetLengthSid(sid)
        buffer = ctypes.create_string_buffer(total)
        if not advapi32.InitializeAcl(buffer, total, 2):
            raise ArmingFixtureError("FIXTURE_ACL_FAILED")
        if not advapi32.AddAccessAllowedAce(buffer, 2, 0x1F01FF, sid):
            raise ArmingFixtureError("FIXTURE_ACL_FAILED")
        code = advapi32.SetNamedSecurityInfoW(str(path), 1, 0x4 | 0x80000000,
                                              None, None, buffer, None)
        if code != 0:
            raise ArmingFixtureError("FIXTURE_ACL_FAILED")
    finally:
        kernel32.LocalFree(sid)


def _fixture_publish_slot(store_path, name, payload):
    """Atomic protected publication of one arming slot (temp -> DACL ->
    bytes -> replace); the fixture stands in for the reviewed tool writer."""
    import os as _os
    import uuid as _uuid
    temporary = _os.path.join(store_path, f".{name}.{_uuid.uuid4().hex}.tmp")
    with open(temporary, "xb") as handle:
        _fixture_apply_protected_dacl(temporary)
        handle.write(payload)
        handle.flush()
        _os.fsync(handle.fileno())
    _os.replace(temporary, _os.path.join(store_path, name))


def install_arming_proposal(bridge, *, authorization_id=SYNTHETIC_ARMING_AUTHORIZATION_ID,
                            permit_id=SYNTHETIC_ARMING_PERMIT_ID,
                            risk_policy_id="SIM_POLICY_ARM", max_native_cancels=1,
                            contract_lock=SYNTHETIC_ARMING_CONTRACT_LOCK):
    """OPERATOR-role prepare-arm output (SYNTHETIC): the strict
    ne1_arming_proposal_v1 slot the frozen client accepts."""
    import hashlib as _hashlib
    import json as _json
    import uuid as _uuid
    from datetime import datetime as _dt
    from urllib.parse import urlsplit as _urlsplit
    store = Path(bridge._store_path)
    request_raw = (store / "pairing-request.json").read_bytes()
    request = _json.loads(request_raw.decode("utf-8"))
    record = {
        "schema_version": "ne1_arming_proposal_v1",
        "proposal_id": _uuid.uuid4().hex,
        "scope": {
            "pairing_request_id": request["request_id"],
            "pairing_request_sha256": _hashlib.sha256(request_raw).hexdigest(),
            "instance_id": request["instance_id"],
            "session_epoch": request["session_epoch"],
            "generation": request["generation"],
            "process_instance_id": request["process_instance_id"],
            "deployment_key": request["deployment_key"],
            "strategy_id": request["strategy_id"],
            "client_file_sha256": request["client_file_sha256"],
            "adapter_version": request["adapter_version"],
            "contract_lock_sha256": contract_lock,
            "execution_origin": "http://127.0.0.1:"
                                + str(_urlsplit(request["execution_api_url"]).port),
            "pairing_params_sha256": request["params_sha256"],
        },
        "binding": synthetic_binding(instance_id=bridge._instance_id,
                                     session_epoch=bridge._session_epoch),
        "operator_action_id": "SIM_ACTION_ARM_1",
        "authorization_id": authorization_id,
        "permit_id": permit_id,
        "execution_mode": "native",
        "risk_policy_id": risk_policy_id,
        "max_native_sends": 1,
        "max_native_cancels": max_native_cancels,
        "created_at": _dt.now(timezone.utc).isoformat(timespec="microseconds")
                      .replace("+00:00", "Z"),
    }
    _fixture_publish_slot(bridge._store_path, "arming-proposal.json",
                          _json.dumps(record, sort_keys=True).encode("utf-8"))
    return record


def install_arm_credential(bridge, *, expected_control_revision=1,
                           authorization_record_sha256="af" * 32,
                           permit_record_sha256="bf" * 32,
                           challenge_override=None, expires_override=None,
                           arm_id_override=None):
    """USER-role arming.json delivery (SYNTHETIC): the strict
    ne1_client_arming_v1 one-time credential for the published request."""
    import hashlib as _hashlib
    import json as _json
    import uuid as _uuid
    from datetime import datetime as _dt
    store = Path(bridge._store_path)
    request_raw = (store / "arming-request.json").read_bytes()
    request = _json.loads(request_raw.decode("utf-8"))
    plan = _json.loads((store / "arming-plan.json").read_text(encoding="utf-8"))
    challenge = _json.loads((store / "arming-challenge.json")
                            .read_text(encoding="utf-8"))
    expires = min(_dt.fromisoformat(request["expires_at"].replace("Z", "+00:00")),
                  _dt.fromisoformat(challenge["expires_at"].replace("Z", "+00:00")))
    record = {
        "schema_version": "ne1_client_arming_v1",
        "arm_id": arm_id_override or _uuid.uuid4().hex,
        "request_id": request["request_id"],
        "request_sha256": _hashlib.sha256(request_raw).hexdigest(),
        "plan_id": plan["plan_id"],
        "scope": request["scope"],
        "binding": request["binding"],
        "target_params_sha256": request["target_params_sha256"],
        "authorization_id": request["authorization_id"],
        "authorization_record_sha256": authorization_record_sha256,
        "permit_id": request["permit_id"],
        "permit_record_sha256": permit_record_sha256,
        "service_instance_id": request["service_instance_id"],
        "service_epoch": request["service_epoch"],
        "expected_control_revision": expected_control_revision,
        "prerequisites_id": "SIM_PREREQ_1",
        "prerequisites_sha256": "cf" * 32,
        "challenge": challenge["challenge"] if challenge_override is None
                     else challenge_override,
        "user_sid": _fixture_current_user_sid(),
        "action_source": "USER_LOCAL_CONSOLE",
        "explicit_user_action": True,
        "confirmed_at": _dt.now(timezone.utc).isoformat(timespec="microseconds")
                        .replace("+00:00", "Z"),
        "expires_at": (expires_override or expires).isoformat(
            timespec="microseconds").replace("+00:00", "Z"),
    }
    _fixture_publish_slot(bridge._store_path, "arming.json",
                          _json.dumps(record, sort_keys=True).encode("utf-8"))
    return record


def inject_arming_service_seams(bridge, *, instance=SYNTHETIC_ARMING_SERVICE_INSTANCE,
                                epoch=SYNTHETIC_ARMING_SERVICE_EPOCH):
    """SYNTHETIC service identity facts for the paired-fixture seam: the real
    flow learns these from the authenticated activation handshake; the seam
    injects only these two non-secret facts, never any gate state."""
    bridge._service_instance_id = instance
    with bridge._service_epoch_lock:
        bridge._service_epoch = epoch


def arm_bridge_to_armed(module, bridge):
    """Drive the frozen client's PUBLIC local-slot protocol to ARMED over the
    real HTTP service: proposal -> plan -> request/challenge -> USER
    credential -> exact RECORDED ACK -> legal callback install. Asserts the
    ruled end state (ARMED, effective, Params never armed anything)."""
    install_arming_proposal(bridge)
    bridge._process_arming_files()          # DISARMED -> PREPARED_DISABLED
    assert bridge._arm_state == module.ARM_STATE_PREPARED_DISABLED, bridge._last_arming_reason
    bridge._process_arming_files()          # PREPARED_DISABLED -> ARM_READY
    assert bridge._arm_state == module.ARM_STATE_ARM_READY, bridge._last_arming_reason
    install_arm_credential(bridge)
    bridge._process_arming_files()          # ARM_READY -> ARM_PENDING (+ ACK)
    assert bridge._arm_state == module.ARM_STATE_ARM_PENDING, bridge._last_arming_reason
    assert bridge._arming_ack_received, bridge._last_arming_reason
    bridge._pump_once()
    assert bridge._arm_state == module.ARM_STATE_ARMED, bridge._last_arming_reason
    assert bridge._arming_effective_now() is True
    assert bridge.params_map.enable_trading is False  # Params never armed anything
    return bridge


# public aliases for the W2 cross-end tests (fixture-role helpers)
publish_arming_slot = _fixture_publish_slot
arming_fixture_user_sid = _fixture_current_user_sid
loosen_arming_slot_dacl = _loosen_dacl


def arm_bridge_from_tool_round(module, bridge):
    """Tool-driven variant of the chain: the proposal slot was published by the
    REAL prepare-arm CLI and the credential by the REAL arm tool; drive only
    the client's public consumption side (lane cycles + legal callback)."""
    bridge._process_arming_files()          # DISARMED -> PREPARED_DISABLED
    assert bridge._arm_state == module.ARM_STATE_PREPARED_DISABLED, bridge._last_arming_reason
    bridge._process_arming_files()          # PREPARED_DISABLED -> ARM_READY
    assert bridge._arm_state == module.ARM_STATE_ARM_READY, bridge._last_arming_reason
    bridge._process_arming_files()          # ARM_READY -> ARM_PENDING (+ ACK)
    assert bridge._arm_state == module.ARM_STATE_ARM_PENDING, bridge._last_arming_reason
    assert bridge._arming_ack_received, bridge._last_arming_reason
    bridge._pump_once()                     # legal callback -> ARMED
    assert bridge._arm_state == module.ARM_STATE_ARMED, bridge._last_arming_reason
    assert bridge._arming_effective_now() is True
    assert bridge.params_map.enable_trading is False  # Params never armed anything
    return bridge


def armed_dispatch(bridge, *, kind="SUBMIT", command_id="SIM_CMD_1", ttl_ms=400,
                   order_id=None):
    """A dispatch pinned to the armed round's effective hash, permit and
    control revision (AR2.3 gate contract)."""
    dispatch = synthetic_dispatch(
        binding=synthetic_binding(instance_id=bridge._instance_id,
                                  session_epoch=bridge._session_epoch),
        kind=kind, params_sha256=bridge._effective_params_sha256,
        command_id=command_id, ttl_ms=ttl_ms, order_id=order_id)
    dispatch["permit_id"] = bridge._arm_context.permit_id
    dispatch["control_revision"] = bridge._arm_context.control_revision
    return dispatch
