from __future__ import annotations

import importlib.util
import json
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy.dialects import postgresql

from ak_trading.persistence.config import DatabaseSettings


SETTINGS = DatabaseSettings(
    "postgresql://inventory_user:inventory_password@db.invalid:55432/ak_inventory"
)


class FakeResult:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def mappings(self):
        return self

    def all(self):
        return self.rows

    def one(self):
        assert len(self.rows) == 1
        return self.rows[0]

    def first(self):
        return self.rows[0] if self.rows else None

    def scalar_one(self):
        return next(iter(self.one().values()))


class FakeConnection:
    """Database boundary recording SQL, with PostgreSQL transaction failure rules."""

    def __init__(self):
        self.transcript = []
        self.read_only = False
        self.aborted = False
        self.directory_error = None
        self.size_error = None
        self.query_error = None
        self.market_rows = []
        self.current_schema = "public"
        self.visible_schemas = {"public"}
        self.tables = [
            ("public", name, 123.0)
            for name in (
                "alembic_version", "reference_snapshots", "reference_products",
                "reference_closed_dates", "strategy_runs", "strategy_events",
                "run_reports", "preopen_reports", "ingestion_batches",
                "market_bars", "member_position_ranks",
            )
        ] + [("research", "unmapped_table", -1.0)]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.transcript.append(("ROLLBACK", {}))

    @contextmanager
    def begin(self):
        self.transcript.append(("BEGIN", {}))
        yield self

    @contextmanager
    def begin_nested(self):
        self.transcript.append(("SAVEPOINT", {}))
        try:
            yield self
        except Exception:
            self.transcript.append(("ROLLBACK TO SAVEPOINT", {}))
            self.aborted = False
            raise
        else:
            self.transcript.append(("RELEASE SAVEPOINT", {}))

    def execute(self, statement, parameters=None):
        compiled = statement.compile(dialect=postgresql.dialect())
        sql = " ".join(str(compiled).split())
        params = {**compiled.params, **(parameters or {})}
        self.transcript.append((sql, params))
        assert not self.aborted, "transaction was left aborted"
        if sql in ("SET TRANSACTION READ ONLY",
                   "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"):
            self.read_only = True
            return FakeResult()
        assert self.read_only, "business query ran before READ ONLY"
        if "set_config(" in sql:
            assert "statement_timeout" in sql
            return FakeResult()
        if "pg_database_size(" in sql:
            if self.size_error:
                self.aborted = True
                raise self.size_error
            return FakeResult([{"bytes": 193_273_528_320}])
        if "current_database()" in sql:
            return FakeResult([{
                "database": "ak_inventory", "server_version": "17.5",
                "schema_name": self.current_schema,
            }])
        if "pg_catalog.pg_namespace" in sql and "pg_catalog.pg_class" in sql:
            return FakeResult([
                {"schema_name": schema, "table_name": name,
                 "relation_kind": "r", "estimated_rows": estimate,
                 "visible": schema in self.visible_schemas}
                for schema, name, estimate in self.tables
            ])
        if sql == "SHOW data_directory":
            if self.directory_error:
                self.aborted = True
                raise self.directory_error
            return FakeResult([{"data_directory": "D:/PostgreSQL/data"}])
        if self.query_error and self.query_error[0] in sql:
            self.aborted = True
            raise self.query_error[1]
        if "FROM public.alembic_version" in sql:
            return FakeResult([{"version_num": "20260831_0003"}])
        if "count(" in sql.lower():
            for name, count in (
                ("reference_snapshots", 2), ("reference_products", 80),
                ("reference_closed_dates", 34),
            ):
                if f"FROM public.{name}" in sql:
                    return FakeResult([{"count": count}])
        if "FROM public.market_bars" in sql:
            assert "count(" not in sql.lower()
            assert "market_bars.symbol = " in sql
            assert "market_bars.duration_seconds = " in sql
            assert "ORDER BY public.market_bars.datetime_nano ASC" in sql
            assert "LIMIT" in sql
            return FakeResult(self.market_rows)
        if "FROM public.reference_snapshots" in sql:
            return FakeResult([{
                "id": 2, "calendar_valid_from": date(2026, 1, 1),
                "calendar_valid_through": date(2026, 12, 31),
                "imported_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
            }])
        raise AssertionError(f"unexpected inspection query: {sql}")


