"""Async econ transport protocol, immutable macro cache and derived tags.

The cache is append-only: identical (item_id, revision, content) appends
are idempotent, conflicting content is rejected and new revisions extend
the log. ``snapshot`` returns a frozen ``MacroContext`` that later appends
never mutate. The default refresh calls only the four actually-known econ
tools through the frozen response adapter ``macro-adapter-v2.1.0``; unknown
response shapes degrade with the raw hash preserved and no guessed fields.
All verification-completion times come from the injected ``clock_nano``
callable; the request-start bound is never treated as a response-known
time, and a clock that runs backwards degrades the affected response
(devplan-v2.1.0 M1 §4.2, §6, §6.1).
"""
from __future__ import annotations

import unicodedata
from dataclasses import replace as _replace
from datetime import datetime, timezone
from typing import Callable, Mapping, Protocol

from ak_trading.v2.artifacts import sha256_hex, stable_id
from ak_trading.v2.contracts import (
    DataQuality,
    JsonValue,
    MacroContext,
    MacroItem,
)
from ak_trading.v2.schemas import SchemaError, canonical_json_bytes, parse_json_bytes

MACRO_POLICY_VERSION = "macro-risk-v2.1.0"
ADAPTER_VERSION = "macro-adapter-v2.1.1"
FACT_TTL_NANO = 4 * 3600 * 1_000_000_000
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
KNOWN_PRODUCTS = frozenset({
    "SHFE.RB", "SHFE.CU", "SHFE.AU", "DCE.M", "DCE.I", "INE.SC",
})
TAG_WORDS: Mapping[str, tuple[str, ...]] = {
    "GLOBAL_EVENT": ("加息", "降息", "美联储", "federal reserve", "利率决议", "战争", "war", "制裁", "sanction"),
    "ENERGY_EVENT": ("原油", "石油", "crude", "oil", "opec", "能源供应", "海峡"),
    "METAL_EVENT": ("铜矿", "金矿", "矿山停产", "铜冶炼", "金属关税"),
    "AGRI_EVENT": ("大豆", "豆粕", "soybean", "soymeal", "粮食禁运", "干旱"),
}
TAG_PRODUCTS: Mapping[str, tuple[str, ...]] = {
    "GLOBAL_EVENT": ("SHFE.RB", "SHFE.CU", "SHFE.AU", "DCE.M", "DCE.I", "INE.SC"),
    "ENERGY_EVENT": ("INE.SC", "SHFE.RB", "DCE.I"),
    "METAL_EVENT": ("SHFE.CU", "SHFE.AU", "SHFE.RB", "DCE.I"),
    "AGRI_EVENT": ("DCE.M",),
}
_REFRESH_TOOLS = (
    "get_pipeline_status", "get_recent_meetings", "get_frontpage_news", "get_investments",
)


class EconTransport(Protocol):
    async def call_tool(self, name: str, arguments: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]: ...


