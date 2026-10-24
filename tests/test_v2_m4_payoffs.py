from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D
import json

import pytest

from ak_trading.v2.decision.payoffs import (
    PayoffObservation, condition_key, estimate_proposal_payoff, fit_path_payoff_model,
)
from ak_trading.v2.decision.gates import evaluate_gate
from ak_trading.v2.policy import research_policy
from test_v2_m4_gates import example
from test_v2_m4_ledger import HASH, NS


def observations(p,c,profits=("80","-120"),blocks=80,per_block=8):
    key=condition_key(p,c)
    values=json.loads(key)
    from test_v2_m4_ledger import HASH as _ANY
    return tuple(PayoffObservation(f"obs-{b}-{n}",p.strategy_id,p.contract_id,c.features.product,key,
        c.exit_binding.binding_sha256,"OPEN_INITIAL",
        c.spec.max_holding,"ATR_1",values[12],b*10+1,b*10+2,b*10+3,
        (date(2025,1,1)+timedelta(days=b)).isoformat(),"UP_FIRST" if n%2==0 else "DOWN_FIRST",True,
        D(profits[n%2]),D(100),max(D(0),-D(profits[n%2])),None,D(values[15]),HASH)
        for b in range(blocks) for n in range(per_block))


def test_hand_mixed_ev_zero_win_06_and_cvar_120_no_trade():
    p,c=example()
    m=fit_path_payoff_model(observations(p,c),3*NS,c.run_policy.research_policy_version)
    e=estimate_proposal_payoff(m,p,c,"opportunity")
    assert (e.expected_net_cny,e.net_profit_probability,e.loss_cvar95_cny)==(D(0),.6,D(120))
    assert e.conservative_ev_cny==D(0) and e.status=="READY"
    assert "NON_POSITIVE_EV" in evaluate_gate(replace(p,payoff=e),c,research_policy(),checked_at_nano=c.now_nano).reasons


def test_missing_positive_path_is_not_zero_or_renormalized():
    p,c=example()
    f=replace(c.forecasts[0],path_probabilities={"UP_FIRST":.5,"DOWN_FIRST":.4,"NEITHER":.1,"AMBIGUOUS":0.})
    c=replace(c,forecasts=(f,))
    m=fit_path_payoff_model(observations(p,c),3*NS,c.run_policy.research_policy_version)
    e=estimate_proposal_payoff(m,p,c,"opportunity")
    assert e.status=="INSUFFICIENT" and e.missing_probability_mass==.1 and e.expected_net_cny is None


def test_block_count_not_minute_row_count_and_deterministic_order():
    p,c=example()
    rows=observations(p,c,blocks=10,per_block=100)
    a=fit_path_payoff_model(rows,3*NS,c.run_policy.research_policy_version)
    b=fit_path_payoff_model(tuple(reversed(rows)),3*NS,c.run_policy.research_policy_version)
    assert a.content_hash()==b.content_hash()
    e=estimate_proposal_payoff(a,p,c,"opportunity")
    assert e.sample_count==1000 and e.effective_blocks==10 and e.status=="INSUFFICIENT"


def test_tampered_source_map_or_display_id_cannot_pose_as_model():
    p,c=example()
    rows=observations(p,c)
    model=fit_path_payoff_model(rows,3*NS,c.run_policy.research_policy_version)
    from ak_trading.v2.decision.payoffs import PathPayoffModel
    with pytest.raises(ValueError,match="source_map"):
        PathPayoffModel(model.model_id,3*NS,rows,model.policy_version,
                        {r.observation_id:"c"*64 for r in rows})
    forged=PathPayoffModel("payoff:"+"d"*64,3*NS,rows,model.policy_version,model.source_hashes)
    e=estimate_proposal_payoff(forged,p,c,"opportunity")
    assert e.status=="INVALID" and "EVIDENCE_IDENTITY_MISMATCH" in e.reasons


def test_future_training_reject_and_different_budget_never_scales_money():
    p,c=example()
    rows=observations(p,c)
    with pytest.raises(ValueError,match="future_payoff"):
        fit_path_payoff_model(rows,10,c.run_policy.research_policy_version)
    m=fit_path_payoff_model(rows,3*NS,c.run_policy.research_policy_version)
    p=replace(p,budget=replace(p.budget,equity_at_start_cny=D(200000)))
    e=estimate_proposal_payoff(m,p,c,"opportunity")
    assert e.sample_count==0 and e.missing_probability_mass==1.


def test_initial_layer_structure_and_remaining_horizon_do_not_pool():
    p,c=example()
    m=fit_path_payoff_model(observations(p,c),3*NS,c.run_policy.research_policy_version)
    p=replace(p,action="ADD",next_state=replace(p.next_state,phase="HOLDING",layer_filled_lots=(1,),filled_layers=(0,),
              active_exit_nano=131*NS,pending_exit_nano=131*NS))
    assert estimate_proposal_payoff(m,p,c,"opportunity").sample_count==0


def test_untriggered_mature_path_zero_in_distribution():
    p,c=example()
    rows=observations(p,c,profits=("80","0"))
    rows=tuple(replace(r,traded=False,peak_margin_cny=D(0)) if r.net_pnl_cny==0 else r for r in rows)
    e=estimate_proposal_payoff(fit_path_payoff_model(rows,3*NS,c.run_policy.research_policy_version),p,c,"opportunity")
    assert e.expected_net_cny==D(48) and e.net_profit_probability==.6
    with pytest.raises(ValueError,match="untriggered"):
        replace(rows[0],traded=False)


def path_cash(prices):
    """Hand-fixture whole-path execution, no probability or routing consumes these prices."""
    from dataclasses import replace
    from ak_trading.v2.contracts import OrderIntent
    from ak_trading.v2.decision.execution import simulate_execution,simulate_protective_exit,position_for
    from test_v2_m4_ledger import ledger,rule,bar,budget
    book=ledger("100000")
    r=rule()
    b=replace(budget(),stop_price=D(95),target_price=D(100))
    # Initial layer at100; add fills are adverse limit orders registered earlier.
    first=OrderIntent("first","d","trend_m1_v2","episode","rb",0,NS,20*NS,1,"OPEN","ENTRY",0,D(100))
    out=simulate_execution(first,bar(t=NS,price="100"),r,7,None)
    for f in out.fills: book.apply_fill(f,r)
    total=1
    used=0
    for step,price in enumerate(prices[1:],2):
        p=D(price)
        observed=bar(t=step*NS,price=price)
        if p<=95 or p>=100:
            exit_i=OrderIntent("exit","d","trend_m1_v2","episode","rb",NS,2*NS,20*NS,-total,"CLOSE_TODAY","STOP",None,None)
            out=simulate_protective_exit(exit_i,observed,r,7,position_for(book.snapshot(step*NS),"rb"),b)
            for f in out.fills: book.apply_fill(f,r)
            break
        if used<2 and p<=b.layers[used+1].trigger_price:
            used+=1
            lots=b.layers[used].lots
            add=OrderIntent(f"add{used}","d","trend_m1_v2","episode","rb",NS,2*NS,20*NS,lots,"OPEN","ADD",used,p)
            out=simulate_execution(add,observed,r,7,position_for(book.snapshot(step*NS),"rb"))
            for f in out.fills: book.apply_fill(f,r)
            total+=sum(f.lots for f in out.fills)
    return book.snapshot(10*NS)


def test_identical_terminal_prices_have_different_full_martingale_cashflows():
    a=path_cash(("100","98","96","100"))
    b=path_cash(("100","96","94","100"))
    # A: 2*(100-98)*10 + 4*(100-96)*10=200, all legs fees14 ->186.
    assert a.cash_cny-D(100000)==D(186)
    # B hits gap stop94 before terminal100: -60-40-6 fees=-106; no recovery fantasy.
    assert b.cash_cny-D(100000)==D(-106)
    assert not a.lots and not b.lots


# --- L2 legacy thin-forwarding convergence (arbitration 20260917-r1) -------
# Landed from data/maintenance_artifacts/v2-legacy-convergence-l2-prep/
# l2-test-draft.py per its own landing note (merged here by the E1 work
# order). Three groups: L-A field equality through both entries, the frozen
# 2026-09-14 baseline forwarding invariance, and L-C cross exit-version
# buckets rebuilt on a REAL different exit identity (the old version-string
# trick no longer changes the forwarded binding).

from ak_trading.v2.artifacts import canonical_json_bytes, sha256_hex
from ak_trading.v2.contracts import ExitSpec, StrategyQualification
from ak_trading.v2.decision import pipeline
from ak_trading.v2.exits import registry as exits_registry
from ak_trading.v2.schemas import encode_record
from ak_trading.v2.strategies import registry as strategies_registry
from ak_trading.v2.strategies.registry import build_strategy_spec, select_exit_binding
from test_v2_m4_ledger import MODELS

L2_FAMILIES = ("TREND", "BREAKOUT", "PULLBACK", "REVERSION", "GRID", "MARTINGALE", "WAIT")
L2_MODES = ("MINUTE", "DAILY")

