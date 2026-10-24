"""TE-M06--M09, entirely SYNTHETIC, temporary, offline fixtures.

The client producer is NOT exercised: these independently assembled frozen v2
meta records prove the exporter only. No production store, SDK, service,
registration, broker, .env or real credential is used. Windows DACL/reparse and
live SQLite WAL tests run for real; absence of the platform fails, never skips.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from datetime import datetime, timezone
import hashlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys

import pytest

from ak_trading.infiny_execution import contracts as c
from ak_trading.infiny_execution.native_contracts import Origin
from ak_trading.infiny_execution.native_evidence import (
    CallbackKind, ClientThreadEvidence, OperatorRegistrationManifest,
)

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "scripts/export_infiny_thread_evidence.py"
MANIFEST = "register-thread-manifest.json"
SYNTHETIC_SECRET = "SYNTHETIC_DO_NOT_READ_SECRET_a7ef9634"
NOW = datetime(2026, 9, 19, 1, 2, 3, 456789, tzinfo=timezone.utc)


def load_script(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


exporter = load_script(TOOL, "synthetic_te_export")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def hex_id(name):
    return sha(("SYNTHETIC_" + name).encode())[:32]


def json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def set_dacl(path, *, everyone=False, read_only=False):
    """Real inherited protected Windows DACL; only temporary fixture paths."""
    assert sys.platform == "win32", "TE-M08 requires actual Windows permission tests"
    from ctypes import wintypes as wt
    from scripts.pair_infiny_execution import _SECURITY
    sid = _SECURITY.current_user_sid()
    rights = "FRFX" if read_only else "FA"
    flags = "OICI" if path.is_dir() else ""
    sddl = f"D:P(A;{flags};{rights};;;{sid})(A;{flags};{rights};;;SY)(A;{flags};{rights};;;BA)"
    if everyone:
        sddl += f"(A;{flags};FA;;;WD)"
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
        wt.LPCWSTR, wt.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wt.DWORD))
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wt.BOOL
    advapi.SetFileSecurityW.argtypes = (wt.LPCWSTR, wt.DWORD, ctypes.c_void_p)
    advapi.SetFileSecurityW.restype = wt.BOOL
    kernel.LocalFree.argtypes = (ctypes.c_void_p,)
    pointer = ctypes.c_void_p()
    assert advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(pointer), None)
    try:
        assert advapi.SetFileSecurityW(str(path), 0x4 | 0x80000000, pointer)
    finally:
        kernel.LocalFree(pointer)


@pytest.fixture
def case(tmp_path):
    root = tmp_path / "SYNTHETIC_TE"
    root.mkdir()
    set_dacl(root)
    strategy = root / "self_strategy"
    strategy.mkdir()
    store = strategy / ".ak_execution" / "strategy-7"
    store.mkdir(parents=True)
    adapter = strategy / "AKExecutionBridge.py"
    adapter.write_bytes(b"# SYNTHETIC approved client bytes; never imported\n")
    request = {
        "schema_version": "ne1_pairing_request_v1", "request_id": hex_id("request"),
        "active": True, "created_at": NOW.isoformat().replace("+00:00", "Z"),
        **{name: hex_id(name) for name in exporter.SCOPE_FIELDS},
        "deployment_key": sha(os.path.normcase(os.path.normpath(str(adapter))).encode()),
        "strategy_id": 7, "adapter_version": "SYNTHETIC_ne1-execution-arming-thread-v1",
        "client_file_sha256": sha(adapter.read_bytes()), "store_layout_version": 2,
        "store_directory": str(store), "exchange": "SYNTHETIC", "instrument_id": "SYNTHETIC_RB",
        "hedge_flag": "1", "execution_api_url": "http://127.0.0.1:17779/execution/v1",
        "params_sha256": sha(b"SYNTHETIC_params"),
    }
    request_path = store / "pairing-request.json"
    request_path.write_bytes(json_bytes(request) + b"\n")
    # These are inert test sentinels. The tool must never open their bytes.
    for name in ("identity.json", "pairing.json", "arming.json", "arming-challenge.json",
                 "SYNTHETIC.token", ".env"):
        (store / name).write_text(SYNTHETIC_SECRET, encoding="utf-8")
    lock = root / "SYNTHETIC-approved-lock.json"
    lock.write_bytes(json_bytes({"fixture": "SYNTHETIC", "approved_release": "SYNTHETIC-v1"}))
    authorization = root / "SYNTHETIC-operator-authorization.json"
    authorization.write_bytes(json_bytes({"fixture": "SYNTHETIC", "operator_action_id": "SYNTHETIC_action"}))
    # Independent frozen-format producer fixture, NOT AKExecutionBridge.
    payload = {"schema_version": "infiny_execution_v2", "type": "ClientThreadEvidence", "data": {
        "evidence_id": "cte-SYNTHETIC_record", **{name: request[name] for name in exporter.SCOPE_FIELDS},
        "callback_thread_id": 1234567, "callback_kind": "ON_START",
        "observed_at": "2026-09-19T01:02:03.456789Z", "origin": "synthetic",
    }}
    raw = json_bytes(payload)
    db = sqlite3.connect(store / "outbox.db")
    assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    db.execute("PRAGMA synchronous=FULL")
    db.execute("PRAGMA wal_autocheckpoint=0")
    db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    db.execute("CREATE TABLE events (id TEXT PRIMARY KEY, body TEXT)")
    db.execute("INSERT INTO events VALUES (?, ?)", ("SYNTHETIC_existing_event", "SYNTHETIC_business"))
    db.execute("INSERT INTO meta VALUES (?, ?)", ("SYNTHETIC_other_secret", SYNTHETIC_SECRET))
    db.commit()
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # FIXTURE only; before new record
    key = exporter.KEY_PREFIX + request["generation"]
    db.execute("INSERT INTO meta VALUES (?, ?)", (key, raw.decode()))
    db.commit()  # only WAL contains the evidence, writer remains alive
    kwargs = dict(client_store=store, client_request=request_path, client_file=adapter,
                  expected_client_sha256=sha(adapter.read_bytes()), contract_lock=lock,
                  operator_authorization=authorization, operator_action_id="SYNTHETIC_action",
                  registration_id="SYNTHETIC_registration", output_directory=root / "SYNTHETIC_export")
    fixture = dict(root=root, store=store, adapter=adapter, request=request,
                   request_path=request_path, payload=payload, raw=raw, key=key, db=db,
                   kwargs=kwargs, lock=lock, authorization=authorization)
    try:
        yield fixture
    finally:
        db.close()


def cli_args(case):
    result = []
    for key, value in case["kwargs"].items():
        result += ["--" + key.replace("_", "-"), str(value)]
    return result


def run_cli(case):
    return subprocess.run([sys.executable, "-B", "-X", "utf8", str(TOOL), *cli_args(case)],
                          cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30)


def export(case):
    return exporter.export_thread_evidence(**case["kwargs"])


def no_manifest(case):
    assert not (case["kwargs"]["output_directory"] / MANIFEST).exists()


def reject(case, match=None):
    with pytest.raises(exporter.ThreadEvidenceExportError, match=match) as caught:
        export(case)
    no_manifest(case)
    assert SYNTHETIC_SECRET not in str(caught.value)


def update_request(case, changes=None, *, replace=False, raw=None):
    case["request"].update(changes or {})
    raw = json_bytes(case["request"]) if raw is None else raw
    if replace:
        temporary = case["store"] / "SYNTHETIC-new-request.json"
        temporary.write_bytes(raw)
        os.replace(temporary, case["request_path"])
    else:
        case["request_path"].write_bytes(raw)


def update_record(case, raw):
    case["db"].execute("UPDATE meta SET value=? WHERE key=?", (raw, case["key"]))
    case["db"].commit()


def packet_change(case, field, value):
    payload = json.loads(case["raw"])
    payload["data"][field] = value
    return json_bytes(payload).decode()


def test_public_signature_and_cli_surface():
    signature = inspect.signature(exporter.export_thread_evidence)
    names = ["client_store", "client_request", "client_file", "expected_client_sha256",
             "contract_lock", "operator_authorization", "operator_action_id", "registration_id",
             "output_directory"]
    assert list(signature.parameters) == names
    assert all(p.kind == inspect.Parameter.KEYWORD_ONLY for p in signature.parameters.values())
    assert {a.dest for a in exporter.build_parser()._actions} == {*names, "help"}
    assert exporter.TOOL_VERSION == "ne1-thread-evidence-export-20260918-v1"


def test_m06_independent_process_reads_live_wal_and_preserves_original(case):
    main = case["store"] / "outbox.db"
    wal = case["store"] / "outbox.db-wal"
    source_hashes = {p: sha(p.read_bytes()) for p in (main, wal)}
    assert wal.stat().st_size > 32
    # Counterexample control: copying ONLY the main DB demonstrably loses row.
    main_copy = case["root"] / "SYNTHETIC-main-only.db"
    main_copy.write_bytes(main.read_bytes())
    with sqlite3.connect(main_copy) as control:
        assert control.execute("SELECT value FROM meta WHERE key=?", (case["key"],)).fetchone() is None
    started_at = datetime.now(timezone.utc)
    result = run_cli(case)
    finished_at = datetime.now(timezone.utc)
    assert result.returncode == 0, (result.stdout, result.stderr)
    status = json.loads(result.stdout)
    assert status["status"] == "exported" and status["origin"] == "synthetic"
    assert status["record_sha256"] == sha(case["raw"])
    output = case["kwargs"]["output_directory"]
    assert {p.name for p in output.iterdir()} == set(exporter.OUTPUT_NAMES)
    assert (output / "thread-evidence.json").read_bytes() == case["raw"]
    assert (output / "pairing-request.json").read_bytes() == case["request_path"].read_bytes()
    assert {p: sha(p.read_bytes()) for p in (main, wal)} == source_hashes
    assert case["db"].execute("SELECT * FROM events").fetchall() == [
        ("SYNTHETIC_existing_event", "SYNTHETIC_business")]
    assert case["db"].execute("SELECT count(*) FROM meta").fetchone() == (2,)
    receipt = json.loads((output / "source-receipt.json").read_bytes())
    assert receipt.keys() == exporter.RECEIPT_FIELDS
    assert receipt["schema_version"] == "ne1_thread_evidence_export_v1"
    assert receipt["source_key"] == case["key"] and receipt["source_table"] == "meta"
    assert receipt["evidence_sha256"] == sha(case["raw"])
    assert receipt["client_file_sha256"] == sha(case["adapter"].read_bytes())
    assert receipt["contract_lock_sha256"] == sha(case["lock"].read_bytes())
    assert receipt["pairing_request_sha256"] == sha(case["request_path"].read_bytes())
    assert receipt["origin"] == "synthetic"
    assert started_at <= datetime.fromisoformat(receipt["exported_at"].replace("Z", "+00:00")) <= finished_at
    decoded = c.decode((output / "thread-evidence.json").read_bytes(), ClientThreadEvidence,
                       schema_version=c.V2_SCHEMA_VERSION)
    assert decoded.callback_thread_id == 1234567 and decoded.callback_kind is CallbackKind.ON_START
    assert decoded.observed_at == NOW and decoded.origin is Origin.SYNTHETIC
    for field in exporter.SCOPE_FIELDS:
        assert getattr(decoded, field) == receipt[field] == case["request"][field]
    assert SYNTHETIC_SECRET not in result.stdout + result.stderr
    assert all(SYNTHETIC_SECRET.encode() not in p.read_bytes() for p in output.iterdir())


def test_m08_sql_and_file_spies_prove_allowlist_without_sdk(case, monkeypatch):
    opens, statements, connections, executions = [], [], [], []
    original_open = exporter._open_handle
    original_connect = sqlite3.connect

    def open_spy(path, **kwargs):
        if not kwargs.get("directory"):
            opens.append(Path(path))
        return original_open(path, **kwargs)

    class SpyConnection(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            executions.append((sql, parameters))
            return super().execute(sql, parameters)

    def connect_spy(*args, **kwargs):
        connections.append((args, kwargs.copy()))
        conn = original_connect(*args, **kwargs, factory=SpyConnection)
        conn.set_trace_callback(statements.append)
        return conn

    class NoSDK:
        def find_spec(self, fullname, path=None, target=None):
            assert not fullname.startswith(("pythongo", "tqsdk", "InfiniTrader", "integrations.infiny"))
            return None

    monkeypatch.setattr(exporter, "_open_handle", open_spy)
    monkeypatch.setattr(exporter.sqlite3, "connect", connect_spy)
    guard = NoSDK()
    sys.meta_path.insert(0, guard)
    try:
        export(case)
    finally:
        sys.meta_path.remove(guard)
    output = case["kwargs"]["output_directory"]
    expected = {case["request_path"], case["adapter"], case["lock"], case["authorization"],
                *(case["store"] / ("outbox.db" + suffix) for suffix in ("", "-wal", "-shm")),
                *(output / name for name in exporter.OUTPUT_NAMES)}
    assert set(opens) == expected
    assert len(connections) == 1 and connections[0][0][0].endswith("?mode=ro")
    assert connections[0][1]["uri"] is True
    assert "immutable" not in connections[0][0][0]
    selects = [(sql, params) for sql, params in executions if sql.startswith("SELECT")]
    assert selects == [("SELECT value, typeof(value) FROM meta WHERE key = ? LIMIT 2", (case["key"],))]
    assert [sql for sql, _ in executions] == ["PRAGMA query_only=ON", "PRAGMA trusted_schema=OFF",
                                             "BEGIN", selects[0][0], "ROLLBACK"]
    assert not any("checkpoint" in s.lower() or "journal_mode" in s.lower() for s in statements)
    assert SYNTHETIC_SECRET not in " ".join(statements)


@pytest.mark.parametrize("field,value", [
    ("active", False), ("active", 1), ("strategy_id", True), ("strategy_id", 8),
    ("store_layout_version", True), ("store_layout_version", 1),
    ("schema_version", "SYNTHETIC_unknown"), ("client_file_sha256", "f" * 64),
    ("deployment_key", "e" * 64), ("created_at", "2026-09-19T00:00:00"),
    ("created_at", "2026-09-19T00:00:00+01:00"), ("request_id", "bad id"),
    ("generation", "../elsewhere"), ("process_instance_id", True),
    ("execution_api_url", "http://SYNTHETIC_user:SYNTHETIC_password@127.0.0.1:17779/"),
    ("SYNTHETIC_unknown", "value"),
])
def test_m07_request_format_scope_or_release_refused(case, field, value):
    update_request(case, {field: value})
    reject(case)


@pytest.mark.parametrize("field", exporter.SCOPE_FIELDS)
def test_m07_wrong_record_scope_refused(case, field):
    update_record(case, packet_change(case, field, hex_id("wrong_" + field)))
    reject(case, "THREAD_EVIDENCE_SCOPE_CHANGED")


@pytest.mark.parametrize("mode", ["changed", "stop", "restart", "identical_replace", "delete",
                                  "whitespace", "invalid"])
@pytest.mark.parametrize("stage", ["db_read", "receipt", "manifest"])
def test_m07_request_change_through_publication_fails_closed(case, monkeypatch, mode, stage):
    def mutate():
        if mode == "identical_replace":
            update_request(case, replace=True, raw=case["request_path"].read_bytes())
        elif mode == "delete":
            case["request_path"].unlink()
        elif mode == "whitespace":
            update_request(case, raw=case["request_path"].read_bytes() + b"\n")
        elif mode == "invalid":
            update_request(case, raw=b"{SYNTHETIC_invalid")
        else:
            changes = {"request_id": hex_id("replacement")}
            if mode == "stop":
                changes["active"] = False
            if mode == "restart":
                changes.update({name: hex_id("new_" + name) for name in exporter.SCOPE_FIELDS})
            update_request(case, changes, replace=True)

    if stage == "db_read":
        original = exporter._read_record

        def read(*args):
            result = original(*args)
            mutate()
            return result
        monkeypatch.setattr(exporter, "_read_record", read)
    else:
        original = exporter._publish_new
        target = "source-receipt.json" if stage == "receipt" else MANIFEST

        def publish(path, raw, **kwargs):
            original(path, raw, **kwargs)
            if path.name == target:
                mutate()
        monkeypatch.setattr(exporter, "_publish_new", publish)
    reject(case, "THREAD_EVIDENCE_SCOPE_CHANGED")


@pytest.mark.parametrize("kind", ["missing", "blob", "null", "oversize", "duplicate", "newline", "bom",
                                  "bad_utf8", "unknown", "schema", "type", "upper_origin", "bool_thread",
                                  "nan", "invalid_id", "naive_time", "extra_envelope", "pretty"])
def test_m02_m07_bad_original_is_never_repaired(case, kind):
    if kind == "missing":
        case["db"].execute("DELETE FROM meta WHERE key=?", (case["key"],))
        case["db"].commit()
    elif kind == "bad_utf8":
        case["db"].execute("UPDATE meta SET value=CAST(x'fffe' AS TEXT) WHERE key=?", (case["key"],))
        case["db"].commit()
    else:
        raw = case["raw"].decode()
        mutations = {
            "blob": case["raw"], "null": None, "oversize": "x" * 8193,
            "duplicate": raw.replace('"callback_thread_id":1234567',
                                     '"callback_thread_id":1234567,"callback_thread_id":1234567'),
            "newline": raw + "\n", "bom": "\ufeff" + raw,
            "unknown": packet_change(case, "SYNTHETIC_extra", True),
            "schema": raw.replace("infiny_execution_v2", "infiny_client_execution_transport_v1_1"),
            "type": raw.replace("ClientThreadEvidence", "NativeRouteEvidence"),
            "upper_origin": raw.replace('"synthetic"', '"SYNTHETIC"'),
            "bool_thread": packet_change(case, "callback_thread_id", True),
            "nan": packet_change(case, "callback_thread_id", float("nan")),
            "invalid_id": packet_change(case, "evidence_id", "bad id"),
            "naive_time": packet_change(case, "observed_at", "2026-09-19T01:02:03"),
            "extra_envelope": raw[:-1] + ',"unknown":0}',
            "pretty": json.dumps(case["payload"], indent=2),
        }
        update_record(case, mutations[kind])
    reject(case)


def test_m06_only_uncommitted_memory_row_is_unavailable(case):
    case["db"].execute("DELETE FROM meta WHERE key=?", (case["key"],))
    case["db"].commit()
    case["db"].execute("INSERT INTO meta VALUES (?, ?)", (case["key"], case["raw"].decode()))
    try:
        reject(case, "THREAD_EVIDENCE_MISSING")
    finally:
        case["db"].rollback()


def test_m06_db_readonly_real_dacl_success_and_no_writes(case):
    paths = [case["store"] / ("outbox.db" + s) for s in ("", "-wal", "-shm")]
    try:
        for path in paths:
            set_dacl(path, read_only=True)
        export(case)
    finally:
        for path in paths:
            set_dacl(path)


@pytest.mark.parametrize("slot", ["store", "request_path", "adapter", "lock", "authorization", "output_parent"])
def test_m08_real_broad_dacl_is_refused(case, slot):
    path = case["root"] if slot == "output_parent" else case[slot]
    try:
        set_dacl(path, everyone=True)
        reject(case)
    finally:
        set_dacl(path)


def test_m08_real_readonly_output_parent_refused(case):
    try:
        set_dacl(case["root"], read_only=True)
        reject(case)
    finally:
        set_dacl(case["root"])


@pytest.mark.parametrize("name", [".env", ".env.production", "SYNTHETIC.token", "identity.json",
                                  "pairing.json", "arming.json", "arming-challenge.json",
                                  "SYNTHETIC-credentials.json", "SYNTHETIC-secret.json"])
def test_m08_secret_input_name_rejected_before_open(case, monkeypatch, name):
    target = case["store"] / name
    if not target.exists():
        target.write_text(SYNTHETIC_SECRET, encoding="utf-8")
    original = exporter._open_handle

    def spy(path, **kwargs):
        assert path != target
        return original(path, **kwargs)
    monkeypatch.setattr(exporter, "_open_handle", spy)
    case["kwargs"]["operator_authorization"] = target
    reject(case, "SECRET_PATH_REFUSED")


@pytest.mark.parametrize("value", [r"\\SYNTHETIC-server\share\output", r"\\?\C:\output",
                                   "https://SYNTHETIC/output", "relative/output", "C:output",
                                   "D:/SYNTHETIC/../output", "D:/SYNTHETIC/NUL",
                                   "D:/SYNTHETIC/file:stream", "D:/SYNTHETIC/space. ",
                                   "%TEMP%/SYNTHETIC", "D:/SYNTHETIC/${HOME}"])
def test_m08_unsafe_local_paths_refused(case, value, monkeypatch):
    safe_output = case["kwargs"]["output_directory"]
    case["kwargs"]["output_directory"] = value

    def no_open(*args, **kwargs):
        pytest.fail("unsafe path reached filesystem I/O")
    monkeypatch.setattr(exporter, "_open_handle", no_open)
    with pytest.raises(exporter.ThreadEvidenceExportError):
        export(case)
    # Never stat an unsafe/UNC argument even in the test's assertion.
    assert not safe_output.exists()


def test_m08_wrong_request_path_and_store_slot(case):
    new = case["root"] / "SYNTHETIC-request.json"
    new.write_bytes(case["request_path"].read_bytes())
    case["kwargs"]["client_request"] = new
    reject(case, "REQUEST_SLOT_MISMATCH")
    case["kwargs"]["client_request"] = case["request_path"]
    update_request(case, {"store_directory": str(case["root"] / "SYNTHETIC-other-slot")})
    reject(case, "REQUEST_SLOT_MISMATCH")


def test_m08_store_cannot_be_output_parent(case):
    case["kwargs"]["output_directory"] = case["store"] / "SYNTHETIC-export"
    reject(case, "OUTPUT_INSIDE_STORE")


@pytest.mark.parametrize("kind", ["directory", "file", "published"])
def test_m08_existing_output_never_overwritten(case, kind):
    output = case["kwargs"]["output_directory"]
    if kind == "published":
        export(case)
        before = {p.name: p.read_bytes() for p in output.iterdir()}
    elif kind == "directory":
        output.mkdir()
        before = {}
    else:
        output.write_text("SYNTHETIC_EXISTING", encoding="utf-8")
        before = output.read_bytes()
    with pytest.raises(exporter.ThreadEvidenceExportError, match="OUTPUT_DIRECTORY_CONFLICT"):
        export(case)
    if output.is_dir():
        assert {p.name: p.read_bytes() for p in output.iterdir()} == before
    else:
        assert output.read_bytes() == before


@pytest.mark.parametrize("target", exporter.OUTPUT_NAMES)
@pytest.mark.parametrize("when", ["before", "after"])
def test_m08_publication_io_failure_never_leaves_manifest(case, monkeypatch, target, when):
    original = exporter._publish_new

    def publish(path, raw, **kwargs):
        if path.name == target and when == "before":
            raise OSError(SYNTHETIC_SECRET)
        original(path, raw, **kwargs)
        if path.name == target and when == "after":
            raise OSError(SYNTHETIC_SECRET)
    monkeypatch.setattr(exporter, "_publish_new", publish)
    reject(case)


def test_m08_fsync_failure_has_no_manifest_or_temp(case, monkeypatch):
    def fail(*args):
        raise OSError("SYNTHETIC_disk_full")
    monkeypatch.setattr(exporter.os, "fsync", fail)
    reject(case)
    assert list(case["kwargs"]["output_directory"].iterdir()) == []


@pytest.mark.parametrize("input_name", ["adapter", "lock", "authorization"])
@pytest.mark.parametrize("mutation", ["append", "identical_replace"])
def test_m09_inputs_reverified_before_manifest(case, monkeypatch, input_name, mutation):
    original = exporter._publish_new

    def publish(path, raw, **kwargs):
        original(path, raw, **kwargs)
        if path.name == "source-receipt.json":
            target = case[input_name]
            if mutation == "append":
                target.write_bytes(target.read_bytes() + b"\n")
            else:
                new = target.with_name("SYNTHETIC-new-" + target.name)
                new.write_bytes(target.read_bytes())
                os.replace(new, target)
    monkeypatch.setattr(exporter, "_publish_new", publish)
    reject(case, "INPUT_CHANGED")


@pytest.mark.parametrize("artifact", ["thread-evidence.json", "pairing-request.json", "source-receipt.json"])
def test_m09_output_tampering_before_manifest_fails(case, monkeypatch, artifact):
    original = exporter._publish_new

    def publish(path, raw, **kwargs):
        original(path, raw, **kwargs)
        if path.name == "source-receipt.json":
            target = path.parent / artifact
            target.write_bytes(target.read_bytes() + b"\n")
    monkeypatch.setattr(exporter, "_publish_new", publish)
    reject(case, "OUTPUT_CHANGED")


def test_m09_manifest_exact_v2_and_last_publication(case, monkeypatch):
    original = exporter._publish_new
    order = []

    def publish(path, raw, **kwargs):
        order.append(path.name)
        if path.name == MANIFEST:
            assert {p.name for p in path.parent.iterdir()} == set(exporter.OUTPUT_NAMES[:3])
        original(path, raw, **kwargs)
    monkeypatch.setattr(exporter, "_publish_new", publish)
    export(case)
    assert order == list(exporter.OUTPUT_NAMES)
    output = case["kwargs"]["output_directory"]
    raw = (output / MANIFEST).read_bytes()
    manifest = c.decode(raw, OperatorRegistrationManifest, schema_version=c.V2_SCHEMA_VERSION)
    assert c.encode(manifest, schema_version=c.V2_SCHEMA_VERSION).encode() == raw
    assert manifest.record_type.value == "ClientThreadEvidence"
    assert manifest.registration_id == "SYNTHETIC_registration"
    assert manifest.operator_action_id == "SYNTHETIC_action"
    assert manifest.record_path == str(output / "thread-evidence.json")
    assert manifest.record_sha256 == sha(case["raw"])
    assert manifest.contract_lock_sha256 == sha(case["lock"].read_bytes())
    assert manifest.authorization_artifact_path == str(case["authorization"])
    assert manifest.authorization_artifact_sha256 == sha(case["authorization"].read_bytes())
    assert [(a.path, a.sha256) for a in manifest.artifacts] == [
        (str(output / name), sha((output / name).read_bytes())) for name in exporter.OUTPUT_NAMES[1:3]]
    # Read-only existing manifest verifier. No CLI registration/journal/service.
    service_cli = load_script(ROOT / "scripts/run_infiny_execution.py", "SYNTHETIC_manifest_reader")
    assert service_cli._decode_manifest(output / MANIFEST, case["lock"]) == manifest


@pytest.mark.parametrize("item", ["record", "request", "receipt", "authorization", "lock"])
def test_m09_existing_manifest_verifier_refuses_changed_bytes(case, item):
    export(case)
    output = case["kwargs"]["output_directory"]
    paths = dict(record=output / "thread-evidence.json", request=output / "pairing-request.json",
                 receipt=output / "source-receipt.json", authorization=case["authorization"], lock=case["lock"])
    paths[item].write_bytes(paths[item].read_bytes() + b"\n")
    service_cli = load_script(ROOT / "scripts/run_infiny_execution.py", "SYNTHETIC_manifest_tampering")
    with pytest.raises(ValueError, match="HASH_MISMATCH|LOCK_MISMATCH"):
        service_cli._decode_manifest(output / MANIFEST, case["lock"])


@pytest.mark.parametrize("kind", ["unknown", "wrong_envelope", "duplicate"])
def test_m09_manifest_strict_codec_rejects_unknown_or_wrong_category(case, kind):
    export(case)
    raw = (case["kwargs"]["output_directory"] / MANIFEST).read_bytes()
    packet = json.loads(raw)
    if kind == "unknown":
        packet["data"]["SYNTHETIC_unknown"] = 1
        raw = json_bytes(packet)
    elif kind == "wrong_envelope":
        packet["type"] = "ClientThreadEvidence"
        raw = json_bytes(packet)
    else:
        raw = raw.replace(b'"registration_id":"SYNTHETIC_registration"',
                          b'"registration_id":"SYNTHETIC_registration","registration_id":"SYNTHETIC_registration"')
    with pytest.raises(c.ContractError):
        c.decode(raw, OperatorRegistrationManifest, schema_version=c.V2_SCHEMA_VERSION)


@pytest.mark.parametrize("input_name", ["request_path", "authorization", "lock"])
@pytest.mark.parametrize("raw", [b'{"SYNTHETIC":NaN}', b'{"SYNTHETIC":1,"SYNTHETIC":2}',
                                 b'[]', b'\xef\xbb\xbf{}', b'\xff', b'{"SYNTHETIC":Infinity}'])
def test_m09_json_bad_format_duplicate_or_nan_refused(case, input_name, raw):
    case[input_name].write_bytes(raw)
    reject(case)


def test_m09_action_and_id_validation(case):
    case["kwargs"]["operator_action_id"] = "SYNTHETIC_wrong_action"
    reject(case, "OPERATOR_AUTHORIZATION_ACTION_MISMATCH")
    case["kwargs"]["operator_action_id"] = True
    reject(case)
    case["kwargs"]["operator_action_id"] = "SYNTHETIC_action"
    case["kwargs"]["registration_id"] = "SYNTHETIC bad id"
    reject(case)


def test_m08_cli_refusal_never_discloses_malformed_record(case):
    update_record(case, SYNTHETIC_SECRET)
    result = run_cli(case)
    assert result.returncode == 2
    assert json.loads(result.stdout)["status"] == "refused"
    assert SYNTHETIC_SECRET not in result.stdout + result.stderr
    assert "Traceback" not in result.stderr
    no_manifest(case)


def test_m08_real_hardlink_input_refused(case):
    alias = case["root"] / "SYNTHETIC-lock-alias.json"
    os.link(case["lock"], alias)
    try:
        reject(case, "HARDLINK_PATH_REFUSED")
    finally:
        alias.unlink()


@contextmanager
def junction(path, target):
    """Actual mount-point reparse tag using Win32, no shell/SDK/network."""
    from ctypes import wintypes as wt
    path.mkdir()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.LPVOID,
                                  wt.DWORD, wt.DWORD, wt.HANDLE)
    kernel.CreateFileW.restype = wt.HANDLE
    kernel.DeviceIoControl.argtypes = (wt.HANDLE, wt.DWORD, wt.LPVOID, wt.DWORD,
                                      wt.LPVOID, wt.DWORD, ctypes.POINTER(wt.DWORD), wt.LPVOID)
    kernel.DeviceIoControl.restype = wt.BOOL
    kernel.CloseHandle.argtypes = (wt.HANDLE,)
    handle = kernel.CreateFileW(str(path), 0x40000000, 0, None, 3, 0x02200000, None)
    assert handle != ctypes.c_void_p(-1).value
    substitute = ("\\??\\" + str(target)).encode("utf-16-le")
    display = str(target).encode("utf-16-le")
    names = substitute + b"\0\0" + display + b"\0\0"
    data = struct.pack("<LHHHHHH", 0xA0000003, 8 + len(names), 0,
                       0, len(substitute), len(substitute) + 2, len(display)) + names
    buffer = ctypes.create_string_buffer(data)
    returned = wt.DWORD()
    try:
        assert kernel.DeviceIoControl(handle, 0x000900A4, buffer, len(data), None, 0,
                                      ctypes.byref(returned), None)
    finally:
        kernel.CloseHandle(handle)
    try:
        yield path
    finally:
        path.rmdir()


def test_m08_real_junction_ancestor_refused(case):
    alias = case["root"] / "SYNTHETIC-junction"
    with junction(alias, case["adapter"].parent):
        case["kwargs"]["client_file"] = alias / "AKExecutionBridge.py"
        update_request(case, {"deployment_key": sha(os.path.normcase(
            os.path.normpath(str(case["kwargs"]["client_file"]))).encode())})
        reject(case, "REPARSE_PATH_REFUSED")


def test_m08_actual_directory_replacement_blocked_by_handle(case, monkeypatch):
    original = exporter._read_record
    attempted = []

    def read(*args):
        with pytest.raises(OSError):
            case["store"].rename(case["store"].with_name("SYNTHETIC-replaced-slot"))
        attempted.append(True)
        return original(*args)
    monkeypatch.setattr(exporter, "_read_record", read)
    export(case)
    assert attempted == [True]


def test_m08_db_replacement_is_blocked_while_reading(case, monkeypatch):
    original = exporter.sqlite3.connect

    def connect(*args, **kwargs):
        with pytest.raises(OSError):
            (case["store"] / "outbox.db").rename(case["store"] / "SYNTHETIC-replaced.db")
        return original(*args, **kwargs)
    monkeypatch.setattr(exporter.sqlite3, "connect", connect)
    export(case)


def test_m08_db_failure_has_no_success(case, monkeypatch):
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError(SYNTHETIC_SECRET)
    monkeypatch.setattr(exporter.sqlite3, "connect", fail)
    reject(case)


def test_m06_missing_live_sidecars_refuses_without_creating_them(case):
    case["db"].close()
    sidecars = [case["store"] / ("outbox.db" + s) for s in ("-wal", "-shm")]
    assert all(not p.exists() for p in sidecars)
    reject(case)
    assert all(not p.exists() for p in sidecars)


@pytest.mark.parametrize("argument", ["contract_lock", "operator_authorization"])
@pytest.mark.parametrize("name", ["outbox.db", "outbox.db-wal", "receipts.db", "arming-plan.json"])
def test_m08_arbitrary_store_input_cannot_dump_database(case, monkeypatch, argument, name):
    case["kwargs"][argument] = case["store"] / name

    def no_open(*args, **kwargs):
        pytest.fail("database/arming input reached file read")
    monkeypatch.setattr(exporter, "_open_handle", no_open)
    reject(case, "NONSECRET_JSON_ARTIFACT_REQUIRED")


def test_m07_scope_changed_after_export_is_historical_not_rebound(case):
    export(case)
    output = case["kwargs"]["output_directory"]
    before = {p.name: p.read_bytes() for p in output.iterdir()}
    update_request(case, {"generation": hex_id("next_generation"),
                          "session_epoch": hex_id("next_session")}, replace=True)
    # Completed export stays an immutable historical snapshot. A fresh export
    # cannot claim the next active scope from the old original.
    case["kwargs"]["output_directory"] = case["root"] / "SYNTHETIC_next_export"
    reject(case, "THREAD_EVIDENCE_MISSING")
    assert {p.name: p.read_bytes() for p in output.iterdir()} == before


def test_m08_actual_output_directory_replacement_blocked(case, monkeypatch):
    original = exporter._publish_new
    attempts = []

    def publish(path, raw, **kwargs):
        if path.name == "source-receipt.json":
            with pytest.raises(OSError):
                path.parent.rename(path.parent.with_name("SYNTHETIC_replaced_output"))
            attempts.append(True)
        original(path, raw, **kwargs)
    monkeypatch.setattr(exporter, "_publish_new", publish)
    export(case)
    assert attempts == [True]


def test_m08_concurrent_output_creation_refused(case, monkeypatch):
    original = exporter._read_record
    sentinel = b"SYNTHETIC_other_owner"

    def read(*args):
        result = original(*args)
        output = case["kwargs"]["output_directory"]
        output.mkdir()
        (output / "SYNTHETIC_other_owner").write_bytes(sentinel)
        return result
    monkeypatch.setattr(exporter, "_read_record", read)
    reject(case)
    assert (case["kwargs"]["output_directory"] / "SYNTHETIC_other_owner").read_bytes() == sentinel


@pytest.mark.parametrize("stage", ["receipt", "manifest_link"])
def test_m08_foreign_manifest_conflict_is_not_deleted(case, monkeypatch, stage):
    sentinel = b"SYNTHETIC_foreign_writer_not_a_success_manifest"
    if stage == "receipt":
        original = exporter._publish_new

        def publish(path, raw, **kwargs):
            original(path, raw, **kwargs)
            if path.name == "source-receipt.json":
                (path.parent / MANIFEST).write_bytes(sentinel)
        monkeypatch.setattr(exporter, "_publish_new", publish)
    else:
        original = exporter.os.link

        def link(source, destination):
            if Path(destination).name == MANIFEST:
                Path(destination).write_bytes(sentinel)
            return original(source, destination)
        monkeypatch.setattr(exporter.os, "link", link)
    with pytest.raises(exporter.ThreadEvidenceExportError):
        export(case)
    assert (case["kwargs"]["output_directory"] / MANIFEST).read_bytes() == sentinel


def test_m09_receipt_unknown_keys_and_types_refused(case):
    export(case)
    raw = (case["kwargs"]["output_directory"] / "source-receipt.json").read_bytes()
    receipt = json.loads(raw)
    for changes in ({"unknown": True}, {"evidence_sha256": "A" * 64},
                    {"operator_action_id": True}, {"origin": "SYNTHETIC"},
                    {"source_table": "events"}, {"source_key": "SYNTHETIC_other"},
                    {"exported_at": "2026-09-19T00:00:00"}):
        with pytest.raises((exporter.ThreadEvidenceExportError, c.ContractError)):
            exporter._receipt_bytes({**receipt, **changes})
