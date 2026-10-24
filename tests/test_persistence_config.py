from pathlib import Path
import os

import pytest

from ak_trading.persistence.config import (
    DatabaseConfigurationError,
    DatabaseSettings,
    load_database_settings,
    load_test_database_settings,
    redact_database_text,
)


def test_test_session_disables_project_application_database():
    assert os.environ["AK_DATABASE_URL"] == ""
    assert load_database_settings() is None


def test_database_settings_normalizes_driver_and_hides_credentials():
    settings = DatabaseSettings(
        "postgresql://research_user:p%40ss@127.0.0.1:55432/ak_research"
    )

    assert settings.sqlalchemy_url.startswith("postgresql+psycopg://")
    assert settings.safe_location == "127.0.0.1:55432/ak_research"
    assert settings.database_name == "ak_research"
    assert "p%40ss" not in repr(settings)
    assert "research_user" not in repr(settings)
    redacted = redact_database_text(
        "research_user failed with p@ss using " + settings.url, settings
    )
    assert "research_user" not in redacted
    assert "p@ss" not in redacted
    assert settings.url not in redacted


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///local.db",
        "postgresql:///missing-host",
        "postgresql://localhost:bad/missing_port",
        "postgresql://localhost/",
        "postgresql://localhost/database/extra",
    ],
)
def test_database_settings_rejects_unsafe_or_incomplete_urls(url: str):
    with pytest.raises(DatabaseConfigurationError):
        DatabaseSettings(url)


def test_optional_database_settings_preserves_file_only_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("AK_DATABASE_URL", raising=False)
    env_file = tmp_path / "empty.env"
    env_file.write_text("AK_DATABASE_URL=\n", encoding="utf-8")

    assert load_database_settings(env_file) is None


def test_test_database_requires_suffix_and_distinct_physical_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    env_file = tmp_path / "empty.env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setenv(
        "AK_DATABASE_URL", "postgresql://app:one@127.0.0.1:5432/ak_app"
    )
    monkeypatch.setenv(
        "AK_TEST_DATABASE_URL", "postgresql://test:two@127.0.0.1:5432/ak_app"
    )
    with pytest.raises(DatabaseConfigurationError, match="different database"):
        load_test_database_settings(env_file, required=True)

    monkeypatch.setenv(
        "AK_TEST_DATABASE_URL", "postgresql://test:two@127.0.0.1:5432/ak_isolated"
    )
    with pytest.raises(DatabaseConfigurationError, match="end with '_test'"):
        load_test_database_settings(env_file, required=True)

    monkeypatch.setenv(
        "AK_TEST_DATABASE_URL",
        "postgresql://test:two@127.0.0.1:5432/ak_isolated_test",
    )
    settings = load_test_database_settings(env_file, required=True)
    assert settings is not None
    assert settings.database_name == "ak_isolated_test"