# Frozen 2026-09-14 baseline: probe-verified equal to the binding_sha256 of
# every exit_bindings entry in data/research_runs/v2/registered-experiments/
# m5-r3-smoke-chunk01.json (which the arbitration RULING 1.4 keeps as-is).
_L2_BASELINE_BINDING_SHA256 = {
    "breakout_d1_v2": "a4bc54aee3a8ebd5381b4f1b41fc25ad775e7bd531fa896e295f640a314b2d33",
    "breakout_m1_v2": "081f58e3c681cba86f08968c5b505fb2c7a56ed1dd05ebad1889e55a2e28363d",
    "grid_d1_v2": "490e49342ae669dc5e8da44d25d95c431c9cf81398016e475556672df3999fdc",
    "grid_m1_v2": "d5008bd8119aec8b7a9e33bc7ddd05bf71d11197e0a8ff7c09086e24544571f4",
    "martingale_d1_v2": "d8e85b9a864b15749b23a0d6920e5da66266edf7f84aa0404a945f37b090ee90",
    "martingale_m1_v2": "a1274732cc5a757705cd3931baf3def09b7c961f51bc3b8e39003f0692794a98",
    "pullback_d1_v2": "b313182b6fbc82ec300c5a33b7c9398faba140ccb942e575e28d97ed6a27c7ba",
    "pullback_m1_v2": "13cab9f394108326e2dd6606bc59fac7e84e02423885d1763e25f48217288bf0",
    "reversion_d1_v2": "8924cede5f54e9afab66ff7e9843e052e8e8ff8773881bbd998c1cf129911782",
    "reversion_m1_v2": "eb8a9fb55752ceb03310445e1bb1ffd13934e6cf0ac44984dcbb354ce09678d6",
    "trend_d1_v2": "d9809e272fbf3384e5d0aaf090be5a69e909fabe4b54beca86ff35e60f796eb5",
    "trend_m1_v2": "fcca840cdd0b8e8e11852d785c783ea140bf1e3653c5ff95b5c20ffcf699564e",
    "wait_d1_v2": "ebf4ce49080a31afe69074e1180540d87654ffe26e8dfc23cddc6a794f242847",
    "wait_m1_v2": "c89f32ee6bb3987ea967fe2d72846deca2b7dfd90ed8f49cd4caaca286aea463",
}

L2_PLACEHOLDER = "f" * 64


# --- L-A: field-by-field equality through both entries, both modes ---------

@pytest.mark.parametrize("family,mode", [(f, m) for f in L2_FAMILIES for m in L2_MODES])
def test_both_binding_entries_agree_field_by_field(family, mode):
    spec = build_strategy_spec(family, mode)
    forwarded = pipeline.legacy_exit_binding(spec)
    orthodox = select_exit_binding(spec, profile="legacy")
    assert forwarded == orthodox  # full record equality
    assert forwarded.strategy_id == spec.strategy_id
    # Entry identity is the code digest, never the full-record hash.
    assert forwarded.entry_spec_sha256 == spec.code_sha256
    assert forwarded.entry_spec_sha256 != sha256_hex(
        canonical_json_bytes(encode_record(spec)))
    # Single exit spec hash is the M3C single-element-array canonical form.
    assert list(forwarded.exit_spec_sha256s) == [
        exits_registry.exit_spec_sha256(exits_registry.legacy_exit_spec(mode))]
    assert forwarded.exit_spec_sha256s[0] != sha256_hex(canonical_json_bytes(
        encode_record(exits_registry.legacy_exit_spec(mode))))  # no object form
    # Priority carries the real registered policy id in business order.
    policy_id = strategies_registry.EXIT_PROFILE_IDS[mode]["legacy"]
    assert forwarded.priority == (policy_id,) and forwarded.priority != ("LEGACY",)
    assert forwarded.legacy_rules_version == strategies_registry.EXIT_LEGACY_RULES_VERSION
    assert forwarded.legacy_rules_version != exits_registry.LEGACY_RULES_VERSION
    assert forwarded.sizing_spec_sha256 is None
    assert L2_PLACEHOLDER not in (forwarded.entry_spec_sha256, *forwarded.exit_spec_sha256s,
                                  forwarded.binding_sha256)


@pytest.mark.parametrize("mode", L2_MODES)
def test_both_spec_entries_agree_and_no_placeholder(mode):
    forwarded = pipeline.legacy_exit_spec(mode)
    producer = exits_registry.legacy_exit_spec(mode)
    assert isinstance(forwarded, ExitSpec)
    assert forwarded == producer
    for field in ("exit_policy_id", "family", "mode", "version", "parameter_version",
                  "code_sha256", "parameters", "required_market_keys",
                  "required_state_keys", "applicable_entry_families",
                  "observation_duration_seconds", "execution_timing", "validation_stage"):
        assert getattr(forwarded, field) == getattr(producer, field), field
    assert forwarded.code_sha256 != L2_PLACEHOLDER
    assert forwarded.parameters == {}  # 08 §5.4 exactly empty
    assert forwarded.exit_policy_id == f"exit_legacy_{'m1' if mode == 'MINUTE' else 'd1'}_v1"
    assert exits_registry.exit_spec_sha256(forwarded) == sha256_hex(
        canonical_json_bytes([encode_record(forwarded)]))  # array material


# --- Thin-forwarding invariance: orthodox binding value unchanged ----------

@pytest.mark.parametrize("strategy_id", sorted(_L2_BASELINE_BINDING_SHA256))
def test_forwarded_binding_keeps_frozen_baseline_value(strategy_id):
    """The pure forwarding must not change the orthodox binding values
    (arbitration IMPLEMENTATION ORDERS closing note): same StrategySpec ->
    same binding as the running chunk01 registration."""
    family = strategy_id.rsplit("_", 2)[0].upper()
    mode = "MINUTE" if strategy_id.endswith("_m1_v2") else "DAILY"
    spec = build_strategy_spec(family, mode)
    assert pipeline.legacy_exit_binding(spec).binding_sha256 \
        == _L2_BASELINE_BINDING_SHA256[strategy_id] \
        == select_exit_binding(spec, profile="legacy").binding_sha256


def test_identity_neutral_entry_changes_keep_binding_but_registry_hash_moves():
    """RULING 1.1 spec correction: the binding is the entry CODE digest, not
    the full entry identity -- version/parameter-only changes leave it equal
    (no pooling protection from the binding alone) while the strategy
    registry hash moves, so registry/checkpoint scope rejects the swap."""
    spec = build_strategy_spec("TREND", "MINUTE")
    binding = pipeline.legacy_exit_binding(spec)
    for changed in (replace(spec, version="2.1.0-x"),
                    replace(spec, parameter_version=spec.parameter_version + ":x"),
                    replace(spec, parameters={**spec.parameters, "round_risk_ratio": "0.005"})):
        assert pipeline.legacy_exit_binding(changed).binding_sha256 == binding.binding_sha256
    bumped = replace(spec, version="2.1.0-x")
    assert pipeline.registry_hash_for((spec,)) != pipeline.registry_hash_for((bumped,))
    # strategy_id IS in the binding projection: a real identity change moves it.
    other = pipeline.legacy_exit_binding(replace(spec, strategy_id="wait_m1_v2"))
    assert other.binding_sha256 != binding.binding_sha256


# --- L-C: cross exit-version buckets, rebuilt on a real exit identity -------

def test_cross_exit_version_samples_never_share_buckets():
    """007 §7.2 replacement per arbitration L-C: the differing exit identity
    is a REAL other registered profile (trailing), not a StrategySpec.version
    string bump -- the thin forward keeps the binding on the code digest, so
    a version-only bump no longer creates a second binding."""
    p, c = example()
    rows = observations(p, c)
    key = condition_key(p, c)
    values = json.loads(key)
    assert len(values) == 18 and values[16] == c.exit_binding.binding_sha256
    assert values[17] == "OPEN_INITIAL"
    model = fit_path_payoff_model(rows, 3 * NS, c.run_policy.research_policy_version)
    e = estimate_proposal_payoff(model, p, c, "opportunity")
    assert e.exit_binding_sha256 == c.exit_binding.binding_sha256
    assert e.exit_state_condition == "OPEN_INITIAL"
    assert e.sample_count == len(rows) and e.status == "READY"
    # A genuinely different exit spec/profile identity -> zero support.
    assert c.spec.family == "TREND"  # eligible for the trailing profile
    other = select_exit_binding(c.spec, profile="trailing")
    assert other.exit_spec_sha256s != c.exit_binding.exit_spec_sha256s
    assert other.binding_sha256 != c.exit_binding.binding_sha256
    other_ctx = replace(c, exit_binding=other)
    e2 = estimate_proposal_payoff(model, p, other_ctx, "opportunity")
    assert e2.sample_count == 0 and e2.missing_probability_mass == 1.0
    assert e2.status == "INSUFFICIENT"
    # Entry-version-only change: binding stays equal (code digest unchanged),
    # so the payoff bucket WOULD pool -- rejection belongs to the scope layers.
    same_binding = pipeline.legacy_exit_binding(replace(c.spec, version="2.1.0-x"))
    assert same_binding.binding_sha256 == c.exit_binding.binding_sha256
    # Tampering the binding on an observation breaks its identity check.
    bad = rows[0]
    with pytest.raises(ValueError, match="identity_mismatch"):
        PayoffObservation(bad.observation_id, bad.strategy_id, bad.contract_id,
            bad.product, bad.condition_key, "9" * 64, bad.exit_state_condition,
            bad.horizon, bad.barrier_key, bad.geometry_key, bad.started_at_nano,
            bad.ended_at_nano, bad.available_at_nano, bad.block_id, bad.path_outcome,
            bad.traded, bad.net_pnl_cny, bad.peak_margin_cny, bad.maximum_loss_cny,
            bad.return_before_stop, bad.budget_cny, bad.source_sha256)


