# Data Pipeline Runbook (DP08 r2, OFFLINE_ENGINEERING_RELEASE)

- Work order: `docs/devplan-datapipeline-20260923-r1/work-orders/DP08-integration.md`
- Evidence: `docs/datapipeline-evidence-20260923-r1/DP08/r2/**` (r2; r1 superseded)
- Release identity: `release-manifest.json` (built programmatically from the seven node freeze manifests; verify with `scripts/verify_datapipeline_release.py`)
- Date: 2026-09-23 · Mode: OFFLINE / SYNTHETIC — no network, no live database, no SDK, no `.env`, no git write

> **Status in one line:** OFFLINE_ENGINEERING_PASS candidate (owner self-test green, independent review pending). LIVE collection, production migration and scheduled tasks are **NOT_RUN** (see "Not-run register" below). **No old consumer is switched to the new pipeline.**

## 1. What is released

The six delivered nodes (DP01 contracts, DP02 storage/migration, DP03 collector, DP04 calendar, DP05 validation, DP06 monitoring, DP07 legacy remediation) integrated by DP08 into one verified release:

| Piece | Authoritative location (this round) | Frozen by |
| --- | --- | --- |
| DP01 contracts/ports/policy + config examples | `docs/devplan-datapipeline-20260923-r1/DP01/r1/src|config/**` | `dp01-freeze-manifest.json` |
| DP02 storage/models/migrate/readback + `migrations/datapipeline/**` | `docs/devplan-datapipeline-20260923-r1/DP02/r1/**` | `dp02-freeze-manifest.json` |
| DP03 collector/scheduler/session_factory + collect CLI | `src/ak_trading/datapipeline/{collector,scheduler,session_factory}.py`, `scripts/collect_datapipeline.py` | DP03 `file-manifest.json` |
| DP04 calendar + evidence schema | `src/ak_trading/datapipeline/calendar.py`, `config/datapipeline/calendar-evidence.schema.json` | `dp04-delivery-manifest.json` |
| DP05 validation/gaps + validate CLI | `docs/devplan-datapipeline-20260923-r1/DP05/r1/src|scripts/**` | `dp05-freeze-manifest.json` |
| DP06 monitoring/reporting/dashboard + report CLI + task scripts | `src/ak_trading/datapipeline/{monitoring,reporting,dashboard}.py`, `scripts/{report_datapipeline.py,run_datapipeline_daily.ps1,install_datapipeline.ps1}` | DP06 `file-hashes.json` |
| DP07 legacy/rank_recovery + known-gaps | `docs/devplan-datapipeline-20260923-r1/DP07/r1/src|config|scripts/**` | `dp07-freeze-manifest.json` |

The candidate tree `D:/Projects/AK-trading-datapipeline-20260923-r1/` is **not materialized** (DP00 NR-05). Until an authorized materialization, the integrated package is assembled by `tests/datapipeline/fixtures/DP08/bootstrap.py` into `.tmp/datapipeline/DP08/staging/src` with every file hash-verified against its owning node's freeze manifest (drift = refusal); the two DP-X08FIX-fixed files pin their post-fix sha256 via the documented overlay (r2).

## 2. Exact entry points

| Purpose | Command (repo root) |
| --- | --- |
| Integration tests (V12/V13, G1–G4) | `python -B -m pytest tests/datapipeline/test_end_to_end.py tests/datapipeline/test_boundaries.py tests/datapipeline/test_release.py --confcutdir tests/datapipeline -p no:cacheprovider --basetemp .tmp/datapipeline/DP08-r2` |
| Release verification (independent reviewer) | `python -B scripts/verify_datapipeline_release.py --manifest docs/datapipeline-evidence-20260923-r1/DP08/r2/release-manifest.json --output <fresh-dir-outside-frozen-faces>` |
| Build a NEW release manifest (after node revisions) | `python -B scripts/verify_datapipeline_release.py --build-manifest <new-manifest.json> --dp08-summary <dp08-summary.json>` |
| Offline collection (SYNTHETIC only; `--real-source` refuses with NOT_RUN before any SDK import) | `python -B scripts/collect_datapipeline.py --output-root <explicit-root>` |
| Offline validation report | `python -B scripts/validate_datapipeline.py --spec <spec.json> --output-root <explicit-dir>` |
| Offline monitoring report | `python -B scripts/report_datapipeline.py <explicit input/output roots>` |

Exit codes follow the frozen DP01 `CLIExitCode` (0 only when nothing known-bad remains).

## 3. Migration and schema state

- Migration chain: `migrations/datapipeline/**` (isolated directory), single explicit revision **`20260923_dp01`**, version table **`datapipeline_v1.alembic_version`** (schema-qualified).
- The root Alembic chain (`migrations/versions/2026073*_000{1,2,3}*.py`) is untouched and never stamped: root-version-table writes = 0 (asserted in the V12 boundary tests). `upgrade head` on the root chain, merges and stamps are forbidden (D04).
- Objects created by `20260923_dp01`: `acquisition_w2.{bar_versions, ingest_batches, batch_rows}` (adopted after full signature comparison; drift = stop) + `datapipeline_v1.{pipeline_runs, task_checkpoints, observation_records, pipeline_commits, quality_findings, status_events}`.
- Storage runtime proven this round: SQLite double (logic). Isolated PostgreSQL = **NOT_RUN** (no database injected).

## 4. Policy / universe / calendar / generation versions

