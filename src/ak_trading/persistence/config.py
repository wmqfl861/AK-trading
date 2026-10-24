from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[3]
_POSTGRES_SCHEMES = frozenset({"postgres", "postgresql", "postgresql+psycopg"})


class DatabaseConfigurationError(RuntimeError):
    """Raised for missing or unsafe database configuration."""


@dataclass(frozen=True)
class DatabaseSettings:
    url: str = field(repr=False)
    connect_timeout: int = 5
    statement_timeout_ms: int = 5000

    def __post_init__(self) -> None:
        try:
            parsed = urlsplit(self.url)
        except ValueError as exc:
            raise DatabaseConfigurationError("AK_DATABASE_URL is not a valid URL") from exc
        if parsed.scheme.lower() not in _POSTGRES_SCHEMES:
            raise DatabaseConfigurationError("AK_DATABASE_URL must use PostgreSQL")
        if not parsed.hostname or not parsed.path.strip("/"):
            raise DatabaseConfigurationError(
                "AK_DATABASE_URL must include a host and database name"
            )
        if "/" in unquote(parsed.path.strip("/")):
            raise DatabaseConfigurationError(
                "AK_DATABASE_URL must name exactly one database"
            )
        try:
            parsed.port
        except ValueError as exc:
            raise DatabaseConfigurationError(
                "AK_DATABASE_URL contains an invalid port"
            ) from exc
        if self.connect_timeout <= 0:
            raise DatabaseConfigurationError(
                "AK_DATABASE_CONNECT_TIMEOUT must be a positive integer"
            )
        if self.statement_timeout_ms <= 0:
            raise DatabaseConfigurationError(
                "AK_DATABASE_STATEMENT_TIMEOUT_MS must be a positive integer"
            )

    @property
    def sqlalchemy_url(self) -> str:
        if self.url.startswith("postgresql+psycopg://"):
            return self.url
        if self.url.startswith("postgresql://"):
            return "postgresql+psycopg://" + self.url[len("postgresql://") :]
        if self.url.startswith("postgres://"):
            return "postgresql+psycopg://" + self.url[len("postgres://") :]
        return self.url

    @property
    def database_name(self) -> str:
        return unquote(urlsplit(self.url).path.strip("/"))

    @property
    def safe_location(self) -> str:
        parsed = urlsplit(self.url)
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        return f"{host}{port}/{self.database_name}"

    @property
    def secrets(self) -> tuple[str, ...]:
        values = [self.url, self.sqlalchemy_url]
        parsed = urlsplit(self.url)
        if parsed.username:
            values.extend((parsed.username, unquote(parsed.username)))
        if parsed.password:
            values.extend((parsed.password, unquote(parsed.password)))
        return tuple(dict.fromkeys(value for value in values if value))


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise DatabaseConfigurationError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise DatabaseConfigurationError(f"{name} must be a positive integer")
    return value


def load_database_settings(
    env_file: Path | None = None, *, required: bool = False, variable: str = "AK_DATABASE_URL"
) -> DatabaseSettings | None:
    load_dotenv(env_file or ROOT / ".env")
    url = os.getenv(variable, "").strip()
    if not url:
        if required:
            raise DatabaseConfigurationError(f"{variable} is required")
        return None
    return DatabaseSettings(
        url=url,
        connect_timeout=_positive_int("AK_DATABASE_CONNECT_TIMEOUT", 5),
        statement_timeout_ms=_positive_int(
            "AK_DATABASE_STATEMENT_TIMEOUT_MS", 5000
        ),
    )


def load_test_database_settings(
    env_file: Path | None = None, *, required: bool = False
) -> DatabaseSettings | None:
    settings = load_database_settings(
        env_file, required=required, variable="AK_TEST_DATABASE_URL"
    )
    if settings is None:
        return None
    application_url = os.getenv("AK_DATABASE_URL", "").strip()
    if application_url:
        application = DatabaseSettings(url=application_url)
        test_identity = (
            urlsplit(settings.url).hostname,
            urlsplit(settings.url).port or 5432,
            settings.database_name,
        )
        application_identity = (
            urlsplit(application.url).hostname,
            urlsplit(application.url).port or 5432,
            application.database_name,
        )
        if test_identity == application_identity:
            raise DatabaseConfigurationError(
                "AK_TEST_DATABASE_URL must use a different database than AK_DATABASE_URL"
            )
    if not settings.database_name.lower().endswith("_test"):
        raise DatabaseConfigurationError(
            "AK_TEST_DATABASE_URL database name must end with '_test'"
        )
    return settings


def redact_database_text(value: object, settings: DatabaseSettings | None) -> str:
    text = str(value)
    if settings is None:
        return text
    for secret in sorted(settings.secrets, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    return text
