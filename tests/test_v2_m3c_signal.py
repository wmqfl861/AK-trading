"""M3C P3 signal-reversal tests (08 SS5.2/5.4/10-7; P3-OQ-1..4).

SYNTHETIC counterexamples: trigger anchor with equality on the volume leg,
strict direction leg, OQ-2 slot continuity/gap vectors, D2 channel
failures, variant identity and OQ-3 arbitration attribution. All market
payloads are real M2 FeatureSnapshot originals run through the D2
resolver (evaluate_with_facts); the ExitContext channel itself is tested
at the resolver level while M1's D2 type refreeze is pending.
"""
import dataclasses
import datetime

from decimal import Decimal

import pytest

from ak_trading.v2.contracts import (
    AccountSnapshot, DataQuality, ExitContext, ExitPolicyBinding, ExitState,
    FactorValue, FeatureSnapshot, Fill, LayerSpec, PositionLot, RoundBudget,
    SessionWindow, StrategyState, StressPolicy, TradingRule, FeeLeg,
    exit_binding_content_hash,
)
from ak_trading.v2.exits.arbitration import arbitrate_exit
from ak_trading.v2.exits.features import (
    MarketChannelUnavailable, MarketKeyFacts, resolve_market_snapshot,
)
from ak_trading.v2.exits.registry import (
    _code_sha256, evaluate_exit, exit_registry_specs, exit_spec_sha256,
)
from ak_trading.v2.exits.signal_reversal import (
    VARIANT_PRICE_ONLY, VARIANT_VOLUME, SignalReversalPolicy,
)
from ak_trading.v2.exits.state import (
    commit_exit_observation, commit_selected_exit,
)
from ak_trading.v2.strategies.registry import strategy_registry_v2

NANO = 1_767_920_700_000_000_000
MINUTE = 60_000_000_000
HOUR = 60 * MINUTE
DAY1 = datetime.date(2026, 1, 8)
DAY2 = datetime.date(2026, 1, 9)
DAY3 = datetime.date(2026, 1, 12)
H64A = "a" * 64
DQ_READY = DataQuality(status="READY", reasons=(), temporal_basis="observed",
                       input_hash=H64A)
DQ_MISSING = DataQuality(status="MISSING", reasons=("WARMUP_BARS",),
                         temporal_basis="unknown", input_hash=H64A)

SPECS = exit_registry_specs("MINUTE")
SPECS_D1 = exit_registry_specs("DAILY")
SIGNAL_SPEC = next(s for s in SPECS if s.exit_policy_id.endswith(
    "exit_signal_reversal_m1_v1") and "nomvol" not in s.exit_policy_id)
NOMVOL_SPEC = next(s for s in SPECS if "nomvol" in s.exit_policy_id)
SIGNAL_D1 = next(s for s in SPECS_D1 if "nomvol" not in s.exit_policy_id
                 and s.family == "SIGNAL_REVERSAL")
ENTRY_SPEC = strategy_registry_v2("MINUTE")["trend_m1_v2"].spec


def _rule():
    return TradingRule(
        rule_id="SYN-RULE-1", contract_id="SYN.rb2609",
        effective_from_nano=NANO - 10 ** 12,
        effective_until_nano=NANO + 10 ** 15,
        available_at_nano=NANO - 10 ** 12, multiplier=Decimal("10"),
        price_tick=Decimal("0.2"), lot_step=1, min_open_lots=1,
        open_fee=FeeLeg(Decimal("1"), Decimal("0"), True),
        close_fee=FeeLeg(Decimal("1"), Decimal("0"), True),
        close_today_fee=FeeLeg(Decimal("1"), Decimal("0"), True),
        long_margin_rate=Decimal("0.1"), short_margin_rate=Decimal("0.1"),
        limit_up=None, limit_down=None, slippage_ticks=Decimal("0"),
        impact_ticks=Decimal("0"), participation_rate=Decimal("1"),
        evidence="VERIFIED", source_sha256=H64A)


def _session(start, end, day=DAY1, sid="SYN-SESS-1"):
    return SessionWindow(session_id=sid, product="SYN", trading_day=day,
                         start_nano=start, end_nano=end, kind="DAY",
                         calendar_version="cal-v1", evidence_status="VERIFIED")


def _fill():
    return Fill(fill_id="SYN-FILL-OPEN-1", intent_id="SYN-INT-OPEN-1",
                contract_id="SYN.rb2609", strategy_id="SYNTHETIC:entry-1",
                episode_id="SYN-EP-1", execution_nano=NANO,
                trading_day=DAY1, side=1, lots=3, offset="OPEN",
                reference_price=Decimal("100"), execution_price=Decimal("100"),
                fee_cny=Decimal("3"), slippage_cny=Decimal("0"),
                rule_id="SYN-RULE-1", evidence="VERIFIED")


def _budget():
    return RoundBudget(
        budget_id="SYN-BUDGET-1", episode_id="SYN-EP-1", contract_id="SYN.rb2609",
        frozen_at_nano=NANO, range_id=None, frozen_range=None,
        stress_policy=StressPolicy(
            policy_id="SYNTHETIC:stress-1", gap_atr_multiple=Decimal("1"),
            locked_trading_days=3, adverse_atr_per_day=Decimal("1"),
            release_extra_atr=Decimal("1"), fee_multiplier=Decimal("1"),
            slippage_multiplier=Decimal("1"), margin_multiplier=Decimal("1")),
        initial_atr_points=Decimal("2"), side=1,
        layers=(LayerSpec(index=0, trigger_price=Decimal("100"),
                          reference_entry_price=Decimal("100"), lots=3),),
        stop_price=Decimal("98"), target_price=Decimal("106"),
        latest_exit_nano=NANO + 60 * MINUTE, maximum_lots=3,
        planned_loss_cny=Decimal("100"), stress_loss_cny=Decimal("120"),
        reserved_margin_cny=Decimal("1000"), day_loss_limit_cny=Decimal("200"),
        equity_at_start_cny=Decimal("100000"))


