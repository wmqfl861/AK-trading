# Read-only coordination readiness checker

Plan identifier: `COORDINATION-READINESS-REPORT-r1` — the small read-only
progress and identity consistency checker requested by Codex project review
report r1, section C6
(`D:/AKF260920/coordination/continue-current-model-20260922-r1/codex-project-review-report-r1.md`,
disk SHA-256 `2d48c847…cb2ccde`).

This is an isolated auxiliary reporting tool. It is **not** a new hard
prerequisite for G1 and does not replace any review. Per C6 it has no
capability to: write the register (R), modify frozen packages, start
processes, sample, issue authorizations, or change any gate state. It reads
four explicitly passed JSON files and writes two report files.

## The five checks

| id | description | fires when |
|---|---|---|
| CHK-01 | retired identity still marked as current | a register record sets `marked_current: true` for an identity the confirmed identity table lists as retired (e.g. old `freeze-r2` identity marked current after succession to `freeze-r3`) |
| CHK-02 | prerequisite not completed while successor declared READY/CLOSED | a record declares a gate `READY`/`CLOSED`/`PASS`/… while any prerequisite (per the confirmed gate table) has no completed declaration (e.g. `G10-R1 = READY` while `G09-R1 = AUTH_ISSUED` — authorization issued is not a window granted) |
| CHK-03 | payload frozen only, mis-written as sealed/reviewed | a register record writes a payload as `SEALED`/`REVIEWED`/`PASS`/… while the file table's authoritative state is frozen-only (e.g. `FROZEN_UNSEALED_FAILED_PRESEAL` back-filled as sealed) |
| CHK-04 | path / sha256 / revision transcription inconsistency | a register record transcribes a file's path, sha256, or revision differently from the authoritative file table (e.g. `runs/<run-id>` vs `rounds/<run-id>` (PG-06 class), wrong digest, `r10` vs `r_10` (lexicographic trap class)) |
| CHK-05 | missing or placeholder UTC treated as precise historical time | a record claims `precise` for a timestamp whose value is missing, a placeholder (`TBD`, `TO_BE_DETERMINED_AT_LAUNCH`, `RESERVED`, `1970-01-01T00:00:00Z`, `<…>`, …), date-only, or timezone-less |

Preparation states are never upgraded: `PREPARED`, `NOT_READY`, `NOT_STARTED`,
`BLOCKED_*`, `AUTH_ISSUED`, `WAITING_SEAT_PREPARED`, `ISSUED`, `FROZEN*`,
`REPAIR_REVIEWED_PENDING_P18` are all *not completed*. A gate counts as
completed only when **every** declaration of it is in
`{CLOSED, PASS, COMPLETED, ACTIVATED, CONTINUATION_PERMITTED}`. `READY` is a
trigger for successors but never a completion status for prerequisites.
Unknown status strings are conservatively not completed.

## Usage

```
python -m ak_trading.readiness \
  --register  <register-snapshot.json> \
  --identities <identity-table.json> \
  --gates  <gate-table.json> \
  --files  <file-table.json> \
  --out-dir <output directory>
```

Programmatic:

```python
from ak_trading.readiness import load_inputs, run_checks, write_report

inputs = load_inputs(reg_path, ident_path, gate_path, file_path)
report = run_checks(inputs)          # findings, informational, gate/identity/payload views
write_report(report, out_dir)        # writes state-snapshot.json + contradictions.md
```

Outputs (only these two files, only into `--out-dir`):

- `state-snapshot.json` — inputs with programmatically computed SHA-256,
  conservative gate view (declarations, phase, `conservative_completed`),
  identity view, payload view, per-check counts, register coverage stats,
  fixed limitations text. A *view*, never a gate mutation.
- `contradictions.md` — human-readable findings per check with record key,
  expected-vs-actual evidence, informational notes, limitations.

Exit code 0 with findings listed; 2 on malformed inputs or unsafe report
destinations (fail fast, no guessing).

### Q70 report destination and reference protection

Before `mkdir` or either report write, `write_report` validates both destinations
as one operation. Both report targets must be absent. An existing ordinary
output directory is allowed; existing targets, symlinks, junctions and other
reparse redirection anywhere in the output route are rejected. The API raises
`ReadinessInputError` and the CLI returns 2 before either write on a rejected
route. This is a local metadata preflight, not a hostile-concurrency guarantee.

The four input paths, explicit `source_register`, and every file-table `path`
(including frozen payload references) are protected. A target cannot equal a
reference or lie below an explicitly referenced directory; absent references
remain protected too. Protection uses absolute paths, Windows case/alias
identity, dot-dot normalization, resolved local ancestors and same-file
identity, separately from CHK-04's lexical comparison. Existing hardlinks are
also rejected by the absent-target policy. CLI paths are anchored at invocation
cwd; relative `source_register` is anchored at the snapshot directory, and
relative file references at the file-table directory. Literal placeholders such
as `<run-id>` stay literal and are never expanded. Only necessary local metadata
is inspected; referenced contents are not read. UNC/device paths, mapped network
drives, unsupported redirection or ambiguous paths fail with an explicit reason
before filesystem traversal that could access a network.

### Q70 CHK-04 and CHK-05 precision

CHK-04 compares path transcriptions by trimming surrounding whitespace and
translating backslashes to slashes, then comparing case-sensitively. Every other
character remains significant: roots, UNC shares, drive anchors, repeated or
trailing separators, and literal dot/dot-dot components. No filesystem
resolution or segment filtering participates in transcription equality.
Present null, non-string or blank path/revision fields produce exactly one
malformed-field CHK-04 finding with the record, file, field and original value.
Missing optional fields remain absent. Supplied fields without a usable
authoritative comparator are explicitly unverifiable, not consistent. Revision
separator differences such as `r21` versus `r_21` remain mismatches.

