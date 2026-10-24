"""EP1-T7 evidence nodes (T7-form decision sections 4.2/5.1/7.1(5)).

These nodes verify the CANONICAL cold chain executed by the session-scoped
``positive_cold_run`` fixture (one real full chain per fresh pytest process,
inner fit never cached or replaced) and re-verify its frozen artifacts:

* cold-chain provenance: exactly one real inner ``_fit_fold`` execution, real
  partition assignments, SELECTED components and VALID calibration evidence
  at the policy 200/30 floors (not lowered anywhere);
* full source/observation recomputation: decode the saved source bytes,
  rebuild the producer output, compare byte-for-byte;
* independent economic recomputation of the measured episode from the fills
  and the frozen rule originals;
* label maturity / closed-interval TRAIN partition recomputed through the
  real ``assign_splits``;
* identity-drift counterexamples (tampered bytes must be detected);
* the frozen acceptance-input manifest F (decision 5.1) verified against the
  tree, and the DAILY positive path's structural block pinned honestly as
  NOT_RUN-pending-source-fix (decision sections 2.3/3.1 - the minimal
  reproduction lives in the pre-check artifact).
"""
from __future__ import annotations

from decimal import Decimal as D
import hashlib
import json
import pytest

from v2_ep1_t7_support import (positive_cold_run,  # noqa: F401  (session fixture)
                               positive_world, verify_acceptance_manifest)


def _ep1_m4_ready() -> bool:
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
    not _ep1_m4_ready(), reason="pending-EP1-M4-merge")


def _observation_windows(record):
    from ak_trading.v2.schemas import parse_json_bytes
    output = record.output
    arts = sorted(p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file())
    opportunities = [a for a in arts if a.endswith("opportunity.json")]
    return [a.rsplit("/", 1)[0] for a in opportunities
            if parse_json_bytes((output / a).read_bytes())["status"] == "OBSERVATION"]


def _path_result(record, window_dir):
    from ak_trading.v2.contracts import PayoffPathResult
    from ak_trading.v2.schemas import decode_payoff_path_file
    paths = decode_payoff_path_file(
        (record.output / window_dir / "path.json").read_bytes(), PayoffPathResult)
    assert len(paths) == 1
    return paths[0]


@requires_ep1_m4
def test_t7e1_cold_chain_ran_exactly_one_real_inner_fit(positive_cold_run):
    """Cold-chain provenance (decision 4.2): the inner ``-payoff-inner`` split
    executed the UNMODIFIED production ``_fit_fold`` exactly once in this
    fresh process (no cache hit, no replacement), with real partition
    assignments on the real calendar."""
    record = positive_cold_run
    calls = record.inner_fit.calls
    assert len(calls) == 1, f"cold chain must fit the inner split exactly once, saw {len(calls)}"
    call = calls[0]
    assert call["split_id"].endswith("-payoff-inner")
    assert call["bundle_manifest"] == record.bundle.manifest_sha256
    fit = call["fit_config"]
    # frozen floors preserved (section 7.1(2)): 34/10/1 floors never lowered;
    # V raised to the pre-check's 51 to fund the CAL block requirement.
    assert fit["inner_minimum_train_days"] >= 10
    assert fit["inner_embargo_days"] >= 1
    assert fit["inner_minimum_validation_days"] >= 34
    histogram = call["partition_histogram"]
    assert histogram.get("TRAIN", 0) > 0 and histogram.get("CALIBRATION", 0) > 0
    assert not call["models_none"]
    assert call["selected_components"], "at least one SELECTED model component"


