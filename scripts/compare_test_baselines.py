"""Record and compare offline test baselines (K03, r4 plan).

This tool consumes already-terminal captures produced by the existing offline
launcher (``scripts/test_offline.py``) under the existing supervisor stack
(``measure.py`` + ``job_guarded2.py``). It is a parser and comparator over
sealed records; it never starts, restarts or supervises test runs, and it
never rewrites a captured native exit code.

Usage::

    python scripts/compare_test_baselines.py record \
        --capture <offline-capture-set-v1-descriptor.json> \
        --output <new-test-baseline-v1.json>

    python scripts/compare_test_baselines.py compare \
        --baseline <test-baseline-v1.json> \
        (--candidate <test-baseline-v1.json> | --candidate-capture <descriptor.json>) \
        [--output <new-test-baseline-diff-v1.json>]

Exit codes
    record   0 = baseline written; 2 = invalid/incomplete capture set.
    compare  0 = complete comparable records, no added failed/error/
                 collection-error identities (this does NOT convert a native
                 pytest exit 1 into a passing suite);
             1 = added failures, errors or collection errors;
             2 = invalid, incomplete or incomparable input.

Descriptor contract (``offline-capture-set-v1``)
    ``schema_version``, ``scope`` (``full-suite-windows`` or
    ``historical-forward``) and ``members``. Every member references one
    capture directory with ``request``/``result``/``stdout``/``stderr``
    ArtifactRefs (all in the same directory, whose name must equal
    ``capture_id``), a ``source`` (root/revision/snapshot/import_evidence),
    an ``environment`` fingerprint ArtifactRef and a ``harness`` set
    (launcher/offline_guard/measure/job_guard). ``ArtifactRef`` is
    ``{"path": <absolute>, "sha256": <hex>}`` and is verified byte-exactly.
    Streams may set ``raw: null`` only with an independent
    ``raw_content_sha256`` of the uncompressed content; when both forms
    exist both hashes and decompressed equality are verified. A
    ``full-suite-windows`` set contains exactly the member kinds
    ``full_suite``, ``duplicate_probe``, ``confirmation_probe`` and
    ``range_probe``; a ``historical-forward`` set contains exactly one
    member of kind ``historical-forward``. Members of one set must share
    source bytes, environment identity and harness identities, and their
    selected nodeids must be globally disjoint.

Design decisions worth knowing when reviewing
    * Comparisons are set-based on nodeid identity, never on counts: a
      same-count/different-node change is a regression (exit 1).
    * A candidate must cover the baseline population per member kind:
      missing selected nodeids, files or domains are population loss
      (exit 2), never "resolved" failures.
    * Baseline JSON files are self-contained for comparison; ``compare``
      re-validates their structure and count equations instead of
      re-reading capture files.
    * Environment identity is the digest of the fingerprint content
      (python + normalized executable + complete package list); comparing
      records with different environments - for example a historical
      minimal-environment capture against the full environment - is
      input error (exit 2).
    * Candidate source changes are expected and recorded as findings;
      harness changes across baseline/candidate are reported as findings
      but do not by themselves make the comparison incomparable.
    * Output files are written once: an existing target path is refused,
      and writing serializes + re-validates the full payload before an
      atomic replace of a temporary sibling.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

DESCRIPTOR_SCHEMA = "offline-capture-set-v1"
BASELINE_SCHEMA = "test-baseline-v1"
DIFF_SCHEMA = "test-baseline-diff-v1"
IMPORT_EVIDENCE_SCHEMA = "import-evidence-v1"

SCOPE_FULL = "full-suite-windows"
SCOPE_HISTORICAL = "historical-forward"
SCOPE_KINDS = {
    SCOPE_FULL: ("full_suite", "duplicate_probe", "confirmation_probe", "range_probe"),
    SCOPE_HISTORICAL: ("historical-forward",),
}

STATS_PREFIX = "OFFLINE-DOMAIN-STATS "
STATS_KEYS = frozenset({
    "selected", "deselected", "collected", "ran", "passed", "failed", "skipped",
    "errors", "selected_nodeids", "failed_nodeids", "error_nodeids",
    "collection_errors", "collected_files",
})
COUNT_KEYS = ("selected", "deselected", "collected", "ran", "passed", "failed", "skipped", "errors")
LIST_KEYS = ("selected_nodeids", "failed_nodeids", "error_nodeids", "collection_errors", "collected_files")
HARNESS_KEYS = ("launcher", "offline_guard", "measure", "job_guard")
MEMBER_KEYS = frozenset({"kind", "capture_id", "request", "result", "stdout", "stderr", "source", "environment", "harness", "selection"})
STREAM_KEYS = frozenset({"raw", "gzip", "raw_content_sha256"})
SOURCE_KEYS = frozenset({"root", "revision", "snapshot", "import_evidence"})
SELECTION_KEYS = frozenset({"expected_files", "expected_domains"})
ERROR_NODEID_RE = re.compile(r"^(?P<node>.+) \((?P<phase>setup|teardown)\)$")
ALLOWED_DOMAIN_EXITS = frozenset({0, 1, 5})

RUN_MARKER_RE = re.compile(r"^--- offline domain (?P<name>.+?): running ---$")
DONE_MARKER_RE = re.compile(r"^=== offline domain (?P<name>.+?): exit (?P<code>-?\d+) ===$")
DONE_NO_EXIT_RE = re.compile(r"^=== offline domain (?P<name>.+?): no child exit ===$")
PLAN_RE = re.compile(r"^Offline plan: (?P<count>\d+) domain\(s\): (?P<names>.+)$")
COMMAND_RE = re.compile(r"^Offline command: .*$")
EVIDENCE_RE = re.compile(r"^Offline evidence: .*$")
SUMMARY_RE = re.compile(
    r"^Offline summary: (?P<planned>\d+) domain\(s\) planned, (?P<launched>\d+) launched, "
    r"(?P<empty>\d+) skipped-empty, (?P<unrun>\d+) unrun, (?P<failed>\d+) launcher-failed$"
)
DOMAIN_SUMMARY_RE = re.compile(
    r"^Offline domain (?P<name>.+?): exit (?P<code>-?\d+), selected (?P<selected>\d+), "
    r"deselected (?P<deselected>\d+), ran (?P<ran>\d+), passed (?P<passed>\d+), "
    r"failed (?P<failed>\d+), skipped (?P<skipped>\d+), errors (?P<errors>\d+), "
    r"collection_errors (?P<collection_errors>\d+)$"
)
TOTALS_RE = re.compile(
    r"^Offline totals: selected (?P<selected>\d+) \(\+(?P<deselected>\d+) deselected\), "
    r"ran (?P<ran>\d+), passed (?P<passed>\d+), failed (?P<failed>\d+), "
    r"skipped (?P<skipped>\d+), errors (?P<errors>\d+), "
    r"collection_error_files (?P<collection_error_files>\d+), unresolved_files (?P<unresolved_files>\d+)$"
)
OVERALL_RE = re.compile(r"^Offline overall exit: (?P<code>-?\d+)$")
NOT_DISPATCHED_RE = re.compile(r"^Offline not-dispatched files \(no completed collection attempt\): (?P<files>.+)$")
COLLECTION_ERROR_FILES_RE = re.compile(r"^Offline collection error files: (?P<files>.+)$")
UNRESOLVED_RE = re.compile(r"^Offline unresolved files: (?P<files>.+)$")
LAUNCHER_FAILED_RE = re.compile(r"^Offline launcher-failed domains: (?P<domains>.+)$")
INTERRUPTED_RE = re.compile(r"^Offline (?:interrupted|stopped early); domains not run: (?P<domains>.+)$")


class InvalidInputError(Exception):
    """Invalid, incomplete or incomparable input; the CLI reports exit 2."""

    def __init__(self, findings):
        if isinstance(findings, str):
            findings = [findings]
        self.findings = list(findings)
        super().__init__("; ".join(self.findings))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_path_key(value: str) -> str:
    return value.replace("\\", "/").lower()


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_hex64(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reject_constant(name):
    raise ValueError(f"non-standard JSON constant {name!r}")


def _no_duplicate_keys(pairs):
    seen = set()
    for key, _ in pairs:
        if key in seen:
            raise ValueError(f"duplicate JSON key {key!r}")
        seen.add(key)
    return dict(pairs)


def loads_strict(text: str, label: str):
    """Parse JSON rejecting duplicate keys, NaN/Infinity and truncation."""
    try:
        return json.loads(text, object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant)
    except ValueError as error:
        raise InvalidInputError([f"{label}: invalid JSON ({error})"]) from error


def read_json_file(path: Path, label: str):
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise InvalidInputError([f"{label}: cannot read {path}: {error}"]) from error
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise InvalidInputError([f"{label}: {path} is not valid UTF-8 ({error})"]) from error
    return loads_strict(text, f"{label} ({path})")


def read_artifact_ref(ref, label: str) -> Path:
    if not isinstance(ref, dict) or set(ref) != {"path", "sha256"}:
        raise InvalidInputError([f"{label}: ArtifactRef must be an object with exactly 'path' and 'sha256'"])
    path_value, digest = ref["path"], ref["sha256"]
    if not isinstance(path_value, str) or not path_value:
        raise InvalidInputError([f"{label}: ArtifactRef path must be a non-empty string"])
    path = Path(path_value)
    if not path.is_absolute():
        raise InvalidInputError([f"{label}: ArtifactRef path must be absolute ({path_value})"])
    if not _is_hex64(digest):
        raise InvalidInputError([f"{label}: ArtifactRef sha256 must be 64 lowercase hex characters"])
    if not path.is_file():
        raise InvalidInputError([f"{label}: referenced file is missing: {path_value}"])
    actual = sha256_file(path)
    if actual != digest:
        raise InvalidInputError([
            f"{label}: hash mismatch for {path_value}: descriptor says {digest}, file is {actual}"
        ])
    return path


def load_stream(stream, label: str) -> bytes:
    """Verify a raw/gzip capture stream and return the uncompressed bytes."""
    if not isinstance(stream, dict) or not set(stream) <= STREAM_KEYS or "gzip" not in stream:
        raise InvalidInputError([f"{label}: stream must be an object with a 'gzip' ArtifactRef ('raw', 'raw_content_sha256' optional)"])
    gzip_path = read_artifact_ref(stream["gzip"], f"{label}.gzip")
    try:
        decompressed = gzip.decompress(gzip_path.read_bytes())
    except (OSError, EOFError) as error:
        raise InvalidInputError([f"{label}: gzip stream is corrupt ({error})"]) from error
    raw = stream.get("raw")
    if raw is not None:
        raw_path = read_artifact_ref(raw, f"{label}.raw")
        raw_bytes = raw_path.read_bytes()
        if raw_bytes != decompressed:
            raise InvalidInputError([
                f"{label}: gzip content differs from the raw file {raw_path}"
            ])
        declared_content_hash = stream.get("raw_content_sha256")
        if declared_content_hash is not None and sha256_bytes(raw_bytes) != declared_content_hash:
            raise InvalidInputError([
                f"{label}: retained raw_content_sha256 does not match the raw file {raw_path}"
            ])
        return raw_bytes
    content_hash = stream.get("raw_content_sha256")
    if not _is_hex64(content_hash):
        raise InvalidInputError([
            f"{label}: raw is null, so an independent 'raw_content_sha256' of the uncompressed content is required"
        ])
    actual = sha256_bytes(decompressed)
    if actual != content_hash:
        raise InvalidInputError([
            f"{label}: decompressed content hash {actual} does not match retained raw_content_sha256 {content_hash}"
        ])
    return decompressed


# ---------------------------------------------------------------------------
# OFFLINE-DOMAIN-STATS payload validation
# ---------------------------------------------------------------------------

def validate_stats(stats, label: str) -> None:
    if not isinstance(stats, dict):
        raise InvalidInputError([f"{label}: statistics payload must be a JSON object"])
    keys = set(stats)
    missing, extra = STATS_KEYS - keys, keys - STATS_KEYS
    if missing or extra:
        raise InvalidInputError([
            f"{label}: statistics key set mismatch (missing {sorted(missing)}, unexpected {sorted(extra)})"
        ])
    findings = []
    for key in COUNT_KEYS:
        if not _is_int(stats[key]) or stats[key] < 0:
            findings.append(f"{label}: count '{key}' must be a non-negative integer, got {stats[key]!r}")
    for key in LIST_KEYS:
        if not isinstance(stats[key], list) or not all(isinstance(item, str) and item for item in stats[key]):
            findings.append(f"{label}: '{key}' must be a list of non-empty strings")
    if findings:
        raise InvalidInputError(findings)

    selected = stats["selected_nodeids"]
    if stats["selected"] != len(selected):
        findings.append(f"{label}: selected {stats['selected']} != len(selected_nodeids) {len(selected)}")
    if len(set(selected)) != len(selected):
        findings.append(f"{label}: duplicate entries in selected_nodeids")
    if stats["collected"] != stats["selected"] + stats["deselected"]:
        findings.append(
            f"{label}: collected {stats['collected']} != selected {stats['selected']} + deselected {stats['deselected']}"
        )
    if stats["ran"] > stats["selected"]:
        findings.append(f"{label}: ran {stats['ran']} exceeds selected {stats['selected']}")
    failed, errors = stats["failed_nodeids"], stats["error_nodeids"]
    if stats["failed"] != len(failed):
        findings.append(f"{label}: failed {stats['failed']} != len(failed_nodeids) {len(failed)}")
    if stats["errors"] != len(errors):
        findings.append(f"{label}: errors {stats['errors']} != len(error_nodeids) {len(errors)}")
    if len(set(failed)) != len(failed):
        findings.append(f"{label}: duplicate entries in failed_nodeids")
    if len(set(errors)) != len(errors):
        findings.append(f"{label}: duplicate entries in error_nodeids")

    selected_set = set(selected)
    setup_error_nodes = set()
    for entry in failed:
        if entry not in selected_set:
            findings.append(f"{label}: failed nodeid not in selected population: {entry}")
    for entry in errors:
        match = ERROR_NODEID_RE.match(entry)
        if match is None:
            findings.append(f"{label}: error nodeid lacks a '(setup)'/'(teardown)' phase: {entry}")
            continue
        if match.group("node") not in selected_set:
            findings.append(f"{label}: error nodeid not in selected population: {entry}")
        if match.group("phase") == "setup":
            setup_error_nodes.add(match.group("node"))
    # Call-phase outcomes plus setup-errored tests (which never reach the call
    # phase) must cover exactly the started tests; a passed call and a teardown
    # error legitimately coexist on the same nodeid.
    call_outcomes = stats["passed"] + stats["failed"] + stats["skipped"]
    if call_outcomes + len(setup_error_nodes) != stats["ran"]:
        findings.append(
            f"{label}: passed {stats['passed']} + failed {stats['failed']} + skipped {stats['skipped']} "
            f"+ setup-errored {len(setup_error_nodes)} != ran {stats['ran']}"
        )
    files = stats["collected_files"]
    if len(set(files)) != len(files):
        findings.append(f"{label}: duplicate entries in collected_files")
    for entry in files:
        if not entry.endswith(".py"):
            findings.append(f"{label}: collected_files entry is not a python file: {entry}")
    collection_errors = stats["collection_errors"]
    if len(set(collection_errors)) != len(collection_errors):
        findings.append(f"{label}: duplicate entries in collection_errors")
    both = sorted(set(files) & set(collection_errors))
    if both:
        findings.append(f"{label}: files both collected and collection-errored: {both}")
    if findings:
        raise InvalidInputError(findings)


# ---------------------------------------------------------------------------
# Offline launcher stdout parsing
# ---------------------------------------------------------------------------

class ParsedLauncherOutput:
    def __init__(self):
        self.planned_domains: list[str] = []
        self.sections: dict[str, dict] = {}
        self.summary: dict = {}
        self.domain_summaries: dict[str, dict] = {}
        self.totals: dict = {}
        self.overall_exit: int | None = None
        self.collection_error_files: list[str] = []
        self.anomalies: list[str] = []
        self.completeness: list[str] = []
        self.output_layout: str | None = None  # "live-forwarding" | "post-hoc-dump"


def aggregate_domain_exits(codes: list[int]) -> int:
    """Re-derive scripts/test_offline.py's _overall_exit_code aggregation."""
    if 4 in codes:
        return 4
    if 3 in codes:
        return 3
    if 2 in codes:
        return 2
    if 1 in codes:
        return 1
    if any(code not in (0, 5) for code in codes):
        return 3
    if any(code == 0 for code in codes):
        return 0
    return 5


