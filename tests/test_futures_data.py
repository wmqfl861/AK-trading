"""S1 focused tests: identity, cache, calendar/D, observation grid, roll/pair
selection and the normalized readiness gate (finalized-plan §十二.11,
minimum 16 cases)."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ak_trading.futures import data as futures_data
from ak_trading.futures.data import (
    ActionInputs,
    DaySequence,
    PairCandidate,
    ProductCalendarEvidence,
    SessionWindows,
    aggregate_readiness,
    build_day_sequence,
    build_observation_grid,
    day_sequence_digest,
    evaluate_readiness,
    exit_preferred_before_cutoff,
    load_universe_cache,
    pair_gap_days,
    rank_pair_candidates,
    rank_roll_candidates,
    roll_open_eligibility,
    roll_switch_confirmed,
    synchronized_far_bar,
    update_pair_selection,
)
from ak_trading.futures.spec import (
    ContractBar,
    ContractMetadata,
    FieldEvidence,
    PairSelectionState,
    ProductKey,
    ReadinessAction,
    ResearchPolicy,
    parse_product_token,
)

SH = timezone(timedelta(hours=8))


def make_bar(
    index: int,
    close: str,
    *,
    symbol: str = "SHFE.rb2609",
    high: str | None = None,
    low: str | None = None,
    volume: str | None = "1000",
    day: date | None = None,
    nano: int | None = None,
) -> ContractBar:
    close_dec = Decimal(close)
    high_dec = Decimal(high) if high else close_dec + Decimal("5")
    low_dec = Decimal(low) if low else close_dec - Decimal("5")
    session_close = datetime.combine(
        day or date(2026, 8, 1) + timedelta(days=index),
        datetime.min.time(),
        tzinfo=SH,
    ) + timedelta(hours=15)
    return ContractBar(
        symbol=symbol,
        duration_seconds=86400,
        datetime_nano=nano if nano is not None else int(session_close.timestamp()) * 1_000_000_000,
        ts=session_close - timedelta(hours=1),
        source="tqsdk",
        trading_day=day or date(2026, 8, 1) + timedelta(days=index),
        trading_day_source="tqsdk",
        session_open=session_close - timedelta(hours=6),
        session_close=session_close,
        available_at=session_close,
        open=close_dec,
        high=high_dec,
        low=low_dec,
        close=close_dec,
        volume=Decimal(volume) if volume is not None else None,
    )


# ---------------------------------------------------------------------------
# ProductKey / tokens
# ---------------------------------------------------------------------------


def test_product_token_normalizes_exchange_and_product_case() -> None:
    assert parse_product_token("shfe.rb") == ProductKey("SHFE", "RB")
    assert str(parse_product_token("SHFE.rb")) == "SHFE.RB"
    assert parse_product_token("SHFE.rb") == parse_product_token("shfe.RB")


def test_product_token_rejects_unknown_exchange_and_non_letters() -> None:
    with pytest.raises(ValueError, match="unknown exchange"):
        parse_product_token("NYSE.rb")
    with pytest.raises(ValueError, match="ASCII letters"):
        parse_product_token("SHFE.rb2609")
    with pytest.raises(ValueError, match="EXCHANGE.product"):
        parse_product_token("SHFErb")


# ---------------------------------------------------------------------------
# Universe cache / contract instances
# ---------------------------------------------------------------------------


@pytest.fixture()
def small_universe_cache(tmp_path):
    payload = {
        "generated_at": "2026-09-01T14:58:47+08:00",
        "products": {
            "SHFE.rb": [
                {"symbol": "SHFE.rb2009", "expire_datetime": 1600153200, "expired": True},
                {"symbol": "SHFE.rb2609", "expire_datetime": 1789455600, "expired": False},
            ],
            "CZCE.TA": [
                {"symbol": "CZCE.TA010", "expire_datetime": 1603350000, "expired": True},
                {"symbol": "CZCE.TA609", "expire_datetime": 1789369200, "expired": False},
            ],
            "GFEX.si": [
                {"symbol": "GFEX.si2609", "expire_datetime": 1789369200, "expired": False},
            ],
        },
    }
    path = tmp_path / "universe.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_universe_cache_resolves_products_case_insensitively_and_keeps_raw_symbols(
    small_universe_cache,
) -> None:
    cache = load_universe_cache(small_universe_cache)
    rb = futures_data.resolve_product_contracts(cache, ProductKey("SHFE", "RB"))
    assert [instance.raw_symbol for instance in rb] == ["SHFE.rb2009", "SHFE.rb2609"]
    ta = futures_data.resolve_product_contracts(cache, ProductKey("CZCE", "TA"))
    assert [instance.raw_symbol for instance in ta] == ["CZCE.TA010", "CZCE.TA609"]
    si = futures_data.resolve_product_contracts(cache, ProductKey("GFEX", "SI"))
    assert [instance.raw_symbol for instance in si] == ["GFEX.si2609"]


def test_expiry_seconds_become_shanghai_datetimes_in_instance_ids(
    small_universe_cache,
) -> None:
    cache = load_universe_cache(small_universe_cache)
    rb = futures_data.resolve_product_contracts(cache, ProductKey("SHFE", "RB"))[0]
    assert rb.expiry_at is not None
    assert rb.expiry_at.utcoffset() == timedelta(hours=8)
    assert rb.expiry_at.timestamp() == 1600153200
    assert rb.instance_id == f"SHFE.rb2009|{rb.expiry_at.isoformat()}"


def test_czce_three_digit_codes_keep_instance_identity_and_expiry(
    small_universe_cache,
) -> None:
    cache = load_universe_cache(small_universe_cache)
    ta = futures_data.resolve_product_contracts(cache, ProductKey("CZCE", "TA"))
    old, new = ta
    assert old.raw_symbol == "CZCE.TA010"
    assert old.expiry_at is not None and new.expiry_at is not None
    assert old.expiry_at < new.expiry_at
    assert old.instance_id != new.instance_id


def test_listing_unknown_limits_window_to_730_days_before_expiry(
    small_universe_cache,
) -> None:
    cache = load_universe_cache(small_universe_cache)
    # rb2009's fallback start precedes the request start: the request start
    # binds. rb2609 expires in 2026, so its fallback window binds instead.
    rb2009, rb2609 = futures_data.resolve_product_contracts(cache, ProductKey("SHFE", "RB"))
    request_start = datetime(2020, 7, 20, tzinfo=SH)
    window_start_old, flag_old = rb2009.research_window_start(request_start)
    assert flag_old == "listing_history_unknown"
    assert window_start_old == request_start
    window_start_new, flag_new = rb2609.research_window_start(request_start)
    assert flag_new == "listing_history_unknown"
    assert rb2609.expiry_at is not None
    assert window_start_new == rb2609.expiry_at - timedelta(days=730)


def test_contract_bar_rejects_wrong_duration_and_naive_ts() -> None:
    with pytest.raises(ValueError, match="duration"):
        ContractBar(
            symbol="SHFE.rb2609", duration_seconds=60, datetime_nano=1,
            ts=datetime(2026, 1, 1, tzinfo=SH), source="t",
        )
    with pytest.raises(ValueError, match="timezone"):
        ContractBar(
            symbol="SHFE.rb2609", duration_seconds=86400, datetime_nano=1,
            ts=datetime(2026, 1, 1), source="t",
        )


# ---------------------------------------------------------------------------
# calendar_union_v1
# ---------------------------------------------------------------------------


def _evidence(
    product: ProductKey,
    start: date,
    end: date,
    closed: set[date],
    *,
    listed_from: date | None = None,
) -> ProductCalendarEvidence:
    # Weekends count as explicitly closed evidence: within the declared
    # validity range a day neither open nor closed would be unknown (§五.6.b).
    all_closed = set(closed)
    cursor = start
    while cursor <= end:
        if cursor.weekday() >= 5:
            all_closed.add(cursor)
        cursor += timedelta(days=1)
    open_days: dict[date, SessionWindows] = {}
    cursor = start
    while cursor <= end:
        if cursor not in all_closed:
            session_close = datetime.combine(cursor, datetime.min.time(), tzinfo=SH) + timedelta(hours=15)
            open_days[cursor] = SessionWindows(
                windows=((session_close - timedelta(hours=6), session_close),)
            )
        cursor += timedelta(days=1)
    return ProductCalendarEvidence(
        product=product,
        valid_from=start,
        valid_through=end,
        open_days=open_days,
        closed_days=frozenset(all_closed),
        listed_from=listed_from,
        source_status="approved",
        evidence_id=f"ev-{product}",
        evidence_sha256=f"hash-{product}",
    )


def test_day_sequence_digest_uses_lf_including_last_line() -> None:
    import hashlib

    days = [date(2026, 1, 5), date(2026, 1, 6)]
    expected = hashlib.sha256(b"2026-01-05\n2026-01-06\n").hexdigest()
    assert day_sequence_digest(days) == expected
    empty = hashlib.sha256(b"").hexdigest()
    assert day_sequence_digest([]) == empty


def test_day_sequence_unavailable_when_any_day_unknown() -> None:
    start, end = date(2026, 7, 1), date(2026, 7, 10)
    # Coverage gap: DCE evidence starts later -> unknown days for DCE.M.
    rb = _evidence(ProductKey("SHFE", "RB"), start, end, set())
    m_late = _evidence(ProductKey("DCE", "M"), date(2026, 7, 6), end, set())
    seq = build_day_sequence(
        datetime(2026, 7, 1, tzinfo=SH),
        datetime(2026, 7, 11, tzinfo=SH),
        datetime(2026, 7, 11, tzinfo=SH),
        {ProductKey("SHFE", "RB"): rb, ProductKey("DCE", "M"): m_late},
        universe=(ProductKey("SHFE", "RB"), ProductKey("DCE", "M")),
    )
    assert isinstance(seq, DaySequence)
    assert seq.status == "unavailable"
    assert seq.dates == ()
    assert seq.calendar_missing_ranges
    assert seq.day_sequence_sha256 == day_sequence_digest([])


def test_day_sequence_d_excludes_all_closed_days_and_unfinished_final_session() -> None:
    start, end = date(2026, 7, 1), date(2026, 7, 10)
    holiday = {date(2026, 7, 3)}
    rb = _evidence(ProductKey("SHFE", "RB"), start, end, holiday)
    # as_of exactly at the last session close: the final day still qualifies
    # (session_close <= as_of); one second earlier it must not.
    last_close = rb.open_days[date(2026, 7, 10)].final_close()
    seq_included = build_day_sequence(
        datetime(2026, 7, 1, tzinfo=SH), datetime(2026, 7, 11, tzinfo=SH),
        last_close, {ProductKey("SHFE", "RB"): rb},
        universe=(ProductKey("SHFE", "RB"),),
    )
    assert seq_included.status == "available"
    assert date(2026, 7, 3) not in seq_included.dates
    assert seq_included.dates[-1] == date(2026, 7, 10)
    assert all(day.weekday() < 5 for day in seq_included.dates)

    seq_excluded = build_day_sequence(
        datetime(2026, 7, 1, tzinfo=SH), datetime(2026, 7, 11, tzinfo=SH),
        last_close - timedelta(seconds=1), {ProductKey("SHFE", "RB"): rb},
        universe=(ProductKey("SHFE", "RB"),),
    )
    assert seq_excluded.status == "available"
    assert seq_excluded.dates[-1] == date(2026, 7, 9)


def test_day_sequence_uses_only_calendar_evidence_not_bars_or_eligibility() -> None:
    # D is built purely from frozen calendar evidence: adding not_yet_listed
    # products before their listing keeps those days in D via other products,
    # and delisted products contribute no further open days.
    start, end = date(2026, 7, 1), date(2026, 7, 10)
    rb = _evidence(ProductKey("SHFE", "RB"), start, end, set())
    si = _evidence(ProductKey("GFEX", "SI"), start, end, set(), listed_from=date(2026, 7, 6))
    seq = build_day_sequence(
        datetime(2026, 7, 1, tzinfo=SH), datetime(2026, 7, 11, tzinfo=SH),
        datetime(2026, 7, 12, tzinfo=SH),
        {ProductKey("SHFE", "RB"): rb, ProductKey("GFEX", "SI"): si},
        universe=(ProductKey("SHFE", "RB"), ProductKey("GFEX", "SI")),
    )
    assert seq.status == "available"
    assert date(2026, 7, 1) in seq.dates  # RB open even though SI not listed
    assert seq.per_product_summary["GFEX.SI"] == "not_yet_listed|open"
    # Construction hash is stable across repeated builds with equal evidence.
    seq_again = build_day_sequence(
        datetime(2026, 7, 1, tzinfo=SH), datetime(2026, 7, 11, tzinfo=SH),
        datetime(2026, 7, 12, tzinfo=SH),
        {ProductKey("SHFE", "RB"): rb, ProductKey("GFEX", "SI"): si},
        universe=(ProductKey("SHFE", "RB"), ProductKey("GFEX", "SI")),
    )
    assert seq.construction_sha256 == seq_again.construction_sha256
    assert seq.day_sequence_sha256 == seq_again.day_sequence_sha256


# ---------------------------------------------------------------------------
# Observation grid
# ---------------------------------------------------------------------------


def test_observation_grid_sorts_by_instance_and_nano_and_keeps_gaps() -> None:
    instances = [
        futures_data.ContractInstance(
            instance_id="SHFE.rb2609|2070-01-01T00:00:00+08:00",
            raw_symbol="SHFE.rb2609",
            product=ProductKey("SHFE", "RB"),
            expiry_at=datetime(2070, 1, 1, tzinfo=SH),
            expired_in_cache=False,
        ),
        futures_data.ContractInstance(
            instance_id="SHFE.rb2605|2069-09-01T00:00:00+08:00",
            raw_symbol="SHFE.rb2605",
            product=ProductKey("SHFE", "RB"),
            expiry_at=datetime(2069, 9, 1, tzinfo=SH),
            expired_in_cache=False,
        ),
    ]
    bars_a = [make_bar(i, "10", day=date(2026, 8, 3 + i)) for i in (2, 0, 1)]
    bars_b = [make_bar(0, "11", symbol="SHFE.rb2605", day=date(2026, 8, 4))]
    grid = build_observation_grid(
        instances,
        {"SHFE.rb2609|2070-01-01T00:00:00+08:00": bars_a,
         "SHFE.rb2605|2069-09-01T00:00:00+08:00": bars_b},
    )
    assert [row.raw_symbol for row in grid] == ["SHFE.rb2605"] + ["SHFE.rb2609"] * 3
    nanos = [row.bar.datetime_nano for row in grid if row.raw_symbol == "SHFE.rb2609"]
    assert nanos == sorted(nanos)
    # A gap day (no bar on 2026-08-05) is preserved, not filled.
    days = [row.bar.trading_day for row in grid if row.raw_symbol == "SHFE.rb2609"]
    assert days == [date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5)]


# ---------------------------------------------------------------------------
# Roll policy
# ---------------------------------------------------------------------------


def _instance(symbol: str, expiry: datetime) -> futures_data.ContractInstance:
    return futures_data.ContractInstance(
        instance_id=f"{symbol}|{expiry.isoformat()}",
        raw_symbol=symbol,
        product=ProductKey("SHFE", "RB"),
        expiry_at=expiry,
        expired_in_cache=False,
    )


def test_roll_ranking_volume_desc_then_expiry_asc_then_symbol() -> None:
    decision = datetime(2026, 8, 1, tzinfo=SH)
    candidates = [
        futures_data.RollCandidate(instance=_instance("SHFE.rb2701", decision + timedelta(days=150)), median_volume20=500, volume_window_rows=20),
        futures_data.RollCandidate(instance=_instance("SHFE.rb2612", decision + timedelta(days=130)), median_volume20=900, volume_window_rows=20),
        futures_data.RollCandidate(instance=_instance("SHFE.rb2611", decision + timedelta(days=100)), median_volume20=900, volume_window_rows=20),
    ]
    ranked = rank_roll_candidates(candidates, decision, ResearchPolicy())
    assert [c.instance.raw_symbol for c in ranked] == [
        "SHFE.rb2611", "SHFE.rb2612", "SHFE.rb2701",
    ]


def test_roll_open_eligibility_enforces_expiry_buffer_and_cutoffs() -> None:
    policy = ResearchPolicy()
    decision = datetime(2026, 8, 1, tzinfo=SH)
    near_expiry = _instance("SHFE.rb2609", decision + timedelta(days=19))
    ok, reason = roll_open_eligibility(near_expiry, decision, policy)
    assert not ok and reason == "within_expiry_buffer"
    good = _instance("SHFE.rb2701", decision + timedelta(days=120))
    assert roll_open_eligibility(good, decision, policy) == (True, None)
    metadata = ContractMetadata(
        contract_instance_id=good.instance_id, raw_symbol=good.raw_symbol,
        product=good.product, expiry_at=good.expiry_at,
        last_trade_at=decision - timedelta(days=1),
    )
    blocked, why = roll_open_eligibility(good, decision, policy, metadata)
    assert not blocked and why == "past_trading_cutoff"


def test_roll_switch_needs_volume_ratio_and_two_confirmations_logic() -> None:
    policy = ResearchPolicy()
    assert roll_switch_confirmed(100.0, 119.9, policy) == (False, "volume_below_ratio")
    confirmed, _ = roll_switch_confirmed(100.0, 120.0, policy)
    assert confirmed
    assert roll_switch_confirmed(None, 500.0, policy) == (False, "volume_unavailable")


def test_exit_preferred_with_five_or_fewer_confirmed_sessions() -> None:
    policy = ResearchPolicy()
    assert exit_preferred_before_cutoff(5, policy)[0] is True
    assert exit_preferred_before_cutoff(6, policy)[0] is False
    assert exit_preferred_before_cutoff(None, policy) == (False, "session_evidence_unknown")


# ---------------------------------------------------------------------------
# Carry pair selection
# ---------------------------------------------------------------------------


def _pair(far_symbol: str, gap_days: float, volume: float) -> PairCandidate:
    near_expiry = datetime(2026, 9, 14, tzinfo=SH)
    far_expiry = near_expiry + timedelta(days=gap_days)
    near_bar = make_bar(0, "3000", day=date(2026, 8, 1))
    far_bar = make_bar(0, "2980", symbol=far_symbol, day=date(2026, 8, 1))
    return PairCandidate(
        near_symbol="SHFE.rb2609",
        far_symbol=far_symbol,
        near_expiry_at=near_expiry,
        far_expiry_at=far_expiry,
        near_bar=near_bar,
        far_bar=far_bar,
        far_median_volume20=volume,
    )


def test_pair_ranking_prefers_gap_near_90_then_volume() -> None:
    ranked = rank_pair_candidates(
        [_pair("SHFE.rb2701", 150, 5000), _pair("SHFE.rb2612", 90, 100), _pair("SHFE.rb2705", 91, 4000)],
        ResearchPolicy(),
    )
    assert [c.far_symbol for c in ranked] == ["SHFE.rb2612", "SHFE.rb2705", "SHFE.rb2701"]


def test_pair_gap_out_of_60_180_range_is_filtered() -> None:
    ranked = rank_pair_candidates(
        [_pair("SHFE.rb2610", 30, 9999), _pair("SHFE.rb2706", 200, 9999), _pair("SHFE.rb2701", 120, 10)],
        ResearchPolicy(),
    )
    assert [c.far_symbol for c in ranked] == ["SHFE.rb2701"]


def test_synchronized_far_bar_requires_identical_nano_same_day() -> None:
    near = make_bar(0, "3000", day=date(2026, 8, 3))
    same = make_bar(0, "2980", symbol="SHFE.rb2701", day=date(2026, 8, 3))
    shifted_nano = make_bar(
        0, "2980", symbol="SHFE.rb2701", day=date(2026, 8, 3),
        nano=near.datetime_nano + 1_000_000,
    )
    far_bars = [shifted_nano, same]
    assert synchronized_far_bar(near, far_bars) is same
    assert synchronized_far_bar(near, [shifted_nano]) is None
    # No forward fill: a later far bar never substitutes a missing sync row.
    next_day = make_bar(0, "2980", symbol="SHFE.rb2701", day=date(2026, 8, 4))
    assert synchronized_far_bar(near, [next_day]) is None


def test_pair_selection_two_confirmation_then_sticky_then_switch() -> None:
    policy = ResearchPolicy()
    # Observation 1: first candidate becomes pending, carry unavailable.
    state1, status1, _ = update_pair_selection(None, [_pair("SHFE.rb2701", 120, 500)], policy)
    assert status1 == "unavailable"
    assert state1.pending_far_symbol == "SHFE.rb2701" and state1.confirmation_count == 1
    # Observation 2: same candidate again -> confirmed, ready.
    state2, status2, _ = update_pair_selection(state1, [_pair("SHFE.rb2701", 120, 500)], policy)
    assert status2 == "ready"
    assert state2.far_symbol == "SHFE.rb2701"
    # Observation 3: a different best candidate appears once -> sticky.
    state3, status3, _ = update_pair_selection(
        state2,
        [_pair("SHFE.rb2701", 120, 500), _pair("SHFE.rb2705", 90, 900)],
        policy,
    )
    assert status3 == "ready"
    assert state3.far_symbol == "SHFE.rb2701"
    assert state3.pending_far_symbol == "SHFE.rb2705"
    # Observation 4: second consecutive selection -> switch.
    state4, status4, _ = update_pair_selection(
        state3,
        [_pair("SHFE.rb2701", 120, 500), _pair("SHFE.rb2705", 90, 900)],
        policy,
    )
    assert status4 == "ready"
    assert state4.far_symbol == "SHFE.rb2705"


def test_pair_selection_near_roll_resets_confirmation() -> None:
    policy = ResearchPolicy()
    state, status, _ = update_pair_selection(None, [_pair("SHFE.rb2701", 120, 500)], policy)
    state, status, _ = update_pair_selection(state, [_pair("SHFE.rb2701", 120, 500)], policy)
    assert status == "ready"
    rolled = replace_far(state, near="SHFE.rb2701")
    state_after, status_after, reason = update_pair_selection(
        rolled, [_pair("SHFE.rb2706", 120, 500)], policy
    )
    assert status_after == "unavailable"
    assert reason == "near_roll_reset_pair_confirmation"
    assert state_after.confirmation_count == 1


def replace_far(state: PairSelectionState, *, near: str) -> PairSelectionState:
    from dataclasses import replace

    return replace(state, near_symbol=near, far_symbol=None, far_expiry_at=None,
                   pending_far_symbol=None, pending_far_expiry_at=None,
                   confirmation_count=0)


def test_pair_gap_days_uses_seconds_precision() -> None:
    near = datetime(2026, 9, 14, tzinfo=SH)
    far = near + timedelta(days=90, hours=12)
    assert pair_gap_days(near, far) == pytest.approx(90.5)


# ---------------------------------------------------------------------------
# Normalized gate
# ---------------------------------------------------------------------------


def _inputs(**overrides) -> ActionInputs:
    defaults = {
        name: FieldEvidence(name=name, status="present_approved")
        for name in (
            "identity", "bar_validity", "bar_history", "trading_day_source",
            "sessions", "bar_availability", "parameter_availability",
            "requirements", "multiplier", "currency", "price_tick",
            "lot_rules", "expiry", "lifecycle", "fees", "margin",
            "settlement", "limits", "capacity", "pair_sync", "nav_risk",
        )
    }
    for name, status in overrides.items():
        defaults[name] = FieldEvidence(name=name, status=status)
    return ActionInputs(**defaults)


def test_gate_signal_ready_with_minimal_contract_fields() -> None:
    result = evaluate_readiness(
        ReadinessAction.SIGNAL, ProductKey("SHFE", "RB"), "SHFE.rb2609|x",
        datetime(2026, 8, 3, tzinfo=SH), _inputs(), trading_day=date(2026, 8, 3),
    )
    assert result.status == "ready"
    assert result.missing_fields == () and result.invalid_fields == ()


def test_gate_open_blocks_on_reference_only_multiplier_and_missing_limits() -> None:
    inputs = _inputs(multiplier="present_reference_only", limits="missing")
    result = evaluate_readiness(
        ReadinessAction.OPEN_LONG, ProductKey("SHFE", "RB"), "SHFE.rb2609|x",
        datetime(2026, 8, 3, tzinfo=SH), inputs,
    )
    assert result.status == "blocked"
    assert "multiplier" in result.missing_fields
    assert "limits" in result.missing_fields
    assert any("insufficient_source_grade" in reason for reason in result.reasons)


def test_gate_close_independent_of_open_only_fields() -> None:
    inputs = _inputs(margin="missing", nav_risk="missing", bar_history="missing")
    result = evaluate_readiness(
        ReadinessAction.CLOSE_HISTORY, ProductKey("SHFE", "RB"), "SHFE.rb2609|x",
        datetime(2026, 8, 3, tzinfo=SH), inputs,
    )
    assert result.status == "ready"


def test_gate_value_blocks_when_margin_unknown_but_not_on_tick() -> None:
    inputs = _inputs(margin="unknown", price_tick="missing")
    result = evaluate_readiness(
        ReadinessAction.VALUE, ProductKey("SHFE", "RB"), "SHFE.rb2609|x",
        datetime(2026, 8, 3, tzinfo=SH), inputs,
    )
    assert result.status == "blocked"
    assert "margin" in result.missing_fields
    assert "price_tick" not in result.missing_fields


def test_gate_settlement_conditional_on_formula_basis() -> None:
    event = datetime(2026, 8, 3, tzinfo=SH)
    inputs = _inputs(settlement="missing", margin="present_approved", limits="present_approved")
    base = evaluate_readiness(
        ReadinessAction.OPEN_LONG, ProductKey("SHFE", "RB"), "c", event, inputs,
        margin_formula_uses_settlement=False,
    )
    assert base.status == "ready"
    with_formula = evaluate_readiness(
        ReadinessAction.OPEN_LONG, ProductKey("SHFE", "RB"), "c", event, inputs,
        margin_formula_uses_settlement=True,
    )
    assert with_formula.status == "blocked"
    assert "settlement" in with_formula.missing_fields


def test_gate_aggregate_partial_and_not_requested_distinct() -> None:
    ready = evaluate_readiness(
        ReadinessAction.SIGNAL, ProductKey("SHFE", "RB"), "c",
        datetime(2026, 8, 3, tzinfo=SH), _inputs(),
    )
    blocked = evaluate_readiness(
        ReadinessAction.OPEN_LONG, ProductKey("SHFE", "RB"), "c",
        datetime(2026, 8, 3, tzinfo=SH), _inputs(multiplier="missing"),
    )
    aggregate = aggregate_readiness([ready, blocked])
    assert aggregate["status"] == "partial"
    assert aggregate["ready_count"] == 1 and aggregate["blocked_count"] == 1
    assert aggregate_readiness([])["status"] == "blocked"
    not_requested = FieldEvidence(name="member_factor", status="not_requested")
    assert not_requested.blocks() is False


def test_synchronized_far_bar_requires_compatible_source_family_a4() -> None:
    near = make_bar(0, "3000", day=date(2026, 8, 3))
    # Same family, per-contract labels: pairs fine.
    same_family = make_bar(
        0, "2980", symbol="SHFE.rb2701", day=date(2026, 8, 3),
    )
    same_family = _with_source(same_family, "tqsdk:SHFE.rb2701:86400:2024:2026")
    near_labeled = _with_source(near, "tqsdk:SHFE.rb2609:86400:2024:2026")
    assert synchronized_far_bar(near_labeled, [same_family]) is same_family
    # Different source family: nominally equal timestamps never pair.
    other_family = _with_source(same_family, "akshare:SHFE.rb2701")
    assert synchronized_far_bar(near_labeled, [other_family]) is None


def _with_source(bar: ContractBar, source: str) -> ContractBar:
    from dataclasses import replace

    return replace(bar, source=source)
