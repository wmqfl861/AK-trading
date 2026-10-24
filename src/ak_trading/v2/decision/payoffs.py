"""Complete episode distributions conditioned on frozen ATR_1 path classes."""
from __future__ import annotations

import json
from dataclasses import dataclass, fields, replace
from datetime import date
from decimal import Decimal
from random import Random
from types import MappingProxyType
from typing import TYPE_CHECKING, Mapping

from ak_trading.v2.artifacts import canonical_json_bytes, sha256_hex, stable_id
from ak_trading.v2.clock import ClockError, horizon_window
from ak_trading.v2.contracts import (
    ForecastRequest, Horizon, MarketForecast, PathLabel, PayoffEstimate,
    PayoffPathResult, StrategyContext, StrategyProposal, TradingRule,
    OBSERVATION_FROM_REPLAY_P1_V2, PAYOFF_PATH_INTEGRITY_SUFFIXES,
    payoff_training_plan_sha256,
)
from ak_trading.v2.decision.execution import fee_amount
from ak_trading.v2.schemas import calibration_geometry_key, encode_record

if TYPE_CHECKING:  # replay imports payoffs; the annotation stays string-only
    from ak_trading.v2.decision.replay import ReplayResult

ZERO = Decimal("0")
PATHS = ("UP_FIRST", "DOWN_FIRST", "NEITHER", "AMBIGUOUS")
MINUTE_NANO = 60_000_000_000


def proposal_forecast(proposal: StrategyProposal, context: StrategyContext) -> MarketForecast | None:
    matches = [f for f in context.forecasts if f.forecast_id == proposal.forecast_id]
    return matches[0] if len(matches) == 1 else None


def payoff_event(proposal: StrategyProposal, context: StrategyContext) -> str:
    family = context.spec.family
    if family in ("REVERSION", "GRID", "MARTINGALE"):
        return "RETURN_BEFORE_BREAK"
    side = proposal.budget.side if proposal.budget else proposal.next_state.direction
    return ("UP" if side == 1 else "DOWN") + ("_BREAKOUT" if family == "BREAKOUT" else "_TREND")


def condition_key(proposal: StrategyProposal, context: StrategyContext) -> str:
    forecast = proposal_forecast(proposal, context)
    if forecast is None or proposal.budget is None or context.rule is None:
        raise ValueError("payoff_condition_missing_forecast_budget_or_rule")
    geometry = calibration_geometry_key(context.features, forecast.frozen_range,
                                        payoff_event(proposal, context))
    if geometry is None:
        raise ValueError("payoff_geometry_missing")
    budget = proposal.budget
    values = [context.features.product, proposal.strategy_id, context.spec.parameter_version,
              context.spec.mode, forecast.horizon.unit, forecast.horizon.value,
              context.features.session_kind, context.regime.base, proposal.next_state.phase,
              budget.side, list(proposal.next_state.layer_filled_lots), forecast.barrier_key,
              geometry, encode_record(forecast.horizon), context.rule.rule_id,
              str(budget.equity_at_start_cny * Decimal(str(context.spec.parameters["round_risk_ratio"]))),
              context.exit_binding.binding_sha256, EXIT_STATE_CONDITION_P1]
    return canonical_json_bytes(values).decode().rstrip("\n")


# 007 §5.2: P1 single-exit registrations record exactly OPEN_INITIAL; the full
# ExitState hash stays a source fact, never a per-row statistical bucket.
EXIT_STATE_CONDITION_P1 = "OPEN_INITIAL"


@dataclass(frozen=True)
class PayoffObservation:
    observation_id: str
    strategy_id: str
    contract_id: str
    product: str
    condition_key: str
    exit_binding_sha256: str
    exit_state_condition: str
    horizon: Horizon
    barrier_key: str
    geometry_key: str
    started_at_nano: int
    ended_at_nano: int
    available_at_nano: int
    block_id: str
    path_outcome: str
    traded: bool
    net_pnl_cny: Decimal
    peak_margin_cny: Decimal
    maximum_loss_cny: Decimal
    return_before_stop: bool | None
    budget_cny: Decimal
    source_sha256: str

    def __post_init__(self) -> None:
        import json
        values = json.loads(self.condition_key)
        if not isinstance(values, list) or len(values) != 18:
            raise ValueError("condition_key_shape")
        if canonical_json_bytes(values).decode().rstrip("\n") != self.condition_key:
            raise ValueError("condition_key_not_canonical")
        if (values[0] != self.product or values[1] != self.strategy_id
                or values[4:6] != [self.horizon.unit, self.horizon.value]
                or values[11] != self.barrier_key or values[12] != self.geometry_key
                or values[13] != encode_record(self.horizon) or values[15] != str(self.budget_cny)
                or values[16] != self.exit_binding_sha256
                or values[17] != self.exit_state_condition):
            raise ValueError("observation_condition_identity_mismatch")
        if len(self.exit_binding_sha256)!=64 or any(c not in "0123456789abcdef" for c in self.exit_binding_sha256):
            raise ValueError("observation_exit_binding_hash_invalid")
        if not self.exit_state_condition:
            raise ValueError("observation_exit_state_condition_required")
        if self.barrier_key != "ATR_1" or self.path_outcome not in PATHS:
            raise ValueError("path_partition_must_be_ATR_1")
        if not all(type(n) is int for n in (self.started_at_nano,self.ended_at_nano,self.available_at_nano)) \
                or not self.started_at_nano < self.ended_at_nano <= self.available_at_nano:
            raise ValueError("observation_not_mature")
        date.fromisoformat(self.block_id)
        for name in ("net_pnl_cny", "peak_margin_cny", "maximum_loss_cny", "budget_cny"):
            value = getattr(self,name)
            if not isinstance(value,Decimal) or not value.is_finite():
                raise ValueError("observation_amount_invalid")
        if min(self.peak_margin_cny,self.maximum_loss_cny,self.budget_cny) < 0:
            raise ValueError("negative_risk_amount")
        if type(self.traded) is not bool or (self.return_before_stop is not None and type(self.return_before_stop) is not bool):
            raise TypeError("observation_boolean_required")
        if not self.traded and any((self.net_pnl_cny,self.peak_margin_cny,self.maximum_loss_cny)):
            raise ValueError("untriggered_path_must_have_zero_cashflows")
        if len(self.source_sha256)!=64 or any(c not in "0123456789abcdef" for c in self.source_sha256):
            raise ValueError("observation_source_hash_invalid")


def observation_payload(row: PayoffObservation) -> dict:
    result = {f.name:getattr(row,f.name) for f in fields(row)}
    result["horizon"] = encode_record(row.horizon)
    for key in ("net_pnl_cny","peak_margin_cny","maximum_loss_cny","budget_cny"):
        result[key] = str(result[key])
    return result


# --- E1 sole P1 payoff producer (arbitration 2026-09-17 RULING 3) ----------
#
# Frozen rejection-reason namespaces (E1 deliverable; the frozen list lives in
# data/maintenance_artifacts/v2-emergency-e1-m4-20260917/implementation.md):
#   * "PAYOFF_SAMPLE_UNAVAILABLE:<reason>" -- sample-level exclusions. M5 may
#     catch ValueError by exactly this prefix and count them per window; the
#     original scope stays registered (N stays 0 / INSUFFICIENT).
#   * "PAYOFF_INTEGRITY:<reason>" -- identity tampering, ledger
#     non-conservation or broken proposal->decision->intent->receipt->fill->
#     checkpoint chains. Blocking: must fail the run.
#   * "PAYOFF_INTEGRITY_PROGRAM:<reason>" -- program-interface misuse of this
#     entry point. Blocking like INTEGRITY.
# M5 must catch ONLY the PAYOFF_SAMPLE_UNAVAILABLE: prefix; every other
# ValueError (and every TypeError raised below) must propagate uncaught.
OBSERVATION_FROM_REPLAY_VERSION = "observation-from-replay-p1-v1"
PAYOFF_SAMPLE_UNAVAILABLE_PREFIX = "PAYOFF_SAMPLE_UNAVAILABLE:"
PAYOFF_INTEGRITY_PREFIX = "PAYOFF_INTEGRITY:"
PAYOFF_INTEGRITY_PROGRAM_PREFIX = "PAYOFF_INTEGRITY_PROGRAM:"

