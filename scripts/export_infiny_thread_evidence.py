"""TE-LM-D: export one durable client thread original, without registration.

The active request is checked before/after publication; this is a snapshot, not
ownership of the client's lifecycle. No adapter import, identity/pairing/arming
read, credential, HTTP, checkpoint, or broker operation belongs in this tool.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import sqlite3
import stat
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ak_trading.infiny_execution import contracts as c
from ak_trading.infiny_execution.native_evidence import (
    ClientThreadEvidence, OperatorArtifact, OperatorRecordType,
    OperatorRegistrationManifest,
)
# Pure validators/Windows ACL checks only. Never call its store/secret loaders.
from scripts.pair_infiny_execution import (
    execution_origin, validate_request, verify_private_access,
)

TOOL_VERSION = "ne1-thread-evidence-export-20260918-v1"
RECEIPT_SCHEMA = "ne1_thread_evidence_export_v1"
KEY_PREFIX = "client_thread_evidence_v1:"
RECORD_LIMIT = 8192
RECEIPT_LIMIT = 16 * 1024
JSON_LIMIT = 32 * 1024
RECEIPT_FIELDS = frozenset({
    "schema_version", "exported_at", "evidence_id", "evidence_sha256",
    "source_table", "source_key", "client_store_directory", "pairing_request_id",
    "pairing_request_sha256", "client_file_sha256", "contract_lock_sha256",
    "instance_id", "session_epoch", "generation", "process_instance_id",
    "registration_id", "operator_action_id", "origin",
})
OUTPUT_NAMES = ("thread-evidence.json", "pairing-request.json",
                "source-receipt.json", "register-thread-manifest.json")
SCOPE_FIELDS = ("instance_id", "session_epoch", "generation", "process_instance_id")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_PRIVATE_NAMES = {"identity.json", "pairing.json", "arming.json", "arming-challenge.json",
                  "credentials.json", "credential.json", "secrets.json", "secret.json"}


class ThreadEvidenceExportError(ValueError):
    """Only static, non-secret rejection codes are exposed to the caller."""


def _require(ok, code):
    if not ok:
        raise ThreadEvidenceExportError(code)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _normalized(path):
    return os.path.normcase(os.path.normpath(str(path)))


def _path(value):
    raw = os.fspath(value)
    _require(type(raw) is str and raw and not any(ord(ch) < 32 for ch in raw),
             "LOCAL_PATH_REQUIRED")
    win = PureWindowsPath(raw)
    _require(not win.drive.startswith("\\\\") and not raw.startswith(("//", "\\\\"))
             and "${" not in raw and "%" not in raw and "://" not in raw,
             "LOCAL_PATH_REQUIRED")
    _require(Path(raw).is_absolute() and ".." not in win.parts, "LOCAL_PATH_REQUIRED")
    for part in win.parts:
        if part == win.anchor:
            continue
        _require(part.rstrip(" .") == part and not PureWindowsPath(part).is_reserved()
                 and not any(ch in '<>:"|?*' or ord(ch) == 127 for ch in part),
                 "LOCAL_PATH_REQUIRED")
        name = part.casefold()
        _require(name not in _PRIVATE_NAMES and not name.startswith(".env")
                 and not name.endswith((".env", ".token", ".key", ".pem"))
                 and not re.search(r"(^|[._-])(token|password|credentials?|secrets?)([._-]|$)", name),
                 "SECRET_PATH_REFUSED")
    return Path(raw)


def _stat(path, *, directory):
    info = path.lstat()
    _require(not stat.S_ISLNK(info.st_mode)
             and not (getattr(info, "st_file_attributes", 0) & 0x400),
             "REPARSE_PATH_REFUSED")
    _require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode),
             "PATH_TYPE_INVALID")
    if not directory:
        _require(info.st_nlink == 1, "HARDLINK_PATH_REFUSED")
    return info


def _identity(info):
    return info.st_dev, info.st_ino


def _open_handle(path, *, directory=False, mutable=False):
    """Pin the actual Windows object with no delete sharing, never follow reparse.

    Directory handles pin ancestors; DB/WAL/SHM allow the client to write. Input
    file handles exclude writers only during the bounded read, not the export.
    """
    _require(sys.platform == "win32", "PLATFORM_PATH_GUARD_UNSUPPORTED")
    from ctypes import wintypes as wt

    class FileInformation(ctypes.Structure):
        _fields_ = [("attributes", wt.DWORD), ("created", wt.FILETIME),
                    ("accessed", wt.FILETIME), ("written", wt.FILETIME),
                    ("volume", wt.DWORD), ("size_high", wt.DWORD),
                    ("size_low", wt.DWORD), ("links", wt.DWORD),
                    ("index_high", wt.DWORD), ("index_low", wt.DWORD)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.LPVOID,
                                  wt.DWORD, wt.DWORD, wt.HANDLE)
    kernel.CreateFileW.restype = wt.HANDLE
    kernel.GetFileInformationByHandle.argtypes = (wt.HANDLE, ctypes.POINTER(FileInformation))
    kernel.GetFileInformationByHandle.restype = wt.BOOL
    kernel.CloseHandle.argtypes = (wt.HANDLE,)
    kernel.CloseHandle.restype = wt.BOOL
    flags = 0x00200000 | (0x02000000 if directory else 0)  # OPEN_REPARSE / BACKUP
    # Attribute-only handles do not participate in Windows delete sharing.
    # GENERIC_READ is required even for directories to forbid their rename.
    handle = kernel.CreateFileW(str(path), 0x80000000,
                                1 | (2 if directory or mutable else 0), None,
                                3, flags, None)  # OPEN_EXISTING, no SHARE_DELETE
    if handle == ctypes.c_void_p(-1).value:
        raise ThreadEvidenceExportError("PATH_ACCESS_REFUSED")
    try:
        info = FileInformation()
        _require(kernel.GetFileInformationByHandle(handle, ctypes.byref(info)),
                 "PATH_ACCESS_REFUSED")
        _require(not info.attributes & 0x400, "REPARSE_PATH_REFUSED")
        _require(bool(info.attributes & 0x10) == directory, "PATH_TYPE_INVALID")
        _require(directory or info.links == 1, "HARDLINK_PATH_REFUSED")
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    return handle, kernel


class _Paths:
    """No resolved symlinks, long-lived client lock, or secret file inspection."""

    def __init__(self):
        self.stack = ExitStack()
        self.directories = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return self.stack.__exit__(*args)

    def directory(self, path):
        path = _path(path)
        for ancestor in reversed((path, *path.parents)):
            key = _normalized(ancestor)
            info = _stat(ancestor, directory=True)
            if key in self.directories:
                _require(_identity(info) == self.directories[key], "PATH_REPLACED")
                continue
            handle, kernel = _open_handle(ancestor, directory=True)
            self.stack.callback(kernel.CloseHandle, handle)
            _require(_identity(info) == _identity(_stat(ancestor, directory=True)),
                     "PATH_REPLACED")
            self.directories[key] = _identity(info)
        return path

    def read(self, path, limit):
        path = _path(path)
        self.directory(path.parent)
        before = _stat(path, directory=False)
        verify_private_access(path, need_write=False)
        handle, kernel = _open_handle(path)
        import msvcrt
        try:
            _require(_identity(before) == _identity(_stat(path, directory=False)),
                     "PATH_REPLACED")
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            kernel.CloseHandle(handle)
            raise
        with os.fdopen(fd, "rb") as stream:
            _require(_identity(os.fstat(stream.fileno())) == _identity(before), "PATH_REPLACED")
            raw = stream.read(limit + 1)
        _require(len(raw) <= limit, "INPUT_TOO_LARGE")
        after = _stat(path, directory=False)
        _require((_identity(before), before.st_mtime_ns, before.st_size)
                 == (_identity(after), after.st_mtime_ns, after.st_size), "PATH_REPLACED")
        return raw, _identity(before)

    def pin_database(self, path):
        self.directory(path.parent)
        info = _stat(path, directory=False)
        verify_private_access(path, need_write=False)
        handle, kernel = _open_handle(path, mutable=True)
        self.stack.callback(kernel.CloseHandle, handle)
        _require(_identity(info) == _identity(_stat(path, directory=False)), "PATH_REPLACED")


def _json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            _require(key not in value, "JSON_DUPLICATE_KEY")
            value[key] = item
        return value

    def nonfinite(_):
        raise ThreadEvidenceExportError("JSON_NONFINITE")

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                           parse_constant=nonfinite)
        c._depth(value)
    except (UnicodeError, ValueError, RecursionError, OverflowError):
        raise ThreadEvidenceExportError("JSON_INVALID") from None
    _require(type(value) is dict, "JSON_INVALID")
    return value


def _utc(value):
    _require(type(value) is str and re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|\+00:00)",
        value), "UTC_INVALID")
    c.validate_utc_datetime(datetime.fromisoformat(value.replace("Z", "+00:00")), "utc")


def _request(raw):
    record = validate_request(_json(raw))
    _require(record["active"] is True, "PAIRING_REQUEST_INACTIVE")
    _utc(record["created_at"])
    execution_origin(record["execution_api_url"])
    c.validate_identifier(record["adapter_version"], "adapter_version")
    return record


def _read_record(paths, database, generation):
    # A read-only SQLite connection may create WAL/SHM if absent. Refuse that
    # situation; the exporter never prepares/repairs the client's live store.
    for suffix in ("", "-wal", "-shm"):
        paths.pin_database(Path(str(database) + suffix))
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True,
                                 timeout=0, isolation_level=None)
    try:
        connection.text_factory = bytes  # retain original UTF-8, no transcoding
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")

        def authorize(action, first, second, db, source):
            allowed = (
                action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_TRANSACTION)
                or (action == sqlite3.SQLITE_READ and db == "main" and source is None
                    and first == "meta" and second in ("key", "value"))
                or (action == sqlite3.SQLITE_FUNCTION and second == "typeof")
            )
            return sqlite3.SQLITE_OK if allowed else sqlite3.SQLITE_DENY

        connection.set_authorizer(authorize)
        connection.execute("BEGIN")
        rows = connection.execute(
            "SELECT value, typeof(value) FROM meta WHERE key = ? LIMIT 2",
            (KEY_PREFIX + generation,),
        ).fetchall()
        connection.execute("ROLLBACK")
    finally:
        connection.close()
    _require(len(rows) == 1, "THREAD_EVIDENCE_MISSING_OR_AMBIGUOUS")
    raw, storage_type = rows[0]
    _require(storage_type == b"text" and type(raw) is bytes and 0 < len(raw) <= RECORD_LIMIT,
             "THREAD_EVIDENCE_INVALID")
    try:
        record = c.decode(raw, ClientThreadEvidence, schema_version=c.V2_SCHEMA_VERSION)
        _require(c.encode(record, schema_version=c.V2_SCHEMA_VERSION).encode("utf-8") == raw,
                 "THREAD_EVIDENCE_NOT_CANONICAL")
    except c.ContractError:
        raise ThreadEvidenceExportError("THREAD_EVIDENCE_INVALID") from None
    return record, raw


def _receipt_bytes(record):
    _require(type(record) is dict and record.keys() == RECEIPT_FIELDS, "RECEIPT_INVALID")
    _require(record["schema_version"] == RECEIPT_SCHEMA and record["source_table"] == "meta"
             and record["source_key"] == KEY_PREFIX + record["generation"]
             and record["origin"] in ("production", "synthetic"), "RECEIPT_INVALID")
    for field in ("evidence_id", "pairing_request_id", "registration_id", "operator_action_id",
                  *SCOPE_FIELDS):
        c.validate_identifier(record[field], field)
    for field in ("evidence_sha256", "pairing_request_sha256", "client_file_sha256",
                  "contract_lock_sha256"):
        _require(type(record[field]) is str and _HEX64.fullmatch(record[field]), "RECEIPT_INVALID")
    _path(record["client_store_directory"])
    _utc(record["exported_at"])
    raw = json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False,
                     separators=(",", ":")).encode("utf-8")
    _require(len(raw) <= RECEIPT_LIMIT, "RECEIPT_TOO_LARGE")
    return raw


def _publish_new(path, raw, *, publication=None):
    """fsync temporary bytes then exclusive hardlink publication; no overwrite.

    For the success marker, remember the temp inode before linking so even a
    failure immediately after the link can remove only our own marker.
    """
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=".te-",
                                         delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
            if publication is not None:
                publication[path] = _identity(os.fstat(stream.fileno()))
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink()


def export_thread_evidence(*, client_store: Path, client_request: Path,
                           client_file: Path, expected_client_sha256: str,
                           contract_lock: Path, operator_authorization: Path,
                           operator_action_id: str, registration_id: str,
                           output_directory: Path) -> dict:
    """Export a verified original and manifest; never interpret hashes as approval."""
    try:
        return _export(client_store, client_request, client_file, expected_client_sha256,
                       contract_lock, operator_authorization, operator_action_id,
                       registration_id, output_directory)
    except ThreadEvidenceExportError:
        raise
    except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError):
        # Never include a path, JSON parser excerpt, SQLite value, or OS message.
        raise ThreadEvidenceExportError("THREAD_EVIDENCE_EXPORT_REFUSED") from None


def _export(client_store, client_request, client_file, expected_client_sha256,
            contract_lock, operator_authorization, operator_action_id,
            registration_id, output_directory):
    _require(type(expected_client_sha256) is str and _HEX64.fullmatch(expected_client_sha256),
             "CLIENT_HASH_INVALID")
    c.validate_identifier(operator_action_id, "operator_action_id")
    c.validate_identifier(registration_id, "registration_id")
    store, request_path, adapter, lock, authorization, output = map(_path, (
        client_store, client_request, client_file, contract_lock, operator_authorization,
        output_directory))
    _require(request_path == store / "pairing-request.json", "REQUEST_SLOT_MISMATCH")
    _require(adapter.name == "AKExecutionBridge.py", "CLIENT_FILE_INVALID")
    _require(not output.is_relative_to(store) and output != store, "OUTPUT_INSIDE_STORE")
    # No arbitrary input may be used to dump a store DB/sidecar or a private
    # arming slot under the guise of authorization/lock JSON.
    for artifact in (lock, authorization):
        _require(artifact.suffix.casefold() == ".json" and not artifact.is_relative_to(store),
                 "NONSECRET_JSON_ARTIFACT_REQUIRED")
    _require(len({_normalized(p) for p in (request_path, adapter, lock, authorization)}) == 4,
             "INPUT_PATH_COLLISION")
    with _Paths() as paths:
        paths.directory(store)
        paths.directory(output.parent)
        verify_private_access(store, need_write=False)
        verify_private_access(output.parent, need_write=True)
        _require(not os.path.lexists(output), "OUTPUT_DIRECTORY_CONFLICT")
        request_raw, request_identity = paths.read(request_path, JSON_LIMIT)
        request = _request(request_raw)
        _require(_normalized(_path(request["store_directory"])) == _normalized(store)
                 and store.parent.name == ".ak_execution"
                 and store.name == f"strategy-{request['strategy_id']}", "REQUEST_SLOT_MISMATCH")
        _require(request["deployment_key"] == _sha(_normalized(adapter).encode("utf-8")),
                 "CLIENT_DEPLOYMENT_MISMATCH")
        inputs = {}
        for path, limit in ((adapter, 8 * 1024 * 1024), (lock, JSON_LIMIT),
                            (authorization, JSON_LIMIT)):
            inputs[path] = (paths.read(path, limit), limit)
        adapter_raw = inputs[adapter][0][0]
        lock_raw = inputs[lock][0][0]
        authorization_raw = inputs[authorization][0][0]
        _require(_sha(adapter_raw) == expected_client_sha256 == request["client_file_sha256"],
                 "CLIENT_HASH_MISMATCH")
        _require(bool(_json(lock_raw)), "CONTRACT_LOCK_INVALID")
        # Same minimum as the existing operator loader: strict JSON and action
        # match. Scope/meaning/user consent are for the trusted registrar/human.
        _require(_json(authorization_raw).get("operator_action_id") == operator_action_id,
                 "OPERATOR_AUTHORIZATION_ACTION_MISMATCH")
        record, record_raw = _read_record(paths, store / "outbox.db", request["generation"])
        _require(all(getattr(record, field) == request[field] for field in SCOPE_FIELDS),
                 "THREAD_EVIDENCE_SCOPE_CHANGED")
        receipt = {
            "schema_version": RECEIPT_SCHEMA,
            "exported_at": datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
            "evidence_id": record.evidence_id, "evidence_sha256": _sha(record_raw),
            "source_table": "meta", "source_key": KEY_PREFIX + request["generation"],
            "client_store_directory": str(store), "pairing_request_id": request["request_id"],
            "pairing_request_sha256": _sha(request_raw), "client_file_sha256": _sha(adapter_raw),
            "contract_lock_sha256": _sha(lock_raw),
            **{field: getattr(record, field) for field in SCOPE_FIELDS},
            "registration_id": registration_id, "operator_action_id": operator_action_id,
            "origin": record.origin.value,
        }
        receipt_raw = _receipt_bytes(receipt)
        manifest = OperatorRegistrationManifest(
            registration_id=registration_id, record_type=OperatorRecordType.CLIENT_THREAD_EVIDENCE,
            record_path=str(output / OUTPUT_NAMES[0]), record_sha256=_sha(record_raw),
            contract_lock_sha256=_sha(lock_raw), operator_action_id=operator_action_id,
            authorization_artifact_path=str(authorization),
            authorization_artifact_sha256=_sha(authorization_raw), artifacts=(
                OperatorArtifact(path=str(output / OUTPUT_NAMES[1]), sha256=_sha(request_raw)),
                OperatorArtifact(path=str(output / OUTPUT_NAMES[2]), sha256=_sha(receipt_raw)),
            ))
        manifest_raw = c.encode(manifest, schema_version=c.V2_SCHEMA_VERSION).encode("utf-8")

        def reverify():
            try:
                current, identity = paths.read(request_path, JSON_LIMIT)
                _require(current == request_raw and _sha(current) == _sha(request_raw)
                         and identity == request_identity and _request(current)["active"],
                         "THREAD_EVIDENCE_SCOPE_CHANGED")
            except (OSError, ValueError):
                raise ThreadEvidenceExportError("THREAD_EVIDENCE_SCOPE_CHANGED") from None
            for path, (expected, limit) in inputs.items():
                _require(paths.read(path, limit) == expected, "INPUT_CHANGED")
            paths.directory(store)
            paths.directory(output.parent)
            verify_private_access(store, need_write=False)
            verify_private_access(output.parent, need_write=True)

        reverify()
        output.mkdir()  # exclusive reservation; a failed directory is never reused
        paths.directory(output)
        verify_private_access(output, need_write=True)
        payloads = dict(zip(OUTPUT_NAMES, (record_raw, request_raw, receipt_raw, manifest_raw)))
        published = {}
        publication = {}
        try:
            for name in OUTPUT_NAMES[:3]:
                _publish_new(output / name, payloads[name])
                published[name] = paths.read(output / name, max(JSON_LIMIT, len(payloads[name])))
                _require(published[name][0] == payloads[name], "OUTPUT_CHANGED")
            reverify()
            _require(set(os.listdir(output)) == set(OUTPUT_NAMES[:3]), "OUTPUT_CHANGED")
            for name, expected in published.items():
                _require(paths.read(output / name, JSON_LIMIT) == expected, "OUTPUT_CHANGED")
            # Last business artifact: no success marker precedes the originals.
            _publish_new(output / OUTPUT_NAMES[3], manifest_raw, publication=publication)
            reverify()
            _require(set(os.listdir(output)) == set(OUTPUT_NAMES), "OUTPUT_CHANGED")
            for name in OUTPUT_NAMES:
                _require(paths.read(output / name, JSON_LIMIT)[0] == payloads[name], "OUTPUT_CHANGED")
        except BaseException:
            # Ancestors/output are pinned against rename. Remove only our success
            # marker; retain partial originals for diagnosis, never reuse them.
            marker = output / OUTPUT_NAMES[3]
            if marker in publication:
                try:
                    if _identity(marker.lstat()) == publication[marker]:
                        marker.unlink()
                except FileNotFoundError:
                    pass
            raise
        return {"status": "exported", "tool_version": TOOL_VERSION,
                "evidence_id": record.evidence_id, "record_sha256": _sha(record_raw),
                "manifest_path": str(output / OUTPUT_NAMES[3]), "origin": record.origin.value,
                "registration_id": registration_id, "operator_action_id": operator_action_id}


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    for option in ("client-store", "client-request", "client-file", "contract-lock",
                   "operator-authorization", "output-directory"):
        parser.add_argument("--" + option, required=True, type=Path)
    for option in ("expected-client-sha256", "operator-action-id", "registration-id"):
        parser.add_argument("--" + option, required=True)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        result = export_thread_evidence(**vars(args))
    except ThreadEvidenceExportError as exc:
        print(json.dumps({"status": "refused", "tool_version": TOOL_VERSION, "code": str(exc)}))
        return 2
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