def test_entry_only_version_or_parameter_change_rejected_by_qualification_scope():
    """L-C second half: equal binding value must NOT authorize entry
    version/parameter mixing -- the qualification scope (and registry hash
    above) rejects it."""
    p, c = example()
    binding = pipeline.legacy_exit_binding(c.spec)
    q = StrategyQualification("qualification", c.spec.strategy_id, c.spec.version,
        c.spec.parameter_version, c.spec.code_sha256, "d" * 64,
        binding.binding_sha256, binding.exit_spec_sha256s, "SHFE.RB", ("rb",),
        "MINUTE", (c.spec.max_holding,), "PRICE_ONLY", "research-policy-v2.1.0",
        "macro-risk-v2.1.0",
        ("BASE", "FEE_X2", "SLIPPAGE_X2", "MARGIN_X1_5", "GAP", "NO_STOP"),
        dict(MODELS), HASH, "PASS", HASH, HASH, NS, 500 * NS, ())
    shadow = replace(c, run_policy=replace(c.run_policy, purpose="SHADOW_OPERATIONAL"),
                     qualification=q)
    ok = evaluate_gate(p, shadow, research_policy(), checked_at_nano=c.now_nano)
    assert "QUALIFICATION_MISMATCH" not in ok.reasons
    for bumped in (replace(c.spec, version="2.1.0-x"),
                   replace(c.spec, parameter_version=c.spec.parameter_version + ":x")):
        # The forwarded binding is oblivious to both bumps...
        assert pipeline.legacy_exit_binding(bumped).binding_sha256 \
            == binding.binding_sha256
        # ...so the qualification scope is the layer that refuses the mix.
        mixed = replace(shadow, spec=bumped)
        gate = evaluate_gate(p, mixed, research_policy(), checked_at_nano=c.now_nano)
        assert "QUALIFICATION_MISMATCH" in gate.reasons and not gate.eligible


def test_new_profiles_stay_scoped_to_eligible_families():
    """The legacy forward binds every family (WAIT identity-only); the newer
    profiles remain refused outside single-layer TREND/BREAKOUT/PULLBACK."""
    for family in ("REVERSION", "GRID", "MARTINGALE", "WAIT"):
        spec = build_strategy_spec(family, "MINUTE")
        with pytest.raises(ValueError, match="exit_profile_not_eligible_for_family_p1"):
            select_exit_binding(spec, profile="trailing")
        assert pipeline.legacy_exit_binding(spec).priority == ("exit_legacy_m1_v1",)


# --- T1-T5: real observation_from_replay producer (E1, decision 3.5) -------
#
# Every case calls the real seven-parameter producer directly. The replay is
# a real run_decision_replay over SYNTHETIC minute bars; the context/strategy
# pair is the established scheduler fixture (clearly labeled SYNTHETIC small
# input per arbitration 3.5) -- the ledger, fills, receipts, checkpoints and
# states are real immutable M1/M4 records, and no PayoffObservation is ever
# hand-built here.

from ak_trading.v2.contracts import DataQuality, ExecutionResult, ForecastRequest, LayerSpec
from ak_trading.v2.decision.payoffs import (
    PAYOFF_INTEGRITY_PREFIX, PAYOFF_INTEGRITY_PROGRAM_PREFIX,
    PAYOFF_SAMPLE_UNAVAILABLE_PREFIX, observation_from_replay,
)
from ak_trading.v2.decision.replay import run_decision_replay
from ak_trading.v2.probability.labels import label_forecast
from test_v2_m4_ledger import DAY, MODELS, account, bar, run_policy
from test_v2_m4_replay import replay_inputs

P1_ENTRY_NANO = 100 * NS
P1_TRAINING_END = 200 * NS


def _m5_washes(error) -> bool:
    """M5's contracted exclusion filter: only the frozen excludable prefix."""
    return str(error).startswith(PAYOFF_SAMPLE_UNAVAILABLE_PREFIX)


def _p1_bars(until=135, mark=100, stop_bar=None, stop_low="93", stop_high="106",
             stop_close="94", blocked=()):
    bars = []
    for t in range(70, until):
        if t == stop_bar:
            bars.append(replace(bar(t=t * NS, price="100"), high=D(stop_high),
                                low=D(stop_low), close=D(stop_close)))
        elif t in blocked:
            bars.append(bar(t=t * NS, price="200"))
        else:
            # `mark` re-prices only the pre-entry history (t <= 100): the bar
            # visible right after the entry fill, i.e. the mid-path mark.
            price = str(mark) if t <= 100 else "100"
            bars.append(bar(t=t * NS, price=price))
    return tuple(bars)


def _p1_fixture(monkeypatch, bars, *, side=1, latest_exit_nano=200 * NS,
                entry_valid_until_nano=None, exit_reasons=("STOP_TOUCHED",)):
    """SYNTHETIC scheduler fixture around the real replay engine (as in
    test_v2_m4_replay): returns bundle, models, reference and the captured
    decision-time context of the single OPEN proposal."""
    import ak_trading.v2.decision.replay as replay_module
    from ak_trading.v2.decision.execution import position_for
    from ak_trading.v2.strategies.registry import fresh_state
    bundle, models, _empty, reference = replay_inputs()
    bundle = replace(bundle, bars=tuple(bars))
    holder = {}

    def fixture_context(bundle, contract, now, account_, previous, fm, pm, spec,
                        seasonal, ref, rp, qs, *, exit_binding, forecast_provider):
        _p, ctx = _p1_side_example(side)
        ctx = replace(ctx, exit_binding=exit_binding)
        fs = replace(ctx.features, snapshot_id=f"features-{now}", decision_nano=now,
                     available_at_nano=now,
                     values=tuple(replace(v, available_at_nano=now) for v in ctx.features.values))
        reg = replace(ctx.regime, assessment_id=f"regime-{now}", snapshot_id=fs.snapshot_id,
                      decision_nano=now)
        f = replace(ctx.forecasts[0], forecast_id=f"forecast-{now}", snapshot_id=fs.snapshot_id,
                    assessment_id=reg.assessment_id, decision_nano=now, published_at_nano=now,
                    expires_at_nano=now + 2 * NS, window_start_nano=now + NS,
                    window_end_nano=now + 31 * NS)
        hashes = {**MODELS, "payoff": pm.content_hash()}
        return replace(ctx, now_nano=now, features=fs, forecasts=(f,), regime=reg,
                       account=account_, spec=spec, run_policy=rp,
                       position=position_for(account_, contract), model_hashes=hashes,
                       macro=replace(ctx.macro, decision_nano=now),
                       bars=tuple(b for b in bundle.bars if b.end_nano <= now))

    class FixtureStrategy:
        def __init__(self, spec):
            self.spec = spec

        def evaluate(self, ctx, previous):
            p, _ = _p1_side_example(side)
            now = ctx.now_nano
            if previous is not None and previous.episode_id:
                if previous.phase == "EXITING" or ctx.position is not None:
                    p = replace(p, proposal_id=f"exit-{now}", decision_nano=now, action="EXIT",
                                target_lots=0, earliest_execution_nano=now + NS,
                                valid_until_nano=now + 2 * NS,
                                next_state=replace(previous, phase="EXITING"),
                                budget=previous.budget,
                                forecast_id=ctx.forecasts[0].forecast_id,
                                assessment_id=ctx.regime.assessment_id,
                                reasons=tuple(exit_reasons))
                else:
                    p = replace(p, proposal_id=f"hold-{now}", decision_nano=now, action="HOLD",
                                target_lots=1, earliest_execution_nano=now + NS,
                                valid_until_nano=now + 2 * NS, next_state=previous,
                                budget=previous.budget, forecast_id=None, payoff=None)
            elif now == P1_ENTRY_NANO:
                b = _p1_side_budget(p, latest_exit_nano, side)
                p = _p1_side_proposal(p, b)
                p = replace(p, forecast_id=ctx.forecasts[0].forecast_id,
                            assessment_id=ctx.regime.assessment_id)
                if entry_valid_until_nano is not None:
                    p = replace(p, valid_until_nano=entry_valid_until_nano)
                holder["proposal"] = p
                holder["context"] = ctx
            else:
                p = replace(p, proposal_id=f"wait-{now}", decision_nano=now, action="WAIT",
                            target_lots=0, budget=None, episode_id=None,
                            next_state=previous or fresh_state(self.spec.strategy_id, "rb"),
                            earliest_execution_nano=now + NS, valid_until_nano=now + 2 * NS,
                            forecast_id=None, payoff=None)
            return p

    spec = build_strategy_spec("TREND", "MINUTE")
    monkeypatch.setattr(replay_module, "build_strategy_context", fixture_context)
    monkeypatch.setattr(replay_module, "strategy_registry_v2",
                        lambda mode: {spec.strategy_id: FixtureStrategy(spec)})
    return bundle, models, reference, holder, spec


def _p1_side_example(side=1):
    """example() flipped to a short regime/forecast/budget when side=-1, so
    the gate evidence (PERSIST:DOWN_TREND), condition key and proposal all
    agree on the direction."""
    from ak_trading.v2.schemas import calibration_geometry_key
    p, c = example()
    if side == -1:
        fs = c.features
        evidence = tuple(
            replace(e, event="PERSIST:DOWN_TREND", base_regime="DOWN_TREND",
                    geometry_key=calibration_geometry_key(fs, None, "DOWN_TREND"))
            if e.event == "PERSIST:UP_TREND"
            else replace(e, base_regime="DOWN_TREND")
            for e in c.forecasts[0].evidence)
        forecast = replace(c.forecasts[0], base_regime="DOWN_TREND",
                           persistence_probabilities={"DOWN_TREND": .85},
                           direction_probabilities={"DOWN": .6, "UP": .3, "NEUTRAL": .1},
                           path_probabilities={"DOWN_FIRST": .6, "UP_FIRST": .4,
                                               "NEITHER": 0., "AMBIGUOUS": 0.},
                           evidence=evidence)
        c = replace(c, regime=replace(c.regime, base="DOWN_TREND"), forecasts=(forecast,))
    return p, c


def _p1_side_proposal(p, b):
    return replace(p, budget=b, target_lots=b.side * b.layers[0].lots,
                   stop_price=b.stop_price, target_price=b.target_price,
                   next_state=replace(p.next_state, budget=b, direction=b.side))


def _p1_side_budget(p, latest_exit_nano, side=1):
    b = replace(p.budget, latest_exit_nano=latest_exit_nano)
    if side == -1:
        b = replace(b, side=-1, stop_price=D(106), target_price=D(90))
    return b


