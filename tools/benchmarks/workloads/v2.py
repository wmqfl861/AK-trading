"""NF21 v2 benchmark workloads (SYNTHETIC) - feature snapshots, decision
replay and validation metrics.

Owner: NF21 (v2 benchmark seat), work order
``docs/devplan-next-frontier-20260924-r1/work-orders/NF21-v2-benchmark.md``,
handed over under the C20 contract (``docs/benchmarks/README.md`` §12).
This single module serves all v2 cases: ``build(ctx)`` dispatches on
``ctx.params["case"]``; every case manifest freezes its own params/seed,
budgets and expected smoke digest, and lists this file plus its frozen
fixture-parameter JSON under ``tools/benchmarks/fixtures/v2/``.

Measured production entry points (all imported under the hash-verified
source map at ``tools/benchmarks/fixtures/v2/source-map.json``; the map
is the transitive import closure of the measured module roots - 64
modules: 58 v2-domain files plus the 6 cross-domain
``ak_trading.futures``/``ak_trading.probability`` modules reached via
``v2.baseline`` (marked cross_domain) - and the CLI hash-checks every
map entry against the repository tree before the first operation runs,
before any measured module is imported; a runtime cross-check executing
all seven cases loads 61 of the 64, the remaining three being mapped
conditional imports the cases do not exercise):

* ``ak_trading.v2.factors.build_feature_snapshot`` (cases
  ``feature_prefix_cold`` / ``feature_prefix_hot``),
* ``ak_trading.v2.decision.replay.run_decision_replay`` (cases
  ``decision_replay_wait_only`` / ``decision_replay_filled``; the filled
  case additionally exercises ``decision.risk.enforce_account_risk``,
  ``decision.routing.choose_proposal`` and ``decision.execution`` for
  real inside the replay host),
* ``ak_trading.v2.validation.metrics.evaluate_forecasts`` /
  ``block_bootstrap`` / ``evaluate_replay`` (validation cases).

Honesty boundaries (frozen):

* Every input is SYNTHETIC and fully deterministic: bar prices, volumes,
  open interest, forecasts, labels and payoff observations are derived
  from integer index formulas and the frozen seed.  No clock, no
  randomness other than the seed-driven production bootstrap RNG, no I/O,
  no network, no production database, no historical PIT data.
* ``decision_replay_wait_only`` mirrors the M4 smoke seed
  (``tests/test_v2_m4_replay.py::replay_inputs``: 150 minute bars,
  five-minute window, WAIT-only) and by naming rule covers no fills -
  it must never be read as strategy matching throughput.
* ``decision_replay_filled`` reproduces candidate selection, the account
  risk filter, legal next-window execution and ledger accounting by
  installing SYNTHETIC fixture strategy contexts around each
  ``run_decision_replay`` call - exactly the scheduler-fixture pattern of
  ``tests/test_v2_m4_replay.py``.  Strategy evaluation and forecast
  construction are therefore NOT measured in that case; the scheduler,
  gates, risk filter, execution simulator and ledger are.
* ``feature_prefix_cold`` clears the production module's content-keyed
  caches inside every measured operation so each operation is one
  independent full construction (cache clearing itself is timed).  The
  caches are pure (byte-identical outputs), verified per operation by
  snapshot-id equality against the hot path fixture expectations.
* The 1000-replicate trading-day block bootstrap semantics of
  ``validation/metrics.py`` are preserved untouched; verify() fails if a
  future code change silently shrinks them.
* Validation statistics keep their honest production statuses on
  synthetic data (INSUFFICIENT is expected and retained; nothing here
  claims statistical PASS quality).
"""

from __future__ import annotations

import hashlib
import importlib
import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_ROOT = Path(__file__).resolve().parents[3]
_FIXTURE_DIR = _ROOT / "tools" / "benchmarks" / "fixtures" / "v2"

NANO = 1_000_000_000
MINUTE = 60 * NANO


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _digest(obj: Any) -> str:
    return hashlib.sha256(_canonical(obj)).hexdigest()


def _load_fixture_params(name: str) -> Dict[str, Any]:
    path = _FIXTURE_DIR / ("%s.json" % name)
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _import_measured(dotted: str) -> Any:
    """Import a measured v2 module and verify it resolves inside the
    repository tree covered by the NF21 source map (identity binding;
    any mismatch fails the case honestly instead of measuring the wrong
    bytes)."""
    module = importlib.import_module(dotted)
    src = Path(module.__file__).resolve()
    relative = dotted.replace(".", "/")
    expected = (_ROOT / "src" / ("%s.py" % relative)).resolve()
    if src != expected:
        raise RuntimeError(
            "measured module %s resolves to %s, expected %s (source-map binding)"
            % (dotted, src, expected)
        )
    return module


def _checked_params(ctx: Any, fixture_name: str, keys: Tuple[str, ...]) -> Dict[str, Any]:
    """Load the frozen fixture parameters and assert the manifest params
    agree with them on the shared keys (double binding of frozen inputs)."""
    fixture = _load_fixture_params(fixture_name)
    shared = {key: fixture[key] for key in keys if key in fixture}
    manifest_shared = {key: ctx.params[key] for key in keys if key in ctx.params}
    if shared != manifest_shared:
        raise RuntimeError(
            "manifest params disagree with frozen fixture %s: %r != %r"
            % (fixture_name, manifest_shared, shared)
        )
    return fixture


# ---------------------------------------------------------------------------
# SYNTHETIC fixture constructors (shapes mirror the frozen M2/M4 test
# fixtures: tests/test_v2_m2_causality.py session/bar constructors and
# tests/test_v2_m4_ledger.py account/rule/bar constructors).
# ---------------------------------------------------------------------------

_HASH_ZERO = "0" * 64


def _quality(status: str = "READY", basis: str = "observed", input_hash: str = _HASH_ZERO):
    contracts = _import_measured("ak_trading.v2.contracts")
    return contracts.DataQuality(status=status, reasons=(), temporal_basis=basis,
                                 input_hash=input_hash)


