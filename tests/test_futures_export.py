"""S1 focused tests: read-only export mechanism, pagination, bounds and
bundle publishing (finalized-plan §十二.11, minimum 6 cases).

The read-only transaction mechanism is asserted via driver call order on a
fake connection and, for the real export, separately evidenced by the SHOW
observations recorded in the bundle manifest. SQLite/:memory: fixtures with
a single ``market_bars`` table verify SQL shape and pagination only — a fake
passing never claims a real database was contacted.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import create_engine, insert, text  # noqa: E402

from ak_trading.futures.data import datetime_to_nano, nano_to_datetime  # noqa: E402
from ak_trading.futures.io import (  # noqa: E402
    OutputDirectoryError,
    assert_output_directory_is_new,
    publish_input_bundle,
    read_input_bundle,
)
from ak_trading.persistence import futures_research  # noqa: E402
from ak_trading.persistence.futures_research import (  # noqa: E402
    ExportLimits,
    ExportRequest,
    ReadOnlySnapshotEvidence,
    begin_read_only_snapshot,
    select_contract_bars,
    validate_bar_row,
)
from ak_trading.persistence.schema import market_bars  # noqa: E402
from ak_trading.futures.spec import ProductKey  # noqa: E402

SH = timezone(timedelta(hours=8))


def _sqlite_engine():
    engine = create_engine("sqlite:///:memory:")
    market_bars.create(engine)
    return engine


def _row(symbol: str, day: date, *, close: str = "3000", volume: str | None = "1000"):
    session_close = datetime.combine(day, datetime.min.time(), tzinfo=SH) + timedelta(hours=15)
    return {
        "symbol": symbol,
        "duration_seconds": 86400,
        "datetime_nano": datetime_to_nano(session_close),
        "ts": session_close,
        "trading_day": day,
        "trading_day_source": "tqsdk",
        "open": Decimal(close),
        "high": Decimal(close) + Decimal("5"),
        "low": Decimal(close) - Decimal("5"),
        "close": Decimal(close),
        "volume": Decimal(volume) if volume is not None else None,
        "open_oi": None,
        "close_oi": None,
        "source": "tqsdk",
        "batch_id": "b1",
        "imported_at": session_close,
    }


@pytest.fixture()
def sqlite_engine_with_bars():
    engine = _sqlite_engine()
    rows = []
    row_id = 0
    for offset in range(30):
        day = date(2026, 7, 1) + timedelta(days=offset)
        for symbol in ("SHFE.rb2609", "SHFE.rb2610"):
            row_id += 1
            row = _row(symbol, day)
            row["id"] = row_id
            rows.append(row)
    with engine.begin() as connection:
        connection.execute(insert(market_bars), rows)
    return engine


def test_select_contract_bars_paginates_without_loss_or_duplication(
    sqlite_engine_with_bars,
) -> None:
    start = datetime_to_nano(datetime(2026, 7, 1, tzinfo=SH))
    end = datetime_to_nano(datetime(2026, 8, 1, tzinfo=SH))
    with sqlite_engine_with_bars.connect() as connection:
        pages = list(
            select_contract_bars(
                connection, ["SHFE.rb2609", "SHFE.rb2610"], 86400, start, end,
                page_size=7,
            )
        )
    collected = [row for page in pages for row in page]
    assert len(collected) == 60
    keys = [(row["symbol"], row["datetime_nano"]) for row in collected]
    assert len(set(keys)) == len(keys)  # no duplicates across page boundaries
    assert keys == sorted(keys)  # ordered keyset traversal
    assert all(len(page) <= 7 for page in pages)
    assert len(pages) >= 2  # pagination actually happened


def test_select_contract_bars_range_is_start_inclusive_end_exclusive(
    sqlite_engine_with_bars,
) -> None:
    boundary_start = datetime_to_nano(datetime(2026, 7, 2, tzinfo=SH))
    boundary_end = datetime_to_nano(datetime(2026, 7, 4, tzinfo=SH))
    with sqlite_engine_with_bars.connect() as connection:
        rows = [
            row
            for page in select_contract_bars(
                connection, ["SHFE.rb2609"], 86400, boundary_start, boundary_end, page_size=100
            )
            for row in page
        ]
    days = sorted(nano_to_datetime(row["datetime_nano"]).date() for row in rows)
    assert days == [date(2026, 7, 2), date(2026, 7, 3)]


def test_read_only_snapshot_mechanism_call_order_and_verification() -> None:
    class FakeResult:
        def __init__(self, value):
            self._value = value

        def scalar_one(self):
            return self._value

    class FakeTransaction:
        def __init__(self, log):
            self._log = log

        def rollback(self):
            self._log.append("rollback")

    class FakeConnection:
        dialect = type("D", (), {"driver": "fake_driver"})()

        def __init__(self, isolation, read_only):
            self.calls: list[str] = []
            self._isolation = isolation
            self._read_only = read_only

        def begin(self):
            self.calls.append("begin")
            return FakeTransaction(self.calls)

        def execute(self, statement, params=None):
            sql = str(statement).strip()
            self.calls.append(sql)
            if sql.startswith("SET TRANSACTION"):
                return FakeResult(None)
            if sql.startswith("SHOW transaction_isolation"):
                return FakeResult(self._isolation)
            if sql.startswith("SHOW transaction_read_only"):
                return FakeResult(self._read_only)
            raise AssertionError(f"unexpected statement before verification: {sql}")

    connection = FakeConnection("repeatable read", "on")
    evidence = begin_read_only_snapshot(connection)
    assert evidence.verified is True
    assert evidence.mechanism == "explicit_set_transaction_read_only"
    assert connection.calls == [
        "begin",
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY",
        "SHOW transaction_isolation",
        "SHOW transaction_read_only",
    ]

    mismatched = FakeConnection("read committed", "off")
    evidence_bad = begin_read_only_snapshot(mismatched)
    assert evidence_bad.verified is False
    assert evidence_bad.not_executed_reason == "show_verification_mismatch"


def test_validate_bar_row_preserves_null_volume_and_rejects_bad_prices() -> None:
    good = _row("SHFE.rb2609", date(2026, 7, 1))
    reason, bar = validate_bar_row(good)
    assert reason is None and bar is not None
    assert bar.volume == Decimal("1000")

    null_volume = _row("SHFE.rb2609", date(2026, 7, 1), volume=None)
    reason, bar = validate_bar_row(null_volume)
    assert reason is None and bar is not None and bar.volume is None

    negative = _row("SHFE.rb2609", date(2026, 7, 1), volume="-5")
    assert validate_bar_row(negative)[0] == "negative_volume_oi"

    non_positive = dict(good, open=Decimal("0"))
    assert validate_bar_row(non_positive)[0] == "invalid_ohlc"

    unordered = dict(good, high=Decimal("1"), low=Decimal("2"))
    assert validate_bar_row(unordered)[0] == "ordering_error"

    missing_ts = dict(good, ts="not-a-datetime")
    assert validate_bar_row(missing_ts)[0] == "conflicting_identity"


def _request(tmp_path, *, limits=None) -> ExportRequest:
    cache = tmp_path / "cache" / "universe.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        '{"generated_at":"2026-09-01T14:58:47+08:00","products":{"SHFE.rb":['
        '{"symbol":"SHFE.rb2609","expire_datetime":1789455600,"expired":false},'
        '{"symbol":"SHFE.rb2610","expire_datetime":1790751600,"expired":false}]}}',
        encoding="utf-8",
    )
    catalog = tmp_path / "catalog" / "catalog.json"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.write_text(
        '{"schema_version":1,"observed_at":"2026-08-05T20:30:00+08:00",'
        '"products":[{"exchange":"SHFE","code":"rb","instrument_kind":"future","status":"listed"}]}',
        encoding="utf-8",
    )
    return ExportRequest(
        products=(ProductKey("SHFE", "RB"),),
        span_start=datetime(2026, 7, 1, tzinfo=SH),
        span_end=datetime(2026, 8, 1, tzinfo=SH),
        as_of=datetime(2026, 8, 1, tzinfo=SH),
        universe_cache_path=cache,
        catalog_path=catalog,
        limits=limits or ExportLimits(max_contracts=10, max_bar_rows=1000, page_size=13),
    )


def _sqlite_snapshot_stub(evidence=None):
    def stub(connection):
        connection.begin()
        return evidence or ReadOnlySnapshotEvidence(
            mechanism="sqlite_test_stub_not_a_real_database",
            driver="sqlite_test_stub",
            observed_isolation="repeatable read",
            observed_read_only="on",
            verified=True,
        )

    return stub


def test_export_publishes_bundle_with_decimal_nano_roundtrip(
    tmp_path, sqlite_engine_with_bars, monkeypatch
) -> None:
    request = _request(tmp_path)
    output = tmp_path / "out"
    monkeypatch.setattr(
        futures_research, "begin_read_only_snapshot", _sqlite_snapshot_stub()
    )
    outcome = futures_research.export_futures_bundle(
        sqlite_engine_with_bars, request, output
    )
    assert outcome.status == "published", outcome.reasons
    assert outcome.row_counts["valid_rows"] == 60
    assert outcome.read_only_evidence.mechanism == "sqlite_test_stub_not_a_real_database"

    bundle = read_input_bundle(output)
    assert len(bundle.bars) == 60
    by_key = {(bar.symbol, bar.datetime_nano): bar for bar in bundle.bars}
    sample = by_key[("SHFE.rb2609", datetime_to_nano(datetime(2026, 7, 15, 15, 0, tzinfo=SH)))]
    assert sample.close == Decimal("3000")  # Decimal preserved exactly
    assert sample.volume == Decimal("1000")
    assert sample.trading_day == date(2026, 7, 15)
    assert sample.available_at is not None  # conservative import-time bound
    assert bundle.manifest["dataset_kind"] == "real_export"
    assert bundle.manifest["member_ranks"] == "not_requested"
    assert bundle.manifest["missing_sidecars"]["session_windows"].startswith("not_present")
    assert "bars.csv" in bundle.manifest["content_hashes"]

    # Publishing again into the same directory is refused (exclusive).
    with pytest.raises(OutputDirectoryError):
        assert_output_directory_is_new(output)


def test_export_rejects_invalid_rows_and_publishes_nothing(
    tmp_path, monkeypatch
) -> None:
    engine = _sqlite_engine()
    bad_rows = []
    for offset, row in enumerate(
        [
            _row("SHFE.rb2609", date(2026, 7, 1)),
            dict(_row("SHFE.rb2609", date(2026, 7, 2)), high=Decimal("1")),
            dict(_row("SHFE.rb2609", date(2026, 7, 3)), close=Decimal("0")),
        ],
        start=1,
    ):
        row["id"] = offset
        bad_rows.append(row)
    with engine.begin() as connection:
        connection.execute(insert(market_bars), bad_rows)
    request = _request(tmp_path)
    output = tmp_path / "out"
    monkeypatch.setattr(
        futures_research, "begin_read_only_snapshot", _sqlite_snapshot_stub()
    )
    outcome = futures_research.export_futures_bundle(engine, request, output)
    assert outcome.status == "validation_rejected"
    assert not output.exists()  # no directory, no success marker


def test_export_scope_limit_and_empty_range_never_truncate(
    tmp_path, sqlite_engine_with_bars, monkeypatch
) -> None:
    request_tight = _request(tmp_path, limits=ExportLimits(max_contracts=10, max_bar_rows=10, page_size=13))
    monkeypatch.setattr(
        futures_research, "begin_read_only_snapshot", _sqlite_snapshot_stub()
    )
    outcome = futures_research.export_futures_bundle(
        sqlite_engine_with_bars, request_tight, tmp_path / "out1"
    )
    assert outcome.status == "scope_limit"
    assert outcome.reasons[0].startswith("max_bar_rows_exceeded")
    assert not (tmp_path / "out1").exists()

    empty_request = _request(tmp_path)
    # A range with no rows at all.
    from dataclasses import replace

    empty_request = replace(
        empty_request,
        span_start=datetime(2027, 1, 1, tzinfo=SH),
        span_end=datetime(2027, 2, 1, tzinfo=SH),
        as_of=datetime(2027, 2, 1, tzinfo=SH),
    )
    outcome_empty = futures_research.export_futures_bundle(
        sqlite_engine_with_bars, empty_request, tmp_path / "out2"
    )
    assert outcome_empty.status == "empty_real_export"
    assert not (tmp_path / "out2").exists()


def test_output_directory_rejects_overlap_with_inputs(tmp_path) -> None:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    with pytest.raises(OutputDirectoryError):
        assert_output_directory_is_new(inputs / "nested", [inputs])
    with pytest.raises(OutputDirectoryError):
        assert_output_directory_is_new(inputs, [inputs / "child"])
    clean = tmp_path / "fresh"
    assert_output_directory_is_new(clean, [inputs])


def test_publish_input_bundle_manifest_is_written_last_with_complete_marker(
    tmp_path,
) -> None:
    output = tmp_path / "bundle"
    manifest = publish_input_bundle(
        output,
        dataset_kind="synthetic_demo",
        created_at="2026-09-07T00:00:00+08:00",
        bars_rows=[],
        contracts=[],
        metadata={"k": "v"},
        availability={"member_factor": {"status": "not_requested"}},
        request_info={"products": []},
        source_hashes={},
        read_only_transaction={"executed": False, "reason": "not_executed"},
        clock_policy={"d_status": "unavailable"},
    )
    assert manifest["status"] == "complete"
    assert manifest["schema_version"] == "futures_bundle_v1"
    files = sorted(p.name for p in output.iterdir())
    assert files == [
        "availability.json", "bars.csv", "contracts.json", "manifest.json",
        "metadata.json",
    ]


def test_cli_blocks_without_env_and_rejects_probe_options(tmp_path, monkeypatch) -> None:
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "export_futures_input.py"
    spec = importlib.util.spec_from_file_location("export_futures_input_cli", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.delenv("AK_DATABASE_URL", raising=False)
    code = module.main([
        "--products", "SHFE.rb", "--start", "2026-07-01T00:00:00+08:00",
        "--end", "2026-08-01T00:00:00+08:00", "--universe-cache", "u.json",
        "--catalog", "c.json", "--output", str(tmp_path / "o"),
    ])
    assert code == 3  # export_blocked, never a .env read

    code = module.main([
        "--products", "SHFE.rb", "--start", "2026-07-01T00:00:00+08:00",
        "--end", "2026-08-01T00:00:00+08:00", "--universe-cache", "u.json",
        "--catalog", "c.json", "--output", str(tmp_path / "o"),
        "--include-member-ranks",
    ])
    assert code == 2  # unsupported probing option rejected explicitly

    code = module.main([
        "--products", "NYSE.rb", "--start", "2026-07-01T00:00:00+08:00",
        "--end", "2026-08-01T00:00:00+08:00", "--universe-cache", "u.json",
        "--catalog", "c.json", "--output", str(tmp_path / "o"),
    ])
    assert code == 2
