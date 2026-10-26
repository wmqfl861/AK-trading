# Independent Hard Review — M_HOLIDAY_SESSION_ORIGINALS_002 (bip1-m-holiday-hard-review-001)

**Verdict: PASS (unconditional, scoped engineering PASS).** Full machine-readable verdict in `VERDICT.json`; findings in `FINDINGS.json`; per-check evidence in `work/`.

Reviewer: fresh independent ZCode GLM-5.3/max session on the dedicated reviewer store (`session/session.sqlite`), no participation in the author, advice, or owner sessions of this scope. Offline only — no network/API/URL replay of the two consumed one-use queries. Source write-freeze honored: the only writes are this dispatch's `work/`, `REVIEW.md`, `FINDINGS.json`, `VERDICT.json`, `REVIEW-FREEZE.json`.

## What was reviewed

The frozen 19-file bounded source delivery at `D:/AKS261026/R1/m-holiday-source-001` (9 control/fact files + 2 raw search dirs × 5 files), against the effective plan (`AUTHOR-DISPOSITION.md` + `FINAL-DISPOSITION.md`, byte-pinned) and the pinned current rules. Review standard: the plan as written; no new acceptance criteria invented, none relaxed.

## Verification performed (all offline, independently re-derived)

| Leg | Result | Evidence |
| --- | --- | --- |
| REQUEST pin verification | 75/75 pins byte-exact (inputs, capacity, python binary); freeze-before snapshot 19 files / 163,950 bytes | `work/w1_pincheck.json` |
| Independent semantic validation (R01–R20) | **20/20 PASS** after two documented reviewer-probe correction rounds (failed runs preserved) | `work/w2b_semantic.json` |
| Owner verifier `verify-009.py` re-run, reviewer-owned output | 13/14 checks + 10/10 counterexamples on the **current** 19-file root; the only FAIL is V01 = `['FROZEN-DELIVERY.json']` outside the owner whitelist, i.e. the disclosed F-01 footprint | `work/w4_verify009_reviewer_run.json` |
| Reviewer counterexample probes (P1–P10) | **10/10 REJECTED_AS_EXPECTED** | `work/w3_probes.json` |

Key independent confirmations:

