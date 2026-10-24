import json
import os
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from ak_trading.market_reference import (
    FeeComponent,
    FeeValueStatus,
    MarkupMode,
    MarkupPolicy,
    ReferenceDataError,
    SHANGHAI_TZ,
    build_preopen_report,
    fee_for_action,
    load_reference_book,
    parse_price,
    product_report,
    write_report_artifacts,
)


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "market_reference.json"


@pytest.fixture
def book():
    return load_reference_book(CONFIG)


def _write_modified_config(tmp_path: Path, mutate) -> Path:
    raw = json.loads(CONFIG.read_text(encoding="utf-8"))
    mutate(raw)
    path = tmp_path / "reference.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return path


def test_checked_config_covers_exact_research_universe(book):
    assert book.expected_products == ("RB", "TA", "M")
    assert tuple(product.code for product in book.products) == ("RB", "TA", "M")
    assert [product.research_contract for product in book.products] == [
        "SHFE.rb2610",
        "CZCE.TA609",
        "DCE.m2609",
    ]
    assert {product.tick_value for product in book.products} == {Decimal("10.00")}


def test_rb_ad_valorem_fee_rounds_before_adding_markup(book):
    rb = book.product("RB")

    fee = fee_for_action(
        rb,
        book.broker_markup,
        "open",
        trade_price=Decimal("3100"),
    )

    assert fee["exchange_fee_cny"] == "3.10"
    assert fee["broker_markup_cny"] == "0.01"
    assert fee["total_cny"] == "3.11"
    assert fee["component_value"] == "0.0001"


def test_verified_reference_without_prices_is_warning_with_missing_inputs(book):
    verified_products = tuple(
        replace(
            product,
            static_provenance=replace(product.static_provenance, values_verified=True),
            fees=replace(
                product.fees,
                provenance=replace(product.fees.provenance, values_verified=True),
            ),
            margin=replace(
                product.margin,
                provenance=replace(product.margin.provenance, values_verified=True),
            ),
        )
        for product in book.products
    )
    verified_book = replace(
        book,
        broker_markup=replace(book.broker_markup, confirmed_with_broker=True),
        products=verified_products,
    )

    report = build_preopen_report(
        verified_book,
        date(2026, 7, 29),
        datetime(2026, 7, 29, 8, 0, tzinfo=SHANGHAI_TZ),
        notice_check={"status": "checked"},
    )

    assert report["status"] == "warning"
    rb = report["products"][0]
    assert rb["status"] == "warning"
    assert any(
        "open fee total unavailable" in warning and "trade price" in warning
        for warning in rb["warnings"]
    )
    assert any("settlement price" in warning for warning in rb["warnings"])
    assert all(
        "not human-signed" not in warning
        for product in report["products"]
        for warning in product["warnings"]
    )


def test_markup_is_charged_once_per_side_and_twice_per_round_trip(book):
    report = product_report(
        book.product("RB"),
        book.broker_markup,
        date(2026, 7, 28),
        trade_price=Decimal("3100"),
    )

    regular = report["fees"]["round_trips"]["regular"]
    same_day = report["fees"]["round_trips"]["same_day"]
    assert regular["exchange_fee_cny"] == "6.20"
    assert regular["broker_markup_cny"] == "0.02"
    assert regular["total_cny"] == "6.22"
    assert same_day["broker_markup_cny"] == "0.02"


def test_explicit_zero_close_today_is_not_unknown(book):
    ta = book.product("TA")

    fee = fee_for_action(ta, book.broker_markup, "close_today")
    report = product_report(
        ta, book.broker_markup, date(2026, 7, 28), trade_price=Decimal("5700")
    )

    assert fee["component_status"] == "explicit_zero"
    assert fee["exchange_fee_cny"] == "0.00"
    assert fee["broker_markup_cny"] == "0.01"
    assert fee["total_cny"] == "0.01"
    assert report["fees"]["round_trips"]["same_day"]["total_cny"] == "3.02"
    assert report["fees"]["round_trips"]["regular"]["total_cny"] == "6.02"


