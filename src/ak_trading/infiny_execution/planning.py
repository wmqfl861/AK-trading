"""Pure limit-order planning; no sender, journal, clock reads or native imports.

Reservation views must be assembled by the trusted ledger. These functions do
not authenticate payloads and their results never grant native capabilities.
"""
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from decimal import Decimal, localcontext
import hashlib
from math import lcm

from . import contracts as c
from . import native_contracts as nc
from . import native_evidence as ne


_ZERO = Decimal("0")
_SPLIT_EXCHANGES = frozenset({"SHFE", "INE"})


def _typed(value, expected, name):
    if type(value) is not expected:
        raise c.ContractError(f"{name}: expected {expected.__name__}")


def _now(value):
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise c.ContractError("now: expected aware datetime")
    return value.astimezone(timezone.utc)


def _decimal_text(value):
    if value == 0:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _plan_hash(intent_hash, snapshot_id, revision, policy_id, expires_at, legs):
    rows = ";".join(
        f"{leg.leg_id},{leg.phase.value},{leg.direction.value},{leg.offset.value},"
        f"{leg.volume},{_decimal_text(leg.price)},{leg.position_bucket.value if leg.position_bucket else '-'},"
        f"{_decimal_text(leg.cash_reserve)},{'+'.join(sorted(leg.depends_on)) or '-'}"
        for leg in legs)
    expires = expires_at.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    payload = f"infiny_execution_plan_v1|{intent_hash}|{snapshot_id}|{revision}|{policy_id}|{expires}|{rows}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _finish(intent, snapshot, policy, expires_at, legs, blockers):
    blockers = tuple(dict.fromkeys(blockers))
    legs = () if blockers else tuple(legs)
    identity = c.canonical_hash(intent)
    return c.Plan(plan_id=_plan_hash(identity, snapshot.snapshot_id, snapshot.ledger_revision, policy.policy_id, expires_at, legs),
                  intent_hash=identity, snapshot_id=snapshot.snapshot_id, ledger_revision=snapshot.ledger_revision,
                  policy_id=policy.policy_id, expires_at=expires_at, legs=legs, blockers=blockers)


def _volume_bounds(contract, policy, blockers):
    if contract is None or None in (contract.min_limit_order_volume, contract.max_limit_order_volume, policy.max_leg_qty):
        blockers.append("CONTRACT_MISSING")
        return None
    minimum = contract.min_limit_order_volume
    maximum = min(contract.max_limit_order_volume, policy.max_leg_qty)
    step = 1
    for rule in contract.additional_volume_limits:
        if not rule.verified:
            blockers.append("VOLUME_INVALID")
            return None
        minimum = max(minimum, rule.min_volume)
        maximum = min(maximum, rule.max_volume)
        step = lcm(step, rule.volume_step)
    minimum = (minimum + step - 1) // step * step
    maximum = maximum // step * step
    if minimum > maximum:
        blockers.append("VOLUME_SPLIT_IMPOSSIBLE")
        return None
    return minimum, maximum, step


def _split(total, bounds, blockers):
    minimum, maximum, step = bounds
    count = (total + maximum - 1) // maximum
    if total % step or total < count * minimum or count > c.MAX_MESSAGE_BYTES // 128:
        blockers.append("VOLUME_SPLIT_IMPOSSIBLE")
        return ()
    result = []
    for remaining in range(count - 1, -1, -1):
        quantity = min(maximum, total - remaining * minimum)
        result.append(quantity)
        total -= quantity
    return tuple(result)


def _binding(expected, actual, blockers):
    for names, code in ((('account_key',), 'ACCOUNT_MISMATCH'),
                        (('strategy_key', 'strategy_id'), 'OWNERSHIP_CONFLICT'),
                        (('instance_id', 'session_epoch'), 'SESSION_CHANGED'),
                        (('trading_day',), 'TRADING_DAY_CHANGED'),
                        (('exchange', 'instrument_id'), 'INSTRUMENT_MISMATCH'),
                        (('hedge_flag',), 'HEDGE_UNVERIFIED')):
        if any(getattr(expected, name) != getattr(actual, name) for name in names):
            blockers.append(code)


def _deadline(stamp, ttl):
    try:
        return stamp + timedelta(microseconds=int(ttl * 1000000))
    except OverflowError:
        return datetime.max.replace(tzinfo=timezone.utc)


def _freshness(snapshot, policy, now, blockers):
    deadlines = []
    for label, item, ttl in (("QUOTE", snapshot.times.quote, policy.quote_ttl),
                             ("ACCOUNT", snapshot.times.account, policy.account_ttl),
                             ("POSITION", snapshot.times.positions, policy.position_ttl),
                             ("CONTRACT", snapshot.times.contract, policy.contract_ttl),
                             *(("POSITION", p.timing, policy.position_ttl) for p in snapshot.positions)):
        result = c.validate_timing(item, now, ttl, require_source=label == "QUOTE", policy=policy)
        if not result.ok:
            blockers.append("QUOTE_STALE" if label == "QUOTE" else "SNAPSHOT_STALE")
        if item.completeness is not c.Completeness.COMPLETE or item.snapshot_id is None:
            blockers.append("QUOTE_MISSING" if label == "QUOTE" else "BASELINE_GAP")
        for stamp in (item.sampled_at, item.received_at, item.source_at):
            if stamp is not None:
                deadlines.append(_deadline(stamp, ttl))
    packet = c.ComponentTiming(sampled_at=snapshot.times.sampled_at, received_at=snapshot.times.received_at,
                               source_at=None, snapshot_id=snapshot.snapshot_id, source=snapshot.source,
                               completeness=c.Completeness.COMPLETE)
    if not c.validate_timing(packet, now, policy.packet_ttl, policy=policy).ok:
        blockers.append("SNAPSHOT_STALE")
    for stamp in (snapshot.times.sampled_at, snapshot.times.sent_at, snapshot.times.received_at):
        if stamp is None:
            blockers.append("SNAPSHOT_STALE")
        else:
            if stamp > _deadline(now, policy.future_skew) or now > _deadline(stamp, policy.packet_ttl):
                blockers.append("SNAPSHOT_STALE")
            deadlines.append(_deadline(stamp, policy.packet_ttl))
    return deadlines


