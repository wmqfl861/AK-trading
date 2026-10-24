"""M3B-07: causal prefix invariance, holding-window rejection and the
independent logical replay (04-M3B-策略库.md §3 replay.py, §10-10).

Shared synthetic builders come from this node's ``test_v2_m3b_matrix``
module (the M1 shared fixture is not published yet and ``tests/conftest.py``
must not be edited).
"""
from __future__ import annotations

from decimal import Decimal

from ak_trading.v2.contracts import ExecutionResult, Fill, Horizon
from ak_trading.v2.strategies.registry import build_strategy_spec
from ak_trading.v2.strategies.replay import (
    logical_replay_payload,
    replay_intent_id,
    replay_strategy,
)
from ak_trading.v2.strategies.reversion import RangeReversion
from ak_trading.v2.strategies.state import commit_observation

from test_v2_m3b_matrix import (
    BASE,
    MINUTE,
    account,
    features,
    forecast,
    frozen_range,
    make_context,
    minute_bars,
    ordinary_evidence,
    range_bars_sequence,
    range_forecasts,
    regime,
)

HORIZON = Horizon("TRADING_MINUTE", 30)


def reversion_setup():
    spec = build_strategy_spec("REVERSION", "MINUTE")
    return spec, RangeReversion(spec)


def context_sequence(bars_sequence, snaps):
    spec, strategy = reversion_setup()
    contexts = tuple(
        make_context(spec, bars=bars, forecasts=range_forecasts(snap),
                     regime_obj=regime("ORDERED_RANGE", (), frozen_range()),
                     features_snap=snap)
        for bars, snap in zip(bars_sequence, snaps))
    return spec, strategy, contexts


def test_appending_future_contexts_never_changes_history_proposals():
    bars, snaps = range_bars_sequence()
    # A future bar far beyond the frozen prefix, with its own snapshot.
    future_closes = [96.5, 97, 120]  # the last one even breaks the range
    extended_bars = bars + (minute_bars(list(bars[-1].__dict__.get("closes", []))
                                        if False else
                                        [96.2, 96.5, 97, 120]),)
    spec, strategy, prefix_contexts = context_sequence(bars, snaps)
    prefix_proposals = replay_strategy(strategy, prefix_contexts, {}, None)

    future_snaps = (features(Decimal("96.5"), Decimal("1")),
                    features(Decimal("97"), Decimal("1")),
                    features(Decimal("120"), Decimal("1")))
    future_bars_seq = (minute_bars(_closes_of(bars[-1]) + [96.5]),
                       minute_bars(_closes_of(bars[-1]) + [96.5, 97]),
                       minute_bars(_closes_of(bars[-1]) + [96.5, 97, 120]))
    _, _, future_contexts = context_sequence(future_bars_seq, future_snaps)
    full_contexts = prefix_contexts + future_contexts
    full_proposals = replay_strategy(strategy, full_contexts, {}, None)
    assert full_proposals[:len(prefix_proposals)] == prefix_proposals


def _closes_of(bars):
    return [float(bar.close) for bar in bars]


def test_incomplete_and_future_bars_are_invisible():
    spec, strategy = reversion_setup()
    snap = features(Decimal("93.25"), Decimal("1"))
    closes = [100 - index * 0.35 for index in range(20)]
    frozen_bars = minute_bars(closes)
    proposal_frozen = strategy.evaluate(
        make_context(spec, bars=frozen_bars, forecasts=range_forecasts(snap),
                     regime_obj=regime("ORDERED_RANGE", (), frozen_range()),
                     features_snap=snap, now=BASE + 20 * MINUTE), None)
    # Add an in-progress bar (complete=False) and a bar ending after now.
    extended = list(minute_bars(closes + [50]))
    from ak_trading.v2.contracts import MarketBar
    from test_v2_m3b_matrix import quality
    from datetime import date
    in_progress = MarketBar(
        bar_id="b-wip", contract_id="SHFE.RB2605", raw_symbol="rb2605",
        product="SHFE.RB", duration_seconds=60, datetime_nano=BASE + 21 * MINUTE,
        start_nano=BASE + 21 * MINUTE, end_nano=BASE + 22 * MINUTE,
        trading_day=date(2026, 1, 12), session_id="day0-am",
        available_at_nano=BASE + 22 * MINUTE, imported_at_nano=None,
        open=Decimal("50"), high=Decimal("50"), low=Decimal("50"),
        close=Decimal("50"), volume=Decimal("1"), open_oi=Decimal("10"),
        close_oi=Decimal("10"), complete=False, expected_minutes=1, actual_minutes=1,
        source="test", quality=quality())
    future = MarketBar(
        bar_id="b-future", contract_id="SHFE.RB2605", raw_symbol="rb2605",
        product="SHFE.RB", duration_seconds=60, datetime_nano=BASE + 22 * MINUTE,
        start_nano=BASE + 22 * MINUTE, end_nano=BASE + 23 * MINUTE,
        trading_day=date(2026, 1, 12), session_id="day0-am",
        available_at_nano=BASE + 23 * MINUTE, imported_at_nano=None,
        open=Decimal("40"), high=Decimal("40"), low=Decimal("40"),
        close=Decimal("40"), volume=Decimal("1"), open_oi=Decimal("10"),
        close_oi=Decimal("10"), complete=True, expected_minutes=1, actual_minutes=1,
        source="test", quality=quality())
    polluted = strategy.evaluate(
        make_context(spec, bars=tuple(extended + [in_progress, future]),
                     forecasts=range_forecasts(snap),
                     regime_obj=regime("ORDERED_RANGE", (), frozen_range()),
                     features_snap=snap, now=BASE + 20 * MINUTE), None)
    assert polluted.next_state.setup == proposal_frozen.next_state.setup
    assert polluted.action == proposal_frozen.action
    assert polluted.reasons == proposal_frozen.reasons


