# DP08 r3 bounded release-manifest successor — independent hard review

- Review seat: independent ZCode GLM-5.3/max, session took no part in the DP08 r3 plan or implementation
- Date: 2026-10-25 (UTC) · Work order: `D:\Maintenance\aktr-development-20261025\dp08r3-hard-review-001\WORKORDER.md`
- Review stage: `D:\AKRV261025\DP08R3-001` (137 frozen inputs, byte-exact copies of canonical originals)
- Standard: DP-HARD-r2 verdict PASS routing/carry_forward + C05-PLAN section 2 (effective 2026-10-25), under canonical AGENTS.md / PROJECT-MODEL-ROLES.md / PROJECT-WORKFLOW-RULES.md

## Verdict: PASS (unconditional, for the bounded 9-file scope only)

The existing DP08 r3 release-manifest successor is accepted exactly as frozen. This PASS covers only
`docs/datapipeline-evidence-20260923-r1/DP08/r3/**` (9 files). It is **not** a formal/overall DP08
PASS: register entry NR-DP08-08 (independent hard review of DP08 — blocks: formal DP08 PASS) and every
other inherited NOT_RUN stay in force, untouched. No repair was performed; no self-repaired artifact
was signed.

## Accepted hashes (all verified against the immutable canonical originals)

| File | SHA-256 |
| --- | --- |
| DP08-r3-report.md | `770045088b3cd549be6dac84e9c269dbbbd65478523faf7061ca45c3d4d9013d` |
| build_evidence.py | `9b7fdde19a0ae63e2923071c9699f08234618c683ec551167c46d27ae524d2e3` |
| manifest-diff-r2-r3.json | `c60d2cc5bc98e1034d8f738383f4c8bd2e6e279696a5186b8a1899d487f7433e` |
| not-run-register.json | `7395204f3e7357a9f45c320b65add4d632144e1445d4bd1d9059ae41c1cd28eb` |
| r2-manifest-recheck.json | `ebcb17ce602cc45b5dd4ad7a1126a779adb5c86536717a0ab75c78847f23a345` |
| release-manifest.json | `943dbe26e3b80fe9ea46d916c2b6e6efef0940297f91c9845974cb5307b80980` |
| release-verification-summary.md | `58a6e739f1f0aaf958a84f839cbbdea93c01d5b2ca72dbe4d5d8769aca161e17` |
| release-verification.json | `c8d6db813412323c43914d2e48849f0a40ddbf051df1d5458305be5b317e1da8` |
| verification-runs.json | `86d178bd515eb21927e440a4aaf5a48ca63ca6831899cd9f318371f41c169853` |

## 1. Immutable originals and exact review copy

All 137 STAGE-INPUTS entries re-hashed programmatically: canonical originals match the frozen pins
137/137, and every staged copy under `D:\AKRV261025\DP08R3-001` was byte-equal to canonical at review
start. After all review activity, canonical originals still match the pins 137/137 — zero product
edits by this review. The only stage-vs-canonical delta at close is the stage r3
`release-manifest.json`, which is the builder-reproduction output (pristine copy preserved in
`stage/.review-r3-baseline/`), plus recorded input copies and `.tmp` run outputs (all stage-internal).

## 2. Routing authority

DP-HARD-r2 (verdict PASS, 2026-09-23) `carry_forward` routes exactly this work: "the release manifest
now needs a revision pass to re-pin the three DP03 r2 files (bookkeeping for the release owner)". Its
`reviewed_bytes` equal the three r3 pins. C05-PLAN §2 independently records the same three-file
acceptance chain (DP03 r1 → DP-X08FIX → DP03 r2) and assigns the release owner to publish the new
consumption list. The r3 successor is the legitimate, in-scope execution of that routing.

## 3. Structural acceptance — 38/38 independent checks (work/02-structure.json)

- Own r2→r3 diff: pathset 117 == 117, **exactly 3 changed entries** (`collector.py`
  `f5cf3d0e→64387dda`, `synthetic_doubles.py` `5afba7eb→c9318daf`, `test_collector.py`
  `8363b95c→3ca9617b`; changed fields only `sha256`+`pin_source`), **114 byte-identical**.
- CAS/provenance chain closed independently against the source records (not the r3 artifacts):
  collector `82e5615b` (DP03 r1 freeze) → `f5cf3d0e` (DP-X08FIX after == r2 pin) → `64387dda`
  (DP03 r2 after == r3 pin == live canonical bytes == DP-HARD-r2 reviewed_bytes); the other two
  files' r2 pins equal their DP03 r1 freeze pins → DP03 r2 after == r3 pin == live == reviewed_bytes.
- DP03 r2 unchanged 9 files: before == after == r3 pin == live bytes.
- Non-files top-level deltas: only `built_at_utc`, `pin_overlays` (dp03_r2 record + superseded flag
  on the collector x08fix entry; readback.py x08fix pin still effective), `notes` (one appended
  note). `schema`/`v_matrix`/`node_test_results`/`dp08` identical; V15 stays NOT_RUN.