# Frozen excludable reason suffixes.
_REASONS_UNAVAILABLE = frozenset({
    "DOMAIN_NOT_P1_SINGLE_LAYER_OPEN", "LABEL_NOT_MATURE", "START_RESIDUE_PRESENT",
    "MIXED_EPISODE_ACCOUNT", "EPISODE_NOT_CLOSED", "VALUATION_INCOMPLETE",
    "RULE_ORIGINAL_MISSING", "EVIDENCE_AFTER_TRAINING_END", "MATURITY_WINDOW_BELOW_MINIMUM",
})
# Frozen blocking reason suffixes (PAYOFF_INTEGRITY: namespace).
_REASONS_INTEGRITY = frozenset({
    "REPLAY_RECONCILIATION_FAILED", "PROPOSAL_NOT_UNIQUE_IN_REPLAY", "OPEN_TARGET_MISMATCH",
    "PROPOSAL_FORECAST_NOT_UNIQUE", "CONTEXT_REPLAY_FORECAST_CONFLICT", "IDENTITY_MISMATCH",
    "BARRIER_NOT_ATR_1", "LABEL_REQUEST_MISMATCH", "CONDITION_KEY_CONFLICT",
    "DECISION_CHAIN_MISSING", "INTENT_CHAIN_MISSING", "FILL_INTENT_OWNER_MISMATCH",
    "RECEIPT_CHAIN_MISSING", "CONFLICTING_DUPLICATE_FILL", "UNCOMMITTED_FILL",
    "FILL_RULE_HASH_MISMATCH", "FILL_RULE_PIT_MISMATCH", "FILL_ECONOMICS_MISMATCH",
    "FILL_OFFSET_MISMATCH", "EPISODE_DIRECTION_CHANGED", "CASH_CONSERVATION_MISMATCH",
    "EXIT_BINDING_CHANGED", "MATURITY_ORDER_BROKEN",
})
# Frozen blocking reason suffixes (PAYOFF_INTEGRITY_PROGRAM: namespace).
_REASONS_INTEGRITY_PROGRAM = frozenset({"BLOCK_ID_NOT_TRADING_DAY"})

P1_OPEN_FAMILIES = frozenset({"TREND", "BREAKOUT", "PULLBACK"})
MINIMUM_TRADE_MINUTES = 60
MINIMUM_TRADE_DAYS = 20
_REPLAY_TUPLE_FIELDS = ("features", "forecasts", "regimes", "proposals", "decisions",
                        "intents", "fills", "equity", "checkpoints", "execution_results")


def _unavailable(reason: str) -> ValueError:
    assert reason in _REASONS_UNAVAILABLE
    return ValueError(PAYOFF_SAMPLE_UNAVAILABLE_PREFIX + reason)


def _integrity(reason: str) -> ValueError:
    assert reason in _REASONS_INTEGRITY
    return ValueError(PAYOFF_INTEGRITY_PREFIX + reason)


def _integrity_program(reason: str) -> ValueError:
    assert reason in _REASONS_INTEGRITY_PROGRAM
    return ValueError(PAYOFF_INTEGRITY_PROGRAM_PREFIX + reason)


# E-P1 RULING 3.4: the eleven NEW blocking suffixes registered by M1 in the
# contracts layer (PAYOFF_PATH_INTEGRITY_SUFFIXES), raised through the same
# frozen "PAYOFF_INTEGRITY:" namespace. They are disjoint from E1's frozen
# 9 + 23 + 1 sets; SOURCE_BYTES_MISMATCH is the M5 source-recompute guard.
_REASONS_PATH_INTEGRITY = frozenset(PAYOFF_PATH_INTEGRITY_SUFFIXES)


def _path_integrity(reason: str) -> ValueError:
    assert reason in _REASONS_PATH_INTEGRITY
    return ValueError(PAYOFF_INTEGRITY_PREFIX + reason)


def _record_hash(record: object) -> str:
    return sha256_hex(canonical_json_bytes(encode_record(record)))


def _trading_minutes(sessions, start_nano: int, end_nano: int) -> int:
    total = 0
    for session in sessions:
        low = max(session.start_nano, start_nano)
        high = min(session.end_nano, end_nano)
        if high > low:
            total += (high - low + MINUTE_NANO - 1) // MINUTE_NANO
    return total


def _trading_days(sessions, start_nano: int, end_nano: int) -> int:
    return len({s.trading_day for s in sessions
                if s.start_nano < end_nano and s.end_nano > start_nano})


def _account_economics(account) -> tuple:
    return (account.account_id, account.initial_cash_cny, account.cash_cny,
            account.realized_gross_cny, account.fees_cny, account.slippage_cny,
            account.unrealized_cny, account.equity_cny, account.margin_cny,
            account.reserved_margin_cny, account.lots)


def _released(checkpoint) -> bool:
    """Episode-free, pending-free and reservation-free committed state."""
    return (not checkpoint.account.lots and not checkpoint.pending_intents
            and checkpoint.account.reserved_margin_cny == ZERO
            and not any(s.budget is not None or s.episode_id is not None
                        or s.pending_intent_ids for s in checkpoint.states))


def _rule_book(rule_originals):
    """§3.3 multi-rule originals: unique rule_id, content-addressed."""
    if not isinstance(rule_originals, tuple) or any(
            not isinstance(rule, TradingRule) for rule in rule_originals):
        raise TypeError("rule_originals_must_be_tuple_of_TradingRule")
    book: dict[str, tuple[TradingRule, str]] = {}
    for rule in rule_originals:
        if rule.rule_id in book:
            # Same id twice -- even with equal content -- is a conflict.
            raise _path_integrity("RULE_ORIGINAL_CONFLICT")
        book[rule.rule_id] = (rule, _record_hash(rule))
    return book