_PARENT_PRODUCTIONS = (
    SUMMARY_RE, DOMAIN_SUMMARY_RE, TOTALS_RE, OVERALL_RE, PLAN_RE, COMMAND_RE,
    EVIDENCE_RE, NOT_DISPATCHED_RE, COLLECTION_ERROR_FILES_RE, UNRESOLVED_RE,
    LAUNCHER_FAILED_RE, INTERRUPTED_RE,
)


def parse_launcher_stdout(text: str, label: str) -> ParsedLauncherOutput:
    """Parse one offline launcher stdout capture.

    Two launcher generations produce different section layouts and both are
    accepted, never mixed within one file: the current launcher forwards child
    output live between ``--- offline domain X: running ---`` and
    ``=== offline domain X: exit N ===``; the historical launcher printed the
    exit marker first and dumped the accumulated child output afterwards.
    """
    parsed = ParsedLauncherOutput()
    findings: list[str] = []
    lines = text.splitlines()
    index = 0
    total = len(lines)
    saw_plan = saw_summary = saw_totals = saw_overall = False
    current_section: dict | None = None
    layouts: set[str] = set()

    def open_section(name: str, layout: str, exit_code: int | None) -> bool:
        if name in parsed.sections:
            findings.append(f"{label}: duplicate domain section for {name!r}")
            return False
        parsed.sections[name] = {
            "name": name, "layout": layout, "stats_lines": [], "exit_code": exit_code,
        }
        return True

    def close_section() -> None:
        nonlocal current_section
        if current_section is not None and current_section["layout"] == "live" and current_section["exit_code"] is None:
            # registered at open; nothing extra to do at close
            pass
        current_section = None

    while index < total:
        line = lines[index]
        if line == "":
            index += 1
            continue
        run = RUN_MARKER_RE.match(line)
        done = DONE_MARKER_RE.match(line)
        done_missing = DONE_NO_EXIT_RE.match(line)

        if current_section is not None and current_section["layout"] == "live":
            if done is not None:
                if done.group("name") != current_section["name"]:
                    findings.append(
                        f"{label}: exit marker for {done.group('name')!r} closes section {current_section['name']!r}"
                    )
                    break
                current_section["exit_code"] = int(done.group("code"))
                close_section()
                index += 1
                continue
            if done_missing is not None:
                findings.append(f"{label}: domain {current_section['name']!r} ended with 'no child exit'")
                break
            if run is not None:
                findings.append(f"{label}: nested domain start marker inside section {current_section['name']!r}")
                break
            if line.startswith(STATS_PREFIX):
                current_section["stats_lines"].append(line[len(STATS_PREFIX):])
            index += 1
            continue

        if current_section is not None:  # post-hoc dump layout
            if line.startswith(STATS_PREFIX):
                current_section["stats_lines"].append(line[len(STATS_PREFIX):])
                index += 1
                continue
            if (
                run is not None or done is not None or done_missing is not None
                or any(production.match(line) for production in _PARENT_PRODUCTIONS)
            ):
                close_section()
                continue
            index += 1
            continue

        # Parent-level processing outside any domain section.
        if run is not None or done is not None or done_missing is not None:
            if not saw_plan:
                findings.append(f"{label}: domain marker before the 'Offline plan' line")
                break
            if saw_summary or saw_totals or saw_overall:
                findings.append(f"{label}: domain marker after the summary block")
                break
            if run is not None:
                if not open_section(run.group("name"), "live", None):
                    break
                layouts.add("live")
                current_section = parsed.sections[run.group("name")]
            else:
                if done_missing is not None:
                    findings.append(f"{label}: domain ended with 'no child exit'")
                    break
                if not open_section(done.group("name"), "dump", int(done.group("code"))):
                    break
                layouts.add("dump")
                current_section = parsed.sections[done.group("name")]
            index += 1
            continue

        if (match := COMMAND_RE.match(line)) is not None and not saw_plan:
            index += 1
            continue
        if (match := EVIDENCE_RE.match(line)) is not None and not saw_summary:
            index += 1
            continue
        if (match := PLAN_RE.match(line)) is not None:
            if saw_plan:
                findings.append(f"{label}: duplicate 'Offline plan' line")
                break
            names = [item.strip() for item in match.group("names").split(",")]
            if any(not item for item in names):
                findings.append(f"{label}: empty domain name in plan line")
                break
            if int(match.group("count")) != len(names):
                findings.append(f"{label}: plan line count disagrees with the listed domain names")
                break
            if len(set(names)) != len(names):
                findings.append(f"{label}: duplicate domain name in plan line")
                break
            parsed.planned_domains = names
            saw_plan = True
            index += 1
            continue
        if (match := SUMMARY_RE.match(line)) is not None:
            if saw_summary:
                findings.append(f"{label}: duplicate 'Offline summary' line")
                break
            parsed.summary = {key: int(match.group(key)) for key in ("planned", "launched", "empty", "unrun", "failed")}
            saw_summary = True
            index += 1
            continue
        if (match := DOMAIN_SUMMARY_RE.match(line)) is not None:
            if not saw_summary:
                findings.append(f"{label}: domain summary line before 'Offline summary'")
                break
            name = match.group("name")
            if name in parsed.domain_summaries:
                findings.append(f"{label}: duplicate domain summary line for {name!r}")
                break
            parsed.domain_summaries[name] = {
                "exit_code": int(match.group("code")),
                **{key: int(match.group(key)) for key in (
                    "selected", "deselected", "ran", "passed", "failed", "skipped",
                    "errors", "collection_errors")},
            }
            index += 1
            continue
        if (match := NOT_DISPATCHED_RE.match(line)) is not None:
            parsed.completeness.append(f"not-dispatched files: {match.group('files')}")
            index += 1
            continue
        if (match := COLLECTION_ERROR_FILES_RE.match(line)) is not None:
            parsed.collection_error_files = [item.strip() for item in match.group("files").split(",")]
            index += 1
            continue
        if (match := UNRESOLVED_RE.match(line)) is not None:
            parsed.completeness.append(f"unresolved files: {match.group('files')}")
            index += 1
            continue
        if (match := LAUNCHER_FAILED_RE.match(line)) is not None:
            parsed.completeness.append(f"launcher-failed domains: {match.group('domains')}")
            index += 1
            continue
        if (match := INTERRUPTED_RE.match(line)) is not None:
            parsed.completeness.append(f"interrupted/stopped early; domains not run: {match.group('domains')}")
            index += 1
            continue
        if (match := TOTALS_RE.match(line)) is not None:
            if saw_totals:
                findings.append(f"{label}: duplicate 'Offline totals' line")
                break
            parsed.totals = {key: int(match.group(key)) for key in (
                "selected", "deselected", "ran", "passed", "failed", "skipped",
                "errors", "collection_error_files", "unresolved_files")}
            saw_totals = True
            index += 1
            continue
        if (match := OVERALL_RE.match(line)) is not None:
            if not saw_totals:
                findings.append(f"{label}: overall exit line before 'Offline totals'")
                break
            if saw_overall:
                findings.append(f"{label}: duplicate 'Offline overall exit' line")
                break
            parsed.overall_exit = int(match.group("code"))
            saw_overall = True
            index += 1
            continue
        findings.append(f"{label}: unexpected parent-level line: {line[:160]!r}")
        break

    if current_section is not None:
        findings.append(f"{label}: domain section {current_section['name']!r} never closed (truncated stream)")
    if len(layouts) > 1:
        findings.append(f"{label}: mixed launcher output layouts {sorted(layouts)} in one capture")
    if layouts:
        parsed.output_layout = "live-forwarding" if layouts == {"live"} else "post-hoc-dump"
    if not saw_plan:
        findings.append(f"{label}: missing 'Offline plan' line")
    elif parsed.planned_domains and set(parsed.sections) != set(parsed.planned_domains):
        missing = sorted(set(parsed.planned_domains) - set(parsed.sections))
        extra = sorted(set(parsed.sections) - set(parsed.planned_domains))
        findings.append(f"{label}: captured sections disagree with the plan (missing {missing}, unexpected {extra})")
    if not saw_summary:
        findings.append(f"{label}: missing 'Offline summary' line")
    if not saw_totals:
        findings.append(f"{label}: missing 'Offline totals' line")
    if not saw_overall:
        findings.append(f"{label}: missing 'Offline overall exit' line")
    if findings:
        raise InvalidInputError(findings)

    # Per-domain statistics payload and exit consistency.
    for name, section in parsed.sections.items():
        domain_label = f"{label}: domain {name!r}"
        if len(section["stats_lines"]) != 1:
            findings.append(f"{domain_label}: expected exactly one {STATS_PREFIX.strip()} line, found {len(section['stats_lines'])}")
            continue
        stats = loads_strict(section["stats_lines"][0], f"{domain_label} statistics")
        validate_stats(stats, domain_label)
        code = section["exit_code"]
        if code is None:
            findings.append(f"{domain_label}: section has no exit marker")
            continue
        if code not in ALLOWED_DOMAIN_EXITS:
            findings.append(f"{domain_label}: native exit {code} is not a completed offline domain outcome {sorted(ALLOWED_DOMAIN_EXITS)}")
            continue
        failure_identities = stats["failed"] + stats["errors"] + len(stats["collection_errors"])
        if failure_identities > 0 and code == 0:
            findings.append(f"{domain_label}: exit 0 despite {failure_identities} failure/error/collection-error identities")
        if failure_identities == 0 and stats["selected"] > 0 and code != 0:
            findings.append(f"{domain_label}: exit {code} without any failure identity and {stats['selected']} selected tests")
        # Exit 5 is expected only for a genuinely empty domain. With
        # --continue-on-collection-errors a domain whose files all failed
        # collection legitimately completes with selected=0,
        # collection_errors>=1 and native exit 1 (plan §2: collection
        # failures are recorded outcomes, not missing records).
        if stats["selected"] == 0 and failure_identities == 0 and code != 5:
            findings.append(f"{domain_label}: exit {code} with nothing selected and nothing recorded (expected 5)")
        summary = parsed.domain_summaries.get(name)
        if summary is None:
            findings.append(f"{domain_label}: no parent summary line for this domain")
        else:
            for key in ("selected", "deselected", "ran", "passed", "failed", "skipped", "errors"):
                expected = stats[key]
                if summary[key] != expected:
                    findings.append(f"{domain_label}: parent summary {key} {summary[key]} != statistics {expected}")
            if summary["collection_errors"] != len(stats["collection_errors"]):
                findings.append(f"{domain_label}: parent summary collection_errors disagrees with statistics")
            if summary["exit_code"] != code:
                findings.append(f"{domain_label}: parent summary exit {summary['exit_code']} != section exit {code}")
        section["stats"] = stats
    parsed.domain_summaries = {
        name: summary for name, summary in parsed.domain_summaries.items() if name in parsed.sections
    }

    # Completeness: a baseline member must be fully dispatched and terminal.
    summary = parsed.summary
    if summary.get("planned") != len(parsed.planned_domains):
        findings.append(f"{label}: summary planned {summary.get('planned')} != plan line {len(parsed.planned_domains)}")
    if summary.get("launched") != len(parsed.sections):
        findings.append(f"{label}: summary launched {summary.get('launched')} != captured sections {len(parsed.sections)}")
    if summary.get("empty", 0):
        parsed.completeness.append(f"{summary['empty']} skipped-empty domain(s)")
    if summary.get("unrun", 0):
        parsed.completeness.append(f"{summary['unrun']} unrun domain(s)")
    if summary.get("failed", 0):
        parsed.completeness.append(f"{summary['failed']} launcher-failed domain(s)")
    if set(parsed.domain_summaries) != set(parsed.sections):
        findings.append(f"{label}: domain summary lines do not match captured sections exactly")
    if findings:
        raise InvalidInputError(findings)

    # Totals cross-check, mirroring the launcher's own aggregation.
    all_stats = [section["stats"] for section in parsed.sections.values()]
    computed = {
        "selected": len({node for stats in all_stats for node in stats["selected_nodeids"]}),
        "deselected": sum(stats["deselected"] for stats in all_stats),
        "ran": sum(stats["ran"] for stats in all_stats),
        "passed": sum(stats["passed"] for stats in all_stats),
        "failed": len({node for stats in all_stats for node in stats["failed_nodeids"]}),
        "skipped": sum(stats["skipped"] for stats in all_stats),
        "errors": len({node for stats in all_stats for node in stats["error_nodeids"]}),
        "collection_error_files": len({
            entry for stats in all_stats for entry in stats["collection_errors"] if entry.endswith(".py")
        }),
    }
    for key, value in computed.items():
        if parsed.totals.get(key) != value:
            findings.append(f"{label}: totals {key} {parsed.totals.get(key)} != recomputed {value}")
    if parsed.totals.get("unresolved_files"):
        parsed.completeness.append(f"unresolved_files={parsed.totals['unresolved_files']}")
    expected_error_files = sorted({
        entry for stats in all_stats for entry in stats["collection_errors"] if entry.endswith(".py")
    })
    if parsed.collection_error_files and parsed.collection_error_files != expected_error_files:
        findings.append(
            f"{label}: 'Offline collection error files' line {parsed.collection_error_files} != statistics {expected_error_files}"
        )
    codes = [section["exit_code"] for section in parsed.sections.values()]
    recomputed_overall = aggregate_domain_exits(codes)
    if parsed.overall_exit != recomputed_overall:
        findings.append(
            f"{label}: printed overall exit {parsed.overall_exit} != recomputed aggregation {recomputed_overall} of {codes}"
        )
    if findings:
        raise InvalidInputError(findings)
    return parsed