def _p1_model(side=1):
    base, ctx = _p1_side_example(side)
    if side == -1:
        base = _p1_side_proposal(base, replace(base.budget, side=-1, stop_price=D(106),
                                               target_price=D(90)))
    return fit_path_payoff_model(observations(base, ctx, profits=("80", "-30")),
                                 3 * NS, ctx.run_policy.research_policy_version)


def _p1_run(bundle, models, pm, reference, spec, start=P1_ENTRY_NANO, end=135 * NS):
    from test_v2_m4_ledger import HASH, exit_fixtures_for
    from ak_trading.v2.policy import research_policy
    _es, _bd, _ = exit_fixtures_for((spec,))
    return run_decision_replay(bundle, models, pm, (spec,), account("100000"), run_policy(),
                               (), {}, research_policy(), None, reference, HASH, None,
                               start, end, exit_specs=_es, exit_bindings=_bd)


def _p1_evidence(replay, ctx, training_end, bundle):
    """Assemble request + real ATR_1 label exactly as the M5 trainer does."""
    opened = next(p for p in replay.proposals
                  if p.action == "OPEN" and p.forecast_id is not None and p.budget is not None)
    forecast = next(f for f in replay.forecasts if f.forecast_id == opened.forecast_id)
    request = ForecastRequest(forecast.request_id, ctx.features, forecast.horizon,
                              forecast.window_start_nano, forecast.window_end_nano,
                              forecast.barrier_key, forecast.upper_distance,
                              forecast.lower_distance, forecast.frozen_range)
    future = tuple(b for b in bundle.bars if b.contract_id == "rb" and b.duration_seconds == 60
                   and request.window_start_nano <= b.start_nano < request.window_end_nano)
    label = label_forecast(request, future, bundle.sessions, (), training_end)
    return opened, request, label


def _p1_produce(replay, ctx, request, label, training_end=P1_TRAINING_END,
                block_id=None):
    return observation_from_replay(
        next(p for p in replay.proposals
             if p.action == "OPEN" and p.forecast_id is not None and p.budget is not None),
        ctx, request, label, replay, training_end,
        block_id if block_id is not None else str(ctx.features.trading_day))


# T1 -------------------------------------------------------------------------

def test_t1_long_stop_episode_exact_economics(monkeypatch):
    """Seven-param direct call on a real committed long episode: entry 100 at
    101*NS, protective stop 94 at 102*NS. Gross -60, both fees -2 -> net -62
    (the slippage memo is never deducted a second time), actual peak margin
    100, path-inclusive maximum loss 62, no condition shortening."""
    bundle, models, reference, holder, spec = _p1_fixture(
        monkeypatch, _p1_bars(stop_bar=102))
    replay = _p1_run(bundle, models, _p1_model(), reference, spec)
    assert replay.status == "COMPLETE" and len(replay.fills) == 2
    opened, request, label = _p1_evidence(replay, holder["context"], P1_TRAINING_END, bundle)
    assert label.status == "MATURE"
    obs = observation_from_replay(opened, holder["context"], request, label, replay,
                                  P1_TRAINING_END, str(holder["context"].features.trading_day))
    assert (obs.traded, obs.net_pnl_cny, obs.peak_margin_cny, obs.maximum_loss_cny) \
        == (True, D(-62), D(100), D(62))
    assert obs.return_before_stop is None
    assert obs.exit_state_condition == "OPEN_INITIAL" and obs.barrier_key == "ATR_1"
    assert obs.path_outcome == label.path_outcome and obs.path_outcome in PATHS_CHECK
    values = json.loads(obs.condition_key)
    assert len(values) == 18 and values[16] == holder["context"].exit_binding.binding_sha256
    assert values[17] == "OPEN_INITIAL"
    assert obs.started_at_nano == P1_ENTRY_NANO and obs.block_id == str(DAY)


def test_t1_short_episode_and_same_terminal_different_path(monkeypatch):
    """Short mirror (entry 100, stop 106) produces symmetric economics; two
    long paths with the SAME terminal exit price 94 but different mid-path
    marks share net -62 yet differ in actual peak margin (100 vs 94)."""
    bundle, models, reference, holder, spec = _p1_fixture(
        monkeypatch, _p1_bars(stop_bar=102, stop_low="99", stop_high="107", stop_close="106"),
        side=-1)
    replay = _p1_run(bundle, models, _p1_model(side=-1), reference, spec)
    opened, request, label = _p1_evidence(replay, holder["context"], P1_TRAINING_END, bundle)
    short = observation_from_replay(opened, holder["context"], request, label, replay,
                                    P1_TRAINING_END, str(holder["context"].features.trading_day))
    assert (short.traded, short.net_pnl_cny, short.peak_margin_cny, short.maximum_loss_cny) \
        == (True, D(-62), D(100), D(62))
    # Same-terminal different-path pair (long, exit 94 in both).
    results = []
    for mark in (100, 94):
        b, m, r, h, s = _p1_fixture(monkeypatch, _p1_bars(mark=mark, stop_bar=102))
        replay = _p1_run(b, m, _p1_model(), r, s)
        opened, request, label = _p1_evidence(replay, h["context"], P1_TRAINING_END, b)
        results.append(observation_from_replay(opened, h["context"], request, label, replay,
                                               P1_TRAINING_END, str(h["context"].features.trading_day)))
    a, c = results
    assert a.net_pnl_cny == c.net_pnl_cny == D(-62) and a.maximum_loss_cny == c.maximum_loss_cny == D(62)
    assert (a.peak_margin_cny, c.peak_margin_cny) == (D(100), D(94))
    assert a.observation_id != c.observation_id


def test_t1_target_path_profit_keeps_original_window(monkeypatch):
    """A target exit (104) is a profit +38 with the original label window and
    no early-window shortening; ended waits for the full ATR_1 label."""
    bundle, models, reference, holder, spec = _p1_fixture(
        monkeypatch, _p1_bars(stop_bar=102, stop_low="95", stop_high="106", stop_close="104"),
        exit_reasons=("TARGET_REACHED",))
    replay = _p1_run(bundle, models, _p1_model(), reference, spec)
    opened, request, label = _p1_evidence(replay, holder["context"], P1_TRAINING_END, bundle)
    obs = observation_from_replay(opened, holder["context"], request, label, replay,
                                  P1_TRAINING_END, str(holder["context"].features.trading_day))
    assert (obs.traded, obs.net_pnl_cny, obs.maximum_loss_cny) == (True, D(38), D(1))
    last_fill = max(f.execution_nano for f in replay.fills)
    assert obs.ended_at_nano == request.window_end_nano == label.window_end_nano
    assert obs.ended_at_nano > last_fill


# T2 -------------------------------------------------------------------------

def test_t2_maturity_boundary_and_night_block(monkeypatch):
    """Equal-to-cutoff availability is accepted; one nano earlier is future
    evidence; the night row keeps the explicit trading day as block_id."""
    bundle, models, reference, holder, spec = _p1_fixture(
        monkeypatch, _p1_bars(stop_bar=102))
    replay = _p1_run(bundle, models, _p1_model(), reference, spec)
    opened, request, label = _p1_evidence(replay, holder["context"], P1_TRAINING_END, bundle)
    boundary = observation_from_replay(opened, holder["context"], request, label, replay,
                                       label.available_at_nano, str(DAY))
    assert boundary.available_at_nano == boundary.ended_at_nano == 131 * NS
    with pytest.raises(ValueError, match="EVIDENCE_AFTER_TRAINING_END") as later:
        observation_from_replay(opened, holder["context"], request, label, replay,
                                label.available_at_nano - 1, str(DAY))
    assert _m5_washes(later.value)
    night = replace(holder["context"],
                    features=replace(holder["context"].features, trading_day=date(2026, 1, 13)))
    night_request = replace(request, features=night.features)
    night_label = replace(label, request_id=night_request.request_id)
    obs = observation_from_replay(opened, night, night_request, night_label, replay,
                                  P1_TRAINING_END, "2026-01-13")
    assert obs.block_id == "2026-01-13"
    with pytest.raises(ValueError, match="BLOCK_ID_NOT_TRADING_DAY") as bad_block:
        observation_from_replay(opened, night, night_request, night_label, replay,
                                P1_TRAINING_END, "2026-01-12")
    assert str(bad_block.value).startswith(PAYOFF_INTEGRITY_PROGRAM_PREFIX)


def test_t2_terminal_commit_after_cutoff_rejected(monkeypatch):
    """A late protective stop (134*NS) with a mature label whose availability
    is inside the cutoff still rejects: the terminal commit is future
    evidence."""
    bars = _p1_bars(until=140, stop_bar=134)
    bundle, models, reference, holder, spec = _p1_fixture(monkeypatch, bars)
    replay = _p1_run(bundle, models, _p1_model(), reference, spec, end=140 * NS)
    opened, request, label = _p1_evidence(replay, holder["context"], P1_TRAINING_END, bundle)
    assert label.status == "MATURE" and label.available_at_nano == 131 * NS
    with pytest.raises(ValueError, match="EVIDENCE_AFTER_TRAINING_END") as error:
        observation_from_replay(opened, holder["context"], request, label, replay,
                                132 * NS, str(DAY))
    assert _m5_washes(error.value)


