"""NE1 pairing/arming last-mile tool: inspect / build-actor-manifest / install /
prepare-arm / arm / disarm.

Implements RULING 02/03 of decision NE1-ARB-PAIRING-LASTMILE-001 and RULING
01/03/04/05 (LM-D rows) of decision NE1-ARB-ARMING-001
(data/maintenance_artifacts/infiny-arming-mechanism-20260918-r1/decision.md).
Tool version ``ne1-pairing-20260918-arming-v1b`` (v1b = ARM-W2-I-X01 fix:
UTC texts unified to the frozen client ``Z``-suffix codec; plan_id validated
as a hash-style 64-hex identifier in plan/request/state documents).

Role split (R2.1): the SERVICE process generates tokens; the USER who runs
InfiniTrader executes ``install`` personally; the main session may deliver
reviewed commands and read NON-SECRET descriptions/receipts. This tool never
prints, logs, hashes or copies a token value: the token enters this process
only as file bytes read from the service pairing directory and leaves only as
file bytes written into the client store credential slot. ``--source`` takes a
file PATH reference; there is no ``--token``, no stdin/env token input, and no
secret-reading dry-run.

Frozen file contracts consumed here (decision R1.3/R1.4/R2.2):
  - client store layout 2 with ``identity.json`` (``ne1_client_identity_v1``)
    and ``pairing-request.json`` (``ne1_pairing_request_v1``);
  - service pairing file ``<pairing-dir>/<instance_id>.token`` with strict
    schema ``ne1_service_pairing_v2`` (the legacy two-line format is refused);
  - non-secret ``service-start.json`` = the frozen LM-S launcher's verbatim
    ``execution_started`` status line (service identity under ``instance_id``
    as ``execution-<16hex>``; per-instance ``session_epoch`` + ``token_file``
    references under the ``pairings`` list; reading contract corrected
    against the implemented frozen service release
    ``ne1-service-20260917-pairing-v2``);
  - client credential slot ``pairing.json`` (``ne1_client_pairing_v1``),
    written under ``pairing-transfer.lock`` via fsync + atomic replace,
    with the published file's DACL re-verified before the receipt exists.

Windows DACL verification is a hard precondition (R2.4 / AR4.3): the
current-user SID must own the secret-bearing objects and every allow-ACE
trustee must be within {current user, OWNER_RIGHTS, SYSTEM, Administrators};
POSIX mode bits are never used as proof. Unprovable platform support rejects
the operation. The same discipline covers the non-secret arming slots
(integrity) and the private ``arming-challenge.json`` / ``arming.json``
(secrecy): the proof always happens BEFORE any secret byte enters this
process or lands on disk (AR4.3), and the published private credential's
DACL is re-verified after the atomic replace.

Arming face (NE1-ARB-ARMING-001, AR1.1/AR1.3/AR4/AR5): ``prepare-arm``
publishes a non-secret candidate proposal into the store slot
(``PREPARATION_SUBMITTED``); ``arm`` is the USER-ONLY interactive action -
it refuses any non-interactive stdin (redirect/pipe/no console), has NO
``--yes``/env prefill/auto-confirm surface, displays the complete scope and
requires the live phrase ``ARM <last 8 hex of the current arming
request_id>`` before atomically delivering the one-shot credential
``arming.json`` (``ARM_SUBMITTED_AWAITING_CLIENT``, never ARMED);
``disarm`` publishes a scoped revocation request (``DISARM_REQUESTED``).
``inspect`` additionally reports the non-secret arming state and the
EXISTENCE of private slots only (never opens or hashes them). The random
challenge value is never displayed, echoed, receipted or hashed by this
tool; it moves file bytes -> memory -> file bytes only.

Exit codes: 0 atomic transfer/publication succeeded (status is the stage
fact - TRANSFERRED_AWAITING_HANDSHAKE / PREPARATION_SUBMITTED /
ARM_SUBMITTED_AWAITING_CLIENT / DISARM_REQUESTED; never a trading-
capability claim), 2 validation/security rejection, 3 I/O or publish
failure. ``--help`` performs no runtime resource reads; ``inspect``
performs zero writes.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import secrets as _secrets
import struct
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PureWindowsPath

TOOL_VERSION = "ne1-pairing-20260918-arming-v1b"
STORE_LAYOUT_VERSION = 2
STORE_ROOT_NAME = ".ak_execution"

IDENTITY_SCHEMA = "ne1_client_identity_v1"
REQUEST_SCHEMA = "ne1_pairing_request_v1"
SERVICE_PAIRING_SCHEMA = "ne1_service_pairing_v2"
CLIENT_PAIRING_SCHEMA = "ne1_client_pairing_v1"
RECEIPT_SCHEMA = "ne1_pairing_transfer_receipt_v1"
ADAPTER_SCHEMA = "ne1_adapter_registration_v1"

REQUEST_FIELDS = frozenset({
    "schema_version", "request_id", "active", "created_at",
    "instance_id", "session_epoch", "generation", "process_instance_id",
    "deployment_key", "strategy_id", "adapter_version", "client_file_sha256",
    "store_layout_version", "store_directory",
    "exchange", "instrument_id", "hedge_flag", "execution_api_url",
    "params_sha256",
})
IDENTITY_FIELDS = frozenset({
    "schema_version", "instance_id", "deployment_key", "strategy_id",
    "created_at",
})
SERVICE_PAIRING_FIELDS = frozenset({
    "schema_version", "instance_id", "session_epoch", "service_instance_id",
    "service_epoch", "contract_lock_sha256", "diagnostic_only", "token",
})
CLIENT_PAIRING_FIELDS = frozenset({
    "schema_version", "request_id", "request_sha256", "instance_id",
    "session_epoch", "generation", "deployment_key", "strategy_id",
    "params_sha256", "client_file_sha256", "execution_origin",
    "service_instance_id", "service_epoch", "contract_lock_sha256", "token",
})
BINDING_TEMPLATE_FIELDS = frozenset({
    "actor_id", "account_key", "strategy_key", "strategy_id", "trading_day",
    "exchange", "instrument_id", "hedge_flag",
})

HEX32 = re.compile(r"[0-9a-f]{32}")
HEX64 = re.compile(r"[0-9a-f]{64}")
ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}")
# RULING X01-01 (NE1-ARB-LM-X01-001): the one legal service identity syntax
# is exactly ``execution-`` + 16 lowercase ASCII hex characters (26 chars
# total, case-sensitive, leading zeros kept). This regex is deliberately
# file-local and never reused for generic identifier checks.
SERVICE_INSTANCE_ID = re.compile(r"execution-[0-9a-f]{16}")

PAIRING_FILE_NAME = "pairing.json"
REQUEST_FILE_NAME = "pairing-request.json"
IDENTITY_FILE_NAME = "identity.json"
TRANSFER_LOCK_NAME = "pairing-transfer.lock"

# ---------------------------------------------------------------------------
# NE1-ARB-ARMING-001 RULING 04: fixed store-slot file names and frozen
# schema_version strings for the arming-v1 extension. Nothing outside this
# whitelist is ever read or written by the arming face.
# ---------------------------------------------------------------------------

ARMING_PROPOSAL_FILE = "arming-proposal.json"
ARMING_PLAN_FILE = "arming-plan.json"
ARMING_REQUEST_FILE = "arming-request.json"
ARMING_CHALLENGE_FILE = "arming-challenge.json"
ARMING_CREDENTIAL_FILE = "arming.json"
DISARM_FILE = "disarm.json"
ARMING_STATE_FILE = "arming-state.json"
ARMING_TRANSFER_LOCK_NAME = "arming-transfer.lock"

ARMING_PROPOSAL_SCHEMA = "ne1_arming_proposal_v1"
ARMING_PLAN_SCHEMA = "ne1_arming_plan_v1"
ARMING_REQUEST_SCHEMA = "ne1_arming_request_v1"
ARMING_CHALLENGE_SCHEMA = "ne1_arming_challenge_v1"
ARMING_CREDENTIAL_SCHEMA = "ne1_client_arming_v1"
DISARM_SCHEMA = "ne1_client_disarm_v1"
ARMING_STATE_SCHEMA = "ne1_arming_state_v1"
ARMING_REGISTRATIONS_SCHEMA = "ne1_arming_registrations_v1"
ARMING_PREREQUISITES_SCHEMA = "ne1_arming_prerequisites_v1"
ARMING_RECEIPT_SCHEMA = "ne1_arming_tool_receipt_v1"
PROPOSAL_CANDIDATE_SCHEMA = "ne1_arming_proposal_candidate_v1"

ARMING_SCOPE_FIELDS = frozenset({
    "pairing_request_id", "pairing_request_sha256", "instance_id",
    "session_epoch", "generation", "process_instance_id", "deployment_key",
    "strategy_id", "client_file_sha256", "adapter_version",
    "contract_lock_sha256", "execution_origin", "pairing_params_sha256",
})
ARMING_PROPOSAL_FIELDS = frozenset({
    "schema_version", "proposal_id", "scope", "binding", "operator_action_id",
    "authorization_id", "permit_id", "execution_mode", "risk_policy_id",
    "max_native_sends", "max_native_cancels", "created_at",
})
ARMING_PLAN_FIELDS = frozenset({
    "schema_version", "plan_id", "proposal_id", "proposal_sha256", "scope",
    "binding", "authorization_id", "permit_id", "target_params_sha256",
    "effective_overrides", "created_at",
})
ARMING_REQUEST_FIELDS = frozenset({
    "schema_version", "request_id", "plan_id", "plan_sha256", "scope",
    "binding", "authorization_id", "permit_id", "target_params_sha256",
    "service_instance_id", "service_epoch", "created_at", "expires_at",
    "active",
})
ARMING_CHALLENGE_FIELDS = frozenset({
    "schema_version", "request_id", "request_sha256", "challenge",
    "issued_at", "expires_at", "writer_sid",
})
ARMING_CREDENTIAL_FIELDS = frozenset({
    "schema_version", "arm_id", "request_id", "request_sha256", "plan_id",
    "scope", "binding", "target_params_sha256", "authorization_id",
    "authorization_record_sha256", "permit_id", "permit_record_sha256",
    "service_instance_id", "service_epoch", "expected_control_revision",
    "prerequisites_id", "prerequisites_sha256", "challenge", "user_sid",
    "action_source", "explicit_user_action", "confirmed_at", "expires_at",
})
DISARM_FIELDS = frozenset({
    "schema_version", "disarm_id", "scope", "arm_request_id", "arm_id",
    "requested_at", "user_sid", "reason",
})
ARMING_STATE_FIELDS = frozenset({
    "schema_version", "scope", "plan_id", "arm_request_id", "arm_id", "state",
    "consumed", "revoked", "effective_enable_trading", "target_params_sha256",
    "service_instance_id", "service_epoch", "control_revision", "expires_at",
    "local_gate_closed", "service_gate_closed", "unresolved_dispatch_count",
    "count_complete", "updated_at", "reason_code",
})
REGISTRATIONS_FIELDS = frozenset({
    "schema_version", "binding", "target_params_sha256",
    "contract_lock_sha256", "authorization_id", "permit_id", "entries",
})
REGISTRATION_ENTRY_FIELDS = frozenset({
    "record_type", "record_path", "record_sha256", "manifest_path",
    "manifest_sha256", "receipt_path", "receipt_sha256",
})
PREREQUISITES_FIELDS = frozenset({
    "schema_version", "prerequisites_id", "created_at", "expires_at",
    "contract_lock_sha256", "client_file_sha256", "service_file_sha256",
    "tool_file_sha256", "evidence_refs",
})
PREREQUISITE_REF_FIELDS = frozenset({"kind", "path", "sha256"})
ARMING_RECEIPT_FIELDS = frozenset({
    "operation", "status", "action_source", "scope", "request_id", "arm_id",
    "disarm_id", "plan_id", "authorization_id", "permit_id",
    "target_params_sha256", "client_file_sha256", "contract_lock_sha256",
    "service_instance_id", "service_epoch", "control_revision",
    "prerequisites_id", "prerequisites_sha256", "user_sid",
    "local_gate_closed", "service_gate_closed", "recorded_at", "reason_code",
})
PROPOSAL_CANDIDATE_FIELDS = frozenset({
    "schema_version", "binding", "operator_action_id", "authorization_id",
    "permit_id", "risk_policy_id", "max_native_sends", "max_native_cancels",
})
# AR2.1: the only five target-plan override fields ever accepted.
ARMING_OVERRIDE_FIELDS = frozenset({
    "execution_mode", "risk_policy_id", "authorization_id",
    "max_native_sends", "max_native_cancels",
})

STATUS_TRANSFERRED = "TRANSFERRED_AWAITING_HANDSHAKE"
ARM_STATUS_PREPARED = "PREPARATION_SUBMITTED"
ARM_STATUS_SUBMITTED = "ARM_SUBMITTED_AWAITING_CLIENT"
DISARM_STATUS_REQUESTED = "DISARM_REQUESTED"

ARM_ACTION_SOURCE = "USER_LOCAL_CONSOLE"   # AR1.2: the only action source
ARM_DISARM_REASON = "USER_DISARM"          # AR4.2: fixed disarm enum
ARM_MAX_WINDOW_SECONDS = 300               # AR4.2 request/challenge window
ARM_PHRASE_PREFIX = "ARM"                  # AR1.3 confirmation phrase prefix
ARM_PHRASE_ATTEMPTS = 3                    # live typed attempts, never auto
ARMING_JSON_LIMIT = 64 * 1024              # AR4.1 max JSON size for arming slots

PREREQUISITE_KINDS = frozenset({
    "RELEASE_REVIEW", "S3_ZERO_NATIVE", "MANUAL_TAKEOVER_APPROVAL",
    "SINGLE_ORDER_APPROVAL", "NATIVE_ASSEMBLY_ACCEPTANCE",
})
REGISTRATION_RECORD_TYPES = frozenset({
    "ActorRegistration", "NativeAuthorization", "NativePilotPermit",
    "NativeBaselineEvidence", "NativeRouteEvidence",
    "NativeSettlementEvidence", "ClientThreadEvidence",
})
REGISTRATIONS_REQUIRED_TYPES = frozenset({
    "ActorRegistration", "NativeAuthorization", "NativePilotPermit",
})
REGISTRATIONS_EVIDENCE_TYPES = frozenset({
    "NativeBaselineEvidence", "NativeRouteEvidence",
    "NativeSettlementEvidence", "ClientThreadEvidence",
})

# Well-known trustee SIDs permitted on private pairing objects: the current
# user, OWNER_RIGHTS (only meaningful while the owner check passes), LOCAL
# SYSTEM and the Administrators group. "Users"/"Everyone"/any other grant is
# an ordinary-other-principal access and rejects the operation (R2.4).
SID_SYSTEM = "S-1-5-18"
SID_ADMINISTRATORS = "S-1-5-32-544"
SID_OWNER_RIGHTS = "S-1-3-4"

# write preflight bits: GENERIC_WRITE | FILE_WRITE_DATA | FILE_APPEND_DATA |
# FILE_WRITE_ATTRIBUTES | WRITE_DAC | WRITE_OWNER
_WRITE_MASK = 0x40000000 | 0x00000002 | 0x00000004 | 0x00000100 | 0x00040000 | 0x00080000
_ACE_TYPE_ALLOW = 0
_ACE_FLAG_INHERITED = 0x10


class PairingError(ValueError):
    """Security/validation rejection; message doubles as the machine code."""


class PairingPublishError(Exception):
    """I/O or publication failure after validation (exit code 3)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise PairingError(code)


