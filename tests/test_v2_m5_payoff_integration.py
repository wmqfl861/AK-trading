"""M5 payoff training integration tests (arbitration 2026-09-17 T7/T8-deep;
E-P1 supplement 2026-09-17 EP1-M5; EP1-T7-FORM-r1 fixture rework 2026-09-17).

The two strict T7 nodes now run against the canonical positive cold chain of
the frozen EP1-T7 fixture P1 (tests/v2_ep1_t7_support.py, pre-check
data/maintenance_artifacts/v2-ep1-t7-fixture-20260917-r1/precheck-scale-table.md):
a 433-day SYNTHETIC ramp world (426 outer-train days + 2 head + 1 cal +
2 test + 2 maturity-tail days) with 60-minute sessions and real daily bars,
whose joint inner solution (N=426, f=0.86, V=51, E=1) was verified with the
real ``assign_splits``/``embargo_end`` to carry >=30 calibration evidence
blocks and a legal model inner dev split. The chain is executed once per
fresh pytest process by the session-scoped ``positive_cold_run`` fixture:
real ``_execute_validation(smoke)`` -> real inner ``_fit_fold`` (observed,
never replaced) -> pre-frozen plan windows -> ``replay_payoff_training_window``
-> ``observation_from_replay``.

The old 30-minute NO_CANDIDATE world is retained verbatim as the negative
world (T01 all-WAIT branch, empty-models, dependency gate, T8). Its known
structural shortfalls (no legal open decision, no daily background, model dev
28 < 34) are exactly why it stays negative - the arbitration's arithmetic
counterexamples live on in it.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path
import pytest

from v2_ep1_t7_support import positive_cold_run  # noqa: F401  (session fixture)

NS = 10 ** 9
MINUTE = 60 * NS
DAY_NS = 86400 * NS
BASE = 1700000000 * NS
WORLD_DAYS = 161
CHANNEL_LAST_DAY = 145          # days 0..145 form one rising channel
PRODUCT = "SHFE.RB"
CONTRACT = "SYNTHETIC_UNIT:rb"
FEATURE_KEY = "BASE:opening_gap_points:v2.1.0:d60"


def _assert_reconciled_and_released(path):
    reconciliation = path.reconciliation
    assert reconciliation["cash_difference_cny"] == D(0)
    assert reconciliation["equity_difference_cny"] == D(0)
    assert reconciliation["position_match"] is True
    assert reconciliation["fees_match"] is True
    assert reconciliation["status"] == "PASS"
    assert path.checkpoints
    checkpoint = path.checkpoints[-1]
    assert not checkpoint.account.lots
    assert not checkpoint.pending_intents
    assert checkpoint.account.reserved_margin_cny == D(0)
    for state in checkpoint.states:
        assert state.budget is None
        assert state.episode_id is None
        assert not state.pending_intent_ids


def _assertion_only_released_path():
    """ASSERTION_ONLY_SYNTHETIC objects; no OPEN, producer or cold-chain credit."""
    from types import SimpleNamespace
    account = SimpleNamespace(lots=(), reserved_margin_cny=D(0))
    state = SimpleNamespace(budget=None, episode_id=None, pending_intent_ids=())
    checkpoint = SimpleNamespace(account=account, pending_intents=(), states=(state,))
    return SimpleNamespace(
        reconciliation={"cash_difference_cny": D(0), "equity_difference_cny": D(0),
                        "position_match": True, "fees_match": True, "status": "PASS"},
        checkpoints=(checkpoint,))


def test_path_release_assertion_accepts_canonical_fields():
    _assert_reconciled_and_released(_assertion_only_released_path())


@pytest.mark.parametrize("defect", [
    "cash_difference", "equity_difference", "missing_cash", "position_mismatch",
    "fees_mismatch", "truthy_position_flag", "failed_reconciliation", "missing_status",
    "no_checkpoint", "remaining_lots", "pending_order", "reserved_margin",
    "budget_residue", "episode_residue", "pending_state_residue",
])
def test_path_release_assertion_rejects_unreconciled_or_unreleased(defect):
    path = _assertion_only_released_path()
    # An invented true alias cannot hide wrong canonical facts or residue.
    path.reconciliation["closed_and_released"] = True
    checkpoint = path.checkpoints[-1]
    state = checkpoint.states[0]
    if defect == "cash_difference":
        path.reconciliation["cash_difference_cny"] = D("0.01")
    elif defect == "equity_difference":
        path.reconciliation["equity_difference_cny"] = D("0.01")
    elif defect == "missing_cash":
        del path.reconciliation["cash_difference_cny"]
    elif defect == "position_mismatch":
        path.reconciliation["position_match"] = False
    elif defect == "fees_mismatch":
        path.reconciliation["fees_match"] = False
    elif defect == "truthy_position_flag":
        path.reconciliation["position_match"] = 1
    elif defect == "failed_reconciliation":
        path.reconciliation["status"] = "FAIL"
    elif defect == "missing_status":
        del path.reconciliation["status"]
    elif defect == "no_checkpoint":
        path.checkpoints = ()
    elif defect == "remaining_lots":
        checkpoint.account.lots = (object(),)
    elif defect == "pending_order":
        checkpoint.pending_intents = (object(),)
    elif defect == "reserved_margin":
        checkpoint.account.reserved_margin_cny = D("0.01")
    elif defect == "budget_residue":
        state.budget = object()
    elif defect == "episode_residue":
        state.episode_id = "SYNTHETIC-residue"
    elif defect == "pending_state_residue":
        state.pending_intent_ids = ("SYNTHETIC-pending",)
    with pytest.raises((AssertionError, KeyError)):
        _assert_reconciled_and_released(path)


def _ep1_m4_ready() -> bool:
    """Import-probe the two EP1-M4 symbols this chain consumes."""
    import importlib
    for module_name, symbol in (
        ("ak_trading.v2.decision.payoff_paths", "replay_payoff_training_window"),
        ("ak_trading.v2.decision.payoffs", "payoff_observation_source"),
    ):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            return False
        if not hasattr(module, symbol):
            return False
    return True


requires_ep1_m4 = pytest.mark.skipif(
    not _ep1_m4_ready(),
    reason="pending-EP1-M4-merge: ak_trading.v2.decision.payoff_paths."
           "replay_payoff_training_window / payoffs.payoff_observation_source "
           "are not on this tree; the main session runs this judgement after "
           "EP1-M4 merges")


# ---------------------------------------------------------------------------
# legacy negative-world machinery (baseline f19ca4e7, retained verbatim)
# ---------------------------------------------------------------------------

def _world():
    """SYNTHETIC small world: 161 trading days, 30 minute bars per day.

    EP1-M5 v3 channel design (earlier variants and their honest failures are
    recorded in the implementation notes): days 0..145 form ONE persistent
    rising channel. Known structural gaps, kept deliberately (T7-form
    decision sections 2.2/2.3/2.4): with 30-minute sessions the TREND minute
    spec can never legally OPEN, there is no daily bar so the cross-timeframe
    background can never publish, and the 126-day outer TRAIN leaves the
    model's inner dev at 28 < 34 days. This world remains the T01 all-WAIT
    negative; the positive world lives in v2_ep1_t7_support."""
    from ak_trading.v2.contracts import (DataQuality, FeeLeg, FrozenInput, Instrument,
                                         MarketBar, SessionWindow, TradingRule)
    from ak_trading.v2.validation.experiments import digest
    quality = DataQuality("READY", ("SYNTHETIC_UNIT",), "reconstructed", "a" * 64)
    sessions = tuple(
        SessionWindow(f"day-{i}", PRODUCT, date(2024, 1, 1) + timedelta(days=i),
                      BASE + i * DAY_NS, BASE + i * DAY_NS + 30 * MINUTE,
                      "DAY", "SYNTHETIC_UNIT", "SCENARIO")
        for i in range(WORLD_DAYS))
    instrument = Instrument(CONTRACT, "SHFE.rb9901", PRODUCT, "synthetic", ("synthetic",),
                            BASE, None, None, None, quality)
    leg = FeeLeg(D(1), D(0), False)
    rule = TradingRule("unit-rule", CONTRACT, sessions[0].start_nano,
                       sessions[-1].end_nano + 1, sessions[0].start_nano, D(10), D('.1'),
                       1, 1, leg, leg, leg, D('.1'), D('.1'), D(1000), D(1), D(0), D(0),
                       D('.1'), "SCENARIO", "1" * 64)
    bars = []
    drift = D("0.12")
    for i, session in enumerate(sessions):
        level = D(100) + drift * D(30) * D(i)     # day-over-day continuation
        reference = level
        for j in range(30):
            start = session.start_nano + j * MINUTE
            if i <= CHANNEL_LAST_DAY:
                wiggle = D("0.01") * D((-1) ** j)
                close = level + drift * D(j) + wiggle
            else:
                gap = D(i % 5) / 10
                phase = i % 4
                drift_bar = (D(j) * D("0.07") if phase == 0 else -D(j) * D("0.07")
                             if phase == 1 else D("0.5") * D((-1) ** j) * D(j) / D(30)
                             if phase == 2 else D("0.12") * D(j if j < 15 else 30 - j))
                close = level + gap + drift_bar + D(i % 3) / 10
            bars.append(MarketBar(
                f"m-{i}-{j}", CONTRACT, "SHFE.rb9901", PRODUCT, 60, start, start,
                start + MINUTE, session.trading_day, session.session_id, start + MINUTE,
                start + MINUTE, close, close + D('.2'), close - D('.2'), close,
                reference, D(1000), D(1000), True, 1, 1, "SYNTHETIC_UNIT", quality))
    bundle = FrozenInput(Path("SYNTHETIC_UNIT"), digest("t7-miniworld"), (instrument,),
                         sessions, tuple(bars), (), (rule,))
    return bundle, sessions


def _fit_models(bundle, sessions):
    """Real small model fit on days 2..38 with a standard-registry factor."""
    from ak_trading.v2.contracts import MacroContext
    from ak_trading.v2.decision.execution import fee_amount
    from ak_trading.v2.factor_specs import factor_registry_v2, baseline_registry_v2
    from ak_trading.v2.factors import build_feature_snapshot
    from ak_trading.v2.probability.forecast import build_forecast_requests
    from ak_trading.v2.probability.labels import label_forecast
    from ak_trading.v2.probability.models import default_fit_config, fit_forecast_models
    from ak_trading.v2.probability.regimes import assess_regime, fit_regime_reference, freeze_range
    from ak_trading.v2.strategies.registry import build_strategy_spec
    horizon = build_strategy_spec("TREND", "MINUTE").max_holding
    cal_end_day = 143
    train_cut_day = 120
    records = []
    for s in sessions[2:cal_end_day + 1]:
        decision = s.end_nano - MINUTE
        prefix = tuple(b for b in bundle.bars
                       if b.duration_seconds == 60 and b.end_nano <= s.end_nano)
        snap = build_feature_snapshot(CONTRACT, prefix, decision,
                                      factor_registry_v2(60) + baseline_registry_v2(60),
                                      None, None, bundle.rules[0], sessions)
        for r in (x for x in build_forecast_requests(snap, (horizon,), freeze_range(snap),
                                                     sessions) if x.barrier_key == "ATR_1"):
            window = tuple(b for b in bundle.bars
                           if b.duration_seconds == 60
                           and r.window_start_nano <= b.start_nano < r.window_end_nano)
            lab = label_forecast(r, window, sessions, (), sessions[cal_end_day].end_nano)
            if lab.status == "MATURE":
                records.append((snap, r, lab))
    features = [x[0] for x in records]
    requests = [x[1] for x in records]
    labels = [x[2] for x in records]
    train = [i for i, l in enumerate(labels)
             if l.available_at_nano is not None and l.available_at_nano <= sessions[train_cut_day].end_nano]
    cal = [i for i, l in enumerate(labels)
           if l.available_at_nano is not None
           and sessions[train_cut_day + 1].end_nano < l.available_at_nano <= sessions[cal_end_day].end_nano
           and features[i].decision_nano > sessions[train_cut_day].end_nano]
    train_end = sessions[train_cut_day].end_nano
    cal_end = sessions[cal_end_day].end_nano
    reference = fit_regime_reference(tuple(features[i] for i in train), train_end, train_end)

    def regime_for(f, ref_id=None):
        from dataclasses import replace
        rule = bundle.rules[0]
        price = f.reference_price
        macro = MacroContext("price-only", f.decision_nano, (), "macro-risk-v2.1.0", (),
                             "PRICE_ONLY", f.quality)
        cost = ((fee_amount(rule.open_fee, price, 1, rule.multiplier)
                 + max(fee_amount(leg, price, 1, rule.multiplier)
                       for leg in (rule.close_fee, rule.close_today_fee))) / rule.multiplier
                + 2 * rule.price_tick * (rule.slippage_ticks + rule.impact_ticks))
        regime = assess_regime(f, macro, None, freeze_range(f), cost, reference, None)
        return replace(regime, reference_id=ref_id) if ref_id is not None else regime

    fit = default_fit_config((FEATURE_KEY,), train_end, cal_end, sessions[-1].end_nano + 1)
    fit.update({"inner_minimum_train_days": 10, "inner_minimum_validation_days": 5,
                "inner_embargo_days": 1})
    models = fit_forecast_models(
        tuple(features[i] for i in train), tuple(regime_for(features[i], "inner-prefix") for i in train),
        tuple(requests[i] for i in train), tuple(labels[i] for i in train),
        tuple(features[i] for i in cal), tuple(regime_for(features[i]) for i in cal),
        tuple(requests[i] for i in cal), tuple(labels[i] for i in cal),
        fit, reference, seasonality=None)
    return models, reference


def _config(bundle, sessions, models, run_id):
    from ak_trading.v2.probability.models import default_fit_config
    from ak_trading.v2.schemas import parse_json_bytes
    from ak_trading.v2.strategies.registry import build_strategy_spec, select_exit_binding
    from ak_trading.v2.validation.experiments import digest
    spec = build_strategy_spec("TREND", "MINUTE")
    binding = select_exit_binding(spec).binding_sha256
    scope = {"market_family": "CN_FUTURES_V2", "products": [PRODUCT],
             "contract_ids": [CONTRACT],
             "decision_start_nano": sessions[146].start_nano,
             "decision_end_nano": sessions[150].start_nano,
             "maturity_end_nano": sessions[160].end_nano + 1,
             "calendar_sha256": digest(sessions)}
    split = {"split_id": "r4-unit-fold",
             "train_start_nano": sessions[20].start_nano,
             "train_end_nano": sessions[145].end_nano,
             "calibration_start_nano": sessions[146].start_nano,
             "calibration_end_nano": sessions[149].end_nano,
             "test_start_nano": sessions[150].start_nano,
             "test_end_nano": sessions[160].end_nano + 1,
             "embargo_days": 1, "embargo_minutes": 60, "final_holdout": False}
    fit = dict(default_fit_config((FEATURE_KEY,), split["train_end_nano"],
                                  split["calibration_end_nano"], split["test_end_nano"] + 1))
    fit.update({"seed": 7, "inner_split_fraction": 0.64, "inner_minimum_train_days": 10,
                "inner_minimum_validation_days": 34, "inner_embargo_days": 1})
    config = parse_json_bytes((Path(__file__).parent / "fixtures/v2/m5_research.json").read_bytes())
    config.update({
        "run_id": run_id,
        "contract_lock_path": "data/research_runs/v2/contracts-2.1.3/contract-lock.p3-r1.json",
        "contract_ids": [CONTRACT], "strategy_ids": [spec.strategy_id],
        "holdout_scope": scope, "holdout_id": digest(scope),
        "split_specs": [split], "model_fit_configs": {"r4-unit-fold": fit},
        "required_group_keys": [], "evaluation_as_of_nano": sessions[-1].end_nano + 1,
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
    return config


def _bypass_lock(monkeypatch):
    """Bridge the merge window: the ep1-r1 lock does not exist yet, so no
    existing lock can verify (producer bytes changed) and their document
    versions predate the 2.1.7 bump. Neither is the subject of these tests;
    replace with the real contracts-2.1.5 ep1-r1 lock once EP1-REVIEW
    publishes it."""
    import ak_trading.v2
    import ak_trading.v2.artifacts as artifacts
    monkeypatch.setattr(artifacts, "verify_contract_lock", lambda lock, root: None,
                        raising=True)
    monkeypatch.setattr(ak_trading.v2, "DOCUMENT_VERSION", "devplan-v2.1.6", raising=True)


_INNER_FOLD_CACHE = {}


def _outer_frozen_inner_real(monkeypatch, models, reference):
    """E-P1 EVIDENCE correction: a single shared frozen fold for every period
    proves nothing about the inner OOS clock. The OUTER (expensive) forecast
    fold stays frozen; every ``-payoff-inner`` split fits for real through
    the unmodified production ``_fit_fold`` (deterministic, so one real fit
    is cached and reused across the tests of this module — the fit itself is
    never skipped or stubbed)."""
    from ak_trading.v2.validation import experiments as ex
    real = ex._fit_fold

    def fold(bundle_, config_, split_, specs_):
        if split_.split_id.endswith("-payoff-inner"):
            key = (bundle_.manifest_sha256, split_.split_id,
                   ex.digest((config_["model_fit_configs"], config_["macro_mode"],
                              config_["synthetic"], tuple(s.strategy_id for s in specs_))))
            if key not in _INNER_FOLD_CACHE:
                _INNER_FOLD_CACHE[key] = real(bundle_, config_, split_, specs_)
            return _INNER_FOLD_CACHE[key]
        return {"snapshots": (), "seasonal": None, "reference": reference, "requests": (),
                "labels": (), "assignments": (), "models": models, "forecasts": (),
                "exclusions": []}
    monkeypatch.setattr(ex, "_fit_fold", fold)


def _empty_fold(monkeypatch, reference):
    from ak_trading.v2.validation import experiments as ex

    def fold(bundle_, config_, split_, specs_):
        return {"snapshots": (), "seasonal": None, "reference": reference, "requests": (),
                "labels": (), "assignments": (), "models": None, "forecasts": (),
                "exclusions": []}
    monkeypatch.setattr(ex, "_fit_fold", fold)


def _declare_plan_set(bundle, config):
    """Freeze and register the plan set on a v2.1.3 config exactly the way
    the freeze process will: build the deterministic plan set from the
    frozen calendar/split/strategy term, hash it, declare it on the config."""
    from ak_trading.v2.strategies.registry import strategy_specs_v2
    from ak_trading.v2.validation.experiments import (
        _payoff_training_plans, payoff_training_plan_set_sha256,
    )
    from ak_trading.v2.validation.splits import SplitSpec
    specs = tuple(s for mode in ("MINUTE", "DAILY") for s in strategy_specs_v2(mode)
                  if s.strategy_id in config["strategy_ids"])
    plans = [entry[3] for s in config["split_specs"]
             for entry in _payoff_training_plans(bundle, config, SplitSpec(**s), specs)[0]]
    digest_ = payoff_training_plan_set_sha256(plans)
    config = {**config, "schema_version": "ak_probability_research_config_v2.1.3",
              "payoff_training_plan_sha256": digest_}
    return config, digest_


_WORLD_CACHE = {}


def _cached_world():
    if "world" not in _WORLD_CACHE:
        bundle, sessions = _world()
        models, reference = _fit_models(bundle, sessions)
        _WORLD_CACHE["world"] = (bundle, sessions, models, reference)
    return _WORLD_CACHE["world"]


def _publish_at_decision(models, request, regime):
    """Deterministic publication provider: publish at the decision boundary."""
    from ak_trading.v2.probability.forecast import predict_market
    return predict_market(models, request, regime, request.features.decision_nano)



# ---------------------------------------------------------------------------
# strict T7 nodes on the canonical positive cold chain
# ---------------------------------------------------------------------------

@requires_ep1_m4
def test_t7_smoke_runs_real_training_chain_and_requires_producer_evidence(positive_cold_run):
    """The chain really ran on the frozen positive world: registration once,
    per-window source payloads and a full manifest (arbitration 3.3 item 9),
    and the strict verdict — at least one legal observation with its saved
    source original (a producer-frozen exclusion alone does NOT pass this)."""
    import hashlib
    import json
    from ak_trading.v2.schemas import parse_json_bytes
    record = positive_cold_run
    output = record.output
    arts = sorted(p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file())
    training = [a for a in arts if "training_paths" in a]
    registry = record.runroot / "experiment-registry.jsonl"
    assert registry.is_file()
    assert sum(1 for _ in registry.open()) == 1
    window_dirs = {a.rsplit("/", 1)[0] for a in training}
    assert training and window_dirs
    # EP1-T01 directory assertion fix: check the actual artifact files inside
    # each window directory (the pre-E-P1 assertion was structurally false).
    assert all((output / d / "opportunity.json").is_file() for d in window_dirs)
    assert all((output / d / "plan.json").is_file() for d in window_dirs)
    assert all((output / d / "path.json").is_file() for d in window_dirs)
    assert all((output / d / "manifest.json").is_file() for d in window_dirs)
    assert (output / "manifest.json").is_file()
    observations = [a for a in training if a.endswith("observation.json")]
    assert observations, ("producer never produced evidence; positive world must yield "
                          "at least one legal observation")
    for name in observations:
        obs = parse_json_bytes((output / name).read_bytes())
        assert obs["exit_state_condition"] == "OPEN_INITIAL"
        items = json.loads(obs["condition_key"])
        assert len(items) == 18 and items[17] == "OPEN_INITIAL"
        source = output / (name.rsplit("/", 1)[0] + "/source.json")
        assert source.is_file()
        assert hashlib.sha256(source.read_bytes()).hexdigest() == obs["source_sha256"]


@requires_ep1_m4
def test_t7_positive_full_chain_produces_real_observation(positive_cold_run):
    """EP1-T02 positive short chain: smoke entry -> real inner fit/partition
    -> measurement interface -> sole producer -> non-empty fitted payoff with
    a naturally triggered, filled, closed and matured single-layer
    observation; registration and plan-set identity exactly once."""
    import hashlib
    import json
    from ak_trading.v2.schemas import decode_payoff_path_file, parse_json_bytes
    from ak_trading.v2.contracts import PayoffPathResult
    record = positive_cold_run
    output = record.output
    readiness = record.readiness
    assert readiness["engineering"] == "PASS", readiness.get("blockers")
    assert "PAYOFF_TRAINING_EPISODES_NOT_AVAILABLE" not in (readiness.get("blockers") or [])
    registry = record.runroot / "experiment-registry.jsonl"
    assert sum(1 for _ in registry.open()) == 1
    frozen = parse_json_bytes((record.runroot / "registered-experiments"
                               / f"{record.run_id}.json").read_bytes())
    assert frozen["schema_version"] == "ak_probability_experiment_v2.1.3"
    assert frozen["spec"]["payoff_training_plan_sha256"] == record.declared_plan
    arts = sorted(p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file())
    opportunities = [a for a in arts if a.endswith("opportunity.json")]
    states = [parse_json_bytes((output / a).read_bytes())["status"] for a in opportunities]
    assert "OBSERVATION" in states, states
    observation_dirs = [a.rsplit("/", 1)[0] for a in opportunities
                        if parse_json_bytes((output / a).read_bytes())["status"] == "OBSERVATION"]
    for window_dir in observation_dirs:
        obs = parse_json_bytes((output / window_dir / "observation.json").read_bytes())
        assert obs["exit_state_condition"] == "OPEN_INITIAL"
        items = json.loads(obs["condition_key"])
        assert len(items) == 18 and items[17] == "OPEN_INITIAL"
        source = output / window_dir / "source.json"
        assert source.is_file()
        assert hashlib.sha256(source.read_bytes()).hexdigest() == obs["source_sha256"]
        paths = decode_payoff_path_file((output / window_dir / "path.json").read_bytes(),
                                        PayoffPathResult)
        assert len(paths) == 1
        result = paths[0]
        assert result.status == "COMPLETE"
        assert any(f.offset == "OPEN" for f in result.fills)
        assert any(f.lots != 0 for f in result.fills if f.offset == "OPEN")
        _assert_reconciled_and_released(result)
        assert result.initial_account.account_id.startswith("payoff-path:")
        assert result.path_decisions and result.path_decisions[0].request is not None


@requires_ep1_m4
def test_t7_all_empty_models_raise_stage_incomplete_before_payoff_training(tmp_path, monkeypatch):
    from ak_trading.v2.validation import experiments as ex
    from ak_trading.v2.validation.experiments import StageIncompleteError
    bundle, sessions, models, reference = _cached_world()
    config = _config(bundle, sessions, models, run_id="m5-ep1-t7-empty-models")
    _bypass_lock(monkeypatch)
    monkeypatch.setattr(ex, "RUN_ROOT", tmp_path / "runroot")
    _empty_fold(monkeypatch, reference)
    seen = []
    monkeypatch.setattr(ex, "_fit_training_payoffs",
                        lambda *a, **k: seen.append(1) or (_ for _ in ()).throw(
                            AssertionError("payoff training must not run without models")))
    with pytest.raises(StageIncompleteError,
                       match="TRAIN_OR_CALIBRATION_INSUFFICIENT_NO_MODEL_ARTIFACT"):
        ex._execute_validation(bundle, config, tmp_path / "out", "smoke")
    assert not seen


@pytest.mark.skipif(_ep1_m4_ready(), reason="EP1-M4 merged on this tree: the "
                   "dependency gate no longer fires; see the requires_ep1_m4 tests")
def test_t7_missing_measurement_interface_fails_before_any_fit(tmp_path, monkeypatch):
    """Pre-merge companion (EP1-M5 dependency gate): without the EP1-M4
    interface symbols both entries fail as engineering stage errors before
    lock-content verification, registration and any expensive fit."""
    from ak_trading.v2.validation import experiments as ex
    bundle, sessions, models, reference = _cached_world()
    config = _config(bundle, sessions, models, run_id="m5-ep1-t7-missing-m4")
    _bypass_lock(monkeypatch)
    monkeypatch.setattr(ex, "RUN_ROOT", tmp_path / "runroot")
    monkeypatch.setattr(ex, "_fit_fold",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("expensive _fit_fold must not run")))
    with pytest.raises(ex.StageIncompleteError,
                       match="PAYOFF_TRAINING_DEPENDENCY_MISSING.*replay_payoff_training_window"):
        ex._execute_validation(bundle, config, tmp_path / "out", "smoke")
    assert not (tmp_path / "runroot" / "experiment-registry.jsonl").exists()


@requires_ep1_m4
def test_t8_shadow_replay_consumes_training_then_hits_ruling5_gap(tmp_path, monkeypatch):
    """T8 second half: small input really walks run_shadow_replay_experiment's
    training consumption, then exposes the RULING 5 shadow-independent
    wiring gaps. The expected failure is the never-assigned ``_reg`` at the
    ledger construction; even past it, ledger.checkpoint misses the required
    exit_registry_sha256 and run_shadow lacks exit_specs/exit_bindings (007
    target wiring). Those belong to the follow-up shadow workorder; this test
    pins them instead of papering over them."""
    from ak_trading.v2.validation import experiments as ex
    bundle, sessions, models, reference = _cached_world()
    config = _config(bundle, sessions, models, run_id="m5-ep1-t8-shadow-deep")
    _bypass_lock(monkeypatch)
    monkeypatch.setattr(ex, "RUN_ROOT", tmp_path / "runroot")
    _outer_frozen_inner_real(monkeypatch, models, reference)
    seen = []
    real = ex._fit_training_payoffs
    monkeypatch.setattr(ex, "_fit_training_payoffs",
                        lambda *a, **k: seen.append(1) or real(*a, **k))
    with pytest.raises(NameError, match="_reg"):
        ex.run_shadow_replay_experiment(bundle, config, tmp_path / "out", None)
    assert seen, "training consumption must run before the RULING 5 gap"


# ---------------------------------------------------------------------------
# EP1-T01 dual branches (T7-form decision section 4.1)
# ---------------------------------------------------------------------------

@requires_ep1_m4
def test_t01_all_wait_branch_negative_world_records_every_window(tmp_path, monkeypatch):
    """Branch 1 (all WAIT): the legacy 30-minute NO_CANDIDATE world, real
    strategy observation and measurement, per-window artifacts saved, honest
    NO_CANDIDATE everywhere - no observation, no fabricated producer receipt,
    no zero-return sample, and the payoff-episodes blocker keeps engineering
    FAIL."""
    from ak_trading.v2.schemas import parse_json_bytes
    from ak_trading.v2.validation import experiments as ex
    bundle, sessions, models, reference = _cached_world()
    config = _config(bundle, sessions, models, run_id="m5-ep1-t01-all-wait")
    _bypass_lock(monkeypatch)
    monkeypatch.setattr(ex, "RUN_ROOT", tmp_path / "runroot")
    _outer_frozen_inner_real(monkeypatch, models, reference)
    output = tmp_path / "out"
    readiness = ex._execute_validation(bundle, config, output, "smoke")
    arts = sorted(p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file())
    opportunities = [a for a in arts if a.endswith("opportunity.json")]
    assert opportunities, "every planned window must record its opportunity"
    for name in opportunities:
        payload = parse_json_bytes((output / name).read_bytes())
        assert payload["status"] == "NO_CANDIDATE", payload
        assert payload["status_reason"] == "NO_TRIGGERED_BUDGETED_PATH"
        assert payload["candidates_checked"] == 0, "no gate-eligible candidate may exist"
    window_dirs = {a.rsplit("/", 1)[0] for a in opportunities}
    assert all((output / d / "plan.json").is_file() for d in window_dirs)
    assert all((output / d / "path.json").is_file() for d in window_dirs)
    assert all((output / d / "manifest.json").is_file() for d in window_dirs)
    # no observation and no fabricated producer receipt anywhere
    assert not [a for a in arts if a.endswith("observation.json")]
    assert not [a for a in arts if a.endswith("source.json")]
    assert readiness["engineering"] == "FAIL"
    assert "PAYOFF_TRAINING_EPISODES_NOT_AVAILABLE" in (readiness.get("blockers") or [])
    # denominators preserved: per-window candidate checks kept, not merged
    from ak_trading.v2.schemas import decode_payoff_path_file
    from ak_trading.v2.contracts import PayoffPathResult
    for d in window_dirs:
        paths = decode_payoff_path_file((output / d / "path.json").read_bytes(),
                                        PayoffPathResult)
        assert len(paths) == 1 and paths[0].status == "NO_CANDIDATE"


@requires_ep1_m4
def test_t01_ordinary_gate_rejects_natural_open_with_empty_payoff(positive_cold_run, monkeypatch):
    """Branch 2: from the positive world's real prefix and real fitted outer
    stand-in models, the real M3B TREND naturally produces a structurally
    complete OPEN (forecast, budget, request all present); the ordinary
    ``run_decision_replay`` consuming an honest EMPTY payoff must keep the
    gate ineligible, select nothing and emit no ENTRY/OPEN fill."""
    from decimal import Decimal
    from ak_trading.v2.contracts import RunPolicy
    from ak_trading.v2.decision.payoffs import fit_path_payoff_model
    from ak_trading.v2.strategies.registry import build_strategy_spec, select_exit_binding
    from ak_trading.v2.validation import experiments as ex
    from ak_trading.v2.validation.splits import SplitSpec
    import v2_ep1_t7_support as sup
    record = positive_cold_run
    bundle, sessions = record.bundle, record.sessions
    stand_in, _prov = sup.outer_stand_in()
    spec = build_strategy_spec("BREAKOUT", "MINUTE")
    split = SplitSpec(**record.config["split_specs"][0])
    empty_payoff = fit_path_payoff_model((), split.train_end_nano,
                                         record.config["research_policy_version"])
    run_policy = RunPolicy(
        purpose="RESEARCH_EVALUATION", routing_mode="FIXED", seed=7,
        fixed_strategy_id=spec.strategy_id,
        fixed_exit_binding_sha256=select_exit_binding(spec).binding_sha256,
        research_policy_version=record.config["research_policy_version"],
        macro_policy_version=record.config["macro_policy_version"],
        macro_mode="PRICE_ONLY", cost_scenario="BASE")
    lock_hash = __import__("hashlib").sha256(
        (ex.PROJECT_ROOT / record.config["contract_lock_path"]).read_bytes()).hexdigest()
    result, _rules = ex._replay_fold(bundle, split, stand_in, empty_payoff, (spec,),
                                     run_policy, lock_hash, Decimal(record.config["initial_cash_cny"]))
    opens = [p for d in result.decisions for p in d.proposals if p.action == "OPEN"]
    assert opens, "the positive world must naturally produce a real OPEN proposal"
    opened = opens[0]
    assert opened.forecast_id and opened.budget is not None and opened.target_lots > 0
    gates = [g for d in result.decisions for p, g in zip(d.proposals, d.gates)
             if p.proposal_id == opened.proposal_id]
    assert gates and not gates[0].eligible
    assert any("INSUFFICIENT" in reason or "PAYOFF" in reason
               for reason in gates[0].reasons), gates[0].reasons
    assert not any(f.offset == "OPEN" for f in result.fills)
    assert all(d.selected_proposal_id != opened.proposal_id for d in result.decisions)
    assert result.status == "COMPLETE", result.status
    # keep the real unselected OPEN for branch 3
    test_t01_ordinary_gate_rejects_natural_open_with_empty_payoff.last = (result, opened)


@requires_ep1_m4
def test_t01_legacy_producer_rejects_unselected_ordinary_open(positive_cold_run, monkeypatch):
    """Branch 3: the real unselected OPEN from the ordinary replay, with its
    original context/request and a real matured label, handed to the legacy
    producer branch against that ordinary ReplayResult must be refused with
    PAYOFF_INTEGRITY:DECISION_CHAIN_MISSING - an ordinary ReplayResult is
    never an executed measurement episode."""
    from ak_trading.v2.contracts import AccountSnapshot, DataQuality
    from ak_trading.v2.decision.payoffs import observation_from_replay
    from ak_trading.v2.decision.pipeline import build_strategy_context
    from ak_trading.v2.decision.payoffs import fit_path_payoff_model
    from ak_trading.v2.probability.labels import label_forecast
    from ak_trading.v2.strategies.registry import build_strategy_spec, select_exit_binding
    from ak_trading.v2.validation import experiments as ex
    from ak_trading.v2.validation.experiments import label_window_bars
    import v2_ep1_t7_support as sup
    record = positive_cold_run
    bundle, sessions = record.bundle, record.sessions
    result, opened = test_t01_ordinary_gate_rejects_natural_open_with_empty_payoff.last
    spec = build_strategy_spec("BREAKOUT", "MINUTE")
    stand_in, _prov = sup.outer_stand_in()
    empty_payoff = fit_path_payoff_model((), opened.decision_nano,
                                         record.config["research_policy_version"])
    from ak_trading.v2.contracts import RunPolicy
    run_policy = RunPolicy(
        purpose="RESEARCH_EVALUATION", routing_mode="FIXED", seed=7,
        fixed_strategy_id=spec.strategy_id,
        fixed_exit_binding_sha256=select_exit_binding(spec).binding_sha256,
        research_policy_version=record.config["research_policy_version"],
        macro_policy_version=record.config["macro_policy_version"],
        macro_mode="PRICE_ONLY", cost_scenario="BASE")
    initial = None  # the context account is only an identity carrier here
    import decimal
    cash = decimal.Decimal(record.config["initial_cash_cny"])
    account = AccountSnapshot("t01-ordinary", opened.decision_nano,
                              sessions[0].trading_day, cash, cash,
                              decimal.Decimal(0), decimal.Decimal(0), decimal.Decimal(0),
                              decimal.Decimal(0), cash, decimal.Decimal(0),
                              decimal.Decimal(0), cash, decimal.Decimal(0), cash, (),
                              False, DataQuality("READY", ("SYNTHETIC",), "reconstructed",
                                                 "t01"))
    context = build_strategy_context(
        bundle, opened.contract_id, opened.decision_nano, account, None,
        stand_in["models"], empty_payoff, spec, None, stand_in["reference"],
        run_policy, (), exit_binding=select_exit_binding(spec),
        forecast_provider=_publish_at_decision)
    forecast = next(f for f in context.forecasts if f.forecast_id == opened.forecast_id)
    from ak_trading.v2.contracts import ForecastRequest
    request = ForecastRequest(forecast.request_id, context.features, forecast.horizon,
                              forecast.window_start_nano, forecast.window_end_nano,
                              forecast.barrier_key, forecast.upper_distance,
                              forecast.lower_distance, forecast.frozen_range)
    label = label_forecast(request, label_window_bars(bundle, request), sessions, (),
                           record.config["evaluation_as_of_nano"])
    assert label.status == "MATURE", label.status
    split_end = ex.SplitSpec(**record.config["split_specs"][0]).train_end_nano
    with pytest.raises(ValueError, match="PAYOFF_INTEGRITY:DECISION_CHAIN_MISSING"):
        observation_from_replay(opened, context, request, label, result, split_end,
                                str(context.features.trading_day))
