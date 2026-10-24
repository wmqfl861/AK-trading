import argparse
from contextlib import closing
import json
from pathlib import Path
import tempfile

from ak_trading.config import load_settings
from ak_trading.journal import redact_text
from ak_trading.market_reference import ReferenceDataError, load_reference_book
from ak_trading.persistence.config import (
    DatabaseConfigurationError,
    load_database_settings,
    redact_database_text,
)
from ak_trading.persistence.database import DatabaseOperationError, create_database_engine
from ak_trading.persistence.market_data import (
    MarketDataImportError,
    import_market_bars,
    iter_tqsdk_kline_csv,
    write_recent_kline_frame_csv,
)
from ak_trading.persistence.storage import store_reference_snapshot
from ak_trading.tqsdk_errors import (
    TqSdkOperation,
    TqSdkOperationError,
    expected_tqsdk_error,
)


ROOT = Path(__file__).resolve().parents[1]


class RecentSnapshotError(RuntimeError):
    """An expected recent-window acquisition or validation failure."""


def _raise_expected_tqsdk_error(
    exc: Exception, *, operation: TqSdkOperation
) -> None:
    expected = expected_tqsdk_error(exc, operation=operation)
    if expected is None:
        raise exc
    raise expected from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Snapshot the free recent TqSdk K-line window into PostgreSQL"
    )
    parser.add_argument("--symbol", action="append", required=True)
    parser.add_argument("--duration", type=int, required=True, help="K-line seconds")
    parser.add_argument("--rows", type=int, default=10_000)
    parser.add_argument(
        "--reference-config",
        type=Path,
        default=ROOT / "config" / "market_reference.json",
    )
    return parser


def _validated_arguments(args) -> tuple[tuple[str, ...], int, int]:
    symbols = tuple(symbol.strip() for symbol in args.symbol)
    if any(not symbol for symbol in symbols):
        raise ValueError("--symbol must not be blank")
    if len(set(symbols)) != len(symbols):
        raise ValueError("--symbol values must be unique")
    if args.duration <= 0:
        raise ValueError("--duration must be positive")
    if not 1 <= args.rows <= 10_000:
        raise ValueError("--rows must be between 1 and 10000")
    return symbols, args.duration, args.rows


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        symbols, duration, requested_rows = _validated_arguments(args)
    except ValueError as exc:
        print("RECENT_SNAPSHOT_SETUP_FAIL", str(exc))
        return 2

    tq_settings = None
    database_settings = None
    engine = None
    api = None
    try:
        book = load_reference_book(args.reference_config)
        try:
            tq_settings = load_settings()
        except RuntimeError as exc:
            raise TqSdkOperationError(str(exc)) from exc

        from tqsdk import TqApi, TqAuth

        staged = []
        with tempfile.TemporaryDirectory(prefix="ak_trading_recent_klines_") as raw_dir:
            temp_dir = Path(raw_dir)
            try:
                api = TqApi(auth=TqAuth(tq_settings.tq_user, tq_settings.tq_pass))
            except Exception as exc:
                _raise_expected_tqsdk_error(exc, operation="session")
            with closing(api):
                for index, symbol in enumerate(symbols):
                    try:
                        frame = api.get_kline_serial(
                            symbol,
                            duration_seconds=duration,
                            data_length=requested_rows,
                        )
                    except Exception as exc:
                        _raise_expected_tqsdk_error(exc, operation="market_data")
                    csv_path = temp_dir / f"market_{index}.csv"
                    frame_result = write_recent_kline_frame_csv(
                        frame, csv_path, symbol=symbol
                    )
                    if frame_result.rows != requested_rows:
                        raise RecentSnapshotError(
                            f"{symbol} returned {frame_result.rows} valid rows; "
                            f"expected {requested_rows}"
                        )
                    staged.append((symbol, csv_path, frame_result))
            api = None

            # All downloads must finish and every CSV must pass a full validation pass
            # before the first database connection or per-symbol import transaction.
            for symbol, csv_path, frame_result in staged:
                validated_rows = sum(
                    1
                    for _ in iter_tqsdk_kline_csv(
                        csv_path,
                        symbol=symbol,
                        duration_seconds=duration,
                        calendar=book.calendar,
                    )
                )
                if validated_rows != frame_result.rows:
                    raise RecentSnapshotError(
                        f"{symbol} validation row count changed before import"
                    )

            database_settings = load_database_settings(required=True)
            assert database_settings is not None
            engine = create_database_engine(database_settings)
            snapshot = store_reference_snapshot(
                engine, args.reference_config, settings=database_settings
            )
            imported = []
            imports_complete = False
            try:
                for symbol, csv_path, frame_result in staged:
                    source_label = (
                        f"tqsdk:get_kline_serial:recent_{requested_rows}:"
                        f"{symbol}:{duration}:{frame_result.first_datetime_nano}:"
                        f"{frame_result.last_datetime_nano}"
                    )
                    result = import_market_bars(
                        engine,
                        csv_path,
                        symbol=symbol,
                        duration_seconds=duration,
                        calendar=book.calendar,
                        reference_sha256=snapshot.content_sha256,
                        source_label=source_label,
                        settings=database_settings,
                    )
                    imported.append(
                        {
                            "symbol": symbol,
                            "batch_id": result.batch_id,
                            "inserted": result.inserted,
                            "rows": result.rows_read,
                            "rows_written": result.rows_written,
                            "null_trading_days": result.null_trading_days,
                            "first_datetime_nano": frame_result.first_datetime_nano,
                            "last_datetime_nano": frame_result.last_datetime_nano,
                        }
                    )
                imports_complete = True
            finally:
                if not imports_complete and imported:
                    print(
                        "RECENT_SNAPSHOT_PARTIAL",
                        json.dumps(imported, ensure_ascii=True, sort_keys=True),
                    )
        print(
            "RECENT_SNAPSHOT_IMPORTED",
            json.dumps(imported, ensure_ascii=True, sort_keys=True),
        )
        return 0
    except (
        DatabaseConfigurationError,
        DatabaseOperationError,
        MarketDataImportError,
        OSError,
        RecentSnapshotError,
        ReferenceDataError,
        TqSdkOperationError,
    ) as exc:
        text = redact_database_text(exc, database_settings)
        secrets = ()
        if tq_settings is not None:
            secrets = (tq_settings.tq_user, tq_settings.tq_pass)
        print(
            "RECENT_SNAPSHOT_FAIL",
            type(exc).__name__,
            redact_text(text, secrets=secrets)[:300],
        )
        return 2
    finally:
        if api is not None:
            try:
                api.close()
            except Exception:
                pass
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
