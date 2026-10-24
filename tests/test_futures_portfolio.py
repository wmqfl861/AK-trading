"""S2 focused tests: portfolio admission, netting, risk wrap, sizing.

Finalized-plan §十二.14 (minimum 8 cases) and §九 pipeline stages a-e.
"""
from __future__ import annotations

import math
from decimal import Decimal

import pytest

from ak_trading.futures.portfolio import (
    PER_VARIANT_EXPOSURE_CAP,
    StrategyRequest,
    admission_review,
    build_portfolio_targets,
    build_slot_budgets,
    constrain_requests,
    net_requests_by_product,
    quantize_lots,
    risk_config_from_policy,
)
from ak_trading.futures.spec import ProductKey, ResearchPolicy

POLICY = ResearchPolicy()
RB = ProductKey("SHFE", "RB")
IF = ProductKey("CFFEX", "IF")
SC = ProductKey("INE", "SC")
M = ProductKey("DCE", "M")
TA = ProductKey("CZCE", "TA")
SI = ProductKey("GFEX", "SI")
ALL_SIX = (RB, SC, M, TA, IF, SI)


def request(
    strategy_id="donchian_trend",
    family="trend",
    product=RB,
    *,
    signal_status="ready",
    direction=1,
    vol=0.02,
    close="3500",
    multiplier="10",
    has_position=False,
    position_direction=0,
    contract=None,
) -> StrategyRequest:
    return StrategyRequest(
        strategy_id=strategy_id,
        family=family,
        product=product,
        contract_instance_id=contract or "SHFE.rb2609|x",
        raw_symbol="SHFE.rb2609",
        signal_status=signal_status,
        direction=direction,
        observed_daily_vol=vol,
        completed_close=Decimal(close),
        multiplier=Decimal(multiplier),
        has_position=has_position,
        position_direction=position_direction,
        decision_id="d1",
    )


def test_admission_drops_warmup_and_invalid_vol_before_risk() -> None:
    requests = [
        request(signal_status="warmup"),
        request(signal_status="unavailable", has_position=True, direction=1),
        request(vol=None),
        request(vol=0.0),
        request(close="0"),
        request(vol=0.02),
    ]
    budgets = build_slot_budgets(requests, POLICY)
    result = admission_review(requests, budgets, POLICY)
    assert len(result.accepted) == 1
    statuses = [target.status for target in result.slot_unallocated]
    reasons = [target.reason for target in result.slot_unallocated]
    assert statuses == ["slot_unallocated"] * 4
    assert "slot_unallocated_warmup" in reasons
    assert "volatility_invalid_not_floored" in reasons
    # An unavailable signal on a held position becomes a reduce_only close
    # intent, never a fabricated zero signal.
    assert [target.status for target in result.close_intents] == ["close_intent"]
    assert result.close_intents[0].reason.startswith("reduce_only:")


def test_slot_budgets_split_trend_and_keep_empty_slots_unallocated() -> None:
    requests = [
        request("donchian_trend", "trend", RB),
        request("ma_trend", "trend", RB),
        request("zscore_reversion", "mean_reversion", RB),
    ]
    budgets = build_slot_budgets(requests, POLICY)
    full_slot = POLICY.slot_share_base
    assert budgets[("donchian_trend", RB)].share == pytest.approx(full_slot / 2)
    assert budgets[("ma_trend", RB)].share == pytest.approx(full_slot / 2)
    assert budgets[("zscore_reversion", RB)].share == pytest.approx(full_slot)
    # A family with no request gets no budget: no redistribution ever.
    assert ("term_structure_carry", RB) not in budgets
    assert ("zscore_reversion", IF) not in budgets


def test_slot_budgets_lone_trend_variant_still_half_slot_b9() -> None:
    # A single trend variant in a multi-product batch gets b/2 (its fixed
    # half of the family/product slot), never the full slot; the missing
    # variant's half stays in cash (§九.2a).
    requests = [
        request("donchian_trend", "trend", RB),
        request("donchian_trend", "trend", IF, close="4000", multiplier="300"),
        request("zscore_reversion", "mean_reversion", RB),
    ]
    budgets = build_slot_budgets(requests, POLICY)
    assert budgets[("donchian_trend", RB)].share == pytest.approx(
        POLICY.slot_share_base / 2
    )
    assert budgets[("donchian_trend", IF)].share == pytest.approx(
        POLICY.slot_share_base / 2
    )


def test_slot_budgets_six_product_two_variant_batch_b9() -> None:
    # Two trend variants across six products each get b/2 — the divisor is
    # per (family, product) slot, never the batch-wide variant count.
    requests = []
    for product in ALL_SIX:
        requests.append(request("donchian_trend", "trend", product))
        requests.append(request("ma_trend", "trend", product))
    budgets = build_slot_budgets(requests, POLICY)
    for product in ALL_SIX:
        assert budgets[("donchian_trend", product)].share == pytest.approx(
            POLICY.slot_share_base / 2
        )
        assert budgets[("ma_trend", product)].share == pytest.approx(
            POLICY.slot_share_base / 2
        )


