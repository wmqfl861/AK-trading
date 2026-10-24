"""C1 tests: identity classification (H02/H05) and calendar rules (H04/H07).

SYNTHETIC calendar sessions are explicitly labelled fixtures; they never
claim to be official calendar evidence for the real dates involved.
"""

from __future__ import annotations

import pytest

from ak_trading.acquisition import contracts as c
from ak_trading.acquisition import identity as ident
from ak_trading.acquisition.calendar import (
    CalendarBook,
    SessionEvidence,
    TargetSpec,
    compute_watermark_nano,
    daily_row_status,
    minute_row_status,
)


# ---------------------------------------------------------------------------
# H02/H05 identity
# ---------------------------------------------------------------------------

class TestSymbolClassification:
    @pytest.fixture()
    def universe(self, synthetic_inventory_payload):
        return ident.Universe.from_mapping(
            synthetic_inventory_payload, provenance="fixture"
        )

    @pytest.fixture()
    def classifier(self, universe):
        return ident.SymbolClassifier(universe)

    def test_baseline_conservation_81(self, universe):
        universe.check_baseline_conservation()
        counts = universe.baseline_product_counts()
        assert counts == ident.BASELINE_VENUE_COUNTS
        assert sum(counts.values()) == 81

    def test_kq_main_and_index_are_reference_only(self, classifier):
        main = classifier.classify("KQ.m@SHFE.rb")
        index = classifier.classify("KQ.i@SHFE.rb")
        assert main.instrument_kind is c.InstrumentKind.MAIN_CONTINUOUS_REFERENCE
        assert index.instrument_kind is c.InstrumentKind.INDEX_REFERENCE
        expansion = ident.expand_denominator(
            ident.Universe.from_mapping(
                __import__("json").loads(
                    (__import__("pathlib").Path(__file__).parent
                     / "fixtures/synthetic_inventory.json").read_text("utf-8")
                ),
                provenance="fixture",
            )
        )
        # Reference symbols never appear in concrete coverage sets.
        for symbols in expansion.concrete_by_product.values():
            assert not any(s.startswith("KQ.") for s in symbols)

    def test_concrete_four_digit_delivery(self, classifier):
        identity = classifier.classify("SHFE.rb2601", expired=False)
        assert identity.instrument_kind is c.InstrumentKind.CONCRETE_FUTURE
        assert identity.delivery_yyyymm == "2026-01"
        assert identity.lifecycle_status is c.LifecycleStatus.ACTIVE

    def test_czce_three_digit_requires_source_mapping(self, classifier):
        # Without expiry evidence: delivery month stays unknown (H02).
        ambiguous = classifier.classify("CZCE.TA601", expired=False)
        assert ambiguous.instrument_kind is c.InstrumentKind.CONCRETE_FUTURE
        assert ambiguous.delivery_yyyymm is None
        assert "CZCE_3DIGIT_YEAR_AMBIGUOUS" in ambiguous.delivery_unknown_reason
        # With expiry evidence: mapped from the source, not guessed.
        from ak_trading.acquisition.contracts import iso_instant_to_nano

        mapped = classifier.classify(
            "CZCE.MA605",
            expired=False,
            expire_datetime_nano=iso_instant_to_nano("2026-05-15T15:00:00+08:00"),
        )
        assert mapped.delivery_yyyymm == "2026-05"
        assert mapped.delivery_provenance == "source_expire_evidence"

    def test_f_suffix_quarantined_never_stripped(self, classifier):
        identity = classifier.classify("DCE.l2602F")
        assert identity.instrument_kind is c.InstrumentKind.F_SUFFIX_QUARANTINED
        assert identity.delivery_yyyymm is None
        assert "QUARANTINED_PENDING_CLASSIFICATION" in identity.delivery_unknown_reason
        # Even a source ins_class=FUTURE would not clear quarantine: the
        # classifier itself never strips; stripping must not produce a task.
        stripped = "DCE.l2602"
        assert stripped not in {
            s for s in ident.expand_denominator(
                ident.Universe.from_mapping(
                    __import__("json").loads(
                        (__import__("pathlib").Path(__file__).parent
                         / "fixtures/synthetic_inventory.json").read_text("utf-8")
                    ),
                    provenance="fixture",
                )
            ).quarantined
        }

    def test_f_suffix_fixture_count_matches_baseline_shape(self, synthetic_inventory_payload):
        universe = ident.Universe.from_mapping(
            synthetic_inventory_payload, provenance="fixture"
        )
        expansion = ident.expand_denominator(universe)
        assert len(expansion.quarantined) == 42
        per_product = {}
        for symbol in expansion.quarantined:
            product = __import__("re").sub(r"[0-9]{2,4}F$", "", symbol)
            per_product[product] = per_product.get(product, 0) + 1
        assert per_product == {"DCE.l": 14, "DCE.pp": 14, "DCE.v": 14}

    def test_unknown_shapes_and_lifecycle_preserved(self, classifier):
        weird = classifier.classify("SHFE.rb@weird#1")
        assert weird.instrument_kind is c.InstrumentKind.UNKNOWN_UNCLASSIFIED
        unknown_life = classifier.classify("SHFE.rb2510", expired=None)
        assert unknown_life.lifecycle_status is c.LifecycleStatus.LIFECYCLE_UNKNOWN
        expansion = ident.expand_denominator(
            ident.Universe.from_mapping(
                __import__("json").loads(
                    (__import__("pathlib").Path(__file__).parent
                     / "fixtures/synthetic_inventory.json").read_text("utf-8")
                ),
                provenance="fixture",
            )
        )
        # Unknown-lifecycle symbols never leave the denominator.
        assert any("rb2510" in s for s in expansion.lifecycle_unknown)

    def test_new_product_enters_new_dated_denominator(self, universe):
        product = ident.ProductEntry(
            product_key="SHFE.zz", exchange="SHFE", source_product_code="zz",
            status="NEW_VERIFIED", priority="NEW", is_baseline=False,
        )
        merged = ident.register_new_product(
            universe,
            product,
            [ident.SymbolEntry("SHFE.zz2601", "SHFE.zz", False, None,
                               universe.observed_at_nano)],
        )
        # 81 baseline stays 81; the new product is a separate denominator.
        merged.check_baseline_conservation()
        assert sum(1 for p in merged.products if not p.is_baseline) == 1

    def test_metadata_quota_limits(self):
        quota = ident.MetadataQuota(max_calls=12, max_symbols_per_call=100)
        for _ in range(12):
            quota.charge_discovery("SHFE")
        with pytest.raises(c.ContractError):
            quota.charge_discovery("DCE")
        quota2 = ident.MetadataQuota(max_calls=12, max_symbols_per_call=100)
        with pytest.raises(c.ContractError):
            quota2.charge_discovery("SHFE.DCE")  # multi-venue discovery refused
        with pytest.raises(c.ContractError):
            quota2.charge_symbol_details([f"S{i}" for i in range(101)])


