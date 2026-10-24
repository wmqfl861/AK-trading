"""Query retained contract margin and fee references without network or database access."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ak_trading.settlement_reference import _local_path, load_settlement_reference


def _output_path(value: str) -> Path:
    path = _local_path(value)
    if path.exists() or Path(value).is_symlink():
        raise ValueError("output must be a new local file; overwriting is disabled")
    if not path.parent.is_dir():
        raise ValueError("output parent directory must already exist")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--day", required=True, type=date.fromisoformat)
    parser.add_argument("--price", type=Decimal)
    parser.add_argument("--settlement", type=Decimal)
    parser.add_argument("--root", type=Path, default=ROOT / "data")
    parser.add_argument("--output", help="write JSON to a new local file (never overwrite)")
    try:
        args = parser.parse_args(argv)
        output = _output_path(args.output) if args.output else None
        report = load_settlement_reference(args.root, day=args.day, symbol=args.symbol,
                                           trade_price=args.price, settlement_price=args.settlement)
        encoded = json.dumps(report, ensure_ascii=True, indent=2, allow_nan=False) + "\n"
        if output:
            with output.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded)
        else:
            print(encoded, end="")
        return 0 if report["available"] else 1
    except (ValueError, OSError, ArithmeticError) as exc:
        print(f"settlement reference: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