def _produce_from_replay(opened: StrategyProposal, context: StrategyContext,
                         request: ForecastRequest, label: PathLabel, replay: "ReplayResult",
                         training_end_nano: int, block_id: str,
                         rule_originals: tuple[TradingRule, ...] | None):
    """Legacy ReplayResult branch of the sole producer.

    ``rule_originals=None`` keeps the frozen seven-parameter single-rule
    interpretation byte-for-byte (producer v1, context.rule as the one
    provable original). An explicit tuple switches the SAME validation to the
    multi-rule originals (E-P1 RULING 2.4/3.3): every fill, valuation mark and
    checkpoint rule is verified against the provided book, per-fill fees and
    FIFO multipliers use each fill's own rule, and the source carries producer
    version observation-from-replay-p1-v2 with the full originals. The
    selected DecisionRecord->OrderIntent chain validation is unchanged in
    both sub-branches.
    """
    multi = rule_originals is not None
    book = _rule_book(rule_originals) if multi else None
    # -- replay-level integrity (3.4: ledger non-conservation blocks). The
    # replay-level "INCOMPLETE" flag may simply reflect an unclosed episode
    # (rejected later as sample-unavailable); the conservation facts
    # themselves are the blocking test.
    reconciliation = replay.reconciliation
    if (reconciliation.get("cash_difference_cny") != ZERO
            or reconciliation.get("equity_difference_cny") != ZERO
            or reconciliation.get("position_match") is not True
            or reconciliation.get("fees_match") is not True):
        raise _integrity("REPLAY_RECONCILIATION_FAILED")

    # -- 3.3.1: P1 single-layer first-OPEN support domain --
    same_id = [p for p in replay.proposals if p.proposal_id == opened.proposal_id]
    if len(same_id) != 1 or same_id[0] != opened:
        raise _integrity("PROPOSAL_NOT_UNIQUE_IN_REPLAY")
    if (opened.action != "OPEN" or opened.episode_id is None or opened.budget is None
            or opened.forecast_id is None or opened.layer_index != 0
            or context.spec.family not in P1_OPEN_FAMILIES
            or len(opened.budget.layers) != 1):
        raise _unavailable("DOMAIN_NOT_P1_SINGLE_LAYER_OPEN")
    if opened.target_lots != opened.budget.side * opened.budget.layers[0].lots:
        raise _integrity("OPEN_TARGET_MISMATCH")
    if any(i.purpose == "ADD" for i in replay.intents if i.episode_id == opened.episode_id):
        raise _unavailable("DOMAIN_NOT_P1_SINGLE_LAYER_OPEN")

    # -- 3.3.2: identity and the original ATR_1 label, item by item --
    replay_forecasts = [f for f in replay.forecasts if f.forecast_id == opened.forecast_id]
    if len(replay_forecasts) != 1:
        raise _integrity("PROPOSAL_FORECAST_NOT_UNIQUE")
    forecast = replay_forecasts[0]
    context_matches = [f for f in context.forecasts if f.forecast_id == opened.forecast_id]
    if len(context_matches) != 1 or context_matches[0] != forecast:
        raise _integrity("CONTEXT_REPLAY_FORECAST_CONFLICT")
    if not (context.now_nano == opened.decision_nano == request.features.decision_nano
            == forecast.decision_nano):
        raise _integrity("IDENTITY_MISMATCH")
    if request.features != context.features or forecast.snapshot_id != context.features.snapshot_id:
        raise _integrity("IDENTITY_MISMATCH")
    if not (opened.strategy_id == context.spec.strategy_id
            and forecast.contract_id == request.features.contract_id
            == opened.contract_id == context.features.contract_id
            and forecast.product == context.features.product):
        raise _integrity("IDENTITY_MISMATCH")
    if request.request_id != forecast.request_id:
        raise _integrity("IDENTITY_MISMATCH")
    if not (request.horizon == forecast.horizon == label.horizon):
        raise _integrity("LABEL_REQUEST_MISMATCH")
    if not (request.window_start_nano == forecast.window_start_nano
            == label.window_start_nano and request.window_end_nano == forecast.window_end_nano
            == label.window_end_nano and request.window_start_nano > opened.decision_nano):
        raise _integrity("LABEL_REQUEST_MISMATCH")
    if label.request_id != request.request_id or label.contract_id != request.features.contract_id:
        raise _integrity("LABEL_REQUEST_MISMATCH")
    if request.frozen_range != forecast.frozen_range:
        raise _integrity("IDENTITY_MISMATCH")
    if not (forecast.barrier_key == "ATR_1" == request.barrier_key
            and forecast.upper_distance == forecast.lower_distance
            == request.upper_distance == request.lower_distance
            == context.features.atr_points):
        raise _integrity("BARRIER_NOT_ATR_1")
    if label.status != "MATURE":
        raise _unavailable("LABEL_NOT_MATURE")
    if label.available_at_nano is None or label.path_outcome not in PATHS:
        raise _integrity("LABEL_REQUEST_MISMATCH")
    if context.rule is None:
        raise _unavailable("RULE_ORIGINAL_MISSING")
    rule = context.rule
    rule_digest = _record_hash(rule)
    if multi:
        # The decision-time entry judgment rule is always a consumed original.
        if rule.rule_id not in book:
            raise _unavailable("RULE_ORIGINAL_MISSING")
        if book[rule.rule_id][1] != rule_digest:
            raise _path_integrity("RULE_ORIGINAL_CONFLICT")
    if calibration_geometry_key(context.features, forecast.frozen_range,
                                payoff_event(opened, context)) is None:
        # Outside the provably conditionable domain (e.g. a range-free
        # BREAKOUT event with no frozen range): excluded, never a bare leak.
        raise _unavailable("DOMAIN_NOT_P1_SINGLE_LAYER_OPEN")
    key = condition_key(opened, context)
    values = json.loads(key)
    if (len(values) != 18 or values[16] != context.exit_binding.binding_sha256
            or values[17] != EXIT_STATE_CONDITION_P1):
        raise _integrity("CONDITION_KEY_CONFLICT")

    # -- 3.3.3: forward chain proposal->decision->intent->receipt->fill->checkpoint --
    selected = [d for d in replay.decisions if d.selected_proposal_id == opened.proposal_id]
    if len(selected) != 1 or opened not in selected[0].proposals:
        raise _integrity("DECISION_CHAIN_MISSING")
    decision = selected[0]
    episode_intents = tuple(i for i in replay.intents
                            if i.episode_id == opened.episode_id
                            and i.contract_id == opened.contract_id
                            and i.strategy_id == opened.strategy_id)
    decision_ids = {d.decision_id for d in replay.decisions}
    if any(i.decision_id not in decision_ids for i in episode_intents):
        raise _integrity("INTENT_CHAIN_MISSING")
    opens = tuple(i for i in episode_intents if i.offset == "OPEN")
    if len(opens) != 1 or opens[0].purpose != "ENTRY" or opens[0].decision_id != decision.decision_id:
        raise _integrity("INTENT_CHAIN_MISSING")
    entry = opens[0]
    checkpoints = tuple(sorted(replay.checkpoints, key=lambda c: c.sequence))
    # 3.3.4: mixed-account paths are rejected, never re-attributed.
    if any(f.episode_id != opened.episode_id for f in replay.fills):
        raise _unavailable("MIXED_EPISODE_ACCOUNT")
    by_fill: dict[str, tuple] = {}
    for fill in replay.fills:
        digest = _record_hash(fill)
        previous = by_fill.get(fill.fill_id)
        if previous is not None and previous[1] != digest:
            raise _integrity("CONFLICTING_DUPLICATE_FILL")
        if previous is None:
            by_fill[fill.fill_id] = (fill, digest)
    by_intent = {i.intent_id: i for i in replay.intents}
    receipts = tuple(r for r in replay.execution_results if r.fills)
    fill_rules: dict[str, TradingRule] = {}
    for fill, digest in by_fill.values():
        intent = by_intent.get(fill.intent_id)
        if (intent is None or intent.contract_id != fill.contract_id
                or intent.strategy_id != fill.strategy_id
                or intent.episode_id != fill.episode_id or intent.offset != fill.offset):
            raise _integrity("FILL_INTENT_OWNER_MISMATCH")
        if not any(any(other == fill for other in r.fills) for r in receipts
                   if r.intent_id == fill.intent_id):
            raise _integrity("RECEIPT_CHAIN_MISSING")
        committed = next((c for c in checkpoints
                          if fill.fill_id in c.processed_fill_hashes), None)
        if (committed is None or committed.processed_fill_hashes[fill.fill_id] != digest
                or committed.account.as_of_nano < fill.execution_nano):
            raise _integrity("UNCOMMITTED_FILL")
        # 3.3.4 / 3.2: single-rule keeps the ONE original carried by
        # context.rule; the explicit multi-rule branch verifies each fill
        # against its own original from the provided book.
        if multi:
            booked = book.get(fill.rule_id)
            if booked is None:
                raise _unavailable("RULE_ORIGINAL_MISSING")
            fill_rule = booked[0]
        else:
            if fill.rule_id != rule.rule_id:
                raise _unavailable("RULE_ORIGINAL_MISSING")
            fill_rule = rule
        fill_rules[fill.fill_id] = fill_rule
        if fill_rule.contract_id != fill.contract_id or not (
                fill_rule.available_at_nano <= fill.execution_nano
                and fill_rule.effective_from_nano <= fill.execution_nano < fill_rule.effective_until_nano):
            raise _integrity("FILL_RULE_PIT_MISMATCH")
        leg = {"OPEN": fill_rule.open_fee, "CLOSE": fill_rule.close_fee,
               "CLOSE_TODAY": fill_rule.close_today_fee}[fill.offset]
        if leg is None:
            raise _unavailable("RULE_ORIGINAL_MISSING")
        if (fill.lots % fill_rule.lot_step or fill.execution_price % fill_rule.price_tick
                or fill.fee_cny != fee_amount(leg, fill.execution_price, fill.lots, fill_rule.multiplier)
                or fill.slippage_cny != abs(fill.execution_price - fill.reference_price)
                * fill.lots * fill_rule.multiplier):
            raise _integrity("FILL_ECONOMICS_MISMATCH")
    for checkpoint in checkpoints:
        if multi:
            for rule_id, recorded in checkpoint.rule_hashes.items():
                booked = book.get(rule_id)
                if booked is None:
                    raise _unavailable("RULE_ORIGINAL_MISSING")
                if booked[1] != recorded:
                    raise _path_integrity("RULE_ORIGINAL_CONFLICT")
        elif rule.rule_id in checkpoint.rule_hashes and checkpoint.rule_hashes[rule.rule_id] != rule_digest:
            raise _integrity("FILL_RULE_HASH_MISMATCH")
        for mark in checkpoint.valuation_marks:
            if mark.rule_id is None:
                continue
            if multi:
                booked = book.get(mark.rule_id)
                if booked is None:
                    raise _unavailable("RULE_ORIGINAL_MISSING")
                if mark.valued_at_nano is not None and not (
                        booked[0].available_at_nano <= mark.valued_at_nano
                        and booked[0].effective_from_nano <= mark.valued_at_nano
                        < booked[0].effective_until_nano):
                    raise _path_integrity("RULE_PIT_MISMATCH")
            elif mark.rule_id != rule.rule_id:
                raise _unavailable("RULE_ORIGINAL_MISSING")
        binding = [b for b in checkpoint.exit_bindings if b.strategy_id == context.spec.strategy_id]
        if len(binding) != 1 or binding[0] != context.exit_binding:
            raise _integrity("EXIT_BINDING_CHANGED")
    if multi:
        consumed = ({rule.rule_id} | set(fill_rules[f.fill_id].rule_id for f, _ in by_fill.values())
                    | {rule_id for checkpoint in checkpoints for rule_id in checkpoint.rule_hashes}
                    | {mark.rule_id for checkpoint in checkpoints
                       for mark in checkpoint.valuation_marks if mark.rule_id is not None})
        if set(book) != consumed:
            # Unconsumed (e.g. future) originals posed as consumed sources.
            raise _path_integrity("RULE_ORIGINAL_CONFLICT")

    # -- 3.3.1 start residue: provable flat, pending-free, budget-free start --
    prior = next((c for c in reversed(checkpoints)
                  if c.account.as_of_nano < opened.decision_nano), None)
    if prior is not None:
        if (prior.account.lots or prior.pending_intents
                or any(s.episode_id is not None or s.budget is not None for s in prior.states)):
            raise _unavailable("START_RESIDUE_PRESENT")
        if _account_economics(prior.account) != _account_economics(context.account):
            raise _integrity("IDENTITY_MISMATCH")
    else:
        account = context.account
        if (account.lots or account.realized_gross_cny or account.fees_cny
                or account.slippage_cny or account.unrealized_cny or account.margin_cny
                or account.reserved_margin_cny
                or account.equity_cny != account.cash_cny or account.cash_cny != account.initial_cash_cny):
            raise _unavailable("START_RESIDUE_PRESENT")
    if context.account.quality.status != "READY":
        raise _unavailable("VALUATION_INCOMPLETE")
    equity_before = context.account.equity_cny

    # -- 3.3.3/3.3.8: one complete committed episode or one genuine zero path --
    ordered = tuple(sorted(by_fill.values(), key=lambda item: (item[0].execution_nano, item[0].fill_id)))
    episode_opens = tuple(f for f, _ in ordered if f.offset == "OPEN")
    first_fill_nano = ordered[0][0].execution_nano if ordered else None
    if episode_opens:
        traded = True
        if any(f.side != opened.budget.side or f.intent_id != entry.intent_id for f in episode_opens):
            raise _integrity("EPISODE_DIRECTION_CHANGED")
        queue: list[list] = []
        realized = ZERO
        for fill, _ in ordered:
            if fill.offset == "OPEN":
                queue.append([fill, fill.lots])
                continue
            remaining = fill.lots
            for held in queue:
                origin, quantity = held
                if not quantity or origin.side != -fill.side:
                    continue
                if (origin.trading_day == fill.trading_day) != (fill.offset == "CLOSE_TODAY"):
                    continue
                matched = min(quantity, remaining)
                realized += origin.side * matched * (fill.execution_price - origin.execution_price) \
                    * fill_rules[fill.fill_id].multiplier
                held[1] = quantity - matched
                remaining -= matched
                if not remaining:
                    break
            if remaining:
                raise _integrity("FILL_OFFSET_MISMATCH")
        if any(quantity for _, quantity in queue):
            raise _integrity("FILL_OFFSET_MISMATCH")
        fees_total = sum((f.fee_cny for f, _ in ordered), ZERO)
        net_pnl = realized - fees_total
        # 3.3.4: execution prices already carry slippage; the memo field is
        # never deducted a second time.
        peak_margin = max((a.margin_cny for a in replay.equity), default=ZERO)
    else:
        traded = False
        if ordered:
            raise _integrity("FILL_OFFSET_MISMATCH")
        entry_receipts = [r for r in replay.execution_results if r.intent_id == entry.intent_id]
        if not entry_receipts:
            raise _integrity("RECEIPT_CHAIN_MISSING")
        last = entry_receipts[-1]
        if last.status not in ("CANCELLED", "EXPIRED") or last.fills or last.remaining_lots != entry.delta_lots:
            raise _unavailable("EPISODE_NOT_CLOSED")
        if any(i.offset != "OPEN" for i in episode_intents):
            raise _integrity("INTENT_CHAIN_MISSING")
        net_pnl = peak_margin = maximum_loss = ZERO

    # -- 3.3.3/3.3.6: closure, release and maturity ordering --
    if replay.status != "COMPLETE":
        raise _unavailable("EPISODE_NOT_CLOSED")
    if not checkpoints or not _released(checkpoints[-1]):
        raise _unavailable("EPISODE_NOT_CLOSED")
    fill_ids = set(by_fill)
    closed = next((c for c in checkpoints if _released(c) and fill_ids <= set(c.processed_fill_hashes)
                   and c.account.as_of_nano >= opened.earliest_execution_nano), None)
    if closed is None:
        raise _unavailable("EPISODE_NOT_CLOSED")
    closed_at = closed.account.as_of_nano
    if traded:
        net_after = closed.account.cash_cny
        if net_after - context.account.cash_cny != net_pnl:
            raise _integrity("CASH_CONSERVATION_MISMATCH")
        # 3.3.4: maximum_loss is the worst actual path drawdown of THIS round
        # relative to the pre-entry equity, fees included. The reliable
        # sequence must cover the holding period itself -- terminal-only
        # valuations never replace path loss, and a mid-episode valuation gap
        # rejects instead of computing a mixed amount.
        holding_ready = [a.equity_cny for a in replay.equity
                         if first_fill_nano <= a.as_of_nano and a.lots
                         and a.quality.status == "READY"]
        terminal_ready = [a.equity_cny for a in replay.equity
                          if first_fill_nano <= a.as_of_nano <= closed_at and not a.lots
                          and a.quality.status == "READY"]
        if not holding_ready or not terminal_ready:
            raise _unavailable("VALUATION_INCOMPLETE")
        maximum_loss = max(ZERO, equity_before - min(holding_ready + terminal_ready))
    started = opened.decision_nano
    ended = max(request.window_end_nano, closed_at)
    available = max(label.available_at_nano, closed_at, rule.available_at_nano)
    if not started < ended <= available:
        raise _integrity("MATURITY_ORDER_BROKEN")
    if available > training_end_nano:
        raise _unavailable("EVIDENCE_AFTER_TRAINING_END")
    # 3.3.6: the early exit never shortens the original training window.
    effective_end = max(label.window_end_nano, opened.budget.latest_exit_nano, available)
    if label.horizon.unit == "TRADING_MINUTE":
        if _trading_minutes(context.sessions, started, effective_end) < MINIMUM_TRADE_MINUTES:
            raise _unavailable("MATURITY_WINDOW_BELOW_MINIMUM")
    else:
        if _trading_days(context.sessions, started, effective_end) < MINIMUM_TRADE_DAYS:
            raise _unavailable("MATURITY_WINDOW_BELOW_MINIMUM")

    # -- 3.3.5: no proven regression target precedes the stop in P1 families --
    return_before_stop = None

    # -- 3.3.9: canonical source projection, deterministic identity --
    source = {
        "producer_version": OBSERVATION_FROM_REPLAY_P1_V2 if multi
        else OBSERVATION_FROM_REPLAY_VERSION,
        "block_id": block_id,
        "training_end_nano": training_end_nano,
        "opened": encode_record(opened),
        "context": encode_record(context),
        "spec": encode_record(context.spec),
        "exit_binding": encode_record(context.exit_binding),
        "rule_original" if not multi else "rule_originals": encode_record(rule) if not multi
        else [encode_record(book[rule_id][0])
              for rule_id in sorted(book, key=lambda k: (k, book[k][1]))],
        "request": encode_record(request),
        "label": encode_record(label),
        "replay": {
            "status": replay.status,
            "reconciliation": {name: str(value) if isinstance(value, Decimal) else value
                               for name, value in replay.reconciliation.items()},
            "features": [encode_record(row) for row in replay.features],
            "forecasts": [encode_record(row) for row in replay.forecasts],
            "regimes": [encode_record(row) for row in replay.regimes],
            "proposals": [encode_record(row) for row in replay.proposals],
            "decisions": [encode_record(row) for row in replay.decisions],
            "intents": [encode_record(row) for row in replay.intents],
            "fills": [encode_record(row) for row in replay.fills],
            "execution_results": [encode_record(row) for row in replay.execution_results],
            "equity": [encode_record(row) for row in replay.equity],
            "checkpoints": [encode_record(row) for row in checkpoints],
        },
        "episode_facts": {
            "selected_decision_id": decision.decision_id,
            "entry_intent_id": entry.intent_id,
            "traded": traded,
            "first_fill_execution_nano": first_fill_nano,
            "fill_trading_days": [str(f.trading_day) for f, _ in ordered],
            "episode_closed_and_released_at_nano": closed_at,
            "net_pnl_cny": str(net_pnl),
            "peak_margin_cny": str(peak_margin),
            "maximum_loss_cny": str(maximum_loss),
        },
    }
    facts = {"key": key, "values": values, "forecast": forecast, "started": started,
             "ended": ended, "available": available, "traded": traded, "net_pnl": net_pnl,
             "peak_margin": peak_margin, "maximum_loss": maximum_loss,
             "return_before_stop": return_before_stop}
    return facts, source


