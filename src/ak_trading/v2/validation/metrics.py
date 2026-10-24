"""Trading-date block statistics; no minute or ADD is an independent episode."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
import math
import random
from typing import Mapping, TYPE_CHECKING

from ak_trading.v2.contracts import JsonValue, MarketForecast, PathLabel, TradingRule
from ak_trading.v2.schemas import probability_in_bucket
from .experiments import COST_SCENARIOS, content, digest

if TYPE_CHECKING:
    from ak_trading.v2.decision.replay import ReplayResult

BOOTSTRAP_REPLICATES = 1000
HIGH_BUCKETS = ("[0.70,0.75)", "[0.75,0.80)", "[0.80,0.85)", "[0.85,0.90)", "[0.90,0.95)", "[0.95,1.00]")
LOW_BUCKETS = ("[0,0.05)", "[0.05,0.10]", "(0.10,0.20]")


def worst_status(statuses) -> str:
    statuses = tuple(statuses)
    if any(s not in ("PASS", "FAIL", "INSUFFICIENT") for s in statuses):
        raise ValueError("unknown_statistics_status")
    return "FAIL" if "FAIL" in statuses else "INSUFFICIENT" if not statuses or "INSUFFICIENT" in statuses else "PASS"


def _finite(values) -> tuple[float, ...]:
    values = tuple(values)
    if any(type(v) not in (float, int) or not math.isfinite(v) for v in values):
        raise ValueError("finite_numbers_required")
    return tuple(float(v) for v in values)


def binary_brier(probabilities, outcomes) -> float | None:
    p, y = _finite(probabilities), _finite(outcomes)
    if len(p) != len(y) or any(not 0 <= x <= 1 for x in p) or any(x not in (0, 1) for x in y):
        raise ValueError("invalid_binary_samples")
    return sum((a-b)**2 for a, b in zip(p, y)) / len(p) if p else None


def multiclass_brier(probabilities, outcomes) -> float | None:
    if len(probabilities) != len(outcomes):
        raise ValueError("classification_length_mismatch")
    losses = []
    for p, y in zip(probabilities, outcomes):
        _finite(p.values())
        if y not in p or abs(sum(p.values()) - 1) > 1e-12 or any(not 0 <= v <= 1 for v in p.values()):
            raise ValueError("invalid_multiclass_sample")
        losses.append(sum((v - float(k == y))**2 for k, v in p.items()))
    return sum(losses) / len(losses) if losses else None


def block_bootstrap(values, blocks, seed: int) -> tuple[float | None, float | None]:
    """Resample whole dates, weighting each date equally before resampling."""
    values = _finite(values)
    if type(seed) is not int or len(values) != len(blocks):
        raise ValueError("invalid_bootstrap_arguments")
    grouped = defaultdict(list)
    for value, block in zip(values, blocks):
        if not isinstance(block, str) or not block:
            raise ValueError("explicit_block_id_required")
        grouped[block].append(value)
    means = [sum(grouped[b]) / len(grouped[b]) for b in sorted(grouped)]
    if len(means) < 2:
        return None, None
    rng = random.Random(seed)
    samples = sorted(sum(rng.choice(means) for _ in means) / len(means) for _ in range(BOOTSTRAP_REPLICATES))
    return samples[49], samples[949]


def _ece(probabilities, outcomes) -> float | None:
    if not probabilities:
        return None
    classes = tuple(probabilities[0])
    errors = []
    for category in classes:
        buckets = defaultdict(list)
        for p, y in zip(probabilities, outcomes):
            buckets[min(19, int(p[category] * 20))].append((p[category], float(y == category)))
        errors.append(sum(abs(sum(p-y for p, y in bucket)) for bucket in buckets.values()) / len(probabilities))
    return sum(errors) / len(errors)


@dataclass(frozen=True)
class _Score:
    forecast: MarketForecast
    block: str
    event: str
    barrier: str
    geometry: str
    probabilities: Mapping[str, float]
    outcome: str
    pinball: float | None = None
    coverage: float | None = None
    width: float | None = None


def _geometry(forecast: MarketForecast, event: str) -> str:
    bare = event.split(":", 1)[-1] if event.startswith("PERSIST:") else "STOP_TOUCH" if event.startswith("STOP_TOUCH:") else event
    if bare in ("DIRECTION", "PATH", "UP_TOUCH", "DOWN_TOUCH", "UP_TREND", "DOWN_TREND", "COMPRESSION", "AMPLITUDE"):
        return f"geometry-v2.1.1|{bare}|ANCHOR_ATR"
    keys = {e.geometry_key for e in forecast.evidence if e.event == event and e.model_id == forecast.model_id
            and e.product == forecast.product and e.horizon == forecast.horizon
            and e.geometry_key.startswith("geometry-v2.1.1|")}
    if len(keys) > 1:
        raise ValueError("conflicting_forecast_geometry_evidence")
    # No tick lives on MarketForecast. Never guess the missing fifth bucket.
    return next(iter(keys)) if keys else "UNAVAILABLE"


def _scores(f: MarketForecast, label: PathLabel, block: str) -> list[_Score]:
    scores = []
    for event, probs, outcome, barrier in (
        ("DIRECTION", f.direction_probabilities, label.direction, "COMMON"),
        ("PATH", f.path_probabilities, label.path_outcome, f.barrier_key),
    ):
        if outcome is not None:
            scores.append(_Score(f, block, event, barrier, _geometry(f, event), probs, outcome))
    for event, prob in f.persistence_probabilities.items():
        outcome = label.persistence[event]
        if outcome is not None:
            event = "PERSIST:" + event
            scores.append(_Score(f, block, event, "COMMON", _geometry(f, event), {"YES": prob, "NO": 1-prob}, "YES" if outcome else "NO"))
    for side, prob in f.stop_touch_probabilities.items():
        outcome = label.stop_touched.get(side)
        if outcome is not None:
            event = "STOP_TOUCH:" + side
            scores.append(_Score(f, block, event, "COMMON", _geometry(f, event), {"YES": prob, "NO": 1-prob}, "YES" if outcome else "NO"))
    if f.return_before_break_probability is not None and label.competing_outcome in ("RETURN_FIRST", "BREAK_FIRST", "NEITHER"):
        p = f.return_before_break_probability
        scores.append(_Score(f, block, "RETURN_BEFORE_BREAK", "COMMON", _geometry(f, "RETURN_BEFORE_BREAK"),
                             {"YES": p, "NO": 1-p}, "YES" if label.competing_outcome == "RETURN_FIRST" else "NO"))
    for t in f.time_cdf:
        if t.unknown_probability or (t.event in ("RETURN_FIRST", "BREAK_FIRST") and label.competing_outcome in (None, "AMBIGUOUS")):
            continue
        step = {"UP_TOUCH": label.first_up_step, "DOWN_TOUCH": label.first_down_step,
                "RETURN_FIRST": label.return_step, "BREAK_FIRST": label.break_step}[t.event]
        if t.event in ("RETURN_FIRST", "BREAK_FIRST") and label.competing_outcome != t.event:
            step = None
        scores.append(_Score(f, block, f"TIME:{t.event}:{t.step}",
                             f.barrier_key if t.event in ("UP_TOUCH", "DOWN_TOUCH") else "COMMON",
                             _geometry(f, t.event), {"YES": t.probability, "NO": 1-t.probability},
                             "YES" if step is not None and step <= t.step else "NO"))
    if label.terminal_return is not None:
        qs = (("Q05", .05), ("Q25", .25), ("Q50", .50), ("Q75", .75), ("Q95", .95))
        loss = sum(max(q * (label.terminal_return-f.return_quantiles[key]),
                       (q-1) * (label.terminal_return-f.return_quantiles[key])) for key, q in qs) / 5
        coverage = float(f.return_quantiles["Q05"] <= label.terminal_return <= f.return_quantiles["Q95"])
        scores.append(_Score(f, block, "AMPLITUDE", "COMMON", _geometry(f, "AMPLITUDE"), {}, "", loss, coverage,
                             f.return_quantiles["Q95"]-f.return_quantiles["Q05"]))
    return scores


def evaluate_forecasts(forecasts: tuple[MarketForecast, ...], labels: tuple[PathLabel, ...],
                        block_ids: Mapping[str, str], seed: int) -> Mapping[str, JsonValue]:
    """Score mature on-time requests; late failures count without requiring a label."""
    by_label = {}
    for label in labels:
        if label.request_id in by_label:
            raise ValueError("duplicate_label_request_id")
        by_label[label.request_id] = label
    seen, request_ids, blocks_by_day = {}, {}, {}
    excluded = Counter()
    samples = []
    common = {}
    for f in forecasts:
        if f.published_at_nano>=f.window_start_nano and f.quality.status!="INVALID":raise ValueError("late_forecast_not_invalid")
    for f in forecasts:
        if f.quality.status=="INVALID" and "FORECAST_LATE" in f.quality.reasons:
            if not f.published_at_nano>=f.window_start_nano or f.expires_at_nano!=f.window_end_nano:
                raise ValueError("late_forecast_window_or_expiry_changed")
    for f in sorted(forecasts,key=lambda p:(p.published_at_nano,p.forecast_id)):
        if f.forecast_id in seen:
            if seen[f.forecast_id]!=f:raise ValueError("conflicting_forecast_id")
            continue
        seen[f.forecast_id]=f
        if f.request_id in request_ids:
            original=request_ids[f.request_id]
            if (f.snapshot_id,f.window_start_nano,f.window_end_nano,f.horizon,f.frozen_range)!=(original.snapshot_id,original.window_start_nano,original.window_end_nano,original.horizon,original.frozen_range):
                raise ValueError("retried_request_identity_changed")
            continue
        request_ids[f.request_id]=f
        if f.quality.status == "INVALID":
            excluded["FORECAST_LATE" if "FORECAST_LATE" in f.quality.reasons else "INVALID"] += 1
            continue
        label = by_label.get(f.request_id)
        if label is None:
            excluded["MISSING_LABEL"] += 1
            continue
        if (label.contract_id, label.horizon, label.window_start_nano, label.window_end_nano, label.label_version, label.range_id) != (
            f.contract_id, f.horizon, f.window_start_nano, f.window_end_nano, f.label_version,
            f.frozen_range.range_id if f.frozen_range else None
        ):
            raise ValueError("forecast_label_identity_mismatch")
        if label.status != "MATURE":
            excluded[label.status] += 1
            continue
        if label.available_at_nano is None or label.available_at_nano < label.window_end_nano:
            raise ValueError("mature_label_without_maturity_time")
        block = block_ids.get(f.request_id)
        if not isinstance(block, str) or not block:
            raise ValueError("explicit_request_block_required")
        if f.trading_day in blocks_by_day and blocks_by_day[f.trading_day] != block:
            raise ValueError("same_trading_day_cannot_be_split_into_independent_blocks")
        blocks_by_day[f.trading_day] = block
        for score in _scores(f, label, block):
            identity = (f.contract_id, f.snapshot_id, f.horizon, score.event, score.geometry, score.barrier, f.model_id)
            if identity in common:
                previous = common[identity]
                if (previous.probabilities, previous.outcome, previous.pinball, previous.coverage) != (score.probabilities, score.outcome, score.pinball, score.coverage):
                    raise ValueError("COMMON_barrier_components_disagree")
                continue
            common[identity] = score
            samples.append(score)
    if set(by_label) - set(request_ids):
        raise ValueError("labels_without_forecasts")
    grouped = defaultdict(list)
    for s in samples:
        f = s.forecast
        buckets = ["ALL"]
        if "YES" in s.probabilities:
            options = LOW_BUCKETS if s.event.startswith("STOP_TOUCH:") else HIGH_BUCKETS
            buckets += [b for b in options if probability_in_bucket(s.probabilities["YES"], b)]
        for policy in ("confidence-ordinary-v2.1.0", "confidence-martingale-v2.1.0"):
            for bucket in buckets:
                key = (f.model_id, f.product, f.contract_id, "DAILY" if f.duration_seconds == 86400 else "MINUTE",
                       f.horizon.unit, f.horizon.value, f.session_kind, f.trading_day.year, f.base_regime,
                       s.event, policy, bucket, s.barrier, s.geometry)
                grouped[key].append(s)
    # Report product-primary rows as well as every registered contract slice.
    product_groups=defaultdict(list)
    for key,members in list(grouped.items()):
        key=list(key);key[2]=None
        product_groups[tuple(key)].extend(members)
    grouped.update(product_groups)
    groups = []
    diagnostics = []
    for key, members in sorted(grouped.items(),key=lambda item:tuple("" if v is None else str(v) for v in item[0])):
        model, product, contract, mode, unit, value, session, year, regime, event, policy, bucket, barrier, geometry = key
        n, blocks = len(members), len({s.block for s in members})
        amplitude = event == "AMPLITUDE"
        p, y = [s.probabilities for s in members], [s.outcome for s in members]
        binary = not amplitude and "YES" in p[0]
        if amplitude:
            brier = logloss = ece = error = None
            pinball = sum(s.pinball for s in members) / n
            coverage = sum(s.coverage for s in members) / n
            losses = [s.pinball for s in members]
            baseline = None
        else:
            losses = [(s.probabilities["YES"]-float(s.outcome == "YES"))**2 if binary else
                      sum((v-float(c == s.outcome))**2 for c, v in s.probabilities.items()) for s in members]
            brier = sum(losses) / n
            logloss = -sum(math.log(max(s.probabilities[s.outcome], 1e-15)) for s in members) / n
            ece = _ece(p, y)
            error = abs(sum(s.probabilities["YES"]-float(s.outcome == "YES") for s in members)/n) if binary else ece
            pinball = coverage = None
            frequencies = Counter(y)
            baseline = sum((frequencies["YES"]/n-float(o == "YES"))**2 for o in y)/n if binary else sum(
                sum((frequencies[c]/n-float(o == c))**2 for c in p[0]) for o in y)/n
        low, high = block_bootstrap(losses, [s.block for s in members], seed)
        martingale = "martingale" in policy
        sufficient = n >= (500 if martingale else 200) and blocks >= (60 if martingale else 30)
        failed = (not amplitude and (error > (.03 if martingale else .05) or brier > baseline + 1e-12)) or (amplitude and not .85 <= coverage <= .95)
        status = "INSUFFICIENT" if not sufficient or geometry == "UNAVAILABLE" else "FAIL" if failed else "PASS"
        groups.append({"group_key": "", "split_id": "", "model_bundle_sha256": "", "product": product,
                       "contract_id": contract, "mode": mode, "horizon_unit": unit, "horizon_value": value,
                       "session": session, "year": year, "regime": regime, "event": event, "policy_id": policy,
                       "barrier_key": barrier, "geometry_key": geometry, "bucket": bucket, "calibration_error": error,
                       "n": n, "blocks": blocks, "brier": brier, "log_loss": logloss, "ece": ece,
                       "pinball": pinball, "coverage": coverage, "ci_lower": low, "ci_upper": high, "status": status})
        diagnostics.append({"model_id": model, "group_index": len(groups)-1, "baseline_brier": baseline,
                            "interval_width": sum(s.width for s in members)/n if amplitude else None,
                            "ambiguity_rate": sum(s.outcome == "AMBIGUOUS" for s in members)/n})
    return {"forecast_groups": groups, "exclusions": dict(sorted(excluded.items())), "diagnostics": diagnostics,
            "bootstrap": {"method": "TRADING_DAY_BLOCK", "replicates": 1000, "seed": seed}}


def evaluate_replay(result: ReplayResult, initial_equity: Decimal,
                      block_ids: Mapping[str, str], seed: int, *,
                      rules: tuple[TradingRule, ...]) -> Mapping[str, JsonValue]:
    if not isinstance(initial_equity, Decimal) or not initial_equity.is_finite() or initial_equity <= 0:
        raise ValueError("positive_decimal_initial_equity_required")
    equities = sorted(result.equity, key=lambda a: a.as_of_nano)
    if len({a.account_id for a in equities}) > 1:
        raise ValueError("independent_accounts_cannot_be_concatenated")
    if any(a.initial_cash_cny != initial_equity for a in equities):
        raise ValueError("account_initial_cash_changed")
    accounting = _episode_accounting(result,initial_equity,rules)
    valid_fills = accounting.pop("valid_fills")
    fill_ids, episodes, outstanding = set(), defaultdict(list), Counter()
    for fill in valid_fills:
        fill_ids.add(fill.fill_id)
        episodes[fill.episode_id].append(fill)
        outstanding[fill.episode_id] += fill.lots if fill.offset == "OPEN" else -fill.lots
        if outstanding[fill.episode_id] < 0 and accounting["replay_summary"]["status"] == "COMPLETE":
            raise ValueError("episode_closed_more_than_opened")
    mature = accounting["closed_episode_ids"]
    if any(e not in block_ids for e in mature):
        raise ValueError("explicit_episode_block_required")
    day_blocks = {}
    for episode in mature:
        day = min(episodes[episode], key=lambda f: f.execution_nano).trading_day
        if day in day_blocks and day_blocks[day] != block_ids[episode]:
            raise ValueError("episode_dates_not_independent")
        day_blocks[day] = block_ids[episode]
    peak, drawdown, peak_margin, max_inventory = initial_equity, Decimal(0), Decimal(0), 0
    shortfall = Decimal(0)
    daily = {}
    recovery_start, longest, previous_day = None, 0, None
    days_since_peak = 0
    for a in equities:
        peak = max(peak, a.equity_cny)
        drawdown = max(drawdown, (peak-a.equity_cny)/peak)
        peak_margin = max(peak_margin, a.margin_cny + a.reserved_margin_cny)
        max_inventory = max(max_inventory, sum(l.lots for l in a.lots))
        shortfall = max(shortfall, a.margin_cny+a.reserved_margin_cny-a.equity_cny)
        daily[a.trading_day] = a.equity_cny
        if a.trading_day != previous_day:
            days_since_peak += 1
            previous_day = a.trading_day
        if a.equity_cny >= peak:
            longest = max(longest, days_since_peak)
            days_since_peak = 0
            recovery_start = None
        else:
            recovery_start = recovery_start or a.trading_day
    net = equities[-1].equity_cny-initial_equity if equities else Decimal(0)
    # Account-level daily returns can be scored without guessing a multiplier
    # or allocating concurrent closes across strategies from incomplete metadata.
    daily_changes, last = [], initial_equity
    for day, equity in sorted(daily.items()):
        daily_changes.append(equity-last)
        last = equity
    lower, upper = block_bootstrap([float(v) for v in daily_changes], [str(d) for d in sorted(daily)], seed)
    losses = sorted((max(Decimal(0), -v) for v in daily_changes), reverse=True)
    tail = losses[:max(1, math.ceil(len(losses)*.05))]
    cvar = sum(tail)/len(tail) if tail else None
    decisions = {d.decision_id: d for d in result.decisions}
    selected = sum(d.outcome == "SELECTED" for d in decisions.values())
    no_trade = sum(d.outcome == "NO_TRADE" for d in decisions.values())
    reasons = Counter(reason for d in decisions.values() for reason in d.reasons)
    reasons.update(reason for e in result.execution_results for reason in e.reasons)
    complete = accounting["replay_summary"]["status"] == "COMPLETE"
    episode_pnl = [accounting["episode_net_pnl_cny"][e] for e in mature]
    episode_blocks = [block_ids[e] for e in mature]
    ev_low,_ = block_bootstrap([float(v) for v in episode_pnl],episode_blocks,seed)
    episode_losses=sorted((max(Decimal(0),-v) for v in episode_pnl),reverse=True)
    tail_mass=Decimal(len(episode_losses))*Decimal("0.05")
    rest=tail_mass;tail_loss=Decimal(0)
    for loss in episode_losses:
        weight=min(Decimal(1),rest);tail_loss+=weight*loss;rest-=weight
        if rest<=0:break
    episode_cvar=tail_loss/tail_mass if tail_mass else None
    budgets={p.episode_id:p.budget for p in result.proposals if p.action=="OPEN" and p.budget is not None}
    episode_records=[]
    forecasts_by_id={f.forecast_id:f for f in result.forecasts}
    for episode in mature:
        proposal=next((p for p in result.proposals if p.episode_id==episode and p.action=="OPEN"),None)
        forecast=forecasts_by_id.get(proposal.forecast_id) if proposal else None
        budget=budgets.get(episode)
        if forecast is None or budget is None:continue
        episode_records.append({"episode_id":episode,"strategy_id":proposal.strategy_id,"product":forecast.product,
                                "contract_id":forecast.contract_id,"mode":"DAILY" if forecast.horizon.unit=="TRADING_DAY" else "MINUTE",
                                "horizon_unit":forecast.horizon.unit,"horizon_value":forecast.horizon.value,
                                "block_id":block_ids[episode],"net_pnl_cny":str(accounting["episode_net_pnl_cny"][episode]),
                                "budget_cny":str(budget.planned_loss_cny),"stress_loss_ratio":float(budget.stress_loss_cny/budget.equity_at_start_cny)})
    return {"episodes": len(mature), "blocks": len({block_ids[e] for e in mature}),
            "episode_records":episode_records,
            "open_episodes": len(accounting["replay_summary"]["open_episode_ids"]),
            "net_pnl_cny": str(sum(episode_pnl,Decimal(0))) if accounting["replay_summary"]["closed_episode_net_pnl_cny"] is not None else None,
            "conservative_ev_cny":str(Decimal(str(ev_low))) if ev_low is not None else None,
            "cvar95_cny":str(episode_cvar) if episode_cvar is not None else None,
            "episode_net_pnl_cny":{k:str(v) for k,v in accounting["episode_net_pnl_cny"].items()},
            "replay_summary":accounting["replay_summary"],
            "daily_conservative_ev_cny": str(Decimal(str(lower))) if lower is not None else None,
            "daily_cvar95_cny": str(cvar) if cvar is not None else None,
            "max_drawdown_ratio": float(drawdown), "peak_margin_cny": str(peak_margin),
            "margin_shortfall_cny": str(shortfall), "max_inventory": max_inventory,
            "longest_recovery_days": None if recovery_start else longest,
            "admission_rate": selected/len(decisions) if decisions else 0.0,
            "no_trade_rate": no_trade/len(decisions) if decisions else 0.0,
            "rejection_reasons": dict(reasons), "status": "INSUFFICIENT" if accounting["replay_summary"]["status"] != "FAILED" else "FAIL",
            "reasons": list(accounting["replay_summary"]["reasons"]),
            "bootstrap": {"method": "TRADING_DAY_BLOCK", "replicates": 1000, "seed": seed}}


def _episode_accounting(result: ReplayResult, initial_equity: Decimal, rules: tuple[TradingRule,...]) -> dict:
    """Validate committed fill/rule originals and recompute episode FIFO cashflows."""
    by_rule={r.rule_id:r for r in rules}
    if len(by_rule)!=len(rules):raise ValueError("duplicate_rule_id")
    intents={i.intent_id:i for i in result.intents}
    if len(intents)!=len(result.intents):raise ValueError("duplicate_intent_id")
    seen={};valid=[];reasons=[];failed=False
    checkpoints=sorted(result.checkpoints,key=lambda c:c.sequence)
    for fill in sorted(result.fills,key=lambda f:(f.execution_nano,f.fill_id)):
        h=digest(fill)
        if fill.fill_id in seen:
            if seen[fill.fill_id]!=h:
                failed=True;reasons.append("CONFLICTING_DUPLICATE_FILL")
            continue
        seen[fill.fill_id]=h
        committed=next((c for c in checkpoints if fill.fill_id in c.processed_fill_hashes),None)
        if committed is None:
            reasons.append("UNCOMMITTED_FILL");continue
        if committed.processed_fill_hashes[fill.fill_id]!=h:
            failed=True;reasons.append("PROCESSED_FILL_HASH_MISMATCH");continue
        if not committed.processed_event_ids or committed.account.as_of_nano<fill.execution_nano:
            reasons.append("FILL_COMMIT_EVENT_MISSING");continue
        rule=by_rule.get(fill.rule_id)
        if rule is None:
            reasons.append("FILL_RULE_ORIGINAL_MISSING");continue
        if committed.rule_hashes.get(rule.rule_id)!=digest(rule):
            failed=True;reasons.append("FILL_RULE_HASH_MISMATCH");continue
        if rule.contract_id!=fill.contract_id or not rule.available_at_nano<=fill.execution_nano or not rule.effective_from_nano<=fill.execution_nano<rule.effective_until_nano:
            failed=True;reasons.append("FILL_RULE_PIT_MISMATCH");continue
        intent=intents.get(fill.intent_id)
        if intent is None or not any(any(f.fill_id==fill.fill_id and f==fill for f in r.fills) for r in result.execution_results):
            reasons.append("FILL_INTENT_OR_EXECUTION_REFERENCE_MISSING");continue
        if (intent.contract_id,intent.strategy_id,intent.episode_id,intent.offset)!=(fill.contract_id,fill.strategy_id,fill.episode_id,fill.offset):
            failed=True;reasons.append("FILL_INTENT_IDENTITY_MISMATCH");continue
        leg={"OPEN":rule.open_fee,"CLOSE":rule.close_fee,"CLOSE_TODAY":rule.close_today_fee}[fill.offset]
        if leg is None:
            reasons.append("FILL_FEE_RULE_MISSING");continue
        fee=fill.lots*(leg.per_lot_cny+fill.execution_price*rule.multiplier*leg.notional_rate)
        if fee!=fill.fee_cny or fill.lots%rule.lot_step or fill.execution_price%rule.price_tick or fill.slippage_cny!=abs(fill.execution_price-fill.reference_price)*fill.lots*rule.multiplier:
            failed=True;reasons.append("FILL_ECONOMIC_FACTS_MISMATCH");continue
        valid.append(fill)
    terminal=result.equity[-1] if result.equity else (checkpoints[-1].account if checkpoints else None)
    last=checkpoints[-1] if checkpoints else None
    if last and set(last.processed_fill_hashes)-set(seen):reasons.append("ANCESTOR_FILL_ORIGINAL_MISSING")
    queues=defaultdict(list);net=defaultdict(lambda:Decimal(0));owners={}
    for fill in valid:
        e=fill.episode_id;owner=(fill.contract_id,fill.strategy_id)
        if e in owners and owners[e]!=owner:
            failed=True;reasons.append("EPISODE_OWNER_MISMATCH");continue
        owners[e]=owner
        net[e]-=fill.fee_cny
        if fill.offset=="OPEN":
            if any(row[0].side!=fill.side for row in queues[e]):
                failed=True;reasons.append("EPISODE_DIRECTION_CHANGED");continue
            queues[e].append([fill,fill.lots])
        else:
            remaining=fill.lots
            for opened in queues[e]:
                origin,quantity=opened
                if not quantity:continue
                if origin.side!=-fill.side or (origin.trading_day==fill.trading_day)!=(fill.offset=="CLOSE_TODAY"):
                    continue
                quantity=min(quantity,remaining)
                net[e]+=origin.side*quantity*(fill.execution_price-origin.execution_price)*by_rule[fill.rule_id].multiplier
                opened[1]-=quantity;remaining-=quantity
                if not remaining:break
            if remaining:
                reasons.append("ANCESTOR_OPEN_OR_OFFSET_MISMATCH")
    active_episodes={l.episode_id for l in terminal.lots} if terminal else set()
    pending_ids=[i.intent_id for i in last.pending_intents] if last else []
    pending_episodes={i.episode_id for i in last.pending_intents} if last else set()
    resource_episodes={s.episode_id for s in last.states if s.episode_id is not None and (s.budget is not None or s.pending_intent_ids)} if last else set()
    open_ids=sorted(active_episodes|pending_episodes|resource_episodes|{e for e,q in queues.items() if sum(row[1] for row in q)})
    closed=sorted(e for e in queues if e not in open_ids and sum(row[1] for row in queues[e])==0)
    known=not reasons and terminal is not None and terminal.quality.status=="READY"
    if terminal is None:reasons.append("TERMINAL_ACCOUNT_MISSING")
    elif terminal.quality.status!="READY":reasons.append("VALUATION_SUBTOTAL_INCOMPLETE")
    closed_net=sum((net[e] for e in closed),Decimal(0));open_net=sum((net[e] for e in open_ids),Decimal(0))
    if known and terminal.equity_cny-initial_equity != closed_net+open_net+terminal.unrealized_cny:
        failed=True;known=False;reasons.append("EPISODE_ACCOUNT_RECONCILIATION_MISMATCH")
    if open_ids or pending_ids:reasons.append("OPEN_EPISODES_OR_PENDING_INTENTS")
    if result.status!="COMPLETE" or result.reconciliation.get("status")!="PASS":reasons.append("REPLAY_INCOMPLETE")
    summary={"account_id":terminal.account_id if terminal else "UNKNOWN","initial_equity_cny":str(initial_equity),
             "terminal_equity_cny":str(terminal.equity_cny) if terminal and terminal.quality.status=="READY" else None,
             "closed_episode_net_pnl_cny":str(closed_net) if known else None,
             "open_episode_realized_net_pnl_cny":str(open_net) if known else None,
             "terminal_unrealized_cny":str(terminal.unrealized_cny) if known else None,
             "open_episode_ids":open_ids,"pending_intent_ids":pending_ids,
             "reconciliation_status":str(result.reconciliation.get("status","INCOMPLETE")),
             "status":"FAILED" if failed else "INCOMPLETE" if reasons else "COMPLETE","reasons":sorted(set(reasons))}
    # Unknown original facts invalidate the whole statistical path, not only a bad row.
    return {"replay_summary":summary,"valid_fills":tuple(valid),"closed_episode_ids":closed if known else [],
            "episode_net_pnl_cny":{e:net[e] for e in closed} if known else {}}


def forecast_group_key(group: Mapping) -> str:
    from ak_trading.v2.schemas import canonical_json_bytes
    return canonical_json_bytes(["FORECAST", *(group[k] for k in ("split_id", "product", "contract_id", "mode", "horizon_unit", "horizon_value", "session", "year", "regime", "event", "policy_id", "bucket", "barrier_key", "geometry_key"))]).decode().strip()


def strategy_group_key(group: Mapping) -> str:
    from ak_trading.v2.schemas import canonical_json_bytes
    return canonical_json_bytes(["STRATEGY", *(group[k] for k in ("split_id", "run_policy_sha256", "strategy_id", "product", "contract_id", "mode", "horizon_unit", "horizon_value", "macro_mode", "cost_scenario", "exit_binding_sha256"))]).decode().strip()
