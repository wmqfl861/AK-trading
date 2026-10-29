# T7 R0 Frozen-Input / Scope Check Report — v2-ep1-t7-review-r1 (goal-t7regime-20261029-r15)

* **Task:** `aktr-zcode-t7-R0-review-013-20261029` — r15 new-F input-closure
  review under author022 (Sol's regime-reference preparation reuse:
  `regimes.py` + `experiments.py` fold-lifetime wiring, with
  `test_v2_m3a_models.py`/`test_v2_m5_experiments.py` regressions). **Input
  closure ONLY — no full models/experiments/payoff/fit, no full-T7 product
  acceptance; the B command's generation body and
  `freeze_acceptance_manifests` were NEVER executed.** Owners WRITE_STOP.
* **Reviewer:** the original independent T7 R0 reviewer session
  (`sess_6c24d61a-5ae5-40d3-938b-d722f8cfa468`, GLM-5.3/max, byte-exact
  cwd/store); process-only `PYTHONPATH=r15/src`,
  `PYTHONDONTWRITEBYTECODE=1` throughout.
* **Candidate:** `C:\AKWorktrees\aktr-goal-t7regime-20261029-r15`; F
  (**H_F `4510fd876efa893dafb55d4405ca1fae6156af4a4f6873a13144f48ba12d4c19`**
  — REQUEST-pinned, official-verifier-returned), 129 attachments; DAILY
  manifest `6a2daf55…3a3e82f` (111). All **248 REQUEST pins** verified;
  review output ABSENT before creation; trading boundary
  `false/hold/NOT_RUN/0/0`; 256KiB control cap respected (no world-size
  sandbox copies; compact scratch only).

## 1. Delta and closure verification (exit 0; `r15-checks.json`)

* **Exactly the authorized five-entry delta vs predecessor r13:**
  `src/ak_trading/v2/probability/regimes.py`,
  `src/ak_trading/v2/validation/experiments.py`,
  `tests/test_v2_m3a_models.py`, `tests/test_v2_m5_experiments.py`, and
  the DAILY child manifest — no additions, no removals, no other changes
  across all 129 members (both manifest tables compared programmatically).
* **Row bindings:** F pins the ACTUAL r15 bytes for all four changed files
  and the DAILY child; each changed file's bytes differ from the r13
  predecessor (stale-source mixes are computationally distinguishable,
  and the official verifier's per-attachment hash check enforces it).
* **Membership-rejection probe (KB-scale synthetic scratch):** a
  schema-valid manifest omitting required floor files is REJECTED by the
  official verifier (`acceptance_manifest_required_source_not_listed:
  scripts/research_probability_v2.py`) — proportionate changed-input
  rejection within the control cap (the full-world sandbox rejection
  pattern was proven in R0-010/011/012 and is reused as evidence).
* **Fixture verifiers:** the frozen support's
  `verify_acceptance_manifest()` and `verify_daily_acceptance_manifest()`
  (official verifier + tree + regeneration identity) both return `[]`.
* **Unchanged core:** 12 world inputs and 25 legacy closure files
  byte-identical to r13; **zero `.pyc`**; import origins for the chain
  (incl. the changed regimes/experiments) under r15/src; reuse pins
  (registry/artifacts/support/splits/m5_research) all matched.

## 2. Fresh checks (raw exits under `logs/`; guarded records under dispatch/work)

| Check | Exit | Outcome |
|---|---|---|
| REQUEST pins | 0 | **248/248** exact (first attempt crashed on a missing results dir — created; preserved). |
| Official M1 verifier | 0 | returns the pinned H_F `4510fd87…d4c19`. |
| Compact checks script | 0 | §1 (5-entry delta, bindings, membership probe, fixture verifiers, data12, legacy 25, origins, no-pyc, reuse pins). |
| Guarded manifest nodes via frozen `job_guarded2.py` (BINDING-COMMAND-022-001 verification contract, aggregate 2 GiB Job) | 0 | child exit 0, **2 selected / 2 ran / 2 passed / 0 failed/skipped in 6.78 s** (`dispatch/work/r0-manifest-run-001/`). |

## 3. Reused verified facts (explicit mapping; pins re-verified)

r13's R0 evidence maps unchanged: F3 closure (25 legacy files re-compared),
14/14 producer bindings (registry pin), 13/13 negatives (artifacts/support
pins), 14/14 world regeneration (support+manifests verified via the fixture
verifiers above), inner-split/plan-set/config contracts (splits +
m5_research pins), and the r10 clock / r12 data / r13 factors delta
verifications (those bytes unchanged r13→r15). The changed
regimes/experiments bytes invalidate only their own test credit —
developer-side under author022 — and factor/regime semantics remain the
final CPU reviewer's scope.

## 4. Findings

**No blocking findings.** Non-blocking (carried): recipe.json raw newlines;
F schema cannot express the separately-pinned closure inputs; the changed
four files are author022 candidate bytes with developer evidence only.

## 5. Verdict

**R0_FROZEN_INPUT_SCOPE_CHECK_OK (H_F `4510fd87…d4c19`).** The frozen r15 F
passes the input-closure obligations: exactly the authorized five-entry
delta, verified bindings, unchanged world/legacy closure, clean staging
with verified origins, official verifier green, both fixture verifiers
green, both manifest nodes 2/2 under the frozen guarded 2 GiB contract,
membership rejection proven, reused-evidence pins intact. **Input closure
only — no fit, no full models/experiments/payoff, no complete CPU/T7 PASS,
no lock authored.** The original M1 may bind **this report's actual bytes**
(below) as the fresh r15 L0's `reviewer_report_sha256` input.

*Machine records in `R0-RESULT.json`; report bytes sealed in the dispatch
`R0-HANDOFF.json` (closed streams only; seal-writer streams excluded).*
