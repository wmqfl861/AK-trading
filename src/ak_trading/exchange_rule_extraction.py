"""Deterministic extraction of rule values from retained official Markdown.

The extractor deliberately recognizes only exact labels and concrete value forms.
It does not infer values from page position, search snippets, navigation text, or
template placeholders.  Its text-valued result type is adapted to the existing
``RuleItem`` text convention by :mod:`ak_trading.exchange_rule_sources`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Iterable, Mapping, Sequence

from ak_trading.exchange_rules import (
    FeeBasis,
    RuleCategory,
    RuleStatus,
    RuleValueKind,
    SourceDocument,
)


AUTO_EXTRACTION_NOTE = (
    "auto: deterministic extraction; per-field manual review waived by user "
    "2026-08-20"
)

_TEMPLATE_RE = re.compile(r"\{\{.*?\}\}", re.DOTALL)
_NUMBER_PATTERN = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_PERCENT_RE = re.compile(rf"(?P<number>{_NUMBER_PATTERN})\s*[%％]")
_CNY_PER_UNIT_RE = re.compile(
    rf"(?P<number>{_NUMBER_PATTERN})\s*元"
    r"(?:\s*[（(]人民币[）)])?\s*(?:/|每)\s*"
    r"(?P<unit>手|吨|克|千克|公斤|点)"
)
_PER_LOT_RE = re.compile(
    rf"(?P<number>{_NUMBER_PATTERN})\s*"
    r"(?P<unit>吨|克|千克|公斤)\s*(?:/|每)\s*手"
)
_LOT_RE = re.compile(rf"(?P<number>{_NUMBER_PATTERN})\s*手")
_TEN_THOUSAND_RE = re.compile(rf"万分之\s*(?P<number>{_NUMBER_PATTERN})")
_TIME_RE = re.compile(r"(?<!\d)(?:[01]?\d|2[0-3])\s*[:：]\s*[0-5]\d(?!\d)")
_DAY_RE = re.compile(
    r"(?:\d+|[一二三四五六七八九十百两]+)\s*(?:个)?"
    r"(?:交易日|工作日|日|天)"
)
_DATE_TOKEN = (
    r"(?P<year>20\d{2})\s*(?:年|[-/.])\s*"
    r"(?P<month>0?[1-9]|1[0-2])\s*(?:月|[-/.])\s*"
    r"(?P<day>0?[1-9]|[12]\d|3[01])\s*日?"
)
_EFFECTIVE_DATE_RE = re.compile(
    rf"(?:(?:自|于|从|公告自|通知自)\s*)?{_DATE_TOKEN}"
    r"(?:[^\n。；;]{0,24})?(?:起|开始|执行|实施|生效|适用)"
)
_WEEKDAY_RE = re.compile(
    r"(?:第(?:\d+|[一二三四五六七八九十百两]+)个)?"
    r"(?:星期|周)[一二三四五六日天]"
)
_CONTRACT_CODE_RE = re.compile(r"(?<![A-Za-z])([A-Za-z]{1,3})\d{3,4}(?!\d)")
_HEADING_NUMBER_RE = re.compile(
    r"^(?:第)?(?:[一二三四五六七八九十百]+|\d+)(?:章|节|条)?[、.．]\s*"
)

_MARGIN_LABELS = frozenset(
    {
        "最低交易保证金",
        "合约最低保证金",
        "保证金",
        "交易保证金",
        "交易保证金比例",
        "交易保证金标准",
        "一般持仓交易保证金比例",
        "一般持仓交易保证金标准",
        "投机交易保证金比例",
        "投机交易保证金标准",
    }
)
_PROSE_CLAUSE_SPLIT_RE = re.compile(r"[。；;\n]")
# ``玻璃期货2606合约`` / ``sc2609合约`` name one specific contract month; the
# resulting rate is a contract-specific temporary value, not the product-level
# ordinary rate or price-limit identity.
_CONTRACT_MONTH_RE = re.compile(r"(?:[A-Za-z]{1,3})?\d{3,4}\s*合约")
_TEMPLATE_SPAN_RE = re.compile(r"\{\{[^{}\n]{1,120}\}\}")
# Holiday/period notices adjust margins temporarily and end with an explicit
# reversion clause; their values never become the standing ordinary rate.
_REVERSION_PHRASE_RE = re.compile(r"恢复[至到]调整?前(?:水平|标准)")
_PRICE_LIMIT_LABELS = frozenset(
    {
        "涨跌停板",
        "涨跌停板幅度",
        "涨跌停幅度",
        "涨跌停",
        "每日价格波动限制",
        "每日价格最大波动限制",
    }
)
_OPEN_FEE_LABELS = frozenset(
    {
        "开仓手续费",
        "开仓手续费标准",
        "开仓交易手续费",
        "开仓交易手续费标准",
    }
)
_CLOSE_FEE_LABELS = frozenset(
    {
        "平仓手续费",
        "平仓手续费标准",
        "平仓交易手续费",
        "平仓交易手续费标准",
    }
)
_CLOSE_TODAY_FEE_LABELS = frozenset(
    {
        "平今手续费",
        "平今手续费标准",
        "平今仓手续费",
        "平今仓手续费标准",
        "平今仓交易手续费",
        "平今仓交易手续费标准",
        "日内平今仓交易手续费",
        "日内平今仓交易手续费标准",
    }
)
_OPEN_CLOSE_FEE_LABELS = frozenset(
    {
        "开平手续费",
        "开平手续费标准",
        "开平仓手续费",
        "开平仓手续费标准",
        "开平仓交易手续费",
        "开平仓交易手续费标准",
    }
)
_FEE_LABELS = (
    _OPEN_FEE_LABELS
    | _CLOSE_FEE_LABELS
    | _CLOSE_TODAY_FEE_LABELS
    | _OPEN_CLOSE_FEE_LABELS
)
_DELIVERY_LABELS = frozenset(
    {"最后交易日", "最后交割日", "交割日期", "交割方式"}
)
_OTHER_LABELS = frozenset(
    {
        "交易单位",
        "报价单位",
        "最小变动价位",
        "合约月份",
        "交易时间",
        "交易代码",
        "限仓",
        "交易限额",
    }
)
_ALL_LABELS = (
    _MARGIN_LABELS
    | _PRICE_LIMIT_LABELS
    | _FEE_LABELS
    | _DELIVERY_LABELS
    | _OTHER_LABELS
)
_PROSE_LABEL_PATTERN = "|".join(
    re.escape(label) for label in sorted(_ALL_LABELS, key=len, reverse=True)
)
_PROSE_LABEL_RE = re.compile(
    rf"(?P<label>{_PROSE_LABEL_PATTERN})\s*(?:调整)?\s*(?:为|是|[:：])\s*"
)

_RAW_RATE_UNIT = "ratio"
_CNY_PER_RAW_UNIT = {
    "手": FeeBasis.CNY_PER_LOT.value,
    "吨": "cny_per_ton",
    "克": "cny_per_gram",
    "千克": "cny_per_kilogram",
    "公斤": "cny_per_kilogram",
    "点": "cny_per_point",
}
_QUANTITY_PER_LOT_UNIT = {
    "吨": "ton_per_lot",
    "克": "gram_per_lot",
    "千克": "kilogram_per_lot",
    "公斤": "kilogram_per_lot",
}
_DELIVERY_METHODS = (
    "实物交割",
    "现金交割",
    "保税交割",
    "仓库交割",
    "厂库交割",
    "滚动交割",
    "集中交割",
)


@dataclass(frozen=True)
class LabeledSpecValue:
    """One exact labeled value parsed before rule-identity mapping."""

    label: str
    value_text: str
    value: Decimal | str
    unit: str | None
    excerpt: str


@dataclass(frozen=True)
class ExtractedRuleValue:
    """A consolidated candidate or conflict for one existing rule identity."""

    category: RuleCategory
    field: str
    status: RuleStatus
    value_kind: RuleValueKind
    value: Decimal | str | None
    unit: str | None
    excerpt: str
    source_document_id: str
    source_url: str
    note: str
    effective_date: date | None = None
    # "parameter_table" marks values from exchange settlement/parameter files
    # (daily CSV, matrix pages) as opposed to static contract text.
    source_class: str = "document"

    @property
    def identity(self) -> tuple[RuleCategory, str]:
        return self.category, self.field


def _decimal(number: str) -> Decimal | None:
    try:
        value = Decimal(number.replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite() or value < 0:
        return None
    return value


def _clean_cell(value: str) -> str:
    value = value.replace("\xa0", " ").strip()
    value = re.sub(r"^#{1,6}\s*", "", value)
    value = value.replace("**", "").replace("__", "")
    value = re.sub(r"\[([^\]]+)]\([^)]*\)", r"\1", value)
    return value.strip(" \t`*")


def _normalize_heading_label(value: str) -> str:
    value = _clean_cell(value)
    value = _HEADING_NUMBER_RE.sub("", value)
    return value.strip(" \t:：")


def _table_cells(line: str) -> list[str] | None:
    stripped = line.strip()
    if not stripped.startswith("|") or stripped.count("|") < 2:
        return None
    body = stripped[1:-1] if stripped.endswith("|") else stripped[1:]
    cells = body.split("|")
    if not cells or all(re.fullmatch(r"\s*:?-{3,}:?\s*", cell) for cell in cells):
        return None
    return cells


def _line_applies_to_product(line: str, product: str | None) -> bool:
    if not product:
        return True
    mentioned = {match.group(1).upper() for match in _CONTRACT_CODE_RE.finditer(line)}
    return not mentioned or product.upper() in mentioned


def _prose_label_is_qualified(line: str, start: int, label: str) -> bool:
    prefix = line[max(0, start - 10) : start]
    if label in _PRICE_LIMIT_LABELS and re.search(r"(?:首日|临时|特殊)\s*$", prefix):
        return False
    if label in {"保证金", "交易保证金", "交易保证金比例", "交易保证金标准"}:
        if re.search(r"(?:套保|套期保值|卖方会员)\S{0,4}$", prefix):
            return False
    if label == "交易时间" and re.search(r"(?:最后交易日|连续)\s*$", prefix):
        return False
    return True


def _mentioned_product_names(
    context: str,
    product_names: Sequence[str],
) -> set[str]:
    """Return catalog product names actually mentioned in a clause context.

    A shorter name occurring only inside a longer name (``铝`` inside
    ``氧化铝``) is not an independent mention, so longest names are matched
    first and their spans are excluded from shorter-name matches.
    """

    spans: list[tuple[int, int, str]] = []
    for name in sorted(set(product_names), key=len, reverse=True):
        if not name:
            continue
        cursor = context.find(name)
        while cursor != -1:
            end = cursor + len(name)
            overlapped = any(
                start < end and cursor < stop for start, stop, _ in spans
            )
            if not overlapped:
                spans.append((cursor, end, name))
            cursor = context.find(name, cursor + 1)
    return {name for _, _, name in spans}


def _prose_clause_applies_to_product(
    line: str,
    start: int,
    label: str,
    *,
    product_name: str | None,
    product_names: Sequence[str] | None,
) -> bool:
    """Qualify one prose-label match against its sentence context.

    Notice clauses usually name their product immediately before the label,
    for example ``玻璃期货合约的交易保证金标准调整为9%``.  When the sentence
    names other listed products only, the value belongs to those products.
    Clauses pinning a numeric contract month (``2606合约``) describe
    contract-specific temporary margins/limits rather than the product-level
    ordinary identities, so they are skipped for those labels.
    """

    clause_start = 0
    for match in _PROSE_CLAUSE_SPLIT_RE.finditer(line, 0, start):
        clause_start = match.end()
    context = line[clause_start:start]
    if label in (_MARGIN_LABELS | _PRICE_LIMIT_LABELS):
        if _CONTRACT_MONTH_RE.search(context):
            return False
    names = tuple(product_names or ())
    if not names:
        return True
    if product_name:
        # An own-name mention tied to the option product (``铜期权``) must
        # not make a futures clause look self-referential.
        masked = context.replace(f"{product_name}期权", "\x00")
    else:
        masked = context
    mentioned = _mentioned_product_names(masked, names)
    if not mentioned:
        return True
    return product_name in mentioned



def _numeric_clause(value: str) -> str:
    return re.split(r"[，,；;。]", value, maxsplit=1)[0].strip()


def _rate_value(value_text: str) -> tuple[Decimal, str] | None:
    matches = tuple(_PERCENT_RE.finditer(value_text))
    if len(matches) != 1:
        return None
    percent = _decimal(matches[0].group("number"))
    if percent is None:
        return None
    ratio = percent / Decimal("100")
    if ratio > 1:
        return None
    return ratio, _RAW_RATE_UNIT


def _fee_value(value_text: str) -> tuple[Decimal, str] | None:
    per_unit_matches = tuple(_CNY_PER_UNIT_RE.finditer(value_text))
    percent_matches = tuple(_PERCENT_RE.finditer(value_text))
    ten_thousand_matches = tuple(_TEN_THOUSAND_RE.finditer(value_text))
    match_count = len(per_unit_matches) + len(percent_matches) + len(ten_thousand_matches)
    if match_count != 1:
        return None
    if per_unit_matches:
        match = per_unit_matches[0]
        if match.group("unit") != "手":
            return None
        value = _decimal(match.group("number"))
        return None if value is None else (value, FeeBasis.CNY_PER_LOT.value)
    if percent_matches:
        percent = _decimal(percent_matches[0].group("number"))
        if percent is None or percent > 100:
            return None
        return percent / Decimal("100"), FeeBasis.RATE_PER_NOTIONAL.value
    fraction = _decimal(ten_thousand_matches[0].group("number"))
    if fraction is None:
        return None
    return fraction / Decimal("10000"), FeeBasis.RATE_PER_NOTIONAL.value


def _parse_labeled_value(
    label: str,
    value_text: str,
    excerpt: str,
) -> LabeledSpecValue | None:
    value_text = _clean_cell(value_text)
    if (
        not value_text
        or _TEMPLATE_RE.search(value_text)
        or _TEMPLATE_RE.search(excerpt)
        or _normalize_heading_label(value_text) in _ALL_LABELS
    ):
        return None

    if label in _MARGIN_LABELS or label in _PRICE_LIMIT_LABELS:
        parsed_rate = _rate_value(value_text)
        if parsed_rate is None:
            return None
        value, unit = parsed_rate
        return LabeledSpecValue(label, value_text, value, unit, excerpt.strip())

    if label in _FEE_LABELS:
        parsed_fee = _fee_value(value_text)
        if parsed_fee is None:
            return None
        value, unit = parsed_fee
        return LabeledSpecValue(label, value_text, value, unit, excerpt.strip())

    if label == "交易单位":
        matches = tuple(_PER_LOT_RE.finditer(value_text))
        if len(matches) != 1:
            return None
        value = _decimal(matches[0].group("number"))
        if value is None:
            return None
        unit = _QUANTITY_PER_LOT_UNIT[matches[0].group("unit")]
        return LabeledSpecValue(label, value_text, value, unit, excerpt.strip())

    if label == "最小变动价位":
        matches = tuple(_CNY_PER_UNIT_RE.finditer(value_text))
        if len(matches) != 1:
            return None
        value = _decimal(matches[0].group("number"))
        if value is None:
            return None
        unit = _CNY_PER_RAW_UNIT[matches[0].group("unit")]
        return LabeledSpecValue(label, value_text, value, unit, excerpt.strip())

    if label == "报价单位":
        if not re.search(r"元(?:\s*[（(]人民币[）)])?\s*(?:/|每)\s*(?:吨|手|克|千克|公斤|点)", value_text):
            return None
        return LabeledSpecValue(label, value_text, value_text, None, excerpt.strip())

    if label in {"限仓", "交易限额"}:
        matches = tuple(_LOT_RE.finditer(value_text))
        if len(matches) != 1:
            return None
        value = _decimal(matches[0].group("number"))
        if value is None:
            return None
        return LabeledSpecValue(label, value_text, value, "lot", excerpt.strip())

    if label == "交易时间":
        if not _TIME_RE.search(value_text):
            return None
        return LabeledSpecValue(label, value_text, value_text, None, excerpt.strip())

    if label in {"最后交易日", "最后交割日", "交割日期"}:
        if not (
            _DAY_RE.search(value_text)
            or _WEEKDAY_RE.search(value_text)
            or "同最后交易日" in value_text
        ):
            return None
        return LabeledSpecValue(label, value_text, value_text, None, excerpt.strip())

    if label == "交割方式":
        if not any(method in value_text for method in _DELIVERY_METHODS):
            return None
        return LabeledSpecValue(label, value_text, value_text, None, excerpt.strip())

    if label == "合约月份":
        if not re.search(r"\d+\s*月", value_text):
            return None
        return LabeledSpecValue(label, value_text, value_text, None, excerpt.strip())

    if label == "交易代码":
        if not re.fullmatch(r"[A-Za-z]{1,4}", value_text):
            return None
        return LabeledSpecValue(label, value_text, value_text.upper(), None, excerpt.strip())

    return None


def parse_labeled_spec_values(
    markdown: str,
    *,
    product: str | None = None,
    product_name: str | None = None,
    product_names: Sequence[str] | None = None,
) -> tuple[LabeledSpecValue, ...]:
    """Parse exact labeled values without mapping unrelated specs to rule fields.

    ``product_names`` carries every futures product name of the product's
    exchange so prose notice clauses naming other products are not attributed
    to this product (see :func:`_prose_clause_applies_to_product`).
    """

    if not isinstance(markdown, str) or not markdown.strip():
        return ()
    lines = markdown.splitlines()
    parsed: list[LabeledSpecValue] = []

    for index, raw_line in enumerate(lines):
        if not raw_line.strip() or not _line_applies_to_product(raw_line, product):
            continue
        cells = _table_cells(raw_line)
        if cells is not None:
            for cell_index in range(len(cells) - 1):
                label = _normalize_heading_label(cells[cell_index])
                if label not in _ALL_LABELS:
                    continue
                candidate = _parse_labeled_value(
                    label,
                    cells[cell_index + 1],
                    raw_line,
                )
                if candidate is not None:
                    parsed.append(candidate)
            continue

        normalized_line = _normalize_heading_label(raw_line)
        inline_match = re.fullmatch(
            rf"(?P<label>{_PROSE_LABEL_PATTERN})\s*[:：]\s*(?P<value>.+)",
            normalized_line,
        )
        if inline_match:
            candidate = _parse_labeled_value(
                inline_match.group("label"),
                inline_match.group("value"),
                raw_line,
            )
            if candidate is not None:
                parsed.append(candidate)

        # CZCE/DCE pages also publish compact prose such as
        # ``交易单位 10吨/手`` without a colon or the ``为`` copula.
        # Let the same exact-label validators decide whether the suffix is a
        # concrete value; navigation text therefore remains rejected.
        if inline_match is None:
            for label in sorted(_ALL_LABELS, key=len, reverse=True):
                if not normalized_line.startswith(label):
                    continue
                suffix = normalized_line[len(label) :]
                # Require an actual label/value separator.  Without this
                # guard a value such as ``最后交易日后第3个交易日`` would be
                # mistaken for a second compact ``最后交易日`` row.
                if not suffix or suffix[0] not in " \t:：":
                    continue
                remainder = suffix.strip(" \t:：")
                if not remainder:
                    continue
                candidate = _parse_labeled_value(label, remainder, raw_line)
                if candidate is not None:
                    parsed.append(candidate)
                break

        if normalized_line in _ALL_LABELS:
            # Rendered contract tables sometimes wrap one value across
            # several short lines (``合约价值的`` / ``5`` / ``％``), so the
            # following lines are joined until the label parses or a stop
            # condition (next label, long line, product mismatch) is hit.
            fragments: list[str] = []
            for next_index in range(index + 1, min(index + 5, len(lines))):
                next_line = lines[next_index]
                if not next_line.strip():
                    if fragments:
                        break
                    continue
                if _normalize_heading_label(next_line) in _ALL_LABELS:
                    break
                if len(next_line.strip()) > 30:
                    break
                if not _line_applies_to_product(next_line, product):
                    break
                fragments.append(next_line.strip())
                joined = " ".join(fragments)
                candidate = _parse_labeled_value(
                    normalized_line,
                    joined,
                    f"{raw_line.strip()}\n{' '.join(fragments)}",
                )
                if candidate is not None:
                    parsed.append(candidate)
                    break

        for match in _PROSE_LABEL_RE.finditer(raw_line):
            label = match.group("label")
            if not _prose_label_is_qualified(raw_line, match.start("label"), label):
                continue
            if not _prose_clause_applies_to_product(
                raw_line,
                match.start("label"),
                label,
                product_name=product_name,
                product_names=product_names,
            ):
                continue
            remainder = raw_line[match.end() :].strip()
            value_text = (
                _numeric_clause(remainder)
                if label in (_MARGIN_LABELS | _PRICE_LIMIT_LABELS | _FEE_LABELS)
                else re.split(r"[；;。]", remainder, maxsplit=1)[0].strip()
            )
            candidate = _parse_labeled_value(label, value_text, raw_line)
            if candidate is not None:
                parsed.append(candidate)

    unique: list[LabeledSpecValue] = []
    seen: set[tuple[object, ...]] = set()
    for item in parsed:
        key = (item.label, item.value, item.unit, item.excerpt)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return tuple(unique)


def _retained_markdown(document: SourceDocument) -> str | None:
    try:
        payload = json.loads(document.content)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    markdown = payload.get("markdown")
    return markdown if isinstance(markdown, str) and markdown.strip() else None


def _document_effective_date(document: SourceDocument) -> date | None:
    """Read one unambiguous explicit effective date from retained evidence.

    Publication dates and arbitrary historical dates are intentionally ignored;
    a date must be followed by an explicit effective-action cue (for example
    ``自2026年8月1日起``). Multiple differing cues make the document
    ambiguous and therefore ineligible for automatic conflict resolution.
    """

    if document.effective_date is not None:
        return document.effective_date
    markdown = _retained_markdown(document) or ""
    dates: set[date] = set()
    for match in _EFFECTIVE_DATE_RE.finditer(
        f"{document.title}\n{markdown}"
    ):
        try:
            dates.add(
                date(
                    int(match.group("year")),
                    int(match.group("month")),
                    int(match.group("day")),
                )
            )
        except ValueError:
            continue
    return next(iter(dates)) if len(dates) == 1 else None


def _value_kind(value: Decimal | str) -> RuleValueKind:
    if isinstance(value, str):
        return RuleValueKind.TEXT
    return RuleValueKind.EXPLICIT_ZERO if value == 0 else RuleValueKind.DECIMAL


def _candidate(
    document: SourceDocument,
    *,
    category: RuleCategory,
    field: str,
    value: Decimal | str,
    unit: str | None,
    excerpt: str,
    source_class: str = "document",
    effective_date: date | None = None,
) -> ExtractedRuleValue:
    return ExtractedRuleValue(
        category=category,
        field=field,
        status=RuleStatus.CANDIDATE,
        value_kind=_value_kind(value),
        value=value,
        unit=unit,
        excerpt=excerpt,
        source_document_id=document.document_id,
        source_url=document.url,
        note=AUTO_EXTRACTION_NOTE,
        effective_date=effective_date or _document_effective_date(document),
        source_class=source_class,
    )


_FEE_UNIT_VALUES = {basis.value for basis in FeeBasis}


def _parameter_document_date(markdown: str) -> date | None:
    """Read the parameter date stamped inside official parameter files."""

    for index, line in enumerate(markdown.splitlines()):
        if index > 40:
            break
        table_match = re.search(r"参数表[（(](\d{8})[)）]", line)
        if table_match:
            raw = table_match.group(1)
            try:
                return date(
                    int(raw[0:4]), int(raw[4:6]), int(raw[6:8])
                )
            except ValueError:
                return None
        updated_match = re.search(
            r"更新日期[：:]\s*(\d{4})年(\d{1,2})月(\d{1,2})", line
        )
        if updated_match:
            try:
                return date(
                    int(updated_match.group(1)),
                    int(updated_match.group(2)),
                    int(updated_match.group(3)),
                )
            except ValueError:
                return None
    return None


_INE_FEE_NAME_ALIASES = {
    "原油": "原油",
    "20号胶": "20号胶",
    "低硫燃料油": "低硫燃料油",
    "国际铜": "铜（BC）",
}


def _parse_ine_fee_sheet(
    markdown: str,
    *,
    product_name: str,
) -> list[tuple[str, Decimal, str, str]]:
    """Parse INE fee-schedule sheets flattened from the official XLSX.

    The 期货交易手续费 row lists one clause per product, for example
    ``原油20元/手；\\n20号胶0.02‰；`` where ``‰`` is per-mille of turnover.
    Flattened XLSX rows are tab-joined, so clauses carry leading cells; the
    trading-fee section spans from its label line to the next fee item
    (delivery/exercise fees) and is searched with an anchored regex.
    """

    clause_key = _INE_FEE_NAME_ALIASES.get(product_name, product_name)
    lines = markdown.splitlines()
    start = next(
        (index for index, line in enumerate(lines) if "期货交易手续费" in line),
        None,
    )
    if start is None:
        return []
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if "交割手续费" in lines[index] or "行权" in lines[index]
        ),
        len(lines),
    )
    section = "\n".join(lines[start:end])
    match = re.search(
        rf"{re.escape(clause_key)}\s*(\d+(?:\.\d+)?)\s*(元/手|‰)",
        section,
    )
    if not match:
        return []
    try:
        number = Decimal(match.group(1))
    except InvalidOperation:
        return []
    if match.group(2) == "‰":
        value, unit = number / Decimal("1000"), "rate_per_notional"
    else:
        value, unit = number, "cny_per_lot"
    excerpt = f"上海国际能源交易中心收费一览 · 期货交易手续费 | {match.group(0)}"
    return [
        ("fee_open", value, unit, excerpt),
        ("fee_close", value, unit, excerpt),
    ]


def _parse_settlement_parameter_csv(
    markdown: str,
    *,
    product: str,
) -> list[tuple[str, Decimal, str, str]]:
    """Parse CFFEX-style daily settlement parameter CSV rows for one product.

    Rows look like ``IC2608 ,12.00%,12.00%,万分之0.23,万分之0.5,1000%`` under
    the header ``期货合约,合约多头保证金标准,合约空头保证金标准,交易手续费标准,
    交割手续费标准,平今仓收取率``.
    """

    results: list[tuple[str, Decimal, str, str]] = []
    lines = [line for line in markdown.splitlines() if line.strip()]
    header_index = None
    for index, line in enumerate(lines):
        if (
            "合约多头保证金标准" in line
            and "合约空头保证金标准" in line
            and "交易手续费标准" in line
        ):
            header_index = index
            break
    if header_index is None:
        return results
    for line in lines[header_index + 1 :]:
        cells = [cell.strip() for cell in line.split(",")]
        if len(cells) < 6:
            continue
        code_match = re.match(r"[A-Za-z]+", cells[0])
        if not code_match or code_match.group(0).upper() != product.upper():
            continue
        long_margin = _percent_ratio(cells[1])
        short_margin = _percent_ratio(cells[2])
        excerpt = (
            f"{cells[0]} | 多头 {cells[1]} 空头 {cells[2]} "
            f"手续费 {cells[3]} 平今收取率 {cells[5]}"
        )
        if long_margin is None or short_margin is None:
            continue
        if long_margin != short_margin:
            # Unequal long/short margins cannot collapse into the single
            # ordinary-rate identity; leave the field to other evidence.
            continue
        results.append(("margin", long_margin, "rate_of_notional", excerpt))
        fee = _parameter_fee_value(cells[3])
        if fee is None:
            continue
        fee_value, fee_unit = fee
        results.append(("fee_open", fee_value, fee_unit, excerpt))
        results.append(("fee_close", fee_value, fee_unit, excerpt))
        close_rate = _percent_ratio(cells[5])
        if close_rate is not None:
            results.append(
                (
                    "fee_close_today",
                    fee_value * close_rate,
                    fee_unit,
                    f"{excerpt} 平今=手续费×收取率({cells[3]}×{cells[5]})",
                )
            )
    return results


_MATRIX_LABELS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("margin", ("交易保证金比例（投机）", "交易保证金比例(投机)")),
    ("limit", ("涨跌停板幅度",)),
    ("fee", ("交易手续费（元/手）", "交易手续费(元/手)")),
)

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _matrix_nameish(line: str) -> bool:
    # Product names may contain digits (黄大豆1号); pure-numeric value cells
    # carry no CJK and are excluded by the CJK requirement itself.
    return (
        bool(line)
        and len(line) <= 8
        and bool(_CJK_RE.search(line))
        and "/" not in line
        and "%" not in line
        and "万分之" not in line
        and not line.startswith("合约")
    )


def _matrix_valueish(line: str) -> bool:
    if not line or len(line) > 24:
        return False
    return bool(
        re.fullmatch(r"[±+-]?\d+(\.\d+)?%", line)
        or re.fullmatch(r"\d+(\.\d+)?", line)
        or "万分之" in line
        or line == "合约规定"
    )


def _parse_matrix_parameter_page(
    markdown: str,
    *,
    product_name: str,
) -> list[tuple[str, Decimal, str, str]]:
    """Parse exchange parameter matrix pages (one column per product).

    The retained text lists all product names on consecutive lines, then a
    labeled row (for example ``交易保证金比例（投机）``) followed by one value
    line per product in the same order.
    """

    results: list[tuple[str, Decimal, str, str]] = []
    lines = [line.strip() for line in markdown.splitlines()]
    label_positions: list[tuple[str, int]] = []
    for kind, labels in _MATRIX_LABELS:
        for index, line in enumerate(lines):
            if any(line.startswith(label) for label in labels):
                label_positions.append((kind, index))
                break
    if not label_positions:
        return results
    # The product-name run precedes the FIRST labeled row; later labels are
    # separated from it by the previous row's values, so back-scan only once.
    first_label = min(index for _, index in label_positions)
    run_start = first_label
    while run_start > 0 and _matrix_nameish(lines[run_start - 1]):
        run_start -= 1
    name_run = lines[run_start:first_label]
    if product_name not in name_run:
        return results
    column = name_run.index(product_name)
    width = len(name_run)
    for kind, label_index in label_positions:
        # Collect the contiguous value run and require it to match the
        # product-run width exactly: a missing or stray cell would shift
        # every following column onto the wrong product.
        values: list[str] = []
        cursor = label_index + 1
        while cursor < len(lines) and _matrix_valueish(lines[cursor]):
            values.append(lines[cursor])
            cursor += 1
        if len(values) != width or column >= width:
            continue
        raw = values[column]
        label = lines[label_index]
        excerpt = (
            f"{label} | {' / '.join(values)} | {product_name} 列{column + 1}: {raw}"
        )
        if kind == "margin":
            ratio = _percent_ratio(raw)
            if ratio is not None:
                results.append(
                    ("margin", ratio, "rate_of_notional", f"{excerpt}（投机档）")
                )
        elif kind == "limit":
            ratio = _percent_ratio(raw.lstrip("±"))
            if ratio is not None:
                results.append(
                    ("limit", ratio, "rate_of_previous_settlement", excerpt)
                )
        else:
            fee = _parameter_fee_value(raw)
            if fee is not None:
                fee_value, fee_unit = fee
                results.append(("fee_open", fee_value, fee_unit, excerpt))
                results.append(("fee_close", fee_value, fee_unit, excerpt))
    return results


def _percent_ratio(cell: str) -> Decimal | None:
    match = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)\s*%", cell.strip())
    if not match:
        return None
    try:
        value = Decimal(match.group(1))
    except InvalidOperation:
        return None
    if not value.is_finite() or value < 0:
        return None
    return value / Decimal("100")


def _decimal_or_none(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    if not number.is_finite() or number < 0:
        return None
    return number


def _consolidated_instrument_value(
    values: Sequence[Decimal],
) -> Decimal | None:
    """Collapse per-contract settlement values into one product-level value.

    Near-delivery contracts carry escalated temporary ratios, so the ordinary
    product-level value is the most common one across the product's listed
    contracts; a tie leaves the field to other evidence instead of guessing.
    """

    if not values:
        return None
    counts: dict[Decimal, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    best = max(counts.values())
    leaders = [value for value, count in counts.items() if count == best]
    return leaders[0] if len(leaders) == 1 else None


def _parse_shfe_daily_parameter_json(
    markdown: str,
    *,
    product: str,
) -> list[tuple[str, Decimal, str, str]]:
    """Parse SHFE/INE ``js{YYYYMMDD}.dat`` settlement-parameter JSON rows.

    Rows look like ``{"INSTRUMENTID":"cu2609","SPECLONGMARGINRATIO":0.15,
    "SPECSHORTMARGINRATIO":0.15,"TRADEFEERATIO":0.05,"TRADEFEEUNIT":0,
    "TTRADEFEEUNIT":0,...,"PRODUCTID":"cu_f","PRODUCTNAME":"铜"}``.
    Margin ratios are plain decimal rates; fee ``*UNIT`` fields are yuan per
    lot.  Ratio-based fee fields are intentionally not converted because the
    exchange publishes them without an explicit basis, and a wrong basis
    would fabricate a fee value.
    """

    try:
        payload = json.loads(markdown)
    except (json.JSONDecodeError, ValueError):
        return []
    rows = payload.get("o_cursor") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list):
        return []
    prefix = f"{product.lower()}_"
    margins: list[Decimal] = []
    fee_units: list[Decimal] = []
    close_today_units: list[Decimal] = []
    instruments: list[str] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        product_id = row.get("PRODUCTID")
        if not isinstance(product_id, str) or not product_id.startswith(prefix):
            continue
        long_margin = _decimal_or_none(row.get("SPECLONGMARGINRATIO"))
        short_margin = _decimal_or_none(row.get("SPECSHORTMARGINRATIO"))
        if long_margin is None or short_margin is None:
            continue
        if long_margin > 1 or short_margin > 1:
            continue
        if long_margin == short_margin:
            margins.append(long_margin)
            instruments.append(str(row.get("INSTRUMENTID") or product_id))
        fee_unit = _decimal_or_none(row.get("TRADEFEEUNIT"))
        if fee_unit:
            fee_units.append(fee_unit)
        close_today_unit = _decimal_or_none(row.get("TTRADEFEEUNIT"))
        if close_today_unit:
            close_today_units.append(close_today_unit)
    results: list[tuple[str, Decimal, str, str]] = []
    margin = _consolidated_instrument_value(margins)
    if margin is not None:
        excerpt = (
            f"{product.upper()} daily settlement parameters | instruments="
            f"{','.join(instruments[:6])}"
            f"{'…' if len(instruments) > 6 else ''} | "
            f"SPECLONGMARGINRATIO=SPECSHORTMARGINRATIO={margin}"
        )
        results.append(("margin", margin, "rate_of_notional", excerpt))
        fee = _consolidated_instrument_value(fee_units)
        if fee is not None:
            fee_excerpt = f"{excerpt} | TRADEFEEUNIT={fee} 元/手"
            results.append(("fee_open", fee, "cny_per_lot", fee_excerpt))
            results.append(("fee_close", fee, "cny_per_lot", fee_excerpt))
            close_today = _consolidated_instrument_value(close_today_units)
            if close_today is not None:
                results.append(
                    (
                        "fee_close_today",
                        close_today,
                        "cny_per_lot",
                        f"{fee_excerpt} | TTRADEFEEUNIT={close_today} 元/手",
                    )
                )
    return results


def _parameter_fee_value(cell: str) -> tuple[Decimal, str] | None:
    match = re.search(r"万分之\s*([\d.]+)", cell)
    if match:
        try:
            value = Decimal(match.group(1)) / Decimal("10000")
        except InvalidOperation:
            return None
        if value.is_finite() and value >= 0:
            return value, "rate_per_notional"
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", cell.strip()):
        try:
            value = Decimal(cell.strip())
        except InvalidOperation:
            return None
        if value.is_finite() and value >= 0:
            return value, "cny_per_lot"
    return None


_PARAMETER_KINDS = {
    "margin": (RuleCategory.MARGIN, "ordinary_rate"),
    "limit": (RuleCategory.LIMIT, "price_and_position_limits"),
    "fee_open": (RuleCategory.FEE, "open"),
    "fee_close": (RuleCategory.FEE, "close"),
    "fee_close_today": (RuleCategory.FEE, "close_today"),
}


def _map_document_values(
    document: SourceDocument,
    *,
    product: str,
    product_name: str | None = None,
    product_names: Sequence[str] | None = None,
) -> tuple[ExtractedRuleValue, ...]:
    markdown = _retained_markdown(document)
    if markdown is None:
        return ()
    parameter_rows = _parse_settlement_parameter_csv(markdown, product=product)
    parameter_rows = parameter_rows + _parse_shfe_daily_parameter_json(
        markdown, product=product
    )
    if product_name:
        parameter_rows = parameter_rows + _parse_matrix_parameter_page(
            markdown, product_name=product_name
        ) + _parse_ine_fee_sheet(markdown, product_name=product_name)
    parameter_results: list[ExtractedRuleValue] = []
    for kind, value, unit, excerpt in parameter_rows:
        category, field = _PARAMETER_KINDS[kind]
        if (
            category is RuleCategory.FEE
            and isinstance(value, Decimal)
            and unit not in _FEE_UNIT_VALUES
        ):
            # Extraction-path fee units must stay inside the FeeBasis
            # vocabulary so structured-rule and extraction agree.
            continue
        parameter_results.append(
            _candidate(
                document,
                category=category,
                field=field,
                value=value,
                unit=unit,
                excerpt=excerpt,
                source_class="parameter_table",
                effective_date=_parameter_document_date(markdown),
            )
        )
    labeled = parse_labeled_spec_values(
        markdown,
        product=product,
        product_name=product_name,
        product_names=product_names,
    )
    # Aggregate overview pages reproduce the parameter tables of many
    # products; when one document lists several differing labeled values for
    # the same margin/limit label, the rows cannot be attributed to this
    # product and the document's labeled candidates for that label are
    # dropped instead of fabricating a conflict winner.
    ambiguous_labels: set[str] = set()
    by_label_values: dict[str, set[Decimal]] = {}
    for item in labeled:
        if item.label in _MARGIN_LABELS or item.label in _PRICE_LIMIT_LABELS:
            if isinstance(item.value, Decimal):
                by_label_values.setdefault(item.label, set()).add(item.value)
    for label, values in by_label_values.items():
        if len(values) > 1:
            ambiguous_labels.add(label)
    reversion_declared = bool(_REVERSION_PHRASE_RE.search(markdown))
    labeled = tuple(
        item
        for item in labeled
        if item.label not in ambiguous_labels
        and not (
            reversion_declared
            and (
                item.label in _MARGIN_LABELS or item.label in _PRICE_LIMIT_LABELS
            )
        )
    )
    result: list[ExtractedRuleValue] = parameter_results
    delivery: list[LabeledSpecValue] = []
    for item in labeled:
        if item.label in _MARGIN_LABELS and isinstance(item.value, Decimal):
            result.append(
                _candidate(
                    document,
                    category=RuleCategory.MARGIN,
                    field="ordinary_rate",
                    value=item.value,
                    unit="rate_of_notional",
                    excerpt=item.excerpt,
                )
            )
        elif item.label in _PRICE_LIMIT_LABELS and isinstance(item.value, Decimal):
            result.append(
                _candidate(
                    document,
                    category=RuleCategory.LIMIT,
                    field="price_and_position_limits",
                    value=item.value,
                    unit="rate_of_previous_settlement",
                    excerpt=item.excerpt,
                )
            )
        elif item.label in _FEE_LABELS and isinstance(item.value, Decimal):
            fields: tuple[str, ...]
            if item.label in _OPEN_FEE_LABELS:
                fields = ("open",)
            elif item.label in _CLOSE_FEE_LABELS:
                fields = ("close",)
            elif item.label in _CLOSE_TODAY_FEE_LABELS:
                fields = ("close_today",)
            else:
                fields = ("open", "close")
            for field in fields:
                result.append(
                    _candidate(
                        document,
                        category=RuleCategory.FEE,
                        field=field,
                        value=item.value,
                        unit=item.unit,
                        excerpt=item.excerpt,
                    )
                )
        elif item.label == "交易时间" and isinstance(item.value, str):
            result.append(
                _candidate(
                    document,
                    category=RuleCategory.SESSION,
                    field="trading_sessions",
                    value=item.value,
                    unit=None,
                    excerpt=item.excerpt,
                )
            )
        elif item.label in _DELIVERY_LABELS and isinstance(item.value, str):
            delivery.append(item)

    if delivery:
        excerpts = tuple(dict.fromkeys(item.excerpt for item in delivery))
        joined = "\n".join(excerpts)
        result.append(
            _candidate(
                document,
                category=RuleCategory.DELIVERY,
                field="delivery_and_closeout",
                value=joined,
                unit=None,
                excerpt=joined,
            )
        )
    return tuple(result)


def _signature(item: ExtractedRuleValue) -> tuple[object, ...]:
    return item.value_kind, item.value, item.unit


def _has_newer_effective_date(
    candidates: Sequence[ExtractedRuleValue],
    reference: ExtractedRuleValue,
) -> bool:
    """True when a candidate carries a strictly newer explicit effective date."""

    if reference.effective_date is None:
        return any(candidate.effective_date is not None for candidate in candidates)
    return any(
        candidate.effective_date is not None
        and candidate.effective_date > reference.effective_date
        for candidate in candidates
    )


def _conflict(candidates: Sequence[ExtractedRuleValue]) -> ExtractedRuleValue:
    ordered = sorted(
        candidates,
        key=lambda item: (item.source_document_id, item.excerpt),
    )
    excerpts = tuple(
        dict.fromkeys(
            f"[{item.source_document_id}; effective_on="
            f"{item.effective_date.isoformat() if item.effective_date else 'unknown'}] "
            f"{item.excerpt}"
            for item in ordered
        )
    )
    note = (
        "conflict: deterministic extraction found differing retained values:\n"
        + "\n".join(excerpts)
    )
    first = ordered[0]
    return ExtractedRuleValue(
        category=first.category,
        field=first.field,
        status=RuleStatus.REVIEW_REQUIRED,
        value_kind=RuleValueKind.UNKNOWN,
        value=None,
        unit=None,
        excerpt=note,
        source_document_id=first.source_document_id,
        source_url=first.source_url,
        note=note,
    )


def extract_rule_values(
    documents: Iterable[SourceDocument],
    *,
    product: str,
    target_day: date | None = None,
    product_name: str | None = None,
    product_names: Sequence[str] | None = None,
    preferred_document_urls: frozenset[str] | None = None,
) -> tuple[ExtractedRuleValue, ...]:
    """Extract and consolidate safe rule identities across retained documents.

    ``preferred_document_urls`` carries the product's catalog-maintained seed
    URLs; when undated contract-text candidates disagree, the catalog page
    (the exchange's current published contract page) outranks stray pages
    that web discovery attached to the product.
    """

    grouped: dict[tuple[RuleCategory, str], list[ExtractedRuleValue]] = {}
    for document in sorted(documents, key=lambda item: item.document_id):
        for candidate in _map_document_values(
            document,
            product=product,
            product_name=product_name,
            product_names=product_names,
        ):
            grouped.setdefault(candidate.identity, []).append(candidate)

    decisions: list[ExtractedRuleValue] = []
    for identity in sorted(grouped, key=lambda item: (item[0].value, item[1])):
        candidates = grouped[identity]
        signatures = {_signature(item) for item in candidates}
        if len(signatures) == 1:
            decisions.append(
                sorted(
                    candidates,
                    key=lambda item: (item.source_document_id, item.excerpt),
                )[0]
            )
        else:
            parameter_candidates = [
                candidate
                for candidate in candidates
                if candidate.source_class == "parameter_table"
            ]
            document_candidates = [
                candidate
                for candidate in candidates
                if candidate.source_class != "parameter_table"
            ]
            chosen = (
                sorted(
                    parameter_candidates,
                    key=lambda item: (item.source_document_id, item.excerpt),
                )[0]
                if parameter_candidates
                and len(
                    {_signature(item) for item in parameter_candidates}
                )
                == 1
                else None
            )
            if (
                chosen is not None
                and document_candidates
                and not _has_newer_effective_date(document_candidates, chosen)
            ):
                # Exchange parameter tables carry the currently effective
                # values; static contract text only states contract floors.
                # A strictly newer dated notice still wins, and every
                # superseded excerpt is preserved for review.
                superseded = "; ".join(
                    dict.fromkeys(
                        f"[{item.source_document_id}; effective_on="
                        f"{item.effective_date.isoformat() if item.effective_date else 'unknown'}] "
                        f"{item.excerpt}"
                        for item in document_candidates
                    )
                )
                decisions.append(
                    replace(
                        chosen,
                        note=chosen.note
                        + "; parameter-table value supersedes contract-text "
                        f"candidates: {superseded}",
                    )
                )
                continue
            dated_candidates = [
                candidate
                for candidate in candidates
                if candidate.effective_date is not None
                and (
                    target_day is None or candidate.effective_date <= target_day
                )
            ]
            undated_candidates = [
                candidate
                for candidate in candidates
                if candidate.effective_date is None
            ]
            if dated_candidates and undated_candidates:
                latest_date = max(
                    candidate.effective_date for candidate in dated_candidates
                )
                latest = [
                    candidate
                    for candidate in dated_candidates
                    if candidate.effective_date == latest_date
                ]
                if len({_signature(candidate) for candidate in latest}) == 1:
                    # Adjustment notices carry an explicit effective date and
                    # outrank undated static contract text, which only states
                    # the contract floor; superseded excerpts stay visible.
                    chosen = sorted(
                        latest,
                        key=lambda item: (item.source_document_id, item.excerpt),
                    )[0]
                    evidence = "\n".join(
                        f"[{candidate.source_document_id}; effective_on="
                        f"{candidate.effective_date.isoformat() if candidate.effective_date else 'unknown'}] "
                        f"{candidate.excerpt}"
                        for candidate in sorted(
                            candidates,
                            key=lambda item: (item.source_document_id, item.excerpt),
                        )
                    )
                    decisions.append(
                        replace(
                            chosen,
                            note=(
                                f"{AUTO_EXTRACTION_NOTE}\n"
                                "dated-notice value supersedes undated "
                                f"contract-text candidates; selected "
                                f"effective_on={latest_date.isoformat()}\n"
                                f"{evidence}"
                            ),
                        )
                    )
                    continue
            if (
                preferred_document_urls
                and undated_candidates
                and not dated_candidates
            ):
                preferred = [
                    candidate
                    for candidate in undated_candidates
                    if candidate.source_url in preferred_document_urls
                ]
                if preferred and len({_signature(c) for c in preferred}) == 1:
                    chosen = sorted(
                        preferred,
                        key=lambda item: (item.source_document_id, item.excerpt),
                    )[0]
                    evidence = "\n".join(
                        f"[{candidate.source_document_id}; effective_on="
                        f"{candidate.effective_date.isoformat() if candidate.effective_date else 'unknown'}] "
                        f"{candidate.excerpt}"
                        for candidate in sorted(
                            candidates,
                            key=lambda item: (item.source_document_id, item.excerpt),
                        )
                    )
                    decisions.append(
                        replace(
                            chosen,
                            note=(
                                f"{AUTO_EXTRACTION_NOTE}\n"
                                "catalog-seed page value supersedes stray "
                                "discovered-page candidates\n"
                                f"{evidence}"
                            ),
                        )
                    )
                    continue
            dated = target_day is not None and all(
                candidate.effective_date is not None for candidate in candidates
            )
            if dated:
                eligible = [
                    candidate
                    for candidate in candidates
                    if candidate.effective_date is not None
                    and candidate.effective_date <= target_day
                ]
                if eligible:
                    latest_date = max(
                        candidate.effective_date for candidate in eligible
                    )
                    latest = [
                        candidate
                        for candidate in eligible
                        if candidate.effective_date == latest_date
                    ]
                    latest_signatures = {_signature(candidate) for candidate in latest}
                    if len(latest_signatures) == 1:
                        chosen = sorted(
                            latest,
                            key=lambda item: (item.source_document_id, item.excerpt),
                        )[0]
                        evidence = "\n".join(
                            f"[{candidate.source_document_id}; effective_on="
                            f"{candidate.effective_date.isoformat() if candidate.effective_date else 'unknown'}] "
                            f"{candidate.excerpt}"
                            for candidate in sorted(
                                candidates,
                                key=lambda item: (item.source_document_id, item.excerpt),
                            )
                        )
                        decisions.append(
                            replace(
                                chosen,
                                note=(
                                    f"{AUTO_EXTRACTION_NOTE}\n"
                                    "date-aware conflict resolved; selected "
                                    f"effective_on={latest_date.isoformat()}\n"
                                    f"{evidence}"
                                ),
                            )
                        )
                        continue
            decisions.append(_conflict(candidates))
    return tuple(decisions)
