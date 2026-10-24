"""Fixed-slot futures portfolio admission, netting, risk wrap and sizing.

Implements finalized-plan §九 pipeline stages a-e as pure functions:
admission before risk, per-variant caps, same-contract signed netting, a
narrow wrapper over ``ak_trading.research.apply_risk`` (instrument 0.20 /
gross 0.80 / cluster 0.50 / portfolio-vol cap, all six products as one
conservative cluster), and toward-zero lot quantization. Only fills (built
by ``ak_trading.futures.backtest``) change holdings; this module never
fabricates zero signals for unavailable inputs.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from typing import Mapping, Sequence

from ak_trading.futures.spec import (
    PortfolioTarget,
    ProductKey,
    ResearchPolicy,
)
from ak_trading.research import ResearchConfig, apply_risk

__all__ = [
    "StrategyRequest",
    "SlotBudget",
    "PER_VARIANT_EXPOSURE_CAP",
    "build_slot_budgets",
    "admission_review",
    "net_requests_by_product",
    "constrain_requests",
    "quantize_lots",
    "build_portfolio_targets",
    "risk_config_from_policy",
]

#: §九.2b: every variant is capped at 0.10 before netting.
PER_VARIANT_EXPOSURE_CAP = 0.10

#: §八.B.2: the trend family's two registered variants split every
#: family/product slot; the split is fixed, not request-count derived.
TREND_VARIANT_IDS: tuple[str, ...] = ("donchian_trend", "ma_trend")


@dataclass(frozen=True)
class StrategyRequest:
    """One strategy's directional request arriving at the portfolio."""

    strategy_id: str
    family: str
    product: ProductKey
    contract_instance_id: str
    raw_symbol: str
    signal_status: str  # ready | flat | warmup | unavailable
    direction: int  # -1 / 0 / +1 logical target
    observed_daily_vol: float | None
    completed_close: Decimal | None
    multiplier: Decimal | None
    lot_step: int = 1
    has_position: bool = False
    position_direction: int = 0
    decision_id: str = ""

    def __post_init__(self) -> None:
        if self.direction not in (-1, 0, 1):
            raise ValueError("direction must be -1, 0 or +1")


@dataclass(frozen=True)
class SlotBudget:
    """Slot share for one variant; trend variants split a family slot."""

    strategy_id: str
    family: str
    product: ProductKey
    share: float


def build_slot_budgets(
    requests: Sequence[StrategyRequest],
    policy: ResearchPolicy,
    families: Sequence[str] = ("trend", "mean_reversion", "term_structure_carry"),
    products: Sequence[ProductKey] = (),
) -> dict[tuple[str, ProductKey], SlotBudget]:
    """Fixed allocation table per §九.1/§八.B.2 (B9 fix).

    b = 1/18 per family/product slot. The trend slot is ALWAYS split b/2
    per variant (the family has exactly two registered variants), so a lone
    trend variant never absorbs the full slot and a missing variant's half
    stays in cash — budgets are a fixed table, never count-derived and
    never redistributed.
    """
    universe = tuple(products) or tuple({r.product for r in requests})
    slot = policy.slot_share_base
    per_variant_share: dict[tuple[str, str], float] = {}
    for family in families:
        variants = sorted({r.strategy_id for r in requests if r.family == family})
        if family == "trend":
            # Fixed two-way split regardless of how many trend variants are
            # requesting; the absent half remains unallocated cash (§九.2a).
            variants = sorted(TREND_VARIANT_IDS)
        for strategy_id in variants:
            per_variant_share[(family, strategy_id)] = slot / len(variants)
    budgets: dict[tuple[str, ProductKey], SlotBudget] = {}
    for request in requests:
        share = per_variant_share.get((request.family, request.strategy_id))
        if share is None:
            continue
        budgets[(request.strategy_id, request.product)] = SlotBudget(
            strategy_id=request.strategy_id,
            family=request.family,
            product=request.product,
            share=share,
        )
    return budgets


@dataclass(frozen=True)
class AdmissionResult:
    accepted: tuple[StrategyRequest, ...]
    slot_unallocated: tuple[PortfolioTarget, ...]
    close_intents: tuple[PortfolioTarget, ...]


