import hashlib, json, os, shutil
from datetime import datetime, timezone
from pathlib import Path

STAGE = Path("C:/AKWorktrees/aktr-goal-a4-20261026/a4hr-staging")
DISP = Path("C:/AKWorktrees/aktr-goal-20261026/zcode-dispatch/a4-hard-review-001")
EV = DISP / "evidence"
NL = bytes([10])

def load(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))

phase_a = load(EV / "phase-a-frozen-hashes-before.json")
phase_d = load(EV / "phase-d-frozen-hashes-after.json")
pins = phase_d
result = load(EV / "required-rerun-r1" / "RESULT.json")
probes = load(EV / "probes-r1" / "PROBES-SUMMARY.json")

assert phase_a["verified_ok"] == 266 and phase_a["mismatch_count"] == 0 and not phase_a["extra_files_not_pinned"]
assert phase_d["verified_ok"] == 266 and phase_d["mismatch_count"] == 0 and not phase_d["extra_files_not_pinned"]
gd = phase_a["guard_block_delta"]
assert gd["reconstructed_matches_prechange_pin_33351e7a"] is True
assert gd["current_script_matches_owner_pin_db0e61e3"] is True
assert gd["new_test_file"]["matches_owner_pin_ddae2262"] is True
assert result["returncode"] == 0 and "passed 18" in result["totals_line_last"] and result["violation"] is None
assert result["product_hashes"]["pre"] == result["product_hashes"]["post"]
assert probes["probe_count"] == 11 and all(p["exit_matches_expected"] for p in probes["probes"])
cvr = pins["computed_vs_request_pins"]
for key in ("WORKORDER.md", "PHASE0-PLAN.md", "PHASE0-DISPOSITION.md", "ROOT-FROZEN-INPUTS.json", "DELIVERY.md", "interpreter"):
    assert cvr[key]["request_pin_paths"], key

sub = {
    "@@GENERATED_UTC@@": datetime.now(timezone.utc).isoformat(),
    "@@SCRIPT_HASH@@": gd["current_script_sha256"],
    "@@TEST_HASH@@": gd["new_test_file"]["sha256"],
    "@@CLI_TEST_HASH@@": result["product_hashes"]["pre"]["test_v2_m5_cli"],
    "@@PRECHANGE_HASH@@": gd["reconstructed_sha256"],
    "@@RFI_HASH@@": cvr["ROOT-FROZEN-INPUTS.json"]["computed_sha256"],
    "@@DELIVERY_HASH@@": cvr["DELIVERY.md"]["computed_sha256"],
    "@@PLAN_HASH@@": cvr["PHASE0-PLAN.md"]["computed_sha256"],
    "@@DISPO_HASH@@": cvr["PHASE0-DISPOSITION.md"]["computed_sha256"],
    "@@WORKORDER_HASH@@": cvr["WORKORDER.md"]["computed_sha256"],
    "@@PYEXE_HASH@@": phase_a["interpreter"]["python_executable_sha256"],
    "@@RAW_EXIT@@": str(result["returncode"]),
    "@@WALL@@": str(result["wall_seconds"]),
    "@@RSS_BYTES@@": str(result["peak_tree_rss_bytes"]),
    "@@RSS_MIB@@": str(result["peak_tree_rss_mib"]),
    "@@SAMPLES@@": str(result["rss_samples"]),
    "@@TOTALS_LINE@@": result["totals_line_last"],
    "@@OVERALL_LINE@@": result["overall_exit_line_last"],
    "@@PROBE_COUNT@@": str(probes["probe_count"]),
}

def apply(text):
    for k, v in sub.items():
        text = text.replace(k, v)
    return text

targets = {}
for name in ("REVIEW.md", "FINDINGS.json", "VERDICT.json"):
    raw = (STAGE / name).read_text(encoding="utf-8")
    out = apply(raw)
    if "@@" in out:
        raise SystemExit("UNSUBSTITUTED_PLACEHOLDER in " + name)
    if name.endswith(".json"):
        json.loads(out)
    target = DISP / name
    if target.exists():
        raise SystemExit("TARGET_ALREADY_EXISTS " + str(target))
    targets[name] = out.encode("utf-8")

for name, data in targets.items():
    tmp = (DISP / name).with_suffix((DISP / name).suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, DISP / name)
    print("PUBLISHED", name, len(data), "bytes sha256", hashlib.sha256(data).hexdigest())

shutil.rmtree(STAGE)
print("staging removed")
print("verdict", json.loads(targets["VERDICT.json"])["verdict"])
