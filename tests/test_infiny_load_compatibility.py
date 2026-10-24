from __future__ import annotations

import builtins
import importlib.util
from pathlib import Path
import sys
import types


def test_strategy_loads_without_numpy_ctypes_or_pythongo_graphics(monkeypatch):
    native = types.ModuleType("INFINIGO")
    state_calls = []
    def update_state(strategy_id, data, /):
        assert isinstance(strategy_id, int)
        assert isinstance(data, dict)
        state_calls.append((strategy_id, data))
    native.updateState = update_state
    native.updateParam = lambda **kwargs: None
    native.writeLog = lambda **kwargs: None
    monkeypatch.setitem(sys.modules, "INFINIGO", native)
    original = builtins.__import__
    def guarded(name, *args, **kwargs):
        if name.split(".")[0] in {"pythongo", "numpy", "pandas", "ctypes", "PyQt5", "pydantic"}:
            raise ImportError("embedded numerical/GUI dependencies unavailable")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded)
    path = Path(__file__).resolve().parents[1] / "integrations/infiny/AIBridge.py"
    spec = importlib.util.spec_from_file_location("loadable_observer_test", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    bridge = module.AIBridge()
    bridge.strategy_id = 23
    bridge.on_init()
    bridge.set_params({"instrument_id": "rb2610"})
    assert state_calls and all(call[0] == 23 for call in state_calls)
    assert bridge.params_map.enable_trading is False
    assert bridge.trading is False
    assert {p["name"] for p in bridge.get_params()} >= {"exchange", "instrument_id", "api_url"}
    assert module.AKObserverBridge().send_order() is None
