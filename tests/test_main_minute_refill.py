from __future__ import annotations

import csv
import importlib.util
import ssl
import sys
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("main_minute_refill", ROOT / "scripts/fill_recent_main_minute.py")
refill = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(refill)
SYMBOLS = ["KQ.m@SHFE.rb", "KQ.m@DCE.m", "KQ.m@CZCE.TA"]
SECRET = "password=must-never-appear"
NOW = datetime.fromisoformat("2026-09-09T10:00:30+08:00").timestamp()


def bar(when="2026-09-08T14:59:00+08:00", **overrides):
    return {"datetime": int(datetime.fromisoformat(when).timestamp()) * 1_000_000_000,
            "open": 10.0, "high": 12.0, "low": 9.0, "close": 11.0,
            "volume": 3.0, "open_oi": 0.0, "close_oi": 0.0, **overrides}


class Clock:
    def __init__(self):
        self.elapsed = 0.0

    def time(self):
        return NOW + self.elapsed

    def monotonic(self):
        return self.elapsed


class FakeApi:
    def __init__(self, clock):
        self.clock = clock
        self.frames = {symbol: pd.DataFrame([bar()]) for symbol in SYMBOLS}
        self.calls = []
        self.deadlines = []
        self.ready_after = 0
        self.closed = False
        self.fetch_errors = []

    def get_kline_serial(self, symbol, duration, data_length):
        assert duration == 60
        self.calls.append((symbol, data_length))
        if self.fetch_errors:
            error = self.fetch_errors.pop(0)
            if error is not None:
                raise error
        frame = self.frames[symbol]
        if isinstance(frame, Exception):
            raise frame
        return frame

    def create_task(self, coroutine):
        # Run the subscription like a public SDK task in its event loop.
        try:
            coroutine.send(None)
        except StopIteration as done:
            result, error = done.value, None
        except Exception as exc:
            result, error = None, exc
        def task_result():
            if error is not None:
                raise error
            return result
        return SimpleNamespace(done=lambda: True, result=task_result)

    def is_serial_ready(self, frame):
        return len(self.deadlines) >= self.ready_after

    def wait_update(self, *, deadline):
        assert self.clock.time() < deadline <= self.clock.time() + 120
        self.deadlines.append(deadline)
        self.clock.elapsed += 1
        return True

    def close(self):
        self.closed = True


@pytest.fixture
def harness(tmp_path, monkeypatch):
    clock = Clock()
    api = FakeApi(clock)
    engine = SimpleNamespace(disposed=False)
    engine.dispose = lambda: setattr(engine, "disposed", True)
    symbols_file = tmp_path / "symbols.txt"
    symbols_file.write_text("\n".join(SYMBOLS), encoding="utf-8")
    downloads = tmp_path / "data/downloads"
    downloads.mkdir(parents=True)
    imported = []
    paths = []
    events = []
    book = refill.load_reference_book(ROOT / "config/market_reference.json")

    def fake_import(engine, path, **kwargs):
        paths.append(Path(path))
        with Path(path).open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        imported.append((kwargs["symbol"], rows, kwargs))
        return SimpleNamespace(rows_written=len(rows))

    monkeypatch.setattr(refill, "ROOT", tmp_path)
    monkeypatch.setattr(refill, "time", clock, raising=False)
    monkeypatch.setattr(refill, "load_settings", lambda: events.append("credentials") or SimpleNamespace(tq_user="user", tq_pass=SECRET))
    monkeypatch.setattr(refill, "load_database_settings", lambda **_: SimpleNamespace())
    monkeypatch.setattr(refill, "create_database_engine", lambda _: engine)
    monkeypatch.setattr(refill, "load_reference_book", lambda _: book)
    monkeypatch.setattr(refill, "store_reference_snapshot", lambda *_, **__: SimpleNamespace(content_sha256="reference-hash"))
    monkeypatch.setattr(refill, "import_market_bars", fake_import)
    monkeypatch.setitem(sys.modules, "tqsdk", SimpleNamespace(
        TqApi=lambda **_: api, TqAuth=lambda *_: object(), TqTimeoutError=type("TqTimeoutError", (Exception,), {})))

    def run(*args):
        monkeypatch.setattr(sys, "argv", ["fill_recent_main_minute.py", "--symbols-file", str(symbols_file), *args])
        return refill.main()

    return SimpleNamespace(api=api, clock=clock, engine=engine, symbols_file=symbols_file,
                           imported=imported, paths=paths, events=events, book=book, run=run)


