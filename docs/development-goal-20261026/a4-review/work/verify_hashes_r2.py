import hashlib, json, os, sys
from datetime import datetime, timezone
from pathlib import Path

CAND = Path("C:/AKWorktrees/aktr-goal-a4-20261026")
DISP = Path("C:/AKWorktrees/aktr-goal-20261026/zcode-dispatch/a4-hard-review-001")
PINS_FILE = DISP / "ROOT-FROZEN-INPUTS.json"
OUT = DISP / "evidence" / "phase-a-frozen-hashes-before.json"
NL = bytes([10])

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1048576), b""):
            h.update(chunk)
    return h.hexdigest()

pins = json.loads(PINS_FILE.read_text(encoding="utf-8"))
entries = pins["files"]
results = []
mismatch = []
for entry in entries:
    path = Path(entry["path"])
    record = {"path": str(path), "pinned_bytes": entry["bytes"], "pinned_sha256": entry["sha256"]}
    if not path.is_file():
        record["status"] = "MISSING"
    else:
        size = path.stat().st_size
        digest = sha256_file(path)
        record["actual_bytes"] = size
        record["actual_sha256"] = digest
        record["status"] = "OK" if (size == entry["bytes"] and digest == entry["sha256"]) else "MISMATCH"
    if record["status"] != "OK":
        mismatch.append(record)
    results.append(record)

walked = set()
for root, dirs, names in os.walk(CAND):
    for name in names:
        walked.add(str(Path(root, name).resolve()))
pinned_paths = set(entry["path"] for entry in entries)
extra = sorted(walked - pinned_paths)
unpinned = sorted(pinned_paths - walked)

script = CAND / "scripts" / "research_probability_v2.py"
data = script.read_bytes()
anchor = b"# A4 entry guard (2026-10-26)"
pos = data.find(anchor)
comment_start = data.rfind(NL, 0, pos) + 1
if_line_start = data.rfind(NL, 0, comment_start - 1) + 1
raise_line = b"""raise StageIncompleteError("ORDINARY_SHADOW_REPLAY_NOT_AVAILABLE")"""
rpos = data.find(raise_line)
block_end = data.find(NL, rpos) + 1
block = data[if_line_start:block_end]
reconstructed = data[:if_line_start] + data[block_end:]
if_start = b"""        if args.mode == "shadow-replay":""" + NL
seam = reconstructed[max(0, if_line_start - 200):if_line_start + 120]
delta = {
    "anchor_occurrences": data.count(anchor),
    "if_line_start_equals_previous_line_start_plus_zero": if_line_start < comment_start,
    "block_bytes": len(block),
    "block_lines": block.count(NL),
    "block_starts_with_if_shadow_replay": block.startswith(if_start),
    "block_ends_with_guard_raise": block.rstrip(NL).endswith(raise_line),
    "guard_raise_occurrences_in_file": data.count(raise_line),
    "block_sha256": hashlib.sha256(block).hexdigest(),
    "block_first_line": block.split(NL)[0].decode("utf-8"),
    "block_last_line": block.rstrip(NL).split(NL)[-1].decode("utf-8"),
    "reconstructed_bytes": len(reconstructed),
    "reconstructed_sha256": hashlib.sha256(reconstructed).hexdigest(),
    "reconstructed_matches_prechange_pin_33351e7a": hashlib.sha256(reconstructed).hexdigest() == "33351e7acd0004701ae07239ba0bd44710288b0f93cb4f28072f8cf6067c2b9f",
    "reconstructed_seam_context": seam.decode("utf-8"),
    "current_script_sha256": hashlib.sha256(data).hexdigest(),
    "current_script_matches_owner_pin_db0e61e3": hashlib.sha256(data).hexdigest() == "db0e61e37b6213c776a36625769e7391439628f0c0c451415471a68b0bc685ba",
    "crlf_bytes_in_script": data.count(bytes([13, 10])),
}
testfile = CAND / "tests" / "test_v2_m5_shadow_entry_guard.py"
tdata = testfile.read_bytes()
delta["new_test_file"] = {
    "path": str(testfile),
    "bytes": len(tdata),
    "sha256": hashlib.sha256(tdata).hexdigest(),
    "matches_owner_pin_ddae2262": hashlib.sha256(tdata).hexdigest() == "ddae2262b68756662cbdc90d37d5500bc63c9e9e2ea0b8a87cf58356b5e9ac28",
    "crlf_line_count": tdata.count(bytes([13, 10])),
}
interp = {
    "python_executable": sys.executable,
    "python_version": sys.version,
    "python_executable_sha256": sha256_file(sys.executable),
}
payload_out = {
    "phase": "A-before-required-rerun",
    "generated_utc": datetime.now(timezone.utc).isoformat(),
    "note": "attempt1 preserved separately; its block extractor omitted the if-line (683B/9L); this run anchors the block at the if-line start",
    "pins_file": str(PINS_FILE),
    "pins_file_sha256": sha256_file(PINS_FILE),
    "candidate_root": str(CAND),
    "pinned_files_total": len(entries),
    "verified_ok": sum(1 for r in results if r["status"] == "OK"),
    "mismatch_count": len(mismatch),
    "mismatches": mismatch,
    "walked_files_total": len(walked),
    "extra_files_not_pinned": extra,
    "pinned_but_not_walked": unpinned,
    "guard_block_delta": delta,
    "interpreter": interp,
    "per_file": results,
}
text = json.dumps(payload_out, indent=2)
json.loads(text)
tmp = OUT.with_suffix(".json.tmp")
tmp.write_bytes(text.encode("utf-8") + NL)
os.replace(tmp, OUT)
print("verified_ok", payload_out["verified_ok"], "of", payload_out["pinned_files_total"])
print("extra", len(extra), "unpinned", len(unpinned))
print("block", json.dumps({k: delta[k] for k in ("block_bytes", "block_lines", "block_starts_with_if_shadow_replay", "block_ends_with_guard_raise", "reconstructed_bytes", "reconstructed_matches_prechange_pin_33351e7a", "current_script_matches_owner_pin_db0e61e3", "crlf_bytes_in_script")}))
print("block_first_line", delta["block_first_line"])
print("block_last_line", delta["block_last_line"])
print("seam:", delta["reconstructed_seam_context"])
