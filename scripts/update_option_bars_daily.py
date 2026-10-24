"""Incremental option daily data collection with one coordinator per repository.

Without a cutoff, success means every frozen-scope symbol was imported; the
provider's latest candle may still be forming. --expected-through excludes raw
bar dates after the given China-time date and requires that exact latest date.
Recovery can target a bounded symbols file or the incomplete scope of a past run.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
import csv
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
DAILY = 86400
CST = timezone(timedelta(hours=8))
DATA_DIR = ROOT / "data" / "downloads"
RUNS_DIR = DATA_DIR / "option_daily_runs"
FREE_EXCHANGES = ("SHFE", "DCE", "CZCE", "CFFEX", "INE", "GFEX")
BAR_FIELDS = ("open", "high", "low", "close", "volume", "open_oi", "close_oi")
MAX_EXPLICIT_SYMBOLS = 1000
RETRY_STATUSES = {"error", "failed", "timeout", "not_ready", "empty", "invalid_rows", "no_rows_acknowledged", "stale", "pending"}
# Require a delivery month, option side and strike; futures and paths cannot match.
# MS series options occur in the provider's frozen DCE/CZCE option universe.
OPTION_SYMBOL = re.compile(
    r"(?:"
    r"(?:SHFE|DCE|CZCE|CFFEX|INE|GFEX)\.[A-Za-z]{1,8}[0-9]{3,4}(?:-[CP]-|[CP])"
    r"|DCE\.[a-z]{1,8}[0-9]{4}-MS-[CP]-"
    r"|CZCE\.[A-Z]{1,8}[0-9]{3}MS[CP]"
    r")[0-9]+(?:\.[0-9]+)?"
)


def expected_date(value: str) -> str:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("expected-through must be YYYY-MM-DD")
    date.fromisoformat(value)
    return value


def read_symbols_file(path: Path) -> list[str]:
    symbols = []
    with path.open(encoding="utf-8-sig") as handle:
        for number, line in enumerate(handle, 1):
            symbol = line.rstrip("\r\n")
            if not OPTION_SYMBOL.fullmatch(symbol):
                raise ValueError(f"invalid option symbol on line {number}")
            symbols.append(symbol)
            if len(symbols) > MAX_EXPLICIT_SYMBOLS:
                raise ValueError("symbols-file requires 1..1000 distinct option symbols")
    if not symbols or len(set(symbols)) != len(symbols):
        raise ValueError("symbols-file requires 1..1000 distinct option symbols")
    return sorted(symbols)


def write_json(path: Path, value: object) -> None:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2)
    temp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        temp.write_text(payload + "\n", encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def manifest_hash(manifest: dict) -> str:
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def result_is_success(result: dict, manifest: dict) -> bool:
    return (
        result.get("status") == "ok" and type(result.get("rows")) is int and result["rows"] > 0
        and ("expected_through" not in manifest
             or result.get("latest_date") == manifest["expected_through"])
    )


def read_retry_run(run_dir: Path) -> tuple[list[str], dict]:
    """Recover only incomplete symbols; never trust an unanchored manifest or conflicts."""
    run_dir = run_dir.resolve()
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    if (not isinstance(manifest, dict) or not isinstance(manifest.get("run_id"), str)
            or not manifest["run_id"] or type(manifest.get("shard_count")) is not int
            or not 1 <= manifest["shard_count"] <= 8):
        raise ValueError("invalid retry manifest")
    symbols = manifest.get("symbols")
    if (not isinstance(symbols, list) or not symbols
            or any(not isinstance(s, str) or not OPTION_SYMBOL.fullmatch(s) for s in symbols)
            or len(set(symbols)) != len(symbols)):
        raise ValueError("invalid retry manifest symbols")
    if "expected_through" in manifest:
        expected_date(manifest["expected_through"])
    digest = manifest_hash(manifest)
    anchored = False
    successful = set()
    for index in range(manifest["shard_count"]):
        assigned = symbols[index::manifest["shard_count"]]
        summary_path = run_dir / f"shard-{index}.summary.json"
        log_path = run_dir / f"shard-{index}.jsonl"
        summary = None
        results = []
        if summary_path.exists():
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            if (not isinstance(summary, dict) or summary.get("run_id") != manifest["run_id"]
                    or summary.get("manifest_sha256") != digest
                    or type(summary.get("shard_index")) is not int or summary["shard_index"] != index
                    or summary.get("assigned_symbols") != assigned):
                raise ValueError(f"retry shard {index} manifest mismatch")
            results = summary.get("results")
            anchored = True
        if log_path.exists():
            records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
            if summary is not None and records != results:
                raise ValueError(f"retry shard {index} log/summary mismatch")
            results = records
        if (not isinstance(results, list) or len(results) > len(assigned)
                or any(not isinstance(r, dict) for r in results)
                or [r.get("symbol") for r in results] != assigned[:len(results)]):
            raise ValueError(f"retry shard {index} invalid result partition")
        for result in results:
            if (result.get("status") not in RETRY_STATUSES | {"ok"}
                    or type(result.get("rows")) is not int or result["rows"] < 0):
                raise ValueError(f"retry shard {index} invalid result")
            if result_is_success(result, manifest):
                successful.add(result["symbol"])
        if summary is not None:
            complete = len(results) == len(assigned) and all(result_is_success(r, manifest) for r in results)
            if (summary.get("pending") != assigned[len(results):]
                    or summary.get("status") not in ("complete", "incomplete")
                    or (summary["status"] == "complete" and not complete)):
                raise ValueError(f"retry shard {index} invalid completion evidence")
    if not anchored:
        raise ValueError("retry requires a shard summary anchoring the manifest hash")
    return [s for s in symbols if s not in successful], {
        "run_id": manifest["run_id"], "run_dir": str(run_dir), "manifest_sha256": digest,
    }


@contextmanager
def run_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    os.set_inheritable(handle.fileno(), False)
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        yield
    finally:
        if acquired:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def existing_workers() -> list[int]:
    import psutil
    expected = Path(__file__).resolve()
    found = []
    for proc in psutil.process_iter(["pid", "name"]):
        if proc.pid == os.getpid() or (proc.info["name"] or "").lower() not in ("python.exe", "python"):
            continue
        try:
            for arg in proc.cmdline()[1:]:
                if arg.lower().endswith("update_option_bars_daily.py"):
                    path = Path(arg)
                    if not path.is_absolute():
                        path = Path(proc.cwd()) / path
                    if path.resolve() == expected:
                        found.append(proc.pid)
                        break
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as exc:
            raise RuntimeError("process_detection_unavailable") from exc
    return sorted(found)


def query_active_universe(api) -> list[str]:
    return sorted({s for s in api.query_quotes(ins_class="OPTION", expired=False)
                   if s.split(".")[0] in FREE_EXCHANGES})


def fetch_and_import(api, engine, symbol, db_settings, snapshot, calendar, bars, *, temp_dir,
                     expected_through=None, result_details=None):
    """Keep the (status, rows) return contract; optional evidence comes from this snapshot."""
    if expected_through is not None:
        expected_through = expected_date(expected_through.isoformat() if type(expected_through) is date else expected_through)
        if result_details is not None:
            result_details["latest_date"] = None
    # TqSdk's synchronous get_kline_serial already waits for initial data. Only
    # an unready returned serial needs this additional, fixed 10-second budget.
    frame = api.get_kline_serial(symbol, DAILY, bars)
    if not api.is_serial_ready(frame):
        deadline, expires = time.time() + 10, time.monotonic() + 10
        wait_update = getattr(api, "wait_update", None)
        if wait_update is None:
            return "not_ready", 0
        while not api.is_serial_ready(frame):
            if time.monotonic() >= expires or not wait_update(deadline=deadline):
                return "not_ready", 0
    frame = frame.copy(deep=True)
    if frame.empty:
        return "empty", 0
    valid = frame[frame["datetime"] > 0]
    if valid.empty:
        return "empty", 0
    latest_date = None
    if expected_through is not None:
        days = valid["datetime"].map(lambda nano: datetime.fromtimestamp(int(nano) // 1_000_000_000, tz=CST).date().isoformat())
        valid = valid[days <= expected_through]
        if valid.empty:
            return "stale", 0
        latest_date = days[days <= expected_through].max()
        if result_details is not None:
            result_details["latest_date"] = latest_date
    for row in valid.to_dict("records"):
        if any(not math.isfinite(float(row[name])) or float(row[name]) < 0
               for name in BAR_FIELDS):
            return "invalid_rows", 0
        if row["high"] < max(row["open"], row["close"], row["low"]) or row["low"] > min(row["open"], row["close"]):
            return "invalid_rows", 0
    from ak_trading.persistence.market_data import expected_kline_columns, import_market_bars
    fd, tmp = tempfile.mkstemp(prefix="option-", suffix=".csv", dir=temp_dir)
    os.close(fd)
    try:
        with open(tmp, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(expected_kline_columns(symbol))
            for row in valid.to_dict("records"):
                nano = int(row["datetime"])
                seconds, nanos = divmod(nano, 1_000_000_000)
                ts = datetime.fromtimestamp(seconds, tz=CST) + timedelta(microseconds=nanos // 1000)
                writer.writerow([ts.isoformat(), nano] + [row[name] for name in BAR_FIELDS])
        result = import_market_bars(
            engine, tmp, symbol=symbol, duration_seconds=DAILY, calendar=calendar,
            reference_sha256=snapshot.content_sha256,
            source_label=f"tqsdk_option_daily:{symbol}:{DAILY}", settings=db_settings,
        )
        status = "ok" if result.rows_written > 0 else "no_rows_acknowledged"
        if expected_through is not None and latest_date != expected_through:
            status = "stale"
        return status, result.rows_written
    finally:
        Path(tmp).unlink(missing_ok=True)


def worker(args) -> int:
    run_dir = args.run_dir.resolve()
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    digest = manifest_hash(manifest)
    if digest != args.manifest_sha or not 0 <= args.shard_index < manifest["shard_count"]:
        raise ValueError("worker_manifest_mismatch")
    import psutil
    parent = psutil.Process(os.getppid())
    if parent.pid != manifest["coordinator_pid"] or abs(parent.create_time() - manifest["coordinator_created"]) > 0.001:
        raise ValueError("worker_coordinator_mismatch")
    symbols = manifest["symbols"][args.shard_index::manifest["shard_count"]]
    expected_through = manifest.get("expected_through")
    if expected_through is not None:
        expected_through = expected_date(expected_through)
    from ak_trading.config import load_settings
    from ak_trading.market_reference import load_reference_book
    from ak_trading.persistence.config import load_database_settings
    from ak_trading.persistence.database import create_database_engine
    from ak_trading.persistence.storage import store_reference_snapshot
    from tqsdk import TqApi, TqAuth
    from tqsdk.exceptions import TqTimeoutError

    results = []
    api = None
    consecutive_failures = 0
    engine = None
    try:
        settings = load_settings()
        db_settings = load_database_settings(required=True)
        engine = create_database_engine(db_settings)
        book = load_reference_book(ROOT / "config/market_reference.json")
        snapshot = store_reference_snapshot(engine, ROOT / "config/market_reference.json", settings=db_settings)
        for index, symbol in enumerate(symbols):
            status, rows, error_type = "error", 0, None
            details = {}
            for attempt in range(2):
                details = {"latest_date": None} if expected_through is not None else {}
                error_type = None
                try:
                    if api is None or (attempt == 0 and index and index % manifest["recycle_every"] == 0):
                        if api is not None:
                            api.close()
                            api = None
                        api = TqApi(auth=TqAuth(settings.tq_user, settings.tq_pass))
                    fetch_options = {"temp_dir": run_dir}
                    if expected_through is not None:
                        fetch_options.update(expected_through=expected_through, result_details=details)
                    status, rows = fetch_and_import(api, engine, symbol, db_settings, snapshot,
                                                   book.calendar, manifest["bars"], **fetch_options)
                    if status not in ("empty", "not_ready"):
                        break
                except (TqTimeoutError, ConnectionError, TimeoutError) as exc:
                    error_type = type(exc).__name__
                    status = "timeout"
                except Exception as exc:
                    error_type = type(exc).__name__
                    status = "error"
                    break
                # At most two attempts, each on a fresh connection. Programming,
                # permission and validation failures are reported, never retried.
                if api is not None:
                    try:
                        api.close()
                    except (TqTimeoutError, ConnectionError, TimeoutError):
                        pass
                    api = None
            result = {"symbol": symbol, "status": status, "rows": rows, "error_type": error_type}
            result.update(details)
            results.append(result)
            with (run_dir / f"shard-{args.shard_index}.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(result) + "\n")
            consecutive_failures = consecutive_failures + 1 if status in ("error", "timeout") else 0
            if (index + 1) % 50 == 0:
                print(f"shard={args.shard_index} processed={index+1}/{len(symbols)}", flush=True)
            if consecutive_failures >= 3:
                break
    finally:
        try:
            if api is not None:
                api.close()
        finally:
            if engine is not None:
                engine.dispose()
    complete = len(results) == len(symbols) and all(result_is_success(r, manifest) for r in results)
    summary = {
        "run_id": manifest["run_id"], "manifest_sha256": digest,
        "shard_index": args.shard_index, "assigned_symbols": symbols,
        "results": results, "pending": symbols[len(results):],
        "status": "complete" if complete else "incomplete",
        "closed_bar_status": closed_bar_status(manifest),
        "finished_at": datetime.now(CST).isoformat(),
    }
    if expected_through is not None:
        summary["expected_through"] = expected_through
    write_json(run_dir / f"shard-{args.shard_index}.summary.json", summary)
    return 0 if complete else 1


def validate_summary(summary, manifest, index, returncode):
    expected = manifest["symbols"][index::manifest["shard_count"]]
    if not isinstance(summary, dict):
        return False
    results = summary.get("results", [])
    return (
        returncode == 0 and summary.get("run_id") == manifest["run_id"]
        and summary.get("manifest_sha256") == manifest_hash(manifest)
        and type(summary.get("shard_index")) is int and summary["shard_index"] == index
        and summary.get("assigned_symbols") == expected
        and isinstance(results, list) and all(isinstance(r, dict) for r in results)
        and [r.get("symbol") for r in results] == expected
        and all(result_is_success(r, manifest) for r in results)
        and summary.get("status") == "complete" and summary.get("pending") == []
    )


def closed_bar_status(manifest):
    return ("raw_timestamp_date_checked_through_cutoff" if "expected_through" in manifest
            else "not_certified_provider_latest_may_be_forming")


def coordinate(args):
    import psutil
    with run_lock(DATA_DIR / "option_daily.lock"):
        peers = existing_workers()
        if peers:
            print(json.dumps({"status": "blocked_existing_run", "pids": peers}))
            return 2
        requested = args.requested_symbols
        if requested == []:
            print(json.dumps({"status": "nothing_to_retry", "symbols": 0}))
            return 0
        run_id = datetime.now(CST).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:12]
        run_dir = RUNS_DIR / run_id
        run_dir.mkdir(parents=True)
        from ak_trading.config import load_settings
        from tqsdk import TqApi, TqAuth
        settings = load_settings()
        try:
            with TqApi(auth=TqAuth(settings.tq_user, settings.tq_pass)) as api:
                symbols = query_active_universe(api)
        except Exception as exc:
            write_json(run_dir / "report.json", {"status": "discovery_failed", "error_type": type(exc).__name__})
            return 1
        if requested is not None:
            missing = sorted(set(requested) - set(symbols))
            if missing:
                report = {"status": "scope_not_in_active_options", "missing_symbols": missing}
                write_json(run_dir / "report.json", report)
                print(json.dumps(report), flush=True)
                return 1
            symbols = requested
        elif args.exchange:
            symbols = [s for s in symbols if s.split(".")[0] in args.exchange]
        if args.limit:
            symbols = symbols[:args.limit]
        if not symbols:
            write_json(run_dir / "report.json", {"status": "empty_discovery"})
            return 1
        manifest = {
            "run_id": run_id, "started_at": datetime.now(CST).isoformat(),
            "coordinator_pid": os.getpid(), "coordinator_created": psutil.Process().create_time(),
            "symbols": symbols, "shard_count": args.shard_count, "bars": args.bars,
            "recycle_every": args.recycle_every, "scope": "provider_discovered_active_six_exchanges",
        }
        if args.symbols_file is not None:
            manifest["scope"] = "explicit_symbols_file"
        elif args.retry_source is not None:
            manifest["scope"] = "retry_incomplete_symbols"
            manifest["retry_source"] = args.retry_source
        if args.expected_through is not None:
            manifest["expected_through"] = args.expected_through
        write_json(run_dir / "manifest.json", manifest)
        digest = manifest_hash(manifest)
        with ExitStack() as stack:
            children = []
            for index in range(args.shard_count):
                out = stack.enter_context((run_dir / f"shard-{index}.out.log").open("w", encoding="utf-8"))
                err = stack.enter_context((run_dir / f"shard-{index}.err.log").open("w", encoding="utf-8"))
                cmd = [sys.executable, "-B", "-u", str(Path(__file__).resolve()), "--worker",
                       "--run-dir", str(run_dir), "--manifest-sha", digest, "--shard-index", str(index)]
                children.append(subprocess.Popen(cmd, cwd=ROOT, stdout=out, stderr=err, close_fds=True))
            codes = [child.wait() for child in children]
        valid, rows = [], 0
        for index, code in enumerate(codes):
            try:
                summary = json.loads((run_dir / f"shard-{index}.summary.json").read_text(encoding="utf-8"))
                ok = validate_summary(summary, manifest, index, code)
                rows += sum(r.get("rows", 0) for r in summary.get("results", []))
            except (OSError, ValueError, TypeError):
                ok = False
            valid.append(ok)
        report = {"run_id": run_id, "status": "complete" if all(valid) else "incomplete",
                  "shards_valid": valid, "child_exit_codes": codes, "symbols": len(symbols),
                  "rows_acknowledged": rows, "closed_bar_status": closed_bar_status(manifest),
                  "finished_at": datetime.now(CST).isoformat()}
        if args.expected_through is not None:
            report["expected_through"] = args.expected_through
        write_json(run_dir / "report.json", report)
        print(json.dumps(report), flush=True)
        return 0 if all(valid) else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard-count", type=int, default=6)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--bars", type=int, default=15)
    parser.add_argument("--recycle-every", type=int, default=500)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--exchange", action="append", choices=FREE_EXCHANGES)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--symbols-file", type=Path, help="UTF-8 file with 1..1000 distinct option symbols, one per line")
    scope.add_argument("--retry-run", type=Path, help="Previous run directory; retry only failed, empty or pending symbols")
    parser.add_argument("--expected-through", help="Require the latest retained raw bar date (China time) to equal YYYY-MM-DD")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--run-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--manifest-sha", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not 1 <= args.shard_count <= 8 or not 1 <= args.bars <= 10000 or args.recycle_every < 1 or args.limit < 0:
        parser.error("invalid shard count, bar count, recycle interval or limit")
    if args.worker and (args.run_dir is None or not args.manifest_sha):
        parser.error("worker requires a coordinator manifest")
    if args.worker and (args.symbols_file is not None or args.retry_run is not None or args.expected_through is not None):
        parser.error("worker scope and cutoff must come from its coordinator manifest")
    if (args.symbols_file is not None or args.retry_run is not None) and (args.exchange or args.limit):
        parser.error("explicit recovery scope cannot be combined with --exchange or --limit")
    args.requested_symbols = None
    args.retry_source = None
    try:
        if args.expected_through is not None:
            args.expected_through = expected_date(args.expected_through)
        if args.symbols_file is not None:
            args.requested_symbols = read_symbols_file(args.symbols_file)
        elif args.retry_run is not None:
            args.requested_symbols, args.retry_source = read_retry_run(args.retry_run)
    except (OSError, ValueError, TypeError) as exc:
        parser.error(str(exc))
    try:
        return worker(args) if args.worker else coordinate(args)
    except (BlockingIOError, PermissionError):
        print(json.dumps({"status": "blocked_existing_run_or_lock_unavailable"}))
        return 2
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