class FakeEngine:
    def __init__(self, connection=None):
        self.connection = connection or FakeConnection()
        self.disposed = False

    def connect(self):
        return self.connection

    def dispose(self):
        self.disposed = True


def test_default_inventory_uses_catalog_truth_in_a_read_only_transaction():
    from ak_trading.persistence.inspection import inspect_database

    engine = FakeEngine()
    report = inspect_database(engine, settings=SETTINGS)

    assert report["read_only"] is True
    assert report["isolation_level"] == "repeatable read"
    assert report["location"] == "db.invalid:55432/ak_inventory"
    assert report["database"] == "ak_inventory"
    assert report["server_version"] == "17.5"
    assert report["migration"] == {
        "status": "available", "revisions": ["20260831_0003"],
    }
    tables = {(row["schema"], row["name"]): row for row in report["tables"]}
    assert tables["public", "market_bars"]["estimated_rows"] == 123
    assert tables["public", "market_bars"]["approximate"] is True
    assert tables["research", "unmapped_table"]["estimated_rows"] is None
    assert report["schemas"] == ["public", "research"]
    assert report["estimates"]["source"] == "pg_catalog.pg_class.reltuples"
    assert report["expected_schema"]["missing_tables"] == [
        "exchange_rule_approvals", "exchange_rule_current", "exchange_rule_items",
        "exchange_rule_runs", "exchange_rule_snapshots", "exchange_rule_source_contents",
        "exchange_rule_source_documents",
    ]
    assert report["reference_counts"]["reference_products"] == {
        "status": "available", "rows": 80, "exact": True,
    }
    assert report["calendar_coverage"]["snapshot_id"] == 2
    assert report["calendar_coverage"]["valid_from"] == "2026-01-01"
    assert report["calendar_coverage"]["valid_through"] == "2026-12-31"
    assert report["calendar_coverage"]["scope"] == "latest_reference_snapshot"
    assert report["market_coverage"] == {"status": "not_requested"}
    assert report["data_directory"] == {
        "status": "available", "path": "D:/PostgreSQL/data",
    }
    transcript = engine.connection.transcript
    read_only_at = next(i for i, (sql, _) in enumerate(transcript)
                        if sql == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
    assert all(i > read_only_at for i, (sql, _) in enumerate(transcript)
               if sql.startswith(("SELECT", "SHOW")))
    assert not any("FROM public.market_bars" in sql for sql, _ in transcript)
    assert not any(sql.startswith(("CREATE", "INSERT", "UPDATE", "DELETE", "ALTER", "DROP"))
                   for sql, _ in transcript)
    assert "inventory_user" not in json.dumps(report)
    assert "inventory_password" not in json.dumps(report)


class FakeDatabaseError(Exception):
    def __init__(self, sqlstate):
        super().__init__("driver failure postgresql://leaked_user:leaked_password@db.invalid/private")
        self.sqlstate = sqlstate


def test_directory_permission_denial_recovers_the_transaction_and_hides_driver_text():
    from ak_trading.persistence.inspection import inspect_database

    engine = FakeEngine()
    engine.connection.directory_error = FakeDatabaseError("42501")

    report = inspect_database(engine, settings=SETTINGS)

    assert report["data_directory"] == {
        "status": "unavailable", "reason": "permission_denied",
    }
    assert report["reference_counts"]["reference_products"]["rows"] == 80
    assert report["migration"]["revisions"] == ["20260831_0003"]
    assert ("ROLLBACK TO SAVEPOINT", {}) in engine.connection.transcript
    assert not engine.connection.aborted
    assert "leaked" not in json.dumps(report)


def test_missing_reference_tables_do_not_prevent_catalog_inventory():
    from ak_trading.persistence.inspection import inspect_database

    engine = FakeEngine()
    engine.connection.tables = [
        row for row in engine.connection.tables
        if not row[1].startswith("reference_") and row[1] != "alembic_version"
    ]

    report = inspect_database(engine, settings=SETTINGS)

    assert report["migration"] == {"status": "unavailable", "reason": "table_absent"}
    for result in report["reference_counts"].values():
        assert result == {"status": "unavailable", "reason": "table_absent"}
    assert report["calendar_coverage"] == {
        "status": "unavailable", "reason": "table_absent",
    }
    assert not any("FROM public.reference_" in sql for sql, _ in engine.connection.transcript)
    assert "reference_snapshots" in report["expected_schema"]["missing_tables"]


@pytest.mark.parametrize("sqlstate, reason", [("42501", "permission_denied"),
                                             ("57014", "statement_timeout"),
                                             ("42P01", "table_absent"),
                                             ("XX000", "query_failed")])
def test_optional_reference_failure_does_not_hide_remaining_inventory(sqlstate, reason):
    from ak_trading.persistence.inspection import inspect_database

    engine = FakeEngine()
    engine.connection.query_error = ("FROM public.reference_products", FakeDatabaseError(sqlstate))

    report = inspect_database(engine, settings=SETTINGS)

    assert report["reference_counts"]["reference_products"] == {
        "status": "unavailable", "reason": reason,
    }
    assert report["reference_counts"]["reference_closed_dates"]["rows"] == 34
    assert report["calendar_coverage"]["snapshot_id"] == 2
    assert "leaked" not in json.dumps(report)


def _load_cli():
    path = Path(__file__).resolve().parents[1] / "scripts" / "manage_database.py"
    spec = importlib.util.spec_from_file_location("inspection_manage_database", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("arguments", [
    ["--symbol", "SHFE.rb"], ["--duration", "60"],
    ["--symbol", " ", "--duration", "60"],
    ["--symbol", "SHFE.rb", "--duration", "0"],
    ["--symbol", "SHFE.rb", "--duration", "-1"],
    ["--symbol", "SHFE.rb", "--duration", "2147483648"],
    ["--symbol", "SHFE.rb", "--duration", "60", "--limit", "0"],
    ["--symbol", "SHFE.rb", "--duration", "60", "--limit", "10001"],
    ["--symbol", "SHFE.rb", "--duration", "60", "--start", "2026-09-01T09:00:00"],
    ["--symbol", "SHFE.rb", "--duration", "60", "--end", "bad-input"],
    ["--symbol", "SHFE.rb", "--duration", "60", "--start", "2026-09-02T00:00:00Z",
     "--end", "2026-09-01T00:00:00Z"],
    ["--symbol", "SHFE.rb", "--duration", "60", "--start", "2026-09-01T08:00:00+08:00",
     "--end", "2026-09-01T00:00:00Z"],
    ["--symbol", "SHFE.rb", "--duration", "60", "--start", ""],
    ["--start", "2026-09-01T00:00:00Z"], ["--limit", "10"],
])
def test_invalid_inspect_inputs_fail_before_settings_are_loaded(arguments, monkeypatch, capsys):
    cli = _load_cli()

    def forbidden_settings(**kwargs):
        pytest.fail("invalid input attempted to load database settings")

    monkeypatch.setattr(cli, "load_database_settings", forbidden_settings)

    assert cli.main(["inspect", *arguments]) == 2
    assert "DATABASE_FAIL" in capsys.readouterr().out


def test_cli_inspection_uses_existing_settings_and_bounded_engine(monkeypatch, capsys):
    import ak_trading.persistence.database as database

    cli = _load_cli()
    engine = FakeEngine()
    loaded = []
    engine_settings = []

    def load_settings(**kwargs):
        loaded.append(kwargs)
        return SETTINGS

    def create_engine(settings):
        engine_settings.append(settings)
        return engine

    monkeypatch.setattr(cli, "load_database_settings", load_settings)
    monkeypatch.setattr(cli, "create_database_engine", create_engine)
    monkeypatch.setattr(database, "create_database_engine", create_engine)

    assert cli.main(["inspect"]) == 0
    prefix, output = capsys.readouterr().out.split(" ", 1)
    assert prefix == "DATABASE_INSPECTION"
    assert json.loads(output)["read_only"] is True
    assert loaded == [{"required": True}]
    assert len(engine_settings) == 1
    assert engine_settings[0].connect_timeout == 5
    assert engine_settings[0].statement_timeout_ms == 5000
    assert engine.disposed


def test_symbol_coverage_is_bounded_parameterized_and_marks_truncation():
    from ak_trading.persistence.inspection import InspectionRequest, inspect_database

    symbol = "SHFE.rb'; SELECT pg_sleep(10); --"
    engine = FakeEngine()
    engine.connection.market_rows = [
        {"datetime_nano": 1_783_000_000_000_000_000, "trading_day": date(2026, 7, 2)},
        {"datetime_nano": 1_783_000_060_000_000_000, "trading_day": None},
        {"datetime_nano": 1_783_000_120_000_000_000, "trading_day": date(2026, 7, 3)},
    ]
    request = InspectionRequest(
        symbol=symbol, duration_seconds=60, limit=2,
        start="2026-07-01T08:00:00+08:00", end="2026-07-04T00:00:00Z",
    )

    coverage = inspect_database(engine, settings=SETTINGS, request=request)["market_coverage"]

    assert coverage["status"] == "available"
    assert coverage["scope"] == "earliest_matching_rows"
    assert coverage["rows"] == 2
    assert coverage["limit"] == 2
    assert coverage["truncated"] is True
    assert coverage["complete_for_filter"] is False
    assert coverage["first_datetime_nano"] == 1_783_000_000_000_000_000
    assert coverage["last_datetime_nano"] == 1_783_000_060_000_000_000
    assert coverage["null_trading_days"] == 1
    assert coverage["first_trading_day"] == "2026-07-02"
    assert coverage["last_trading_day"] == "2026-07-02"
    market_queries = [(sql, params) for sql, params in engine.connection.transcript
                      if "FROM public.market_bars" in sql]
    assert len(market_queries) == 1
    sql, params = market_queries[0]
    assert symbol not in sql
    assert symbol in params.values()
    assert request.start_nano == 1_782_864_000_000_000_000
    assert request.end_nano == 1_783_123_200_000_000_000
    assert request.start_nano in params.values()
    assert request.end_nano in params.values()
    assert "market_bars.datetime_nano >= " in sql
    assert "market_bars.datetime_nano < " in sql
    assert 3 in params.values()  # Two reported rows plus one truncation sentinel.
    assert 60 in params.values()


def test_empty_coverage_is_exact_for_the_filter_and_defaults_to_1000_rows():
    from ak_trading.persistence.inspection import InspectionRequest, inspect_database

    engine = FakeEngine()
    coverage = inspect_database(engine, settings=SETTINGS, request=InspectionRequest(
        symbol="KQ.m@SHFE.rb", duration_seconds=60,
    ))["market_coverage"]

    assert coverage["rows"] == 0
    assert coverage["limit"] == 1000
    assert coverage["truncated"] is False
    assert coverage["complete_for_filter"] is True
    assert coverage["first_datetime_nano"] is None
    assert coverage["last_datetime_nano"] is None
    assert coverage["first_trading_day"] is None
    assert coverage["null_trading_days"] == 0
    sql, params = next((sql, params) for sql, params in engine.connection.transcript
                       if "FROM public.market_bars" in sql)
    assert 1001 in params.values()
    assert "datetime_nano >= " not in sql
    assert "datetime_nano < " not in sql


def test_requested_market_coverage_reports_absent_table_without_querying_it():
    from ak_trading.persistence.inspection import InspectionRequest, inspect_database

    engine = FakeEngine()
    engine.connection.tables = [row for row in engine.connection.tables if row[1] != "market_bars"]
    coverage = inspect_database(engine, settings=SETTINGS, request=InspectionRequest(
        symbol="KQ.m@SHFE.rb", duration_seconds=60,
    ))["market_coverage"]

    assert coverage == {"status": "unavailable", "reason": "table_absent"}
    assert not any("FROM public.market_bars" in sql for sql, _ in engine.connection.transcript)


@pytest.mark.parametrize("stage", ["settings", "engine", "connect"])
def test_cli_uses_fixed_safe_failures_even_without_settings(stage, monkeypatch, capsys):
    import ak_trading.persistence.database as database

    cli = _load_cli()
    engine = FakeEngine()

    def fail(*args, **kwargs):
        raise RuntimeError("postgresql://unknown_user:unknown_password@db.invalid/private secret-token")

    monkeypatch.setattr(cli, "load_database_settings", fail if stage == "settings" else lambda **kwargs: SETTINGS)
    factory = fail if stage == "engine" else lambda settings: engine
    monkeypatch.setattr(cli, "create_database_engine", factory)
    monkeypatch.setattr(database, "create_database_engine", factory)
    if stage == "connect":
        engine.connect = fail

    assert cli.main(["inspect"]) == 2
    output = capsys.readouterr().out
    assert output == "DATABASE_FAIL DatabaseOperationError database inspection failed\n"
    assert engine.disposed == (stage == "connect")


@pytest.mark.parametrize("connect_timeout, statement_timeout, expected_connect, expected_statement", [
    (900, 999_999, 5, 5000), (2, 200, 2, 200),
])
def test_inspect_caps_timeouts_and_preserves_stricter_settings(
    connect_timeout, statement_timeout, expected_connect, expected_statement, monkeypatch, capsys,
):
    import ak_trading.persistence.database as database

    cli = _load_cli()
    settings = DatabaseSettings(SETTINGS.url, connect_timeout=connect_timeout,
                                statement_timeout_ms=statement_timeout)
    engine = FakeEngine()
    received = []

    def factory(bounded):
        received.append(bounded)
        return engine

    monkeypatch.setattr(cli, "load_database_settings", lambda **kwargs: settings)
    monkeypatch.setattr(cli, "create_database_engine", factory)
    monkeypatch.setattr(database, "create_database_engine", factory)

    assert cli.main(["inspect"]) == 0
    assert received[0].connect_timeout == expected_connect
    assert received[0].statement_timeout_ms == expected_statement
    assert settings.connect_timeout == connect_timeout
    assert settings.statement_timeout_ms == statement_timeout
    timeout_values = [value for sql, params in engine.connection.transcript
                      if "set_config(" in sql for value in params.values()]
    assert f"{expected_statement}ms" in timeout_values
    capsys.readouterr()


def test_database_size_is_optional_and_permission_failure_is_sanitized():
    from ak_trading.persistence.inspection import inspect_database

    engine = FakeEngine()
    report = inspect_database(engine, settings=SETTINGS)
    assert report["database_size"] == {"status": "available", "bytes": 193_273_528_320}

    engine = FakeEngine()
    engine.connection.size_error = FakeDatabaseError("42501")
    report = inspect_database(engine, settings=SETTINGS)
    assert report["database_size"] == {"status": "unavailable", "reason": "permission_denied"}
    assert report["reference_counts"]["reference_products"]["rows"] == 80
    assert "leaked" not in json.dumps(report)


def test_visible_public_tables_are_found_after_an_empty_search_path_schema():
    from ak_trading.persistence.inspection import InspectionRequest, inspect_database

    engine = FakeEngine()
    engine.connection.current_schema = "empty_user_schema"
    engine.connection.tables.append(("empty_user_schema", None, None))

    report = inspect_database(engine, settings=SETTINGS, request=InspectionRequest(
        symbol="KQ.m@SHFE.rb", duration_seconds=60,
    ))

    assert report["expected_schema"]["resolved_tables"]["market_bars"] == "public"
    assert "market_bars" not in report["expected_schema"]["missing_tables"]
    assert report["reference_counts"]["reference_products"]["rows"] == 80
    assert report["calendar_coverage"]["snapshot_id"] == 2
    assert report["migration"]["revisions"] == ["20260831_0003"]
    assert report["market_coverage"]["rows"] == 0
    assert "empty_user_schema" in report["schemas"]
    assert any("pg_catalog.pg_table_is_visible(c.oid)" in sql
               for sql, _ in engine.connection.transcript)


def test_hidden_same_name_table_does_not_satisfy_expected_schema():
    from ak_trading.persistence.inspection import inspect_database

    engine = FakeEngine()
    engine.connection.tables.append(("research", "exchange_rule_current", 3.0))

    report = inspect_database(engine, settings=SETTINGS)

    assert "exchange_rule_current" in report["expected_schema"]["missing_tables"]
    assert "exchange_rule_current" not in report["expected_schema"]["resolved_tables"]
    assert any(row["schema"] == "research" and row["name"] == "exchange_rule_current"
               for row in report["tables"])