# ---------------------------------------------------------------------------
# local-path safety (same discipline as scripts/install_infiny_execution.py)
# ---------------------------------------------------------------------------

def _safe_local_path(value: Path, *, allow_missing: bool = True) -> Path:
    raw = str(value)
    windows = PureWindowsPath(raw)
    if not raw or windows.drive.startswith("\\\\"):
        raise PairingError("LOCAL_PATH_REQUIRED")
    reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", "CLOCK$"}
    reserved.update(f"{prefix}{number}" for prefix in ("COM", "LPT")
                    for number in "123456789")
    for part in windows.parts:
        if part in (windows.anchor, ".", ".."):
            continue
        stem = part.split(".", 1)[0].rstrip(" ").upper()
        if (part.rstrip(" .") != part or stem in reserved
                or any(ord(char) < 32 or ord(char) == 127 or char in '<>:"|?*'
                       for char in part)):
            raise PairingError("RESERVED_OR_INVALID_PATH")
    absolute = Path(value).absolute()
    for ancestor in reversed((absolute, *absolute.parents)):
        if ancestor.is_symlink() or (hasattr(ancestor, "is_junction")
                                     and ancestor.is_junction()):
            raise PairingError("LINK_OR_JUNCTION_REJECTED")
    if not allow_missing and not absolute.exists():
        raise PairingError("PATH_DOES_NOT_EXIST")
    return absolute


def _normalized(value: Path | str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(str(value))))


def _require_file(path: Path, label: str) -> Path:
    safe = _safe_local_path(path, allow_missing=False)
    _require(safe.is_file() and not safe.is_symlink(), f"{label}_INVALID")
    return safe


def _require_directory(path: Path, label: str) -> Path:
    safe = _safe_local_path(path, allow_missing=False)
    _require(safe.is_dir() and not safe.is_symlink(), f"{label}_INVALID")
    return safe


# ---------------------------------------------------------------------------
# small shared helpers
# ---------------------------------------------------------------------------

def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=True, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _hash_bytes(payload: bytes) -> str:
    import hashlib
    return hashlib.sha256(payload).hexdigest()


def _hash_file(path: Path) -> str:
    return _hash_bytes(path.read_bytes())


def _utc_text(moment: datetime) -> str:
    """Aware-UTC ISO text in the FROZEN CLIENT codec form (ARM-W2-I-X01):
    ``AKExecutionBridge._iso`` always emits the ``Z`` suffix and the client's
    ``_utc_text`` only accepts ``Z`` — never a ``+00:00`` suffix."""
    return moment.astimezone(timezone.utc).isoformat(
        timespec="microseconds").replace("+00:00", "Z")


def _now_iso() -> str:
    return _utc_text(datetime.now(timezone.utc))


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _load_small_json(path: Path, *, limit: int = 32 * 1024,
                     code_prefix: str = "PAIRING_JSON") -> tuple[dict, bytes]:
    raw = path.read_bytes()
    _require(len(raw) <= limit, f"{code_prefix}_TOO_LARGE")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PairingError(f"{code_prefix}_FORMAT_INVALID") from exc
    _require(isinstance(value, dict), f"{code_prefix}_FORMAT_INVALID")
    return value, raw