def _produce_from_path(opened: StrategyProposal, context: StrategyContext,
                       request: ForecastRequest, label: PathLabel, path: PayoffPathResult,
                       training_end_nano: int, block_id: str,
                       rule_originals: tuple[TradingRule, ...]):
    """PayoffPathResult branch of the sole producer (E-P1 RULING 2.4/3).

    Verifies the unique real M3B candidate -> PayoffPathDecision ->
    PayoffPathOrder -> ExecutionResult -> Fill -> PayoffPathCheckpoint chain,
    the multi-rule originals economics per fill/mark/checkpoint, the isolated
    initial account, conservation and maturity -- never synthesizing a normal
    DecisionRecord for the legacy chain.
    """
    book = _rule_book(rule_originals)
    # §2.3/§3.4 chain: source kind, sampling plan and candidate identity.
    if path.source_kind != "P1_ACTION_CONDITIONAL_REPLAY":
        raise _path_integrity("SOURCE_KIND_MISMATCH")
    if (path.plan.strategy_id != context.spec.strategy_id
            or path.plan.exit_binding_sha256 != context.exit_binding.binding_sha256
            or path.plan.training_end_nano != training_end_nano
            or not (path.plan.window_start_nano <= opened.decision_nano
                    < path.plan.window_end_nano)):
        raise _path_integrity("SAMPLING_PLAN_MISMATCH")
    entries = [d for d in path.path_decisions if d.action == "MEASURE_ENTRY"]
    if len(entries) != 1:
        raise _path_integrity("PATH_DECISION_CHAIN_MISSING")
    entry_decision = entries[0]
    if entry_decision.plan_sha256 != payoff_training_plan_sha256(path.plan):
        raise _path_integrity("SAMPLING_PLAN_MISMATCH")
    if (entry_decision.proposal != opened or entry_decision.context != context
            or entry_decision.request != request):
        raise _path_integrity("CANDIDATE_SOURCE_MISMATCH")
    reconciliation = path.reconciliation
    if (reconciliation.get("cash_difference_cny") != ZERO
            or reconciliation.get("equity_difference_cny") != ZERO
            or reconciliation.get("position_match") is not True
            or reconciliation.get("fees_match") is not True):
        raise _integrity("REPLAY_RECONCILIATION_FAILED")
    # -- 3.3.1: P1 single-layer first-OPEN support domain --
    same_id = [p for p in path.proposals if p.proposal_id == opened.proposal_id]
    if len(same_id) != 1 or same_id[0] != opened:
        raise _integrity("PROPOSAL_NOT_UNIQUE_IN_REPLAY")
    if (opened.action != "OPEN" or opened.episode_id is None or opened.budget is None
            or opened.forecast_id is None or opened.layer_index != 0
            or context.spec.family not in P1_OPEN_FAMILIES
            or len(opened.budget.layers) != 1):
        raise _unavailable("DOMAIN_NOT_P1_SINGLE_LAYER_OPEN")
    if opened.target_lots != opened.budget.side * opened.budget.layers[0].lots:
        raise _integrity("OPEN_TARGET_MISMATCH")
    if any(o.purpose == "ADD" for o in path.orders if o.episode_id == opened.episode_id):
        raise _unavailable("DOMAIN_NOT_P1_SINGLE_LAYER_OPEN")
    # -- 3.3.2: identity and the original ATR_1 label --
    path_forecasts = [f for f in path.forecasts if f.forecast_id == opened.forecast_id]
    if len(path_forecasts) != 1:
        raise _integrity("PROPOSAL_FORECAST_NOT_UNIQUE")
    forecast = path_forecasts[0]
    context_matches = [f for f in context.forecasts if f.forecast_id == opened.forecast_id]
    if len(context_matches) != 1 or context_matches[0] != forecast:
        raise _integrity("CONTEXT_REPLAY_FORECAST_CONFLICT")
    if not (context.now_nano == opened.decision_nano == request.features.decision_nano
            == forecast.decision_nano == entry_decision.observed_at_nano):
        raise _integrity("IDENTITY_MISMATCH")
    if request.features != context.features or forecast.snapshot_id != context.features.snapshot_id:
        raise _integrity("IDENTITY_MISMATCH")
    if not (opened.strategy_id == context.spec.strategy_id
            and forecast.contract_id == request.features.contract_id
            == opened.contract_id == context.features.contract_id
            and forecast.product == context.features.product):
        raise _integrity("IDENTITY_MISMATCH")
    if request.request_id != forecast.request_id:
        raise _integrity("IDENTITY_MISMATCH")
    if not (request.horizon == forecast.horizon == label.horizon):
        raise _integrity("LABEL_REQUEST_MISMATCH")
    if not (request.window_start_nano == forecast.window_start_nano
            == label.window_start_nano and request.window_end_nano == forecast.window_end_nano
            == label.window_end_nano and request.window_start_nano > opened.decision_nano):
        raise _integrity("LABEL_REQUEST_MISMATCH")
    if label.request_id != request.request_id or label.contract_id != request.features.contract_id:
        raise _integrity("LABEL_REQUEST_MISMATCH")
    if request.frozen_range != forecast.frozen_range:
        raise _integrity("IDENTITY_MISMATCH")
    if not (forecast.barrier_key == "ATR_1" == request.barrier_key
            and forecast.upper_distance == forecast.lower_distance
            == request.upper_distance == request.lower_distance
            == context.features.atr_points):
        raise _integrity("BARRIER_NOT_ATR_1")
    if label.status != "MATURE":
        raise _unavailable("LABEL_NOT_MATURE")
    if label.available_at_nano is None or label.path_outcome not in PATHS:
        raise _integrity("LABEL_REQUEST_MISMATCH")
    if context.rule is None:
        raise _unavailable("RULE_ORIGINAL_MISSING")
    rule = context.rule
    rule_digest = _record_hash(rule)
    if calibration_geometry_key(context.features, forecast.frozen_range,
                                payoff_event(opened, context)) is None:
        raise _unavailable("DOMAIN_NOT_P1_SINGLE_LAYER_OPEN")
    key = condition_key(opened, context)
    values = json.loads(key)
    if (len(values) != 18 or values[16] != context.exit_binding.binding_sha256
            or values[17] != EXIT_STATE_CONDITION_P1):
        raise _integrity("CONDITION_KEY_CONFLICT")
    # -- path order chain: every order resolves into this package's decisions --
    decision_ids = {d.path_decision_id for d in path.path_decisions}
    entry_orders = [o for o in path.orders
                    if o.path_decision_id == entry_decision.path_decision_id]
    opens = tuple(o for o in entry_orders if o.offset == "OPEN")
    if (len(opens) != 1 or opens[0].purpose != "ENTRY"
            or opens[0].episode_id != opened.episode_id
            or opens[0].contract_id != opened.contract_id
            or opens[0].strategy_id != opened.strategy_id):
        raise _path_integrity("PATH_ORDER_CHAIN_MISSING")
    entry = opens[0]
    if any(o.path_decision_id not in decision_ids for o in path.orders):
        raise _path_integrity("PATH_ORDER_CHAIN_MISSING")
    episode_orders = tuple(o for o in path.orders if o.episode_id == opened.episode_id
                           and o.contract_id == opened.contract_id
                           and o.strategy_id == opened.strategy_id)
    # -- fills: uniqueness, ownership, receipts, commits --
    by_fill: dict[str, tuple] = {}
    for fill in path.fills:
        digest = _record_hash(fill)
        previous = by_fill.get(fill.fill_id)
        if previous is not None and previous[1] != digest:
            raise _integrity("CONFLICTING_DUPLICATE_FILL")
        if previous is None:
            by_fill[fill.fill_id] = (fill, digest)
    by_order = {o.intent_id: o for o in path.orders}
    if any(fill.intent_id not in by_order for fill, _ in by_fill.values()):
        raise _path_integrity("PATH_ORDER_CHAIN_MISSING")
    for fill, digest in by_fill.values():
        order = by_order[fill.intent_id]
        if (order.contract_id != fill.contract_id or order.strategy_id != fill.strategy_id
                or order.episode_id != fill.episode_id or order.offset != fill.offset):
            raise _integrity("FILL_INTENT_OWNER_MISMATCH")
    if any(r.intent_id not in by_order for r in path.execution_results):
        raise _path_integrity("PATH_ORDER_CHAIN_MISSING")
    commits = {c.execution_index: c for c in path.receipt_commits}
    if len(commits) != len(path.receipt_commits) or set(commits) != set(
            range(len(path.execution_results))):
        raise _path_integrity("RECEIPT_COMMIT_MISMATCH")
    checkpoint_hashes = {c.checkpoint_hash for c in path.checkpoints}
    for index, commit in commits.items():
        if (commit.checkpoint_sha256 not in checkpoint_hashes
                or commit.observed_at_nano < entry_decision.observed_at_nano):
            raise _path_integrity("RECEIPT_COMMIT_MISMATCH")
        for fill in path.execution_results[index].fills:
            if fill.execution_nano > commit.observed_at_nano:
                raise _path_integrity("RECEIPT_COMMIT_MISMATCH")
    checkpoints = tuple(sorted(path.checkpoints, key=lambda c: c.sequence))
    for fill, digest in by_fill.values():
        committed = next((c for c in checkpoints
                          if fill.fill_id in c.processed_fill_hashes), None)
        if (committed is None or committed.processed_fill_hashes[fill.fill_id] != digest
                or committed.account.as_of_nano < fill.execution_nano):
            raise _integrity("UNCOMMITTED_FILL")
    if any(not any(commit.observed_at_nano >= f.execution_nano
                   for commit in commits.values()) for f, _ in by_fill.values()):
        raise _integrity("RECEIPT_CHAIN_MISSING")
    # -- isolated initial account: decision anchor and flat start --
    if entry_decision.account_before_sha256 != _record_hash(context.account):
        raise _path_integrity("INITIAL_ACCOUNT_MISMATCH")
    if context.account.account_id != path.initial_account.account_id:
        raise _path_integrity("INITIAL_ACCOUNT_MISMATCH")
    prior = next((c for c in reversed(checkpoints)
                  if c.account.as_of_nano < opened.decision_nano), None)
    if prior is not None:
        if (prior.account.lots or prior.pending_intents
                or any(s.episode_id is not None or s.budget is not None for s in prior.states)):
            raise _path_integrity("INITIAL_ACCOUNT_MISMATCH")
        if _account_economics(prior.account) != _account_economics(context.account):
            raise _path_integrity("INITIAL_ACCOUNT_MISMATCH")
    elif _account_economics(context.account) != _account_economics(path.initial_account):
        raise _path_integrity("INITIAL_ACCOUNT_MISMATCH")
    if context.account.quality.status != "READY":
        raise _unavailable("VALUATION_INCOMPLETE")
    equity_before = context.account.equity_cny
    # -- multi-rule originals: book content, per-fill/mark/checkpoint economics --
    if [(r.rule_id, _record_hash(r)) for r in sorted(
            rule_originals, key=lambda r: (r.rule_id, _record_hash(r)))] != [
            (r.rule_id, _record_hash(r)) for r in sorted(
                path.rule_originals, key=lambda r: (r.rule_id, _record_hash(r)))]:
        raise _path_integrity("RULE_ORIGINAL_CONFLICT")
    if rule.rule_id not in book:
        raise _path_integrity("RULE_CONSUMPTION_MISMATCH")
    if book[rule.rule_id][1] != rule_digest:
        raise _path_integrity("RULE_ORIGINAL_CONFLICT")
    fill_rules: dict[str, TradingRule] = {}
    for fill, _digest in by_fill.values():
        booked = book.get(fill.rule_id)
        if booked is None:
            raise _path_integrity("RULE_CONSUMPTION_MISMATCH")
        fill_rule = booked[0]
        fill_rules[fill.fill_id] = fill_rule
        if fill_rule.contract_id != fill.contract_id or not (
                fill_rule.available_at_nano <= fill.execution_nano
                and fill_rule.effective_from_nano <= fill.execution_nano
                < fill_rule.effective_until_nano):
            raise _path_integrity("RULE_PIT_MISMATCH")
        leg = {"OPEN": fill_rule.open_fee, "CLOSE": fill_rule.close_fee,
               "CLOSE_TODAY": fill_rule.close_today_fee}[fill.offset]
        if leg is None:
            raise _path_integrity("RULE_CONSUMPTION_MISMATCH")
        if (fill.lots % fill_rule.lot_step or fill.execution_price % fill_rule.price_tick
                or fill.fee_cny != fee_amount(leg, fill.execution_price, fill.lots,
                                              fill_rule.multiplier)
                or fill.slippage_cny != abs(fill.execution_price - fill.reference_price)
                * fill.lots * fill_rule.multiplier):
            raise _integrity("FILL_ECONOMICS_MISMATCH")
    for checkpoint in checkpoints:
        for rule_id, recorded in checkpoint.rule_hashes.items():
            booked = book.get(rule_id)
            if booked is None:
                raise _path_integrity("RULE_CONSUMPTION_MISMATCH")
            if booked[1] != recorded:
                raise _path_integrity("RULE_ORIGINAL_CONFLICT")
        for mark in checkpoint.valuation_marks:
            if mark.rule_id is None:
                continue
            booked = book.get(mark.rule_id)
            if booked is None:
                raise _path_integrity("RULE_CONSUMPTION_MISMATCH")
            if mark.valued_at_nano is not None and not (
                    booked[0].available_at_nano <= mark.valued_at_nano
                    and booked[0].effective_from_nano <= mark.valued_at_nano
                    < booked[0].effective_until_nano):
                raise _path_integrity("RULE_PIT_MISMATCH")
        binding = [b for b in checkpoint.exit_bindings if b.strategy_id == context.spec.strategy_id]
        if len(binding) != 1 or binding[0] != context.exit_binding:
            raise _integrity("EXIT_BINDING_CHANGED")
    consumed = ({rule.rule_id} | set(fill_rules[f.fill_id].rule_id for f, _ in by_fill.values())
                | {rule_id for checkpoint in checkpoints for rule_id in checkpoint.rule_hashes}
                | {mark.rule_id for checkpoint in checkpoints
                   for mark in checkpoint.valuation_marks if mark.rule_id is not None})
    if set(book) != consumed:
        raise _path_integrity("RULE_CONSUMPTION_MISMATCH")
    # -- 3.3.3/3.3.6: an unfinished path is a candidate-stage exclusion, never
    # a broken offset chain; the measurement result preserves its real state.
    if path.status != "COMPLETE":
        raise _unavailable("EPISODE_NOT_CLOSED")
    # -- 3.3.3/3.3.8: one complete committed episode or one genuine zero path --
    ordered = tuple(sorted(by_fill.values(), key=lambda item: (item[0].execution_nano, item[0].fill_id)))
    episode_opens = tuple(f for f, _ in ordered if f.offset == "OPEN")
    first_fill_nano = ordered[0][0].execution_nano if ordered else None
    if episode_opens:
        traded = True
        if any(f.side != opened.budget.side or f.intent_id != entry.intent_id for f in episode_opens):
            raise _integrity("EPISODE_DIRECTION_CHANGED")
        queue: list[list] = []
        realized = ZERO
        for fill, _ in ordered:
            if fill.offset == "OPEN":
                queue.append([fill, fill.lots])
                continue
            remaining = fill.lots
            for held in queue:
                origin, quantity = held
                if not quantity or origin.side != -fill.side:
                    continue
                if (origin.trading_day == fill.trading_day) != (fill.offset == "CLOSE_TODAY"):
                    continue
                matched = min(quantity, remaining)
                realized += origin.side * matched * (fill.execution_price - origin.execution_price) \
                    * fill_rules[fill.fill_id].multiplier
                held[1] = quantity - matched
                remaining -= matched
                if not remaining:
                    break
            if remaining:
                raise _integrity("FILL_OFFSET_MISMATCH")
        if any(quantity for _, quantity in queue):
            raise _integrity("FILL_OFFSET_MISMATCH")
        fees_total = sum((f.fee_cny for f, _ in ordered), ZERO)
        net_pnl = realized - fees_total
        # 3.3.4: execution prices already carry slippage; the memo field is
        # never deducted a second time.
        peak_margin = max((a.margin_cny for a in path.equity), default=ZERO)
    else:
        traded = False
        if ordered:
            raise _integrity("FILL_OFFSET_MISMATCH")
        entry_receipts = [r for r in path.execution_results if r.intent_id == entry.intent_id]
        if not entry_receipts:
            raise _integrity("RECEIPT_CHAIN_MISSING")
        last = entry_receipts[-1]
        if last.status not in ("CANCELLED", "EXPIRED") or last.fills or last.remaining_lots != entry.delta_lots:
            raise _unavailable("EPISODE_NOT_CLOSED")
        if any(o.offset != "OPEN" for o in episode_orders):
            raise _path_integrity("PATH_ORDER_CHAIN_MISSING")
        net_pnl = peak_margin = maximum_loss = ZERO
    # -- 3.3.3/3.3.6: closure, release and maturity ordering --
    if not checkpoints or not _released(checkpoints[-1]):
        raise _unavailable("EPISODE_NOT_CLOSED")
    fill_ids = set(by_fill)
    closed = next((c for c in checkpoints if _released(c) and fill_ids <= set(c.processed_fill_hashes)
                   and c.account.as_of_nano >= opened.earliest_execution_nano), None)
    if closed is None:
        raise _unavailable("EPISODE_NOT_CLOSED")
    closed_at = closed.account.as_of_nano
    if traded:
        net_after = closed.account.cash_cny
        if net_after - context.account.cash_cny != net_pnl:
            raise _integrity("CASH_CONSERVATION_MISMATCH")
        holding_ready = [a.equity_cny for a in path.equity
                         if first_fill_nano <= a.as_of_nano and a.lots
                         and a.quality.status == "READY"]
        terminal_ready = [a.equity_cny for a in path.equity
                          if first_fill_nano <= a.as_of_nano <= closed_at and not a.lots
                          and a.quality.status == "READY"]
        if not holding_ready or not terminal_ready:
            raise _unavailable("VALUATION_INCOMPLETE")
        maximum_loss = max(ZERO, equity_before - min(holding_ready + terminal_ready))
    started = opened.decision_nano
    ended = max(request.window_end_nano, closed_at)
    # §3.3: availability waits for the label, the release, every receipt
    # commit and every consumed original's visible time.
    available_marks = [label.available_at_nano, closed_at, rule.available_at_nano,
                       max(commit.observed_at_nano for commit in commits.values())]
    available_marks.extend(original.available_at_nano
                           for original, _digest in book.values())
    available = max(available_marks)
    if not started < ended <= available:
        raise _integrity("MATURITY_ORDER_BROKEN")
    if available > training_end_nano:
        raise _unavailable("EVIDENCE_AFTER_TRAINING_END")
    # 3.3.6: the early exit never shortens the original training window.
    effective_end = max(label.window_end_nano, opened.budget.latest_exit_nano, available)
    if label.horizon.unit == "TRADING_MINUTE":
        if _trading_minutes(path.sessions, started, effective_end) < MINIMUM_TRADE_MINUTES:
            raise _unavailable("MATURITY_WINDOW_BELOW_MINIMUM")
    else:
        if _trading_days(path.sessions, started, effective_end) < MINIMUM_TRADE_DAYS:
            # R3 DAY5 maturity extension: the actual five-day label and
            # evidence times stay unchanged. Only a fully verified P1 DAILY5
            # path may use the frozen calendar's conservative DAY20 support
            # endpoint when the original individual-window floor fails.
            daily5 = Horizon("TRADING_DAY", 5)
            if not (context.spec.mode == "DAILY"
                    and context.spec.max_holding == daily5
                    and request.horizon == label.horizon == daily5
                    and context.features.duration_seconds == 86400
                    and context.features.session_kind == "DAILY"):
                raise _unavailable("MATURITY_WINDOW_BELOW_MINIMUM")
            try:
                _support_start, support_end = horizon_window(
                    started, context.features.product,
                    Horizon("TRADING_DAY", MINIMUM_TRADE_DAYS), path.sessions)
            except ClockError as error:
                if str(error) in ("horizon_exceeds_calendar", "no_trading_days_in_calendar"):
                    raise _unavailable("MATURITY_WINDOW_BELOW_MINIMUM") from error
                raise
            if max(support_end, effective_end) > training_end_nano:
                raise _unavailable("MATURITY_WINDOW_BELOW_MINIMUM")
    return_before_stop = None
    source = {
        "producer_version": OBSERVATION_FROM_REPLAY_P1_V2,
        "source_kind": path.source_kind,
        "path_id": path.path_id,
        "plan": encode_record(path.plan),
        "plan_sha256": payoff_training_plan_sha256(path.plan),
        "selection_rule": path.plan.selection_rule,
        "block_id": block_id,
        "training_end_nano": training_end_nano,
        "opened": encode_record(opened),
        "context": encode_record(context),
        "spec": encode_record(context.spec),
        "exit_binding": encode_record(context.exit_binding),
        "rule_originals": [encode_record(book[rule_id][0])
                           for rule_id in sorted(book, key=lambda k: (k, book[k][1]))],
        "request": encode_record(request),
        "label": encode_record(label),
        "initial_account": encode_record(path.initial_account),
        "path": {
            "input_manifest_sha256": path.input_manifest_sha256,
            "features": [encode_record(row) for row in path.features],
            "forecasts": [encode_record(row) for row in path.forecasts],
            "regimes": [encode_record(row) for row in path.regimes],
            "proposals": [encode_record(row) for row in path.proposals],
            "candidate_checks": {pid: {domain: list(reasons) for domain, reasons
                                       in sorted(domains.items())}
                                 for pid, domains in path.candidate_checks.items()},
            "exit_specs": [encode_record(row) for row in path.exit_specs],
            "exit_features": [encode_record(row) for row in path.exit_features],
            "exit_proposals": [encode_record(row) for row in path.exit_proposals],
            "path_decisions": [encode_record(row) for row in path.path_decisions],
            "orders": [encode_record(row) for row in path.orders],
            "fills": [encode_record(row) for row in path.fills],
            "execution_results": [encode_record(row) for row in path.execution_results],
            "receipt_commits": [encode_record(row) for row in path.receipt_commits],
            "equity": [encode_record(row) for row in path.equity],
            "checkpoints": [encode_record(row) for row in checkpoints],
            "rule_originals": [encode_record(row) for row in path.rule_originals],
            "bar_originals": [encode_record(row) for row in path.bar_originals],
            "sessions": [encode_record(row) for row in path.sessions],
            "exclusions": list(path.exclusions),
            "reconciliation": {name: str(value) if isinstance(value, Decimal) else value
                               for name, value in path.reconciliation.items()},
            "status": path.status,
        },
        "episode_facts": {
            "path_decision_id": entry_decision.path_decision_id,
            "entry_intent_id": entry.intent_id,
            "traded": traded,
            "first_fill_execution_nano": first_fill_nano,
            "fill_trading_days": [str(f.trading_day) for f, _ in ordered],
            "episode_closed_and_released_at_nano": closed_at,
            "net_pnl_cny": str(net_pnl),
            "peak_margin_cny": str(peak_margin),
            "maximum_loss_cny": str(maximum_loss),
        },
    }
    facts = {"key": key, "values": values, "forecast": forecast, "started": started,
             "ended": ended, "available": available, "traded": traded, "net_pnl": net_pnl,
             "peak_margin": peak_margin, "maximum_loss": maximum_loss,
             "return_before_stop": return_before_stop}
    return facts, source


