"""SYNTHETIC OPT-02 regressions: no database, SDK, browser or network calls."""
from __future__ import annotations

from collections import Counter
from contextlib import AbstractContextManager
from datetime import date, timedelta
import importlib.util
import json
from pathlib import Path
import traceback
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError

from ak_trading import member_rank
from ak_trading.persistence import member_rank as persistence


ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 9, 1)
SYNTHETIC_PASSWORD = "SYNTHETIC_OPT02_PASSWORD_8ea42"
SYNTHETIC_DSN = (
    f"postgresql://SYNTHETIC_OPT02_USER:{SYNTHETIC_PASSWORD}"
    "@synthetic.invalid:5432/SYNTHETIC_OPT02_DB"
)
SYNTHETIC_PARAMETER = "SYNTHETIC_OPT02_PARAMETER_029b"
PRIVATE_MARKERS = (
    SYNTHETIC_PASSWORD, SYNTHETIC_DSN, SYNTHETIC_PARAMETER,
    "SYNTHETIC_OPT02_USER", "SYNTHETIC_OPT02_DB", "SYNTHETIC_OPT02_SQL",
)


def _row(exchange="CZCE", day=DAY, rank_no=1):
    return member_rank.MemberRankRow(
        exchange=exchange, trading_day=day, product_id="SYNTHETIC",
        instrument_id="ALL", metric=member_rank.METRIC_LONG, rank_no=rank_no,
        member_name=f"SYNTHETIC member {rank_no}", quantity=100 + rank_no,
        source_url="https://synthetic.invalid/ranks",
    )


def _result(exchange, day, status="ok"):
    return member_rank.MemberRankFetchResult(
        exchange=exchange, trading_day=day, status=status, source_urls=(),
        rows=(_row(exchange, day),) if status == "ok" else (),
        note="SYNTHETIC disclosure",
    )


def _driver_failure(kind="operational"):
    error = RuntimeError(
        f"SYNTHETIC driver failure: {SYNTHETIC_DSN}; password={SYNTHETIC_PASSWORD}"
    )
    if kind == "runtime":
        return error
    if kind == "database":
        return persistence.DatabaseOperationError(str(error))
    if kind == "member":
        return persistence.MemberRankImportError(str(error))
    return OperationalError(
        "SELECT :SYNTHETIC_OPT02_SQL", {"value": SYNTHETIC_PARAMETER}, error,
    )


class FakeEngine:
    """In-memory statement recorder with SYNTHETIC driver failure injection."""

    def __init__(self, *, failure=None, stage="execute", fail_transaction=None, batches=()):
        self.failure = failure
        self.stage = stage
        self.fail_transaction = fail_transaction
        self.batches = list(batches)
        self.begun = 0
        self.disposed = 0
        self.statements = []
        self.events = []

    @property
    def url(self):
        raise AssertionError("The public error boundary must not read engine.url")

    def _fail(self, stage, transaction):
        if (
            self.failure is not None and stage == self.stage
            and self.fail_transaction in (None, transaction)
        ):
            raise self.failure

    def begin(self):
        self.begun += 1
        self.events.append((self.begun, "begin"))
        self._fail("begin", self.begun)
        return FakeTransaction(self, self.begun)

    def dispose(self):
        self.disposed += 1


class FakeTransaction(AbstractContextManager):
    def __init__(self, engine, number):
        self.engine = engine
        self.number = number
        self.execute_count = 0

    def __enter__(self):
        self.engine._fail("enter", self.number)
        self.engine.events.append((self.number, "enter"))
        return self

    def __exit__(self, exc_type, exc_value, tb):
        if exc_type is not None:
            self.engine.events.append((self.number, "rollback"))
            return False
        self.engine._fail("commit", self.number)
        self.engine.events.append((self.number, "commit"))
        return False

    def execute(self, statement):
        self.execute_count += 1
        self.engine.statements.append(statement)
        self.engine._fail("execute", self.number)
        self.engine._fail(f"execute-{self.execute_count}", self.number)
        if statement.is_insert:
            values = statement.compile(dialect=postgresql.dialect()).params
            size = sum(name.startswith("exchange_m") for name in values)
            assert size > 0
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(range(size))))
        assert self.engine.batches, "Unexpected query: no SYNTHETIC result batch supplied"
        return self.engine.batches.pop(0)


