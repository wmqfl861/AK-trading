"""NF22 datapipeline-domain SYNTHETIC workloads (r3: two cases, one file).

Case 1 - ``datapipeline/frame_synthetic`` (H22 FRAME_SYNTHETIC, r1 interim
form, UNCHANGED below): offline kline-frame construction + validation.
Case 2 - ``datapipeline/collect_store_sqlite`` (H22 COLLECT_STORE_SQLITE,
r3 persistence-matched implementation, appended at the bottom of this
file): SYNTHETIC frames through the REAL DP store commit path + pinned
acquisition closure; r3 (Q51 D01/D02) configures EVERY actual DBAPI
connection to journal_mode=DELETE + synchronous=OFF on both attached
file DBs, with per-connection six-value effective readback facts (main
stays native and is read back only).

``tools/benchmarks/cases/datapipeline.json``; tier ladder frozen in
``tools/benchmarks/fixtures/datapipeline/frame_tiers.json``.

What this r1 case IS:

* pure offline, SYNTHETIC, standard-library-only measurement of synthetic
  kline-frame CONSTRUCTION + VALIDATION cost across three pre-registered
  row tiers (small/medium/large) cycled deterministically;
* one harness-timed ``operation()`` per frame; ``units`` = accepted input
  rows of that frame (padding/invalid/duplicate rows are counted and
  reported, never counted as accepted - H22 denominator rule);
* NF22-owned measured code (logical module
  ``benchmark.datapipeline.frame_synthetic``), because the DP-owner release
  source-map handover is PENDING.

What this case is NOT (H22 honesty boundaries):

* it does NOT measure any ``ak_trading.datapipeline`` production module
  (that binding waits for the DP owner's approved source-map; the column
  contract and per-tier composition merely mirror the documented DP
  synthetic-frame shape so the eventual DP-bound case is comparable in
  form);
* it is NOT a database throughput case (generation cost and commit cost
  are deliberately separated; the commit-cost leg lives in the
  COLLECT_STORE_SQLITE r3 section appended below in this same file);
* SQLite results never imply PostgreSQL throughput.

Determinism: every row value derives from the frozen seed and integer
indices (documented formulas in the tier fixture); no clocks, no RNG, no
I/O beyond the read-only fixture load in ``setup``.  The digest covers the
per-row classification stream, so any dropped, leaked or misclassified row
changes the digest.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[3]
FRAME_TIERS_REL = "tools/benchmarks/fixtures/datapipeline/frame_tiers.json"
_NANOSECOND = 1_000_000_000

# Classification markers (frozen; part of the digest alphabet).
CLS_ACCEPTED = "a"
CLS_PADDING = "p"
CLS_INVALID = "i"
CLS_DUPLICATE = "d"

_REQUIRED_TIER_KEYS = (
    "tier",
    "duration_seconds",
    "padding_rows",
    "closed_rows",
    "live_rows",
    "invalid_rows",
    "duplicate_rows",
    "expected",
)


def load_tier_ladder() -> Dict[str, Any]:
    """Load and structurally validate the frozen tier-ladder fixture.

    Raises ``RuntimeError`` on any drift so the harness records an honest
    ERROR instead of silently measuring a mutated ladder.
    """
    path = _REPO_ROOT / FRAME_TIERS_REL
    if not path.is_file():
        raise RuntimeError("frame tier fixture missing: %s" % path)
    with open(path, "r", encoding="utf-8") as handle:
        ladder = json.load(handle)
    tiers = ladder.get("tiers")
    if not isinstance(tiers, list) or len(tiers) != 3:
        raise RuntimeError("tier ladder must have exactly 3 tiers (H22)")
    previous_total = -1
    for tier in tiers:
        for key in _REQUIRED_TIER_KEYS:
            if key not in tier:
                raise RuntimeError("tier %r missing key %r" % (tier.get("tier"), key))
        composition = (
            int(tier["padding_rows"])
            + int(tier["closed_rows"])
            + int(tier["live_rows"])
            + int(tier["duplicate_rows"])
        )
        expected = tier["expected"]
        if int(expected["total_rows"]) != composition:
            raise RuntimeError(
                "tier %r total_rows %s != composition %d"
                % (tier["tier"], expected["total_rows"], composition)
            )
        if composition <= previous_total:
            raise RuntimeError("tier %r does not grow the workload (H22)" % tier["tier"])
        previous_total = composition
    return ladder


def _fmt(value: float) -> str:
    """Deterministic float formatting for digest bytes."""
    return repr(float(value))


def classify_row(row: Dict[str, float], seen_keys: set) -> str:
    """Frozen per-row classifier (rule order is part of case identity).

    Rule order: NaN datetime -> padding; non-finite fields -> invalid;
    OHLC sanity -> invalid; duplicate accepted key -> duplicate; else
    accepted.  Only ACCEPTED rows register their key in ``seen_keys``.
    """
    dt = row["datetime"]
    if math.isnan(dt):
        return CLS_PADDING
    for column in ("open", "high", "low", "close", "volume", "open_oi", "close_oi"):
        if not math.isfinite(row[column]):
            return CLS_INVALID
    o, h, l, c = row["open"], row["high"], row["low"], row["close"]
    if h < l or h < max(o, c) or l > min(o, c):
        return CLS_INVALID
    if dt in seen_keys:
        return CLS_DUPLICATE
    return CLS_ACCEPTED


class FrameSyntheticWorkload:
    """SYNTHETIC kline-frame construction + validation (H22 FRAME_SYNTHETIC)."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.seed = int(ctx.seed)
        self.ladder: Dict[str, Any] = {}
        self.tiers: List[Dict[str, Any]] = []
        self.ladder_sha256 = ""
        self.operations = 0
        self.tier_frames: Dict[str, int] = {}
        self.class_totals: Dict[str, int] = {}
        self.units_total = 0
        self._acc = hashlib.sha256()

    # -- setup ---------------------------------------------------------------

    def setup(self, ctx: Any) -> None:
        self.ladder = load_tier_ladder()
        self.tiers = list(self.ladder["tiers"])
        self.ladder_sha256 = hashlib.sha256(
            (_REPO_ROOT / FRAME_TIERS_REL).read_bytes()
        ).hexdigest()
        self.operations = 0
        self.tier_frames = {str(t["tier"]): 0 for t in self.tiers}
        self.class_totals = {CLS_ACCEPTED: 0, CLS_PADDING: 0, CLS_INVALID: 0, CLS_DUPLICATE: 0}
        self.units_total = 0
        self._acc = hashlib.sha256()

    # -- frame construction (the measured work) ------------------------------

    def _build_frame_columns(self, tier: Dict[str, Any]) -> Dict[str, List[float]]:
        """Construct the SDK-shaped columnar frame for one tier.

        Layout mirrors the documented DP synthetic frame: NaN padding head,
        closed bars then live (in-formation) bars, deliberately-broken OHLC
        rows at the head of the closed range, duplicate-key rows appended
        at the tail.  All values derive from the frozen seed and indices.
        """
        seed = self.seed
        start_nano = 1_700_000_000_000_000_000 + (seed % 1_000_000_000)
        price = 4000.0 + (seed % 97) * 0.25
        duration = int(tier["duration_seconds"])
        padding_rows = int(tier["padding_rows"])
        closed_rows = int(tier["closed_rows"])
        live_rows = int(tier["live_rows"])
        invalid_rows = int(tier["invalid_rows"])
        duplicate_rows = int(tier["duplicate_rows"])

        nan = float("nan")
        columns: Dict[str, List[float]] = {
            "datetime": [nan] * padding_rows,
            "open": [nan] * padding_rows,
            "high": [nan] * padding_rows,
            "low": [nan] * padding_rows,
            "close": [nan] * padding_rows,
            "volume": [nan] * padding_rows,
            "open_oi": [nan] * padding_rows,
            "close_oi": [nan] * padding_rows,
        }
        total_bars = closed_rows + live_rows
        for index in range(total_bars):
            step = 1.0 + index * 0.5
            if index < invalid_rows:
                # Deliberately broken OHLC (high below low) - INVALID probes.
                columns["datetime"].append(float(start_nano + index * duration * _NANOSECOND))
                columns["open"].append(price + step)
                columns["high"].append(price + step - 5.0)
                columns["low"].append(price + step + 5.0)
                columns["close"].append(price + step)
            else:
                columns["datetime"].append(float(start_nano + index * duration * _NANOSECOND))
                columns["open"].append(price + step)
                columns["high"].append(price + step + 3.0)
                columns["low"].append(price + step - 3.0)
                columns["close"].append(price + step + 1.0)
            columns["volume"].append(100.0 + index)
            columns["open_oi"].append(5000.0 + index)
            columns["close_oi"].append(5010.0 + index)
        if closed_rows > 0:
            duplicate_key = float(
                start_nano + (closed_rows - 1) * duration * _NANOSECOND
            )
            for _ in range(duplicate_rows):
                # Same-key duplicates of the LAST closed bar (valid OHLC).
                columns["datetime"].append(duplicate_key)
                columns["open"].append(price)
                columns["high"].append(price + 1.0)
                columns["low"].append(price - 1.0)
                columns["close"].append(price)
                columns["volume"].append(7.0)
                columns["open_oi"].append(10.0)
                columns["close_oi"].append(11.0)
        return columns

    # -- measured operation ---------------------------------------------------

    def operation(self, ctx: Any) -> Dict[str, Any]:
        tier = self.tiers[self.operations % len(self.tiers)]
        self.operations += 1
        columns = self._build_frame_columns(tier)
        seen_keys: set = set()
        accepted = 0
        acc = self._acc
        names = ("datetime", "open", "high", "low", "close", "volume", "open_oi", "close_oi")
        total = len(columns["datetime"])
        for row_index in range(total):
            row = {name: columns[name][row_index] for name in names}
            marker = classify_row(row, seen_keys)
            if marker == CLS_ACCEPTED:
                seen_keys.add(row["datetime"])
                accepted += 1
            self.class_totals[marker] += 1
            acc.update(
                ("%s|%s|%s;" % (marker, _fmt(row["datetime"]), _fmt(row["close"]))).encode("utf-8")
            )
        self.tier_frames[str(tier["tier"])] += 1
        self.units_total += accepted
        return {"ok": True, "units": accepted}

    # -- verification (frozen rules, H22 counterexamples) ---------------------

    def _expected_totals(self) -> Dict[str, int]:
        """Expected class totals for the tiers actually visited (full cycles
        plus the leading remainder of the next cycle), from the frozen
        ladder's expectation table."""
        totals = {CLS_ACCEPTED: 0, CLS_PADDING: 0, CLS_INVALID: 0, CLS_DUPLICATE: 0}
        full_cycles, remainder = divmod(self.operations, len(self.tiers))
        visits = list(self.tiers) * full_cycles + self.tiers[:remainder]
        for tier in visits:
            expected = tier["expected"]
            totals[CLS_ACCEPTED] += int(expected["accepted_rows"])
            totals[CLS_PADDING] += int(expected["rejected_padding"])
            totals[CLS_INVALID] += int(expected["rejected_invalid"])
            totals[CLS_DUPLICATE] += int(expected["duplicates"])
        return totals

    def verify(self, ctx: Any) -> Dict[str, Any]:
        fail_reasons: List[str] = []
        if self.operations <= 0:
            fail_reasons.append("no operations executed")
        if self.units_total <= 0:
            fail_reasons.append("no accepted rows recorded")
        expected = self._expected_totals()
        for cls, label in (
            (CLS_ACCEPTED, "accepted"),
            (CLS_PADDING, "padding"),
            (CLS_INVALID, "invalid"),
            (CLS_DUPLICATE, "duplicates"),
        ):
            actual = self.class_totals[cls]
            if actual != expected[cls]:
                fail_reasons.append(
                    "ROW_IDENTITY_MISMATCH %s: classified=%d expected=%d"
                    % (label, actual, expected[cls])
                )
        summary = {
            "operations": self.operations,
            "tier_frames": dict(self.tier_frames),
            "accepted_rows": self.class_totals[CLS_ACCEPTED],
            "rejected_padding_rows": self.class_totals[CLS_PADDING],
            "rejected_invalid_rows": self.class_totals[CLS_INVALID],
            "duplicate_rows": self.class_totals[CLS_DUPLICATE],
            "units_total": self.units_total,
            "seed": self.seed,
            "ladder_sha256": self.ladder_sha256,
        }
        return {
            "status": "FAIL" if fail_reasons else "PASS",
            "digest": self._acc.hexdigest(),
            "summary": summary,
            "fail_reasons": fail_reasons,
        }

    def teardown(self, ctx: Any) -> None:
        pass  # nothing to release; the harness owns tmp cleanup


