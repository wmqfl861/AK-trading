"""EP1-T7-FIXTURE frozen positive-world support helpers (tests only).

Implements the r1 pre-check profile P1 (data/maintenance_artifacts/
v2-ep1-t7-fixture-20260917-r1/precheck-scale-table.md section 4):

* a deterministic SYNTHETIC ramp world (433 trading days: 2 head + 426 outer
  train + 1 outer cal + 2 outer test + 2 maturity-tail days no split uses;
  one continuous 60-minute DAY session per day, minute bars plus real daily
  aggregates so minute TREND gets a real published D1 cross-timeframe background);
* a REAL outer stand-in ``fit_forecast_models`` fit with role-correct outer
  cutoffs (E-P1 arbitration 3.5 / T7-form decision section 4.2: the outer
  fold may be frozen small models only with an independent real generation
  source and outer-role cutoffs - its full provenance is recorded);
* the canonical cold-chain smoke execution: real ``_execute_validation`` ->
  real ``_fit_training_payoffs`` -> the UNMODIFIED production ``_fit_fold``
  on the inner split (observed, never replaced) -> natural candidate windows
  -> ``replay_payoff_training_window`` -> ``observation_from_replay``;
* the frozen acceptance-input manifest (decision section 5.1) reader and
  verifier.

No production switch is added anywhere; the only seams are test-side
monkeypatches that (a) return the frozen outer stand-in fold for the outer
split id and (b) OBSERVE the real inner ``_fit_fold`` call (arguments and
full return value) without altering it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal as D
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

import pytest

NS = 60_000_000_000
DAY_NS = 86_400_000_000_000
BASE = 1700000000 * NS
PRODUCT = "SHFE.RB"
CONTRACT = "SYNTHETIC_UNIT:rb"
FEATURE_KEY = "META:price_tick:v2.1.0"

# --- frozen recipe (pre-check section 4; changing any value re-freezes F) ----
POSITIVE_RECIPE: dict[str, Any] = {
    "recipe_id": "ep1-t7-positive-ramp-breakout-r2",
    "seed": 7,
    "session_minutes": 60,            # >= 60 continuous SYNTHETIC minutes (section 7.1(2))
    "world_days": 433,                # head 2 + train 426 + outer cal 1 + outer test 2
                                       #   + maturity tail 2 (HoldoutScope requires
                                       #   decision_end_nano < maturity_end_nano strictly)
    "head_days": 2,
    "train_days": 426,                # N
    "outer_cal_days": 1,
    "outer_test_days": 2,
    "maturity_tail_days": 2,          # no split uses these days; they only fund the
                                       #   scope's maturity bound beyond the decision window
    "drift_per_bar": "0.5",
    "wiggle": "0.05",
    "bar_band": "0.05",               # high/low = close +- band
    "bar_volume": "1000",
    "shape": ("persistent linear ramp: every completed close stands at least "
              "0.25 above the prior-20-bar high plus one tick, so the real "
              "regime is UP_TREND with the BREAKOUT flag at every decision "
              "and the PERSIST:UP_BREAKOUT M30 label is deterministically true"),
    "labels": ["SYNTHETIC", "SCENARIO"],
}
# inner joint solution (section 3/4): f=0.86, V=51, E=1 -> B=366, C=52, O=8, W=7
INNER_SPLIT_FRACTION = 0.86
INNER_MINIMUM_VALIDATION_DAYS = 51
INNER_EMBARGO_DAYS = 1
INNER_MINIMUM_TRAIN_DAYS = 10
SPLIT_ID = "t7e1-outer"
STRATEGY_FAMILY = "BREAKOUT"
RUN_ID = "m5-ep1t7-positive-cold-1"

_SESSION_MINUTES = POSITIVE_RECIPE["session_minutes"]
_WORLD_DAYS = POSITIVE_RECIPE["world_days"]
_DRIFT = D(POSITIVE_RECIPE["drift_per_bar"])
_WIGGLE = D(POSITIVE_RECIPE["wiggle"])
_BAND = D(POSITIVE_RECIPE["bar_band"])


def _quality():
    from ak_trading.v2.contracts import DataQuality
    return DataQuality("READY", ("SYNTHETIC_UNIT",), "reconstructed", "a" * 64)


def build_positive_world():
    """Deterministic SYNTHETIC channel bundle (single contract, minute+daily)."""
    from ak_trading.v2.contracts import (FeeLeg, FrozenInput, Instrument, MarketBar,
                                         SessionWindow, TradingRule)
    from ak_trading.v2.validation.experiments import digest
    quality = _quality()
    sessions = tuple(
        SessionWindow(f"day-{i}", PRODUCT, date(2024, 1, 1) + timedelta(days=i),
                      BASE + i * DAY_NS, BASE + i * DAY_NS + _SESSION_MINUTES * NS,
                      "DAY", "SYNTHETIC_UNIT", "SCENARIO")
        for i in range(_WORLD_DAYS))
    instrument = Instrument(CONTRACT, "SHFE.rb9901", PRODUCT, "synthetic", ("synthetic",),
                            BASE, None, None, None, quality)
    leg = FeeLeg(D(1), D(0), False)
    rule = TradingRule("unit-rule", CONTRACT, sessions[0].start_nano,
                       sessions[-1].end_nano + 1, sessions[0].start_nano, D(10), D('.1'),
                       1, 1, leg, leg, leg, D('.1'), D('.1'), D(1000), D(1), D(0), D(0),
                       D('.1'), "SCENARIO", "1" * 64)
    bars: list[MarketBar] = []
    for i, session in enumerate(sessions):
        closes: list[D] = []
        day_high = day_low = None
        for j in range(_SESSION_MINUTES):
            start = session.start_nano + j * NS
            k = i * _SESSION_MINUTES + j          # global bar index: one ramp
            level = D(100) + _DRIFT * D(k)
            close = level + _WIGGLE * D((-1) ** k)
            hi, lo = close + _BAND, close - _BAND
            closes.append(close)
            day_high = hi if day_high is None else max(day_high, hi)
            day_low = lo if day_low is None else min(day_low, lo)
            bars.append(MarketBar(
                f"m-{i}-{j}", CONTRACT, "SHFE.rb9901", PRODUCT, 60, start, start,
                start + NS, session.trading_day, session.session_id, start + NS,
                start + NS, close, hi, lo, close, level,
                D(POSITIVE_RECIPE["bar_volume"]), D(POSITIVE_RECIPE["bar_volume"]),
                True, 1, 1, "SYNTHETIC_UNIT", quality))
        bars.append(MarketBar(
            f"d-{i}", CONTRACT, "SHFE.rb9901", PRODUCT, 86400, session.start_nano,
            session.start_nano, session.end_nano, session.trading_day,
            session.session_id, session.end_nano, session.end_nano, closes[0],
            day_high, day_low, closes[-1], level,
            D(POSITIVE_RECIPE["bar_volume"]) * D(_SESSION_MINUTES),
            D(POSITIVE_RECIPE["bar_volume"]) * D(_SESSION_MINUTES),
            True, _SESSION_MINUTES, _SESSION_MINUTES, "SYNTHETIC_UNIT", quality))
    bundle = FrozenInput(Path("SYNTHETIC_UNIT"), digest("t7e1-positive-world-r1"),
                         (instrument,), sessions, tuple(bars), (), (rule,))
    return bundle, sessions


_WORLD_CACHE: dict[str, Any] = {}


def positive_world():
    if "world" not in _WORLD_CACHE:
        _WORLD_CACHE["world"] = build_positive_world()
    return _WORLD_CACHE["world"]


def world_day_bounds(sessions, g: int) -> tuple[int, int]:
    return sessions[g].start_nano, sessions[g].end_nano


def outer_split_bounds(sessions):
    head = POSITIVE_RECIPE["head_days"]
    train_days = POSITIVE_RECIPE["train_days"]
    cal_days = POSITIVE_RECIPE["outer_cal_days"]
    test_days = POSITIVE_RECIPE["outer_test_days"]
    t0, t1 = head, head + train_days - 1
    c0, c1 = t1 + 1, t1 + cal_days
    e0, e1 = c1 + 1, c1 + test_days
    assert e1 + POSITIVE_RECIPE["maturity_tail_days"] == len(sessions) - 1
    return (sessions[t0].start_nano, sessions[t1].end_nano,
            sessions[c0].start_nano, sessions[c1].end_nano,
            sessions[e0].start_nano, sessions[e1].end_nano + 1)


# ---------------------------------------------------------------------------
# outer stand-in: REAL fit with outer-role cutoffs (arbitration 3.5, section 4.2)
# ---------------------------------------------------------------------------

def fit_outer_stand_in(bundle, sessions):
    """Real ``fit_forecast_models`` on real decision-time snapshots.

    TRAIN: one minute snapshot + one daily snapshot per day over train days
    3..300 (outer-role inner prefix). CAL: six minute snapshots plus one daily
    snapshot per day over days 305..366 (62 days -> >=30 distinct trading-day
    blocks and >=200 minute samples for VALID ordinary evidence, needed by the
    T01 ordinary-replay branch that must produce a structurally complete
    natural OPEN). Returns (fold-like dict, provenance dict).
    """
    from ak_trading.v2.contracts import Horizon, MacroContext
    from ak_trading.v2.decision.execution import fee_amount
    from ak_trading.v2.factor_specs import baseline_registry_v2, factor_registry_v2
    from ak_trading.v2.factors import build_feature_snapshot
    from ak_trading.v2.probability.forecast import build_forecast_requests
    from ak_trading.v2.probability.labels import label_forecast
    from ak_trading.v2.probability.models import default_fit_config, fit_forecast_models
    from ak_trading.v2.probability.regimes import assess_regime, fit_regime_reference, freeze_range
    from ak_trading.v2.strategies.registry import build_strategy_spec
    from dataclasses import replace
    rule = bundle.rules[0]
    minute_spec = build_strategy_spec("BREAKOUT", "MINUTE")
    TRAIN_FIRST, TRAIN_LAST = 3, 300
    CAL_FIRST, CAL_LAST = 305, 366
    TRAIN_CUT = TRAIN_LAST
    CAL_END = sessions[CAL_LAST].end_nano
    TRAIN_END = sessions[TRAIN_LAST].end_nano
    MINUTE_HORIZON = minute_spec.max_holding
    DAILY_HORIZON = Horizon("TRADING_DAY", 1)

    def snapshot(duration, when):
        from ak_trading.v2.data import visible_bars
        bars = visible_bars(bundle, CONTRACT, duration, when, duration != 86400)
        return build_feature_snapshot(
            CONTRACT, bars, when,
            factor_registry_v2(duration) + baseline_registry_v2(duration),
            None, None, rule, sessions)

    def collect(day_indices, minute_slots):
        """(features, requests, labels) with ATR_1-only requests per snapshot."""
        features, requests, labels = [], [], []
        for g in day_indices:
            session = sessions[g]
            plans = [(60, MINUTE_HORIZON, session.start_nano + (slot + 1) * NS)
                     for slot in minute_slots]
            plans.append((86400, DAILY_HORIZON, session.end_nano))
            for duration, horizon, when in plans:
                snap = snapshot(duration, when)
                frozen = freeze_range(snap)
                if (snap.reference_price is None or snap.atr_points is None
                        or snap.atr_points <= 0 or frozen is None):
                    continue  # factor warmup (e.g. daily ATR14/prior-20) - skip
                batch = build_forecast_requests(snap, (horizon,), frozen, sessions)
                for request in (r for r in batch if r.barrier_key == "ATR_1"):
                    window = tuple(b for b in bundle.bars
                                   if b.duration_seconds == duration
                                   and request.window_start_nano <= b.start_nano < request.window_end_nano)
                    label = label_forecast(request, window, sessions, (), CAL_END)
                    if label.status != "MATURE":
                        break
                    if g <= TRAIN_CUT and label.available_at_nano is not None \
                            and label.available_at_nano > TRAIN_END:
                        break
                    features.append(snap); requests.append(request); labels.append(label)
                    break
        return features, requests, labels

    train = collect(range(TRAIN_FIRST, TRAIN_LAST + 1), (12,))
    cal = collect(range(CAL_FIRST, CAL_LAST + 1), (2, 6, 10, 14, 18, 22))
    reference = fit_regime_reference(tuple(train[0]), TRAIN_END, TRAIN_END)

    def regime_for(f, ref_id=None):
        price = f.reference_price
        macro = MacroContext("price-only", f.decision_nano, (), "macro-risk-v2.1.0", (),
                             "PRICE_ONLY", f.quality)
        cost = ((fee_amount(rule.open_fee, price, 1, rule.multiplier)
                 + max(fee_amount(leg, price, 1, rule.multiplier)
                       for leg in (rule.close_fee, rule.close_today_fee))) / rule.multiplier
                + 2 * rule.price_tick * (rule.slippage_ticks + rule.impact_ticks))
        regime = assess_regime(f, macro, None, freeze_range(f), cost, reference, None)
        return replace(regime, reference_id=ref_id) if ref_id is not None else regime

    fit = default_fit_config((FEATURE_KEY,), TRAIN_END, CAL_END, sessions[-1].end_nano + 1)
    fit.update({"seed": POSITIVE_RECIPE["seed"], "inner_split_fraction": 0.70,
                "inner_minimum_train_days": 10, "inner_minimum_validation_days": 5,
                "inner_embargo_days": 1})
    models = fit_forecast_models(
        tuple(train[0]), tuple(regime_for(f, "outer-stand-in-train") for f in train[0]),
        tuple(train[1]), tuple(train[2]),
        tuple(cal[0]), tuple(regime_for(f) for f in cal[0]),
        tuple(cal[1]), tuple(cal[2]),
        fit, reference, seasonality=None)
    fold = {"snapshots": (), "seasonal": None, "reference": reference, "requests": (),
            "labels": (), "assignments": (), "models": models, "forecasts": (),
            "exclusions": []}
    provenance = {
        "kind": "REAL_FIT_FORECAST_MODELS_OUTER_STAND_IN",
        "train_days": [TRAIN_FIRST, TRAIN_LAST], "cal_days": [CAL_FIRST, CAL_LAST],
        "train_minute_slots": (12,), "cal_minute_slots": (2, 6, 10, 14, 18, 22),
        "train_samples": len(train[1]), "cal_samples": len(cal[1]),
        "training_end_nano": TRAIN_END, "calibration_end_nano": CAL_END,
        "valid_until_nano": sessions[-1].end_nano + 1,
        "fit_config": dict(fit), "model_set_id": models.model_set_id,
        "reference_id": reference.reference_id,
        "evidence_rows": len(models.evidence),
        "valid_evidence_rows": sum(1 for e in models.evidence if e.status == "VALID"),
    }
    return fold, provenance


def outer_stand_in():
    if "stand_in" not in _WORLD_CACHE:
        bundle, sessions = positive_world()
        _WORLD_CACHE["stand_in"] = fit_outer_stand_in(bundle, sessions)
    return _WORLD_CACHE["stand_in"]


# ---------------------------------------------------------------------------
# research config for the positive world
# ---------------------------------------------------------------------------

def positive_config(bundle, sessions, run_id: str) -> dict[str, Any]:
    from ak_trading.v2.schemas import parse_json_bytes
    from ak_trading.v2.strategies.registry import build_strategy_spec, select_exit_binding
    from ak_trading.v2.validation.experiments import digest
    from ak_trading.v2.probability.models import default_fit_config
    spec = build_strategy_spec("BREAKOUT", "MINUTE")
    binding = select_exit_binding(spec).binding_sha256
    train_start, train_end, cal_start, cal_end, test_start, test_end = outer_split_bounds(sessions)
    scope = {"market_family": "CN_FUTURES_V2", "products": [PRODUCT],
             "contract_ids": [CONTRACT],
             "decision_start_nano": test_start, "decision_end_nano": test_end,
             "maturity_end_nano": sessions[-1].end_nano + 1,
             "calendar_sha256": digest(sessions)}
    split = {"split_id": SPLIT_ID,
             "train_start_nano": train_start, "train_end_nano": train_end,
             "calibration_start_nano": cal_start, "calibration_end_nano": cal_end,
             "test_start_nano": test_start, "test_end_nano": test_end,
             "embargo_days": INNER_EMBARGO_DAYS, "embargo_minutes": 60, "final_holdout": False}
    fit = dict(default_fit_config((FEATURE_KEY,), train_end, cal_end, test_end + 1))
    fit.update({"seed": POSITIVE_RECIPE["seed"], "inner_split_fraction": INNER_SPLIT_FRACTION,
                "inner_minimum_train_days": INNER_MINIMUM_TRAIN_DAYS,
                "inner_minimum_validation_days": INNER_MINIMUM_VALIDATION_DAYS,
                "inner_embargo_days": INNER_EMBARGO_DAYS})
    config = parse_json_bytes((Path(__file__).parent / "fixtures/v2/m5_research.json").read_bytes())
    config.update({
        "run_id": run_id,
        "contract_lock_path": "data/research_runs/v2/contracts-2.1.3/contract-lock.p3-r1.json",
        "contract_ids": [CONTRACT], "strategy_ids": [spec.strategy_id],
        "holdout_scope": scope, "holdout_id": digest(scope),
        "split_specs": [split], "model_fit_configs": {SPLIT_ID: fit},
        "required_group_keys": [],
        "evaluation_as_of_nano": sessions[-1].end_nano + 1,
        "run_policies": [
            {"purpose": "RESEARCH_EVALUATION", "routing_mode": "BEST_EV", "seed": 7,
             "fixed_strategy_id": None, "fixed_exit_binding_sha256": None,
             "research_policy_version": "research-policy-v2.1.0",
             "macro_policy_version": "macro-risk-v2.1.0",
             "macro_mode": "PRICE_ONLY", "cost_scenario": "BASE"},
            {"purpose": "RESEARCH_EVALUATION", "routing_mode": "FIXED", "seed": 7,
             "fixed_strategy_id": spec.strategy_id, "fixed_exit_binding_sha256": binding,
             "research_policy_version": "research-policy-v2.1.0",
             "macro_policy_version": "macro-risk-v2.1.0",
             "macro_mode": "PRICE_ONLY", "cost_scenario": "BASE"},
        ],
    })
    return declare_plan_set(bundle, config)


def declare_plan_set(bundle, config):
    """Freeze and declare the deterministic plan set (v2.1.3 schema field)."""
    from ak_trading.v2.strategies.registry import strategy_specs_v2
    from ak_trading.v2.validation.experiments import (
        _payoff_training_plans, payoff_training_plan_set_sha256)
    from ak_trading.v2.validation.splits import SplitSpec
    specs = tuple(s for mode in ("MINUTE", "DAILY") for s in strategy_specs_v2(mode)
                  if s.strategy_id in config["strategy_ids"])
    plans = [entry[3] for s in config["split_specs"]
             for entry in _payoff_training_plans(bundle, config, SplitSpec(**s), specs)[0]]
    digest_ = payoff_training_plan_set_sha256(plans)
    config = {**config, "schema_version": "ak_probability_research_config_v2.1.3",
              "payoff_training_plan_sha256": digest_}
    return config, digest_


# ---------------------------------------------------------------------------
# canonical cold chain
# ---------------------------------------------------------------------------

@dataclass
class InnerFitCapture:
    calls: list[dict[str, Any]] = field(default_factory=list)

    def record(self, bundle, config, split, specs, fold):
        from collections import Counter
        assignments = fold["assignments"]
        models = fold["models"]
        self.calls.append({
            "split_id": split.split_id,
            "bundle_manifest": bundle.manifest_sha256,
            "fit_config": dict(config["model_fit_configs"][split.split_id]),
            "partition_histogram": dict(Counter(a.partition for a in assignments)),
            "models_none": models is None,
            "model_set_id": None if models is None else models.model_set_id,
            "selected_components": None if models is None else sorted(
                k for k, v in models.models.items() if str(v.get("fit_status", "")).startswith("SELECTED")),
            "failed_components": None if models is None else sorted(
                k for k, v in models.models.items()
                if str(v.get("fit_status", "")).startswith("FAILED")),
            "evidence_total": 0 if models is None else len(models.evidence),
            "evidence_valid": 0 if models is None else sum(
                1 for e in models.evidence if e.status == "VALID"),
            "evidence_rows": [] if models is None else [
                {"event": e.event, "unit": e.horizon.unit, "value": e.horizon.value,
                 "barrier": e.barrier_key, "bucket": e.bucket,
                 "samples": e.sample_count, "blocks": e.effective_blocks,
                 "error": None if e.calibration_error is None else round(e.calibration_error, 6),
                 "status": e.status}
                for e in models.evidence],
            "snapshots": len(fold["snapshots"]), "requests": len(fold["requests"]),
        })


@dataclass
class ColdRunRecord:
    run_id: str
    readiness: Any
    output: Path
    runroot: Path
    config: dict[str, Any]
    declared_plan: str
    bundle: Any
    sessions: Any
    stand_in_provenance: dict[str, Any]
    inner_fit: InnerFitCapture
    elapsed_seconds: float


def _bypass_lock(mp):
    import ak_trading.v2
    import ak_trading.v2.artifacts as artifacts
    mp.setattr(artifacts, "verify_contract_lock", lambda lock, root: None, raising=True)
    mp.setattr(ak_trading.v2, "DOCUMENT_VERSION", "devplan-v2.1.6", raising=True)


def execute_positive_smoke(mp, runroot: Path, output: Path, run_id: str = RUN_ID) -> ColdRunRecord:
    """The canonical owner cold chain (one per fresh process).

    Patches only: lock bridge (merge window, as the baseline tests), RUN_ROOT,
    and the ``_fit_fold`` dispatcher — outer split id returns the frozen outer
    stand-in fold; every inner ``-payoff-inner`` call on the positive world
    runs the UNMODIFIED production function with an observing wrapper.
    """
    import time as _time
    from ak_trading.v2.validation import experiments as ex
    bundle, sessions = positive_world()
    stand_in, provenance = outer_stand_in()
    config, declared_plan = positive_config(bundle, sessions, run_id)
    capture = InnerFitCapture()
    real_fit_fold = ex._fit_fold
    positive_manifest = bundle.manifest_sha256

    def dispatcher(bundle_, config_, split_, specs_):
        if bundle_.manifest_sha256 != positive_manifest:
            return real_fit_fold(bundle_, config_, split_, specs_)
        if split_.split_id == SPLIT_ID:
            return stand_in
        result = real_fit_fold(bundle_, config_, split_, specs_)
        if split_.split_id.endswith("-payoff-inner"):
            capture.record(bundle_, config_, split_, specs_, result)
        return result

    _bypass_lock(mp)
    mp.setattr(ex, "RUN_ROOT", runroot)
    mp.setattr(ex, "_fit_fold", dispatcher)
    started = _time.perf_counter()
    readiness = ex._execute_validation(bundle, config, output, "smoke")
    elapsed = _time.perf_counter() - started
    return ColdRunRecord(run_id=run_id, readiness=readiness, output=output,
                         runroot=runroot, config=config, declared_plan=declared_plan,
                         bundle=bundle, sessions=sessions,
                         stand_in_provenance=provenance, inner_fit=capture,
                         elapsed_seconds=elapsed)


@pytest.fixture(scope="session")
def positive_cold_run(tmp_path_factory):
    """Session-wide canonical cold chain; runs once per fresh pytest process.

    ``_execute_validation`` refuses a pre-existing output directory
    (experiments.py ``output_directory_exists``), so the record's output is a
    NOT-yet-existing child of the reserved temp parent (r1 correction: the
    interrupted 17:49 run passed the mktemp-created directory itself and the
    session fixture errored before registration)."""
    runroot = tmp_path_factory.mktemp("t7e1-runroot")
    output = tmp_path_factory.mktemp("t7e1-output") / "out"
    from ak_trading.v2.validation import experiments as ex
    assert not getattr(ex, "_T7E1_COLD_DONE", False)
    try:
        ex._T7E1_COLD_DONE = True
        mp = pytest.MonkeyPatch()
        record = execute_positive_smoke(mp, runroot, output)
        yield record
    finally:
        ex._T7E1_COLD_DONE = False
        try:
            mp.undo()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# frozen acceptance-input manifest (decision section 5.1)
# ---------------------------------------------------------------------------

FIXTURE_DIR = Path(__file__).parent / "fixtures/v2/ep1_t7"
MANIFEST_NAME = "acceptance-input-manifest.json"


def read_acceptance_manifest() -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / MANIFEST_NAME).read_bytes().decode("utf-8"))


def verify_acceptance_manifest() -> list[str]:
    """Every listed file must exist with the exact byte length and SHA-256;
    the fixture tree must contain nothing extra; regeneration from the recipe
    must reproduce the recorded world identity."""
    problems: list[str] = []
    manifest = read_acceptance_manifest()
    root = Path(__file__).resolve().parents[1]
    seen = set()
    for rel, meta in sorted(manifest["files"].items()):
        path = FIXTURE_DIR / rel
        seen.add(rel)
        if not path.is_file():
            problems.append(f"missing:{rel}")
            continue
        raw = path.read_bytes()
        if len(raw) != meta["bytes"]:
            problems.append(f"length:{rel}")
        if sha256(raw).hexdigest() != meta["sha256"]:
            problems.append(f"sha256:{rel}")
    actual = {p.relative_to(FIXTURE_DIR).as_posix()
              for p in FIXTURE_DIR.rglob("*") if p.is_file()}
    extra = actual - seen - {MANIFEST_NAME}
    if extra:
        problems.append(f"extra_files:{sorted(extra)}")
    for rel, expected in sorted(manifest.get("code_files", {}).items()):
        path = root / rel
        if not path.is_file():
            problems.append(f"missing_code:{rel}")
            continue
        if sha256(path.read_bytes()).hexdigest() != expected:
            problems.append(f"code_drift:{rel}")
    bundle, sessions = positive_world()
    from ak_trading.v2.validation.experiments import digest
    if digest(sessions) != manifest["derived_identity"]["calendar_sha256"]:
        problems.append("regenerated_calendar_mismatch")
    if bundle.manifest_sha256 != manifest["derived_identity"]["bundle_manifest_sha256"]:
        problems.append("regenerated_bundle_mismatch")
    return problems


def freeze_acceptance_manifest() -> dict[str, Any]:
    """Owner-side one-time freeze: materialize the raw inputs and write F.

    Run: python -B -X utf8 tests/v2_ep1_t7_support.py freeze
    """
    from ak_trading.v2.artifacts import canonical_json_bytes
    from ak_trading.v2.validation.experiments import content, digest
    from ak_trading.v2.validation.experiments import code_hashes
    bundle, sessions = positive_world()
    inputs = FIXTURE_DIR / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    (FIXTURE_DIR / "recipe.json").write_bytes(
        canonical_json_bytes(POSITIVE_RECIPE).replace(b",", b",\n"))
    partition = {
        "split_id": SPLIT_ID,
        "split": None,  # filled below from the real bounds
        "fit_config_overrides": {
            "seed": POSITIVE_RECIPE["seed"],
            "inner_split_fraction": INNER_SPLIT_FRACTION,
            "inner_minimum_train_days": INNER_MINIMUM_TRAIN_DAYS,
            "inner_minimum_validation_days": INNER_MINIMUM_VALIDATION_DAYS,
            "inner_embargo_days": INNER_EMBARGO_DAYS,
        },
        "policy_floors_unchanged": {"minimum_samples": 200, "minimum_blocks": 30},
    }
    bounds = outer_split_bounds(sessions)
    partition["split"] = {
        "train_start_nano": bounds[0], "train_end_nano": bounds[1],
        "calibration_start_nano": bounds[2], "calibration_end_nano": bounds[3],
        "test_start_nano": bounds[4], "test_end_nano": bounds[5],
        "embargo_days": INNER_EMBARGO_DAYS, "embargo_minutes": 60, "final_holdout": False,
    }
    (FIXTURE_DIR / "partition-params.json").write_bytes(
        canonical_json_bytes(partition).replace(b",", b",\n"))
    minute_bars = tuple(b for b in bundle.bars if b.duration_seconds == 60)
    daily_bars = tuple(b for b in bundle.bars if b.duration_seconds == 86400)
    (inputs / "sessions.json").write_bytes(
        canonical_json_bytes({"sessions": [content(s) for s in sessions]}))
    (inputs / "bars-minute.json").write_bytes(
        canonical_json_bytes({"bars": [content(b) for b in minute_bars]}))
    (inputs / "bars-daily.json").write_bytes(
        canonical_json_bytes({"bars": [content(b) for b in daily_bars]}))
    (inputs / "rule.json").write_bytes(
        canonical_json_bytes({"rules": [content(r) for r in bundle.rules]}))

    def entry(path: Path) -> dict[str, Any]:
        raw = path.read_bytes()
        return {"bytes": len(raw), "sha256": sha256(raw).hexdigest()}

    files = {rel: entry(FIXTURE_DIR / rel) for rel in sorted(
        "recipe.json partition-params.json inputs/sessions.json "
        "inputs/bars-minute.json inputs/bars-daily.json inputs/rule.json".split())}
    root = Path(__file__).resolve().parents[1]
    code = code_hashes()
    for rel in ("tests/test_v2_m5_payoff_integration.py", "tests/test_v2_m5_t7_evidence.py",
                "tests/v2_ep1_t7_support.py"):
        code[rel] = sha256((root / rel).read_bytes()).hexdigest()
    manifest = {
        "schema_version": "ak_ep1_t7_acceptance_input_v1",
        "profile": "P1-MINUTE-BREAKOUT",
        "synthetic": True,
        "labels": ["SYNTHETIC", "SCENARIO"],
        "recipe_sha256": entry(FIXTURE_DIR / "recipe.json")["sha256"],
        "seed": POSITIVE_RECIPE["seed"],
        "files": files,
        "code_files": code,
        "derived_identity": {"calendar_sha256": digest(sessions),
                             "bundle_manifest_sha256": bundle.manifest_sha256},
        "not_contained": ["final lock hash", "reviewer report hash",
                          "post-run readiness", "positive-run outputs"],
    }
    (FIXTURE_DIR / MANIFEST_NAME).write_bytes(
        json.dumps(manifest, indent=1, sort_keys=True).encode("utf-8") + b"\n")
    return manifest


if __name__ == "__main__":
    import sys
    if sys.argv[1:2] == ["freeze"]:
        manifest = freeze_acceptance_manifest()
        print("frozen", MANIFEST_NAME, "files:", len(manifest["files"]),
              "code_files:", len(manifest["code_files"]))
    else:
        print("usage: python -B -X utf8 tests/v2_ep1_t7_support.py freeze")
