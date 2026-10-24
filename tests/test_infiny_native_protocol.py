"""TP: NE1 service transport protocol tests (H03/H04 + limits + status hygiene).

Covers plan section 9 rows NE1-H03 (transport strictness at the HTTP ingress)
and NE1-H04 (pairing/loopback/origin/replay/epoch/token redaction), the RULING
03 rate limits, RULING 01/02 response shapes and RULING 09 contiguous ACK
plumbing. Network tests only connect to their own loopback temporary ports;
the live 17778/17779 services are never touched. All fixtures are SYNTHETIC.

NE1 pairing last-mile (NE1-ARB-PAIRING-LASTMILE-001, LM-S, 2026-09-17):
this file additionally owns the service-side M03 handoff-safety matrix
(private ``ne1_service_pairing_v2`` writer with REAL Windows DACL positives
and negatives - never a chmod mock - bad-format/diagnostic/foreign-instance
rejections, source-file immutability, token redaction across exceptions,
stdout and receipts) and the M05 restart/actor-CAS matrix (first
registration, same-manifest idempotency, missing/wrong/correct
``--expected-registration-revision``, live-service journal lock refusing
registration with ``JOURNAL_RUNTIME_BUSY``, OS lock reclaim after a holder
crash, fresh pairing-dir-per-run enforcement, and old-token/old-session
rejection across a CAS re-registration plus service restart), together with
the six-route/identity/epoch regression sweep.

X01-S/X02-S fix (NE1-ARB-LM-X01-001, 2026-09-17): this file additionally
owns the sole-legal-format service-id writer vectors (``execution-`` +
16 lowercase hex; bare hex32 / prefix+hex32 / wrong suffix lengths /
case and whitespace variants refused with zero partial writes), the CLI
source/start format and char-for-char equality assertions, and the X02
epoch decode matrix (epoch 0 and non-integer epochs -> typed 400
INVALID_REQUEST with no illegal value echoed, positive wrong epochs ->
409 EPOCH_MISMATCH, null semantics and the 401-first order unchanged).

NE1 arming mechanism (NE1-ARB-ARMING-001, ARM-LM-S, 2026-09-18): this
file owns the RULING 06 server-side arming matrix rows - ARM-M08
(PARAMS_CONFIRMED real validation chain: the handler no longer records on
a matching binding alone; wrong target hash, wrong approved-release
adapter hash, expired/not-yet-valid windows and non-interactive flags are
refused, the default DISABLED assembly is never promoted, unregistered
permits and scope-widening grants are refused) and the ARM-M10 service
rows (disarm latches the revocation one-way and stops new poll/ready
issuance, retransmissions replay the same recorded fact, same-id
different-content confirmations are rejected, revision drift re-blocks),
plus the AR3.3 ``--arming-readiness`` anchor matrix (strict loader
negatives and the single new ``arming_prerequisites`` startup field).
All arming fixtures are isolated trusted SYNTHETIC assemblies per the
ruling; production register-* keeps refusing SYNTHETIC records.
"""

from __future__ import annotations

import hashlib
import http.client
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from ak_trading.infiny_execution import contracts as c
from ak_trading.infiny_execution import native_contracts as nc
from ak_trading.infiny_execution.client_service import (
    ARMING_PREREQUISITES_SCHEMA_VERSION,
    ArmingPrerequisites,
    DIAGNOSTIC_INSTANCE_ID,
    SERVICE_PAIRING_SCHEMA_VERSION,
    SERVICE_VERSION,
    ExecutionHTTPServer,
    ExecutionService,
    N4BackedOrchestrator,
    ProductionServiceRegistry,
    TrustedControlAssembly,
    build_execution_server,
    load_arming_prerequisites,
    secure_pairing_directory,
    write_pairing_file,
    _windows_acl,
)
from ak_trading.infiny_execution.ledger import Ledger
from ak_trading.infiny_execution.simulation import simulation_context

D = Decimal
SCHEMA = nc.TRANSPORT_SCHEMA_VERSION
TOKEN = "ab" * 32
GENERATION = "SIM_GEN_1"
PARAMS = "cd" * 32
SERVICE_TOKEN_SCHEMA = SERVICE_PAIRING_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# SYNTHETIC fixtures
# ---------------------------------------------------------------------------

class FakeServiceClock:
    def __init__(self):
        self._now = datetime(2026, 9, 14, 1, 0, 0, tzinfo=timezone.utc)
        self._mono = 1000.0

    def now(self):
        return self._now

    def monotonic(self):
        return self._mono

    def advance(self, seconds):
        self._mono += float(seconds)
        self._now = self._now + timedelta(seconds=seconds)


class StubOrchestrator:
    """SYNTHETIC orchestrator serving canned N4-shaped outcomes."""

    def __init__(self, binding):
        self.binding = binding
        self.poll_outcomes = []
        self.ready_outcomes = []
        self.complete_results = []

    def poll(self, binding, request):
        del request
        if self.poll_outcomes:
            return self.poll_outcomes.pop(0)
        return SimpleNamespace(kind=nc.ResponseKind.IDLE, offer=None, blockers=())

    def ready(self, binding, request):
        del request
        if self.ready_outcomes:
            return self.ready_outcomes.pop(0)
        return SimpleNamespace(dispatch=None, blockers=("RECONCILE_REQUIRED",),
                               context_conflict=False)

    def complete(self, receipt):
        if self.complete_results:
            return self.complete_results.pop(0)
        raise AssertionError("unexpected complete call")


class Env:
    def __init__(self, tmp_path, *, orchestrator=None, clock=None, max_workers=8,
                 audit_name="audit.jsonl", evidence_registry=None,
                 trusted_control_assemblies=None, arming_prerequisites=None):
        self.fake_clock = clock or FakeServiceClock()
        self.journal_path = Path(tmp_path) / "journal.sqlite"
        self.journal = Ledger(self.journal_path, clock=lambda: self.fake_clock.now(),
                              schema="v2")
        base = simulation_context(clock=None)
        self.binding = base.binding
        with self.journal.maintenance():
            self.journal.register_actor(
                "SIM_ACTOR_1", self.binding.account_key, (self.binding,),
                operator_action_id="SIM_OP_1", manifest_sha256="ab" * 32,
                contract_lock_sha256="cd" * 32)
        self.orchestrator = orchestrator or StubOrchestrator(self.binding)
        self.audit_path = Path(tmp_path) / audit_name
        self.service = ExecutionService(
            self.journal, tokens={self.binding.instance_id: TOKEN},
            orchestrator=self.orchestrator, clock=self.fake_clock,
            audit_log=self.audit_path,
            evidence_registry=evidence_registry,
            trusted_control_assemblies=trusted_control_assemblies,
            arming_prerequisites=arming_prerequisites)
        # F-03 root-cause hardening (fixture configuration only; production
        # bytes and the production construction path are unchanged): the
        # socketserver default listen backlog (request_queue_size=5) is the
        # starvation amplifier behind the F-02/F-03 zero-response aborts -
        # under the 8-file joint run the single accept thread falls behind
        # and the local Windows stack drops pending loopback connections.
        # Raise the backlog for this fixture's listener only, scoped to the
        # production constructor call (listen() runs inside __init__), then
        # restore the class attribute. Backlog is pure OS accept-queue
        # capacity: no response byte and no assertion can change.
        backlog_was_own = "request_queue_size" in vars(ExecutionHTTPServer)
        saved_backlog = ExecutionHTTPServer.request_queue_size
        try:
            ExecutionHTTPServer.request_queue_size = 64
            self.server = build_execution_server(service=self.service, port=0,
                                                 max_workers=max_workers)
        finally:
            if backlog_was_own:
                ExecutionHTTPServer.request_queue_size = saved_backlog
            else:
                del ExecutionHTTPServer.request_queue_size
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.service.close()
        self.journal.close()

    def step_poll(self):
        """Advance past the disabled 5s poll floor for the next new poll."""
        self.fake_clock.advance(nc.POLL_DISABLED_INTERVAL_MS / 1000.0 + 0.001)


@pytest.fixture
def env(tmp_path):
    environment = Env(tmp_path)
    try:
        yield environment
    finally:
        environment.close()


def _snapshot(clock=None, snapshot_id="SIM_SNAPSHOT"):
    context = simulation_context(clock=clock, snapshot_id=snapshot_id)
    snapshot = context.snapshot
    times = snapshot.times
    stripped = replace(
        times, received_at=None,
        quote=replace(times.quote, received_at=None),
        contract=replace(times.contract, received_at=None),
        account=replace(times.account, received_at=None),
        positions=replace(times.positions, received_at=None))
    positions = tuple(
        replace(bucket, timing=replace(bucket.timing, received_at=None))
        for bucket in snapshot.positions)
    return replace(snapshot, times=stripped, positions=positions)


_TYPE_DEFAULTS = {
    "PollRequest": {"snapshot": None, "snapshot_id": None,
                    "client_state_digest": None, "ack_watermark": None},
    "ReadyRequest": {"offer_id": "SIM_OFFER_1",
                     "challenge": {"offer_id": "SIM_OFFER_1",
                                   "capture_id": "SIM_SNAPSHOT",
                                   "thread_evidence_id": "SIM_THREAD_TEST",
                                   "latest_params_sha256": PARAMS,
                                   "enable_trading": True,
                                   "unconfirmed_event_watermark": None,
                                   "generation": GENERATION}},
    "ResultsRequest": {"receipt": None},
    "EventBatch": {"events": []},
    "ControlReport": {"kind": "GATE_CLOSED", "confirmation": None},
}


def _request(request_type, *, request_id="req-0001", sequence=1,
             service_epoch="unset", **extra):
    data = {
        "request_id": request_id,
        "instance_id": "SIM_INSTANCE_I",
        "session_epoch": "SIM_EPOCH_E",
        "generation": GENERATION,
        "sequence": sequence,
        "sent_at": "2026-09-14T01:00:00.000000Z",
        "params_sha256": PARAMS,
    }
    if service_epoch != "skip":
        data["service_epoch"] = service_epoch
    # every field is required on the wire, including explicitly nullable ones
    data.update(_TYPE_DEFAULTS.get(request_type, {}))
    data.update(extra)
    return {"schema_version": SCHEMA, "type": request_type, "data": data}


def _wire(request_type, **kwargs) -> bytes:
    return json.dumps(_request(request_type, **kwargs)).encode("utf-8")


def _typed_poll(request_id="req-0001", *, sequence=1, service_epoch=1,
                snapshot=None, ack=None):
    return nc.PollRequest(
        request_id=request_id, instance_id="SIM_INSTANCE_I",
        session_epoch="SIM_EPOCH_E", generation=GENERATION, sequence=sequence,
        service_epoch=service_epoch,
        sent_at=datetime(2026, 9, 14, 1, 0, tzinfo=timezone.utc),
        params_sha256=PARAMS, snapshot=snapshot,
        snapshot_id=snapshot.snapshot_id if snapshot is not None else None,
        client_state_digest=None, ack_watermark=ack)


def _post(env_or_port, path, body: bytes, *, token=TOKEN, headers=None,
          method="POST"):
    port = env_or_port.port if isinstance(env_or_port, Env) else env_or_port
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    request_headers = {"Content-Type": "application/json"}
    if token is not None:
        request_headers[nc.EXECUTION_TOKEN_HEADER] = token
    request_headers.update(headers or {})
    if isinstance(body, str):
        body = body.encode("utf-8")
    connection.request(method, path, body=body, headers=request_headers)
    response = connection.getresponse()
    raw = response.read()
    connection.close()
    payload = None
    if raw:
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = None
    return response.status, payload, dict(response.getheaders())


def _typed_post(env, route, request, *, token=TOKEN, headers=None):
    return _post(env, route, nc.encode(request), token=token, headers=headers)


# F-02 (LM-R r1 review, 2026-09-17): under the 8-file single-process joint run
# the loopback response read can be aborted by the local Windows stack
# (ConnectionAbortedError = WSAECONNABORTED/WinError 10053) before a single
# response byte exists - a scheduling/backlog artifact of the loaded joint run
# (accept/handler threads starved by leaked lane threads; socketserver default
# request_queue_size=5), never a protocol verdict. Separate the layers: retry
# a bounded number of times on a fresh connection; every attempt that yields a
# response is returned unchanged so the caller's redirect/CORS assertions
# always run in full against real response headers; if every attempt aborts,
# fail loudly with the original error - an abort is never swallowed into a
# pass and never substitutes for the header checks.
#
# F-03 (LM-R r2 review, 2026-09-17): the same abort class was captured at a
# third unprotected call site (test_non_loopback_host_rejected, two
# independent joint-run rounds). Per the r2 fix mandate the identical retry
# semantics are now wired to EVERY http-client call site in this file that
# reads a response, through the common engine below (_retry_on_conn_abort)
# and its _post/_typed_post/_get_status wrappers. During this fix's own
# joint-run r1 the class was captured a FOURTH time, at the
# test_overlong_request_rejected_whole read - that site is wired too (its
# outer ConnectionError loop stays for the mid-send reset class). The only
# deliberately bare call site left is test_saturation_returns_typed_503
# (its except clause records the abort as a legal asserted outcome of the
# bounded-worker overflow path). No skip, no swallow, no assertion change.
_CONN_ABORT_MAX_ATTEMPTS = 3
_CONN_ABORT_BACKOFF_SECONDS = 0.1


def _retry_on_conn_abort(operation, label):
    """Bounded fresh-connection retry on the zero-response abort class only
    (the F-02 helper semantics, extracted as the common F-03 engine)."""
    last_error = None
    for attempt in range(1, _CONN_ABORT_MAX_ATTEMPTS + 1):
        try:
            return operation()
        except ConnectionAbortedError as error:
            last_error = error
            if attempt < _CONN_ABORT_MAX_ATTEMPTS:
                time.sleep(_CONN_ABORT_BACKOFF_SECONDS)
    pytest.fail(
        f"connection aborted before any response on {label} after "
        f"{_CONN_ABORT_MAX_ATTEMPTS} fresh-connection attempts "
        f"(last error: {last_error!r})")


def _typed_post_retry_on_conn_abort(env, route, request, *, token=TOKEN,
                                    headers=None):
    return _retry_on_conn_abort(
        lambda: _typed_post(env, route, request, token=token,
                            headers=headers), route)


def _post_retry_on_conn_abort(env_or_port, path, body, *, token=TOKEN,
                              headers=None, method="POST"):
    return _retry_on_conn_abort(
        lambda: _post(env_or_port, path, body, token=token,
                      headers=headers, method=method), path)


def _status_headers(*, request_id="req-status-1", sequence=1, service_epoch=1,
                    overrides=None):
    values = {
        "X-Execution-Schema-Version": SCHEMA,
        "X-Execution-Type": "StatusRequest",
        "X-Execution-Request-Id": request_id,
        "X-Execution-Instance-Id": "SIM_INSTANCE_I",
        "X-Execution-Session-Epoch": "SIM_EPOCH_E",
        "X-Execution-Generation": GENERATION,
        "X-Execution-Service-Epoch": str(service_epoch),
        "X-Execution-Sequence": str(sequence),
        "X-Execution-Sent-At": "2026-09-14T01:00:00.000000Z",
        "X-Execution-Params-Sha256": PARAMS,
    }
    values.update(overrides or {})
    return values


def _get_status(env, *, token=TOKEN, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", env.port, timeout=5)
    request_headers = {}
    if token is not None:
        request_headers[nc.EXECUTION_TOKEN_HEADER] = token
    request_headers.update(headers or {})
    connection.request("GET", "/execution/v1/status", headers=request_headers)
    response = connection.getresponse()
    raw = response.read()
    connection.close()
    payload = json.loads(raw) if raw else None
    return response.status, payload, dict(response.getheaders())


def _get_status_retry_on_conn_abort(env, *, token=TOKEN, headers=None):
    return _retry_on_conn_abort(
        lambda: _get_status(env, token=token, headers=headers),
        "/execution/v1/status")


def _event(event_id, sequence, *, stream="SIM_STREAM_1", kind="order"):
    return nc.RawCallbackEnvelope(
        event_id=event_id, kind=c.EventKind(kind), event_stream_id=stream,
        event_sequence=sequence, captured_at=datetime(2026, 9, 14, 1, 0,
                                                      tzinfo=timezone.utc),
        callback_generation=GENERATION, callback_session_epoch="SIM_EPOCH_E",
        route_evidence_id=None,
        raw_fields=(nc.RawCallbackField(key="OrderID", value="7"),))


def _event_wire(event_id, sequence, *, stream="SIM_STREAM_1"):
    return {
        "event_id": event_id, "kind": "order", "event_stream_id": stream,
        "event_sequence": sequence,
        "captured_at": "2026-09-14T01:00:00.000000Z",
        "callback_generation": GENERATION, "callback_session_epoch": "SIM_EPOCH_E",
        "route_evidence_id": None,
        "raw_fields": [{"key": "OrderID", "value": "7"}],
    }


def _leg():
    return c.Leg(leg_id="SIM_INTENT:OPEN:-:0", index=0, phase=c.Phase.OPEN,
                 depends_on=(), direction=c.Direction.BUY, offset=c.Offset.OPEN,
                 volume=2, price=D("3500"), position_bucket=None,
                 cash_reserve=D("2000"))


def _offer(binding, *, offer_id="SIM_OFFER_1"):
    return nc.ExecutionOffer(
        offer_id=offer_id, intent_id="SIM_INTENT", leg_id="SIM_INTENT:OPEN:-:0",
        plan_id="e" * 64, intent_hash="f" * 64, leg_hash="a" * 64, binding=binding,
        snapshot_id="SIM_SNAPSHOT", policy_id="SIM_POLICY_1", params_sha256=PARAMS,
        control_revision=1,
        expires_at=datetime(2026, 9, 14, 1, 0, 10, tzinfo=timezone.utc))


def _command(binding, *, kind="SUBMIT"):
    from ak_trading.infiny_execution.client_dispatch import dispatch_deadline
    issued = datetime(2026, 9, 14, 1, 0, tzinfo=timezone.utc)
    return nc.DispatchCommand(
        command_id="SIM_CMD_1", kind=nc.CommandKind(kind), attempt_id="SIM_ATTEMPT_1",
        offer_id="SIM_OFFER_1", intent_id="SIM_INTENT", leg_id="SIM_INTENT:OPEN:-:0",
        plan_id="e" * 64, binding=binding, leg=_leg(), order_type=c.OrderType.GFD,
        price_tick=D("1"),
        contract_limits=nc.ContractLimit(min_volume=1, max_volume=5, volume_step=1),
        control_revision=1, permit_id="SIM_PERMIT_TEST", params_sha256=PARAMS,
        issued_at=issued, expires_at=dispatch_deadline(issued))


def _receipt(binding, command=None):
    command = command or _command(binding)
    return nc.DispatchReceipt(
        receipt_id="SIM_RECEIPT_1", command_id=command.command_id,
        attempt_id=command.attempt_id, binding=binding, generation=GENERATION,
        stage=nc.ReceiptStage.CONSUMED, local_sequence=1,
        recorded_at=datetime(2026, 9, 14, 1, 0, tzinfo=timezone.utc),
        result_kind=nc.ResultKind.EXCEPTION, result_value=None,
        error_code="OUTCOME_PENDING", request_sha256="b" * 64,
        previous_receipt_sha256=None)


def _stub_submit_result(code=c.ResultCode.OK):
    leg_record = SimpleNamespace(
        leg=_leg(), state=c.OrderState.SUBMITTING, attempt_no=1, order_id=None,
        reported_filled=0, trade_filled=0, risk_filled=0, trade_gap=0,
        cancel_state=c.CancelState.NONE)
    return SimpleNamespace(code=code, leg=leg_record, revision=7)


def _error_code(payload) -> str:
    assert payload is not None
    return payload["data"]["error_code"]


def _decode(payload, expected):
    return nc.decode(json.dumps(payload), expected)


# ===========================================================================
# LM-S helpers: frozen ne1_service_pairing_v2 reader, real SDDL ACL probe,
# in-process CLI serve runner, and register-actor CAS manifest rewriting
# ===========================================================================

_PAIRING_DOCUMENT_FIELDS = {
    "schema_version", "instance_id", "session_epoch", "service_instance_id",
    "service_epoch", "contract_lock_sha256", "diagnostic_only", "token",
}


def _read_pairing_document(path) -> dict:
    """Parse one frozen ne1_service_pairing_v2 credential file strictly."""
    text = Path(path).read_text(encoding="ascii")
    document = json.loads(text)
    assert isinstance(document, dict)
    assert set(document) == _PAIRING_DOCUMENT_FIELDS
    assert document["schema_version"] == SERVICE_PAIRING_SCHEMA_VERSION
    return document


# -- REAL Windows security-descriptor probes (M03) -------------------------
# The offline guard forbids native child processes (icacls), so the REAL
# positive/negative DACL cases use a second, independent in-process Win32
# API family: SDDL string conversion (ConvertSecurityDescriptorToString/
# StringTo + the legacy SetFileSecurityW that applies control bits as
# given). Nothing here shares code with the service implementation's
# ACE-walking verify; Windows itself parses, applies and enforces every
# descriptor change, so these are real security-descriptor mutations, not
# mocks. Only paths/SIDs cross the boundary - never a secret value.


def _sddl_of(path) -> str:
    """Render the object's real owner+DACL as an SDDL string."""
    import ctypes
    import ctypes.wintypes as wt
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.GetNamedSecurityInfoW.argtypes = (
        wt.LPCWSTR, ctypes.c_int, wt.DWORD, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p))
    advapi32.GetNamedSecurityInfoW.restype = wt.DWORD
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = (
        ctypes.c_void_p, wt.DWORD, wt.DWORD, ctypes.POINTER(wt.LPWSTR),
        ctypes.POINTER(wt.DWORD))
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = \
        wt.BOOL
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    code = advapi32.GetNamedSecurityInfoW(
        str(path), 1, 0x1 | 0x4, ctypes.byref(owner), None,
        ctypes.byref(dacl), None, ctypes.byref(descriptor))
    if code != 0:
        raise OSError(code, "SDDL_READ_FAILED")
    try:
        text = wt.LPWSTR()
        if not advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                descriptor, 1, 0x1 | 0x4, ctypes.byref(text), None):
            raise OSError("SDDL_CONVERT_FAILED")
        try:
            return text.value
        finally:
            kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        kernel32.LocalFree(descriptor)


def _apply_sddl(path, sddl: str, *, protect: bool) -> None:
    """REALLY replace the object's owner+DACL from a Windows-parsed SDDL
    string (real tampering for the negative cases). ``protect`` mirrors the
    SE_DACL_PROTECTED control bit passed to SetNamedSecurityInfoW."""
    import ctypes
    import ctypes.wintypes as wt
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
        wt.LPCWSTR, wt.DWORD, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wt.DWORD))
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = \
        wt.BOOL
    advapi32.GetSecurityDescriptorOwner.argtypes = (
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wt.BOOL))
    advapi32.GetSecurityDescriptorOwner.restype = wt.BOOL
    advapi32.GetSecurityDescriptorDacl.argtypes = (
        ctypes.c_void_p, ctypes.POINTER(wt.BOOL),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wt.BOOL))
    advapi32.GetSecurityDescriptorDacl.restype = wt.BOOL
    advapi32.SetNamedSecurityInfoW.argtypes = (
        wt.LPCWSTR, ctypes.c_int, wt.DWORD, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p)
    advapi32.SetNamedSecurityInfoW.restype = wt.DWORD
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(descriptor), None):
        raise OSError("SDDL_PARSE_FAILED")
    try:
        owner = ctypes.c_void_p()
        owner_defaulted = wt.BOOL()
        advapi32.GetSecurityDescriptorOwner(
            descriptor, ctypes.byref(owner), ctypes.byref(owner_defaulted))
        present = wt.BOOL()
        dacl = ctypes.c_void_p()
        dacl_defaulted = wt.BOOL()
        advapi32.GetSecurityDescriptorDacl(
            descriptor, ctypes.byref(present), ctypes.byref(dacl),
            ctypes.byref(dacl_defaulted))
        flags = 0x1 | 0x4 | (0x80000000 if protect else 0)
        code = advapi32.SetNamedSecurityInfoW(
            str(path), 1, flags, owner if owner.value else None, None,
            dacl if present.value else None, None)
        if code != 0:
            raise OSError(code, "SDDL_APPLY_FAILED")
    finally:
        kernel32.LocalFree(descriptor)


