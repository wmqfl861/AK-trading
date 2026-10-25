"""Checks for the dedicated mcp_readonly role setup (P1-6 repair).

Three layers:

1. Static structure checks on scripts/sql/create_mcp_readonly_role.sql.
   Offline, no database. They guard the original defect (psql variables are
   never interpolated inside dollar-quoted DO bodies) plus the least-privilege
   allowlist and the no-argv secret path.
2. Behaviour checks for scripts/setup_mcp_readonly.py with getpass and
   subprocess stubbed out. Offline, nothing is spawned.
3. End-to-end assertions against a disposable, task-local PostgreSQL cluster
   with a SYNTHETIC secret. Enabled only when both AK_MCP_READONLY_E2E_DSN
   (superuser dsn) and AK_MCP_READONLY_E2E_READ_DSN (mcp_readonly dsn) are
   set; skipped otherwise. Never point them at a real database.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SQL_PATH = ROOT / "scripts" / "sql" / "create_mcp_readonly_role.sql"
HELPER_PATH = ROOT / "scripts" / "setup_mcp_readonly.py"
PASSWORD_ENV = "AK_MCP_ROLE_PASSWORD"
E2E_DSN_ENV = "AK_MCP_READONLY_E2E_DSN"
E2E_READ_DSN_ENV = "AK_MCP_READONLY_E2E_READ_DSN"
ROLE = "mcp_readonly"

ALLOWED_TABLES = (
    "market_bars",
    "member_position_ranks",
    "reference_closed_dates",
    "reference_products",
)


def sql_text() -> str:
    return SQL_PATH.read_text(encoding="utf-8")


def dollar_quoted_spans(text: str) -> list[str]:
    """Bodies between plain $$ markers (the script only uses untagged $$)."""
    parts = text.split("$$")
    return [parts[index] for index in range(1, len(parts), 2)]


# --- 1. static structure of the SQL script ---------------------------------


def test_no_psql_variable_interpolation_inside_dollar_quotes() -> None:
    spans = dollar_quoted_spans(sql_text())
    assert spans, "expected at least one dollar-quoted block"
    for span in spans:
        assert ":'" not in span, "psql :'var' inside a dollar-quoted body is never interpolated"
        assert ':"' not in span


def test_password_literal_bound_exactly_once_and_outside_blocks() -> None:
    text = sql_text()
    code = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("--")
    )
    assert code.count(":'role_password'") == 1
    outside = "".join(code.split("$$")[0::2])
    assert ":'role_password'" in outside
    assert "set_config('ak.role_password', :'role_password', false)" in outside


def test_password_comes_from_environment_not_argv() -> None:
    assert f"\\getenv role_password {PASSWORD_ENV}" in sql_text()


def test_on_error_stop_and_single_transaction() -> None:
    text = sql_text()
    assert "\\set ON_ERROR_STOP on" in text
    assert "\nBEGIN;" in text
    assert "\nCOMMIT;" in text


def test_empty_password_is_refused_in_block() -> None:
    assert "RAISE EXCEPTION" in sql_text()
    assert "AK_MCP_ROLE_PASSWORD is not set" in sql_text()


def test_local_secret_copies_are_scrubbed() -> None:
    text = sql_text()
    assert "\\unset role_password" in text
    assert "\\unset secret_bound" in text
    assert "\\unset secret_cleared" in text
    assert "set_config('ak.role_password', '', false)" in text


def test_table_allowlist_and_snapshot_exclusion() -> None:
    text = sql_text()
    for table in ALLOWED_TABLES:
        assert f"GRANT SELECT ON public.{table} TO {ROLE};" in text
    grant_lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip().upper().startswith("GRANT")
    ]
    assert grant_lines, "expected GRANT statements"
    assert len([line for line in grant_lines if line.startswith("GRANT SELECT")]) == len(
        ALLOWED_TABLES
    )
    for line in grant_lines:
        assert "reference_snapshots" not in line


# --- 2. helper behaviour with stubbed getpass/subprocess --------------------


def load_helper():
    spec = importlib.util.spec_from_file_location("setup_mcp_readonly", HELPER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeResult:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


def stub_helper(monkeypatch, password_inputs, returncode=0):
    helper = load_helper()
    recorded: dict[str, object] = {}

    def fake_getpass(*_args, **_kwargs) -> str:
        if not password_inputs:
            raise AssertionError("unexpected extra getpass prompt")
        return password_inputs.pop(0)

    def fake_run(argv, env, check):
        recorded["argv"] = list(argv)
        recorded["env"] = dict(env)
        recorded["check"] = check
        return FakeResult(returncode)

    monkeypatch.setattr(helper.getpass, "getpass", fake_getpass)
    monkeypatch.setattr(helper.subprocess, "run", fake_run)
    monkeypatch.delenv(PASSWORD_ENV, raising=False)
    return helper, recorded


def test_helper_sends_secret_via_child_env_never_argv(monkeypatch) -> None:
    secret = "synthetic-'\"quotes\"; --$$inject"
    helper, recorded = stub_helper(monkeypatch, [secret, secret])
    assert helper.main(["--sql", str(SQL_PATH)]) == 0
    argv = recorded["argv"]
    assert argv[0] == "psql"
    assert "-X" in argv
    assert argv[-1] == str(SQL_PATH)
    for part in argv:
        assert secret not in part, "secret must never appear in child argv"
    assert recorded["env"][PASSWORD_ENV] == secret
    assert PASSWORD_ENV not in os.environ, "parent environment must stay clean"


def test_helper_refuses_empty_password_without_spawning(monkeypatch) -> None:
    helper, recorded = stub_helper(monkeypatch, [""])
    with pytest.raises(SystemExit, match="empty password"):
        helper.main(["--sql", str(SQL_PATH)])
    assert recorded == {}


def test_helper_refuses_password_mismatch(monkeypatch) -> None:
    helper, _ = stub_helper(monkeypatch, ["synthetic-a", "synthetic-b"])
    with pytest.raises(SystemExit, match="do not match"):
        helper.main(["--sql", str(SQL_PATH)])


def test_helper_missing_sql_file_is_a_clear_error(monkeypatch, tmp_path) -> None:
    helper, recorded = stub_helper(monkeypatch, ["synthetic-x", "synthetic-x"])
    missing = tmp_path / "does-not-exist.sql"
    with pytest.raises(SystemExit, match="SQL script not found"):
        helper.main(["--sql", str(missing)])
    assert recorded == {}


def test_helper_propagates_psql_exit_code(monkeypatch) -> None:
    secret = "synthetic-plain"
    helper, _ = stub_helper(monkeypatch, [secret, secret], returncode=3)
    assert helper.main(["--sql", str(SQL_PATH)]) == 3


# --- 3. disposable-cluster end-to-end (opt-in; SYNTHETIC secret only) -------


def _e2e_dsns() -> tuple[str, str]:
    return os.environ.get(E2E_DSN_ENV, ""), os.environ.get(E2E_READ_DSN_ENV, "")


def _psql(dsn: str, *args: str, password: str | None = None):
    env = dict(os.environ)
    env.pop(PASSWORD_ENV, None)
    if password is not None:
        env[PASSWORD_ENV] = password
    completed = subprocess.run(
        ["psql", "-X", dsn, *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed


def _psql_ok(dsn: str, *args: str, password: str | None = None):
    completed = _psql(dsn, *args, password=password)
    assert completed.returncode == 0, f"psql {args[:2]} rc={completed.returncode}: {completed.stderr}"
    return completed


def _e2e_schema(dsn: str) -> None:
    _psql_ok(
        dsn,
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        "CREATE TABLE IF NOT EXISTS public.market_bars (id int); "
        "CREATE TABLE IF NOT EXISTS public.member_position_ranks (id int); "
        "CREATE TABLE IF NOT EXISTS public.reference_closed_dates (id int); "
        "CREATE TABLE IF NOT EXISTS public.reference_products (id int); "
        "CREATE TABLE IF NOT EXISTS public.reference_snapshots (id int, raw_json text); "
        "TRUNCATE public.market_bars; INSERT INTO public.market_bars VALUES (1); "
        "TRUNCATE public.reference_snapshots; INSERT INTO public.reference_snapshots "
        "VALUES (1, 'SYNTHETIC');",
    )


def _e2e_drop_role(dsn: str) -> None:
    _psql_ok(
        dsn,
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        f"DO $do$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}') THEN "
        f"EXECUTE 'DROP OWNED BY {ROLE}; DROP ROLE {ROLE};'; END IF; END $do$;",
    )


def _role_exists(dsn: str) -> bool:
    completed = _psql_ok(dsn, "-At", "-c", f"SELECT count(*) FROM pg_roles WHERE rolname = '{ROLE}';")
    return completed.stdout.strip() == "1"


@pytest.mark.skipif(
    not all(_e2e_dsns()),
    reason="AK_MCP_READONLY_E2E_DSN / AK_MCP_READONLY_E2E_READ_DSN unset: "
    "run only against a disposable task-local cluster with a SYNTHETIC secret",
)
def test_e2e_create_existing_repeated_and_least_privilege() -> None:
    dsn, read_dsn = _e2e_dsns()
    secret = "synthetic-'\"quotes\"; --$$inject"

    _e2e_drop_role(dsn)
    _e2e_schema(dsn)

    # New role and repeated (existing-role) invocation both succeed.
    for _ in range(2):
        completed = _psql(dsn, "-f", str(SQL_PATH), password=secret)
        assert completed.returncode == 0, completed.stderr

    # Exactly one matching role: hostile password characters injected nothing.
    count = _psql_ok(
        dsn, "-At", "-c", "SELECT count(*) FROM pg_roles WHERE rolname LIKE 'mcp%';"
    ).stdout.strip()
    assert count == "1"
    login = _psql_ok(
        dsn, "-At", "-c", f"SELECT rolcanlogin FROM pg_roles WHERE rolname = '{ROLE}';"
    ).stdout.strip()
    assert login == "t"

    # Allowed reads work through the read-only identity.
    for table in ALLOWED_TABLES:
        _psql_ok(read_dsn, "-At", "-c", f"SELECT count(*) FROM public.{table};")

    # Least privilege: writes, new objects and the snapshot table are denied.
    denials = [
        "-c", "INSERT INTO public.market_bars VALUES (2);",
        "-c", "UPDATE public.market_bars SET id = 2;",
        "-c", "DELETE FROM public.market_bars;",
        "-c", "CREATE TABLE public.should_not_exist (id int);",
        "-c", "SELECT raw_json FROM public.reference_snapshots;",
    ]
    for index in range(0, len(denials), 2):
        completed = _psql(read_dsn, denials[index], denials[index + 1])
        assert completed.returncode != 0, f"unexpectedly allowed: {denials[index + 1]}"
        assert "permission denied" in completed.stderr, completed.stderr
    leftover = _psql_ok(
        dsn, "-At", "-c", "SELECT count(*) FROM public.market_bars;"
    ).stdout.strip()
    assert leftover == "1"

    # A table created after the grants must not leak to the role either.
    _psql_ok(dsn, "-c", "CREATE TABLE public.created_after_grants (id int);")
    completed = _psql(read_dsn, "-At", "-c", "SELECT count(*) FROM public.created_after_grants;")
    assert completed.returncode != 0
    assert "permission denied" in completed.stderr


@pytest.mark.skipif(not all(_e2e_dsns()), reason="disposable cluster not provided")
def test_e2e_missing_or_empty_password_is_rejected_and_changes_nothing() -> None:
    dsn, _ = _e2e_dsns()
    _e2e_drop_role(dsn)
    # Missing environment variable: refuse without creating the role,
    # whatever psql \getenv does on an unset variable (error or empty).
    missing = _psql(dsn, "-f", str(SQL_PATH), password=None)
    assert missing.returncode != 0, "unset AK_MCP_ROLE_PASSWORD must not succeed"
    assert "permission denied" not in missing.stderr, missing.stderr
    assert not _role_exists(dsn)
    # Set but empty: explicit refusal message from the DO block.
    empty = _psql(dsn, "-f", str(SQL_PATH), password="")
    assert empty.returncode != 0
    assert "AK_MCP_ROLE_PASSWORD is not set" in empty.stderr, empty.stderr
    assert not _role_exists(dsn)


@pytest.mark.skipif(not all(_e2e_dsns()), reason="disposable cluster not provided")
def test_e2e_forced_error_rolls_the_whole_script_back() -> None:
    dsn, _ = _e2e_dsns()
    _e2e_drop_role(dsn)
    original = SQL_PATH.read_text(encoding="utf-8")
    sabotaged = original.replace(
        "COMMIT;",
        "GRANT SELECT ON public.no_such_table TO mcp_readonly;\nCOMMIT;",
    )
    assert sabotaged != original
    forced = Path(SQL_PATH.parent / "create_mcp_readonly_role_forced_error_test.sql")
    forced.write_text(sabotaged, encoding="utf-8")
    try:
        completed = _psql(dsn, "-f", str(forced), password="synthetic-ok")
        assert completed.returncode != 0
        # The CREATE ROLE earlier in the same transaction must be rolled back.
        assert not _role_exists(dsn)
    finally:
        forced.unlink(missing_ok=True)
