"""Process guards used by scripts/test_offline.py, including Python children.

Installation is explicit and uses only the standard library. Application,
pytest, dotenv and database modules are patched lazily, before their import
returns. This is an accidental-I/O guard for trusted tests, not an OS sandbox.
"""
from __future__ import annotations

from contextlib import contextmanager
import functools
import hashlib
import importlib.abc
import importlib.machinery
import ipaddress
import os
from pathlib import Path
import socket
import sys
import weakref

EXCLUDED_NODES = (
    "tests/test_member_rank.py::TestMemberRankImport::test_import_is_idempotent_and_upserts",
    "tests/test_catalog_collection.py::test_catalog_collection_keeps_other_products_after_one_search_failure",
    "tests/test_catalog_collection.py::test_legacy_three_product_collection_consumes_isolated_search_errors",
)


class OfflineViolation(RuntimeError):
    """A blocked operation; messages never include credentials or addresses."""


_installed = False
_empty_root: Path | None = None
_repo_root: Path | None = None
_fixture_roots: tuple[Path, ...] = ()
_http_servers: weakref.WeakSet = weakref.WeakSet()
_powershell_fixtures: dict[tuple[Path, Path], bytes] = {}
_startup_path = ""


@contextmanager
def allow_powershell_fixture(script: Path, executable: str):
    """Trust one temporary native fixture; its Python children remain guarded.

    PowerShell itself is not sandboxed. Callers must review the fixture as
    offline code. Registration lasts only for this context and is invalidated
    if the script changes. Ordinary pytest can use the same fixture helper.
    """
    script = Path(script).resolve()
    executable = Path(executable).resolve()
    if _installed and not _fixture_path(script):
        raise OfflineViolation("offline guard: native fixtures require a temporary script")
    if script.suffix.lower() != ".ps1" or executable.name.lower() not in {
        "powershell.exe", "pwsh", "pwsh.exe",
    }:
        raise OfflineViolation("offline guard: only explicit PowerShell fixtures may be registered")
    key = (executable, script)
    previous = _powershell_fixtures.get(key)
    _powershell_fixtures[key] = hashlib.sha256(script.read_bytes()).digest()
    try:
        yield
    finally:
        if previous is None:
            _powershell_fixtures.pop(key, None)
        else:
            _powershell_fixtures[key] = previous


def _registered_powershell(executable, arguments) -> bool:
    if not isinstance(arguments, (tuple, list)):
        return False
    expected = ("-noprofile", "-noninteractive", "-executionpolicy", "bypass", "-file")
    if len(arguments) != 11 or tuple(str(value).lower() for value in arguments[1:6]) != expected:
        return False
    if str(arguments[7]).lower() != "-python" or str(arguments[9]).lower() != "-logdir":
        return False
    script = Path(arguments[6]).resolve()
    expected_hash = _powershell_fixtures.get((Path(executable).resolve(), script))
    return (
        expected_hash is not None
        and _fixture_path(script)
        and _fixture_path(Path(arguments[10]).resolve())
        and Path(arguments[8]).resolve() == Path(sys.executable).resolve()
        and hashlib.sha256(script.read_bytes()).digest() == expected_hash
    )


def require_installed() -> None:
    if not _installed:
        raise OfflineViolation("offline guard is not installed; use scripts/test_offline.py")


def _fixture_path(path: Path) -> bool:
    return any(path.is_relative_to(root) for root in _fixture_roots)


def _dotenv_path(path, stream=None):
    if not path:
        return None if stream is not None else _empty_root / ".env"
    candidate = Path(os.fsdecode(path)).resolve()
    if not _fixture_path(candidate):
        raise OfflineViolation("offline guard: dotenv reads require a temporary fixture")
    return candidate


def _block_database(*args, **kwargs):
    raise OfflineViolation("offline guard: database connections are disabled")


async def _block_async_database(*args, **kwargs):
    _block_database()


