"""Member position rank collection (每日多空持仓排名采集).

Collects official exchange disclosures of per-member top-20 volume, long
open interest, and short open interest (成交量 / 持买仓量 / 持卖仓量).

Verified endpoints (2026-08):

- SHFE: ``GET https://www.shfe.com.cn/data/tradedata/future/dailydata/pm{YYYYMMDD}.dat``
  JSON ``o_cursor`` rows carry three parallel lists per rank:
  ``CJ1/ABBR1`` = 成交量, ``CJ2/ABBR2`` = 持买仓量, ``CJ3/ABBR3`` = 持卖仓量.
  Includes SHFE products plus INE ``bc``/``lu``/``nr``. ``sc``/``ec``/``wr``
  are not disclosed in this file (confirmed against 2026-08 samples on both
  shfe.com.cn and ine.cn) — a documented gap, never silently filled.
- INE: same ``pm`` format at ``https://www.ine.cn/...`` (covers ``bc``/``lu``/``nr``).
- CFFEX: ``GET http://www.cffex.com.cn/sj/ccpm/{YYYYMM}/{DD}/{PRODUCT}.xml``.
  ``datatypeid`` 0 = 成交量 (double counted), 1 = 持买仓量, 2 = 持卖仓量
  (mapping verified numerically against official daily stats on 2026-08-28).
- GFEX: three POST endpoints under ``http://www.gfex.com.cn/u/...``:
  ``interfacesWebVariety/loadList`` (varieties), ``...MemberDealPosiQuotes/loadListContract_id``
  (contracts per variety/day) and ``...MemberDealPosiQuotes/loadList`` with
  ``data_type`` 1/2/3 (成交量 / 持买 / 持卖). The trailing ``总计`` row becomes
  the aggregate row.
- CZCE: ``https://www.czce.com.cn/cn/DFSStaticFiles/Future/{YYYY}/{YYYYMMDD}/FutureDataHolding.htm``
  fetched through the humanlike browser (WAF); product-aggregate and
  per-contract blocks with three side-by-side member lists.
- DCE: the JSON API ``POST /dcereport/publicweb/dailystat/memberDealPosi``
  returns all three metric lists per variety in one payload, but the endpoint
  is guarded by dynamic WAF tokens. Collection drives the official SPA
  (``/frontend/dcereport/#/zh/memberDealPosiQuotes``) in the humanlike
  browser: the date is typed into the picker and each variety pill is
  clicked, harvesting the responses the page itself requests. The disclosure
  is variety-aggregate (top-20 members per metric plus 合计).

Unknown quantities are never invented; days without disclosure surface as
``unavailable``.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import httpx

METRIC_VOLUME = "volume"
METRIC_LONG = "long"
METRIC_SHORT = "short"

_AGGREGATE_MEMBER = "合计"
_AGGREGATE_RANK = 0

_CFFEX_PRODUCTS: tuple[str, ...] = (
    "IF", "IH", "IC", "IM", "T", "TF", "TL", "TS",
)
_CFFEX_METRIC_BY_TYPE = {"0": METRIC_VOLUME, "1": METRIC_LONG, "2": METRIC_SHORT}
_GFEX_METRIC_BY_TYPE = {"1": METRIC_VOLUME, "2": METRIC_LONG, "3": METRIC_SHORT}

_DEFAULT_TIMEOUT = 30.0
_REQUEST_PAUSE_SECONDS = 0.4
_MAX_ATTEMPTS = 3
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}


class MemberRankError(RuntimeError):
    """A member-rank fetch or parse failure (credential-safe message)."""


class MemberRankParseError(MemberRankError):
    """The official payload did not match the expected disclosure format."""


@dataclass(frozen=True)
class MemberRankRow:
    exchange: str
    trading_day: date
    product_id: str
    instrument_id: str  # 'ALL' for product aggregates, else contract id
    metric: str  # volume | long | short
    rank_no: int  # 1..20; 0 = aggregate/total row
    member_name: str
    quantity: int
    change_qty: int | None = None
    member_id: str | None = None
    basis: str | None = None  # 'double' when the source double counts volume
    source_url: str = ""


@dataclass(frozen=True)
class MemberRankFetchResult:
    exchange: str
    trading_day: date
    status: str  # 'ok' | 'unavailable' | 'error'
    source_urls: tuple[str, ...]
    rows: tuple[MemberRankRow, ...] = field(default_factory=tuple)
    evidence: Mapping[str, bytes] = field(default_factory=dict)  # url -> raw bytes
    note: str | None = None


# ---------------------------------------------------------------------------
# SHFE / INE pm .dat
# ---------------------------------------------------------------------------


def _int_or_none(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _required_int(value: object, context: str) -> int:
    number = _int_or_none(value)
    if number is None:
        raise MemberRankParseError(f"{context}: quantity is not an integer: {value!r}")
    return number


def parse_pm_payload(
    payload: Mapping[str, object],
    *,
    exchange: str,
    trading_day: date,
    source_url: str,
) -> list[MemberRankRow]:
    """Parse a SHFE/INE ``pm{date}.dat`` JSON payload into metric rows.

    RANK semantics in the source file: ``1..20`` are disclosed member rows,
    ``999`` is the instrument total, ``-1`` the brokerage-member subtotal and
    ``0`` the non-brokerage-member subtotal of a product aggregate group.
    All summary rows are stored with ``rank_no=0`` and their source label as
    ``member_name`` so the three summaries stay distinguishable.
    """

    rows_raw = payload.get("o_cursor")
    if not isinstance(rows_raw, list):
        raise MemberRankParseError("pm payload missing o_cursor list")
    rows: list[MemberRankRow] = []
    for index, raw in enumerate(rows_raw):
        if not isinstance(raw, Mapping):
            raise MemberRankParseError(f"pm row {index} is not an object")
        instrument = str(raw.get("INSTRUMENTID") or "").strip().lower()
        if not instrument:
            raise MemberRankParseError(f"pm row {index} missing INSTRUMENTID")
        product_id = instrument[:-3] if instrument.endswith("all") else instrument
        product_id = "".join(ch for ch in product_id if ch.isalpha())
        instrument_id = "ALL" if instrument.endswith("all") else instrument
        rank_raw = _int_or_none(raw.get("RANK"))
        if rank_raw is None:
            raise MemberRankParseError(f"pm row {index} missing RANK")
        if 1 <= rank_raw <= 20:
            rank_no, summary = rank_raw, False
        elif rank_raw == 999:
            rank_no, summary = _AGGREGATE_RANK, True
        elif rank_raw in (-1, 0):
            rank_no, summary = _AGGREGATE_RANK, True
        else:
            raise MemberRankParseError(
                f"pm row {index} unexpected rank {rank_raw!r}"
            )
        for suffix, metric in (
            ("1", METRIC_VOLUME), ("2", METRIC_LONG), ("3", METRIC_SHORT),
        ):
            member = str(raw.get(f"PARTICIPANTABBR{suffix}") or "").strip()
            raw_quantity = raw.get(f"CJ{suffix}")
            if not member and (raw_quantity is None or str(raw_quantity) == ""):
                # A disclosure list can be shorter than 20 members; the
                # trailing cells of that metric are blank in the source.
                continue
            quantity = _required_int(
                raw_quantity, f"pm row {index} CJ{suffix}",
            )
            if summary:
                # 999 rows have an empty label; subtotal rows carry their own.
                member = member or _AGGREGATE_MEMBER
            elif not member:
                raise MemberRankParseError(
                    f"pm row {index} missing PARTICIPANTABBR{suffix}"
                )
            rows.append(MemberRankRow(
                exchange=exchange,
                trading_day=trading_day,
                product_id=product_id,
                instrument_id=instrument_id,
                metric=metric,
                rank_no=rank_no,
                member_name=member,
                quantity=quantity,
                change_qty=_int_or_none(raw.get(f"CJ{suffix}_CHG")),
                member_id=_pm_member_id(raw.get(f"PARTICIPANTID{suffix}")),
                source_url=source_url,
            ))
    return rows


def _pm_member_id(value: object) -> str | None:
    """Subtotal rows repeat their long label in the id field; keep real ids."""

    text = str(value or "").strip()
    if text and len(text) <= 16 and text.isascii() and text.isalnum():
        return text
    return None


def fetch_pm_rank(
    day: date,
    *,
    exchange: str,
    base_url: str,
    client: httpx.Client | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> MemberRankFetchResult:
    """Fetch one day of SHFE- or INE-style ``pm`` ranking data."""

    url = f"{base_url.rstrip('/')}/data/tradedata/future/dailydata/pm{day:%Y%m%d}.dat"
    own_client = client is None
    if own_client:
        client = httpx.Client(headers={**_BROWSER_HEADERS, "Referer": f"{base_url.rstrip('/')}/"}, timeout=timeout)
    assert client is not None
    try:
        response = _request_with_retry(client, url)
        if response.status_code == 404:
            return MemberRankFetchResult(
                exchange=exchange, trading_day=day, status="unavailable",
                source_urls=(url,), note="no pm disclosure for this day",
            )
        if response.status_code != 200:
            raise MemberRankError(f"pm endpoint returned HTTP {response.status_code}")
        payload = json.loads(response.content.decode("utf-8"))
        rows = parse_pm_payload(
            payload, exchange=exchange, trading_day=day, source_url=url,
        )
        return MemberRankFetchResult(
            exchange=exchange, trading_day=day, status="ok",
            source_urls=(url,), rows=tuple(rows),
            evidence={url: response.content},
        )
    finally:
        if own_client:
            client.close()


def fetch_shfe_rank(day: date, **kwargs: object) -> MemberRankFetchResult:
    return fetch_pm_rank(
        day, exchange="SHFE", base_url="https://www.shfe.com.cn", **kwargs
    )


def fetch_ine_rank(day: date, **kwargs: object) -> MemberRankFetchResult:
    """Fetch INE ranking; only bc/lu/nr are disclosed in ine pm files."""

    result = fetch_pm_rank(
        day, exchange="INE", base_url="https://www.ine.cn", **kwargs
    )
    if result.status != "ok":
        return result
    covered = ",".join(sorted({row.product_id for row in result.rows}))
    return MemberRankFetchResult(
        exchange=result.exchange,
        trading_day=result.trading_day,
        status=result.status,
        source_urls=result.source_urls,
        rows=result.rows,
        evidence=result.evidence,
        note=(
            f"products disclosed: {covered}; sc/ec ranking is not published "
            "in ine pm files (known gap)"
        ),
    )


# ---------------------------------------------------------------------------
# CFFEX XML
# ---------------------------------------------------------------------------


def parse_cffex_xml(
    text: str,
    *,
    trading_day: date,
    source_url: str,
) -> list[MemberRankRow]:
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise MemberRankParseError(f"cffex xml not parseable: {exc}") from exc
    rows: list[MemberRankRow] = []
    for node in root.findall("data"):
        instrument = (node.findtext("instrumentid") or "").strip()
        datatype = (node.findtext("datatypeid") or "").strip()
        metric = _CFFEX_METRIC_BY_TYPE.get(datatype)
        rank_no = _int_or_none(node.findtext("rank"))
        member = (node.findtext("shortname") or "").strip()
        quantity = _required_int(node.findtext("volume"), "cffex volume")
        if not instrument:
            raise MemberRankParseError("cffex row missing instrumentid")
        if metric is None:
            raise MemberRankParseError(f"cffex row unknown datatypeid {datatype!r}")
        if rank_no is None or not (1 <= rank_no <= 20):
            raise MemberRankParseError(f"cffex row unexpected rank {rank_no!r}")
        if not member:
            raise MemberRankParseError("cffex row missing shortname")
        rows.append(MemberRankRow(
            exchange="CFFEX",
            trading_day=trading_day,
            product_id=instrument[:2],
            instrument_id=instrument,
            metric=metric,
            rank_no=rank_no,
            member_name=member,
            quantity=quantity,
            change_qty=_int_or_none(node.findtext("varvolume")),
            member_id=(node.findtext("partyid") or "").strip() or None,
            basis="double" if metric == METRIC_VOLUME else None,
            source_url=source_url,
        ))
    if not rows:
        raise MemberRankParseError("cffex xml contained no rank rows")
    return rows


def fetch_cffex_rank(
    day: date,
    *,
    products: Sequence[str] = _CFFEX_PRODUCTS,
    client: httpx.Client | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> MemberRankFetchResult:
    """Fetch one day of CFFEX per-product ranking XML files."""

    own_client = client is None
    if own_client:
        client = httpx.Client(headers=_BROWSER_HEADERS, timeout=timeout)
    assert client is not None
    urls: list[str] = []
    rows: list[MemberRankRow] = []
    evidence: dict[str, bytes] = {}
    try:
        for product in products:
            url = (
                f"http://www.cffex.com.cn/sj/ccpm/{day:%Y%m}/{day:%d}/{product}.xml"
            )
            response = _request_with_retry(client, url)
            if response.status_code in (302, 404):
                # CFFEX redirects to an error page for days without disclosure.
                continue
            if response.status_code != 200:
                raise MemberRankError(
                    f"cffex {product} returned HTTP {response.status_code}"
                )
            text = response.content.decode("utf-8", errors="strict")
            rows.extend(parse_cffex_xml(text, trading_day=day, source_url=url))
            urls.append(url)
            evidence[url] = response.content
            time.sleep(_REQUEST_PAUSE_SECONDS)
        if not rows:
            return MemberRankFetchResult(
                exchange="CFFEX", trading_day=day, status="unavailable",
                source_urls=tuple(urls), note="no cffex xml disclosure for this day",
            )
        return MemberRankFetchResult(
            exchange="CFFEX", trading_day=day, status="ok",
            source_urls=tuple(urls), rows=tuple(rows), evidence=evidence,
        )
    finally:
        if own_client:
            client.close()


# ---------------------------------------------------------------------------
# GFEX POST JSON
# ---------------------------------------------------------------------------


def parse_gfex_payload(
    payload: Mapping[str, object],
    *,
    product_id: str,
    instrument_id: str,
    data_type: str,
    trading_day: date,
    source_url: str = "",
) -> list[MemberRankRow]:
    metric = _GFEX_METRIC_BY_TYPE.get(data_type)
    if metric is None:
        raise MemberRankParseError(f"gfex unknown data_type {data_type!r}")
    rows_raw = payload.get("data")
    if not isinstance(rows_raw, list) or not rows_raw:
        raise MemberRankParseError(f"gfex {instrument_id} data_type {data_type} empty")
    rows: list[MemberRankRow] = []
    for index, raw in enumerate(rows_raw):
        if not isinstance(raw, Mapping):
            raise MemberRankParseError(f"gfex row {index} not an object")
        marker = str(raw.get("contractId") or "").strip()
        is_total = marker in {"总计", "Total", "total", "0"}
        if is_total:
            rank_no, member = _AGGREGATE_RANK, _AGGREGATE_MEMBER
        else:
            rank_no = _int_or_none(marker)
            member = str(raw.get("abbr") or "").strip()
            if rank_no is None or not (1 <= rank_no <= 20):
                raise MemberRankParseError(f"gfex row {index} bad rank {marker!r}")
            if not member:
                raise MemberRankParseError(f"gfex row {index} missing abbr")
        rows.append(MemberRankRow(
            exchange="GFEX",
            trading_day=trading_day,
            product_id=product_id,
            instrument_id=instrument_id,
            metric=metric,
            rank_no=rank_no,
            member_name=member,
            quantity=_required_int(raw.get("todayQty"), f"gfex row {index} todayQty"),
            change_qty=_int_or_none(raw.get("qtySub")),
            member_id=str(raw.get("memberId") or "").strip() or None,
            source_url=source_url,
        ))
    return rows


def fetch_gfex_rank(
    day: date,
    *,
    varieties: Sequence[str] | None = None,
    client: httpx.Client | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> MemberRankFetchResult:
    """Fetch one day of GFEX ranking via the official POST interfaces."""

    base = "http://www.gfex.com.cn/u/interfacesWebTiMemberDealPosiQuotes"
    own_client = client is None
    if own_client:
        client = httpx.Client(
            headers={**_BROWSER_HEADERS, "Referer": "http://www.gfex.com.cn/"},
            timeout=timeout,
        )
    assert client is not None
    try:
        if varieties is None:
            varieties = _gfex_varieties(client)
        rows: list[MemberRankRow] = []
        urls: list[str] = []
        evidence: dict[str, bytes] = {}
        for variety in varieties:
            contracts = _gfex_contracts(client, variety, day)
            time.sleep(_REQUEST_PAUSE_SECONDS)
            for contract in contracts:
                for data_type in ("1", "2", "3"):
                    url = f"{base}/loadList"
                    response = _request_with_retry(
                        client, url,
                        data={
                            "variety": variety,
                            "trade_date": f"{day:%Y%m%d}",
                            "trade_type": "0",
                            "contract_id": contract,
                            "data_type": data_type,
                        },
                    )
                    if response.status_code != 200:
                        raise MemberRankError(
                            f"gfex {contract} data_type {data_type} "
                            f"HTTP {response.status_code}"
                        )
                    payload = json.loads(response.content.decode("utf-8"))
                    if str(payload.get("code")) != "0":
                        # GFEX rejects non-disclosed contracts (volume threshold)
                        # with code '1'; treat as absent rather than failure.
                        continue
                    rows.extend(parse_gfex_payload(
                        payload, product_id=variety, instrument_id=contract,
                        data_type=data_type, trading_day=day, source_url=url,
                    ))
                    evidence[f"{url}#{contract}/{data_type}"] = response.content
                    time.sleep(_REQUEST_PAUSE_SECONDS)
            urls.append(f"{base}/loadList#{variety}")
        if not rows:
            return MemberRankFetchResult(
                exchange="GFEX", trading_day=day, status="unavailable",
                source_urls=tuple(urls), note="no gfex disclosure for this day",
            )
        return MemberRankFetchResult(
            exchange="GFEX", trading_day=day, status="ok",
            source_urls=tuple(urls), rows=tuple(rows), evidence=evidence,
        )
    finally:
        if own_client:
            client.close()


def _gfex_varieties(client: httpx.Client) -> list[str]:
    response = _request_with_retry(
        client, "http://www.gfex.com.cn/u/interfacesWebVariety/loadList",
        data={"trade_type": "0"},
    )
    if response.status_code != 200:
        raise MemberRankError(f"gfex variety list HTTP {response.status_code}")
    try:
        payload = json.loads(response.content.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise MemberRankError("gfex variety list invalid JSON") from exc
    if not isinstance(payload, Mapping) or str(payload.get("code")) != "0":
        raise MemberRankError("gfex variety list invalid status")
    data = payload.get("data")
    if not isinstance(data, list):
        raise MemberRankError("gfex variety list missing data")
    if not data:
        raise MemberRankError("gfex variety list empty")
    varieties: list[str] = []
    for item in data:
        variety = item.get("varietyId") if isinstance(item, Mapping) else None
        if not isinstance(variety, str) or not variety.strip():
            raise MemberRankError("gfex variety list invalid varietyId")
        varieties.append(variety.strip())
    return varieties


def _gfex_contracts(client: httpx.Client, variety: str, day: date) -> list[str]:
    response = _request_with_retry(
        client,
        "http://www.gfex.com.cn/u/interfacesWebTiMemberDealPosiQuotes/loadListContract_id",
        data={
            "variety": variety,
            "trade_date": f"{day:%Y%m%d}",
            "trade_type": "0",
        },
    )
    if response.status_code != 200:
        raise MemberRankError(f"gfex contract list HTTP {response.status_code}")
    payload = json.loads(response.content.decode("utf-8"))
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    return [str(item) for item in data if item]


# ---------------------------------------------------------------------------
# CZCE htm via humanlike browser
# ---------------------------------------------------------------------------


class _CzceTableParser(HTMLParser):
    """Extract product/contract blocks from FutureDataHolding.htm."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict] = []
        self._current: dict | None = None
        self._cells: list[str] = []
        self._capture_header = False

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag == "td":
            self._capture_header = True
            self._cells.append("")

    def handle_endtag(self, tag: str) -> None:
        if tag == "td":
            self._capture_header = False
        elif tag == "tr" and self._cells:
            self._finish_row()
        elif tag == "table" and self._current is not None:
            self.blocks.append(self._current)
            self._current = None

    def handle_data(self, data: str) -> None:
        if self._capture_header and self._cells:
            self._cells[-1] += data

    def _finish_row(self) -> None:
        cells = [cell.strip() for cell in self._cells]
        self._cells = []
        joined = "".join(cells)
        if joined[:3].startswith("品种") or joined[:3].startswith("合约"):
            # The real disclosure is one big table: flush the previous block
            # when the next block header arrives instead of overwriting it.
            if self._current is not None and self._current["rows"]:
                self.blocks.append(self._current)
            self._current = {"header": joined, "rows": []}
            return
        if self._current is not None and cells and cells[0]:
            self._current["rows"].append(cells)