def _binding(specs, priority=None):
    values = dict(strategy_id="SYNTHETIC:entry-1", entry_spec_sha256=H64A,
                  exit_spec_sha256s=tuple(sorted({exit_spec_sha256(s)
                                                  for s in specs})),
                  sizing_spec_sha256=None,
                  priority=priority or tuple(s.exit_policy_id for s in specs),
                  legacy_rules_version="m3b-legacy-rules-v2.1.2")
    return ExitPolicyBinding(binding_sha256=exit_binding_content_hash(**values),
                             **values)


def _state(binding, **overrides):
    values = dict(
        account_id="SYN-ACCOUNT-1", contract_id="SYN.rb2609",
        strategy_id="SYNTHETIC:entry-1", episode_id="SYN-EP-1",
        first_fill_nano=NANO, exit_binding_sha256=binding.binding_sha256,
        realized_gross_cny=Decimal("0"), fees_paid_cny=Decimal("3"),
        price_peak=None, price_peak_source_nano=None, price_peak_source_hash=None,
        net_peak_r=None, net_peak_source_nano=None, net_peak_source_hash=None,
        armed=False, protection_stop_price=None, protection_line_version=None,
        protection_effective_from_nano=None, confirmations=(),
        no_more_add=False, qbase=3, closed_lots=0, ladder_progress=(),
        phase="MANAGING", last_committed_event_id=None, state_prefix_hash=H64A)
    values.update(overrides)
    return ExitState(**values)


def _context(now_offset, *, previous=None, specs=None, binding=None,
             sessions=None, side=1, lots=3, mark_price=None):
    specs = specs or (SIGNAL_SPEC,)
    budget = _budget()
    if side == -1:
        budget = dataclasses.replace(budget, side=-1,
                                     stop_price=Decimal("102"),
                                     target_price=Decimal("94"))
    now = NANO + int(now_offset)
    sessions = sessions if sessions is not None else [
        _session(NANO - MINUTE, NANO + 60 * MINUTE)]
    lot = PositionLot(lot_id="SYN-LOT-1", contract_id="SYN.rb2609",
                      strategy_id="SYNTHETIC:entry-1", episode_id="SYN-EP-1",
                      side=side, lots=lots, opened_at_nano=NANO,
                      opened_trading_day=DAY1, entry_price=Decimal("100"),
                      opening_fee_cny=Decimal(lots))
    account = AccountSnapshot(
        account_id="SYN-ACCOUNT-1", as_of_nano=now, trading_day=DAY1,
        initial_cash_cny=Decimal("100000"), cash_cny=Decimal("100000"),
        realized_gross_cny=Decimal("0"), fees_cny=Decimal("0"),
        slippage_cny=Decimal("0"), unrealized_cny=Decimal("0"),
        equity_cny=Decimal("100000"), margin_cny=Decimal("0"),
        reserved_margin_cny=Decimal("0"),
        day_start_equity_cny=Decimal("100000"), day_net_cny=Decimal("0"),
        peak_equity_cny=Decimal("100000"), lots=(lot,), killed=False,
        quality=DQ_READY)
    entry_state = StrategyState(
        strategy_id="SYNTHETIC:entry-1", episode_id="SYN-EP-1",
        contract_id="SYN.rb2609", phase="HOLDING", setup=None,
        last_regime=None, direction=1, filled_layers=(0,),
        layer_filled_lots=(3,), pending_layer_index=None,
        active_exit_nano=NANO + 60 * MINUTE, pending_exit_nano=None,
        pending_intent_ids=(), budget=budget, confirmation_count=0,
        cooldown_until_nano=None, last_event_id="SYN-FILL-OPEN-1")
    from ak_trading.v2.contracts import ValuationMark
    mark = None
    if mark_price is not None:
        mark = (ValuationMark(contract_id="SYN.rb2609",
                              price=Decimal(mark_price), as_of_nano=now,
                              valued_at_nano=now, source_bar_id="SYN-BAR-1",
                              source_bar_sha256=H64A, rule_id="SYN-RULE-1",
                              status="CURRENT", reasons=()),)
    return ExitContext(
        now_nano=now, account_id="SYN-ACCOUNT-1", contract_id="SYN.rb2609",
        strategy_id="SYNTHETIC:entry-1", episode_id="SYN-EP-1",
        exit_binding=binding or _binding(specs), exit_specs=tuple(specs),
        market_features=(), forecasts=(), entry_spec=ENTRY_SPEC,
        entry_state=entry_state, budget=budget, account=account,
        lots=(lot,), marks=mark if mark is not None else (),
        episode_fills=(_fill(),), rules=(_rule(),),
        sessions=tuple(sessions),
        previous_exit_state=previous, continuation=None)