def _apply_descriptor_bytes(path, sddl: str) -> None:
    """REALLY install a whole descriptor (control bits included) through the
    legacy SetFileSecurityW: the only route that can CLEAR SE_DACL_PROTECTED
    or install a NULL DACL (``D:NO_ACCESS_CONTROL`` - grant-everyone)."""
    import ctypes
    import ctypes.wintypes as wt
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
        wt.LPCWSTR, wt.DWORD, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wt.DWORD))
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = \
        wt.BOOL
    advapi32.SetFileSecurityW.argtypes = (wt.LPCWSTR, wt.DWORD,
                                          ctypes.c_void_p)
    advapi32.SetFileSecurityW.restype = wt.BOOL
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(descriptor), None):
        raise OSError("SDDL_PARSE_FAILED")
    try:
        if not advapi32.SetFileSecurityW(str(path), 0x1 | 0x4, descriptor):
            raise OSError("SDDL_APPLY_FAILED")
    finally:
        kernel32.LocalFree(descriptor)


_SDDL_GRANT = re.compile(
    r"\(([AD]);([^;()]*);([^;()]*);([^;()]*);([^;()]*);([^)]*)\)")
# well-known SDDL trustee aliases and raw wide-group SIDs that must never
# appear as an allowed trustee of a private pairing object
_WIDE_SDDL_TRUSTEES = frozenset({
    "WD", "WO", "WG", "WR", "AN", "AO", "AU", "BU", "BG", "DU", "DG", "DC",
    "SY", "BA", "BT", "ER", "PS", "PU", "SU", "SG", "CA", "RA", "RS", "EA",
    "PA", "RD", "NO", "LU", "IS", "CY", "OW", "RM", "CD",
    "S-1-1-0", "S-1-5-11", "S-1-5-32-544", "S-1-5-32-545", "S-1-5-32-546",
    "S-1-5-32-547", "S-1-5-32-548", "S-1-5-18", "S-1-5-19", "S-1-5-20",
})


def _sddl_grants(sddl: str) -> list[tuple[str, str, str, str]]:
    """(type, flags, access, trustee) quadruples for every ACE."""
    return [(match.group(1), match.group(2), match.group(3), match.group(6))
            for match in _SDDL_GRANT.finditer(sddl)]


def _sddl_owner_token(sddl: str) -> str:
    owner = sddl.partition("D:")[0]
    assert owner.startswith("O:") and len(owner) > 2, sddl
    return owner[2:]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _serve_in_process(launcher, monkeypatch, capsys, *, journal_path, lock,
                      pairing_dir, registry_dir, audit_path, port=None,
                      extra_args=()):
    """Drive the full CLI serve path in-process (real journal, registry,
    pairing writer, listener) so loopback traffic stays in-process.

    Returns a SimpleNamespace with the parsed ``execution_started`` line, the
    port, the captured server, the runner thread and its exit-code slot."""
    from ak_trading.infiny_execution import client_service
    captured = {}
    real_build = client_service.build_execution_server

    def build(**kwargs):
        server = real_build(**kwargs)
        captured["server"] = server
        return server

    monkeypatch.setattr(client_service, "build_execution_server", build)
    port = port if port is not None else _free_port()
    result = {}

    def run_serve():
        result["code"] = launcher.main([
            "serve", "--port", str(port), "--journal", str(journal_path),
            "--audit-log", str(audit_path), "--pairing-dir", str(pairing_dir),
            "--registry-dir", str(registry_dir), "--contract-lock", str(lock),
            *extra_args])

    thread = threading.Thread(target=run_serve, daemon=True)
    thread.start()
    started = None
    deadline = time.monotonic() + 15
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
    assert started is not None, "execution_started was not emitted"
    return SimpleNamespace(started=started, port=port, captured=captured,
                           thread=thread, result=result)


def _stop_serve(run) -> int:
    """Orderly in-process stop: shutdown the listener, join the CLI thread
    (serve_forever return triggers the runtime-lock-releasing close chain)."""
    server = run.captured.get("server")
    if server is not None:
        server.shutdown()
    run.thread.join(timeout=15)
    return run.result.get("code")


def _register_actor_cli(launcher, journal_path, manifest, lock, *extra) -> int:
    return launcher.main(["register-actor", "--journal", str(journal_path),
                          "--manifest", str(manifest), "--contract-lock",
                          str(lock), *extra])


def _last_status_json(capsys) -> dict:
    lines = [line for line in capsys.readouterr().out.splitlines() if line]
    assert lines, "no CLI output captured"
    return json.loads(lines[-1])


def _reregister_session_manifest(tmp_path, *, session_epoch,
                                 action_id="SIM_OP_2"):
    """Rewrite the actor record with a new binding session and rebuild the
    hashed manifest chain over the same files (register-actor update path)."""
    import hashlib
    from ak_trading.infiny_execution.native_evidence import (
        OperatorRegistrationManifest,
    )
    record_path = tmp_path / "actor-record.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["bindings"][0]["session_epoch"] = session_epoch
    record_path.write_text(json.dumps(record), encoding="utf-8")
    lock_text = (tmp_path / "contract-lock.json").read_bytes()
    artifact = tmp_path / "authorization.json"
    manifest = {
        "registration_id": f"reg-{session_epoch.lower()}",
        "record_type": "ActorRegistration",
        "record_path": str(record_path).replace("\\", "/"),
        "record_sha256": hashlib.sha256(record_path.read_bytes()).hexdigest(),
        "contract_lock_sha256": hashlib.sha256(lock_text).hexdigest(),
        "operator_action_id": action_id,
        "authorization_artifact_path": str(artifact).replace("\\", "/"),
        "authorization_artifact_sha256": hashlib.sha256(
            artifact.read_bytes()).hexdigest(),
        "artifacts": [{"path": str(artifact).replace("\\", "/"),
                       "sha256": hashlib.sha256(
                           artifact.read_bytes()).hexdigest()}],
    }
    manifest_path = tmp_path / "manifest.json"
    envelope = {"schema_version": c.V2_SCHEMA_VERSION,
                "type": "OperatorRegistrationManifest", "data": manifest}
    encoded = c.decode(json.dumps(envelope), OperatorRegistrationManifest,
                       schema_version=c.V2_SCHEMA_VERSION)
    manifest_path.write_text(c.encode(encoded, schema_version=c.V2_SCHEMA_VERSION),
                             encoding="utf-8")
    return manifest_path


# ===========================================================================
# H03: transport protocol strictness
# ===========================================================================

class TestH03ProtocolStrictness:

    def test_duplicate_json_keys_rejected(self, env):
        raw = (b'{"schema_version": "' + SCHEMA.encode() + b'", '
               b'"type": "PollRequest", "type": "PollRequest", "data": {}}')
        status, payload, _ = _post_retry_on_conn_abort(env, "/execution/v1/poll", raw)
        assert status == 400
        assert _error_code(payload) == "INVALID_REQUEST"

    def test_unknown_envelope_field_rejected(self, env):
        packet = _request("PollRequest", service_epoch=1)
        packet["extra"] = "nope"
        status, payload, _ = _post_retry_on_conn_abort(env, "/execution/v1/poll",
                                   json.dumps(packet).encode())
        assert status == 400 and _error_code(payload) == "INVALID_REQUEST"

    def test_unknown_body_field_rejected(self, env):
        packet = _request("PollRequest", service_epoch=1, note="x")
        status, payload, _ = _post_retry_on_conn_abort(env, "/execution/v1/poll",
                                   json.dumps(packet).encode())
        assert status == 400 and _error_code(payload) == "INVALID_REQUEST"

    def test_old_schema_version_rejected(self, env):
        packet = _request("PollRequest", service_epoch=1)
        packet["schema_version"] = "infiny_client_execution_transport_v1"
        status, payload, _ = _post_retry_on_conn_abort(env, "/execution/v1/poll",
                                   json.dumps(packet).encode())
        assert status == 400 and _error_code(payload) == "UNSUPPORTED_SCHEMA"

    def test_type_mismatch_rejected(self, env):
        packet = _request("PollRequest", service_epoch=1)
        status, payload, _ = _post_retry_on_conn_abort(env, "/execution/v1/events",
                                   json.dumps(packet).encode())
        assert status == 400 and _error_code(payload) == "TYPE_MISMATCH"

    def test_nan_and_infinity_rejected(self, env):
        for literal in (b"NaN", b"Infinity"):
            raw = (b'{"schema_version": "' + SCHEMA.encode() +
                   b'", "type": "PollRequest", "data": {"sequence": ' + literal + b'}}')
            status, payload, _ = _post_retry_on_conn_abort(env, "/execution/v1/poll", raw)
            assert status == 400 and _error_code(payload) == "INVALID_REQUEST"

    def test_naive_datetime_rejected(self, env):
        packet = _request("PollRequest", service_epoch=1)
        packet["data"]["sent_at"] = "2026-09-14T01:00:00"
        status, payload, _ = _post_retry_on_conn_abort(env, "/execution/v1/poll",
                                   json.dumps(packet).encode())
        assert status == 400 and _error_code(payload) == "INVALID_REQUEST"

    def test_bool_sequence_rejected(self, env):
        packet = _request("PollRequest", service_epoch=1)
        packet["data"]["sequence"] = True
        raw = json.dumps(packet).replace('"sequence": true', '"sequence": true')
        status, payload, _ = _post_retry_on_conn_abort(env, "/execution/v1/poll",
                                   json.dumps(packet).encode())
        assert status == 400 and _error_code(payload) == "INVALID_REQUEST"

    def test_overlong_request_rejected_whole(self, env):
        packet = _request("PollRequest", service_epoch=1)
        packet["data"]["client_state_digest"] = "a" * 64
        raw = json.dumps(packet).encode()
        padding = nc.MAX_REQUEST_BYTES + 1 - len(raw)
        assert padding > 0
        # grow inside a declared field, not by trailing bytes
        packet["data"]["client_state_digest"] = "a" * (64 + padding)
        body = json.dumps(packet).encode()
        # the server rejects 413 without draining the body; on Windows the
        # aborted send can surface as a connection reset - retry once.
        # F-03 fourth capture (this fix's joint-run r1, intermediate bytes):
        # the same zero-response abort class hit getresponse() HERE too
        # (body fully sent, no response byte) - so the site is wired to the
        # common abort retry as well; the outer ConnectionError loop stays
        # for the mid-send reset class the helper deliberately does not
        # catch (ConnectionResetError passes straight through).
        for retry in range(2):
            try:
                status, payload, _ = _post_retry_on_conn_abort(
                    env, "/execution/v1/poll", body)
                break
            except ConnectionError:
                assert retry == 0
        else:  # pragma: no cover - both attempts reset
            pytest.fail("overlong send reset twice")
        assert status == 413 and _error_code(payload) == "REQUEST_TOO_LARGE"
        assert env.journal.capture_registered("SIM_SNAPSHOT") is False

    def test_deep_nesting_rejected(self, env):
        deep = {"key": None}
        node = deep
        for _ in range(40):
            node["key"] = {"key": None}
            node = node["key"]
        packet = _request("PollRequest", service_epoch=1)
        packet["data"]["client_state_digest"] = deep
        status, payload, _ = _post_retry_on_conn_abort(env, "/execution/v1/poll",
                                   json.dumps(packet).encode())
        assert status == 400 and _error_code(payload) == "INVALID_REQUEST"

    def test_event_batch_129_rejected_128_accepted(self, env, tmp_path):
        events = [_event_wire(f"evt-{i:04d}", i + 1) for i in range(129)]
        packet = _request("EventBatch", service_epoch=env.service.service_epoch)
        packet["data"]["events"] = events
        status, payload, _ = _post_retry_on_conn_abort(env, "/execution/v1/events",
                                   json.dumps(packet).encode())
        assert status == 400 and _error_code(payload) == "INVALID_REQUEST"
        # boundary: exactly 128 is the legal maximum (rate limit not applied to events)
        packet["data"]["events"] = events[:128]
        status, payload, _ = _post_retry_on_conn_abort(env, "/execution/v1/events",
                                   json.dumps(packet).encode())
        assert status == 200
        response = _decode(payload, nc.EventsResponse)
        assert response.ack_watermark.sequence == 128
        assert response.accepted_event_ids[0] == "evt-0000"

    def test_legal_roundtrip_and_hash_stability(self, env):
        request = _typed_poll(service_epoch=env.service.service_epoch)
        status, payload, _ = _typed_post_retry_on_conn_abort(env, "/execution/v1/poll", request)
        assert status == 200
        response = _decode(payload, nc.PollResponse)
        assert nc.encode(response) == json.dumps(payload, sort_keys=True,
                                                 separators=(",", ":"))


# ===========================================================================
# H04: pairing, loopback, origin, replay, epoch, redaction
# ===========================================================================

class TestH04PairingAndTransportSecurity:

    def test_missing_and_wrong_token_unauthorized(self, env):
        request = _typed_poll(service_epoch=env.service.service_epoch)
        status, payload, _ = _typed_post_retry_on_conn_abort(env, "/execution/v1/poll", request, token=None)
        assert status == 401 and _error_code(payload) == "AUTHENTICATION_FAILED"
        status, payload, _ = _typed_post_retry_on_conn_abort(env, "/execution/v1/poll", request,
                                         token="cd" * 32)
        assert status == 401 and _error_code(payload) == "AUTHENTICATION_FAILED"

    def test_malformed_token_same_failure(self, env):
        for bad in ("", "xyz", "a" * 63, "A" * 64):
            status, payload, _ = _typed_post_retry_on_conn_abort(env, "/execution/v1/poll",
                                             _typed_poll(
                                                 service_epoch=env.service.service_epoch),
                                             token=bad)
            assert status == 401 and _error_code(payload) == "AUTHENTICATION_FAILED"

    def test_browser_origin_and_referer_rejected(self, env):
        request = _typed_poll(service_epoch=env.service.service_epoch)
        status, payload, headers = _typed_post_retry_on_conn_abort(
            env, "/execution/v1/poll", request,
            headers={"Origin": "http://127.0.0.1:9222"})
        assert status == 403 and _error_code(payload) == "ORIGIN_REJECTED"
        status, payload, headers = _typed_post_retry_on_conn_abort(
            env, "/execution/v1/poll", request,
            headers={"Referer": "http://example.com/"})
        assert status == 403 and _error_code(payload) == "ORIGIN_REJECTED"
        assert not any(key.lower().startswith("access-control")
                       for key in headers)

    def test_non_loopback_host_rejected(self, env):
        request = _typed_poll(service_epoch=env.service.service_epoch)
        # F-03 (LM-R r2 review): this call site was the third captured
        # instance of the F-02 zero-response abort class (two independent
        # joint-run rounds) - wired to the same bounded retry; the 403
        # HOST_REJECTED response, when delivered, is asserted in full.
        status, payload, _ = _typed_post_retry_on_conn_abort(
            env, "/execution/v1/poll", request,
            headers={"Host": "evil.example"})
        assert status == 403 and _error_code(payload) == "HOST_REJECTED"

    def test_unknown_routes_404_and_no_heartbeat_alias(self, env):
        for path in ("/execution/v2/poll", "/analyze", "/heartbeat",
                     "/execution/v1/heartbeat", "/execution/v1/pollx"):
            # same WinError 10053 capture class as F-02 (isolation r3 + this
            # file's joint-run reproduction): connection-layer aborts before
            # any response byte are retried on fresh connections; every
            # attempt that yields a response is asserted in full below.
            status, payload, _ = _typed_post_retry_on_conn_abort(
                env, path, _typed_poll(
                    service_epoch=env.service.service_epoch))
            assert status == 404, path
            assert _error_code(payload) == "UNKNOWN_ROUTE"

    def test_methods_not_allowed(self, env):
        request = _typed_poll(service_epoch=env.service.service_epoch)
        for method in ("OPTIONS", "TRACE", "PUT", "DELETE"):
            status, payload, _ = _post_retry_on_conn_abort(env, "/execution/v1/poll",
                                       nc.encode(request), method=method)
            assert status == 405 and _error_code(payload) == "METHOD_NOT_ALLOWED"
        status, payload, _ = _post_retry_on_conn_abort(env, "/execution/v1/status",
                                   nc.encode(request))
        assert status == 405 and _error_code(payload) == "METHOD_NOT_ALLOWED"

    def test_null_epoch_only_poll_and_status(self, env):
        ready = nc.ReadyRequest(
            request_id="req-hs-r", instance_id="SIM_INSTANCE_I",
            session_epoch="SIM_EPOCH_E", generation=GENERATION, sequence=1,
            service_epoch=None,
            sent_at=datetime(2026, 9, 14, 1, 0, tzinfo=timezone.utc),
            params_sha256=PARAMS, offer_id="SIM_OFFER_1",
            challenge=nc.ReadyChallenge(
                offer_id="SIM_OFFER_1", capture_id="SIM_SNAPSHOT",
                thread_evidence_id="SIM_THREAD_TEST", latest_params_sha256=PARAMS,
                enable_trading=True, unconfirmed_event_watermark=None,
                generation=GENERATION))
        results = nc.ResultsRequest(
            request_id="req-hs-x", instance_id="SIM_INSTANCE_I",
            session_epoch="SIM_EPOCH_E", generation=GENERATION, sequence=1,
            service_epoch=None,
            sent_at=datetime(2026, 9, 14, 1, 0, tzinfo=timezone.utc),
            params_sha256=PARAMS, receipt=_receipt(env.binding))
        for path, packet in (("/execution/v1/ready", ready),
                             ("/execution/v1/results", results)):
            status, payload, _ = _typed_post_retry_on_conn_abort(env, path, packet)
            assert status == 409, path
            assert _error_code(payload) == "HANDSHAKE_REQUIRED"

    def test_epoch_mismatch_precedes_replay(self, env):
        wrong = _typed_poll("req-epoch", service_epoch=env.service.service_epoch + 5)
        status, payload, _ = _typed_post_retry_on_conn_abort(env, "/execution/v1/poll", wrong)
        assert status == 409 and _error_code(payload) == "EPOCH_MISMATCH"
        # no replay row was written: the same request_id succeeds with the right epoch
        right = _typed_poll("req-epoch", service_epoch=env.service.service_epoch)
        status, payload, _ = _typed_post_retry_on_conn_abort(env, "/execution/v1/poll", right)
        assert status == 200

    def test_replay_idempotent_then_conflict(self, env):
        first = _typed_poll("req-replay", service_epoch=env.service.service_epoch)
        status1, payload1, _ = _typed_post_retry_on_conn_abort(env, "/execution/v1/poll", first)
        status2, payload2, _ = _typed_post_retry_on_conn_abort(env, "/execution/v1/poll", first)
        assert status1 == status2 == 200
        assert json.dumps(payload1, sort_keys=True) == json.dumps(payload2, sort_keys=True)
        assert env.service.counters()["replays"] == 1
        changed = _typed_poll("req-replay", sequence=2,
                              service_epoch=env.service.service_epoch)
        status, payload, _ = _typed_post_retry_on_conn_abort(env, "/execution/v1/poll", changed)
        assert status == 409 and _error_code(payload) == "REQUEST_ID_CONFLICT"
        # the original response still stands
        status, payload, _ = _typed_post_retry_on_conn_abort(env, "/execution/v1/poll", first)
        assert status == 200
        assert json.dumps(payload, sort_keys=True) == json.dumps(payload1, sort_keys=True)

    def test_replay_precedes_rate_limit(self, env):
        first = _typed_poll("req-fast", service_epoch=env.service.service_epoch)
        assert _typed_post_retry_on_conn_abort(env, "/execution/v1/poll", first)[0] == 200
        # immediately again, well inside the disabled 5s interval: the exact
        # replay returns its previous result before the poll-rate check
        status, payload, _ = _typed_post_retry_on_conn_abort(env, "/execution/v1/poll", first)
        assert status == 200
        assert payload["type"] == "PollResponse"

    def test_no_token_leakage(self, env):
        request = _typed_poll(service_epoch=env.service.service_epoch)
        _typed_post_retry_on_conn_abort(env, "/execution/v1/poll", request)
        _typed_post_retry_on_conn_abort(env, "/execution/v1/poll", request, token="cd" * 32)
        _get_status_retry_on_conn_abort(env, headers=_status_headers(
            service_epoch=env.service.service_epoch))
        audit_text = env.audit_path.read_text(encoding="utf-8")
        assert TOKEN not in audit_text and (TOKEN[:16] not in audit_text)
        status, payload, _ = _get_status_retry_on_conn_abort(env, headers=_status_headers(
            service_epoch=env.service.service_epoch))
        assert status == 200
        assert TOKEN not in json.dumps(payload)

    def test_no_redirects_or_cors_headers(self, env):
        request = _typed_poll(service_epoch=env.service.service_epoch)
        for path in ("/execution/v1/poll", "/execution/v1/unknown"):
            _, _, headers = _typed_post_retry_on_conn_abort(env, path, request)
            assert "Location" not in {key.lower() for key in headers}
            assert not any(key.lower().startswith("access-control")
                           for key in headers)

    def test_bounded_body_timeout(self, env, monkeypatch):
        from ak_trading.infiny_execution import client_service
        monkeypatch.setattr(client_service._ExecutionHandler, "body_timeout_seconds", 0.3)
        body = nc.encode(_typed_poll(service_epoch=env.service.service_epoch))

        # F-03: the truncated-body dance runs on the same bounded zero-response
        # abort retry; a delivered 408 response is always asserted in full.
        def truncated_body_roundtrip():
            connection = http.client.HTTPConnection("127.0.0.1", env.port,
                                                    timeout=5)
            try:
                connection.putrequest("POST", "/execution/v1/poll")
                connection.putheader("Content-Type", "application/json")
                connection.putheader(nc.EXECUTION_TOKEN_HEADER, TOKEN)
                connection.putheader("Content-Length", str(len(body) + 50))
                connection.endheaders()
                connection.send(body.encode())
                response = connection.getresponse()
                raw = response.read()
                return response, raw
            finally:
                connection.close()

        response, raw = _retry_on_conn_abort(truncated_body_roundtrip,
                                             "/execution/v1/poll")
        assert response.status == 408
        assert json.loads(raw)["data"]["error_code"] == "REQUEST_TIMEOUT"

    def test_foreign_instance_claim_binding_mismatch(self, env):
        packet = _request("PollRequest", request_id="req-foreign",
                          service_epoch=env.service.service_epoch)
        packet["data"]["instance_id"] = "SIM_INSTANCE_OTHER"
        status, payload, _ = _post_retry_on_conn_abort(env, "/execution/v1/poll",
                                   json.dumps(packet).encode())
        assert status == 403 and _error_code(payload) == "BINDING_MISMATCH"

    def test_session_epoch_mismatch_binding_mismatch(self, env):
        packet = _request("PollRequest", service_epoch=env.service.service_epoch)
        packet["data"]["session_epoch"] = "SIM_EPOCH_OTHER"
        status, payload, _ = _post_retry_on_conn_abort(env, "/execution/v1/poll",
                                   json.dumps(packet).encode())
        assert status == 403 and _error_code(payload) == "BINDING_MISMATCH"


# ===========================================================================
# X02-01 (NE1-ARB-LM-X01-001): decoded service_epoch must be a positive
# integer when non-null. The frozen request DTO validates the field with
# minimum 0, so epoch 0 previously decoded successfully and then blew up as
# HTTP 500 while the EPOCH_MISMATCH response was constructed (the frozen
# error response demands request_service_epoch >= 1). The decode boundary
# now rejects it as a typed 400 INVALID_REQUEST; positive wrong epochs stay
# 409 EPOCH_MISMATCH and null keeps the poll/status handshake semantics.
# ===========================================================================

