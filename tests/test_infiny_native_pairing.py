"""TE: NE1 pairing/arming last-mile delivery face (NE1-ARB-PAIRING-LASTMILE-001
+ NE1-ARB-ARMING-001 RULING 06 tool-side matrix, ARM-LM-D rows).

Primary coverage:

* M03 (credential handover security, pairing decision RULING 02):
  - REAL Windows DACL positive/negative examples through actual
    SetNamedSecurityInfoW security descriptors - never a mocked chmod;
  - the permission proof happens BEFORE any secret byte enters the process
    or lands on disk, and the published credential's DACL is re-verified;
  - bad format / legacy two-line file / diagnostic token / stale service
    epoch / wrong session / wrong instance / wrong origin / wrong release
    hash / inactive or changed request / missing token are all rejected;
  - same-slot rotation under a NEW active request succeeds, while foreign
    slot credentials and same-request conflicts are refused;
  - SYNTHETIC test tokens never appear in stdout, receipts or installer
    evidence backups.

* M08 (installer slot discipline, pairing decision §六): deterministic store
  slot measured read-only; ``absent`` only when nothing exists; plan never
  creates the store; apply/rollback never touch credentials or wiring.

* ARM tool-side matrix (arming decision RULING 06, owner D):
  - ARM-M02 prepare-arm: non-secret proposal publication, scope derived from
    the CURRENT request, proposal-conflict / closed-round rejections;
  - ARM-M04 human entrance: subprocess pipe/redirect/no-console rejections
    (real, not mocked), controlled-console positive path (SYNTHETIC console
    seam), empty/wrong phrase, expiry-at-enter, request-replaced-after-display,
    and NO --yes/auto-confirm surface anywhere;
  - ARM-M05 ACL: real DACL positive/negative on the store and the PRIVATE
    challenge slot, permission proof ordered before the challenge bytes enter
    the process;
  - ARM-M06 strict format: frozen schema field sets, unknown fields/duplicate
    keys/type errors/bool-as-int, tampered grant-record hashes, wrong writer
    SID, inactive/expired requests - zero activation in every case;
  - ARM-M09 one-shot (tool side): existing credential, consumed and revoked
    rounds all refuse re-delivery;
  - ARM-M10 disarm: offline scoped revocation, idempotent repeat, explicit
    STALE_SCOPE for old rounds, absent-round rejection;
  - secret hygiene: the SYNTHETIC challenge nonce exists ONLY in the private
    source slot and the delivered credential - never in stdout, receipts or
    any other file.

Every token, credential, identity and journal in this file is a SYNTHETIC
value generated inside pytest sandbox directories. No real token, no .env
content, no production store, no network and no UI is ever touched. The
DACL cases exercise the real win32 security surface on the deployment host.
"""
from __future__ import annotations

import ctypes
import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "scripts" / "pair_infiny_execution.py"
INSTALLER = ROOT / "scripts" / "install_infiny_execution.py"
LAUNCHER = ROOT / "scripts" / "run_infiny_execution.py"
CLIENT_SOURCE = ROOT / "integrations" / "infiny" / "AKExecutionBridge.py"
TEMPLATE = ROOT / "config" / "infiny_native_execution.example.json"

TOOL_VERSION = "ne1-pairing-20260918-arming-v1b"
INSTALLER_VERSION = "ne1-delivery-20260918-arming-v1"

# SYNTHETIC credentials (never a production token; labeled on purpose).
SYNTHETIC_TOKEN = "1f0e" * 16
SYNTHETIC_TOKEN_2 = "2b1d" * 16
# SYNTHETIC arming challenge nonce (private-slot secret; labeled on purpose).
SYNTHETIC_CHALLENGE = "7c31" * 16
SERVICE_INSTANCE = "execution-" + "a1b2c3d4e5f60718"
SERVICE_INSTANCE_2 = "execution-" + "0123456789abcdef"
OBSERVER_FAKE = ('# sandbox observer pair, version observer-20260914-reconnect-v5\n'
                 'class AKObserverBridge:\n    pass\n')

ARM_AUTH_ID = "auth-arming-synth-1"
ARM_PERMIT_ID = "permit-arming-synth-1"

if sys.platform != "win32":  # fail loudly, never silently skip M03/M08
    raise RuntimeError("the pairing last-mile DACL contract is Windows-only")


# ---------------------------------------------------------------------------
# generic helpers
# ---------------------------------------------------------------------------

def _sha_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha_file(path: Path) -> str:
    return _sha_bytes(path.read_bytes())


def _hex(seed: str, length: int = 32) -> str:
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return digest[:length]


def _run(script: Path, argv: list[str]) -> tuple[int, dict]:
    result = subprocess.run([sys.executable, "-B", "-X", "utf8", str(script), *argv],
                            capture_output=True, text=True, cwd=str(ROOT))
    text = result.stdout.strip()
    assert text, (f"no stdout (exit {result.returncode}); "
                  f"stderr tail: {result.stderr.strip()[-500:]}")
    return result.returncode, json.loads(text.splitlines()[-1])


def _tool(argv: list[str]) -> tuple[int, dict]:
    return _run(TOOL, argv)


def _installer(argv: list[str]) -> tuple[int, dict]:
    return _run(INSTALLER, argv)


_TOOL_MODULE_CACHE: dict = {}


def _load_tool():
    """Import scripts/pair_infiny_execution.py for in-process seam tests."""
    module = _TOOL_MODULE_CACHE.get("tool")
    if module is None:
        spec = importlib.util.spec_from_file_location("ne1_pairing_tool", TOOL)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _TOOL_MODULE_CACHE["tool"] = module
    return module