def test_t2_flat_but_pending_or_short_window_rejected(monkeypatch):
    """Flat-but-pending (blocked entry never terminated) rejects as an
    unclosed episode; a 31-minute holding plan under the 60 trading-minute
    floor rejects without early-window relaxation."""
    bundle, models, reference, holder, spec = _p1_fixture(
        monkeypatch, _p1_bars(blocked=tuple(range(101, 110))), entry_valid_until_nano=131 * NS)
    replay = _p1_run(bundle, models, _p1_model(), reference, spec, end=110 * NS)
    assert replay.status == "INCOMPLETE"
    last = replay.checkpoints[-1]
    assert not last.account.lots and (last.pending_intents or last.account.reserved_margin_cny
                                      or any(s.budget is not None for s in last.states))
    opened, request, label = _p1_evidence(replay, holder["context"], P1_TRAINING_END, bundle)
    with pytest.raises(ValueError, match="EPISODE_NOT_CLOSED") as pending_error:
        _p1_produce(replay, holder["context"], request, label)
    assert _m5_washes(pending_error.value)
    b, m, r, h, s = _p1_fixture(monkeypatch, _p1_bars(stop_bar=102), latest_exit_nano=131 * NS)
    short = _p1_run(b, m, _p1_model(), r, s)
    opened, request, label = _p1_evidence(short, h["context"], P1_TRAINING_END, b)
    with pytest.raises(ValueError, match="MATURITY_WINDOW_BELOW_MINIMUM") as window_error:
        _p1_produce(short, h["context"], request, label)
    assert _m5_washes(window_error.value)


# T3 -------------------------------------------------------------------------

def _t3_base(monkeypatch):
    bundle, models, reference, holder, spec = _p1_fixture(
        monkeypatch, _p1_bars(stop_bar=102))
    replay = _p1_run(bundle, models, _p1_model(), reference, spec)
    opened, request, label = _p1_evidence(replay, holder["context"], P1_TRAINING_END, bundle)
    return holder["context"], request, label, replay, opened


def _rewrite_fill(replay, old, new):
    from ak_trading.v2.decision.ledger import record_hash
    digest = record_hash(new)
    return replace(
        replay,
        fills=tuple(new if f == old else f for f in replay.fills),
        execution_results=tuple(replace(r, fills=tuple(new if f == old else f for f in r.fills))
                                for r in replay.execution_results),
        checkpoints=tuple(replace(c, processed_fill_hashes={
            **dict(c.processed_fill_hashes), new.fill_id: digest})
            if old.fill_id in c.processed_fill_hashes else c for c in replay.checkpoints))


def test_t3_original_integrity_rejections(monkeypatch):
    """Missing rule, later-rule-without-original, tampered/duplicate/
    uncommitted fills, missing intent/receipt, cross-episode mixing, wrong
    fee/offset, STALE marks: none yields a sample. Capability gaps (missing
    rule original, mixed account, stale valuation) are excludable; tampering
    and chain breaks are blocking and cannot be washed by the M5 filter."""
    from test_v2_m4_ledger import fill as ledger_fill
    ctx, request, label, replay, opened = _t3_base(monkeypatch)
    close = next(f for f in replay.fills if f.offset != "OPEN")
    entry = next(f for f in replay.fills if f.offset == "OPEN")
    entry_intent = next(i for i in replay.intents if i.intent_id == entry.intent_id)
    blocking = []

    def must_block(probe, reason):
        with pytest.raises(ValueError, match=reason) as error:
            probe()
        message = str(error.value)
        assert not _m5_washes(error.value)
        assert message.startswith((PAYOFF_INTEGRITY_PREFIX, PAYOFF_INTEGRITY_PROGRAM_PREFIX))
        blocking.append(message)

    def must_exclude(probe, reason):
        with pytest.raises(ValueError, match=reason) as error:
            probe()
        assert _m5_washes(error.value)

    # Capability gaps: excludable (kept in the M5 per-window statistics).
    must_exclude(lambda: observation_from_replay(opened, replace(ctx, rule=None), request,
                                                 label, replay, P1_TRAINING_END, str(DAY)),
                 "RULE_ORIGINAL_MISSING")
    must_exclude(lambda: observation_from_replay(
        opened, ctx, request, label,
        _rewrite_fill(replay, close, replace(close, rule_id="rule-later")),
        P1_TRAINING_END, str(DAY)), "RULE_ORIGINAL_MISSING")
    must_exclude(lambda: observation_from_replay(
        opened, ctx, request, label,
        replace(replay, fills=replay.fills + (
            ledger_fill("other", 104 * NS, "100", episode="two"),)),
        P1_TRAINING_END, str(DAY)), "MIXED_EPISODE_ACCOUNT")
    stale_quality = DataQuality("STALE", ("PRICE_STALE",), "observed", HASH)
    must_exclude(lambda: observation_from_replay(
        opened, ctx, request, label,
        replace(replay, equity=tuple(
            replace(a, quality=stale_quality) if 101 * NS <= a.as_of_nano <= 102 * NS else a
            for a in replay.equity)),
        P1_TRAINING_END, str(DAY)), "VALUATION_INCOMPLETE")
    # Tampering and chain breaks: blocking integrity errors.
    must_block(lambda: observation_from_replay(
        opened, ctx, request, label,
        replace(replay, fills=replay.fills + (replace(entry, fee_cny=D(2)),)),
        P1_TRAINING_END, str(DAY)), "CONFLICTING_DUPLICATE_FILL")
    uncommitted = replace(ledger_fill("extra", 103 * NS, "100"), intent_id=entry.intent_id,
                          episode_id=entry.episode_id, strategy_id=entry.strategy_id,
                          contract_id=entry.contract_id)
    must_block(lambda: observation_from_replay(
        opened, ctx, request, label,
        replace(replay, fills=replay.fills + (uncommitted,),
                execution_results=replay.execution_results + (
                    ExecutionResult(entry.intent_id, (uncommitted,), 0, "FILLED", ()),)),
        P1_TRAINING_END, str(DAY)), "UNCOMMITTED_FILL")
    # Removing the entry intent breaks the chain at the OPEN leg first;
    # removing the close leg's intent breaks it at the fill-owner check.
    must_block(lambda: observation_from_replay(
        opened, ctx, request, label,
        replace(replay, intents=tuple(i for i in replay.intents if i.intent_id != entry.intent_id)),
        P1_TRAINING_END, str(DAY)), "INTENT_CHAIN_MISSING")
    must_block(lambda: observation_from_replay(
        opened, ctx, request, label,
        replace(replay, intents=tuple(i for i in replay.intents if i.intent_id != close.intent_id)),
        P1_TRAINING_END, str(DAY)), "FILL_INTENT_OWNER_MISMATCH")
    must_block(lambda: observation_from_replay(
        opened, ctx, request, label,
        replace(replay, execution_results=tuple(
            replace(r, fills=()) for r in replay.execution_results)),
        P1_TRAINING_END, str(DAY)), "RECEIPT_CHAIN_MISSING")
    must_block(lambda: observation_from_replay(
        opened, ctx, request, label,
        _rewrite_fill(replay, close, replace(close, fee_cny=D(2))),
        P1_TRAINING_END, str(DAY)), "FILL_ECONOMICS_MISMATCH")
    close_intent = next(i for i in replay.intents if i.intent_id == close.intent_id)
    wrong_offset = _rewrite_fill(replay, close, replace(close, offset="CLOSE"))
    wrong_offset = replace(wrong_offset, intents=tuple(
        replace(i, offset="CLOSE") if i.intent_id == close.intent_id else i
        for i in wrong_offset.intents))
    must_block(lambda: observation_from_replay(
        opened, ctx, request, label, wrong_offset, P1_TRAINING_END, str(DAY)),
        "FILL_OFFSET_MISMATCH")
    with_rule = next(c for c in replay.checkpoints if ctx.rule.rule_id in c.rule_hashes)
    must_block(lambda: observation_from_replay(
        opened, ctx, request, label,
        replace(replay, checkpoints=tuple(
            replace(c, rule_hashes={**dict(c.rule_hashes), ctx.rule.rule_id: "e" * 64})
            if c == with_rule else c for c in replay.checkpoints)),
        P1_TRAINING_END, str(DAY)), "FILL_RULE_HASH_MISMATCH")
    assert blocking


def test_t3_fill_hash_tampering_and_cash_conservation(monkeypatch):
    ctx, request, label, replay, opened = _t3_base(monkeypatch)
    entry = next(f for f in replay.fills if f.offset == "OPEN")
    # Bare tampering diverges from its own receipt (and the checkpoint still
    # carries the true digest): rejected as an integrity violation.
    with pytest.raises(ValueError, match="RECEIPT_CHAIN_MISSING") as tampered:
        observation_from_replay(opened, ctx, request, label,
                                replace(replay, fills=tuple(
                                    replace(f, execution_price=D("100.1")) if f == entry else f
                                    for f in replay.fills)),
                                P1_TRAINING_END, str(DAY))
    assert not _m5_washes(tampered.value)
    # Consistently rewritten economics break the isolated cash identity.
    moved = _rewrite_fill(replay, entry, replace(entry, execution_price=D("100.1"),
                                                 slippage_cny=D(1)))
    with pytest.raises(ValueError) as conservation:
        observation_from_replay(opened, ctx, request, label, moved, P1_TRAINING_END, str(DAY))
    assert str(conservation.value).startswith(PAYOFF_INTEGRITY_PREFIX)


# T4 -------------------------------------------------------------------------

def test_t4_genuine_zero_path_is_real_zero(monkeypatch):
    """A legal budgeted candidate whose entry is limit-blocked and then
    EXPIRED terminal, with the reservation released and a mature label, is a
    true zero observation -- never an INSUFFICIENT fabrication."""
    bundle, models, reference, holder, spec = _p1_fixture(
        monkeypatch, _p1_bars(blocked=(101, 102)))
    replay = _p1_run(bundle, models, _p1_model(), reference, spec)
    assert replay.status == "COMPLETE" and not replay.fills
    opened, request, label = _p1_evidence(replay, holder["context"], P1_TRAINING_END, bundle)
    assert label.status == "MATURE"
    obs = _p1_produce(replay, holder["context"], request, label)
    assert (obs.traded, obs.net_pnl_cny, obs.peak_margin_cny, obs.maximum_loss_cny,
            obs.return_before_stop) == (False, D(0), D(0), D(0), None)
    assert obs.path_outcome == label.path_outcome


