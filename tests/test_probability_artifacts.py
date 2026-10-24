from pathlib import Path

import pytest

from ak_trading.probability.artifacts import safe_path


@pytest.mark.parametrize("value", ["//server/share/input", "//?/C:/file", "C:/data/a.json:stream", "COM¹.json", "NUL .json", ".env", "config/secrets.env", "a\x00b"])
def test_research_rejects_unsafe_or_credential_paths_before_resolving(value, monkeypatch):
    def resolve_forbidden(*args, **kwargs):
        pytest.fail("unsafe path reached filesystem resolution")
    monkeypatch.setattr(Path, "resolve", resolve_forbidden)
    with pytest.raises(ValueError):
        safe_path(Path(value))


def test_resolved_credential_alias_is_rejected_before_read(monkeypatch):
    monkeypatch.setattr(Path, "is_symlink", lambda self: False)
    if hasattr(Path, "is_junction"):
        monkeypatch.setattr(Path, "is_junction", lambda self: False)
    monkeypatch.setattr(Path, "resolve", lambda self: Path("C:/fixture/.env"))
    with pytest.raises(ValueError):
        safe_path(Path("C:/fixture/contracts.json"))


def test_overflowing_json_literal_is_not_finite_metadata():
    from ak_trading.probability.artifacts import strict_json
    with pytest.raises(ValueError):
        strict_json(b'{"x":1e400}')
