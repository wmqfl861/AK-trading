from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Any, Mapping

from ak_trading.journal import sanitize_event
from ak_trading.market_reference import ReferenceDataError, reference_book_from_mapping
from ak_trading.persistence.config import DatabaseSettings, redact_database_text
from ak_trading.persistence.database import DatabaseOperationError

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection, Engine


class PersistenceError(DatabaseOperationError):
    """A credential-safe persistence or artifact-validation failure."""


class PersistenceConflictError(PersistenceError):
    """Raised when an immutable database identity has different content."""


@dataclass(frozen=True)
class ReferenceSnapshotResult:
    snapshot_id: int
    content_sha256: str
    inserted: bool
    products: int
    closed_dates: int


@dataclass(frozen=True)
class JournalImportResult:
    source: str
    runs: int
    events_read: int
    events_inserted: int


@dataclass(frozen=True)
class ReportImportResult:
    identity: str
    content_sha256: str
    inserted: bool


def result_dict(result: object) -> dict[str, Any]:
    """Convert one public result dataclass into a JSON-serializable mapping."""
    return asdict(result)  # type: ignore[arg-type]


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant {value!r}")


def _decode_json_bytes(raw_bytes: bytes, source: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(
            raw_bytes.decode("utf-8"), parse_constant=_reject_json_constant
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise PersistenceError(f"invalid JSON artifact: {source}") from exc
    if not isinstance(value, Mapping):
        raise PersistenceError(f"JSON artifact root must be an object: {source}")
    return value


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PersistenceError("artifact contains a non-JSON value") from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _verify_preopen_report_id(payload: Mapping[str, Any], report_id: str) -> None:
    if len(report_id) != 64 or any(character not in "0123456789abcdef" for character in report_id):
        raise PersistenceError("preopen.report_id must be a lowercase SHA-256 digest")
    without_id = dict(payload)
    without_id.pop("report_id", None)
    canonical = json.dumps(
        without_id,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if _sha256(canonical) != report_id:
        raise PersistenceError("preopen.report_id does not match report content")


def _validate_preopen_reference(
    payload: Mapping[str, Any], reference_content: Mapping[str, Any]
) -> None:
    book = reference_book_from_mapping(reference_content)
    if payload.get("expected_products") != list(book.expected_products):
        raise PersistenceError("pre-open report products do not match reference snapshot")
    if payload.get("broker_markup") != book.broker_markup.as_dict():
        raise PersistenceError("pre-open report broker markup does not match reference snapshot")
    expected_calendar = {
        "valid_from": book.calendar.valid_from.isoformat(),
        "valid_through": book.calendar.valid_through.isoformat(),
        "source_url": book.calendar.source_url,
        "last_reviewed_date": book.calendar.last_reviewed_date.isoformat(),
    }
    if payload.get("calendar") != expected_calendar:
        raise PersistenceError("pre-open report calendar does not match reference snapshot")
    products = payload.get("products")
    if not isinstance(products, list) or len(products) != len(book.products):
        raise PersistenceError("pre-open report product details are incomplete")
    for report_product, product in zip(products, book.products, strict=True):
        if not isinstance(report_product, Mapping):
            raise PersistenceError("pre-open report product must be an object")
        expected = {
            "code": product.code,
            "exchange": product.exchange,
            "main_symbol": product.main_symbol,
            "research_contract": product.research_contract,
            "last_trading_day": product.last_trading_day.isoformat(),
            "contract_size": {
                "value": format(product.contract_size, "f"),
                "unit": "ton_per_lot",
            },
            "price_tick": {
                "value": format(product.price_tick, "f"),
                "unit": "cny_per_ton",
            },
            "sessions": {
                "day": list(product.day_sessions),
                "night": list(product.night_sessions),
                "calendar_warning": "Night sessions and holiday exceptions follow exchange notices.",
            },
            "fees": {
                "basis": product.fees.basis.value,
                "applicable_contracts": list(product.fees.applicable_contracts),
                "effective_from": product.fees.effective_from.isoformat(),
                "must_reverify_before": product.fees.must_reverify_before.isoformat(),
                "provenance": product.fees.provenance.as_dict(),
            },
            "margin": {
                "exchange_rate_of_notional": format(
                    product.margin.rate_of_notional, "f"
                ),
                "applicable_contracts": list(product.margin.applicable_contracts),
                "price_basis": product.margin.price_basis,
                "effective_from": product.margin.effective_from.isoformat(),
                "must_reverify_before": product.margin.must_reverify_before.isoformat(),
                "note": product.margin.note,
                "provenance": product.margin.provenance.as_dict(),
            },
            "static_provenance": product.static_provenance.as_dict(),
        }
        for field in (
            "code",
            "exchange",
            "main_symbol",
            "research_contract",
            "last_trading_day",
            "contract_size",
            "price_tick",
            "sessions",
            "static_provenance",
        ):
            if report_product.get(field) != expected[field]:
                raise PersistenceError(
                    f"pre-open report {product.code}.{field} does not match reference snapshot"
                )
        for section in ("fees", "margin"):
            actual_section = report_product.get(section)
            if not isinstance(actual_section, Mapping):
                raise PersistenceError(
                    f"pre-open report {product.code}.{section} must be an object"
                )
            for field, value in expected[section].items():
                if actual_section.get(field) != value:
                    raise PersistenceError(
                        f"pre-open report {product.code}.{section}.{field} "
                        "does not match reference snapshot"
                    )


def _safe_failure(
    action: str, exc: BaseException, settings: DatabaseSettings | None
) -> PersistenceError:
    return PersistenceError(
        f"{action} failed: {redact_database_text(exc, settings)}"
    )


def _required_text(value: object, field: str, *, maximum: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PersistenceError(f"{field} must be a non-empty string")
    result = value.strip()
    if maximum is not None and len(result) > maximum:
        raise PersistenceError(f"{field} exceeds {maximum} characters")
    return result


def _current_revision(connection: Connection) -> str | None:
    from sqlalchemy import text

    return connection.execute(
        text("SELECT version_num FROM alembic_version")
    ).scalar_one_or_none()


def _ensure_strategy_run(
    connection: Connection,
    manifest: Mapping[str, Any],
    *,
    source_path: str | None,
) -> tuple[str, bool]:
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert

    from ak_trading.persistence.schema import strategy_runs

    if manifest.get("event") != "run_manifest":
        raise PersistenceError("the first event for a run must be run_manifest")
    if manifest.get("schema_version") != 1:
        raise PersistenceError("unsupported run_manifest schema_version")
    run_id = _required_text(manifest.get("run_id"), "run_id", maximum=64)
    mode = _required_text(manifest.get("mode"), "mode", maximum=16)
    if mode not in {"sim", "backtest"}:
        raise PersistenceError("run_manifest.mode must be sim or backtest")
    symbol = _required_text(manifest.get("symbol"), "symbol")
    market_symbol = manifest.get("market_symbol")
    if market_symbol is not None:
        market_symbol = _required_text(market_symbol, "market_symbol")
    manifest_sha256 = _sha256(_canonical_json_bytes(manifest))
    statement = (
        insert(strategy_runs)
        .values(
            run_id=run_id,
            mode=mode,
            symbol=symbol,
            market_symbol=market_symbol,
            manifest=dict(manifest),
            manifest_sha256=manifest_sha256,
            source_path=source_path,
            alembic_revision=_current_revision(connection),
        )
        .on_conflict_do_nothing(index_elements=[strategy_runs.c.run_id])
        .returning(strategy_runs.c.run_id)
    )
    inserted = connection.execute(statement).scalar_one_or_none() is not None
    if not inserted:
        existing = connection.execute(
            select(strategy_runs.c.manifest_sha256).where(
                strategy_runs.c.run_id == run_id
            )
        ).scalar_one()
        if existing != manifest_sha256:
            raise PersistenceConflictError(
                f"run_id {run_id!r} already has a different manifest"
            )
    return run_id, inserted


def _insert_strategy_event(
    connection: Connection,
    *,
    run_id: str,
    sequence_no: int,
    payload: Mapping[str, Any],
    source_path: str | None,
    source_line: int | None,
) -> bool:
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert

    from ak_trading.persistence.schema import strategy_events

    event_run_id = _required_text(payload.get("run_id"), "event.run_id", maximum=64)
    if event_run_id != run_id:
        raise PersistenceError("event run_id does not match its manifest")
    event_type = _required_text(payload.get("event"), "event.event", maximum=64)
    payload_sha256 = _sha256(_canonical_json_bytes(payload))
    statement = (
        insert(strategy_events)
        .values(
            run_id=run_id,
            sequence_no=sequence_no,
            event_type=event_type,
            payload=dict(payload),
            payload_sha256=payload_sha256,
            source_path=source_path,
            source_line=source_line,
        )
        .on_conflict_do_nothing(
            index_elements=[
                strategy_events.c.run_id,
                strategy_events.c.sequence_no,
            ]
        )
        .returning(strategy_events.c.id)
    )
    inserted = connection.execute(statement).scalar_one_or_none() is not None
    if not inserted:
        existing = connection.execute(
            select(strategy_events.c.payload_sha256).where(
                strategy_events.c.run_id == run_id,
                strategy_events.c.sequence_no == sequence_no,
            )
        ).scalar_one()
        if existing != payload_sha256:
            raise PersistenceConflictError(
                f"run {run_id!r} sequence {sequence_no} has different content"
            )
    return inserted


class PostgresJournal:
    """Synchronously persist sanitized events with an internal sequence number."""

    def __init__(
        self,
        engine: Engine,
        *,
        settings: DatabaseSettings | None = None,
        source_path: str | Path | None = None,
    ) -> None:
        self._engine = engine
        self._settings = settings
        self._source_path = str(Path(source_path).resolve()) if source_path else None
        self._lock = Lock()
        self._run_id: str | None = None
        self._sequence_no = 0

    def __call__(self, event: Mapping[str, Any]) -> None:
        sanitized = sanitize_event(event)
        if not isinstance(sanitized, Mapping):
            raise PersistenceError("journal event must be an object")
        payload = dict(sanitized)
        with self._lock:
            next_sequence = self._sequence_no + 1
            try:
                with self._engine.begin() as connection:
                    if self._run_id is None:
                        run_id, _ = _ensure_strategy_run(
                            connection,
                            payload,
                            source_path=self._source_path,
                        )
                    else:
                        run_id = self._run_id
                        if payload.get("event") == "run_manifest":
                            raise PersistenceError(
                                "a journal run may contain only one run_manifest"
                            )
                    _insert_strategy_event(
                        connection,
                        run_id=run_id,
                        sequence_no=next_sequence,
                        payload=payload,
                        source_path=self._source_path,
                        source_line=None,
                    )
            except PersistenceError:
                raise
            except Exception as exc:
                raise _safe_failure("strategy event write", exc, self._settings) from None
            self._run_id = run_id
            self._sequence_no = next_sequence


def store_reference_snapshot(
    engine: Engine,
    path: str | Path,
    *,
    settings: DatabaseSettings | None = None,
) -> ReferenceSnapshotResult:
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert

    from ak_trading.persistence.schema import (
        reference_closed_dates,
        reference_products,
        reference_snapshots,
    )

    source = Path(path)
    try:
        raw_bytes = source.read_bytes()
        content = _decode_json_bytes(raw_bytes, source)
        book = reference_book_from_mapping(content)
        digest = _sha256(raw_bytes)
        with engine.begin() as connection:
            statement = (
                insert(reference_snapshots)
                .values(
                    content_sha256=digest,
                    schema_version=book.schema_version,
                    raw_json=raw_bytes,
                    content=dict(content),
                    source_path=str(source.resolve()),
                    calendar_valid_from=book.calendar.valid_from,
                    calendar_valid_through=book.calendar.valid_through,
                    broker_markup=dict(content["broker_markup"]),
                )
                .on_conflict_do_nothing(
                    index_elements=[reference_snapshots.c.content_sha256]
                )
                .returning(reference_snapshots.c.id)
            )
            snapshot_id = connection.execute(statement).scalar_one_or_none()
            inserted = snapshot_id is not None
            if snapshot_id is None:
                row = connection.execute(
                    select(
                        reference_snapshots.c.id,
                        reference_snapshots.c.raw_json,
                    ).where(reference_snapshots.c.content_sha256 == digest)
                ).one()
                snapshot_id = row.id
                if bytes(row.raw_json) != raw_bytes:
                    raise PersistenceConflictError(
                        "reference snapshot SHA-256 collision detected"
                    )
            else:
                raw_products = content["products"]
                connection.execute(
                    insert(reference_products),
                    [
                        {
                            "snapshot_id": snapshot_id,
                            "code": product.code,
                            "exchange": product.exchange,
                            "main_symbol": product.main_symbol,
                            "research_contract": product.research_contract,
                            "last_trading_day": product.last_trading_day,
                            "payload": dict(raw_products[index]),
                        }
                        for index, product in enumerate(book.products)
                    ],
                )
                if book.calendar.closed_dates:
                    connection.execute(
                        insert(reference_closed_dates),
                        [
                            {"snapshot_id": snapshot_id, "closed_date": closed_date}
                            for closed_date in sorted(book.calendar.closed_dates)
                        ],
                    )
        return ReferenceSnapshotResult(
            snapshot_id=int(snapshot_id),
            content_sha256=digest,
            inserted=inserted,
            products=len(book.products),
            closed_dates=len(book.calendar.closed_dates),
        )
    except (PersistenceError, ReferenceDataError):
        raise
    except Exception as exc:
        raise _safe_failure("reference snapshot import", exc, settings) from None


def load_reference_snapshot_bytes(
    engine: Engine,
    snapshot_id: int,
    *,
    settings: DatabaseSettings | None = None,
) -> bytes:
    from sqlalchemy import select

    from ak_trading.persistence.schema import reference_snapshots

    try:
        with engine.connect() as connection:
            raw = connection.execute(
                select(reference_snapshots.c.raw_json).where(
                    reference_snapshots.c.id == snapshot_id
                )
            ).scalar_one()
        return bytes(raw)
    except Exception as exc:
        raise _safe_failure("reference snapshot read", exc, settings) from None


def _read_journal(path: Path) -> dict[str, list[tuple[int, Mapping[str, Any]]]]:
    grouped: dict[str, list[tuple[int, Mapping[str, Any]]]] = defaultdict(list)
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                if not line.strip():
                    raise PersistenceError(
                        f"blank JSONL record at {path}:{line_no}"
                    )
                try:
                    value = json.loads(line, parse_constant=_reject_json_constant)
                except (json.JSONDecodeError, ValueError) as exc:
                    raise PersistenceError(
                        f"invalid JSONL record at {path}:{line_no}"
                    ) from exc
                if not isinstance(value, Mapping):
                    raise PersistenceError(
                        f"non-object JSONL record at {path}:{line_no}"
                    )
                sanitized = sanitize_event(value)
                if not isinstance(sanitized, Mapping):
                    raise PersistenceError(
                        f"non-object JSONL record at {path}:{line_no}"
                    )
                run_id = _required_text(
                    sanitized.get("run_id"),
                    f"{path}:{line_no}.run_id",
                    maximum=64,
                )
                grouped[run_id].append((line_no, dict(sanitized)))
    except UnicodeError as exc:
        raise PersistenceError(f"journal is not UTF-8: {path}") from exc
    if not grouped:
        raise PersistenceError(f"journal has no events: {path}")
    for run_id, records in grouped.items():
        manifests = [record for _, record in records if record.get("event") == "run_manifest"]
        if len(manifests) != 1 or records[0][1].get("event") != "run_manifest":
            raise PersistenceError(
                f"run {run_id!r} must have exactly one leading run_manifest"
            )
    return dict(grouped)


def import_journal(
    engine: Engine,
    path: str | Path,
    *,
    settings: DatabaseSettings | None = None,
) -> JournalImportResult:
    source = Path(path)
    grouped = _read_journal(source)
    inserted_events = 0
    source_path = str(source.resolve())
    try:
        with engine.begin() as connection:
            for run_id, records in grouped.items():
                stored_run_id, _ = _ensure_strategy_run(
                    connection,
                    records[0][1],
                    source_path=source_path,
                )
                if stored_run_id != run_id:
                    raise PersistenceError("journal grouping identity changed")
                for sequence_no, (line_no, payload) in enumerate(records, start=1):
                    if _insert_strategy_event(
                        connection,
                        run_id=run_id,
                        sequence_no=sequence_no,
                        payload=payload,
                        source_path=source_path,
                        source_line=line_no,
                    ):
                        inserted_events += 1
        return JournalImportResult(
            source=source_path,
            runs=len(grouped),
            events_read=sum(len(records) for records in grouped.values()),
            events_inserted=inserted_events,
        )
    except PersistenceError:
        raise
    except Exception as exc:
        raise _safe_failure("journal import", exc, settings) from None


def store_run_report(
    engine: Engine,
    path: str | Path,
    *,
    settings: DatabaseSettings | None = None,
) -> ReportImportResult:
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert

    from ak_trading.persistence.schema import run_reports, strategy_runs

    source = Path(path)
    try:
        raw_bytes = source.read_bytes()
        payload = _decode_json_bytes(raw_bytes, source)
        if payload.get("schema_version") != 1:
            raise PersistenceError("unsupported run report schema_version")
        run_id = _required_text(payload.get("run_id"), "report.run_id", maximum=64)
        lifecycle = payload.get("lifecycle")
        if not isinstance(lifecycle, Mapping):
            raise PersistenceError("report.lifecycle must be an object")
        state = _required_text(
            lifecycle.get("state"), "report.lifecycle.state", maximum=32
        )
        if state not in {"complete", "incomplete"}:
            raise PersistenceError("report.lifecycle.state is unsupported")
        report_manifest = payload.get("manifest")
        identity = payload.get("identity")
        if not isinstance(report_manifest, Mapping):
            raise PersistenceError("report.manifest must be an object")
        if not isinstance(identity, Mapping):
            raise PersistenceError("report.identity must be an object")
        digest = _sha256(raw_bytes)
        with engine.begin() as connection:
            stored_manifest = connection.execute(
                select(strategy_runs.c.manifest).where(strategy_runs.c.run_id == run_id)
            ).scalar_one_or_none()
            if stored_manifest is None:
                raise PersistenceError(
                    f"run report references unknown run_id {run_id!r}"
                )
            if dict(report_manifest) != stored_manifest:
                raise PersistenceError(
                    f"run report manifest does not match run_id {run_id!r}"
                )
            expected_identity = {
                "mode": stored_manifest.get("mode"),
                "symbol": stored_manifest.get("symbol"),
            }
            if dict(identity) != expected_identity:
                raise PersistenceError(
                    f"run report identity does not match run_id {run_id!r}"
                )
            statement = (
                insert(run_reports)
                .values(
                    run_id=run_id,
                    lifecycle_state=state,
                    payload=dict(payload),
                    content_sha256=digest,
                    source_path=str(source.resolve()),
                )
                .on_conflict_do_nothing(index_elements=[run_reports.c.run_id])
                .returning(run_reports.c.run_id)
            )
            inserted = connection.execute(statement).scalar_one_or_none() is not None
            if not inserted:
                existing = connection.execute(
                    select(run_reports.c.content_sha256).where(
                        run_reports.c.run_id == run_id
                    )
                ).scalar_one()
                if existing != digest:
                    raise PersistenceConflictError(
                        f"run_id {run_id!r} already has a different report"
                    )
        return ReportImportResult(run_id, digest, inserted)
    except PersistenceError:
        raise
    except Exception as exc:
        raise _safe_failure("run report import", exc, settings) from None


def import_run_artifacts(
    engine: Engine,
    journal_path: str | Path,
    report_path: str | Path | None = None,
    *,
    settings: DatabaseSettings | None = None,
) -> tuple[JournalImportResult, ReportImportResult | None]:
    journal_result = import_journal(engine, journal_path, settings=settings)
    report_result = None
    if report_path is not None and Path(report_path).exists():
        report_result = store_run_report(engine, report_path, settings=settings)
    return journal_result, report_result


def store_preopen_report(
    engine: Engine,
    json_path: str | Path,
    markdown_path: str | Path,
    *,
    reference_snapshot_id: int,
    settings: DatabaseSettings | None = None,
) -> ReportImportResult:
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert

    from ak_trading.persistence.schema import preopen_reports, reference_snapshots

    json_source = Path(json_path)
    markdown_source = Path(markdown_path)
    try:
        json_bytes = json_source.read_bytes()
        markdown_bytes = markdown_source.read_bytes()
        payload = _decode_json_bytes(json_bytes, json_source)
        markdown = markdown_bytes.decode("utf-8")
        report_id = _required_text(
            payload.get("report_id"), "preopen.report_id", maximum=64
        )
        _verify_preopen_report_id(payload, report_id)
        trading_day = date.fromisoformat(
            _required_text(payload.get("trading_day"), "preopen.trading_day")
        )
        status = _required_text(payload.get("status"), "preopen.status", maximum=32)
        digest = _sha256(json_bytes + b"\0" + markdown_bytes)
        with engine.begin() as connection:
            reference_content = connection.execute(
                select(reference_snapshots.c.content).where(
                    reference_snapshots.c.id == reference_snapshot_id
                )
            ).scalar_one_or_none()
            if reference_content is None:
                raise PersistenceError(
                    f"unknown reference snapshot {reference_snapshot_id}"
                )
            _validate_preopen_reference(payload, reference_content)
            statement = (
                insert(preopen_reports)
                .values(
                    report_id=report_id,
                    trading_day=trading_day,
                    status=status,
                    reference_snapshot_id=reference_snapshot_id,
                    payload=dict(payload),
                    markdown=markdown,
                    content_sha256=digest,
                    json_path=str(json_source.resolve()),
                    markdown_path=str(markdown_source.resolve()),
                )
                .on_conflict_do_nothing(index_elements=[preopen_reports.c.report_id])
                .returning(preopen_reports.c.report_id)
            )
            inserted = connection.execute(statement).scalar_one_or_none() is not None
            if not inserted:
                existing = connection.execute(
                    select(
                        preopen_reports.c.content_sha256,
                        preopen_reports.c.reference_snapshot_id,
                    ).where(preopen_reports.c.report_id == report_id)
                ).one()
                if (
                    existing.content_sha256 != digest
                    or existing.reference_snapshot_id != reference_snapshot_id
                ):
                    raise PersistenceConflictError(
                        f"report_id {report_id!r} already has different content"
                    )
        return ReportImportResult(report_id, digest, inserted)
    except (PersistenceError, UnicodeError, ValueError):
        raise
    except Exception as exc:
        raise _safe_failure("pre-open report import", exc, settings) from None