def test_t4_unmatured_and_unselected_cannot_zero(monkeypatch):
    """An unmatured label and a deselected candidate cannot be completed as
    zero rows: one is sample-unavailable, the other an integrity violation."""
    bundle, models, reference, holder, spec = _p1_fixture(
        monkeypatch, _p1_bars(blocked=(101, 102)))
    replay = _p1_run(bundle, models, _p1_model(), reference, spec)
    opened, request, label = _p1_evidence(replay, holder["context"], 120 * NS, bundle)
    assert label.status == "UNMATURED"
    with pytest.raises(ValueError, match="LABEL_NOT_MATURE") as immature:
        _p1_produce(replay, holder["context"], request, label)
    assert _m5_washes(immature.value)
    fresh = _p1_evidence(replay, holder["context"], P1_TRAINING_END, bundle)
    opened, request, label = fresh
    deselected = replace(replay, decisions=tuple(
        replace(d, selected_proposal_id=None) for d in replay.decisions))
    with pytest.raises(ValueError, match="DECISION_CHAIN_MISSING") as unselected:
        _p1_produce(deselected, holder["context"], request, label)
    assert not _m5_washes(unselected.value)


def test_t4_add_and_multilayer_are_not_open_initial(monkeypatch):
    """ADD intents and multi-layer budgets stay outside the P1 first-OPEN
    support domain; they are excluded, never relabeled OPEN_INITIAL."""
    ctx, request, label, replay, opened = _t3_base(monkeypatch)
    entry = next(f for f in replay.fills if f.offset == "OPEN")
    add_intent = replace(next(i for i in replay.intents if i.intent_id == entry.intent_id),
                         intent_id="add-intent", purpose="ADD")
    with pytest.raises(ValueError, match="DOMAIN_NOT_P1_SINGLE_LAYER_OPEN") as add_error:
        _p1_produce(replace(replay, intents=replay.intents + (add_intent,)), ctx, request, label)
    assert _m5_washes(add_error.value)
    layered_budget = replace(opened.budget,
                             layers=(opened.budget.layers[0],
                                     LayerSpec(1, D(98), D(98), 1)),
                             maximum_lots=2)
    layered = replace(opened, budget=layered_budget,
                      next_state=replace(opened.next_state, budget=layered_budget,
                                         layer_filled_lots=(0, 0)))
    relaid = replace(replay, proposals=tuple(layered if p == opened else p for p in replay.proposals),
                     decisions=tuple(replace(d, proposals=tuple(
                         layered if p == opened else p for p in d.proposals))
                         for d in replay.decisions))
    with pytest.raises(ValueError, match="DOMAIN_NOT_P1_SINGLE_LAYER_OPEN") as layer_error:
        observation_from_replay(layered, ctx, request, label, relaid, P1_TRAINING_END, str(DAY))
    assert _m5_washes(layer_error.value)


# T5 -------------------------------------------------------------------------

def test_t5_deterministic_identity_and_hash_sensitivity(monkeypatch):
    """Identical evidence is byte-identical; binding changes reject; label,
    training visibility and checkpoint changes move the source hash."""
    from ak_trading.v2.artifacts import stable_id
    ctx, request, label, replay, opened = _t3_base(monkeypatch)
    first = _p1_produce(replay, ctx, request, label)
    second = _p1_produce(replay, ctx, request, label)
    assert first == second and first.source_sha256 == second.source_sha256
    assert first.observation_id == second.observation_id
    assert first.observation_id == stable_id("payoff_observation", first.source_sha256)
    relabeled = _p1_produce(replay, ctx, request, replace(label, path_outcome="UP_FIRST"))
    assert relabeled.source_sha256 != first.source_sha256
    assert relabeled.observation_id != first.observation_id
    assert relabeled.condition_key == first.condition_key
    later_visibility = _p1_produce(replay, ctx, request, label, training_end=300 * NS)
    assert later_visibility.source_sha256 != first.source_sha256
    last = replay.checkpoints[-1]
    rehashed = _p1_produce(replace(replay, checkpoints=tuple(
        replace(c, previous_hash="b" * 64) if c == last else c for c in replay.checkpoints)),
        ctx, request, label)
    assert rehashed.source_sha256 != first.source_sha256
    other_binding = select_exit_binding(ctx.spec, profile="trailing")
    with pytest.raises(ValueError, match="EXIT_BINDING_CHANGED") as binding_error:
        _p1_produce(replay, replace(ctx, exit_binding=other_binding), request, label)
    assert str(binding_error.value).startswith(PAYOFF_INTEGRITY_PREFIX)
    # The fill-leg evidence change either moves the hash or is rejected.
    close = next(f for f in replay.fills if f.offset != "OPEN")
    moved = _rewrite_fill(replay, close, replace(close, execution_price=D("94.1"),
                                                 slippage_cny=D(1)))
    with pytest.raises(ValueError) as fill_change:
        _p1_produce(moved, ctx, request, label)
    assert str(fill_change.value).startswith(PAYOFF_INTEGRITY_PREFIX)


PATHS_CHECK = ("UP_FIRST", "DOWN_FIRST", "NEITHER", "AMBIGUOUS")


# --- Existing F02: first nonempty range, with a real natural TREND producer ---
# All populations, models, sessions and cashflows below are SYNTHETIC engineering
# controls. They are neither historical PIT data nor an accepted model package.

_RANGE_NS = 1_000_000_000
_RANGE_MINUTE = 60 * _RANGE_NS
_RANGE_DAY = 86400 * _RANGE_NS
_RANGE_SYNTHETIC_HASH = sha256_hex(b"SYNTHETIC_M4_NONEMPTY_RANGE_CONTROL")


def _range_synthetic_models(base):
    """Small predeclared numerical families; real predictor/publisher consume them."""
    from ak_trading.v2.contracts import CalibrationEvidence, Horizon
    from ak_trading.v2.policy import confidence_policy
    from ak_trading.v2.probability.calibration import canonical_evidence_id
    from ak_trading.v2.probability.models import (
        ForecastModelSet, _fit_conditional, _fit_empirical_quantile,
        _fit_unconditional, default_fit_config, model_key,
    )
    from ak_trading.v2.probability.regimes import RegimeReference
    from ak_trading.v2.schemas import calibration_geometry_key

    train, calibration, expiry = base - 200 * _RANGE_DAY, base - 100 * _RANGE_DAY, base + 20 * _RANGE_DAY
    reference = RegimeReference("pending", train, train,
        {"SHFE.RB|DAY|60": (.001, .1, 1.), "SHFE.RB|DAILY|86400": (.001, .1, 1.)},
        {"SYNTHETIC": _RANGE_SYNTHETIC_HASH})
    reference = replace(reference, reference_id="regime_reference:" + reference.content_hash())
    feature_keys = ("META:price_tick:v2.1.0",)
    models = {}
    for mode, horizon in (("MINUTE", Horizon("TRADING_MINUTE", 30)), ("DAILY", Horizon("TRADING_DAY", 1))):
        levels = [("SHFE.RB|DAY|UP_TREND", "SHFE.RB|DAY", "SHFE.RB")] * 600

        def put(task, barrier, model):
            model = {**model, "fit_status": "SELECTED:SYNTHETIC_PREDECLARED_FAMILY"}
            models[model_key("SHFE.RB", mode, horizon, barrier, task, model["algorithm"])] = model

        put("DIRECTION", "COMMON", _fit_unconditional(
            ["UP"] * 540 + ["DOWN"] * 30 + ["NEUTRAL"] * 30,
            ("UP", "DOWN", "NEUTRAL"), list(feature_keys), [.1], [1.]))
        for barrier in ("ATR_0.5", "ATR_1", "ATR_2"):
            put("PATH", barrier, _fit_unconditional(
                ["UP_FIRST"] * 540 + ["DOWN_FIRST"] * 60,
                ("UP_FIRST", "DOWN_FIRST", "NEITHER", "AMBIGUOUS"), list(feature_keys), [.1], [1.]))
        put("QUANTILE", "COMMON", _fit_empirical_quantile(
            [-2. + .04 * i for i in range(100)] * 6, levels,
            [.05, .25, .5, .75, .95], list(feature_keys), [.1], [1.], 10))
        for task, positive_count in (("PERSIST:UP_TREND", 540), ("PERSIST:DOWN_TREND", 60),
                                     ("STOP_TOUCH:LONG", 30), ("STOP_TOUCH:SHORT", 30)):
            put(task, "COMMON", _fit_conditional(
                [(.1,)] * 600, ["POS"] * positive_count + ["NEG"] * (600 - positive_count),
                levels, ("NEG", "POS"), list(feature_keys), [.1], [1.]))
    evidence = []
    features = example()[1].features
    for regime in ("UP_TREND", "UNKNOWN"):
        for event, barrier, geometry, bucket in (
            ("PATH", "ATR_0.5", "PATH", "ALL"), ("PATH", "ATR_1", "PATH", "ALL"),
            ("PATH", "ATR_2", "PATH", "ALL"),
            ("PERSIST:UP_TREND", "COMMON", "UP_TREND", "[0.85,0.90)"),
        ):
            evidence.append(CalibrationEvidence("pending", "pending", confidence_policy("TREND").policy_id,
                "SHFE.RB", "DAY", 60, regime, barrier,
                calibration_geometry_key(features, None, geometry), event, Horizon("TRADING_MINUTE", 30),
                "path-label-v2.1.0", bucket, 600, 80, .01, .1, train, calibration, expiry, "VALID"))
    provisional = ForecastModelSet("pending", reference.reference_id, feature_keys, train, calibration, expiry,
        default_fit_config(feature_keys, train, calibration, expiry), models, tuple(evidence),
        {"SYNTHETIC_NUMERIC_POPULATION": _RANGE_SYNTHETIC_HASH})
    identity = "forecast_model_set:" + provisional.content_hash()
    bound = tuple(replace(e, model_id=identity) for e in evidence)
    bound = tuple(replace(e, evidence_id=canonical_evidence_id(e)) for e in bound)
    return replace(provisional, model_set_id=identity, evidence=bound), reference


