# T7 R0 Frozen-Input / Scope Check Report — v2-ep1-t7-review-r1 (goal-t7factor-20261029-r13)

* **Task:** `aktr-zcode-t7-R0-review-012-20261029` — fresh R0 for the r13
  factor-input closure under author019 (Sol's `factors.py` per-call
  preparation-reuse + declared-day lookup-index repair, with M2 causality
  regressions: 27 AST tests retained + 4 added; models/support unchanged).
  **F/DAILY input closure only — no fit, no full M2/payoff/DAILY, no
  complete CPU/T7 product PASS; verification only (freeze_acceptance_
  manifests NEVER re-run, F never regenerated).** B/source owner
  WRITE_STOP.
* **Reviewer:** the original independent T7 R0 reviewer session
  (`sess_6c24d61a-5ae5-40d3-938b-d722f8cfa468`, GLM-5.3/max, byte-exact
  cwd/store); CLI cwd/main never imported as product.
* **Candidate:** `C:\AKWorktrees\aktr-goal-t7factor-20261029-r13`; F
  (**H_F `b6c7257c586cc3471636a972cfc24922dc81c04f2bacc9a73fbd1c957e4010d6`**
  — REQUEST-pinned, official-verifier-returned), 129 attachments; DAILY
  manifest `b6c67765…5da44c` (111). All **237 REQUEST pins** verified
  before AND after; review output ABSENT before use; trading boundary
  `false/hold/NOT_RUN/0/0` throughout.

## 1. Affected-changes verification (exit 0; `results/affected-changes.json`)

* **Immediate predecessor r12; the F tables differ in exactly the
  authorized three entries** — `src/ak_trading/v2/factors.py` (79 112 B),
  `tests/test_v2_m2_causality.py` (97 214 B), and the child DAILY
  manifest — no additions, no removals, no other changes across all 129
  members. (Against r11 the cumulative delta adds the author018
  data/test-data pair — consistent with the chain; two reviewer-side
  wrong-base/escaping attempts preserved.)
* **Bindings:** F pins the actual r13 factors/test bytes and the DAILY
  manifest's exact bytes; LF H values recompute to the frozen records.
* **Unchanged core:** all 12 world inputs byte-identical; 25 legacy
  closure files identical; **zero `.pyc`/`__pycache__`**; import origins
  for the chain (incl. the new `ak_trading.v2.factors`) under r13/src.
* **Changed-input rejection (reviewer scratch):** a pristine full copy of
  the F world is ACCEPTED at the new H_F; reverting only `factors.py` to
  the predecessor r12 bytes is REJECTED
  (`acceptance_manifest_attachment_size_mismatch:src/ak_trading/v2/factors.py`).
* **Reuse pins:** registry/artifacts/support/experiments/splits/
  m5_research all matched their reviewed hashes on r13 (factors itself is
  the authorized change and is not a reuse dependency this round).

## 2. Fresh checks (raw exits under `logs/`; guarded run records under dispatch/work)

| Check | Exit | Outcome |
|---|---|---|
| REQUEST pins (×2, pre/post) | 0 | **237/237** exact; interpreter pinned. |
| Official M1 verifier | 0 | returns the pinned H_F `b6c7257c…4010d6`. |
| Affected-changes script | 0 | §1 (3-entry delta vs r12, bindings, data12, legacy 25, origins, no-pyc, sandbox acceptance + factors-revert rejection). |
| Guarded manifest nodes via frozen `job_guarded2.py` (BINDING-COMMAND-r13-001 contract, aggregate 2 GiB Job) | 0 | child_exit 0, Job pids final 0, **2 selected / 2 ran / 2 passed / 0 failed/skipped** (`r0-manifest-run-001/` records). |

## 3. Reused verified facts (pins re-verified; not re-executed)

The 009 F3 closure, 007/008 14/14 producer bindings, 13/13 negatives,
14/14 world regeneration, inner-split/plan-set/config contracts, the r10
clock and r12 data delta verifications (clock/data bytes unchanged
r12→r13) all stand reused. The changed factors bytes invalidate only
factor-file test credit — developer-side under author019 — which this R0
does not convert into acceptance.

## 4. Findings

**No blocking findings.** Non-blocking (carried): recipe.json raw
newlines; F schema cannot express the separately-pinned closure inputs;
factors/M2-test bytes are author019 candidate bytes with developer
evidence only — factor semantics belong to the final CPU reviewer, not
this R0.

## 5. Verdict

**R0_FROZEN_INPUT_SCOPE_CHECK_OK (H_F `b6c7257c…4010d6`).** The frozen
r13 F passes the input-closure obligations: exactly the authorized
three-entry delta vs r12, verified bindings, unchanged world/legacy
closure, clean staging with verified import origins, official verifier
green, both manifest tests 2/2 under the frozen guarded 2 GiB Job
contract, changed-input rejection proven, reused-evidence pins intact.
Candidate byte-identical throughout (237/237 before and after). **Input
closure only — no fit, no full M2/payoff, no complete CPU/T7 PASS, no
lock authored.** The original M1 may bind **this report's actual bytes**
(below) as the fresh r13 L0's `reviewer_report_sha256` input.

*Machine records in `R0-RESULT.json`; report bytes sealed in the dispatch
`R0-HANDOFF.json` (closed streams only; seal-writer streams excluded).
WRITE_STOP.*
