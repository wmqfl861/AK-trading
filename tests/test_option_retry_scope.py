from __future__ import annotations

import copy
import csv
import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("option_retry", ROOT / "scripts/update_option_bars_daily.py")
updater = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(updater)


def test_symbols_file_freezes_sorted_bounded_scope_before_services(tmp_path, monkeypatch):
    symbols_file = tmp_path / "symbols.txt"
    symbols_file.write_text("SHFE.cu2610C80000\nDCE.m2610-C-3000\nCZCE.SR701P5000\n", encoding="utf-8")
    captured = []
    monkeypatch.setattr(updater, "coordinate", lambda args: captured.append(args) or 0)
    assert updater.main(["--symbols-file", str(symbols_file), "--expected-through", "2026-09-08"]) == 0
    assert captured[0].requested_symbols == ["CZCE.SR701P5000", "DCE.m2610-C-3000", "SHFE.cu2610C80000"]
    assert captured[0].expected_through == "2026-09-08"
    assert captured[0].shard_count == 6


SYMBOLS = ["DCE.m2610-C-3000", "CZCE.SR701P5000", "SHFE.cu2610C80000", "DCE.i2701-P-700"]


def frozen_run(tmp_path):
    run_dir = tmp_path / "previous"
    run_dir.mkdir()
    manifest = {"run_id": "previous", "symbols": SYMBOLS, "shard_count": 2}
    updater.write_json(run_dir / "manifest.json", manifest)
    for index, statuses in enumerate((("ok", "empty"), ("timeout",))):
        assigned = SYMBOLS[index::2]
        results = [{"symbol": symbol, "status": status, "rows": 2 if status == "ok" else 0,
                    "error_type": None} for symbol, status in zip(assigned, statuses)]
        updater.write_json(run_dir / f"shard-{index}.summary.json", {
            "run_id": manifest["run_id"], "manifest_sha256": updater.manifest_hash(manifest),
            "shard_index": index, "assigned_symbols": assigned, "results": results,
            "pending": assigned[len(results):], "status": "incomplete",
        })
        (run_dir / f"shard-{index}.jsonl").write_text(
            "".join(json.dumps(result) + "\n" for result in results), encoding="utf-8")
    return run_dir


def test_retry_uses_only_failed_empty_and_pending_in_frozen_order(tmp_path, monkeypatch):
    run_dir = frozen_run(tmp_path)
    # A missing coordinator report is normal after interruption; shards carry the hash.
    captured = []
    monkeypatch.setattr(updater, "coordinate", lambda args: captured.append(args) or 0)
    assert updater.main(["--retry-run", str(run_dir)]) == 0
    assert captured[0].requested_symbols == [SYMBOLS[1], SYMBOLS[2], SYMBOLS[3]]
    assert captured[0].retry_source["run_id"] == "previous"
    assert not (run_dir / "report.json").exists()


def bar(day, **overrides):
    timestamp = datetime.fromisoformat(day + "T00:00:00+08:00")
    return {"datetime": int(timestamp.timestamp()) * 1_000_000_000,
            "open": 10, "high": 12, "low": 9, "close": 11, "volume": 3,
            "open_oi": 0, "close_oi": 0, **overrides}


@pytest.fixture
def imports(monkeypatch):
    from ak_trading.persistence import market_data
    captured = []
    def fake_import(engine, filename, **kwargs):
        with Path(filename).open(encoding="utf-8", newline="") as handle:
            captured.append(list(csv.reader(handle)))
        return SimpleNamespace(rows_written=len(captured[-1]) - 1)
    monkeypatch.setattr(market_data, "import_market_bars", fake_import)
    return captured


@pytest.mark.parametrize("days,expected", [
    (["2026-09-07", "2026-09-08", "2026-09-09"], ("ok", 2)),
    (["2026-09-07", "2026-09-09"], ("stale", 1)),
    (["2026-09-09"], ("stale", 0)),
])
def test_cutoff_imports_only_real_closed_rows_and_retains_stale_data(days, expected, tmp_path, imports):
    frame = pd.DataFrame([bar(day, close=float("nan")) if day > "2026-09-08" else bar(day) for day in days])
    original = frame.copy(deep=True)
    api = SimpleNamespace(get_kline_serial=lambda *_: frame, is_serial_ready=lambda _: True)
    details = {}
    result = updater.fetch_and_import(api, None, SYMBOLS[0], None,
                                     SimpleNamespace(content_sha256="hash"), None, 15,
                                     temp_dir=tmp_path, expected_through="2026-09-08", result_details=details)
    assert result == expected
    retained_days = [day for day in days if day <= "2026-09-08"]
    assert details["latest_date"] == (max(retained_days) if retained_days else None)
    assert [row[0][:10] for call in imports for row in call[1:]] == retained_days
    pd.testing.assert_frame_equal(frame, original)
    assert list(tmp_path.iterdir()) == []


