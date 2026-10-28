# Independent Hard Review — T7-B028 Streaming Scan + Real Classification (frozen successor)

* **Task:** `t7-b-streaming-hard-review-011`. Judged against effective
  author011 + author012 (§4/§5 B-side obligations). No developer role, no
  source repairs; frozen owner bytes untouched (re-verified after review).
* **Under review (SOURCE-FREEZE, hashes re-verified post-review):**
  `run_daily_bounded.py` 46 959 B `eb9aaa79…fa50d`,
  `new_semantics_tests.py` 16 619 B `2717312c…7be7a`,
  `test_run_daily_bounded.py` 20 153 B `1fad2b9f…ab68` (unchanged from the
  026 frozen tests). Root's actual classification consumed:
  `ROOT-CLASSIFICATION.json` — **0 ro / 92 active / 83 base roots
  preserved** (zero-ro is the valid conservative fallback per author012
  §4). Preserved history retained: 027's fabricated-ro failure, 028's
  initial classification-forwarding bug (caught by the fake-proof test),
  and both real-comparison attempts (the attempt-1 refusal was the
  supervisor CORRECTLY rejecting a nested active sub-root; attempt-2 was
  the owner's script bug).
* **Budget:** within the 600 s / external-stop-570 / final-120 policy;
  ROOT-LIVE-BUDGET checked — no expensive operation was needed (all checks
  ≤ 60 s; validation tree far below 512 MiB; new outputs ≤ 16 MiB; no
  DAILY/fit; trading boundary `false/hold/NOT_RUN/0/0`).

## 1. Code review (actual source, against author012 §4/§5)

* **Streaming `retained_state`** (`run_daily_bounded.py:158-189`): one
  `os.scandir` pass with a single `stat(follow_symlinks=False)` per entry —
  no whole-tree list, no repeated type/size queries; symlinks/reparse
  points are seen-but-never-followed (no silent loss, no double count);
  OSError collected per entry/directory and **fails the quota check
  closed** upstream (lines 447-449, 625-626 region). Active roots are
  genuinely re-traversed each quota cycle (line 623-626) with the verified
  ro base added arithmetically — no undocumented cache or skip.
* **Classification gate** (lines 306-379): the Root table is the only
  authority — `::ro::` claims are honored only for table-ro paths
  (mismatch → `ro_claim_mismatched_classification_refused`), `::ro` without
  credentials refused, every table-ro root must be supplied
  (`classification_ro_root_missing_refused`), unknown plain roots stay
  ACTIVE, per-category nested-overlap and cross-category overlap refused,
  out-dir must live inside an ACTIVE root (`output_in_ro_root_refused`).
  The table is forwarded to the worker (lines 832-833 — the initially
  missing forwarding, now fixed and covered by the fake-proof test).
* **`verify_ro_root`** (lines 382-421): reads the pins record, checks root
  identity, authority fields, then a **full member+content comparison with
  1 MiB chunked hashing** — member-set equality both ways, exact
  bytes+SHA per file; unreadable/missing/mismatched pins produce specific
  integrity FAILURES (`*_refused`, lines 434-437), never a silent active
  fallback — exactly author012's "mismatch keeps the integrity failure".

## 2. Independently executed (raw exits; logs under `logs/`)

1. **`new_semantics_tests.py`** (pinned python `-B -X utf8 -m pytest -q -p
   no:cacheprovider`, fresh basetemp): **14 passed in 25.20 s, raw exit 0**.
2. **Prior unmarked quota semantics** (`test_run_daily_bounded.py::`
   during-run + final-state quota nodes, fresh basetemp): **2 passed in
   4.09 s, raw exit 0** — unchanged behavior on the frozen 026 test bytes.
3. **Reviewer counterexamples (real CLI with SYNTHETIC tables — never real
   Root approval):**
   * **C1** forged `::ro::` claim on a table-ACTIVE root → exit 2,
     `ro_claim_mismatched_classification_refused` ✓
   * **C2** `::ro::` claim on a table-unlisted path → same refusal ✓
   * **C3** table-ro root not supplied → `classification_ro_root_missing_refused` ✓
   * **C4** table-honored ro with pins whose content SHA mismatches →
     `ro_pins_content_mismatch_refused` (integrity FAIL, not fallback) ✓;
     **C4b** unreadable pins → `ro_pins_unreadable_refused` ✓ (first
     attempt mis-placed the out-dir outside any retained root and was
     correctly refused by membership — probe bug, preserved; the fix
     re-ran only C4/C4b)
   * **C5 zero-ro fallback success**: all-active table, child writes
     3 000 B → **complete success: CLI 0 / stop completed / supervisor 0,
     classification {ro:0, active:1}, all three streams captured,
     final-scan growth +3 035 B counted** ✓ (during-run check ticked zero
     because the child finished sub-second; live-cycle evidence exists in
     the owner's real comparison — quota_checks len 1)
   * **C6 streaming parity/alias** (direct module call): nested + empty
     dirs, file symlink and directory symlink to off-tree content — total
     counts exactly the regular non-link files once (150 B), zero errors ✓
4. **Real-comparison coverage validated** (`REAL-COMPARISON-012.json`):
   one streaming enumeration → full total == classified total =
   1 101 780 574 B, coverage equal, zero scan errors; then a real
   supervisor run with complete success (child 0 / worker 0 / unified 0,
   no failure reasons, out inside the classified active dispatch root,
   before-matches-full-total, active growth counted, streams captured,
   ro base 0). Internally consistent with the classification table and
   the code paths I exercised.

## 3. Observations (non-blocking)

* **OBS-1:** the DELIVERY prose quotes the streaming scan as "10.7 s (vs
  rglob 78.6 s)" while `REAL-COMPARISON-012.json` records
  `one_scan_seconds: 3.2` — the record is authoritative and even better;
  the prose/record discrepancy is noted, not a defect.
* **OBS-2 (retained note):** 027's fabricated-ro failure, the 028
  classification-forwarding bug and both comparison-harness attempts
  remain preserved in the owner records as history, per the workorder.

## 4. Verdict

**PASS — unconditional.** The frozen 028 successor implements author012
§4/§5 faithfully: streaming one-stat-per-entry scandir traversal with
fail-closed errors and correct alias handling; classification authority
consumed only from Root's table with forged/mismatched/insufficient
credentials refused, missing table-ro roots refused, unknowns staying
active, and zero-ro as the actual valid fallback; chunked-hash ro
verification with integrity failures never masked by fallback; the
complete 83-base retained set preserved (92 active, 0 ro); a corrected
one-shot real comparison proving full==classified coverage and a complete
successful short-child run; all 16 affected tests green independently and
all seven reviewer counterexamples exact. This PASS covers the B028
supervisor repair only — no DAILY/fit was run, no F/L0/R1 credit, and the
standing chain (integration → F → R0 → L0 → owner runs → R1 → L1 → R2)
is unaffected.

*Raw exits: semantics 0 (14P), quota nodes 0 (2P), probes 0, c4 rerun 0.
Budget within policy; parse-validated VERDICT/HASHES alongside.*