def _mkt(close, ma20, vr, decision_nano, *, duration=60, contract=None,
         vr_quality=None, ma20_quality=None, snapshot_id=None):
    d = f":d{duration}"
    values = [
        FactorValue(factor_key=f"BASE:close:v2.1.0{d}", value=Decimal(close),
                    available_at_nano=decision_nano, quality=DQ_READY),
        FactorValue(factor_key=f"BASE:ma20:v2.1.0{d}",
                    value=Decimal(ma20) if ma20 is not None else None,
                    available_at_nano=decision_nano,
                    quality=ma20_quality or DQ_READY),
        FactorValue(factor_key=f"BASE:volume_ratio20:v2.1.0{d}",
                    value=None if vr is None else Decimal(str(vr)),
                    available_at_nano=decision_nano,
                    quality=vr_quality or DQ_READY),
    ]
    return FeatureSnapshot(
        snapshot_id=snapshot_id or f"SYN-MKT-{decision_nano}",
        contract_id=contract or "SYN.rb2609", raw_symbol="rb2609",
        product="SYN", trading_day=DAY1, session_id="SYN-SESS-1",
        session_kind="DAY", decision_nano=decision_nano,
        available_at_nano=decision_nano, duration_seconds=duration,
        source_bar_ids=("SYN-BAR-1",), reference_price=None,
        atr_points=Decimal("5"), values=tuple(values),
        daily_background_id=None, quality=DQ_READY)


def _facts(close, ma20, vr, decision_nano, **kwargs):
    spec = kwargs.pop("spec", SIGNAL_SPEC)
    duration = spec.observation_duration_seconds
    snapshot = _mkt(close, ma20, vr, decision_nano, duration=duration,
                    **kwargs)
    keys = {key.split(":")[1] for key in spec.required_market_keys}
    wanted = tuple(sorted(f"BASE:{name}:v2.1.0:d{duration}"
                          for name in keys))
    return resolve_market_snapshot((snapshot,), "SYN.rb2609", duration,
                                   wanted, decision_nano)


def _policy(spec=SIGNAL_SPEC, variant=None):
    if variant is None:
        variant = VARIANT_VOLUME if spec is not NOMVOL_SPEC else VARIANT_PRICE_ONLY
    return SignalReversalPolicy(spec, variant)


def _confirm(state, policy_id):
    for item in state.confirmations:
        if item.exit_policy_id == policy_id and item.rule_id == "trigger":
            return item
    return None


S1 = 10 * MINUTE          # slot A end
S2 = 11 * MINUTE          # adjacent slot B end
S3 = 12 * MINUTE          # adjacent slot C end
S4 = 13 * MINUTE          # adjacent slot D end
QUAL = ("98", "100", "1.5")   # close<ma20 (long reversal), vr exactly 1.5


def test_p3_trigger_anchor_equality_and_cap() -> None:
    # 08 SS10-7: two distinct completed bars each with C-MA20<0 and
    # volume_ratio20 == 1.5 (EQUALITY counts) -> EXIT/0, strictly next
    # window; a third evaluation of the same slots never re-triggers and
    # the counter caps at 2.
    binding = _binding((SIGNAL_SPEC,))
    base = _state(binding)
    policy = _policy()
    first = policy.evaluate_with_facts(
        _context(S1 + 1, previous=base), base, _facts(*QUAL, NANO + S1))
    assert first.action == "KEEP"
    assert "SIGNAL_REVERSAL_CONFIRMING" in first.reasons
    committed = commit_exit_observation(base, first)
    assert _confirm(committed, SIGNAL_SPEC.exit_policy_id).count == 1
    second = policy.evaluate_with_facts(
        _context(S2 + 1, previous=committed), committed,
        _facts(*QUAL, NANO + S2))
    assert second.action == "EXIT" and second.target_lots == 0
    assert second.earliest_execution_nano > second.observed_at_nano
    assert "SIGNAL_REVERSAL_CONFIRMED" in second.reasons
    confirmed = commit_exit_observation(committed, second)
    assert _confirm(confirmed, SIGNAL_SPEC.exit_policy_id).count == 2
    # Same-slot re-evaluation: idempotent, no recount, no chain advance.
    replay = policy.evaluate_with_facts(
        _context(S2 + 5, previous=confirmed), confirmed,
        _facts(*QUAL, NANO + S2))
    assert replay.action == "EXIT"
    assert _confirm(replay.next_state, SIGNAL_SPEC.exit_policy_id).count == 2
    assert commit_exit_observation(confirmed, replay) is confirmed


def test_p3_slot_reset_and_recovery() -> None:
    # Interruption resets: a bar with vr=1.4 (volume leg fails) resets to
    # 0; a bar with C == MA20 (direction leg exactly zero is NOT a
    # reversal) resets to 0; two later qualifying bars build 1 -> 2 EXIT.
    binding = _binding((SIGNAL_SPEC,))
    policy = _policy()
    base = _state(binding)
    first = policy.evaluate_with_facts(
        _context(S1 + 1, previous=base), base, _facts(*QUAL, NANO + S1))
    count_one = commit_exit_observation(base, first)
    assert _confirm(count_one, SIGNAL_SPEC.exit_policy_id).count == 1
    weak = policy.evaluate_with_facts(
        _context(S2 + 1, previous=count_one), count_one,
        _facts("98", "100", "1.4", NANO + S2))
    assert weak.action == "KEEP"
    assert "SIGNAL_REVERSAL_RESET" in weak.reasons
    assert _confirm(weak.next_state, SIGNAL_SPEC.exit_policy_id).count == 0
    flat = policy.evaluate_with_facts(
        _context(S3 + 1, previous=count_one), count_one,
        _facts("100", "100", "2.0", NANO + S3))
    assert _confirm(flat.next_state, SIGNAL_SPEC.exit_policy_id).count == 0
    restarted = commit_exit_observation(count_one, flat)
    third = policy.evaluate_with_facts(
        _context(S4 + 1, previous=restarted), restarted,
        _facts(*QUAL, NANO + S4))
    assert _confirm(third.next_state, SIGNAL_SPEC.exit_policy_id).count == 1
    third_committed = commit_exit_observation(restarted, third)
    again = policy.evaluate_with_facts(
        _context(S4 + MINUTE + 1, previous=third_committed), third_committed,
        _facts(*QUAL, NANO + S4 + MINUTE))
    # Adjacent to S4: 1 -> 2.
    assert again.action == "EXIT"


