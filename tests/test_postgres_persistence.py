from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path

import pytest
pytest.importorskip("sqlalchemy")
from sqlalchemy import func, select, text, update

from ak_trading.artifacts import build_run_manifest, write_journal_report
from ak_trading.journal import CompositeJournal, JsonlJournal
from ak_trading.market_reference import (
    SHANGHAI_TZ,
    build_preopen_report,
    load_reference_book,
    write_report_artifacts,
)
from ak_trading.persistence.config import load_test_database_settings
from ak_trading.persistence.database import create_database_engine, upgrade_database
from ak_trading.persistence.market_data import (
    MarketDataImportError,
    expected_kline_columns,
    import_market_bars,
    iter_tqsdk_kline_csv,
)
from ak_trading.persistence.query import (
    MarketDataQueryError,
    export_market_bars_csv,
    query_market_bars,
    summarize_market_data_quality,
)
from ak_trading.persistence.schema import (
    market_bars,
    preopen_reports,
    reference_products,
    strategy_events,
)
from ak_trading.persistence.storage import (
    PersistenceError,
    PersistenceConflictError,
    PostgresJournal,
    import_run_artifacts,
    load_reference_snapshot_bytes,
    store_preopen_report,
    store_reference_snapshot,
    store_run_report,
)


pytestmark = pytest.mark.postgres
ROOT = Path(__file__).resolve().parents[1]
_TABLES = (
    "market_bars",
    "ingestion_batches",
    "preopen_reports",
    "run_reports",
    "strategy_events",
    "strategy_runs",
    "reference_closed_dates",
    "reference_products",
    "reference_snapshots",
    "member_position_ranks",
)


@pytest.fixture(scope="session")
def postgres_settings():
    settings = load_test_database_settings()
    if settings is None:
        pytest.skip("AK_TEST_DATABASE_URL is not configured")
    upgrade_database(settings)
    upgrade_database(settings)
    return settings


@pytest.fixture
def postgres_engine(postgres_settings):
    engine = create_database_engine(postgres_settings)
    truncate = "TRUNCATE TABLE " + ", ".join(_TABLES) + " RESTART IDENTITY CASCADE"
    with engine.begin() as connection:
        connection.execute(text(truncate))
    try:
        yield engine
    finally:
        with engine.begin() as connection:
            connection.execute(text(truncate))
        engine.dispose()


def _run_events(run_id: str):
    manifest = build_run_manifest(
        run_id=run_id,
        mode="backtest",
        symbol="DCE.m2609",
        duration_seconds=60,
        fast_window=5,
        slow_window=20,
        max_abs_position=1,
        initial_balance=1_000_000.0,
        backtest_start="2026-01-05",
        backtest_end="2026-01-06",
    )
    started = {
        "event": "run_started",
        "run_id": run_id,
        "mode": "backtest",
        "symbol": "DCE.m2609",
        "position": 0,
    }
    finished = {
        "event": "run_finished",
        "run_id": run_id,
        "mode": "backtest",
        "symbol": "DCE.m2609",
        "reason": "backtest_finished",
        "updates": 2,
        "decisions": 1,
        "target_changes": 0,
        "final_position": 0,
    }
    return manifest, started, finished


def _nano(local_text: str, remainder: int) -> int:
    local = datetime.fromisoformat(local_text).replace(tzinfo=SHANGHAI_TZ)
    return int(local.timestamp()) * 1_000_000_000 + remainder


def _write_bars(path: Path, symbol: str) -> tuple[int, int, int]:
    nanos = (
        _nano("2026-07-28 21:01:00", 123_456_100),
        _nano("2026-07-28 21:01:00", 123_456_900),
        _nano("2027-01-04 09:01:00", 123_456_000),
    )
    display = (
        "2026-07-28 21:01:00.123456",
        "2026-07-28 21:01:00.123456",
        "2027-01-04 09:01:00.123456",
    )
    columns = expected_kline_columns(symbol)
    rows = [",".join(columns)]
    for timestamp, nano in zip(display, nanos, strict=True):
        rows.append(
            ",".join(
                [
                    timestamp,
                    str(nano),
                    "3100.125",
                    "3102.25",
                    "3099.75",
                    "3101.5",
                    "12",
                    "123.5",
                    "124.5",
                ]
            )
        )
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return nanos


def test_reference_snapshot_is_byte_exact_and_idempotent(
    postgres_engine, postgres_settings
):
    path = ROOT / "config" / "market_reference.json"

    first = store_reference_snapshot(
        postgres_engine, path, settings=postgres_settings
    )
    second = store_reference_snapshot(
        postgres_engine, path, settings=postgres_settings
    )

    assert first.inserted is True
    assert second.inserted is False
    assert second.snapshot_id == first.snapshot_id
    assert (
        load_reference_snapshot_bytes(
            postgres_engine, first.snapshot_id, settings=postgres_settings
        )
        == path.read_bytes()
    )
    with postgres_engine.connect() as connection:
        product_count = connection.execute(
            select(func.count()).select_from(reference_products)
        ).scalar_one()
    assert product_count == 3