- NOT_RUN register: r2's 8 entries inherited verbatim with recorded source sha256; no status change,
  no additions, no deletions; no new leg claimed anywhere.
- All r3 evidence artifacts (recheck, both verification runs, diff, summary) are internally
  consistent with the claims in DP08-r3-report.md.

## 4. Independent verifier re-run on canonical originals (current evidence)

```
python -B -X utf8 scripts/verify_datapipeline_release.py \
  --manifest docs/datapipeline-evidence-20260923-r1/DP08/r3/release-manifest.json \
  --output work\verify-canonical-001        → native exit 0
```

CLEAN · 117/117 byte-identical · drift 0 · missing 0 · dependency pin mismatches 0 · gate errors 0 ·
V15 NOT_RUN. This is a fresh 2026-10-25 run against the immutable canonical tree, not the historical
117/117.

## 5. Builder reproduction inside the isolated review stage

Source inspection first: builder and verifier are file-only (hashing, JSON, subprocess re-invocation
of the verifier itself); no network/DB/SDK/.env/git access; all writes confined to the stage.

The 137-file stage initially lacked 10 builder-required inputs cited in the verifier source
(7 `NODE_MANIFESTS` freeze manifests, `DP-X08FIX/r1/fix-manifest.json`, `DP08/r1/file-manifest.json`,
and the DP04 pytest log whose result the DP04 delivery-manifest payload omits). These were copied
read-only from canonical into the stage with bytes/sha256 recorded (work/04, work/06) per the work
order's cited-required-records allowance.

- Attempt-001: builder aborted at its diff gate on `{node_test_results, v_matrix, repo_root}` —
  diagnosed (work/05b) as the DP04 harvest gap plus the stage root; evidence preserved.
- Attempt-002 (after staging the cited log): preflight reproduced exactly (frozen r2 manifest replay
  over the live tree: exit 1, drift == exactly the routed triple, 114/117, no other blocks); the
  manifest rebuild passed every overlay assertion (path-already-pinned, CAS, live-tree hash+bytes,
  changed-set == triple, unchanged-9); the pipeline then aborted on `{repo_root}` **only** — the
  builder's own strictness about a root different from canonical, unavoidable in any isolated
  review stage, and precisely the review-root metadata difference the work order told me to record
  separately.
- Reviewer-completed equivalents of the aborted steps: the rebuilt r3 manifest is **identical to the
  canonical frozen r3 manifest** in `files[117]`, `pin_overlays` (including the dp03_r2 chain
  records), `v_matrix`, `node_test_results`, `notes`, `dp08`, and `schema` — differences exactly
  `{built_at_utc, repo_root}`. The regenerated `r2-manifest-recheck.json` is byte-identical to
  canonical. The rebuilt manifest independently verifies CLEAN 117/117, exit 0, over the stage tree.

## 6. Counterexample probes (4/4, labeled SYNTHETIC copies under work/)

| Probe | Mutation | Expected rejection | Observed |
| --- | --- | --- | --- |
| P1 | one byte flipped in non-triple pinned `scripts/report_datapipeline.py` | exit 1, DRIFT exactly that path | exit 1, DRIFT `scripts/report_datapipeline.py` ✔ |
| P1-restore | byte restored | exit 0 CLEAN | exit 0, CLEAN 117/117 ✔ |
| P2 | one byte flipped in triple file `collector.py` | exit 1, DRIFT vs the **r3** pin | exit 1, manifest pin reported `64387dda…` (not superseded `f5cf3d0e…`) ✔ |
| P3 | tampered `sha256_after_r2` in a copy of `DP03/r2/file-manifest.json` | loud builder abort, no r3 rewrite | exit 1, `live tree != DP03 r2 after for tests/datapipeline/fixtures/DP03/synthetic_doubles.py`; r3 manifest sha unchanged ✔ |

Minimum reproducers and raw stdout/stderr/exit evidence are preserved in work/08-probes.json and the
probe output directories.

## 7. Reviewer-side incidents (preserved, none affect the verdict)

- 02-structure.py check B5 initially FAILED because my probe read the wrong DP03 r1 manifest key
  (`files`/`write_set` instead of `write_set_files`); corrected in 02b/02c with true closure — the
  r3 artifacts were right, my first fixture was wrong.
- 08-probes.py first run crashed mid-P2 on a NameError in a print statement; fixed and the entire
  probe suite re-run from scratch (both raw runs preserved in the transcript; final JSON from the
  complete run).
- Builder re-run attempts 001/002 raw outputs preserved under work/05*.

## 8. Explicit scope limits

- Bounded PASS for the 9-file r3 release successor only. NR-DP08-08 and all other inherited
  NOT_RUNs (V15 live canary, PostgreSQL legs, real TqSdk sessions, junction inspection, candidate
  materialization, production cutover gates) remain in force.
- No coverage or claim for full DP08 product/storage/live acceptance, C05 full-baseline closure,
  T7, or DP01 package drifts.
- No live/network/DB/SDK operation, no credentials access, no git writes, no canonical writes, no
  plan-PASS gate; `enable_trading=False`, observer=`hold`, `broker=NOT_RUN`, real send/cancel `0/0`.
