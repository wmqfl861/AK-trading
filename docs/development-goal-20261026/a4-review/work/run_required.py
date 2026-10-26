import hashlib, json, os, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path
import psutil

CAND = Path("C:/AKWorktrees/aktr-goal-a4-20261026")
RUN = Path("C:/AKWorktrees/aktr-goal-20261026/zcode-dispatch/a4-hard-review-001/evidence/required-rerun-r1")
NL = bytes([10])
TIMEOUT_S = 3600
MEM_CAP = 2 * 1024 ** 3
OUT_CAP = 512 * 1024 ** 2

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1048576), b""):
            h.update(chunk)
    return h.hexdigest()

RUN.mkdir(parents=True, exist_ok=True)
pre_script = sha256_file(CAND / "scripts/research_probability_v2.py")
pre_test = sha256_file(CAND / "tests/test_v2_m5_shadow_entry_guard.py")
pre_cli = sha256_file(CAND / "tests/test_v2_m5_cli.py")

argv = [sys.executable, "-B", "-X", "utf8", "scripts/test_offline.py", "-q",
        "tests/test_v2_m5_shadow_entry_guard.py", "tests/test_v2_m5_cli.py"]
env = dict(os.environ)
env["PYTHONPATH"] = str(CAND / "src")

stdout_path = RUN / "stdout.txt"
stderr_path = RUN / "stderr.txt"
out_f = open(stdout_path, "wb")
err_f = open(stderr_path, "wb")
started_utc = datetime.now(timezone.utc).isoformat()
start = time.monotonic()
proc = subprocess.Popen(argv, cwd=str(CAND), stdout=out_f, stderr=err_f, env=env)

peak_rss = 0
samples = 0
violation = None
child_pids_seen = set()

def tree_snapshot(pid):
    parent = psutil.Process(pid)
    procs = [parent] + parent.children(recursive=True)
    return parent, procs

def tree_rss(pid):
    try:
        _, procs = tree_snapshot(pid)
    except psutil.NoSuchProcess:
        return 0
    total = 0
    for p in procs:
        try:
            total += p.memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return total

def kill_tree():
    try:
        _, procs = tree_snapshot(proc.pid)
        for p in procs:
            try:
                p.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except psutil.NoSuchProcess:
        pass

while True:
    if proc.poll() is not None:
        break
    elapsed = time.monotonic() - start
    if elapsed > TIMEOUT_S:
        violation = "TIMEOUT_3600S"
        kill_tree()
        break
    try:
        _, procs = tree_snapshot(proc.pid)
        for p in procs:
            child_pids_seen.add(p.pid)
    except psutil.NoSuchProcess:
        procs = []
    rss = sum(p.info.get("rss", 0) for p in [] )
    rss = tree_rss(proc.pid)
    samples += 1
    if rss > peak_rss:
        peak_rss = rss
    if rss > MEM_CAP:
        violation = "MEM_CAP_2GIB_EXCEEDED"
        kill_tree()
        break
    try:
        so = stdout_path.stat().st_size
        se = stderr_path.stat().st_size
    except OSError:
        so = se = 0
    if so > OUT_CAP or se > OUT_CAP:
        violation = "OUTPUT_CAP_512MIB_EXCEEDED"
        kill_tree()
        break
    time.sleep(0.5)

returncode = proc.wait()
leftover_alive = sorted(pid for pid in child_pids_seen if psutil.pid_exists(pid) and pid != os.getpid())
ended_utc = datetime.now(timezone.utc).isoformat()
wall = time.monotonic() - start
out_f.flush()
out_f.close()
err_f.flush()
err_f.close()
stdout_text = stdout_path.read_text(encoding="utf-8", errors="replace")
stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")
totals = [l for l in stdout_text.splitlines() if l.startswith("Offline totals:")]
overall = [l for l in stdout_text.splitlines() if l.startswith("Offline overall exit:")]
domains = [l for l in stdout_text.splitlines() if l.startswith("Offline domain ")]
command_line = [l for l in stdout_text.splitlines() if l.startswith("Offline command:")]
plan_line = [l for l in stdout_text.splitlines() if l.startswith("Offline plan:")]
post_script = sha256_file(CAND / "scripts/research_probability_v2.py")
post_test = sha256_file(CAND / "tests/test_v2_m5_shadow_entry_guard.py")
post_cli = sha256_file(CAND / "tests/test_v2_m5_cli.py")
result = {
    "task": "required-independent-rerun-all-18",
    "generated_utc": ended_utc,
    "argv": argv,
    "argv_string": " ".join(argv),
    "cwd": str(CAND),
    "env_pythonpath": env["PYTHONPATH"],
    "interpreter": {"executable": sys.executable, "version": sys.version,
                    "executable_sha256": sha256_file(sys.executable)},
    "started_utc": started_utc,
    "ended_utc": ended_utc,
    "wall_seconds": round(wall, 3),
    "returncode": returncode,
    "violation": violation,
    "peak_tree_rss_bytes": peak_rss,
    "peak_tree_rss_mib": round(peak_rss / 1048576, 1),
    "rss_samples": samples,
    "child_pids_seen": sorted(child_pids_seen),
    "leftover_alive_pids_after_wait": leftover_alive,
    "stdout_bytes": stdout_path.stat().st_size,
    "stderr_bytes": stderr_path.stat().st_size,
    "launcher_command_line": command_line,
    "launcher_plan_line": plan_line,
    "domain_lines": domains,
    "totals_line_last": totals[-1] if totals else None,
    "overall_exit_line_last": overall[-1] if overall else None,
    "product_hashes": {"pre": {"research_probability_v2": pre_script, "test_v2_m5_shadow_entry_guard": pre_test, "test_v2_m5_cli": pre_cli},
                       "post": {"research_probability_v2": post_script, "test_v2_m5_shadow_entry_guard": post_test, "test_v2_m5_cli": post_cli}},
}
text = json.dumps(result, indent=2)
json.loads(text)
tmp = RUN / "RESULT.json.tmp"
tmp.write_bytes(text.encode("utf-8") + NL)
os.replace(tmp, RUN / "RESULT.json")
(RUN / "cmd.txt").write_bytes((" ".join(argv) + " (cwd=" + str(CAND) + ")").encode("utf-8") + NL)
(RUN / "exit.txt").write_bytes(str(returncode).encode("utf-8") + NL)
print("returncode", returncode)
print("wall_seconds", round(wall, 3))
print("peak_rss_mib", result["peak_tree_rss_mib"])
print("violation", violation)
print("totals", result["totals_line_last"])
print("overall", result["overall_exit_line_last"])
print("domains", domains)
print("stderr_bytes", result["stderr_bytes"])
print("leftover_alive", leftover_alive)
print("hash_unchanged", pre_script == post_script and pre_test == post_test and pre_cli == post_cli)
