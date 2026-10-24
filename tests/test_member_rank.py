from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from ak_trading.member_rank import (
    METRIC_LONG,
    METRIC_SHORT,
    METRIC_VOLUME,
    MemberRankError,
    MemberRankParseError,
    collect_day,
    fetch_cffex_rank,
    fetch_gfex_rank,
    fetch_pm_rank,
    parse_cffex_xml,
    parse_czce_htm,
    parse_gfex_payload,
    parse_pm_payload,
)

DAY = date(2026, 8, 28)


def _pm_payload() -> dict:
    return {
        "o_msg": "查询成功",
        "o_code": "0000",
        "report_date": "20260828",
        "o_cursor": [
            {
                "INSTRUMENTID": "cuall",
                "PRODUCTNAME": "铜",
                "RANK": -1,
                "PARTICIPANTID1": "A1", "PARTICIPANTABBR1": "期货公司会员/境外特殊经纪参与者",
                "CJ1": 284306, "CJ1_CHG": -37721,
                "PARTICIPANTID2": "A1", "PARTICIPANTABBR2": "期货公司会员/境外特殊经纪参与者",
                "CJ2": 576859, "CJ2_CHG": 8119,
                "PARTICIPANTID3": "A1", "PARTICIPANTABBR3": "期货公司会员/境外特殊经纪参与者",
                "CJ3": 566407, "CJ3_CHG": 7563,
            },
            {
                "INSTRUMENTID": "cuall",
                "PRODUCTNAME": "铜",
                "RANK": 1,
                "PARTICIPANTID1": "0001", "PARTICIPANTABBR1": "中信期货(代客)",
                "CJ1": 22011, "CJ1_CHG": 1234,
                "PARTICIPANTID2": "0002", "PARTICIPANTABBR2": "国泰君安(代客)",
                "CJ2": 33011, "CJ2_CHG": -22,
                "PARTICIPANTID3": "0003", "PARTICIPANTABBR3": "东证期货(代客)",
                "CJ3": 44011, "CJ3_CHG": 0,
            },
            {
                "INSTRUMENTID": "cu2609",
                "PRODUCTNAME": "铜",
                "RANK": 999,
                "PARTICIPANTID1": "", "PARTICIPANTABBR1": "",
                "CJ1": 43954, "CJ1_CHG": -5498,
                "PARTICIPANTID2": "", "PARTICIPANTABBR2": "",
                "CJ2": 63588, "CJ2_CHG": -4804,
                "PARTICIPANTID3": "", "PARTICIPANTABBR3": "",
                "CJ3": 59915, "CJ3_CHG": -4446,
            },
        ],
    }


class TestParsePmPayload:
    def test_member_rows_map_to_three_metrics(self) -> None:
        rows = parse_pm_payload(
            _pm_payload(), exchange="SHFE", trading_day=DAY,
            source_url="https://www.shfe.com.cn/x.dat",
        )
        members = [r for r in rows if r.rank_no == 1 and r.instrument_id == "ALL"]
        assert {(r.metric, r.member_name, r.quantity) for r in members} == {
            (METRIC_VOLUME, "中信期货(代客)", 22011),
            (METRIC_LONG, "国泰君安(代客)", 33011),
            (METRIC_SHORT, "东证期货(代客)", 44011),
        }
        assert all(r.product_id == "cu" for r in rows)
        assert all(r.exchange == "SHFE" for r in rows)
        assert all(r.source_url.endswith("x.dat") for r in rows)

    def test_summary_rows_keep_labels(self) -> None:
        rows = parse_pm_payload(
            _pm_payload(), exchange="SHFE", trading_day=DAY, source_url="u"
        )
        summaries = [r for r in rows if r.instrument_id == "ALL" and r.rank_no == 0]
        assert {r.member_name for r in summaries} == {"期货公司会员/境外特殊经纪参与者"}
        totals = [
            r for r in rows
            if r.instrument_id == "cu2609" and r.rank_no == 0
        ]
        assert {r.member_name for r in totals} == {"合计"}
        assert {(r.metric, r.quantity) for r in totals} == {
            (METRIC_VOLUME, 43954),
            (METRIC_LONG, 63588),
            (METRIC_SHORT, 59915),
        }

    def test_negative_change_preserved(self) -> None:
        rows = parse_pm_payload(
            _pm_payload(), exchange="SHFE", trading_day=DAY, source_url="u"
        )
        volume_member = next(
            r for r in rows if r.member_name == "中信期货(代客)"
        )
        assert volume_member.change_qty == 1234
        long_member = next(r for r in rows if r.member_name == "国泰君安(代客)")
        assert long_member.change_qty == -22

    def test_missing_member_on_ranked_row_rejected(self) -> None:
        payload = _pm_payload()
        payload["o_cursor"][1]["PARTICIPANTABBR3"] = ""
        with pytest.raises(MemberRankParseError):
            parse_pm_payload(payload, exchange="SHFE", trading_day=DAY, source_url="u")

    def test_bad_quantity_rejected(self) -> None:
        payload = _pm_payload()
        payload["o_cursor"][1]["CJ1"] = "n/a"
        with pytest.raises(MemberRankParseError):
            parse_pm_payload(payload, exchange="SHFE", trading_day=DAY, source_url="u")

    def test_bad_rank_rejected(self) -> None:
        payload = _pm_payload()
        payload["o_cursor"][1]["RANK"] = 21
        with pytest.raises(MemberRankParseError):
            parse_pm_payload(payload, exchange="SHFE", trading_day=DAY, source_url="u")


