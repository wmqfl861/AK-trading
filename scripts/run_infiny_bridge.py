"""Run the loopback-only Infiny observation bridge.

The launcher intentionally has no SDK, database, LLM, or environment-provider
imports. History is loaded once during startup by ``DailyObserver`` and the
service only receives its evaluator callable.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Sequence

from ak_trading.infiny_bridge.service import (
    DEFAULT_HOST,
    BridgeService,
    build_server,
    serve_forever,
)


DEFAULT_ISOLATED_PORT = 17778
_ALLOWED_STRATEGIES = ("ma_trend", "donchian_trend", "zscore_reversion")


def _date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("history-through must be YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise argparse.ArgumentTypeError("history-through must be YYYY-MM-DD")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the loopback-only, observation-mode Infiny bridge"
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_ISOLATED_PORT)
    parser.add_argument("--symbol", action="append", required=True)
    parser.add_argument("--strategy", choices=_ALLOWED_STRATEGIES, default="ma_trend")
    parser.add_argument("--history-bundle", type=Path)
    parser.add_argument("--history-pointer", type=Path)
    parser.add_argument("--reference-root", type=Path)
    parser.add_argument("--history-through", type=_date)
    parser.add_argument("--audit-log", type=Path, required=True)
    parser.add_argument(
        "--append",
        action="store_true",
        help="append to an existing audit log; otherwise an existing path is rejected",
    )
    parser.add_argument("--allow-test", action="store_true")
    parser.add_argument("--max-workers", type=int, default=32)
    parser.add_argument("--evaluator-workers", type=int, default=8)
    parser.add_argument("--evaluation-budget", type=float, default=0.25)
    parser.add_argument("--body-timeout", type=float, default=3.0)
    parser.add_argument(
        "--raw-batches",
        type=Path,
        help="enable the raw batch sink; value is a directory whose active "
             "journal file is raw-batches.jsonl; omission disables capture",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _fail(label: str) -> int:
    print(label)
    return 2


def _validate_args(args: argparse.Namespace) -> str | None:
    if args.host != DEFAULT_HOST:
        return "HOST_REJECTED"
    if isinstance(args.port, bool) or not 1 <= args.port <= 65535:
        return "PORT_INVALID"
    if not args.symbol or any(not isinstance(symbol, str) or not symbol.strip() for symbol in args.symbol):
        return "SYMBOL_REQUIRED"
    if len(set(args.symbol)) != len(args.symbol):
        return "SYMBOL_DUPLICATE"
    if args.history_pointer is not None and (args.history_bundle is not None or args.history_through is not None):
        return "HISTORY_POINTER_CONFLICT"
    if args.history_through is not None and args.history_bundle is None:
        return "HISTORY_THROUGH_REQUIRES_BUNDLE"
    if args.history_bundle is not None and not args.history_bundle.exists():
        return "HISTORY_BUNDLE_MISSING"
    if args.history_bundle is not None and not args.history_bundle.is_dir():
        return "HISTORY_BUNDLE_INVALID"
    if args.audit_log.exists() and not args.append:
        return "AUDIT_LOG_EXISTS_USE_APPEND"
    if args.audit_log.exists() and not args.audit_log.is_file():
        return "AUDIT_LOG_INVALID"
    if isinstance(args.max_workers, bool) or not 1 <= args.max_workers <= 32:
        return "WORKER_LIMIT_INVALID"
    if isinstance(args.evaluator_workers, bool) or not 1 <= args.evaluator_workers <= 32:
        return "EVALUATOR_LIMIT_INVALID"
    if isinstance(args.evaluation_budget, bool) or not 0 < args.evaluation_budget < 15:
        return "EVALUATION_BUDGET_INVALID"
    if isinstance(args.body_timeout, bool) or not 0 < args.body_timeout <= 3:
        return "BODY_TIMEOUT_INVALID"
    if args.raw_batches is not None:
        raw = args.raw_batches
        if not raw.is_absolute():
            return "RAW_BATCHES_PATH_MUST_BE_ABSOLUTE"
        if raw.exists() and not raw.is_dir():
            return "RAW_BATCHES_PATH_INVALID"
    return None


def _status_line(observer: object, port: int, raw_sink: object | None = None) -> str:
    status = observer.status()  # type: ignore[attr-defined]
    output = {
        "event": "bridge_started",
        "host": DEFAULT_HOST,
        "port": port,
        "observer_only": True,
        "execution_enabled": False,
        "raw_sink": {"enabled": raw_sink is not None},
        "observer": status,
    }
    return json.dumps(output, ensure_ascii=True, separators=(",", ":"))


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    invalid = _validate_args(args)
    if invalid is not None:
        return _fail(invalid)

    # This is the one application decision import. It is deliberately delayed
    # until after CLI validation so bad launch arguments never load history.
    try:
        from ak_trading.infiny_bridge.decision import DailyObserver

        if args.history_pointer is not None:
            from ak_trading.infiny_bridge.history import ReloadingDailyObserver
            observer = ReloadingDailyObserver(args.history_pointer, tuple(args.symbol), strategy=args.strategy)
        else:
            observer = DailyObserver(
                args.history_bundle, tuple(args.symbol), strategy=args.strategy,
                expected_history_date=args.history_through,
            )
    except Exception:
        return _fail("OBSERVER_STARTUP_FAILED")

    service: BridgeService | None = None
    server = None
    raw_sink = None
    try:
        if args.raw_batches is not None:
            from ak_trading.infiny_bridge.raw_sink import RawBatchSink

            raw_sink = RawBatchSink(args.raw_batches)
            if not raw_sink.start():
                return _fail("RAW_SINK_START_FAILED")
        service = BridgeService(
            observer.evaluate,
            allowed_symbols=tuple(args.symbol),
            allow_test=args.allow_test,
            audit_log=args.audit_log,
            evaluator_workers=args.evaluator_workers,
            evaluation_budget_seconds=args.evaluation_budget,
            body_timeout_seconds=args.body_timeout,
            raw_sink=raw_sink,
        )
        if args.reference_root is not None:
            service.reference_root = args.reference_root
        server = build_server(
            host=DEFAULT_HOST,
            port=args.port,
            service=service,
            max_workers=args.max_workers,
        )
        if args.dry_run:
            print(_status_line(observer, server.server_address[1], raw_sink))
            server.server_close()
            service.close()
            return 0
        if args.history_pointer is not None:
            observer.start()
        print(_status_line(observer, server.server_address[1], raw_sink), flush=True)
        serve_forever(server)
    except KeyboardInterrupt:
        return 130
    except OSError:
        return _fail("BRIDGE_BIND_FAILED")
    except Exception:
        return _fail("BRIDGE_STARTUP_FAILED")
    finally:
        if server is not None:
            try:
                server.server_close()
            except OSError:
                pass
        if service is not None:
            service.close()
        if args.history_pointer is not None:
            observer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