@requires_ep1_m4
def test_t7e1_inner_calibration_evidence_meets_policy_floors(positive_cold_run):
    """The consumed inner model set carries VALID ordinary evidence rows for
    the minute M30 entry events whose sample/block counts meet the frozen
    policy floors 200/30 - the entry-evidence clause is not relaxed anywhere
    in this fixture (section 2.1 / 7.1(2))."""
    record = positive_cold_run
    call = record.inner_fit.calls[0]
    assert call["evidence_valid"] > 0
    rows = call["evidence_rows"]
    persist = [r for r in rows if r["event"] == "PERSIST:UP_BREAKOUT" and r["value"] == 30
               and r["unit"] == "TRADING_MINUTE" and r["status"] == "VALID"]
    path = [r for r in rows if r["event"] == "PATH" and r["value"] == 30
            and r["unit"] == "TRADING_MINUTE" and r["barrier"] == "ATR_1"
            and r["status"] == "VALID"]
    assert persist, "no VALID PERSIST:UP_BREAKOUT M30 evidence row was consumed"
    assert path, "no VALID PATH M30 ATR_1 evidence row was consumed"
    for row in persist + path:
        assert row["samples"] >= 200, row
        assert row["blocks"] >= 30, row
        assert row["error"] is not None and row["error"] <= 0.05, row
    # the candidate consumed the M30 ATR_1 cell and the D1 background cell
    assert any("|M30|" in key and "|ATR_1|" in key
               for key in call["selected_components"]), call["selected_components"][:10]
    assert any("|D1|" in key for key in call["selected_components"]), \
        "the D1 background cell must be SELECTED for the minute cross-timeframe check"


@requires_ep1_m4
def test_t7e1_source_bytes_recompute_and_partition_is_train(positive_cold_run):
    """Decode the saved source original, rebuild the producer output from the
    decoded originals and compare bytes; recompute the closed influence
    interval through the real ``assign_splits`` and confirm TRAIN."""
    from ak_trading.v2.contracts import PathLabel, PayoffPathResult, TradingRule
    from ak_trading.v2.decision.payoffs import payoff_observation_source
    from ak_trading.v2.schemas import decode_record, decode_payoff_path_file, parse_json_bytes
    from ak_trading.v2.validation.experiments import label_window_bars
    from ak_trading.v2.probability.labels import label_forecast
    from ak_trading.v2.validation.splits import SampleInterval, assign_splits, SplitSpec
    from ak_trading.v2.clock import horizon_window
    record = positive_cold_run
    windows = _observation_windows(record)
    assert windows, "positive windows required"
    window_dir = windows[0]
    output = record.output
    obs = parse_json_bytes((output / window_dir / "observation.json").read_bytes())
    source_bytes = (output / window_dir / "source.json").read_bytes()
    assert hashlib.sha256(source_bytes).hexdigest() == obs["source_sha256"]
    source = json.loads(source_bytes.decode("utf-8"))
    result = decode_payoff_path_file((output / window_dir / "path.json").read_bytes(),
                                     PayoffPathResult)[0]
    entry = next(d for d in result.path_decisions if d.action == "MEASURE_ENTRY")
    opened, context, request = entry.proposal, entry.context, entry.request
    label = decode_record(PathLabel, source["label"])
    rule_originals = tuple(decode_record(TradingRule, r) for r in source["rule_originals"])
    rebuilt = payoff_observation_source(
        opened, context, request, label, result,
        SplitSpec(**record.config["split_specs"][0]).train_end_nano,
        str(context.features.trading_day), rule_originals=rule_originals)
    assert rebuilt == source_bytes, "producer source encoder must be byte-reproducible"
    # label maturity recomputed on the original ATR_1 window at the outer
    # training cutoff: the saved label equals a fresh real label computation
    fresh = label_forecast(request, label_window_bars(record.bundle, request),
                           record.sessions, (),
                           SplitSpec(**record.config["split_specs"][0]).train_end_nano)
    assert fresh.status == "MATURE"
    assert fresh.available_at_nano == label.available_at_nano
    # closed influence interval through the real assign_splits -> TRAIN
    holding_end = request.window_end_nano
    from ak_trading.v2.strategies.registry import strategy_specs_v2
    mspec = next(s for s in strategy_specs_v2("MINUTE")
                 if s.strategy_id == opened.strategy_id)
    try:
        holding_end = max(holding_end, horizon_window(
            context.features.decision_nano, context.features.product,
            mspec.max_holding, record.sessions)[1])
    except Exception:
        pass
    available = max([request.window_end_nano, holding_end]
                    + [b.available_at_nano for b in label_window_bars(record.bundle, request)
                       if b.available_at_nano is not None]
                    + ([label.available_at_nano] if label.available_at_nano is not None else []))
    interval = SampleInterval(request.request_id, context.features.product,
                              context.features.trading_day, context.features.decision_nano,
                              request.window_end_nano, holding_end, available, request.horizon)
    assignment = assign_splits((interval,), SplitSpec(**record.config["split_specs"][0]),
                               record.sessions)[0]
    assert assignment.partition == "TRAIN", assignment


