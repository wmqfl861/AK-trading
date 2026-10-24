from __future__ import annotations

import importlib.util
import json
import xml.etree.ElementTree as ElementTree
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from ak_trading import member_rank


DAY = date(2026, 9, 9)
EXCHANGES = ("CFFEX", "GFEX", "SHFE", "INE", "CZCE")
PRIVATE_URL = (
    "https://fixture-user:fixture-password@gfex.example.test/ranks"
    "?token=fixture-token"
)


@pytest.fixture
def healthy_exchanges(monkeypatch: pytest.MonkeyPatch):
    expected = {}
    calls = []
    for quantity, exchange in enumerate(("CFFEX", "SHFE", "INE", "CZCE"), start=101):
        url = f"https://{exchange.lower()}.example.test/ranks"
        result = member_rank.MemberRankFetchResult(
            exchange=exchange,
            trading_day=DAY,
            status="ok",
            source_urls=(url,),
            rows=(member_rank.MemberRankRow(
                exchange=exchange,
                trading_day=DAY,
                product_id="fixture",
                instrument_id="ALL",
                metric=member_rank.METRIC_LONG,
                rank_no=1,
                member_name="Fixture member",
                quantity=quantity,
                source_url=url,
            ),),
            evidence={url: b"synthetic disclosure"},
        )
        expected[exchange] = result

        def fetch(day: date, result=result):
            assert day == DAY
            calls.append(result.exchange)
            return result

        monkeypatch.setitem(member_rank.FETCHERS, exchange, fetch)
    return expected, calls


def _assert_partial_results(results, healthy_exchanges, error_type: str) -> None:
    expected, calls = healthy_exchanges
    assert tuple(results) == EXCHANGES
    assert calls == ["CFFEX", "SHFE", "INE", "CZCE"]
    for exchange, healthy in expected.items():
        assert results[exchange] is healthy
        assert results[exchange].status == "ok"
        assert results[exchange].rows[0].quantity > 0
        assert results[exchange].evidence
    failed = results["GFEX"]
    assert failed.exchange == "GFEX"
    assert failed.trading_day == DAY
    assert failed.status == "error"
    assert failed.rows == ()
    assert failed.evidence == {}
    assert failed.source_urls == ()
    assert failed.note == f"member-rank fetch failed: {error_type}"
    assert "fixture-user" not in repr(failed)
    assert "fixture-password" not in repr(failed)
    assert "fixture-token" not in repr(failed)


@pytest.mark.parametrize(
    ("status_code", "body", "message"),
    [
        pytest.param(200, b"<html>upstream failure</html>", "invalid JSON", id="invalid-json"),
        pytest.param(200, b"\xff", "invalid JSON", id="invalid-utf8"),
        pytest.param(403, b"forbidden", "HTTP 403", id="http-status"),
        pytest.param(200, b"[]", "invalid status", id="non-object"),
        pytest.param(200, b"null", "invalid status", id="null-payload"),
        pytest.param(200, b'{"data":[{"varietyId":"si"}]}', "invalid status", id="missing-code"),
        pytest.param(200, b'{"code":"1","data":[{"varietyId":"si"}]}', "invalid status", id="error-code"),
        pytest.param(200, b'{"code":"0","data":null}', "missing data", id="missing-data"),
        pytest.param(200, b'{"code":"0","data":{}}', "missing data", id="invalid-data"),
        pytest.param(200, b'{"code":"0","data":[]}', "empty", id="empty-varieties"),
        pytest.param(200, b'{"code":"0","data":["si"]}', "invalid varietyId", id="non-object-item"),
        pytest.param(200, b'{"code":"0","data":[{}]}', "invalid varietyId", id="missing-id"),
        pytest.param(200, b'{"code":"0","data":[{"varietyId":null}]}', "invalid varietyId", id="null-id"),
        pytest.param(200, b'{"code":"0","data":[{"varietyId":"  "}]}', "invalid varietyId", id="blank-id"),
        pytest.param(200, b'{"code":"0","data":[{"varietyId":123}]}', "invalid varietyId", id="numeric-id"),
        pytest.param(200, b'{"code":"0","data":[{"varietyId":"si"},{}]}', "invalid varietyId", id="partially-invalid-list"),
    ],
)
def test_gfex_discovery_failure_preserves_other_exchange_results(
    monkeypatch: pytest.MonkeyPatch, healthy_exchanges, status_code, body, message,
) -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert str(request.url) == "http://www.gfex.com.cn/u/interfacesWebVariety/loadList"
        return httpx.Response(status_code, content=body)

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as client:
        def fetch(day: date):
            return member_rank.fetch_gfex_rank(day, client=client)

        monkeypatch.setitem(member_rank.FETCHERS, "GFEX", fetch)
        results = member_rank.collect_day(DAY, exchanges=EXCHANGES)
        _assert_partial_results(results, healthy_exchanges, "MemberRankError")
        # Direct callers receive the documented fetch error, never raw JSON or
        # attribute errors, and discovery does not continue to contract requests.
        with pytest.raises(member_rank.MemberRankError) as caught:
            member_rank.fetch_gfex_rank(DAY, client=client)
        assert str(caught.value) == f"gfex variety list {message}"
    assert len(requests) == 2