class MacroCache:
    """Append-only immutable macro item store."""

    def __init__(self, items: tuple[MacroItem, ...]) -> None:
        if not isinstance(items, tuple) or any(not isinstance(item, MacroItem) for item in items):
            raise TypeError("items_must_be_tuple_of_MacroItem")
        self._items: list[MacroItem] = list(items)

    def append(self, items: tuple[MacroItem, ...]) -> None:
        if not isinstance(items, tuple) or any(not isinstance(item, MacroItem) for item in items):
            raise TypeError("items_must_be_tuple_of_MacroItem")
        for item in items:
            for existing in self._items:
                if existing.item_id != item.item_id:
                    continue
                if existing.revision == item.revision:
                    if existing != item:
                        raise ValueError("conflicting_macro_item_revision")
                    break
            else:
                self._items.append(item)

    @property
    def items(self) -> tuple[MacroItem, ...]:
        return tuple(self._items)

    def snapshot(self, decision_nano: int, product: str,
                 required: bool, price_only_validated: bool) -> MacroContext:
        """Frozen point-in-time macro context for one product decision.

        F15(r3) semantics:
        1. Select the PIT-latest VISIBLE revision per entity (regardless
           of validity) — a degraded r2 BLOCKS the entity, no fallback.
        2. Then gate the selected revision for usability (expired, not
           published, degraded, missing unit).
        3. Context identity and input_hash bind to the FULL fingerprint
           (item:revision:raw_hash) of ALL selected entities, including
           rejected ones — different rejected revisions produce different
           identities.
        4. Entity selection and context bytes are deterministic: sorted
           by item_id, independent of cache insertion order.
        """
        if isinstance(decision_nano, bool) or not isinstance(decision_nano, int):
            raise TypeError("decision_nano_must_be_int")
        if not isinstance(product, str):
            raise TypeError("product_must_be_str")
        if not isinstance(required, bool) or not isinstance(price_only_validated, bool):
            raise TypeError("required_and_price_only_validated_must_be_bool")

        # Step 1: PIT-latest visible revision per entity.
        # "Visible" = available_at <= decision (we can see it exists).
        # Sort candidates deterministically by (available, first_seen, revision)
        # so equal timestamps resolve by revision string, not input order.
        visible: list[MacroItem] = []
        for item in self._items:
            if item.available_at_nano > decision_nano:
                continue
            visible.append(item)
        latest_per_entity: dict[str, MacroItem] = {}
        for item in sorted(visible, key=lambda i: (i.item_id, i.available_at_nano,
                                                    i.first_seen_nano, i.revision)):
            latest_per_entity[item.item_id] = item

        # Step 2: gate selected revisions for usability.
        reasons: list[str] = []
        usable: list[MacroItem] = []
        rejected: list[MacroItem] = []
        for entity_id in sorted(latest_per_entity):
            item = latest_per_entity[entity_id]
            rejected_this = None
            if item.valid_from_nano > decision_nano:
                reasons.append("macro_item_not_yet_valid")
                rejected_this = "macro_item_not_yet_valid"
            elif item.valid_until_nano <= decision_nano:
                reasons.append("macro_item_expired")
                rejected_this = "macro_item_expired"
            elif item.publication_state != "published" or item.published_at_nano is None:
                reasons.append("macro_item_not_published")
                rejected_this = "macro_item_not_published"
            elif item.degradation_state != "normal":
                reasons.append("macro_item_degraded")
                rejected_this = "macro_item_degraded"
            elif item.kind == "ECON_OPINION" and item.unit is None:
                reasons.append("macro_item_unit_missing")
                rejected_this = "macro_item_unit_missing"
            if rejected_this:
                rejected.append(item)
            else:
                usable.append(item)

        # Fingerprint ALL selected entities (usable AND rejected) so that
        # switching between revisions changes context identity even in
        # BLOCKED mode.
        def _revision_fingerprint(items: list[MacroItem]) -> str:
            return "|".join(f"{i.item_id}:{i.revision}:{i.raw_sha256}"
                            for i in sorted(items, key=lambda i: i.item_id))

        normalized = product.upper()
        relevant = [
            item for item in usable
            if not item.products or normalized in {entry.upper() for entry in item.products}
        ]
        relevant_rejected = [
            item for item in rejected
            if not item.products or normalized in {entry.upper() for entry in item.products}
        ]
        # For coverage and tag purposes, all product-relevant entities count.
        all_relevant = sorted((*relevant, *relevant_rejected), key=lambda i: i.item_id)
        covered = bool(relevant)  # only usable items provide coverage
        risk_tags = _risk_tags_for_product(relevant, normalized)
        if normalized not in KNOWN_PRODUCTS:
            risk_tags = (*risk_tags, "MACRO_COVERAGE_UNKNOWN")

        full_fingerprint = _revision_fingerprint(all_relevant)
        if covered:
            mode = "WITH_MACRO"
            quality = DataQuality(
                status="READY", reasons=(), temporal_basis="observed",
                input_hash=sha256_hex(full_fingerprint.encode("utf-8")),
            )
        elif required:
            mode = "BLOCKED"
            reasons.append("macro_required_missing")
            quality = DataQuality(
                status="MISSING", reasons=tuple(dict.fromkeys(reasons)),
                temporal_basis="unknown",
                input_hash=sha256_hex(f"{normalized}|{full_fingerprint}".encode("utf-8")),
            )
        elif price_only_validated:
            mode = "PRICE_ONLY"
            reasons.append("price_only_fallback_validated")
            quality = DataQuality(
                status="DEGRADED", reasons=tuple(dict.fromkeys(reasons)),
                temporal_basis="observed",
                input_hash=sha256_hex(f"{normalized}|price_only|{full_fingerprint}".encode("utf-8")),
            )
        else:
            mode = "BLOCKED"
            reasons.append("price_only_not_validated")
            quality = DataQuality(
                status="MISSING", reasons=tuple(dict.fromkeys(reasons)),
                temporal_basis="unknown",
                input_hash=sha256_hex(f"{normalized}|blocked|{full_fingerprint}".encode("utf-8")),
            )
        context_id = stable_id(
            "macro-context",
            f"{decision_nano}|{normalized}|{full_fingerprint}",
        )
        return MacroContext(
            context_id=context_id,
            decision_nano=decision_nano,
            items=tuple(relevant),
            policy_version=MACRO_POLICY_VERSION,
            risk_tags=tuple(risk_tags),
            mode=mode,
            quality=quality,
        )


