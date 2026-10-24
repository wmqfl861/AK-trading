"""NF20 benchmark CLI - the frozen repeatable entry point (H20.9).

Invocation (from the repository root)::

    python -B -m tools.benchmarks.cli --help
    python -B -m tools.benchmarks.cli run --source-map <map.json> \
        --output-root <dir> --profile smoke|formal [--suite suite.json] \
        [--case <case_id> ...] [--case-manifest <path> ...] [--run-id <id>]
    python -B -m tools.benchmarks.cli compare --candidate <run_dir> \
        --baseline-store <dir> --policy <policy.json> --out <dir> [--runs-root <dir>]
    python -B -m tools.benchmarks.cli promote --run <run_dir> --case <id> \
        --output-root <dir> --promoter <name>
    python -B -m tools.benchmarks.cli drift --run <run_dir> --out <file> [--case <id>]
    python -B -m tools.benchmarks.cli validate-case --case-manifest <path>

H20.2: this module imports ONLY the standard library at top level;
``--help`` and argument-validation errors return before any harness or
project module is imported.  Exit codes: 0 success; 2 usage/validation
error (nothing imported under test, nothing executed); 3 command completed
but reported failures (non-OK cases, REGRESSION/INVALID verdicts, DRIFT).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List, Optional


def _repo_root() -> Path:
    """Repository root inferred from this file's location
    (``<root>/tools/benchmarks/cli.py``); overridable with --root."""
    return Path(__file__).resolve().parents[2]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -B -m tools.benchmarks.cli",
        description="NF20 reproducible offline benchmark harness (C20 contract %s "
        "family). All inputs are explicit; nothing is auto-discovered." % "C20-1.0.1",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser(
        "run", help="execute a suite/case selection under a profile budget"
    )
    p_run.add_argument("--source-map", required=True, type=Path,
                       help="candidate source map JSON (containment+hash verified before any import)")
    p_run.add_argument("--output-root", required=True, type=Path,
                       help="run output root; run data lands in <root>/runs/<run_id>/")
    p_run.add_argument("--profile", required=True, choices=("smoke", "formal"),
                       help="smoke: <=30s/case, <=180s/suite, runnability+correctness only; "
                       "formal: subprocess batches, baseline-grade")
    p_run.add_argument("--suite", type=Path, default=None,
                       help="suite registry JSON (case_id -> manifest, NF20-owned)")
    p_run.add_argument("--case", action="append", default=None,
                       help="case id from the suite (repeatable)")
    p_run.add_argument("--case-manifest", action="append", default=None, type=Path,
                       help="explicit case manifest path (repeatable)")
    p_run.add_argument("--run-id", default=None,
                       help="explicit run id (default: generated); refuses existing dirs")
    p_run.add_argument("--root", type=Path, default=None,
                       help="repository root for relative path resolution (default: inferred)")
    p_run.add_argument("--allow-budget-override", action="store_true",
                       help="record a pre-run resource adjustment raising the formal "
                       "per-case cap above the frozen 600s default (H20.4)")

    p_batch = sub.add_parser(
        "batch", help="internal: execute ONE formal batch in this process (used by the runner)"
    )
    p_batch.add_argument("--case-manifest", required=True, type=Path)
    p_batch.add_argument("--source-map", required=True, type=Path)
    p_batch.add_argument("--partial-out", required=True, type=Path)
    p_batch.add_argument("--batch-index", required=True, type=int)
    p_batch.add_argument("--warmup-operations", required=True, type=int)
    p_batch.add_argument("--operations", required=True, type=int)
    p_batch.add_argument("--root", type=Path, default=None)

    p_cmp = sub.add_parser("compare", help="compare a candidate run against pinned baselines")
    p_cmp.add_argument("--candidate", required=True, type=Path, help="candidate run directory")
    p_cmp.add_argument("--baseline-store", required=True, type=Path)
    p_cmp.add_argument("--policy", required=True, type=Path,
                       help="pre-registered baseline policy JSON (H20.8)")
    p_cmp.add_argument("--out", required=True, type=Path,
                       help="report output directory (must not exist)")
    p_cmp.add_argument("--runs-root", type=Path, default=None,
                       help="runs root used to validate policy baseline citations")

    p_prom = sub.add_parser("promote", help="manually promote an OK formal case run to a new baseline version")
    p_prom.add_argument("--run", required=True, type=Path, help="run directory")
    p_prom.add_argument("--case", required=True, action="append",
                        help="case id to promote (repeatable)")
    p_prom.add_argument("--output-root", required=True, type=Path,
                        help="root containing baselines/ (store = <output-root>/baselines)")
    p_prom.add_argument("--promoter", required=True,
                        help="responsible person for the promotion record (H20.8)")

    p_drift = sub.add_parser("drift", help="re-hash a run's identity inputs against the worktree")
    p_drift.add_argument("--run", required=True, type=Path, help="run directory")
    p_drift.add_argument("--case", action="append", default=None,
                         help="restrict to case ids (default: all cases of the run)")
    p_drift.add_argument("--out", required=True, type=Path, help="output JSON file (must not exist)")
    p_drift.add_argument("--root", type=Path, default=None)

    p_val = sub.add_parser("validate-case", help="statically validate a case manifest (no execution)")
    p_val.add_argument("--case-manifest", required=True, type=Path)
    p_val.add_argument("--root", type=Path, default=None)
    return parser


# ---------------------------------------------------------------------------
# handlers (harness modules are imported lazily, after argparse validation)
# ---------------------------------------------------------------------------

def _cmd_run(args: argparse.Namespace) -> int:
    from tools.benchmarks import contracts as C
    from tools.benchmarks import runner

    root = (args.root or _repo_root()).resolve()
    if args.suite is None and not args.case_manifest:
        raise C.ContractError("run requires --suite and/or --case-manifest (H20.2 explicit inputs)")
    rc, run_id = runner.run_suite(
        root=root,
        output_root=args.output_root.resolve(),
        profile=args.profile,
        source_map_path=args.source_map,
        suite_path=args.suite,
        case_ids=tuple(args.case or ()),
        manifest_paths=tuple(args.case_manifest or ()),
        run_id_arg=args.run_id,
        allow_budget_override=bool(args.allow_budget_override),
        ambient_pythonhashseed=os.environ.get("PYTHONHASHSEED", "UNSET"),
    )
    print("run %s finished with exit code %d" % (run_id, rc))
    return rc


def _cmd_batch(args: argparse.Namespace) -> int:
    from tools.benchmarks import runner

    root = (args.root or _repo_root()).resolve()
    return runner.run_batch_child(
        root=root,
        manifest_path=str(args.case_manifest),
        source_map_path=str(args.source_map),
        partial_out=args.partial_out,
        batch_index=args.batch_index,
        warmup_operations=args.warmup_operations,
        operations=args.operations,
    )


def _cmd_compare(args: argparse.Namespace) -> int:
    from tools.benchmarks import compare

    rc, report = compare.compare_run(
        candidate_run_dir=args.candidate.resolve(),
        baseline_store_dir=args.baseline_store.resolve(),
        policy_path=args.policy,
        out_dir=args.out.resolve(),
        runs_root=args.runs_root.resolve() if args.runs_root else None,
    )
    summary = report["summary"]
    print(
        "compare finished rc=%d | REGRESSION=%d NO_REGRESSION=%d INCONCLUSIVE=%d "
        "INCOMPARABLE=%d NO_BASELINE=%d INVALID=%d"
        % (
            rc,
            summary["REGRESSION"], summary["NO_REGRESSION"], summary["INCONCLUSIVE"],
            summary["INCOMPARABLE"], summary["NO_BASELINE"], summary["INVALID"],
        )
    )
    return rc


def _cmd_promote(args: argparse.Namespace) -> int:
    from tools.benchmarks import contracts as C
    from tools.benchmarks import compare

    run_dir = args.run.resolve()
    run_summary = C.load_json_file(run_dir / "run.json", "run summary")
    store = compare.BaselineStore((args.output_root.resolve() / "baselines"))
    for case_id in args.case:
        record_path = run_dir / "results" / ("%s.json" % case_id.replace("/", "__"))
        record = C.load_json_file(record_path, "case result record")
        C.validate_result_record(record)
        version = store.promote(
            case_id=case_id,
            record=record,
            source_run_id=str(run_summary.get("run_id")),
            source_run_dir=run_dir,
            promoter=args.promoter,
        )
        print("promoted %s to baseline v%d" % (case_id, version))
    return 0


def _cmd_drift(args: argparse.Namespace) -> int:
    from tools.benchmarks import compare
    from tools.benchmarks import contracts as C

    root = (args.root or _repo_root()).resolve()
    run_dir = args.run.resolve()
    _, records = compare.load_run(run_dir)
    selected = args.case or sorted(records)
    unknown = [c for c in selected if c not in records]
    if unknown:
        raise C.ContractError("run has no case records for: %s" % unknown)
    out = args.out.resolve()
    if out.exists():
        raise C.ContractError("drift output file already exists: %s" % out)
    reports = []
    any_drift = False
    for case_id in selected:
        report = compare.drift_check(root, records[case_id])
        reports.append(report)
        if report["status"] == "DRIFT":
            any_drift = True
    C.write_json_atomic(out, {"schema": C.DRIFT_REPORT_SCHEMA, "cases": reports})
    print("drift check: %s" % ("DRIFT" if any_drift else "NO_DRIFT"))
    return 3 if any_drift else 0


def _cmd_validate_case(args: argparse.Namespace) -> int:
    from tools.benchmarks import contracts as C
    from tools.benchmarks import runner

    root = (args.root or _repo_root()).resolve()
    manifest_path = args.case_manifest.resolve()
    manifest = C.load_json_file(manifest_path, "case manifest")
    C.validate_case_manifest(manifest)
    for fixture in manifest.get("fixtures", []):
        info = runner.hash_relative(root, str(fixture["path"]))
        print("fixture %s %s" % (fixture["path"], info["sha256"][:16]))
    info = runner.hash_relative(root, str(manifest["workload"]["source"]))
    print("workload %s %s" % (manifest["workload"]["source"], info["sha256"][:16]))
    print("case %s VALID" % manifest["case_id"])
    return 0


_HANDLERS = {
    "run": _cmd_run,
    "batch": _cmd_batch,
    "compare": _cmd_compare,
    "promote": _cmd_promote,
    "drift": _cmd_drift,
    "validate-case": _cmd_validate_case,
}


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)  # usage errors exit 2 before any import
    from tools.benchmarks import contracts as C

    try:
        return _HANDLERS[args.command](args)
    except C.ContractError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