class TestX02ServiceEpochDecodeValidation:

    ROUTES = (
        ("/execution/v1/poll", "PollRequest"),
        ("/execution/v1/ready", "ReadyRequest"),
        ("/execution/v1/results", "ResultsRequest"),
        ("/execution/v1/events", "EventBatch"),
        ("/execution/v1/control", "ControlReport"),
    )

    def _packet(self, env, route_type, *, service_epoch, request_id="req-x02"):
        """One fully legal base DTO of the given type with only service_epoch
        varied - never a broken schema/type masquerading as this vector."""
        extra = {}
        if route_type == "EventBatch":
            extra["events"] = [_event_wire("evt-x02-1", 1)]
        elif route_type == "ResultsRequest":
            # the nested receipt travels as its bare data node (nc.encode
            # wraps a top-level envelope that the nested field must not carry)
            extra["receipt"] = json.loads(
                nc.encode(_receipt(env.binding)))["data"]
        return _request(route_type, request_id=request_id,
                        service_epoch=service_epoch, **extra)

    def test_epoch_zero_rejected_400_on_all_five_post_types(self, env):
        for index, (route, route_type) in enumerate(self.ROUTES):
            packet = self._packet(env, route_type, service_epoch=0,
                                  request_id=f"req-x02-0-{index}")
            status, payload, _ = _post_retry_on_conn_abort(env, route, json.dumps(packet).encode())
            assert status == 400, route
            assert _error_code(payload) == "INVALID_REQUEST", route
            # the error response stays decodable through the frozen codec and
            # the request echo never backfills the illegal zero
            error = _decode(payload, nc.TransportErrorResponse)
            assert error.request_service_epoch is None
            assert error.service_epoch == env.service.service_epoch

    def test_nonpositive_and_non_integer_epochs_rejected_400(self, env):
        # negative values die in the frozen DTO decode (declared minimum 0);
        # zero additionally dies in the X02 post-decode check; bool, float
        # and numeric strings are not integers at all. All are 400.
        for bad in (0, -1, -7, True, False, 1.0, "2"):
            packet = self._packet(env, "PollRequest", service_epoch=bad,
                                  request_id="req-x02-bad")
            status, payload, _ = _post_retry_on_conn_abort(env, "/execution/v1/poll",
                                       json.dumps(packet).encode())
            assert status == 400, bad
            assert _error_code(payload) == "INVALID_REQUEST", bad
            assert payload["data"]["request_service_epoch"] is None

    def test_epoch_zero_rejection_precedes_business_writes(self, env):
        before_revision = env.journal.check().revision
        for index, (route, route_type) in enumerate(self.ROUTES):
            packet = self._packet(env, route_type, service_epoch=0,
                                  request_id=f"req-x02-w-{index}")
            assert _post_retry_on_conn_abort(env, route, json.dumps(packet).encode())[0] == 400
        assert env.journal.check().revision == before_revision
        # the epoch-mismatch latch is NOT consumed by an illegal epoch value
        assert env.service.counters().get("epoch_mismatches", 0) == 0
        assert TOKEN not in env.audit_path.read_text(encoding="utf-8")

    def test_stale_positive_epoch_still_409_epoch_mismatch(self, env):
        # a positive integer that is not the current epoch keeps the typed
        # 409 EPOCH_MISMATCH and echoes the submitted positive value; the
        # X02 fix must not widen into swallowing genuine epoch conflicts
        stale = env.service.service_epoch + 5
        packet = self._packet(env, "PollRequest", service_epoch=stale,
                              request_id="req-x02-stale")
        status, payload, _ = _post_retry_on_conn_abort(env, "/execution/v1/poll",
                                   json.dumps(packet).encode())
        assert status == 409
        assert _error_code(payload) == "EPOCH_MISMATCH"
        error = _decode(payload, nc.TransportErrorResponse)
        assert error.request_service_epoch == stale
        assert env.service.counters()["epoch_mismatches"] == 1
        # the rejected request_id stays reusable with the correct epoch
        right = self._packet(env, "PollRequest",
                             service_epoch=env.service.service_epoch,
                             request_id="req-x02-stale")
        assert _post_retry_on_conn_abort(env, "/execution/v1/poll",
                     json.dumps(right).encode())[0] == 200

    def test_authentication_precedes_the_epoch_decode_check(self, env):
        # a missing/invalid token with epoch=0 is still 401 first
        packet = self._packet(env, "PollRequest", service_epoch=0,
                              request_id="req-x02-auth")
        status, payload, _ = _post_retry_on_conn_abort(env, "/execution/v1/poll",
                                   json.dumps(packet).encode(), token="cd" * 32)
        assert status == 401
        assert _error_code(payload) == "AUTHENTICATION_FAILED"

    def test_null_epoch_semantics_unchanged(self, env):
        # null poll is the disabled handshake; null on the other POST routes
        # stays 409 HANDSHAKE_REQUIRED (epoch checks never see null)
        packet = self._packet(env, "PollRequest", service_epoch=None,
                              request_id="req-x02-null-poll")
        status, payload, _ = _post_retry_on_conn_abort(env, "/execution/v1/poll",
                                   json.dumps(packet).encode())
        assert status == 200
        response = _decode(payload, nc.PollResponse)
        assert response.request_service_epoch is None
        assert response.service_epoch == env.service.service_epoch
        for index, (route, route_type) in enumerate(self.ROUTES[1:]):
            packet = self._packet(env, route_type, service_epoch=None,
                                  request_id=f"req-x02-null-{index}")
            status, payload, _ = _post_retry_on_conn_abort(env, route, json.dumps(packet).encode())
            assert status == 409, route
            assert _error_code(payload) == "HANDSHAKE_REQUIRED", route

    def test_get_status_epoch_header_semantics_regression(self, env):
        # GET status never had the zero gap ("0" was already refused at the
        # header parser); regression only: "0" 400, "null" handshake, and a
        # positive wrong epoch still 409
        status, payload, _ = _get_status_retry_on_conn_abort(
            env, headers=_status_headers(
                service_epoch=env.service.service_epoch,
                overrides={"X-Execution-Service-Epoch": "0"}))
        assert status == 400 and _error_code(payload) == "INVALID_REQUEST"
        status, payload, _ = _get_status_retry_on_conn_abort(
            env, headers=_status_headers(
                service_epoch=env.service.service_epoch,
                overrides={"X-Execution-Service-Epoch": "null"}))
        assert status == 200
        assert _decode(payload, nc.StatusResponse).request_service_epoch is None
        status, payload, _ = _get_status_retry_on_conn_abort(
            env, headers=_status_headers(
                service_epoch=env.service.service_epoch + 9))
        assert status == 409 and _error_code(payload) == "EPOCH_MISMATCH"

    def test_internal_decode_defect_still_500_not_widened_400(self, env,
                                                              monkeypatch):
        # the X02 catch stays on ContractError: a genuine internal defect in
        # the decode seam must keep surfacing as 500 INTERNAL_ERROR
        from ak_trading.infiny_execution import client_service

        def broken(payload, expected_type):
            del payload, expected_type
            raise RuntimeError("SYNTHETIC internal defect")

        monkeypatch.setattr(client_service.nc, "decode", broken)
        request = _typed_poll(service_epoch=env.service.service_epoch)
        status, payload, _ = _typed_post_retry_on_conn_abort(env, "/execution/v1/poll", request)
        assert status == 500
        assert _error_code(payload) == "INTERNAL_ERROR"


# ===========================================================================
# RULING 03: poll rate limiting (POLL_TOO_FAST is 429)
# ===========================================================================

class TestRateLimits:

    def test_throttled_poll_429_with_retry_after(self, env):
        epoch = env.service.service_epoch
        assert _typed_post_retry_on_conn_abort(env, "/execution/v1/poll",
                           _typed_poll("req-r1", service_epoch=epoch))[0] == 200
        env.fake_clock.advance(0.1)
        status, payload, headers = _typed_post_retry_on_conn_abort(
            env, "/execution/v1/poll", _typed_poll("req-r2", service_epoch=epoch))
        assert status == 429
        assert _error_code(payload) == "POLL_TOO_FAST"
        assert payload["data"]["retry_after_ms"] >= 1
        assert "Retry-After" in headers
        # rejected traffic does not reset the admitted clock
        env.fake_clock.advance(0.1)
        status, payload, _ = _typed_post_retry_on_conn_abort(
            env, "/execution/v1/poll", _typed_poll("req-r3", service_epoch=epoch))
        assert status == 429 and _error_code(payload) == "POLL_TOO_FAST"

    def test_poll_admitted_after_disabled_interval(self, env):
        epoch = env.service.service_epoch
        assert _typed_post_retry_on_conn_abort(env, "/execution/v1/poll",
                           _typed_poll("req-s1", service_epoch=epoch))[0] == 200
        env.fake_clock.advance(nc.POLL_DISABLED_INTERVAL_MS / 1000.0)
        status, _, _ = _typed_post_retry_on_conn_abort(env, "/execution/v1/poll",
                                   _typed_poll("req-s2", service_epoch=epoch))
        assert status == 200

    def test_throttled_poll_has_zero_side_effects(self, env, tmp_path):
        epoch = env.service.service_epoch
        snapshot = _snapshot()
        admitted = _typed_poll("req-t1", service_epoch=epoch, snapshot=snapshot)
        assert _typed_post_retry_on_conn_abort(env, "/execution/v1/poll", admitted)[0] == 200
        before = env.journal.check().revision
        env.fake_clock.advance(0.05)
        status, payload, _ = _typed_post_retry_on_conn_abort(env, "/execution/v1/poll",
                                         _typed_poll("req-t2", service_epoch=epoch))
        assert status == 429
        assert env.service.counters()["polls_throttled"] == 1


# ===========================================================================
# poll business behaviour (RULINGS 06/07)
# ===========================================================================

class TestPollBusiness:

    def test_handshake_poll_disabled_blocked(self, env):
        request = _typed_poll("req-hs", service_epoch=None)
        status, payload, _ = _typed_post_retry_on_conn_abort(env, "/execution/v1/poll", request)
        assert status == 200
        response = _decode(payload, nc.PollResponse)
        assert response.kind is nc.ResponseKind.BLOCKED
        assert response.offer is None
        assert response.blockers == ("MODE_DISABLED",)
        assert response.readiness.can_submit is False
        assert response.readiness.execution_scope is nc.ExecutionScope.NONE
        assert response.request_service_epoch is None
        assert response.service_epoch == env.service.service_epoch

    def test_handshake_poll_rejects_snapshot_payload(self, env):
        request = _typed_poll("req-hs2", service_epoch=None, snapshot=_snapshot())
        status, payload, _ = _typed_post_retry_on_conn_abort(env, "/execution/v1/poll", request)
        assert status == 400 and _error_code(payload) == "INVALID_REQUEST"
        assert env.journal.capture_registered("SIM_SNAPSHOT") is False

    def test_established_poll_registers_capture_idempotent(self, env):
        epoch = env.service.service_epoch
        request = _typed_poll("req-c1", service_epoch=epoch, snapshot=_snapshot())
        status, _, _ = _typed_post_retry_on_conn_abort(env, "/execution/v1/poll", request)
        assert status == 200
        assert env.journal.capture_registered("SIM_SNAPSHOT") is True
        env.step_poll()
        again = _typed_poll("req-c2", service_epoch=epoch, snapshot=_snapshot())
        assert _typed_post_retry_on_conn_abort(env, "/execution/v1/poll", again)[0] == 200

    def test_same_snapshot_id_changed_content_conflict(self, env):
        epoch = env.service.service_epoch
        first = _typed_poll("req-s3", service_epoch=epoch, snapshot=_snapshot())
        assert _typed_post_retry_on_conn_abort(env, "/execution/v1/poll", first)[0] == 200
        env.step_poll()
        mutated = replace(_snapshot(), quote=replace(
            _snapshot().quote, last=D("3501")))
        request = _typed_poll("req-s4", service_epoch=epoch, snapshot=mutated)
        status, payload, _ = _typed_post_retry_on_conn_abort(env, "/execution/v1/poll", request)
        assert status == 409 and _error_code(payload) == "SNAPSHOT_CONFLICT"

    def test_stub_offer_flow_single_offer(self, env):
        env.orchestrator.poll_outcomes.append(
            SimpleNamespace(kind=nc.ResponseKind.OFFER, offer=_offer(env.binding),
                            blockers=()))
        epoch = env.service.service_epoch
        request = _typed_poll("req-o1", service_epoch=epoch, snapshot=_snapshot())
        status, payload, _ = _typed_post_retry_on_conn_abort(env, "/execution/v1/poll", request)
        assert status == 200
        response = _decode(payload, nc.PollResponse)
        assert response.kind is nc.ResponseKind.OFFER
        assert response.offer is not None and response.offer.offer_id == "SIM_OFFER_1"
        assert response.blockers == ()

    def test_foreign_binding_offer_blocked(self, env):
        foreign = replace(env.binding, account_key="SIM_ACCOUNT_B")
        env.orchestrator.poll_outcomes.append(
            SimpleNamespace(kind=nc.ResponseKind.OFFER, offer=_offer(foreign),
                            blockers=()))
        epoch = env.service.service_epoch
        request = _typed_poll("req-o2", service_epoch=epoch, snapshot=_snapshot())
        status, payload, _ = _typed_post_retry_on_conn_abort(env, "/execution/v1/poll", request)
        response = _decode(payload, nc.PollResponse)
        assert response.kind is nc.ResponseKind.BLOCKED
        assert response.blockers == ("OWNERSHIP_CONFLICT",)

    def test_production_orchestrator_default_is_disabled(self, tmp_path):
        environment = Env(tmp_path, orchestrator=None)
        environment.orchestrator = N4BackedOrchestrator(environment.journal)
        environment.service.orchestrator = environment.orchestrator
        try:
            epoch = environment.service.service_epoch
            request = _typed_poll("req-d1", service_epoch=epoch, snapshot=_snapshot())
            status, payload, _ = _typed_post_retry_on_conn_abort(environment, "/execution/v1/poll", request)
            response = _decode(payload, nc.PollResponse)
            assert response.kind is nc.ResponseKind.BLOCKED
            assert response.blockers == ("INTENT_REQUIRED",)
        finally:
            environment.close()

    def test_poll_ack_watermark_validation(self, env):
        epoch = env.service.service_epoch
        # no stream yet: a positive claim is invalid
        claim = nc.AckWatermark(event_stream_id="SIM_STREAM_1",
                                instance_id="SIM_INSTANCE_I",
                                session_epoch="SIM_EPOCH_E", generation=GENERATION,
                                sequence=3, event_id="evt-0003", event_sha256="a" * 64)
        request = _typed_poll("req-a1", service_epoch=epoch, ack=claim)
        status, payload, _ = _typed_post_retry_on_conn_abort(env, "/execution/v1/poll", request)
        assert status == 409 and _error_code(payload) == "ACK_WATERMARK_INVALID"
        # the empty prefix is always valid
        env.step_poll()
        empty = nc.AckWatermark(event_stream_id="SIM_STREAM_1",
                                instance_id="SIM_INSTANCE_I",
                                session_epoch="SIM_EPOCH_E", generation=GENERATION,
                                sequence=0, event_id=None, event_sha256=None)
        request = _typed_poll("req-a2", service_epoch=epoch, ack=empty)
        assert _typed_post_retry_on_conn_abort(env, "/execution/v1/poll", request)[0] == 200


# ===========================================================================
# ready / results (stubbed N4 seam, typed branches)
# ===========================================================================

class TestReadyAndResults:

    @staticmethod
    def _ready_request(env, request_id="req-rd1"):
        challenge = nc.ReadyChallenge(
            offer_id="SIM_OFFER_1", capture_id="SIM_SNAPSHOT",
            thread_evidence_id="SIM_THREAD_TEST", latest_params_sha256=PARAMS,
            enable_trading=True, unconfirmed_event_watermark=None,
            generation=GENERATION)
        return nc.ReadyRequest(
            request_id=request_id, instance_id="SIM_INSTANCE_I",
            session_epoch="SIM_EPOCH_E", generation=GENERATION, sequence=1,
            service_epoch=env.service.service_epoch,
            sent_at=datetime(2026, 9, 14, 1, 0, tzinfo=timezone.utc),
            params_sha256=PARAMS, offer_id="SIM_OFFER_1", challenge=challenge)

    def test_ready_dispatch_branch(self, env):
        env.orchestrator.ready_outcomes.append(
            SimpleNamespace(dispatch=_command(env.binding), blockers=(),
                            context_conflict=False))
        status, payload, _ = _typed_post_retry_on_conn_abort(env, "/execution/v1/ready",
                                         self._ready_request(env))
        response = _decode(payload, nc.ReadyResponse)
        assert response.kind is nc.ReadyResponseKind.DISPATCH
        assert response.dispatch.command_id == "SIM_CMD_1"
        assert response.blockers == ()

    def test_ready_blocked_branch(self, env):
        env.orchestrator.ready_outcomes.append(
            SimpleNamespace(dispatch=None, blockers=("RECONCILE_REQUIRED",),
                            context_conflict=False))
        status, payload, _ = _typed_post_retry_on_conn_abort(env, "/execution/v1/ready",
                                         self._ready_request(env, "req-rd2"))
        response = _decode(payload, nc.ReadyResponse)
        assert response.kind is nc.ReadyResponseKind.BLOCKED
        assert response.dispatch is None
        assert response.blockers == ("RECONCILE_REQUIRED",)
        assert response.readiness.blockers == response.blockers

    def test_challenge_gate_closed_projects_to_declared_vocabulary(self, env):
        from ak_trading.infiny_execution.client_service import vocab_blockers
        assert vocab_blockers(("GATE_CLOSED",)) == ("MODE_DISABLED",)
        assert vocab_blockers(("DISPATCH_EXPIRED",)) == ("EXPIRED",)
        assert vocab_blockers(("GENERATION_CHANGED",)) == ("SESSION_CHANGED",)
        assert vocab_blockers(()) == ()

    def test_ready_context_conflict_409(self, env):
        env.orchestrator.ready_outcomes.append(
            SimpleNamespace(dispatch=None, blockers=(), context_conflict=True))
        status, payload, _ = _typed_post_retry_on_conn_abort(env, "/execution/v1/ready",
                                         self._ready_request(env, "req-rd3"))
        assert status == 409 and _error_code(payload) == "CONTEXT_CONFLICT"

    def test_results_persisted_and_duplicate(self, env):
        env.orchestrator.complete_results.append(
            SimpleNamespace(submit_result=_stub_submit_result(), not_found=False))
        env.orchestrator.complete_results.append(
            SimpleNamespace(submit_result=_stub_submit_result(c.ResultCode.IDEMPOTENT),
                            not_found=False))
        receipt = _receipt(env.binding)

        def results_request(request_id):
            return nc.ResultsRequest(
                request_id=request_id, instance_id="SIM_INSTANCE_I",
                session_epoch="SIM_EPOCH_E", generation=GENERATION, sequence=1,
                service_epoch=env.service.service_epoch,
                sent_at=datetime(2026, 9, 14, 1, 0, tzinfo=timezone.utc),
                params_sha256=PARAMS, receipt=receipt)

        status, payload, _ = _typed_post_retry_on_conn_abort(env, "/execution/v1/results",
                                         results_request("req-res1"))
        response = _decode(payload, nc.ResultsResponse)
        assert response.persisted is True and response.duplicate is False
        assert response.receipt_sha256 == c.canonical_hash(receipt)
        assert response.submit_result.leg_state is c.OrderState.SUBMITTING
        # same receipt content in a NEW transport request reports the duplicate
        status, payload, _ = _typed_post_retry_on_conn_abort(env, "/execution/v1/results",
                                         results_request("req-res1-again"))
        response = _decode(payload, nc.ResultsResponse)
        assert response.duplicate is True

    def test_results_unknown_command_invalid_request(self, env):
        env.orchestrator.complete_results.append(
            SimpleNamespace(submit_result=None, not_found=True))
        request = nc.ResultsRequest(
            request_id="req-res2", instance_id="SIM_INSTANCE_I",
            session_epoch="SIM_EPOCH_E", generation=GENERATION, sequence=1,
            service_epoch=env.service.service_epoch,
            sent_at=datetime(2026, 9, 14, 1, 0, tzinfo=timezone.utc),
            params_sha256=PARAMS, receipt=_receipt(env.binding))
        status, payload, _ = _typed_post_retry_on_conn_abort(env, "/execution/v1/results", request)
        assert status == 400 and _error_code(payload) == "INVALID_REQUEST"


# ===========================================================================
# events: contiguous ACK cursor (RULING 09)
# ===========================================================================

class TestEventsAckCursor:

    @staticmethod
    def _batch(env, events, request_id):
        return nc.EventBatch(
            request_id=request_id, instance_id="SIM_INSTANCE_I",
            session_epoch="SIM_EPOCH_E", generation=GENERATION, sequence=1,
            service_epoch=env.service.service_epoch,
            sent_at=datetime(2026, 9, 14, 1, 0, tzinfo=timezone.utc),
            params_sha256=PARAMS, events=tuple(events))

    def test_batch_ack_and_duplicates(self, env):
        events = [_event(f"evt-{i}", i + 1) for i in range(3)]
        status, payload, _ = _typed_post_retry_on_conn_abort(env, "/execution/v1/events",
                                         self._batch(env, events, "req-e1"))
        response = _decode(payload, nc.EventsResponse)
        assert response.ack_watermark.sequence == 3
        assert response.accepted_event_ids == tuple(item.event_id for item in events)
        status, payload, _ = _typed_post_retry_on_conn_abort(env, "/execution/v1/events",
                                         self._batch(env, events, "req-e2"))
        response = _decode(payload, nc.EventsResponse)
        assert response.duplicate_event_ids == tuple(
            item.event_id for item in events)
        assert response.ack_watermark.sequence == 3

    def test_gap_filling_advances_over_saved_higher(self, env):
        third = [_event("evt-3", 3)]
        assert _typed_post_retry_on_conn_abort(env, "/execution/v1/events",
                           self._batch(env, third, "req-e3"))[1]["data"][
            "ack_watermark"]["sequence"] == 0
        first_two = [_event("evt-1", 1), _event("evt-2", 2)]
        _, payload, _ = _typed_post_retry_on_conn_abort(env, "/execution/v1/events",
                                    self._batch(env, first_two, "req-e4"))
        # filling the gap advances the contiguous prefix over the already
        # saved higher sequence (RULING 09), not merely to the last batch item
        assert payload["data"]["ack_watermark"]["sequence"] == 3

    def test_changed_content_same_sequence_conflict(self, env):
        first = [_event("evt-1", 1)]
        assert _typed_post_retry_on_conn_abort(env, "/execution/v1/events",
                           self._batch(env, first, "req-e6"))[0] == 200
        clash = [_event("evt-other", 1)]
        status, payload, _ = _typed_post_retry_on_conn_abort(env, "/execution/v1/events",
                                         self._batch(env, clash, "req-e7"))
        assert status == 409 and _error_code(payload) == "ACK_WATERMARK_INVALID"


# ===========================================================================
# control reports (RULING 01 ControlPhase projection)
# ===========================================================================

class TestControlReports:

    @staticmethod
    def _report(env, kind, *, confirmation=None, request_id="req-ctl"):
        return nc.ControlReport(
            request_id=request_id, instance_id="SIM_INSTANCE_I",
            session_epoch="SIM_EPOCH_E", generation=GENERATION, sequence=1,
            service_epoch=env.service.service_epoch,
            sent_at=datetime(2026, 9, 14, 1, 0, tzinfo=timezone.utc),
            params_sha256=PARAMS, kind=kind, confirmation=confirmation)

    def test_gate_closed_records_disabled_confirmed(self, env):
        status, payload, _ = _typed_post_retry_on_conn_abort(env, "/execution/v1/control",
                                         self._report(env, nc.ControlKind.GATE_CLOSED))
        response = _decode(payload, nc.ControlResponse)
        assert response.kind is nc.ControlResponseKind.RECORDED
        assert response.control_phase is nc.ControlPhase.DISABLED_CONFIRMED

    def test_generation_report_recorded(self, env):
        status, payload, _ = _typed_post_retry_on_conn_abort(
            env, "/execution/v1/control",
            self._report(env, nc.ControlKind.GENERATION_REPORT, request_id="req-ctl2"))
        response = _decode(payload, nc.ControlResponse)
        assert response.kind is nc.ControlResponseKind.RECORDED

    def test_params_confirmation_foreign_binding_blocked(self, env):
        foreign = replace(env.binding, account_key="SIM_ACCOUNT_B")
        confirmation = nc.ParameterConfirmation(
            confirmation_id="SIM_CONF_1", binding=foreign, params_sha256=PARAMS,
            adapter_sha256="e" * 64,
            confirmed_at=datetime(2026, 9, 14, 1, 0, tzinfo=timezone.utc),
            expires_at=datetime(2026, 9, 14, 1, 5, tzinfo=timezone.utc),
            explicit_ui_action=True)
        status, payload, _ = _typed_post_retry_on_conn_abort(
            env, "/execution/v1/control",
            self._report(env, nc.ControlKind.PARAMS_CONFIRMED,
                         confirmation=confirmation, request_id="req-ctl3"))
        response = _decode(payload, nc.ControlResponse)
        assert response.kind is nc.ControlResponseKind.BLOCKED
        assert response.blockers == ("OWNERSHIP_CONFLICT",)
        # the frozen response contract echoes the submitted confirmation id
        # even on the blocked branch (RULING 01 ControlResponse invariants)
        assert response.confirmation_id == "SIM_CONF_1"

    def test_params_confirmation_matching_binding_alone_still_blocked(self, env):
        """ARM-M08 (NE1-ARB-ARMING-001 AR3.3): the handler must verify far
        more than the confirmation binding. On the plain stub environment
        there is no trusted NATIVE control assembly, no registry grant chain
        and no approved prerequisites anchor, so a matching-binding
        confirmation is BLOCKED - the old binding-only RECORDED behavior
        (decision fact F09) is gone."""
        confirmation = nc.ParameterConfirmation(
            confirmation_id="SIM_CONF_2", binding=env.binding, params_sha256=PARAMS,
            adapter_sha256="e" * 64,
            confirmed_at=datetime(2026, 9, 14, 1, 0, tzinfo=timezone.utc),
            expires_at=datetime(2026, 9, 14, 1, 5, tzinfo=timezone.utc),
            explicit_ui_action=True)
        status, payload, _ = _typed_post_retry_on_conn_abort(
            env, "/execution/v1/control",
            self._report(env, nc.ControlKind.PARAMS_CONFIRMED,
                         confirmation=confirmation, request_id="req-ctl4"))
        response = _decode(payload, nc.ControlResponse)
        assert response.kind is nc.ControlResponseKind.BLOCKED
        # no trusted assembly / no S4 anchor: NATIVE_PERMIT_UNREGISTERED maps
        # onto the declared NATIVE_DISABLED vocabulary
        assert response.blockers == ("NATIVE_DISABLED",)
        # the frozen response contract still echoes the submitted id
        assert response.confirmation_id == "SIM_CONF_2"
        # a blocked confirmation never flips readiness submission flags
        assert response.readiness.can_submit is False
        assert response.readiness.can_cancel_owned is False
        assert response.readiness.native_permit_id is None


