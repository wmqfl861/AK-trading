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
    "postgresql://freshness_user:freshness_password@db.invalid/ak_freshness"
)
OBSERVED_AT = datetime(2026, 9, 8, 0, 0, tzinfo=timezone.utc)


class FakeResult:
    def __init__(self, row=None):
        self.row = row

    def mappings(self):
        return self

    def first(self):
        return self.row


class FakeConnection:
    """DB boundary with PostgreSQL's aborted-transaction/savepoint behavior."""

    def __init__(self, bars=None, ranks=None):
        self.bars = bars or {}
        self.ranks = ranks or {}
        self.transcript = []
        self.read_only = False
        self.aborted = False
        self.savepoints = 0
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True
        self.transcript.append(("ROLLBACK", {}))

    @contextmanager
    def begin_nested(self):
        self.transcript.append(("SAVEPOINT", {}))
        self.savepoints += 1
        try:
            yield self
        except Exception:
            self.aborted = False
            self.transcript.append(("ROLLBACK TO SAVEPOINT", {}))
            raise
        else:
            self.transcript.append(("RELEASE SAVEPOINT", {}))
        finally:
            self.savepoints -= 1

    def execute(self, statement, parameters=None):
        compiled = statement.compile(dialect=postgresql.dialect())
        sql = " ".join(str(compiled).split())
        params = {**compiled.params, **(parameters or {})}
        self.transcript.append((sql, params))
        assert not self.aborted, "previous query failure was not recovered"
        if sql == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY":
            self.read_only = True
            return FakeResult()
        assert self.read_only, "query ran before repeatable-read READ ONLY"
        if "set_config(" in sql:
            assert "statement_timeout" in sql
            return FakeResult()
        assert self.savepoints == 1, "probe lacks a recovery savepoint"
        assert "LIMIT" in sql and 1 in params.values()
        assert not any(word in sql.lower() for word in ("count(", "max(", "group by"))
        if "FROM market_bars" in sql:
            assert "market_bars.symbol = " in sql
            assert "market_bars.duration_seconds = " in sql
            assert "ORDER BY market_bars.ts DESC" in sql
            row = self.bars.get((params["symbol_1"], params["duration_seconds_1"]))
        elif "FROM member_position_ranks" in sql:
            assert "member_position_ranks.exchange = " in sql
            assert "ORDER BY member_position_ranks.trading_day DESC" in sql
            row = self.ranks.get(params["exchange_1"])
        else:
            raise AssertionError(f"unexpected query: {sql}")
        if isinstance(row, Exception):
            self.aborted = True
            raise row
        return FakeResult(row)


class FakeEngine:
    def __init__(self, connection=None):
        self.connection = connection or FakeConnection()
        self.disposed = False

    def connect(self):
        return self.connection

    def dispose(self):
        self.disposed = True


def bar(timestamp, trading_day=None, source="reference_calendar_date"):
    return {
        "ts": datetime.fromisoformat(timestamp),
        "trading_day": date.fromisoformat(trading_day) if trading_day else None,
        "trading_day_source": source,
    }


def inspect(engine, *, symbols=("SHFE.rb2610",), duration=86400, exchanges=(),
            expected="2026-09-07", observed_at=OBSERVED_AT):
    from ak_trading.persistence.freshness import FreshnessRequest, inspect_data_freshness

    return inspect_data_freshness(
        engine, settings=SETTINGS, observed_at=observed_at,
        request=FreshnessRequest(
            expected_through=expected, symbols=symbols,
            duration_seconds=duration, exchanges=exchanges,
        ),
    )


