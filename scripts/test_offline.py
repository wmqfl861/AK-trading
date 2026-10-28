"""Run pytest offline: python scripts/test_offline.py [pytest arguments].

Test paths, -k, -m, --collect-only and reporting options are forwarded. Plugin,
configuration and response-file overrides are refused. --basetemp must name a
new directory; an explicit directory is retained for inspection after the run.

Import-domain isolation (C05): every selected test file under tests/datapipeline/
runs in its own guarded pytest subprocess while all remaining tests share one
"normal" subprocess, dispatched in selection order. The historical DP03/DP04/
DP08 fixtures rebuild and evict the ak_trading package during collection, so
they must never share an interpreter with ordinary tests or with each other.
Path ownership is decided before any collection import by reusing pytest's own
argument parser and the pinned -c pyproject.toml enumeration rules
(testpaths/python_files/norecursedirs); explicit files and ::node suffixes keep
their native selection semantics. Each domain keeps its native pytest exit code
and streams; the parent aggregates them without masking failures and returns
130 on KeyboardInterrupt.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading

ROOT = Path(__file__).resolve().parents[1]
DATAPIPELINE_TESTS = ROOT / "tests" / "datapipeline"
DATAPIPELINE_PREFIX = "tests/datapipeline/"
NORMAL_DOMAIN = "normal"
DOMAIN_STATS_PREFIX = "OFFLINE-DOMAIN-STATS "

# Written into every child bootstrap directory; imported there before pytest.
_CHILD_STATISTICS_SOURCE = '''"""Per-domain statistics for scripts/test_offline.py children.

