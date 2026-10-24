from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ak_trading.settlement_reference import load_settlement_reference

DAY = date(2026, 9, 9)
SYMBOL = "SHFE.rb2610"
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("query_settlement_reference", ROOT / "scripts/query_settlement_reference.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


def official_row(**changes):
    return {"INSTRUMENTID": "rb2610", "PRODUCTID": "rb_f", "SPECLONGMARGINRATIO": "0.10",
            "SPECSHORTMARGINRATIO": "0.11", "HEDGLONGMARGINRATIO": "0.07", "HEDGSHORTMARGINRATIO": "0.08",
            "TRADEFEERATIO": "0.1", "TRADEFEEUNIT": "0", "TTRADEFEERATIO": "0", "TTRADEFEEUNIT": "0",
            "SETTLEMENTPRICE": "3095", **changes}


def reference_row(**changes):
    return {"交易所": "SHFE", "合约代码": "rb2610", "品种代码": "rb", "品种名称": "螺纹钢",
            "合约乘数": "10", "开仓费率": "0.000101", "开仓费用/手": "0.01", "平仓费率": "0",
            "平仓费用/手": "2.00", "平今费率": "0", "平今费用/手": "0", "做多保证金率": "0.1",
            "做空保证金率": "0.12", "做多保证金/手": "0", "做空保证金/手": "5",
            "1手开仓费用": "3.13", "做多1手保证金": "3097.0", "最新价": "3097",
            "更新时间": "2026-09-09 15:59:37", **changes}


def snapshot(tmp_path, *, rows=None, references=None, day="20260909", filename="js_shfe.json", logged_day="2026-09-09", payload=False):
    base = tmp_path / "settlement_params"
    folder = base / day
    folder.mkdir(parents=True, exist_ok=True)
    if rows is not None:
        raw = {"o_cursor": rows, "TRADINGDAY": logged_day} if payload else rows
        (folder / filename).write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        if logged_day is not None and not payload:
            logs = base / "task_logs"
            logs.mkdir(exist_ok=True)
            exchange = "INE" if "ine" in filename else "SHFE"
            (logs / f"settlement_params_{day}_182002.log").write_text(
                json.dumps(dict(site=exchange, status="ok", day=logged_day, rows=len(rows))) + "\n", encoding="utf-8")
    if references is not None:
        with (folder / "openctp.csv").open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(dict.fromkeys(k for row in references for k in row)))
            writer.writeheader()
            writer.writerows(references)
    return folder


def load(tmp_path, **kwargs):
    return load_settlement_reference(tmp_path, day=DAY, symbol=SYMBOL, **kwargs)


def test_exact_contract_keeps_official_separate_from_reference_and_product_ranges(tmp_path):
    folder = snapshot(tmp_path, rows=[official_row(INSTRUMENTID="rb2609", SPECLONGMARGINRATIO="0.15"), official_row()],
                      references=[reference_row(**{"合约代码": "rb2609", "做多保证金率": "0.2"}), reference_row()])
    (folder / "summary.json").write_text('{"spec_long": 0.99}', encoding="utf-8")
    report = load(tmp_path)
    assert report["available"] and report["investor_charges"] is None
    assert report["official"]["margins"]["spec_long"]["rate"]["value"] == "0.10"
    assert report["reference"]["margins"]["long"]["rate"]["value"] == "0.1"
    assert report["official"]["product_summary"]["raw_numeric_ranges"]["SPECLONGMARGINRATIO"] == dict(min="0.10", max="0.15", valid_count=2)
    assert report["official"]["product_summary"]["contract_count"] == 2
    assert report["official"]["provenance"]["classification"] == "official_exchange"
    assert report["reference"]["provenance"]["classification"] == "reference_broker_unverified"
    json.dumps(report, allow_nan=False)


def test_preserves_exact_decimals_adds_both_fee_components_and_compares_reported(tmp_path):
    snapshot(tmp_path, references=[reference_row(**{"开仓费率": "0.00010100000000000000001"})])
    report = load(tmp_path, trade_price=Decimal("3100.25"), settlement_price=Decimal("3101.12"))["reference"]
    opening = report["fees"]["open"]
    assert opening["ratio"]["value"] == "0.00010100000000000000001"
    assert opening["per_lot_cny"] == "3.1412525000000000003100250"
    assert opening["calculated_minus_reported_cny"] == "0.0112525000000000003100250"
    assert report["margins"]["long"]["rate_based_per_lot_cny"] == "3101.120"
    assert report["margins"]["short"]["flat"]["value"] == "5"
    assert report["margins"]["short"]["rate_based_per_lot_cny"] == "3721.3440"


