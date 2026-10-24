"""Check only the requested stored data; no downloads or scheduler changes.

Example (the caller must choose the reference date, including holidays):
    python scripts/check_data_freshness.py --expected-through 2026-09-07 \
        --symbol SHFE.rb2610 --symbol KQ.m@SHFE.rb --duration 86400 --exchange SHFE

--symbol is repeatable; the required --duration in seconds applies to every
symbol. Run separately for another duration. --exchange optionally samples the
latest ranking date for each requested exchange. At most 100 samples are allowed.

Settings use load_database_settings(required=True): the repository .env and the
existing environment. --env-file selects another local dotenv file without
changing the existing environment precedence. Credentials are never reported.
Only PostgreSQL is contacted; no market-data APIs, calendars or jobs are called.

JSON goes to stdout. --output publishes the same JSON to a NEW file in an
existing local directory. Existing files, Windows device names and UNC paths
are refused. A same-directory temporary file is fsynced then atomically linked
without replacement; filesystems without hard links fail safely. Daily bars use
the raw Asia/Shanghai date; other bars use stored trading_day when present, else
the raw date. Neither stored provenance nor historical completeness is certified.

Exit 0 means all requested samples are fresh (comparison date >= reference).
Exit 1 means stale/empty data. Exit 2 means any input, query, connection or output
error. observed_at is UTC wall-clock metadata, never the comparison reference.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath

from ak_trading.persistence.config import load_database_settings
from ak_trading.persistence.database import create_database_engine
from ak_trading.persistence.freshness import (
    FreshnessRequest,
    freshness_error_report,
    inspect_data_freshness,
)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse may echo supplied values; they can contain unknown secrets.
        raise ValueError("invalid arguments; use --help")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(
        description="Bounded read-only freshness of explicitly requested stored samples.",
        epilog=(
            "No holiday inference. Daily: raw Asia/Shanghai date. Other durations: "
            "stored trading_day if present, else raw date (unverified provenance). "
            "Samples do not certify historical completeness or completed sessions. "
            "Exit 0: all fresh; 1: stale/empty; 2: error."
        ),
        allow_abbrev=False,
    )
    parser.add_argument("--expected-through", required=True, metavar="YYYY-MM-DD",
                        help="explicit comparison reference; no wall-clock or calendar inference")
    parser.add_argument("--symbol", required=True, action="append",
                        help="exact stored symbol, repeatable; at most 100 total samples")
    parser.add_argument("--duration", required=True, type=int,
                        help="positive K-line seconds, applied to every requested symbol")
    parser.add_argument("--exchange", action="append", default=[],
                        help="ranking exchange, repeatable; only supplied exchanges are checked")
    parser.add_argument("--env-file", type=Path,
                        help="local dotenv file (default: repository .env); environment takes precedence")
    parser.add_argument("--output", help="NEW .json file in an existing local directory; never overwritten")
    return parser


def _local_path(value: str) -> Path:
    """Reject device/stream/network paths before touching the filesystem."""
    windows = PureWindowsPath(value)
    if not value or windows.drive.startswith("\\\\"):
        raise ValueError("a local filesystem path is required")
    reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", "CLOCK$"}
    reserved.update(f"{prefix}{number}" for prefix in ("COM", "LPT") for number in "123456789¹²³")
    for part in windows.parts:
        if part == windows.anchor or part in (".", ".."):
            continue
        if (part.rstrip(" .") != part or part.split(".", 1)[0].rstrip(" ").upper() in reserved
                or any(ord(char) < 32 or ord(char) == 127 or char in '<>:"|?*' for char in part)):
            raise ValueError("reserved or invalid local path")
    return Path(value).absolute()


def _output_path(value: str) -> Path:
    target = _local_path(value)
    if target.suffix.lower() != ".json":
        raise ValueError("--output must name a new .json file")
    if target.exists() or target.is_symlink():
        raise FileExistsError("--output must not already exist")
    if not target.parent.is_dir():
        raise ValueError("--output parent directory must already exist")
    return target


def _publish_json(target: Path, content: str) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", prefix=".freshness-",
            suffix=".tmp", dir=target.parent, delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        # Unlike replace/rename on POSIX, link atomically fails if target exists,
        # including a dangling symlink or a competing writer's newly created file.
        os.link(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _record_error(report: dict, error: Exception, stage: str) -> None:
    report["errors"].append({"stage": stage, "error_type": type(error).__name__})
    report.update(overall_status="error", passed=False, exit_code=2)


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        request = FreshnessRequest(
            expected_through=args.expected_through, symbols=tuple(args.symbol),
            duration_seconds=args.duration, exchanges=tuple(args.exchange),
        )
        target = _output_path(args.output) if args.output is not None else None
        if args.env_file is not None:
            args.env_file = _local_path(str(args.env_file))
            if not args.env_file.is_file():
                raise ValueError("--env-file must be an existing local file")
    except Exception as exc:
        print(json.dumps({
            "overall_status": "error", "passed": False, "exit_code": 2,
            "checks": [], "errors": [{"stage": "arguments", "error_type": type(exc).__name__}],
            "message": "Invalid request or path; use --help for required arguments.",
        }, sort_keys=True))
        return 2

    observed_at = datetime.now(timezone.utc)
    engine = None
    report = None
    stage = "settings"
    try:
        settings = (load_database_settings(required=True) if args.env_file is None else
                    load_database_settings(env_file=args.env_file, required=True))
        if settings is None:
            raise ValueError("database settings are required")
        settings = replace(
            settings, connect_timeout=min(settings.connect_timeout, 5),
            statement_timeout_ms=min(settings.statement_timeout_ms, 5000),
        )
        stage = "engine"
        engine = create_database_engine(settings)
        stage = "database"
        report = inspect_data_freshness(
            engine, settings=settings, request=request, observed_at=observed_at,
        )
    except Exception as exc:
        report = freshness_error_report(request, observed_at=observed_at, error=exc, stage=stage)
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception as exc:
                if report is not None:
                    _record_error(report, exc, "dispose")

    content = json.dumps(report, ensure_ascii=True, sort_keys=True, allow_nan=False)
    if target is not None:
        try:
            _publish_json(target, content)
        except Exception as exc:
            _record_error(report, exc, "output")
            content = json.dumps(report, ensure_ascii=True, sort_keys=True, allow_nan=False)
    print(content)
    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