def test_ready_serial_does_not_wait_and_preserves_frame(harness):
    original = harness.api.frames[SYMBOLS[0]].copy(deep=True)
    assert harness.run() == 0
    assert harness.api.deadlines == []
    assert len(harness.imported) == 3
    pd.testing.assert_frame_equal(harness.api.frames[SYMBOLS[0]], original)
    assert harness.api.closed and harness.engine.disposed
    assert all(not path.exists() for path in harness.paths)


def test_pending_serial_uses_future_deadline_until_ready(harness):
    harness.api.ready_after = 2
    assert harness.run("--timeout", "5") == 0
    assert len(harness.api.deadlines) == 2
    assert harness.api.deadlines == [NOW + 5, NOW + 5]


def test_partial_failure_returns_nonzero_and_sanitizes_errors(harness, capsys):
    harness.api.frames[SYMBOLS[1]] = ValueError(SECRET)
    assert harness.run() == 1
    output = capsys.readouterr().out
    assert SECRET not in output
    assert "FILL_SUMMARY done=2 failed=1" in output
    assert "FILL_OK" in output
    assert harness.api.closed and harness.engine.disposed


def test_unready_stream_has_one_budget_per_attempt_and_two_retries(harness, capsys):
    harness.symbols_file.write_text(SYMBOLS[0], encoding="utf-8")
    harness.api.ready_after = 999
    assert harness.run("--timeout", "2") == 1
    assert harness.clock.elapsed == 6
    assert len(harness.api.calls) == 3
    assert len(harness.api.deadlines) == 6
    assert harness.imported == []
    assert capsys.readouterr().out.count("FILL_RETRY") == 2
    assert harness.api.closed and harness.engine.disposed


def test_standalone_wait_handles_sdk_false_and_already_ready(harness, monkeypatch):
    frame = harness.api.frames[SYMBOLS[0]]
    assert refill.wait_ready(harness.api, frame, 2) is True
    harness.api.ready_after = 1
    monkeypatch.setattr(harness.api, "wait_update", lambda **_: False)
    assert refill.wait_ready(harness.api, frame, 2) is False


def test_subscription_timeout_cancels_public_task(harness, monkeypatch):
    pending = SimpleNamespace(cancelled=False)
    pending.done = lambda: False
    pending.cancel = lambda: setattr(pending, "cancelled", True)
    def create_pending(coroutine):
        coroutine.close()
        return pending
    monkeypatch.setattr(harness.api, "create_task", create_pending)
    with pytest.raises(TimeoutError):
        refill.fetch_snapshot(harness.api, SYMBOLS[0], 2000, 2)
    assert pending.cancelled
    assert harness.clock.elapsed == 2
    # Real public SDK tasks start on wait_update, not at create_task time.
    state = {}
    def deferred(coroutine):
        state["coroutine"] = coroutine
        state["done"] = False
        return SimpleNamespace(done=lambda: state["done"], result=lambda: state["frame"],
                               cancel=lambda: coroutine.close())
    def quiet_update(*, deadline):
        harness.clock.elapsed += deadline - harness.clock.time()
        try:
            state["coroutine"].send(None)
        except StopIteration as result:
            state.update(done=True, frame=result.value)
        return False
    monkeypatch.setattr(harness.api, "create_task", deferred)
    monkeypatch.setattr(harness.api, "wait_update", quiet_update)
    snapshot = refill.fetch_snapshot(harness.api, SYMBOLS[0], 2000, 30)
    assert len(snapshot) == 1 and harness.clock.elapsed == 3