class _V2FixtureKit:
    """Lazily-imported v2 dataclass constructors (SYNTHETIC inputs only)."""

    def __init__(self) -> None:
        self.contracts = _import_measured("ak_trading.v2.contracts")

    # -- feature-prefix fixtures (M2 shape) -----------------------------

    def feature_session(self, day: date, index: int, slots: int, kind: str, hour: int):
        start = int(datetime(day.year, day.month, day.day, hour, 0,
                             tzinfo=timezone.utc).timestamp()) * NANO
        return self.contracts.SessionWindow(
            session_id="%s%d" % (kind[0], index), product="SHFE.RB", trading_day=day,
            start_nano=start, end_nano=start + slots * MINUTE, kind=kind,
            calendar_version="cal-nf21-v1", evidence_status="VERIFIED")

    def feature_bar(self, start: int, index: int, session, contract: str = "RB2610"):
        close = Decimal(100 + (index % 11))
        end = start + MINUTE
        return self.contracts.MarketBar(
            bar_id="%s|%d" % (contract, start), contract_id=contract, raw_symbol="rb2610",
            product="SHFE.RB", duration_seconds=60, datetime_nano=start, start_nano=start,
            end_nano=end, trading_day=session.trading_day, session_id=session.session_id,
            available_at_nano=end, imported_at_nano=None, open=close,
            high=close + Decimal(1), low=close - Decimal(1), close=close,
            volume=Decimal(10 + index % 4), open_oi=Decimal(1000 + index),
            close_oi=Decimal(1000 + index), complete=True, expected_minutes=1,
            actual_minutes=1, source="synthetic-nf21",
            quality=_quality(basis="observed"))

    def feature_rule(self, from_nano: int, until_nano: int):
        c = self.contracts
        return c.TradingRule(
            rule_id="rule-nf21-rb", contract_id="RB2610", effective_from_nano=from_nano,
            effective_until_nano=until_nano, available_at_nano=from_nano,
            multiplier=Decimal(10), price_tick=Decimal("1"), lot_step=1, min_open_lots=1,
            open_fee=c.FeeLeg(Decimal("4"), Decimal("0"), False),
            close_fee=c.FeeLeg(Decimal("4"), Decimal("0"), False), close_today_fee=None,
            long_margin_rate=Decimal("0.1"), short_margin_rate=Decimal("0.1"),
            limit_up=None, limit_down=None, slippage_ticks=Decimal("1"),
            impact_ticks=Decimal("0"), participation_rate=Decimal("0.1"),
            evidence="SCENARIO", source_sha256=_HASH_ZERO)

    # -- decision-replay fixtures (M4 shape) ----------------------------

    def replay_day(self) -> date:
        return date(2026, 1, 12)

    def replay_ns(self) -> int:
        return 60_000_000_000

    def replay_quality(self, status: str = "READY"):
        return self.contracts.DataQuality(status, (), "reconstructed", "a" * 64)

    def replay_instrument(self, contract: str = "rb"):
        c = self.contracts
        ns = self.replay_ns()
        return c.Instrument(contract, "SHFE.rb2605", "SHFE.RB", "METAL", ("STEEL",),
                            0, 10000 * ns, 10000 * ns, 9000 * ns, self.replay_quality())

    def replay_account(self, cash: str = "100000"):
        c = self.contracts
        value = Decimal(cash)
        ns = self.replay_ns()
        return c.AccountSnapshot("synthetic", 0, self.replay_day(), value, value,
                                 Decimal(0), Decimal(0), Decimal(0), Decimal(0), value,
                                 Decimal(0), Decimal(0), value, Decimal(0), value, (),
                                 False, self.replay_quality())

    def replay_run_policy(self, mode: str = "BEST_EV"):
        c = self.contracts
        return c.RunPolicy("RESEARCH_EVALUATION", mode, 7,
                           "trend_m1_v2" if mode == "FIXED" else None,
                           "c" * 64 if mode == "FIXED" else None,
                           "research-policy-v2.1.0", "macro-risk-v2.1.0",
                           "PRICE_ONLY", "BASE")

    def replay_rule(self, contract: str = "rb"):
        c = self.contracts
        ns = self.replay_ns()
        leg = c.FeeLeg(Decimal(1), Decimal(0), False)
        return c.TradingRule("rule-" + contract, contract, 0, 10000 * ns, 0,
                             Decimal(10), Decimal("0.1"), 1, 1, leg, leg, leg,
                             Decimal("0.1"), Decimal("0.1"), Decimal(200), Decimal(1),
                             Decimal(0), Decimal(0), Decimal("0.1"), "SCENARIO", "a" * 64)

    def replay_bar(self, t: int, price: str = "100", contract: str = "rb"):
        c = self.contracts
        ns = self.replay_ns()
        p = Decimal(price)
        return c.MarketBar("bar-%s-%d" % (contract, t), contract, "SHFE.rb2605", "SHFE.RB",
                           60, t, t, t + ns, self.replay_day(), "session", t + ns, None,
                           p, p, p, p, Decimal(30), Decimal(100), Decimal(100), True, 1, 1,
                           "SYNTHETIC", self.replay_quality())


# ---------------------------------------------------------------------------
# Case: v2/feature_prefix_cold and v2/feature_prefix_hot
# ---------------------------------------------------------------------------

