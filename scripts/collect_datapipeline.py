#!/usr/bin/env python
"""DP03 collection CLI entry (SYNTHETIC/offline default; r1).

Default mode is OFFLINE / SYNTHETIC (policy pins mode=OFFLINE_SYNTHETIC and
data_origin=SYNTHETIC): the only session this script can build in r1 is the
scripted SYNTHETIC session.  The real TqSdk entry exists behind
``--real-source`` but is the acceptance blocker NOT_RUN: the request is
refused BEFORE any tqsdk import, socket or credential read, with a clear
non-zero exit (H03.10).

Usage (candidate-tree layout; the package must be importable as
``ak_trading.datapipeline``):

    python -B scripts/collect_datapipeline.py \
        --output-root <absolute run root> \
        --universe <universe.json> \
        --synthetic-timeline <timeline.json> \
        [--symbols KQ.m@SHFE.rb,SHFE.rb2601] [--mode rotating_incremental]

Exit codes follow the frozen CLIExitCode table (0 only when everything
required is ACCEPTED with no gaps/unknowns/unpublished batches).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, List, Mapping, Optional, Sequence

EXIT_CONTRACT_VIOLATION = 1
EXIT_RUN_FAILED = 2

_PROGRAM = "collect_datapipeline.py (DP03 r1)"


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog=_PROGRAM)
    parser.add_argument("--output-root", required=True, help="absolute injected run root (required)")
    parser.add_argument("--universe", help="universe.json path (default: DP00 frozen path)")
    parser.add_argument(
        "--symbols",
        help="comma-separated source_symbol subset (default: all r1 targets in the universe)",
    )
    parser.add_argument(
        "--mode",
        choices=("rotating_incremental", "continuous"),
        default="rotating_incremental",
    )
    parser.add_argument(
        "--synthetic-timeline",
        help="SYNTHETIC timeline JSON (offline execution; required unless --real-source)",
    )
    parser.add_argument(
        "--real-source",
        action="store_true",
        help="request a REAL TqSdk session (NOT_RUN in r1; refused without user injection)",
    )
    parser.add_argument("--run-id", default=None)
    parser.add_argument(
        "--group-service-seconds",
        type=float,
        default=None,
        help="offline/test bound for one group's service window (default: frozen budget)",
    )
    parser.add_argument(
        "--stale-after-seconds",
        type=float,
        default=None,
        help="offline/test bound for the no-data staleness window (default: frozen budget)",
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def _default_universe_path(repo_root: Path) -> Path:
    return (
        repo_root
        / "docs"
        / "devplan-datapipeline-20260923-r1"
        / "DP00"
        / "r1"
        / "universe.json"
    )


def _load_universe_symbols(path: Path, subset: Optional[str]) -> List[tuple]:
    """Parse the DP00 universe.json symbol groups into (symbol, kind, product).

    Only ``r1_target: true`` members generate tasks (DP00 task_expansion_rule:
    942 symbols x {60, 86400} = 1884 tasks in r1).
    """
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    symbols_payload = payload.get("symbols", {})
    wanted = {s.strip() for s in subset.split(",")} if subset else None
    rows: List[tuple] = []
    if isinstance(symbols_payload, dict):
        groups: List[Sequence[Mapping[str, Any]]] = list(symbols_payload.values())
    else:
        groups = [symbols_payload]
    for group in groups:
        for entry in group:
            symbol = entry.get("source_symbol") or entry.get("symbol")
            if not symbol or entry.get("r1_target") is not True:
                continue
            if wanted is not None and symbol not in wanted:
                continue
            rows.append(
                (
                    symbol,
                    entry.get("instrument_kind", "UNKNOWN"),
                    entry.get("product_key", symbol.split("@")[-1].split(".")[0]),
                )
            )
    if not rows:
        raise SystemExit(f"{_PROGRAM}: universe yielded no r1 target symbols (subset={subset!r})")
    return rows


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)

    # H03.10: refuse the real entry BEFORE any import that could touch the
    # SDK surface (tqsdk is never imported without an authorized injection).
    if args.real_source:
        print(
            f"{_PROGRAM}: REAL TqSdk session entry is NOT_RUN in r1 (acceptance "
            "blocker); SYNTHETIC sessions only. Real construction requires a "
            "separate user-controlled injection and a passing real-integration "
            "review.",
            file=sys.stderr,
        )
        return EXIT_RUN_FAILED

    output_root = Path(args.output_root)
    if not output_root.is_absolute():
        print(f"{_PROGRAM}: --output-root must be an absolute injected path", file=sys.stderr)
        return EXIT_CONTRACT_VIOLATION

    try:
        from ak_trading.datapipeline.collector import CollectionEngine
        from ak_trading.datapipeline.contracts import (
            DataOrigin,
            InstrumentKind,
            UniverseSymbolRecord,
            universe_sha256,
        )
        from ak_trading.datapipeline.policy import default_policy
        from ak_trading.datapipeline.scheduler import FairRotatingScheduler, SchedulerStateFile
        from ak_trading.datapipeline.session_factory import (
            SyntheticSessionFactory,
            build_synthetic_frame,
        )
    except Exception as exc:  # pragma: no cover - environment guidance
        print(
            f"{_PROGRAM}: cannot import ak_trading.datapipeline ({type(exc).__name__}); "
            "the candidate-tree package (DP01 frozen contracts + DP03 modules) must "
            "be importable first",
            file=sys.stderr,
        )
        return EXIT_CONTRACT_VIOLATION

    if not args.synthetic_timeline:
        print(
            f"{_PROGRAM}: offline default requires --synthetic-timeline (no real "
            "source authorized in r1)",
            file=sys.stderr,
        )
        return EXIT_CONTRACT_VIOLATION

    repo_root = Path(__file__).resolve().parents[1]
    universe_path = Path(args.universe) if args.universe else _default_universe_path(repo_root)
    try:
        symbol_rows = _load_universe_symbols(universe_path, args.symbols)
    except (OSError, ValueError) as exc:
        print(f"{_PROGRAM}: universe load failed ({type(exc).__name__})", file=sys.stderr)
        return EXIT_CONTRACT_VIOLATION

    source_id = "tqsdk-free-realtime-get_kline-serial"
    records = [
        UniverseSymbolRecord(
            source_symbol=symbol,
            instrument_kind=InstrumentKind(kind) if kind in ("MAIN_CONTINUOUS", "INDEX", "CONTRACT") else InstrumentKind.MAIN_CONTINUOUS,
            product_key=product,
        )
        for symbol, kind, product in symbol_rows
    ]
    universe_hash = universe_sha256(
        dataset="futures_kline",
        source_id=source_id,
        durations_seconds=(60, 86400),
        symbols=records,
    )

    timeline_doc = json.loads(Path(args.synthetic_timeline).read_text(encoding="utf-8"))
    timelines = timeline_doc["timelines"]
    frames_spec = timeline_doc["frames"]
    import time as _time

    clock_nano = _time.time_ns
    frames = {
        (spec["symbol"], int(spec["duration_seconds"])): build_synthetic_frame(
            start_nano=int(spec["start_nano"]),
            duration_seconds=int(spec["duration_seconds"]),
            closed_rows=int(spec.get("closed_rows", 4)),
            live_rows=int(spec.get("live_rows", 1)),
            padding_rows=int(spec.get("padding_rows", 0)),
            invalid_rows=int(spec.get("invalid_rows", 0)),
            duplicate_rows=int(spec.get("duplicate_rows", 0)),
        )
        for spec in frames_spec
    }
    factory = SyntheticSessionFactory(
        frames=frames,
        timelines=timelines,
        clock_nano=clock_nano,
        ready_after_updates=int(timeline_doc.get("ready_after_updates", 0)),
    )

    state_file = SchedulerStateFile(output_root / "spool" / "worker" / "scheduler-state.json")
    scheduler = FairRotatingScheduler(
        source_id=source_id,
        symbols=symbol_rows,
        durations_seconds=(60, 86400),
        clock_nano=clock_nano,
        state_file=state_file,
    )
    if args.mode == "continuous" and not scheduler.continuous_eligible():
        print(
            f"{_PROGRAM}: continuous mode requires the WHOLE target set to fit "
            "the subscription budget; staying rotating_incremental",
            file=sys.stderr,
        )

    engine = CollectionEngine(
        worker_id="worker-1",
        dataset="futures_kline",
        scheduler=scheduler,
        session_factory=factory,
        store=None,  # DP02 storage facade not yet wired (G2 gate)
        status_sink=None,
        run_root=output_root,
        universe_sha256=universe_hash,
        policy=default_policy(),
        clock_nano=clock_nano,
        group_service_seconds=args.group_service_seconds,
        stale_after_seconds=args.stale_after_seconds,
    )
    summary = engine.run(run_id=args.run_id)
    print(
        json.dumps(
            {
                "run_id": summary.run_id,
                "terminal_state": summary.terminal_state,
                "exit_code": summary.exit_code,
                "snapshots": summary.snapshot_count,
                "requested_length_total": summary.requested_length_total,
                "returned_rows_total": summary.returned_rows_total,
                "pending_uncommitted": summary.pending_uncommitted,
                "reconnects": summary.reconnects,
                "backpressure_events": summary.backpressure_events,
            },
            sort_keys=True,
        )
    )
    return int(summary.exit_code or 0)


if __name__ == "__main__":
    raise SystemExit(main())
