# Forward integrated306 — independent formal product hard review

- Verdict: **PASS** (unconditional). VERDICT.json / FINDINGS.json (zero findings) / HASHES.json sealed at this prefix.
- Reviewer: the original independent forward reviewer `sess_01d66719…` (GLM-5.3/max), byte-exact cwd `D:\Projects\AK-trading`, original private CU store; developer `sess_43fc3d2f…` distinct; no implementation or repair participation. Authorities: attached WORKORDER, AUTHOR-EXECUTION-DISPOSITION-003/004, effective PLAN §9, DISPATCH-INPUT-FREEZE (18 refs), FORMAL-EXECUTION-BINDING (D=2100s), ROOT-FRESH-ADMISSION, capacity-052.
- Old formal001 (`independent-full13-001`) is retired-incomplete (CLI1, helper/pytest UNKNOWN, RESULT absent, 49/50 partial) — preserved, never replayed, no verdict retroactively issued.

## The one formal run (`independent-full13-002`) — complete pass

Launched EXACTLY ONCE with the installed Bash explicit background lifecycle (no foreground wait) and observed to real terminal: background exit 0, helper raw native 0, `session_finish pytest_exitstatus 0`. Frozen helper `verify_forward_acceptance_005.py` (sole delta vs 004: the embedded reporter expected-tuple, verified as a 2-line diff), unchanged 21-element argv (base 17 + `--tb=long --maxfail=1 -p akfp_full13_reporting_001`), suspended Job enrollment before imports, offline/credential guards.

- **306 passed in 1112.18s (0:18:32), [100%]** — zero failures/skips/deselections. Helper elapsed 1120.375s ≤ D=2100s; peak Job commit 2,006,671,360 ≤ 2GiB; output 34.2MB ≤ 1GiB; `budget_violations: []`; 378 source pins, zero mismatches; stderr carries only the 1533 reporter events (its 11 "FAILED" substrings are `CALIBRATION_FAILED` node-id parameters, not failures).
- Reconciliation (`reviewer-checks/formal002-reconciliation.json`): collection_finish count 306 with `ordered_node_ids` exactly equal to the frozen population; 306 node_starts and 306 node_finishes, both exactly equal to the frozen order; zero non-passing phases; request argv equals the binding.

## Pre-review verification (`prereview-integrated-r1.json`, 16/16)

All 18 dispatch refs rehash clean. The integrated closure (378 files) matches the live tree member-for-member; the changed set vs the retired 377-file closure — computed programmatically — is exactly one added file (`tests/test_mcp_readonly_role.py`) plus nine modified, identical to the owner's changed-source mapping. The forward feed/test files are byte-identical to the accepted r7 revision. Population: 306 unique nodes, the original 298 preserved as an ordered subsequence with zero removals, eight accepted additions, and the 13-file block order equal to the argv order. The coverage mapping's three VERIFIED_CHANGED_SCOPE groups exactly partition the 306 nodes; P27_replay/P16/C05 acceptance-evidence lists are all nonempty; the readiness label is exact; the binding is D=2100/argv-21.

## Independent checks

- **Immutable validator004 rerun against helper005** (`validator004-vs-helper005-SYNTHETIC.json`): native 0 — 4/4 positives (full acyclic C→N→H→P→R→B→A→D chain; legitimate added-node population; one-use gate; constructor statics on the real file) and 93/93 negative rejections, each asserting its exact intended protocol code.
- **Capacity floor** (`capacity-floor-check-r2.json`, taken before launch): RAM after the whole 052 reservations 8.07GB ≥ 6GiB floor; D after whole charges 4.45GB ≥ 4GiB floor; UNKNOWN charges retained. (The r1 record's key-typo is preserved; r2 is the effective check.)
- **Reviewer counterexamples** (`counterexamples-003/`, 600s/2GiB/1GiB bounded Job, raw 0, 5/5): trend duplicate/restart re-delivery on reviewer-shifted windows holds the confirmation counter with OPEN only on the third distinct bar; prefix invariance under interleaved duplicates; pure redelivery never issues OPEN and never grows the counter; pullback short-mirror duplicate arming/reclaim bars hold the counter with OPEN exactly on the third distinct bar; a late duplicate of an older bar never regresses the achieved counter. (Attempts 001/002 with my import-path and expectation defects — the latter my error, the product behaving correctly per the frozen third-bar semantics — are preserved verbatim.)

## Integrity and honesty

Post-review drift recheck: 378/378 clean; no product, owner, helper or prior-review file was modified; writes stayed under this prefix plus the Maintenance delivery path. Prior failures preserved untouched: r7 full13 F/124 (cause unestablished), r12 124/DEADLINE (183/1/114 split), r7 RED raw1, the retired formal001, and the OLD-SUBMISSION-CLOSEOUT. Owner feed-58 and r12/r13 coverage runs remain readiness evidence only — this PASS rests on the single complete independent formal002 run plus the checks above. Runtime/live-SDK gates remain separate and unchanged; `enable_trading=false`, observer `hold`, broker `NOT_RUN`, real sends/cancels 0, no credentials/network/model changes, BIP1 untouched.
