"""Keep twenty contract-minute workers alive until the trial deadline.

Every CHECK_INTERVAL seconds, for each partition group: relaunch a missing
worker, or kill+relaunch a worker whose output log has gone stale. Matching
relies on each group's unique anchor product (first product of the group),
which appears in the worker's --products command line. Stops relaunching
after DEADLINE (trial expiry guard) and exits once all groups report done.
"""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DL = ROOT / "data" / "downloads"
GROUPS = 20
CHECK_INTERVAL = 300
STALE_SECONDS = 1200
DEADLINE = datetime.fromisoformat("2026-09-05T13:30:00+08:00")
PYTHON = r"D:\Programs\Python\Python312\python.exe"


def log(message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    with (DL / "minute_watchdog.log").open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp} {message}\n")


def anchor(group: int) -> str:
    return (DL / f"bmin20_{group}.txt").read_text().strip().split(",")[0]


def worker_pids(group: int) -> list[int]:
    try:
        output = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             f"Where-Object {{$_.CommandLine -like '*--products*{anchor(group)},*'}}).ProcessId"],
            capture_output=True, text=True, timeout=60,
        ).stdout
        return [int(line) for line in output.split() if line.strip().isdigit()]
    except Exception as exc:  # noqa: BLE001
        log(f"group{group} pid-scan error: {exc}")
        return []


def log_age_seconds(group: int) -> float:
    path = DL / f"bmin20_{group}.out.log"
    if not path.exists():
        return 1e9
    return time.time() - path.stat().st_mtime


def launch(group: int) -> None:
    products = (DL / f"bmin20_{group}.txt").read_text().strip()
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"Start-Process '{PYTHON}' -ArgumentList "
         f"'-u','scripts\\backfill_contract_bars.py','--task','minute',"
         f"'--products','{products}' "
         f"-WorkingDirectory '{ROOT}' -WindowStyle Hidden "
         f"-RedirectStandardOutput '{DL}\\bmin20_{group}.out.log' "
         f"-RedirectStandardError '{DL}\\bmin20_{group}.err.log'"],
        capture_output=True, text=True, timeout=60,
    )
    log(f"group{group} launched")


def main() -> int:
    log("watchdog start")
    idle_rounds = 0
    while datetime.now(DEADLINE.tzinfo) < DEADLINE:
        acted = False
        for group in range(GROUPS):
            pids = worker_pids(group)
            age = log_age_seconds(group)
            if len(pids) > 1:
                # Duplicate workers on one partition risk upsert deadlocks;
                # keep the newest (highest pid) only.
                for pid in sorted(pids)[:-1]:
                    subprocess.run(
                        ["powershell", "-NoProfile", "-Command",
                         f"Stop-Process -Id {pid} -Force"],
                        capture_output=True, timeout=30,
                    )
                log(f"group{group} killed duplicate {sorted(pids)[:-1]}")
                acted = True
            elif not pids:
                launch(group)
                acted = True
            elif age > STALE_SECONDS:
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     f"Stop-Process -Id {pids[0]} -Force"],
                    capture_output=True, timeout=30,
                )
                log(f"group{group} killed stale worker {pids[0]} (age {age:.0f}s)")
                launch(group)
                acted = True
        idle_rounds = 0 if acted else idle_rounds + 1
        # Exit when everything has been quiet-finished for a long stretch.
        if idle_rounds > 576:
            log("watchdog exit: long idle")
            return 0
        time.sleep(CHECK_INTERVAL)
    log("watchdog exit: deadline reached")
    return 0


if __name__ == "__main__":
    sys.exit(main())
