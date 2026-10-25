# P1-6 Independent Product HARD REVIEW (p16-hard-review-002)

Date: 2026-10-25. Reviewer: independent ZCode session `sess_05fbc6b7-b371-42cd-9903-b396f5423422`
(GLM-5.3/max, unchanged configuration), continuing `p16-hard-review-001` after its CLI
ended native 1 (provider HTTP502; original failure preserved, not relabeled).
Developer: `sess_94359608-7177-48ad-a860-7c67fecefa22` (terminal; owner evidence
`p16-owner-003/004`). This reviewer took no part in development.

Candidate: `D:\AKWorktrees\aktr-r3-p16-20261025`, branch `work/zcode-r3-p16-20261025`.
Review standard: Astra effective r3 IMPLEMENTATION-PLAN §5, REPAIR-ORDER P1-6 closure
conditions ("隔离库验证语法、role 已存在/重复执行、最小权限及失败回滚，秘密值不进 argv/回显/日志；实际写集硬审"),
and the original SQL intended contract. No new acceptance criteria invented; no
plan-review PASS loop; no broader production-deployment requirements.

## 1. Frozen write-set — hashes verified at start AND end (identical to ROOT-SOURCE-RECEIPT)

| File | Bytes | SHA-256 |
| --- | --- | --- |
| `scripts/sql/create_mcp_readonly_role.sql` | 2566 | `f3cd484ab0ca04e5088f2fd3154b12ebbebf9f78aab2bc50e6cbd1b355d648db` |
| `scripts/setup_mcp_readonly.py` | 2820 | `512af47fa2554633ced08adbc0fe1bca806a08863aacd3fbb0a6c9c511758209` |
| `tests/test_mcp_readonly_role.py` | 12662 | `68466e0444f7c137c536b5758fb81b9abb4eeab345a430caff3cfe9cc7bfa121` |
| `docs/mcp-readonly-role.md` | 4565 | `f7398e486724c3571a62b6c08722e9680c4b2a64cfe3291249707fd8bdfaedf2` |

Source re-hashed again before this continuation's probes (`101-source-rehash.txt`) and after
cluster shutdown (`210-end-state.txt`): identical both times. No source file was modified by
this review. `git status --porcelain` at end shows exactly the four write-set entries and
nothing else; the transient test sibling `scripts/sql/create_mcp_readonly_role_forced_error_test.sql`
is absent after the e2e run (`095-post-e2e-treestate.txt`, `210-end-state.txt`).

## 2. Reused independent results (p16-hard-review-001, validated then reused — not re-run)

Pins validated first (`100-pin-validation.txt`): all old evidence files present with recorded
bytes/contents; source bytes unchanged, so the results below apply to the exact frozen hash set.

| Run (001 evidence) | argv/cwd | raw exit | Real counts |
| --- | --- | --- | --- |
| `070-guard-nodsn` offline guard, DSNs unset | `Python312 scripts/test_offline.py -q tests/test_mcp_readonly_role.py`, cwd candidate root | 0 | **12 passed, 3 skipped** |
| `080-guard-dsn` unchanged guard, task DSNs set | same command; `AK_MCP_READONLY_E2E_DSN`/`_READ_DSN` exported | 1 | 12 passed, **3 failed** (`OfflineViolation`: guard blocks psql subprocess) — guard intact, preserved as observed, not a product success |
| `090-e2e-pytest` authorized direct pytest | `Python312 -m pytest tests/test_mcp_readonly_role.py -v --tb=short`, cwd candidate root, DSNs via env only | 0 | **15 passed, 0 skipped** in 6.59s |

The 001 cluster (127.0.0.1:55432, PG 18.6) required **scram-sha-256 for `mcp_readonly`**
(HBA evidence `040-hba-used.txt`), so the passing read-DSN assertions are genuine password
verifications. This closes the gap in the owner's own evidence: their cluster was
`initdb -A trust` (STEP-DELIVERY §四), which accepts any password, so their read-DSN pass
alone could not prove exact hostile-password storage. Root shut the 001 cluster down after
the reviewer CLI failed (receipt: native 0, postmaster.pid absent — re-confirmed in
`100-pin-validation.txt`).