def build(ctx: Any) -> FrameSyntheticWorkload:
    return FrameSyntheticWorkload(ctx)


# ===========================================================================
# COLLECT_STORE_SQLITE (r3 persistence-matched implementation; Q31-W04 r2 /
# GAP-05 form-b origin, Q51 D01/D02 configuration)
# ===========================================================================
#
# Case identity: ``datapipeline/collect_store_sqlite`` (manifest
# ``tools/benchmarks/cases/datapipeline/collect_store_sqlite.json``).
#
# What this r2 case IS (the r1 deliberately-absent stub, now real):
#
# * SYNTHETIC frame data driven through the REAL reviewed DP production
#   commit path: ``ak_trading.datapipeline.storage.SqlPipelineStore.commit``
#   (dp08-r3 RELEASE_MANIFEST bytes) over a per-trial file-backed SQLite
#   double (``models.attach_sqlite_schemas`` + ``adopt_or_create_*``), with
#   the pinned cross-domain ``ak_trading.acquisition.storage`` 3-symbol
#   closure (module resolve + ``RawSnapshotMeta`` + ``build_batch_plan`` +
#   ``ingest_batch``; datapipeline storage.py:463/652/672/708) executing on
#   every commit - WORKTREE_SNAPSHOT grade rows 13-15 of the r2 source map
#   (GAP-05; release-grade handoff still pending, so the evidence grade of
#   this leg is NOT upgraded and ``eligible_for_baseline`` stays false).
# * one harness-timed ``operation()`` per synthetic frame = staged commit /
#   idempotent replay / published-only readback (frozen design section 4);
#   ``units`` = accepted input rows = VALID (closed + provisional) rows of
#   that frame; padding/invalid rows are rejected by the real store and are
#   never counted as accepted.
# * deterministic: every value derives from the frozen seed and integer
#   indices; the business clock is an injected integer-nanosecond counter
#   (the performance clock stays harness-owned ``perf_counter_ns``); no
#   wall time, no RNG, no network, no production DB.
#
# What this case is NOT:
#
# * NOT a PostgreSQL throughput claim; NOT real PIT data (SYNTHETIC only);
# * NOT measuring ak_trading.datapipeline.{policy,scheduler,session_factory,
#   collector} (no engine run loop is driven - the manifest's
#   measured_modules lists exactly the modules the commit path touches);
# * NOT a release-grade PASS for the acquisition dependency: rows 13-15 are
#   worktree-snapshot facts pinned by hash, and the OPT-04 open finding
#   against ``export_existing_versions`` (outside the three consumed
#   symbols, same file) is disclosed, not scoped away.
#
# Fail-closed pin rule: the production modules are imported ONLY from the
# run root whose files the C20 runner has already hash-verified against the
# source map (verify_source_map_hashes runs before this module loads).  The
# import helper below mutates NO sys.path entry: it pre-registers the
# ``ak_trading`` parent package from its pinned file (explicit
# submodule_search_locations), purges any pre-loaded ``ak_trading`` from an
# outer environment, and asserts every loaded module's file lives under the
# run root (SOURCE_PIN_VIOLATION otherwise; this also defeats editable-
# install meta-path finders that hijack the top-level name).  A missing
# acquisition file therefore surfaces as SOURCE_MISSING at the runner's map
# gate before any import.