class FeaturePrefixWorkload:
    """``build_feature_snapshot`` over pre-registered increasing prefixes.

    Tiers (frozen in ``fixtures/v2/feature_prefix.json``): prefixes of 90,
    150 and 300 minute bars of one 400-slot DAY session; every evaluation
    cuts off at the completion time of the prefix's last planned slot (the
    same slot-aligned decision semantics for every tier).

    * ``cold``: every measured operation clears the production module's
      content-keyed caches first (the clearing is part of the timed
      operation) and then builds one full snapshot - one independent
      cold construction per operation.
    * ``hot``: decisions advance one minute per operation over the same
      fixture (the per-minute production pattern); caches are never
      cleared and harness warmup primes them.  Cache churn (the
      production prefix cache resets at its frozen size limit) is part of
      the honest hot-path measurement.

    Correctness: every operation records the snapshot identity, factor
    keys, statuses, values, reasons and availability; the cold case also
    asserts snapshot-id equality with the first hot-path snapshot built
    from the same inputs (byte-identical outputs across cache states).
    """

    def __init__(self, ctx: Any, mode: str) -> None:
        self.ctx = ctx
        self.mode = mode
        self.factors = None
        self.records: List[Dict[str, Any]] = []
        self.ready_values = 0
        self.pit_violations = 0
        self.cache_identity_checks = 0
        self._reference_ids: Dict[int, str] = {}

    def setup(self, ctx: Any) -> None:
        fixture = _checked_params(ctx, "feature_prefix",
                                  ("case_family", "product", "contract"))
        if self.mode not in ("cold", "hot"):
            raise RuntimeError("unknown feature mode %r" % self.mode)
        if fixture["case_family"] != "FEATURE_PREFIX":
            raise RuntimeError("fixture case_family mismatch")
        self.tiers: List[int] = list(fixture["tiers"])
        self.expected_values = int(fixture["expected_values_per_snapshot"])
        self.bars_total = int(fixture["bars_total"])
        kit = _V2FixtureKit()
        session = kit.feature_session(date(2026, 1, 5), 0, int(fixture["session_slots"]),
                                      "DAY", int(fixture["session_hour"]))
        self.session = session
        self.bars = tuple(kit.feature_bar(session.start_nano + i * MINUTE, i, session)
                          for i in range(self.bars_total))
        self.rule = kit.feature_rule(session.start_nano, session.end_nano)
        factor_specs = _import_measured("ak_trading.v2.factor_specs")
        self.specs = factor_specs.factor_registry_v2(60)
        self.factors = _import_measured("ak_trading.v2.factors")
        self.hot_base = int(fixture["hot_base_index"])
        self.cursor = 0
        self.records = []
        self.ready_values = 0
        self.pit_violations = 0
        self.cache_identity_checks = 0
        self._reference_ids = {}

    def _clear_production_caches(self) -> None:
        self.factors._GRID_CACHE.clear()
        self.factors._PREFIX_CACHE.clear()
        self.factors._WILDER_STATE_CACHE.clear()

    def _one_snapshot(self, length: int, decision: int) -> None:
        snapshot = self.factors.build_feature_snapshot(
            "RB2610", self.bars[:length], decision, self.specs, None, None,
            self.rule, (self.session,))
        record = {
            "length": length,
            "decision": decision,
            "snapshot_id": snapshot.snapshot_id,
            "status": snapshot.quality.status,
            "reasons": list(snapshot.quality.reasons),
            "available_at": snapshot.available_at_nano,
            "values": [
                [value.factor_key, value.quality.status,
                 None if value.value is None else str(value.value),
                 list(value.quality.reasons), value.available_at_nano]
                for value in snapshot.values
            ],
        }
        self.records.append(record)
        if snapshot.available_at_nano > decision:
            self.pit_violations += 1
        self.ready_values += sum(
            1 for value in snapshot.values if value.quality.status == "READY")
        # Cold/hot byte-identity probe: the first snapshot built for a
        # given (tier, decision) must keep the same snapshot_id no matter
        # which cache state produced it.
        key = (length, decision)
        if key in self._reference_ids:
            if self._reference_ids[key] != snapshot.snapshot_id:
                raise RuntimeError("snapshot identity changed across cache states")
            self.cache_identity_checks += 1
        else:
            self._reference_ids[key] = snapshot.snapshot_id

    def operation(self, ctx: Any) -> Dict[str, Any]:
        if self.mode == "cold":
            tier = self.tiers[self.cursor % len(self.tiers)]
            self._clear_production_caches()
            decision = self.session.start_nano + tier * MINUTE
            self._one_snapshot(tier, decision)
        else:
            index = self.hot_base + self.cursor
            if index >= self.bars_total:
                return {"ok": False, "units": 0,
                        "reason": "HOT_CURSOR_EXHAUSTED_bars_total"}
            decision = self.session.start_nano + (index + 1) * MINUTE
            self._one_snapshot(index + 1, decision)
        self.cursor += 1
        return {"ok": True, "units": 1}

    def teardown(self, ctx: Any) -> None:
        pass

    def verify(self, ctx: Any) -> Dict[str, Any]:
        reasons: List[str] = []
        if not self.records:
            reasons.append("no snapshots built")
        if self.pit_violations:
            reasons.append("available_at exceeded decision in %d ops" % self.pit_violations)
        if self.ready_values == 0:
            reasons.append("no READY factor values (illegal-input early exit is not a benchmark)")
        if self.records and any(record["status"] == "WARMUP" for record in self.records):
            reasons.append("warmup snapshot in measured set")
        if self.records and len(self.records[0]["values"]) != self.expected_values:
            reasons.append("unexpected factor row count %d" % len(self.records[0]["values"]))
        # Untimed cache-state independence probe (hot mode): rebuild the
        # last evaluated (prefix, decision) from cleared caches; the
        # snapshot identity must equal the cache-warm measured one (CR005
        # full-prefix/incremental equivalence, checked without polluting
        # the timed operations).
        if self.records and self.mode == "hot":
            last = self.records[-1]
            self._clear_production_caches()
            probe = self.factors.build_feature_snapshot(
                "RB2610", self.bars[:last["length"]], last["decision"], self.specs,
                None, None, self.rule, (self.session,))
            self.cache_identity_checks += 1
            if probe.snapshot_id != last["snapshot_id"]:
                reasons.append("hot snapshot identity not reproducible from cold caches")
        summary = {
            "mode": self.mode,
            "snapshots": len(self.records),
            "ready_values": self.ready_values,
            "tiers": self.tiers if self.mode == "cold" else None,
            "cache_identity_checks": self.cache_identity_checks,
            "statuses": sorted({record["status"] for record in self.records}),
        }
        return {
            "status": "PASS" if not reasons else "FAIL",
            "digest": _digest({"case": "feature_prefix_%s" % self.mode,
                               "records": self.records}),
            "summary": summary,
            "fail_reasons": reasons,
        }


# ---------------------------------------------------------------------------
# Case: v2/decision_replay_wait_only (M4 smoke seed semantics)
# ---------------------------------------------------------------------------