def _load_installer():
    spec = importlib.util.spec_from_file_location("ne1_installer_v2", INSTALLER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(path: Path, payload: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    path.write_bytes(payload)
    return path


def _json_line(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=True, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# REAL Windows DACL editor (SetNamedSecurityInfoW); never a mocked chmod
# ---------------------------------------------------------------------------

class _DaclEditor:
    """Minimal real-DACL writer used to build the positive/negative cases."""

    def __init__(self) -> None:
        assert sys.platform == "win32"
        import ctypes.wintypes as wt
        self._wt = wt
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
        self._advapi32 = advapi32
        self._kernel32 = kernel32

    def _sid(self, text: str) -> int:
        pointer = ctypes.c_void_p()
        if not self._advapi32.ConvertStringSidToSidW(text, ctypes.byref(pointer)):
            raise OSError("ConvertStringSidToSidW failed")
        return pointer.value or 0

    def replace(self, path: Path, trustee_sids: list[str], *, directory: bool,
                protected: bool = True) -> None:
        """Install a REAL DACL granting FILE_ALL_ACCESS to the given SIDs."""
        acl = ctypes.create_string_buffer(1024)
        if not self._advapi32.InitializeAcl(acl, 1024, 2):
            raise OSError("InitializeAcl failed")
        inheritance = 0x3 if directory else 0x0
        pointers = []
        try:
            for sid in trustee_sids:
                pointer = self._sid(sid)
                pointers.append(pointer)
                if not self._advapi32.AddAccessAllowedAceEx(
                        acl, 2, inheritance, 0x001F01FF, ctypes.c_void_p(pointer)):
                    raise OSError("AddAccessAllowedAceEx failed")
            flags = 0x4 | (0x80000000 if protected else 0x0)
            code = self._advapi32.SetNamedSecurityInfoW(
                str(path), 1, flags, None, None, acl, None)
            if code != 0:
                raise OSError(code, "SetNamedSecurityInfoW failed")
        finally:
            for pointer in pointers:
                self._kernel32.LocalFree(ctypes.c_void_p(pointer))


_EDITOR = _DaclEditor()
_EVERYONE = "S-1-1-0"


def _current_user_sid() -> str:
    return _load_tool()._SECURITY.current_user_sid()


def _protect_current_user_only(path: Path, *, directory: bool) -> None:
    """Positive case: protected DACL, current user as sole trustee."""
    _EDITOR.replace(path, [_current_user_sid()], directory=directory, protected=True)


def _grant_everyone(path: Path, *, directory: bool) -> None:
    """Negative case: a genuinely loose DACL - Everyone gains full access."""
    _EDITOR.replace(path, [_current_user_sid(), _EVERYONE],
                    directory=directory, protected=True)


# ---------------------------------------------------------------------------
# SYNTHETIC client store / service-side fixtures (frozen R1.3/R1.4/R2.2 shapes)
# ---------------------------------------------------------------------------

def _build_store(tmp_path: Path, *, strategy_id: int = 7, active: bool = True,
                 url: str = "http://127.0.0.1:17779/execution/v1/poll",
                 session_seed: str = "session", request_seed: str = "request",
                 protect_acl: bool = True):
    strategy_dir = tmp_path / "self_strategy"
    store = strategy_dir / ".ak_execution" / f"strategy-{strategy_id}"
    store.mkdir(parents=True)
    instance_id = _hex("instance")
    deployment_key = _hex("deployment", 64)
    identity = {"schema_version": "ne1_client_identity_v1",
                "instance_id": instance_id,
                "deployment_key": deployment_key,
                "strategy_id": strategy_id,
                "created_at": "2026-09-17T00:00:00+00:00"}
    request = {"schema_version": "ne1_pairing_request_v1",
               "request_id": _hex(request_seed),
               "active": active,
               "created_at": "2026-09-17T00:00:00+00:00",
               "instance_id": instance_id,
               "session_epoch": _hex(session_seed),
               "generation": _hex("generation-" + session_seed),
               "process_instance_id": _hex("process-" + session_seed),
               "deployment_key": deployment_key,
               "strategy_id": strategy_id,
               "adapter_version": "ne1-execution-20260917-pairing-v2-SYNTHETIC",
               "client_file_sha256": _hex("client-file", 64),
               "store_layout_version": 2,
               "store_directory": str(store),
               "exchange": "SHFE",
               "instrument_id": "rb2601",
               "hedge_flag": "speculation",
               "execution_api_url": url,
               "params_sha256": _hex("params-" + session_seed, 64)}
    _write(store / "identity.json", _json_line(identity))
    request_path = _write(store / "pairing-request.json", _json_line(request))
    if protect_acl:
        _protect_current_user_only(store, directory=True)
    return {"strategy_dir": strategy_dir, "store": store,
            "identity": identity, "request": request,
            "request_path": request_path,
            "request_raw": request_path.read_bytes(),
            "request_sha256": _sha_file(request_path)}


def _build_service_side(tmp_path: Path, box: dict, *, token: str = SYNTHETIC_TOKEN,
                        service_epoch: int = 3, service_instance: str = SERVICE_INSTANCE,
                        instance_id: str | None = None, session_epoch: str | None = None,
                        diagnostic: bool = False, contract_lock_sha256: str | None = None,
                        run_dir: str = "pairing-runs/run-1"):
    lock_path = _write(tmp_path / "contract-lock.json",
                       _json_line({"note": "SYNTHETIC contract lock"}))
    if contract_lock_sha256 is None:
        contract_lock_sha256 = _sha_file(lock_path)
    instance = instance_id if instance_id is not None else box["request"]["instance_id"]
    session = (session_epoch if session_epoch is not None
               else (None if diagnostic else box["request"]["session_epoch"]))
    pairing_dir = tmp_path / run_dir
    pairing_dir.mkdir(parents=True, exist_ok=True)
    _protect_current_user_only(pairing_dir, directory=True)
    source = pairing_dir / f"{instance}.token"
    record = {"schema_version": "ne1_service_pairing_v2",
              "instance_id": instance,
              "session_epoch": session,
              "service_instance_id": service_instance,
              "service_epoch": service_epoch,
              "contract_lock_sha256": contract_lock_sha256,
              "diagnostic_only": diagnostic,
              "token": token}
    _write(source, _json_line(record))
    service_start = {"event": "execution_started", "host": "127.0.0.1", "port": 17779,
                     "service_epoch": service_epoch,
                     "service_version": "ne1-service-20260917-pairing-v2",
                     "instance_id": service_instance,
                     "execution_enabled": False, "observer_only": True,
                     "observer_port_untouched": 17778,
                     "actors": ["actor-synth-1"], "instances": [instance],
                     "audit_log": str(tmp_path / "audit.jsonl"),
                     "contract_lock_sha256": contract_lock_sha256,
                     "pairing_dir": str(pairing_dir),
                     "pairings": [{"instance_id": instance,
                                   "session_epoch": session,
                                   "token_file": str(source),
                                   "diagnostic_only": diagnostic}]}
    start_path = _write(tmp_path / run_dir / "service-start.json", _json_line(service_start))
    return {"source": source, "record": record, "service_start": service_start,
            "service_start_path": start_path, "pairing_dir": pairing_dir,
            "lock_path": lock_path, "contract_lock_sha256": contract_lock_sha256}


def _install_argv(box: dict, service: dict, receipt: Path, *,
                  expected_client_sha256: str | None = None) -> list[str]:
    expected = expected_client_sha256 or box["request"]["client_file_sha256"]
    return ["install",
            "--client-store", str(box["store"]),
            "--client-request", str(box["request_path"]),
            "--source", str(service["source"]),
            "--service-start", str(service["service_start_path"]),
            "--contract-lock", str(service["lock_path"]),
            "--expected-client-sha256", expected,
            "--receipt", str(receipt)]


def _credential_path(box: dict) -> Path:
    return box["store"] / "pairing.json"


def _snapshot(directory: Path) -> dict:
    return {str(item.relative_to(directory)): (item.stat().st_size,
                                               item.stat().st_mtime_ns)
            for item in sorted(directory.rglob("*")) if item.is_file()}


# ---------------------------------------------------------------------------
# tool surface / version guards
# ---------------------------------------------------------------------------

def test_tool_version_and_no_secret_cli_surface():
    module = _load_tool()
    assert module.TOOL_VERSION == TOOL_VERSION
    source = TOOL.read_text(encoding="utf-8")
    assert '"--token"' not in source          # no token-value parameter (R2.1)
    assert '"--yes"' not in source            # AR1.3: no auto-confirm flag
    assert "os.environ" not in source and "getenv" not in source
    # AR1.3: stdin usage is confined to the interactive console seam - one
    # TTY probe (sys.stdin) and exactly ONE live line read (input()).
    assert source.count("input(") == 1, "exactly one console read allowed"
    assert source.count("sys.stdin") == 1, "one TTY probe allowed"
    console_block = source[source.index("def _console_attached"):]
    probe = console_block.split("def _console_read_line")[0]
    reader = console_block.split("def _console_read_line")[1]
    assert "sys.stdin" in probe and "input(" not in probe
    assert "input(" in reader.split("def ")[0]
    # AR-M04: no automatic arm call chain - cmd_arm is only wired to the CLI
    # subcommand handler, never invoked by another command or helper.
    assert source.count("cmd_arm") == 2
    result = subprocess.run([sys.executable, "-B", "-X", "utf8", str(TOOL), "--help"],
                            capture_output=True, text=True, cwd=str(ROOT))
    assert result.returncode == 0
    for command in ("inspect", "build-actor-manifest", "install",
                    "prepare-arm", "arm", "disarm"):
        assert command in result.stdout


def test_installer_version_bumped_to_pairing_v2():
    assert _load_installer().TOOL_VERSION == INSTALLER_VERSION


# ---------------------------------------------------------------------------
# M03: install happy paths with REAL DACL positive examples
# ---------------------------------------------------------------------------

def test_install_transfers_synthetic_credential_protected_dacl(tmp_path: Path):
    box = _build_store(tmp_path)
    service = _build_service_side(tmp_path, box)
    receipt = tmp_path / "receipts" / "pairing-transfer.json"
    receipt.parent.mkdir(parents=True)
    code, payload = _tool(_install_argv(box, service, receipt))
    assert code == 0, payload
    assert payload["status"] == "TRANSFERRED_AWAITING_HANDSHAKE"
    assert payload["transfer_kind"] == "FRESH"
    credential = _credential_path(box)
    assert credential.is_file()
    saved = json.loads(credential.read_text(encoding="utf-8"))
    assert saved["schema_version"] == "ne1_client_pairing_v1"
    assert saved["token"] == SYNTHETIC_TOKEN
    assert saved["request_id"] == box["request"]["request_id"]
    assert saved["request_sha256"] == box["request_sha256"]
    assert saved["instance_id"] == box["request"]["instance_id"]
    assert saved["session_epoch"] == box["request"]["session_epoch"]
    assert saved["generation"] == box["request"]["generation"]
    assert saved["deployment_key"] == box["request"]["deployment_key"]
    assert saved["strategy_id"] == box["request"]["strategy_id"]
    assert saved["params_sha256"] == box["request"]["params_sha256"]
    assert saved["client_file_sha256"] == box["request"]["client_file_sha256"]
    assert saved["execution_origin"] == "http://127.0.0.1:17779"
    assert saved["service_instance_id"] == SERVICE_INSTANCE
    assert saved["service_epoch"] == 3
    assert saved["contract_lock_sha256"] == service["contract_lock_sha256"]
    receipt_record = json.loads(receipt.read_text(encoding="utf-8"))
    assert receipt_record["tool_version"] == TOOL_VERSION
    assert receipt_record["status"] == "TRANSFERRED_AWAITING_HANDSHAKE"
    assert receipt_record["request"]["request_id"] == box["request"]["request_id"]
    assert receipt_record["request"]["session_epoch"] == box["request"]["session_epoch"]
    assert receipt_record["request"]["generation"] == box["request"]["generation"]
    assert receipt_record["service"]["service_epoch"] == 3
    assert "credential_slot_after_publish" in receipt_record["acl_verification"]
    # SYNTHETIC-token hygiene: stdout/result/receipt never carry the token
    assert SYNTHETIC_TOKEN not in json.dumps(payload)
    assert SYNTHETIC_TOKEN.encode("ascii") not in receipt.read_bytes()


def test_install_accepts_default_inherited_profile_dacl(tmp_path: Path):
    """Real positive case 2: profile-inherited privileged trustees pass."""
    box = _build_store(tmp_path, protect_acl=False)
    service = _build_service_side(tmp_path, box)
    # no explicit DACL anywhere: inherited Administrator/SYSTEM/OWNER_RIGHTS
    receipt = tmp_path / "receipt.json"
    code, payload = _tool(_install_argv(box, service, receipt))
    assert code == 0 and payload["transfer_kind"] == "FRESH", payload
    assert _credential_path(box).is_file()


# ---------------------------------------------------------------------------
# M03: REAL DACL negative examples (loose ACL rejects, credential never lands)
# ---------------------------------------------------------------------------

def test_install_rejects_loose_source_directory_dacl(tmp_path: Path):
    box = _build_store(tmp_path)
    service = _build_service_side(tmp_path, box)
    _grant_everyone(service["pairing_dir"], directory=True)
    receipt = tmp_path / "receipt.json"
    code, payload = _tool(_install_argv(box, service, receipt))
    assert code == 2 and payload["code"] == "PAIRING_ACL_RESTRICTED", payload
    assert not _credential_path(box).exists()
    assert not receipt.exists()


def test_install_rejects_loose_client_store_dacl(tmp_path: Path):
    box = _build_store(tmp_path)
    service = _build_service_side(tmp_path, box)
    _grant_everyone(box["store"], directory=True)
    receipt = tmp_path / "receipt.json"
    code, payload = _tool(_install_argv(box, service, receipt))
    assert code == 2 and payload["code"] == "PAIRING_ACL_RESTRICTED", payload
    assert not _credential_path(box).exists()
    assert not receipt.exists()


def test_install_dacl_proven_before_secret_read_and_write(tmp_path, monkeypatch):
    """Order proof: permission verification precedes the token entering the
    process (source read) and the disk (credential publication)."""
    tool = _load_tool()
    box = _build_store(Path(tmp_path))
    service = _build_service_side(Path(tmp_path), box)
    events: list[tuple[str, str]] = []
    real_verify = tool.verify_private_access
    real_read = Path.read_bytes
    real_publish = tool._atomic_publish

    def traced_verify(path, *, need_write):
        events.append(("acl", str(path)))
        return real_verify(path, need_write=need_write)

    def traced_read(self):
        events.append(("read", str(self)))
        return real_read(self)

    def traced_publish(path, payload):
        events.append(("publish", str(path)))
        return real_publish(path, payload)

    monkeypatch.setattr(tool, "verify_private_access", traced_verify)
    monkeypatch.setattr(Path, "read_bytes", traced_read)
    monkeypatch.setattr(tool, "_atomic_publish", traced_publish)
    receipt = Path(tmp_path) / "receipt.json"
    args = type("Args", (), {"client_store": str(box["store"]),
                             "client_request": str(box["request_path"]),
                             "source": str(service["source"]),
                             "service_start": str(service["service_start_path"]),
                             "contract_lock": str(service["lock_path"]),
                             "expected_client_sha256":
                                 box["request"]["client_file_sha256"],
                             "receipt": str(receipt)})()
    assert tool.cmd_install(args)["status"] == "TRANSFERRED_AWAITING_HANDSHAKE"
    first_acl = next(index for index, event in enumerate(events) if event[0] == "acl")
    source_read = next(index for index, event in enumerate(events)
                       if event[0] == "read"
                       and event[1] == str(service["source"]))
    publish = next(index for index, event in enumerate(events) if event[0] == "publish")
    assert first_acl < source_read < publish
    post_publish_acl = [index for index, event in enumerate(events)
                        if event[0] == "acl" and index > publish]
    assert post_publish_acl, "published credential DACL must be re-verified"


def test_install_acl_failure_never_reads_token(tmp_path, monkeypatch):
    """A failing permission check rejects before the token enters the process."""
    tool = _load_tool()
    box = _build_store(Path(tmp_path))
    service = _build_service_side(Path(tmp_path), box)
    real_read = Path.read_bytes
    reads: list[str] = []

    def traced_read(self):
        reads.append(str(self))
        return real_read(self)

    def failing_verify(path, *, need_write):
        raise tool.PairingError("PAIRING_ACL_RESTRICTED")

    monkeypatch.setattr(Path, "read_bytes", traced_read)
    monkeypatch.setattr(tool, "verify_private_access", failing_verify)
    receipt = Path(tmp_path) / "receipt.json"
    args = type("Args", (), {"client_store": str(box["store"]),
                             "client_request": str(box["request_path"]),
                             "source": str(service["source"]),
                             "service_start": str(service["service_start_path"]),
                             "contract_lock": str(service["lock_path"]),
                             "expected_client_sha256":
                                 box["request"]["client_file_sha256"],
                             "receipt": str(receipt)})()
    with pytest.raises(tool.PairingError) as raised:
        tool.cmd_install(args)
    assert str(raised.value) == "PAIRING_ACL_RESTRICTED"
    assert str(service["source"]) not in reads
    assert not _credential_path(box).exists()
    assert not receipt.exists()


# ---------------------------------------------------------------------------
# M03: rejection matrix (bad format / diagnostic / stale / mismatch / changed)
# ---------------------------------------------------------------------------

def test_install_rejects_legacy_two_line_token_file(tmp_path: Path):
    box = _build_store(tmp_path)
    service = _build_service_side(tmp_path, box)
    legacy = f"{box['request']['instance_id']}\n{SYNTHETIC_TOKEN}\n"
    _write(service["source"], legacy)
    code, payload = _tool(_install_argv(box, service, tmp_path / "receipt.json"))
    assert code == 2 and payload["code"] == "PAIRING_FORMAT_INVALID", payload


def test_install_rejects_unknown_schema_and_extra_fields(tmp_path: Path):
    box = _build_store(tmp_path)
    service = _build_service_side(tmp_path, box)
    record = dict(service["record"])
    record["schema_version"] = "ne1_service_pairing_v1"
    _write(service["source"], _json_line(record))
    code, payload = _tool(_install_argv(box, service, tmp_path / "r1.json"))
    assert code == 2 and payload["code"] == "PAIRING_FORMAT_INVALID", payload
    record = dict(service["record"])
    record["extra"] = "field"
    _write(service["source"], _json_line(record))
    code, payload = _tool(_install_argv(box, service, tmp_path / "r2.json"))
    assert code == 2 and payload["code"] == "PAIRING_FORMAT_INVALID", payload


def test_install_rejects_diagnostic_token(tmp_path: Path):
    box = _build_store(tmp_path)
    service = _build_service_side(tmp_path, box, instance_id="unregistered",
                                  diagnostic=True)
    code, payload = _tool(_install_argv(box, service, tmp_path / "receipt.json"))
    assert code == 2 and payload["code"] == "PAIRING_DIAGNOSTIC_ONLY", payload


def test_install_rejects_stale_service_epoch(tmp_path: Path):
    box = _build_store(tmp_path)
    service = _build_service_side(tmp_path, box, service_epoch=4)
    record = dict(service["record"])
    record["service_epoch"] = 3  # token from the previous service run
    _write(service["source"], _json_line(record))
    code, payload = _tool(_install_argv(box, service, tmp_path / "receipt.json"))
    assert code == 2 and payload["code"] == "PAIRING_SERVICE_MISMATCH", payload


def test_install_rejects_wrong_session_epoch(tmp_path: Path):
    box = _build_store(tmp_path)
    service = _build_service_side(tmp_path, box,
                                  session_epoch=_hex("other-session"))
    code, payload = _tool(_install_argv(box, service, tmp_path / "receipt.json"))
    assert code == 2 and payload["code"] == "PAIRING_CONTEXT_MISMATCH", payload


def test_install_rejects_wrong_instance(tmp_path: Path):
    box = _build_store(tmp_path)
    service = _build_service_side(tmp_path, box, instance_id=_hex("other-instance"))
    code, payload = _tool(_install_argv(box, service, tmp_path / "receipt.json"))
    assert code == 2 and payload["code"] == "PAIRING_CONTEXT_MISMATCH", payload


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:17779/execution/v1/poll?leak=1",
    "http://user:pass@127.0.0.1:17779/execution/v1/poll",
    "http://10.0.0.5:17779/execution/v1/poll",
    "http://127.0.0.1/execution/v1/poll",
    "https://127.0.0.1:17779/execution/v1/poll",
])
def test_install_rejects_non_loopback_or_decorated_origin(tmp_path: Path, url: str):
    box = _build_store(tmp_path, url=url)
    service = _build_service_side(tmp_path, box)
    code, payload = _tool(_install_argv(box, service, tmp_path / "receipt.json"))
    assert code == 2 and payload["code"] == "PAIRING_ORIGIN_INVALID", payload


def test_install_rejects_wrong_release_hash(tmp_path: Path):
    box = _build_store(tmp_path)
    service = _build_service_side(tmp_path, box)
    code, payload = _tool(_install_argv(box, service, tmp_path / "receipt.json",
                                        expected_client_sha256=_hex("other", 64)))
    assert code == 2 and payload["code"] == "PAIRING_CONTEXT_MISMATCH", payload


def test_install_rejects_contract_lock_mismatch(tmp_path: Path):
    box = _build_store(tmp_path)
    service = _build_service_side(tmp_path, box,
                                  contract_lock_sha256=_hex("other-lock", 64))
    code, payload = _tool(_install_argv(box, service, tmp_path / "receipt.json"))
    assert code == 2 and payload["code"] == "PAIRING_CONTEXT_MISMATCH", payload


def test_install_rejects_inactive_request(tmp_path: Path):
    box = _build_store(tmp_path, active=False)
    service = _build_service_side(tmp_path, box)
    code, payload = _tool(_install_argv(box, service, tmp_path / "receipt.json"))
    assert code == 2 and payload["code"] == "PAIRING_REQUEST_INACTIVE", payload


def test_install_missing_token_source(tmp_path: Path):
    box = _build_store(tmp_path)
    service = _build_service_side(tmp_path, box)
    service["source"].unlink()
    code, payload = _tool(_install_argv(box, service, tmp_path / "receipt.json"))
    assert code == 2 and payload["code"] == "PAIRING_MISSING", payload


def test_install_receipt_path_must_be_fresh(tmp_path: Path):
    box = _build_store(tmp_path)
    service = _build_service_side(tmp_path, box)
    receipt = _write(tmp_path / "receipt.json", "stale")
    code, payload = _tool(_install_argv(box, service, receipt))
    assert code == 2 and payload["code"] == "RECEIPT_PATH_MUST_BE_FRESH", payload


def test_install_rejects_token_leaked_into_service_start(tmp_path: Path):
    box = _build_store(tmp_path)
    service = _build_service_side(tmp_path, box)
    leaked = dict(service["service_start"])
    leaked["debug_token"] = SYNTHETIC_TOKEN
    _write(service["service_start_path"], _json_line(leaked))
    code, payload = _tool(_install_argv(box, service, tmp_path / "receipt.json"))
    assert code == 2 and payload["code"] == "PAIRING_SERVICE_START_LEAKED_TOKEN", payload


# ---------------------------------------------------------------------------
# M03: slot rotation, conflicts, idempotence, changed request, transfer lock
# ---------------------------------------------------------------------------

def _saved_credential(box: dict) -> dict:
    return json.loads(_credential_path(box).read_text(encoding="utf-8"))


def test_install_same_request_idempotent_and_conflict_refused(tmp_path: Path):
    box = _build_store(tmp_path)
    service = _build_service_side(tmp_path, box)
    assert _tool(_install_argv(box, service, tmp_path / "r1.json"))[0] == 0
    first_bytes = _credential_path(box).read_bytes()
    # identical retry: fresh receipt, byte-identical credential -> IDEMPOTENT
    code, payload = _tool(_install_argv(box, service, tmp_path / "r2.json"))
    assert code == 0 and payload["transfer_kind"] == "IDEMPOTENT", payload
    assert _credential_path(box).read_bytes() == first_bytes
    # same request, different secret/service context -> refused, file intact
    other = _build_service_side(tmp_path, box, token=SYNTHETIC_TOKEN_2,
                                service_instance=SERVICE_INSTANCE_2,
                                service_epoch=4, run_dir="pairing-runs/run-2")
    code, payload = _tool(_install_argv(box, other, tmp_path / "r3.json"))
    assert code == 2 and payload["code"] == "PAIRING_CREDENTIAL_CONFLICT", payload
    assert _credential_path(box).read_bytes() == first_bytes


def test_install_foreign_existing_credential_refused(tmp_path: Path):
    box = _build_store(tmp_path)
    service = _build_service_side(tmp_path, box)
    foreign = {"schema_version": "ne1_client_pairing_v1",
               "request_id": _hex("foreign-request"),
               "request_sha256": _hex("foreign-req", 64),
               "instance_id": _hex("foreign-instance"),
               "session_epoch": _hex("foreign-session"),
               "generation": _hex("foreign-generation"),
               "deployment_key": _hex("foreign-deploy", 64),
               "strategy_id": box["request"]["strategy_id"],
               "params_sha256": _hex("foreign-params", 64),
               "client_file_sha256": box["request"]["client_file_sha256"],
               "execution_origin": "http://127.0.0.1:17779",
               "service_instance_id": SERVICE_INSTANCE,
               "service_epoch": 3,
               "contract_lock_sha256": service["contract_lock_sha256"],
               "token": SYNTHETIC_TOKEN_2}
    slot_file = _write(_credential_path(box), _json_line(foreign))
    before = slot_file.read_bytes()
    code, payload = _tool(_install_argv(box, service, tmp_path / "receipt.json"))
    assert code == 2 and payload["code"] == "PAIRING_TARGET_SLOT_MISMATCH", payload
    assert slot_file.read_bytes() == before


def test_install_corrupt_existing_credential_refused(tmp_path: Path):
    box = _build_store(tmp_path)
    service = _build_service_side(tmp_path, box)
    slot_file = _write(_credential_path(box), b"not-json")
    code, payload = _tool(_install_argv(box, service, tmp_path / "receipt.json"))
    assert code == 2 and payload["code"].startswith("PAIRING_TARGET"), payload
    assert slot_file.read_bytes() == b"not-json"


def test_install_same_slot_rotation_under_new_active_request(tmp_path: Path):
    box = _build_store(tmp_path)
    service = _build_service_side(tmp_path, box)
    assert _tool(_install_argv(box, service, tmp_path / "r1.json"))[0] == 0
    # client stop/start: new request_id/session/generation, same slot identity
    restarted = _build_store(tmp_path / "restart", session_seed="session-2",
                             request_seed="request-2")
    # keep the same store directory as the first start
    restarted["store"] = box["store"]
    restarted["request_path"] = box["store"] / "pairing-request.json"
    _write(restarted["request_path"], _json_line({
        **restarted["request"], "store_directory": str(box["store"])}))
    restarted["request_raw"] = restarted["request_path"].read_bytes()
    restarted["request_sha256"] = _sha_bytes(restarted["request_raw"])
    # new service run: new instance/epoch/pairing dir/token
    new_service = _build_service_side(
        tmp_path / "restart", restarted, token=SYNTHETIC_TOKEN_2,
        service_instance=SERVICE_INSTANCE_2, service_epoch=4,
        run_dir="pairing-runs/run-2")
    old_token_bytes = service["source"].read_bytes()
    code, payload = _tool(_install_argv(restarted, new_service,
                                        tmp_path / "r2.json"))
    assert code == 0 and payload["transfer_kind"] == "ROTATED", payload
    saved = _saved_credential(box)
    assert saved["token"] == SYNTHETIC_TOKEN_2
    assert saved["request_id"] == restarted["request"]["request_id"]
    assert saved["session_epoch"] == restarted["request"]["session_epoch"]
    # the old service-side token file is untouched (service files never reused)
    assert service["source"].read_bytes() == old_token_bytes


def test_install_request_changed_during_transfer(tmp_path, monkeypatch):
    """A request change between pre-verification and publication is caught by
    the post-publication re-verification (PAIRING_REQUEST_CHANGED)."""
    tool = _load_tool()
    box = _build_store(Path(tmp_path))
    service = _build_service_side(Path(tmp_path), box)
    real_publish = tool._atomic_publish

    def mutating_publish(path, payload):
        changed = dict(box["request"])
        changed["params_sha256"] = _hex("changed-params", 64)
        _write(box["request_path"], _json_line(changed))
        return real_publish(path, payload)

    monkeypatch.setattr(tool, "_atomic_publish", mutating_publish)
    receipt = Path(tmp_path) / "receipt.json"
    args = type("Args", (), {"client_store": str(box["store"]),
                             "client_request": str(box["request_path"]),
                             "source": str(service["source"]),
                             "service_start": str(service["service_start_path"]),
                             "contract_lock": str(service["lock_path"]),
                             "expected_client_sha256":
                                 box["request"]["client_file_sha256"],
                             "receipt": str(receipt)})()
    with pytest.raises(tool.PairingError) as raised:
        tool.cmd_install(args)
    assert str(raised.value) == "PAIRING_REQUEST_CHANGED"
    assert not receipt.exists()


def test_install_transfer_lock_busy(tmp_path: Path):
    import msvcrt
    box = _build_store(tmp_path)
    service = _build_service_side(tmp_path, box)
    lock_path = box["store"] / "pairing-transfer.lock"
    handle = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    try:
        msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
        code, payload = _tool(_install_argv(box, service, tmp_path / "receipt.json"))
        assert code == 2 and payload["code"] == "PAIRING_TRANSFER_BUSY", payload
        assert not _credential_path(box).exists()
    finally:
        os.lseek(handle, 0, os.SEEK_SET)
        msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)
        os.close(handle)


