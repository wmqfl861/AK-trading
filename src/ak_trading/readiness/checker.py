"""Read-only progress and identity consistency checker.

Plan identifier: ``COORDINATION-READINESS-REPORT-r1``
(Codex project review report r1, section C6).

Five mechanical checks over explicitly passed inputs:

* CHK-01  retired identity still marked as current;
* CHK-02  prerequisite not completed while a successor is declared READY/CLOSED;
* CHK-03  payload frozen only, mis-written as sealed/reviewed;
* CHK-04  path / sha256 / revision transcription inconsistencies;
* CHK-05  missing or placeholder UTC treated as a precise historical time.

Forbidden capabilities (never implemented on purpose): writing the register,
modifying frozen packages, starting processes, sampling, issuing
authorizations, changing gate states. Outputs are two report files written to
an explicitly passed output directory: ``state-snapshot.json`` and
``contradictions.md``. The snapshot is a *view*: it normalizes declared states
conservatively and never upgrades a preparation state into a completed one.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import models
from .models import (
    FILE_TRANSCRIPTIONS_BLOCK,
    GATE_COMPLETION_STATUSES,
    GATE_PREPARATION_STATUSES,
    GATE_SUCCESSOR_TRIGGER_STATUSES,
    GATE_STATES_BLOCK,
    IDENTITY_REFS_BLOCK,
    PAYLOAD_CLAIMS_BLOCK,
    PAYLOAD_FROZEN_ONLY_STATES,
    PAYLOAD_SEALED_FAMILY_CLAIMS,
    PAYLOAD_STATES_BLOCK,
    PRECISION_CLAIM_VALUES,
    REVISION_STRIP_PATTERN,
    SHA256_HEX_PATTERN,
    TIMESTAMP_PRECISION_BLOCK,
    TIMESTAMPS_BLOCK,
    FileTable,
    Finding,
    GateTable,
    IdentityTable,
    Informational,
    PLAN_ID,
    SCHEMA_VERSION,
    TOOL_NAME,
    ReadinessInputError,
    ReadinessInputs,
    RegisterSnapshot,
)

CHECK_IDS = ("CHK-01", "CHK-02", "CHK-03", "CHK-04", "CHK-05")
CHECK_TITLES = {
    "CHK-01": "retired identity still marked as current",
    "CHK-02": "prerequisite not completed while successor declared READY/CLOSED",
    "CHK-03": "payload frozen only, mis-written as sealed/reviewed",
    "CHK-04": "path / sha256 / revision transcription inconsistency",
    "CHK-05": "missing or placeholder UTC treated as precise historical time",
}
OUTPUT_SNAPSHOT_NAME = "state-snapshot.json"
OUTPUT_CONTRADICTIONS_NAME = "contradictions.md"

_SNAPSHOT_LIMITATIONS = (
    "Read-only auxiliary report per Codex review report r1 section C6; not a "
    "new hard prerequisite for G1 and not a replacement for any review.",
    "Only the five mechanical checks listed above are performed; "
    "natural-language rulings in the register are never interpreted.",
    "Gate phases are normalized conservatively; preparation states "
    "(PREPARED/AUTH_ISSUED/blocked/waiting-seat) are never upgraded to "
    "completed, and authorization issued is not treated as a window granted.",
    "The snapshot does not write the register, does not modify frozen "
    "packages, and does not change any gate state; dispositions belong to "
    "Codex/the main session.",
)


@dataclass(frozen=True)
class CheckResult:
    findings: tuple[Finding, ...]
    informational: tuple[Informational, ...]


@dataclass(frozen=True)
class ReadinessReport:
    inputs: ReadinessInputs
    findings: tuple[Finding, ...]
    informational: tuple[Informational, ...]
    gate_view: Mapping[str, Any]
    identity_view: Mapping[str, Any]
    payload_view: Mapping[str, Any]
    register_stats: Mapping[str, int]
    generated_at_utc: str

    def findings_for(self, check_id: str) -> tuple[Finding, ...]:
        return tuple(finding for finding in self.findings if finding.check_id == check_id)

    @property
    def totals(self) -> Mapping[str, int]:
        counter: Counter[str] = Counter(finding.check_id for finding in self.findings)
        return {check_id: counter.get(check_id, 0) for check_id in CHECK_IDS}


# --- Record block access -----------------------------------------------------


def _record_object(record: Any) -> Mapping[str, Any] | None:
    if isinstance(record, Mapping):
        return record
    return None


def _block(record_obj: Mapping[str, Any], name: str) -> Mapping[str, Any] | None:
    value = record_obj.get(name)
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ReadinessInputError(
            f"record block '{name}' must be an object, got {type(value).__name__}"
        )
    return value


# --- CHK-01: retired identity still marked current ---------------------------


def check_retired_identities(
    snapshot: RegisterSnapshot, identities: IdentityTable
) -> CheckResult:
    findings: list[Finding] = []
    informational: list[Informational] = []
    for record_key, record in snapshot.records.items():
        record_obj = _record_object(record)
        if record_obj is None:
            continue
        block = _block(record_obj, IDENTITY_REFS_BLOCK)
        if block is None:
            continue
        if not isinstance(block, Mapping):
            raise ReadinessInputError(f"record '{record_key}': identity_refs must be an object")
        for slot, ref in block.items():
            if not isinstance(ref, Mapping):
                raise ReadinessInputError(
                    f"record '{record_key}': identity_refs['{slot}'] must be an object "
                    f"with 'value' and 'marked_current'"
                )
            value = ref.get("value")
            marked_current = ref.get("marked_current")
            if not isinstance(value, str) or not isinstance(marked_current, bool):
                raise ReadinessInputError(
                    f"record '{record_key}': identity_refs['{slot}'] needs string 'value' "
                    f"and boolean 'marked_current'"
                )
            slot_entry = identities.slot(slot)
            if slot_entry is None:
                informational.append(
                    Informational(
                        code="IDENTITY_SLOT_NOT_IN_TABLE",
                        record=record_key,
                        summary=f"identity slot '{slot}' is not covered by the confirmed identity table",
                        detail={"slot": slot, "value": value},
                    )
                )
                continue
            if marked_current and identities.is_retired(slot, value):
                findings.append(
                    Finding(
                        check_id="CHK-01",
                        code="RETIRED_IDENTITY_MARKED_CURRENT",
                        severity="high",
                        record=record_key,
                        summary=(
                            f"record marks retired identity '{value}' of slot '{slot}' as current; "
                            f"current identity is '{identities.slot(slot).get('current')}'"
                        ),
                        detail={
                            "slot": slot,
                            "marked_current_value": value,
                            "table_current": identities.slot(slot).get("current"),
                            "table_retired": list(identities.slot(slot).get("retired", [])),
                        },
                    )
                )
            elif marked_current and not identities.is_current(slot, value):
                informational.append(
                    Informational(
                        code="UNKNOWN_IDENTITY_MARKED_CURRENT",
                        record=record_key,
                        summary=(
                            f"record marks unknown identity '{value}' of slot '{slot}' as current; "
                            f"it is neither the table current nor listed as retired"
                        ),
                        detail={"slot": slot, "value": value},
                    )
                )
    return CheckResult(findings=tuple(findings), informational=tuple(informational))


# --- CHK-02: prerequisite not completed, successor READY/CLOSED ---------------


def _gate_declarations(snapshot: RegisterSnapshot) -> dict[str, list[dict[str, Any]]]:
    declarations: dict[str, list[dict[str, Any]]] = {}
    for record_key, record in snapshot.records.items():
        record_obj = _record_object(record)
        if record_obj is None:
            continue
        block = _block(record_obj, GATE_STATES_BLOCK)
        if block is None:
            continue
        for gate_id, declared in block.items():
            if not isinstance(declared, Mapping) or not isinstance(declared.get("status"), str):
                raise ReadinessInputError(
                    f"record '{record_key}': gate_states['{gate_id}'] must be an object "
                    f"with a string 'status'"
                )
            declarations.setdefault(gate_id, []).append(
                {
                    "record": record_key,
                    "status": declared["status"],
                    "declared_at_utc": declared.get("declared_at_utc"),
                }
            )
    return declarations


def _gate_completed(declarations: Sequence[Mapping[str, Any]]) -> bool:
    """Conservative completion: every declaration must be in the completion set."""

    return bool(declarations) and all(
        str(item["status"]).strip().upper() in GATE_COMPLETION_STATUSES
        for item in declarations
    )


def _gate_phase(statuses: Iterable[str]) -> str:
    normalized = [status.strip().upper() for status in statuses]
    if not normalized:
        return "undeclared"
    if all(status in GATE_COMPLETION_STATUSES for status in normalized):
        return "terminal"
    if all(status in GATE_PREPARATION_STATUSES for status in normalized):
        return "preparation"
    if all(status in {"READY"} | set(GATE_COMPLETION_STATUSES) for status in normalized):
        # READY without any execution evidence stays a declared readiness, not
        # an executed measurement; classify below terminal.
        return "declared_ready"
    if all(status in models.GATE_FAILED_STATUSES for status in normalized):
        return "failed"
    return "mixed"


def build_gate_view(
    snapshot: RegisterSnapshot, gates: GateTable
) -> tuple[Mapping[str, Any], list[Informational]]:
    declarations = _gate_declarations(snapshot)
    view: dict[str, Any] = {}
    informational: list[Informational] = []
    for gate_id, entry in gates.gates.items():
        gate_declarations = declarations.get(gate_id, [])
        statuses = [str(item["status"]) for item in gate_declarations]
        completed = _gate_completed(gate_declarations)
        view[gate_id] = {
            "sequence": entry.get("sequence"),
            "prerequisites": list(entry.get("prerequisites", [])),
            "declarations": gate_declarations,
            "declared_statuses": sorted(set(statuses)),
            "conservative_completed": completed,
            "phase": _gate_phase(statuses),
            "note": (
                "conservative view only; this tool never changes gate state"
            ),
        }
    for gate_id in sorted(set(declarations) - set(gates.gates)):
        informational.append(
            Informational(
                code="GATE_NOT_IN_TABLE",
                record=",".join(
                    item["record"] for item in declarations[gate_id]
                ),
                summary=(
                    f"gate '{gate_id}' is declared in the register snapshot but not defined "
                    f"in the confirmed gate table; no ordering check applied"
                ),
                detail={"gate": gate_id},
            )
        )
    return view, informational


def check_gate_ordering(
    snapshot: RegisterSnapshot, gates: GateTable
) -> tuple[CheckResult, Mapping[str, Any], list[Informational]]:
    declarations = _gate_declarations(snapshot)
    gate_view, view_informational = build_gate_view(snapshot, gates)
    findings: list[Finding] = []
    # Completion per gate, evaluated conservatively across all declarations.
    completed_by_gate = {
        gate_id: _gate_completed(items) for gate_id, items in declarations.items()
    }
    for gate_id, items in declarations.items():
        prerequisites = gates.prerequisites(gate_id)
        if not prerequisites:
            continue
        uncompleted = [
            prerequisite
            for prerequisite in prerequisites
            if not completed_by_gate.get(prerequisite, False)
        ]
        if not uncompleted:
            continue
        for declaration in items:
            status = str(declaration["status"]).strip().upper()
            if status in GATE_SUCCESSOR_TRIGGER_STATUSES:
                findings.append(
                    Finding(
                        check_id="CHK-02",
                        code="SUCCESSOR_READY_WITH_INCOMPLETE_PREREQUISITE",
                        severity="high",
                        record=declaration["record"],
                        summary=(
                            f"record declares gate '{gate_id}' as {declaration['status']} while "
                            f"prerequisite(s) {uncompleted} are not completed "
                            f"(authorization issued or prepared is not completed)"
                        ),
                        detail={
                            "gate": gate_id,
                            "declared_status": declaration["status"],
                            "uncompleted_prerequisites": [
                                {
                                    "gate": prerequisite,
                                    "declared_statuses": sorted(
                                        {
                                            str(item["status"])
                                            for item in declarations.get(prerequisite, [])
                                        }
                                    ),
                                }
                                for prerequisite in uncompleted
                            ],
                        },
                    )
                )
    return (
        CheckResult(findings=tuple(findings), informational=tuple(view_informational)),
        gate_view,
        view_informational,
    )


# --- CHK-03: frozen payload mis-written as sealed/reviewed --------------------


def check_payload_states(snapshot: RegisterSnapshot, files: FileTable) -> CheckResult:
    findings: list[Finding] = []
    informational: list[Informational] = []
    claims: list[tuple[str, str, str, str]] = []  # record, payload_id, claimed, block
    for record_key, record in snapshot.records.items():
        record_obj = _record_object(record)
        if record_obj is None:
            continue
        for block_name in (PAYLOAD_STATES_BLOCK, PAYLOAD_CLAIMS_BLOCK):
            block = _block(record_obj, block_name)
            if block is None:
                continue
            for payload_id, claimed in block.items():
                if not isinstance(claimed, str):
                    raise ReadinessInputError(
                        f"record '{record_key}': {block_name}['{payload_id}'] must be a string state"
                    )
                claims.append((record_key, payload_id, claimed, block_name))
    for record_key, payload_id, claimed, block_name in claims:
        payload_entry = files.payload(payload_id)
        if payload_entry is None:
            informational.append(
                Informational(
                    code="PAYLOAD_NOT_IN_TABLE",
                    record=record_key,
                    summary=(
                        f"payload '{payload_id}' referenced by {block_name} has no authoritative "
                        f"entry in the file table; no state check applied"
                    ),
                    detail={"payload": payload_id, "claimed": claimed},
                )
            )
            continue
        authoritative = str(payload_entry.get("payload_state", ""))
        if (
            authoritative in PAYLOAD_FROZEN_ONLY_STATES
            and claimed.strip().upper() in PAYLOAD_SEALED_FAMILY_CLAIMS
        ):
            findings.append(
                Finding(
                    check_id="CHK-03",
                    code="FROZEN_WRITTEN_AS_SEALED",
                    severity="high",
                    record=record_key,
                    summary=(
                        f"record writes payload '{payload_id}' as '{claimed}' while the "
                        f"authoritative state is '{authoritative}' (frozen only; a failed or "
                        f"unsealed candidate must not be back-filled as sealed/reviewed)"
                    ),
                    detail={
                        "payload": payload_id,
                        "authoritative_state": authoritative,
                        "claimed_state": claimed,
                        "source_block": block_name,
                    },
                )
            )
    return CheckResult(findings=tuple(findings), informational=tuple(informational))


# --- CHK-04: path / sha256 / revision transcription ---------------------------


def _path_transcription(path: str) -> str:
    """Lexical contract only: trim edges, translate slashes, retain all else."""

    return path.strip().replace("\\", "/")


def _path_segments(path: str) -> list[str]:
    # Diagnostic hint only; even here preserve roots and empty components.
    return _path_transcription(path).split("/")


def _runs_vs_rounds_note(expected: str, actual: str) -> str:
    expected_segments = _path_segments(expected)
    actual_segments = _path_segments(actual)
    if (
        len(expected_segments) == len(actual_segments)
        and expected_segments[:1] == ["rounds"]
        and actual_segments[:1] == ["runs"]
        and expected_segments[1:] == actual_segments[1:]
    ):
        return "authoritative layout uses 'rounds/', transcription uses 'runs/' (PG-06 class)"
    return ""


def check_file_transcriptions(snapshot: RegisterSnapshot, files: FileTable) -> CheckResult:
    findings: list[Finding] = []
    informational: list[Informational] = []
    for record_key, record in snapshot.records.items():
        record_obj = _record_object(record)
        if record_obj is None:
            continue
        block = _block(record_obj, FILE_TRANSCRIPTIONS_BLOCK)
        if block is None:
            continue
        for file_id, transcription in block.items():
            if not isinstance(transcription, Mapping):
                raise ReadinessInputError(
                    f"record '{record_key}': file_transcriptions['{file_id}'] must be an object"
                )
            entry = files.file(file_id)
            if entry is None:
                informational.append(
                    Informational(
                        code="FILE_NOT_IN_TABLE",
                        record=record_key,
                        summary=f"transcribed file '{file_id}' has no authoritative entry in the file table; transcription is unverifiable",
                        detail={"file": file_id, "transcription": dict(transcription)},
                    )
                )
                entry = {}
            _compare_path(findings, record_key, file_id, entry, transcription)
            _compare_sha256(findings, record_key, file_id, entry, transcription)
            _compare_revision(findings, record_key, file_id, entry, transcription)
    return CheckResult(findings=tuple(findings), informational=tuple(informational))


def _text_comparison_ready(
    findings: list[Finding], record_key: str, file_id: str, field: str,
    entry: Mapping[str, Any], transcription: Mapping[str, Any],
) -> bool:
    if field not in transcription:
        return False
    actual = transcription[field]
    if not isinstance(actual, str) or not actual.strip():
        findings.append(
            Finding(
                check_id="CHK-04", code=f"{field.upper()}_TRANSCRIPTION_MALFORMED",
                severity="medium", record=record_key,
                summary=f"{field} for '{file_id}' must be a nonblank string; the supplied field is invalid",
                detail={"file": file_id, "field": field, "actual": actual},
            )
        )
        return False
    expected = entry.get(field)
    if not isinstance(expected, str) or not expected.strip():
        findings.append(
            Finding(
                check_id="CHK-04", code=f"{field.upper()}_TRANSCRIPTION_UNVERIFIABLE",
                severity="medium", record=record_key,
                summary=f"{field} for '{file_id}' is unverifiable: no usable authoritative comparator",
                detail={"file": file_id, "field": field, "actual": actual,
                        "expected": expected, "authority_field_present": field in entry},
            )
        )
        return False
    return True


def _compare_path(
    findings: list[Finding], record_key: str, file_id: str, entry: Mapping[str, Any], transcription: Mapping[str, Any]
) -> None:
    if not _text_comparison_ready(findings, record_key, file_id, "path", entry, transcription):
        return
    actual = transcription["path"]
    expected = entry["path"]
    actual_normalized = _path_transcription(actual)
    expected_normalized = _path_transcription(expected)
    if actual_normalized != expected_normalized:
        findings.append(
            Finding(
                check_id="CHK-04",
                code="PATH_TRANSCRIPTION_MISMATCH",
                severity="medium",
                record=record_key,
                summary=(
                    f"path for '{file_id}' transcribed as '{actual}' but authoritative path is "
                    f"'{expected}'{_runs_vs_rounds_note(str(expected), actual) and '; ' + _runs_vs_rounds_note(str(expected), actual)}"
                ),
                detail={
                    "file": file_id,
                    "field": "path",
                    "expected": expected,
                    "actual": actual,
                    "note": _runs_vs_rounds_note(str(expected), actual),
                },
            )
        )


def _compare_sha256(
    findings: list[Finding], record_key: str, file_id: str, entry: Mapping[str, Any], transcription: Mapping[str, Any]
) -> None:
    if "sha256" not in transcription:
        return
    actual = transcription.get("sha256")
    expected = entry.get("sha256")
    if not isinstance(actual, str):
        findings.append(
            Finding(
                check_id="CHK-04",
                code="SHA256_TRANSCRIPTION_MALFORMED",
                severity="medium",
                record=record_key,
                summary=f"sha256 for '{file_id}' is transcribed as a non-string value",
                detail={"file": file_id, "field": "sha256", "actual": actual},
            )
        )
        return
    if not SHA256_HEX_PATTERN.match(actual):
        findings.append(
            Finding(
                check_id="CHK-04",
                code="SHA256_TRANSCRIPTION_MALFORMED",
                severity="medium",
                record=record_key,
                summary=(
                    f"sha256 for '{file_id}' is not 64 lowercase hex characters: '{actual}'"
                ),
                detail={"file": file_id, "field": "sha256", "actual": actual},
            )
        )
        return
    if not _text_comparison_ready(findings, record_key, file_id, "sha256", entry, transcription):
        return
    if actual != expected:
        findings.append(
            Finding(
                check_id="CHK-04",
                code="SHA256_TRANSCRIPTION_MISMATCH",
                severity="high",
                record=record_key,
                summary=f"sha256 for '{file_id}' transcribed as '{actual}' but authoritative digest is '{expected}'",
                detail={
                    "file": file_id,
                    "field": "sha256",
                    "expected": expected,
                    "actual": actual,
                },
            )
        )


def _compare_revision(
    findings: list[Finding], record_key: str, file_id: str, entry: Mapping[str, Any], transcription: Mapping[str, Any]
) -> None:
    if not _text_comparison_ready(findings, record_key, file_id, "revision", entry, transcription):
        return
    actual = transcription["revision"]
    expected = entry["revision"]
    if actual.strip() == expected.strip():
        return
    trap_note = ""
    if (
        REVISION_STRIP_PATTERN.sub("", actual.strip())
        == REVISION_STRIP_PATTERN.sub("", str(expected).strip())
    ):
        trap_note = (
            f"difference is separator-only (r10 vs r_10 lexicographic trap class); "
            f"authoritative revision is '{expected}'"
        )
    findings.append(
        Finding(
            check_id="CHK-04",
            code="REVISION_TRANSCRIPTION_MISMATCH",
            severity="medium",
            record=record_key,
            summary=(
                f"revision for '{file_id}' transcribed as '{actual}' but authoritative revision "
                f"is '{expected}'" + (f"; {trap_note}" if trap_note else "")
            ),
            detail={
                "file": file_id,
                "field": "revision",
                "expected": expected,
                "actual": actual,
                "note": trap_note,
            },
        )
    )


# --- CHK-05: placeholder UTC claimed precise ---------------------------------


def check_timestamps(snapshot: RegisterSnapshot) -> CheckResult:
    findings: list[Finding] = []
    informational: list[Informational] = []
    for record_key, record in snapshot.records.items():
        record_obj = _record_object(record)
        if record_obj is None:
            continue
        timestamps = _block(record_obj, TIMESTAMPS_BLOCK)
        precision_claims = _block(record_obj, TIMESTAMP_PRECISION_BLOCK)
        if timestamps is None and precision_claims is None:
            continue
        values: Mapping[str, Any] = timestamps or {}
        claims: Mapping[str, Any] = precision_claims or {}
        for field_name, claim in claims.items():
            if claim not in PRECISION_CLAIM_VALUES:
                raise ReadinessInputError(
                    f"record '{record_key}': timestamp_precision_claims['{field_name}'] must be "
                    f"one of {sorted(PRECISION_CLAIM_VALUES)}"
                )
            if claim != "precise":
                continue
            value = values.get(field_name)
            classification = models.classify_timestamp(value)
            if classification == "precise_utc":
                continue
            if classification == "missing":
                summary = (
                    f"record claims timestamp '{field_name}' is precise, but the value is missing/null"
                )
                code = "MISSING_UTC_CLAIMED_PRECISE"
            elif classification == "placeholder":
                summary = (
                    f"record claims timestamp '{field_name}' is precise, but the value is the "
                    f"placeholder '{value}' (reserved placeholders must stay placeholders; "
                    f"they are not historical facts)"
                )
                code = "PLACEHOLDER_UTC_CLAIMED_PRECISE"
            elif classification == "date_only":
                summary = (
                    f"record claims timestamp '{field_name}' is precise, but '{value}' carries "
                    f"day precision only (not a historical instant)"
                )
                code = "DATE_ONLY_CLAIMED_PRECISE"
            elif classification == "datetime_no_tz":
                summary = (
                    f"record claims timestamp '{field_name}' is precise, but '{value}' has no "
                    f"explicit timezone (not a precise UTC instant)"
                )
                code = "NAIVE_DATETIME_CLAIMED_PRECISE"
            else:
                summary = (
                    f"record claims timestamp '{field_name}' is precise, but '{value}' is not a "
                    f"parseable UTC timestamp"
                )
                code = "NON_TEMPORAL_CLAIMED_PRECISE"
            findings.append(
                Finding(
                    check_id="CHK-05",
                    code=code,
                    severity="medium",
                    record=record_key,
                    summary=summary,
                    detail={
                        "field": field_name,
                        "value": value,
                        "classification": classification,
                        "claim": claim,
                    },
                )
            )
        # Informational: legitimate placeholders kept as placeholders.
        for field_name, value in values.items():
            classification = models.classify_timestamp(value)
            if classification == "placeholder" and claims.get(field_name) != "precise":
                informational.append(
                    Informational(
                        code="PLACEHOLDER_TIMESTAMP_KEPT",
                        record=record_key,
                        summary=(
                            f"timestamp '{field_name}' is a reserved placeholder and is not "
                            f"claimed precise; kept as placeholder (no contradiction)"
                        ),
                        detail={"field": field_name, "value": value},
                    )
                )
    return CheckResult(findings=tuple(findings), informational=tuple(informational))


# --- Identity / payload views ------------------------------------------------


def build_identity_view(
    snapshot: RegisterSnapshot, identities: IdentityTable
) -> Mapping[str, Any]:
    references: dict[str, list[Mapping[str, Any]]] = {}
    for record_key, record in snapshot.records.items():
        record_obj = _record_object(record)
        if record_obj is None:
            continue
        block = _block(record_obj, IDENTITY_REFS_BLOCK)
        if block is None:
            continue
        for slot, ref in block.items():
            references.setdefault(str(slot), []).append(
                {
                    "record": record_key,
                    "value": ref.get("value"),
                    "marked_current": ref.get("marked_current"),
                }
            )
    slots_view: dict[str, Any] = {}
    for slot, entry in identities.slots.items():
        slots_view[slot] = {
            "current": entry.get("current"),
            "retired": list(entry.get("retired", [])),
            "register_references": references.get(slot, []),
        }
    return {
        "confirmed_by": identities.confirmed_by,
        "slots": slots_view,
        "slots_referenced_but_not_in_table": sorted(set(references) - set(identities.slots)),
    }


def build_payload_view(snapshot: RegisterSnapshot, files: FileTable) -> Mapping[str, Any]:
    register_mentions: dict[str, list[Mapping[str, Any]]] = {}
    for record_key, record in snapshot.records.items():
        record_obj = _record_object(record)
        if record_obj is None:
            continue
        for block_name in (PAYLOAD_STATES_BLOCK, PAYLOAD_CLAIMS_BLOCK):
            block = _block(record_obj, block_name)
            if block is None:
                continue
            for payload_id, claimed in block.items():
                register_mentions.setdefault(payload_id, []).append(
                    {
                        "record": record_key,
                        "source_block": block_name,
                        "state": claimed,
                    }
                )
    payloads_view: dict[str, Any] = {}
    for payload_id, entry in files.payloads.items():
        payloads_view[payload_id] = {
            "authoritative_state": entry.get("payload_state"),
            "files": list(entry.get("files", [])),
            "register_mentions": register_mentions.get(payload_id, []),
        }
    return {
        "confirmed_by": files.confirmed_by,
        "payloads": payloads_view,
        "payloads_referenced_but_not_in_table": sorted(set(register_mentions) - set(files.payloads)),
    }


# --- Orchestration -----------------------------------------------------------


def run_checks(inputs: ReadinessInputs) -> ReadinessReport:
    snapshot = models.parse_register_snapshot(inputs.register)
    identities = models.parse_identity_table(inputs.identities)
    gates = models.parse_gate_table(inputs.gates)
    files = models.parse_file_table(inputs.files)

    chk01 = check_retired_identities(snapshot, identities)
    chk02, gate_view, gate_informational = check_gate_ordering(snapshot, gates)
    chk03 = check_payload_states(snapshot, files)
    chk04 = check_file_transcriptions(snapshot, files)
    chk05 = check_timestamps(snapshot)

    findings = tuple(
        finding
        for result in (chk01, chk02, chk03, chk04, chk05)
        for finding in result.findings
    )
    informational = tuple(
        item
        for result in (chk01, chk02, chk03, chk04, chk05)
        for item in result.informational
    ) + tuple(gate_informational)
    identity_view = build_identity_view(snapshot, identities)
    payload_view = build_payload_view(snapshot, files)
    register_stats = {
        "total_records": len(snapshot.records),
        "prose_only_records": sum(
            1 for record in snapshot.records.values() if not isinstance(record, Mapping)
        ),
        "records_with_structured_blocks": sum(
            1
            for record in snapshot.records.values()
            if isinstance(record, Mapping)
            and any(name in record for name in models.KNOWN_BLOCKS)
        ),
    }
    generated_at_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return ReadinessReport(
        inputs=inputs,
        findings=findings,
        informational=informational,
        gate_view=gate_view,
        identity_view=identity_view,
        payload_view=payload_view,
        register_stats=register_stats,
        generated_at_utc=generated_at_utc,
    )


# --- Output rendering --------------------------------------------------------


def _input_descriptor(source: Any) -> Mapping[str, Any]:
    return {
        "path": str(source.path),
        "sha256": source.sha256,
        "provenance": source.provenance,
    }


def render_state_snapshot(report: ReadinessReport) -> Mapping[str, Any]:
    snapshot = {
        "tool": TOOL_NAME,
        "plan_id": PLAN_ID,
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": report.generated_at_utc,
        "inputs": {
            "register_snapshot": _input_descriptor(report.inputs.register),
            "identity_table": _input_descriptor(report.inputs.identities),
            "gate_table": _input_descriptor(report.inputs.gates),
            "file_table": _input_descriptor(report.inputs.files),
        },
        "synthetic_inputs": report.inputs.any_synthetic,
        "register_stats": dict(report.register_stats),
        "identity_view": dict(report.identity_view),
        "gate_view": dict(report.gate_view),
        "payload_view": dict(report.payload_view),
        "check_summary": {
            check_id: {
                "title": CHECK_TITLES[check_id],
                "findings": report.totals[check_id],
            }
            for check_id in CHECK_IDS
        },
        "total_findings": len(report.findings),
        "informational_count": len(report.informational),
        "limitations": list(_SNAPSHOT_LIMITATIONS),
    }
    return snapshot


def _md_escape(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_contradictions_markdown(report: ReadinessReport) -> str:
    lines: list[str] = []
    lines.append("# Coordination readiness contradictions report")
    lines.append("")
    lines.append(f"- Plan: `{PLAN_ID}` (Codex project review report r1, section C6)")
    lines.append(f"- Generated at (tool run time, real): `{report.generated_at_utc}`")
    synthetic = "yes" if report.inputs.any_synthetic else "no"
    lines.append(f"- SYNTHETIC inputs: {synthetic}")
    lines.append("")
    lines.append("**Scope disclaimer:** read-only auxiliary report. This tool does not write")
    lines.append("the register, does not modify frozen packages, does not start processes,")
    lines.append("does not sample, does not issue authorizations, and does not change any")
    lines.append("gate state. It is not a hard prerequisite for G1 and does not replace any")
    lines.append("review. Findings are transcription/consistency observations awaiting")
    lines.append("disposition by Codex / the main session. Natural-language rulings are")
    lines.append("never interpreted; only the structured blocks of the passed inputs are read.")
    lines.append("")
    lines.append("## Inputs (SHA-256 computed programmatically at read time)")
    lines.append("")
    lines.append("| role | path | sha256 | provenance |")
    lines.append("|---|---|---|---|")
    for role, source in (
        ("register-snapshot", report.inputs.register),
        ("identity-table", report.inputs.identities),
        ("gate-table", report.inputs.gates),
        ("file-table", report.inputs.files),
    ):
        lines.append(
            f"| {role} | `{_md_escape(source.path)}` | `{source.sha256}` | "
            f"{_md_escape(source.provenance or 'unlabeled')} |"
        )
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("| check | description | findings |")
    lines.append("|---|---|---|")
    for check_id in CHECK_IDS:
        lines.append(f"| {check_id} | {CHECK_TITLES[check_id]} | {report.totals[check_id]} |")
    lines.append(f"| **total** | | **{len(report.findings)}** |")
    lines.append("")
    for check_id in CHECK_IDS:
        lines.append(f"## {check_id} — {CHECK_TITLES[check_id]}")
        lines.append("")
        check_findings = report.findings_for(check_id)
        if not check_findings:
            lines.append("No contradictions found by this check.")
            lines.append("")
            continue
        for index, finding in enumerate(check_findings, start=1):
            lines.append(
                f"### {check_id}-{index:03d} `{finding.code}` (severity: {finding.severity})"
            )
            lines.append("")
            lines.append(f"- record: `{_md_escape(finding.record)}`")
            lines.append(f"- {finding.summary}")
            if finding.detail:
                lines.append("- detail:")
                for key, value in finding.detail.items():
                    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
                    lines.append(f"  - `{key}`: `{_md_escape(rendered)}`")
            lines.append("")
    lines.append("## Informational (not contradictions)")
    lines.append("")
    if not report.informational:
        lines.append("None.")
        lines.append("")
    else:
        for item in report.informational:
            lines.append(f"- `{item.code}` — record `{_md_escape(item.record)}`: {item.summary}")
        lines.append("")
    lines.append("## Limitations")
    lines.append("")
    for limitation in _SNAPSHOT_LIMITATIONS:
        lines.append(f"- {limitation}")
    lines.append("")
    return "\n".join(lines)


def _physical_key(path: Path) -> str:
    # Deliberately separate from the CHK-04 lexical transcription contract.
    return os.path.normcase(os.path.abspath(path))


def _protected_report_paths(report: ReadinessReport) -> list[tuple[str, Path]]:
    inputs = report.inputs
    references: list[tuple[str, str | Path, Path]] = [
        (source.role, source.path, Path.cwd())
        for source in (inputs.register, inputs.identities, inputs.gates, inputs.files)
    ]
    register = inputs.register.payload
    if "source_register" in register:
        value = register["source_register"]
        if not isinstance(value, str):
            raise ReadinessInputError("cannot protect non-string source_register reference")
        if value:
            references.append(("source_register", value, inputs.register.path.parent))
    for file_id, entry in inputs.files.payload["files"].items():
        if "path" in entry:
            value = entry["path"]
            if not isinstance(value, str) or not value.strip():
                raise ReadinessInputError(f"cannot protect unusable file-table path for '{file_id}'")
            references.append((f"file-table:{file_id}", value, inputs.files.path.parent))
    protected: list[tuple[str, Path]] = []
    for label, value, base in references:
        absolute = models.local_absolute_path(value, base)
        protected.append((label, absolute))
        protected.append((label, models.resolve_local_metadata(absolute, symbolic=True)))
    return protected


def _prevalidate_report_targets(report: ReadinessReport, output_dir: str | Path) -> tuple[Path, Path, Path]:
    """Validate both targets and every protected identity before any write."""

    output = models.local_absolute_path(output_dir, Path.cwd())
    resolved_output = models.resolve_local_metadata(output, reject_redirection=True)
    try:
        if output.exists() and not output.is_dir():
            raise ReadinessInputError(f"output path exists and is not a directory: {output}")
        targets = (output / OUTPUT_SNAPSHOT_NAME, output / OUTPUT_CONTRADICTIONS_NAME)
        resolved_targets = []
        for target in targets:
            resolved = models.resolve_local_metadata(target, reject_redirection=True)
            # lexists includes dangling links; neither report may be replaced.
            if os.path.lexists(target):
                raise ReadinessInputError(f"report target must be absent: {target}")
            resolved_targets.append(resolved)
        protected = _protected_report_paths(report)
        for target, resolved in zip(targets, resolved_targets):
            for label, reference in protected:
                reference_key = _physical_key(reference)
                for candidate in (target, resolved):
                    candidate_key = _physical_key(candidate)
                    # Also protects referenced directories (even absent ones)
                    # and prevents mkdir from replacing a named source path.
                    if candidate_key == reference_key or candidate_key.startswith(reference_key.rstrip(os.sep) + os.sep):
                        raise ReadinessInputError(f"report target collides with protected {label}: {target}")
                # Targets are absent by policy, but preserve the explicit
                # same-file guard if identity changes during metadata checks.
                if os.path.exists(resolved) and os.path.exists(reference) and os.path.samefile(resolved, reference):
                    raise ReadinessInputError(f"report target aliases protected {label}: {target}")
        if _physical_key(targets[0]) == _physical_key(targets[1]):
            raise ReadinessInputError("report destinations collide")
    except OSError as exc:
        raise ReadinessInputError(f"cannot establish report destination safety: {exc}") from exc
    return resolved_output, resolved_targets[0], resolved_targets[1]


def write_report(report: ReadinessReport, output_dir: str | Path) -> tuple[Path, Path]:
    """Write two new reports after a joint, read-only destination check.

    Existing regular output directories are allowed; existing targets and
    output-route redirection are rejected. This does not promise protection
    against hostile concurrent filesystem changes.
    """

    output, snapshot_path, contradictions_path = _prevalidate_report_targets(report, output_dir)
    snapshot_text = json.dumps(render_state_snapshot(report), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    contradictions_text = render_contradictions_markdown(report)
    output.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(snapshot_text, encoding="utf-8")
    contradictions_path.write_text(contradictions_text, encoding="utf-8")
    return snapshot_path, contradictions_path


# --- CLI ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ak_trading.readiness.checker",
        description=(
            "Read-only progress/identity consistency checker "
            f"({PLAN_ID}). Reads four JSON inputs, writes "
            f"{OUTPUT_SNAPSHOT_NAME} + {OUTPUT_CONTRADICTIONS_NAME}. Never writes the register."
        ),
    )
    parser.add_argument("--register", required=True, help="register snapshot JSON path")
    parser.add_argument("--identities", required=True, help="confirmed identity table JSON path")
    parser.add_argument("--gates", required=True, help="confirmed gate table JSON path")
    parser.add_argument("--files", required=True, help="file/payload reference table JSON path")
    parser.add_argument("--out-dir", required=True, help="directory for the two report files")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        inputs = models.load_inputs(args.register, args.identities, args.gates, args.files)
        report = run_checks(inputs)
        snapshot_path, contradictions_path = write_report(report, args.out_dir)
    except ReadinessInputError as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return 2
    totals = report.totals
    print(f"findings: " + ", ".join(f"{key}={totals[key]}" for key in CHECK_IDS))
    print(f"total findings: {len(report.findings)}")
    print(f"snapshot: {snapshot_path}")
    print(f"contradictions: {contradictions_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