def _contract(snapshot, now, blockers):
    contract = snapshot.contract
    if contract is None:
        blockers.append("CONTRACT_MISSING")
        return
    if any(getattr(contract, field) is None for field in (
            "exchange", "instrument_id", "expiry", "price_tick", "multiplier",
            "min_limit_order_volume", "max_limit_order_volume")):
        blockers.append("CONTRACT_MISSING")
    if contract.asset_class is not c.AssetClass.FUTURE:
        blockers.append("INSTRUMENT_UNSUPPORTED")
    if contract.expiry is not None and contract.expiry <= max(snapshot.binding.trading_day, now.date()):
        blockers.append("CONTRACT_EXPIRED")


def _market(snapshot, prices, directions, blockers):
    quote, contract = snapshot.quote, snapshot.contract
    if quote is None:
        blockers.append("QUOTE_MISSING")
        return
    status = quote.market_status
    if status is not c.MarketStatus.CONTINUOUS:
        blockers.append("AUCTION" if status is c.MarketStatus.AUCTION else
                        "MARKET_CLOSED" if status is c.MarketStatus.CLOSED else "MARKET_STATUS_UNKNOWN")
    if None in (quote.last, quote.lower_limit, quote.upper_limit):
        blockers.append("QUOTE_MISSING")
        return
    if quote.lower_limit >= quote.upper_limit:
        blockers.append("PRICE_OUT_OF_BOUNDS")
        return
    for price in (*prices, quote.last, quote.bid1, quote.ask1):
        if price is None:
            continue
        if not quote.lower_limit <= price <= quote.upper_limit:
            blockers.append("PRICE_OUT_OF_BOUNDS")
        if contract is not None and contract.price_tick is not None:
            # The wire permits 64 digits and exponents +/-128. Do not let the
            # caller's ambient Decimal precision round a tick remainder.
            with localcontext() as decimal_context:
                decimal_context.prec = 512
                if price % contract.price_tick:
                    blockers.append("TICK_MALIGNED")
    if any(price in (quote.lower_limit, quote.upper_limit) for price in (quote.last, quote.bid1, quote.ask1)):
        blockers.append("LIMIT_LOCKED")
    if quote.bid1 is None or quote.ask1 is None:
        blockers.append("BOOK_EMPTY")
        return
    if quote.bid1 > quote.ask1:
        blockers.append("BOOK_CROSSED")
    if contract is not None and contract.price_tick is not None:
        with localcontext() as decimal_context:
            decimal_context.prec = 512
            if quote.ask1 - quote.bid1 > 10 * contract.price_tick:
                blockers.append("SPREAD_TOO_WIDE")
    for direction in directions:
        quantity = quote.ask_volume1 if direction is c.Direction.BUY else quote.bid_volume1
        if quantity is None or quantity == 0:
            blockers.append("BOOK_EMPTY")


def _positions(snapshot, blockers):
    positions = {p.side: p for p in snapshot.positions}
    if set(positions) != {c.Side.LONG, c.Side.SHORT}:
        blockers.append("MISSING_POSITION_BUCKET")
    for position in positions.values():
        if any(getattr(position, field) is None for field in (
                "Position", "PositionClose", "FrozenClosing", "YdPositionClose", "YdFrozenClosing")):
            blockers.append("POSITION_DATA_INVALID")
    return positions


def _action(action):
    direction = c.Direction.BUY if action in (c.Action.OPEN_LONG, c.Action.CLOSE_SHORT, c.Action.REVERSE_TO_LONG) else c.Direction.SELL
    opening = action not in (c.Action.CLOSE_LONG, c.Action.CLOSE_SHORT)
    closing = action not in (c.Action.OPEN_LONG, c.Action.OPEN_SHORT)
    side = c.Side.SHORT if direction is c.Direction.BUY else c.Side.LONG
    return direction, opening, closing, side


def _resource_scope(snapshot, resources, blockers):
    if resources.account_key != snapshot.binding.account_key:
        blockers.append("ACCOUNT_MISMATCH")
    if resources.snapshot_id != snapshot.snapshot_id:
        blockers.append("SNAPSHOT_STALE")
    if resources.ledger_revision != snapshot.ledger_revision:
        blockers.append("LEDGER_REVISION_MISMATCH")
    if resources.reconcile.required:
        blockers.extend(("RECONCILE_REQUIRED", *(reason.value for reason in resources.reconcile.reasons)))
    for reservation in resources.reservations:
        if reservation.state is not c.ReservationState.RELEASED and reservation.resource_key.account_key != resources.account_key:
            blockers.append("ACCOUNT_MISMATCH")


def _reservation_phase(reservation, blockers):
    parts = reservation.leg_id.rsplit(":", 3)
    if len(parts) != 4 or not parts[0] or not parts[3].isascii() or not parts[3].isdigit():
        blockers.append("BASELINE_GAP")
        return None
    _, phase, bucket, sequence = parts
    if str(int(sequence)) != sequence:
        blockers.append("BASELINE_GAP")
        return None
    if phase == "OPEN" and bucket == "-" and reservation.resource_key.bucket is c.Bucket.ALL:
        return c.Phase.OPEN
    if phase == "CLOSE" and bucket == reservation.resource_key.bucket.value:
        return c.Phase.CLOSE
    blockers.append("BASELINE_GAP")
    return None


def _reflected(reservation, snapshot, proof=None):
    if reservation.state is not c.ReservationState.REFLECTED or snapshot.proof_id is None:
        return False
    if reservation.reflected_evidence_id != snapshot.proof_id:
        return False
    # plan() consumes a trusted journal view. validate_plan() additionally has
    # the actual barrier; a caller-provided boolean never authenticates it.
    return proof is None or (proof.verified and reservation.leg_id in proof.covered_leg_ids)


