"""Private compact fold-record storage for the T7 memory unit.

This module is the ``FUTURE_OUTPUT`` created by dispatch
``zcode-dispatch/t7-memory-owner-001`` on 2026-10-26 under
MEMORY-DISPOSITION.md §2/§3 (PERFORMANCE-DISPOSITION.md §4): it removes the
simultaneous residency of every full ``FeatureSnapshot``/``ForecastRequest``
object graph during one fold while preserving every logical record, its exact
value-equality semantics and its exact canonical encoding representation.

Design boundaries fixed by the author disposition:

- Internal only. No public record type, schema, budget or output projection
  changes; the public dataclasses are materialized on demand through their
  original constructors with the complete real ``source_bar_ids`` tuple.
- ``append_*`` only stores already-constructed records verbatim (compact
  form); join/business validation stays in the shared join implementation in
  ``probability.models``/``probability.calibration`` so public tuple entries
  and internal view entries behave identically — no second, looser path.
- Source-bar id sequences are stored as immutable chained nodes whose parent
  is verified against the actually appended content (full-content check, no
  hash-only equality). A confirmed immutable-sequence equivalence may be
  reused; digests never decide value equality. Interior (non-append)
  insertions keep the actual sequence.
- Value equality never collapses encoding differences: the original
  ``Decimal`` objects (``1.0`` vs ``1.00``), float values, nested per-request
  feature representations and ``FactorValue`` tuples are stored as-is.
- The materialization cache is bounded by record count AND an approximate
  deep byte size; eviction releases objects only, never logical records.
  Optional private page files (``page_dir``) are sealed with per-page
  checksums and are verified on reopen; corruption raises, it never yields
  default values.
"""
from __future__ import annotations

import hashlib
import json
import sys
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Generic, Iterator, Sequence, TypeVar

from ak_trading.v2 import CONTRACT_VERSION
from ak_trading.v2.contracts import (
    FactorValue,
    FeatureSnapshot,
    ForecastRequest,
)
from ak_trading.v2.schemas import (
    SchemaError,
    decode_record,
    encode_record,
    parse_json_bytes,
)

__all__ = [
    "FoldPageError",
    "StoreClosedError",
    "StoreSealedError",
    "SnapshotRef",
    "RequestRef",
    "SnapshotMeta",
    "RequestMeta",
    "RecordView",
    "FoldRecords",
    "record_file_size",
    "stream_record_file",
    "open_sealed",
]

_PAGE_INTERNAL_VERSION = 1
_PAGE_ROLLOVER_BYTES = 32 * 1024 * 1024
_EQUIV_MEMO_LIMIT = 65536
_T = TypeVar("_T")


class FoldPageError(ValueError):
    """Sealed page manifest/page integrity failure (never a default value)."""


class StoreClosedError(RuntimeError):
    """Operation on a closed store; closed views never fake empty results."""


class StoreSealedError(RuntimeError):
    """Append after seal()."""


def _check_index(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name}_must_be_int")
    if value < 0:
        raise ValueError(f"{name}_must_be_non_negative")
    return value


# ---------------------------------------------------------------------------
# compact source-sequence nodes (immutable, content-verified chaining)
# ---------------------------------------------------------------------------

class _SeqNode:
    """One immutable node: content == parent.content + delta (exact)."""

    __slots__ = ("parent", "delta", "length")

    def __init__(self, parent: "_SeqNode | None", delta: tuple[str, ...]) -> None:
        self.parent = parent
        self.delta = delta
        self.length = (parent.length if parent is not None else 0) + len(delta)

    def materialize(self) -> tuple[str, ...]:
        chunks: list[tuple[str, ...]] = []
        node: "_SeqNode | None" = self
        while node is not None:
            chunks.append(node.delta)
            node = node.parent
        chunks.reverse()
        if len(chunks) == 1:
            return chunks[0]
        return tuple(element for chunk in chunks for element in chunk)

    def iter_elements(self) -> Iterator[str]:
        node: "_SeqNode | None" = self
        chain: list[_SeqNode] = []
        while node is not None:
            chain.append(node)
            node = node.parent
        for item in reversed(chain):
            yield from item.delta