def test_fresh_sample_uses_shanghai_daily_date_and_indexed_read_only_probes():
    engine = FakeEngine(FakeConnection(
        bars={("SHFE.rb2610", 86400): bar("2026-09-06T16:00:00+00:00", "2026-09-08")},
        ranks={"SHFE": {"trading_day": date(2026, 9, 7)}},
    ))

    report = inspect(engine, exchanges=("SHFE",))

    assert report["overall_status"] == "fresh"
    assert report["passed"] is True
    assert report["exit_code"] == 0
    assert report["expected_through"] == "2026-09-07"
    assert report["observed_at"] == "2026-09-08T00:00:00+00:00"
    assert report["coverage"]["scope"] == "requested_samples_only"
    assert report["coverage"]["historical_completeness_certified"] is False
    assert report["coverage"]["symbols"] == ["SHFE.rb2610"]
    assert report["coverage"]["exchanges"] == ["SHFE"]
    daily, rank = report["checks"]
    assert daily["latest_raw_date"] == "2026-09-07"
    assert daily["latest_trading_day"] == "2026-09-08"
    assert daily["comparison_date"] == "2026-09-07"
    assert daily["basis"] == "daily_raw_date_asia_shanghai"
    assert rank["latest_trading_day"] == "2026-09-07"
    assert rank["basis"] == "stored_ranking_trading_day"
    assert engine.connection.closed
    assert not engine.disposed  # The caller owns the supplied engine.


