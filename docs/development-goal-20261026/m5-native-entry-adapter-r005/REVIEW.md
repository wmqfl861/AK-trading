# REVIEW — r005 actual-source seam hard review (r005-review-003, publication successor)

Successor to `reviews/r005-review-002` (same independent ZCode GLM-5.3/max reviewer,
dispatch `m5-native-entry-adapter-review-018-002`, effective authority author020 +
retained 018/019 obligations). This call is the conditional publication repair
(`m5-native-entry-adapter-review-020-001`) for exactly one identified defect:

**Defect (publication-only):** `r005-review-002/VERDICT.json` was syntactically
invalid JSON — line 46 closed the `not_run` object with `]` instead of `}`.
Root's raw exit 1 (`JSONDecodeError: Expecting ',' delimiter: line46 column2
char2692`, chunk 823ce5) is reproduced by the reviewer (ROOT-FINDING.json pinned
below). The 002 directory is preserved immutable; this successor re-issues the
verified verdict in parse-validated form. No product source, evidence, or suite
was changed or re-executed for this repair.

## Verdict: **PASS** (unconditional, adapter scope)

The verdict is unchanged from review-002 because the defect was in publication
JSON only; every substantive verification input and check remains applicable:

- All 6 frozen r005 inputs re-hashed this call and still match
  SOURCE-FREEZE-005-R5.json byte-for-byte (see HASHES.json).
- The independent rerun (RVR1), both counterexample probes and all hash
  comparisons were completed inside review-002 against those same frozen bytes;
  none is re-executed here (work order: no duplicate suite).
- No unresolved verification was found in re-reading the retained evidence.

## Corrected record content

- **Distinct checks: 16 of 16 PASS** (the suite's 21 result rows include the five
  K-capture rows twice via `check()` inside `crCheck` plus `outcomes.push(...cr)`;
  distinct identities are P1–P8, N1–N3, K22/K19/K20/K18/K18b). Raw suite exit 0.
- **Independent rerun**: `ZRO117_RUNID=RVR1 node reviews/r005-review-002/scratch/test-actual-source.cjs`
  → native exit 0, 21/21 rows PASS (16/16 distinct), stderr empty; extraction
  provenance byte-equal to owner RUNID=005; scratch copy's only deviation is the
  line-24 output-root constant (frozen sha `5f2d43e9…` → scratch `846be2a7…`).
- **Counterexamples**: CE1 registry-driven dispatch (registered name → ToolNotFound
  rejection NOT replayed; executor path entered; threw on the reviewer's minimal
  probe tool shape, consistent with the owner's declared executor-branch limit);
  CE2 pin-drift through the real preload (native exit 3 `INSTALLED_INPUT_DRIFT`,
  entry never executed, exclusive capture claimed).
- **Retained first reviewer probe harness failures** (honest record, product
  unaffected): CE1 attempt 1 used top-level `await` in a `.cjs` (ESM warning +
  SyntaxError; preserved in `r005-review-002/scratch/ce1.stdout.txt` /
  `ce1.stderr.txt`); CE2 attempt 1 died on reviewer shell-escaping
  (`SyntaxError: unterminated string literal` in the builder one-liner). Both were
  repaired under new names (`probe-ce1-registry-driven-v2.cjs`,
  `probe-ce2-pin-drift.py`) and the failed attempts remain preserved.
- **Non-blocking observations** (unchanged): O1 five K-rows double-counted in the
  "21/21" summary (cosmetic; both instances PASS); O2 seam capture claimed before
  extraction (append-only, no overwrite risk); O3 N3's `embeddedSearchEnabled:false`
  is the correctly-scoped R5 control, with RUNID=004's real embedded-search branch
  behavior preserved.

## Honest limits (retained)

- Original037, formal006, generation001, full100: NOT_RUN / NOT_GRANTED (not
  authorized here). No whole-CLI bootstrap, old C14/C21, or model/auth/config/
  credential probe was run; none is required at this layer per author020.
- Adapter PASS is tooling-scope only — NOT candidate615, NOT M5 product PASS.
  The full live binding check remains inside original037 in enforcement mode
  (mandatory exclusive `ZRO117_CAPTURE_FILE`, no test sentinel) before its prompt.
- The R1–R4 preimage uncertainties and the declared post-freeze README deviation
  remain preserved as history; this review binds the final frozen bytes.

## Publication provenance

- Preserved originals (immutable): `r005-review-002/{REVIEW.md, VERDICT.json,
  HASHES.json}` — VERDICT.json preserved invalid at sha `4a71a10c…` (2840 bytes),
  exactly as recorded in ROOT-FINDING.json.
- This successor directory is exclusively new: REVIEW.md (this file), VERDICT.json
  and HASHES.json produced via `json.dumps` and parse-validated before the final
  response. HASHES.json pins the closed final bytes of REVIEW.md and VERDICT.json
  (written last) and contains no self-hash.

Trading=false, observer=hold, broker=NOT_RUN, sends/cancels=0/0. No .env/credential/
model-config reads or changes, no network, no git, no nested agents, no installed-byte
or owner-evidence writes. Reviewer writes are confined to `reviews/r005-review-003/**`.
