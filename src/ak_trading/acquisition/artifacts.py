"""C2 artifacts: RAW_SDK_SNAPSHOT persistence and per-product records (H06/H13).

File discipline:

- Atomic publish only: bytes go to an exclusively created temporary file in
  the destination directory (same volume), then flush + fsync + ``os.link``.
  A published file is immutable: the link operation refuses an existing
  destination atomically (no overwrites of prior evidence, H12).
- Published raw snapshots are re-read and re-hashed before the run may mark
  them RAW_VERIFIED (H06). Raw files exist even for zero-row / all-padding /
  refused responses: absence of data is recorded as structured evidence.
- ``RAW_SDK_SNAPSHOT`` means the SDK-decoded frame snapshot serialized here;
  it is explicitly NOT a WebSocket wire capture (raw_kind_is_wire_capture =
  false in the policy).
- Path safety: writes are constrained to the run's output root; absolute
  paths, ``..`` traversal, and junction/symlink escapes are rejected.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .contracts import (
    BAR_VALUE_FIELDS,
    CANONICAL_SERIALIZER_VERSION,
    ContractError,
    RawSnapshotMeta,
    sha256_bytes,
)

RAW_KIND = "RAW_SDK_SNAPSHOT"
SNAPSHOT_SCHEMA = "acq_w2_raw_snapshot_v1"

PRODUCT_ARTIFACT_FILES = (
    "identity.json",
    "requests.jsonl",
    "raw-manifest.json",
    "rows.jsonl",
    "reconciliation.json",
    "coverage.json",
    "gaps.json",
    "acceptance.json",
)


class ArtifactError(RuntimeError):
    """Raised for unsafe or non-atomic artifact operations."""


class ArtifactCleanupError(ArtifactError):
    """Publication committed; only the owned temporary link needs cleanup.

    ``path`` and ``sha256`` identify the complete, already published evidence.
    Callers must not treat this as an uncommitted write or retry publication.
    """

    committed = True

    def __init__(self, path: Path, sha256: str, temp_path: Path) -> None:
        self.path = path
        self.sha256 = sha256
        self.temp_path = temp_path
        super().__init__(
            f"evidence already committed at {path}; temporary cleanup failed: "
            f"{temp_path}; do not retry publication"
        )


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------

def _has_reparse_point(path: Path) -> bool:
    if hasattr(os.path, "isjunction") and os.path.isjunction(path):
        return True
    if path.is_symlink():
        return True
    return False


def resolve_within_root(root: Path, relative: str | Path) -> Path:
    """Resolve ``root/relative`` refusing escapes, absolute hijacks, junctions."""
    root = Path(root).resolve(strict=False)
    rel = Path(relative)
    if rel.is_absolute():
        raise ArtifactError(f"absolute paths are not accepted: {relative}")
    parts = [p for p in rel.parts if p not in (".",)]
    if ".." in parts:
        raise ArtifactError("path traversal ('..') is not accepted")
    current = root
    for component in parts:
        candidate = current / component
        if _has_reparse_point(candidate):
            raise ArtifactError(
                f"junction/symlink escape refused: {candidate}"
            )
        current = candidate
    resolved = current.resolve(strict=False)
    root_resolved = root.resolve(strict=False)
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        raise ArtifactError(f"resolved path escapes output root: {resolved}") from None
    return resolved


def ensure_no_reparse_chain(path: Path) -> None:
    """Refuse any reparse point in the existing prefix of ``path``."""
    accumulated = Path(path.anchor) if path.anchor else Path(".")
    parts = path.parts[1:] if path.anchor else path.parts
    for component in parts:
        accumulated = accumulated / component
        if _has_reparse_point(accumulated):
            raise ArtifactError(f"reparse point in path chain: {accumulated}")


# ---------------------------------------------------------------------------
# Atomic publish
# ---------------------------------------------------------------------------

def atomic_publish_bytes(directory: Path, filename: str, payload: bytes) -> tuple[Path, str]:
    """Publish complete bytes via an atomic, same-volume, no-clobber link.

    Before commit, failures leave no destination and clean only our own temp.
    Existing destinations are never changed. Unsupported link operations fail
    closed. After commit, cleanup failure raises ``ArtifactCleanupError`` with
    the committed path/hash; the destination remains valid and must not be retried.
    """
    directory = Path(directory)
    ensure_no_reparse_chain(directory)
    destination = resolve_within_root(directory, filename)
    destination.parent.mkdir(parents=True, exist_ok=True)
    ensure_no_reparse_chain(destination.parent)
    digest = sha256_bytes(payload)
    temp_path = destination.parent / f".tmp-{uuid.uuid4().hex}-{destination.name}"
    # Open outside the cleanup block: a collision does not give us ownership.
    handle = open(temp_path, "xb")
    try:
        with handle:
            if handle.write(payload) != len(payload):
                raise OSError("incomplete artifact temporary write")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp_path, destination)
        except FileExistsError:
            raise ArtifactError(
                f"refusing to overwrite existing evidence file: {destination}"
            ) from None
        except (OSError, NotImplementedError, AttributeError) as exc:
            raise ArtifactError(
                f"atomic no-clobber publish failed: {destination}"
            ) from exc
    except BaseException as exc:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # Keep the original failure (including an interruption) observable.
            exc.add_note(f"temporary cleanup failed before commit: {temp_path}")
        raise
    # A successful link is the commit point. Never remove the destination,
    # even when unlinking the extra, owned temporary name subsequently fails.
    try:
        temp_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ArtifactCleanupError(destination, digest, temp_path) from exc
    return destination, digest


def verify_published_file(path: Path, expected_sha256: str) -> int:
    """Re-read + re-hash; returns byte count (H06 RAW_VERIFIED precondition)."""
    payload = path.read_bytes()
    digest = sha256_bytes(payload)
    if digest != expected_sha256:
        raise ArtifactError(f"published file hash mismatch: {path}")
    return len(payload)


# ---------------------------------------------------------------------------
# RAW_SDK_SNAPSHOT serialization
# ---------------------------------------------------------------------------

def frame_columns_of(frame: Any) -> dict[str, list[Any]]:
    columns: dict[str, list[Any]] = {}
    for name in ("datetime", *BAR_VALUE_FIELDS):
        if name not in getattr(frame, "columns", ()):
            raise ArtifactError(f"frame missing required column: {name}")
        columns[name] = [float(v) if hasattr(v, "item") else v for v in frame[name].tolist()]
    return columns


def _jsonable_value(value: Any) -> tuple[Any, bool]:
    """Return (json_value, was_nan). NaN floats become null."""
    if isinstance(value, float):
        if value != value:
            return None, True
    return value, False


def serialize_snapshot(
    meta: RawSnapshotMeta,
    frame: Any,
    *,
    synthetic: bool,
    counts: Mapping[str, int],
) -> bytes:
    """Canonical bytes of the RAW_SDK_SNAPSHOT record.

    Floats serialize via Python's shortest round-trip repr inside JSON numbers;
    NaN becomes JSON null with an explicit nan flag so padding rows survive
    losslessly. ``raw_kind`` states plainly that this is the SDK-decoded frame,
    not a wire capture.
    """
    columns = frame_columns_of(frame)
    serialized_columns: dict[str, Any] = {}
    nan_counts: dict[str, int] = {}
    for name, values in columns.items():
        out = []
        nan_seen = 0
        for value in values:
            json_value, was_nan = _jsonable_value(value)
            if was_nan:
                nan_seen += 1
            out.append(json_value)
        serialized_columns[name] = out
        if nan_seen:
            nan_counts[name] = nan_seen
    record = {
        "attempt_id": meta.attempt_id,
        "counts": {key: int(value) for key, value in counts.items()},
        "duration_seconds": meta.duration_seconds,
        "first_valid_nano": (
            str(meta.first_valid_nano) if meta.first_valid_nano is not None else None
        ),
        "last_valid_nano": (
            str(meta.last_valid_nano) if meta.last_valid_nano is not None else None
        ),
        "nan_value_columns": nan_counts,
        "observed_at_nano": str(meta.observed_at_nano),
        "raw_kind": RAW_KIND,
        "raw_kind_is_wire_capture": False,
        "requested_length": meta.requested_length,
        "returned_length": meta.returned_length,
        "sdk_version": meta.sdk_version,
        "serializer_version": CANONICAL_SERIALIZER_VERSION,
        "source_symbol": meta.source_symbol,
        "synthetic": synthetic,
        "task_id": meta.task_id,
    }
    if synthetic:
        record["synthetic_label"] = "SYNTHETIC"
    body = json.dumps(
        {"columns": serialized_columns, "meta": record},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return body


def publish_raw_snapshot(
    output_root: Path,
    product_key: str,
    meta: RawSnapshotMeta,
    frame: Any,
    *,
    synthetic: bool,
    counts: Mapping[str, int],
) -> tuple[RawSnapshotMeta, str]:
    """Write the raw snapshot file and verify it; returns updated meta + status."""
    import re

    def _safe(component: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]", "_", component)

    safe_product = _safe(product_key.replace(".", "_"))
    raw_dir = resolve_within_root(output_root, f"products/{safe_product}/raw")
    filename = (
        f"{_safe(meta.source_symbol.replace('.', '_'))}-{meta.duration_seconds}s-"
        f"{_safe(meta.attempt_id)}.raw.json"
    )
    payload = serialize_snapshot(meta, frame, synthetic=synthetic, counts=counts)
    if len(payload) > 268_435_456:  # raw_file_bytes_max guard
        raise ArtifactError("raw snapshot exceeds single-file byte budget")
    path, digest = atomic_publish_bytes(raw_dir, filename, payload)
    size = verify_published_file(path, digest)
    import dataclasses

    updated = dataclasses.replace(
        meta,
        raw_path=str(path),
        raw_sha256=digest,
        raw_bytes=size,
    )
    return updated, "RAW_VERIFIED"


def append_jsonl(
    output_root: Path, relative_path: str, entries: Sequence[Mapping[str, Any]]
) -> None:
    """Append-only JSONL publish; each call writes a new uniquely named part."""
    if not entries:
        return
    target_dir = resolve_within_root(output_root, str(Path(relative_path).parent))
    stem = Path(relative_path).name
    part = f".part-{uuid.uuid4().hex[:8]}-{stem}"
    payload = b"".join(
        json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        + b"\n"
        for entry in entries
    )
    atomic_publish_bytes(target_dir, part, payload)


def write_product_json(
    output_root: Path, product_key: str, filename: str, payload: Mapping[str, Any]
) -> Path:
    safe_product = product_key.replace(".", "_")
    directory = resolve_within_root(output_root, f"products/{safe_product}")
    body = json.dumps(
        payload, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    path, _digest = atomic_publish_bytes(directory, filename, body)
    return path


# ---------------------------------------------------------------------------
# H13 per-product artifact assembly
# ---------------------------------------------------------------------------

def raw_manifest_entry(meta: RawSnapshotMeta, status: str) -> dict[str, Any]:
    return {
        "attempt_id": meta.attempt_id,
        "bytes": meta.raw_bytes,
        "duration_seconds": meta.duration_seconds,
        "first_valid_nano": (
            str(meta.first_valid_nano) if meta.first_valid_nano is not None else None
        ),
        "invalid_rows": meta.invalid_rows,
        "last_valid_nano": (
            str(meta.last_valid_nano) if meta.last_valid_nano is not None else None
        ),
        "padding_rows": meta.padding_rows,
        "raw_path": meta.raw_path,
        "raw_sha256": meta.raw_sha256,
        "returned_length": meta.returned_length,
        "rows": meta.frame_rows,
        "source_symbol": meta.source_symbol,
        "status": status,
        "task_id": meta.task_id,
        "valid_rows": meta.valid_rows,
    }


__all__ = [
    "PRODUCT_ARTIFACT_FILES",
    "RAW_KIND",
    "SNAPSHOT_SCHEMA",
    "ArtifactCleanupError",
    "ArtifactError",
    "append_jsonl",
    "atomic_publish_bytes",
    "ensure_no_reparse_chain",
    "frame_columns_of",
    "publish_raw_snapshot",
    "raw_manifest_entry",
    "resolve_within_root",
    "serialize_snapshot",
    "verify_published_file",
    "write_product_json",
]