def test_explicit_zero_and_flat_fees_work_without_price_or_multiplier(tmp_path):
    snapshot(tmp_path, references=[reference_row(**{"合约乘数": ""})])
    fees = load(tmp_path)["reference"]["fees"]
    assert fees["open"]["per_lot_cny"] is None
    assert fees["close"]["per_lot_cny"] == "2.00"
    assert fees["close_today"]["per_lot_cny"] == "0"
    assert fees["close_today"]["ratio"]["status"] == "valid"


def test_missing_flat_or_ratio_is_unknown_not_zero(tmp_path):
    snapshot(tmp_path, references=[reference_row(**{"平仓费率": "", "平今费用/手": ""})])
    fees = load(tmp_path)["reference"]["fees"]
    assert fees["close"]["per_lot_cny"] is None
    assert fees["close_today"]["per_lot_cny"] is None
    assert fees["close_today"]["flat"]["status"] == "missing"


def test_unknown_official_fee_units_block_nonzero_but_allow_explicit_zero(tmp_path):
    snapshot(tmp_path, rows=[official_row()], references=[reference_row()])
    report = load(tmp_path, trade_price=Decimal("3100"), settlement_price=Decimal("3095"))["official"]
    assert report["fees"]["open"]["ratio"]["raw"] == "0.1"
    assert report["fees"]["open"]["ratio"]["status"] == "unit_unverified"
    assert report["fees"]["open"]["per_lot_cny"] is None
    assert report["fees"]["close_today"]["per_lot_cny"] == "0"
    assert report["margins"]["spec_long"]["rate_based_per_lot_cny"] == "3095.00"
    assert report["margins"]["hedge_short"]["rate_based_per_lot_cny"] == "2476.00"
    assert report["calculation_multiplier"]["provenance"]["source"] == "openctp"


def test_numeric_sentinels_nonfinite_negative_and_out_of_range_rates_are_unknown(tmp_path):
    for invalid in ("nan", "Infinity", "-1", "1.7976931348623157e308", "7", "True", "0e-999999999"):
        snapshot(tmp_path, references=[reference_row(**{"开仓费率": invalid, "做多保证金率": invalid})])
        report = load(tmp_path, trade_price=Decimal("3100"), settlement_price=Decimal("3100"))["reference"]
        assert report["fees"]["open"]["per_lot_cny"] is None, invalid
        assert report["margins"]["long"]["rate"]["value"] is None, invalid
        assert report["margins"]["long"]["rate_based_per_lot_cny"] is None, invalid


def test_undated_sources_are_visible_but_never_approved_or_calculated(tmp_path):
    snapshot(tmp_path, rows=[official_row()], references=[reference_row(**{"更新时间": ""})], logged_day=None)
    report = load(tmp_path, trade_price=Decimal("3100"), settlement_price=Decimal("3100"))
    assert not report["available"]
    assert report["official"]["status"] == "date_unverified"
    assert report["official"]["raw"]["SPECLONGMARGINRATIO"] == "0.10"
    assert report["official"]["provenance"]["source_date"] is None
    assert report["reference"]["fees"]["close_today"]["per_lot_cny"] is None


def test_source_hash_is_raw_bytes_and_log_date_is_auditable(tmp_path):
    folder = snapshot(tmp_path, rows=[official_row()], references=[reference_row()])
    report = load(tmp_path)
    for key, filename in (("official", "js_shfe.json"), ("reference", "openctp.csv")):
        source = report[key]["provenance"]
        assert source["sha256"] == hashlib.sha256((folder / filename).read_bytes()).hexdigest()
        assert source["source_date"] == "2026-09-09"
    evidence = report["official"]["provenance"]["date_evidence"][0]
    assert evidence["kind"] == "collector_log" and evidence["line"] == 1
    assert evidence["sha256"] == hashlib.sha256(Path(evidence["path"]).read_bytes()).hexdigest()