def _patch_module(module) -> None:
    name = module.__name__
    if name in {"ak_trading.config", "ak_trading.persistence.config"}:
        module.ROOT = _empty_root
    elif name == "dotenv.main":
        original = module.DotEnv.__init__

        @functools.wraps(original)
        def init(self, dotenv_path=None, stream=None, *args, **kwargs):
            return original(self, _dotenv_path(dotenv_path, stream), stream, *args, **kwargs)

        def find_dotenv(filename=".env", raise_error_if_not_found=False, usecwd=False):
            candidate = _dotenv_path(_empty_root / filename)
            if candidate.is_file():
                return str(candidate)
            if raise_error_if_not_found:
                raise IOError("offline guard: no temporary dotenv fixture")
            return ""

        module.DotEnv.__init__ = init
        module.find_dotenv = find_dotenv
    elif name == "http.server":
        original = module.HTTPServer.server_bind

        @functools.wraps(original)
        def server_bind(server):
            original(server)
            _http_servers.add(server)

        module.HTTPServer.server_bind = server_bind
    elif name == "subprocess":
        import inspect

        original = module.Popen.__init__
        signature = inspect.signature(original)

        @functools.wraps(original)
        def popen(self, args, *positional, **keywords):
            bound = signature.bind(self, args, *positional, **keywords)
            environment = dict(bound.arguments.get("env") or os.environ)
            _check_python_child(
                bound.arguments.get("executable") or (args[0] if isinstance(args, (list, tuple)) else ""),
                args, environment, _startup_path,
            )
            if bound.arguments.get("shell"):
                raise OfflineViolation("offline guard: shell subprocesses are disabled")
            bound.arguments["env"] = environment
            return original(*bound.args, **bound.kwargs)

        module.Popen.__init__ = popen
    elif name == "psycopg.connection":
        module.Connection.connect = classmethod(_block_database)
    elif name == "psycopg.connection_async":
        module.AsyncConnection.connect = classmethod(_block_async_database)
    elif name == "psycopg":
        module.connect = _block_database
    elif name in {"psycopg2", "psycopg2._psycopg"}:
        if hasattr(module, "connect"):
            module.connect = _block_database
        if hasattr(module, "_connect"):
            module._connect = _block_database
    elif name == "psycopg.pq":
        original = module.PGconn

        class GuardedPGconn:
            # libpq uses native sockets, outside Python's socket audit events.
            connect = staticmethod(_block_database)
            connect_start = staticmethod(_block_database)

            def __new__(cls, *args, **kwargs):
                return original(*args, **kwargs)

        module.PGconn = GuardedPGconn
    elif name == "sqlalchemy":
        def before_connect(dialect, conn_rec, cargs, cparams):
            if dialect.name != "sqlite":
                _block_database()

        module.event.listen(module.engine.Engine, "do_connect", before_connect)


_PATCHED_MODULES = {
    "ak_trading.config", "ak_trading.persistence.config", "dotenv.main", "http.server",
    "psycopg", "psycopg.connection", "psycopg.connection_async", "psycopg.pq",
    "psycopg2", "psycopg2._psycopg", "sqlalchemy", "subprocess",
}


class _GuardLoader(importlib.abc.Loader):
    def __init__(self, original):
        self.original = original

    def create_module(self, spec):
        return self.original.create_module(spec)

    def exec_module(self, module):
        self.original.exec_module(module)
        _patch_module(module)


class _GuardFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname not in _PATCHED_MODULES:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is not None and spec.loader is not None:
            spec.loader = _GuardLoader(spec.loader)
        return spec


def _loopback(host) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="replace")
    if str(host).lower().rstrip(".") == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return (mapped or address).is_loopback


def _check_connection(address) -> None:
    if not isinstance(address, tuple) or len(address) < 2 or not _loopback(address[0]):
        raise OfflineViolation("offline guard: external network and database sockets are disabled")
    port = address[1]
    if not any(
        server.socket.fileno() >= 0 and server.server_address[1] == port
        for server in tuple(_http_servers)
    ):
        raise OfflineViolation("offline guard: only local HTTP fixture connections are allowed")


def _check_python_child(executable, arguments, environment, inherited_path) -> None:
    python_child = Path(os.fsdecode(executable)).resolve() == Path(sys.executable).resolve()
    if not python_child and not _registered_powershell(executable, arguments):
        raise OfflineViolation("offline guard: only guarded Python or registered native fixtures are allowed")
    if not isinstance(arguments, (tuple, list)):
        raise OfflineViolation("offline guard: shell subprocesses are disabled")
    options = iter(arguments[1:] if python_child else [])
    for argument in options:
        if argument in {"-c", "-m", "--"} or not argument.startswith("-"):
            break
        if argument in {"-W", "-X"}:
            next(options, None)
        elif argument.startswith(("-W", "-X", "--")):
            continue
        elif any(flag in argument[1:] for flag in "ISEs"):
            raise OfflineViolation("offline guard: Python startup guard cannot be disabled")
    if environment is not None:
        # A few CLI tests deliberately construct a minimal env. Add only the
        # startup protection; preserve their explicit synthetic credentials.
        if Path(sys.argv[0]).resolve() != _repo_root / "scripts" / "test_offline.py":
            environment["PYTHONPATH"] = inherited_path
        else:
            environment.setdefault("PYTHONPATH", inherited_path)
        environment["PYTHONNOUSERSITE"] = "1"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"


