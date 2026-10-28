"""Regression coverage for the stable offline entry point; no real I/O probes."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "test_offline.py"
# Keep the regression oracle independent of the production constant.
DANGEROUS_NODES = (
    "tests/test_member_rank.py::TestMemberRankImport::test_import_is_idempotent_and_upserts",
    "tests/test_catalog_collection.py::test_catalog_collection_keeps_other_products_after_one_search_failure",
    "tests/test_catalog_collection.py::test_legacy_three_product_collection_consumes_isolated_search_errors",
)


def _launcher():
    spec = importlib.util.spec_from_file_location("offline_launcher", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(*arguments, env=None, cwd=None):
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPT), *map(str, arguments)],
        cwd=cwd or ROOT, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=45, check=False,
    )


def _probe(tmp_path, content, *arguments, env=None):
    test_file = tmp_path / "test_probe.py"
    test_file.write_text(textwrap.dedent(content), encoding="utf-8")
    return _run(test_file, "-q", *arguments, env=env, cwd=tmp_path)


def test_parent_environment_unchanged_and_bootstrap_variables_not_inherited(tmp_path):
    parent = {
        "PATH": "keep", "AK_DATABASE_URL": "app-secret", "AK_TEST_DATABASE_URL": "test-secret",
        "TQ_USER": "user-secret", "TQ_PASS": "pass-secret", "TQ_DEFAULT_SYMBOL": "secret",
        "PGPASSWORD": "pg-secret", "PGSERVICEFILE": "real-config",
        "PYTEST_ADDOPTS": "-m postgres", "PYTEST_PLUGINS": "rogue",
        "PYTHONPATH": "rogue", "FUTURES_TEST_EMPTY_ROOT": "real-root",
        "HTTP_PROXY": "proxy-secret", "PYTHON_DOTENV_DISABLED": "1",
    }
    before = parent.copy()
    child = _launcher().child_environment(parent, tmp_path)
    assert parent == before
    assert child["PATH"] == "keep"
    assert child["AK_DATABASE_URL"] == child["AK_TEST_DATABASE_URL"] == ""
    assert not any(name in child for name in (
        "TQ_USER", "TQ_PASS", "TQ_DEFAULT_SYMBOL", "PGPASSWORD", "PGSERVICEFILE",
        "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "FUTURES_TEST_EMPTY_ROOT", "HTTP_PROXY",
        "PYTHON_DOTENV_DISABLED",
    ))
    assert child["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert child["PYTHONPATH"].split(os.pathsep) == [str(tmp_path), str(ROOT / "src")]


@pytest.mark.parametrize("arguments", [
    ["-p", "no:ak_trading.testing.offline_guard"], ["-pno:offline"],
    ["-o", "addopts=-m postgres"], ["--override-ini=addopts=-m postgres"],
    ["-c", "rogue.ini"], ["--rootdir=/"], ["--confcutdir=/"],
    ["--noconftest"], ["--import-mode=importlib"], ["--pyargs"],
    ["@selections.txt"], ["--over=addopts=-m postgres"], ["--base=/"],
])
def test_cannot_override_guard_or_selection_bootstrap(arguments):
    with pytest.raises(ValueError, match="overrides are disabled"):
        _launcher().prepare_arguments(arguments)


def test_basetemp_refuses_preexisting_path_without_deleting_it(tmp_path):
    sentinel = tmp_path / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    result = _run("--basetemp", tmp_path, "--collect-only")
    assert result.returncode == 4
    assert "must not already exist" in result.stderr
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_nine_historical_exclusions_survive_extra_marker_and_deselect_options():
    paths = [str(ROOT / node) for node in DANGEROUS_NODES]
    result = _run(
        ROOT / "tests/test_postgres_persistence.py", *paths,
        "--collect-only", "-q", "-m", "postgres or not postgres", "--deselect=unrelated",
    )
    assert result.returncode == 5, result.stdout + result.stderr
    assert "0 selected, 9 deselected" in result.stdout
    assert "no tests collected (9 deselected)" in result.stdout


def test_parameterized_explicit_nodes_remain_excluded_with_alternate_nodeids():
    from types import SimpleNamespace
    from ak_trading.testing import offline_guard

    observed = []
    items = [
        SimpleNamespace(
            path=ROOT / node.split("::", 1)[0],
            nodeid="other-root/" + node + "[variant]",
            get_closest_marker=lambda name: None,
        ) for node in DANGEROUS_NODES
    ]
    # The plugin is normally installed only in the child. Supply the repository
    # identity for this pure selection check if ordinary pytest collected us.
    previous = offline_guard._repo_root
    offline_guard._repo_root = ROOT
    try:
        offline_guard.OfflinePlugin().pytest_collection_modifyitems(
            SimpleNamespace(hook=SimpleNamespace(pytest_deselected=lambda items: observed.extend(items))),
            items,
        )
    finally:
        offline_guard._repo_root = previous
    assert items == [] and len(observed) == 3


def test_guard_precedes_collection_clears_credentials_and_allows_fake_env(tmp_path):
    environment = dict(os.environ)
    environment.update({
        "AK_DATABASE_URL": "parent-app-secret", "AK_TEST_DATABASE_URL": "parent-test-secret",
        "TQ_USER": "parent-user-secret", "TQ_PASS": "parent-pass-secret",
        "PYTEST_ADDOPTS": "-p nonexistent_guard_bypass", "PYTEST_PLUGINS": "nonexistent_plugin",
        "FUTURES_TEST_EMPTY_ROOT": str(ROOT),
    })
    result = _probe(tmp_path, '''
        import os
        from pathlib import Path
        import pytest
        import dotenv
        import ak_trading.config as app
        import ak_trading.persistence.config as db
        from ak_trading.testing.offline_guard import OfflineViolation

        # Module-level checks run before session autouse fixtures.
        assert app.ROOT == db.ROOT
        assert list(app.ROOT.iterdir()) == []
        assert db.load_database_settings() is None
        assert db.load_test_database_settings() is None
        assert not os.environ.get("TQ_USER") and not os.environ.get("TQ_PASS")
        assert not os.environ.get("AK_DATABASE_URL")
        assert not os.environ.get("AK_TEST_DATABASE_URL")
        assert dotenv.load_dotenv() is False
        assert dotenv.dotenv_values() == {}
        real_env = Path(app.__file__).resolve().parents[2] / ".env"
        for attempt in (lambda: dotenv.load_dotenv(real_env),
                        lambda: dotenv.dotenv_values(real_env),
                        lambda: real_env.read_text()):
            with pytest.raises(OfflineViolation):
                attempt()

        def test_fake_fixture(tmp_path, monkeypatch):
            fake = tmp_path / "fake.env"
            fake.write_text("TQ_USER=fake-user\\nTQ_PASS=fake-pass\\n"
                            "AK_DATABASE_URL=postgresql://fake:fake@localhost/fake_test\\n")
            for name in ("TQ_USER", "TQ_PASS", "AK_DATABASE_URL"):
                monkeypatch.delenv(name, raising=False)
            assert app.load_settings(fake).tq_user == "fake-user"
            assert db.load_database_settings(fake).database_name == "fake_test"
            assert dotenv.dotenv_values(fake)["TQ_PASS"] == "fake-pass"
    ''', env=environment)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert not any(secret in result.stdout + result.stderr for secret in (
        "parent-app-secret", "parent-test-secret", "parent-user-secret", "parent-pass-secret",
    ))


def test_network_and_native_database_calls_block_but_http_and_sqlite_work(tmp_path):
    result = _probe(tmp_path, '''
        import asyncio
        import socket
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import pytest
        from ak_trading.testing.offline_guard import OfflineViolation

        def test_connections():
            # Documentation-only addresses; guards must raise before any OS I/O.
            with pytest.raises(OfflineViolation):
                socket.getaddrinfo("offline-probe.invalid", 443)
            for address in (("203.0.113.1", 443), ("127.0.0.1", 5432),
                            ("127.0.0.1", 65432), ("0.0.0.0", 5432)):
                for method in ("connect", "connect_ex"):
                    with socket.socket() as connection:
                        with pytest.raises(OfflineViolation):
                            getattr(connection, method)(address)
                with pytest.raises(OfflineViolation):
                    socket.create_connection(address, timeout=0.1)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
                with pytest.raises(OfflineViolation):
                    connection.sendto(b"blocked", ("203.0.113.1", 53))
            if hasattr(socket, "AF_UNIX"):
                with socket.socket(socket.AF_UNIX) as connection:
                    with pytest.raises(OfflineViolation):
                        connection.connect("/never-a-database-socket")

            import psycopg
            for connect in (psycopg.connect, psycopg.Connection.connect,
                            psycopg.pq.PGconn.connect, psycopg.pq.PGconn.connect_start):
                with pytest.raises(OfflineViolation):
                    connect("host=127.0.0.1 port=65432 dbname=fake_test connect_timeout=1")
            with pytest.raises(OfflineViolation):
                asyncio.run(psycopg.AsyncConnection.connect("host=127.0.0.1 dbname=fake_test"))
            from sqlalchemy import create_engine, text
            engine = create_engine("sqlite:///:memory:")
            with engine.connect() as connection:
                assert connection.execute(text("select 7")).scalar() == 7
            engine.dispose()

            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"fixture-ok")
                def log_message(self, *args):
                    pass
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                from urllib.request import urlopen
                with urlopen("http://localhost:%s/" % server.server_port, timeout=2) as response:
                    assert response.read() == b"fixture-ok"
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
    ''')
    assert result.returncode == 0, result.stdout + result.stderr


def test_python_descendants_inherit_guard_with_minimal_explicit_environment(tmp_path):
    result = _probe(tmp_path, '''
        import os
        import subprocess
        import sys
        import pytest
        from ak_trading.testing.offline_guard import OfflineViolation

        def test_child():
            environment = {name: os.environ[name] for name in ("PATH", "SYSTEMROOT", "WINDIR")
                           if name in os.environ}
            environment["TQ_PASS"] = "synthetic-canary"
            code = """
        import os, sys, socket
        assert not any(name in sys.modules for name in ('dotenv', 'psycopg', 'sqlalchemy'))
        import ak_trading.persistence.config as db
        assert db.load_database_settings() is None
        assert db.load_test_database_settings() is None
        assert os.environ['TQ_PASS'] == 'synthetic-canary'
        from ak_trading.testing.offline_guard import OfflineViolation
        try:
            socket.create_connection(('127.0.0.1', 5432), timeout=0.1)
        except OfflineViolation:
            print('guarded-child')
        else:
            raise AssertionError('unguarded child')
        """
            child = subprocess.run([sys.executable, "-c", code], env=environment,
                                   capture_output=True, text=True, timeout=10)
            assert child.returncode == 0, child.stderr
            assert child.stdout.strip() == "guarded-child"
            assert "PYTHONPATH" not in environment
            for option in ("-S", "-I", "-E", "-BS"):
                with pytest.raises(OfflineViolation):
                    subprocess.run([sys.executable, option, "-c", "pass"], timeout=5)
    ''')
    assert result.returncode == 0, result.stdout + result.stderr


def test_native_fixture_registration_is_scoped_and_rejects_changed_scripts(tmp_path, monkeypatch):
    from ak_trading.testing import offline_guard

    def forbidden(*args, **kwargs):
        raise AssertionError("pure registration rules must not launch any subprocess")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    script = tmp_path / "fixture.ps1"
    script.write_text("exit 0\n", encoding="utf-8")
    executable = tmp_path / "powershell.exe"
    arguments = [str(executable), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-File", str(script), "-Python", sys.executable, "-LogDir", str(tmp_path / "logs")]
    # Pure predicate context: this file runs both under plain pytest (guard not
    # installed, no fixture roots) and the canonical launcher (installed, with
    # basetemp roots). Scope the allowed fixture roots to exactly this test's
    # tmp_path and restore the previous state on teardown, so both entries
    # judge the same registration rules instead of depending on the entry.
    monkeypatch.setattr(offline_guard, "_fixture_roots", (tmp_path.resolve(),))
    assert not offline_guard._registered_powershell(executable, arguments)
    with offline_guard.allow_powershell_fixture(script, str(executable)):
        assert offline_guard._registered_powershell(executable, arguments)
        # Un-installed judgment context: a correctly registered fixture is
        # still refused while no root allows its script/log locations.
        monkeypatch.setattr(offline_guard, "_fixture_roots", ())
        assert not offline_guard._registered_powershell(executable, arguments)
        monkeypatch.setattr(offline_guard, "_fixture_roots", (tmp_path.resolve(),))
        assert offline_guard._registered_powershell(executable, arguments)
        assert not offline_guard._registered_powershell(executable, [*arguments, "-Command", "anything"])
        with pytest.raises(offline_guard.OfflineViolation):
            offline_guard._check_python_child(executable, [str(executable), "-Command", "anything"], {}, "")
        script.write_text("exit 1\n", encoding="utf-8")
        assert not offline_guard._registered_powershell(executable, arguments)
    script.write_text("exit 0\n", encoding="utf-8")
    assert not offline_guard._registered_powershell(executable, arguments)


def _powershell_argv(executable, script, python, logdir) -> list[str]:
    """Canonical 11-element fixture vector (offline_guard.py:69-85 contract)."""
    return [str(executable), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(script), "-Python", str(python), "-LogDir", str(logdir)]


def _forbid_subprocess(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("pure registration rules must not launch any subprocess")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)


def test_powershell_predicate_truth_table(tmp_path, monkeypatch):
    """MH05 plan step 1: record every registration predicate's true/false state.

    The recorded table covers the registration key, the full script hash, the
    resolved executable/script/logdir, fixture-root containment, the installed
    flag, Python identity and the exact argument vector, judged with roots
    scoped to only this test's tmp_path.
    """
    from ak_trading.testing import offline_guard

    _forbid_subprocess(monkeypatch)
    monkeypatch.setattr(offline_guard, "_fixture_roots", (tmp_path.resolve(),))

    script = tmp_path / "fixture.ps1"
    script.write_text("exit 0\n", encoding="utf-8")
    executable = tmp_path / "powershell.exe"
    logdir = tmp_path / "logs"
    arguments = _powershell_argv(executable, script, sys.executable, logdir)
    observed = {
        "resolved_executable": str(executable.resolve()),
        "resolved_script": str(script.resolve()),
        "resolved_logdir": str(logdir.resolve()),
        "script_full_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
        "fixture_roots": tuple(str(root) for root in offline_guard._fixture_roots),
        "guard_installed_flag": offline_guard._installed,
        "script_within_roots": offline_guard._fixture_path(script.resolve()),
        "logdir_within_roots": offline_guard._fixture_path(logdir.resolve()),
        "python_identity_holds": Path(arguments[8]).resolve() == Path(sys.executable).resolve(),
        "argument_vector_shape_holds": (
            len(arguments) == 11
            and tuple(str(value).lower() for value in arguments[1:6])
            == ("-noprofile", "-noninteractive", "-executionpolicy", "bypass", "-file")
            and str(arguments[7]).lower() == "-python"
            and str(arguments[9]).lower() == "-logdir"
        ),
        "judgment_before_registration": offline_guard._registered_powershell(executable, arguments),
    }
    assert observed["script_within_roots"] and observed["logdir_within_roots"]
    assert observed["python_identity_holds"] and observed["argument_vector_shape_holds"]
    assert observed["judgment_before_registration"] is False

    with offline_guard.allow_powershell_fixture(script, str(executable)):
        key = (executable.resolve(), script.resolve())
        observed["registration_key"] = (str(key[0]), str(key[1]))
        observed["stored_hash_matches_bytes"] = (
            offline_guard._powershell_fixtures.get(key)
            == hashlib.sha256(script.read_bytes()).digest()
        )
        observed["judgment_exact_argv"] = offline_guard._registered_powershell(
            executable, arguments)
        script.write_text("exit 1\n", encoding="utf-8")
        observed["stored_hash_matches_changed_bytes"] = (
            offline_guard._powershell_fixtures.get(key)
            == hashlib.sha256(script.read_bytes()).digest()
        )
        observed["judgment_after_one_byte_change"] = offline_guard._registered_powershell(
            executable, arguments)
        script.write_text("exit 0\n", encoding="utf-8")
        observed["judgment_after_byte_restore_in_context"] = (
            offline_guard._registered_powershell(executable, arguments))
    observed["judgment_after_context_exit"] = offline_guard._registered_powershell(
        executable, arguments)

    assert observed["stored_hash_matches_bytes"] is True
    assert observed["stored_hash_matches_changed_bytes"] is False
    assert observed["judgment_exact_argv"] is True
    assert observed["judgment_after_one_byte_change"] is False
    assert observed["judgment_after_byte_restore_in_context"] is True
    assert observed["judgment_after_context_exit"] is False


def test_registration_gate_differs_between_installed_and_plain_contexts(tmp_path, monkeypatch):
    """MH05 plan step 2: installed and un-installed pure judgment contexts.

    Installed: the registration gate refuses scripts outside the declared
    roots outright (offline_guard.py:51). Un-installed: the gate step is
    skipped, but judgment still refuses the same script because script/LogDir
    containment is unconditional (offline_guard.py:81).
    """
    from ak_trading.testing import offline_guard

    _forbid_subprocess(monkeypatch)
    inside = tmp_path / "allowed-root"
    inside.mkdir()
    outside = tmp_path / "denied-root"
    outside.mkdir()
    script_in = inside / "fixture.ps1"
    script_in.write_text("exit 0\n", encoding="utf-8")
    script_out = outside / "fixture.ps1"
    script_out.write_text("exit 0\n", encoding="utf-8")
    executable = inside / "powershell.exe"
    monkeypatch.setattr(offline_guard, "_fixture_roots", (inside.resolve(),))
    argv_in = _powershell_argv(executable, script_in, sys.executable, inside / "logs")
    argv_out = _powershell_argv(executable, script_out, sys.executable, outside / "logs")

    with monkeypatch.context() as installed:
        installed.setattr(offline_guard, "_installed", True)
        with pytest.raises(offline_guard.OfflineViolation):
            with offline_guard.allow_powershell_fixture(script_out, str(executable)):
                pass
        assert (executable.resolve(), script_out.resolve()) not in offline_guard._powershell_fixtures
        with offline_guard.allow_powershell_fixture(script_in, str(executable)):
            assert offline_guard._registered_powershell(executable, argv_in)
            assert not offline_guard._registered_powershell(executable, argv_out)

    with monkeypatch.context() as plain:
        plain.setattr(offline_guard, "_installed", False)
        with offline_guard.allow_powershell_fixture(script_out, str(executable)):
            # Gate skipped, so the registration exists, yet judgment refuses:
            # the out-of-root script and LogDir fail unconditional containment.
            assert (executable.resolve(), script_out.resolve()) in offline_guard._powershell_fixtures
            assert not offline_guard._registered_powershell(executable, argv_out)
        assert (executable.resolve(), script_out.resolve()) not in offline_guard._powershell_fixtures


def test_canonical_launcher_entry_registers_fixture_in_declared_roots(tmp_path):
    """MH05 plan step 2: canonical-launcher integration positive.

    Under scripts/test_offline.py the guard is installed with the launcher's
    own declared roots and this tmp_path lives inside them; no guard state is
    overridden, so the entry itself must supply meaningful containment. Under
    plain pytest the real global roots stay empty and a registered fixture is
    refused - the baseline failure mechanism kept as a constraint, not bypassed.
    """
    from ak_trading.testing import offline_guard

    script = tmp_path / "fixture.ps1"
    script.write_text("exit 0\n", encoding="utf-8")
    executable = tmp_path / "powershell.exe"
    arguments = _powershell_argv(executable, script, sys.executable, tmp_path / "logs")
    if offline_guard._installed:
        assert any(tmp_path.resolve().is_relative_to(root)
                   for root in offline_guard._fixture_roots)
        assert not offline_guard._registered_powershell(executable, arguments)
        with offline_guard.allow_powershell_fixture(script, str(executable)):
            assert offline_guard._registered_powershell(executable, arguments)
            # shell=True cannot ride on a registration: the child gate refuses
            # string command lines before Popen could launch anything.
            with pytest.raises(offline_guard.OfflineViolation):
                subprocess.Popen(str(executable) + " -NoProfile", shell=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert not offline_guard._registered_powershell(executable, arguments)
    else:
        assert offline_guard._fixture_roots == ()
        with offline_guard.allow_powershell_fixture(script, str(executable)):
            assert not offline_guard._registered_powershell(executable, arguments)
    # No global guard state was modified by this test in either entry.
    assert offline_guard._registered_powershell(executable, arguments) is False


def test_windows_normalization_rules_for_paths_flags_and_tokens(tmp_path, monkeypatch):
    """MH05 plan step 3: Windows case, separators, spaces/non-ASCII, tokenization.

    Verified against the production guard's own rules: flags, parameter names
    and the executable name compare case-insensitively; script/LogDir values
    survive spaces and non-ASCII as single tokens; forward slashes resolve onto
    the same registration key; case-folded paths match on Windows (whose
    filesystem canonicalizes case) and must not match on POSIX; and naive
    space-split tokenization or a single string command line is refused.
    """
    from ak_trading.testing import offline_guard

    _forbid_subprocess(monkeypatch)
    deep = tmp_path / "spaced rööt" / "中日-ünïcode"
    deep.mkdir(parents=True)
    script = deep / "Fïxture.PS1"
    script.write_text("exit 0\n", encoding="utf-8")
    executable = deep / "PowerShell.EXE"
    logdir = deep / "lög dir"
    monkeypatch.setattr(offline_guard, "_fixture_roots", (tmp_path.resolve(),))

    with offline_guard.allow_powershell_fixture(script, str(executable)):
        arguments = [str(executable), "-NOPROFILE", "-noninteractive", "-executionpolicy",
                     "BYPASS", "-file", str(script), "-python", sys.executable,
                     "-LOGDIR", str(logdir)]
        assert offline_guard._registered_powershell(executable, arguments)

        def variant(index, value):
            argv = list(arguments)
            argv[index] = value
            return argv

        assert offline_guard._registered_powershell(
            executable, variant(6, str(script).replace("\\", "/")))
        folded = str(script).lower()
        if sys.platform == "win32":
            assert offline_guard._registered_powershell(executable, variant(6, folded))
        else:
            assert not offline_guard._registered_powershell(executable, variant(6, folded))

        split = [str(executable), "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                 "Bypass", "-File", *str(script).split(), "-Python", sys.executable,
                 "-LogDir", str(logdir)]
        assert not offline_guard._registered_powershell(executable, split)
        assert not offline_guard._registered_powershell(executable, " ".join(arguments))


def test_registration_rejects_impersonation_and_boundary_crossings(tmp_path, monkeypatch):
    """MH05 plan step 4 rejections: same basename/different path, out-of-root
    script or LogDir, a different Python binary, malformed vectors and the
    unregistered-executable child gate - all with roots scoped to tmp_path."""
    from ak_trading.testing import offline_guard

    _forbid_subprocess(monkeypatch)
    monkeypatch.setattr(offline_guard, "_fixture_roots", (tmp_path.resolve(),))

    original = tmp_path / "fixture.ps1"
    original.write_text("exit 0\n", encoding="utf-8")
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    impostor = decoy / "fixture.ps1"
    impostor.write_text("exit 0\n", encoding="utf-8")
    executable = tmp_path / "powershell.exe"
    twin_executable = decoy / "powershell.exe"
    outside_logdir = tmp_path.parent / (tmp_path.name + "-outside-logs")

    with offline_guard.allow_powershell_fixture(original, str(executable)):
        base = _powershell_argv(executable, original, sys.executable, tmp_path / "logs")
        assert offline_guard._registered_powershell(executable, base)
        # Same basename is not the same registration key (script or executable).
        assert not offline_guard._registered_powershell(
            executable, _powershell_argv(executable, impostor, sys.executable, tmp_path / "logs"))
        assert not offline_guard._registered_powershell(
            twin_executable,
            _powershell_argv(twin_executable, original, sys.executable, tmp_path / "logs"))
        # Script or LogDir outside the scoped roots is refused.
        assert not offline_guard._registered_powershell(
            executable, _powershell_argv(executable, original, sys.executable, outside_logdir))
        # A different Python binary cannot inherit the registration.
        assert not offline_guard._registered_powershell(
            executable,
            _powershell_argv(executable, original, str(tmp_path / "python.exe"), tmp_path / "logs"))
        # Missing, extra or reordered vector elements are refused.
        assert not offline_guard._registered_powershell(executable, base[:10])
        assert not offline_guard._registered_powershell(executable, [*base, "-Extra"])
        reordered = [base[0], base[2], base[1], *base[3:]]
        assert not offline_guard._registered_powershell(executable, reordered)
        swapped = list(base)
        swapped[7], swapped[9] = swapped[9], swapped[7]
        swapped[8], swapped[10] = swapped[10], swapped[8]
        assert not offline_guard._registered_powershell(executable, swapped)
        # An unregistered executable is refused at the python-child checkpoint.
        with pytest.raises(offline_guard.OfflineViolation):
            offline_guard._check_python_child(
                twin_executable,
                _powershell_argv(twin_executable, original, sys.executable, tmp_path / "logs"),
                {}, "",
            )


def test_exit_code_targeted_selection_and_new_basetemp_are_preserved(tmp_path):
    basetemp = tmp_path / "new-results"
    result = _probe(tmp_path, '''
        def test_success(tmp_path):
            (tmp_path / "proof.txt").write_text("retained")
        def test_failure():
            assert False, "intentional launcher failure"
    ''', "-k", "success", "--basetemp", basetemp)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed, 1 deselected" in result.stdout
    assert list(basetemp.rglob("proof.txt"))
    failed = _run(tmp_path / "test_probe.py", "-q", "-k", "failure")
    assert failed.returncode == 1, failed.stdout + failed.stderr
    assert "intentional launcher failure" in failed.stdout


# --- C05 import-domain isolation regressions (synthetic, real subprocesses) ---

MINI_PYPROJECT = """
[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
"""

MINI_NORMAL = '''
"""Normal-domain probe: module-level import, like ordinary business tests."""
from pathlib import Path

import ak_trading
from ak_trading.testing import offline_guard

def _sandbox_root() -> Path:
    # Depth-independent: the sandbox root is the nearest ancestor carrying the
    # pinned pyproject.toml.
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    raise AssertionError("no pyproject.toml above this probe")


def test_module_origin_and_identity(tmp_path):
    assert Path(ak_trading.__file__).resolve() == (
        _sandbox_root() / "src" / "ak_trading" / "__init__.py"
    )
    plugin = offline_guard.OfflinePlugin()
    assert type(plugin) is offline_guard.OfflinePlugin
    import importlib

    again = importlib.import_module("ak_trading.testing.offline_guard")
    assert again.OfflinePlugin is type(plugin)
    (tmp_path / "normal-proof.txt").write_text("retained", encoding="utf-8")
'''

MINI_HISTORY = '''
"""Historical-domain probe: evict ak_trading.* and re-root onto an incomplete
package at collection time, exactly like the DP03/DP04/DP08 bootstraps."""
import sys
import types
from pathlib import Path

STUB_ROOT = Path(__file__).resolve().parent / "broken_package"

for _name in [
    name for name in list(sys.modules)
    if name == "ak_trading" or name.startswith("ak_trading.")
]:
    del sys.modules[_name]
_pkg = types.ModuleType("ak_trading")
_pkg.__path__ = [str(STUB_ROOT)]
sys.modules["ak_trading"] = _pkg


def test_history_domain_uses_rebuilt_package(tmp_path):
    import ak_trading

    assert ak_trading.__path__[0] == str(STUB_ROOT)
    try:
        from ak_trading.testing import offline_guard  # incomplete stub tree
    except ImportError:
        pass
    else:
        raise AssertionError("synthetic history stub unexpectedly complete")
    (tmp_path / "history-proof.txt").write_text("retained", encoding="utf-8")
'''

MINI_BROKEN = "raise ImportError('synthetic historical collection error (raw2 probe)')\n"

MINI_PIN = '''
"""Historical pin sample: a deliberately wrong sha256 pin must keep failing."""
import hashlib
from pathlib import Path

WRONG_PIN = "0" * 64


def test_pinned_fixture_bytes():
    target = Path(__file__).resolve().parent / "synthetic_pinned.txt"
    assert hashlib.sha256(target.read_bytes()).hexdigest() == WRONG_PIN
'''


def _mini_repo(tmp_path: Path) -> Path:
    """Build a throwaway repository that runs the real candidate launcher+guard."""
    root = tmp_path / "mini"
    (root / "scripts").mkdir(parents=True)
    (root / "src" / "ak_trading" / "testing").mkdir(parents=True)
    (root / "tests" / "datapipeline").mkdir(parents=True)
    launcher_target = root / "scripts" / "test_offline.py"
    guard_target = root / "src" / "ak_trading" / "testing" / "offline_guard.py"
    shutil.copyfile(SCRIPT, launcher_target)
    guard_source = ROOT / "src" / "ak_trading" / "testing" / "offline_guard.py"
    shutil.copyfile(guard_source, guard_target)
    # The sandbox must exercise the candidate bytes, not a drifting copy; the
    # guard copy must stay byte-identical because _bridged_run trusts it to
    # guard every process in the sandbox subtree.
    assert launcher_target.read_bytes() == SCRIPT.read_bytes()
    assert guard_target.read_bytes() == guard_source.read_bytes()
    assert (root / "src" / "ak_trading" / "__init__.py").write_text("") == 0
    assert (root / "src" / "ak_trading" / "testing" / "__init__.py").write_text("") == 0
    (root / "pyproject.toml").write_text(MINI_PYPROJECT, encoding="utf-8")
    (root / "tests" / "test_normal_identity.py").write_text(MINI_NORMAL, encoding="utf-8")
    datapipeline = root / "tests" / "datapipeline"
    (datapipeline / "test_synthetic_history.py").write_text(MINI_HISTORY, encoding="utf-8")
    (datapipeline / "broken_package").mkdir()
    (datapipeline / "test_synthetic_broken.py").write_text(MINI_BROKEN, encoding="utf-8")
    (datapipeline / "test_synthetic_pin.py").write_text(MINI_PIN, encoding="utf-8")
    (datapipeline / "synthetic_pinned.txt").write_bytes(b"synthetic pinned bytes\n")
    return root


_BRIDGE_RUNNER_PS1 = textwrap.dedent('''
    param([string]$Python, [string]$LogDir)
    $ErrorActionPreference = "Stop"
    # Start the bridge interpreter without the repository startup chain: the
    # outer guard legitimately forces that chain into this PowerShell process
    # too, and a bridge python started with it would re-force the chain onto
    # the sandbox launcher (observed as the sandbox resolving ak_trading to the
    # outer worktree). The sandbox launcher re-establishes the full guard
    # chain from candidate bytes in every process that executes test code.
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
    Remove-Item Env:PYTHONSTARTUP -ErrorAction SilentlyContinue
    Remove-Item Env:PYTEST_ADDOPTS -ErrorAction SilentlyContinue
    Remove-Item Env:PYTEST_PLUGINS -ErrorAction SilentlyContinue
    $env:PYTHONIOENCODING = "utf-8"
    & $Python -B (Join-Path $LogDir "bridge.py")
    exit $LASTEXITCODE
''')

_BRIDGE_PY = '''\
"""Clean-start bridge for sandbox launcher runs (see _bridged_run)."""
import json
import os
import subprocess
import sys

here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "command.json"), encoding="utf-8") as handle:
    payload = json.load(handle)
environment = {
    name: value for name, value in os.environ.items()
    if name.upper() not in {
        "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP",
        "PYTEST_ADDOPTS", "PYTEST_PLUGINS",
    }
}
environment["PYTHONIOENCODING"] = "utf-8"
with open(payload["stdout"], "wb") as out, open(payload["stderr"], "wb") as err:
    code = subprocess.run(
        [payload["python"], *payload["argv"]],
        cwd=payload["cwd"], env=environment,
        stdout=out, stderr=err,
    ).returncode
sys.exit(code)
'''


class _BridgedResult:
    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


_bridge_counter = 0


def _bridged_run(root: Path, argv: list[str], timeout: int = 240) -> _BridgedResult:
    """Run a sandbox command with a production-like clean interpreter start.

    Harness-vs-product cause of the first three focused failures: this test
    file executes inside the repository's guarded pytest child, and
    offline_guard._check_python_child force-overwrites PYTHONPATH with the
    repository startup chain for every direct python child spawned here. A
    sandbox launcher started that way never sees its own bootstrap: the outer
    sitecustomize pre-imports this repository's ak_trading and the guard
    installed inside the sandbox launcher rewrites the sandbox children's
    PYTHONPATH, so the sandbox src tree is bypassed (observed as the mini-repo
    normal domain resolving ak_trading to this worktree's src). Production
    launcher processes are not pre-guarded this way. The repair starts the
    sandbox through the guard's own sanctioned native-fixture bridge
    (offline_guard.allow_powershell_fixture, exact 11-argument contract); the
    bridge's python child is the sandbox launcher itself, which installs the
    guard - byte-identical to the candidate, asserted in _mini_repo - in every
    process that executes test code. No guard protection is removed anywhere.
    """
    global _bridge_counter
    from ak_trading.testing import offline_guard

    _bridge_counter += 1
    logdir = root.parent / "bridge-runs" / f"run-{_bridge_counter:03d}"
    logdir.mkdir(parents=True, exist_ok=True)
    (logdir / "runner.ps1").write_text(_BRIDGE_RUNNER_PS1, encoding="utf-8-sig")
    (logdir / "bridge.py").write_text(_BRIDGE_PY, encoding="utf-8")
    command = {
        "python": sys.executable,
        "argv": [str(part) for part in argv],
        "cwd": str(root),
        "stdout": str(logdir / "stdout.txt"),
        "stderr": str(logdir / "stderr.txt"),
    }
    (logdir / "command.json").write_text(json.dumps(command), encoding="utf-8")
    executable = shutil.which("powershell") or shutil.which("pwsh")
    assert executable, "offline sandbox bridge requires PowerShell"
    fixture_argv = [
        executable, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", str(logdir / "runner.ps1"), "-Python", sys.executable,
        "-LogDir", str(logdir),
    ]
    with offline_guard.allow_powershell_fixture(logdir / "runner.ps1", executable):
        completed = subprocess.run(
            fixture_argv, cwd=root, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, check=False,
        )
    stdout_file = logdir / "stdout.txt"
    stderr_file = logdir / "stderr.txt"
    stdout = (
        stdout_file.read_text(encoding="utf-8", errors="replace")
        if stdout_file.exists() else ""
    )
    stderr = (
        stderr_file.read_text(encoding="utf-8", errors="replace")
        if stderr_file.exists() else ""
    )
    if not stdout_file.exists() or completed.stderr:
        stderr = f"{stderr}\n[powershell-bridge] {completed.stdout}{completed.stderr}".strip("\n")
    return _BridgedResult(completed.returncode, stdout, stderr)


def _mini_run(root: Path, *arguments, timeout=240):
    return _bridged_run(
        root, ["-B", str(root / "scripts" / "test_offline.py"), *map(str, arguments)],
        timeout=timeout,
    )


def _collect_only_nodeids(result) -> list[str]:
    return [
        line.strip()
        for line in result.stdout.splitlines()
        if line.startswith("tests/") and "::" in line
    ]


def test_isolated_domains_survive_contamination_collection_error_and_pin_failure(tmp_path):
    root = _mini_repo(tmp_path)
    result = _mini_run(root, "-q")
    combined = result.stdout + result.stderr
    assert result.returncode == 2, combined  # collection error outranks test failure
    assert (
        "Offline plan: 4 domain(s): datapipeline-test_synthetic_broken.py, "
        "datapipeline-test_synthetic_history.py, datapipeline-test_synthetic_pin.py, normal"
    ) in result.stdout
    assert "=== offline domain datapipeline-test_synthetic_broken.py: exit 2 ===" in result.stdout
    assert "=== offline domain datapipeline-test_synthetic_history.py: exit 0 ===" in result.stdout
    assert "=== offline domain datapipeline-test_synthetic_pin.py: exit 1 ===" in result.stdout
    # Isolation: the normal domain still passes despite its polluted siblings.
    assert "=== offline domain normal: exit 0 ===" in result.stdout
    assert "1 passed" in result.stdout
    assert "OFFLINE-DOMAIN-STATS" in result.stdout
    assert "unresolved_files 0" in result.stdout
    assert "Offline overall exit: 2" in result.stdout
    # The deliberately wrong pin keeps failing; its streamed failure text
    # stays visible (the exit line now follows the live stream, so the whole
    # stdout is the failure section).
    assert "AssertionError" in result.stdout


def test_shared_process_run_proves_the_contamination_probe_has_teeth(tmp_path):
    root = _mini_repo(tmp_path)
    history = root / "tests" / "datapipeline" / "test_synthetic_history.py"
    normal = root / "tests" / "test_normal_identity.py"
    shared = _bridged_run(
        root, ["-B", "-m", "pytest", "-q", str(history), str(normal)], timeout=120
    )
    # One plain pytest process: the evicted/stubbed package breaks the normal
    # module at collection, so the isolation probes above are not vacuous.
    assert shared.returncode != 0, shared.stdout + shared.stderr
    assert "ModuleNotFoundError" in shared.stdout + shared.stderr


def test_domain_order_permutations_preserve_nodeids_and_results(tmp_path):
    root = _mini_repo(tmp_path)
    normal = "tests/test_normal_identity.py"
    history = "tests/datapipeline/test_synthetic_history.py"
    forward = _mini_run(root, "--collect-only", "-q", normal, history)
    reverse = _mini_run(root, "--collect-only", "-q", history, normal)
    assert forward.returncode == 0, forward.stdout + forward.stderr
    assert reverse.returncode == 0, reverse.stdout + reverse.stderr
    forward_ids = _collect_only_nodeids(forward)
    reverse_ids = _collect_only_nodeids(reverse)
    assert sorted(forward_ids) == sorted(reverse_ids)
    assert len(forward_ids) == len(set(forward_ids)) == 2  # each node exactly once
    run_forward = _mini_run(root, "-q", normal, history)
    run_reverse = _mini_run(root, "-q", history, normal)
    assert run_forward.returncode == run_reverse.returncode == 0
    assert "=== offline domain normal: exit 0 ===" in run_forward.stdout
    assert "=== offline domain normal: exit 0 ===" in run_reverse.stdout
    assert (
        "=== offline domain datapipeline-test_synthetic_history.py: exit 0 ==="
        in run_forward.stdout
    )
    assert (
        "=== offline domain datapipeline-test_synthetic_history.py: exit 0 ==="
        in run_reverse.stdout
    )
    for run in (run_forward, run_reverse):
        assert "Offline totals: selected 2 (+0 deselected), ran 2, passed 2" in run.stdout


def test_reversed_historical_file_order_keeps_native_domain_results(tmp_path):
    root = _mini_repo(tmp_path)
    broken = "tests/datapipeline/test_synthetic_broken.py"
    pin = "tests/datapipeline/test_synthetic_pin.py"
    history = "tests/datapipeline/test_synthetic_history.py"
    forward = _mini_run(root, "-q", broken, pin, history)
    reverse = _mini_run(root, "-q", history, pin, broken)
    for run in (forward, reverse):
        assert run.returncode == 2, run.stdout + run.stderr
        assert "=== offline domain datapipeline-test_synthetic_broken.py: exit 2 ===" in run.stdout
        assert "=== offline domain datapipeline-test_synthetic_pin.py: exit 1 ===" in run.stdout
        assert "=== offline domain datapipeline-test_synthetic_history.py: exit 0 ===" in run.stdout
        assert "Offline overall exit: 2" in run.stdout


def test_directory_selection_routes_each_datapipeline_file_to_its_own_domain(tmp_path):
    root = _mini_repo(tmp_path)
    result = _mini_run(root, "-q", "tests/datapipeline")
    combined = result.stdout + result.stderr
    assert result.returncode == 2, combined
    plan_lines = [line for line in result.stdout.splitlines() if line.startswith("Offline plan:")]
    assert plan_lines == [
        "Offline plan: 3 domain(s): datapipeline-test_synthetic_broken.py, "
        "datapipeline-test_synthetic_history.py, datapipeline-test_synthetic_pin.py"
    ]
    assert "datapipeline-test_synthetic_broken.py" in result.stdout


def test_explicit_node_selection_routes_single_node_to_single_domain(tmp_path):
    root = _mini_repo(tmp_path)
    result = _mini_run(
        root, "-q",
        "tests/datapipeline/test_synthetic_history.py::test_history_domain_uses_rebuilt_package",
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "Offline plan: 1 domain(s): datapipeline-test_synthetic_history.py" in result.stdout
    assert "1 passed" in result.stdout


def test_option_values_are_not_routed_as_paths_and_empty_domain_is_neutral(tmp_path):
    root = _mini_repo(tmp_path)
    result = _mini_run(
        root, "-q",
        "tests/test_normal_identity.py", "tests/datapipeline/test_synthetic_history.py",
        "--deselect", "tests/test_normal_identity.py::test_module_origin_and_identity",
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined  # raw5 empty normal domain is neutral
    assert "Offline plan: 2 domain(s): normal, datapipeline-test_synthetic_history.py" in result.stdout
    assert "=== offline domain normal: exit 5 ===" in result.stdout
    assert "=== offline domain datapipeline-test_synthetic_history.py: exit 0 ===" in result.stdout
    assert "deselected 1" in result.stdout
    assert "Offline overall exit: 0" in result.stdout


def test_all_domains_empty_selection_returns_five(tmp_path):
    root = _mini_repo(tmp_path)
    result = _mini_run(
        root, "-q",
        "tests/test_normal_identity.py", "tests/datapipeline/test_synthetic_history.py",
        "-k", "no_such_test_exists_anywhere",
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 5, combined
    assert "=== offline domain normal: exit 5 ===" in result.stdout
    assert (
        "=== offline domain datapipeline-test_synthetic_history.py: exit 5 ===" in result.stdout
    )
    assert "Offline overall exit: 5" in result.stdout


def test_test_failure_without_collection_error_propagates_exit_one(tmp_path):
    root = _mini_repo(tmp_path)
    result = _mini_run(
        root, "-q",
        "tests/datapipeline/test_synthetic_pin.py",
        "tests/datapipeline/test_synthetic_history.py",
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 1, combined
    assert "=== offline domain datapipeline-test_synthetic_pin.py: exit 1 ===" in result.stdout
    assert "=== offline domain datapipeline-test_synthetic_history.py: exit 0 ===" in result.stdout
    assert "Offline overall exit: 1" in result.stdout


def test_per_domain_basetemp_is_derived_and_requested_directory_retained(tmp_path):
    root = _mini_repo(tmp_path)
    requested = tmp_path / "requested-results"
    sentinel = tmp_path / "caller-owned.txt"
    sentinel.write_text("keep", encoding="utf-8")
    result = _mini_run(
        root, "-q",
        "tests/test_normal_identity.py", "tests/datapipeline/test_synthetic_history.py",
        "--basetemp", requested,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    # pytest materializes a given --basetemp lazily on first tmp-fixture use,
    # which the synthetic probes now exercise; content must be retained.
    assert (requested / "normal").is_dir()
    assert list((requested / "normal").rglob("normal-proof.txt"))
    assert (requested / "datapipeline-test_synthetic_history.py").is_dir()
    assert list((requested / "datapipeline-test_synthetic_history.py").rglob("history-proof.txt"))
    assert requested.is_dir()  # accepted requested root is retained
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_real_datapipeline_calendar_failures_are_preserved_through_isolation():
    result = subprocess.run(
        [sys.executable, "-B", str(SCRIPT), "-q", "tests/datapipeline/test_calendar.py"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=240, check=False,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 1, combined
    assert "Offline plan: 1 domain(s): datapipeline-test_calendar.py" in result.stdout
    assert "=== offline domain datapipeline-test_calendar.py: exit 1 ===" in result.stdout
    assert '"failed": ' in result.stdout
    failed_count = [
        line for line in result.stdout.splitlines() if line.startswith("Offline domain")
    ]
    assert any("failed 9, skipped 0, errors 0" in line for line in failed_count), failed_count
    assert "unresolved_files 0" in result.stdout
    assert "Offline overall exit: 1" in result.stdout


class _InterruptingPopen:
    """Fake child whose wait() raises KeyboardInterrupt in the parent.

    Emulates the real streaming path: the parent blocks in process.wait()
    while daemon readers drain (here already-EOF) stream objects.
    """

    def __init__(self, *arguments, **keywords):
        self.returncode = None
        self.stdout = io.StringIO("")
        self.stderr = io.StringIO("")
        self._waited = False

    def wait(self, timeout=None):
        if not self._waited:
            self._waited = True
            raise KeyboardInterrupt
        return self.returncode

    def kill(self):
        return None


class _OneGoodThenInterruptPopen:
    """First child completes with a real stats stream; later ones interrupt.

    A completed fake child must set returncode like a real exited process,
    otherwise the launcher honestly reports "no child exit" for it. The
    streamed stdout carries the statistics line the launcher parses.
    """

    popens = 0
    STATS = (
        'OFFLINE-DOMAIN-STATS {"selected": 1, "deselected": 0, "collected": 1, "ran": 1, '
        '"passed": 1, "failed": 0, "skipped": 0, "errors": 0, "selected_nodeids": ["tests/x"], '
        '"failed_nodeids": [], "error_nodeids": [], "collection_errors": [], '
        '"collected_files": ["tests/test_config.py"]}\n'
    )

    def __init__(self, *arguments, **keywords):
        self.returncode = None
        type(self).popens += 1
        self._first = type(self).popens == 1
        self.stdout = io.StringIO(self.STATS if self._first else "")
        self.stderr = io.StringIO("")
        self._waited = False

    def wait(self, timeout=None):
        if self._first:
            self.returncode = 0
            return 0
        if not self._waited:
            self._waited = True
            raise KeyboardInterrupt
        return self.returncode

    def kill(self):
        return None


def test_parent_keyboardinterrupt_on_first_domain_returns_130_and_lists_unrun(
    tmp_path, monkeypatch, capsys
):
    launcher = _launcher()
    monkeypatch.setattr(subprocess, "Popen", _InterruptingPopen)
    code = launcher.main([
        "-q", "tests/test_config.py", "tests/datapipeline/test_calendar.py",
    ])
    output = capsys.readouterr().out
    assert code == 130
    assert "Offline plan: 2 domain(s): normal, datapipeline-test_calendar.py" in output
    assert "domains not run: datapipeline-test_calendar.py" in output
    assert "Offline overall exit: 130" in output


def test_parent_keyboardinterrupt_mid_dispatch_keeps_completed_domain(
    monkeypatch, capsys
):
    launcher = _launcher()
    _OneGoodThenInterruptPopen.popens = 0
    monkeypatch.setattr(subprocess, "Popen", _OneGoodThenInterruptPopen)
    # Three domains: the first completes, the second is interrupted mid-run,
    # the third is never dispatched and must be listed as not run.
    code = launcher.main([
        "-q", "tests/test_config.py", "tests/datapipeline/test_calendar.py",
        "tests/datapipeline/test_collector.py",
    ])
    output = capsys.readouterr().out
    assert code == 130
    assert "=== offline domain normal: exit 0 ===" in output
    assert "=== offline domain datapipeline-test_calendar.py" in output
    assert "domains not run: datapipeline-test_collector.py" in output
    assert "Offline overall exit: 130" in output
    assert _OneGoodThenInterruptPopen.popens == 2


# --- F1/F2/F3 source-repair regressions (independent preparation findings) ---

def test_nested_datapipeline_file_names_never_share_a_domain(tmp_path):
    """tests/datapipeline/sub/test_x.py and tests/datapipeline/sub-test_x.py
    must never flatten to one domain name: the shared subprocess would let the
    evict-and-stub history file contaminate the identity probe exactly like
    the historical DP03/DP04/DP08 bootstraps (plan §4.1)."""
    root = _mini_repo(tmp_path)
    nested = root / "tests" / "datapipeline" / "sub"
    nested.mkdir()
    (nested / "test_x.py").write_text(MINI_HISTORY, encoding="utf-8")
    (nested / "broken_package").mkdir()
    (root / "tests" / "datapipeline" / "sub-test_x.py").write_text(MINI_NORMAL, encoding="utf-8")
    result = _mini_run(
        root, "-q",
        "tests/datapipeline/sub/test_x.py", "tests/datapipeline/sub-test_x.py",
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert (
        "Offline plan: 2 domain(s): datapipeline-sub%stest_x.py, datapipeline-sub%dtest_x.py"
        in result.stdout
    )
    assert "=== offline domain datapipeline-sub%stest_x.py: exit 0 ===" in result.stdout
    assert "=== offline domain datapipeline-sub%dtest_x.py: exit 0 ===" in result.stdout
    assert result.stdout.count("1 passed") == 2  # each file executed in its own process
    assert "unresolved_files 0" in result.stdout


def test_planning_failure_without_paths_preserves_native_failure(tmp_path):
    """A planning failure (corrupted pinned ini) with zero positional arguments
    must surface the native child failure instead of reporting a legal empty
    selection (plan §4.5: collection errors and missing input are never 'no
    tests collected')."""
    root = _mini_repo(tmp_path)
    (root / "pyproject.toml").write_text(
        "[tool.pytest.ini_options\nthis is not valid toml ][\n", encoding="utf-8"
    )
    result = _mini_run(root)
    combined = result.stdout + result.stderr
    assert result.returncode == 4, combined  # native usage/config failure, not 5
    assert "Offline planning failed" in result.stderr
    assert "Offline plan: 1 domain(s): normal" in result.stdout
    assert "1 domain(s) planned, 1 launched, 0 skipped-empty" in result.stdout
    assert "=== offline domain normal: exit 4 ===" in result.stdout
    assert "Offline overall exit: 4" in result.stdout


def test_not_dispatched_files_are_reported_separately_from_unresolved(tmp_path):
    """Files of domains never given a completed collection attempt (user stop,
    interrupt, launcher failure) must not be reported as unresolved enumeration
    disagreements; the not-dispatched listing keeps the accounting exact."""
    root = _mini_repo(tmp_path)
    result = _mini_run(
        root, "-q",
        "tests/datapipeline/test_synthetic_pin.py",
        "tests/datapipeline/test_synthetic_history.py",
        "--maxfail", "1",
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 1, combined  # pin failure is the user stop trigger
    assert "=== offline domain datapipeline-test_synthetic_pin.py: exit 1 ===" in result.stdout
    assert "domains not run: datapipeline-test_synthetic_history.py" in result.stdout
    assert "unresolved_files 0" in result.stdout
    assert (
        "Offline not-dispatched files (no completed collection attempt): "
        "tests/datapipeline/test_synthetic_history.py" in result.stdout
    )
    assert "Offline unresolved files:" not in result.stdout
    assert "Offline overall exit: 1" in result.stdout


# --- RF1 repair regressions (reviewer-confirmed encoding collision) ---

def test_rf1_dash_separator_adjacent_paths_never_share_a_domain(tmp_path):
    """The reviewer's exact boundary pair: tests/datapipeline/a-/b/test_x.py
    and tests/datapipeline/a/-b/test_x.py previously flattened to one domain
    name ('a---b-test_x.py') and the shared subprocess let the collection-time
    evict-and-stub file destroy both files' native results. With the
    prefix-free escape encoding they must run as two isolated domains."""
    root = _mini_repo(tmp_path)
    first = root / "tests" / "datapipeline" / "a-" / "b"
    first.mkdir(parents=True)
    (first / "test_x.py").write_text(MINI_HISTORY, encoding="utf-8")
    (first / "broken_package").mkdir()
    second = root / "tests" / "datapipeline" / "a" / "-b"
    second.mkdir(parents=True)
    (second / "test_x.py").write_text(MINI_NORMAL, encoding="utf-8")
    result = _mini_run(
        root, "-q",
        "tests/datapipeline/a-/b/test_x.py", "tests/datapipeline/a/-b/test_x.py",
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert (
        "Offline plan: 2 domain(s): datapipeline-a%d%sb%stest_x.py, "
        "datapipeline-a%s%db%stest_x.py" in result.stdout
    )
    assert "=== offline domain datapipeline-a%d%sb%stest_x.py: exit 0 ===" in result.stdout
    assert "=== offline domain datapipeline-a%s%db%stest_x.py: exit 0 ===" in result.stdout
    assert result.stdout.count("1 passed") == 2  # each file executed in its own process
    assert "unresolved_files 0" in result.stdout
    # Reversed argument order must yield the identical isolated outcome.
    reversed_result = _mini_run(
        root, "-q",
        "tests/datapipeline/a/-b/test_x.py", "tests/datapipeline/a-/b/test_x.py",
    )
    assert reversed_result.returncode == 0, reversed_result.stdout + reversed_result.stderr
    assert reversed_result.stdout.count("1 passed") == 2


def test_domain_name_encoding_is_prefix_free_roundtrip_and_unique():
    """Property check of the product encoder itself (not a re-implementation):
    over an adversarial universe of remainders built from the escape char,
    dashes, separators and pass-through characters, the encoding must be
    unique and must round-trip through the inverse defined by the documented
    prefix-free code. The exact F1/RF1 boundary pairs are asserted explicitly."""
    launcher = _launcher()
    inverse = {"p": "%", "d": "-", "s": "/"}

    def decode(encoded: str) -> str:
        assert encoded.startswith("datapipeline-")
        body = encoded[len("datapipeline-"):]
        out = []
        index = 0
        while index < len(body):
            character = body[index]
            if character == "%":
                index += 1
                out.append(inverse[body[index]])
            else:
                out.append(character)
            index += 1
        return "".join(out)

    alphabet = "ab-%x"
    universe = [""]
    for _ in range(4):
        universe = [item + character for item in universe for character in alphabet]
    suffixed = sorted(set(
        candidate + variant
        for candidate in universe
        for variant in ("", "/test_x.py", "-test_x.py", ".py", "/a-/b.py", "a%")
    ))
    encodings = {launcher._datapipeline_domain_name(item) for item in suffixed}
    assert len(encodings) == len(suffixed)  # uniqueness on the adversarial universe
    for item in suffixed:
        assert decode(launcher._datapipeline_domain_name(item)) == item

    # Exact boundary pairs, including the reviewer's reproduced collision.
    names = [
        launcher._datapipeline_domain_name(path)
        for path in (
            "a-/b/test_x.py", "a/-b/test_x.py",       # RF1 pair
            "sub/test_x.py", "sub-test_x.py",         # F1 pair
            "a--b/test.py", "a/-/-b/test.py",         # dash runs and separators
            "100%/full.py", "100%25.py",              # escape char itself
            "sub/test", "sub/test.py",                # extension boundary
        )
    ]
    assert len(names) == len(set(names))
    assert launcher._datapipeline_domain_name("a-/b/test_x.py") == (
        "datapipeline-a%d%sb%stest_x.py"
    )
    assert launcher._datapipeline_domain_name("a/-b/test_x.py") == (
        "datapipeline-a%s%db%stest_x.py"
    )


# --- live-output streaming / durable-evidence regressions (T7 author 010) ---

def _streaming_probe(tmp_path: Path, body: str) -> Path:
    probe = tmp_path / "test_stream_probe.py"
    probe.write_text(textwrap.dedent(body), encoding="utf-8")
    return probe


def test_child_output_is_visible_live_before_child_exit(tmp_path):
    """PHASE-A must arrive while the domain child is still sleeping. Under the
    old PIPE-plus-communicate forwarding nothing reached the launcher's stdout
    before the child exited (~4s here), so an early bounded arrival proves
    real live streaming through the launcher."""
    probe = _streaming_probe(tmp_path, '''
        import time
        print("PHASE-A-MARKER", flush=True)
        time.sleep(4)
        print("PHASE-B-MARKER", flush=True)
        def test_probe():
            pass
    ''')
    start = time.monotonic()
    process = subprocess.Popen(
        [sys.executable, "-B", str(SCRIPT), "-q", "-s", str(probe)],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )
    first = None
    second = None
    for line in process.stdout:
        if "PHASE-A-MARKER" in line and first is None:
            first = time.monotonic() - start
        if "PHASE-B-MARKER" in line:
            second = time.monotonic() - start
    process.wait(timeout=60)
    total = time.monotonic() - start
    assert process.returncode == 0
    assert first is not None and second is not None, (first, second)
    # Live-streaming proof: PHASE-A arrives at its emission time, one full
    # child sleep (~4s) before PHASE-B. The old communicate()-then-forward
    # behavior delivered both markers together at child exit (gap ~0).
    assert 3.0 <= second - first <= 5.5, (first, second)
    assert total >= 3.5


def test_written_phase_evidence_survives_forced_launcher_kill(tmp_path):
    """Force-terminate the launcher while its domain child sleeps: the
    per-domain evidence file must still hold the already-flushed phase
    marker (the launcher's temp workspace is only cleaned on normal exit)."""
    probe = _streaming_probe(tmp_path, '''
        import time
        print("DURABLE-PHASE-MARKER", flush=True)
        time.sleep(8)
        print("NEVER-MARKER", flush=True)
        def test_probe():
            pass
    ''')
    process = subprocess.Popen(
        [sys.executable, "-B", str(SCRIPT), "-q", "-s", str(probe)],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )
    evidence = None
    saw_marker = False
    for line in process.stdout:
        if line.startswith("Offline evidence: "):
            evidence = line.split(": ", 1)[1].strip()
        if "DURABLE-PHASE-MARKER" in line:
            saw_marker = True
            break
    assert saw_marker and evidence, (evidence, saw_marker)
    process.kill()  # hard termination of the launcher itself
    process.wait(timeout=30)
    evidence_file = Path(evidence) / "normal.stdout.txt"
    assert evidence_file.is_file(), evidence
    retained = evidence_file.read_text(encoding="utf-8", errors="replace")
    assert "DURABLE-PHASE-MARKER" in retained
    assert "NEVER-MARKER" not in retained


def test_dual_streams_drain_large_volumes_without_deadlock_or_duplication(tmp_path):
    """Both child streams exceed the OS pipe buffer; the concurrent reader
    threads must drain them without deadlock, forward every line exactly
    once (single stderr header), and the abnormal native exit 3 stays 3."""
    probe = _streaming_probe(tmp_path, '''
        import os
        import sys
        for index in range(400):
            print(f"OUTLINE-{index:03d}-" + "x" * 480, flush=True)
        for index in range(200):
            print(f"ERRLINE-{index:03d}-" + "y" * 480, file=sys.stderr, flush=True)
        def test_probe():
            os._exit(3)
    ''')
    result = subprocess.run(
        [sys.executable, "-B", str(SCRIPT), "-q", "-s", str(probe)],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=90, check=False,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 3, (result.returncode, combined[-3000:])
    assert result.stdout.count("OUTLINE-000-") == 1
    assert result.stdout.count("OUTLINE-399-") == 1
    assert result.stdout.count("--- domain stderr ---") == 1
    assert result.stdout.count("ERRLINE-000-") == 1
    assert result.stdout.count("ERRLINE-199-") == 1
    assert result.stdout.count("PHASE-A-MARKER") == 0  # no cross-test leakage
    assert "=== offline domain normal: exit 3 ===" in result.stdout
    assert "statistics unavailable" in result.stdout
    assert "Offline overall exit: 3" in result.stdout