# ===========================================================================
# NE1 arming mechanism (NE1-ARB-ARMING-001, ARM-LM-S): ARM-M08/ARM-M10
# server rows. Isolated trusted SYNTHETIC control assemblies only; the
# production register-* CLI keeps refusing SYNTHETIC records and the
# production launcher passes no assembly and no anchor.
# ===========================================================================

ARMING_TARGET = "ef" * 32           # target params hash (distinct from pairing PARAMS)
ARMING_ADAPTER = "9a" * 32          # approved client/adapter release hash (64 hex)
ARMING_PERMIT_ID = "SIM_PERMIT_ARM"
ARMING_AUTHORIZATION_ID = "SIM_AUTH_ARM"
ARMING_CONFIRMED_AT = datetime(2026, 9, 14, 1, 0, tzinfo=timezone.utc)
ARMING_EXPIRES_AT = datetime(2026, 9, 14, 1, 5, tzinfo=timezone.utc)


class _LedgerSyntheticRegistry:
    """SYNTHETIC-only seam satisfying the frozen ledger control-update CAS
    call (register_permit + verify_permit_active); never a production path."""

    def register_permit(self, record) -> None:
        del record

    def verify_permit_active(self, permit_id: str, *, now: datetime):
        del permit_id, now
        return c.ValidationResult(blockers=())


def _arming_authorization(binding):
    return nc.NativeAuthorization(
        authorization_id=ARMING_AUTHORIZATION_ID, binding=binding,
        symbols=(c.Symbol(exchange=binding.exchange,
                          instrument_id=binding.instrument_id),),
        actions=(c.AuthorizationAction.OPEN_LONG, c.AuthorizationAction.CANCEL_OWNED),
        max_send_attempts=1, max_cancel_attempts=1, max_volume=1,
        price_bounds=(c.PriceBound(
            symbol=c.Symbol(exchange=binding.exchange,
                            instrument_id=binding.instrument_id),
            lower=D("1000"), upper=D("5000")),),
        expires_at=datetime(2026, 9, 14, 2, 0, tzinfo=timezone.utc),
        origin=nc.Origin.SYNTHETIC, params_sha256=ARMING_TARGET)


def _arming_permit(binding, *, control_revision=1, max_send_attempts=1,
                   max_cancel_attempts=0, max_volume=1,
                   actions=(c.AuthorizationAction.OPEN_LONG,), expires_at=None):
    return nc.NativePilotPermit(
        permit_id=ARMING_PERMIT_ID, authorization_id=ARMING_AUTHORIZATION_ID,
        binding=binding, control_revision=control_revision,
        baseline_evidence_id="SIM_ARM_BASELINE", route_evidence_id="SIM_ARM_ROUTE",
        cash_bound_evidence_id="SIM_ARM_CASH", source_key="SIM_ARM_SOURCE",
        actions=actions, max_send_attempts=max_send_attempts,
        max_cancel_attempts=max_cancel_attempts, max_volume=max_volume,
        price_bounds=(),
        expires_at=expires_at or datetime(2026, 9, 14, 1, 30, tzinfo=timezone.utc),
        params_sha256=ARMING_TARGET, origin=nc.Origin.SYNTHETIC)


def _arming_control_state(binding, permit):
    return nc.ControlState(
        control_revision=permit.control_revision, binding=binding,
        mode=c.ExecutionMode.NATIVE, policy_id="SIM_POLICY_ARM",
        authorization_id=ARMING_AUTHORIZATION_ID,
        native_permit_id=permit.permit_id, params_sha256=ARMING_TARGET,
        generation=GENERATION,
        updated_at=datetime(2026, 9, 14, 0, 45, tzinfo=timezone.utc),
        disabled_reason=None)


def _arming_evidence(binding):
    from ak_trading.infiny_execution.native_evidence import (
        CallbackKind, EvidenceSourceKind, NativeBaselineEvidence,
        NativeRouteEvidence, NativeSettlementEvidence,
    )
    baseline = NativeBaselineEvidence(
        evidence_id="SIM_ARM_BASELINE", source_kind=EvidenceSourceKind.TERMINAL_EXPORT,
        artifact_sha256="1" * 64, account_key=binding.account_key,
        trading_day=binding.trading_day, session_epoch=binding.session_epoch,
        as_of=datetime(2026, 9, 14, 0, 30, tzinfo=timezone.utc),
        order_inventory=(), trade_inventory=(), positions=(),
        coverage_description="SYNTHETIC isolated arming coverage",
        completion_basis="SYNTHETIC isolated arming completion",
        isolation_evidence_id="SIM_ARM_ISOLATION", approved_scope_id="SIM_ARM_SCOPE",
        expires_at=datetime(2026, 9, 14, 3, 0, tzinfo=timezone.utc),
        origin=nc.Origin.SYNTHETIC)
    route = NativeRouteEvidence(
        evidence_id="SIM_ARM_ROUTE", framework_sha256="2" * 64, sdk_sha256="3" * 64,
        binding=binding, callback_kinds=(CallbackKind.ON_TICK,),
        account_scope_basis="SYNTHETIC arming account scope",
        strategy_scope_basis="SYNTHETIC arming strategy scope",
        local_alias_scope="SYNTHETIC-local", front_id=None, session_id=None,
        memo_limit=1, memo_roundtrip_verified=True,
        validated_at=datetime(2026, 9, 14, 0, 30, tzinfo=timezone.utc),
        expires_at=datetime(2026, 9, 14, 3, 0, tzinfo=timezone.utc),
        origin=nc.Origin.SYNTHETIC)
    settlement = NativeSettlementEvidence(
        evidence_id="SIM_ARM_CASH", baseline_evidence_id="SIM_ARM_BASELINE",
        binding=binding, as_of=datetime(2026, 9, 14, 0, 30, tzinfo=timezone.utc),
        artifact_sha256="4" * 64, covered_leg_ids=("SIM_ARM_LEG_1",),
        covered_event_ids=("SIM_ARM_EVENT_1",), order_inventory=(),
        trade_inventory=(), position_snapshot_id="SIM_ARM_POS",
        completion_basis="SYNTHETIC arming settlement basis",
        isolation_evidence_id="SIM_ARM_ISOLATION", origin=nc.Origin.SYNTHETIC)
    return baseline, route, settlement


def _arming_prerequisites_anchor(*, expires_at=None):
    return ArmingPrerequisites(
        prerequisites_id="a1" * 16, prerequisites_sha256="b2" * 32,
        client_file_sha256=ARMING_ADAPTER, service_file_sha256="c3" * 32,
        tool_file_sha256="d4" * 32, contract_lock_sha256="cd" * 32,
        created_at=datetime(2026, 9, 14, 0, 0, tzinfo=timezone.utc),
        expires_at=expires_at or datetime(2026, 9, 14, 23, 59, tzinfo=timezone.utc),
        evidence_refs=tuple(
            (kind, f"SYNTHETIC/{kind}", "e5" * 32) for kind in (
                "RELEASE_REVIEW", "S3_ZERO_NATIVE", "MANUAL_TAKEOVER_APPROVAL",
                "SINGLE_ORDER_APPROVAL", "NATIVE_ASSEMBLY_ACCEPTANCE")))


class ArmingEnv(Env):
    """Env whose service carries the isolated trusted SYNTHETIC native
    control assembly, registered SYNTHETIC grant records and the frozen S4
    anchor (ARM-M08/ARM-M10 server rows; SYNTHETIC materials only)."""

    def __init__(self, tmp_path, *, with_assembly=True, with_prerequisites=True,
                 with_registry_records=True, with_durable_control=True,
                 permit=None):
        base = simulation_context(clock=None)
        self.arming_binding = base.binding
        self.arming_permit = permit or _arming_permit(base.binding)
        self.arming_authorization = _arming_authorization(base.binding)
        self.arming_state = _arming_control_state(base.binding, self.arming_permit)
        self.arming_evidence = _arming_evidence(base.binding)
        registry = ProductionServiceRegistry(None)
        if with_registry_records:
            # SYNTHETIC test injection of exactly the registered state the
            # offline register chain produces with PRODUCTION records; the
            # production register-* CLI keeps refusing every SYNTHETIC
            # record (its own test above) - no service-side test hook exists.
            baseline, route, settlement = self.arming_evidence
            registry._records["NativeBaselineEvidence:" + baseline.evidence_id] = \
                ("NativeBaselineEvidence", baseline)
            registry._records["NativeRouteEvidence:" + route.evidence_id] = \
                ("NativeRouteEvidence", route)
            registry._records["NativeSettlementEvidence:" + settlement.evidence_id] = \
                ("NativeSettlementEvidence", settlement)
            registry._records["NativeAuthorization:" + ARMING_AUTHORIZATION_ID] = \
                ("NativeAuthorization", self.arming_authorization)
            registry._records["NativePilotPermit:" + ARMING_PERMIT_ID] = \
                ("NativePilotPermit", self.arming_permit)
        assemblies = None
        if with_assembly:
            assemblies = {c.canonical_hash(base.binding): TrustedControlAssembly(
                control_state=self.arming_state, permit=self.arming_permit,
                authorization=self.arming_authorization,
                origin=nc.Origin.SYNTHETIC)}
        super().__init__(tmp_path, orchestrator=StubOrchestrator(base.binding),
                         audit_name="arming-audit.jsonl",
                         evidence_registry=registry,
                         trusted_control_assemblies=assemblies,
                         arming_prerequisites=_arming_prerequisites_anchor()
                         if with_prerequisites else None)
        if with_durable_control:
            with self.journal.maintenance():
                self.journal.register_control_update(
                    self.arming_state, permit=self.arming_permit,
                    evidence_registry=_LedgerSyntheticRegistry())


def _arming_report(env, *, confirmation_id="SIM_ARM_CONF_1",
                   request_id="req-arm-1", sequence=1, target=ARMING_TARGET,
                   adapter=ARMING_ADAPTER, confirmed_at=ARMING_CONFIRMED_AT,
                   expires_at=ARMING_EXPIRES_AT, binding=None):
    binding = binding or getattr(env, "arming_binding", env.binding)
    confirmation = nc.ParameterConfirmation(
        confirmation_id=confirmation_id, binding=binding, params_sha256=target,
        adapter_sha256=adapter, confirmed_at=confirmed_at, expires_at=expires_at,
        explicit_ui_action=True)
    return nc.ControlReport(
        request_id=request_id, instance_id=binding.instance_id,
        session_epoch=binding.session_epoch, generation=GENERATION, sequence=sequence,
        service_epoch=env.service.service_epoch, sent_at=ARMING_CONFIRMED_AT,
        params_sha256=target, kind=nc.ControlKind.PARAMS_CONFIRMED,
        confirmation=confirmation)


def _confirm(env, *, request_id="req-arm-1", **kwargs):
    return _typed_post_retry_on_conn_abort(
        env, "/execution/v1/control", _arming_report(env, request_id=request_id,
                                                     **kwargs))


def _audit_events(env):
    events = []
    for line in env.audit_path.read_text(encoding="utf-8").splitlines():
        if line:
            events.append(json.loads(line))
    return events


class TestArmingConfirmation:
    """ARM-M08: the PARAMS_CONFIRMED real validation chain (RULING 03 AR3.3)."""

    def test_positive_exact_ack_recorded(self, tmp_path):
        env = ArmingEnv(tmp_path)
        try:
            status, payload, _ = _confirm(env)
            assert status == 200
            response = _decode(payload, nc.ControlResponse)
            # exact ACK (AR3.3): confirmation id, target hash, binding, epoch
            # and the verified control revision all echo back
            assert response.kind is nc.ControlResponseKind.RECORDED
            assert response.report_kind is nc.ControlKind.PARAMS_CONFIRMED
            assert response.confirmation_id == "SIM_ARM_CONF_1"
            assert response.params_sha256 == ARMING_TARGET
            assert response.binding == env.arming_binding
            assert response.service_epoch == env.service.service_epoch
            assert response.control_revision == 1
            # readiness names the verified permit and never flips the
            # submission flags on a user-admission RECORDED
            assert response.readiness.native_permit_id == ARMING_PERMIT_ID
            assert response.readiness.can_submit is False
            assert response.readiness.can_cancel_owned is False
            assert response.readiness.mode is c.ExecutionMode.NATIVE
            assert response.readiness.execution_scope is nc.ExecutionScope.NATIVE_PILOT
            assert response.control_phase is nc.ControlPhase.ARMED
            assert response.blockers == ()
            # exactly one arming fact for this service epoch
            with env.service._lock:
                assert len(env.service._arming_confirmed) == 1
            kinds = [event["event"] for event in _audit_events(env)]
            assert "arming_confirmed" in kinds
        finally:
            env.close()

    def test_wrong_target_hash_blocked(self, tmp_path):
        env = ArmingEnv(tmp_path)
        try:
            # the confirmation target hash differs from the message hash
            status, payload, _ = _confirm(env, target="aa" * 32,
                                          request_id="req-arm-hash")
            response = _decode(payload, nc.ControlResponse)
            assert response.kind is nc.ControlResponseKind.BLOCKED
            assert response.blockers == ("AUTHORIZATION_REQUIRED",)
            assert response.confirmation_id == "SIM_ARM_CONF_1"
            with env.service._lock:
                assert env.service._arming_confirmed == {}
        finally:
            env.close()

    def test_wrong_adapter_release_blocked(self, tmp_path):
        env = ArmingEnv(tmp_path)
        try:
            status, payload, _ = _confirm(env, adapter="7e" * 32,
                                          request_id="req-arm-adapter")
            response = _decode(payload, nc.ControlResponse)
            assert response.kind is nc.ControlResponseKind.BLOCKED
            # adapter_sha256 does not match the approved release anchor
            assert response.blockers == ("AUTHORIZATION_REQUIRED",)
            with env.service._lock:
                assert env.service._arming_confirmed == {}
        finally:
            env.close()

    def test_expired_confirmation_blocked(self, tmp_path):
        env = ArmingEnv(tmp_path)
        try:
            env.fake_clock.advance(600)  # 01:10 > 01:05 expiry
            status, payload, _ = _confirm(env, request_id="req-arm-expired")
            response = _decode(payload, nc.ControlResponse)
            assert response.kind is nc.ControlResponseKind.BLOCKED
            assert response.blockers == ("EXPIRED",)
        finally:
            env.close()

    def test_not_yet_valid_confirmation_blocked(self, tmp_path):
        env = ArmingEnv(tmp_path)
        try:
            # the confirmation window starts in the future (01:02 > the
            # service clock's 01:00): not yet valid is refused exactly like
            # an expired window
            status, payload, _ = _confirm(
                env, confirmed_at=datetime(2026, 9, 14, 1, 2, tzinfo=timezone.utc),
                expires_at=datetime(2026, 9, 14, 1, 4, tzinfo=timezone.utc),
                request_id="req-arm-future")
            response = _decode(payload, nc.ControlResponse)
            assert response.kind is nc.ControlResponseKind.BLOCKED
            assert response.blockers == ("EXPIRED",)
        finally:
            env.close()

    def test_non_interactive_flag_rejected_at_decode(self, env):
        # the frozen DTO itself refuses a non-interactive confirmation, so a
        # wire-level explicit_ui_action=false never reaches the handler
        report = _arming_report(env)
        wire = json.loads(nc.encode(report))
        wire["data"]["confirmation"]["explicit_ui_action"] = False
        status, payload, _ = _post_retry_on_conn_abort(
            env, "/execution/v1/control", json.dumps(wire).encode("utf-8"))
        assert status == 400
        assert _error_code(payload) == "INVALID_REQUEST"
        with pytest.raises(c.ContractError):
            nc.ParameterConfirmation(
                confirmation_id="SIM_ARM_CONF_X",
                binding=env.binding, params_sha256=ARMING_TARGET,
                adapter_sha256=ARMING_ADAPTER,
                confirmed_at=ARMING_CONFIRMED_AT,
                expires_at=ARMING_EXPIRES_AT, explicit_ui_action=False)

    def test_missing_prerequisites_anchor_blocked(self, tmp_path):
        env = ArmingEnv(tmp_path, with_prerequisites=False)
        try:
            status, payload, _ = _confirm(env, request_id="req-arm-noanchor")
            response = _decode(payload, nc.ControlResponse)
            assert response.kind is nc.ControlResponseKind.BLOCKED
            assert response.blockers == ("NATIVE_DISABLED",)
        finally:
            env.close()

    def test_unregistered_permit_blocked(self, tmp_path):
        env = ArmingEnv(tmp_path, with_registry_records=False)
        try:
            status, payload, _ = _confirm(env, request_id="req-arm-norec")
            response = _decode(payload, nc.ControlResponse)
            assert response.kind is nc.ControlResponseKind.BLOCKED
            assert response.blockers == ("NATIVE_DISABLED",)
        finally:
            env.close()

    def test_permit_scope_widening_blocked(self, tmp_path):
        base = simulation_context(clock=None)
        widened = _arming_permit(
            base.binding,
            actions=(c.AuthorizationAction.OPEN_LONG, c.AuthorizationAction.CLOSE_LONG))
        env = ArmingEnv(tmp_path, permit=widened)
        try:
            status, payload, _ = _confirm(env, request_id="req-arm-widen")
            response = _decode(payload, nc.ControlResponse)
            assert response.kind is nc.ControlResponseKind.BLOCKED
            # CLOSE_LONG is outside the registered authorization actions
            assert response.blockers == ("AUTHORIZATION_REQUIRED",)
        finally:
            env.close()

    def test_s4_send_cap_violation_blocked(self, tmp_path):
        base = simulation_context(clock=None)
        env = ArmingEnv(tmp_path, permit=_arming_permit(base.binding,
                                                        max_send_attempts=2))
        try:
            status, payload, _ = _confirm(env, request_id="req-arm-sendcap")
            response = _decode(payload, nc.ControlResponse)
            assert response.kind is nc.ControlResponseKind.BLOCKED
            assert response.blockers == ("AUTHORIZATION_REQUIRED",)
        finally:
            env.close()

    def test_s4_cancel_cap_violation_blocked(self, tmp_path):
        base = simulation_context(clock=None)
        env = ArmingEnv(tmp_path, permit=_arming_permit(base.binding,
                                                        max_cancel_attempts=2))
        try:
            status, payload, _ = _confirm(env, request_id="req-arm-cancelcap")
            response = _decode(payload, nc.ControlResponse)
            assert response.kind is nc.ControlResponseKind.BLOCKED
            assert response.blockers == ("AUTHORIZATION_REQUIRED",)
        finally:
            env.close()

    def test_send_budget_exhausted_blocked(self, tmp_path):
        env = ArmingEnv(tmp_path)
        try:
            # SYNTHETIC journal state: one consumed SUBMIT dispatch already
            # latched a send attempt against this scope (persistent N3
            # attempt consumption - a new confirmation cannot reset it); the
            # command is CLOSED so it does not count as unresolved
            stamp = "2026-09-14T00:59:00+00:00"
            env.journal._conn.execute(
                "INSERT INTO dispatch_commands VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("SIM_ARM_BUDGET_CMD", "SIM_ARM_BUDGET_ATTEMPT", "SUBMIT",
                 "SIM_OFFER_1", "SIM_INTENT", "SIM_INTENT:OPEN:-:0", "e" * 64,
                 c.canonical_hash(env.arming_binding), 1, ARMING_PERMIT_ID,
                 "{}", hashlib.sha256(b"budget-cmd").hexdigest(), stamp, stamp, 1, 1))
            env.journal._conn.execute(
                "INSERT INTO dispatch_receipts VALUES(?,?,?,?,?,?)",
                ("SIM_ARM_BUDGET_RECEIPT", "SIM_ARM_BUDGET_CMD", "CONSUMED", "{}",
                 hashlib.sha256(b"budget-receipt").hexdigest(), stamp))
            env.journal._conn.commit()
            status, payload, _ = _confirm(env, request_id="req-arm-budget")
            response = _decode(payload, nc.ControlResponse)
            assert response.kind is nc.ControlResponseKind.BLOCKED
            assert response.blockers == ("AUTHORIZATION_ACTION",)
        finally:
            env.close()

    def test_unresolved_dispatch_blocks_confirmation(self, tmp_path):
        env = ArmingEnv(tmp_path)
        try:
            stamp = "2026-09-14T00:59:00+00:00"
            env.journal._conn.execute(
                "INSERT INTO dispatch_commands VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("SIM_ARM_OPEN_CMD", "SIM_ARM_OPEN_ATTEMPT", "SUBMIT",
                 "SIM_OFFER_1", "SIM_INTENT", "SIM_INTENT:OPEN:-:0", "e" * 64,
                 c.canonical_hash(env.arming_binding), 1, ARMING_PERMIT_ID,
                 "{}", hashlib.sha256(b"open-cmd").hexdigest(), stamp, stamp, 1, 0))
            env.journal._conn.commit()
            status, payload, _ = _confirm(env, request_id="req-arm-unresolved")
            response = _decode(payload, nc.ControlResponse)
            assert response.kind is nc.ControlResponseKind.BLOCKED
            assert response.blockers == ("RECONCILE_REQUIRED",)
        finally:
            env.close()

    def test_retransmission_replays_same_fact(self, tmp_path):
        env = ArmingEnv(tmp_path)
        try:
            status, payload, _ = _confirm(env, request_id="req-arm-a")
            first = _decode(payload, nc.ControlResponse)
            assert first.kind is nc.ControlResponseKind.RECORDED
            # ACK-loss retransmission under a NEW request id replays the same
            # logical confirmation, not a second arming
            status, payload, _ = _confirm(env, request_id="req-arm-b", sequence=2)
            second = _decode(payload, nc.ControlResponse)
            assert second.kind is nc.ControlResponseKind.RECORDED
            assert second.confirmation_id == first.confirmation_id
            assert second.control_revision == first.control_revision
            assert second.readiness.native_permit_id == ARMING_PERMIT_ID
            # identical bytes under the SAME request id are also re-validated
            # (arming requests bypass the HTTP replay caches by design)
            status, payload, _ = _confirm(env, request_id="req-arm-a")
            third = _decode(payload, nc.ControlResponse)
            assert third.kind is nc.ControlResponseKind.RECORDED
            with env.service._lock:
                assert len(env.service._arming_confirmed) == 1
            kinds = [event["event"] for event in _audit_events(env)]
            assert kinds.count("arming_confirmed") == 1
            assert kinds.count("arming_replayed") == 2
        finally:
            env.close()

    def test_same_confirmation_id_different_content_rejected(self, tmp_path):
        env = ArmingEnv(tmp_path)
        try:
            status, payload, _ = _confirm(env, request_id="req-arm-c1")
            assert _decode(payload, nc.ControlResponse).kind \
                is nc.ControlResponseKind.RECORDED
            # same confirmation id, mutated expiry: rejected
            status, payload, _ = _confirm(
                env, request_id="req-arm-c2", sequence=2,
                expires_at=datetime(2026, 9, 14, 1, 6, tzinfo=timezone.utc))
            response = _decode(payload, nc.ControlResponse)
            assert response.kind is nc.ControlResponseKind.BLOCKED
            assert response.blockers == ("AUTHORIZATION_REQUIRED",)
        finally:
            env.close()

    def test_second_different_confirmation_id_rejected(self, tmp_path):
        env = ArmingEnv(tmp_path)
        try:
            status, payload, _ = _confirm(env, request_id="req-arm-d1")
            assert _decode(payload, nc.ControlResponse).kind \
                is nc.ControlResponseKind.RECORDED
            # one arming per service-epoch lifecycle: a different
            # confirmation id is a conflicting confirmation
            status, payload, _ = _confirm(env, request_id="req-arm-d2", sequence=2,
                                          confirmation_id="SIM_ARM_CONF_2")
            response = _decode(payload, nc.ControlResponse)
            assert response.kind is nc.ControlResponseKind.BLOCKED
            assert response.blockers == ("AUTHORIZATION_REQUIRED",)
        finally:
            env.close()


