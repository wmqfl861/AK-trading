"""EXITSTATE feature production for the M3C exit library (08-M3C §6.2/§6.5).

Pure, context-derived episode/account state features under the
``EXITSTATE:`` namespace. This module never reads M2 factor inputs, never
consults M3A forecasts and never mutates any ledger fact: every value is
recomputed from the explicit :class:`ExitContext` (committed episode fills,
real position lots, valuation marks, the frozen ``RoundBudget``, trading
rules and the session calendar) plus the previously committed
:class:`ExitState`.

Key rulings implemented here:

* ``A0`` is the entry-frozen ``RoundBudget.initial_atr_points`` (VOL004
  Wilder scale, 08 §5.1). No runtime ATR key is consumed anywhere in P1.
* ``Nt = RGt - Ft + Ut - Kclose(t)`` — the *projected net liquidation*
  value. When any component is unknown (unvalued mark, unknown fee leg,
  missing rule) the value is ``None`` with non-empty quality reasons: it is
  never replaced by ``0``, the entry price or an unrealized-only subtotal
  (08 §6.2 "估值失效", §6.5).
* Peaks move forward only. A STALE/missing current observation carries the
  last committed peak with a ``DEGRADED`` gap marker instead of refreshing
  it; a later backfill never rewrites committed decisions (08 §6.2).
* The trading clock intersects wall time with legal session windows
  (evidence != UNKNOWN). Lunch breaks do not age an episode and missing
  bars never pause the clock; daily ages count completed trading days only
  (entry-day close is the first completed step, 08 §6.2/§10-6).

The public surface frozen for P0/P1 (08 §4.2):

    exit_feature_registry_v1(mode) -> tuple[ExitFeatureSpec, ...]
    build_exit_feature_snapshot(context, previous) -> ExitFeatureSnapshot
    collect_exit_facts(context, previous) -> ExitObservationFacts   (M3C internal)
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Mapping

from ak_trading.v2.artifacts import canonical_json_bytes, sha256_hex
from ak_trading.v2.contracts import (
    DataQuality,
    ExitContext,
    ExitFeatureSpec,
    ExitFeatureSnapshot,
    ExitFeatureValue,
    ExitState,
    FeeLeg,
    Fill,
    PositionLot,
    RoundBudget,
    SessionWindow,
    TradingRule,
    ValuationMark,
)
from ak_trading.v2.decision.execution import fee_amount

EXIT_FEATURE_FORMULA_VERSION = "v1.0.0"
MINUTE_NANO = 60_000_000_000
DURATION_NANO: Mapping[str, int] = {"MINUTE": 60, "DAILY": 86400}


class ExitFactsError(ValueError):
    """Raised when the explicit context cannot yield a consistent fact set."""


# ─── shared canonical helpers (JSON-safe projections; Decimal -> str) ───────

def exit_jsonable(value: object) -> object:
    """Recursively convert Decimals/tuples into canonical-JSON-safe values."""
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ExitFactsError("non_finite_decimal_in_projection")
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): exit_jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [exit_jsonable(item) for item in value]
    raise ExitFactsError(f"unencodable_projection_type:{type(value).__name__}")


def exit_digest(payload: object) -> str:
    """sha256 over the canonical bytes of a JSON-safe projection."""
    return sha256_hex(canonical_json_bytes(exit_jsonable(payload)))


# ─── the 16 field definitions x mode (08 §6.2 table, prep §3 freeze) ────────

@dataclass(frozen=True)
class _FieldDef:
    name: str
    unit: str
    window_kind: str
    source_scope: str
    clock_unit: str
    source_keys: tuple[str, ...]


_FIELD_DEFS: tuple[_FieldDef, ...] = (
    _FieldDef("unrealized_pnl_cny", "cny", "CURRENT_STATE", "EPISODE", "NONE",
              ("account.lots", "valuation_mark")),
    _FieldDef("unrealized_pnl_r", "ratio", "CURRENT_STATE", "EPISODE", "NONE",
              ("account.lots", "round_budget", "valuation_mark")),
    _FieldDef("entry_distance_atr", "atr", "CURRENT_STATE", "EPISODE", "NONE",
              ("account.lots", "round_budget", "valuation_mark")),
    _FieldDef("episode_net_liquidation_cny", "cny", "EPISODE_PREFIX", "EPISODE", "NONE",
              ("episode_fills", "account.lots", "round_budget", "trading_rule", "valuation_mark")),
    _FieldDef("episode_net_liquidation_r", "ratio", "EPISODE_PREFIX", "EPISODE", "NONE",
              ("episode_fills", "account.lots", "round_budget", "trading_rule", "valuation_mark")),
    _FieldDef("peak_net_liquidation_r", "ratio", "EPISODE_PREFIX", "EPISODE", "NONE",
              ("committed_exit_state", "episode_fills")),
    _FieldDef("profit_giveback_r", "ratio", "EPISODE_PREFIX", "EPISODE", "NONE",
              ("committed_exit_state", "episode_fills")),
    _FieldDef("peak_favorable_price", "price_point", "EPISODE_PREFIX", "EPISODE", "NONE",
              ("committed_exit_state", "valuation_mark")),
    _FieldDef("trailing_atr_distance", "atr", "EPISODE_PREFIX", "EPISODE", "NONE",
              ("committed_exit_state", "round_budget", "valuation_mark")),
    _FieldDef("effective_stop_distance_atr", "atr", "CURRENT_STATE", "EPISODE", "NONE",
              ("committed_exit_state", "round_budget", "valuation_mark")),
    _FieldDef("age_trading_minutes", "trading_minute", "EPISODE_PREFIX_CALENDAR", "EPISODE",
              "TRADING_MINUTE", ("episode_fills", "session_calendar")),
    _FieldDef("age_trading_days", "trading_day", "EPISODE_PREFIX_CALENDAR", "EPISODE",
              "TRADING_DAY", ("episode_fills", "session_calendar")),
    _FieldDef("remaining_steps", "steps", "CURRENT_STATE", "EPISODE", "SAME_AS_MODE",
              ("entry_state", "round_budget", "session_calendar")),
    _FieldDef("elapsed_fraction", "fraction", "CURRENT_STATE", "EPISODE", "SAME_AS_MODE",
              ("episode_fills", "entry_state", "round_budget", "session_calendar")),
    _FieldDef("account_drawdown_ratio", "ratio", "ACCOUNT_PREFIX", "ACCOUNT", "NONE",
              ("account",)),
    _FieldDef("closed_fraction", "ratio", "LIQUIDATION_PREFIX", "EPISODE", "NONE",
              ("committed_exit_state",)),
)


def _key(name: str, duration: int) -> str:
    return f"EXITSTATE:{name}:{EXIT_FEATURE_FORMULA_VERSION}:d{duration}"


def exit_feature_registry_v1(
        mode: Literal["MINUTE", "DAILY"]) -> tuple[ExitFeatureSpec, ...]:
    """The exact registered feature specs for one observation mode.

    d60 -> 15 specs (dual-mode fields at d60 + ``age_trading_minutes``);
    d86400 -> 15 specs (dual-mode fields at d86400 + ``age_trading_days``);
    together the frozen 30-key EXITSTATE registry (prep §3).
    """
    if mode not in DURATION_NANO:
        raise ValueError("mode_must_be_MINUTE_or_DAILY")
    duration = DURATION_NANO[mode]
    specs = []
    for definition in _FIELD_DEFS:
        single = (definition.name == "age_trading_minutes" and duration == 60) or (
            definition.name == "age_trading_days" and duration == 86400)
        dual = definition.name not in ("age_trading_minutes", "age_trading_days")
        if not (single or dual):
            continue
        clock = definition.clock_unit
        if clock == "SAME_AS_MODE":
            clock = "TRADING_MINUTE" if duration == 60 else "TRADING_DAY"
        specs.append(ExitFeatureSpec(
            feature_key=_key(definition.name, duration),
            formula_version=EXIT_FEATURE_FORMULA_VERSION,
            unit=definition.unit,
            window_kind=definition.window_kind,  # type: ignore[arg-type]
            minimum_source_facts=1,
            source_scope=definition.source_scope,  # type: ignore[arg-type]
            clock_unit=clock,  # type: ignore[arg-type]
            required_source_keys=tuple(sorted(definition.source_keys))))
    return tuple(specs)


# ─── raw fact collection ────────────────────────────────────────────────────

@dataclass(frozen=True)
class ExitObservationFacts:
    """Every numeric fact the P1 policies consume, with explicit blockers.

    ``None`` plus non-empty ``*_reasons`` means "unknown" — never a guessed
    substitute (08 §6.5). Prices are actual prices; ``price_peak`` is the
    actual-price favorable peak (long max / short min close).
    """
    duration_seconds: int
    now_nano: int
    side: int
    a0: Decimal
    r0: Decimal
    first_fill_nano: int | None
    # valuation
    mark: ValuationMark | None
    mark_current: bool
    valuation_reasons: tuple[str, ...]
    # position / cashflows
    rule: TradingRule | None
    cost_reasons: tuple[str, ...]
    remaining_lots: int
    weighted_entry: Decimal | None
    realized_gross: Decimal
    fees_paid: Decimal
    unrealized: Decimal | None
    exit_cost: Decimal | None
    net_liquidation: Decimal | None
    net_liquidation_r: Decimal | None
    nli_reasons: tuple[str, ...]
    # peaks (actual-price space; carried values survive gaps)
    price_peak: Decimal | None
    price_peak_carried: bool
    price_peak_reasons: tuple[str, ...]
    net_peak_r: Decimal | None
    net_peak_carried: bool
    net_peak_reasons: tuple[str, ...]
    giveback_r: Decimal | None
    # protection geometry
    effective_stop_price: Decimal
    # clocks
    age_steps: Decimal | int | None
    age_reasons: tuple[str, ...]
    remaining_steps: Decimal | int | None
    remaining_reasons: tuple[str, ...]
    elapsed_fraction: Decimal | None
    elapsed_reasons: tuple[str, ...]
    deadline_nano: int | None
    # account / liquidation
    account_drawdown: Decimal | None
    account_reasons: tuple[str, ...]
    closed_fraction: Decimal | None
    closed_reasons: tuple[str, ...]


def _episode_scope(context: ExitContext) -> tuple[str, str, str, str]:
    return (context.account_id, context.contract_id,
            context.strategy_id, context.episode_id)


def _episode_lots(context: ExitContext) -> tuple[PositionLot, ...]:
    return tuple(lot for lot in context.lots
                 if lot.episode_id == context.episode_id
                 and lot.contract_id == context.contract_id
                 and lot.strategy_id == context.strategy_id)


def _episode_fills(context: ExitContext) -> tuple[Fill, ...]:
    return tuple(fill for fill in context.episode_fills
                 if fill.episode_id == context.episode_id
                 and fill.contract_id == context.contract_id
                 and fill.strategy_id == context.strategy_id)


def episode_cashflows(fills: tuple[Fill, ...], side: int,
                      multiplier: Decimal) -> tuple[Decimal, Decimal, int, Decimal | None]:
    """(RGt, Ft, closed lots, remaining open lots) from verifiable fills.

    Closed-batch gross uses FIFO matching of close fills against open fills
    (08 §6.2: closed-batch cashflows must come from the episode fill
    ledger, not from account-level realized P&L allocation). Fees are the
    exact paid ``fee_cny`` amounts; actual fill slippage is already inside
    ``execution_price`` and is never deducted a second time.
    """
    queue: list[list[int | Decimal]] = []
    gross = Decimal("0")
    fees = Decimal("0")
    closed = 0
    opened = 0
    for fill in sorted(fills, key=lambda item: (item.execution_nano, item.fill_id)):
        fees += fill.fee_cny
        if fill.offset == "OPEN":
            queue.append([fill.lots, fill.execution_price])
            opened += fill.lots
            continue
        closed += fill.lots
        remaining = fill.lots
        while remaining > 0 and queue:
            head_lots, head_price = int(queue[0][0]), queue[0][1]
            take = min(head_lots, remaining)
            gross += side * take * (fill.execution_price - head_price) * multiplier
            remaining -= take
            if take == head_lots:
                queue.pop(0)
            else:
                queue[0][0] = head_lots - take
    remaining_open = sum(int(entry[0]) for entry in queue)
    weighted: Decimal | None = None
    if remaining_open:
        weighted = (sum(Decimal(int(entry[0])) * entry[1] for entry in queue)
                    / Decimal(remaining_open))
    return gross, fees, closed, weighted


def _visible_rule(context: ExitContext) -> TradingRule | None:
    now = context.now_nano
    candidates = [rule for rule in context.rules
                  if rule.contract_id == context.contract_id
                  and rule.effective_from_nano <= now < rule.effective_until_nano
                  and rule.available_at_nano <= now]
    if not candidates:
        return None
    return max(candidates, key=lambda rule: (rule.effective_from_nano, rule.rule_id))


def _current_mark(context: ExitContext) -> ValuationMark | None:
    now = context.now_nano
    candidates = [mark for mark in context.marks
                  if mark.contract_id == context.contract_id
                  and mark.as_of_nano is not None and mark.as_of_nano <= now]
    if candidates:
        return max(candidates, key=lambda mark: (mark.as_of_nano,
                                                 mark.valued_at_nano or 0))
    # An UNVALUED mark carries no timing facts at all (M1 contract); it must
    # still surface as MARK_UNVALUED instead of degrading to MARK_MISSING.
    unvalued = [mark for mark in context.marks
                if mark.contract_id == context.contract_id
                and mark.status == "UNVALUED"]
    return unvalued[-1] if unvalued else None


def _legal_sessions(context: ExitContext) -> tuple[SessionWindow, ...]:
    return tuple(session for session in context.sessions
                 if session.evidence_status != "UNKNOWN")


def _overlap_nano(sessions: tuple[SessionWindow, ...], start: int, end: int) -> int:
    if end <= start:
        return 0
    total = 0
    for session in sessions:
        total += max(0, min(session.end_nano, end) - max(session.start_nano, start))
    return total


def _trading_minute_steps(sessions: tuple[SessionWindow, ...], start: int,
                          end: int) -> Decimal:
    return Decimal(_overlap_nano(sessions, start, end)) / Decimal(MINUTE_NANO)


def _completed_trading_days(sessions: tuple[SessionWindow, ...], start: int,
                            end: int) -> int:
    """Distinct trading days whose latest session end lies in (start, end]."""
    if end <= start:
        return 0
    ends: dict[str, int] = {}
    for session in sessions:
        marker = session.trading_day.isoformat()
        ends[marker] = max(ends.get(marker, 0), session.end_nano)
    return sum(1 for end_nano in ends.values() if start < end_nano <= end)


def collect_exit_facts(context: ExitContext,
                       previous: ExitState | None) -> ExitObservationFacts:
    """Compute every fact once; policies and the snapshot share this."""
    durations = {spec.observation_duration_seconds for spec in context.exit_specs}
    if len(durations) != 1:
        raise ExitFactsError("bound_exit_specs_must_share_one_observation_duration")
    duration = durations.pop()
    now = context.now_nano
    budget: RoundBudget = context.budget
    side = budget.side
    a0 = budget.initial_atr_points
    r0 = budget.planned_loss_cny

    fills = _episode_fills(context)
    open_fills = tuple(fill for fill in fills if fill.offset == "OPEN")
    first_fill = previous.first_fill_nano if previous is not None else (
        min((fill.execution_nano for fill in open_fills), default=None))

    rule = _visible_rule(context)
    mark = _current_mark(context)
    mark_current = (mark is not None and mark.status == "CURRENT"
                    and mark.price is not None)
    valuation_reasons: tuple[str, ...] = ()
    if mark is None:
        valuation_reasons = ("MARK_MISSING",)
    elif mark.status == "UNVALUED":
        valuation_reasons = ("MARK_UNVALUED", "NLI_UNVALUED")
    elif mark.status == "STALE":
        valuation_reasons = ("MARK_STALE",)
    elif mark.price is None:
        valuation_reasons = ("MARK_PRICE_MISSING",)

    lots = _episode_lots(context)
    remaining_lots = sum(lot.lots for lot in lots)
    weighted_entry = None
    if remaining_lots:
        weighted_entry = (sum(Decimal(lot.lots) * lot.entry_price for lot in lots)
                          / Decimal(remaining_lots))

    cost_reasons: tuple[str, ...] = ()
    if rule is None:
        cost_reasons = ("RULE_MISSING",)
    else:
        today = sum(lot.lots for lot in lots
                    if lot.opened_trading_day == context.account.trading_day)
        old = remaining_lots - today
        if old > 0 and rule.close_fee is None:
            cost_reasons = ("CLOSE_FEE_UNKNOWN",)
        if today > 0 and rule.close_today_fee is None:
            cost_reasons = ("CLOSE_TODAY_FEE_UNKNOWN",)

    multiplier = rule.multiplier if rule is not None else None
    realized_gross = Decimal("0")
    fees_paid = Decimal("0")
    if fills and multiplier is not None:
        realized_gross, fees_paid, _, _ = episode_cashflows(fills, side, multiplier)

    price = mark.price if (mark is not None and mark.price is not None) else None

    unrealized: Decimal | None = None
    if mark_current and price is not None and remaining_lots and weighted_entry is not None:
        if multiplier is not None:
            unrealized = side * Decimal(remaining_lots) * (price - weighted_entry) * multiplier

    exit_cost: Decimal | None = None
    if (rule is not None and not cost_reasons and price is not None
            and mark_current and multiplier is not None):
        today = sum(lot.lots for lot in lots
                    if lot.opened_trading_day == context.account.trading_day)
        old = remaining_lots - today
        exit_cost = Decimal("0")
        for leg_lots, leg in ((old, rule.close_fee), (today, rule.close_today_fee)):
            if leg_lots:
                exit_cost += fee_amount(leg, price, leg_lots, multiplier)
        exit_cost += (Decimal(remaining_lots)
                      * (rule.slippage_ticks + rule.impact_ticks)
                      * rule.price_tick * multiplier)

    net_liquidation: Decimal | None = None
    nli_reasons: tuple[str, ...] = ()
    if unrealized is not None and exit_cost is not None:
        net_liquidation = realized_gross - fees_paid + unrealized - exit_cost
    elif mark is not None and mark.status == "UNVALUED":
        nli_reasons = ("MARK_UNVALUED",)
    elif mark is not None and mark.status == "STALE":
        nli_reasons = ("MARK_STALE",)
    elif mark is None:
        nli_reasons = ("MARK_MISSING",)
    else:
        nli_reasons = cost_reasons or ("VALUATION_INCOMPLETE",)

    net_liquidation_r: Decimal | None = None
    if net_liquidation is not None:
        if r0 > 0:
            net_liquidation_r = net_liquidation / r0
        else:
            nli_reasons = ("PLANNED_LOSS_NON_POSITIVE",)

    # peaks: actual-price space; forward-only; gaps carry + DEGRADED marker
    previous_price_peak = previous.price_peak if previous is not None else None
    previous_net_peak = previous.net_peak_r if previous is not None else None
    price_peak: Decimal | None = None
    price_peak_carried = False
    price_peak_reasons: tuple[str, ...] = ()
    if first_fill is not None and mark_current and price is not None \
            and mark.as_of_nano is not None and mark.as_of_nano >= first_fill:
        if previous_price_peak is not None:
            better = max(previous_price_peak, price) if side == 1 \
                else min(previous_price_peak, price)
            price_peak, price_peak_carried = better, better == previous_price_peak
        else:
            price_peak = price
    elif previous_price_peak is not None:
        price_peak, price_peak_carried = previous_price_peak, True
        price_peak_reasons = ("OBSERVATION_GAP",)
    elif first_fill is None:
        price_peak_reasons = ("NO_REAL_FILL",)

    net_peak_r: Decimal | None = None
    net_peak_carried = False
    net_peak_reasons: tuple[str, ...] = ()
    if net_liquidation_r is not None:
        candidate = max(Decimal("0"), net_liquidation_r)
        if previous_net_peak is not None:
            net_peak_r = max(previous_net_peak, candidate)
            net_peak_carried = previous_net_peak >= candidate
        else:
            net_peak_r = candidate
    elif previous_net_peak is not None:
        net_peak_r, net_peak_carried = previous_net_peak, True
        net_peak_reasons = ("OBSERVATION_GAP",)
    else:
        net_peak_reasons = nli_reasons or ("NO_QUALIFIED_NET_OBSERVATION",)

    giveback_r: Decimal | None = None
    if net_peak_r is not None and net_liquidation_r is not None:
        giveback_r = net_peak_r - net_liquidation_r

    effective_stop = (previous.protection_stop_price if previous is not None
                      and previous.armed and previous.protection_stop_price is not None
                      else budget.stop_price)

    # trading clocks (08 §6.2): lunch excluded; missing bars never pause
    sessions = _legal_sessions(context)
    age_reasons: tuple[str, ...] = ()
    remaining_reasons: tuple[str, ...] = ()
    elapsed_reasons: tuple[str, ...] = ()
    age_steps: Decimal | int | None = None
    remaining_steps: Decimal | int | None = None
    elapsed: Decimal | None = None
    if first_fill is None:
        age_reasons = ("NO_REAL_FILL",)
    elif not sessions:
        age_reasons = ("CALENDAR_UNKNOWN",)
    else:
        if duration == 60:
            age_steps = _trading_minute_steps(sessions, first_fill, now)
        else:
            age_steps = _completed_trading_days(sessions, first_fill, now)
        deadlines = [value for value in (context.entry_state.active_exit_nano,
                                         budget.latest_exit_nano)
                     if value is not None]
        deadline = min(deadlines) if deadlines else None
        if deadline is None:
            remaining_reasons = ("DEADLINE_UNKNOWN",)
        elif deadline <= now:
            remaining_steps = 0
        elif duration == 60:
            remaining_steps = _trading_minute_steps(sessions, now, deadline)
        else:
            remaining_steps = _completed_trading_days(sessions, now, deadline)
        if remaining_steps is not None and age_steps is not None:
            age_value = Decimal(age_steps)
            if age_value == 0 and Decimal(remaining_steps) == 0:
                elapsed_reasons = ("NO_VALID_HOLDING_WINDOW",)
            elif Decimal(remaining_steps) == 0:
                elapsed = Decimal("1")
            else:
                elapsed = age_value / (age_value + Decimal(remaining_steps))
        elif remaining_reasons:
            elapsed_reasons = remaining_reasons
    if not elapsed_reasons and elapsed is None and first_fill is not None:
        elapsed_reasons = age_reasons or remaining_reasons

    account = context.account
    account_drawdown: Decimal | None = None
    account_reasons: tuple[str, ...] = ()
    if account.quality.status != "READY":
        account_reasons = ("ACCOUNT_QUALITY_NOT_READY",)
    elif account.peak_equity_cny <= 0:
        account_drawdown = None
        account_reasons = ("ACCOUNT_PEAK_NON_POSITIVE",)
    else:
        account_drawdown = ((account.peak_equity_cny - account.equity_cny)
                            / account.peak_equity_cny)

    closed_fraction: Decimal | None = None
    closed_reasons: tuple[str, ...] = ()
    if previous is None or previous.qbase is None:
        closed_reasons = ("QBASE_NOT_FROZEN",)
    else:
        closed_fraction = Decimal(previous.closed_lots) / Decimal(previous.qbase)

    return ExitObservationFacts(
        duration_seconds=duration, now_nano=now, side=side, a0=a0, r0=r0,
        first_fill_nano=first_fill,
        mark=mark, mark_current=mark_current,
        valuation_reasons=valuation_reasons,
        rule=rule, cost_reasons=cost_reasons,
        remaining_lots=remaining_lots, weighted_entry=weighted_entry,
        realized_gross=realized_gross, fees_paid=fees_paid,
        unrealized=unrealized, exit_cost=exit_cost,
        net_liquidation=net_liquidation, net_liquidation_r=net_liquidation_r,
        nli_reasons=nli_reasons,
        price_peak=price_peak, price_peak_carried=price_peak_carried,
        price_peak_reasons=price_peak_reasons,
        net_peak_r=net_peak_r, net_peak_carried=net_peak_carried,
        net_peak_reasons=net_peak_reasons, giveback_r=giveback_r,
        effective_stop_price=effective_stop,
        age_steps=age_steps, age_reasons=age_reasons,
        remaining_steps=remaining_steps, remaining_reasons=remaining_reasons,
        elapsed_fraction=elapsed, elapsed_reasons=elapsed_reasons,
        deadline_nano=(min([value for value in
                            (context.entry_state.active_exit_nano,
                             budget.latest_exit_nano) if value is not None],
                           default=None)),
        account_drawdown=account_drawdown, account_reasons=account_reasons,
        closed_fraction=closed_fraction, closed_reasons=closed_reasons)


# ─── snapshot assembly ──────────────────────────────────────────────────────

def _quality(status: str, reasons: tuple[str, ...], basis: str,
             payload: object) -> DataQuality:
    return DataQuality(status=status, reasons=reasons,  # type: ignore[arg-type]
                       temporal_basis=basis,  # type: ignore[arg-type]
                       input_hash=exit_digest(payload))


def _value(key: str, value: Decimal | int | None, status: str,
           reasons: tuple[str, ...], basis: str, now: int,
           sources: Mapping[str, str], payload: object) -> ExitFeatureValue:
    return ExitFeatureValue(
        feature_key=key, value=value, observed_at_nano=now,
        available_at_nano=now,
        quality=_quality(status, reasons, basis, [key, payload]),
        source_hashes=dict(sources))


def _ready(key: str, value: Decimal | int | None, now: int,
           sources: Mapping[str, str]) -> ExitFeatureValue:
    if value is None:
        raise ExitFactsError(f"ready_value_must_not_be_none:{key}")
    return _value(key, value, "READY", (), "observed", now, sources, value)


def _blocked(key: str, status: str, reasons: tuple[str, ...], now: int,
             sources: Mapping[str, str]) -> ExitFeatureValue:
    return _value(key, None, status, reasons, "unknown", now, sources, reasons)


def build_exit_feature_snapshot(context: ExitContext,
                                previous: ExitState | None) -> ExitFeatureSnapshot:
    """Build the mode-scoped EXITSTATE snapshot (sorted, unique, visible)."""
    facts = collect_exit_facts(context, previous)
    duration = facts.duration_seconds
    now = facts.now_nano
    budget = context.budget
    mark_source = {"round_budget": exit_digest(["budget", budget.budget_id, str(facts.a0)]),
                   "episode_fills": exit_digest([facts.realized_gross, facts.fees_paid]),
                   "valuation_mark": (exit_digest(["mark", facts.mark.status])
                                      if facts.mark is not None
                                      else exit_digest(["mark", "MISSING"])),
                   "trading_rule": (exit_digest(["rule", facts.rule.rule_id])
                                    if facts.rule is not None
                                    else exit_digest(["rule", "MISSING"])),
                   "session_calendar": exit_digest(
                       ["sessions", [(s.session_id, s.start_nano, s.end_nano)
                                     for s in _legal_sessions(context)]])}
    price = facts.mark.price if facts.mark is not None else None

    values: list[ExitFeatureValue] = []

    def add(name: str, builder) -> None:
        values.append(builder(_key(name, duration)))

    # unrealized pair
    if facts.unrealized is not None:
        add("unrealized_pnl_cny",
            lambda key: _ready(key, facts.unrealized, now, mark_source))
        ratio = facts.unrealized / facts.r0 if facts.r0 > 0 else None
        if ratio is not None:
            add("unrealized_pnl_r",
                lambda key: _ready(key, ratio, now, mark_source))
        else:
            add("unrealized_pnl_r",
                lambda key: _blocked(key, "INVALID", ("PLANNED_LOSS_NON_POSITIVE",),
                                     now, mark_source))
    else:
        status = "STALE" if facts.valuation_reasons == ("MARK_STALE",) else "MISSING"
        add("unrealized_pnl_cny",
            lambda key: _blocked(key, status, facts.valuation_reasons or
                                 ("VALUATION_INCOMPLETE",), now, mark_source))
        add("unrealized_pnl_r",
            lambda key: _blocked(key, status, facts.valuation_reasons or
                                 ("VALUATION_INCOMPLETE",), now, mark_source))

    # entry distance / trailing distance / effective stop distance
    if (facts.mark_current and price is not None and facts.weighted_entry is not None
            and facts.remaining_lots):
        if facts.a0 > 0:
            entry_distance = facts.side * (price - facts.weighted_entry) / facts.a0
            add("entry_distance_atr",
                lambda key: _ready(key, entry_distance, now, mark_source))
            if facts.price_peak is not None:
                trail = facts.side * (facts.price_peak - price) / facts.a0
                add("trailing_atr_distance",
                    lambda key: _ready(key, trail, now, mark_source))
            else:
                add("trailing_atr_distance",
                    lambda key: _blocked(key, "WARMUP", ("NO_QUALIFIED_PEAK",),
                                         now, mark_source))
            stop_distance = facts.side * (price - facts.effective_stop_price) / facts.a0
            add("effective_stop_distance_atr",
                lambda key: _ready(key, stop_distance, now, mark_source))
        else:
            for name in ("entry_distance_atr", "trailing_atr_distance",
                         "effective_stop_distance_atr"):
                add(name, lambda key: _blocked(
                    key, "INVALID", ("INITIAL_ATR_NON_POSITIVE",), now, mark_source))
    else:
        reasons = facts.valuation_reasons or (("NO_REMAINING_LOTS",)
                                              if not facts.remaining_lots
                                              else ("VALUATION_INCOMPLETE",))
        status = "STALE" if reasons == ("MARK_STALE",) else "MISSING"
        for name in ("entry_distance_atr", "trailing_atr_distance",
                     "effective_stop_distance_atr"):
            add(name, lambda key: _blocked(key, status, reasons, now, mark_source))

    # net liquidation pair
    if facts.net_liquidation is not None:
        add("episode_net_liquidation_cny",
            lambda key: _ready(key, facts.net_liquidation, now, mark_source))
        if facts.net_liquidation_r is not None:
            add("episode_net_liquidation_r",
                lambda key: _ready(key, facts.net_liquidation_r, now, mark_source))
        else:
            add("episode_net_liquidation_r",
                lambda key: _blocked(key, "INVALID", facts.nli_reasons,
                                     now, mark_source))
    else:
        status = "STALE" if facts.nli_reasons[:1] == ("MARK_STALE",) else "MISSING"
        add("episode_net_liquidation_cny",
            lambda key: _blocked(key, status, facts.nli_reasons, now, mark_source))
        add("episode_net_liquidation_r",
            lambda key: _blocked(key, status, facts.nli_reasons, now, mark_source))

    # peaks (carry + DEGRADED gap semantics)
    if facts.price_peak is not None and not facts.price_peak_reasons:
        add("peak_favorable_price",
            lambda key: _value(key, facts.price_peak, "READY", (),
                               "observed" if not facts.price_peak_carried
                               else "reconstructed",
                               now, mark_source, facts.price_peak))
    elif facts.price_peak is not None:
        add("peak_favorable_price",
            lambda key: _value(key, facts.price_peak, "DEGRADED",
                               facts.price_peak_reasons + ("CARRIED_FROM_COMMITTED_STATE",),
                               "reconstructed", now, mark_source, facts.price_peak))
    else:
        add("peak_favorable_price",
            lambda key: _blocked(key, "WARMUP", facts.price_peak_reasons or
                                 ("NO_QUALIFIED_PEAK",), now, mark_source))

    if facts.net_peak_r is not None and not facts.net_peak_reasons:
        add("peak_net_liquidation_r",
            lambda key: _value(key, facts.net_peak_r, "READY", (),
                               "observed" if not facts.net_peak_carried
                               else "reconstructed",
                               now, mark_source, facts.net_peak_r))
    elif facts.net_peak_r is not None:
        add("peak_net_liquidation_r",
            lambda key: _value(key, facts.net_peak_r, "DEGRADED",
                               facts.net_peak_reasons + ("CARRIED_FROM_COMMITTED_STATE",),
                               "reconstructed", now, mark_source, facts.net_peak_r))
    else:
        add("peak_net_liquidation_r",
            lambda key: _blocked(key, "WARMUP", facts.net_peak_reasons,
                                 now, mark_source))

    if facts.giveback_r is not None:
        add("profit_giveback_r",
            lambda key: _ready(key, facts.giveback_r, now, mark_source))
    else:
        add("profit_giveback_r",
            lambda key: _blocked(key, "MISSING", facts.nli_reasons or
                                 ("PEAK_NOT_ESTABLISHED",), now, mark_source))

    # clocks
    if duration == 60:
        if facts.age_steps is not None:
            add("age_trading_minutes",
                lambda key: _ready(key, facts.age_steps, now, mark_source))
        else:
            add("age_trading_minutes",
                lambda key: _blocked(key, "MISSING", facts.age_reasons,
                                     now, mark_source))
    else:
        if facts.age_steps is not None:
            add("age_trading_days",
                lambda key: _ready(key, facts.age_steps, now, mark_source))
        else:
            add("age_trading_days",
                lambda key: _blocked(key, "MISSING", facts.age_reasons,
                                     now, mark_source))

    if facts.remaining_steps is not None:
        add("remaining_steps",
            lambda key: _ready(key, facts.remaining_steps, now, mark_source))
    else:
        add("remaining_steps",
            lambda key: _blocked(key, "MISSING", facts.remaining_reasons,
                                 now, mark_source))

    if facts.elapsed_fraction is not None:
        add("elapsed_fraction",
            lambda key: _ready(key, facts.elapsed_fraction, now, mark_source))
    else:
        add("elapsed_fraction",
            lambda key: _blocked(key, "MISSING", facts.elapsed_reasons,
                                 now, mark_source))

    if facts.account_drawdown is not None:
        add("account_drawdown_ratio",
            lambda key: _ready(key, facts.account_drawdown, now, mark_source))
    else:
        add("account_drawdown_ratio",
            lambda key: _blocked(key, "MISSING", facts.account_reasons,
                                 now, mark_source))

    if facts.closed_fraction is not None:
        add("closed_fraction",
            lambda key: _ready(key, facts.closed_fraction, now, mark_source))
    else:
        add("closed_fraction",
            lambda key: _blocked(key, "WARMUP", facts.closed_reasons,
                                 now, mark_source))

    ordered = tuple(sorted(values, key=lambda value: value.feature_key))
    keys = [value.feature_key for value in ordered]
    if keys != sorted(set(keys)):
        raise ExitFactsError("snapshot_values_must_be_sorted_unique")

    snapshot_core = [context.account_id, context.contract_id,
                     context.strategy_id, context.episode_id,
                     context.exit_binding.binding_sha256, duration, now,
                     [(value.feature_key,
                       str(value.value) if isinstance(value.value, Decimal)
                       else value.value,
                       value.quality.status) for value in ordered]]
    source_state = exit_digest([
        previous.state_prefix_hash if previous is not None else None,
        context.account_id, context.contract_id, context.strategy_id,
        context.episode_id, context.exit_binding.binding_sha256, now])
    return ExitFeatureSnapshot(
        snapshot_id=exit_digest(["exit-snapshot", snapshot_core]),
        account_id=context.account_id, contract_id=context.contract_id,
        strategy_id=context.strategy_id, episode_id=context.episode_id,
        exit_binding_sha256=context.exit_binding.binding_sha256,
        duration_seconds=duration, observed_at_nano=now,
        available_at_nano=now, values=ordered, source_state_sha256=source_state)


def next_window_start(sessions: tuple[SessionWindow, ...], now_nano: int,
                      duration_seconds: int) -> int:
    """Earliest legal execution window strictly after ``now_nano``.

    Inside a session the next same-mode observation boundary; outside any
    session the next session start. Pure calendar arithmetic — no order is
    created here (08 §5.3 "最早下一合法窗口").
    """
    step = duration_seconds * 1_000_000_000
    in_session = any(session.start_nano <= now_nano < session.end_nano
                     for session in sessions)
    if in_session:
        boundary = (now_nano // step + 1) * step
        covered = [session for session in sessions
                   if session.start_nano <= boundary < session.end_nano]
        if covered or not sessions:
            return boundary
    starts = [session.start_nano for session in sessions
              if session.start_nano > now_nano]
    if starts:
        return min(starts)
    return now_nano + step


# ─── P2 / D2: direct M2 FeatureSnapshot market-key channel ─────────────────
#
# D2 ruling (m3c-p2 open-questions-resolution §2): VOLATILITY_STOP consumes
# the exact BASE keys straight from M2 FeatureSnapshot originals carried by
# ExitContext.market_features. BASE values are NEVER wrapped as EXITSTATE
# keys and never enter ExitFeatureSnapshot.values. Until M1 lands the type
# refreeze (market_features: tuple[FeatureSnapshot, ...]), the resolver
# reports the channel as unavailable and callers degrade to KEEP — the M4
# hard-risk path keeps running either way.

from ak_trading.v2.contracts import FeatureSnapshot, FactorValue  # noqa: E402


class MarketChannelUnavailable(Exception):
    """Raised by resolve_market_snapshot when no legal M2 original exists.

    The message tuple carries the precise D2 reasons (channel pending M1,
    no visible snapshot for the mode/contract, duplicate originals, ...).
    """

    def __init__(self, reasons: tuple[str, ...]) -> None:
        super().__init__(";".join(reasons))
        self.reasons = reasons


@dataclass(frozen=True)
class MarketKeyFacts:
    """The selected M2 snapshot plus per-required-key field status (D2).

    ``values`` holds exactly the requested keys that passed field-level
    validation (finite positive Decimal, READY, visible at the decision
    time); ``unavailable_keys`` lists requested keys that did not, with
    ``reasons`` explaining each. The observation slot end (OQ-6 dedup) is
    the snapshot's decision_nano — repeated evaluations of the same
    completed observation share one slot and never inflate counters.
    """
    snapshot: FeatureSnapshot
    slot_end_nano: int
    values: Mapping[str, FactorValue]
    unavailable_keys: tuple[str, ...]
    reasons: tuple[str, ...]


def _is_feature_snapshot(item: object) -> bool:
    return isinstance(item, FeatureSnapshot)


def resolve_market_snapshot(market_features: tuple, contract_id: str,
                            duration_seconds: int,
                            required_keys: tuple[str, ...],
                            now_nano: int) -> MarketKeyFacts:
    """Whole-snapshot selection, then exact-key extraction (D2 §selection).

    1. Only M2 FeatureSnapshot originals count; anything else in the tuple
       means the D2 channel is not live yet (or was bypassed) — refuse.
    2. Candidates: same contract, same mode duration, anchored inside the
       visible prefix (decision_nano <= now, available_at_nano <= now).
       The latest decision_nano wins; two different originals at the same
       anchor or an exact duplicate are rejected, never silently resolved.
    3. Per required key: exactly one FactorValue, finite positive Decimal,
       READY field quality, available_at_nano <= now. Aggregate snapshot
       quality never launders a bad field (and an unrelated factor's
       failure never vetoes two legal inputs).
    """
    if not all(_is_feature_snapshot(item) for item in market_features):
        non_conforming = sum(
            1 for item in market_features if not _is_feature_snapshot(item))
        if non_conforming:
            raise MarketChannelUnavailable((
                "MARKET_CHANNEL_PENDING_M1_D2_TYPE",))
    snapshots = [item for item in market_features
                 if isinstance(item, FeatureSnapshot)
                 and item.contract_id == contract_id
                 and item.duration_seconds == duration_seconds
                 and item.decision_nano <= now_nano
                 and item.available_at_nano <= now_nano]
    if not snapshots:
        raise MarketChannelUnavailable(("MARKET_SNAPSHOT_MISSING",))
    anchor = max(item.decision_nano for item in snapshots)
    at_anchor = [item for item in snapshots if item.decision_nano == anchor]
    identities = {item.snapshot_id for item in at_anchor}
    if len(at_anchor) > 1 and len(identities) > 1:
        raise MarketChannelUnavailable(("MARKET_SNAPSHOT_AMBIGUOUS",))
    if len(at_anchor) > 1 and len(identities) == 1:
        raise MarketChannelUnavailable(("MARKET_SNAPSHOT_DUPLICATED",))
    selected = at_anchor[0]

    values: dict[str, FactorValue] = {}
    unavailable: list[str] = []
    reasons: list[str] = []
    for key in required_keys:
        matches = [item for item in selected.values if item.factor_key == key]
        if not matches:
            unavailable.append(key)
            reasons.append(f"MARKET_KEY_MISSING:{key.split(':')[1]}")
            continue
        if len(matches) > 1:
            unavailable.append(key)
            reasons.append(f"MARKET_KEY_DUPLICATED:{key.split(':')[1]}")
            continue
        item = matches[0]
        if item.available_at_nano is None or item.available_at_nano > now_nano:
            unavailable.append(key)
            reasons.append(f"MARKET_KEY_NOT_VISIBLE:{key.split(':')[1]}")
            continue
        if item.quality.status != "READY":
            unavailable.append(key)
            reasons.append(
                f"MARKET_KEY_NOT_READY:{key.split(':')[1]}:{item.quality.status}")
            continue
        if not isinstance(item.value, Decimal) \
                or not item.value.is_finite() or item.value <= 0:
            unavailable.append(key)
            reasons.append(f"MARKET_VALUE_INVALID:{key.split(':')[1]}")
            continue
        values[key] = item
    return MarketKeyFacts(
        snapshot=selected, slot_end_nano=selected.decision_nano,
        values=values, unavailable_keys=tuple(unavailable),
        reasons=tuple(reasons))