def test_transient_fetch_retries_only_affected_symbol_and_imports_once(harness, capsys):
    harness.api.fetch_errors = [None, ConnectionError(SECRET), ssl.SSLError(SECRET), None]
    assert harness.run() == 0
    assert [symbol for symbol, _ in harness.api.calls] == [SYMBOLS[0], SYMBOLS[1], SYMBOLS[1], SYMBOLS[1], SYMBOLS[2]]
    assert [symbol for symbol, _, _ in harness.imported] == SYMBOLS
    assert capsys.readouterr().out.count("FILL_RETRY") == 2


def test_database_connection_failure_is_not_retried_and_temp_is_removed(harness, monkeypatch, capsys):
    original_import = refill.import_market_bars
    failed_paths = []
    def fail_second(engine, path, **kwargs):
        if kwargs["symbol"] == SYMBOLS[1]:
            failed_paths.append(Path(path))
            raise ConnectionError(SECRET)
        return original_import(engine, path, **kwargs)
    monkeypatch.setattr(refill, "import_market_bars", fail_second)
    assert harness.run() == 1
    assert len(harness.api.calls) == 3 and len(failed_paths) == 1
    assert all(not path.exists() for path in failed_paths + harness.paths)
    output = capsys.readouterr().out
    assert SECRET not in output and "FILL_RETRY" not in output
    assert "context=import" in output
    assert harness.api.closed and harness.engine.disposed


def test_empty_and_stale_counts_make_partial_run_fail(harness, capsys):
    harness.api.frames[SYMBOLS[1]] = pd.DataFrame([bar(datetime=0), bar(datetime=float("nan"))])
    harness.api.frames[SYMBOLS[2]] = pd.DataFrame([bar("2026-09-07T14:59:00+08:00")])
    assert harness.run("--expected-through", "2026-09-08") == 1
    output = capsys.readouterr().out
    assert "FILL_SUMMARY done=1 failed=0 rows=2 empty=1 stale=1" in output
    assert "FILL_EMPTY" in output and "calendar_empty=unknown" in output
    assert "FILL_STALE" in output and "2026-09-07T14:59:00+08:00" in output
    assert [symbol for symbol, _, _ in harness.imported] == [SYMBOLS[0], SYMBOLS[2]]


def test_forming_row_is_excluded_but_a_closed_final_row_is_kept(harness):
    harness.api.frames[SYMBOLS[0]] = pd.DataFrame([
        bar("2026-09-09T09:59:00+08:00"),
        bar("2026-09-09T10:00:00+08:00", close=float("nan")),
        bar(datetime=0), bar(datetime=float("nan")),
    ])
    harness.api.frames[SYMBOLS[1]] = pd.DataFrame([
        bar("2026-09-09T09:58:00+08:00"), bar("2026-09-09T09:59:00+08:00")])
    originals = {s: f.copy(deep=True) for s, f in harness.api.frames.items()}
    assert harness.run() == 0
    assert [len(rows) for _, rows, _ in harness.imported] == [1, 2, 1]
    for symbol, original in originals.items():
        pd.testing.assert_frame_equal(harness.api.frames[symbol], original)


def test_explicit_aware_cutoffs_are_exclusive_for_start_and_inclusive_for_close(harness):
    rows = [bar("2026-09-08T14:58:00+08:00"), bar(), bar("2026-09-08T15:00:00+08:00")]
    for symbol in SYMBOLS:
        harness.api.frames[symbol] = pd.DataFrame(rows)
    assert harness.run("--closed-before", "2026-09-08T07:00:00Z", "--end", "2026-09-08T14:59:00+08:00") == 0
    assert all(len(rows) == 1 for _, rows, _ in harness.imported)
    valid, _ = refill.select_closed_frame(pd.DataFrame(rows), calendar=harness.book.calendar,
                                         closed_before=datetime.fromisoformat("2026-09-08T15:00:00+08:00"))
    assert len(valid) == 2
    cutoff = datetime.fromisoformat("2026-09-08T15:00:00.000001+08:00")
    start = bar()["datetime"] + 1000
    precise = pd.DataFrame([bar(datetime=start), bar(datetime=start + 1)])
    valid, _ = refill.select_closed_frame(precise, calendar=harness.book.calendar, closed_before=cutoff)
    assert list(valid["datetime"]) == [start]


