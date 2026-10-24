"""NE1 execution client installer/rollback tool (plan 12.1/12.2/12.5, C3 final).

Promoted from the delivery-prep draft on 2026-09-14 after the C3 service-side
freeze (client_service.py 0799c2df..., run_infiny_execution.py e3c6cfe3...,
TP eb796a4c...). Plan reference: docs/infiny-native-execution-plan.md
(infiny-native-execution-plan-v1.0, sha256 89563d4a...):
  - 12.1 precise deployment objects
  - 12.2 install tool and backup discipline (five fixed steps)
  - 12.5 rollback steps
  - NE1-H38 (install coexistence/backup/rollback), NE1-H06 (template never
    restores enable_trading), NE1-H39/H40 (count/hash honesty)
Pattern reference: scripts/install_infiny_observer.py (safe-path and receipt
discipline; that tool keeps its own narrower observer-only scope).

NE1 pairing last-mile upgrade (decision NE1-ARB-PAIRING-LASTMILE-001, §六
LM-D row; tool version ``ne1-delivery-c3-pairing-v2``):
  - ``plan``/``apply`` accept the NON-SECRET ``--strategy-id INTEGER`` and
    verify the client's deterministic default store slot
    ``<strategy-directory>/.ak_execution/strategy-<strategy_id>`` (RULING 01
    R1.1). ``--client-store absent`` is accepted ONLY when that slot is
    measured as not existing on disk; once any slot exists, the exact actual
    directory must be declared (an existing store must never be recorded as
    absent - M08).
  - ``plan`` performs the measurement read-only and NEVER creates the store
    (only the client's first valid ``on_start`` creates it, R1.2/R1.3).
  - ``apply`` never modifies strategy wiring or credentials: it still only
    copies the single release file, and the store consistency backup covers
    receipts.db/outbox.db exclusively - pairing.json, identity.json and the
    pairing request are never copied into ordinary evidence (R2.2/R2.3).
  - ``rollback`` is unchanged in kind: code bytes only; journal, outbox,
    receipts, credential facts are never rolled back or restored.

NE1 arming upgrade (decision NE1-ARB-ARMING-001, §八 ARM-LM-D row; tool
version ``ne1-delivery-20260918-arming-v1``):
  - the client store's arming-v1 extension slots (``arming-proposal.json``,
    ``arming-plan.json``, ``arming-request.json``, ``arming-challenge.json``,
    ``arming.json``, ``disarm.json``, ``arming-state.json``,
    ``arming-transfer.lock`` - AR4.1) are NEVER backed up, copied, restored
    or hashed by this installer (AR4.3/AR-M13): the consistency backup stays
    receipts.db/outbox.db ONLY and a hard guard proves the backup tree never
    contains anything else;
  - rollback stays code-bytes-only: it never rolls back, restores or
    "repairs" arming state, credentials or business facts; leftover arming
    slots of a superseded round are judged expired/revoked by the CLIENT,
    never by this tool;
  - the parameter template safe defaults (enable_trading=false, mode
    disabled, 0/0 budgets) remain frozen.

Deployment object: exactly ONE file - the self-contained client
``AKExecutionBridge.py`` - copied from an explicitly given release directory
into the InfiniTrader ``pyStrategy/self_strategy`` directory. The observer
pair (``AKObserverBridge.py`` + ``AIBridge.py``) is verified read-only and is
NEVER written by this tool. The tool performs no UI automation, never enables
trading, never starts/stops the 17778 or 17779 services, and contains no
network code at all.

Pending-state provenance (C3 ruling, recorded honestly): the frozen service
launcher has no offline introspection subcommand; ``--pending-state`` is an
operator-produced JSON whose authoritative production source is the
authenticated ``GET /execution/v1/status`` snapshot taken while the service
process runs (execution counters and pending_reconcile_count are durable
journal facts; the /events duplicate view and the status ACK-watermark lookup
are process-local views) plus the client store counts. A journal that is not
absent therefore requires ``source`` and ``sampled_at`` on the record.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath

TOOL_VERSION = "ne1-delivery-20260918-arming-v1"
OPERATION = "ne1_execution_client_deploy_v1"
ROLLBACK_OPERATION = "ne1_execution_client_rollback_v1"
TARGET_NAME = "AKExecutionBridge.py"
OBSERVER_NAMES = ("AKObserverBridge.py", "AIBridge.py")
PLAN_RELATIVE = "docs/infiny-native-execution-plan.md"
TEMPLATE_RELATIVE = "config/infiny_native_execution.example.json"
INSTALLER_RELATIVE = "scripts/install_infiny_execution.py"

# NE1 pairing last-mile (RULING 01 R1.1): the client's deterministic default
# store slot lives at <strategy-directory>/.ak_execution/strategy-<strategy_id>.
STORE_ROOT_NAME = ".ak_execution"
STORE_SLOT_PREFIX = "strategy-"

# NE1-ARB-ARMING-001 AR4.1: the client store's arming-v1 extension slot
# names. Recorded in backup receipts as the FIXED exclusion list only; the
# installer never reads, copies, hashes or restores any of them (AR4.3).
ARMING_SLOT_NAMES = (
    "arming-proposal.json", "arming-plan.json", "arming-request.json",
    "arming-challenge.json", "arming.json", "disarm.json",
    "arming-state.json", "arming-transfer.lock",
)

# Informational version marker expected inside both deployed observer files.
OBSERVER_VERSION_MARKER = "observer-20260914-reconnect-v5"

# ---------------------------------------------------------------------------
# Frozen expectations (overall-revision-r1 chain; never promote a raw
# observation to a frozen value - see data/maintenance_artifacts/
# infiny-ne1-delivery-prep/hash-slots-checklist.md for provenance rules).
# ---------------------------------------------------------------------------
EXPECTED_PLAN_SHA256 = "89563d4a4566613282105ead6b14c92c7be374f901d159e916d6b063c53431fd"
# Observer pair: LAST MAIN-SESSION-RECORDED deployment baseline (v5 acceptance,
# sampled 2026-09-14, data/maintenance_artifacts/infiny-v5-observer-acceptance).
# The deploy-time authority is the MEASURED pair hash recorded with its
# sampling time in deployment.json; a drifted pair blocks apply until a
# separately recorded observer maintenance supplies the new expected value.
OBSERVER_EXPECTED_SHA256 = "2ae4981ef49f99cc12fc39f92aff4d5655f7d9d46191acbd8c54e5ae8b221fb1"

# Exact parameter template key set (plan 12.3). "volume" is deliberately NOT
# a template key; the client accepts it only as an order_volume alias and
# rejects conflicting values.
TEMPLATE_KEYS = (
    "exchange", "instrument_id", "additional_symbols", "api_url",
    "execution_api_url", "batch_size", "account_tag", "investor_id",
    "hedge_flag", "enable_trading", "execution_mode", "order_volume",
    "order_type", "risk_policy_id", "authorization_id",
    "parameter_confirmation", "max_native_sends", "max_native_cancels",
)
TEMPLATE_EMPTY_KEYS = (
    "exchange", "instrument_id", "additional_symbols", "account_tag",
    "investor_id", "hedge_flag", "risk_policy_id", "authorization_id",
    "parameter_confirmation",
)
TEMPLATE_API_URL = "http://127.0.0.1:17778/analyze"
TEMPLATE_EXECUTION_API_URL = "http://127.0.0.1:17779/execution/v1/poll"
PENDING_STATE_SOURCES = frozenset({
    "service_status", "client_store_counts", "manual_journal_review"})

_INSTALL_ID_HEX = re.compile(r"[0-9a-f]{32}")


class InstallError(ValueError):
    """Rejected operation; message doubles as the machine-readable code."""


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise InstallError(code)


# ---------------------------------------------------------------------------
# local-path safety (adapted from scripts/install_infiny_observer.py)
# ---------------------------------------------------------------------------

def _safe_local_path(value: Path, *, allow_missing: bool = True) -> Path:
    raw = str(value)
    windows = PureWindowsPath(raw)
    if not raw or windows.drive.startswith("\\\\"):
        raise InstallError("LOCAL_PATH_REQUIRED")
    reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", "CLOCK$"}
    reserved.update(f"{prefix}{number}" for prefix in ("COM", "LPT") for number in "123456789")
    for part in windows.parts:
        if part in (windows.anchor, ".", ".."):
            continue
        stem = part.split(".", 1)[0].rstrip(" ").upper()
        if (part.rstrip(" .") != part or stem in reserved
                or any(ord(char) < 32 or ord(char) == 127 or char in '<>:"|?*' for char in part)):
            raise InstallError("RESERVED_OR_INVALID_PATH")
    absolute = Path(value).absolute()
    for ancestor in reversed((absolute, *absolute.parents)):
        if ancestor.is_symlink() or (hasattr(ancestor, "is_junction") and ancestor.is_junction()):
            raise InstallError("LINK_OR_JUNCTION_REJECTED")
    if not allow_missing and not absolute.exists():
        raise InstallError("PATH_DOES_NOT_EXIST")
    return absolute


# ---------------------------------------------------------------------------
# small shared helpers
# ---------------------------------------------------------------------------

def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _hash_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _hash_file(path: Path) -> str:
    return _hash_bytes(path.read_bytes())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _load_small_json(path: Path, *, limit: int = 32 * 1024) -> dict:
    path = _safe_local_path(path, allow_missing=False)
    _require(path.is_file() and not path.is_symlink(), "EVIDENCE_FILE_INVALID")
    raw = path.read_bytes()
    _require(len(raw) <= limit, "EVIDENCE_FILE_TOO_LARGE")
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    _require(isinstance(value, dict), "EVIDENCE_JSON_NOT_OBJECT")
    return value


def _fresh_directory(value: str) -> Path:
    directory = _safe_local_path(Path(value))
    _require(not directory.exists(), "EVIDENCE_DIRECTORY_MUST_BE_FRESH")
    return directory


def _atomic_write_new(path: Path, payload: bytes) -> None:
    """Create a new file (no replace); mirrors the observer installer."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=str(path.parent), prefix=f".{path.name}.",
                                         delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _atomic_replace(path: Path, payload: bytes) -> None:
    """Write temp + fsync in the target directory, then atomic os.replace."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=str(path.parent), prefix=f".{path.name}.",
                                         delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# validators (plan 12.2 step 1, template 12.3, H38)
# ---------------------------------------------------------------------------

def validate_release_bytes(payload: bytes) -> str:
    """Structural checks on the release client file; returns ADAPTER_VERSION."""
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InstallError("RELEASE_NOT_UTF8") from exc
    compile(payload, TARGET_NAME, "exec")
    _require(text.count("class AKExecutionBridge") == 1, "RELEASE_CLASS_COUNT_INVALID")
    _require("class AKObserverBridge" not in text, "RELEASE_MUST_NOT_CONTAIN_OBSERVER")
    _require("AK_OBSERVER_INSTALL_ID" not in text, "RELEASE_MUST_NOT_CARRY_OBSERVER_MARKER")
    _require("AK_EXECUTION_INSTALL_ID" not in text, "RELEASE_MUST_NOT_CARRY_INSTALL_MARKER")
    match = re.search(r'ADAPTER_VERSION\s*=\s*"ne1-execution-[0-9A-Za-z._-]+"', text)
    _require(match is not None, "RELEASE_ADAPTER_VERSION_MISSING")
    _require('DEFAULT_EXECUTION_API_URL = "http://127.0.0.1:17779' in text,
             "RELEASE_EXECUTION_URL_BASELINE_MISSING")
    _require("enable_trading" in text and "Field(False" in text,
             "RELEASE_DEFAULT_DISABLED_GUARD_MISSING")
    return match.group(0).split('=', 1)[1].strip().strip('"')


def validate_template_bytes(payload: bytes) -> dict:
    """Strict validation of config/infiny_native_execution.example.json (12.3)."""
    value = json.loads(payload.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    _require(isinstance(value, dict), "TEMPLATE_NOT_OBJECT")
    _require(tuple(sorted(value)) == tuple(sorted(TEMPLATE_KEYS)), "TEMPLATE_KEYSET_INVALID")
    _require("volume" not in value, "TEMPLATE_MUST_NOT_CARRY_VOLUME_ALIAS")
    for key in TEMPLATE_EMPTY_KEYS:
        _require(value[key] == "", f"TEMPLATE_{key.upper()}_MUST_BE_EMPTY")
    _require(value["api_url"] == TEMPLATE_API_URL, "TEMPLATE_API_URL_INVALID")
    _require(value["execution_api_url"] == TEMPLATE_EXECUTION_API_URL,
             "TEMPLATE_EXECUTION_API_URL_INVALID")
    _require(value["enable_trading"] is False, "TEMPLATE_ENABLE_TRADING_MUST_BE_FALSE")
    _require(value["execution_mode"] == "disabled", "TEMPLATE_MODE_MUST_BE_DISABLED")
    _require(type(value["batch_size"]) is int and not isinstance(value["batch_size"], bool)
             and value["batch_size"] >= 1, "TEMPLATE_BATCH_SIZE_INVALID")
    _require(type(value["order_volume"]) is int and not isinstance(value["order_volume"], bool)
             and value["order_volume"] >= 1, "TEMPLATE_ORDER_VOLUME_INVALID")
    _require(value["order_type"] in ("GFD", "FAK"), "TEMPLATE_ORDER_TYPE_INVALID")
    _require(value["max_native_sends"] == 0, "TEMPLATE_MAX_SENDS_MUST_BE_ZERO")
    _require(value["max_native_cancels"] == 0, "TEMPLATE_MAX_CANCELS_MUST_BE_ZERO")
    return value


def read_observer_state(strategy_directory: Path, expected_sha256: str) -> dict:
    """12.2 step 1: observer pair must exist, be identical, match expectation."""
    state = {"expected_sha256": expected_sha256,
             "version_marker": OBSERVER_VERSION_MARKER, "hashes": {}, "identical": False,
             "version_marker_present": {}, "sampled_at": _now_iso()}
    payloads = {}
    for name in OBSERVER_NAMES:
        path = strategy_directory / name
        _require(path.exists() and path.is_file() and not path.is_symlink(),
                 f"OBSERVER_FILE_INVALID:{name}")
        payloads[name] = path.read_bytes()
        state["hashes"][name] = _hash_bytes(payloads[name])
        state["version_marker_present"][name] = OBSERVER_VERSION_MARKER in payloads[name].decode(
            "utf-8", errors="replace")
    state["identical"] = state["hashes"][OBSERVER_NAMES[0]] == state["hashes"][OBSERVER_NAMES[1]]
    state["matches_expected"] = (
        state["identical"] and state["hashes"][OBSERVER_NAMES[0]] == expected_sha256)
    state["version_ok"] = all(state["version_marker_present"].values())
    # Report-style result: apply refuses when not ok; plan records the mismatch.
    state["ok"] = bool(state["matches_expected"] and state["version_ok"])
    return state


def load_stopped_evidence(path: Path) -> dict:
    """12.2: apply requires user-stopped-execution-instance evidence."""
    record = _load_small_json(path)
    for key in ("instance", "generation", "stopped_at", "stop_log_ref", "confirmed_by"):
        _require(isinstance(record.get(key), str) and record[key].strip(), f"STOPPED_EVIDENCE_{key.upper()}_MISSING")
    _require(record.get("reload_pending") is True, "STOPPED_EVIDENCE_RELOAD_PENDING_REQUIRED")
    return record


def load_pending_state(path: Path) -> dict:
    """12.2 step 3: pending attempts / unacked events / real call counters.

    Provenance (C3 ruling, see module docstring): ``source``/``sampled_at``
    are REQUIRED whenever the journal exists; counters come from the running
    service's authenticated status snapshot (durable journal facts) and the
    client store counts, never from memory of a stopped process.
    """
    record = _load_small_json(path)
    for key in ("journal_pending_attempts", "client_unacked_events",
                "real_native_send_attempts", "real_native_cancel_attempts"):
        value = record.get(key)
        _require(type(value) is int and not isinstance(value, bool) and value >= 0,
                 f"PENDING_STATE_{key.upper()}_INVALID")
    _require(isinstance(record.get("recorded_at"), str) and record["recorded_at"].strip(),
             "PENDING_STATE_RECORDED_AT_MISSING")
    source = record.get("source")
    if source is not None:
        _require(source in PENDING_STATE_SOURCES, "PENDING_STATE_SOURCE_INVALID")
        _require(isinstance(record.get("sampled_at"), str) and record["sampled_at"].strip(),
                 "PENDING_STATE_SAMPLED_AT_REQUIRED_WITH_SOURCE")
    return record


def load_gate_evidence(path: Path) -> dict:
    """12.5: rollback requires gate-closed / stopped-issuing evidence."""
    record = _load_small_json(path)
    _require(record.get("gate_closed") is True, "GATE_EVIDENCE_GATE_CLOSED_REQUIRED")
    _require(record.get("service_stopped_issuing") is True,
             "GATE_EVIDENCE_SERVICE_STOP_REQUIRED")
    _require(isinstance(record.get("recorded_at"), str) and record["recorded_at"].strip(),
             "GATE_EVIDENCE_RECORDED_AT_MISSING")
    refs = record.get("evidence_refs")
    _require(isinstance(refs, list) and refs and all(isinstance(item, str) and item.strip()
             for item in refs), "GATE_EVIDENCE_REFS_INVALID")
    return record


def load_disposition(path: Path) -> dict:
    record = _load_small_json(path)
    for key in ("summary", "unresolved_orders", "user_disposition", "recorded_by"):
        _require(isinstance(record.get(key), (str, list)) and record[key], f"DISPOSITION_{key.upper()}_MISSING")
    return record


# ---------------------------------------------------------------------------
# sqlite consistency backups (12.2 step 3; never a raw file copy - WAL lesson 8.3)
# ---------------------------------------------------------------------------

def backup_sqlite(source: Path, destination: Path) -> dict:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    destination_connection = sqlite3.connect(str(destination))
    try:
        source_connection.backup(destination_connection)
        destination_connection.commit()
        row = destination_connection.execute("PRAGMA integrity_check").fetchone()
        integrity = row[0] if row else "unknown"
    finally:
        destination_connection.close()
        source_connection.close()
    return {"source": str(source), "backup": str(destination),
            "backup_sha256": _hash_file(destination), "integrity_check": integrity}


def _sqlite_scalar(connection: sqlite3.Connection, sql: str):
    try:
        row = connection.execute(sql).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def backup_client_store(store_directory: Path, backup_directory: Path) -> dict:
    """Back up the deployed client's receipts/outbox (NE1 client store layout:
    receipts.db + outbox.db). Counts come from the verified backup copy.

    NE1-ARB-ARMING-001 AR4.3/AR-M13: ONLY these two business databases are
    ever collected. The arming-v1 slots (arming-proposal/plan/request/
    challenge/state, arming.json, disarm.json, arming-transfer.lock) and the
    pairing credentials (pairing.json, identity.json, pairing-request.json)
    are NEVER read into a backup - a hard guard below proves the backup tree
    contains nothing beyond the expected database files."""
    record = {"directory": str(store_directory), "files": {},
              "excluded_slots": list(ARMING_SLOT_NAMES),
              "exclusion_note": "credential/arming slots are never backed "
                                "up, copied, hashed or restored by the "
                                "installer (AR4.3/AR-M13)"}
    _require(store_directory.is_dir() and not store_directory.is_symlink(),
             "CLIENT_STORE_DIRECTORY_INVALID")
    for name, queries in (("receipts.db", {"receipt_count": "SELECT COUNT(*) FROM receipts"}),
                          ("outbox.db", {"unacked_events": "SELECT COUNT(*) FROM events WHERE acked=0",
                                         "unacked_bytes": "SELECT COALESCE(SUM(LENGTH(body)),0) FROM events WHERE acked=0"})):
        source = store_directory / name
        entry: dict = {"present": source.exists()}
        if source.exists():
            _require(source.is_file() and not source.is_symlink(), f"CLIENT_STORE_{name.upper()}_INVALID")
            entry.update(backup_sqlite(source, backup_directory / name))
            connection = sqlite3.connect(f"file:{(backup_directory / name).as_posix()}?mode=ro", uri=True)
            try:
                for key, sql in queries.items():
                    entry[key] = _sqlite_scalar(connection, sql)
            finally:
                connection.close()
        record["files"][name] = entry
    # hard guard: nothing beyond the two business databases was collected
    collected = sorted(path.name for path in backup_directory.iterdir())
    _require(collected == [name for name in ("outbox.db", "receipts.db")
                           if (store_directory / name).exists()],
             "CLIENT_STORE_BACKUP_SCOPE_VIOLATION")
    return record


# ---------------------------------------------------------------------------
# deterministic client-store slot discipline (RULING 01 R1.1 / M08; read-only)
# ---------------------------------------------------------------------------

def _validate_strategy_id(value: object) -> int:
    _require(type(value) is int and not isinstance(value, bool) and value >= 0,
             "STRATEGY_ID_INVALID")
    return value


def _discover_strategy_slots(strategy_directory: Path) -> list[dict]:
    """Read-only discovery of ``<strategy-directory>/.ak_execution/strategy-*``
    slots. Nothing is ever created here; a malformed store root or slot name
    is rejected instead of guessed."""
    root = strategy_directory / STORE_ROOT_NAME
    if not root.exists():
        return []
    _require(root.is_dir() and not root.is_symlink(), "STORE_ROOT_INVALID")
    slots = []
    for entry in sorted(root.iterdir()):
        if not entry.name.startswith(STORE_SLOT_PREFIX):
            continue
        suffix = entry.name[len(STORE_SLOT_PREFIX):]
        _require(entry.is_dir() and not entry.is_symlink() and suffix.isdigit()
                 and _validate_strategy_id(int(suffix)) >= 0,
                 f"STORE_SLOT_INVALID:{entry.name}")
        slots.append({"name": entry.name, "strategy_id": int(suffix),
                      "path": str(entry)})
    return slots


def resolve_client_store(strategy_directory: Path, declared: str,
                         strategy_id: int | None) -> tuple[Path | None, dict]:
    """Enforce the M08 declaration rules and return (store_path_or_None, record).

    - ``absent`` is accepted ONLY when no ``strategy-*`` slot exists on disk:
      an existing store must never be recorded as absent;
    - an explicit declaration must be the exact actual directory of a
      discovered slot;
    - with ``--strategy-id`` the declared/existing state must match the
      measured slot (declared store => slot exists; absent => slot absent).

    Measurement is purely read-only (plan must not create the store; only the
    client's first valid on_start creates it)."""
    if strategy_id is not None:
        _validate_strategy_id(strategy_id)
    slots = _discover_strategy_slots(strategy_directory)
    slot_path = (strategy_directory / STORE_ROOT_NAME
                 / f"{STORE_SLOT_PREFIX}{strategy_id}"
                 if strategy_id is not None else None)
    record = {"declared": declared,
              "strategy_id": strategy_id,
              "measured_at": _now_iso(),
              "store_root": str(strategy_directory / STORE_ROOT_NAME),
              "expected_slot_path": str(slot_path) if slot_path is not None else None,
              "existing_slots": [dict(slot) for slot in slots],
              "slot_exists": (any(slot["strategy_id"] == strategy_id
                                  for slot in slots)
                              if strategy_id is not None else None)}
    if declared == "absent":
        _require(not slots, "CLIENT_STORE_SLOT_EXISTS_MUST_BE_EXPLICIT")
        if strategy_id is not None:
            _require(not record["slot_exists"], "CLIENT_STORE_SLOT_EXISTS_MUST_BE_EXPLICIT")
        record["path"] = "absent"
        return None, record
    path = _safe_local_path(Path(declared), allow_missing=False)
    _require(path.is_dir() and not path.is_symlink(), "CLIENT_STORE_DIRECTORY_INVALID")
    normalized = os.path.normcase(str(path))
    _require(normalized in {os.path.normcase(slot["path"]) for slot in slots},
             "CLIENT_STORE_SLOT_MISMATCH")
    if strategy_id is not None:
        _require(record["slot_exists"]
                 and normalized == os.path.normcase(str(slot_path)),
                 "CLIENT_STORE_SLOT_MISMATCH")
    record["path"] = str(path)
    return path, record


# ---------------------------------------------------------------------------
# repo-root resolution (plan doc + template live in the repository)
# ---------------------------------------------------------------------------

def _resolve_repo_root(args) -> Path:
    if args.repo_root is not None:
        root = _safe_local_path(Path(args.repo_root), allow_missing=False)
    else:
        root = Path(__file__).resolve().parents[1]
    _require((root / PLAN_RELATIVE).is_file(), "REPO_ROOT_PLAN_DOC_MISSING")
    return root


def _resolve_paths(args, *, need_release: bool) -> dict:
    strategy_directory = _safe_local_path(Path(args.strategy_directory), allow_missing=False)
    _require(strategy_directory.is_dir(), "STRATEGY_DIRECTORY_NOT_A_DIRECTORY")
    paths = {"strategy_directory": strategy_directory}
    if need_release:
        release_directory = _safe_local_path(Path(args.release_directory), allow_missing=False)
        _require(release_directory.is_dir(), "RELEASE_DIRECTORY_NOT_A_DIRECTORY")
        release_file = release_directory / TARGET_NAME
        _require(release_file.is_file() and not release_file.is_symlink(),
                 "RELEASE_FILE_INVALID")
        paths["release_directory"] = release_directory
        paths["release_file"] = release_file
    return paths


def _sqlite_target(value: str, label: str) -> Path | None:
    if value == "absent":
        return None
    path = _safe_local_path(Path(value), allow_missing=False)
    _require(path.is_file() and not path.is_symlink(), f"{label}_INVALID")
    return path


def _require_pending_provenance(pending: dict, journal_is_real: bool) -> None:
    if journal_is_real:
        _require(pending.get("source") is not None, "PENDING_STATE_SOURCE_REQUIRED_FOR_JOURNAL")
        _require(isinstance(pending.get("sampled_at"), str) and pending["sampled_at"].strip(),
                 "PENDING_STATE_SAMPLED_AT_REQUIRED_FOR_JOURNAL")


# ---------------------------------------------------------------------------
# plan (dry-run, read-only audit; writes only into the fresh evidence dir)
# ---------------------------------------------------------------------------

def cmd_plan(args) -> dict:
    repo_root = _resolve_repo_root(args)
    paths = _resolve_paths(args, need_release=True)
    template_path = _safe_local_path(Path(args.template) if args.template
                                     else repo_root / TEMPLATE_RELATIVE, allow_missing=False)
    plan_path = repo_root / PLAN_RELATIVE
    release_payload = paths["release_file"].read_bytes()
    adapter_version = validate_release_bytes(release_payload)
    release_sha256 = _hash_bytes(release_payload)
    template_payload = template_path.read_bytes()
    validate_template_bytes(template_payload)
    observer = read_observer_state(paths["strategy_directory"], args.observer_expected_sha256)
    plan_sha256 = _hash_file(plan_path)

    journal = _sqlite_target(args.journal, "JOURNAL")
    client_store, slot_record = resolve_client_store(
        paths["strategy_directory"], args.client_store,
        args.strategy_id if args.strategy_id is not None else None)
    pending = load_pending_state(_safe_local_path(Path(args.pending_state), allow_missing=False))
    _require_pending_provenance(pending, journal is not None)

    blockers = []
    if plan_sha256 != args.plan_expected_sha256:
        blockers.append("PLAN_HASH_MISMATCH")
    if not observer["ok"]:
        blockers.append("OBSERVER_PAIR_MISMATCH")
    if release_sha256 != args.expected_source_sha256:
        blockers.append("RELEASE_HASH_MISMATCH")
    target = paths["strategy_directory"] / TARGET_NAME
    if target.exists():
        target_sha256 = _hash_file(target)
        if args.expected_target_sha256 == "ABSENT":
            blockers.append("TARGET_UNEXPECTEDLY_PRESENT")
    else:
        target_sha256 = "ABSENT"
        if args.expected_target_sha256 != "ABSENT":
            blockers.append("TARGET_MISSING")
    if pending["journal_pending_attempts"] > 0:
        blockers.append("JOURNAL_PENDING_ATTEMPTS_PRESENT")

    plan_record = {
        "schema_version": 1,
        "operation": OPERATION,
        "tool_version": TOOL_VERSION,
        "phase": "plan",
        "created_at": _now_iso(),
        "plan": {"path": PLAN_RELATIVE, "sha256": plan_sha256,
                 "expected_sha256": args.plan_expected_sha256},
        "strategy_directory": str(paths["strategy_directory"]),
        "release": {"directory": str(paths["release_directory"]),
                    "file": str(paths["release_file"]),
                    "sha256": release_sha256,
                    "expected_sha256": args.expected_source_sha256,
                    "adapter_version": adapter_version},
        "target": {"path": str(target), "current_sha256": target_sha256,
                   "expected_sha256": args.expected_target_sha256},
        "observer_pair": observer,
        "params_template": {"path": str(template_path), "sha256": _hash_bytes(template_payload)},
        "journal": {"path": str(journal) if journal is not None else "absent"},
        "client_store": slot_record,
        "pending_state": pending,
        "blockers": blockers,
        "notes": [
            "Plan is read-only for the strategy directory; apply performs backup-then-replace.",
            "apply additionally requires --stopped-evidence (user stopped the execution instance).",
            "native call counters start point is recorded at service start (service-start.json), not at install.",
            "pending-state provenance: running-service status snapshot + client store counts; the /events duplicate view and status ACK watermark are process-local (C3 ruling).",
            "deterministic slot measurement is read-only: the plan never creates the client store; only the client's first valid on_start creates it (RULING 01 R1.1/R1.2).",
        ],
    }
    evidence = _fresh_directory(args.evidence_directory)
    evidence.mkdir(parents=True)
    _atomic_write_new(evidence / "deployment-plan.json", _canonical_json(plan_record))
    return {"status": "planned", "blockers": blockers,
            "plan_file": str(evidence / "deployment-plan.json"),
            "evidence_directory": str(evidence)}


# ---------------------------------------------------------------------------
# apply (12.2 five fixed steps)
# ---------------------------------------------------------------------------

def cmd_apply(args) -> dict:
    repo_root = _resolve_repo_root(args)
    paths = _resolve_paths(args, need_release=True)
    strategy_directory = paths["strategy_directory"]
    target = strategy_directory / TARGET_NAME
    stopped = load_stopped_evidence(_safe_local_path(Path(args.stopped_evidence), allow_missing=False))
    pending = load_pending_state(_safe_local_path(Path(args.pending_state), allow_missing=False))
    install_id = secrets.token_hex(16)

    # Step 1: path safety + plan hash + observer pair precheck.
    plan_sha256 = _hash_file(repo_root / PLAN_RELATIVE)
    _require(plan_sha256 == args.plan_expected_sha256, "PLAN_HASH_MISMATCH")
    observer = read_observer_state(strategy_directory, args.observer_expected_sha256)
    _require(observer["ok"], "OBSERVER_PAIR_MISMATCH")

    # Step 2: fresh evidence directory; backup current execution target and
    # both observer files BEFORE any replacement; verify backups by re-read.
    evidence = _fresh_directory(args.evidence_directory)
    backup_dir = evidence / "backup"
    backup_dir.mkdir(parents=True)
    prior_payload = target.read_bytes() if target.exists() else None
    prior_record: dict = {"sha256": "ABSENT", "backup_path": None, "backup_sha256": None}
    if prior_payload is not None:
        _require(not target.is_symlink() and target.is_file(), "TARGET_INVALID")
        prior_sha256 = _hash_bytes(prior_payload)
        backup_path = backup_dir / f"{TARGET_NAME}.client-backup-{prior_sha256[:8]}.py"
        _atomic_write_new(backup_path, prior_payload)
        _require(_hash_file(backup_path) == prior_sha256, "BACKUP_VERIFY_FAILED")
        prior_record = {"sha256": prior_sha256, "backup_path": str(backup_path),
                        "backup_sha256": _hash_file(backup_path)}
    else:
        _atomic_write_new(backup_dir / f"{TARGET_NAME}.ABSENT.json",
                          _canonical_json({"prior": "ABSENT", "install_id": install_id}))
    observer_backups = {}
    for name in OBSERVER_NAMES:
        payload = (strategy_directory / name).read_bytes()
        digest = _hash_bytes(payload)
        backup_path = backup_dir / f"{name}.client-backup-{digest[:8]}.py"
        _atomic_write_new(backup_path, payload)
        _require(_hash_file(backup_path) == digest, "BACKUP_VERIFY_FAILED")
        observer_backups[name] = {"sha256": digest, "backup_path": str(backup_path),
                                  "sampled_at": _now_iso()}

    # Step 3: consistency backups of journal + client outbox; pending gate.
    journal = _sqlite_target(args.journal, "JOURNAL")
    _require_pending_provenance(pending, journal is not None)
    journal_record = {"path": str(journal) if journal is not None else "absent", "backup": None}
    if journal is not None:
        journal_record["backup"] = backup_sqlite(journal, backup_dir / "journal-consistency.sqlite")
        _require(journal_record["backup"]["integrity_check"] == "ok", "JOURNAL_BACKUP_INTEGRITY_FAILED")
    client_store, slot_record = resolve_client_store(
        strategy_directory, args.client_store,
        args.strategy_id if args.strategy_id is not None else None)
    client_store_record = dict(slot_record)
    if client_store is not None:
        # receipts.db/outbox.db ONLY: pairing.json / identity.json /
        # pairing-request.json are credentials/context, never copied into
        # ordinary evidence (R2.2/R2.3, M08).
        client_store_record.update(backup_client_store(client_store, backup_dir / "client-store"))
    _require(pending["journal_pending_attempts"] == 0,
             "DEPLOY_BLOCKED_PENDING_JOURNAL_ATTEMPTS")
    if client_store is not None:
        unacked = client_store_record.get("files", {}).get("outbox.db", {}).get("unacked_events")
        if unacked is not None:
            _require(pending["client_unacked_events"] == unacked, "PENDING_STATE_UNACKED_MISMATCH")

    # Step 4: verify release, re-verify current target hash, atomic replace.
    release_payload = paths["release_file"].read_bytes()
    adapter_version = validate_release_bytes(release_payload)
    release_sha256 = _hash_bytes(release_payload)
    _require(release_sha256 == args.expected_source_sha256, "RELEASE_HASH_MISMATCH")
    if prior_payload is None:
        _require(args.expected_target_sha256 == "ABSENT", "EXPECTED_TARGET_SHA256_MISMATCH")
        _require(not target.exists(), "TARGET_APPEARED_DURING_INSTALL")
    else:
        _require(args.expected_target_sha256 == prior_record["sha256"],
                 "EXPECTED_TARGET_SHA256_MISMATCH")
        _require(target.is_file() and _hash_file(target) == prior_record["sha256"],
                 "TARGET_CHANGED_DURING_INSTALL")
    template_path = _safe_local_path(Path(args.template) if args.template
                                     else repo_root / TEMPLATE_RELATIVE, allow_missing=False)
    template_payload = template_path.read_bytes()
    validate_template_bytes(template_payload)

    replaced = False
    try:
        _atomic_replace(target, release_payload)
        replaced = True
        _require(_hash_file(target) == release_sha256, "TARGET_HASH_MISMATCH_AFTER_REPLACE")

        # Step 5: final verification + deployment.json publication.
        final_observer = read_observer_state(strategy_directory, args.observer_expected_sha256)
        _require(final_observer["ok"] and final_observer["hashes"] == observer["hashes"],
                 "OBSERVER_PAIR_CHANGED_DURING_INSTALL")
        record = {
            "schema_version": 1,
            "operation": OPERATION,
            "tool_version": TOOL_VERSION,
            "created_at": _now_iso(),
            "install_id": install_id,
            "plan": {"path": PLAN_RELATIVE, "sha256": plan_sha256},
            "installer": {"relative": INSTALLER_RELATIVE,
                          "sha256": _hash_file(Path(__file__).resolve())},
            "strategy_directory": str(strategy_directory),
            "target_relative": TARGET_NAME,
            "target_sha256": release_sha256,
            "source": {"release_file": str(paths["release_file"]),
                       "sha256": release_sha256,
                       "adapter_version": adapter_version},
            "prior_target": prior_record,
            "observer_pair": {"expected_sha256": args.observer_expected_sha256,
                              "hashes": final_observer["hashes"],
                              "identical": final_observer["identical"],
                              "sampled_at": final_observer["sampled_at"]},
            "backups": {"journal": journal_record, "client_store": client_store_record,
                        "observer": observer_backups},
            "pending_state": pending,
            "stopped_evidence": stopped,
            "params_template": {"path": str(template_path),
                                "sha256": _hash_bytes(template_payload)},
            "native_count_start": None,
            "native_count_start_note": ("counters start point is recorded at service start "
                                        "into service-start.json in this evidence directory"),
            "client_reload_state": "CLIENT_RELOAD_PENDING",
            "environment": {"installer_python": f"{os.name}:{sys.version_info.major}."
                                                f"{sys.version_info.minor}.{sys.version_info.micro}",
                            "sdk_version": args.sdk_version,
                            "client_python_version": args.client_python_version},
        }
        _atomic_write_new(evidence / "deployment.json", _canonical_json(record))
    except BaseException:
        if replaced:
            if prior_payload is None:
                if target.exists() and _hash_file(target) == release_sha256:
                    target.unlink()
            elif target.exists() and _hash_file(target) == release_sha256:
                _atomic_replace(target, prior_payload)
        raise
    return {"status": "deployed", "install_id": install_id,
            "target": str(target), "target_sha256": release_sha256,
            "evidence_directory": str(evidence),
            "client_reload_state": "CLIENT_RELOAD_PENDING"}


# ---------------------------------------------------------------------------
# rollback (12.5)
# ---------------------------------------------------------------------------

def cmd_rollback(args) -> dict:
    repo_root = _resolve_repo_root(args)
    strategy_directory = _safe_local_path(Path(args.strategy_directory), allow_missing=False)
    _require(strategy_directory.is_dir(), "STRATEGY_DIRECTORY_NOT_A_DIRECTORY")
    evidence = _safe_local_path(Path(args.evidence_directory), allow_missing=False)
    receipt_path = evidence / "deployment.json"
    receipt = _load_small_json(receipt_path)
    _require(receipt.get("operation") == OPERATION and receipt.get("schema_version") == 1,
             "DEPLOYMENT_RECEIPT_INVALID")
    _require(_INSTALL_ID_HEX.fullmatch(str(receipt.get("install_id", ""))) is not None,
             "DEPLOYMENT_RECEIPT_INVALID")
    _require(receipt.get("strategy_directory") == str(strategy_directory),
             "RECEIPT_NOT_BOUND_TO_STRATEGY_DIRECTORY")
    gate = load_gate_evidence(_safe_local_path(Path(args.gate_evidence), allow_missing=False))

    pending = None
    if args.pending_state is not None:
        pending = load_pending_state(_safe_local_path(Path(args.pending_state), allow_missing=False))
        unresolved = (pending["journal_pending_attempts"] + pending["client_unacked_events"]
                      + pending["real_native_send_attempts"])
        _require(args.pending_disposition is not None or unresolved == 0,
                 "ROLLBACK_REQUIRES_PENDING_DISPOSITION")
    disposition = (load_disposition(_safe_local_path(Path(args.pending_disposition)))
                   if args.pending_disposition is not None else None)

    rollback_dir = _fresh_directory(args.rollback_evidence_directory)
    backup_dir = rollback_dir / "backup"
    backup_dir.mkdir(parents=True)
    target = strategy_directory / TARGET_NAME
    _require(target.is_file() and not target.is_symlink(), "TARGET_MISSING_FOR_ROLLBACK")
    current_payload = target.read_bytes()
    current_sha256 = _hash_bytes(current_payload)
    _atomic_write_new(backup_dir / f"{TARGET_NAME}.failed-{current_sha256[:8]}.py",
                      current_payload)

    # Current facts snapshot (journal + client store) before restoring bytes.
    facts: dict = {}
    if args.journal is not None and args.journal != "absent":
        journal = _sqlite_target(args.journal, "JOURNAL")
        if journal is not None:
            facts["journal"] = backup_sqlite(journal, backup_dir / "journal-at-rollback.sqlite")
    if args.client_store is not None and args.client_store != "absent":
        store = _safe_local_path(Path(args.client_store))
        facts["client_store"] = backup_client_store(store, backup_dir / "client-store-at-rollback")

    # Byte-level rollback against the install receipt.
    _require(current_sha256 == receipt["target_sha256"] or args.accept_target_drift,
             "TARGET_DRIFT_SINCE_INSTALL")
    prior = receipt["prior_target"]
    restored = False
    try:
        if prior["sha256"] == "ABSENT":
            _require(current_sha256 == receipt["target_sha256"],
                     "CANNOT_REMOVE_DRIFTED_TARGET")
            target.unlink()
            restored_state = "REMOVED_WAS_ABSENT"
        else:
            backup_path = _safe_local_path(Path(prior["backup_path"]), allow_missing=False)
            prior_payload = backup_path.read_bytes()
            _require(_hash_bytes(prior_payload) == prior["sha256"], "PRIOR_BACKUP_HASH_MISMATCH")
            _atomic_replace(target, prior_payload)
            _require(_hash_file(target) == prior["sha256"], "RESTORED_TARGET_HASH_MISMATCH")
            restored_state = "RESTORED_PRIOR_BYTES"
        restored = True
        observer = read_observer_state(strategy_directory, args.observer_expected_sha256)
        _require(observer["ok"], "OBSERVER_PAIR_MISMATCH_AFTER_ROLLBACK")
        record = {
            "schema_version": 1,
            "operation": ROLLBACK_OPERATION,
            "tool_version": TOOL_VERSION,
            "created_at": _now_iso(),
            "deployment_receipt": {"path": str(receipt_path),
                                   "install_id": receipt["install_id"],
                                   "target_sha256_at_install": receipt["target_sha256"]},
            "gate_evidence": gate,
            "target": {"path": str(target), "failed_sha256": current_sha256,
                       "restored_sha256": prior["sha256"], "state": restored_state,
                       "drift_accepted": bool(args.accept_target_drift)},
            "observer_pair": {"hashes": observer["hashes"], "identical": observer["identical"],
                              "sampled_at": observer["sampled_at"]},
            "facts_backup": facts,
            "pending_state": pending,
            "pending_disposition": disposition,
            "final_counts": (_load_small_json(Path(args.final_counts))
                             if args.final_counts is not None else None),
            "ledger_policy": "code rollback only; journal, outbox, receipts, authorization "
                             "consumption and audit packages are never deleted (plan 12.5)",
        }
        _atomic_write_new(rollback_dir / "rollback.json", _canonical_json(record))
    except BaseException:
        if restored and prior["sha256"] != "ABSENT" and _hash_file(target) == prior["sha256"]:
            _atomic_replace(target, current_payload)
        raise
    return {"status": "rolled_back", "state": restored_state,
            "rollback_receipt": str(rollback_dir / "rollback.json"),
            "observer_unchanged": True}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _add_common(parser: argparse.ArgumentParser, *, with_release: bool) -> None:
    parser.add_argument("--strategy-directory", type=Path, required=True,
                        help="InfiniTrader pyStrategy/self_strategy directory (explicit, never searched)")
    if with_release:
        parser.add_argument("--release-directory", type=Path, required=True,
                            help="directory holding the reviewed release AKExecutionBridge.py")
    parser.add_argument("--repo-root", type=Path, default=None,
                        help="repository root (defaults to the installer's parent)")


def _add_hash_overrides(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--plan-expected-sha256", default=EXPECTED_PLAN_SHA256,
                        help="frozen sha256 of the plan document (sandbox override)")
    parser.add_argument("--observer-expected-sha256", default=OBSERVER_EXPECTED_SHA256,
                        help="last recorded observer-pair baseline sha256 (sandbox override)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Installer/rollback for the NE1 execution client (plan 12.1/12.2/12.5); "
                    "no network, no UI automation, no trading capability")
    sub = parser.add_subparsers(dest="action", required=True)

    plan = sub.add_parser("plan", help="read-only deployment plan into a fresh evidence directory")
    _add_common(plan, with_release=True)
    plan.add_argument("--evidence-directory", type=Path, required=True)
    plan.add_argument("--strategy-id", type=int, default=None,
                      help="framework strategy_id (non-secret) whose deterministic "
                           "store slot .ak_execution/strategy-<id> is verified "
                           "(runbook production commands always pass it)")
    plan.add_argument("--journal", required=True,
                      help="execution service journal path or literal 'absent'")
    plan.add_argument("--client-store", required=True,
                      help="deployed client store directory (receipts.db/outbox.db) or literal 'absent'")
    plan.add_argument("--pending-state", type=Path, required=True,
                      help="operator JSON: pending attempts/unacked events/real counters "
                           "(source: running-service status snapshot + client store counts)")
    plan.add_argument("--expected-source-sha256", required=True,
                      help="reviewed release hash from the frozen release manifest")
    plan.add_argument("--expected-target-sha256", required=True,
                      help="current target hash, or literal 'ABSENT'")
    plan.add_argument("--template", type=Path, default=None,
                      help="parameter template path (default: <repo>/config/infiny_native_execution.example.json)")
    _add_hash_overrides(plan)
    plan.set_defaults(handler=cmd_plan)

    apply_parser = sub.add_parser("apply", help="backup-then-replace the single execution client file")
    _add_common(apply_parser, with_release=True)
    apply_parser.add_argument("--evidence-directory", type=Path, required=True)
    apply_parser.add_argument("--expected-source-sha256", required=True)
    apply_parser.add_argument("--expected-target-sha256", required=True,
                              help="hex sha256 or literal 'ABSENT'")
    apply_parser.add_argument("--strategy-id", type=int, default=None,
                              help="framework strategy_id (non-secret) whose deterministic "
                                   "store slot .ak_execution/strategy-<id> is verified "
                                   "(runbook production commands always pass it)")
    apply_parser.add_argument("--stopped-evidence", type=Path, required=True,
                              help="JSON: user stopped the execution instance (12.2)")
    apply_parser.add_argument("--journal", required=True)
    apply_parser.add_argument("--client-store", required=True)
    apply_parser.add_argument("--pending-state", type=Path, required=True)
    apply_parser.add_argument("--template", type=Path, default=None)
    apply_parser.add_argument("--sdk-version", default="unrecorded",
                              help="InfiniTrader/PythonGO SDK version recorded verbatim")
    apply_parser.add_argument("--client-python-version", default="unrecorded",
                              help="client embedded Python version recorded verbatim")
    _add_hash_overrides(apply_parser)
    apply_parser.set_defaults(handler=cmd_apply)

    rollback = sub.add_parser("rollback", help="restore prior bytes or remove this install (12.5)")
    _add_common(rollback, with_release=False)
    rollback.add_argument("--evidence-directory", type=Path, required=True,
                          help="the apply-time evidence directory containing deployment.json")
    rollback.add_argument("--rollback-evidence-directory", type=Path, required=True)
    rollback.add_argument("--gate-evidence", type=Path, required=True,
                          help="JSON: user gate closed + service stopped issuing (5.3)")
    rollback.add_argument("--journal", default=None)
    rollback.add_argument("--client-store", default=None)
    rollback.add_argument("--pending-state", type=Path, default=None)
    rollback.add_argument("--pending-disposition", type=Path, default=None,
                          help="required when unresolved records exist at rollback time")
    rollback.add_argument("--final-counts", type=Path, default=None,
                          help="JSON: final real send/cancel counters for the receipt")
    rollback.add_argument("--accept-target-drift", action="store_true",
                          help="explicitly accept that the target changed since install (recorded)")
    _add_hash_overrides(rollback)
    rollback.set_defaults(handler=cmd_rollback)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.handler(args)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "rejected", "error_type": type(exc).__name__,
                          "code": str(exc)}, ensure_ascii=True))
        return 2
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
