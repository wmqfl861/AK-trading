"""Contract-level references from local settlement snapshots; no investor pricing.

Amounts are JSON-safe decimal strings. Official nonzero fee ratios have no
verified unit in these retained files, so they cannot produce fee amounts.
Snapshot directory dates establish observation, never the effective source day.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path

_EXCHANGES = dict(zip(
    ("上海期货交易所", "上海国际能源交易中心", "郑州商品交易所",
     "大连商品交易所", "中国金融期货交易所", "广州期货交易所"),
    ("SHFE", "INE", "CZCE", "DCE", "CFFEX", "GFEX"),
))
_CONTRACT = re.compile(r"([A-Za-z]+)(\d{3,4})(?:-?[CPcp]-?\d+(?:\.\d+)?)?")
_DATE_FIELDS = {"DATE", "DAY", "TRADINGDAY", "TRADINGDATE", "TRADEDATE", "SOURCE_DATE"}


def _number(value, *, rate=False, positive=False):
    if value is None or isinstance(value, bool) or len(str(value)) > 128:
        return None
    try:
        result = Decimal(str(value).strip())
    except InvalidOperation:
        return None
    if (not result.is_finite() or result < 0 or result >= Decimal("1e30")
            or not -128 <= result.as_tuple().exponent <= 30
            or (result != 0 and result < Decimal("1e-30"))
            or (rate and result > 1) or (positive and result == 0)):
        return None
    return result


def _text(value):
    return None if value is None else format(value, "f")


def _local_path(path):
    path = Path(path)
    for resolve in (False, True):
        candidate = path.resolve() if resolve else path
        if (str(candidate).startswith(("\\\\", "//")) or "://" in str(candidate)
                or candidate.drive and not candidate.root
                or any(part.lower().startswith(".env") or Path(part).is_reserved()
                       or ":" in part for part in candidate.parts if part != candidate.anchor)):
            raise ValueError("path must be local, excluding .env, device paths, and alternate streams")
    return candidate


def _get(row, key):
    return next((value for name, value in row.items() if name.upper() == key.upper()), None)


def _field(row, key, unit):
    key = next((name for name in row if name.upper() == key.upper()), key)
    raw = row.get(key)
    value = _number(raw, rate=unit == "rate_of_notional", positive=unit == "units_per_lot")
    status = "missing" if raw is None or raw == "" else "invalid" if value is None else "valid"
    if unit == "unverified" and value is not None and value != 0:
        value, status = None, "unit_unverified"
    return dict(field=key, raw=raw, value=_text(value), unit=unit, status=status)


def _date(value):
    text = str(value or "").strip()
    try:
        if re.fullmatch(r"\d{8}", text):
            return datetime.strptime(text, "%Y%m%d").date()
        if re.match(r"^\d{4}-\d{2}-\d{2}(?:$|[ T])", text):
            return datetime.fromisoformat(text).date()
    except ValueError:
        pass
    return None


def _contract(value):
    match = _CONTRACT.fullmatch(str(value or "").strip())
    if not match or not 1 <= int(match[2][-2:]) <= 12:
        return None
    return match[1].lower(), "future" if match.end(2) == match.end() else "option"


def _identity(row, exchange, official):
    if official:
        instrument = _get(row, "INSTRUMENTID")
        if not instrument:
            product = str(_get(row, "PRODUCTID") or "").strip()
            month = str(_get(row, "DELIVMONTH") or "").strip()
            base = product.removesuffix("_f")
            instrument = base + month if re.fullmatch(r"[A-Za-z]+", base) and re.fullmatch(r"\d{3,4}", month) else ""
    else:
        source_exchange = str(row.get("交易所", "")).strip()
        if _EXCHANGES.get(source_exchange, source_exchange.upper()) != exchange:
            return ""
        instrument = row.get("合约代码")
    return str(instrument or "").strip().lower()


def _source_date(path, row, metadata, rows, exchange, official, requested):
    evidence = []
    if official:
        for origin, values in (("row", row), ("payload", metadata)):
            evidence.extend(dict(kind=origin, field=k, value=v) for k, v in values.items()
                            if k.upper() in _DATE_FIELDS)
        match = re.search(r"(\d{8})(?=\.(?:json|dat)$)", path.name)
        if match:
            evidence.append(dict(kind="filename", value=match[1]))
        log_dir = path.parent.parent / "task_logs"
        logs = sorted(log_dir.glob(f"settlement_params_{path.parent.name}_*.log")) if (
            not log_dir.is_symlink() and log_dir.resolve().is_relative_to(path.parent.parent)) else []
        for log in logs:
            if log.is_symlink() or not log.is_file() or not log.resolve().is_relative_to(path.parent.parent):
                continue
            raw = log.read_bytes()
            for line, text in enumerate(raw.decode("utf-8-sig").splitlines(), 1):
                try:
                    event = json.loads(text)
                except ValueError:
                    continue
                if (isinstance(event, dict) and event.get("site") == exchange
                        and event.get("status") == "ok" and event.get("rows") == len(rows)):
                    evidence.append(dict(kind="collector_log", value=event.get("day"),
                                         path=str(log), line=line, sha256=hashlib.sha256(raw).hexdigest()))
    elif "更新时间" in row:
        evidence.append(dict(kind="row", field="更新时间", value=row["更新时间"]))
    days = {_date(item["value"]) for item in evidence}
    source_day = next(iter(days)) if len(days) == 1 and None not in days else None
    state = "unverified" if not evidence else "conflicting" if len(days) > 1 else "invalid"
    if source_day:
        state = "verified" if source_day <= requested and source_day <= _date(path.parent.name) else "future"
    return dict(source_date=source_day.isoformat() if source_day else None,
                date_status=state, date_evidence=evidence)


def _summary(rows, exchange, official, product_class):
    selected = [row for row in rows if _contract(_identity(row, exchange, official)) == product_class]
    fields = (("SPECLONGMARGINRATIO", "SPECSHORTMARGINRATIO", "HEDGLONGMARGINRATIO",
               "HEDGSHORTMARGINRATIO", "TRADEFEERATIO", "TRADEFEEUNIT", "TTRADEFEERATIO", "TTRADEFEEUNIT")
              if official else ("做多保证金率", "做空保证金率", "开仓费率", "开仓费用/手", "平仓费率",
                                "平仓费用/手", "平今费率", "平今费用/手"))
    ranges = {}
    for key in fields:
        numbers = [n for row in selected if (n := _number(_get(row, key))) is not None]
        if numbers:
            ranges[key] = dict(min=_text(min(numbers)), max=_text(max(numbers)), valid_count=len(numbers))
    return dict(contract_count=len({_identity(r, exchange, official) for r in selected}),
                raw_numeric_ranges=ranges, units="raw source field units; no representative contract")


def _load(base, folders, exchange, instrument, official, day, diagnostics):
    last = None
    pattern = re.compile(rf"js_{exchange.lower()}(?:_?\d{{8}})?\.(?:json|dat)$")
    for folder in folders:
        files = sorted(p for p in folder.iterdir() if (pattern.fullmatch(p.name) if official else p.name == "openctp.csv"))
        for path in files:
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(base):
                continue
            try:
                raw = path.read_bytes()
                if official:
                    payload = json.loads(raw.decode("utf-8-sig"), parse_float=str, parse_int=str, parse_constant=str)
                    rows = payload.get("o_cursor") if isinstance(payload, dict) else payload
                    metadata = payload if isinstance(payload, dict) else {}
                else:
                    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
                    metadata = {}
                if not isinstance(rows, list) or not all(isinstance(r, dict) and None not in r for r in rows):
                    raise ValueError("expected contract rows")
                matches = [r for r in rows if _identity(r, exchange, official) == instrument.lower()]
                last = dict(status="contract_not_found", available=False,
                            provenance=dict(source="official_js" if official else "openctp",
                                            classification="official_exchange" if official else "reference_broker_unverified",
                                            path=str(path), sha256=hashlib.sha256(raw).hexdigest(),
                                            snapshot_date=_date(folder.name).isoformat()),
                            product_summary=_summary(rows, exchange, official, _contract(instrument)))
                if not matches:
                    diagnostics.append(dict(path=str(path), status="contract_not_found"))
                    continue
                if len(matches) != 1:
                    last.update(status="ambiguous_contract", matching_rows=len(matches))
                    return last
                row = matches[0]
                dates = _source_date(path, row, metadata, rows, exchange, official, day)
                last["provenance"].update(dates, fallback=(dates["source_date"] != day.isoformat()
                                                          or _date(folder.name) != day))
                last.update(raw=row, status="available" if dates["date_status"] == "verified" else "date_" + dates["date_status"])
                last["available"] = last["status"] == "available"
                return last
            except (OSError, UnicodeError, ValueError, csv.Error) as exc:
                diagnostics.append(dict(path=str(path), status="unreadable", reason=str(exc)))
    return last


def _metrics(section, official):
    if not section or "raw" not in section:
        return
    row = section["raw"]
    section["multiplier"] = _field(row, "VOLUMEMULTIPLE" if official else "合约乘数", "units_per_lot")
    section["fees"] = {}
    for action, prefix in (("open", "开仓"), ("close", "平仓"), ("close_today", "平今")):
        ratio = ("TTRADEFEERATIO" if action == "close_today" else "TRADEFEERATIO") if official else prefix + "费率"
        flat = ("TTRADEFEEUNIT" if action == "close_today" else "TRADEFEEUNIT") if official else prefix + "费用/手"
        section["fees"][action] = dict(
            ratio=_field(row, ratio, "unverified" if official else "rate_of_notional"),
            flat=_field(row, flat, "cny_per_lot"), per_lot_cny=None)
        if not official:
            section["fees"][action]["reported_per_lot"] = _field(row, "1手" + prefix + "费用", "cny_per_lot")
    sides = (("spec_long", "SPECLONG"), ("spec_short", "SPECSHORT"),
             ("hedge_long", "HEDGLONG"), ("hedge_short", "HEDGSHORT")) if official else (("long", "做多"), ("short", "做空"))
    section["margins"] = {}
    for side, prefix in sides:
        section["margins"][side] = dict(rate=_field(row, prefix + ("MARGINRATIO" if official else "保证金率"), "rate_of_notional"),
                                          rate_based_per_lot_cny=None)
        if not official:
            section["margins"][side].update(flat=_field(row, prefix + "保证金/手", "cny_per_lot"),
                                            reported_per_lot=_field(row, prefix + "1手保证金", "cny_per_lot"))


def _calculate(section, multiplier_source, future, price, settlement):
    if not section or "fees" not in section:
        return
    multiplier = None
    if multiplier_source and multiplier_source["available"]:
        multiplier = _number(multiplier_source["multiplier"]["value"], positive=True)
        if multiplier is not None:
            section["calculation_multiplier"] = dict(multiplier_source["multiplier"], provenance=multiplier_source["provenance"])
    eligible = section["available"] and future and not str(_get(section["raw"], "PRODUCTID") or "").endswith("_o")
    with localcontext() as context:
        context.prec = 512
        for fee in section["fees"].values():
            ratio, flat = (_number(fee[key]["value"]) for key in ("ratio", "flat"))
            if eligible and ratio is not None and flat is not None:
                if ratio == 0:
                    fee["per_lot_cny"] = _text(flat)
                elif price is not None and multiplier is not None:
                    fee["per_lot_cny"] = _text(ratio * price * multiplier + flat)
            if "reported_per_lot" in fee and fee["per_lot_cny"] is not None:
                observed = _number(fee["reported_per_lot"]["value"])
                fee["calculated_minus_reported_cny"] = _text(Decimal(fee["per_lot_cny"]) - observed) if observed is not None else None
        for margin in section["margins"].values():
            rate = _number(margin["rate"]["value"], rate=True)
            if eligible and rate is not None and settlement is not None and multiplier is not None:
                margin["rate_based_per_lot_cny"] = _text(rate * settlement * multiplier)


def load_settlement_reference(root: Path, *, day: date, symbol: str,
                              trade_price: Decimal | None = None,
                              settlement_price: Decimal | None = None) -> dict:
    """Read data/settlement_params (or that directory directly), never latest.json.

    Only exact contracts from snapshots observed on/before ``day`` are selected.
    ``available`` means a dated local reference exists, not approval to trade or
    confirmation of an investor's charges. No rounding policy is invented.
    """
    if type(day) is not date:
        raise ValueError("day must be a date")
    if not isinstance(symbol, str):
        raise ValueError("symbol must be EXCHANGE.contract")
    exchange, separator, instrument = symbol.strip().partition(".")
    exchange = exchange.upper()
    identity = _contract(instrument)
    if not separator or exchange not in _EXCHANGES.values() or identity is None:
        raise ValueError("symbol must be a concrete EXCHANGE.contract (future or option)")
    for name, value in (("trade_price", trade_price), ("settlement_price", settlement_price)):
        if value is not None and (not isinstance(value, Decimal) or _number(value, positive=True) is None):
            raise ValueError(f"{name} must be a finite positive Decimal below 1e30")
    root = _local_path(root)
    if root.exists() and not root.is_dir():
        raise ValueError("root must be a local data directory")
    base = _local_path(root if root.name == "settlement_params" else root / "settlement_params")
    if base.exists() and not base.is_dir():
        raise ValueError("root must be a local data directory")
    folders = sorted((p for p in base.iterdir() if re.fullmatch(r"\d{8}", p.name)
                      and _date(p.name) is not None and _date(p.name) <= day
                      and not p.is_symlink() and p.is_dir() and p.resolve().is_relative_to(base)), reverse=True) if base.exists() else []
    diagnostics = []
    official = _load(base, folders, exchange, instrument, True, day, diagnostics) if exchange in {"SHFE", "INE"} else None
    reference = _load(base, folders, exchange, instrument, False, day, diagnostics)
    _metrics(official, True)
    _metrics(reference, False)
    for section in (official, reference):
        own = section if section and section.get("multiplier", {}).get("value") is not None else reference
        _calculate(section, own, identity[1] == "future", trade_price, settlement_price)
    available = any(s and s["available"] for s in (official, reference))
    return dict(schema_version=1, available=available, status="available" if available else "unavailable",
                symbol=f"{exchange}.{instrument}", day=day.isoformat(), product=f"{exchange}.{identity[0]}",
                instrument_class=identity[1], inputs=dict(trade_price=_text(trade_price), settlement_price=_text(settlement_price)),
                official=official, reference=reference, diagnostics=diagnostics,
                investor_charges=None,
                notes=["Local references do not establish broker/investor charges or Infini capabilities.",
                       "Nonzero official fee ratios have unverified units; zero is scale-independent.",
                       "Fee = ratio * supplied trade price * multiplier + flat; both components must be known.",
                       "Futures margin rate amounts use supplied settlement price; flat and reported amounts remain separate.",
                       "Product ranges describe raw rows only; source dates and multipliers retain their own provenance."])