def parse_czce_htm(
    html: str,
    *,
    trading_day: date,
    source_url: str = "",
) -> list[MemberRankRow]:
    parser = _CzceTableParser()
    try:
        parser.feed(html)
        parser.close()
    except AssertionError:
        # Malformed markup segments must not abort the whole disclosure file.
        pass
    if parser._current is not None:  # unterminated final table
        parser.blocks.append(parser._current)
    rows: list[MemberRankRow] = []
    for block in parser.blocks:
        header = block["header"].replace("\u00a0", " ")
        kind = "product" if header.startswith("品种") else "contract"
        if kind == "product":
            code = _czce_product_code(header)
            instrument_id = "ALL"
        else:
            code = _czce_contract_code(header)
            instrument_id = code.upper()
        if not code:
            continue
        product_id = _czce_normalize_product(code)
        for cells in block["rows"]:
            if len(cells) != 10:
                continue
            rank_no = _int_or_none(cells[0])
            if rank_no is None or not (1 <= rank_no <= 20):
                continue
            for member_cell, qty_cell, chg_cell, metric in (
                (1, 2, 3, METRIC_VOLUME),
                (4, 5, 6, METRIC_LONG),
                (7, 8, 9, METRIC_SHORT),
            ):
                member = cells[member_cell]
                if not member:
                    continue
                quantity = _int_or_none(cells[qty_cell])
                if quantity is None:
                    continue
                rows.append(MemberRankRow(
                    exchange="CZCE",
                    trading_day=trading_day,
                    product_id=product_id,
                    instrument_id=instrument_id,
                    metric=metric,
                    rank_no=rank_no,
                    member_name=member,
                    quantity=quantity,
                    change_qty=_int_or_none(cells[chg_cell]),
                    source_url=source_url,
                ))
    if not rows:
        raise MemberRankParseError("czce htm contained no rank rows")
    return rows


