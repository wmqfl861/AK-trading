from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("option_daily", ROOT / "scripts/update_option_bars_daily.py")
updater = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(updater)


def manifest():
    return {"run_id": "new-run", "symbols": ["SHFE.a", "DCE.b", "CZCE.c"], "shard_count": 2}


def summary(m):
    assigned = m["symbols"][::2]
    return {"run_id": m["run_id"], "manifest_sha256": updater.manifest_hash(m),
            "shard_index": 0, "assigned_symbols": assigned,
            "results": [{"symbol": s, "status": "ok", "rows": 15} for s in assigned],
            "status": "complete", "pending": []}


@pytest.mark.parametrize("mutation", ["stale", "hash", "exit", "missing", "duplicate", "empty", "zero", "bool", "pending", "shard"])
def test_summary_rejects_stale_missing_or_false_success(mutation):
    m = manifest()
    s = summary(m)
    code = 0
    if mutation == "stale": s["run_id"] = "old-bounded-test"
    elif mutation == "hash": s["manifest_sha256"] = "wrong"
    elif mutation == "exit": code = 1
    elif mutation == "missing": s["results"].pop()
    elif mutation == "duplicate": s["results"][1] = copy.deepcopy(s["results"][0])
    elif mutation == "empty": s["results"][0]["status"] = "empty"
    elif mutation == "zero": s["results"][0]["rows"] = 0
    elif mutation == "bool": s["results"][0]["rows"] = True
    elif mutation == "pending": s["pending"] = ["SHFE.a"]
    elif mutation == "shard": s["shard_index"] = 1
    assert not updater.validate_summary(s, m, 0, code)


def test_summary_requires_exact_partition_and_positive_imports():
    m = manifest()
    assert updater.validate_summary(summary(m), m, 0, 0)
    assert not updater.validate_summary({"shard": "0/1", "ok": 50}, m, 0, 0)


def test_process_lock_blocks_manual_and_scheduler_overlap(tmp_path):
    with updater.run_lock(tmp_path / "lock"):
        with pytest.raises(OSError):
            with updater.run_lock(tmp_path / "lock"):
                pytest.fail("overlap acquired lock")
    with updater.run_lock(tmp_path / "lock"):
        pass


def test_existing_run_blocks_before_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "DATA_DIR", tmp_path)
    monkeypatch.setattr(updater, "existing_workers", lambda: [123])
    assert updater.main([]) == 2
    assert not (tmp_path / "manifest.json").exists()


def test_single_discovery_is_sorted_deduplicated_and_six_exchange_only():
    api = SimpleNamespace(query_quotes=lambda **_: ["SHFE.a", "SSE.x", "DCE.b", "SHFE.a", "KQ.x"])
    assert updater.query_active_universe(api) == ["DCE.b", "SHFE.a"]


@pytest.mark.parametrize("status", ["not_ready", "empty", "invalid_rows"])
def test_nonready_empty_invalid_never_import(status, tmp_path):
    row = {"datetime": 0, **{k: 0 for k in updater.BAR_FIELDS}}
    if status == "invalid_rows": row.update(datetime=1, close=float("nan"))
    api = SimpleNamespace(get_kline_serial=lambda *_: pd.DataFrame([row]),
                          is_serial_ready=lambda _: status != "not_ready")
    assert updater.fetch_and_import(api, None, "SHFE.a", None, None, None, 15, temp_dir=tmp_path) == (status, 0)


def test_ready_snapshot_does_not_wait_on_past_absolute_deadline(tmp_path, monkeypatch):
    from ak_trading.persistence import market_data
    original = pd.DataFrame([{"datetime": 1788768000000000000, "open": 10, "high": 12,
                              "low": 9, "close": 11, "volume": 3, "open_oi": 0, "close_oi": 0}])
    calls = []
    def fake_import(engine, filename, **kwargs):
        calls.append(Path(filename).read_text(encoding="utf-8"))
        return SimpleNamespace(rows_written=1)
    monkeypatch.setattr(market_data, "import_market_bars", fake_import)
    api = SimpleNamespace(get_kline_serial=lambda *_: original, is_serial_ready=lambda _: True)
    result = updater.fetch_and_import(api, None, "SHFE.a", None,
                                     SimpleNamespace(content_sha256="hash"), None, 15, temp_dir=tmp_path)
    assert result == ("ok", 1) and calls
    assert list(tmp_path.iterdir()) == []
    assert len(original) == 1


def test_wrapper_delegates_once_and_does_not_accept_legacy_summary():
    script = (ROOT / "scripts/run_option_daily_update.ps1").read_text(encoding="utf-8")
    assert "& $Python -B -u $Script" in script
    assert "exit $LASTEXITCODE" in script
    assert "Start-Process" not in script and "ConvertFrom-Json" not in script
    assert "Remove-Item" not in script


@pytest.mark.parametrize("args", [["--shard-count", "0"], ["--bars", "0"], ["--recycle-every", "0"], ["--worker"]])
def test_invalid_arguments_rejected_before_services(args):
    with pytest.raises(SystemExit) as error:
        updater.main(args)
    assert error.value.code == 2
