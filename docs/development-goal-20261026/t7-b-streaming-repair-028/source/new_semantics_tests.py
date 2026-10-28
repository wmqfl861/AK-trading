"""author011 §5 small-root regressions — SYNTHETIC roots, short children.

Covers: ro+active positive end-to-end (child/worker/outer success, complete
capture, consistent start/cycle/final accounting); ro-base + active growth
crossing quota DURING the run (must fail — exposes the 026 zero-base bug);
bare ::ro refused; wrong/missing pins refused; out inside ro refused;
cross-category overlap refused; ro member growth and content change fail
even with child 0; active nesting refused; existing unmarked semantics
retained (2 prior tests, separate file).
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

WORK = Path(__file__).resolve().parent
SUPERVISOR = WORK / "run_daily_bounded.py"


def make_ro_root(tmp: Path, name: str, files: dict[str, bytes], authority_record: str = "D:/synthetic/terminal-record.json") -> tuple[Path, Path]:
    root = tmp / name
    root.mkdir(parents=True)
    for rel, data in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    pins = {
        "root": str(root),
        "authority": {"record": authority_record, "confirmed_no_writer": True},
        "files": {},
    }
    for path in sorted(root.rglob("*")):
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            raw = path.read_bytes()
            pins["files"][rel] = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    pins_path = tmp / (name + ".pins.json")
    pins_path.write_text(json.dumps(pins, indent=1), encoding="utf-8")
    return root, pins_path


def run_supervisor(tmp_path: Path, argv: list[str], **kw) -> dict:
    out = kw.pop("out", tmp_path / "run")
    cmd = [sys.executable, "-B", str(SUPERVISOR),
           "--deadline-seconds", str(kw.get("deadline", 30)),
           "--sample-interval", str(kw.get("interval", 1.0)),
           "--sample-timeout", str(kw.get("sample_timeout", 10)),
           "--cleanup-reserve-seconds", str(kw.get("reserve", 8)),
           "--quota-check-every-seconds", str(kw.get("quota_every", 1.0)),
           "--cwd", str(WORK), "--out-dir", str(out)]
    for r in kw.get("roots", []):
        cmd += ["--retained-root", r]
    if kw.get("classification"):
        cmd += ["--classification", str(kw["classification"])]
    if kw.get("quota"):
        cmd += ["--output-quota-bytes", str(kw["quota"])]
    cmd += ["--", *argv]
    env = {**os.environ, **kw.get("env", {})}
    t0 = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=kw.get("outer", 60), env=env)
    facts = {"_supervisor_returncode": proc.returncode,
             "_supervisor_stdout": proc.stdout, "_supervisor_stderr": proc.stderr,
             "_out": str(out), "_outer_wall": round(time.monotonic() - t0, 2)}
    facts_path = Path(out) / "RUN-FACTS.json"
    if facts_path.is_file():
        try:
            facts.update(json.loads(facts_path.read_bytes()))
        except ValueError:
            pass
    verdict_path = Path(out) / "OUTER-VERDICT.json"
    if verdict_path.is_file():
        try:
            facts["_verdict"] = json.loads(verdict_path.read_bytes())
        except ValueError:
            pass
    return facts




def write_synthetic_classification(tmp_path: Path, ro_root: Path,
                                   active_roots: list[Path]) -> Path:
    """Explicitly SYNTHETIC classification table for the ro-semantics tests
    only — never a real Root approval (author012 §2)."""
    table = {"schema": "synthetic-test-classification/v1",
             "ro_roots": [{"path": str(ro_root), "classification": "ro",
                           "synthetic": True}],
             "active_roots": [{"path": str(a), "classification": "active",
                               "synthetic": True} for a in active_roots]}
    path = tmp_path / "synthetic-classification.json"
    path.write_text(json.dumps(table), encoding="utf-8")
    return path

def test_ro_active_positive_end_to_end(tmp_path):
    ro, pins = make_ro_root(tmp_path, "rohist", {"a.bin": b"R" * 3000, "sub/b.bin": b"R" * 2000})
    active = tmp_path / "active"
    active.mkdir()
    out = active / "run"
    table = write_synthetic_classification(tmp_path, ro, [active])
    facts = run_supervisor(tmp_path, [sys.executable, "-c", "print('ok')"],
                           deadline=25, quota=10 * 1024, quota_every=0.3,
                           classification=table,
                           roots=[str(ro) + "::ro::" + str(pins), str(active)],
                           out=out)
    assert facts["stop_reason"] == "completed"
    assert facts["product_returncode"] == 0
    assert facts["_supervisor_returncode"] == 0
    v = facts["_verdict"]
    assert v["unified_supervisor_exit"] == 0 and v["failure_reasons"] == []
    assert facts["ro_bytes_verified_base"] == 5000
    assert facts["retained_bytes_before"] >= 5000
    out_dir = Path(facts["_out"])
    worker_files = (facts["stdout"]["bytes"] + facts["stderr"]["bytes"]
                   + facts["rss_samples"]["bytes"]
                   + (out_dir / "PRODUCT-EXIT.json").stat().st_size)
    delta = facts["retained_bytes_after"] - facts["retained_bytes_before"]
    assert delta == worker_files  # exactly the worker-time outputs grew
    assert facts["ro_bytes_final_recheck"] == 5000


def test_ro_base_plus_growth_crosses_quota_during_run(tmp_path):
    # MUST fail during the run: 026 added base 0, so it wrongly passed.
    ro, pins = make_ro_root(tmp_path, "robase", {"base.bin": b"B" * 8000})
    active = tmp_path / "active"
    active.mkdir()
    out = active / "run"
    grower = (f"import time\n"
              f"open(r'{active / 'grown.bin'}','wb').write(b'G' * 4000)\n"
              f"time.sleep(15)\n")
    table = write_synthetic_classification(tmp_path, ro, [active])
    facts = run_supervisor(tmp_path, [sys.executable, "-c", grower],
                           deadline=25, reserve=5, quota=9000, quota_every=0.5,
                           classification=table,
                           roots=[str(ro) + "::ro::" + str(pins), str(active)],
                           out=out)
    assert facts["stop_reason"] == "output_quota_exceeded_during_run"
    assert facts["_supervisor_returncode"] == 3
    assert facts["product_returncode"] == 130
    assert any(c["retained_bytes"] >= 9000 for c in facts["quota_checks"])


def test_bare_ro_refused(tmp_path):
    active = tmp_path / "active"
    active.mkdir()
    out = active / "run"
    facts = run_supervisor(tmp_path, [sys.executable, "-c", "print('never')"],
                           deadline=15, roots=[str(tmp_path / "x") + "::ro", str(active)],
                           out=out)
    assert facts["stop_reason"] == "ro_credentials_missing_refused"
    assert facts["_supervisor_returncode"] == 2
    assert facts.get("product_pid") is None


def test_wrong_pins_content_refused(tmp_path):
    ro, pins = make_ro_root(tmp_path, "rocontent", {"f.bin": b"C" * 1000})
    (ro / "f.bin").write_bytes(b"TAMPERED" * 100)  # content drift after pins
    active = tmp_path / "active"
    active.mkdir()
    out = active / "run"
    table = write_synthetic_classification(tmp_path, ro, [active])
    facts = run_supervisor(tmp_path, [sys.executable, "-c", "print('never')"],
                           deadline=15, classification=table,
                           roots=[str(ro) + "::ro::" + str(pins), str(active)],
                           out=out)
    assert facts["stop_reason"] == "ro_pins_content_mismatch_refused"
    assert facts["_supervisor_returncode"] == 2
    assert facts.get("product_pid") is None
    assert not (out / "stdout.txt").exists()  # refusal wrote nothing


def test_pins_member_growth_refused(tmp_path):
    ro, pins = make_ro_root(tmp_path, "romember", {"f.bin": b"M" * 500})
    (ro / "extra.bin").write_bytes(b"NEW")  # member drift after pins
    active = tmp_path / "active"
    active.mkdir()
    out = active / "run"
    table = write_synthetic_classification(tmp_path, ro, [active])
    facts = run_supervisor(tmp_path, [sys.executable, "-c", "print('never')"],
                           deadline=15, classification=table,
                           roots=[str(ro) + "::ro::" + str(pins), str(active)],
                           out=out)
    assert facts["stop_reason"] == "ro_pins_member_mismatch_refused"
    assert facts["_supervisor_returncode"] == 2


def test_pins_authority_incomplete_refused(tmp_path):
    ro, pins_path = make_ro_root(tmp_path, "roauth", {"f.bin": b"A" * 300})
    pins = json.loads(pins_path.read_bytes())
    pins["authority"]["confirmed_no_writer"] = False
    pins_path.write_text(json.dumps(pins), encoding="utf-8")
    active = tmp_path / "active"
    active.mkdir()
    out = active / "run"
    table = write_synthetic_classification(tmp_path, ro, [active])
    facts = run_supervisor(tmp_path, [sys.executable, "-c", "print('never')"],
                           deadline=15, classification=table,
                           roots=[str(ro) + "::ro::" + str(pins_path), str(active)],
                           out=out)
    assert facts["stop_reason"] == "ro_pins_authority_incomplete_refused"
    assert facts["_supervisor_returncode"] == 2


def test_output_inside_ro_refused_no_ro_write(tmp_path):
    ro, pins = make_ro_root(tmp_path, "roout", {"f.bin": b"O" * 200})
    ro_before = sorted(p.name for p in ro.iterdir())
    table = write_synthetic_classification(tmp_path, ro, [tmp_path / "active2"])
    facts = run_supervisor(tmp_path, [sys.executable, "-c", "print('never')"],
                           deadline=15, classification=table,
                           roots=[str(ro) + "::ro::" + str(pins), str(tmp_path / "active2")],
                           out=ro / "run")  # out INSIDE the ro root
    # no facts file exists (the refusal must not write into the ro root);
    # the stop reason travels on the CLI stream
    assert not (ro / "run" / "RUN-FACTS.json").exists()
    assert facts["_supervisor_returncode"] == 2
    assert "worker_refusal_facts_unpublished" in facts["_supervisor_stdout"]
    assert sorted(p.name for p in ro.iterdir()) == ro_before  # nothing written into ro


def test_cross_category_overlap_refused(tmp_path):
    ro, pins = make_ro_root(tmp_path, "roover", {"f.bin": b"V" * 100})
    active = tmp_path  # active root is a PARENT of the ro root
    out = active / "active-run"
    table = write_synthetic_classification(tmp_path, ro, [active])
    facts = run_supervisor(tmp_path, [sys.executable, "-c", "print('never')"],
                           deadline=15, classification=table,
                           roots=[str(ro) + "::ro::" + str(pins), str(active)],
                           out=out)
    assert facts["stop_reason"] == "cross_category_overlap_refused"
    assert facts["_supervisor_returncode"] == 2


def test_active_nested_overlap_refused(tmp_path):
    a1 = tmp_path / "a1"
    a2 = a1 / "a2"
    a1.mkdir()
    a2.mkdir()
    out = a1 / "run"
    facts = run_supervisor(tmp_path, [sys.executable, "-c", "print('never')"],
                           deadline=15, roots=[str(a1), str(a2)], out=out)
    assert facts["stop_reason"] == "active_category_nested_overlap_refused"
    assert facts["_supervisor_returncode"] == 2


def test_ro_change_after_start_fails_despite_child0(tmp_path):
    ro, pins = make_ro_root(tmp_path, "rolate", {"f.bin": b"L" * 400})
    active = tmp_path / "active"
    active.mkdir()
    out = active / "run"
    mutator = (f"import time\n"
               f"time.sleep(4)\n"
               f"open(r'{ro / 'f.bin'}','ab').write(b'X')\n"
               f"time.sleep(2)\n")
    table = write_synthetic_classification(tmp_path, ro, [active])
    facts = run_supervisor(tmp_path, [sys.executable, "-c", mutator],
                           deadline=25, reserve=6, quota=10 * 1024, quota_every=1.0,
                           classification=table,
                           roots=[str(ro) + "::ro::" + str(pins), str(active)],
                           out=out)
    assert facts["product_returncode"] == 0  # child itself succeeded
    assert facts["stop_reason"] == "ro_violation_final"
    assert facts["_supervisor_returncode"] == 3
    assert "ro_violation_final" in facts["_verdict"]["failure_reasons"]



def write_real_style_classification(tmp_path: Path, active_roots: list[Path]) -> Path:
    """Zero-ro table: every root active (Root's actual 028 fallback shape)."""
    table = {"schema": "synthetic-test-classification/v1",
             "ro_roots": [],
             "active_roots": [{"path": str(a), "classification": "active",
                               "synthetic": True} for a in active_roots]}
    path = tmp_path / "zero-ro-classification.json"
    path.write_text(json.dumps(table), encoding="utf-8")
    return path


def test_ro_claim_on_active_classified_root_fails(tmp_path):
    # author012: a claimed ro proof for a root the table classifies ACTIVE
    # is a mismatched-proof FAIL — never honored, never silently active.
    ro, pins = make_ro_root(tmp_path, "fakeproof", {"f.bin": b"P" * 100})
    active = tmp_path / "active"
    active.mkdir()
    out = active / "run"
    table = write_real_style_classification(tmp_path, [ro, active])
    facts = run_supervisor(tmp_path, [sys.executable, "-c", "print('never')"],
                           deadline=15, classification=table,
                           roots=[str(ro) + "::ro::" + str(pins), str(active)],
                           out=out)
    assert facts["stop_reason"] == "ro_claim_mismatched_classification_refused"
    assert facts["_supervisor_returncode"] == 2
    assert facts.get("product_pid") is None


def test_zero_ro_all_active_positive(tmp_path):
    # author012: zero ro is the valid fallback — everything active, full
    # per-cycle scans, normal success path end to end.
    a1 = tmp_path / "hist"
    a1.mkdir()
    (a1 / "h.bin").write_bytes(b"H" * 2000)
    active = tmp_path / "active"
    active.mkdir()
    out = active / "run"
    table = write_real_style_classification(tmp_path, [a1, active])
    facts = run_supervisor(tmp_path, [sys.executable, "-c", "print('zero-ro ok')"],
                           deadline=25, quota=10 * 1024, classification=table,
                           roots=[str(a1), str(active)], out=out)
    assert facts["stop_reason"] == "completed"
    assert facts["_supervisor_returncode"] == 0
    assert facts.get("ro_bytes_verified_base") in (None, 0)


def test_streaming_scan_counts_match_rglob(tmp_path):
    # author012 §5: the streaming scandir traversal must count exactly the
    # regular-file bytes the old rglob did — including in nested dirs, and
    # with symlinks seen-but-not-counted.
    import importlib.util
    spec = importlib.util.spec_from_file_location("sup", SUPERVISOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "scanroot"
    (root / "sub" / "deep").mkdir(parents=True)
    (root / "a.bin").write_bytes(b"A" * 1000)
    (root / "sub" / "b.bin").write_bytes(b"B" * 2000)
    (root / "sub" / "deep" / "c.bin").write_bytes(b"C" * 3000)
    (root / "empty-dir").mkdir()
    try:
        (root / "link.bin").symlink_to(root / "a.bin")
    except OSError:
        pass  # symlink creation may be unavailable; parity still testable
    streamed, errors = module.retained_state([root])
    expected = sum(p.stat().st_size for p in root.rglob("*")
                   if p.is_file() and not p.is_symlink())
    assert errors == []
    assert streamed == expected == 6000


def test_scan_error_propagates_fail_closed(tmp_path):
    # author012: scan errors fail — a vanished root mid-scan is an error,
    # never silently counted as zero.
    import importlib.util
    spec = importlib.util.spec_from_file_location("sup", SUPERVISOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    missing = tmp_path / "does-not-exist"
    total, errors = module.retained_state([missing])
    assert total == 0
    assert errors and errors[0].endswith(("FileNotFoundError", "OSError"))