# ---------------------------------------------------------------------------
# H04/H07 calendar
# ---------------------------------------------------------------------------

def _nano(iso: str) -> int:
    return c.iso_instant_to_nano(iso)


class TestCalendarRules:
    @pytest.fixture()
    def book(self, synthetic_calendar_payload):
        return CalendarBook.from_mapping(synthetic_calendar_payload)

    def test_night_session_belongs_to_next_trading_day(self, book):
        # 2026-09-17 21:30 civil bar belongs to trading day 2026-09-18 (rb).
        assert (
            book.trading_day_for_instant("SHFE.rb", _nano("2026-09-17T21:30:00+08:00"))
            == "2026-09-18"
        )
        # Day session of 09-18 belongs to 09-18.
        assert (
            book.trading_day_for_instant("SHFE.rb", _nano("2026-09-18T10:00:00+08:00"))
            == "2026-09-18"
        )

    def test_friday_night_crossing_into_saturday_is_legal(self, book):
        # au long night 21:00 -> 02:30 crosses midnight into Saturday civil
        # time; bars after midnight still belong to trading day 2026-09-21.
        assert (
            book.trading_day_for_instant("SHFE.au", _nano("2026-09-19T01:00:00+08:00"))
            == "2026-09-21"
        )
        # The Saturday civil date itself is NOT a trading day for day session.
        assert (
            book.trading_day_for_instant("SHFE.rb", _nano("2026-09-19T10:00:00+08:00"))
            is None
        )

    def test_no_evidence_is_unknown_not_invented(self, book):
        # CZCE.SF has no session evidence in the fixture -> unknown, never a
        # generated default session (H04: no scenario calendar).
        assert (
            book.trading_day_for_instant("CZCE.SF", _nano("2026-09-18T10:00:00+08:00"))
            is None
        )
        assert book.day_close_nano("CZCE.SF", "2026-09-18") is None

    def test_day_close_is_last_session_end_plus_buffer(self, book):
        close = book.day_close_nano("SHFE.rb", "2026-09-18")
        assert close == _nano("2026-09-18T15:00:00+08:00") + 60 * 10**9
        # NOT start + 86400: day starts 21:00 prior night; +86400 would land
        # on 2026-09-18T21:00 which is the NEXT day's night session.
        wrong = _nano("2026-09-17T21:00:00+08:00") + 86400 * 10**9
        assert close != wrong

    def test_minute_closure_watermark_semantics(self, book):
        watermark = _nano("2026-09-18T15:30:00+08:00")
        # Bar fully inside the day session and before the watermark: CLOSED.
        status, day, _ = minute_row_status(
            book=book, product_key="SHFE.rb", duration_seconds=60,
            bar_start_nano=_nano("2026-09-18T14:59:00+08:00"),
            bar_end_nano=_nano("2026-09-18T15:00:00+08:00"),
            watermark_nano=watermark,
        )
        assert status is c.ValidRowStatus.CLOSED and day == "2026-09-18"
        # Live tail inside an ACTIVE night session, beyond the watermark:
        # PROVISIONAL (night of 09-17 21:xx belongs to trading day 09-18).
        status, day, reason = minute_row_status(
            book=book, product_key="SHFE.rb", duration_seconds=60,
            bar_start_nano=_nano("2026-09-17T21:29:00+08:00"),
            bar_end_nano=_nano("2026-09-17T21:30:00+08:00"),
            watermark_nano=_nano("2026-09-17T21:29:30+08:00"),
        )
        assert status is c.ValidRowStatus.PROVISIONAL
        assert day == "2026-09-18"
        assert reason == "beyond_capture_watermark"
        # A bar starting exactly at session end has no applicable session
        # grid: UNKNOWN (never counted as closed, never fabricated).
        status, day2, reason = minute_row_status(
            book=book, product_key="SHFE.rb", duration_seconds=60,
            bar_start_nano=_nano("2026-09-18T15:00:00+08:00"),
            bar_end_nano=_nano("2026-09-18T15:01:00+08:00"),
            watermark_nano=watermark,
        )
        assert status is c.ValidRowStatus.UNKNOWN
        assert reason == "CALENDAR_UNKNOWN"
        # No session evidence at all: UNKNOWN with calendar reason.
        status, day3, reason = minute_row_status(
            book=book, product_key="CZCE.SF", duration_seconds=60,
            bar_start_nano=_nano("2026-09-18T10:00:00+08:00"),
            bar_end_nano=_nano("2026-09-18T10:01:00+08:00"),
            watermark_nano=watermark,
        )
        assert status is c.ValidRowStatus.UNKNOWN and day3 is None
        assert reason == "CALENDAR_UNKNOWN"

    def test_daily_closure_waits_for_final_session(self, book):
        # Before the day's final session end + buffer: PROVISIONAL.
        status, _ = daily_row_status(
            book=book, product_key="SHFE.rb", trading_day="2026-09-18",
            watermark_nano=_nano("2026-09-18T14:00:00+08:00"),
        )
        assert status is c.ValidRowStatus.PROVISIONAL
        # After final session end + 60 s buffer: CLOSED.
        status, _ = daily_row_status(
            book=book, product_key="SHFE.rb", trading_day="2026-09-18",
            watermark_nano=_nano("2026-09-18T15:01:00+08:00"),
        )
        assert status is c.ValidRowStatus.CLOSED
        # Unknown trading day: UNKNOWN, never zero-filled.
        status, day = daily_row_status(
            book=book, product_key="SHFE.rb", trading_day=None,
            watermark_nano=_nano("2026-09-18T15:01:00+08:00"),
        )
        assert status is c.ValidRowStatus.UNKNOWN and day is None

    def test_target_a_and_b_overlap_counting(self, book):
        target_a = TargetSpec.target_a("2026-09-18")
        target_b = TargetSpec.target_b(
            _nano("2026-09-18T00:00:00+08:00"), _nano("2026-09-20T00:00:00+08:00")
        )
        # A night bar (09-17 21:30) is in target A (trading day) but NOT in B
        # (civil window starts 09-18 00:00).
        bar_start = _nano("2026-09-17T21:30:00+08:00")
        day = book.trading_day_for_instant("SHFE.rb", bar_start)
        assert target_a.references_bar(
            product_key="SHFE.rb", bar_start_nano=bar_start, trading_day=day, book=book
        )
        assert not target_b.references_bar(
            product_key="SHFE.rb", bar_start_nano=bar_start, trading_day=day, book=book
        )
        # A Friday-night bar crossing midnight (au 09-19 01:00) is in B (civil
        # window ends 09-20 00:00) and belongs to trading day 09-21, so it is
        # NOT in target A.
        bar_start = _nano("2026-09-19T01:00:00+08:00")
        day = book.trading_day_for_instant("SHFE.au", bar_start)
        assert day == "2026-09-21"
        assert target_b.references_bar(
            product_key="SHFE.au", bar_start_nano=bar_start, trading_day=day, book=book
        )
        assert not target_a.references_bar(
            product_key="SHFE.au", bar_start_nano=bar_start, trading_day=day, book=book
        )

    def test_watermark_is_observed_minus_60s(self):
        observed = _nano("2026-09-19T10:00:00+08:00")
        assert compute_watermark_nano(observed) == observed - 60 * 10**9

    def test_session_evidence_validation(self):
        with pytest.raises(c.ContractError):
            SessionEvidence(
                product_key="p", trading_day="2026-09-18",
                segments=(
                    (_nano("2026-09-18T10:00:00+08:00"), _nano("2026-09-18T09:00:00+08:00")),
                ),
                provenance="x", observed_at_nano=1,
            )
        with pytest.raises(c.ContractError):
            SessionEvidence(
                product_key="p", trading_day="2026-09-18", segments=(),
                provenance="x", observed_at_nano=1,
            )