def test_through_uses_night_trading_day_and_raw_following_midnight(harness, capsys):
    dates = ["2026-09-07T21:00:00+08:00", "2026-09-08T00:01:00+08:00",
             "2026-09-08T14:59:00+08:00", "2026-09-08T21:00:00+08:00",
             "2026-09-09T00:00:00+08:00"]
    for symbol in SYMBOLS:
        harness.api.frames[symbol] = pd.DataFrame([bar(day) for day in dates])
    assert harness.run("--through", "2026-09-08", "--expected-through", "2026-09-08") == 0
    assert all(len(rows) == 3 for _, rows, _ in harness.imported)
    output = capsys.readouterr().out
    assert "reference_calendar_night" in output
    assert "raw_latest=2026-09-09T00:00:00+08:00 latest=2026-09-08T14:59:00+08:00" in output
    # A Monday label on a Friday-night bar does not claim Monday raw coverage.
    frame = pd.DataFrame([bar("2026-09-04T21:00:00+08:00")])
    _, details = refill.select_closed_frame(frame, calendar=harness.book.calendar, through=date(2026, 9, 7))
    assert details["latest_date"] == date(2026, 9, 4)


def test_unknown_calendar_labels_remain_explicit_and_do_not_fake_freshness(harness, capsys):
    # Saturday after-midnight data is unmapped by the local importer calendar.
    for symbol in SYMBOLS:
        harness.api.frames[symbol] = pd.DataFrame([bar("2026-09-05T00:01:00+08:00")])
    assert harness.run("--through", "2026-09-07", "--expected-through", "2026-09-07") == 1
    output = capsys.readouterr().out
    assert "unknown_trading_day_rows=1" in output and "non_trading_calendar_date" in output
    assert "stale=3" in output
    valid, details = refill.select_closed_frame(harness.api.frames[SYMBOLS[0]], calendar=None, through=date(2026, 9, 7))
    assert len(valid) == 1 and details["label_sources"] == "calendar_not_configured"


def test_invalid_closed_ohlc_never_silently_drops_a_row_or_leaks(harness, capsys):
    harness.api.frames[SYMBOLS[0]] = pd.DataFrame([bar("2026-09-08T14:58:00+08:00"), bar(close=float("nan"))])
    harness.api.frames[SYMBOLS[1]] = pd.DataFrame([bar(high=8)])
    harness.api.frames[SYMBOLS[2]] = pd.DataFrame([bar(open=SECRET)])
    assert harness.run() == 1
    assert harness.imported == []
    output = capsys.readouterr().out
    assert SECRET not in output and "FILL_SUMMARY done=0 failed=3" in output
    assert output.count("context=validation") == 3


def test_timestamp_validation_and_integer_nanoseconds_are_preserved(harness):
    nano = bar()["datetime"] + 123_456_789
    frame = pd.DataFrame([bar(datetime=nano)])
    valid, _ = refill.select_closed_frame(frame, calendar=harness.book.calendar)
    assert str(valid["datetime"].dtype) == "int64"
    assert int(valid.iloc[0]["datetime"]) != nano  # Row coercion loses nanosecond precision.
    for symbol in SYMBOLS:
        harness.api.frames[symbol] = frame
    assert harness.run() == 0
    assert all(int(rows[0]["datetime_nano"]) == nano for _, rows, _ in harness.imported)
    for bad in (float("inf"), -1, 1.5, SECRET):
        with pytest.raises(ValueError):
            refill.select_closed_frame(pd.DataFrame([bar(datetime=bad)]), calendar=harness.book.calendar)
    with pytest.raises(ValueError):
        refill.select_closed_frame(pd.DataFrame([bar(), bar()]), calendar=harness.book.calendar)


