from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def load_installer(root, monkeypatch):
    source = Path(__file__).resolve().parents[1] / "scripts/install_infiny_observer.py"
    spec = importlib.util.spec_from_file_location("observer_install", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", root)
    template = root / "integrations/infiny/AIBridge.py"
    template.parent.mkdir(parents=True)
    actual = Path(__file__).resolve().parents[1] / "integrations/infiny/AIBridge.py"
    template.write_bytes(actual.read_bytes())
    return module


def test_observer_install_preserves_original_and_can_remove_exact_installed_file(tmp_path, monkeypatch):
    module = load_installer(tmp_path / "repo", monkeypatch)
    directory = tmp_path / "self_strategy"
    directory.mkdir()
    original = directory / "AIBridge.py"
    original.write_bytes(b"old-original")
    receipt = tmp_path / "receipt.json"
    result = module.deploy_observer(directory, receipt)
    target = directory / "AKObserverBridge.py"
    assert original.read_bytes() == b"old-original"
    assert "17778/analyze" in target.read_text(encoding="utf-8")
    assert result["execution_enabled"] is False
    assert target.read_text(encoding="utf-8").count("class AKObserverBridge") == 1
    removed = module.remove_observer(receipt, directory)
    assert removed["status"] == "removed"
    assert not target.exists()
    assert original.read_bytes() == b"old-original"


def test_existing_observer_or_modified_install_is_preserved(tmp_path, monkeypatch):
    module = load_installer(tmp_path / "repo", monkeypatch)
    directory = tmp_path / "self_strategy"
    directory.mkdir()
    receipt = tmp_path / "receipt.json"
    module.deploy_observer(directory, receipt)
    target = directory / "AKObserverBridge.py"
    target.write_bytes(b"user changed")
    with pytest.raises(ValueError):
        module.remove_observer(receipt, directory)
    with pytest.raises(ValueError):
        module.deploy_observer(directory, tmp_path / "new-receipt.json")
    assert target.read_bytes() == b"user changed"


def test_receipt_collision_preserves_other_writers_file(tmp_path, monkeypatch):
    module = load_installer(tmp_path / "repo", monkeypatch)
    directory = tmp_path / "self_strategy"
    directory.mkdir()
    receipt = tmp_path / "receipt.json"
    create = module._atomic_create
    def concurrent_write(path, payload):
        if path == receipt:
            receipt.write_bytes(b"other writer")
            raise FileExistsError("collision")
        return create(path, payload)
    monkeypatch.setattr(module, "_atomic_create", concurrent_write)
    with pytest.raises(FileExistsError):
        module.deploy_observer(directory, receipt)
    assert receipt.read_bytes() == b"other writer"
    assert not (directory / "AKObserverBridge.py").exists()


def test_receipt_cannot_choose_a_different_strategy_root(tmp_path, monkeypatch):
    module = load_installer(tmp_path / "repo", monkeypatch)
    directory, other = tmp_path / "first", tmp_path / "other"
    directory.mkdir()
    other.mkdir()
    receipt = tmp_path / "receipt.json"
    module.deploy_observer(directory, receipt)
    with pytest.raises(ValueError, match="bound"):
        module.remove_observer(receipt, other)
    assert (directory / "AKObserverBridge.py").exists()


def test_receipt_failure_rolls_back_only_the_new_target(tmp_path, monkeypatch):
    module = load_installer(tmp_path / "repo", monkeypatch)
    directory = tmp_path / "self_strategy"
    directory.mkdir()
    receipt = tmp_path / "receipt.json"
    create = module._atomic_create
    def failed_write(path, payload):
        if path == receipt:
            raise OSError("disk write failure")
        return create(path, payload)
    monkeypatch.setattr(module, "_atomic_create", failed_write)
    with pytest.raises(OSError):
        module.deploy_observer(directory, receipt)
    assert not (directory / "AKObserverBridge.py").exists()
    assert not receipt.exists()