def _unreflected(reservation, snapshot, proof=None):
    return reservation.not_proven_reflected if _reflected(reservation, snapshot, proof) else reservation.volume


def _same_position(reservation, binding, side):
    key = reservation.resource_key
    return (key.account_key, key.exchange, key.instrument_id, key.hedge_flag, key.side) == (
        binding.account_key, binding.exchange, binding.instrument_id, binding.hedge_flag, side)


def _close_available(position, snapshot, resources, blockers, exclude=(), proof=None):
    yesterday = position.YdPositionClose - position.YdFrozenClosing
    today = position.PositionClose - position.YdPositionClose - position.FrozenClosing + position.YdFrozenClosing
    deductions = {c.Bucket.TD: 0, c.Bucket.YD: 0, c.Bucket.ALL: 0}
    for reservation in resources.reservations:
        if reservation.state is c.ReservationState.RELEASED or reservation.leg_id in exclude:
            continue
        if _reservation_phase(reservation, blockers) is c.Phase.CLOSE and _same_position(reservation, position.binding, position.side):
            deductions[reservation.resource_key.bucket] += _unreflected(reservation, snapshot, proof)
    # An ALL commitment cannot prove which SHFE bucket it consumes; deduct it
    # from each strict bucket conservatively. DCE uses the combined pool once.
    if snapshot.binding.exchange not in _SPLIT_EXCHANGES:
        return max(0, today + yesterday - sum(deductions.values())), 0
    return (max(0, today - deductions[c.Bucket.TD] - deductions[c.Bucket.ALL]),
            max(0, yesterday - deductions[c.Bucket.YD] - deductions[c.Bucket.ALL]))


def _close_allocations(intent, position, snapshot, resources, blockers):
    today, yesterday = _close_available(position, snapshot, resources, blockers)
    total = position.Position if intent.action in (c.Action.REVERSE_TO_LONG, c.Action.REVERSE_TO_SHORT) else intent.volume
    if total == 0:
        blockers.append("INSUFFICIENT_POSITION")
        return ()
    if intent.binding.exchange not in _SPLIT_EXCHANGES:
        if intent.close_policy is not c.ClosePolicy.AUTO_YD_FIRST:
            blockers.append("UNSUPPORTED_CLOSE_POLICY")
            return ()
        if today + yesterday < total:
            blockers.append("INSUFFICIENT_POSITION")
            return ()
        return ((c.Bucket.ALL, c.Offset.CLOSE, total),)
    if intent.close_policy is c.ClosePolicy.TODAY_ONLY:
        allocation = ((c.Bucket.TD, c.Offset.CLOSE_TODAY, total),) if today >= total else ()
    elif intent.close_policy is c.ClosePolicy.YESTERDAY_ONLY:
        allocation = ((c.Bucket.YD, c.Offset.CLOSE_YESTERDAY, total),) if yesterday >= total else ()
    else:
        used_yesterday = min(total, yesterday)
        allocation = tuple((bucket, offset, quantity) for bucket, offset, quantity in (
            (c.Bucket.YD, c.Offset.CLOSE_YESTERDAY, used_yesterday),
            (c.Bucket.TD, c.Offset.CLOSE_TODAY, total-used_yesterday)) if quantity) if today+yesterday >= total else ()
    if not allocation:
        blockers.append("INSUFFICIENT_POSITION")
    return allocation


def _draft(intent, snapshot, resources, policy, bounds, positions, blockers):
    direction, opening, closing, side = _action(intent.action)
    allocations = ()
    if closing:
        allocations = _close_allocations(intent, positions[side], snapshot, resources, blockers)
        if opening and positions[c.Side.LONG if side is c.Side.SHORT else c.Side.SHORT].Position:
            blockers.append("POSITION_DATA_INVALID")
    if opening:
        allocations += ((None, c.Offset.OPEN, intent.volume),)
    legs = []
    for bucket, offset, total in allocations:
        phase = c.Phase.OPEN if offset is c.Offset.OPEN else c.Phase.CLOSE
        dependencies = tuple(leg.leg_id for leg in legs if leg.phase is c.Phase.CLOSE) if phase is c.Phase.OPEN else ()
        for index, quantity in enumerate(_split(total, bounds, blockers)):
            cash = policy.cash_per_open_lot if phase is c.Phase.OPEN else policy.fee_per_close_lot
            with localcontext() as decimal_context:
                decimal_context.prec = 512
                reserve = quantity * cash
            leg_id = f"{intent.intent_id}:{phase.value}:{bucket.value if bucket else '-'}:{index}"
            if len(leg_id) > 128:
                blockers.append("VOLUME_INVALID")
                continue
            legs.append(c.Leg(leg_id=leg_id, index=len(legs), phase=phase, depends_on=dependencies,
                              direction=direction, offset=offset, volume=quantity,
                              price=intent.prices.open if phase is c.Phase.OPEN else intent.prices.close,
                              position_bucket=bucket, cash_reserve=reserve))
    return legs


def _budget_fields(intent, snapshot, policy, blockers):
    _, opening, closing, _ = _action(intent.action)
    if opening:
        if policy.cash_per_open_lot is None:
            blockers.append("CASH_BOUND_MISSING")
        if policy.cash_bound_source is None or not policy.cash_bound_source.verified or policy.cash_bound_source.source is not snapshot.source:
            blockers.append("CASH_BOUND_UNVERIFIED")
    if closing and policy.fee_per_close_lot is None:
        blockers.append("CASH_BOUND_MISSING")
    if policy.max_cash_reserve is None:
        blockers.append("CASH_BOUND_MISSING")
    if policy.max_gross_position is None:
        blockers.append("GROSS_POSITION_LIMIT")
    if policy.max_inflight_legs is None:
        blockers.append("INFLIGHT_LIMIT")
    if snapshot.account is None or snapshot.account.available is None or snapshot.account.currency is None:
        blockers.append("INSUFFICIENT_CASH")


