"""Supervisor self-tests v3 (author006 §4; review006 F1-F5 + OBS) — tiny
helper subprocesses only, no business fit. Prior coverage retained (exit 0,
nonzero, 130 with PID+create-time descendant, blocked sampler, denied
sampler, job-memory actual cause, occupied stdout/stderr, failed startup,
deadline clamp, pre-launch quota refusal, identity gate) plus the review
targets: assign-failure leaves NO unresumed orphan (reaped via own handle),
during-run cumulative quota overrun stops the run, occupied rss-samples
refused without side-effect files, whole-operation absolute clock covers
the identity probe, and exception-path cleanup. Bounded to the shared
author006 validation allowance."""
from __future__ import annotations

import ctypes
import json
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

WORK = Path(__file__).resolve().parent
SUPERVISOR = WORK / "run_daily_bounded.py"


def run_supervisor(tmp_path: Path, argv: list[str], **kw) -> dict:
    out = kw.pop("out", tmp_path / "run")
    cmd = [sys.executable, "-B", str(SUPERVISOR),
           "--deadline-seconds", str(kw.get("deadline", 30)),
           "--sample-interval", str(kw.get("interval", 0.5)),
           "--sample-timeout", str(kw.get("sample_timeout", 5)),
           "--cleanup-reserve-seconds", str(kw.get("reserve", 10)),
           "--cwd", str(WORK), "--out-dir", str(out)]
    if kw.get("job_memory_limit"):
        cmd += ["--job-memory-limit", str(kw["job_memory_limit"])]
    if kw.get("sampler_argv"):
        cmd += ["--sampler-argv-json", json.dumps(kw["sampler_argv"])]
    for root in kw.get("retained_roots", []):
        cmd += ["--retained-root", str(root)]
    if kw.get("quota"):
        cmd += ["--output-quota-bytes", str(kw["quota"])]
    if kw.get("quota_every"):
        cmd += ["--quota-check-every-seconds", str(kw["quota_every"])]
    if kw.get("identity_probe"):
        cmd += ["--identity-probe-json", json.dumps(kw["identity_probe"])]
    cmd += ["--", *argv]
    t0 = time.monotonic()
    env = {**kw.get("env", {})}
    if env:
        import os
        full_env = {**os.environ, **env}
    else:
        full_env = None
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=kw.get("outer", 90), env=full_env)
    outer = round(time.monotonic() - t0, 2)
    facts = {"_supervisor_returncode": proc.returncode,
             "_supervisor_stdout": proc.stdout, "_supervisor_stderr": proc.stderr,
             "_outer_wall": outer, "_out": str(out)}
    facts_path = Path(out) / "RUN-FACTS.json"
    if facts_path.is_file():
        try:
            facts.update(json.loads(facts_path.read_bytes()))
        except ValueError:
            pass  # deliberately non-JSON (occupied) - scenario-intended
    return facts


def test_exit_zero(tmp_path):
    facts = run_supervisor(tmp_path, [sys.executable, "-c", "print('ok')"], deadline=25)
    assert facts["stop_reason"] == "completed"
    assert facts["product_returncode"] == 0
    assert facts["_supervisor_returncode"] == 0 == facts["supervisor_exit"]
    assert facts["job_assigned"] is True
    assert facts["wall_seconds"] < 25


def test_specified_nonzero(tmp_path):
    facts = run_supervisor(tmp_path, [sys.executable, "-c", "raise SystemExit(7)"],
                           deadline=25)
    assert facts["stop_reason"] == "completed"
    assert facts["product_returncode"] == 7
    assert facts["_supervisor_returncode"] == 3 == facts["supervisor_exit"]


