"""Second-pass retry for contract-minute blocks that failed on auth SSL blips."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RETRY = ROOT / "data" / "downloads" / "sweep_retry_list.json"
LOG = ROOT / "data" / "downloads" / "sweep_retry2.log"
PYTHON = sys.executable


def run_one(item: dict) -> dict:
    symbol, year = item["symbol"], item["year"]
    command = [
        PYTHON, str(ROOT / "scripts" / "download_market_data.py"),
        "--symbol", symbol,
        "--start", f"{year}-01-01",
        "--end", f"{year}-12-31",
        "--duration", "60",
        "--acknowledge-data-cost",
    ]
    env = dict(os.environ)
    env["AK_DATABASE_STATEMENT_TIMEOUT_MS"] = "120000"
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=1500, env=env, cwd=str(ROOT),
        )
        text = completed.stdout or ""
        if "DOWNLOAD_IMPORTED" in text:
            status = "ok"
        elif "DOWNLOAD_FAIL" in text:
            status = "fail"
        else:
            status = f"exit{completed.returncode}"
        note = text.strip().splitlines()[-1][:160] if text.strip() else ""
    except subprocess.TimeoutExpired:
        status, note = "timeout", ""
    return {"symbol": symbol, "year": year, "status": status, "note": note}


def main() -> int:
    items = json.loads(RETRY.read_text(encoding="utf-8"))
    LOG.write_text(f"retry2 start {len(items)}\n", encoding="utf-8")
    stats: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(run_one, item) for item in items]
        for index, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            stats[result["status"]] = stats.get(result["status"], 0) + 1
            with LOG.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(result, ensure_ascii=False) + "\n")
            if index % 20 == 0 or index == len(items):
                print(f"{index}/{len(items)} {stats}", flush=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"retry2 done {stats}\n")
    print("retry2 done", stats, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