def test_postgres_journal_keeps_payload_shape_and_detects_conflicts(
    postgres_engine, postgres_settings, tmp_path: Path
):
    journal_path = tmp_path / "run.jsonl"
    report_path = tmp_path / "run.summary.json"
    file_journal = JsonlJournal(journal_path)
    database_journal = PostgresJournal(
        postgres_engine,
        settings=postgres_settings,
        source_path=journal_path,
    )
    journal = CompositeJournal(file_journal, database_journal)
    events = _run_events("run-journal")
    for event in events:
        journal(event)
    write_journal_report(journal_path, report_path, run_id="run-journal")

    first_report = store_run_report(
        postgres_engine, report_path, settings=postgres_settings
    )
    second_report = store_run_report(
        postgres_engine, report_path, settings=postgres_settings
    )

    assert first_report.inserted is True
    assert second_report.inserted is False
    with postgres_engine.connect() as connection:
        rows = connection.execute(
            select(strategy_events.c.sequence_no, strategy_events.c.payload)
            .where(strategy_events.c.run_id == "run-journal")
            .order_by(strategy_events.c.sequence_no)
        ).all()
    assert [row.sequence_no for row in rows] == [1, 2, 3]
    assert [row.payload for row in rows] == list(events)
    assert all("sequence_no" not in row.payload for row in rows)

    replay = PostgresJournal(postgres_engine, settings=postgres_settings)
    replay(events[0])
    with pytest.raises(PersistenceConflictError, match="different content"):
        replay({**events[1], "position": 1})


def test_existing_artifact_import_is_idempotent(
    postgres_engine, postgres_settings, tmp_path: Path
):
    journal_path = tmp_path / "existing.jsonl"
    report_path = tmp_path / "existing.summary.json"
    journal = JsonlJournal(journal_path)
    for event in _run_events("run-import"):
        journal(event)
    write_journal_report(journal_path, report_path, run_id="run-import")

    first_journal, first_report = import_run_artifacts(
        postgres_engine,
        journal_path,
        report_path,
        settings=postgres_settings,
    )
    second_journal, second_report = import_run_artifacts(
        postgres_engine,
        journal_path,
        report_path,
        settings=postgres_settings,
    )

    assert first_journal.events_inserted == 3
    assert second_journal.events_inserted == 0
    assert first_report is not None and first_report.inserted is True
    assert second_report is not None and second_report.inserted is False


def test_preopen_report_is_tied_to_exact_reference_snapshot(
    postgres_engine, postgres_settings, tmp_path: Path
):
    config_path = ROOT / "config" / "market_reference.json"
    book = load_reference_book(config_path)
    report = build_preopen_report(
        book,
        book.calendar.next_trading_day(book.calendar.valid_from, include_current=True),
        datetime(2026, 1, 5, 8, 0, tzinfo=SHANGHAI_TZ),
        notice_check={"status": "offline_not_checked"},
    )
    json_path, markdown_path = write_report_artifacts(report, tmp_path)
    snapshot = store_reference_snapshot(
        postgres_engine, config_path, settings=postgres_settings
    )

    first = store_preopen_report(
        postgres_engine,
        json_path,
        markdown_path,
        reference_snapshot_id=snapshot.snapshot_id,
        settings=postgres_settings,
    )
    second = store_preopen_report(
        postgres_engine,
        json_path,
        markdown_path,
        reference_snapshot_id=snapshot.snapshot_id,
        settings=postgres_settings,
    )

    assert first.inserted is True
    assert second.inserted is False
    with postgres_engine.connect() as connection:
        stored = connection.execute(select(preopen_reports.c.payload)).scalar_one()
    assert stored["report_id"] == report["report_id"]

    tampered = json.loads(json_path.read_text(encoding="utf-8"))
    tampered["broker_markup"]["value"] = "99"
    json_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(PersistenceError, match="report_id"):
        store_preopen_report(
            postgres_engine,
            json_path,
            markdown_path,
            reference_snapshot_id=snapshot.snapshot_id,
            settings=postgres_settings,
        )


