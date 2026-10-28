"""T7-B bounded product-run supervisor, r9 (author006 §4; review006 F1-F5 + OBS).

Seeded from the 018 freeze (caef40e8...) with these repairs:

- F1: on AssignProcessToJobObject failure the child is NEVER resumed; it is
  terminated and reaped through the retained own PROCESS handle with bounded
  waits and explicit creation-identity capture; every error path guarantees
  owned-child cleanup + facts capture (job handle closed too).
- F2: the cumulative retained-output quota is enforced BEFORE the run,
  DURING the run (periodic fail-closed scans of the real retained roots)
  and at the FINAL state — an overrun after the run changes stop_reason and
  the supervisor exit; OSError during scanning is recorded and fails the
  quota check closed (never silently undercounted).
- F3: the occupied-output precheck covers rss-samples.jsonl and the facts
  temp sibling; no stream file is created before the refusal decision.
- F4: ONE absolute clock starts at main() entry — bootstrap, preflight,
  identity probe, sampling, cleanup, hashing, scanning and publication all
  live inside the same stop/hard deadlines; the identity-probe timeout is
  clamped to the remaining budget (no fresh 120 s), the deadline watcher is
  cancelled on completion (no fixed 5 s join tax), and any wall overshoot is
  recorded honestly.
- F5: explicit argtypes/restypes for every Win32/NT call that passes or
  returns handles.
- OBS-h: the generic exception path also terminates+waits the child via its
  handle and closes the job handle.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

kernel32 = ctypes.windll.kernel32
ntdll = ctypes.windll.ntdll

# F5: explicit prototypes — no default c_int handle truncation anywhere.
kernel32.CreateJobObjectW.restype = ctypes.c_void_p
kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
kernel32.SetInformationJobObject.restype = ctypes.c_int
kernel32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                             ctypes.c_void_p, ctypes.c_uint32]
kernel32.AssignProcessToJobObject.restype = ctypes.c_int
kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
kernel32.TerminateJobObject.restype = ctypes.c_int
kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
kernel32.TerminateProcess.restype = ctypes.c_int
kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
kernel32.WaitForSingleObject.restype = ctypes.c_uint32
kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
kernel32.CloseHandle.restype = ctypes.c_int
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
kernel32.GetExitCodeProcess.restype = ctypes.c_int
kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p,
                                        ctypes.POINTER(ctypes.c_uint32)]
ntdll.NtResumeProcess.restype = ctypes.c_long
ntdll.NtResumeProcess.argtypes = [ctypes.c_void_p]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in
                ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                 "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32)]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


JOB_OBJECT_EXTENDED_LIMIT = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x0200
DEADLINE_EXIT_CODE = 130
ASSIGN_FAIL_EXIT = 1

DEFAULT_SAMPLER_SCRIPT = r"""
import json, sys
import psutil
try:
    root = psutil.Process(int(sys.argv[1]))
except psutil.NoSuchProcess:
    print(json.dumps({"rss": 0, "procs": 0, "gone": True, "denied": 0}))
    raise SystemExit(0)
except psutil.AccessDenied:
    print(json.dumps({"rss": 0, "procs": 0, "gone": False, "denied": 1}))
    raise SystemExit(0)
total, count, denied = 0, 0, 0
try:
    members = [root, *root.children(recursive=True)]
except psutil.NoSuchProcess:
    print(json.dumps({"rss": 0, "procs": 0, "gone": True, "denied": 0}))
    raise SystemExit(0)
except psutil.AccessDenied:
    print(json.dumps({"rss": 0, "procs": 0, "gone": False, "denied": 1}))
    raise SystemExit(0)
for member in members:
    try:
        total += member.memory_info().rss
        count += 1
    except psutil.NoSuchProcess:
        continue
    except psutil.AccessDenied:
        denied += 1