def _czce_product_code(header: str) -> str:
    tail = ""
    for ch in reversed(header.replace(" ", "")):
        if ch.isascii() and ch.isalpha():
            tail = ch + tail
        elif tail:
            break
    return tail[:3]


# CZCE block headers carry display names; PTA's product code is TA.
_CZCE_PRODUCT_CODE_OVERRIDES = {"PTA": "TA"}


def _czce_normalize_product(code: str) -> str:
    upper = code.upper()
    letters = "".join(ch for ch in upper if ch.isalpha())
    return _CZCE_PRODUCT_CODE_OVERRIDES.get(letters, letters)


def _czce_contract_code(header: str) -> str:
    token = ""
    for ch in header.replace(" ", "").replace("\u00a0", ""):
        if ch.isascii() and (ch.isalnum() or ch in "()"):
            token += ch
        elif token:
            break
    letters = "".join(ch for ch in token if ch.isalpha())
    digits = "".join(ch for ch in token if ch.isdigit())
    return (letters + digits).lower() if letters and digits else ""


_CZCE_BULK_SCRIPT = r"""
import json, sys, time
from pathlib import Path
urls_json, profile, out_dir = sys.argv[1], sys.argv[2], sys.argv[3]
targets = json.loads(urls_json)
from scrapling.fetchers import StealthySession
results = []
with StealthySession(headless=True, real_chrome=True, user_data_dir=profile,
                     block_webrtc=True, hide_canvas=True, locale='zh-CN',
                     timeout=120000, google_search=False) as session:
    home = session.fetch('https://www.czce.com.cn/', network_idle=True, wait=2000)
    time.sleep(1.0)
    home_status = getattr(home, 'status', None)
    for item in targets:
        day, url = item['day'], item['url']
        # Resume support: a capture from an earlier (possibly timed-out) run
        # is kept instead of re-fetched.
        existing = Path(item['path'])
        if existing.is_file() and existing.stat().st_size > 1000:
            results.append({'day': day, 'status': 200, 'bytes': existing.stat().st_size, 'resumed': True})
            continue
        try:
            target = session.fetch(url, network_idle=True, wait=2500)
            status = getattr(target, 'status', None)
            Path(item['path']).write_bytes(target.body)
            results.append({'day': day, 'status': status, 'bytes': len(target.body)})
        except Exception as exc:
            results.append({'day': day, 'status': 'error', 'note': str(exc)[:160]})
        time.sleep(1.0)
print(json.dumps({'home_status': home_status, 'results': results}))
"""