COLLECT_SYNTHETIC_MARKER = "SYNTHETIC-NF22-COLLECT-STORE-BENCH"
COLLECT_DATASET = "futures_kline"
COLLECT_SOURCE_ID = "tqsdk-free-realtime-get-kline-serial"
COLLECT_SYMBOL = "KQ.m@SHFE.rb"
COLLECT_DURATION = 60
COLLECT_TRADING_DAY = "2026-09-21"
#: T0 = 2026-09-21T01:30:00Z (same SYNTHETIC instant family as the DP08
#: fixture world; a bare integer constant, no clock is read).
COLLECT_T0_NANO = 1_789_930_600_000_000_000
COLLECT_WINDOW_STRIDE_NANO = 2 * 86_400_000_000_000
COLLECT_READBACK_PAGE = 1000

#: Frozen per-operation frame composition ladder (cycled by op index):
#: (shape name, padding rows, invalid rows, closed rows, provisional rows).
#: VALID = closed + provisional; accepted units = VALID rows per op.
COLLECT_SHAPES: tuple = (
    ("collect-s", 4, 2, 24, 3),
    ("collect-m", 6, 3, 41, 5),
    ("collect-l", 2, 1, 62, 2),
)

#: r3 persistence-matched per-connection configuration targets (Q51 D01):
#: both attached file DBs, explicit journal_mode=DELETE + synchronous=OFF,
#: set on EVERY actual DBAPI connection after the production ATTACH
#: listener ran and before that connection can take part in any measured
#: transaction.  ``main`` is never written; its native values are read back
#: per connection.  No checkpoint PRAGMA and no other setting PRAGMA is
#: issued anywhere in this workload (mechanically enforced by the dedicated
#: regression test; the only PRAGMA reads are journal_mode / synchronous /
#: database_list).
COLLECT_FILE_DB_PRAGMA_TARGETS: tuple = (
    ("acquisition_w2", "journal_mode", "DELETE"),
    ("acquisition_w2", "synchronous", "OFF"),
    ("datapipeline_v1", "journal_mode", "DELETE"),
    ("datapipeline_v1", "synchronous", "OFF"),
)

#: Six-value readback scope: journal_mode + synchronous for main and both
#: attached file DBs on every recorded connection (C6 facts).
COLLECT_DB_SCHEMAS: tuple = ("main", "acquisition_w2", "datapipeline_v1")

#: canonical synchronous normalization (readbacks may return 0..3 or words).
COLLECT_SYNC_CANONICAL: Dict[str, str] = {
    "0": "0", "off": "0",
    "1": "1", "normal": "1",
    "2": "2", "full": "2",
    "3": "3", "extra": "3",
}

_PINNED_PRODUCTION_MODULES = (
    "ak_trading",
    "ak_trading.acquisition",
    "ak_trading.acquisition.contracts",
    "ak_trading.acquisition.storage",
    "ak_trading.datapipeline",
    "ak_trading.datapipeline.contracts",
    "ak_trading.datapipeline.ports",
    "ak_trading.datapipeline.models",
    "ak_trading.datapipeline.readback",
    "ak_trading.datapipeline.storage",
)


