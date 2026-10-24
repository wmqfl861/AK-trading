"""Deterministic, outcome-blind routing for event-level probability research."""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from decimal import Context, Decimal, DecimalException, ROUND_HALF_EVEN, localcontext
import hashlib
import json
import math

from .contracts import (
    OUTCOMES, REGIMES, ActionOutcome, FactorRow, PayoffEstimate,
    ProbabilityPrediction, ResearchSelection,
)

CASH_ACTION_ID = "cash"
MODES = ("best_ev", "probability_sample", "fixed", "cash")


def _identifier(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _probability(value: object, name: str) -> float:
    result = _finite(value, name)
    if not 0.0 <= result <= 1.0:
        raise ValueError(f"{name} must be in [0, 1]")
    return result


def _count(value: object, name: str, minimum: int = 0) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


def _distribution(values: Mapping[str, float], keys: Sequence[str], name: str) -> None:
    if set(values) != set(keys):
        raise ValueError(f"{name} must contain exactly {tuple(keys)}")
    for value in values.values():
        _probability(value, name)
    if not math.isclose(math.fsum(values.values()), 1.0, rel_tol=0.0, abs_tol=1e-12):
        raise ValueError(f"{name} must sum to one")


def _uniform(seed: int, event_id: str, mode: str) -> float:
    payload = json.dumps([seed, event_id, mode], ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return (int.from_bytes(hashlib.sha256(payload).digest()[:8], "big") >> 11) / (1 << 53)


def choose_strategy(
    event_id: str,
    mode: str,
    estimates: Sequence[PayoffEstimate],
    prediction: ProbabilityPrediction,
    *,
    min_win_probability: float = 0.5,
    min_ev: float = 0.0,
    min_train_samples: int = 30,
    max_ambiguous_probability: float = 0.25,
    seed: int = 7,
    fixed_action_id: str | None = None,
    allow_uncalibrated: bool = False,
) -> ResearchSelection:
    """Choose without accessing labels, realized outcomes, or global RNG state.

    Sampling is a heuristic policy, not calibrated probabilities of optimality:
    risk mass = max(eligible win probability) * (1 - ambiguous probability),
    score[action] = (conservative EV - min EV) * win probability. Normalize
    scores to the risk mass and assign the remainder to cash. The reported
    confidence is the selected action's estimated win probability, not its
    sampling weight. Fixed and cash benchmarks bypass all model/payoff gates.

    The cash sentinel is ``cash``; callers map parameterized ActionSpec cash
    IDs to it and pass only noncash payoff candidates. Fixed IDs are validated
    against the action registry by the caller, so estimates may be empty.
    """
    _identifier(event_id, "event_id")
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    if prediction.event_id != event_id:
        raise ValueError("prediction event_id does not match the decision")
    _probability(min_win_probability, "min_win_probability")
    _probability(max_ambiguous_probability, "max_ambiguous_probability")
    min_ev = _finite(min_ev, "min_ev")
    _count(min_train_samples, "min_train_samples", 1)
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer")
    if not isinstance(allow_uncalibrated, bool):
        raise ValueError("allow_uncalibrated must be a bool")
    candidates: dict[str, PayoffEstimate] = {}
    for item in estimates:
        _identifier(item.action_id, "action_id")
        if item.event_id != event_id:
            raise ValueError("estimate event_id does not match the decision")
        if item.action_id in candidates:
            raise ValueError("duplicate action_id in estimates")
        if item.action_id == CASH_ACTION_ID or item.action_id.startswith("cash:"):
            raise ValueError("cash must not be a payoff candidate")
        candidates[item.action_id] = item
    action_ids = set(candidates) | {CASH_ACTION_ID}
    if mode == "fixed":
        _identifier(fixed_action_id, "fixed_action_id")
        action_ids.add(fixed_action_id)
    weights = {action_id: 0.0 for action_id in sorted(action_ids)}
    random_u = _uniform(seed, event_id, mode) if mode == "probability_sample" else None
    override = "; allow_uncalibrated=True" if allow_uncalibrated and mode in MODES[:2] else ""

    def deterministic(action_id: str, reason: str, ev: float | None = 0.0,
                      confidence: float | None = None) -> ResearchSelection:
        weights[action_id] = 1.0
        return ResearchSelection(event_id, mode, action_id, weights, reason + override, ev, confidence, random_u)

    if mode == "cash":
        return deterministic(CASH_ACTION_ID, "cash_benchmark")
    if mode == "fixed":
        fixed = candidates.get(fixed_action_id)
        ev = None
        if fixed is not None and fixed.conservative_ev is not None:
            try:
                ev = _finite(fixed.conservative_ev, "fixed conservative_ev")
            except ValueError:
                pass  # Optional annotation cannot disable the fixed benchmark.
        return deterministic(fixed_action_id, "fixed_benchmark", 0.0 if fixed_action_id == CASH_ACTION_ID else ev)

    rejected: Counter[str] = Counter()
    eligible: list[PayoffEstimate] = []
    for item in sorted(candidates.values(), key=lambda item: item.action_id):
        _count(item.count, "payoff count")
        for field in ("mean_net", "standard_error", "conservative_ev", "win_probability", "loss_q10"):
            value = getattr(item, field)
            if value is not None:
                _finite(value, field)
        if item.standard_error is not None and item.standard_error < 0:
            raise ValueError("standard_error must be nonnegative")
        if item.win_probability is not None:
            _probability(item.win_probability, "win_probability")
        if item.status != "ready":
            rejected["payoff_not_ready"] += 1
        elif item.count < min_train_samples:
            rejected["insufficient_payoff_samples"] += 1
        elif item.conservative_ev is None or item.win_probability is None:
            rejected["missing_payoff_evidence"] += 1
        elif item.win_probability < min_win_probability:
            rejected["win_probability_below_threshold"] += 1
        elif item.conservative_ev <= 0:
            rejected["non_positive_conservative_ev"] += 1
        elif item.conservative_ev <= min_ev:
            rejected["conservative_ev_below_threshold"] += 1
        else:
            eligible.append(item)

    _count(prediction.train_count, "prediction train_count")
    _count(prediction.calibration_count, "prediction calibration_count")
    if prediction.model_status == "insufficient_training":
        return deterministic(CASH_ACTION_ID, "insufficient_training")
    if prediction.model_status not in ("calibrated", "uncalibrated"):
        raise ValueError("unknown prediction model_status")
    _distribution(prediction.probabilities, OUTCOMES, "probabilities")
    _distribution(prediction.regime_probabilities, REGIMES, "regime_probabilities")
    if _finite(prediction.temperature, "temperature") <= 0:
        raise ValueError("temperature must be positive")
    if prediction.train_count < min_train_samples:
        return deterministic(CASH_ACTION_ID, "insufficient_prediction_samples")
    if prediction.model_status != "calibrated" and not allow_uncalibrated:
        return deterministic(CASH_ACTION_ID, "uncalibrated")
    ambiguity = prediction.probabilities["ambiguous"]
    if ambiguity > max_ambiguous_probability:
        return deterministic(CASH_ACTION_ID, "ambiguous_probability_above_cap")
    if not candidates:
        return deterministic(CASH_ACTION_ID, "no_candidate_estimates")
    if not eligible:
        detail = ",".join(f"{reason}={count}" for reason, count in sorted(rejected.items()))
        return deterministic(CASH_ACTION_ID, "no_eligible_estimates; " + detail)
    if mode == "best_ev":
        best = min(eligible, key=lambda item: (-item.conservative_ev, item.action_id))
        return deterministic(best.action_id, "highest_conservative_ev", best.conservative_ev, best.win_probability)

    risk_mass = max(item.win_probability for item in eligible) * (1.0 - ambiguity)
    # Scaling before subtracting/multiplying prevents overflow for finite EVs.
    scale = max(abs(min_ev), max(item.conservative_ev for item in eligible))
    scores = [(item.conservative_ev / scale - min_ev / scale) * item.win_probability for item in eligible]
    total_score = math.fsum(scores)
    if total_score == 0 or risk_mass == 0:
        return deterministic(CASH_ACTION_ID, "heuristic_probability_sample; zero_risk_mass")
    for item, score in zip(eligible, scores):
        weights[item.action_id] = risk_mass * (score / total_score)
    weights[CASH_ACTION_ID] = max(0.0, 1.0 - math.fsum(weights.values()))
    _distribution(weights, tuple(weights), "sampling weights")
    cumulative = 0.0
    selected = None
    for action_id, weight in weights.items():
        if weight > 0:
            selected = action_id
            cumulative = math.fsum((cumulative, weight))
            if random_u < cumulative:
                break
    chosen = candidates.get(selected)
    return ResearchSelection(
        event_id, mode, selected, weights, "heuristic_probability_sample" + override,
        chosen.conservative_ev if chosen is not None else 0.0,
        chosen.win_probability if chosen is not None else None, random_u,
    )


def _ordered_events(events: Sequence[FactorRow]) -> list[FactorRow]:
    identities: set[str] = set()
    for row in events:
        _identifier(row.event_id, "event_id")
        _identifier(row.contract_id, "contract_id")
        _count(row.index, "event index")
        _count(row.timestamp_nano, "event timestamp_nano")
        if row.event_id in identities:
            raise ValueError("duplicate event_id")
        identities.add(row.event_id)
    ordered = sorted(events, key=lambda row: (row.contract_id, row.index, row.event_id))
    previous: FactorRow | None = None
    for row in ordered:
        if previous is not None and previous.contract_id == row.contract_id:
            if previous.index == row.index:
                raise ValueError("duplicate contract event index")
            if previous.timestamp_nano >= row.timestamp_nano:
                raise ValueError("event timestamps must increase with each contract's indices")
        previous = row
    return ordered


def select_nonoverlapping(events: Sequence[FactorRow], horizon: int) -> tuple[str, ...]:
    """Freeze a common schedule before routing, using only ready observations.

    Callers supply the test partition after maturity/purge checks. Each decision
    at i reserves bars i+1..i+horizon; i+horizon is a valid next decision. Gaps
    use original contract indices, never positions within a filtered sequence.
    Different contracts have separate schedules; no portfolio is implied.
    """
    _count(horizon, "horizon", 1)
    if horizon > 60:
        raise ValueError("horizon must be in 1..60")
    next_index: dict[str, int] = {}
    selected: list[str] = []
    for row in _ordered_events(events):
        if row.status != "ready" or row.index < next_index.get(row.contract_id, 0):
            continue
        selected.append(row.event_id)
        next_index[row.contract_id] = row.index + horizon
    return tuple(selected)


def _validate_selection(selection: ResearchSelection) -> None:
    mode = selection.mode
    if isinstance(mode, str) and mode.startswith("fixed:") and len(mode) > len("fixed:"):
        mode = "fixed"
    if mode not in MODES:
        raise ValueError("unknown selection mode")
    weights = selection.weights
    if not isinstance(weights, Mapping) or CASH_ACTION_ID not in weights:
        raise ValueError("selection weights must include cash")
    for action_id in weights:
        _identifier(action_id, "weight action_id")
    _distribution(weights, tuple(weights), "selection weights")
    if selection.action_id not in weights or weights[selection.action_id] <= 0:
        raise ValueError("selected action must have positive weight")
    if selection.predicted_ev is not None:
        _finite(selection.predicted_ev, "selected predicted_ev")
    if selection.prediction_confidence is not None:
        _probability(selection.prediction_confidence, "selected confidence")
    if mode == "probability_sample":
        draw = _probability(selection.random_u, "selection random_u")
        if draw == 1:
            raise ValueError("selection random_u must be below one")
        cumulative = 0.0
        sampled = None
        for action_id, weight in sorted(weights.items()):
            if weight > 0:
                sampled = action_id
                cumulative = math.fsum((cumulative, weight))
                if draw < cumulative:
                    break
        if sampled != selection.action_id:
            raise ValueError("selection weights and random_u disagree with selected action")
    elif selection.random_u is not None or weights[selection.action_id] != 1.0:
        raise ValueError("deterministic selection requires unit weight and no random_u")
    if mode == "cash" and selection.action_id != CASH_ACTION_ID:
        raise ValueError("cash benchmark selected a noncash action")


_ARITHMETIC = Context(prec=128, rounding=ROUND_HALF_EVEN)
_ZERO = Decimal(0)


def _outcome_problem(outcome: ActionOutcome, row: FactorRow, horizon: int,
                     terminal_nano: int | None) -> str | None:
    if outcome.contract_id != row.contract_id or outcome.index != row.index:
        return "outcome_identity_mismatch"
    if outcome.status != "complete":
        return f"outcome_{outcome.status}:{outcome.reason}"
    if (isinstance(outcome.terminal_nano, bool) or not isinstance(outcome.terminal_nano, int)
            or outcome.terminal_nano <= row.timestamp_nano
            or (terminal_nano is not None and outcome.terminal_nano != terminal_nano)):
        return "invalid_outcome_terminal"
    money = (outcome.gross_pnl, outcome.fees, outcome.slippage, outcome.net_pnl, outcome.max_margin)
    if any(not isinstance(value, Decimal) or not value.is_finite() for value in money):
        return "invalid_outcome_money"
    if min(outcome.fees, outcome.slippage, outcome.max_margin) < 0:
        return "negative_outcome_cost_or_margin"
    try:
        if outcome.net_pnl != outcome.gross_pnl - outcome.fees - outcome.slippage:
            return "outcome_net_accounting_mismatch"
    except DecimalException:
        return "invalid_outcome_arithmetic"
    if not isinstance(outcome.traded, bool):
        return "invalid_outcome_traded"
    if not outcome.traded and (any(value != 0 for value in money) or outcome.fills):
        return "nonzero_untraded_outcome"
    if isinstance(outcome.direction, bool) or outcome.direction not in (-1, 0, 1):
        return "invalid_outcome_direction"
    if (isinstance(outcome.ambiguous_bars, bool) or not isinstance(outcome.ambiguous_bars, int)
            or not 0 <= outcome.ambiguous_bars <= horizon):
        return "invalid_outcome_ambiguous_bars"
    for fill in outcome.fills:
        if not isinstance(fill, Mapping):
            return "invalid_outcome_fill"
        if "index" in fill:
            index = fill["index"]
            if isinstance(index, bool) or not isinstance(index, int) or not row.index < index <= row.index + horizon:
                return "outcome_fill_outside_window"
        if "lots" in fill:
            lots = fill["lots"]
            if isinstance(lots, bool) or not isinstance(lots, int) or lots <= 0:
                return "invalid_outcome_fill_lots"
    return None


def _event_result(selection: ResearchSelection, row: FactorRow, outcome: ActionOutcome | None,
                  horizon: int, terminal_nano: int | None) -> dict:
    record = {
        "event_id": row.event_id, "index": row.index, "timestamp_nano": row.timestamp_nano,
        "action_id": selection.action_id, "cash": selection.action_id == CASH_ACTION_ID,
        "status": "complete", "reason": "cash" if selection.action_id == CASH_ACTION_ID else "",
        "traded": False, "net_pnl": "0", "turnover_lots": 0,
    }
    if selection.action_id == CASH_ACTION_ID:
        return record
    problem = "missing_outcome" if outcome is None else _outcome_problem(outcome, row, horizon, terminal_nano)
    if problem is not None:
        record.update(status="incomplete", reason=problem, traded=None, net_pnl=None, turnover_lots=None)
        return record
    turnover = 0
    if outcome.traded:
        turnover = sum(fill["lots"] for fill in outcome.fills) if outcome.fills and all("lots" in fill for fill in outcome.fills) else None
    record.update(reason=outcome.reason, traded=outcome.traded, net_pnl=str(outcome.net_pnl), turnover_lots=turnover)
    return record


def _counts(records: list[dict]) -> dict:
    complete = [row for row in records if row["status"] == "complete"]
    traded = [row for row in complete if row["traded"]]
    wins = sum(Decimal(row["net_pnl"]) > 0 for row in traded)
    losses = sum(Decimal(row["net_pnl"]) < 0 for row in traded)
    incomplete = len(records) - len(complete)
    reasons = Counter(row["reason"] for row in records if row["status"] != "complete")
    return {
        "status": "incomplete" if incomplete else "complete" if records else "empty",
        "event_ids": [row["event_id"] for row in records],
        "opportunity_count": len(records), "complete_count": len(complete), "incomplete_count": incomplete,
        "traded_count": len(traded), "no_trade_count": len(complete) - len(traded),
        "cash_count": sum(row["cash"] for row in records), "wins": wins, "losses": losses,
        "flat_trades": len(traded) - wins - losses,
        "trade_win_rate": wins / len(traded) if traded and not incomplete else None,
        "opportunity_win_rate": wins / len(records) if records and not incomplete else None,
        "participation_rate": len(traded) / len(records) if records and not incomplete else None,
        "reasons": dict(sorted(reasons.items())),
    }


def _contract_metrics(records: list[dict]) -> dict:
    metrics = _counts(records)
    total = _ZERO
    running = peak = drawdown = _ZERO
    cumulative: list[str | None] = []
    gap = False
    turnover = 0
    for record in records:
        if record["status"] != "complete":
            gap = True
        else:
            pnl = Decimal(record["net_pnl"])
            total += pnl
            if not gap:
                running += pnl
                peak = max(peak, running)
                drawdown = max(drawdown, peak - running)
        cumulative.append(None if gap else str(running))
        if record["turnover_lots"] is None:
            turnover = None
        elif turnover is not None:
            turnover += record["turnover_lots"]
    complete = metrics["status"] == "complete"
    metrics.update(
        total_net=str(total) if complete else None,
        mean_net=str(total / len(records)) if complete and records else None,
        mean_trade_net=str(total / metrics["traded_count"]) if complete and metrics["traded_count"] else None,
        known_total_net=str(total),
        cumulative_event_pnl=cumulative,
        max_drawdown_cny=str(drawdown) if complete else None,
        turnover_lots=turnover,
        events=records,
    )
    return metrics


def aggregate_selected(
    selections: Sequence[ResearchSelection],
    outcomes: Sequence[ActionOutcome],
    events: Sequence[FactorRow],
    *,
    horizon: int,
) -> dict:
    """Compare modes on the same predetermined, disjoint contract episodes.

    Only selections whose event IDs occur in ``events`` are considered. Each
    represented mode must supply exactly the shared nonoverlapping schedule;
    duplicate selections/outcomes or action-specific sample drops are errors.
    Fixed benchmark aliases ``fixed:<name>`` may label different fixed actions.

    Cash needs no outcome. Missing/invalid selected outcomes retain their rows
    and invalidate full-period PNL/rates/drawdown; known counts and known PNL
    remain explicitly partial. Money is serialized as Decimal strings. Rates
    are fractions in [0,1], or None for an undefined/incomplete denominator.
    Currency is reported only per contract. Cumulative event PNL and its
    drawdown are flat-window diagnostics with fresh initial cash per episode,
    not account equity or a portfolio return. Turnover is executed lots,
    including entry/exit fills, not an assumed capital/notional ratio.
    """
    schedule = select_nonoverlapping(events, horizon)
    scheduled_ids = set(schedule)
    event_by_id = {row.event_id: row for row in events}
    terminal_by_index = {(row.contract_id, row.index): row.timestamp_nano for row in events}
    by_mode: dict[str, dict[str, ResearchSelection]] = {}
    for selection in selections:
        if selection.event_id not in event_by_id:
            continue
        _validate_selection(selection)
        mode_selections = by_mode.setdefault(selection.mode, {})
        if selection.event_id in mode_selections:
            raise ValueError("duplicate mode/event selection")
        mode_selections[selection.event_id] = selection
    if scheduled_ids and not by_mode:
        raise ValueError("each mode must provide the same full event schedule")
    for mode_selections in by_mode.values():
        if set(mode_selections) != scheduled_ids:
            raise ValueError("each mode must provide the same full event schedule; overlapping selections are prohibited")
    needed = {(item.event_id, item.action_id) for mode_selections in by_mode.values()
              for item in mode_selections.values() if item.action_id != CASH_ACTION_ID}
    outcome_by_key: dict[tuple[str, str], ActionOutcome] = {}
    for outcome in outcomes:
        key = (outcome.event_id, outcome.action_id)
        if key not in needed:
            continue
        if key in outcome_by_key:
            raise ValueError("duplicate selected event/action outcome")
        outcome_by_key[key] = outcome
    per_mode: dict[str, dict] = {}
    incomplete_reasons: Counter[str] = Counter()
    incomplete_count = 0
    with localcontext(_ARITHMETIC):
        for mode, mode_selections in sorted(by_mode.items()):
            records: list[dict] = []
            by_contract: dict[str, list[dict]] = {}
            for event_id in schedule:
                row = event_by_id[event_id]
                selection = mode_selections[event_id]
                outcome = outcome_by_key.get((event_id, selection.action_id))
                terminal = terminal_by_index.get((row.contract_id, row.index + horizon))
                record = _event_result(selection, row, outcome, horizon, terminal)
                records.append(record)
                by_contract.setdefault(row.contract_id, []).append(record)
            mode_metrics = _counts(records)
            mode_metrics["per_contract"] = {contract: _contract_metrics(rows) for contract, rows in by_contract.items()}
            per_mode[mode] = mode_metrics
            incomplete_count += mode_metrics["incomplete_count"]
            incomplete_reasons.update(mode_metrics["reasons"])
    return {
        "status": "incomplete" if incomplete_count else "complete" if schedule else "empty",
        "horizon": horizon, "scheduled_event_ids": list(schedule), "opportunity_count": len(schedule),
        "incomplete_count": incomplete_count, "reasons": dict(sorted(incomplete_reasons.items())),
        "per_mode": per_mode,
        "currency_scope": "per_contract_only",
        "pnl_basis": "independent_flat_windows_fresh_initial_cash",
        "drawdown_basis": "cumulative_event_pnl_diagnostic_not_account_equity",
        "rate_basis": "fractions; trade_win_rate excludes cash/no-trade; opportunity rates include them",
    }
