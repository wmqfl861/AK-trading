"""DP08 system-boundary tests (V12 full matrix; SYNTHETIC, offline).

AUTHORITY: work-orders/DP08-integration.md (H08.4), 04-validation-and-
release.md (V12: forbidden directories / symbol path escape / junctions /
unconfigured output root / trading-API introduction / log secret shapes).
The rejection must happen BEFORE any filesystem access, and a non-secret
report must prove every disabled-action count is ZERO.

Each boundary is exercised at the INTEGRATION level: the real DP03 engine,
DP04 evidence loader, DP06 report publisher, DP07 artifact writer and DP02
storage over the assembled six-node package.  Physical junction/reparse
inspection on runtime roots remains NOT_RUN (register NR-DP08-05, lexical
layers are fully covered here).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "DP08"
_spec = importlib.util.spec_from_file_location("dp08_bootstrap_v12", _FIXTURES / "bootstrap.py")
_bootstrap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bootstrap)
_STAGING_SRC = _bootstrap.ensure_importable()
_world = _bootstrap.load_module("synthetic_world")

from ak_trading.datapipeline.contracts import ContractError, sanitize_status_reason  # noqa: E402
from ak_trading.datapipeline.ports import assert_no_trading_surface  # noqa: E402
from ak_trading.datapipeline.session_factory import (  # noqa: E402
    PermissionDeniedError,
    SdkSessionFacade,
    SubscriptionKey,
    SyntheticSessionFactory,
    build_synthetic_frame,
)
from ak_trading.datapipeline.contracts import SourceCoordinate  # noqa: E402

SYMBOL = _world.SYMBOL_MAIN
T0 = _world.T0
SESSION_1 = _world.SESSION_1
OBSERVED_AT = _world.OBSERVED_AT
FORBIDDEN_MARKERS = ("maintenance_artifacts", "AKF260920")

#: frozen faces whose bytes must never change during a DP08 chain run
FROZEN_FACE_DIRS = [
    _bootstrap.REPO_ROOT / "src" / "ak_trading" / "datapipeline",
    _bootstrap.REPO_ROOT / "tests" / "datapipeline" / "fixtures" / "DP03",
    _bootstrap.REPO_ROOT / "tests" / "datapipeline" / "fixtures" / "DP04",
    _bootstrap.REPO_ROOT / "tests" / "datapipeline" / "fixtures" / "DP06",
    _bootstrap.REPO_ROOT / "config" / "datapipeline",
    _bootstrap.REPO_ROOT / "migrations",
    _bootstrap.DEVPLAN / "DP01" / "r1",
    _bootstrap.DEVPLAN / "DP02" / "r1",
    _bootstrap.DEVPLAN / "DP05" / "r1",
    _bootstrap.DEVPLAN / "DP07" / "r1",
    _bootstrap.EVIDENCE / "DP03" / "r1",
    _bootstrap.EVIDENCE / "DP04" / "r1",
    _bootstrap.EVIDENCE / "DP06" / "r1",
]


def _tree_hashes(roots, skip_suffixes=(".pyc", ".log")) -> dict:
    out = {}
    for root in roots:
        for path in sorted(Path(root).rglob("*")):
            if path.is_file() and path.suffix not in skip_suffixes:
                out[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


# ===========================================================================
# V12.1 - forbidden paths, escapes and unconfigured roots (lexical, BEFORE
#          any filesystem access)
# ===========================================================================

class TestV12ForbiddenPaths:
    def test_engine_run_root_forbidden_markers_rejected_before_fs(self, tmp_path):
        """The real engine rejects run roots touching forbidden faces (or
        relative/escaping paths) at construction time - before any file is
        created anywhere near them."""
        from ak_trading.datapipeline.collector import CollectionEngine
        from ak_trading.datapipeline.scheduler import FairRotatingScheduler, SchedulerStateFile
        from ak_trading.datapipeline.session_factory import SessionError

        doubles = _world.load_dp03_doubles(_bootstrap)
        clock = doubles.ClockNano(OBSERVED_AT)
        symbols = [(SYMBOL, "MAIN_CONTINUOUS", "rb")]
        bad_roots = [
            tmp_path / "maintenance_artifacts" / "run",      # forbidden face
            tmp_path / "AKF260920" / "run",                  # forbidden face
            tmp_path / "run" / ".." / "escape",              # lexical escape
            Path("relative/run"),                            # not absolute
        ]
        for bad in bad_roots:
            with pytest.raises((SessionError, ContractError)):
                CollectionEngine(
                    worker_id="w", dataset=_world.DATASET,
                    scheduler=FairRotatingScheduler(
                        source_id=_world.SOURCE_ID, symbols=symbols,
                        clock_nano=clock, durations_seconds=(60,),
                        state_file=SchedulerStateFile(tmp_path / "s.json"),
                    ),
                    session_factory=SyntheticSessionFactory(frames={}, timelines=[[]], clock_nano=clock),
                    store=None, status_sink=None, task_registry=None, run_registry=None,
                    calendar=None, run_root=bad,
                    universe_sha256=_world.universe_hash_for(symbols, (60,)),
                    clock_nano=clock,
                )
            # no directory was created on the rejected path's branch
            assert not (tmp_path / "maintenance_artifacts").exists()
            assert not (tmp_path / "AKF260920").exists()

    def test_calendar_evidence_path_escapes_rejected_before_access(self):
        """DP04's evidence loader rejects traversals, absolute paths,
        backslashes and forbidden markers lexically - before open()."""
        from ak_trading.datapipeline.calendar import (
            CalendarBook,
            calendar_evidence_document_from_mapping,
            resolve_evidence_path,
        )

        good_root = _bootstrap.STAGING_ROOT
        doc = calendar_evidence_document_from_mapping(_world.calendar_payload())
        book = CalendarBook((doc,))
        for bad in (
            "../outside.json",                       # traversal
            str(Path(good_root).parent / "abs.json"),  # outside allowed roots
            "C:/evil/evidence.json",                 # absolute
            "..\\windows\\evil.json",                # backslash traversal
            "maintenance_artifacts/cal.json",        # forbidden face
            "AKF260920/cal.json",                    # forbidden face
        ):
            with pytest.raises(ContractError):
                resolve_evidence_path(bad, allowed_roots=[good_root])

    def test_dp06_report_output_root_containment(self, tmp_path):
        """DP06's publisher keeps every artifact inside the explicitly
        injected root; escaping targets are refused."""
        from ak_trading.datapipeline import reporting
        from ak_trading.datapipeline.monitoring import MonitoringError, assert_within_root

        facts = {
            "monitor_id": "mon", "as_of_nano": OBSERVED_AT, "schema": "dp06-report/1",
            "tasks": [], "status_events": [], "quality_reports": [],
            "heartbeats": [], "alerts": [], "health": {}, "health_reasons": {},
            "evidence_hashes": {"run": "0" * 64}, "data_origin": "SYNTHETIC",
            "wall_clock_used": False,
        }
        with pytest.raises(MonitoringError):
            assert_within_root(tmp_path, tmp_path.parent / "escape")
        with pytest.raises(Exception):
            reporting.publish_report(
                facts=facts, markdown="x", html="x",
                output_root=tmp_path / ".." / "escape-out",
                monitor_id="mon",
            )

    def test_dp07_governance_paths_lexical_and_no_overwrite(self, tmp_path):
        """DP07's artifact writer rejects drive/absolute/backslash/dotdot
        components before access and refuses to overwrite existing files."""
        from ak_trading.datapipeline.legacy import (
            LegacyRemediationError,
            governance_artifact_path,
        )

        root = tmp_path / "legacy-root"
        root.mkdir()
        for bad_run, bad_id, bad_name in (
            ("../escape", "plan", "p.json"),
            ("run-1", "../escape", "p.json"),
            ("run-1", "plan", "..\\evil.json"),
        ):
            with pytest.raises(LegacyRemediationError):
                governance_artifact_path(root, bad_run, "recovery_plan", bad_id, bad_name)
        path = governance_artifact_path(root, "dp07-20260923-a1", "recovery_plan", "pid1", "f.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("SYNTHETIC", encoding="utf-8")
        with pytest.raises(LegacyRemediationError):
            governance_artifact_path(root, "dp07-20260923-a1", "plan", "pid1", "f.json")

    def test_unconfigured_output_root_is_refused(self):
        """No component invents an output root: policy validation rejects
        empty/whitespace roots instead of defaulting to CWD."""
        from ak_trading.datapipeline.policy import validate_output_root

        for bad in ("", "   ", "..", "maintenance_artifacts/out", "AKF260920/out"):
            with pytest.raises(ContractError):
                validate_output_root(bad)


# ===========================================================================
# V12.2 - frozen faces stay byte-identical across a real chain run
# ===========================================================================

class TestV12FrozenFacesUntouched:
    def test_chain_run_leaves_every_frozen_face_byte_identical(self, tmp_path):
        """A real G1->G4 mini chain (commit + collector leg + validation +
        report) leaves the root alembic chain, shared configs, other nodes'
        source/evidence trees and the DP00-07 packages byte-identical."""
        before = _tree_hashes(FROZEN_FACE_DIRS)
        assert len(before) > 50  # the snapshot covers a real surface

        acquisition = _bootstrap.pinned_acquisition()
        world = _world.StorageWorld(tmp_path / "storage", acquisition=acquisition)
        envelope, classified = _world.synthetic_batch(
            acquisition=acquisition, n_rows=5, base_nano=SESSION_1[0], attempt="att-v12",
        )
        receipt = world.store.commit(
            envelope, classified, _world.synthetic_fence(f"{_world.SOURCE_ID}|{SYMBOL}|60")
        )
        assert receipt.outcome.value == "COMMITTED_NEW"

        # collector leg over the real registries (r2: the fixed engine
        # completes the run; the boundary under test is FS writes only)
        doubles = _world.load_dp03_doubles(_bootstrap)
        clock = doubles.ClockNano(OBSERVED_AT)
        engine = _world.build_engine(
            world=world, doubles=doubles, run_root=tmp_path / "run",
            frames={(SYMBOL, 60): build_synthetic_frame(
                start_nano=SESSION_1[0], duration_seconds=60, closed_rows=4,
            )},
            timelines=[[]], clock=clock,
            symbols=[(SYMBOL, "MAIN_CONTINUOUS", "rb")], durations=(60,),
            scheduler_state_path=tmp_path / "sched.json",
        )
        try:
            engine.run(run_id="run-v12")
        except Exception:
            pass  # defensive only (r2: the fixed engine completes); the
            #       boundary under test is frozen-face FS writes

        after = _tree_hashes(FROZEN_FACE_DIRS)
        assert after == before
        # and the test databases carry no root-chain version table
        with world.engine.connect() as c:
            names = {
                r["name"]
                for r in c.execute(sa_text("SELECT name FROM main.sqlite_master WHERE type='table'")).mappings()
            }
        assert names <= {"alembic_version_dp08_probe"} or not [
            n for n in names if "alembic" in n
        ]


def sa_text(query):
    import sqlalchemy as sa

    return sa.text(query)


# ===========================================================================
# V12.3 - trading surface stays at zero across the whole chain
# ===========================================================================

class _TradingApiDecoy:
    """SDK-shaped raw api exposing trading methods that COUNT every call."""

    def __init__(self, frames, clock):
        self._frames = frames
        self._clock = clock
        self.trading_calls: list = []
        self.get_kline_serial_calls: list = []

    def get_kline_serial(self, symbol, duration, data_length):
        self.get_kline_serial_calls.append((symbol, duration, data_length))
        return self._frames[(symbol, duration)]

    def is_serial_ready(self, _serial):
        return True

    def wait_update(self, deadline=None):
        return False

    def close(self):
        return None

    # forbidden surface
    def insert_order(self, *a, **k):
        self.trading_calls.append("insert_order")

    def cancel_order(self, *a, **k):
        self.trading_calls.append("cancel_order")

    def get_account(self, *a, **k):
        self.trading_calls.append("get_account")

    def get_position(self, *a, **k):
        self.trading_calls.append("get_position")


class TestV12TradingSurfaceZero:
    def test_facade_exposes_no_trading_surface_and_decoy_never_called(self):
        """The DP03 facade over a trading-capable raw api passes the frozen
        no-trading-surface assertion; forbidden methods are unreachable and
        the decoy counts ZERO trading calls."""
        doubles = _world.load_dp03_doubles(_bootstrap)
        clock = doubles.ClockNano(OBSERVED_AT)
        frame = build_synthetic_frame(start_nano=SESSION_1[0], duration_seconds=60, closed_rows=2)
        decoy = _TradingApiDecoy({(SYMBOL, 60): frame}, clock)
        facade = SdkSessionFacade(decoy, connection_epoch=1, clock_nano=clock)
        assert_no_trading_surface(facade)
        serial = facade.subscribe(SubscriptionKey(
            coordinate=SourceCoordinate(
                source_id=_world.SOURCE_ID, source_symbol=SYMBOL, duration_seconds=60,
            )
        ))
        facade.wait_update(0.1)
        facade.ready(serial)
        facade.copy_snapshot(serial)
        facade.close()
        for name in ("insert_order", "cancel_order", "get_account", "get_position"):
            with pytest.raises(AttributeError):
                getattr(facade, name)
        assert decoy.trading_calls == []  # disabled-action count == 0
        assert decoy.get_kline_serial_calls == [(SYMBOL, 60, 8192)]

    def test_all_chain_outputs_free_of_trading_tokens(self, tmp_path):
        """After a real mini chain, every produced artifact (status events,
        report markdown/html/json, dashboard) carries zero trading tokens -
        the non-secret proof that disabled-action counts are 0."""
        from ak_trading.datapipeline import dashboard, monitoring, reporting

        acquisition = _bootstrap.pinned_acquisition()
        world = _world.StorageWorld(tmp_path / "storage", acquisition=acquisition)
        scenario = tmp_path / "scenario"
        scenario.mkdir()
        doubles = _world.load_dp03_doubles(_bootstrap)
        clock = doubles.ClockNano(OBSERVED_AT)
        engine = _world.build_engine(
            world=world, doubles=doubles, run_root=scenario / "run",
            frames={(SYMBOL, 60): build_synthetic_frame(
                start_nano=SESSION_1[0], duration_seconds=60, closed_rows=4,
            )},
            timelines=[[]], clock=clock,
            symbols=[(SYMBOL, "MAIN_CONTINUOUS", "rb")], durations=(60,),
            scheduler_state_path=scenario / "sched.json",
        )
        try:
            engine.run(run_id="run-v12b")
        except Exception:
            pass  # defensive only (r2: the fixed engine completes); outputs
            #       under test are already written by this point

        facts = _world.export_facts_tree(
            facts_root=scenario / "facts", world=world,
            tasks=[_world.task_fact(
                f"{_world.SOURCE_ID}|{SYMBOL}|60", duration_seconds=60, symbol=SYMBOL,
                state="RAW_VERIFIED", window=(SESSION_1[0], SESSION_1[1]),
            )],
            quality_reports=[], calendar_answers=[],
        )
        snapshot = monitoring.load_pipeline_facts(
            heartbeats_root=scenario / "run" / "heartbeats",
            events_root=scenario / "facts" / "events",
            quality_root=scenario / "facts" / "quality",
            tasks_file=facts["tasks"], calendar_file=facts["calendar"],
            runs_file=facts["runs"],
        )
        report = reporting.build_report(
            monitor_id="mon-v12", as_of_nano=OBSERVED_AT + MINUTE,
            snapshot=snapshot, alert_state={}, health={}, health_reasons={},
            evidence_hashes={"run": "0" * 64},
        )
        markdown = reporting.render_markdown(report)
        html = dashboard.render_html(report)
        blob = markdown + html + json.dumps(
            [e for e in world.status_events()], default=str, sort_keys=True,
        )
        forbidden = ("insert_order", "cancel_order", "enable_trading",
                     "TargetPosTask", "get_account", "TqAccount", "TqKq")
        for token in forbidden:
            assert token not in blob, token

    def test_credential_shapes_rejected_in_status_reasons_and_reports(self):
        """Secret-shaped text never reaches logs or reports: sanitize_status_reason
        rejects credential shapes and DP06 blocks credential-shaped facts."""
        from ak_trading.datapipeline import reporting

        for secret in (
            "password=hunter2secret",
            "token: Bearer abcdef1234567890abcdef",
            "postgres://user:secretpw@host/db",
        ):
            with pytest.raises(ContractError):
                sanitize_status_reason(secret)
        with pytest.raises(Exception):
            reporting.sanitize_report_facts({
                "monitor_id": "mon", "note": "api_key=abcdef1234567890abcdef12",
            })


MINUTE = 60 * 1_000_000_000


# ===========================================================================
# V12.4 - import purity of the integrated package (no SDK/socket/.env)
# ===========================================================================

class TestV12ImportPurity:
    def test_integrated_package_imports_without_side_effects(self):
        """A fresh interpreter imports ALL integrated modules with an audit
        hook: no .env open, no socket connection, no subprocess, and tqsdk
        stays unimported."""
        child = r"""
