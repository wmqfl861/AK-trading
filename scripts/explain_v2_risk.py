#!/usr/bin/env python
"""NF30 risk-semantics sidecar CLI (work order NF30, contract C30).

Explains the scope/basis/population of v2 risk metrics in an existing
metrics.json (ak_probability_metrics_v2.1.4) and, when supplied, a selected
risk_report.json (ak_probability_risk_report_v2.1.0) plus its explicit
read-only binding.

Read-only mapping + sidecar validation. No SDK, no network, no database, no
research-run side effects, no mutation of producer artifacts. Rejections exit
non-zero and write no success marker. The output root must be new (existing
evidence is never overwritten).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from ak_trading.v2.validation.risk_semantics import (  # noqa: E402
    RiskSemanticsError, build_risk_semantics, write_outputs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="explain_v2_risk.py", description=__doc__)
    parser.add_argument("--metrics", required=True,
                        help="path to metrics.json (ak_probability_metrics_v2.1.4)")
    parser.add_argument("--risk-report", default=None,
                        help="path to risk_report.json (ak_probability_risk_report_v2.1.0)")
    parser.add_argument("--binding", default=None,
                        help="path to binding JSON declaring artifact_sha256, split_id, "
                             "run_policy_sha256, account_id, model_bundle_sha256, cost_scenario "
                             "for the risk_report")
    parser.add_argument("--output-root", required=True,
                        help="explicit NEW output root (must not exist; never overwrites evidence)")
    parser.add_argument("--source-root", default=None,
                        help="repository root holding the producer sources "
                             "(default: inferred from this checkout)")
    args = parser.parse_args(argv)
    try:
        sidecar = build_risk_semantics(
            metrics_path=args.metrics,
            risk_report_path=args.risk_report,
            binding_path=args.binding,
            source_root=args.source_root)
        result = write_outputs(sidecar, args.output_root)
    except RiskSemanticsError as error:
        print(json.dumps({"rejected": error.code, "detail": error.detail},
                         ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