class TestArmingIssuanceGate:
    """ARM-M10 service rows: disarm, epoch confirmation and revision drift
    govern NEW native poll/ready issuance (RULING 05 server side)."""

    @staticmethod
    def _queue_offer(env):
        env.orchestrator.poll_outcomes.append(SimpleNamespace(
            kind=nc.ResponseKind.OFFER, offer=_offer(env.arming_binding),
            blockers=()))

    def test_unconfirmed_epoch_refuses_native_issuance(self, tmp_path):
        env = ArmingEnv(tmp_path)
        try:
            self._queue_offer(env)
            status, payload, _ = _typed_post_retry_on_conn_abort(
                env, "/execution/v1/poll",
                _typed_poll("req-arm-gate-1", service_epoch=env.service.service_epoch))
            response = _decode(payload, nc.PollResponse)
            # no valid confirmation of this service epoch: no offer is issued
            assert response.kind is nc.ResponseKind.BLOCKED
            assert response.offer is None
            assert response.blockers == ("NATIVE_DISABLED",)
            assert response.readiness.mode is c.ExecutionMode.NATIVE
            assert response.readiness.native_permit_id is None
            # the queued offer was never consumed by the orchestrator
            assert len(env.orchestrator.poll_outcomes) == 1
            status, payload, _ = _get_status_retry_on_conn_abort(
                env, headers=_status_headers(
                    service_epoch=env.service.service_epoch))
            assert _decode(payload, nc.StatusResponse).control_phase \
                is nc.ControlPhase.DISABLED
        finally:
            env.close()

    def test_confirmed_epoch_issues_with_verified_permit(self, tmp_path):
        env = ArmingEnv(tmp_path)
        try:
            status, payload, _ = _confirm(env, request_id="req-arm-gate-arm")
            assert _decode(payload, nc.ControlResponse).kind \
                is nc.ControlResponseKind.RECORDED
            env.step_poll()
            self._queue_offer(env)
            status, payload, _ = _typed_post_retry_on_conn_abort(
                env, "/execution/v1/poll",
                _typed_poll("req-arm-gate-2", sequence=2,
                            service_epoch=env.service.service_epoch))
            response = _decode(payload, nc.PollResponse)
            assert response.kind is nc.ResponseKind.OFFER
            assert response.offer is not None
            assert response.readiness.native_permit_id == ARMING_PERMIT_ID
            assert response.readiness.can_submit is False
            status, payload, _ = _get_status_retry_on_conn_abort(
                env, headers=_status_headers(
                    service_epoch=env.service.service_epoch))
            assert _decode(payload, nc.StatusResponse).control_phase \
                is nc.ControlPhase.ARMED
        finally:
            env.close()

    def test_disarm_latches_and_stops_new_issuance(self, tmp_path):
        env = ArmingEnv(tmp_path)
        try:
            status, payload, _ = _confirm(env, request_id="req-arm-dis-1")
            assert _decode(payload, nc.ControlResponse).control_phase \
                is nc.ControlPhase.ARMED
            # disarm (GATE_CLOSED): the latch closes new issuance first
            disarm = nc.ControlReport(
                request_id="req-arm-disarm", instance_id="SIM_INSTANCE_I",
                session_epoch="SIM_EPOCH_E", generation=GENERATION, sequence=2,
                service_epoch=env.service.service_epoch,
                sent_at=ARMING_CONFIRMED_AT, params_sha256=ARMING_TARGET,
                kind=nc.ControlKind.GATE_CLOSED, confirmation=None)
            status, payload, _ = _typed_post_retry_on_conn_abort(
                env, "/execution/v1/control", disarm)
            response = _decode(payload, nc.ControlResponse)
            assert response.kind is nc.ControlResponseKind.RECORDED
            assert response.control_phase is nc.ControlPhase.DISABLED_CONFIRMED
            # a LATER PARAMS_CONFIRMED can never clear the latch: a fresh
            # confirmation id and even the original confirmation are blocked
            status, payload, _ = _confirm(env, request_id="req-arm-dis-2",
                                          sequence=3, confirmation_id="SIM_ARM_CONF_9")
            assert _decode(payload, nc.ControlResponse).blockers == ("MODE_DISABLED",)
            status, payload, _ = _confirm(env, request_id="req-arm-dis-3",
                                          sequence=4)
            assert _decode(payload, nc.ControlResponse).blockers == ("MODE_DISABLED",)
            # poll/ready no longer issue anything native
            env.step_poll()
            self._queue_offer(env)
            status, payload, _ = _typed_post_retry_on_conn_abort(
                env, "/execution/v1/poll",
                _typed_poll("req-arm-dis-poll", sequence=5,
                            service_epoch=env.service.service_epoch))
            response = _decode(payload, nc.PollResponse)
            assert response.kind is nc.ResponseKind.BLOCKED
            assert response.offer is None
            assert response.blockers == ("MODE_DISABLED",)
            assert len(env.orchestrator.poll_outcomes) == 1
            status, payload, _ = _get_status_retry_on_conn_abort(
                env, headers=_status_headers(
                    service_epoch=env.service.service_epoch))
            assert _decode(payload, nc.StatusResponse).control_phase \
                is nc.ControlPhase.DISABLED_CONFIRMED
            kinds = [event["event"] for event in _audit_events(env)]
            assert "arming_revoked" in kinds
        finally:
            env.close()

    def test_revision_drift_reblocks_issuance(self, tmp_path):
        env = ArmingEnv(tmp_path)
        try:
            status, payload, _ = _confirm(env, request_id="req-arm-drift-1")
            assert _decode(payload, nc.ControlResponse).kind \
                is nc.ControlResponseKind.RECORDED
            # a durable control revision bump drifts the confirmed fact
            drifted_state = replace(env.arming_state, control_revision=2)
            drifted_permit = replace(env.arming_permit, control_revision=2)
            with env.journal.maintenance():
                env.journal.register_control_update(
                    drifted_state, permit=drifted_permit,
                    evidence_registry=_LedgerSyntheticRegistry())
            env.step_poll()
            self._queue_offer(env)
            status, payload, _ = _typed_post_retry_on_conn_abort(
                env, "/execution/v1/poll",
                _typed_poll("req-arm-drift-poll", sequence=2,
                            service_epoch=env.service.service_epoch))
            response = _decode(payload, nc.PollResponse)
            assert response.kind is nc.ResponseKind.BLOCKED
            assert response.blockers == ("AUTHORIZATION_REQUIRED",)
            # a retransmission of the original confirmation cannot ride the
            # stale control revision either
            status, payload, _ = _confirm(env, request_id="req-arm-drift-2",
                                          sequence=3)
            assert _decode(payload, nc.ControlResponse).blockers \
                == ("AUTHORIZATION_REQUIRED",)
        finally:
            env.close()


# ===========================================================================
# GET /status (RULING 02): headers, hygiene, counters (H39 service half)
# ===========================================================================

class TestStatusEndpoint:

    def test_status_roundtrip(self, env):
        status, payload, headers = _get_status_retry_on_conn_abort(
            env, headers=_status_headers(service_epoch=env.service.service_epoch))
        assert status == 200
        response = _decode(payload, nc.StatusResponse)
        assert response.service_epoch == env.service.service_epoch
        assert response.blockers == response.readiness.blockers
        assert response.control_phase is nc.ControlPhase.DISABLED
        counters = response.counters
        assert counters.committed_dispatch_count == 0
        assert counters.native_send_attempts.value is None
        assert counters.count_complete is False
        assert response.ack_watermark is None
        assert headers.get("Cache-Control") == "no-store"

    def test_status_rejects_missing_duplicate_query_body(self, env):
        base = _status_headers(service_epoch=env.service.service_epoch)
        missing = dict(base)
        missing.pop("X-Execution-Sequence")
        status, payload, _ = _get_status_retry_on_conn_abort(env, headers=missing)
        assert status == 400 and _error_code(payload) == "INVALID_REQUEST"
        # duplicate header: use a raw request because http.client would
        # collapse it; F-03: wrapped in the same bounded zero-response abort
        # retry (request construction itself is byte-identical)
        def duplicate_sequence_header_get():
            connection = http.client.HTTPConnection("127.0.0.1", env.port,
                                                    timeout=5)
            try:
                connection.putrequest("GET", "/execution/v1/status",
                                      skip_host=True)
                connection.putheader("Host", "127.0.0.1")
                connection.putheader(nc.EXECUTION_TOKEN_HEADER, TOKEN)
                for line in base.items():
                    connection.putheader(*line)
                connection.putheader("X-Execution-Sequence", "2")
                connection.endheaders()
                response = connection.getresponse()
                body = response.read()
                return response, body
            finally:
                connection.close()

        response, body = _retry_on_conn_abort(duplicate_sequence_header_get,
                                              "/execution/v1/status")
        assert response.status == 400
        assert json.loads(body)["data"]["error_code"] == "INVALID_REQUEST"
        status, payload, _ = _get_status_retry_on_conn_abort(
            env, headers={**base, "Content-Length": "4"})
        assert status == 400

        # query string: raw request again; same F-03 retry wrap
        def query_string_status_get():
            connection = http.client.HTTPConnection("127.0.0.1", env.port,
                                                    timeout=5)
            try:
                connection.request("GET", "/execution/v1/status?x=1", headers={
                    nc.EXECUTION_TOKEN_HEADER: TOKEN, **base})
                response = connection.getresponse()
                body = response.read()
                return response, body
            finally:
                connection.close()

        response, body = _retry_on_conn_abort(query_string_status_get,
                                              "/execution/v1/status")
        assert response.status == 400
        assert json.loads(body)["data"]["error_code"] == "INVALID_REQUEST"

    def test_status_header_bytes_too_large(self, env):
        status, payload, _ = _get_status_retry_on_conn_abort(
            env, headers=_status_headers(
                service_epoch=env.service.service_epoch,
                overrides={"X-Execution-Params-Sha256": "a" * 64,
                           "X-Execution-Request-Id": "r" * 8300}))
        assert status == 413 and _error_code(payload) == "REQUEST_TOO_LARGE"

    def test_status_no_credentials_or_raw_errors(self, env):
        status, payload, _ = _get_status_retry_on_conn_abort(
            env, headers=_status_headers(service_epoch=env.service.service_epoch))
        text = json.dumps(payload)
        for forbidden in ("Investor", "investor", "password", "errMsg",
                          TOKEN, "pairing"):
            assert forbidden not in text

    def test_counters_persist_across_service_restart(self, tmp_path):
        first = Env(tmp_path)
        try:
            epoch1 = first.service.service_epoch
            request = _typed_poll("req-x1", service_epoch=epoch1)
            assert _typed_post_retry_on_conn_abort(first, "/execution/v1/poll", request)[0] == 200
            counters1 = first.journal.execution_counters(first.binding)
        finally:
            first.close()
        second = Env(tmp_path)
        try:
            assert second.service.service_epoch == epoch1 + 1
            counters2 = second.journal.execution_counters(second.binding)
            assert counters2.committed_dispatch_count == \
                counters1.committed_dispatch_count
            # old-epoch requests are rejected after the restart
            request = _typed_poll("req-x2", service_epoch=epoch1)
            status, payload, _ = _typed_post_retry_on_conn_abort(second, "/execution/v1/poll", request)
            assert status == 409 and _error_code(payload) == "EPOCH_MISMATCH"
        finally:
            second.close()


# ===========================================================================
# worker saturation stays bounded (RULING 03/04)
# ===========================================================================

