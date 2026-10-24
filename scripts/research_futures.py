"""Offline futures research CLI (S3): demo replay, frozen-input signals.

Finalized-plan §十三.2-6 (audit fix round 1: B1/B3/B5/B6/B7): ``--demo`` and
``--input-bundle`` are mutually exclusive and one is required; ``--mode``
selects signals, readiness diagnostics or backtest. Validation supports smoke
and fixed-parameter walk-forward execution with independent accounts.
The frozen-bundle backtest path is INPUT-DRIVEN:
bars and contracts are loaded from the supplied bundle and rules/policy
from the supplied config, with unsupported config keys rejected (exit 2).
The CLI never connects to a database, never reads ``.env``, never imports
TqSdk and never creates accounts or orders. Exit codes: 0 completed,
2 usage/schema/conflict, 3 legitimate request with missing inputs or an
incomplete ledger.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from typing import Mapping

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_BLOCKED = 3

SH = timezone(timedelta(hours=8))

#: Config keys the frozen backtest path understands; anything else is
#: rejected instead of silently ignored (B7).
SUPPORTED_CONFIG_TOP_KEYS = frozenset(
    {"stage", "mode", "validation", "demo", "policy", "session_template",
     "contracts", "roll", "dataset_note"}
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Offline futures strategy library runner (demo replay / frozen-input signals)."
    )
    parser.add_argument("--demo", action="store_true",
                        help="deterministic synthetic full-session demo input")
    parser.add_argument("--input-bundle", default=None,
                        help="path to a frozen futures_bundle_v1 input bundle")
    parser.add_argument("--mode", required=True, choices=("signals", "backtest", "readiness"))
    parser.add_argument("--validation", default=None,
                        choices=("smoke", "walk-forward"),
                        help="backtest validation scope (backtest mode only)")
    parser.add_argument("--config", default=None,
                        help="frozen config JSON for reproduction")
    parser.add_argument("--output", required=True,
                        help="output directory (must not exist)")
    parser.add_argument("--include-session-probe", action="store_true",
                        help=argparse.SUPPRESS)
    parser.add_argument("--include-member-ranks", action="store_true",
                        help=argparse.SUPPRESS)
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Shared replay inputs
# ---------------------------------------------------------------------------


def _bands_for_prices(prices, ratio: str, tick: str):
    tick_dec = Decimal(tick)
    ratio_dec = Decimal(ratio)
    bands: dict[str, tuple[Decimal, Decimal]] = {}
    previous = next(iter(prices.values()))
    for day, price in prices.items():
        upper = (
            previous * (1 + ratio_dec) / tick_dec
        ).to_integral_value(rounding=ROUND_CEILING) * tick_dec
        lower = (
            previous * (1 - ratio_dec) / tick_dec
        ).to_integral_value(rounding=ROUND_FLOOR) * tick_dec
        bands[day.isoformat()] = (upper, lower)
        previous = price
    return bands


class ReplayInputs:
    """Everything a replay needs, independent of where it came from."""

    def __init__(self, *, dates, bars, sessions, rules, instances, products,
                 expiries, roll, universe_products, note, source_manifest_sha256=None):
        self.dates = dates
        self.bars = bars
        self.sessions = sessions
        self.rules = rules
        self.instances = instances
        self.products = products
        self.expiries = expiries
        self.roll = roll  # {"day": iso, "old": iid, "new": iid} | None
        self.universe_products = universe_products
        self.note = note
        self.source_manifest_sha256 = source_manifest_sha256


def _reject_unknown_fields(payload, allowed, label):
    if not isinstance(payload, Mapping):
        raise ValueError(f"{label}_must_be_object")
    extra = set(payload) - set(allowed)
    if extra:
        raise ValueError(f"unsupported_{label}_keys:" + ",".join(sorted(extra)))


def _rules_from_payload(payload: Mapping, series):
    _reject_unknown_fields(payload, (
        "multiplier", "price_tick", "lot_step", "min_open_lots",
        "fee_open_cny_per_lot", "fee_close_cny_per_lot", "fee_close_today_cny_per_lot",
        "margin_long_rate", "margin_short_rate", "limit_band_ratio", "median_volume20_override",
    ), "contract_rules")
    from ak_trading.futures.backtest import ContractRuleSet
    from ak_trading.futures.spec import (
        FeeComponent, FeeSpec, MarginRate, MarginSpec,
    )
    fee = FeeSpec(
        open=FeeComponent(cny_per_lot=Decimal(str(payload["fee_open_cny_per_lot"])), source_id="demo_fixture"),
        close=FeeComponent(cny_per_lot=Decimal(str(payload["fee_close_cny_per_lot"])), source_id="demo_fixture"),
        close_today=FeeComponent(cny_per_lot=Decimal(str(payload["fee_close_today_cny_per_lot"])), source_id="demo_fixture"),
        source_id="demo_fixture",
    )
    margin = MarginSpec(
        long=MarginRate(Decimal(str(payload["margin_long_rate"])), benchmark="close_price"),
        short=MarginRate(Decimal(str(payload["margin_short_rate"])), benchmark="close_price"),
        basis="exchange_only", source_id="demo_fixture",
    )
    return ContractRuleSet(
        multiplier=Decimal(str(payload["multiplier"])),
        price_tick=Decimal(str(payload["price_tick"])),
        lot_step=int(payload["lot_step"]),
        min_open_lots=int(payload["min_open_lots"]),
        fee=fee, margin=margin,
        limit_bands=_bands_for_prices(
            {day: bar.close for day, bar in series.items()},
            str(payload["limit_band_ratio"]), str(payload["price_tick"]),
        ),
        median_volume20=float(payload["median_volume20_override"]),
    )


def _inputs_from_fixture(total_days: int = 630):
    """Deterministic demo fixture -> (inputs, config payload) (B7)."""
    from ak_trading.futures.backtest import SessionSchedule
    from ak_trading.futures.data import ContractInstance
    from ak_trading.futures.io import build_demo_fixture
    from ak_trading.futures.spec import ProductKey

    fixture = build_demo_fixture(total_days)
    dates = [date.fromisoformat(d) for d in fixture["dates"]]
    instances: dict[str, ContractInstance] = {}
    rules: dict[str, object] = {}
    bars: dict[str, dict] = {}
    sessions: dict[str, dict] = {}
    products: dict[str, ProductKey] = {}
    expiries: dict[str, datetime | None] = {}
    rule_payloads: dict[str, dict] = {}
    for instance_id, meta in fixture["contracts"].items():
        product = ProductKey(*str(meta["product"]).split("."))
        expiry = datetime.fromisoformat(str(meta["expiry_at"]))
        instances[instance_id] = ContractInstance(
            instance_id=f"{instance_id}|{expiry.isoformat()}",
            raw_symbol=instance_id, product=product, expiry_at=expiry,
            expired_in_cache=False, listing_at=None,
        )
        products[instance_id] = product
        expiries[instance_id] = expiry
        series = {
            date.fromisoformat(d): b for d, b in fixture["bars"][instance_id].items()
        }
        rule_payload = dict(meta["rules"])
        rule_payloads[instance_id] = rule_payload
        rules[instance_id] = _rules_from_payload(rule_payload, series)
        bars[instance_id] = series
        sessions[instance_id] = {
            date.fromisoformat(iso_day): SessionSchedule(
                date.fromisoformat(iso_day),
                datetime.fromisoformat(open_iso),
                datetime.fromisoformat(close_iso),
            )
            for iso_day, (open_iso, close_iso) in fixture["sessions"][instance_id].items()
            if date.fromisoformat(iso_day) in series
        }
    roll = dict(fixture["roll"]) if fixture.get("roll") else None
    inputs = ReplayInputs(
        dates=dates, bars=bars, sessions=sessions, rules=rules,
        instances=instances, products=products, expiries=expiries,
        roll=roll,
        universe_products=tuple(sorted({p for p in products.values()}, key=str)),
        note="synthetic_demo fixture",
    )
    config = {
        "stage": "S3", "mode": "backtest", "validation": "smoke", "demo": True,
        "dataset_note": "synthetic_demo fixture_valid rules; not approved real evidence",
        "policy": {
            "initial_nav_cny": "400000000",
            "slot_share_base": 1.0 / 18.0,
            "target_daily": 0.10 / (252 ** 0.5),
        },
        "session_template": {"open": "09:00", "close": "15:00"},
        "contracts": {
            iid: {
                "product": str(products[iid]),
                "expiry_at": expiries[iid].isoformat(),
                "rules": rule_payloads[iid],
            }
            for iid in sorted(instances)
        },
        "roll": roll,
    }
    return inputs, config


def _inputs_from_bundle(bundle_dir: Path, config: Mapping) -> ReplayInputs:
    """Load bars/contracts from the bundle and rules from the config (B7)."""
    from ak_trading.futures.backtest import SessionSchedule
    from ak_trading.futures.data import ContractInstance
    from ak_trading.futures.io import read_input_bundle, sha256_file
    from ak_trading.futures.spec import ProductKey

    from dataclasses import replace

    bundle = read_input_bundle(bundle_dir)
    if bundle.manifest.get("dataset_kind") != "synthetic_demo":
        raise ValueError("replay_requires_synthetic_fixture_evidence")
    template = config.get("session_template") or {}
    _reject_unknown_fields(template, ("open", "close"), "session_template")
    if config.get("roll") is not None:
        _reject_unknown_fields(config["roll"], ("day", "new", "old", "product", "reason"), "roll")
    open_time = time.fromisoformat(str(template.get("open", "09:00")))
    close_time = time.fromisoformat(str(template.get("close", "15:00")))
    bars: dict[str, dict] = {}
    sessions: dict[str, dict] = {}
    for bar in bundle.bars:
        day = bar.trading_day or bar.ts.date()
        tz = bar.ts.tzinfo or SH
        base_open = datetime.combine(day, open_time, tzinfo=tz)
        base_close = datetime.combine(day, close_time, tzinfo=tz)
        if bar.trading_day is None or bar.ts != base_open or base_close <= base_open:
            raise ValueError(f"synthetic_session_template_mismatch:{bar.symbol}")
        bars.setdefault(bar.symbol, {})[day] = replace(
            bar, session_open=base_open, session_close=base_close
        )
        sessions.setdefault(bar.symbol, {})[day] = SessionSchedule(day, base_open, base_close)
    contract_meta = {c["raw_symbol"]: c for c in bundle.contracts}
    instances: dict[str, ContractInstance] = {}
    rules: dict[str, object] = {}
    products: dict[str, ProductKey] = {}
    expiries: dict[str, datetime | None] = {}
    config_contracts = config.get("contracts") or {}
    for symbol in sorted(bars):
        meta = contract_meta.get(symbol, {})
        config_entry = config_contracts.get(symbol)
        if config_entry is None:
            raise ValueError(f"config_missing_contract_rules:{symbol}")
        _reject_unknown_fields(config_entry, ("product", "expiry_at", "rules"), "contract")
        product = ProductKey(
            *str(config_entry.get("product") or meta.get("product")).split(".")
        )
        expiry_raw = config_entry.get("expiry_at") or meta.get("expiry_at")
        expiry = datetime.fromisoformat(str(expiry_raw)) if expiry_raw else None
        instances[symbol] = ContractInstance(
            instance_id=f"{symbol}|{expiry.isoformat() if expiry else 'unknown'}",
            raw_symbol=symbol, product=product, expiry_at=expiry,
            expired_in_cache=False, listing_at=None,
        )
        products[symbol] = product
        expiries[symbol] = expiry
        rules[symbol] = _rules_from_payload(config_entry["rules"], bars[symbol])
    roll = config.get("roll") or None
    return ReplayInputs(
        dates=sorted({day for series in bars.values() for day in series}),
        bars=bars, sessions=sessions, rules=rules, instances=instances,
        products=products, expiries=expiries, roll=roll,
        universe_products=tuple(sorted({p for p in products.values()}, key=str)),
        note="frozen bundle input + frozen config",
        source_manifest_sha256=sha256_file(bundle_dir / "manifest.json"),
    )


# ---------------------------------------------------------------------------
# Compositions
# ---------------------------------------------------------------------------


def _active_contract(inputs, product, day):
    if inputs.roll and str(product) == inputs.roll.get("product"):
        return (
            inputs.roll["new"]
            if day >= date.fromisoformat(str(inputs.roll["day"]))
            else inputs.roll["old"]
        )
    for iid, p in inputs.products.items():
        if p == product:
            return iid
    return None


def _compose_fixed_risk(inputs, policy, registry, scenario, family_filter=None,
                        *, scored_days=None, warmup_days=()):
    """Causal registry -> portfolio -> engine composition with signal trace."""
    from ak_trading.futures.backtest import (
        LedgerEngine, OpenIntent, ReplayPlan, run_replay,
    )
    from ak_trading.futures.portfolio import (
        StrategyRequest, admission_review, build_portfolio_targets,
        build_slot_budgets, constrain_requests, net_requests_by_product,
    )
    from ak_trading.futures.spec import (
        ClockStatus, ContractMetadata, ProductKey, StrategyContext,
    )
    from ak_trading.futures.structures import build_structure_frame

    dates = tuple(inputs.dates if scored_days is None else scored_days)
    warmup_days = tuple(warmup_days)
    if not dates:
        raise ValueError("scored_days_must_not_be_empty")
    bars, sessions = inputs.bars, inputs.sessions
    final_day = dates[-1]
    engine = LedgerEngine(
        initial_cash=policy.initial_nav_cny, policy=policy, scenario=scenario,
        contracts={
            iid: (inputs.products[iid], iid, inputs.rules[iid]) for iid in inputs.rules
        },
    )
    history: dict[str, list] = {i: [] for i in bars}
    strategy_states: dict[tuple[str, str], object] = {}
    last_active: dict = {}
    signal_rows: list[dict[str, object]] = []
    active_registry = registry
    if family_filter is not None:
        active_registry = {
            sid: s for sid, s in registry.items() if s.spec.family in family_filter
        }

    def provider(engine_state: LedgerEngine, day: date) -> list:
        trace_start = len(signal_rows)
        intents: list[OpenIntent] = []
        for iid, series in sorted(bars.items()):
            bar = series.get(day)
            if bar is not None:
                history[iid].append(bar)
        known_nav = (
            engine_state.marks[-1].equity if engine_state.marks else policy.initial_nav_cny
        )
        for product in inputs.universe_products:
            active = _active_contract(inputs, product, day)
            previous_active = last_active.get(product)
            if previous_active and previous_active != active:
                if engine_state.ledgers[previous_active].lots != 0:
                    intents.append(OpenIntent(
                        decision_id=f"{day.isoformat()}|roll_exit|{previous_active}",
                        product=product, contract_instance_id=previous_active,
                        raw_symbol=previous_active, target_lots=0, kind="close", is_roll=True,
                    ))
            last_active[product] = active
            if active is None or not history.get(active):
                continue
            active_bars = history[active]
            requests = []
            for strategy_id, strategy in sorted(active_registry.items()):
                pair_candidates = ()
                pair_bars: dict[str, tuple] = {}
                if strategy_id == "term_structure_carry":
                    near_expiry = inputs.expiries[active]
                    for other_id, other_expiry in inputs.expiries.items():
                        if other_id == active or inputs.products[other_id] != product:
                            continue
                        if other_expiry is None or near_expiry is None:
                            continue
                        gap = (other_expiry - near_expiry).total_seconds() / 86400.0
                        if policy.pair_gap_days_min <= gap <= policy.pair_gap_days_max and other_expiry > near_expiry:
                            pair_candidates += (ContractMetadata(
                                contract_instance_id=inputs.instances[other_id].instance_id,
                                raw_symbol=other_id, product=product,
                                expiry_at=other_expiry,
                            ),)
                            pair_bars[other_id] = tuple(history.get(other_id, []))
                frame = build_structure_frame(active_bars)
                context = StrategyContext(
                    strategy_id=strategy_id, product=product,
                    contract_instance_id=inputs.instances[active].instance_id,
                    raw_symbol=active,
                    decision_at=active_bars[-1].available_at,
                    clock_status=ClockStatus.VERIFIED,
                    completed_bar_id=f"{active}:{active_bars[-1].datetime_nano}",
                    bars=tuple(active_bars), structure=frame, factors={},
                    expiry_at=inputs.expiries[active],
                    pair_candidates=pair_candidates, pair_bars=pair_bars,
                )
                previous_state = strategy_states.get((strategy_id, active))
                if warmup_days and day == warmup_days[-1] and previous_state is not None:
                    from dataclasses import replace
                    previous_state = replace(previous_state, direction=0, signal_age=None, episode_id=None)
                result = strategy.evaluate(context, previous_state)
                strategy_states[(strategy_id, active)] = result.next_state
                requests.append(StrategyRequest(
                    strategy_id=strategy_id, family=strategy.spec.family,
                    product=product, contract_instance_id=active, raw_symbol=active,
                    signal_status=result.status,
                    direction=(
                        result.target_position if result.target_position is not None else 0
                    ),
                    observed_daily_vol=frame.daily_vol20,
                    completed_close=active_bars[-1].close,
                    multiplier=inputs.rules[active].multiplier,
                    lot_step=inputs.rules[active].lot_step,
                    has_position=engine_state.ledgers[active].lots != 0,
                    decision_id=f"{day.isoformat()}|{strategy_id}",
                ))
            budgets = build_slot_budgets(requests, policy, products=inputs.universe_products)
            admission = admission_review(requests, budgets, policy)
            # Signal trace (B6): one row per strategy/product decision.
            for request in requests:
                marker = next(
                    (
                        t for t in admission.slot_unallocated + admission.close_intents
                        if request.strategy_id in (t.strategy_ids or ())
                        and t.product == product
                    ),
                    None,
                )
                signal_rows.append({
                    "strategy_id": request.strategy_id,
                    "family": request.family,
                    "exchange": str(product).split(".")[0],
                    "product": str(product),
                    "contract_instance_id": active,
                    "raw_symbol": active,
                    "decision_at": day.isoformat(),
                    "clock_status": "verified",
                    "completed_bar_id": f"{active}:{active_bars[-1].datetime_nano}",
                    "target_position": str(request.direction),
                    "status": marker.status if marker else "admitted",
                    "reason": marker.reason if marker else "accepted_for_sizing",
                    "diagnostics": json.dumps({
                        "signal_status": request.signal_status,
                        "observed_daily_vol": request.observed_daily_vol,
                        "decision_id": request.decision_id,
                    }, sort_keys=True),
                })
            if not admission.accepted:
                continue
            net, _, _, contract_nets = net_requests_by_product(
                admission.accepted, budgets, policy
            )
            volatility = {r.product: r.observed_daily_vol for r in admission.accepted}
            constrained = constrain_requests(
                net, volatility, policy,
                (ProductKey("SHFE", "RB"), ProductKey("INE", "SC"), ProductKey("DCE", "M"),
                 ProductKey("CZCE", "TA"), ProductKey("CFFEX", "IF"), ProductKey("GFEX", "SI")),
                killed=engine_state.killed or engine_state.insolvent,
            )
            if constrained.refused:
                signal_rows.append({
                    "strategy_id": "portfolio",
                    "family": "portfolio_target",
                    "exchange": str(product).split(".")[0],
                    "product": str(product),
                    "contract_instance_id": active,
                    "raw_symbol": active,
                    "decision_at": day.isoformat(),
                    "clock_status": "verified",
                    "completed_bar_id": f"{active}:{active_bars[-1].datetime_nano}",
                    "target_position": "",
                    "status": "risk_refusal",
                    "reason": "risk_layer_invalid_input_batch_refused",
                    "diagnostics": "{}",
                })
                continue
            targets = build_portfolio_targets(
                admission.accepted, budgets, policy, constrained.targets, known_nav,
                killed=engine_state.killed or engine_state.insolvent,
                contract_nets=contract_nets,
            )
            for target in targets:
                signal_rows.append({
                    "strategy_id": "+".join(target.strategy_ids) or "portfolio",
                    "family": "portfolio_target",
                    "exchange": str(target.product).split(".")[0],
                    "product": str(target.product),
                    "contract_instance_id": target.contract_instance_id,
                    "raw_symbol": target.raw_symbol,
                    "decision_at": day.isoformat(),
                    "clock_status": "verified",
                    "completed_bar_id": f"{active}:{active_bars[-1].datetime_nano}",
                    "target_position": "",
                    "status": target.status,
                    "reason": target.reason,
                    "diagnostics": json.dumps({
                        "requested_exposure": target.requested_exposure,
                        "post_risk_exposure": target.post_risk_exposure,
                        "q_star": str(target.q_star) if target.q_star is not None else None,
                        "target_lots": target.target_lots,
                        "decision_id": target.decision_id,
                    }, sort_keys=True),
                })
                held = engine_state.ledgers[active].lots
                if target.target_lots == 0 and held != 0:
                    kind = "close"
                elif target.status == "active":
                    kind = "open"
                else:
                    continue
                intents.append(OpenIntent(
                    decision_id=target.decision_id or f"{day.isoformat()}|{active}",
                    product=product, contract_instance_id=active, raw_symbol=active,
                    target_lots=target.target_lots, kind=kind,
                    used_bar_id=f"{active}:{active_bars[-1].datetime_nano}",
                    requires_full_exit_of=(
                        previous_active if previous_active and previous_active != active else None
                    ),
                ))
        if day < dates[0] and (not warmup_days or day != warmup_days[-1]):
            del signal_rows[trace_start:]
        return intents

    plan = ReplayPlan(
        days=dates, sessions=sessions, bars=bars, decisions={},
        terminal_days={iid: final_day for iid in sessions},
    )
    outcome = run_replay(engine, plan, decision_provider=provider, warmup_days=warmup_days,
                         enforce_availability=scored_days is not None)
    return engine, outcome, signal_rows


def _compose_long_only(inputs, policy, scenario, *, scored_days=None, warmup_days=()):
    """§十一.7b long_only_rolled_contract baseline (B1).

    Per eligible product: initial notional budget = initial_NAV*(0.50/6);
    first entry uses the first legal decision's completed close; the
    position then holds real lots with only forced risk reduction, roll and
    terminal exits — no daily rebalance. Rolls close the old leg and open
    the successor under the §十.7 linkage (new leg only when the old leg
    fully exited; partial completion lands in cash).
    """
    from ak_trading.futures.backtest import (
        LedgerEngine, OpenIntent, ReplayPlan, run_replay,
    )
    from ak_trading.futures.structures import build_structure_frame

    dates = tuple(inputs.dates if scored_days is None else scored_days)
    warmup_days = tuple(warmup_days)
    if not dates:
        raise ValueError("scored_days_must_not_be_empty")
    bars, sessions = inputs.bars, inputs.sessions
    final_day = dates[-1]
    budget_per_product = policy.initial_nav_cny * Decimal("0.5") / Decimal("6")
    engine = LedgerEngine(
        initial_cash=policy.initial_nav_cny, policy=policy, scenario=scenario,
        contracts={
            iid: (inputs.products[iid], iid, inputs.rules[iid]) for iid in inputs.rules
        },
    )
    history: dict[str, list] = {i: [] for i in bars}
    entered: dict = {}
    last_active: dict = {}
    signal_rows: list[dict[str, object]] = []

    def provider(engine_state: LedgerEngine, day: date) -> list:
        trace_start = len(signal_rows)
        intents: list[OpenIntent] = []
        for iid, series in sorted(bars.items()):
            bar = series.get(day)
            if bar is not None:
                history[iid].append(bar)
        if day < dates[0] and day != warmup_days[-1]:
            return []
        for product in inputs.universe_products:
            active = _active_contract(inputs, product, day)
            previous_active = last_active.get(product)
            if previous_active and previous_active != active:
                held = engine_state.ledgers[previous_active].lots
                if held != 0:
                    old_close = (
                        history[previous_active][-1].close
                        if history[previous_active] else None
                    )
                    new_close = (
                        history[active][-1].close if history.get(active) else None
                    )
                    intents.append(OpenIntent(
                        decision_id=f"{day.isoformat()}|long_only_roll_exit|{previous_active}",
                        product=product, contract_instance_id=previous_active,
                        raw_symbol=previous_active, target_lots=0, kind="close", is_roll=True,
                    ))
                    if old_close and new_close:
                        old_notional = (
                            Decimal(abs(held)) * old_close
                            * inputs.rules[previous_active].multiplier
                        )
                        target_lots = int(
                            old_notional / (new_close * inputs.rules[active].multiplier)
                        )
                        intents.append(OpenIntent(
                            decision_id=f"{day.isoformat()}|long_only_roll_open|{active}",
                            product=product, contract_instance_id=active,
                            raw_symbol=active, target_lots=target_lots, kind="open",
                            requires_full_exit_of=previous_active,
                        ))
                        signal_rows.append({
                            "strategy_id": "long_only_rolled_contract",
                            "family": "long_only_baseline",
                            "exchange": str(product).split(".")[0],
                            "product": str(product),
                            "contract_instance_id": active,
                            "raw_symbol": active,
                            "decision_at": day.isoformat(),
                            "clock_status": "verified",
                            "completed_bar_id": f"{active}:{history[active][-1].datetime_nano}",
                            "target_position": "1",
                            "status": "roll",
                            "reason": "roll_notional_transfer_only_reduces",
                            "diagnostics": json.dumps({"target_lots": target_lots},
                                                      sort_keys=True),
                        })
            last_active[product] = active
            if active is None or entered.get(product) or not history.get(active):
                continue
            frame = build_structure_frame(history[active])
            close = history[active][-1].close
            if frame.close is None or close is None or close <= 0:
                continue
            entered[product] = True
            target_lots = int(
                budget_per_product / (close * inputs.rules[active].multiplier)
            )
            if target_lots <= 0:
                continue
            signal_rows.append({
                "strategy_id": "long_only_rolled_contract",
                "family": "long_only_baseline",
                "exchange": str(product).split(".")[0],
                "product": str(product),
                "contract_instance_id": active,
                "raw_symbol": active,
                "decision_at": day.isoformat(),
                "clock_status": "verified",
                "completed_bar_id": f"{active}:{history[active][-1].datetime_nano}",
                "target_position": "1",
                "status": "entry",
                "reason": "first_legal_decision_close_budget_entry",
                "diagnostics": json.dumps({
                    "budget_per_product": str(budget_per_product),
                    "target_lots": target_lots,
                }, sort_keys=True),
            })
            intents.append(OpenIntent(
                decision_id=f"{day.isoformat()}|long_only_entry|{active}",
                product=product, contract_instance_id=active, raw_symbol=active,
                target_lots=target_lots, kind="open",
                used_bar_id=f"{active}:{history[active][-1].datetime_nano}",
            ))
        if day < dates[0] and (not warmup_days or day != warmup_days[-1]):
            del signal_rows[trace_start:]
        return intents

    plan = ReplayPlan(
        days=dates, sessions=sessions, bars=bars, decisions={},
        terminal_days={iid: final_day for iid in sessions},
    )
    outcome = run_replay(engine, plan, decision_provider=provider, warmup_days=warmup_days,
                         enforce_availability=scored_days is not None)
    return engine, outcome, signal_rows


def _summarize_outcome(outcome, policy) -> dict:
    from ak_trading.futures.io import equity_rows, fills_rows, trades_rows
    from ak_trading.futures.validation import summarize_run

    marks = outcome.marks
    summary = dict(summarize_run(
        days=[m.trading_day for m in marks],
        nav_close_by_day=[m.equity for m in marks],
        initial_nav=policy.initial_nav_cny,
        turnover_notional=outcome.turnover_notional,
        trades=outcome.trades,
        status=outcome.status,
        marks=marks,
        blocked=[dict(entry) for entry in outcome.blocked],
        fees_cny=outcome.reconciliation["fees"],
        slippage_cny=outcome.reconciliation["slippage"],
        final_unrealized_pnl=outcome.final_unrealized_pnl,
        total_gross_pnl=outcome.reconciliation["cumulative_gross"],
        roll_costs_cny=outcome.roll_costs_cny,
        open_trade_count=outcome.open_trade_count,
    ))
    summary["reconciliation"] = {
        key: (str(value) if isinstance(value, Decimal) else value)
        for key, value in dict(outcome.reconciliation).items()
    }
    summary["open_positions"] = dict(outcome.open_positions)
    summary["incomplete_reasons"] = {
        k: list(v) for k, v in outcome.incomplete_reasons.items()
    }
    summary["_ledger_rows"] = {
        "fills_rows": fills_rows(outcome.fills),
        "equity_rows": equity_rows(marks),
        "trades_rows": trades_rows(outcome.trades),
    }
    return summary


def _run_scenario_ledgers(inputs, policy, registry, *, scored_days=None, warmup_days=()):
    """Compose every portfolio x scenario; returns ledgers + signal traces."""
    portfolios = [
        ("futures_fixed_risk", ("base", "stress"), "fixed", None),
        ("family_standalone_trend", ("base",), "fixed", ("trend",)),
        ("family_standalone_mean_reversion", ("base",), "fixed", ("mean_reversion",)),
        ("family_standalone_carry", ("base",), "fixed", ("term_structure_carry",)),
        ("long_only_rolled_contract", ("base", "stress"), "long_only", None),
    ]
    ledger_payloads: dict[str, dict[str, dict]] = {}
    signals_by_scenario: dict[str, list] = {}
    top_reports: dict[str, object] = {}
    for portfolio_id, scenarios, kind, family_filter in portfolios:
        for scenario in scenarios:
            if kind == "long_only":
                _engine, outcome, rows = _compose_long_only(
                    inputs, policy, scenario, scored_days=scored_days, warmup_days=warmup_days
                )
            else:
                _engine, outcome, rows = _compose_fixed_risk(
                    inputs, policy, registry, scenario, family_filter,
                    scored_days=scored_days, warmup_days=warmup_days,
                )
            summary = _summarize_outcome(outcome, policy)
            rows_payload = summary.pop("_ledger_rows")
            ledger_payloads.setdefault(scenario, {})[portfolio_id] = {
                **rows_payload, "report": summary,
            }
            signals_by_scenario.setdefault(scenario, []).extend(rows)
            if portfolio_id == "futures_fixed_risk" and scenario == "base":
                top_reports["futures_fixed_risk_base"] = summary
            if portfolio_id == "long_only_rolled_contract" and scenario == "base":
                top_reports["long_only_base"] = summary
    return ledger_payloads, signals_by_scenario, top_reports


def run_backtests(output: Path, *, inputs, config: Mapping, created_at: str,
                  validation: str, policy) -> dict:
    """Shared publish path for --demo and frozen-bundle replays (B7)."""
    if validation == "walk-forward":
        return run_walk_forward_backtests(
            output, inputs=inputs, config=config, created_at=created_at, policy=policy
        )
    if validation != "smoke":
        raise ValueError("unknown_validation_mode")
    from ak_trading.futures.data import day_sequence_digest
    from ak_trading.futures.io import (
        BARS_CSV_COLUMNS, bars_rows_from_bars, equity_rows, publish_backtest_bundle,
        publish_input_bundle, write_canonical_csv,
    )
    from ak_trading.futures.strategies import default_registry
    from ak_trading.futures.validation import build_folds, build_splits_document

    registry = default_registry(policy)
    dates = inputs.dates
    final_day = dates[-1]
    folds = build_folds(dates)
    splits_doc = build_splits_document(
        dates, folds,
        universe=[str(p) for p in inputs.universe_products],
        span=(dates[0].isoformat(), final_day.isoformat()),
        construction_sha256=day_sequence_digest(dates),
        terminal_schedule={iid: final_day.isoformat() for iid in sorted(inputs.sessions)},
    )

    all_bars = [bar for series in inputs.bars.values() for bar in series.values()]
    all_bars.sort(key=lambda b: (b.symbol, b.datetime_nano))
    bars_rows = bars_rows_from_bars(all_bars)
    import tempfile as _tempfile

    with _tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp_csv:
        tmp_path_csv = Path(tmp_csv.name)
    try:
        input_bars_sha = write_canonical_csv(tmp_path_csv, BARS_CSV_COLUMNS, bars_rows)
    finally:
        tmp_path_csv.unlink(missing_ok=True)

    ledger_payloads, signals_by_scenario, top_reports = _run_scenario_ledgers(
        inputs, policy, registry
    )

    # cash baseline over the same D (§十一.7a).
    from ak_trading.futures.backtest import EquityMark

    cash_marks = [
        EquityMark(
            trading_day=day, cash=policy.initial_nav_cny,
            equity=policy.initial_nav_cny, required_margin=Decimal("0"),
            status="active", unrealized_pnl=Decimal("0"),
            gross_notional=Decimal("0"), invested=False,
        )
        for day in dates
    ]
    from ak_trading.futures.validation import summarize_run

    cash_rows = equity_rows(cash_marks)
    cash_summary = dict(summarize_run(
        days=dates, nav_close_by_day=[m.equity for m in cash_marks],
        initial_nav=policy.initial_nav_cny, turnover_notional=Decimal("0"),
        trades=[], status="complete", marks=cash_marks, blocked=[],
        fees_cny=Decimal("0"), slippage_cny=Decimal("0"),
        roll_costs_cny=Decimal("0"), final_unrealized_pnl=Decimal("0"),
        total_gross_pnl=Decimal("0"), open_trade_count=0,
    ))
    cash_summary["note"] = "zero-interest research cash; not evidence of trading readiness"
    ledger_payloads["base"]["cash_baseline"] = {
        "fills_rows": [], "equity_rows": cash_rows, "trades_rows": [],
        "report": cash_summary,
    }

    signals_rows = [
        {**row, "scenario": scenario}
        for scenario in ("base", "stress")
        for row in signals_by_scenario.get(scenario, [])
    ]
    rolls_rows = []
    if inputs.roll:
        rolls_rows.append({
            "decision_at": inputs.roll["day"],
            "product": inputs.roll["product"],
            "old_contract_instance_id": inputs.roll["old"],
            "new_contract_instance_id": inputs.roll["new"],
            "reason": "past_liquidity_forward_roll_fixture",
            "decision_id": "demo_roll_schedule",
        })

    report = {
        "dataset_kind": "synthetic_demo",
        "validation": validation,
        "days": len(dates),
        "folds_status": folds.status,
        "folds_retained": list(folds.retained_k) if folds.status == "available" else [],
        "portfolios": sorted(ledger_payloads["base"]),
        "futures_fixed_risk_base": top_reports.get("futures_fixed_risk_base"),
        "long_only_rolled_contract_base": top_reports.get("long_only_base"),
        "input_source": str(config.get("dataset_note", "synthetic_demo")),
        "walk_forward_note": (
            "fold construction and retention verified in tests; no per-fold OOS execution; "
            "the replayed window is the full demo span (smoke)"
        ),
    }
    manifest = publish_backtest_bundle(
        output,
        created_at=created_at,
        config=dict(config),
        report=report,
        availability={"member_factor": {"status": "not_requested",
                                         "reason": "deferred_no_default_strategy_dependency"}},
        splits=splits_doc,
        signals_rows=signals_rows,
        rolls_rows=rolls_rows,
        input_manifest_sha256=input_bars_sha,
        scenario_ledgers=ledger_payloads,
    )

    input_dir = output / "input"
    publish_input_bundle(
        input_dir,
        dataset_kind="synthetic_demo",
        created_at=created_at,
        bars_rows=bars_rows,
        contracts=[
            {
                "instance_id": inputs.instances[iid].instance_id,
                "raw_symbol": iid,
                "product": str(inputs.products[iid]),
                "expiry_at": (
                    inputs.expiries[iid].isoformat() if inputs.expiries[iid] else None
                ),
                "identity_status": "synthetic_fixture",
            }
            for iid in sorted(inputs.instances)
        ],
        metadata={
            "graded_sources": {"synthetic_demo": {"grade": "fixture_valid",
                                              "dataset_kind": "synthetic_demo"}},
            "revision_history_unavailable": False,
        },
        availability={"member_factor": {"status": "not_requested",
                                         "reason": "deferred_no_default_strategy_dependency"}},
        request_info={"demo": True, "days": len(dates)},
        source_hashes={"demo_fixture": "demo_fixture_v1"},
        read_only_transaction={"executed": False, "reason": "synthetic_demo_no_database"},
        clock_policy={"calendar_rule_version": "calendar_union_v1",
                      "d_status": "available_synthetic_fixture"},
    )
    legacy_dir = output / "legacy_reference"
    legacy_dir.mkdir()
    legacy_report = _legacy_reference_report(inputs.bars, dates)
    (legacy_dir / "report.json").write_bytes(
        json.dumps(legacy_report, sort_keys=True, allow_nan=False,
                   ensure_ascii=False, indent=1).encode("utf-8") + b"\n"
    )
    return {
        "status": "published",
        "output": str(output),
        "manifest_status": manifest.get("status"),
        "portfolios": report["portfolios"],
        "folds_status": folds.status,
    }


def _publish_walk_input(output, inputs, created_at):
    from ak_trading.futures.io import bars_rows_from_bars, publish_input_bundle

    bars = [bar for series in inputs.bars.values() for day, bar in series.items()
            if day in set(inputs.dates)]
    bars.sort(key=lambda bar: (bar.symbol, bar.datetime_nano))
    return publish_input_bundle(
        output, dataset_kind="synthetic_demo", created_at=created_at,
        bars_rows=bars_rows_from_bars(bars),
        contracts=[{
            "instance_id": inputs.instances[iid].instance_id,
            "raw_symbol": iid, "product": str(inputs.products[iid]),
            "expiry_at": inputs.expiries[iid].isoformat() if inputs.expiries[iid] else None,
            "identity_status": "synthetic_fixture",
        } for iid in sorted(inputs.instances)],
        metadata={"graded_sources": {"synthetic_demo": {"grade": "fixture_valid",
                  "dataset_kind": "synthetic_demo"}}, "revision_history_unavailable": False},
        availability={"member_factor": {"status": "not_requested"}},
        request_info={"demo": True, "days": len(inputs.dates)},
        source_hashes={"demo_fixture": "demo_fixture_v1"},
        read_only_transaction={"executed": False, "reason": "synthetic_demo_no_database"},
        clock_policy={"calendar_rule_version": "calendar_union_v1",
                      "d_status": "available_synthetic_fixture"},
    )


def _policy_from_config(config):
    from dataclasses import fields
    from ak_trading.futures.spec import ResearchPolicy
    import math

    raw = config.get("policy")
    if not isinstance(raw, dict):
        raise ValueError("policy_must_be_an_object")
    allowed = {item.name for item in fields(ResearchPolicy)}
    if set(raw) - allowed:
        raise ValueError("unsupported_policy_keys:" + ",".join(sorted(set(raw) - allowed)))
    defaults = ResearchPolicy()
    values = {}
    for name, value in raw.items():
        default = getattr(defaults, name)
        if isinstance(value, bool):
            raise ValueError(f"invalid_policy_value:{name}")
        if isinstance(default, Decimal):
            number = Decimal(str(value))
            if not number.is_finite() or number <= 0:
                raise ValueError(f"invalid_policy_value:{name}")
        elif isinstance(default, int):
            if not isinstance(value, int) or value <= 0:
                raise ValueError(f"invalid_policy_value:{name}")
            number = value
        else:
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"invalid_policy_value:{name}")
            number = float(value)
        values[name] = number
    policy = ResearchPolicy(**values)
    for name in ("slot_share_base", "instrument_cap", "gross_cap", "cluster_cap",
                 "max_margin_ratio", "drawdown_kill", "participation_cap"):
        if getattr(policy, name) > 1:
            raise ValueError(f"policy_ratio_exceeds_one:{name}")
    if not policy.pair_gap_days_min <= policy.pair_gap_days_target <= policy.pair_gap_days_max:
        raise ValueError("invalid_pair_gap_order")
    return policy


def _policy_payload(policy):
    from dataclasses import fields
    return {item.name: str(getattr(policy, item.name)) if isinstance(getattr(policy, item.name), Decimal)
            else getattr(policy, item.name) for item in fields(policy)}


def _cash_window(dates, policy):
    from ak_trading.futures.backtest import EquityMark
    from ak_trading.futures.io import equity_rows
    from ak_trading.futures.validation import summarize_run

    zero = Decimal("0")
    marks = [EquityMark(trading_day=day, cash=policy.initial_nav_cny,
                       equity=policy.initial_nav_cny, required_margin=zero,
                       status="active", unrealized_pnl=zero, gross_notional=zero,
                       invested=False) for day in dates]
    report = dict(summarize_run(
        days=dates, nav_close_by_day=[mark.equity for mark in marks],
        initial_nav=policy.initial_nav_cny, turnover_notional=zero,
        trades=[], status="complete", marks=marks, blocked=[], fees_cny=zero,
        slippage_cny=zero, roll_costs_cny=zero, final_unrealized_pnl=zero,
        total_gross_pnl=zero, open_trade_count=0,
    ))
    return {"fills_rows": [], "equity_rows": equity_rows(marks),
            "trades_rows": [], "report": report}


def run_walk_forward_backtests(output: Path, *, inputs, config, created_at, policy):
    from ak_trading.futures.io import (
        assert_output_directory_is_new, canonical_json_bytes, publish_backtest_bundle,
        sha256_bytes, sha256_file,
    )
    from ak_trading.futures.data import day_sequence_digest
    from ak_trading.futures.strategies import default_registry
    from ak_trading.futures.validation import build_folds, build_splits_document
    from ak_trading.futures.walkforward import run_walk_forward, aggregate_oos_ledgers

    folds = build_folds(inputs.dates)
    if folds.status != "available":
        return {"status": "blocked", "reason": "insufficient_history", "detail": folds.reason,
                "validation": "walk-forward"}
    assert_output_directory_is_new(output)
    output.mkdir(parents=True)
    frozen_config = {**dict(config), "validation": "walk-forward"}
    (output / "config.json").write_bytes(canonical_json_bytes(frozen_config))
    _publish_walk_input(output / "input", inputs, created_at)
    input_sha = sha256_file(output / "input/manifest.json")
    registry = default_registry(policy)
    parameters = {sid: dict(strategy.spec.parameters) for sid, strategy in sorted(registry.items())}
    parameter_sha = sha256_bytes(canonical_json_bytes({"strategies": parameters, "policy": _policy_payload(policy)}))
    aggregate_records = {}
    window_reports = []

    def replay_window(window):
        ledgers, signals, _ = _run_scenario_ledgers(
            inputs, policy, registry, scored_days=window.days, warmup_days=window.warmup_days
        )
        ledgers.setdefault("base", {})["cash_baseline"] = _cash_window(window.days, policy)
        statuses = {scenario: {portfolio: ledger["report"]["status"]
                     for portfolio, ledger in sorted(portfolios.items())}
                    for scenario, portfolios in sorted(ledgers.items())}
        execution_status = "complete" if all(
            status == "complete" for portfolios in statuses.values() for status in portfolios.values()
        ) else "incomplete"
        window_report = {
            "window_id": window.window_id, "kind": window.kind,
            "fold_index": window.fold_index,
            "scored_days": [day.isoformat() for day in window.days],
            "warmup_days": [day.isoformat() for day in window.warmup_days],
            "initial_nav": str(policy.initial_nav_cny),
            "execution_status": execution_status, "portfolios": statuses,
            "parameter_sha256": parameter_sha, "parameter_selection": "fixed_no_optimization",
            "account_reset": True, "warmup_trades": 0,
            "artifact_path": window.window_id,
        }
        identities = {iid: instance.instance_id for iid, instance in inputs.instances.items()}
        for portfolios in ledgers.values():
            for ledger in portfolios.values():
                for key in ("fills_rows", "trades_rows"):
                    for row in ledger[key]:
                        row["contract_instance_id"] = identities.get(row["contract_instance_id"], row["contract_instance_id"])
                report = ledger["report"]
                for key in ("open_positions", "incomplete_reasons"):
                    if key in report:
                        report[key] = {identities.get(iid, iid): value for iid, value in report[key].items()}
        rows = [{**row, "scenario": scenario,
                 "contract_instance_id": identities.get(row["contract_instance_id"], row["contract_instance_id"])}
                for scenario, seq in sorted(signals.items()) for row in seq]
        roll_rows = []
        if inputs.roll:
            roll_day = date.fromisoformat(inputs.roll["day"])
            decision_days = set(window.days[:-1]) | set(window.warmup_days[-1:])
            if roll_day in decision_days:
                roll_rows.append({
                    "decision_at": inputs.roll["day"], "product": inputs.roll["product"],
                    "old_contract_instance_id": identities[inputs.roll["old"]],
                    "new_contract_instance_id": identities[inputs.roll["new"]],
                    "reason": "frozen_roll_schedule_decision_not_fill_confirmation",
                    "decision_id": f"{inputs.roll['day']}|roll_exit|{inputs.roll['old']}",
                })
        publish_backtest_bundle(
            output / window.window_id, created_at=created_at,
            config=frozen_config, report=window_report,
            availability={"dataset_kind": "synthetic_demo", "real_strict_execution": "not_requested"},
            splits={"scored_days": window_report["scored_days"], "warmup_days": window_report["warmup_days"]},
            signals_rows=rows, rolls_rows=roll_rows, input_manifest_sha256=input_sha,
            scenario_ledgers=ledgers,
        )
        if window.kind == "development_oos":
            for scenario, portfolios in sorted(ledgers.items()):
                for portfolio, ledger in sorted(portfolios.items()):
                    aggregate_records.setdefault(scenario, {}).setdefault(portfolio, []).append({
                        "window_id": window.window_id, "status": ledger["report"]["status"],
                        "initial_nav": policy.initial_nav_cny,
                        "nav_close": [row["equity"] for row in ledger["equity_rows"]],
                    })
        window_reports.append(window_report)
        return window_report

    execution = run_walk_forward(inputs.dates, replay_window)
    aggregate = {scenario: {portfolio: aggregate_oos_ledgers(records)
                 for portfolio, records in sorted(portfolios.items())}
                 for scenario, portfolios in sorted(aggregate_records.items())}
    holdout = next(item for item in window_reports if item["kind"] == "final_holdout")
    scored = [item for item in window_reports if item["kind"] != "development_train"]
    report = {
        "schema_version": "futures_walk_forward_v1", "status": "published",
        "validation": "walk-forward", "dataset_kind": "synthetic_demo",
        "parameter_selection": "fixed_no_optimization", "strategy_parameters": parameters,
        "parameter_sha256": parameter_sha, "effective_policy": _policy_payload(policy),
        "windows": window_reports,
        "execution_status": "complete" if all(w["execution_status"] == "complete" for w in scored) else "incomplete",
        "development_oos_aggregate": aggregate,
        "final_holdout": {"window_id": "final_holdout", "consumed": True,
                          "excluded_from_development_aggregate": True,
                          "execution_status": holdout["execution_status"]},
        "accounting": "independent_reset_accounts_not_continuous_cny",
        "real_performance_status": "unavailable_synthetic_validation_only",
        "callback_execution_status": execution.status,
    }
    splits = build_splits_document(
        inputs.dates, folds, universe=[str(p) for p in inputs.universe_products],
        span=(inputs.dates[0].isoformat(), inputs.dates[-1].isoformat()),
        construction_sha256=day_sequence_digest(inputs.dates),
    )
    (output / "splits.json").write_bytes(canonical_json_bytes(splits))
    (output / "report.json").write_bytes(canonical_json_bytes(report))
    hashes = {path.relative_to(output).as_posix(): sha256_file(path)
              for path in sorted(output.rglob("*")) if path.is_file()}
    source_manifest_sha = inputs.source_manifest_sha256 or input_sha
    (output / "manifest.json").write_bytes(canonical_json_bytes({
        "schema_version": "futures_walk_forward_v1", "status": "complete",
        "dataset_kind": "synthetic_demo", "created_at": created_at,
        "content_hashes": hashes, "input_manifest_sha256": input_sha,
        "source_input_manifest_sha256": source_manifest_sha,
        "parameter_sha256": parameter_sha, "holdout_consumed": True,
        "completion_basis": "artifact_publication_only_see_report_execution_status",
    }))
    return {"status": "published", "validation": "walk-forward", "output": str(output),
            "windows": len(window_reports), "execution_status": report["execution_status"]}


def run_readiness(output: Path, bundle_path: Path):
    from ak_trading.futures.io import read_input_bundle, assert_output_directory_is_new, canonical_json_bytes, sha256_file
    from ak_trading.futures.readiness import inspect_readiness

    bundle = read_input_bundle(bundle_path)
    report = inspect_readiness(bundle)
    assert_output_directory_is_new(output, (bundle_path,))
    output.mkdir(parents=True)
    (output / "report.json").write_bytes(canonical_json_bytes(report))
    (output / "manifest.json").write_bytes(canonical_json_bytes({
        "schema_version": "futures_readiness_v1", "status": "complete",
        "completion_basis": "diagnostic_publication_not_execution_readiness",
        "input_manifest_sha256": sha256_file(bundle_path / "manifest.json"),
        "content_hashes": {"report.json": sha256_file(output / "report.json")},
        "account_artifacts": "not_requested",
    }))
    return {"status": "published", "output": str(output), "execution_status": report["execution_status"],
            "strict_execution_enabled": False, "counts": report["counts"]}


def _legacy_reference_report(bars, dates) -> dict:
    from ak_trading.research import Bar, ResearchConfig, run_research

    grid_origin = datetime(2024, 1, 1, tzinfo=SH)

    def demo_bar(symbol, index, bar):
        open_time = grid_origin + timedelta(hours=24 * index)
        return Bar(
            symbol=symbol, open_time=open_time,
            close_time=open_time + timedelta(hours=23),
            open=float(bar.open), high=float(bar.high),
            low=float(bar.low), close=float(bar.close),
        )

    by_product: dict[str, tuple[str, dict]] = {}
    for symbol, series in sorted(bars.items()):
        key = symbol.rsplit(".", 1)[0]
        if key not in by_product or len(series) > len(by_product[key][1]):
            by_product[key] = (symbol, series)
    common_days = set(dates)
    for _, series in by_product.values():
        common_days &= set(series.keys())
    day_index = {day: index for index, day in enumerate(dates)}
    rows = []
    for key, (symbol, series) in sorted(by_product.items()):
        for day in sorted(common_days):
            bar = series.get(day)
            if bar is not None:
                rows.append(demo_bar(symbol, day_index[day], bar))
    rows.sort(key=lambda b: (b.close_time, b.symbol))
    if len(rows) < 4:
        return {"status": "not_enough_data", "rows": len(rows)}
    config = ResearchConfig(train_bars=max(4, len(common_days) // 3))
    report = run_research(rows, config)
    final_equity: dict[str, object] = {}
    if isinstance(report, dict):
        # The legacy prototype reports per-row portfolios; the terminal
        # equity is the LAST row's per-mode account equity (A8).
        legacy_rows = report.get("rows") or []
        for mode in ("dynamic", "fixed"):
            for row in reversed(legacy_rows):
                account = (row.get("portfolios") or {}).get(mode)
                if isinstance(account, dict) and "equity" in account:
                    final_equity[mode] = account["equity"]
                    break
        if not final_equity:
            final_equity = {
                "note": (
                    "legacy adapter produced no terminal equity field; "
                    f"rows={len(rows)}, train_bars={config.train_bars}"
                )
            }
    return {
        "status": "published",
        "synchronized_days": len(common_days),
        "symbols": sorted(symbol for symbol, _ in by_product.values()),
        "final_equity": final_equity,
        "accounting": "normalized_initial_capital",
        "holding": "next_open_to_same_close",
        "comparable_to_futures": False,
        "dataset_kind": "synthetic_demo",
        "timestamp_mapping": "regularized_24h_grid_not_exchange_sessions",
        "rows": len(rows),
    }


# ---------------------------------------------------------------------------
# signals mode on a frozen real bundle
# ---------------------------------------------------------------------------


def run_real_signals(output: Path, *, bundle_path: Path, created_at: str):
    from ak_trading.futures.data import synchronized_far_bar
    from ak_trading.futures.io import publish_signals_bundle, read_input_bundle
    from ak_trading.futures.spec import (
        ClockStatus, ContractMetadata, ProductKey, StrategyContext,
    )
    from ak_trading.futures.strategies import (
        DonchianTrend, MATrend, TermStructureCarry, ZScoreReversion,
    )
    from ak_trading.futures.structures import build_structure_frame

    bundle = read_input_bundle(bundle_path)
    if not bundle.bars:
        return {
            "status": "blocked",
            "reason": "no_readable_real_bars",
            "note": "an empty real input cannot obtain AC9 PASS (finalized-plan §十三.2)",
        }
    bars_by_symbol: dict[str, list] = {}
    for bar in bundle.bars:
        bars_by_symbol.setdefault(bar.symbol, []).append(bar)
    for symbol in bars_by_symbol:
        bars_by_symbol[symbol].sort(key=lambda b: b.datetime_nano)
    contracts_by_product: dict[str, list[dict]] = {}
    for contract in bundle.contracts:
        contracts_by_product.setdefault(str(contract["product"]), []).append(contract)

    strategies = {
        "donchian_trend": DonchianTrend(),
        "ma_trend": MATrend(),
        "zscore_reversion": ZScoreReversion(),
        "term_structure_carry": TermStructureCarry(),
    }
    clock_summary = {"trading_day_declared": 0, "raw_timestamp_only": 0}
    signals_rows: list[dict[str, object]] = []
    per_product: dict[str, dict] = {}
    availability_matrix: dict[str, object] = {
        "per_strategy": {},
        "per_field": {},
        "carry_pairing": {},
    }

    def expiry_of(contracts, symbol):
        for contract in contracts:
            if contract["raw_symbol"] == symbol:
                raw = contract.get("expiry_at")
                return datetime.fromisoformat(raw) if raw else None
        return None

    for product_name in sorted(contracts_by_product):
        contracts = contracts_by_product[product_name]
        symbol_rows = {
            c["raw_symbol"]: bars_by_symbol.get(c["raw_symbol"], []) for c in contracts
        }
        total = sum(len(v) for v in symbol_rows.values())
        declared = sum(
            1 for v in symbol_rows.values() for b in v if b.trading_day is not None
        )
        clock_summary["trading_day_declared"] += declared
        clock_summary["raw_timestamp_only"] += total - declared
        product_key = ProductKey(*product_name.split("."))
        per_product[product_name] = {
            "bar_rows": total,
            "evaluated_contracts": 0,
            "warmup_contracts": 0,
        }
        availability_matrix["per_strategy"][product_name] = {}
        for strategy_id, strategy in sorted(strategies.items()):
            evaluated = warmup = 0
            if strategy_id != "term_structure_carry":
                for symbol, bars in sorted(symbol_rows.items()):
                    if not bars:
                        continue
                    frame = build_structure_frame(bars)
                    if len(bars) < strategy.spec.required_bars:
                        warmup += 1
                        continue
                    evaluated += 1
                    context = StrategyContext(
                        strategy_id=strategy_id, product=product_key,
                        contract_instance_id=symbol, raw_symbol=symbol,
                        decision_at=None,
                        clock_status=ClockStatus.OBSERVATION_ONLY,
                        completed_bar_id=f"{symbol}:{bars[-1].datetime_nano}",
                        bars=tuple(bars), structure=frame, factors={},
                    )
                    result = strategy.evaluate(context, None)
                    candidate = result.diagnostics.get("candidate_direction")
                    signals_rows.append({
                        "strategy_id": strategy_id, "family": result.family,
                        "exchange": product_name.split(".")[0],
                        "product": product_name,
                        "contract_instance_id": symbol, "raw_symbol": symbol,
                        "decision_at": "", "clock_status": "observation_only",
                        "completed_bar_id": f"{symbol}:{bars[-1].datetime_nano}",
                        "target_position": "", "status": result.status,
                        "reason": result.reason,
                        "diagnostics": json.dumps(
                            {"candidate_direction": candidate, "non_actionable": True},
                            sort_keys=True),
                    })
            else:
                # Carry on real data (B5): sequential observation-only
                # evaluation over the best synchronized pair, with pairing
                # diagnostics (§十三.2).
                richest = sorted(
                    ((s, b) for s, b in symbol_rows.items() if len(b) >= 21),
                    key=lambda item: -len(item[1]),
                )[:10]
                pair_samples = []
                for i, (near_symbol, near_bars) in enumerate(richest):
                    for far_symbol, far_bars in richest[i + 1:]:
                        near_expiry = expiry_of(contracts, near_symbol)
                        far_expiry = expiry_of(contracts, far_symbol)
                        if near_expiry is None or far_expiry is None:
                            continue
                        gap = (far_expiry - near_expiry).total_seconds() / 86400.0
                        if not (60.0 <= gap <= 180.0) or far_expiry <= near_expiry:
                            continue
                        sync_count = sum(
                            1 for nb in near_bars
                            if synchronized_far_bar(nb, far_bars) is not None
                        )
                        if sync_count:
                            pair_samples.append({
                                "near": near_symbol, "far": far_symbol,
                                "gap_days": round(gap, 2),
                                "synchronized_observations": sync_count,
                            })
                availability_matrix["carry_pairing"][product_name] = {
                    "sampled_contracts": len(richest),
                    "pairs_with_sync_in_gap_range": len(pair_samples),
                    "top_pairs": sorted(
                        pair_samples, key=lambda p: -p["synchronized_observations"]
                    )[:5],
                }
                if pair_samples:
                    best = max(
                        pair_samples, key=lambda p: p["synchronized_observations"]
                    )
                    near_bars = symbol_rows[best["near"]]
                    far_bars = symbol_rows[best["far"]]
                    carry_state = None
                    final_status = final_reason = None
                    evaluated_points = 0
                    sync_points = [
                        nb for nb in near_bars
                        if synchronized_far_bar(nb, far_bars) is not None
                    ][-25:]
                    for near_bar in sync_points:
                        window = [
                            b for b in near_bars
                            if b.datetime_nano <= near_bar.datetime_nano
                        ]
                        if len(window) < 21:
                            continue
                        context = StrategyContext(
                            strategy_id="term_structure_carry",
                            product=product_key,
                            contract_instance_id=best["near"],
                            raw_symbol=best["near"],
                            decision_at=None,
                            clock_status=ClockStatus.OBSERVATION_ONLY,
                            completed_bar_id=f"{best['near']}:{near_bar.datetime_nano}",
                            bars=tuple(window), structure=None, factors={},
                            expiry_at=expiry_of(contracts, best["near"]),
                            pair_candidates=(ContractMetadata(
                                contract_instance_id=best["far"],
                                raw_symbol=best["far"], product=product_key,
                                expiry_at=expiry_of(contracts, best["far"]),
                            ),),
                            pair_bars={best["far"]: tuple(far_bars)},
                        )
                        result = strategies["term_structure_carry"].evaluate(
                            context, carry_state
                        )
                        carry_state = result.next_state
                        final_status, final_reason = result.status, result.reason
                        evaluated_points += 1
                    evaluated = 1 if evaluated_points else 0
                    availability_matrix["carry_pairing"][product_name]["best_pair"] = best
                    availability_matrix["carry_pairing"][product_name][
                        "sequential_sync_points_evaluated"
                    ] = evaluated_points
                    availability_matrix["carry_pairing"][product_name][
                        "final_status"
                    ] = final_status
                    availability_matrix["carry_pairing"][product_name][
                        "final_reason"
                    ] = final_reason
                    if evaluated_points:
                        signals_rows.append({
                            "strategy_id": "term_structure_carry",
                            "family": "term_structure_carry",
                            "exchange": product_name.split(".")[0],
                            "product": product_name,
                            "contract_instance_id": best["near"],
                            "raw_symbol": best["near"],
                            "decision_at": "",
                            "clock_status": "observation_only",
                            "completed_bar_id": (
                                f"{best['near']}:{sync_points[-1].datetime_nano}"
                            ),
                            "target_position": "", "status": final_status,
                            "reason": final_reason,
                            "diagnostics": json.dumps(
                                {"best_pair": best, "non_actionable": True},
                                sort_keys=True),
                        })
            per_product[product_name]["evaluated_contracts"] += evaluated
            per_product[product_name]["warmup_contracts"] += warmup
            availability_matrix["per_strategy"][product_name][strategy_id] = {
                "evaluated": evaluated,
                "warmup_only": warmup,
                "required_bars": strategy.spec.required_bars,
                "required_features": list(strategy.spec.required_features),
                "required_factors": list(strategy.spec.required_factors),
                "status": "observation_only",
            }
        products_payload = (bundle.availability.get("products") or {}).get(product_name)
        if products_payload:
            availability_matrix["per_field"][product_name] = {
                "strict_fields": products_payload.get("strict_fields", {}),
                "exported_bar_rows": products_payload.get("exported_bar_rows"),
            }

    report = {
        "stage": "S3+fix1",
        "dataset": str(bundle_path),
        "mode": "signals",
        "strategies_evaluated": sorted(strategies),
        "clock_policy": {
            **clock_summary,
            "note": "no verified historical session evidence; observation_only only",
        },
        "products": per_product,
        "availability_matrix": availability_matrix,
        "D_status": "unavailable",
        "net_pnl": None, "sharpe": None, "equity_curve": None,
        "performance_status": "unavailable_no_real_strict_backtest",
        "execution_readiness": (
            "blocked for strict open/close/value actions: multiplier/fees/margin/"
            "limits lack approved historical evidence"
        ),
        "account_artifacts": "not_requested",
    }
    publish_signals_bundle(
        output,
        created_at=created_at,
        config={"mode": "signals", "input_bundle": str(bundle_path)},
        report=report,
        availability={
            "member_factor": {"status": "not_requested",
                              "reason": "deferred_no_default_strategy_dependency"},
            "availability_matrix": availability_matrix,
        },
        signals_rows=signals_rows,
    )
    return {
        "status": "published",
        "output": str(output),
        "signals_rows": len(signals_rows),
        "products": sorted(per_product),
    }


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.include_session_probe or args.include_member_ranks:
        print(json.dumps({"status": "rejected", "reason": "unsupported_option"}))
        return EXIT_USAGE
    if args.demo == bool(args.input_bundle):
        print(json.dumps({
            "status": "rejected",
            "reason": "exactly_one_of_demo_or_input_bundle_required",
        }))
        return EXIT_USAGE
    if args.validation is not None and args.mode != "backtest":
        print(json.dumps({
            "status": "rejected",
            "reason": "validation_only_applies_to_backtest",
        }))
        return EXIT_USAGE
    output = Path(args.output)
    if output.exists():
        print(json.dumps({"status": "rejected", "reason": "output_directory_exists"}))
        return EXIT_USAGE
    if args.config is not None and not Path(args.config).exists():
        print(json.dumps({"status": "rejected", "reason": "config_path_missing"}))
        return EXIT_USAGE

    created_at = "2026-09-07T00:00:00+08:00"  # deterministic payload policy
    try:
        from ak_trading.futures.spec import ResearchPolicy

        if args.demo:
            if args.mode != "backtest":
                print(json.dumps({
                    "status": "rejected",
                    "reason": "demo_supports_backtest_mode_use_input_bundle_for_signals",
                }))
                return EXIT_USAGE
            validation = args.validation or "smoke"
            inputs, config = _inputs_from_fixture(630)
            if args.config is not None:
                loaded = json.loads(Path(args.config).read_bytes().decode("utf-8"))
                unsupported = sorted(set(loaded) - SUPPORTED_CONFIG_TOP_KEYS)
                if unsupported:
                    print(json.dumps({
                        "status": "rejected",
                        "reason": "unsupported_config_keys",
                        "keys": unsupported,
                    }))
                    return EXIT_USAGE
                for key, value in loaded.items():
                    if key != "policy" and config.get(key) != value:
                        raise ValueError(f"demo_config_override_not_supported:{key}")
                config = {**config, "policy": loaded.get("policy", config["policy"])}
            policy = _policy_from_config(config)
            summary = run_backtests(
                output, inputs=inputs, config=config, created_at=created_at,
                validation=validation, policy=policy,
            )
            print(json.dumps(summary, ensure_ascii=False))
            return EXIT_BLOCKED if summary.get("status") == "blocked" else EXIT_OK

        bundle = Path(args.input_bundle)  # type: ignore[arg-type]
        if not bundle.exists():
            print(json.dumps({"status": "blocked", "reason": "input_bundle_missing"}))
            return EXIT_BLOCKED
        try:
            manifest = json.loads((bundle / "manifest.json").read_bytes().decode("utf-8"))
        except Exception:
            print(json.dumps({"status": "blocked",
                              "reason": "input_bundle_not_readable"}))
            return EXIT_BLOCKED
        dataset_kind = str(manifest.get("dataset_kind", ""))
        if args.mode == "readiness":
            summary = run_readiness(output, bundle)
            print(json.dumps(summary, ensure_ascii=False))
            return EXIT_OK
        if args.mode == "signals":
            if dataset_kind != "real_export":
                print(json.dumps({
                    "status": "rejected",
                    "reason": "signals_mode_requires_real_export_bundle",
                }))
                return EXIT_USAGE
            summary = run_real_signals(output, bundle_path=bundle, created_at=created_at)
            code = EXIT_BLOCKED if summary.get("status") == "blocked" else EXIT_OK
            print(json.dumps(summary, ensure_ascii=False))
            return code
        if dataset_kind != "synthetic_demo":
            print(json.dumps({
                "status": "blocked",
                "reason": "real_strict_backtest_not_implemented",
                "implementation_status": "not_implemented",
                "data_readiness": "not_assessed",
                "detail": (
                    "This entry point implements synthetic replay only. "
                    "It has not evaluated historical rule coverage in the supplied bundle."
                ),
                "available_alternative": (
                    "scripts/backtest_history.py: single-contract historical-price "
                    "scenario backtest with explicitly configured economic assumptions"
                ),
            }))
            return EXIT_BLOCKED
        # Frozen synthetic input: the replay is INPUT-DRIVEN (B7) — bars and
        # contracts come from the bundle, rules/policy/roll from the config.
        if args.config is None:
            print(json.dumps({
                "status": "rejected",
                "reason": "frozen_bundle_replay_requires_config",
            }))
            return EXIT_USAGE
        loaded_config = json.loads(Path(args.config).read_bytes().decode("utf-8"))
        unsupported = sorted(set(loaded_config) - SUPPORTED_CONFIG_TOP_KEYS)
        if unsupported:
            print(json.dumps({
                "status": "rejected",
                "reason": "unsupported_config_keys",
                "keys": unsupported,
            }))
            return EXIT_USAGE
        inputs = _inputs_from_bundle(bundle, loaded_config)
        policy = _policy_from_config(loaded_config)
        validation = args.validation or str(loaded_config.get("validation", "smoke"))
        summary = run_backtests(
            output, inputs=inputs, config=loaded_config, created_at=created_at,
            validation=validation, policy=policy,
        )
        print(json.dumps(summary, ensure_ascii=False))
        return EXIT_BLOCKED if summary.get("status") == "blocked" else EXIT_OK
    except Exception as exc:  # schema/data errors map to usage exit code
        print(json.dumps({
            "status": "rejected",
            "reason": "invalid_input",
            "error_type": type(exc).__name__,
            "detail": str(exc)[:200],
        }))
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