Standard library only: this module runs inside the guarded child process and
must not depend on repository packages or pytest at import time.
"""
from __future__ import annotations

import json


PREFIX = __PREFIX__


class DomainStatsPlugin:
    """Count the pytest events the offline launcher aggregates per domain."""

    def __init__(self):
        self.deselected = 0
        self.selected_nodeids = []
        self.ran_nodeids = []
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.errors = 0
        self.failed_nodeids = []
        self.error_nodeids = []
        self.collection_errors = []
        self.collected_files = []

    def pytest_deselected(self, items):
        self.deselected += len(items)

    def pytest_collection_finish(self, session):
        self.selected_nodeids = [item.nodeid for item in session.items]

    def pytest_collectreport(self, report):
        if report.failed:
            self.collection_errors.append(report.nodeid or "<session>")
        elif report.nodeid.endswith(".py"):
            self.collected_files.append(report.nodeid)

    def pytest_runtest_logstart(self, nodeid, location):
        self.ran_nodeids.append(nodeid)

    def pytest_runtest_logreport(self, report):
        if report.outcome == "skipped":
            self.skipped += 1
        elif report.outcome == "failed":
            if report.when == "call":
                self.failed += 1
                self.failed_nodeids.append(report.nodeid)
            else:
                self.errors += 1
                self.error_nodeids.append(report.nodeid + " (" + report.when + ")")
        elif report.outcome == "passed" and report.when == "call":
            self.passed += 1

    def snapshot(self):
        return {
            "selected": len(self.selected_nodeids),
            "deselected": self.deselected,
            "collected": len(self.selected_nodeids) + self.deselected,
            "ran": len(set(self.ran_nodeids)),
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "errors": self.errors,
            "selected_nodeids": self.selected_nodeids,
            "failed_nodeids": self.failed_nodeids,
            "error_nodeids": self.error_nodeids,
            "collection_errors": self.collection_errors,
            "collected_files": self.collected_files,
        }


def main(argv):
    import pytest

    from ak_trading.testing import offline_guard

    stats = DomainStatsPlugin()
    code = pytest.main(argv, plugins=[offline_guard.OfflinePlugin(), stats])
    print(PREFIX + json.dumps(stats.snapshot()), flush=True)
    return code
'''.replace("__PREFIX__", repr(DOMAIN_STATS_PREFIX))


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


class DomainPlan:
    """One protected subprocess: forwarded options plus its own positional set."""

    def __init__(self, name: str, options: list[str]):
        self.name = name
        self.options = options
        self.arguments: list[str] = []
        self.files: list[str] = []
        self.state = "planned"
        self.exit_code: int | None = None
        self.stats: dict | None = None
        # Planning-failure fallbacks must always reach a child pytest run so
        # the native failure surfaces; an empty positional list must never be
        # reported as a legal empty selection (plan §4.5).
        self.launch_anyway = False


def _planning_configuration(arguments: list[str]):
    """Reuse pytest's own parser and ini handling before any collection import.

    Builds the real pytest Config for the pinned -c pyproject.toml with conftest
    loading disabled: the parent must never import test modules or execute
    conftests. Any parser failure propagates to the caller, which falls back to
    a single native-failure domain.
    """
    import _pytest.config as pytest_config

    class PlanningConfig(pytest_config.Config):
        # typing marks Config @final; this single-purpose override exists only
        # to guarantee the "no conftests in the planning parent" invariant.
        def pytest_load_initial_conftests(self, early_config, parser, args):
            return None

    planning_arguments = ["-c", str(ROOT / "pyproject.toml"), *arguments]
    pluginmanager = pytest_config.PytestPluginManager()
    invocation = pytest_config.Config.InvocationParams(
        args=tuple(planning_arguments), plugins=None, dir=ROOT,
    )
    config = PlanningConfig(pluginmanager, invocation_params=invocation)
    pluginmanager.consider_preparse(planning_arguments, exclude_only=True)
    for module_name in pytest_config.default_plugins:
        pluginmanager.import_plugin(module_name)
    previous_directory = os.getcwd()
    # The testpaths glob resolves against the process cwd, exactly like the
    # child (which always runs with cwd=ROOT); mirror that here.
    os.chdir(ROOT)
    try:
        config.parse(planning_arguments, addopts=False)
    finally:
        os.chdir(previous_directory)
    return config


def _planning_config_guarded(arguments: list[str]):
    """Plan with the same plugin environment the guarded children get."""
    reserved = ("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "PYTEST_PLUGINS")
    saved = {name: os.environ.get(name) for name in reserved}
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    os.environ["PYTEST_PLUGINS"] = ""
    try:
        return _planning_configuration(arguments)
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _option_value_actions(parser) -> dict[str, bool]:
    """Map exact option spellings to whether they consume the next argv token.

    Introspects pytest's own argparse registration (allow_abbrev=False, so
    spellings match exactly) instead of maintaining a hand-written table.
    """
    consumes: dict[str, bool] = {}
    for action in parser.optparser._actions:
        takes_value = (
            action.nargs is None
            or action.nargs in ("*", "+")
            or (isinstance(action.nargs, int) and action.nargs > 0)
        )
        for spelling in action.option_strings:
            consumes[spelling] = takes_value
    return consumes


def _partition_arguments(arguments: list[str], consumes: dict[str, bool]):
    """Split forwarded argv into option tokens and positional tokens."""
    options: list[str] = []
    positionals: list[str] = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            positionals.extend(arguments[index + 1:])
            break
        if argument.startswith("-") and argument != "-":
            options.append(argument)
            if "=" not in argument and consumes.get(argument) and index + 1 < len(arguments):
                options.append(arguments[index + 1])
                index += 2
                continue
            index += 1
            continue
        positionals.append(argument)
        index += 1
    return options, positionals


def _repo_relative(path: Path) -> str | None:
    """Repo-relative posix path, case-robust on Windows; None outside the repo."""
    resolved = path.resolve()
    normalized = os.path.normcase(str(resolved))
    root = os.path.normcase(str(ROOT))
    if not normalized.startswith(root + os.sep):
        return None
    return str(resolved)[len(str(ROOT)) + 1:].replace("\\", "/")


def _datapipeline_remainder(path: Path) -> str | None:
    """Path relative to tests/datapipeline (case-robust), if it lies under it."""
    resolved = path.resolve()
    normalized = os.path.normcase(str(resolved))
    base = os.path.normcase(str(DATAPIPELINE_TESTS))
    if not normalized.startswith(base + os.sep):
        return None
    return str(resolved)[len(str(DATAPIPELINE_TESTS)) + 1:].replace("\\", "/")


def _datapipeline_domain_name(remainder: str) -> str:
    """Deterministically and injectively encode the tests/datapipeline-relative
    path as a filesystem-safe domain name using a prefix-free escape code:
    '%' → '%p', '-' → '%d', '/' → '%s'; every other filename character passes
    through unchanged. A '%' in the output always starts exactly one
    two-character escape, so decoding is unambiguous and distinct test files
    can never share one guarded subprocess (plan §4.1); the extension is kept
    so `sub/test` cannot collide with `sub/test.py` either."""
    escape = {"%": "%p", "-": "%d", "/": "%s"}
    return "datapipeline-" + "".join(
        escape.get(character, character) for character in remainder
    )


def _enumerate_test_files(directory: Path, python_files: list[str], norecursedirs: list[str]):
    """Mirror pytest's directory collection: scandir name order and fnmatch rules."""
    from _pytest.pathlib import fnmatch_ex

    found: list[Path] = []

    def visit(current: Path) -> None:
        try:
            entries = sorted(current.iterdir(), key=lambda entry: entry.name)
        except OSError as error:
            print(f"Offline enumeration: cannot list {current}: {error}", file=sys.stderr)
            return
        for entry in entries:
            if entry.is_dir():
                if (entry / "pyvenv.cfg").is_file():
                    continue  # pytest's _in_venv skip (non-conda part)
                if any(fnmatch_ex(pattern, entry) for pattern in norecursedirs):
                    continue
                visit(entry)
            elif entry.is_file() and any(fnmatch_ex(pattern, entry) for pattern in python_files):
                found.append(entry)

    visit(directory)
    return found


