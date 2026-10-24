"""Regression coverage for the stable offline entry point; no real I/O probes."""
from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import textwrap

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
