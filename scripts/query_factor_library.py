"""Query or verify the local Chinese futures research factor catalog."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ak_trading.factor_library import FactorLibrary, FactorLibraryError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("info", help="catalog counts, status and families")
    commands.add_parser("verify", help="check archive, source checksums and catalog integrity")
    products = commands.add_parser("products", help="research product IDs, not executable order symbols")
    products.add_argument("--limit", type=int, default=100)
    search = commands.add_parser("search", help="filter factor definitions with their product conditions")
    search.add_argument("--product")
    search.add_argument("--family")
    search.add_argument("--keyword")
    search.add_argument("--priority", choices=("P0", "P1", "P2"))
    search.add_argument("--limit", type=int, default=30)
    factor = commands.add_parser("show", help="one factor, applicable products and source evidence")
    factor.add_argument("factor_id")
    starters = commands.add_parser("starters", help="source package's baseline candidates, not validated signals")
    starters.add_argument("--product")
    starters.add_argument("--limit", type=int, default=30)
    args = parser.parse_args(argv)
    try:
        library = FactorLibrary()
        if args.command == "info":
            result = library.info()
        elif args.command == "verify":
            result = library.verify()
        elif args.command == "products":
            result = library.products(limit=args.limit)
        elif args.command == "show":
            result = library.factor(args.factor_id)
        elif args.command == "starters":
            result = library.starters(product=args.product, limit=args.limit)
        else:
            result = library.search(product=args.product, family=args.family, keyword=args.keyword,
                                    priority=args.priority, limit=args.limit)
    except (FactorLibraryError, OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
