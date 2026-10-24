import argparse
import math
from datetime import date
from pathlib import Path

from tqsdk import TqBacktest, TqSim

from ak_trading.artifacts import (
    build_run_manifest,
    default_journal_path,
    new_run_id,
    report_path_for,
    require_distinct_artifact_paths,
    write_journal_report,
)
from ak_trading.config import load_settings
from ak_trading.journal import CompositeJournal, JsonlJournal, redact_text
from ak_trading.persistence.config import load_database_settings, redact_database_text
from ak_trading.persistence.database import create_database_engine
from ak_trading.persistence.storage import PostgresJournal, store_run_report
from ak_trading.risk import require_positive_position_limit
from ak_trading.runner import StrategyRunner
from ak_trading.session import create_api
from ak_trading.strategy import DualMAStrategy
from ak_trading.symbols import require_concrete_symbol


def _error_text(exc: BaseException, settings=None, database_settings=None) -> str:
    text = redact_database_text(exc, database_settings)
    secrets = ()
    if settings is not None:
        secrets = (settings.tq_user, settings.tq_pass)
    return redact_text(text, secrets=secrets)[:200]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backtest dual-MA on one concrete futures contract"
    )
    parser.add_argument("--symbol", required=True, help="concrete EXCHANGE.contract")
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--duration", type=int, default=300, help="K-line seconds")
    parser.add_argument("--fast", type=int, default=5, help="fast MA bars")
    parser.add_argument("--slow", type=int, default=20, help="slow MA bars")
    parser.add_argument("--balance", type=float, default=1_000_000.0)
    parser.add_argument(
        "--max-abs-position", type=int, default=1, help="maximum absolute target lots"
    )
    parser.add_argument("--journal", type=Path)
    parser.add_argument("--report", type=Path, help="JSON report path beside the journal")
    args = parser.parse_args()

    settings = None
    database_settings = None
    try:
        if args.journal is not None and args.report is not None:
            require_distinct_artifact_paths(args.journal, args.report)
        symbol = require_concrete_symbol(args.symbol)
        if args.start > args.end:
            raise ValueError("start date must not be after end date")
        if args.duration <= 0:
            raise ValueError("duration must be a positive integer")
        if not math.isfinite(args.balance) or args.balance <= 0:
            raise ValueError("balance must be a finite positive number")
        max_abs_position = require_positive_position_limit(args.max_abs_position)
        strategy = DualMAStrategy(args.fast, args.slow)
        settings = load_settings()
        database_settings = load_database_settings()
    except (RuntimeError, ValueError) as exc:
        print(
            "SETUP_FAIL",
            type(exc).__name__,
            _error_text(exc, settings, database_settings),
        )
        return 2

    run_id = new_run_id()
    journal_path = args.journal or default_journal_path(
        Path("data/backtests"),
        prefix=f"dual_ma_{args.start}_{args.end}",
        symbol=symbol,
        run_id=run_id,
    )
    report_path = args.report or report_path_for(journal_path)
    require_distinct_artifact_paths(journal_path, report_path)
    database_engine = None
    try:
        file_journal = JsonlJournal(journal_path)
        if database_settings is None:
            journal = file_journal
        else:
            database_engine = create_database_engine(database_settings)
            journal = CompositeJournal(
                file_journal,
                PostgresJournal(
                    database_engine,
                    settings=database_settings,
                    source_path=journal_path,
                ),
            )
        journal(
            build_run_manifest(
                run_id=run_id,
                mode="backtest",
                symbol=symbol,
                duration_seconds=args.duration,
                fast_window=args.fast,
                slow_window=args.slow,
                max_abs_position=max_abs_position,
                initial_balance=args.balance,
                backtest_start=args.start.isoformat(),
                backtest_end=args.end.isoformat(),
            )
        )
    except Exception as exc:
        if database_engine is not None:
            database_engine.dispose()
        print(
            "JOURNAL_SETUP_FAIL",
            type(exc).__name__,
            _error_text(exc, settings, database_settings),
        )
        return 2
    sim = TqSim(init_balance=args.balance)
    api = None
    summary = None
    exit_code = 0
    try:
        api = create_api(
            settings,
            sim_balance=args.balance,
            sim_account=sim,
            backtest=TqBacktest(start_dt=args.start, end_dt=args.end),
        )
        runner = StrategyRunner(
            api,
            symbol,
            strategy,
            mode="backtest",
            duration_seconds=args.duration,
            max_abs_position=max_abs_position,
            decision_sink=journal,
            run_id=run_id,
        )
        summary = runner.run()
    except Exception as exc:
        print(
            "BACKTEST_FAIL",
            type(exc).__name__,
            _error_text(exc, settings, database_settings),
        )
        exit_code = 2
    finally:
        try:
            if api is not None:
                api.close()
        except Exception as exc:
            print(
                "BACKTEST_CLOSE_FAIL",
                type(exc).__name__,
                _error_text(exc, settings, database_settings),
            )
            exit_code = max(exit_code, 3)
        finally:
            try:
                report = write_journal_report(journal_path, report_path, run_id=run_id)
                print("BACKTEST_REPORT", report_path, report["lifecycle"]["state"])
                if database_engine is not None:
                    stored = store_run_report(
                        database_engine,
                        report_path,
                        settings=database_settings,
                    )
                    print("BACKTEST_DATABASE_REPORT", stored.identity)
            except Exception as exc:
                print(
                    "BACKTEST_REPORT_FAIL",
                    type(exc).__name__,
                    _error_text(exc, settings, database_settings),
                )
                exit_code = max(exit_code, 3)
            finally:
                if database_engine is not None:
                    database_engine.dispose()

    if exit_code == 0:
        print("BACKTEST_DONE", summary)
        print("BACKTEST_STAT", sim.tqsdk_stat)
        print("JOURNAL", journal_path)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
