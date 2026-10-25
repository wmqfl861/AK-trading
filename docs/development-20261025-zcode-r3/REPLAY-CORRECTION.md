# Pin correction for replay-hard-review-002 reports (append-only, machine-derived)

Reviewer session `sess_5cf8971a-54d0-4748-ad5a-f68c8067904c` (same original independent
reviewer; bounded record correction `replay-review-pin-correction-003`, not product
repair, not a new review cycle). All hash values in this file and in
`VERDICT.json` beside it are machine-derived by `make_correction.py` from the
actual files; none was retyped by hand.

## Classification

**REPORT_TRANSCRIPTION_ERROR.**

- `src/ak_trading/v2/decision/replay.py` (40650 bytes): computed now `…772b6735af64` — EQUALS the owner FREEZE `sha256_after` and the original REQUEST input pin; the hash string recorded in the sealed 002 VERDICT/REVIEW MATCHED the computed value.
- `tests/test_v2_m4_replay.py` (58147 bytes): computed now `…1035e6ede182` — EQUALS the owner FREEZE `sha256_after` and the original REQUEST input pin; the hash string recorded in the sealed 002 VERDICT/REVIEW DID NOT match the computed value.

The reviewed source bytes are unchanged and identical to the authoritative pins;
only the hash string(s) I hand-transcribed into the 002 report files were wrong.
The on-disk evidence (run streams, probe outputs) was produced by machine hashing
and is unaffected; the sealed 002 artifacts remain untouched (append-only).

## Character-level divergence (recorded in 002 VERDICT.json vs authoritative)

- `src/ak_trading/v2/decision/replay.py`: identical
- `tests/test_v2_m4_replay.py`: {"first_mismatch_index": 20, "mismatch_positions": [20], "recorded_chars_at_positions": {"20": "8"}, "authoritative_chars_at_positions": {"20": "6"}}

## Sealed old reports (unchanged, read-only)

- `replay-hard-review-002/work/REVIEW.md`: SHA-256 `a95dcb77d7dd253840b56bfc7f31675fa7fa215b6470c2bbcbb2d31084a31b30`
- `replay-hard-review-002/work/VERDICT.json`: SHA-256 `bb5cd982e0864c7ab5d4b4773c41f99158185f6340d977c830fc65846094e12a`

## Reused results (no rerun)

- rerun1-replay-file: native exit 0; 20 selected; 20 passed / 0 failed / 0 skipped — evidence `../replay-hard-review-001/work/rerun1-replay-file.*`
- rerun2-eight-file: native exit 1; 205 selected; 191 passed / 14 failed / 0 skipped — evidence `../replay-hard-review-002/work/rerun2-eight-file.*`
- probe_counterexamples_v2: native exit 0; 24/24 checks passed — evidence `../replay-hard-review-002/work/probe-v2.*`

Supplementary check: every other pinned artifact hash recorded in the sealed 002
VERDICT (8-file stdout, probe stdout) matches its actual file: True.

## Effective verdict

**PASS — bounded to the frozen two-file write set, unconditional within that scope.**
Scope, bounds and not-granted items are identical to replay-hard-review-002; the
correction changes only the recorded source-pin strings to the machine-derived
authoritative values. B01 remains an open pre-existing baseline blocker; full M4,
shadow/SHCP7 and M5 acceptance remain not granted.
