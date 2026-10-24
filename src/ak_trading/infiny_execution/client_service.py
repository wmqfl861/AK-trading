"""NE1 C3 local execution service: 127.0.0.1:17779, /execution/v1/ (six routes).

The service is the planning/ledger half of the execution channel. It never
imports INFINIGO/pythongo, never holds a real Investor, and never calls a
counter: the default N4-backed orchestrator assembles ``Executor`` with
``port=None, mode=DISABLED`` (every prepare/commit attempt yields the declared
``MODE_DISABLED`` business blocker) and only ``/results`` receipt projection
plus journal bookkeeping run in production today. The 17778 observer service
is a separate process with a separate lifecycle; nothing here touches it.

Frozen dependencies consumed (C1.1/C2 passed hashes):
  * native_contracts e5c44742…  transport DTOs, error table, GET header map,
    contiguous ACK cursor, request fingerprint (RULINGS 01-03, 06, 09, 10).
  * ledger e6d824bd…            journal: actors/maintenance/service epoch,
    HTTP replay, captures, event streams, counters, control revision.
  * executor 82c185c7…          two-phase native orchestration (N4).
  * planning 619dfef4…          plan_native_pilot (N2, consumed via Executor).

Service-owned vocabulary projection (documented, frozen for review): internal
orchestration codes that are not declared ``BLOCKER_CODES`` map as follows so
every V2Readiness stays constructible —
  CONTEXT_REQUIRED/CONTROL_STATE_REQUIRED/CONTROL_STATE_MISMATCH/OFFER_UNKNOWN/
  OFFER_MISMATCH/PARAMS_CHANGED                         -> AUTHORIZATION_REQUIRED
  NATIVE_PERMIT_UNREGISTERED/NATIVE_PERMIT_EXPIRED       -> NATIVE_DISABLED
  DISPATCH_JOURNAL_REQUIRED/GATE_CLOSED                  -> MODE_DISABLED
  ATTEMPT_EXISTS                                         -> INFLIGHT_LIMIT
  PERMIT_EXHAUSTED                                       -> AUTHORIZATION_ACTION
  BLOCKED_DISPATCH                                       -> RECONCILE_REQUIRED
  GENERATION_CHANGED                                     -> SESSION_CHANGED
  DISPATCH_EXPIRED                                       -> EXPIRED
Ledger ``ResultCode.REVISION_CONFLICT`` becomes the typed transport error
``CONTEXT_CONFLICT`` (409); ``SNAPSHOT_CONFLICT`` maps 1:1; event stream
conflicts (EVENT_CONFLICT/EVENT_STREAM_MIXED/EVENT_STREAM_CONFLICT) become
``ACK_WATERMARK_INVALID``; receipt content conflicts become
``RECEIPT_CONFLICT``; journal ``NOT_FOUND`` on the results seam (a receipt for
a command this journal never committed) is reported as ``INVALID_REQUEST``
because the typed error table declares no not-found transport code.

Known C3 limitations (explicit, not hidden): duplicate-event detection inside
``/events`` responses and the status ACK lookup are process-local views (the
frozen journal API exposes no stream-by-instance reader); durable intent
registration and control arming remain offline operator machinery (RULING 05)
and no HTTP route can create either.

OV-F06 remediation (overall review r1, deployment-host latency): the armed
/ready path previously cost five ``synchronous=FULL`` write transactions
(replay registration + N4 double CAS + begin_dispatch + response store) and
structurally exceeded the frozen 500 ms ticket window. Service-side fixes,
all inside this file:

1. Bounded in-memory replay index in front of the durable journal replay:
   exact retries are served with zero journal writes; a same-request-id
   different-fingerprint conflict is rejected from memory. The durable
   journal cache remains the cross-restart source of truth.
2. Post-send response durability: ``store_http_response`` runs after the
   response bytes are written to the socket, removing one FULL fsync from
   every response latency (ticket issue and consumption windows).
3. Deferred durable replay registration for the /ready route only: the
   ticket-critical path performs no journal write before business. Safety
   anchor: N3 ``begin_dispatch`` is atomically idempotent per offer (a retry
   re-reads the same committed command, zero new attempts or permits), and
   the post-send registration still detects conflicts durably — the response
   may already have left, so the conflict is latched
   (``replay_conflicts_postsend``) and the earlier journal entry is never
   overwritten. Original replay responses keep their first stored bytes.

Remaining in-window cost after these fixes is three FULL writes inside the
frozen N4/N3 code (persisted-context CAS refresh x2 + begin_dispatch), which
this owner cannot merge without a cross-node change; measured numbers live in
the TP two-phase timing test and the revision report.

NE1 pairing last-mile (NE1-ARB-PAIRING-LASTMILE-001, LM-S, 2026-09-17): the
service release identifier is ``SERVICE_VERSION`` (``ne1-service-20260917-
pairing-v2``). ``write_pairing_file`` now emits the frozen private
``ne1_service_pairing_v2`` JSON (strict fields incl. service identity/epoch,
binding session, contract-lock hash, diagnostic marker) and enforces the
RULING 02 R2.4 Windows protection domain through real security descriptors:
the parent pairing directory is tightened and verified (protected DACL,
current-user owner and sole trustee) BEFORE any secret byte is written, file
creation is exclusive (existing service-side source files are refused unread),
and the file itself ends protected and re-verified. The ACL seam is
self-contained in-process Win32 (advapi32) - no shell, no subprocess window,
paths and the caller's own SID only, never a secret value - and fails closed
whenever the platform cannot prove the DACL.

X01-S/X02-S fix (NE1-ARB-LM-X01-001, 2026-09-17): the service-instance id
has exactly one legal form, ``execution-`` + 16 lowercase hex chars
(RULING X01-01); ``write_pairing_file`` enforces it with a dedicated regex
before any path/ACL/secret work while the generic opaque-identifier check
(and with it the frozen Binding/session semantics) stays unchanged. RULING
X02-01: ``_decode_request`` additionally rejects a decoded non-null
``service_epoch`` below ``INITIAL_SERVICE_EPOCH`` as a typed 400
``INVALID_REQUEST`` (the frozen error response can never echo such a value,
which previously turned epoch 0 into a 500); null-epoch handshake semantics
and 409 ``EPOCH_MISMATCH`` for a positive wrong epoch are unchanged.

NE1 arming mechanism (NE1-ARB-ARMING-001, ARM-LM-S, 2026-09-18): the service
release identifier is ``ne1-service-20260918-arming-v1``. The
``/execution/v1/control`` PARAMS_CONFIRMED branch now runs the RULING 03
AR3.3 real validation chain instead of comparing only the confirmation
binding: transport authentication/session/generation/service-epoch checks
still run first (unchanged ingress order), then the confirmation target hash
must equal the message hash, ``adapter_sha256`` must match the approved
release anchored by the frozen ``ArmingPrerequisites``, the time window must
be valid and unexpired, and the binding must sit under an isolated
``TrustedControlAssembly`` (NATIVE ControlState whose registered permit and
authorization are re-verified against the authoritative service registry:
scope, target hash, revision, origin, evidence registration/expiry, action/
volume/price subsets, the S4 caps send=1 and cancel 0|1, and persistent
attempt consumption). The default DISABLED production assembly is never
promoted by this handler: without a trusted assembly and prerequisites the
confirmation is BLOCKED. The service keeps the per-epoch confirmed/revoked
arming lifecycle (GATE_CLOSED/STOPPED latch it one-way; a later
PARAMS_CONFIRMED can never clear the latch), native poll/ready refuse new
issuance without a valid confirmation of this service epoch, identical
logical confirmations replay the same recorded fact while a same-id
different-content confirmation is rejected, arming requests never
short-circuit through the HTTP replay caches (every retransmission re-passes
current control validity), and the positive ACK is exact (confirmation id,
target hash, binding, epoch, control revision echo; readiness carries the
verified permit id while can_submit/can_cancel_owned stay false).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import socket
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Full, Queue

from . import contracts as c
from . import native_contracts as nc
from .contracts import ContractError
from .dispatch_journal import DispatchJournal
from .executor import Executor
from .ledger import ActorRecord, Ledger, LedgerError, SubmitResult
from .native import NativeExecutionError
from .native_evidence import EvidenceRegistry, Origin

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 17779
DEFAULT_ACTOR_QUEUE = 16
DEFAULT_ACTOR_TIMEOUT_SECONDS = 2.5

# NE1 pairing last-mile (RULING 02, LM-S): service release identity and the
# frozen private pairing-file contract. The version string is recorded in the
# execution_started/service-start line; the schema string is written verbatim
# into every <instance_id>.token file.
SERVICE_VERSION = "ne1-service-20260918-arming-v1"
SERVICE_PAIRING_SCHEMA_VERSION = "ne1_service_pairing_v2"
DIAGNOSTIC_INSTANCE_ID = "unregistered"
_SERVICE_PAIRING_FIELDS = (
    "schema_version", "instance_id", "session_epoch", "service_instance_id",
    "service_epoch", "contract_lock_sha256", "diagnostic_only", "token",
)

# NE1 arming mechanism (NE1-ARB-ARMING-001 RULING 03 AR3.3 / RULING 05 AR5.3):
# the frozen non-secret S4 prerequisites anchor schema and its strict field
# set. The file is an audit/stage evidence anchor only - never an
# authorization, permit or mode upgrade path.
ARMING_PREREQUISITES_SCHEMA_VERSION = "ne1_arming_prerequisites_v1"
ARMING_PREREQUISITES_MAX_BYTES = 64 * 1024
_PREREQUISITES_FIELDS = (
    "schema_version", "prerequisites_id", "created_at", "expires_at",
    "contract_lock_sha256", "client_file_sha256", "service_file_sha256",
    "tool_file_sha256", "evidence_refs",
)
_PREREQUISITES_KINDS = frozenset({
    "RELEASE_REVIEW", "S3_ZERO_NATIVE", "MANUAL_TAKEOVER_APPROVAL",
    "SINGLE_ORDER_APPROVAL", "NATIVE_ASSEMBLY_ACCEPTANCE",
})

_ROUTES = frozenset({
    "/execution/v1/poll", "/execution/v1/ready", "/execution/v1/results",
    "/execution/v1/events", "/execution/v1/control", "/execution/v1/status",
})
_STATUS_PATH = "/execution/v1/status"
_POST_ROUTES = _ROUTES - {_STATUS_PATH}
_ROUTE_TYPES = {
    "/execution/v1/poll": nc.PollRequest,
    "/execution/v1/ready": nc.ReadyRequest,
    "/execution/v1/results": nc.ResultsRequest,
    "/execution/v1/events": nc.EventBatch,
    "/execution/v1/control": nc.ControlReport,
}
_ENVELOPE_KEYS = {"schema_version", "type", "data"}

_LOGGER = logging.getLogger("ak_trading.infiny_execution.client_service")

# Frozen vocabulary projection for undeclared internal codes (module docstring).
_EXTRA_BLOCKERS = {
    "CONTEXT_REQUIRED": "AUTHORIZATION_REQUIRED",
    "CONTROL_STATE_REQUIRED": "AUTHORIZATION_REQUIRED",
    "CONTROL_STATE_MISMATCH": "AUTHORIZATION_REQUIRED",
    "OFFER_UNKNOWN": "AUTHORIZATION_REQUIRED",
    "OFFER_MISMATCH": "AUTHORIZATION_REQUIRED",
    "PARAMS_CHANGED": "AUTHORIZATION_REQUIRED",
    "NATIVE_PERMIT_UNREGISTERED": "NATIVE_DISABLED",
    "NATIVE_PERMIT_EXPIRED": "NATIVE_DISABLED",
    "DISPATCH_JOURNAL_REQUIRED": "MODE_DISABLED",
    "GATE_CLOSED": "MODE_DISABLED",
    "ATTEMPT_EXISTS": "INFLIGHT_LIMIT",
    "PERMIT_EXHAUSTED": "AUTHORIZATION_ACTION",
    "BLOCKED_DISPATCH": "RECONCILE_REQUIRED",
    "GENERATION_CHANGED": "SESSION_CHANGED",
    "DISPATCH_EXPIRED": "EXPIRED",
    # NE1-ARB-ARMING-001 AR3.3: internal arming window failure; the frozen
    # transport error enum and blocker vocabulary stay untouched.
    "ARMING_EXPIRED": "EXPIRED",
}
_EVENT_CONFLICT_BLOCKERS = frozenset({
    "EVENT_CONFLICT", "EVENT_STREAM_MIXED", "EVENT_STREAM_CONFLICT", "EVENT_NOT_FOUND",
})
_HEX = frozenset("0123456789abcdef")
_HEX64 = re.compile(r"[0-9a-f]{64}")
# X01-01 (NE1-ARB-LM-X01-001): the sole legal service-instance id format is
# exactly "execution-" followed by 16 lowercase ASCII hex characters (26 chars
# total, case-sensitive, leading zeros kept). No trim, casefold, prefixing,
# truncation or UUID parsing is ever applied; bare hex32, execution-<32hex>,
# other prefixes and arbitrary opaque strings are all rejected as service
# identities. This is a dedicated service-id rule only - the generic opaque
# identifier check below stays untouched (Binding/session frozen semantics).
_SERVICE_INSTANCE_ID = re.compile(r"execution-[0-9a-f]{16}")
_OPAQUE_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}")
# a Binding instance_id must additionally form one safe path component:
# no separators, no drive colon, no reserved device basename, no trailing dot
_SAFE_FILE_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_WINDOWS_DEVICE_BASENAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)})
_SENT_AT_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(\.[0-9]{1,6})?(Z|[+-][0-9]{2}:[0-9]{2})")
# strict UTC ISO for the arming prerequisites anchor (zero offset only)
_STRICT_UTC_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(\.[0-9]{1,6})?(Z|\+00:00)")
_HEX32 = re.compile(r"[0-9a-f]{32}")


class ServiceTransportError(Exception):
    """Typed transport failure carrying the frozen code and resolution stage."""

    def __init__(self, code: nc.TransportErrorCode, *, retry_after_ms: int | None = None,
                 request: nc.TransportMessage | None = None,
                 binding: c.Binding | None = None) -> None:
        super().__init__(code.value)
        self.code = code
        self.retry_after_ms = retry_after_ms
        self.request = request
        self.binding = binding


def generate_pairing_token() -> str:
    """Fresh 256-bit pairing token, fixed lowercase-hex wire form (RULING 10)."""
    return secrets.token_hex(nc.PAIRING_TOKEN_BYTES)


# ---------------------------------------------------------------------------
# Windows DACL primitives (RULING 02 R2.4): real security descriptors, no shell
# ---------------------------------------------------------------------------

class _WindowsACL:
    """Real Windows security-descriptor operations via advapi32, in-process.

    No shell, no subprocess, no window: explicit Win32 argument lists carrying
    only paths and the caller's own SID, never a secret value. ``apply_*``
    installs a PROTECTED DACL whose only allowed trustee is the given SID
    (object+container inheritance on directories, none on files); ``verify_*``
    fails closed on any deviation: foreign owner, NULL/missing DACL, DACL not
    protected from inheritance, unexpected ACE types, or any trustee other
    than the current user. Construction fails on non-Windows platforms, which
    is the required fail-closed stance wherever DACL support cannot be proven.
    """

    _TOKEN_QUERY = 0x0008
    _TOKEN_USER = 1
    _OWNER_SECURITY_INFORMATION = 0x00000001
    _DACL_SECURITY_INFORMATION = 0x00000004
    _PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
    _SE_FILE_OBJECT = 1
    _ACL_REVISION = 2
    _ACL_SIZE_INFORMATION = 2
    _FILE_ALL_ACCESS = 0x001F01FF
    _SUB_CONTAINERS_AND_OBJECTS_INHERIT = 0x0003
    _SE_DACL_PROTECTED = 0x1000
    _ACCESS_ALLOWED_ACE_TYPE = 0
    _ERROR_SUCCESS = 0

    def __init__(self) -> None:
        if os.name != "nt":  # pragma: no cover - deployment host is Windows
            raise OSError("DACL_PLATFORM_UNSUPPORTED")
        import ctypes
        import ctypes.wintypes as wt
        self._ctypes = ctypes
        self._wt = wt
        void_p = ctypes.c_void_p
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._GetCurrentProcess = kernel32.GetCurrentProcess
        self._GetCurrentProcess.restype = wt.HANDLE
        self._CloseHandle = kernel32.CloseHandle
        self._CloseHandle.argtypes = (wt.HANDLE,)
        self._LocalFree = kernel32.LocalFree
        self._LocalFree.argtypes = (void_p,)
        self._LocalFree.restype = void_p
        self._OpenProcessToken = advapi32.OpenProcessToken
        self._OpenProcessToken.argtypes = (wt.HANDLE, wt.DWORD,
                                           ctypes.POINTER(wt.HANDLE))
        self._GetTokenInformation = advapi32.GetTokenInformation
        self._GetTokenInformation.argtypes = (
            wt.HANDLE, wt.DWORD, void_p, wt.DWORD, ctypes.POINTER(wt.DWORD))
        self._ConvertSidToStringSidW = advapi32.ConvertSidToStringSidW
        self._ConvertSidToStringSidW.argtypes = (void_p,
                                                 ctypes.POINTER(wt.LPWSTR))
        self._ConvertStringSidToSidW = advapi32.ConvertStringSidToSidW
        self._ConvertStringSidToSidW.argtypes = (wt.LPCWSTR,
                                                 ctypes.POINTER(void_p))
        self._EqualSid = advapi32.EqualSid
        self._EqualSid.argtypes = (void_p, void_p)
        self._InitializeAcl = advapi32.InitializeAcl
        self._InitializeAcl.argtypes = (void_p, wt.DWORD, wt.DWORD)
        self._AddAccessAllowedAceEx = advapi32.AddAccessAllowedAceEx
        self._AddAccessAllowedAceEx.argtypes = (void_p, wt.DWORD, wt.DWORD,
                                                wt.DWORD, void_p)
        self._SetNamedSecurityInfoW = advapi32.SetNamedSecurityInfoW
        self._SetNamedSecurityInfoW.argtypes = (wt.LPCWSTR, ctypes.c_int,
                                                wt.DWORD, void_p, void_p,
                                                void_p, void_p)
        self._SetNamedSecurityInfoW.restype = wt.DWORD
        self._GetNamedSecurityInfoW = advapi32.GetNamedSecurityInfoW
        self._GetNamedSecurityInfoW.argtypes = (
            wt.LPCWSTR, ctypes.c_int, wt.DWORD, ctypes.POINTER(void_p),
            ctypes.POINTER(void_p), ctypes.POINTER(void_p),
            ctypes.POINTER(void_p), ctypes.POINTER(void_p))
        self._GetNamedSecurityInfoW.restype = wt.DWORD
        self._GetSecurityDescriptorControl = advapi32.GetSecurityDescriptorControl
        self._GetSecurityDescriptorControl.argtypes = (
            void_p, ctypes.POINTER(wt.WORD), ctypes.POINTER(wt.DWORD))
        self._GetAclInformation = advapi32.GetAclInformation
        self._GetAclInformation.argtypes = (void_p, void_p, wt.DWORD,
                                            ctypes.c_int)
        self._GetAce = advapi32.GetAce
        self._GetAce.argtypes = (void_p, wt.DWORD, ctypes.POINTER(void_p))

    # -- helpers -------------------------------------------------------------

    def _ace_header(self, ace_pointer):
        ctypes = self._ctypes

        class _ACE_HEADER(ctypes.Structure):
            _fields_ = [("AceType", ctypes.c_ubyte),
                        ("AceFlags", ctypes.c_ubyte),
                        ("AceSize", ctypes.c_uint16)]

        return ctypes.cast(ace_pointer, ctypes.POINTER(_ACE_HEADER)).contents

    def _sid_from_string(self, sid: str):
        """PSID owned by the caller; release with LocalFree."""
        ctypes = self._ctypes
        pointer = ctypes.c_void_p()
        if not self._ConvertStringSidToSidW(sid, ctypes.byref(pointer)):
            raise OSError("DACL_SID_PARSE_FAILED")
        return pointer

    def current_user_sid(self) -> str:
        """SDDL string form of the process token's user SID (not a secret)."""
        ctypes = self._ctypes
        handle = self._wt.HANDLE()
        if not self._OpenProcessToken(self._GetCurrentProcess(),
                                      self._TOKEN_QUERY, ctypes.byref(handle)):
            raise OSError("DACL_TOKEN_OPEN_FAILED")
        try:
            needed = self._wt.DWORD(0)
            self._GetTokenInformation(handle, self._TOKEN_USER, None, 0,
                                      ctypes.byref(needed))
            buffer = ctypes.create_string_buffer(max(needed.value, 64))
            if not self._GetTokenInformation(
                    handle, self._TOKEN_USER, buffer, needed.value,
                    ctypes.byref(needed)):
                raise OSError("DACL_TOKEN_READ_FAILED")
            # TOKEN_USER starts with SID_AND_ATTRIBUTES.Sid (pointer first)
            sid = ctypes.cast(buffer,
                              ctypes.POINTER(ctypes.c_void_p)).contents
            text = self._wt.LPWSTR()
            if not self._ConvertSidToStringSidW(sid, ctypes.byref(text)):
                raise OSError("DACL_SID_CONVERT_FAILED")
            try:
                return str(text.value)
            finally:
                self._LocalFree(ctypes.cast(text, ctypes.c_void_p))
        finally:
            self._CloseHandle(handle)

    # -- operations ----------------------------------------------------------

    def apply_protected_dacl(self, path: Path, *, sid: str,
                             directory: bool) -> None:
        """Replace the object's DACL with a protected current-user-only ACL."""
        ctypes = self._ctypes
        sid_pointer = self._sid_from_string(sid)
        try:
            acl = ctypes.create_string_buffer(512)
            if not self._InitializeAcl(acl, 512, self._ACL_REVISION):
                raise OSError("DACL_ACL_INIT_FAILED")
            inheritance = (self._SUB_CONTAINERS_AND_OBJECTS_INHERIT
                           if directory else 0)
            if not self._AddAccessAllowedAceEx(
                    acl, self._ACL_REVISION, inheritance,
                    self._FILE_ALL_ACCESS, sid_pointer):
                raise OSError("DACL_ACE_ADD_FAILED")
            flags = (self._DACL_SECURITY_INFORMATION
                     | self._PROTECTED_DACL_SECURITY_INFORMATION)
            code = self._SetNamedSecurityInfoW(
                str(path), self._SE_FILE_OBJECT, flags, None, None, acl, None)
            if code != self._ERROR_SUCCESS:
                raise OSError(code, "DACL_APPLY_FAILED")
        finally:
            self._LocalFree(sid_pointer)

    def verify_private(self, path: Path, *, sid: str) -> None:
        """Fail closed unless owner and every allowed trustee are ``sid``."""
        ctypes = self._ctypes
        expected = self._sid_from_string(sid)
        try:
            owner = ctypes.c_void_p()
            dacl = ctypes.c_void_p()
            descriptor = ctypes.c_void_p()
            flags = (self._OWNER_SECURITY_INFORMATION
                     | self._DACL_SECURITY_INFORMATION)
            code = self._GetNamedSecurityInfoW(
                str(path), self._SE_FILE_OBJECT, flags, ctypes.byref(owner),
                None, ctypes.byref(dacl), None, ctypes.byref(descriptor))
            if code != self._ERROR_SUCCESS:
                raise OSError(code, "DACL_READ_FAILED")
            try:
                if not owner.value or not self._EqualSid(owner, expected):
                    raise OSError("DACL_OWNER_MISMATCH")
                if not dacl.value:
                    # a NULL DACL would grant everyone: never acceptable
                    raise OSError("DACL_MISSING")
                control = self._wt.WORD()
                revision = self._wt.DWORD()
                if not self._GetSecurityDescriptorControl(
                        descriptor, ctypes.byref(control),
                        ctypes.byref(revision)):
                    raise OSError("DACL_CONTROL_UNREADABLE")
                if not control.value & self._SE_DACL_PROTECTED:
                    raise OSError("DACL_INHERITANCE_UNPROTECTED")
                info = ctypes.create_string_buffer(12)
                if not self._GetAclInformation(dacl, info, 12,
                                               self._ACL_SIZE_INFORMATION):
                    raise OSError("DACL_ACE_COUNT_UNREADABLE")
                ace_count = ctypes.cast(info,
                                        ctypes.POINTER(self._wt.DWORD))[0]
                if ace_count < 1:
                    raise OSError("DACL_EMPTY")
                for index in range(ace_count):
                    ace = ctypes.c_void_p()
                    if not self._GetAce(dacl, index, ctypes.byref(ace)):
                        raise OSError("DACL_ACE_UNREADABLE")
                    header = self._ace_header(ace)
                    if header.AceType != self._ACCESS_ALLOWED_ACE_TYPE:
                        raise OSError("DACL_UNEXPECTED_ACE_TYPE")
                    # ACCESS_ALLOWED_ACE: header (4 bytes) + mask (4 bytes)
                    base = ctypes.cast(ace, ctypes.c_void_p).value
                    ace_sid = ctypes.c_void_p(base + 8)
                    if not self._EqualSid(ace_sid, expected):
                        raise OSError("DACL_FOREIGN_TRUSTEE")
            finally:
                if descriptor.value:
                    self._LocalFree(descriptor)
        finally:
            self._LocalFree(expected)


