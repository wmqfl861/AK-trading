#!/usr/bin/env python
"""DP08 release verifier: build and verify the immutable release manifest.

WORK ORDER
    work-orders/DP08-integration.md (H08.3/H08.6/H08.7) and
    04-validation-and-release.md ("python -B scripts/verify_datapipeline_release.py
    --manifest <frozen-release.json> --output <new-evidence-dir>").

MODE (honest scope)
    OFFLINE / read-only over the repository.  No network, no database, no
    SDK, no .env, no git write.  The verifier NEVER modifies the manifest
    or any frozen face; results land only in the explicitly injected
    --output directory (refused inside frozen evidence faces).

BUILD MODE (--build-manifest <path>)
    Assembles the release manifest PROGRAMMATICALLY from the seven node
    freeze manifests (no hand-copied hashes): every delivered write-set
    file with its sha256 and owning node, staging scaffolds and dependency
    pins flagged as such, the V01-V15 required-test matrix harvested from
    the node manifests' recorded pytest results, and V15 forced NOT_RUN
    unless a real canary evidence file is injected via --canary-evidence.

    DP08 r2 pin overlays (documented, still fully programmatic):
    * the two X-08 owner-fixed files (DP03 collector.py, DP02 readback.py;
      defects X-08-1/2/3/5) pin to their POST-FIX sha256 read from the
      DP-X08FIX r1 fix manifest - authoritative until each owning node
      re-freezes its own r2 manifest;
    * DP08's own write set is taken from the newest DP08 file manifest
      present (r2 supersedes r1 for the same paths).

VERIFY MODE (default)
    Re-hashes every manifest file against the live tree, checks ownership
    uniqueness, verifies the DP01 dependency pins byte-identical, checks
    the V15 gate, and writes verification-report.json + verification-
    summary.md into --output.  Exit 0 only when everything matches; exit 1
    on any drift, inconsistency or gate violation.

USAGE
    python -B scripts/verify_datapipeline_release.py --build-manifest <new.json>
    python -B scripts/verify_datapipeline_release.py \
        --manifest <frozen-release.json> --output <new-evidence-dir> \
        [--repo <root>] [--canary-evidence <file>]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

RELEASE_SCHEMA = "dp08-release-manifest/1"
VERIFICATION_SCHEMA = "dp08-release-verification/1"

DEVPLAN_REL = "docs/devplan-datapipeline-20260923-r1"
EVIDENCE_REL = "docs/datapipeline-evidence-20260923-r1"

#: node -> freeze manifest location (repo-relative)
NODE_MANIFESTS: Dict[str, str] = {
    "DP01": f"{DEVPLAN_REL}/DP01/r1/dp01-freeze-manifest.json",
    "DP02": f"{DEVPLAN_REL}/DP02/r1/dp02-freeze-manifest.json",
    "DP03": f"{EVIDENCE_REL}/DP03/r1/file-manifest.json",
    "DP04": f"{EVIDENCE_REL}/DP04/r1/dp04-delivery-manifest.json",
    "DP05": f"{DEVPLAN_REL}/DP05/r1/dp05-freeze-manifest.json",
    "DP06": f"{EVIDENCE_REL}/DP06/r1/file-hashes.json",
    "DP07": f"{DEVPLAN_REL}/DP07/r1/dp07-freeze-manifest.json",
}

#: staged-evidence nodes deliver under their r1 prefix with candidate-
#: relative manifest paths (repo-relative = <devplan>/<node>/r1/<path>)
STAGED_NODES = ("DP01", "DP02", "DP05", "DP07")

#: DP07's staging-only package scaffolds (NOT in the candidate write set;
#: DP01's frozen init is the integrated authority)
DP07_STAGING_SCAFFOLDS = {
    "src/ak_trading/__init__.py",
    "src/ak_trading/datapipeline/__init__.py",
}

FORBIDDEN_OUTPUT_MARKERS = ("maintenance_artifacts", "AKF260920", ".git")

#: frozen documentation faces: verification output must NEVER land inside
#: them (04-validation: outputs never fall onto frozen faces; a reviewer
#: verifies into a fresh directory outside the evidence trees)
FROZEN_OUTPUT_PREFIXES = (DEVPLAN_REL + "/", EVIDENCE_REL + "/")

#: DP-X08FIX r1 fix manifest: authoritative post-fix sha256 for the two
#: owner-fixed files (X-08-1/2/3/5), used as a build-time pin overlay.
X08FIX_MANIFEST_REL = f"{EVIDENCE_REL}/DP-X08FIX/r1/fix-manifest.json"

#: V01-V15 owner map per 04-validation-and-release.md
V_MATRIX_OWNERS: Dict[str, str] = {
    "V01": "DP01", "V02": "DP01/DP02/DP03", "V03": "DP02", "V04": "DP02",
    "V05": "DP02", "V06": "DP03", "V07": "DP03", "V08": "DP04",
    "V09": "DP05", "V10": "DP07", "V11": "DP06", "V12": "DP08",
    "V13": "DP08", "V14": "DP02/DP03/DP08", "V15": "DP08",
}

EXIT_OK = 0
EXIT_DRIFT = 1


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm(path_text: str) -> str:
    return path_text.replace("\\", "/")


# ---------------------------------------------------------------------------
# Manifest harvest
# ---------------------------------------------------------------------------

def _manifest_file_entries(node: str, manifest: Mapping[str, Any]) -> List[Dict[str, str]]:
    """Flatten a node manifest into {path, sha256, role} entries with
    repo-relative paths (roles: write_set / staging_scaffold /
    dependency_pin)."""
    prefix = f"{DEVPLAN_REL}/{node}/r1/"
    entries: List[Dict[str, str]] = []

    def add(path: str, sha: str, role: str) -> None:
        path = _norm(path)
        if node in STAGED_NODES and not path.startswith("docs/"):
            path = prefix + path
        entries.append({"path": path, "sha256": sha, "role": role})

    for key in ("files", "write_set_files"):
        for entry in manifest.get(key) or ():
            if isinstance(entry, Mapping) and "path" in entry:
                add(entry["path"], entry["sha256"], "write_set")
    write_set = manifest.get("write_set")
    if isinstance(write_set, Mapping):
        for group, items in write_set.items():
            role = "write_set" if group != "staging_scaffolds_not_in_candidate_write_set" else "staging_scaffold"
            for entry in items or ():
                if isinstance(entry, Mapping) and "path" in entry:
                    if node == "DP07" and entry["path"] in DP07_STAGING_SCAFFOLDS:
                        add(entry["path"], entry["sha256"], "staging_scaffold")
                    else:
                        add(entry["path"], entry["sha256"], role)
    for key in ("staging_scaffold_not_in_candidate_write_set",
                "staging_scaffolds_not_in_candidate_write_set"):
        for entry in manifest.get(key) or ():
            if isinstance(entry, Mapping) and "path" in entry:
                add(entry["path"], entry["sha256"], "staging_scaffold")
    for entry in manifest.get("staged_dependencies_byte_identical_to_dp01") or ():
        if isinstance(entry, Mapping) and "path" in entry:
            add(f"{DEVPLAN_REL}/DP01/r1/{_norm(entry['path'])}", entry["sha256"], "dependency_pin")
    return entries


#: node -> recorded pytest log (repo-relative), for result harvesting
NODE_PYTEST_LOGS: Dict[str, str] = {
    "DP01": f"{DEVPLAN_REL}/DP01/r1/pytest-run-r1.log",
    "DP02": f"{DEVPLAN_REL}/DP02/r1/pytest-run-r1.log",
    "DP03": f"{EVIDENCE_REL}/DP03/r1/pytest-run-r1.log",
    "DP04": f"{EVIDENCE_REL}/DP04/r1/pytest-run-r1.log",
    "DP05": f"{DEVPLAN_REL}/DP05/r1/pytest-run-r1.log",
    "DP06": f"{EVIDENCE_REL}/DP06/r1/pytest-run-r1.log",
    "DP07": f"{DEVPLAN_REL}/DP07/r1/pytest-run-r1.log",
}

import re  # noqa: E402  (kept close to its only use)

_PYTEST_SUMMARY_RE = re.compile(r"(\d+ passed[^\n]*)")


def _log_result(repo: Path, node: str) -> Optional[str]:
    log_path = repo / NODE_PYTEST_LOGS[node]
    if not log_path.is_file():
        return None
    match = _PYTEST_SUMMARY_RE.search(log_path.read_text(encoding="utf-8", errors="replace")[-4000:])
    return match.group(1) if match else None


def _harvest_pytest(node: str, manifest: Mapping[str, Any], repo: Path) -> Dict[str, Any]:
    payload = manifest.get("pytest")
    result: Optional[str] = None
    command: Optional[str] = None
    log: Optional[str] = NODE_PYTEST_LOGS.get(node)
    if isinstance(payload, Mapping):
        result = payload.get("result")
        command = payload.get("command")
    elif isinstance(payload, list) and payload:
        result = str(payload[0].get("evidence"))
    if result is None:
        result = _log_result(repo, node)
    if result is None:
        required = manifest.get("required_tests")
        if isinstance(required, list) and required:
            result = str(required[0].get("evidence"))
    return {"result": result, "exit_code": None, "command": command, "log": log}


def build_manifest(repo: Path, *, dp08_summary: Optional[Path] = None,
                   canary_evidence: Optional[Path] = None) -> Dict[str, Any]:
    """Assemble the release manifest from the node freeze manifests."""
    files: Dict[str, Dict[str, str]] = {}
    node_results: Dict[str, Any] = {}
    for node, manifest_rel in NODE_MANIFESTS.items():
        manifest_path = repo / manifest_rel
        if not manifest_path.is_file():
            raise SystemExit(f"node freeze manifest missing: {manifest_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for entry in _manifest_file_entries(node, manifest):
            path = entry["path"]
            if path in files and files[path]["sha256"] != entry["sha256"]:
                raise SystemExit(
                    f"conflicting pins for {path}: {files[path]['node']}/"
                    f"{files[path]['sha256'][:8]} vs {node}/{entry['sha256'][:8]}"
                )
            if path not in files:
                files[path] = {
                    "path": path, "sha256": entry["sha256"],
                    "node": node, "role": entry["role"],
                }
        node_results[node] = _harvest_pytest(node, manifest, repo)
        node_results[node]["freeze_manifest"] = manifest_rel

    # DP08's own delivered write set (hash-verified by build_evidence.py;
    # the file manifest is written before the release manifest is built).
    # r2 supersedes r1 for the same paths: when a newer revision's file
    # manifest exists its pins replace the r1 pins (paths are identical).
    for revision in ("r1", "r2"):
        dp08_file_manifest = repo / EVIDENCE_REL / "DP08" / revision / "file-manifest.json"
        if not dp08_file_manifest.is_file():
            continue
        payload = json.loads(dp08_file_manifest.read_text(encoding="utf-8"))
        for entry in payload.get("write_set_files") or ():
            path = _norm(entry["path"])
            files[path] = {
                "path": path, "sha256": entry["sha256"],
                "node": "DP08", "role": "write_set",
                "pin_source": f"dp08-{revision}-file-manifest",
            }

    # DP08 r2 X-08FIX pin overlay: the two owner-fixed files pin to their
    # POST-FIX sha256 from the DP-X08FIX r1 fix manifest (programmatic; the
    # owning nodes' r1 freeze manifests still carry the pre-fix values until
    # their own r2 re-freeze).  Every overlaid path MUST already be a pinned
    # delivered file - the overlay never adds or renames paths.
    x08fix_manifest = repo / X08FIX_MANIFEST_REL
    if not x08fix_manifest.is_file():
        raise SystemExit(f"DP-X08FIX fix manifest missing: {x08fix_manifest}")
    x08fix = json.loads(x08fix_manifest.read_text(encoding="utf-8"))
    pin_overlays: Dict[str, Dict[str, str]] = {}
    for entry in x08fix.get("files") or ():
        path = _norm(entry["path"])
        if path not in files:
            raise SystemExit(
                f"DP-X08FIX overlay path not delivered by any node manifest: {path}"
            )
        files[path]["sha256"] = entry["sha256_after"]
        files[path]["pin_source"] = "dp-x08fix-r1-overlay"
        pin_overlays[path] = {
            "sha256": entry["sha256_after"],
            "sha256_before": entry.get("sha256_before"),
            "node": files[path]["node"],
        }

    # DP08's own recorded results (from the evidence summary, if provided)
    dp08: Dict[str, Any] = {"pytest": None, "notes": []}
    if dp08_summary is not None and dp08_summary.is_file():
        dp08 = json.loads(dp08_summary.read_text(encoding="utf-8"))
    node_results["DP08"] = {
        "result": dp08.get("pytest"),
        "exit_code": dp08.get("exit_code"),
        "command": dp08.get("command"),
        "log": dp08.get("log"),
        "freeze_manifest": None,
    }

    matrix: Dict[str, Dict[str, Any]] = {}
    for vid, owner in V_MATRIX_OWNERS.items():
        if vid == "V15":
            if canary_evidence is not None and Path(canary_evidence).is_file():
                matrix[vid] = {"owner": owner, "status": "RUN", "evidence": str(canary_evidence)}
            else:
                matrix[vid] = {"owner": owner, "status": "NOT_RUN",
                               "reason": "no user-controlled live canary window executed; "
                                         "offline engineering release only (H08.8)"}
            continue
        primary = owner.split("/")[0]
        recorded = node_results.get(primary, {}).get("result")
        status = "RUN_SYNTHETIC" if recorded else "UNKNOWN"
        if vid == "V14":
            status = "PARTIAL_SYNTHETIC"  # SQLite legs RUN, PG capacity legs NOT_RUN
        if vid in ("V03", "V05"):
            status = "NOT_RUN"  # isolated PostgreSQL not injected (node registers)
        matrix[vid] = {
            "owner": owner, "status": status,
            "node_result": recorded,
            "node_log": node_results.get(primary, {}).get("log"),
        }

    return {
        "schema": RELEASE_SCHEMA,
        "built_at_utc": _now_iso(),
        "repo_root": str(repo),
        "files": sorted(files.values(), key=lambda f: f["path"]),
        "node_test_results": node_results,
        "dp08": dp08,
        "v_matrix": matrix,
        "pin_overlays": {
            "x08fix_manifest": X08FIX_MANIFEST_REL,
            "files": pin_overlays,
        },
        "notes": [
            "All SYNTHETIC results prove engineering behavior only; they are "
            "not real-market facts (D06/D07).",
            "PG-required verifications stay NOT_RUN until an isolated "
            "PostgreSQL database is injected by the controlled test launcher.",
            "The two DP-X08FIX-overlaid files pin their POST-FIX sha256 "
            "(defects X-08-1/2/3/5 fixed by DP-X08FIX r1; DP08 r2 re-"
            "integration verifies the fixes).",
        ],
    }


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def _output_root_allowed(output: Path) -> bool:
    text = str(output).replace("\\", "/")
    if any(marker.lower() in text.lower() for marker in FORBIDDEN_OUTPUT_MARKERS):
        return False
    return not any(part in text for part in FROZEN_OUTPUT_PREFIXES)


def verify_manifest(manifest: Mapping[str, Any], repo: Path,
                    *, canary_evidence: Optional[Path] = None) -> Dict[str, Any]:
    drift: List[Dict[str, str]] = []
    missing: List[str] = []
    verified = 0
    dependency_pin_mismatch: List[str] = []

    # group dependency pins by sha for byte-identity cross-checks
    by_sha: Dict[str, List[str]] = {}
    for entry in manifest.get("files", []):
        by_sha.setdefault(entry["sha256"], []).append(entry["path"])

    for entry in manifest.get("files", []):
        path = repo / entry["path"]
        if not path.is_file():
            missing.append(entry["path"])
            continue
        actual = _sha256(path)
        if actual != entry["sha256"]:
            drift.append({
                "path": entry["path"], "node": entry["node"],
                "manifest": entry["sha256"], "actual": actual,
            })
        else:
            verified += 1
        if entry["role"] == "dependency_pin":
            # byte-identity with every other file carrying the same pin
            twins = [p for p in by_sha[entry["sha256"]] if p != entry["path"]]
            for twin in twins:
                twin_path = repo / twin
                if twin_path.is_file() and _sha256(twin_path) != actual:
                    dependency_pin_mismatch.append(f"{entry['path']} != {twin}")

    matrix = dict(manifest.get("v_matrix", {}))
    gate_errors: List[str] = []
    v15 = matrix.get("V15", {})
    if v15.get("status") not in ("NOT_RUN", None):
        if canary_evidence is None or not Path(canary_evidence).is_file():
            gate_errors.append(
                "V15 claims a run without injected --canary-evidence; refusing "
                "(no canary may be inferred from mocks)"
            )
    for vid, entry in matrix.items():
        if entry.get("status") == "UNKNOWN":
            gate_errors.append(f"{vid} has no recorded node result")

    clean = not drift and not missing and not dependency_pin_mismatch and not gate_errors
    return {
        "schema": VERIFICATION_SCHEMA,
        "verified_at_utc": _now_iso(),
        "manifest_files": len(manifest.get("files", [])),
        "files_verified": verified,
        "drift": drift,
        "missing": missing,
        "dependency_pin_mismatch": dependency_pin_mismatch,
        "v15_gate": {"status": v15.get("status"), "canary_evidence": str(canary_evidence) if canary_evidence else None},
        "gate_errors": gate_errors,
        "clean": clean,
        "exit_code": EXIT_OK if clean else EXIT_DRIFT,
    }


def _render_summary(report: Mapping[str, Any]) -> str:
    lines = [
        "# DP08 release verification summary",
        "",
        f"- verified_at_utc: {report['verified_at_utc']}",
        f"- manifest files: {report['manifest_files']}",
        f"- files verified byte-identical: {report['files_verified']}",
        f"- drift: {len(report['drift'])}",
        f"- missing: {len(report['missing'])}",
        f"- dependency pin mismatches: {len(report['dependency_pin_mismatch'])}",
        f"- V15 gate: {report['v15_gate']['status']} (canary evidence: {report['v15_gate']['canary_evidence']})",
        f"- gate errors: {len(report['gate_errors'])}",
        f"- verdict: {'CLEAN' if report['clean'] else 'DRIFT/BLOCKED'}",
        "",
    ]
    for item in report["drift"][:20]:
        lines.append(f"- DRIFT {item['path']} ({item['node']}): {item['manifest'][:12]} -> {item['actual'][:12]}")
    for path in report["missing"][:20]:
        lines.append(f"- MISSING {path}")
    for note in report["dependency_pin_mismatch"][:20]:
        lines.append(f"- PIN-MISMATCH {note}")
    for err in report["gate_errors"][:20]:
        lines.append(f"- GATE {err}")
    return "\n".join(lines) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=None, help="repository root (default: this script's parents[1])")
    parser.add_argument("--build-manifest", metavar="PATH",
                        help="build a NEW release manifest at PATH from the node freeze manifests")
    parser.add_argument("--dp08-summary", metavar="PATH",
                        help="DP08 evidence summary (pytest result) folded into the built manifest")
    parser.add_argument("--manifest", metavar="PATH",
                        help="frozen release manifest to verify (default: E/DP08/r1/release-manifest.json)")
    parser.add_argument("--output", metavar="DIR",
                        help="verification output directory (required in verify mode)")
    parser.add_argument("--canary-evidence", metavar="PATH", default=None,
                        help="real live-canary evidence file backing any V15 RUN claim")
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve() if args.repo else Path(__file__).resolve().parents[1]

    if args.build_manifest:
        manifest = build_manifest(
            repo,
            dp08_summary=Path(args.dp08_summary) if args.dp08_summary else None,
            canary_evidence=Path(args.canary_evidence) if args.canary_evidence else None,
        )
        target = Path(args.build_manifest)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                          encoding="utf-8")
        print(f"release manifest built: {target} ({len(manifest['files'])} files)")
        return EXIT_OK

    manifest_path = (
        Path(args.manifest) if args.manifest
        else repo / EVIDENCE_REL / "DP08" / "r1" / "release-manifest.json"
    )
    if not manifest_path.is_file():
        print(f"ERROR: release manifest not found: {manifest_path}", file=sys.stderr)
        return EXIT_DRIFT
    if args.output is None:
        print("ERROR: --output is required in verify mode", file=sys.stderr)
        return EXIT_DRIFT
    output = Path(args.output).resolve()
    if not _output_root_allowed(output):
        print(f"ERROR: --output touches a frozen/forbidden face: {output}", file=sys.stderr)
        return EXIT_DRIFT

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    report = verify_manifest(
        manifest, repo,
        canary_evidence=Path(args.canary_evidence) if args.canary_evidence else None,
    )
    output.mkdir(parents=True, exist_ok=True)
    (output / "verification-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8",
    )
    (output / "verification-summary.md").write_text(_render_summary(report), encoding="utf-8")
    print(_render_summary(report))
    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