def _produce(opened: StrategyProposal, context: StrategyContext, request: ForecastRequest,
             label: PathLabel, replay: "ReplayResult | PayoffPathResult", training_end_nano: int,
             block_id: str, rule_originals: tuple[TradingRule, ...] | None):
    """ONE shared definition of the strict validation and the canonical source
    projection for both public entry points (E-P1 RULING 2.4)."""
    # -- program-interface type discipline (TypeError keeps failing in M5) --
    if not isinstance(opened, StrategyProposal):
        raise TypeError("observation_from_replay_opened_must_be_StrategyProposal")
    if not isinstance(context, StrategyContext):
        raise TypeError("observation_from_replay_context_must_be_StrategyContext")
    if not isinstance(request, ForecastRequest):
        raise TypeError("observation_from_replay_request_must_be_ForecastRequest")
    if not isinstance(label, PathLabel):
        raise TypeError("observation_from_replay_label_must_be_PathLabel")
    if rule_originals is not None and (not isinstance(rule_originals, tuple) or any(
            not isinstance(rule, TradingRule) for rule in rule_originals)):
        raise TypeError("observation_from_replay_rule_originals_must_be_tuple_of_TradingRule")
    if type(training_end_nano) is not int:
        raise TypeError("observation_from_replay_training_end_must_be_int_not_bool")
    if not isinstance(block_id, str):
        raise TypeError("observation_from_replay_block_id_must_be_str")
    # 3.3.7: block_id is exactly the explicit context trading day (an ISO date;
    # night-session rows use the trading day, never the natural date).
    try:
        date.fromisoformat(block_id)
    except ValueError:
        raise _integrity_program("BLOCK_ID_NOT_TRADING_DAY") from None
    if block_id != str(context.features.trading_day):
        raise _integrity_program("BLOCK_ID_NOT_TRADING_DAY")
    if isinstance(replay, PayoffPathResult):
        # §2.4: the new branch requires the explicit keyword originals; an
        # explicitly empty tuple is meaningful (and must match the result).
        if rule_originals is None:
            raise TypeError("observation_from_replay_path_requires_rule_originals")
        return _produce_from_path(opened, context, request, label, replay,
                                  training_end_nano, block_id, rule_originals)
    for name in (*_REPLAY_TUPLE_FIELDS, "reconciliation", "status"):
        if not hasattr(replay, name):
            raise TypeError("observation_from_replay_replay_must_be_ReplayResult")
    for name in _REPLAY_TUPLE_FIELDS:
        if not isinstance(getattr(replay, name), tuple):
            raise TypeError("observation_from_replay_replay_fields_must_be_tuples")
    return _produce_from_replay(opened, context, request, label, replay,
                                training_end_nano, block_id, rule_originals)


