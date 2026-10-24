"""SYNTHETIC controls for the real host/producer/strategy/execution/ledger.

No strategy, gate, publisher, risk computation, execution or ledger is patched.
The only controlled runtime dependencies are SDK data, clocks and crash cuts.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from functools import lru_cache
import json
from pathlib import Path
import subprocess
import sys

import pytest

from ak_trading.v2.artifacts import canonical_json_bytes, sha256_hex
from ak_trading.v2.contracts import (
    CalibrationEvidence, DataQuality, FeeLeg, FrozenInput, Horizon, Instrument,
    MarketBar, SessionWindow,
)
from ak_trading.v2.decision.payoffs import PayoffObservation, condition_key, fit_path_payoff_model
from ak_trading.v2.decision.pipeline import legacy_exit_binding
from ak_trading.v2.exits.registry import exit_registry_specs
from ak_trading.v2.policy import confidence_policy
from ak_trading.v2.probability.calibration import canonical_evidence_id
from ak_trading.v2.probability.models import (
    ForecastModelSet, _fit_unconditional, _fit_conditional, _fit_empirical_quantile,
    default_fit_config, model_key,
)
from ak_trading.v2.probability.regimes import RegimeReference
from ak_trading.v2.schemas import calibration_geometry_key, encode_record, parse_json_bytes
from ak_trading.v2.strategies.registry import build_strategy_spec, strategy_registry_v2
from ak_trading.v2.strategies.state import commit_observation, commit_selected_proposal
from ak_trading.v2.validation.forward_paper import (
    SCHEMA, NS, MINUTE, Activation, ClockSample, ForwardPaperError, ForwardPaperHost,
    Journal, SourceEvent, content, default_policies, received_batch, verified_file,
)

BASE = int(datetime(2026, 1, 12, tzinfo=timezone.utc).timestamp()) * NS
DAY_NS = 86400 * NS
HASH = sha256_hex(b"SYNTHETIC_FORWARD_PAPER_CONTROL")
CONTRACT = "SHFE.RB2605"


class FakeClock:
    def __init__(self, when=BASE + 100 * MINUTE + NS):
        self.when = when
        self.offset, self.uncertainty = 0, 0
        self.jump = 0

    def __call__(self):
        return ClockSample(self.when, self.when + self.jump, HASH, self.offset, self.uncertainty,
                           self.when, "SYNTHETIC_INDEPENDENT_CLOCK")

    def at(self, when):
        self.when = when
        return self()


def q():
    return DataQuality("READY", ("SCENARIO",), "reconstructed", HASH)


@lru_cache(maxsize=2)
def synthetic_models(side=1):
    """Original numerical fit primitives over explicitly synthetic populations.

    This fixture is neither a real fitted/accepted M5 pack nor a qualification.
    Predictions and all downstream evidence checks execute in production code.
    """
    train, calibration, expiry = BASE - 200 * DAY_NS, BASE - 100 * DAY_NS, BASE + 20 * DAY_NS
    reference = RegimeReference("pending", train, train,
        {"SHFE.RB|DAY|60": (.001, .1, 1.), "SHFE.RB|DAILY|86400": (.001, .1, 1.)}, {"synthetic": HASH})
    reference = replace(reference, reference_id="regime_reference:" + reference.content_hash())
    keys = ("META:price_tick:v2.1.0",)
    models = {}
    for mode, horizon in (("MINUTE", Horizon("TRADING_MINUTE", 30)), ("DAILY", Horizon("TRADING_DAY", 1))):
        levels = [("SHFE.RB|DAY|UP_TREND", "SHFE.RB|DAY", "SHFE.RB")] * 600
        def put(task, barrier, model):
            # Predeclared primitive family in this synthetic control. No fitted
            # statistics are changed to make an application assertion pass.
            model = {**model, "fit_status": "SELECTED:SYNTHETIC_PREDECLARED_FAMILY"}
            models[model_key("SHFE.RB", mode, horizon, barrier, task, model["algorithm"])] = model
        direction, other = ("UP", "DOWN") if side == 1 else ("DOWN", "UP")
        put("DIRECTION", "COMMON", _fit_unconditional([direction] * 540 + [other] * 30 + ["NEUTRAL"] * 30,
            ("UP", "DOWN", "NEUTRAL"), list(keys), [.1], [1.]))
        for barrier in ("ATR_0.5", "ATR_1", "ATR_2"):
            put("PATH", barrier, _fit_unconditional([direction + "_FIRST"] * 540 + [other + "_FIRST"] * 60,
                ("UP_FIRST", "DOWN_FIRST", "NEITHER", "AMBIGUOUS"), list(keys), [.1], [1.]))
        put("QUANTILE", "COMMON", _fit_empirical_quantile(
            [-2. + .04 * i for i in range(100)] * 6, levels,
            [.05, .25, .5, .75, .95], list(keys), [.1], [1.], 10))
        for task, count in (("PERSIST:UP_TREND", 540 if side == 1 else 60),
                            ("PERSIST:DOWN_TREND", 60 if side == 1 else 540),
                            ("STOP_TOUCH:LONG", 30), ("STOP_TOUCH:SHORT", 30)):
            put(task, "COMMON", _fit_conditional([(.1,)] * 600, ["POS"] * count + ["NEG"] * (600 - count),
                levels, ("NEG", "POS"), list(keys), [.1], [1.]))
    from test_v2_m4_gates import example
    geometry_snapshot = example()[1].features
    evidence = []
    trend = "UP_TREND" if side == 1 else "DOWN_TREND"
    for regime in (trend, "UNKNOWN"):
        for event, barrier, geometry, bucket in (
                ("PATH", "ATR_0.5", "PATH", "ALL"), ("PATH", "ATR_1", "PATH", "ALL"),
                ("PATH", "ATR_2", "PATH", "ALL"), ("PERSIST:" + trend, "COMMON", trend, "[0.85,0.90)")):
            evidence.append(CalibrationEvidence("pending", "pending", confidence_policy("TREND").policy_id,
                "SHFE.RB", "DAY", 60, regime, barrier, calibration_geometry_key(geometry_snapshot, None, geometry), event, Horizon("TRADING_MINUTE", 30),
                "path-label-v2.1.0", bucket, 600, 80, .01, .1, train, calibration, expiry, "VALID"))
    provisional = ForecastModelSet("pending", reference.reference_id, keys, train, calibration, expiry,
        default_fit_config(keys, train, calibration, expiry), models, tuple(evidence), {"synthetic_numeric_population": HASH})
    identity = "forecast_model_set:" + provisional.content_hash()
    bound = tuple(replace(e, model_id=identity) for e in evidence)
    bound = tuple(replace(e, evidence_id=canonical_evidence_id(e)) for e in bound)
    return replace(provisional, model_set_id=identity, evidence=bound), reference


@lru_cache(maxsize=2)
def synthetic_activation(side=1):
    from test_v2_m4_gates import example
    from test_v2_m4_payoffs import observations
    from test_v2_m4_ledger import rule
    specs = tuple(build_strategy_spec(family, "MINUTE") for family in ("TREND", "BREAKOUT", "PULLBACK"))
    inst = Instrument(CONTRACT, "SHFE.rb2605", "SHFE.RB", "METAL", ("STEEL",), BASE - 300 * DAY_NS,
                      BASE + 90 * DAY_NS, BASE + 80 * DAY_NS, BASE + 70 * DAY_NS, q())
    sessions = tuple(SessionWindow("day" + str(i), "SHFE.RB", date(2026, 1, 12) + timedelta(days=i),
                     BASE + i * DAY_NS, BASE + (i + 1) * DAY_NS, "DAY", "SYNTHETIC_24H", "SCENARIO")
                     for i in range(-80, 5))
    def bar(start, price, duration, index, session):
        end = start + (MINUTE if duration == 60 else DAY_NS)
        return MarketBar("warm-" + str(duration) + "-" + str(index), CONTRACT, inst.raw_symbol, inst.product,
            duration, start, start, end, session.trading_day, session.session_id, end, None,
            price, price + D(".2"), price - D(".2"), price, D(100000), D(1000), D(1000),
            True, 1 if duration == 60 else 1440, 1 if duration == 60 else 1440, "SYNTHETIC", q())
    minute = tuple(replace(bar(BASE + i * MINUTE, D(100 if side == 1 else 120) + side * D(".1") * i, 60, i,
                              sessions[80] if i >= 0 else sessions[79]), volume=D(100000 + (i % 7) * 1000)) for i in range(-1, 100))
    daily = tuple(bar(BASE + i * DAY_NS, D(100 if side == 1 else 200) + side * D(i + 80), 86400, i, sessions[i + 80]) for i in range(-80, 0))
    r = rule(CONTRACT, effective_from_nano=BASE - 300 * DAY_NS, effective_until_nano=BASE + 90 * DAY_NS,
             available_at_nano=BASE - 300 * DAY_NS, limit_up=D(10000), limit_down=D(".1"), participation_rate=D(".1"))
    bundle = FrozenInput(Path("SYNTHETIC"), HASH, (inst,), sessions, daily + minute, (), (r,))
    models, reference = synthetic_models(side)
    p, c = example()
    payoff = fit_path_payoff_model(observations(p, c, blocks=1, per_block=4), BASE - 50 * DAY_NS,
                                   c.run_policy.research_policy_version)
    exits = {e.exit_policy_id: e for mode in ("MINUTE", "DAILY") for e in exit_registry_specs(mode)}
    preliminary = Activation(bundle, models, payoff, reference, specs, tuple(exits[k] for k in sorted(exits)),
        tuple(sorted((legacy_exit_binding(s) for s in specs), key=lambda b: b.binding_sha256)),
        default_policies(specs), CONTRACT, BASE + 100 * MINUTE, HASH, {"synthetic": HASH}, True)
    proposal, context = production_candidate(preliminary, BASE + 100 * MINUTE + NS)
    assert proposal.action == "OPEN", proposal.reasons
    return replace(preliminary, payoff=payoff_for(proposal, context))


def event(kind, minute, *, price="110", historical=False, **changes):
    start = BASE + minute * MINUTE
    row = {"contract_id": CONTRACT, "raw_symbol": "SHFE.rb2605", "duration_seconds": 60,
           "start_nano": start, "sdk_id": minute, "open": price, "observed_progression": True,
           "historical": historical}
    if kind == "CLOSED":
        row.update(high=str(D(price) + D(".2")), low=str(D(price) - D(".2")), close=price,
                   volume="100000", open_oi="1000", close_oi="1000", progressed_to_nano=start + MINUTE)
    row.update(changes)
    return SourceEvent(kind + "-" + str(minute), kind, start, row)


def send(host, clock, *events):
    host.process_batch(received_batch(host.run_id, host.source_watermark + 1, events, clock()))


def production_candidate(activation, now, account=None):
    """Real causal context, numeric inference and natural three confirmations."""
    from ak_trading.v2.decision.pipeline import build_strategy_context
    from ak_trading.v2.probability.forecast import predict_market, publish_market_forecast
    from test_v2_m3b_matrix import account as empty_account
    strategy = strategy_registry_v2("MINUTE")["trend_m1_v2"]
    state, proposal, context = None, None, None
    for delta in (2, 1, 0):
        d = now - delta * MINUTE
        def provider(models, request, regime):
            if request.features.duration_seconds == 86400:
                return predict_market(models, request, regime, request.features.decision_nano)
            return publish_market_forecast(models, request, regime, clock_nano=lambda: d)
        context = build_strategy_context(activation.bundle, CONTRACT, d,
            account or empty_account(), state, activation.models, activation.payoff, strategy.spec,
            None, activation.reference, activation.run_policies[0], (),
            exit_binding=legacy_exit_binding(strategy.spec), forecast_provider=provider)
        proposal = strategy.evaluate(context, state)
        state = commit_observation(state, proposal)
    return proposal, context


def payoff_for(proposal, context):
    """Explicit synthetic path cashflows with all positive probability cells."""
    key = condition_key(proposal, context)
    values = json.loads(key)
    favorable = 0 if proposal.budget.side == 1 else 1
    rows = tuple(PayoffObservation("SYNTHETIC-" + str(block) + "-" + str(n), proposal.strategy_id,
        proposal.contract_id, context.features.product, key, context.exit_binding.binding_sha256, "OPEN_INITIAL",
        context.spec.max_holding, "ATR_1", values[12], block * 10 + 1, block * 10 + 2, block * 10 + 3,
        (date(2024, 1, 1) + timedelta(days=block)).isoformat(),
        ("UP_FIRST", "DOWN_FIRST", "NEITHER", "AMBIGUOUS")[n % 4], True,
        D(100 if n % 4 == favorable else -10), D(100), D(0 if n % 4 == favorable else 10), None, D(values[15]), HASH)
        for block in range(80) for n in range(8))
    return fit_path_payoff_model(rows, BASE - 50 * DAY_NS, context.run_policy.research_policy_version)


def test_real_context_and_publisher_natural_trend_candidate_has_no_fixture_gate():
    activation = synthetic_activation()
    p, c = production_candidate(activation, BASE + 100 * MINUTE + NS)
    assert p.action == "OPEN", (p.reasons, c.features.quality, c.regime, [f.quality for f in c.forecasts])
    from ak_trading.v2.decision.gates import evaluate_gate
    from ak_trading.v2.decision.payoffs import estimate_proposal_payoff
    from ak_trading.v2.decision.pipeline import model_hashes_for
    from ak_trading.v2.policy import research_policy
    model = payoff_for(p, c)
    c = replace(c, model_hashes=model_hashes_for(activation.models, model, activation.reference, None))
    p = replace(p, payoff=estimate_proposal_payoff(model, p, c, "SYNTHETIC"))
    gate = evaluate_gate(p, c, research_policy(), checked_at_nano=c.now_nano)
    if not gate.eligible:
        print({"payoff": encode_record(p.payoff), "planned_loss": str(p.budget.planned_loss_cny),
               "budget_frozen_range": encode_record(p.budget.frozen_range)
                   if p.budget.frozen_range is not None else None,
               "setup_frozen_range": encode_record(p.next_state.setup.frozen_range)
                   if p.next_state.setup is not None and p.next_state.setup.frozen_range is not None else None,
               "context_regime_frozen_range": encode_record(c.regime.frozen_range)
                   if c.regime.frozen_range is not None else None,
               "forecast_frozen_ranges": [encode_record(f.frozen_range) if f.frozen_range is not None else None
                                           for f in c.forecasts]})
    assert gate.eligible, (gate.reasons, p.payoff, p.budget)


def admit_control(host, *, side=1, mode="BEST_EV"):
    """Real context/inference/strategy/payoff/gate/risk/routing before S/C.

    The separate feed integration controls drive the full receive/clock path.
    """
    from ak_trading.v2.decision.execution import capacity_before
    from ak_trading.v2.decision.gates import evaluate_gate
    from ak_trading.v2.decision.payoffs import estimate_proposal_payoff
    from ak_trading.v2.decision.pipeline import exit_registry_hash_for, model_hashes_for
    from ak_trading.v2.decision.risk import enforce_account_risk
    from ak_trading.v2.decision.routing import choose_proposal, opportunity_id
    from ak_trading.v2.policy import research_policy
    book = host.accounts[mode]
    a, now = host.activation, host.clock().utc_nano
    account = book.ledger.snapshot(now)
    proposal, context = production_candidate(a, now, account)
    assert proposal.action == "OPEN" and proposal.budget.side == side, proposal.reasons
    opp = opportunity_id(CONTRACT, now, context.spec.max_holding, account)
    proposal = replace(proposal, payoff=estimate_proposal_payoff(a.payoff, proposal, context, opp))
    gate = evaluate_gate(proposal, context, research_policy(), checked_at_nano=now)
    assert gate.eligible, gate.reasons
    book.states[proposal.strategy_id] = commit_observation(None, proposal)
    cp = book.ledger._fork().checkpoint(tuple(book.states.values()), tuple(book.pending.values()),
        model_hashes_for(a.models, a.payoff, a.reference, None), {}, pending_remaining_lots=book.remaining,
        exit_registry_sha256=exit_registry_hash_for(a.exit_specs), exit_bindings=a.bindings,
        exit_states=(), exit_execution_links=())
    cap = capacity_before(context.bars, CONTRACT, 60, now, context.rule)
    gates = enforce_account_risk((proposal,), (gate,), cp.account, a.bundle.instruments,
        cp.states, cp.pending_intents, a.bundle.rules, {CONTRACT: cap}, research_policy(), valuations=cp.valuation_marks)
    decision = choose_proposal((proposal,), gates, account, opp, now, book.policy, ())
    assert decision.selected_proposal_id == proposal.proposal_id, decision
    host.records[mode]["features"].append(context.features)
    host.records[mode]["forecasts"].extend(context.forecasts)
    host.records[mode]["regimes"].append(context.regime)
    host._record_decision(book, decision, account,
        clocks={proposal.proposal_id: {"evaluation": host.clock().payload(), "gate": host.clock().payload()}})
    intents = host._new_intents(book, proposal, decision, now)
    book.ledger.sync_reservations(tuple(book.states.values()), a.bundle.rules, now)
    book.financial_cursor = now
    host._commit_intents({mode: intents})
    return intents[0]


def test_live_provider_preserves_observed_publication_and_reconstructed_background_axes(tmp_path):
    from ak_trading.v2.decision.pipeline import build_strategy_context
    a = synthetic_activation()
    d = BASE + 100 * MINUTE + NS
    clock = FakeClock(d + 100)
    host = ForwardPaperHost(tmp_path / "run", "r", clock=clock, activation=a, synthetic=True)
    spec = next(s for s in a.specs if s.family == "TREND")
    context = build_strategy_context(a.bundle, CONTRACT, d, host.accounts["BEST_EV"].ledger.snapshot(d),
        None, a.models, a.payoff, spec, None, a.reference, a.run_policies[0], (),
        exit_binding=legacy_exit_binding(spec), forecast_provider=host._provider(d))
    assert context.now_nano == context.features.decision_nano == d
    live = [f for f in host.facts if f["kind"] == "PUBLICATION" and f["role"] == "FOREGROUND_LIVE"]
    background = [f for f in host.facts if f["kind"] == "PUBLICATION" and f["role"] == "BACKGROUND_RECONSTRUCTED"]
    assert len(live) == 3 and len(background) == 1
    assert all(d < f["published_at_nano"] == d + 100 < f["window_start_nano"] for f in live)
    assert background[0]["decision_nano"] == BASE
    assert background[0]["publication_clock"] == "RESEARCH_RECONSTRUCTED_NOT_OBSERVED"
    assert background[0]["reconstruction_clock"]["raw_utc_nano"] == d + 100
    assert background[0]["source_hashes"]


@pytest.mark.parametrize("change", ["request_id", "geometry", "range", "model", "role"])
def test_provider_rejects_wrong_original_request_or_undeclared_role(tmp_path, change):
    from ak_trading.v2.probability.forecast import build_forecast_requests
    a = synthetic_activation()
    d = BASE + 100 * MINUTE + NS
    _, context = production_candidate(a, d)
    request = next(r for r in build_forecast_requests(context.features, (context.spec.max_holding,),
                   context.regime.frozen_range, a.bundle.sessions) if r.barrier_key == "ATR_1")
    models, provider_d = a.models, d
    if change == "request_id":
        request = replace(request, request_id="SYNTHETIC_WRONG_REQUEST")
    elif change == "geometry":
        request = replace(request, upper_distance=request.upper_distance + D(".1"))
    elif change == "range":
        request = replace(request, frozen_range=replace(request.frozen_range, source_snapshot_id="SYNTHETIC_WRONG_SOURCE"))
    elif change == "model":
        models = replace(models)
    else:
        provider_d += MINUTE
    host = ForwardPaperHost(tmp_path / "run", "r", clock=FakeClock(d + 10), activation=a, synthetic=True)
    with pytest.raises(ForwardPaperError, match="PROVIDER_|UNDECLARED_"):
        host._provider(provider_d)(models, request, context.regime)
    assert not [f for f in host.facts if f["kind"] == "PUBLICATION"]


@pytest.mark.parametrize("late_by", [0, 1])
def test_actual_provider_keeps_forecast_late_at_original_window(tmp_path, late_by):
    from ak_trading.v2.probability.forecast import build_forecast_requests
    a, d = synthetic_activation(), BASE + 100 * MINUTE + NS
    _, context = production_candidate(a, d)
    request = next(r for r in build_forecast_requests(context.features, (context.spec.max_holding,),
                   context.regime.frozen_range, a.bundle.sessions) if r.barrier_key == "ATR_1")
    clock = FakeClock(request.window_start_nano + late_by)
    host = ForwardPaperHost(tmp_path / "run", "r", clock=clock, activation=a, synthetic=True)
    forecast = host._provider(d)(a.models, request, context.regime)
    assert forecast.quality.status == "INVALID" and "FORECAST_LATE" in forecast.quality.reasons
    assert forecast.window_start_nano == request.window_start_nano and forecast.expires_at_nano == request.window_end_nano


@pytest.mark.parametrize("bad", [True, "clock", -1])
def test_provider_never_publishes_from_bad_completion_clock(tmp_path, bad):
    from ak_trading.v2.probability.forecast import build_forecast_requests, UnpublishableForecastError
    a, d = synthetic_activation(), BASE + 100 * MINUTE + NS
    _, context = production_candidate(a, d)
    request = next(r for r in build_forecast_requests(context.features, (context.spec.max_holding,),
                   context.regime.frozen_range, a.bundle.sessions) if r.barrier_key == "ATR_1")
    clock = FakeClock(d - 1) if bad == -1 else lambda: bad
    host = ForwardPaperHost(tmp_path / "run", "r", clock=clock, activation=a, synthetic=True)
    with pytest.raises((ForwardPaperError, UnpublishableForecastError)):
        host._provider(d)(a.models, request, context.regime)
    assert not [f for f in host.facts if f["kind"] == "PUBLICATION"]


class AdvancingClock(FakeClock):
    """Only the clock is controlled; each actual host sample advances UTC."""
    def __call__(self):
        result = super().__call__()
        self.when += 1000
        return result


def drive_host_prefix(tmp_path, *, suffix=False):
    a = synthetic_activation()
    if suffix:
        original = a.bundle.bars[-1]
        future = replace(original, bar_id="SYNTHETIC_UNAVAILABLE_SUFFIX", start_nano=BASE + 200 * MINUTE,
            datetime_nano=BASE + 200 * MINUTE, end_nano=BASE + 201 * MINUTE,
            available_at_nano=BASE + 201 * MINUTE, open=D(999), high=D(1000), low=D(998), close=D(999))
        a = replace(a, bundle=replace(a.bundle, bars=a.bundle.bars + (future,)))
    clock = AdvancingClock()
    host = ForwardPaperHost(tmp_path / "run", "natural", clock=clock, activation=a, synthetic=True)
    for minute in (101, 102, 103):
        clock.at(BASE + minute * MINUTE + NS)
        closed_price = str(D(110) + D(".1") * (minute - 101))
        opening_price = str(D(110) + D(".1") * (minute - 100))
        send(host, clock, event("CLOSED", minute - 1, price=closed_price), event("OPEN", minute, price=opening_price))
    return host, clock


def test_complete_nonempty_host_chain_and_four_original_policies(tmp_path):
    host, clock = drive_host_prefix(tmp_path)
    by_mode = host.records
    for mode, records in by_mode.items():
        assert records["decisions"]
        candidates = {p.strategy_id for d in records["decisions"] for p in d.proposals}
        assert candidates == {"trend_m1_v2", "breakout_m1_v2", "pullback_m1_v2"}
        assert all(d.mode == mode and d.purpose == "RESEARCH_EVALUATION" for d in records["decisions"])
    for mode in ("BEST_EV", "FIXED"):
        assert host.accounts[mode].pending, [(d.outcome, d.reasons, [(g.eligible, g.reasons) for g in d.gates]) for d in by_mode[mode]["decisions"]]
    assert not host.accounts["CASH"].pending and not by_mode["CASH"]["intents"]
    prefix_rows = [f for f in host.facts if f["kind"] == "DECISION" and f["origin"] == "STRATEGY"]
    for at in {d.decision_nano for d in by_mode["BEST_EV"]["decisions"]}:
        assert len({f["input_prefix_sha256"] for f in prefix_rows
                    if next(d for d in by_mode[f["mode"]]["decisions"] if d.decision_id == f["decision_id"]).decision_nano == at}) == 1
    # The OPEN precedes CLOSED even when supplied in the opposite input order.
    # The entry fills inside its production limit (reference + 0.1*ATR floored
    # to tick = 110.2); a worse open is legitimately ENTRY_GAP_EXCEEDED.
    clock.at(BASE + 104 * MINUTE + NS)
    send(host, clock, event("CLOSED", 103, price="110.3"), event("OPEN", 104, price="110.2"))
    for mode in ("BEST_EV", "FIXED"):
        assert by_mode[mode]["fills"] and host.accounts[mode].ledger._account.lots
        assert host.accounts[mode].ledger._account.fees_cny > 0
        fill = by_mode[mode]["fills"][0]
        assert fill.execution_nano == BASE + 104 * MINUTE
        assert fill.intent_id in {i.intent_id for i in by_mode[mode]["intents"]}
    clock.at(BASE + 106 * MINUTE + 2 * NS)
    send(host, clock, SourceEvent("risk-clock", "CLOCK", 0, {}))
    # A clock can create a future exit attempt; it never invents an OPEN/Fill.
    old_fills = {mode: len(r["fills"]) for mode, r in by_mode.items()}
    assert "OPEN-107" not in host.open_sources
    clock.at(BASE + 107 * MINUTE + NS)
    send(host, clock, event("OPEN", 107, price="110.3"))
    for mode in ("BEST_EV", "FIXED"):
        assert len(by_mode[mode]["fills"]) > old_fills[mode]
        assert not host.accounts[mode].ledger._account.lots
        assert host.accounts[mode].ledger.reconcile()["fees_match"]
    assert not by_mode["CASH"]["fills"] and host.accounts["CASH"].ledger._account.fees_cny == 0
    live = [f for f in host.facts if f["kind"] == "PUBLICATION" and f["role"] == "FOREGROUND_LIVE"]
    assert live and all(f["decision_nano"] < f["published_at_nano"] for f in live)
    for mode, records in by_mode.items():
        for decision in records["decisions"]:
            if decision.outcome == "SELECTED":
                proposal = next(p for p in decision.proposals if p.proposal_id == decision.selected_proposal_id)
                forecast = next(f for f in records["forecasts"] if f.forecast_id == proposal.forecast_id)
                gate = next(g for g in decision.gates if g.proposal_id == proposal.proposal_id)
                assert decision.decision_nano < forecast.published_at_nano <= gate.checked_at_nano < proposal.earliest_execution_nano
    host.finish("CONTROL_COMPLETE")
    expected = {m: encode_record(b.ledger._account) for m, b in host.accounts.items()}
    restored = ForwardPaperHost(host.journal.root, "natural", clock=clock, activation=host.activation, synthetic=True, resume=True)
    assert {m: encode_record(b.ledger._account) for m, b in restored.accounts.items()} == expected
    assert all(restored.accounts[m].ledger.reconcile()["fees_match"] for m in restored.accounts)


def test_unavailable_suffix_does_not_change_natural_committed_prefix(tmp_path):
    original, _ = drive_host_prefix(tmp_path / "original")
    changed, _ = drive_host_prefix(tmp_path / "changed", suffix=True)
    for mode in original.records:
        assert [encode_record(d) for d in original.records[mode]["decisions"]] == [encode_record(d) for d in changed.records[mode]["decisions"]]
        assert original.accounts[mode].pending == changed.accounts[mode].pending
    assert original.source_chain == changed.source_chain


def test_transiently_blocked_entry_retries_at_next_real_open(tmp_path):
    """Production 003 §4: a transiently blocked unfilled OPEN entry keeps its
    intent and signed remainder; only expiry or an independent cancel
    terminates it. The state machine must never settle it while pending."""
    clock, activation = FakeClock(), synthetic_activation()
    host = ForwardPaperHost(tmp_path / "run", "r", clock=clock, activation=activation, synthetic=True)
    intent = admit_control(host)
    book = host.accounts["BEST_EV"]
    reference = book.states[intent.strategy_id].budget.layers[0].reference_entry_price
    clock.at(BASE + 101 * MINUTE + NS)
    send(host, clock, event("OPEN", 101, price=str(reference + D("5"))))
    assert intent.intent_id in book.pending and book.remaining[intent.intent_id] == intent.delta_lots
    assert [(e.status, e.reasons) for e in host.records["BEST_EV"]["executions"]] == [
        ("BLOCKED", ("ENTRY_GAP_EXCEEDED",))]
    assert not host.records["BEST_EV"]["fills"] and not book.ledger._account.lots
    # A later real OPEN inside the original window fills the same intent once.
    clock.at(BASE + 102 * MINUTE + NS)
    send(host, clock, event("OPEN", 102, price=str(reference)))
    assert intent.intent_id not in book.pending and not book.remaining
    assert [f.intent_id for f in host.records["BEST_EV"]["fills"]] == [intent.intent_id]
    assert book.ledger._account.lots and book.ledger.reconcile()["fees_match"]
    assert [e.status for e in host.records["BEST_EV"]["executions"]] == ["BLOCKED", "FILLED"]


def admit_protective_control(host):
    """Actual original exit proposal, context, M4 management gate and routing."""
    from ak_trading.v2.data import visible_rule
    from ak_trading.v2.decision.gates import evaluate_gate
    from ak_trading.v2.decision.pipeline import build_strategy_context
    from ak_trading.v2.decision.replay import _hard_exit
    from ak_trading.v2.decision.routing import choose_proposal, opportunity_id
    from ak_trading.v2.policy import research_policy
    book, a = host.accounts["BEST_EV"], host.activation
    now = host.clock().utc_nano
    account = book.ledger.snapshot(now)
    state = book.states["trend_m1_v2"]
    p = _hard_exit(state, account, now, BASE + 102 * MINUTE, "STOP_TOUCHED")
    spec = next(s for s in a.specs if s.strategy_id == p.strategy_id)
    c = build_strategy_context(host._bundle(), CONTRACT, now, account, state, a.models, a.payoff,
        spec, None, a.reference, book.policy, (), exit_binding=legacy_exit_binding(spec),
        forecast_provider=host._provider(now))
    gate = evaluate_gate(p, c, research_policy(), checked_at_nano=now)
    assert gate.eligible, gate.reasons
    decision = choose_proposal((p,), (gate,), account, opportunity_id(CONTRACT, now, spec.max_holding, account),
                               now, book.policy, ())
    assert decision.selected_proposal_id == p.proposal_id
    host.records["BEST_EV"]["features"].append(c.features)
    host.records["BEST_EV"]["forecasts"].extend(c.forecasts)
    host.records["BEST_EV"]["regimes"].append(c.regime)
    host._record_decision(book, decision, account,
        clocks={p.proposal_id: {"evaluation": host.clock().payload(), "gate": host.clock().payload()}})
    book.states[p.strategy_id] = commit_observation(state, p)
    intents = host._new_intents(book, p, decision, now)
    book.ledger.sync_reservations(tuple(book.states.values()), a.bundle.rules, now)
    book.financial_cursor = now
    host._commit_intents({"BEST_EV": intents})
    assert intents and intents[0].purpose == "STOP"
    return intents[0]


def protective_host(tmp_path, side):
    clock, activation = FakeClock(), synthetic_activation(side)
    host = ForwardPaperHost(tmp_path / "run", "r", clock=clock, activation=activation, synthetic=True)
    admit_control(host, side=side)
    opening = str(host.accounts["BEST_EV"].states["trend_m1_v2"].budget.layers[0].reference_entry_price)
    clock.at(BASE + 101 * MINUTE + NS)
    send(host, clock, event("OPEN", 101, price=opening))
    assert host.accounts["BEST_EV"].ledger._account.lots
    clock.at(BASE + 101 * MINUTE + 2 * NS)
    intent = admit_protective_control(host)
    clock.at(BASE + 102 * MINUTE + NS)
    send(host, clock, event("OPEN", 102, price=opening))
    assert host.accounts["BEST_EV"].barrier is not None
    return host, clock, intent


@pytest.mark.parametrize("side", [1, -1])
def test_nonempty_protective_barrier_waits_and_uses_original_stop_first(tmp_path, side):
    host, clock, intent = protective_host(tmp_path, side)
    book = host.accounts["BEST_EV"]
    account, cursor, state = book.ledger._account, book.financial_cursor, book.states[intent.strategy_id]
    assert len(host.records["BEST_EV"]["fills"]) == 1
    # Historical closed rows supply genuine recovery data, without invoking a
    # foreground strategy path whose independent live-clock prerequisite is gated.
    clock.at(BASE + 103 * MINUTE + NS)
    send(host, clock, event("OPEN", 103, price="999"), SourceEvent("health", "HEALTH", 0, {"connected": False}))
    assert book.ledger._account == account and book.financial_cursor == cursor and book.states[intent.strategy_id] == state
    price = book.barrier["open"]["open"]
    high, low = str(max(state.budget.stop_price, state.budget.target_price) + 1), str(min(state.budget.stop_price, state.budget.target_price) - 1)
    send(host, clock, event("CLOSED", 102, price=price, high=high, low=low, historical=True))
    assert book.barrier is None and not book.ledger._account.lots
    result = host.records["BEST_EV"]["executions"][-1]
    assert result.status == "FILLED" and "STOP_FIRST_AMBIGUITY" in result.reasons
    assert result.fills[0].reference_price == state.budget.stop_price
    assert result.fills[0].execution_nano == BASE + 102 * MINUTE
    assert book.ledger._account.as_of_nano >= cursor and book.ledger.reconcile()["fees_match"]
    facts = [f for f in host.facts if f["kind"] == "PROTECTIVE_RECEIPT"]
    assert facts[-1]["recovered_receipt"] and not facts[-1]["timely_forward_credit"]
    fees = book.ledger._account.fees_cny
    send(host, clock, event("CLOSED", 102, price=price, high=high, low=low, historical=True))
    assert book.ledger._account.fees_cny == fees


@pytest.mark.parametrize("side", [1, -1])
def test_protective_timeout_crash_restore_late_original_keeps_frozen_book(tmp_path, side):
    host, clock, intent = protective_host(tmp_path, side)
    original = host.accounts["BEST_EV"].ledger._account
    clock.at(BASE + 105 * MINUTE + NS)
    send(host, clock, SourceEvent("timeout", "CLOCK", 0, {}))
    book = host.accounts["BEST_EV"]
    assert "PROTECTIVE_RECEIPT_TIMEOUT" in book.barrier["incomplete_reasons"]
    # Preserve later raw data while the owned financial account stays frozen.
    send(host, clock, event("CLOSED", 104, historical=True))
    assert book.ledger._account == original
    host.finish("INTERRUPTED")
    if side == 1:
        script = """import sys,json
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'tests'))
from test_v2_forward_paper import FakeClock,synthetic_activation,BASE,MINUTE,NS
from ak_trading.v2.validation.forward_paper import ForwardPaperHost
from ak_trading.v2.schemas import encode_record
h=ForwardPaperHost(Path(sys.argv[1]),'r',clock=FakeClock(BASE+105*MINUTE+NS),activation=synthetic_activation(),synthetic=True,resume=True)
print(json.dumps({'account':encode_record(h.accounts['BEST_EV'].ledger._account),'barrier':h.accounts['BEST_EV'].barrier is not None,'sdk':'tqsdk' in sys.modules}))
"""
        process = subprocess.run([sys.executable, "-c", script, str(host.journal.root)], capture_output=True,
                                 text=True, timeout=45)
        assert process.returncode == 0, process.stderr
        assert json.loads(process.stdout) == {"account": encode_record(original), "barrier": True, "sdk": False}
    restored = ForwardPaperHost(host.journal.root, "r", clock=clock, activation=host.activation,
                               synthetic=True, resume=True)
    book = restored.accounts["BEST_EV"]
    assert book.ledger._account == original and book.barrier is not None and book.pending
    budget = book.states[intent.strategy_id].budget
    price = book.barrier["open"]["open"]
    high, low = str(max(budget.stop_price, budget.target_price) + 1), str(min(budget.stop_price, budget.target_price) - 1)
    send(restored, clock, event("CLOSED", 102, price=price, high=high, low=low, historical=True))
    assert book.barrier is None and not book.ledger._account.lots
    assert restored.forward_bar_count == 0 and not restored.strategy_batches
    fills, fees = tuple(restored.records["BEST_EV"]["fills"]), book.ledger._account.fees_cny
    again = ForwardPaperHost(host.journal.root, "r", clock=clock, activation=host.activation,
                            synthetic=True, resume=True)
    assert tuple(again.records["BEST_EV"]["fills"]) == fills and again.accounts["BEST_EV"].ledger._account.fees_cny == fees


def rewrite_last_member(root, name, mutate):
    """SYNTHETIC corruption with repaired outer hashes probes semantic checks."""
    identity = parse_json_bytes((root / "IDENTITY.json").read_bytes())
    journal = Journal(root, identity, create=False)
    directory = root / "segments" / f"{journal.sequence:08d}"
    member = parse_json_bytes((directory / name).read_bytes())
    mutate(member)
    raw = canonical_json_bytes(member)
    (directory / name).write_bytes(raw)
    manifest = parse_json_bytes((directory / "manifest.json").read_bytes())
    manifest["files"][name] = {"bytes": len(raw), "sha256": sha256_hex(raw)}
    (directory / "manifest.json").write_bytes(canonical_json_bytes(manifest))


@pytest.mark.parametrize("change", ["s_reference", "intent_set", "clock_qualification", "financial_parent"])
def test_confirmation_semantic_corruption_is_rejected_even_with_new_member_hash(tmp_path, change):
    clock, activation = FakeClock(), synthetic_activation()
    host = ForwardPaperHost(tmp_path / "run", "r", clock=clock, activation=activation, synthetic=True)
    intent = admit_control(host)
    def mutate(confirmation):
        if change == "s_reference":
            confirmation["s_manifest_sha256"] = "1" * 64
        elif change == "intent_set":
            confirmation["intent_ids"]["BEST_EV"].append("FOREIGN_INTENT")
        elif change == "clock_qualification":
            confirmation["s_completed"].update(raw_utc_nano=intent.not_before_nano,
                monotonic_ns=intent.not_before_nano, sampled_utc_nano=intent.not_before_nano)
        else:
            confirmation["original_financial_parent"]["BEST_EV"] += 1
    rewrite_last_member(host.journal.root, "EVENT.json", mutate)
    with pytest.raises(ForwardPaperError, match="CONFIRMATION"):
        ForwardPaperHost(host.journal.root, "r", clock=clock, activation=activation, synthetic=True, resume=True)


@pytest.mark.parametrize("change", ["missing_decision", "selected", "intent", "account", "prefix", "receive_chain"])
def test_original_selected_decision_and_intent_sources_are_required_on_recovery(tmp_path, change):
    clock, activation = FakeClock(), synthetic_activation()
    host = ForwardPaperHost(tmp_path / "run", "r", clock=clock, activation=activation, synthetic=True)
    def cut(where):
        if where == "before_c_seal":
            raise RuntimeError("SYNTHETIC_UNCONFIRMED_CUT")
    host.fault = cut
    with pytest.raises(RuntimeError, match="SYNTHETIC_UNCONFIRMED_CUT"):
        admit_control(host)
    assert host.journal.segments[-1][0]["kind"] == "INTENT_S"
    if change in ("missing_decision", "selected", "intent"):
        def mutate(records):
            rows = records["BEST_EV"]
            if change == "missing_decision":
                rows["decisions"].clear()
            elif change == "selected":
                rows["decisions"][0]["selected_proposal_id"] = None
            else:
                rows["intents"][0]["decision_id"] = "SYNTHETIC_FOREIGN_DECISION"
        rewrite_last_member(host.journal.root, "RECORDS.json", mutate)
    else:
        def mutate(envelope):
            fact = next(f for f in envelope["facts"] if f["kind"] == "DECISION")
            if change == "account":
                fact["account_before"]["cash_cny"] = "1"
            elif change == "prefix":
                fact["input_prefix_sha256"] = "1" * 64
            else:
                fact["source_chain"] = "2" * 64
        rewrite_last_member(host.journal.root, "HOST.json", mutate)
    with pytest.raises((ForwardPaperError, ValueError), match="RECOVERY_|[Aa]ccount|record_construction_failed"):
        ForwardPaperHost(host.journal.root, "r", clock=clock, activation=activation, synthetic=True, resume=True)


def test_original_market_source_drift_rejected_and_report_same_in_fresh_cli(tmp_path):
    clock, activation = FakeClock(), synthetic_activation()
    host = ForwardPaperHost(tmp_path / "run", "r", clock=clock, activation=activation, synthetic=True)
    admit_control(host)
    opening = str(host.accounts["BEST_EV"].states["trend_m1_v2"].budget.layers[0].reference_entry_price)
    clock.at(BASE + 101 * MINUTE + NS)
    send(host, clock, event("OPEN", 101, price=opening))
    clock.at(BASE + 102 * MINUTE + NS)
    send(host, clock, event("CLOSED", 101, historical=True))
    host.finish("INTERRUPTED")
    expected = host.report()
    result = subprocess.run([sys.executable, "scripts/run_forward_paper.py", "report", "--run-dir", str(host.journal.root)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    actual = parse_json_bytes(Path(json.loads(result.stdout)["report"]).read_bytes())
    assert actual == expected
    summary = actual["accounts"]["BEST_EV"]["financial_summary"]
    assert summary["inventory_peak_lots"] > 0 and D(summary["used_margin_peak_cny"]) > 0
    def mutate(envelope):
        envelope["open_sources"]["OPEN-101"]["event"]["data"]["open"] = "1"
    rewrite_last_member(host.journal.root, "HOST.json", mutate)
    with pytest.raises(ForwardPaperError, match="RECOVERY_MARKET_ORIGINAL_MISMATCH"):
        ForwardPaperHost(host.journal.root, "r", clock=clock, activation=activation, synthetic=True, resume=True)


def test_receive_is_deeply_copied_and_open_closed_phases_are_distinct(tmp_path):
    clock = FakeClock()
    data = {"nested": {"value": [1]}}
    item = SourceEvent("b", "BOOTSTRAP", 0, data)
    data["nested"]["value"].append(2)
    assert item.data["nested"]["value"] == (1,)
    with pytest.raises(TypeError):
        item.data["nested"]["x"] = 2
    host = ForwardPaperHost(tmp_path / "capture", "capture", clock=clock, synthetic=True)
    clock.at(BASE + 101 * MINUTE + NS)
    send(host, clock, event("CLOSED", 100), event("OPEN", 101))
    kinds = [s["EVENT.json"].get("kind") for m, s in host.journal.segments if m["kind"] == "EVENT"]
    assert kinds == ["OPEN", "CLOSED"] and host.forward_bar_count == 1
    assert host.report()["strategy_evaluation"] == "NOT_RUN"
    assert host.report()["paper_account"] == "NOT_ACTIVATED"
    assert host.report()["publication_latency"] == {"n": 0, "p99_nano": None, "status": "INSUFFICIENT"}


@pytest.mark.parametrize("error,uncertainty,valid", [(100_000_000, 0, True), (100_000_000, 1, False),
                                                   (0, 100_000_001, False)])
def test_independent_clock_error_boundary(error, uncertainty, valid):
    c = replace(FakeClock()(), offset_nano=error, uncertainty_ns=uncertainty)
    assert (not c.reasons()) is valid


def test_clock_unknown_expiry_reversal_jump_and_bool():
    c = FakeClock()()
    assert "CLOCK_UNVERIFIED" in replace(c, offset_nano=None).reasons()
    assert "CLOCK_EVIDENCE_EXPIRED" in replace(c, sampled_utc_nano=c.raw_utc_nano - 60 * NS - 1).reasons()
    assert "CLOCK_REVERSED" in replace(c, raw_utc_nano=c.raw_utc_nano - 1).reasons(c)
    assert "CLOCK_JUMP" in replace(c, raw_utc_nano=c.raw_utc_nano + NS).reasons(c)
    with pytest.raises(ForwardPaperError):
        replace(c, raw_utc_nano=True)


def test_duplicate_gap_conflict_timer_and_existing_run_are_honest(tmp_path):
    clock = FakeClock()
    host = ForwardPaperHost(tmp_path / "capture", "c", clock=clock, synthetic=True)
    batch = received_batch("c", 1, (SourceEvent("timer", "CLOCK", 0, {}),), clock())
    host.process_batch(batch)
    host.process_batch(batch)
    assert host.forward_bar_count == 0 and not host.open_sources
    host.process_batch(received_batch("c", 3, (SourceEvent("later", "CLOCK", 0, {}),), clock()))
    assert "SOURCE_GAP" in host.pauses
    host.process_batch(received_batch("c", 3, (SourceEvent("conflict", "CLOCK", 0, {}),), clock()))
    assert host.conflicted
    with pytest.raises(FileExistsError):
        ForwardPaperHost(tmp_path / "capture", "c", clock=clock, synthetic=True)


@pytest.mark.parametrize("point,committed", [("payloads_fsynced", False), ("manifest_fsynced", False),
    ("segment_published", True), ("before_head", True), ("head_published", True)])
def test_journal_fault_cuts_head_is_only_a_hint(tmp_path, point, committed):
    root, identity = tmp_path / "j", {"schema_version": SCHEMA, "run_id": "j"}
    journal = Journal(root, identity)
    journal.seal("START", {}, {}, {}, {})
    def fail(where):
        if where == point:
            raise RuntimeError("SYNTHETIC_CRASH")
    journal.fault = fail
    with pytest.raises(RuntimeError, match="SYNTHETIC_CRASH"):
        journal.seal("NEXT", {"x": 1}, {}, {}, {})
    restored = Journal(root, identity, create=False)
    assert restored.sequence == (2 if committed else 1)
    assert bool(restored.orphans) is (not committed)
    if (root / "HEAD.json").exists():
        (root / "HEAD.json").unlink()
    assert Journal(root, identity, create=False).sequence == restored.sequence


def test_journal_integrity_members_chain_and_credential_paths(tmp_path):
    root = tmp_path / "j"
    journal = Journal(root, {"id": "j"})
    original = {"state": [1]}
    journal.seal("ONE", {}, original, {}, {})
    original["state"].append(2)
    assert journal.segments[-1][1]["HOST.json"]["state"] == [1]
    (root / "segments/00000001/HOST.json").write_bytes(b"{}\n")
    with pytest.raises(ForwardPaperError, match="MEMBER_HASH"):
        Journal(root, {"id": "j"}, create=False)
    with pytest.raises(ForwardPaperError, match="CREDENTIAL_PATH"):
        verified_file({"path": str(tmp_path / ".env"), "bytes": 0, "sha256": HASH})


@pytest.mark.parametrize("operation", ["seal", "report"])
def test_output_budget_is_checked_before_every_write_and_preserves_committed_prefix(tmp_path, operation):
    journal = Journal(tmp_path / "bounded", {"run_id": "bounded"})
    journal.seal("START", {}, {}, {}, {})
    initial_sequence, initial_parent = journal.sequence, journal.parent
    used = sum(p.stat().st_size for p in journal.root.rglob("*") if p.is_file())
    journal.output_limit_bytes = used + 200
    with pytest.raises(ForwardPaperError, match="OUTPUT_BUDGET"):
        if operation == "seal":
            journal.seal("TOO_LARGE", {"large": "x" * 1000}, {}, {}, {})
        else:
            journal.report({"run_id": "bounded", "large": "x" * 1000})
    assert sum(p.stat().st_size for p in journal.root.rglob("*") if p.is_file()) <= used + 200
    restored = Journal(journal.root, {"run_id": "bounded"}, create=False)
    assert (restored.sequence, restored.parent) == (initial_sequence, initial_parent)


@pytest.mark.parametrize("changes", [{"high": "1"}, {"volume": "-1"}, {"open": "NaN"},
                                     {"close": "unparseable"}, {"raw_symbol": "SHFE.cu2605"}])
def test_capture_does_not_credit_invalid_ohlc_or_contract(tmp_path, changes):
    clock = FakeClock(BASE + 101 * MINUTE + NS)
    host = ForwardPaperHost(tmp_path / "capture", "c", clock=clock, synthetic=True)
    send(host, clock, event("CLOSED", 100, **changes))
    assert host.forward_bar_count == 0
    assert host.raw_closed and host.facts[-1]["kind"] == "CLOSED_REJECTED"


@pytest.mark.parametrize("cut", ["s_returned", "s_completion_sampled", "before_c_seal"])
def test_missing_confirmation_never_signs_from_restart_clock_and_releases_entry(tmp_path, cut):
    clock, activation = FakeClock(), synthetic_activation()
    host = ForwardPaperHost(tmp_path / "run", "r", clock=clock, activation=activation, synthetic=True)
    def fail(where):
        if where == cut:
            raise RuntimeError("SYNTHETIC_CRASH")
    host.fault = fail
    with pytest.raises(RuntimeError, match="SYNTHETIC_CRASH"):
        admit_control(host)
    assert host.poisoned
    with pytest.raises(ForwardPaperError, match="RECOVERY_REQUIRED"):
        send(host, clock, event("OPEN", 101))
    s = host.journal.segments[-1][1]
    assert s["CHECKPOINTS.json"]["BEST_EV"]["account"]["reserved_margin_cny"] != "0"
    restored = ForwardPaperHost(tmp_path / "run", "r", clock=clock, activation=activation, synthetic=True, resume=True)
    book = restored.accounts["BEST_EV"]
    assert not book.pending and not book.ledger._account.lots and book.ledger._account.reserved_margin_cny == 0
    assert not restored.records["BEST_EV"]["fills"]
    assert any(x["status"] == "UNCONFIRMED_AFTER_CRASH" for x in book.admission.values())
    again = ForwardPaperHost(tmp_path / "run", "r", clock=clock, activation=activation, synthetic=True, resume=True)
    assert again.accounts["BEST_EV"].ledger._account == book.ledger._account


@pytest.mark.parametrize("point,confirmed", [("payloads_fsynced", False), ("manifest_fsynced", False),
    ("segment_published", True), ("before_head", True), ("head_published", True)])
def test_c_publication_faults_recover_only_actual_committed_qualification(tmp_path, point, confirmed):
    clock, activation = FakeClock(), synthetic_activation()
    host = ForwardPaperHost(tmp_path / "run", "r", clock=clock, activation=activation, synthetic=True)
    c_started = [False]
    def control_cut(where):
        if where == "before_c_seal":
            c_started[0] = True
    def storage_cut(where):
        if c_started[0] and where == point:
            raise RuntimeError("SYNTHETIC_C_CRASH")
    host.fault, host.journal.fault = control_cut, storage_cut
    with pytest.raises(RuntimeError, match="SYNTHETIC_C_CRASH"):
        admit_control(host)
    restored = ForwardPaperHost(host.journal.root, "r", clock=clock, activation=activation, synthetic=True, resume=True)
    book = restored.accounts["BEST_EV"]
    assert bool(book.pending) is confirmed and not book.ledger._account.lots
    assert bool(book.ledger._account.reserved_margin_cny) is confirmed
    statuses = {value["status"] for value in book.admission.values()}
    assert statuses == ({"CONFIRMED_ELIGIBLE"} if confirmed else {"UNCONFIRMED_AFTER_CRASH"})


@pytest.mark.parametrize("cut", ["MISSING_C", "LATE_S"])
def test_unconfirmed_or_late_exit_attempt_keeps_position_costs_and_exit_debt(tmp_path, cut):
    clock, activation = FakeClock(), synthetic_activation()
    host = ForwardPaperHost(tmp_path / "run", "r", clock=clock, activation=activation, synthetic=True)
    entry = admit_control(host)
    opening = str(host.accounts["BEST_EV"].states[entry.strategy_id].budget.layers[0].reference_entry_price)
    clock.at(BASE + 101 * MINUTE + NS)
    send(host, clock, event("OPEN", 101, price=opening))
    original = host.accounts["BEST_EV"].ledger._account
    clock.at(BASE + 101 * MINUTE + 2 * NS)
    def fault(where):
        if where == "s_returned":
            if cut == "MISSING_C":
                raise RuntimeError("SYNTHETIC_EXIT_CRASH")
            clock.at(BASE + 102 * MINUTE)
    host.fault = fault
    if cut == "MISSING_C":
        with pytest.raises(RuntimeError, match="SYNTHETIC_EXIT_CRASH"):
            admit_protective_control(host)
        host = ForwardPaperHost(host.journal.root, "r", clock=clock, activation=activation, synthetic=True, resume=True)
    else:
        admit_protective_control(host)
    book = host.accounts["BEST_EV"]
    assert replace(book.ledger._account, as_of_nano=original.as_of_nano) == original and not book.pending
    assert book.debt == {"trend_m1_v2"} and book.states["trend_m1_v2"].phase == "EXITING"
    assert book.states["trend_m1_v2"].active_exit_nano is not None
    assert len(host.records["BEST_EV"]["fills"]) == 1


@pytest.mark.parametrize("late_by", [0, 1])
def test_s_completion_plus_uncertainty_equality_and_late_reject(tmp_path, late_by):
    clock, activation = FakeClock(), synthetic_activation()
    host = ForwardPaperHost(tmp_path / "run", "r", clock=clock, activation=activation, synthetic=True)
    def advance(where):
        if where == "s_returned":
            clock.at(BASE + 101 * MINUTE - 10 + late_by)
            clock.uncertainty = 10
    host.fault = advance
    intent = admit_control(host)
    book = host.accounts["BEST_EV"]
    assert book.admission[intent.intent_id]["status"] == "MISSED_COMMIT_WINDOW"
    assert not book.pending and not book.ledger._account.lots and book.ledger._account.reserved_margin_cny == 0


@pytest.mark.parametrize("processing_age,filled", [(NS, True), (5 * NS, True), (5 * NS + 1, False)])
def test_on_time_s_c_may_cross_open_but_processing_age_is_independent(tmp_path, processing_age, filled):
    clock, activation = FakeClock(), synthetic_activation()
    host = ForwardPaperHost(tmp_path / "run", "r", clock=clock, activation=activation, synthetic=True)
    def c_crosses_open(where):
        if where == "before_c_seal":
            clock.at(BASE + 101 * MINUTE + processing_age)
    host.fault = c_crosses_open
    intent = admit_control(host)
    opening = str(host.accounts["BEST_EV"].states[intent.strategy_id].budget.layers[0].reference_entry_price)
    receipt_clock = replace(clock(), raw_utc_nano=BASE + 101 * MINUTE + NS,
                            monotonic_ns=BASE + 101 * MINUTE + NS, sampled_utc_nano=BASE + 101 * MINUTE + NS)
    host.process_batch(received_batch("r", 1, (event("OPEN", 101, price=opening),), receipt_clock))
    assert bool(host.accounts["BEST_EV"].ledger._account.lots) is filled
    assert bool(host.records["BEST_EV"]["fills"]) is filled
    assert host.accounts["CASH"].ledger._account.fees_cny == 0


@pytest.mark.parametrize("historical,age", [(True, NS), (False, 5 * NS + 1)])
def test_historical_and_late_open_never_backfill(tmp_path, historical, age):
    clock, activation = FakeClock(), synthetic_activation()
    host = ForwardPaperHost(tmp_path / "run", "r", clock=clock, activation=activation, synthetic=True)
    intent = admit_control(host)
    opening = str(host.accounts["BEST_EV"].states[intent.strategy_id].budget.layers[0].reference_entry_price)
    clock.at(BASE + 101 * MINUTE + age)
    send(host, clock, event("OPEN", 101, price=opening, historical=historical))
    assert not host.records["BEST_EV"]["fills"]


def test_fresh_process_capture_restore_and_cli_report_zero_sdk(tmp_path):
    root, clock = tmp_path / "run", FakeClock()
    host = ForwardPaperHost(root, "fresh", clock=clock, synthetic=True)
    send(host, clock, SourceEvent("c", "CLOCK", 0, {}))
    script = """import sys,json
