import json
from datetime import date, datetime, timezone
from decimal import Decimal

from ak_trading.exchange_rule_extraction import (
    AUTO_EXTRACTION_NOTE,
    extract_rule_values,
    parse_labeled_spec_values,
)
from ak_trading.exchange_rule_sources import (
    DiscoveredDocument,
    ExchangeSource,
    ProductSource,
    collect_evidence,
    fill_required_unknowns,
)
from ak_trading.exchange_rules import (
    FetchedDocument,
    RuleCategory,
    RuleStatus,
    RuleValueKind,
    SourceDocument,
)


NOW = datetime(2026, 8, 20, 4, 0, tzinfo=timezone.utc)

# Copied from retained SHFE business-rule document
# 351735c37128ec804f8202ab43fa1f6a220dcdfcb0504459168f5695b69dabbf.
SHFE_RULE_EXCERPT = """\
**第七条** 铸造铝合金期货合约的交易时间为上午9:00~11:30 ，下午1:30~3:00和交易所规定的其他交易时间。

**第八条** 铸造铝合金期货合约的最后交易日为合约月份的15日，遇国家法定节假日顺延，春节月份等最后交易日交易所可另行调整并通知。

**第二十条** 铸造铝合金期货合约的交割日期为最后交易日后连续二个工作日。

**第三十条** 铸造铝合金期货合约的最低交易保证金为合约价值的5%。

**第三十二条** 铸造铝合金期货合约涨跌停板幅度为上一交易日结算价±3%。
"""

# Copied from retained INE notice
# 09d18791993c7bfdac3bd2206ed49b920459ab92a7bdaf5ffa13ddb11693963e.
INE_NOTICE_EXCERPT = """\
经研究决定，自2026年6月2日（星期二）收盘结算时起，涨跌停板幅度和交易保证金比例调整如下：

原油、低硫燃料油期货合约的涨跌停板幅度为17%，套保持仓交易保证金比例为18%，一般持仓交易保证金比例为19%。
"""

# Copied from retained INE fee notice
# 8c592a0490e1970f2ed59165ea910bc2cea95dd8d752a3333c8a8fdc16262a70.
INE_FEE_NOTICE_EXCERPT = (
    "原油期货sc2609、sc2610、sc2611、sc2612、sc2701、sc2702、sc2703、"
    "sc2706、sc2709、sc2712、sc2803、sc2806、sc2809、sc2812、sc2903"
    "合约交易手续费调整为20元/手，日内平今仓交易手续费调整为60元/手。"
)

# Copied from retained CFFEX 2-year treasury-futures page
# 09f43996e13a211b3dc78c9355b0cd137cc3266d3a7186b83e93e0e14e8821f2.
CFFEX_TABLE_EXCERPT = """\
| 2年期国债期货合约表 | |||
| 合约标的 | 面值为200万元人民币、票面利率为3%的名义中短期国债 | 每日价格最大波动限制 | 上一交易日结算价的±0.5% |
| 可交割国债 | 发行期限不高于5年，合约到期月份首日剩余期限为1.5-2.25年的记账式附息国债 | 最低交易保证金 | 合约价值的0.5% |
| 报价方式 | 百元净价报价 | 最后交易日 | 合约到期月份的第二个星期五 |
| 最小变动价位 | 0.002元 | 最后交割日 | 最后交易日后的第三个交易日 |
| 合约月份 | 最近的三个季月（3月、6月、9月、12月中的最近三个月循环） | 交割方式 | 实物交割 |
| 交易时间 | 9:30 - 11:30, 13:00 - 15:15 | 交易代码 | TS |
| 最后交易日交易时间 | 9:30 - 11:30 | 上市交易所 | 中国金融期货交易所 |
"""

# Copied from retained CFFEX homepage markdown
# 05ebf8b6b66e3e33a3c3a51ad481696d4df175768dc1c2ed442814a951757917.
CFFEX_HOMEPAGE_EXCERPT = """\
[第十二届“中金所杯”全国大学生金融知识大赛正式启动](http://www.cffex.com.cn/cn/jysdt/20260807/48440.html)2026-08-07

**一、比赛名称**

第十二届“中金所杯”全国大学生金融知识大赛

**三、比赛时间**

报名时间：即日起至2026年10月31日

初赛时间：2026年9月10日-11月20日
"""

# Copied from retained GFEX notice
# d54e8592a3d37136b9d4a110a0835d054e725de8d2f22c7dd63c1dbf5a51d1b6.
GFEX_NOTICE_EXCERPT = """\
广期所发〔2026〕38号

各会员单位：

经研究决定，自2026年2月5日结算时起，铂、钯期货合约涨跌停板幅度调整为20%，交易保证金标准调整为22%。

如遇上述涨跌停板幅度、交易保证金标准与现行执行的涨跌停板幅度、交易保证金标准不同时，则按两者中幅度大、标准高的执行。
"""

# Copied from a retained CZCE navigation/WAF response
# 0337df11763070389bf727ac244865fe1809ece569bf43ea94e1e9525bbc725a.
CZCE_NAV_EXCERPT = (
    "$_ts=window['$_ts'];if(!$_ts)$_ts={};$_ts.nsd=80401;\n"
    '$_ts.cd="qhfSr1qlE13ktkgllaqfkfAbqk3ElaqbrP3utkWsqclim13lxGVMkrAlmSWqx'
    'GQbrcGulaqbqP3ktqQsqc3rxG3LWSWqxGA";\n'
    "if($_ts.lcd)$_ts.lcd();\n"
)