def fetch_czce_rank_bulk(
    days: Sequence[date],
    *,
    profile_directory: Path | None = None,
    subprocess_timeout: float = 3000.0,
) -> dict[date, MemberRankFetchResult]:
    """Fetch several days of CZCE ranking inside one humanlike session.

    Browser startup dominates single-day fetches, so historical backfill runs
    all days of a range through one persistent session.
    """

    if not days:
        return {}
    if not _SCRAPLING_PYTHON.is_file():
        raise MemberRankError(
            "czce collection requires the local Scrapling interpreter "
            f"(expected at {_SCRAPLING_PYTHON})"
        )
    if profile_directory is None:
        profile_directory = Path("data/rank_probe/czce_profile")
    profile_directory.mkdir(parents=True, exist_ok=True)
    targets = []
    for day in days:
        url = (
            f"https://www.czce.com.cn/cn/DFSStaticFiles/Future/"
            f"{day:%Y}/{day:%Y%m%d}/FutureDataHolding.htm"
        )
        targets.append({
            "day": f"{day:%Y%m%d}",
            "url": url,
            "path": str(profile_directory / f"czce_{day:%Y%m%d}.htm"),
        })
    # One subprocess call per chunk keeps the command line bounded. A chunk
    # that times out is not fatal: days whose capture file already exists are
    # parsed anyway and the rest are marked resumable-incomplete, so the next
    # run picks up where this one stopped.
    results: dict[date, MemberRankFetchResult] = {}
    chunk_size = 30
    for start in range(0, len(targets), chunk_size):
        chunk = targets[start:start + chunk_size]
        chunk_days = days[start:start + chunk_size]
        command = [
            str(_SCRAPLING_PYTHON), "-c", _CZCE_BULK_SCRIPT,
            json.dumps(chunk), str(profile_directory), str(profile_directory),
        ]
        status_payload: Mapping[str, object] = {}
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=subprocess_timeout,
            )
            if completed.returncode == 0:
                lines = [
                    line
                    for line in (completed.stdout or "").splitlines()
                    if line.strip()
                ]
                if lines:
                    try:
                        status_payload = json.loads(lines[-1])
                    except json.JSONDecodeError:
                        status_payload = {}
        except subprocess.TimeoutExpired:
            status_payload = {}
        by_day = {
            item.get("day"): item
            for item in (status_payload.get("results") or [])
            if isinstance(item, Mapping)
        }
        for day, target in zip(chunk_days, chunk):
            item = by_day.get(f"{day:%Y%m%d}", {})
            status = item.get("status")
            url = target["url"]
            path = Path(target["path"])
            if not path.is_file():
                results[day] = MemberRankFetchResult(
                    exchange="CZCE", trading_day=day, status="error",
                    source_urls=(url,),
                    note="czce bulk incomplete for this day (resumable)",
                )
                continue
            if status == 404 or path.stat().st_size < 1000:
                results[day] = MemberRankFetchResult(
                    exchange="CZCE", trading_day=day, status="unavailable",
                    source_urls=(url,),
                    note="no czce holding file for this day",
                )
                continue
            if status not in (200, None):
                results[day] = MemberRankFetchResult(
                    exchange="CZCE", trading_day=day, status="error",
                    source_urls=(url,), note=f"czce bulk status {status!r}",
                )
                continue
            raw = path.read_bytes()
            try:
                rows = parse_czce_htm(
                    raw.decode("utf-8", errors="replace"), trading_day=day,
                    source_url=url,
                )
            except MemberRankParseError as exc:
                results[day] = MemberRankFetchResult(
                    exchange="CZCE", trading_day=day, status="error",
                    source_urls=(url,), note=str(exc),
                )
                continue
            results[day] = MemberRankFetchResult(
                exchange="CZCE", trading_day=day, status="ok",
                source_urls=(url,), rows=tuple(rows), evidence={url: raw},
            )
    return results