_CFFEX_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<positionRank>
  <data Value="1" Text="IF2609"><instrumentid>IF2609</instrumentid><tradingday>20260828</tradingday><datatypeid>1</datatypeid><rank>1</rank><shortname>国泰君安(代客)</shortname><volume>103092</volume><varvolume>573</varvolume><partyid>0001</partyid><productid>IF</productid></data>
  <data Value="1" Text="IF2609"><instrumentid>IF2609</instrumentid><tradingday>20260828</tradingday><datatypeid>0</datatypeid><rank>2</rank><shortname>中信期货(代客)</shortname><volume>17212</volume><varvolume>-65</varvolume><partyid>0018</partyid><productid>IF</productid></data>
  <data Value="1" Text="IF2609"><instrumentid>IF2609</instrumentid><tradingday>20260828</tradingday><datatypeid>2</datatypeid><rank>3</rank><shortname>银河期货(代客)</shortname><volume>16287</volume><varvolume>-415</varvolume><partyid>0109</partyid><productid>IF</productid></data>
</positionRank>
"""


class TestParseCffexXml:
    def test_metric_mapping_and_basis(self) -> None:
        rows = parse_cffex_xml(_CFFEX_SAMPLE, trading_day=DAY, source_url="x.xml")
        by_rank = {(r.metric, r.rank_no): r for r in rows}
        assert by_rank[(METRIC_LONG, 1)].member_name == "国泰君安(代客)"
        assert by_rank[(METRIC_LONG, 1)].basis is None
        assert by_rank[(METRIC_VOLUME, 2)].basis == "double"
        assert by_rank[(METRIC_SHORT, 3)].member_id == "0109"
        assert all(r.product_id == "IF" for r in rows)
        assert by_rank[(METRIC_VOLUME, 2)].change_qty == -65

    def test_unknown_datatype_rejected(self) -> None:
        broken = _CFFEX_SAMPLE.replace(
            "<datatypeid>2</datatypeid>", "<datatypeid>9</datatypeid>"
        )
        with pytest.raises(MemberRankParseError):
            parse_cffex_xml(broken, trading_day=DAY, source_url="x.xml")

    def test_empty_document_rejected(self) -> None:
        with pytest.raises(MemberRankParseError):
            parse_cffex_xml("<positionRank></positionRank>", trading_day=DAY, source_url="x")


class TestParseGfexPayload:
    def _payload(self) -> dict:
        return {
            "code": "0",
            "data": [
                {"contractId": "1", "memberId": "0008", "abbr": "中信期货",
                 "todayQty": 5008, "qtySub": -1070},
                {"contractId": "2", "memberId": "0016", "abbr": "国泰君安期货",
                 "todayQty": 4800, "qtySub": 200},
                {"contractId": "总计", "memberId": "", "abbr": "",
                 "todayQty": 28510, "qtySub": -22060},
            ],
        }

    def test_ranks_and_total(self) -> None:
        rows = parse_gfex_payload(
            self._payload(), product_id="si", instrument_id="si2610",
            data_type="1", trading_day=DAY,
        )
        assert [r.rank_no for r in rows] == [1, 2, 0]
        assert rows[-1].member_name == "合计"
        assert rows[-1].quantity == 28510
        assert rows[0].change_qty == -1070
        assert all(r.metric == METRIC_VOLUME for r in rows)

    def test_unknown_data_type_rejected(self) -> None:
        with pytest.raises(MemberRankParseError):
            parse_gfex_payload(
                self._payload(), product_id="si", instrument_id="si2610",
                data_type="9", trading_day=DAY,
            )

    def test_empty_data_rejected(self) -> None:
        with pytest.raises(MemberRankParseError):
            parse_gfex_payload(
                {"code": "0", "data": []}, product_id="si", instrument_id="si2610",
                data_type="1", trading_day=DAY,
            )


_CZCE_SAMPLE = """<html><body>
<table>
<tr><td colspan="10"><b>品种：苹果AP&nbsp;&nbsp;&nbsp;&nbsp;日期：2026-08-28</b></td></tr>
<tr><td>名次</td><td>会员简称</td><td>交易量（手）</td><td>增减量</td><td>会员简称</td><td>持买仓量</td><td>增减量</td><td>会员简称</td><td>持卖仓量</td><td>增减量</td></tr>
<tr><td>1</td><td>国泰君安（代客）</td><td>56,306</td><td>30,970</td><td>国泰君安（代客）</td><td>16,020</td><td>3,527</td><td>国泰君安（代客）</td><td>18,630</td><td>-733</td></tr>
<tr><td>2</td><td>中信财富（代客）</td><td>49,199</td><td>19,616</td><td>中信建投（代客）</td><td>14,720</td><td>2,242</td><td>中信建投（代客）</td><td>11,313</td><td>-2,168</td></tr>
</table>
<table>
<tr><td colspan="10"><b>合约：AP610&nbsp;&nbsp;&nbsp;&nbsp;日期：2026-08-28</b></td></tr>
<tr><td>1</td><td>国泰君安（代客）</td><td>1,634</td><td>352</td><td>东证期货（代客）</td><td>815</td><td>93</td><td>海通期货（代客）</td><td>593</td><td>-13</td></tr>
</table>
<table>
<tr><td colspan="10"><b>品种：PTA&nbsp;&nbsp;&nbsp;&nbsp;日期：2026-08-28</b></td></tr>
<tr><td>1</td><td>永安期货（代客）</td><td>88,306</td><td>1,970</td><td>永安期货（代客）</td><td>26,020</td><td>527</td><td>南华期货（代客）</td><td>28,630</td><td>773</td></tr>
</table>
</body></html>"""


class TestParseCzceHtm:
    def test_product_and_contract_blocks(self) -> None:
        rows = parse_czce_htm(_CZCE_SAMPLE, trading_day=DAY)
        products = {r.product_id for r in rows}
        assert products == {"AP", "TA"}
        instruments = {r.instrument_id for r in rows}
        assert instruments == {"ALL", "AP610"}
        ap_all = [
            r for r in rows
            if r.product_id == "AP" and r.instrument_id == "ALL" and r.rank_no == 1
        ]
        assert {(r.metric, r.quantity) for r in ap_all} == {
            (METRIC_VOLUME, 56306),
            (METRIC_LONG, 16020),
            (METRIC_SHORT, 18630),
        }
        assert ap_all[0].change_qty == 30970
        short2 = next(
            r for r in rows
            if r.instrument_id == "ALL" and r.metric == METRIC_SHORT and r.rank_no == 2
        )
        assert short2.change_qty == -2168
        contract = [
            r for r in rows if r.instrument_id == "AP610" and r.rank_no == 1
        ]
        assert {(r.metric, r.member_name) for r in contract} == {
            (METRIC_VOLUME, "国泰君安（代客）"),
            (METRIC_LONG, "东证期货（代客）"),
            (METRIC_SHORT, "海通期货（代客）"),
        }

    def test_no_rows_rejected(self) -> None:
        with pytest.raises(MemberRankParseError):
            parse_czce_htm("<html><body><p>empty</p></body></html>", trading_day=DAY)


def _mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), timeout=10.0)


class TestFetchersOffline:
    def test_pm_rank_ok(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert "pm20260828.dat" in str(request.url)
            return httpx.Response(200, content=json.dumps(_pm_payload()).encode())

        result = fetch_pm_rank(
            DAY, exchange="SHFE", base_url="https://www.shfe.com.cn",
            client=_mock_client(handler),
        )
        assert result.status == "ok"
        assert len(result.rows) == 9
        assert list(result.evidence)

    def test_pm_rank_404_unavailable(self) -> None:
        result = fetch_pm_rank(
            DAY, exchange="SHFE", base_url="https://www.shfe.com.cn",
            client=_mock_client(lambda r: httpx.Response(404, text="none")),
        )
        assert result.status == "unavailable"
        assert not result.rows

    def test_pm_rank_server_error_raises(self) -> None:
        with pytest.raises(MemberRankError):
            fetch_pm_rank(
                DAY, exchange="SHFE", base_url="https://www.shfe.com.cn",
                client=_mock_client(lambda r: httpx.Response(503, text="down")),
            )

    def test_cffex_rank_aggregates_products(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("IF.xml"):
                return httpx.Response(200, text=_CFFEX_SAMPLE)
            return httpx.Response(404, text="none")

        result = fetch_cffex_rank(
            DAY, products=("IF", "T"), client=_mock_client(handler),
        )
        assert result.status == "ok"
        assert {r.product_id for r in result.rows} == {"IF"}
        assert len(result.source_urls) == 1

    def test_cffex_rank_all_missing_unavailable(self) -> None:
        result = fetch_cffex_rank(
            DAY, products=("IF",),
            client=_mock_client(lambda r: httpx.Response(404, text="none")),
        )
        assert result.status == "unavailable"

    def test_gfex_rank_full_chain(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "interfacesWebVariety" in url:
                return httpx.Response(200, json={
                    "code": "0",
                    "data": [{"varietyId": "si", "varietyName": "工业硅"}],
                })
            if "loadListContract_id" in url:
                return httpx.Response(200, json={"code": "0", "data": ["si2610", "si2611"]})
            from urllib.parse import parse_qs

            body = {
                key: values[0]
                for key, values in parse_qs(request.content.decode()).items()
            }
            if body["contract_id"] == "si2611":
                # below disclosure threshold: GFEX answers code '1' with no data
                return httpx.Response(200, json={"code": "1", "data": None})
            payload = {
                "code": "0",
                "data": [
                    {"contractId": "1", "memberId": "0008", "abbr": "中信期货",
                     "todayQty": 5008, "qtySub": -1070},
                    {"contractId": "总计", "memberId": "", "abbr": "",
                     "todayQty": 28510, "qtySub": -22060},
                ],
            }
            return httpx.Response(200, json=payload)

        result = fetch_gfex_rank(DAY, client=_mock_client(handler))
        assert result.status == "ok"
        contracts = {r.instrument_id for r in result.rows}
        assert contracts == {"si2610"}
        metrics = {r.metric for r in result.rows if r.instrument_id == "si2610"}
        assert metrics == {METRIC_VOLUME, METRIC_LONG, METRIC_SHORT}

    def test_collect_day_maps_fetch_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ak_trading import member_rank as module

        def boom(day):  # noqa: ANN001
            raise MemberRankError("waf throttled")

        monkeypatch.setitem(module.FETCHERS, "SHFE", boom)
        results = collect_day(DAY, exchanges=("SHFE",))
        assert results["SHFE"].status == "error"
        assert results["SHFE"].note == "member-rank fetch failed: MemberRankError"


class TestParseDceRecords:
    def _records(self) -> list[dict]:
        def member(rank: str, name: str, qty: int, chg: int) -> dict:
            return {"rank": rank, "qtyAbbr": name, "todayQty": qty, "qtySub": chg}

        data = {
            "contractId": "期货公司会员",
            "todayQty": 133593, "qtySub": -8431,
            "todayBuyQty": 66872, "buySub": -37141,
            "todaySellQty": 53143, "sellSub": -40100,
            "qtyFutureList": [
                member("1", "国泰君安（代客）", 21967, -7146),
                member("2", "中信期货（代客）", 15882, -3528),
                member("合计", "", 133593, -8431),
            ],
            "buyFutureList": [
                member("1", "永安期货（代客）", 9001, 101),
                member("合计", "", 66872, -37141),
            ],
            "sellFutureList": [
                member("1", "银河期货（代客）", 8002, -33),
                member("合计", "", 53143, -40100),
            ],
        }
        return [
            {"variety": "m", "name": "豆粕", "clicked": True,
             "responses": [json.dumps({"success": True, "data": data})]},
            {"variety": "y", "name": "豆油", "clicked": True, "responses": []},
        ]

    def test_three_metric_lists_parsed(self) -> None:
        from ak_trading.member_rank import parse_dce_records

        rows = parse_dce_records(self._records(), trading_day=DAY)
        assert all(r.exchange == "DCE" and r.product_id == "m" for r in rows)
        assert all(r.instrument_id == "ALL" for r in rows)
        by_metric = {}
        for r in rows:
            by_metric.setdefault(r.metric, []).append(r)
        assert by_metric[METRIC_VOLUME][0].member_name == "国泰君安（代客）"
        assert by_metric[METRIC_VOLUME][0].rank_no == 1
        assert by_metric[METRIC_LONG][0].quantity == 9001
        totals = [r for r in rows if r.rank_no == 0]
        assert {(r.metric, r.quantity) for r in totals} == {
            (METRIC_VOLUME, 133593),
            (METRIC_LONG, 66872),
            (METRIC_SHORT, 53143),
        }
        assert all(r.member_name == "合计" for r in totals)

    def test_no_data_raises(self) -> None:
        from ak_trading.member_rank import parse_dce_records

        with pytest.raises(MemberRankParseError):
            parse_dce_records(
                [{"variety": "m", "responses": []}], trading_day=DAY
            )


class TestMemberRankImport:

    def _rows(self) -> list:
        from ak_trading.member_rank import MemberRankRow

        return parse_pm_payload(
            _pm_payload(), exchange="SHFE", trading_day=DAY,
            source_url="https://www.shfe.com.cn/data/tradedata/future/dailydata/pm20260828.dat",
        ) + parse_cffex_xml(_CFFEX_SAMPLE, trading_day=DAY, source_url="http://cffex/IF.xml")

    def test_import_is_idempotent_and_upserts(self) -> None:
        sqlalchemy = pytest.importorskip("sqlalchemy")
        from ak_trading.persistence.config import load_test_database_settings
        from ak_trading.persistence.database import (
            create_database_engine,
            upgrade_database,
        )
        from ak_trading.persistence.member_rank import (
            MemberRankImportError,
            import_member_rank_rows,
        )
        from ak_trading.persistence.schema import member_position_ranks

        settings = load_test_database_settings()
        if settings is None:
            pytest.skip("AK_TEST_DATABASE_URL is not configured")
        upgrade_database(settings)
        engine = create_database_engine(settings)
        try:
            with engine.begin() as connection:
                from sqlalchemy import text

                connection.execute(
                    text("TRUNCATE TABLE member_position_ranks RESTART IDENTITY")
                )
            rows = self._rows()
            first = import_member_rank_rows(engine, rows)
            assert first.rows_received == len(rows)
            assert first.rows_written == len(rows)
            second = import_member_rank_rows(engine, rows)
            assert second.rows_written == len(rows)  # upsert, not duplicate
            with engine.begin() as connection:
                total = connection.execute(
                    sqlalchemy.select(sqlalchemy.func.count()).select_from(
                        member_position_ranks
                    )
                ).scalar_one()
            assert total == len(rows)
            # A corrected restatement updates in place instead of stacking rows.
            corrected = [type(rows[0])(**{**rows[0].__dict__, "quantity": 123})]
            import_member_rank_rows(engine, corrected)
            with engine.begin() as connection:
                total = connection.execute(
                    sqlalchemy.select(sqlalchemy.func.count()).select_from(
                        member_position_ranks
                    )
                ).scalar_one()
            assert total == len(rows)
        finally:
            engine.dispose()

    def test_empty_rows_rejected(self) -> None:
        pytest.importorskip("sqlalchemy")
        from ak_trading.persistence.member_rank import (
            MemberRankImportError,
            import_member_rank_rows,
        )

        with pytest.raises(MemberRankImportError):
            import_member_rank_rows(None, [])  # type: ignore[arg-type]
