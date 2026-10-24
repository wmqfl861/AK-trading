"""Read-only PostgreSQL to synchronized normalized-research CSV export.

Reads ``market_bars`` with SELECT-only statements and produces a strict,
gap-free, synchronized CSV that the unmodified ``research_multistrategy.py
--csv`` path accepts. Bars are never fabricated and runs are never spliced
across gaps: only contiguous synchronized grid segments are exported.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ak_trading.market_reference import SHANGHAI_TZ
from ak_trading.persistence.config import DatabaseSettings, redact_database_text
from ak_trading.persistence.database import DatabaseOperationError
from ak_trading.persistence.market_data import datetime_from_nano
from ak_trading.research import Bar
from ak_trading.research_io import MAX_CSV_BYTES, csv_bytes

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

CONTINUOUS_MAIN_PREFIX = "KQ.m@"
ALLOWED_DURATIONS = (60, 86400)
MAX_INSTRUMENTS = 16
MAX_ROWS = 200_000
MIN_ROWS = 4
MIN_TRAIN_BARS = 100
MAX_CONFIG_BYTES = 65_536
_MAX_LISTED_RUNS = 20
_MAX_LISTED_GAPS = 20
_AUTO_SYMBOL_CAP = 256


class ResearchExportError(DatabaseOperationError):
    """A credential-safe export, mapping, or synchronization failure."""


@dataclass(frozen=True)
class Segment:
    """One maximal contiguous run of the synchronized grid."""

    start_nano: int
    end_nano: int
    frames: int
    start_index: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": _iso_nano(self.start_nano),
            "end": _iso_nano(self.end_nano),
            "frames": self.frames,
        }


@dataclass(frozen=True)
class ResearchExportResult:
    path: str
    config_path: str | None
    duration_seconds: int
    raw_symbols: tuple[str, ...]
    symbols: tuple[str, ...]
    frames: int
    rows: int
    csv_bytes_count: int
    csv_sha256: str
    segment: dict[str, Any]
    runs: tuple[dict[str, Any], ...]
    gap_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "config_path": self.config_path,
            "duration_seconds": self.duration_seconds,
            "raw_symbols": list(self.raw_symbols),
            "symbols": list(self.symbols),
            "frames": self.frames,
            "rows": self.rows,
            "csv_bytes": self.csv_bytes_count,
            "csv_sha256": self.csv_sha256,
            "segment": dict(self.segment),
            "runs": [dict(run) for run in self.runs],
            "gap_count": self.gap_count,
        }


def _iso_nano(nano: int) -> str:
    return datetime_from_nano(nano).astimezone(SHANGHAI_TZ).isoformat()


def _bounded_int(value: object, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ResearchExportError(f"{name} must be an integer between {low} and {high}")
    return value


def _safe_failure(action: str, exc: BaseException, settings: DatabaseSettings | None) -> ResearchExportError:
    return ResearchExportError(f"{action} failed: {redact_database_text(exc, settings)}")


def map_research_symbol(raw: object) -> str:
    """Map one database symbol to the normalized research charset.

    ``KQ.m@SHFE.rb`` becomes ``SHFE.rb`` by stripping the main-continuous
    prefix. Any other ``@``-bearing symbol (for example the ``KQ.i@`` index
    series) is rejected rather than silently relabeled.
    """
    if not isinstance(raw, str):
        raise ResearchExportError("symbol must be a string")
    symbol = raw.strip()
    if not symbol:
        raise ResearchExportError("symbol must not be blank")
    if "@" in symbol:
        if not symbol.startswith(CONTINUOUS_MAIN_PREFIX):
            raise ResearchExportError(
                f"symbol rejected: {symbol!r} is not a main continuous series"
            )
        symbol = symbol[len(CONTINUOUS_MAIN_PREFIX) :]
    if (
        not symbol
        or not symbol.isascii()
        or not all(c.isalnum() or c in "_.-" for c in symbol)
        or symbol.startswith("KQ.")
    ):
        raise ResearchExportError(
            f"symbol rejected: {raw.strip()!r} cannot map to the normalized research charset"
        )
    return symbol


def build_research_universe(
    raw_symbols: object, *, max_instruments: int = MAX_INSTRUMENTS
) -> dict[str, str]:
    """Validate requested symbols and return raw -> mapped, sorted by mapped name."""
    max_instruments = _bounded_int(max_instruments, "max_instruments", 1, MAX_INSTRUMENTS)
    if isinstance(raw_symbols, str) or not isinstance(raw_symbols, (list, tuple)):
        raise ResearchExportError("symbols must be a list of strings")
    entries: list[str] = []
    seen: set[str] = set()
    for raw in raw_symbols:
        if not isinstance(raw, str) or not raw.strip():
            raise ResearchExportError("symbol entries must be non-empty strings")
        entry = raw.strip()
        if entry in seen:
            raise ResearchExportError(f"duplicate symbol requested: {entry}")
        seen.add(entry)
        entries.append(entry)
    if not 1 <= len(entries) <= max_instruments:
        raise ResearchExportError(
            f"request between 1 and {max_instruments} symbols (got {len(entries)})"
        )
    universe: dict[str, str] = {}
    for entry in entries:
        mapped = map_research_symbol(entry)
        for previous, existing in universe.items():
            if existing == mapped:
                raise ResearchExportError(
                    f"symbol collision: {entry} and {previous} both map to {mapped}"
                )
        universe[entry] = mapped
    return dict(sorted(universe.items(), key=lambda item: item[1]))


def synchronized_segments(grid: list[int], duration_seconds: int) -> list[Segment]:
    """Split an ascending common timestamp grid into duration-uniform runs."""
    if duration_seconds not in ALLOWED_DURATIONS:
        raise ResearchExportError(
            f"duration_seconds must be one of {list(ALLOWED_DURATIONS)}"
        )
    step = duration_seconds * 1_000_000_000
    runs: list[Segment] = []
    start = 0
    for index in range(1, len(grid) + 1):
        if index == len(grid) or grid[index] - grid[index - 1] != step:
            if index > start:
                runs.append(
                    Segment(grid[start], grid[index - 1], index - start, start)
                )
            start = index
    return runs


def _select_segment(runs: list[Segment], min_frames: int) -> Segment:
    if not runs:
        raise ResearchExportError(
            "no synchronized timestamps across the requested instruments inside the window"
        )
    best = max(runs, key=lambda run: (run.frames, -run.start_nano))
    if best.frames < min_frames:
        detail = {
            "required_frames": min_frames,
            "best_frames": best.frames,
            "run_count": len(runs),
            "runs": [run.as_dict() for run in runs[:_MAX_LISTED_RUNS]],
            "gaps": _gap_details(runs)[:_MAX_LISTED_GAPS],
            "hint": "narrow --start/--end around one session or fix the missing data",
        }
        raise ResearchExportError(
            "no gap-free synchronized segment reaches the required minimum: "
            + json.dumps(detail, ensure_ascii=True, sort_keys=True)
        )
    return best


def _gap_details(runs: list[Segment]) -> list[dict[str, Any]]:
    gaps = []
    for first, second in zip(runs, runs[1:]):
        gaps.append(
            {
                "after": _iso_nano(first.end_nano),
                "before": _iso_nano(second.start_nano),
                "gap_seconds": (second.start_nano - first.end_nano) // 1_000_000_000,
            }
        )
    return gaps


def build_research_bars(
    series_by_symbol: dict[str, dict[int, Any]],
    universe: dict[str, str],
    segment_nanos: list[int],
    duration_seconds: int,
) -> list[Bar]:
    """Construct research bars with close_time = open_time + duration - 1 second."""
    span = timedelta(seconds=duration_seconds - 1)
    bars: list[Bar] = []
    for nano in segment_nanos:
        open_time = datetime_from_nano(nano).astimezone(SHANGHAI_TZ)
        stamp = open_time.isoformat()
        for raw, mapped in universe.items():
            row = series_by_symbol[raw][nano]
            values: dict[str, float] = {}
            for name in ("open", "high", "low", "close"):
                source = getattr(row, name)
                if source is None:
                    raise ResearchExportError(
                        f"null {name} for {raw} at {stamp}"
                    )
                value = float(source)
                if not math.isfinite(value) or value <= 0:
                    raise ResearchExportError(
                        f"non-finite or non-positive {name} for {raw} at {stamp}"
                    )
                values[name] = value
            if not (
                values["low"] <= min(values["open"], values["close"])
                and max(values["open"], values["close"]) <= values["high"]
            ):
                raise ResearchExportError(f"invalid OHLC ordering for {raw} at {stamp}")
            bars.append(
                Bar(
                    mapped,
                    open_time,
                    open_time + span,
                    values["open"],
                    values["high"],
                    values["low"],
                    values["close"],
                )
            )
    return bars


def demo_config_for(frames: object) -> dict[str, int]:
    """Companion research config using the train_bars floor of 100."""
    frames = _bounded_int(frames, "frames", 1, MAX_ROWS)
    if frames <= MIN_TRAIN_BARS:
        raise ResearchExportError(
            f"segment has {frames} frames; the research train_bars floor "
            f"{MIN_TRAIN_BARS} requires at least {MIN_TRAIN_BARS + 1} frames "
            "(train_bars < frames so one OOS bar exists)"
        )
    return {"train_bars": MIN_TRAIN_BARS}


def _skip_scan_symbols(
    engine: Engine,
    *,
    duration_seconds: int,
    lower: str | None,
    upper: str | None,
    cap: int,
    settings: DatabaseSettings | None,
) -> list[str]:
    """Enumerate ascending distinct symbols through indexed min() seeks.

    A plain ``SELECT DISTINCT`` cannot use the ``(symbol, duration)`` index when
    only the duration is constrained, so it times out on large tables. Each
    ``min(symbol) WHERE symbol > previous`` step is an index seek instead.
    """
    from sqlalchemy import select

    from ak_trading.persistence.schema import market_bars

    found: list[str] = []
    previous: str | None = None
    try:
        with engine.connect() as connection:
            while True:
                statement = select(market_bars.c.symbol).where(
                    market_bars.c.duration_seconds == duration_seconds
                )
                if previous is None:
                    if lower is not None:
                        statement = statement.where(market_bars.c.symbol >= lower)
                else:
                    statement = statement.where(market_bars.c.symbol > previous)
                if upper is not None:
                    statement = statement.where(market_bars.c.symbol < upper)
                statement = statement.order_by(market_bars.c.symbol.asc()).limit(1)
                value = connection.execute(statement).scalar()
                if value is None:
                    break
                found.append(value)
                previous = value
                if len(found) > cap:
                    raise ResearchExportError(
                        "too many distinct symbols for auto selection "
                        f"(more than {cap}); pass --symbols explicitly"
                    )
    except ResearchExportError:
        raise
    except Exception as exc:
        raise _safe_failure("auto symbol resolution", exc, settings) from None
    return found


def resolve_auto_symbols(
    engine: Engine,
    *,
    duration_seconds: int,
    limit: int,
    settings: DatabaseSettings | None = None,
) -> list[str]:
    """Distinct symbols for one duration, main-continuous first, deterministic."""
    limit = _bounded_int(limit, "limit", 1, MAX_INSTRUMENTS)
    # The window upper bound keeps every 'KQ.m@'-prefixed symbol inside while
    # the Python-side prefix filter guarantees correctness regardless of
    # collation details.
    window_upper = CONTINUOUS_MAIN_PREFIX + "z" * 100
    main = [
        symbol
        for symbol in _skip_scan_symbols(
            engine,
            duration_seconds=duration_seconds,
            lower=CONTINUOUS_MAIN_PREFIX,
            upper=window_upper,
            cap=_AUTO_SYMBOL_CAP,
            settings=settings,
        )
        if symbol.startswith(CONTINUOUS_MAIN_PREFIX)
    ]
    pool = main
    if not pool:
        pool = _skip_scan_symbols(
            engine,
            duration_seconds=duration_seconds,
            lower=None,
            upper=None,
            cap=_AUTO_SYMBOL_CAP,
            settings=settings,
        )
    if not pool:
        raise ResearchExportError(
            "no symbols found for the requested duration; import data first"
        )
    return pool[:limit]


def _fetch_series(
    engine: Engine,
    *,
    symbol: str,
    duration_seconds: int,
    start_nano: int | None,
    end_nano: int | None,
    fetch_limit: int,
    settings: DatabaseSettings | None,
) -> dict[int, Any]:
    from sqlalchemy import select

    from ak_trading.persistence.schema import market_bars

    statement = select(
        market_bars.c.symbol,
        market_bars.c.datetime_nano,
        market_bars.c.ts,
        market_bars.c.open,
        market_bars.c.high,
        market_bars.c.low,
        market_bars.c.close,
    ).where(
        market_bars.c.symbol == symbol,
        market_bars.c.duration_seconds == duration_seconds,
    )
    if start_nano is not None:
        statement = statement.where(market_bars.c.datetime_nano >= start_nano)
    if end_nano is not None:
        statement = statement.where(market_bars.c.datetime_nano < end_nano)
    statement = statement.order_by(market_bars.c.datetime_nano.asc()).limit(fetch_limit + 1)
    try:
        with engine.connect() as connection:
            rows = connection.execute(statement).all()
    except Exception as exc:
        raise _safe_failure("market-bar query", exc, settings) from None
    series: dict[int, Any] = {}
    for row in rows:
        if row.datetime_nano in series:
            raise ResearchExportError(
                f"duplicate bar identity for {symbol} at {_iso_nano(row.datetime_nano)}"
            )
        series[row.datetime_nano] = row
    if len(rows) > fetch_limit:
        raise ResearchExportError(
            f"row guard exceeded for {symbol}: more than {fetch_limit} bars in the "
            "window; narrow --start/--end or raise --max-rows"
        )
    return series


def _publish_csv(target: Path, payload: bytes) -> None:
    """Atomically publish the CSV; never overwrite an existing target."""
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # A same-directory hard link publishes the fully fsynced inode while
        # atomically failing if another writer created the target first.
        os.link(temp_path, target)
        temp_path.unlink()
    except FileExistsError:
        raise ResearchExportError(f"export target already exists: {target}") from None
    except OSError as exc:
        raise ResearchExportError(f"cannot publish export target: {exc}") from None
    finally:
        temp_path.unlink(missing_ok=True)


def _publish_config(target: Path, payload: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        raise ResearchExportError(f"config target already exists: {target}") from None
    except OSError as exc:
        raise ResearchExportError(f"cannot write config target: {exc}") from None


def export_research_input_csv(
    engine: Engine,
    output: str | Path,
    *,
    symbols: object = "auto",
    duration_seconds: int = 60,
    start_nano: int | None = None,
    end_nano: int | None = None,
    max_instruments: int = 3,
    max_rows: int = MAX_ROWS,
    config_out: str | Path | None = None,
    settings: DatabaseSettings | None = None,
) -> ResearchExportResult:
    """Export the longest gap-free synchronized segment as a strict research CSV."""
    if isinstance(duration_seconds, bool) or duration_seconds not in ALLOWED_DURATIONS:
        raise ResearchExportError(
            f"duration_seconds must be one of {list(ALLOWED_DURATIONS)}"
        )
    max_instruments = _bounded_int(max_instruments, "max_instruments", 1, MAX_INSTRUMENTS)
    max_rows = _bounded_int(max_rows, "max_rows", MIN_ROWS, MAX_ROWS)
    target = Path(output)
    if target.suffix.lower() != ".csv":
        raise ResearchExportError("output must be a .csv path")
    if target.exists():
        raise ResearchExportError(f"export target already exists: {target}")
    config_target: Path | None = None
    config_payload: bytes | None = None
    if config_out is not None:
        config_target = Path(config_out)
        if config_target.suffix.lower() != ".json":
            raise ResearchExportError("config output must be a .json path")
        if config_target.exists():
            raise ResearchExportError(f"config target already exists: {config_target}")

    requested: list[str]
    if isinstance(symbols, str):
        if symbols.strip() == "auto":
            requested = resolve_auto_symbols(
                engine,
                duration_seconds=duration_seconds,
                limit=max_instruments,
                settings=settings,
            )
        else:
            requested = symbols.split(",")
    elif isinstance(symbols, (list, tuple)):
        requested = list(symbols)
    else:
        raise ResearchExportError('symbols must be "auto" or a comma-separated list')
    universe = build_research_universe(requested, max_instruments=max_instruments)

    series_by_symbol: dict[str, dict[int, Any]] = {}
    common: set[int] | None = None
    for raw in universe:
        series = _fetch_series(
            engine,
            symbol=raw,
            duration_seconds=duration_seconds,
            start_nano=start_nano,
            end_nano=end_nano,
            fetch_limit=max_rows,
            settings=settings,
        )
        if not series:
            raise ResearchExportError(f"no bars for {raw} in the requested window")
        series_by_symbol[raw] = series
        keys = set(series)
        common = keys if common is None else common & keys
    grid = sorted(common or ())
    runs = synchronized_segments(grid, duration_seconds)
    # validate_bars needs >= 2 frames and >= 4 rows: 1 instrument needs 4 frames.
    min_frames = 4 if len(universe) == 1 else 2
    best = _select_segment(runs, min_frames)
    segment_nanos = grid[best.start_index : best.start_index + best.frames]

    bars = build_research_bars(series_by_symbol, universe, segment_nanos, duration_seconds)
    rows = len(bars)
    if rows > max_rows:
        raise ResearchExportError(
            f"row guard exceeded: {best.frames} frames x {len(universe)} instruments "
            f"= {rows} rows > max_rows {max_rows}; narrow --start/--end or raise --max-rows"
        )
    try:
        payload = csv_bytes(bars)
    except ValueError as exc:
        raise ResearchExportError(
            f"exported bars fail the strict research contract: {exc}"
        ) from None
    if len(payload) > MAX_CSV_BYTES:
        raise ResearchExportError(
            f"export exceeds the {MAX_CSV_BYTES} byte CSV limit ({len(payload)} bytes); "
            "narrow the window or reduce instruments"
        )
    if config_target is not None:
        config = demo_config_for(best.frames)
        config_payload = (
            json.dumps(config, sort_keys=True, indent=2, allow_nan=False) + "\n"
        ).encode("utf-8")
        if len(config_payload) > MAX_CONFIG_BYTES:
            raise ResearchExportError("config target exceeds the 64 KiB limit")

    # The companion config is written first; the CSV is published last so a
    # successful return always implies the main artifact exists.
    if config_target is not None and config_payload is not None:
        _publish_config(config_target, config_payload)
        try:
            _publish_csv(target, payload)
        except ResearchExportError:
            config_target.unlink(missing_ok=True)
            raise
    else:
        _publish_csv(target, payload)

    return ResearchExportResult(
        path=str(target.resolve()),
        config_path=str(config_target.resolve()) if config_target is not None else None,
        duration_seconds=duration_seconds,
        raw_symbols=tuple(universe),
        symbols=tuple(universe.values()),
        frames=best.frames,
        rows=rows,
        csv_bytes_count=len(payload),
        csv_sha256=hashlib.sha256(payload).hexdigest(),
        segment=best.as_dict(),
        runs=tuple(run.as_dict() for run in runs[:_MAX_LISTED_RUNS]),
        gap_count=max(len(runs) - 1, 0),
    )
