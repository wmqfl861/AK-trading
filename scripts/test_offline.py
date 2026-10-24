"""Run pytest offline: python scripts/test_offline.py [pytest arguments].

Test paths, -k, -m, --collect-only and reporting options are forwarded. Plugin,
configuration and response-file overrides are refused. --basetemp must name a
new directory; an explicit directory is retained for inspection after the run.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]


def prepare_arguments(arguments: list[str]) -> tuple[list[str], Path | None]:
    """Keep pytest selection flexible without exposing its bootstrap controls."""
    result: list[str] = []
    basetemp = None
    iterator = iter(arguments)
    protected = (
        "--override-ini", "--rootdir", "--confcutdir", "--noconftest",
        "--import-mode", "--pyargs", "--basetemp",
    )
    for argument in iterator:
        if argument == "--basetemp" or argument.startswith("--basetemp="):
            if basetemp is not None:
                raise ValueError("--basetemp may be supplied only once")
            value = argument.partition("=")[2] if "=" in argument else next(iterator, "")
            if not value or value.startswith("-"):
                raise ValueError("--basetemp requires a new directory")
            basetemp = Path(value).absolute()
            if basetemp.exists() or basetemp.is_symlink():
                raise ValueError("--basetemp must not already exist")
            continue
        option = argument.partition("=")[0]
        if (
            argument.startswith("@")
            or argument.startswith(("-p", "-o", "-c"))
            or (option.startswith("--") and option != "--co" and any(
                name.startswith(option) or option.startswith(name) for name in protected
            ))
        ):
            raise ValueError("plugin, configuration and response-file overrides are disabled")
        result.append(argument)
    return result, basetemp


def child_environment(parent: dict[str, str], bootstrap: Path) -> dict[str, str]:
    """Copy, never mutate or print, the caller's environment."""
    environment = {
        name: value for name, value in parent.items()
        if not name.upper().startswith((
            "AK_DATABASE", "AK_TEST_DATABASE", "TQ_", "PG", "PYTEST_", "FUTURES_TEST_",
        )) and name.upper() not in {
            "DATABASE_URL", "TEST_DATABASE_URL", "PYTHONPATH", "PYTHONHOME",
            "PYTHONSTARTUP", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
            "PYTHON_DOTENV_DISABLED",
        }
    }
    environment.update({
        "AK_DATABASE_URL": "", "AK_TEST_DATABASE_URL": "",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": os.pathsep.join((str(bootstrap), str(ROOT / "src"))),
        "NO_PROXY": "localhost,127.0.0.1,::1",
    })
    return environment


def main(arguments: list[str] | None = None) -> int:
    try:
        arguments, requested_temp = prepare_arguments(
            list(sys.argv[1:] if arguments is None else arguments)
        )
    except ValueError as exc:
        print(f"Offline launcher: {exc}", file=sys.stderr)
        return 4

    with TemporaryDirectory(prefix="ak-trading-offline-") as temporary:
        workspace = Path(temporary).resolve()
        empty_root = workspace / "empty-settings"
        empty_root.mkdir()
        bootstrap = workspace / "bootstrap"
        bootstrap.mkdir()
        basetemp = requested_temp or workspace / "pytest"
        if requested_temp is not None:
            # pytest deletes basetemp on first use. Reserve a NEW path here so
            # no caller-owned directory can ever be passed to that cleanup.
            try:
                requested_temp.mkdir(parents=True, exist_ok=False)
            except OSError:
                print("Offline launcher: cannot reserve a new --basetemp directory", file=sys.stderr)
                return 4
        startup = (
            "import os, sys\n"
            "try:\n"
            "    from ak_trading.testing import offline_guard\n"
            f"    offline_guard.install({str(empty_root)!r}, {str(ROOT)!r}, "
            f"{[str(workspace), str(basetemp)]!r})\n"
            "except BaseException:\n"
            "    sys.stderr.write('Offline guard startup failed; refusing to run.\\n')\n"
            "    os._exit(4)\n"
        )
        (bootstrap / "sitecustomize.py").write_text(startup, encoding="utf-8")
        code = (
            "from ak_trading.testing import offline_guard; "
            "offline_guard.require_installed(); "
            "import pytest, sys; "
            "raise SystemExit(pytest.main(sys.argv[1:], plugins=[offline_guard.OfflinePlugin()]))"
        )
        command = [
            sys.executable, "-B", "-c", code,
            "-c", str(ROOT / "pyproject.toml"), "-o", "addopts=",
            "-p", "no:cacheprovider", "--basetemp", str(basetemp), *arguments,
        ]
        # Deliberately omit argument values and environment from diagnostics.
        print(
            "Offline command: python scripts/test_offline.py "
            f"({len(arguments)} pytest arguments forwarded; guard before collection)",
            flush=True,
        )
        try:
            return subprocess.run(
                command, cwd=ROOT, env=child_environment(dict(os.environ), bootstrap),
                check=False,
            ).returncode
        except KeyboardInterrupt:
            return 130


if __name__ == "__main__":
    raise SystemExit(main())