def test_variant_cap_and_same_contract_netting() -> None:
    # With the sizing floor (0.005) a standard half-slot tops out at
    # (1/36)*target_daily/0.005; a deliberately larger policy exercises the
    # 0.10 per-variant cap that §九.2b prescribes as defense in depth.
    wide = ResearchPolicy(slot_share_base=0.5)
    requests = [
        request("donchian_trend", "trend", vol=0.0001),
        request("ma_trend", "trend", vol=0.0001),
    ]
    budgets_wide = build_slot_budgets(requests, wide)
    _, detail_wide, _, _ = net_requests_by_product(requests, budgets_wide, wide)
    for variant in detail_wide.values():
        for exposure in variant.values():
            assert abs(exposure) == pytest.approx(PER_VARIANT_EXPOSURE_CAP)

    # Standard policy: two same-direction half slots net per contract.
    budgets = build_slot_budgets(requests, POLICY)
    per_product, detail, markers, contract_nets = net_requests_by_product(
        requests, budgets, POLICY
    )
    half_slot = (POLICY.slot_share_base / 2) * POLICY.target_daily / POLICY.sizing_vol_floor
    for variant in detail.values():
        for exposure in variant.values():
            assert exposure == pytest.approx(half_slot)
    assert per_product[RB] == pytest.approx(half_slot * 2)
    assert len(contract_nets) == 1  # both variants on one contract net once

    # Opposite directions on the same contract net to zero with a marker.
    opposed = [
        request("donchian_trend", "trend", direction=1),
        request("ma_trend", "trend", direction=-1),
    ]
    per_product2, _, markers2, nets2 = net_requests_by_product(opposed, budgets, POLICY)
    assert per_product2[RB] == pytest.approx(0.0, abs=1e-12)  # same-contract offset
    assert [m.status for m in markers2] == ["netted_flat"]
    assert nets2[(RB, "SHFE.rb2609|x")].net_exposure == pytest.approx(0.0, abs=1e-12)

    # Different concrete contracts of the same product NEVER offset: the
    # risk layer receives the ABSOLUTE sum — a long far-month plus a short
    # near-month both consume budget, no hedge credit (B10).
    split = [
        request("donchian_trend", "trend", direction=1, contract="SHFE.rb2609|x"),
        request("ma_trend", "trend", direction=-1, contract="SHFE.rb2610|y"),
    ]
    per_product3, _, markers3, nets3 = net_requests_by_product(split, budgets, POLICY)
    assert markers3 == []  # no netted_flat marker across contracts
    per_variant_02 = (POLICY.slot_share_base / 2) * POLICY.target_daily / 0.02
    assert per_product3[RB] == pytest.approx(2 * per_variant_02)  # abs aggregation
    assert nets3[(RB, "SHFE.rb2609|x")].net_exposure == pytest.approx(per_variant_02)
    assert nets3[(RB, "SHFE.rb2610|y")].net_exposure == pytest.approx(-per_variant_02)


def test_risk_wrapper_gross_and_cluster_flags_both_legitimate() -> None:
    # Six products each requesting 0.20: source order applies the gross cap
    # (1.20 -> 0.80) first and then the cluster cap (0.80 -> 0.50), so both
    # flags can legitimately appear (§九.3).
    requested = {product: 0.20 for product in ALL_SIX}
    volatility = {product: 0.02 for product in ALL_SIX}
    outcome = constrain_requests(requested, volatility, POLICY, ALL_SIX)
    assert "gross_cap" in outcome.limits
    assert "cluster_cap" in outcome.limits
    # After gross (1.2->0.8) and cluster (0.8->0.5), the conservative
    # portfolio-vol cap (target_daily) binds last: 0.5*0.02 -> scaled.
    assert "portfolio_vol_cap" in outcome.limits
    total = sum(abs(value) for value in outcome.targets.values())
    assert total == pytest.approx(POLICY.target_daily / 0.02, rel=1e-9)
    assert not outcome.refused


def test_risk_wrapper_maps_invalid_input_to_refusal() -> None:
    requested = {RB: 0.1, IF: 0.1}
    volatility = {RB: 0.02, IF: None}  # invalid vol reaching the wrapper
    outcome = constrain_requests(requested, volatility, POLICY, ALL_SIX)
    assert outcome.refused is True
    assert outcome.risk_refusals == ("CFFEX.IF",)
    assert "invalid_risk_input" in outcome.limits


def test_risk_wrapper_kill_switch_refuses_new_risk() -> None:
    outcome = constrain_requests(
        {RB: 0.1}, {RB: 0.02}, POLICY, ALL_SIX, killed=True
    )
    assert outcome.refused is True
    assert outcome.targets["SHFE.RB"] == 0.0
    assert "kill_switch" in outcome.limits