def test_market_bar_upsert_uses_nano_identity_and_null_calendar_count(
    postgres_engine, postgres_settings, tmp_path: Path
):
    symbol = "KQ.m@SHFE.rb"
    csv_path = tmp_path / "bars.csv"
    nanos = _write_bars(csv_path, symbol)
    config_path = ROOT / "config" / "market_reference.json"
    book = load_reference_book(config_path)
    snapshot = store_reference_snapshot(
        postgres_engine, config_path, settings=postgres_settings
    )

    first = import_market_bars(
        postgres_engine,
        csv_path,
        symbol=symbol,
        duration_seconds=60,
        calendar=book.calendar,
        reference_sha256=snapshot.content_sha256,
        settings=postgres_settings,
        chunk_size=2,
    )
    second = import_market_bars(
        postgres_engine,
        csv_path,
        symbol=symbol,
        duration_seconds=60,
        calendar=book.calendar,
        reference_sha256=snapshot.content_sha256,
        settings=postgres_settings,
    )

    assert first.inserted is True
    assert first.rows_read == 3
    assert first.rows_written == 3
    assert first.null_trading_days == 1
    assert second.inserted is False
    with postgres_engine.connect() as connection:
        rows = connection.execute(
            select(
                market_bars.c.datetime_nano,
                market_bars.c.ts,
                market_bars.c.trading_day,
            ).order_by(market_bars.c.datetime_nano)
        ).all()
    assert [row.datetime_nano for row in rows] == sorted(nanos)
    assert rows[0].ts == rows[1].ts
    assert sum(row.trading_day is None for row in rows) == 1

    with pytest.raises(MarketDataImportError, match="different metadata"):
        import_market_bars(
            postgres_engine,
            csv_path,
            symbol=symbol,
            duration_seconds=300,
            calendar=book.calendar,
            reference_sha256=snapshot.content_sha256,
            settings=postgres_settings,
        )


def test_market_bar_query_quality_and_atomic_export(
    postgres_engine, postgres_settings, tmp_path: Path
):
    symbol = "KQ.m@SHFE.rb"
    csv_path = tmp_path / "bars.csv"
    nanos = _write_bars(csv_path, symbol)
    book = load_reference_book(ROOT / "config" / "market_reference.json")
    import_market_bars(
        postgres_engine,
        csv_path,
        symbol=symbol,
        duration_seconds=60,
        calendar=book.calendar,
        settings=postgres_settings,
    )

    rows = query_market_bars(
        postgres_engine,
        symbol=symbol,
        duration_seconds=60,
        start_nano=nanos[0],
        end_nano=nanos[2],
        limit=10,
        settings=postgres_settings,
    )
    assert [row.datetime_nano for row in rows] == list(nanos[:2])
    assert rows[0].open == "3100.1250000000"

    with postgres_engine.begin() as connection:
        connection.execute(
            update(market_bars)
            .where(market_bars.c.datetime_nano == nanos[0])
            .values(high=Decimal("1"), volume=None)
        )
        connection.execute(
            update(market_bars)
            .where(market_bars.c.datetime_nano == nanos[2])
            .values(close_oi=Decimal("-1"))
        )

    quality = summarize_market_data_quality(
        postgres_engine,
        symbol=symbol,
        duration_seconds=60,
        settings=postgres_settings,
    )
    assert len(quality) == 1
    assert quality[0].rows == 3
    assert quality[0].distinct_datetime_nanos == 3
    assert quality[0].duplicate_identities == 0
    assert quality[0].null_trading_days == 1
    assert quality[0].null_ohlc == 0
    assert quality[0].invalid_ohlc == 1
    assert quality[0].null_volume_or_oi == 1
    assert quality[0].invalid_volume_or_oi == 1
    assert quality[0].non_increasing_identities == 0
    assert quality[0].largest_observed_interval_nano is not None

    export_path = tmp_path / "export.csv"
    exported = export_market_bars_csv(
        postgres_engine,
        export_path,
        symbol=symbol,
        duration_seconds=60,
        start_nano=nanos[1],
        end_nano=nanos[2],
        settings=postgres_settings,
    )
    assert exported.rows == 1
    parsed = list(
        iter_tqsdk_kline_csv(export_path, symbol=symbol, duration_seconds=60)
    )
    assert [bar.datetime_nano for bar in parsed] == [nanos[1]]
    assert parsed[0].open == Decimal("3100.1250000000")

    original = export_path.read_bytes()
    with pytest.raises(MarketDataQueryError, match="already exists"):
        export_market_bars_csv(
            postgres_engine,
            export_path,
            symbol=symbol,
            duration_seconds=60,
            settings=postgres_settings,
        )
    assert export_path.read_bytes() == original
    assert not list(tmp_path.glob(".export.csv.*.tmp"))

    empty_path = tmp_path / "empty.csv"
    with pytest.raises(MarketDataQueryError, match="no market bars"):
        export_market_bars_csv(
            postgres_engine,
            empty_path,
            symbol=symbol,
            duration_seconds=60,
            start_nano=nanos[2] + 1,
            settings=postgres_settings,
        )
    assert not empty_path.exists()
