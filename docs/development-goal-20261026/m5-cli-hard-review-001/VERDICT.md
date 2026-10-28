# VERDICT — m5-cli-hard-review-001 (independent product hard review)

## Unconditional PASS

The two-file M5 CLI stage-completion repair at frozen hashes
`scripts/research_probability_v2.py` 7458 bytes / sha256 `580c2f32…344c` and
`tests/test_v2_m5_cli.py` 30451 bytes / sha256 `e4fe98a1…c2edc` in
`C:/AKWorktrees/aktr-goal-m5cli-20261028` passes independent product hard
review against CONTINUATION-20261028 §1 (sha256 `6a3a1ef4…c3fa`) and 06-M5 §8.
No conditional pass. The reviewer made no source edits; the candidate is
byte-identical before and after review (pins re-verified post-run).

## Basis

1. **Diff is exactly the planned repair.** Versus the canonical preimage
   (`db0e61e3…685ba`, matching the owner's pin): one functional line adds
   `walk-forward` to the exit-0 mode tuple plus a rationale comment; tests add
   5 mapping tests and helpers, remove nothing.
2. **Required command independently rerun** under bounded supervision
   (113.8s wall, 142MiB peak RSS, no bound termination): native exit **0**,
   **23 selected / 23 ran / 23 passed / 0 failed / 0 skipped / 0 errors /
   0 collection errors / 0 unresolved files**, stderr empty.
3. **Counterexamples 15/15 at expected native exits**, including the
   opposite-result control: the preimage bytes return 3 where the frozen
   candidate returns 0 on the contract case (walk-forward, engineering PASS,
   statistics FAIL, m5_complete false). `m5_complete` is not consulted
   (true→0 and false→0), final-holdout stays 3 even fully PASS, exception
   classes keep 3/4/2/1 with per-class error.json identities, the real
   missing-lock gate and the A4 shadow early refusal (creates nothing) are
   unchanged, and input-error branches keep exit 2.
4. **Historical failure disposition verified.** The prior owner's 22P/1F was
   the six absent chunk-config inputs; the six create-only copies match both
   the FREEZE target pins and the canonical D: source hashes; the prior
   failure evidence is preserved untouched.
5. **Safety boundary held.** No SDK/DB/network/order API, no credential
   access, `enable_trading=False`, observer `hold`, `broker=NOT_RUN`,
   real send/cancel 0/0. Evidence total 888KiB (≤256MiB bound).

Credit scope: SYNTHETIC unit/engineering regression credit only — no real M5
fit, lock-content, historical PIT/full100, ordinary-shadow or final-holdout
credit is issued. Non-blocking observations O1–O4 in FINDINGS.json.

## Evidence paths

- `C:/AKWorktrees/aktr-goal-20261026/zcode-dispatch/m5-cli-hard-review-001/work/FINDINGS.json`
- `.../work/required-rerun.json`, `.../work/required-rerun.stdout.txt`, `.../work/required-rerun.stderr.txt`
- `.../work/probes-result.json`, `.../work/probe-*.stdout.txt`, `.../work/probe-*.stderr.txt`
- `.../work/pin-verification.json`
- `.../work/supervise_required_rerun.py`, `.../work/run_probes.py`, `.../work/cli_exit_probe.py`, `.../work/verify_pins.py`