def test_install_no_synthetic_token_in_any_evidence(tmp_path: Path):
    """After a successful transfer, the token exists ONLY in the client
    credential slot and the service-side source file."""
    box = _build_store(tmp_path)
    service = _build_service_side(tmp_path, box)
    receipt = tmp_path / "receipt.json"
    assert _tool(_install_argv(box, service, receipt))[0] == 0
    allowed = {str(_credential_path(box)), str(service["source"])}
    for path in tmp_path.rglob("*"):
        if path.is_file() and str(path) not in allowed:
            assert SYNTHETIC_TOKEN.encode("ascii") not in path.read_bytes(), path


# ---------------------------------------------------------------------------
# M03: inspect (zero writes, existence only) and validator units
# ---------------------------------------------------------------------------

def test_inspect_reports_existence_without_opening_credential(tmp_path: Path):
    box = _build_store(tmp_path)
    argv = ["inspect", "--client-store", str(box["store"])]
    code, payload = _tool(argv)
    assert code == 0, payload
    assert payload["credential_present"] is False
    assert payload["store"]["deterministic_slot_name"] == "strategy-7"
    assert payload["store"]["layout_version"] == 2
    assert payload["request"]["instance_id"] == box["request"]["instance_id"]
    assert payload["request"]["raw_sha256"] == box["request_sha256"]
    before = _snapshot(box["store"])
    # a corrupt credential file is reported as PRESENT but never opened
    garbage = b"garbage-not-json"
    _write(_credential_path(box), garbage)
    code, payload = _tool(argv)
    assert code == 0 and payload["credential_present"] is True, payload
    after = _snapshot(box["store"])
    assert set(after) == set(before) | {"pairing.json"}
    assert after["pairing.json"][0] == len(garbage)
    for name, snapshot in before.items():
        assert after[name] == snapshot


def test_inspect_zero_writes_proof(tmp_path: Path):
    box = _build_store(tmp_path)
    before = _snapshot(box["store"])
    assert _tool(["inspect", "--client-store", str(box["store"])])[0] == 0
    assert _snapshot(box["store"]) == before


def test_inspect_rejects_store_without_identity(tmp_path: Path):
    empty = tmp_path / "not-a-store"
    empty.mkdir()
    code, payload = _tool(["inspect", "--client-store", str(empty)])
    assert code == 2 and payload["code"] == "STORE_IDENTITY_MISSING", payload


def test_service_start_validator_consumes_frozen_launcher_line(tmp_path: Path):
    tool = _load_tool()
    box = _build_store(tmp_path)
    service = _build_service_side(tmp_path, box)
    record = json.loads(service["service_start_path"].read_text(encoding="utf-8"))
    normalized = tool.validate_service_start(record)
    assert normalized["service_instance_id"] == SERVICE_INSTANCE
    assert normalized["service_epoch"] == 3
    assert normalized["instances"][box["request"]["instance_id"]] == {
        "session_epoch": box["request"]["session_epoch"],
        "token_path": str(service["source"])}
    for mutation in ({"execution_enabled": True}, {"event": "other"},
                     {"instance_id": "bad id!"}, {"pairings": []}):
        with pytest.raises(tool.PairingError):
            tool.validate_service_start({**record, **mutation})


def test_execution_origin_rules():
    tool = _load_tool()
    assert tool.execution_origin("http://127.0.0.1:17779/execution/v1/poll") \
        == "http://127.0.0.1:17779"
    for bad in ("http://127.0.0.1:17779/poll?x=1", "http://127.0.0.1:17779#f",
                "http://u:p@127.0.0.1:17779/p", "http://localhost:17779/p",
                "http://127.0.0.1/p", "https://127.0.0.1:17779/p"):
        with pytest.raises(tool.PairingError):
            tool.execution_origin(bad)


# ---------------------------------------------------------------------------
# X01 (NE1-ARB-LM-X01-001 RULING X01-01/02/03): the exact service-ID syntax
# ``execution-<16 lowercase hex>`` enforced at all three LM-D entrances.
# Leading-zero exec16 stays legal (V01 positive / V03 old-file compat), value
# matching is never replaced by syntax (V02), and a non-compliant existing
# pairing.json rejects with its original bytes preserved (X01-03).
# ---------------------------------------------------------------------------

X01_LEGAL_EXEC16 = SERVICE_INSTANCE_2      # "execution-0123456789abcdef"
X01_BAD_SERVICE_IDS = (
    ("bare-hex32", _hex("x01-bare-hex32")),
    ("execution-plus-32hex", "execution-" + _hex("x01-wide", 32)),
    ("wrong-prefix", "observer-0123456789abcdef"),
    ("uppercase-prefix", "Execution-0123456789abcdef"),
    ("uppercase-hex-suffix", "execution-0123456789ABCDEF"),
    ("15-hex-suffix", "execution-0123456789abcde"),
    ("17-hex-suffix", "execution-0123456789abcdef0"),
    ("non-hex-suffix", "execution-0123456789abcdeg"),
    ("newline-padded", "execution-0123456789abcdef\n"),
    ("non-string-null", None),
    ("non-string-int", 12345),
    ("non-string-bool", True),
)


def _x01_service_pairing(service_instance_id) -> dict:
    return {"schema_version": "ne1_service_pairing_v2",
            "instance_id": _hex("x01-instance"),
            "session_epoch": _hex("x01-session"),
            "service_instance_id": service_instance_id,
            "service_epoch": 3,
            "contract_lock_sha256": _hex("x01-lock", 64),
            "diagnostic_only": False,
            "token": SYNTHETIC_TOKEN}


def _x01_client_pairing(service_instance_id) -> dict:
    return {"schema_version": "ne1_client_pairing_v1",
            "request_id": _hex("x01-request"),
            "request_sha256": _hex("x01-request-raw", 64),
            "instance_id": _hex("x01-instance"),
            "session_epoch": _hex("x01-session"),
            "generation": _hex("x01-generation"),
            "deployment_key": _hex("x01-deployment", 64),
            "strategy_id": 7,
            "params_sha256": _hex("x01-params", 64),
            "client_file_sha256": _hex("x01-client-file", 64),
            "execution_origin": "http://127.0.0.1:17779",
            "service_instance_id": service_instance_id,
            "service_epoch": 3,
            "contract_lock_sha256": _hex("x01-lock", 64),
            "token": SYNTHETIC_TOKEN}


def _x01_service_start(service_instance_id) -> dict:
    instance = _hex("x01-instance")
    return {"event": "execution_started", "host": "127.0.0.1", "port": 17779,
            "service_epoch": 3,
            "service_version": "ne1-service-20260917-pairing-v2",
            "instance_id": service_instance_id,
            "execution_enabled": False,
            "contract_lock_sha256": _hex("x01-lock", 64),
            "pairing_dir": "C:/synthetic/x01/pairing-runs/run-1",
            "pairings": [{"instance_id": instance,
                          "session_epoch": _hex("x01-session"),
                          "token_file": f"C:/synthetic/x01/pairing-runs/run-1/"
                                        f"{instance}.token",
                          "diagnostic_only": False}]}


def test_x01_legal_exec16_passes_all_three_entrances():
    """V01 positive / V03 compat: the leading-zero SYNTHETIC
    ``execution-0123456789abcdef`` passes each entrance unchanged (no trim,
    no casefold, no reformatting); the internal mapping still carries the
    verbatim value."""
    tool = _load_tool()
    assert tool.validate_service_pairing(
        _x01_service_pairing(X01_LEGAL_EXEC16))["service_instance_id"] \
        == X01_LEGAL_EXEC16
    assert tool.validate_client_pairing(
        _x01_client_pairing(X01_LEGAL_EXEC16))["service_instance_id"] \
        == X01_LEGAL_EXEC16
    assert tool.validate_service_start(
        _x01_service_start(X01_LEGAL_EXEC16))["service_instance_id"] \
        == X01_LEGAL_EXEC16