class DecisionReplayWaitOnlyWorkload:
    """One ``run_decision_replay`` over the M4 smoke seed per operation:
    150 deterministic minute bars, a five-minute window (70..75 minutes),
    WAIT strategy spec, no qualifications, cash-only account.  Pre-
    registered counts live in ``fixtures/v2/decision_replay.json`` and
    verify() enforces them.  Zero fills is the defining property of this
    case (name rule from the NF21 work order)."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.records: List[Dict[str, Any]] = []

    def setup(self, ctx: Any) -> None:
        fixture = _checked_params(ctx, "decision_replay",
                                  ("case_family", "contract", "initial_cash_cny"))
        config = fixture["wait_only"]
        kit = _V2FixtureKit()
        ns = kit.replay_ns()
        day = kit.replay_day()
        models_mod = _import_measured("ak_trading.v2.probability.models")
        regimes_mod = _import_measured("ak_trading.v2.probability.regimes")
        payoffs_mod = _import_measured("ak_trading.v2.decision.payoffs")
        registry_mod = _import_measured("ak_trading.v2.strategies.registry")
        pipeline_mod = _import_measured("ak_trading.v2.decision.pipeline")
        policy_mod = _import_measured("ak_trading.v2.policy")
        contracts = kit.contracts
        self.expected = config["expected"]
        self.start = int(config["window_start_minutes"]) * ns
        self.end = int(config["window_end_minutes"]) * ns
        self.initial_cash = fixture["initial_cash_cny"]
        bar_count = int(config["bars"])
        bars = tuple(
            kit.replay_bar(t=i * ns, price=str(Decimal(100) + Decimal(i) * Decimal("0.1")
                                                + (Decimal("0.1") if i % 3 == 0 else Decimal(0))))
            for i in range(1, bar_count + 1))
        session = contracts.SessionWindow("session", "SHFE.RB", day, ns,
                                          int(config["session_end_minutes"]) * ns,
                                          "DAY", "synthetic", "SCENARIO")
        self.bundle = contracts.FrozenInput(Path("SYNTHETIC"), "a" * 64,
                                            (kit.replay_instrument(),), (session,), bars, (),
                                            (kit.replay_rule(),))
        self.models = models_mod.ForecastModelSet(
            "models", "reference", (), ns, 2 * ns, 1000 * ns,
            models_mod.default_fit_config((), ns, 2 * ns, 1000 * ns), {}, (), {})
        self.payoff = payoffs_mod.fit_path_payoff_model((), ns, "research-policy-v2.1.0")
        self.reference = regimes_mod.RegimeReference(
            "reference", ns, ns, {"SHFE.RB|DAY|60": (0.001, 0.01, 0.1)}, {})
        self.specs = (registry_mod.build_strategy_spec("WAIT", "MINUTE"),)
        self.exit_specs = (pipeline_mod.legacy_exit_spec("MINUTE"),)
        self.bindings = (pipeline_mod.legacy_exit_binding(self.specs[0]),)
        self.run_policy = kit.replay_run_policy()
        self.policy = policy_mod.research_policy()
        self.account_factory = kit.replay_account
        self.replay_mod = _import_measured("ak_trading.v2.decision.replay")
        self.records = []

    def operation(self, ctx: Any) -> Dict[str, Any]:
        result = self.replay_mod.run_decision_replay(
            self.bundle, self.models, self.payoff, self.specs,
            self.account_factory(self.initial_cash), self.run_policy, (), {},
            self.policy, None, self.reference, "a" * 64, None,
            self.start, self.end,
            exit_specs=self.exit_specs, exit_bindings=self.bindings)
        record = {
            "status": result.status,
            "decisions": len(result.decisions),
            "intents": len(result.intents),
            "fills": len(result.fills),
            "execution_results": len(result.execution_results),
            "equity_rows": len(result.equity),
            "terminal_cash": str(result.equity[-1].cash_cny),
            "terminal_equity": str(result.equity[-1].equity_cny),
            "reconciliation": {key: str(value) if isinstance(value, Decimal) else value
                               for key, value in result.reconciliation.items()},
            "decision_ids_digest": _digest(sorted(d.decision_id for d in result.decisions)),
        }
        self.records.append(record)
        return {"ok": True, "units": 1}

    def teardown(self, ctx: Any) -> None:
        pass

    def verify(self, ctx: Any) -> Dict[str, Any]:
        reasons: List[str] = []
        if not self.records:
            reasons.append("no replay runs")
        expected = self.expected
        for record in self.records:
            for key, value in expected.items():
                if key in ("reconciliation_status",):
                    if record["reconciliation"].get("status") != value:
                        reasons.append("reconciliation status %r != %r"
                                       % (record["reconciliation"].get("status"), value))
                elif str(record[key]) != str(value):
                    reasons.append("%s %r != pre-registered %r" % (key, record[key], value))
        summary = {
            "runs": len(self.records),
            "expected_counts": expected,
            "statuses": sorted({record["status"] for record in self.records}),
        }
        return {
            "status": "PASS" if not reasons else "FAIL",
            "digest": _digest({"case": "decision_replay_wait_only",
                               "records": self.records}),
            "summary": summary,
            "fail_reasons": sorted(set(reasons)),
        }


# ---------------------------------------------------------------------------
# Case: v2/decision_replay_filled (scheduler fixture: selection + risk +
# legal next-window execution + account updates)
# ---------------------------------------------------------------------------

class DecisionReplayFilledWorkload:
    """``run_decision_replay`` with SYNTHETIC fixture strategy contexts
    (the ``tests/test_v2_m4_replay.py`` scheduler-fixture pattern,
    replicated in this workload's own fixture code, never imported from
    tests): an OPEN proposal becomes a selected decision, passes the
    account risk filter (``enforce_account_risk`` runs for real), fills
    on the next legal minute bar, exits later and the ledger reconciles.

    Each operation temporarily replaces ``build_strategy_context`` and
    ``strategy_registry_v2`` on the replay module and restores them
    afterwards.  Strategy evaluation / forecast construction are fixture
    overhead inside the timed call and are NOT the measured claim; the
    measured claim is the unmodified replay scheduler + gates + risk +
    execution + accounting path.  Pre-registered counts
    (decisions/intents/fills/fees/terminal cash) live in
    ``fixtures/v2/decision_replay.json`` and are enforced by verify()."""

    def __init__(self, ctx: Any, fixture_identity: bool = True) -> None:
        self.ctx = ctx
        self.fixture_identity = fixture_identity
        self.records: List[Dict[str, Any]] = []

    def setup(self, ctx: Any) -> None:
        fixture = _load_fixture_params("decision_replay")
        if self.fixture_identity:
            _checked_params(ctx, "decision_replay",
                            ("case_family", "contract", "initial_cash_cny"))
            if fixture["case_family"] != "DECISION_REPLAY":
                raise RuntimeError("fixture case_family mismatch")
        config = fixture["filled"]
        self.expected = config["expected"]
        self.initial_cash = fixture["initial_cash_cny"]
        kit = _V2FixtureKit()
        self.kit = kit
        ns = kit.replay_ns()
        day = kit.replay_day()
        self.ns = ns
        self.start = int(config["window_start_minutes"]) * ns
        self.end = int(config["window_end_minutes"]) * ns
        contracts = kit.contracts
        self.contracts = contracts
        models_mod = _import_measured("ak_trading.v2.probability.models")
        regimes_mod = _import_measured("ak_trading.v2.probability.regimes")
        payoffs_mod = _import_measured("ak_trading.v2.decision.payoffs")
        pipeline_mod = _import_measured("ak_trading.v2.decision.pipeline")
        registry_mod = _import_measured("ak_trading.v2.strategies.registry")
        execution_mod = _import_measured("ak_trading.v2.decision.execution")
        policy_mod = _import_measured("ak_trading.v2.policy")
        schemas_mod = _import_measured("ak_trading.v2.schemas")

        # deterministic 40-bar window fixture; the bar at minute 106 is
        # locked (all prices 1) exactly as in the M4 scheduler test.
        bars = tuple(kit.replay_bar(t=t * ns, price="100")
                     for t in range(int(config["bars_from"]),
                                    int(config["bars_to"])))
        locked = int(config["locked_minute"]) * ns
        bars = tuple(
            contracts.MarketBar(
                b.bar_id, b.contract_id, b.raw_symbol, b.product, b.duration_seconds,
                b.datetime_nano, b.start_nano, b.end_nano, b.trading_day, b.session_id,
                b.available_at_nano, b.imported_at_nano, Decimal(1), Decimal(1),
                Decimal(1), Decimal(1), b.volume, b.open_oi, b.close_oi, b.complete,
                b.expected_minutes, b.actual_minutes, b.source, b.quality)
            if b.start_nano == locked else b for b in bars)
        session = contracts.SessionWindow("session", "SHFE.RB", day, ns,
                                          int(config["session_end_minutes"]) * ns,
                                          "DAY", "synthetic", "SCENARIO")
        self.bundle = contracts.FrozenInput(Path("SYNTHETIC"), "a" * 64,
                                            (kit.replay_instrument(),), (session,),
                                            bars, (), (kit.replay_rule(),))
        self.models = models_mod.ForecastModelSet(
            "models", "reference", (), ns, 2 * ns, 1000 * ns,
            models_mod.default_fit_config((), ns, 2 * ns, 1000 * ns), {}, (), {})
        self.reference = regimes_mod.RegimeReference(
            "reference", ns, ns, {"SHFE.RB|DAY|60": (0.001, 0.01, 0.1)}, {})
        # base proposal/context + payoff observations (frozen shapes from
        # the M4 gates/payoffs test fixtures)
        self._example = self._build_example(registry_mod, pipeline_mod, policy_mod,
                                            payoffs_mod, schemas_mod)
        proposal, context = self._example
        observations = self._observations(payoffs_mod, proposal, context,
                                          int(config["payoff_observation_blocks"]),
                                          int(config["payoff_observation_per_block"]))
        self.payoff_model = payoffs_mod.fit_path_payoff_model(
            observations, 3 * ns, context.run_policy.research_policy_version)
        self.payoff_hash = self.payoff_model.content_hash()
        self.spec = context.spec
        self.exit_specs = (pipeline_mod.legacy_exit_spec("MINUTE"),)
        self.bindings = (pipeline_mod.legacy_exit_binding(self.spec),)
        self.run_policy = kit.replay_run_policy()
        self.policy = policy_mod.research_policy()
        self.account_factory = kit.replay_account
        self.replay_mod = _import_measured("ak_trading.v2.decision.replay")
        self.records = []

    # -- SYNTHETIC example proposal/context (M4 gates test shape) -------

    def _build_example(self, registry_mod, pipeline_mod, policy_mod, payoffs_mod,
                       schemas_mod):
        kit = self.kit
        contracts = self.contracts
        ns = self.ns
        spec = registry_mod.build_strategy_spec("TREND", "MINUTE")
        now = 100 * ns
        rule = kit.replay_rule()
        quality = kit.replay_quality
        fs = contracts.FeatureSnapshot(
            "features", "rb", "SHFE.rb2605", "SHFE.RB", kit.replay_day(), "session",
            "DAY", now, now, 60, ("bar-rb-5940000000000",), Decimal(100), Decimal(1),
            tuple(contracts.FactorValue(key, Decimal(1), now, quality())
                  for key in spec.required_factor_keys)
            + (contracts.FactorValue("META:price_tick:v2.1.0", Decimal("0.1"), now, quality()),),
            None, quality())
        regime = contracts.RegimeAssessment("regime", fs.snapshot_id, "reference", now,
                                            "UP_TREND", (), None, "ALIGNED", 3, (), quality())
        horizon = contracts.Horizon("TRADING_MINUTE", 30)
        cp = policy_mod.confidence_policy("TREND")
        events = (("PATH", "PATH", "ALL"), ("PERSIST:UP_TREND", "UP_TREND", "[0.85,0.90)"))
        evidence = tuple(
            contracts.CalibrationEvidence(
                "e-" + event, "model", cp.policy_id, "SHFE.RB", "DAY", 60, regime.base,
                "ATR_1" if event == "PATH" else "COMMON",
                schemas_mod.calibration_geometry_key(fs, None, geometry), event, horizon,
                "path-label-v2.1.0", bucket, 600, 80, 0.01, 0.1, ns, 2 * ns, 300 * ns,
                "VALID")
            for event, geometry, bucket in events)
        keys = ("Q05", "Q25", "Q50", "Q75", "Q95")
        forecast = contracts.MarketForecast(
            "forecast", "request", fs.snapshot_id, "rb", "SHFE.RB", kit.replay_day(),
            "session", "DAY", 60, regime.assessment_id, regime.base, "reference", now,
            now, 102 * ns, horizon, 101 * ns, 131 * ns, "ATR_1", Decimal(100), Decimal(1),
            Decimal(1), Decimal(1), None, "path-label-v2.1.0", "model",
            {"UP": 0.6, "DOWN": 0.3, "NEUTRAL": 0.1},
            dict(zip(keys, [-0.02, -0.01, 0.0, 0.01, 0.02])),
            dict(zip(keys, map(Decimal, [-2, -1, 0, 1, 2]))),
            dict(zip(keys, [-2.0, -1.0, 0.0, 1.0, 2.0])),
            {"UP_FIRST": 0.6, "DOWN_FIRST": 0.4, "NEITHER": 0.0, "AMBIGUOUS": 0.0}, (),
            {"UP_TREND": 0.85}, {}, None, evidence, False, quality())
        budget = self._frozen_budget(pipeline_mod, policy_mod, ns, now)
        next_state = contracts.StrategyState(
            spec.strategy_id, budget.episode_id, "rb", "WAIT_TRIGGER", None, None, 1,
            (), (0,), None, None, 131 * ns, (), budget, 3, None, None)
        proposal = contracts.StrategyProposal(
            "proposal", spec.strategy_id, budget.episode_id, "rb", now, "OPEN", 1, 0,
            Decimal(100), budget.target_price, budget.stop_price, 101 * ns, 102 * ns,
            forecast.forecast_id, regime.assessment_id, budget, None, next_state, ())
        geometry = schemas_mod.calibration_geometry_key(fs, None, "UP_TREND")
        payoff = contracts.PayoffEstimate(
            "payoff", proposal.proposal_id, "payoff:" + "a" * 64, horizon, "ATR_1",
            geometry, "opportunity",
            pipeline_mod.legacy_exit_binding(spec).binding_sha256, "OPEN_INITIAL",
            3 * ns, 600, 80, Decimal(15), Decimal(10), 0.8, Decimal(30), Decimal(100),
            None, 0.0, "READY", ())
        proposal = replace(proposal, payoff=payoff)
        macro = contracts.MacroContext("macro", now, (), "macro-risk-v2.1.0", (),
                                       "PRICE_ONLY", quality())
        context = contracts.StrategyContext(
            now, spec, pipeline_mod.legacy_exit_binding(spec), fs, (forecast,), regime,
            macro, tuple(self.kit.replay_bar(t=i * ns) for i in range(80, 100)),
            (contracts.SessionWindow("session", "SHFE.RB", self.kit.replay_day(), ns,
                                     300 * ns, "DAY", "synthetic", "SCENARIO"),),
            self.kit.replay_account("100000"), None, rule, cp, policy_mod.stress_policy(),
            self.kit.replay_run_policy(), None,
            {"forecast": "a" * 64, "payoff": "a" * 64, "regime_reference": "a" * 64},
            200 * ns)
        return proposal, context

    def _frozen_budget(self, pipeline_mod, policy_mod, ns, now):
        contracts = self.contracts
        layers = (contracts.LayerSpec(0, Decimal(100), Decimal(100), 1),)
        return contracts.RoundBudget(
            budget_id="budget-episode", episode_id="episode", contract_id="rb",
            frozen_at_nano=now, range_id=None, frozen_range=None,
            stress_policy=policy_mod.stress_policy(), initial_atr_points=Decimal(1),
            side=1, layers=layers, stop_price=Decimal(94), target_price=Decimal(104),
            latest_exit_nano=131 * ns, maximum_lots=1,
            planned_loss_cny=Decimal(63), stress_loss_cny=Decimal(105),
            reserved_margin_cny=Decimal(150), day_loss_limit_cny=Decimal(2000),
            equity_at_start_cny=Decimal(100000))

    def _observations(self, payoffs_mod, proposal, context, blocks, per_block):
        import json as _json
        key = payoffs_mod.condition_key(proposal, context)
        values = _json.loads(key)
        contracts = self.contracts
        day0 = date(2025, 1, 1)
        profits = ("80", "-30")
        return tuple(
            payoffs_mod.PayoffObservation(
                "obs-%d-%d" % (b, n), proposal.strategy_id, proposal.contract_id,
                context.features.product, key, context.exit_binding.binding_sha256,
                "OPEN_INITIAL", context.spec.max_holding, "ATR_1", values[12],
                b * 10 + 1, b * 10 + 2, b * 10 + 3,
                (day0 + timedelta(days=b)).isoformat(),
                "UP_FIRST" if n % 2 == 0 else "DOWN_FIRST", True,
                Decimal(profits[n % 2]), Decimal(100),
                max(Decimal(0), -Decimal(profits[n % 2])), None, Decimal(values[15]),
                "a" * 64)
            for b in range(blocks) for n in range(per_block))

    # -- fixture strategy + context patching ----------------------------

    def _fixture_context(self, bundle, contract, now, account, previous, forecast_models,
                         payoff_model, spec, seasonal, reference, run_policy, qualifications,
                         *, exit_binding, forecast_provider):
        from dataclasses import replace
        kit = self.kit
        contracts = self.contracts
        ns = self.ns
        _proposal, base = self._example
        context = replace(base, exit_binding=exit_binding)
        fs = replace(context.features, snapshot_id="features-%d" % now, decision_nano=now,
                     available_at_nano=now, reference_price=Decimal(100),
                     values=tuple(replace(v, available_at_nano=now)
                                  for v in context.features.values))
        regime = replace(context.regime, assessment_id="regime-%d" % now,
                         snapshot_id=fs.snapshot_id, decision_nano=now)
        forecast = replace(
            context.forecasts[0], forecast_id="forecast-%d" % now,
            snapshot_id=fs.snapshot_id, assessment_id=regime.assessment_id,
            decision_nano=now, published_at_nano=now, expires_at_nano=now + 2 * ns,
            window_start_nano=now + ns, window_end_nano=now + 31 * ns)
        hashes = {"forecast": "a" * 64, "payoff": self.payoff_hash,
                  "regime_reference": "a" * 64}
        execution_mod = _import_measured("ak_trading.v2.decision.execution")
        return replace(context, now_nano=now, features=fs, forecasts=(forecast,),
                       regime=regime, account=account, spec=spec, run_policy=run_policy,
                       position=execution_mod.position_for(account, contract),
                       macro=replace(context.macro, decision_nano=now),
                       bars=tuple(b for b in bundle.bars if b.end_nano <= now),
                       model_hashes=hashes)

    def _fixture_strategy_class(self):
        from dataclasses import replace
        kit = self.kit
        ns = self.ns
        workload = self

        class FixtureStrategy:
            def __init__(self_inner, spec):
                self_inner.spec = spec

            def evaluate(self_inner, context, previous):
                proposal, _base = workload._example
                now = context.now_nano
                if previous is not None and previous.episode_id:
                    proposal = replace(
                        proposal, proposal_id="exit-%d" % now, decision_nano=now,
                        action="EXIT", target_lots=0,
                        earliest_execution_nano=now + ns,
                        valid_until_nano=now + 2 * ns,
                        next_state=replace(previous, phase="EXITING"),
                        budget=previous.budget,
                        forecast_id=context.forecasts[0].forecast_id,
                        assessment_id=context.regime.assessment_id)
                elif now == 100 * ns:
                    proposal = replace(
                        proposal, forecast_id=context.forecasts[0].forecast_id,
                        assessment_id=context.regime.assessment_id)
                else:
                    registry_mod = _import_measured("ak_trading.v2.strategies.registry")
                    proposal = replace(
                        proposal, proposal_id="wait-%d" % now, decision_nano=now,
                        action="WAIT", target_lots=0, budget=None, episode_id=None,
                        next_state=(previous or registry_mod.fresh_state(
                            self_inner.spec.strategy_id, "rb")),
                        earliest_execution_nano=now + ns,
                        valid_until_nano=now + 2 * ns, forecast_id=None, payoff=None)
                return proposal

        return FixtureStrategy

    def operation(self, ctx: Any) -> Dict[str, Any]:
        strategy_cls = self._fixture_strategy_class()
        spec = self.spec
        saved_context = self.replay_mod.build_strategy_context
        saved_registry = self.replay_mod.strategy_registry_v2
        self.replay_mod.build_strategy_context = self._fixture_context
        self.replay_mod.strategy_registry_v2 = (
            lambda mode: {spec.strategy_id: strategy_cls(spec)})
        try:
            result = self.replay_mod.run_decision_replay(
                self.bundle, self.models, self.payoff_model, (spec,),
                self.account_factory(self.initial_cash), self.run_policy, (), {},
                self.policy, None, self.reference, "a" * 64, None,
                self.start, self.end,
                exit_specs=self.exit_specs, exit_bindings=self.bindings)
        finally:
            self.replay_mod.build_strategy_context = saved_context
            self.replay_mod.strategy_registry_v2 = saved_registry
        intent_created = {intent.intent_id: intent.created_at_nano
                          for intent in result.intents}
        record = {
            "status": result.status,
            "decisions": len(result.decisions),
            "intents": len(result.intents),
            "fills": len(result.fills),
            "execution_results": len(result.execution_results),
            "equity_rows": len(result.equity),
            "fees_cny": str(result.equity[-1].fees_cny),
            "terminal_cash_cny": str(result.equity[-1].cash_cny),
            "terminal_lots": len(result.equity[-1].lots),
            "reconciliation": {key: str(value) if isinstance(value, Decimal) else value
                               for key, value in result.reconciliation.items()},
            "fills_detail": sorted(
                [fill.fill_id, str(fill.execution_price), int(fill.side), int(fill.lots),
                 fill.offset, fill.execution_nano, intent_created.get(fill.intent_id)]
                for fill in result.fills),
            "selected_decisions": sum(1 for d in result.decisions
                                      if d.selected_proposal_id is not None),
        }
        # Event-order probe (as in the M4 test): every fill must execute
        # strictly after its intent became visible.
        for fill in result.fills:
            if fill.execution_nano <= intent_created.get(fill.intent_id, fill.execution_nano):
                record["order_violation"] = True
        self.records.append(record)
        return {"ok": True, "units": 1}

    def teardown(self, ctx: Any) -> None:
        pass

    def verify(self, ctx: Any) -> Dict[str, Any]:
        reasons: List[str] = []
        if not self.records:
            reasons.append("no replay runs")
        expected = self.expected
        for record in self.records:
            if record.get("order_violation"):
                reasons.append("fill executed at or before intent creation")
            for key, value in expected.items():
                if key == "reconciliation_status":
                    if record["reconciliation"].get("status") != value:
                        reasons.append("reconciliation %r != %r"
                                       % (record["reconciliation"].get("status"), value))
                elif key == "selected_decisions":
                    if record["selected_decisions"] < int(value):
                        reasons.append("selected decisions %d < %s"
                                       % (record["selected_decisions"], value))
                elif str(record[key]) != str(value):
                    reasons.append("%s %r != pre-registered %r" % (key, record[key], value))
        summary = {
            "runs": len(self.records),
            "expected_counts": expected,
            "statuses": sorted({record["status"] for record in self.records}),
        }
        return {
            "status": "PASS" if not reasons else "FAIL",
            "digest": _digest({"case": "decision_replay_filled",
                               "records": self.records}),
            "summary": summary,
            "fail_reasons": sorted(set(reasons)),
        }


# ---------------------------------------------------------------------------
# Case: v2/validation_forecasts
# ---------------------------------------------------------------------------

class ValidationForecastsWorkload:
    """``evaluate_forecasts`` over a frozen SYNTHETIC forecast/label set
    (blocks x requests-per-block, fixed seed, fixed bucket/block layout).
    The production 1000-replicate trading-day block bootstrap runs inside
    every call; verify() fails if that semantic ever shrinks."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.records: List[Dict[str, Any]] = []

    def setup(self, ctx: Any) -> None:
        from dataclasses import replace
        fixture = _checked_params(ctx, "validation",
                                  ("case_family", "bootstrap_replicates"))
        config = fixture["forecasts"]
        if int(fixture["bootstrap_replicates"]) != 1000:
            raise RuntimeError("fixture must preserve the frozen 1000-replicate bootstrap")
        self.seed = int(fixture["seed"])
        filled = DecisionReplayFilledWorkload(ctx, fixture_identity=False)
        filled.setup(ctx)
        proposal, context = filled._example
        base = context.forecasts[0]
        self.metrics = _import_measured("ak_trading.v2.validation.metrics")
        self.replicates = int(self.metrics.BOOTSTRAP_REPLICATES)
        contracts = _import_measured("ak_trading.v2.contracts")
        day_nano = 86400 * NANO
        day0 = date(2026, 1, 5)
        forecasts = []
        labels = []
        blocks: Dict[str, str] = {}
        for d in range(int(config["days"])):
            day = day0 + timedelta(days=d)
            for i in range(int(config["requests_per_day"])):
                request_id = "req-%03d-%d" % (d, i)
                forecast = replace(
                    base, forecast_id="fc-%03d-%d" % (d, i), request_id=request_id,
                    snapshot_id="snap-%03d-%d" % (d, i), trading_day=day,
                    decision_nano=base.decision_nano + d * day_nano,
                    published_at_nano=base.published_at_nano + d * day_nano,
                    expires_at_nano=base.expires_at_nano + d * day_nano,
                    window_start_nano=base.window_start_nano + d * day_nano,
                    window_end_nano=base.window_end_nano + d * day_nano)
                forecasts.append(forecast)
                labels.append(contracts.PathLabel(
                    request_id, forecast.contract_id, forecast.label_version,
                    forecast.horizon, forecast.window_start_nano, forecast.window_end_nano,
                    forecast.window_end_nano, "MATURE", (),
                    "UP" if (d + i) % 2 == 0 else "DOWN",
                    0.001 * ((d % 7) - 3), Decimal("1.5"), 0.4,
                    "UP_FIRST" if (d + i) % 3 == 0 else "DOWN_FIRST",
                    5 + (i % 4), None, None, None, None,
                    {key: ((d + i) % 2 == 0) for key in contracts.PERSISTENCE_EVENTS},
                    {}, None))
                blocks[request_id] = "day-%03d" % d
        self.forecasts = tuple(forecasts)
        self.labels = tuple(labels)
        self.blocks = blocks
        self.records = []

    def operation(self, ctx: Any) -> Dict[str, Any]:
        result = self.metrics.evaluate_forecasts(self.forecasts, self.labels,
                                                 self.blocks, self.seed)
        self.records.append(self._record(result))
        return {"ok": True, "units": 1}

    @staticmethod
    def _record(result) -> Dict[str, Any]:
        return {
            "groups": len(result["forecast_groups"]),
            "exclusions": dict(result["exclusions"]),
            "statuses": sorted({group["status"]
                                for group in result["forecast_groups"]}),
            "max_n": max((group["n"] for group in result["forecast_groups"]), default=0),
            "distinct_blocks": max((group["blocks"] for group in result["forecast_groups"]),
                                   default=0),
            "bootstrap": dict(result["bootstrap"]),
            "payload_digest": _digest(result),
        }

    def teardown(self, ctx: Any) -> None:
        pass

    def verify(self, ctx: Any) -> Dict[str, Any]:
        reasons: List[str] = []
        if not self.records:
            reasons.append("no evaluate_forecasts runs")
        for record in self.records:
            if record["bootstrap"].get("replicates") != self.replicates:
                reasons.append("bootstrap replicates shrank to %r"
                               % record["bootstrap"].get("replicates"))
            if record["bootstrap"].get("method") != "TRADING_DAY_BLOCK":
                reasons.append("bootstrap method changed")
            if not record["groups"]:
                reasons.append("no forecast groups produced")
            if record["exclusions"]:
                reasons.append("unexpected exclusions %r" % record["exclusions"])
        summary = {
            "runs": len(self.records),
            "groups": self.records[0]["groups"] if self.records else 0,
            "statuses": self.records[0]["statuses"] if self.records else [],
            "bootstrap_replicates": self.replicates,
        }
        return {
            "status": "PASS" if not reasons else "FAIL",
            "digest": _digest({"case": "validation_forecasts",
                               "records": self.records}),
            "summary": summary,
            "fail_reasons": sorted(set(reasons)),
        }