def _seq_nodes_equal(left: _SeqNode, right: _SeqNode,
                     memo: dict[tuple[int, int], bool]) -> bool:
    """Content equality of two immutable nodes; == on the id sequences.

    Identity and previously confirmed (immutable) equivalences are reused;
    otherwise the actual contents are compared element by element. Digests
    are never consulted for equality decisions.
    """
    if left is right:
        return True
    if left.length != right.length:
        return False
    key = (id(left), id(right)) if id(left) <= id(right) else (id(right), id(left))
    confirmed = memo.get(key)
    if confirmed is not None:
        return confirmed
    equal = all(a == b for a, b in zip(left.iter_elements(), right.iter_elements()))
    if equal and len(memo) < _EQUIV_MEMO_LIMIT:
        memo[key] = True
    return equal


# ---------------------------------------------------------------------------
# compact records (field-exact copies minus the resident big objects)
# ---------------------------------------------------------------------------

class _CompactSnapshot:
    __slots__ = (
        "snapshot_id", "contract_id", "raw_symbol", "product", "trading_day",
        "session_id", "session_kind", "decision_nano", "available_at_nano",
        "duration_seconds", "source", "reference_price", "atr_points",
        "values", "daily_background_id", "quality",
    )

    def __init__(self, snapshot: FeatureSnapshot, source: _SeqNode) -> None:
        self.snapshot_id = snapshot.snapshot_id
        self.contract_id = snapshot.contract_id
        self.raw_symbol = snapshot.raw_symbol
        self.product = snapshot.product
        self.trading_day = snapshot.trading_day
        self.session_id = snapshot.session_id
        self.session_kind = snapshot.session_kind
        self.decision_nano = snapshot.decision_nano
        self.available_at_nano = snapshot.available_at_nano
        self.duration_seconds = snapshot.duration_seconds
        self.source = source
        self.reference_price = snapshot.reference_price
        self.atr_points = snapshot.atr_points
        self.values = snapshot.values
        self.daily_background_id = snapshot.daily_background_id
        self.quality = snapshot.quality

    def materialize(self) -> FeatureSnapshot:
        return FeatureSnapshot(
            snapshot_id=self.snapshot_id,
            contract_id=self.contract_id,
            raw_symbol=self.raw_symbol,
            product=self.product,
            trading_day=self.trading_day,
            session_id=self.session_id,
            session_kind=self.session_kind,
            decision_nano=self.decision_nano,
            available_at_nano=self.available_at_nano,
            duration_seconds=self.duration_seconds,
            source_bar_ids=self.source.materialize(),
            reference_price=self.reference_price,
            atr_points=self.atr_points,
            values=self.values,
            daily_background_id=self.daily_background_id,
            quality=self.quality,
        )


class _CompactRequest:
    __slots__ = (
        "request_id", "features", "horizon", "window_start_nano",
        "window_end_nano", "barrier_key", "upper_distance", "lower_distance",
        "frozen_range",
    )

    def __init__(self, request: ForecastRequest, features: _CompactSnapshot) -> None:
        self.request_id = request.request_id
        self.features = features
        self.horizon = request.horizon
        self.window_start_nano = request.window_start_nano
        self.window_end_nano = request.window_end_nano
        self.barrier_key = request.barrier_key
        self.upper_distance = request.upper_distance
        self.lower_distance = request.lower_distance
        self.frozen_range = request.frozen_range

    def materialize(self, features: FeatureSnapshot) -> ForecastRequest:
        return ForecastRequest(
            request_id=self.request_id,
            features=features,
            horizon=self.horizon,
            window_start_nano=self.window_start_nano,
            window_end_nano=self.window_end_nano,
            barrier_key=self.barrier_key,
            upper_distance=self.upper_distance,
            lower_distance=self.lower_distance,
            frozen_range=self.frozen_range,
        )


def _snapshot_value_equal(a: _CompactSnapshot, b: _CompactSnapshot,
                          memo: dict[tuple[int, int], bool]) -> bool:
    """Exactly the semantics of ``FeatureSnapshot.__eq__`` on the records."""
    if a is b:
        return True
    return (
        a.snapshot_id == b.snapshot_id
        and a.contract_id == b.contract_id
        and a.raw_symbol == b.raw_symbol
        and a.product == b.product
        and a.trading_day == b.trading_day
        and a.session_id == b.session_id
        and a.session_kind == b.session_kind
        and a.decision_nano == b.decision_nano
        and a.available_at_nano == b.available_at_nano
        and a.duration_seconds == b.duration_seconds
        and _seq_nodes_equal(a.source, b.source, memo)
        and a.reference_price == b.reference_price
        and a.atr_points == b.atr_points
        and a.values == b.values
        and a.daily_background_id == b.daily_background_id
        and a.quality == b.quality
    )