def load_cli():
    path = Path(__file__).resolve().parents[1] / "scripts" / "check_data_freshness.py"
    spec = importlib.util.spec_from_file_location("freshness_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ARGS = ["--expected-through", "2026-09-07", "--symbol", "SHFE.rb2610", "--duration", "86400"]


def install_cli_boundary(cli, monkeypatch, engine, settings=SETTINGS):
    loaded, created = [], []

    def load_settings(**kwargs):
        loaded.append(kwargs)
        return settings

    def create_engine(bounded):
        created.append(bounded)
        return engine

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is timezone.utc
            return OBSERVED_AT

    monkeypatch.setattr(cli, "load_database_settings", load_settings)
    monkeypatch.setattr(cli, "create_database_engine", create_engine)
    monkeypatch.setattr(cli, "datetime", FrozenDatetime)
    return loaded, created


def test_cli_caps_timeouts_disposes_engine_and_publishes_new_json_atomically(
    tmp_path, monkeypatch, capsys,
):
    cli = load_cli()
    engine = FakeEngine(FakeConnection(bars={
        ("SHFE.rb2610", 86400): bar("2026-09-07T00:00:00+08:00"),
    }))
    settings = DatabaseSettings(SETTINGS.url, connect_timeout=90, statement_timeout_ms=90000)
    loaded, created = install_cli_boundary(cli, monkeypatch, engine, settings)
    output = tmp_path / "freshness.json"
    import os
    link = os.link
    published = []

    def checked_link(source, target):
        assert not Path(target).exists()
        assert Path(source).parent == Path(target).parent
        assert json.loads(Path(source).read_text(encoding="utf-8"))["passed"] is True
        published.append(Path(target))
        return link(source, target)

    monkeypatch.setattr(cli.os, "link", checked_link)

    assert cli.main([*ARGS, "--output", str(output)]) == 0

    report = json.loads(capsys.readouterr().out)
    assert json.loads(output.read_text(encoding="utf-8")) == report
    assert report["observed_at"] == "2026-09-08T00:00:00+00:00"
    assert report["passed"] is True
    assert loaded == [{"required": True}]
    assert len(created) == 1
    assert created[0].connect_timeout == 5
    assert created[0].statement_timeout_ms == 5000
    assert settings.statement_timeout_ms == 90000
    assert engine.disposed and engine.connection.closed
    assert published == [output]
    assert list(tmp_path.iterdir()) == [output]
    assert "freshness_user" not in json.dumps(report)
    assert "freshness_password" not in json.dumps(report)
    assert "postgresql" not in json.dumps(report)


@pytest.mark.parametrize("timestamp, trading_day, expected, status, basis, comparison", [
    ("2026-09-06T15:59:59+00:00", None, "2026-09-07", "stale",
     "raw_date_asia_shanghai_fallback", "2026-09-06"),
    ("2026-09-06T16:00:00+00:00", None, "2026-09-07", "fresh",
     "raw_date_asia_shanghai_fallback", "2026-09-07"),
    ("2026-09-04T21:00:00+08:00", "2026-09-07", "2026-09-07", "fresh",
     "stored_trading_day", "2026-09-07"),
    ("2026-09-07T00:30:00+08:00", "2026-09-04", "2026-09-07", "stale",
     "stored_trading_day", "2026-09-04"),
    ("2026-09-04T21:00:00+08:00", None, "2026-09-07", "stale",
     "raw_date_asia_shanghai_fallback", "2026-09-04"),
    ("2026-09-08T00:00:00+08:00", "2026-09-08", "2026-09-07", "fresh",
     "stored_trading_day", "2026-09-08"),
    ("2099-12-31T00:00:00+08:00", "2099-12-31", "2099-12-31", "fresh",
     "stored_trading_day", "2099-12-31"),
])
def test_intraday_compares_explicit_reference_with_separate_unverified_dates(
    timestamp, trading_day, expected, status, basis, comparison,
):
    engine = FakeEngine(FakeConnection(bars={
        ("SHFE.rb2610", 60): bar(timestamp, trading_day, "reference_calendar_night"),
    }))

    report = inspect(engine, duration=60, expected=expected)

    check = report["checks"][0]
    assert check["status"] == status
    assert check["basis"] == basis
    assert check["comparison_date"] == comparison
    assert check["latest_trading_day"] == trading_day
    assert check["trading_day_source"] == "reference_calendar_night"
    assert check["trading_day_source_verified"] is False
    assert report["passed"] == (status == "fresh")
    assert report["observed_at"] == "2026-09-08T00:00:00+00:00"


def test_daily_never_promotes_older_raw_date_using_newer_stored_trading_day():
    engine = FakeEngine(FakeConnection(bars={
        ("SHFE.rb2610", 86400): bar("2026-09-04T21:00:00+08:00", "2026-09-07"),
    }))

    report = inspect(engine)

    assert report["overall_status"] == "stale"
    assert report["exit_code"] == 1
    assert report["checks"][0]["latest_raw_date"] == "2026-09-04"
    assert report["checks"][0]["latest_trading_day"] == "2026-09-07"


class FakeDatabaseError(Exception):
    def __init__(self, sqlstate):
        super().__init__("postgresql://unknown_user:unknown_password@private.invalid/private secret-token")
        self.sqlstate = sqlstate


@pytest.mark.parametrize("sqlstate", ["57014", "42501", "42P01", "42703", "XX000"])
def test_query_failure_isolated_by_savepoint_does_not_hide_later_samples(sqlstate):
    engine = FakeEngine(FakeConnection(
        bars={
            ("SHFE.rb2610", 86400): FakeDatabaseError(sqlstate),
            ("KQ.m@SHFE.rb", 86400): bar("2026-09-07T00:00:00+08:00"),
        },
        ranks={"SHFE": {"trading_day": date(2026, 9, 7)}},
    ))

    report = inspect(engine, symbols=("SHFE.rb2610", "KQ.m@SHFE.rb"), exchanges=("SHFE", "DCE"))

    assert [check["status"] for check in report["checks"]] == ["error", "fresh", "fresh", "empty"]
    assert report["checks"][0]["error_type"] == "FakeDatabaseError"
    assert report["overall_status"] == "error"
    assert report["exit_code"] == 2
    assert report["passed"] is False
    assert ("ROLLBACK TO SAVEPOINT", {}) in engine.connection.transcript
    assert not engine.connection.aborted
    assert engine.connection.closed
    assert "unknown_" not in json.dumps(report)
    assert "secret-token" not in json.dumps(report)
    assert "postgresql" not in json.dumps(report)


def test_ranking_failure_recovers_for_the_next_exchange():
    engine = FakeEngine(FakeConnection(ranks={
        "SHFE": FakeDatabaseError("42501"), "DCE": {"trading_day": date(2026, 9, 4)},
    }))

    report = inspect(engine, exchanges=("SHFE", "DCE", "CZCE"))

    assert [check["status"] for check in report["checks"]] == ["empty", "error", "stale", "empty"]
    assert report["exit_code"] == 2


@pytest.mark.parametrize("ranks, overall", [({}, "empty"), ({"SHFE": {"trading_day": date(2026, 9, 4)}}, "stale")])
def test_stale_and_empty_samples_fail_the_prerequisite(ranks, overall):
    report = inspect(FakeEngine(FakeConnection(ranks=ranks)), exchanges=("SHFE",))

    assert report["overall_status"] == overall
    assert report["exit_code"] == 1
    assert report["passed"] is False
    assert report["checks"][0]["latest_raw_date"] is None
    assert report["checks"][0]["latest_trading_day"] is None


def test_coverage_queries_only_normalized_requested_samples_and_binds_symbols():
    symbol = "SHFE.rb'; SELECT pg_sleep(10); --"
    engine = FakeEngine()

    report = inspect(engine, symbols=(symbol, " KQ.m@SHFE.rb ", symbol), exchanges=("shfe", "SHFE"))

    assert report["coverage"]["symbols"] == [symbol, "KQ.m@SHFE.rb"]
    assert report["coverage"]["exchanges"] == ["SHFE"]
    selects = [(sql, params) for sql, params in engine.connection.transcript if "FROM " in sql]
    assert len(selects) == 3
    assert all(symbol not in sql for sql, _ in selects)
    assert symbol in selects[0][1].values()


@pytest.mark.parametrize("row", [
    {"ts": datetime(2026, 9, 7), "trading_day": None, "trading_day_source": "test"},
    {"ts": None, "trading_day": None, "trading_day_source": "test"},
    {"ts": datetime(2026, 9, 7, tzinfo=timezone.utc), "trading_day": "2026-09-07", "trading_day_source": "test"},
])
def test_malformed_stored_dates_fail_closed_without_hiding_other_samples(row):
    engine = FakeEngine(FakeConnection(
        bars={("SHFE.rb2610", 86400): row},
        ranks={"SHFE": {"trading_day": date(2026, 9, 7)}},
    ))

    report = inspect(engine, exchanges=("SHFE",))

    assert [check["status"] for check in report["checks"]] == ["error", "fresh"]
    assert report["exit_code"] == 2


def test_provenance_does_not_echo_free_form_payloads_or_configured_secrets():
    engine = FakeEngine(FakeConnection(bars={
        ("SHFE.rb2610", 86400): bar("2026-09-07T00:00:00+08:00", source=SETTINGS.url),
        ("KQ.m@SHFE.rb", 86400): bar("2026-09-07T00:00:00+08:00", source="freshness_password"),
    }))

    report = inspect(engine, symbols=("SHFE.rb2610", "KQ.m@SHFE.rb"))

    assert report["checks"][0]["trading_day_source"] == "unrecognized"
    assert report["checks"][1]["trading_day_source"] == "[REDACTED]"
    assert SETTINGS.url not in json.dumps(report)
    assert "freshness_password" not in json.dumps(report)


@pytest.mark.parametrize("field, value", [
    ("expected_through", ""), ("expected_through", "20260907"),
    ("expected_through", "2026-9-07"), ("expected_through", "2026-02-29"),
    ("expected_through", "2026-09-07T00:00:00Z"), ("expected_through", date(2026, 9, 7)),
    ("duration_seconds", 0), ("duration_seconds", -1),
    ("duration_seconds", True), ("duration_seconds", 60.0),
    ("duration_seconds", 2_147_483_648),
    ("symbols", ()), ("symbols", "SHFE.rb2610"),
    ("symbols", (" ",)), ("symbols", ("RB\x00",)),
    ("symbols", ("r" * 257,)), ("symbols", tuple(f"SHFE.rb{i}" for i in range(101))),
    ("exchanges", "SHFE"), ("exchanges", (" ",)), ("exchanges", ("SHFE; SELECT",)),
])
def test_request_rejects_invalid_or_unbounded_samples_before_connecting(field, value):
    from ak_trading.persistence.freshness import FreshnessRequest

    values = dict(expected_through="2026-09-07", symbols=("SHFE.rb2610",), duration_seconds=60)
    values[field] = value
    with pytest.raises(ValueError):
        FreshnessRequest(**values)


def test_naive_observation_clock_rejected_and_aware_clock_normalized_without_affecting_status():
    engine = FakeEngine()
    with pytest.raises(ValueError):
        inspect(engine, observed_at=datetime(2026, 9, 8))
    assert engine.connection.transcript == []

    report = inspect(engine, observed_at=datetime.fromisoformat("2026-09-08T08:00:00+08:00"))
    assert report["observed_at"] == "2026-09-08T00:00:00+00:00"
    assert report["overall_status"] == "empty"


@pytest.mark.parametrize("arguments", [
    [], ["--expected-through", "2026-09-07", "--duration", "60"],
    ["--expected-through", "2026-09-07", "--symbol", "SHFE.rb2610"],
    ["--symbol", "SHFE.rb2610", "--duration", "60"],
    [*ARGS, "--expected-through", "2026-02-29"],
    [*ARGS, "--duration", "0"], [*ARGS, "--duration", "2147483648"],
    [*ARGS, "--duration", "postgresql://secret:secret@hidden/db"],
    [*ARGS, "--symbol", " "], [*ARGS, "--exchange", ""],
    [*ARGS, "--secret-token", "unknown-password"],
])
def test_invalid_cli_arguments_never_load_environment_or_database(arguments, monkeypatch, capsys):
    cli = load_cli()

    def forbidden(*args, **kwargs):
        pytest.fail("invalid input attempted settings/database access")

    monkeypatch.setattr(cli, "load_database_settings", forbidden)
    monkeypatch.setattr(cli, "create_database_engine", forbidden)

    assert cli.main(arguments) == 2
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["overall_status"] == "error"
    assert report["passed"] is False
    assert report["errors"][0]["stage"] == "arguments"
    assert "secret" not in captured.out + captured.err
    assert "unknown-password" not in captured.out + captured.err


@pytest.mark.parametrize("stage", ["settings", "engine", "connect", "setup", "dispose"])
def test_cli_all_failure_stages_hide_exception_text_and_dispose_when_created(stage, monkeypatch, capsys):
    cli = load_cli()
    engine = FakeEngine(FakeConnection(bars={
        ("SHFE.rb2610", 86400): bar("2026-09-07T00:00:00+08:00"),
    }))
    install_cli_boundary(cli, monkeypatch, engine)

    def fail(*args, **kwargs):
        raise RuntimeError("postgresql://unknown_user:unknown_password@hidden.invalid/private secret-token")

    if stage == "settings":
        monkeypatch.setattr(cli, "load_database_settings", fail)
    elif stage == "engine":
        monkeypatch.setattr(cli, "create_database_engine", fail)
    elif stage == "connect":
        monkeypatch.setattr(engine, "connect", fail)
    elif stage == "setup":
        monkeypatch.setattr(engine.connection, "execute", fail)
    else:
        def failing_dispose():
            engine.disposed = True
            fail()
        monkeypatch.setattr(engine, "dispose", failing_dispose)

    assert cli.main(ARGS) == 2
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["overall_status"] == "error"
    assert report["passed"] is False
    assert report["errors"][0]["error_type"] == "RuntimeError"
    if stage != "dispose":
        assert all(check["status"] == "error" for check in report["checks"])
    assert engine.disposed == (stage not in ("settings", "engine"))
    assert "unknown_" not in captured.out + captured.err
    assert "postgresql" not in captured.out + captured.err
    assert "secret-token" not in captured.out + captured.err


@pytest.mark.parametrize("connect_timeout, statement_timeout", [(1, 200), (5, 5000)])
def test_cli_preserves_stricter_timeouts_and_forwards_explicit_env_file(
    connect_timeout, statement_timeout, tmp_path, monkeypatch, capsys,
):
    cli = load_cli()
    engine = FakeEngine()
    settings = DatabaseSettings(SETTINGS.url, connect_timeout=connect_timeout,
                                statement_timeout_ms=statement_timeout)
    loaded, created = install_cli_boundary(cli, monkeypatch, engine, settings)
    env_file = tmp_path / "unit.env"
    env_file.write_text("# offline fixture, never actually loaded", encoding="utf-8")

    assert cli.main([*ARGS, "--env-file", str(env_file)]) == 1

    report = json.loads(capsys.readouterr().out)
    assert report["overall_status"] == "empty"
    assert loaded == [{"required": True, "env_file": env_file}]
    assert created[0].connect_timeout == connect_timeout
    assert created[0].statement_timeout_ms == statement_timeout
    timeouts = [params for sql, params in engine.connection.transcript if "set_config(" in sql]
    assert timeouts == [{"timeout": f"{statement_timeout}ms"}]


@pytest.mark.parametrize("name", [
    "NUL", "nul.json", "NUL .json", "COM1.json", "lpt9.JSON", "CONOUT$.json", "AUX/ok.json",
    "nul/ok.json", "COM¹.json", "fresh.json:stream", "fresh.json.", "fresh.json ",
    "fresh\x00.json", "fresh\n.json", "*.json", "\\\\host\\share\\fresh.json",
    "\\\\?\\C:\\fresh.json", "fresh.txt", "missing/fresh.json",
])
def test_invalid_output_paths_fail_before_database_access(name, tmp_path, monkeypatch, capsys):
    cli = load_cli()

    def forbidden(*args, **kwargs):
        pytest.fail("invalid path attempted database access")

    monkeypatch.setattr(cli, "load_database_settings", forbidden)
    monkeypatch.setattr(cli, "create_database_engine", forbidden)
    target = str(tmp_path / name)

    assert cli.main([*ARGS, "--output", target]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["errors"][0]["stage"] == "arguments"
    assert list(tmp_path.iterdir()) == []


def test_existing_output_is_never_overwritten_and_database_not_contacted(tmp_path, monkeypatch, capsys):
    cli = load_cli()
    target = tmp_path / "freshness.json"
    target.write_text("original", encoding="utf-8")

    def forbidden(*args, **kwargs):
        pytest.fail("existing output attempted database access")

    monkeypatch.setattr(cli, "load_database_settings", forbidden)

    assert cli.main([*ARGS, "--output", str(target)]) == 2
    assert target.read_text(encoding="utf-8") == "original"
    assert json.loads(capsys.readouterr().out)["errors"][0]["error_type"] == "FileExistsError"
    assert list(tmp_path.iterdir()) == [target]


def test_output_collision_after_probe_preserves_concurrent_file_and_removes_temporary(
    tmp_path, monkeypatch, capsys,
):
    cli = load_cli()
    engine = FakeEngine()
    install_cli_boundary(cli, monkeypatch, engine)
    target = tmp_path / "freshness.json"
    import os
    link = os.link

    def racing_link(source, destination):
        Path(destination).write_text("concurrent writer", encoding="utf-8")
        return link(source, destination)

    monkeypatch.setattr(cli.os, "link", racing_link)

    assert cli.main([*ARGS, "--output", str(target)]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["errors"] == [{"stage": "output", "error_type": "FileExistsError"}]
    assert report["passed"] is False
    assert target.read_text(encoding="utf-8") == "concurrent writer"
    assert list(tmp_path.iterdir()) == [target]
    assert engine.disposed


@pytest.mark.parametrize("operation", ["fsync", "link"])
def test_output_io_failure_leaves_no_partial_or_temporary_file(operation, tmp_path, monkeypatch, capsys):
    cli = load_cli()
    engine = FakeEngine()
    install_cli_boundary(cli, monkeypatch, engine)
    target = tmp_path / "freshness.json"

    def fail(*args, **kwargs):
        raise OSError("secret-token path and unknown_password")

    monkeypatch.setattr(cli.os, operation, fail)

    assert cli.main([*ARGS, "--output", str(target)]) == 2
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["errors"] == [{"stage": "output", "error_type": "OSError"}]
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []
    assert "secret-token" not in captured.out + captured.err
    assert "unknown_password" not in captured.out + captured.err
    assert engine.disposed