def _atomic_write_new(path: Path, payload: bytes) -> None:
    """Exclusive-create a new file (temp + fsync + hardlink publication)."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=str(path.parent),
                                         prefix=f".{path.name}.",
                                         delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _atomic_publish(path: Path, payload: bytes) -> None:
    """Protected temp file in the target directory + fsync + atomic replace.

    Kept as a module-level seam: tests substitute it to prove pre/post
    verification around the publication point.
    """
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=str(path.parent),
                                         prefix=f".{path.name}.",
                                         delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Windows DACL verification (R2.4); ctypes only, no subprocess, no token data
# ---------------------------------------------------------------------------

class _WindowsSecurity:
    """Lazy ctypes binding; every probe returns facts, never file content."""

    def __init__(self) -> None:
        self._advapi32 = None
        self._kernel32 = None
        self._current_user_sid = None

    def _bind(self):
        if self._advapi32 is not None:
            return
        if os.name != "nt" or sys.platform != "win32":
            raise PairingError("PLATFORM_ACL_UNSUPPORTED")
        from ctypes import wintypes
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.OpenProcessToken.argtypes = (
            wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
        kernel32.OpenProcessToken.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.LocalFree.argtypes = (wintypes.HANDLE,)
        kernel32.LocalFree.restype = wintypes.HANDLE
        advapi32.GetTokenInformation.argtypes = (
            wintypes.HANDLE, wintypes.INT, wintypes.LPVOID, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD))
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.ConvertSidToStringSidW.argtypes = (
            ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR))
        advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
        advapi32.GetNamedSecurityInfoW.argtypes = (
            wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p))
        advapi32.GetNamedSecurityInfoW.restype = ctypes.c_ulong
        advapi32.GetAce.argtypes = (
            ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))
        advapi32.GetAce.restype = wintypes.BOOL
        self._advapi32 = advapi32
        self._kernel32 = kernel32
        self._wintypes = wintypes

    def _sid_string(self, sid_pointer: int) -> str:
        buffer = self._wintypes.LPWSTR()
        if not self._advapi32.ConvertSidToStringSidW(
                ctypes.c_void_p(sid_pointer), ctypes.byref(buffer)):
            raise PairingError("PLATFORM_ACL_UNSUPPORTED")
        try:
            return buffer.value or ""
        finally:
            self._kernel32.LocalFree(ctypes.cast(buffer, self._wintypes.HANDLE))

    def current_user_sid(self) -> str:
        if self._current_user_sid is not None:
            return self._current_user_sid
        self._bind()
        token = self._wintypes.HANDLE()
        if not self._kernel32.OpenProcessToken(
                self._kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            raise PairingError("PLATFORM_ACL_UNSUPPORTED")
        try:
            size = self._wintypes.DWORD(0)
            self._advapi32.GetTokenInformation(token, 1, None, 0,
                                               ctypes.byref(size))
            buffer = ctypes.create_string_buffer(max(size.value, 1))
            if not self._advapi32.GetTokenInformation(
                    token, 1, buffer, size, ctypes.byref(size)):
                raise PairingError("PLATFORM_ACL_UNSUPPORTED")
            sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
            if not sid_pointer:
                raise PairingError("PLATFORM_ACL_UNSUPPORTED")
            self._current_user_sid = self._sid_string(sid_pointer)
            return self._current_user_sid
        finally:
            self._kernel32.CloseHandle(token)

    def describe(self, path: Path) -> dict:
        """Owner SID + allow-ACE trustees for one filesystem object."""
        self._bind()
        owner = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        result = self._advapi32.GetNamedSecurityInfoW(
            str(path), 1, 0x1 | 0x4, ctypes.byref(owner), None,
            ctypes.byref(dacl), None, None)
        if result != 0:
            raise PairingError("PLATFORM_ACL_UNSUPPORTED")
        entry = {"path": str(path), "owner_sid": None, "allow_trustees": [],
                 "write_granted": False, "dacl_present": bool(dacl.value)}
        if owner.value:
            entry["owner_sid"] = self._sid_string(owner.value)
        if dacl.value:
            header = (ctypes.c_ubyte * 8).from_address(dacl.value)
            ace_count = struct.unpack_from("<H", header, 4)[0]
            for index in range(ace_count):
                ace_pointer = ctypes.c_void_p()
                if not self._advapi32.GetAce(dacl.value, index,
                                             ctypes.byref(ace_pointer)):
                    raise PairingError("PLATFORM_ACL_UNSUPPORTED")
                raw = ctypes.string_at(ace_pointer.value, 8)
                ace_type, mask = raw[0], struct.unpack_from("<I", raw, 4)[0]
                if ace_type != _ACE_TYPE_ALLOW:
                    continue  # deny ACEs only restrict access
                entry["allow_trustees"].append(
                    self._sid_string(ace_pointer.value + 8))
                if mask & _WRITE_MASK:
                    entry["write_granted"] = True
        return entry


_SECURITY = _WindowsSecurity()


def verify_private_access(path: Path, *, need_write: bool) -> dict:
    """R2.4 hard precondition. Returns a non-secret verification record.

    Rules: DACL must be present (a NULL DACL grants everyone); the owner must
    be the current user or the Administrators group; every allow-ACE trustee
    must be within {current user, OWNER_RIGHTS, SYSTEM, Administrators}. POSIX
    chmod bits are never consulted.
    """
    try:
        current = _SECURITY.current_user_sid()
        described = _SECURITY.describe(path)
    except PairingError:
        raise
    except OSError as exc:
        raise PairingError("PLATFORM_ACL_UNSUPPORTED") from exc
    allowed = {current, SID_SYSTEM, SID_ADMINISTRATORS, SID_OWNER_RIGHTS}
    owner_ok = described["owner_sid"] in (current, SID_ADMINISTRATORS)
    trustees_ok = set(described["allow_trustees"]) <= allowed
    write_ok = (not need_write) or described["write_granted"]
    record = {"platform": "windows", "owner_ok": bool(owner_ok),
              "allowed_trustees_only": bool(trustees_ok),
              "dacl_present": described["dacl_present"],
              "write_access_granted": bool(described["write_granted"])}
    _require(described["dacl_present"] and owner_ok and trustees_ok and write_ok,
             "PAIRING_ACL_RESTRICTED")
    return record


# ---------------------------------------------------------------------------
# strict record validators (frozen contracts; unknown keys always rejected)
# ---------------------------------------------------------------------------

def _is_hex32(value: object) -> bool:
    return type(value) is str and HEX32.fullmatch(value) is not None


def _is_hex64(value: object) -> bool:
    return type(value) is str and HEX64.fullmatch(value) is not None


def _is_identifier(value: object) -> bool:
    return type(value) is str and IDENTIFIER.fullmatch(value) is not None


def _is_service_instance_id(value: object) -> bool:
    """RULING X01-01 (NE1-ARB-LM-X01-001): the service identity is exactly
    ``execution-`` + 16 lowercase ASCII hex characters. No trim, casefold,
    prefixing, truncation, UUID parsing or compatibility conversion; bare
    hex32, ``execution-<32hex>``, other prefixes, whitespace/newlines, path
    characters and non-strings are all rejected. Used only for the three
    service-identity entrances (service pairing file, client pairing.json,
    service-start top-level ``instance_id``); the generic identifier check
    is unchanged."""
    return type(value) is str and SERVICE_INSTANCE_ID.fullmatch(value) is not None


def _is_nonempty_str(value: object) -> bool:
    return type(value) is str and 0 < len(value) <= 256


def _is_strategy_id(value: object) -> bool:
    return type(value) is int and not isinstance(value, bool) and value >= 0


def validate_identity(record: dict) -> dict:
    _require(set(record) == IDENTITY_FIELDS, "STORE_IDENTITY_INVALID")
    _require(record["schema_version"] == IDENTITY_SCHEMA, "STORE_IDENTITY_INVALID")
    for key in ("instance_id",):
        _require(_is_hex32(record[key]), "STORE_IDENTITY_INVALID")
    _require(_is_hex64(record["deployment_key"]), "STORE_IDENTITY_INVALID")
    _require(_is_strategy_id(record["strategy_id"]), "STORE_IDENTITY_INVALID")
    _require(_is_nonempty_str(record["created_at"]), "STORE_IDENTITY_INVALID")
    return record


def validate_request(record: dict) -> dict:
    _require(set(record) == REQUEST_FIELDS, "PAIRING_REQUEST_FORMAT_INVALID")
    _require(record["schema_version"] == REQUEST_SCHEMA,
             "PAIRING_REQUEST_FORMAT_INVALID")
    _require(type(record["active"]) is bool, "PAIRING_REQUEST_FORMAT_INVALID")
    for key in ("request_id", "instance_id", "session_epoch", "generation",
                "process_instance_id"):
        _require(_is_hex32(record[key]), "PAIRING_REQUEST_FORMAT_INVALID")
    for key in ("deployment_key", "client_file_sha256", "params_sha256"):
        _require(_is_hex64(record[key]), "PAIRING_REQUEST_FORMAT_INVALID")
    _require(_is_strategy_id(record["strategy_id"]),
             "PAIRING_REQUEST_FORMAT_INVALID")
    _require(_is_nonempty_str(record["adapter_version"]),
             "PAIRING_REQUEST_FORMAT_INVALID")
    _require(type(record["store_layout_version"]) is int
             and record["store_layout_version"] == STORE_LAYOUT_VERSION,
             "STORE_LAYOUT_UNSUPPORTED")
    _require(_is_nonempty_str(record["created_at"]),
             "PAIRING_REQUEST_FORMAT_INVALID")
    _require(_is_nonempty_str(record["store_directory"]),
             "PAIRING_REQUEST_FORMAT_INVALID")
    for key in ("exchange", "instrument_id", "hedge_flag"):
        _require(_is_identifier(record[key]), "PAIRING_REQUEST_FORMAT_INVALID")
    _require(type(record["execution_api_url"]) is str,
             "PAIRING_REQUEST_FORMAT_INVALID")
    return record


def execution_origin(url: object) -> str:
    """Normalized origin (scheme/host/explicit port) from the validated URL.

    R1.4/R2.2: the execution URL must be an explicit loopback address with no
    userinfo, query or fragment; the origin never carries those parts.
    """
    from urllib.parse import urlsplit
    _require(type(url) is str and 0 < len(url) <= 256, "PAIRING_ORIGIN_INVALID")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise PairingError("PAIRING_ORIGIN_INVALID") from exc
    _require(parsed.scheme == "http", "PAIRING_ORIGIN_INVALID")
    _require(parsed.username is None and parsed.password is None,
             "PAIRING_ORIGIN_INVALID")
    _require(not parsed.query and not parsed.fragment, "PAIRING_ORIGIN_INVALID")
    _require(parsed.hostname == "127.0.0.1", "PAIRING_ORIGIN_INVALID")
    _require(port is not None and 1 <= port <= 65535, "PAIRING_ORIGIN_INVALID")
    return f"http://127.0.0.1:{port}"


def validate_service_pairing(record: dict) -> dict:
    _require(set(record) == SERVICE_PAIRING_FIELDS, "PAIRING_FORMAT_INVALID")
    _require(record["schema_version"] == SERVICE_PAIRING_SCHEMA,
             "PAIRING_FORMAT_INVALID")
    _require(_is_hex32(record["instance_id"]) or record["instance_id"] == "unregistered",
             "PAIRING_FORMAT_INVALID")
    _require(type(record["service_epoch"]) is int
             and not isinstance(record["service_epoch"], bool)
             and record["service_epoch"] >= 1, "PAIRING_FORMAT_INVALID")
    # RULING X01-01/X01-02 (NE1-ARB-LM-X01-001): the service identity is
    # exactly ``execution-`` + 16 lowercase hex - not a bare 32-hex UUID,
    # not any opaque identifier. The value is still mapped/written as-is.
    _require(_is_service_instance_id(record["service_instance_id"]),
             "PAIRING_FORMAT_INVALID")
    _require(_is_hex64(record["contract_lock_sha256"]), "PAIRING_FORMAT_INVALID")
    _require(type(record["diagnostic_only"]) is bool, "PAIRING_FORMAT_INVALID")
    _require(_is_hex64(record["token"]), "PAIRING_FORMAT_INVALID")
    session = record["session_epoch"]
    if record["instance_id"] == "unregistered":
        _require(session is None and record["diagnostic_only"] is True,
                 "PAIRING_FORMAT_INVALID")
    else:
        _require(_is_hex32(session) and record["diagnostic_only"] is False,
                 "PAIRING_FORMAT_INVALID")
    return record


def validate_client_pairing(record: dict) -> dict:
    _require(set(record) == CLIENT_PAIRING_FIELDS, "PAIRING_TARGET_INVALID")
    _require(record["schema_version"] == CLIENT_PAIRING_SCHEMA,
             "PAIRING_TARGET_INVALID")
    for key in ("request_id", "instance_id", "session_epoch", "generation"):
        _require(_is_hex32(record[key]), "PAIRING_TARGET_INVALID")
    # RULING X01-02/X01-03: existing pairing.json slots are held to the same
    # exact service-ID syntax; a non-compliant slot rejects with its original
    # bytes preserved and is never silently overwritten.
    _require(_is_service_instance_id(record["service_instance_id"]),
             "PAIRING_TARGET_INVALID")
    for key in ("request_sha256", "deployment_key", "params_sha256",
                "client_file_sha256", "contract_lock_sha256", "token"):
        _require(_is_hex64(record[key]), "PAIRING_TARGET_INVALID")
    _require(_is_strategy_id(record["strategy_id"]), "PAIRING_TARGET_INVALID")
    _require(type(record["service_epoch"]) is int
             and not isinstance(record["service_epoch"], bool)
             and record["service_epoch"] >= 1, "PAIRING_TARGET_INVALID")
    _require(_is_nonempty_str(record["execution_origin"]),
             "PAIRING_TARGET_INVALID")
    return record


def validate_service_start(record: dict) -> dict:
    """Non-secret service-start.json (R2.2): the frozen LM-S launcher's
    ``execution_started`` status line, saved verbatim by the main session.

    Reading contract CORRECTED against the implemented frozen launcher
    (scripts/run_infiny_execution.py, release ``ne1-service-20260917-pairing-v2``):
    the service identity key is ``instance_id`` (exactly ``execution-`` +
    16 lowercase hex per RULING X01-01 of NE1-ARB-LM-X01-001; syntax never
    replaces the source/start value comparison); per-instance session and
    token-file references live in the ``pairings`` LIST with entries
    ``{instance_id, session_epoch, token_file, diagnostic_only}``;
    ``execution_enabled`` must be false. The file may carry additional
    non-secret status fields (port, actors, audit_log, ...) which are not
    re-validated here; token values must never appear anywhere in it (the
    caller cross-checks that after loading the source credential).
    """
    for key in ("event", "service_version", "contract_lock_sha256", "pairing_dir",
                "service_epoch", "instance_id", "execution_enabled", "pairings"):
        _require(key in record, "PAIRING_SERVICE_START_INVALID")
    _require(record["event"] == "execution_started",
             "PAIRING_SERVICE_START_INVALID")
    _require(record.get("host") == "127.0.0.1", "PAIRING_SERVICE_START_INVALID")
    _require(record["execution_enabled"] is False,
             "PAIRING_SERVICE_START_INVALID")
    _require(_is_nonempty_str(record["service_version"]),
             "PAIRING_SERVICE_START_INVALID")
    _require(_is_hex64(record["contract_lock_sha256"]),
             "PAIRING_SERVICE_START_INVALID")
    _require(_is_nonempty_str(record["pairing_dir"]),
             "PAIRING_SERVICE_START_INVALID")
    _require(type(record["service_epoch"]) is int
             and not isinstance(record["service_epoch"], bool)
             and record["service_epoch"] >= 1, "PAIRING_SERVICE_START_INVALID")
    # RULING X01-02: the TOP-LEVEL service identity is exactly
    # ``execution-`` + 16 lowercase hex; the ``pairings[*].instance_id``
    # entries below remain client hex32 identities and are NOT touched.
    _require(_is_service_instance_id(record["instance_id"]),
             "PAIRING_SERVICE_START_INVALID")
    pairings = record["pairings"]
    _require(type(pairings) is list and pairings, "PAIRING_SERVICE_START_INVALID")
    normalized = {}
    for entry in pairings:
        _require(type(entry) is dict, "PAIRING_SERVICE_START_INVALID")
        _require(set(entry) == {"instance_id", "session_epoch", "token_file",
                                "diagnostic_only"},
                 "PAIRING_SERVICE_START_INVALID")
        instance_id = entry["instance_id"]
        _require(_is_hex32(instance_id) or instance_id == "unregistered",
                 "PAIRING_SERVICE_START_INVALID")
        _require(_is_nonempty_str(entry["token_file"]),
                 "PAIRING_SERVICE_START_INVALID")
        _require(type(entry["diagnostic_only"]) is bool,
                 "PAIRING_SERVICE_START_INVALID")
        session_epoch = entry["session_epoch"]
        if instance_id == "unregistered":
            _require(session_epoch is None and entry["diagnostic_only"] is True,
                     "PAIRING_SERVICE_START_INVALID")
        else:
            _require(_is_hex32(session_epoch) and entry["diagnostic_only"] is False,
                     "PAIRING_SERVICE_START_INVALID")
        _require(instance_id not in normalized, "PAIRING_SERVICE_START_INVALID")
        normalized[instance_id] = {"session_epoch": session_epoch,
                                   "token_path": entry["token_file"]}
    # NE1-ARB-ARMING-001 AR3.3: the arming-era service start line may carry
    # exactly one additional top-level field ``arming_prerequisites`` (strict
    # object or null). Absent/null keeps the legacy pairing-era shape valid
    # for install; the arm command REQUIRES a present, matching value.
    prerequisites = record.get("arming_prerequisites", None)
    if prerequisites is not None:
        _require(type(prerequisites) is dict
                 and set(prerequisites) == {"prerequisites_id",
                                            "prerequisites_sha256",
                                            "client_file_sha256"},
                 "PAIRING_SERVICE_START_INVALID")
        _require(_is_hex32(prerequisites["prerequisites_id"]),
                 "PAIRING_SERVICE_START_INVALID")
        _require(_is_hex64(prerequisites["prerequisites_sha256"]),
                 "PAIRING_SERVICE_START_INVALID")
        _require(_is_hex64(prerequisites["client_file_sha256"]),
                 "PAIRING_SERVICE_START_INVALID")
    return {"service_version": record["service_version"],
            "service_instance_id": record["instance_id"],
            "service_epoch": record["service_epoch"],
            "contract_lock_sha256": record["contract_lock_sha256"],
            "pairing_dir": record["pairing_dir"],
            "instances": normalized,
            "arming_prerequisites": prerequisites}


# ---------------------------------------------------------------------------
# store helpers
# ---------------------------------------------------------------------------

def _deterministic_slot(strategy_directory: Path, strategy_id: int) -> Path:
    return strategy_directory / STORE_ROOT_NAME / f"strategy-{strategy_id}"


def _load_store_identity(store: Path) -> dict:
    identity_path = store / IDENTITY_FILE_NAME
    _require(identity_path.is_file() and not identity_path.is_symlink(),
             "STORE_IDENTITY_MISSING")
    record, _ = _load_small_json(identity_path, code_prefix="STORE_IDENTITY")
    return validate_identity(record)


def _load_client_request(path: Path) -> tuple[dict, bytes]:
    record, raw = _load_small_json(path, code_prefix="PAIRING_REQUEST")
    return validate_request(record), raw


# ---------------------------------------------------------------------------
# transfer locks (pairing-transfer.lock / arming-transfer.lock; bounded,
# non-blocking, short-lived - never the client's lifecycle client.lock)
# ---------------------------------------------------------------------------

class _TransferLock:
    def __init__(self, store: Path, name: str = TRANSFER_LOCK_NAME) -> None:
        self._path = store / name
        self._fd: int | None = None

    def __enter__(self) -> "_TransferLock":
        try:
            self._fd = os.open(str(self._path), os.O_CREAT | os.O_RDWR, 0o600)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._release()
            raise PairingError("PAIRING_TRANSFER_BUSY") from None
        return self

    def _release(self) -> None:
        if self._fd is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt
                try:
                    os.lseek(self._fd, 0, os.SEEK_SET)
                    msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl
                try:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            os.close(self._fd)
            self._fd = None

    def __exit__(self, exc_type, exc, tb) -> None:
        self._release()


# ---------------------------------------------------------------------------
# inspect (zero writes; never opens pairing.json or any private arming slot)
# ---------------------------------------------------------------------------

def _inspect_arming_slot(store: Path, name: str, validator) -> dict | None:
    """Read ONE non-secret arming slot through its frozen validator. Private
    slots are NEVER passed here - existence only, per AR4.3."""
    path = store / name
    if not path.exists():
        return None
    _require(path.is_file() and not path.is_symlink(),
             f"ARM_SLOT_INVALID:{name}")
    record, _ = _load_arming_json(path, "ARM_SLOT")
    validator(record)
    return record


def cmd_inspect(args) -> dict:
    store = _require_directory(Path(args.client_store), "CLIENT_STORE")
    identity = _load_store_identity(store)
    request_path = store / REQUEST_FILE_NAME
    _require(request_path.is_file() and not request_path.is_symlink(),
             "PAIRING_REQUEST_MISSING")
    request, raw = _load_client_request(request_path)
    credential = store / PAIRING_FILE_NAME
    if credential.exists():
        # existence only: the credential file itself is never opened here
        _require(not credential.is_symlink(), "PAIRING_TARGET_INVALID")
    proposal = _inspect_arming_slot(store, ARMING_PROPOSAL_FILE,
                                    validate_arming_proposal)
    plan = _inspect_arming_slot(store, ARMING_PLAN_FILE, validate_arming_plan)
    arming_request = _inspect_arming_slot(store, ARMING_REQUEST_FILE,
                                          validate_arming_request)
    state = _inspect_arming_slot(store, ARMING_STATE_FILE,
                                 validate_arming_state)
    now = datetime.now(timezone.utc)
    pairing_request_sha256 = _hash_bytes(raw)
    # AR5.3: detect request change / staleness explicitly; recorded booleans
    # are tombstone facts, never a live authorization (no historical true is
    # reported as current state).
    changed = any(record is not None
                  and record["scope"]["pairing_request_sha256"]
                  != pairing_request_sha256
                  for record in (proposal, plan, arming_request, state))
    deadlines = []
    for record, key in ((arming_request, "expires_at"), (state, "expires_at")):
        if record is not None and record[key] is not None:
            deadlines.append(_parse_utc(record[key], "ARM_SLOT_INVALID"))
    arming = {
        "sampled_at": _now_iso(),
        "proposal_present": proposal is not None,
        "proposal": ({"proposal_id": proposal["proposal_id"],
                      "authorization_id": proposal["authorization_id"],
                      "permit_id": proposal["permit_id"],
                      "created_at": proposal["created_at"]}
                     if proposal is not None else None),
        "plan_present": plan is not None,
        "plan": ({"plan_id": plan["plan_id"],
                  "target_params_sha256": plan["target_params_sha256"],
                  "created_at": plan["created_at"]}
                 if plan is not None else None),
        "request_present": arming_request is not None,
        "request": ({"request_id": arming_request["request_id"],
                     "active": arming_request["active"],
                     "created_at": arming_request["created_at"],
                     "expires_at": arming_request["expires_at"],
                     "service_instance_id":
                         arming_request["service_instance_id"],
                     "service_epoch": arming_request["service_epoch"],
                     "target_params_sha256":
                         arming_request["target_params_sha256"]}
                    if arming_request is not None else None),
        "state_present": state is not None,
        "state": ({"state": state["state"],
                   "consumed": state["consumed"],
                   "revoked": state["revoked"],
                   "effective_enable_trading":
                       state["effective_enable_trading"],
                   "local_gate_closed": state["local_gate_closed"],
                   "service_gate_closed": state["service_gate_closed"],
                   "unresolved_dispatch_count":
                       state["unresolved_dispatch_count"],
                   "count_complete": state["count_complete"],
                   "updated_at": state["updated_at"],
                   "expires_at": state["expires_at"]}
                  if state is not None else None),
        # private slots: EXISTENCE ONLY (never opened, never hashed)
        "private_slots": {
            "arming_challenge": (store / ARMING_CHALLENGE_FILE).exists(),
            "arming_credential": (store / ARMING_CREDENTIAL_FILE).exists(),
        },
        "disarm_present": (store / DISARM_FILE).exists(),
        "pairing_request_changed": bool(changed),
        "expired": any(deadline <= now for deadline in deadlines),
        "note": "recorded non-secret facts only; historical true values are "
                "tombstones and never a live authorization or gate state",
    }
    return {
        "schema_version": "ne1_pairing_inspect_v1",
        "tool_version": TOOL_VERSION,
        "sampled_at": arming["sampled_at"],
        "store": {"path": str(store),
                  "layout_version": request["store_layout_version"],
                  "deterministic_slot_name": store.name,
                  "identity": identity},
        "request": {"active": request["active"],
                    "request_id": request["request_id"],
                    "instance_id": request["instance_id"],
                    "session_epoch": request["session_epoch"],
                    "generation": request["generation"],
                    "deployment_key": request["deployment_key"],
                    "strategy_id": request["strategy_id"],
                    "adapter_version": request["adapter_version"],
                    "client_file_sha256": request["client_file_sha256"],
                    "store_directory": request["store_directory"],
                    "execution_api_url": request["execution_api_url"],
                    "params_sha256": request["params_sha256"],
                    "raw_sha256": pairing_request_sha256},
        "credential_present": credential.exists(),
        "arming": arming,
    }


# ---------------------------------------------------------------------------
# build-actor-manifest (non-secret inputs only; no journal, no service)
# ---------------------------------------------------------------------------

def _decode_binding(data: dict):
    from ak_trading.infiny_execution import contracts as codec
    packet = {"schema_version": getattr(codec.Binding, "schema", codec.SCHEMA_VERSION),
              "type": "Binding", "data": data}
    return codec.decode(json.dumps(packet), codec.Binding)


def _validate_binding_template(record: dict) -> dict:
    _require(set(record) == BINDING_TEMPLATE_FIELDS, "BINDING_TEMPLATE_INVALID")
    for key in ("actor_id", "account_key", "strategy_key", "exchange",
                "instrument_id", "hedge_flag"):
        _require(_is_identifier(record[key]), f"BINDING_TEMPLATE_{key.upper()}_INVALID")
    _require(_is_strategy_id(record["strategy_id"]), "BINDING_TEMPLATE_INVALID")
    _require(type(record["trading_day"]) is str
             and ISO_DATE.fullmatch(record["trading_day"]) is not None,
             "BINDING_TEMPLATE_TRADING_DAY_INVALID")
    return record


def _load_operator_authorization(path: Path, operator_action_id: str) -> bytes:
    record, raw = _load_small_json(path, code_prefix="OPERATOR_AUTHORIZATION")
    _require(record.get("operator_action_id") == operator_action_id,
             "OPERATOR_AUTHORIZATION_ACTION_MISMATCH")
    return raw


def cmd_build_actor_manifest(args) -> dict:
    request_path = _require_file(Path(args.client_request), "CLIENT_REQUEST")
    template_path = _require_file(Path(args.binding_template), "BINDING_TEMPLATE")
    lock_path = _require_file(Path(args.contract_lock), "CONTRACT_LOCK")
    authorization_path = _require_file(Path(args.operator_authorization),
                                       "OPERATOR_AUTHORIZATION")
    output = _safe_local_path(Path(args.output_directory))
    _require(not output.exists(), "OUTPUT_DIRECTORY_MUST_BE_FRESH")
    _require(output.parent.is_dir(), "OUTPUT_PARENT_DIRECTORY_MISSING")
    _require(_is_identifier(args.operator_action_id), "OPERATOR_ACTION_ID_INVALID")

    request, request_raw = _load_client_request(request_path)
    _require(request["active"] is True, "PAIRING_REQUEST_INACTIVE")
    template, template_raw = _load_small_json(template_path,
                                              code_prefix="BINDING_TEMPLATE")
    _validate_binding_template(template)
    for key in ("strategy_id", "exchange", "instrument_id", "hedge_flag"):
        _require(template[key] == request[key], "BINDING_TEMPLATE_REQUEST_MISMATCH")

    authorization_raw = _load_operator_authorization(authorization_path,
                                                     args.operator_action_id)
    lock_record, lock_raw = _load_small_json(lock_path, code_prefix="CONTRACT_LOCK")
    _require(isinstance(lock_record, dict) and lock_record, "CONTRACT_LOCK_INVALID")

    # Binding identity fields come from the CURRENT request, never guessed.
    binding_data = {
        "account_key": template["account_key"],
        "strategy_key": template["strategy_key"],
        "strategy_id": template["strategy_id"],
        "instance_id": request["instance_id"],
        "session_epoch": request["session_epoch"],
        "trading_day": template["trading_day"],
        "exchange": request["exchange"],
        "instrument_id": request["instrument_id"],
        "hedge_flag": request["hedge_flag"],
    }
    try:
        _decode_binding(binding_data)  # validates through the frozen codec
    except ImportError as exc:
        raise PairingError("REGISTRATION_CODEC_UNAVAILABLE") from exc
    except Exception as exc:
        raise PairingError("BINDING_INVALID") from exc

    registration_id = _secrets.token_hex(16)
    output.mkdir(parents=False)
    record_path = output / "register-actor.json"
    record_payload = {"schema_version": ADAPTER_SCHEMA,
                      "actor_id": template["actor_id"],
                      "account_key": template["account_key"],
                      "bindings": [binding_data]}
    _atomic_write_new(record_path, _canonical_json(record_payload))
    try:
        from ak_trading.infiny_execution import contracts as codec
        from ak_trading.infiny_execution.native_evidence import (
            OperatorArtifact, OperatorRegistrationManifest, OperatorRecordType)
        manifest = OperatorRegistrationManifest(
            registration_id=registration_id,
            record_type=OperatorRecordType.ACTOR_REGISTRATION,
            record_path=str(record_path),
            record_sha256=_hash_file(record_path),
            contract_lock_sha256=_hash_bytes(lock_raw),
            operator_action_id=args.operator_action_id,
            authorization_artifact_path=str(authorization_path),
            authorization_artifact_sha256=_hash_bytes(authorization_raw),
            artifacts=(
                OperatorArtifact(path=str(request_path),
                                 sha256=_hash_bytes(request_raw)),
                OperatorArtifact(path=str(template_path),
                                 sha256=_hash_bytes(template_raw)),
            ))
        manifest_path = output / "register-actor-manifest.json"
        _atomic_write_new(manifest_path,
                          (codec.encode(manifest,
                                        schema_version=codec.V2_SCHEMA_VERSION)
                           + "\n").encode("utf-8"))
    except ImportError as exc:
        raise PairingError("REGISTRATION_CODEC_UNAVAILABLE") from exc
    return {
        "status": "built",
        "tool_version": TOOL_VERSION,
        "registration_id": registration_id,
        "record_path": str(record_path),
        "manifest_path": str(output / "register-actor-manifest.json"),
        "request": {"request_id": request["request_id"],
                    "instance_id": request["instance_id"],
                    "session_epoch": request["session_epoch"],
                    "strategy_id": request["strategy_id"]},
        "contract_lock_sha256": _hash_bytes(lock_raw),
        "note": "offline registration input only; no journal write, no service",
    }


# ---------------------------------------------------------------------------
# install (the user-executed credential handover)
# ---------------------------------------------------------------------------

def _reverify_context(context: dict) -> None:
    """Pre/post publication re-verification (R2.3): same user, same active
    request bytes/hash, same slot identity, same client release hash."""
    _require(_SECURITY.current_user_sid() == context["user_sid"],
             "PAIRING_CONTEXT_MISMATCH")
    request, raw = _load_client_request(Path(context["request_path"]))
    _require(raw == context["request_raw"], "PAIRING_REQUEST_CHANGED")
    _require(request["client_file_sha256"] == context["expected_client_sha256"],
             "PAIRING_CONTEXT_MISMATCH")
    identity = _load_store_identity(Path(context["store"]))
    _require(identity["instance_id"] == context["identity"]["instance_id"]
             and identity["deployment_key"] == context["identity"]["deployment_key"]
             and identity["strategy_id"] == context["identity"]["strategy_id"],
             "PAIRING_CONTEXT_MISMATCH")


def cmd_install(args) -> dict:
    store = _require_directory(Path(args.client_store), "CLIENT_STORE")
    request_path = _require_file(Path(args.client_request), "CLIENT_REQUEST")
    source_candidate = _safe_local_path(Path(args.source))
    _require(source_candidate.exists(), "PAIRING_MISSING")
    _require(source_candidate.is_file() and not source_candidate.is_symlink(),
             "PAIRING_SOURCE_INVALID")
    source_path = source_candidate
    service_start_path = _require_file(Path(args.service_start), "SERVICE_START")
    lock_path = _require_file(Path(args.contract_lock), "CONTRACT_LOCK")
    receipt_path = _safe_local_path(Path(args.receipt))
    _require(not receipt_path.exists(), "RECEIPT_PATH_MUST_BE_FRESH")
    _require(receipt_path.parent.is_dir(), "RECEIPT_PARENT_DIRECTORY_MISSING")
    _require(_is_hex64(args.expected_client_sha256), "EXPECTED_CLIENT_SHA256_INVALID")

    # R2.4: prove permissions BEFORE any secret enters the process, and before
    # any secret write. Failure rejects without reading the token bytes.
    acl_records = {
        "source": verify_private_access(source_path, need_write=False),
        "source_directory": verify_private_access(source_path.parent,
                                                  need_write=False),
        "client_store": verify_private_access(store, need_write=True),
    }
    credential_path = store / PAIRING_FILE_NAME
    if credential_path.exists():
        _require(not credential_path.is_symlink(), "PAIRING_TARGET_INVALID")
        acl_records["credential_slot"] = verify_private_access(credential_path,
                                                               need_write=True)

    request, request_raw = _load_client_request(request_path)
    _require(request["active"] is True, "PAIRING_REQUEST_INACTIVE")
    _require(request["store_layout_version"] == STORE_LAYOUT_VERSION,
             "STORE_LAYOUT_UNSUPPORTED")
    _require(_normalized(request["store_directory"]) == _normalized(store),
             "PAIRING_CONTEXT_MISMATCH")
    _require(store.parent.name == STORE_ROOT_NAME
             and store.name == f"strategy-{request['strategy_id']}",
             "STORE_LAYOUT_UNSUPPORTED")
    _require(request["client_file_sha256"] == args.expected_client_sha256,
             "PAIRING_CONTEXT_MISMATCH")
    identity = _load_store_identity(store)
    _require(identity["instance_id"] == request["instance_id"]
             and identity["deployment_key"] == request["deployment_key"]
             and identity["strategy_id"] == request["strategy_id"],
             "PAIRING_CONTEXT_MISMATCH")
    origin = execution_origin(request["execution_api_url"])

    service_start_raw = service_start_path.read_bytes()
    _require(len(service_start_raw) <= 64 * 1024, "PAIRING_SERVICE_START_INVALID")
    try:
        service_start_record = json.loads(service_start_raw.decode("utf-8"),
                                          object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PairingError("PAIRING_SERVICE_START_INVALID") from exc
    _require(isinstance(service_start_record, dict), "PAIRING_SERVICE_START_INVALID")
    service_start = validate_service_start(service_start_record)

    lock_raw = lock_path.read_bytes()
    _require(len(lock_raw) <= 256 * 1024, "CONTRACT_LOCK_INVALID")
    lock_sha256 = _hash_bytes(lock_raw)
    _require(lock_sha256 == service_start["contract_lock_sha256"],
             "PAIRING_CONTEXT_MISMATCH")

    source_raw = source_path.read_bytes()
    _require(len(source_raw) <= 8192, "PAIRING_FORMAT_INVALID")
    try:
        source_record = json.loads(source_raw.decode("utf-8"),
                                   object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PairingError("PAIRING_FORMAT_INVALID") from exc
    _require(isinstance(source_record, dict), "PAIRING_FORMAT_INVALID")
    source = validate_service_pairing(source_record)
    _require(source["instance_id"] != "unregistered"
             and source["diagnostic_only"] is False, "PAIRING_DIAGNOSTIC_ONLY")

    _require(source["instance_id"] == request["instance_id"],
             "PAIRING_CONTEXT_MISMATCH")
    _require(source["session_epoch"] == request["session_epoch"],
             "PAIRING_CONTEXT_MISMATCH")
    _require(source["contract_lock_sha256"] == lock_sha256,
             "PAIRING_CONTEXT_MISMATCH")
    # Cross-leak guard (R2.1/R2.2): the token value must never appear in the
    # non-secret service-start line or in the non-secret client request.
    _require(source["token"].encode("ascii") not in service_start_raw,
             "PAIRING_SERVICE_START_LEAKED_TOKEN")
    _require(source["token"].encode("ascii") not in request_raw,
             "PAIRING_REQUEST_LEAKED_TOKEN")
    _require(source["service_instance_id"] == service_start["service_instance_id"]
             and source["service_epoch"] == service_start["service_epoch"],
             "PAIRING_SERVICE_MISMATCH")
    instance_entry = service_start["instances"].get(source["instance_id"])
    _require(instance_entry is not None, "PAIRING_SERVICE_MISMATCH")
    _require(instance_entry["session_epoch"] == source["session_epoch"],
             "PAIRING_SERVICE_MISMATCH")
    _require(_normalized(instance_entry["token_path"]) == _normalized(source_path),
             "PAIRING_SERVICE_MISMATCH")
    _require(_normalized(service_start["pairing_dir"])
             == _normalized(source_path.parent), "PAIRING_SERVICE_MISMATCH")

    # The token moves file-bytes -> memory -> file; nothing else touches it.
    pairing_payload = {
        "schema_version": CLIENT_PAIRING_SCHEMA,
        "request_id": request["request_id"],
        "request_sha256": _hash_bytes(request_raw),
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
        "token": source["token"],
    }
    pairing_bytes = _canonical_json(pairing_payload)
    del source_record, source

    existing_kind = "FRESH"
    if credential_path.exists():
        existing, _ = _load_small_json(credential_path, limit=8192,
                                       code_prefix="PAIRING_TARGET")
        validate_client_pairing(existing)
        _require(existing["instance_id"] == request["instance_id"]
                 and existing["deployment_key"] == request["deployment_key"]
                 and existing["strategy_id"] == request["strategy_id"],
                 "PAIRING_TARGET_SLOT_MISMATCH")
        if existing["request_id"] == request["request_id"]:
            if credential_path.read_bytes() == pairing_bytes:
                existing_kind = "IDEMPOTENT"
            else:
                # same request, different secret/service context: refused
                raise PairingError("PAIRING_CREDENTIAL_CONFLICT")
        else:
            # same immutable slot, previous request invalidated: rotate
            existing_kind = "ROTATED"

    context = {"user_sid": _SECURITY.current_user_sid(),
               "request_path": str(request_path), "request_raw": request_raw,
               "expected_client_sha256": args.expected_client_sha256,
               "store": str(store),
               "identity": {"instance_id": identity["instance_id"],
                            "deployment_key": identity["deployment_key"],
                            "strategy_id": identity["strategy_id"]}}
    receipt = {
        "schema_version": RECEIPT_SCHEMA,
        "tool_version": TOOL_VERSION,
        "status": STATUS_TRANSFERRED,
        "transfer_kind": existing_kind,
        "created_at": _now_iso(),
        "tool_code_sha256": _hash_file(Path(__file__).resolve()),
        "client_store": str(store),
        "client_request": {"path": str(request_path),
                           "sha256": _hash_bytes(request_raw)},
        "identity": context["identity"],
        "request": {"request_id": request["request_id"],
                    "instance_id": request["instance_id"],
                    "session_epoch": request["session_epoch"],
                    "generation": request["generation"],
                    "strategy_id": request["strategy_id"],
                    "adapter_version": request["adapter_version"],
                    "client_file_sha256": request["client_file_sha256"],
                    "params_sha256": request["params_sha256"]},
        "execution_origin": origin,
        "service": {"service_instance_id": pairing_payload["service_instance_id"],
                    "service_epoch": pairing_payload["service_epoch"],
                    "service_version": service_start["service_version"],
                    "contract_lock_sha256": lock_sha256,
                    "contract_lock": {"path": str(lock_path),
                                      "sha256": lock_sha256},
                    "service_start": {"path": str(service_start_path),
                                      "sha256": _hash_bytes(service_start_raw)},
                    "source_path": str(source_path)},
        "credential_slot": {"path": str(credential_path),
                            "kind": existing_kind,
                            "note": "path reference only; content hashes of the "
                                    "credential file are never recorded"},
        "acl_verification": acl_records,
        "boundary_note": "credential handover only; enable_trading stays false "
                         "and no broker/trading capability is granted",
    }

    published = False
    try:
        with _TransferLock(store):
            _reverify_context(context)
            if existing_kind != "IDEMPOTENT":
                _atomic_publish(credential_path, pairing_bytes)
                published = True
                # R2.2 rejection table: a permission-verification failure
                # after publication is recorded with its safe failure code
                # (exit 3 - publication already happened); the client must
                # not run an unverified candidate.
                try:
                    acl_records["credential_slot_after_publish"] = (
                        verify_private_access(credential_path, need_write=True))
                except PairingError as exc:
                    raise PairingPublishError(
                        "PAIRING_ACL_VERIFY_AFTER_PUBLISH_FAILED:" + str(exc)) from exc
                receipt["acl_verification"] = acl_records
            _reverify_context(context)  # post-publication re-verification
        _atomic_write_new(receipt_path, _canonical_json(receipt))
    except PairingError:
        raise
    except PairingPublishError:
        raise
    except OSError as exc:
        # publication failures never leave a half-written credential; a
        # receipt failure after a completed (idempotent-safe) transfer is
        # recorded distinctly
        code = "PAIRING_RECEIPT_WRITE_FAILED" if (published
                                                  or existing_kind == "IDEMPOTENT") \
            else "PAIRING_PUBLISH_FAILED"
        raise PairingPublishError(code) from exc
    return {"status": STATUS_TRANSFERRED, "transfer_kind": existing_kind,
            "credential_slot": str(credential_path),
            "receipt": str(receipt_path)}


# ---------------------------------------------------------------------------
# NE1-ARB-ARMING-001: strict arming-protocol validators (RULING 04). Every
# validator raises tool-local ARM_* codes only; secret values are never
# embedded in any message.
# ---------------------------------------------------------------------------

def _is_utc_iso(value: object) -> bool:
    if type(value) is not str or not (1 < len(value) <= 64):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0)


def _parse_utc(value: object, code: str) -> datetime:
    _require(_is_utc_iso(value), code)
    return datetime.fromisoformat(value)


def _is_strict_int(value: object) -> bool:
    """AR4.1: integers never accept bool."""
    return type(value) is int


def _is_binding_dict(value: object) -> bool:
    return type(value) is dict and set(value) == {
        "account_key", "strategy_key", "strategy_id", "instance_id",
        "session_epoch", "trading_day", "exchange", "instrument_id",
        "hedge_flag"}


def validate_arming_scope(record: object) -> dict:
    _require(type(record) is dict and set(record) == ARMING_SCOPE_FIELDS,
             "ARM_SCOPE_INVALID")
    for key in ("pairing_request_id", "instance_id", "session_epoch",
                "generation", "process_instance_id"):
        _require(_is_hex32(record[key]), "ARM_SCOPE_INVALID")
    for key in ("pairing_request_sha256", "deployment_key",
                "client_file_sha256", "contract_lock_sha256",
                "pairing_params_sha256"):
        _require(_is_hex64(record[key]), "ARM_SCOPE_INVALID")
    _require(_is_strategy_id(record["strategy_id"]), "ARM_SCOPE_INVALID")
    _require(_is_nonempty_str(record["adapter_version"]), "ARM_SCOPE_INVALID")
    # AR4.2: origin must stay the frozen explicit loopback form and can never
    # be switched from a credential.
    origin = record["execution_origin"]
    _require(type(origin) is str, "ARM_SCOPE_INVALID")
    _require(execution_origin(origin) == origin, "ARM_SCOPE_INVALID")
    return record


def _validate_binding_wire(value: object, code: str) -> dict:
    """Binding uses the complete existing v2 field set and codec constraints
    (AR4.2); decode through the frozen codec and return the canonical dict."""
    _require(_is_binding_dict(value), code)
    try:
        decoded = _decode_binding(value)
    except ImportError as exc:
        raise PairingError("REGISTRATION_CODEC_UNAVAILABLE") from exc
    except PairingError:
        raise
    except Exception as exc:
        raise PairingError(code) from exc
    return value


def validate_arming_proposal(record: object) -> dict:
    _require(type(record) is dict and set(record) == ARMING_PROPOSAL_FIELDS,
             "ARM_PROPOSAL_INVALID")
    _require(record["schema_version"] == ARMING_PROPOSAL_SCHEMA,
             "ARM_PROPOSAL_INVALID")
    _require(_is_hex32(record["proposal_id"]), "ARM_PROPOSAL_INVALID")
    validate_arming_scope(record["scope"])
    _validate_binding_wire(record["binding"], "ARM_PROPOSAL_INVALID")
    for key in ("operator_action_id", "authorization_id", "permit_id"):
        _require(_is_identifier(record[key]), "ARM_PROPOSAL_INVALID")
    _require(record["execution_mode"] == "native", "ARM_PROPOSAL_INVALID")
    _require(_is_identifier(record["risk_policy_id"])
             or record["risk_policy_id"] == "", "ARM_PROPOSAL_INVALID")
    _require(_is_strict_int(record["max_native_sends"])
             and record["max_native_sends"] == 1, "ARM_PROPOSAL_INVALID")
    _require(_is_strict_int(record["max_native_cancels"])
             and record["max_native_cancels"] in (0, 1),
             "ARM_PROPOSAL_INVALID")
    _require(_is_utc_iso(record["created_at"]), "ARM_PROPOSAL_INVALID")
    return record


def validate_arming_plan(record: object) -> dict:
    _require(type(record) is dict and set(record) == ARMING_PLAN_FIELDS,
             "ARM_PLAN_INVALID")
    _require(record["schema_version"] == ARMING_PLAN_SCHEMA,
             "ARM_PLAN_INVALID")
    # plan_id is a HASH-style identifier (64 lowercase hex, decision AR4.2 and
    # the frozen client codec _hex64); only proposal_id stays a 32-hex random
    # id (ARM-W2-I-X01 fix 2).
    _require(_is_hex64(record["plan_id"]), "ARM_PLAN_INVALID")
    _require(_is_hex32(record["proposal_id"]), "ARM_PLAN_INVALID")
    _require(_is_hex64(record["proposal_sha256"]), "ARM_PLAN_INVALID")
    validate_arming_scope(record["scope"])
    _validate_binding_wire(record["binding"], "ARM_PLAN_INVALID")
    _require(_is_identifier(record["authorization_id"]), "ARM_PLAN_INVALID")
    _require(_is_identifier(record["permit_id"]), "ARM_PLAN_INVALID")
    _require(_is_hex64(record["target_params_sha256"]), "ARM_PLAN_INVALID")
    # AR2.1/AR4.2: effective_overrides carries exactly the five allowed fields.
    overrides = record["effective_overrides"]
    _require(type(overrides) is dict and set(overrides) == ARMING_OVERRIDE_FIELDS,
             "ARM_PLAN_INVALID")
    _require(overrides["execution_mode"] == "native", "ARM_PLAN_INVALID")
    _require(_is_identifier(overrides["risk_policy_id"])
             or overrides["risk_policy_id"] == "", "ARM_PLAN_INVALID")
    _require(_is_identifier(overrides["authorization_id"]),
             "ARM_PLAN_INVALID")
    _require(_is_strict_int(overrides["max_native_sends"])
             and overrides["max_native_sends"] == 1, "ARM_PLAN_INVALID")
    _require(_is_strict_int(overrides["max_native_cancels"])
             and overrides["max_native_cancels"] in (0, 1),
             "ARM_PLAN_INVALID")
    _require(_is_utc_iso(record["created_at"]), "ARM_PLAN_INVALID")
    return record


def validate_arming_request(record: object) -> dict:
    _require(type(record) is dict and set(record) == ARMING_REQUEST_FIELDS,
             "ARM_REQUEST_INVALID")
    _require(record["schema_version"] == ARMING_REQUEST_SCHEMA,
             "ARM_REQUEST_INVALID")
    _require(_is_hex32(record["request_id"]), "ARM_REQUEST_INVALID")
    # hash-style plan identifier: 64-hex, not a 32-hex random id (X01 fix 2)
    _require(_is_hex64(record["plan_id"]), "ARM_REQUEST_INVALID")
    _require(_is_hex64(record["plan_sha256"]), "ARM_REQUEST_INVALID")
    validate_arming_scope(record["scope"])
    _validate_binding_wire(record["binding"], "ARM_REQUEST_INVALID")
    _require(_is_identifier(record["authorization_id"]), "ARM_REQUEST_INVALID")
    _require(_is_identifier(record["permit_id"]), "ARM_REQUEST_INVALID")
    _require(_is_hex64(record["target_params_sha256"]), "ARM_REQUEST_INVALID")
    _require(_is_service_instance_id(record["service_instance_id"]),
             "ARM_REQUEST_INVALID")
    _require(_is_strict_int(record["service_epoch"])
             and record["service_epoch"] >= 1, "ARM_REQUEST_INVALID")
    created = _parse_utc(record["created_at"], "ARM_REQUEST_INVALID")
    expires = _parse_utc(record["expires_at"], "ARM_REQUEST_INVALID")
    _require(type(record["active"]) is bool, "ARM_REQUEST_INVALID")
    _require(expires > created, "ARM_REQUEST_INVALID")
    _require((expires - created).total_seconds() <= ARM_MAX_WINDOW_SECONDS,
             "ARM_WINDOW_EXCEEDED")
    return record


def validate_arming_challenge(record: object) -> dict:
    """Private slot schema. Validated structurally; the challenge VALUE is
    never displayed, logged, hashed or embedded in any error message."""
    _require(type(record) is dict and set(record) == ARMING_CHALLENGE_FIELDS,
             "ARM_CHALLENGE_INVALID")
    _require(record["schema_version"] == ARMING_CHALLENGE_SCHEMA,
             "ARM_CHALLENGE_INVALID")
    _require(_is_hex32(record["request_id"]), "ARM_CHALLENGE_INVALID")
    _require(_is_hex64(record["request_sha256"]), "ARM_CHALLENGE_INVALID")
    _require(_is_hex64(record["challenge"]), "ARM_CHALLENGE_INVALID")
    issued = _parse_utc(record["issued_at"], "ARM_CHALLENGE_INVALID")
    expires = _parse_utc(record["expires_at"], "ARM_CHALLENGE_INVALID")
    _require(expires > issued, "ARM_CHALLENGE_INVALID")
    _require((expires - issued).total_seconds() <= ARM_MAX_WINDOW_SECONDS,
             "ARM_WINDOW_EXCEEDED")
    _require(_is_nonempty_str(record["writer_sid"]), "ARM_CHALLENGE_INVALID")
    return record


def validate_arming_state(record: object) -> dict:
    _require(type(record) is dict and set(record) == ARMING_STATE_FIELDS,
             "ARM_STATE_INVALID")
    _require(record["schema_version"] == ARMING_STATE_SCHEMA,
             "ARM_STATE_INVALID")
    validate_arming_scope(record["scope"])
    # plan_id is a hash-style identifier (64-hex) while arm_request_id/arm_id
    # stay random 32-hex ids — mirrors the frozen client state codec
    # (AKExecutionBridge _validate_arming_state_record; X01 fix 2).
    _require(record["plan_id"] is None or _is_hex64(record["plan_id"]),
             "ARM_STATE_INVALID")
    for key in ("arm_request_id", "arm_id"):
        _require(record[key] is None or _is_hex32(record[key]),
                 "ARM_STATE_INVALID")
    for key in ("target_params_sha256",):
        _require(record[key] is None or _is_hex64(record[key]),
                 "ARM_STATE_INVALID")
    _require(record["service_instance_id"] is None
             or _is_service_instance_id(record["service_instance_id"]),
             "ARM_STATE_INVALID")
    for key in ("control_revision", "unresolved_dispatch_count"):
        _require(record[key] is None
                 or (_is_strict_int(record[key]) and record[key] >= 0),
                 "ARM_STATE_INVALID")
    _require(record["service_epoch"] is None
             or (_is_strict_int(record["service_epoch"])
                 and record["service_epoch"] >= 1), "ARM_STATE_INVALID")
    for key in ("state", "reason_code"):
        _require(_is_nonempty_str(record[key]), "ARM_STATE_INVALID")
    for key in ("consumed", "revoked", "effective_enable_trading",
                "local_gate_closed", "service_gate_closed", "count_complete"):
        _require(record[key] is None or type(record[key]) is bool,
                 "ARM_STATE_INVALID")
    if record["expires_at"] is not None:
        _parse_utc(record["expires_at"], "ARM_STATE_INVALID")
    _require(_is_utc_iso(record["updated_at"]), "ARM_STATE_INVALID")
    return record


def validate_arming_registrations(record: object) -> dict:
    _require(type(record) is dict and set(record) == REGISTRATIONS_FIELDS,
             "ARM_REGISTRATIONS_INVALID")
    _require(record["schema_version"] == ARMING_REGISTRATIONS_SCHEMA,
             "ARM_REGISTRATIONS_INVALID")
    _validate_binding_wire(record["binding"], "ARM_REGISTRATIONS_INVALID")
    _require(_is_hex64(record["target_params_sha256"]),
             "ARM_REGISTRATIONS_INVALID")
    _require(_is_hex64(record["contract_lock_sha256"]),
             "ARM_REGISTRATIONS_INVALID")
    _require(_is_identifier(record["authorization_id"]),
             "ARM_REGISTRATIONS_INVALID")
    _require(_is_identifier(record["permit_id"]),
             "ARM_REGISTRATIONS_INVALID")
    entries = record["entries"]
    _require(type(entries) is list and entries, "ARM_REGISTRATIONS_INVALID")
    types_seen = set()
    for entry in entries:
        _require(type(entry) is dict and set(entry) == REGISTRATION_ENTRY_FIELDS,
                 "ARM_REGISTRATIONS_INVALID")
        _require(entry["record_type"] in REGISTRATION_RECORD_TYPES,
                 "ARM_REGISTRATIONS_INVALID")
        for key in ("record_path", "manifest_path", "receipt_path"):
            _require(_is_nonempty_str(entry[key]),
                     "ARM_REGISTRATIONS_INVALID")
        for key in ("record_sha256", "manifest_sha256", "receipt_sha256"):
            _require(_is_hex64(entry[key]), "ARM_REGISTRATIONS_INVALID")
        types_seen.add(entry["record_type"])
    # AR5.3: entries cover this round's actor, the required evidence,
    # authorization and permit.
    _require(REGISTRATIONS_REQUIRED_TYPES <= types_seen,
             "ARM_REGISTRATIONS_INCOMPLETE")
    _require(bool(types_seen & REGISTRATIONS_EVIDENCE_TYPES),
             "ARM_REGISTRATIONS_INCOMPLETE")
    return record


def validate_arming_prerequisites(record: object) -> dict:
    _require(type(record) is dict and set(record) == PREREQUISITES_FIELDS,
             "ARM_PREREQUISITES_INVALID")
    _require(record["schema_version"] == ARMING_PREREQUISITES_SCHEMA,
             "ARM_PREREQUISITES_INVALID")
    _require(_is_hex32(record["prerequisites_id"]),
             "ARM_PREREQUISITES_INVALID")
    _parse_utc(record["created_at"], "ARM_PREREQUISITES_INVALID")
    expires = _parse_utc(record["expires_at"], "ARM_PREREQUISITES_INVALID")
    _require(_is_hex64(record["contract_lock_sha256"]),
             "ARM_PREREQUISITES_INVALID")
    for key in ("client_file_sha256", "service_file_sha256",
                "tool_file_sha256"):
        _require(_is_hex64(record[key]), "ARM_PREREQUISITES_INVALID")
    refs = record["evidence_refs"]
    _require(type(refs) is list and refs, "ARM_PREREQUISITES_INVALID")
    kinds_seen = set()
    for ref in refs:
        _require(type(ref) is dict and set(ref) == PREREQUISITE_REF_FIELDS,
                 "ARM_PREREQUISITES_INVALID")
        _require(ref["kind"] in PREREQUISITE_KINDS,
                 "ARM_PREREQUISITES_INVALID")
        _require(_is_nonempty_str(ref["path"]), "ARM_PREREQUISITES_INVALID")
        _require(_is_hex64(ref["sha256"]), "ARM_PREREQUISITES_INVALID")
        kinds_seen.add(ref["kind"])
    # AR5.3: all five gate kinds must be covered; a bare "approved" bool is
    # not a prerequisites file.
    _require(PREREQUISITE_KINDS <= kinds_seen,
             "ARM_PREREQUISITES_INCOMPLETE")
    return record


def validate_proposal_candidate(record: object) -> dict:
    """Operator-authored non-secret candidate input (prepare-arm --proposal).
    Carries exactly the non-derived proposal fields; scope, proposal_id,
    execution_mode and created_at are derived by the tool from the CURRENT
    pairing request, never from this file."""
    _require(type(record) is dict and set(record) == PROPOSAL_CANDIDATE_FIELDS,
             "ARM_CANDIDATE_INVALID")
    _require(record["schema_version"] == PROPOSAL_CANDIDATE_SCHEMA,
             "ARM_CANDIDATE_INVALID")
    _require(type(record["binding"]) is dict
             and set(record["binding"]) == BINDING_TEMPLATE_FIELDS,
             "ARM_CANDIDATE_INVALID")
    _validate_binding_template(record["binding"])
    for key in ("operator_action_id", "authorization_id", "permit_id"):
        _require(_is_identifier(record[key]), "ARM_CANDIDATE_INVALID")
    _require(_is_identifier(record["risk_policy_id"])
             or record["risk_policy_id"] == "", "ARM_CANDIDATE_INVALID")
    _require(_is_strict_int(record["max_native_sends"])
             and record["max_native_sends"] == 1, "ARM_CANDIDATE_INVALID")
    _require(_is_strict_int(record["max_native_cancels"])
             and record["max_native_cancels"] in (0, 1),
             "ARM_CANDIDATE_INVALID")
    return record


def _load_arming_json(path: Path, code_prefix: str) -> tuple[dict, bytes]:
    return _load_small_json(_require_file(path, code_prefix),
                            limit=ARMING_JSON_LIMIT, code_prefix=code_prefix)


def _build_arming_scope(request: dict, request_raw: bytes,
                        lock_sha256: str) -> dict:
    """AR4.2: every scope field comes from the CURRENT client-published
    request/identity and the approved lock; nothing is guessed."""
    scope = {
        "pairing_request_id": request["request_id"],
        "pairing_request_sha256": _hash_bytes(request_raw),
        "instance_id": request["instance_id"],
        "session_epoch": request["session_epoch"],
        "generation": request["generation"],
        "process_instance_id": request["process_instance_id"],
        "deployment_key": request["deployment_key"],
        "strategy_id": request["strategy_id"],
        "client_file_sha256": request["client_file_sha256"],
        "adapter_version": request["adapter_version"],
        "contract_lock_sha256": lock_sha256,
        "execution_origin": execution_origin(request["execution_api_url"]),
        "pairing_params_sha256": request["params_sha256"],
    }
    return validate_arming_scope(scope)


def _verify_store_request_context(store: Path, request: dict,
                                  expected_client_sha256: str,
                                  identity: dict | None = None) -> dict:
    """Shared pairing-request/store checks (install discipline, R2.3)."""
    _require(request["active"] is True, "PAIRING_REQUEST_INACTIVE")
    _require(request["store_layout_version"] == STORE_LAYOUT_VERSION,
             "STORE_LAYOUT_UNSUPPORTED")
    _require(_normalized(request["store_directory"]) == _normalized(store),
             "PAIRING_CONTEXT_MISMATCH")
    _require(store.parent.name == STORE_ROOT_NAME
             and store.name == f"strategy-{request['strategy_id']}",
             "STORE_LAYOUT_UNSUPPORTED")
    _require(request["client_file_sha256"] == expected_client_sha256,
             "PAIRING_CONTEXT_MISMATCH")
    if identity is None:
        identity = _load_store_identity(store)
    _require(identity["instance_id"] == request["instance_id"]
             and identity["deployment_key"] == request["deployment_key"]
             and identity["strategy_id"] == request["strategy_id"],
             "PAIRING_CONTEXT_MISMATCH")
    return identity


def _decode_v2_record(path: Path, record_type: str):
    """Decode a complete existing v2 record export through the frozen codec
    (AR4.2: full v2 records only, for display and precheck)."""
    try:
        from ak_trading.infiny_execution import contracts as codec
        from ak_trading.infiny_execution import native_contracts
    except ImportError as exc:
        raise PairingError("REGISTRATION_CODEC_UNAVAILABLE") from exc
    classes = {"NativeAuthorization": native_contracts.NativeAuthorization,
               "NativePilotPermit": native_contracts.NativePilotPermit}
    expected = classes[record_type]
    text = path.read_text(encoding="utf-8")
    _require(len(text.encode("utf-8")) <= ARMING_JSON_LIMIT,
             f"ARM_{record_type.upper()}_RECORD_INVALID")
    try:
        return codec.decode(text, expected, schema_version=expected.schema)
    except Exception as exc:
        raise PairingError(f"ARM_{record_type.upper()}_RECORD_INVALID") from exc


def _binding_wire(record_binding: dict):
    try:
        return _decode_binding(record_binding)
    except ImportError as exc:
        raise PairingError("REGISTRATION_CODEC_UNAVAILABLE") from exc
    except Exception as exc:
        raise PairingError("ARM_BINDING_INVALID") from exc


# ---------------------------------------------------------------------------
# AR1.3 interactive-console seam. These are the ONLY stdin/stdout interactivity
# points in the tool; there is no --yes, no env prefill and no scripted
# auto-confirm path anywhere.
# ---------------------------------------------------------------------------

def _console_attached() -> bool:
    """True only when stdin AND stdout are real TTYs and (on Windows) the
    process owns a console window. Redirection, pipes and no-console modes
    all fail this probe."""
    try:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return False
    except Exception:
        return False
    if sys.platform == "win32":
        try:
            return ctypes.windll.kernel32.GetConsoleWindow() != 0
        except Exception:
            return False
    return True


def _console_read_line(prompt: str) -> str:
    """Read ONE live console line; the only stdin read in this tool. A
    console that disappears mid-prompt is a rejection, never a bypass."""
    try:
        return input(prompt)
    except EOFError as exc:
        raise PairingError("ARM_INTERACTIVE_REQUIRED") from exc


def _arm_confirmation_phrase(request_id: str) -> str:
    """AR1.3: the phrase binds to the current NON-SECRET arming request_id."""
    return f"{ARM_PHRASE_PREFIX} {request_id[-8:]}"


def _render_arm_scope(request: dict, authorization, permit,
                      prerequisites: dict, lock_sha256: str,
                      earliest_deadline: str) -> str:
    """AR1.3 full-scope display: opaque account id, complete Binding, dual
    hashes, native mode, actions, volume, price bounds, expiry, grant ids,
    send=1 with explicit cancel, and the single-activation statement."""
    scope = request["scope"]
    binding = request["binding"]
    lines = [
        "=" * 72,
        "NE1 EXECUTION ARMING - FULL SCOPE REVIEW (AR1.3)",
        "=" * 72,
        f"opaque account      : {binding['account_key']}",
        f"complete binding    : {json.dumps(binding, ensure_ascii=True, sort_keys=True)}",
        f"adapter version     : {scope['adapter_version']}",
        f"client file sha256  : {scope['client_file_sha256']}",
        f"contract lock sha256: {lock_sha256}",
        f"pairing params hash : {scope['pairing_params_sha256']} (frozen pairing hash)",
        f"target params hash  : {request['target_params_sha256']} (to-be-confirmed target hash)",
        f"execution mode      : native (one-shot activation)",
        f"actions authorized  : {[item.value for item in authorization.actions]}",
        f"max volume          : {authorization.max_volume}",
        f"price bounds        : {[(b.symbol.exchange, b.symbol.instrument_id, str(b.lower), str(b.upper)) for b in authorization.price_bounds]}",
        f"authorization id    : {request['authorization_id']}",
        f"permit id           : {request['permit_id']}",
        f"send attempts       : {authorization.max_send_attempts} (permit {permit.max_send_attempts}) - EXACTLY 1",
        f"cancel attempts     : {authorization.max_cancel_attempts} (permit {permit.max_cancel_attempts}) - EXPLICIT 0 or 1",
        f"authorization expiry: {_utc_text(authorization.expires_at)}",
        f"permit expiry       : {_utc_text(permit.expires_at)}",
        f"arming deadline     : {earliest_deadline} (earliest of all deadlines)",
        f"service identity    : {request['service_instance_id']} epoch {request['service_epoch']}",
        f"control revision    : {permit.control_revision}",
        f"prerequisites id    : {prerequisites['prerequisites_id']}",
        f"request id          : {request['request_id']}",
        "-" * 72,
        "THIS IS THE ONLY ACTIVATION FOR THIS CLIENT LIFETIME.",
        "A consumed/revoked/expired round can never be re-armed in this session.",
        "-" * 72,
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# prepare-arm (AR1.1/AR5.3): non-secret candidate proposal publication
# ---------------------------------------------------------------------------

def cmd_prepare_arm(args) -> dict:
    store = _require_directory(Path(args.client_store), "CLIENT_STORE")
    request_path = _require_file(Path(args.client_request), "CLIENT_REQUEST")
    candidate_path = _require_file(Path(args.proposal), "ARM_CANDIDATE")
    lock_path = _require_file(Path(args.contract_lock), "CONTRACT_LOCK")
    receipt_path = _safe_local_path(Path(args.receipt))
    _require(not receipt_path.exists(), "RECEIPT_PATH_MUST_BE_FRESH")
    _require(receipt_path.parent.is_dir(), "RECEIPT_PARENT_DIRECTORY_MISSING")
    _require(_is_hex64(args.expected_client_sha256),
             "EXPECTED_CLIENT_SHA256_INVALID")

    # AR4.3: same-domain DACL protects non-secret slot integrity too.
    acl_records = {"client_store": verify_private_access(store, need_write=True)}
    proposal_slot = store / ARMING_PROPOSAL_FILE
    if proposal_slot.exists():
        _require(not proposal_slot.is_symlink(), "ARM_PROPOSAL_INVALID")
        acl_records["proposal_slot"] = verify_private_access(
            proposal_slot, need_write=True)

    request, request_raw = _load_client_request(request_path)
    identity = _verify_store_request_context(store, request,
                                             args.expected_client_sha256)
    candidate, _ = _load_arming_json(candidate_path, "ARM_CANDIDATE")
    validate_proposal_candidate(candidate)
    for key in ("strategy_id", "exchange", "instrument_id", "hedge_flag"):
        _require(candidate["binding"][key] == request[key],
                 "ARM_CANDIDATE_REQUEST_MISMATCH")

    lock_raw = lock_path.read_bytes()
    _require(len(lock_raw) <= 256 * 1024, "CONTRACT_LOCK_INVALID")
    lock_sha256 = _hash_bytes(lock_raw)
    scope = _build_arming_scope(request, request_raw, lock_sha256)

    # ARM-M02: an existing candidate for this store is a proposal conflict;
    # candidate changes require a fresh client round (AR2.1), never overwrite.
    _require(not proposal_slot.exists(), "ARM_PROPOSAL_ALREADY_PRESENT")
    state_path = store / ARMING_STATE_FILE
    if state_path.exists():
        state, _ = _load_arming_json(state_path, "ARM_STATE")
        validate_arming_state(state)
        _require(state["consumed"] is not True and state["revoked"] is not True,
                 "ARM_ROUND_CLOSED")

    proposal_id = _secrets.token_hex(16)
    binding_wire = {
        "account_key": candidate["binding"]["account_key"],
        "strategy_key": candidate["binding"]["strategy_key"],
        "strategy_id": candidate["binding"]["strategy_id"],
        "instance_id": request["instance_id"],
        "session_epoch": request["session_epoch"],
        "trading_day": candidate["binding"]["trading_day"],
        "exchange": request["exchange"],
        "instrument_id": request["instrument_id"],
        "hedge_flag": request["hedge_flag"],
    }
    payload = {
        "schema_version": ARMING_PROPOSAL_SCHEMA,
        "proposal_id": proposal_id,
        "scope": scope,
        "binding": _validate_binding_wire(binding_wire, "ARM_PROPOSAL_INVALID"),
        "operator_action_id": candidate["operator_action_id"],
        "authorization_id": candidate["authorization_id"],
        "permit_id": candidate["permit_id"],
        "execution_mode": "native",
        "risk_policy_id": candidate["risk_policy_id"],
        "max_native_sends": candidate["max_native_sends"],
        "max_native_cancels": candidate["max_native_cancels"],
        "created_at": _now_iso(),
    }
    proposal_bytes = _canonical_json(payload)
    validate_arming_proposal(payload)

    receipt = {
        "schema_version": ARMING_RECEIPT_SCHEMA,
        "operation": "prepare-arm",
        "status": ARM_STATUS_PREPARED,
        "action_source": ARM_ACTION_SOURCE,
        "scope": scope,
        "request_id": request["request_id"],
        "arm_id": None, "disarm_id": None, "plan_id": None,
        "authorization_id": candidate["authorization_id"],
        "permit_id": candidate["permit_id"],
        "target_params_sha256": None,  # computed by the CLIENT only (AR2.1)
        "client_file_sha256": args.expected_client_sha256,
        "contract_lock_sha256": lock_sha256,
        "service_instance_id": None, "service_epoch": None,
        "control_revision": None,
        "prerequisites_id": None, "prerequisites_sha256": None,
        "user_sid": _SECURITY.current_user_sid(),
        "local_gate_closed": None, "service_gate_closed": None,
        "recorded_at": _now_iso(),
        "reason_code": None,
    }

    published = False
    try:
        with _TransferLock(store, ARMING_TRANSFER_LOCK_NAME):
            current, current_raw = _load_client_request(request_path)
            _require(current_raw == request_raw, "PAIRING_REQUEST_CHANGED")
            _atomic_publish(proposal_slot, proposal_bytes)
            published = True
            try:
                acl_records["proposal_slot_after_publish"] = (
                    verify_private_access(proposal_slot, need_write=True))
            except PairingError as exc:
                raise PairingPublishError(
                    "ARM_ACL_VERIFY_AFTER_PUBLISH_FAILED:" + str(exc)) from exc
        _atomic_write_new(receipt_path, _canonical_json(receipt))
    except PairingError:
        raise
    except PairingPublishError:
        raise
    except OSError as exc:
        code = "ARM_RECEIPT_WRITE_FAILED" if published else "ARM_PUBLISH_FAILED"
        raise PairingPublishError(code) from exc
    return {"status": ARM_STATUS_PREPARED, "operation": "prepare-arm",
            "proposal_slot": str(proposal_slot),
            "proposal_id": proposal_id,
            "receipt": str(receipt_path)}


# ---------------------------------------------------------------------------
# arm (AR1.1/AR1.3/AR5.3): the USER-ONLY interactive one-shot delivery
# ---------------------------------------------------------------------------

def cmd_arm(args) -> dict:
    # AR1.3: the interactive-console precondition comes FIRST - before any
    # file read, before any secret byte can enter the process. Redirection,
    # pipes, missing consoles and every non-interactive mode reject here.
    _require(_console_attached(), "ARM_INTERACTIVE_REQUIRED")

    store = _require_directory(Path(args.client_store), "CLIENT_STORE")
    request_path_arg = _require_file(Path(args.arming_request),
                                     "ARMING_REQUEST")
    service_start_path = _require_file(Path(args.service_start),
                                       "SERVICE_START")
    authorization_path = _require_file(Path(args.authorization_record),
                                       "ARM_AUTHORIZATION_RECORD")
    permit_path = _require_file(Path(args.permit_record),
                                "ARM_PERMIT_RECORD")
    registrations_path = _require_file(Path(args.registration_receipts),
                                       "ARM_REGISTRATIONS")
    readiness_path = _require_file(Path(args.s4_readiness),
                                   "ARM_PREREQUISITES")
    lock_path = _require_file(Path(args.contract_lock), "CONTRACT_LOCK")
    receipt_path = _safe_local_path(Path(args.receipt))
    _require(not receipt_path.exists(), "RECEIPT_PATH_MUST_BE_FRESH")
    _require(receipt_path.parent.is_dir(), "RECEIPT_PARENT_DIRECTORY_MISSING")
    _require(_is_hex64(args.expected_client_sha256),
             "EXPECTED_CLIENT_SHA256_INVALID")

    # AR4.3: prove permissions BEFORE any secret (the challenge) enters the
    # process or any secret write happens.
    acl_records = {"client_store": verify_private_access(store, need_write=True)}
    challenge_path = store / ARMING_CHALLENGE_FILE
    credential_slot = store / ARMING_CREDENTIAL_FILE
    if challenge_path.exists():
        _require(not challenge_path.is_symlink(), "ARM_CHALLENGE_INVALID")
        acl_records["challenge_slot"] = verify_private_access(
            challenge_path, need_write=False)
    if credential_slot.exists():
        _require(not credential_slot.is_symlink(), "ARM_CREDENTIAL_INVALID")

    # --- current pairing context (the store's own request) ---------------
    pairing_request_path = store / REQUEST_FILE_NAME
    _require(pairing_request_path.is_file()
             and not pairing_request_path.is_symlink(),
             "PAIRING_REQUEST_MISSING")
    pairing_request, pairing_raw = _load_client_request(pairing_request_path)
    _verify_store_request_context(store, pairing_request,
                                  args.expected_client_sha256)

    lock_raw = lock_path.read_bytes()
    _require(len(lock_raw) <= 256 * 1024, "CONTRACT_LOCK_INVALID")
    lock_sha256 = _hash_bytes(lock_raw)

    # --- the client's published activation request ----------------------
    request, request_raw = _load_arming_json(request_path_arg,
                                             "ARM_REQUEST")
    validate_arming_request(request)
    request_sha256 = _hash_bytes(request_raw)
    _require(request["active"] is True, "ARM_REQUEST_INACTIVE")
    _require(_parse_utc(request["expires_at"], "ARM_REQUEST_INVALID")
             > datetime.now(timezone.utc), "ARM_REQUEST_EXPIRED")
    scope = request["scope"]
    current_scope = _build_arming_scope(pairing_request, pairing_raw,
                                        lock_sha256)
    _require(scope == current_scope, "ARM_SCOPE_MISMATCH")
    request_binding = _binding_wire(request["binding"])

    # --- service start: current identity + loaded arming prerequisites ---
    service_start_raw = service_start_path.read_bytes()
    _require(len(service_start_raw) <= 64 * 1024,
             "PAIRING_SERVICE_START_INVALID")
    try:
        service_record = json.loads(
            service_start_raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PairingError("PAIRING_SERVICE_START_INVALID") from exc
    _require(isinstance(service_record, dict),
             "PAIRING_SERVICE_START_INVALID")
    service_start = validate_service_start(service_record)
    _require(service_start["contract_lock_sha256"] == lock_sha256,
             "PAIRING_CONTEXT_MISMATCH")
    _require(request["service_instance_id"]
             == service_start["service_instance_id"]
             and request["service_epoch"] == service_start["service_epoch"],
             "ARM_SERVICE_MISMATCH")

    # --- S4 prerequisites (AR3.3/AR5.3): same file the service loaded ----
    prerequisites, prerequisites_raw = _load_arming_json(
        readiness_path, "ARM_PREREQUISITES")
    validate_arming_prerequisites(prerequisites)
    prerequisites_sha256 = _hash_bytes(prerequisites_raw)
    _require(_parse_utc(prerequisites["expires_at"],
                        "ARM_PREREQUISITES_INVALID")
             > datetime.now(timezone.utc), "ARM_PREREQUISITES_EXPIRED")
    _require(prerequisites["contract_lock_sha256"] == lock_sha256,
             "ARM_PREREQUISITES_LOCK_MISMATCH")
    _require(prerequisites["client_file_sha256"]
             == args.expected_client_sha256 == scope["client_file_sha256"],
             "ARM_PREREQUISITES_CLIENT_MISMATCH")
    _require(prerequisites["tool_file_sha256"]
             == _hash_file(Path(__file__).resolve()),
             "ARM_PREREQUISITES_TOOL_MISMATCH")
    for ref in prerequisites["evidence_refs"]:
        ref_path = _require_file(Path(ref["path"]),
                                 "ARM_PREREQUISITES_REF")
        _require(_hash_file(ref_path) == ref["sha256"],
                 "ARM_PREREQUISITES_REF_HASH_MISMATCH")
    loaded = service_start["arming_prerequisites"]
    _require(loaded is not None, "ARM_PREREQUISITES_NOT_LOADED")
    _require(loaded["prerequisites_id"] == prerequisites["prerequisites_id"]
             and loaded["prerequisites_sha256"] == prerequisites_sha256
             and loaded["client_file_sha256"]
             == prerequisites["client_file_sha256"],
             "ARM_PREREQUISITES_SERVICE_MISMATCH")

    # --- registration receipts index (AR5.3) -----------------------------
    registrations, _ = _load_arming_json(registrations_path,
                                         "ARM_REGISTRATIONS")
    validate_arming_registrations(registrations)
    _require(registrations["contract_lock_sha256"] == lock_sha256,
             "ARM_REGISTRATIONS_LOCK_MISMATCH")
    _require(registrations["target_params_sha256"]
             == request["target_params_sha256"],
             "ARM_REGISTRATIONS_TARGET_MISMATCH")
    _require(registrations["authorization_id"] == request["authorization_id"]
             and registrations["permit_id"] == request["permit_id"],
             "ARM_REGISTRATIONS_ID_MISMATCH")
    _require(_binding_wire(registrations["binding"]) == request_binding,
             "ARM_REGISTRATIONS_BINDING_MISMATCH")
    entry_hashes: dict[str, str] = {}
    for entry in registrations["entries"]:
        for key in ("record", "manifest", "receipt"):
            ref_path = _require_file(Path(entry[f"{key}_path"]),
                                     "ARM_REGISTRATIONS_REF")
            _require(_hash_file(ref_path) == entry[f"{key}_sha256"],
                     "ARM_REGISTRATIONS_REF_HASH_MISMATCH")
        if entry["record_type"] in ("NativeAuthorization", "NativePilotPermit"):
            _require(entry["record_type"] not in entry_hashes,
                     "ARM_REGISTRATIONS_DUPLICATE_RECORD")
            entry_hashes[entry["record_type"]] = entry["record_sha256"]

    # --- grant records: complete existing v2 exports ---------------------
    authorization_raw = authorization_path.read_bytes()
    permit_raw = permit_path.read_bytes()
    authorization_sha256 = _hash_bytes(authorization_raw)
    permit_sha256 = _hash_bytes(permit_raw)
    _require(entry_hashes.get("NativeAuthorization") == authorization_sha256,
             "ARM_AUTHORIZATION_RECORD_UNREGISTERED")
    _require(entry_hashes.get("NativePilotPermit") == permit_sha256,
             "ARM_PERMIT_RECORD_UNREGISTERED")
    authorization = _decode_v2_record(authorization_path,
                                      "NativeAuthorization")
    permit = _decode_v2_record(permit_path, "NativePilotPermit")
    _require(authorization.authorization_id == request["authorization_id"],
             "ARM_AUTHORIZATION_MISMATCH")
    _require(permit.permit_id == request["permit_id"]
             and permit.authorization_id == authorization.authorization_id,
             "ARM_PERMIT_MISMATCH")
    _require(authorization.binding == request_binding
             and permit.binding == request_binding,
             "ARM_GRANT_BINDING_MISMATCH")
    _require(authorization.params_sha256 == request["target_params_sha256"]
             and permit.params_sha256 == request["target_params_sha256"],
             "ARM_GRANT_TARGET_MISMATCH")
    _require(_is_strict_int(authorization.max_send_attempts)
             and authorization.max_send_attempts == 1
             and permit.max_send_attempts == 1,
             "ARM_GRANT_SEND_BUDGET_INVALID")
    _require(authorization.max_cancel_attempts in (0, 1)
             and permit.max_cancel_attempts in (0, 1),
             "ARM_GRANT_CANCEL_BUDGET_INVALID")
    _require(_is_strict_int(permit.control_revision)
             and permit.control_revision >= 0,
             "ARM_PERMIT_REVISION_INVALID")
    now = datetime.now(timezone.utc)
    _require(authorization.expires_at > now and permit.expires_at > now,
             "ARM_GRANT_EXPIRED")

    # --- the client's frozen plan (proposal -> plan -> request chain) -----
    plan_path = store / ARMING_PLAN_FILE
    _require(plan_path.is_file() and not plan_path.is_symlink(),
             "ARM_PLAN_MISSING")
    plan, plan_raw = _load_arming_json(plan_path, "ARM_PLAN")
    validate_arming_plan(plan)
    _require(plan["plan_id"] == request["plan_id"],
             "ARM_PLAN_REQUEST_MISMATCH")
    _require(_hash_bytes(plan_raw) == request["plan_sha256"],
             "ARM_PLAN_HASH_MISMATCH")
    _require(plan["target_params_sha256"] == request["target_params_sha256"],
             "ARM_PLAN_TARGET_MISMATCH")
    _require(plan["authorization_id"] == request["authorization_id"]
             and plan["permit_id"] == request["permit_id"],
             "ARM_PLAN_GRANT_MISMATCH")
    _require(plan["scope"] == scope, "ARM_PLAN_SCOPE_MISMATCH")
    _require(_binding_wire(plan["binding"]) == request_binding,
             "ARM_PLAN_BINDING_MISMATCH")
    proposal_slot = store / ARMING_PROPOSAL_FILE
    _require(proposal_slot.is_file() and not proposal_slot.is_symlink(),
             "ARM_PROPOSAL_MISSING")
    proposal, proposal_raw = _load_arming_json(proposal_slot,
                                               "ARM_PROPOSAL")
    validate_arming_proposal(proposal)
    _require(plan["proposal_id"] == proposal["proposal_id"]
             and plan["proposal_sha256"] == _hash_bytes(proposal_raw),
             "ARM_PLAN_PROPOSAL_MISMATCH")

    # --- one-shot guards (AR5.1) -----------------------------------------
    _require(not credential_slot.exists(), "ARM_CREDENTIAL_ALREADY_PRESENT")
    state_path = store / ARMING_STATE_FILE
    state_observed = None
    if state_path.exists():
        state_observed, state_raw = _load_arming_json(state_path,
                                                      "ARM_STATE")
        validate_arming_state(state_observed)
        _require(state_observed["consumed"] is not True,
                 "ARM_ALREADY_CONSUMED")
        _require(state_observed["revoked"] is not True, "ARM_REVOKED")
        _require(state_observed["scope"] == scope, "ARM_STATE_SCOPE_MISMATCH")

    # --- the private challenge: ACL first (above), then read -------------
    _require(challenge_path.is_file() and not challenge_path.is_symlink(),
             "ARM_CHALLENGE_MISSING")
    challenge, challenge_raw = _load_arming_json(challenge_path,
                                                 "ARM_CHALLENGE")
    validate_arming_challenge(challenge)
    _require(challenge["request_id"] == request["request_id"],
             "ARM_CHALLENGE_REQUEST_MISMATCH")
    _require(challenge["request_sha256"] == request_sha256,
             "ARM_CHALLENGE_REQUEST_HASH_MISMATCH")
    _require(challenge["writer_sid"] == _SECURITY.current_user_sid(),
             "ARM_CHALLENGE_WRITER_SID_MISMATCH")
    challenge_deadline = _parse_utc(challenge["expires_at"],
                                    "ARM_CHALLENGE_INVALID")
    _require(challenge_deadline > datetime.now(timezone.utc),
             "ARM_CHALLENGE_EXPIRED")

    # --- AR1.3 interactive confirmation -----------------------------------
    deadlines = [
        _parse_utc(request["expires_at"], "ARM_REQUEST_INVALID"),
        challenge_deadline,
        _parse_utc(prerequisites["expires_at"], "ARM_PREREQUISITES_INVALID"),
        authorization.expires_at, permit.expires_at,
    ]
    earliest = min(deadlines)
    _require(earliest > datetime.now(timezone.utc), "ARM_DEADLINE_PASSED")
    print(_render_arm_scope(request, authorization, permit, prerequisites,
                            lock_sha256, _utc_text(earliest)))
    expected_phrase = _arm_confirmation_phrase(request["request_id"])
    confirmed = False
    for _attempt in range(ARM_PHRASE_ATTEMPTS):
        answer = _console_read_line(
            f"Type '{expected_phrase}' to arm this scope "
            f"(attempt {_attempt + 1}/{ARM_PHRASE_ATTEMPTS}): ")
        _require(type(answer) is str and answer.strip() != "",
                 "ARM_CONFIRMATION_EMPTY")
        if answer == expected_phrase:
            confirmed = True
            break
    _require(confirmed, "ARM_PHRASE_MISMATCH")

    # Live liveness re-verification AFTER the phrase (ARM-M04): the request
    # must be byte-identical, active and unexpired at confirmation time.
    current_request, current_request_raw = _load_arming_json(
        request_path_arg, "ARM_REQUEST")
    validate_arming_request(current_request)
    _require(current_request_raw == request_raw, "ARM_REQUEST_CHANGED")
    _require(current_request["active"] is True, "ARM_REQUEST_INACTIVE")
    _require(_parse_utc(current_request["expires_at"], "ARM_REQUEST_INVALID")
             > datetime.now(timezone.utc), "ARM_REQUEST_EXPIRED")

    confirmed_at = datetime.now(timezone.utc)
    arm_id = _secrets.token_hex(16)
    user_sid = _SECURITY.current_user_sid()
    credential = {
        "schema_version": ARMING_CREDENTIAL_SCHEMA,
        "arm_id": arm_id,
        "request_id": request["request_id"],
        "request_sha256": request_sha256,
        "plan_id": request["plan_id"],
        "scope": scope,
        "binding": request["binding"],
        "target_params_sha256": request["target_params_sha256"],
        "authorization_id": request["authorization_id"],
        "authorization_record_sha256": authorization_sha256,
        "permit_id": request["permit_id"],
        "permit_record_sha256": permit_sha256,
        "service_instance_id": request["service_instance_id"],
        "service_epoch": request["service_epoch"],
        "expected_control_revision": permit.control_revision,
        "prerequisites_id": prerequisites["prerequisites_id"],
        "prerequisites_sha256": prerequisites_sha256,
        "challenge": challenge["challenge"],
        "user_sid": user_sid,
        "action_source": ARM_ACTION_SOURCE,
        "explicit_user_action": True,
        "confirmed_at": _utc_text(confirmed_at),
        "expires_at": _utc_text(earliest),
    }
    credential_bytes = _canonical_json(credential)
    _require(set(credential) == ARMING_CREDENTIAL_FIELDS,
             "ARM_CREDENTIAL_INVALID")

    receipt = {
        "schema_version": ARMING_RECEIPT_SCHEMA,
        "operation": "arm",
        "status": ARM_STATUS_SUBMITTED,
        "action_source": ARM_ACTION_SOURCE,
        "scope": scope,
        "request_id": request["request_id"],
        "arm_id": arm_id,
        "disarm_id": None,
        "plan_id": request["plan_id"],
        "authorization_id": request["authorization_id"],
        "permit_id": request["permit_id"],
        "target_params_sha256": request["target_params_sha256"],
        "client_file_sha256": args.expected_client_sha256,
        "contract_lock_sha256": lock_sha256,
        "service_instance_id": request["service_instance_id"],
        "service_epoch": request["service_epoch"],
        "control_revision": permit.control_revision,
        "prerequisites_id": prerequisites["prerequisites_id"],
        "prerequisites_sha256": prerequisites_sha256,
        "user_sid": user_sid,
        # the tool never claims gate facts it cannot observe (AR5.2)
        "local_gate_closed": None,
        "service_gate_closed": None,
        "recorded_at": _now_iso(),
        "reason_code": None,
    }

    published = False
    try:
        with _TransferLock(store, ARMING_TRANSFER_LOCK_NAME):
            fresh_request, fresh_raw = _load_arming_json(request_path_arg,
                                                         "ARM_REQUEST")
            _require(fresh_raw == request_raw, "ARM_REQUEST_CHANGED")
            fresh_pairing, fresh_pairing_raw = _load_client_request(
                pairing_request_path)
            _require(fresh_pairing_raw == pairing_raw, "PAIRING_REQUEST_CHANGED")
            fresh_challenge, fresh_challenge_raw = _load_arming_json(
                challenge_path, "ARM_CHALLENGE")
            _require(fresh_challenge_raw == challenge_raw,
                     "ARM_CHALLENGE_CHANGED")
            _atomic_publish(credential_slot, credential_bytes)
            published = True
            try:
                acl_records["credential_slot_after_publish"] = (
                    verify_private_access(credential_slot, need_write=True))
            except PairingError as exc:
                raise PairingPublishError(
                    "ARM_ACL_VERIFY_AFTER_PUBLISH_FAILED:" + str(exc)) from exc
        _atomic_write_new(receipt_path, _canonical_json(receipt))
    except PairingError:
        raise
    except PairingPublishError:
        raise
    except OSError as exc:
        # AR5.3: a receipt failure after delivery never regenerates arm_id;
        # a re-run hits ARM_CREDENTIAL_ALREADY_PRESENT instead.
        code = "ARM_RECEIPT_WRITE_FAILED" if published else "ARM_PUBLISH_FAILED"
        raise PairingPublishError(code) from exc
    return {"status": ARM_STATUS_SUBMITTED, "operation": "arm",
            "credential_slot": str(credential_slot),
            "arm_id": arm_id, "receipt": str(receipt_path),
            "note": "awaiting client consumption; never ARMED by this tool"}


# ---------------------------------------------------------------------------
# disarm (AR5.2/AR5.3): scoped revocation request publication
# ---------------------------------------------------------------------------

def cmd_disarm(args) -> dict:
    store = _require_directory(Path(args.client_store), "CLIENT_STORE")
    request_path = _require_file(Path(args.client_request), "CLIENT_REQUEST")
    receipt_path = _safe_local_path(Path(args.receipt))
    _require(not receipt_path.exists(), "RECEIPT_PATH_MUST_BE_FRESH")
    _require(receipt_path.parent.is_dir(), "RECEIPT_PARENT_DIRECTORY_MISSING")

    acl_records = {"client_store": verify_private_access(store, need_write=True)}
    request, request_raw = _load_client_request(request_path)

    # The revoked round's scope is COPIED from the recorded arming files
    # (client-published), never rebuilt or guessed; the tool has no
    # --contract-lock input here and must not fabricate scope fields.
    recorded: tuple[str, dict] | None = None
    state_observed = None
    state_path = store / ARMING_STATE_FILE
    if state_path.exists():
        state_observed, _ = _load_arming_json(state_path, "ARM_STATE")
        validate_arming_state(state_observed)
        recorded = ("state", state_observed["scope"])
    else:
        for name, validator in (
                (ARMING_REQUEST_FILE, validate_arming_request),
                (ARMING_PLAN_FILE, validate_arming_plan),
                (ARMING_PROPOSAL_FILE, validate_arming_proposal)):
            path = store / name
            if path.exists():
                record, _ = _load_arming_json(path, "ARM_SLOT")
                validator(record)
                recorded = (name, record["scope"])
                break
    _require(recorded is not None, "ARM_ROUND_ABSENT")
    scope = recorded[1]

    # AR5.2: revoking an OLD round must not be reported as revoking the
    # current instance - stale scope is returned explicitly.
    reason_code = None
    current_identity = {
        "pairing_request_id": request["request_id"],
        "instance_id": request["instance_id"],
        "session_epoch": request["session_epoch"],
        "generation": request["generation"],
        "process_instance_id": request["process_instance_id"],
        "deployment_key": request["deployment_key"],
        "strategy_id": request["strategy_id"],
    }
    stale = any(scope[key] != value
                for key, value in current_identity.items())
    if stale:
        reason_code = "STALE_SCOPE"

    arm_request_id = None
    arm_id = None
    plan_id = None
    service_instance_id = None
    service_epoch = None
    local_gate_closed = None
    service_gate_closed = None
    if state_observed is not None:
        arm_request_id = state_observed["arm_request_id"]
        arm_id = state_observed["arm_id"]
        plan_id = state_observed["plan_id"]
        service_instance_id = state_observed["service_instance_id"]
        service_epoch = state_observed["service_epoch"]
        # AR5.2 (decision :184/:220): the just-submitted revocation stays
        # PENDING/UNKNOWN. The observed state's gate booleans are PRE-disarm
        # facts of the round's past, never this request's outcome — copying
        # them would claim/imply an immediate gate result the tool cannot
        # observe. Identity references (ids/epochs) above stay observable facts.
    else:
        request_slot = store / ARMING_REQUEST_FILE
        if request_slot.exists():
            record, _ = _load_arming_json(request_slot, "ARM_SLOT")
            validate_arming_request(record)
            if record["scope"] == scope:
                arm_request_id = record["request_id"]
                plan_id = record["plan_id"]
                service_instance_id = record["service_instance_id"]
                service_epoch = record["service_epoch"]

    disarm_id = _secrets.token_hex(16)
    payload = {
        "schema_version": DISARM_SCHEMA,
        "disarm_id": disarm_id,
        "scope": scope,
        "arm_request_id": arm_request_id,
        "arm_id": arm_id,
        "requested_at": _now_iso(),
        "user_sid": _SECURITY.current_user_sid(),
        "reason": ARM_DISARM_REASON,
    }
    payload_bytes = _canonical_json(payload)

    # AR5.2: repeat disarm is idempotent for the SAME scope; a pending
    # revocation of a DIFFERENT round is never overwritten.
    disarm_slot = store / DISARM_FILE
    if disarm_slot.exists():
        existing, existing_raw = _load_arming_json(disarm_slot, "DISARM_SLOT")
        _require(existing.get("schema_version") == DISARM_SCHEMA,
                 "DISARM_SLOT_INVALID")
        _require(existing["scope"] == scope, "DISARM_SCOPE_CONFLICT")

    receipt = {
        "schema_version": ARMING_RECEIPT_SCHEMA,
        "operation": "disarm",
        "status": DISARM_STATUS_REQUESTED,
        "action_source": ARM_ACTION_SOURCE,
        "scope": scope,
        "request_id": arm_request_id or request["request_id"],
        "arm_id": arm_id,
        "disarm_id": disarm_id,
        "plan_id": plan_id,
        "authorization_id": None, "permit_id": None,
        "target_params_sha256": None,
        "client_file_sha256": request["client_file_sha256"],
        "contract_lock_sha256": scope["contract_lock_sha256"],
        "service_instance_id": service_instance_id,
        "service_epoch": service_epoch,
        "control_revision": None,
        "prerequisites_id": None, "prerequisites_sha256": None,
        "user_sid": payload["user_sid"],
        "local_gate_closed": local_gate_closed,
        "service_gate_closed": service_gate_closed,
        "recorded_at": _now_iso(),
        "reason_code": reason_code,
    }

    published = False
    try:
        with _TransferLock(store, ARMING_TRANSFER_LOCK_NAME):
            _atomic_publish(disarm_slot, payload_bytes)
            published = True
            try:
                acl_records["disarm_slot_after_publish"] = (
                    verify_private_access(disarm_slot, need_write=True))
            except PairingError as exc:
                raise PairingPublishError(
                    "ARM_ACL_VERIFY_AFTER_PUBLISH_FAILED:" + str(exc)) from exc
        _atomic_write_new(receipt_path, _canonical_json(receipt))
    except PairingError:
        raise
    except PairingPublishError:
        raise
    except OSError as exc:
        code = "ARM_RECEIPT_WRITE_FAILED" if published else "ARM_PUBLISH_FAILED"
        raise PairingPublishError(code) from exc
    return {"status": DISARM_STATUS_REQUESTED, "operation": "disarm",
            "disarm_slot": str(disarm_slot), "disarm_id": disarm_id,
            "reason_code": reason_code, "receipt": str(receipt_path),
            "note": "request safely submitted; gate closure is observed, "
                    "not assumed (REVOKE_PENDING until client/service ACK)"}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="NE1 pairing/arming last-mile tool (decisions "
                    "NE1-ARB-PAIRING-LASTMILE-001 RULING 02/03 and "
                    "NE1-ARB-ARMING-001 RULING 01/03/04/05); no network, no "
                    "trading capability, tokens/challenges never enter "
                    "URLs/logs/arguments; arm is user-only interactive")
    sub = parser.add_subparsers(dest="action", required=True)

    inspect = sub.add_parser("inspect", help="non-secret store identity/request/"
                             "layout summary plus credential EXISTENCE (zero writes)")
    inspect.add_argument("--client-store", type=Path, required=True,
                         help="client deterministic store directory (layout 2)")
    inspect.set_defaults(handler=cmd_inspect)

    build = sub.add_parser("build-actor-manifest",
                           help="offline actor-registration manifest from the "
                                "active pairing request (no journal, no service)")
    build.add_argument("--client-request", type=Path, required=True,
                       help="path of the store's pairing-request.json")
    build.add_argument("--binding-template", type=Path, required=True,
                       help="user-verified non-secret binding template JSON")
    build.add_argument("--contract-lock", type=Path, required=True,
                       help="frozen contract-lock.json")
    build.add_argument("--operator-action-id", required=True,
                       help="unique operator action id for this registration")
    build.add_argument("--operator-authorization", type=Path, required=True,
                       help="user's record permitting this offline registration")
    build.add_argument("--output-directory", type=Path, required=True,
                       help="NEW directory for register-actor.json + manifest")
    build.set_defaults(handler=cmd_build_actor_manifest)

    install = sub.add_parser("install", help="user-executed atomic credential "
                             "handover into the client store (exit 0 = "
                             "TRANSFERRED_AWAITING_HANDSHAKE)")
    install.add_argument("--client-store", type=Path, required=True)
    install.add_argument("--client-request", type=Path, required=True,
                         help="path of the store's ACTIVE pairing-request.json")
    install.add_argument("--source", type=Path, required=True,
                         help="service pairing file path reference "
                              "(<pairing-dir>/<instance_id>.token)")
    install.add_argument("--service-start", type=Path, required=True,
                         help="non-secret service-start.json for this run")
    install.add_argument("--contract-lock", type=Path, required=True)
    install.add_argument("--expected-client-sha256", required=True,
                         help="full sha256 of the reviewed client release")
    install.add_argument("--receipt", type=Path, required=True,
                         help="NEW receipt path (TRANSFERRED_AWAITING_HANDSHAKE)")
    install.set_defaults(handler=cmd_install)

    prepare = sub.add_parser(
        "prepare-arm", help="publish the non-secret arming candidate proposal "
                            "into the store slot (status "
                            "PREPARATION_SUBMITTED; the client publishes the "
                            "plan asynchronously)")
    prepare.add_argument("--client-store", type=Path, required=True)
    prepare.add_argument("--client-request", type=Path, required=True,
                         help="path of the store's ACTIVE pairing-request.json")
    prepare.add_argument("--proposal", type=Path, required=True,
                         help="non-secret candidate JSON "
                              "(ne1_arming_proposal_candidate_v1)")
    prepare.add_argument("--contract-lock", type=Path, required=True)
    prepare.add_argument("--expected-client-sha256", required=True,
                         help="full sha256 of the reviewed client release")
    prepare.add_argument("--receipt", type=Path, required=True,
                         help="NEW receipt path (ne1_arming_tool_receipt_v1)")
    prepare.set_defaults(handler=cmd_prepare_arm)

    arm = sub.add_parser(
        "arm", help="USER-ONLY interactive one-shot arming credential "
                    "delivery (status ARM_SUBMITTED_AWAITING_CLIENT; refuses "
                    "any non-interactive stdin, no --yes, no auto-confirm)")
    arm.add_argument("--client-store", type=Path, required=True)
    arm.add_argument("--arming-request", type=Path, required=True,
                     help="path of the client's non-secret "
                          "arming-request.json (ne1_arming_request_v1)")
    arm.add_argument("--service-start", type=Path, required=True,
                     help="non-secret service-start.json carrying "
                          "arming_prerequisites for this run")
    arm.add_argument("--authorization-record", type=Path, required=True,
                     help="complete existing v2 NativeAuthorization record "
                          "export (non-secret)")
    arm.add_argument("--permit-record", type=Path, required=True,
                     help="complete existing v2 NativePilotPermit record "
                          "export (non-secret)")
    arm.add_argument("--registration-receipts", type=Path, required=True,
                     help="non-secret registration index "
                          "(ne1_arming_registrations_v1)")
    arm.add_argument("--s4-readiness", type=Path, required=True,
                     help="approved non-secret S4 prerequisites file "
                          "(ne1_arming_prerequisites_v1; same file the "
                          "service loaded via --arming-readiness)")
    arm.add_argument("--contract-lock", type=Path, required=True)
    arm.add_argument("--expected-client-sha256", required=True)
    arm.add_argument("--receipt", type=Path, required=True,
                     help="NEW receipt path (ne1_arming_tool_receipt_v1)")
    arm.set_defaults(handler=cmd_arm)

    disarm = sub.add_parser(
        "disarm", help="publish a scoped revocation request (status "
                       "DISARM_REQUESTED; offline, idempotent, no cancel "
                       "orders)")
    disarm.add_argument("--client-store", type=Path, required=True)
    disarm.add_argument("--client-request", type=Path, required=True,
                        help="path of the store's pairing-request.json "
                             "identifying the round being revoked")
    disarm.add_argument("--receipt", type=Path, required=True,
                        help="NEW receipt path (ne1_arming_tool_receipt_v1)")
    disarm.set_defaults(handler=cmd_disarm)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.handler(args)
    except PairingError as exc:
        print(json.dumps({"status": "rejected", "code": str(exc)},
                         ensure_ascii=True))
        return 2
    except PairingPublishError as exc:
        print(json.dumps({"status": "failed", "code": exc.code},
                         ensure_ascii=True))
        return 3
    except (OSError, ValueError, KeyError, TypeError) as exc:
        code = "PAIRING_PUBLISH_FAILED" if isinstance(exc, OSError) \
            else "PAIRING_INTERNAL_INVALID"
        print(json.dumps({"status": "failed" if isinstance(exc, OSError)
                          else "rejected", "error_type": type(exc).__name__,
                          "code": code}, ensure_ascii=True))
        return 3 if isinstance(exc, OSError) else 2
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