def _budget(snapshot, resources, policy, legs, positions, blockers, claimed=(), proof=None):
    active = tuple(r for r in resources.reservations if r.state is not c.ReservationState.RELEASED)
    with localcontext() as decimal_context:
        decimal_context.prec = 512
        recorded = sum((r.cash_reserve for r in active), _ZERO)
        if recorded != resources.cash_reserved:
            blockers.append("BASELINE_GAP")
        # Policy caps all commitments; SDK available already subtracts proven
        # reflected freezes. Only a covering barrier eliminates double use.
        extra = sum((leg.cash_reserve for leg in legs if leg.leg_id not in claimed), _ZERO)
        commitment = max(recorded, resources.cash_reserved) + extra
        local_cash = max(_ZERO, resources.cash_reserved - recorded) + extra
        for reservation in active:
            if not _reflected(reservation, snapshot, proof) or reservation.not_proven_reflected:
                local_cash += reservation.cash_reserve
    if local_cash > snapshot.account.available or commitment > policy.max_cash_reserve:
        blockers.append("INSUFFICIENT_CASH")
    gross = max(resources.gross_position, sum(p.Position for p in positions.values()))
    gross += sum(_unreflected(r, snapshot, proof) for r in active if _reservation_phase(r, blockers) is c.Phase.OPEN)
    gross += sum(leg.volume for leg in legs if leg.phase is c.Phase.OPEN and leg.leg_id not in claimed)
    if gross > policy.max_gross_position:
        blockers.append("GROSS_POSITION_LIMIT")
    if resources.inflight_legs >= policy.max_inflight_legs:
        blockers.append("INFLIGHT_LIMIT")


def plan(intent: c.OrderIntent, snapshot: c.ExecutionSnapshot, reservation_view: c.ReservationView,
         policy: c.RiskPolicy, now: datetime) -> c.Plan:
    """Calculate all legs or a diagnostic rejection without taking resources.

    Reverse OPEN legs are budget commitments only: dependencies are not a fill
    barrier. The executor must settle every CLOSE, obtain a new complete
    snapshot and replan the unattempted OPEN before sending it.
    """
    for value, expected, name in ((intent, c.OrderIntent, "intent"), (snapshot, c.ExecutionSnapshot, "snapshot"),
                                  (reservation_view, c.ReservationView, "reservation_view"), (policy, c.RiskPolicy, "policy")):
        _typed(value, expected, name)
    now = _now(now)
    blockers = []
    _binding(intent.binding, snapshot.binding, blockers)
    _resource_scope(snapshot, reservation_view, blockers)
    if intent.expires_at <= now or intent.created_at > _deadline(now, policy.timing.future_skew):
        blockers.append("EXPIRED")
    if policy.valid_until <= now:
        blockers.append("POLICY_EXPIRED")
    deadlines = _freshness(snapshot, policy.timing, now, blockers)
    _contract(snapshot, now, blockers)
    direction, opening, closing, _ = _action(intent.action)
    prices = ((intent.prices.open,) if opening else ()) + ((intent.prices.close,) if closing else ())
    _market(snapshot, prices, (direction,), blockers)
    bounds = _volume_bounds(snapshot.contract, policy, blockers)
    positions = _positions(snapshot, blockers)
    _budget_fields(intent, snapshot, policy, blockers)
    legs = []
    if bounds is not None and not blockers:
        legs = _draft(intent, snapshot, reservation_view, policy, bounds, positions, blockers)
        _budget(snapshot, reservation_view, policy, legs, positions, blockers)
    return _finish(intent, snapshot, policy, min(intent.expires_at, policy.valid_until, *deadlines), legs, blockers)


def _proof_scope(context, now, blockers):
    snapshot, proof = context.snapshot, context.proof
    if proof is None:
        blockers.append("PROOF_REQUIRED")
        return
    if context.mode is not c.ExecutionMode.SIMULATION or snapshot.source is not c.Source.SIMULATION or not proof.verified:
        blockers.append("ACCOUNT_RECONCILIATION_UNSUPPORTED")
    if proof.session_epoch != snapshot.binding.session_epoch:
        blockers.append("SESSION_CHANGED")
    if proof.trading_day != snapshot.binding.trading_day:
        blockers.append("TRADING_DAY_CHANGED")
    if (proof.proof_id != snapshot.proof_id or proof.position_snapshot_id != snapshot.times.positions.snapshot_id or
            any(p.timing.snapshot_id != proof.position_snapshot_id for p in snapshot.positions)):
        blockers.append("PROOF_REQUIRED")
    coverage = snapshot.coverage
    if (context.readiness.coverage != coverage or coverage.level is not c.CoverageLevel.FULL_ACCOUNT or
            coverage.complete_through != proof.complete_through or coverage.as_of != proof.as_of or
            coverage.account_active_count is None or proof.order_inventory is None or
            coverage.account_active_count != sum(item.active for item in proof.order_inventory)):
        blockers.append("BASELINE_GAP")
    if (proof.as_of > _deadline(now, context.policy.timing.future_skew) or
            now > _deadline(proof.as_of, context.policy.timing.position_ttl)):
        blockers.append("PROOF_REQUIRED")
    if proof.complete_through is None or now > _deadline(proof.complete_through.as_of, context.policy.timing.position_ttl):
        blockers.append("PROOF_REQUIRED")


def _reservation_key(leg, binding):
    side = (c.Side.LONG if leg.direction is c.Direction.BUY else c.Side.SHORT) if leg.phase is c.Phase.OPEN else (
        c.Side.LONG if leg.direction is c.Direction.SELL else c.Side.SHORT)
    return c.ReservationKey(account_key=binding.account_key, exchange=binding.exchange,
                            instrument_id=binding.instrument_id, hedge_flag=binding.hedge_flag,
                            side=side, bucket=leg.position_bucket or c.Bucket.ALL)