def test_invalid_cli_and_scope_fail_before_credentials_or_provider(harness, monkeypatch, capsys, tmp_path):
    monkeypatch.setitem(sys.modules, "tqsdk", None)
    bad_args = [
        ["--rows", "0"], ["--rows", "10001"], ["--rows", "nan"],
        ["--timeout", "0"], ["--timeout", "121"], ["--timeout", "nan"], ["--timeout", "inf"],
        ["--through", "2026-02-30"], ["--through", "9999-12-31"],
        ["--through", "2026-09-07", "--expected-through", "2026-09-08"],
        ["--closed-before", "2026-09-08T15:00:00"], ["--end", "not-a-date"],
        ["--unknown-option"],
    ]
    for args in bad_args:
        with pytest.raises(SystemExit) as error:
            harness.run(*args)
        assert error.value.code == 2
    for content in ("", "KQ.i@SHFE.rb", "KQ.m@UNKNOWN.rb", SECRET,
                    "\n".join("KQ.m@SHFE." + chr(65 + i // 26) + chr(65 + i % 26) for i in range(513))):
        harness.symbols_file.write_text(content, encoding="utf-8")
        with pytest.raises(SystemExit) as error:
            harness.run()
        assert error.value.code == 2
    for path in (tmp_path, tmp_path / "missing.txt"):
        with pytest.raises(SystemExit) as error:
            refill.main(["--symbols-file", str(path)])
        assert error.value.code == 2
    assert harness.events == []
    assert SECRET not in capsys.readouterr().err


def test_existing_81_symbol_scope_and_cli_boundaries(harness, capsys):
    assert len(refill.read_symbols(ROOT / "data/downloads/kqm_symbols.txt")) == 81
    harness.symbols_file.write_text("\ufeff" + SYMBOLS[0] + "\n\n" + SYMBOLS[0], encoding="utf-8")
    assert harness.run("--rows", "1", "--timeout", "120") == 0
    assert harness.api.calls == [(SYMBOLS[0], 1)]
    assert harness.run("--rows", "10000") == 0
    assert harness.api.calls[-1] == (SYMBOLS[0], 10000)
    assert "FILL_SUMMARY" in capsys.readouterr().out


def test_api_constructor_transient_ssl_and_permanent_auth_failures_are_safe(harness, monkeypatch, capsys):
    factory_calls = []
    def failing_api(**kwargs):
        factory_calls.append(kwargs)
        if len(factory_calls) <= 2:
            raise ssl.SSLError(SECRET)
        return harness.api
    provider = sys.modules["tqsdk"]
    monkeypatch.setattr(provider, "TqApi", failing_api)
    assert harness.run() == 0
    assert len(factory_calls) == 3 and len(harness.imported) == 3
    factory_calls.clear()
    def auth_failure(**kwargs):
        factory_calls.append(kwargs)
        raise RuntimeError(SECRET)
    monkeypatch.setattr(provider, "TqApi", auth_failure)
    harness.engine.disposed = False
    assert harness.run() == 1
    assert len(factory_calls) == 1 and harness.engine.disposed
    output = capsys.readouterr().out
    assert SECRET not in output and "context=api_create" in output
    assert "FILL_SUMMARY done=0 failed=3" in output


def test_cleanup_failures_still_dispose_engine_and_fail_exit(harness, monkeypatch, capsys):
    def bad_close():
        harness.api.closed = True
        raise RuntimeError(SECRET)
    monkeypatch.setattr(harness.api, "close", bad_close)
    assert harness.run() == 1
    assert harness.api.closed and harness.engine.disposed
    output = capsys.readouterr().out
    assert SECRET not in output and "cleanup_failed=1" in output


def test_setup_failure_disposes_engine_and_suppresses_exception(harness, monkeypatch, capsys):
    def fail_setup(*args, **kwargs):
        raise RuntimeError(SECRET)
    monkeypatch.setattr(refill, "store_reference_snapshot", fail_setup)
    assert harness.run() == 1
    assert harness.engine.disposed and not harness.api.closed
    assert harness.imported == []
    output = capsys.readouterr().out
    assert SECRET not in output and "context=database_setup" in output
