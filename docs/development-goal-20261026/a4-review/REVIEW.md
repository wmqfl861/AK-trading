# A4 finite shadow-replay entry guard — independent product hard review (a4-hard-review-001)

**Verdict: PASS (unconditional).** Product hard review of the frozen two-file A4 write set in
candidate `C:/AKWorktrees/aktr-goal-a4-20261026`, performed by an independent ZCode session that
took no part in the module's development or repair (owner a4-owner-001 reached TERMINAL native 0 /
WRITE_STOP before this review started). Review standard: effective `PHASE0-PLAN.md` §6 and
`PHASE0-DISPOSITION.md` (EFFECTIVE) — exactly the versions hash-pinned to this review's
REQUEST.json (`/inputs/273`, `/inputs/274`); no acceptance criterion was invented or relaxed.

## Scope and write set

| file | status | bytes | sha256 |
| --- | --- | --- | --- |
| `scripts/research_probability_v2.py` | modified | 6918 | `db0e61e37b6213c776a36625769e7391439628f0c0c451415471a68b0bc685ba` |
| `tests/test_v2_m5_shadow_entry_guard.py` | NEW | 5827 | `ddae2262b68756662cbdc90d37d5500bc63c9e9e2ea0b8a87cf58356b5e9ac28` |

Nothing else changed: a full recursive walk of the candidate finds **exactly** the 266
ROOT-FROZEN-INPUTS files (manifest sha256 `a7307863ad2e4472901ad9f643d88d35a4be3adc58177caa5ccf5f7ce4cb3e4d`, REQUEST pin `/inputs/267`), all
byte-identical **before and after** this review's runs — 266/266 OK both times, zero extra or
missing files, no strays, no `__pycache__`. The untouched original `tests/test_v2_m5_cli.py`
hashes to `d885da1717e0982b455248275fe670e07c96cdcbac17f1a8093fd7cac072f693`.

## Plan conformance (code + bytes)

- **Guard position**: `main()` raises `StageIncompleteError("ORDINARY_SHADOW_REPLAY_NOT_AVAILABLE")`
  for `--mode shadow-replay` immediately **after** the two feed/reference relationship checks and
  **before** `output = _path(args.output)` and any config/bundle/feed/lock read, fit, registration
  or holdout action — the exact placement mandated by §6.
- **Byte-delta proof**: the entire script change is one 724-byte / 10-line block
  (`if args.mode == "shadow-replay":` … `raise StageIncompleteError(…)`); deleting it
  reconstructs 6194 bytes hashing to the pre-change snapshot pin `33351e7acd0004701ae07239ba0bd44710288b0f93cb4f28072f8cf6067c2b9f`.
  Pure LF preserved (0 CRLF). Mode names and original argument-syntax checks are unchanged
  (raw argparse usage captured in probes P03/P04 still lists all five modes and the
  mutually-exclusive source group).
- **Exit semantics**: the existing handler is reused → native exit **3** with the structured
  failure JSON on stderr and empty stdout; because the guard fires while `output` is still None,
  **no output directory and no `error.json` are created**. Relationship/parser errors keep native
  exit **2** and fire first. The later (future full-wiring) shadow-replay branch is retained
  untouched; other modes are byte-unaffected and keep their original pipeline (P09/P10).

## Required independent rerun (all 18)

```
D:\Programs\Python\Python312\python.exe -B -X utf8 scripts/test_offline.py -q tests/test_v2_m5_shadow_entry_guard.py tests/test_v2_m5_cli.py
```

cwd = candidate root; explicit `PYTHONPATH=<candidate>/src`; module-resolution probe confirms
every `ak_trading` module resolves under the candidate (no MAIN editable-install borrow);
interpreter sha256 `4d6f5f81a4bca11191c4c7c6b43632694d0a4ce74e068619d8fdc161d469859a` (REQUEST `/python` pin).

**Raw exit 0** — `Offline totals: selected 18 (+0 deselected), ran 18, passed 18, failed 0, skipped 0, errors 0, collection_error_files 0, unresolved_files 0`. Single normal domain, stderr empty (0 bytes), stdout 2750
bytes. Wall **89.297 s** (bound 3600 s). **Measured** process-tree peak RSS
**106.6 MiB** (111,788,032 bytes across 120 psutil samples; 3 child PIDs observed, none alive
after termination) — the 2 GiB process-tree cap is proven by direct measurement, not a free-RAM
proxy; output streams far below the 512 MiB cap. All three product-file hashes identical
before/after the run. Full record: `evidence/required-rerun-r1/RESULT.json` plus raw
`stdout.txt` / `stderr.txt` / `cmd.txt` / `exit.txt`.

## Reviewer counterexamples (11 probes, real CLI processes)

| id | probe | expected → observed |
| --- | --- | --- |
| P01 | shadow-replay without `--feed`, all paths missing | exit 2 `feed_required_only_for_shadow_replay` → match |
| P02 | shadow-replay + `--feed` + `--reference-run` | exit 2 `reference_required_only_for_reproduce` → match |
| P03 | `--fixture` and `--input-bundle` both given | argparse exit 2, sources mutually exclusive → match |
| P04 | `--mode live` | argparse exit 2, invalid choice → match |
| P05 | all inputs missing, output parent nonexistent | exit 3, exact guard JSON, zero files created → match |
| P06 | output parent is an existing FILE | exit 3, sentinel byte-identical → match |
| P07 | pre-existing output dir | exit 3, dir byte-identical, no `error.json` (guard precedes `output_directory_exists`) → match |
| P08 | complete legal request, real fixture/config/feed | exit 3, no output dir created → match |
| P09 | smoke + missing config | exit 2, `error.json` written (original pipeline unchanged) → match |
| P10 | reproduce + reference-run + missing config | exit 2, `error.json` written → match |
| P11 | `--input-bundle` source variant | exit 3, zero files created → match |

All 11 matched. Precedence chain established at process level: argparse 2 → relationship 2 →
guard 3 (exact structured JSON, empty stdout, zero filesystem effects) → ordinary pipeline
errors 2 with normal output creation for non-shadow modes. Both source variants are guarded —
**no guard bypass found**. Raw argv/stdout/stderr/listings preserved under `evidence/probes-r1/`.

## Findings

Seven findings, **zero blocking** (details in `FINDINGS.json`): guard conforms to §6 (F01);
required population green (F02); precedence verified (F03); frozen integrity verified (F04);
the owner's historical baseline 11P1F / combined 17P1F runs were a Root thin-input delivery
failure (six m5-r4 chunk configs absent), documented in `A4-INPUT-HANDOFF-001.json`, original
failures preserved, superseded by owner final-tests-r2 (raw 0, 18/18) and this review's
independent rerun — not a product regression (F05); the owner's RAM evidence used a
free-physical-memory proxy, now closed by this review's direct process-tree measurement
(F06, non-blocking); and a reviewer-side Phase-A extractor off-by-one was corrected before any
conclusion, attempt1 preserved (F07).

## Boundary statements

This PASS closes only the A4 entry-guard protection slice ("late failure with unclear
message"). It does **not** close P0-2 shadow or P1-2, does not modify T8 or any acceptance
artifact, and full ordinary shadow remains NOT closed, per §6. Safety: `enable_trading=False`,
observer hold, broker `NOT_RUN`, real send/cancel `0/0`; no credentials, network, DB, broker,
service or UI access; no git operations; no model/LLM configuration changes; candidate, source,
fixtures and owner artifacts were read-only throughout — this review wrote only under its
dispatch directory.

Generated 2026-10-26T13:04:13.080110+00:00.
