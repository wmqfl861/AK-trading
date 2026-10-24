from __future__ import annotations

import builtins
import importlib.util
from pathlib import Path
import sys
import types


def test_probe_loads_without_pythongo_graphics_or_numerical_stack(monkeypatch):
    native = types.ModuleType("INFINIGO")
    native.updateState = lambda strategy_id, data, /: None
    native.writeLog = lambda **kwargs: None
    monkeypatch.setitem(sys.modules, "INFINIGO", native)
    original = builtins.__import__
    def guarded(name, *args, **kwargs):
        if name.split(".")[0] in {"pythongo", "numpy", "pandas", "ctypes", "PyQt5", "pydantic", "qdarkstyle"} and name != "pythongo.core":
            raise ImportError("embedded optional dependency blocked")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded)
    path = Path(__file__).resolve().parents[1] / "integrations/infiny/HistoryDepthProbe.py"
    spec = importlib.util.spec_from_file_location("probe_load_test", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    probe = module.HistoryDepthProbe()
    probe.strategy_name = "probe-test"
    probe.on_init()
    assert probe.get_params()[0]["value"] == "SHFE"
    assert probe.trading is False
