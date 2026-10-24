"""Contract identity, calendar/D construction, selectors and the gate table.

Pure, offline module (finalized-plan §五/§六/§七): it never connects to a
database, never reads the environment and never inspects future rows. Every
unknown is surfaced as ``None``/status plus a reason — never as zero.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Sequence

from ak_trading.futures.spec import (
    ClockStatus,
    ContractBar,
    ContractMetadata,
    FieldEvidence,
    GateResult,
    PairSelectionState,
    ProductKey,
    ReadinessAction,
    ResearchPolicy,
    parse_product_token,
)

__all__ = [
    "SHANGHAI_TZ",
    "datetime_to_nano",
    "nano_to_datetime",
    "ContractInstance",
    "UniverseCache",
    "UniversePolicy",
    "SessionWindows",
    "ProductCalendarEvidence",
    "DaySequence",
    "ObservationRow",
    "RollCandidate",
    "RollSelectionState",
    "PairCandidate",
    "ActionInputs",
    "CALENDAR_RULE_VERSION",
    "FIXED_UNIVERSE",
    "load_universe_cache",
    "resolve_product_contracts",
    "instance_observation_start",
    "build_day_sequence",
    "build_observation_grid",
    "median_volume",
    "rank_roll_candidates",
    "roll_open_eligibility",
    "roll_switch_confirmed",
    "exit_preferred_before_cutoff",
    "synchronized_far_bar",
    "pair_gap_days",
    "update_pair_selection",
    "evaluate_readiness",
    "aggregate_readiness",
    "load_catalog_products",
]

SHANGHAI_TZ = timezone(timedelta(hours=8))

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def datetime_to_nano(value: datetime) -> int:
    """Exact integer nanoseconds since epoch (no float precision loss)."""
    delta = value - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1_000


def nano_to_datetime(value: int) -> datetime:
    """Inverse of :func:`datetime_to_nano` (microsecond-precision result)."""
    seconds, nanos = divmod(value, 1_000_000_000)
    return datetime.fromtimestamp(0, tz=timezone.utc) + timedelta(
        seconds=seconds, microseconds=nanos // 1_000
    )

CALENDAR_RULE_VERSION = "calendar_union_v1"

#: Fixed candidate universe U (§五.5): six products, never substituted.
FIXED_UNIVERSE: tuple[ProductKey, ...] = (
    ProductKey("SHFE", "RB"),
    ProductKey("INE", "SC"),
    ProductKey("DCE", "M"),
    ProductKey("CZCE", "TA"),
    ProductKey("CFFEX", "IF"),
    ProductKey("GFEX", "SI"),
)

_LISTING_FALLBACK_MAX_DAYS = 730


# ---------------------------------------------------------------------------
# Universe cache and contract instances
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContractInstance:
    """One cached contract identity; expiry seconds kept verbatim."""

    instance_id: str
    raw_symbol: str
    product: ProductKey
    expiry_at: datetime | None
    expired_in_cache: bool
    identity_status: str = "reconstructed_static"
    listing_at: datetime | None = None
    identity_issues: tuple[str, ...] = ()

    @property
    def listing_history_unknown(self) -> bool:
        return self.listing_at is None

    def research_window_start(
        self, request_start: datetime
    ) -> tuple[datetime, str | None]:
        """Observation start given the 730-day fallback rule (§五.3).

        Without ``listing_at`` the instance may only be observed from at most
        730 calendar days before expiry, flagged ``listing_history_unknown``.
        The flag is a disclosure, not proof of listing date.
        """
        if self.listing_at is not None:
            return max(request_start, self.listing_at), None
        if self.expiry_at is None:
            return request_start, "listing_history_unknown_no_expiry"
        fallback_start = self.expiry_at - timedelta(days=_LISTING_FALLBACK_MAX_DAYS)
        return max(request_start, fallback_start), "listing_history_unknown"


@dataclass(frozen=True)
class UniverseCache:
    """Immutable view over the FUTURE discovery cache."""

    generated_at: str
    sha256: str
    path: str
    products: Mapping[ProductKey, tuple[ContractInstance, ...]]


def _instance_from_entry(entry: Mapping[str, object], product: ProductKey) -> ContractInstance:
    raw_symbol = str(entry["symbol"])
    expiry_seconds = entry.get("expire_datetime")
    expiry_at: datetime | None = None
    issues: list[str] = []
    if expiry_seconds is None:
        issues.append("missing_expiry")
    else:
        expiry_at = datetime.fromtimestamp(int(expiry_seconds), tz=SHANGHAI_TZ)
    instance_id = raw_symbol if expiry_at is None else f"{raw_symbol}|{expiry_at.isoformat()}"
    issues.append("listing_at_missing")
    return ContractInstance(
        instance_id=instance_id,
        raw_symbol=raw_symbol,
        product=product,
        expiry_at=expiry_at,
        expired_in_cache=bool(entry.get("expired", False)),
        identity_issues=tuple(issues),
    )


def load_universe_cache(path: str | Path) -> UniverseCache:
    """Load the contract universe cache without touching the network."""
    cache_path = Path(path)
    payload = cache_path.read_bytes()
    document = json.loads(payload.decode("utf-8"))
    products: dict[ProductKey, tuple[ContractInstance, ...]] = {}
    for key, entries in (document.get("products") or {}).items():
        exchange, _, product_code = str(key).partition(".")
        product = ProductKey(exchange=exchange.upper(), product=product_code.upper())
        instances = tuple(
            _instance_from_entry(entry, product)
            for entry in sorted(entries, key=lambda e: str(e.get("symbol")))
        )
        products[product] = instances
    return UniverseCache(
        generated_at=str(document.get("generated_at", "")),
        sha256=hashlib.sha256(payload).hexdigest(),
        path=str(cache_path),
        products=products,
    )


def resolve_product_contracts(
    cache: UniverseCache, product: ProductKey
) -> tuple[ContractInstance, ...]:
    """Resolve the cached membership of one product.

    Cache keys preserve raw case (``SHFE.rb`` vs ``DCE.M``), so lookup is
    performed on the normalized ProductKey. Returned symbols keep their raw
    stored case and are never upper()-ed (§五.2).
    """
    return cache.products.get(product, ())


@dataclass(frozen=True)
class UniversePolicy:
    """Fixed candidate universe; no substitution for missing data (§五.5)."""

    products: tuple[ProductKey, ...] = FIXED_UNIVERSE

    def review(self, requested: Sequence[ProductKey]) -> tuple[tuple[ProductKey, ...], tuple[str, ...]]:
        ordered: list[ProductKey] = []
        issues: list[str] = []
        for token in requested:
            try:
                key = token if isinstance(token, ProductKey) else parse_product_token(str(token))
            except ValueError as exc:
                issues.append(f"invalid_product_token:{exc}")
                continue
            if key not in self.products:
                issues.append(f"outside_fixed_universe:{key}")
                continue
            if key not in ordered:
                ordered.append(key)
        return tuple(sorted(ordered, key=lambda k: str(k))), tuple(issues)


# ---------------------------------------------------------------------------
# Calendar union D (calendar_union_v1, §五.6)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionWindows:
    """Complete session windows for one product on one trading day."""

    windows: tuple[tuple[datetime, datetime], ...] = ()

    def final_close(self) -> datetime | None:
        if not self.windows:
            return None
        return max(end for _, end in self.windows)


@dataclass(frozen=True)
class ProductCalendarEvidence:
    """Pre-frozen calendar evidence for one product (§五.6.a)."""

    product: ProductKey
    valid_from: date
    valid_through: date
    open_days: Mapping[date, SessionWindows]
    closed_days: frozenset[date]
    listed_from: date | None = None
    delisted_through: date | None = None
    source_status: str = "review_required"
    evidence_id: str = ""
    evidence_sha256: str = ""

    def status_on(self, day: date) -> str:
        """Classify one civil date: open/closed/not_yet_listed/delisted/unknown."""
        if self.listed_from is not None and day < self.listed_from:
            return "not_yet_listed"
        if self.delisted_through is not None and day > self.delisted_through:
            return "delisted"
        if not (self.valid_from <= day <= self.valid_through):
            return "unknown"
        if day in self.closed_days:
            return "closed"
        windows = self.open_days.get(day)
        if windows is not None:
            return "open"
        # Within the declared validity range a day that is neither declared
        # closed nor declared open cannot be guessed (§五.6.b).
        return "unknown"


def _canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")


def day_sequence_digest(dates: Sequence[date]) -> str:
    """SHA256 over UTF-8 ``YYYY-MM-DD\\n`` lines including the final LF."""
    payload = "".join(f"{day.isoformat()}\n" for day in dates).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class DaySequence:
    """Result of calendar_union_v1; the only legal common day axis."""

    rule_version: str
    status: str  # available | unavailable
    dates: tuple[date, ...]
    universe: tuple[ProductKey, ...]
    span_start: datetime
    span_end: datetime
    as_of: datetime
    per_product_summary: Mapping[str, str]
    calendar_missing_ranges: tuple[Mapping[str, str], ...]
    day_sequence_sha256: str
    construction_sha256: str
    evidence_hashes: Mapping[str, str]

    @property
    def common_mark_available(self) -> bool:
        return self.status == "available"


def build_day_sequence(
    span_start: datetime,
    span_end: datetime,
    as_of: datetime,
    evidence_by_product: Mapping[ProductKey, ProductCalendarEvidence],
    universe: Sequence[ProductKey] = FIXED_UNIVERSE,
) -> DaySequence:
    """Construct D exactly per calendar_union_v1 (§五.6).

    Any unknown day or conflicting open-session evidence for any product
    makes the whole sequence unavailable — a partial D is never assembled.
    """
    products = tuple(universe)
    missing_evidence = [str(p) for p in products if p not in evidence_by_product]

    construction_input = {
        "rule_version": CALENDAR_RULE_VERSION,
        "span_start": span_start.isoformat(),
        "span_end": span_end.isoformat(),
        "as_of": as_of.isoformat(),
        "universe": [str(p) for p in products],
        "evidence": {
            str(p): {
                "sha256": evidence_by_product[p].evidence_sha256,
                "source_status": evidence_by_product[p].source_status,
            }
            for p in products
            if p in evidence_by_product
        },
    }

    if missing_evidence:
        ranges = tuple(
            {"product": name, "range": f"{span_start.date().isoformat()}..{span_end.date().isoformat()}", "status": "unknown"}
            for name in missing_evidence
        )
        return DaySequence(
            rule_version=CALENDAR_RULE_VERSION,
            status="unavailable",
            dates=(),
            universe=products,
            span_start=span_start,
            span_end=span_end,
            as_of=as_of,
            per_product_summary={name: "no_evidence" for name in missing_evidence},
            calendar_missing_ranges=ranges,
            day_sequence_sha256=hashlib.sha256(b"").hexdigest(),
            construction_sha256=hashlib.sha256(_canonical_json(construction_input)).hexdigest(),
            evidence_hashes={
                str(p): evidence_by_product[p].evidence_sha256
                for p in products
                if p in evidence_by_product
            },
        )

    first_day = span_start.date()
    last_day_exclusive = span_end.date()
    civil_days: list[date] = []
    cursor = first_day
    while cursor < last_day_exclusive:
        civil_days.append(cursor)
        cursor += timedelta(days=1)

    statuses: dict[ProductKey, dict[date, str]] = {}
    open_conflicts: list[str] = []
    for product in products:
        evidence = evidence_by_product[product]
        per_day: dict[date, str] = {}
        for day in civil_days:
            status = evidence.status_on(day)
            if status == "open":
                windows = evidence.open_days[day]
                if windows.final_close() is None:
                    status = "unknown"
                    open_conflicts.append(f"{product}|{day.isoformat()}|empty_session")
            per_day[day] = status
        statuses[product] = per_day

    unknown_days = sorted(
        {
            day
            for product in products
            for day, status in statuses[product].items()
            if status == "unknown"
        }
    )
    if unknown_days or open_conflicts:
        ranges: list[Mapping[str, str]] = [
            {
                "product": str(product),
                "range": f"{unknown_days[0].isoformat()}..{unknown_days[-1].isoformat()}",
                "status": "unknown",
            }
            for product in products
            if any(statuses[product][d] == "unknown" for d in unknown_days)
        ]
        return DaySequence(
            rule_version=CALENDAR_RULE_VERSION,
            status="unavailable",
            dates=(),
            universe=products,
            span_start=span_start,
            span_end=span_end,
            as_of=as_of,
            per_product_summary={
                str(product): "has_unknown_days" for product in products
            },
            calendar_missing_ranges=tuple(ranges),
            day_sequence_sha256=hashlib.sha256(b"").hexdigest(),
            construction_sha256=hashlib.sha256(_canonical_json(construction_input)).hexdigest(),
            evidence_hashes={
                str(p): evidence_by_product[p].evidence_sha256 for p in products
            },
        )

    selected: list[date] = []
    for day in civil_days:
        if not any(statuses[p][day] == "open" for p in products):
            continue  # all closed / not listed / delisted => not in D
        open_products = [p for p in products if statuses[p][day] == "open"]
        final_closes = [
            evidence_by_product[p].open_days[day].final_close() for p in open_products
        ]
        if all(close is not None and close <= as_of for close in final_closes):
            selected.append(day)

    summary = {
        str(product): "|".join(
            sorted({statuses[product][day] for day in civil_days})
        )
        for product in products
    }
    return DaySequence(
        rule_version=CALENDAR_RULE_VERSION,
        status="available",
        dates=tuple(selected),
        universe=products,
        span_start=span_start,
        span_end=span_end,
        as_of=as_of,
        per_product_summary=summary,
        calendar_missing_ranges=(),
        day_sequence_sha256=day_sequence_digest(selected),
        construction_sha256=hashlib.sha256(_canonical_json(construction_input)).hexdigest(),
        evidence_hashes={
            str(p): evidence_by_product[p].evidence_sha256 for p in products
        },
    )


# ---------------------------------------------------------------------------
# Observation grid (no-calendar mode, §五.7)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObservationRow:
    """One raw observation row; gaps and clock doubts preserved as-is."""

    contract_instance_id: str
    raw_symbol: str
    product: ProductKey
    bar: ContractBar
    clock_status: ClockStatus


def build_observation_grid(
    instances: Sequence[ContractInstance],
    bars_by_instance: Mapping[str, Sequence[ContractBar]],
    clock_status: ClockStatus = ClockStatus.UNVERIFIED,
) -> tuple[ObservationRow, ...]:
    """Sort raw observations by (instance, datetime_nano); no day claims."""
    by_instance = {
        instance.instance_id: instance for instance in instances
    }
    rows: list[ObservationRow] = []
    for instance_id, bars in bars_by_instance.items():
        instance = by_instance.get(instance_id)
        if instance is None:
            continue
        for bar in sorted(bars, key=lambda b: (b.datetime_nano,)):
            rows.append(
                ObservationRow(
                    contract_instance_id=instance_id,
                    raw_symbol=instance.raw_symbol,
                    product=instance.product,
                    bar=bar,
                    clock_status=clock_status,
                )
            )
    rows.sort(key=lambda row: (row.contract_instance_id, row.bar.datetime_nano))
    return tuple(rows)


# ---------------------------------------------------------------------------
# Roll policy: past_liquidity_forward_roll (§五.10-11)
# ---------------------------------------------------------------------------


def median_volume(bars: Sequence[ContractBar]) -> float | None:
    """Median of non-negative completed volumes; None when nothing observed."""
    values = sorted(
        float(bar.volume) for bar in bars if bar.volume is not None and float(bar.volume) >= 0
    )
    if not values:
        return None
    middle = len(values) // 2
    if len(values) % 2 == 1:
        return values[middle]
    return (values[middle - 1] + values[middle]) / 2.0


@dataclass(frozen=True)
class RollCandidate:
    """One candidate/active contract with its trailing liquidity measure."""

    instance: ContractInstance
    median_volume20: float | None
    volume_window_rows: int

    @property
    def liquidity_status(self) -> str:
        if self.median_volume20 is None:
            return "no_observed_volume"
        if self.volume_window_rows < 20:
            return "insufficient_window"
        return "ready"


@dataclass(frozen=True)
class RollSelectionState:
    """Selector memory: active contract and pending successor (§五.10)."""

    active_symbol: str | None = None
    pending_successor: str | None = None
    confirmation_count: int = 0


def roll_open_eligibility(
    instance: ContractInstance,
    decision_at: datetime,
    policy: ResearchPolicy,
    metadata: ContractMetadata | None = None,
) -> tuple[bool, str | None]:
    """New-open admission: identity/expiry known plus the 20-day buffer.

    ``last_trade_at`` / ``delivery_cutoff_at`` are checked when known
    (§五.10); unknown cutoffs never block by themselves at this layer —
    the strict open gate in ``evaluate_readiness`` handles evidence quality.
    """
    if instance.expiry_at is None:
        return False, "expiry_unknown"
    if not (instance.expiry_at > decision_at + timedelta(days=policy.expiry_buffer_days)):
        return False, "within_expiry_buffer"
    if metadata is not None:
        for cutoff in (metadata.last_trade_at, metadata.delivery_cutoff_at):
            if cutoff is not None and cutoff <= decision_at:
                return False, "past_trading_cutoff"
    return True, None


def rank_roll_candidates(
    candidates: Sequence[RollCandidate],
    decision_at: datetime,
    policy: ResearchPolicy,
    require_eligibility: bool = True,
) -> tuple[RollCandidate, ...]:
    """Order: median volume desc, expiry asc, raw symbol (§五.10)."""
    eligible: list[RollCandidate] = []
    for candidate in candidates:
        if require_eligibility:
            ok, reason = roll_open_eligibility(candidate.instance, decision_at, policy)
            if not ok:
                continue
        eligible.append(candidate)
    return tuple(
        sorted(
            eligible,
            key=lambda c: (
                -(c.median_volume20 if c.median_volume20 is not None else -math.inf),
                c.instance.expiry_at or datetime.max.replace(tzinfo=timezone.utc),
                c.instance.raw_symbol,
            ),
        )
    )


def roll_switch_confirmed(
    active_median_volume: float | None,
    successor_median_volume: float | None,
    policy: ResearchPolicy,
) -> tuple[bool, str]:
    """Successor must beat 1.2x active on completed observations."""
    if active_median_volume is None or successor_median_volume is None:
        return False, "volume_unavailable"
    if successor_median_volume >= policy.roll_volume_ratio * active_median_volume:
        return True, "confirmed"
    return False, "volume_below_ratio"


def exit_preferred_before_cutoff(
    confirmed_remaining_sessions: int | None,
    policy: ResearchPolicy,
) -> tuple[bool, str | None]:
    """Prefer exiting when <= 5 confirmed trade sessions remain (§五.11)."""
    if confirmed_remaining_sessions is None:
        return False, "session_evidence_unknown"
    if confirmed_remaining_sessions <= policy.exit_session_buffer:
        return True, None
    return False, None


# ---------------------------------------------------------------------------
# Carry pair selection (§五.12-13)
# ---------------------------------------------------------------------------


def pair_gap_days(near_expiry: datetime, far_expiry: datetime) -> float:
    """Gap in days; ACT/365 threshold semantics live in the factor."""
    return (far_expiry - near_expiry).total_seconds() / 86400.0


def _source_family(source: str) -> str:
    """Clock/source grading family, e.g. 'tqsdk:IF2106:...' -> 'tqsdk'.

    The stored per-contract source label names the exact series; the clock
    GRADE is the family prefix, which is what pairing compatibility (A4)
    requires — nominally equal timestamps from different source families
    never pair, while the same family's per-contract labels do.
    """
    return source.split(":", 1)[0].strip().lower()


def synchronized_far_bar(
    near_bar: ContractBar, far_bars: Sequence[ContractBar]
) -> ContractBar | None:
    """Same completed trading session and identical datetime_nano (§五.13).

    No forward filling, no cross-day pairing; both legs must share the same
    source/clock GRADING family and duration (A4), while their per-contract
    source labels may legitimately differ.
    """
    near_family = _source_family(near_bar.source)
    for far_bar in far_bars:
        if (
            far_bar.datetime_nano == near_bar.datetime_nano
            and far_bar.trading_day == near_bar.trading_day
            and far_bar.available_at is not None
            and near_bar.available_at is not None
            and _source_family(far_bar.source) == near_family
            and far_bar.duration_seconds == near_bar.duration_seconds
        ):
            return far_bar
    return None


@dataclass(frozen=True)
class PairCandidate:
    """One synchronized near/far observation with gap diagnostics."""

    near_symbol: str
    far_symbol: str
    near_expiry_at: datetime
    far_expiry_at: datetime
    near_bar: ContractBar
    far_bar: ContractBar
    far_median_volume20: float | None

    @property
    def gap_days(self) -> float:
        return pair_gap_days(self.near_expiry_at, self.far_expiry_at)


def rank_pair_candidates(
    candidates: Sequence[PairCandidate], policy: ResearchPolicy
) -> tuple[PairCandidate, ...]:
    """Order: |gap-90| asc, far volume desc, far expiry asc, symbol (§五.12)."""
    in_range = [
        candidate
        for candidate in candidates
        if policy.pair_gap_days_min <= candidate.gap_days <= policy.pair_gap_days_max
        and candidate.near_symbol != candidate.far_symbol
        and candidate.far_expiry_at > candidate.near_expiry_at
    ]
    return tuple(
        sorted(
            in_range,
            key=lambda c: (
                abs(c.gap_days - policy.pair_gap_days_target),
                -(c.far_median_volume20 if c.far_median_volume20 is not None else -math.inf),
                c.far_expiry_at,
                c.far_symbol,
            ),
        )
    )


def update_pair_selection(
    previous: PairSelectionState | None,
    candidates: Sequence[PairCandidate],
    policy: ResearchPolicy,
) -> tuple[PairSelectionState, str, str | None]:
    """Sticky far selection with two-observation confirmation (§五.12).

    Candidates are ranked here (idempotent for pre-ranked lists), so callers
    never have to know the ordering rule. Returns (next_state, status,
    reason). ``unavailable`` keeps carry null.
    """
    ranked = rank_pair_candidates(candidates, policy)
    if not ranked:
        if previous is not None and previous.far_symbol is not None:
            return (
                replace(previous, pending_far_symbol=None, pending_far_expiry_at=None,
                        confirmation_count=0),
                "unavailable", "no_synchronized_pair_this_observation",
            )
        return (
            PairSelectionState(near_symbol=previous.near_symbol if previous else ""),
            "unavailable",
            "no_candidate_pairs",
        )

    best = ranked[0]
    if (
        previous is not None
        and previous.near_symbol
        and previous.near_symbol != best.near_symbol
    ):
        # A near roll resets pairing regardless of the far state; the new
        # far needs two fresh confirmations (§五.12).
        return (
            PairSelectionState(
                near_symbol=best.near_symbol,
                pending_far_symbol=best.far_symbol,
                pending_far_expiry_at=best.far_expiry_at,
                confirmation_count=1,
            ),
            "unavailable",
            "near_roll_reset_pair_confirmation",
        )
    if previous is None or previous.far_symbol is None:
        # Continuing a pending confirmation: two consecutive selections of
        # the same candidate establish the pair (§五.12).
        if (
            previous is not None
            and previous.pending_far_symbol == best.far_symbol
            and previous.confirmation_count + 1 >= 2
        ):
            return (
                PairSelectionState(
                    near_symbol=best.near_symbol,
                    far_symbol=best.far_symbol,
                    far_expiry_at=best.far_expiry_at,
                ),
                "ready",
                "pair_confirmed_after_two_observations",
            )
        return (
            PairSelectionState(
                near_symbol=best.near_symbol,
                pending_far_symbol=best.far_symbol,
                pending_far_expiry_at=best.far_expiry_at,
                confirmation_count=1,
            ),
            "unavailable",
            "awaiting_second_confirmation",
        )

    if previous.far_symbol == best.far_symbol:
        return (
            replace(previous, pending_far_symbol=None, pending_far_expiry_at=None,
                    confirmation_count=0),
            "ready",
            None,
        )

    # A different best candidate: sticky far unless confirmed twice.
    pending = previous.pending_far_symbol
    if pending == best.far_symbol and previous.confirmation_count + 1 >= 2:
        return (
            PairSelectionState(
                near_symbol=best.near_symbol,
                far_symbol=best.far_symbol,
                far_expiry_at=best.far_expiry_at,
            ),
            "ready",
            "pair_changed_after_two_confirmations",
        )
    return (
        PairSelectionState(
            near_symbol=best.near_symbol,
            far_symbol=previous.far_symbol,
            far_expiry_at=previous.far_expiry_at,
            pending_far_symbol=best.far_symbol,
            pending_far_expiry_at=best.far_expiry_at,
            confirmation_count=previous.confirmation_count + 1
            if pending == best.far_symbol
            else 1,
        ),
        "unavailable" if previous.far_symbol is None else "ready",
        "far_switch_pending_confirmation",
    )


# ---------------------------------------------------------------------------
# Normalized execution-readiness gate (§七)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionInputs:
    """Field-level evidence bundle for one (product, contract, day, action)."""

    identity: FieldEvidence
    bar_validity: FieldEvidence
    bar_history: FieldEvidence
    trading_day_source: FieldEvidence
    sessions: FieldEvidence
    bar_availability: FieldEvidence
    parameter_availability: FieldEvidence
    requirements: FieldEvidence
    multiplier: FieldEvidence
    currency: FieldEvidence
    price_tick: FieldEvidence
    lot_rules: FieldEvidence
    expiry: FieldEvidence
    lifecycle: FieldEvidence
    fees: FieldEvidence
    margin: FieldEvidence
    settlement: FieldEvidence
    limits: FieldEvidence
    capacity: FieldEvidence
    pair_sync: FieldEvidence
    nav_risk: FieldEvidence


#: §七.2 gate matrix. Keys are FieldEvidence attribute names; values are
#: "R" (required, blocks), "NA" (not applicable with basis) or "NR"
#: (not requested for this action). Conditional rows (settlement/limits
#: depending on formula basis, capacity on close) are evaluated in
#: ``evaluate_readiness`` after the static pass.
_GATE_MATRIX: Mapping[ReadinessAction, Mapping[str, str]] = {
    ReadinessAction.OBSERVE: {
        "identity": "NA", "bar_validity": "R", "bar_history": "NR",
        "trading_day_source": "NR", "sessions": "NR", "bar_availability": "NR",
        "parameter_availability": "NR", "requirements": "NR",
        "multiplier": "NR", "currency": "NR", "price_tick": "NR",
        "lot_rules": "NR", "expiry": "NR", "lifecycle": "NR", "fees": "NR",
        "margin": "NR", "settlement": "NR", "limits": "NR", "capacity": "NR",
        "pair_sync": "NR", "nav_risk": "NR",
    },
    ReadinessAction.SIGNAL: {
        "identity": "R", "bar_validity": "R", "bar_history": "R",
        "trading_day_source": "R", "sessions": "R", "bar_availability": "R",
        "parameter_availability": "R", "requirements": "R",
        "multiplier": "NA", "currency": "NA", "price_tick": "NA",
        "lot_rules": "NA", "expiry": "R", "lifecycle": "NA", "fees": "NA",
        "margin": "NA", "settlement": "NA", "limits": "NA", "capacity": "NA",
        "pair_sync": "NA", "nav_risk": "NA",
    },
    ReadinessAction.OPEN_LONG: {
        "identity": "R", "bar_validity": "R", "bar_history": "R",
        "trading_day_source": "R", "sessions": "R", "bar_availability": "R",
        "parameter_availability": "R", "requirements": "R",
        "multiplier": "R", "currency": "R", "price_tick": "R",
        "lot_rules": "R", "expiry": "R", "lifecycle": "R", "fees": "R",
        "margin": "R", "settlement": "NA", "limits": "R", "capacity": "R",
        "pair_sync": "NA", "nav_risk": "R",
    },
    ReadinessAction.OPEN_SHORT: {
        "identity": "R", "bar_validity": "R", "bar_history": "R",
        "trading_day_source": "R", "sessions": "R", "bar_availability": "R",
        "parameter_availability": "R", "requirements": "R",
        "multiplier": "R", "currency": "R", "price_tick": "R",
        "lot_rules": "R", "expiry": "R", "lifecycle": "R", "fees": "R",
        "margin": "R", "settlement": "NA", "limits": "R", "capacity": "R",
        "pair_sync": "NA", "nav_risk": "R",
    },
    ReadinessAction.CLOSE_TODAY: {
        "identity": "R", "bar_validity": "R", "bar_history": "NA",
        "trading_day_source": "R", "sessions": "R", "bar_availability": "R",
        "parameter_availability": "NA", "requirements": "NA",
        "multiplier": "R", "currency": "R", "price_tick": "R",
        "lot_rules": "R", "expiry": "R", "lifecycle": "R", "fees": "R",
        "margin": "NA", "settlement": "NA", "limits": "R", "capacity": "R",
        "pair_sync": "NA", "nav_risk": "NA",
    },
    ReadinessAction.CLOSE_HISTORY: {
        "identity": "R", "bar_validity": "R", "bar_history": "NA",
        "trading_day_source": "NA", "sessions": "R", "bar_availability": "R",
        "parameter_availability": "NA", "requirements": "NA",
        "multiplier": "R", "currency": "R", "price_tick": "R",
        "lot_rules": "R", "expiry": "R", "lifecycle": "R", "fees": "R",
        "margin": "NA", "settlement": "NA", "limits": "R", "capacity": "R",
        "pair_sync": "NA", "nav_risk": "NA",
    },
    ReadinessAction.VALUE: {
        "identity": "R", "bar_validity": "R", "bar_history": "NA",
        "trading_day_source": "R", "sessions": "R", "bar_availability": "R",
        "parameter_availability": "NA", "requirements": "NA",
        "multiplier": "R", "currency": "R", "price_tick": "NA",
        "lot_rules": "NA", "expiry": "R", "lifecycle": "R", "fees": "R",
        "margin": "R", "settlement": "NA", "limits": "NA", "capacity": "NA",
        "pair_sync": "NA", "nav_risk": "R",
    },
}


def evaluate_readiness(
    action: ReadinessAction,
    product: ProductKey,
    contract_instance_id: str,
    event_time: datetime,
    inputs: ActionInputs,
    trading_day: date | None = None,
    margin_formula_uses_settlement: bool = False,
    limit_formula_uses_settlement: bool = False,
) -> GateResult:
    """Evaluate G(c,d,a,t) for one leaf action (§七.3).

    Only ``ready`` or ``blocked`` is returned; numeric constraints such as
    price-at-limit or insufficient lots are fill outcomes, never readiness.
    """
    matrix = _GATE_MATRIX[action]
    missing: list[str] = []
    invalid: list[str] = []
    reasons: list[str] = []
    evidence: list[str] = []

    for attr, necessity in matrix.items():
        evidence_field: FieldEvidence = getattr(inputs, attr)
        if evidence_field.evidence_id:
            evidence.append(evidence_field.evidence_id)
        if necessity == "NR":
            continue
        if necessity == "NA":
            continue
        if evidence_field.status == "not_applicable":
            continue  # skipped with recorded basis
        if evidence_field.status in ("missing", "unknown"):
            missing.append(attr)
            reasons.append(f"{attr}:{evidence_field.status}:{evidence_field.reason or 'no_reason'}")
            continue
        if evidence_field.status == "invalid":
            invalid.append(attr)
            reasons.append(f"{attr}:invalid:{evidence_field.reason or 'no_reason'}")
            continue
        if evidence_field.status in ("present_reference_only", "present_unapproved"):
            missing.append(attr)
            reasons.append(f"{attr}:{evidence_field.status}:insufficient_source_grade")
            continue

    # Conditional rows (§七.2): settlement is required when the margin or
    # limit formulas reference it; capacity applies to close legs too.
    if action in (ReadinessAction.OPEN_LONG, ReadinessAction.OPEN_SHORT, ReadinessAction.CLOSE_TODAY, ReadinessAction.CLOSE_HISTORY):
        needs_settlement = (
            action in (ReadinessAction.OPEN_LONG, ReadinessAction.OPEN_SHORT)
            and margin_formula_uses_settlement
        ) or (action in (ReadinessAction.CLOSE_TODAY, ReadinessAction.CLOSE_HISTORY) and limit_formula_uses_settlement)
        if needs_settlement and inputs.settlement.status != "not_applicable":
            if inputs.settlement.blocks() or inputs.settlement.status != "present_approved":
                missing.append("settlement")
                reasons.append("settlement:required_by_formula_basis")

    status = "ready" if not missing and not invalid else "blocked"
    return GateResult(
        action=action,
        product=product,
        contract_instance_id=contract_instance_id,
        trading_day=trading_day,
        event_time=event_time,
        status=status,
        missing_fields=tuple(sorted(set(missing))),
        invalid_fields=tuple(sorted(set(invalid))),
        reasons=tuple(sorted(set(reasons))),
        evidence_ids=tuple(sorted(set(evidence))),
        checked_at_event_time=event_time,
    )


def aggregate_readiness(
    results: Sequence[GateResult],
) -> Mapping[str, object]:
    """Aggregate leaf results per §七.6 (ready / partial / blocked)."""
    if not results:
        return {
            "status": "blocked",
            "ready_count": 0,
            "blocked_count": 0,
            "denominator": "requested_leaf_actions",
            "reason": "no_leaf_actions",
        }
    ready = sum(1 for r in results if r.status == "ready")
    blocked = len(results) - ready
    if ready == len(results):
        status = "ready"
    elif ready > 0:
        status = "partial"
    else:
        status = "blocked"
    return {
        "status": status,
        "ready_count": ready,
        "blocked_count": blocked,
        "denominator": "requested_leaf_actions",
    }


# ---------------------------------------------------------------------------
# Catalog adapter (read-only, graded)
# ---------------------------------------------------------------------------


def load_catalog_products(path: str | Path) -> Mapping[ProductKey, str]:
    """Map catalog futures products to status; review_required by default."""
    document = json.loads(Path(path).read_bytes().decode("utf-8"))
    products: dict[ProductKey, str] = {}
    for entry in document.get("products") or []:
        if str(entry.get("instrument_kind", "")).lower() != "future":
            continue
        key = ProductKey(
            exchange=str(entry["exchange"]).upper(),
            product=str(entry["code"]).upper(),
        )
        products[key] = str(entry.get("status", "review_required"))
    return products
