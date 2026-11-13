"""Round-5 external helper: compare two ``scripts/test_offline.py`` logs.

Reads the ``OFFLINE-DOMAIN-STATS {...}`` line of every domain in both logs and
reports, by node identity (never by count):

    added failed / added errors / added collection errors   (regressions)
    resolved failed / resolved errors / resolved collection errors
    nodes only in BEFORE (population loss) and only in AFTER (new tests)

Exit code: 0 no additions, 1 additions, 2 unusable input.

This is the ~100-line comparison the round-4 FIXES K03 described. It needs no
capture descriptor, supervisor or environment fingerprint, so it also works on
a plain clone (Linux, CI, a reviewer's machine). It does not replace
``scripts/compare_test_baselines.py``, which binds the evidence chain on the
project's Windows host.

Usage:
    python compare_offline_logs.py BEFORE.log AFTER.log [--json OUT.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PREFIX = "OFFLINE-DOMAIN-STATS "
SETS = ("failed_nodeids", "error_nodeids", "collection_errors")


def load(path: Path) -> dict:
    merged = {"selected": set(), "failed_nodeids": set(), "error_nodeids": set(),
              "collection_errors": set(), "domains": 0,
              "counts": {"ran": 0, "passed": 0, "failed": 0, "skipped": 0, "errors": 0}}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith(PREFIX):
            continue
        stats = json.loads(line[len(PREFIX):])
        merged["domains"] += 1
        merged["selected"].update(stats.get("selected_nodeids", ()))
        for name in SETS:
            merged[name].update(stats.get(name, ()))
        for name in merged["counts"]:
            merged["counts"][name] += int(stats.get(name, 0))
    if not merged["domains"]:
        raise SystemExit(f"no {PREFIX.strip()} line in {path}")
    return merged


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        before, after = load(args.before), load(args.after)
    except (OSError, ValueError) as error:
        print(f"unusable input: {error}", file=sys.stderr)
        return 2
    report = {
        "before": {"log": str(args.before), "domains": before["domains"], **before["counts"],
                   "collection_error_files": len(before["collection_errors"])},
        "after": {"log": str(args.after), "domains": after["domains"], **after["counts"],
                  "collection_error_files": len(after["collection_errors"])},
        "only_in_before": sorted(before["selected"] - after["selected"]),
        "only_in_after_count": len(after["selected"] - before["selected"]),
    }
    added_total = 0
    for name in SETS:
        added = sorted(after[name] - before[name])
        resolved = sorted(before[name] - after[name])
        report[f"added_{name}"] = added
        report[f"resolved_{name}"] = resolved
        added_total += len(added)
    for side in ("before", "after"):
        print(side, json.dumps(report[side], ensure_ascii=False))
    for name in SETS:
        print(f"added {name}: {len(report['added_' + name])}   "
              f"resolved {name}: {len(report['resolved_' + name])}")
    print(f"only in before (population loss): {len(report['only_in_before'])}   "
          f"only in after (new tests): {report['only_in_after_count']}")
    for name in SETS:
        for node in report[f"added_{name}"]:
            print(f"  ADDED {name}: {node}")
    if args.json is not None:
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    return 1 if added_total else 0


if __name__ == "__main__":
    raise SystemExit(main())