def test_p3_boundary_semantics_side_by_side() -> None:
    # Volume leg: 1.4999 resets, exactly 1.5 counts. Direction leg:
    # +0.01 (with trend) resets, exactly 0 does NOT count (strict <),
    # -0.01 counts. The two equality semantics must not be swapped.
    binding = _binding((SIGNAL_SPEC,))
    policy = _policy()
    base = _state(binding)
    cases = [
        ("97.5", "100", "1.4999", 0),      # volume leg just under
        ("97.5", "100", "1.5", 1),         # volume equality counts
        ("100.01", "100", "2.0", 0),       # direction with-trend
        ("100", "100", "2.0", 0),          # direction exactly zero: strict
        ("99.99", "100", "2.0", 1),        # strict reversal counts
    ]
    for close, ma, vr, expected in cases:
        proposal = policy.evaluate_with_facts(
            _context(S1 + 1, previous=base), base,
            _facts(close, ma, vr, NANO + S1))
        count = _confirm(proposal.next_state,
                         SIGNAL_SPEC.exit_policy_id)
        assert (count.count if count else 0) == expected, (close, ma, vr)


def test_p3_market_channel_failures() -> None:
    # Required field invalid (warmup/None/zero) -> real slot recorded,
    # count reset; no snapshot at all with an expected slot due -> reset
    # with the OLD id kept; channel pending -> dedicated reason; none of
    # this ever fabricates an observation id for a missing bar.
    binding = _binding((SIGNAL_SPEC,))
    policy = _policy()
    base = _state(binding)
    first = policy.evaluate_with_facts(
        _context(S1 + 1, previous=base), base, _facts(*QUAL, NANO + S1))
    counted = commit_exit_observation(base, first)
    old_id = _confirm(counted, SIGNAL_SPEC.exit_policy_id).last_observation_id
    # MA20 warmup: the field is present but not READY.
    warm = policy.evaluate_with_facts(
        _context(S2 + 1, previous=counted), counted,
        _facts("98", None, "1.5", NANO + S2, ma20_quality=DQ_MISSING))
    assert warm.action == "KEEP"
    reset = _confirm(warm.next_state, SIGNAL_SPEC.exit_policy_id)
    assert reset.count == 0
    assert reset.last_observation_id != old_id      # real slot recorded
    # V=0 -> volume_ratio20 value 0 rejected as non-positive by the
    # resolver: a zero-volume bar cannot prove a volume reversal.
    zero = policy.evaluate_with_facts(
        _context(S3 + 1, previous=counted), counted,
        _facts("98", "100", "0", NANO + S3))
    assert zero.action == "KEEP"
    assert _confirm(zero.next_state, SIGNAL_SPEC.exit_policy_id).count == 0
    # No snapshot, adjacent minute due -> interruption reset, old id kept.
    missing = policy.evaluate_with_facts(
        _context(S2 + 1, previous=counted), counted, None,
        ("MARKET_SNAPSHOT_MISSING",))
    kept = _confirm(missing.next_state, SIGNAL_SPEC.exit_policy_id)
    assert kept.count == 0
    assert kept.last_observation_id == old_id
    # Channel pending (resolver level, pre-M1-D2 world).
    pending = policy.evaluate_with_facts(
        _context(S2 + 1, previous=base), base, None,
        ("MARKET_CHANNEL_PENDING_M1_D2_TYPE",))
    assert pending.action == "KEEP"
    assert "SIGNAL_MARKET_CHANNEL_PENDING" in pending.reasons


