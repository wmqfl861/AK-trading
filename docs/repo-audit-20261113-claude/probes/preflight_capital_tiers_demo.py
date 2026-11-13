"""Round-5 external demonstration (advisory, read-only): what the K04 preflight
reports at five equity levels.

It reuses the SYNTHETIC world builders of ``tests/test_preflight_v2_research.py``
with rebar-like numbers (price 3500, daily range 60, multiplier 10, 372 trading
days, one DAILY trend strategy) and runs ``scripts/preflight_v2_research.py``
once per equity level into a temporary directory. Nothing in the repository is
written.

Columns: configured equity, exit code, ``structurally_empty``, capital verdict,
the witness's required equity (only present when a witness exists), calibration
verdict, calibration sample upper bound / policy minimum.

What to look at (REVIEW Z03):
    * below the feasible level the tool answers UNKNOWN with exit 4 and
      ``structurally_empty`` null, although no instant in the whole window can
      fund one lot;
    * the calibration bound counts the whole authorized window (367) while the
      split's calibration window is 62 days.

Run (repository root):
    python docs/repo-audit-20261113-claude/probes/preflight_capital_tiers_demo.py
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from decimal import Decimal as D
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for entry in ("src", "scripts", "tests"):
    sys.path.insert(0, str(ROOT / entry))

import test_preflight_v2_research as helpers  # noqa: E402

DAYS = 372
EQUITIES = ("1000000", "300000", "100000", "50000", "15000")
STRATEGY = "trend_d1_v2"
DAY_NANO = 86_400_000_000_000


def main() -> int:
    sessions = helpers._sessions(DAYS)
    bars = helpers._bars(DAYS, 2, sessions, price=D("3500"), span=D("60"))
    rows = []
    split = None
    with tempfile.TemporaryDirectory(prefix="r5-preflight-demo-") as scratch:
        for equity in EQUITIES:
            work = Path(scratch) / f"equity-{equity}"
            work.mkdir()
            bundle, config, scope = work / "bundle", work / "config.json", work / "scope.json"
            helpers._write_bundle(bundle, days=DAYS, sessions=sessions, bars=bars)
            written = helpers._write_config(config, equity=equity, strategy_ids=[STRATEGY], days=DAYS)
            split = written["split_specs"][0]
            helpers._write_scope(scope, bundle, config)
            with contextlib.redirect_stdout(io.StringIO()):
                code = helpers._run(bundle=bundle, config=config, output=work / "out", scope=scope)
            report = helpers._report(work / "out")
            capital = report["checks"]["capital"]["evidence"][f"{STRATEGY}|{helpers.CONTRACT}"]
            calibration = next(iter(report["checks"]["calibration"]["evidence"].values()))
            rows.append((equity, code, report["structurally_empty"], capital["verdict"],
                         (capital.get("witness") or {}).get("required_equity_cny"),
                         report["checks"]["calibration"]["verdict"],
                         f'{calibration["sample_upper_bound"]}/{calibration["minimum_samples"]}'))
    print("split days: train", (split["train_end_nano"] - split["train_start_nano"]) // DAY_NANO,
          "calibration", (split["calibration_end_nano"] - split["calibration_start_nano"]) // DAY_NANO,
          "test", (split["test_end_nano"] - split["test_start_nano"]) // DAY_NANO)
    print(f"{'equity':>9} {'exit':>4} {'structurally_empty':>19} {'capital':>9} "
          f"{'witness needs':>14} {'calibration':>12} {'samples/min':>12}")
    for equity, code, empty, verdict, required, calibration, samples in rows:
        required_text = "-" if required is None else f"{D(required):,.0f}"
        print(f"{equity:>9} {code:>4} {json.dumps(empty):>19} {verdict:>9} "
              f"{required_text:>14} {calibration:>12} {samples:>12}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