@pytest.fixture
def services(monkeypatch):
    from ak_trading import config, market_reference
    from ak_trading.persistence import config as db_config, database, storage
    monkeypatch.setattr(config, "load_settings", lambda: SimpleNamespace(tq_user="fake", tq_pass="fake"))
    monkeypatch.setattr(db_config, "load_database_settings", lambda **_: None)
    monkeypatch.setattr(database, "create_database_engine", lambda _: SimpleNamespace(dispose=lambda: None))
    monkeypatch.setattr(market_reference, "load_reference_book", lambda _: SimpleNamespace(calendar=None))
    monkeypatch.setattr(storage, "store_reference_snapshot", lambda *_, **kw: SimpleNamespace(content_sha256="hash"))
    sdk = ModuleType("tqsdk")
    sdk.TqAuth = lambda *_: None
    errors = ModuleType("tqsdk.exceptions")
    errors.TqTimeoutError = type("TqTimeoutError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "tqsdk", sdk)
    monkeypatch.setitem(sys.modules, "tqsdk.exceptions", errors)
    return sdk


@pytest.mark.parametrize("mode", ["explicit", "retry", "default"])
def test_coordinator_handoff_is_exact_scope_and_six_shards(mode, tmp_path, monkeypatch, services):
    symbols_file = tmp_path / "symbols.txt"
    symbols_file.write_text("\n".join(SYMBOLS[:2]) + "\n", encoding="utf-8")
    if mode == "explicit":
        arguments = ["--symbols-file", str(symbols_file), "--expected-through", "2026-09-08"]
        expected = [SYMBOLS[1], SYMBOLS[0]]
    elif mode == "retry":
        arguments = ["--retry-run", str(frozen_run(tmp_path)), "--expected-through", "2026-09-08"]
        expected = SYMBOLS[1:]
    else:
        arguments = []
        expected = [SYMBOLS[1], SYMBOLS[3], "DCE.m2610", SYMBOLS[0], SYMBOLS[2]]
    class Discovery:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def query_quotes(self, **kwargs):
            assert kwargs == {"ins_class": "OPTION", "expired": False}
            return SYMBOLS + ["DCE.m2610"]  # Provider membership remains mandatory.
    services.TqApi = lambda **_: Discovery()
    monkeypatch.setattr(updater, "DATA_DIR", tmp_path)
    monkeypatch.setattr(updater, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(updater, "existing_workers", lambda: [])
    handoffs = []
    def launch(command, **kwargs):
        run_dir = Path(command[command.index("--run-dir") + 1])
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        digest = command[command.index("--manifest-sha") + 1]
        index = int(command[command.index("--shard-index") + 1])
        assert updater.manifest_hash(manifest) == digest
        assert kwargs["close_fds"] is True
        assigned = manifest["symbols"][index::manifest["shard_count"]]
        handoffs.append(assigned)
        updater.write_json(run_dir / f"shard-{index}.summary.json", {
            "run_id": manifest["run_id"], "manifest_sha256": digest, "shard_index": index,
            "assigned_symbols": assigned, "results": [
                {"symbol": s, "status": "ok", "rows": 1, "latest_date": "2026-09-08"} for s in assigned
            ], "status": "complete", "pending": [],
        })
        return SimpleNamespace(wait=lambda: 0)
    monkeypatch.setattr(updater.subprocess, "Popen", launch)
    assert updater.main(arguments) == 0
    assert handoffs == [[s] for s in expected] + [[] for _ in range(6 - len(expected))]
    run_dir, = (tmp_path / "runs").iterdir()
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    if mode == "default":
        assert set(manifest) == {"run_id", "started_at", "coordinator_pid", "coordinator_created", "symbols",
                                 "shard_count", "bars", "recycle_every", "scope"}
        assert set(report) == {"run_id", "status", "shards_valid", "child_exit_codes", "symbols",
                               "rows_acknowledged", "closed_bar_status", "finished_at"}
        assert manifest["scope"] == "provider_discovered_active_six_exchanges"
        assert report["closed_bar_status"] == "not_certified_provider_latest_may_be_forming"
    else:
        assert manifest["expected_through"] == report["expected_through"] == "2026-09-08"
        assert manifest["scope"] == ("explicit_symbols_file" if mode == "explicit" else "retry_incomplete_symbols")
        assert report["closed_bar_status"] == "raw_timestamp_date_checked_through_cutoff"
        if mode == "retry":
            assert manifest["retry_source"]["run_id"] == "previous"


@pytest.mark.parametrize("latest", [None, "2026-09-07", "2026-09-09", "2026-09-08"])
def test_completion_requires_each_result_actual_latest_date(latest):
    manifest = {"run_id": "freshness", "shard_count": 1, "symbols": SYMBOLS[:2], "expected_through": "2026-09-08"}
    summary = {"run_id": "freshness", "shard_index": 0, "manifest_sha256": updater.manifest_hash(manifest),
               "assigned_symbols": SYMBOLS[:2], "pending": [], "status": "complete", "results": [
                   {"symbol": SYMBOLS[0], "status": "ok", "rows": 1, "latest_date": "2026-09-08"},
                   {"symbol": SYMBOLS[1], "status": "ok", "rows": 1, "latest_date": latest},
               ]}
    assert updater.validate_summary(summary, manifest, 0, 0) is (latest == "2026-09-08")


def worker_manifest(tmp_path, monkeypatch, symbols=None, cutoff="2026-09-08"):
    import psutil
    parent_pid = updater.os.getppid()
    monkeypatch.setattr(psutil, "Process", lambda *_: SimpleNamespace(pid=parent_pid, create_time=lambda: 1.0))
    manifest = {"run_id": "worker", "symbols": symbols or SYMBOLS[:1], "shard_count": 6,
                "coordinator_pid": parent_pid, "coordinator_created": 1.0,
                "bars": 15, "recycle_every": 500}
    if cutoff:
        manifest["expected_through"] = cutoff
    updater.write_json(tmp_path / "manifest.json", manifest)
    return manifest, SimpleNamespace(run_dir=tmp_path, manifest_sha=updater.manifest_hash(manifest), shard_index=0)


@pytest.mark.parametrize("first,second,status,count", [
    ("empty", "ok", "ok", 2), ("not_ready", "ok", "ok", 2),
    ("timeout", "ok", "ok", 2), ("connection", "ok", "ok", 2),
    ("sdk_timeout", "ok", "ok", 2), ("empty", "empty", "empty", 2),
    ("timeout", "timeout", "timeout", 2),
    ("bug", "ok", "error", 1), ("permission", "ok", "error", 1),
    ("stale", "ok", "stale", 1), ("invalid", "ok", "invalid_rows", 1),
])
def test_worker_reconnects_only_transient_failures_and_records_final_date(
    first, second, status, count, tmp_path, monkeypatch, services, imports,
):
    _, args = worker_manifest(tmp_path, monkeypatch)
    outcomes = iter([first, second])
    clients = []
    class API:
        def __init__(self, **_):
            self.outcome = next(outcomes)
            self.closed = False
            clients.append(self)
        def get_kline_serial(self, *_):
            errors = {"timeout": TimeoutError, "connection": ConnectionError, "bug": ValueError,
                      "permission": PermissionError, "sdk_timeout": sys.modules["tqsdk.exceptions"].TqTimeoutError}
            if self.outcome in errors:
                raise errors[self.outcome]("synthetic")
            if self.outcome in ("empty", "not_ready"):
                return pd.DataFrame([bar("2026-09-08", datetime=0)])
            return pd.DataFrame([bar("2026-09-07" if self.outcome == "stale" else "2026-09-08",
                                     close=float("nan") if self.outcome == "invalid" else 11)])
        def is_serial_ready(self, _): return self.outcome != "not_ready"
        def wait_update(self, **_): return False
        def close(self): self.closed = True
    services.TqApi = API
    assert updater.worker(args) == (0 if status == "ok" else 1)
    assert len(clients) == count and all(client.closed for client in clients)
    result, = [json.loads(line) for line in (tmp_path / "shard-0.jsonl").read_text().splitlines()]
    assert result["status"] == status
    if status == "ok":
        assert result["latest_date"] == "2026-09-08" and result["error_type"] is None
    if status == "stale":
        assert result["latest_date"] == "2026-09-07" and result["rows"] == 1
    summary = json.loads((tmp_path / "shard-0.summary.json").read_text())
    assert summary["results"] == [result]


@pytest.mark.parametrize("content", [
    "", "\n", "DCE.m2610\n", "SHFE.cu2610\n", "CZCE.SR701\n",
    "KQ.m@DCE.m\n", "SSE.10000001\n", "../DCE.m2610-C-3000\n",
    "DCE.m2610-C-3000/extra\n", "DCE.m2610-C-3000\\extra\n",
    "DCE.m2610-C-3000 # note\n", "DCE.m2610-C-3000,CZCE.SR701P5000\n",
    " DCE.m2610-C-3000\n", "DCE.m2610-C-3000\t\n", "DCE.m2610-C3000\n",
    "DCE.m2610C-3000\n", "DCE.m2610-X-3000\n", "DCE.m2610-C-3000\n\n",
    "DCE.m2610-C-3000\nDCE.m2610-C-3000\n", "DCE.m2610-C-3000\x00\n",
])
def test_symbols_file_rejects_futures_paths_duplicates_and_line_garbage(content, tmp_path, monkeypatch):
    path = tmp_path / "symbols.txt"
    path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(updater, "coordinate", lambda _: pytest.fail("invalid input reached services"))
    with pytest.raises(SystemExit) as exc:
        updater.main(["--symbols-file", str(path)])
    assert exc.value.code == 2


@pytest.mark.parametrize("symbol", [
    "DCE.m2610-C-3000", "CZCE.SR701P5000", "SHFE.cu2610C80000", "CFFEX.IO2609-P-4000",
    "GFEX.si2610-C-8000", "INE.sc2610C500", "CZCE.CF701MSC15200", "CZCE.OI701MSP10000",
    "DCE.a2611-MS-C-4250", "DCE.m2611-MS-P-2650",
])
def test_symbols_file_accepts_provider_option_forms(symbol, tmp_path, monkeypatch):
    path = tmp_path / "symbols.txt"
    path.write_text(symbol + "\n", encoding="utf-8")
    captured = []
    monkeypatch.setattr(updater, "coordinate", lambda args: captured.append(args.requested_symbols) or 0)
    assert updater.main(["--symbols-file", str(path)]) == 0
    assert captured == [[symbol]]


@pytest.mark.parametrize("count", [1, 1000, 1001])
def test_symbols_file_exact_count_bound(count, tmp_path, monkeypatch):
    path = tmp_path / "symbols.txt"
    path.write_text("\n".join(f"DCE.m2610-C-{strike}" for strike in range(1000, 1000 + count)), encoding="utf-8-sig")
    captured = []
    monkeypatch.setattr(updater, "coordinate", lambda args: captured.append(args.requested_symbols) or 0)
    if count <= 1000:
        assert updater.main(["--symbols-file", str(path)]) == 0
        assert len(captured[0]) == count
    else:
        with pytest.raises(SystemExit) as exc:
            updater.main(["--symbols-file", str(path)])
        assert exc.value.code == 2 and captured == []


@pytest.mark.parametrize("arguments", [
    ["--symbols-file", "missing", "--retry-run", "missing"],
    ["--symbols-file", "missing"], ["--retry-run", "missing"],
    ["--symbols-file", "missing", "--limit", "10"],
    ["--retry-run", "missing", "--exchange", "DCE"],
    ["--expected-through", "2026-9-08"], ["--expected-through", "2026-02-30"],
    ["--expected-through", "20260908"], ["--expected-through", "2026-09-08T00:00:00"],
    ["--worker", "--run-dir", "missing", "--manifest-sha", "hash", "--expected-through", "2026-09-08"],
    ["--worker", "--run-dir", "missing", "--manifest-sha", "hash", "--retry-run", "missing"],
])
def test_invalid_recovery_cli_stops_before_services(arguments, monkeypatch):
    monkeypatch.setattr(updater, "coordinate", lambda _: pytest.fail("services reached"))
    monkeypatch.setattr(updater, "worker", lambda _: pytest.fail("worker reached"))
    with pytest.raises(SystemExit) as exc:
        updater.main(arguments)
    assert exc.value.code == 2


@pytest.mark.parametrize("mutation", [
    "missing_manifest", "changed_manifest", "hash", "run_id", "assignment", "duplicate",
    "wrong_shard", "missing_head", "pending", "log_conflict", "truncated_log",
    "no_hash_anchor", "unknown_status", "bool_rows", "bool_shards",
])
def test_retry_rejects_unverifiable_or_conflicting_artifacts(mutation, tmp_path, monkeypatch):
    run_dir = frozen_run(tmp_path)
    manifest_path = run_dir / "manifest.json"
    summary_path = run_dir / "shard-0.summary.json"
    log_path = run_dir / "shard-0.jsonl"
    summary = json.loads(summary_path.read_text())
    if mutation == "missing_manifest": manifest_path.unlink()
    elif mutation in ("changed_manifest", "bool_shards"):
        manifest = json.loads(manifest_path.read_text())
        manifest["symbols"] = list(reversed(manifest["symbols"]))
        if mutation == "bool_shards": manifest["shard_count"] = True
        updater.write_json(manifest_path, manifest)
    elif mutation == "no_hash_anchor":
        for path in run_dir.glob("*.summary.json"): path.unlink()
    elif mutation == "log_conflict": log_path.write_text("", encoding="utf-8")
    elif mutation == "truncated_log": log_path.write_text('{"symbol":', encoding="utf-8")
    else:
        if mutation == "hash": summary["manifest_sha256"] = "0" * 64
        elif mutation == "run_id": summary["run_id"] = "other-run"
        elif mutation == "assignment": summary["assigned_symbols"].reverse()
        elif mutation == "duplicate": summary["results"][1] = copy.deepcopy(summary["results"][0])
        elif mutation == "wrong_shard": summary["results"][0]["symbol"] = SYMBOLS[1]
        elif mutation == "missing_head": summary["results"].pop(0)
        elif mutation == "pending": summary["pending"] = [SYMBOLS[0]]
        elif mutation == "unknown_status": summary["results"][0]["status"] = "mystery"
        elif mutation == "bool_rows": summary["results"][0]["rows"] = True
        updater.write_json(summary_path, summary)
        log_path.unlink()  # Partition/evidence validation must work even without a log.
    monkeypatch.setattr(updater, "coordinate", lambda _: pytest.fail("unverified retry reached services"))
    with pytest.raises(SystemExit) as exc:
        updater.main(["--retry-run", str(run_dir)])
    assert exc.value.code == 2


@pytest.mark.parametrize("missing", ["summary", "both"])
def test_missing_shard_is_pending_but_valid_prefix_success_is_not_retried(missing, tmp_path):
    run_dir = frozen_run(tmp_path)
    (run_dir / "shard-0.summary.json").unlink()
    if missing == "both":
        (run_dir / "shard-0.jsonl").unlink()
    selected, _ = updater.read_retry_run(run_dir)
    assert selected == (SYMBOLS if missing == "both" else SYMBOLS[1:])


@pytest.mark.parametrize("status", sorted(updater.RETRY_STATUSES))
def test_retry_includes_known_incomplete_result_statuses(status, tmp_path):
    run_dir = frozen_run(tmp_path)
    path = run_dir / "shard-0.summary.json"
    summary = json.loads(path.read_text())
    summary["results"][0]["status"] = status
    updater.write_json(path, summary)
    (run_dir / "shard-0.jsonl").unlink()
    assert updater.read_retry_run(run_dir)[0] == SYMBOLS


def test_all_successful_retry_is_offline_noop_under_existing_lock(tmp_path, monkeypatch):
    run_dir = frozen_run(tmp_path)
    for index in range(2):
        path = run_dir / f"shard-{index}.summary.json"
        summary = json.loads(path.read_text())
        summary.update(status="complete", pending=[], results=[
            {"symbol": s, "status": "ok", "rows": 1} for s in SYMBOLS[index::2]
        ])
        updater.write_json(path, summary)
        (run_dir / f"shard-{index}.jsonl").unlink()
    monkeypatch.setattr(updater, "DATA_DIR", tmp_path)
    monkeypatch.setattr(updater, "RUNS_DIR", tmp_path / "new-runs")
    monkeypatch.setattr(updater, "existing_workers", lambda: [])
    from ak_trading import config
    monkeypatch.setattr(config, "load_settings", lambda: pytest.fail("empty retry opened services"))
    assert updater.main(["--retry-run", str(run_dir)]) == 0
    assert not (tmp_path / "new-runs").exists()
    with updater.run_lock(tmp_path / "option_daily.lock"):
        assert updater.main(["--retry-run", str(run_dir)]) == 2


def test_readiness_waits_once_on_same_serial_and_fixed_future_deadline(tmp_path, monkeypatch, imports):
    frame = pd.DataFrame([bar("2026-09-08", datetime=0)])
    clock = [100.0]
    deadlines = []
    def wait(*, deadline):
        deadlines.append(deadline)
        clock[0] += 1
        if len(deadlines) == 2:
            frame.loc[0] = bar("2026-09-08")
        return True
    monkeypatch.setattr(updater.time, "time", lambda: clock[0])
    monkeypatch.setattr(updater.time, "monotonic", lambda: clock[0])
    api = SimpleNamespace(get_kline_serial=lambda *_: frame,
                          is_serial_ready=lambda _: frame.iloc[0]["datetime"] > 0, wait_update=wait)
    result = updater.fetch_and_import(api, None, SYMBOLS[0], None, SimpleNamespace(content_sha256="hash"),
                                     None, 15, temp_dir=tmp_path, expected_through="2026-09-08")
    assert result == ("ok", 1) and len(imports) == 1
    assert deadlines == [110.0, 110.0]


def test_readiness_never_extends_deadline_for_unrelated_updates(tmp_path, monkeypatch, imports):
    clock = [100.0]
    deadlines = []
    def wait(*, deadline):
        deadlines.append(deadline)
        clock[0] += 4
        return True
    monkeypatch.setattr(updater.time, "time", lambda: clock[0])
    monkeypatch.setattr(updater.time, "monotonic", lambda: clock[0])
    api = SimpleNamespace(get_kline_serial=lambda *_: pd.DataFrame(), is_serial_ready=lambda _: False, wait_update=wait)
    assert updater.fetch_and_import(api, None, SYMBOLS[0], None, None, None, 15, temp_dir=tmp_path) == ("not_ready", 0)
    assert deadlines == [110.0, 110.0, 110.0] and imports == []


def test_cutoff_uses_china_time_raw_timestamp_max_not_row_order(tmp_path, imports):
    frame = pd.DataFrame([bar("2026-09-08", datetime=1788883199999999999),
                          bar("2026-09-07"), bar("2026-09-09")])
    # 1788883199999999999 is 2026-09-08 23:59:59.999999999 in China time.
    api = SimpleNamespace(get_kline_serial=lambda *_: frame, is_serial_ready=lambda _: True)
    details = {}
    assert updater.fetch_and_import(api, None, SYMBOLS[0], None, SimpleNamespace(content_sha256="hash"),
                                    None, 15, temp_dir=tmp_path, expected_through="2026-09-08",
                                    result_details=details) == ("ok", 2)
    assert details["latest_date"] == "2026-09-08"
    assert [row[1] for row in imports[0][1:]] == ["1788883199999999999", str(bar("2026-09-07")["datetime"])]
    assert imports[0][1][0] == "2026-09-08T23:59:59.999999+08:00"


@pytest.mark.parametrize("mode", ["explicit", "retry"])
def test_scope_membership_failure_never_launches_workers(mode, tmp_path, monkeypatch, services):
    if mode == "explicit":
        path = tmp_path / "symbols.txt"
        path.write_text(SYMBOLS[0] + "\n", encoding="utf-8")
        arguments = ["--symbols-file", str(path)]
    else:
        arguments = ["--retry-run", str(frozen_run(tmp_path))]
    class Discovery:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def query_quotes(self, **kwargs): return ["CFFEX.IO2609-C-4000"]
    services.TqApi = lambda **_: Discovery()
    monkeypatch.setattr(updater, "DATA_DIR", tmp_path)
    monkeypatch.setattr(updater, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(updater, "existing_workers", lambda: [])
    monkeypatch.setattr(updater.subprocess, "Popen", lambda *_, **kw: pytest.fail("unknown scope launched workers"))
    assert updater.main(arguments) == 1
    run_dir, = (tmp_path / "runs").iterdir()
    assert not (run_dir / "manifest.json").exists()
    assert json.loads((run_dir / "report.json").read_text())["status"] == "scope_not_in_active_options"


def test_explicit_scope_still_blocks_existing_workers_before_credentials(tmp_path, monkeypatch):
    path = tmp_path / "symbols.txt"
    path.write_text(SYMBOLS[0], encoding="utf-8")
    from ak_trading import config
    monkeypatch.setattr(config, "load_settings", lambda: pytest.fail("credentials read despite existing worker"))
    monkeypatch.setattr(updater, "DATA_DIR", tmp_path)
    monkeypatch.setattr(updater, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(updater, "existing_workers", lambda: [123])
    assert updater.main(["--symbols-file", str(path)]) == 2
    assert not (tmp_path / "runs").exists()


@pytest.mark.parametrize("change", ["hash", "parent"])
def test_worker_rejects_manifest_or_parent_tampering_before_credentials(change, tmp_path, monkeypatch):
    manifest, args = worker_manifest(tmp_path, monkeypatch)
    from ak_trading import config
    monkeypatch.setattr(config, "load_settings", lambda: pytest.fail("worker read credentials before validation"))
    if change == "hash":
        manifest["symbols"] = SYMBOLS[1:]
    else:
        manifest["coordinator_created"] = 2.0
        args.manifest_sha = updater.manifest_hash(manifest)
    updater.write_json(tmp_path / "manifest.json", manifest)
    with pytest.raises(ValueError, match="worker_.*_mismatch"):
        updater.worker(args)


def test_default_worker_keeps_legacy_result_schema_and_provider_latest(tmp_path, monkeypatch, services, imports):
    _, args = worker_manifest(tmp_path, monkeypatch, cutoff=None)
    services.TqApi = lambda **_: SimpleNamespace(
        get_kline_serial=lambda *_: pd.DataFrame([bar("2026-09-07"), bar("2026-09-09")]),
        is_serial_ready=lambda _: True, close=lambda: None,
    )
    assert updater.worker(args) == 0
    result = json.loads((tmp_path / "shard-0.jsonl").read_text())
    assert result == {"symbol": SYMBOLS[0], "status": "ok", "rows": 2, "error_type": None}
    summary = json.loads((tmp_path / "shard-0.summary.json").read_text())
    assert "expected_through" not in summary
    assert summary["closed_bar_status"] == "not_certified_provider_latest_may_be_forming"


def test_zero_import_acknowledgment_cannot_certify_freshness(tmp_path, monkeypatch):
    from ak_trading.persistence import market_data
    monkeypatch.setattr(market_data, "import_market_bars", lambda *_, **kw: SimpleNamespace(rows_written=0))
    api = SimpleNamespace(get_kline_serial=lambda *_: pd.DataFrame([bar("2026-09-08")]), is_serial_ready=lambda _: True)
    assert updater.fetch_and_import(api, None, SYMBOLS[0], None, SimpleNamespace(content_sha256="hash"),
                                    None, 15, temp_dir=tmp_path, expected_through="2026-09-08") == ("no_rows_acknowledged", 0)


def test_freshness_retry_rechecks_claimed_success_without_latest_date(tmp_path):
    run_dir = frozen_run(tmp_path)
    path = run_dir / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["expected_through"] = "2026-09-08"
    updater.write_json(path, manifest)
    for index in range(2):
        path = run_dir / f"shard-{index}.summary.json"
        summary = json.loads(path.read_text())
        summary["manifest_sha256"] = updater.manifest_hash(manifest)
        updater.write_json(path, summary)
    assert updater.read_retry_run(run_dir)[0] == SYMBOLS


def test_worker_circuit_breaker_retains_unprocessed_partition(tmp_path, monkeypatch, services):
    symbols = [f"DCE.m2610-C-{strike}" for strike in range(3000, 3025)]
    _, args = worker_manifest(tmp_path, monkeypatch, symbols=symbols)
    clients = []
    class API:
        def __init__(self, **_): clients.append(self)
        def get_kline_serial(self, *_): raise TimeoutError("synthetic")
        def close(self): pass
    services.TqApi = API
    assert updater.worker(args) == 1
    summary = json.loads((tmp_path / "shard-0.summary.json").read_text())
    assert [r["symbol"] for r in summary["results"]] == [symbols[0], symbols[6], symbols[12]]
    assert summary["pending"] == [symbols[18], symbols[24]]
    assert len(clients) == 6