@pytest.mark.parametrize("label,bad", X01_BAD_SERVICE_IDS,
                         ids=[label for label, _ in X01_BAD_SERVICE_IDS])
def test_x01_rejects_non_compliant_service_ids_at_all_three_entrances(label, bad):
    """V01 negative: bare hex32, execution-<32hex>, wrong/uppercase prefix,
    case/length/non-hex suffix variants, padding and non-strings are
    refused by EVERY entrance with that entrance's frozen error code."""
    tool = _load_tool()
    with pytest.raises(tool.PairingError) as raised:
        tool.validate_service_pairing(_x01_service_pairing(bad))
    assert str(raised.value) == "PAIRING_FORMAT_INVALID"
    with pytest.raises(tool.PairingError) as raised:
        tool.validate_client_pairing(_x01_client_pairing(bad))
    assert str(raised.value) == "PAIRING_TARGET_INVALID"
    with pytest.raises(tool.PairingError) as raised:
        tool.validate_service_start(_x01_service_start(bad))
    assert str(raised.value) == "PAIRING_SERVICE_START_INVALID"


def test_x01_install_leading_zero_exec16_written_verbatim(tmp_path: Path):
    """V03: a compliant old-style ``execution-<16hex>`` value (leading
    zeros) completes the real tool handover and is written byte-exact into
    the credential and the receipt - format legality is not transformation."""
    box = _build_store(tmp_path)
    service = _build_service_side(tmp_path, box, service_instance=X01_LEGAL_EXEC16)
    receipt = tmp_path / "receipt.json"
    code, payload = _tool(_install_argv(box, service, receipt))
    assert code == 0, payload
    saved = _saved_credential(box)
    assert saved["service_instance_id"] == X01_LEGAL_EXEC16
    assert saved["service_epoch"] == 3
    receipt_record = json.loads(receipt.read_text(encoding="utf-8"))
    assert receipt_record["service"]["service_instance_id"] == X01_LEGAL_EXEC16


def test_x01_install_rejects_bare_hex32_source_service_identity(tmp_path: Path):
    """Tool-level entrance 1: the misused client hex32 identity in the
    service pairing file rejects with PAIRING_FORMAT_INVALID before any
    credential is published."""
    box = _build_store(tmp_path)
    service = _build_service_side(tmp_path, box)
    record = dict(service["record"])
    record["service_instance_id"] = _hex("x01-bare-hex32")
    _write(service["source"], _json_line(record))
    code, payload = _tool(_install_argv(box, service, tmp_path / "receipt.json"))
    assert code == 2 and payload["code"] == "PAIRING_FORMAT_INVALID", payload
    assert not _credential_path(box).exists()
    assert not (tmp_path / "receipt.json").exists()


def test_x01_install_rejects_wrong_prefix_service_start_identity(tmp_path: Path):
    """Tool-level entrance 3: a wrong-prefix top-level service identity in
    service-start.json rejects with PAIRING_SERVICE_START_INVALID."""
    box = _build_store(tmp_path)
    service = _build_service_side(tmp_path, box)
    start = dict(service["service_start"])
    start["instance_id"] = "observer-0123456789abcdef"
    _write(service["service_start_path"], _json_line(start))
    code, payload = _tool(_install_argv(box, service, tmp_path / "receipt.json"))
    assert code == 2 and payload["code"] == "PAIRING_SERVICE_START_INVALID", payload
    assert not _credential_path(box).exists()
    assert not (tmp_path / "receipt.json").exists()


def test_x01_legal_but_different_service_ids_still_mismatch(tmp_path: Path):
    """V02: syntax never replaces value matching - two individually legal
    service IDs under the same epoch are refused as PAIRING_SERVICE_MISMATCH
    and the target file is not published."""
    box = _build_store(tmp_path)
    service = _build_service_side(tmp_path, box)  # SERVICE_INSTANCE everywhere
    diverged = dict(service["service_start"])
    diverged["instance_id"] = X01_LEGAL_EXEC16  # legal, but != source value
    _write(service["service_start_path"], _json_line(diverged))
    code, payload = _tool(_install_argv(box, service, tmp_path / "receipt.json"))
    assert code == 2 and payload["code"] == "PAIRING_SERVICE_MISMATCH", payload
    assert not _credential_path(box).exists()


def test_x01_existing_non_compliant_credential_slot_keeps_bytes(tmp_path: Path):
    """X01-03: an existing pairing.json whose service_instance_id is a bare
    hex32 rejects with PAIRING_TARGET_INVALID and its original bytes are
    preserved - no silent overwrite even with a fresh valid request."""
    box = _build_store(tmp_path)
    service = _build_service_side(tmp_path, box)
    stale = _x01_client_pairing(_hex("x01-bare-hex32"))
    slot_file = _write(_credential_path(box), _json_line(stale))
    before = slot_file.read_bytes()
    code, payload = _tool(_install_argv(box, service, tmp_path / "receipt.json"))
    assert code == 2 and payload["code"] == "PAIRING_TARGET_INVALID", payload
    assert slot_file.read_bytes() == before
    assert not (tmp_path / "receipt.json").exists()


# ---------------------------------------------------------------------------
# build-actor-manifest (non-secret offline registration input)
# ---------------------------------------------------------------------------

def _binding_template(**overrides) -> dict:
    template = {"actor_id": "actor-synth-1", "account_key": "acct-synth-1",
                "strategy_key": "strat-synth-1", "strategy_id": 7,
                "trading_day": "2026-09-17", "exchange": "SHFE",
                "instrument_id": "rb2601", "hedge_flag": "speculation"}
    template.update(overrides)
    return template


def _authorization(tmp_path: Path, action_id: str = "op-action-1") -> Path:
    return _write(tmp_path / "operator-authorization.json", _json_line(
        {"operator_action_id": action_id, "authorized_by": "user",
         "note": "SYNTHETIC offline registration approval"}))


def _build_argv(box: dict, service: dict, output: Path, *,
                template: dict | None = None,
                action_id: str = "op-action-1") -> list[str]:
    template_path = _write(Path(service["lock_path"]).parent / "binding-template.json",
                           _json_line(template or _binding_template()))
    return ["build-actor-manifest",
            "--client-request", str(box["request_path"]),
            "--binding-template", str(template_path),
            "--contract-lock", str(service["lock_path"]),
            "--operator-action-id", action_id,
            "--operator-authorization",
            str(_authorization(Path(service["lock_path"]).parent, action_id)),
            "--output-directory", str(output)]


