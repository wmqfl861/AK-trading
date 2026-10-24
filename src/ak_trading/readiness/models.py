"""Input contracts and state vocabulary for the read-only readiness checker.

Plan identifier: ``COORDINATION-READINESS-REPORT-r1``
(Codex project review report r1, section C6).

The checker consumes four *explicitly passed* JSON inputs:

1. a register snapshot (a mechanical copy of dispatch-register ``R`` entries,
   wrapped in the schema below; the live ``R`` is never read or written),
2. a current-identity table confirmed by Codex / the main session,
3. a gate sequence table confirmed by Codex / the main session,
4. a file/payload reference table.

The checker performs only mechanical comparisons over the structured blocks
documented below. Records whose information exists only in prose strings are
reported as NOT_CHECKABLE; natural-language rulings are never interpreted.

Forbidden capabilities (Codex C6): the module never writes the register, never
modifies frozen packages, never starts processes, never samples, never issues
authorizations, and never changes any gate state. It only reads the four input
files and writes two report files into an explicit output directory.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Mapping

TOOL_NAME = "coordination-readiness-checker"
PLAN_ID = "COORDINATION-READINESS-REPORT-r1"
SCHEMA_VERSION = 1

REGISTER_SNAPSHOT_KIND = "coordination-readiness-register-snapshot"
IDENTITY_TABLE_KIND = "coordination-readiness-identity-table"
GATE_TABLE_KIND = "coordination-readiness-gate-table"
FILE_TABLE_KIND = "coordination-readiness-file-table"

SYNTHETIC_PROVENANCE = "SYNTHETIC"

# --- Record block names (all optional inside a snapshot record) -------------
IDENTITY_REFS_BLOCK = "identity_refs"  # {slot: {"value", "marked_current"}}
GATE_STATES_BLOCK = "gate_states"  # {gate_id: {"status", "declared_at_utc"?}}
PAYLOAD_STATES_BLOCK = "payload_states"  # {payload_id: state} (register side)
PAYLOAD_CLAIMS_BLOCK = "payload_claims"  # {payload_id: claimed state}
FILE_TRANSCRIPTIONS_BLOCK = "file_transcriptions"  # {file_id: {path,sha256,revision}}
TIMESTAMPS_BLOCK = "timestamps"  # {field: value|null}
TIMESTAMP_PRECISION_BLOCK = "timestamp_precision_claims"  # {field: claim}
KNOWN_BLOCKS = frozenset(
    {
        IDENTITY_REFS_BLOCK,
        GATE_STATES_BLOCK,
        PAYLOAD_STATES_BLOCK,
        PAYLOAD_CLAIMS_BLOCK,
        FILE_TRANSCRIPTIONS_BLOCK,
        TIMESTAMPS_BLOCK,
        TIMESTAMP_PRECISION_BLOCK,
    }
)

# --- Gate status vocabulary (conservative; preparation never completes) -----
# A prerequisite counts as completed ONLY if its declared status is in this
# set. Preparation states (PREPARED, AUTH_ISSUED, waiting seats, blocked
# states, ...) are deliberately excluded: authorization issued is not a window
# granted, a prepared waiting seat is not a started measurement.
GATE_COMPLETION_STATUSES = frozenset(
    {
        "CLOSED",
        "PASS",
        "COMPLETED",
        "ACTIVATED",
        "CONTINUATION_PERMITTED",
    }
)
# A successor declaring one of these while a prerequisite is not completed is
# the CHECK-02 contradiction. READY is a trigger for successors but is NOT a
# completion status for prerequisites.
GATE_SUCCESSOR_TRIGGER_STATUSES = frozenset(
    set(GATE_COMPLETION_STATUSES) | {"READY"}
)
GATE_PREPARATION_STATUSES = frozenset(
    {
        "PREPARED",
        "PREPARED_NOT_READY",
        "NOT_READY",
        "NOT_STARTED",
        "BLOCKED_NOT_STARTED",
        "BLOCKED_ADAPTER_DOMAIN_PIN",
        "AUTH_ISSUED",
        "WAITING_SEAT_PREPARED",
        "ISSUED",
        "FROZEN",
        "FROZEN_PENDING_SEAL",
        "REPAIR_REVIEWED_PENDING_P18",
    }
)
GATE_FAILED_STATUSES = frozenset({"FAILED", "INVALID", "REJECT", "HOLD"})

# --- Payload state vocabulary (CHECK-03) -------------------------------------
# Authoritative payload states that mean "frozen only" (not sealed, not
# reviewed). Historical PASS artifacts belonging to retired identities do not
# change this classification.
PAYLOAD_FROZEN_ONLY_STATES = frozenset(
    {
        "FROZEN",
        "FROZEN_UNSEALED",
        "FROZEN_UNSEALED_FAILED_PRESEAL",
        "FROZEN_PENDING_SEAL",
    }
)
# Claims in this set contradict a frozen-only authoritative state.
PAYLOAD_SEALED_FAMILY_CLAIMS = frozenset(
    {
        "SEALED",
        "SEALED_READBACK",
        "REVIEWED",
        "REVIEWED_PASS",
        "PASS",
        "PASS_CURRENT",
        "COMPLETED",
        "CLOSED",
    }
)

# --- Timestamp vocabulary (CHECK-05) -----------------------------------------
PRECISION_CLAIM_VALUES = frozenset({"precise", "placeholder", "range", "unrecorded"})
PLACEHOLDER_TIMESTAMP_VALUES = frozenset(
    {
        "",
        "TBD",
        "TO_BE_DETERMINED",
        "TO_BE_DETERMINED_AT_LAUNCH",
        "TO_BE_DECIDED",
        "TO_BE_FILLED",
        "UNKNOWN",
        "UNRECORDED",
        "NOT_RECORDED",
        "PENDING",
        "RESERVED",
        "PLACEHOLDER",
        "N/A",
        "NA",
        "1970-01-01T00:00:00Z",
        "1970-01-01T00:00:00+00:00",
        "0001-01-01T00:00:00Z",
        "9999-99-99",
    }
)
# Full UTC instant: date + time + explicit timezone (Z or +/-hh:mm).
PRECISE_UTC_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
)
# Date-only values carry day precision, not a precise historical instant.
DATE_ONLY_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Anything with timezone info but missing clock time is also not an instant.
DATETIME_NO_TZ_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$")

SHA256_HEX_PATTERN = re.compile(r"^[0-9a-f]{64}$")
REVISION_STRIP_PATTERN = re.compile(r"[_\-]")


class ReadinessInputError(ValueError):
    """A passed input file violates its documented schema.

    The checker fails fast on malformed inputs instead of guessing; it never
    silently repairs or reinterprets an input.
    """


@dataclass(frozen=True)
class InputFile:
    """One explicitly passed JSON input, with programmatic SHA-256."""

    role: str
    path: Path
    sha256: str
    payload: Mapping[str, Any]

    @property
    def provenance(self) -> str:
        value = self.payload.get("provenance")
        return str(value) if value is not None else ""


@dataclass(frozen=True)
class IdentityTable:
    """Current-vs-retired identities per slot (confirmed by Codex/main)."""

    confirmed_by: str
    slots: Mapping[str, Mapping[str, Any]]

    def slot(self, name: str) -> Mapping[str, Any] | None:
        return self.slots.get(name)

    def is_retired(self, slot: str, identity: str) -> bool:
        entry = self.slots.get(slot)
        if not isinstance(entry, Mapping):
            return False
        retired = entry.get("retired", [])
        return isinstance(retired, list) and identity in retired

    def is_current(self, slot: str, identity: str) -> bool:
        entry = self.slots.get(slot)
        return isinstance(entry, Mapping) and entry.get("current") == identity


@dataclass(frozen=True)
class GateTable:
    """Gate ordering with prerequisites (confirmed by Codex/main)."""

    confirmed_by: str
    gates: Mapping[str, Mapping[str, Any]]

    def prerequisites(self, gate_id: str) -> tuple[str, ...]:
        entry = self.gates.get(gate_id)
        if not isinstance(entry, Mapping):
            return ()
        prereqs = entry.get("prerequisites", [])
        if not isinstance(prereqs, list):
            raise ReadinessInputError(
                f"gate {gate_id!r}: 'prerequisites' must be a list of gate ids"
            )
        return tuple(str(item) for item in prereqs)


@dataclass(frozen=True)
class FileTable:
    """Authoritative file references (path/sha256/revision) and payload states."""

    confirmed_by: str
    files: Mapping[str, Mapping[str, Any]]
    payloads: Mapping[str, Mapping[str, Any]]

    def file(self, file_id: str) -> Mapping[str, Any] | None:
        return self.files.get(file_id)

    def payload(self, payload_id: str) -> Mapping[str, Any] | None:
        return self.payloads.get(payload_id)


@dataclass(frozen=True)
class RegisterSnapshot:
    """Mechanical copy of register entries in the documented wrapper schema."""

    source_register: str
    records: Mapping[str, Any]


@dataclass(frozen=True)
class ReadinessInputs:
    register: InputFile
    identities: InputFile
    gates: InputFile
    files: InputFile

    @property
    def any_synthetic(self) -> bool:
        return any(
            source.provenance == SYNTHETIC_PROVENANCE
            for source in (self.register, self.identities, self.gates, self.files)
        )


@dataclass(frozen=True)
class Finding:
    """One contradiction reported by one of the five checks."""

    check_id: str
    code: str
    severity: str
    record: str
    summary: str
    detail: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Informational:
    """A non-contradiction observation (NOT_CHECKABLE / unresolved counts)."""

    code: str
    record: str
    summary: str
    detail: Mapping[str, Any] = field(default_factory=dict)


# --- Parsing / validation ----------------------------------------------------


def local_absolute_path(value: str | Path, base: Path) -> Path:
    """Anchor a literal local path without touching UNC/device namespaces."""

    text = str(value).replace("\\", "/")
    windows = PureWindowsPath(text)
    if text.startswith("//") or "\x00" in text:
        raise ReadinessInputError(f"cannot establish local path safety (UNC/device/NUL): {value}")
    if windows.drive and (
        len(windows.drive) != 2 or not windows.drive[0].isalpha()
        or not windows.root or os.name != "nt"
    ):
        raise ReadinessInputError(f"cannot establish local path safety (drive anchor): {value}")
    # Reject alternate streams and Windows components with ambiguous trailing
    # dots/spaces. Dot/dot-dot components themselves retain their OS meaning.
    for part in windows.parts[1:] if windows.anchor else windows.parts:
        if (":" in part or (part not in {".", ".."} and part.rstrip(" .") != part)
                or (os.name == "nt" and PureWindowsPath(part).is_reserved())):
            raise ReadinessInputError(f"cannot establish local path safety (ambiguous component): {value}")
    path = Path(text)
    if os.name == "nt" and windows.root and not windows.drive:
        path = Path(base.anchor) / path
    absolute = path if path.is_absolute() else base / path
    if os.name == "nt":
        # A drive-letter path may also name a mapped network drive. Query only
        # the local drive type before issuing any filesystem metadata calls.
        import ctypes

        drive_type = ctypes.windll.kernel32.GetDriveTypeW(str(absolute.anchor))
        if drive_type not in {2, 3, 5, 6}:  # removable, fixed, CD-ROM, RAM disk
            raise ReadinessInputError(f"cannot establish local drive safety: {absolute.anchor}")
    return absolute


def resolve_local_metadata(
    path: Path, *, reject_redirection: bool = False, symbolic: bool = False, depth: int = 0
) -> Path:
    """Resolve only local ancestors using metadata, never referenced contents.

    Inspect links before following them so a local link to UNC cannot initiate
    network I/O. Literal placeholder components cannot exist on Windows and
    remain literal; their already resolved ancestors still participate in the
    protection comparison. This is not a hostile-concurrency contract.
    """

    if depth > 40:
        raise ReadinessInputError(f"cannot establish path safety (link cycle): {path}")
    path = local_absolute_path(path, Path.cwd())
    current = Path(path.anchor)
    parts = path.parts[1:]
    for index, part in enumerate(parts):
        if part == "..":
            current = current.parent
            continue
        current = current / part
        if os.name == "nt" and any(character in part for character in '<>"|?*'):
            if symbolic and "<" in part and ">" in part:
                return Path(os.path.abspath(current.joinpath(*parts[index + 1:])))
            raise ReadinessInputError(f"cannot establish path safety (invalid component): {current}")
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ReadinessInputError(f"cannot establish path safety for {current}: {exc}") from exc
        is_link = stat.S_ISLNK(metadata.st_mode)
        is_reparse = bool(getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)
        if is_link or is_reparse:
            if reject_redirection:
                raise ReadinessInputError(f"output route contains symlink/junction/reparse redirection: {current}")
            try:
                target = os.readlink(current)
            except OSError as exc:
                raise ReadinessInputError(f"cannot resolve local reparse metadata: {current}: {exc}") from exc
            # readlink on a local Windows junction returns an extended local
            # drive spelling. Do not accept extended UNC/device targets.
            if target.startswith("\\\\?\\"):
                suffix = target[4:]
                if len(suffix) >= 3 and suffix[0].isalpha() and suffix[1:3] == ":\\":
                    target = suffix
            destination = local_absolute_path(target, current.parent)
            return resolve_local_metadata(
                destination.joinpath(*parts[index + 1:]),
                reject_redirection=reject_redirection, symbolic=symbolic, depth=depth + 1,
            )
        if index < len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise ReadinessInputError(f"path ancestor is not a directory: {current}")
        try:
            # Canonicalize existing local ancestors (including Windows short
            # names); all reparse components were inspected before this call.
            current = current.resolve(strict=True)
        except OSError as exc:
            raise ReadinessInputError(f"cannot canonicalize local metadata: {current}: {exc}") from exc
    return current


def _load_json(role: str, path: Path) -> InputFile:
    path = local_absolute_path(path, Path.cwd())
    resolved = resolve_local_metadata(path)
    if not resolved.is_file():
        raise ReadinessInputError(f"{role} input is not a file: {path}")
    import hashlib
    import json

    raw = resolved.read_bytes()
    sha256 = hashlib.sha256(raw).hexdigest()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReadinessInputError(f"{role} input {path} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ReadinessInputError(f"{role} input {path} must be a JSON object at top level")
    return InputFile(role=role, path=path, sha256=sha256, payload=payload)


def _require(payload: Mapping[str, Any], key: str, kind: type, role: str, path: Path) -> Any:
    if key not in payload:
        raise ReadinessInputError(f"{role} input {path}: missing required key '{key}'")
    value = payload[key]
    if not isinstance(value, kind):
        raise ReadinessInputError(
            f"{role} input {path}: key '{key}' must be {kind.__name__}, got {type(value).__name__}"
        )
    return value


def parse_register_snapshot(source: InputFile) -> RegisterSnapshot:
    payload = source.payload
    if payload.get("snapshot_kind") != REGISTER_SNAPSHOT_KIND:
        raise ReadinessInputError(
            f"register snapshot {source.path}: 'snapshot_kind' must be "
            f"'{REGISTER_SNAPSHOT_KIND}' (no auto-detection, no guessing)"
        )
    _require(payload, "snapshot_version", int, "register snapshot", source.path)
    records = _require(payload, "records", dict, "register snapshot", source.path)
    source_register = payload.get("source_register", "")
    for key, record in records.items():
        if not isinstance(key, str) or not key:
            raise ReadinessInputError(
                f"register snapshot {source.path}: record keys must be non-empty strings"
            )
        if record is not None and not isinstance(record, (str, dict)):
            raise ReadinessInputError(
                f"register snapshot {source.path}: record '{key}' must be a string "
                f"(prose-only) or an object with the documented blocks"
            )
    return RegisterSnapshot(source_register=str(source_register), records=records)


def parse_identity_table(source: InputFile) -> IdentityTable:
    payload = source.payload
    if payload.get("table_kind") != IDENTITY_TABLE_KIND:
        raise ReadinessInputError(
            f"identity table {source.path}: 'table_kind' must be '{IDENTITY_TABLE_KIND}'"
        )
    _require(payload, "table_version", int, "identity table", source.path)
    slots = _require(payload, "slots", dict, "identity table", source.path)
    confirmed_by = str(payload.get("confirmed_by", ""))
    for slot, entry in slots.items():
        if not isinstance(entry, dict):
            raise ReadinessInputError(
                f"identity table {source.path}: slot '{slot}' must be an object"
            )
        current = entry.get("current")
        if not isinstance(current, str) or not current:
            raise ReadinessInputError(
                f"identity table {source.path}: slot '{slot}' needs a non-empty 'current'"
            )
        retired = entry.get("retired", [])
        if not isinstance(retired, list) or any(not isinstance(item, str) for item in retired):
            raise ReadinessInputError(
                f"identity table {source.path}: slot '{slot}' 'retired' must be a list of strings"
            )
        if current in retired:
            raise ReadinessInputError(
                f"identity table {source.path}: slot '{slot}' lists its current identity "
                f"as retired; fix the table before running the checker"
            )
    return IdentityTable(confirmed_by=confirmed_by, slots=slots)


def parse_gate_table(source: InputFile) -> GateTable:
    payload = source.payload
    if payload.get("table_kind") != GATE_TABLE_KIND:
        raise ReadinessInputError(
            f"gate table {source.path}: 'table_kind' must be '{GATE_TABLE_KIND}'"
        )
    _require(payload, "table_version", int, "gate table", source.path)
    gates = _require(payload, "gates", dict, "gate table", source.path)
    confirmed_by = str(payload.get("confirmed_by", ""))
    for gate_id, entry in gates.items():
        if not isinstance(entry, dict):
            raise ReadinessInputError(
                f"gate table {source.path}: gate '{gate_id}' must be an object"
            )
        _require_key = f"gate table {source.path}: gate '{gate_id}'"
        if "sequence" not in entry or not isinstance(entry["sequence"], int):
            raise ReadinessInputError(f"{_require_key}: 'sequence' must be an integer")
        prerequisites = entry.get("prerequisites", [])
        if not isinstance(prerequisites, list) or any(
            not isinstance(item, str) for item in prerequisites
        ):
            raise ReadinessInputError(
                f"{_require_key}: 'prerequisites' must be a list of gate ids"
            )
        for prerequisite in prerequisites:
            if prerequisite not in gates:
                raise ReadinessInputError(
                    f"{_require_key}: prerequisite '{prerequisite}' is not defined in the gate table"
                )
            if prerequisite == gate_id:
                raise ReadinessInputError(f"{_require_key}: gate lists itself as a prerequisite")
    return GateTable(confirmed_by=confirmed_by, gates=gates)


def parse_file_table(source: InputFile) -> FileTable:
    payload = source.payload
    if payload.get("table_kind") != FILE_TABLE_KIND:
        raise ReadinessInputError(
            f"file table {source.path}: 'table_kind' must be '{FILE_TABLE_KIND}'"
        )
    _require(payload, "table_version", int, "file table", source.path)
    files = _require(payload, "files", dict, "file table", source.path)
    payloads = _require(payload, "payloads", dict, "file table", source.path)
    confirmed_by = str(payload.get("confirmed_by", ""))
    for file_id, entry in files.items():
        if not isinstance(entry, dict):
            raise ReadinessInputError(
                f"file table {source.path}: file '{file_id}' must be an object"
            )
        sha256 = entry.get("sha256")
        if sha256 is not None and (
            not isinstance(sha256, str) or not SHA256_HEX_PATTERN.match(sha256)
        ):
            raise ReadinessInputError(
                f"file table {source.path}: file '{file_id}' sha256 must be 64 lowercase hex "
                f"characters when present (programmatic hashing, no manual digests)"
            )
        for optional_key in ("path", "revision"):
            value = entry.get(optional_key)
            if value is not None and not isinstance(value, str):
                raise ReadinessInputError(
                    f"file table {source.path}: file '{file_id}' '{optional_key}' must be a string"
                )
    for payload_id, entry in payloads.items():
        if not isinstance(entry, dict):
            raise ReadinessInputError(
                f"file table {source.path}: payload '{payload_id}' must be an object"
            )
        state = entry.get("payload_state")
        if not isinstance(state, str) or not state:
            raise ReadinessInputError(
                f"file table {source.path}: payload '{payload_id}' needs a non-empty 'payload_state'"
            )
    return FileTable(confirmed_by=confirmed_by, files=files, payloads=payloads)


def load_inputs(
    register_path: str | Path,
    identity_path: str | Path,
    gate_path: str | Path,
    file_path: str | Path,
) -> ReadinessInputs:
    """Load and validate the four explicitly passed inputs (read-only)."""

    register = _load_json("register-snapshot", Path(register_path))
    identities = _load_json("identity-table", Path(identity_path))
    gates = _load_json("gate-table", Path(gate_path))
    files = _load_json("file-table", Path(file_path))
    parse_register_snapshot(register)
    parse_identity_table(identities)
    parse_gate_table(gates)
    parse_file_table(files)
    return ReadinessInputs(register=register, identities=identities, gates=gates, files=files)


def classify_timestamp(value: Any) -> str:
    """Mechanically classify a timestamp value.

    Returns one of: ``precise_utc``, ``date_only``, ``datetime_no_tz``,
    ``placeholder``, ``non_temporal``, ``missing``.
    """

    if value is None:
        return "missing"
    if not isinstance(value, str):
        return "non_temporal"
    text = value.strip()
    if not text:
        return "missing"
    if text.startswith("<") and text.endswith(">"):
        return "placeholder"
    if text.upper() in PLACEHOLDER_TIMESTAMP_VALUES:
        return "placeholder"
    if PRECISE_UTC_PATTERN.fullmatch(text):
        # fromisoformat validates calendars/clocks but accepts offset minute
        # overflow and truncates fractions after six digits. Validate offsets
        # separately and retain the full fraction for reserved-instant checks.
        zone = "Z" if text.endswith("Z") else text[-6:]
        if zone != "Z" and (int(zone[1:3]) > 23 or int(zone[4:6]) > 59):
            return "non_temporal"
        fraction = PRECISE_UTC_PATTERN.fullmatch(text).group(1) or ""
        try:
            instant = datetime.fromisoformat(text).astimezone(timezone.utc)
        except (ValueError, OverflowError):
            return "non_temporal"
        nonzero_fraction = any(character != "0" for character in fraction.lstrip("."))
        sentinels = (
            datetime(1970, 1, 1, tzinfo=timezone.utc),
            datetime(1, 1, 1, tzinfo=timezone.utc),
        )
        if not nonzero_fraction and instant in sentinels:
            return "placeholder"
        return "precise_utc"
    if DATE_ONLY_PATTERN.fullmatch(text):
        try:
            date.fromisoformat(text)
        except ValueError:
            return "non_temporal"
        return "date_only"
    if DATETIME_NO_TZ_PATTERN.fullmatch(text):
        try:
            datetime.fromisoformat(text)
        except ValueError:
            return "non_temporal"
        return "datetime_no_tz"
    return "non_temporal"