def admission_review(
    requests: Sequence[StrategyRequest],
    budgets: Mapping[tuple[str, ProductKey], SlotBudget],
    policy: ResearchPolicy,
) -> AdmissionResult:
    """Pipeline stage (a) (§九.2a): drop invalid requests BEFORE risk.

    warmup/unavailable/invalid-volatility requests never reach apply_risk;
    each becomes slot_unallocated with its reason. A request whose direction
    differs from an existing position's reduction becomes an independent
    reduce_only close intent — never a fabricated zero signal.
    """
    accepted: list[StrategyRequest] = []
    unallocated: list[PortfolioTarget] = []
    close_intents: list[PortfolioTarget] = []

    for request in requests:
        budget = budgets.get((request.strategy_id, request.product))
        drop_reason: str | None = None
        if budget is None:
            drop_reason = "no_slot_budget"
        elif request.signal_status not in ("ready", "flat"):
            drop_reason = f"slot_unallocated_{request.signal_status}"
        elif request.observed_daily_vol is None or not math.isfinite(
            request.observed_daily_vol
        ) or request.observed_daily_vol <= 0:
            drop_reason = "volatility_invalid_not_floored"
        elif request.completed_close is None or request.completed_close <= 0:
            drop_reason = "completed_close_invalid"
        elif request.multiplier is None or request.multiplier <= 0:
            drop_reason = "multiplier_invalid"

        if drop_reason is not None:
            if request.has_position:
                close_intents.append(
                    PortfolioTarget(
                        decision_id=request.decision_id,
                        strategy_ids=(request.strategy_id,),
                        product=request.product,
                        contract_instance_id=request.contract_instance_id,
                        raw_symbol=request.raw_symbol,
                        target_lots=0,
                        status="close_intent",
                        reason=f"reduce_only:{drop_reason}",
                    )
                )
            else:
                unallocated.append(
                    PortfolioTarget(
                        decision_id=request.decision_id,
                        strategy_ids=(request.strategy_id,),
                        product=request.product,
                        contract_instance_id=request.contract_instance_id,
                        raw_symbol=request.raw_symbol,
                        target_lots=0,
                        status="slot_unallocated",
                        reason=drop_reason,
                    )
                )
            continue
        accepted.append(request)

    return AdmissionResult(
        accepted=tuple(accepted),
        slot_unallocated=tuple(unallocated),
        close_intents=tuple(close_intents),
    )


@dataclass(frozen=True)
class ContractNet:
    """Per-contract signed net after variant caps (B10 fix)."""

    product: ProductKey
    contract_instance_id: str
    raw_symbol: str
    net_exposure: float
    strategy_ids: tuple[str, ...]
    raw_requested_exposure: float
    representative: StrategyRequest


