from pathlib import Path
from datetime import date
import hashlib,json
from test_infiny_decision import make_history
from ak_trading.infiny_bridge.history import ReloadingDailyObserver


def test_history_pointer_loads_verified_snapshot_and_retains_it_on_bad_update(tmp_path):
    history = tmp_path / "bundles" / "first"
    make_history(history)
    pointer = tmp_path / "current.json"
    payload = {"schema_version": "infiny_history_pointer_v1", "bundle": str(history),
               "expected_history_date": "2026-09-08",
               "manifest_sha256": hashlib.sha256((history / "manifest.json").read_bytes()).hexdigest()}
    pointer.write_text(json.dumps(payload),encoding="utf-8")
    watcher = ReloadingDailyObserver(pointer,["SHFE.rb2610","SHFE.rb2610C3100"])
    assert watcher.status()["history_through"] == "2026-09-08"
    payload["manifest_sha256"] = "0"*64
    pointer.write_text(json.dumps(payload),encoding="utf-8")
    assert watcher.refresh() is False
    assert watcher.status()["history_through"] == "2026-09-08"
    assert watcher.status()["history_refresh_error"] == "ValueError"


def test_malformed_history_pointer_keeps_reload_loop_alive(tmp_path):
    history = tmp_path / "bundles" / "first"
    make_history(history)
    pointer = tmp_path / "current.json"
    pointer.write_text("[]",encoding="utf-8")
    watcher = ReloadingDailyObserver(pointer,["SHFE.rb2610C3100"])
    assert watcher.status()["history_refresh_error"] == "ValueError"
    payload = {"schema_version": "infiny_history_pointer_v1", "bundle": str(history),
               "expected_history_date": "2026-09-08",
               "manifest_sha256": hashlib.sha256((history / "manifest.json").read_bytes()).hexdigest()}
    pointer.write_text(json.dumps(payload),encoding="utf-8")
    assert watcher.refresh() is True
    assert watcher.status()["history_through"] == "2026-09-08"
    watcher.start()
    watcher.close()


def test_missing_history_pointer_does_not_block_market_observation(tmp_path):
    watcher=ReloadingDailyObserver(tmp_path/'missing.json',["SHFE.rb2610C3100"])
    assert watcher.evaluate({"exchange":"SHFE","instrument_id":"rb2610C3100"})["reason"] == "option_observation_only"
    assert watcher.status()["order_execution_enabled"] is False