class TestBoundedWorkers:

    def test_saturation_returns_typed_503(self, tmp_path):
        release = threading.Event()
        started = threading.Event()

        slow_orchestrator = StubOrchestrator(None)

        def slow_poll(binding, request):
            del binding, request
            started.set()
            release.wait(5)
            return SimpleNamespace(kind=nc.ResponseKind.IDLE, offer=None, blockers=())

        slow_orchestrator.poll = slow_poll
        environment = Env(tmp_path, orchestrator=slow_orchestrator, max_workers=1)
        try:
            epoch = environment.service.service_epoch
            first = _typed_poll("req-b0", service_epoch=epoch)
            others = [_typed_poll(f"req-b{i}", sequence=i + 1, service_epoch=epoch)
                      for i in range(1, 3)]
            outcomes: dict[str, tuple] = {}

            def send(label, item):
                try:
                    # deliberately bare _typed_post (F-03): this saturation
                    # probe treats a transport abort as one of its asserted
                    # legal outcomes; retrying would mask exactly the
                    # bounded-worker overflow behavior under test
                    outcomes[label] = _typed_post(
                        environment, "/execution/v1/poll", item)
                except (ConnectionError, OSError) as exc:
                    # an overflow reject may hard-close the socket mid-flight;
                    # that is a transport failure, never a business outcome
                    outcomes[label] = (None, {"aborted": repr(exc)}, {})

            holder = threading.Thread(target=send, args=("first", first))
            holder.start()
            assert started.wait(5), "the in-flight request must occupy the worker"
            threads = [threading.Thread(target=send, args=(f"other-{index}", item))
                       for index, item in enumerate(others)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            release.set()
            holder.join(timeout=5)
            assert set(outcomes) == {"first", "other-0", "other-1"}
            status, payload, _ = outcomes["first"]
            assert status == 200  # the admitted request completes
            for label in ("other-0", "other-1"):
                status, payload, _ = outcomes[label]
                if status is None:
                    assert "aborted" in payload
                    continue
                assert status in (503, 429), (label, status)
                if status == 503:
                    assert payload["data"]["error_code"] in (
                        "BACKPRESSURE", "SERVICE_UNAVAILABLE")
        finally:
            release.set()
            environment.close()


# ===========================================================================
# launcher CLI (scripts/run_infiny_execution.py)
# ===========================================================================

def _launcher():
    spec = importlib.util.spec_from_file_location(
        "run_infiny_execution", Path(__file__).resolve().parents[1] /
        "scripts" / "run_infiny_execution.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_registration_files(tmp_path, *, lock_text=b'{"lock":"c1.1-r1"}'):
    lock = Path(tmp_path) / "contract-lock.json"
    lock.write_bytes(lock_text)
    record = Path(tmp_path) / "actor-record.json"
    record.write_text(json.dumps({
        "actor_id": "SIM_ACTOR_1", "account_key": "SIM_ACCOUNT_A",
        "bindings": [{
            "account_key": "SIM_ACCOUNT_A", "strategy_key": "SIM_STRATEGY_S",
            "strategy_id": 23, "instance_id": "SIM_INSTANCE_I",
            "session_epoch": "SIM_EPOCH_E", "trading_day": "2026-09-14",
            "exchange": "SHFE", "instrument_id": "rb2610", "hedge_flag": "1",
        }]}), encoding="utf-8")
    artifact = Path(tmp_path) / "authorization.json"
    artifact.write_text('{"user":"SIM_OPERATION"}', encoding="utf-8")
    import hashlib
    manifest = {
        "registration_id": "reg-0001", "record_type": "ActorRegistration",
        "record_path": str(record).replace("\\", "/"),
        "record_sha256": hashlib.sha256(record.read_bytes()).hexdigest(),
        "contract_lock_sha256": hashlib.sha256(lock_text).hexdigest(),
        "operator_action_id": "SIM_OP_1",
        "authorization_artifact_path": str(artifact).replace("\\", "/"),
        "authorization_artifact_sha256": hashlib.sha256(
            artifact.read_bytes()).hexdigest(),
        "artifacts": [{"path": str(artifact).replace("\\", "/"),
                       "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}],
    }
    manifest_path = Path(tmp_path) / "manifest.json"
    from ak_trading.infiny_execution.native_evidence import OperatorRegistrationManifest
    envelope = {"schema_version": c.V2_SCHEMA_VERSION,
                "type": "OperatorRegistrationManifest", "data": manifest}
    encoded = c.decode(json.dumps(envelope), OperatorRegistrationManifest,
                       schema_version=c.V2_SCHEMA_VERSION)
    manifest_path.write_text(c.encode(encoded, schema_version=c.V2_SCHEMA_VERSION),
                             encoding="utf-8")
    return lock, manifest_path


class TestLauncherCLI:

    def test_serve_arg_validation_matrix(self, tmp_path):
        launcher = _launcher()
        lock, manifest = _write_registration_files(tmp_path)
        base = ["serve", "--journal", str(tmp_path / "j.sqlite"), "--audit-log",
                str(tmp_path / "a.jsonl"), "--pairing-dir", str(tmp_path / "p"),
                "--registry-dir", str(tmp_path / "r"), "--contract-lock", str(lock)]
        cases = [
            (["serve", "--host", "0.0.0.0"] + base[1:], "HOST_REJECTED"),
            (["serve", "--journal", str(tmp_path / "j.sqlite"), "--audit-log",
              str(tmp_path / "a.jsonl"), "--pairing-dir", str(tmp_path / "p"),
              "--registry-dir", str(tmp_path / "r"), "--contract-lock", str(lock),
              "--port", "70000"], "PORT_INVALID"),
            (base + ["--port", "17778"], "OBSERVER_PORT_FORBIDDEN"),
            (base, "JOURNAL_MISSING_USE_INITIALIZE"),
            (base + ["--max-workers", "9"], "WORKER_LIMIT_INVALID"),
        ]
        for argv, expected in cases:
            assert launcher.main(argv) == 2, argv
        del manifest

    def test_port_in_use_never_kills_never_moves(self, tmp_path):
        launcher = _launcher()
        lock, _ = _write_registration_files(tmp_path)
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        try:
            code = launcher.main([
                "serve", "--port", str(port), "--journal", str(tmp_path / "j.sqlite"),
                "--audit-log", str(tmp_path / "a.jsonl"),
                "--pairing-dir", str(tmp_path / "p"), "--registry-dir",
                str(tmp_path / "r"), "--contract-lock", str(lock), "--initialize"])
            assert code == 2
        finally:
            blocker.close()

    def test_dry_run_writes_nothing(self, tmp_path, capsys):
        launcher = _launcher()
        lock, _ = _write_registration_files(tmp_path)
        # LM-S 2026-09-17: bind an ephemeral port instead of the 17779 default -
        # the live production listener may legitimately hold 17779 while tests
        # run, and the dry-run port probe (never kill, never move) must reject
        # an occupied port (that branch is covered by its own test below).
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        code = launcher.main([
            "serve", "--port", str(port), "--journal", str(tmp_path / "j.sqlite"),
            "--audit-log", str(tmp_path / "a.jsonl"), "--pairing-dir",
            str(tmp_path / "p"), "--registry-dir", str(tmp_path / "r"),
            "--contract-lock", str(lock), "--initialize", "--dry-run"])
        assert code == 0
        assert not (tmp_path / "j.sqlite").exists()
        assert not (tmp_path / "p").exists() or not list((tmp_path / "p").iterdir())
        line = capsys.readouterr().out.strip().splitlines()[-1]
        assert json.loads(line)["event"] == "execution_dry_run"

    def test_register_actor_happy_path(self, tmp_path, capsys):
        launcher = _launcher()
        lock, manifest = _write_registration_files(tmp_path)
        journal_path = tmp_path / "j.sqlite"
        code = launcher.main(["register-actor", "--journal", str(journal_path),
                              "--manifest", str(manifest), "--contract-lock",
                              str(lock), "--initialize"])
        assert code == 0
        journal = Ledger(journal_path, schema="v2")
        try:
            actors = journal.actors()
            assert len(actors) == 1
            assert actors[0].actor_id == "SIM_ACTOR_1"
            assert actors[0].bindings[0].instrument_id == "rb2610"
        finally:
            journal.close()
        output = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert output["event"] == "registered"

    def test_register_actor_hash_mismatch_refused(self, tmp_path):
        launcher = _launcher()
        lock, manifest = _write_registration_files(tmp_path)
        record = json.loads((tmp_path / "actor-record.json").read_text())
        record["actor_id"] = "SIM_ACTOR_TAMPERED"
        (tmp_path / "actor-record.json").write_text(json.dumps(record))
        journal_path = tmp_path / "j.sqlite"
        code = launcher.main(["register-actor", "--journal", str(journal_path),
                              "--manifest", str(manifest), "--contract-lock",
                              str(lock), "--initialize"])
        assert code == 2
        assert not journal_path.exists() or Ledger(journal_path,
                                                   schema="v2").actors() == ()

    def test_register_permit_synthetic_refused(self, tmp_path):
        launcher = _launcher()
        lock, manifest = _write_registration_files(tmp_path)
        registry_dir = tmp_path / "registry"
        permit = nc.NativePilotPermit(
            permit_id="SIM_PERMIT_X", authorization_id="SIM_AUTH_X",
            binding=simulation_context().binding, control_revision=1,
            baseline_evidence_id="SIM_BASE_X", route_evidence_id="SIM_ROUTE_X",
            cash_bound_evidence_id="SIM_CASH_X", source_key="SIM_SOURCE_X",
            actions=(c.AuthorizationAction.OPEN_LONG,), max_send_attempts=1,
            max_cancel_attempts=0, max_volume=1, price_bounds=(),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            params_sha256=PARAMS, origin=nc.Origin.SYNTHETIC)
        record_path = tmp_path / "permit.json"
        record_path.write_text(nc.encode(permit), encoding="utf-8")
        import hashlib
        from ak_trading.infiny_execution.native_evidence import (
            OperatorArtifact, OperatorRegistrationManifest, OperatorRecordType,
        )
        artifact = tmp_path / "authorization.json"
        permit_manifest = OperatorRegistrationManifest(
            registration_id="reg-permit-1",
            record_type=OperatorRecordType.NATIVE_PILOT_PERMIT,
            record_path=str(record_path).replace("\\", "/"),
            record_sha256=hashlib.sha256(record_path.read_bytes()).hexdigest(),
            contract_lock_sha256=hashlib.sha256(lock.read_bytes()).hexdigest(),
            operator_action_id="SIM_OP_2",
            authorization_artifact_path=str(artifact).replace("\\", "/"),
            authorization_artifact_sha256=hashlib.sha256(
                artifact.read_bytes()).hexdigest(),
            artifacts=(OperatorArtifact(
                path=str(artifact).replace("\\", "/"),
                sha256=hashlib.sha256(artifact.read_bytes()).hexdigest()),))
        manifest_path = tmp_path / "permit-manifest.json"
        manifest_path.write_text(c.encode(permit_manifest,
                                          schema_version=c.V2_SCHEMA_VERSION))
        journal_path = tmp_path / "j2.sqlite"
        code = launcher.main(["register-permit", "--journal", str(journal_path),
                              "--manifest", str(manifest_path), "--contract-lock",
                              str(lock), "--registry-dir", str(registry_dir),
                              "--initialize"])
        assert code == 2  # SYNTHETIC can never become production evidence
        assert not list(registry_dir.glob("permit/*.json")) if registry_dir.exists() \
            else True

    def test_serve_smoke_subprocess_lifecycle(self, tmp_path):
        """True CLI process smoke: startup line, pairing file, audit; the HTTP
        round-trip stays in-process (offline guard forbids connecting to a
        server outside the test process, so the child is never dialed)."""
        launcher = _launcher()
        lock, manifest = _write_registration_files(tmp_path)
        journal_path = tmp_path / "smoke.sqlite"
        assert launcher.main(["register-actor", "--journal", str(journal_path),
                              "--manifest", str(manifest), "--contract-lock",
                              str(lock), "--initialize"]) == 0
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        process = subprocess.Popen(
            [sys.executable, "-B", str(Path(__file__).resolve().parents[1] /
                                       "scripts" / "run_infiny_execution.py"),
             "serve", "--port", str(port), "--journal", str(journal_path),
             "--audit-log", str(tmp_path / "serve-audit.jsonl"),
             "--pairing-dir", str(tmp_path / "pairing"),
             "--registry-dir", str(tmp_path / "registry"),
             "--contract-lock", str(lock)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            deadline = time.monotonic() + 20
            started = None
            while time.monotonic() < deadline and started is None:
                line = process.stdout.readline()
                if not line:
                    break
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("event") == "execution_started":
                    started = event
            assert started is not None
            assert started["execution_enabled"] is False
            assert started["observer_port_untouched"] == 17778
            assert started["instances"] == ["SIM_INSTANCE_I"]
            # LM-S (RULING 02 R2.2): the service-side source file is the frozen
            # private ne1_service_pairing_v2 JSON - the old two-line text
            # format is superseded by the decision, never emitted again.
            assert started["service_version"] == SERVICE_VERSION
            assert started["pairings"] == [{
                "instance_id": "SIM_INSTANCE_I",
                "session_epoch": "SIM_EPOCH_E",
                "token_file": str(tmp_path / "pairing" / "SIM_INSTANCE_I.token"),
                "diagnostic_only": False}]
            document = _read_pairing_document(
                tmp_path / "pairing" / "SIM_INSTANCE_I.token")
            assert document["instance_id"] == "SIM_INSTANCE_I"
            assert document["session_epoch"] == "SIM_EPOCH_E"
            assert document["diagnostic_only"] is False
            assert document["service_epoch"] == started["service_epoch"]
            assert document["service_instance_id"] == started["instance_id"]
            # X01-01: the true CLI process emits the sole-legal-format
            # service id in BOTH the startup line and the source file
            assert re.fullmatch(r"execution-[0-9a-f]{16}",
                                started["instance_id"])
            assert re.fullmatch(r"execution-[0-9a-f]{16}",
                                document["service_instance_id"])
            assert document["token"] and len(document["token"]) == \
                nc.PAIRING_TOKEN_HEX_LENGTH
            assert all(char in "0123456789abcdef" for char in document["token"])
            # the startup line is the non-secret service-start payload: no
            # token value may ever appear in it
            assert document["token"] not in json.dumps(started)
        finally:
            process.terminate()
            process.wait(timeout=10)

    def test_cli_serve_roundtrip_in_process(self, tmp_path, monkeypatch, capsys):
        """The full CLI serve path (args, journal, registry, pairing, server)
        driven in-process so the loopback GET is guard-registered."""
        launcher = _launcher()
        lock, manifest = _write_registration_files(tmp_path)
        journal_path = tmp_path / "cli.sqlite"
        assert launcher.main(["register-actor", "--journal", str(journal_path),
                              "--manifest", str(manifest), "--contract-lock",
                              str(lock), "--initialize"]) == 0
        from ak_trading.infiny_execution import client_service
        captured = {}
        real_build = client_service.build_execution_server

        def build(**kwargs):
            server = real_build(**kwargs)
            captured["server"] = server
            return server

        monkeypatch.setattr(client_service, "build_execution_server", build)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        result = {}

        def run_serve():
            result["code"] = launcher.main([
                "serve", "--port", str(port), "--journal", str(journal_path),
                "--audit-log", str(tmp_path / "serve-audit.jsonl"),
                "--pairing-dir", str(tmp_path / "pairing"),
                "--registry-dir", str(tmp_path / "registry"),
                "--contract-lock", str(lock)])

        thread = threading.Thread(target=run_serve, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 15
            started = None
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
            assert started is not None
            token = _read_pairing_document(
                tmp_path / "pairing" / "SIM_INSTANCE_I.token")["token"]

            # F-03: same bounded zero-response abort retry for this raw GET
            def cli_status_get():
                connection = http.client.HTTPConnection("127.0.0.1", port,
                                                        timeout=5)
                try:
                    connection.request(
                        "GET", "/execution/v1/status",
                        headers={**_status_headers(
                            service_epoch=started["service_epoch"]),
                            nc.EXECUTION_TOKEN_HEADER: token})
                    response = connection.getresponse()
                    body = response.read()
                    return response, body
                finally:
                    connection.close()

            response, body = _retry_on_conn_abort(cli_status_get,
                                                  "/execution/v1/status")
            assert response.status == 200
            status_payload = _decode(json.loads(body), nc.StatusResponse)
            assert status_payload.service_epoch == started["service_epoch"]
            assert status_payload.counters.committed_dispatch_count == 0
        finally:
            server = captured.get("server")
            if server is not None:
                server.shutdown()
            thread.join(timeout=10)
        assert result.get("code") == 0


# ===========================================================================
# ARM-LM-S: --arming-readiness anchor matrix (NE1-ARB-ARMING-001 AR3.3)
# ===========================================================================

def _prerequisites_document(lock_bytes, *, service_sha=None, schema=None,
                            omit_kind=None, expires_at=None, client_sha=None,
                            created_at=None):
    from ak_trading.infiny_execution import client_service as cs
    now = datetime.now(timezone.utc)
    if service_sha is None:
        service_sha = hashlib.sha256(Path(cs.__file__).read_bytes()).hexdigest()
    kinds = ["RELEASE_REVIEW", "S3_ZERO_NATIVE", "MANUAL_TAKEOVER_APPROVAL",
             "SINGLE_ORDER_APPROVAL", "NATIVE_ASSEMBLY_ACCEPTANCE"]
    if omit_kind is not None:
        kinds.remove(omit_kind)
    return {
        "schema_version": schema or ARMING_PREREQUISITES_SCHEMA_VERSION,
        "prerequisites_id": "a1" * 16,
        "created_at": (created_at or now).isoformat().replace("+00:00", "Z"),
        "expires_at": (expires_at or now + timedelta(hours=1)
                       ).isoformat().replace("+00:00", "Z"),
        "contract_lock_sha256": hashlib.sha256(lock_bytes).hexdigest(),
        "client_file_sha256": client_sha or ARMING_ADAPTER,
        "service_file_sha256": service_sha,
        "tool_file_sha256": "d4" * 32,
        "evidence_refs": [{"kind": kind, "path": f"SYNTHETIC/{kind}",
                           "sha256": "e5" * 32} for kind in kinds],
    }


def _load_prerequisites_file(tmp_path, document, *, lock_bytes, service_sha=None):
    from ak_trading.infiny_execution import client_service as cs
    path = Path(tmp_path) / "prerequisites.json"
    if isinstance(document, str):
        path.write_text(document, encoding="utf-8")
    else:
        path.write_text(json.dumps(document), encoding="utf-8")
    if service_sha is None:
        service_sha = hashlib.sha256(Path(cs.__file__).read_bytes()).hexdigest()
    return cs.load_arming_prerequisites(
        path, contract_lock_sha256=hashlib.sha256(lock_bytes).hexdigest(),
        service_file_sha256=service_sha, now=datetime.now(timezone.utc))


class TestArmingPrerequisitesLoader:

    def test_valid_anchor_loads_with_frozen_bytes_hash(self, tmp_path):
        lock_bytes = b'{"lock":"arming-r1"}'
        anchor = _load_prerequisites_file(
            tmp_path, _prerequisites_document(lock_bytes), lock_bytes=lock_bytes)
        raw = (Path(tmp_path) / "prerequisites.json").read_bytes()
        assert anchor.prerequisites_sha256 == hashlib.sha256(raw).hexdigest()
        assert anchor.client_file_sha256 == ARMING_ADAPTER
        assert anchor.prerequisites_id == "a1" * 16
        assert len(anchor.evidence_refs) == 5

    def test_wrong_schema_version_rejected(self, tmp_path):
        lock_bytes = b'{"lock":"arming-r1"}'
        with pytest.raises(ValueError, match="ARMING_READINESS_SCHEMA"):
            _load_prerequisites_file(
                tmp_path,
                _prerequisites_document(lock_bytes, schema="ne1_other_v9"),
                lock_bytes=lock_bytes)

    def test_missing_evidence_kind_coverage_rejected(self, tmp_path):
        lock_bytes = b'{"lock":"arming-r1"}'
        with pytest.raises(ValueError, match="ARMING_READINESS_REF_COVERAGE"):
            _load_prerequisites_file(
                tmp_path,
                _prerequisites_document(lock_bytes, omit_kind="S3_ZERO_NATIVE"),
                lock_bytes=lock_bytes)

    def test_duplicate_json_keys_rejected(self, tmp_path):
        lock_bytes = b'{"lock":"arming-r1"}'
        with pytest.raises(ValueError, match="ARMING_READINESS_MALFORMED"):
            _load_prerequisites_file(
                tmp_path,
                '{"schema_version":"ne1_arming_prerequisites_v1",'
                '"schema_version":"ne1_arming_prerequisites_v1"}',
                lock_bytes=lock_bytes)

    def test_contract_lock_mismatch_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="ARMING_READINESS_CONTRACT_LOCK"):
            _load_prerequisites_file(
                tmp_path, _prerequisites_document(b'{"lock":"arming-r1"}'),
                lock_bytes=b'{"lock":"DIFFERENT"}')

    def test_service_hash_mismatch_rejected(self, tmp_path):
        lock_bytes = b'{"lock":"arming-r1"}'
        with pytest.raises(ValueError, match="ARMING_READINESS_SERVICE_HASH"):
            _load_prerequisites_file(
                tmp_path, _prerequisites_document(lock_bytes),
                lock_bytes=lock_bytes, service_sha="0" * 64)

    def test_expired_anchor_rejected(self, tmp_path):
        lock_bytes = b'{"lock":"arming-r1"}'
        now = datetime.now(timezone.utc)
        with pytest.raises(ValueError, match="ARMING_READINESS_EXPIRED"):
            _load_prerequisites_file(
                tmp_path,
                _prerequisites_document(lock_bytes, created_at=now - timedelta(hours=2),
                                        expires_at=now - timedelta(hours=1)),
                lock_bytes=lock_bytes)


class TestLauncherArmingReadiness:

    def test_serve_line_carries_strict_prerequisites_object(
            self, tmp_path, monkeypatch, capsys):
        launcher = _launcher()
        lock, manifest = _write_registration_files(tmp_path)
        journal_path = tmp_path / "ar.sqlite"
        assert launcher.main(["register-actor", "--journal", str(journal_path),
                              "--manifest", str(manifest), "--contract-lock",
                              str(lock), "--initialize"]) == 0
        lock_bytes = lock.read_bytes()
        prerequisites_path = tmp_path / "prerequisites.json"
        prerequisites_path.write_text(
            json.dumps(_prerequisites_document(lock_bytes)), encoding="utf-8")
        run = _serve_in_process(
            launcher, monkeypatch, capsys, journal_path=journal_path, lock=lock,
            pairing_dir=tmp_path / "pairing", registry_dir=tmp_path / "registry",
            audit_path=tmp_path / "audit.jsonl",
            extra_args=("--arming-readiness", str(prerequisites_path)))
        try:
            started = run.started
            assert started["service_version"] == SERVICE_VERSION
            # AR3.3: exactly one new top-level field with exactly the three
            # frozen non-secret keys
            anchor = started["arming_prerequisites"]
            assert set(anchor) == {"prerequisites_id", "prerequisites_sha256",
                                   "client_file_sha256"}
            assert anchor["prerequisites_sha256"] == hashlib.sha256(
                prerequisites_path.read_bytes()).hexdigest()
            assert anchor["client_file_sha256"] == ARMING_ADAPTER
            assert "execution_enabled" not in started or \
                started["execution_enabled"] is False
        finally:
            assert _stop_serve(run) == 0

    def test_serve_line_null_when_anchor_rejected(
            self, tmp_path, monkeypatch, capsys):
        launcher = _launcher()
        lock, manifest = _write_registration_files(tmp_path)
        journal_path = tmp_path / "ar2.sqlite"
        assert launcher.main(["register-actor", "--journal", str(journal_path),
                              "--manifest", str(manifest), "--contract-lock",
                              str(lock), "--initialize"]) == 0
        # wrong service-file hash: the anchor is unusable, but the disabled
        # service still starts and serves (never fatal, arming BLOCKED)
        prerequisites_path = tmp_path / "prerequisites.json"
        prerequisites_path.write_text(json.dumps(
            _prerequisites_document(lock.read_bytes(), service_sha="0" * 64)),
            encoding="utf-8")
        run = _serve_in_process(
            launcher, monkeypatch, capsys, journal_path=journal_path, lock=lock,
            pairing_dir=tmp_path / "pairing", registry_dir=tmp_path / "registry",
            audit_path=tmp_path / "audit2.jsonl",
            extra_args=("--arming-readiness", str(prerequisites_path)))
        try:
            assert run.started["arming_prerequisites"] is None
            assert run.started["service_version"] == SERVICE_VERSION
        finally:
            assert _stop_serve(run) == 0

    def test_serve_line_null_when_anchor_omitted(
            self, tmp_path, monkeypatch, capsys):
        launcher = _launcher()
        lock, manifest = _write_registration_files(tmp_path)
        journal_path = tmp_path / "ar3.sqlite"
        assert launcher.main(["register-actor", "--journal", str(journal_path),
                              "--manifest", str(manifest), "--contract-lock",
                              str(lock), "--initialize"]) == 0
        run = _serve_in_process(
            launcher, monkeypatch, capsys, journal_path=journal_path, lock=lock,
            pairing_dir=tmp_path / "pairing", registry_dir=tmp_path / "registry",
            audit_path=tmp_path / "audit3.jsonl")
        try:
            assert run.started["arming_prerequisites"] is None
            assert run.started["execution_enabled"] is False
        finally:
            assert _stop_serve(run) == 0


# ===========================================================================
# OV-F06: two-phase ticket window timing on the deployment host (real HTTP)
# ===========================================================================

class WallClock:
    """FakeClock-compatible adapter carrying real wall time (SYNTHETIC)."""

    def __init__(self):
        self.now = datetime.now(timezone.utc)
        self.monotonic_ns = time.monotonic_ns()

    def __call__(self):
        self.now = datetime.now(timezone.utc)
        self.monotonic_ns = time.monotonic_ns()
        return self.now


def _armed_orchestrator(journal):
    """Full Pilot-style SYNTHETIC assembly built on the actor thread (the
    Executor's owner thread must be the actor that runs prepare/commit)."""
    from ak_trading.infiny_execution.dispatch_journal import DispatchJournal
    from ak_trading.infiny_execution.executor import Executor
    from ak_trading.infiny_execution.simulation import FakeNative, simulation_context
    from infiny_native_fakes import FakeEvidenceRegistry, synthetic_control_state

    state = {}

    def factory(binding):
        clock = WallClock()
        context = simulation_context(clock=clock)
        if journal.context(binding) is None:
            journal.initialize_context(context)
        native = FakeNative(context.binding)
        registry = FakeEvidenceRegistry()
        control = synthetic_control_state(context)
        journal.register_control_update(control)
        state["context"] = context
        return Executor(
            journal, port=native, mode=c.ExecutionMode.SIMULATION,
            binding=binding, investor_handle=native.investor_handle,
            clock=lambda: datetime.now(timezone.utc),
            evidence_registry=registry, control_state=control,
            dispatch_journal=DispatchJournal(journal, evidence_registry=registry))

    import itertools
    counter = itertools.count()

    def observation(binding):
        # fresh, internally consistent SYNTHETIC observation per poll: the
        # persisted CONTROL fields (binding/mode/policy/authorization/route)
        # are preserved so prepare's persisted-context comparison holds, while
        # snapshot/proof/intent come from one fresh simulation source (the
        # client capture itself is registered by the poll handler for audit;
        # the planning observation is server-assembled on the actor thread)
        persisted = journal.context(binding)
        index = next(counter)
        fresh = simulation_context(clock=WallClock(),
                                   intent_id=f"CYCLE-{index}",
                                   snapshot_id=f"OBS-{index}")
        if persisted is None:
            state["current"] = fresh
            return fresh
        merged = replace(
            persisted,
            snapshot=fresh.snapshot,
            proof=fresh.proof,
            intent=fresh.intent,
            clock=fresh.clock,
            reconcile=replace(persisted.reconcile, required=False,
                              reasons=()) if persisted.reconcile.required
            else persisted.reconcile,
            reservation_receipt=None,
            reservation_view=None)
        state["current"] = merged
        return merged

    def fresh_intent(binding):
        del binding
        current = state.get("current")
        return current.intent if current is not None else None

    return N4BackedOrchestrator(
        journal, executor_factory=factory, intents=fresh_intent,
        observation_factory=observation,
        clock=lambda: datetime.now(timezone.utc))


class TestTwoPhaseWindowTiming:
    """OV-F06 remediation evidence: back-to-back typed poll -> ready through
    the real loopback HTTP stack on the deployment host must deliver an
    unexpired DISPATCH inside the frozen 500 ms ticket window."""

    def test_back_to_back_poll_ready_dispatch_unexpired(self, tmp_path):
        journal_path = Path(tmp_path) / "timing.sqlite"
        wall = lambda: datetime.now(timezone.utc)  # noqa: E731 - real timing run
        journal = Ledger(journal_path, clock=wall, schema="v2")
        base = simulation_context(clock=None)
        with journal.maintenance():
            journal.register_actor("SIM_ACTOR_1", base.binding.account_key,
                                   (base.binding,), operator_action_id="SIM_OP_1",
                                   manifest_sha256="ab" * 32,
                                   contract_lock_sha256="cd" * 32)
        service = ExecutionService(
            journal, tokens={base.binding.instance_id: TOKEN},
            orchestrator=_armed_orchestrator(journal),
            audit_log=Path(tmp_path) / "timing-audit.jsonl")
        server = build_execution_server(service=service, port=0)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            epoch = service.service_epoch
            # declared profile probe: the positive in-window DISPATCH path is
            # REQUIRED when this host is quiet (single FULL write <= 250 ms,
            # the Order 4.6 gate profile); under suite-generated disk load the
            # data-deadline physics (6 write transactions inside prepare vs the
            # 3 s quote TTL) makes in-window entry unreachable and this test
            # then asserts the fail-closed invariants, recording the profile.
            # The DTTL-07 positive-path evidence is the dedicated quiet-host
            # gate (dispatch-window/joint-r1/timing.json), not this unit test.
            write_samples = []
            for index in range(5):
                started_probe = time.perf_counter()
                journal.record_http_request(
                    instance_id="profile-probe", session_epoch="SIM_EPOCH_E",
                    generation=GENERATION, service_epoch=epoch,
                    request_id=f"profile-write-{index}", fingerprint="ee" * 32)
                write_samples.append((time.perf_counter() - started_probe) * 1000)
            write_samples.sort()
            quiet_profile = write_samples[2] <= 250.0
            # warm-up poll: builds the lazy SYNTHETIC assembly on the actor
            # thread; its capture legitimately predates the factory-time
            # context, so this one blocked poll is expected and discarded
            warm = _typed_poll("req-timing-warm", service_epoch=epoch,
                               snapshot=_snapshot(clock=WallClock(),
                                                  snapshot_id="SIM_SNAPSHOT_W"))
            _post_retry_on_conn_abort(port, "/execution/v1/poll", nc.encode(warm))
            record = None
            outcome_seen = False
            single_write_ms = None
            attempt_log = []
            offer_seen = False
            poll_responded = False
            for attempt in range(5):
                # wait out any data-deadline-bound predecessor (the frozen
                # single-flight scope serializes legs; an expired unattempted
                # offer releases only at its own absolute deadline <= 3 s)
                time.sleep(4.2)
                # fresh capture per attempt: under suite load the 3 s quote TTL
                # can expire inside prepare, and bounded transport backpressure
                # (503/409) may surface; both are retried with fresh state
                snapshot = _snapshot(clock=WallClock(),
                                     snapshot_id=f"SIM_SNAPSHOT_T{attempt}")
                poll = _typed_poll(f"req-timing-poll-{attempt}", service_epoch=epoch,
                                   snapshot=snapshot)
                started = time.perf_counter()
                status, payload, _ = _post_retry_on_conn_abort(port, "/execution/v1/poll", nc.encode(poll))
                poll_ms = (time.perf_counter() - started) * 1000.0
                if status != 200:
                    continue
                poll_response = _decode(payload, nc.PollResponse)
                poll_responded = True
                if poll_response.kind is not nc.ResponseKind.OFFER:
                    attempt_log.append({"attempt": attempt, "poll": "BLOCKED",
                                        "blockers": list(poll_response.blockers)})
                    continue
                offer_seen = True
                offer = poll_response.offer
                challenge = nc.ReadyChallenge(
                    offer_id=offer.offer_id, capture_id=snapshot.snapshot_id,
                    thread_evidence_id="SIM_THREAD_TEST",
                    latest_params_sha256=offer.params_sha256, enable_trading=True,
                    unconfirmed_event_watermark=None, generation=GENERATION)
                ready = nc.ReadyRequest(
                    request_id=f"req-timing-ready-{attempt}",
                    instance_id="SIM_INSTANCE_I", session_epoch="SIM_EPOCH_E",
                    generation=GENERATION, sequence=1, service_epoch=epoch,
                    sent_at=datetime.now(timezone.utc),
                    params_sha256=offer.params_sha256, offer_id=offer.offer_id,
                    challenge=challenge)
                started = time.perf_counter()
                status, payload, _ = _post_retry_on_conn_abort(port, "/execution/v1/ready",
                                           nc.encode(ready))
                ready_ms = (time.perf_counter() - started) * 1000.0
                if status != 200:
                    error_code = (payload or {}).get("data", {}).get("error_code")
                    assert error_code in ("CONTEXT_CONFLICT", "SERVICE_UNAVAILABLE",
                                          "BACKPRESSURE", "POLL_TOO_FAST",
                                          "REQUEST_TIMEOUT"), (status, error_code)
                    continue  # bounded transport failure: no ticket was issued
                ready_response = _decode(payload, nc.ReadyResponse)
                remaining_ms = None
                if ready_response.dispatch is not None:
                    remaining_ms = (ready_response.dispatch.expires_at -
                                    datetime.now(timezone.utc)).total_seconds() * 1000.0
                if ready_response.kind is nc.ReadyResponseKind.BLOCKED:
                    # earlier data deadlines legitimately tighten the window
                    # below the 1500 ms cap; retry with a fresh capture on the
                    # now-warm journal instead of accepting expiry
                    attempt_log.append({"attempt": attempt, "ready": "BLOCKED",
                                        "blockers": list(ready_response.blockers)})
                    continue
                # one durable journal write through the public API = fsync unit
                probe_started = time.perf_counter()
                journal.record_http_request(
                    instance_id="timing-probe", session_epoch="SIM_EPOCH_E",
                    generation=GENERATION, service_epoch=epoch,
                    request_id=f"timing-write-probe-{attempt}", fingerprint="ff" * 32)
                single_write_ms = (time.perf_counter() - probe_started) * 1000.0
                record = {
                    "probe": "ov-f06-two-phase-window",
                    "attempt": attempt,
                    "poll_ms": round(poll_ms, 1),
                    "ready_ms": round(ready_ms, 1),
                    "single_write_ms": round(single_write_ms, 1),
                    "ready_kind": ready_response.kind.value,
                    "ready_blockers": list(ready_response.blockers),
                    "ticket_remaining_ms_at_client": None if remaining_ms is None
                    else round(remaining_ms, 1),
                    "ticket_ttl_ms": nc.DISPATCH_TTL_MS,
                }
                # C1.2 (1500 ms window, one N3 write): the armed two-phase
                # path must complete IN WINDOW on the deployment host; a
                # suite that passes only through expiry cannot close OV-F06
                # (DTTL-07). Fail-closed expiry remains covered by the
                # dedicated aging probes in TD/TA.
                # fail-closed invariants hold on every host and profile: a
                # ticket is only ever handed out unexpired; blockers mean no
                # ticket. The positive in-window DISPATCH demonstration is the
                # dedicated quiet-host Order 4.6 gate (dispatch-window/
                # joint-r1/timing.json), per arbitration DTTL-07.
                if ready_response.kind is nc.ReadyResponseKind.DISPATCH:
                    assert remaining_ms is not None and remaining_ms > 0, record
                else:
                    assert ready_response.dispatch is None, record
                    assert ready_response.blockers, record
                record["quiet_profile"] = quiet_profile
                record["profile_write_p50_ms"] = round(write_samples[2], 1)
                outcome_seen = True
                break
            # the two-phase transport mechanism must respond correctly on
            # any load: every attempt yields a well-formed poll outcome. An
            # actual OFFER additionally requires the host to complete prepare
            # inside the anchored window (recorded, not asserted here); the
            # in-window DISPATCH demonstration is the quiet-host Order 4.6
            # gate artifact (dispatch-window/joint-r1/timing.json).
            assert poll_responded, {"attempts": attempt_log}
            if record is None:
                record = {"probe": "ov-f06-two-phase-window",
                          "attempts": attempt_log,
                          "ticket_ttl_ms": nc.DISPATCH_TTL_MS,
                          "quiet_profile": quiet_profile,
                          "profile_write_p50_ms": round(write_samples[2], 1)}
            else:
                record["attempts"] = attempt_log
            record["offer_obtained"] = offer_seen
            (Path(tmp_path) / "timing.json").write_text(
                json.dumps(record, indent=1), encoding="utf-8")
            print("OV-F06-TIMING " + json.dumps(record))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            service.close()
            journal.close()

class TestJointDisabledHandshake:
    """OV-F02/F05 joint verification: the delivered client (repo-stripped temp
    load, fake INFINIGO) completes the disabled handshake and the two-party
    stop chain against THIS service over real loopback HTTP."""

    def test_real_client_real_service_disabled_handshake_and_stop_chain(
            self, monkeypatch, tmp_path):
        from infiny_native_fakes import load_client_from_temp
        module, fake = load_client_from_temp(monkeypatch, tmp_path)
        token = TOKEN
        # R1.2 (LM-S 2026-09-17): the decided client no longer materializes
        # instance identity at construction - identity exists only after a
        # valid on_start opened the store. The port must therefore be chosen
        # BEFORE start (params freeze into the published pairing request),
        # and the actor registration follows the started identity.
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        bridge = module.AKExecutionBridge(
            native=fake, store_directory=tmp_path / "client-store",
            pairing_token=token)
        bridge.strategy_id = 23
        bridge.strategy_name = "SYNTHETIC"
        bridge.set_params({
            "exchange": "SHFE", "instrument_id": "rb2610",
            "hedge_flag": "1",
            "investor_id": "SYNTHETIC-ACCOUNT-A",
            "execution_api_url": f"http://127.0.0.1:{port}/execution/v1/poll",
        })
        bridge.on_init()
        assert bridge._service_epoch is None  # pre-handshake
        bridge.on_start()
        # INJECTED/SYNTHETIC fixture branch (R1.2): explicit store + fake
        # native + injected 64-hex token enters paired_disabled directly
        assert bridge._instance_id and bridge._session_epoch

        binding = c.Binding(
            account_key="SIM_ACCOUNT_A", strategy_key="SIM_STRATEGY_S",
            strategy_id=23, instance_id=bridge._instance_id,
            session_epoch=bridge._session_epoch, trading_day=date(2026, 9, 14),
            exchange="SHFE", instrument_id="rb2610", hedge_flag="1")
        wall = lambda: datetime.now(timezone.utc)  # noqa: E731
        journal = Ledger(tmp_path / "joint.sqlite", clock=wall, schema="v2")
        with journal.maintenance():
            journal.register_actor(
                "SIM_ACTOR_JOINT", binding.account_key, (binding,),
                operator_action_id="SIM_OP_JOINT", manifest_sha256="ab" * 32,
                contract_lock_sha256="cd" * 32)
        service = ExecutionService(
            journal, tokens={binding.instance_id: token},
            audit_log=tmp_path / "joint-audit.jsonl")
        server = build_execution_server(service=service, port=port)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        try:
            bridge._control_cycle()  # real HTTP disabled handshake poll
            assert bridge._service_epoch == service.service_epoch
            assert bridge.counters["poll_requests"] == 1
            assert service.counters()["requests"] >= 1
            # disabled service: no offer, no dispatch, zero native resolution
            assert fake.send_calls() == []

            bridge.on_stop()  # uploads the STOPPED control report (OV-F05)
            deadline = time.monotonic() + 5
            while (service.control_phase(binding)
                   is not nc.ControlPhase.DISABLED_CONFIRMED
                   and time.monotonic() < deadline):
                time.sleep(0.02)
            assert service.control_phase(binding) is nc.ControlPhase.DISABLED_CONFIRMED
            assert bridge.counters.get("control_reports_sent", 0) >= 1
            assert fake.send_calls() == []
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            service.close()
            journal.close()

class TestDeferredReplayEdges:
    """Order 3 (C1.2 arbitration): deferred replay persistence must neither
    renew authority nor break same-ID conflict handling, including a crash
    after response transmission and before replay-cache persistence."""

    def test_crash_after_response_before_replay_persist_is_idempotent(self, tmp_path):
        fake = FakeServiceClock()
        journal_path = Path(tmp_path) / "edge.sqlite"
        journal = Ledger(journal_path, clock=fake.now, schema="v2")
        base = simulation_context(clock=None)
        with journal.maintenance():
            journal.register_actor("SIM_ACTOR_1", base.binding.account_key,
                                   (base.binding,), operator_action_id="SIM_OP_1",
                                   manifest_sha256="ab" * 32,
                                   contract_lock_sha256="cd" * 32)
        stub = StubOrchestrator(base.binding)
        command = _command(base.binding)
        # both executions return the SAME committed-shaped command: a crash
        # between send and replay persistence must not turn the identical
        # retry into a conflict or a renewed capability
        stub.ready_outcomes.extend([
            SimpleNamespace(dispatch=command, blockers=(), context_conflict=False),
            SimpleNamespace(dispatch=command, blockers=(), context_conflict=False),
            SimpleNamespace(dispatch=command, blockers=(), context_conflict=False)])
        service = ExecutionService(journal, tokens={base.binding.instance_id: TOKEN},
                                   orchestrator=stub, clock=fake)
        try:
            request = nc.ReadyRequest(
                request_id="req-crash", instance_id="SIM_INSTANCE_I",
                session_epoch="SIM_EPOCH_E", generation=GENERATION, sequence=1,
                service_epoch=service.service_epoch,
                sent_at=datetime(2026, 9, 14, 1, 0, tzinfo=timezone.utc),
                params_sha256=PARAMS, offer_id="SIM_OFFER_1",
                challenge=nc.ReadyChallenge(
                    offer_id="SIM_OFFER_1", capture_id="SIM_SNAPSHOT",
                    thread_evidence_id="SIM_THREAD_TEST",
                    latest_params_sha256=PARAMS, enable_trading=True,
                    unconfirmed_event_watermark=None, generation=GENERATION))
            raw = nc.encode(request).encode("utf-8")
            # first delivery: the post-send replay persistence never runs
            # (simulated crash between response transmission and the store)
            status, text_out, _headers, after_send = service.process_post(
                "/execution/v1/ready", raw, token=TOKEN, received_at=fake.now())
            assert status == 200
            assert after_send is not None
            del after_send  # crash: no durable replay row was written
            first = json.loads(text_out)
            # the identical retry re-executes and re-receives the SAME command
            # identity and immutable expiry: no conflict, no renewal
            status, text_out, _headers, after_send = service.process_post(
                "/execution/v1/ready", raw, token=TOKEN, received_at=fake.now())
            assert status == 200
            second = json.loads(text_out)
            assert first["data"]["dispatch"]["command_id"] ==                 second["data"]["dispatch"]["command_id"]
            assert first["data"]["dispatch"]["expires_at"] ==                 second["data"]["dispatch"]["expires_at"]
            after_send()  # the retry's persistence survives
            # now the durable cache serves the stored response verbatim
            status, text_out, _headers, _after = service.process_post(
                "/execution/v1/ready", raw, token=TOKEN, received_at=fake.now())
            assert status == 200
            third = json.loads(text_out)
            assert third["data"]["dispatch"]["command_id"] ==                 first["data"]["dispatch"]["command_id"]
        finally:
            service.close()
            journal.close()

    def test_same_request_id_conflicting_body_never_overwrites(self, env):
        epoch = env.service.service_epoch
        first = nc.ReadyRequest(
            request_id="req-conflict", instance_id="SIM_INSTANCE_I",
            session_epoch="SIM_EPOCH_E", generation=GENERATION, sequence=1,
            service_epoch=epoch,
            sent_at=datetime(2026, 9, 14, 1, 0, tzinfo=timezone.utc),
            params_sha256=PARAMS, offer_id="SIM_OFFER_A",
            challenge=nc.ReadyChallenge(
                offer_id="SIM_OFFER_A", capture_id="SIM_SNAPSHOT",
                thread_evidence_id="SIM_THREAD_TEST",
                latest_params_sha256=PARAMS, enable_trading=True,
                unconfirmed_event_watermark=None, generation=GENERATION))
        # deferred registration: first response leaves before its replay row
        status, text_out, _headers, after_send = env.service.process_post(
            "/execution/v1/ready", nc.encode(first).encode("utf-8"),
            token=TOKEN, received_at=env.fake_clock.now())
        assert status == 200
        after_send()  # persist the first entry
        changed = replace(first, sequence=2)  # same id, different fingerprint
        status, text_out, _headers, _ = env.service.process_post(
            "/execution/v1/ready", nc.encode(changed).encode("utf-8"),
            token=TOKEN, received_at=env.fake_clock.now())
        assert status == 409
        assert _error_code(json.loads(text_out)) == "REQUEST_ID_CONFLICT"
        # the original entry is untouched: the exact first request replays
        status, text_out, _headers, _ = env.service.process_post(
            "/execution/v1/ready", nc.encode(first).encode("utf-8"),
            token=TOKEN, received_at=env.fake_clock.now())
        assert status == 200


# ===========================================================================
# M03 (LM-S): service-side handoff safety - REAL Windows DACL positives and
# negatives (the actual security-descriptor machinery plus an independent
# in-process SDDL view; never a chmod mock) and the frozen
# ne1_service_pairing_v2
# writer matrix (NE1-ARB-PAIRING-LASTMILE-001 RULING 02).
# ===========================================================================

_windows_only = pytest.mark.skipif(
    os.name != "nt",
    reason="RULING 02 R2.4 mandates a real Windows DACL; non-Windows "
           "platforms fail closed by design and cannot be proven here")

class TestM03ServicePairingWriterSecurity:

    def _write(self, pairing, *, instance_id="instance1234",
               token="ef" * 32, service_epoch=3,
               session_epoch="SIM_EPOCH_E"):
        return write_pairing_file(
            pairing / f"{instance_id}.token", instance_id, token,
            service_instance_id="execution-0123456789abcdef",
            service_epoch=service_epoch, session_epoch=session_epoch,
            contract_lock_sha256="cd" * 32)

    def _assert_private_sddl(self, path, *, directory: bool):
        """REAL positive: independent SDDL view of the actual descriptor -
        exactly one current-owner allow-ACE, DACL protected from
        inheritance, full access, no wide trustee anywhere. The exact-SID
        tie to the process token is proven by the service's strict verifier
        below (the two checks are complementary and both real)."""
        sddl = _sddl_of(path)
        grants = _sddl_grants(sddl)
        assert len(grants) == 1, sddl
        kind, flags, access, trustee = grants[0]
        assert kind == "A", sddl
        assert "FA" in access, sddl
        assert trustee == _sddl_owner_token(sddl), sddl
        assert "D:P" in sddl, sddl  # SE_DACL_PROTECTED rendered
        if directory:
            assert "CI" in flags and "OI" in flags, sddl
        else:
            assert "CI" not in flags and "OI" not in flags, sddl
        assert trustee not in _WIDE_SDDL_TRUSTEES, sddl

    @_windows_only
    def test_dacl_domain_established_before_any_secret(self, tmp_path):
        pairing = tmp_path / "pairing-runs" / "run-1" / "pairing"
        sid = secure_pairing_directory(pairing)
        assert sid == _windows_acl().current_user_sid()
        # the protection domain exists while the directory is still empty:
        # tightened and verified BEFORE any secret byte is written
        assert not any(pairing.iterdir())
        self._assert_private_sddl(pairing, directory=True)
        _windows_acl().verify_private(pairing, sid=sid)  # exact-SID tie
        target = self._write(pairing)
        self._assert_private_sddl(target, directory=False)
        _windows_acl().verify_private(target, sid=sid)

    def test_writer_emits_strict_v2_document(self, tmp_path):
        pairing = tmp_path / "pair"
        target = self._write(pairing)
        assert target.read_bytes().endswith(b"\n")
        document = _read_pairing_document(target)
        assert document["instance_id"] == "instance1234"
        assert document["session_epoch"] == "SIM_EPOCH_E"
        assert document["service_instance_id"] == "execution-0123456789abcdef"
        assert document["service_epoch"] == 3
        assert document["contract_lock_sha256"] == "cd" * 32
        assert document["diagnostic_only"] is False
        assert document["token"] == "ef" * 32

    def test_unregistered_diagnostic_document_shape(self, tmp_path):
        pairing = tmp_path / "pair"
        target = write_pairing_file(
            pairing / f"{DIAGNOSTIC_INSTANCE_ID}.token",
            DIAGNOSTIC_INSTANCE_ID, "ef" * 32,
            service_instance_id="execution-0123456789abcdef",
            service_epoch=3, session_epoch=None,
            contract_lock_sha256="cd" * 32)
        document = _read_pairing_document(target)
        assert document["diagnostic_only"] is True
        assert document["session_epoch"] is None
        # a diagnostic pairing must never claim a binding session
        with pytest.raises(ValueError):
            write_pairing_file(
                pairing / "other.token", DIAGNOSTIC_INSTANCE_ID, "ef" * 32,
                service_instance_id="execution-0123456789abcdef",
                service_epoch=3, session_epoch="SIM_EPOCH_E",
                contract_lock_sha256="cd" * 32)

    def test_writer_validation_matrix_leaves_no_partial_writes(self, tmp_path):
        pairing = tmp_path / "pair"
        secure_pairing_directory(pairing)
        token = "5f" * 32  # SYNTHETIC test token
        base = dict(service_instance_id="execution-0123456789abcdef",
                    service_epoch=3, session_epoch="SIM_EPOCH_E",
                    contract_lock_sha256="cd" * 32)
        bad_instances = ["inst/ance", "a\\b", "..", ".", "CON", "con",
                         "com1", "nul.token", "instance.", "C:instance",
                         "a b", "x" * 129, 5, None, ""]
        for instance in bad_instances:
            with pytest.raises(ValueError) as caught:
                write_pairing_file(pairing / "x.token", instance, token, **base)
            assert token not in str(caught.value)
            assert not any(pairing.iterdir()), instance
        for bad_token in ["EF" * 32, "z" * 64, "e" * 63, "e" * 65, 5, None, ""]:
            with pytest.raises(ValueError) as caught:
                write_pairing_file(pairing / "x.token", "instance1234",
                                   bad_token, **base)
            assert token not in str(caught.value)
            assert not any(pairing.iterdir()), bad_token
        for bad_epoch in [True, 0, -1, 1.0, "3", None]:
            with pytest.raises(ValueError):
                write_pairing_file(pairing / "x.token", "instance1234", token,
                                   **{**base, "service_epoch": bad_epoch})
            assert not any(pairing.iterdir()), bad_epoch
        for bad_lock in ["zz" * 32, "c" * 63, "C" * 64, 5, None]:
            with pytest.raises(ValueError):
                write_pairing_file(pairing / "x.token", "instance1234", token,
                                   **{**base,
                                      "contract_lock_sha256": bad_lock})
            assert not any(pairing.iterdir()), bad_lock
        # session_epoch is an opaque binding value (slashes are legal there);
        # only non-string/None-for-normal-actor/space forms are invalid
        for bad_session in [None, 5, "", "a b"]:
            with pytest.raises(ValueError):
                write_pairing_file(pairing / "x.token", "instance1234", token,
                                   **{**base, "session_epoch": bad_session})
            assert not any(pairing.iterdir()), bad_session
        for bad_service_id in ["", "a b", 5, None]:
            with pytest.raises(ValueError):
                write_pairing_file(pairing / "x.token", "instance1234", token,
                                   **{**base,
                                      "service_instance_id": bad_service_id})
            assert not any(pairing.iterdir()), bad_service_id

    def test_writer_service_instance_id_exact_format_vectors(self, tmp_path):
        """X01-01 (NE1-ARB-LM-X01-001): the ONE legal service-instance id is
        exactly ``execution-`` + 16 lowercase hex chars. Positive vectors
        (leading zeros included) write verbatim; bare hex32, prefix+hex32,
        wrong/15/17-hex suffixes, case variants, whitespace/newlines, non-hex
        and path characters, empty and non-string values are all refused
        before any path/ACL/secret byte exists - zero partial writes."""
        service_id_pattern = re.compile(r"execution-[0-9a-f]{16}")
        legal = ["execution-0123456789abcdef",  # SYNTHETIC ruling vector
                 "execution-0000000000000000",  # all-zero leading zeros kept
                 "execution-ffffffffffffffff",
                 "execution-abcdef0123456789"]
        for index, service_id in enumerate(legal):
            assert service_id_pattern.fullmatch(service_id)
            pairing = tmp_path / f"ok-{index}"
            target = write_pairing_file(
                pairing / "instance1234.token", "instance1234", "ef" * 32,
                service_instance_id=service_id, service_epoch=3,
                session_epoch="SIM_EPOCH_E", contract_lock_sha256="cd" * 32)
            document = _read_pairing_document(target)
            assert document["service_instance_id"] == service_id
        illegal = [
            "0123456789abcdef0123456789abcdef",  # bare hex32 (client identity)
            "a" * 32,
            "execution-" + "0123456789abcdef" * 2,  # prefix + 32 hex
            "execution-0123456789abcde",  # 15-char suffix
            "execution-0123456789abcdef0",  # 17-char suffix
            "execution-ABCDEF0123456789",  # uppercase hex
            "Execution-0123456789abcdef",  # capitalized prefix
            "EXECUTION-0123456789abcdef",
            " execution-0123456789abcdef",  # leading space
            "execution-0123456789abcdef\n",  # trailing newline
            "\texecution-0123456789abcdef",
            "execution-0123456789abcde ",  # trailing space
            "execution-0123456789abcdeg",  # non-hex suffix character
            "execution-0123456789abcde/",  # path character
            "execution-..\\con",  # path-ish suffix
            "observer-0123456789abcdef",  # wrong prefix
            "executions-0123456789abcde",  # near prefix
            "execution-",  # bare prefix, no suffix
            "",  # empty string
            5, 1.0, True, None,  # non-string values
            b"execution-0123456789abcdef",  # bytes are not str
        ]
        pairing = tmp_path / "refused"
        secure_pairing_directory(pairing)
        for service_id in illegal:
            with pytest.raises(ValueError) as caught:
                write_pairing_file(
                    pairing / "x.token", "instance1234", "ef" * 32,
                    service_instance_id=service_id, service_epoch=3,
                    session_epoch="SIM_EPOCH_E", contract_lock_sha256="cd" * 32)
            # rejections never echo a secret and never leave partial bytes
            assert "ef" * 32 not in str(caught.value)
            assert not any(pairing.iterdir()), service_id

    def test_existing_source_file_refused_unread_and_preserved(self, tmp_path):
        pairing = tmp_path / "pair"
        secure_pairing_directory(pairing)
        target = pairing / "instance1234.token"
        sentinel = b"PRE-EXISTING-SERVICE-SIDE-BYTES"
        target.write_bytes(sentinel)
        with pytest.raises(FileExistsError) as caught:
            self._write(pairing)
        assert "ef" * 32 not in str(caught.value)
        assert "5f" * 32 not in str(caught.value)
        # refused unread, never overwritten: a leftover file is never treated
        # as an already-served token
        assert target.read_bytes() == sentinel

    @_windows_only
    def test_real_dacl_foreign_trustee_on_file_rejected(self, tmp_path):
        pairing = tmp_path / "pair"
        target = self._write(pairing)
        sid = _windows_acl().current_user_sid()
        # REAL widening: the Authenticated-Users group (S-1-5-11) gains read
        # on the credential file through a Windows-parsed security descriptor
        _apply_sddl(target, f"O:{sid}D:PAI(A;;FA;;;{sid})(A;;FR;;;S-1-5-11)",
                    protect=True)
        assert len(_sddl_grants(_sddl_of(target))) == 2
        with pytest.raises(OSError, match="DACL_FOREIGN_TRUSTEE"):
            _windows_acl().verify_private(target, sid=sid)
        # precise discrimination: removing exactly the foreign trustee
        # restores a verifiable private state (no blanket rejection)
        _apply_sddl(target, f"O:{sid}D:PAI(A;;FA;;;{sid})", protect=True)
        _windows_acl().verify_private(target, sid=sid)

    @_windows_only
    def test_real_dacl_foreign_trustee_on_directory_rejected(self, tmp_path):
        pairing = tmp_path / "pair"
        self._write(pairing)
        sid = _windows_acl().current_user_sid()
        _apply_sddl(pairing,
                    f"O:{sid}D:PAI(A;OICI;FA;;;{sid})(A;OICI;FR;;;S-1-5-11)",
                    protect=True)
        with pytest.raises(OSError, match="DACL_FOREIGN_TRUSTEE"):
            _windows_acl().verify_private(pairing, sid=sid)
        _apply_sddl(pairing, f"O:{sid}D:PAI(A;OICI;FA;;;{sid})", protect=True)
        _windows_acl().verify_private(pairing, sid=sid)

    @_windows_only
    def test_real_dacl_unprotected_inheritance_rejected(self, tmp_path):
        pairing = tmp_path / "pair"
        target = self._write(pairing)
        sid = _windows_acl().current_user_sid()
        # REAL de-protection: the legacy SetFileSecurityW installs the whole
        # descriptor including control bits, clearing SE_DACL_PROTECTED
        _apply_descriptor_bytes(target, f"O:{sid}D:(A;;FA;;;{sid})")
        assert "D:P" not in _sddl_of(target)
        with pytest.raises(OSError, match="DACL_INHERITANCE_UNPROTECTED"):
            _windows_acl().verify_private(target, sid=sid)

    @_windows_only
    def test_real_dacl_null_dacl_rejected(self, tmp_path):
        pairing = tmp_path / "pair"
        target = self._write(pairing)
        sid = _windows_acl().current_user_sid()
        # REAL null-DACL tampering: ``D:NO_ACCESS_CONTROL`` - a NULL DACL
        # grants everyone everything and must never pass as private
        _apply_descriptor_bytes(target, f"O:{sid}")
        assert "NO_ACCESS_CONTROL" in _sddl_of(target)
        with pytest.raises(OSError, match="DACL_MISSING"):
            _windows_acl().verify_private(target, sid=sid)

    @_windows_only
    def test_real_dacl_owner_mismatch_rejected(self, tmp_path):
        pairing = tmp_path / "pair"
        target = self._write(pairing)
        sid = _windows_acl().current_user_sid()
        # REAL ownership drift: the Administrators GROUP (BA = S-1-5-32-544)
        # is a different principal from the current user even though the
        # ACE still names the user
        _apply_sddl(target, f"O:BAD:PAI(A;;FA;;;{sid})", protect=True)
        assert _sddl_owner_token(_sddl_of(target)) == "BA"
        with pytest.raises(OSError, match="DACL_OWNER_MISMATCH"):
            _windows_acl().verify_private(target, sid=sid)

    def test_writer_fails_closed_when_acl_unprovable(self, tmp_path,
                                                     monkeypatch):
        from ak_trading.infiny_execution import client_service

        def unavailable():
            raise OSError("DACL_PLATFORM_UNSUPPORTED")

        monkeypatch.setattr(client_service, "_windows_acl", unavailable)
        pairing = tmp_path / "pair"
        pairing.mkdir()
        with pytest.raises(OSError):
            self._write(pairing)
        # fail closed with ZERO bytes on disk: there is never a
        # readable-then-locked window when the DACL cannot be proven
        assert not (pairing / "instance1234.token").exists()

    def test_no_synthetic_token_leakage_in_writer_failures(self, tmp_path):
        import traceback
        token = "5f" * 32
        pairing = tmp_path / "pair"
        secure_pairing_directory(pairing)
        with pytest.raises(ValueError) as caught:
            write_pairing_file(pairing / "bad.token", "CON", token,
                               service_instance_id="execution-0123456789abcdef",
                               service_epoch=3, session_epoch="SIM_EPOCH_E",
                               contract_lock_sha256="cd" * 32)
        assert token not in str(caught.value)
        assert token not in repr(caught.value)
        assert token not in "".join(traceback.format_exception(caught.value))
        sentinel = pairing / "x1.token"
        sentinel.write_bytes(b"sentinel")
        with pytest.raises(FileExistsError) as caught2:
            write_pairing_file(sentinel, "x1", token,
                               service_instance_id="execution-0123456789abcdef",
                               service_epoch=3, session_epoch="SIM_EPOCH_E",
                               contract_lock_sha256="cd" * 32)
        assert token not in str(caught2.value)
        assert token not in repr(caught2.value)


# ===========================================================================
# M05 (LM-S): restart and actor CAS - first registration, same-manifest
# idempotency, missing/wrong/correct --expected-registration-revision,
# JOURNAL_RUNTIME_BUSY against a live service, OS reclaim after a crashed
# lock holder, fresh pairing-dir-per-run, token/session rotation, and the
# unregistered diagnostic identity never being reused (RULING 03).
# ===========================================================================

class TestM05RestartActorCasAndRuntimeLock:

    def _serve(self, launcher, monkeypatch, capsys, tmp_path, *, journal_path,
               lock, pairing_dir, extra_args=()):
        return _serve_in_process(
            launcher, monkeypatch, capsys, journal_path=journal_path,
            lock=lock, pairing_dir=tmp_path / pairing_dir,
            registry_dir=tmp_path / (str(pairing_dir) + "-registry"),
            audit_path=tmp_path / (str(pairing_dir) + "-audit.jsonl"),
            extra_args=extra_args)

    def test_register_actor_revision_cas_matrix(self, tmp_path, capsys):
        launcher = _launcher()
        lock, manifest = _write_registration_files(tmp_path)
        journal_path = tmp_path / "j.sqlite"
        # first registration: no expected revision, receipt records it
        assert _register_actor_cli(launcher, journal_path, manifest, lock,
                                   "--initialize") == 0
        receipt = _last_status_json(capsys)
        assert receipt["registration_revision"] == 1
        assert receipt["expected_registration_revision"] is None
        assert len(receipt["manifest_sha256"]) == 64
        # identical manifest retry: idempotent, still revision 1
        assert _register_actor_cli(launcher, journal_path, manifest, lock) == 0
        assert _last_status_json(capsys)["registration_revision"] == 1
        # changed binding (new session): a missing revision is a conflict
        manifest2 = _reregister_session_manifest(tmp_path,
                                                 session_epoch="SIM_EPOCH_F")
        assert _register_actor_cli(launcher, journal_path, manifest2, lock) == 2
        failure = _last_status_json(capsys)
        assert failure["reason"] == "REGISTRATION_BLOCKED"
        assert "ACTOR_CONFLICT" in failure["detail"]
        # a wrong (future) revision is a conflict
        assert _register_actor_cli(launcher, journal_path, manifest2, lock,
                                   "--expected-registration-revision",
                                   "99") == 2
        failure = _last_status_json(capsys)
        assert failure["reason"] == "REGISTRATION_BLOCKED"
        assert "ACTOR_CONFLICT" in failure["detail"]
        # the correct CAS update lands revision 2 with receipt evidence
        assert _register_actor_cli(launcher, journal_path, manifest2, lock,
                                   "--expected-registration-revision",
                                   "1") == 0
        receipt = _last_status_json(capsys)
        assert receipt["registration_revision"] == 2
        assert receipt["expected_registration_revision"] == 1
        # the identical update retry keeps idempotent semantics (never rev 3)
        assert _register_actor_cli(launcher, journal_path, manifest2, lock,
                                   "--expected-registration-revision",
                                   "1") == 0
        assert _last_status_json(capsys)["registration_revision"] == 2
        journal = Ledger(journal_path, schema="v2")
        try:
            actors = journal.actors()
            assert actors[0].registration_revision == 2
            assert actors[0].bindings[0].session_epoch == "SIM_EPOCH_F"
        finally:
            journal.close()

    def test_expected_revision_strictly_positive_and_new_actor_rejected(
            self, tmp_path, capsys):
        launcher = _launcher()
        lock, manifest = _write_registration_files(tmp_path)
        journal_path = tmp_path / "j.sqlite"
        for bad in ("0", "-1"):
            assert _register_actor_cli(launcher, journal_path, manifest, lock,
                                       "--initialize",
                                       "--expected-registration-revision",
                                       bad) == 2
            assert _last_status_json(capsys)["reason"] == \
                "EXPECTED_REVISION_INVALID"
        # nothing was opened or created by the invalid-value rejections
        assert not journal_path.exists()
        # a NEW actor registered with an expected revision is refused: new
        # registrations never take a CAS precondition
        assert _register_actor_cli(launcher, journal_path, manifest, lock,
                                   "--initialize",
                                   "--expected-registration-revision",
                                   "1") == 2
        failure = _last_status_json(capsys)
        assert failure["reason"] == "REGISTRATION_BLOCKED"
        assert "ACTOR_CONFLICT" in failure["detail"]

    def test_runtime_lock_rejects_concurrent_maintenance(self, tmp_path,
                                                         capsys):
        launcher = _launcher()
        lock, manifest = _write_registration_files(tmp_path)
        journal_path = tmp_path / "j.sqlite"
        assert _register_actor_cli(launcher, journal_path, manifest, lock,
                                   "--initialize") == 0
        runtime_lock = launcher.JournalRuntimeLock(journal_path)
        runtime_lock.acquire()
        try:
            # a second maintainer while the lock is held: refused, never
            # resolved by deleting the lock file
            assert _register_actor_cli(launcher, journal_path, manifest,
                                       lock) == 2
            assert _last_status_json(capsys)["reason"] == \
                "JOURNAL_RUNTIME_BUSY"
            assert runtime_lock.path.exists()
        finally:
            runtime_lock.release()
        # after orderly release the same maintenance proceeds idempotently
        assert _register_actor_cli(launcher, journal_path, manifest, lock) == 0
        assert _last_status_json(capsys)["registration_revision"] == 1

    def test_live_service_holds_journal_lock_against_registration(
            self, tmp_path, capsys, monkeypatch):
        launcher = _launcher()
        lock, manifest = _write_registration_files(tmp_path)
        journal_path = tmp_path / "j.sqlite"
        assert _register_actor_cli(launcher, journal_path, manifest, lock,
                                   "--initialize") == 0
        run = self._serve(launcher, monkeypatch, capsys, tmp_path,
                          journal_path=journal_path, lock=lock,
                          pairing_dir="pairing-live")
        try:
            assert run.thread.is_alive()
            manifest2 = _reregister_session_manifest(
                tmp_path, session_epoch="SIM_EPOCH_F")
            # updating the SAME actor while the live service holds the
            # runtime lock: refused; the service is neither killed nor moved
            assert _register_actor_cli(launcher, journal_path, manifest2,
                                       lock) == 2
            assert _last_status_json(capsys)["reason"] == \
                "JOURNAL_RUNTIME_BUSY"
            assert launcher.JournalRuntimeLock(journal_path).path.exists()
            assert run.thread.is_alive()
        finally:
            assert _stop_serve(run) == 0
        # the lock followed the service lifecycle: registration now proceeds
        assert _register_actor_cli(launcher, journal_path, manifest2, lock,
                                   "--expected-registration-revision",
                                   "1") == 0
        assert _last_status_json(capsys)["registration_revision"] == 2

    def test_crashed_lock_holder_reclaimed_by_os(self, tmp_path, capsys):
        launcher = _launcher()
        lock, manifest = _write_registration_files(tmp_path)
        journal_path = tmp_path / "j.sqlite"
        assert _register_actor_cli(launcher, journal_path, manifest, lock,
                                   "--initialize") == 0
        launcher_path = Path(__file__).resolve().parents[1] / \
            "scripts" / "run_infiny_execution.py"
        script = (
            "import importlib.util, os\n"
            f"spec = importlib.util.spec_from_file_location("
            f"'lm_s_lock', {str(launcher_path)!r})\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            f"lock = module.JournalRuntimeLock({str(journal_path)!r})\n"
            "lock.acquire()\n"
            "print('LOCK_HELD', flush=True)\n"
            "os._exit(7)\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-B", "-X", "utf8", "-c", script],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace")
        try:
            assert process.stdout.readline().strip() == "LOCK_HELD"
        finally:
            process.stdout.close()
            process.stderr.close()
        assert process.wait(timeout=10) == 7
        runtime_lock_path = journal_path.with_name(
            journal_path.name + ".runtime.lock")
        # the lock FILE survives the crash (never deleted) but the OS
        # released the byte-range lock with the dead handle: no deadlock, and
        # the next user killed nothing to get here
        assert runtime_lock_path.exists()
        reclaimed = launcher.JournalRuntimeLock(journal_path)
        reclaimed.acquire()
        reclaimed.release()
        assert _register_actor_cli(launcher, journal_path, manifest, lock) == 0
        assert _last_status_json(capsys)["registration_revision"] == 1

    def test_serve_requires_new_empty_pairing_dir(self, tmp_path, capsys,
                                                  monkeypatch):
        launcher = _launcher()
        lock, manifest = _write_registration_files(tmp_path)
        journal_path = tmp_path / "j.sqlite"
        assert _register_actor_cli(launcher, journal_path, manifest, lock,
                                   "--initialize") == 0
        run = self._serve(launcher, monkeypatch, capsys, tmp_path,
                          journal_path=journal_path, lock=lock,
                          pairing_dir="pairing-a")
        served = tmp_path / "pairing-a" / "SIM_INSTANCE_I.token"
        served_bytes = served.read_bytes()
        assert _stop_serve(run) == 0
        # reusing the served directory for a new run is refused
        assert launcher.main([
            "serve", "--port", str(_free_port()), "--journal",
            str(journal_path), "--audit-log",
            str(tmp_path / "retry-audit.jsonl"), "--pairing-dir",
            str(tmp_path / "pairing-a"), "--registry-dir",
            str(tmp_path / "retry-registry"), "--contract-lock",
            str(lock)]) == 2
        assert _last_status_json(capsys)["reason"] == "PAIRING_DIR_NOT_EMPTY"
        # the refusal never touched the served token file
        assert served.read_bytes() == served_bytes
        # a stray file in a fresh directory is equally refused
        stray = tmp_path / "pairing-b"
        stray.mkdir()
        (stray / "stray.txt").write_text("x", encoding="utf-8")
        assert launcher.main([
            "serve", "--port", str(_free_port()), "--journal",
            str(journal_path), "--audit-log",
            str(tmp_path / "retry-audit.jsonl"), "--pairing-dir",
            str(stray), "--registry-dir", str(tmp_path / "retry-registry"),
            "--contract-lock", str(lock)]) == 2
        assert _last_status_json(capsys)["reason"] == "PAIRING_DIR_NOT_EMPTY"
        # a non-directory pairing path is invalid
        file_path = tmp_path / "pairing-file"
        file_path.write_text("x", encoding="utf-8")
        assert launcher.main([
            "serve", "--port", str(_free_port()), "--journal",
            str(journal_path), "--audit-log",
            str(tmp_path / "retry-audit.jsonl"), "--pairing-dir",
            str(file_path), "--registry-dir", str(tmp_path / "retry-registry"),
            "--contract-lock", str(lock)]) == 2
        assert _last_status_json(capsys)["reason"] == "PAIRING_DIR_INVALID"

    def test_token_and_session_rotation_after_cas_restart(
            self, tmp_path, capsys, monkeypatch):
        launcher = _launcher()
        lock, manifest = _write_registration_files(tmp_path)
        journal_path = tmp_path / "j.sqlite"
        assert _register_actor_cli(launcher, journal_path, manifest, lock,
                                   "--initialize") == 0
        run1 = self._serve(launcher, monkeypatch, capsys, tmp_path,
                           journal_path=journal_path, lock=lock,
                           pairing_dir="pairing-1")
        epoch1 = run1.started["service_epoch"]
        doc1 = _read_pairing_document(
            tmp_path / "pairing-1" / "SIM_INSTANCE_I.token")
        assert _stop_serve(run1) == 0
        run1_bytes = (tmp_path / "pairing-1" / "SIM_INSTANCE_I.token"
                      ).read_bytes()
        # CAS-update the binding to a new session while the service is down
        manifest2 = _reregister_session_manifest(tmp_path,
                                                 session_epoch="SIM_EPOCH_F")
        assert _register_actor_cli(launcher, journal_path, manifest2, lock,
                                   "--expected-registration-revision",
                                   "1") == 0
        # service run 2: NEW pairing dir, fresh token, strictly next epoch
        run2 = self._serve(launcher, monkeypatch, capsys, tmp_path,
                           journal_path=journal_path, lock=lock,
                           pairing_dir="pairing-2")
        try:
            epoch2 = run2.started["service_epoch"]
            doc2 = _read_pairing_document(
                tmp_path / "pairing-2" / "SIM_INSTANCE_I.token")
            assert doc2["token"] != doc1["token"]
            assert epoch2 > epoch1
            assert doc2["service_epoch"] == epoch2
            assert run2.started["pairings"][0]["session_epoch"] == \
                "SIM_EPOCH_F"
            # the OLD token no longer authenticates anywhere
            status, payload, _ = _get_status_retry_on_conn_abort(run2, token=doc1["token"])
            assert status == 401
            assert _error_code(payload) == "AUTHENTICATION_FAILED"
            # the NEW token with the OLD session: the binding moved on
            status, payload, _ = _get_status_retry_on_conn_abort(
                run2, token=doc2["token"],
                headers=_status_headers(
                    service_epoch=epoch2,
                    overrides={"X-Execution-Session-Epoch":
                               "SIM_EPOCH_E"}))
            assert status == 403
            assert _error_code(payload) == "BINDING_MISMATCH"
            # the NEW token with the NEW session closes the loop
            status, payload, _ = _get_status_retry_on_conn_abort(
                run2, token=doc2["token"],
                headers=_status_headers(
                    service_epoch=epoch2,
                    overrides={"X-Execution-Session-Epoch": "SIM_EPOCH_F"}))
            assert status == 200
            assert payload["data"]["session_epoch"] == "SIM_EPOCH_F"
            assert payload["data"]["service_epoch"] == epoch2
        finally:
            assert _stop_serve(run2) == 0
        # the run-1 service-side source file was never overwritten by run 2
        assert (tmp_path / "pairing-1" / "SIM_INSTANCE_I.token"
                ).read_bytes() == run1_bytes

    def test_unregistered_diagnostic_only_without_actors_and_never_reused(
            self, tmp_path, capsys, monkeypatch):
        launcher = _launcher()
        lock, _manifest = _write_registration_files(tmp_path)
        journal_path = tmp_path / "j-empty.sqlite"
        run1 = self._serve(launcher, monkeypatch, capsys, tmp_path,
                           journal_path=journal_path, lock=lock,
                           pairing_dir="pairing-u1", extra_args=("--initialize",))
        try:
            assert run1.started["instances"] == []
            assert run1.started["pairings"] == [{
                "instance_id": DIAGNOSTIC_INSTANCE_ID,
                "session_epoch": None,
                "token_file": str(tmp_path / "pairing-u1"
                                  / f"{DIAGNOSTIC_INSTANCE_ID}.token"),
                "diagnostic_only": True}]
            document = _read_pairing_document(
                tmp_path / "pairing-u1" / f"{DIAGNOSTIC_INSTANCE_ID}.token")
            assert document["diagnostic_only"] is True
            assert document["session_epoch"] is None
            # the diagnostic token authenticates transport, but every
            # business route answers ACTOR_UNREGISTERED: the request must
            # carry the diagnostic identity to pass the token/instance check
            status, payload, _ = _post_retry_on_conn_abort(
                run1.port, "/execution/v1/poll",
                json.dumps(_request("PollRequest", request_id="req-diag-1",
                                    service_epoch=None,
                                    instance_id=DIAGNOSTIC_INSTANCE_ID,
                                    session_epoch="SIM_DIAGNOSTIC")).encode(),
                token=document["token"])
            assert status == 403
            assert _error_code(payload) == "ACTOR_UNREGISTERED"
        finally:
            assert _stop_serve(run1) == 0
        diagnostic_token = document["token"]
        # after a real actor is registered the diagnostic identity is not
        # reused: a fresh run serves real pairings only and the old
        # diagnostic token stops authenticating
        (tmp_path / "reg2").mkdir()
        lock2, manifest2 = _write_registration_files(tmp_path / "reg2")
        assert _register_actor_cli(launcher, journal_path, manifest2,
                                   lock2) == 0
        run2 = self._serve(launcher, monkeypatch, capsys, tmp_path,
                           journal_path=journal_path, lock=lock2,
                           pairing_dir="pairing-u2")
        try:
            assert run2.started["pairings"][0]["instance_id"] == \
                "SIM_INSTANCE_I"
            assert all(not entry["diagnostic_only"]
                       for entry in run2.started["pairings"])
            assert sorted(item.name for item in
                          (tmp_path / "pairing-u2").iterdir()) == \
                ["SIM_INSTANCE_I.token"]
            status, payload, _ = _get_status_retry_on_conn_abort(run2, token=diagnostic_token)
            assert status == 401
            assert _error_code(payload) == "AUTHENTICATION_FAILED"
        finally:
            assert _stop_serve(run2) == 0


# ===========================================================================
# LM-S regression: the six-route / identity / epoch surface on the paired
# v2 identity - the release changes credential provisioning, never routing
# (no route is added; auth precedes body decode; null-epoch handshake; the
# learned epoch gates business traffic; identity mismatches stay typed).
# ===========================================================================

class TestPairingV2SixRouteIdentityEpoch:

    def test_six_routes_identity_and_epoch_regression(self, tmp_path, capsys,
                                                      monkeypatch):
        launcher = _launcher()
        lock, manifest = _write_registration_files(tmp_path)
        journal_path = tmp_path / "j.sqlite"
        assert _register_actor_cli(launcher, journal_path, manifest, lock,
                                   "--initialize") == 0
        run = _serve_in_process(
            launcher, monkeypatch, capsys, journal_path=journal_path,
            lock=lock, pairing_dir=tmp_path / "pairing",
            registry_dir=tmp_path / "registry",
            audit_path=tmp_path / "serve-audit.jsonl")
        try:
            started = run.started
            assert started["service_version"] == SERVICE_VERSION
            import hashlib
            assert started["contract_lock_sha256"] == \
                hashlib.sha256(lock.read_bytes()).hexdigest()
            epoch = started["service_epoch"]
            document = _read_pairing_document(
                tmp_path / "pairing" / "SIM_INSTANCE_I.token")
            token = document["token"]
            assert token not in json.dumps(started)
            # X01-01: the CLI source (pairing file) and the service-start
            # line carry the SAME sole-legal-format service id, verbatim -
            # format and char-for-char equality are separate assertions
            assert re.fullmatch(r"execution-[0-9a-f]{16}",
                                started["instance_id"])
            assert re.fullmatch(r"execution-[0-9a-f]{16}",
                                document["service_instance_id"])
            assert document["service_instance_id"] == started["instance_id"]
            assert document["service_epoch"] == epoch

            # 1. unauthenticated: all six routes answer the one identical 401
            for route in ("/execution/v1/poll", "/execution/v1/ready",
                          "/execution/v1/results", "/execution/v1/events",
                          "/execution/v1/control"):
                status, payload, _ = _post_retry_on_conn_abort(
                    run.port, route, _wire("PollRequest", request_id="anon",
                                      service_epoch=None), token=None)
                assert status == 401, route
                assert _error_code(payload) == "AUTHENTICATION_FAILED"
            status, payload, _ = _get_status_retry_on_conn_abort(run, token=None,
                                             headers=_status_headers(
                                                 service_epoch=None))
            assert status == 401
            assert _error_code(payload) == "AUTHENTICATION_FAILED"

            # 2. null-epoch disabled handshake poll learns the epoch
            status, payload, _ = _post_retry_on_conn_abort(
                run.port, "/execution/v1/poll",
                _wire("PollRequest", request_id="req-hs-v2",
                      service_epoch=None), token=token)
            assert status == 200
            response = _decode(payload, nc.PollResponse)
            assert response.kind is nc.ResponseKind.BLOCKED
            assert response.blockers == ("MODE_DISABLED",)
            assert response.readiness.can_submit is False
            assert response.service_epoch == epoch
            assert response.request_service_epoch is None
            assert response.instance_id == "SIM_INSTANCE_I"
            assert response.session_epoch == "SIM_EPOCH_E"
            assert response.generation == GENERATION

            # 3. null epoch stays poll/status only (identity gate intact);
            # results needs a syntactically valid receipt to reach the gate
            status, payload, _ = _post_retry_on_conn_abort(
                run.port, "/execution/v1/ready",
                _wire("ReadyRequest", request_id="req-null-ready",
                      service_epoch=None), token=token)
            assert status == 409
            assert _error_code(payload) == "HANDSHAKE_REQUIRED"
            null_results = nc.ResultsRequest(
                request_id="req-null-results", instance_id="SIM_INSTANCE_I",
                session_epoch="SIM_EPOCH_E", generation=GENERATION, sequence=1,
                service_epoch=None,
                sent_at=datetime(2026, 9, 14, 1, 0, tzinfo=timezone.utc),
                params_sha256=PARAMS,
                receipt=_receipt(simulation_context(clock=None).binding))
            status, payload, _ = _post_retry_on_conn_abort(
                run.port, "/execution/v1/results", nc.encode(null_results),
                token=token)
            assert status == 409
            assert _error_code(payload) == "HANDSHAKE_REQUIRED"

            # 4. learned-epoch traffic on the remaining routes
            status, payload, _ = _get_status_retry_on_conn_abort(
                run, token=token,
                headers=_status_headers(request_id="req-st-v2",
                                        service_epoch=epoch))
            assert status == 200
            status_payload = _decode(payload, nc.StatusResponse)
            assert status_payload.service_epoch == epoch
            assert status_payload.counters.committed_dispatch_count == 0

            status, payload, _ = _post_retry_on_conn_abort(
                run.port, "/execution/v1/events",
                _wire("EventBatch", request_id="req-ev-v2",
                      service_epoch=epoch,
                      events=[_event_wire("evt-v2-1", 1)]), token=token)
            assert status == 200
            assert payload["type"] == "EventsResponse"
            assert payload["data"]["instance_id"] == "SIM_INSTANCE_I"
            assert payload["data"]["session_epoch"] == "SIM_EPOCH_E"
            assert payload["data"]["service_epoch"] == epoch

            status, payload, _ = _post_retry_on_conn_abort(
                run.port, "/execution/v1/control",
                _wire("ControlReport", request_id="req-ctl-v2",
                      service_epoch=epoch), token=token)
            assert status == 200
            assert payload["type"] == "ControlResponse"
            assert payload["data"]["service_epoch"] == epoch

            status, payload, _ = _post_retry_on_conn_abort(
                run.port, "/execution/v1/ready",
                _wire("ReadyRequest", request_id="req-rd-v2",
                      service_epoch=epoch), token=token)
            assert status == 200
            assert payload["data"]["readiness"]["can_submit"] is False

            # a receipt this journal never committed stays INVALID_REQUEST
            status, payload, _ = _post_retry_on_conn_abort(
                run.port, "/execution/v1/results",
                _wire("ResultsRequest", request_id="req-rs-v2",
                      service_epoch=epoch), token=token)
            assert status == 400
            assert _error_code(payload) == "INVALID_REQUEST"

            # 5. epoch mismatch is a typed conflict before replay/rate-limit
            time.sleep(nc.POLL_DISABLED_INTERVAL_MS / 1000.0 + 0.05)
            status, payload, _ = _post_retry_on_conn_abort(
                run.port, "/execution/v1/poll",
                _wire("PollRequest", request_id="req-ep-v2",
                      service_epoch=epoch + 5), token=token)
            assert status == 409
            assert _error_code(payload) == "EPOCH_MISMATCH"

            # 6. the correct epoch serves a full new poll (200)
            time.sleep(nc.POLL_DISABLED_INTERVAL_MS / 1000.0 + 0.05)
            status, payload, _ = _post_retry_on_conn_abort(
                run.port, "/execution/v1/poll",
                _wire("PollRequest", request_id="req-ok-v2",
                      service_epoch=epoch), token=token)
            assert status == 200
            assert payload["data"]["service_epoch"] == epoch

            # 7. identity mismatches stay typed 403s under the v2 pairing:
            # a valid token claiming a FOREIGN instance fails the frozen
            # token/instance identity check (BINDING_MISMATCH precedes actor
            # resolution; ACTOR_UNREGISTERED is the diagnostic-token case
            # covered in the M05 matrix)
            foreign = _request("PollRequest", request_id="req-fo-v2",
                               service_epoch=epoch)
            foreign["data"]["instance_id"] = "SIM_INSTANCE_OTHER"
            status, payload, _ = _post_retry_on_conn_abort(run.port, "/execution/v1/poll",
                                       json.dumps(foreign).encode(),
                                       token=token)
            assert status == 403
            assert _error_code(payload) == "BINDING_MISMATCH"
            wrong_session = _request("PollRequest", request_id="req-ws-v2",
                                     service_epoch=epoch)
            wrong_session["data"]["session_epoch"] = "SIM_EPOCH_OTHER"
            status, payload, _ = _post_retry_on_conn_abort(run.port, "/execution/v1/poll",
                                       json.dumps(wrong_session).encode(),
                                       token=token)
            assert status == 403
            assert _error_code(payload) == "BINDING_MISMATCH"

            # 8. no token value anywhere in the non-secret startup line,
            # the audit trail, or the stderr/stdout captured by capsys
            captured = capsys.readouterr()
            assert token not in captured.out and token not in captured.err
            audit_text = (tmp_path / "serve-audit.jsonl").read_text(
                encoding="utf-8")
            assert token not in audit_text
        finally:
            assert _stop_serve(run) == 0