def plan_domains(arguments: list[str], config) -> list[DomainPlan]:
    """Assign every selected test file to exactly one protected subprocess."""
    consumes = _option_value_actions(config._parser)
    options, positionals = _partition_arguments(arguments, consumes)
    if positionals != list(getattr(config.option, "file_or_dir", None) or []):
        raise ValueError("argument partition disagrees with pytest's parser")

    python_files = config.getini("python_files")
    norecursedirs = config.getini("norecursedirs")
    domains: dict[str, DomainPlan] = {}
    order: list[DomainPlan] = []

    def obtain(name: str) -> DomainPlan:
        if name not in domains:
            domains[name] = DomainPlan(name, options)
            order.append(domains[name])
        return domains[name]

    def route_file(path: Path, token: str) -> None:
        remainder = _datapipeline_remainder(path)
        relative = _repo_relative(path)
        if remainder is not None:
            domain = obtain(_datapipeline_domain_name(remainder))
        else:
            domain = obtain(NORMAL_DOMAIN)
        domain.arguments.append(token)
        if relative is not None:
            domain.files.append(relative)

    for initial in config.args:
        candidate = Path(initial.split("::", 1)[0])
        if not candidate.is_absolute():
            candidate = ROOT / candidate
        resolved = candidate.resolve()
        if resolved.is_dir():
            for path in _enumerate_test_files(resolved, python_files, norecursedirs):
                route_file(path, _repo_relative(path) or path.as_posix())
        else:
            route_file(resolved, initial)
    return order


def _parse_statistics(stdout: str) -> dict | None:
    for line in reversed(stdout.splitlines()):
        if line.startswith(DOMAIN_STATS_PREFIX):
            try:
                return json.loads(line[len(DOMAIN_STATS_PREFIX):])
            except ValueError:
                return None
    return None


