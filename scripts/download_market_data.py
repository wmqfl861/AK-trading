import argparse
from contextlib import closing
from datetime import date
import os
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
from ak_trading.persistence.database import (
    DatabaseOperationError,
    create_database_engine,
    database_status,
)
from ak_trading.persistence.market_data import (
    MarketDataImportError,
    import_market_bars,
)
from ak_trading.persistence.storage import store_reference_snapshot
from ak_trading.tqsdk_errors import (
    TqSdkOperation,
    TqSdkOperationError,
    expected_tqsdk_error,
)


ROOT = Path(__file__).resolve().parents[1]


def _safe_error(exc: BaseException, tq_settings, db_settings) -> str:
    text = redact_database_text(exc, db_settings)
    if tq_settings is not None:
        text = redact_text(
            text, secrets=(tq_settings.tq_user, tq_settings.tq_pass)
        )
    return text[:300]


def _raise_expected_tqsdk_error(
    exc: Exception, *, operation: TqSdkOperation
) -> None:
    expected = expected_tqsdk_error(exc, operation=operation)
    if expected is None:
        raise exc
    raise expected from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Download one TqSdk K-line range and import it into PostgreSQL"
    )
    parser.add_argument("--symbol", required=True, help="one TqSdk symbol")
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--duration", required=True, type=int, help="K-line seconds")
    parser.add_argument(
        "--reference-config",
        type=Path,
        default=ROOT / "config" / "market_reference.json",
    )
    parser.add_argument(
        "--csv-output",
        type=Path,
        help="retain the downloaded CSV at this path; otherwise use a temporary file",
    )
    parser.add_argument(
        "--acknowledge-data-cost",
        action="store_true",
        help="confirm DataDownloader may require paid data permissions",
    )
    args = parser.parse_args(argv)

    if not args.acknowledge_data_cost:
        print("DOWNLOAD_CONFIRMATION_REQUIRED --acknowledge-data-cost")
        return 2
    if args.start > args.end:
        print("DOWNLOAD_SETUP_FAIL start date must not be after end date")
        return 2
    if args.duration <= 0:
        print("DOWNLOAD_SETUP_FAIL duration must be positive for K-lines")
        return 2

    tq_settings = None
    db_settings = None
    engine = None
    api = None
    temporary_csv = args.csv_output is None
    csv_path: Path | None = None
    download_finished = False
    try:
        try:
            tq_settings = load_settings()
        except RuntimeError as exc:
            raise TqSdkOperationError(str(exc)) from exc
        db_settings = load_database_settings(required=True)
        assert db_settings is not None
        status = database_status(db_settings)
        if not status["alembic_revision"]:
            raise DatabaseOperationError(
                "database is not migrated; run scripts/manage_database.py migrate"
            )
        book = load_reference_book(args.reference_config)
        engine = create_database_engine(db_settings)
        snapshot = store_reference_snapshot(
            engine, args.reference_config, settings=db_settings
        )

        from tqsdk import TqApi, TqAuth
        from tqsdk.tools import DataDownloader

        if args.csv_output is None:
            download_dir = ROOT / "data" / "downloads"
            download_dir.mkdir(parents=True, exist_ok=True)
            descriptor, raw_path = tempfile.mkstemp(
                prefix="ak_kline_", suffix=".csv", dir=download_dir
            )
            os.close(descriptor)
            Path(raw_path).unlink(missing_ok=True)
            csv_path = Path(raw_path)
        else:
            csv_path = args.csv_output
            csv_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            api = TqApi(auth=TqAuth(tq_settings.tq_user, tq_settings.tq_pass))
        except Exception as exc:
            _raise_expected_tqsdk_error(exc, operation="session")
        try:
            downloader = DataDownloader(
                api,
                symbol_list=args.symbol,
                dur_sec=args.duration,
                start_dt=args.start,
                end_dt=args.end,
                csv_file_name=str(csv_path),
            )
        except Exception as exc:
            _raise_expected_tqsdk_error(exc, operation="history_download")
        last_progress = -1
        with closing(api):
            while not downloader.is_finished():
                api.wait_update()
                progress = int(downloader.get_progress())
                if progress >= last_progress + 10:
                    print("DOWNLOAD_PROGRESS", progress)
                    last_progress = progress
        api = None
        download_finished = True

        result = import_market_bars(
            engine,
            csv_path,
            symbol=args.symbol,
            duration_seconds=args.duration,
            calendar=book.calendar,
            reference_sha256=snapshot.content_sha256,
            source_label=(
                f"tqsdk:{args.symbol}:{args.duration}:"
                f"{args.start.isoformat()}:{args.end.isoformat()}"
            ),
            settings=db_settings,
        )
        print(
            "DOWNLOAD_IMPORTED",
            "batch",
            result.batch_id,
            "rows",
            result.rows_read,
            "null_trading_days",
            result.null_trading_days,
        )
        if args.csv_output is not None:
            print("DOWNLOAD_CSV", csv_path)
        return 0
    except (
        DatabaseConfigurationError,
        DatabaseOperationError,
        MarketDataImportError,
        OSError,
        ReferenceDataError,
        TqSdkOperationError,
    ) as exc:
        print(
            "DOWNLOAD_FAIL",
            type(exc).__name__,
            _safe_error(exc, tq_settings, db_settings),
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
        if temporary_csv and csv_path is not None and csv_path.exists():
            csv_path.unlink(missing_ok=True)
            if download_finished:
                print("DOWNLOAD_TEMP_CSV_REMOVED", csv_path.name)


if __name__ == "__main__":
    raise SystemExit(main())