# ---------------------------------------------------------------------------
# Descriptor validation and member recording
# ---------------------------------------------------------------------------

def _require_keys(obj, required: frozenset, label: str, *, exact: bool = True) -> None:
    if not isinstance(obj, dict):
        raise InvalidInputError([f"{label}: must be a JSON object"])
    keys = set(obj)
    missing, extra = required - keys, keys - required
    if missing or (extra and exact):
        message = f"{label}: key set mismatch (missing {sorted(missing)}"
        if extra and exact:
            message += f", unexpected {sorted(extra)}"
        raise InvalidInputError([message + ")"])


def load_environment(ref, label: str) -> dict:
    env = read_json_file(read_artifact_ref(ref, f"{label}.environment"), f"{label}.environment")
    _require_keys(env, frozenset({"python", "executable", "packages"}), f"{label}.environment")
    if not isinstance(env["python"], str) or not env["python"]:
        raise InvalidInputError([f"{label}.environment: 'python' must be a non-empty string"])
    if not isinstance(env["executable"], str) or not env["executable"]:
        raise InvalidInputError([f"{label}.environment: 'executable' must be a non-empty string"])
    packages = env["packages"]
    if not isinstance(packages, list) or not all(
        isinstance(pair, list) and len(pair) == 2 and all(isinstance(item, str) for item in pair)
        for pair in packages
    ):
        raise InvalidInputError([f"{label}.environment: 'packages' must be a list of [name, version] pairs"])
    return env