def test_quantization_toward_zero_and_below_minimum_lot() -> None:
    # q* = 4.9995 floors to 4 (never 5), and the loss is reported.
    result = quantize_lots(0.0005, Decimal("9999"), Decimal("1"), Decimal("1"))
    assert result.q_star == Decimal("4.9995")
    assert result.target_lots == 4
    # Negative exposures also quantize toward zero, not away.
    negative = quantize_lots(-0.00005, Decimal("1000"), Decimal("1"), Decimal("1"))
    assert negative.target_lots == 0
    assert negative.reason == "below_minimum_lot"
    # A tiny request that quantizes to zero is below_minimum_lot, not zero-fill.
    tiny = quantize_lots(1e-9, Decimal("400000000"), Decimal("3500"), Decimal("10"))
    assert tiny.target_lots == 0
    assert tiny.reason == "below_minimum_lot"


def test_quantization_reference_notional_from_plan_table() -> None:
    # §九.4 reference semantics: sigma=2% single full slot at NAV 400M
    # requests slot*target_daily/sigma*NAV of notional; lots floor exactly.
    exposure = POLICY.slot_share_base * POLICY.target_daily / 0.02
    expected_notional = exposure * 400_000_000
    result = quantize_lots(exposure, Decimal("400000000"), Decimal("3500"), Decimal("10"))
    assert float(result.q_star * Decimal("35000")) == pytest.approx(expected_notional, rel=1e-9)
    assert result.target_lots == int(expected_notional // 35000)
    assert result.reason is None


def test_build_targets_statuses_and_diagnostics() -> None:
    requests = [
        request("donchian_trend", "trend", vol=0.02),
        request("zscore_reversion", "mean_reversion", product=IF, vol=0.02,
                close="4000", multiplier="300", direction=-1),
    ]
    budgets = build_slot_budgets(requests, POLICY)
    accepted = admission_review(requests, budgets, POLICY).accepted
    _, _, _, contract_nets = net_requests_by_product(accepted, budgets, POLICY)
    constrained = {str(RB): 0.0175, str(IF): -0.0175}
    targets = build_portfolio_targets(
        accepted, budgets, POLICY, constrained, Decimal("400000000"),
        contract_nets=contract_nets,
    )
    by_status = {t.status: t for t in targets}
    active = by_status["active"]
    expected_lots = int(0.0175 * 400_000_000 // 35000)
    assert active.target_lots == expected_lots
    assert active.raw_requested_exposure is not None
    assert active.post_risk_exposure == pytest.approx(0.0175)
    assert active.q_star is not None

    killed_targets = build_portfolio_targets(
        accepted, budgets, POLICY, constrained, Decimal("400000000"),
        killed=True, contract_nets=contract_nets,
    )
    assert all(t.status == "risk_refusal" for t in killed_targets)

    tiny_nav = build_portfolio_targets(
        accepted, budgets, POLICY, {str(RB): 1e-9, str(IF): 1e-9}, Decimal("100"),
        contract_nets=contract_nets,
    )
    assert {t.status for t in tiny_nav} == {"below_minimum_lot"}


def test_build_targets_allocates_product_exposure_per_contract_b10() -> None:
    # A roll window with two coexisting contracts: the product's post-risk
    # exposure is split pro-rata by absolute net share and each contract is
    # quantized on its own close — never the full product total twice.
    requests = [
        request("donchian_trend", "trend", vol=0.02, contract="SHFE.rb2609|x",
                close="3500"),
        request("ma_trend", "trend", vol=0.02, contract="SHFE.rb2610|y",
                close="3400"),
    ]
    budgets = build_slot_budgets(requests, POLICY)
    accepted = admission_review(requests, budgets, POLICY).accepted
    per_product, _, _, contract_nets = net_requests_by_product(accepted, budgets, POLICY)
    half_slot = (POLICY.slot_share_base / 2) * POLICY.target_daily / 0.02
    assert per_product[RB] == pytest.approx(2 * half_slot)
    # Risk halves the product total; each contract receives half of THAT.
    constrained = {str(RB): half_slot}
    targets = build_portfolio_targets(
        accepted, budgets, POLICY, constrained, Decimal("400000000"),
        contract_nets=contract_nets,
    )
    assert len(targets) == 2
    for target in targets:
        assert target.post_risk_exposure == pytest.approx(half_slot / 2)
    lots = {t.contract_instance_id: t.target_lots for t in targets}
    assert lots["SHFE.rb2609|x"] == int((half_slot / 2) * 400_000_000 // 35000)
    assert lots["SHFE.rb2610|y"] == int((half_slot / 2) * 400_000_000 // 34000)


def test_risk_config_mapping_matches_plan_caps() -> None:
    config = risk_config_from_policy(POLICY)
    assert config.instrument_cap == 0.20
    assert config.gross_cap == 0.80
    assert config.cluster_cap == 0.50
    assert config.portfolio_vol_cap == POLICY.target_daily
    assert config.kill_switch is False