# ---------------------------------------------------------------------------
# Case: v2/validation_bootstrap
# ---------------------------------------------------------------------------

class ValidationBootstrapWorkload:
    """Direct ``block_bootstrap`` (metrics.py:56) on a frozen value/block
    layout (40 trading-day blocks x 6 values, seed-driven 1000 replicates
    with the production semantics untouched)."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.records: List[Dict[str, Any]] = []

    def setup(self, ctx: Any) -> None:
        fixture = _checked_params(ctx, "validation",
                                  ("case_family", "bootstrap_replicates"))
        config = fixture["bootstrap"]
        if int(fixture["bootstrap_replicates"]) != 1000:
            raise RuntimeError("fixture must preserve the frozen 1000-replicate bootstrap")
        self.metrics = _import_measured("ak_trading.v2.validation.metrics")
        self.replicates = int(self.metrics.BOOTSTRAP_REPLICATES)
        self.seed = int(fixture["seed"])
        per_block = int(config["values_per_block"])
        days = int(config["days"])
        self.values = [float((i % 13) - 6) + 0.25 * (i % 5)
                       for i in range(per_block * days)]
        self.blocks = ["day-%03d" % ((i // per_block) % days)
                       for i in range(per_block * days)]
        self.records = []

    def operation(self, ctx: Any) -> Dict[str, Any]:
        low, high = self.metrics.block_bootstrap(self.values, self.blocks, self.seed)
        self.records.append({"low": low, "high": high, "n": len(self.values),
                             "blocks": len(set(self.blocks)),
                             "seed": self.seed,
                             "replicates": self.replicates})
        return {"ok": True, "units": 1}

    def teardown(self, ctx: Any) -> None:
        pass

    def verify(self, ctx: Any) -> Dict[str, Any]:
        reasons: List[str] = []
        if not self.records:
            reasons.append("no bootstrap runs")
        for record in self.records:
            if record["low"] is None or record["high"] is None:
                reasons.append("bootstrap returned None interval")
            elif record["low"] > record["high"]:
                reasons.append("bootstrap interval inverted")
            if record["replicates"] != 1000:
                reasons.append("bootstrap replicates changed")
        lows = {record["low"] for record in self.records}
        highs = {record["high"] for record in self.records}
        if len(lows) > 1 or len(highs) > 1:
            reasons.append("bootstrap interval not deterministic across runs")
        summary = {
            "runs": len(self.records),
            "blocks": self.records[0]["blocks"] if self.records else 0,
            "values": self.records[0]["n"] if self.records else 0,
            "ci": [self.records[0]["low"], self.records[0]["high"]] if self.records else None,
            "replicates": self.replicates,
        }
        return {
            "status": "PASS" if not reasons else "FAIL",
            "digest": _digest({"case": "validation_bootstrap",
                               "records": self.records}),
            "summary": summary,
            "fail_reasons": sorted(set(reasons)),
        }


# ---------------------------------------------------------------------------
# Case: v2/validation_replay_metrics
# ---------------------------------------------------------------------------

class ValidationReplayMetricsWorkload:
    """``evaluate_replay`` over the filled synthetic replay built once in
    setup (not part of the timed operations).  With one closed synthetic
    episode the production report keeps its honest INSUFFICIENT status;
    the case measures the accounting/validation path, never statistical
    quality."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.records: List[Dict[str, Any]] = []

    def setup(self, ctx: Any) -> None:
        fixture = _checked_params(ctx, "validation",
                                  ("case_family", "bootstrap_replicates"))
        if int(fixture["bootstrap_replicates"]) != 1000:
            raise RuntimeError("fixture must preserve the frozen 1000-replicate bootstrap")
        config = fixture["replay_metrics"]
        self.initial_equity = Decimal(fixture["initial_cash_cny"])
        self.seed = int(fixture["seed"])
        filled = DecisionReplayFilledWorkload(ctx, fixture_identity=False)
        filled.setup(ctx)
        self.filled = filled
        self.replay_result = self._run_once()
        if not self.replay_result.fills:
            raise RuntimeError("setup replay produced no fills; fixture broken")
        episodes = sorted({fill.episode_id for fill in self.replay_result.fills})
        self.block_ids = {episode: config["episode_block"] for episode in episodes}
        self.rules = (filled.kit.replay_rule(),)
        self.metrics = _import_measured("ak_trading.v2.validation.metrics")
        self.replicates = int(self.metrics.BOOTSTRAP_REPLICATES)
        self.records = []

    def _run_once(self):
        filled = self.filled
        strategy_cls = filled._fixture_strategy_class()
        spec = filled.spec
        saved_context = filled.replay_mod.build_strategy_context
        saved_registry = filled.replay_mod.strategy_registry_v2
        filled.replay_mod.build_strategy_context = filled._fixture_context
        filled.replay_mod.strategy_registry_v2 = (
            lambda mode: {spec.strategy_id: strategy_cls(spec)})
        try:
            return filled.replay_mod.run_decision_replay(
                filled.bundle, filled.models, filled.payoff_model, (spec,),
                filled.account_factory("100000"), filled.run_policy, (), {},
                filled.policy, None, filled.reference, "a" * 64, None,
                filled.start, filled.end,
                exit_specs=filled.exit_specs, exit_bindings=filled.bindings)
        finally:
            filled.replay_mod.build_strategy_context = saved_context
            filled.replay_mod.strategy_registry_v2 = saved_registry

    def operation(self, ctx: Any) -> Dict[str, Any]:
        result = self.metrics.evaluate_replay(
            self.replay_result, self.initial_equity, self.block_ids, self.seed,
            rules=self.rules)
        self.records.append({
            "episodes": result["episodes"],
            "status": result["status"],
            "net_pnl_cny": result["net_pnl_cny"],
            "summary_status": result["replay_summary"]["status"],
            "summary_reasons": list(result["replay_summary"]["reasons"]),
            "max_drawdown_ratio": result["max_drawdown_ratio"],
            "admission_rate": result["admission_rate"],
            "bootstrap": dict(result["bootstrap"]),
            "payload_digest": _digest(result),
        })
        return {"ok": True, "units": 1}

    def teardown(self, ctx: Any) -> None:
        pass

    def verify(self, ctx: Any) -> Dict[str, Any]:
        reasons: List[str] = []
        if not self.records:
            reasons.append("no evaluate_replay runs")
        for record in self.records:
            if record["episodes"] < 1:
                reasons.append("no closed episodes accounted")
            if record["bootstrap"].get("replicates") != self.replicates:
                reasons.append("bootstrap replicates shrank")
            if record["summary_status"] != "COMPLETE":
                reasons.append("replay summary not COMPLETE: %r" % record["summary_status"])
        summary = {
            "runs": len(self.records),
            "episodes": self.records[0]["episodes"] if self.records else 0,
            "status": self.records[0]["status"] if self.records else None,
            "bootstrap_replicates": self.replicates,
        }
        return {
            "status": "PASS" if not reasons else "FAIL",
            "digest": _digest({"case": "validation_replay_metrics",
                               "records": self.records}),
            "summary": summary,
            "fail_reasons": sorted(set(reasons)),
        }


# ---------------------------------------------------------------------------
# entrypoint
# ---------------------------------------------------------------------------

_CASES = {
    "feature_prefix_cold": lambda ctx: FeaturePrefixWorkload(ctx, "cold"),
    "feature_prefix_hot": lambda ctx: FeaturePrefixWorkload(ctx, "hot"),
    "decision_replay_wait_only": DecisionReplayWaitOnlyWorkload,
    "decision_replay_filled": DecisionReplayFilledWorkload,
    "validation_forecasts": ValidationForecastsWorkload,
    "validation_bootstrap": ValidationBootstrapWorkload,
    "validation_replay_metrics": ValidationReplayMetricsWorkload,
}


def build(ctx: Any):
    case = ctx.params.get("case")
    factory = _CASES.get(case)
    if factory is None:
        raise RuntimeError("unknown v2 case %r (expected one of %s)"
                           % (case, sorted(_CASES)))
    return factory(ctx)
