"""NF23 developer tooling: manifests, source map, digest freeze, hashes.

Every artifact this script produces is generated programmatically (no
hand-copied hashes/digests), per the plan's evidence rules:

* ``write-manifests``  - emit the ten frozen case manifests (expected digests
  null until frozen by ``freeze-digests``);
* ``build-source-map`` - hash the measured observer production modules from
  the current worktree (WORKTREE_SNAPSHOT provenance; see README of this
  directory for the PENDING observer-owner release binding);
* ``freeze-digests``   - read a finished smoke run's result records and write
  each case's verify() digest into its manifest (the C20 smoke freeze);
* ``hash-deliverables``- SHA-256 manifest of the NF23 delivered files.

Run from the repository root:  python -B tools/benchmarks/fixtures/observer/devtools.py <command> [...]
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[4]
HERE = Path(__file__).resolve().parent

SPEC_PATH = "tools/benchmarks/fixtures/observer/payload-spec.json"
WORKLOAD_PATH = "tools/benchmarks/workloads/observer.py"
SOURCE_MAP_PATH = "tools/benchmarks/fixtures/observer/source-map.json"
FLAGSHIP_MANIFEST = "tools/benchmarks/cases/observer.json"
CASES_DIR = "tools/benchmarks/fixtures/observer/cases"

SEED = 20260924
POLICY_VERSION = "nf23-observer-bench-r1"

SMOKE_BUDGET = {"warmup_operations": 20, "operations": 200, "case_timeout_s": 30}
FORMAL_BUDGET = {
    "batches": 5,
    "warmup_operations": 20,
    "operations_per_batch": 20,
    "case_timeout_s": 600,
}

# logical module -> (relative path, why it is in the map)
MEASURED_SOURCES: Dict[str, List[str]] = {
    "ak_trading.infiny_bridge.protocol": [
        "src/ak_trading/infiny_bridge/protocol.py",
        "parse_json_body / normalize_payload measured by H23.1 cases",
    ],
    "ak_trading.infiny_bridge.service": [
        "src/ak_trading/infiny_bridge/service.py",
        "BridgeService.analyze/heartbeat/status measured by H23.2 cases",
    ],
    "ak_trading.infiny_bridge.connection": [
        "src/ak_trading/infiny_bridge/connection.py",
        "clock_sample/utc_time/TimingPolicy on the service measurement path",
    ],
    "ak_trading.infiny_bridge.raw_sink": [
        "src/ak_trading/infiny_bridge/raw_sink.py",
        "import-time dependency of service.py (sink object stays disabled)",
    ],
}

# case_id -> manifest-relative path (flagship case lives at the literal
# work-order path tools/benchmarks/cases/observer.json; the other nine live
# in this fixture subtree, mirroring the core-case layout)
CASE_FILES: Dict[str, str] = {}

# case_id -> (entrypoint, units, includes_io, measured, description)
CASES: Dict[str, Dict[str, Any]] = {}


def _register_cases() -> None:
    parse_measured = ["ak_trading.infiny_bridge.protocol"]
    norm_measured = [
        "ak_trading.infiny_bridge.protocol",
        "ak_trading.infiny_bridge.connection",
    ]
    service_measured = [
        "ak_trading.infiny_bridge.service",
        "ak_trading.infiny_bridge.protocol",
        "ak_trading.infiny_bridge.connection",
    ]
    io_note = (
        "Timed operation includes one temp-audit JSONL write+flush in the run "
        "temporary directory (audit log enabled); single evaluator worker, raw "
        "sink disabled; no HTTP/socket."
    )
    pure_note = "Pure in-process CPU measurement: no I/O, no sockets, no service objects."
    entries: List[Dict[str, Any]] = [
        {
            "case_id": "observer/parse_accept_typical_inprocess",
            "entrypoint": "build_parse_accept",
            "units": "payloads",
            "includes_io": False,
            "measured": parse_measured,
            "description": "SYNTHETIC in-process measurement of protocol.parse_json_body on one canonical legal request body (SHFE.rb2610, 1 symbol, 4 standard ticks, fixed byte count; see payload-spec fixture). " + pure_note + " Not an HTTP measurement.",
        },
        {
            "case_id": "observer/parse_reject_malformed_inprocess",
            "entrypoint": "build_parse_reject",
            "units": "payloads",
            "includes_io": False,
            "measured": parse_measured,
            "description": "SYNTHETIC in-process measurement of the parse FORMAT-REJECTION path: malformed JSON, duplicate keys and NaN constants cycled deterministically; a correctly produced rejection is the successful work unit and is accounted separately from the accept case (rejection samples never mix into accept latency). " + pure_note,
        },
        {
            "case_id": "observer/parse_max_legal_inprocess",
            "entrypoint": "build_parse_max_legal",
            "units": "payloads",
            "includes_io": False,
            "measured": parse_measured,
            "description": "SYNTHETIC in-process boundary measurement: parse_json_body on a legal body of exactly MAX_BODY_BYTES (262144) bytes. " + pure_note,
        },
        {
            "case_id": "observer/normalize_accept_typical_inprocess",
            "entrypoint": "build_normalize_accept",
            "units": "payloads",
            "includes_io": False,
            "measured": norm_measured,
            "description": "SYNTHETIC in-process measurement of protocol.normalize_payload on the canonical legal envelope (4 standard ticks, clean accept; blocked_reason must stay None). " + pure_note,
        },
        {
            "case_id": "observer/normalize_reject_violations_inprocess",
            "entrypoint": "build_normalize_reject",
            "units": "payloads",
            "includes_io": False,
            "measured": norm_measured,
            "description": "SYNTHETIC in-process measurement of the normalize REJECTION path: invalid_tick / ticks_limit / invalid_quote violations cycled deterministically; correctly produced rejections are the work unit and are accounted separately from the accept case. " + pure_note,
        },
        {
            "case_id": "observer/normalize_max_legal_inprocess",
            "entrypoint": "build_normalize_max_legal",
            "units": "payloads",
            "includes_io": False,
            "measured": norm_measured,
            "description": "SYNTHETIC in-process boundary measurement: normalize_payload on a legal envelope carrying the maximum legal tick count (MAX_TICKS=1000). " + pure_note,
        },
        {
            "case_id": "observer/analyze_accept_typical_inprocess_hold_double",
            "entrypoint": "build_analyze_accept",
            "units": "requests",
            "includes_io": True,
            "measured": service_measured,
            "description": "SYNTHETIC in-process measurement of the standard request path BridgeService.analyze on an isolated service (unique batch/sequence per operation, business clock advanced per protocol, deterministic_hold_double evaluator). Verify reconciles counters/sequence/receipts/audit lines and requires every response to be hold. " + io_note + " NOT an HTTP round-trip and NOT real strategy evaluation.",
        },
        {
            "case_id": "observer/analyze_replay_duplicate_inprocess_hold_double",
            "entrypoint": "build_analyze_replay",
            "units": "requests",
            "includes_io": True,
            "measured": service_measured,
            "description": "SYNTHETIC in-process measurement of the duplicate-batch CACHE-HIT (replay) path: one seed analyze caches the response, every measured operation replays the same batch id and must NOT re-invoke the evaluator (replay counter must equal the op count). Reported separately from the business-processing case so cache hits never masquerade as processing speed. " + io_note,
        },
        {
            "case_id": "observer/heartbeat_typical_inprocess_hold_double",
            "entrypoint": "build_heartbeat",
            "units": "requests",
            "includes_io": True,
            "measured": service_measured,
            "description": "SYNTHETIC in-process measurement of BridgeService.heartbeat (minimal legal heartbeat envelope; evaluator must never be invoked; response hold/heartbeat). " + io_note,
        },
        {
            "case_id": "observer/status_snapshot_inprocess_hold_double",
            "entrypoint": "build_status_snapshot",
            "units": "status_snapshots",
            "includes_io": False,
            "measured": service_measured,
            "description": "SYNTHETIC in-process measurement of BridgeService.status over a full 20-deep receipt ring seeded deterministically in setup (client status, freshness views, counters, evaluator status). The timed operation performs no audit writes; the audit file only records the setup seeds. No HTTP.",
        },
    ]
    for entry in entries:
        CASES[entry["case_id"]] = entry
    flagship = "observer/analyze_accept_typical_inprocess_hold_double"
    for case_id in CASES:
        if case_id == flagship:
            CASE_FILES[case_id] = FLAGSHIP_MANIFEST
        else:
            CASE_FILES[case_id] = "%s/%s.json" % (CASES_DIR, case_id.split("/", 1)[1])


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")


def build_manifest(case_id: str, expected_digest: Optional[str]) -> Dict[str, Any]:
    entry = CASES[case_id]
    return {
        "schema": "ak-benchmark-case-manifest/1.0.0",
        "case_id": case_id,
        "description": entry["description"],
        "origin": "SYNTHETIC",
        "domain": "observer",
        "backend": "python-inprocess",
        "eligible_for_baseline": True,
        "algorithm_version": "UNKNOWN",
        "policy_version": POLICY_VERSION,
        "workload": {"source": WORKLOAD_PATH, "entrypoint": entry["entrypoint"]},
        "measured_modules": entry["measured"],
        "fixtures": [
            {"path": SPEC_PATH, "role": "input"},
            {"path": WORKLOAD_PATH, "role": "workload"},
        ],
        "params": {"seed": SEED, "case": case_id, "payload_spec": SPEC_PATH},
        "measurement": {
            "sample_mode": "per_operation",
            "units": entry["units"],
            "units_per_operation": 1,
            "min_operations": 100,
            "batch_size": 100,
            "reset_method": "none",
            "includes_io": entry["includes_io"],
        },
        "resources": {"network": False, "production_db": False, "temp_sqlite": False},
        "environment": {"depends_on": ["ak_trading"]},
        "correctness": {"kind": "digest", "expected_digest": expected_digest, "normalizations": []},
        "budget": {"smoke": SMOKE_BUDGET, "formal": FORMAL_BUDGET},
    }


def cmd_write_manifests(frozen: Optional[Dict[str, str]] = None) -> int:
    _register_cases()
    for case_id, rel in CASE_FILES.items():
        digest = (frozen or {}).get(case_id)
        _write_json(REPO_ROOT / rel, build_manifest(case_id, digest))
        print("wrote %s (expected_digest=%s)" % (rel, digest or "null"))
    return 0


def cmd_build_source_map() -> int:
    modules = []
    for logical, (rel, note) in MEASURED_SOURCES.items():
        path = REPO_ROOT / rel
        data = path.read_bytes()
        modules.append(
            {
                "logical_module": logical,
                "source_relative_path": rel,
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
                "owner": "NF23",
                "release_id": "WORKTREE_SNAPSHOT",
                "provenance_kind": "WORKTREE_SNAPSHOT",
                "note": note,
            }
        )
    source_map = {
        "schema": "ak-benchmark-source-map/1.0.0",
        "description": (
            "NF23 measured-module source map. Provenance is WORKTREE_SNAPSHOT: "
            "hashes were taken from the current repository worktree by "
            "devtools.py build-source-map (programmatic, no hand copying). The "
            "observer original owner's candidate input closure / release "
            "binding is PENDING handover; baseline-grade formal runs must "
            "rebind to that owner-approved identity before promotion."
        ),
        "modules": modules,
    }
    _write_json(REPO_ROOT / SOURCE_MAP_PATH, source_map)
    for module in modules:
        print("%s %s %d bytes" % (module["logical_module"], module["sha256"][:16], module["bytes"]))
    return 0


def cmd_freeze_digests(run_dir: Path) -> int:
    results = run_dir / "results"
    if not results.is_dir():
        print("error: no results dir under %s" % run_dir, file=sys.stderr)
        return 2
    frozen: Dict[str, str] = {}
    for record_path in sorted(results.glob("*.json")):
        record = json.loads(record_path.read_text(encoding="utf-8"))
        digest = (record.get("correctness") or {}).get("digest")
        if record.get("status") != "OK" or not digest:
            print(
                "error: case %s not OK (status=%s); refusing to freeze"
                % (record.get("case_id"), record.get("status")),
                file=sys.stderr,
            )
            return 2
        frozen[str(record["case_id"])] = str(digest)
    _register_cases()
    missing = sorted(set(CASES) - set(frozen))
    if missing:
        print("error: run is missing records for: %s" % missing, file=sys.stderr)
        return 2
    cmd_write_manifests(frozen)
    return 0


def cmd_hash_deliverables(out: Path, extra: Optional[List[str]] = None) -> int:
    _register_cases()
    rels = sorted(
        [SPEC_PATH, WORKLOAD_PATH, SOURCE_MAP_PATH, str(Path(__file__).relative_to(REPO_ROOT)).replace("\\", "/")]
        + list(CASE_FILES.values())
        + ["tests/benchmarks/test_observer_workload.py", "docs/benchmarks/observer.md"]
        + (extra or [])
    )
    files = []
    for rel in rels:
        path = REPO_ROOT / rel
        if not path.is_file():
            print("  skip (absent): %s" % rel)
            continue
        data = path.read_bytes()
        files.append(
            {
                "path": rel,
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
            }
        )
    payload = {
        "schema": "ak-nf23-deliverable-hashes/1.0.0",
        "description": "Programmatic SHA-256 of the NF23 delivered files (generated by devtools.py hash-deliverables; no hand-copied hashes).",
        "files": files,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    _write_json(out, payload)
    print("wrote %s (%d files)" % (out, len(files)))
    return 0


def main(argv: List[str]) -> int:
    if len(argv) < 1:
        print(__doc__)
        return 2
    command = argv[0]
    if command == "write-manifests":
        return cmd_write_manifests()
    if command == "build-source-map":
        return cmd_build_source_map()
    if command == "freeze-digests":
        if len(argv) != 2:
            print("usage: freeze-digests <run_dir>", file=sys.stderr)
            return 2
        return cmd_freeze_digests(Path(argv[1]).resolve())
    if command == "hash-deliverables":
        if len(argv) < 2:
            print("usage: hash-deliverables <out.json> [extra-rel ...]", file=sys.stderr)
            return 2
        return cmd_hash_deliverables(Path(argv[1]).resolve(), argv[2:] or None)
    print("unknown command %r" % command, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