def _launch_domain(domain: DomainPlan, workspace: Path, empty_root: Path, basetemp_root: Path):
    """Run one guarded pytest child with live, durable output forwarding.

    Both child streams are drained concurrently by reader threads (no PIPE
    deadlock), forwarded line-by-line with an immediate flush so output is
    visible before the child exits, and appended to per-domain evidence files
    under workspace/evidence that are flushed per line: already-written phase
    evidence therefore survives a forced launcher kill (the workspace is only
    cleaned on a normal launcher exit). Each domain prints a starting line
    before the stream and its native exit code afterwards; there is no
    duplicate final dump. Returns (state, stdout, stderr, interrupted) with
    the accumulated stream text used for statistics parsing.
    """
    domain_basetemp = basetemp_root / domain.name
    if domain_basetemp.exists() or domain_basetemp.is_symlink():
        print(
            f"Offline launcher: per-domain basetemp already exists: {domain_basetemp}",
            file=sys.stderr,
        )
        return "basetemp_error", "", "", False
    bootstrap = workspace / "bootstrap" / domain.name
    bootstrap.mkdir(parents=True)
    startup = (
        "import os, sys\n"
        "try:\n"
        "    from ak_trading.testing import offline_guard\n"
        f"    offline_guard.install({str(empty_root)!r}, {str(ROOT)!r}, "
        f"{[str(workspace), str(domain_basetemp)]!r})\n"
        "except BaseException:\n"
        "    sys.stderr.write('Offline guard startup failed; refusing to run.\\n')\n"
        "    os._exit(4)\n"
    )
    (bootstrap / "sitecustomize.py").write_text(startup, encoding="utf-8")
    (bootstrap / "offline_domain_stats.py").write_text(
        _CHILD_STATISTICS_SOURCE, encoding="utf-8"
    )
    code = (
        "from ak_trading.testing import offline_guard\n"
        "offline_guard.require_installed()\n"
        "import sys\n"
        "import offline_domain_stats\n"
        "raise SystemExit(offline_domain_stats.main(sys.argv[1:]))\n"
    )
    command = [
        sys.executable, "-B", "-c", code,
        "-c", str(ROOT / "pyproject.toml"), "-o", "addopts=",
        "-p", "no:cacheprovider", "--basetemp", str(domain_basetemp),
        *domain.options, *domain.arguments,
    ]
    try:
        process = subprocess.Popen(
            command, cwd=ROOT,
            env=child_environment(dict(os.environ), bootstrap),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
    except OSError as error:
        print(
            f"Offline launcher: cannot start domain {domain.name}: {error!r}",
            file=sys.stderr,
        )
        return "spawn_error", "", "", False
    print(f"--- offline domain {domain.name}: running ---", flush=True)
    evidence_dir = workspace / "evidence"
    stdout_sink: list[str] = []
    stderr_sink: list[str] = []
    stderr_header = [False]
    stream_lock = threading.Lock()

    def forward(line: str) -> None:
        with stream_lock:
            sys.stdout.write(line)
            sys.stdout.flush()

    def forward_stderr(line: str) -> None:
        with stream_lock:
            if not stderr_header[0]:
                stderr_header[0] = True
                sys.stdout.write("--- domain stderr ---\n")
            sys.stdout.write(line)
            sys.stdout.flush()

    def pump(stream, sink: list[str], forwarder, evidence_file) -> None:
        for line in iter(stream.readline, ""):
            sink.append(line)
            if evidence_file is not None:
                evidence_file.write(line)
                evidence_file.flush()
            forwarder(line)
        if evidence_file is not None:
            evidence_file.close()

    readers = []
    for stream, sink, forwarder, suffix in (
        (process.stdout, stdout_sink, forward, ".stdout.txt"),
        (process.stderr, stderr_sink, forward_stderr, ".stderr.txt"),
    ):
        if stream is None:
            continue
        evidence_file = open(
            evidence_dir / (domain.name + suffix), "w",
            encoding="utf-8", errors="replace",
        )
        reader = threading.Thread(
            target=pump, args=(stream, sink, forwarder, evidence_file), daemon=True
        )
        reader.start()
        readers.append(reader)
    interrupted = False
    try:
        process.wait()
    except KeyboardInterrupt:
        interrupted = True
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
    for reader in readers:
        reader.join(timeout=10)
    stdout = "".join(stdout_sink)
    stderr = "".join(stderr_sink)
    domain.exit_code = process.returncode
    domain.stats = _parse_statistics(stdout)
    exit_note = f"exit {domain.exit_code}" if domain.exit_code is not None else "no child exit"
    print(f"=== offline domain {domain.name}: {exit_note} ===", flush=True)
    return ("interrupted" if interrupted else "launched"), stdout, stderr, interrupted


def _domain_counts(domain: DomainPlan) -> str:
    stats = domain.stats
    if stats is None:
        return "statistics unavailable"
    return (
        f"selected {stats.get('selected', 0)}, deselected {stats.get('deselected', 0)}, "
        f"ran {stats.get('ran', 0)}, passed {stats.get('passed', 0)}, "
        f"failed {stats.get('failed', 0)}, skipped {stats.get('skipped', 0)}, "
        f"errors {stats.get('errors', 0)}, "
        f"collection_errors {len(stats.get('collection_errors', []))}"
    )


def _overall_exit_code(domains: list[DomainPlan], interrupted: bool) -> int:
    """Aggregate native pytest exits: 4 > 3 > 2 > 1 > (0 | 5); interrupt = 130."""
    if interrupted:
        return 130
    codes = [domain.exit_code for domain in domains if domain.state == "launched"]
    internal = any(
        domain.state in ("spawn_error", "basetemp_error") for domain in domains
    )
    if 4 in codes:
        return 4
    if 3 in codes or internal:
        return 3
    if 2 in codes:
        return 2
    if 1 in codes:
        return 1
    if any(code not in (0, 5) for code in codes):
        return 3  # abnormal child exit: report as internal, raw values stay listed
    any_selected = any(
        domain.stats and domain.stats.get("selected", 0) > 0
        for domain in domains if domain.state == "launched"
    )
    any_clean_zero = any(
        domain.exit_code == 0 for domain in domains if domain.state == "launched"
    )
    if any_selected or any_clean_zero:
        return 0
    return 5


def _print_summary(domains: list[DomainPlan], interrupted: bool, overall: int) -> None:
    launched = [domain for domain in domains if domain.state == "launched"]
    empty = [domain for domain in domains if domain.state == "empty"]
    unrun = [domain for domain in domains if domain.state == "unrun"]
    failed_states = [domain for domain in domains if domain.state in ("spawn_error", "basetemp_error")]
    print(
        f"Offline summary: {len(domains)} domain(s) planned, {len(launched)} launched, "
        f"{len(empty)} skipped-empty, {len(unrun)} unrun, {len(failed_states)} launcher-failed"
    )
    selected_nodeids: set[str] = set()
    failed_nodeids: set[str] = set()
    error_nodeids: set[str] = set()
    resolved_files: set[str] = set()
    error_files: set[str] = set()
    deselected = 0
    ran = 0
    passed = 0
    skipped = 0
    for domain in launched:
        exit_note = (
            f"exit {domain.exit_code}" if domain.exit_code is not None else "no child exit"
        )
        print(f"Offline domain {domain.name}: {exit_note}, {_domain_counts(domain)}")
        stats = domain.stats or {}
        selected_nodeids.update(stats.get("selected_nodeids", []))
        failed_nodeids.update(stats.get("failed_nodeids", []))
        error_nodeids.update(stats.get("error_nodeids", []))
        deselected += stats.get("deselected", 0)
        ran += stats.get("ran", 0)
        passed += stats.get("passed", 0)
        skipped += stats.get("skipped", 0)
        resolved_files.update(stats.get("collected_files", []))
        error_files.update(
            entry for entry in stats.get("collection_errors", []) if entry.endswith(".py")
        )
    planned_files: set[str] = set()
    undispatched_files: set[str] = set()
    never_dispatched = ("unrun", "spawn_error", "basetemp_error", "interrupted")
    for domain in domains:
        if domain.state in never_dispatched:
            # These files never got a completed collection attempt (interrupt,
            # user stop, launcher failure); they are not enumeration
            # disagreements, so keep them out of "unresolved".
            undispatched_files.update(domain.files)
        else:
            planned_files.update(domain.files)
    unresolved = sorted(planned_files - resolved_files - error_files)
    print(
        f"Offline totals: selected {len(selected_nodeids)} (+{deselected} deselected), "
        f"ran {ran}, passed {passed}, failed {len(failed_nodeids)}, "
        f"skipped {skipped}, errors {len(error_nodeids)}, "
        f"collection_error_files {len(error_files)}, unresolved_files {len(unresolved)}"
    )
    if undispatched_files:
        print(
            "Offline not-dispatched files (no completed collection attempt): "
            + ", ".join(sorted(undispatched_files))
        )
    if error_files:
        print(f"Offline collection error files: {', '.join(sorted(error_files))}")
    if unresolved:
        print(f"Offline unresolved files: {', '.join(unresolved)}")
    if failed_states:
        print(
            "Offline launcher-failed domains: "
            + ", ".join(f"{domain.name} ({domain.state})" for domain in failed_states)
        )
    if unrun:
        print(
            ("Offline interrupted; " if interrupted else "Offline stopped early; ")
            + "domains not run: " + ", ".join(domain.name for domain in unrun)
        )
    print(f"Offline overall exit: {overall}")


def _run_isolated(arguments: list[str], requested_temp: Path | None) -> int:
    planning_failure: BaseException | None = None
    config = None
    try:
        config = _planning_config_guarded(arguments)
    except BaseException as failure:  # native failure path below
        planning_failure = failure

    maxfail: int | None = None
    if config is None:
        print(
            f"Offline planning failed ({type(planning_failure).__name__}); "
            "running one single-domain command to preserve the native failure",
            file=sys.stderr,
        )
        domains = [DomainPlan(NORMAL_DOMAIN, [])]
        domains[0].arguments = list(arguments)
        domains[0].launch_anyway = True
    else:
        if getattr(config.option, "help", None) or getattr(config.option, "version", None):
            domains = [DomainPlan(NORMAL_DOMAIN, [])]
            domains[0].arguments = list(arguments)
        else:
            try:
                domains = plan_domains(arguments, config)
            except ValueError as failure:
                print(f"Offline launcher: {failure}", file=sys.stderr)
                return 4
        maxfail = getattr(config.option, "maxfail", None)

    print(
        "Offline command: python scripts/test_offline.py "
        f"({len(arguments)} pytest arguments forwarded; guard before collection)"
    )
    print(
        f"Offline plan: {len(domains)} domain(s): "
        + (", ".join(domain.name for domain in domains) if domains else "(none)")
    )

    interrupted = False
    failures_so_far = 0
    with TemporaryDirectory(prefix="ak-trading-offline-") as temporary:
        workspace = Path(temporary).resolve()
        empty_root = workspace / "empty-settings"
        empty_root.mkdir()
        basetemp_root = requested_temp or workspace / "pytest"
        if requested_temp is not None:
            # pytest deletes basetemp on first use. Reserve a NEW path here so
            # no caller-owned directory can ever be passed to that cleanup.
            try:
                requested_temp.mkdir(parents=True, exist_ok=False)
            except OSError:
                print(
                    "Offline launcher: cannot reserve a new --basetemp directory",
                    file=sys.stderr,
                )
                return 4
        try:
            # pytest creates --basetemp without parents; the per-domain leaf
            # below must stay nonexistent, the root is launcher-owned.
            basetemp_root.mkdir(parents=True, exist_ok=True)
        except OSError:
            print(
                "Offline launcher: cannot prepare the per-domain basetemp root",
                file=sys.stderr,
            )
            return 4
        evidence_dir = workspace / "evidence"
        evidence_dir.mkdir()
        print(f"Offline evidence: {evidence_dir}", flush=True)
        try:
            for index, domain in enumerate(domains):
                if not domain.arguments and not domain.launch_anyway:
                    domain.state = "empty"
                    continue
                if maxfail is not None and failures_so_far >= maxfail:
                    domain.state = "unrun"
                    continue
                state, stdout, stderr, was_interrupted = _launch_domain(
                    domain, workspace, empty_root, basetemp_root
                )
                if state == "basetemp_error":
                    domain.state = "basetemp_error"
                    continue
                if state == "spawn_error":
                    domain.state = "spawn_error"
                    continue
                domain.state = state
                stats = domain.stats or {}
                failures_so_far += stats.get("failed", 0) + stats.get("errors", 0)
                if domain.exit_code == 1 and not domain.stats:
                    failures_so_far += 1
                if was_interrupted:
                    interrupted = True
                    for remaining in domains[index + 1:]:
                        if remaining.state == "planned":
                            remaining.state = "unrun"
                    break
        except KeyboardInterrupt:
            interrupted = True
            for domain in domains:
                if domain.state == "planned":
                    domain.state = "unrun"

    overall = _overall_exit_code(domains, interrupted)
    _print_summary(domains, interrupted, overall)
    return overall


def main(arguments: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
    try:
        arguments, requested_temp = prepare_arguments(
            list(sys.argv[1:] if arguments is None else arguments)
        )
    except ValueError as exc:
        print(f"Offline launcher: {exc}", file=sys.stderr)
        return 4

    try:
        return _run_isolated(arguments, requested_temp)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
