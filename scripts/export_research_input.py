"""Export a strict synchronized research CSV from PostgreSQL market_bars.

Read-only: SELECT statements only, no TqSdk imports, no trading, no database
writes. Output feeds the unmodified ``research_multistrategy.py --csv`` path.
"""
import argparse
import json
from pathlib import Path
import sys

# Run from a source checkout without installation or global environment changes.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ak_trading.persistence.config import load_database_settings, redact_database_text
from ak_trading.persistence.database import create_database_engine
from ak_trading.persistence.query import parse_aware_datetime_nano
from ak_trading.research_export import (
    ALLOWED_DURATIONS,
    MAX_INSTRUMENTS,
    MAX_ROWS,
    MIN_ROWS,
    ResearchExportError,
    export_research_input_csv,
)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, '{"status":"error","detail":"invalid arguments; use --help"}\n')


def main() -> int:
    parser = Parser(
        description="Read-only PostgreSQL export of a synchronized normalized research CSV"
    )
    parser.add_argument(
        "--symbols",
        default="auto",
        help='comma-separated database symbols such as "KQ.m@SHFE.rb,KQ.m@CZCE.TA" or "auto"',
    )
    parser.add_argument("--start", help="inclusive timezone-aware ISO datetime")
    parser.add_argument("--end", help="exclusive timezone-aware ISO datetime")
    parser.add_argument(
        "--duration", type=int, choices=ALLOWED_DURATIONS, default=60,
        help="bar duration in seconds (60 or 86400)",
    )
    parser.add_argument("--output", type=Path, required=True, help="new .csv output path")
    parser.add_argument(
        "--max-instruments", type=int, default=3,
        help=f"instrument cap for the universe, 1..{MAX_INSTRUMENTS}",
    )
    parser.add_argument(
        "--max-rows", type=int, default=MAX_ROWS,
        help=f"row guard, {MIN_ROWS}..{MAX_ROWS}",
    )
    parser.add_argument(
        "--config-out", type=Path, default=None,
        help="optional new .json path for the companion research config (train_bars=100)",
    )
    args = parser.parse_args()
    settings = None
    engine = None
    try:
        if not 1 <= args.max_instruments <= MAX_INSTRUMENTS:
            raise ResearchExportError(
                f"--max-instruments must be between 1 and {MAX_INSTRUMENTS}"
            )
        if not MIN_ROWS <= args.max_rows <= MAX_ROWS:
            raise ResearchExportError(
                f"--max-rows must be between {MIN_ROWS} and {MAX_ROWS}"
            )
        start = parse_aware_datetime_nano(args.start, "--start") if args.start else None
        end = parse_aware_datetime_nano(args.end, "--end") if args.end else None
        if start is not None and end is not None and start >= end:
            raise ResearchExportError("--start must be before --end")
        settings = load_database_settings(required=True)
        assert settings is not None
        engine = create_database_engine(settings)
        result = export_research_input_csv(
            engine,
            args.output,
            symbols=args.symbols,
            duration_seconds=args.duration,
            start_nano=start,
            end_nano=end,
            max_instruments=args.max_instruments,
            max_rows=args.max_rows,
            config_out=args.config_out,
            settings=settings,
        )
    except Exception as exc:
        # Do not echo file contents, user arguments, paths or credentials on failure.
        print(
            json.dumps(
                {
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "detail": redact_database_text(exc, settings)[:4000],
                }
            )
        )
        return 2
    finally:
        if engine is not None:
            engine.dispose()
    print(
        json.dumps(
            {"status": "complete", **result.as_dict()},
            sort_keys=True,
            allow_nan=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