from pathlib import Path
from ak_trading.v2.validation.forward_paper import ForwardPaperHost,ClockSample
h=ForwardPaperHost(Path(sys.argv[1]),'fresh',clock=lambda:ClockSample(1,1,'a'*64),synthetic=True,resume=True)
print(json.dumps({'sequence':h.journal.sequence,'fills':len(h.records['BEST_EV']['fills']),'sdk':'tqsdk' in sys.modules}))
"""
    process = subprocess.run([sys.executable, "-c", script, str(root)], capture_output=True, text=True, timeout=30)
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout) == {"sequence": host.journal.sequence, "fills": 0, "sdk": False}
    cli = subprocess.run([sys.executable, "scripts/run_forward_paper.py", "report", "--run-dir", str(root)],
                         capture_output=True, text=True, timeout=30)
    assert cli.returncode == 0, cli.stderr
    report = parse_json_bytes(Path(json.loads(cli.stdout)["report"]).read_bytes())
    assert report["forward_bar_count"] == 0 and report["strategy_evaluation"] == "NOT_RUN"


def test_termination_keeps_actual_risk_and_immutable_report_revisions(tmp_path):
    clock, activation = FakeClock(), synthetic_activation()
    host = ForwardPaperHost(tmp_path / "run", "r", clock=clock, activation=activation, synthetic=True)
    admit_control(host)
    opening = str(host.accounts["BEST_EV"].states["trend_m1_v2"].budget.layers[0].reference_entry_price)
    clock.at(BASE + 101 * MINUTE + NS)
    send(host, clock, event("OPEN", 101, price=opening))
    first = host.finish("INTERRUPTED")
    second = host.write_report()
    assert first != second and first.is_file() and first.with_name("REPORT.md").is_file()
    assert host.report()["accounts"]["BEST_EV"]["status"] == "INCOMPLETE"
    assert host.accounts["BEST_EV"].ledger._account.lots and host.report()["real_send_count"] == 0