def _claim(plan, context, blockers):
    """Match a journal-issued receipt to its complete latest typed view.

    This verifies consistency, not provenance: the caller must obtain both
    values from N3, never from an untrusted payload. No resources are released.
    """
    receipt, resources = context.reservation_receipt, context.reservation_view
    if receipt is None:
        blockers.append("RESERVATION_REQUIRED")
        return (), ()
    if (receipt.plan_id != plan.plan_id or receipt.reservation_ids != tuple(leg.leg_id for leg in plan.legs) or
            receipt.revision > resources.ledger_revision or receipt.revision <= plan.ledger_revision):
        blockers.append("RESERVATION_REQUIRED")
        return (), ()
    claimed, completed = [], []
    for leg in plan.legs:
        candidates = tuple(r for r in resources.reservations if r.leg_id == leg.leg_id)
        active = tuple(r for r in candidates if r.state is not c.ReservationState.RELEASED)
        released = tuple(r for r in candidates if r.state is c.ReservationState.RELEASED)
        if active and released:
            # F08: one receipt view cannot hold an active reservation and a
            # release history for the same leg; reject regardless of stamps.
            blockers.append("RESERVATION_REQUIRED")
            continue
        reservation = active[0] if len(active) == 1 else None
        if reservation is None and leg.phase is c.Phase.CLOSE:
            # A RELEASED row is a release-time audit record: its stamp is the
            # proof that covered the release, never the current one, and later
            # proofs must not re-stamp history (interface L54/L70). Only the
            # trusted ledger writes such rows, after its own FILLED/settled/
            # trade-gap checks; this function never re-authenticates them and
            # keeps matching resource key, volume and cash exactly.
            closed = tuple(r for r in released if r.reflected_evidence_id is not None)
            if closed:
                reservation = closed[-1]
                completed.append(leg.leg_id)
        if (reservation is None or reservation.resource_key != _reservation_key(leg, context.binding) or
                reservation.volume != leg.volume or reservation.cash_reserve != leg.cash_reserve):
            blockers.append("RESERVATION_REQUIRED")
        else:
            claimed.append(leg.leg_id)
    return tuple(claimed), tuple(completed)


def _frozen_legs(plan, intent, snapshot, policy, bounds, completed, blockers):
    direction, opening, closing, _ = _action(intent.action)
    closes = tuple(leg for leg in plan.legs if leg.phase is c.Phase.CLOSE)
    opens = tuple(leg for leg in plan.legs if leg.phase is c.Phase.OPEN)
    if bool(opens) != opening or bool(closes) != closing:
        blockers.append("QUANTITY_CONFLICT")
    if opening and sum(x.volume for x in opens) != intent.volume:
        blockers.append("QUANTITY_CONFLICT")
    if closing and not opening and sum(x.volume for x in closes) != intent.volume:
        blockers.append("QUANTITY_CONFLICT")
    sequences = {}
    minimum, maximum, step = bounds
    saw_open = False
    for leg in plan.legs:
        saw_open |= leg.phase is c.Phase.OPEN
        if saw_open and leg.phase is c.Phase.CLOSE:
            blockers.append("QUANTITY_CONFLICT")
        key = (leg.phase, leg.position_bucket)
        sequence = sequences.get(key, 0)
        sequences[key] = sequence + 1
        expected_id = f"{intent.intent_id}:{leg.phase.value}:{leg.position_bucket.value if leg.position_bucket else '-'}:{sequence}"
        expected_price = intent.prices.open if leg.phase is c.Phase.OPEN else intent.prices.close
        dependencies = tuple(x.leg_id for x in closes) if leg.phase is c.Phase.OPEN else ()
        if (leg.leg_id != expected_id or leg.direction is not direction or leg.price != expected_price or
                leg.depends_on != dependencies):
            blockers.append("QUANTITY_CONFLICT")
        if leg.leg_id not in completed:
            if not minimum <= leg.volume <= maximum or leg.volume % step:
                blockers.append("VOLUME_SPLIT_IMPOSSIBLE")
            rate = policy.cash_per_open_lot if leg.phase is c.Phase.OPEN else policy.fee_per_close_lot
            with localcontext() as decimal_context:
                decimal_context.prec = 512
                if rate is None or leg.cash_reserve != leg.volume * rate:
                    blockers.append("INSUFFICIENT_CASH")
        if leg.phase is c.Phase.CLOSE:
            if snapshot.binding.exchange in _SPLIT_EXCHANGES:
                pairs = {c.Bucket.TD: c.Offset.CLOSE_TODAY, c.Bucket.YD: c.Offset.CLOSE_YESTERDAY}
                if pairs.get(leg.position_bucket) is not leg.offset:
                    blockers.append("UNSUPPORTED_CLOSE_POLICY")
                if (intent.close_policy is c.ClosePolicy.TODAY_ONLY and leg.position_bucket is not c.Bucket.TD or
                        intent.close_policy is c.ClosePolicy.YESTERDAY_ONLY and leg.position_bucket is not c.Bucket.YD):
                    blockers.append("UNSUPPORTED_CLOSE_POLICY")
            elif (leg.offset is not c.Offset.CLOSE or leg.position_bucket is not c.Bucket.ALL or
                  intent.close_policy is not c.ClosePolicy.AUTO_YD_FIRST):
                blockers.append("UNSUPPORTED_CLOSE_POLICY")
    if snapshot.binding.exchange in _SPLIT_EXCHANGES and intent.close_policy is c.ClosePolicy.AUTO_YD_FIRST:
        buckets = [leg.position_bucket for leg in closes]
        if c.Bucket.TD in buckets and c.Bucket.YD in buckets[buckets.index(c.Bucket.TD):]:
            blockers.append("UNSUPPORTED_CLOSE_POLICY")