## 3. Fresh counterexamples on a NEW cluster (p16-hard-review-002, port 55433)

New throwaway PG 18.6 cluster under this work dir only (`110-initdb` … `114-connectivity`:
postmaster PID 132620, bound 127.0.0.1:55433, C locale/UTF8, same scram-for-mcp_readonly HBA).
All connections explicit `-h 127.0.0.1 -p 55433`; `PGPASSFILE`/`PGSERVICEFILE` point at
nonexistent task-local files; inherited `PG*`/`AK_DATABASE*`/DSN vars unset per command
(`probes/env.sh`). The production PostgreSQL on 127.0.0.1:5432 was never contacted.
SYNTHETIC secrets only: A = hostile quotes/semicolon/`--`/`$$`; B adds a backslash; WRONG is
deliberately incorrect (fingerprints in evidence, values never printed).

| # | Probe (argv semantics; secrets only via env) | raw exit | Result |
| --- | --- | --- | --- |
| 120 | SQL run with `AK_MCP_ROLE_PASSWORD` unset (psql -X … -f sql) | **3** | `\getenv` leaves var unset → server **syntax error at or near ":"** at the single interpolation point; ON_ERROR_STOP aborts; role count 0. Fail-closed; owner's open hypothesis pinned (mechanism = interpolation error, not DO-block RAISE) |
| 131 | Helper's **real** `run_psql()` (no stubs), password A via child env only | 0 | New role created; stdout shows only `BEGIN/DO/GRANT×6/ALTER DEFAULT PRIVILEGES/COMMIT` — no secret row ever echoed (`\gset` suppression works in practice) |
| 132 | Auth as `mcp_readonly` with A (PGPASSWORD env, SCRAM) | 0 | `mcp_readonly\|ak_trading` — hostile password stored **byte-exactly** |
| 133 | Same with WRONG | 2 | `FATAL: password authentication failed` — genuine verification, not trust |
| 134 | Admin: role attributes | 0 | `rolsuper/createdb/createrole/replication/bypassrls = f/f/f/f/f`, `rolcanlogin=t` — least-privilege attributes |
| 140 | SQL re-run with B (existing-role path) | 0 | Clean idempotent re-run |
| 141 | Auth with B / with old A | 0 / 2 | B works, **A now rejected** — ALTER genuinely rotates the password (frozen tests only re-ran the same secret; this is added discriminating power) |
| 142 | As role: SELECT the 4 allowlisted tables | 0×4 | All readable (`market_bars`=1 row) |
| 142b | As role: SELECT `probe_pre_existing` (non-allowlisted, created before grants) / `reference_snapshots.raw_json` / INSERT `market_bars` | 1 / 1 / 1 | `permission denied` each; row count stays 1 |
| 143 | Table created **after** grants, then SELECT as role | 1 | `permission denied` — `ALTER DEFAULT PRIVILEGES … REVOKE` effective |
| 145 | Admin adds extra grant, re-runs SQL (3rd time), SELECT as role; grant-count check; cleanup revoke | 0 | Extra pre-existing grant persists (matches documented "keeps its grants"); exactly 1 SELECT grant on `market_bars` after 3 runs — no duplicate grants |
| 150 | Fixed-string greps for all 4 raw SYNTHETIC secrets over every 001/002 stdout/stderr evidence file and both server logs; broad `synthetic` net (case-insensitive) on server logs | 0 | **Zero hits anywhere** — no argv/echo/log leakage (080's traceback shows only the escaped source form and percent-encoded DSN, never the runtime secret) |
| 160 | Doc claim spot-check | 0 | `scripts/run_market_mcp.py:28` calls `load_dotenv(ROOT / ".env")` with default `override=False` → process-level env wins; doc's switching guidance is factually accurate |

Forced-error whole-script rollback and the empty-password DO-block RAISE refusal were
independently verified by the reused frozen e2e (`090`: tests 2 and 3, both PASSED, no skip).

002 cluster shut down by this reviewer after all probes (`200-pgctl-stop`: exit 0,
postmaster.pid absent, `210-end-state.txt`).

## 4. Findings

**Material product defects: none.** All REPAIR-ORDER P1-6 closure conditions are met with
independent evidence. Verdict: **PASS** (unconditional; see VERDICT.json).

Non-blocking observations (recorded truthfully; none is a defect against the narrow contract):

- **O1 — transient source-tree write by the frozen test.**
  `test_e2e_forced_error_rolls_the_whole_script_back` writes a fixed sibling
  `scripts/sql/create_mcp_readonly_role_forced_error_test.sql` into the source tree and
  unlinks it in `finally` (verified absent after the run). Risk: transient tree mutation,
  collision between parallel runs, debris on hard kill. Run unmodified per work order; for
  any further probing an isolated copy was mandated. Test hygiene note for the owner; not a
  product-contract violation.
- **O2 — owner e2e was trust-auth.** Their read-DSN pass could not by itself prove exact
  hostile-password storage. Resolved by this review's SCRAM-based runs (001 `090` + probes
  132/141). Owner-evidence observation only.
