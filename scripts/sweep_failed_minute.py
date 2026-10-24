"""One-shot sweep for contract-minute blocks that failed under DB contention.

Reads the failed minute records from the pre-purge ledger backup and re-runs
the standard download+import CLI for each (contract, year) with a small
process pool. Imports are idempotent upserts, so partial blocks from the
timed-out attempts are simply completed in place.
"""

from __future__ import annotations

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = sorted((ROOT / "data" / "downloads").glob(
    "backfill_contract_progress.jsonl.bak_*"
))[-1]
LOG = ROOT / "data" / "downloads" / "sweep_failed_minute.log"
PYTHON = sys.executable
WORKERS = 10


def main() -> int:
    records = []
    seen = set()
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("task") != "minute" or record.get("status") != "failed":
            continue
        key = (record.get("symbol"), record.get("year"))
        if key in seen or not key[0] or not key[1]:
            continue
        seen.add(key)
        records.append(key)
    LOG.write_text(
        f"sweep start {len(records)} blocks from {LEDGER.name}\n",
        encoding="utf-8",
    )
    print(f"{len(records)} blocks to sweep", flush=True)

    def run_one(item: tuple[str, int]) -> dict:
        symbol, year = item
        command = [
            PYTHON, str(ROOT / "scripts" / "download_market_data.py"),
            "--symbol", symbol,
            "--start", f"{year}-01-01",
            "--end", f"{year}-12-31",
            "--duration", "60",
            "--acknowledge-data-cost",
        ]
        env_note = ""
        try:
            import os

            env = dict(os.environ)
            env["AK_DATABASE_STATEMENT_TIMEOUT_MS"] = "120000"
            completed = subprocess.run(
                command, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=1500, env=env, cwd=str(ROOT),
            )
            tail = (completed.stdout or "").strip().splitlines()
            last = tail[-1] if tail else ""
            if "DOWNLOAD_IMPORTED" in last:
                status = "ok"
            elif "DOWNLOAD_FAIL" in "".join(tail):
                status = "fail"
            else:
                status = f"exit{completed.returncode}"
            env_note = last[:160]
        except subprocess.TimeoutExpired:
            status = "timeout"
        return {"symbol": symbol, "year": year, "status": status,
                "note": env_note}

    stats = {"ok": 0, "fail": 0, "timeout": 0}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(run_one, item): item for item in records}
        for index, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            stats[result["status"]] = stats.get(result["status"], 0) + 1
            line = json.dumps(result, ensure_ascii=False)
            with LOG.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            if index % 25 == 0 or index == len(records):
                print(f"{index}/{len(records)} {stats}", flush=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"sweep done {stats}\n")
    print("sweep done", stats, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