def _remaining_close(plan, context, positions, claimed, completed, blockers):
    _, opening, closing, side = _action(context.intent.action)
    if not closing:
        return
    position = positions[side]
    closes = tuple(leg for leg in plan.legs if leg.phase is c.Phase.CLOSE)
    remaining = tuple(leg for leg in closes if leg.leg_id not in completed)
    if opening:
        if positions[c.Side.LONG if side is c.Side.SHORT else c.Side.SHORT].Position:
            blockers.append("POSITION_DATA_INVALID")
        # A settled earlier CLOSE reduces the position needed by the remaining
        # CLOSE legs. It does not settle the OPEN dependencies. Until all
        # closes finish, retain the original topology and check only the exact
        # remaining original-side position and its current available buckets.
        if sum(leg.volume for leg in remaining) != position.Position:
            blockers.append("RECONCILE_REQUIRED")
        if completed and not remaining and plan.snapshot_id != context.snapshot.snapshot_id:
            blockers.append("RECONCILE_REQUIRED")
    today, yesterday = _close_available(position, context.snapshot, context.reservation_view, blockers,
                                        exclude=claimed, proof=context.proof)
    requirements = {c.Bucket.TD: 0, c.Bucket.YD: 0, c.Bucket.ALL: 0}
    active = {r.leg_id: r for r in context.reservation_view.reservations if r.state is not c.ReservationState.RELEASED}
    for leg in remaining:
        reservation = active.get(leg.leg_id)
        quantity = _unreflected(reservation, context.snapshot, context.proof) if reservation else leg.volume
        requirements[leg.position_bucket] += quantity
    if (requirements[c.Bucket.TD] > today or requirements[c.Bucket.YD] > yesterday or
            requirements[c.Bucket.ALL] > today + yesterday):
        blockers.append("INSUFFICIENT_POSITION")


def validate_plan(plan: c.Plan, latest_context: c.ExecutionContext, now: datetime) -> c.ValidationResult:
    """Recheck frozen legs and current journal evidence, without submitting.

    An OK result validates the plan budget; it does not settle dependencies or
    select a leg. In particular N3/N4 must gate reverse OPEN on settled fills
    and refresh_pending_open before an attempt.
    """
    _typed(plan, c.Plan, "plan")
    _typed(latest_context, c.ExecutionContext, "latest_context")
    now = _now(now)
    context = latest_context
    intent = context.intent
    action = c.AuthorizationAction(intent.action.value) if intent is not None else None
    blockers = list(plan.blockers) + list(c.validate_context(context, now, action=action, require_attempt=True).blockers)
    if plan.expires_at < now:
        blockers.append("EXPIRED")
    if intent is None or context.reservation_view is None:
        return c.ValidationResult(blockers=tuple(dict.fromkeys(blockers)))
    if plan.intent_hash != c.canonical_hash(intent):
        blockers.append("INTENT_CONFLICT")
    if plan.plan_id != _plan_hash(plan.intent_hash, plan.snapshot_id, plan.ledger_revision, plan.policy_id, plan.expires_at, plan.legs):
        blockers.append("INTENT_CONFLICT")
    if plan.policy_id != context.policy.policy_id:
        blockers.append("POLICY_EXPIRED")
    if plan.ledger_revision > context.ledger_revision:
        blockers.append("LEDGER_REVISION_MISMATCH")
    _binding(intent.binding, context.binding, blockers)
    _binding(intent.binding, context.snapshot.binding, blockers)
    _resource_scope(context.snapshot, context.reservation_view, blockers)
    deadlines = _freshness(context.snapshot, context.policy.timing, now, blockers)
    if plan.expires_at > min(intent.expires_at, context.policy.valid_until, *deadlines):
        blockers.append("EXPIRED")
    _contract(context.snapshot, now, blockers)
    _proof_scope(context, now, blockers)
    positions = _positions(context.snapshot, blockers)
    _budget_fields(intent, context.snapshot, context.policy, blockers)
    bounds = _volume_bounds(context.snapshot.contract, context.policy, blockers)
    claimed, completed = _claim(plan, context, blockers)
    pending = tuple(leg for leg in plan.legs if leg.leg_id not in completed)
    _market(context.snapshot, tuple(leg.price for leg in pending), tuple(leg.direction for leg in pending), blockers)
    auth = context.authorization
    if auth is not None:
        if intent.volume > auth.max_volume or any(leg.volume > auth.max_volume for leg in plan.legs):
            blockers.append("AUTHORIZATION_ACTION")
        price_bounds = tuple(bound for bound in auth.price_bounds if
                             (bound.symbol.exchange, bound.symbol.instrument_id) == (intent.binding.exchange, intent.binding.instrument_id))
        if not price_bounds or any(not any(bound.lower <= leg.price <= bound.upper for bound in price_bounds) for leg in pending):
            blockers.append("PRICE_OUT_OF_BOUNDS")
    structural = {"MISSING_POSITION_BUCKET", "POSITION_DATA_INVALID", "CASH_BOUND_MISSING", "CASH_BOUND_UNVERIFIED",
                  "CONTRACT_MISSING", "INSUFFICIENT_CASH"}
    if bounds is not None and not structural.intersection(blockers):
        _frozen_legs(plan, intent, context.snapshot, context.policy, bounds, completed, blockers)
        _remaining_close(plan, context, positions, claimed, completed, blockers)
        if context.policy.max_gross_position is not None and context.policy.max_inflight_legs is not None:
            _budget(context.snapshot, context.reservation_view, context.policy, pending, positions, blockers, claimed, context.proof)
    return c.ValidationResult(blockers=tuple(dict.fromkeys(blockers)))


# ---------------------------------------------------------------------------
# NE1-C2: native pilot limited-scope admission (plan/validate_plan untouched)
# ---------------------------------------------------------------------------

PILOT_BASELINE_MAX_AGE = timedelta(minutes=5)
_PILOT_SINGLE_SEND_ACTIONS = (c.Action.OPEN_LONG, c.Action.OPEN_SHORT,
                              c.Action.CLOSE_LONG, c.Action.CLOSE_SHORT)