def install(empty_root, repo_root, fixture_roots) -> None:
    """Install before importing pytest or any test/conftest/application module."""
    global _installed, _empty_root, _repo_root, _fixture_roots, _startup_path
    if _installed:
        return
    _empty_root = Path(empty_root).resolve()
    _repo_root = Path(repo_root).resolve()
    _fixture_roots = tuple(Path(root).resolve() for root in fixture_roots)
    if not _empty_root.is_dir() or not _fixture_path(_empty_root):
        raise OfflineViolation("offline guard: missing isolated settings root")
    _startup_path = os.environ.get("PYTHONPATH", "")

    def audit(event, arguments):
        if event == "open":
            filename = arguments[0]
            if isinstance(filename, (str, bytes, os.PathLike)):
                name = os.path.basename(os.fsdecode(filename)).lower()
                if name == ".env" or name.startswith(".env.") or name.endswith(".env"):
                    if not _fixture_path(Path(os.fsdecode(filename)).resolve()):
                        raise OfflineViolation("offline guard: real dotenv files are disabled")
        elif event in {"socket.connect", "socket.sendto"}:
            _check_connection(arguments[-1])
        elif event == "socket.sendmsg" and arguments[-1] is not None:
            _check_connection(arguments[-1])
        elif event == "socket.bind":
            address = arguments[1]
            if not isinstance(address, tuple) or not _loopback(address[0]):
                raise OfflineViolation("offline guard: only loopback fixture binds are allowed")
    sys.addaudithook(audit)
    original_getaddrinfo = socket.getaddrinfo

    def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        if host is not None and not _loopback(host):
            raise OfflineViolation("offline guard: external DNS is disabled")
        if str(host).lower().rstrip(".") == "localhost":
            host = "::1" if family == socket.AF_INET6 else "127.0.0.1"
        return original_getaddrinfo(host, port, family, type, proto, flags)

    def gethostbyname(host):
        if not _loopback(host):
            raise OfflineViolation("offline guard: external DNS is disabled")
        return "127.0.0.1" if str(host).lower().rstrip(".") == "localhost" else host

    def gethostbyaddr(host):
        return "localhost", [], [gethostbyname(host)]

    socket.getaddrinfo = getaddrinfo
    socket.gethostbyname = gethostbyname
    socket.gethostbyname_ex = gethostbyaddr
    socket.gethostbyaddr = gethostbyaddr
    sys.meta_path.insert(0, _GuardFinder())
    for name in _PATCHED_MODULES:
        if name in sys.modules:
            _patch_module(sys.modules[name])
    _installed = True


class OfflinePlugin:
    """Mandatory selection is independent of pytest's -m/-k expressions."""

    def pytest_sessionstart(self, session):
        self.session = session

    def pytest_ignore_collect(self, collection_path, config):
        # Explicit targets outside the repo otherwise make pytest enumerate
        # every ancestor (including the shared OS temp directory on Windows).
        # Keep those unrelated, concurrently deleted directories out of the
        # collection tree without narrowing any requested test directory.
        if self.session.isinitpath(collection_path, with_parents=True):
            return None
        if not any(self.session.isinitpath(parent) for parent in collection_path.parents):
            return True
        return None

    def pytest_report_header(self):
        return "offline guard active: postgres + 3 explicit nodes excluded; temporary settings"

    def pytest_collection_modifyitems(self, config, items):
        excluded = []
        selected = []
        for item in items:
            relative = Path(item.path).resolve().relative_to(_repo_root) if Path(item.path).resolve().is_relative_to(_repo_root) else None
            suffix = item.nodeid.split("::", 1)[1] if "::" in item.nodeid else ""
            node = f"{relative.as_posix()}::{suffix}" if relative else ""
            mandatory = item.get_closest_marker("postgres") is not None or any(
                node == blocked or node.startswith(blocked + "[") for blocked in EXCLUDED_NODES
            )
            (excluded if mandatory else selected).append(item)
        if excluded:
            items[:] = selected
            config.hook.pytest_deselected(items=excluded)

    def pytest_terminal_summary(self, terminalreporter):
        deselected = len(terminalreporter.stats.get("deselected", []))
        terminalreporter.write_line(
            f"Offline selection: {terminalreporter._session.testscollected} selected, "
            f"{deselected} deselected (mandatory exclusions cannot be overridden)"
        )