def test_deadline_130_with_descendant(tmp_path):
    marker = tmp_path / "grandchild.json"
    child = (f"import subprocess,sys,time,json,psutil;"
             f"gc=subprocess.Popen([sys.executable,'-c','import time;time.sleep(300)']);"
             f"ident={{'gcpid': gc.pid, 'gccreate': psutil.Process(gc.pid).create_time()}};"
             f"print(json.dumps(ident),file=open(r'{marker}','w'));"
             f"time.sleep(300)")
    facts = run_supervisor(tmp_path, [sys.executable, "-c", child],
                           deadline=8, reserve=3, interval=0.3)
    assert facts["stop_reason"] == "deadline"
    assert facts["product_returncode"] == 130
    # N2 (review007): if the clamped sampler call timed out at the same
    # moment the deadline fired, the recorded stop reason stays the TRUE
    # causal one (deadline) and the sampler failure carries the ACTUAL
    # clamped timeout — never a mislabeled sampler_timeout stop.
    for failure in facts.get("sampler_failures", []):
        if failure["kind"] == "timeout":
            assert failure["deadline_causal"] is True
            assert failure["clamped_to"] <= 5.0  # <= the 5s stop window
    ident = json.loads(marker.read_text(encoding="utf-8"))
    grandchild, gc_create = ident["gcpid"], ident["gccreate"]
    for _ in range(20):
        try:
            proc = psutil.Process(grandchild)
            assert proc.create_time() == gc_create
            time.sleep(0.5)
        except psutil.NoSuchProcess:
            break
    with pytest.raises(psutil.NoSuchProcess):
        psutil.Process(grandchild)


def test_sampler_permanently_blocked(tmp_path):
    hanging = tmp_path / "hang_sampler.py"
    hanging.write_text("import time\ntime.sleep(600)\n", encoding="utf-8")
    facts = run_supervisor(
        tmp_path, [sys.executable, "-c", "import time; time.sleep(60)"],
        deadline=40, interval=0.5, sample_timeout=3,
        sampler_argv=[sys.executable, "-B", str(hanging), "1"])
    assert facts["stop_reason"] == "sampler_timeout_after_3.0s"
    assert facts["product_returncode"] == 130
    assert facts["sampler_failures"][0]["kind"] == "timeout"


def test_sampler_denied_stops_run(tmp_path):
    denying = tmp_path / "denied_sampler.py"
    denying.write_text(
        'import json;print(json.dumps({"rss": 0, "procs": 0, "gone": False, "denied": 1}))\n',
        encoding="utf-8")
    facts = run_supervisor(
        tmp_path, [sys.executable, "-c", "import time; time.sleep(60)"],
        deadline=40, interval=0.5,
        sampler_argv=[sys.executable, "-B", str(denying), "1"])
    assert facts["stop_reason"] == "sampler_denied"
    assert facts["product_returncode"] == 130
    assert facts["sampler_failures"][0]["kind"] == "access_denied"


def test_small_job_memory_quota_actual_cause(tmp_path):
    hog = ("import sys\n"
           "try:\n"
           "    data = bytearray(300 * 1024 * 1024)\n"
           "    for i in range(0, len(data), 4096):\n"
           "        data[i] = 1\n"
           "    print('allocated', flush=True)\n"
           "except MemoryError:\n"
           "    print('QUOTA_MEMORY_ERROR', flush=True)\n"
           "    raise SystemExit(9)\n"
           "import time; time.sleep(30)\n")
    facts = run_supervisor(tmp_path, [sys.executable, "-c", hog],
                           deadline=45, interval=0.5,
                           job_memory_limit=100 * 1024 * 1024)
    assert facts["stop_reason"] == "completed"
    assert facts["product_returncode"] == 9
    stdout = (tmp_path / "run" / "stdout.txt")
    assert "QUOTA_MEMORY_ERROR" in stdout.read_text(encoding="utf-8", errors="replace")


def test_occupied_output_refused_no_overwrite(tmp_path):
    out = tmp_path / "run"
    out.mkdir()
    (out / "stdout.txt").write_text("PREEXISTING PARTIAL LOG", encoding="utf-8")
    facts = run_supervisor(tmp_path, [sys.executable, "-c", "print('ok')"],
                           deadline=20, out=out)
    assert facts["stop_reason"].startswith("output_occupied_refused")
    assert facts["_supervisor_returncode"] == 2
    assert (out / "stdout.txt").read_text(encoding="utf-8") == "PREEXISTING PARTIAL LOG"
    assert not (out / "stderr.txt").exists()
    assert not (out / "rss-samples.jsonl").exists()