@dataclass(frozen=True, slots=True, kw_only=True)
class NativePilotScope:
    """Trusted-assembler inputs for the native pilot scope (NE1-C1 frozen types).

    The registry is the only capability origin: permits and evidence enter as
    verified typed records, never as payload booleans. Planning verifies only;
    it never registers, consumes or mutates anything.
    """

    readiness: "nc.V2Readiness"
    permit: "nc.NativePilotPermit"
    baseline: "ne.NativeBaselineEvidence"
    route: "ne.NativeRouteEvidence"
    registry: "ne.EvidenceRegistry"


@dataclass(frozen=True, slots=True, kw_only=True)
class NativePilotAdmission(NativePilotScope):
    """Full typed revalidation bundle for one pilot plan attempt."""

    intent: c.OrderIntent
    snapshot: c.ExecutionSnapshot
    reservation_view: c.ReservationView
    policy: c.RiskPolicy
    reservation_receipt: c.ReservationReceipt | None

    @property
    def binding(self):
        return self.intent.binding

    @property
    def proof(self):
        # Native absorption belongs to N3's settlement-evidence path; the v1
        # full-account proof machinery never applies to pilot scope.
        return None


def _scope_admission(scope, intent, snapshot, now, blockers):
    """Dispatch by execution_scope: only NATIVE_PILOT is admitted here.

    simulation_full_account keeps its byte-identical v1 path inside
    validate_plan/_proof_scope; every other scope is blocked for these entry
    points. Pilot checks verify only and consume nothing.
    """
    readiness = scope.readiness
    if readiness.execution_scope is not nc.ExecutionScope.NATIVE_PILOT:
        blockers.append("NATIVE_DISABLED")
        return
    if (readiness.mode is not c.ExecutionMode.NATIVE or not readiness.can_submit or
            readiness.state is not c.ReadinessState.EXECUTABLE or readiness.blockers):
        blockers.append("NATIVE_DISABLED")
        return
    if readiness.native_permit_id != scope.permit.permit_id:
        blockers.append("AUTHORIZATION_REQUIRED")
        return
    # A simulation snapshot can never enter the native pilot scope (H34).
    if snapshot.source is not c.Source.PYTHONGO:
        blockers.append("OWNERSHIP_CONFLICT")
        return
    if scope.permit.expires_at <= now or not scope.registry.verify_permit_active(
            scope.permit.permit_id, now=now).ok:
        blockers.append("AUTHORIZATION_REQUIRED")
        return
    _binding(scope.permit.binding, intent.binding, blockers)
    _binding(intent.binding, snapshot.binding, blockers)
    action = c.AuthorizationAction(intent.action.value)
    if (action not in scope.permit.actions or intent.action not in _PILOT_SINGLE_SEND_ACTIONS or
            intent.volume > scope.permit.max_volume):
        blockers.append("AUTHORIZATION_ACTION")
    price = intent.prices.open if intent.action not in (c.Action.CLOSE_LONG, c.Action.CLOSE_SHORT) else intent.prices.close
    bounds = tuple(bound for bound in scope.permit.price_bounds if
                   (bound.symbol.exchange, bound.symbol.instrument_id) ==
                   (intent.binding.exchange, intent.binding.instrument_id))
    if scope.permit.price_bounds and (not bounds or
                                      not any(bound.lower <= price <= bound.upper for bound in bounds)):
        blockers.append("PRICE_OUT_OF_BOUNDS")
    if scope.permit.baseline_evidence_id != scope.baseline.evidence_id:
        blockers.append("BASELINE_GAP")
    if scope.permit.route_evidence_id != scope.route.evidence_id:
        blockers.append("AUTHORIZATION_REQUIRED")
    if not ne.validate_baseline(scope.baseline, now=now).ok:
        blockers.append("BASELINE_GAP")
    if not ne.validate_route(scope.route, now=now).ok:
        blockers.append("AUTHORIZATION_REQUIRED")
    if (scope.baseline.as_of > now + timedelta(seconds=2) or
            now - scope.baseline.as_of > PILOT_BASELINE_MAX_AGE):
        # The 5-minute arming window is its own bound: it never renews quote,
        # account or position component TTLs (H07).
        blockers.append("BASELINE_GAP")
    if scope.baseline.account_key != intent.binding.account_key:
        blockers.append("ACCOUNT_MISMATCH")
    if scope.baseline.session_epoch != intent.binding.session_epoch:
        blockers.append("SESSION_CHANGED")
    if scope.baseline.trading_day != intent.binding.trading_day:
        blockers.append("TRADING_DAY_CHANGED")
    if scope.route.binding != intent.binding:
        blockers.append("OWNERSHIP_CONFLICT")


def _pilot_budget_fields(intent, snapshot, policy, blockers):
    """Pilot cash bound: the permit's cash-bound evidence is the authority;
    policy.cash_bound_source is not consulted (H34) and the v1 simulation
    proof machinery never applies. Rate fields still come from the policy."""
    _, opening, closing, _ = _action(intent.action)
    if opening and policy.cash_per_open_lot is None:
        blockers.append("CASH_BOUND_MISSING")
    if closing and policy.fee_per_close_lot is None:
        blockers.append("CASH_BOUND_MISSING")
    if policy.max_cash_reserve is None:
        blockers.append("CASH_BOUND_MISSING")
    if policy.max_gross_position is None:
        blockers.append("GROSS_POSITION_LIMIT")
    if policy.max_inflight_legs is None:
        blockers.append("INFLIGHT_LIMIT")
    if (snapshot.account is None or snapshot.account.available is None or
            snapshot.account.currency is None):
        blockers.append("INSUFFICIENT_CASH")


