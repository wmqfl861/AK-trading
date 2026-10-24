"""TE: NE1 deployment face - installer plan/apply/rollback, backups, journal
consistency copies, the migration CLI, and self-contained loading of the
installer-deployed client file (plan 10.1 TE row; NE1-H36 install face,
H37 self-contained loading, H38 install coexistence).

Since decision NE1-ARB-ARMING-001 (ARM-M13, delivery owner): the installer
never backs up, copies, hashes or restores the client store's arming-v1
slots (arming-*.json / disarm.json / arming-transfer.lock / arming.json);
apply and rollback keep them byte-identical and no SYNTHETIC arming secret
ever lands in ordinary evidence.

Every test runs inside pytest tmp directories against sandbox strategy
directories; the real self_strategy directory and ports 17778/17779 are never
touched, no network is used, and subprocesses only spawn the same guarded
interpreter on repository CLIs.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from infiny_native_fakes import FakeNativeModule

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_infiny_execution.py"
MIGRATE = ROOT / "scripts" / "migrate_infiny_execution.py"
CLIENT_SOURCE = ROOT / "integrations" / "infiny" / "AKExecutionBridge.py"
TEMPLATE = ROOT / "config" / "infiny_native_execution.example.json"

INSTALLER_VERSION = "ne1-delivery-20260918-arming-v1"

OBSERVER_FAKE = ('# sandbox observer pair, version observer-20260914-reconnect-v5\n'
                 'class AKObserverBridge:\n    pass\n')

# SYNTHETIC arming material (labeled on purpose; never a production secret).
SYNTHETIC_ARMING_NONCE = "9f3c" * 16


def _sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _run(argv: list[str]) -> tuple[int, dict]:
    result = subprocess.run([sys.executable, str(INSTALLER), *argv],
                            capture_output=True, text=True)
    text = result.stdout.strip()
    if not text:
        raise AssertionError(
            f"installer produced no stdout (exit {result.returncode}); "
            f"stderr tail: {result.stderr.strip()[-500:]}")
    payload = json.loads(text.splitlines()[-1])
    return result.returncode, payload


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _sandbox(tmp_path: Path) -> dict:
    """A fake self_strategy: identical observer pair + no execution file yet."""
    strategy = tmp_path / "strategy"
    strategy.mkdir(parents=True)
    for name in ("AKObserverBridge.py", "AIBridge.py"):
        (strategy / name).write_text(OBSERVER_FAKE, encoding="utf-8")
    release = tmp_path / "release"
    release.mkdir(parents=True)
    (release / "AKExecutionBridge.py").write_bytes(CLIENT_SOURCE.read_bytes())
    return {"strategy": strategy, "release": release,
            "observer_sha": _sha(strategy / "AIBridge.py"),
            "release_sha": _sha(release / "AKExecutionBridge.py")}


def _evidence_json(tmp_path: Path) -> dict:
    return {
        "stopped": _write(tmp_path / "stopped.json", json.dumps({
            "instance": "SANDBOX-INST", "generation": "a" * 16,
            "stopped_at": "2026-09-14T00:00:00Z", "stop_log_ref": "sandbox/log",
            "confirmed_by": "user", "reload_pending": True})),
        "pending": _write(tmp_path / "pending.json", json.dumps({
            "journal_pending_attempts": 0, "client_unacked_events": 0,
            "real_native_send_attempts": 0, "real_native_cancel_attempts": 0,
            "recorded_at": "2026-09-14T00:00:00Z"})),
        "pending_blocked": _write(tmp_path / "pending-blocked.json", json.dumps({
            "journal_pending_attempts": 1, "client_unacked_events": 0,
            "real_native_send_attempts": 0, "real_native_cancel_attempts": 0,
            "recorded_at": "2026-09-14T00:00:00Z"})),
        "gate": _write(tmp_path / "gate.json", json.dumps({
            "gate_closed": True, "service_stopped_issuing": True,
            "recorded_at": "2026-09-14T01:00:00Z",
            "evidence_refs": ["sandbox/gate.txt"]})),
    }


def _plan_args(box: dict, evidence_dir: Path, pending: Path, **extra) -> list[str]:
    args = ["plan",
            "--strategy-directory", str(box["strategy"]),
            "--release-directory", str(box["release"]),
            "--evidence-directory", str(evidence_dir),
            "--journal", extra.pop("journal", "absent"),
            "--client-store", "absent",
            "--pending-state", str(pending),
            "--expected-source-sha256", box["release_sha"],
            "--expected-target-sha256", extra.pop("target", "ABSENT"),
            "--observer-expected-sha256", box["observer_sha"]]
    args.extend(token for key, value in extra.items()
                for token in (f"--{key.replace('_', '-')}", str(value)))
    return args


def _apply_args(box: dict, evidence_dir: Path, ev: dict, **extra) -> list[str]:
    args = ["apply",
            "--strategy-directory", str(box["strategy"]),
            "--release-directory", str(box["release"]),
            "--evidence-directory", str(evidence_dir),
            "--expected-source-sha256", box["release_sha"],
            "--expected-target-sha256", extra.pop("target", "ABSENT"),
            "--stopped-evidence", str(ev["stopped"]),
            "--journal", extra.pop("journal", "absent"),
            "--client-store", "absent",
            "--pending-state", str(extra.pop("pending", ev["pending"])),
            "--observer-expected-sha256", box["observer_sha"]]
    args.extend(token for key, value in extra.items()
                for token in (f"--{key.replace('_', '-')}", str(value)))
    return args


# ---------------------------------------------------------------------------
# CLI hygiene (plan 10.2: --help/--dry-run must have no side effects)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("script", [INSTALLER, MIGRATE])
def test_help_has_no_side_effects(script: Path, tmp_path: Path):
    result = subprocess.run([sys.executable, str(script), "--help"],
                            capture_output=True, text=True, cwd=str(tmp_path))
    assert result.returncode == 0
    assert "usage:" in result.stdout
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# installer: plan / apply / rollback full flow
# ---------------------------------------------------------------------------

def test_plan_reports_clean_blockers_and_records_chain(tmp_path: Path):
    box = _sandbox(tmp_path)
    ev = _evidence_json(tmp_path)
    evidence = tmp_path / "ev-plan"
    code, payload = _run(_plan_args(box, evidence, ev["pending"]))
    assert code == 0 and payload["status"] == "planned" and payload["blockers"] == []
    record = json.loads((evidence / "deployment-plan.json").read_text(encoding="utf-8"))
    assert record["plan"]["sha256"] == record["plan"]["expected_sha256"]
    assert record["observer_pair"]["ok"] is True
    assert record["observer_pair"]["sampled_at"]
    assert record["release"]["adapter_version"].startswith("ne1-execution-")
    # plan is read-only for the strategy directory
    assert not (box["strategy"] / "AKExecutionBridge.py").exists()


def test_apply_deploys_backs_up_and_publishes_receipt(tmp_path: Path):
    box = _sandbox(tmp_path)
    ev = _evidence_json(tmp_path)
    evidence = tmp_path / "ev-deploy"
    code, payload = _run(_apply_args(box, evidence, ev))
    assert code == 0 and payload["status"] == "deployed", payload
    target = box["strategy"] / "AKExecutionBridge.py"
    assert _sha(target) == box["release_sha"]
    receipt = json.loads((evidence / "deployment.json").read_text(encoding="utf-8"))
    assert receipt["client_reload_state"] == "CLIENT_RELOAD_PENDING"
    assert receipt["prior_target"]["sha256"] == "ABSENT"
    assert receipt["native_count_start"] is None  # recorded at service start
    assert receipt["installer"]["sha256"] == _sha(INSTALLER)
    assert receipt["params_template"]["sha256"] == _sha(TEMPLATE)
    assert receipt["observer_pair"]["identical"] is True
    assert receipt["observer_pair"]["sampled_at"]
    backup = evidence / "backup"
    assert (backup / "AKExecutionBridge.py.ABSENT.json").is_file()
    for name in ("AIBridge.py", "AKObserverBridge.py"):
        saved = backup / f"{name}.client-backup-{box['observer_sha'][:8]}.py"
        assert saved.is_file() and _sha(saved) == box["observer_sha"]
    # observer pair untouched by the deployment (H38)
    assert _sha(box["strategy"] / "AIBridge.py") == box["observer_sha"]
    assert _sha(box["strategy"] / "AKObserverBridge.py") == box["observer_sha"]


def test_apply_blocks_on_pending_journal_attempts(tmp_path: Path):
    box = _sandbox(tmp_path)
    ev = _evidence_json(tmp_path)
    code, payload = _run(_apply_args(box, tmp_path / "ev-blocked", ev,
                                     pending=ev["pending_blocked"]))
    assert code == 2 and payload["code"] == "DEPLOY_BLOCKED_PENDING_JOURNAL_ATTEMPTS"
    assert not (box["strategy"] / "AKExecutionBridge.py").exists()
    assert not (tmp_path / "ev-blocked" / "deployment.json").exists()


def test_apply_requires_pending_state_provenance_for_real_journal(tmp_path: Path):
    box = _sandbox(tmp_path)
    ev = _evidence_json(tmp_path)
    journal = tmp_path / "journal.sqlite"
    connection = sqlite3.connect(str(journal))
    connection.execute("CREATE TABLE ledger_meta (key TEXT PRIMARY KEY, value TEXT)")
    connection.execute("INSERT INTO ledger_meta VALUES ('schema','v1')")
    connection.commit()
    connection.close()
    code, payload = _run(_apply_args(box, tmp_path / "ev-prov", ev,
                                     journal=str(journal)))
    assert code == 2 and payload["code"] == "PENDING_STATE_SOURCE_REQUIRED_FOR_JOURNAL"
    with_provenance = _write(tmp_path / "pending-sourced.json", json.dumps({
        "journal_pending_attempts": 0, "client_unacked_events": 0,
        "real_native_send_attempts": 0, "real_native_cancel_attempts": 0,
        "recorded_at": "2026-09-14T00:00:00Z",
        "source": "service_status", "sampled_at": "2026-09-14T00:00:00Z"}))
    code, payload = _run(_apply_args(box, tmp_path / "ev-prov2", ev,
                                     journal=str(journal), pending=with_provenance))
    assert code == 0 and payload["status"] == "deployed", payload
    backup = tmp_path / "ev-prov2" / "backup" / "journal-consistency.sqlite"
    check = sqlite3.connect(f"file:{backup.as_posix()}?mode=ro", uri=True)
    try:
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert check.execute("SELECT COUNT(*) FROM ledger_meta").fetchone()[0] == 1
    finally:
        check.close()
    # the source journal itself is never opened for write
    assert journal.read_bytes()[:15] == b"SQLite format 3"


def test_plan_flags_observer_pair_drift(tmp_path: Path):
    box = _sandbox(tmp_path)
    ev = _evidence_json(tmp_path)
    (box["strategy"] / "AIBridge.py").write_text(OBSERVER_FAKE + "# drifted\n",
                                                 encoding="utf-8")
    code, payload = _run(_plan_args(box, tmp_path / "ev-mismatch", ev["pending"]))
    assert code == 0 and "OBSERVER_PAIR_MISMATCH" in payload["blockers"]


def test_second_install_rejected_until_target_hash_updated(tmp_path: Path):
    box = _sandbox(tmp_path)
    ev = _evidence_json(tmp_path)
    assert _run(_apply_args(box, tmp_path / "ev-1", ev))[0] == 0
    code, payload = _run(_apply_args(box, tmp_path / "ev-2", ev))
    assert code == 2 and payload["code"] == "EXPECTED_TARGET_SHA256_MISMATCH"


def test_tampered_release_and_template_are_rejected(tmp_path: Path):
    box = _sandbox(tmp_path)
    ev = _evidence_json(tmp_path)
    bad_release = tmp_path / "bad-release"
    bad_release.mkdir()
    (bad_release / "AKExecutionBridge.py").write_text(
        CLIENT_SOURCE.read_text(encoding="utf-8") + "\nclass AKObserverBridge:\n    pass\n",
        encoding="utf-8")
    args = ["apply", "--strategy-directory", str(box["strategy"]),
            "--release-directory", str(bad_release),
            "--evidence-directory", str(tmp_path / "ev-badrel"),
            "--expected-source-sha256", _sha(bad_release / "AKExecutionBridge.py"),
            "--expected-target-sha256", "ABSENT",
            "--stopped-evidence", str(ev["stopped"]),
            "--journal", "absent", "--client-store", "absent",
            "--pending-state", str(ev["pending"]),
            "--observer-expected-sha256", box["observer_sha"]]
    code, payload = _run(args)
    assert code == 2 and payload["code"] == "RELEASE_MUST_NOT_CONTAIN_OBSERVER"

    box2 = _sandbox(tmp_path / "second")
    bad_template = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    bad_template["enable_trading"] = True
    template_path = _write(tmp_path / "bad-template.json", json.dumps(bad_template))
    code, payload = _run(_apply_args(box2, tmp_path / "ev-badtpl", ev,
                                     template=str(template_path)))
    assert code == 2 and payload["code"] == "TEMPLATE_ENABLE_TRADING_MUST_BE_FALSE"
    assert not (box2["strategy"] / "AKExecutionBridge.py").exists()


def test_rollback_removes_absent_prior_install_and_verifies_observer(tmp_path: Path):
    box = _sandbox(tmp_path)
    ev = _evidence_json(tmp_path)
    assert _run(_apply_args(box, tmp_path / "ev-deploy", ev))[0] == 0
    rollback_dir = tmp_path / "ev-rollback"
    code, payload = _run([
        "rollback", "--strategy-directory", str(box["strategy"]),
        "--evidence-directory", str(tmp_path / "ev-deploy"),
        "--rollback-evidence-directory", str(rollback_dir),
        "--gate-evidence", str(ev["gate"]),
        "--observer-expected-sha256", box["observer_sha"]])
    assert code == 0 and payload["state"] == "REMOVED_WAS_ABSENT", payload
    assert not (box["strategy"] / "AKExecutionBridge.py").exists()
    record = json.loads((rollback_dir / "rollback.json").read_text(encoding="utf-8"))
    assert record["target"]["restored_sha256"] == "ABSENT"
    assert record["gate_evidence"]["gate_closed"] is True
    failed = rollback_dir / "backup" / f"AKExecutionBridge.py.failed-{box['release_sha'][:8]}.py"
    assert failed.is_file() and _sha(failed) == box["release_sha"]
    assert _sha(box["strategy"] / "AIBridge.py") == box["observer_sha"]


def test_rollback_restores_prior_bytes_after_second_deployment(tmp_path: Path):
    box = _sandbox(tmp_path)
    ev = _evidence_json(tmp_path)
    assert _run(_apply_args(box, tmp_path / "ev-a", ev))[0] == 0
    first_sha = _sha(box["strategy"] / "AKExecutionBridge.py")
    second = box["release"] / "AKExecutionBridge.py"
    second.write_text(second.read_text(encoding="utf-8") + "\n# second release marker\n",
                      encoding="utf-8")
    box["release_sha"] = _sha(second)  # the args builder re-binds to the new release
    second_sha = box["release_sha"]
    ev2 = dict(ev, stopped=_write(tmp_path / "stopped2.json", json.dumps({
        "instance": "SANDBOX-INST", "generation": "b" * 16,
        "stopped_at": "2026-09-14T02:00:00Z", "stop_log_ref": "sandbox/log2",
        "confirmed_by": "user", "reload_pending": True})))
    code, payload = _run(_apply_args(box, tmp_path / "ev-b", ev2, target=first_sha))
    assert code == 0 and payload["target_sha256"] == second_sha
    receipt = json.loads((tmp_path / "ev-b" / "deployment.json").read_text(encoding="utf-8"))
    assert receipt["prior_target"]["sha256"] == first_sha
    code, payload = _run([
        "rollback", "--strategy-directory", str(box["strategy"]),
        "--evidence-directory", str(tmp_path / "ev-b"),
        "--rollback-evidence-directory", str(tmp_path / "ev-rollback"),
        "--gate-evidence", str(ev["gate"]),
        "--observer-expected-sha256", box["observer_sha"]])
    assert code == 0 and payload["state"] == "RESTORED_PRIOR_BYTES"
    assert _sha(box["strategy"] / "AKExecutionBridge.py") == first_sha
    # drifted target cannot be silently rolled back
    (box["strategy"] / "AKExecutionBridge.py").write_text("# manual edit\n", encoding="utf-8")
    code, payload = _run([
        "rollback", "--strategy-directory", str(box["strategy"]),
        "--evidence-directory", str(tmp_path / "ev-b"),
        "--rollback-evidence-directory", str(tmp_path / "ev-rollback2"),
        "--gate-evidence", str(ev["gate"]),
        "--observer-expected-sha256", box["observer_sha"]])
    assert code == 2 and payload["code"] == "TARGET_DRIFT_SINCE_INSTALL"


def test_invalid_gate_evidence_rejected(tmp_path: Path):
    box = _sandbox(tmp_path)
    ev = _evidence_json(tmp_path)
    assert _run(_apply_args(box, tmp_path / "ev-deploy", ev))[0] == 0
    bad_gate = _write(tmp_path / "gate-bad.json", json.dumps({
        "gate_closed": False, "service_stopped_issuing": True,
        "recorded_at": "2026-09-14T01:00:00Z", "evidence_refs": ["x"]}))
    code, payload = _run([
        "rollback", "--strategy-directory", str(box["strategy"]),
        "--evidence-directory", str(tmp_path / "ev-deploy"),
        "--rollback-evidence-directory", str(tmp_path / "ev-rb"),
        "--gate-evidence", str(bad_gate),
        "--observer-expected-sha256", box["observer_sha"]])
    assert code == 2 and payload["code"] == "GATE_EVIDENCE_GATE_CLOSED_REQUIRED"
    assert (box["strategy"] / "AKExecutionBridge.py").exists()


# ---------------------------------------------------------------------------
# migration CLI (plan 10.2: measured in temp dirs; deep crash faces live in TD)
# ---------------------------------------------------------------------------

def _build_v1_journal(path: Path) -> None:
    connection = sqlite3.connect(str(path))
    try:
        connection.executescript(
            "CREATE TABLE intents (id TEXT PRIMARY KEY, account_key TEXT);"
            "CREATE TABLE legs (id TEXT PRIMARY KEY, attempt_no INTEGER,"
            " cancel_state TEXT, order_id INTEGER);"
            "CREATE TABLE events (id TEXT PRIMARY KEY, kind TEXT, applied INTEGER);"
            "CREATE TABLE trades (id TEXT PRIMARY KEY);"
            "CREATE TABLE reservations (id TEXT PRIMARY KEY, state TEXT);")
        connection.executemany("INSERT INTO intents VALUES (?,?)",
                               [("i1", "SIM_ACCOUNT_A"), ("i2", "SIM_ACCOUNT_A")])
        connection.execute("INSERT INTO legs VALUES ('l1',1,'FILLED',7)")
        connection.execute("INSERT INTO legs VALUES ('l2',0,'UNKNOWN',NULL)")
        connection.execute("INSERT INTO events VALUES ('e1','CALLBACK',0)")
        connection.execute("INSERT INTO events VALUES ('e2','CALLBACK',1)")
        connection.execute("INSERT INTO trades VALUES ('t1')")
        connection.execute("INSERT INTO reservations VALUES ('r1','HELD')")
        connection.commit()
    finally:
        connection.close()


def _migrate(argv: list[str]) -> tuple[int, dict]:
    result = subprocess.run([sys.executable, str(MIGRATE), *argv],
                            capture_output=True, text=True)
    payload = json.loads(result.stdout) if result.stdout.strip() else {}
    return result.returncode, payload


def test_migration_creates_conserved_v2_copy_and_keeps_source_readonly(tmp_path: Path):
    source = tmp_path / "v1.sqlite"
    _build_v1_journal(source)
    source_sha = _sha(source)
    output = tmp_path / "v2.sqlite"
    code, report = _migrate(["--source", str(source), "--output", str(output)])
    assert code == 0 and report["conserved"] is True
    assert report["schema"] == "v2" and report["native_enabled"] is False
    assert report["counts_before"] == report["counts_after"] == {
        "intents": 2, "legs": 2, "events": 2, "trades": 1, "reservations": 1}
    assert report["unresolved_before"] == report["unresolved_after"]
    assert _sha(source) == source_sha  # input stays read-only, byte-identical
    connection = sqlite3.connect(f"file:{output.as_posix()}?mode=ro", uri=True)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute(
            "SELECT value FROM ledger_meta WHERE key='schema'").fetchone()[0] == "v2"
    finally:
        connection.close()


def test_migration_guards(tmp_path: Path):
    source = tmp_path / "v1.sqlite"
    _build_v1_journal(source)
    output = tmp_path / "v2.sqlite"
    assert _migrate(["--source", str(tmp_path / "missing.sqlite"),
                     "--output", str(output)])[1] == {"error": "source_missing"}
    assert _migrate(["--source", str(source), "--output", str(source)])[1] == \
        {"error": "output_exists"}
    code, payload = _migrate(["--source", str(source), "--output", str(output)])
    assert code == 0
    # migrating an already-v2 journal refuses (unexpected schema)
    report = _migrate(["--source", str(output), "--output", str(tmp_path / "v3.sqlite")])
    assert report[0] == 4 and report[1]["error"] == "unexpected_schema"
    plain = tmp_path / "plain.sqlite"
    sqlite3.connect(str(plain)).close()  # no v1 tables
    assert _migrate(["--source", str(plain),
                     "--output", str(tmp_path / "v4.sqlite")])[1]["error"] == \
        "unexpected_schema"


# ---------------------------------------------------------------------------
# self-contained loading of the DEPLOYED file (H37 delivery face)
# ---------------------------------------------------------------------------

def test_deployed_file_loads_self_contained_without_repo(tmp_path: Path,
                                                         monkeypatch):
    box = _sandbox(tmp_path)
    ev = _evidence_json(tmp_path)
    assert _run(_apply_args(box, tmp_path / "ev-deploy", ev))[0] == 0
    deployed = box["strategy"] / "AKExecutionBridge.py"
    assert _sha(deployed) == box["release_sha"]

    fake = FakeNativeModule()
    monkeypatch.setitem(sys.modules, "INFINIGO", fake)
    saved_path = list(sys.path)
    saved_modules = set(sys.modules)
    import importlib.util
    sys.path[:] = [entry for entry in saved_path if "AK-trading" not in entry] \
        + [str(box["strategy"])]
    try:
        spec = importlib.util.spec_from_file_location("te_deployed_client", deployed)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = saved_path
    # no repository package was imported while loading the deployed file
    assert not ({name for name in set(sys.modules) - saved_modules
                 if name.startswith("ak_trading")})
    assert module.ADAPTER_VERSION.startswith("ne1-execution-")
    bridge = module.AKExecutionBridge()
    assert bridge.params_map.enable_trading is False
    assert bridge.params_map.execution_mode == "disabled"
    assert bridge.params_map.max_native_sends == 0
    assert bridge.counters["native_send_attempts"] == 0
    try:
        bridge.send_order({"Volume": 1})  # public bypass entry stays disabled
        raised = False
    except Exception as exc:  # ContractError standalone / repo error type under pytest
        raised = True
        assert "MODE_DISABLED" in str(exc), exc
    assert raised
    assert fake.send_calls() == []


# ---------------------------------------------------------------------------
# NE1-ARB-ARMING-001 ARM-M13: installer never touches the arming slots
# ---------------------------------------------------------------------------

def _slot_with_arming_secrets(tmp_path: Path, strategy_id: int = 7) -> Path:
    """A client store slot carrying SYNTHETIC business DBs plus the arming-v1
    slots, including a private credential with a SYNTHETIC nonce."""
    slot = tmp_path / "strategy" / ".ak_execution" / f"strategy-{strategy_id}"
    slot.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(slot / "receipts.db"))
    connection.execute("CREATE TABLE receipts (id TEXT PRIMARY KEY)")
    connection.execute("INSERT INTO receipts VALUES ('r-arm-1')")
    connection.commit()
    connection.close()
    connection = sqlite3.connect(str(slot / "outbox.db"))
    connection.execute(
        "CREATE TABLE events (id TEXT PRIMARY KEY, acked INTEGER, body TEXT)")
    connection.execute("INSERT INTO events VALUES ('e-arm-1', 1, '{}')")
    connection.commit()
    connection.close()
    _write(slot / "arming.json", json.dumps({
        "schema_version": "ne1_client_arming_v1-SYNTHETIC",
        "arm_id": "a" * 32, "challenge": SYNTHETIC_ARMING_NONCE,
        "action_source": "USER_LOCAL_CONSOLE"}))
    _write(slot / "arming-challenge.json", json.dumps({
        "schema_version": "ne1_arming_challenge_v1-SYNTHETIC",
        "challenge": SYNTHETIC_ARMING_NONCE}))
    _write(slot / "arming-state.json", json.dumps({
        "schema_version": "ne1_arming_state_v1-SYNTHETIC",
        "state": "ARMED", "consumed": True}))
    _write(slot / "disarm.json", json.dumps({
        "schema_version": "ne1_client_disarm_v1-SYNTHETIC",
        "reason": "USER_DISARM"}))
    return slot


def test_apply_never_backs_up_or_touches_arming_slots(tmp_path: Path):
    box = _sandbox(tmp_path)
    ev = _evidence_json(tmp_path)
    slot = _slot_with_arming_secrets(tmp_path)
    before = {name: (slot / name).read_bytes() for name in
              ("arming.json", "arming-challenge.json", "arming-state.json",
               "disarm.json")}
    code, payload = _run(_apply_args(box, tmp_path / "ev-arm", ev,
                                     client_store=str(slot),
                                     strategy_id=7))
    assert code == 0 and payload["status"] == "deployed", payload
    receipt = json.loads((tmp_path / "ev-arm" / "deployment.json")
                         .read_text(encoding="utf-8"))
    assert receipt["tool_version"] == INSTALLER_VERSION
    store_record = receipt["backups"]["client_store"]
    # the backup tree contains ONLY the two business databases (hard guard)
    backup_store = tmp_path / "ev-arm" / "backup" / "client-store"
    assert sorted(path.name for path in backup_store.iterdir()) == \
        ["outbox.db", "receipts.db"]
    assert store_record["files"]["receipts.db"]["receipt_count"] == 1
    assert "arming.json" in store_record["excluded_slots"]
    # arming slots stay byte-identical; the SYNTHETIC nonce never leaks into
    # any installer evidence file
    for name, payload_bytes in before.items():
        assert (slot / name).read_bytes() == payload_bytes
    for path in (tmp_path / "ev-arm").rglob("*"):
        if path.is_file():
            assert SYNTHETIC_ARMING_NONCE.encode("ascii") \
                not in path.read_bytes(), path


def test_rollback_leaves_arming_slots_and_facts_intact(tmp_path: Path):
    box = _sandbox(tmp_path)
    ev = _evidence_json(tmp_path)
    assert _run(_apply_args(box, tmp_path / "ev-deploy", ev))[0] == 0
    slot = _slot_with_arming_secrets(tmp_path)
    before = {name: (slot / name).read_bytes() for name in
              ("arming.json", "arming-challenge.json", "arming-state.json",
               "disarm.json")}
    receipts_before = (slot / "receipts.db").read_bytes()
    code, payload = _run([
        "rollback", "--strategy-directory", str(box["strategy"]),
        "--evidence-directory", str(tmp_path / "ev-deploy"),
        "--rollback-evidence-directory", str(tmp_path / "ev-rollback"),
        "--gate-evidence", str(ev["gate"]),
        "--client-store", str(slot),
        "--observer-expected-sha256", box["observer_sha"]])
    assert code == 0 and payload["state"] == "REMOVED_WAS_ABSENT", payload
    for name, payload_bytes in before.items():
        assert (slot / name).read_bytes() == payload_bytes
    assert (slot / "receipts.db").read_bytes() == receipts_before
    for path in (tmp_path / "ev-rollback").rglob("*"):
        if path.is_file():
            assert SYNTHETIC_ARMING_NONCE.encode("ascii") \
                not in path.read_bytes(), path