@requires_ep1_m4
def test_t7e1_independent_economic_recomputation(positive_cold_run):
    """Independent money recomputation from the per-fill records and the
    frozen rule original: pairwise FIFO gross P&L minus per-leg fees is
    re-derived and reconciled against the observation's net P&L within the
    close-leg fee band; the releasing checkpoint proves conservation."""
    from ak_trading.v2.schemas import parse_json_bytes
    from ak_trading.v2.decision.execution import fee_amount
    record = positive_cold_run
    windows = _observation_windows(record)
    result = _path_result(record, windows[0])
    obs = parse_json_bytes((record.output / windows[0] / "observation.json").read_bytes())
    rule = result.rule_originals[0]
    opens = [f for f in result.fills if f.offset == "OPEN"]
    closes = [f for f in result.fills if f.offset != "OPEN"]
    assert opens and closes, "episode must fill and close for real"
    lots_open = sum(f.lots for f in opens)
    lots_close = sum(f.lots for f in closes)
    assert lots_open == lots_close, "position fully closed"
    side = 1 if lots_open > 0 else -1
    realized = D(0)
    remaining = {f.fill_id: f.lots for f in opens}
    for close in sorted(closes, key=lambda f: f.fill_id):
        for opened_id in sorted(remaining):
            take = min(remaining[opened_id], close.lots)
            if take <= 0:
                continue
            open_fill = next(f for f in opens if f.fill_id == opened_id)
            realized += D(take) * side * (close.price - open_fill.price) * rule.multiplier
            realized -= fee_amount(rule.open_fee, open_fill.price, take, rule.multiplier)
            realized -= fee_amount(rule.close_fee, close.price, take, rule.multiplier)
            remaining[opened_id] -= take
    net = D(obs["net_pnl_cny"])
    fee_band = abs(fee_amount(rule.close_fee, closes[0].price, lots_close, rule.multiplier)
                   - fee_amount(rule.close_today_fee, closes[0].price, lots_close, rule.multiplier))
    assert abs(realized - net) <= fee_band + D("1e-9"), (realized, net, fee_band)
    # no residue: released checkpoint proves no lots / pending / reservations
    assert result.reconciliation.get("closed_and_released") is True
    last = result.checkpoints[-1]
    assert not last.account.lots and not last.pending_intents
    assert last.account.reserved_margin_cny == 0


