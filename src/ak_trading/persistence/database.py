from __future__ import annotations

from pathlib import Path
from typing import Any

from ak_trading.persistence.config import DatabaseSettings, redact_database_text

ROOT = Path(__file__).resolve().parents[3]


class DatabaseOperationError(RuntimeError):
    """A credential-safe public database error."""


def _dependency_error(exc: ImportError) -> DatabaseOperationError:
    return DatabaseOperationError(
        "PostgreSQL support is not installed; run pip install -e \".[postgres]\""
    )


def create_database_engine(settings: DatabaseSettings):
    try:
        from sqlalchemy import create_engine
    except ImportError as exc:
        raise _dependency_error(exc) from None
    return create_engine(
        settings.sqlalchemy_url,
        pool_pre_ping=True,
        pool_timeout=settings.connect_timeout,
        connect_args={
            "connect_timeout": settings.connect_timeout,
            "options": f"-c statement_timeout={settings.statement_timeout_ms}",
        },
    )


def upgrade_database(settings: DatabaseSettings, revision: str = "head") -> None:
    try:
        from alembic import command
        from alembic.config import Config
    except ImportError as exc:
        raise _dependency_error(exc) from None
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.sqlalchemy_url.replace("%", "%%"))
    try:
        command.upgrade(config, revision)
    except Exception as exc:
        message = redact_database_text(exc, settings)
        raise DatabaseOperationError(f"database migration failed: {message}") from None


def database_status(settings: DatabaseSettings) -> dict[str, Any]:
    try:
        from sqlalchemy import inspect, text
    except ImportError as exc:
        raise _dependency_error(exc) from None
    engine = create_database_engine(settings)
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT current_database(), current_user, "
                    "current_setting('server_version')"
                )
            ).one()
            revision = None
            if inspect(connection).has_table("alembic_version"):
                revision = connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one_or_none()
        return {
            "database": row[0],
            "user": row[1],
            "server_version": row[2],
            "alembic_revision": revision,
            "location": settings.safe_location,
        }
    except Exception as exc:
        message = redact_database_text(exc, settings)
        raise DatabaseOperationError(f"database status failed: {message}") from None
    finally:
        engine.dispose()