- **O3 — doc nuance on refusal paths.** With the env var *unset*, failure surfaces as the
  psql interpolation syntax error (exit 3) before the DO block; the DO-block
  `RAISE EXCEPTION` is the *empty-string* path. `docs/mcp-readonly-role.md` collapses both
  into "DO 块显式 RAISE EXCEPTION". Outcome identical (non-zero exit, full rollback, no
  role); wording could distinguish the two paths.
- **O4 — drifted roles are not converged.** An existing `mcp_readonly` holding extra grants
  keeps them across re-runs (explicitly documented in the SQL header; behavior confirmed in
  probe 145). Converging a drifted role back to least privilege is an operator action,
  outside this script's stated contract.
- **O5 — discarded reviewer attempt.** A broken `eval` quoting attempt in probe 142 was
  preserved under `evidence/discarded/` (bash exit 2, no product content) and redone cleanly
  (`142b-*`). No secrets were exposed by the attempt (its outputs contain only bash errors).
- **O6 — pytest traceback echoes synthetic literal.** `080-guard-dsn.stdout` contains the
  frozen test's escaped source literal and the percent-encoded DSN via pytest's traceback.
  Both are SYNTHETIC and already public inside the frozen test file; no real secret exists
  in this review.

## 5. Boundaries (unchanged, truthfully reported)

- Production PostgreSQL (127.0.0.1:5432) never connected, restarted or modified; no
  project/production database touched; no `.env`/credentials/pgpass read; secrets are
  SYNTHETIC only and appear in no argv, echoed output or evidence file.
- Production role creation, password setting and MCP connection switch remain user-only
  operations (NOT_RUN here); **no production adoption is claimed by this verdict**.
- No source edits, no fixes, no commits/pushes, no model/configuration changes, no nested
  agents. Offline guard untouched and still enforcing (O5 in 001 evidence: raw exit 1).
- Trading boundary: this module is unrelated to order execution; `enable_trading=False`,
  observer answers hold, `broker=NOT_RUN`, real send/cancel = 0.
- Native-code discipline (AGENTS §7/§8, both incidents incl. manifest-recovery-002)
  followed: every command's raw exit/stdout/stderr captured separately and preserved as-is;
  the original CLI native 1 (HTTP502) remains recorded as a failure, not rewritten.

## 6. Evidence index

- Reused (p16-hard-review-001/work/evidence/): `010-start-hashes.txt`, `070-guard-nodsn.*`,
  `080-guard-dsn.*`, `090-e2e-pytest.*`, `095-post-e2e-treestate.txt`; `server.log`; HBA `040-hba-used.txt`.
- New (p16-hard-review-002/work/evidence/): `100-pin-validation.txt`, `101-source-rehash.txt`,
  `102-bindtest.txt`, `110-initdb.*`, `111-hba-used.txt`, `112-pgctl-start.*`, `113-createdb.*`,
  `114-*`, `120-getenv-unset.*`, `120-role-count.*`, `130-schema-prep.*`, `131-helper-real-run.*`,
  `132-auth-A.*`, `133-auth-wrong.*`, `134-role-attributes.*`, `140-rerun-B.*`, `141-auth-*`,
  `142-*`, `142b-*`, `143-*`, `145-*`, `150-secret-greps.txt`, `160-doc-claims.txt`,
  `200-pgctl-stop.*`, `210-end-state.txt`, `discarded/*`.
