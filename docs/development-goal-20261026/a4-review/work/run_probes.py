import hashlib, json, os, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

CAND = Path("C:/AKWorktrees/aktr-goal-a4-20261026")
DISP = Path("C:/AKWorktrees/aktr-goal-20261026/zcode-dispatch/a4-hard-review-001")
ROOT_PROBES = DISP / "evidence" / "probes-r1"
NL = bytes([10])
SCRIPT = CAND / "scripts/research_probability_v2.py"
FIXTURE = CAND / "tests/fixtures/v2/shared.json"
CONFIG = CAND / "tests/fixtures/v2/m5_research.json"
FEED = CAND / "tests/fixtures/v2/m5_stream.jsonl"

for label, p in (("script", SCRIPT), ("fixture", FIXTURE), ("config", CONFIG), ("feed", FEED)):
    if not p.is_file():
        raise SystemExit("REQUIRED_INPUT_MISSING " + label + " " + str(p))

GUARD_FAILURE = {"mode": "ERROR", "exit_code": 3, "reason": "ORDINARY_SHADOW_REPLAY_NOT_AVAILABLE",
                 "engineering": "FAIL", "statistics": "INSUFFICIENT", "runtime": "OFFLINE_ONLY",
                 "m5_complete": False}

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1048576), b""):
            h.update(chunk)
    return h.hexdigest()

def listing(root):
    entries = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_dir():
            entries.append({"rel": rel, "type": "dir"})
        elif path.is_file():
            entries.append({"rel": rel, "type": "file", "bytes": path.stat().st_size,
                            "sha256": sha256_file(path)})
    return entries

ROOT_PROBES.mkdir(parents=True, exist_ok=True)

def run_probe(pid, title, args_builder, expected_exit, setup=None):
    pdir = ROOT_PROBES / pid
    pdir.mkdir(exist_ok=False)
    scratch = pdir / "scratch"
    scratch.mkdir()
    sentinel_rel = None
    if setup == "file_parent":
        (scratch / "P06_parent").write_bytes(b"SENTINEL-A4HR-001" + NL)
        sentinel_rel = "P06_parent"
    elif setup == "existing_dir":
        outdir = scratch / "P07_out"
        outdir.mkdir()
        (outdir / "sentinel.json").write_bytes(b"SENTINEL-A4HR-001" + NL)
        sentinel_rel = "P07_out/sentinel.json"
    sentinel_before = None
    if sentinel_rel is not None:
        sentinel_before = sha256_file(scratch / sentinel_rel)
    argv = [sys.executable, "-B", "-X", "utf8", str(SCRIPT)] + args_builder(scratch)
    started = time.monotonic()
    timed_out = False
    try:
        completed = subprocess.run(argv, cwd=str(scratch), stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, timeout=180)
        rc = completed.returncode
        out_b = completed.stdout
        err_b = completed.stderr
    except subprocess.TimeoutExpired as exc:
        rc = None
        out_b = exc.stdout if isinstance(exc.stdout, bytes) else b""
        err_b = exc.stderr if isinstance(exc.stderr, bytes) else b""
        timed_out = True
    wall = round(time.monotonic() - started, 3)
    (pdir / "stdout.txt").write_bytes(out_b)
    (pdir / "stderr.txt").write_bytes(err_b)
    (pdir / "argv.json").write_bytes(json.dumps(argv).encode("utf-8") + NL)
    parsed_stderr = None
    stderr_json_valid = False
    try:
        parsed_stderr = json.loads(err_b.decode("utf-8"))
        stderr_json_valid = True
    except ValueError:
        parsed_stderr = None
    after = listing(scratch)
    sentinel_after = None
    if sentinel_rel is not None and (scratch / sentinel_rel).is_file():
        sentinel_after = sha256_file(scratch / sentinel_rel)
    error_json = None
    for entry in after:
        if entry["type"] == "file" and entry["rel"].endswith("/error.json"):
            try:
                error_json = json.loads((scratch / entry["rel"]).read_text(encoding="utf-8"))
            except ValueError:
                error_json = None
    record = {
        "id": pid,
        "title": title,
        "expected_exit": expected_exit,
        "argv": argv,
        "cwd": str(scratch),
        "timed_out": timed_out,
        "wall_seconds": wall,
        "returncode": rc,
        "exit_matches_expected": rc == expected_exit,
        "stdout_bytes": len(out_b),
        "stdout_empty": len(out_b) == 0,
        "stderr_bytes": len(err_b),
        "stderr_json_valid": stderr_json_valid,
        "stderr_json": parsed_stderr,
        "stderr_exact_guard_failure": parsed_stderr == GUARD_FAILURE if stderr_json_valid else False,
        "listing_after": after,
        "sentinel_rel": sentinel_rel,
        "sentinel_sha_before": sentinel_before,
        "sentinel_sha_after": sentinel_after,
        "sentinel_unchanged": sentinel_before is not None and sentinel_before == sentinel_after,
        "error_json": error_json,
    }
    return record