def test_dated_snapshot_fallback_and_real_source_day_are_explicit(tmp_path):
    snapshot(tmp_path, rows=[official_row()], day="20260906", logged_day="2026-09-04")
    source = load(tmp_path)["official"]["provenance"]
    assert source["fallback"] and source["snapshot_date"] == "2026-09-06"
    assert source["source_date"] == "2026-09-04"
    snapshot(tmp_path, rows=[official_row(SPECLONGMARGINRATIO="0.9")], day="20260910", logged_day="2026-09-10")
    assert load(tmp_path)["official"]["margins"]["spec_long"]["rate"]["value"] == "0.10"


def test_future_or_conflicting_source_dates_block_calculation(tmp_path):
    snapshot(tmp_path, rows=[official_row(TRADINGDAY="20260910")], logged_day=None)
    assert load(tmp_path)["official"]["status"] == "date_future"
    folder = snapshot(tmp_path, rows=[official_row(TRADINGDAY="20260908")])
    assert load(tmp_path)["official"]["status"] == "date_conflicting"
    log = folder.parent / "task_logs/settlement_params_20260909_182002.log"
    log.write_text('{"site":"SHFE","status":"ok","day":"2026-09-08","rows":99}\n', encoding="utf-8")
    assert load(tmp_path)["official"]["provenance"]["source_date"] == "2026-09-08"


def test_official_payload_dates_filenames_delivery_month_and_exchange_isolation(tmp_path):
    row = official_row(INSTRUMENTID="", PRODUCTID="rb", DELIVMONTH="2610")
    snapshot(tmp_path, rows=[row], filename="js_shfe_20260909.json", logged_day=None)
    assert load(tmp_path)["official"]["available"]
    snapshot(tmp_path, rows=[official_row(INSTRUMENTID="sc2610", PRODUCTID="sc_f", TRADEFEERATIO=0, TRADEFEEUNIT=20)],
             filename="js_ine.json", payload=True)
    report = load_settlement_reference(tmp_path / "settlement_params", day=DAY, symbol="INE.sc2610")
    assert report["official"]["fees"]["open"]["per_lot_cny"] == "20"
    assert report["official"]["provenance"]["date_evidence"][0]["kind"] == "payload"


def test_exact_exchange_and_month_required_duplicates_are_ambiguous(tmp_path):
    snapshot(tmp_path, references=[reference_row(**{"交易所": "DCE"}), reference_row(**{"合约代码": "rb2609"})])
    assert not load(tmp_path)["available"]
    snapshot(tmp_path, references=[reference_row(), reference_row()])
    assert load(tmp_path)["reference"]["status"] == "ambiguous_contract"


def test_chinese_exchange_names_map_exactly(tmp_path):
    names = {"上海期货交易所": "SHFE", "上海国际能源交易中心": "INE", "郑州商品交易所": "CZCE",
             "大连商品交易所": "DCE", "中国金融期货交易所": "CFFEX", "广州期货交易所": "GFEX"}
    for chinese, exchange in names.items():
        snapshot(tmp_path, references=[reference_row(**{"交易所": chinese})])
        assert load_settlement_reference(tmp_path, day=DAY, symbol=f"{exchange}.rb2610")["reference"]["available"]


def test_options_return_raw_without_general_fee_or_margin_formula(tmp_path):
    snapshot(tmp_path, references=[reference_row(**{"合约代码": "rb2610C3200"})])
    report = load_settlement_reference(tmp_path, day=DAY, symbol="SHFE.rb2610C3200",
                                       trade_price=Decimal("10"), settlement_price=Decimal("3100"))
    assert report["available"] and report["instrument_class"] == "option"
    assert report["reference"]["fees"]["close_today"]["per_lot_cny"] is None
    assert report["reference"]["margins"]["long"]["rate_based_per_lot_cny"] is None
    assert report["reference"]["fees"]["open"]["reported_per_lot"]["value"] == "3.13"


def test_no_implicit_price_from_source_and_no_wrong_contract_multiplier(tmp_path):
    snapshot(tmp_path, rows=[official_row()], references=[reference_row(**{"合约代码": "rb2609"})])
    report = load(tmp_path, settlement_price=Decimal("3100"))
    assert report["official"]["margins"]["spec_long"]["rate_based_per_lot_cny"] is None
    snapshot(tmp_path, references=[reference_row()])
    report = load(tmp_path)
    assert report["reference"]["fees"]["open"]["per_lot_cny"] is None
    assert report["reference"]["margins"]["long"]["rate_based_per_lot_cny"] is None


