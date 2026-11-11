"""K05 natural-chain smoke (r4-plan01 §5): every registered trading family
reaches a genuine OPEN through the REAL production chain on SYNTHETIC
realistic-shape data, and WAIT never opens.

Chain under test (no hand-set regimes, no forced forecasts, no fabricated
OPEN): ``decision.pipeline.build_strategy_context`` → real
``strategy.evaluate`` → real payoff attachment
(``estimate_proposal_payoff``) → ``gates.evaluate_gate`` →
``routing.choose_proposal``. Regimes come from ``assess_regime`` with a
published daily background, forecasts from the real predictor/publisher over
prebuilt SYNTHETIC model populations (declared provenance), payoffs from the
reused immutable M4 synthetic payoff builder. Availability delays 0, 1ns,
1s and 59s ride on the bars themselves; per K01 the observation clock is
bar-driven, so a delayed decision still confirms on the bar it observes.

All populations are SYNTHETIC engineering data, never historical PIT.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

import v2_realistic_fixtures as rf

TRADING_FAMILIES = ("TREND", "BREAKOUT", "PULLBACK", "REVERSION", "GRID", "MARTINGALE")
DELAYS = {"0s": 0, "1ns": 1, "1s": 1_000_000_000, "59s": 59 * 1_000_000_000}


@pytest.mark.parametrize("delay_ns", DELAYS.values(), ids=list(DELAYS))
@pytest.mark.parametrize("mode", ["MINUTE", "DAILY"])
@pytest.mark.parametrize("family", TRADING_FAMILIES)
def test_natural_chain_open(family, mode, delay_ns):
    """WAIT/WAIT/OPEN with counts 1,2,3; the OPEN passes the real payoff
    gate and is selected by the real router."""
    scn, result = rf.natural_open(family, mode, delay_ns=delay_ns)
    trace = [(record["action"], record["count"]) for record in result["trace"]]
    assert trace == [("WAIT", 1), ("WAIT", 2), ("OPEN", 3)], trace
    for record in result["trace"]:
        # genuine inference/publication ran at every decision
        assert any(kind == "publish_market_forecast"
                   for kind, _, _ in record["publications"])
    open_record = result["open"]
    assert open_record is not None
    assert open_record["gate"].eligible, open_record["gate"].reasons
    assert open_record["routing"].selected_proposal_id is not None
    assert open_record["routing"].outcome == "SELECTED"


@pytest.mark.parametrize("mode", ["MINUTE", "DAILY"])
def test_wait_family_never_opens(mode):
    _, result = rf.natural_open("WAIT", mode)
    for record in result["trace"]:
        assert record["action"] == "WAIT" and record["count"] == 0
        assert "NO_NEW_RISK" in record["reasons"]


# --- negatives ------------------------------------------------------------------


def test_duplicate_delivery_never_double_counts():
    """Delivering the same decision twice advances the counter exactly once
    (K01 duplicate-bar idempotence through the full natural chain)."""
    scn = rf.scenario("TREND", "MINUTE")
    harvest = rf.run_chain(scn, scn["base_models"], collect_events=rf.FAMILY_EVENTS["TREND"])
    bound = rf.bind_evidence(scn["base_models"], harvest["probes"], "TREND")
    d0, d1 = scn["decisions"][0], scn["decisions"][1]
    result = rf.run_chain(dict(scn, decisions=[d0, d0, d1]), bound)
    counts = [(record["action"], record["count"]) for record in result["trace"]]
    assert counts == [("WAIT", 1), ("WAIT", 1), ("WAIT", 2)], counts


def test_unavailable_bar_is_not_an_observation():
    """A decision before the bar's availability sees no eligible bar: the
    counter fails closed at zero and no setup is established."""
    delay = 59 * 1_000_000_000
    scn = rf.scenario("REVERSION", "MINUTE", delay_ns=delay)
    harvest = rf.run_chain(scn, scn["base_models"], collect_events=rf.FAMILY_EVENTS["REVERSION"])
    bound = rf.bind_evidence(scn["base_models"], harvest["probes"], "REVERSION")
    first_end = scn["observations"][0].end_nano
    early = rf.run_chain(dict(scn, decisions=[first_end + 1]), bound)
    record = early["trace"][0]
    assert record["action"] == "WAIT" and record["count"] == 0
    assert record["reasons"], "an explicit no-observation reason is expected"


def test_no_bar_of_the_frequency_fails_closed():
    """A minute decision on a bundle with only daily bars has no observation
    of the spec's frequency: the production context builder itself refuses
    to fabricate a snapshot (fail closed), rather than letting a strategy
    count anything."""
    scn = rf.scenario("REVERSION", "MINUTE")
    from ak_trading.v2.contracts import FrozenInput
    from ak_trading.v2.factors import FactorsError
    daily_only = dict(scn, bundle=FrozenInput(
        scn["bundle"].root, scn["bundle"].manifest_sha256, scn["bundle"].instruments,
        scn["bundle"].sessions,
        tuple(b for b in scn["bundle"].bars if b.duration_seconds == 86400),
        scn["bundle"].macro_items, scn["bundle"].rules))
    with pytest.raises(FactorsError, match="no_visible_bars_for_snapshot"):
        rf.run_chain(daily_only, scn["base_models"])


def test_bar_beyond_expiry_restarts_at_one():
    """A genuinely later bar (end strictly after the setup expiry) expires
    the period; the rebuilt observation restarts at 1 without OPEN."""
    from ak_trading.v2.contracts import FrozenInput
    scn = rf.scenario("TREND", "DAILY")
    harvest = rf.run_chain(scn, scn["base_models"], collect_events=rf.FAMILY_EVENTS["TREND"])
    bound = rf.bind_evidence(scn["base_models"], harvest["probes"], "TREND")
    establishing = rf.run_chain(dict(scn, decisions=scn["decisions"][:2]), bound)
    assert [record["count"] for record in establishing["trace"]] == [1, 2]
    extra = rf.daily_bar(len(scn["observations"]), 113.0, scn["bundle"].sessions)
    extended = dict(scn, bundle=FrozenInput(
        scn["bundle"].root, scn["bundle"].manifest_sha256, scn["bundle"].instruments,
        scn["bundle"].sessions, scn["bundle"].bars + (extra,), scn["bundle"].macro_items,
        scn["bundle"].rules))
    late = rf.run_chain(dict(extended, decisions=[extra.available_at_nano]), bound)
    record = late["trace"][0]
    assert record["action"] == "WAIT" and record["count"] == 1, record["reasons"]


def test_insufficient_daily_calendar_declines_setup():
    """Trimming the calendar beyond the establishment day leaves the 5-bar
    DAILY setup without its nth trading-day close: establishment declines
    with SETUP_CALENDAR_INSUFFICIENT and zero credit."""
    # The 5-bar DAILY setup is established on day 60 and needs four more
    # trading-day closes; a calendar ending on day 63 supplies only three.
    short = rf.exchange_calendar(64)
    scn = rf.scenario("REVERSION", "DAILY", calendar=short)
    result = rf.run_chain(scn, scn["base_models"])
    for record in result["trace"]:
        assert record["action"] == "WAIT" and record["count"] == 0, record["reasons"]
    assert "SETUP_CALENDAR_INSUFFICIENT" in result["trace"][0]["reasons"]


def test_range_family_rejects_absent_and_directional_backgrounds():
    """K02 option A boundary: without a published daily background the cross
    stays UNKNOWN; a clear UP direction conflicts with the structural range
    (CONFLICT). Both reject the range families' admission."""
    _, neutral = rf.natural_open("REVERSION", "DAILY")
    assert neutral["open"] is not None

    directional = rf.natural_open("REVERSION", "DAILY", flavor="UP")
    for record in directional[1]["trace"]:
        assert record["action"] != "OPEN"
    assert any("CROSS_TIMEFRAME_NOT_VERIFIED" in record["reasons"]
               for record in directional[1]["trace"])

    scn = rf.scenario("REVERSION", "DAILY")
    from dataclasses import replace as _replace
    no_background_models = _replace(
        scn["base_models"],
        models={key: model for key, model in scn["base_models"].models.items()
                if "TRADING_DAY" not in key or "20" not in key.split("|")})
    absent = rf.run_chain(scn, no_background_models)
    assert all(record["action"] == "WAIT" for record in absent["trace"])
    assert any("CROSS_TIMEFRAME_NOT_VERIFIED" in record["reasons"]
               for record in absent["trace"])


def test_infeasible_budget_never_proposes_risk():
    """A tiny account cannot fund even the smallest complete legal layer
    plan: the family waits with BUDGET_INFEASIBLE and the chain never sees
    an OPEN proposal (money caps unchanged)."""
    scn = rf.scenario("MARTINGALE", "MINUTE")
    bound = rf.bind_evidence(
        scn["base_models"],
        rf.run_chain(scn, scn["base_models"],
                     collect_events=rf.FAMILY_EVENTS["MARTINGALE"])["probes"],
        "MARTINGALE")

    import test_v2_m4_ledger as ledger
    original_account = ledger.account
    ledger.account = lambda equity="1000000", lots=(): original_account("1000")
    try:
        poor = rf.run_chain(scn, bound)
    finally:
        ledger.account = original_account
    for record in poor["trace"]:
        assert record["action"] != "OPEN"
    assert any("BUDGET_INFEASIBLE" in record["reasons"]
               for record in poor["trace"])