def net_requests_by_product(
    accepted: Sequence[StrategyRequest],
    budgets: Mapping[tuple[str, ProductKey], SlotBudget],
    policy: ResearchPolicy,
) -> tuple[dict[ProductKey, float], dict[str, dict[str, float]], list[PortfolioTarget], dict[tuple[ProductKey, str], "ContractNet"]]:
    """Stage (b): cap variants, net per (product, concrete contract).

    Netting happens per contract so different expiries never offset
    (§九.2b). The per-product figure handed to the risk layer aggregates
    ABSOLUTE contract nets: a long far-month and a short near-month both
    consume budget — no hedge credit is ever granted (B10 fix). Returns
    (per-product abs exposure, per-variant detail, netted_flat markers,
    per-contract nets).
    """
    detail: dict[str, dict[str, float]] = {}
    markers: list[PortfolioTarget] = []
    by_contract: dict[tuple[ProductKey, str], ContractNet] = {}
    raw_by_contract: dict[tuple[ProductKey, str], float] = {}
    strategies_by_contract: dict[tuple[ProductKey, str], list[str]] = {}
    representative_by_contract: dict[tuple[ProductKey, str], StrategyRequest] = {}

    for request in accepted:
        budget = budgets[(request.strategy_id, request.product)]
        raw = (
            budget.share
            * policy.target_daily
            / max(request.observed_daily_vol, policy.sizing_vol_floor)
            * request.direction
        )
        capped = max(-PER_VARIANT_EXPOSURE_CAP, min(PER_VARIANT_EXPOSURE_CAP, raw))
        detail.setdefault(request.strategy_id, {})[str(request.product)] = capped
        key = (request.product, request.contract_instance_id)
        existing = by_contract.get(key)
        net = capped + (existing.net_exposure if existing else 0.0)
        raw_by_contract[key] = raw_by_contract.get(key, 0.0) + raw
        strategies_by_contract.setdefault(key, []).append(request.strategy_id)
        representative_by_contract.setdefault(key, request)
        by_contract[key] = ContractNet(
            product=request.product,
            contract_instance_id=request.contract_instance_id,
            raw_symbol=request.raw_symbol,
            net_exposure=net,
            strategy_ids=tuple(strategies_by_contract[key]),
            raw_requested_exposure=raw_by_contract[key],
            representative=representative_by_contract[key],
        )

    per_product: dict[ProductKey, float] = {}
    for (product, contract_id), contract_net in by_contract.items():
        if contract_net.net_exposure == 0.0:
            markers.append(
                PortfolioTarget(
                    decision_id="",
                    strategy_ids=contract_net.strategy_ids,
                    product=product,
                    contract_instance_id=contract_id,
                    raw_symbol=contract_net.raw_symbol,
                    target_lots=0,
                    status="netted_flat",
                    reason="offsetting_requests_same_contract",
                )
            )
        # Absolute aggregation: opposing contracts across expiries both
        # consume risk budget; the risk layer never sees a signed offset.
        per_product[product] = per_product.get(product, 0.0) + abs(contract_net.net_exposure)
    return per_product, detail, markers, by_contract


def risk_config_from_policy(policy: ResearchPolicy) -> ResearchConfig:
    """Map §九.2c caps onto the frozen research risk API."""
    return ResearchConfig(
        instrument_cap=policy.instrument_cap,
        gross_cap=policy.gross_cap,
        cluster_cap=policy.cluster_cap,
        portfolio_vol_cap=policy.target_daily,
        kill_switch=False,
    )


@dataclass(frozen=True)
class ConstrainResult:
    targets: dict[str, float]
    limits: tuple[str, ...]
    risk_refusals: tuple[str, ...]
    refused: bool


def constrain_requests(
    requested: Mapping[ProductKey, float],
    volatility: Mapping[ProductKey, float | None],
    policy: ResearchPolicy,
    cluster_products: Sequence[ProductKey],
    *,
    killed: bool = False,
) -> ConstrainResult:
    """Stage (c-d): apply_risk wrap; invalid_risk_input => risk_refusal.

    The cluster list always contains all six product keys even when only a
    subset requested exposure (§九.2c). Any invalid_risk_input flag cancels
    new risk for the affected batch conservatively — exits remain callable
    by the engine independent of this result.
    """
    requested_by_key = {str(product): value for product, value in requested.items()}
    volatility_by_key = {
        str(product): value for product, value in volatility.items()
    }
    outcome = apply_risk(
        requested_by_key,
        volatility_by_key,
        [[str(p) for p in cluster_products]],
        risk_config_from_policy(policy),
        killed=killed,
    )
    limits = tuple(outcome.get("limits", ()))
    targets = {str(key): value for key, value in outcome.get("targets", {}).items()}
    refused_keys: list[str] = []
    refused = False
    if "invalid_risk_input" in limits or "kill_switch" in limits:
        refused = True
        for key, value in requested_by_key.items():
            vol = volatility_by_key.get(key)
            if vol is None or not math.isfinite(vol) or vol <= 1e-12:
                refused_keys.append(key)
    return ConstrainResult(
        targets=targets,
        limits=limits,
        risk_refusals=tuple(refused_keys),
        refused=refused,
    )


@dataclass(frozen=True)
class QuantizeResult:
    q_star: Decimal
    target_lots: int
    reason: str | None


