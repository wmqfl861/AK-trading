"""State advancement, execution feedback, round-budget freezing and restore
validation (04-M3B-策略库.md §4, §7).

All functions are pure state transforms: the M4 ledger checkpoint is the
source of truth and M3B never persists a parallel account. Money amounts are
``Decimal``; the M1 pure stress formula (``policy.calculate_stress_loss``) is
the only stress source and the stress policy object is stored inside every
frozen ``RoundBudget``.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Literal, Mapping

from ak_trading.v2 import policy as m1_policy
from ak_trading.v2.artifacts import stable_id
from ak_trading.v2.contracts import (
    AccountSnapshot,
    ExecutionResult,
    FrozenRange,
    Horizon,
    LayerSpec,
    MarketForecast,
    OrderIntent,
    RoundBudget,
    StrategyContext,
    StrategySpec,
    StrategyState,
    TradingRule,
)

STRESS_POLICY_ID = "stress-v2.1.0"
OBSERVATION_DURATIONS: Mapping[str, int] = {"MINUTE": 60, "DAILY": 86400}
RESEARCH_MAXIMUM_ROUND_LOTS = 7
MAXIMUM_STOP_ATR_FRACTION = Decimal("0.1")

RANGE_FAMILIES = frozenset({"REVERSION", "GRID", "MARTINGALE"})


class StateTransitionError(ValueError):
    """Fixed-reason state/restore violation."""


# --- geometry and money helpers ------------------------------------------------


def worst_entry_price(reference: Decimal, side: Literal[-1, 1], rule: TradingRule,
                      initial_atr_points: Decimal) -> Decimal:
    """Known fill reference plus adverse 0.1 initial ATR plus registered slip."""
    slip = (rule.slippage_ticks + rule.impact_ticks) * rule.price_tick
    return reference + side * (MAXIMUM_STOP_ATR_FRACTION * initial_atr_points + slip)


def _leg_fee_cny(leg, price: Decimal, lots: int, rule: TradingRule) -> Decimal:
    return leg.per_lot_cny * lots + leg.notional_rate * price * rule.multiplier * lots


def round_loss_cny(layers: tuple[LayerSpec, ...], side: Literal[-1, 1],
                   exit_price: Decimal, rule: TradingRule,
                   initial_atr_points: Decimal,
                   *, fee_multiplier: Decimal = Decimal("1")) -> Decimal:
    """Planned round loss at ``exit_price`` from worst allowed entries:
    ``sum_i lots_i * (worst_entry_i - exit) * multiplier + open fees + exit
    fees`` (04-M3B §6.6). Unknown fee legs raise instead of counting zero.
    A zero ``initial_atr_points`` reproduces the ideal (no-offset) hand calc.
    """
    if rule.open_fee is None:
        raise StateTransitionError("open_fee_unknown")
    close_legs = [leg for leg in (rule.close_fee, rule.close_today_fee) if leg is not None]
    if not close_legs:
        raise StateTransitionError("close_fee_unknown")
    gross = Decimal("0")
    open_fees = Decimal("0")
    total_lots = 0
    for layer in layers:
        worst = worst_entry_price(layer.reference_entry_price, side, rule, initial_atr_points)
        gross += layer.lots * side * (worst - exit_price) * rule.multiplier
        open_fees += _leg_fee_cny(rule.open_fee, worst, layer.lots, rule)
        total_lots += layer.lots
    exit_fees = max(_leg_fee_cny(leg, exit_price, total_lots, rule) for leg in close_legs)
    total = gross + fee_multiplier * (open_fees + exit_fees)
    return total if total > 0 else Decimal("0")


def peak_margin_cny(layers: tuple[LayerSpec, ...], side: Literal[-1, 1],
                    rule: TradingRule, initial_atr_points: Decimal,
                    stress_policy) -> Decimal | None:
    """Peak margin over every layer-path prefix under price/rate stress;
    ``None`` when the frozen rule carries no margin rate (cannot be guessed)."""
    rate = rule.long_margin_rate if side == 1 else rule.short_margin_rate
    if rate is None:
        return None
    stressed = min(rate * stress_policy.margin_multiplier, Decimal("1"))
    peak = Decimal("0")
    running = Decimal("0")
    for layer in layers:
        worst = worst_entry_price(layer.reference_entry_price, side, rule, initial_atr_points)
        running += worst * rule.multiplier * layer.lots * stressed
        if running > peak:
            peak = running
    return peak


def locate_forecast(forecasts: tuple[MarketForecast, ...], *, barrier_key: str,
                    horizon: Horizon, duration_seconds: int) -> MarketForecast | None:
    for forecast in forecasts:
        if (forecast.horizon == horizon and forecast.barrier_key == barrier_key
                and forecast.duration_seconds == duration_seconds):
            return forecast
    return None


def _layer_plan(spec: StrategySpec, context: StrategyContext,
                side: Literal[-1, 1]) -> tuple[tuple[Decimal, ...], tuple[int, ...]]:
    """Trigger lines and lot multipliers per family; layer 0 keeps the known
    confirmation reference C, later layers use the planned trigger lines."""
    reference = context.features.reference_price
    atr = context.features.atr_points
    if reference is None or atr is None:
        raise StateTransitionError("reference_price_or_atr_missing")
    family = spec.family
    if family not in RANGE_FAMILIES:
        return (reference,), (1,)
    frozen = context.regime.frozen_range
    if frozen is None:
        raise StateTransitionError("frozen_range_required")
    width = frozen.upper - frozen.lower
    if family == "REVERSION":
        fraction = Decimal(str(spec.parameters["entry_fraction"]))
        fractions = (fraction,)
        multipliers = (1,)
    else:
        fractions = tuple(Decimal(str(value)) for value in spec.parameters["layer_fractions"])
        multipliers = tuple(int(value) for value in spec.parameters["layer_multipliers"])
    triggers = tuple(
        frozen.lower + fraction * width if side == 1 else frozen.upper - fraction * width
        for fraction in fractions)
    return triggers, multipliers


def _budget_geometry(spec: StrategySpec, context: StrategyContext,
                     side: Literal[-1, 1]) -> tuple[Decimal, Decimal, FrozenRange | None, str | None]:
    reference = context.features.reference_price
    atr = context.features.atr_points
    if reference is None or atr is None:
        raise StateTransitionError("reference_price_or_atr_missing")
    if spec.family in RANGE_FAMILIES:
        frozen = context.regime.frozen_range
        if frozen is None:
            raise StateTransitionError("frozen_range_required")
        stop = frozen.stop_lower if side == 1 else frozen.stop_upper
        return stop, frozen.center, frozen, frozen.range_id
    stop_multiple = Decimal(str(spec.parameters["stop_loss_multiple"]))
    target_multiple = Decimal(str(spec.parameters["take_profit_multiple"]))
    stop = reference - side * stop_multiple * atr
    target = reference + side * target_multiple * atr
    return stop, target, None, None


def freeze_round_budget(spec: StrategySpec, context: StrategyContext,
                        side: Literal[-1, 1], episode_id: str) -> RoundBudget | None:
    """Freeze the whole-round budget before the first layer.

    Returns ``None`` (no start) whenever any resource check fails: the M1
    stress policy must be stress-v2.1.0, the max-holding ATR_1 forecast must
    exist and its window must fit the holding deadline, a feasible integer
    base lot q must exist within the spec/research caps such that planned
    loss, stress loss and peak margin all pass, and all fees/margin rates
    must be known. The full prediction window is never shortened to fit.
    """
    rule = context.rule
    if rule is None:
        return None
    if rule.evidence == "UNKNOWN":
        # FEE-C1-01: an UNKNOWN-evidence fee timeline cannot price a round
        # budget; single-layer/martingale candidates then fail with
        # BUDGET_INFEASIBLE instead of freezing guessed fees.
        return None
    if context.stress_policy.policy_id != STRESS_POLICY_ID:
        return None
    features = context.features
    reference = features.reference_price
    atr = features.atr_points
    if reference is None or reference <= 0 or atr is None or atr <= 0:
        return None
    forecast = locate_forecast(
        context.forecasts, barrier_key=spec.path_barrier_key, horizon=spec.max_holding,
        duration_seconds=OBSERVATION_DURATIONS[spec.mode])
    if forecast is None:
        return None
    latest_exit = forecast.window_end_nano
    if latest_exit > context.holding_deadline_nano:
        return None
    try:
        triggers, multipliers = _layer_plan(spec, context, side)
        stop, target, frozen, range_id = _budget_geometry(spec, context, side)
    except StateTransitionError:
        return None

    research = m1_policy.research_policy()
    equity = context.account.equity_cny
    round_cap = equity * Decimal(str(spec.parameters["round_risk_ratio"]))
    stress_cap = equity * Decimal(str(research["maximum_stress_loss_to_equity"]))
    day_loss_limit = equity * Decimal(str(research["maximum_day_loss_ratio"]))
    margin_cap = equity * Decimal(str(research["maximum_strategy_margin_ratio"]))

    total_units = sum(multipliers)
    hard_cap = min(spec.max_total_lots, RESEARCH_MAXIMUM_ROUND_LOTS)
    base_cap = hard_cap // total_units
    minimum_lots = max(rule.lot_step, rule.min_open_lots)
    for base_lots in range(base_cap, 0, -1):
        if base_lots < minimum_lots or base_lots % rule.lot_step != 0:
            continue
        lots_tuple = tuple(multiplier * base_lots for multiplier in multipliers)
        if sum(lots_tuple) > hard_cap:
            continue
        layers = tuple(
            LayerSpec(
                index=index,
                trigger_price=trigger,
                reference_entry_price=(reference if index == 0 else trigger),
                lots=lots)
            for index, (trigger, lots) in enumerate(zip(triggers, lots_tuple)))
        try:
            planned = round_loss_cny(layers, side, stop, rule, atr)
            stress = m1_policy.calculate_stress_loss(
                layers, side, stop, atr, rule, context.stress_policy)
            margin = peak_margin_cny(layers, side, rule, atr, context.stress_policy)
        except (StateTransitionError, ValueError):
            continue
        if margin is None:
            continue
        if planned > round_cap or stress > stress_cap or stress > equity:
            continue
        if margin > margin_cap:
            continue
        identity = "|".join((
            episode_id, features.contract_id, str(side), str(stop), str(target),
            str(latest_exit), *(f"{layer.reference_entry_price}@{layer.lots}" for layer in layers),
        ))
        return RoundBudget(
            budget_id=stable_id("budget", identity),
            episode_id=episode_id,
            contract_id=features.contract_id,
            frozen_at_nano=context.now_nano,
            range_id=range_id,
            frozen_range=frozen,
            stress_policy=context.stress_policy,
            initial_atr_points=atr,
            side=side,
            layers=layers,
            stop_price=stop,
            target_price=target,
            latest_exit_nano=latest_exit,
            maximum_lots=sum(lots_tuple),
            planned_loss_cny=planned,
            stress_loss_cny=stress,
            reserved_margin_cny=margin,
            day_loss_limit_cny=day_loss_limit,
            equity_at_start_cny=equity,
        )
    return None


# --- candidate state builders ---------------------------------------------------


def open_candidate_state(spec: StrategySpec, context: StrategyContext,
                         episode_id: str, budget: RoundBudget,
                         window_end_nano: int, setup=None,
                         confirmation_count: int = 0) -> StrategyState:
    """Selected-OPEN candidate state: WAIT_TRIGGER with frozen budget and the
    pending exit committed only after an actual fill (apply_execution)."""
    return StrategyState(
        strategy_id=spec.strategy_id, episode_id=episode_id,
        contract_id=context.features.contract_id, phase="WAIT_TRIGGER",
        setup=setup, last_regime=context.regime, direction=budget.side,
        filled_layers=(), layer_filled_lots=tuple(0 for _ in budget.layers),
        pending_layer_index=None, active_exit_nano=None,
        pending_exit_nano=window_end_nano, pending_intent_ids=(),
        budget=budget, confirmation_count=confirmation_count,
        cooldown_until_nano=None, last_event_id=None,
    )


def paused_state(state: StrategyState, regime, cooldown_until_nano: int | None) -> StrategyState:
    return StrategyState(
        strategy_id=state.strategy_id, episode_id=None,
        contract_id=state.contract_id, phase="PAUSED", setup=None,
        last_regime=regime, direction=0, filled_layers=(), layer_filled_lots=(),
        pending_layer_index=None, active_exit_nano=None, pending_exit_nano=None,
        pending_intent_ids=(), budget=None,
        confirmation_count=state.confirmation_count,
        cooldown_until_nano=cooldown_until_nano,
        last_event_id=state.last_event_id,
    )


def add_candidate_state(state: StrategyState, layer_index: int,
                        new_pending_exit_nano: int) -> StrategyState:
    if state.budget is None or state.active_exit_nano is None:
        raise StateTransitionError("add_requires_budget_and_active_exit")
    return StrategyState(
        strategy_id=state.strategy_id, episode_id=state.episode_id,
        contract_id=state.contract_id, phase=state.phase, setup=state.setup,
        last_regime=state.last_regime, direction=state.direction,
        filled_layers=state.filled_layers, layer_filled_lots=state.layer_filled_lots,
        pending_layer_index=layer_index, active_exit_nano=state.active_exit_nano,
        pending_exit_nano=min(new_pending_exit_nano, state.active_exit_nano),
        pending_intent_ids=(), budget=state.budget,
        confirmation_count=state.confirmation_count,
        cooldown_until_nano=state.cooldown_until_nano,
        last_event_id=state.last_event_id,
    )


# --- execution feedback ----------------------------------------------------------


def _lots_for_strategy(account: AccountSnapshot, strategy_id: str, contract_id: str) -> int:
    return sum(lot.lots for lot in account.lots
               if lot.strategy_id == strategy_id and lot.contract_id == contract_id)


def apply_execution(previous: StrategyState, result: ExecutionResult,
                    account: AccountSnapshot, now_nano: int) -> StrategyState:
    """Advance factual state from one execution receipt.

    Receipts are incremental: every fill an intent still owes can arrive in
    later PARTIAL/FILLED receipts, so an intent leaves ``pending_intent_ids``
    only on a terminal status (FILLED/CANCELLED/EXPIRED) or when the whole
    episode settles; a BLOCKED exit keeps the intent pending and the state
    EXITING so the exit stays owed (EXIT_PENDING). Deduplication
    responsibility: this layer applies exactly the fills each receipt
    carries and is a no-op for empty terminal replays of already-settled
    intents; suppressing re-delivery of already-counted fills (by
    ``fill_id``) is the M4 checkpoint's job (``processed_event_ids``)
    because ``StrategyState`` deliberately carries no fill ledger. Unknown
    intents and contract/episode mismatches raise immediately. OPEN fills
    commit ``active_exit_nano`` at min(budget latest exit, pending exit);
    ADD fills only ever tighten it. A fully cancelled unfilled OPEN clears
    the whole candidate; cancelled ADDs keep the real episode.
    """
    intent_id = result.intent_id
    known = intent_id in previous.pending_intent_ids
    replay_of_settled = intent_id == previous.last_event_id
    if not known and not replay_of_settled:
        raise StateTransitionError("unknown_intent_for_state")
    for fill in result.fills:
        if fill.contract_id != previous.contract_id:
            raise StateTransitionError("fill_contract_mismatch")
        if fill.strategy_id != previous.strategy_id:
            raise StateTransitionError("fill_strategy_mismatch")
        if previous.episode_id is not None and fill.episode_id != previous.episode_id:
            raise StateTransitionError("fill_episode_mismatch")

    if not result.fills:
        if result.status in ("FILLED", "PARTIAL"):
            raise StateTransitionError("execution_status_without_fills")
        if not known:
            # Empty replay of an already-settled terminal receipt: idempotent.
            return previous

    entry_lots = sum(fill.lots for fill in result.fills if fill.offset == "OPEN")
    exit_lots = sum(fill.lots for fill in result.fills if fill.offset != "OPEN")
    terminal = result.status in ("FILLED", "CANCELLED", "EXPIRED")

    layer_filled = list(previous.layer_filled_lots)
    budget = previous.budget
    pending_layer = previous.pending_layer_index
    active_exit = previous.active_exit_nano
    pending_exit = previous.pending_exit_nano
    phase = previous.phase
    episode_id = previous.episode_id
    filled_layers = previous.filled_layers
    cooldown = previous.cooldown_until_nano
    pending_intents = previous.pending_intent_ids

    if entry_lots > 0:
        if budget is None or episode_id is None:
            raise StateTransitionError("entry_fill_requires_episode")
        layer = 0 if pending_layer is None else pending_layer
        if layer >= len(layer_filled):
            raise StateTransitionError("layer_index_out_of_plan")
        layer_filled[layer] += entry_lots
        if layer_filled[layer] > budget.layers[layer].lots:
            raise StateTransitionError("layer_filled_exceeds_plan")
        phase = "HOLDING"
        if active_exit is None:
            active_exit = min(budget.latest_exit_nano, pending_exit or budget.latest_exit_nano)
        elif pending_exit is not None and pending_exit < active_exit:
            active_exit = pending_exit
        pending_exit = None
        if layer_filled[layer] >= budget.layers[layer].lots and pending_layer is not None:
            pending_layer = None

    if exit_lots > 0:
        remaining = _lots_for_strategy(account, previous.strategy_id, previous.contract_id)
        if remaining <= 0:
            # Flat: the PAUSED transition happens here, while the calendar-
            # aware cooldown value is anchored by the strategy's next
            # evaluation (which owns the frozen session calendar), so it can
            # never unlock early across lunch/overnight closures.
            return paused_state(
                _with_updates(previous, phase="PAUSED", episode=None, budget=None,
                              filled_layers=(), layer_filled=(), pending_layer=None,
                              active_exit=None, pending_exit=None,
                              pending_intents=_drop(pending_intents, intent_id),
                              cooldown=previous.cooldown_until_nano,
                              last_event=intent_id),
                previous.last_regime, previous.cooldown_until_nano)
        phase = "EXITING"

    if not result.fills and result.status in ("CANCELLED", "EXPIRED", "BLOCKED"):
        holding_remaining = _lots_for_strategy(
            account, previous.strategy_id, previous.contract_id)
        if result.status == "BLOCKED" and holding_remaining > 0:
            # Exit blocked by limit/capacity: the exit stays owed — keep the
            # intent pending and the phase EXITING (EXIT_PENDING).
            phase = "EXITING"
        elif (episode_id is not None and phase == "WAIT_TRIGGER"
                and all(lots == 0 for lots in layer_filled)):
            # Fully unfilled OPEN cancelled: explicit fresh observation state.
            return StrategyState(
                strategy_id=previous.strategy_id, episode_id=None,
                contract_id=previous.contract_id, phase="OBSERVE",
                setup=None, last_regime=previous.last_regime, direction=0,
                filled_layers=(), layer_filled_lots=(), pending_layer_index=None,
                active_exit_nano=None, pending_exit_nano=None, pending_intent_ids=(),
                budget=None, confirmation_count=0,
                cooldown_until_nano=previous.cooldown_until_nano,
                last_event_id=intent_id,
            )
        else:
            # Cancelled ADD/exit: drop this intent's pendings, keep the rest.
            pending_layer = None
            pending_exit = None
            pending_intents = _drop(pending_intents, intent_id)

    if terminal:
        pending_intents = _drop(pending_intents, intent_id)

    if budget is not None and episode_id is not None:
        filled_layers = tuple(
            index for index, layer in enumerate(budget.layers)
            if layer_filled[index] >= layer.lots)

    return _with_updates(
        previous, phase=phase, episode=episode_id, budget=budget,
        filled_layers=filled_layers, layer_filled=tuple(layer_filled),
        pending_layer=pending_layer, active_exit=active_exit, pending_exit=pending_exit,
        pending_intents=pending_intents,
        cooldown=cooldown, last_event=intent_id)


def _drop(ids: tuple[str, ...], value: str) -> tuple[str, ...]:
    return tuple(item for item in ids if item != value)


def _with_updates(state: StrategyState, *, phase: str, episode: str | None,
                  budget, filled_layers: tuple[int, ...],
                  layer_filled: tuple[int, ...], pending_layer: int | None,
                  active_exit: int | None, pending_exit: int | None,
                  pending_intents: tuple[str, ...], cooldown: int | None,
                  last_event: str | None) -> StrategyState:
    return StrategyState(
        strategy_id=state.strategy_id, episode_id=episode,
        contract_id=state.contract_id, phase=phase, setup=state.setup,
        last_regime=state.last_regime, direction=state.direction,
        filled_layers=filled_layers, layer_filled_lots=layer_filled,
        pending_layer_index=pending_layer, active_exit_nano=active_exit,
        pending_exit_nano=pending_exit, pending_intent_ids=pending_intents,
        budget=budget, confirmation_count=state.confirmation_count,
        cooldown_until_nano=cooldown, last_event_id=last_event,
    )


# --- restore validation -----------------------------------------------------------


def validate_restored_state(state: StrategyState, account: AccountSnapshot) -> None:
    """Reject corrupted restored states; raising stops new risk everywhere."""
    lots = _lots_for_strategy(account, state.strategy_id, state.contract_id)
    filled_total = sum(state.layer_filled_lots)
    if lots > 0:
        if state.episode_id is None:
            raise StateTransitionError("account_lots_without_episode")
        if state.phase not in ("HOLDING", "EXITING"):
            raise StateTransitionError("account_lots_with_non_holding_phase")
        if filled_total < lots:
            raise StateTransitionError("filled_layers_below_account_lots")
    else:
        if state.phase in ("HOLDING", "EXITING"):
            raise StateTransitionError("holding_phase_without_account_lots")
        if filled_total != 0 and state.episode_id is None:
            raise StateTransitionError("filled_layers_without_episode")
    if state.budget is not None and state.episode_id is not None:
        if len(state.layer_filled_lots) != len(state.budget.layers):
            raise StateTransitionError("layer_filled_lots_length_mismatch")
        for index, layer in enumerate(state.budget.layers):
            if state.layer_filled_lots[index] > layer.lots:
                raise StateTransitionError("layer_filled_exceeds_plan")
        if state.active_exit_nano is not None \
                and state.active_exit_nano > state.budget.latest_exit_nano:
            raise StateTransitionError("active_exit_exceeds_budget_latest_exit")
    if state.pending_layer_index is not None and state.budget is not None:
        if state.pending_layer_index in state.filled_layers:
            raise StateTransitionError("pending_layer_already_complete")
    for lot in account.lots:
        if lot.strategy_id == state.strategy_id and lot.contract_id == state.contract_id:
            if state.episode_id is None or lot.episode_id != state.episode_id:
                raise StateTransitionError("episode_attribution_mismatch")
    if account.killed and lots > 0 and state.phase != "EXITING":
        raise StateTransitionError("killed_account_must_be_exiting")


# --- observation / selected-proposal commits ---------------------------------------


def commit_observation(previous: StrategyState | None,
                       proposal: StrategyProposal) -> StrategyState:
    """Save the resource-free observation fields of any proposal.

    Only setup, last_regime, confirmation_count, last_event_id and (without a
    selected episode) the OBSERVE/WAIT_TRIGGER phase may change; execution
    fields, budget, layers, pendings and cooldown are never touched here.
    """
    nxt = proposal.next_state
    if previous is None:
        return StrategyState(
            strategy_id=proposal.strategy_id, episode_id=None,
            contract_id=proposal.contract_id, phase="OBSERVE",
            setup=nxt.setup, last_regime=nxt.last_regime, direction=nxt.direction,
            filled_layers=(), layer_filled_lots=(), pending_layer_index=None,
            active_exit_nano=None, pending_exit_nano=None, pending_intent_ids=(),
            budget=None, confirmation_count=nxt.confirmation_count,
            cooldown_until_nano=nxt.cooldown_until_nano, last_event_id=nxt.last_event_id,
        )
    phase = previous.phase
    if previous.episode_id is None and nxt.phase in ("OBSERVE", "WAIT_TRIGGER"):
        if phase in ("OBSERVE", "WAIT_TRIGGER"):
            phase = nxt.phase
        elif phase == "PAUSED":
            # The strategy's own cooldown gate already verified expiry; a
            # PAUSED state holds no position so the observation-only
            # transition back to OBSERVE is safe.
            phase = "OBSERVE"
    direction = previous.direction if previous.episode_id is not None else nxt.direction
    return StrategyState(
        strategy_id=previous.strategy_id, episode_id=previous.episode_id,
        contract_id=previous.contract_id, phase=phase, setup=nxt.setup,
        last_regime=nxt.last_regime, direction=direction,
        filled_layers=previous.filled_layers,
        layer_filled_lots=previous.layer_filled_lots,
        pending_layer_index=previous.pending_layer_index,
        active_exit_nano=previous.active_exit_nano,
        pending_exit_nano=previous.pending_exit_nano,
        pending_intent_ids=previous.pending_intent_ids, budget=previous.budget,
        confirmation_count=nxt.confirmation_count,
        cooldown_until_nano=previous.cooldown_until_nano,
        last_event_id=nxt.last_event_id,
    )


def commit_selected_proposal(previous: StrategyState, proposal: StrategyProposal,
                             intents: tuple[OrderIntent, ...]) -> StrategyState:
    """Bind the selected candidate's episode/resources and record intent ids.

    Pure transform: M4 persists this in its single checkpoint; no parallel
    file. The previous state is verified before any binding happens.
    """
    nxt = proposal.next_state
    if nxt.strategy_id != previous.strategy_id:
        raise StateTransitionError("proposal_strategy_mismatch")
    if nxt.contract_id != previous.contract_id:
        raise StateTransitionError("proposal_contract_mismatch")
    for intent in intents:
        if intent.strategy_id != proposal.strategy_id:
            raise StateTransitionError("intent_strategy_mismatch")
        if intent.episode_id != nxt.episode_id:
            raise StateTransitionError("intent_episode_mismatch")
        if intent.contract_id != previous.contract_id:
            raise StateTransitionError("intent_contract_mismatch")
    if previous.episode_id is not None and nxt.episode_id != previous.episode_id:
        raise StateTransitionError("cannot_rebind_episode")
    if previous.episode_id is None and previous.pending_intent_ids:
        raise StateTransitionError("pending_intents_without_episode")
    pending = tuple(dict.fromkeys(
        tuple(nxt.pending_intent_ids) + tuple(intent.intent_id for intent in intents)))
    return StrategyState(
        strategy_id=previous.strategy_id, episode_id=nxt.episode_id,
        contract_id=previous.contract_id, phase=nxt.phase, setup=nxt.setup,
        last_regime=nxt.last_regime, direction=nxt.direction,
        filled_layers=nxt.filled_layers, layer_filled_lots=nxt.layer_filled_lots,
        pending_layer_index=nxt.pending_layer_index,
        active_exit_nano=nxt.active_exit_nano, pending_exit_nano=nxt.pending_exit_nano,
        pending_intent_ids=pending, budget=nxt.budget,
        confirmation_count=nxt.confirmation_count,
        cooldown_until_nano=previous.cooldown_until_nano,
        last_event_id=nxt.last_event_id,
    )