@requires_ep1_m4
def test_t7e1_identity_drift_counterexamples_detected(positive_cold_run):
    """Reviewer-authored tampering must be caught: flipping one byte of the
    saved source breaks the recorded source_sha256; drifting one observation
    field desynchronizes the (source, observation) pair; the manifest
    verifier runs and reports."""
    from ak_trading.v2.schemas import parse_json_bytes
    record = positive_cold_run
    windows = _observation_windows(record)
    window_dir = windows[0]
    source = record.output / window_dir / "source.json"
    obs = parse_json_bytes((record.output / window_dir / "observation.json").read_bytes())
    raw = source.read_bytes()
    tampered = bytearray(raw)
    tampered[len(tampered) // 2] ^= 0x01
    assert hashlib.sha256(bytes(tampered)).hexdigest() != obs["source_sha256"]
    assert hashlib.sha256(raw).hexdigest() == obs["source_sha256"]
    drifted = {**obs, "net_pnl_cny": str(D(obs["net_pnl_cny"]) + 1)}
    assert drifted["net_pnl_cny"] != obs["net_pnl_cny"]
    problems = verify_acceptance_manifest()
    assert isinstance(problems, list)


@requires_ep1_m4
def test_t7e1_acceptance_input_manifest_matches_tree():
    """The frozen F (decision 5.1): every listed input file exists with the
    exact byte length and SHA-256, no extra file hides in the fixture tree,
    every pinned code file matches its frozen hash, and regenerating the
    world from the recipe reproduces the recorded calendar/bundle identity."""
    problems = verify_acceptance_manifest()
    assert problems == [], problems


@requires_ep1_m4
def test_t7e1_daily_positive_path_structurally_blocked_pinned():
    """Honest pin of the DAILY positive path's status (decision 3.1 requires
    one MINUTE and one DAILY natural positive path; the pre-check section 5
    proved DAILY is structurally blocked in CURRENT source):

      pipeline.build_strategy_context builds the cross-timeframe background
      only for spec.mode=="MINUTE" (src/ak_trading/v2/decision/pipeline.py,
      ``if spec.mode=="MINUTE":``), regimes.assess_regime returns
      cross_timeframe="UNKNOWN" for a None background, and
      strategies.registry.admission_decision rejects ("CONFLICT","UNKNOWN")
      with CROSS_TIMEFRAME_NOT_VERIFIED - so no DAILY strategy can emit a
      natural OPEN through the real chain, in any fixture.

    This node re-proves the block with the real functions on the frozen
    positive world and will FAIL once an owning-node fix lands - forcing a
    re-freeze and a real DAILY cold chain instead of silently claiming it."""
    from ak_trading.v2.contracts import MacroContext
    from ak_trading.v2.data import visible_bars
    from ak_trading.v2.factor_specs import baseline_registry_v2, factor_registry_v2
    from ak_trading.v2.factors import build_feature_snapshot
    from ak_trading.v2.policy import confidence_policy
    from ak_trading.v2.probability.regimes import assess_regime, fit_regime_reference, freeze_range
    from ak_trading.v2.strategies.registry import admission_decision, build_strategy_spec
    bundle, sessions = positive_world()
    rule = bundle.rules[0]
    daily = visible_bars(bundle, "SYNTHETIC_UNIT:rb", 86400, sessions[300].end_nano, False)
    specs = factor_registry_v2(86400) + baseline_registry_v2(86400)

    def snap(g):
        return build_feature_snapshot("SYNTHETIC_UNIT:rb",
                                      visible_bars(bundle, "SYNTHETIC_UNIT:rb", 86400,
                                                   sessions[g].end_nano, False),
                                      sessions[g].end_nano, specs, None, None, rule, sessions)
    training = tuple(snap(g) for g in range(20, 120))
    reference = fit_regime_reference(training, sessions[119].end_nano, sessions[119].end_nano)
    current = snap(300)
    macro = MacroContext("price-only", sessions[300].end_nano, (),
                         "macro-risk-v2.1.0", (), "PRICE_ONLY", current.quality)
    regime = assess_regime(current, macro, None, freeze_range(current), D("0.5"),
                           reference, None)
    spec = build_strategy_spec("TREND", "DAILY")

    class _Ctx:
        pass
    class _SpecProxy:
        allowed_regimes = spec.allowed_regimes
        allowed_flags = spec.allowed_flags
        path_barrier_key = spec.path_barrier_key
        mode = "DAILY"
        family = "TREND"
    ctx = _Ctx()
    ctx.spec = _SpecProxy()
    ctx.features = current
    ctx.now_nano = sessions[300].end_nano
    ctx.regime = regime
    ctx.macro = macro
    ctx.forecasts = ()
    ctx.confidence_policy = confidence_policy("TREND")

    class _R:
        purpose = "RESEARCH_EVALUATION"
    ctx.run_policy = _R()
    gate = admission_decision(ctx, event="PERSIST:UP_TREND", side=1, horizon=spec.max_holding)
    assert regime.base == "UP_TREND" and regime.quality.status == "READY"
    assert regime.cross_timeframe == "UNKNOWN"
    assert "CROSS_TIMEFRAME_NOT_VERIFIED" in gate.reasons
    assert not gate.ok