def quantize_lots(
    post_risk_exposure: float,
    nav: Decimal,
    completed_close: Decimal,
    multiplier: Decimal,
    lot_step: int = 1,
) -> QuantizeResult:
    """Stage (e): q* = exposure*NAV/(close*multiplier), toward zero (§九.8).

    Quantization never rounds up to a minimum; a non-zero request that
    quantizes to zero is below_minimum_lot.
    """
    if lot_step < 1:
        raise ValueError("lot_step must be a positive integer")
    if completed_close <= 0 or multiplier <= 0:
        return QuantizeResult(Decimal(0), 0, "invalid_price_or_multiplier")
    q_star = Decimal(str(post_risk_exposure)) * nav / (completed_close * multiplier)
    sign = Decimal(0) if q_star == 0 else (Decimal(1) if q_star > 0 else Decimal(-1))
    magnitude = abs(q_star)
    lots = int((magnitude / Decimal(lot_step)).to_integral_value(rounding=ROUND_FLOOR))
    return QuantizeResult(
        q_star=q_star,
        target_lots=int(sign) * lots,
        reason="below_minimum_lot" if q_star != 0 and lots == 0 else None,
    )


def build_portfolio_targets(
    accepted: Sequence[StrategyRequest],
    budgets: Mapping[tuple[str, ProductKey], SlotBudget],
    policy: ResearchPolicy,
    constrained: Mapping[object, float],
    nav: Decimal,
    killed: bool = False,
    contract_nets: Mapping[tuple[ProductKey, str], ContractNet] | None = None,
) -> list[PortfolioTarget]:
    """Assemble final PortfolioTargets with sizing diagnostics (§九.8).

    The risk layer returns one constrained figure per product (keyed by
    str(ProductKey)); it is allocated back to each contract PRO RATA its
    absolute net share so a roll's coexisting contracts split the product
    exposure instead of each receiving the whole of it (B10 fix). Only
    Fill (in backtest) changes holdings; targets record raw requested,
    post-risk exposures and quantized lots with explicit statuses.
    """
    if contract_nets is None:
        _, _, _, contract_nets = net_requests_by_product(accepted, budgets, policy)
    targets: list[PortfolioTarget] = []

    product_requested_abs: dict[ProductKey, float] = {}
    for (product, _contract_id), contract_net in contract_nets.items():
        product_requested_abs[product] = (
            product_requested_abs.get(product, 0.0) + abs(contract_net.net_exposure)
        )

    for (product, contract_id), contract_net in sorted(
        contract_nets.items(), key=lambda item: (str(item[0][0]), item[0][1])
    ):
        post_risk_product = constrained.get(product)
        if post_risk_product is None:
            post_risk_product = constrained.get(str(product))
        if post_risk_product is None:
            post_risk_product = 0.0
        requested_abs = product_requested_abs.get(product, 0.0)
        if requested_abs > 0:
            share = abs(contract_net.net_exposure) / requested_abs
        else:
            share = 0.0
        post_risk_contract = post_risk_product * share
        # Preserve the per-contract sign: the risk layer scales magnitude.
        if contract_net.net_exposure < 0 and post_risk_product > 0:
            post_risk_contract = -post_risk_contract
        representative = contract_net.representative
        quantized = quantize_lots(
            post_risk_contract,
            nav,
            representative.completed_close,
            representative.multiplier,
            representative.lot_step,
        )
        if killed:
            status, reason = "risk_refusal", "kill_switch_close_only"
        elif quantized.reason == "below_minimum_lot":
            status, reason = "below_minimum_lot", "quantized_to_zero"
        elif quantized.target_lots == 0:
            status, reason = "netted_flat", "zero_target_after_netting"
        else:
            status, reason = "active", "sized"
        targets.append(
            PortfolioTarget(
                decision_id=representative.decision_id,
                strategy_ids=contract_net.strategy_ids,
                product=product,
                contract_instance_id=contract_id,
                raw_symbol=contract_net.raw_symbol,
                target_lots=quantized.target_lots,
                status=status,
                reason=reason,
                requested_exposure=contract_net.net_exposure,
                post_risk_exposure=post_risk_contract,
                q_star=quantized.q_star,
                raw_requested_exposure=contract_net.raw_requested_exposure,
            )
        )
    return targets
