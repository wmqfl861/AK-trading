"""Daily exchange-rule collection orchestration without trading dependencies."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Iterable

from ak_trading.exchange_rule_artifacts import (
    ExchangeRuleArtifactStore,
    build_catalog_collection_run,
    build_collection_run,
    source_registry_sha256,
)
from ak_trading.exchange_rule_sources import (
    CollectionEvidence,
    DiscoveredDocument,
    ExchangeRuleSourceError,
    ExchangeSource,
    LocalWebSearchDocumentFetcher,
    build_exchange_sources_from_catalog,
    collect_evidence,
    discover_official_documents,
    document_suffix,
    fetch_exchange_daily_parameters,
    fixture_documents,
    load_exchange_sources,
    validate_sources_against_catalog,
)
from ak_trading.exchange_rules import (
    ExchangeRuleError,
    FetchedDocument,
    RuleSnapshot,
    RuleStatus,
    SourceDocument,
    diff_snapshots,
    legacy_reference_snapshot,
)
from ak_trading.futures_catalog import FuturesCatalogError, load_futures_catalog
from ak_trading.market_reference import ReferenceDataError, SHANGHAI_TZ, load_reference_book


class ExchangeRuleCollectionError(RuntimeError):
    """Raised for invalid collection configuration or an unsafe collection state."""


@dataclass(frozen=True)
class CollectionResult:
    """The local result of one collection or legacy-bootstrap execution."""

    snapshot: RuleSnapshot | None
    previous_snapshot: RuleSnapshot | None
    changes: tuple
    run: dict[str, object]
    run_path: Path
    snapshot_path: Path | None
    documents_written: tuple[Path, ...]
    catalog_run: dict[str, object] | None = None
    catalog_run_path: Path | None = None

    @property
    def exit_code(self) -> int:
        status = self.run["status"]
        if status in {"success", "skipped_closed_day"}:
            return 0
        if status == "review_required":
            return 1
        return 2


def _require_aware(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ExchangeRuleCollectionError("collection clock must be timezone-aware")
    return now.astimezone(SHANGHAI_TZ)


def _source_documents_from_fixture(
    source: ExchangeSource,
    fixture_path: Path,
    now: datetime,
) -> tuple[tuple[DiscoveredDocument, FetchedDocument], ...]:
    return fixture_documents(fixture_path, source=source, collected_at=now)


def _choose_previous_snapshot(store: ExchangeRuleArtifactStore) -> RuleSnapshot | None:
    return store.current_snapshot() or store.latest_legacy_baseline()


def _candidate_status(evidence: Iterable[CollectionEvidence]) -> tuple[RuleStatus, str | None]:
    errors: list[str] = []
    reviews: list[str] = []
    for item in evidence:
        errors.extend(item.errors)
        reviews.extend(item.review_reasons)
        if any(rule.status is RuleStatus.REVIEW_REQUIRED for rule in item.items):
            reviews.append("At least one collected rule field needs manual review.")
    if errors and not reviews:
        return RuleStatus.REVIEW_REQUIRED, "; ".join(sorted(set(errors)))
    if reviews or errors:
        return RuleStatus.REVIEW_REQUIRED, "; ".join(sorted(set(reviews + errors)))
    return RuleStatus.CANDIDATE, "All configured fields were deterministically collected."


def _write_documents(
    store: ExchangeRuleArtifactStore,
    evidence: Iterable[CollectionEvidence],
) -> tuple[Path, ...]:
    paths: list[Path] = []
    for item in evidence:
        for document in item.documents:
            suffix = Path(document.content_path).suffix or document_suffix(
                document.content_type, document.url
            )
            paths.append(
                store.store_document(
                    content_sha256=document.content_sha256,
                    suffix=suffix,
                    content=document.raw_content,
                )
            )
    return tuple(sorted(set(paths)))


def _fixture_fetcher(
    fetched_by_url: dict[str, FetchedDocument],
) -> Callable[[str], FetchedDocument]:
    def fetch(url: str) -> FetchedDocument:
        try:
            return fetched_by_url[url]
        except KeyError as exc:
            raise ExchangeRuleSourceError(f"fixture has no document for {url}") from exc

    return fetch


def _collect_source(
    source: ExchangeSource,
    *,
    store: ExchangeRuleArtifactStore,
    now: datetime,
    fixture_path: Path | None,
    search_timeout: float,
    fetch_timeout: float,
    websearch_executable: str | Path | None,
    humanlike_enabled: bool,
    target_day: date | None = None,
) -> CollectionEvidence:
    def content_path_builder(content_sha256: str, suffix: str) -> str:
        return store.document_relative_path(content_sha256, suffix)

    if fixture_path is not None:
        fixture_pairs = _source_documents_from_fixture(source, fixture_path, now)
        discovered = tuple(discovered for discovered, _ in fixture_pairs)
        fetched_by_url = {discovered.url: fetched for discovered, fetched in fixture_pairs}
        return collect_evidence(
            source,
            discovered,
            fetcher=_fixture_fetcher(fetched_by_url),
            content_path_builder=content_path_builder,
            target_day=target_day,
        )

    discovered, discovery_errors = discover_official_documents(
        source,
        executable=websearch_executable,
        timeout=search_timeout,
    )
    fetcher = LocalWebSearchDocumentFetcher(
        executable=websearch_executable,
        timeout=fetch_timeout,
        now=lambda: now,
        exchange=source.code,
        exchange_homepage=source.homepage_url,
        official_domains=source.official_domains,
        humanlike_enabled=humanlike_enabled,
    )

    shared_documents: tuple[SourceDocument, ...] = ()
    if target_day is not None:
        daily = fetch_exchange_daily_parameters(
            source.code,
            target_day,
            timeout=fetch_timeout,
            official_domains=source.official_domains,
        )
        if daily is not None:
            preliminary = SourceDocument.from_fetched(
                daily,
                exchange=source.code,
                content_path="pending",
            )
            shared_documents = (
                SourceDocument.from_fetched(
                    daily,
                    exchange=source.code,
                    content_path=content_path_builder(
                        preliminary.content_sha256,
                        document_suffix(daily.content_type, daily.url),
                    ),
                ),
            )
    return collect_evidence(
        source,
        discovered,
        fetcher=fetcher.fetch,
        content_path_builder=content_path_builder,
        discovery_errors=discovery_errors,
        target_day=target_day,
        shared_documents=shared_documents,
    )


def collect_exchange_rules(
    *,
    store: ExchangeRuleArtifactStore,
    source_registry_path: str | Path,
    reference_path: str | Path,
    now: datetime,
    trading_day: date | None = None,
    fixture_paths: dict[str, Path] | None = None,
    search_timeout: float = 45.0,
    fetch_timeout: float = 120.0,
    websearch_executable: str | Path | None = None,
    humanlike_enabled: bool = True,
) -> CollectionResult:
    """Collect candidate evidence for all configured exchanges.

    Every field remains a candidate or review-required. This function never moves
    the current-approved pointer and never reads credentials or TqSdk settings.
    """

    local_now = _require_aware(now)
    try:
        book = load_reference_book(Path(reference_path))
    except ReferenceDataError as exc:
        raise ExchangeRuleCollectionError("cannot load the legacy trading calendar") from exc
    registry_path = Path(source_registry_path)
    target_day = trading_day or book.calendar.target_trading_day(local_now)
    try:
        book.calendar.require_covered(target_day)
    except ReferenceDataError as exc:
        raise ExchangeRuleCollectionError(
            "requested trading day is outside calendar coverage"
        ) from exc
    if not book.calendar.is_trading_day(target_day):
        previous = _choose_previous_snapshot(store)
        run = build_collection_run(
            target_trading_day=target_day,
            started_at=local_now,
            completed_at=local_now,
            status="skipped_closed_day",
            source_registry_sha256=source_registry_sha256(source_registry_path),
            snapshot_id=None,
            previous_snapshot_id=previous.snapshot_id if previous else None,
            document_ids=(),
            errors=(),
            review_reasons=("Configured calendar marks this target as a closed day.",),
        )
        run_path = store.store_run(run)
        return CollectionResult(
            snapshot=None,
            previous_snapshot=previous,
            changes=(),
            run=run,
            run_path=run_path,
            snapshot_path=None,
            documents_written=(),
        )
    sources = load_exchange_sources(registry_path)
    expected_sources = {product.exchange for product in book.products}
    configured_sources = {source.code for source in sources}
    if expected_sources != configured_sources:
        raise ExchangeRuleCollectionError(
            "source registry exchanges do not match the legacy reference universe"
        )
    expected_products = {product.code for product in book.products}
    configured_products = {
        product.code.upper() for source in sources for product in source.products
    }
    if expected_products != configured_products:
        raise ExchangeRuleCollectionError(
            "source registry products do not match the legacy reference universe"
        )

    started_at = local_now
    previous = _choose_previous_snapshot(store)
    evidences: list[CollectionEvidence] = []
    collection_errors: list[str] = []
    fixture_paths = fixture_paths or {}
    for source in sources:
        try:
            evidences.append(
                _collect_source(
                    source,
                    store=store,
                    now=local_now,
                    fixture_path=fixture_paths.get(source.code),
                    search_timeout=search_timeout,
                    fetch_timeout=fetch_timeout,
                    websearch_executable=websearch_executable,
                    humanlike_enabled=humanlike_enabled,
                    target_day=target_day,
                )
            )
        except ExchangeRuleSourceError as exc:
            collection_errors.append(f"{source.code}: {type(exc).__name__}: {exc}")
            evidences.append(
                collect_evidence(
                    source,
                    (),
                    fetcher=lambda url: (_ for _ in ()).throw(
                        ExchangeRuleSourceError("unexpected fetch for empty discovery")
                    ),
                    content_path_builder=lambda content_sha256, suffix: store.document_relative_path(
                        content_sha256, suffix
                    ),
                    target_day=target_day,
                )
            )

    all_documents = tuple(
        document for evidence in evidences for document in evidence.documents
    )
    all_items = tuple(rule for evidence in evidences for rule in evidence.items)
    all_errors = collection_errors + [
        error for evidence in evidences for error in evidence.errors
    ]
    all_reviews = [
        reason for evidence in evidences for reason in evidence.review_reasons
    ]
    status, review_reason = _candidate_status(evidences)
    if collection_errors:
        status = RuleStatus.REVIEW_REQUIRED
        review_reason = "; ".join(sorted(set((review_reason and [review_reason] or []) + collection_errors)))
    completed_at = local_now
    documents_written = _write_documents(store, evidences)
    snapshot: RuleSnapshot | None = None
    snapshot_path: Path | None = None
    changes = ()
    if all_items:
        snapshot = RuleSnapshot.create(
            target_trading_day=target_day,
            collected_at=completed_at,
            status=status,
            origin="official_exchange_collection",
            documents=all_documents,
            items=all_items,
            review_reason=review_reason,
            parent_snapshot_id=previous.snapshot_id if previous else None,
        )
        snapshot_path = store.store_snapshot(snapshot)
        changes = diff_snapshots(previous, snapshot)
        run_status = "review_required" if status is RuleStatus.REVIEW_REQUIRED else "success"
    else:
        run_status = "error"
        all_errors.append("No official exchange-rule items were collected.")

    run = build_collection_run(
        target_trading_day=target_day,
        started_at=started_at,
        completed_at=completed_at,
        status=run_status,
        source_registry_sha256=source_registry_sha256(registry_path),
        snapshot_id=snapshot.snapshot_id if snapshot else None,
        previous_snapshot_id=previous.snapshot_id if previous else None,
        document_ids=(document.document_id for document in all_documents),
        errors=all_errors,
        review_reasons=all_reviews,
    )
    run_path = store.store_run(run)
    return CollectionResult(
        snapshot=snapshot,
        previous_snapshot=previous,
        changes=changes,
        run=run,
        run_path=run_path,
        snapshot_path=snapshot_path,
        documents_written=documents_written,
    )


def collect_exchange_rules_from_catalog(
    *,
    store: ExchangeRuleArtifactStore,
    catalog_path: str | Path,
    now: datetime,
    trading_day: date | None = None,
    fixture_paths: dict[str, Path] | None = None,
    search_timeout: float = 45.0,
    fetch_timeout: float = 120.0,
    websearch_executable: str | Path | None = None,
    humanlike_enabled: bool = True,
) -> CollectionResult:
    """Collect all catalog-listed futures without using legacy market_reference.

    Each exchange is evaluated against its own calendar. Product-level rule items
    use a null representative contract, and unresolved parser/source fields remain
    explicit review-required values.
    """

    local_now = _require_aware(now)
    try:
        catalog = load_futures_catalog(catalog_path, require_all_exchanges=True)
        sources = build_exchange_sources_from_catalog(catalog_path)
    except (FuturesCatalogError, ExchangeRuleSourceError) as exc:
        raise ExchangeRuleCollectionError("cannot load official futures catalog") from exc
    target_day = trading_day or catalog.target_trading_day(local_now)
    for calendar in catalog.calendars:
        try:
            calendar.require_covered(target_day)
        except FuturesCatalogError as exc:
            raise ExchangeRuleCollectionError(str(exc)) from exc

    started_at = local_now
    previous = _choose_previous_snapshot(store)
    fixture_paths = fixture_paths or {}
    evidences: list[CollectionEvidence] = []
    collection_errors: list[str] = []
    exchange_statuses: dict[str, str] = {}
    processed_pairs: set[tuple[str, str]] = set()
    closed_pairs: set[tuple[str, str]] = set()
    review_reasons: list[str] = []
    closed_reviews: list[str] = []

    for source in sources:
        exchange = source.code.upper()
        exchange_products = catalog.products_for_exchange(exchange)
        calendar = catalog.calendar_for(exchange)
        if not calendar.is_trading_day(target_day):
            exchange_statuses[exchange] = "closed"
            closed_pairs.update(product.identity for product in exchange_products)
            closed_reviews.append(
                f"{exchange}: official calendar marks {target_day} as closed."
            )
            continue
        try:
            evidence = _collect_source(
                source,
                store=store,
                now=local_now,
                fixture_path=fixture_paths.get(exchange),
                search_timeout=search_timeout,
                fetch_timeout=fetch_timeout,
                websearch_executable=websearch_executable,
                humanlike_enabled=humanlike_enabled,
                target_day=target_day,
            )
            evidences.append(evidence)
            exchange_statuses[exchange] = (
                "partial"
                if evidence.errors
                else "review_required"
                if evidence.review_reasons
                or any(item.status is RuleStatus.REVIEW_REQUIRED for item in evidence.items)
                else "collected"
            )
            processed_pairs.update(product.identity for product in exchange_products)
            review_reasons.extend(evidence.review_reasons)
        except ExchangeRuleSourceError as exc:
            collection_errors.append(f"{exchange}: {type(exc).__name__}: {exc}")
            exchange_statuses[exchange] = "error"
            review_reasons.append(f"{exchange}: no complete evidence was collected.")
            evidences.append(
                collect_evidence(
                    source,
                    (),
                    fetcher=lambda url: (_ for _ in ()).throw(
                        ExchangeRuleSourceError("unexpected fetch for empty discovery")
                    ),
                    content_path_builder=lambda content_sha256, suffix: store.document_relative_path(
                        content_sha256, suffix
                    ),
                    target_day=target_day,
                )
            )

    all_documents = tuple(document for evidence in evidences for document in evidence.documents)
    all_items = tuple(rule for evidence in evidences for rule in evidence.items)
    all_errors = collection_errors + [
        error for evidence in evidences for error in evidence.errors
    ]
    all_exchanges_closed = len(closed_pairs) == len(catalog.future_products)
    combined_reviews = list(review_reasons)
    if catalog.review_reason:
        combined_reviews.append(f"Catalog review: {catalog.review_reason}")
    documents_written = _write_documents(store, evidences)
    snapshot: RuleSnapshot | None = None
    snapshot_path: Path | None = None
    changes = ()
    if all_exchanges_closed:
        run_status = "skipped_closed_day"
        combined_reviews.extend(closed_reviews)
    elif all_items:
        status, snapshot_review_reason = _candidate_status(evidences)
        if collection_errors or catalog.review_reason:
            status = RuleStatus.REVIEW_REQUIRED
        if snapshot_review_reason:
            combined_reviews.append(snapshot_review_reason)
        snapshot = RuleSnapshot.create(
            target_trading_day=target_day,
            collected_at=local_now,
            status=status,
            origin="official_futures_catalog_collection",
            documents=all_documents,
            items=all_items,
            review_reason="; ".join(sorted(set(combined_reviews)))
            or "Catalog-driven futures collection requires review.",
            parent_snapshot_id=previous.snapshot_id if previous else None,
        )
        snapshot_path = store.store_snapshot(snapshot)
        changes = diff_snapshots(previous, snapshot)
        run_status = "review_required" if status is RuleStatus.REVIEW_REQUIRED else "success"
    else:
        run_status = "error"
        all_errors.append("No official futures rule items were collected.")

    run = build_collection_run(
        target_trading_day=target_day,
        started_at=started_at,
        completed_at=local_now,
        status=run_status,
        source_registry_sha256=catalog.catalog_id,
        snapshot_id=snapshot.snapshot_id if snapshot else None,
        previous_snapshot_id=previous.snapshot_id if previous else None,
        document_ids=(document.document_id for document in all_documents),
        errors=all_errors,
        review_reasons=combined_reviews,
    )
    run_path = store.store_run(run)
    catalog_run = build_catalog_collection_run(
        target_trading_day=target_day,
        started_at=started_at,
        completed_at=local_now,
        catalog_id=catalog.catalog_id,
        exchange_statuses=exchange_statuses,
        expected_futures=(product.identity for product in catalog.future_products),
        processed_futures=processed_pairs,
        closed_futures=closed_pairs,
        excluded_options=(item.as_dict() for item in catalog.excluded_options),
        snapshot_id=snapshot.snapshot_id if snapshot else None,
        errors=all_errors,
        review_reasons=(
            () if all_exchanges_closed else combined_reviews
        ),
        document_ids=(document.document_id for document in all_documents),
        previous_snapshot_id=previous.snapshot_id if previous else None,
    )
    catalog_run_path = store.store_catalog_run(catalog_run)
    return CollectionResult(
        snapshot=snapshot,
        previous_snapshot=previous,
        changes=changes,
        run=run,
        run_path=run_path,
        snapshot_path=snapshot_path,
        documents_written=documents_written,
        catalog_run=catalog_run,
        catalog_run_path=catalog_run_path,
    )


def bootstrap_legacy_reference(
    *,
    store: ExchangeRuleArtifactStore,
    reference_path: str | Path,
    now: datetime,
    trading_day: date | None = None,
) -> CollectionResult:
    """Store the unapproved legacy reference exactly once as a diff baseline."""

    local_now = _require_aware(now)
    try:
        book = load_reference_book(Path(reference_path))
    except ReferenceDataError as exc:
        raise ExchangeRuleCollectionError("cannot load the legacy trading calendar") from exc
    target_day = trading_day or book.calendar.target_trading_day(local_now)
    book.calendar.require_covered(target_day)
    previous = _choose_previous_snapshot(store)
    if store.latest_legacy_baseline() is not None:
        raise ExchangeRuleCollectionError(
            "legacy baseline already exists; bootstrap is one-time"
        )
    document_path = store.document_relative_path(
        hashlib.sha256(Path(reference_path).read_bytes()).hexdigest(),
        ".json",
    )
    snapshot = legacy_reference_snapshot(
        reference_path,
        target_trading_day=target_day,
        collected_at=local_now,
        document_path=document_path,
    )
    document = snapshot.documents[0]
    written = store.store_document(
        content_sha256=document.content_sha256,
        suffix=".json",
        content=document.raw_content,
    )
    snapshot_path = store.store_snapshot(snapshot)
    changes = diff_snapshots(previous, snapshot)
    run = build_collection_run(
        target_trading_day=target_day,
        started_at=local_now,
        completed_at=local_now,
        status="review_required",
        source_registry_sha256=None,
        snapshot_id=snapshot.snapshot_id,
        previous_snapshot_id=previous.snapshot_id if previous else None,
        document_ids=(document.document_id,),
        errors=(),
        review_reasons=(
            "Legacy market_reference.json was stored as an unapproved baseline; "
            "official source collection and human review remain required.",
        ),
    )
    run_path = store.store_run(run)
    return CollectionResult(
        snapshot=snapshot,
        previous_snapshot=previous,
        changes=changes,
        run=run,
        run_path=run_path,
        snapshot_path=snapshot_path,
        documents_written=(written,),
    )