def _deep_size_estimate(root: object) -> int:
    """Conservative approximate deep size; used only for cache byte bounds."""
    seen: set[int] = set()
    stack: list[object] = [root]
    total = 0
    while stack:
        item = stack.pop()
        if item is None or isinstance(item, (bool, int, float, str, bytes,
                                             bytearray, Decimal, date)):
            try:
                total += sys.getsizeof(item)
            except TypeError:  # pragma: no cover - defensive
                total += 64
            continue
        identity = id(item)
        if identity in seen:
            continue
        seen.add(identity)
        try:
            total += sys.getsizeof(item)
        except TypeError:  # pragma: no cover - defensive
            total += 64
        if isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, (tuple, list, set, frozenset)):
            stack.extend(item)
        else:
            state = getattr(item, "__dict__", None)
            if state is not None:
                stack.append(state)
    return total


# ---------------------------------------------------------------------------
# refs and metadata
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SnapshotRef:
    """Internal handle bound to one store slot (no value semantics)."""

    store_id: int
    slot: int


@dataclass(frozen=True)
class RequestRef:
    """Internal handle bound to one store slot (no value semantics)."""

    store_id: int
    slot: int


@dataclass(frozen=True)
class SnapshotMeta:
    snapshot_id: str
    contract_id: str
    product: str
    trading_day: date
    session_id: str
    session_kind: str
    decision_nano: int
    available_at_nano: int
    duration_seconds: int
    reference_price: Decimal | None
    atr_points: Decimal | None
    handle: SnapshotRef


@dataclass(frozen=True)
class RequestMeta:
    request_id: str
    horizon: object
    window_start_nano: int
    window_end_nano: int
    barrier_key: str
    upper_distance: Decimal
    lower_distance: Decimal
    frozen_range: object
    handle: RequestRef
    features_ref: SnapshotRef
    features_snapshot_id: str


# ---------------------------------------------------------------------------
# views
# ---------------------------------------------------------------------------

class RecordView(Generic[_T]):
    """Stable ordered view over the store members bound at creation.

    ``len()``, repeated iteration and integer indexing reflect all actual
    logical records (duplicates included); ``__getitem__`` materializes the
    complete public dataclass through its original constructor.
    """

    __slots__ = ("_store", "_kind", "_indices")

    def __init__(self, store: "FoldRecords", kind: str,
                 indices: tuple[int, ...]) -> None:
        self._store = store
        self._kind = kind
        self._indices = indices

    @property
    def record_kind(self) -> str:
        return self._kind

    @property
    def store(self) -> "FoldRecords":
        return self._store

    def __len__(self) -> int:
        return len(self._indices)

    def __iter__(self) -> Iterator[_T]:
        for index in self._indices:
            yield self._store._materialize(self._kind, index)

    def __getitem__(self, index: int) -> _T:
        if isinstance(index, bool) or not isinstance(index, int):
            raise TypeError("index_must_be_int")
        if index < 0:
            raise IndexError("view_index_out_of_range")
        if index >= len(self._indices):
            raise IndexError("view_index_out_of_range")
        return self._store._materialize(self._kind, self._indices[index])

    def select(self, indices: Sequence[int]) -> "RecordView[_T]":
        """Ordered sub-view; duplicates and given order are preserved."""
        selected: list[int] = []
        for value in indices:
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError("index_must_be_int")
            if value < 0 or value >= len(self._indices):
                raise IndexError("view_index_out_of_range")
            selected.append(self._indices[value])
        return RecordView(self._store, self._kind, tuple(selected))

    def meta(self, index: int) -> SnapshotMeta | RequestMeta:
        if isinstance(index, bool) or not isinstance(index, int):
            raise TypeError("index_must_be_int")
        if index < 0 or index >= len(self._indices):
            raise IndexError("view_index_out_of_range")
        return self._store._meta(self._kind, self._indices[index])

    def snapshot_by_id(self, snapshot_id: object) -> FeatureSnapshot | None:
        """Materialize the view's record with this id (post uniqueness check).

        Duplicates inside this view are never hidden by a dict overwrite:
        they raise, mirroring the join's own duplicate detection.
        """
        if not isinstance(snapshot_id, str):
            raise TypeError("snapshot_id_must_be_str")
        if self._kind != "FeatureSnapshot":
            raise TypeError("snapshot_by_id_requires_snapshot_view")
        members = set(self._indices)
        matches = [slot for slot in self._store._snapshot_slots_by_id.get(snapshot_id, ())
                   if slot in members]
        if len(matches) > 1:
            raise ValueError("duplicate_snapshot_id")
        if not matches:
            return None
        return self._store._materialize(self._kind, matches[0])


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------

