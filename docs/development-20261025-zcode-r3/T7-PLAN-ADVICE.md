All verification is complete. Below is my independent advisory on the effective T7 repair plan.

---

# Independent plan advice — T7-EXISTING-REPAIR-PLAN (r3 P1-3 supplement, 2026-10-25)

Advisory only. No plan PASS gate, no product verdict, no change to scope, populations, or budgets.

## What I verified (all checks passed)

- **Unit A/B write-sets are real and disjoint.** All four strategy production files exist (`src/ak_trading/v2/strategies/{breakout,pullback,registry,trend}.py`), all seven M3B test files exist, all seven `tests/fixtures/v2/ep1_t7/` files exist, `tests/v2_ep1_t7_support.py` exists, and `tests/fixtures/v2/ep1_t7_daily_r3_001/` is currently ABSENT — the plan's first-write precondition holds today.
- **Original obligations are quoted accurately.** P0 plan §10.2 contains exactly the seven single-file commands plus the combined seven-file command (PLAN.md:215–226). The budget quote matches T7 form decision §7.4 byte-for-byte (4h/profile, 8h two profiles, 15-min snapshot check, 15-min final-lock recheck only under genuine frozen-fit reuse, "工作单预算，不是实测" caveat preserved, decision.md:297–312). RULING 6's M1-five / M4-nine / M5-seven file command groups are real (training-path decision.md:305–309), and §7.5's two-file quick-check command matches the plan's §4.
- **The §1 acceptance split is factually grounded.** `CURRENT-DRIFTS.json` shows exactly 64 pins / 12 drifts, and the 12 drifted files map one-to-one onto the plan's six-with-chains / six-without split. Spot-checked chain records exist: `D:/AKS261014/DAILY/mainline-seam-r3/hard-review-r1-acceptance-followup-002/VERDICT.json`, `docs/development-20261025-zcode-r3/REPLAY-VERDICT.json`, and the M2 evidence `VERDICT.json` + `PROBE-BASELINE-REGISTRY.json`.
- **Unit B's migration target is correctly identified.** `test_t7e1_daily_positive_path_structurally_blocked_pinned` exists at tests/test_v2_m5_t7_evidence.py:261 and pins DAILY as structurally blocked; the plan's step 2 (migrate to a real DAILY positive, keep negative branches) is the right move. Notably, current `pipeline.py` bytes already carry the DAILY seam (`_daily_background` with published-forecast evidence, pipeline.py:424, plus the DAILY ruling003 canonical path at pipeline.py:244), so the pin's "blocked in CURRENT source" premise is stale relative to today's bytes — migration is plausibly executable **without production writes**, which is what unit B's write-set requires.

## Findings (prioritized)

**A1 — Unit A dispatch precondition: the 2026-10-16 M3B directional-fixture r2 freeze overlaps unit A's test lease** (plan §2; AGENTS.md §2 write-freeze). `directional-fixture-r2` (delta on `test_v2_m3b_directional.py`, `test_v2_m3b_martingale.py`, `test_v2_m3b_matrix.py`, 206/0/3) sits at `D:/AKS261016/M3B/directional-fixture-r2` in status READY_FOR_IMMEDIATE_INDEPENDENT_HARD_REVIEW with no record that the review ever landed. Three of those files are inside unit A's four-test write lease (two writable, martingale run-only). Recommendation: the unit A work order must explicitly disposition this freeze — cleanest is to declare unit A's freeze+independent review as consuming/superseding the r2 pending review (same bytes, same seven-file command, broader scope), with succession from owner `sess_7cb10507` recorded per the plan's own "有限接续" clause. Do not silently overwrite or ignore the frozen state.

**B1 — Unit B routing gap: production-defect routing is named only for `experiments.py`** (plan §3). The DAILY positive path rides `pipeline.py`/`gates.py` (DAILY seam r3 chain). If a genuine defect there blocks the positive, the plan names no owner or escalation route, and the T7 owner is correctly forbidden from writing production. Recommendation: pre-name the DAILY-seam production owner (or state that any such defect escalates to Root for owner binding) in the dispatch record, so unit B cannot stall or be tempted to broaden its own write-set.

**C1 — Minimum missing dispatch information** (per my work order, naming exact gaps): the plan refers to “现任专用 M5 ZCode owner”、“M1 的专用 ZCode owner” and the DAILY seam owner without session/store/cwd bindings. Root must pin those SIDs at dispatch, plus absolute paths for the LOCAL_ONLY external records on `D:/AKS261014`, `D:/AKS261015`, `D:/AKS261016`. These are the only inputs I found genuinely missing; nothing else blocks dispatch.

**D1 — Unit A byte-reuse fallback should be stated** (plan §1). The plan says chained files are programmatically byte-compared at dispatch; it does not say what happens if a candidate byte falls outside the recorded range (e.g., a chained file drifted past its last acceptance). Recommendation: state the fallback — the file moves to the no-acceptance bucket and routes to its owning seat — rather than blocking unit A, which needs none of those six files to proceed.

**E1 — M1 involvement is likely required, not conditional** (plan §3 step 5). If the DAILY profile gets its own fixture root, the acceptance-input-manifest regeneration and required_tests coverage will need the artifacts.py side; T7 form §5.3 recorded that `DOWNSTREAM_TEST_FILES` lacked `test_v2_m5_payoff_integration.py` at decision time (decision.md:217). Current artifacts.py bytes may already include it. Recommendation: the T7 owner pre-checks `artifacts.py` coverage in prepare mode; treat M1 as a probable (not exceptional) participant so the R0→L0 chain isn't serialized at freeze time.

**F1 — Budget realism (info, not a defect).** The 4h/profile cold chain is the original obligation and must not be relaxed; but this host's history (M5 CDF full100-class batches, ~3h single inner fits) means wall-clock contention can consume the budget without a product defect. Schedule unit B cold chains off concurrent heavy batches; keep timeout = failure as the plan states.

## Bottom line

Unit A and unit B are genuinely disjoint, immediately executable in parallel, and neither depends on M5 C1, C05, or full T7 completion — the plan's core structure is sound and its citations check out. The only pre-dispatch actions are A1 (disposition the r2 freeze) and C1 (owner/path bindings); B1/D1/E1 are one-line work-order additions; F1 is scheduling hygiene. No additional plan review round is needed for these.