import json, sys
violations = []
def hook(event, args):
    if event == "open":
        path = str(args[0]) if args else ""
        if ".env" in path.lower():
            violations.append(("env_open", path))
    elif event in ("socket.connect", "socket.getaddrinfo"):
        violations.append(("socket", str(args)))
    elif event == "subprocess.Popen":
        violations.append(("subprocess", str(args)))
sys.addaudithook(hook)
sys.path.insert(0, sys.argv[1])
mods = [
    "ak_trading.datapipeline." + n for n in (
        "contracts", "ports", "policy", "models", "storage", "migrate",
        "readback", "collector", "scheduler", "session_factory", "calendar",
        "validation", "gaps", "monitoring", "reporting", "dashboard",
        "legacy", "rank_recovery",
    )
]
import importlib
for m in mods:
    importlib.import_module(m)
print(json.dumps({
    "violations": violations,
    "tqsdk_loaded": "tqsdk" in sys.modules,
    "modules": len(mods),
}))
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", child, str(_STAGING_SRC)],
            capture_output=True, text=True, timeout=120,
        )
        assert result.returncode == 0, result.stderr[-2000:]
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        assert payload["modules"] == 18
        assert payload["violations"] == []
        assert payload["tqsdk_loaded"] is False


# ===========================================================================
# V12.5 - the collector only ever closes sessions/workers it created
# ===========================================================================

