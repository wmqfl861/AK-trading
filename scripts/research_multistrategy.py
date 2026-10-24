"""Offline multi-strategy research. No settings, credentials, DB or TqSdk imports."""
import argparse
import json
from pathlib import Path
import sys

# Run from a source checkout without installation or global environment changes.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ak_trading.research import run_research
from ak_trading.research_io import csv_bytes, demo_bars, load_config, load_csv, write_bundle


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, '{"status":"error","detail":"invalid arguments; use --help"}\n')


def main() -> int:
    parser = Parser(description="Offline normalized research comparison, not futures performance")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--demo", action="store_true")
    source.add_argument("--csv", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.output.exists():
            raise ValueError("output exists")
        config = load_config(args.config)
        if args.demo:
            bars = demo_bars()
            raw = csv_bytes(bars)
        else:
            bars, raw = load_csv(args.csv)
        report = run_research(bars, config)
        write_bundle(args.output, raw, report)
    except (OSError, ValueError, TypeError, OverflowError) as exc:
        # Do not echo file contents, user arguments, paths or credentials on failure.
        print(json.dumps({"status": "error", "error_type": type(exc).__name__,
                          "detail": "invalid input/config, existing output, or I/O failure; see --help and input contract"}))
        return 2
    print(json.dumps({"status": "complete", "accounting": report["accounting"],
                      "summary": report["summary"]}, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