def observation_from_replay(
    opened: StrategyProposal,
    context: StrategyContext,
    request: ForecastRequest,
    label: PathLabel,
    replay: "ReplayResult | PayoffPathResult",
    training_end_nano: int,
    block_id: str,
    *,
    rule_originals: tuple[TradingRule, ...] | None = None,
) -> PayoffObservation:
    """Sole producer of P1 payoff observations from one frozen replay window.

    Implements arbitration v2-emergency-payoff-import-arbitration-20260917-r1
    RULING 3.3 items 1-9 and E-P1 v2-ep1-training-path-20260917-r1 RULING 2.4:
    the legacy seven positional parameters keep their exact frozen meaning
    (ReplayResult, single rule via context.rule, producer v1); an explicit
    ``rule_originals`` tuple or a ``PayoffPathResult`` selects the multi-rule
    measurement branches (producer observation-from-replay-p1-v2). No file,
    network, order or registry side effects, no defaults, no placeholder
    records. Identical evidence yields byte-identical fields, source hash and
    observation id.
    """
    facts, source = _produce(opened, context, request, label, replay, training_end_nano,
                             block_id, rule_originals)
    source_sha256 = sha256_hex(canonical_json_bytes(source))
    values = facts["values"]
    return PayoffObservation(
        stable_id("payoff_observation", source_sha256), opened.strategy_id, opened.contract_id,
        context.features.product, facts["key"], context.exit_binding.binding_sha256,
        EXIT_STATE_CONDITION_P1, facts["forecast"].horizon, "ATR_1", values[12],
        facts["started"], facts["ended"], facts["available"], block_id, label.path_outcome,
        facts["traded"], facts["net_pnl"], facts["peak_margin"], facts["maximum_loss"],
        facts["return_before_stop"], Decimal(values[15]), source_sha256)


