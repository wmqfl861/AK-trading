"""Offline normalized-price research, never a futures account or execution engine."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from statistics import mean, stdev
from typing import Iterable

EXPERTS = ("breakout", "range")
LEARNABLE_STATES = ("trend", "range")
RESERVED_CASH_STATE = "uncertain"
STATES = (*LEARNABLE_STATES, RESERVED_CASH_STATE)
STATE_SEMANTICS = {"trend": "learned_conditional_market_state", "range": "learned_conditional_market_state",
                   "uncertain": "reserved_cash_score_not_a_learned_market_regime"}


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Bar:
    symbol: str
    open_time: datetime
    close_time: datetime
    open: float
    high: float
    low: float
    close: float
    series_kind: str = "normalized_research"

    def record(self) -> dict:
        return {**asdict(self), "open_time": self.open_time.isoformat(), "close_time": self.close_time.isoformat()}


def validate_bars(bars: Iterable[Bar]) -> list[dict[str, Bar]]:
    """Require an explicit synchronized grid. Never infer exchange sessions or gaps."""
    rows = list(bars)
    if not 4 <= len(rows) <= 200000:
        raise ValueError("input must have 4..200000 rows")
    symbols = sorted({b.symbol for b in rows})
    if not 1 <= len(symbols) <= 16:
        raise ValueError("input must contain 1..16 fixed instruments")
    frames: dict[datetime, dict[str, Bar]] = {}
    last_by_symbol: dict[str, datetime] = {}
    duration = None
    for b in rows:
        if not isinstance(b.symbol, str) or not b.symbol or not b.symbol.isascii() or not all(c.isalnum() or c in "_.-" for c in b.symbol) or b.symbol.startswith("KQ."):
            raise ValueError("invalid symbol or continuous series rejected")
        if b.series_kind != "normalized_research":
            raise ValueError("explicit normalized_research series_kind required")
        for stamp in (b.open_time, b.close_time):
            if not isinstance(stamp, datetime) or stamp.tzinfo is None or stamp.utcoffset() is None:
                raise ValueError("timezone-aware open/close timestamps required")
        if b.close_time <= b.open_time:
            raise ValueError("close timestamp must follow open")
        d = b.close_time - b.open_time
        duration = d if duration is None else duration
        if d != duration:
            raise ValueError("inconsistent bar duration")
        if b.symbol in last_by_symbol and b.open_time <= last_by_symbol[b.symbol]:
            raise ValueError("duplicate or unordered instrument timestamps")
        last_by_symbol[b.symbol] = b.open_time
        for name in ("open", "high", "low", "close"):
            _finite(getattr(b, name), name, 1e-9, 1e12)
        if not b.low <= min(b.open, b.close) <= max(b.open, b.close) <= b.high:
            raise ValueError("invalid OHLC ordering")
        if b.high / b.low > 2:
            raise ValueError("bar range outside normalized prototype bounds")
        frames.setdefault(b.open_time, {})[b.symbol] = b
    ordered = [frames[t] for t in sorted(frames)]
    if len(ordered) < 2 or any(sorted(frame) != symbols for frame in ordered):
        raise ValueError("missing synchronized instrument/bar")
    times = [frame[symbols[0]].open_time for frame in ordered]
    interval = times[1] - times[0]
    if interval <= duration or any(b - a != interval for a, b in zip(times, times[1:])):
        raise ValueError("overlapping bars or missing regular interval")
    for prev, frame in zip(ordered, ordered[1:]):
        for s in symbols:
            if not 0.5 <= frame[s].open / prev[s].close <= 2:
                raise ValueError("price discontinuity outside normalized prototype bounds")
    return ordered


def _states(frames: list[dict[str, Bar]], config: ResearchConfig) -> list[dict]:
    histories = {s: [] for s in frames[0]}
    previous: dict[str, dict] = {}
    result = []
    for frame in frames:
        state = {}
        for s, bar in sorted(frame.items()):
            histories[s].append(bar.close)
            state[s] = evaluate_experts(histories[s][-config.lookback - 1:], config, previous.get(s))
        result.append(state)
        previous = state
    return result


def fit_model(bars: Iterable[Bar], config: ResearchConfig) -> dict:
    """Fit on rows 0..T-1 only; last label is decision T-2 -> outcome T-1."""
    frames = validate_bars(bars)
    if len(frames) <= config.train_bars:
        raise ValueError("at least one OOS execution bar required")
    return _fit(frames[:config.train_bars], config)


def _fit(train: list[dict[str, Bar]], config: ResearchConfig) -> dict:
    states = _states(train, config)
    symbols = sorted(train[0])
    samples = {s: {e: {r: [] for r in STATES} for e in EXPERTS} for s in symbols}
    for d in range(config.lookback, len(train) - 1):
        for s in symbols:
            state = states[d][s]
            if state["reason"] != "ready" or state["state"] not in LEARNABLE_STATES:
                continue
            outcome = train[d + 1][s]
            for e, signal in state["signals"].items():
                if abs(signal) > 1e-12:
                    payoff = settle_bar([signal], outcome.open, outcome.close, fee_bps=config.fee_bps, slippage_bps=config.slippage_bps)
                    # Stats per unit signal magnitude, matching risk-budget sizing.
                    samples[s][e][state["state"]].append(payoff["net_pnl"] / abs(signal))
    tables = {}
    for s in symbols:
        tables[s] = {}
        for e in EXPERTS:
            tables[s][e] = {}
            for regime, values in samples[s][e].items():
                n = len(values)
                available = n >= config.min_samples
                avg = mean(values) if n else None
                variance = stdev(values) ** 2 if n >= 2 else None
                downside = math.sqrt(sum(min(0, x) ** 2 for x in values) / n) if n else None
                edge = avg - config.edge_penalty * math.sqrt(variance / n) if available else None
                tables[s][e][regime] = {"count": n, "available": available, "mean": avg, "variance": variance, "downside": downside, "edge": edge}
    returns = {s: [b[s].close / a[s].close - 1 for a, b in zip(train, train[1:])] for s in symbols}
    corr = {s: {} for s in symbols}
    moments = {s: (mean(values), stdev(values)) for s, values in returns.items()}
    for s in symbols:
        for other in symbols:
            x, y = returns[s], returns[other]
            mx, sx = moments[s]
            my, sy = moments[other]
            value = sum((a - mx) * (b - my) for a, b in zip(x, y)) / ((len(x) - 1) * sx * sy) if sx > 1e-12 and sy > 1e-12 else 1.0
            corr[s][other] = max(-1.0, min(1.0, value))
    unseen = set(symbols)
    clusters = []
    while unseen:
        group, pending = [], [min(unseen)]
        while pending:
            s = pending.pop()
            if s not in unseen:
                continue
            unseen.remove(s)
            group.append(s)
            pending.extend(sorted(o for o in unseen if abs(corr[s][o]) >= config.correlation_threshold))
        clusters.append(sorted(group))
    model = {"learnable_states": list(LEARNABLE_STATES), "reserved_cash_state": RESERVED_CASH_STATE,
             "state_semantics": dict(STATE_SEMANTICS), "training_frames": len(train), "last_signal_close": train[-2][symbols[0]].close_time.isoformat(),
             "last_label_close": train[-1][symbols[0]].close_time.isoformat(),
             "training_digest": digest([[f[s].record() for s in symbols] for f in train]),
             "config_digest": digest(asdict(config)), "tables": tables, "correlation": corr, "clusters": clusters}
    return {**model, "model_digest": digest(model)}


@dataclass(frozen=True)
class ResearchConfig:
    train_bars: int = 300
    lookback: int = 12
    min_samples: int = 30
    ema_alpha: float = 0.25
    hysteresis: float = 0.1
    uncertainty_limit: float = 0.95
    edge_penalty: float = 1.0
    edge_full_scale: float = 0.002
    min_evidence_mass: float = 0.5
    fee_bps: float = 1.0
    slippage_bps: float = 1.0
    target_vol: float = 0.005
    vol_floor: float = 0.001
    expert_cap: float = 0.25
    instrument_cap: float = 0.4
    gross_cap: float = 0.8
    cluster_cap: float = 0.5
    correlation_threshold: float = 0.7
    portfolio_vol_cap: float = 0.005
    max_drawdown: float = 0.1
    kill_switch: bool = False

    def __post_init__(self) -> None:
        for name, low, high in (("train_bars", 100, 100000), ("lookback", 3, 1000), ("min_samples", 2, 100000)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"{name}: invalid integer")
        if self.train_bars < self.lookback + self.min_samples + 2:
            raise ValueError("training window cannot contain enough completed labels")
        for name in ("edge_full_scale", "min_evidence_mass", "ema_alpha", "uncertainty_limit", "target_vol", "vol_floor", "expert_cap", "instrument_cap", "gross_cap", "cluster_cap", "correlation_threshold", "portfolio_vol_cap", "max_drawdown"):
            _finite(getattr(self, name), name, 1e-9, 1)
        _finite(self.hysteresis, "hysteresis", 0, 1)
        _finite(self.edge_penalty, "edge_penalty", 0, 10)
        _finite(self.fee_bps, "fee_bps", 0, 1000)
        _finite(self.slippage_bps, "slippage_bps", 0, 1000)
        if not isinstance(self.kill_switch, bool):
            raise ValueError("kill_switch must be boolean")


def evaluate_experts(closes: Iterable[float], config: ResearchConfig, previous: dict | None = None) -> dict:
    """Evaluate both experts at a closed bar; `previous` is the prior state only."""
    result = {"signals": dict.fromkeys(EXPERTS, 0.0), "scores": {"trend": 0.0, "range": 0.0, "uncertain": 1.0},
              "state": "uncertain", "uncertainty": 1.0, "volatility": None,
              "score_kind": "uncalibrated_heuristic", "reason": "warmup"}
    values = list(closes)[-config.lookback - 1:]
    if len(values) < config.lookback + 1:
        return result
    try:
        values = [_finite(x, "close", 1e-9, 1e12) for x in values]
    except ValueError:
        result["reason"] = "invalid_price"
        return result
    returns = [b / a - 1 for a, b in zip(values, values[1:])]
    vol = stdev(returns)
    spread = stdev(values[:-1])
    movement = math.fsum(abs(b - a) for a, b in zip(values, values[1:]))
    if vol <= 1e-12 or spread <= 1e-12 or movement <= 1e-12:
        result["reason"] = "no_volatility"
        return result
    last, prior = values[-1], values[:-1]
    breakout = 1.0 if last > max(prior) else -1.0 if last < min(prior) else 0.0
    range_signal = max(-1.0, min(1.0, -(last - mean(prior)) / (2 * spread)))
    efficiency = abs(last - values[0]) / movement
    raw = {"trend": 0.05 + 0.9 * efficiency, "range": 0.05 + 0.9 * (1 - efficiency),
           "uncertain": 0.05 + 1.8 * efficiency * (1 - efficiency)}
    total = sum(raw.values())
    scores = {s: raw[s] / total for s in STATES}
    if previous is not None and previous["reason"] == "ready":
        scores = {s: config.ema_alpha * scores[s] + (1 - config.ema_alpha) * previous["scores"][s] for s in STATES}
    # The third score is reserved cash: it is <=1/3 and cannot win a ready state.
    state = max(LEARNABLE_STATES, key=lambda s: scores[s])
    if previous is not None and previous["reason"] == "ready":
        old = previous["state"]
        if scores[state] - scores[old] < config.hysteresis:
            state = old
    entropy = -sum(p * math.log(p) for p in scores.values() if p > 0) / math.log(3)
    return {**result, "signals": {"breakout": breakout, "range": range_signal}, "scores": scores,
            "state": state, "uncertainty": min(1.0, max(0.0, entropy)), "volatility": vol, "reason": "ready"}



def mix_evidence(scores: dict[str, float], table: dict, config: ResearchConfig) -> dict:
    """Subprobability moments: unsupported state mass is never renormalized."""
    known = {r: table[r] for r in LEARNABLE_STATES if table[r]["available"] and table[r]["count"] >= config.min_samples}
    mass = sum(scores[r] for r in known)
    if mass == 0:
        return {"mass": 0.0, "mean": None, "variance": None, "downside": None, "edge": None}
    avg = sum(scores[r] * known[r]["mean"] for r in known)
    second = sum(scores[r] * (known[r]["variance"] + known[r]["mean"] ** 2) for r in known)
    return {"mass": mass, "mean": avg, "variance": max(0.0, second - avg ** 2),
            "downside": sum(scores[r] * known[r]["downside"] for r in known),
            "edge": sum(scores[r] * known[r]["edge"] for r in known)}


def apply_risk(requested: dict[str, float], volatility: dict[str, float | None], clusters: list[list[str]],
               config: ResearchConfig, *, killed: bool = False) -> dict:
    """Deterministic scale-down-only overlay; no correlation hedge credit."""
    targets, limits = {}, []
    if killed or config.kill_switch:
        return {"targets": dict.fromkeys(requested, 0.0), "limits": ["kill_switch"]}
    for s, value in sorted(requested.items()):
        vol = volatility.get(s)
        if vol is None or not math.isfinite(vol) or vol <= 1e-12 or not math.isfinite(value):
            targets[s] = 0.0
            limits.append("invalid_risk_input")
        else:
            targets[s] = max(-config.instrument_cap, min(config.instrument_cap, value))
            if targets[s] != value:
                limits.append("instrument_cap")
    def cap(names: list[str], bound: float, name: str) -> None:
        gross = sum(abs(targets[s]) for s in names)
        if gross > bound:
            for s in names:
                targets[s] *= bound / gross
            limits.append(name)
    cap(list(targets), config.gross_cap, "gross_cap")
    covered = set()
    for cluster in clusters:
        names = [s for s in cluster if s in targets]
        covered.update(names)
        cap(names, config.cluster_cap, "cluster_cap")
    # Unknown correlations are conservatively one joint cluster.
    unknown = [s for s in targets if s not in covered]
    if unknown:
        cap(list(targets), config.cluster_cap, "unknown_correlation_cap")
    worst_vol = sum(abs(targets[s]) * (volatility[s] or 0) for s in targets if targets[s])
    if worst_vol > config.portfolio_vol_cap:
        targets = {s: x * config.portfolio_vol_cap / worst_vol for s, x in targets.items()}
        limits.append("portfolio_vol_cap")
    return {"targets": targets, "limits": sorted(set(limits))}


def allocate(states: dict[str, dict], model: dict, config: ResearchConfig, *, mode: str, killed: bool = False) -> dict:
    """Allocate both experts concurrently. Fixed slots never inspect edge sign."""
    if mode not in ("dynamic", "fixed"):
        raise ValueError("mode must be dynamic or fixed")
    budgets = {}
    scored = []
    for s, state in sorted(states.items()):
        budgets[s] = {}
        for e in EXPERTS:
            evidence = mix_evidence(state["scores"], model["tables"][s][e], config)
            reason = "ready"
            if state["reason"] != "ready":
                reason = state["reason"]
            elif state["uncertainty"] > config.uncertainty_limit:
                reason = "uncertain"
            elif evidence["mass"] < config.min_evidence_mass:
                reason = "insufficient_evidence"
            elif abs(state["signals"][e]) <= 1e-12:
                reason = "flat_signal"
            elif mode == "dynamic" and (evidence["edge"] is None or evidence["edge"] <= 0):
                reason = "no_edge"
            item = {"evidence": evidence, "reason": reason, "risk_budget": 0.0, "requested_exposure": 0.0, "expert_exposure": 0.0}
            budgets[s][e] = item
            if reason == "ready":
                if mode == "fixed":
                    item["risk_budget"] = evidence["mass"] / (len(states) * len(EXPERTS))
                else:
                    score = evidence["edge"] / max(config.vol_floor, math.sqrt(evidence["variance"]) + evidence["downside"])
                    scored.append((s, e, score))
    if scored:
        # Absolute edge and uncertainty attenuate the TOTAL budget before relative weights.
        strength = min(1.0, max(budgets[s][e]["evidence"]["edge"] for s, e, _ in scored) / config.edge_full_scale)
        total = sum(score for _, _, score in scored)
        for s, e, score in scored:
            budgets[s][e]["risk_budget"] = strength * score / total * budgets[s][e]["evidence"]["mass"] * (1 - states[s]["uncertainty"])
    requested, volatility = {}, {}
    for s, state in sorted(states.items()):
        volatility[s] = state["volatility"]
        for e, item in budgets[s].items():
            if item["risk_budget"]:
                request = item["risk_budget"] * config.target_vol / max(config.vol_floor, state["volatility"]) * state["signals"][e]
                item["requested_exposure"] = request
                item["expert_exposure"] = max(-config.expert_cap, min(config.expert_cap, request))
        requested[s] = math.fsum(item["expert_exposure"] for item in budgets[s].values())
    risk = apply_risk(requested, volatility, model["clusters"], config, killed=killed)
    return {"budgets": budgets, "net_requested": requested, **risk}


def run_research(bars: Iterable[Bar], config: ResearchConfig) -> dict:
    """Frozen-prefix comparison. No OOS labels enter the model or its thresholds."""
    frames = validate_bars(bars)
    if len(frames) <= config.train_bars:
        raise ValueError("at least one OOS execution bar required")
    symbols = sorted(frames[0])
    model = _fit(frames[:config.train_bars], config)
    states = _states(frames, config)
    portfolios = {m: {"equity": 1.0, "peak": 1.0, "killed": config.kill_switch} for m in ("dynamic", "fixed")}
    rows = []
    for t in range(config.train_bars, len(frames)):
        decision = states[t - 1]
        row = {"bar_index": t, "decision_close": frames[t - 1][symbols[0]].close_time.isoformat(),
               "execution_open": frames[t][symbols[0]].open_time.isoformat(),
               "scheduled_exit_close": frames[t][symbols[0]].close_time.isoformat(),
               "states": decision, "portfolios": {}}
        for mode, account in portfolios.items():
            prior_drawdown = max(0, 1 - account["equity"] / account["peak"])
            account["killed"] = account["killed"] or prior_drawdown >= config.max_drawdown
            allocation = allocate(decision, model, config, mode=mode, killed=account["killed"])
            fills = {s: settle_bar([allocation["targets"][s]], frames[t][s].open, frames[t][s].close,
                                  fee_bps=config.fee_bps, slippage_bps=config.slippage_bps) for s in symbols}
            totals = {k: math.fsum(f[k] for f in fills.values()) for k in ("gross_pnl", "net_pnl", "turnover", "fees", "slippage")}
            account["equity"] += totals["net_pnl"]
            account["peak"] = max(account["peak"], account["equity"])
            row["portfolios"][mode] = {**allocation, **totals, "settlements": fills,
                                       "equity": account["equity"], "drawdown": max(0, 1 - account["equity"] / account["peak"]),
                                       "killed": account["killed"], "gross_exposure": sum(abs(x) for x in allocation["targets"].values()),
                                       "cash_capacity": 1 - sum(abs(x) for x in allocation["targets"].values()), "end_exposure": 0.0}
        rows.append(row)
    summary = {}
    for mode in portfolios:
        records = [row["portfolios"][mode] for row in rows]
        summary[mode] = {k: math.fsum(r[k] for r in records) for k in ("gross_pnl", "net_pnl", "fees", "slippage", "turnover")}
        summary[mode].update(bars=len(rows), final_equity=records[-1]["equity"],
                             max_drawdown=max(r["drawdown"] for r in records),
                             max_gross_exposure=max(r["gross_exposure"] for r in records),
                             mean_gross_exposure=mean(r["gross_exposure"] for r in records),
                             invested_bars=sum(r["gross_exposure"] > 1e-12 for r in records), final_exposure=0.0)
    return {"schema_version": 1, "implementation_version": "normalized-research-v1.1",
            "state_semantics": dict(STATE_SEMANTICS),
            "accounting": "initial-capital-normalized price-unit PnL, not futures returns",
            "score_kind": "uncalibrated_heuristic", "config": asdict(config),
            "input_digest": digest([[f[s].record() for s in symbols] for f in frames]), "model": model,
            "availability": {
                "macro": {"status": "unavailable_pending_valid_input", "requires": "point-in-time releases: actual publication timestamp, value, vintage and source; never revised hindsight"},
                "carry": {"status": "unavailable_pending_valid_input", "requires": "synchronized dated near/far deliverable contract prices, expiries, roll and cost metadata; never main continuous proxy"}},
            "limitations": ["Synthetic/offline research cannot establish profitability or calibrated probabilities.",
                            "Shared risk limits are not realized-risk matched: different exposure and turnover cannot establish allocator alpha or risk-adjusted superiority.",
                            "Only trend/range are learnable regimes; uncertain is reserved unallocated cash mass, not a third learned market forecast.",
                            "Precommitted same-bar close exit is an accounting convention, not the user's real holding-period or overnight decision.",
                            "Fixed initial-capital exposures, no reinvestment or interest; drawdown kill observed at the next decision, not intrabar loss protection.",
                            "No exchange sessions, settlement, contract multipliers, lots, rolls, margin, price limits, liquidity or market-impact simulation.",
                            "Two experts do not establish broad diversification. Frozen noisy conditional estimates can become stale.",
                            "Missing state evidence is not renormalized; scores/edge penalties are uncalibrated heuristics."],
            "summary": summary, "rows": rows}


def _finite(value: object, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name}: expected finite number")
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name}: outside finite bounds")
    return float(value)


def settle_bar(
    expert_exposures: Iterable[float], open_price: float, close_price: float,
    *, fee_bps: float, slippage_bps: float,
) -> dict:
    """Net signed entry notionals, enter at open and precommit exit at close.

    PnL and costs are units of initial research capital, not futures returns.
    """
    op = _finite(open_price, "open", 1e-9, 1e12)
    cl = _finite(close_price, "close", 1e-9, 1e12)
    fee = _finite(fee_bps, "fee_bps", 0, 1000) / 10000
    slip = _finite(slippage_bps, "slippage_bps", 0, 1000) / 10000
    exposure = math.fsum(_finite(x, "exposure", -1e6, 1e6) for x in expert_exposures)
    units = exposure / op
    turnover = abs(units) * (op + cl)
    gross = units * (cl - op)
    return {"exposure": exposure, "units": units, "gross_pnl": gross,
            "turnover": turnover, "fees": turnover * fee,
            "slippage": turnover * slip, "net_pnl": gross - turnover * (fee + slip)}