def _risk_tags_for_product(items: list[MacroItem], normalized_product: str) -> tuple[str, ...]:
    tags: set[str] = set()
    for item in items:
        if item.kind != "DERIVED_TAG" or not item.derived_tag:
            continue
        products = TAG_PRODUCTS.get(item.derived_tag, ())
        if normalized_product in {entry.upper() for entry in products}:
            tags.add(item.derived_tag)
    return tuple(sorted(tags))


def _degraded_marker(tool: str, payload: bytes, first_seen_nano: int, reason: str) -> MacroItem:
    digest = sha256_hex(payload)
    return MacroItem(
        item_id=stable_id("macro-degraded", f"{tool}|{digest}"),
        meeting_id=None,
        revision="initial",
        kind="FACT",
        derived_tag=None,
        derivation_version=None,
        published_at_nano=None,
        first_seen_nano=first_seen_nano,
        retrieved_at_nano=first_seen_nano,
        available_at_nano=first_seen_nano,
        valid_from_nano=first_seen_nano,
        valid_until_nano=first_seen_nano,
        publication_state="unknown",
        degradation_state="degraded",
        assets=(),
        products=(),
        unit=None,
        source_urls=(),
        text="",
        parent_ids=(),
        raw_sha256=digest,
        quality=DataQuality(
            status="DEGRADED", reasons=(reason,), temporal_basis="observed",
            input_hash=sha256_hex(f"{tool}|{digest}".encode("utf-8")),
        ),
    )


def _record_int(record: Mapping[str, object], key: str) -> int | None:
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _record_str(record: Mapping[str, object], *keys: str) -> str | None:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str):
            return value
    return None


def _record_urls(record: Mapping[str, object]) -> tuple[str, ...]:
    urls: list[str] = []
    for key in ("url", "source_url"):
        value = record.get(key)
        if isinstance(value, str):
            urls.append(value)
    for key in ("urls", "source_urls"):
        value = record.get(key)
        if isinstance(value, list):
            urls.extend(entry for entry in value if isinstance(entry, str))
    return tuple(dict.fromkeys(urls))