@pytest.fixture
def collector():
    path = ROOT / "scripts/collect_member_ranks.py"
    spec = importlib.util.spec_from_file_location("opt02_candidate_collector", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Every test consumes its own candidate, never the historical source snapshot.
    assert Path(module.__file__).resolve() == path.resolve()
    assert Path(persistence.__file__).resolve() == (
        ROOT / "src/ak_trading/persistence/member_rank.py"
    ).resolve()
    assert Path(member_rank.__file__).resolve() == (
        ROOT / "src/ak_trading/member_rank.py"
    ).resolve()
    return module


def _patch_engine(monkeypatch, collector, engine):
    settings = object()
    monkeypatch.setattr(collector, "load_database_settings", lambda: settings)

    def create(value):
        assert value is settings
        return engine

    monkeypatch.setattr(collector, "create_database_engine", create)


def _record_evidence(monkeypatch, collector):
    recorded = []
    save = collector._save_evidence

    def record(directory, exchange, day, result):
        recorded.append((exchange, day))
        return save(directory, exchange, day, result)

    monkeypatch.setattr(collector, "_save_evidence", record)
    return recorded


def _output(capsys):
    output = capsys.readouterr()
    assert output.err == ""
    lines = output.out.splitlines()
    assert lines[-1].startswith("summary ")
    return (
        [json.loads(line) for line in lines[:-1]],
        json.loads(lines[-1].removeprefix("summary ")), output.out,
    )


def _assert_daily_accounting(records, summary, expected):
    assert len(records) == len(expected)
    actual = [(item["exchange"], date.fromisoformat(item["day"])) for item in records]
    assert Counter(actual) == Counter(expected)
    assert len(set(actual)) == len(expected)
    counts = Counter(item["status"] for item in records)
    assert {name: summary[name] for name in ("ok", "error", "unavailable")} == {
        name: counts[name] for name in ("ok", "error", "unavailable")
    }
    assert sum(summary[name] for name in ("ok", "error", "unavailable")) == len(expected)


@pytest.mark.parametrize(
    ("span", "exchanges", "no_bulk"),
    [
        pytest.param(1, ("CZCE",), False, id="single-day-czce"),
        pytest.param(3, ("CZCE",), True, id="multi-day-no-bulk"),
        pytest.param(1, ("CZCE", "SHFE", "GFEX"), False, id="single-day-mixed"),
        pytest.param(3, ("CZCE", "SHFE", "GFEX"), True, id="multi-day-mixed-no-bulk"),
        pytest.param(3, ("CZCE", "SHFE", "GFEX"), False, id="multi-day-mixed-bulk"),
    ],
)
def test_each_exchange_day_requested_recorded_and_counted_once(
    monkeypatch, collector, tmp_path, capsys, span, exchanges, no_bulk,
):
    days = [DAY + timedelta(days=offset) for offset in range(span)]
    direct_calls, bulk_calls, requested = [], [], []
    collect = collector.collect_day
    for exchange in exchanges:
        def fetch(day, exchange=exchange):
            requested.append((exchange, day))
            return _result(exchange, day)
        monkeypatch.setitem(member_rank.FETCHERS, exchange, fetch)

    def collect_day(day, *, exchanges):
        direct_calls.append((day, tuple(exchanges)))
        return collect(day, exchanges=exchanges)

    def bulk(values):
        bulk_calls.append(tuple(values))
        requested.extend(("CZCE", day) for day in values)
        return {day: _result("CZCE", day) for day in values}

    monkeypatch.setattr(collector, "collect_day", collect_day)
    monkeypatch.setattr(collector, "fetch_czce_rank_bulk", bulk)
    recorded = _record_evidence(monkeypatch, collector)
    engine = FakeEngine()
    _patch_engine(monkeypatch, collector, engine)
    argv = ["--start", days[0].isoformat(), "--end", days[-1].isoformat(),
            "--evidence-dir", str(tmp_path)]
    for exchange in exchanges:
        argv.extend(["--exchange", exchange])
    if no_bulk:
        argv.append("--no-czce-bulk")
    rc = collector.main(argv)
    records, summary, _ = _output(capsys)
    expected = [(exchange, day) for day in days for exchange in exchanges]
    assert Counter(requested) == Counter(expected)
    assert Counter(recorded) == Counter(expected)
    _assert_daily_accounting(records, summary, expected)
    assert summary["ok"] == summary["imported_rows"] == len(expected)
    assert rc == 0 and engine.disposed == 1
    others = tuple(exchange for exchange in exchanges if exchange != "CZCE")
    is_bulk = span > 1 and not no_bulk
    planned_direct = []
    for day in days:
        if others:
            planned_direct.append((day, others))
        if not is_bulk:
            planned_direct.append((day, ("CZCE",)))
    assert direct_calls == planned_direct
    assert bulk_calls == ([tuple(days)] if is_bulk else [])
    # All direct exchanges finish before the bulk request; daily CZCE is last otherwise.
    expected_order = [
        (exchange, day) for day, selected in planned_direct for exchange in selected
    ] + ([("CZCE", day) for day in days] if is_bulk else [])
    assert requested == expected_order
    assert len(list(tmp_path.rglob("manifest.json"))) == len(expected)


@pytest.mark.parametrize("mode", ["partial", "raised"])
def test_bulk_partial_or_total_failure_keeps_other_exchanges_and_daily_counts(
    monkeypatch, collector, tmp_path, capsys, mode,
):
    days = [DAY + timedelta(days=offset) for offset in range(3)]
    monkeypatch.setitem(member_rank.FETCHERS, "SHFE", lambda day: _result("SHFE", day))
    requests = []

    def bulk(values):
        requests.append(tuple(values))
        if mode == "raised":
            raise member_rank.MemberRankError("SYNTHETIC bulk source unavailable")
        return {days[0]: _result("CZCE", days[0]),
                days[1]: _result("CZCE", days[1], "unavailable")}

    monkeypatch.setattr(collector, "fetch_czce_rank_bulk", bulk)
    recorded = _record_evidence(monkeypatch, collector)
    engine = FakeEngine()
    _patch_engine(monkeypatch, collector, engine)
    rc = collector.main([
        "--start", days[0].isoformat(), "--end", days[-1].isoformat(),
        "--exchange", "CZCE", "--exchange", "SHFE", "--evidence-dir", str(tmp_path),
    ])
    records, summary, _ = _output(capsys)
    expected = [(exchange, day) for exchange in ("SHFE", "CZCE") for day in days]
    assert requests == [tuple(days)]
    assert Counter(recorded) == Counter(expected)
    _assert_daily_accounting(records, summary, expected)
    assert [(item["exchange"], item["day"]) for item in records[:3]] == [
        ("SHFE", day.isoformat()) for day in days
    ]
    assert all(item["status"] == "ok" for item in records[:3])
    expected_counts = (4, 1, 1) if mode == "partial" else (3, 0, 3)
    assert (summary["ok"], summary["unavailable"], summary["error"]) == expected_counts
    assert summary["imported_rows"] == expected_counts[0]
    assert rc == 1 and engine.disposed == 1


def test_bulk_retains_one_synthetic_browser_request_per_thirty_day_chunk(
    monkeypatch, collector, tmp_path, capsys,
):
    days = [DAY + timedelta(days=offset) for offset in range(31)]
    chunks = []
    original_bulk = member_rank.fetch_czce_rank_bulk
    monkeypatch.setattr(member_rank, "_SCRAPLING_PYTHON", Path(__file__))

    def run(command, **kwargs):
        # This fake records the real implementation's chunk argv and writes only
        # SYNTHETIC captures. No subprocess/browser/interpreter is launched.
        targets = json.loads(command[3])
        chunks.append([item["day"] for item in targets])
        for item in targets:
            Path(item["path"]).write_text("SYNTHETIC " * 150, encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout=json.dumps({"results": [
            {"day": item["day"], "status": 200} for item in targets
        ]}))

    monkeypatch.setattr(member_rank.subprocess, "run", run)
    monkeypatch.setattr(member_rank, "parse_czce_htm", lambda raw, *, trading_day, source_url: [
        _row("CZCE", trading_day)
    ])
    monkeypatch.setattr(collector, "fetch_czce_rank_bulk", lambda values: original_bulk(
        values, profile_directory=tmp_path / "synthetic-captures",
    ))
    # A direct CZCE request in bulk mode is a regression, not an allowed fallback.
    def unexpected_day(*args, **kwargs):
        pytest.fail("bulk CZCE must not be requested through collect_day")
    monkeypatch.setattr(collector, "collect_day", unexpected_day)
    recorded = _record_evidence(monkeypatch, collector)
    rc = collector.main([
        "--start", days[0].isoformat(), "--end", days[-1].isoformat(),
        "--exchange", "CZCE", "--dry-run", "--evidence-dir", str(tmp_path / "evidence"),
    ])
    records, summary, _ = _output(capsys)
    assert chunks == [[day.strftime("%Y%m%d") for day in days[:30]],
                      [days[-1].strftime("%Y%m%d")]]
    expected = [("CZCE", day) for day in days]
    assert recorded == expected
    _assert_daily_accounting(records, summary, expected)
    assert summary["ok"] == 31 and summary["imported_rows"] == 0 and rc == 0


@pytest.mark.parametrize("boundary", ["import", "top20"])
@pytest.mark.parametrize("stage", ["begin", "enter", "execute", "execute-2", "commit"])
@pytest.mark.parametrize("kind", ["runtime", "operational", "database", "member"])
def test_driver_errors_are_fixed_and_suppressed_on_every_public_surface(boundary, stage, kind):
    engine = FakeEngine(failure=_driver_failure(kind), stage=stage, batches=[[], []])
    with pytest.raises(persistence.MemberRankImportError) as caught:
        if boundary == "import":
            persistence.import_member_rank_rows(engine, [_row(rank_no=1), _row(rank_no=2)], chunk_size=1)
        else:
            persistence.top20_net_by_product(engine, DAY, exchange="CZCE")
    message = "member-rank import failed" if boundary == "import" else "top20 net query failed"
    public = caught.value
    # str(), formatted traceback and the JSON error representation must all be safe.
    surfaces = [str(public), "".join(traceback.format_exception(public)),
                json.dumps({"status": "error", "note": str(public)})]
    for text in surfaces:
        assert all(marker not in text for marker in PRIVATE_MARKERS)
        assert message in text
    assert str(public) == message
    assert public.__cause__ is None
    assert public.__suppress_context__ is True
    assert engine.begun == 1
    if stage.startswith("execute"):
        assert engine.events[-1] == (1, "rollback")


@pytest.mark.parametrize("stage", ["begin", "enter", "execute", "commit"])
def test_cli_import_failure_is_safe_and_counted_while_healthy_exchanges_continue(
    monkeypatch, collector, tmp_path, capsys, stage,
):
    requested = []
    for exchange in ("CZCE", "SHFE", "CFFEX", "GFEX", "INE"):
        def fetch(day, exchange=exchange):
            requested.append((exchange, day))
            if exchange == "GFEX":
                raise member_rank.MemberRankError("SYNTHETIC upstream failure")
            return _result(exchange, day, "unavailable" if exchange == "INE" else "ok")
        monkeypatch.setitem(member_rank.FETCHERS, exchange, fetch)
    # CFFEX import succeeds, SHFE import fails, then the independent CZCE succeeds.
    engine = FakeEngine(failure=_driver_failure(), stage=stage, fail_transaction=2)
    _patch_engine(monkeypatch, collector, engine)
    recorded = _record_evidence(monkeypatch, collector)
    rc = collector.main([
        "--start", DAY.isoformat(), "--end", DAY.isoformat(),
        "--exchange", "CZCE", "--exchange", "SHFE", "--exchange", "CFFEX",
        "--exchange", "GFEX", "--exchange", "INE", "--evidence-dir", str(tmp_path),
    ])
    records, summary, text = _output(capsys)
    assert all(marker not in text for marker in PRIVATE_MARKERS)
    expected = [(exchange, DAY) for exchange in ("CZCE", "SHFE", "CFFEX", "GFEX", "INE")]
    assert Counter(requested) == Counter(recorded) == Counter(expected)
    _assert_daily_accounting(records, summary, expected)
    by_exchange = {item["exchange"]: item for item in records}
    assert by_exchange["SHFE"]["status"] == "error"
    assert by_exchange["SHFE"]["note"] == "import failed: member-rank import failed"
    assert "imported" not in by_exchange["SHFE"]
    assert by_exchange["GFEX"]["status"] == "error"
    assert by_exchange["INE"]["status"] == "unavailable"
    assert by_exchange["CFFEX"]["imported"] == by_exchange["CZCE"]["imported"] == 1
    assert (summary["ok"], summary["error"], summary["unavailable"]) == (2, 2, 1)
    assert summary["imported_rows"] == 2
    assert rc == 1 and engine.begun == 3 and engine.disposed == 1


def test_import_success_preserves_chunk_transaction_and_upsert_statement():
    engine = FakeEngine()
    rows = [_row(rank_no=number) for number in (1, 2, 3)]
    outcome = persistence.import_member_rank_rows(engine, rows, chunk_size=2)
    assert outcome.as_dict() == {
        "exchange": "CZCE", "trading_day": DAY.isoformat(), "rows_received": 3, "rows_written": 3,
    }
    assert engine.events == [(1, "begin"), (1, "enter"), (1, "commit")]
    assert len(engine.statements) == 2
    all_quantities = []
    for statement in engine.statements:
        compiled = statement.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        assert "ON CONFLICT ON CONSTRAINT uq_member_ranks_day_instrument_metric_rank_member DO UPDATE" in sql
        assert "RETURNING member_position_ranks.id" in sql
        all_quantities.extend(value for name, value in compiled.params.items() if name.startswith("quantity_m"))
    assert all_quantities == [row.quantity for row in rows]


def test_top20_success_preserves_aggregate_precedence_and_contract_rollup():
    def aggregate(exchange, product, metric, quantity):
        return SimpleNamespace(exchange=exchange, product_id=product, metric=metric, quantity=quantity)
    engine = FakeEngine(batches=[
        [aggregate("CZCE", "SYNTHETIC", "long", 100), aggregate("CZCE", "SYNTHETIC", "short", 30)],
        [aggregate("CZCE", "SYNTHETIC", "long", 999),
         aggregate("CFFEX", "SYNTHETIC", "long", 80), aggregate("CFFEX", "SYNTHETIC", "short", 20)],
    ])
    result = persistence.top20_net_by_product(engine, DAY)
    assert [(item.exchange, item.long_top20, item.short_top20, item.net_top20) for item in result] == [
        ("CFFEX", 80, 20, 60), ("CZCE", 100, 30, 70),
    ]
    assert engine.events == [(1, "begin"), (1, "enter"), (1, "commit")]
    assert len(engine.statements) == 2 and not engine.batches
    for statement in engine.statements:
        values = statement.compile().params
        assert DAY in values.values()
        assert 1 in values.values() and 20 in values.values()
        assert [member_rank.METRIC_LONG, member_rank.METRIC_SHORT] in values.values()