def test_occupied_rss_samples_refused_no_side_effects(tmp_path):
    # review006 F3: occupied rss-samples.jsonl alone must be refused BEFORE
    # any other stream file is created
    out = tmp_path / "run"
    out.mkdir()
    (out / "rss-samples.jsonl").write_text("PREEXISTING SAMPLES\n", encoding="utf-8")
    facts = run_supervisor(tmp_path, [sys.executable, "-c", "print('ok')"],
                           deadline=20, out=out)
    assert facts["stop_reason"].startswith("output_occupied_refused")
    assert "rss-samples.jsonl" in facts["stop_reason"]
    assert facts["_supervisor_returncode"] == 2
    assert not (out / "stdout.txt").exists()
    assert not (out / "stderr.txt").exists()
    assert (out / "rss-samples.jsonl").read_text(encoding="utf-8") == \
        "PREEXISTING SAMPLES\n"


def test_failed_startup_captured(tmp_path):
    facts = run_supervisor(tmp_path, [str(tmp_path / "nonexistent_exe.exe")],
                           deadline=20)
    assert facts["stop_reason"] == "supervisor_exception:FileNotFoundError"
    assert facts["_supervisor_returncode"] == 3 == facts["supervisor_exit"]
    assert facts["product_returncode"] == "unknown"
    assert facts["cleanup"]["terminated"] is True
    assert facts["cleanup"]["job_closed"] is True


def test_deadline_clamp_refused(tmp_path):
    facts = run_supervisor(tmp_path, [sys.executable, "-c", "print('ok')"],
                           deadline=10, reserve=10)
    assert facts["stop_reason"] == "deadline_clamped_refused"
    assert facts["_supervisor_returncode"] == 2
    assert facts.get("product_pid") is None


def test_output_quota_cumulative_refused(tmp_path):
    retained = tmp_path / "retained"
    retained.mkdir()
    (retained / "existing.bin").write_bytes(b"x" * 5000)
    facts = run_supervisor(tmp_path, [sys.executable, "-c", "print('ok')"],
                           deadline=20, retained_roots=[retained], quota=4096,
                           out=retained / "run")
    assert facts["stop_reason"] == "output_quota_exhausted_refused"
    assert facts["_supervisor_returncode"] == 2
    assert facts["retained_bytes_before"] >= 5000
    assert facts.get("product_pid") is None
    facts2 = run_supervisor(tmp_path, [sys.executable, "-c", "print('ok')"],
                            deadline=20, retained_roots=[tmp_path / "elsewhere"],
                            quota=10 * 1024**3, out=tmp_path / "run2")
    assert facts2["stop_reason"] == "output_root_membership_refused"


def test_quota_exceeded_during_run_stops(tmp_path):
    # review006 F2: the child GROWS the retained output past the quota while
    # running; the supervisor must stop it (not certify completion)
    retained = tmp_path / "retained"
    retained.mkdir()
    writer = (f"import time\n"
              f"open(r'{retained / 'grown.bin'}','wb').write(b'y' * 1200000)\n"
              f"time.sleep(20)\n")
    facts = run_supervisor(tmp_path, [sys.executable, "-c", writer],
                           deadline=40, reserve=8, interval=0.3,
                           retained_roots=[retained], quota=1000 * 1024,
                           out=retained / "run", quota_every=0.5)
    assert facts["stop_reason"] == "output_quota_exceeded_during_run"
    assert facts["_supervisor_returncode"] == 3 == facts["supervisor_exit"]
    assert facts["product_returncode"] == 130
    assert any(check["retained_bytes"] >= 1000 * 1024 for check in facts["quota_checks"])


def test_quota_exceeded_final_state_flags(tmp_path):
    # review006 F2 final-state enforcement: growth lands between periodic
    # checks and the child exits 0 — the final scan must still flag it
    retained = tmp_path / "retained"
    retained.mkdir()
    (retained / "seed.bin").write_bytes(b"x" * 900 * 1024)
    writer = (f"open(r'{retained / 'final.bin'}','wb').write(b'z' * 300 * 1024)\n")
    facts = run_supervisor(tmp_path, [sys.executable, "-c", writer],
                           deadline=30, reserve=8, interval=0.3,
                           retained_roots=[retained], quota=1024 * 1024,
                           out=retained / "run", quota_every=999)
    assert facts["stop_reason"] == "output_quota_exceeded_final"
    assert facts["_supervisor_returncode"] == 3
    assert facts["retained_bytes_after"] >= 1200 * 1024