def _import_pinned_production_modules(run_root: Path) -> Dict[str, Any]:
    """Import the measured production modules from the hash-verified run
    root ONLY (see the fail-closed pin rule above).

    No ``sys.path`` mutation happens at all: the ``ak_trading`` parent
    package is loaded from its pinned file with an explicit
    ``submodule_search_locations`` and pre-registered in ``sys.modules``,
    which also defeats any site-installed editable-install meta-path finder
    that would otherwise resolve ``ak_trading`` to an outer worktree (that
    hijack was observed live and is exactly what the pin assertion below
    fails closed on)."""
    import importlib
    import importlib.util
    import sys

    for name in [
        name
        for name in list(sys.modules)
        if name == "ak_trading" or name.startswith("ak_trading.")
    ]:
        del sys.modules[name]
    resolved_root = run_root.resolve()
    package_dir = resolved_root / "src" / "ak_trading"
    spec = importlib.util.spec_from_file_location(
        "ak_trading",
        package_dir / "__init__.py",
        submodule_search_locations=[str(package_dir)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot build import spec for the pinned ak_trading package")
    package = importlib.util.module_from_spec(spec)
    sys.modules["ak_trading"] = package
    spec.loader.exec_module(package)
    loaded: Dict[str, Any] = {"ak_trading": package}
    for name in _PINNED_PRODUCTION_MODULES[1:]:
        module = importlib.import_module(name)
        module_path = Path(module.__file__).resolve()
        if resolved_root not in module_path.parents:
            raise RuntimeError(
                "SOURCE_PIN_VIOLATION: %s resolved to %s, outside the pinned "
                "run root %s (fail-closed; no outer ak_trading may be measured)"
                % (name, module_path, resolved_root)
            )
        loaded[name] = module
    package_path = Path(package.__file__).resolve()
    if resolved_root not in package_path.parents:
        raise RuntimeError(
            "SOURCE_PIN_VIOLATION: ak_trading resolved to %s, outside the "
            "pinned run root %s" % (package_path, resolved_root)
        )
    return loaded


class _DeterministicClock:
    """Injectable integer-nanosecond business clock (store receipts stay
    deterministic; the harness owns the separate performance clock)."""

    def __init__(self, start_nano: int) -> None:
        self.now = int(start_nano)

    def __call__(self) -> int:
        self.now += 1_000_000_000
        return self.now


class CollectStoreSqliteWorkload:
    """SYNTHETIC production-binding store commit case (H22 COLLECT_STORE_SQLITE)."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.seed = int(ctx.seed)
        self.run_root = Path(__file__).resolve().parents[3]
        # All production-module imports happen HERE, at build time, i.e.
        # BEFORE the C20 NetworkGuard patches socket (a lazy `import
        # sqlalchemy` inside setup/guard pulls in ssl, whose `class
        # SSLSocket(socket)` then subclasses the guard's stub - observed
        # live, fixed by import placement; no harness semantics touched).
        self.mods = _import_pinned_production_modules(self.run_root)
        import sqlalchemy as sa  # environment dependency (GAP-04; C20 records identity)

        self._sa = sa
        self._engine: Any = None
        self._store: Any = None
        self._clock: Optional[Any] = None
        self.operations = 0
        self.units_total = 0
        self._acc = hashlib.sha256()
        self.op_records: List[Dict[str, Any]] = []
        self.published_rows_total = 0
        # r3 C6 per-connection fact state (Q51 D02).  ``_phase`` attributes
        # each connection's configuration/readback/recording cost to the
        # workload phase in which the connection is actually created
        # (D02-6: nothing is moved out of the measured phase - connections
        # created during operations pay their configuration cost inside
        # operation timing).  ``_batch_index`` is derived from the harness
        # formal-batch tmp layout (``batch-tmp-NN``); None for in-process /
        # smoke runs.  ``pass_id`` is NOT observable in-workload and stays
        # null (bound by the C7 orchestration layer).
        self._phase = "build"
        tmp_name = Path(str(ctx.tmp_dir)).name
        batch_suffix = tmp_name[10:] if tmp_name.startswith("batch-tmp-") else ""
        self._batch_index: Optional[int] = (
            int(batch_suffix) if batch_suffix.isdigit() else None
        )
        self._connection_facts: List[Dict[str, Any]] = []
        self._id_to_connection_index: Dict[int, int] = {}
        self._checkout_counts: Dict[int, int] = {}
        self._unrecorded_checkouts: List[Dict[str, Any]] = []
        self._layout_facts: Dict[str, Any] = {}
        self._env_identity: Dict[str, Any] = {}

    # -- setup ---------------------------------------------------------------

    def setup(self, ctx: Any) -> None:
        sa = self._sa
        models = self.mods["ak_trading.datapipeline.models"]
        storage = self.mods["ak_trading.datapipeline.storage"]

        tmp = Path(self.ctx.tmp_dir)
        tmp.mkdir(parents=True, exist_ok=True)
        acq_path = tmp / "nf22-collect-acquisition.sqlite"
        pipe_path = tmp / "nf22-collect-pipeline.sqlite"
        for stale in (acq_path, pipe_path):  # fresh trial identity (design 4.3)
            if stale.exists():
                stale.unlink()

        engine = sa.create_engine("sqlite://")
        models.attach_sqlite_schemas(
            engine, acquisition_path=str(acq_path), pipeline_path=str(pipe_path)
        )

        # r3 persistence-matched configuration (Q51 D01/D02): the connect
        # listener below fires on EVERY new DBAPI connection - the setup
        # connection, any pool refill and any rebuilt connection after
        # invalidation or dispose (connection rebuild walks the same path).
        # SQLAlchemy runs connect listeners in registration order and
        # ``attach_sqlite_schemas`` registered its ATTACH listener above, so
        # each connection is ATTACHed BEFORE the two file DBs are configured,
        # and the six-value effective readback completes inside the listener
        # - i.e. before the pool hands the connection to any caller, hence
        # before the connection can take part in any measured transaction
        # (D02-1).  The DELETE journal file I/O of the measured commits
        # stays inside operation timing (D02-6).  No checkpoint PRAGMA and
        # no other setting PRAGMA is ever issued by this workload.
        from sqlalchemy import event

        event.listen(engine, "connect", self._on_dbapi_connect)
        event.listen(engine, "checkout", self._on_dbapi_checkout)

        self._phase = "setup"

        with engine.connect() as connection:
            adoption = {}
            adoption.update(models.adopt_or_create_acquisition_tables(connection))
            adoption.update(models.adopt_or_create_pipeline_tables(connection))
        self._adoption = adoption
        self._engine = engine

        self._capture_layout_facts()
        self._capture_env_identity()

        self._clock = _DeterministicClock(COLLECT_T0_NANO + 7_200_000_000_000)
        self._store = storage.SqlPipelineStore(
            engine,
            dataset=COLLECT_DATASET,
            writer_id="w-nf22",
            acquisition=self.mods["ak_trading.acquisition.storage"],
            time_source=self._clock,
        )
        self.operations = 0
        self.units_total = 0
        self._acc = hashlib.sha256()
        self.op_records = []
        self.published_rows_total = 0
        self._acc.update(
            (
                "nf22-collect-store-sqlite-r3-persistence-matched|seed=%d|marker=%s\n"
                % (self.seed, COLLECT_SYNTHETIC_MARKER)
            ).encode("utf-8")
        )

    # -- r3 per-connection configuration + C6 facts (Q51 D01/D02) -----------

    def _on_dbapi_connect(self, dbapi_connection: Any, connection_record: Any) -> None:
        """Connect listener: configure BOTH attached file DBs on this actual
        connection and record its six-value effective readback facts.

        Registered after ``attach_sqlite_schemas``' own connect listener, so
        per SQLAlchemy's registration-order execution the ATTACH statements
        have already run for this connection when the PRAGMAs below execute.
        The whole listener completes before the pool returns the connection,
        so no measured transaction can ever run ahead of the readback.  A
        rebuilt connection (pool refill, invalidation, post-dispose
        reconnect) walks this exact same path (D02-1).
        """
        set_returns = self._set_file_db_pragmas(dbapi_connection)
        self._record_connection_facts(
            dbapi_connection, set_returns=set_returns, origin="connect_listener"
        )

    def _on_dbapi_checkout(
        self,
        dbapi_connection: Any,
        connection_record: Any,
        connection_proxy: Any = None,
    ) -> None:
        """Checkout listener: participation evidence for C6 coverage.

        Every pool checkout of a raw connection is counted against its
        recorded fact index; a checkout on a connection that has NO
        completed connect-time fact is preserved verbatim and fails
        verification (PARTICIPATING_CONNECTION_UNRECORDED).
        """
        connection_index = self._id_to_connection_index.get(id(dbapi_connection))
        if connection_index is None:
            self._unrecorded_checkouts.append(
                {
                    "dbapi_connection_id": id(dbapi_connection),
                    "phase": self._phase,
                }
            )
            return
        self._checkout_counts[connection_index] = (
            self._checkout_counts.get(connection_index, 0) + 1
        )

    def _set_file_db_pragmas(self, dbapi_connection: Any) -> Dict[str, Any]:
        """Execute EXACTLY the four declared PRAGMA settings (D01) on the
        given raw connection, capturing each statement's raw return (a
        journal_mode assignment returns the resulting mode row; a
        synchronous assignment returns nothing).  A raising statement is
        recorded as a SET_ERROR string, never silently retried or defaulted
        - verification fails on it (D02-3)."""
        cursor = dbapi_connection.cursor()
        returns: Dict[str, Any] = {}
        try:
            for schema, key, value in COLLECT_FILE_DB_PRAGMA_TARGETS:
                name = "%s.%s" % (schema, key)
                try:
                    cursor.execute("PRAGMA %s.%s=%s" % (schema, key, value))
                    row = cursor.fetchone() if cursor.description else None
                    returns[name] = row[0] if row is not None else None
                except Exception as exc:  # noqa: BLE001 - recorded, never masked
                    returns[name] = "SET_ERROR: %s: %s" % (type(exc).__name__, exc)
        finally:
            cursor.close()
        return returns

    def _record_connection_facts(
        self,
        dbapi_connection: Any,
        set_returns: Optional[Dict[str, Any]],
        origin: str,
    ) -> Dict[str, Any]:
        """Read back the six effective values plus the connection's database
        list on the given raw connection and append its C6 fact record.

        Any per-value readback failure is recorded verbatim
        (READBACK_ERROR/READBACK_EMPTY) - never substituted with a default -
        and marks the whole connection DEVIATION so verification fails with
        detail (D02-3).  Tracking values (pid, dbapi id, tmp path) are raw
        facts and never digest inputs (D02-5/D04).
        """
        import os

        deviations: List[str] = []
        effective: Dict[str, Dict[str, Any]] = {}
        database_rows: List[Dict[str, Any]] = []
        cursor = None
        cursor_error = None
        try:
            cursor = dbapi_connection.cursor()
        except Exception as exc:  # noqa: BLE001 - recorded, never masked
            cursor_error = "READBACK_ERROR: cursor unavailable: %s: %s" % (
                type(exc).__name__,
                exc,
            )
            deviations.append("READBACK_FAILED cursor: %s: %s" % (type(exc).__name__, exc))
        if cursor is not None:
            try:
                for schema in COLLECT_DB_SCHEMAS:
                    entry: Dict[str, Any] = {}
                    for key in ("journal_mode", "synchronous"):
                        name = "%s.%s" % (schema, key)
                        try:
                            row = cursor.execute("PRAGMA %s" % name).fetchone()
                            entry[key] = None if row is None else str(row[0])
                            if row is None:
                                deviations.append("READBACK_EMPTY %s" % name)
                        except Exception as exc:  # noqa: BLE001 - recorded, never masked
                            entry[key] = "READBACK_ERROR: %s: %s" % (
                                type(exc).__name__,
                                exc,
                            )
                            deviations.append("READBACK_FAILED %s: %s" % (name, exc))
                    effective[schema] = entry
                try:
                    for row in cursor.execute("PRAGMA database_list").fetchall():
                        database_rows.append(
                            {
                                "seq": int(row[0]),
                                "name": str(row[1]),
                                "file": str(row[2] or ""),
                            }
                        )
                except Exception as exc:  # noqa: BLE001 - recorded, never masked
                    deviations.append("READBACK_FAILED database_list: %s" % exc)
            finally:
                try:
                    cursor.close()
                except Exception:  # noqa: BLE001 - already-recorded failure path
                    deviations.append("READBACK_FAILED cursor.close")
        else:
            # six explicit error markers - the structure stays inspectable,
            # and no slot ever receives a fabricated default value (D02-3)
            for schema in COLLECT_DB_SCHEMAS:
                effective[schema] = {"journal_mode": cursor_error, "synchronous": cursor_error}

        # deviation rules: the two file DBs MUST hold DELETE/OFF effectively;
        # unread values (missing keys) are deviations, never defaults
        for schema in ("acquisition_w2", "datapipeline_v1"):
            journal = effective.get(schema, {}).get("journal_mode")
            if not isinstance(journal, str) or journal.strip().lower() != "delete":
                deviations.append(
                    "CONFIG_NOT_EFFECTIVE %s.journal_mode: requested=delete "
                    "effective=%r" % (schema, journal)
                )
            sync = effective.get(schema, {}).get("synchronous")
            sync_canonical = (
                COLLECT_SYNC_CANONICAL.get(str(sync).strip().lower())
                if isinstance(sync, str)
                else None
            )
            if sync_canonical != "0":
                deviations.append(
                    "CONFIG_NOT_EFFECTIVE %s.synchronous: requested=off(0) "
                    "effective=%r" % (schema, sync)
                )
        # main records NATIVE facts (no target); the established layout pins
        # main to the in-memory database whose native journal mode is
        # "memory" - anything else is a layout deviation, not a config target
        main_journal = effective.get("main", {}).get("journal_mode")
        if not isinstance(main_journal, str) or main_journal.strip().lower() != "memory":
            deviations.append(
                "MAIN_NOT_NATIVE_MEMORY_JOURNAL: effective=%r (in-memory main "
                "expected)" % (main_journal,)
            )
        for name, value in (set_returns or {}).items():
            if isinstance(value, str) and value.startswith("SET_ERROR"):
                deviations.append("SET_FAILED %s: %s" % (name, value))

        files = {
            row["name"]: row["file"]
            for row in database_rows
            if row["name"] in COLLECT_DB_SCHEMAS
        }
        if set_returns is not None:
            for schema in ("acquisition_w2", "datapipeline_v1"):
                if schema not in files or not files[schema]:
                    deviations.append(
                        "SCHEMA_FILE_CORRESPONDENCE_MISSING %s: database_list=%r"
                        % (schema, database_rows)
                    )
        fact = {
            "connection_index": len(self._connection_facts),
            "origin": origin,
            "phase_created": self._phase,
            "pid": os.getpid(),
            "dbapi_connection_id": id(dbapi_connection),
            "batch_index": self._batch_index,
            "tmp_dir": str(self.ctx.tmp_dir),
            "requested": {
                schema: {"journal_mode": "delete", "synchronous": "off"}
                for schema in ("acquisition_w2", "datapipeline_v1")
            },
            "set_returns": dict(set_returns or {}),
            "effective": effective,
            "database_list": database_rows,
            "files": files,
            "effective_complete": all(
                isinstance(effective.get(schema, {}).get(key), str)
                and not effective[schema][key].startswith("READBACK_ERROR")
                for schema in COLLECT_DB_SCHEMAS
                for key in ("journal_mode", "synchronous")
                if schema in effective
            )
            and len(effective) == len(COLLECT_DB_SCHEMAS),
            "deviations": deviations,
            "status": "DEVIATION" if deviations else "OK",
        }
        self._id_to_connection_index[id(dbapi_connection)] = fact["connection_index"]
        self._connection_facts.append(fact)
        return fact

    def _capture_layout_facts(self) -> None:
        """Confirm the established layout (D02-2): in-memory main, all
        measured tables in the ATTACHed file DBs.  Table expectations are
        derived from the pinned production models module - nothing is
        hardcoded here."""
        sa = self._sa
        models = self.mods["ak_trading.datapipeline.models"]
        expected: Dict[str, List[str]] = {"main": []}
        for table in list(models.acquisition_tables) + list(models.pipeline_tables):
            expected.setdefault(table.schema or "main", []).append(table.name)
        actual: Dict[str, List[str]] = {}
        with self._engine.connect() as connection:
            for schema in COLLECT_DB_SCHEMAS:
                rows = connection.execute(
                    sa.text(
                        "SELECT name FROM %s.sqlite_master WHERE type='table' "
                        "AND name NOT LIKE 'sqlite_%%' ORDER BY name" % schema
                    )
                ).scalars()
                actual[schema] = [str(name) for name in rows]
        measured = sorted(name for names in expected.values() for name in names)
        self._layout_facts = {
            "main_engine_url": "sqlite:// (in-memory main; native, never configured)",
            "expected_tables_per_schema": {
                schema: sorted(names) for schema, names in expected.items()
            },
            "actual_tables_per_schema": actual,
            "measured_tables_in_main": sorted(
                set(actual.get("main", [])) & set(measured)
            ),
        }

    def _capture_env_identity(self) -> None:
        """C6 environment identity facts: SQLite library, driver, Python, OS
        (D02-2).  ``sqlite3.version`` is deprecated (removal announced for
        Python 3.14); it is read under warning suppression and recorded as
        None when absent - no default is invented."""
        import platform
        import sqlite3
        import sys
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            driver_module_version = getattr(sqlite3, "version", None)
        self._env_identity = {
            "sqlite_library_version": sqlite3.sqlite_version,
            "sqlite_driver": "pysqlite (stdlib sqlite3, DBAPI 2.0)",
            "sqlite_driver_module_version": driver_module_version,
            "sqlalchemy_version": self._sa.__version__,
            "python_version": sys.version,
            "os": {
                "sys.platform": sys.platform,
                "platform": platform.platform(),
                "machine": platform.machine(),
            },
            "pid_note": "pid recorded per connection fact (raw, process-local)",
        }

    # -- synthetic frame construction ----------------------------------------

    def _cents(self, value: int) -> str:
        sign = "-" if value < 0 else ""
        value = abs(int(value))
        return "%s%d.%02d" % (sign, value // 100, value % 100)

    def _build_frame(self, op_index: int) -> Dict[str, Any]:
        """One deterministic SYNTHETIC envelope + acquisition ClassifiedRow
        sequence for operation ``op_index`` (frozen composition ladder)."""
        dp_contracts = self.mods["ak_trading.datapipeline.contracts"]
        acq_contracts = self.mods["ak_trading.acquisition.contracts"]

        shape_name, padding_n, invalid_n, closed_n, provisional_n = COLLECT_SHAPES[
            op_index % len(COLLECT_SHAPES)
        ]
        attempt_id = "nf22-att-%04d" % op_index
        task_id = "nf22-task-collect"
        window_base = COLLECT_T0_NANO + op_index * COLLECT_WINDOW_STRIDE_NANO
        observed_at = window_base + 7_200_000_000_000
        base_cents = 400_000 + (self.seed % 97) * 25 + (op_index % 7) * 10

        rows_obs: List[Any] = []
        classified: List[Any] = []
        nanos: List[int] = []

        def _append_padding(ordinal: int) -> None:
            rows_obs.append(
                dp_contracts.RowObservation(
                    raw_row_ordinal=ordinal,
                    frame_partition=dp_contracts.FramePartition.PADDING,
                    row_status=None,
                    data_origin=dp_contracts.DataOrigin.SYNTHETIC,
                    acquisition_mode=dp_contracts.AcquisitionMode.SESSION_BACKFILL,
                    observed_at_nano=observed_at,
                    first_observed_at_nano=observed_at,
                    connection_epoch=1,
                    continuity_id="nf22-c1",
                    update_seq=ordinal,
                    key=None,
                    content_sha256=None,
                    event_start_nano=None,
                    event_end_nano=None,
                    availability_basis=dp_contracts.AvailabilityBasis.RETROSPECTIVE_CAPTURE,
                )
            )
            classified.append(
                acq_contracts.ClassifiedRow(
                    raw_row_ordinal=ordinal,
                    datetime_nano=None,
                    source_symbol=COLLECT_SYMBOL,
                    duration_seconds=COLLECT_DURATION,
                    bar_start_nano=None,
                    bar_end_nano=None,
                    trading_day=None,
                    trading_day_source="none",
                    frame_partition=acq_contracts.FramePartition.PADDING,
                    valid_status=None,
                    values={},
                    content_sha256=None,
                    evidence_version="nf22-ev1",
                    reason="synthetic padding head",
                )
            )

        def _append_invalid(ordinal: int) -> None:
            rows_obs.append(
                dp_contracts.RowObservation(
                    raw_row_ordinal=ordinal,
                    frame_partition=dp_contracts.FramePartition.INVALID,
                    row_status=None,
                    data_origin=dp_contracts.DataOrigin.SYNTHETIC,
                    acquisition_mode=dp_contracts.AcquisitionMode.SESSION_BACKFILL,
                    observed_at_nano=observed_at,
                    first_observed_at_nano=observed_at,
                    connection_epoch=1,
                    continuity_id="nf22-c1",
                    update_seq=ordinal,
                    key=None,
                    content_sha256=None,
                    event_start_nano=None,
                    event_end_nano=None,
                    availability_basis=dp_contracts.AvailabilityBasis.RETROSPECTIVE_CAPTURE,
                )
            )
            classified.append(
                acq_contracts.ClassifiedRow(
                    raw_row_ordinal=ordinal,
                    datetime_nano=None,
                    source_symbol=COLLECT_SYMBOL,
                    duration_seconds=COLLECT_DURATION,
                    bar_start_nano=None,
                    bar_end_nano=None,
                    trading_day=None,
                    trading_day_source="none",
                    frame_partition=acq_contracts.FramePartition.INVALID,
                    valid_status=None,
                    values={},
                    content_sha256=None,
                    evidence_version="nf22-ev1",
                    reason="synthetic invalid probe (unusable OHLC)",
                )
            )

        def _append_valid(ordinal: int, valid_index: int, status_value: str) -> None:
            nano = window_base + valid_index * COLLECT_DURATION * 1_000_000_000
            open_c = base_cents + valid_index * 3
            values = {
                "open": self._cents(open_c),
                "high": self._cents(open_c + 5),
                "low": self._cents(open_c - 5),
                "close": self._cents(open_c + 1),
                "volume": "%d" % (100 + valid_index),
                "open_oi": "%d" % (5000 + valid_index),
                "close_oi": "%d" % (5010 + valid_index),
            }
            content = dp_contracts.canonical_bar_content(
                duration_seconds=COLLECT_DURATION,
                datetime_nano=nano,
                bar_start_nano=nano,
                bar_end_nano=nano + COLLECT_DURATION * 1_000_000_000,
                trading_day=COLLECT_TRADING_DAY,
                values=values,
            )
            status = dp_contracts.RowStatus(status_value)
            key = dp_contracts.BarKey(
                source_id=COLLECT_SOURCE_ID,
                source_symbol=COLLECT_SYMBOL,
                duration_seconds=COLLECT_DURATION,
                datetime_nano=nano,
            )
            closed = status is dp_contracts.RowStatus.CLOSED
            rows_obs.append(
                dp_contracts.RowObservation(
                    raw_row_ordinal=ordinal,
                    frame_partition=dp_contracts.FramePartition.VALID,
                    row_status=status,
                    data_origin=dp_contracts.DataOrigin.SYNTHETIC,
                    acquisition_mode=dp_contracts.AcquisitionMode.SESSION_BACKFILL,
                    observed_at_nano=observed_at,
                    first_observed_at_nano=observed_at,
                    connection_epoch=1,
                    continuity_id="nf22-c1",
                    update_seq=ordinal,
                    key=key,
                    content_sha256=content,
                    event_start_nano=nano,
                    event_end_nano=nano + COLLECT_DURATION * 1_000_000_000,
                    availability_basis=(
                        dp_contracts.AvailabilityBasis.RETROSPECTIVE_CAPTURE
                        if closed
                        else dp_contracts.AvailabilityBasis.LIVE_CAPTURE_EVIDENCE
                    ),
                )
            )
            classified.append(
                acq_contracts.ClassifiedRow(
                    raw_row_ordinal=ordinal,
                    datetime_nano=nano,
                    source_symbol=COLLECT_SYMBOL,
                    duration_seconds=COLLECT_DURATION,
                    bar_start_nano=nano,
                    bar_end_nano=nano + COLLECT_DURATION * 1_000_000_000,
                    trading_day=COLLECT_TRADING_DAY,
                    trading_day_source="session_evidence",
                    frame_partition=acq_contracts.FramePartition.VALID,
                    valid_status=acq_contracts.ValidRowStatus(status_value),
                    values=values,
                    content_sha256=content,
                    evidence_version="nf22-ev1",
                    reason=None,
                )
            )
            nanos.append(nano)

        ordinal = 0
        for _ in range(padding_n):
            _append_padding(ordinal)
            ordinal += 1
        for _ in range(invalid_n):
            _append_invalid(ordinal)
            ordinal += 1
        for _ in range(closed_n):
            _append_valid(ordinal, len(nanos), "CLOSED")
            ordinal += 1
        for _ in range(provisional_n):
            _append_valid(ordinal, len(nanos), "PROVISIONAL")
            ordinal += 1

        frame_rows = ordinal
        valid_rows = closed_n + provisional_n
        raw_payload = "%s|%s|%d" % (COLLECT_SYNTHETIC_MARKER, attempt_id, op_index)
        raw_sha256 = hashlib.sha256(raw_payload.encode("utf-8")).hexdigest()
        meta = dp_contracts.RawSnapshotMetaV1(
            task_id=task_id,
            attempt_id=attempt_id,
            source_symbol=COLLECT_SYMBOL,
            duration_seconds=COLLECT_DURATION,
            requested_length=8192,
            returned_length=frame_rows,
            frame_rows=frame_rows,
            padding_rows=padding_n,
            invalid_rows=invalid_n,
            valid_rows=valid_rows,
            observed_at_nano=observed_at,
            sdk_version="synthetic-sdk-nf22",
            serializer_version="v1",
            raw_path="raw/nf22/%s/frame.jsonl" % attempt_id,
            raw_sha256=raw_sha256,
            raw_bytes=len(raw_payload),
            first_valid_nano=(nanos[0] if nanos else None),
            last_valid_nano=(nanos[-1] if nanos else None),
        )
        envelope = dp_contracts.SnapshotEnvelope(
            snapshot_id="snap-nf22-%04d" % op_index,
            task_id=task_id,
            attempt_id=attempt_id,
            coordinate=dp_contracts.SourceCoordinate(
                source_id=COLLECT_SOURCE_ID,
                source_symbol=COLLECT_SYMBOL,
                duration_seconds=COLLECT_DURATION,
            ),
            connection_baseline=dp_contracts.ConnectionBaseline(
                connection_epoch=1,
                baseline_snapshot_id="nf22-b1",
                ready_update_seq=1,
            ),
            meta=meta,
            rows=tuple(rows_obs),
            raw_bytes_sha256=raw_sha256,
            policy_sha256=hashlib.sha256(b"nf22-synthetic-policy-r2").hexdigest(),
            source_bundle_sha256=hashlib.sha256(b"nf22-synthetic-source-bundle-r2").hexdigest(),
            calendar_evidence_sha256=None,
            generation_evidence_sha256=None,
            universe_sha256=hashlib.sha256(b"nf22-synthetic-universe-r2").hexdigest(),
            data_origin=dp_contracts.DataOrigin.SYNTHETIC,
            synthetic_marker=COLLECT_SYNTHETIC_MARKER,
        )
        return {
            "shape": shape_name,
            "envelope": envelope,
            "classified": classified,
            "padding": padding_n,
            "invalid": invalid_n,
            "closed": closed_n,
            "provisional": provisional_n,
            "valid": valid_rows,
            "frame": frame_rows,
        }

    # -- measured operation ---------------------------------------------------

    def operation(self, ctx: Any) -> Dict[str, Any]:
        ports = self.mods["ak_trading.datapipeline.ports"]
        readback = self.mods["ak_trading.datapipeline.readback"]

        self._phase = "operation"  # r3: honest phase attribution (D02-6)
        op_index = self.operations
        self.operations += 1
        frame = self._build_frame(op_index)
        envelope = frame["envelope"]
        classified = frame["classified"]
        fence = ports.FencingToken(
            dataset=COLLECT_DATASET,
            task_key="nf22|%s|%d" % (COLLECT_SYMBOL, COLLECT_DURATION),
            lease_id="nf22-lease-r2",
            fencing_epoch=1,
        )

        # stage 1: staged commit through the real DP store + acquisition closure
        receipt = self._store.commit(envelope, tuple(classified), fence)
        dispositions = [p.disposition for p in receipt.row_provenance]
        new_versions = dispositions.count("NEW_VERSION")
        quarantined = dispositions.count("QUARANTINED")
        same_first = dispositions.count("SAME_VERSION")
        if receipt.outcome.value != "COMMITTED_NEW":
            return {
                "ok": False,
                "units": 0,
                "reason": "OP%d stage-1 outcome %s (expected COMMITTED_NEW)"
                % (op_index, receipt.outcome.value),
            }
        if new_versions != frame["valid"] or quarantined != frame["padding"] + frame["invalid"] or same_first:
            return {
                "ok": False,
                "units": 0,
                "reason": "OP%d stage-1 disposition conservation broken: new=%d quar=%d same=%d"
                % (op_index, new_versions, quarantined, same_first),
            }

        # stage 2: idempotent replay of the SAME envelope (H09: 0 new versions)
        replay = self._store.commit(envelope, tuple(classified), fence)
        replay_new = [p.disposition for p in replay.row_provenance].count("NEW_VERSION")
        if (
            replay.outcome.value != "COMMITTED_SAME_CONTENT"
            or replay.batch_id != receipt.batch_id
            or replay.commit_id != receipt.commit_id
            or replay_new != 0
        ):
            return {
                "ok": False,
                "units": 0,
                "reason": "OP%d replay not idempotent: outcome=%s new=%d batch_eq=%s"
                % (
                    op_index,
                    replay.outcome.value,
                    replay_new,
                    replay.batch_id == receipt.batch_id,
                ),
            }

        # stage 3: published-only readback count (CLOSED+VALID, conflict-free);
        # the full published-row iteration is done once in verify() for the
        # digest (per-op full scans are quadratic and blew the smoke budget).
        as_of = self._clock.now + 3_600_000_000_000
        with self._engine.connect() as connection:
            published_count = readback.count_available_versions(
                connection, as_of_nano=as_of, dataset=COLLECT_DATASET
            )
        self.published_rows_total = published_count

        self.units_total += frame["valid"]
        self._acc.update(
            (
                "op=%d|shape=%s|att=%s|batch=%s|commit=%s|new=%d|quar=%d|"
                "replay=%s|pub=%d\n"
                % (
                    op_index,
                    frame["shape"],
                    envelope.attempt_id,
                    receipt.batch_id,
                    receipt.commit_id,
                    new_versions,
                    quarantined,
                    replay.outcome.value,
                    self.published_rows_total,
                )
            ).encode("utf-8")
        )
        self.op_records.append(
            {
                "op": op_index,
                "shape": frame["shape"],
                "attempt_id": envelope.attempt_id,
                "batch_id": receipt.batch_id,
                "commit_id": receipt.commit_id,
                "outcome": receipt.outcome.value,
                "new_versions": new_versions,
                "quarantined": quarantined,
                "replay_outcome": replay.outcome.value,
                "published_total": self.published_rows_total,
                "identity_inputs": {
                    "task_id": envelope.task_id,
                    "attempt_id": envelope.attempt_id,
                    "source_id": COLLECT_SOURCE_ID,
                    "source_symbol": COLLECT_SYMBOL,
                    "duration_seconds": COLLECT_DURATION,
                    "logical_task_key": "%s|%s|%d"
                    % (COLLECT_SOURCE_ID, COLLECT_SYMBOL, COLLECT_DURATION),
                    "raw_sha256": envelope.meta.raw_sha256,
                    "policy_sha256": envelope.policy_sha256,
                    "universe_sha256": envelope.universe_sha256,
                    "source_sha256": envelope.source_bundle_sha256,
                    "calendar_sha256": "UNKNOWN",
                    "row_count": frame["frame"],
                },
                "composition": {
                    "padding": frame["padding"],
                    "invalid": frame["invalid"],
                    "closed": frame["closed"],
                    "provisional": frame["provisional"],
                    "valid": frame["valid"],
                    "frame": frame["frame"],
                },
            }
        )
        return {"ok": True, "units": frame["valid"]}

    # -- verification (frozen rules, analytic expectations) -------------------

    def _table_counts(self) -> Dict[str, int]:
        sa = self._sa
        models = self.mods["ak_trading.datapipeline.models"]

        def one(table: Any) -> int:
            with self._engine.connect() as connection:
                return int(
                    connection.execute(
                        sa.select(sa.func.count()).select_from(table)
                    ).scalar_one()
                )

        return {
            "bar_versions": one(models.bar_versions),
            "batch_rows": one(models.batch_rows),
            "ingest_batches": one(models.ingest_batches),
            "observation_records": one(models.observation_records),
            "pipeline_commits": one(models.pipeline_commits),
            "quality_findings": one(models.quality_findings),
            "status_events": one(models.status_events),
            "task_checkpoints": one(models.task_checkpoints),
        }

    def _expected_totals(self) -> Dict[str, int]:
        totals = {"frame": 0, "valid": 0, "closed": 0, "ops": self.operations}
        for index in range(self.operations):
            _, padding_n, invalid_n, closed_n, provisional_n = COLLECT_SHAPES[
                index % len(COLLECT_SHAPES)
            ]
            totals["frame"] += padding_n + invalid_n + closed_n + provisional_n
            totals["valid"] += closed_n + provisional_n
            totals["closed"] += closed_n
        return totals

    def _verify_sqlite_config(self) -> List[str]:
        """C6 configuration gate (Q51 D02-3).  Any deviation, readback
        failure, missing participation coverage or layout error yields a
        detailed SQLITE_CONFIG_INVALID reason; verify() then returns FAIL
        and the C20 frozen state machine maps that to a case-level INVALID
        run.  No default value, no first-connection result and no empty
        object ever substitutes for missing evidence."""
        reasons: List[str] = []
        if not self._connection_facts:
            reasons.append(
                "SQLITE_CONFIG_INVALID NO_CONNECTION_FACTS: no DBAPI connection "
                "was ever configured/read back (coverage evidence missing)"
            )
        for fact in self._connection_facts:
            index = fact["connection_index"]
            checkouts = self._checkout_counts.get(index, 0)
            fact["checkouts"] = checkouts
            if fact["status"] != "OK":
                for deviation in fact["deviations"]:
                    reasons.append(
                        "SQLITE_CONFIG_INVALID connection[%d] (%s, phase=%s): %s"
                        % (index, fact["origin"], fact["phase_created"], deviation)
                    )
            if checkouts > 0 and not fact.get("effective_complete", False):
                reasons.append(
                    "SQLITE_CONFIG_INVALID connection[%d] "
                    "PARTICIPATED_WITHOUT_COMPLETE_READBACK (six-value coverage "
                    "broken)" % index
                )
        for unknown in self._unrecorded_checkouts:
            reasons.append(
                "SQLITE_CONFIG_INVALID PARTICIPATING_CONNECTION_UNRECORDED: "
                "checkout of dbapi id %s at phase=%s has no connect-time fact"
                % (unknown["dbapi_connection_id"], unknown["phase"])
            )
        layout = self._layout_facts
        if layout:
            expected = layout.get("expected_tables_per_schema", {})
            actual = layout.get("actual_tables_per_schema", {})
            for schema in ("acquisition_w2", "datapipeline_v1"):
                missing = sorted(
                    set(expected.get(schema, [])) - set(actual.get(schema, []))
                )
                if missing:
                    reasons.append(
                        "SQLITE_CONFIG_INVALID LAYOUT_MISSING_TABLES %s: %s"
                        % (schema, missing)
                    )
            if layout.get("measured_tables_in_main"):
                reasons.append(
                    "SQLITE_CONFIG_INVALID LAYOUT_MAIN_NOT_EMPTY: measured tables "
                    "found in in-memory main: %s" % layout["measured_tables_in_main"]
                )
        return reasons

    def _assemble_sqlite_facts(self) -> Dict[str, Any]:
        """Assemble the C6 raw-facts object (W02 consumes per-batch copies
        of this as ``verify.summary.sqlite_facts``; it is the per-batch
        original fact set, never a whole-run claim).  Tracking values (pid,
        dbapi ids, tmp paths) stay raw and are never digest inputs."""
        participation = [
            {
                "connection_index": fact["connection_index"],
                "phase_created": fact["phase_created"],
                "checkouts": fact.get("checkouts", 0),
                "status": fact["status"],
            }
            for fact in self._connection_facts
        ]
        return {
            "facts_schema": "q51-r3-connection-facts/1.0",
            "pass_id": None,
            "pass_id_note": (
                "observation/confirmation pass identity is bound by the C7 "
                "orchestration layer, not observable inside the workload"
            ),
            "batch_index": self._batch_index,
            "requested": {
                "acquisition_w2": {"journal_mode": "delete", "synchronous": "off"},
                "datapipeline_v1": {"journal_mode": "delete", "synchronous": "off"},
                "main": None,
            },
            "environment": dict(self._env_identity),
            "layout": dict(self._layout_facts),
            "connections": [dict(fact) for fact in self._connection_facts],
            "coverage": {
                "connections_recorded": len(self._connection_facts),
                "connections_ok": sum(
                    1 for fact in self._connection_facts if fact["status"] == "OK"
                ),
                "connections_participated": sum(
                    1 for entry in participation if entry["checkouts"] > 0
                ),
                "participation": participation,
                "unrecorded_checkouts": list(self._unrecorded_checkouts),
            },
        }

    def verify(self, ctx: Any) -> Dict[str, Any]:
        self._phase = "verify"  # r3: honest phase attribution (D02-6)
        fail_reasons: List[str] = []
        if self.operations <= 0:
            fail_reasons.append("no operations executed")
        expected = self._expected_totals()
        published_cumulative = 0
        unique_batches = set()
        for record in self.op_records:
            if record["outcome"] != "COMMITTED_NEW":
                fail_reasons.append(
                    "OP%d outcome %s != COMMITTED_NEW" % (record["op"], record["outcome"])
                )
            if record["replay_outcome"] != "COMMITTED_SAME_CONTENT":
                fail_reasons.append(
                    "OP%d replay outcome %s != COMMITTED_SAME_CONTENT"
                    % (record["op"], record["replay_outcome"])
                )
            composition = record["composition"]
            if record["new_versions"] != composition["valid"]:
                fail_reasons.append(
                    "OP%d new_versions %d != valid %d"
                    % (record["op"], record["new_versions"], composition["valid"])
                )
            if record["quarantined"] != composition["padding"] + composition["invalid"]:
                fail_reasons.append(
                    "OP%d quarantined %d != rejected %d"
                    % (
                        record["op"],
                        record["quarantined"],
                        composition["padding"] + composition["invalid"],
                    )
                )
            published_cumulative += composition["closed"]
            if record["published_total"] != published_cumulative:
                fail_reasons.append(
                    "PUBLISHED_LEAK_OP%d: published %d != cumulative closed %d"
                    % (record["op"], record["published_total"], published_cumulative)
                )
            unique_batches.add(record["batch_id"])
        if self.op_records and len(unique_batches) != self.operations:
            fail_reasons.append(
                "BATCH_IDENTITY_COLLISION: %d unique batch ids for %d ops"
                % (len(unique_batches), self.operations)
            )
        if self.units_total != expected["valid"]:
            fail_reasons.append(
                "UNITS_MISMATCH: %d != expected valid rows %d"
                % (self.units_total, expected["valid"])
            )
        if self.published_rows_total != expected["closed"]:
            fail_reasons.append(
                "PUBLISHED_MISMATCH: %d != expected published CLOSED rows %d"
                % (self.published_rows_total, expected["closed"])
            )
        counts = self._table_counts() if self._engine is not None else {}
        if counts:
            # analytic expectations from the frozen composition ladder and
            # the H02.3/H09 conservation rules of the reviewed store
            for table, expected_value in (
                ("bar_versions", expected["valid"]),
                ("batch_rows", expected["frame"]),
                ("observation_records", expected["frame"]),
                ("ingest_batches", expected["ops"]),
                ("pipeline_commits", expected["ops"]),
            ):
                if counts.get(table) != expected_value:
                    fail_reasons.append(
                        "TABLE_COUNT_MISMATCH %s: %s != %d"
                        % (table, counts.get(table), expected_value)
                    )
            if counts.get("quality_findings") != 0:
                fail_reasons.append("UNEXPECTED_CONFLICT_FINDINGS: %s" % counts.get("quality_findings"))
        # full published-only readback, once, into the digest
        if self._engine is not None and self.operations > 0:
            readback = self.mods["ak_trading.datapipeline.readback"]
            as_of = self._clock.now + 3_600_000_000_000
            with self._engine.connect() as connection:
                published_rows = [
                    dict(row)
                    for row in readback.iter_available_versions(
                        connection,
                        as_of_nano=as_of,
                        dataset=COLLECT_DATASET,
                        page_size=COLLECT_READBACK_PAGE,
                    )
                ]
            if len(published_rows) != expected["closed"]:
                fail_reasons.append(
                    "PUBLISHED_FINAL_MISMATCH: %d != expected %d CLOSED rows"
                    % (len(published_rows), expected["closed"])
                )
            for row in published_rows:
                self._acc.update(
                    (
                        "pub|%s|%s|%s|%s|%s\n"
                        % (
                            row["key_canonical"],
                            row["content_sha256"],
                            row["version_id"],
                            row["row_status"],
                            row["available_at_nano"],
                        )
                    ).encode("utf-8")
                )
        self._acc.update(
            (
                "final|ops=%d|units=%d|published=%d|%s\n"
                % (
                    self.operations,
                    self.units_total,
                    self.published_rows_total,
                    "|".join(
                        "%s=%s" % (name, counts.get(name)) for name in sorted(counts)
                    ),
                )
            ).encode("utf-8")
        )
        # r3 C6 gate runs AFTER all engine use inside verify() so that any
        # connection this very phase creates is covered by the reasons too
        config_fail_reasons = self._verify_sqlite_config()
        fail_reasons = config_fail_reasons + fail_reasons
        summary = {
            "operations": self.operations,
            "units_total": self.units_total,
            "frame_rows_total": expected["frame"],
            "valid_rows_total": expected["valid"],
            "published_rows_total": self.published_rows_total,
            "unique_batch_ids": len(unique_batches),
            "table_counts": counts,
            "sqlite_config_status": "INVALID" if config_fail_reasons else "OK",
            "sqlite_config_fail_reasons": config_fail_reasons,
            "sqlite_facts": self._assemble_sqlite_facts(),
            "seed": self.seed,
            "synthetic_marker": COLLECT_SYNTHETIC_MARKER,
            "evidence_grade": "WORKTREE_SNAPSHOT (acquisition rows 13-15; release-grade handoff pending, GAP-05)",
        }
        return {
            "status": "FAIL" if fail_reasons else "PASS",
            "digest": self._acc.hexdigest(),
            "summary": summary,
            "fail_reasons": fail_reasons,
        }

    def teardown(self, ctx: Any) -> None:
        self._phase = "teardown"
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None  # trial DB files live under ctx.tmp_dir (harness-owned)


def build_collect_store_sqlite(ctx: Any) -> CollectStoreSqliteWorkload:
    return CollectStoreSqliteWorkload(ctx)
