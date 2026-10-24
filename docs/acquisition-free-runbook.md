# W2 Free-Channel Acquisition Runbook (ACQ-W2-DEV-20260919-r1)

Status: DEVELOPMENT ONLY. No independent review seal exists; `run`/`resume`
therefore ALWAYS refuse (exit 2) until the gates in H15 are satisfied in
order: implementation delivered -> independent software review PASS -> concrete
run order -> explicit user confirmation. This runbook contains no credentials
and no live endpoints.

## 1. Components

| Component | Path (relative to repository root `D:/Projects/AK-trading/`; migrated to final src/ location per ACQ-W2 clarification Q6 migration r1) |
| --- | --- |
| Policy (frozen copy, byte-identical to arbitration) | `config/execution-policy.frozen.json` (sha256 `cd2a5c31...41eed83`) |
| CLI entry | `scripts/acquire_futures_free.py` |
| Package | `src/ak_trading/acquisition/` (contracts, identity, calendar, tq_adapter, capture, artifacts, storage, scheduler, cli) |
| Tests (SYNTHETIC) | `tests/` (incl. real-PG disposable-instance test) |
| Offline evidence driver | not migrated; preserved at `data/maintenance_artifacts/acquisition-w2-collector-20260919-r1/scripts/synthetic_evidence_run.py` with its frozen SYNTHETIC evidence tree |

## 2. Commands and expected exit codes

```text
python -B -X utf8 scripts/acquire_futures_free.py plan --policy <policy.json> --inventory <inventory.json> [--calendar-evidence <evidence.json>] --output <new_plan_dir>
python -B -X utf8 scripts/acquire_futures_free.py verify-package --package <package_dir> --offline
python -B -X utf8 scripts/acquire_futures_free.py run --plan <frozen_plan.json> --authorization <user_confirmation.json> --review-seal <review-seal.json>
python -B -X utf8 scripts/acquire_futures_free.py resume --plan <same_plan.json> --authorization <same_valid_authorization.json> --review-seal <review-seal.json>
```

| Exit code | Meaning |
| --- | --- |
| 0 | success / package verified |
| 2 | gate refusal: authorization, hash binding, review seal, session window, isolation, or credential presence — raised BEFORE any TqApi/engine object is constructed |
| 3 | blocked: account authentication / account-level permission denial |
| 4 | execution failure or package integrity problems |
| 5 | DEFERRED_BUDGET: policy budget exhausted; clean partial progress, resumable |

## 3. Run order template (to be filled by the main session per run)

```json
{
  "run_order_id": "<dated id>",
  "policy_path": "data/maintenance_artifacts/acquisition-execution-arbitration-20260919-r1/execution-policy.json",
  "policy_sha256": "cd2a5c314394ef52a8e7b2b3f8374ce0c76c18d06a5f2f5bb2259ab1441eed83",
  "review_seal_path": "<path to the W2 software-review PASS seal>",
  "code_manifest_sha256": "<from `plan.json#/code_manifest_sha256` at freeze time>",
  "plan_path": "<frozen plan.json>",
  "output_root": "<new dedicated output directory>",
  "db_target_non_sensitive": "<host:port/dbname of the acquisition_w2 schema target>",
  "session_window": {"start": "<ISO+08>", "end": "<ISO+08>"},
  "budget_caps": {"max_serial_attempts": 24, "max_wall_seconds": 1800},
  "protected_paths": ["<cold-chain dirs>", "<active review frozen sets>", "..."],
  "protected_db_objects": ["public.market_bars", "<legacy ingestion/reference tables>", "..."],
  "health_checks": [
    {"name": "observer_bridge_alive", "kind": "process_alive", "target": "<known pid>"},
    {"name": "cold_chain_marker", "kind": "file_exists", "target": "<known marker file>"}
  ],
  "allow_subsequent_batches": false
}
```

## 4. User confirmation binding method (H01)

The user's run confirmation must be recorded by the main session as
`user_confirmation.json` referencing, by SHA-256: the policy file, the plan,
the review seal, and the code manifest, plus the output root, the
non-sensitive DB target identity, the session window, budget caps, the
protection list, and expiry. `approver` must be `HUMAN_USER`. The collector
refuses agent-signed, expired, out-of-scope, or hash-mismatched
confirmations, and never self-signs. Credential VALUES never enter any file;
the run reads `TQ_USER`, `TQ_PASS`, `AK_DATABASE_URL` from the process
environment only (no .env loading anywhere in the package).

## 5. Abort and resume

- Abort: terminate the process. Every artifact file is atomically published;
  the task lease (`task-lease.json`) goes stale and is recoverable only after
  the holder PID is verified dead; attempt counters and `next_eligible_at`
  persist in `run-state.json` and are NOT reset by restart.
- Resume: re-issue `resume` with the same plan and a still-valid
  authorization. Committed ingest batches are recognized by immutable batch
  identity and recovered with zero re-inserted rows; interrupted chunks
  re-run idempotently (row-level ON CONFLICT dedup + stored-disposition
  recounting).
- Rate limit: the worker pauses honoring max(60 s, vendor Retry-After); two
  consecutive account rate limits stop new requests for the run order
  (remaining tasks -> DEFERRED, resumable later).
- Account denial: run exits 3; account tasks stop immediately; no channel or
  account switching (paid channels permanently excluded).

## 6. Failure/recovery matrix

| Interruption point | State on disk | Recovery behavior |
| --- | --- | --- |
| during serial download | no raw file published | attempt counter +1, backoff 60/300/900 s, max 4 attempts incl. initial |
| raw published, not registered | raw file + no manifest entry | next attempt republishes under a new attempt id; old file retained as evidence |
| raw verified, DB chunk mid-commit | committed chunks recorded in `ingest_batches.chunks_committed` | resume re-runs only missing chunk ranges |
| DB committed, checkpoint unwritten | batch identity in DB | `find_batch` recovers; zero new rows; INGEST_VERIFIED after reconciliation |
| manifest/acceptance interrupted | product files partially finalized | verify-package reports the missing files; re-run finalizes only absent files |

## 7. Resource and isolation notes (H10/H12)

The collector runs as a single process, below-normal priority, one TqApi
connection, <=4 symbols / <=8 serials per group, <=24 serial attempts and
<=30 min per run order, <=2 uncommitted raw snapshots, 1 GiB RSS cap, disk
floor 60 GiB free after reservations (>=2 GiB batch peak), and refuses to
start when health signals or the protection list are missing. It never
kills, restarts, or reconfigures existing services, never writes outside its
output root, and rejects junction/symlink path escapes.

## 8. PostgreSQL test instance (reviewer)

`tests/test_acquisition_postgres.py` creates its OWN disposable cluster
(initdb into a temp dir, trust auth, random port, database
`ak_acq_w2_test_<nonce>`), starts and stops it via pg_ctl with file-redirected
stdio, and deletes it afterwards. It must never be pointed at an existing
instance; if PostgreSQL binaries are absent the module skips with an honest
NOT_RUN marker (mocks are not acceptable substitutes per dev-plan section 7).
Note for Windows: pg_ctl's stdio must not be pipes — the postmaster inherits
the handles and deadlocks the caller.

## 9. What this package deliberately does NOT do

- No paid channel, no `DataDownloader`, no account/channel fallback (H01).
- No writes to `market_bars` or any legacy ingestion/reference table; writes
  only the `acquisition_w2` schema's three tables via the project
  `create_database_engine` factory with explicitly qualified names (H08).
- No SCENARIO calendar or zero-filling; unknown denominators stay null (H04/H13/H14).
- No scheduler installation; no Windows Task Scheduler jobs are created.