def test_p3_gap_vectors_and_closures() -> None:
    # OQ-2 vectors: A(1) -> B never evaluated (data gap) -> C qualifying
    # restarts at 1 (not 2); D adjacent -> 2/EXIT. A(1) -> A repeat -> B
    # adjacent qualifying -> 2. Lunch bridges adjacency. Unknown calendar
    # caps at 1. Daily gap resets.
    binding = _binding((SIGNAL_SPEC,))
    policy = _policy()
    base = _state(binding)
    a = commit_exit_observation(base, policy.evaluate_with_facts(
        _context(S1 + 1, previous=base), base, _facts(*QUAL, NANO + S1)))
    assert _confirm(a, SIGNAL_SPEC.exit_policy_id).count == 1
    # B (slot S2) never arrived; C arrives at S3: the calendar-covered
    # boundary S2 lies in (S1, S3) -> gap -> restart at 1.
    c = policy.evaluate_with_facts(
        _context(S3 + 1, previous=a), a, _facts(*QUAL, NANO + S3))
    assert "SIGNAL_GAP_RESET" in c.reasons
    assert _confirm(c.next_state, SIGNAL_SPEC.exit_policy_id).count == 1
    c_committed = commit_exit_observation(a, c)
    d = policy.evaluate_with_facts(
        _context(S4 + 1, previous=c_committed), c_committed,
        _facts(*QUAL, NANO + S4))
    assert d.action == "EXIT"
    # A -> A repeat (unchanged) -> adjacent B qualifying -> 2.
    repeat = policy.evaluate_with_facts(
        _context(S1 + 30, previous=a), a, _facts(*QUAL, NANO + S1))
    assert commit_exit_observation(a, repeat) is a
    b = policy.evaluate_with_facts(
        _context(S2 + 1, previous=a), a, _facts(*QUAL, NANO + S2))
    assert b.action == "EXIT"
    # Lunch bridge: sessions [0,10m] and [100m,110m]; a count=1 sequence
    # seeded at the last AM slot (10m) CONTINUES to 2 at the first PM
    # slot (101m) — no expected minute boundary lies between them, so the
    # closure is adjacency, not a gap (a fresh-state count of 1 would
    # prove nothing about continuation).
    lunch_sessions = (_session(NANO, NANO + 10 * MINUTE, sid="AM"),
                      _session(NANO + 100 * MINUTE, NANO + 110 * MINUTE,
                               sid="PM"))
    am = policy.evaluate_with_facts(
        _context(10 * MINUTE + 1, previous=_state(binding),
                 sessions=lunch_sessions), _state(binding),
        _facts(*QUAL, NANO + 10 * MINUTE))
    assert _confirm(am.next_state, SIGNAL_SPEC.exit_policy_id).count == 1
    am_committed = commit_exit_observation(_state(binding), am)
    bridge = policy.evaluate_with_facts(
        _context(101 * MINUTE + 1, previous=am_committed,
                 sessions=lunch_sessions), am_committed,
        _facts(*QUAL, NANO + 101 * MINUTE))
    assert bridge.action == "EXIT"
    assert _confirm(bridge.next_state, SIGNAL_SPEC.exit_policy_id).count == 2
    # Unknown calendar: cannot prove adjacency -> at most 1.
    unknown = policy.evaluate_with_facts(
        _context(S2 + 1, previous=a, sessions=()), a,
        _facts(*QUAL, NANO + S2))
    assert "SIGNAL_CALENDAR_UNKNOWN" in unknown.reasons
    assert _confirm(unknown.next_state, SIGNAL_SPEC.exit_policy_id).count == 1
    # Daily gap: day1 -> day3 with day2 covered in between -> reset -> 1.
    daily_sessions = (_session(NANO, NANO + 6 * HOUR, day=DAY1),
                      _session(NANO + 24 * HOUR, NANO + 30 * HOUR, day=DAY2,
                               sid="D2"),
                      _session(NANO + 48 * HOUR, NANO + 54 * HOUR, day=DAY3,
                               sid="D3"))
    daily_policy = SignalReversalPolicy(SIGNAL_D1, VARIANT_VOLUME)
    daily_base = _state(_binding((SIGNAL_D1,)))
    day1 = daily_policy.evaluate_with_facts(
        _context(7 * HOUR, previous=daily_base, specs=(SIGNAL_D1,),
                 sessions=daily_sessions), daily_base,
        _facts(*QUAL, NANO + 6 * HOUR, spec=SIGNAL_D1))
    day1_committed = commit_exit_observation(daily_base, day1)
    assert _confirm(day1_committed, SIGNAL_D1.exit_policy_id).count == 1
    day3 = daily_policy.evaluate_with_facts(
        _context(55 * HOUR, previous=day1_committed, specs=(SIGNAL_D1,),
                 sessions=daily_sessions), day1_committed,
        _facts(*QUAL, NANO + 54 * HOUR, spec=SIGNAL_D1))
    assert _confirm(day3.next_state, SIGNAL_D1.exit_policy_id).count == 1
    assert "SIGNAL_GAP_RESET" in day3.reasons


def test_p3_mode_isolation_and_no_forecast_dependency() -> None:
    # A d86400 snapshot cannot feed the d60 spec; the policy consumes no
    # forecast/probability keys at all (0.70/0.85 can never gate an exit;
    # the legacy 0.65/0.80 channels run in M4 without waiting for bar 2).
    binding = _binding((SIGNAL_SPEC,))
    policy = _policy()
    base = _state(binding)
    daily_snapshot = _mkt(*QUAL, NANO + S1, duration=86400)
    with pytest.raises(MarketChannelUnavailable):
        resolve_market_snapshot(
            (daily_snapshot,), "SYN.rb2609", 60,
            tuple(sorted(SIGNAL_SPEC.required_market_keys)), NANO + S1)
    result = policy.evaluate(_context(S1 + 1, previous=base), base)
    assert result.action == "KEEP"
    assert "SIGNAL_MARKET_CHANNEL_PENDING" in result.reasons or \
        "MARKET_SNAPSHOT_MISSING" in result.reasons
    # No forecast key is required or consumed.
    assert not any("forecast" in key or "probab" in key
                   for key in SIGNAL_SPEC.required_market_keys)
    # A count=1 candidate is KEEP — it never delays other channels and
    # never claims EXIT before the second qualifying bar.
    first = policy.evaluate_with_facts(
        _context(S1 + 1, previous=base), base, _facts(*QUAL, NANO + S1))
    assert first.action == "KEEP" and first.target_lots == 3


def test_p3_daily_weekend_continuation() -> None:
    # OQ-2 daily branch of "合法午休/周末两侧相邻槽可继续": DAY1 and DAY3
    # have sessions, DAY2 (the weekend) has NONE — the calendar proves the
    # closure, so a count=1 sequence seeded on DAY1 CONTINUES to 2/EXIT on
    # DAY3. A degenerate natural-day counter (48h apart -> "gap") fails
    # this; only the calendar-driven adjacency check passes.
    weekend_sessions = (
        _session(NANO, NANO + 6 * HOUR, day=DAY1),
        _session(NANO + 48 * HOUR, NANO + 54 * HOUR, day=DAY3, sid="D3"),
    )
    daily_policy = SignalReversalPolicy(SIGNAL_D1, VARIANT_VOLUME)
    base = _state(_binding((SIGNAL_D1,)))
    day1 = daily_policy.evaluate_with_facts(
        _context(7 * HOUR, previous=base, specs=(SIGNAL_D1,),
                 sessions=weekend_sessions), base,
        _facts(*QUAL, NANO + 6 * HOUR, spec=SIGNAL_D1))
    assert day1.action == "KEEP"
    seeded = commit_exit_observation(base, day1)
    assert _confirm(seeded, SIGNAL_D1.exit_policy_id).count == 1
    day3 = daily_policy.evaluate_with_facts(
        _context(55 * HOUR, previous=seeded, specs=(SIGNAL_D1,),
                 sessions=weekend_sessions), seeded,
        _facts(*QUAL, NANO + 54 * HOUR, spec=SIGNAL_D1))
    assert "SIGNAL_GAP_RESET" not in day3.reasons
    assert day3.action == "EXIT" and day3.target_lots == 0
    assert _confirm(day3.next_state, SIGNAL_D1.exit_policy_id).count == 2