def payoff_observation_source(
    opened: StrategyProposal,
    context: StrategyContext,
    request: ForecastRequest,
    label: PathLabel,
    replay: "ReplayResult | PayoffPathResult",
    training_end_nano: int,
    block_id: str,
    *,
    rule_originals: tuple[TradingRule, ...] | None = None,
) -> bytes:
    """Exactly the canonical source bytes the sole producer hashes (§2.4).

    Runs the same single strict validation as :func:`observation_from_replay`
    and returns ``canonical_json_bytes`` of the same source projection; it
    never constructs a second observation. M5 persists these exact bytes and
    must recompute ``SHA256(payoff_observation_source(...)) ==
    observation.source_sha256`` from them.
    """
    _facts, source = _produce(opened, context, request, label, replay, training_end_nano,
                              block_id, rule_originals)
    return canonical_json_bytes(source)


@dataclass(frozen=True)
class PathPayoffModel:
    model_id: str
    training_end_nano: int
    observations: tuple[PayoffObservation, ...]
    policy_version: str
    source_hashes: Mapping[str, str]

    def __post_init__(self) -> None:
        if type(self.training_end_nano) is not int:
            raise TypeError("training_must_be_int")
        if not isinstance(self.observations,tuple) or any(not isinstance(r,PayoffObservation) for r in self.observations):
            raise TypeError("payoff_observations_required")
        if any(r.available_at_nano > self.training_end_nano for r in self.observations):
            raise ValueError("future_payoff_observation")
        if len({r.observation_id for r in self.observations}) != len(self.observations):
            raise ValueError("duplicate_payoff_observations")
        frozen = MappingProxyType(dict(self.source_hashes))
        # Source map must be exactly each observation's own recorded source;
        # a tampered mapping with a recomputed id cannot pose as this model.
        expected_sources = {r.observation_id:r.source_sha256 for r in self.observations}
        if dict(frozen) != expected_sources:
            raise ValueError("payoff_source_map_mismatch")
        object.__setattr__(self,"source_hashes",frozen)

    def content_hash(self) -> str:
        payload = {"training_end_nano":self.training_end_nano,"policy_version":self.policy_version,
                   "observations":[observation_payload(r) for r in sorted(self.observations,key=lambda r:r.observation_id)],
                   "source_hashes":dict(self.source_hashes)}
        return sha256_hex(canonical_json_bytes(payload))

    def identity_matches(self) -> bool:
        """The published id must be the final content hash in its fixed format."""
        return self.model_id == "payoff:"+self.content_hash()