CHK-05 validates real dates, clocks and timezone values. Valid aware instants
are normalized to UTC when checking the reserved epoch/year-0001 sentinel
instants, including zero fractions and equivalent offset spellings. Nonzero
fractional digits are retained for this decision, even beyond microseconds.
Impossible instants are invalid; valid historical dates and leap days have no
age cutoff. Textual placeholders retain their meaning. Finding and informational
rendering use the same record/field precision claim, so a precise-claimed
placeholder cannot also be described as not claimed precise; other fields keep
their legitimate informational notes.

## Input contracts (v1)

All four inputs are explicitly passed. The tool never reads the live register
`R`; the main session captures a snapshot copy first. Natural-language
rulings are never interpreted: only the structured blocks below are read, and
prose-only records are counted (`register_stats.prose_only_records`) but not
analyzed. Top-level `provenance: "SYNTHETIC"` is surfaced in both outputs.

### 1. Register snapshot (`snapshot_kind: coordination-readiness-register-snapshot`)

```jsonc
{
  "snapshot_kind": "coordination-readiness-register-snapshot",
  "snapshot_version": 1,
  "provenance": "SYNTHETIC",            // or the capture label used by the main session
  "source_register": "dispatch-register.json@<capture label>",
  "records": {
    "<record key>": {                    // string records = prose-only, counted only
      "identity_refs": {                 // CHK-01 (all blocks optional)
        "fpcold_freeze": {"value": "freeze-r2", "marked_current": true}
      },
      "gate_states": {                   // CHK-02
        "B-G10-R1": {"status": "READY", "declared_at_utc": "2026-09-27T09:00:00Z"}
      },
      "payload_states": {"publication-r8": "SEALED"},   // register-side state write (CHK-03)
      "payload_claims":  {"publication-r8": "SEALED"},  // summary-side claim (CHK-03)
      "file_transcriptions": {           // CHK-04
        "fp-launch-binding": {"path": "...", "sha256": "...", "revision": "r_10"}
      },
      "timestamps": {"first_sample_at_utc": "TO_BE_DETERMINED_AT_LAUNCH"},   // CHK-05
      "timestamp_precision_claims": {"first_sample_at_utc": "precise"}       // CHK-05
    }
  }
}
```

### 2. Identity table (`table_kind: coordination-readiness-identity-table`)

```json
{
  "table_kind": "coordination-readiness-identity-table",
  "table_version": 1,
  "confirmed_by": "codex-project-review-report-r1 A1.2 / main-session label",
  "slots": {
    "fpcold_freeze": {"current": "freeze-r3", "retired": ["freeze-r2", "268e2045-old-freeze-id"]}
  }
}
```

Historical mentions of retired identities (`marked_current: false`) are fine;
only "marked current" contradictions fire CHK-01.

### 3. Gate table (`table_kind: coordination-readiness-gate-table`)

```json
{
  "table_kind": "coordination-readiness-gate-table",
  "table_version": 1,
  "confirmed_by": "…",
  "gates": {
    "B-G09-R1": {"sequence": 9, "prerequisites": ["B-G08"]},
    "B-G10-R1": {"sequence": 10, "prerequisites": ["B-G09-R1"]}
  }
}
```

Every prerequisite must be a defined gate. Gate *statuses* always come from
the register snapshot's `gate_states`; the table only supplies ordering.

### 4. File/payload reference table (`table_kind: coordination-readiness-file-table`)

```json
{
  "table_kind": "coordination-readiness-file-table",
  "table_version": 1,
  "confirmed_by": "…",
  "files": {
    "fp-launch-binding": {
      "path": "rounds/<run-id>/launch/binding.json",
      "sha256": "<64 lowercase hex, computed programmatically>",
      "revision": "r_10"
    }
  },
  "payloads": {
    "publication-r8": {"payload_state": "FROZEN_UNSEALED_FAILED_PRESEAL", "files": ["pub-r8-binding"]}
  }
}
```

`sha256`, when present, must be 64 lowercase hex characters (reject manual or
truncated digests at load time).

## SYNTHETIC fixtures and self-test

`fixtures/synthetic/` holds labeled SYNTHETIC inputs mirroring this case's
error classes (identity succession freeze-r2→freeze-r3, AUTH_ISSUED≠window,
failed-preseal candidate kept frozen, `runs/` vs `rounds/`, `r10` vs `r_10`,
UTC placeholders):

```
python -m pytest src/ak_trading/readiness/tests/test_checker.py -q
```

Expected: all pass. The broken fixture yields exactly
`CHK-01=1, CHK-02=1, CHK-03=1, CHK-04=3, CHK-05=1`; the clean fixture yields
zero findings and proves preparation states (including `AUTH_ISSUED`) are
never upgraded to completed. Tests also pin the forbidden-capability boundary:
module sources import no socket/subprocess/http modules, and the only writes
a run performs are the two report files (inputs verified byte-identical).

## Module layout

```
src/ak_trading/readiness/
  __init__.py      public API
  __main__.py      CLI entry (python -m ak_trading.readiness)
  models.py        input contracts, validation, state vocabulary
  checker.py       five checks, snapshot/markdown rendering, CLI
  fixtures/synthetic/  SYNTHETIC self-test inputs (broken + clean + tables)
  tests/test_checker.py
```