def test_identity_probe_gate(tmp_path):
    failing = [sys.executable, "-c", "raise SystemExit(3)"]
    facts = run_supervisor(tmp_path, [sys.executable, "-c", "print('ok')"],
                           deadline=20, identity_probe=failing)
    assert facts["stop_reason"] == "identity_probe_failed"
    assert facts["identity_probe_exit"] == 3
    assert facts.get("product_pid") is None


def test_identity_probe_time_inside_wall(tmp_path):
    # review006 F4: the probe runs INSIDE the one absolute clock — the
    # recorded wall covers the probe time, and outer runtime stays bounded
    sleeping = [sys.executable, "-c", "import time; time.sleep(5); "]
    facts = run_supervisor(tmp_path, [sys.executable, "-c", "print('ok')"],
                           deadline=30, identity_probe=sleeping)
    assert facts["stop_reason"] == "completed"
    assert facts["wall_seconds"] >= 5.0          # probe counted in-wall
    assert facts["_outer_wall"] < 30             # bounded overall


def test_assign_failure_reaps_uncontained_child(tmp_path, monkeypatch):
    # review006 F1: AssignProcessToJobObject fails -> the suspended child is
    # NEVER resumed; it is terminated+reaped via its own handle, exact PID +
    # create-time identity captured while owned; no orphan survives main().
    facts = run_supervisor(
        tmp_path, [sys.executable, "-c", "import time; time.sleep(60)"],
        deadline=12, reserve=4, interval=0.5,
        env={"T7_SUPERVISOR_TEST_FAIL_ASSIGN": "1"})
    rc = facts["_supervisor_returncode"]
    out = Path(facts["_out"])
    assert facts["stop_reason"] == "assign_job_failed"
    assert facts["job_assigned"] is False
    assert facts["assign_failure_reaped"] is True
    assert isinstance(facts["product_returncode"], int)
    assert facts["cleanup"]["job_closed"] is True
    assert rc == 3
    pid = facts["product_pid"]
    create = facts["product_create_time"]
    with pytest.raises(psutil.NoSuchProcess):  # no orphan survived main()
        proc = psutil.Process(pid)
        assert proc.create_time() == create  # same process if somehow alive


def test_identity_probe_timeout_handled_refusal(tmp_path):
    # review007 N1: a blocking identity probe under a clamped timeout must
    # publish refusal facts (exit 2, stop reason captured) — never an
    # escaping TimeoutExpired traceback with CLI exit 1 and no facts.
    blocking = [sys.executable, "-c", "import time; time.sleep(30)"]
    facts = run_supervisor(tmp_path, [sys.executable, "-c", "print('ok')"],
                           deadline=8, reserve=2, identity_probe=blocking)
    assert facts["stop_reason"] == "identity_probe_timeout"
    assert facts["_supervisor_returncode"] == 2 == facts["supervisor_exit"]
    assert 0 < facts["identity_probe_timeout_clamp"] <= 6.0
    assert facts.get("product_pid") is None          # product never launched
    out = Path(facts["_out"])
    assert not (out / "stdout.txt").exists()          # no stream side effects
    assert not (out / "stderr.txt").exists()
    assert facts["_outer_wall"] < 15                  # bounded overall


