"""Frozen input reading, strict package validation and PIT visibility.

A frozen package is only usable through its manifest: every listed file is
hash-verified before decoding, records must decode against the registered
schemas, and inputs must not be mixed across packages (bars/rules reference
declared instruments). Larger research sets are merged read-only from
already-accepted shards by ``publish_input_collection`` into a brand-new
collection directory; the database is never re-queried. Visibility is
point-in-time: a bar is only visible when it is complete, closed and its
availability time has passed the decision time; intraday snapshots never
see the current trading day's final daily bar (devplan-v2.1.0 M1 §5, §7).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

from ak_trading.v2 import CONTRACT_VERSION, INPUT_SCHEMA_VERSION
from ak_trading.v2.artifacts import ArtifactsError, sha256_hex
from ak_trading.v2.contracts import (
    FrozenInput,
    Instrument,
    MacroItem,
    MarketBar,
    SessionWindow,
    TradingRule,
)
from ak_trading.v2.schemas import (
    AVAILABILITY_SCHEMA_ID,
    BARS_SCHEMA_ID,
    INSTRUMENTS_SCHEMA_ID,
    MACRO_ITEMS_SCHEMA_ID,
    RULES_SCHEMA_ID,
    SESSIONS_SCHEMA_ID,
    SchemaError,
    canonical_json_bytes,
    decode_csv_bytes,
    decode_record_file,
    parse_json_bytes,
)

COLLECTION_SCHEMA_ID = "ak_probability_input_collection_v2.1.0"
_REQUIRED_FILES = frozenset({
    "bars.csv", "instruments.json", "sessions.json", "macro_items.json", "rules.json",
    "availability.json",
})
_MANIFEST_KEYS = frozenset({
    "schema_version", "contract_version", "dataset_kind", "created_at_nano", "request",
    "read_only_transaction", "files", "source_hashes", "limitations",
})
_COLLECTION_KEYS = frozenset({"schema_version", "contract_version", "dataset_kind", "shards", "files", "limitations"})
_AVAILABILITY_KEYS = frozenset({
    "schema_version", "input_manifest_sha256", "contract_status", "minute_history_status",
    "realtime_status", "reasons",
})


def _safe_package_path(root: Path, name: object) -> Path:
    if not isinstance(name, str) or not name:
        raise ArtifactsError("manifest_file_name_must_be_non_empty_str")
    if name.startswith(("/", "\\")) or ":" in name or ".." in Path(name).parts:
        raise ArtifactsError(f"unsafe_manifest_file_name:{name}")
    candidate = (root / name).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise ArtifactsError(f"manifest_file_escapes_package:{name}")
    target = root / name
    if target.is_symlink() or not target.is_file():
        raise ArtifactsError(f"package_file_missing_or_symlink:{name}")
    return target


def _read_single_package(path: Path) -> tuple[FrozenInput, str]:
    """Verify and load one input package; returns (input, dataset_kind)."""
    if not path.is_dir() or path.is_symlink():
        raise ArtifactsError("frozen_input_path_must_be_directory")
    manifest_path = path / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ArtifactsError("manifest_missing")
    manifest_bytes = manifest_path.read_bytes()
    try:
        manifest = parse_json_bytes(manifest_bytes)
    except SchemaError as exc:
        raise ArtifactsError(f"manifest_invalid_json:{exc}") from exc
    if not isinstance(manifest, dict) or set(manifest.keys()) != _MANIFEST_KEYS:
        raise ArtifactsError("manifest_root_fields_mismatch")
    if manifest["schema_version"] != INPUT_SCHEMA_VERSION:
        raise ArtifactsError("manifest_schema_version_mismatch")
    if manifest["contract_version"] != CONTRACT_VERSION:
        raise ArtifactsError("manifest_contract_version_mismatch")
    _validate_input_manifest_semantics(manifest)
    files = manifest["files"]
    if not isinstance(files, dict) or "manifest.json" in files:
        raise ArtifactsError("manifest_must_not_hash_itself")
    if not _REQUIRED_FILES <= set(files.keys()):
        raise ArtifactsError("manifest_missing_required_files")
    payloads: dict[str, bytes] = {}
    for name in sorted(files.keys()):
        target = _safe_package_path(path, name)
        payload = target.read_bytes()
        if sha256_hex(payload) != files[name]:
            raise ArtifactsError(f"file_hash_mismatch:{name}")
        payloads[name] = payload

    try:
        instruments = decode_record_file(payloads["instruments.json"], Instrument, INSTRUMENTS_SCHEMA_ID)
        sessions = decode_record_file(payloads["sessions.json"], SessionWindow, SESSIONS_SCHEMA_ID)
        macro_items = decode_record_file(payloads["macro_items.json"], MacroItem, MACRO_ITEMS_SCHEMA_ID)
        rules = decode_record_file(payloads["rules.json"], TradingRule, RULES_SCHEMA_ID)
        bars = decode_csv_bytes(payloads["bars.csv"], MarketBar)
    except SchemaError as exc:
        raise ArtifactsError(f"package_decode_failed:{exc}") from exc

    availability = parse_json_bytes(payloads["availability.json"])
    if not isinstance(availability, dict) or set(availability.keys()) != _AVAILABILITY_KEYS:
        raise ArtifactsError("availability_root_fields_mismatch")
    if availability["schema_version"] != AVAILABILITY_SCHEMA_ID:
        raise ArtifactsError("availability_schema_version_mismatch")
    _validate_availability_semantics(availability)

    bars = _check_no_input_mixing(instruments, sessions, bars, rules)
    bundle = FrozenInput(
        root=path,
        manifest_sha256=sha256_hex(manifest_bytes),
        instruments=instruments,
        sessions=sessions,
        bars=bars,
        macro_items=macro_items,
        rules=rules,
    )
    dataset_kind = manifest["dataset_kind"]
    if not isinstance(dataset_kind, str):
        raise ArtifactsError("dataset_kind_must_be_str")
    return bundle, dataset_kind


def _merge_bundles(parts: list[tuple[FrozenInput, str]], root: Path,
                   manifest_sha256: str) -> FrozenInput:
    kinds = {kind for _, kind in parts}
    if len(kinds) != 1:
        raise ArtifactsError("collection_mixed_dataset_kind")
    instruments: dict[str, Instrument] = {}
    sessions: dict[str, SessionWindow] = {}
    rules: dict[str, TradingRule] = {}
    macro_items: dict[str, MacroItem] = {}
    bars: dict[tuple[str, int, int], MarketBar] = {}

    def merge_unique(mapping: dict, records: tuple, key_field: str, label: str) -> None:
        for record in records:
            key = getattr(record, key_field)
            existing = mapping.get(key)
            if existing is not None:
                if existing != record:
                    raise ArtifactsError(f"collection_{label}_conflict:{key}")
                continue
            mapping[key] = record

    for bundle, _ in parts:
        merge_unique(instruments, bundle.instruments, "contract_id", "instrument")
        merge_unique(sessions, bundle.sessions, "session_id", "session")
        merge_unique(rules, bundle.rules, "rule_id", "rule")
        merge_unique(macro_items, bundle.macro_items, "item_id", "macro_item")
        for bar in bundle.bars:
            key = (bar.contract_id, bar.duration_seconds, bar.datetime_nano)
            existing = bars.get(key)
            if existing is not None:
                if existing != bar:
                    raise ArtifactsError("collection_bar_key_conflict")
                continue
            bars[key] = bar
    merged_bars = tuple(bars[key] for key in sorted(bars))
    merged_bars = _check_no_input_mixing(
        tuple(instruments.values()), tuple(sessions.values()), merged_bars, tuple(rules.values()))
    return FrozenInput(
        root=root,
        manifest_sha256=manifest_sha256,
        instruments=tuple(instruments.values()),
        sessions=tuple(sessions.values()),
        bars=merged_bars,
        macro_items=tuple(macro_items.values()),
        rules=tuple(rules.values()),
    )


_REQUEST_FIELDS = ("contract_ids", "raw_symbols", "start_nano", "end_nano",
                   "as_of_nano", "durations", "page_size", "maximum_rows",
                   "maximum_bytes")
_HEX = frozenset("0123456789abcdef")


def _is_hex_hash(value: object) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(char in _HEX for char in value))


def _validate_input_manifest_semantics(manifest: dict) -> None:
    """Strict manifest semantics (F10): request shape, dataset enum, hashes,
    nano integers and limitations are all validated before any file is read."""
    dataset_kind = manifest["dataset_kind"]
    if dataset_kind not in {"HISTORICAL_RECONSTRUCTED", "HISTORICAL_PIT",
                            "SYNTHETIC", "FORWARD_SHADOW"}:
        raise ArtifactsError("manifest_dataset_kind_not_in_allowed_enum")
    created = manifest["created_at_nano"]
    if isinstance(created, bool) or not isinstance(created, int):
        raise ArtifactsError("manifest_created_at_nano_must_be_int_not_bool")
    request = manifest["request"]
    if not isinstance(request, dict) or sorted(request) != sorted(_REQUEST_FIELDS):
        raise ArtifactsError("manifest_request_fields_mismatch")
    for field in ("contract_ids", "raw_symbols"):
        if (not isinstance(request[field], list)
                or not request[field]
                or any(not isinstance(item, str) for item in request[field])):
            raise ArtifactsError(f"manifest_request_{field}_must_be_non_empty_str_list")
    for field in ("start_nano", "end_nano", "as_of_nano", "page_size",
                  "maximum_rows", "maximum_bytes"):
        if isinstance(request[field], bool) or not isinstance(request[field], int):
            raise ArtifactsError(f"manifest_request_{field}_must_be_int_not_bool")
    if request["start_nano"] >= request["end_nano"]:
        raise ArtifactsError("manifest_request_start_must_precede_end")
    if request["durations"] != [60, 86400]:
        raise ArtifactsError("manifest_request_durations_must_be_60_86400")
    transaction = manifest["read_only_transaction"]
    if (not isinstance(transaction, dict)
            or set(transaction) != {"isolation", "read_only", "verified"}
            or not isinstance(transaction["isolation"], str)
            or transaction["read_only"] is not True
            or not isinstance(transaction["verified"], bool)):
        raise ArtifactsError("manifest_read_only_transaction_malformed")
    # F10(r3): SYNTHETIC inputs must declare NOT_APPLICABLE/true/false;
    # a real-database transaction claim on a synthetic bundle is invalid.
    if manifest["dataset_kind"] == "SYNTHETIC":
        if (transaction["isolation"] != "NOT_APPLICABLE"
                or transaction["verified"] is not False):
            raise ArtifactsError("synthetic_input_transaction_must_be_not_applicable_unverified")
    source_hashes = manifest["source_hashes"]
    if not isinstance(source_hashes, dict) or any(
            not isinstance(key, str) or not _is_hex_hash(value)
            for key, value in source_hashes.items()):
        raise ArtifactsError("manifest_source_hashes_values_must_be_sha256")
    limitations = manifest["limitations"]
    if not isinstance(limitations, list) or any(
            not isinstance(item, str) for item in limitations):
        raise ArtifactsError("manifest_limitations_must_be_list_str")


def _validate_availability_semantics(availability: dict) -> None:
    """Strict availability.json semantics (F10)."""
    reference = availability.get("input_manifest_sha256")
    if reference is not None and not _is_hex_hash(reference):
        raise ArtifactsError("availability_input_manifest_sha256_must_be_hex_or_null")
    if not isinstance(availability["contract_status"], dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in availability["contract_status"].items()):
        raise ArtifactsError("availability_contract_status_must_be_dict_str_str")
    for field in ("minute_history_status", "realtime_status"):
        if not isinstance(availability[field], str):
            raise ArtifactsError(f"availability_{field}_must_be_str")
    if not isinstance(availability["reasons"], list) or any(
            not isinstance(item, str) for item in availability["reasons"]):
        raise ArtifactsError("availability_reasons_must_be_list_str")


def read_frozen_input(path: Path) -> FrozenInput:
    """Load a frozen input package (single bundle or read-only collection)."""
    if not isinstance(path, Path):
        raise TypeError("path_must_be_Path")
    if not path.is_dir() or path.is_symlink():
        raise ArtifactsError("frozen_input_path_must_be_directory")
    manifest_path = path / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ArtifactsError("manifest_missing")
    try:
        manifest = parse_json_bytes(manifest_path.read_bytes())
    except SchemaError as exc:
        raise ArtifactsError(f"manifest_invalid_json:{exc}") from exc
    if not isinstance(manifest, dict):
        raise ArtifactsError("manifest_root_must_be_object")
    schema_version = manifest.get("schema_version")
    if schema_version == INPUT_SCHEMA_VERSION:
        bundle, _ = _read_single_package(path)
        return bundle
    if schema_version != COLLECTION_SCHEMA_ID:
        raise ArtifactsError("manifest_schema_version_mismatch")
    if set(manifest.keys()) != _COLLECTION_KEYS:
        raise ArtifactsError("collection_manifest_root_fields_mismatch")
    # F10(r3): a collection root manifest must list at least one actual file.
    if not isinstance(manifest.get("files"), dict) or not manifest["files"]:
        raise ArtifactsError("collection_files_must_not_be_empty")
    if manifest["contract_version"] != CONTRACT_VERSION:
        raise ArtifactsError("collection_contract_version_mismatch")
    shards = manifest["shards"]
    if not isinstance(shards, list) or not shards or any(
        not isinstance(shard, dict) or set(shard.keys()) != {"path", "manifest_sha256"}
        for shard in shards
    ):
        raise ArtifactsError("collection_shards_malformed")
    parts: list[tuple[FrozenInput, str]] = []
    for shard in shards:
        _safe_package_path(path, str(shard["path"]) + "/manifest.json")
        shard_dir = path / str(shard["path"])
        bundle, shard_kind = _read_single_package(shard_dir)
        if bundle.manifest_sha256 != shard["manifest_sha256"]:
            raise ArtifactsError(f"collection_shard_manifest_mismatch:{shard['path']}")
        if shard_kind != manifest["dataset_kind"]:
            raise ArtifactsError("collection_shard_dataset_kind_mismatch")
        parts.append((bundle, shard_kind))
    collection_manifest_sha256 = sha256_hex(manifest_path.read_bytes())
    return _merge_bundles(parts, path, collection_manifest_sha256)


def publish_input_collection(shards: tuple[Path, ...], output: Path) -> FrozenInput:
    """Merge accepted read-only shards into a brand-new collection directory.

    Shards are never re-queried or edited: files are copied verbatim into
    ``shards/<input manifest sha>/`` and the collection manifest is
    committed last atomically. Only compatible schemas and the same
    evidence level merge; identical identities must be byte-equal and bar
    keys may neither conflict nor lose rows.
    """
    if not isinstance(shards, tuple) or not shards or any(
        not isinstance(shard, Path) for shard in shards):
        raise TypeError("shards_must_be_non_empty_tuple_of_Path")
    if not isinstance(output, Path):
        raise TypeError("output_must_be_Path")
    if output.exists() or output.is_symlink():
        raise ArtifactsError("output_directory_exists")
    parts: list[tuple[FrozenInput, str]] = []
    for shard in shards:
        bundle, dataset_kind = _read_single_package(shard)
        parts.append((bundle, dataset_kind))
    kinds = {kind for _, kind in parts}
    if len(kinds) != 1:
        raise ArtifactsError("collection_mixed_dataset_kind")

    output.mkdir(parents=True, exist_ok=False)
    files: dict[str, str] = {}
    shard_entries: list[dict[str, str]] = []
    copied_hashes: set[str] = set()
    for bundle, _ in parts:
        if bundle.manifest_sha256 in copied_hashes:
            continue
        copied_hashes.add(bundle.manifest_sha256)
        relative_dir = f"shards/{bundle.manifest_sha256}"
        shard_root = output / relative_dir
        shard_root.mkdir(parents=True, exist_ok=False)
        manifest_bytes = (bundle.root / "manifest.json").read_bytes()
        declared = parse_json_bytes(manifest_bytes)
        copied: list[tuple[str, bytes]] = [("manifest.json", manifest_bytes)]
        for name in sorted(declared["files"].keys()):
            source = _safe_package_path(bundle.root, str(name))
            copied.append((str(name), source.read_bytes()))
        for name, payload in copied:
            target = shard_root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "xb") as handle:
                handle.write(payload)
            files[f"{relative_dir}/{name}"] = sha256_hex(payload)
        shard_entries.append({"path": relative_dir, "manifest_sha256": bundle.manifest_sha256})

    limitations = [
        "collection_merged_read_only_from_accepted_shards",
        "database_not_requeried",
        "identical_identities_deduplicated_bar_keys_neither_conflict_nor_drop",
    ]
    collection_manifest = canonical_json_bytes({
        "schema_version": COLLECTION_SCHEMA_ID,
        "contract_version": CONTRACT_VERSION,
        "dataset_kind": next(iter(kinds)),
        "shards": shard_entries,
        "files": files,
        "limitations": limitations,
    })
    collection_sha = sha256_hex(collection_manifest)
    temporary = output / "manifest.json.tmp"
    with open(temporary, "wb") as handle:
        handle.write(collection_manifest)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, output / "manifest.json")
    return _merge_bundles(parts, output, collection_sha)


def _check_no_input_mixing(instruments: tuple[Instrument, ...], sessions: tuple[SessionWindow, ...],
                           bars: tuple[MarketBar, ...], rules: tuple[TradingRule, ...]) \
        -> tuple[MarketBar, ...]:
    by_contract = {instrument.contract_id: instrument for instrument in instruments}
    if len(by_contract) != len(instruments):
        raise ArtifactsError("duplicate_instrument_contract_id")
    deduplicated: dict[tuple[str, int, int], MarketBar] = {}
    for bar in bars:
        instrument = by_contract.get(bar.contract_id)
        if instrument is None:
            raise ArtifactsError(f"bar_contract_not_declared:{bar.contract_id}")
        if bar.raw_symbol != instrument.raw_symbol or bar.product != instrument.product:
            raise ArtifactsError(f"bar_identity_mismatch:{bar.contract_id}")
        key = (bar.contract_id, bar.duration_seconds, bar.datetime_nano)
        existing = deduplicated.get(key)
        if existing is not None:
            if existing != bar:
                raise ArtifactsError("conflicting_duplicate_bar")
            continue
        deduplicated[key] = bar
    merged_bars = tuple(deduplicated[key] for key in sorted(deduplicated))
    session_ids = {session.session_id for session in sessions}
    if len(session_ids) != len(sessions):
        raise ArtifactsError("duplicate_session_id")
    for bar in merged_bars:
        if bar.session_id is not None and bar.session_id not in session_ids:
            raise ArtifactsError(f"bar_session_not_declared:{bar.session_id}")
    for rule in rules:
        if rule.contract_id not in by_contract:
            raise ArtifactsError(f"rule_contract_not_declared:{rule.contract_id}")
    return merged_bars


def _current_trading_day(bundle: FrozenInput, contract_id: str, decision_nano: int):
    # The current trading day is derived ONLY from the requested contract's
    # product sessions (F05): another product's overlapping calendar must not
    # leak through the intraday daily filter.
    products = {instrument.product for instrument in bundle.instruments
                if instrument.contract_id == contract_id}
    products.update(bar.product for bar in bundle.bars if bar.contract_id == contract_id)
    scoped = [s for s in bundle.sessions if s.product in products]
    for session in sorted(scoped, key=lambda s: s.start_nano):
        if session.start_nano <= decision_nano < session.end_nano:
            return session.trading_day
    preceding = [s for s in scoped if s.end_nano <= decision_nano]
    if preceding:
        return max(preceding, key=lambda s: s.end_nano).trading_day
    return None


def visible_bars(bundle: FrozenInput, contract_id: str, duration_seconds: int,
                 decision_nano: int, intraday: bool) -> tuple[MarketBar, ...]:
    """Bars of one contract legally visible at ``decision_nano``.

    A bar must be complete, closed and available at or before the decision
    time. With ``intraday=True`` the current trading day's daily bars are
    unconditionally filtered out, even if the package already carries the
    day's final values.
    """
    if not isinstance(bundle, FrozenInput):
        raise TypeError("bundle_must_be_FrozenInput")
    if not isinstance(contract_id, str):
        raise TypeError("contract_id_must_be_str")
    if isinstance(duration_seconds, bool) or not isinstance(duration_seconds, int):
        raise TypeError("duration_seconds_must_be_int")
    if isinstance(decision_nano, bool) or not isinstance(decision_nano, int):
        raise TypeError("decision_nano_must_be_int")
    if not isinstance(intraday, bool):
        raise TypeError("intraday_must_be_bool")
    current_day = (_current_trading_day(bundle, contract_id, decision_nano)
                   if intraday and duration_seconds == 86400 else None)
    # F21(r3): build the set of products for this contract to check minute
    # grid alignment against the contract's own sessions.
    _products = {bar.product for bar in bundle.bars if bar.contract_id == contract_id}
    _sessions = [s for s in bundle.sessions if s.product in _products]

    def _on_minute_grid(bar: object) -> bool:
        if not isinstance(bar, MarketBar) or bar.duration_seconds != 60:
            return True
        for session in _sessions:
            if session.start_nano <= bar.start_nano < session.end_nano:
                return (bar.start_nano - session.start_nano) % 60_000_000_000 == 0
        return True  # no calendar for this product — let other checks handle

    visible = [
        bar for bar in bundle.bars
        if bar.contract_id == contract_id
        and bar.duration_seconds == duration_seconds
        and bar.complete
        and bar.end_nano <= decision_nano
        and bar.available_at_nano is not None
        and bar.available_at_nano <= decision_nano
        # F07: bars whose availability basis is unknown are retained for audit
        # but are never READY-consumable at this boundary.
        and bar.quality.status == "READY"
        # F21(r3): READY bars with an unknown temporal basis carry no honest
        # source evidence — they are rejected at the consumption boundary.
        and bar.quality.temporal_basis != "unknown"
        # F21(r3): 60-second bars must align to the planned minute grid.
        and _on_minute_grid(bar)
        and not (current_day is not None and bar.trading_day == current_day)
    ]
    return tuple(sorted(visible, key=lambda b: (b.start_nano, b.datetime_nano)))


def visible_rule(bundle: FrozenInput, contract_id: str, decision_nano: int) -> TradingRule | None:
    """The latest trading rule effective and available at ``decision_nano``."""
    if not isinstance(bundle, FrozenInput):
        raise TypeError("bundle_must_be_FrozenInput")
    if not isinstance(contract_id, str):
        raise TypeError("contract_id_must_be_str")
    if isinstance(decision_nano, bool) or not isinstance(decision_nano, int):
        raise TypeError("decision_nano_must_be_int")
    candidates = [
        rule for rule in bundle.rules
        if rule.contract_id == contract_id
        and rule.effective_from_nano <= decision_nano < rule.effective_until_nano
        and rule.available_at_nano <= decision_nano
    ]
    if not candidates:
        return None
    latest_from = max(rule.effective_from_nano for rule in candidates)
    effective = [rule for rule in candidates if rule.effective_from_nano == latest_from]
    if len(effective) > 1:
        raise ValueError("conflicting_rules_same_effective_from")
    return effective[0]
