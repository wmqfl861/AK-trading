"""M3C P0 contract tests: time-decay parameter shape, elapsed fraction, clock.

OQ-2 Plan A P0 entry ``test_p0_contract_time_decay_p1_parameter_shape``
(open-questions-resolution §2). stage=P0 / contract-only: the P1 full-close
TIME_DECAY parameter version (08 §5.4), the frozen elapsed-fraction formula
and its equality/strict-less boundaries (08 §6.2, §10.6), the deadline
tightening-only-earlier property, and the trading calendar clock computed
locally on SYNTHETIC SessionWindow records. No ``ak_trading.v2.exits``
implementation exists at P0 and none is imported; the trading-clock
implementation itself is NOT claimed verified. M1 types are read-only.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal

import pytest

from ak_trading.v2.contracts import (
    EXIT_PARAMETER_WHITELISTS,
    DataQuality,
    ExitFeatureValue,
    ExitPolicyBinding,
    ExitSpec,
    SessionWindow,
    exit_binding_content_hash,
)
from ak_trading.v2.schemas import decode_record, encode_record

H64 = "a" * 64
H64_B = "b" * 64
H64_C = "c" * 64
NANO = 1_767_920_700_000_000_000
MINUTE_NANO = 60_000_000_000
DQ_READY = DataQuality(status="READY", reasons=(), temporal_basis="observed",
                       input_hash=H64)
DQ_MISSING = DataQuality(status="MISSING", reasons=("NLI_UNVALUED",),
                         temporal_basis="unknown", input_hash=H64)

TIME_DECAY_P1 = {
    "soft_time_fractions": ["0.75"], "minimum_progress_r": ["0.50"],
    "cumulative_close_fractions": ["1"],
}
TIME_DECAY_P2 = {
    "soft_time_fractions": ["0.50", "0.75"], "minimum_progress_r": ["0.25", "0.50"],
    "cumulative_close_fractions": ["0.50", "1"],
}
SOFT_TIME = Decimal("0.75")
MIN_PROGRESS = Decimal("0.50")


def _decay_spec(mode: str = "MINUTE", parameters: dict | None = None,
                parameter_version: str | None = None,
                **overrides) -> ExitSpec:
    suffix, duration = ("m1", 60) if mode == "MINUTE" else ("d1", 86400)
    values = dict(
        exit_policy_id=f"SYNTHETIC:exit_time_decay_{suffix}_v1",
        family="TIME_DECAY", mode=mode, version="1.0.0",
        parameter_version=parameter_version or
        f"SYNTHETIC:exit_time_decay_{suffix}_v1:params-1",
        code_sha256=H64, parameters=dict(parameters or TIME_DECAY_P1),
        required_market_keys=(),
        required_state_keys=(
            f"EXITSTATE:elapsed_fraction:v1.0.0:d{duration}",
            f"EXITSTATE:episode_net_liquidation_r:v1.0.0:d{duration}"),
        applicable_entry_families=("BREAKOUT", "PULLBACK", "TREND"),
        observation_duration_seconds=duration,
        execution_timing="NEXT_LEGAL_WINDOW", validation_stage="RESEARCH")
    values.update(overrides)
    return ExitSpec(**values)


def _binding() -> ExitPolicyBinding:
    values = dict(
        strategy_id="SYNTHETIC:entry-1", entry_spec_sha256=H64,
        exit_spec_sha256s=(H64_B, H64_C), sizing_spec_sha256=None,
        priority=("SYNTHETIC:exit_time_decay_m1_v1",),
        legacy_rules_version="m3b-legacy-rules-v2.1.2")
    sha = exit_binding_content_hash(**values)
    return ExitPolicyBinding(binding_sha256=sha, **values)


def _session(session_id: str, day: date, start: int, end: int,
             kind: str = "DAY") -> SessionWindow:
    return SessionWindow(session_id=session_id, product="SYNPRODUCT",
                         trading_day=day, start_nano=start, end_nano=end,
                         kind=kind, calendar_version="SYNTHETIC-calendar-1",
                         evidence_status="SCENARIO")


# ─── 08 §6.2 frozen relations, computed locally over SYNTHETIC facts ───

def _elapsed_fraction(age: Decimal | None,
                      remaining: Decimal | None) -> Decimal | None:
    """age/(age+remaining); deadline reached → 1; no legal window → None."""
    if age is None or remaining is None:
        return None
    if remaining == 0:
        return Decimal(1)
    return age / (age + remaining)


def _time_decay_triggers(elapsed: Decimal | None,
                         nli_r: Decimal | None) -> bool:
    """elapsed >= soft threshold (equality fires) AND nli_r strictly below."""
    return (elapsed is not None and elapsed >= SOFT_TIME
            and nli_r is not None and nli_r < MIN_PROGRESS)


def _trading_minutes(first_fill: int, now: int,
                     sessions) -> Decimal:
    """Sum of SessionWindow intersections with [first_fill, now] / 60e9."""
    total = 0
    for window in sessions:
        low, high = max(window.start_nano, first_fill), min(window.end_nano, now)
        if high > low:
            total += high - low
    return Decimal(total) / Decimal(MINUTE_NANO)


def test_p0_contract_time_decay_p1_parameter_shape() -> None:
    spec = _decay_spec()
    assert set(spec.parameters) == EXIT_PARAMETER_WHITELISTS["TIME_DECAY"]
    assert spec.parameter_version.endswith(":params-1")
    assert dict(spec.parameters)["cumulative_close_fractions"] == ("1",)  # full close
    # The P2 two-stage arrays are a distinct registered parameter version,
    # never an overwrite of the active P1 identity (08 §5.2/§5.4).
    p2 = _decay_spec(parameters=TIME_DECAY_P2,
                     parameter_version="SYNTHETIC:exit_time_decay_m1_v1:params-2")
    assert p2.parameter_version != spec.parameter_version
    daily = _decay_spec("DAILY")
    assert daily.observation_duration_seconds == 86400
    assert all(key.endswith(":d86400") for key in daily.required_state_keys)
    # Counterexamples: every shape drift is rejected by M1.
    with pytest.raises(ValueError, match="equal_length"):
        _decay_spec(parameters={"soft_time_fractions": ["0.50", "0.75"],
                                "minimum_progress_r": ["0.25"],
                                "cumulative_close_fractions": ["0.50", "1"]})
    with pytest.raises(ValueError, match="strictly_increasing"):
        _decay_spec(parameters={"soft_time_fractions": ["0.75", "0.50"],
                                "minimum_progress_r": ["0.25", "0.50"],
                                "cumulative_close_fractions": ["0.50", "1"]})
    with pytest.raises(ValueError, match="last_must_be_one"):
        _decay_spec(parameters={"soft_time_fractions": ["0.75"],
                                "minimum_progress_r": ["0.50"],
                                "cumulative_close_fractions": ["0.9"]})
    with pytest.raises(ValueError, match="within_0_1"):
        _decay_spec(parameters={"soft_time_fractions": ["1.5"],
                                "minimum_progress_r": ["0.50"],
                                "cumulative_close_fractions": ["1"]})
    with pytest.raises(ValueError, match="within_0_1"):
        _decay_spec(parameters={"soft_time_fractions": ["0.75"],
                                "minimum_progress_r": ["0.50"],
                                "cumulative_close_fractions": ["0", "1"]})
    with pytest.raises(ValueError, match="minimum_progress_r_must_be_positive"):
        _decay_spec(parameters={"soft_time_fractions": ["0.75"],
                                "minimum_progress_r": ["0"],
                                "cumulative_close_fractions": ["1"]})
    with pytest.raises(ValueError, match="exit_parameters_keys"):
        _decay_spec(parameters={**TIME_DECAY_P1, "confirmation_bars": 1})
    with pytest.raises(ValueError, match="exit_parameters_keys"):
        _decay_spec(parameters={"soft_time_fractions": ["0.75"],
                                "minimum_progress_r": ["0.50"]})  # missing key
    with pytest.raises(ValueError, match="observation_duration_seconds"):
        _decay_spec(observation_duration_seconds=86400)  # MINUTE id, DAILY clock


def test_p0_time_decay_elapsed_fraction_boundaries() -> None:
    """08 §10.6: equality fires on the time side, strict-less on progress."""
    horizon = Decimal(30)                      # valid total span: 30 trading minutes
    assert _elapsed_fraction(Decimal("22.5"), horizon - Decimal("22.5")) == \
        Decimal("0.75")
    assert _time_decay_triggers(Decimal("0.75"), Decimal("0.4999")) is True
    # Progress exactly at the threshold does NOT fire (strict less-than).
    assert _time_decay_triggers(Decimal("0.75"), Decimal("0.50")) is False
    # A hair below the time threshold does not fire either.
    below = _elapsed_fraction(Decimal("22.4"), horizon - Decimal("22.4"))
    assert below < Decimal("0.75")
    assert _time_decay_triggers(below, Decimal("0.4999")) is False
    # Deadline reached → elapsed = 1 regardless of age; unknown window → None.
    assert _elapsed_fraction(Decimal("29.5"), Decimal(0)) == Decimal(1)
    assert _time_decay_triggers(Decimal(1), Decimal("0.6")) is False  # progress gate
    assert _elapsed_fraction(None, Decimal("5")) is None
    assert _time_decay_triggers(None, Decimal("0.1")) is False        # no elapsed
    assert _time_decay_triggers(Decimal("0.8"), None) is False        # Nt unknown
    # Boundary values are representable as committed feature records; a None
    # value always carries non-empty reasons.
    key = "EXITSTATE:elapsed_fraction:v1.0.0:d60"
    ready = ExitFeatureValue(feature_key=key, value=Decimal("0.75"),
                             observed_at_nano=NANO, available_at_nano=NANO,
                             quality=DQ_READY, source_hashes={"SYN:1": H64})
    assert ready.value == Decimal("0.75")
    unvalued = ExitFeatureValue(feature_key=key, value=None,
                                observed_at_nano=NANO, available_at_nano=NANO,
                                quality=DQ_MISSING, source_hashes={"SYN:1": H64})
    assert unvalued.quality.reasons == ("NLI_UNVALUED",)
    with pytest.raises(ValueError, match="quality_reasons"):
        ExitFeatureValue(feature_key=key, value=None, observed_at_nano=NANO,
                         available_at_nano=NANO, quality=DQ_READY,
                         source_hashes={"SYN:1": H64})
    assert decode_record(ExitFeatureValue, encode_record(ready)) == ready


def test_p0_time_decay_deadline_tightening_and_trading_clock() -> None:
    """active_exit tightening can only make the threshold arrive earlier."""
    age = Decimal(15)
    original = _elapsed_fraction(age, Decimal(5))
    tightened = _elapsed_fraction(age, Decimal(2))
    assert original == Decimal("0.75")
    assert tightened == Decimal(15) / Decimal(17) > Decimal("0.88")
    # Monotone: shrinking remaining never lowers the fraction, so any
    # tightened deadline triggers whenever the original one did (08 §5.2).
    for remaining in (Decimal(1), Decimal(2), Decimal(5), Decimal(10), Decimal(30)):
        smaller = max(Decimal(1), remaining - Decimal(1))
        assert _elapsed_fraction(age, smaller) >= _elapsed_fraction(age, remaining)
    # Trading clock: 10 min open + 90 min lunch (not a session) + 5 min open
    # gives exactly 15 trading minutes; the clock consumes only the session
    # calendar, so missing bars inside the windows cannot stop it.
    sessions = (
        _session("SYN-SESS-1", date(2026, 1, 9), NANO, NANO + 10 * MINUTE_NANO),
        _session("SYN-SESS-2", date(2026, 1, 9),
                 NANO + 100 * MINUTE_NANO, NANO + 105 * MINUTE_NANO),
    )
    now = NANO + 105 * MINUTE_NANO
    assert _trading_minutes(NANO, now, sessions) == Decimal(15)
    # Lunch break never intersects: extending `now` into it adds no minutes.
    assert _trading_minutes(NANO, NANO + 55 * MINUTE_NANO, sessions) == Decimal(10)
    # Daily clock: completed trading-day steps only; the entry day close is
    # the first completed step and an unfinished current day is not counted.
    day_sessions = (
        _session("SYN-DAY-1", date(2026, 1, 9), NANO, NANO + 300 * MINUTE_NANO),
        _session("SYN-DAY-2", date(2026, 1, 12), NANO + 1440 * MINUTE_NANO,
                 NANO + 1740 * MINUTE_NANO),
        _session("SYN-DAY-3", date(2026, 1, 13), NANO + 2880 * MINUTE_NANO,
                 NANO + 3180 * MINUTE_NANO),
    )
    mid_day3 = NANO + 3000 * MINUTE_NANO
    completed = len({w.trading_day for w in day_sessions if w.end_nano <= mid_day3})
    assert completed == 2                      # day3 end not reached: not counted
    age_key = "EXITSTATE:age_trading_days:v1.0.0:d86400"
    age_value = ExitFeatureValue(feature_key=age_key, value=completed,
                                 observed_at_nano=mid_day3,
                                 available_at_nano=mid_day3, quality=DQ_READY,
                                 source_hashes={"SYN:calendar": H64})
    assert age_value.value == 2
    # OQ-4 binding bytes: the policy identity consumed by the clock branch.
    binding = _binding()
    payload = {
        "strategy_id": binding.strategy_id,
        "entry_spec_sha256": binding.entry_spec_sha256,
        "exit_spec_sha256s": list(binding.exit_spec_sha256s),
        "sizing_spec_sha256": binding.sizing_spec_sha256,
        "priority": list(binding.priority),
        "legacy_rules_version": binding.legacy_rules_version,
    }
    canonical = (json.dumps(payload, sort_keys=True, separators=(",", ":"),
                            ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    assert hashlib.sha256(canonical).hexdigest() == binding.binding_sha256
    assert decode_record(ExitPolicyBinding, encode_record(binding)) == binding
    with pytest.raises(ValueError, match="duplicates"):
        exit_binding_content_hash("SYNTHETIC:entry-1", H64, (H64_B, H64_B),
                                  None, ("p",), "1.0.0")


# ═══ P1 behavior: trading clock, elapsed_fraction and the full-close rule ═══
# Fixtures mirror the trailing file (M3C-owned SYNTHETIC builders; the P0
# contract tests above stay untouched). Session geometry reproduces the
# 08 §10-6 clock cases: 10 open + 90 lunch + 5 open = 15 trading minutes.

import dataclasses  # noqa: E402
import datetime  # noqa: E402

from ak_trading.v2.contracts import (  # noqa: E402
    AccountSnapshot, ExitContext, ExitState, FeeLeg, Fill, LayerSpec,
    PositionLot, RoundBudget, StrategyState, StressPolicy, TradingRule,
    ValuationMark,
)
from ak_trading.v2.strategies.registry import strategy_registry_v2  # noqa: E402
from ak_trading.v2.exits.features import (  # noqa: E402
    build_exit_feature_snapshot, collect_exit_facts,
)
from ak_trading.v2.exits.registry import exit_registry_specs, exit_spec_sha256  # noqa: E402
from ak_trading.v2.exits.time_decay import TimeDecayPolicy  # noqa: E402

MINUTE = 60_000_000_000
HOUR = 60 * MINUTE
DAY1 = datetime.date(2026, 1, 8)
DAY2 = datetime.date(2026, 1, 9)
TD_SPEC = next(s for s in exit_registry_specs("MINUTE") if s.family == "TIME_DECAY")
TD_SPEC_D1 = next(s for s in exit_registry_specs("DAILY") if s.family == "TIME_DECAY")
ENTRY_SPEC = strategy_registry_v2("MINUTE")["trend_m1_v2"].spec


def _td_rule() -> TradingRule:
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
        evidence="VERIFIED", source_sha256=H64)


def _td_mark(price, as_of_nano, status="CURRENT"):
    if status == "UNVALUED":
        return ValuationMark(contract_id="SYN.rb2609", price=None,
                             as_of_nano=None, valued_at_nano=None,
                             source_bar_id=None, source_bar_sha256=None,
                             rule_id=None, status="UNVALUED",
                             reasons=("PRICE_MISSING",))
    return ValuationMark(
        contract_id="SYN.rb2609", price=Decimal(price), as_of_nano=as_of_nano,
        valued_at_nano=as_of_nano, source_bar_id="SYN-BAR-1",
        source_bar_sha256=H64, rule_id="SYN-RULE-1", status=status,
        reasons=("PRICE_STALE",) if status == "STALE" else ())


def _td_session(start, end, day=DAY1, sid="SYN-SESS-1"):
    return SessionWindow(session_id=sid, product="SYN", trading_day=day,
                         start_nano=start, end_nano=end, kind="DAY",
                         calendar_version="cal-v1", evidence_status="VERIFIED")


def _td_open_fill(offset_nano=0, lots=3):
    return Fill(fill_id="SYN-FILL-OPEN-1", intent_id="SYN-INT-OPEN-1",
                contract_id="SYN.rb2609", strategy_id="SYNTHETIC:entry-1",
                episode_id="SYN-EP-1", execution_nano=NANO + offset_nano,
                trading_day=DAY1, side=1, lots=lots, offset="OPEN",
                reference_price=Decimal("100"), execution_price=Decimal("100"),
                fee_cny=Decimal(lots), slippage_cny=Decimal("0"),
                rule_id="SYN-RULE-1", evidence="VERIFIED")


def _td_lot(lots=3):
    return PositionLot(lot_id="SYN-LOT-1", contract_id="SYN.rb2609",
                       strategy_id="SYNTHETIC:entry-1", episode_id="SYN-EP-1",
                       side=1, lots=lots, opened_at_nano=NANO,
                       opened_trading_day=DAY1, entry_price=Decimal("100"),
                       opening_fee_cny=Decimal(lots))


def _td_account(now):
    return AccountSnapshot(
        account_id="SYN-ACCOUNT-1", as_of_nano=now, trading_day=DAY1,
        initial_cash_cny=Decimal("100000"), cash_cny=Decimal("100000"),
        realized_gross_cny=Decimal("0"), fees_cny=Decimal("0"),
        slippage_cny=Decimal("0"), unrealized_cny=Decimal("0"),
        equity_cny=Decimal("100000"), margin_cny=Decimal("0"),
        reserved_margin_cny=Decimal("0"),
        day_start_equity_cny=Decimal("100000"), day_net_cny=Decimal("0"),
        peak_equity_cny=Decimal("100000"), lots=(_td_lot(),), killed=False,
        quality=DQ_READY)


def _td_budget(latest_offset=30 * MINUTE, planned_loss="100"):
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
        latest_exit_nano=NANO + latest_offset, maximum_lots=3,
        planned_loss_cny=Decimal(planned_loss), stress_loss_cny=Decimal("120"),
        reserved_margin_cny=Decimal("1000"), day_loss_limit_cny=Decimal("200"),
        equity_at_start_cny=Decimal("100000"))


def _td_binding(specs):
    values = dict(strategy_id="SYNTHETIC:entry-1", entry_spec_sha256=H64,
                  exit_spec_sha256s=tuple(sorted({exit_spec_sha256(s) for s in specs})),
                  sizing_spec_sha256=None,
                  priority=tuple(s.exit_policy_id for s in specs),
                  legacy_rules_version="m3b-legacy-rules-v2.1.2")
    return ExitPolicyBinding(binding_sha256=exit_binding_content_hash(**values),
                             **values)


def _td_state(binding, **overrides):
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
        phase="MANAGING", last_committed_event_id=None, state_prefix_hash=H64)
    values.update(overrides)
    return ExitState(**values)


def _td_entry_state(budget, active_exit_offset):
    return StrategyState(
        strategy_id="SYNTHETIC:entry-1", episode_id="SYN-EP-1",
        contract_id="SYN.rb2609", phase="HOLDING", setup=None, last_regime=None,
        direction=1, filled_layers=(0,), layer_filled_lots=(3,),
        pending_layer_index=None, active_exit_nano=NANO + active_exit_offset,
        pending_exit_nano=None, pending_intent_ids=(), budget=budget,
        confirmation_count=0, cooldown_until_nano=None,
        last_event_id="SYN-FILL-OPEN-1")


def _td_context(now_offset, price, *, specs=None, sessions=None, budget=None,
                active_exit_offset=30 * MINUTE, status="CURRENT"):
    specs = specs or (TD_SPEC,)
    budget = budget or _td_budget()
    sessions = sessions if sessions is not None else [
        _td_session(NANO - MINUTE, NANO + 60 * MINUTE)]
    now = NANO + int(now_offset)
    mark = _td_mark(price, now, status) if status != "NONE" else None
    return ExitContext(
        now_nano=now, account_id="SYN-ACCOUNT-1", contract_id="SYN.rb2609",
        strategy_id="SYNTHETIC:entry-1", episode_id="SYN-EP-1",
        exit_binding=_td_binding(specs), exit_specs=tuple(specs),
        market_features=(), forecasts=(), entry_spec=ENTRY_SPEC,
        entry_state=_td_entry_state(budget, active_exit_offset), budget=budget,
        account=_td_account(now), lots=(_td_lot(),),
        marks=(mark,) if mark is not None else (),
        episode_fills=(_td_open_fill(),), rules=(_td_rule(),),
        sessions=tuple(sessions),
        previous_exit_state=_td_state(_td_binding(specs)), continuation=None)


def _td_policy(spec=TD_SPEC):
    return TimeDecayPolicy(spec)


def test_p1_time_decay_boundary_equality() -> None:
    # Counterexample 8 (08 §10-6): elapsed exactly 0.75 satisfies the time
    # threshold (>=); progress exactly 0.50 does NOT satisfy "insufficient"
    # (strictly below required).
    # No closes: N = -3 + 30*(P-100) - 3; P=102 -> N=54.
    # R0=108 -> N/R0 = 0.5 exactly -> KEEP despite elapsed == 0.75.
    context = _td_context(Decimal("22.5") * MINUTE, "102",
                          budget=_td_budget(planned_loss="108"))
    proposal = _td_policy().evaluate(context, context.previous_exit_state)
    assert proposal.action == "KEEP"
    assert "PROGRESS_AT_OR_ABOVE_THRESHOLD" in proposal.reasons
    # R0=200 -> 0.27 < 0.50 with elapsed == 0.75 -> EXIT (time equality fires).
    context_low = _td_context(Decimal("22.5") * MINUTE, "102",
                              budget=_td_budget(planned_loss="200"))
    exit_proposal = _td_policy().evaluate(context_low,
                                          context_low.previous_exit_state)
    assert exit_proposal.action == "EXIT"
    assert exit_proposal.target_lots == 0
    assert "TIME_DECAY_NO_PROGRESS" in exit_proposal.reasons


def test_p1_time_decay_trading_clock_lunch_and_missing_bars() -> None:
    # Counterexample 9 (08 §10-6): 10 open + 90 lunch + 5 open = 15 trading
    # minutes of age; the clock never sees bars at all, so missing market
    # data cannot pause it.
    sessions = (_td_session(NANO, NANO + 10 * MINUTE, sid="SYN-SESS-AM"),
                _td_session(NANO + 100 * MINUTE, NANO + 105 * MINUTE,
                            sid="SYN-SESS-PM"))
    context = _td_context(105 * MINUTE, "102", sessions=sessions,
                          budget=_td_budget(latest_offset=105 * MINUTE),
                          active_exit_offset=105 * MINUTE)
    facts = collect_exit_facts(context, context.previous_exit_state)
    assert facts.age_steps == Decimal("15")
    snapshot = build_exit_feature_snapshot(context, context.previous_exit_state)
    ages = {v.feature_key: v for v in snapshot.values
            if "age_trading_minutes" in v.feature_key}
    assert ages["EXITSTATE:age_trading_minutes:v1.0.0:d60"].value == Decimal("15")
    # Deadline reached: elapsed jumps to exactly 1 (never beyond, a fraction).
    deadline_facts = collect_exit_facts(
        _td_context(120 * MINUTE, "102", sessions=sessions,
                    budget=_td_budget(latest_offset=105 * MINUTE),
                    active_exit_offset=105 * MINUTE),
        context.previous_exit_state)
    assert deadline_facts.remaining_steps == 0
    assert deadline_facts.elapsed_fraction == Decimal("1")


def test_p1_time_decay_daily_completed_days_only() -> None:
    # Daily clock: entry-day close is the first completed step; the running
    # day never counts (08 §6.2).
    sessions = (_td_session(NANO, NANO + 6 * HOUR),
                _td_session(NANO + 24 * HOUR, NANO + 30 * HOUR, day=DAY2,
                            sid="SYN-SESS-D2"))
    context = _td_context(7 * HOUR, "102", specs=(TD_SPEC_D1,),
                          sessions=sessions,
                          budget=_td_budget(latest_offset=30 * HOUR),
                          active_exit_offset=30 * HOUR)
    facts = collect_exit_facts(context, context.previous_exit_state)
    assert facts.age_steps == 1          # day 1 completed; day 2 running
    assert facts.remaining_steps == 1    # day 2 ends exactly at the deadline
    assert facts.elapsed_fraction == Decimal("0.5")
    assert _td_policy(TD_SPEC_D1).evaluate(
        context, context.previous_exit_state).action == "KEEP"
    # Past the daily deadline: elapsed == 1, low progress -> full close.
    late = _td_context(31 * HOUR, "100.2", specs=(TD_SPEC_D1,), sessions=sessions,
                       budget=_td_budget(latest_offset=30 * HOUR),
                       active_exit_offset=30 * HOUR)
    assert _td_policy(TD_SPEC_D1).evaluate(
        late, late.previous_exit_state).action == "EXIT"


def test_p1_time_decay_deadline_tightening_only_earlier() -> None:
    # Counterexample 10: at age 15m with a 30m deadline the fraction is
    # 0.6667 (<0.75, KEEP); tightening active_exit to 17m makes it 15/17
    # >= 0.75 -> EXIT. A tightened deadline can never delay the trigger.
    loose = _td_context(15 * MINUTE, "102")
    facts = collect_exit_facts(loose, loose.previous_exit_state)
    assert facts.elapsed_fraction == Decimal("15") / Decimal("30")
    assert _td_policy().evaluate(loose, loose.previous_exit_state).action == "KEEP"
    tight = _td_context(15 * MINUTE, "100.2", active_exit_offset=17 * MINUTE)
    tight_facts = collect_exit_facts(tight, tight.previous_exit_state)
    assert tight_facts.elapsed_fraction > Decimal("0.75")
    assert _td_policy().evaluate(tight, tight.previous_exit_state).action == "EXIT"
    # The effective deadline is min(active_exit, latest_exit); M3B's
    # StrategyState structurally rejects active_exit > latest_exit, so a
    # budget cut can never be silently out-ranked by a stale active_exit.
    assert tight_facts.deadline_nano == NANO + 17 * MINUTE
    with pytest.raises(ValueError, match="active_exit_must_not_exceed"):
        _td_entry_state(_td_budget(latest_offset=20 * MINUTE), 30 * MINUTE)


def test_p1_time_decay_unknown_clock_or_valuation_keeps() -> None:
    # CLOCK_UNAVAILABLE: no legal calendar -> KEEP, and the M4 hard deadline
    # channel is untouched (this policy adds nothing).
    no_calendar = _td_context(15 * MINUTE, "102", sessions=())
    proposal = _td_policy().evaluate(no_calendar,
                                     no_calendar.previous_exit_state)
    assert proposal.action == "KEEP"
    assert "CALENDAR_UNKNOWN" in proposal.reasons
    # Nt undecidable (UNVALUED mark): progress cannot be guessed -> KEEP.
    unvalued = _td_context(22 * MINUTE, None, status="UNVALUED")
    kept = _td_policy().evaluate(unvalued, unvalued.previous_exit_state)
    assert kept.action == "KEEP"
    assert "MARK_UNVALUED" in kept.reasons or "NLI_UNVALUED" in kept.reasons
    # The candidate carries no observation facts: commit is a pure no-op.
    from ak_trading.v2.exits.state import commit_exit_observation
    assert commit_exit_observation(unvalued.previous_exit_state, kept) \
        is unvalued.previous_exit_state