def fit_path_payoff_model(observations: tuple[PayoffObservation, ...],
                          training_end_nano: int, policy_version: str) -> PathPayoffModel:
    ordered = tuple(sorted(observations,key=lambda r:r.observation_id))
    provisional = PathPayoffModel("pending",training_end_nano,ordered,policy_version,
                                 {r.observation_id:r.source_sha256 for r in ordered})
    return PathPayoffModel("payoff:"+provisional.content_hash(),training_end_nano,
                           ordered,policy_version,provisional.source_hashes)


def _cvar(distribution: list[tuple[Decimal, Decimal]]) -> Decimal:
    remaining = Decimal("0.05")
    loss = ZERO
    for pnl, weight in sorted(distribution):
        used = min(remaining,weight)
        loss += used * max(ZERO,-pnl)
        remaining -= used
        if remaining <= 0:
            break
    return loss / Decimal("0.05")


def estimate_proposal_payoff(model: PathPayoffModel, proposal: StrategyProposal,
                             context: StrategyContext, opportunity_set_id: str) -> PayoffEstimate:
    forecast = proposal_forecast(proposal,context)
    horizon = forecast.horizon if forecast else context.spec.max_holding
    geometry = calibration_geometry_key(context.features,forecast.frozen_range if forecast else None,
                                        payoff_event(proposal,context))
    reasons = []
    rows = ()
    if forecast is None or geometry is None or proposal.budget is None:
        reasons.append("EVIDENCE_IDENTITY_MISMATCH")
    else:
        expected_range=(proposal.budget.frozen_range if proposal.budget.frozen_range is not None
                        else proposal.next_state.setup.frozen_range if proposal.next_state.setup is not None
                        and proposal.next_state.setup.frozen_range is not None
                        else context.regime.frozen_range)
        if (forecast.quality.status!="READY" or forecast.snapshot_id!=context.features.snapshot_id
                or forecast.frozen_range!=expected_range or forecast.reference_price!=context.features.reference_price):
            reasons.append("EVIDENCE_IDENTITY_MISMATCH")
        try:
            key = condition_key(proposal,context)
            rows = tuple(r for r in model.observations if r.condition_key==key)
        except ValueError:
            reasons.append("EVIDENCE_IDENTITY_MISMATCH")
        if (forecast.barrier_key != "ATR_1" or forecast.upper_distance != context.features.atr_points
                or forecast.lower_distance != context.features.atr_points):
            reasons.append("BARRIER_MISMATCH")
    if not model.identity_matches():
        reasons.append("EVIDENCE_IDENTITY_MISMATCH")
    if model.training_end_nano >= context.now_nano:
        reasons.append("FUTURE_PAYOFF_MODEL")
    if model.policy_version != context.run_policy.research_policy_version:
        reasons.append("PAYOFF_POLICY_MISMATCH")
    by_path = {p:tuple(r for r in rows if r.path_outcome==p) for p in PATHS}
    probabilities = {p:Decimal(str(forecast.path_probabilities[p])) if forecast else ZERO for p in PATHS}
    missing = sum((probabilities[p] for p in PATHS if not by_path[p]),ZERO)
    if missing > 0:
        reasons.append("UNSUPPORTED_PATH_MASS")
    blocks = sorted({r.block_id for r in rows})
    cp = context.confidence_policy
    if len(rows)<cp.minimum_samples:
        reasons.append("INSUFFICIENT_SAMPLES")
    if len(blocks)<cp.minimum_blocks:
        reasons.append("INSUFFICIENT_BLOCKS")
    expected = conservative = cvar = peak = None
    win = before_stop = None
    if forecast and rows and missing==0 and not any(r in reasons for r in (
            "BARRIER_MISMATCH","FUTURE_PAYOFF_MODEL","PAYOFF_POLICY_MISMATCH","EVIDENCE_IDENTITY_MISMATCH")):
        distribution = [(r.net_pnl_cny,probabilities[p]/len(by_path[p]))
                        for p in PATHS for r in by_path[p] if probabilities[p]>0]
        expected = sum((p*w for p,w in distribution),ZERO)
        win = float(sum((w for p,w in distribution if p>0),ZERO))
        cvar = _cvar(distribution)
        peak = max(r.peak_margin_cny for r in rows)
        if all(r.return_before_stop is not None for r in rows):
            before_stop = float(sum((probabilities[p]*sum(r.return_before_stop for r in by_path[p])/len(by_path[p])
                                    for p in PATHS if probabilities[p]>0),ZERO))
        # Same date across nearby observations/contracts is one resampling unit.
        block_stats = {b:{p:(sum((r.net_pnl_cny for r in by_path[p] if r.block_id==b),ZERO),
                                  sum(r.block_id==b for r in by_path[p])) for p in PATHS} for b in blocks}
        rng = Random(7)
        means=[]
        for _ in range(1000):
            selected = [blocks[rng.randrange(len(blocks))] for _ in blocks]
            total = ZERO
            supported = True
            for p in PATHS:
                if probabilities[p]==0:
                    continue
                count = sum(block_stats[b][p][1] for b in selected)
                if count==0:
                    supported = False
                    break
                total += probabilities[p]*sum((block_stats[b][p][0] for b in selected),ZERO)/count
            if supported:
                means.append(total)
        if len(means)==1000:
            conservative=sorted(means)[49]
        else:
            reasons.append("BOOTSTRAP_PATH_SUPPORT_MISSING")
    status = "READY" if not reasons else "INSUFFICIENT"
    if any(r in reasons for r in ("BARRIER_MISMATCH","FUTURE_PAYOFF_MODEL","PAYOFF_POLICY_MISMATCH","EVIDENCE_IDENTITY_MISMATCH")):
        status="INVALID"
    identity=[model.content_hash(),proposal.proposal_id,context.features.snapshot_id,opportunity_set_id,
              geometry,sorted(reasons)]
    return PayoffEstimate(stable_id("payoff_estimate",canonical_json_bytes(identity)),proposal.proposal_id,
        model.model_id,horizon,"ATR_1",geometry or "UNAVAILABLE",opportunity_set_id,
        context.exit_binding.binding_sha256,EXIT_STATE_CONDITION_P1,model.training_end_nano,
        len(rows),len(blocks),expected,conservative,win,cvar,peak,before_stop,float(missing),status,
        tuple(sorted(set(reasons))))