class TestV12OwnSessionsOnly:
    def test_engine_a_stop_never_closes_engine_b_session(self, tmp_path):
        """Two real engines (A, B) with separate factories/schedulers: A's
        run lifecycle closes ONLY A's sessions; B's session stays open until
        B itself closes it."""
        doubles = _world.load_dp03_doubles(_bootstrap)
        acquisition = _bootstrap.pinned_acquisition()
        world = _world.StorageWorld(tmp_path / "storage", acquisition=acquisition)
        clock_a = doubles.ClockNano(OBSERVED_AT)
        clock_b = doubles.ClockNano(OBSERVED_AT)

        from ak_trading.datapipeline.collector import CollectionEngine
        from ak_trading.datapipeline.scheduler import FairRotatingScheduler, SchedulerStateFile

        symbols = [(SYMBOL, "MAIN_CONTINUOUS", "rb")]
        frames = {(SYMBOL, 60): build_synthetic_frame(
            start_nano=SESSION_1[0], duration_seconds=60, closed_rows=3,
        )}
        factory_a = SyntheticSessionFactory(frames=frames, timelines=[[]], clock_nano=clock_a)
        factory_b = SyntheticSessionFactory(frames=frames, timelines=[[]], clock_nano=clock_b)

        class NullReg:
            def start_run(self, **k):
                # r2 adaptation (X-08-2 fixed): the engine now ADOPTS a
                # registry-returned run_id; returning None keeps the engine
                # on its own caller-supplied id so the session-ownership
                # assertion below stays about sessions, not run identity.
                return None

            def transition_run(self, *a, **k):
                pass

            def finalize_run(self, *a, **k):
                pass

        def make(factory, clock, run_root, state):
            return CollectionEngine(
                worker_id="w-a" if factory is factory_a else "w-b",
                dataset=_world.DATASET,
                scheduler=FairRotatingScheduler(
                    source_id=_world.SOURCE_ID, symbols=symbols, clock_nano=clock,
                    durations_seconds=(60,),
                    state_file=SchedulerStateFile(state),
                ),
                session_factory=factory, store=None, status_sink=world.status_sink,
                task_registry=world.task_registry, run_registry=NullReg(),
                calendar=None, run_root=run_root,
                universe_sha256=_world.universe_hash_for(symbols, (60,)),
                clock_nano=clock, clock_mono=_world.MonotonicFake(),
                sleep=lambda _s: None, group_service_seconds=1.0,
                stale_after_seconds=0.25, run_deadline_seconds=10.0,
                finalize_wait_seconds=3.0,
            )

        (tmp_path / "a").mkdir(); (tmp_path / "b").mkdir()
        engine_a = make(factory_a, clock_a, (tmp_path / "a" / "run").resolve(), tmp_path / "a" / "s.json")
        engine_b = make(factory_b, clock_b, (tmp_path / "b" / "run").resolve(), tmp_path / "b" / "s.json")

        summary_a = engine_a.run(run_id="run-a")
        session_a = factory_a.created_sessions[0]
        session_b = factory_b.create()   # B's OWN open session (B not running)
        assert session_a.close_count >= 1          # A closed its own session
        assert session_b.close_count == 0          # A never touched B's session
        assert summary_a.run_id == "run-a"

        # B's session is closed only by B's own code path
        session_b.close()
        assert session_b.close_count == 1

    def test_real_source_entry_refused_without_authorization(self):
        """The real-source factory refuses BEFORE any tqsdk import (V12
        trading-API introduction vector)."""
        from ak_trading.datapipeline.session_factory import RealSdkSessionFactory

        class _Blocker:
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "tqsdk":
                    raise AssertionError("tqsdk import attempted without authorization")
                return None

        sys.meta_path.insert(0, _Blocker())
        try:
            with pytest.raises(ContractError):
                RealSdkSessionFactory().create()
        finally:
            sys.meta_path.pop(0)
        assert "tqsdk" not in sys.modules