_WINDOWS_ACL_TOOLKIT: _WindowsACL | None = None


def _windows_acl() -> _WindowsACL:
    """Process-wide lazy toolkit; a construction failure is never cached."""
    global _WINDOWS_ACL_TOOLKIT
    if _WINDOWS_ACL_TOOLKIT is None:
        _WINDOWS_ACL_TOOLKIT = _WindowsACL()
    return _WINDOWS_ACL_TOOLKIT


def secure_pairing_directory(directory: str | Path) -> str:
    """Create-if-needed, tighten and verify the private pairing directory.

    The protection domain is established BEFORE any secret byte is written:
    protected DACL (inheritance cut), the actual Windows current-user SID as
    owner and sole allowed trustee. Any deviation - foreign owner or trustee,
    inheritable DACL, or a platform whose ACL support cannot be proven -
    raises OSError so callers fail closed instead of downgrading to chmod.
    Returns the verified SID string (paths and SIDs only, no secrets).
    """
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    if os.path.islink(target) or not target.is_dir():
        raise OSError("PAIRING_DIR_INVALID")
    toolkit = _windows_acl()
    sid = toolkit.current_user_sid()
    toolkit.apply_protected_dacl(target, sid=sid, directory=True)
    toolkit.verify_private(target, sid=sid)
    return sid


def _require_opaque(value, name: str) -> None:
    if not (type(value) is str and _OPAQUE_IDENTIFIER.fullmatch(value)):
        raise ValueError(f"{name} must be an opaque identifier")


def _require_safe_instance_filename(instance_id) -> None:
    """A binding instance_id used as ``<instance>.token`` must be one safe
    path component: separators, drive colons, device names and trailing
    dots are all rejected (RULING 02 R2.2)."""
    if not (type(instance_id) is str
            and _SAFE_FILE_COMPONENT.fullmatch(instance_id)):
        raise ValueError("instance_id must form a safe single file name")
    if instance_id.split(".")[0].upper() in _WINDOWS_DEVICE_BASENAMES:
        raise ValueError("instance_id must not be a reserved device name")
    if instance_id.endswith((".", " ")):
        raise ValueError("instance_id must not end with a dot or space")


