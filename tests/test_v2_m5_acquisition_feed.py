"""P05 acquisition feed preparation vectors (PREPARE_ONLY; SYNTHETIC only).

Work order: docs/devplan-nextphase-20260923-r1/work-orders/P05-minute-feed-prepare.md

Tier RUN_NOW (executed in this revision):
  * the freeze gate blocks adapter construction while the P02 feed-input
    contract is DRAFT_NOT_FROZEN (Codex freeze + R8 handover pending);
  * SYNTHETIC scenario vectors for the work order's offline-acceptance list are
    validated against the UNMODIFIED M5 consumer (FeedEvent/FeedMonitor/
    ReplayClock from ak_trading.v2.validation.shadow). The vectors prove the
    fixture corpus is well-formed and produces exactly the expected audit
    reasons; no consumer assertion is modified and nothing asserts the adapter
    exists.

Tier PENDING_* (skipped drafts): adapter-layer assertions that require the
frozen contract (or the R8 published handover). They become active only in the
post-freeze implementation revision.

Never asserted anywhere: SHADOW_ACCEPTED, W2 PASS, or production real-time
readiness. Every vector uses the M5 ReplayClock (SYNTHETIC), which by design can
never verify as a real receiver clock.
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from ak_trading.v2.contracts import FrozenInput, MarketBar
from ak_trading.v2.schemas import decode_shared_fixture
from ak_trading.v2.validation import acquisition_feed as af
from ak_trading.v2.validation.experiments import digest
from ak_trading.v2.validation.shadow import (
    FeedEvent, FeedMonitor, ReplayClock, SECOND, validate_clock_evidence,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
P02_DRAFT_CONTRACT = (
    REPO_ROOT / "docs/nextphase-evidence-20260923-r1/P02/r1/feed-input-contract.json"
)
SOURCE_ID = "SYNTHETIC_ACQ_P05"
PENDING_FREEZE_REASON = (
    "PENDING_FREEZE: P02 feed-input-contract is DRAFT_NOT_FROZEN; this "
    "adapter-layer draft activates only in the post-freeze implementation revision"
)
PENDING_R8_REASON = (
    "PENDING_R8_HANDOVER: execution-side feed consumption is defined only after "
    "the R8 responsibility chain provides a published handover"
)


def shared_fixture():
    path = Path(__file__).parent / "fixtures/v2/shared.json"
    decoded = decode_shared_fixture(path.read_bytes())
    bundle = FrozenInput(path.parent, digest(path.read_bytes().hex()), decoded["instruments"],
                         decoded["sessions"], decoded["bars"], decoded["macro_items"], decoded["rules"])
    return decoded, bundle


def session_of(bundle: FrozenInput, session_id: str):
    return next(s for s in bundle.sessions if s.session_id == session_id)


def next_night_after(bundle: FrozenInput, session):
    return next(s for s in bundle.sessions
                if s.kind == "NIGHT" and s.start_nano > session.end_nano)


def minute_bar(base: MarketBar, *, bar_id: str, session, offset_minutes: int = 0,
               **overrides) -> MarketBar:
    """A closed, complete 60s SYNTHETIC bar inside the given session window."""
    start = session.start_nano + offset_minutes * 60 * SECOND
    values = dict(
        bar_id=bar_id, start_nano=start, end_nano=start + 60 * SECOND, datetime_nano=start,
        session_id=session.session_id, trading_day=session.trading_day,
        available_at_nano=start + 60 * SECOND, imported_at_nano=start + 60 * SECOND,
        source=SOURCE_ID)
    values.update(overrides)
    return replace(base, **values)


def bar_event(sequence: int, bar: MarketBar) -> FeedEvent:
    # received_at_nano carries the source-reported receive instant (diagnostic
    # only); M5 validation time always comes from the independent ReceiverClock.
    return FeedEvent(f"acq-{sequence}", sequence, "BAR", bar.end_nano, bar.end_nano,
                     bar, (), SOURCE_ID)


# --------------------------------------------------------------------------
# Freeze gate (contract-independent preparation)
# --------------------------------------------------------------------------

def test_prepare_only_gate_blocks_on_current_p02_draft():
    document = af.load_contract_document(P02_DRAFT_CONTRACT)
    assert document["status"] == "DRAFT_NOT_FROZEN"
    assert af.contract_status(document) == "DRAFT_NOT_FROZEN"
    assert af.KNOWN_UNFROZEN_STATUSES == ("DRAFT_NOT_FROZEN",)
    assert not af.is_frozen_contract(document)
    with pytest.raises(af.ContractNotFrozen, match="MUST_BE_FROZEN_BY_CODEX"):
        af.build_minute_feed(document)


def test_gate_requires_frozen_status_and_non_draft_schema():
    frozen_shape = {"status": "FROZEN", "schema": "ak_feed_input_contract_example_v1"}
    assert af.is_frozen_contract(frozen_shape)
    af.require_frozen_contract(frozen_shape)  # gate itself opens, ...
    with pytest.raises(NotImplementedError, match="pending_freeze"):
        af.build_minute_feed(frozen_shape)    # ... but no implementation exists yet
    assert not af.is_frozen_contract({"status": "FROZEN", "schema": af.DRAFT_SCHEMA})
    assert not af.is_frozen_contract({"status": "DRAFT_NOT_FROZEN",
                                      "schema": "ak_feed_input_contract_example_v1"})
    with pytest.raises(ValueError, match="contract_status_missing"):
        af.contract_status({})
    with pytest.raises(ValueError, match="contract_document_must_be_mapping"):
        af.contract_status(["not-a-contract"])


def test_module_is_prepare_only_with_no_forbidden_surfaces():
    import types
    assert af.NODE_STATUS == "PREPARE_ONLY"
    assert af.FEED_READINESS == "NOT_ACCEPTED"
    # Exact public surface: nothing beyond the documented prepare surface can
    # appear, so no order/trade/connection/ingestion entry point can sneak in.
    expected_surface = {
        "NODE_STATUS", "FEED_READINESS", "IMPLEMENTATION_GATE", "R8_PENDING_MARKER",
        "DRAFT_SCHEMA", "FROZEN_STATUS", "KNOWN_UNFROZEN_STATUSES",
        "WORK_ORDER_SCENARIO_KEYS",  # names the dispatch document (工单), not orders
        "SCENARIO_STATUS", "ADDITIONAL_DRAFT_SCENARIOS", "ContractNotFrozen",
        "contract_status", "is_frozen_contract", "require_frozen_contract",
        "load_contract_document", "build_minute_feed",
        # stdlib helpers re-exported by import (whitelisted below as modules/types)
        "Any", "Path", "Mapping", "annotations", "json",
    }
    assert {name for name in dir(af) if not name.startswith("_")} == expected_surface
    forbidden = ("order", "trade", "tq", "engine", "connect", "sqlite", "postgres", "env")
    callables = [name for name in expected_surface if callable(getattr(af, name))]
    assert not [name for name in callables if any(f in name.lower() for f in forbidden)]
    imported = {name for name, value in vars(af).items() if isinstance(value, types.ModuleType)}
    assert imported <= {"json", "pathlib", "collections"}


# --------------------------------------------------------------------------
# RUN_NOW scenario vectors against the unmodified M5 consumer
# --------------------------------------------------------------------------

def test_vector_normal_closed_bar():
    decoded, bundle = shared_fixture()
    session = session_of(bundle, decoded["bars"][0].session_id)
    bar = minute_bar(decoded["bars"][0], bar_id="acq-normal-1", session=session)
    clock = ReplayClock(bar.end_nano + SECOND)
    row = FeedMonitor(bundle).receive(bar_event(1, bar), clock)
    assert row.reasons == ()
    assert row.visible_bar is not None and row.visible_bar.bar_id == bar.bar_id
    assert row.clock_verified is False  # SYNTHETIC receiver clock never verifies


def test_vector_duplicate_same_content_idempotent():
    decoded, bundle = shared_fixture()
    session = session_of(bundle, decoded["bars"][0].session_id)
    bar = minute_bar(decoded["bars"][0], bar_id="acq-dup-1", session=session)
    event = bar_event(1, bar)
    monitor = FeedMonitor(bundle)
    clock = ReplayClock(bar.end_nano + SECOND)
    first = monitor.receive(event, clock)
    assert first.reasons == ()
    second = monitor.receive(event, clock)
    assert "DUPLICATE" in second.reasons and "DUPLICATE_BAR" in second.reasons
    assert second.visible_bar is None
    # Idempotent absorb: identity, sequence and watermark state are unchanged.
    assert len(monitor.seen) == 1 and len(monitor.sequences) == 1
    assert monitor.watermark == 1 and monitor.seen[event.event_id] == digest(event)


def test_vector_same_sequence_different_content_conflict():
    decoded, bundle = shared_fixture()
    session = session_of(bundle, decoded["bars"][0].session_id)
    bar_a = minute_bar(decoded["bars"][0], bar_id="acq-seq-a", session=session,
                       close=Decimal("100"))
    bar_b = minute_bar(decoded["bars"][0], bar_id="acq-seq-b", session=session,
                       offset_minutes=1, close=Decimal("101"))
    monitor = FeedMonitor(bundle)
    clock = ReplayClock(bar_a.end_nano + SECOND)
    monitor.receive(bar_event(1, bar_a), clock)
    same_sequence = FeedEvent("acq-1b", 1, "BAR", bar_b.end_nano, bar_b.end_nano,
                              bar_b, (), SOURCE_ID)
    with pytest.raises(ValueError, match="conflicting_sequence_identity"):
        monitor.receive(same_sequence, clock)
    same_id_new_content = FeedEvent("acq-1", 2, "BAR", bar_b.end_nano, bar_b.end_nano,
                                    bar_b, (), SOURCE_ID)
    with pytest.raises(ValueError, match="conflicting_duplicate"):
        monitor.receive(same_id_new_content, clock)
    assert monitor.watermark == 1  # both conflicts rejected, nothing registered


def test_vector_conflicting_bar_revision_never_overwrites_published():
    decoded, bundle = shared_fixture()
    session = session_of(bundle, decoded["bars"][0].session_id)
    bar_a = minute_bar(decoded["bars"][0], bar_id="acq-rev-a", session=session,
                       close=Decimal("100"))
    bar_b = minute_bar(decoded["bars"][0], bar_id="acq-rev-b", session=session,
                       close=Decimal("101"))  # same (contract, start, duration) key
    monitor = FeedMonitor(bundle)
    clock = ReplayClock(bar_a.end_nano + SECOND)
    assert monitor.receive(bar_event(1, bar_a), clock).reasons == ()
    revision = monitor.receive(bar_event(2, bar_b), clock)
    assert "CONFLICTING_BAR_REVISION" in revision.reasons
    assert revision.visible_bar is None
    key = (bar_a.contract_id, bar_a.start_nano, bar_a.duration_seconds)
    assert monitor.bar_keys[key] == digest(bar_a)  # published version kept
    resend = monitor.receive(bar_event(3, bar_a), clock)
    assert "DUPLICATE_BAR" in resend.reasons and resend.visible_bar is None
    assert monitor.bar_keys[key] == digest(bar_a)  # still the original content


def test_vector_out_of_order_below_watermark():
    decoded, bundle = shared_fixture()
    session = session_of(bundle, decoded["bars"][0].session_id)
    base = decoded["bars"][0]
    first = minute_bar(base, bar_id="acq-ooo-1", session=session)
    third = minute_bar(base, bar_id="acq-ooo-3", session=session, offset_minutes=2)
    second = minute_bar(base, bar_id="acq-ooo-2", session=session, offset_minutes=1)
    monitor = FeedMonitor(bundle)
    clock = ReplayClock(first.end_nano + SECOND)
    monitor.receive(bar_event(1, first), clock)
    monitor.receive(bar_event(3, third), clock)
    late_row = monitor.receive(bar_event(2, second), clock)
    assert "OUT_OF_ORDER" in late_row.reasons
    assert monitor.watermark == 3
    # The late bar is still auditable and effective from its own receive time,
    # never a rewrite of published history.
    assert late_row.event.bar.bar_id == second.bar_id


def test_vector_sequence_gap_flagged():
    decoded, bundle = shared_fixture()
    session = session_of(bundle, decoded["bars"][0].session_id)
    base = decoded["bars"][0]
    first = minute_bar(base, bar_id="acq-gap-1", session=session)
    fourth = minute_bar(base, bar_id="acq-gap-4", session=session, offset_minutes=3)
    monitor = FeedMonitor(bundle)
    clock = ReplayClock(first.end_nano + SECOND)
    assert monitor.receive(bar_event(1, first), clock).reasons == ()
    row = monitor.receive(bar_event(4, fourth), clock)
    assert "SEQUENCE_GAP" in row.reasons and monitor.watermark == 4


def test_vector_late_fill_and_stale_boundary():
    decoded, bundle = shared_fixture()
    session = session_of(bundle, decoded["bars"][0].session_id)
    bar = minute_bar(decoded["bars"][0], bar_id="acq-late-1", session=session)
    monitor = FeedMonitor(bundle)
    late = monitor.receive(bar_event(1, bar), ReplayClock(bar.end_nano + 3 * SECOND))
    assert "LATE_FILL" in late.reasons and "DATA_STALE" not in late.reasons
    assert late.visible_bar is not None and late.visible_bar.quality.status != "STALE"
    stale = FeedMonitor(bundle).receive(bar_event(1, bar),
                                        ReplayClock(bar.end_nano + 121 * SECOND))
    assert "LATE_FILL" in stale.reasons and "DATA_STALE" in stale.reasons
    assert stale.visible_bar.quality.status == "STALE"
    # Any >120s gap may not open new risk (monitor freshness gate).
    monitor.last_bar_end[bar.contract_id] = bar.end_nano
    assert monitor.can_add_risk(bar.contract_id, bar.end_nano + 120 * SECOND)
    assert not monitor.can_add_risk(bar.contract_id, bar.end_nano + 121 * SECOND)


def test_vector_disconnect_reconnect_duplicate_resend():
    decoded, bundle = shared_fixture()
    session = session_of(bundle, decoded["bars"][0].session_id)
    bar = minute_bar(decoded["bars"][0], bar_id="acq-disc-1", session=session)
    event = bar_event(1, bar)
    monitor = FeedMonitor(bundle)
    clock = ReplayClock(bar.end_nano + SECOND)
    assert monitor.receive(event, clock).reasons == ()
    monitor.last_bar_end[bar.contract_id] = bar.end_nano
    clock.advance(bar.end_nano + 10 * SECOND)
    disconnect = FeedEvent("acq-2", 2, "DISCONNECT", clock.now_nano(), clock.now_nano(),
                           None, (), SOURCE_ID)
    assert "DISCONNECTED" in monitor.receive(disconnect, clock).reasons
    assert not monitor.connected and not monitor.can_add_risk(bar.contract_id, clock.now_nano())
    clock.advance(bar.end_nano + 20 * SECOND)
    reconnect = FeedEvent("acq-3", 3, "RECONNECT", clock.now_nano(), clock.now_nano(),
                          None, (), SOURCE_ID)
    assert "RECONNECTED_AWAIT_FRESH_BAR" in monitor.receive(reconnect, clock).reasons
    assert monitor.connected
    # Post-reconnect duplicate re-delivery of the same content is absorbed.
    clock.advance(bar.end_nano + 21 * SECOND)
    resend = monitor.receive(event, clock)
    assert "DUPLICATE" in resend.reasons and resend.visible_bar is None
    assert monitor.seen[event.event_id] == digest(event) and monitor.watermark == 3


def test_vector_trading_day_change_and_night_session():
    decoded, bundle = shared_fixture()
    base = decoded["bars"][0]
    day_session = session_of(bundle, base.session_id)
    night_session = next_night_after(bundle, day_session)
    day_bar = minute_bar(base, bar_id="acq-day-1", session=day_session)
    night_bar = minute_bar(base, bar_id="acq-night-1", session=night_session)
    monitor = FeedMonitor(bundle)
    clock = ReplayClock(day_bar.end_nano + SECOND)
    day_row = monitor.receive(bar_event(1, day_bar), clock)
    clock.advance(night_bar.end_nano + SECOND)
    night_row = monitor.receive(bar_event(2, night_bar), clock)
    assert day_row.reasons == () and night_row.reasons == ()
    assert night_row.visible_bar.trading_day == night_session.trading_day
    # A bar whose trading day contradicts the session calendar is rejected,
    # never guessed into the mapping.
    wrong = minute_bar(base, bar_id="acq-night-wrong", session=night_session,
                       offset_minutes=1, trading_day=day_session.trading_day)
    clock.advance(night_bar.end_nano + 2 * SECOND)
    wrong_row = monitor.receive(bar_event(3, wrong), clock)
    assert "TRADING_DAY_INVALID" in wrong_row.reasons
    assert wrong_row.visible_bar is None


def test_vector_non_specific_contract_rejected():
    decoded, bundle = shared_fixture()
    session = session_of(bundle, decoded["bars"][0].session_id)
    bar = minute_bar(decoded["bars"][0], bar_id="acq-main-continuous", session=session,
                     contract_id="KQ.m@SHFE.rb", raw_symbol="KQ.m@SHFE.rb")
    row = FeedMonitor(bundle).receive(bar_event(1, bar), ReplayClock(bar.end_nano + SECOND))
    assert "IDENTITY_INVALID" in row.reasons
    assert row.visible_bar is None  # main-continuous symbols never enter the feed


def test_vector_unfinished_bar_rejected():
    decoded, bundle = shared_fixture()
    session = session_of(bundle, decoded["bars"][0].session_id)
    bar = minute_bar(decoded["bars"][0], bar_id="acq-forming-1", session=session,
                     complete=False, actual_minutes=0)
    row = FeedMonitor(bundle).receive(bar_event(1, bar), ReplayClock(bar.end_nano + SECOND))
    assert "BAR_INCOMPLETE" in row.reasons
    assert row.visible_bar is None  # forming bars are never published


def test_vector_source_receive_stamp_stays_diagnostic():
    decoded, bundle = shared_fixture()
    session = session_of(bundle, decoded["bars"][0].session_id)
    bar = minute_bar(decoded["bars"][0], bar_id="acq-clock-1", session=session)
    event = bar_event(1, bar)
    clock = ReplayClock(bar.end_nano + SECOND)
    row = FeedMonitor(bundle).receive(event, clock)
    # The source-reported stamp is preserved verbatim for diagnosis, while the
    # receiver-validated timing comes from the independent clock.
    assert row.event.received_at_nano == event.received_at_nano == bar.end_nano
    assert row.runner_received_at_nano == clock.now_nano()
    evidence = dict(clock.evidence())
    assert evidence["source_kind"] == "SYNTHETIC"
    assert validate_clock_evidence(evidence, clock.now_nano())[1] is False
    assert row.clock_verified is False


# --------------------------------------------------------------------------
# Scenario-register traceability (work order -> vectors -> statuses)
# --------------------------------------------------------------------------

def test_work_order_scenario_register_is_complete_and_covered():
    slugs = tuple(slug for slug, _ in af.WORK_ORDER_SCENARIO_KEYS)
    assert len(slugs) == len(set(slugs)) == 11
    assert set(slugs) == set(af.SCENARIO_STATUS)
    allowed = {"RUN_NOW", "PENDING_FREEZE", "PENDING_R8_HANDOVER"}
    assert set(af.SCENARIO_STATUS.values()) <= allowed
    assert set(af.ADDITIONAL_DRAFT_SCENARIOS.values()) <= allowed - {"RUN_NOW"}
    run_now = {slug for slug, status in af.SCENARIO_STATUS.items() if status == "RUN_NOW"}
    assert run_now == set(RUN_NOW_VECTORS)
    # The remaining two work-order scenarios are gated, never silently dropped.
    gated = {slug for slug, status in af.SCENARIO_STATUS.items() if status != "RUN_NOW"}
    assert gated == {"producer_watermark_jump"}
    assert af.SCENARIO_STATUS["producer_watermark_jump"] == "PENDING_FREEZE"


RUN_NOW_VECTORS = {
    "normal_closed_bar": test_vector_normal_closed_bar,
    "duplicate_same_content_idempotent": test_vector_duplicate_same_content_idempotent,
    "same_sequence_different_content_conflict": test_vector_same_sequence_different_content_conflict,
    "out_of_order_below_watermark": test_vector_out_of_order_below_watermark,
    "late_fill_flagged": test_vector_late_fill_and_stale_boundary,
    "sequence_gap_flagged": test_vector_sequence_gap_flagged,
    "disconnect_reconnect_duplicate_resend": test_vector_disconnect_reconnect_duplicate_resend,
    "trading_day_change_night_session": test_vector_trading_day_change_and_night_session,
    "non_specific_contract_rejected": test_vector_non_specific_contract_rejected,
    "unfinished_bar_rejected": test_vector_unfinished_bar_rejected,
}


# --------------------------------------------------------------------------
# PENDING drafts (skipped until the freeze gate opens; bodies are outlines)
# --------------------------------------------------------------------------

@pytest.mark.skip(reason=PENDING_FREEZE_REASON)
def test_draft_adapter_contract_record_ingestion():
    """Draft: frozen-contract records (feed-input-contract section 1 shape) map
    onto FeedEvent/MarketBar with decimal OHLC preserved verbatim, dual clocks
    kept separate, and origin/evidence_version carried in the feed source id."""


@pytest.mark.skip(reason=PENDING_FREEZE_REASON)
def test_draft_adapter_producer_watermark_jump():
    """Draft: a producer watermark jumping past unconfirmed closes publishes
    nothing beyond the confirmed boundary and emits an error/audit event per
    contract section 2; bars beyond the watermark stay unpublished."""


@pytest.mark.skip(reason=PENDING_FREEZE_REASON)
def test_draft_adapter_watermark_restart_persistence():
    """Draft: after a simulated restart the adapter restores the persisted
    per-(contract, duration) watermark; a regressing watermark is an error
    event and never reopens published history."""


@pytest.mark.skip(reason=PENDING_FREEZE_REASON)
def test_draft_adapter_legally_empty_window_requires_source_rule():
    """Draft: an exchange no-trade window is only legal with source-rule
    evidence attached; without it the window stays in the coverage denominator
    as a gap (never silently excised)."""


@pytest.mark.skip(reason=PENDING_FREEZE_REASON)
def test_draft_adapter_reorder_buffer_bound_two_seconds():
    """Draft: the reorder buffer holds unpublished events at most 2s; older
   补齐 follow the late path effective from their true receive time."""


@pytest.mark.skip(reason=PENDING_R8_REASON)
def test_draft_execution_side_feed_consumption():
    """Draft: how (whether) the execution side consumes the same feed is
    defined only by the R8 published handover; nothing is inferred here."""