class _PageWriter:
    """Append-only private page file with footer checksum."""

    def __init__(self, page_dir: Path, kind: str, page_index: int) -> None:
        self.path = page_dir / f"{kind.lower()}-page-{page_index:06d}.page"
        self.count = 0
        self.body_bytes = 0
        self._hasher = hashlib.sha256()
        self._handle = self.path.open("wb")
        header = {
            "internal": "ak-fold-page", "version": _PAGE_INTERNAL_VERSION,
            "kind": kind, "page_index": page_index,
        }
        self._write_line(canonical_line(header))

    def _write_line(self, payload: bytes) -> None:
        self._handle.write(payload)
        self._handle.write(b"\n")

    def append(self, record: object) -> None:
        line = canonical_line(encode_record(record))
        self._hasher.update(line)
        self._hasher.update(b"\n")
        self._write_line(line)
        self.count += 1
        self.body_bytes += len(line) + 1

    def seal(self) -> dict[str, object]:
        footer = {"footer_sha256": self._hasher.hexdigest(), "records": self.count}
        self._write_line(canonical_line(footer))
        self._handle.flush()
        self._handle.close()
        raw = self.path.read_bytes()
        return {
            "path": self.path.name, "bytes": len(raw),
            "records": self.count, "footer_sha256": footer["footer_sha256"],
            "file_sha256": hashlib.sha256(raw).hexdigest(),
        }

    def rollover_needed(self) -> bool:
        return self.body_bytes >= _PAGE_ROLLOVER_BYTES


def canonical_line(payload: object) -> bytes:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)
    return text.encode("utf-8")