def write_pairing_file(
    path: str | Path, instance_id: str, token: str, *,
    service_instance_id: str, service_epoch: int,
    session_epoch: str | None, contract_lock_sha256: str,
) -> Path:
    """Write one private ``ne1_service_pairing_v2`` credential file (RULING 02).

    Strict single JSON object with exactly the frozen fields. Every field is
    validated (token stays 256-bit lowercase hex, service_instance_id must be
    exactly ``execution-`` + 16 lowercase hex chars - the one-legal-format
    service start identity, X01-01 - service_epoch a positive
    non-bool int, contract lock a sha256 hex, normal actors carry their
    registered binding session while only ``instance_id="unregistered"``
    takes ``session_epoch=None`` with ``diagnostic_only=true``), the
    instance must form a safe single file name, and the parent directory is
    tightened AND verified before any secret byte exists on disk. Creation
    is exclusive: an existing service-side source file is refused without
    being read or overwritten. After flush/fsync the file itself receives a
    protected current-user-only DACL and is re-verified. Error messages
    never contain the token value.
    """
    _require_safe_instance_filename(instance_id)
    if not (type(token) is str and len(token) == nc.PAIRING_TOKEN_HEX_LENGTH
            and all(char in _HEX for char in token)):
        raise ValueError("pairing token must be fixed-length lowercase hex")
    # X01-01: dedicated exact-format service id check. Rejected here, before
    # any path resolution, ACL work or secret byte reaches the disk.
    if not (type(service_instance_id) is str
            and _SERVICE_INSTANCE_ID.fullmatch(service_instance_id)):
        raise ValueError(
            "service_instance_id must be execution-<16 lowercase hex>")
    if type(service_epoch) is not int \
            or service_epoch < nc.INITIAL_SERVICE_EPOCH:
        raise ValueError("service_epoch must be a positive integer, not bool")
    if not (type(contract_lock_sha256) is str
            and _HEX64.fullmatch(contract_lock_sha256)):
        raise ValueError("contract_lock_sha256 must be sha256 hex")
    diagnostic_only = instance_id == DIAGNOSTIC_INSTANCE_ID
    if diagnostic_only:
        if session_epoch is not None:
            raise ValueError("diagnostic pairing must not carry a session epoch")
    elif type(session_epoch) is not str:
        raise ValueError("registered pairing requires the binding session epoch")
    else:
        _require_opaque(session_epoch, "session_epoch")
    target = Path(path)
    toolkit = _windows_acl()
    sid = secure_pairing_directory(target.parent)
    if target.exists() or os.path.islink(target):
        # never read, never overwrite a service-side source file
        raise FileExistsError("PAIRING_FILE_EXISTS")
    document = {name: None for name in _SERVICE_PAIRING_FIELDS}
    document.update({
        "schema_version": SERVICE_PAIRING_SCHEMA_VERSION,
        "instance_id": instance_id,
        "session_epoch": session_epoch,
        "service_instance_id": service_instance_id,
        "service_epoch": service_epoch,
        "contract_lock_sha256": contract_lock_sha256,
        "diagnostic_only": diagnostic_only,
        "token": token,
    })
    handle = open(target, "x", encoding="ascii", newline="\n")
    try:
        handle.write(json.dumps(document, ensure_ascii=True,
                                separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    # born private through the protected parent (its inheritable ACE is the
    # only access grant); now protect the file's own DACL and re-verify
    toolkit.apply_protected_dacl(target, sid=sid, directory=False)
    toolkit.verify_private(target, sid=sid)
    return target


# ---------------------------------------------------------------------------
# NE1 arming mechanism (NE1-ARB-ARMING-001 RULING 03 AR3.3 / RULING 05 AR5.3)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ArmingPrerequisites:
    """Frozen non-secret S4 arming anchor loaded once at service start.

    The bytes hash is fixed at load time; ``client_file_sha256`` is the
    approved client/adapter release hash the PARAMS_CONFIRMED chain compares
    ``adapter_sha256`` against (the service's independent version anchor -
    never the client's self-reported hash). This record is an evidence
    anchor only: it grants no authorization, permit or mode upgrade, and its
    absence leaves the service running disabled with arming BLOCKED.
    """
    prerequisites_id: str
    prerequisites_sha256: str
    client_file_sha256: str
    service_file_sha256: str
    tool_file_sha256: str
    contract_lock_sha256: str
    created_at: datetime
    expires_at: datetime
    evidence_refs: tuple[tuple[str, str, str], ...]


def _prerequisites_time(value, name: str) -> datetime:
    if not (type(value) is str and _STRICT_UTC_PATTERN.fullmatch(value)):
        raise ValueError(f"ARMING_READINESS_TIME:{name}")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() != timedelta(0):
        raise ValueError(f"ARMING_READINESS_TIME:{name}")
    return parsed


def load_arming_prerequisites(
    path: str | Path, *, contract_lock_sha256: str, service_file_sha256: str,
    now: datetime,
) -> ArmingPrerequisites:
    """Strictly load and verify one ``ne1_arming_prerequisites_v1`` file.

    Every deviation - unreadable/redirected path, size cap, strict UTF-8
    single JSON object, duplicate keys, unknown or missing fields, non-hex
    hashes, non-strict-UTC times, missing evidence kind coverage, contract
    lock mismatch, service-file hash mismatch, expiry - raises ValueError
    with a stable reason; the caller keeps serving under the original
    disabled rules with production arming permanently BLOCKED (AR3.3).
    """
    target = Path(path)
    if target.is_symlink() or not target.is_file():
        raise ValueError("ARMING_READINESS_UNREADABLE")
    raw = target.read_bytes()
    if len(raw) > ARMING_PREREQUISITES_MAX_BYTES:
        raise ValueError("ARMING_READINESS_TOO_LARGE")
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_pairs,
                              parse_constant=_nonfinite)
    except (UnicodeError, ValueError, RecursionError):
        raise ValueError("ARMING_READINESS_MALFORMED") from None
    if not isinstance(document, dict) or set(document) != set(_PREREQUISITES_FIELDS):
        raise ValueError("ARMING_READINESS_FIELDS")
    if document["schema_version"] != ARMING_PREREQUISITES_SCHEMA_VERSION:
        raise ValueError("ARMING_READINESS_SCHEMA")
    if not (type(document["prerequisites_id"]) is str
            and _HEX32.fullmatch(document["prerequisites_id"])):
        raise ValueError("ARMING_READINESS_ID")
    for name in ("contract_lock_sha256", "client_file_sha256",
                 "service_file_sha256", "tool_file_sha256"):
        if not (type(document[name]) is str and _HEX64.fullmatch(document[name])):
            raise ValueError(f"ARMING_READINESS_HASH:{name}")
    created_at = _prerequisites_time(document["created_at"], "created_at")
    expires_at = _prerequisites_time(document["expires_at"], "expires_at")
    if expires_at <= created_at:
        raise ValueError("ARMING_READINESS_WINDOW")
    refs = document["evidence_refs"]
    if type(refs) is not list or not refs:
        raise ValueError("ARMING_READINESS_REFS")
    parsed_refs: list[tuple[str, str, str]] = []
    kinds_seen: set[str] = set()
    for item in refs:
        if type(item) is not dict or set(item) != {"kind", "path", "sha256"}:
            raise ValueError("ARMING_READINESS_REFS")
        kind, ref_path, digest = item["kind"], item["path"], item["sha256"]
        if not (type(kind) is str and kind in _PREREQUISITES_KINDS):
            raise ValueError("ARMING_READINESS_REF_KIND")
        if kind in kinds_seen:
            raise ValueError("ARMING_READINESS_REF_KIND")
        kinds_seen.add(kind)
        if not (type(ref_path) is str and 0 < len(ref_path) <= 4096
                and not any(ord(ch) < 32 for ch in ref_path)):
            raise ValueError("ARMING_READINESS_REF_PATH")
        if not (type(digest) is str and _HEX64.fullmatch(digest)):
            raise ValueError("ARMING_READINESS_HASH:evidence")
        parsed_refs.append((kind, ref_path, digest))
    if kinds_seen != set(_PREREQUISITES_KINDS):
        raise ValueError("ARMING_READINESS_REF_COVERAGE")
    if document["contract_lock_sha256"] != contract_lock_sha256:
        raise ValueError("ARMING_READINESS_CONTRACT_LOCK")
    if document["service_file_sha256"] != service_file_sha256:
        raise ValueError("ARMING_READINESS_SERVICE_HASH")
    if expires_at <= now:
        raise ValueError("ARMING_READINESS_EXPIRED")
    return ArmingPrerequisites(
        prerequisites_id=document["prerequisites_id"],
        prerequisites_sha256=hashlib.sha256(raw).hexdigest(),
        client_file_sha256=document["client_file_sha256"],
        service_file_sha256=document["service_file_sha256"],
        tool_file_sha256=document["tool_file_sha256"],
        contract_lock_sha256=document["contract_lock_sha256"],
        created_at=created_at, expires_at=expires_at,
        evidence_refs=tuple(parsed_refs))


@dataclass(frozen=True, slots=True)
class TrustedControlAssembly:
    """The single in-process source of a trusted NATIVE control context.

    RULING 03 AR3.3: the PARAMS_CONFIRMED handler may only promote a
    binding that sits under an existing trusted control assembly (NATIVE
    ControlState plus its registered permit and authorization records).
    Production NEVER constructs one in this release - the launcher keeps the
    default disabled assembly (port=None/mode=DISABLED) and passes no
    assemblies, so a production PARAMS_CONFIRMED is always BLOCKED here.
    Offline SYNTHETIC trusted assemblies prove the confirmation logic in
    isolation; the future S4 owner owns the production assembler.
    """
    control_state: "nc.ControlState"
    permit: nc.NativePilotPermit
    authorization: nc.NativeAuthorization
    origin: "Origin"

    def validate(self) -> None:
        if self.control_state.mode is not c.ExecutionMode.NATIVE:
            raise ValueError("trusted control assembly requires a NATIVE control state")
        if (self.control_state.native_permit_id is None
                or self.control_state.native_permit_id != self.permit.permit_id):
            raise ValueError("assembly control state must name exactly its permit")
        if self.permit.authorization_id != self.authorization.authorization_id:
            raise ValueError("assembly permit must reference its authorization")
        if not (self.control_state.binding == self.permit.binding
                == self.authorization.binding):
            raise ValueError("assembly records must share exactly one binding")
        if self.permit.control_revision != self.control_state.control_revision:
            raise ValueError("assembly permit revision must match the control state")
        if (self.permit.origin is not self.origin
                or self.authorization.origin is not self.origin):
            raise ValueError("assembly origin must match both grant records")


@dataclass(frozen=True, slots=True)
class _ConfirmedArming:
    """One recorded arming fact of the current service epoch (AR3.3)."""
    confirmation_id: str
    params_sha256: str
    adapter_sha256: str
    permit_id: str
    control_revision: int
    confirmed_at: datetime
    expires_at: datetime


class _Clock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()


def vocab_blockers(codes) -> tuple[str, ...]:
    """Project arbitrary internal codes onto the declared blocker vocabulary.

    An empty input means "no blockers"; a nonempty input whose codes are all
    unmappable collapses to the honest production stance (MODE_DISABLED).
    """
    result: list[str] = []
    for code in codes:
        code = _EXTRA_BLOCKERS.get(code, code)
        if code in c.BLOCKER_CODES and code not in result:
            result.append(code)
    if result or not codes:
        return tuple(result)
    return ("MODE_DISABLED",)


class _ReplayCache:
    """Bounded in-memory replay index in front of the durable journal cache.

    Exact retries are served with zero journal writes (OV-F06 remediation);
    the durable journal rows remain the cross-restart replay source of truth.
    """

    def __init__(self, capacity: int = 512) -> None:
        self._capacity = capacity
        self._entries: "OrderedDict[tuple, tuple[str, str | None]]" = OrderedDict()

    def get(self, key):
        entry = self._entries.get(key)
        if entry is not None:
            self._entries.move_to_end(key)
        return entry

    def put(self, key, fingerprint: str, response: str) -> None:
        self._entries[key] = (fingerprint, response)
        self._entries.move_to_end(key)
        while len(self._entries) > self._capacity:
            self._entries.popitem(last=False)


# ---------------------------------------------------------------------------
# orchestrator seam between HTTP handlers and the N4 two-phase API
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class PollOutcome:
    kind: nc.ResponseKind
    offer: nc.ExecutionOffer | None
    blockers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReadyOutcome:
    dispatch: nc.DispatchCommand | None
    blockers: tuple[str, ...]
    context_conflict: bool = False


@dataclass(frozen=True, slots=True)
class CompleteOutcome:
    submit_result: SubmitResult | None
    not_found: bool = False


class N4BackedOrchestrator:
    """Production seam over Executor/DispatchJournal.

    Default assembly is port=None / mode=DISABLED: prepare and commit can only
    produce declared blockers and the single native call right never exists
    server-side. ``executor_factory`` is the trusted-assembler hook used by
    offline SYNTHETIC assembly; production CLI/HTTP paths never pass one.
    """

    def __init__(self, journal: Ledger, *, evidence_registry: EvidenceRegistry | None = None,
                 executor_factory: Callable[[c.Binding], Executor] | None = None,
                 intents: Callable[[c.Binding], c.OrderIntent | None] | None = None,
                 observation_factory: Callable[[c.Binding], c.ExecutionContext] | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self._journal = journal
        self._registry = evidence_registry
        self._executor_factory = executor_factory
        self._intents = intents
        # SYNTHETIC armed assembly hook: a fresh, internally consistent
        # observation context (snapshot+proof from the same source) per poll;
        # prepare still compares persisted controls and CAS-adopts it. The
        # production default stays the persisted context (disabled service).
        self._observation_factory = observation_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._executors: dict[str, Executor] = {}
        self._pending_releases: list[tuple[str, datetime]] = []
        self._lock = threading.Lock()

    def _executor(self, binding: c.Binding) -> Executor:
        key = c.canonical_hash(binding)
        with self._lock:
            found = self._executors.get(key)
            if found is not None:
                return found
            if self._executor_factory is not None:
                executor = self._executor_factory(binding)
            else:
                executor = Executor(
                    self._journal, port=None, mode=c.ExecutionMode.DISABLED,
                    binding=binding, clock=self._clock,
                    evidence_registry=self._registry,
                    dispatch_journal=DispatchJournal(self._journal,
                                                     evidence_registry=self._registry))
            self._executors[key] = executor
            return executor

    def _release_expired_unattempted(self) -> None:
        """Plan section 5.2: every unattempted offer that expired releases its
        reservation before further cycles can reserve again (several offers
        may expire unattempted in a row under load). Attempts, permits and
        reservations of consumed offers are never touched."""
        if not self._pending_releases:
            return
        now = self._clock()
        still_pending = []
        for plan_id, expires_at in self._pending_releases:
            if expires_at > now:
                still_pending.append((plan_id, expires_at))
                continue
            try:
                self._journal.release_unattempted(plan_id)
            except LedgerError:
                pass  # attempted or already released: the journal decides
        self._pending_releases = still_pending

    def poll(self, binding: c.Binding, request: nc.PollRequest) -> PollOutcome:
        executor = self._executor(binding)
        self._release_expired_unattempted()
        if self._observation_factory is not None:
            # the observation factory publishes its intent to the provider
            # before the intent lookup, so both derive from one source
            context = self._observation_factory(binding)
        else:
            context = self._journal.context(binding)
        intent = self._intents(binding) if self._intents is not None else None
        if intent is None:
            return PollOutcome(nc.ResponseKind.BLOCKED, None, ("INTENT_REQUIRED",))
        if context is None:
            return PollOutcome(nc.ResponseKind.BLOCKED, None, ("AUTHORIZATION_REQUIRED",))
        # consecutive cycles: the served intent travels WITH the observation
        # (N4's deadline derivation reads context.intent); prepare re-registers
        # and CAS-adopts the full observation under persisted control
        # comparison, and reservation slots reset for the fresh operation.
        context = replace(context, intent=intent, reservation_receipt=None,
                          reservation_view=None)
        try:
            offer = executor.prepare_native_offer(intent, context)
        except (NativeExecutionError, LedgerError) as exc:
            codes = exc.args if isinstance(exc, NativeExecutionError) else exc.blockers
            return PollOutcome(nc.ResponseKind.BLOCKED, None, vocab_blockers(codes))
        except ContractError:
            # C1.2 validators reject an offer born past its anchored deadline
            # (sample-time anchor + host write latency): fail closed, zero writes
            return PollOutcome(nc.ResponseKind.BLOCKED, None, ("EXPIRED",))
        if offer.binding != binding:
            return PollOutcome(nc.ResponseKind.BLOCKED, None, ("OWNERSHIP_CONFLICT",))
        self._pending_releases.append((offer.plan_id, offer.expires_at))
        return PollOutcome(nc.ResponseKind.OFFER, offer, ())

    def ready(self, binding: c.Binding, request: nc.ReadyRequest) -> ReadyOutcome:
        executor = self._executor(binding)
        context = self._journal.context(binding)
        if context is None:
            return ReadyOutcome(None, ("AUTHORIZATION_REQUIRED",))
        try:
            command = executor.commit_native_offer(request.offer_id, request.challenge, context)
        except NativeExecutionError as exc:
            conflict = bool(exc.args) and exc.args[0] == "CONTROL_REVISION_MISMATCH"
            return ReadyOutcome(None, vocab_blockers(exc.args), context_conflict=conflict)
        except LedgerError as exc:
            conflict = exc.code is c.ResultCode.REVISION_CONFLICT
            return ReadyOutcome(None, vocab_blockers(exc.blockers), context_conflict=conflict)
        if command.binding != binding:
            return ReadyOutcome(None, ("OWNERSHIP_CONFLICT",))
        # the attempt is latched: its plan is no longer unattempted-releasable
        self._pending_releases = [(plan_id, expires_at) for plan_id, expires_at
                                  in self._pending_releases if plan_id != command.plan_id]
        return ReadyOutcome(command, ())

    def complete(self, receipt: nc.DispatchReceipt) -> CompleteOutcome:
        executor = self._executor(receipt.binding)
        try:
            return CompleteOutcome(executor.complete_native_dispatch(receipt))
        except LedgerError as exc:
            if exc.code is c.ResultCode.NOT_FOUND:
                return CompleteOutcome(None, not_found=True)
            raise


# ---------------------------------------------------------------------------
# durable service-owned production evidence registry (RULING 05)
# ---------------------------------------------------------------------------

class ProductionServiceRegistry(EvidenceRegistry):
    """File-backed production registry fed only by stopped-service operator CLI.

    A registration is operator input verified here (record hash, lock hash,
    origin=production, structural/temporal validators from native_evidence);
    N3 stores route/settlement copies journal-side when the record type has a
    journal seam. ``TestEvidenceFactory`` output can never enter: SYNTHETIC
    origin is refused on every register call.
    """

    _DIRS = {
        "NativeBaselineEvidence": "baseline",
        "NativeRouteEvidence": "route",
        "NativeSettlementEvidence": "settlement",
        "ClientThreadEvidence": "thread",
        "NativeAuthorization": "authorization",
        "NativePilotPermit": "permit",
    }
    _IDS = {
        "NativeBaselineEvidence": "evidence_id",
        "NativeRouteEvidence": "evidence_id",
        "NativeSettlementEvidence": "evidence_id",
        "ClientThreadEvidence": "evidence_id",
        "NativeAuthorization": "authorization_id",
        "NativePilotPermit": "permit_id",
    }

    def __init__(self, registry_dir: str | Path | None = None) -> None:
        self._root = Path(registry_dir) if registry_dir is not None else None
        self._lock = threading.RLock()
        self._records: dict[str, tuple[str, c.ContractData]] = {}
        if self._root is not None:
            self._load()

    def _types(self) -> dict[str, type]:
        from .native_evidence import (
            ClientThreadEvidence, NativeBaselineEvidence, NativeRouteEvidence,
            NativeSettlementEvidence,
        )
        return {
            "NativeBaselineEvidence": NativeBaselineEvidence,
            "NativeRouteEvidence": NativeRouteEvidence,
            "NativeSettlementEvidence": NativeSettlementEvidence,
            "ClientThreadEvidence": ClientThreadEvidence,
            "NativeAuthorization": nc.NativeAuthorization,
            "NativePilotPermit": nc.NativePilotPermit,
        }

    def _load(self) -> None:
        types = self._types()
        if self._root is None or not self._root.exists():
            return
        for kind, folder in self._DIRS.items():
            directory = self._root / folder
            if not directory.exists():
                continue
            for file in sorted(directory.glob("*.json")):
                record = c.decode(file.read_text(encoding="utf-8"), types[kind],
                                  schema_version=c.V2_SCHEMA_VERSION)
                self._records[self._key(kind, record)] = (kind, record)

    def _key(self, kind: str, record) -> str:
        return kind + ":" + getattr(record, self._IDS[kind])

    def _require_production(self, record) -> None:
        origin = getattr(record, "origin", None)
        if origin is not None and origin is not Origin.PRODUCTION:
            raise ContractError("SYNTHETIC records can never be registered as production")

    def _store(self, kind: str, record) -> None:
        from .native_evidence import validate_baseline, validate_route, validate_settlement
        self._require_production(record)
        if kind == "NativeBaselineEvidence":
            if not validate_baseline(record, now=datetime.now(timezone.utc)).ok:
                raise ContractError("baseline evidence expired")
        elif kind == "NativeRouteEvidence":
            if not validate_route(record, now=datetime.now(timezone.utc)).ok:
                raise ContractError("route evidence expired")
        elif kind == "NativeSettlementEvidence":
            if not validate_settlement(record, now=datetime.now(timezone.utc)).ok:
                raise ContractError("settlement evidence invalid")
        elif kind == "NativePilotPermit":
            self._validate_permit_dependencies(record)
        key = self._key(kind, record)
        with self._lock:
            existing = self._records.get(key)
            if existing is not None:
                if c.canonical_hash(existing[1]) != c.canonical_hash(record):
                    raise ContractError("conflicting re-registration of an immutable record")
                return
            if self._root is not None:
                folder = self._root / self._DIRS[kind]
                folder.mkdir(parents=True, exist_ok=True)
                identity = getattr(record, self._IDS[kind])
                (folder / (identity + ".json")).write_text(
                    c.encode(record, schema_version=c.V2_SCHEMA_VERSION), encoding="utf-8")
            self._records[key] = (kind, record)

    def _validate_permit_dependencies(self, permit) -> None:
        if ("NativeAuthorization:" + permit.authorization_id) not in self._records:
            raise ContractError("permit references an unregistered authorization")
        for field in ("baseline_evidence_id", "route_evidence_id", "cash_bound_evidence_id"):
            identity = getattr(permit, field)
            if not any(key.endswith(":" + identity) for key in self._records):
                raise ContractError(f"permit references an unregistered record: {field}")

    def register_baseline(self, evidence) -> None:
        self._store("NativeBaselineEvidence", evidence)

    def register_route(self, evidence) -> None:
        self._store("NativeRouteEvidence", evidence)

    def register_thread(self, evidence) -> None:
        self._store("ClientThreadEvidence", evidence)

    def register_settlement(self, evidence) -> None:
        self._store("NativeSettlementEvidence", evidence)

    def register_permit(self, permit) -> None:
        self._store("NativePilotPermit", permit)

    def register_authorization(self, authorization) -> None:
        self._store("NativeAuthorization", authorization)

    def verify_permit_active(self, permit_id: str, *, now: datetime) -> c.ValidationResult:
        record = self._records.get("NativePilotPermit:" + permit_id)
        if record is None:
            return c.ValidationResult(blockers=("NATIVE_PERMIT_UNREGISTERED",))
        if record[1].expires_at <= now:
            return c.ValidationResult(blockers=("NATIVE_PERMIT_EXPIRED",))
        return c.ValidationResult(blockers=())

    def record_by_id(self, kind: str, identity: str):
        """Read one registered record; the registry is the authority (AR3.3).

        Grant copies submitted by tools are pre-checks only - this immutable
        registered state (offline register-* under journal maintenance) is
        what the PARAMS_CONFIRMED chain consults. Returns None when absent.
        """
        with self._lock:
            found = self._records.get(kind + ":" + identity)
        return found[1] if found is not None else None

    def permit_record(self, permit_id: str) -> nc.NativePilotPermit | None:
        record = self.record_by_id("NativePilotPermit", permit_id)
        return record if isinstance(record, nc.NativePilotPermit) else None

    def authorization_record(self, authorization_id: str) -> nc.NativeAuthorization | None:
        record = self.record_by_id("NativeAuthorization", authorization_id)
        return record if isinstance(record, nc.NativeAuthorization) else None


# ---------------------------------------------------------------------------
# fixed serial account actors (RULING 04)
# ---------------------------------------------------------------------------

class _AccountActorCall:
    __slots__ = ("done", "result", "error")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.result = None
        self.error: BaseException | None = None


class _AccountActor:
    """One fixed serial worker thread per registered account actor."""

    _SENTINEL = object()

    def __init__(self, record: ActorRecord, *, queue_size: int = DEFAULT_ACTOR_QUEUE) -> None:
        self.record = record
        self._queue: Queue = Queue(maxsize=queue_size)
        self._thread = threading.Thread(
            target=self._run, name=f"execution-actor-{record.actor_id}", daemon=True)

    def start(self) -> None:
        if not self._thread.is_alive() and not self._thread.ident:
            self._thread.start()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is self._SENTINEL:
                    return
                function, box = item
                try:
                    box.result = function()
                except BaseException as exc:  # surfaced to the waiting worker
                    box.error = exc
                finally:
                    box.done.set()
            finally:
                self._queue.task_done()

    def call(self, function: Callable[[], object], *, timeout: float) -> object:
        box = _AccountActorCall()
        try:
            self._queue.put((function, box), timeout=timeout)
        except Full:
            raise ServiceTransportError(nc.TransportErrorCode.BACKPRESSURE) from None
        if not box.done.wait(timeout):
            # The durable work may still complete; the HTTP answer stays bounded.
            raise ServiceTransportError(nc.TransportErrorCode.SERVICE_UNAVAILABLE)
        if box.error is not None:
            raise box.error
        return box.result

    def stop(self, *, timeout: float = 1.0) -> None:
        try:
            self._queue.put_nowait(self._SENTINEL)
        except Full:
            return
        self._thread.join(timeout=timeout)


# ---------------------------------------------------------------------------
# the execution service
# ---------------------------------------------------------------------------

class ExecutionService:
    """Six-route transport service over the N3 journal and N4 orchestrator."""

    def __init__(
        self,
        journal: Ledger,
        *,
        tokens: Mapping[str, str] | None = None,
        orchestrator: N4BackedOrchestrator | None = None,
        clock: object | None = None,
        audit_log: str | Path | None = None,
        poll_armed_interval_ms: int = nc.POLL_ARMED_MIN_INTERVAL_MS,
        poll_disabled_interval_ms: int = nc.POLL_DISABLED_INTERVAL_MS,
        actor_call_timeout: float = DEFAULT_ACTOR_TIMEOUT_SECONDS,
        evidence_registry: EvidenceRegistry | None = None,
        trusted_control_assemblies: Mapping[str, TrustedControlAssembly] | None = None,
        arming_prerequisites: ArmingPrerequisites | None = None,
    ) -> None:
        if type(journal) is not Ledger:
            raise TypeError("Ledger journal required")
        self.journal = journal
        self.clock = clock or _Clock()
        self.orchestrator = orchestrator or N4BackedOrchestrator(journal)
        self.poll_armed_interval_ms = int(poll_armed_interval_ms)
        self.poll_disabled_interval_ms = int(poll_disabled_interval_ms)
        self.actor_call_timeout = float(actor_call_timeout)
        self._lock = threading.RLock()
        self._audit_handle = None
        self._closed = False
        self._counters: dict[str, int] = {}
        self._last_poll_mono: dict[str, float] = {}
        self._known_events: dict[tuple[str, int], str] = {}
        self._streams_seen: dict[tuple[str, str, str], str] = {}
        self._gate_closed: dict[str, bool] = {}
        self._tokens: dict[str, str] = {}
        self._replay_cache = _ReplayCache()
        self._ingest_backlog: list = []
        # NE1 arming mechanism (AR3.3): authoritative grant registry, the
        # trusted NATIVE assembly seam (production passes none) and the
        # frozen S4 prerequisites anchor. All three default to None, so the
        # production PARAMS_CONFIRMED chain can only ever be BLOCKED until
        # the S4 owner assembles a trusted native context.
        self._evidence_registry = evidence_registry
        if arming_prerequisites is not None \
                and not isinstance(arming_prerequisites, ArmingPrerequisites):
            raise TypeError("ArmingPrerequisites required")
        self._arming_prerequisites = arming_prerequisites
        assemblies: dict[str, TrustedControlAssembly] = {}
        for key, assembly in (trusted_control_assemblies or {}).items():
            if not (type(key) is str and _HEX64.fullmatch(key)):
                raise ValueError(
                    "trusted assembly keys must be binding canonical hashes")
            if not isinstance(assembly, TrustedControlAssembly):
                raise TypeError("TrustedControlAssembly required")
            assembly.validate()
            assemblies[key] = assembly
        self._trusted_control_assemblies = assemblies
        # per-service-epoch arming lifecycle: one confirmed fact per binding,
        # one-way revocation latch (GATE_CLOSED/STOPPED); process-local by
        # design - a restart allocates a new epoch and re-arms nothing.
        self._arming_confirmed: dict[str, _ConfirmedArming] = {}
        self._arming_revoked: set[str] = set()
        for instance_id, token in (tokens or {}).items():
            self.register_pairing(instance_id, token)
        # A real service start allocates the next strictly increasing epoch.
        self.service_epoch = max(int(journal.allocate_service_epoch()),
                                 nc.INITIAL_SERVICE_EPOCH)
        self.instance_id = f"execution-{secrets.token_hex(8)}"
        records = journal.actors()
        if len(records) > nc.MAX_ACCOUNT_ACTORS:
            raise ValueError("registered account actors exceed MAX_ACCOUNT_ACTORS")
        self._actor_list: list[_AccountActor] = []
        self._instances: dict[str, tuple[_AccountActor, c.Binding]] = {}
        armed_native = 0
        for record in records:
            if not record.enabled:
                continue
            actor = _AccountActor(record)
            self._actor_list.append(actor)
            for binding in record.bindings:
                if binding.instance_id in self._instances:
                    raise ValueError("duplicate registered instance identity")
                self._instances[binding.instance_id] = (actor, binding)
            if self._context_mode(record) is c.ExecutionMode.NATIVE:
                armed_native += 1
        if armed_native > nc.MAX_ARMED_NATIVE_ACCOUNTS:
            raise ValueError("armed native accounts exceed MAX_ARMED_NATIVE_ACCOUNTS")
        if audit_log is not None:
            path = Path(audit_log)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._audit_handle = path.open("a", encoding="utf-8")
        for actor in self._actor_list:
            actor.start()

    def _context_mode(self, record: ActorRecord):
        for binding in record.bindings:
            context = self.journal.context(binding)
            if context is not None:
                return context.mode
        return None

    def register_pairing(self, instance_id: str, token: str) -> None:
        """Register (or replace) the pairing token for a communicating instance."""
        if not (type(instance_id) is str and instance_id):
            raise ValueError("instance_id required")
        if not (type(token) is str and len(token) == nc.PAIRING_TOKEN_HEX_LENGTH and
                all(char in _HEX for char in token)):
            raise ValueError("pairing token must be fixed-length lowercase hex")
        with self._lock:
            self._tokens[instance_id] = token

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            actors = list(self._actor_list)
            handle = self._audit_handle
            self._audit_handle = None
        for actor in actors:
            actor.stop()
        if handle is not None:
            try:
                handle.close()
            except OSError:
                _LOGGER.warning("audit_close_failed")

    # -- small utilities -----------------------------------------------------

    def _inc(self, key: str) -> None:
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + 1

    def counters(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counters)

    def _now(self) -> datetime:
        value = self.clock.now()
        return value if isinstance(value, datetime) else datetime.now(timezone.utc)

    def _mono(self) -> float:
        value = getattr(self.clock, "monotonic", None)
        return float(value()) if callable(value) else time.monotonic()

    def _audit(self, event: str, **fields) -> None:
        handle = self._audit_handle
        if handle is None:
            return
        record = {"event": event, "at": self._now().isoformat().replace("+00:00", "Z")}
        record.update({key: value for key, value in fields.items() if value is not None})
        try:
            handle.write(json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n")
            handle.flush()
        except OSError:
            _LOGGER.warning("audit_write_failed")

    def _ingest_one(self, reconciler, binding: c.Binding, item, instance_id: str) -> None:
        from .reconcile import CallbackContext, normalize_event
        raw = {field.key: field.value for field in item.raw_fields}
        context = CallbackContext(
            event_id=item.event_id, kind=item.kind, received_at=self._now(),
            session_epoch=binding.session_epoch, source=c.Source.SIMULATION,
            account_key=binding.account_key, strategy_key=binding.strategy_key,
            simulation_scope=self._armed(binding))
        try:
            reconciler.ingest(normalize_event(raw, context))
            self._inc("events_ingested")
        except (LedgerError, ContractError, ValueError, TypeError):
            self._inc("events_quarantined")
            self._audit("event_quarantined", instance_id=instance_id,
                        event_id=item.event_id)

    def _drain_ingest_backlog(self) -> None:
        """FIFO business ingestion of stream-persisted events (budgeted)."""
        if not self._ingest_backlog:
            return
        from .reconcile import Reconciler
        reconciler = Reconciler(self.journal)
        budget = self._mono() + 0.8
        drained = 0
        remaining = []
        while self._ingest_backlog:
            binding, item = self._ingest_backlog.pop(0)
            if drained >= 4 or self._mono() > budget:
                remaining.append((binding, item))
                continue
            drained += 1
            self._ingest_one(reconciler, binding, item, "backlog")
        self._ingest_backlog = remaining

    # -- readiness / phase projections --------------------------------------

    def _disabled_readiness(self, blockers: tuple[str, ...]) -> nc.V2Readiness:
        return nc.V2Readiness(
            connection=c.ConnectionState.OFFLINE, state=c.ReadinessState.DISCONNECTED,
            can_plan=False, can_submit=False, can_cancel_owned=False,
            blockers=blockers, mode=c.ExecutionMode.DISABLED, coverage=None,
            evidence_id=None, execution_scope=nc.ExecutionScope.NONE, native_permit_id=None)

    def _armed_readiness(self, binding: c.Binding,
                         blockers: tuple[str, ...] = ()) -> nc.V2Readiness:
        context = self.journal.context(binding)
        mode = context.mode if context is not None else c.ExecutionMode.DISABLED
        scope = (nc.ExecutionScope.SIMULATION_FULL_ACCOUNT
                 if mode is c.ExecutionMode.SIMULATION else
                 nc.ExecutionScope.NATIVE_PILOT if mode is c.ExecutionMode.NATIVE
                 else nc.ExecutionScope.NONE)
        state = c.ReadinessState.RECONCILE_REQUIRED if blockers else c.ReadinessState.PLAN_READY
        return nc.V2Readiness(
            connection=c.ConnectionState.SERVICE_ONLY, state=state,
            can_plan=not blockers, can_submit=False, can_cancel_owned=False,
            blockers=blockers, mode=mode, coverage=None, evidence_id=None,
            execution_scope=scope, native_permit_id=None)

    def _armed(self, binding: c.Binding) -> bool:
        context = self.journal.context(binding)
        return context is not None and context.mode is not c.ExecutionMode.DISABLED

    # -- NE1 arming mechanism (NE1-ARB-ARMING-001 RULING 03/05) ---------------

    def _assembly_for(self, binding: c.Binding) -> TrustedControlAssembly | None:
        if not self._trusted_control_assemblies:
            return None
        return self._trusted_control_assemblies.get(c.canonical_hash(binding))

    def _native_issuance_blockers(self, binding: c.Binding) -> tuple[str, ...] | None:
        """AR3.3 native issuance gate. None means the binding is not under a
        trusted native assembly (the planning-mode flow is unchanged);
        otherwise these are the internal blockers that stop NEW poll/ready
        issuance on this call - an empty tuple means issuance is allowed."""
        assembly = self._assembly_for(binding)
        if assembly is None:
            return None
        key = c.canonical_hash(binding)
        now = self._now()
        with self._lock:
            revoked = key in self._arming_revoked
            closed = bool(self._gate_closed.get(key))
            fact = self._arming_confirmed.get(key)
        if revoked or closed:
            return ("GATE_CLOSED",)
        if fact is None:
            # no valid confirmation of THIS service epoch: never issue
            return ("NATIVE_PERMIT_UNREGISTERED",)
        if now >= fact.expires_at:
            return ("ARMING_EXPIRED",)
        try:
            durable_revision = self.journal.control_revision(binding)
        except LedgerError:
            return ("CONTROL_STATE_MISMATCH",)
        if durable_revision != fact.control_revision:
            return ("CONTROL_STATE_MISMATCH",)
        return ()

    def _native_readiness(self, blockers: tuple[str, ...], *,
                          permit_id: str | None = None) -> nc.V2Readiness:
        """Readiness projection for a trusted native assembly binding.

        can_submit/can_cancel_owned stay false: recording the user admission
        never flips them (AR3.3); the verified permit id is echoed whenever
        the fact backing this projection was just verified."""
        return nc.V2Readiness(
            connection=c.ConnectionState.SERVICE_ONLY,
            state=(c.ReadinessState.RECONCILE_REQUIRED if blockers
                   else c.ReadinessState.PLAN_READY),
            can_plan=not blockers, can_submit=False, can_cancel_owned=False,
            blockers=tuple(blockers), mode=c.ExecutionMode.NATIVE, coverage=None,
            evidence_id=None, execution_scope=nc.ExecutionScope.NATIVE_PILOT,
            native_permit_id=permit_id)

    def _native_confirmed_readiness(self, binding: c.Binding) -> nc.V2Readiness:
        with self._lock:
            fact = self._arming_confirmed.get(c.canonical_hash(binding))
        return self._native_readiness((), permit_id=fact.permit_id if fact else None)

    def _permit_evidence_blockers(self, registry: ProductionServiceRegistry,
                                  permit: nc.NativePilotPermit,
                                  now: datetime) -> tuple[str, ...]:
        """AR3.3: evidence registration and temporal validity for the permit."""
        from .native_evidence import (
            NativeBaselineEvidence, NativeRouteEvidence, NativeSettlementEvidence,
            validate_baseline, validate_route, validate_settlement,
        )
        baseline = registry.record_by_id("NativeBaselineEvidence",
                                         permit.baseline_evidence_id)
        route = registry.record_by_id("NativeRouteEvidence", permit.route_evidence_id)
        settlement = registry.record_by_id("NativeSettlementEvidence",
                                           permit.cash_bound_evidence_id)
        if not (isinstance(baseline, NativeBaselineEvidence)
                and isinstance(route, NativeRouteEvidence)
                and isinstance(settlement, NativeSettlementEvidence)):
            return ("NATIVE_PERMIT_UNREGISTERED",)
        for check in (validate_baseline(baseline, now=now),
                      validate_route(route, now=now),
                      validate_settlement(settlement, now=now)):
            if not check.ok:
                return ("NATIVE_PERMIT_UNREGISTERED",)
        return ()

    @staticmethod
    def _grant_scope_blockers(permit: nc.NativePilotPermit,
                              authorization: nc.NativeAuthorization
                              ) -> tuple[str, ...]:
        """AR3.3: send and CANCEL_OWNED scopes stay separate; the S4 caps
        (send exactly 1, cancel explicitly 0 or 1) hold and neither record
        may widen the other's authority (actions, attempts, volume, prices)."""
        if not set(permit.actions) <= set(authorization.actions):
            return ("CONTROL_STATE_MISMATCH",)
        if permit.max_send_attempts != 1:
            return ("CONTROL_STATE_MISMATCH",)
        if permit.max_cancel_attempts not in (0, 1):
            return ("CONTROL_STATE_MISMATCH",)
        if permit.max_send_attempts > authorization.max_send_attempts:
            return ("CONTROL_STATE_MISMATCH",)
        if permit.max_cancel_attempts > authorization.max_cancel_attempts:
            return ("CONTROL_STATE_MISMATCH",)
        if permit.max_volume > authorization.max_volume:
            return ("CONTROL_STATE_MISMATCH",)
        authorized_symbols = {(item.exchange, item.instrument_id)
                              for item in authorization.symbols}
        authorized_bounds = {(bound.symbol.exchange, bound.symbol.instrument_id): bound
                             for bound in authorization.price_bounds}
        for bound in permit.price_bounds:
            symbol = (bound.symbol.exchange, bound.symbol.instrument_id)
            if symbol not in authorized_symbols:
                return ("CONTROL_STATE_MISMATCH",)
            outer = authorized_bounds.get(symbol)
            if outer is not None and (bound.lower < outer.lower
                                      or bound.upper > outer.upper):
                return ("CONTROL_STATE_MISMATCH",)
        return ()

    def _validate_arming_confirmation(self, request: nc.ControlReport,
                                      confirmation: nc.ParameterConfirmation | None,
                                      binding: c.Binding) -> tuple[str, ...]:
        """The RULING 03 AR3.3 real validation chain (internal blocker codes).

        Transport authentication/session/generation/service-epoch checks have
        already run at the ingress; this is the confirmation, anchor, trusted
        assembly, registered grant chain, budget and lifecycle verification.
        The chain is replayed for EVERY retransmission (arming requests never
        short-circuit through the HTTP replay caches), so a revoked, expired
        or revision-drifted fact can never re-authorize through a cached ACK.
        """
        if confirmation is None or confirmation.binding != binding:
            return ("OWNERSHIP_CONFLICT",)
        key = c.canonical_hash(binding)
        with self._lock:
            if key in self._arming_revoked:
                # the disarm latch is one-way: a later PARAMS_CONFIRMED can
                # never clear it (AR3.3/AR5.2)
                return ("GATE_CLOSED",)
        if confirmation.explicit_ui_action is not True:
            # the frozen DTO already rejects a non-interactive flag at decode;
            # this re-assertion keeps the handler honest on its own
            return ("CONTROL_STATE_MISMATCH",)
        now = self._now()
        if confirmation.confirmed_at > now or now >= confirmation.expires_at:
            return ("ARMING_EXPIRED",)
        if confirmation.params_sha256 != request.params_sha256:
            # the confirmation target hash must equal the message hash
            return ("PARAMS_CHANGED",)
        prerequisites = self._arming_prerequisites
        if prerequisites is None:
            # no approved S4 anchor: production arming is always BLOCKED
            return ("NATIVE_PERMIT_UNREGISTERED",)
        if prerequisites.expires_at <= now:
            return ("ARMING_EXPIRED",)
        if confirmation.adapter_sha256 != prerequisites.client_file_sha256:
            # adapter hash does not match the approved release
            return ("CONTROL_STATE_MISMATCH",)
        assembly = self._assembly_for(binding)
        if assembly is None:
            # the default DISABLED assembly can never be promoted here
            return ("NATIVE_PERMIT_UNREGISTERED",)
        state = assembly.control_state
        if state.mode is not c.ExecutionMode.NATIVE or state.native_permit_id is None:
            return ("NATIVE_PERMIT_UNREGISTERED",)
        if state.binding != binding:
            return ("OWNERSHIP_CONFLICT",)
        if state.params_sha256 != request.params_sha256:
            return ("PARAMS_CHANGED",)
        try:
            durable_revision = self.journal.control_revision(binding)
        except LedgerError:
            return ("CONTROL_STATE_MISMATCH",)
        if durable_revision != state.control_revision:
            return ("CONTROL_STATE_MISMATCH",)
        registry = self._evidence_registry
        if not isinstance(registry, ProductionServiceRegistry):
            return ("NATIVE_PERMIT_UNREGISTERED",)
        permit = registry.permit_record(state.native_permit_id)
        if permit is None:
            return ("NATIVE_PERMIT_UNREGISTERED",)
        authorization = registry.authorization_record(permit.authorization_id)
        if authorization is None:
            return ("NATIVE_PERMIT_UNREGISTERED",)
        if permit.binding != binding or authorization.binding != binding:
            return ("OWNERSHIP_CONFLICT",)
        if (permit.params_sha256 != request.params_sha256
                or authorization.params_sha256 != request.params_sha256):
            return ("PARAMS_CHANGED",)
        if permit.control_revision != state.control_revision:
            return ("CONTROL_STATE_MISMATCH",)
        if (permit.origin is not assembly.origin
                or authorization.origin is not assembly.origin):
            return ("NATIVE_PERMIT_UNREGISTERED",)
        if permit.expires_at <= now or authorization.expires_at <= now:
            return ("NATIVE_PERMIT_EXPIRED",)
        if confirmation.expires_at > min(permit.expires_at,
                                         authorization.expires_at):
            # the confirmed admission may not outlive its grants
            return ("ARMING_EXPIRED",)
        blockers = self._permit_evidence_blockers(registry, permit, now)
        if blockers:
            return blockers
        blockers = self._grant_scope_blockers(permit, authorization)
        if blockers:
            return blockers
        try:
            counters = self.journal.execution_counters(binding)
        except LedgerError:
            return ("LEDGER_FAILURE",)
        if counters.unresolved_dispatch_count:
            return ("BLOCKED_DISPATCH",)
        if (counters.native_send_attempts.lower_bound > 0
                and counters.native_send_attempts.lower_bound >= permit.max_send_attempts):
            return ("PERMIT_EXHAUSTED",)
        if (counters.native_cancel_attempts.lower_bound > 0
                and counters.native_cancel_attempts.lower_bound
                >= permit.max_cancel_attempts):
            return ("PERMIT_EXHAUSTED",)
        with self._lock:
            existing = self._arming_confirmed.get(key)
            if existing is None:
                self._arming_confirmed[key] = _ConfirmedArming(
                    confirmation_id=confirmation.confirmation_id,
                    params_sha256=confirmation.params_sha256,
                    adapter_sha256=confirmation.adapter_sha256,
                    permit_id=permit.permit_id,
                    control_revision=state.control_revision,
                    confirmed_at=confirmation.confirmed_at,
                    expires_at=confirmation.expires_at)
                replay = False
            elif (existing.confirmation_id == confirmation.confirmation_id
                  and existing.params_sha256 == confirmation.params_sha256
                  and existing.adapter_sha256 == confirmation.adapter_sha256
                  and existing.permit_id == permit.permit_id
                  and existing.control_revision == state.control_revision
                  and existing.confirmed_at == confirmation.confirmed_at
                  and existing.expires_at == confirmation.expires_at):
                # an identical logical confirmation replays the same fact
                replay = True
            else:
                # one arming per lifecycle: a second confirmation id, or the
                # same id with different content, is rejected
                return ("CONTROL_STATE_MISMATCH",)
        self._audit("arming_confirmed" if not replay else "arming_replayed",
                    instance_id=request.instance_id,
                    confirmation_id=confirmation.confirmation_id,
                    permit_id=permit.permit_id,
                    control_revision=state.control_revision,
                    replay=replay)
        return ()

    def control_phase(self, binding: c.Binding) -> nc.ControlPhase:
        """Audited control projection only; never a new order state machine."""
        key = c.canonical_hash(binding)
        with self._lock:
            closed = bool(self._gate_closed.get(key))
        native_gate = self._native_issuance_blockers(binding)
        if native_gate is not None:
            # the per-epoch arming lifecycle governs a trusted native
            # assembly binding; the planning-mode _armed wording never
            # substitutes for it (AR3.3)
            if not native_gate:
                return nc.ControlPhase.ARMED
            if native_gate[0] == "GATE_CLOSED":
                counters = self.journal.execution_counters(binding)
                if counters.unresolved_dispatch_count:
                    return nc.ControlPhase.REVOKE_PENDING
                return nc.ControlPhase.DISABLED_CONFIRMED
            return nc.ControlPhase.DISABLED
        if not self._armed(binding):
            return nc.ControlPhase.DISABLED_CONFIRMED if closed else nc.ControlPhase.DISABLED
        if closed:
            counters = self.journal.execution_counters(binding)
            if counters.unresolved_dispatch_count:
                return nc.ControlPhase.REVOKE_PENDING
            return nc.ControlPhase.DISABLED_CONFIRMED
        return nc.ControlPhase.ARMED

    def _revisions(self, binding: c.Binding) -> tuple[int, int]:
        ledger_revision = int(self.journal.check().revision)
        control_revision = self.journal.control_revision(binding)
        return ledger_revision, (int(control_revision) if control_revision is not None else 0)

    # -- typed error rendering ----------------------------------------------

    def _transport_error(
        self, code: nc.TransportErrorCode, *, received_at: datetime,
        request: nc.TransportMessage | None = None, binding: c.Binding | None = None,
        retry_after_ms: int | None = None,
    ) -> tuple[int, str, dict[str, str]]:
        now = self._now()
        revisions = self._revisions(binding) if binding is not None else (None, None)
        error = nc.TransportErrorResponse(
            request_id=request.request_id if request is not None else None,
            instance_id=request.instance_id if request is not None else None,
            session_epoch=request.session_epoch if request is not None else None,
            generation=request.generation if request is not None else None,
            sequence=request.sequence if request is not None else None,
            params_sha256=request.params_sha256 if request is not None else None,
            service_epoch=self.service_epoch,
            request_service_epoch=request.service_epoch if request is not None else None,
            sent_at=now, received_at=received_at, binding=binding,
            ledger_revision=revisions[0], control_revision=revisions[1],
            error_code=code, http_status=nc.TRANSPORT_ERROR_HTTP_STATUS[code],
            retry_after_ms=retry_after_ms)
        status = nc.TRANSPORT_ERROR_HTTP_STATUS[code]
        headers: dict[str, str] = {}
        if code is nc.TransportErrorCode.POLL_TOO_FAST and retry_after_ms is not None:
            headers["Retry-After"] = str(max(1, math.ceil(retry_after_ms / 1000)))
        self._inc("transport_errors")
        self._inc("transport_error_" + code.value)
        return status, nc.encode(error), headers

    def _response(
        self, payload: nc.TransportResponse, *, request: nc.TransportMessage,
        binding: c.Binding, received_at: datetime,
    ) -> tuple[int, str, dict[str, str]]:
        text = nc.encode(payload)
        if len(text.encode("utf-8")) > nc.MAX_RESPONSE_BYTES:
            # Post-commit size failure keeps the durable facts (RULING 03).
            return self._transport_error(
                nc.TransportErrorCode.RESPONSE_TOO_LARGE, received_at=received_at,
                request=request, binding=binding)
        return HTTPStatus.OK, text, {}

    # -- authentication (RULING 10) -------------------------------------------

    def _authenticate(self, token: str | None) -> str:
        """Constant-time pairing check; one identical failure for every cause."""
        if not (type(token) is str and len(token) == nc.PAIRING_TOKEN_HEX_LENGTH and
                all(char in _HEX for char in token)):
            hmac.compare_digest(secrets.token_bytes(nc.PAIRING_TOKEN_BYTES),
                                secrets.token_bytes(nc.PAIRING_TOKEN_BYTES))
            raise ServiceTransportError(nc.TransportErrorCode.AUTHENTICATION_FAILED)
        supplied = bytes.fromhex(token)
        with self._lock:
            entries = tuple(self._tokens.items())
        matched: str | None = None
        for instance_id, expected in entries:
            if hmac.compare_digest(supplied, bytes.fromhex(expected)):
                matched = instance_id
        if matched is None:
            # keep the comparison path taken even with an empty registry
            hmac.compare_digest(supplied, secrets.token_bytes(nc.PAIRING_TOKEN_BYTES))
            raise ServiceTransportError(nc.TransportErrorCode.AUTHENTICATION_FAILED)
        return matched

    def _resolve_binding(self, request: nc.TransportMessage) -> tuple[_AccountActor, c.Binding]:
        with self._lock:
            found = self._instances.get(request.instance_id)
        if found is None:
            raise ServiceTransportError(
                nc.TransportErrorCode.ACTOR_UNREGISTERED, request=request)
        actor, binding = found
        if binding.session_epoch != request.session_epoch:
            raise ServiceTransportError(
                nc.TransportErrorCode.BINDING_MISMATCH, request=request, binding=binding)
        return actor, binding

    # -- replay (RULING 01 fingerprint namespace) ------------------------------

    def _replay_lookup(self, request: nc.TransportMessage, fingerprint: str) -> str | None:
        code, stored = self.journal.record_http_request(
            instance_id=request.instance_id, session_epoch=request.session_epoch,
            generation=request.generation, service_epoch=self.service_epoch,
            request_id=request.request_id, fingerprint=fingerprint)
        return stored if code is c.ResultCode.IDEMPOTENT else None

    def _replay_store(self, request: nc.TransportMessage, fingerprint: str, response: str) -> None:
        self.journal.store_http_response(
            instance_id=request.instance_id, session_epoch=request.session_epoch,
            generation=request.generation, service_epoch=self.service_epoch,
            request_id=request.request_id, fingerprint=fingerprint, response=response)

    # -- poll rate limiting (RULING 03) ----------------------------------------

    def _poll_interval_ms(self, binding: c.Binding) -> int:
        return (self.poll_armed_interval_ms if self._armed(binding)
                else self.poll_disabled_interval_ms)

    def _admit_poll(self, binding: c.Binding, instance_id: str) -> int | None:
        """Admit a new poll; returns retry_after_ms when throttled."""
        interval_ms = self._poll_interval_ms(binding)
        now = self._mono()
        with self._lock:
            last = self._last_poll_mono.get(instance_id)
            if last is not None and (now - last) * 1000.0 < interval_ms:
                elapsed_ms = (now - last) * 1000.0
                return max(1, math.ceil(interval_ms - elapsed_ms))
            self._last_poll_mono[instance_id] = now
        return None

    # -- response base ---------------------------------------------------------

    def _base_response(self, request: nc.TransportMessage, binding: c.Binding,
                       received_at: datetime) -> dict:
        ledger_revision, control_revision = self._revisions(binding)
        return {
            "request_id": request.request_id, "instance_id": request.instance_id,
            "session_epoch": request.session_epoch, "generation": request.generation,
            "sequence": request.sequence, "params_sha256": request.params_sha256,
            "service_epoch": self.service_epoch,
            "request_service_epoch": request.service_epoch,
            "sent_at": self._now(), "received_at": received_at, "binding": binding,
            "ledger_revision": ledger_revision, "control_revision": control_revision,
        }

    # -- the six business handlers ---------------------------------------------

    def handle_poll(self, request: nc.PollRequest, binding: c.Binding,
                    actor: _AccountActor, received_at: datetime) -> nc.PollResponse:
        def work() -> nc.PollResponse:
            self._drain_ingest_backlog()
            if request.service_epoch is None:
                # Initial disabled handshake (RULING 07): metadata only.
                if (request.snapshot is not None or request.snapshot_id is not None or
                        request.ack_watermark is not None):
                    raise ServiceTransportError(nc.TransportErrorCode.INVALID_REQUEST)
                readiness = self._disabled_readiness(("MODE_DISABLED",))
                return nc.PollResponse(
                    kind=nc.ResponseKind.BLOCKED, offer=None, readiness=readiness,
                    blockers=readiness.blockers,
                    **self._base_response(request, binding, received_at))
            if request.ack_watermark is not None:
                try:
                    self.journal.validate_client_ack(request.ack_watermark)
                except LedgerError as exc:
                    raise ServiceTransportError(
                        nc.TransportErrorCode.ACK_WATERMARK_INVALID,
                        request=request, binding=binding) from exc
            if request.snapshot is not None:
                try:
                    self.journal.register_capture(
                        request.snapshot, instance_id=request.instance_id,
                        session_epoch=request.session_epoch, generation=request.generation)
                except LedgerError as exc:
                    if "SNAPSHOT_CONFLICT" in exc.blockers:
                        raise ServiceTransportError(
                            nc.TransportErrorCode.SNAPSHOT_CONFLICT,
                            request=request, binding=binding) from exc
                    raise ServiceTransportError(
                        nc.TransportErrorCode.PERSISTENCE_UNAVAILABLE,
                        request=request, binding=binding) from exc
            native_gate = self._native_issuance_blockers(binding)
            if native_gate:
                # AR3.3/AR5.2: no valid confirmation of this service epoch
                # (or a revoked/expired/drifted one) stops NEW native
                # issuance BEFORE any orchestrator work runs
                gate_blockers = vocab_blockers(native_gate)
                return nc.PollResponse(
                    kind=nc.ResponseKind.BLOCKED, offer=None,
                    readiness=self._native_readiness(gate_blockers),
                    blockers=gate_blockers,
                    **self._base_response(request, binding, received_at))
            outcome = self.orchestrator.poll(binding, request)
            blockers = vocab_blockers(outcome.blockers)
            offer = outcome.offer
            if offer is not None and offer.binding != binding:
                blockers = ("OWNERSHIP_CONFLICT",)
                offer = None
            if offer is not None and not blockers:
                readiness = (self._native_confirmed_readiness(binding)
                             if native_gate is not None
                             else self._armed_readiness(binding))
                return nc.PollResponse(
                    kind=nc.ResponseKind.OFFER, offer=offer, readiness=readiness,
                    blockers=(), **self._base_response(request, binding, received_at))
            if native_gate is not None:
                readiness = self._native_readiness(blockers)
            else:
                readiness = (self._disabled_readiness(blockers) if not self._armed(binding)
                             else self._armed_readiness(binding, blockers))
            return nc.PollResponse(
                kind=(nc.ResponseKind.BLOCKED if blockers else nc.ResponseKind.IDLE),
                offer=None, readiness=readiness, blockers=blockers,
                **self._base_response(request, binding, received_at))

        return actor.call(work, timeout=self.actor_call_timeout)

    def handle_ready(self, request: nc.ReadyRequest, binding: c.Binding,
                     actor: _AccountActor, received_at: datetime) -> nc.ReadyResponse:
        def work() -> nc.ReadyResponse:
            self._drain_ingest_backlog()
            native_gate = self._native_issuance_blockers(binding)
            if native_gate:
                # AR3.3/AR5.2: disarm or a missing/expired/drifted epoch
                # confirmation stops NEW native ticket issuance here too
                gate_blockers = vocab_blockers(native_gate)
                return nc.ReadyResponse(
                    kind=nc.ReadyResponseKind.BLOCKED, dispatch=None,
                    readiness=self._native_readiness(gate_blockers),
                    blockers=gate_blockers,
                    **self._base_response(request, binding, received_at))
            outcome = self.orchestrator.ready(binding, request)
            if outcome.context_conflict:
                raise ServiceTransportError(
                    nc.TransportErrorCode.CONTEXT_CONFLICT, request=request, binding=binding)
            blockers = vocab_blockers(outcome.blockers)
            command = outcome.dispatch
            if command is not None and command.binding != binding:
                blockers = ("OWNERSHIP_CONFLICT",)
                command = None
            if command is not None and not blockers:
                readiness = (self._native_confirmed_readiness(binding)
                             if native_gate is not None
                             else self._armed_readiness(binding))
                return nc.ReadyResponse(
                    kind=nc.ReadyResponseKind.DISPATCH, dispatch=command,
                    readiness=readiness, blockers=(),
                    **self._base_response(request, binding, received_at))
            blockers = blockers or ("RECONCILE_REQUIRED",)
            if native_gate is not None:
                readiness = self._native_readiness(blockers)
            else:
                readiness = (self._disabled_readiness(blockers) if not self._armed(binding)
                             else self._armed_readiness(binding, blockers))
            return nc.ReadyResponse(
                kind=nc.ReadyResponseKind.BLOCKED, dispatch=None, readiness=readiness,
                blockers=blockers, **self._base_response(request, binding, received_at))

        return actor.call(work, timeout=self.actor_call_timeout)

    def handle_results(self, request: nc.ResultsRequest, binding: c.Binding,
                       actor: _AccountActor, received_at: datetime) -> nc.ResultsResponse:
        def work() -> nc.ResultsResponse:
            self._drain_ingest_backlog()
            try:
                outcome = self.orchestrator.complete(request.receipt)
            except LedgerError as exc:
                if exc.code is c.ResultCode.NOT_FOUND:
                    raise ServiceTransportError(
                        nc.TransportErrorCode.INVALID_REQUEST, request=request,
                        binding=binding) from exc
                if exc.code is c.ResultCode.REVISION_CONFLICT or \
                        "OWNERSHIP_CONFLICT" in exc.blockers:
                    raise ServiceTransportError(
                        nc.TransportErrorCode.RECEIPT_CONFLICT, request=request,
                        binding=binding) from exc
                raise ServiceTransportError(
                    nc.TransportErrorCode.PERSISTENCE_UNAVAILABLE, request=request,
                    binding=binding) from exc
            if outcome.not_found or outcome.submit_result is None:
                raise ServiceTransportError(
                    nc.TransportErrorCode.INVALID_REQUEST, request=request, binding=binding)
            result = outcome.submit_result
            leg = result.leg
            projection = nc.SubmitProjection(
                code=result.code, leg_id=leg.leg.leg_id, leg_state=leg.state,
                attempt_no=leg.attempt_no, order_id=leg.order_id,
                reported_filled=leg.reported_filled, trade_filled=leg.trade_filled,
                risk_filled=leg.risk_filled, trade_gap=leg.trade_gap,
                cancel_state=leg.cancel_state, revision=result.revision)
            return nc.ResultsResponse(
                receipt_id=request.receipt.receipt_id, command_id=request.receipt.command_id,
                attempt_id=request.receipt.attempt_id,
                receipt_sha256=c.canonical_hash(request.receipt), persisted=True,
                duplicate=result.code is c.ResultCode.IDEMPOTENT,
                submit_result=projection,
                **self._base_response(request, binding, received_at))

        return actor.call(work, timeout=self.actor_call_timeout)

    def handle_events(self, request: nc.EventBatch, binding: c.Binding,
                      actor: _AccountActor, received_at: datetime) -> nc.EventsResponse:
        def work() -> nc.EventsResponse:
            accepted = tuple(item.event_id for item in request.events)
            duplicates: list[str] = []
            for item in request.events:
                key = (item.event_stream_id, item.event_sequence)
                with self._lock:
                    known = self._known_events.get(key)
                if known == item.event_id and item.event_id not in duplicates:
                    duplicates.append(item.event_id)
            try:
                watermark = self.journal.record_event_envelopes(
                    tuple(request.events), instance_id=request.instance_id)
            except LedgerError as exc:
                if any(code in _EVENT_CONFLICT_BLOCKERS for code in exc.blockers):
                    raise ServiceTransportError(
                        nc.TransportErrorCode.ACK_WATERMARK_INVALID, request=request,
                        binding=binding) from exc
                raise ServiceTransportError(
                    nc.TransportErrorCode.PERSISTENCE_UNAVAILABLE, request=request,
                    binding=binding) from exc
            with self._lock:
                for item in request.events:
                    self._known_events[(item.event_stream_id, item.event_sequence)] = item.event_id
                self._streams_seen[(request.instance_id, request.session_epoch,
                                    request.generation)] = request.events[0].event_stream_id
            # Plan 5.2 step 7: normalized ingestion into the reconciler. The
            # stream store above is transport bookkeeping; THIS is the business
            # projection. Ingestion runs under a per-request budget (each
            # ingest is one FULL write); whatever does not fit stays in a FIFO
            # backlog that the next actor work drains first - the ACK already
            # certifies durable stream storage, so deferred business projection
            # never blocks the response. Scope: the simulation assembly ingests
            # with simulation attribution (native production attribution needs
            # the S4 route-evidence machinery). A rejected event is quarantined
            # by the journal and still counts as durably saved for the ACK.
            from .reconcile import Reconciler
            reconciler = Reconciler(self.journal)
            budget = self._mono() + 1.2
            ingested = 0
            for item in request.events:
                if ingested >= 6 or self._mono() > budget:
                    self._ingest_backlog.append((binding, item))
                    continue
                ingested += 1
                self._ingest_one(reconciler, binding, item, request.instance_id)
            return nc.EventsResponse(
                event_stream_id=request.events[0].event_stream_id,
                accepted_event_ids=accepted, duplicate_event_ids=tuple(duplicates),
                persisted=True, ack_watermark=watermark,
                **self._base_response(request, binding, received_at))

        return actor.call(work, timeout=self.actor_call_timeout)

    def handle_control(self, request: nc.ControlReport, binding: c.Binding,
                       actor: _AccountActor, received_at: datetime) -> nc.ControlResponse:
        def work() -> nc.ControlResponse:
            self._drain_ingest_backlog()
            blockers: tuple[str, ...] = ()
            confirmation_id: str | None = None
            if request.kind is nc.ControlKind.PARAMS_CONFIRMED:
                confirmation = request.confirmation
                # the frozen response contract always echoes a submitted
                # confirmation id, including the blocked branch
                confirmation_id = confirmation.confirmation_id \
                    if confirmation is not None else None
                # RULING 03 AR3.3: the real validation chain replaces the old
                # binding-only comparison; a plain RECORDED is never a local
                # activation permission (readiness flags stay false)
                blockers = vocab_blockers(
                    self._validate_arming_confirmation(request, confirmation, binding))
            if request.kind in (nc.ControlKind.GATE_CLOSED, nc.ControlKind.STOPPED):
                key = c.canonical_hash(binding)
                # RULING 05 server side: disarm first closes the new-issuance
                # entrance and latches the revocation one-way for this
                # service epoch; existing ticket evidence keeps its identity
                # and a later PARAMS_CONFIRMED can never clear the latch.
                with self._lock:
                    self._gate_closed[key] = True
                    self._arming_revoked.add(key)
                self._audit("arming_revoked", instance_id=request.instance_id,
                            kind=request.kind.value)
            phase = self.control_phase(binding)
            if self._assembly_for(binding) is not None:
                if request.kind is nc.ControlKind.PARAMS_CONFIRMED:
                    # the frozen response contract requires the readiness
                    # blockers to equal the response blockers: a BLOCKED
                    # confirmation carries its own chain blockers, a RECORDED
                    # one the confirmed native projection
                    readiness = (self._native_readiness(blockers) if blockers
                                 else self._native_confirmed_readiness(binding))
                else:
                    # a RECORDED non-arming report carries empty readiness
                    # blockers (the lifecycle verdict lives in control_phase)
                    readiness = self._native_readiness(())
            else:
                readiness = (self._disabled_readiness(blockers) if not self._armed(binding)
                             else self._armed_readiness(binding, blockers))
            return nc.ControlResponse(
                kind=(nc.ControlResponseKind.BLOCKED if blockers
                      else nc.ControlResponseKind.RECORDED),
                report_kind=request.kind, confirmation_id=confirmation_id,
                control_phase=phase, readiness=readiness, blockers=blockers,
                **self._base_response(request, binding, received_at))

        return actor.call(work, timeout=self.actor_call_timeout)

    def handle_status(self, request: nc.StatusRequest, binding: c.Binding,
                      received_at: datetime) -> nc.StatusResponse:
        counters = self.journal.execution_counters(binding)
        pending = self.journal.pending_reconcile_count(binding)
        with self._lock:
            stream = self._streams_seen.get(
                (request.instance_id, request.session_epoch, request.generation))
        watermark = self.journal.ack_watermark(event_stream_id=stream) if stream else None
        native_gate = self._native_issuance_blockers(binding)
        if native_gate is None:
            readiness = (self._disabled_readiness(("MODE_DISABLED",)) if not self._armed(binding)
                         else self._armed_readiness(binding, ()))
        elif native_gate:
            readiness = self._native_readiness(vocab_blockers(native_gate))
        else:
            readiness = self._native_confirmed_readiness(binding)
        return nc.StatusResponse(
            control_phase=self.control_phase(binding), readiness=readiness,
            blockers=readiness.blockers, counters=counters,
            pending_reconcile_count=pending, ack_watermark=watermark,
            **self._base_response(request, binding, received_at))

    # -- entry points used by the HTTP layer ------------------------------------

    def _after_send_replay(self, request: nc.TransportMessage, fingerprint: str,
                           namespace: tuple, response: str, *, route: str,
                           deferred: bool) -> Callable[[], None]:
        """Durably persist the replay entry after the response left the socket."""

        def finish() -> None:
            try:
                if deferred:
                    code, prior = self.journal.record_http_request(
                        instance_id=request.instance_id,
                        session_epoch=request.session_epoch,
                        generation=request.generation,
                        service_epoch=self.service_epoch,
                        request_id=request.request_id, fingerprint=fingerprint)
                    if code is c.ResultCode.IDEMPOTENT and prior is not None:
                        # the original stored response is preserved verbatim
                        self._replay_cache.put(namespace, fingerprint, prior)
                        self._inc("ready_deferred_registrations")
                        return
                self._replay_store(request, fingerprint, response)
                self._replay_cache.put(namespace, fingerprint, response)
                if deferred:
                    self._inc("ready_deferred_registrations")
            except LedgerError as exc:
                if "REQUEST_ID_CONFLICT" in exc.blockers:
                    # post-send conflict: the response already left; the
                    # durable cache keeps the earlier request untouched
                    self._inc("replay_conflicts_postsend")
                    self._audit("replay_conflict_postsend", route=route,
                                instance_id=request.instance_id,
                                request_id=request.request_id)
                else:
                    self._inc("replay_store_failed")
                    self._audit("replay_store_failed", route=route,
                                instance_id=request.instance_id,
                                request_id=request.request_id)
            except Exception:
                self._inc("replay_store_failed")
                self._audit("replay_store_failed", route=route)

        return finish

    def process_post(self, route: str, raw: bytes, *, token: str | None,
                     received_at: datetime) -> tuple[int, str, dict[str, str],
                                                     Callable[[], None] | None]:
        self._inc("requests")
        try:
            instance_id = self._authenticate(token)
        except ServiceTransportError as exc:
            self._audit("request_rejected", route=route, error=exc.code.value)
            return (*self._transport_error(exc.code, received_at=received_at), None)
        try:
            request = _decode_request(route, raw)
        except ServiceTransportError as exc:
            self._audit("request_rejected", route=route, instance_id=instance_id,
                        error=exc.code.value)
            return (*self._transport_error(exc.code, received_at=received_at), None)
        if request.instance_id != instance_id:
            with self._lock:
                found = self._instances.get(instance_id)
            return (*self._transport_error(
                nc.TransportErrorCode.BINDING_MISMATCH, received_at=received_at,
                request=request, binding=found[1] if found else None), None)
        if request.service_epoch is None and route not in ("/execution/v1/poll", _STATUS_PATH):
            return (*self._transport_error(
                nc.TransportErrorCode.HANDSHAKE_REQUIRED, received_at=received_at,
                request=request), None)
        binding_for_error: c.Binding | None = None
        try:
            _, binding_for_error = self._resolve_binding(request)
        except ServiceTransportError:
            binding_for_error = None
        if request.service_epoch is not None and request.service_epoch != self.service_epoch:
            # epoch mismatch precedes replay lookup and any business write
            self._inc("epoch_mismatches")
            return (*self._transport_error(
                nc.TransportErrorCode.EPOCH_MISMATCH, received_at=received_at,
                request=request, binding=binding_for_error), None)
        try:
            actor, binding = self._resolve_binding(request)
        except ServiceTransportError as exc:
            self._audit("request_rejected", route=route, instance_id=instance_id,
                        error=exc.code.value)
            return (*self._transport_error(exc.code, received_at=received_at,
                                           request=request, binding=exc.binding), None)
        fingerprint = nc.request_fingerprint("POST", route, request)
        namespace = (request.instance_id, request.session_epoch, request.generation,
                     self.service_epoch, request.request_id)
        # AR3.3 (NE1-ARB-ARMING-001): an arming confirmation never
        # short-circuits through the HTTP replay caches - every
        # retransmission of the same logical confirmation re-runs the full
        # current-validity chain (revocation latch, expiry, control
        # revision), so a cached positive ACK can never keep authorizing
        # after the lifecycle moved on. Identical content replays the same
        # recorded fact inside the handler; the durable journal replay row
        # is still written post-send as transport evidence.
        arming_confirmation = (
            route == "/execution/v1/control"
            and isinstance(request, nc.ControlReport)
            and request.kind is nc.ControlKind.PARAMS_CONFIRMED)
        # OV-F06/C1.2 DTTL-04: the frozen N4 offer deadline anchors at the
        # capture sample time, so every synchronous write between sample and
        # issuance consumes the ticket window. The replay registration is
        # deferred on BOTH publication paths (poll and ready); deferred
        # persistence for earlier responses is explicitly allowed by DTTL-04.
        deferred = route in ("/execution/v1/ready", "/execution/v1/poll")
        cached = None if arming_confirmation else self._replay_cache.get(namespace)
        if cached is not None:
            cached_fingerprint, cached_response = cached
            if cached_fingerprint != fingerprint:
                self._audit("request_rejected", route=route,
                            instance_id=instance_id, error="REQUEST_ID_CONFLICT")
                return (*self._transport_error(
                    nc.TransportErrorCode.REQUEST_ID_CONFLICT, received_at=received_at,
                    request=request, binding=binding), None)
            if cached_response is not None:
                self._inc("replays")
                self._inc("replays_memory")
                return HTTPStatus.OK, cached_response, {}, None
            if deferred:
                # an identical request is in flight (registration deferred);
                # bounded-wait for its stored response so the retry receives
                # the ORIGINAL bytes rather than re-executing the business
                deadline_wait = time.monotonic() + 1.5
                while time.monotonic() < deadline_wait:
                    time.sleep(0.01)
                    cached = self._replay_cache.get(namespace)
                    if cached is not None and cached[1] is not None:
                        self._inc("replays")
                        self._inc("replays_memory")
                        return HTTPStatus.OK, cached[1], {}, None
                cached = self._replay_cache.get(namespace)
                if cached is not None and cached[1] is not None:
                    self._inc("replays")
                    self._inc("replays_memory")
                    return HTTPStatus.OK, cached[1], {}, None
                # registration lost (crash window): fall through and re-execute;
                # the business path is idempotent by design
        if not deferred and not arming_confirmation:
            try:
                stored = self._replay_lookup(request, fingerprint)
            except LedgerError as exc:
                if "REQUEST_ID_CONFLICT" in exc.blockers:
                    return (*self._transport_error(
                        nc.TransportErrorCode.REQUEST_ID_CONFLICT, received_at=received_at,
                        request=request, binding=binding), None)
                return (*self._transport_error(
                    nc.TransportErrorCode.PERSISTENCE_UNAVAILABLE, received_at=received_at,
                    request=request, binding=binding), None)
            if stored:
                self._inc("replays")
                self._replay_cache.put(namespace, fingerprint, stored)
                return HTTPStatus.OK, stored, {}, None
        if deferred:
            self._replay_cache.put(namespace, fingerprint, None)  # in-flight
        if route == "/execution/v1/poll":
            retry_after = self._admit_poll(binding, request.instance_id)
            if retry_after is not None:
                self._inc("polls_throttled")
                self._audit("poll_throttled", instance_id=request.instance_id,
                            retry_after_ms=retry_after)
                return (*self._transport_error(
                    nc.TransportErrorCode.POLL_TOO_FAST, received_at=received_at,
                    request=request, binding=binding, retry_after_ms=retry_after), None)
        try:
            if route == "/execution/v1/poll":
                response = self.handle_poll(request, binding, actor, received_at)
            elif route == "/execution/v1/ready":
                response = self.handle_ready(request, binding, actor, received_at)
            elif route == "/execution/v1/results":
                response = self.handle_results(request, binding, actor, received_at)
            elif route == "/execution/v1/events":
                response = self.handle_events(request, binding, actor, received_at)
            else:
                response = self.handle_control(request, binding, actor, received_at)
        except ServiceTransportError as exc:
            self._audit("request_failed", route=route, instance_id=request.instance_id,
                        error=exc.code.value)
            return (*self._transport_error(
                exc.code, received_at=received_at,
                request=exc.request or request, binding=exc.binding or binding,
                retry_after_ms=exc.retry_after_ms), None)
        status, text, headers = self._response(
            response, request=request, binding=binding, received_at=received_at)
        after_send: Callable[[], None] | None = None
        if status == HTTPStatus.OK:
            after_send = self._after_send_replay(
                request, fingerprint, namespace, text, route=route, deferred=deferred)
        self._audit("request_served", route=route, instance_id=request.instance_id,
                    request_id=request.request_id, status=status)
        return status, text, headers, after_send

    def process_status(self, headers, *, token: str | None,
                       received_at: datetime) -> tuple[int, str, dict[str, str],
                                                        Callable[[], None] | None]:
        self._inc("requests")
        self._inc("status_requests")
        try:
            instance_id = self._authenticate(token)
        except ServiceTransportError as exc:
            self._audit("request_rejected", route=_STATUS_PATH, error=exc.code.value)
            return (*self._transport_error(exc.code, received_at=received_at), None)
        request, error = _status_request_from_headers(headers)
        if error is not None:
            self._audit("request_rejected", route=_STATUS_PATH, instance_id=instance_id,
                        error=error.value)
            return (*self._transport_error(error, received_at=received_at), None)
        if request.instance_id != instance_id:
            with self._lock:
                found = self._instances.get(instance_id)
            return (*self._transport_error(
                nc.TransportErrorCode.BINDING_MISMATCH, received_at=received_at,
                request=request, binding=found[1] if found else None), None)
        if request.service_epoch is not None and request.service_epoch != self.service_epoch:
            self._inc("epoch_mismatches")
            return (*self._transport_error(
                nc.TransportErrorCode.EPOCH_MISMATCH, received_at=received_at,
                request=request), None)
        try:
            _, binding = self._resolve_binding(request)
        except ServiceTransportError as exc:
            self._audit("request_rejected", route=_STATUS_PATH, instance_id=instance_id,
                        error=exc.code.value)
            return (*self._transport_error(exc.code, received_at=received_at,
                                           request=request, binding=exc.binding), None)
        fingerprint = nc.request_fingerprint("GET", _STATUS_PATH, request)
        namespace = (request.instance_id, request.session_epoch, request.generation,
                     self.service_epoch, request.request_id)
        cached = self._replay_cache.get(namespace)
        if cached is not None:
            cached_fingerprint, cached_response = cached
            if cached_fingerprint != fingerprint:
                return (*self._transport_error(
                    nc.TransportErrorCode.REQUEST_ID_CONFLICT, received_at=received_at,
                    request=request, binding=binding), None)
            if cached_response is not None:
                self._inc("replays")
                self._inc("replays_memory")
                return HTTPStatus.OK, cached_response, {}, None
        try:
            stored = self._replay_lookup(request, fingerprint)
        except LedgerError as exc:
            if "REQUEST_ID_CONFLICT" in exc.blockers:
                return (*self._transport_error(
                    nc.TransportErrorCode.REQUEST_ID_CONFLICT, received_at=received_at,
                    request=request, binding=binding), None)
            return (*self._transport_error(
                nc.TransportErrorCode.PERSISTENCE_UNAVAILABLE, received_at=received_at,
                request=request, binding=binding), None)
        if stored:
            self._inc("replays")
            self._replay_cache.put(namespace, fingerprint, stored)
            return HTTPStatus.OK, stored, {}, None
        response = self.handle_status(request, binding, received_at)
        status, text, headers = self._response(
            response, request=request, binding=binding, received_at=received_at)
        after_send: Callable[[], None] | None = None
        if status == HTTPStatus.OK:
            after_send = self._after_send_replay(
                request, fingerprint, namespace, text, route=_STATUS_PATH, deferred=False)
        self._audit("request_served", route=_STATUS_PATH, instance_id=request.instance_id,
                    request_id=request.request_id, status=status)
        return status, text, headers, after_send


# ---------------------------------------------------------------------------
# strict request decoding (H03)
# ---------------------------------------------------------------------------

def _reject_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _nonfinite(value):
    raise ValueError("nonfinite JSON number")


def _depth_ok(value, depth: int = 0) -> bool:
    if depth > nc.MAX_NESTING:
        return False
    if type(value) is dict:
        return all(_depth_ok(item, depth + 1) for item in value.values())
    if type(value) is list:
        return all(_depth_ok(item, depth + 1) for item in value)
    return True


def _decode_request(route: str, raw: bytes):
    """Decode one typed route request; failures are typed transport errors."""
    if len(raw) > nc.MAX_REQUEST_BYTES:
        raise ServiceTransportError(nc.TransportErrorCode.REQUEST_TOO_LARGE)
    try:
        packet = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_pairs,
                            parse_constant=_nonfinite)
    except (UnicodeError, ValueError, RecursionError):
        raise ServiceTransportError(nc.TransportErrorCode.INVALID_REQUEST) from None
    if not isinstance(packet, dict) or set(packet) != _ENVELOPE_KEYS:
        raise ServiceTransportError(nc.TransportErrorCode.INVALID_REQUEST)
    if not _depth_ok(packet):
        raise ServiceTransportError(nc.TransportErrorCode.INVALID_REQUEST)
    if packet["schema_version"] != nc.TRANSPORT_SCHEMA_VERSION:
        raise ServiceTransportError(nc.TransportErrorCode.UNSUPPORTED_SCHEMA)
    expected = _ROUTE_TYPES[route]
    if packet["type"] != expected.__name__:
        raise ServiceTransportError(nc.TransportErrorCode.TYPE_MISMATCH)
    try:
        request = nc.decode(raw, expected)
        # X02-01 (NE1-ARB-LM-X01-001): the frozen request DTO validates
        # service_epoch with minimum 0, so a non-null non-positive integer
        # decodes successfully and previously surfaced as a 500 while the
        # EPOCH_MISMATCH response was constructed (its request echo demands
        # >= INITIAL_SERVICE_EPOCH). Reject it here, after decode and before
        # return, as a typed 400 INVALID_REQUEST; null keeps the poll/status
        # handshake semantics and a positive integer that is not the current
        # epoch remains 409 EPOCH_MISMATCH downstream.
        if request.service_epoch is not None:
            c.validate_integer(request.service_epoch, "service_epoch",
                               nc.INITIAL_SERVICE_EPOCH)
        return request
    except ContractError:
        raise ServiceTransportError(nc.TransportErrorCode.INVALID_REQUEST) from None


def _status_request_from_headers(headers):
    """Build the typed StatusRequest from the ten frozen headers (RULING 02)."""
    def single(name: str) -> str | None:
        values = headers.get_all(name) if hasattr(headers, "get_all") else None
        if values is None:
            value = headers.get(name)
            values = [value] if value is not None else []
        return values[0] if len(values) == 1 else None

    try:
        raw = {field: single(header) for field, header in nc.STATUS_REQUEST_HEADERS.items()}
        if any(value is None for value in raw.values()):
            return None, nc.TransportErrorCode.INVALID_REQUEST
        if raw["schema_version"] != nc.TRANSPORT_SCHEMA_VERSION:
            return None, nc.TransportErrorCode.UNSUPPORTED_SCHEMA
        if raw["type"] != "StatusRequest":
            return None, nc.TransportErrorCode.TYPE_MISMATCH
        if raw["service_epoch"] == "null":
            service_epoch = None
        elif re.fullmatch(r"[1-9][0-9]*", raw["service_epoch"]):
            service_epoch = int(raw["service_epoch"])
        else:
            return None, nc.TransportErrorCode.INVALID_REQUEST
        if not re.fullmatch(r"0|[1-9][0-9]*", raw["sequence"]):
            return None, nc.TransportErrorCode.INVALID_REQUEST
        if not _SENT_AT_PATTERN.fullmatch(raw["sent_at"]):
            return None, nc.TransportErrorCode.INVALID_REQUEST
        request = nc.StatusRequest(
            request_id=raw["request_id"], instance_id=raw["instance_id"],
            session_epoch=raw["session_epoch"], generation=raw["generation"],
            service_epoch=service_epoch, sequence=int(raw["sequence"]),
            sent_at=datetime.fromisoformat(raw["sent_at"].replace("Z", "+00:00")),
            params_sha256=raw["params_sha256"])
        return request, None
    except (ValueError, ContractError, OverflowError):
        return None, nc.TransportErrorCode.INVALID_REQUEST


# ---------------------------------------------------------------------------
# bounded loopback HTTP server
# ---------------------------------------------------------------------------

class _BoundedThreadingMixIn:
    daemon_threads = True
    block_on_close = False

    def __init__(self, *args, max_workers: int = nc.MAX_HTTP_WORKERS, **kwargs) -> None:
        if isinstance(max_workers, bool) or not isinstance(max_workers, int):
            raise ValueError("max_workers must be an integer")
        if not 1 <= max_workers <= nc.MAX_HTTP_WORKERS:
            raise ValueError("max_workers must be between 1 and MAX_HTTP_WORKERS")
        self._worker_slots = threading.BoundedSemaphore(max_workers)
        self.max_workers = max_workers
        super().__init__(*args, **kwargs)

    def process_request(self, request: socket.socket, client_address) -> None:
        if not self._worker_slots.acquire(blocking=False):
            try:
                service = self.execution_service
                body = service.busy_error_text().encode("utf-8")
                request.settimeout(0.2)
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Type: application/json\r\n"
                    + f"Content-Length: {len(body)}\r\n".encode("ascii")
                    + b"Connection: close\r\nCache-Control: no-store\r\n\r\n" + body)
            except Exception:
                pass
            finally:
                request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._worker_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._worker_slots.release()


class ExecutionHTTPServer(_BoundedThreadingMixIn, ThreadingHTTPServer):
    """Loopback-only execution server; the host must be 127.0.0.1."""

    allow_reuse_address = True

    def __init__(self, server_address, handler_class, **kwargs) -> None:
        host, port = server_address
        if host != DEFAULT_HOST:
            raise ValueError("host must be 127.0.0.1")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        super().__init__(server_address, handler_class, **kwargs)


class _ExecutionHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: ExecutionHTTPServer
    body_timeout_seconds = 3.0

    def _service(self) -> ExecutionService:
        return self.server.execution_service  # type: ignore[attr-defined]

    def _respond(self, status: int, body: str,
                 extra_headers: dict[str, str] | None = None) -> None:
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except OSError:
            pass

    def _error(self, code: nc.TransportErrorCode) -> None:
        try:
            service = self._service()
            received_at = service._now()
        except Exception:
            received_at = datetime.now(timezone.utc)
            service = None
        if service is None:
            raw = json.dumps({"error_code": code.value}).encode("utf-8")
            self.send_response(nc.TRANSPORT_ERROR_HTTP_STATUS[code])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(raw)
            except OSError:
                pass
            return
        status, body, headers = service._transport_error(code, received_at=received_at)
        self._respond(status, body, headers)

    def send_error(self, code, message=None, explain=None):
        mapping = {
            HTTPStatus.NOT_FOUND: nc.TransportErrorCode.UNKNOWN_ROUTE,
            HTTPStatus.METHOD_NOT_ALLOWED: nc.TransportErrorCode.METHOD_NOT_ALLOWED,
            HTTPStatus.REQUEST_TIMEOUT: nc.TransportErrorCode.REQUEST_TIMEOUT,
            HTTPStatus.BAD_REQUEST: nc.TransportErrorCode.INVALID_REQUEST,
        }
        self._error(mapping.get(code, nc.TransportErrorCode.INTERNAL_ERROR))

    # -- shared early transport gates (H04) ------------------------------------

    def _origin_rejected(self) -> bool:
        return bool(self.headers.get("Origin") or self.headers.get("Referer"))

    def _host_rejected(self) -> bool:
        host = self.headers.get("Host")
        if not isinstance(host, str) or not host:
            return True
        parts = host.rsplit(":", 1)
        if parts[0] != DEFAULT_HOST:
            return True
        return len(parts) == 2 and (not parts[1].isdigit() or len(parts[1]) == 0)

    def _token(self) -> str | None:
        values = self.headers.get_all(nc.EXECUTION_TOKEN_HEADER) \
            if hasattr(self.headers, "get_all") else None
        if values is None:
            value = self.headers.get(nc.EXECUTION_TOKEN_HEADER)
            values = [value] if value is not None else []
        return values[0] if len(values) == 1 else None

    def handle_one_request(self) -> None:
        try:
            self.connection.settimeout(self.body_timeout_seconds)
            super().handle_one_request()
        except (socket.timeout, TimeoutError):
            try:
                self._error(nc.TransportErrorCode.REQUEST_TIMEOUT)
            except Exception:
                pass

    def log_message(self, _format, *_args) -> None:
        return

    # -- routing ----------------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802
        service = self._service()
        received_at = service._now()
        path, _, query = self.path.partition("?")
        if path == _STATUS_PATH:
            self._error(nc.TransportErrorCode.METHOD_NOT_ALLOWED)
            return
        if path not in _POST_ROUTES:
            self._error(nc.TransportErrorCode.UNKNOWN_ROUTE if not query
                        else nc.TransportErrorCode.INVALID_REQUEST)
            return
        if query:
            self._error(nc.TransportErrorCode.INVALID_REQUEST)
            return
        if self._origin_rejected():
            self._error(nc.TransportErrorCode.ORIGIN_REJECTED)
            return
        if self._host_rejected():
            self._error(nc.TransportErrorCode.HOST_REJECTED)
            return
        header = self.headers.get("Content-Length")
        if header is None:
            self._error(nc.TransportErrorCode.INVALID_REQUEST)
            return
        try:
            length = int(header)
        except ValueError:
            self._error(nc.TransportErrorCode.INVALID_REQUEST)
            return
        if length < 0 or length > nc.MAX_REQUEST_BYTES:
            self._error(nc.TransportErrorCode.REQUEST_TOO_LARGE)
            return
        self.connection.settimeout(self.body_timeout_seconds)
        try:
            body = self.rfile.read(length) if length else b""
        except (OSError, TimeoutError):
            self._error(nc.TransportErrorCode.REQUEST_TIMEOUT)
            return
        if len(body) != length:
            self._error(nc.TransportErrorCode.REQUEST_TIMEOUT)
            return
        token = self._token()
        try:
            status, payload, headers, after_send = service.process_post(
                path, body, token=token, received_at=received_at)
            self._respond(status, payload, headers)
            self._run_after_send(after_send)
        except (ConnectionError, TimeoutError, socket.timeout):
            self._error(nc.TransportErrorCode.REQUEST_TIMEOUT)
        except Exception:
            _LOGGER.exception("request_failed")
            self._error(nc.TransportErrorCode.INTERNAL_ERROR)

    def do_GET(self) -> None:  # noqa: N802
        service = self._service()
        received_at = service._now()
        path, _, query = self.path.partition("?")
        if path != _STATUS_PATH:
            self._error(nc.TransportErrorCode.UNKNOWN_ROUTE)
            return
        if query:
            self._error(nc.TransportErrorCode.INVALID_REQUEST)
            return
        if self._origin_rejected():
            self._error(nc.TransportErrorCode.ORIGIN_REJECTED)
            return
        if self._host_rejected():
            self._error(nc.TransportErrorCode.HOST_REJECTED)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length != 0:
            self._error(nc.TransportErrorCode.INVALID_REQUEST)
            return
        encoded = sum(len(f"{name}: {value}".encode("latin-1", "ignore"))
                      for name, value in self.headers.items()
                      if name.lower().startswith("x-execution-"))
        if encoded > nc.STATUS_MAX_HEADER_BYTES:
            self._error(nc.TransportErrorCode.REQUEST_TOO_LARGE)
            return
        token = self._token()
        try:
            status, payload, headers, after_send = service.process_status(
                self.headers, token=token, received_at=received_at)
            self._respond(status, payload, headers)
            self._run_after_send(after_send)
        except (ConnectionError, TimeoutError, socket.timeout):
            self._error(nc.TransportErrorCode.REQUEST_TIMEOUT)
        except Exception:
            _LOGGER.exception("request_failed")
            self._error(nc.TransportErrorCode.INTERNAL_ERROR)

    @staticmethod
    def _run_after_send(after_send) -> None:
        """Post-send durability hook; never fails the finished response."""
        if after_send is None:
            return
        try:
            after_send()
        except Exception:
            _LOGGER.warning("after_send_failed")

    def _method_not_allowed(self) -> None:
        self._error(nc.TransportErrorCode.METHOD_NOT_ALLOWED)

    do_PUT = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_OPTIONS = _method_not_allowed
    do_TRACE = _method_not_allowed
    do_CONNECT = _method_not_allowed
    do_HEAD = _method_not_allowed


def build_execution_server(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    service: ExecutionService | None = None,
    journal: Ledger | None = None,
    max_workers: int = nc.MAX_HTTP_WORKERS,
    **service_options,
) -> ExecutionHTTPServer:
    """Build an in-process loopback execution server for the CLI and tests."""
    if host != DEFAULT_HOST:
        raise ValueError("host must be 127.0.0.1")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    if service is None:
        if journal is None:
            raise ValueError("service or journal is required")
        service = ExecutionService(journal, **service_options)
    server = ExecutionHTTPServer((DEFAULT_HOST, port), _ExecutionHandler,
                                 max_workers=max_workers)

    def _busy() -> str:
        return service._transport_error(
            nc.TransportErrorCode.BACKPRESSURE, received_at=service._now())[1]

    service.busy_error_text = _busy  # type: ignore[attr-defined]
    server.execution_service = service  # type: ignore[attr-defined]
    return server


def serve_forever(server: ExecutionHTTPServer) -> None:
    """Serve until interrupted, then close sockets and service resources."""
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()
        service = getattr(server, "execution_service", None)
        if service is not None:
            service.close()


__all__ = [
    "DEFAULT_HOST", "DEFAULT_PORT", "ExecutionService", "ExecutionHTTPServer",
    "N4BackedOrchestrator", "ProductionServiceRegistry", "PollOutcome", "ReadyOutcome",
    "CompleteOutcome", "ServiceTransportError", "vocab_blockers",
    "SERVICE_VERSION", "SERVICE_PAIRING_SCHEMA_VERSION", "DIAGNOSTIC_INSTANCE_ID",
    "ARMING_PREREQUISITES_SCHEMA_VERSION", "ArmingPrerequisites",
    "TrustedControlAssembly", "load_arming_prerequisites",
    "generate_pairing_token", "secure_pairing_directory", "write_pairing_file",
    "build_execution_server", "serve_forever",
]
