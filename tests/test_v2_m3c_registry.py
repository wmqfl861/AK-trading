"""M3C P0 contract tests: exit registry identity, whitelists and bindings.

OQ-2 Plan A P0 entry ``test_p0_contract_registry_binding_and_parameters``
(open-questions-resolution §2). stage=P0 / contract-only: these assertions
cover the M1 public contract surface (08-M3C §4.1/§5.4, 007 §5.1, OQ-4) and
prove nothing about P1 registry/trailing/time-decay behaviour. No
``ak_trading.v2.exits`` module is imported — none exists at P0. Every fixture
is SYNTHETIC; M1 types are consumed read-only.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from ak_trading.v2.contracts import (
    EXIT_PARAMETER_WHITELISTS,
    ExitPolicyBinding,
    ExitSpec,
    exit_binding_content_hash,
)
from ak_trading.v2.schemas import (
    EXIT_BINDINGS_SCHEMA_ID,
    EXIT_SPECS_SCHEMA_ID,
    canonical_json_bytes,
    decode_record,
    encode_record,
)

H64 = "a" * 64
H64_B = "b" * 64
H64_C = "c" * 64
LEGACY_RULES = "m3b-legacy-rules-v2.1.2"

TRAILING_PARAMETERS = {
    "arm_profit_r": "0.5", "trail_atr_multiple": "1",
    "atr_basis": "FROZEN_ATR", "peak_source": "COMPLETED_CLOSE",
}
TIME_DECAY_P1_PARAMETERS = {
    "soft_time_fractions": ["0.75"], "minimum_progress_r": ["0.50"],
    "cumulative_close_fractions": ["1"],
}


def _spec_sha256(spec: ExitSpec) -> str:
    """SYNTHETIC spec content hash over canonical encode bytes (one trailing LF)."""
    return hashlib.sha256(canonical_json_bytes(encode_record(spec))).hexdigest()


def _trailing_spec(mode: str = "MINUTE", **overrides) -> ExitSpec:
    suffix, duration = ("m1", 60) if mode == "MINUTE" else ("d1", 86400)
    key = f"d{duration}"
    values = dict(
        exit_policy_id=f"SYNTHETIC:exit_trailing_atr_{suffix}_v1",
        family="TRAILING_STOP", mode=mode, version="1.0.0",
        parameter_version=f"SYNTHETIC:exit_trailing_atr_{suffix}_v1:params-1",
        code_sha256=H64, parameters=dict(TRAILING_PARAMETERS),
        required_market_keys=(f"BASE:close:v2.1.0:{key}",),
        required_state_keys=(
            f"EXITSTATE:effective_stop_distance_atr:v1.0.0:{key}",
            f"EXITSTATE:episode_net_liquidation_r:v1.0.0:{key}",
            f"EXITSTATE:peak_favorable_price:v1.0.0:{key}"),
        applicable_entry_families=("BREAKOUT", "PULLBACK", "TREND"),
        observation_duration_seconds=duration,
        execution_timing="NEXT_LEGAL_WINDOW", validation_stage="RESEARCH")
    values.update(overrides)
    return ExitSpec(**values)


def _decay_spec(mode: str = "MINUTE", **overrides) -> ExitSpec:
    suffix, duration = ("m1", 60) if mode == "MINUTE" else ("d1", 86400)
    key = f"d{duration}"
    values = dict(
        exit_policy_id=f"SYNTHETIC:exit_time_decay_{suffix}_v1",
        family="TIME_DECAY", mode=mode, version="1.0.0",
        parameter_version=f"SYNTHETIC:exit_time_decay_{suffix}_v1:params-1",
        code_sha256=H64_B, parameters=dict(TIME_DECAY_P1_PARAMETERS),
        required_market_keys=(),
        required_state_keys=(
            f"EXITSTATE:elapsed_fraction:v1.0.0:{key}",
            f"EXITSTATE:episode_net_liquidation_r:v1.0.0:{key}"),
        applicable_entry_families=("BREAKOUT", "PULLBACK", "TREND"),
        observation_duration_seconds=duration,
        execution_timing="NEXT_LEGAL_WINDOW", validation_stage="RESEARCH")
    values.update(overrides)
    return ExitSpec(**values)


def _legacy_spec(mode: str = "MINUTE", **overrides) -> ExitSpec:
    suffix, duration = ("m1", 60) if mode == "MINUTE" else ("d1", 86400)
    values = dict(
        exit_policy_id=f"SYNTHETIC:exit_legacy_{suffix}_v1",
        family="LEGACY", mode=mode, version="1.0.0",
        parameter_version=f"SYNTHETIC:exit_legacy_{suffix}_v1:params-1",
        code_sha256=H64_C, parameters={},
        required_market_keys=(), required_state_keys=(),
        applicable_entry_families=("BREAKOUT", "GRID", "MARTINGALE",
                                   "PULLBACK", "REVERSION", "TREND"),
        observation_duration_seconds=duration,
        execution_timing="LEGACY_PROTECTIVE", validation_stage="RESEARCH")
    values.update(overrides)
    return ExitSpec(**values)


def _binding(strategy_id: str = "SYNTHETIC:entry-1", **overrides) -> ExitPolicyBinding:
    values = dict(
        strategy_id=strategy_id,
        entry_spec_sha256=H64,
        exit_spec_sha256s=tuple(sorted(
            _spec_sha256(spec) for spec in (_trailing_spec(), _decay_spec()))),
        sizing_spec_sha256=None,
        priority=("SYNTHETIC:exit_trailing_atr_m1_v1",
                  "SYNTHETIC:exit_time_decay_m1_v1"),
        legacy_rules_version=LEGACY_RULES,
    )
    values.update(overrides)
    sha = exit_binding_content_hash(**values)
    return ExitPolicyBinding(binding_sha256=sha, **values)


def test_p0_contract_registry_binding_and_parameters() -> None:
    minute = (_legacy_spec(), _trailing_spec(), _decay_spec())
    daily = (_legacy_spec("DAILY"), _trailing_spec("DAILY"), _decay_spec("DAILY"))
    for spec in minute + daily:
        assert spec.version == "1.0.0"
        assert spec.parameter_version == f"{spec.exit_policy_id}:params-1"
        assert spec.validation_stage == "RESEARCH"
        assert set(spec.parameters) == EXIT_PARAMETER_WHITELISTS[spec.family]
        assert spec.observation_duration_seconds == (60 if spec.mode == "MINUTE" else 86400)
    assert minute[0].execution_timing == "LEGACY_PROTECTIVE"
    assert minute[1].execution_timing == "NEXT_LEGAL_WINDOW"
    # Exact key sets and frozen defensive copies (lists become tuples).
    assert dict(_trailing_spec().parameters) == {
        "arm_profit_r": ("0.5"), "trail_atr_multiple": ("1"),
        "atr_basis": ("FROZEN_ATR"), "peak_source": ("COMPLETED_CLOSE")}
    assert dict(_decay_spec().parameters)["soft_time_fractions"] == ("0.75",)
    assert EXIT_PARAMETER_WHITELISTS["LEGACY"] == frozenset()
    # Counterexamples (08 §10.1): every whitelist drift is rejected.
    with pytest.raises(ValueError, match="exit_parameters_keys"):
        _trailing_spec(parameters={"arm_profit_r": "0.5"})                    # missing keys
    with pytest.raises(ValueError, match="exit_parameters_keys"):
        _trailing_spec(parameters={**TRAILING_PARAMETERS, "confirmation_bars": 1})  # extra
    with pytest.raises(ValueError, match="exit_parameter_atr_basis_enum"):
        _trailing_spec(parameters={**TRAILING_PARAMETERS, "atr_basis": "BASE_ATR14"})
    with pytest.raises(ValueError, match="observation_duration_seconds"):
        _trailing_spec(observation_duration_seconds=86400)                    # mode mismatch
    with pytest.raises(ValueError, match="WAIT"):
        _trailing_spec(applicable_entry_families=("WAIT",))
    with pytest.raises(ValueError, match="non_empty_tuple"):
        _trailing_spec(applicable_entry_families=())
    with pytest.raises(ValueError, match="required_state_keys_must_be_sorted"):
        _trailing_spec(required_state_keys=(
            f"EXITSTATE:peak_favorable_price:v1.0.0:d60",
            f"EXITSTATE:episode_net_liquidation_r:v1.0.0:d60",
            f"EXITSTATE:effective_stop_distance_atr:v1.0.0:d60"))
    with pytest.raises(ValueError, match="whitelist"):
        _legacy_spec(parameters={"anything": "1"})
    with pytest.raises(ValueError, match="exit_parameter_arm_profit_r"):
        _trailing_spec(parameters={**TRAILING_PARAMETERS, "arm_profit_r": "0"})
    # Binding identity: supplied hash must equal the OQ-4 recomputation.
    binding = _binding()
    assert binding.binding_sha256 == exit_binding_content_hash(
        binding.strategy_id, binding.entry_spec_sha256, binding.exit_spec_sha256s,
        binding.sizing_spec_sha256, binding.priority, binding.legacy_rules_version)
    with pytest.raises(ValueError, match="content_hash"):
        ExitPolicyBinding(binding_sha256=H64, strategy_id=binding.strategy_id,
                          entry_spec_sha256=binding.entry_spec_sha256,
                          exit_spec_sha256s=binding.exit_spec_sha256s,
                          sizing_spec_sha256=None, priority=binding.priority,
                          legacy_rules_version=binding.legacy_rules_version)
    with pytest.raises(ValueError, match="stored_sorted"):
        _binding(exit_spec_sha256s=(H64_C, H64_B))
    with pytest.raises(ValueError, match="duplicates"):
        _binding(exit_spec_sha256s=(H64_B, H64_B))
    with pytest.raises(ValueError, match="duplicates"):
        _binding(priority=("SYNTHETIC:p1", "SYNTHETIC:p1"))


def test_p0_registry_minute_daily_registration_identity() -> None:
    """Independent minute/daily identities (08 §5.4: same values are not shared evidence)."""
    for mode in ("MINUTE", "DAILY"):
        for build in (_legacy_spec, _trailing_spec, _decay_spec):
            spec = build(mode)
            assert spec.exit_policy_id.endswith(
                "_m1_v1" if mode == "MINUTE" else "_d1_v1")
            assert all(key.endswith(f":d{spec.observation_duration_seconds}")
                       for key in spec.required_state_keys)
    trailing_m1, trailing_d1 = _trailing_spec(), _trailing_spec("DAILY")
    assert trailing_m1.exit_policy_id != trailing_d1.exit_policy_id
    assert _spec_sha256(trailing_m1) != _spec_sha256(trailing_d1)
    # Cross-mode key tails are rejected in both directions.
    with pytest.raises(ValueError, match="duration_suffix_mismatch"):
        _trailing_spec(required_market_keys=("BASE:close:v2.1.0:d86400",))
    with pytest.raises(ValueError, match="duration_suffix_mismatch"):
        _trailing_spec("DAILY", required_state_keys=(
            "EXITSTATE:peak_favorable_price:v1.0.0:d60",))
    # Runtime keys never carry the literal brace expansion.
    for spec in (_trailing_spec(), _trailing_spec("DAILY")):
        assert "{d}" not in "".join(spec.required_state_keys)


def test_p0_registry_binding_hash_bytes_and_roundtrip() -> None:
    """OQ-4: exit_binding_content_hash is the single binding formula, byte-exact."""
    got = exit_binding_content_hash(
        "SYNTHETIC:entry-1", "11" * 32, ("22" * 32, "33" * 32), None,
        ("SYNTHETIC:legacy", "SYNTHETIC:trailing"), "1.0.0")
    assert got == "be0cf5030962ee6ec30ba6036b41c0b870f2af3dd0aaf0260f716c3d17d54c61"
    payload = {
        "strategy_id": "SYNTHETIC:entry-1",
        "entry_spec_sha256": "11" * 32,
        "exit_spec_sha256s": sorted(["22" * 32, "33" * 32]),
        "sizing_spec_sha256": None,
        "priority": ["SYNTHETIC:legacy", "SYNTHETIC:trailing"],
        "legacy_rules_version": "1.0.0",
    }
    canonical = (json.dumps(payload, sort_keys=True, separators=(",", ":"),
                            ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    assert hashlib.sha256(canonical).hexdigest() == got
    # Without the single trailing LF the digest is not a binding value.
    assert hashlib.sha256(canonical[:-1]).hexdigest() != got
    binding = _binding()
    assert exit_binding_content_hash(
        binding.strategy_id, binding.entry_spec_sha256,
        tuple(reversed(binding.exit_spec_sha256s)), binding.sizing_spec_sha256,
        binding.priority, binding.legacy_rules_version) == binding.binding_sha256
    assert exit_binding_content_hash(
        "s", H64, (H64_B, H64_C), None, ("p1", "p2"), "1") != \
        exit_binding_content_hash("s", H64, (H64_B, H64_C), None, ("p2", "p1"), "1")
    # The null sizing key is encoded as a present JSON null, never dropped.
    null_binding = _binding()
    assert b'"sizing_spec_sha256":null' in canonical_json_bytes({
        "strategy_id": null_binding.strategy_id,
        "entry_spec_sha256": null_binding.entry_spec_sha256,
        "exit_spec_sha256s": list(null_binding.exit_spec_sha256s),
        "sizing_spec_sha256": null_binding.sizing_spec_sha256,
        "priority": list(null_binding.priority),
        "legacy_rules_version": null_binding.legacy_rules_version})
    with pytest.raises(ValueError):
        exit_binding_content_hash("", H64, (H64_B,), None, ("p",), "1")
    with pytest.raises(ValueError, match="64_lowercase_hex"):
        exit_binding_content_hash("s", "not-a-hash", (H64_B,), None, ("p",), "1")
    with pytest.raises(ValueError, match="non_empty_tuple"):
        exit_binding_content_hash("s", H64, (), None, ("p",), "1")
    # Lossless schema roundtrips preserve identity bytes (08 §12 envelopes).
    assert EXIT_SPECS_SCHEMA_ID == "ak_probability_exit_specs_v1.0.0"
    assert EXIT_BINDINGS_SCHEMA_ID == "ak_probability_exit_bindings_v1.0.0"
    for record in (_trailing_spec(), _trailing_spec("DAILY"), _decay_spec(),
                   _legacy_spec(), binding):
        assert canonical_json_bytes(encode_record(decode_record(
            type(record), encode_record(record)))) == \
            canonical_json_bytes(encode_record(record))

# ═══ P1 behavior fixtures (M3C-owned SYNTHETIC builders; P0 tests above
# stay untouched). Geometry follows 08 §10: S0=98/T0=106, A0=2, R0=100,
# multiplier 10, tick 0.2, 1 CNY per lot fees, zero slippage.

import dataclasses  # noqa: E402
import datetime  # noqa: E402

from decimal import Decimal  # noqa: E402
from ak_trading.v2.contracts import (  # noqa: E402
    AccountSnapshot, DataQuality, ExitContext, ExitPolicyBinding, ExitState,
    FeeLeg, Fill, LayerSpec, PositionLot, RoundBudget, SessionWindow,
    StrategyState, StressPolicy, TradingRule, ValuationMark,
    exit_binding_content_hash,
)
from ak_trading.v2.strategies.registry import strategy_registry_v2  # noqa: E402
from ak_trading.v2.exits.features import (  # noqa: E402
    build_exit_feature_snapshot, collect_exit_facts,
)
from ak_trading.v2.exits.registry import (  # noqa: E402
    exit_registry_specs, exit_spec_sha256,
)

NANO = 1_767_920_700_000_000_000
MINUTE = 60_000_000_000
HOUR = 60 * MINUTE
DAY1 = datetime.date(2026, 1, 8)
DAY2 = datetime.date(2026, 1, 9)
SPECS_M1 = exit_registry_specs("MINUTE")
LEGACY_SPEC = next(s for s in SPECS_M1 if s.family == "LEGACY")
TRAILING_SPEC = next(s for s in SPECS_M1 if s.family == "TRAILING_STOP")
TIME_DECAY_SPEC = next(s for s in SPECS_M1 if s.family == "TIME_DECAY")
ENTRY_SPEC = strategy_registry_v2("MINUTE")["trend_m1_v2"].spec
H64A = "a" * 64
DQ_READY = DataQuality(status="READY", reasons=(), temporal_basis="observed",
                       input_hash=H64A)


def _fx_rule() -> TradingRule:
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


def _fx_mark(price, as_of_nano, status="CURRENT"):
    if status == "UNVALUED":
        return ValuationMark(contract_id="SYN.rb2609", price=None,
                             as_of_nano=None, valued_at_nano=None,
                             source_bar_id=None, source_bar_sha256=None,
                             rule_id=None, status="UNVALUED",
                             reasons=("PRICE_MISSING",))
    return ValuationMark(
        contract_id="SYN.rb2609", price=Decimal(price), as_of_nano=as_of_nano,
        valued_at_nano=as_of_nano, source_bar_id="SYN-BAR-1",
        source_bar_sha256=H64A, rule_id="SYN-RULE-1", status=status,
        reasons=("PRICE_STALE",) if status == "STALE" else ())


def _fx_session(start, end, day=DAY1, sid="SYN-SESS-1"):
    return SessionWindow(session_id=sid, product="SYN", trading_day=day,
                         start_nano=start, end_nano=end, kind="DAY",
                         calendar_version="cal-v1", evidence_status="VERIFIED")


def _fx_fill(fill_id="SYN-FILL-OPEN-1", intent_id="SYN-INT-OPEN-1",
             offset="OPEN", lots=3, price="100", fee=None, nano=0, side=1):
    return Fill(fill_id=fill_id, intent_id=intent_id, contract_id="SYN.rb2609",
                strategy_id="SYNTHETIC:entry-1", episode_id="SYN-EP-1",
                execution_nano=NANO + nano, trading_day=DAY1, side=side,
                lots=lots, offset=offset, reference_price=Decimal(price),
                execution_price=Decimal(price),
                fee_cny=Decimal(lots) if fee is None else Decimal(fee),
                slippage_cny=Decimal("0"), rule_id="SYN-RULE-1",
                evidence="VERIFIED")


def _fx_lot(lots=3, price="100", side=1, lot_id="SYN-LOT-1"):
    return PositionLot(lot_id=lot_id, contract_id="SYN.rb2609",
                       strategy_id="SYNTHETIC:entry-1", episode_id="SYN-EP-1",
                       side=side, lots=lots, opened_at_nano=NANO,
                       opened_trading_day=DAY1, entry_price=Decimal(price),
                       opening_fee_cny=Decimal(lots))


def _fx_account(now, lots=(), quality=None):
    return AccountSnapshot(
        account_id="SYN-ACCOUNT-1", as_of_nano=now, trading_day=DAY1,
        initial_cash_cny=Decimal("100000"), cash_cny=Decimal("100000"),
        realized_gross_cny=Decimal("0"), fees_cny=Decimal("0"),
        slippage_cny=Decimal("0"), unrealized_cny=Decimal("0"),
        equity_cny=Decimal("100000"), margin_cny=Decimal("0"),
        reserved_margin_cny=Decimal("0"),
        day_start_equity_cny=Decimal("100000"), day_net_cny=Decimal("0"),
        peak_equity_cny=Decimal("100000"), lots=tuple(lots), killed=False,
        quality=quality or DQ_READY)


def _fx_budget(latest_offset=30 * MINUTE, planned_loss="100", side=1,
               stop="98", target="106", initial_atr="2"):
    return RoundBudget(
        budget_id="SYN-BUDGET-1", episode_id="SYN-EP-1", contract_id="SYN.rb2609",
        frozen_at_nano=NANO, range_id=None, frozen_range=None,
        stress_policy=StressPolicy(
            policy_id="SYNTHETIC:stress-1", gap_atr_multiple=Decimal("1"),
            locked_trading_days=3, adverse_atr_per_day=Decimal("1"),
            release_extra_atr=Decimal("1"), fee_multiplier=Decimal("1"),
            slippage_multiplier=Decimal("1"), margin_multiplier=Decimal("1")),
        initial_atr_points=Decimal(initial_atr), side=side,
        layers=(LayerSpec(index=0, trigger_price=Decimal("100"),
                          reference_entry_price=Decimal("100"), lots=3),),
        stop_price=Decimal(stop), target_price=Decimal(target),
        latest_exit_nano=NANO + latest_offset, maximum_lots=3,
        planned_loss_cny=Decimal(planned_loss), stress_loss_cny=Decimal("120"),
        reserved_margin_cny=Decimal("1000"), day_loss_limit_cny=Decimal("200"),
        equity_at_start_cny=Decimal("100000"))


def _fx_binding(specs, priority=None):
    values = dict(strategy_id="SYNTHETIC:entry-1", entry_spec_sha256=H64A,
                  exit_spec_sha256s=tuple(sorted({exit_spec_sha256(s) for s in specs})),
                  sizing_spec_sha256=None,
                  priority=priority or tuple(s.exit_policy_id for s in specs),
                  legacy_rules_version="m3b-legacy-rules-v2.1.2")
    return ExitPolicyBinding(binding_sha256=exit_binding_content_hash(**values),
                             **values)


def _fx_state(binding, **overrides):
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


def _fx_entry_state(budget, active_exit_offset=30 * MINUTE):
    return StrategyState(
        strategy_id="SYNTHETIC:entry-1", episode_id="SYN-EP-1",
        contract_id="SYN.rb2609", phase="HOLDING", setup=None, last_regime=None,
        direction=1, filled_layers=(0,), layer_filled_lots=(3,),
        pending_layer_index=None, active_exit_nano=NANO + active_exit_offset,
        pending_exit_nano=None, pending_intent_ids=(), budget=budget,
        confirmation_count=0, cooldown_until_nano=None,
        last_event_id="SYN-FILL-OPEN-1")


def _fx_context(now_offset, mark, *, specs=None, budget=None, lots=None,
                fills=None, sessions=None, previous=None, account=None,
                active_exit_offset=30 * MINUTE, binding=None):
    specs = specs or (TRAILING_SPEC,)
    budget = budget or _fx_budget()
    lots = [_fx_lot()] if lots is None else lots
    fills = [_fx_fill()] if fills is None else fills
    sessions = sessions if sessions is not None else [
        _fx_session(NANO - MINUTE, NANO + 60 * MINUTE)]
    now = NANO + int(now_offset)
    return ExitContext(
        now_nano=now, account_id="SYN-ACCOUNT-1", contract_id="SYN.rb2609",
        strategy_id="SYNTHETIC:entry-1", episode_id="SYN-EP-1",
        exit_binding=binding or _fx_binding(specs), exit_specs=tuple(specs),
        market_features=(), forecasts=(), entry_spec=ENTRY_SPEC,
        entry_state=_fx_entry_state(budget, active_exit_offset), budget=budget,
        account=account or _fx_account(now, lots), lots=tuple(lots),
        marks=(mark,) if mark is not None else (),
        episode_fills=tuple(fills), rules=(_fx_rule(),),
        sessions=tuple(sessions),
        previous_exit_state=previous, continuation=None)


# ═══ P1 behavior: registry identities, evaluate_exit, arbitration, word-ban ═

from ak_trading.v2.decision.pipeline import exit_registry_hash_for  # noqa: E402
from ak_trading.v2.exits.registry import (  # noqa: E402
    FORBIDDEN_REASON_SUBSTRINGS, assert_reason_vocabulary, arbitrate_exit,
    evaluate_exit, exit_registry_sha256, exit_registry_specs, exit_registry_v1,
    exit_spec_sha256,
)
from ak_trading.v2.exits.state import commit_exit_observation  # noqa: E402


def test_p1_registry_identities_and_whitelists() -> None:
    for mode, suffix in (("MINUTE", "m1"), ("DAILY", "d1")):
        specs = exit_registry_specs(mode)
        ids = [spec.exit_policy_id for spec in specs]
        assert ids == sorted(ids)
        # P2 registry: the P1 trio plus five P2 registrations per mode.
        # P3 adds the two SIGNAL_REVERSAL variants (OQ-1: independent ids
        # under one family, no runtime variant key).
        assert ids == [f"exit_episode_drawdown_{suffix}_v1",
                       f"exit_legacy_{suffix}_v1",
                       f"exit_profit_ladder_{suffix}_v1",
                       f"exit_scale_out_{suffix}_v1",
                       f"exit_signal_reversal_{suffix}_v1",
                       f"exit_signal_reversal_nomvol_{suffix}_v1",
                       f"exit_time_decay_{suffix}_v1",
                       f"exit_time_decay_{suffix}_v2",
                       f"exit_trailing_atr_{suffix}_v1",
                       f"exit_volatility_stop_{suffix}_v1"]
        by_id = {spec.exit_policy_id: spec for spec in specs}
        legacy = by_id[f"exit_legacy_{suffix}_v1"]
        assert legacy.family == "LEGACY" and dict(legacy.parameters) == {}
        assert legacy.execution_timing == "LEGACY_PROTECTIVE"
        assert legacy.applicable_entry_families == (
            "BREAKOUT", "GRID", "MARTINGALE", "PULLBACK", "REVERSION", "TREND")
        trailing = by_id[f"exit_trailing_atr_{suffix}_v1"]
        assert dict(trailing.parameters) == {
            "arm_profit_r": "0.5", "trail_atr_multiple": "1",
            "atr_basis": "FROZEN_ATR", "peak_source": "COMPLETED_CLOSE"}
        assert trailing.applicable_entry_families == ("BREAKOUT", "PULLBACK", "TREND")
        assert trailing.execution_timing == "NEXT_LEGAL_WINDOW"
        decay = by_id[f"exit_time_decay_{suffix}_v1"]
        assert dict(decay.parameters) == {
            "soft_time_fractions": ("0.75",),
            "minimum_progress_r": ("0.50",),
            "cumulative_close_fractions": ("1",)}
        for spec in specs:
            assert "WAIT" not in spec.applicable_entry_families
            assert spec.version == "1.0.0"
            # The two-stage TIME_DECAY registration carries the params-2
            # identity (08 §5.4); every other registration is params-1.
            expected_suffix = ":params-2" if spec.exit_policy_id.endswith("_v2")                 else ":params-1"
            assert spec.parameter_version == f"{spec.exit_policy_id}{expected_suffix}"
            assert spec.validation_stage == "RESEARCH"
        # M4's assembly helper and the M3C registry hash agree exactly.
        assert exit_registry_sha256(specs) == exit_registry_hash_for(specs)
        assert exit_registry_sha256(specs) == exit_registry_sha256(specs)
    with pytest.raises(ValueError, match="duplicate_exit_specs"):
        exit_registry_sha256((SPECS_M1[0], SPECS_M1[0]))
    # PARTIAL_LIQUIDATION is a sizing combiner and maps to no policy.
    assert set(exit_registry_v1("MINUTE")) == {
        "exit_episode_drawdown_m1_v1", "exit_legacy_m1_v1",
        "exit_profit_ladder_m1_v1", "exit_signal_reversal_m1_v1",
        "exit_signal_reversal_nomvol_m1_v1", "exit_time_decay_m1_v1",
        "exit_time_decay_m1_v2", "exit_trailing_atr_m1_v1",
        "exit_volatility_stop_m1_v1"}


def test_p1_registry_evaluate_and_arbitrate_no_conflict() -> None:
    # Counterexample 12: three bound policies, two of them independently EXIT
    # at the same observation — arbitration yields exactly one full-close
    # target, every loser candidate stays in the audit trail, and legacy
    # never adds an exit of its own.
    specs = (LEGACY_SPEC, TRAILING_SPEC, TIME_DECAY_SPEC)
    priority = (TRAILING_SPEC.exit_policy_id, TIME_DECAY_SPEC.exit_policy_id,
                LEGACY_SPEC.exit_policy_id)
    binding = _fx_binding(specs, priority=priority)
    armed = _fx_state(
        binding, armed=True, price_peak=Decimal("102"),
        price_peak_source_nano=NANO + 5 * MINUTE, price_peak_source_hash=H64A,
        protection_stop_price=Decimal("100"),
        protection_line_version="exit_trailing_atr_m1_v1:line:100",
        protection_effective_from_nano=NANO + 5 * MINUTE)
    # Mark 99.9: below the line (EXIT) and Nt/R0 = -0.09 < 0.5 with
    # elapsed = 22.5/(22.5+7.5) = 0.75 (time decay EXIT too).
    context = _fx_context(Decimal("22.5") * MINUTE,
                          _fx_mark("99.9", NANO + int(Decimal("22.5") * MINUTE)),
                          specs=specs, previous=armed, binding=binding)
    candidates = evaluate_exit(context, armed)
    assert len(candidates) == 3
    exits = [p for p in candidates if p.action == "EXIT"]
    keeps = [p for p in candidates if p.action == "KEEP"]
    assert len(exits) == 2 and len(keeps) == 1
    assert keeps[0].triggering_policy_id == "exit_legacy_m1_v1"
    assert {p.triggering_policy_id for p in exits} == {
        "exit_trailing_atr_m1_v1", "exit_time_decay_m1_v1"}
    winner = arbitrate_exit(candidates, context)
    assert winner.action == "EXIT" and winner.target_lots == 0
    assert winner.triggering_policy_id == "exit_trailing_atr_m1_v1"
    for proposal in candidates:
        assert proposal.target_lots in (0, 3)
        assert proposal.earliest_execution_nano >= proposal.observed_at_nano
    # Nothing triggers: three KEEP candidates, first in binding priority wins.
    calm = _fx_context(10 * MINUTE, _fx_mark("102", NANO + 10 * MINUTE),
                       specs=specs, previous=_fx_state(binding), binding=binding)
    calm_candidates = evaluate_exit(calm, calm.previous_exit_state)
    assert all(p.action == "KEEP" for p in calm_candidates)
    assert arbitrate_exit(calm_candidates, calm).triggering_policy_id \
        == "exit_trailing_atr_m1_v1"


def test_p1_registry_binding_guards() -> None:
    # Unregistered policy ids and specs whose hash is not in the binding
    # are rejected before any evaluation.
    rogue = dataclasses.replace(
        TRAILING_SPEC, exit_policy_id="exit_trailing_atr_m9_v1")
    context = _fx_context(10 * MINUTE, _fx_mark("102", NANO + 10 * MINUTE),
                          specs=(rogue,))
    with pytest.raises(ValueError, match="exit_policy_not_in_registry"):
        evaluate_exit(context, context.previous_exit_state)
    # Same id but drifted parameters: a different content hash that the
    # binding (built from the registry specs) does not carry.
    drifted = dataclasses.replace(
        TRAILING_SPEC,
        parameters={**dict(TRAILING_SPEC.parameters), "arm_profit_r": "0.6"})
    drifted_binding_values = dict(
        strategy_id="SYNTHETIC:entry-1", entry_spec_sha256=H64A,
        exit_spec_sha256s=(exit_spec_sha256(drifted),),
        sizing_spec_sha256=None,
        priority=(drifted.exit_policy_id,),
        legacy_rules_version="m3b-legacy-rules-v2.1.2")
    from ak_trading.v2.contracts import exit_binding_content_hash
    drifted_binding = ExitPolicyBinding(
        binding_sha256=exit_binding_content_hash(**drifted_binding_values),
        **drifted_binding_values)
    now = NANO + 10 * MINUTE
    drifted_context = dataclasses.replace(
        context, exit_binding=drifted_binding, exit_specs=(drifted,))
    with pytest.raises(ValueError):
        evaluate_exit(drifted_context, drifted_context.previous_exit_state)


def test_p1_registry_reason_word_ban() -> None:
    # Counterexample 15 (08 §7.1 mis-routing hazard): M3C reasons never
    # contain STOP/TARGET/ROLL substrings that the current purpose router
    # in decision/replay.py sniffs out of proposal reasons.
    assert FORBIDDEN_REASON_SUBSTRINGS == ("STOP", "TARGET", "ROLL")
    for poisoned in ("TRAILING_STOP_HIT", "PROFIT_TARGET_REACHED",
                     "ROLL_EXIT_FALLBACK"):
        with pytest.raises(ValueError, match="exit_reason_forbidden_substring"):
            assert_reason_vocabulary((poisoned,))
    specs = (LEGACY_SPEC, TRAILING_SPEC, TIME_DECAY_SPEC)
    binding = _fx_binding(specs)
    for price in ("99.9", "102", "94"):
        context = _fx_context(
            10 * MINUTE, _fx_mark(price, NANO + 10 * MINUTE), specs=specs,
            previous=_fx_state(binding))
        for proposal in evaluate_exit(context, context.previous_exit_state):
            assert_reason_vocabulary(proposal.reasons)
            for word in FORBIDDEN_REASON_SUBSTRINGS:
                assert all(word not in reason for reason in proposal.reasons)
