"""M3C P0 contract tests: EXITSTATE feature keys, scopes, visibility.

OQ-2 Plan A P0 entry ``test_p0_contract_feature_keys_scope_and_visibility``
(open-questions-resolution §2). stage=P0 / contract-only: assertions over the
M1 frozen key registry (08-M3C §6.2/§6.5, prep §3) — the 30 precise keys,
reserved EXITCTX/EXITMKT namespaces rejected until a formal change, window/
scope/clock structure, ordering and source-visibility bounds. Formula
hand-computation is P1 work and is intentionally NOT claimed here. All
fixtures are SYNTHETIC; M1 types are consumed read-only.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal

import pytest

from ak_trading.v2.contracts import (
    EXITCTX_RESERVED_KEYS,
    EXITMKT_RESERVED_KEYS,
    EXIT_STATE_KEYS,
    DataQuality,
    ExitFeatureSnapshot,
    ExitFeatureSpec,
    ExitFeatureValue,
    ExitPolicyBinding,
    exit_binding_content_hash,
)
from ak_trading.v2.schemas import (
    EXIT_FEATURE_SPECS_SCHEMA_ID,
    canonical_json_bytes,
    decode_record,
    encode_record,
)

H64 = "a" * 64
H64_B = "b" * 64
H64_C = "c" * 64
NANO = 1_767_920_700_000_000_000
DQ_READY = DataQuality(status="READY", reasons=(), temporal_basis="observed",
                       input_hash=H64)
DQ_MISSING = DataQuality(status="MISSING", reasons=("NLI_UNVALUED",),
                         temporal_basis="unknown", input_hash=H64)

# (name, window_kind, source_scope, clock per registered duration)
# Frozen from prep §3 / 08 §6.2 table; d-specific clocks map the mode.
KEY_DEFINITIONS: tuple[tuple[str, str, str, dict[int, str]], ...] = (
    ("unrealized_pnl_cny", "CURRENT_STATE", "EPISODE", {60: "NONE", 86400: "NONE"}),
    ("unrealized_pnl_r", "CURRENT_STATE", "EPISODE", {60: "NONE", 86400: "NONE"}),
    ("entry_distance_atr", "CURRENT_STATE", "EPISODE", {60: "NONE", 86400: "NONE"}),
    ("episode_net_liquidation_cny", "EPISODE_PREFIX", "EPISODE",
     {60: "NONE", 86400: "NONE"}),
    ("episode_net_liquidation_r", "EPISODE_PREFIX", "EPISODE",
     {60: "NONE", 86400: "NONE"}),
    ("peak_net_liquidation_r", "EPISODE_PREFIX", "EPISODE", {60: "NONE", 86400: "NONE"}),
    ("profit_giveback_r", "EPISODE_PREFIX", "EPISODE", {60: "NONE", 86400: "NONE"}),
    ("peak_favorable_price", "EPISODE_PREFIX", "EPISODE", {60: "NONE", 86400: "NONE"}),
    ("trailing_atr_distance", "EPISODE_PREFIX", "EPISODE", {60: "NONE", 86400: "NONE"}),
    ("effective_stop_distance_atr", "CURRENT_STATE", "EPISODE",
     {60: "NONE", 86400: "NONE"}),
    ("age_trading_minutes", "EPISODE_PREFIX_CALENDAR", "EPISODE", {60: "TRADING_MINUTE"}),
    ("age_trading_days", "EPISODE_PREFIX_CALENDAR", "EPISODE", {86400: "TRADING_DAY"}),
    ("remaining_steps", "CURRENT_STATE", "EPISODE",
     {60: "TRADING_MINUTE", 86400: "TRADING_DAY"}),
    ("elapsed_fraction", "CURRENT_STATE", "EPISODE",
     {60: "TRADING_MINUTE", 86400: "TRADING_DAY"}),
    ("account_drawdown_ratio", "ACCOUNT_PREFIX", "ACCOUNT", {60: "NONE", 86400: "NONE"}),
    ("closed_fraction", "LIQUIDATION_PREFIX", "EPISODE", {60: "NONE", 86400: "NONE"}),
)


def _binding_sha() -> str:
    return exit_binding_content_hash(
        "SYNTHETIC:entry-1", H64, (H64_B, H64_C), None,
        ("SYNTHETIC:exit_trailing_atr_m1_v1",), "m3b-legacy-rules-v2.1.2")


def _feature_spec(key: str, **overrides) -> ExitFeatureSpec:
    name = key.split(":", 2)[1]
    window_kind, scope, clock = "CURRENT_STATE", "EPISODE", "NONE"
    for candidate, kind, candidate_scope, clocks in KEY_DEFINITIONS:
        if candidate != name:
            continue
        for duration, candidate_clock in clocks.items():
            if key.endswith(f":d{duration}"):
                window_kind, scope, clock = kind, candidate_scope, candidate_clock
    values = dict(
        feature_key=key, formula_version="v1.0.0", unit="SYNTHETIC:unit",
        window_kind=window_kind, minimum_source_facts=1,
        source_scope=scope, clock_unit=clock,
        required_source_keys=("SYNTHETIC:source-1",),
    )
    values.update(overrides)
    return ExitFeatureSpec(**values)


def _value(key: str, value, quality: DataQuality = DQ_MISSING,
           observed: int = NANO, available: int = NANO) -> ExitFeatureValue:
    return ExitFeatureValue(
        feature_key=key, value=value, observed_at_nano=observed,
        available_at_nano=available, quality=quality,
        source_hashes={"SYNTHETIC:source-1": H64})


def _snapshot(values, duration: int = 60, observed: int = NANO,
              available: int = NANO) -> ExitFeatureSnapshot:
    return ExitFeatureSnapshot(
        snapshot_id="SYN-SNAP-1", account_id="SYN-ACCOUNT-1",
        contract_id="SYN.rb2609", strategy_id="SYNTHETIC:entry-1",
        episode_id="SYN-EP-1", exit_binding_sha256=_binding_sha(),
        duration_seconds=duration, observed_at_nano=observed,
        available_at_nano=available, values=values, source_state_sha256=H64)


def test_p0_contract_feature_keys_scope_and_visibility() -> None:
    # 30 precise keys: 14 dual-mode definitions x 2 durations + 2 age keys.
    assert len(EXIT_STATE_KEYS) == 30
    assert len(set(EXIT_STATE_KEYS)) == 30
    assert not any("{d}" in key for key in EXIT_STATE_KEYS)
    d60 = {key for key in EXIT_STATE_KEYS if key.endswith(":d60")}
    d86400 = {key for key in EXIT_STATE_KEYS if key.endswith(":d86400")}
    assert len(d60) == 15 and len(d86400) == 15
    assert "EXITSTATE:age_trading_minutes:v1.0.0:d60" in d60
    assert "EXITSTATE:age_trading_minutes:v1.0.0:d86400" not in EXIT_STATE_KEYS
    assert "EXITSTATE:age_trading_days:v1.0.0:d86400" in d86400
    assert "EXITSTATE:age_trading_days:v1.0.0:d60" not in EXIT_STATE_KEYS
    # Every registered definition constructs with the frozen window/scope/clock
    # structure (08 §6.5); unregistered or cross-mode keys are rejected.
    for name, window_kind, scope, clocks in KEY_DEFINITIONS:
        for duration, clock in clocks.items():
            key = f"EXITSTATE:{name}:v1.0.0:d{duration}"
            assert key in EXIT_STATE_KEYS
            spec = _feature_spec(key)
            assert spec.window_kind == window_kind
            assert spec.source_scope == scope
            assert spec.clock_unit == clock
    with pytest.raises(ValueError, match="must_be_registered"):
        _feature_spec("EXITSTATE:age_trading_minutes:v1.0.0:d86400")  # mode isolation
    with pytest.raises(ValueError, match="must_be_registered"):
        _feature_spec("EXITSTATE:not_a_key:v1.0.0:d60")
    # Reserved namespaces: P0 rejects them anywhere an EXITSTATE key fits.
    assert len(EXITCTX_RESERVED_KEYS) == 6
    assert len(EXITMKT_RESERVED_KEYS) == 6
    for reserved in (EXITCTX_RESERVED_KEYS[0], EXITMKT_RESERVED_KEYS[0]):
        with pytest.raises(ValueError, match="reserved_namespace"):
            _feature_spec(reserved)
        with pytest.raises(ValueError, match="reserved_namespace"):
            _value(reserved, None)
    peak_key = "EXITSTATE:peak_favorable_price:v1.0.0:d60"
    elapsed_key = "EXITSTATE:elapsed_fraction:v1.0.0:d60"
    nli_key = "EXITSTATE:episode_net_liquidation_r:v1.0.0:d60"
    # Values must be sorted and unique by feature key.
    with pytest.raises(ValueError, match="sorted_unique"):
        _snapshot((_value(peak_key, None), _value(nli_key, None)))  # reverse order
    with pytest.raises(ValueError, match="sorted_unique"):
        _snapshot((_value(peak_key, None), _value(peak_key, None)))
    # Visibility: no source may become visible after the snapshot bound, and
    # the snapshot itself may not be available before it was observed.
    with pytest.raises(ValueError, match="exceed_snapshot"):
        _snapshot((_value(elapsed_key, Decimal("0.5"), DQ_READY,
                          available=NANO + 1),), available=NANO)
    with pytest.raises(ValueError, match="precede"):
        _snapshot((_value(elapsed_key, Decimal("0.5"), DQ_READY),),
                  observed=NANO + 1, available=NANO)
    # Duration suffix of every value key must match the snapshot mode.
    with pytest.raises(ValueError, match="duration_suffix_mismatch"):
        _snapshot((_value(elapsed_key, None),), duration=86400)
    assert _snapshot((_value(elapsed_key, None), _value(nli_key, None))) \
        .duration_seconds == 60


def test_p0_features_spec_shape_and_quality_discipline() -> None:
    elapsed_key = "EXITSTATE:elapsed_fraction:v1.0.0:d60"
    spec = _feature_spec(elapsed_key)
    assert spec.formula_version == "v1.0.0"
    assert ":v1.0.0:" in spec.feature_key
    assert EXIT_FEATURE_SPECS_SCHEMA_ID == "ak_probability_exit_feature_specs_v1.0.0"
    # Counterexamples: formula/spec/value discipline.
    with pytest.raises(ValueError, match="formula_version"):
        _feature_spec(elapsed_key, formula_version="v0.9.0")
    # A different in-key version segment is simply not a registered key (all
    # 30 frozen keys embed v1.0.0); the key gate rejects it first.
    with pytest.raises(ValueError, match="must_be_registered"):
        _feature_spec("EXITSTATE:elapsed_fraction:v2.0.0:d60")
    with pytest.raises(ValueError, match="at_least_1"):
        _feature_spec(elapsed_key, minimum_source_facts=0)
    with pytest.raises(ValueError, match="required_source_keys_must_be_sorted"):
        _feature_spec(elapsed_key, required_source_keys=("SYNTHETIC:b", "SYNTHETIC:a"))
    with pytest.raises(ValueError, match="quality_reasons"):
        _value(elapsed_key, None, quality=DQ_READY)  # missing value needs reasons
    with pytest.raises(ValueError, match="value_must_be_decimal_int_or_none"):
        _value(elapsed_key, True, quality=DQ_READY)  # bool is not a value
    # READY with a real Decimal value and an int-valued key both encode.
    snapshot = _snapshot((
        _value("EXITSTATE:closed_fraction:v1.0.0:d60", Decimal("0"),
               quality=DQ_READY),
        _value(elapsed_key, Decimal("0.75"), quality=DQ_READY)))
    assert snapshot.values[0].value == Decimal("0")
    assert snapshot.values[1].value == Decimal("0.75")


def test_p0_features_binding_hash_bytes_and_roundtrip() -> None:
    """OQ-4 binding bytes roundtrip: the snapshot binds the episode via the hash."""
    sha = _binding_sha()
    payload = {
        "strategy_id": "SYNTHETIC:entry-1",
        "entry_spec_sha256": H64,
        "exit_spec_sha256s": sorted([H64_B, H64_C]),
        "sizing_spec_sha256": None,
        "priority": ["SYNTHETIC:exit_trailing_atr_m1_v1"],
        "legacy_rules_version": "m3b-legacy-rules-v2.1.2",
    }
    canonical = (json.dumps(payload, sort_keys=True, separators=(",", ":"),
                            ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    assert hashlib.sha256(canonical).hexdigest() == sha
    # exit-hash order-insensitive recomputation gives the identical bytes.
    assert exit_binding_content_hash(
        "SYNTHETIC:entry-1", H64, (H64_C, H64_B), None,
        ("SYNTHETIC:exit_trailing_atr_m1_v1",), "m3b-legacy-rules-v2.1.2") == sha
    peak_key = "EXITSTATE:peak_favorable_price:v1.0.0:d60"
    snapshot = _snapshot((_value(peak_key, Decimal("103.0"), quality=DQ_READY),))
    assert snapshot.exit_binding_sha256 == sha
    decoded = decode_record(ExitFeatureSnapshot, encode_record(snapshot))
    assert decoded == snapshot
    assert canonical_json_bytes(encode_record(decoded)) == \
        canonical_json_bytes(encode_record(snapshot))
    # ExitPolicyBinding built over the same identity roundtrips losslessly;
    # a structurally valid but wrong binding hash is rejected at construction.
    binding = ExitPolicyBinding(
        binding_sha256=sha, strategy_id="SYNTHETIC:entry-1", entry_spec_sha256=H64,
        exit_spec_sha256s=(H64_B, H64_C), sizing_spec_sha256=None,
        priority=("SYNTHETIC:exit_trailing_atr_m1_v1",),
        legacy_rules_version="m3b-legacy-rules-v2.1.2")
    assert decode_record(ExitPolicyBinding, encode_record(binding)) == binding
    with pytest.raises(ValueError, match="content_hash"):
        ExitPolicyBinding(
            binding_sha256=H64, strategy_id="SYNTHETIC:entry-1",
            entry_spec_sha256=H64, exit_spec_sha256s=(H64_B, H64_C),
            sizing_spec_sha256=None,
            priority=("SYNTHETIC:exit_trailing_atr_m1_v1",),
            legacy_rules_version="m3b-legacy-rules-v2.1.2")
    spec = _feature_spec(peak_key)
    assert decode_record(ExitFeatureSpec, encode_record(spec)) == spec

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
                active_exit_offset=30 * MINUTE):
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
        exit_binding=_fx_binding(specs), exit_specs=tuple(specs),
        market_features=(), forecasts=(), entry_spec=ENTRY_SPEC,
        entry_state=_fx_entry_state(budget, active_exit_offset), budget=budget,
        account=account or _fx_account(now, lots), lots=tuple(lots),
        marks=(mark,) if mark is not None else (),
        episode_fills=tuple(fills), rules=(_fx_rule(),),
        sessions=tuple(sessions),
        previous_exit_state=previous, continuation=None)


# ═══ P1 behavior: Nt hand-calc, unknown-valuation, mode isolation ═══════════

def test_p1_features_nli_hand_calc() -> None:
    # 08 §10-4 hand calculation: 3 lots @100 (mult 10), 1 closed @104,
    # 2 remaining, mark 103, fees 1/lot, no slippage:
    # RG=40, F=4, U=60, K=2, N=94, N/R0=0.94.
    close_fill = _fx_fill(fill_id="SYN-FILL-CLOSE-1",
                          intent_id="SYN-INT-CLOSE-1", offset="CLOSE",
                          lots=1, price="104", fee="1", nano=5 * MINUTE)
    context = _fx_context(
        10 * MINUTE, _fx_mark("103", NANO + 10 * MINUTE),
        lots=(_fx_lot(lots=2, lot_id="SYN-LOT-1"),),
        fills=(_fx_fill(), close_fill))
    facts = collect_exit_facts(context, None)
    assert facts.realized_gross == Decimal("40")
    assert facts.fees_paid == Decimal("4")
    assert facts.unrealized == Decimal("60")
    assert facts.exit_cost == Decimal("2")
    assert facts.net_liquidation == Decimal("94")
    assert facts.net_liquidation_r == Decimal("0.94")
    snapshot = build_exit_feature_snapshot(context, None)
    values = {value.feature_key: value for value in snapshot.values}
    assert values["EXITSTATE:unrealized_pnl_cny:v1.0.0:d60"].value == Decimal("60")
    assert values["EXITSTATE:episode_net_liquidation_cny:v1.0.0:d60"].value == Decimal("94")
    assert values["EXITSTATE:episode_net_liquidation_r:v1.0.0:d60"].value == Decimal("0.94")
    assert values["EXITSTATE:entry_distance_atr:v1.0.0:d60"].value == Decimal("1.5")
    assert values["EXITSTATE:peak_favorable_price:v1.0.0:d60"].value == Decimal("103")
    # With a committed net peak of 1.2R the giveback is 1.2-0.94 = 0.26 and
    # realized P&L is never re-losing slippage (08 §10-4 second half).
    previous = _fx_state(
        _fx_binding((TRAILING_SPEC,)), qbase=3, closed_lots=1,
        net_peak_r=Decimal("1.2"), net_peak_source_nano=NANO + 5 * MINUTE,
        net_peak_source_hash=H64A)
    facts_peak = collect_exit_facts(context, previous)
    assert facts_peak.net_peak_r == Decimal("1.2")
    assert facts_peak.giveback_r == Decimal("0.26")
    snapshot_peak = build_exit_feature_snapshot(context, previous)
    peak_values = {value.feature_key: value for value in snapshot_peak.values}
    assert peak_values["EXITSTATE:profit_giveback_r:v1.0.0:d60"].value == Decimal("0.26")
    assert peak_values["EXITSTATE:peak_net_liquidation_r:v1.0.0:d60"].value == Decimal("1.2")


def test_p1_features_unknown_valuation_never_guesses() -> None:
    # Counterexample 7: an UNVALUED mark means Nt is undecidable — never 0,
    # never the entry price, never an unrealized-only subtotal. Field-level
    # quality is independent: the calendar-driven age stays computable.
    context = _fx_context(15 * MINUTE, _fx_mark(None, NANO + 15 * MINUTE,
                                                "UNVALUED"))
    facts = collect_exit_facts(context, None)
    assert facts.unrealized is None
    assert facts.net_liquidation is None
    assert facts.net_liquidation_r is None
    assert facts.nli_reasons
    assert facts.age_steps == Decimal("15")
    snapshot = build_exit_feature_snapshot(context, None)
    values = {value.feature_key: value for value in snapshot.values}
    nli = values["EXITSTATE:episode_net_liquidation_cny:v1.0.0:d60"]
    assert nli.value is None and nli.quality.reasons
    unrealized = values["EXITSTATE:unrealized_pnl_cny:v1.0.0:d60"]
    assert unrealized.value is None and unrealized.quality.status != "READY"
    age = values["EXITSTATE:age_trading_minutes:v1.0.0:d60"]
    assert age.value == Decimal("15") and age.quality.status == "READY"
    # Without a frozen qbase the closed fraction is WARMUP, not a guessed 0.
    closed = values["EXITSTATE:closed_fraction:v1.0.0:d60"]
    assert closed.value is None and closed.quality.status == "WARMUP"
    # No qualifying peak observation -> None, never a fabricated 0.
    peak = values["EXITSTATE:peak_favorable_price:v1.0.0:d60"]
    assert peak.value is None and peak.quality.status == "WARMUP"


def test_p1_features_mode_isolation_and_shape() -> None:
    # Counterexample 11: minute and daily snapshots carry only their own
    # duration-suffixed keys (15 each), sorted and unique; the age key is
    # mode-specific; no runtime ":d{d}" brace keys ever appear.
    from ak_trading.v2.exits.features import exit_feature_registry_v1
    context_m1 = _fx_context(10 * MINUTE, _fx_mark("102", NANO + 10 * MINUTE))
    snapshot = build_exit_feature_snapshot(context_m1, None)
    keys = [value.feature_key for value in snapshot.values]
    assert len(keys) == 15
    assert keys == sorted(set(keys))
    assert all(key.endswith(":d60") for key in keys)
    assert not any(":d86400" in key for key in keys)
    assert "EXITSTATE:age_trading_minutes:v1.0.0:d60" in keys
    assert "EXITSTATE:age_trading_days:v1.0.0:d86400" not in keys
    daily_specs = exit_registry_specs("DAILY")
    sessions = (_fx_session(NANO, NANO + 6 * HOUR),
                _fx_session(NANO + 24 * HOUR, NANO + 30 * HOUR, day=DAY2,
                            sid="SYN-SESS-D2"))
    context_d1 = _fx_context(
        7 * HOUR, _fx_mark("102", NANO + 7 * HOUR), specs=daily_specs,
        sessions=sessions, budget=_fx_budget(latest_offset=30 * HOUR),
        active_exit_offset=30 * HOUR)
    daily = build_exit_feature_snapshot(context_d1, None)
    daily_keys = [value.feature_key for value in daily.values]
    assert len(daily_keys) == 15
    assert all(key.endswith(":d86400") for key in daily_keys)
    assert "EXITSTATE:age_trading_days:v1.0.0:d86400" in daily_keys
    assert "EXITSTATE:age_trading_minutes:v1.0.0:d60" not in daily_keys
    # Registry shape: 15 + 15 = the frozen 30-key EXITSTATE registry.
    assert len(exit_feature_registry_v1("MINUTE")) == 15
    assert len(exit_feature_registry_v1("DAILY")) == 15
    # Visibility invariants: every value is visible no later than the
    # snapshot itself and the snapshot is visible at its observation time.
    for value in snapshot.values:
        assert value.available_at_nano <= snapshot.available_at_nano
    assert snapshot.available_at_nano >= snapshot.observed_at_nano


def test_p1_features_future_mark_invisible() -> None:
    # Counterexample 6 (visibility): a mark dated AFTER the decision time is
    # not visible — it can neither value the position nor arm anything.
    context = _fx_context(10 * MINUTE, _fx_mark("999", NANO + 20 * MINUTE))
    facts = collect_exit_facts(context, None)
    assert facts.mark is None
    assert facts.unrealized is None
    snapshot = build_exit_feature_snapshot(context, None)
    values = {value.feature_key: value for value in snapshot.values}
    unrealized = values["EXITSTATE:unrealized_pnl_cny:v1.0.0:d60"]
    assert unrealized.value is None and unrealized.quality.status == "MISSING"


def test_p1_features_account_and_closed_fraction() -> None:
    context = _fx_context(10 * MINUTE, _fx_mark("102", NANO + 10 * MINUTE))
    facts = collect_exit_facts(context, None)
    assert facts.account_drawdown == Decimal("0")
    assert facts.closed_fraction is None  # qbase not frozen in the state yet
    previous = _fx_state(_fx_binding((TRAILING_SPEC,)), qbase=3, closed_lots=1)
    facts_closed = collect_exit_facts(context, previous)
    assert facts_closed.closed_fraction == Decimal(1) / Decimal(3)
    degraded = _fx_context(
        10 * MINUTE, _fx_mark("102", NANO + 10 * MINUTE),
        account=_fx_account(NANO + 10 * MINUTE, quality=DataQuality(
            status="STALE", reasons=("MARK_STALE",), temporal_basis="observed",
            input_hash=H64A)))
    degraded_facts = collect_exit_facts(degraded, None)
    assert degraded_facts.account_drawdown is None
    assert degraded_facts.account_reasons
