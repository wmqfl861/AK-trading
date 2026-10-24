"""Run a frozen real-futures price event study without accounts or data services."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-contracts-per-product", type=int, default=3)
    parser.add_argument("--min-rows", type=int, default=120)
    args = parser.parse_args(argv)
    if not 1 <= args.max_contracts_per_product <= 3 or not 120 <= args.min_rows <= 10000:
        parser.error("max contracts must be 1..3 and min rows must be 120..10000")
    if not args.input_bundle.is_dir():
        print(json.dumps({"status": "blocked", "reason": "input_bundle_missing"}))
        return 3
    from ak_trading.futures.price_study import run_price_study
    try:
        result = run_price_study(
            args.input_bundle, args.output,
            max_contracts_per_product=args.max_contracts_per_product,
            min_rows=args.min_rows,
        )
    except (ValueError, OSError, RuntimeError) as exc:
        print(json.dumps({"status": "rejected", "error_type": type(exc).__name__,
                          "reason": str(exc)[:240]}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 3 if result["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