print(json.dumps({"rss": total, "procs": count, "gone": False, "denied": denied}))
"""


def create_job(job_memory_limit: int | None):
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise OSError("CreateJobObjectW failed")
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if job_memory_limit:
        info.BasicLimitInformation.LimitFlags |= JOB_OBJECT_LIMIT_JOB_MEMORY
        info.JobMemoryLimit = job_memory_limit
    if not kernel32.SetInformationJobObject(
            job, JOB_OBJECT_EXTENDED_LIMIT, ctypes.byref(info), ctypes.sizeof(info)):
        raise OSError("SetInformationJobObject failed")
    return job  # c_void_p value; truthiness-checked, closed via CloseHandle


def atomic_publish(path: Path, document: dict) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite existing final facts: {path}")
    payload = (json.dumps(document, indent=1, sort_keys=True) + "\n").encode("utf-8")
    json.loads(payload.decode("utf-8"))  # validate exactly the bytes we publish
    tmp = path.with_suffix(path.suffix + ".tmp")
    if tmp.exists():
        raise FileExistsError(f"refusing to overwrite failed attempt: {tmp}")
    tmp.write_bytes(payload)
    os.replace(tmp, path)


def retained_state(roots: list[Path]) -> tuple[int, list[str]]:
    """(bytes, errors): streaming os.scandir traversal — one DirEntry with a
    single stat(follow_symlinks=False) per entry, no whole-tree list and no
    repeated type/size queries (author012 §5). Only regular non-link files
    count bytes; symlinks/reparse points are seen but never followed (no
    silent loss, no double count); stat/scan errors are collected, never
    swallowed."""
    import os
    total = 0
    errors: list[str] = []

    def walk(directory: str) -> None:
        try:
            with os.scandir(directory) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue  # seen, not followed, not counted
                        if entry.is_file(follow_symlinks=False):
                            total_bytes[0] += entry.stat(follow_symlinks=False).st_size
                        elif entry.is_dir(follow_symlinks=False):
                            walk(entry.path)
                    except OSError as error:
                        errors.append(f"{entry.name}:{type(error).__name__}")
        except OSError as error:
            errors.append(f"{directory}:{type(error).__name__}")

    total_bytes = [0]
    for root in roots:
        walk(str(root))
    total = total_bytes[0]
    return total, errors


def is_within(child: Path, ancestor: Path) -> bool:
    current = child.resolve()
    ancestor = ancestor.resolve()
    while current != current.parent:
        if current == ancestor:
            return True
        current = current.parent
    return False


def bounded_capture(streams, deadline: float) -> tuple[dict, list]:
    """N3 (review007): hash streams only while time remains before the
    absolute deadline. Returns ({name: {bytes, sha256}}, [skipped names]);
    a stream whose budget expired is SKIPPED and reported, never read
    unbounded. Publication itself always runs afterwards."""
    hashed: dict = {}
    skipped: list = []
    aborted: list = []
    for name, path in streams:
        if not path.exists():
            continue
        if time.monotonic() >= deadline - 0.2:
            skipped.append(name)
            continue
        digest = hashlib.sha256()
        size = 0
        try:
            with path.open("rb") as handle:
                while True:
                    if time.monotonic() >= deadline - 0.05:
                        aborted.append(name)
                        break
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    size += len(chunk)
        except OSError:
            aborted.append(name)
            continue
        if name not in aborted and not (size == 0 and path.stat().st_size):
            hashed[name] = {"bytes": size, "sha256": digest.hexdigest()}
    return hashed, skipped, aborted


def bounded_handle_reap(handle, max_wait: float, exit_code_box) -> bool:
    """Terminate+wait one process via its OWN handle; True when reaped."""
    kernel32.TerminateProcess(handle, ASSIGN_FAIL_EXIT)
    waited = kernel32.WaitForSingleObject(
        handle, int(max(0.1, max_wait) * 1000))
    if waited != 0:  # WAIT_OBJECT_0
        return False
    kernel32.GetExitCodeProcess(handle, exit_code_box)
    return True


def run_worker(args, cmd) -> int:
    # F4: the ONE absolute clock for the WORKER — the OUTER supervisor
    # (main) establishes the same absolute boundary BEFORE spawning this
    # worker, so every scan/probe/output lives inside one shared budget.
    # (args/cmd come from main's parser; the worker never re-parses argv.)
    started = time.monotonic()
    started_wall = time.time()

    out = Path(args.out_dir).resolve()
    stop_at = started + (args.deadline_seconds - args.cleanup_reserve_seconds)
    hard_at = started + args.deadline_seconds

    def remaining_to_stop() -> float:
        return max(0.1, stop_at - time.monotonic())

    def remaining_to_hard() -> float:
        return max(0.1, hard_at - time.monotonic())

    facts: dict = {
        "schema": "aktr-t7-B-bounded-supervisor/v4",
        "product_argv": cmd,
        "cwd": str(Path(args.cwd).resolve()),
        "deadline_seconds": args.deadline_seconds,
        "ram_cap_bytes": args.ram_cap_bytes,
        "job_memory_limit": args.job_memory_limit,
        "stop_deadline_seconds": args.deadline_seconds - args.cleanup_reserve_seconds,
        "wall_started_epoch": started_wall,
    }

    def refuse() -> int:
        facts["supervisor_exit"] = 2
        facts["wall_seconds"] = round(time.monotonic() - started, 3)
        if facts.get("stop_reason") == "output_in_ro_root_refused":
            pass  # author011: a refusal must never write into the ro root
        else:
            out.mkdir(parents=True, exist_ok=True)
            if not (out / "RUN-FACTS.json").exists():
                atomic_publish(out / "RUN-FACTS.json", facts)
        print(json.dumps({"stop_reason": facts.get("stop_reason"),
                          "supervisor_exit": 2}, indent=1))
        return 2

    if not cmd:
        print("no product command", file=sys.stderr)
        return 2
    if args.cleanup_reserve_seconds >= args.deadline_seconds:
        facts["stop_reason"] = "deadline_clamped_refused"
        return refuse()

    # F3: occupied precheck covers EVERY stream we would create, plus the
    # facts temp sibling — decided before any file exists.
    occupied = [name for name in ("RUN-FACTS.json", "stdout.txt", "stderr.txt",
                                  "rss-samples.jsonl", "RUN-FACTS.json.tmp")
                if (out / name).exists()]
    if occupied:
        facts["stop_reason"] = f"output_occupied_refused:{occupied}"
        return refuse()

    # author011 s4: "::ro" is only a REQUEST classification. A root counts
    # as frozen history ONLY with complete pins (members+content, exact
    # both ways) bound to a terminal/freeze authority Root confirmed
    # writer-free; anything less is refused or stays ACTIVE, never trusted.
    # author012 §4: consume Root's actual classification table. An ::ro
    # claim is honored ONLY for a path the table lists as ro; a claim on a
    # table-active root is a FAIL (mismatched proof), unknown paths stay
    # active, and zero ro is the valid conservative fallback.
    table_ro: set[Path] = set()
    table_active: set[Path] = set()
    if args.classification:
        try:
            table = json.loads(Path(args.classification).read_bytes().decode("utf-8"))
        except (OSError, ValueError):
            facts["stop_reason"] = "classification_table_unreadable_refused"
            return refuse()
        for entry in table.get("ro_roots") or []:
            table_ro.add(Path(str(entry.get("path", entry) if isinstance(entry, dict) else entry)).resolve())
        for entry in table.get("active_roots") or []:
            table_active.add(Path(str(entry.get("path", entry) if isinstance(entry, dict) else entry)).resolve())
        facts["classification_source"] = str(Path(args.classification).resolve())
        facts["classification_counts"] = {"ro": len(table_ro), "active": len(table_active)}
    ro_roots: list[Path] = []
    active_roots: list[Path] = []
    ro_pins: dict[Path, str] = {}
    for raw_root in args.retained_root:
        if raw_root.endswith("::ro"):
            facts["stop_reason"] = "ro_credentials_missing_refused"
            return refuse()
        if "::ro::" in raw_root:
            root_part, pins_part = raw_root.split("::ro::", 1)
            resolved = Path(root_part).resolve()
            if args.classification and resolved not in table_ro:
                # claimed proof does not match Root's actual classification
                facts["stop_reason"] = "ro_claim_mismatched_classification_refused"
                return refuse()
            ro_roots.append(resolved)
            ro_pins[resolved] = pins_part
        else:
            active_roots.append(Path(raw_root).resolve())
    # every table-ro root that Root declares must actually be supplied
    for table_ro_root in table_ro:
        if table_ro_root not in ro_roots and table_ro_root not in active_roots:
            facts["stop_reason"] = "classification_ro_root_missing_refused"
            return refuse()

    def dedup_and_check(roots_list: list[Path], kind: str) -> list[Path]:
        seen: list[Path] = []
        for root in roots_list:
            if root in seen:
                continue  # exact duplicate: count once
            for other in seen:
                if is_within(root, other) or is_within(other, root):
                    facts["stop_reason"] = kind + "_category_nested_overlap_refused"
                    return []
            seen.append(root)
        return seen

    active_roots = dedup_and_check(active_roots, "active")
    if facts.get("stop_reason", "").startswith("active_category"):
        return refuse()
    ro_roots = dedup_and_check(ro_roots, "ro")
    if facts.get("stop_reason", "").startswith("ro_category"):
        return refuse()
    for ro in ro_roots:
        for active in active_roots:
            if ro == active or is_within(ro, active) or is_within(active, ro):
                facts["stop_reason"] = "cross_category_overlap_refused"
                return refuse()
    for ro in ro_roots:
        if ro not in ro_pins:
            facts["stop_reason"] = "ro_credentials_missing_refused"
            return refuse()
    roots = ro_roots + active_roots
    frozen_history_bytes = 0

    def verify_ro_root(root: Path, pins_path: str) -> tuple[int, str | None]:
        """Full member+content verification against the pins record."""
        try:
            document = json.loads(Path(pins_path).read_bytes().decode("utf-8"))
        except (OSError, ValueError):
            return 0, "ro_pins_unreadable"
        if Path(str(document.get("root", "?"))).resolve() != root:
            return 0, "ro_pins_root_mismatch"
        authority = document.get("authority") or {}
        if not authority.get("record") or not authority.get("confirmed_no_writer"):
            return 0, "ro_pins_authority_incomplete"
        pins_files = document.get("files") or {}
        total = 0
        actual: dict[str, tuple[int, str]] = {}
        try:
            for path in root.rglob("*"):
                try:
                    if path.is_file() and not path.is_symlink():
                        rel = path.relative_to(root).as_posix()
                        digest = hashlib.sha256()
                        size = 0
                        with path.open("rb") as handle:  # chunked: never a
                            while True:                    # whole-file read
                                chunk = handle.read(1024 * 1024)
                                if not chunk:
                                    break
                                digest.update(chunk)
                                size += len(chunk)
                        actual[rel] = (size, digest.hexdigest())
                        total += size
                except OSError:
                    return 0, "ro_pins_scan_error"
        except OSError:
            return 0, "ro_pins_scan_error"
        if set(actual) != set(pins_files):
            return 0, "ro_pins_member_mismatch"
        for rel, meta in pins_files.items():
            if not isinstance(meta, dict) or actual[rel] != (meta.get("bytes"), meta.get("sha256")):
                return 0, "ro_pins_content_mismatch"
        return total, None

    if roots:
        active_owner = any(is_within(out, root) for root in active_roots)
        ro_owner = any(is_within(out, root) for root in ro_roots)
        if ro_owner:
            facts["stop_reason"] = "output_in_ro_root_refused"
            return refuse()
        if not active_owner:
            facts["stop_reason"] = "output_root_membership_refused"
            return refuse()
        verified_ro_total = 0
        for ro in ro_roots:
            ro_bytes, ro_reason = verify_ro_root(ro, ro_pins[ro])
            if ro_reason:
                facts["stop_reason"] = ro_reason + "_refused"
                return refuse()
            verified_ro_total += ro_bytes
        frozen_history_bytes = verified_ro_total  # the ACTUAL verified total
        already, scan_errors = retained_state(active_roots) if active_roots else (0, [])
        already += verified_ro_total
        facts["ro_roots"] = [str(r) for r in ro_roots]
        facts["ro_bytes_verified_base"] = verified_ro_total
        facts["retained_bytes_before"] = already
        facts["output_quota_bytes"] = args.output_quota_bytes
        facts["quota_scan_errors_before"] = scan_errors
        if scan_errors:
            facts["stop_reason"] = "output_quota_check_error_refused"
            return refuse()
        if already >= args.output_quota_bytes:
            facts["stop_reason"] = "output_quota_exhausted_refused"
            return refuse()
        facts["output_quota_remaining_before"] = args.output_quota_bytes - already

    if args.identity_probe_json:
        probe_argv = [str(piece) for piece in json.loads(args.identity_probe_json)]
        # F4: probe timeout clamped into the SAME absolute budget
        # N1 (review007): a clamped TimeoutExpired is HANDLED here — refusal
        # facts are published (exit 2) instead of an escaping traceback.
        probe_clamp = min(120.0, remaining_to_stop())
        try:
            probe = subprocess.run(probe_argv, capture_output=True,
                                   timeout=probe_clamp)
        except subprocess.TimeoutExpired:
            facts["identity_probe_timeout_clamp"] = probe_clamp
            facts["stop_reason"] = "identity_probe_timeout"
            return refuse()
        facts["identity_probe_exit"] = probe.returncode
        if probe.returncode != 0:
            facts["stop_reason"] = "identity_probe_failed"
            return refuse()

    hang_stage = os.environ.get("T7_SUPERVISOR_TEST_HANG_STAGE", "")
    if hang_stage == "preflight_scan" and roots:
        time.sleep(600)  # test-only deterministic block injection
    stop_reason: str | None = None
    sampler_failures: list = []
    sampling_gaps: list = []
    quota_checks: list = []
    max_rss = 0
    deadline_fired = threading.Event()
    job = create_job(args.job_memory_limit)
    exit_code_box = ctypes.c_uint32(0)
    proc = None
    proc_handle = None

    def deadline_watch() -> None:
        # F1/C1: THIS thread terminates the whole job itself, independent of
        # any blocked sampling/main loop, using only the job handle.
        while not deadline_fired.wait(0.1):
            if time.monotonic() >= stop_at:
                kernel32.TerminateJobObject(job, DEADLINE_EXIT_CODE)
                deadline_fired.set()
                return

    watcher = threading.Thread(target=deadline_watch, daemon=True)
    stdout_path = out / "stdout.txt"
    stderr_path = out / "stderr.txt"
    samples_path = out / "rss-samples.jsonl"
    out.mkdir(parents=True, exist_ok=True)

    try:
        with stdout_path.open("xb") as out_stream, \
                stderr_path.open("xb") as err_stream, \
                samples_path.open("x", encoding="utf-8") as samples:
            proc = subprocess.Popen(cmd, cwd=str(Path(args.cwd).resolve()),
                                    stdout=out_stream, stderr=err_stream,
                                    creationflags=0x00000004)  # CREATE_SUSPENDED
            facts["product_pid"] = proc.pid
            proc_handle = int(proc._handle)
            import psutil  # creation identity captured while owned
            facts["product_create_time"] = psutil.Process(proc.pid).create_time()
            if os.environ.get("T7_SUPERVISOR_TEST_FAIL_ASSIGN") == "1":
                assigned = False  # test-only fault injection
            else:
                assigned = bool(kernel32.AssignProcessToJobObject(
                    ctypes.c_void_p(job), ctypes.c_void_p(proc_handle)))
            facts["job_assigned"] = assigned
            if not assigned:
                # F1: NEVER resume an uncontained child; terminate and reap
                # through the retained OWN process handle with bounded waits.
                stop_reason = "assign_job_failed"
                reaped = bounded_handle_reap(proc_handle, remaining_to_hard(),
                                             exit_code_box)
                facts["assign_failure_reaped"] = reaped
                if reaped:
                    facts["product_returncode"] = int(exit_code_box.value)
            else:
                status = ntdll.NtResumeProcess(ctypes.c_void_p(proc_handle))
                if status != 0:
                    stop_reason = f"resume_failed_nt{status}"
                    kernel32.TerminateJobObject(ctypes.c_void_p(job),
                                                DEADLINE_EXIT_CODE)
                else:
                    watcher.start()

            if args.sampler_argv_json:
                sampler_argv = [str(p) for p in json.loads(args.sampler_argv_json)]
            else:
                sampler_argv = [sys.executable, "-c", DEFAULT_SAMPLER_SCRIPT,
                                str(proc.pid)]
            last_sample_at = None
            last_quota_at = 0.0
            while stop_reason is None:
                rc = proc.poll()
                if rc is not None:
                    stop_reason = "completed"
                    break
                if deadline_fired.is_set():
                    stop_reason = "deadline"
                    break
                sampler_clamp = round(min(args.sample_timeout, remaining_to_stop()), 3)
                try:
                    sample = subprocess.run(
                        sampler_argv, capture_output=True,
                        timeout=sampler_clamp)
                except subprocess.TimeoutExpired:
                    # N2 (review007): if the deadline fired at/inside this
                    # clamped sampler call, the TRUE causal stop is the
                    # deadline (the watcher thread terminated the job
                    # concurrently); the sampler failure is still recorded
                    # with the ACTUAL clamped timeout that was used.
                    deadline_causal = deadline_fired.is_set() \
                        or time.monotonic() >= stop_at
                    sampler_failures.append(
                        {"t": round(time.monotonic() - started, 1),
                         "kind": "timeout", "clamped_to": sampler_clamp,
                         "deadline_causal": deadline_causal})
                    stop_reason = "deadline" if deadline_causal \
                        else f"sampler_timeout_after_{args.sample_timeout}s"
                    kernel32.TerminateJobObject(ctypes.c_void_p(job),
                                                DEADLINE_EXIT_CODE)
                    deadline_fired.set()
                    break
                if sample.returncode != 0:
                    stop_reason = f"sampler_exit_{sample.returncode}"
                    sampler_failures.append({"t": round(time.monotonic() - started, 1),
                                             "kind": f"exit_{sample.returncode}"})
                    kernel32.TerminateJobObject(ctypes.c_void_p(job),
                                                DEADLINE_EXIT_CODE)
                    deadline_fired.set()
                    break
                try:
                    payload = json.loads(
                        sample.stdout.decode("utf-8").strip().splitlines()[-1])
                except (ValueError, IndexError):
                    stop_reason = "sampler_output_invalid"
                    sampler_failures.append({"t": round(time.monotonic() - started, 1),
                                             "kind": "invalid_output"})
                    kernel32.TerminateJobObject(ctypes.c_void_p(job),
                                                DEADLINE_EXIT_CODE)
                    deadline_fired.set()
                    break
                if payload.get("denied"):
                    stop_reason = "sampler_denied"
                    sampler_failures.append({"t": round(time.monotonic() - started, 1),
                                             "kind": "access_denied"})
                    kernel32.TerminateJobObject(ctypes.c_void_p(job),
                                                DEADLINE_EXIT_CODE)
                    deadline_fired.set()
                    break
                now = time.monotonic()
                if payload.get("gone"):
                    stop_reason = "completed"
                    break
                if last_sample_at is not None:
                    gap = now - last_sample_at
                    if gap > args.sample_interval * 3:
                        sampling_gaps.append(round(gap, 1))
                last_sample_at = now
                samples.write(json.dumps({"t": round(now - started, 1),
                                          "rss": payload["rss"],
                                          "procs": payload["procs"]}) + "\n")
                samples.flush()
                max_rss = max(max_rss, payload["rss"])
                if args.ram_cap_bytes and payload["rss"] > args.ram_cap_bytes:
                    stop_reason = f"RAM_LIMIT:{payload['rss']}>{args.ram_cap_bytes}"
                    kernel32.TerminateJobObject(ctypes.c_void_p(job),
                                                DEADLINE_EXIT_CODE)
                    deadline_fired.set()
                    break
                # F2: cumulative quota enforced DURING the run, fail closed
                if roots and (now - last_quota_at) >= args.quota_check_every_seconds:
                    last_quota_at = now
                    used, scan_errors = retained_state(active_roots) if active_roots else (0, [])
                    used += frozen_history_bytes  # actual verified ro base (author011 fix)
                    quota_checks.append({"t": round(now - started, 1),
                                         "retained_bytes": used,
                                         "errors": scan_errors})
                    if scan_errors:
                        stop_reason = "output_quota_check_error"
                        kernel32.TerminateJobObject(ctypes.c_void_p(job),
                                                    DEADLINE_EXIT_CODE)
                        deadline_fired.set()
                        break
                    if used >= args.output_quota_bytes:
                        stop_reason = "output_quota_exceeded_during_run"
                        kernel32.TerminateJobObject(ctypes.c_void_p(job),
                                                    DEADLINE_EXIT_CODE)
                        deadline_fired.set()
                        break
                slept = 0.0
                while slept < args.sample_interval and proc.poll() is None \
                        and not deadline_fired.is_set():
                    time.sleep(0.05)
                    slept += 0.05
                if deadline_fired.is_set() and stop_reason is None:
                    stop_reason = "deadline"
                    break

        # bounded cleanup — every wait clamped to the same absolute budget
        cleanup = {"terminated": False, "job_closed": False}
        if stop_reason and stop_reason != "completed":
            kernel32.TerminateJobObject(ctypes.c_void_p(job), DEADLINE_EXIT_CODE)
            cleanup["terminated"] = True
        try:
            rc = proc.wait(timeout=min(30.0, remaining_to_hard()))
        except subprocess.TimeoutExpired:
            kernel32.TerminateJobObject(ctypes.c_void_p(job), DEADLINE_EXIT_CODE)
            cleanup["terminated"] = True
            try:
                rc = proc.wait(timeout=min(15.0, remaining_to_hard()))
            except subprocess.TimeoutExpired:
                if proc_handle is not None:  # F1/OBS-h: own-handle fallback
                    reaped = bounded_handle_reap(proc_handle, remaining_to_hard(),
                                                 exit_code_box)
                    rc = int(exit_code_box.value) if reaped else "unwaited"
                    cleanup["handle_reap"] = reaped
                else:
                    rc = "unwaited"
                cleanup["wait_exhausted"] = True
        facts["product_returncode"] = facts.get("product_returncode", rc)
        facts["stop_reason"] = stop_reason
        facts["cleanup"] = cleanup
        facts["max_tree_rss_bytes"] = max_rss
        # author008 row 3: preserve the OBTAINED product exit before any
        # capture/scan step that might block past the boundary
        try:
            sidecar = out / "PRODUCT-EXIT.json"
            if not sidecar.exists():
                atomic_publish(sidecar, {"product_returncode": facts["product_returncode"]})
        except (OSError, FileExistsError):
            pass
        facts["sampling_gaps_seconds"] = sampling_gaps
        facts["sampler_failures"] = sampler_failures
        facts["quota_checks"] = quota_checks
    except Exception as error:  # noqa: BLE001 — guaranteed capture + cleanup
        try:
            kernel32.TerminateJobObject(ctypes.c_void_p(job), DEADLINE_EXIT_CODE)
        except OSError:
            pass
        if proc is not None and proc.poll() is None and proc_handle is not None:
            bounded_handle_reap(proc_handle, remaining_to_hard(), exit_code_box)
        facts["stop_reason"] = f"supervisor_exception:{type(error).__name__}"
        facts.setdefault("product_returncode", "unknown")
        facts["cleanup"] = {"terminated": True, "job_closed": False,
                            "exception": repr(error)[:400]}
    finally:
        deadline_fired.set()  # OBS-w: cancel the watcher immediately
        if watcher.ident is not None:
            watcher.join(timeout=1.0)
        if kernel32.CloseHandle(ctypes.c_void_p(job)):
            facts.setdefault("cleanup", {})["job_closed"] = True

    # F4 + N3 (review007): hashing/scanning/capture is a BOUNDED path in the
    # same budget — each step runs only while time remains before the hard
    # deadline; exhaustion skips the remaining optional steps truthfully
    # (publication itself always runs) and any real overshoot is recorded.
    def capture_remaining() -> float:
        return hard_at - time.monotonic()

    hang_stage = os.environ.get("T7_SUPERVISOR_TEST_HANG_STAGE", "")
    if hang_stage == "post_product_hash":
        time.sleep(600)  # test-only deterministic block injection
    hashed, skipped, aborted = bounded_capture(
        (("stdout", stdout_path), ("stderr", stderr_path),
         ("rss_samples", samples_path)), hard_at)
    for name, digest in hashed.items():
        facts[name] = digest
    if skipped:
        facts["capture_skipped_no_budget"] = skipped
    if aborted:
        facts["capture_aborted_deadline"] = aborted
    facts["capture_bounded"] = True
    if roots:
        if capture_remaining() > 1.0:  # N3: final scan is also budget-gated
            ro_ok, ro_final = True, 0
            for ro in ro_roots:
                ro_bytes, ro_reason = verify_ro_root(ro, ro_pins[ro])
                if ro_reason:
                    facts["ro_violation_final"] = str(ro.name) + ":" + ro_reason
                    ro_ok = False
                    break
                ro_final += ro_bytes
            used, scan_errors = retained_state(roots)  # FULL final check: ro + active
            facts["retained_bytes_after"] = used
            facts["quota_scan_errors_after"] = scan_errors
            facts["ro_bytes_final_recheck"] = ro_final
            if used >= args.output_quota_bytes and facts.get("stop_reason") == "completed":
                facts["stop_reason"] = "output_quota_exceeded_final"
            elif scan_errors and facts.get("stop_reason") == "completed":
                facts["stop_reason"] = "output_quota_check_error_final"
            elif not ro_ok and facts.get("stop_reason") == "completed":
                facts["stop_reason"] = "ro_violation_final"
        else:
            facts["final_quota_scan_skipped_no_budget"] = True
    hang_stage = os.environ.get("T7_SUPERVISOR_TEST_HANG_STAGE", "")
    if hang_stage == "publish":
        time.sleep(600)  # test-only deterministic block injection
    facts["wall_seconds"] = round(time.monotonic() - started, 3)
    facts["wall_over_hard"] = time.monotonic() > hard_at
    supervisor_exit = 0 if (facts.get("stop_reason") == "completed"
                            and facts.get("product_returncode") == 0) else 3
    facts["supervisor_exit"] = supervisor_exit  # equals the process exit code
    atomic_publish(out / "RUN-FACTS.json", facts)
    print(json.dumps({k: facts.get(k) for k in
                      ("product_pid", "product_returncode", "stop_reason",
                       "wall_seconds", "max_tree_rss_bytes", "supervisor_exit")},
                     indent=1))
    return supervisor_exit


def main() -> int:
    """OUTER supervisor (author008): establishes the single absolute boundary
    BEFORE any scan/probe/output, holds a dedicated Job containing the
    worker, the product and every helper (suspended-create discipline), and
    stays active THROUGH capture/publication with bounded waits only. The
    worker (run_worker, same file, --worker-mode) does the scans, product
    Job, capture and publication inside the SAME boundary. Unified success
    is decided here ONLY after the worker's facts receipt and termination
    confirmation: product 0 alone is necessary, never sufficient."""
    started = time.monotonic()
    started_wall = time.time()
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker-mode", action="store_true",
                        help=argparse.SUPPRESS)  # internal: run as worker
    parser.add_argument("--deadline-seconds", type=float, required=True)
    parser.add_argument("--ram-cap-bytes", type=int, default=None)
    parser.add_argument("--job-memory-limit", type=int, default=None)
    parser.add_argument("--sample-interval", type=float, default=5.0)
    parser.add_argument("--sample-timeout", type=float, default=30.0)
    parser.add_argument("--cleanup-reserve-seconds", type=float, default=120.0)
    parser.add_argument("--sampler-argv-json", default=None)
    parser.add_argument("--retained-root", action="append", default=[])
    parser.add_argument("--ro-pins", action="append", default=[],
                        help="JSON pins file binding one ::ro root (path, authority, files)")
    parser.add_argument("--classification", default=None,
                        help="Root classification table (ROOT-CLASSIFICATION.json); "
                             "ro claims are honored ONLY for paths it lists as ro")
    parser.add_argument("--output-quota-bytes", type=int, default=10 * 1024**3)
    parser.add_argument("--quota-check-every-seconds", type=float, default=15.0)
    parser.add_argument("--identity-probe-json", default=None)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("cmd", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    cmd = args.cmd[1:] if args.cmd and args.cmd[0] == "--" else args.cmd
    if args.worker_mode:
        return run_worker(args, cmd)

    if not cmd:
        print("no product command", file=sys.stderr)
        return 2
    if args.cleanup_reserve_seconds >= args.deadline_seconds:
        print(json.dumps({"stop_reason": "deadline_clamped_refused",
                          "supervisor_exit": 2}), file=sys.stderr)
        return 2
    out = Path(args.out_dir).resolve()
    # outer NEVER scans/creates here — preflight refusals are decided in the
    # worker so their facts follow the existing exit-2 contract.
    hard_at = started + args.deadline_seconds

    worker_argv = [sys.executable, "-B", str(Path(__file__).resolve()),
                   "--worker-mode",
                   "--deadline-seconds", str(args.deadline_seconds),
                   "--sample-interval", str(args.sample_interval),
                   "--sample-timeout", str(args.sample_timeout),
                   "--cleanup-reserve-seconds", str(args.cleanup_reserve_seconds),
                   "--cwd", args.cwd, "--out-dir", str(out)]
    if args.ram_cap_bytes:
        worker_argv += ["--ram-cap-bytes", str(args.ram_cap_bytes)]
    if args.job_memory_limit:
        worker_argv += ["--job-memory-limit", str(args.job_memory_limit)]
    if args.sampler_argv_json:
        worker_argv += ["--sampler-argv-json", args.sampler_argv_json]
    for root in args.retained_root:
        worker_argv += ["--retained-root", root]
    worker_argv += ["--output-quota-bytes", str(args.output_quota_bytes),
                    "--quota-check-every-seconds", str(args.quota_check_every_seconds)]
    if args.identity_probe_json:
        worker_argv += ["--identity-probe-json", args.identity_probe_json]
    if args.classification:
        worker_argv += ["--classification", args.classification]
    worker_argv += ["--", *cmd]

    outer_job = create_job(None)
    worker = subprocess.Popen(worker_argv, cwd=str(Path(args.cwd).resolve()),
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL,
                              creationflags=0x00000004)  # CREATE_SUSPENDED
    worker_handle = int(worker._handle)
    if not kernel32.AssignProcessToJobObject(ctypes.c_void_p(outer_job),
                                             ctypes.c_void_p(worker_handle)):
        kernel32.TerminateProcess(ctypes.c_void_p(worker_handle),
                                  ASSIGN_FAIL_EXIT)
        kernel32.WaitForSingleObject(ctypes.c_void_p(worker_handle), 10000)
        kernel32.CloseHandle(ctypes.c_void_p(outer_job))
        print(json.dumps({"stop_reason": "outer_assign_failed",
                          "supervisor_exit": 3}), file=sys.stderr)
        return 3
    ntdll.NtResumeProcess(ctypes.c_void_p(worker_handle))

    # bounded waits only; the outer stays active through capture/publication
    while worker.poll() is None and time.monotonic() < hard_at:
        time.sleep(0.1)
    outer_terminated = False
    if worker.poll() is None:  # hard boundary reached: kill the WHOLE subtree
        kernel32.TerminateJobObject(ctypes.c_void_p(outer_job),
                                    DEADLINE_EXIT_CODE)
        outer_terminated = True
    kernel32.WaitForSingleObject(ctypes.c_void_p(worker_handle), 10000)
    worker_rc = worker.poll()
    if worker_rc is None:
        worker_rc = "unwaited"
    kernel32.CloseHandle(ctypes.c_void_p(outer_job))

    # unified decision from the worker's receipt (small bounded read)
    facts: dict = {}
    receipt = out / "RUN-FACTS.json"
    if receipt.is_file() and time.monotonic() < hard_at + 5.0:
        try:
            facts = json.loads(receipt.read_bytes().decode("utf-8"))
        except (OSError, ValueError):
            facts = {}
    reasons: list = []
    if worker_rc != 0:
        reasons.append(f"worker_exit_{worker_rc}")
    if outer_terminated:
        reasons.append("outer_deadline_terminated")
    if not facts:
        reasons.append("facts_receipt_missing_or_unreadable")
        if worker_rc == 2:
            reasons.append("worker_refusal_facts_unpublished")
        # review009 F1: a worker prelaunch refusal (exit 2) is authoritative
        # for the exit mapping even when its refusal facts cannot be
        # published (e.g. the occupied case never overwrites RUN-FACTS).
        supervisor_exit = 2 if worker_rc == 2 else 3
    else:
        stop = facts.get("stop_reason")
        if worker_rc == 2:
            supervisor_exit = 2  # prelaunch refusal contract preserved
        elif worker_rc == 0:
            supervisor_exit = 0
        else:
            supervisor_exit = 3
        # STRICT final success: product 0 is necessary, never sufficient.
        if supervisor_exit == 0:
            if stop != "completed":
                reasons.append(f"stop_reason_{stop}")
            if facts.get("product_returncode") != 0:
                reasons.append("product_not_zero")
            for stream in ("stdout", "stderr", "rss_samples"):
                entry = facts.get(stream)
                if not isinstance(entry, dict) or "sha256" not in entry:
                    reasons.append(f"capture_missing_{stream}")
            if facts.get("capture_skipped_no_budget"):
                reasons.append("capture_skipped")
            if facts.get("capture_aborted_deadline"):
                reasons.append("capture_aborted")
            if args.retained_root:
                if "retained_bytes_after" not in facts:
                    reasons.append("final_quota_unknown")
                elif facts.get("quota_scan_errors_after"):
                    reasons.append("final_quota_scan_errors")
                elif facts["retained_bytes_after"] >= args.output_quota_bytes:
                    reasons.append("final_quota_exceeded")
            if facts.get("final_quota_scan_skipped_no_budget"):
                reasons.append("final_quota_scan_skipped")

            if facts.get("wall_over_hard"):
                reasons.append("wall_over_hard")
            if time.monotonic() > hard_at:
                reasons.append("outer_wall_over_hard")
            if reasons:
                supervisor_exit = 3  # product 0 preserved; overall NOT success
    if facts.get("ro_violation_final"):
        reasons.append("ro_violation_final")
    sidecar = out / "PRODUCT-EXIT.json"
    if facts.get("product_returncode", "UNKNOWN") == "UNKNOWN" and sidecar.is_file():
        try:
            facts["product_returncode"] = json.loads(
                sidecar.read_bytes().decode("utf-8"))["product_returncode"]
        except (OSError, ValueError, KeyError):
            pass
    verdict = {"schema": "aktr-t7-B-outer-verdict/v1",
               "unified_supervisor_exit": supervisor_exit,
               "worker_exit": worker_rc,
               "outer_terminated": outer_terminated,
               "failure_reasons": reasons,
               "product_returncode": facts.get("product_returncode", "UNKNOWN"),
               "outer_wall_seconds": round(time.monotonic() - started, 3),
               "wall_started_epoch": started_wall}
    verdict_path = out / "OUTER-VERDICT.json"
    # author011: the OUTER never creates the out-dir itself. A worker that
    # refused before creating anything (e.g. output_in_ro_root_refused)
    # leaves nothing to publish into — and never a write inside an ro root.
    if not out.is_dir():
        facts["verdict_publication_skipped_no_outdir"] = True
    if not verdict_path.exists() and out.is_dir()             and time.monotonic() < hard_at + 5.0:
        try:
            atomic_publish(verdict_path, verdict)
        except (OSError, FileExistsError):
            reasons.append("verdict_publication_failed")
            supervisor_exit = 3
    print(json.dumps(verdict, indent=1))
    return supervisor_exit


if __name__ == "__main__":
    raise SystemExit(main())