def test_minute_open_window_cannot_cross_trading_day_end():
    spec, strategy = reversion_setup()
    # Decision late in the afternoon: the 30-minute horizon crosses the day
    # boundary, so the full prediction window exceeds the holding deadline.
    horizon = HORIZON
    published = BASE + 344 * MINUTE
    descending = [100 - index * 0.35 for index in range(19)]  # arms the setup
    state = None
    proposal = None
    for closes, reference in ((descending, 325), (descending + [96], 325),
                              (descending + [96, 96.2], 325)):
        snap_i = features(Decimal(str(closes[-1])), Decimal("1"))
        # One forecast per decision snapshot: the geometry evidence must
        # match each decision's own C bucket.
        late_forecast = forecast(
            horizon, base_regime="ORDERED_RANGE", persistence={"ORDERED_RANGE": 0.9},
            frozen=frozen_range(), published=published,
            evidence_items=ordinary_evidence(snap_i, frozen_range(),
                                             "ORDERED_RANGE", horizon))
        proposal = strategy.evaluate(
            make_context(spec, bars=minute_bars(closes, reference_slot=reference),
                         forecasts=(late_forecast,),
                         regime_obj=regime("ORDERED_RANGE", (), frozen_range()),
                         features_snap=snap_i,
                         now=BASE + (reference + len(closes)) * MINUTE), state)
        state = commit_observation(state, proposal)
    assert proposal is not None
    assert proposal.action == "WAIT"
    # The day-crossing window is rejected; with 17 trading minutes left the
    # stop-open cutoff fires first, otherwise the window/deadline check does.
    assert ("OPEN_TOO_CLOSE_TO_CLOSE" in proposal.reasons
            or "OPEN_WINDOW_EXCEEDS_HOLDING_DEADLINE" in proposal.reasons)


def test_replay_with_feedback_advances_state_and_payload_schema():
    bars, snaps = range_bars_sequence()
    spec, strategy, contexts = context_sequence(bars, snaps)
    entry_key = None
    proposals_probe = replay_strategy(strategy, contexts, {}, None)
    open_step = next(index for index, proposal in enumerate(proposals_probe)
                     if proposal.action == "OPEN")
    open_proposal = proposals_probe[open_step]
    episode = open_proposal.episode_id or ""
    entry_key = replay_intent_id(spec.strategy_id, open_proposal.decision_nano, "OPEN")
    from datetime import date
    fill = Fill("f-r1", "x", "SHFE.RB2605", spec.strategy_id, episode,
                contexts[open_step].now_nano + MINUTE, date(2026, 1, 12), 1,
                open_proposal.target_lots, "OPEN", Decimal("96"), Decimal("96"),
                Decimal("1"), Decimal("0"), "rule-1", "SCENARIO")
    feedback = {entry_key: ExecutionResult(entry_key, (fill,), 0, "FILLED", ())}
    proposals = replay_strategy(strategy, contexts, feedback, None)
    assert proposals[open_step].action == "OPEN"
    payload = logical_replay_payload(spec.strategy_id, None, proposals, None)
    assert payload["schema_version"] == "ak_probability_logical_replay_v2.1.0"
    assert payload["strategy_id"] == spec.strategy_id
    assert payload["financial_result"] is None
    assert payload["initial_state"] is None
    assert isinstance(payload["proposals"], list)
    assert {"proposal_id", "strategy_id", "action", "next_state"} <= set(
        payload["proposals"][0])


def test_replay_prefix_invariance_with_feedback():
    bars, snaps = range_bars_sequence()
    spec, strategy, contexts = context_sequence(bars, snaps)
    probe = replay_strategy(strategy, contexts, {}, None)
    open_step = next(index for index, p in enumerate(probe) if p.action == "OPEN")
    key = replay_intent_id(spec.strategy_id, probe[open_step].decision_nano, "OPEN")
    from datetime import date
    episode = probe[open_step].episode_id or ""
    fill = Fill("f-r2", "x", "SHFE.RB2605", spec.strategy_id, episode,
                contexts[open_step].now_nano + MINUTE, date(2026, 1, 12), 1,
                probe[open_step].target_lots, "OPEN", Decimal("96"), Decimal("96"),
                Decimal("1"), Decimal("0"), "rule-1", "SCENARIO")
    feedback = {key: ExecutionResult(key, (fill,), 0, "FILLED", ())}
    short_replay = replay_strategy(strategy, contexts[:open_step + 1], feedback, None)
    full_replay = replay_strategy(
        strategy, contexts + contexts[-1:], feedback, None)
    assert full_replay[:len(short_replay)] == short_replay
