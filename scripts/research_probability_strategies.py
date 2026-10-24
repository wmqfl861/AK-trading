"""Run factor, path-probability, net-EV and sampled-strategy research offline."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-bundle", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    from ak_trading.probability.pipeline import run_probability_research
    try:
        result = run_probability_research(args.input_bundle, args.output, args.config)
    except (ValueError, OSError, RuntimeError, TypeError, KeyError, ArithmeticError) as exc:
        print(json.dumps({"status": "rejected", "error_type": type(exc).__name__, "reason": "invalid_input_or_research_failed"}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "complete" else 3


if __name__ == "__main__":
    raise SystemExit(main())
