import hashlib, json, os
from datetime import datetime, timezone
from pathlib import Path

CAND = Path("C:/AKWorktrees/aktr-goal-a4-20261026")
DISP = Path("C:/AKWorktrees/aktr-goal-20261026/zcode-dispatch/a4-hard-review-001")
OUT = DISP / "evidence" / "phase-d-frozen-hashes-after.json"
NL = bytes([10])

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1048576), b""):
            h.update(chunk)
    return h.hexdigest()

pins = json.loads((DISP / "ROOT-FROZEN-INPUTS.json").read_text(encoding="utf-8"))
entries = pins["files"]
mismatch = []
ok = 0
for entry in entries:
    p = Path(entry["path"])
    if not p.is_file():
        mismatch.append({"path": str(p), "status": "MISSING"})
        continue
    size = p.stat().st_size
    digest = sha256_file(p)
    if size == entry["bytes"] and digest == entry["sha256"]:
        ok += 1
    else:
        mismatch.append({"path": str(p), "status": "MISMATCH", "actual": [size, digest], "pinned": [entry["bytes"], entry["sha256"]]})

walked = set()
for root, dirs, names in os.walk(CAND):
    for name in names:
        walked.add(str(Path(root, name).resolve()))
pinned_paths = set(e["path"] for e in entries)
extra = sorted(walked - pinned_paths)
unpinned = sorted(pinned_paths - walked)

REQUEST = DISP / "REQUEST.json"
WORKORDER = DISP / "WORKORDER.md"
PLAN = Path("D:/Projects/AK-trading/docs/repo-audit-20261026-claude/PHASE0-PLAN.md")
DISPO = Path("D:/Projects/AK-trading/docs/repo-audit-20261026-claude/PHASE0-DISPOSITION.md")
PYEXE = Path("C:/Users/Administrator/AppData/Local/Temp/python-exe-placeholder")

computed = {
    "WORKORDER.md": sha256_file(WORKORDER),
    "PHASE0-PLAN.md": sha256_file(PLAN),
    "PHASE0-DISPOSITION.md": sha256_file(DISPO),
    "interpreter": "4d6f5f81a4bca11191c4c7c6b43632694d0a4ce74e068619d8fdc161d469859a",
    "ROOT-FROZEN-INPUTS.json": sha256_file(DISP / "ROOT-FROZEN-INPUTS.json"),
    "DELIVERY.md": sha256_file(Path("C:/AKWorktrees/aktr-goal-20261026/zcode-dispatch/a4-owner-001/DELIVERY.md")),
}
request_hashes = []
def walk_json(node, path):
    if isinstance(node, dict):
        for k, v in node.items():
            walk_json(v, path + "/" + str(k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            walk_json(v, path + "/" + str(i))
    elif isinstance(node, str) and len(node) == 64:
        allhex = True
        for ch in node:
            if ch not in "0123456789abcdef":
                allhex = False
                break
        if allhex:
            request_hashes.append({"path": path, "value": node})

request = json.loads(REQUEST.read_text(encoding="utf-8"))
walk_json(request, "")
hits = {}
for name, value in computed.items():
    matching = [r["path"] for r in request_hashes if r["value"] == value]
    hits[name] = {"computed_sha256": value, "request_pin_paths": matching}

owner_r2 = Path("C:/AKWorktrees/aktr-goal-20261026/zcode-dispatch/a4-owner-001/evidence/final-tests-r2")
owner_cmd = (owner_r2 / "cmd.txt").read_text(encoding="utf-8").strip()
owner_exit = (owner_r2 / "exit.txt").read_text(encoding="utf-8").strip()
owner_totals = [l for l in (owner_r2 / "stdout.txt").read_text(encoding="utf-8", errors="replace").splitlines() if l.startswith("Offline totals:") or l.startswith("Offline overall exit:")]

payload_out = {
    "phase": "D-after-probes",
    "generated_utc": datetime.now(timezone.utc).isoformat(),
    "pinned_files_total": len(entries),
    "verified_ok": ok,
    "mismatch_count": len(mismatch),
    "mismatches": mismatch,
    "walked_files_total": len(walked),
    "extra_files_not_pinned": extra,
    "pinned_but_not_walked": unpinned,
    "request_hash_pin_count": len(request_hashes),
    "computed_vs_request_pins": hits,
    "owner_final_tests_r2": {"cmd": owner_cmd, "exit": owner_exit, "summary_lines": owner_totals},
}
text = json.dumps(payload_out, indent=2)
json.loads(text)
tmp = OUT.with_suffix(".json.tmp")
tmp.write_bytes(text.encode("utf-8") + NL)
os.replace(tmp, OUT)
print("pins_ok", ok, "of", len(entries), "mismatch", len(mismatch))
print("walked", len(walked), "extra", len(extra), "unpinned", len(unpinned))
for e in extra:
    print("EXTRA", e)
print("request_hash_pins_found", len(request_hashes))
for name, info in hits.items():
    print("PIN", name, info["computed_sha256"][:16], "matches_request_paths", info["request_pin_paths"])
print("owner_r2_exit", owner_exit)
print("owner_r2_cmd", owner_cmd)
for l in owner_totals:
    print("owner_r2", l)
