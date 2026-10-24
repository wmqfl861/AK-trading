"""Offline CLI/export boundary regressions; credentials and I/O are synthetic."""
from __future__ import annotations

import importlib.util
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")

from ak_trading.futures.data import datetime_to_nano
from ak_trading.persistence import database
from ak_trading.persistence.futures_research import ExportRequest
from ak_trading.futures.spec import ProductKey

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "export_futures_input.py"
PROCESS_URL = "postgresql://process_user:process_secret@invalid.example/process_db"
FILE_URL = "postgresql://file_user:file_secret@invalid.example/file_db"


def load_cli():
    spec = importlib.util.spec_from_file_location("export_connections_cli", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_request(tmp_path: Path) -> ExportRequest:
    inputs = tmp_path / "inputs"
    inputs.mkdir(exist_ok=True)
    cache = inputs / "universe.json"
    cache.write_text(json.dumps({
        "generated_at": "2026-09-01T00:00:00+00:00",
        "products": {"SHFE.rb": [
            {"symbol": "SHFE.rb2609", "expire_datetime": 1789455600, "expired": False},
            {"symbol": "SHFE.rb2610", "expire_datetime": 1790751600, "expired": False},
        ]},
    }), encoding="utf-8")
    catalog = inputs / "catalog.json"
    catalog.write_text(json.dumps({"schema_version": 1, "products": [
        {"exchange": "SHFE", "code": "rb", "instrument_kind": "future", "status": "listed"},
    ]}), encoding="utf-8")
    return ExportRequest(
        products=(ProductKey("SHFE", "RB"),),
        span_start=datetime(2026, 7, 1, tzinfo=timezone.utc),
        span_end=datetime(2026, 8, 1, tzinfo=timezone.utc),
        as_of=datetime(2026, 8, 1, tzinfo=timezone.utc),
        universe_cache_path=cache, catalog_path=catalog,
    )


def cli_args(request: ExportRequest, output: Path) -> list[str]:
    return [
        "--products", "SHFE.rb", "--start", request.span_start.isoformat(),
        "--end", request.span_end.isoformat(),
        "--universe-cache", str(request.universe_cache_path),
        "--catalog", str(request.catalog_path), "--output", str(output),
    ]


class FakeResult:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value

    def mappings(self):
        return self

    def all(self):
        return self.value


class FakeConnection:
    dialect = type("Dialect", (), {"driver": "offline_fixture"})()

    def __init__(self, failure=None):
        self.failure = failure
        self.closed = False

    def begin(self):
        return self

    def rollback(self):
        pass

    def execute(self, statement, params=None):
        sql = str(statement)
        if sql.startswith("SET TRANSACTION"):
            return FakeResult(None)
        if sql == "SHOW transaction_isolation":
            return FakeResult("repeatable read")
        if sql == "SHOW transaction_read_only":
            return FakeResult("on")
        if self.failure == "query":
            raise RuntimeError(FILE_URL)
        ts = datetime(2026, 7, 2, 7, tzinfo=timezone.utc)
        return FakeResult([{
            "symbol": "SHFE.rb2609", "duration_seconds": 86400,
            "datetime_nano": datetime_to_nano(ts), "ts": ts,
            "trading_day": ts.date(), "trading_day_source": "offline_fixture",
            "open": "3000", "high": "3010", "low": "2990", "close": "3005",
            "volume": "1000", "open_oi": None, "close_oi": None,
            "source": "offline_fixture", "batch_id": "fixture", "imported_at": ts,
        }])

    def commit(self):
        pass

    def close(self):
        self.closed = True


class FakeEngine:
    def __init__(self, failure=None):
        self.failure = failure
        self.connection = FakeConnection(failure)
        self.connect_count = 0
        self.disposed = False

    def connect(self):
        self.connect_count += 1
        if self.failure == "connect":
            raise RuntimeError(FILE_URL)
        return self.connection

    def dispose(self):
        self.disposed = True
        if self.failure == "dispose":
            raise RuntimeError(FILE_URL)


@pytest.fixture(autouse=True)
def isolate_connections(monkeypatch):
    import dotenv
    import socket

    monkeypatch.delenv("AK_DATABASE_URL", raising=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("real network, environment loading or engine creation forbidden")

    monkeypatch.setattr(database, "create_database_engine", forbidden)
    monkeypatch.setattr(dotenv, "load_dotenv", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def test_env_file_is_opt_in_and_only_database_url_is_used(tmp_path, monkeypatch, capsys):
    request = make_request(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"AK_DATABASE_URL={FILE_URL}\nUNRELATED_SECRET=do_not_export_me\n"
        "AK_DATABASE_CONNECT_TIMEOUT=999\n", encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    settings_seen = []
    engine = FakeEngine()

    def create_engine(settings):
        settings_seen.append(settings)
        return engine

    monkeypatch.setattr(database, "create_database_engine", create_engine)
    before = dict(os.environ)
    args = cli_args(request, tmp_path / "out")
    assert load_cli().main(args) == 3
    assert not settings_seen
    assert load_cli().main(args + ["--env-file", str(env_file)]) == 0
    assert settings_seen[0].url == FILE_URL
    assert settings_seen[0].connect_timeout == 5
    assert os.environ == before
    assert engine.connect_count == 1 and engine.disposed
    captured = capsys.readouterr()
    output = captured.out + captured.err
    output += "".join(path.read_text(encoding="utf-8") for path in (tmp_path / "out").iterdir())
    for secret in (FILE_URL, "file_user", "file_secret", "do_not_export_me"):
        assert secret not in output


@pytest.mark.parametrize("value,expected", [(PROCESS_URL, 0), ("", 3)])
def test_process_url_takes_precedence_including_empty(tmp_path, monkeypatch, value, expected):
    request = make_request(tmp_path)
    env_file = tmp_path / "explicit.env"
    env_file.write_text(f"AK_DATABASE_URL={FILE_URL}\n", encoding="utf-8")
    monkeypatch.setenv("AK_DATABASE_URL", value)
    seen = []

    def create_engine(settings):
        seen.append(settings.url)
        return FakeEngine()

    monkeypatch.setattr(database, "create_database_engine", create_engine)
    args = cli_args(request, tmp_path / "out") + ["--env-file", str(env_file)]
    assert load_cli().main(args) == expected
    assert seen == ([PROCESS_URL] if value else [])


@pytest.mark.parametrize("fault", ["products", "duration", "cache", "catalog", "output", "metadata"])
def test_local_validation_precedes_engine_creation(tmp_path, monkeypatch, capsys, fault):
    request = make_request(tmp_path)
    monkeypatch.setenv("AK_DATABASE_URL", PROCESS_URL)
    args = cli_args(request, tmp_path / "out")
    if fault == "products":
        args[1] = " , "
    elif fault == "duration":
        args += ["--duration", "0"]
    elif fault == "cache":
        request.universe_cache_path.write_text("{", encoding="utf-8")
    elif fault == "catalog":
        request.catalog_path.unlink()
    elif fault == "output":
        args[-1] = str(request.catalog_path.parent / "nested")
    else:
        args += ["--local-metadata", str(tmp_path / "nonexistent.json")]
    engines = []

    def create_engine(settings):
        engines.append(settings)
        return FakeEngine()

    monkeypatch.setattr(database, "create_database_engine", create_engine)
    assert load_cli().main(args) in (2, 3)
    assert not engines
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.parametrize("failure", ["settings", "engine", "connect", "query", "dispose"])
def test_connection_errors_are_credential_safe(tmp_path, monkeypatch, capsys, failure):
    request = make_request(tmp_path)
    monkeypatch.setenv("AK_DATABASE_URL", FILE_URL if failure != "settings" else "bad:" + FILE_URL)
    engine = FakeEngine(failure)

    def create_engine(settings):
        if failure == "engine":
            raise RuntimeError(FILE_URL)
        return engine

    monkeypatch.setattr(database, "create_database_engine", create_engine)
    assert load_cli().main(cli_args(request, tmp_path / "out")) == 3
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["status"] == "export_blocked"
    for secret in (FILE_URL, "file_user", "file_secret", "Traceback"):
        assert secret not in captured.out + captured.err
    if failure in ("connect", "query", "dispose"):
        assert engine.disposed
    if failure == "query":
        assert engine.connection.closed


def test_env_file_does_not_interpolate_unrelated_credentials(tmp_path, monkeypatch, capsys):
    request = make_request(tmp_path)
    env_file = tmp_path / "chosen.env"
    env_file.write_text(
        "UNRELATED=hidden_secret\nAK_DATABASE_URL=postgresql://user:${UNRELATED}@invalid.example/db\n",
        encoding="utf-8",
    )
    seen = []

    def create_engine(settings):
        seen.append(settings.url)
        return FakeEngine()

    monkeypatch.setattr(database, "create_database_engine", create_engine)
    assert load_cli().main(cli_args(request, tmp_path / "out") + ["--env-file", str(env_file)]) == 0
    assert seen == ["postgresql://user:${UNRELATED}@invalid.example/db"]
    assert "hidden_secret" not in capsys.readouterr().out


def test_explicit_missing_env_file_fails_without_fallback(tmp_path, monkeypatch, capsys):
    request = make_request(tmp_path)
    monkeypatch.setenv("AK_DATABASE_URL", PROCESS_URL)
    args = cli_args(request, tmp_path / "out") + ["--env-file", str(tmp_path / "missing.env")]
    assert load_cli().main(args) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "env_file_unreadable"


@pytest.mark.parametrize("failure", ["connect", "query"])
def test_exporter_database_boundary_returns_safe_failure(tmp_path, failure):
    from ak_trading.persistence.futures_research import export_futures_bundle

    request = make_request(tmp_path)
    engine = FakeEngine(failure)
    outcome = export_futures_bundle(engine, request, tmp_path / "out")
    assert outcome.status == "export_blocked"
    assert "file_secret" not in repr(outcome) and FILE_URL not in repr(outcome)
    assert not (tmp_path / "out").exists()
    if failure == "query":
        assert engine.connection.closed