def test_p3_variant_identity_and_digests() -> None:
    # Two direction-qualifying bars with vr=0.9: the no-volume ablation
    # confirms (EXIT) while the volume-confirmed version resets (KEEP) —
    # same inputs, different registered ids, different outcomes.
    vol_binding = _binding((SIGNAL_SPEC,))
    nomvol_binding = _binding((NOMVOL_SPEC,))
    vol_policy = SignalReversalPolicy(SIGNAL_SPEC, VARIANT_VOLUME)
    nomvol_policy = SignalReversalPolicy(NOMVOL_SPEC, VARIANT_PRICE_ONLY)
    base_vol = _state(vol_binding)
    base_nom = _state(nomvol_binding)
    weak = ("98", "100", "0.9")
    vol1 = vol_policy.evaluate_with_facts(
        _context(S1 + 1, previous=base_vol), base_vol,
        _facts(*weak, NANO + S1))
    vol2 = vol_policy.evaluate_with_facts(
        _context(S2 + 1, previous=commit_exit_observation(base_vol, vol1)),
        commit_exit_observation(base_vol, vol1), _facts(*weak, NANO + S2))
    assert vol2.action == "KEEP"
    assert _confirm(vol2.next_state, SIGNAL_SPEC.exit_policy_id).count == 0
    nom1 = nomvol_policy.evaluate_with_facts(
        _context(S1 + 1, previous=base_nom, specs=(NOMVOL_SPEC,),
                 binding=nomvol_binding), base_nom,
        _facts(*weak, NANO + S1, spec=NOMVOL_SPEC))
    nom1_committed = commit_exit_observation(base_nom, nom1)
    nom2 = nomvol_policy.evaluate_with_facts(
        _context(S2 + 1, previous=nom1_committed, specs=(NOMVOL_SPEC,),
                 binding=nomvol_binding), nom1_committed,
        _facts(*weak, NANO + S2, spec=NOMVOL_SPEC))
    assert nom2.action == "EXIT" and nom2.target_lots == 0
    # A missing volume field never blocks the ablation (it is not its
    # evidence), while the volume version resets on the same snapshot.
    broken = _mkt("98", "100", None, NANO + S1, vr_quality=DQ_MISSING)
    ablation = nomvol_policy.evaluate_with_facts(
        _context(S1 + 1, previous=base_nom, specs=(NOMVOL_SPEC,),
                 binding=nomvol_binding), base_nom,
        resolve_market_snapshot(
            (broken,), "SYN.rb2609", 60,
            tuple(sorted(NOMVOL_SPEC.required_market_keys)), NANO + S1))
    assert _confirm(ablation.next_state, NOMVOL_SPEC.exit_policy_id).count == 1
    # Extra boolean keys are whitelist-rejected; a runtime variant key can
    # never appear.
    with pytest.raises(ValueError, match="exit_parameters_keys"):
        dataclasses.replace(SIGNAL_SPEC, parameters={"confirmation_bars": 2,
                                                    "use_volume": True})
    # Variant digests differ, are stable, and bind the real entrypoint;
    # an entrypoint/variant mismatch is refused.
    vol_digest = _code_sha256("SIGNAL_REVERSAL", VARIANT_VOLUME)
    nomvol_digest = _code_sha256("SIGNAL_REVERSAL", VARIANT_PRICE_ONLY)
    assert vol_digest != nomvol_digest
    assert _code_sha256("SIGNAL_REVERSAL", VARIANT_VOLUME) == vol_digest
    with pytest.raises(ValueError, match="entrypoint_variant_mismatch"):
        vol_policy.evaluate_price_only(_context(S1 + 1), None)
    with pytest.raises(ValueError, match="spec_variant_key_mismatch"):
        SignalReversalPolicy(SIGNAL_SPEC, VARIANT_PRICE_ONLY)
    # A binding whose spec set disagrees with the registry is rejected.
    drifted = dataclasses.replace(
        SIGNAL_SPEC, required_market_keys=tuple(
            sorted([k for k in SIGNAL_SPEC.required_market_keys
                    if "volume_ratio20" not in k])))
    with pytest.raises(ValueError):
        evaluate_exit(_context(S1 + 1, specs=(drifted,)), None)


def test_p3_exit_never_reverses_and_keeps_irreversible() -> None:
    # EXIT is always target 0 (no sign flip, no re-open); a selected
    # LIQUIDATING is never pulled back by a REDUCE; signal's own KEEP can
    # never withdraw an owed exit.
    binding = _binding((SIGNAL_SPEC,))
    policy = _policy()
    base = _state(binding)
    first = commit_exit_observation(base, policy.evaluate_with_facts(
        _context(S1 + 1, previous=base), base, _facts(*QUAL, NANO + S1)))
    second = policy.evaluate_with_facts(
        _context(S2 + 1, previous=first), first, _facts(*QUAL, NANO + S2))
    assert second.action == "EXIT" and second.target_lots == 0
    from ak_trading.v2.contracts import ExitExecutionLink
    link = ExitExecutionLink(
        link_id="SYN-LINK-1", exit_proposal_id=second.proposal_id,
        strategy_proposal_id="SYN-PROP-1", decision_id="SYN-DEC-1",
        intent_id="SYN-INT-1", exit_binding_sha256=binding.binding_sha256,
        action="EXIT", target_lots=0, requested_lots=3,
        execution_kind="NEXT_WINDOW_CLOSE")
    selected = commit_selected_exit(first, second, (link,))
    assert selected.phase == "LIQUIDATING" and selected.no_more_add is True
    reduce_probe = dataclasses.replace(second, action="REDUCE",
                                       target_lots=1, next_state=selected)
    from ak_trading.v2.exits.state import ExitStateError
    with pytest.raises(ExitStateError):
        commit_selected_exit(selected, reduce_probe, (link,))