| Input | Version pin |
| --- | --- |
| Policy | DP01 `config/datapipeline/policy.example.json` (default mode OFFLINE_SYNTHETIC; real entries must be empty) — sha256 in DP01 freeze manifest |
| Universe | DP00 `universe.json` (942 symbols × 2 durations = 1884 tasks), sha256 `4e795222…589874` re-verified programmatically by DP01/DP02 tests |
| Calendar evidence | only the DP00 2026 exchange-level closed-dates anchor (`config/china_futures_catalog.json`, sha256 `411b7228…57af`, review_required carried verbatim); per-product session grids exist only as SYNTHETIC fixtures; pre-2026 = permanent UNKNOWN_CALENDAR |
| Bar generation evidence | UNKNOWN for the real free channel (no FIXED_SLOT/TRADE_TRIGGERED proof); every real expectation stays EXPECTATION_UNKNOWN |

## 5. Task states, recovery and stop procedures

- Task lifecycle (frozen): PLANNED → RUNNING → RAW_VERIFIED → INGEST_VERIFIED → ACCEPTED, plus RETRYABLE_FAILURE / PERMISSION_DENIED / UNSUPPORTED / SOURCE_EMPTY_UNEXPLAINED / QUARANTINED / OUTSIDE_FREE_WINDOW / DEFERRED_BUDGET. Run terminal states: SUCCEEDED(0) < WITH_KNOWN_GAPS(3) < DEFERRED_BUDGET(4) < RECOVERY_REQUIRED(5) < FAILED(2).
- **Recovery**: interrupted commits leave an unpublished batch (invisible to consumers). Restart = new `SqlPipelineStore` over the same database → `recover(batch_id)` (`COMPLETED_BY_RECOVERY` when the sidecar exists, `RESUMED_UNPUBLISHED` → re-drive `commit()` with the same envelope; both proven exactly-once on SQLite).
- **Stop (graceful)**: `engine.stop()` — no new tasks, queue drained, unconfirmed snapshots persisted to `spool/<worker>/pending.jsonl` (never dropped), only confirmed commits count.
- **Stop (rollback)**: stop new scheduling only; keep raw evidence, versions and events. Never drop tables or overwrite history (H08.9).

## 6. New vs. old data consumption (IMPORTANT)

- **Old consumers keep reading the old entry points unchanged.** Nothing writes to `public.market_bars`, no scheduled task is registered or replaced, and the legacy importers/rank queries keep their QUARANTINED_LEGACY dispositions (DP07 ledger).
- New read entry: `PipelineStore.query_available(as_of_nano, dataset, duration_seconds[, source_symbol])` — published-only (pipeline_commits AVAILABLE with `available_at <= as_of`), CLOSED+VALID rows, unresolved CONTENT_CONFLICT keys excluded, PROVISIONAL/UNKNOWN never visible.
- `source_symbol` scoping works as documented (X-08-5 fixed at DP-X08FIX r1, re-verified at DP08 r2: per-symbol views return exactly that symbol's published rows).
- Legacy data surfaced only as LEGACY_UNVERIFIED with explicit dispositions; it never mixes with pipeline versions (origin isolation is enforced by the store, tested both directions).
- Overlap between the old and new datasets must be consumed by explicit preference with source de-duplication — never by summing both.

## 7. Not-run register (all of it)

Full list: `docs/datapipeline-evidence-20260923-r1/DP08/r2/not-run-register.json` (r2; supersedes the r1 register). Headlines:

1. **V15 live canary — NOT_RUN.** No user-controlled live window ran; the release verifier refuses any V15 RUN claim without injected canary evidence.
2. **PostgreSQL legs — NOT_RUN** (V03/V05 acceptance, V04 PG crash points, V14 PG capacity/EXPLAIN). Isolated database not injected; SQLite proves logic only.
3. **Real TqSdk sessions — NOT_RUN.** Real-source entry is refused before any SDK import.
4. **Full collector→publish chain — RESOLVED at r2.** X-08-1/X-08-2 were fixed by DP-X08FIX r1; the chain (collect → publish → symbol-scoped view → validate → monitor, single run identity) is pinned by `test_v13b_collect_publish_validate_monitor_single_identity`. The four X-08 pins now assert the FIXES.
5. **Monitor over real collector events — healthy at r2.** X-08-3 fixed: commit-failure reasons carry the `COMMIT_FAILED:` prefix and heartbeats carry `heartbeat_at_nano` + `run_id`; both detections proven over real collector outputs.
6. **Physical junction checks, candidate-tree materialization, production migration, task registration, outbound alerts — NOT_RUN** (separate authorizations).

## 8. Reviewer re-run protocol (H08.7)

1. `python -B scripts/verify_datapipeline_release.py --manifest docs/datapipeline-evidence-20260923-r1/DP08/r2/release-manifest.json --output <your fresh dir>` → expect exit 0 with every manifest-listed file byte-identical and zero gate errors (the two DP-X08FIX files pin their post-fix sha256 via the documented overlay).
2. Re-run the §2 integration test command → expect **48 passed, 1 skipped** (r2; the skip is the explicit PG gate; r1 was 47/1 with the four defect pins asserting the defects).
3. Counterexamples: tamper any file listed in the release manifest (verifier names the exact path); revert either DP-X08FIX fix (the corresponding fix pin fails loudly); try `--output` inside a frozen face (refused).
4. Review freeze discipline: DP08's write set is frozen from this revision (r2); owner repairs require r3 and fresh independent review.