# Humanlike CZCE fetcher runs inside the Scrapling interpreter so compiled
# browser dependencies resolve; it writes raw response bytes to a file.
_SCRAPLING_PYTHON = Path.home() / ".scrapling" / "Scripts" / "python.exe"

_CZCE_SCRIPT = r"""
import json, sys, time
from pathlib import Path
url, homepage, profile, out_path = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
from scrapling.fetchers import StealthySession
with StealthySession(headless=True, real_chrome=True, user_data_dir=profile,
                     block_webrtc=True, hide_canvas=True, locale='zh-CN',
                     timeout=120000, google_search=False) as session:
    home = session.fetch(homepage, network_idle=True, wait=2000)
    time.sleep(1.0)
    target = session.fetch(url, network_idle=True, wait=2500)
    Path(out_path).write_bytes(target.body)
    print(json.dumps({'home_status': getattr(home, 'status', None),
                      'status': getattr(target, 'status', None),
                      'bytes': len(target.body)}))
"""


def fetch_czce_rank(
    day: date,
    *,
    profile_directory: Path | None = None,
    subprocess_timeout: float = 240.0,
) -> MemberRankFetchResult:
    """Fetch one day of CZCE ranking via the humanlike browser."""

    url = (
        f"https://www.czce.com.cn/cn/DFSStaticFiles/Future/"
        f"{day:%Y}/{day:%Y%m%d}/FutureDataHolding.htm"
    )
    if not _SCRAPLING_PYTHON.is_file():
        raise MemberRankError(
            "czce collection requires the local Scrapling interpreter "
            f"(expected at {_SCRAPLING_PYTHON})"
        )
    if profile_directory is None:
        profile_directory = Path("data/rank_probe/czce_profile")
    profile_directory.mkdir(parents=True, exist_ok=True)
    out_path = profile_directory / f"czce_{day:%Y%m%d}.htm"
    command = [
        str(_SCRAPLING_PYTHON), "-c", _CZCE_SCRIPT, url,
        "https://www.czce.com.cn/", str(profile_directory), str(out_path),
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=subprocess_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise MemberRankError("czce humanlike fetch timed out") from exc
    if completed.returncode != 0:
        raise MemberRankError(f"czce humanlike fetch exit {completed.returncode}")
    lines = [line for line in (completed.stdout or "").splitlines() if line.strip()]
    if not lines:
        raise MemberRankError("czce humanlike fetch produced no status output")
    try:
        status_payload = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise MemberRankError("czce humanlike fetch produced invalid status") from exc
    target_status = status_payload.get("status")
    if target_status == 404:
        return MemberRankFetchResult(
            exchange="CZCE", trading_day=day, status="unavailable",
            source_urls=(url,), note="no czce holding file for this day",
        )
    if target_status != 200 or not out_path.is_file():
        raise MemberRankError(f"czce humanlike fetch status {target_status!r}")
    raw = out_path.read_bytes()
    html = raw.decode("utf-8", errors="replace")
    rows = parse_czce_htm(html, trading_day=day, source_url=url)
    return MemberRankFetchResult(
        exchange="CZCE", trading_day=day, status="ok", source_urls=(url,),
        rows=tuple(rows), evidence={url: raw},
    )


# ---------------------------------------------------------------------------
# Shared HTTP retry helper
# ---------------------------------------------------------------------------


def _request_with_retry(
    client: httpx.Client,
    url: str,
    *,
    data: Mapping[str, str] | None = None,
) -> httpx.Response:
    last_error: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            if data is None:
                response = client.get(url)
            else:
                response = client.post(url, data=dict(data))
            if response.status_code in (429, 500, 502, 503, 504):
                last_error = MemberRankError(f"HTTP {response.status_code} from {url}")
            else:
                return response
        except (httpx.HTTPError, OSError) as exc:
            last_error = exc
        time.sleep(_REQUEST_PAUSE_SECONDS * attempt)
    raise MemberRankError(f"request failed after {_MAX_ATTEMPTS} attempts: {last_error}")


# ---------------------------------------------------------------------------
# DCE SPA via humanlike browser (UI-driven, WAF tokens come from the page)
# ---------------------------------------------------------------------------

_DCE_VARIETY_NAMES: tuple[tuple[str, str], ...] = (
    ("a", "豆一"), ("b", "豆二"), ("m", "豆粕"), ("y", "豆油"),
    ("p", "棕榈油"), ("c", "玉米"), ("cs", "玉米淀粉"), ("rr", "粳米"),
    ("jd", "鸡蛋"), ("lh", "生猪"), ("fb", "纤维板"), ("bb", "胶合板"),
    ("lg", "原木"), ("j", "焦炭"), ("jm", "焦煤"), ("i", "铁矿石"),
    ("l", "聚乙烯"), ("v", "聚氯乙烯"), ("pp", "聚丙烯"), ("eg", "乙二醇"),
    ("eb", "苯乙烯"), ("pg", "液化石油气"),
)
_DCE_APP_URL = (
    "http://www.dce.com.cn/frontend/dcereport/#/zh/memberDealPosiQuotes"
    "?tradeType=0&variety=a"
)

_DCE_SCRIPT = r"""
import json, sys, time
from pathlib import Path
day_text, varieties_json, profile, out_path = (
    sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
)
varieties = json.loads(varieties_json)
from scrapling.fetchers import StealthySession
responses = []
def setup(page):
    def on_resp(resp):
        if 'memberDealPosi' not in resp.url:
            return
        try:
            body = resp.text()
        except Exception:
            return
        responses.append(body)
    page.on('response', on_resp)

def set_date(page):
    page.evaluate(r'''() => {
      const inp = [...document.querySelectorAll('input')].find(i => /20\d{2}/.test(i.value||''));
      if (inp) { inp.focus(); inp.click(); }
    }''')
    time.sleep(1.0)
    page.keyboard.press('Control+a')
    page.keyboard.type(day_text, delay=60)
    time.sleep(0.4)
    page.keyboard.press('Enter')

def action(page):
    time.sleep(8)
    records = []
    target_compact = day_text.replace('-', '')
    for code, name in varieties:
        page.evaluate(
            '(v) => { location.hash = '
            '"#/zh/memberDealPosiQuotes?tradeType=0&variety=" + v; }',
            code,
        )
        # A hash-only change does not remount the SPA; force a reload so the
        # route parameter takes effect, then re-enter the date.
        try:
            page.reload(wait_until='domcontentloaded')
        except Exception:
            pass
        time.sleep(6)
        seen = len(responses)
        entered = False
        for _attempt in range(3):
            set_date(page)
            time.sleep(3.5)
            shown = page.evaluate(
                r'() => (document.body.innerText.match('
                r'/查询日期[：:]\s*(\d+)/) || [])[1]'
            )
            if shown == target_compact:
                entered = True
                break
            time.sleep(2)
        time.sleep(2.5)
        fresh = responses[seen:]
        records.append(
            {'variety': code, 'name': name, 'entered': entered,
             'responses': fresh}
        )
    Path(out_path).write_text(
        json.dumps(records, ensure_ascii=False), encoding='utf-8'
    )
    print(json.dumps({'records': len(records), 'responses': len(responses)}))

with StealthySession(headless=True, real_chrome=True, user_data_dir=profile,
                     block_webrtc=True, hide_canvas=True, locale='zh-CN',
                     timeout=600000, google_search=False) as session:
    session.fetch(
        'http://www.dce.com.cn/frontend/dcereport/'
        '#/zh/memberDealPosiQuotes?tradeType=0&variety=a',
        network_idle=True, wait=6000, page_setup=setup, page_action=action,
    )
"""


def parse_dce_records(
    records: Sequence[Mapping[str, object]],
    *,
    trading_day: date,
    source_url: str = _DCE_APP_URL,
) -> list[MemberRankRow]:
    """Parse captured DCE memberDealPosi responses into metric rows."""

    rows: list[MemberRankRow] = []
    for record in records:
        variety = str(record.get("variety") or "")
        if not variety:
            continue
        responses = record.get("responses") or []
        payload = None
        for raw in responses:
            if not isinstance(raw, str):
                continue
            try:
                candidate = json.loads(raw)
            except json.JSONDecodeError:
                continue
            data = candidate.get("data")
            if isinstance(data, Mapping) and data.get("qtyFutureList"):
                payload = data
                break
        if payload is None:
            continue
        for list_key, metric in (
            ("qtyFutureList", METRIC_VOLUME),
            ("buyFutureList", METRIC_LONG),
            ("sellFutureList", METRIC_SHORT),
        ):
            for entry in payload.get(list_key) or []:
                if not isinstance(entry, Mapping):
                    continue
                rank_text = str(entry.get("rank") or "").strip()
                is_total = rank_text in {"合计", "Total", "total", ""}
                rank_no = _AGGREGATE_RANK if is_total else _int_or_none(rank_text)
                if rank_no is None or not (0 <= rank_no <= 20):
                    continue
                member = str(entry.get("qtyAbbr") or "").strip() or _AGGREGATE_MEMBER
                quantity = _int_or_none(entry.get("todayQty"))
                if quantity is None:
                    continue
                rows.append(MemberRankRow(
                    exchange="DCE",
                    trading_day=trading_day,
                    product_id=variety,
                    instrument_id="ALL",
                    metric=metric,
                    rank_no=rank_no,
                    member_name=member,
                    quantity=quantity,
                    change_qty=_int_or_none(entry.get("qtySub")),
                    source_url=source_url,
                ))
    if not rows:
        raise MemberRankParseError("dce responses contained no rank rows")
    return rows


def fetch_dce_rank(
    day: date,
    *,
    varieties: Sequence[tuple[str, str]] = _DCE_VARIETY_NAMES,
    profile_directory: Path | None = None,
    subprocess_timeout: float = 900.0,
) -> MemberRankFetchResult:
    """Fetch one day of DCE ranking by driving the official SPA in a browser.

    The Ruishu WAF rate-limits this SPA aggressively; each run uses a fresh
    browser profile, and heavy DCE probing from one address can require a
    cooldown before the app serves queries again.
    """

    if not _SCRAPLING_PYTHON.is_file():
        raise MemberRankError(
            "dce collection requires the local Scrapling interpreter "
            f"(expected at {_SCRAPLING_PYTHON})"
        )
    if profile_directory is None:
        profile_directory = Path("data/rank_probe/dce_profiles") / f"{day:%Y%m%d}"
    profile_directory.mkdir(parents=True, exist_ok=True)
    out_path = profile_directory / f"dce_{day:%Y%m%d}.json"
    command = [
        str(_SCRAPLING_PYTHON), "-c", _DCE_SCRIPT,
        f"{day:%Y-%m-%d}", json.dumps(list(varieties)),
        str(profile_directory), str(out_path),
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=subprocess_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise MemberRankError("dce humanlike fetch timed out") from exc
    if completed.returncode != 0 or not out_path.is_file():
        raise MemberRankError(f"dce humanlike fetch exit {completed.returncode}")
    try:
        records = json.loads(out_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        raise MemberRankError("dce capture file not readable") from exc
    entered = [r for r in records if isinstance(r, Mapping) and r.get("entered")]
    if not entered:
        return MemberRankFetchResult(
            exchange="DCE", trading_day=day, status="unavailable",
            source_urls=(_DCE_APP_URL,),
            note="dce date picker never accepted the target day",
        )
    try:
        rows = parse_dce_records(records, trading_day=day)
    except MemberRankParseError:
        return MemberRankFetchResult(
            exchange="DCE", trading_day=day, status="unavailable",
            source_urls=(_DCE_APP_URL,),
            note="dce disclosure empty for this day (non-trading or none)",
        )
    evidence = {
        _DCE_APP_URL: out_path.read_bytes(),
    }
    covered = ",".join(sorted({row.product_id for row in rows}))
    return MemberRankFetchResult(
        exchange="DCE", trading_day=day, status="ok",
        source_urls=(_DCE_APP_URL,), rows=tuple(rows), evidence=evidence,
        note=f"variety-aggregate disclosure; products: {covered}",
    )


FETCHERS = {
    "SHFE": fetch_shfe_rank,
    "INE": fetch_ine_rank,
    "CFFEX": fetch_cffex_rank,
    "GFEX": fetch_gfex_rank,
    "CZCE": fetch_czce_rank,
    "DCE": fetch_dce_rank,
}


def collect_day(
    day: date,
    *,
    exchanges: Iterable[str] | None = None,
) -> dict[str, MemberRankFetchResult]:
    """Collect a day, isolating known fetch/parse failures as ``error`` results.

    Unexpected programming errors propagate to the caller.
    """

    selected = tuple(exchanges) if exchanges is not None else tuple(FETCHERS)
    results: dict[str, MemberRankFetchResult] = {}
    for exchange in selected:
        fetcher = FETCHERS.get(exchange)
        if fetcher is None:
            raise MemberRankError(f"no member-rank fetcher for {exchange!r}")
        try:
            results[exchange] = fetcher(day)
        except (
            MemberRankError, json.JSONDecodeError, UnicodeDecodeError,
            ElementTree.ParseError, httpx.HTTPError,
        ) as exc:
            # Exception messages may contain response data or credentialed URLs.
            results[exchange] = MemberRankFetchResult(
                exchange=exchange, trading_day=day, status="error",
                source_urls=(), note=f"member-rank fetch failed: {type(exc).__name__}",
            )
    return results
