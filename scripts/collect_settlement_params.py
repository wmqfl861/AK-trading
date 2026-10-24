"""Daily settlement-parameter snapshot (margin + fees) with cross-check.

Layer 1 (official): SHFE/INE ``js{date}.dat`` direct JSON (per-contract
spec/hedge margin ratios, trade/close-today fee ratios and per-lot fees).
Layer 2 (reference): the openctp fee/margin table via akshare
(``futures_fees_info``), daily-updated, all exchanges — fills CZCE/DCE/GFEX
and cross-checks the official layer; mismatches are reported, never hidden.

Outputs under ``data/settlement_params/``:
- ``YYYYMMDD/js_shfe.json`` / ``js_ine.json`` — raw official payloads
- ``YYYYMMDD/openctp.csv`` — raw reference table
- ``YYYYMMDD/summary.json`` — per-product latest margin/fee with source and
  cross-check verdicts; plus ``latest.json`` copied for easy consumption.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Mapping

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "settlement_params"
_JS_SITES = {
    "SHFE": "https://www.shfe.com.cn",
    "INE": "https://www.ine.cn",
}
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
}
_LOOKBACK_DAYS = 7


def _fetch_js(site: str, base: str, day: date) -> tuple[date, list[dict]] | None:
    with httpx.Client(headers={**_HEADERS, "Referer": f"{base}/"}, timeout=30) as client:
        for offset in range(_LOOKBACK_DAYS):
            probe = day - timedelta(days=offset)
            url = f"{base}/data/tradedata/future/dailydata/js{probe:%Y%m%d}.dat"
            try:
                response = client.get(url)
            except httpx.HTTPError:
                continue
            if response.status_code != 200:
                continue
            try:
                rows = response.json().get("o_cursor") or []
            except ValueError:
                continue
            if rows and "SPECLONGMARGINRATIO" in rows[0]:
                return probe, rows
    return None


def _js_product_view(rows: list[dict]) -> dict[str, dict]:
    """Collapse per-contract rows to per-product values (majority rule)."""

    from collections import Counter, defaultdict

    buckets: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        product = (row.get("PRODUCTID") or "").replace("_f", "").replace("_o", "")
        if not product:
            continue
        buckets[product]["spec_long"].append(row.get("SPECLONGMARGINRATIO"))
        buckets[product]["fee_ratio"].append(row.get("TRADEFEERATIO"))
        buckets[product]["fee_unit"].append(row.get("TRADEFEEUNIT"))
        buckets[product]["ct_fee_ratio"].append(row.get("TTRADEFEERATIO"))
        buckets[product]["ct_fee_unit"].append(row.get("TTRADEFEEUNIT"))
    view: dict[str, dict] = {}
    for product, fields in buckets.items():
        entry: dict = {}
        for name, values in fields.items():
            clean = [v for v in values if isinstance(v, (int, float))]
            if not clean:
                continue
            entry[name] = Counter(clean).most_common(1)[0][0]
            entry[f"{name}_contracts"] = sorted(set(clean))
        view[product] = entry
    return view


def _fetch_openctp() -> list[dict]:
    import akshare as ak

    frame = ak.futures_fees_info()
    return frame.to_dict(orient="records")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", type=date.fromisoformat, default=date.today())
    args = parser.parse_args()

    day_dir = OUT_DIR / f"{args.day:%Y%m%d}"
    day_dir.mkdir(parents=True, exist_ok=True)

    official: dict[str, dict] = {}
    for site, base in _JS_SITES.items():
        fetched = _fetch_js(site, base, args.day)
        if fetched is None:
            print(json.dumps({"site": site, "status": "unavailable"}))
            continue
        js_day, rows = fetched
        (day_dir / f"js_{site.lower()}.json").write_bytes(
            json.dumps(rows, ensure_ascii=False).encode("utf-8")
        )
        for product, entry in _js_product_view(rows).items():
            official[f"{site}.{product}"] = {
                **entry, "source": f"js.dat:{js_day:%Y%m%d}", "exchange": site,
            }
        print(json.dumps({
            "site": site, "status": "ok", "day": f"{js_day:%Y-%m-%d}",
            "rows": len(rows),
        }))

    reference: dict[str, dict] = {}
    try:
        records = _fetch_openctp()
        fieldnames = list(records[0].keys()) if records else []
        with (day_dir / "openctp.csv").open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(records)
        for record in records:
            key = f"{record.get('交易所')}.{record.get('品种代码')}"
            try:
                reference[key] = {
                    "margin_long": float(record.get("做多保证金率") or 0) or None,
                    "fee_open_ratio": float(record.get("开仓费率") or 0) or None,
                    "fee_open_per_lot": float(record.get("开仓费用/手") or 0) or None,
                    "fee_close_today_ratio": float(record.get("平今费率") or 0) or None,
                    "fee_close_today_per_lot": float(record.get("平今费用/手") or 0) or None,
                    "updated_at": record.get("更新时间"),
                }
            except (TypeError, ValueError):
                continue
        print(json.dumps({
            "site": "openctp", "status": "ok", "contracts": len(records),
            "products": len(reference),
        }))
    except Exception as exc:  # noqa: BLE001 - reference layer must not kill the run
        print(json.dumps({"site": "openctp", "status": "error", "note": str(exc)[:160]}))

    # Cross-check official vs reference on shared products. Official js.dat
    # is per-contract with delivery-month gradient escalation, so a reference
    # value matches when it falls inside the official per-contract range.
    checks = []
    for key, off in official.items():
        ref = reference.get(key)
        tiers = off.get("spec_long_contracts") or []
        if not ref or not ref.get("margin_long") or not tiers:
            continue
        ref_value = ref["margin_long"]
        checks.append({
            "product": key,
            "official_tiers": sorted(tiers),
            "reference": ref_value,
            "match": any(abs(ref_value - tier) < 0.005 for tier in tiers),
        })
    mismatches = [c for c in checks if not c["match"]]

    summary = {
        "day": f"{args.day:%Y-%m-%d}",
        "official_products": sorted(official),
        "reference_products": len(reference),
        "cross_checks": len(checks),
        "mismatches": mismatches,
        "official": official,
        "reference": reference,
    }
    (day_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (OUT_DIR / "latest.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({
        "summary": "ok",
        "official": len(official),
        "checks": len(checks),
        "mismatches": len(mismatches),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
