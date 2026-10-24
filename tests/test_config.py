from pathlib import Path

import pytest

from ak_trading.config import load_settings


def test_load_settings_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    env = tmp_path / ".env"
    env.write_text("TQ_USER=u1\nTQ_PASS=p1\nTQ_DEFAULT_SYMBOL=KQ.m@DCE.i\n", encoding="utf-8")
    monkeypatch.delenv("TQ_USER", raising=False)
    monkeypatch.delenv("TQ_PASS", raising=False)
    monkeypatch.delenv("TQ_DEFAULT_SYMBOL", raising=False)

    settings = load_settings(env)
    assert settings.tq_user == "u1"
    assert settings.tq_pass == "p1"
    assert settings.default_symbol == "KQ.m@DCE.i"
    assert "p1" not in repr(settings)


def test_load_settings_missing_creds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    env = tmp_path / ".env"
    env.write_text("TQ_USER=\nTQ_PASS=\n", encoding="utf-8")
    monkeypatch.delenv("TQ_USER", raising=False)
    monkeypatch.delenv("TQ_PASS", raising=False)
    with pytest.raises(RuntimeError, match="TQ_USER"):
        load_settings(env)


def test_default_symbol_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    env = tmp_path / ".env"
    env.write_text("TQ_USER=u\nTQ_PASS=p\nTQ_DEFAULT_SYMBOL=\n", encoding="utf-8")
    monkeypatch.delenv("TQ_DEFAULT_SYMBOL", raising=False)
    settings = load_settings(env)
    assert settings.default_symbol == "KQ.m@SHFE.rb"