def test_unknown_close_today_fails_that_total_without_hiding_other_actions(book):
    ta = book.product("TA")
    components = dict(ta.fees.components)
    components["close_today"] = FeeComponent(FeeValueStatus.UNKNOWN, None)
    ta = replace(ta, fees=replace(ta.fees, components=components))

    report = product_report(ta, book.broker_markup, date(2026, 7, 28))

    assert report["status"] == "error"
    assert "close_today exchange fee is unknown" in report["errors"]
    assert report["fees"]["actions"]["open"]["total_cny"] == "3.01"
    assert report["fees"]["actions"]["close_today"]["total_cny"] is None
    assert not report["fees"]["round_trips"]["same_day"]["available"]


def test_rate_markup_mode_is_not_treated_as_absolute(book):
    policy = MarkupPolicy(
        mode=MarkupMode.RATE_ADD,
        value=Decimal("0.000001"),
        applies_to=frozenset({"open", "close", "close_today"}),
        confirmed_with_broker=False,
        assumption="test rate markup",
    )

    fee = fee_for_action(
        book.product("RB"), policy, "open", trade_price=Decimal("3100")
    )

    assert fee["exchange_fee_cny"] == "3.10"
    assert fee["broker_markup_cny"] == "0.03"
    assert fee["total_cny"] == "3.13"


def test_exchange_multiplier_markup_mode_is_explicit(book):
    policy = MarkupPolicy(
        mode=MarkupMode.EXCHANGE_MULTIPLIER,
        value=Decimal("1.1"),
        applies_to=frozenset({"open"}),
        confirmed_with_broker=False,
        assumption="test multiplier markup",
    )

    fee = fee_for_action(
        book.product("RB"), policy, "open", trade_price=Decimal("3100")
    )

    assert fee["exchange_fee_cny"] == "3.10"
    assert fee["broker_markup_cny"] == "0.31"
    assert fee["total_cny"] == "3.41"


@pytest.mark.parametrize("bad_value", [True, 1.5, "-1", "NaN", "Infinity"])
def test_unsafe_decimal_values_are_rejected(tmp_path: Path, bad_value):
    path = _write_modified_config(
        tmp_path,
        lambda raw: raw["products"][0].__setitem__("contract_size_tons", bad_value),
    )

    with pytest.raises(ReferenceDataError):
        load_reference_book(path)


def test_percent_in_ratio_field_is_rejected(tmp_path: Path):
    path = _write_modified_config(
        tmp_path,
        lambda raw: raw["products"][0]["margin"].__setitem__(
            "rate_of_notional", "7"
        ),
    )

    with pytest.raises(ReferenceDataError, match="ratio, not a percent"):
        load_reference_book(path)


def test_official_provenance_domain_is_enforced(tmp_path: Path):
    path = _write_modified_config(
        tmp_path,
        lambda raw: raw["products"][0]["fees"]["provenance"].__setitem__(
            "source_url", "https://www.example.com/fee.xlsx"
        ),
    )

    with pytest.raises(ReferenceDataError, match="official domain"):
        load_reference_book(path)


def test_float_prices_are_rejected():
    with pytest.raises(ReferenceDataError, match="decimal string"):
        parse_price(3100.0)


def test_trading_day_derivation_uses_shanghai_cutoff_and_holidays(book):
    before_close = datetime(2026, 7, 28, 14, 59, tzinfo=SHANGHAI_TZ)
    before_night = datetime(2026, 7, 28, 20, 45, tzinfo=SHANGHAI_TZ)
    during_night = datetime(2026, 7, 28, 21, 5, tzinfo=SHANGHAI_TZ)
    holiday_eve = datetime(2026, 9, 24, 20, 30, tzinfo=SHANGHAI_TZ)

    assert book.calendar.target_trading_day(before_close) == date(2026, 7, 28)
    assert book.calendar.target_trading_day(before_night) == date(2026, 7, 29)
    assert book.calendar.target_trading_day(during_night) == date(2026, 7, 29)
    assert book.calendar.target_trading_day(holiday_eve) == date(2026, 9, 28)


def test_calendar_refuses_to_guess_outside_coverage(book):
    with pytest.raises(ReferenceDataError, match="outside calendar coverage"):
        book.calendar.target_trading_day(
            datetime(2027, 1, 1, 8, 0, tzinfo=SHANGHAI_TZ)
        )