def _range_synthetic_payoff_model(proposal, context, training_end):
    """Hand-declared SYNTHETIC cashflows, not output of the path producer."""
    key = condition_key(proposal, context)
    values = json.loads(key)
    paths = ("UP_FIRST", "DOWN_FIRST", "NEITHER", "AMBIGUOUS")
    rows = tuple(PayoffObservation(
        f"SYNTHETIC-nonempty-{block}-{n}", proposal.strategy_id, proposal.contract_id,
        context.features.product, key, context.exit_binding.binding_sha256, "OPEN_INITIAL",
        context.spec.max_holding, "ATR_1", values[12], block * 10 + 1, block * 10 + 2, block * 10 + 3,
        (date(2024, 1, 1) + timedelta(days=block)).isoformat(), paths[n % 4], True,
        D(100 if n % 4 == 0 else -10), D(100), D(0 if n % 4 == 0 else 10), None,
        D(values[15]), _RANGE_SYNTHETIC_HASH)
        for block in range(80) for n in range(8))
    return fit_path_payoff_model(rows, training_end, context.run_policy.research_policy_version)


@pytest.fixture(scope="module")
def natural_nonempty_range_case():
    """FrozenInput -> original inference/publication -> natural three-confirmation OPEN."""
    from datetime import datetime, timezone
    from pathlib import Path
    from ak_trading.v2.contracts import DataQuality, FrozenInput, Instrument, MarketBar, SessionWindow
    from ak_trading.v2.decision.pipeline import build_strategy_context, legacy_exit_binding, model_hashes_for
    from ak_trading.v2.probability.forecast import predict_market, publish_market_forecast
    from ak_trading.v2.strategies.registry import strategy_registry_v2
    from ak_trading.v2.strategies.state import commit_observation
    from test_v2_m4_ledger import account, rule, run_policy

    base = int(datetime(2026, 1, 12, tzinfo=timezone.utc).timestamp()) * _RANGE_NS
    contract = "SHFE.RB2605"
    quality = DataQuality("READY", ("SCENARIO",), "reconstructed", _RANGE_SYNTHETIC_HASH)
    instrument = Instrument(contract, "SHFE.rb2605", "SHFE.RB", "METAL", ("STEEL",),
        base - 300 * _RANGE_DAY, base + 90 * _RANGE_DAY, base + 80 * _RANGE_DAY,
        base + 70 * _RANGE_DAY, quality)
    sessions = tuple(SessionWindow(f"SYNTHETIC-day-{i}", "SHFE.RB", date(2026, 1, 12) + timedelta(days=i),
        base + i * _RANGE_DAY, base + (i + 1) * _RANGE_DAY, "DAY", "SYNTHETIC_24H", "SCENARIO")
        for i in range(-80, 5))

    def bar(start, price, duration, index, session):
        end = start + (_RANGE_MINUTE if duration == 60 else _RANGE_DAY)
        expected_count = 1 if duration == 60 else 1440
        return MarketBar(f"SYNTHETIC-warm-{duration}-{index}", contract, instrument.raw_symbol,
            instrument.product, duration, start, start, end, session.trading_day, session.session_id,
            end, None, price, price + D(".2"), price - D(".2"), price,
            D(100000), D(1000), D(1000), True, expected_count, expected_count, "SYNTHETIC", quality)

    minute = tuple(replace(bar(base + i * _RANGE_MINUTE, D(100) + D(".1") * i, 60, i,
        sessions[80] if i >= 0 else sessions[79]), volume=D(100000 + (i % 7) * 1000))
        for i in range(-1, 100))
    daily = tuple(bar(base + i * _RANGE_DAY, D(100) + D(i + 80), 86400, i, sessions[i + 80])
        for i in range(-80, 0))
    trading_rule = rule(contract, effective_from_nano=base - 300 * _RANGE_DAY,
        effective_until_nano=base + 90 * _RANGE_DAY, available_at_nano=base - 300 * _RANGE_DAY,
        limit_up=D(10000), limit_down=D(".1"), participation_rate=D(".1"))
    bundle = FrozenInput(Path("SYNTHETIC_M4_NONEMPTY_RANGE"), _RANGE_SYNTHETIC_HASH,
        (instrument,), sessions, daily + minute, (), (trading_rule,))
    models, reference = _range_synthetic_models(base)
    seed_proposal, seed_context = example()
    initial_payoff = fit_path_payoff_model(observations(seed_proposal, seed_context, blocks=1, per_block=4),
        base - 50 * _RANGE_DAY, seed_context.run_policy.research_policy_version)
    strategy = strategy_registry_v2("MINUTE")["trend_m1_v2"]
    state, trace = None, []
    for minute_index in (98, 99, 100):
        now = base + minute_index * _RANGE_MINUTE + _RANGE_NS
        publications = []

        def provider(model_set, request, regime):
            if request.features.duration_seconds == 86400:
                forecast = predict_market(model_set, request, regime, request.features.decision_nano)
                publications.append(("predict_market", request.request_id, forecast.forecast_id))
                return forecast
            forecast = publish_market_forecast(model_set, request, regime, clock_nano=lambda: now)
            publications.append(("publish_market_forecast", request.request_id, forecast.forecast_id))
            return forecast

        context = build_strategy_context(bundle, contract, now, account("100000"), state,
            models, initial_payoff, strategy.spec, None, reference, run_policy(), (),
            exit_binding=legacy_exit_binding(strategy.spec), forecast_provider=provider)
        proposal = strategy.evaluate(context, state)
        state = commit_observation(state, proposal)
        trace.append((proposal.action, state.confirmation_count, tuple(publications)))
        assert state.budget is None and not state.pending_intent_ids
    assert [action for action, _, _ in trace] == ["WAIT", "WAIT", "OPEN"], trace
    assert [count for _, count, _ in trace] == [1, 2, 3], trace
    assert all(any(kind == "predict_market" for kind, _, _ in calls) for _, _, calls in trace)
    assert all(sum(kind == "publish_market_forecast" for kind, _, _ in calls) == 3 for _, _, calls in trace)
    model = _range_synthetic_payoff_model(proposal, context, base - 50 * _RANGE_DAY)
    context = replace(context, model_hashes=model_hashes_for(models, model, reference, None))
    return proposal, context, model, tuple(trace)


def _range_selected_forecast(proposal, context):
    matches = [f for f in context.forecasts if f.forecast_id == proposal.forecast_id]
    assert len(matches) == 1
    return matches[0]


def _range_with_forecast(context, forecast):
    return replace(context, forecasts=tuple(
        forecast if f.forecast_id == forecast.forecast_id else f for f in context.forecasts))


def _range_assert_only_identity(model, proposal, context):
    estimate = estimate_proposal_payoff(model, proposal, context, "SYNTHETIC-nonempty-range")
    assert estimate.status == "INVALID"
    assert estimate.reasons == ("EVIDENCE_IDENTITY_MISMATCH",), estimate
    assert (estimate.sample_count, estimate.effective_blocks, estimate.missing_probability_mass) == (640, 80, 0.)
    assert estimate.expected_net_cny is None and estimate.conservative_ev_cny is None
    return estimate


def test_nonempty_range_p1_natural_trend_open_is_ready(natural_nonempty_range_case):
    proposal, context, model, trace = natural_nonempty_range_case
    forecast = _range_selected_forecast(proposal, context)
    assert proposal.action == "OPEN" and proposal.budget.frozen_range is None
    assert proposal.next_state.setup is not None and proposal.next_state.setup.frozen_range is None
    assert context.regime.frozen_range is not None and forecast.frozen_range == context.regime.frozen_range
    assert all(f.quality.status == "READY" for f in context.forecasts)
    assert model.identity_matches()
    key = condition_key(proposal, context)
    assert len(json.loads(key)) == 18 and all(row.condition_key == key for row in model.observations)
    assert {row.path_outcome for row in model.observations} == set(forecast.path_probabilities)
    estimate = estimate_proposal_payoff(model, proposal, context, "SYNTHETIC-natural-P1")
    assert estimate.status == "READY" and estimate.reasons == (), estimate
    assert (estimate.sample_count, estimate.effective_blocks, estimate.missing_probability_mass) == (640, 80, 0.)
    expected = sum((D(str(probability)) * (D(100) if path == "UP_FIRST" else D(-10))
                    for path, probability in forecast.path_probabilities.items()), D(0))
    assert abs(estimate.expected_net_cny - expected) < D("1e-22")
    assert abs(estimate.conservative_ev_cny - expected) < D("1e-22")
    assert estimate.loss_cvar95_cny == D(10) and estimate.peak_margin_cny == D(100)
    assert proposal.budget.frozen_range is None and proposal.next_state.setup.frozen_range is None


@pytest.mark.parametrize("setup_present", [False, True], ids=["absent", "null-range"])
def test_nonempty_range_p2_regime_fallback_keeps_directional_nulls(natural_nonempty_range_case, setup_present):
    proposal, context, model, _ = natural_nonempty_range_case
    if not setup_present:
        proposal = replace(proposal, next_state=replace(proposal.next_state, setup=None))
    estimate = estimate_proposal_payoff(model, proposal, context, "SYNTHETIC-P2")
    assert estimate.status == "READY" and estimate.reasons == (), estimate
    assert proposal.budget.frozen_range is None
    assert proposal.next_state.setup is None or proposal.next_state.setup.frozen_range is None