- **Baseline discipline (F2):** the 12-relation baseline (6 kinds × 2023/2024, all `MISSING_FROM_PINNED_BASELINE`) is frozen inside `WORK-ORDER-BINDING.json` at 19:25:07.867Z — before REQUEST-1 (19:33:50.777Z) and network start (19:33:51.349Z), mtimes ordered — and is deep-equal-copied into `M-DCE-CALENDAR-FACTS.json` with a live-verified FileRef authority; both raw REQUESTs bind that frozen hash.
- **Exact queries/transport:** both query strings byte-identical across binding/REQUESTS/raw; limit 5; no auto-scrape; transport argv code identical in both requests and hash-equal to the pinned accepted code (`51dabe99…`), to the AU provenance and to the old-M search shape; 105s/115s, retries 0, shell false, serial execution (finish-1 < REQUEST-2 < network-2).
- **Branch terminals per F5:** 2023 = `NO_APPLICABLE_LOCATOR` — the sole returned locator is the annual holiday notice (identity **8526813**, excluded document class and already-consumed old source-412 identity; snippet recorded locator-only). 2024 = `QUERY_FAILED` — local HTTP 502, native 22, `success:false`, `search backend failed: No results found.`; never relabeled as NO_MATCH or success-empty (the only NO_MATCH strings are the terminal-basis's explicit "not NO_MATCH" and the legal local-dedup usage per AGENTS §7).
- **Budgets:** scope 2 queries/0 URLs; prior 4/6 cross-checked against the old-FACTS `/source_budget` (1/3 + old scope 3/3 = 4/6); cumulative 6/6 within 6/10; per-request chain 5/6 → 6/6; four doc slots NOT_RUN, no raw dirs, zero fabricated artifacts; scope closed, no rollover.
- **Raw-byte integrity:** stdout == RESPONSE bytes for both searches; `RESULT.transport_metadata` deep-equals the sealed child stderr JSON; all custody FileRefs byte-exact; preterminal registry (14 files/99,658 B) and FROZEN-DELIVERY (18 members/158,047 B) equal live bytes; Root's post-terminal reception registry equals FROZEN-DELIVERY including the freeze self-hash.
- **Honest statuses:** 12/12 relations `NOT_ESTABLISHED_THIS_SCOPE` with empty evidence quotes; snippet dates (1月21日/1月27日) confined to locator records; `INPUT_NOT_DELIVERED` / `calendar_component_status=INPUT_NOT_DELIVERED` / 0 components / 0 A-rows / 0 research credit everywhere; no sessions/census/consumer/FrozenInput/M5 products.
- **Counterexamples:** lookalike/userinfo hosts rejected on parsed-hostname boundary; six alias/migration/attachment forms of consumed identities rejected (fresh id admissible); counterfactual 502→NO_APPLICABLE_LOCATOR relabel detected; third query/fifth URL/prior-reset rejected; doc-slot fabrication absent; single-byte frozen-pin tamper diverges; source412+local200 masquerade unqualified; snippet-as-fact counterfactual structurally visible; baseline mutation (row drop / prior_status flip) detectable; component cannot masquerade as complete input.

## Material limitations assessed (all disclosed by owner/Root, none blocking)

1. **F-01 FROZEN-DELIVERY writer deviation (Q3):** the plan assigns this file to Root after writer termination; Root-003 instead instructed the owner to write it, and Root then published a separate post-terminal reception independently re-verifying all member hashes, preserving the deviation note and claiming no product PASS. Freeze-discipline function performed; no overwrite; qualified acceptance.
2. **F-02 search-001 custody gap:** supervisor crashed post-termination (psapi `GetProcessMemoryInfo`); PID/creation identity/RSS/peak honestly UNKNOWN, `resource_bounds_pass=null`; termination and native 0 independently proven by child-sealed transport metadata; successor custody without replay.
3. **F-03** disclosed loopback health probe (127.0.0.1:3002) preceded the baseline freeze — infrastructure liveness, zero consumption, no external host; baseline ordering vs both scope requests intact.
4. **F-04** duplicate top-level keys in the raw 2023 RESPONSE.json (capture-service artifact; `/data/web/0` unambiguous).
5. **F-05** owner-scratch stderr overwrite (both events recorded in diagnostics; first-failure bytes not independently available — outside the product write-set).
6. **F-06** search-002 `child_live_at_terminal=true` probe artifact (recorded as-is; termination proven by wait() + sealed streams).

## Reviewer-side defects (preserved)

Three probe-defect rounds were mine, not the product's: w2 run-1 (NO_MATCH substring overreach, brittle old-ledger string match, wrong FROZEN-DELIVERY path), w2b run-1 (TERMINAL/TERMINATION typo; old-ledger field semantics), w3 run-1 (pin-key slash format; "412"-string conflation with historical context). All failed outputs preserved (`w2_semantic.json`, `w2b_semantic.run1-failed.json`, `w3_probes.run1-failed.json`). Two tool-streaming interruptions were each verified against filesystem state before a single safe retry.

## Verdict reasoning

The plan's own standard (AUTHOR-DISPOSITION §事实采用/验收与停止): a genuine source failure with correct evidence and controls can be an engineering PASS but must not be converted into input delivery. Here the controls are correct and independently reproducible, the failures (2024 query failure; 2023 no-applicable-locator) are layered and honestly terminal, consumption is exactly accounted, and the zero-qualified outcome is retained everywhere. **PASS** — scoped strictly to this frozen delivery's contract compliance. It is **not** input delivery (INPUT_NOT_DELIVERED stands), grants no research credit, creates no consumer, and authorizes no trading and no replay; any future source increment requires a new author disposition.