def environment_identity(env: dict) -> str:
    payload = {
        "python": env["python"],
        "executable": _norm_path_key(env["executable"]),
        "packages": env["packages"],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256_bytes(canonical.encode("utf-8"))


def load_import_evidence(ref, source, launcher_sha: str, label: str) -> dict:
    evidence = read_json_file(read_artifact_ref(ref, f"{label}.import_evidence"), f"{label}.import_evidence")
    _require_keys(
        evidence,
        frozenset({"schema", "tree", "revision", "launcher_sha256", "established_by", "recorded_at"}),
        f"{label}.import_evidence",
        exact=False,
    )
    if evidence["schema"] != IMPORT_EVIDENCE_SCHEMA:
        raise InvalidInputError([f"{label}.import_evidence: schema must be {IMPORT_EVIDENCE_SCHEMA!r}"])
    if _norm_path_key(evidence["tree"]) != _norm_path_key(source["root"]):
        raise InvalidInputError([f"{label}.import_evidence: tree {evidence['tree']!r} does not match source root"])
    if evidence["revision"] != source["revision"]:
        raise InvalidInputError([f"{label}.import_evidence: revision does not match source revision"])
    if evidence["launcher_sha256"] != launcher_sha:
        raise InvalidInputError([f"{label}.import_evidence: launcher_sha256 does not match the harness launcher"])
    for key in ("established_by", "recorded_at"):
        if not isinstance(evidence[key], str) or not evidence[key]:
            raise InvalidInputError([f"{label}.import_evidence: '{key}' must be a non-empty string"])
    return evidence


def record_member(member, scope: str) -> dict:
    if not isinstance(member, dict):
        raise InvalidInputError(["descriptor member must be a JSON object"])
    _require_keys(member, MEMBER_KEYS, "descriptor member")
    kind, capture_id = member["kind"], member["capture_id"]
    label = f"member {capture_id!r} ({kind})"
    if scope == SCOPE_HISTORICAL and kind != "historical-forward":
        raise InvalidInputError([f"{label}: scope {SCOPE_HISTORICAL!r} requires kind 'historical-forward'"])
    if scope == SCOPE_FULL and kind not in SCOPE_KINDS[SCOPE_FULL]:
        raise InvalidInputError([f"{label}: kind is not one of {list(SCOPE_KINDS[SCOPE_FULL])}"])

    # All run-record references must live in one capture directory named after
    # the capture id; this binds the member to a single sealed invocation.
    for stream_key in ("stdout", "stderr"):
        stream = member[stream_key]
        if not isinstance(stream, dict) or not set(stream) <= STREAM_KEYS or "gzip" not in stream:
            raise InvalidInputError([
                f"{label}.{stream_key}: stream must be an object with a 'gzip' ArtifactRef"
            ])
    stream_refs = [member["request"], member["result"], member["stdout"].get("gzip"), member["stderr"].get("gzip")]
    raw_refs = [member["stdout"].get("raw"), member["stderr"].get("raw")]
    directories = {_norm_path_key(str(Path(ref["path"]).parent)) for ref in stream_refs + [r for r in raw_refs if r]}
    if len(directories) != 1 or Path(next(iter(directories))).name.lower() != str(capture_id).lower():
        raise InvalidInputError([
            f"{label}: request/result/stdout/stderr references must share one directory named {capture_id!r}"
        ])

    request_path = read_artifact_ref(member["request"], f"{label}.request")
    result_path = read_artifact_ref(member["result"], f"{label}.result")
    request = read_json_file(request_path, f"{label}.request")
    result = read_json_file(result_path, f"{label}.result")
    _require_keys(request, frozenset({"started", "argv", "cwd", "wall_seconds"}), f"{label}.request", exact=False)
    for key in ("started", "cwd"):
        if not isinstance(request[key], str) or not request[key]:
            raise InvalidInputError([f"{label}.request: '{key}' must be a non-empty string"])
    if not isinstance(request["argv"], list) or not request["argv"] or not all(
        isinstance(item, str) for item in request["argv"]
    ):
        raise InvalidInputError([f"{label}.request: 'argv' must be a non-empty list of strings"])
    if not isinstance(request["wall_seconds"], (int, float)) or isinstance(request["wall_seconds"], bool):
        raise InvalidInputError([f"{label}.request: 'wall_seconds' must be a number"])
    for key in ("memory_available", "C_free", "D_free"):
        if key in request and (not isinstance(request[key], (int, float)) or isinstance(request[key], bool)):
            raise InvalidInputError([f"{label}.request: '{key}' must be a number"])

    for key in ("child_exit", "supervisor_error", "termination"):
        if key not in result:
            raise InvalidInputError([f"{label}.result: required key {key!r} missing"])
    if not _is_int(result["child_exit"]):
        raise InvalidInputError([f"{label}.result: child_exit must be an integer, not a Boolean or other type"])
    if result["termination"] is not None:
        raise InvalidInputError([
            f"{label}.result: capture is not terminal (termination={result['termination']!r})"
        ])
    if result["supervisor_error"] is not None:
        raise InvalidInputError([
            f"{label}.result: supervisor failed (supervisor_error={result['supervisor_error']!r})"
        ])

    source = member["source"]
    _require_keys(source, SOURCE_KEYS, f"{label}.source")
    root = source["root"]
    if not isinstance(root, str) or not Path(root).is_absolute():
        raise InvalidInputError([f"{label}.source: root must be an absolute path"])
    if not Path(root).is_dir():
        raise InvalidInputError([f"{label}.source: root directory does not exist: {root}"])
    if not isinstance(source["revision"], str) or not source["revision"]:
        raise InvalidInputError([f"{label}.source: revision must be a non-empty string"])
    snapshot_path = read_artifact_ref(source["snapshot"], f"{label}.source.snapshot")

    harness = member["harness"]
    _require_keys(harness, frozenset(HARNESS_KEYS), f"{label}.harness")
    harness_paths = {key: read_artifact_ref(harness[key], f"{label}.harness.{key}") for key in HARNESS_KEYS}
    harness_shas = {key: harness[key]["sha256"] for key in HARNESS_KEYS}

    import_evidence = load_import_evidence(
        source["import_evidence"], source, harness_shas["launcher"], label
    )

    env = load_environment(member["environment"], label)

    argv = request["argv"]
    if _norm_path_key(argv[0]) != _norm_path_key(env["executable"]):
        raise InvalidInputError([
            f"{label}: argv interpreter {argv[0]!r} is not the fingerprinted environment executable {env['executable']!r}"
        ])
    if not any(_norm_path_key(token).endswith("scripts/test_offline.py") for token in argv):
        raise InvalidInputError([f"{label}: argv does not invoke scripts/test_offline.py"])
    if _norm_path_key(request["cwd"]) != _norm_path_key(root):
        raise InvalidInputError([
            f"{label}: request cwd {request['cwd']!r} is not the source root {root!r}"
        ])

    stdout_bytes = load_stream(member["stdout"], f"{label}.stdout")
    stderr_bytes = load_stream(member["stderr"], f"{label}.stderr")
    try:
        stdout_text = stdout_bytes.decode("utf-8", errors="replace")
    except UnicodeDecodeError as error:  # pragma: no cover - decode with replace cannot raise
        raise InvalidInputError([f"{label}.stdout: not decodable ({error})"]) from error

    parsed = parse_launcher_stdout(stdout_text, f"{label}.stdout")
    if parsed.completeness:
        raise InvalidInputError([
            f"{label}: capture is incomplete for baseline evidence: " + "; ".join(parsed.completeness)
        ])
    if result["child_exit"] != parsed.overall_exit:
        raise InvalidInputError([
            f"{label}: guard child_exit {result['child_exit']} != launcher overall exit {parsed.overall_exit}"
        ])

    selection = member["selection"]
    _require_keys(selection, SELECTION_KEYS, f"{label}.selection")
    expected_files, expected_domains = selection["expected_files"], selection["expected_domains"]
    for name, values in (("expected_files", expected_files), ("expected_domains", expected_domains)):
        if not isinstance(values, list) or not all(isinstance(item, str) and item for item in values):
            raise InvalidInputError([f"{label}.selection: '{name}' must be a list of non-empty strings"])
        if len(set(values)) != len(values):
            raise InvalidInputError([f"{label}.selection: duplicate entries in '{name}'"])
    for entry in expected_files:
        if "\\" in entry or ":" in entry or entry.startswith("/") or ".." in entry.split("/"):
            raise InvalidInputError([f"{label}.selection: expected_files entry must be repository-relative posix: {entry!r}"])

    parsed_domain_names = set(parsed.sections)
    if parsed_domain_names != set(expected_domains):
        missing = sorted(set(expected_domains) - parsed_domain_names)
        extra = sorted(parsed_domain_names - set(expected_domains))
        raise InvalidInputError([
            f"{label}: captured domains disagree with expected_domains (missing {missing}, unexpected {extra})"
        ])
    resolved_files = {
        entry
        for section in parsed.sections.values()
        for entry in list(section["stats"]["collected_files"]) + list(section["stats"]["collection_errors"])
        if entry.endswith(".py")
    }
    if resolved_files != set(expected_files):
        missing = sorted(set(expected_files) - resolved_files)
        extra = sorted(resolved_files - set(expected_files))
        raise InvalidInputError([
            f"{label}: selected test-file population mismatch (never resolved {missing}, unexpectedly resolved {extra})"
        ])

    domains_record = [
        {
            "name": name,
            "launch_state": "launched",
            "exit_code": section["exit_code"],
            "stats": section["stats"],
        }
        for name, section in parsed.sections.items()
    ]
    guard_record = {
        "child_exit": result["child_exit"],
        "termination": result["termination"],
        "supervisor_error": result["supervisor_error"],
    }
    for key in ("wall_s", "child_pid"):
        if key in result and _is_int(result[key]):
            guard_record[key] = result[key]
    return {
        "kind": kind,
        "capture_id": capture_id,
        "invocation": {
            "started": request["started"],
            "argv": argv,
            "cwd": request["cwd"],
            "wall_seconds": request["wall_seconds"],
            **{key: request[key] for key in ("memory_available", "C_free", "D_free") if key in request},
        },
        "selection": {"expected_files": sorted(expected_files), "expected_domains": sorted(expected_domains)},
        "resolved_files": sorted(resolved_files),
        "domains": domains_record,
        "parent": {
            "planned": parsed.summary["planned"],
            "launched": parsed.summary["launched"],
            "skipped_empty": parsed.summary["empty"],
            "unrun": parsed.summary["unrun"],
            "launcher_failed": parsed.summary["failed"],
            "interrupted": False,
            "output_layout": parsed.output_layout,
            "overall_exit": parsed.overall_exit,
            "guard": guard_record,
            "totals": dict(parsed.totals),
        },
        "raw_evidence": {
            "capture_directory": str(Path(request_path).parent),
            "request": dict(member["request"]),
            "result": dict(member["result"]),
            "stdout": {
                "raw": dict(member["stdout"]["raw"]) if member["stdout"].get("raw") else None,
                "gzip": dict(member["stdout"]["gzip"]),
                "content_sha256": sha256_bytes(stdout_bytes),
            },
            "stderr": {
                "raw": dict(member["stderr"]["raw"]) if member["stderr"].get("raw") else None,
                "gzip": dict(member["stderr"]["gzip"]),
                "content_sha256": sha256_bytes(stderr_bytes),
            },
            "snapshot": {"path": str(snapshot_path), "sha256": source["snapshot"]["sha256"]},
        },
        "_context": {
            "source": {
                "root": root,
                "revision": source["revision"],
                "snapshot_sha256": source["snapshot"]["sha256"],
            },
            "harness": harness_shas,
            "environment": env,
            "environment_identity": environment_identity(env),
            "environment_ref": dict(member["environment"]),
            "harness_paths": {key: str(path) for key, path in harness_paths.items()},
            "import_evidence": import_evidence,
        },
    }


def record_descriptor(descriptor_path: str) -> dict:
    descriptor = read_json_file(Path(descriptor_path), "capture descriptor")
    _require_keys(descriptor, frozenset({"schema_version", "scope", "members"}), "capture descriptor", exact=False)
    if descriptor["schema_version"] != DESCRIPTOR_SCHEMA:
        raise InvalidInputError([f"capture descriptor: schema_version must be {DESCRIPTOR_SCHEMA!r}"])
    scope = descriptor["scope"]
    if scope not in SCOPE_KINDS:
        raise InvalidInputError([f"capture descriptor: scope must be one of {sorted(SCOPE_KINDS)}"])
    members = descriptor["members"]
    if not isinstance(members, list) or not members:
        raise InvalidInputError(["capture descriptor: members must be a non-empty list"])

    recorded = [_record_one(member, scope) for member in members]

    kinds = [member["kind"] for member in recorded]
    if len(set(kinds)) != len(kinds):
        raise InvalidInputError([f"capture descriptor: duplicate member kinds {sorted(kinds)}"])
    if set(kinds) != set(SCOPE_KINDS[scope]):
        raise InvalidInputError([
            f"capture descriptor: scope {scope!r} requires exactly kinds {sorted(SCOPE_KINDS[scope])}, got {sorted(kinds)}"
        ])

    contexts = [member.pop("_context") for member in recorded]
    sources = {(c["source"]["root"], c["source"]["revision"], c["source"]["snapshot_sha256"]) for c in contexts}
    if len(sources) != 1:
        raise InvalidInputError([
            "capture descriptor: members do not share one source identity: " + repr(sorted(sources))
        ])
    env_identities = {c["environment_identity"] for c in contexts}
    if len(env_identities) != 1:
        raise InvalidInputError(["capture descriptor: members do not share one environment fingerprint identity"])
    harness_sets = {json.dumps(c["harness"], sort_keys=True) for c in contexts}
    if len(harness_sets) != 1:
        raise InvalidInputError(["capture descriptor: members do not share identical harness identities"])

    population = [node for member in recorded for domain in member["domains"] for node in domain["stats"]["selected_nodeids"]]
    if len(set(population)) != len(population):
        overlap = len(population) - len(set(population))
        raise InvalidInputError([
            f"capture descriptor: selected populations overlap across members ({overlap} duplicated nodeids)"
        ])

    context = contexts[0]
    baseline = {
        "schema": BASELINE_SCHEMA,
        "scope": scope,
        "recorded_at": _now(),
        "source": {
            "revision": context["source"]["revision"],
            "root": context["source"]["root"],
            "snapshot_sha256": context["source"]["snapshot_sha256"],
        },
        "harness": context["harness"],
        "environment": {
            "python": context["environment"]["python"],
            "executable": context["environment"]["executable"],
            "packages": context["environment"]["packages"],
            "fingerprint_file": context["environment_ref"],
        },
        "environment_identity": context["environment_identity"],
        "import_evidence": context["import_evidence"],
        "members": recorded,
        "population": {
            "selected_total": len(set(population)),
            "disjoint": True,
        },
    }
    validate_baseline_object(baseline, "recorded baseline")
    return baseline


def _record_one(member, scope: str) -> dict:
    try:
        return record_member(member, scope)
    except InvalidInputError as error:
        capture = member.get("capture_id", "<unknown>") if isinstance(member, dict) else "<invalid>"
        raise InvalidInputError([f"[member {capture!r}] {finding}" for finding in error.findings]) from error


# ---------------------------------------------------------------------------
# Baseline object validation (used for loaded baselines on compare)
# ---------------------------------------------------------------------------

def _validate_loaded_member(member: dict, label: str) -> None:
    _require_keys(
        member,
        frozenset({"kind", "capture_id", "invocation", "selection", "resolved_files", "domains", "parent", "raw_evidence"}),
        label,
        exact=False,
    )
    invocation = member["invocation"]
    for key in ("started", "cwd", "argv", "wall_seconds"):
        if key not in invocation:
            raise InvalidInputError([f"{label}.invocation: required key {key!r} missing"])
    if not isinstance(invocation["argv"], list) or not all(isinstance(i, str) for i in invocation["argv"]):
        raise InvalidInputError([f"{label}.invocation: argv must be a list of strings"])
    parent = member["parent"]
    for key in ("planned", "launched", "skipped_empty", "unrun", "launcher_failed", "interrupted", "overall_exit", "guard", "totals"):
        if key not in parent:
            raise InvalidInputError([f"{label}.parent: required key {key!r} missing"])
    guard = parent["guard"]
    if not _is_int(guard.get("child_exit")):
        raise InvalidInputError([f"{label}.parent.guard: child_exit must be an integer"])
    if guard.get("termination") is not None or guard.get("supervisor_error") is not None:
        raise InvalidInputError([f"{label}: guard record is not a clean terminal capture"])
    if parent["interrupted"] or parent["unrun"] or parent["launcher_failed"] or parent["skipped_empty"]:
        raise InvalidInputError([f"{label}: parent state is not a fully dispatched clean run"])
    # F2: all three counts must agree exactly; a chained != comparison would
    # admit planned/launched disagreements whenever one pair happens to match.
    if parent["planned"] != parent["launched"] or parent["launched"] != len(member["domains"]):
        raise InvalidInputError([
            f"{label}: parent counts inconsistent: planned={parent['planned']}, "
            f"launched={parent['launched']}, domains={len(member['domains'])}"
        ])

    domains = member["domains"]
    if not isinstance(domains, list) or not domains:
        raise InvalidInputError([f"{label}: domains must be a non-empty list"])
    names = [domain.get("name") for domain in domains]
    if len(set(names)) != len(names) or not all(isinstance(n, str) and n for n in names):
        raise InvalidInputError([f"{label}: domain names must be unique non-empty strings"])
    codes = []
    resolved: set[str] = set()
    for domain in domains:
        domain_label = f"{label}: domain {domain['name']!r}"
        if domain.get("launch_state") != "launched":
            raise InvalidInputError([f"{domain_label}: launch_state must be 'launched' in a recorded baseline"])
        code = domain.get("exit_code")
        if not _is_int(code):
            raise InvalidInputError([f"{domain_label}: exit_code must be an integer"])
        if code not in ALLOWED_DOMAIN_EXITS:
            raise InvalidInputError([f"{domain_label}: exit {code} is not a completed offline domain outcome"])
        codes.append(code)
        validate_stats(domain["stats"], domain_label)
        stats = domain["stats"]
        failure_identities = stats["failed"] + stats["errors"] + len(stats["collection_errors"])
        if failure_identities and code == 0:
            raise InvalidInputError([f"{domain_label}: exit 0 despite failure identities"])
        if not failure_identities and stats["selected"] and code != 0:
            raise InvalidInputError([f"{domain_label}: exit {code} without failure identities"])
        # Mirror of the parse-path rule: exit 5 only for a genuinely empty
        # domain; a collection-error-only domain keeps its native exit 1.
        if not stats["selected"] and not failure_identities and code != 5:
            raise InvalidInputError([f"{domain_label}: exit {code} with nothing selected and nothing recorded"])
        resolved.update(entry for entry in list(stats["collected_files"]) + list(stats["collection_errors"]) if entry.endswith(".py"))
    if set(member["selection"]["expected_files"]) != resolved:
        raise InvalidInputError([f"{label}: expected_files disagree with resolved domain files"])
    if set(member["selection"]["expected_domains"]) != set(names):
        raise InvalidInputError([f"{label}: expected_domains disagree with recorded domains"])
    overall = aggregate_domain_exits(codes)
    if parent["overall_exit"] != overall:
        raise InvalidInputError([f"{label}: overall_exit {parent['overall_exit']} != recomputed {overall}"])
    if guard["child_exit"] != parent["overall_exit"]:
        raise InvalidInputError([f"{label}: guard child_exit != overall_exit"])


def validate_baseline_object(baseline, label: str) -> dict:
    _require_keys(
        baseline,
        frozenset({"schema", "scope", "recorded_at", "source", "harness", "environment",
                   "environment_identity", "import_evidence", "members", "population"}),
        label,
        exact=False,
    )
    if baseline["schema"] != BASELINE_SCHEMA:
        raise InvalidInputError([f"{label}: schema must be {BASELINE_SCHEMA!r}"])
    scope = baseline["scope"]
    if scope not in SCOPE_KINDS:
        raise InvalidInputError([f"{label}: unknown scope {scope!r}"])
    source = baseline["source"]
    for key in ("revision", "root", "snapshot_sha256"):
        if not isinstance(source.get(key), str) or not source[key]:
            raise InvalidInputError([f"{label}.source: '{key}' must be a non-empty string"])
    if not _is_hex64(source["snapshot_sha256"]):
        raise InvalidInputError([f"{label}.source: snapshot_sha256 must be hex64"])
    harness = baseline["harness"]
    if set(harness) != set(HARNESS_KEYS) or not all(_is_hex64(harness[key]) for key in HARNESS_KEYS):
        raise InvalidInputError([f"{label}.harness: exactly {list(HARNESS_KEYS)} hex64 hashes required"])

    env = baseline["environment"]
    _require_keys(env, frozenset({"python", "executable", "packages", "fingerprint_file"}), f"{label}.environment", exact=False)
    if not isinstance(env["python"], str) or not isinstance(env["executable"], str):
        raise InvalidInputError([f"{label}.environment: python/executable must be strings"])
    if not isinstance(env["packages"], list):
        raise InvalidInputError([f"{label}.environment: packages must be a list"])
    if environment_identity(env) != baseline["environment_identity"]:
        raise InvalidInputError([f"{label}: stored environment_identity does not match the stored environment content"])

    members = baseline["members"]
    if not isinstance(members, list) or not members:
        raise InvalidInputError([f"{label}: members must be a non-empty list"])
    kinds = [member.get("kind") for member in members]
    if len(set(kinds)) != len(kinds) or set(kinds) != set(SCOPE_KINDS[scope]):
        raise InvalidInputError([f"{label}: scope {scope!r} requires exactly kinds {sorted(SCOPE_KINDS[scope])}, got {sorted(map(str, kinds))}"])
    for member in members:
        _validate_loaded_member(member, f"{label}: member {member.get('capture_id', '?')!r}")

    population = [
        node
        for member in members
        for domain in member["domains"]
        for node in domain["stats"]["selected_nodeids"]
    ]
    if len(set(population)) != len(population):
        raise InvalidInputError([f"{label}: selected populations overlap across members"])
    if baseline["population"].get("selected_total") != len(set(population)):
        raise InvalidInputError([f"{label}: population.selected_total disagrees with members"])
    return baseline


def load_baseline(path_str: str, label: str) -> dict:
    path = Path(path_str)
    if not path.is_file():
        raise InvalidInputError([f"{label}: file not found: {path_str}"])
    return validate_baseline_object(read_json_file(path, label), label)


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def _member_counts(member: dict) -> dict:
    totals = {
        "selected": 0, "deselected": 0, "ran": 0, "passed": 0,
        "failed": 0, "skipped": 0, "errors": 0, "collection_error_files": 0,
    }
    for domain in member["domains"]:
        stats = domain["stats"]
        totals["selected"] += stats["selected"]
        totals["deselected"] += stats["deselected"]
        totals["ran"] += stats["ran"]
        totals["passed"] += stats["passed"]
        totals["skipped"] += stats["skipped"]
        totals["failed"] += len(stats["failed_nodeids"])
        totals["errors"] += len(stats["error_nodeids"])
        totals["collection_error_files"] += sum(1 for e in stats["collection_errors"] if e.endswith(".py"))
    return totals


def _identities(member: dict, key: str) -> set[str]:
    return {entry for domain in member["domains"] for entry in domain["stats"][key]}


def compare_baselines(baseline: dict, candidate: dict) -> tuple[int, dict]:
    findings: list[str] = []
    if baseline["scope"] != candidate["scope"]:
        raise InvalidInputError([
            f"scope mismatch: baseline {baseline['scope']!r} vs candidate {candidate['scope']!r}"
        ])
    if baseline["environment_identity"] != candidate["environment_identity"]:
        raise InvalidInputError([
            "environment fingerprint mismatch: baseline "
            f"{baseline['environment']['executable']} ({baseline['environment_identity'][:12]}...) vs candidate "
            f"{candidate['environment']['executable']} ({candidate['environment_identity'][:12]}...); "
            "records from different environments are incomparable"
        ])
    base_members = {member["kind"]: member for member in baseline["members"]}
    cand_members = {member["kind"]: member for member in candidate["members"]}
    if set(base_members) != set(cand_members):
        missing = sorted(set(base_members) - set(cand_members))
        extra = sorted(set(cand_members) - set(base_members))
        raise InvalidInputError([f"member kinds mismatch (missing {missing}, unexpected {extra})"])

    if baseline["source"]["revision"] != candidate["source"]["revision"]:
        findings.append(
            f"candidate source revision {candidate['source']['revision']} replaces baseline {baseline['source']['revision']} (expected for a candidate)"
        )
    if baseline["source"]["snapshot_sha256"] == candidate["source"]["snapshot_sha256"] and baseline["source"]["revision"] != candidate["source"]["revision"]:
        findings.append("source snapshot bytes identical while revisions differ: check descriptor identity inputs")
    for key in HARNESS_KEYS:
        if baseline["harness"][key] != candidate["harness"][key]:
            findings.append(f"harness '{key}' changed: {baseline['harness'][key][:12]}... -> {candidate['harness'][key][:12]}...")
    base_env_file = baseline["environment"]["fingerprint_file"]
    cand_env_file = candidate["environment"]["fingerprint_file"]
    if base_env_file.get("sha256") == cand_env_file.get("sha256") and base_env_file.get("path") != cand_env_file.get("path"):
        findings.append("environment fingerprint file moved (content identical)")

    member_diffs = []
    added_failures = added_errors = added_collection_errors = 0
    resolved_failures = resolved_errors = resolved_collection_errors = 0
    for kind in sorted(base_members):
        base, cand = base_members[kind], cand_members[kind]
        base_domains = {domain["name"]: domain for domain in base["domains"]}
        cand_domains = {domain["name"]: domain for domain in cand["domains"]}
        missing_domains = sorted(set(base_domains) - set(cand_domains))
        added_domains = sorted(set(cand_domains) - set(base_domains))
        if missing_domains:
            findings.append(f"member {kind!r}: population loss, missing domains {missing_domains}")
            continue
        base_files, cand_files = set(base["selection"]["expected_files"]), set(cand["selection"]["expected_files"])
        missing_files = sorted(base_files - cand_files)
        if missing_files:
            findings.append(f"member {kind!r}: population loss, missing selected files {missing_files}")

        base_nodes = _identities(base, "selected_nodeids")
        cand_nodes = _identities(cand, "selected_nodeids")
        missing_nodes = sorted(base_nodes - cand_nodes)
        if missing_nodes:
            findings.append(
                f"member {kind!r}: population loss, {len(missing_nodes)} previously selected nodeids absent, "
                f"first {missing_nodes[:3]}"
            )

        base_failed, cand_failed = _identities(base, "failed_nodeids"), _identities(cand, "failed_nodeids")
        base_errors, cand_errors = _identities(base, "error_nodeids"), _identities(cand, "error_nodeids")
        base_coll = {e for e in _identities(base, "collection_errors") if e.endswith(".py")}
        cand_coll = {e for e in _identities(cand, "collection_errors") if e.endswith(".py")}

        d_failed_added = sorted(cand_failed - base_failed)
        d_failed_resolved = sorted(base_failed - cand_failed)
        d_errors_added = sorted(cand_errors - base_errors)
        d_errors_resolved = sorted(base_errors - cand_errors)
        d_coll_added = sorted(cand_coll - base_coll)
        d_coll_resolved = sorted(base_coll - cand_coll)

        added_failures += len(d_failed_added)
        added_errors += len(d_errors_added)
        added_collection_errors += len(d_coll_added)
        resolved_failures += len(d_failed_resolved)
        resolved_errors += len(d_errors_resolved)
        resolved_collection_errors += len(d_coll_resolved)

        domain_rows = []
        for name in sorted(set(base_domains) | set(cand_domains)):
            row = {"name": name}
            for side, domains in (("baseline", base_domains), ("candidate", cand_domains)):
                if name in domains:
                    stats = domains[name]["stats"]
                    row[side] = {
                        "exit_code": domains[name]["exit_code"],
                        "selected": stats["selected"],
                        "passed": stats["passed"],
                        "failed": stats["failed"],
                        "skipped": stats["skipped"],
                        "errors": stats["errors"],
                    }
            domain_rows.append(row)

        base_counts, cand_counts = _member_counts(base), _member_counts(cand)
        if cand_counts["skipped"] != base_counts["skipped"]:
            findings.append(
                f"member {kind!r}: skipped count changed {base_counts['skipped']} -> {cand_counts['skipped']}"
            )
        if cand_counts["deselected"] != base_counts["deselected"]:
            findings.append(
                f"member {kind!r}: deselected count changed {base_counts['deselected']} -> {cand_counts['deselected']}"
            )

        member_diffs.append({
            "kind": kind,
            "population": {
                "baseline_selected": len(base_nodes),
                "candidate_selected": len(cand_nodes),
                "added_nodeids": sorted(cand_nodes - base_nodes),
                "missing_nodeids": missing_nodes,
                "added_files": sorted(cand_files - base_files),
                "missing_files": missing_files,
                "added_domains": added_domains,
                "missing_domains": missing_domains,
            },
            "failures": {
                "added_failed": d_failed_added,
                "resolved_failed": d_failed_resolved,
                "added_errors": d_errors_added,
                "resolved_errors": d_errors_resolved,
                "added_collection_errors": d_coll_added,
                "resolved_collection_errors": d_coll_resolved,
            },
            "counts": {"baseline": base_counts, "candidate": cand_counts},
            "domains": domain_rows,
            "native": {
                "baseline_overall_exit": base["parent"]["overall_exit"],
                "candidate_overall_exit": cand["parent"]["overall_exit"],
                "baseline_guard_child_exit": base["parent"]["guard"]["child_exit"],
                "candidate_guard_child_exit": cand["parent"]["guard"]["child_exit"],
            },
        })

    if any("population loss" in finding for finding in findings):
        population_findings = [f for f in findings if "population loss" in f]
        raise InvalidInputError(population_findings)

    exit_code = 1 if (added_failures or added_errors or added_collection_errors) else 0
    diff = {
        "schema": DIFF_SCHEMA,
        "generated_at": _now(),
        "scope": baseline["scope"],
        "baseline": {
            "recorded_at": baseline["recorded_at"],
            "source_revision": baseline["source"]["revision"],
            "environment_identity": baseline["environment_identity"],
        },
        "candidate": {
            "recorded_at": candidate["recorded_at"],
            "source_revision": candidate["source"]["revision"],
            "environment_identity": candidate["environment_identity"],
        },
        "environment_match": True,
        "identity_findings": findings,
        "summary": {
            "added_failed_total": added_failures,
            "added_errors_total": added_errors,
            "added_collection_errors_total": added_collection_errors,
            "resolved_failed_total": resolved_failures,
            "resolved_errors_total": resolved_errors,
            "resolved_collection_errors_total": resolved_collection_errors,
        },
        "members": member_diffs,
        "exit": exit_code,
        "note": "exit 0 does not convert a native pytest exit 1 into a passing suite; native exits are preserved per member",
    }
    return exit_code, diff


# ---------------------------------------------------------------------------
# Output writing
# ---------------------------------------------------------------------------

def write_new_json(path_str: str, payload: dict, label: str) -> None:
    path = Path(path_str)
    if path.exists() or path.is_symlink():
        raise InvalidInputError([f"{label}: output path already exists, refusing to overwrite: {path_str}"])
    if not path.parent.is_dir():
        raise InvalidInputError([f"{label}: output directory does not exist: {path.parent}"])
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    loads_strict(text, f"{label} (serialized round-trip)")
    temporary = path.parent / f"{path.name}.tmp-{uuid.uuid4().hex}"
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _print_record_summary(baseline: dict) -> None:
    print(f"recorded {baseline['schema']} scope={baseline['scope']}")
    for member in baseline["members"]:
        counts = _member_counts(member)
        print(
            f"  member {member['kind']} capture={member['capture_id']}: "
            f"selected {counts['selected']}, ran {counts['ran']}, passed {counts['passed']}, "
            f"failed {counts['failed']}, errors {counts['errors']}, "
            f"collection_error_files {counts['collection_error_files']}, overall_exit {member['parent']['overall_exit']}"
        )
    print(f"  population disjoint, selected_total={baseline['population']['selected_total']}")


def _print_compare_summary(diff: dict) -> None:
    summary = diff["summary"]
    print(
        f"compared scope={diff['scope']}: added failed {summary['added_failed_total']}, "
        f"added errors {summary['added_errors_total']}, "
        f"added collection errors {summary['added_collection_errors_total']}, "
        f"resolved failed {summary['resolved_failed_total']}"
    )
    for member in diff["members"]:
        native = member["native"]
        print(
            f"  member {member['kind']}: baseline exit {native['baseline_overall_exit']} -> "
            f"candidate exit {native['candidate_overall_exit']}"
        )
    for finding in diff["identity_findings"]:
        print(f"  finding: {finding}")


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="compare_test_baselines.py",
        description="Record and compare offline test baselines from terminal captures (K03).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    record_parser = subparsers.add_parser("record", help="record a capture-set descriptor into a baseline JSON")
    record_parser.add_argument("--capture", required=True, help="offline-capture-set-v1 descriptor JSON path")
    record_parser.add_argument("--output", required=True, help="new test-baseline-v1 output path (must not exist)")

    compare_parser = subparsers.add_parser("compare", help="compare a candidate against a baseline")
    compare_parser.add_argument("--baseline", required=True, help="test-baseline-v1 JSON path")
    candidates = compare_parser.add_mutually_exclusive_group(required=True)
    candidates.add_argument("--candidate", help="test-baseline-v1 JSON path")
    candidates.add_argument("--candidate-capture", help="offline-capture-set-v1 descriptor JSON path")
    compare_parser.add_argument("--output", help="new test-baseline-diff-v1 output path (must not exist)")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        arguments = ["--help"]
    parser = build_argument_parser()
    options = parser.parse_args(arguments)
    try:
        if options.command == "record":
            baseline = record_descriptor(options.capture)
            write_new_json(options.output, baseline, "record")
            _print_record_summary(baseline)
            return 0
        baseline = load_baseline(options.baseline, "baseline")
        if options.candidate:
            candidate = load_baseline(options.candidate, "candidate")
        else:
            candidate = record_descriptor(options.candidate_capture)
        exit_code, diff = compare_baselines(baseline, candidate)
        if options.output:
            write_new_json(options.output, diff, "compare")
        _print_compare_summary(diff)
        return exit_code
    except InvalidInputError as error:
        for finding in error.findings:
            print(f"INVALID INPUT: {finding}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