def MFIX(s):
    return str(s / "missing-fixture.json")

def MCFG(s):
    return str(s / "missing-config.json")

def MFEED(s):
    return str(s / "missing-feed.jsonl")

probes = []
probes.append(run_probe("P01", "relation precedence: shadow-replay without --feed is a relationship error before the guard (exit 2)",
    lambda s: ["--mode", "shadow-replay", "--fixture", MFIX(s), "--config", MCFG(s),
               "--output", str(s / "P01out")], 2))
probes.append(run_probe("P02", "relation precedence: shadow-replay plus --reference-run is a relationship error (exit 2)",
    lambda s: ["--mode", "shadow-replay", "--fixture", MFIX(s), "--config", MCFG(s),
               "--output", str(s / "P02out"), "--feed", MFEED(s),
               "--reference-run", str(s / "missing-ref")], 2))
probes.append(run_probe("P03", "sources mutually exclusive: --fixture plus --input-bundle is an argparse error (exit 2)",
    lambda s: ["--mode", "shadow-replay", "--fixture", MFIX(s), "--input-bundle", str(s / "missing-bundle"),
               "--config", MCFG(s), "--output", str(s / "P03out"), "--feed", MFEED(s)], 2))
probes.append(run_probe("P04", "invalid mode keeps argparse native exit 2",
    lambda s: ["--mode", "live", "--fixture", MFIX(s), "--config", MCFG(s),
               "--output", str(s / "P04out")], 2))
probes.append(run_probe("P05", "guard with ALL inputs missing and output parent nonexistent: exit 3, nothing created",
    lambda s: ["--mode", "shadow-replay", "--fixture", MFIX(s), "--config", MCFG(s),
               "--output", str(s / "P05_deep" / "missing_parent" / "run"),
               "--feed", MFEED(s)], 3))
probes.append(run_probe("P06", "guard with output parent being an existing FILE (unusable output): exit 3, sentinel untouched",
    lambda s: ["--mode", "shadow-replay", "--fixture", str(FIXTURE), "--config", str(CONFIG),
               "--output", str(s / "P06_parent" / "run"), "--feed", str(FEED)], 3, setup="file_parent"))
probes.append(run_probe("P07", "guard beats output_directory_exists: pre-created output dir left byte-identical, no error.json",
    lambda s: ["--mode", "shadow-replay", "--fixture", str(FIXTURE), "--config", str(CONFIG),
               "--output", str(s / "P07_out"), "--feed", str(FEED)], 3, setup="existing_dir"))
probes.append(run_probe("P08", "guard fires on a complete legal request with real fixture/config/feed: exit 3, no output dir",
    lambda s: ["--mode", "shadow-replay", "--fixture", str(FIXTURE), "--config", str(CONFIG),
               "--output", str(s / "P08_out"), "--feed", str(FEED)], 3))
probes.append(run_probe("P09", "normal mode unchanged: smoke reads config and writes error.json on missing config (exit 2)",
    lambda s: ["--mode", "smoke", "--fixture", str(FIXTURE), "--config", MCFG(s),
               "--output", str(s / "P09_out")], 2))
probes.append(run_probe("P10", "normal mode unchanged: reproduce with reference-run enters pipeline, missing config exit 2 plus error.json",
    lambda s: ["--mode", "reproduce", "--fixture", str(FIXTURE), "--config", MCFG(s),
               "--reference-run", str(s / "missing-ref"), "--output", str(s / "P10_out")], 2))
probes.append(run_probe("P11", "guard via --input-bundle source variant: exit 3, nothing created",
    lambda s: ["--mode", "shadow-replay", "--input-bundle", str(s / "missing-bundle"),
               "--config", MCFG(s), "--output", str(s / "P11_out"), "--feed", MFEED(s)], 3))

summary = {
    "task": "reviewer-counterexample-probes",
    "generated_utc": datetime.now(timezone.utc).isoformat(),
    "script_under_test": str(SCRIPT),
    "probe_count": len(probes),
    "probes": probes,
}
text = json.dumps(summary, indent=2)
json.loads(text)
tmp = ROOT_PROBES / "PROBES-SUMMARY.json.tmp"
tmp.write_bytes(text.encode("utf-8") + NL)
os.replace(tmp, ROOT_PROBES / "PROBES-SUMMARY.json")
for p in probes:
    print(p["id"], "rc", p["returncode"], "exp", p["expected_exit"], "match", p["exit_matches_expected"],
          "stdout_empty", p["stdout_empty"], "guard_json", p["stderr_exact_guard_failure"],
          "files_after", len(p["listing_after"]), "sentinel_ok", p["sentinel_unchanged"],
          "wall", p["wall_seconds"])