def test_source_expiry_is_a_hard_product_error(book):
    report = product_report(
        book.product("RB"), book.broker_markup, date(2026, 8, 5)
    )

    assert report["status"] == "error"
    assert any("must be reverified" in error for error in report["errors"])


def test_contract_last_trading_day_is_a_hard_product_error(book):
    ta = book.product("TA")
    ta = replace(
        ta,
        fees=replace(ta.fees, must_reverify_before=date(2026, 9, 30)),
        margin=replace(ta.margin, must_reverify_before=date(2026, 9, 30)),
    )

    report = product_report(ta, book.broker_markup, date(2026, 9, 14))

    assert report["status"] == "error"
    assert any("last trading day" in error for error in report["errors"])


def test_one_product_error_does_not_hide_healthy_products(book):
    ta = book.product("TA")
    components = dict(ta.fees.components)
    components["close_today"] = FeeComponent(FeeValueStatus.UNKNOWN, None)
    bad_ta = replace(ta, fees=replace(ta.fees, components=components))
    partial_book = replace(
        book,
        products=(book.product("RB"), bad_ta, book.product("M")),
    )

    report = build_preopen_report(
        partial_book,
        date(2026, 7, 28),
        datetime(2026, 7, 28, 8, 0, tzinfo=SHANGHAI_TZ),
    )

    assert report["status"] == "error"
    assert [product["code"] for product in report["products"]] == ["RB", "TA", "M"]
    assert report["products"][0]["fees"]["actions"]["open"]["component_value"] == "0.0001"
    assert report["products"][2]["fees"]["actions"]["open"]["total_cny"] == "1.51"


def test_fixed_inputs_produce_identical_report(book):
    now = datetime(2026, 7, 28, 8, 0, tzinfo=SHANGHAI_TZ)
    kwargs = {
        "trade_prices": {"RB": Decimal("3100")},
        "notice_check": {"status": "offline_not_checked", "checked_at": now.isoformat()},
    }

    first = build_preopen_report(book, date(2026, 7, 28), now, **kwargs)
    second = build_preopen_report(book, date(2026, 7, 28), now, **kwargs)

    assert first == second
    assert first["report_id"] == second["report_id"]


def test_artifacts_are_utf8_lf_and_json_is_commit_marker(
    book, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    now = datetime(2026, 7, 28, 8, 0, tzinfo=SHANGHAI_TZ)
    first = build_preopen_report(book, date(2026, 7, 28), now)
    json_path, markdown_path = write_report_artifacts(first, tmp_path)

    assert json_path.parent == markdown_path.parent == tmp_path / "2026-07-28"
    assert not json_path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in markdown_path.read_bytes()
    assert "螺纹钢" in markdown_path.read_text(encoding="utf-8")

    second = build_preopen_report(
        book,
        date(2026, 7, 28),
        datetime(2026, 7, 28, 8, 1, tzinfo=SHANGHAI_TZ),
    )
    real_replace = os.replace
    calls = 0

    def fail_json_commit(source, destination):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated commit failure")
        return real_replace(source, destination)

    monkeypatch.setattr("ak_trading.market_reference.os.replace", fail_json_commit)
    with pytest.raises(OSError, match="simulated commit failure"):
        write_report_artifacts(second, tmp_path)
    assert json.loads(json_path.read_text(encoding="utf-8"))["report_id"] == first["report_id"]


def test_unknown_product_price_is_rejected(book):
    with pytest.raises(ReferenceDataError, match="unknown products"):
        build_preopen_report(
            book,
            date(2026, 7, 28),
            datetime(2026, 7, 28, 8, 0, tzinfo=SHANGHAI_TZ),
            trade_prices={"AU": Decimal("800")},
        )


def test_report_keeps_broker_and_exchange_boundaries_visible(book):
    report = build_preopen_report(
        book,
        date(2026, 7, 28),
        datetime(2026, 7, 28, 8, 0, tzinfo=SHANGHAI_TZ),
    )

    assert not report["broker_markup"]["confirmed_with_broker"]
    assert all(
        product["margin"]["broker_actual"] == "unknown_and_may_be_higher"
        for product in report["products"]
    )
    assert "slippage" in report["out_of_scope_costs"]
    assert all(
        not product["reconciled_with_broker_statement"]
        for product in report["products"]
    )