def test_p3_arbitration_attribution_oq3() -> None:
    # OQ-3: signal EXIT vs drawdown EXIT with the mandated priority order
    # -> unique target 0, drawdown takes attribution, the signal's count=2
    # confirmation survives in the merged next_state; a signal KEEP never
    # beats a ladder REDUCE; a missing priority id is rejected outright.
    from ak_trading.v2.exits.drawdown import DrawdownExitPolicy
    from ak_trading.v2.exits.profit_ladder import ProfitLadderPolicy
    drawdown_spec = next(s for s in SPECS if s.family == "DRAWDOWN_EXIT")
    ladder_spec = next(s for s in SPECS if s.family == "PROFIT_LADDER")
    specs = (drawdown_spec, SIGNAL_SPEC, ladder_spec)
    priority = (drawdown_spec.exit_policy_id, SIGNAL_SPEC.exit_policy_id,
                ladder_spec.exit_policy_id)
    binding = _binding(specs, priority=priority)
    from ak_trading.v2.contracts import ExitConfirmation,         exit_observation_slot_id
    # One qualifying signal bar already committed at slot S1 (count=1):
    # the same-observation S2 bar takes it to 2 -> EXIT.
    signal_count = ExitConfirmation(
        exit_policy_id=SIGNAL_SPEC.exit_policy_id, rule_id="trigger",
        count=1,
        last_observation_id=exit_observation_slot_id(
            "SYN.rb2609", 60, NANO + S1))
    peaked = _state(binding, net_peak_r=Decimal("1.2"),
                    net_peak_source_nano=NANO + 5 * MINUTE,
                    net_peak_source_hash=H64A,
                    confirmations=(signal_count,))
    context = _context(S2 + 1, previous=peaked, specs=specs,
                        binding=binding, mark_price="102")
    signal = _policy().evaluate_with_facts(
        context, peaked, _facts(*QUAL, NANO + S2))
    assert signal.action == "EXIT"
    drawdown = DrawdownExitPolicy(drawdown_spec).evaluate(context, peaked)
    assert drawdown.action == "EXIT"
    winner = arbitrate_exit((drawdown, signal), context)
    assert winner.target_lots == 0
    assert winner.triggering_policy_id == drawdown_spec.exit_policy_id
    merged_count = _confirm(winner.next_state, SIGNAL_SPEC.exit_policy_id)
    assert merged_count is not None and merged_count.count == 2
    # Signal KEEP does not preempt a ladder REDUCE; its count survives.
    # The mark drives Nt to 1.02R so the 67% stage REALLY issues a
    # REDUCE (the 33% stage would be a zero-lot D=0 KEEP) — otherwise
    # the branch below would never run.
    ladder_base = _state(binding, qbase=3)
    calm_context = _context(S1 + 1, previous=ladder_base, specs=specs,
                            binding=binding, mark_price="103.6")
    calm_signal = _policy().evaluate_with_facts(
        calm_context, ladder_base, _facts("100", "100", "2.0", NANO + S1))
    assert calm_signal.action == "KEEP"
    ladder = ProfitLadderPolicy(ladder_spec).evaluate(calm_context,
                                                      ladder_base)
    assert ladder.action == "REDUCE" and ladder.target_lots == 1
    reduce_winner = arbitrate_exit((calm_signal, ladder), calm_context)
    # arbitrate returns a replaced copy (merged next_state): identity is
    # asserted by proposal id + attribution, not by object identity.
    assert reduce_winner.proposal_id == ladder.proposal_id
    assert reduce_winner.action == "REDUCE"
    # The signal's reset confirmation (count=0 at the real slot) still
    # survives the merge — losing never drops observation facts.
    survived = _confirm(reduce_winner.next_state, SIGNAL_SPEC.exit_policy_id)
    assert survived is not None and survived.count == 0
    # Priority must cover every triggerable policy id (D3/OQ-3: no
    # runtime family fallback).
    bad_priority = (drawdown_spec.exit_policy_id, ladder_spec.exit_policy_id)
    bad_binding = _binding(specs, priority=bad_priority)
    bad_context = _context(S2 + 1, previous=peaked, specs=specs,
                           binding=bad_binding)
    from ak_trading.v2.exits.arbitration import ArbitrationError
    with pytest.raises(ArbitrationError):
        arbitrate_exit((drawdown, signal), bad_context)