@pytest.mark.parametrize(
    "failure",
    [
        member_rank.MemberRankError(f"source failed: {PRIVATE_URL}"),
        member_rank.MemberRankParseError(f"invalid data: {PRIVATE_URL}"),
        json.JSONDecodeError(f"invalid response: {PRIVATE_URL}", "<html>", 0),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, PRIVATE_URL),
        ElementTree.ParseError(f"invalid XML: {PRIVATE_URL}"),
        httpx.ConnectError(PRIVATE_URL, request=httpx.Request("GET", PRIVATE_URL)),
        httpx.ReadTimeout(PRIVATE_URL, request=httpx.Request("GET", PRIVATE_URL)),
        httpx.DecodingError(PRIVATE_URL, request=httpx.Request("GET", PRIVATE_URL)),
        httpx.HTTPStatusError(
            PRIVATE_URL, request=httpx.Request("GET", PRIVATE_URL),
            response=httpx.Response(503),
        ),
    ],
    ids=lambda failure: type(failure).__name__,
)
def test_collect_day_isolates_known_failures_without_leaking_source_credentials(
    monkeypatch: pytest.MonkeyPatch, healthy_exchanges, failure,
) -> None:
    def fail(day: date):
        assert day == DAY
        raise failure

    monkeypatch.setitem(member_rank.FETCHERS, "GFEX", fail)
    results = member_rank.collect_day(DAY, exchanges=EXCHANGES)
    _assert_partial_results(results, healthy_exchanges, type(failure).__name__)


@pytest.mark.parametrize(
    "failure",
    [RuntimeError("programming bug"), ValueError("programming bug"),
     TypeError("programming bug"), KeyError("programming bug"),
     AttributeError("programming bug"), AssertionError("programming bug")],
    ids=lambda failure: type(failure).__name__,
)
def test_collect_day_propagates_unknown_programming_errors(
    monkeypatch: pytest.MonkeyPatch, healthy_exchanges, failure,
) -> None:
    def fail(day: date):
        raise failure

    monkeypatch.setitem(member_rank.FETCHERS, "GFEX", fail)
    with pytest.raises(type(failure)) as caught:
        member_rank.collect_day(DAY, exchanges=EXCHANGES)
    assert caught.value is failure
    assert healthy_exchanges[1] == ["CFFEX"]


@pytest.mark.parametrize("status", ["unavailable", "error"])
def test_collect_day_preserves_returned_status(
    monkeypatch: pytest.MonkeyPatch, healthy_exchanges, status: str,
) -> None:
    original = member_rank.MemberRankFetchResult(
        exchange="GFEX", trading_day=DAY, status=status,
        source_urls=(), note="fixture source status",
    )
    monkeypatch.setitem(member_rank.FETCHERS, "GFEX", lambda day: original)
    results = member_rank.collect_day(DAY, exchanges=EXCHANGES)
    assert results["GFEX"] is original
    for exchange, healthy in healthy_exchanges[0].items():
        assert results[exchange] is healthy


def test_collector_imports_healthy_results_and_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch, healthy_exchanges, capsys: pytest.CaptureFixture,
) -> None:
    path = Path(__file__).resolve().parents[1] / "scripts" / "collect_member_ranks.py"
    spec = importlib.util.spec_from_file_location("member_rank_collector_partial_test", path)
    assert spec is not None and spec.loader is not None
    collector = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(collector)
    imported = []
    recorded = []
    disposed = []
    settings = object()
    engine = SimpleNamespace(dispose=lambda: disposed.append(True))

    def create_engine(value):
        assert value is settings
        return engine

    def import_rows(value, rows):
        assert value is engine
        imported.append(rows)
        return SimpleNamespace(rows_written=len(rows))

    def record_evidence(directory, exchange, day, result):
        assert day == DAY
        recorded.append(result)
        return []

    def fail(day: date):
        assert day == DAY
        return json.loads("<html>synthetic GFEX failure</html>")

    monkeypatch.setitem(member_rank.FETCHERS, "GFEX", fail)
    monkeypatch.setattr(collector, "load_database_settings", lambda: settings)
    monkeypatch.setattr(collector, "create_database_engine", create_engine)
    monkeypatch.setattr(collector, "import_member_rank_rows", import_rows)
    monkeypatch.setattr(collector, "_save_evidence", record_evidence)
    code = collector.main([
        "--start", DAY.isoformat(), "--end", DAY.isoformat(),
        "--exchange", "CFFEX", "--exchange", "GFEX",
        "--exchange", "SHFE", "--exchange", "INE",
    ])
    assert code == 1
    expected = healthy_exchanges[0]
    assert imported == [expected[name].rows for name in ("CFFEX", "INE", "SHFE")]
    assert {result.exchange: result.status for result in recorded} == {
        "CFFEX": "ok", "GFEX": "error", "INE": "ok", "SHFE": "ok",
    }
    output = capsys.readouterr()
    assert output.err == ""
    lines = output.out.splitlines()
    gfex_record = next(json.loads(line) for line in lines[:-1] if '"GFEX"' in line)
    assert gfex_record["status"] == "error"
    assert gfex_record["rows"] == 0
    assert gfex_record["note"] == "member-rank fetch failed: JSONDecodeError"
    assert lines[-1].startswith("summary ")
    summary = json.loads(lines[-1].removeprefix("summary "))
    assert (summary["ok"], summary["unavailable"], summary["error"]) == (3, 0, 1)
    assert summary["imported_rows"] == 3
    assert disposed == [True]