@pytest.mark.parametrize("priority", ["budget", "setup"])
def test_nonempty_range_p3_high_priority_match_ignores_lower_difference(natural_nonempty_range_case, priority):
    proposal, context, model, _ = natural_nonempty_range_case
    frozen = context.regime.frozen_range
    other = replace(frozen, range_id="SYNTHETIC-lower-priority-range")
    setup_range = other if priority == "budget" else frozen
    proposal = replace(proposal, next_state=replace(proposal.next_state,
        setup=replace(proposal.next_state.setup, frozen_range=setup_range)))
    if priority == "budget":
        proposal = replace(proposal, budget=replace(proposal.budget, frozen_range=frozen, range_id=frozen.range_id))
    context = replace(context, regime=replace(context.regime, frozen_range=other))
    estimate = estimate_proposal_payoff(model, proposal, context, "SYNTHETIC-P3")
    assert estimate.status == "READY" and estimate.reasons == (), estimate


@pytest.mark.parametrize("priority", ["budget", "setup"])
def test_nonempty_range_n1_lower_match_never_rescues_high_mismatch(natural_nonempty_range_case, priority):
    proposal, context, model, _ = natural_nonempty_range_case
    frozen = context.regime.frozen_range
    wrong = replace(frozen, range_id="SYNTHETIC-wrong-high-priority-range")
    proposal = replace(proposal, next_state=replace(proposal.next_state,
        setup=replace(proposal.next_state.setup, frozen_range=frozen if priority == "budget" else wrong)))
    if priority == "budget":
        proposal = replace(proposal, budget=replace(proposal.budget, frozen_range=wrong, range_id=wrong.range_id))
    _range_assert_only_identity(model, proposal, context)


@pytest.mark.parametrize("field", [
    "range_id", "frozen_at_nano", "lower", "center", "upper", "stop_lower", "stop_upper", "source_snapshot_id",
])
def test_nonempty_range_n2_all_eight_forecast_fields_reject_independently(natural_nonempty_range_case, field):
    proposal, context, model, _ = natural_nonempty_range_case
    forecast = _range_selected_forecast(proposal, context)
    frozen = forecast.frozen_range
    if field == "range_id":
        value = "SYNTHETIC-foreign-range-id"
    elif field == "source_snapshot_id":
        value = "SYNTHETIC-foreign-snapshot-id"
    elif field == "frozen_at_nano":
        value = frozen.frozen_at_nano - 1
    else:
        value = getattr(frozen, field) + D(".000001")
    changed = replace(frozen, **{field: value})
    from ak_trading.v2.schemas import calibration_geometry_key
    assert calibration_geometry_key(context.features, changed, "UP_TREND") == calibration_geometry_key(
        context.features, frozen, "UP_TREND")
    if field != "range_id":
        assert changed.range_id == frozen.range_id
    _range_assert_only_identity(model, proposal, _range_with_forecast(context, replace(forecast, frozen_range=changed)))


def test_nonempty_range_n3_all_null_and_missing_inputs_keep_original_semantics(natural_nonempty_range_case):
    proposal, context, model, _ = natural_nonempty_range_case
    all_null = replace(context, regime=replace(context.regime, frozen_range=None))
    _range_assert_only_identity(model, proposal, all_null)
    forecast = _range_selected_forecast(proposal, context)
    null_forecast = _range_with_forecast(all_null, replace(forecast, frozen_range=None,
        stop_touch_probabilities={}, return_before_break_probability=None, time_cdf=()))
    estimate = estimate_proposal_payoff(model, proposal, null_forecast, "SYNTHETIC-N3-null")
    assert estimate.status == "READY" and estimate.reasons == (), estimate
    for absent_proposal, absent_context in ((replace(proposal, budget=None), context),
        (proposal, replace(context, forecasts=tuple(f for f in context.forecasts if f.forecast_id != proposal.forecast_id)))):
        estimate = estimate_proposal_payoff(model, absent_proposal, absent_context, "SYNTHETIC-N3-missing")
        assert estimate.status == "INVALID" and "EVIDENCE_IDENTITY_MISMATCH" in estimate.reasons


@pytest.mark.parametrize("field,reason", [
    ("quality", "EVIDENCE_IDENTITY_MISMATCH"), ("snapshot_id", "EVIDENCE_IDENTITY_MISMATCH"),
    ("reference_price", "EVIDENCE_IDENTITY_MISMATCH"), ("barrier_key", "BARRIER_MISMATCH"),
    ("upper_distance", "BARRIER_MISMATCH"), ("lower_distance", "BARRIER_MISMATCH"),
])
def test_nonempty_range_n4_other_forecast_guards_still_reject(natural_nonempty_range_case, field, reason):
    proposal, context, model, _ = natural_nonempty_range_case
    forecast = _range_selected_forecast(proposal, context)
    if field == "quality":
        value = replace(forecast.quality, status="INVALID", reasons=("SYNTHETIC_N4",))
    elif field == "snapshot_id":
        value = "SYNTHETIC-foreign-current-snapshot"
    elif field == "barrier_key":
        value = "ATR_2"
    elif field == "reference_price":
        # A valid zero-quantile local control allows a one-point reference
        # change without introducing a separate quantile-unit violation.
        # Verify the control, then mutate only its reference price.
        forecast = replace(forecast,
            return_quantiles={key: 0. for key in forecast.return_quantiles},
            point_quantiles={key: D(0) for key in forecast.point_quantiles},
            atr_quantiles={key: 0. for key in forecast.atr_quantiles})
        context = _range_with_forecast(context, forecast)
        control = estimate_proposal_payoff(model, proposal, context, "SYNTHETIC-N4-reference-control")
        assert control.status == "READY" and control.reasons == (), control
        value = forecast.reference_price + D(1)
    else:
        value = getattr(forecast, field) + D(".000001")
    estimate = estimate_proposal_payoff(model, proposal,
        _range_with_forecast(context, replace(forecast, **{field: value})), "SYNTHETIC-N4")
    assert estimate.status == "INVALID" and reason in estimate.reasons, estimate
    assert estimate.expected_net_cny is None


@pytest.mark.parametrize("mutation,reason", [
    ("model_id", "EVIDENCE_IDENTITY_MISMATCH"), ("training_end", "FUTURE_PAYOFF_MODEL"),
    ("policy", "PAYOFF_POLICY_MISMATCH"),
])
def test_nonempty_range_n4_model_guards_still_reject(natural_nonempty_range_case, mutation, reason):
    proposal, context, model, _ = natural_nonempty_range_case
    if mutation == "model_id":
        changed = replace(model, model_id="payoff:" + "d" * 64)
    else:
        changed = fit_path_payoff_model(model.observations,
            context.now_nano if mutation == "training_end" else model.training_end_nano,
            "SYNTHETIC-foreign-policy" if mutation == "policy" else model.policy_version)
    estimate = estimate_proposal_payoff(changed, proposal, context, "SYNTHETIC-N4-model")
    assert estimate.status == "INVALID" and estimate.reasons == (reason,), estimate
    assert (estimate.sample_count, estimate.effective_blocks, estimate.missing_probability_mass) == (640, 80, 0.)
    assert estimate.expected_net_cny is None


def test_nonempty_range_n5_samples_blocks_path_support_ev_and_tail_guards(natural_nonempty_range_case):
    proposal, context, model, _ = natural_nonempty_range_case
    # Fresh control proves every mutated row set begins with valid identities.
    assert estimate_proposal_payoff(model, proposal, context, "SYNTHETIC-N5-control").status == "READY"
    all_rows = model.observations
    for rows, expected_reason in (
        (all_rows[:4], "INSUFFICIENT_SAMPLES"),
        (tuple(replace(row, block_id="2024-01-01") for row in all_rows), "INSUFFICIENT_BLOCKS"),
        (tuple(row for row in all_rows if row.path_outcome != "UP_FIRST"), "UNSUPPORTED_PATH_MASS"),
    ):
        changed = fit_path_payoff_model(rows, model.training_end_nano, model.policy_version)
        estimate = estimate_proposal_payoff(changed, proposal, context, "SYNTHETIC-N5-count")
        assert estimate.status == "INSUFFICIENT" and expected_reason in estimate.reasons
        assert "EVIDENCE_IDENTITY_MISMATCH" not in estimate.reasons
        assert not evaluate_gate(replace(proposal, payoff=estimate), replace(context,
            model_hashes={**context.model_hashes, "payoff": changed.content_hash()}), research_policy(),
            checked_at_nano=context.now_nano).eligible
    for profit, expected_reason in ((D(0), "NON_POSITIVE_EV"), (D(-1), "NON_POSITIVE_EV"),
                                     (-proposal.budget.planned_loss_cny - D(1), "TAIL_LOSS_LIMIT")):
        rows = tuple(replace(row, net_pnl_cny=profit, maximum_loss_cny=max(D(0), -profit)) for row in all_rows)
        changed = fit_path_payoff_model(rows, model.training_end_nano, model.policy_version)
        estimate = estimate_proposal_payoff(changed, proposal, context, "SYNTHETIC-N5-cash")
        assert estimate.status == "READY" and estimate.reasons == (), estimate
        probability_mass = sum((D(str(value)) for value in
            _range_selected_forecast(proposal, context).path_probabilities.values()), D(0))
        # Preserve the original probability mass, including Decimal(str(float))
        # conversion. The estimator does not renormalize these probabilities.
        assert abs(estimate.expected_net_cny - profit * probability_mass) < D("1e-22")
        gate = evaluate_gate(replace(proposal, payoff=estimate), replace(context,
            model_hashes={**context.model_hashes, "payoff": changed.content_hash()}), research_policy(),
            checked_at_nano=context.now_nano)
        assert not gate.eligible and expected_reason in gate.reasons, gate