def _prepare_output(output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    return output


def test_build_actor_manifest_creates_registration_inputs(tmp_path: Path):
    box = _build_store(tmp_path)
    service = _build_service_side(tmp_path, box)
    output = _prepare_output(tmp_path / "registration" / box["request"]["request_id"])
    code, payload = _tool(_build_argv(box, service, output))
    assert code == 0, payload
    record = json.loads((output / "register-actor.json").read_text(encoding="utf-8"))
    assert record["schema_version"] == "ne1_adapter_registration_v1"
    assert record["actor_id"] == "actor-synth-1"
    binding = record["bindings"][0]
    # instance/session come from the CURRENT request, never guessed (R2.3)
    assert binding["instance_id"] == box["request"]["instance_id"]
    assert binding["session_epoch"] == box["request"]["session_epoch"]
    assert binding["strategy_id"] == 7 and binding["trading_day"] == "2026-09-17"
    from ak_trading.infiny_execution import contracts as codec
    from ak_trading.infiny_execution.native_evidence import OperatorRegistrationManifest
    manifest = codec.decode((output / "register-actor-manifest.json").read_text(encoding="utf-8"),
                            OperatorRegistrationManifest,
                            schema_version=codec.V2_SCHEMA_VERSION)
    assert manifest.contract_lock_sha256 == service["contract_lock_sha256"]


def test_build_actor_manifest_rejections(tmp_path: Path):
    box = _build_store(tmp_path, active=False)
    service = _build_service_side(tmp_path, box)
    output = tmp_path / "out-inactive"
    code, payload = _tool(_build_argv(box, service, output))
    assert code == 2 and payload["code"] == "PAIRING_REQUEST_INACTIVE", payload

    box = _build_store(tmp_path / "ok", request_seed="request-ok")
    service = _build_service_side(tmp_path / "ok", box)
    # template/request mismatch (strategy_id and instrument)
    for mismatch in ({"strategy_id": 8}, {"instrument_id": "cu2601"}):
        output = _prepare_output(tmp_path / "ok" / f"out-{sorted(mismatch)[0]}")
        code, payload = _tool(_build_argv(box, service, output, template=_binding_template(**mismatch)))
        assert code == 2 and payload["code"] == "BINDING_TEMPLATE_REQUEST_MISMATCH", payload
    # existing output directory
    output = tmp_path / "ok" / "out-exists"
    output.mkdir(parents=True)
    code, payload = _tool(_build_argv(box, service, output))
    assert code == 2 and payload["code"] == "OUTPUT_DIRECTORY_MUST_BE_FRESH", payload
    # authorization action mismatch: the authorization artifact names a
    # different operator action than the one requested on the command line
    output = _prepare_output(tmp_path / "ok" / "out-auth")
    argv = _build_argv(box, service, output, action_id="op-action-1")
    argv[argv.index("--operator-action-id") + 1] = "other-action"
    code, payload = _tool(argv)
    assert code == 2 and payload["code"] == "OPERATOR_AUTHORIZATION_ACTION_MISMATCH", payload


def test_register_actor_roundtrip_with_expected_revision(tmp_path: Path):
    """The generated manifest is consumable by the frozen register-actor CLI,
    including the RULING 03 CAS discipline (LM-D manifest face)."""
    box = _build_store(tmp_path, request_seed="request-a",
                       session_seed="session-a")
    service = _build_service_side(tmp_path, box)
    output = _prepare_output(tmp_path / "reg" / "a")
    assert _tool(_build_argv(box, service, output))[0] == 0
    journal = tmp_path / "journal" / "exec.db"
    journal.parent.mkdir(parents=True)
    code, payload = _run(LAUNCHER, [
        "register-actor", "--journal", str(journal), "--initialize",
        "--manifest", str(output / "register-actor-manifest.json"),
        "--contract-lock", str(service["lock_path"])])
    assert code == 0, payload
    assert payload["event"] == "registered"
    assert payload["registration_revision"] == 1
    assert payload["expected_registration_revision"] is None

    # same manifest retry stays idempotent at revision 1
    code, payload = _run(LAUNCHER, [
        "register-actor", "--journal", str(journal),
        "--manifest", str(output / "register-actor-manifest.json"),
        "--contract-lock", str(service["lock_path"]),
        "--expected-registration-revision", "1"])
    assert code == 0 and payload["registration_revision"] == 1, payload

    # a NEW session (client stop/start) updates the binding via explicit CAS
    restarted = _build_store(tmp_path / "restart", request_seed="request-b",
                             session_seed="session-b")
    restarted["request_path"] = box["store"] / "pairing-request.json"
    _write(restarted["request_path"], _json_line({
        **restarted["request"], "store_directory": str(box["store"])}))
    restarted["request_raw"] = restarted["request_path"].read_bytes()
    service_b = _build_service_side(tmp_path / "restart", restarted,
                                    run_dir="pairing-runs/run-2")
    output_b = _prepare_output(tmp_path / "reg" / "b")
    assert _tool(_build_argv(restarted, service_b, output_b,
                             action_id="op-action-2"))[0] == 0
    code, payload = _run(LAUNCHER, [
        "register-actor", "--journal", str(journal),
        "--manifest", str(output_b / "register-actor-manifest.json"),
        "--contract-lock", str(service_b["lock_path"]),
        "--expected-registration-revision", "99"])
    assert code == 2, payload
    assert payload["reason"].startswith("REGISTRATION_"), payload
    assert "ACTOR_CONFLICT" in payload["detail"], payload
    code, payload = _run(LAUNCHER, [
        "register-actor", "--journal", str(journal),
        "--manifest", str(output_b / "register-actor-manifest.json"),
        "--contract-lock", str(service_b["lock_path"]),
        "--expected-registration-revision", "1"])
    assert code == 0 and payload["registration_revision"] == 2, payload


# ---------------------------------------------------------------------------
# M08: upgraded installer - deterministic slot discipline
# ---------------------------------------------------------------------------

def _sandbox(tmp_path: Path) -> dict:
    strategy = tmp_path / "strategy"
    strategy.mkdir(parents=True)
    for name in ("AKObserverBridge.py", "AIBridge.py"):
        (strategy / name).write_text(OBSERVER_FAKE, encoding="utf-8")
    release = tmp_path / "release"
    release.mkdir(parents=True)
    (release / "AKExecutionBridge.py").write_bytes(CLIENT_SOURCE.read_bytes())
    return {"strategy": strategy, "release": release,
            "observer_sha": _sha_file(strategy / "AIBridge.py"),
            "release_sha": _sha_file(release / "AKExecutionBridge.py")}


def _operator_evidence(tmp_path: Path) -> dict:
    return {
        "stopped": _write(tmp_path / "stopped.json", _json_line({
            "instance": "SANDBOX-INST", "generation": "c" * 16,
            "stopped_at": "2026-09-17T00:00:00Z", "stop_log_ref": "sandbox/log",
            "confirmed_by": "user", "reload_pending": True})),
        "pending": _write(tmp_path / "pending.json", _json_line({
            "journal_pending_attempts": 0, "client_unacked_events": 0,
            "real_native_send_attempts": 0, "real_native_cancel_attempts": 0,
            "recorded_at": "2026-09-17T00:00:00Z"})),
        "gate": _write(tmp_path / "gate.json", _json_line({
            "gate_closed": True, "service_stopped_issuing": True,
            "recorded_at": "2026-09-17T00:00:00Z",
            "evidence_refs": ["sandbox-gate-closed"]})),
    }


def _plan_args(box: dict, evidence: Path, *, strategy_id: int | None = 7,
               client_store: str = "absent") -> list[str]:
    argv = ["plan", "--strategy-directory", str(box["strategy"]),
            "--release-directory", str(box["release"]),
            "--evidence-directory", str(evidence),
            "--journal", "absent",
            "--client-store", client_store,
            "--pending-state", str(box["pending"]),
            "--expected-source-sha256", box["release_sha"],
            "--expected-target-sha256", "ABSENT",
            "--observer-expected-sha256", box["observer_sha"]]
    if strategy_id is not None:
        argv += ["--strategy-id", str(strategy_id)]
    return argv


def _apply_args(box: dict, evidence: Path, *, strategy_id: int | None = 7,
                client_store: str = "absent", target: str = "ABSENT") -> list[str]:
    argv = ["apply", "--strategy-directory", str(box["strategy"]),
            "--release-directory", str(box["release"]),
            "--evidence-directory", str(evidence),
            "--expected-source-sha256", box["release_sha"],
            "--expected-target-sha256", target,
            "--stopped-evidence", str(box["stopped"]),
            "--journal", "absent",
            "--client-store", client_store,
            "--pending-state", str(box["pending"]),
            "--observer-expected-sha256", box["observer_sha"]]
    if strategy_id is not None:
        argv += ["--strategy-id", str(strategy_id)]
    return argv


def _slot(tmp_path: Path, strategy_id: int = 7) -> Path:
    slot = tmp_path / "strategy" / ".ak_execution" / f"strategy-{strategy_id}"
    slot.mkdir(parents=True, exist_ok=True)
    return slot


def _store_databases(slot: Path) -> None:
    connection = sqlite3.connect(str(slot / "receipts.db"))
    connection.execute("CREATE TABLE receipts (id TEXT PRIMARY KEY)")
    connection.execute("INSERT INTO receipts VALUES ('r-1')")
    connection.commit()
    connection.close()
    connection = sqlite3.connect(str(slot / "outbox.db"))
    connection.execute("CREATE TABLE events (id TEXT PRIMARY KEY, acked INTEGER, body TEXT)")
    connection.execute("INSERT INTO events VALUES ('e-1', 1, '{}')")
    connection.commit()
    connection.close()


def test_plan_measures_slot_and_never_creates_store(tmp_path: Path):
    box = _sandbox(tmp_path)
    box.update(_operator_evidence(tmp_path))
    code, payload = _installer(_plan_args(box, tmp_path / "plan"))
    assert code == 0 and payload["blockers"] == [], payload
    record = json.loads((tmp_path / "plan" / "deployment-plan.json").read_text(encoding="utf-8"))
    slot_record = record["client_store"]
    assert slot_record["declared"] == "absent" and slot_record["path"] == "absent"
    assert slot_record["strategy_id"] == 7
    assert slot_record["slot_exists"] is False
    assert slot_record["existing_slots"] == []
    assert slot_record["expected_slot_path"].endswith(f".ak_execution{os.sep}strategy-7") \
        or slot_record["expected_slot_path"].endswith(".ak_execution/strategy-7")
    # M08: the plan must not create the store (or its root)
    assert not (tmp_path / "strategy" / ".ak_execution").exists()


def test_plan_without_strategy_id_records_legacy_null(tmp_path: Path):
    box = _sandbox(tmp_path)
    box.update(_operator_evidence(tmp_path))
    code, payload = _installer(_plan_args(box, tmp_path / "plan", strategy_id=None))
    assert code == 0, payload
    record = json.loads((tmp_path / "plan" / "deployment-plan.json").read_text(encoding="utf-8"))
    assert record["client_store"]["strategy_id"] is None


def test_plan_rejects_absent_when_slot_exists(tmp_path: Path):
    box = _sandbox(tmp_path)
    box.update(_operator_evidence(tmp_path))
    slot = _slot(tmp_path)
    _store_databases(slot)
    for strategy_id in (7, None):
        code, payload = _installer(_plan_args(box, tmp_path / f"plan-{strategy_id}",
                                              strategy_id=strategy_id))
        assert code == 2, payload
        assert payload["code"] == "CLIENT_STORE_SLOT_EXISTS_MUST_BE_EXPLICIT", payload


def test_plan_rejects_wrong_or_foreign_store_declaration(tmp_path: Path):
    box = _sandbox(tmp_path)
    box.update(_operator_evidence(tmp_path))
    slot = _slot(tmp_path, strategy_id=7)
    _store_databases(slot)
    foreign = tmp_path / "foreign-store"
    foreign.mkdir()
    # declared absent while any slot exists is never acceptable (M08)
    code, payload = _installer(_plan_args(box, tmp_path / "p0", strategy_id=8))
    assert code == 2, payload
    assert payload["code"] == "CLIENT_STORE_SLOT_EXISTS_MUST_BE_EXPLICIT", payload
    # --strategy-id 8 while declaring slot 7's directory: slot mismatch
    code, payload = _installer(_plan_args(box, tmp_path / "p1", strategy_id=8,
                                          client_store=str(slot)))
    assert code == 2 and payload["code"] == "CLIENT_STORE_SLOT_MISMATCH", payload
    # a directory outside .ak_execution/strategy-* is not the production store
    code, payload = _installer(_plan_args(box, tmp_path / "p2", strategy_id=7,
                                          client_store=str(foreign)))
    assert code == 2 and payload["code"] == "CLIENT_STORE_SLOT_MISMATCH", payload
    # the exact actual slot directory is accepted
    code, payload = _installer(_plan_args(box, tmp_path / "p3", strategy_id=7,
                                          client_store=str(slot)))
    assert code == 0, payload
    record = json.loads((tmp_path / "p3" / "deployment-plan.json").read_text(encoding="utf-8"))
    assert record["client_store"]["slot_exists"] is True
    assert record["client_store"]["path"] == str(slot)


def test_apply_with_existing_slot_backs_up_business_dbs_only(tmp_path: Path):
    box = _sandbox(tmp_path)
    box.update(_operator_evidence(tmp_path))
    slot = _slot(tmp_path, strategy_id=7)
    _store_databases(slot)
    identity = {"schema_version": "ne1_client_identity_v1",
                "instance_id": _hex("m08-instance"), "deployment_key": _hex("m08", 64),
                "strategy_id": 7, "created_at": "2026-09-17T00:00:00+00:00"}
    _write(slot / "identity.json", _json_line(identity))
    request = {"schema_version": "ne1_pairing_request_v1",
               "request_id": _hex("m08-request"), "active": True,
               "created_at": "2026-09-17T00:00:00+00:00",
               "instance_id": identity["instance_id"],
               "session_epoch": _hex("m08-session"), "generation": _hex("m08-gen"),
               "process_instance_id": _hex("m08-proc"),
               "deployment_key": identity["deployment_key"], "strategy_id": 7,
               "adapter_version": "SYNTHETIC", "client_file_sha256": _hex("m08c", 64),
               "store_layout_version": 2, "store_directory": str(slot),
               "exchange": "SHFE", "instrument_id": "rb2601",
               "hedge_flag": "speculation",
               "execution_api_url": "http://127.0.0.1:17779/execution/v1/poll",
               "params_sha256": _hex("m08p", 64)}
    _write(slot / "pairing-request.json", _json_line(request))
    pairing_slot_file = _write(slot / "pairing.json", _json_line(
        {"schema_version": "ne1_client_pairing_v1",
         "request_id": request["request_id"], "request_sha256": _hex("m08r", 64),
         "instance_id": identity["instance_id"],
         "session_epoch": request["session_epoch"], "generation": request["generation"],
         "deployment_key": identity["deployment_key"], "strategy_id": 7,
         "params_sha256": request["params_sha256"],
         "client_file_sha256": request["client_file_sha256"],
         "execution_origin": "http://127.0.0.1:17779",
         "service_instance_id": SERVICE_INSTANCE, "service_epoch": 3,
         "contract_lock_sha256": _hex("m08l", 64), "token": SYNTHETIC_TOKEN}))
    pairing_before = pairing_slot_file.read_bytes()
    identity_before = (slot / "identity.json").read_bytes()
    request_before = (slot / "pairing-request.json").read_bytes()

    code, payload = _installer(_apply_args(box, tmp_path / "deploy",
                                           strategy_id=7, client_store=str(slot)))
    assert code == 0 and payload["status"] == "deployed", payload
    receipt = json.loads((tmp_path / "deploy" / "deployment.json").read_text(encoding="utf-8"))
    assert receipt["tool_version"] == INSTALLER_VERSION
    store_record = receipt["backups"]["client_store"]
    assert store_record["strategy_id"] == 7
    assert store_record["slot_exists"] is True
    assert store_record["path"] == str(slot)

    backup_store = tmp_path / "deploy" / "backup" / "client-store"
    backed_up = sorted(path.name for path in backup_store.iterdir())
    assert backed_up == ["outbox.db", "receipts.db"]
    counts = store_record["files"]
    assert counts["receipts.db"]["receipt_count"] == 1
    assert counts["outbox.db"]["unacked_events"] == 0

    # M08: credentials and wiring are never modified by apply ...
    assert pairing_slot_file.read_bytes() == pairing_before
    assert (slot / "identity.json").read_bytes() == identity_before
    assert (slot / "pairing-request.json").read_bytes() == request_before
    # ... and the SYNTHETIC credential never leaks into ordinary evidence
    for path in (tmp_path / "deploy").rglob("*"):
        if path.is_file():
            assert SYNTHETIC_TOKEN.encode("ascii") not in path.read_bytes(), path


def test_rollback_restores_code_only_leaving_store_intact(tmp_path: Path):
    box = _sandbox(tmp_path)
    box.update(_operator_evidence(tmp_path))
    slot = _slot(tmp_path, strategy_id=7)
    _store_databases(slot)
    code, payload = _installer(_apply_args(box, tmp_path / "deploy",
                                           strategy_id=7, client_store=str(slot)))
    assert code == 0, payload
    pairing_slot_file = _write(slot / "pairing.json", _json_line(
        {"schema_version": "ne1_client_pairing_v1",
         "request_id": _hex("rb-request"), "request_sha256": _hex("rb1", 64),
         "instance_id": _hex("rb-instance"), "session_epoch": _hex("rb-session"),
         "generation": _hex("rb-gen"), "deployment_key": _hex("rb2", 64),
         "strategy_id": 7, "params_sha256": _hex("rb3", 64),
         "client_file_sha256": _hex("rb4", 64),
         "execution_origin": "http://127.0.0.1:17779",
         "service_instance_id": SERVICE_INSTANCE, "service_epoch": 3,
         "contract_lock_sha256": _hex("rb5", 64), "token": SYNTHETIC_TOKEN}))
    pairing_before = pairing_slot_file.read_bytes()
    receipts_before = (slot / "receipts.db").read_bytes()

    code, payload = _installer([
        "rollback", "--strategy-directory", str(box["strategy"]),
        "--evidence-directory", str(tmp_path / "deploy"),
        "--rollback-evidence-directory", str(tmp_path / "rollback"),
        "--gate-evidence", str(box["gate"]),
        "--observer-expected-sha256", box["observer_sha"]])
    assert code == 0 and payload["state"] == "REMOVED_WAS_ABSENT", payload
    assert not (box["strategy"] / "AKExecutionBridge.py").exists()
    # M08: business facts and credentials are NOT rolled back or restored
    assert pairing_slot_file.read_bytes() == pairing_before
    assert (slot / "receipts.db").read_bytes() == receipts_before
    assert (tmp_path / "rollback" / "rollback.json").is_file()
    assert _sha_file(box["strategy"] / "AIBridge.py") == box["observer_sha"]


# ---------------------------------------------------------------------------
# NE1-ARB-ARMING-001 RULING 06 tool-side matrix (ARM-LM-D).
# The client-side slots (arming-plan/request/challenge/state) are SYNTHETIC
# fixtures written here to the frozen AR4.2 schemas; the production writer is
# the ARM-LM-C client. Every value is SYNTHETIC and sandbox-local.
# ---------------------------------------------------------------------------

def _utc_iso(moment: datetime) -> str:
    # X01 codec vector: the frozen client/tool codec emits the ``Z`` suffix
    # (AKExecutionBridge._iso / tool _utc_text); string comparisons against
    # tool-emitted timestamps require the same suffix form on this side.
    return moment.isoformat().replace("+00:00", "Z")


def _arming_candidate(**overrides) -> dict:
    candidate = {"schema_version": "ne1_arming_proposal_candidate_v1",
                 "binding": _binding_template(),
                 "operator_action_id": "op-arming-1",
                 "authorization_id": ARM_AUTH_ID,
                 "permit_id": ARM_PERMIT_ID,
                 "risk_policy_id": "risk-policy-synth-1",
                 "max_native_sends": 1, "max_native_cancels": 0}
    candidate.update(overrides)
    return candidate


def _run_prepare_arm(box: dict, service: dict, tmp_path: Path, *,
                     candidate: dict | None = None,
                     tag: str = "p1") -> tuple[int, dict]:
    candidate_path = _write(tmp_path / "arming" / f"candidate-{tag}.json",
                            _json_line(candidate or _arming_candidate()))
    return _tool(["prepare-arm",
                  "--client-store", str(box["store"]),
                  "--client-request", str(box["request_path"]),
                  "--proposal", str(candidate_path),
                  "--contract-lock", str(service["lock_path"]),
                  "--expected-client-sha256",
                  box["request"]["client_file_sha256"],
                  "--receipt", str(tmp_path / "arming" / f"prepare-{tag}.json")])


def _arming_binding(box: dict) -> dict:
    request = box["request"]
    return {"account_key": "acct-synth-1", "strategy_key": "strat-synth-1",
            "strategy_id": request["strategy_id"],
            "instance_id": request["instance_id"],
            "session_epoch": request["session_epoch"],
            "trading_day": "2026-09-17", "exchange": request["exchange"],
            "instrument_id": request["instrument_id"],
            "hedge_flag": request["hedge_flag"]}


def _decode_json_binding(data: dict):
    from ak_trading.infiny_execution import contracts as codec
    packet = {"schema_version": codec.Binding.schema, "type": "Binding",
              "data": data}
    return codec.decode(json.dumps(packet), codec.Binding)


def _build_grants(box: dict, tmp_path: Path, *, send: int = 1, cancel: int = 0,
                  control_revision: int = 2) -> dict:
    """Real v2 NativeAuthorization/NativePilotPermit records via the frozen
    codec (non-secret complete exports, AR4.2)."""
    from ak_trading.infiny_execution import contracts as codec
    from ak_trading.infiny_execution.native_contracts import (
        NativeAuthorization, NativePilotPermit, Origin)
    from decimal import Decimal
    binding = _decode_json_binding(_arming_binding(box))
    symbol = codec.Symbol(exchange=box["request"]["exchange"],
                          instrument_id=box["request"]["instrument_id"])
    bound = codec.PriceBound(symbol=symbol, lower=Decimal("3000"),
                             upper=Decimal("3200"))
    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    authorization = NativeAuthorization(
        authorization_id=ARM_AUTH_ID, binding=binding, symbols=(symbol,),
        actions=(codec.AuthorizationAction.OPEN_LONG,),
        max_send_attempts=send, max_cancel_attempts=cancel, max_volume=1,
        price_bounds=(bound,), expires_at=expires,
        origin=Origin.PRODUCTION, params_sha256=_hex("target", 64))
    permit = NativePilotPermit(
        permit_id=ARM_PERMIT_ID, authorization_id=ARM_AUTH_ID,
        binding=binding, control_revision=control_revision,
        baseline_evidence_id="ev-base-synth",
        route_evidence_id="ev-route-synth",
        cash_bound_evidence_id="ev-cash-synth", source_key="user-order-synth",
        actions=(codec.AuthorizationAction.OPEN_LONG,),
        max_send_attempts=send, max_cancel_attempts=cancel, max_volume=1,
        price_bounds=(bound,), expires_at=expires,
        params_sha256=_hex("target", 64))
    grants_dir = tmp_path / "arming" / "grants"
    auth_path = _write(grants_dir / "authorization.json",
                       (codec.encode(authorization,
                                     schema_version=NativeAuthorization.schema)
                        + "\n"))
    permit_path = _write(grants_dir / "permit.json",
                         (codec.encode(permit,
                                       schema_version=NativePilotPermit.schema)
                          + "\n"))
    return {"authorization_path": auth_path,
            "authorization_sha256": _sha_file(auth_path),
            "permit_path": permit_path,
            "permit_sha256": _sha_file(permit_path),
            "expires_at": expires}


def _registration_entry(tmp_path: Path, record_type: str, tag: str,
                        *, record_path: Path | None = None) -> dict:
    base = tmp_path / "arming" / "registrations" / tag
    base.mkdir(parents=True, exist_ok=True)
    if record_path is None:
        record_path = _write(base / "record.json",
                             _json_line({"record_type": record_type,
                                         "note": "SYNTHETIC"}))
    manifest_path = _write(base / "manifest.json",
                           _json_line({"record_type": record_type,
                                       "record_sha256": _sha_file(record_path),
                                       "note": "SYNTHETIC manifest"}))
    receipt_path = _write(base / "receipt.json",
                          _json_line({"event": "registered",
                                      "record_type": record_type,
                                      "note": "SYNTHETIC receipt"}))
    return {"record_type": record_type, "record_path": str(record_path),
            "record_sha256": _sha_file(record_path),
            "manifest_path": str(manifest_path),
            "manifest_sha256": _sha_file(manifest_path),
            "receipt_path": str(receipt_path),
            "receipt_sha256": _sha_file(receipt_path)}


def _build_registrations(box: dict, tmp_path: Path, grants: dict) -> dict:
    return {"schema_version": "ne1_arming_registrations_v1",
            "binding": _arming_binding(box),
            "target_params_sha256": _hex("target", 64),
            "contract_lock_sha256": _hex("unused-lock", 64),
            "authorization_id": ARM_AUTH_ID, "permit_id": ARM_PERMIT_ID,
            "entries": [
                _registration_entry(tmp_path, "ActorRegistration", "actor"),
                _registration_entry(tmp_path, "NativeBaselineEvidence",
                                    "evidence"),
                _registration_entry(
                    tmp_path, "NativeAuthorization", "authorization",
                    record_path=grants["authorization_path"]),
                _registration_entry(
                    tmp_path, "NativePilotPermit", "permit",
                    record_path=grants["permit_path"]),
            ]}


def _build_prerequisites(box: dict, tmp_path: Path, lock_sha256: str,
                         *, expires_hours: float = 1.0) -> dict:
    refs = []
    for kind in ("RELEASE_REVIEW", "S3_ZERO_NATIVE",
                 "MANUAL_TAKEOVER_APPROVAL", "SINGLE_ORDER_APPROVAL",
                 "NATIVE_ASSEMBLY_ACCEPTANCE"):
        path = _write(tmp_path / "arming" / "prereq" / f"{kind}.json",
                      _json_line({"kind": kind, "stage": "SYNTHETIC",
                                  "note": "sandbox evidence"}))
        refs.append({"kind": kind, "path": str(path),
                     "sha256": _sha_file(path)})
    payload = {"schema_version": "ne1_arming_prerequisites_v1",
               "prerequisites_id": _hex("prereq"),
               "created_at": _utc_iso(datetime.now(timezone.utc)),
               "expires_at": _utc_iso(datetime.now(timezone.utc)
                                      + timedelta(hours=expires_hours)),
               "contract_lock_sha256": lock_sha256,
               "client_file_sha256": box["request"]["client_file_sha256"],
               "service_file_sha256": _hex("service-file", 64),
               "tool_file_sha256": _sha_file(TOOL),
               "evidence_refs": refs}
    path = _write(tmp_path / "arming" / "prerequisites.json",
                  _json_line(payload))
    return {"payload": payload, "path": path, "sha256": _sha_file(path)}


def _build_arming_round(tmp_path: Path, *, prepare_tag: str = "r1",
                        request_active: bool = True,
                        window_seconds: int = 120,
                        cancel: int = 0) -> dict:
    """A complete SYNTHETIC arming round: real prepare-arm + client-side
    plan/request/challenge fixtures + real grant records + registrations +
    prerequisites + arming-era service-start."""
    box = _build_store(tmp_path)
    service = _build_service_side(tmp_path, box)
    lock_sha256 = service["contract_lock_sha256"]

    code, payload = _run_prepare_arm(box, service, tmp_path, tag=prepare_tag)
    assert code == 0, payload
    proposal_path = box["store"] / "arming-proposal.json"
    proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
    scope = proposal["scope"]
    binding = _arming_binding(box)

    grants = _build_grants(box, tmp_path, cancel=cancel)
    registrations = _build_registrations(box, tmp_path, grants)
    registrations["contract_lock_sha256"] = lock_sha256
    registrations_path = _write(
        tmp_path / "arming" / "registrations.json", _json_line(registrations))
    prerequisites = _build_prerequisites(box, tmp_path, lock_sha256)

    now = datetime.now(timezone.utc)
    plan = {"schema_version": "ne1_arming_plan_v1",
            # hash-style plan identifier: 64-hex (X01 fix 2 codec vector)
            "plan_id": _hex("plan", 64), "proposal_id": proposal["proposal_id"],
            "proposal_sha256": _sha_bytes(proposal_path.read_bytes()),
            "scope": scope, "binding": binding,
            "authorization_id": ARM_AUTH_ID, "permit_id": ARM_PERMIT_ID,
            "target_params_sha256": _hex("target", 64),
            "effective_overrides": {"execution_mode": "native",
                                    "risk_policy_id": "risk-policy-synth-1",
                                    "authorization_id": ARM_AUTH_ID,
                                    "max_native_sends": 1,
                                    "max_native_cancels": cancel},
            "created_at": _utc_iso(now)}
    plan_path = _write(box["store"] / "arming-plan.json", _json_line(plan))
    request = {"schema_version": "ne1_arming_request_v1",
               "request_id": _hex("areq"), "plan_id": plan["plan_id"],
               "plan_sha256": _sha_file(plan_path), "scope": scope,
               "binding": binding, "authorization_id": ARM_AUTH_ID,
               "permit_id": ARM_PERMIT_ID,
               "target_params_sha256": _hex("target", 64),
               "service_instance_id": SERVICE_INSTANCE, "service_epoch": 3,
               "created_at": _utc_iso(now),
               "expires_at": _utc_iso(now
                                      + timedelta(seconds=window_seconds)),
               "active": request_active}
    request_path = _write(box["store"] / "arming-request.json",
                          _json_line(request))
    challenge = {"schema_version": "ne1_arming_challenge_v1",
                 "request_id": request["request_id"],
                 "request_sha256": _sha_file(request_path),
                 "challenge": SYNTHETIC_CHALLENGE,
                 "issued_at": _utc_iso(now),
                 "expires_at": _utc_iso(now
                                        + timedelta(seconds=window_seconds)),
                 "writer_sid": _current_user_sid()}
    challenge_path = _write(box["store"] / "arming-challenge.json",
                            _json_line(challenge))
    _protect_current_user_only(challenge_path, directory=False)

    start = dict(service["service_start"])
    start["arming_prerequisites"] = {
        "prerequisites_id": prerequisites["payload"]["prerequisites_id"],
        "prerequisites_sha256": prerequisites["sha256"],
        "client_file_sha256":
            prerequisites["payload"]["client_file_sha256"]}
    service_start_path = _write(service["service_start_path"],
                                _json_line(start))

    return {"box": box, "service": service, "lock_sha256": lock_sha256,
            "proposal": proposal, "scope": scope, "binding": binding,
            "plan": plan, "plan_path": plan_path, "request": request,
            "request_path": request_path, "challenge": challenge,
            "challenge_path": challenge_path, "grants": grants,
            "registrations_path": registrations_path,
            "prerequisites": prerequisites,
            "service_start_path": service_start_path}


def _arm_argv(round_: dict, receipt: Path) -> list[str]:
    box, service = round_["box"], round_["service"]
    return ["arm",
            "--client-store", str(box["store"]),
            "--arming-request", str(round_["request_path"]),
            "--service-start", str(round_["service_start_path"]),
            "--authorization-record",
            str(round_["grants"]["authorization_path"]),
            "--permit-record", str(round_["grants"]["permit_path"]),
            "--registration-receipts", str(round_["registrations_path"]),
            "--s4-readiness", str(round_["prerequisites"]["path"]),
            "--contract-lock", str(service["lock_path"]),
            "--expected-client-sha256",
            box["request"]["client_file_sha256"],
            "--receipt", str(receipt)]


def _arm_args_namespace(round_: dict, receipt: Path):
    return type("Args", (), {
        "client_store": str(round_["box"]["store"]),
        "arming_request": str(round_["request_path"]),
        "service_start": str(round_["service_start_path"]),
        "authorization_record": str(round_["grants"]["authorization_path"]),
        "permit_record": str(round_["grants"]["permit_path"]),
        "registration_receipts": str(round_["registrations_path"]),
        "s4_readiness": str(round_["prerequisites"]["path"]),
        "contract_lock": str(round_["service"]["lock_path"]),
        "expected_client_sha256":
            round_["box"]["request"]["client_file_sha256"],
        "receipt": str(receipt)})()


def _arm_phrase(round_: dict) -> str:
    return "ARM " + round_["request"]["request_id"][-8:]


def _controlled_console(tool, monkeypatch, phrase) -> None:
    """SYNTHETIC controlled test console (ARM-M04): substitutes the live
    reader seam; the TTY probe is satisfied explicitly. The REAL console
    path stays the production code (subprocess tests prove the real
    rejection face)."""
    monkeypatch.setattr(tool, "_console_attached", lambda: True)
    if isinstance(phrase, str):
        monkeypatch.setattr(tool, "_console_read_line",
                            lambda prompt: phrase)


def _run_arm_subprocess(argv: list[str], *, feed: str | None = None):
    if feed is None:
        result = subprocess.run(
            [sys.executable, "-B", "-X", "utf8", str(TOOL), *argv],
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
            cwd=str(ROOT))
    else:
        result = subprocess.run(
            [sys.executable, "-B", "-X", "utf8", str(TOOL), *argv],
            capture_output=True, text=True, input=feed, cwd=str(ROOT))
    text = result.stdout.strip()
    payload = json.loads(text.splitlines()[-1]) if text else {}
    return result.returncode, payload


# ---------------------------------------------------------------------------
# ARM-M02: prepare-arm face
# ---------------------------------------------------------------------------

def test_prepare_arm_publishes_non_secret_proposal_into_store(tmp_path: Path):
    box = _build_store(tmp_path)
    service = _build_service_side(tmp_path, box)
    code, payload = _run_prepare_arm(box, service, tmp_path)
    assert code == 0, payload
    assert payload["status"] == "PREPARATION_SUBMITTED"
    proposal_path = box["store"] / "arming-proposal.json"
    saved = json.loads(proposal_path.read_text(encoding="utf-8"))
    assert saved["schema_version"] == "ne1_arming_proposal_v1"
    assert saved["proposal_id"] == payload["proposal_id"]
    assert saved["execution_mode"] == "native"
    assert saved["max_native_sends"] == 1
    assert saved["max_native_cancels"] == 0
    # scope is derived from the CURRENT request + measured lock (AR4.2)
    assert saved["scope"]["pairing_request_id"] == box["request"]["request_id"]
    assert saved["scope"]["pairing_request_sha256"] == box["request_sha256"]
    assert saved["scope"]["contract_lock_sha256"] \
        == service["contract_lock_sha256"]
    assert saved["scope"]["execution_origin"] == "http://127.0.0.1:17779"
    assert saved["scope"]["pairing_params_sha256"] \
        == box["request"]["params_sha256"]
    receipt = json.loads((tmp_path / "arming" / "prepare-p1.json")
                         .read_text(encoding="utf-8"))
    assert receipt["schema_version"] == "ne1_arming_tool_receipt_v1"
    assert receipt["status"] == "PREPARATION_SUBMITTED"
    assert receipt["target_params_sha256"] is None  # client-computed only
    assert receipt["action_source"] == "USER_LOCAL_CONSOLE"
    assert set(receipt) == ({"schema_version"} | {
        "operation", "status", "action_source", "scope", "request_id",
        "arm_id", "disarm_id", "plan_id", "authorization_id", "permit_id",
        "target_params_sha256", "client_file_sha256", "contract_lock_sha256",
        "service_instance_id", "service_epoch", "control_revision",
        "prerequisites_id", "prerequisites_sha256", "user_sid",
        "local_gate_closed", "service_gate_closed", "recorded_at",
        "reason_code"})


def test_prepare_arm_rejects_conflict_inactive_and_drift(tmp_path: Path):
    box = _build_store(tmp_path)
    service = _build_service_side(tmp_path, box)
    assert _run_prepare_arm(box, service, tmp_path, tag="a")[0] == 0
    # ARM-M02: an existing candidate is a proposal conflict (never overwritten)
    code, payload = _run_prepare_arm(box, service, tmp_path, tag="b")
    assert code == 2 and payload["code"] == "ARM_PROPOSAL_ALREADY_PRESENT", \
        payload
    before = (box["store"] / "arming-proposal.json").read_bytes()
    assert (box["store"] / "arming-proposal.json").read_bytes() == before

    inactive = _build_store(tmp_path / "inactive", active=False)
    service_i = _build_service_side(tmp_path / "inactive", inactive)
    code, payload = _run_prepare_arm(inactive, service_i,
                                     tmp_path / "inactive", tag="c")
    assert code == 2 and payload["code"] == "PAIRING_REQUEST_INACTIVE", payload
    box2 = _build_store(tmp_path / "ok", request_seed="req-ok")
    service2 = _build_service_side(tmp_path / "ok", box2)
    code, payload = _run_prepare_arm(box2, service2, tmp_path / "ok", tag="d",
                                     candidate=_arming_candidate(
                                         max_native_sends=2))
    assert code == 2 and payload["code"] == "ARM_CANDIDATE_INVALID", payload
    candidate_path = _write(tmp_path / "ok" / "cand.json",
                            _json_line(_arming_candidate()))
    code, payload = _tool(["prepare-arm",
                           "--client-store", str(box2["store"]),
                           "--client-request", str(box2["request_path"]),
                           "--proposal", str(candidate_path),
                           "--contract-lock", str(service2["lock_path"]),
                           "--expected-client-sha256", _hex("drift", 64),
                           "--receipt", str(tmp_path / "ok" / "r.json")])
    assert code == 2 and payload["code"] == "PAIRING_CONTEXT_MISMATCH", payload
    code, payload = _run_prepare_arm(
        box2, service2, tmp_path / "ok", tag="e",
        candidate=_arming_candidate(
            binding=_binding_template(instrument_id="cu2601")))
    assert code == 2 \
        and payload["code"] == "ARM_CANDIDATE_REQUEST_MISMATCH", payload


# ---------------------------------------------------------------------------
# ARM-M04: the human entrance (non-interactive rejection + controlled console)
# ---------------------------------------------------------------------------

def test_arm_rejects_every_non_interactive_stdin(tmp_path: Path):
    """REAL subprocess proof: piped phrase (even the CORRECT one), redirected
    stdin and no console all reject BEFORE any file read or write."""
    round_ = _build_arming_round(tmp_path)
    receipt = tmp_path / "arming" / "arm-r.json"
    argv = _arm_argv(round_, receipt)
    code, payload = _run_arm_subprocess(argv, feed=_arm_phrase(round_) + "\n")
    assert code == 2 and payload["code"] == "ARM_INTERACTIVE_REQUIRED", payload
    code, payload = _run_arm_subprocess(argv)
    assert code == 2 and payload["code"] == "ARM_INTERACTIVE_REQUIRED", payload
    assert not (round_["box"]["store"] / "arming.json").exists()
    assert not receipt.exists()


def test_arm_delivers_one_shot_credential_under_controlled_console(
        tmp_path, monkeypatch):
    tool = _load_tool()
    round_ = _build_arming_round(tmp_path)
    receipt = tmp_path / "arming" / "arm-r.json"
    _controlled_console(tool, monkeypatch, _arm_phrase(round_))
    result = tool.cmd_arm(_arm_args_namespace(round_, receipt))
    assert result["status"] == "ARM_SUBMITTED_AWAITING_CLIENT"
    assert result["arm_id"] and len(result["arm_id"]) == 32
    slot = round_["box"]["store"] / "arming.json"
    saved = json.loads(slot.read_text(encoding="utf-8"))
    assert saved["schema_version"] == "ne1_client_arming_v1"
    assert saved["arm_id"] == result["arm_id"]
    assert saved["request_id"] == round_["request"]["request_id"]
    assert saved["request_sha256"] == _sha_file(round_["request_path"])
    assert saved["plan_id"] == round_["plan"]["plan_id"]
    assert saved["target_params_sha256"] == _hex("target", 64)
    assert saved["authorization_id"] == ARM_AUTH_ID
    assert saved["authorization_record_sha256"] \
        == round_["grants"]["authorization_sha256"]
    assert saved["permit_record_sha256"] == round_["grants"]["permit_sha256"]
    assert saved["service_instance_id"] == SERVICE_INSTANCE
    assert saved["service_epoch"] == 3
    assert saved["expected_control_revision"] == 2
    assert saved["prerequisites_id"] \
        == round_["prerequisites"]["payload"]["prerequisites_id"]
    assert saved["challenge"] == SYNTHETIC_CHALLENGE
    assert saved["user_sid"] == _current_user_sid()
    assert saved["action_source"] == "USER_LOCAL_CONSOLE"
    assert saved["explicit_user_action"] is True
    assert saved["confirmed_at"] and saved["expires_at"] > saved["confirmed_at"]
    assert saved["expires_at"] <= round_["request"]["expires_at"]
    record = json.loads(receipt.read_text(encoding="utf-8"))
    assert record["status"] == "ARM_SUBMITTED_AWAITING_CLIENT"
    assert record["arm_id"] == result["arm_id"]
    assert record["local_gate_closed"] is None
    assert record["service_gate_closed"] is None
    assert record["control_revision"] == 2
    assert record["reason_code"] is None
    # secret hygiene: the challenge value exists ONLY in the private source
    # slot and the delivered credential
    allowed = {str(round_["challenge_path"]), str(slot)}
    for path in tmp_path.rglob("*"):
        if path.is_file() and str(path) not in allowed:
            assert SYNTHETIC_CHALLENGE.encode("ascii") \
                not in path.read_bytes(), path
    assert SYNTHETIC_CHALLENGE not in json.dumps(result)


def test_arm_phrase_empty_wrong_and_expiry_rejections(tmp_path, monkeypatch):
    tool = _load_tool()
    # empty confirmation rejects immediately
    round_ = _build_arming_round(tmp_path / "empty")
    _controlled_console(tool, monkeypatch, "")
    with pytest.raises(tool.PairingError) as raised:
        tool.cmd_arm(_arm_args_namespace(round_, tmp_path / "empty" / "r.json"))
    assert str(raised.value) == "ARM_CONFIRMATION_EMPTY"

    # wrong phrase: three live attempts, then refusal; nothing delivered
    calls: list = []
    round_ = _build_arming_round(tmp_path / "wrong")
    monkeypatch.setattr(tool, "_console_attached", lambda: True)
    monkeypatch.setattr(tool, "_console_read_line",
                        lambda prompt: (calls.append(prompt), "ARM deadbeef")[1])
    with pytest.raises(tool.PairingError) as raised:
        tool.cmd_arm(_arm_args_namespace(round_, tmp_path / "wrong" / "r.json"))
    assert str(raised.value) == "ARM_PHRASE_MISMATCH"
    assert len(calls) == 3, "each attempt is a fresh typed line"
    assert not (round_["box"]["store"] / "arming.json").exists()

    # expiry between display and Enter: the clock seam advances past the
    # request window at prompt time; the post-phrase liveness check refuses
    real_datetime = tool.datetime

    class _FutureDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime.now(tz) + timedelta(seconds=600)
    round_ = _build_arming_round(tmp_path / "expire")

    def expire_then_answer(prompt: str) -> str:
        monkeypatch.setattr(tool, "datetime", _FutureDatetime)
        return _arm_phrase(round_)
    monkeypatch.setattr(tool, "_console_attached", lambda: True)
    monkeypatch.setattr(tool, "_console_read_line", expire_then_answer)
    with pytest.raises(tool.PairingError) as raised:
        tool.cmd_arm(_arm_args_namespace(round_, tmp_path / "expire" / "r.json"))
    assert str(raised.value) == "ARM_REQUEST_EXPIRED"
    assert not (round_["box"]["store"] / "arming.json").exists()


def test_arm_request_replaced_after_display_is_refused(tmp_path, monkeypatch):
    tool = _load_tool()
    round_ = _build_arming_round(tmp_path)

    def replace_then_answer(prompt: str) -> str:
        swapped = dict(round_["request"], service_epoch=9)
        _write(round_["request_path"], _json_line(swapped))
        return _arm_phrase(round_)
    monkeypatch.setattr(tool, "_console_attached", lambda: True)
    monkeypatch.setattr(tool, "_console_read_line", replace_then_answer)
    with pytest.raises(tool.PairingError) as raised:
        tool.cmd_arm(_arm_args_namespace(round_, tmp_path / "r.json"))
    assert str(raised.value) == "ARM_REQUEST_CHANGED"
    assert not (round_["box"]["store"] / "arming.json").exists()


# ---------------------------------------------------------------------------
# ARM-M05: ACL ordering on the private challenge (real DACL)
# ---------------------------------------------------------------------------

def test_arm_dacl_proven_before_challenge_read_and_write(tmp_path, monkeypatch):
    tool = _load_tool()
    round_ = _build_arming_round(tmp_path)
    events: list[tuple[str, str]] = []
    real_verify = tool.verify_private_access
    real_read = Path.read_bytes
    real_publish = tool._atomic_publish

    def traced_verify(path, *, need_write):
        events.append(("acl", str(path)))
        return real_verify(path, need_write=need_write)

    def traced_read(self):
        events.append(("read", str(self)))
        return real_read(self)

    def traced_publish(path, payload):
        events.append(("publish", str(path)))
        return real_publish(path, payload)

    monkeypatch.setattr(tool, "verify_private_access", traced_verify)
    monkeypatch.setattr(Path, "read_bytes", traced_read)
    monkeypatch.setattr(tool, "_atomic_publish", traced_publish)
    _controlled_console(tool, monkeypatch, _arm_phrase(round_))
    assert tool.cmd_arm(_arm_args_namespace(
        round_, tmp_path / "r.json"))["status"] == "ARM_SUBMITTED_AWAITING_CLIENT"
    challenge = str(round_["challenge_path"])
    first_acl = next(index for index, event in enumerate(events)
                     if event[0] == "acl")
    challenge_acl = next(index for index, event in enumerate(events)
                         if event[0] == "acl" and event[1] == challenge)
    challenge_read = next(index for index, event in enumerate(events)
                          if event[0] == "read" and event[1] == challenge)
    publish = next(index for index, event in enumerate(events)
                   if event[0] == "publish")
    assert first_acl < challenge_acl < challenge_read < publish
    post_publish_acl = [index for index, event in enumerate(events)
                        if event[0] == "acl" and index > publish]
    assert post_publish_acl, "published credential DACL must be re-verified"


def test_arm_rejects_loose_store_and_challenge_dacls(tmp_path, monkeypatch):
    tool = _load_tool()
    real_read = Path.read_bytes
    # loose STORE DACL: rejected before the challenge is ever read
    round_ = _build_arming_round(tmp_path / "store")
    _grant_everyone(round_["box"]["store"], directory=True)
    reads: list[str] = []

    def traced_read(self):
        reads.append(str(self))
        return real_read(self)
    monkeypatch.setattr(Path, "read_bytes", traced_read)
    _controlled_console(tool, monkeypatch, _arm_phrase(round_))
    with pytest.raises(tool.PairingError) as raised:
        tool.cmd_arm(_arm_args_namespace(round_, tmp_path / "store" / "r.json"))
    assert str(raised.value) == "PAIRING_ACL_RESTRICTED"
    assert str(round_["challenge_path"]) not in reads
    assert not (round_["box"]["store"] / "arming.json").exists()

    # loose PRIVATE challenge slot DACL: rejected before its bytes are read
    monkeypatch.setattr(Path, "read_bytes", real_read)
    round_ = _build_arming_round(tmp_path / "challenge")
    _grant_everyone(round_["challenge_path"], directory=False)
    reads2: list[str] = []

    def traced_read2(self):
        reads2.append(str(self))
        return real_read(self)
    monkeypatch.setattr(Path, "read_bytes", traced_read2)
    _controlled_console(tool, monkeypatch, _arm_phrase(round_))
    with pytest.raises(tool.PairingError) as raised:
        tool.cmd_arm(_arm_args_namespace(
            round_, tmp_path / "challenge" / "r.json"))
    assert str(raised.value) == "PAIRING_ACL_RESTRICTED"
    assert str(round_["challenge_path"]) not in reads2
    assert not (round_["box"]["store"] / "arming.json").exists()


# ---------------------------------------------------------------------------
# ARM-M06: strict frozen formats, binding and identity mismatches
# ---------------------------------------------------------------------------

def _expect_arm_reject(tool, round_, tmp_path, monkeypatch, code: str,
                       *, tag: str = "m") -> None:
    _controlled_console(tool, monkeypatch, _arm_phrase(round_))
    with pytest.raises(tool.PairingError) as raised:
        tool.cmd_arm(_arm_args_namespace(round_, tmp_path / f"{tag}.json"))
    assert str(raised.value) == code, raised.value
    assert not (round_["box"]["store"] / "arming.json").exists()


@pytest.mark.parametrize("change,code", [
    (lambda r: dict(r, schema_version="ne1_arming_request_v2"),
     "ARM_REQUEST_INVALID"),
    (lambda r: dict(r, extra=1), "ARM_REQUEST_INVALID"),
    (lambda r: dict(r, service_epoch=True), "ARM_REQUEST_INVALID"),
    (lambda r: dict(r, active=False), "ARM_REQUEST_INACTIVE"),
], ids=["schema", "extra-field", "bool-as-int", "inactive"])
def test_arm_rejects_request_format_violations(tmp_path, monkeypatch,
                                               change, code):
    tool = _load_tool()
    round_ = _build_arming_round(tmp_path)
    _write(round_["request_path"],
           _json_line(change(round_["request"])))
    _expect_arm_reject(tool, round_, tmp_path, monkeypatch, code)


def test_arm_rejects_duplicate_keys_and_expired_request(tmp_path, monkeypatch):
    tool = _load_tool()
    round_ = _build_arming_round(tmp_path)
    raw = round_["request_path"].read_text(encoding="utf-8").strip()
    duplicated = raw[:-1] + ',"request_id":"' + _hex("dup") + '"}\n'
    _write(round_["request_path"], duplicated)
    _expect_arm_reject(tool, round_, tmp_path, monkeypatch, "DUPLICATE_JSON_KEY")

    round_ = _build_arming_round(tmp_path / "expired", window_seconds=30)
    past = datetime.now(timezone.utc) - timedelta(seconds=200)
    expired = dict(round_["request"], created_at=_utc_iso(past),
                   expires_at=_utc_iso(past + timedelta(seconds=30)))
    _write(round_["request_path"], _json_line(expired))
    _expect_arm_reject(tool, round_, tmp_path / "expired", monkeypatch,
                       "ARM_REQUEST_EXPIRED", tag="e1")


def test_arm_rejects_scope_plan_grant_and_sid_mismatches(tmp_path,
                                                         monkeypatch):
    tool = _load_tool()
    # scope drift vs the CURRENT pairing request bytes
    round_ = _build_arming_round(tmp_path / "s")
    swapped_scope = dict(round_["scope"],
                         pairing_request_sha256=_hex("swapped", 64))
    _write(round_["request_path"], _json_line(
        dict(round_["request"], scope=swapped_scope)))
    _expect_arm_reject(tool, round_, tmp_path / "s", monkeypatch,
                       "ARM_SCOPE_MISMATCH")

    # plan bytes no longer match the request's plan hash
    # (deterministic mutation: a FIXED timestamp constant — no fractional
    #  part — can never byte-collide with the builder's fresh microsecond
    #  timestamp; on this Windows/Python build datetime.now() is coarsely
    #  quantized (measured: 998/1000 successive deltas <= 0 us), and a fresh
    #  now() rewrite collided ~1% of runs, leaving the "tampered" bytes
    #  identical to the original and the probe false-failing with
    #  DID NOT RAISE — the 1/5 full-suite intermittent, now root-caused)
    round_ = _build_arming_round(tmp_path / "p")
    _write(round_["plan_path"], _json_line(
        dict(round_["plan"],
             created_at="2026-09-17T00:00:00Z")))
    _expect_arm_reject(tool, round_, tmp_path / "p", monkeypatch,
                       "ARM_PLAN_HASH_MISMATCH")

    # tampered authorization record: the registrations index catches the
    # reference-hash drift first (the file no longer matches its entry)
    round_ = _build_arming_round(tmp_path / "g")
    tampered = json.loads(round_["grants"]["authorization_path"]
                          .read_text(encoding="utf-8"))
    tampered["data"]["authorization_id"] = "auth-tampered-1"
    _write(round_["grants"]["authorization_path"],
           json.dumps(tampered, sort_keys=True) + "\n")
    _expect_arm_reject(tool, round_, tmp_path / "g", monkeypatch,
                       "ARM_REGISTRATIONS_REF_HASH_MISMATCH")

    # a registrations index naming a DIFFERENT record than the input export:
    # the input grant was never part of this round's registration
    round_ = _build_arming_round(tmp_path / "u")
    registrations = json.loads(round_["registrations_path"]
                               .read_text(encoding="utf-8"))
    entry = next(item for item in registrations["entries"]
                 if item["record_type"] == "NativeAuthorization")
    entry["record_path"] = str(round_["grants"]["permit_path"])
    entry["record_sha256"] = round_["grants"]["permit_sha256"]
    _write(round_["registrations_path"], _json_line(registrations))
    _expect_arm_reject(tool, round_, tmp_path / "u", monkeypatch,
                       "ARM_AUTHORIZATION_RECORD_UNREGISTERED")

    # wrong challenge writer SID (AR4.2: SID from the real process token)
    round_ = _build_arming_round(tmp_path / "w")
    _write(round_["challenge_path"], _json_line(
        dict(round_["challenge"], writer_sid="S-1-5-21-0-0-0-9999")))
    _protect_current_user_only(round_["challenge_path"], directory=False)
    _expect_arm_reject(tool, round_, tmp_path / "w", monkeypatch,
                       "ARM_CHALLENGE_WRITER_SID_MISMATCH")

    # grant budgets: cancel must be explicitly 0 or 1
    round_ = _build_arming_round(tmp_path / "b", cancel=2)
    _expect_arm_reject(tool, round_, tmp_path / "b", monkeypatch,
                       "ARM_GRANT_CANCEL_BUDGET_INVALID")


def test_arm_rejects_prerequisites_and_service_start_mismatches(
        tmp_path, monkeypatch):
    tool = _load_tool()
    # service start without the loaded arming prerequisites (AR3.3)
    round_ = _build_arming_round(tmp_path / "n")
    start = dict(round_["service"]["service_start"])
    start.pop("arming_prerequisites", None)
    _write(round_["service_start_path"], _json_line(start))
    _expect_arm_reject(tool, round_, tmp_path / "n", monkeypatch,
                       "ARM_PREREQUISITES_NOT_LOADED")

    # prerequisites with a missing S4 gate kind
    round_ = _build_arming_round(tmp_path / "k")
    payload = dict(round_["prerequisites"]["payload"])
    payload["evidence_refs"] = [ref for ref in payload["evidence_refs"]
                                if ref["kind"] != "SINGLE_ORDER_APPROVAL"]
    _write(round_["prerequisites"]["path"], _json_line(payload))
    _expect_arm_reject(tool, round_, tmp_path / "k", monkeypatch,
                       "ARM_PREREQUISITES_INCOMPLETE")

    # a drifted tool hash cannot self-approve its own prerequisites
    round_ = _build_arming_round(tmp_path / "t")
    payload = dict(round_["prerequisites"]["payload"],
                   tool_file_sha256=_hex("other-tool", 64))
    _write(round_["prerequisites"]["path"], _json_line(payload))
    _expect_arm_reject(tool, round_, tmp_path / "t", monkeypatch,
                       "ARM_PREREQUISITES_TOOL_MISMATCH")

    # service start naming a DIFFERENT prerequisites file (id mismatch)
    round_ = _build_arming_round(tmp_path / "i")
    current_start = json.loads(
        round_["service_start_path"].read_text(encoding="utf-8"))
    loaded = dict(current_start["arming_prerequisites"],
                  prerequisites_id=_hex("other-prereq"))
    _write(round_["service_start_path"], _json_line(
        dict(current_start, arming_prerequisites=loaded)))
    _expect_arm_reject(tool, round_, tmp_path / "i", monkeypatch,
                       "ARM_PREREQUISITES_SERVICE_MISMATCH")


# ---------------------------------------------------------------------------
# ARM-M09 (tool side): one-shot delivery guards
# ---------------------------------------------------------------------------

def _consumed_state(round_: dict, *, consumed: bool, revoked: bool,
                    state_name: str, reason: str) -> dict:
    return {"schema_version": "ne1_arming_state_v1",
            "scope": round_["scope"], "plan_id": round_["plan"]["plan_id"],
            "arm_request_id": round_["request"]["request_id"],
            "arm_id": _hex("used-arm"), "state": state_name,
            "consumed": consumed, "revoked": revoked,
            "effective_enable_trading": consumed,
            "target_params_sha256": _hex("target", 64),
            "service_instance_id": SERVICE_INSTANCE, "service_epoch": 3,
            "control_revision": 2, "expires_at": None,
            "local_gate_closed": revoked, "service_gate_closed": False,
            "unresolved_dispatch_count": 0, "count_complete": True,
            "updated_at": _utc_iso(datetime.now(timezone.utc)),
            "reason_code": reason}


def test_arm_is_one_shot_per_round(tmp_path, monkeypatch):
    tool = _load_tool()
    # a credential already delivered and not yet consumed: never a second one
    round_ = _build_arming_round(tmp_path)
    existing = _write(round_["box"]["store"] / "arming.json",
                      _json_line({"schema_version": "ne1_client_arming_v1",
                                  "arm_id": _hex("prior-arm"),
                                  "note": "SYNTHETIC unconsumed"}))
    before = existing.read_bytes()
    _controlled_console(tool, monkeypatch, _arm_phrase(round_))
    with pytest.raises(tool.PairingError) as raised:
        tool.cmd_arm(_arm_args_namespace(round_, tmp_path / "r.json"))
    assert str(raised.value) == "ARM_CREDENTIAL_ALREADY_PRESENT"
    assert existing.read_bytes() == before

    # consumed round: tombstone state refuses re-arm
    round_ = _build_arming_round(tmp_path / "consumed")
    state = _consumed_state(round_, consumed=True, revoked=False,
                            state_name="ARMED", reason="CONSUMED")
    _write(round_["box"]["store"] / "arming-state.json", _json_line(state))
    _controlled_console(tool, monkeypatch, _arm_phrase(round_))
    with pytest.raises(tool.PairingError) as raised:
        tool.cmd_arm(_arm_args_namespace(round_, tmp_path / "c.json"))
    assert str(raised.value) == "ARM_ALREADY_CONSUMED"

    # revoked round
    round_ = _build_arming_round(tmp_path / "revoked")
    revoked = _consumed_state(round_, consumed=False, revoked=True,
                              state_name="REVOKE_PENDING",
                              reason="USER_DISARM")
    _write(round_["box"]["store"] / "arming-state.json", _json_line(revoked))
    _controlled_console(tool, monkeypatch, _arm_phrase(round_))
    with pytest.raises(tool.PairingError) as raised:
        tool.cmd_arm(_arm_args_namespace(round_, tmp_path / "v.json"))
    assert str(raised.value) == "ARM_REVOKED"


# ---------------------------------------------------------------------------
# ARM-M10: disarm (offline, scoped, idempotent, stale-explicit)
# ---------------------------------------------------------------------------

def test_disarm_publishes_scoped_revocation_and_is_idempotent(tmp_path: Path):
    round_ = _build_arming_round(tmp_path)
    store = round_["box"]["store"]
    receipt = tmp_path / "arming" / "disarm-1.json"
    argv = ["disarm", "--client-store", str(store),
            "--client-request", str(round_["box"]["request_path"]),
            "--receipt", str(receipt)]
    code, payload = _tool(argv)
    assert code == 0, payload
    assert payload["status"] == "DISARM_REQUESTED"
    slot = store / "disarm.json"
    saved = json.loads(slot.read_text(encoding="utf-8"))
    assert saved["schema_version"] == "ne1_client_disarm_v1"
    assert saved["reason"] == "USER_DISARM"
    assert saved["user_sid"] == _current_user_sid()
    assert saved["scope"] == round_["scope"]
    assert saved["arm_request_id"] == round_["request"]["request_id"]
    assert saved["arm_id"] is None            # no client state published yet
    record = json.loads(receipt.read_text(encoding="utf-8"))
    assert record["status"] == "DISARM_REQUESTED"
    assert record["disarm_id"] == saved["disarm_id"]
    assert record["local_gate_closed"] is None   # PENDING, never assumed
    assert record["service_gate_closed"] is None
    assert record["reason_code"] is None
    # idempotent repeat for the same scope succeeds with a fresh receipt
    argv[argv.index("--receipt") + 1] = str(tmp_path / "arming" / "disarm-2.json")
    code, payload = _tool(argv)
    assert code == 0 and payload["status"] == "DISARM_REQUESTED", payload
    # the private pairing credential was never touched by disarm
    assert not (store / "pairing.json").exists()


def test_disarm_reports_stale_scope_and_absent_round(tmp_path: Path):
    round_ = _build_arming_round(tmp_path)
    store = round_["box"]["store"]
    # the client moved to a NEW session: the old round's disarm is explicit
    restarted = dict(round_["box"]["request"],
                     request_id=_hex("new-request"),
                     session_epoch=_hex("new-session"),
                     generation=_hex("new-generation"))
    new_request = _write(round_["box"]["request_path"],
                         _json_line(restarted))
    code, payload = _tool(["disarm", "--client-store", str(store),
                           "--client-request", str(new_request),
                           "--receipt", str(tmp_path / "arming" / "d.json")])
    assert code == 0, payload
    assert payload["status"] == "DISARM_REQUESTED"
    assert payload["reason_code"] == "STALE_SCOPE"
    record = json.loads((tmp_path / "arming" / "d.json")
                        .read_text(encoding="utf-8"))
    assert record["reason_code"] == "STALE_SCOPE"
    assert record["scope"] == round_["scope"]   # the OLD round, never claimed
    # a store with no arming material at all has nothing to revoke
    bare = _build_store(tmp_path / "bare")
    code, payload = _tool(["disarm", "--client-store", str(bare["store"]),
                           "--client-request", str(bare["request_path"]),
                           "--receipt", str(tmp_path / "bare-d.json")])
    assert code == 2 and payload["code"] == "ARM_ROUND_ABSENT", payload


# ---------------------------------------------------------------------------
# inspect: non-secret arming status, private slots existence-only
# ---------------------------------------------------------------------------

def test_inspect_reports_arming_state_without_opening_private_slots(
        tmp_path: Path):
    round_ = _build_arming_round(tmp_path)
    store = round_["box"]["store"]
    code, payload = _tool(["inspect", "--client-store", str(store)])
    assert code == 0, payload
    arming = payload["arming"]
    assert arming["proposal_present"] is True
    assert arming["proposal"]["authorization_id"] == ARM_AUTH_ID
    assert arming["plan_present"] is True
    assert arming["request_present"] is True
    assert arming["request"]["request_id"] == round_["request"]["request_id"]
    assert arming["state_present"] is False
    assert arming["private_slots"]["arming_challenge"] is True
    assert arming["private_slots"]["arming_credential"] is False
    assert arming["disarm_present"] is False
    assert arming["pairing_request_changed"] is False
    assert arming["expired"] is False
    before = _snapshot(store)
    # garbage in a PRIVATE slot is reported by existence only, never parsed
    garbage = b"garbage-not-json"
    _write(store / "arming.json", garbage)
    code, payload = _tool(["inspect", "--client-store", str(store)])
    assert code == 0
    assert payload["arming"]["private_slots"]["arming_credential"] is True
    after = _snapshot(store)
    assert set(after) == set(before) | {"arming.json"}
    assert after["arming.json"][0] == len(garbage)
    for name, snapshot in before.items():
        assert after[name] == snapshot


def test_inspect_detects_request_change_and_expiry(tmp_path: Path):
    round_ = _build_arming_round(tmp_path)
    store = round_["box"]["store"]
    moved = dict(round_["box"]["request"], request_id=_hex("moved"),
                 session_epoch=_hex("moved-session"))
    _write(round_["box"]["request_path"], _json_line(moved))
    past = datetime.now(timezone.utc) - timedelta(seconds=300)
    expired_request = dict(round_["request"],
                           created_at=_utc_iso(past),
                           expires_at=_utc_iso(past + timedelta(seconds=60)))
    _write(round_["request_path"], _json_line(expired_request))
    code, payload = _tool(["inspect", "--client-store", str(store)])
    assert code == 0, payload
    assert payload["arming"]["pairing_request_changed"] is True
    assert payload["arming"]["expired"] is True
    # recorded tombstones never become a live authorization claim
    assert "never a live authorization" in payload["arming"]["note"]
