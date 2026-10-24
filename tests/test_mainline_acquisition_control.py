"""OPT-03 behavior regressions. All resources/PIDs are SYNTHETIC or test children.

Imports always resolve from this candidate via its unchanged offline launcher.
No SDK, database (including SQLite), account store, or network is used.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from ak_trading.acquisition import cli, contracts as c, scheduler as sch
from ak_trading.acquisition import tq_adapter as tq
from ak_trading.testing import offline_guard

ROOT = Path(__file__).resolve().parents[1]
NOW = c.iso_instant_to_nano("2026-09-18T16:00:00+08:00")
SECOND = c.NANOSECOND


@pytest.fixture(autouse=True)
def forbid_real_factories(monkeypatch):
    calls = {"adapter": 0, "engine": 0}

    def adapter(*args, **kwargs):
        calls["adapter"] += 1
        raise AssertionError("real adapter factory must remain unreachable")

    def engine(*args, **kwargs):
        calls["engine"] += 1
        raise AssertionError("real engine factory must remain unreachable")

    monkeypatch.setattr(tq, "RealTqApiSession", adapter)
    monkeypatch.setattr(cli.storage_mod, "build_project_engine", engine)
    yield calls
    assert calls == {"adapter": 0, "engine": 0}


@pytest.fixture
def policy():
    return c.Policy.from_file(ROOT / "config/execution-policy.frozen.json").validate()


def rss_module(monkeypatch, value=64 * 1024**2, error=None):
    """Replace observations only; never inspect a real account/process resource."""
    observations = []

    def memory_info():
        observations.append("SYNTHETIC_RSS")
        if error:
            raise error
        return SimpleNamespace(rss=value)

    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(
        Process=lambda: SimpleNamespace(memory_info=memory_info),
        pid_exists=lambda pid: False,
    ))
    return observations


def small_plan(policy):
    return {
        "plan_id": "SYNTHETIC-OPT-03", "synthetic": True,
        "policy_path": str(ROOT / "config/execution-policy.frozen.json"),
        "policy_sha256": policy.sha256, "cutoff_nano": str(NOW),
        "window": {"start": "2026-09-01", "end": "2026-09-18"},
        "tasks": [{"product_key": "SHFE.rb", "priority": "P0",
                   "duration_seconds": 60, "concrete_symbols": ["SHFE.rb2601"]}],
    }


def test_candidate_import_receipt():
    assert Path(cli.__file__).resolve() == ROOT / "src/ak_trading/acquisition/cli.py"
    assert Path(sch.__file__).resolve() == ROOT / "src/ak_trading/acquisition/scheduler.py"
    print(json.dumps({"candidate_root": str(ROOT), "cli": cli.__file__,
                      "scheduler": sch.__file__, "inputs": "SYNTHETIC"}))


def gated_cli(monkeypatch, tmp_path, policy):
    """A fully hash-bound SYNTHETIC authorization, not a run authorization."""
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(small_plan(policy)), encoding="utf-8")
    manifest = cli.compute_code_manifest(ROOT / "src/ak_trading/acquisition")["manifest_sha256"]
    seal_path = tmp_path / "seal.json"
    seal_path.write_text(json.dumps({
        "review_id": "SYNTHETIC-TEST-ONLY", "verdict": "PASS",
        "scope": {"covers_package": "ak_acquisition_w2", "code_manifest_sha256": manifest},
    }), encoding="utf-8")
    output = tmp_path / "run"
    output.mkdir()
    marker = tmp_path / "synthetic-health-marker"
    marker.write_text("SYNTHETIC", encoding="utf-8")
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps({
        "confirmation_ref": "SYNTHETIC TEST ONLY", "approver": "HUMAN_USER",
        "confirmed_at_nano": str(NOW - SECOND),
        "scope_policy_sha256": policy.sha256,
        "scope_review_seal_sha256": c.sha256_file(seal_path),
        "scope_plan_sha256": c.sha256_file(plan_path),
        "scope_code_manifest_sha256": manifest,
        "output_root": str(output), "db_target_non_sensitive": "synthetic.invalid:5432/test",
        "session_window_start": "2026-09-18T15:00:00+08:00",
        "session_window_end": "2026-09-18T23:00:00+08:00",
        "max_serial_attempts": 24, "max_wall_seconds": 1800,
        "allow_subsequent_batches": False,
        "protected_paths": [str(tmp_path / "protected")],
        "protected_db_objects": ["SYNTHETIC_PROTECTED"],
        "health_checks": [{"name": "SYNTHETIC", "kind": "file_exists", "target": str(marker)}],
    }), encoding="utf-8")
    monkeypatch.setenv("TQ_USER", "SYNTHETIC_ONLY")
    monkeypatch.setenv("TQ_PASS", "SYNTHETIC_ONLY")
    monkeypatch.setenv("AK_DATABASE_URL", "SYNTHETIC_NOT_A_DATABASE_URL")
    monkeypatch.setattr(cli, "_now_nano", lambda: NOW)
    import shutil
    monkeypatch.setattr(shutil, "disk_usage", lambda path: SimpleNamespace(free=200 * 1024**3))
    monkeypatch.setattr(sch.IsolationGuard, "set_below_normal_priority", lambda self: "SYNTHETIC_NO_CHANGE")
    executed = []
    monkeypatch.setattr(cli, "execute_run", lambda **kwargs: executed.append(kwargs) or cli.EXIT_OK)
    return argparse.Namespace(plan=str(plan_path), authorization=str(auth_path),
                              review_seal=str(seal_path)), executed


def test_rss_legal_full_gate_reaches_fake_execute(monkeypatch, tmp_path, policy):
    observations = rss_module(monkeypatch, value=policy.budget["process_rss_bytes_max"])
    args, executed = gated_cli(monkeypatch, tmp_path, policy)
    assert policy.budget["process_rss_bytes_max"] == 1024**3
    assert cli.cmd_run(args) == cli.EXIT_OK
    assert len(executed) == 1 and len(observations) == 1
    assert executed[0]["policy"].budget == policy.budget


@pytest.mark.parametrize("mode", ["over", "access", "none", "negative", "nan", "bool", "missing"])
def test_rss_unknown_or_over_refuses_before_execute(monkeypatch, tmp_path, policy, mode):
    values = {"over": 1024**3 + 1, "access": 1, "none": None,
              "negative": -1, "nan": float("nan"), "bool": True, "missing": 1}
    rss_module(monkeypatch, value=values[mode], error=PermissionError("SYNTHETIC") if mode == "access" else None)
    if mode == "missing":
        monkeypatch.setitem(sys.modules, "psutil", None)
    args, executed = gated_cli(monkeypatch, tmp_path, policy)
    assert cli.cmd_run(args) == cli.EXIT_GATE_REFUSED
    assert executed == []
    ledger = sch.BudgetLedger(policy)
    with pytest.raises(sch.IsolationRefused):
        ledger.check_rss_bytes()


def test_both_rss_entrypoints_use_the_same_observation(monkeypatch, policy):
    observations = rss_module(monkeypatch, value=123456)
    guard = sch.IsolationGuard(sch.ProtectionList((), ()), minimum_free_bytes=1, batch_peak_bytes=1)
    assert guard.check_rss_bytes(policy.budget["process_rss_bytes_max"]) == 123456
    assert sch.BudgetLedger(policy).check_rss_bytes() == 123456
    assert len(observations) == 2


class FailureRun:
    """Real execute/capture/retry/lease with failing in-memory adapter; no DB."""
    def __init__(self, monkeypatch, tmp_path, policy):
        self.now = NOW
        self.output = tmp_path / "run"
        self.plan = small_plan(policy)
        self.policy = policy
        self.requests = []
        self.serials = []
        self.factories = []
        self.closed = []
        self.after_request = lambda: None
        self.auth = SimpleNamespace()
        rss_module(monkeypatch)
        monkeypatch.setattr(cli.storage_mod, "ensure_schema_and_tables", lambda engine: None)
        monkeypatch.setattr(cli.storage_mod, "install_immutability_guard", lambda engine: None)
        original_record = cli._record_failed_attempt_evidence

        def record(root, request, outcome):
            self.requests.append(request)
            original_record(root, request, outcome)
        monkeypatch.setattr(cli, "_record_failed_attempt_evidence", record)

    def adapter(self):
        self.factories.append("fake-adapter")
        run = self

        class Adapter:
            sdk_version = "SYNTHETIC_NO_SDK"

            def get_kline_serial(self, symbol, duration, **kwargs):
                run.serials.append((symbol, duration))
                run.after_request()
                raise tq.NotReadyTimeoutError()

            def close(self):
                run.closed.append("adapter")
        return Adapter()

    def engine(self):
        self.factories.append("fake-engine")
        return SimpleNamespace(dispose=lambda: self.closed.append("engine"))

    def run(self, *, resume=False, plan=None):
        return cli.execute_run(plan=self.plan if plan is None else plan,
                               policy=self.policy, auth=self.auth, output_root=self.output,
                               adapter_factory=self.adapter, engine_factory=self.engine,
                               clock=lambda: self.now, resume=resume)


@pytest.mark.parametrize("resume", [False, True])
def test_real_failure_restart_backoff_and_attempt_ids(monkeypatch, tmp_path, policy, resume):
    run = FailureRun(monkeypatch, tmp_path, policy)
    assert run.run() == cli.EXIT_OK
    assert len(run.serials) == 2
    assert {r.attempt_id for r in run.requests} == {
        "attempt-SHFE.rb2601|60-1", "attempt-SHFE.rb2601|86400-1"}
    for attempt, backoff in enumerate((60, 300, 900), start=2):
        count = len(run.serials)
        run.now += (backoff - 1) * SECOND
        assert run.run(resume=resume) == cli.EXIT_OK
        assert len(run.serials) == count
        run.now += SECOND
        assert run.run(resume=resume) == cli.EXIT_OK
        assert len(run.serials) == count + 2
        assert {r.attempt_id for r in run.requests[-2:]} == {
            f"attempt-SHFE.rb2601|60-{attempt}", f"attempt-SHFE.rb2601|86400-{attempt}"}
    run.now += 10_000 * SECOND
    assert run.run(resume=resume) == cli.EXIT_OK
    assert len(run.serials) == 8
    store = sch.RunStateStore(run.output)
    assert store.task_state("SHFE.rb2601|60").attempts == 4
    state = json.loads(store.path.read_text(encoding="utf-8"))
    assert set(state["tasks"]) == {"SHFE.rb2601|60", "SHFE.rb2601|86400"}
    assert c.canonical_sha256(run.plan) in json.dumps(store.checkpoints())
    evidence = list((run.output / "products/SHFE_rb").glob("*requests.jsonl"))
    assert len(evidence) == 8
    assert len({json.loads(p.read_text(encoding="utf-8"))["attempt_id"] for p in evidence}) == 8


@pytest.mark.parametrize("changed", ["window", "cutoff_nano", "policy_sha256"])
def test_changed_plan_refused_preserves_state_and_zero_factories(monkeypatch, tmp_path, policy, changed):
    run = FailureRun(monkeypatch, tmp_path, policy)
    assert run.run() == cli.EXIT_OK
    state = (run.output / "run-state.json").read_bytes()
    altered = copy.deepcopy(run.plan)
    altered[changed] = "SYNTHETIC_DIFFERENT"
    run.factories.clear()
    run.now += 10_000 * SECOND
    assert run.run(plan=altered) == cli.EXIT_GATE_REFUSED
    assert run.factories == []
    assert (run.output / "run-state.json").read_bytes() == state


def test_plan_binding_is_canonical_not_mapping_insertion_order(monkeypatch, tmp_path, policy):
    run = FailureRun(monkeypatch, tmp_path, policy)
    assert run.run() == cli.EXIT_OK
    reordered = dict(reversed(list(run.plan.items())))
    run.now += 60 * SECOND
    assert run.run(plan=reordered, resume=True) == cli.EXIT_OK
    assert len(run.serials) == 4


@pytest.mark.parametrize("kind", ["phase-key", "canonical-key", "checkpoint", "corrupt"])
def test_legacy_unbound_nonempty_state_is_never_reset(monkeypatch, tmp_path, policy, kind):
    run = FailureRun(monkeypatch, tmp_path, policy)
    run.output.mkdir()
    payload = {"tasks": {}, "checkpoints": {}}
    if kind in {"phase-key", "canonical-key"}:
        key = ("PILOT_SAMPLE|" if kind == "phase-key" else "") + "SHFE.rb2601|60"
        payload["tasks"][key] = sch.TaskAttemptState(attempts=2).to_json()
    else:
        payload["checkpoints"]["SYNTHETIC_OLD"] = {"x": 1}
    original = b"invalid-json" if kind == "corrupt" else json.dumps(payload).encode()
    state_path = run.output / "run-state.json"
    state_path.write_bytes(original)
    assert run.run(resume=False) == cli.EXIT_GATE_REFUSED
    assert run.factories == [] and run.serials == []
    assert state_path.read_bytes() == original


@pytest.mark.parametrize("terminal", [c.TaskState.INGEST_VERIFIED, c.TaskState.ACCEPTED])
def test_terminal_success_is_not_reissued_on_restart(monkeypatch, tmp_path, policy, terminal):
    run = FailureRun(monkeypatch, tmp_path, policy)
    assert run.run() == cli.EXIT_OK
    store = sch.RunStateStore(run.output)
    gate = sch.RetryGate(store, lambda: run.now)
    for duration in (60, 86400):
        gate.record_success(f"SHFE.rb2601|{duration}", terminal)
    state_before = store.path.read_bytes()
    run.now += 100_000 * SECOND
    assert run.run(resume=False) == cli.EXIT_OK
    assert len(run.serials) == 2
    assert store.path.read_bytes() == state_before


def test_execute_resource_failure_precedes_factories(monkeypatch, tmp_path, policy):
    run = FailureRun(monkeypatch, tmp_path, policy)
    rss_module(monkeypatch, value=1024**3 + 1)
    assert run.run() == cli.EXIT_GATE_REFUSED
    assert run.factories == [] and run.serials == []


def test_lost_token_stops_next_request_in_same_group(monkeypatch, tmp_path, policy):
    run = FailureRun(monkeypatch, tmp_path, policy)

    def replace_token():
        path = run.output / "task-lease.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["owner_token"] = "SYNTHETIC_SUCCESSOR"
        path.write_text(json.dumps(record), encoding="utf-8")
    run.after_request = replace_token
    assert run.run() == cli.EXIT_FAILURE
    assert len(run.serials) == 1
    assert run.closed == ["adapter", "engine"]
    assert json.loads((run.output / "task-lease.json").read_text(encoding="utf-8"))["owner_token"] == "SYNTHETIC_SUCCESSOR"


def write_lease(tmp_path, *, pid=111, heartbeat=NOW, token="SYNTHETIC_OLD"):
    payload = {"holder_pid": pid, "heartbeat_nano": str(heartbeat), "holder_name": "SYNTHETIC"}
    if token is not None:
        payload["owner_token"] = token
    path = tmp_path / "task-lease.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.mark.parametrize("alive", [True, None])
@pytest.mark.parametrize("legacy", [False, True])
def test_301_second_live_or_unknown_metadata_cannot_be_stolen(monkeypatch, tmp_path, alive, legacy):
    path = write_lease(tmp_path, token=None if legacy else "SYNTHETIC_OLD")
    before = path.read_bytes()
    monkeypatch.setattr(sch, "_pid_alive", lambda pid: alive)
    lease = sch.TaskLease(tmp_path, "SYNTHETIC_NEW")
    try:
        assert lease.acquire(222, NOW + 301 * SECOND) is False
        assert path.read_bytes() == before
    finally:
        lease.release(222)


@pytest.mark.parametrize("elapsed,expected", [(299, False), (300, True), (301, True)])
@pytest.mark.parametrize("legacy", [False, True])
def test_only_dead_and_expired_metadata_recovers(monkeypatch, tmp_path, elapsed, expected, legacy):
    path = write_lease(tmp_path, token=None if legacy else "SYNTHETIC_OLD")
    before = path.read_bytes()
    monkeypatch.setattr(sch, "_pid_alive", lambda pid: False)
    lease = sch.TaskLease(tmp_path, "SYNTHETIC_NEW")
    try:
        assert lease.acquire(222, NOW + elapsed * SECOND) is expected
        if expected:
            data = json.loads(path.read_text(encoding="utf-8"))
            assert data["holder_pid"] == 222 and data["owner_token"] != "SYNTHETIC_OLD"
        else:
            assert path.read_bytes() == before
    finally:
        lease.release(222)


def test_same_object_idempotent_but_second_same_pid_refused(tmp_path):
    first, second = sch.TaskLease(tmp_path, "SYNTHETIC_A"), sch.TaskLease(tmp_path, "SYNTHETIC_B")
    try:
        assert first.acquire(111, NOW)
        token = json.loads(first.path.read_text(encoding="utf-8"))["owner_token"]
        assert first.acquire(111, NOW + SECOND)
        assert json.loads(first.path.read_text(encoding="utf-8"))["owner_token"] == token
        assert not second.acquire(111, NOW + 301 * SECOND)
        first.release(111)
        assert (tmp_path / "task-lease.lock").stat().st_size >= 1
        assert second.acquire(111, NOW + 302 * SECOND)
    finally:
        first.release(111)
        second.release(111)


def test_old_token_cannot_heartbeat_or_release_successor(tmp_path):
    first, second = sch.TaskLease(tmp_path, "SYNTHETIC_A"), sch.TaskLease(tmp_path, "SYNTHETIC_B")
    assert first.acquire(111, NOW)
    first.release(111)
    assert second.acquire(111, NOW + SECOND)
    successor = second.path.read_bytes()
    try:
        with pytest.raises(c.ContractError, match="lease lost"):
            first.heartbeat(111, NOW + 2 * SECOND)
        first.release(111)
        assert second.path.read_bytes() == successor
        second.heartbeat(111, NOW + 3 * SECOND)
    finally:
        second.release(111)


@pytest.mark.parametrize("contents", ["{", "{}", "[]", '{"holder_pid":0,"heartbeat_nano":"1"}',
                                      '{"holder_pid":111,"heartbeat_nano":"1","owner_token":""}'])
def test_corrupt_metadata_refuses_without_overwrite(monkeypatch, tmp_path, contents):
    path = tmp_path / "task-lease.json"
    path.write_text(contents, encoding="utf-8")
    monkeypatch.setattr(sch, "_pid_alive", lambda pid: False)
    lease = sch.TaskLease(tmp_path, "SYNTHETIC")
    try:
        assert not lease.acquire(222, NOW)
        assert path.read_text(encoding="utf-8") == contents
    finally:
        lease.release(222)


@pytest.mark.parametrize("failure", ["missing", "denied"])
def test_pid_observation_uncertainty_is_unknown(monkeypatch, failure):
    if failure == "missing":
        monkeypatch.setitem(sys.modules, "psutil", None)
    else:
        def denied(pid):
            raise PermissionError("SYNTHETIC_NO_PROCESS_ACCESS")
        monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(pid_exists=denied))
    assert sch._pid_alive(111) is None


@pytest.mark.parametrize("operation", ["fsync", "replace"])
def test_metadata_failure_closes_lock_and_cleans_owned_temp(monkeypatch, tmp_path, operation):
    first = sch.TaskLease(tmp_path, "SYNTHETIC_A")
    # Pre-create the stable lock byte to fault metadata rather than lock setup.
    (tmp_path / "task-lease.lock").write_bytes(b"0")
    original = getattr(sch.os, operation)

    def failed(*args, **kwargs):
        raise OSError("SYNTHETIC_METADATA_FAILURE")
    monkeypatch.setattr(sch.os, operation, failed)
    assert not first.acquire(111, NOW)
    assert not first.path.exists()
    assert not list(tmp_path.glob("*.tmp"))
    monkeypatch.setattr(sch.os, operation, original)
    second = sch.TaskLease(tmp_path, "SYNTHETIC_B")
    try:
        assert second.acquire(222, NOW)
    finally:
        second.release(222)


def test_wrong_pid_heartbeat_preserves_metadata_and_closes_handle(tmp_path):
    first = sch.TaskLease(tmp_path, "SYNTHETIC_A")
    assert first.acquire(111, NOW)
    original = first.path.read_bytes()
    with pytest.raises(c.ContractError, match="lease lost"):
        first.heartbeat(222, NOW + SECOND)
    first.release(222)
    assert first.path.read_bytes() == original


def test_threads_have_exactly_one_holder(tmp_path):
    barrier = threading.Barrier(2)
    release = threading.Event()
    results = []
    mutex = threading.Lock()
    both = threading.Event()

    def contender(index):
        lease = sch.TaskLease(tmp_path, f"SYNTHETIC_THREAD_{index}")
        try:
            barrier.wait(timeout=10)
            won = lease.acquire(os.getpid(), NOW)
            with mutex:
                results.append(won)
                if len(results) == 2:
                    both.set()
            if won:
                assert release.wait(timeout=10)
        finally:
            lease.release(os.getpid())
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(contender, i) for i in range(2)]
        try:
            assert both.wait(timeout=10)
            assert sorted(results) == [False, True]
        finally:
            release.set()
        for future in futures:
            future.result(timeout=10)


CHILD = r'''
import json, os, sys, time
from pathlib import Path
from ak_trading.testing import offline_guard
offline_guard.require_installed()
from ak_trading.acquisition import scheduler as sch
root, now = Path(sys.argv[1]), int(sys.argv[2])
lease = sch.TaskLease(root, "SYNTHETIC_CHILD")
print("READY", flush=True)
attempts = 0
while True:
    line = sys.stdin.readline().strip()
    if line == "RELEASE":
        break
    assert line == "GO"
    started = time.monotonic()
    won = lease.acquire(os.getpid(), now)
    attempts += 1
    print(json.dumps({"acquired": won, "pid": os.getpid(), "module": sch.__file__,
                      "attempt": attempts,
                      "acquire_ms": round((time.monotonic() - started) * 1000, 3)}), flush=True)
lease.release(os.getpid())
'''

# MH04: the child requires the offline guard, so the parent must hand it the
# same bootstrap contract scripts/test_offline.py provides (guard installation
# via PYTHONPATH + sitecustomize). Plain pytest must fail diagnosably without
# weakening the child, and Windows process startup gets a larger but bounded
# watchdog (60s) than other platforms (30s); the non-blocking acquire itself
# must stay fast.
READY_TIMEOUT_SECONDS = 60.0 if sys.platform == "win32" else 30.0
RESULT_TIMEOUT_SECONDS = 30.0
RELEASE_TIMEOUT_SECONDS = 30.0
TERMINATE_GRACE_SECONDS = 5.0

CHILD_BOOTSTRAP = (
    "import os, sys\n"
    "try:\n"
    "    from ak_trading.testing import offline_guard\n"
    "    offline_guard.install({empty_root!r}, {repo_root!r}, [{fixture_root!r}])\n"
    "except BaseException:\n"
    "    sys.stderr.write('Offline guard startup failed; refusing to run.\\n')\n"
    "    os._exit(4)\n"
)


def guarded_child_environment(tmp_path):
    """Mirror the canonical offline launcher bootstrap for real child processes.

    The child installs an isolated guard (empty settings root, fixture roots
    limited to this test's tmp_path) before require_installed(), exactly like
    scripts/test_offline.py does for the parent, so the two-OS-process lock
    protocol runs guarded under any parent entry point.
    """
    bootstrap = tmp_path / "bootstrap"
    empty_root = tmp_path / "empty-settings"
    bootstrap.mkdir(exist_ok=True)  # successors within one test reuse the bootstrap
    empty_root.mkdir(exist_ok=True)
    (bootstrap / "sitecustomize.py").write_text(
        CHILD_BOOTSTRAP.format(empty_root=str(empty_root), repo_root=str(ROOT),
                               fixture_root=str(tmp_path)),
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join([str(bootstrap), str(ROOT / "src")])
    return environment


def _drain_stream(stream, sink, finished):
    """Independent per-stream consumption: stderr can never fill its pipe and
    block the child, and stdout lines are captured in order."""
    for line in iter(stream.readline, ""):
        sink.append(line)
    finished.set()


class ChildProtocol:
    """Bounded READY -> GO -> result -> RELEASE protocol for one test child."""

    def __init__(self, tmp_path, index, script=CHILD, now=None, environment=None):
        # spawned_at is parent-side creation accounting for every child PID
        # this file creates (used by the Windows delayed-start stress evidence).
        self.spawned_at_epoch = time.time()
        self.child = subprocess.Popen(
            [sys.executable, "-B", "-X", "utf8", "-c", script,
             str(tmp_path), str(NOW if now is None else now)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8",
            env=guarded_child_environment(tmp_path) if environment is None else environment,
        )
        self.out: list[str] = []
        self.err: list[str] = []
        self.finished: list[threading.Event] = []
        self.threads: list[threading.Thread] = []
        for stream, sink in ((self.child.stdout, self.out), (self.child.stderr, self.err)):
            done = threading.Event()
            thread = threading.Thread(target=_drain_stream, args=(stream, sink, done), daemon=True)
            thread.start()
            self.threads.append(thread)
            self.finished.append(done)
        self.evidence_path = tmp_path / f"child-{index}"

    def wait_line(self, count, timeout, what):
        deadline = time.monotonic() + timeout
        while len(self.out) < count:
            status = self.child.poll()
            if status is not None:
                raise AssertionError(
                    f"child exited rc={status} before {what}; first cause stderr="
                    f"{''.join(self.err)}; stdout so far={self.out}")
            if all(done.is_set() for done in self.finished):
                # Stream EOF can be observed before Windows reports the exit
                # code; give the process handle a bounded grace so the
                # first-cause diagnosis carries the real rc instead of a
                # racy rc=None.
                exit_grace = time.monotonic() + 2.0
                status = self.child.poll()
                while status is None and time.monotonic() < exit_grace:
                    time.sleep(0.02)
                    status = self.child.poll()
                if status is not None:
                    raise AssertionError(
                        f"child exited rc={status} before {what}; first cause stderr="
                        f"{''.join(self.err)}; stdout so far={self.out}")
                raise AssertionError(
                    f"child streams closed before {what}; rc={self.child.poll()}; "
                    f"first cause stderr={''.join(self.err)}; stdout={self.out}")
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"watchdog ({timeout}s) exhausted waiting for {what}; rc={self.child.poll()}; "
                    f"stdout={self.out}; stderr={''.join(self.err)}")
            time.sleep(0.02)

    def send(self, line, cleanup_notes=None):
        try:
            self.child.stdin.write(line + "\n")
            self.child.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            if cleanup_notes is not None:
                # Cleanup-only evidence: a dead child's stdin must never mask
                # the primary failure (the old Errno 22 second-exception trap).
                cleanup_notes.append(f"stdin {line!r} to dead child failed: {exc!r}")
            else:
                raise AssertionError(
                    f"child stdin write {line!r} failed: {exc!r}; rc={self.child.poll()}; "
                    f"stderr={''.join(self.err)}") from exc

    def finish(self, timeout, notes):
        if self.child.poll() is None:
            self.send("RELEASE", cleanup_notes=notes)
        try:
            self.child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            notes.append(f"terminating unresponsive child pid={self.child.pid}")
            self.child.terminate()
            try:
                self.child.wait(timeout=TERMINATE_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                notes.append(f"killing child pid={self.child.pid}")
                self.child.kill()
                self.child.wait(timeout=TERMINATE_GRACE_SECONDS)
        try:
            self.child.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass
        for thread in self.threads:
            thread.join(timeout=TERMINATE_GRACE_SECONDS)
        self.evidence_path.with_suffix(".stdout.txt").write_text("".join(self.out), encoding="utf-8")
        self.evidence_path.with_suffix(".stderr.txt").write_text("".join(self.err), encoding="utf-8")
        self.evidence_path.with_suffix(".json").write_text(json.dumps({
            "SYNTHETIC": True,
            "pid": self.child.pid,
            "spawned_at_epoch": self.spawned_at_epoch,
            "returncode": self.child.returncode,
            "cleanup_notes": notes,
        }, indent=1), encoding="utf-8")


def test_processes_hold_nonblocking_lock_until_release(tmp_path):
    protocols = [ChildProtocol(tmp_path, index) for index in range(2)]
    cleanup_notes: list[str] = []
    try:
        for protocol in protocols:
            protocol.wait_line(1, READY_TIMEOUT_SECONDS, "READY")
        assert [protocol.out[0].strip() for protocol in protocols] == ["READY", "READY"]
        for protocol in protocols:
            protocol.send("GO")
        for protocol in protocols:
            protocol.wait_line(2, RESULT_TIMEOUT_SECONDS, "acquire result")
        records = [json.loads(protocol.out[1]) for protocol in protocols]
        assert sorted(r["acquired"] for r in records) == [False, True]
        assert all(Path(r["module"]).resolve() == ROOT / "src/ak_trading/acquisition/scheduler.py" for r in records)
        loser = protocols[0] if records[0]["acquired"] is False else protocols[1]
        holder = protocols[1] if records[0]["acquired"] is False else protocols[0]
        # Mutex must persist while the holder stays alive: the loser retries the
        # real non-blocking acquire (second OS-level attempt) and is refused again.
        loser.send("GO")
        loser.wait_line(3, RESULT_TIMEOUT_SECONDS, "loser retry while holder alive")
        retry = json.loads(loser.out[2])
        assert retry["acquired"] is False and retry["attempt"] == 2
        # Same lease object and PID re-acquire idempotently (heartbeat path).
        holder.send("GO")
        holder.wait_line(3, RESULT_TIMEOUT_SECONDS, "holder idempotent re-acquire")
        assert json.loads(holder.out[2])["acquired"] is True
        print(json.dumps({"SYNTHETIC_child_results": records, "SYNTHETIC_retry": retry}))
    finally:
        for protocol in protocols:
            protocol.finish(RELEASE_TIMEOUT_SECONDS, cleanup_notes)
        if cleanup_notes:
            print(json.dumps({"SYNTHETIC_cleanup_notes": cleanup_notes}))
    for index, protocol in enumerate(protocols):
        assert protocol.child.returncode == 0, (
            f"child {index} rc={protocol.child.returncode}; "
            f"stderr={''.join(protocol.err)}; cleanup={cleanup_notes}")
        assert protocol.err == [], f"child {index} stderr: {''.join(protocol.err)}"
    # After the holder's explicit release both children exited, so the freed
    # key must be acquirable again from a fresh OS process.
    successor = ChildProtocol(tmp_path, 2)
    try:
        successor.wait_line(1, READY_TIMEOUT_SECONDS, "READY")
        successor.send("GO")
        successor.wait_line(2, RESULT_TIMEOUT_SECONDS, "re-acquire result")
        assert json.loads(successor.out[1])["acquired"] is True
    finally:
        successor.finish(RELEASE_TIMEOUT_SECONDS, cleanup_notes)
    assert successor.child.returncode == 0, ''.join(successor.err)


CHILD_DIES_BEFORE_READY = (
    "import sys\n"
    "sys.stderr.write('SYNTHETIC_BOOT_FAILURE\\n')\n"
    "sys.exit(2)\n"
)


def test_child_failure_before_ready_reports_first_cause_fast(tmp_path):
    protocol = ChildProtocol(tmp_path, 0, script=CHILD_DIES_BEFORE_READY)
    started = time.monotonic()
    try:
        with pytest.raises(AssertionError, match="SYNTHETIC_BOOT_FAILURE") as failure:
            protocol.wait_line(1, READY_TIMEOUT_SECONDS, "READY")
    finally:
        protocol.finish(RELEASE_TIMEOUT_SECONDS, [])
    # The protocol must fail on the child's own stderr and exit code, not by
    # waiting out the whole startup watchdog.
    assert time.monotonic() - started < READY_TIMEOUT_SECONDS
    assert "rc=2" in str(failure.value)
    assert protocol.child.returncode == 2
    assert protocol.child.poll() is not None  # no leaked process


CHILD_LATE_READY = (
    "import sys, time\n"
    "time.sleep(30)\n"
    "print('READY', flush=True)\n"
)


def test_hanging_child_before_ready_hits_bounded_watchdog(tmp_path):
    protocol = ChildProtocol(tmp_path, 0, script=CHILD_LATE_READY)
    started = time.monotonic()
    try:
        with pytest.raises(AssertionError, match="watchdog"):
            protocol.wait_line(1, 0.5, "READY")
    finally:
        protocol.finish(RELEASE_TIMEOUT_SECONDS, [])
    assert time.monotonic() - started < READY_TIMEOUT_SECONDS
    assert protocol.child.poll() is not None  # bounded cleanup, no leak


CHILD_HANGS_AFTER_READY = (
    "import sys, time\n"
    "print('READY', flush=True)\n"
    "assert sys.stdin.readline().strip() == 'GO'\n"
    "time.sleep(30)\n"
)


def test_unresponsive_child_after_go_fails_with_evidence_and_bounded_cleanup(tmp_path):
    protocol = ChildProtocol(tmp_path, 0, script=CHILD_HANGS_AFTER_READY)
    notes: list[str] = []
    try:
        protocol.wait_line(1, READY_TIMEOUT_SECONDS, "READY")
        protocol.send("GO")
        started = time.monotonic()
        with pytest.raises(AssertionError, match="watchdog"):
            protocol.wait_line(2, 3.0, "acquire result")
        assert time.monotonic() - started < RESULT_TIMEOUT_SECONDS
    finally:
        # Short release budget so this test exercises the forced-termination
        # path itself (the main test keeps the full platform budget).
        protocol.finish(2.0, notes)
    assert protocol.child.poll() is not None  # terminated, no leaked process
    assert protocol.child.returncode != 0
    assert notes, "cleanup must record the forced termination"


CHILD_HOLDER_DIES = (
    "import json, os, sys\n"
    "from pathlib import Path\n"
    "from ak_trading.testing import offline_guard\n"
    "offline_guard.require_installed()\n"
    "from ak_trading.acquisition import scheduler as sch\n"
    "root, now = Path(sys.argv[1]), int(sys.argv[2])\n"
    "lease = sch.TaskLease(root, 'SYNTHETIC_DYING_HOLDER')\n"
    "print('READY', flush=True)\n"
    "assert sys.stdin.readline().strip() == 'GO'\n"
    "won = lease.acquire(os.getpid(), now)\n"
    "print(json.dumps({'acquired': won, 'pid': os.getpid()}), flush=True)\n"
    "os._exit(9)\n"
)


def test_holder_death_mid_protocol_is_diagnosable_and_recoverable(tmp_path):
    """Counterexample: the holder exits mid-protocol without RELEASE.

    The child's own exit code stays the reported first cause (never an
    Errno-22-style cleanup exception), dead-but-unexpired metadata cannot be
    stolen by another OS process, and after the 300 s expiry a fresh OS
    process recovers the key because the kernel already released the byte
    lock when the holder died.
    """
    notes: list[str] = []
    holder = ChildProtocol(tmp_path, 0, script=CHILD_HOLDER_DIES, now=NOW)
    try:
        holder.wait_line(1, READY_TIMEOUT_SECONDS, "READY")
        holder.send("GO")
        holder.wait_line(2, RESULT_TIMEOUT_SECONDS, "holder acquire result")
        holder_record = json.loads(holder.out[1])
        assert holder_record["acquired"] is True
    finally:
        # The holder exits (rc=9) without RELEASE: cleanup treats the dead
        # stdin as evidence only and must not mask the primary outcome.
        holder.finish(RELEASE_TIMEOUT_SECONDS, notes)
    assert holder.child.returncode == 9
    assert holder.child.poll() is not None  # no leaked process
    # Dead-but-unexpired metadata is refused for a fresh OS process.
    early = ChildProtocol(tmp_path, 1, now=NOW + 299 * SECOND)
    try:
        early.wait_line(1, READY_TIMEOUT_SECONDS, "READY")
        early.send("GO")
        early.wait_line(2, RESULT_TIMEOUT_SECONDS, "unexpired successor result")
        assert json.loads(early.out[1])["acquired"] is False
    finally:
        early.finish(RELEASE_TIMEOUT_SECONDS, notes)
    assert early.child.returncode == 0, ''.join(early.err)
    metadata = json.loads((tmp_path / "task-lease.json").read_text(encoding="utf-8"))
    assert int(metadata["holder_pid"]) == holder_record["pid"]
    # Dead plus expired: the kernel lock vanished with the dead holder, so a
    # fresh OS process recovers the same key and owns the metadata afterwards.
    recovered = ChildProtocol(tmp_path, 2, now=NOW + 301 * SECOND)
    try:
        recovered.wait_line(1, READY_TIMEOUT_SECONDS, "READY")
        recovered.send("GO")
        recovered.wait_line(2, RESULT_TIMEOUT_SECONDS, "expired successor result")
        recovered_record = json.loads(recovered.out[1])
        assert recovered_record["acquired"] is True
    finally:
        recovered.finish(RELEASE_TIMEOUT_SECONDS, notes)
    assert recovered.child.returncode == 0, ''.join(recovered.err)
    assert recovered.child.poll() is not None
    # The recovered holder released cleanly on exit: TaskLease.release removes
    # the metadata it owns, and only the stable lock byte file remains -- no
    # permanent lock, no leaked process, no leftover metadata.
    assert not (tmp_path / "task-lease.json").exists()
    assert (tmp_path / "task-lease.lock").stat().st_size >= 1
    print(json.dumps({"SYNTHETIC_holder_death_notes": notes}))


CHILD_GUARD_PROBE = (
    "import sys\n"
    "from ak_trading.testing import offline_guard\n"
    "offline_guard.require_installed()\n"
    "print('GUARDED', flush=True)\n"
)


def test_child_without_bootstrap_fails_diagnosably_or_guard_injects_it(tmp_path):
    """Counterexample: no guard bootstrap handed to the child.

    Under a plain parent the child must fail fast with its own nonzero rc and
    the guard message as first cause (bootstrap missing is diagnosable, never
    a silent pass or a full-watchdog wait). Under the canonical launcher the
    installed guard forces its bootstrap into every Python child, so the
    unguarded state is structurally unreachable and the injection itself is
    asserted instead of skipped.
    """
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "src")  # bootstrap deliberately absent
    started = time.monotonic()
    probe = ChildProtocol(tmp_path, 0, script=CHILD_GUARD_PROBE, environment=environment)
    if offline_guard._installed:
        try:
            probe.wait_line(1, READY_TIMEOUT_SECONDS, "GUARDED")
        finally:
            probe.finish(RELEASE_TIMEOUT_SECONDS, [])
        assert probe.child.returncode == 0, ''.join(probe.err)
        return
    try:
        with pytest.raises(AssertionError, match="offline guard is not installed") as failure:
            probe.wait_line(1, READY_TIMEOUT_SECONDS, "GUARDED")
    finally:
        probe.finish(RELEASE_TIMEOUT_SECONDS, [])
    assert time.monotonic() - started < READY_TIMEOUT_SECONDS
    assert "rc=1" in str(failure.value)
    assert probe.child.returncode == 1
    assert probe.child.poll() is not None  # no leaked process