def plan_native_pilot(intent: c.OrderIntent, snapshot: c.ExecutionSnapshot,
                      reservation_view: c.ReservationView, policy: c.RiskPolicy,
                      scope: NativePilotScope, now: datetime) -> c.Plan:
    """Pure pilot planning: identical economics, single-send topology only.

    Verifies the limited-scope admission without consuming anything and never
    relaxes price, budget, freshness or volume rules (NE-CR03). Splits that
    would need several real sends are rejected whole, as are reverse actions.
    """
    for value, expected, name in ((intent, c.OrderIntent, "intent"),
                                  (snapshot, c.ExecutionSnapshot, "snapshot"),
                                  (reservation_view, c.ReservationView, "reservation_view"),
                                  (policy, c.RiskPolicy, "policy")):
        _typed(value, expected, name)
    if not isinstance(scope, NativePilotScope):
        raise c.ContractError("scope: expected NativePilotScope")
    now = _now(now)
    blockers = []
    _scope_admission(scope, intent, snapshot, now, blockers)
    if intent.expires_at <= now or intent.created_at > _deadline(now, policy.timing.future_skew):
        blockers.append("EXPIRED")
    if policy.valid_until <= now:
        blockers.append("POLICY_EXPIRED")
    _resource_scope(snapshot, reservation_view, blockers)
    deadlines = _freshness(snapshot, policy.timing, now, blockers)
    _contract(snapshot, now, blockers)
    _, opening, closing, _ = _action(intent.action)
    price = intent.prices.open if opening else intent.prices.close
    direction = c.Direction.BUY if intent.action in (c.Action.OPEN_LONG, c.Action.CLOSE_SHORT) else c.Direction.SELL
    _market(snapshot, (price,), (direction,), blockers)
    _pilot_budget_fields(intent, snapshot, policy, blockers)
    bounds = _volume_bounds(snapshot.contract, policy, blockers)
    positions = _positions(snapshot, blockers)
    legs = []
    if bounds is not None and not blockers:
        legs = _draft(intent, snapshot, reservation_view, policy, bounds, positions, blockers)
        _budget(snapshot, reservation_view, policy, legs, positions, blockers, (), None)
        if len(legs) > scope.permit.max_send_attempts:
            # One bounded real send per permit: a legal split needing more
            # sends than the permit allows fails whole, never partially.
            blockers.append("AUTHORIZATION_ACTION")
    return _finish(intent, snapshot, policy,
                   min(intent.expires_at, policy.valid_until, *deadlines), legs, blockers)


def validate_native_pilot_plan(plan: c.Plan, admission: NativePilotAdmission,
                               now: datetime) -> c.ValidationResult:
    """Recheck a frozen pilot plan against the latest trusted typed evidence.

    An OK result validates scope, topology, budget and freshness only. It
    never grants a native call: dispatch, CONSUMED receipts and the single
    call remain N3/N4/client responsibilities (NE1-H16/H17/H28).
    """
    _typed(plan, c.Plan, "plan")
    if not isinstance(admission, NativePilotAdmission):
        raise c.ContractError("admission: expected NativePilotAdmission")
    now = _now(now)
    intent, snapshot, policy = admission.intent, admission.snapshot, admission.policy
    blockers = list(plan.blockers)
    if plan.intent_hash != c.canonical_hash(intent):
        blockers.append("INTENT_CONFLICT")
    if plan.plan_id != _plan_hash(plan.intent_hash, plan.snapshot_id, plan.ledger_revision,
                                  plan.policy_id, plan.expires_at, plan.legs):
        blockers.append("INTENT_CONFLICT")
    if plan.policy_id != policy.policy_id:
        blockers.append("POLICY_EXPIRED")
    if plan.ledger_revision > snapshot.ledger_revision:
        blockers.append("LEDGER_REVISION_MISMATCH")
    if intent.expires_at <= now or intent.created_at > _deadline(now, policy.timing.future_skew):
        blockers.append("EXPIRED")
    if policy.valid_until <= now:
        blockers.append("POLICY_EXPIRED")
    if plan.expires_at < now:
        blockers.append("EXPIRED")
    _binding(intent.binding, snapshot.binding, blockers)
    _resource_scope(snapshot, admission.reservation_view, blockers)
    deadlines = _freshness(snapshot, policy.timing, now, blockers)
    if plan.expires_at > min(intent.expires_at, policy.valid_until, *deadlines):
        blockers.append("EXPIRED")
    _contract(snapshot, now, blockers)
    _scope_admission(admission, intent, snapshot, now, blockers)
    positions = _positions(snapshot, blockers)
    _pilot_budget_fields(intent, snapshot, policy, blockers)
    bounds = _volume_bounds(snapshot.contract, policy, blockers)
    claimed, completed = _claim(plan, admission, blockers)
    pending = tuple(leg for leg in plan.legs if leg.leg_id not in completed)
    _market(snapshot, tuple(leg.price for leg in pending), tuple(leg.direction for leg in pending), blockers)
    if admission.permit.price_bounds:
        symbol_bounds = tuple(bound for bound in admission.permit.price_bounds if
                              (bound.symbol.exchange, bound.symbol.instrument_id) ==
                              (intent.binding.exchange, intent.binding.instrument_id))
        if not symbol_bounds or any(not any(bound.lower <= leg.price <= bound.upper
                                            for bound in symbol_bounds) for leg in pending):
            blockers.append("PRICE_OUT_OF_BOUNDS")
    if len(pending) > admission.permit.max_send_attempts:
        blockers.append("AUTHORIZATION_ACTION")
    structural = {"MISSING_POSITION_BUCKET", "POSITION_DATA_INVALID", "CASH_BOUND_MISSING",
                  "CONTRACT_MISSING", "INSUFFICIENT_CASH"}
    if bounds is not None and not structural.intersection(blockers):
        _frozen_legs(plan, intent, snapshot, policy, bounds, completed, blockers)
        _remaining_close(plan, admission, positions, claimed, completed, blockers)
        if policy.max_gross_position is not None and policy.max_inflight_legs is not None:
            _budget(snapshot, admission.reservation_view, policy, pending, positions,
                    blockers, claimed, admission.proof)
    return c.ValidationResult(blockers=tuple(dict.fromkeys(blockers)))