class FoldRecords:
    """One fold's exclusive compact record store (internal use only).

    The dual caps bound only the materialization cache; page files, the id
    index and every other payload remain part of the whole process-tree
    budget (``page_bytes_written`` exposes the page share for accounting).
    """

    def __init__(self, *, cache_max_records: int, cache_max_bytes: int,
                 page_dir: Path | None = None) -> None:
        _check_index(cache_max_records, "cache_max_records")
        _check_index(cache_max_bytes, "cache_max_bytes")
        if page_dir is not None and not isinstance(page_dir, Path):
            raise TypeError("page_dir_must_be_Path_or_None")
        self._cache_max_records = cache_max_records
        self._cache_max_bytes = cache_max_bytes
        self._store_id = id(self)
        self._snapshots: list[_CompactSnapshot] = []
        self._requests: list[_CompactRequest] = []
        self._snapshot_slots_by_id: dict[str, list[int]] = {}
        self._request_slots_by_id: dict[str, list[int]] = {}
        # source-sequence spine (content-verified chaining)
        self._root_seq = _SeqNode(None, ())
        self._last_seq_tuple: tuple[str, ...] | None = None
        self._last_seq_node: _SeqNode = self._root_seq
        self._spine_by_length: dict[int, _SeqNode] = {0: self._root_seq}
        self._interned_ids: dict[str, str] = {}
        self._equiv_memo: dict[tuple[int, int], bool] = {}
        # materialization cache: kind+slot -> object, LRU, dual-capped
        self._cache: OrderedDict[tuple[str, int], object] = OrderedDict()
        self._cache_bytes = 0
        self._slot_size_hint: dict[tuple[str, int], int] = {}
        self._sealed = False
        self._closed = False
        self._read_only = False
        # optional pages
        self._page_dir = page_dir
        if page_dir is not None:
            if page_dir.exists():
                if any(page_dir.iterdir()):
                    raise ValueError("page_dir_must_be_empty")
            else:
                page_dir.mkdir(parents=True)
        self._page_writers: dict[str, _PageWriter] = {}
        self._page_counts: dict[str, int] = {}
        self.page_bytes_written = 0
        self._sealed_pages: dict[str, list[dict[str, object]]] = {}

    # -- lifecycle ---------------------------------------------------------

    def _ensure_open(self) -> None:
        if self._closed:
            raise StoreClosedError("store_closed")

    def seal(self) -> None:
        """Finish internal sealing (pages get footers + a manifest)."""
        self._ensure_open()
        if self._sealed:
            return
        for kind, writer in self._page_writers.items():
            self._sealed_pages[kind] = self._sealed_pages.get(kind, [])
            self._sealed_pages[kind].append(writer.seal())
            self.page_bytes_written = sum(
                page["bytes"] for pages in self._sealed_pages.values() for page in pages)
        self._page_writers = {}
        if self._page_dir is not None and self._sealed_pages:
            manifest = {
                "internal": "ak-fold-page-manifest", "version": _PAGE_INTERNAL_VERSION,
                "pages": self._sealed_pages,
            }
            target = self._page_dir / "pages-manifest.json"
            payload = canonical_line(manifest) + b"\n"
            temporary = self._page_dir / "pages-manifest.json.tmp"
            temporary.write_bytes(payload)
            temporary.replace(target)
        self._sealed = True

    def close(self) -> None:
        """Release handles; never marks the fold successful, keeps evidence."""
        if self._closed:
            return
        if not self._sealed:
            for kind, writer in self._page_writers.items():
                self._sealed_pages[kind] = self._sealed_pages.get(kind, [])
                self._sealed_pages[kind].append(writer.seal())
            self._page_writers = {}
            self.page_bytes_written = sum(
                page["bytes"] for pages in self._sealed_pages.values() for page in pages)
        self._cache.clear()
        self._cache_bytes = 0
        self._closed = True

    # -- append ------------------------------------------------------------

    def _intern_sequence(self, ids: tuple[str, ...]) -> _SeqNode:
        parent = self._root_seq
        cut = 0
        previous = self._last_seq_tuple
        if previous is not None:
            common = _common_prefix_length(previous, ids)
            if self._last_seq_node.length <= common:
                # fast path: the whole previous spine content is a verified
                # prefix of ids (full-content slice equality just proved it)
                cut = self._last_seq_node.length
                parent = self._last_seq_node
            else:
                # branch: deepest verified spine node at or below the shared
                # prefix; deeper spine entries no longer match this lineage
                candidates = [length for length in self._spine_by_length if length <= common]
                cut = max(candidates) if candidates else 0
                parent = self._spine_by_length[cut]
                for length in [item for item in self._spine_by_length if item > cut]:
                    del self._spine_by_length[length]
        delta = tuple(self._interned_ids.setdefault(element, element)
                      for element in ids[cut:])
        if not delta and parent is not self._root_seq:
            node = parent  # identical content: reuse the immutable node
        else:
            node = _SeqNode(parent, delta)
        self._spine_by_length[node.length] = node
        self._last_seq_tuple = ids
        self._last_seq_node = node
        return node

    def _page_append(self, kind: str, record: object) -> None:
        if self._page_dir is None:
            return
        writer = self._page_writers.get(kind)
        if writer is None or writer.rollover_needed():
            if writer is not None:
                self._sealed_pages[kind] = self._sealed_pages.get(kind, [])
                sealed = writer.seal()
                self._sealed_pages[kind].append(sealed)
            self._page_counts[kind] = self._page_counts.get(kind, 0) + 1
            writer = _PageWriter(self._page_dir, kind, self._page_counts[kind])
            self._page_writers[kind] = writer
        writer.append(record)

    def append_snapshot(self, snapshot: object) -> SnapshotRef:
        self._ensure_open()
        if self._sealed:
            raise StoreSealedError("store_sealed")
        if self._read_only:
            raise StoreSealedError("store_read_only")
        if not isinstance(snapshot, FeatureSnapshot):
            raise TypeError("snapshot_must_be_FeatureSnapshot")
        source = self._intern_sequence(snapshot.source_bar_ids)
        compact = _CompactSnapshot(snapshot, source)
        slot = len(self._snapshots)
        self._snapshots.append(compact)
        self._snapshot_slots_by_id.setdefault(compact.snapshot_id, []).append(slot)
        self._page_append("FeatureSnapshot", snapshot)
        return SnapshotRef(self._store_id, slot)

    def append_request(self, request: object) -> RequestRef:
        self._ensure_open()
        if self._sealed:
            raise StoreSealedError("store_sealed")
        if self._read_only:
            raise StoreSealedError("store_read_only")
        if not isinstance(request, ForecastRequest):
            raise TypeError("request_must_be_ForecastRequest")
        features = _CompactSnapshot(request.features,
                                    self._intern_sequence(request.features.source_bar_ids))
        compact = _CompactRequest(request, features)
        slot = len(self._requests)
        self._requests.append(compact)
        self._request_slots_by_id.setdefault(compact.request_id, []).append(slot)
        self._page_append("ForecastRequest", request)
        return RequestRef(self._store_id, slot)

    # -- views -------------------------------------------------------------

    def snapshot_view(self) -> RecordView[FeatureSnapshot]:
        """All appended snapshots sorted by the original stable key."""
        self._ensure_open()
        order = sorted(range(len(self._snapshots)),
                       key=lambda slot: self._snapshot_sort_key(slot))
        return RecordView(self, "FeatureSnapshot", tuple(order))

    def _snapshot_sort_key(self, slot: int) -> tuple[int, str, int]:
        compact = self._snapshots[slot]
        return (compact.decision_nano, compact.contract_id, compact.duration_seconds)

    def request_view(self) -> RecordView[ForecastRequest]:
        """All appended requests in actual construction order."""
        self._ensure_open()
        return RecordView(self, "ForecastRequest", tuple(range(len(self._requests))))

    # -- materialization and metadata ---------------------------------------

    def _materialize(self, kind: str, slot: int) -> object:
        self._ensure_open()
        key = (kind, slot)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached
        if kind == "FeatureSnapshot":
            record: object = self._snapshots[slot].materialize()
        else:
            compact = self._requests[slot]
            record = compact.materialize(compact.features.materialize())
        size = self._slot_size_hint.get(key)
        if size is None:
            size = _deep_size_estimate(record)
            self._slot_size_hint[key] = size
        self._cache[key] = record
        self._cache_bytes += size
        self._enforce_cache_bounds()
        return record

    def _enforce_cache_bounds(self) -> None:
        while self._cache and (len(self._cache) > self._cache_max_records
                               or self._cache_bytes > self._cache_max_bytes):
            evicted_key, _ = self._cache.popitem(last=False)
            self._cache_bytes -= self._slot_size_hint.get(evicted_key, 0)

    # -- internal compact access (shared join/reference/geometry cores) ----

    def _resolve_snapshot_ref(self, ref: object) -> _CompactSnapshot:
        self._ensure_open()
        if not isinstance(ref, SnapshotRef) or ref.store_id != self._store_id:
            raise TypeError("ref_not_bound_to_store")
        if ref.slot >= 0:
            return self._snapshots[ref.slot]
        return self._requests[-(ref.slot + 1)].features

    def _snapshot_handle_by_id(self, snapshot_id: str) -> SnapshotRef | None:
        """Internal: any-slot handle lookup; duplicates detected by the join."""
        self._ensure_open()
        slots = self._snapshot_slots_by_id.get(snapshot_id)
        if not slots:
            return None
        return SnapshotRef(self._store_id, slots[0])

    def _snapshot_fields(self, ref: SnapshotRef) -> _CompactSnapshot:
        return self._resolve_snapshot_ref(ref)

    def _request_handle_by_id(self, request_id: str) -> RequestRef | None:
        self._ensure_open()
        slots = self._request_slots_by_id.get(request_id)
        if not slots:
            return None
        return RequestRef(self._store_id, slots[0])

    def _request_compact(self, ref: RequestRef) -> _CompactRequest:
        self._ensure_open()
        if not isinstance(ref, RequestRef) or ref.store_id != self._store_id:
            raise TypeError("ref_not_bound_to_store")
        return self._requests[ref.slot]

    def same_snapshot(self, left: object, right: object) -> bool:
        """Value equality equivalent to the original ``FeatureSnapshot.__eq__``.

        Never id-only or digest-only: type and every field's value semantics
        are compared; a confirmed immutable source-sequence equivalence may
        be reused. Encoding differences (Decimal 1.0 vs 1.00) do not affect
        equality, exactly like the public dataclass.
        """
        self._ensure_open()
        if not isinstance(left, SnapshotRef) or left.store_id != self._store_id:
            raise TypeError("left_ref_not_bound_to_store")
        if not isinstance(right, SnapshotRef) or right.store_id != self._store_id:
            raise TypeError("right_ref_not_bound_to_store")
        return _snapshot_value_equal(self._resolve_snapshot_ref(left),
                                     self._resolve_snapshot_ref(right),
                                     self._equiv_memo)

    def _meta(self, kind: str, slot: int) -> SnapshotMeta | RequestMeta:
        self._ensure_open()
        if kind == "FeatureSnapshot":
            compact = self._snapshots[slot]
            return SnapshotMeta(
                snapshot_id=compact.snapshot_id,
                contract_id=compact.contract_id,
                product=compact.product,
                trading_day=compact.trading_day,
                session_id=compact.session_id,
                session_kind=compact.session_kind,
                decision_nano=compact.decision_nano,
                available_at_nano=compact.available_at_nano,
                duration_seconds=compact.duration_seconds,
                reference_price=compact.reference_price,
                atr_points=compact.atr_points,
                handle=SnapshotRef(self._store_id, slot),
            )
        compact = self._requests[slot]
        return RequestMeta(
            request_id=compact.request_id,
            horizon=compact.horizon,
            window_start_nano=compact.window_start_nano,
            window_end_nano=compact.window_end_nano,
            barrier_key=compact.barrier_key,
            upper_distance=compact.upper_distance,
            lower_distance=compact.lower_distance,
            frozen_range=compact.frozen_range,
            handle=RequestRef(self._store_id, slot),
            features_ref=SnapshotRef(self._store_id, -(slot + 1)),
            features_snapshot_id=compact.features.snapshot_id,
        )

    def _nested_compact(self, ref: SnapshotRef) -> _CompactSnapshot:
        """Compact snapshot for a nested features ref (negative slot encoding)."""
        return self._resolve_snapshot_ref(ref)

    # -- accounting ---------------------------------------------------------

    @property
    def logical_snapshot_count(self) -> int:
        return len(self._snapshots)

    @property
    def logical_request_count(self) -> int:
        return len(self._requests)

    @property
    def sealed(self) -> bool:
        return self._sealed

    @property
    def closed(self) -> bool:
        return self._closed

    def stats(self) -> dict[str, object]:
        return {
            "logical_snapshots": len(self._snapshots),
            "logical_requests": len(self._requests),
            "cache_entries": len(self._cache),
            "cache_bytes_estimate": self._cache_bytes,
            "cache_eviction_bound_records": self._cache_max_records,
            "cache_eviction_bound_bytes": self._cache_max_bytes,
            "spine_length": self._last_seq_node.length,
            "spine_nodes": len(self._spine_by_length),
            "interned_ids": len(self._interned_ids),
            "confirmed_sequence_equivalences": len(self._equiv_memo),
            "page_bytes_written": self.page_bytes_written,
            "sealed": self._sealed,
            "closed": self._closed,
        }