def test_bounded_capture_skips_when_budget_expired(tmp_path):
    # review007 N3: capture is a BOUNDED path — with the absolute deadline
    # already passed, streams are skipped and reported, never read.
    import importlib.util
    spec = importlib.util.spec_from_file_location("supervisor_n3", SUPERVISOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_bytes(b"A" * 100)
    b.write_bytes(b"B" * 100)
    import time as _time
    hashed, skipped, aborted = module.bounded_capture(
        (("a", a), ("b", b)), _time.monotonic() + 10)
    assert set(hashed) == {"a", "b"} and not skipped and not aborted
    hashed2, skipped2, aborted2 = module.bounded_capture(
        (("a", a), ("b", b)), _time.monotonic() - 1)
    assert hashed2 == {} and skipped2 == ["a", "b"] and not aborted2


def test_product0_capture_blocked_supervisor3(tmp_path):
    # author008: product exits 0 but the capture phase blocks -> the OUTER
    # boundary terminates the worker at the hard deadline; overall failure,
    # product return code stays 0, failure reasons name the boundary.
    facts = run_supervisor(tmp_path, [sys.executable, "-c", "print('ok')"],
                           deadline=8, reserve=2,
                           env={"T7_SUPERVISOR_TEST_HANG_STAGE": "post_product_hash"})
    verdict = json.loads((Path(facts["_out"]) / "OUTER-VERDICT.json").read_bytes())
    assert verdict["product_returncode"] == 0          # preserved as 0 (sidecar)
    assert verdict["unified_supervisor_exit"] == 3     # overall NOT success
    assert verdict["outer_terminated"] is True
    assert any("outer_deadline_terminated" in r for r in verdict["failure_reasons"])
    assert verdict["outer_wall_seconds"] <= 10


def test_preflight_blocked_outer_stops_worker(tmp_path):
    # author008: a blocked PRE-PRODUCT scan -> outer stops the whole worker
    # subtree at the common deadline; product never launched; no leftovers.
    retained = tmp_path / "retained"
    retained.mkdir()
    (retained / "seed.bin").write_bytes(b"x" * 100)
    facts = run_supervisor(tmp_path, [sys.executable, "-c", "print('never')"],
                           deadline=8, reserve=2, retained_roots=[retained],
                           out=retained / "run",
                           env={"T7_SUPERVISOR_TEST_HANG_STAGE": "preflight_scan"})
    verdict = json.loads((Path(facts["_out"]) / "OUTER-VERDICT.json").read_bytes())
    assert verdict["unified_supervisor_exit"] == 3
    assert verdict["outer_terminated"] is True
    assert facts.get("product_pid") is None            # product never launched
    run_dir = Path(facts["_out"])
    assert not (run_dir / "stdout.txt").exists()       # no stream side effects


def test_publish_blocked_no_overall_success(tmp_path):
    # author008: blocked facts publication -> no overall success; the outer
    # records capture/publication incompleteness; no fabricated budget-pass.
    facts = run_supervisor(tmp_path, [sys.executable, "-c", "print('ok')"],
                           deadline=8, reserve=2,
                           env={"T7_SUPERVISOR_TEST_HANG_STAGE": "publish"})
    verdict_path = Path(facts["_out"]) / "OUTER-VERDICT.json"
    verdict = json.loads(verdict_path.read_bytes()) if verdict_path.exists() else {}
    assert facts.get("_supervisor_returncode", 3) == 3
    assert verdict.get("outer_terminated") is True or facts.get("stop_reason") is None


def test_occupied_nonjson_facts_outer_exit2(tmp_path):
    # review009 F1 regression: an occupied (non-JSON) RUN-FACTS.json must
    # preserve the file, refuse any product launch, and return the OUTER
    # CLI exit 2 (worker's authoritative prelaunch refusal), not 3.
    out = tmp_path / "run"
    out.mkdir()
    (out / "RUN-FACTS.json").write_text("NOT JSON AT ALL", encoding="utf-8")
    facts = run_supervisor(tmp_path, [sys.executable, "-c", "print('never')"],
                           deadline=8, reserve=2, out=out)
    # the occupied non-JSON file is not readable as facts - that is the
    # point of the scenario; only the harness keys matter below
    assert facts["_supervisor_returncode"] == 2          # outer exit contract
    verdict = json.loads((out / "OUTER-VERDICT.json").read_bytes())
    assert verdict["unified_supervisor_exit"] == 2
    assert verdict["worker_exit"] == 2
    assert (out / "RUN-FACTS.json").read_text(encoding="utf-8") == "NOT JSON AT ALL"
    assert not (out / "stdout.txt").exists()             # product never launched
    assert not (out / "stderr.txt").exists()
