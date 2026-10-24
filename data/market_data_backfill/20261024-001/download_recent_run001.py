"""One bounded data operation; Root injects authorized TqAuth input on stdin."""

import hashlib
import json
import sys
import time
from collections import Counter
from datetime import date
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TASK = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from fill_recent_main_minute import (
    fetch_snapshot,
    load_reference_book,
    select_closed_frame,
    write_recent_kline_frame_csv,
    datetime_from_nano,
    trading_day_for_timestamp,
)


def main():
    output = TASK / "download-run001"
    if output.exists():
        raise FileExistsError("download-run001 is single use")
    symbols = ["KQ.m@SHFE.rb", "KQ.m@CZCE.TA", "KQ.m@DCE.m"]
    tracked = (ROOT / "data/downloads/kqm_symbols.txt").read_text(encoding="utf-8-sig").splitlines()
    if not set(symbols).issubset(tracked):
        raise ValueError("requested symbols are not in existing tracked universe")
    if version("tqsdk") != "3.10.2":
        raise ValueError("expected existing project SDK 3.10.2 runtime")
    book = load_reference_book(ROOT / "config/market_reference.json")
    payload = json.load(sys.stdin)
    from tqsdk import TqApi, TqAuth, TqSim

    output.mkdir()
    api = None
    started = time.monotonic()
    result = {
        "operation": "20261024-001-download-run001",
        "channel": "tqsdk_free_realtime_recent_serial",
        "sdk_version": version("tqsdk"),
        "duration_seconds": 60,
        "requested_rows": 10000,
        "target_complete_trading_day": "2026-10-23",
        "symbols": [],
        "enable_trading": False,
        "broker": "NOT_RUN",
        "real_sends": 0,
        "real_cancels": 0,
    }
    context = "session"
    failed = False
    try:
        api = TqApi(account=TqSim(), auth=TqAuth(payload["tq_user"], payload["tq_pass"]))
        payload.clear()
        for symbol in symbols:
            entry = {"symbol": symbol}
            result["symbols"].append(entry)
            context = "fetch"
            remaining = 420 - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError("operation wall budget exhausted")
            frame = fetch_snapshot(api, symbol, 10000, min(120, remaining))
            context = "validation"
            valid, details = select_closed_frame(frame, calendar=book.calendar, through=date(2026, 10, 23))
            if valid.empty or details["unknown_trading_day_rows"]:
                raise ValueError("empty or unknown-trading-day result")
            path = output / (symbol.replace("@", "_at_").replace(".", "_") + "-60.csv")
            context = "write"
            written = write_recent_kline_frame_csv(valid, path, symbol=symbol)
            if written.rows != len(valid):
                raise ValueError("write row acknowledgement differs")
            labels = [trading_day_for_timestamp(datetime_from_nano(int(nano)), book.calendar)[0].isoformat()
                      for nano in valid["datetime"]]
            entry.update({
                "status": "STAGED_VALIDATED",
                "rows": len(valid),
                "first_datetime_nano": int(valid["datetime"].iloc[0]),
                "last_datetime_nano": int(valid["datetime"].iloc[-1]),
                "raw_latest": details["raw_latest"],
                "selected_latest": details["latest"],
                "coverage_rows_by_trading_day": dict(sorted(Counter(labels).items())),
                "path": path.relative_to(ROOT).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            })
            print(json.dumps({"symbol": symbol, "status": entry["status"], "rows": len(valid)}, ensure_ascii=True), flush=True)
    except Exception as exc:
        failed = True
        result["failure"] = {"context": context, "exception_type": type(exc).__name__}
    finally:
        payload.clear()
        if api is not None:
            try:
                api.close()
            except Exception as exc:
                failed = True
                result["close_failure"] = {"exception_type": type(exc).__name__}
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        result["status"] = "PARTIAL_OR_FAILED" if failed else "STAGING_COMPLETE"
        text = json.dumps(result, indent=2, ensure_ascii=True) + "\n"
        json.loads(text)
        temporary = output / "RESULT.json.tmp"
        temporary.write_text(text, encoding="utf-8", newline="\n")
        temporary.replace(output / "RESULT.json")
    print(json.dumps({"operation": result["operation"], "status": result["status"]}), flush=True)
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