def _common_prefix_length(left: tuple[str, ...], right: tuple[str, ...]) -> int:
    """Largest k with left[:k] == right[:k] (C-speed slice equality)."""
    low, high = 0, min(len(left), len(right))
    while low < high:
        middle = (low + high + 1) // 2
        if left[:middle] == right[:middle]:
            low = middle
        else:
            high = middle - 1
    return low


# ---------------------------------------------------------------------------
# exact canonical file size and streaming output
# ---------------------------------------------------------------------------

def _record_line_bytes(record: object) -> bytes:
    try:
        text = json.dumps(encode_record(record), sort_keys=True,
                          separators=(",", ":"), ensure_ascii=False,
                          allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise SchemaError("canonical_json_encode_failed") from exc
    return text.encode("utf-8")


def _envelope(record_kind: object, schema_version: object) -> tuple[bytes, bytes]:
    try:
        prefix = ("{\"contract_version\":" + json.dumps(CONTRACT_VERSION, ensure_ascii=False,
                                                        allow_nan=False)
                  + ",\"record_type\":" + json.dumps(record_kind, ensure_ascii=False,
                                                     allow_nan=False)
                  + ",\"records\":[")
        suffix = ("],\"schema_version\":" + json.dumps(schema_version, ensure_ascii=False,
                                                       allow_nan=False) + "}\n")
    except (TypeError, ValueError) as exc:
        raise SchemaError("canonical_json_encode_failed") from exc
    return prefix.encode("utf-8"), suffix.encode("utf-8")


def _validate_kind(record_kind: object) -> None:
    from ak_trading.v2.schemas import _ALL_RECORD_TYPES
    if not isinstance(record_kind, str) or record_kind not in _ALL_RECORD_TYPES:
        raise SchemaError(f"unknown_record_kind:{record_kind}")


def record_file_size(record_kind: str, schema_version: str,
                     records: Sequence[object]) -> int:
    """Exact ``len(encode_record_file(...))`` without building the file.

    Byte-for-byte consistent with the real canonical encoding (sorted keys,
    compact separators, UTF-8, one trailing LF, ``allow_nan=False``); raises
    ``SchemaError`` under exactly the same conditions. Never allocates the
    whole file and never fits anything.
    """
    _validate_kind(record_kind)
    prefix, suffix = _envelope(record_kind, schema_version)
    total = len(prefix) + len(suffix)
    count = 0
    for record in records:
        if count:
            total += 1  # separating comma
        total += len(_record_line_bytes(record))
        count += 1
    return total


def stream_record_file(record_kind: str, schema_version: str,
                       records: Sequence[object], out) -> tuple[int, str]:
    """Write the identical canonical file bytes to caller-owned ``out``.

    Returns ``(byte_count, sha256_hex)`` computed incrementally while
    writing; the caller closes and seals the file before manifest use.
    """
    _validate_kind(record_kind)
    if not hasattr(out, "write"):
        raise TypeError("out_must_be_binary_stream")
    prefix, suffix = _envelope(record_kind, schema_version)
    hasher = hashlib.sha256()
    byte_count = 0

    def emit(chunk: bytes) -> None:
        nonlocal byte_count
        out.write(chunk)
        hasher.update(chunk)
        byte_count += len(chunk)

    emit(prefix)
    first = True
    for record in records:
        if not first:
            emit(b",")
        emit(_record_line_bytes(record))
        first = False
    emit(suffix)
    return byte_count, hasher.hexdigest()


# ---------------------------------------------------------------------------
# sealed-page reopening
# ---------------------------------------------------------------------------

def open_sealed(page_dir: Path) -> FoldRecords:
    """Reopen a sealed page directory as a read-only store (with checks)."""
    if not isinstance(page_dir, Path):
        raise TypeError("page_dir_must_be_Path")
    manifest_path = page_dir / "pages-manifest.json"
    if not manifest_path.is_file():
        raise FoldPageError("page_manifest_missing")
    manifest_bytes = manifest_path.read_bytes()
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise FoldPageError("page_manifest_corrupt") from exc
    if not isinstance(manifest, dict) or manifest.get("internal") != "ak-fold-page-manifest"             or manifest.get("version") != _PAGE_INTERNAL_VERSION             or not isinstance(manifest.get("pages"), dict):
        raise FoldPageError("page_manifest_invalid")
    store = FoldRecords(cache_max_records=0, cache_max_bytes=0, page_dir=None)
    store._read_only = True
    kind_types = {"FeatureSnapshot": FeatureSnapshot, "ForecastRequest": ForecastRequest}
    for kind in ("FeatureSnapshot", "ForecastRequest"):
        pages = manifest["pages"].get(kind, [])
        for page in sorted(pages, key=lambda item: item["path"]):
            path = page_dir / str(page["path"])
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != page.get("file_sha256")                     or len(raw) != page.get("bytes"):
                raise FoldPageError(f"page_corrupt:{page['path']}")
            newline_byte = b"\n"
            lines = raw.split(newline_byte)
            if lines[-1] != b"" or len(lines) < 3:
                raise FoldPageError(f"page_corrupt:{page['path']}")
            lines = lines[:-1]
            try:
                header = json.loads(lines[0].decode("utf-8"))
                footer = json.loads(lines[-1].decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise FoldPageError(f"page_corrupt:{page['path']}") from exc
            if header.get("internal") != "ak-fold-page" or header.get("kind") != kind                     or footer.get("records") != page.get("records"):
                raise FoldPageError(f"page_corrupt:{page['path']}")
            body = newline_byte.join(lines[1:-1]) + newline_byte
            if hashlib.sha256(body).hexdigest() != footer.get("footer_sha256"):
                raise FoldPageError(f"page_corrupt:{page['path']}")
            record_type = kind_types[kind]
            for line in lines[1:-1]:
                payload = parse_json_bytes(line)
                record = decode_record(record_type, payload)
                if kind == "FeatureSnapshot":
                    store._snapshots.append(_CompactSnapshot(
                        record, store._intern_sequence(record.source_bar_ids)))
                    slot = len(store._snapshots) - 1
                    store._snapshot_slots_by_id.setdefault(record.snapshot_id,
                                                           []).append(slot)
                else:
                    features = _CompactSnapshot(
                        record.features,
                        store._intern_sequence(record.features.source_bar_ids))
                    store._requests.append(_CompactRequest(record, features))
                    slot = len(store._requests) - 1
                    store._request_slots_by_id.setdefault(record.request_id,
                                                          []).append(slot)
    store._sealed = True
    store._sealed_pages = {kind: list(pages) for kind, pages in manifest["pages"].items()}
    return store
