r"""Create or update the mcp_readonly database role with hidden password input.

The password is read with getpass (hidden input) and handed to psql only
through the child process environment (AK_MCP_ROLE_PASSWORD), so it never
appears in argv, process command lines, shell history or this script's
output. The SQL side (scripts/sql/create_mcp_readonly_role.sql) reads that
variable with psql's \getenv and interpolates it exactly once, outside any
dollar-quoted block.

Requires psql >= 15 on PATH (or pass --psql) and a superuser/createrole
connection. Read docs/mcp-readonly-role.md before touching production.
"""
from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SQL = ROOT / "scripts" / "sql" / "create_mcp_readonly_role.sql"
DEFAULT_DATABASE = "postgresql://postgres@127.0.0.1:5432/ak_trading"
PASSWORD_ENV = "AK_MCP_ROLE_PASSWORD"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create/update the mcp_readonly role (password via hidden input).",
    )
    parser.add_argument(
        "--database",
        default=DEFAULT_DATABASE,
        help="superuser/createrole connection string passed to psql (default: %(default)s)",
    )
    parser.add_argument(
        "--psql", default="psql", help="psql executable (default: %(default)s)"
    )
    parser.add_argument(
        "--sql", default=str(DEFAULT_SQL), help="role SQL script (default: %(default)s)"
    )
    return parser.parse_args(argv)


def read_password() -> str:
    password = getpass.getpass("Password for mcp_readonly (input hidden): ")
    if not password:
        raise SystemExit(f"{Path(sys.argv[0]).name}: empty password refused")
    confirmed = getpass.getpass("Confirm password: ")
    if password != confirmed:
        raise SystemExit(f"{Path(sys.argv[0]).name}: passwords do not match")
    return password


def run_psql(psql: str, database: str, sql: Path, password: str) -> int:
    if not sql.is_file():
        raise SystemExit(f"{Path(sys.argv[0]).name}: SQL script not found: {sql}")
    # -X skips ~/.psqlrc so user startup files can never echo the variable.
    # The secret exists only in the child's environment, never in argv.
    environment = dict(os.environ)
    environment[PASSWORD_ENV] = password
    completed = subprocess.run(
        [psql, "-X", database, "-f", str(sql)],
        env=environment,
        check=False,
    )
    return completed.returncode


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    password = read_password()
    return run_psql(args.psql, args.database, Path(args.sql), password)


if __name__ == "__main__":
    raise SystemExit(main())