def _string_tuple(record: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = record.get(key)
    if isinstance(value, list):
        return tuple(entry for entry in value if isinstance(entry, str))
    return ()


def _build_item(tool: str, record: Mapping[str, object], kind: str,
                received_nano: int, validated_nano: int) -> MacroItem:
    """Generic record builder for shapes carrying explicit nano timestamps."""
    record_bytes = canonical_json_bytes(record)
    record_id = _record_str(record, "item_id", "id")
    text = _record_str(record, "text", "title", "summary") or ""
    published_at = _record_int(record, "published_at_nano")
    source_valid_until = _record_int(record, "valid_until_nano")
    truncated = record.get("truncated") is True
    return _assemble_item(
        item_id=stable_id("macro", f"{tool}|{record_id}|{sha256_hex(record_bytes)}"),
        kind=kind, text=text,
        url_tuple=_record_urls(record), published_nano=published_at,
        received_nano=received_nano, validated_nano=validated_nano,
        meeting_id=_record_str(record, "meeting_id"),
        assets=_string_tuple(record, "assets"), products=_string_tuple(record, "products"),
        unit=_record_str(record, "unit"),
        degraded=truncated, reasons=("text_truncated",) if truncated else (),
        source_valid_until=source_valid_until,
    )


def _iso_to_nano(value: object) -> int | None:
    """Parse an ISO-8601 timestamp (real econ responses carry ISO strings)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    delta = moment.astimezone(timezone.utc) - _EPOCH
    return (delta.days * 86400 + delta.seconds) * 10**9 + delta.microseconds * 1000


def _assemble_item(*, item_id: str, kind: str, text: str, url_tuple: tuple[str, ...],
                   published_nano: int | None, received_nano: int, validated_nano: int,
                   meeting_id: str | None, assets: tuple[str, ...],
                   products: tuple[str, ...], unit: str | None, degraded: bool,
                   reasons: tuple[str, ...], source_valid_until: int | None = None,
                   record_bytes: bytes | None = None) -> MacroItem:
    if published_nano is not None:
        valid_from = published_nano
        valid_until = published_nano + FACT_TTL_NANO
        if source_valid_until is not None:
            valid_until = min(valid_until, source_valid_until)
        publication_state = "published"
    else:
        # No explicit publication evidence: recorded, never treated as
        # published (a state header like state="published" is not a time).
        valid_from = received_nano
        valid_until = received_nano
        publication_state = "unpublished"
    return MacroItem(
        item_id=item_id,
        meeting_id=meeting_id,
        revision="initial",
        kind=kind,
        derived_tag=None,
        derivation_version=None,
        published_at_nano=published_nano,
        first_seen_nano=received_nano,
        retrieved_at_nano=received_nano,
        available_at_nano=max(validated_nano, received_nano),
        valid_from_nano=valid_from,
        valid_until_nano=valid_until,
        publication_state=publication_state,
        degradation_state="degraded" if degraded else "normal",
        assets=assets,
        products=products,
        unit=unit,
        source_urls=url_tuple,
        text=text,
        parent_ids=(),
        raw_sha256=(sha256_hex(record_bytes) if record_bytes is not None
                    else sha256_hex(canonical_json_bytes(
                        {"item_id": item_id, "text": text, "urls": list(url_tuple),
                         "published_nano": published_nano, "degraded": degraded,
                         "meeting_id": meeting_id, "assets": list(assets),
                         "unit": unit}))),
        quality=DataQuality(
            status="DEGRADED" if degraded else "READY",
            reasons=reasons,
            temporal_basis="observed",
            # F15(r3): quality input_hash binds the full record content so
            # different revisions of the same entity always differ.
            input_hash=(sha256_hex(record_bytes) if record_bytes is not None
                        else sha256_hex(f"{item_id}:{degraded}:{unit}".encode("utf-8"))),
        ),
    )


def _response_records(response: object, mapping_key: str | None) -> list | None:
    """Real econ answers are bare lists; a mapping is only accepted when it
    carries the expected container key."""
    if isinstance(response, list):
        return response
    if isinstance(response, Mapping) and mapping_key is not None:
        records = response.get(mapping_key)
        if isinstance(records, list):
            return records
    return None


def _raw_response_bytes(response: object) -> bytes:
    if isinstance(response, (Mapping, list)):
        try:
            return canonical_json_bytes(response)
        except SchemaError:
            return b"unserializable"
    return b"unknown"


def _parse_pipeline_status(response: object, received_nano: int) -> tuple[MacroItem, ...]:
    if not isinstance(response, Mapping):
        return (_degraded_marker("get_pipeline_status", _raw_response_bytes(response),
                                 received_nano, "response_not_object"),)
    # Liveness signal only: kind names and alert fields are never interpreted
    # into items or probability claims.
    return ()


def _parse_meetings(response: object, received_nano: int,
                    validated_nano: int) -> tuple[tuple[MacroItem, ...], dict[str, int]]:
    """Meeting metadata: real degradation evidence and the only source of
    verified per-meeting publication times for the investment join."""
    records = _response_records(response, "meetings")
    if records is None:
        return ((_degraded_marker("get_recent_meetings", _raw_response_bytes(response),
                                  received_nano, "response_schema_unknown"),), {})
    items: list[MacroItem] = []
    published_times: dict[str, int] = {}
    for record in records:
        if not isinstance(record, Mapping):
            continue
        meeting_id = _record_str(record, "meeting_id")
        if not meeting_id:
            continue
        # Header state ("published") is never a timestamp: only the explicit
        # published_at field supplies one, and it may legitimately be null.
        published = _iso_to_nano(record.get("published_at"))
        if published is not None:
            published_times[meeting_id] = published
        remote_degradation = _record_str(record, "degradation_state")
        degraded = remote_degradation not in (None, "", "full_panel")
        business_date = _record_str(record, "business_date") or ""
        items.append(_assemble_item(
            item_id=stable_id("macro", f"get_recent_meetings|{meeting_id}"),
            kind="ECON_OPINION",
            text=f"meeting:{meeting_id}:{business_date}",
            record_bytes=canonical_json_bytes(record),
            url_tuple=(), published_nano=published,
            received_nano=received_nano, validated_nano=validated_nano,
            meeting_id=meeting_id, assets=(), products=(), unit=None,
            degraded=degraded,
            reasons=("expert_degradation",) if degraded else (),
        ))
    return tuple(items), published_times


def _parse_frontpage(response: object, received_nano: int,
                     validated_nano: int) -> tuple[MacroItem, ...]:
    """Real frontpage shape: {"hero": {...}, "featured": [...]} with per-story
    ISO ``ts``. The story ts is source-declared publication evidence; the
    system first_seen stays this round's receipt clock."""
    if not isinstance(response, Mapping) or (
            "hero" not in response and "featured" not in response):
        return (_degraded_marker("get_frontpage_news", _raw_response_bytes(response),
                                 received_nano, "response_schema_unknown"),)
    records: list[Mapping[str, object]] = []
    hero = response.get("hero")
    if isinstance(hero, Mapping):
        records.append(hero)
    featured = response.get("featured")
    if isinstance(featured, list):
        records.extend(record for record in featured if isinstance(record, Mapping))
    items: list[MacroItem] = []
    for record in records:
        record_id = _record_str(record, "id")
        text = _record_str(record, "title") or _record_str(record, "summary")
        if not record_id or text is None:
            continue
        url = _record_str(record, "url") or ""
        items.append(_assemble_item(
            item_id=stable_id("macro", f"get_frontpage_news|{record_id}"),
            kind="FACT", text=text,
            record_bytes=canonical_json_bytes(record),
            url_tuple=(url,) if url else (),
            published_nano=_iso_to_nano(record.get("ts")),
            received_nano=received_nano, validated_nano=validated_nano,
            meeting_id=None, assets=(), products=(), unit=None,
            degraded=False, reasons=(),
        ))
    return tuple(items)


def _parse_investments(response: object, received_nano: int, validated_nano: int,
                       meeting_times: Mapping[str, int]) -> tuple[MacroItem, ...]:
    """Real investments shape: a bare list joined to verified meeting
    publication times by meeting_id. Free-text horizons are not
    machine-verifiable deadlines, so opinions degrade (01 §6). Remote numeric
    probabilities stay in the raw hash/text only, never in MarketForecast."""
    records = _response_records(response, "items")
    if records is None:
        return (_degraded_marker("get_investments", _raw_response_bytes(response),
                                 received_nano, "response_schema_unknown"),)
    items: list[MacroItem] = []
    for record in records:
        if not isinstance(record, Mapping):
            continue
        meeting_id = _record_str(record, "meeting_id")
        text = _record_str(record, "rationale_zh") or _record_str(record, "rationale_en")
        if text is None:
            continue
        probability = record.get("probability")
        unit = "probability" if (isinstance(probability, (int, float))
                                 and not isinstance(probability, bool)) else None
        asset = _record_str(record, "asset")
        # F15: investment identity is the stable entity (meeting+asset),
        # not the content hash — content edits are revisions of the same item.
        items.append(_assemble_item(
            item_id=stable_id("macro", f"get_investments|{meeting_id}|{asset or 'unknown'}"),
            kind="ECON_OPINION", text=text,
            record_bytes=canonical_json_bytes(record),
            url_tuple=(),
            published_nano=meeting_times.get(meeting_id) if meeting_id else None,
            received_nano=received_nano, validated_nano=validated_nano,
            meeting_id=meeting_id, assets=(asset,) if asset else (),
            products=(), unit=unit,
            degraded=True, reasons=("opinion_deadline_not_verifiable",),
        ))
    return tuple(items)


def _parse_tool_response(tool: str, response: object, received_nano: int,
                         validated_nano: int,
                         meeting_times: Mapping[str, int] | None = None) -> tuple[MacroItem, ...]:
    """Adapter macro-adapter-v2.1.1 over the real econ shapes: only the
    documented fields are read; unknown shapes degrade with the raw hash."""
    if tool == "get_pipeline_status":
        return _parse_pipeline_status(response, received_nano)
    if tool == "get_recent_meetings":
        items, _ = _parse_meetings(response, received_nano, validated_nano)
        return items
    if tool == "get_frontpage_news":
        return _parse_frontpage(response, received_nano, validated_nano)
    if tool == "get_investments":
        return _parse_investments(response, received_nano, validated_nano,
                                  meeting_times or {})
    return (_degraded_marker(tool, _raw_response_bytes(response), received_nano,
                             "unknown_tool"),)


async def refresh_macro_cache(transport: EconTransport, cache: MacroCache,
                              retrieved_at_nano: int,
                              clock_nano: Callable[[], int]) -> tuple[MacroItem, ...]:
    """Refresh the cache from the four known econ tools and append new items.

    ``retrieved_at_nano`` is only the request-start lower bound. Every
    response is timestamped by the injected clock at receipt and again at
    validation completion; item ``available_at`` is the validation time (at
    least the receipt time). A clock that runs backwards degrades that
    response instead of moving evidence backwards in time.
    """
    if isinstance(retrieved_at_nano, bool) or not isinstance(retrieved_at_nano, int):
        raise TypeError("retrieved_at_nano_must_be_int")
    if not callable(clock_nano):
        raise TypeError("clock_nano_must_be_callable")
    if not isinstance(cache, MacroCache):
        raise TypeError("cache_must_be_MacroCache")
    new_items: list[MacroItem] = []
    meeting_times: dict[str, int] = {}
    last_clock = retrieved_at_nano
    for tool in _REFRESH_TOOLS:
        response = await transport.call_tool(tool, {})
        received = clock_nano()
        went_backwards = received < last_clock
        if went_backwards:
            received = last_clock
        validated = clock_nano()
        if validated < received:
            went_backwards = True
            validated = received
        last_clock = max(last_clock, validated)
        if went_backwards:
            new_items.append(_degraded_marker(
                tool, _raw_response_bytes(response), received, "clock_went_backwards"))
            continue
        if tool == "get_recent_meetings":
            items, meeting_times = _parse_meetings(response, received, validated)
            new_items.extend(items)
        else:
            new_items.extend(_parse_tool_response(tool, response, received, validated,
                                                  meeting_times))
    # F15: identical unchanged polls stay idempotent; a changed body for
    # the same source entity is a NEW REVISION, not a new item id. Observation
    # timestamps (first_seen/retrieved/available) never make an unchanged
    # body look like a conflict — only content (raw_sha256) separates versions.
    existing_versions: dict[str, dict[str, MacroItem]] = {}
    for existing in cache.items:
        existing_versions.setdefault(existing.item_id, {})[existing.raw_sha256] = existing
    versioned: list[MacroItem] = []
    for item in new_items:
        entity = existing_versions.get(item.item_id)
        if entity is None:
            versioned.append(item)
            continue
        prior = entity.get(item.raw_sha256)
        if prior is not None:
            # Same content already observed: keep the FIRST observation only.
            continue
        item = _replace(item, revision=f"r{len(entity) + 1}")
        versioned.append(item)
    appended = tuple(versioned)
    if appended:
        cache.append(appended)
    return appended


def derive_macro_tags(items: tuple[MacroItem, ...], as_of_nano: int) -> tuple[MacroItem, ...]:
    """Machine-checkable conservative literal tagger (macro-risk-v2.1.0).

    Only FACT items with a source URL, a valid publication/first-seen, no
    degradation and unexpired validity are matched. Text is Unicode NFKC +
    casefold normalized and matched by literal substring; negations hit on
    purpose (false-block analysis happens downstream). Multi-hit parents
    take the union of tags; derived tags inherit the earliest parent cutoff
    and the latest parent availability.
    """
    if not isinstance(items, tuple) or any(not isinstance(item, MacroItem) for item in items):
        raise TypeError("items_must_be_tuple_of_MacroItem")
    if isinstance(as_of_nano, bool) or not isinstance(as_of_nano, int):
        raise TypeError("as_of_nano_must_be_int")
    # F15(r3): when multiple revisions of the same entity are provided,
    # only the PIT-latest revision is eligible for tag derivation — a
    # superseded "war" initial must not produce a tag when the correction
    # removed the risk language.
    latest_by_entity: dict[str, MacroItem] = {}
    for item in sorted(items, key=lambda i: (i.item_id, i.available_at_nano,
                                              i.first_seen_nano, i.revision)):
        latest_by_entity[item.item_id] = item
    eligible = [
        item for item in latest_by_entity.values()
        if item.kind == "FACT"
        and item.publication_state == "published"
        and item.published_at_nano is not None
        and item.degradation_state == "normal"
        and item.source_urls
        and item.valid_until_nano > as_of_nano
    ]
    matches: dict[str, list[MacroItem]] = {}
    for item in eligible:
        normalized = unicodedata.normalize("NFKC", item.text).casefold()
        for tag, words in TAG_WORDS.items():
            if any(word in normalized for word in words):
                matches.setdefault(tag, []).append(item)
    derived: list[MacroItem] = []
    for tag in sorted(matches):
        parents = matches[tag]
        identity = f"{tag}|{MACRO_POLICY_VERSION}|" + "|".join(sorted(p.item_id for p in parents))
        derived.append(MacroItem(
            item_id=stable_id("macro-tag", identity),
            meeting_id=None,
            revision="derived",
            kind="DERIVED_TAG",
            derived_tag=tag,
            derivation_version=MACRO_POLICY_VERSION,
            published_at_nano=min(p.published_at_nano for p in parents),
            first_seen_nano=as_of_nano,
            retrieved_at_nano=as_of_nano,
            available_at_nano=max(max(p.available_at_nano for p in parents), as_of_nano),
            valid_from_nano=as_of_nano,
            valid_until_nano=min(p.valid_until_nano for p in parents),
            publication_state="published",
            degradation_state="normal",
            assets=(),
            products=TAG_PRODUCTS[tag],
            unit=None,
            source_urls=tuple(dict.fromkeys(url for p in parents for url in p.source_urls)),
            text=tag,
            parent_ids=tuple(sorted(p.item_id for p in parents)),
            raw_sha256=sha256_hex(canonical_json_bytes({
                "tag": tag, "version": MACRO_POLICY_VERSION,
                "parents": sorted(p.item_id for p in parents),
                # F15(r3): bind the actual parent revision content —
                # different revisions of the same entity produce
                # different tag hashes.
                "parent_contents": sorted(
                    f"{p.item_id}:{p.revision}:{p.raw_sha256}" for p in parents),
            })),
            quality=DataQuality(
                status="READY", reasons=(), temporal_basis="observed",
                input_hash=sha256_hex("|".join(sorted(
                    f"{p.item_id}:{p.revision}:{p.raw_sha256}" for p in parents
                )).encode("utf-8")),
            ),
        ))
    return tuple(derived)