PLACEHOLDER_EXCERPT = """\
| 合约乘数 | {{ pageList[0].ContractMultiplier }} |
| 交易时间 | {{ pageList[0].TradingHours }} |
| 涨跌停板幅度 | {{ pageList[0].RangeofPriceLimit }} |
| 最低交易保证金 | {{ pageList[0].MinimumTradeMargin }} |
| 最后交易日 | {{ pageList[0].LastTradingDay }} |
"""


def _document(
    markdown: str,
    *,
    exchange: str = "SHFE",
    url: str = "https://www.shfe.com.cn/retained/rules.html",
    title: str = "Retained official rules",
) -> SourceDocument:
    content = json.dumps(
        {
            "schema_version": 1,
            "retrieval_method": "local_websearch_scrape",
            "source_url": url,
            "title": title,
            "markdown": markdown,
        },
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    fetched = FetchedDocument(
        url=url,
        title=title,
        content=content,
        content_type="application/vnd.ak-trading.websearch-scrape+json",
        retrieved_at=NOW,
    )
    return SourceDocument.from_fetched(
        fetched,
        exchange=exchange,
        content_path="documents/fixture.json",
    )


def _by_identity(document: SourceDocument, product: str, product_name=None):
    return {
        item.identity: item
        for item in extract_rule_values(
            (document,), product=product, product_name=product_name
        )
    }


# Copied from the live CFFEX settlement parameter CSV
# http://www.cffex.com.cn/sj/jscs/202607/20/20260720_1.csv (fetched 2026-08-21).
CFFEX_CSV_EXCERPT = """\
期货合约结算业务参数表（20260720）
期货合约,合约多头保证金标准,合约空头保证金标准,交易手续费标准,交割手续费标准,平今仓收取率
IC2608                        ,12.00%,12.00%,万分之0.23,万分之0.5,1000%
IC2609                        ,12.00%,12.00%,万分之0.23,万分之0.5,1000%
IF2608                        ,10.00%,10.00%,万分之0.23,万分之0.5,1000%
"""

# Copied from the humanlike fetch of the live DCE parameter page
# http://www.dce.com.cn/dalianshangpin/ywfw/ywcs/jycs/2082281/index.html.
DCE_MATRIX_EXCERPT = """\
品种/资讯
玉米
黄大豆1号
黄大豆2号
豆粕
豆油
交易保证金比例（投机）
7%
7%
7%
7%
7%
交易保证金比例（套保）
7%
7%
7%
7%
7%
涨跌停板幅度
4%
4%
4%
4%
4%
交易手续费（元/手）
合约规定
2
1
1.5
1.2
"""


# Copied from the official INE fee schedule XLSX cell (fetched 2026-08-21).
INE_FEE_ROW = (
    "1\t期货交易/交割手续费、期权交易/行权手续费、资产作为保证金的手续费"
    "\t期货交易手续费\t《期货交易管理条例》\t会员"
    "\t原油20元/手；\n20号胶0.02‰；\n低硫燃料油0.01‰；\n铜（BC）0.01‰；"
    "\n集运指数（欧线）0.6‰；\n部分合约手续费执行标准的特别规定详见公告通知和结算参数表。\n"
)


# Copied from https://www.shfe.com.cn/data/tradedata/future/dailydata/js20260903.dat
# (fetched 2026-09-04): the daily settlement-parameter JSON behind no WAF.
SHFE_DAILY_PARAMETER_JSON = json.dumps(
    {
        "showlength": "1",
        "o_msg": "查询成功",
        "o_cursor": [
            {
                "INSTRUMENTID": "cu2609",
                "TRADEFEERATIO": 0.05,
                "TTRADEFEERATIO": 0.025,
                "COMMODITYDELIVFEEUNIT": 0,
                "SPECLONGMARGINRATIO": 0.15,
                "HEDGLONGMARGINRATIO": 0.15,
                "COMMODITYDELIVFEERATIO": 0,
                "PRODUCTID": "cu_f",
                "PRODUCTNAME": "铜",
                "TTRADEFEEUNIT": 0,
                "TRADEFEEUNIT": 0,
                "HEDGSHORTMARGINRATIO": 0.15,
                "SETTLEMENTPRICE": 109070,
                "UNIDIRECT": "1",
                "SPECSHORTMARGINRATIO": 0.15,
                "ISUNITODAY": 0,
            },
            {
                "INSTRUMENTID": "cu2701",
                "SPECLONGMARGINRATIO": 0.15,
                "SPECSHORTMARGINRATIO": 0.15,
                "TRADEFEEUNIT": 0,
                "TTRADEFEEUNIT": 0,
                "PRODUCTID": "cu_f",
            },
            {
                "INSTRUMENTID": "sc2610",
                "SPECLONGMARGINRATIO": 0.16,
                "SPECSHORTMARGINRATIO": 0.16,
                "TRADEFEEUNIT": 20,
                "TTRADEFEEUNIT": 10,
                "PRODUCTID": "sc_f",
            },
        ],
    },
    ensure_ascii=False,
)


def test_shfe_daily_parameter_json_extracts_margin_and_unit_fees():
    document = _document(
        SHFE_DAILY_PARAMETER_JSON,
        exchange="SHFE",
        url="https://www.shfe.com.cn/data/tradedata/future/dailydata/js20260903.dat",
    )
    values = _by_identity(document, "CU")

    margin = values[(RuleCategory.MARGIN, "ordinary_rate")]
    assert margin.value == Decimal("0.15")
    assert margin.unit == "rate_of_notional"
    assert margin.source_class == "parameter_table"
    assert "SPECLONGMARGINRATIO" in margin.excerpt
    # cu fees are ratio-based with no published basis: no fabricated fee.
    assert (RuleCategory.FEE, "open") not in values

    # A different product in the same file maps to its own rows only.
    assert (RuleCategory.MARGIN, "ordinary_rate") not in _by_identity(document, "RB")


def test_ine_daily_parameter_json_extracts_unit_fees_per_product():
    document = _document(
        SHFE_DAILY_PARAMETER_JSON,
        exchange="INE",
        url="https://www.ine.cn/data/tradedata/future/dailydata/js20260903.dat",
    )
    values = _by_identity(document, "SC")

    assert values[(RuleCategory.MARGIN, "ordinary_rate")].value == Decimal("0.16")
    fee_open = values[(RuleCategory.FEE, "open")]
    assert fee_open.value == Decimal("20")
    assert fee_open.unit == "cny_per_lot"
    assert values[(RuleCategory.FEE, "close_today")].value == Decimal("10")


def test_daily_parameter_instrument_escalation_needs_a_majority():
    escalated = json.dumps(
        {
            "o_cursor": [
                {
                    "INSTRUMENTID": "rb2610",
                    "SPECLONGMARGINRATIO": 0.13,
                    "SPECSHORTMARGINRATIO": 0.13,
                    "PRODUCTID": "rb_f",
                },
                {
                    "INSTRUMENTID": "rb2701",
                    "SPECLONGMARGINRATIO": 0.09,
                    "SPECSHORTMARGINRATIO": 0.09,
                    "PRODUCTID": "rb_f",
                },
                {
                    "INSTRUMENTID": "rb2705",
                    "SPECLONGMARGINRATIO": 0.09,
                    "SPECSHORTMARGINRATIO": 0.09,
                    "PRODUCTID": "rb_f",
                },
            ]
        }
    )
    document = _document(
        escalated,
        exchange="SHFE",
        url="https://www.shfe.com.cn/data/tradedata/future/dailydata/js20260903.dat",
    )
    values = _by_identity(document, "RB")
    assert values[(RuleCategory.MARGIN, "ordinary_rate")].value == Decimal("0.09")

    tied = escalated.replace("0.09", "0.08", 1)
    tied_document = _document(
        tied,
        exchange="SHFE",
        url="https://www.shfe.com.cn/data/tradedata/future/dailydata/js20260903.dat",
    )
    tied_values = _by_identity(tied_document, "RB")
    # A three-way tie fabricates nothing; the field stays with other evidence.
    assert (RuleCategory.MARGIN, "ordinary_rate") not in tied_values


def test_ine_fee_sheet_extracts_per_product_fees():
    document = _document(
        INE_FEE_ROW,
        exchange="INE",
        url="https://www.ine.cn/reports/businessdata/feeandcharges/202604/W020260427606255890773.xlsx",
    )

    sc = _by_identity(document, "SC", product_name="原油")
    fee = sc[(RuleCategory.FEE, "open")]
    assert fee.value == Decimal("20")
    assert fee.unit == "cny_per_lot"
    assert sc[(RuleCategory.FEE, "close")].value == Decimal("20")

    ec = _by_identity(document, "EC", product_name="集运指数（欧线）")
    assert ec[(RuleCategory.FEE, "open")].value == Decimal("0.0006")
    assert ec[(RuleCategory.FEE, "open")].unit == "rate_per_notional"

    bc = _by_identity(document, "BC", product_name="国际铜")
    assert bc[(RuleCategory.FEE, "open")].value == Decimal("0.00001")


def test_cffex_parameter_csv_extracts_margin_and_all_fee_directions():
    document = _document(
        CFFEX_CSV_EXCERPT,
        exchange="CFFEX",
        url="http://www.cffex.com.cn/sj/jscs/202607/20/20260720_1.csv",
    )
    values = _by_identity(document, "IC")

    margin = values[(RuleCategory.MARGIN, "ordinary_rate")]
    assert margin.value == Decimal("0.12")
    assert margin.unit == "rate_of_notional"
    assert "多头 12.00% 空头 12.00%" in margin.excerpt

    fee_open = values[(RuleCategory.FEE, "open")]
    assert fee_open.value == Decimal("0.000023")
    assert fee_open.unit == "rate_per_notional"

    fee_close = values[(RuleCategory.FEE, "close")]
    assert fee_close.value == Decimal("0.000023")

    close_today = values[(RuleCategory.FEE, "close_today")]
    assert close_today.value == Decimal("0.00023")
    assert "1000%" in close_today.excerpt

    assert (RuleCategory.FEE, "open") not in _by_identity(document, "T")


def test_dce_matrix_page_maps_values_by_product_name():
    document = _document(
        DCE_MATRIX_EXCERPT,
        exchange="DCE",
        url="http://www.dce.com.cn/dalianshangpin/ywfw/ywcs/jycs/2082281/index.html",
    )
    values = _by_identity(document, "M", product_name="豆粕")

    margin = values[(RuleCategory.MARGIN, "ordinary_rate")]
    assert margin.value == Decimal("0.07")
    assert "（投机档）" in margin.excerpt

    limit = values[(RuleCategory.LIMIT, "price_and_position_limits")]
    assert limit.value == Decimal("0.04")
    assert limit.unit == "rate_of_previous_settlement"

    fee_open = values[(RuleCategory.FEE, "open")]
    assert fee_open.value == Decimal("1.5")
    assert fee_open.unit == "cny_per_lot"

    # Without the product name the matrix cannot be mapped.
    assert (RuleCategory.MARGIN, "ordinary_rate") not in _by_identity(document, "M")

    # 玉米 has "合约规定" for fees: no value, no fabrication.
    corn = _by_identity(document, "C", product_name="玉米")
    assert (RuleCategory.MARGIN, "ordinary_rate") in corn
    assert (RuleCategory.FEE, "open") not in corn


def test_parameter_table_supersedes_contract_text_and_keeps_evidence():
    contract = _document(
        "中证500股指期货合约的最低交易保证金为合约价值的8%。",
        exchange="CFFEX",
        url="http://www.cffex.com.cn/contract.html",
    )
    parameter = _document(
        CFFEX_CSV_EXCERPT,
        exchange="CFFEX",
        url="http://www.cffex.com.cn/sj/jscs/202607/20/20260720_1.csv",
    )

    values = {
        item.identity: item
        for item in extract_rule_values(
            (contract, parameter), product="IC", target_day=date(2026, 8, 20)
        )
    }
    margin = values[(RuleCategory.MARGIN, "ordinary_rate")]

    assert margin.value == Decimal("0.12")
    assert margin.source_class == "parameter_table"
    assert "supersedes contract-text" in margin.note
    assert "最低交易保证金为合约价值的8%" in margin.note
    assert margin.effective_date == date(2026, 7, 20)


def test_disagreeing_parameter_tables_stay_review_required():
    first = _document(
        CFFEX_CSV_EXCERPT,
        exchange="CFFEX",
        url="http://www.cffex.com.cn/sj/jscs/202607/20/20260720_1.csv",
    )
    second = _document(
        CFFEX_CSV_EXCERPT.replace("12.00%", "15.00%"),
        exchange="CFFEX",
        url="http://www.cffex.com.cn/sj/jscs/202608/19/20260819_1.csv",
    )

    values = {
        item.identity: item
        for item in extract_rule_values((first, second), product="IC")
    }
    margin = values[(RuleCategory.MARGIN, "ordinary_rate")]

    assert margin.status is RuleStatus.REVIEW_REQUIRED
    assert margin.value is None
    assert "12.00%" in margin.excerpt and "15.00%" in margin.excerpt


def test_newer_dated_notice_blocks_parameter_supersession():
    contract = _document(
        "自2026年8月1日起，中证500股指期货合约最低交易保证金标准为合约价值的10%。",
        exchange="CFFEX",
        url="http://www.cffex.com.cn/notice.html",
    )
    parameter = _document(
        CFFEX_CSV_EXCERPT,
        exchange="CFFEX",
        url="http://www.cffex.com.cn/sj/jscs/202607/20/20260720_1.csv",
    )

    values = {
        item.identity: item
        for item in extract_rule_values(
            (contract, parameter), product="IC", target_day=date(2026, 8, 20)
        )
    }
    margin = values[(RuleCategory.MARGIN, "ordinary_rate")]

    assert margin.value == Decimal("0.10")
    assert "supersedes" not in margin.note
    assert margin.effective_date == date(2026, 8, 1)


def test_real_shfe_prose_extracts_margin_limit_session_and_delivery():
    document = _document(SHFE_RULE_EXCERPT)
    values = _by_identity(document, "AD")

    margin = values[(RuleCategory.MARGIN, "ordinary_rate")]
    assert margin.value == Decimal("0.05")
    assert margin.unit == "rate_of_notional"
    assert margin.excerpt == (
        "**第三十条** 铸造铝合金期货合约的最低交易保证金为合约价值的5%。"
    )

    price_limit = values[(RuleCategory.LIMIT, "price_and_position_limits")]
    assert price_limit.value == Decimal("0.03")
    assert price_limit.unit == "rate_of_previous_settlement"

    session = values[(RuleCategory.SESSION, "trading_sessions")]
    assert isinstance(session.value, str)
    assert "上午9:00~11:30" in session.value

    delivery = values[(RuleCategory.DELIVERY, "delivery_and_closeout")]
    assert isinstance(delivery.value, str)
    assert "最后交易日为合约月份的15日" in delivery.value
    assert "交割日期为最后交易日后连续二个工作日" in delivery.value
    assert all(item.status is RuleStatus.CANDIDATE for item in values.values())
    assert all(item.note == AUTO_EXTRACTION_NOTE for item in values.values())


def test_real_ine_and_gfex_notices_extract_only_unambiguous_rates():
    ine = _by_identity(
        _document(
            INE_NOTICE_EXCERPT,
            exchange="INE",
            url="https://www.ine.cn/publicnotice/notice/202605/rates.html",
        ),
        "SC",
    )
    assert ine[(RuleCategory.MARGIN, "ordinary_rate")].value == Decimal("0.19")
    assert ine[(RuleCategory.LIMIT, "price_and_position_limits")].value == Decimal(
        "0.17"
    )

    gfex = _by_identity(
        _document(
            GFEX_NOTICE_EXCERPT,
            exchange="GFEX",
            url="https://www.gfex.com.cn/gfex/tzts/202602/rates.shtml",
        ),
        "PT",
    )
    assert gfex[(RuleCategory.MARGIN, "ordinary_rate")].value == Decimal("0.22")
    assert gfex[(RuleCategory.LIMIT, "price_and_position_limits")].value == Decimal(
        "0.20"
    )
    assert not any(category is RuleCategory.FEE for category, _ in gfex)


def test_real_ine_fee_notice_does_not_infer_open_or_close_from_generic_fee():
    values = _by_identity(
        _document(
            INE_FEE_NOTICE_EXCERPT,
            exchange="INE",
            url="https://www.ine.cn/publicnotice/notice/202605/fees.html",
        ),
        "SC",
    )

    assert (RuleCategory.FEE, "open") not in values
    assert (RuleCategory.FEE, "close") not in values
    close_today = values[(RuleCategory.FEE, "close_today")]
    assert close_today.value == Decimal("60")
    assert close_today.unit == "cny_per_lot"


def test_cffex_pair_rows_extract_both_sides_and_ignore_longer_session_label():
    values = _by_identity(
        _document(
            CFFEX_TABLE_EXCERPT,
            exchange="CFFEX",
            url="http://www.cffex.com.cn/cn/2ts.html",
            title="2年期国债期货",
        ),
        "TS",
    )

    assert values[(RuleCategory.MARGIN, "ordinary_rate")].value == Decimal("0.005")
    assert values[(RuleCategory.LIMIT, "price_and_position_limits")].value == Decimal(
        "0.005"
    )
    session = values[(RuleCategory.SESSION, "trading_sessions")]
    assert session.value == "9:30 - 11:30, 13:00 - 15:15"
    assert "最后交易日交易时间" not in session.excerpt
    delivery = values[(RuleCategory.DELIVERY, "delivery_and_closeout")]
    assert "最后交易日后的第三个交易日" in delivery.value
    assert "实物交割" in delivery.value


def test_placeholders_homepage_and_czce_navigation_do_not_extract_values():
    placeholder = _document(PLACEHOLDER_EXCERPT)
    assert parse_labeled_spec_values(PLACEHOLDER_EXCERPT, product="SC") == ()
    assert extract_rule_values((placeholder,), product="SC") == ()

    homepage = _document(
        CFFEX_HOMEPAGE_EXCERPT,
        exchange="CFFEX",
        url="http://www.cffex.com.cn/cn/index.html",
    )
    assert extract_rule_values((homepage,), product="IF") == ()

    navigation = _document(
        CZCE_NAV_EXCERPT,
        exchange="CZCE",
        url="https://www.czce.com.cn/cn/navigation.htm",
    )
    assert parse_labeled_spec_values(CZCE_NAV_EXCERPT, product="TA") == ()
    completed = fill_required_unknowns(
        exchange="CZCE",
        product="TA",
        contract=None,
        items=(),
        documents=(navigation,),
    )
    assert len(completed) == 9
    assert all(item.status is RuleStatus.REVIEW_REQUIRED for item in completed)
    assert all(item.value_kind is RuleValueKind.UNKNOWN for item in completed)


def test_conflicting_documents_remain_review_required_and_list_both_excerpts():
    first_excerpt = "| 最低交易保证金 | 合约价值的5% |"
    second_excerpt = "| 最低交易保证金 | 合约价值的8% |"
    first = _document(first_excerpt, url="https://www.shfe.com.cn/first.html")
    second = _document(second_excerpt, url="https://www.shfe.com.cn/second.html")

    decisions = {
        item.identity: item
        for item in extract_rule_values((first, second), product="RB")
    }
    conflict = decisions[(RuleCategory.MARGIN, "ordinary_rate")]
    assert conflict.status is RuleStatus.REVIEW_REQUIRED
    assert conflict.value_kind is RuleValueKind.UNKNOWN
    assert conflict.value is None
    assert first_excerpt in conflict.note
    assert second_excerpt in conflict.note
    assert AUTO_EXTRACTION_NOTE not in conflict.note

    completed = fill_required_unknowns(
        exchange="SHFE",
        product="RB",
        contract=None,
        items=(),
        documents=(first, second),
    )
    margin = next(
        item
        for item in completed
        if item.category is RuleCategory.MARGIN and item.field == "ordinary_rate"
    )
    assert margin.status is RuleStatus.REVIEW_REQUIRED
    assert margin.value_kind is RuleValueKind.UNKNOWN
    assert first_excerpt in (margin.note or "")
    assert second_excerpt in (margin.note or "")


def test_dated_conflict_selects_latest_effective_value_for_target_day():
    first_excerpt = "| 最低交易保证金 | 合约价值的5% |"
    second_excerpt = "| 最低交易保证金 | 合约价值的8% |"
    first = _document(
        "公告自2026年6月1日起执行。\n" + first_excerpt,
        url="https://www.shfe.com.cn/dated-first.html",
        title="保证金通知",
    )
    second = _document(
        "经研究决定，自2026年8月1日起执行。\n" + second_excerpt,
        url="https://www.shfe.com.cn/dated-second.html",
        title="保证金调整通知",
    )

    decisions = {
        item.identity: item
        for item in extract_rule_values(
            (first, second), product="RB", target_day=date(2026, 8, 20)
        )
    }
    margin = decisions[(RuleCategory.MARGIN, "ordinary_rate")]
    assert margin.status is RuleStatus.CANDIDATE
    assert margin.value == Decimal("0.08")
    assert margin.effective_date == date(2026, 8, 1)
    assert "effective_on=2026-08-01" in margin.note
    assert first_excerpt in margin.note
    assert second_excerpt in margin.note


def test_dated_conflict_with_only_future_values_stays_review_required():
    first = _document(
        "公告自2026年9月1日起执行。\n| 最低交易保证金 | 合约价值的5% |",
        url="https://www.shfe.com.cn/future-first.html",
    )
    second = _document(
        "公告自2026年10月1日起执行。\n| 最低交易保证金 | 合约价值的8% |",
        url="https://www.shfe.com.cn/future-second.html",
    )
    decisions = {
        item.identity: item
        for item in extract_rule_values(
            (first, second), product="RB", target_day=date(2026, 8, 20)
        )
    }
    margin = decisions[(RuleCategory.MARGIN, "ordinary_rate")]
    assert margin.status is RuleStatus.REVIEW_REQUIRED
    assert margin.value_kind is RuleValueKind.UNKNOWN
    assert "effective_on=2026-09-01" in margin.note
    assert "effective_on=2026-10-01" in margin.note


def test_unit_parser_handles_full_width_percent_cny_per_ton_and_lots():
    markdown = (
        "| 最低交易保证金 | 合约价值的5％ | 最小变动价位 | 5元每吨 | "
        "限仓 | 不超过3,000手 | 交易单位 | 10吨/手 |"
    )
    values = {item.label: item for item in parse_labeled_spec_values(markdown)}

    assert values["最低交易保证金"].value == Decimal("0.05")
    assert values["最低交易保证金"].unit == "ratio"
    assert values["最小变动价位"].value == Decimal("5")
    assert values["最小变动价位"].unit == "cny_per_ton"
    assert values["限仓"].value == Decimal("3000")
    assert values["限仓"].unit == "lot"
    assert values["交易单位"].value == Decimal("10")
    assert values["交易单位"].unit == "ton_per_lot"


# Copied from the retained CZCE notice
# 3f98c6796d14207e2bb3a634d27a6ece3fe333a3c16c7d2d5c622790c465dd27.
CZCE_MULTI_PRODUCT_NOTICE = (
    "一、自2026年6月5日结算时起，玻璃期货合约的交易保证金标准调整为9%，"
    "涨跌停板幅度调整为8%，其中，玻璃期货2606合约的涨跌停板幅度仍为9%；"
    "纯碱期货合约的交易保证金标准调整为8%，涨跌停板幅度调整为7%。"
)

CZCE_NAMES = ("玻璃", "纯碱", "甲醇", "PTA", "烧碱")


def test_multi_product_notice_clause_maps_each_product_to_its_own_value():
    document = _document(
        CZCE_MULTI_PRODUCT_NOTICE,
        exchange="CZCE",
        url="https://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2026/6/notice.htm",
    )
    fg = {
        item.identity: item
        for item in extract_rule_values(
            (document,),
            product="FG",
            product_name="玻璃",
            product_names=CZCE_NAMES,
        )
    }
    sa = {
        item.identity: item
        for item in extract_rule_values(
            (document,),
            product="SA",
            product_name="纯碱",
            product_names=CZCE_NAMES,
        )
    }
    assert fg[(RuleCategory.MARGIN, "ordinary_rate")].value == Decimal("0.09")
    assert sa[(RuleCategory.MARGIN, "ordinary_rate")].value == Decimal("0.08")
    assert fg[(RuleCategory.LIMIT, "price_and_position_limits")].value == Decimal("0.08")
    assert sa[(RuleCategory.LIMIT, "price_and_position_limits")].value == Decimal("0.07")
    # An unlisted product must not inherit any clause value.
    ma = {
        item.identity: item
        for item in extract_rule_values(
            (document,),
            product="MA",
            product_name="甲醇",
            product_names=CZCE_NAMES,
        )
    }
    assert (RuleCategory.MARGIN, "ordinary_rate") not in ma


def test_notice_clause_for_other_product_is_rejected():
    # Retained DCE notice 9506814b… is a 豆粕 (M) adjustment wrongly discovered
    # for product A; the clause must not leak its 7% into A.
    notice = (
        "经中国证监会批准，自二〇〇一年七月二日起，豆粕合约的最低交易保证金比例"
        "调整为合约价值的7%，涨跌停板幅度调整为3%。"
    )
    document = _document(
        notice,
        exchange="DCE",
        url="http://www.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/1986962/index.html",
    )
    dce_names = ("黄大豆1号", "黄大豆2号", "豆粕", "豆油", "玉米")
    soybean_one = {
        item.identity: item
        for item in extract_rule_values(
            (document,),
            product="A",
            product_name="黄大豆1号",
            product_names=dce_names,
        )
    }
    meal = {
        item.identity: item
        for item in extract_rule_values(
            (document,),
            product="M",
            product_name="豆粕",
            product_names=dce_names,
        )
    }
    assert (RuleCategory.MARGIN, "ordinary_rate") not in soybean_one
    assert meal[(RuleCategory.MARGIN, "ordinary_rate")].value == Decimal("0.07")


def test_contract_month_clause_is_not_the_product_ordinary_margin():
    notice = (
        "自2026年4月29日收盘结算时起，烧碱期货合约的交易保证金标准为12%；"
        "烧碱期货2605合约的交易保证金标准为15%，烧碱期货2606及2607合约的"
        "交易保证金标准为13%。"
    )
    document = _document(
        notice,
        exchange="CZCE",
        url="https://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2026/4/caustic.htm",
    )
    values = {
        item.identity: item
        for item in extract_rule_values(
            (document,),
            product="SH",
            product_name="烧碱",
            product_names=CZCE_NAMES,
        )
    }
    margin = values[(RuleCategory.MARGIN, "ordinary_rate")]
    assert margin.value == Decimal("0.12")


def test_own_name_inside_longer_peer_name_is_not_a_mention():
    notice = "自2026年1月28日起，氧化铝期货合约的交易保证金标准调整为15%。"
    document = _document(
        notice,
        exchange="SHFE",
        url="https://www.shfe.com.cn/publicnotice/notice/202601/ao.html",
    )
    shfe_names = ("铜", "铝", "氧化铝", "铸造铝合金")
    aluminium = extract_rule_values(
        (document,),
        product="AL",
        product_name="铝",
        product_names=shfe_names,
    )
    alumina = extract_rule_values(
        (document,),
        product="AO",
        product_name="氧化铝",
        product_names=shfe_names,
    )
    assert not any(
        item.category is RuleCategory.MARGIN for item in aluminium
    )
    assert any(
        item.category is RuleCategory.MARGIN and item.value == Decimal("0.15")
        for item in alumina
    )


def test_two_product_clause_keeps_both_listed_products():
    notice = (
        "经研究决定，自2026年6月2日收盘结算时起，原油、低硫燃料油期货合约的"
        "涨跌停板幅度为17%，一般持仓交易保证金比例为19%。"
    )
    document = _document(
        notice,
        exchange="INE",
        url="https://www.ine.cn/publicnotice/notice/202606/rates.html",
    )
    ine_names = ("原油", "低硫燃料油", "20号胶", "国际铜", "集运指数（欧线）")
    for product, name in (("SC", "原油"), ("LU", "低硫燃料油")):
        values = {
            item.identity: item
            for item in extract_rule_values(
                (document,),
                product=product,
                product_name=name,
                product_names=ine_names,
            )
        }
        assert values[(RuleCategory.MARGIN, "ordinary_rate")].value == Decimal("0.19")


def test_reversion_notice_does_not_set_standing_margin():
    holiday_notice = (
        "自2026年4月29日收盘结算时起，玻璃和纯碱期货合约的交易保证金标准为14%，"
        "涨跌停板幅度为12%。\n"
        "5月6日恢复交易后，自品种持仓量最大的合约未出现涨跌停板单边市的第一个"
        "交易日结算时起，其他品种期货合约的交易保证金标准和涨跌停板幅度恢复至调整前水平。"
    )
    document = _document(
        holiday_notice,
        exchange="CZCE",
        url="https://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2026/4/holiday.htm",
    )
    values = {
        item.identity: item
        for item in extract_rule_values(
            (document,),
            product="FG",
            product_name="玻璃",
            product_names=CZCE_NAMES,
        )
    }
    assert (RuleCategory.MARGIN, "ordinary_rate") not in values
    assert (RuleCategory.LIMIT, "price_and_position_limits") not in values


def test_aggregate_page_with_differing_labeled_margins_yields_no_margin():
    aggregate = (
                "最低交易保证金\n合约价值的5%\n"
        "……\n最低交易保证金\n合约价值的5％\n"
        "……\n最低交易保证金\n合约价值的7%\n"
    )
    document = _document(
        aggregate,
        exchange="CZCE",
        url="https://www.czce.com.cn/cn/sspz/overview.htm",
    )
    assert extract_rule_values((document,), product="CF", product_name="棉花") == ()


def test_dated_notice_supersedes_undated_contract_text():
    contract = _document(
        "第三十六条 玻璃期货合约最低交易保证金标准为合约价值的5%。",
        exchange="CZCE",
        url="https://www.czce.com.cn/cn/flfg/zcjywgz/lsbb/webinfo/2024/01/old.htm",
    )
    notice = _document(
        CZCE_MULTI_PRODUCT_NOTICE,
        exchange="CZCE",
        url="https://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2026/6/notice.htm",
    )
    values = {
        item.identity: item
        for item in extract_rule_values(
            (contract, notice),
            product="FG",
            product_name="玻璃",
            product_names=CZCE_NAMES,
            target_day=date(2026, 9, 3),
        )
    }
    margin = values[(RuleCategory.MARGIN, "ordinary_rate")]
    assert margin.value == Decimal("0.09")
    assert margin.effective_date == date(2026, 6, 5)
    assert "dated-notice value supersedes undated contract-text" in margin.note
    assert "合约价值的5%" in margin.note


def test_wrapped_vertical_value_lines_are_joined():
    # Rendered contract tables can wrap one value across three short lines.
    wrapped = (
        "最低交易保证金\n合约价值的\n5\n％\n"
        "每日价格波动限制\n上一交易日结算价\n±\n4%\n"
    )
    document = _document(
        wrapped,
        exchange="CZCE",
        url="https://www.czce.com.cn/cn/sspz/ppqh/bzhy/qhhy/H077002029002001index_1.htm",
    )
    values = {
        item.identity: item
        for item in extract_rule_values((document,), product="PR", product_name="瓶片")
    }
    assert values[(RuleCategory.MARGIN, "ordinary_rate")].value == Decimal("0.05")
    assert values[(RuleCategory.LIMIT, "price_and_position_limits")].value == (
        Decimal("0.04")
    )


def test_catalog_seed_page_wins_undated_conflict_against_stray_page():
    current_page = _document(
        "最低交易保证金\n合约价值的7%",
        exchange="CZCE",
        url="https://www.czce.com.cn/cn/sspz/pg/bzhy/qhhy/H077002021001001index_1.htm",
    )
    stale_page = _document(
        "最低交易保证金\n合约价值的5%",
        exchange="CZCE",
        url="https://www.czce.com.cn/cn/sspz/ycz/H077002010index_1.htm",
    )
    values = {
        item.identity: item
        for item in extract_rule_values(
            (current_page, stale_page),
            product="AP",
            product_name="苹果",
            preferred_document_urls=frozenset(
                {
                    "https://www.czce.com.cn/cn/sspz/pg/bzhy/qhhy/"
                    "H077002021001001index_1.htm"
                }
            ),
        )
    }
    margin = values[(RuleCategory.MARGIN, "ordinary_rate")]
    assert margin.value == Decimal("0.07")
    assert "catalog-seed page value supersedes stray discovered-page" in margin.note
    assert "合约价值的5%" in margin.note

    # Without the seed hint the disagreement stays review-required.
    conflicted = {
        item.identity: item
        for item in extract_rule_values(
            (current_page, stale_page),
            product="AP",
            product_name="苹果",
        )
    }
    assert conflicted[(RuleCategory.MARGIN, "ordinary_rate")].value is None


def test_real_czce_and_dce_vertical_spec_layouts_extract_concrete_values():
    czce = """交易单位
5吨/手
最小变动价位
2元/吨
每日价格波动限制
上一交易日结算价±4%及相关规定
最低交易保证金
合约价值的5%
最后交易日
合约交割月份的第10个交易日
交割方式
实物交割
"""
    dce = """交易单位 10吨/手
最小变动价位: 1元/吨
涨跌停板幅度 上一交易日结算价的4％
最低交易保证金 合约价值的5％
最后交割日 最后交易日后第3个交易日
交割方式 实物交割
"""

    czce_values = {item.label: item for item in parse_labeled_spec_values(czce)}
    dce_values = {item.label: item for item in parse_labeled_spec_values(dce)}
    assert czce_values["交易单位"].value == Decimal("5")
    assert czce_values["最小变动价位"].unit == "cny_per_ton"
    assert czce_values["每日价格波动限制"].value == Decimal("0.04")
    assert czce_values["最后交易日"].value == "合约交割月份的第10个交易日"
    assert dce_values["交易单位"].value == Decimal("10")
    assert dce_values["涨跌停板幅度"].value == Decimal("0.04")
    assert dce_values["最低交易保证金"].value == Decimal("0.05")
    assert dce_values["交割方式"].value == "实物交割"


def test_collect_evidence_mixes_candidates_and_unknowns_with_coverage():
    source = ExchangeSource(
        code="CFFEX",
        official_domains=("cffex.com.cn",),
        products=(ProductSource(code="TS", contract=None, query="TS rules"),),
    )
    url = "http://www.cffex.com.cn/cn/2ts.html"
    fetched = FetchedDocument(
        url=url,
        title="2年期国债期货",
        content=json.dumps(
            {
                "schema_version": 1,
                "retrieval_method": "local_websearch_scrape",
                "source_url": url,
                "title": "2年期国债期货",
                "markdown": CFFEX_TABLE_EXCERPT,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8"),
        content_type="application/vnd.ak-trading.websearch-scrape+json",
        retrieved_at=NOW,
    )
    discovered = (
        DiscoveredDocument(
            exchange="CFFEX",
            product="TS",
            contract=None,
            url=url,
            title="2年期国债期货",
            description="retained fixture",
            discovery_method="fixture",
        ),
    )

    evidence = collect_evidence(
        source,
        discovered,
        fetcher=lambda requested_url: fetched,
        content_path_builder=lambda digest, suffix: f"documents/{digest}{suffix}",
    )

    assert len(evidence.items) == 9
    candidates = [item for item in evidence.items if item.status is RuleStatus.CANDIDATE]
    unknowns = [
        item for item in evidence.items if item.status is RuleStatus.REVIEW_REQUIRED
    ]
    assert {(item.category, item.field) for item in candidates} == {
        (RuleCategory.MARGIN, "ordinary_rate"),
        (RuleCategory.LIMIT, "price_and_position_limits"),
        (RuleCategory.SESSION, "trading_sessions"),
        (RuleCategory.DELIVERY, "delivery_and_closeout"),
    }
    assert len(unknowns) == 5
    assert all(item.note == AUTO_EXTRACTION_NOTE for item in candidates)
    assert all(item.source_document_id == evidence.documents[0].document_id for item in candidates)
    assert all(item.excerpt for item in candidates)
    assert all(
        item.value_kind is RuleValueKind.UNKNOWN
        for item in unknowns
        if item.category is RuleCategory.FEE
    )
    text_candidates = [
        item for item in candidates if item.value_kind is RuleValueKind.TEXT
    ]
    assert text_candidates
    assert all(item.value is None for item in text_candidates)
    assert any(
        "deterministic extraction extracted 4 of 9 required fields" in reason
        and "729 fields" in reason
        for reason in evidence.review_reasons
    )