def test_p3_signal_exit_beats_higher_order_reduce() -> None:
    # OQ-3: attribution order only breaks ties AMONG EXITs. A confirmed
    # signal EXIT (count=2) beats a ladder REDUCE from a family placed
    # earlier in the binding priority — rank EXIT > REDUCE always.
    from ak_trading.v2.contracts import ExitConfirmation,         exit_observation_slot_id
    from ak_trading.v2.exits.profit_ladder import ProfitLadderPolicy
    ladder_spec = next(s for s in SPECS if s.family == "PROFIT_LADDER")
    specs = (ladder_spec, SIGNAL_SPEC)
    priority = (ladder_spec.exit_policy_id, SIGNAL_SPEC.exit_policy_id)
    binding = _binding(specs, priority=priority)
    seeded = _state(
        binding, qbase=3,
        confirmations=(ExitConfirmation(
            exit_policy_id=SIGNAL_SPEC.exit_policy_id, rule_id="trigger",
            count=1,
            last_observation_id=exit_observation_slot_id(
                "SYN.rb2609", 60, NANO + S1)),))
    context = _context(S2 + 1, previous=seeded, specs=specs,
                       binding=binding, mark_price="103.6")
    signal = _policy().evaluate_with_facts(
        context, seeded, _facts(*QUAL, NANO + S2))
    assert signal.action == "EXIT" and signal.target_lots == 0
    ladder = ProfitLadderPolicy(ladder_spec).evaluate(context, seeded)
    assert ladder.action == "REDUCE"            # higher order, but rank 1
    winner = arbitrate_exit((ladder, signal), context)
    assert winner.proposal_id == signal.proposal_id
    assert winner.action == "EXIT" and winner.target_lots == 0


def test_p3_causality_and_vocabulary() -> None:
    # A future-dated source is invisible; committed counts and prefix
    # hashes never change by appending later bars; no reason contains the
    # legacy purpose substrings (mis-routing hazard).
    binding = _binding((SIGNAL_SPEC,))
    policy = _policy()
    base = _state(binding)
    first = commit_exit_observation(base, policy.evaluate_with_facts(
        _context(S1 + 1, previous=base), base, _facts(*QUAL, NANO + S1)))
    future = _mkt(*QUAL, NANO + S1 + 10 * MINUTE)
    with pytest.raises(MarketChannelUnavailable):
        resolve_market_snapshot(
            (future,), "SYN.rb2609", 60,
            tuple(sorted(SIGNAL_SPEC.required_market_keys)), NANO + S1)
    later = policy.evaluate_with_facts(
        _context(S2 + 1, previous=first), first, _facts(*QUAL, NANO + S2))
    assert _confirm(first, SIGNAL_SPEC.exit_policy_id).count == 1
    assert first.state_prefix_hash != later.next_state.state_prefix_hash
    for proposal in (policy.evaluate_with_facts(
            _context(S1 + 1, previous=base), base, _facts(*QUAL, NANO + S1)),
            policy.evaluate_with_facts(
                _context(S2 + 1, previous=first), first,
                _facts("100", "100", "0", NANO + S2)),
            SignalReversalPolicy(NOMVOL_SPEC, VARIANT_PRICE_ONLY)
            .evaluate_with_facts(
                _context(S1 + 1, previous=_state(_binding((NOMVOL_SPEC,))),
                         specs=(NOMVOL_SPEC,),
                         binding=_binding((NOMVOL_SPEC,))),
                _state(_binding((NOMVOL_SPEC,))),
                _facts(*QUAL, NANO + S1, spec=NOMVOL_SPEC))):
        for word in ("STOP", "TARGET", "ROLL"):
            assert all(word not in reason for reason in proposal.reasons)


def test_p3_short_mirror() -> None:
    # OQ-1 acceptance: the short mirror. For s=-1 the direction leg is
    # -(C-MA20) < 0, i.e. close ABOVE the MA20; the volume leg is the
    # same no-subtraction ratio. Two adjacent qualifying bars -> EXIT/0.
    binding = _binding((SIGNAL_SPEC,))
    policy = _policy()
    base = _state(binding)
    short_qual = ("102", "100", "1.5")   # close above MA20 against a short
    first = policy.evaluate_with_facts(
        _context(S1 + 1, previous=base, side=-1), base,
        _facts(*short_qual, NANO + S1))
    assert first.action == "KEEP"
    committed = commit_exit_observation(base, first)
    assert _confirm(committed, SIGNAL_SPEC.exit_policy_id).count == 1
    second = policy.evaluate_with_facts(
        _context(S2 + 1, previous=committed, side=-1), committed,
        _facts(*short_qual, NANO + S2))
    assert second.action == "EXIT" and second.target_lots == 0
    # A short bar with close BELOW the MA20 (with-trend for s=-1) resets.
    with_trend = policy.evaluate_with_facts(
        _context(S2 + 1, previous=committed, side=-1), committed,
        _facts("98", "100", "2.0", NANO + S2))
    assert _confirm(with_trend.next_state,
                    SIGNAL_SPEC.exit_policy_id).count == 0


def test_p3_evaluate_exit_pipeline_and_recovery_consistency() -> None:
    # The full pipeline over a bound signal spec: candidates are pure and
    # deterministic; the confirmation chain advances only through the
    # commit boundary; replayed events are idempotent end to end.
    binding = _binding((SIGNAL_SPEC,))
    policy = _policy()
    base = _state(binding)
    context = _context(S1 + 1, previous=base)
    candidates = evaluate_exit(context, base)
    assert len(candidates) == 1
    assert candidates[0].action == "KEEP"   # channel pending: no M2 tuple yet
    committed = commit_exit_observation(base, candidates[0])
    assert committed is base                # nothing observable: no-op
    direct = policy.evaluate_with_facts(context, base,
                                        _facts(*QUAL, NANO + S1))
    committed_one = commit_exit_observation(base, direct)
    assert _confirm(committed_one, SIGNAL_SPEC.exit_policy_id).count == 1
    replay = commit_exit_observation(base, direct)
    assert replay == committed_one          # deterministic chain
    again = policy.evaluate_with_facts(
        _context(S1 + 30, previous=committed_one), committed_one,
        _facts(*QUAL, NANO + S1))
    assert commit_exit_observation(committed_one, again) is committed_one