def test_corrupt_source_does_not_hide_other_source_and_latest_is_ignored(tmp_path):
    folder = snapshot(tmp_path, references=[reference_row()])
    (folder / "js_shfe.json").write_text("not json", encoding="utf-8")
    (folder.parent / "latest.json").write_text("bad summary", encoding="utf-8")
    report = load(tmp_path)
    assert report["available"] and report["official"] is None
    assert report["diagnostics"][0]["status"] == "unreadable"
    assert not load_settlement_reference(tmp_path, day=date(2026, 9, 1), symbol=SYMBOL)["available"]


def test_invalid_input_and_output_paths_cannot_overwrite(tmp_path, capsys):
    for symbol in ("SHFE.rb", "KQ.m@SHFE.rb", "SHFE.../rb2610", "BAD.rb2610", "SHFE.rb2613"):
        with pytest.raises(ValueError):
            load_settlement_reference(tmp_path, day=DAY, symbol=symbol)
    for price in (Decimal("NaN"), Decimal("Infinity"), Decimal("0"), Decimal("-1"), Decimal("1e308"), 1.2):
        with pytest.raises(ValueError):
            load(tmp_path, trade_price=price)
    target = tmp_path / "preserve.json"
    target.write_text("original", encoding="utf-8")
    args = ["--root", str(tmp_path), "--symbol", SYMBOL, "--day", DAY.isoformat()]
    assert cli.main(args + ["--output", str(target)]) == 2
    assert target.read_text(encoding="utf-8") == "original"
    assert cli.main(args + ["--output", str(tmp_path / ".env")]) == 2
    assert not (tmp_path / ".env").exists()
    assert cli.main(args + ["--price", "NaN"]) == 2
    assert cli.main(args + ["--root", str(target)]) == 2
    for unsafe in (str(tmp_path / "report.json:stream"), str(tmp_path / "nul"), "//server/share/report.json"):
        assert cli.main(args + ["--output", unsafe]) == 2
        assert cli.main(args + ["--root", unsafe]) == 2


def test_malformed_raw_values_and_duplicate_logs_cannot_approve_dates(tmp_path):
    folder = snapshot(tmp_path, rows=[official_row(TRADEFEERATIO=True, SPECLONGMARGINRATIO=True)])
    report = load(tmp_path)["official"]
    assert report["fees"]["open"]["ratio"]["status"] == "invalid"
    assert report["margins"]["spec_long"]["rate"]["status"] == "invalid"
    (folder.parent / "task_logs/settlement_params_20260909_192002.log").write_text(
        '{"site":"SHFE","status":"ok","day":"2026-09-08","rows":1}\n', encoding="utf-8")
    assert load(tmp_path)["official"]["status"] == "date_conflicting"
    (folder / "js_shfe.json").write_text('{"o_cursor": [null]}', encoding="utf-8")
    assert not load(tmp_path)["available"]


def test_unverified_latest_contract_cannot_silently_use_older_approved_row(tmp_path):
    snapshot(tmp_path, rows=[official_row()], day="20260908", logged_day="2026-09-08")
    snapshot(tmp_path, rows=[official_row(SPECLONGMARGINRATIO="0.15")], logged_day=None)
    report = load(tmp_path)
    assert not report["available"] and report["official"]["status"] == "date_unverified"
    assert report["official"]["margins"]["spec_long"]["rate"]["value"] == "0.15"


def test_cli_exit_codes_and_exclusive_new_output(tmp_path, capsys):
    args = ["--root", str(tmp_path), "--symbol", SYMBOL, "--day", DAY.isoformat()]
    assert cli.main(args) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "unavailable"
    snapshot(tmp_path, references=[reference_row()])
    output = tmp_path / "report.json"
    assert cli.main(args + ["--price", "3100", "--settlement", "3095", "--output", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["reference"]["fees"]["open"]["per_lot_cny"] == "3.141000"
    assert cli.main(args + ["--output", str(output)]) == 2
    for missing in (["--day", "2026-09-09"], ["--symbol", SYMBOL], ["--symbol", SYMBOL, "--day", "bogus"]):
        with pytest.raises(SystemExit) as exc:
            cli.main(missing)
        assert exc.value.code == 2
