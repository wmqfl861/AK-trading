import argparse
import math
from pathlib import Path

from ak_trading.artifacts import (
    build_run_manifest,
    default_journal_path,
    new_run_id,
    report_path_for,
    require_distinct_artifact_paths,
    write_journal_report,
)
from ak_trading.config import load_settings
from ak_trading.journal import JsonlJournal, redact_text
from ak_trading.persistence.config import load_database_settings, redact_database_text
from ak_trading.persistence.database import create_database_engine
from ak_trading.persistence.storage import import_run_artifacts
from ak_trading.risk import require_positive_position_limit
from ak_trading.runner import StrategyRunner
from ak_trading.runtime import is_valid_price, wait_until
from ak_trading.session import create_api
from ak_trading.strategy import DualMAStrategy
from ak_trading.symbols import require_concrete_symbol, resolve_trade_symbol


def _error_text(exc: BaseException, settings=None, database_settings=None) -> str:
    text = redact_database_text(exc, database_settings)
    secrets = ()
    if settings is not None:
        secrets = (settings.tq_user, settings.tq_pass)
    return redact_text(text, secrets=secrets)[:200]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the dual-MA strategy against live data with local TqSim"
    )
    parser.add_argument("--symbol", help="main or concrete futures symbol")
    parser.add_argument("--duration", type=int, default=300, help="K-line seconds")
    parser.add_argument("--fast", type=int, default=5, help="fast MA bars")
    parser.add_argument("--slow", type=int, default=20, help="slow MA bars")
    parser.add_argument("--balance", type=float, default=1_000_000.0)
    parser.add_argument(
        "--max-abs-position", type=int, default=1, help="maximum absolute target lots"
    )
    parser.add_argument(
        "--timeout", type=float, default=60.0, help="startup/shutdown wall seconds"
    )
    parser.add_argument(
        "--journal", type=Path, help="decision journal path; defaults to a unique run path"
    )
    parser.add_argument("--report", type=Path, help="JSON report path beside the journal")
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        print("BAD_TIMEOUT", args.timeout)
        return 2

    settings = None
    database_settings = None
    try:
        if args.journal is not None and args.report is not None:
            require_distinct_artifact_paths(args.journal, args.report)
        if args.duration <= 0:
            raise ValueError("duration must be a positive integer")
        if not math.isfinite(args.balance) or args.balance <= 0:
            raise ValueError("balance must be a finite positive number")
        strategy = DualMAStrategy(args.fast, args.slow)
        max_abs_position = require_positive_position_limit(args.max_abs_position)
        settings = load_settings()
        database_settings = load_database_settings()
        market_symbol = (args.symbol or settings.default_symbol).strip()
        if not market_symbol:
            raise ValueError("symbol is required (--symbol or TQ_DEFAULT_SYMBOL)")
        api = create_api(settings, sim_balance=args.balance)
    except (RuntimeError, ValueError) as exc:
        print(
            "SETUP_FAIL",
            type(exc).__name__,
            _error_text(exc, settings, database_settings),
        )
        return 2

    runner = None
    journal_path: Path | None = None
    report_path: Path | None = None
    run_id = new_run_id()
    exit_code = 0
    try:
        seed_quote = api.get_quote(market_symbol)

        def seed_ready() -> bool:
            ins_class = str(getattr(seed_quote, "ins_class", "") or "").upper()
            if not seed_quote.datetime or not ins_class:
                return False
            is_main = market_symbol.startswith("KQ.m@") or ins_class in {
                "CONT",
                "CONTINUOUS",
            }
            if is_main and not str(
                getattr(seed_quote, "underlying_symbol", "") or ""
            ).strip():
                return False
            resolve_trade_symbol(seed_quote, market_symbol)
            return True

        if not wait_until(api, seed_ready, timeout=args.timeout):
            print("RESOLVE_TIMEOUT", market_symbol)
            return 2
        trade_symbol = require_concrete_symbol(
            resolve_trade_symbol(seed_quote, market_symbol)
        )

        trade_quote = api.get_quote(trade_symbol)

        def trade_ready() -> bool:
            if not trade_quote.datetime or not getattr(trade_quote, "ins_class", ""):
                return False
            if not is_valid_price(trade_quote.last_price):
                return False
            return resolve_trade_symbol(trade_quote, trade_symbol) == trade_symbol

        if not wait_until(api, trade_ready, timeout=args.timeout):
            print("TRADE_QUOTE_TIMEOUT", trade_symbol)
            return 2

        journal_path = args.journal or default_journal_path(
            Path("data/runs"),
            prefix="dual_ma_sim",
            symbol=trade_symbol,
            run_id=run_id,
        )
        report_path = args.report or report_path_for(journal_path)
        require_distinct_artifact_paths(journal_path, report_path)
        journal = JsonlJournal(journal_path)
        journal(
            build_run_manifest(
                run_id=run_id,
                mode="sim",
                symbol=trade_symbol,
                market_symbol=market_symbol,
                duration_seconds=args.duration,
                fast_window=args.fast,
                slow_window=args.slow,
                max_abs_position=max_abs_position,
                initial_balance=args.balance,
            )
        )
        runner = StrategyRunner(
            api,
            trade_symbol,
            strategy,
            mode="sim",
            duration_seconds=args.duration,
            max_abs_position=max_abs_position,
            decision_sink=journal,
            run_id=run_id,
        )
        print(
            "SIM_STARTED",
            "market_symbol",
            market_symbol,
            "trade_symbol",
            trade_symbol,
            "journal",
            journal_path,
            "report",
            report_path,
        )
        try:
            summary = runner.run()
            print("SIM_STOPPED", summary)
        except KeyboardInterrupt:
            print("SIM_INTERRUPTED")
            exit_code = max(exit_code, 130)
    except (RuntimeError, ValueError) as exc:
        print(
            "STRATEGY_FAIL",
            type(exc).__name__,
            _error_text(exc, settings, database_settings),
        )
        exit_code = 2
    finally:
        try:
            if runner is not None:
                shutdown = runner.shutdown_live(timeout=args.timeout)
                print("SIM_SHUTDOWN", shutdown)
                if not shutdown.flattened or not shutdown.task_finished:
                    exit_code = max(exit_code, 3)
        except KeyboardInterrupt:
            print("SIM_SHUTDOWN_INTERRUPTED")
            exit_code = max(exit_code, 3)
        except Exception as exc:
            print(
                "SIM_SHUTDOWN_FAIL",
                type(exc).__name__,
                _error_text(exc, settings, database_settings),
            )
            exit_code = max(exit_code, 3)
        finally:
            try:
                api.close()
            except Exception as exc:
                print(
                    "SIM_CLOSE_FAIL",
                    type(exc).__name__,
                    _error_text(exc, settings, database_settings),
                )
                exit_code = max(exit_code, 3)
            finally:
                if journal_path is not None and report_path is not None:
                    try:
                        report = write_journal_report(
                            journal_path, report_path, run_id=run_id
                        )
                        print("SIM_REPORT", report_path, report["lifecycle"]["state"])
                        if database_settings is not None:
                            database_engine = create_database_engine(database_settings)
                            try:
                                imported, stored_report = import_run_artifacts(
                                    database_engine,
                                    journal_path,
                                    report_path,
                                    settings=database_settings,
                                )
                            finally:
                                database_engine.dispose()
                            print(
                                "SIM_DATABASE_IMPORT",
                                imported.events_read,
                                stored_report.identity if stored_report else "no_report",
                            )
                    except Exception as exc:
                        print(
                            "SIM_REPORT_FAIL",
                            type(exc).__name__,
                            _error_text(exc, settings, database_settings),
                        )
                        exit_code = max(exit_code, 3)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
