"""Public CLI tests with fake credential, HTTP and persistence boundaries only."""
from __future__ import annotations

import csv
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import runpy
import sys
import time
from types import ModuleType, SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
CST = timezone(timedelta(hours=8))
FIELDS = ("open", "high", "low", "close", "volume", "open_oi", "close_oi")
HEADER = "datetime," + ",".join(FIELDS) + "\n"
BAR = "1788739200000000000,10,12,9,11,5,7,8\n"  # September 7, 08:00 CST


def contract(symbol, expiry="2026-09-18T15:00:00+08:00", *, expired=False, **extra):
    return {"symbol": symbol, "expire_datetime": int(datetime.fromisoformat(expiry).timestamp()),
            "expired": expired, **extra}


@pytest.fixture
def cli(tmp_path, monkeypatch, capsys):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "update_recent_via_edb.py"
    script.write_bytes((ROOT / "scripts" / script.name).read_bytes())
    downloads = tmp_path / "data" / "downloads"
    downloads.mkdir(parents=True)
    cache = downloads / "backfill_contract_universe.json"
    cache.write_text(json.dumps({"generated_at": "2026-09-01T14:58:47+08:00",
                                "products": {"CFFEX.IF": [contract("CFFEX.IF2609")]}}), encoding="utf-8")
    requests, imports, settings_calls, disposed = [], [], [], []
    boundary = SimpleNamespace(responses={}, allow_io=True, setup_error=None, on_get=None, written=None)

    def check_io(stage):
        if not boundary.allow_io:
            pytest.fail("credentials, HTTP and DB must not be touched: " + stage)
        settings_calls.append(stage)
        if boundary.setup_error == stage:
            raise RuntimeError("secret-user secret-password Bearer secret-token")

    def module(name, **attributes):
        fake = ModuleType(name)
        fake.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, fake)

    def env(path):
        check_io("credentials")
        return {"TQ_USER": "secret-user", "TQ_PASS": "secret-password"}

    def post(url, *, json, timeout):
        check_io("token")
        requests.append({"kind": "token", "timeout": timeout})
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"token": "secret-token"})

    def get(url, *, params, headers, timeout):
        check_io("fetch")
        requests.append({"kind": "bars", "timeout": timeout, **params})
        if boundary.on_get:
            boundary.on_get()
        response = boundary.responses.get(params["symbol"], HEADER + BAR)
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(status_code=200, text=response)

    def database_settings(**kwargs):
        check_io("database_settings")
        return object()

    def engine(settings):
        check_io("engine")
        return SimpleNamespace(dispose=lambda: disposed.append(True))

    def snapshot(*args, **kwargs):
        check_io("snapshot")
        return SimpleNamespace(content_sha256="offline")

    def import_bars(engine, path, *, symbol, **kwargs):
        check_io("import")
        with open(path, newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        imports.append((symbol, rows))
        return SimpleNamespace(rows_written=len(rows) if boundary.written is None else boundary.written)

    def no_database_scan(*args, **kwargs):
        pytest.fail("scope selection must never scan market_bars")

    module("dotenv", dotenv_values=env)
    module("httpx", get=get, post=post)
    module("psycopg", connect=no_database_scan)
    module("ak_trading", __path__=[])
    module("ak_trading.persistence", __path__=[])
    module("ak_trading.market_reference", load_reference_book=lambda path: SimpleNamespace(calendar=object()))
    module("ak_trading.persistence.config", load_database_settings=database_settings)
    module("ak_trading.persistence.database", create_database_engine=engine)
    module("ak_trading.persistence.storage", store_reference_snapshot=snapshot)
    module("ak_trading.persistence.market_data", import_market_bars=import_bars,
           expected_kline_columns=lambda symbol: ("datetime", "datetime_nano", *FIELDS))
    monkeypatch.setattr(sys, "path", sys.path[:])
    monkeypatch.setattr(time, "sleep", lambda seconds: None)

    def run(*arguments):
        monkeypatch.setattr(sys, "argv", [str(script), "--duration", "86400", *map(str, arguments)])
        with pytest.raises(SystemExit) as exited:
            runpy.run_path(str(script), run_name="__main__")
        output = capsys.readouterr()
        return SimpleNamespace(code=exited.value.code, stderr=output.err,
                               events=[json.loads(line) for line in output.out.splitlines()])

    return SimpleNamespace(run=run, root=tmp_path, cache=cache, downloads=downloads,
                           boundary=boundary, requests=requests, imports=imports,
                           settings_calls=settings_calls, disposed=disposed)


@pytest.fixture
def clock(monkeypatch):
    class Clock(datetime):
        instant = datetime(2026, 9, 9, 12, tzinfo=CST)

        @classmethod
        def now(cls, tz=None):
            return cls.instant.astimezone(tz)

    monkeypatch.setattr(sys.modules["datetime"], "datetime", Clock)
    return Clock


def test_defaults_follow_the_clock_for_future_scheduled_runs(cli, clock):
    cli.boundary.allow_io = False
    for instant, expected in [(datetime(2026, 9, 9, tzinfo=CST), "2026-09-06..2026-09-08"),
                              (datetime(2027, 1, 2, tzinfo=CST), "2026-12-30..2027-01-01")]:
        clock.instant = instant
        symbols = cli.root / "explicit.txt"
        symbols.write_text("KQ.m@SHFE.rb\n", encoding="utf-8")
        result = cli.run("--symbols-file", symbols, "--dry-run")
        assert result.code == 0
        assert result.events[-1]["SUMMARY"]["range"] == expected
        scope = next(event["SCOPE"] for event in result.events if "SCOPE" in event)
        assert scope["expected_through"] == expected.split("..")[1]


def test_invalid_cli_values_fail_before_credentials_or_scope_reads(cli, clock):
    cli.boundary.allow_io = False
    cli.cache.unlink()
    for args in [("--start", "20260901"), ("--end", "2026-9-07"),
                 ("--start", "2026-02-30"), ("--start", "2026-09-09", "--end", "2026-09-08"),
                 ("--end", "2026-09-10"), ("--expected-through", "2026-09-10"),
                 ("--expected-through", "bad"), ("--limit", "-1"),
                 ("--timeout", "0"), ("--timeout", "31"), ("--timeout", "nan")]:
        result = cli.run(*args)
        assert result.code == 2, args
        assert result.events == [], args
        assert result.stderr, args
    assert cli.settings_calls == []


def test_requests_use_the_bounded_timeout_and_explicit_scope(cli):
    symbols = cli.root / "explicit.txt"
    symbols.write_text("\ufeffKQ.m@SHFE.rb\nCFFEX.IF2609\nKQ.m@SHFE.rb\n", encoding="utf-8")
    result = cli.run("--symbols-file", symbols, "--start", "2026-09-07", "--end", "2026-09-07",
                     "--timeout", "2.5", "--limit", "1")
    assert result.code == 0
    assert [item["timeout"] for item in cli.requests] == [2.5, 2.5]
    bars = cli.requests[1]
    assert bars["symbol"] == "CFFEX.IF2609"
    assert bars["start_time"] == "2026-09-07 00:00:00"
    assert bars["end_time"] == "2026-09-07 23:59:59"
    assert result.events[-1]["SUMMARY"]["symbols"] == 1
    assert cli.disposed == [True]


def test_old_gap_scope_uses_expiry_seconds_and_never_scans_market_bars(cli):
    cli.cache.write_text(json.dumps({"generated_at": "2026-09-01T14:58:47+08:00", "products": {
        "SHFE.rb": [contract("SHFE.rb2608", "2026-08-17T00:30:00+08:00", expired=True),
                    contract("SHFE.rb2607", "2026-08-16T23:59:59+08:00"),
                    contract("SHFE.rb2610"), contract("SHFE.rb2610"),
                    contract("SHFE.rb2608C3000"), contract("KQ.m@SHFE.rb"),
                    contract("SHFE.rb2611", ins_class="OPTION")],
        "DCE.m": [contract("DCE.m2609-C-3000")],
        "CZCE.SR": [contract("CZCE.SR609")],
    }}), encoding="utf-8")
    original = cli.cache.read_bytes()
    cli.boundary.allow_io = False
    result = cli.run("--start", "2026-08-17", "--end", "2026-08-18", "--dry-run", "--limit", "2")

    assert result.code == 0
    scope = next(event["SCOPE"] for event in result.events if "SCOPE" in event)
    assert scope["symbols"] == ["CZCE.SR609", "SHFE.rb2608"]
    assert scope["source"] == "cache"
    assert scope["generated_at"] == "2026-09-01T14:58:47+08:00"
    assert scope["cache_stale"] is True
    assert "expired" in scope["note"] and "new listings" in scope["note"]
    assert cli.cache.read_bytes() == original
    assert sorted(p.name for p in cli.downloads.iterdir()) == [cli.cache.name]


def test_invalid_csv_fails_the_symbol_instead_of_dropping_rows(cli):
    invalid_rows = ["broken\n", BAR.rstrip() + ",extra\n", BAR.replace(",10,", ",NaN,"),
                    BAR.replace(",10,", ",-1,"), BAR.replace(",12,", ",8,"),
                    BAR.replace(",7,", ",Infinity,"), BAR.replace("1788739200000000000", "0"),
                    BAR.replace(",11,", ",oops,"), "\n"]
    for bad_row in invalid_rows:
        earlier = BAR.replace("1788739200000000000", "1788739140000000000")
        cli.boundary.responses = {"CFFEX.IF2609": HEADER + earlier + bad_row}
        result = cli.run("--start", "2026-09-07", "--end", "2026-09-07")
        assert result.code == 1, bad_row
        assert result.events[-1]["SUMMARY"]["done"] == 0
        assert result.events[-1]["SUMMARY"]["failed"] == 1
        assert cli.imports == []


def test_decimal_precision_and_nanosecond_dates_survive_import_csv(cli):
    cli.boundary.responses = {"CFFEX.IF2609": HEADER +
        "1788796799999999999,12345678901234567.1234567890,12345678901234568.1234567890,"
        "12345678901234566.1234567890,12345678901234567.1234567890,9007199254740993,7,8\n"}
    result = cli.run("--start", "2026-09-07", "--end", "2026-09-07")
    assert result.code == 0
    row = cli.imports[0][1][0]
    assert row["open"] == "12345678901234567.1234567890"
    assert row["volume"] == "9007199254740993"
    assert row["datetime_nano"] == "1788796799999999999"
    assert row["datetime"] == "2026-09-07T23:59:59.999999+08:00"


def test_stale_empty_failed_and_fresh_symbols_have_distinct_manifest_results(cli):
    symbols = cli.root / "symbols.txt"
    symbols.write_text("CFFEX.IF2609\nCFFEX.IF2610\nCFFEX.IF2611\nCFFEX.IF2612\n", encoding="utf-8")
    cli.boundary.responses = {
        "CFFEX.IF2609": HEADER + BAR,
        "CFFEX.IF2610": HEADER,
        "CFFEX.IF2611": TimeoutError("secret-password secret-token"),
        "CFFEX.IF2612": HEADER + BAR.replace("1788739200000000000", "1788825600000000000"),
    }
    manifest = cli.root / "run.json"
    result = cli.run("--start", "2026-09-07", "--end", "2026-09-08", "--symbols-file", symbols,
                     "--manifest-output", manifest)
    assert result.code == 1
    assert result.events[-1]["SUMMARY"] == {
        "symbols": 4, "done": 1, "failed": 1, "rows": 2, "duration": 86400,
        "range": "2026-09-07..2026-09-08"}
    report = json.loads(manifest.read_text(encoding="utf-8"))
    assert report["status"] == "incomplete"
    assert [r["status"] for r in report["results"]] == ["stale", "empty", "failed", "ok"]
    assert [r["rows"] for r in report["results"]] == [1, 0, 0, 1]
    assert [r["latest_raw_date"] for r in report["results"]] == ["2026-09-07", None, None, "2026-09-08"]
    assert [r["freshness"] for r in report["results"]] == ["stale", "unavailable", "unavailable", "fresh"]
    assert report["results"][2]["reason"] == "TimeoutError"
    assert all(r["reason"] for r in report["results"][:3])
    assert "secret" not in manifest.read_text() + json.dumps(result.events) + result.stderr
    assert [s for s, rows in cli.imports] == ["CFFEX.IF2609", "CFFEX.IF2612"]
    assert cli.disposed == [True]


def test_raw_dates_bound_import_and_out_of_range_future_does_not_mask_staleness(cli):
    # September 6; September 7 21:00 (not September 8 trading day); September 9.
    cli.boundary.responses = {"CFFEX.IF2609": HEADER +
        BAR.replace("1788739200000000000", "1788652800000000000") +
        BAR.replace("1788739200000000000", "1788786000000000000") +
        BAR.replace("1788739200000000000", "1788912000000000000")}
    manifest = cli.root / "raw-dates.json"
    result = cli.run("--start", "2026-09-07", "--end", "2026-09-08", "--duration", "60",
                     "--manifest-output", manifest)
    assert result.code == 1
    assert len(cli.imports[0][1]) == 1
    assert cli.imports[0][1][0]["datetime"] == "2026-09-07T21:00:00+08:00"
    record = json.loads(manifest.read_text())["results"][0]
    assert record["status"] == "stale"
    assert record["latest_raw_date"] == "2026-09-09"
    assert record["latest_imported_raw_date"] == "2026-09-07"
    assert record["rows_outside_range"] == 2


def test_explicit_expected_through_accepts_known_last_session(cli):
    result = cli.run("--start", "2026-09-07", "--end", "2026-09-08", "--expected-through", "2026-09-07")
    assert result.code == 0
    assert result.events[-1]["SUMMARY"]["done"] == 1


def test_entirely_out_of_range_response_is_empty_and_not_imported(cli):
    result = cli.run("--start", "2026-09-08", "--end", "2026-09-08")
    assert result.code == 1
    assert cli.imports == []
    record = next(event for event in result.events if event.get("status") == "empty")
    assert record["reason"] == "no_rows_in_requested_range"
    assert record["latest_raw_date"] == "2026-09-07"


def test_observed_edb_datetime_nano_header_imports_three_daily_bars(cli):
    symbols = cli.root / "rb.txt"
    symbols.write_text("SHFE.rb2610\n", encoding="utf-8")
    # Exact header observed from EDB; representative rb rows for September 7–9.
    cli.boundary.responses = {"SHFE.rb2610": (
        "datetime_nano,open,high,low,close,volume,open_oi,close_oi\n"
        "1788739200000000000,3100.10,3120,3090,3110,500,700,800\n"
        "1788825600000000000,3110.10,3130,3100,3120,600,800,900\n"
        "1788912000000000000,3120.10,3140,3110,3130,700,900,1000\n"
    )}
    result = cli.run("--symbols-file", symbols, "--start", "2026-09-07", "--end", "2026-09-09")

    assert result.code == 0
    assert result.events[-1]["SUMMARY"]["done"] == 1
    assert result.events[-1]["SUMMARY"]["rows"] == 3
    symbol, rows = cli.imports[0]
    assert symbol == "SHFE.rb2610"
    assert [row["datetime"][:10] for row in rows] == ["2026-09-07", "2026-09-08", "2026-09-09"]
    assert rows[0]["open"] == "3100.10"
    record = next(event for event in result.events if event.get("symbol") == symbol)
    assert record["latest_raw_date"] == "2026-09-09"
    assert record["freshness"] == "fresh"


def test_missing_or_wrong_csv_header_is_failed(cli):
    for text in [BAR, "", HEADER.replace("high,low", "low,high") + BAR]:
        cli.boundary.responses = {"CFFEX.IF2609": text}
        result = cli.run("--start", "2026-09-07", "--end", "2026-09-07")
        assert result.code == 1
        assert result.events[-1]["SUMMARY"]["failed"] == 1
        assert cli.imports == []


def test_empty_or_unusable_cache_scope_never_loads_credentials(cli):
    cli.boundary.allow_io = False
    cli.cache.unlink()
    result = cli.run("--start", "2026-09-07", "--end", "2026-09-07")
    assert result.code == 1
    assert result.events[-1]["SUMMARY"]["symbols"] == 0
    for entries in [[], [contract("SHFE.rb2608", "2026-08-17T15:00:00+08:00")],
                    [dict(contract("SHFE.rb2609"), expire_datetime=None)],
                    [dict(contract("SHFE.rb2609"), expire_datetime=1789714800000)]]:
        cli.cache.write_text(json.dumps({"generated_at": "2026-09-01T14:58:47+08:00",
                                        "products": {"SHFE.rb": entries}}))
        result = cli.run("--start", "2026-09-07", "--end", "2026-09-07")
        assert result.code == 1
        assert result.events[-1]["SUMMARY"]["done"] == 0
    assert cli.settings_calls == []


def test_existing_manifest_is_preserved_before_any_external_access(cli):
    cli.boundary.allow_io = False
    manifest = cli.root / "keep.json"
    manifest.write_text("existing evidence", encoding="utf-8")
    result = cli.run("--manifest-output", manifest)
    assert result.code == 2
    assert manifest.read_text() == "existing evidence"
    assert cli.settings_calls == []


def test_dry_run_writes_selected_scope_manifest_without_credentials(cli):
    cli.boundary.allow_io = False
    manifest = cli.root / "scope.json"
    result = cli.run("--start", "2026-09-07", "--end", "2026-09-07", "--dry-run",
                     "--manifest-output", manifest)
    assert result.code == 0
    report = json.loads(manifest.read_text())
    assert report["status"] == "dry_run"
    assert report["results"][0]["status"] == "selected"
    assert report["results"][0]["freshness"] == "unavailable"
    assert report["SUMMARY"]["done"] == 0
    assert cli.settings_calls == []


def test_setup_failures_are_safe_reported_and_dispose_created_engine(cli):
    for stage in ("credentials", "token", "database_settings", "snapshot"):
        cli.boundary.setup_error = stage
        result = cli.run("--start", "2026-09-07", "--end", "2026-09-07",
                         "--manifest-output", cli.root / (stage + ".json"))
        assert result.code == 1
        assert result.events[-1]["SUMMARY"]["failed"] == 1
        report = json.loads((cli.root / (stage + ".json")).read_text())
        assert report["results"][0]["reason"] == "RuntimeError"
        assert "secret" not in json.dumps(result.events) + json.dumps(report) + result.stderr
    assert cli.disposed == [True]


def test_overlapping_process_is_blocked_before_credentials_and_lock_is_reusable(cli):
    import subprocess
    # OS-level lock in a separate process: no network, env files, or database.
    lock = cli.downloads / "edb_daily_update.lock"
    code = (
        "import os, sys\n"
        "h = open(sys.argv[1], 'a+b')\n"
        "h.write(b'0'); h.flush(); h.seek(0)\n"
        "if os.name == 'nt':\n"
        " import msvcrt\n"
        " msvcrt.locking(h.fileno(), msvcrt.LK_NBLCK, 1)\n"
        "else:\n"
        " import fcntl\n"
        " fcntl.flock(h.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "print('locked', flush=True)\n"
        "sys.stdin.readline()\n"
    )
    with subprocess.Popen([sys.executable, "-B", "-c", code, str(lock)], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as holder:
        try:
            assert holder.stdout.readline().strip() == "locked"
            cli.boundary.allow_io = False
            result = cli.run("--start", "2026-09-07", "--end", "2026-09-07")
            assert result.code == 2
            assert any(e.get("status") == "blocked_existing_run_or_lock_unavailable" for e in result.events)
            assert cli.settings_calls == []
            # A separate minute duration can run while this process owns the daily lock.
            cli.boundary.allow_io = True
            minute = cli.run("--start", "2026-09-07", "--end", "2026-09-07", "--duration", "60")
            assert minute.code == 0
            assert (cli.downloads / "edb_minute_update.lock").exists()
            cli.imports.clear()
        finally:
            holder.communicate("release\n", timeout=10)
    cli.boundary.allow_io = True
    for _ in range(2):
        result = cli.run("--start", "2026-09-07", "--end", "2026-09-07")
        assert result.code == 0
    assert len(cli.imports) == 2  # Repeated scope uses the unchanged native idempotent importer.
    assert lock.exists()  # Never unlink the lock inode while another process might open it.


def test_explicit_symbols_allow_continuous_and_old_contracts_without_cache(cli):
    cli.cache.unlink()
    cli.boundary.allow_io = False
    symbols = cli.root / "old-probes.txt"
    symbols.write_text("\ufeffKQ.m@SHFE.rb\nSHFE.rb2005\nKQ.i@DCE.m\nKQ.m@SHFE.rb\n", encoding="utf-8")
    result = cli.run("--start", "2026-09-07", "--end", "2026-09-07", "--symbols-file", symbols, "--dry-run")
    assert result.code == 0
    scope = next(e["SCOPE"] for e in result.events if "SCOPE" in e)
    assert scope["source"] == "symbols_file"
    assert scope["symbols"] == ["KQ.i@DCE.m", "KQ.m@SHFE.rb", "SHFE.rb2005"]
    assert scope["candidates"] == 3


def test_explicit_file_rejects_non_symbol_text_before_credentials(cli):
    cli.boundary.allow_io = False
    symbols = cli.root / "invalid.txt"
    symbols.write_text("CFFEX.IF2609\nAuthorization: Bearer secret-token\n", encoding="utf-8")
    result = cli.run("--symbols-file", symbols)
    assert result.code == 1
    assert "secret-token" not in json.dumps(result.events) + result.stderr
    assert cli.requests == []


def test_manifest_write_failure_has_safe_nonzero_exit_and_keeps_stdout_summary(cli, monkeypatch):
    manifest = cli.root / "unwritable-after-reservation.json"
    original_open = Path.open

    class FullDisk:
        def write(self, value):
            raise OSError("secret-password secret-token disk full")

        def close(self):
            pass

    def open_file(path, *args, **kwargs):
        return FullDisk() if path == manifest else original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_file)
    result = cli.run("--start", "2026-09-07", "--end", "2026-09-07", "--manifest-output", manifest)
    assert result.code == 1
    assert result.events[-1]["SUMMARY"]["rows"] == 1
    assert any(e.get("reason") == "OSError" for e in result.events)
    assert "secret" not in json.dumps(result.events) + result.stderr
    assert cli.disposed == [True]


def test_real_cache_shape_and_default_timeout_work_without_any_discovery_query(cli):
    # Exercise default cache scope through fake fetch/import, including malformed option codes.
    cli.cache.write_text(json.dumps({"generated_at": "2026-09-01T14:58:47+08:00", "products": {
        "CFFEX.IF": [contract("CFFEX.IF2612", expired=True), contract("CFFEX.IF2609")],
        "CZCE.SR": [contract("CZCE.SR609"), contract("CZCE.SR609C7000")],
    }}))
    result = cli.run("--start", "2026-09-07", "--end", "2026-09-07", "--limit", "2")
    assert result.code == 0
    assert [r["symbol"] for r in cli.requests if r["kind"] == "bars"] == ["CFFEX.IF2609", "CFFEX.IF2612"]
    assert all(0 < r["timeout"] <= 30 for r in cli.requests)
    assert result.events[-1]["SUMMARY"]["done"] == 2
