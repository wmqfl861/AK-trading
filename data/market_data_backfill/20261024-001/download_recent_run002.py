"""Bounded LR data operation and safe TLS-error projection; Root supplies stdin."""

import hashlib
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TASK = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))
from backfill_recent_free import fetch_recent_frame, select_valid_rows
from ak_trading.persistence.market_data import write_recent_kline_frame_csv


def safe_error(exc, secrets):
    queue, seen, errors = [exc], set(), []
    tokens = ("CERTIFICATE_VERIFY_FAILED", "UNEXPECTED_EOF_WHILE_READING",
              "WRONG_VERSION_NUMBER", "TLSV1_ALERT_INTERNAL_ERROR", "SSLEOFError",
              "SSLCertVerificationError", "MaxRetryError", "ConnectionResetError")
    while queue and len(errors) < 12:
        item = queue.pop(0)
        if not isinstance(item, BaseException) or id(item) in seen:
            continue
        seen.add(id(item))
        message = str(item)
        for secret in secrets:
            if secret:
                message = message.replace(secret, "[REDACTED]")
        hosts = sorted(set(re.findall(r"\b(?:[a-z0-9-]+\.)+shinnytech\.com\b", message.lower())))
        record = {"type": type(item).__name__, "hosts": hosts,
                  "reason_tokens": [token for token in tokens if token in message]}
        for name in ("errno", "verify_code"):
            value = getattr(item, name, None)
            if type(value) is int:
                record[name] = value
        errors.append(record)
        queue.extend([item.__cause__, item.__context__, getattr(item, "reason", None)])
        queue.extend(arg for arg in item.args if isinstance(arg, BaseException))
    return errors


def main():
    output = TASK / "download-run002"
    if output.exists():
        raise FileExistsError("download-run002 is single use")
    if version("tqsdk") != "3.10.2":
        raise ValueError("existing project SDK 3.10.2 is required")
    payload = json.load(sys.stdin)
    secrets = (payload["tq_user"], payload["tq_pass"])
    from tqsdk import TqApi, TqAuth, TqSim

    output.mkdir()
    api = None
    result = {"operation": "20261024-001-download-run002", "sdk_version": version("tqsdk"),
              "channel": "tqsdk_free_realtime_recent_serial", "enable_trading": False,
              "broker": "NOT_RUN", "real_sends": 0, "real_cancels": 0,
              "target_complete_trading_day": "2026-10-23", "series": []}
    failed = False
    context = "session"
    started = time.monotonic()
    try:
        api = TqApi(account=TqSim(), auth=TqAuth(*secrets), disable_print=True)
        payload.clear()
        for symbol, duration, rows, name in [
            ("KQ.m@CZCE.LR", 60, 10000, "LR-main-60"),
            ("KQ.i@CZCE.LR", 86400, 128, "LR-index-86400"),
        ]:
            entry = {"symbol": symbol, "duration_seconds": duration, "requested_rows": rows}
            result["series"].append(entry)
            try:
                context = "fetch"
                remaining = 240 - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError("operation wall budget exhausted")
                frame = fetch_recent_frame(api, symbol, duration, rows, min(90, remaining))
                raw = output / (name + ".raw.csv")
                frame.to_csv(raw, index=False)
                entry.update({"frame_rows": len(frame), "raw_path": raw.relative_to(ROOT).as_posix(),
                              "raw_sha256": hashlib.sha256(raw.read_bytes()).hexdigest()})
                quote = api.get_quote(symbol)
                entry["quote_metadata"] = {
                    key: quote.get(key) for key in ("datetime", "ins_class", "underlying_symbol", "expired")
                }
                context = "validation"
                valid = select_valid_rows(frame, duration=duration)
                cutoff = datetime(2026, 10, 23, 16, tzinfo=timezone(timedelta(hours=8)))
                end = int(cutoff.timestamp()) * 1_000_000_000
                if duration == 86400:
                    end = int(datetime(2026, 10, 24, tzinfo=cutoff.tzinfo).timestamp()) * 1_000_000_000
                    closed = valid.loc[valid["datetime"] < end].copy(deep=True)
                else:
                    closed = valid.loc[valid["datetime"] <= end - duration * 1_000_000_000].copy(deep=True)
                entry.update({"valid_rows": len(valid), "selected_closed_rows": len(closed),
                              "first_datetime_nano": int(valid["datetime"].iloc[0]) if len(valid) else None,
                              "last_datetime_nano": int(valid["datetime"].iloc[-1]) if len(valid) else None})
                if len(closed):
                    path = output / (name + ".closed.csv")
                    written = write_recent_kline_frame_csv(closed, path, symbol=symbol)
                    entry.update({"closed_path": path.relative_to(ROOT).as_posix(),
                                  "closed_rows_written": written.rows,
                                  "closed_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
                entry["status"] = "RAW_STAGED" if len(valid) else "SOURCE_SERIAL_EMPTY_UNEXPLAINED"
            except Exception as exc:
                failed = True
                entry.update({"status": "FAILED", "context": context, "safe_error_chain": safe_error(exc, secrets)})
            print(json.dumps({"symbol": symbol, "status": entry["status"]}), flush=True)
        result["lr_unexpired_futures"] = api.query_quotes(
            ins_class="FUTURE", exchange_id="CZCE", product_id="LR", expired=False
        )
    except Exception as exc:
        failed = True
        result["failure"] = {"context": context, "safe_error_chain": safe_error(exc, secrets)}
    finally:
        payload.clear()
        if api is not None:
            try:
                api.close()
            except Exception as exc:
                failed = True
                result["close_failure"] = safe_error(exc, secrets)
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
