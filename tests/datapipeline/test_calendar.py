"""DP04 calendar evidence tests (r1).

Scope: tests/datapipeline only (DP01 frozen pytest.ini shape: explicit
pythonpath injection via the runner, --confcutdir at this directory, a
per-node --basetemp).  Nothing here inherits the legacy environment
fixtures.

Fixtures under fixtures/DP04 are prominently SYNTHETIC (fictional exchange
SYNTHEX, civil year 2027 - disjoint from every real calendar fact).  They
prove LOGIC only: per the work order, official historical calendar coverage
is NOT_RUN/UNKNOWN until real evidence is handed off by DP00.

Real inputs read PROGRAMMATICALLY with hash re-verification:
    - DP01/r1 frozen sources (dp01-freeze-manifest.json) - the contract
      package this node implements against;
    - DP00/r1 universe.json calendar anchor -> config/china_futures_catalog.json
      (the 2026 exchange-level closed-dates anchor, read-only).
Any pinned-hash drift fails hard (BLOCKED semantics, DP01 precedent).
"""

from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Optional

import pytest

_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "DP04"
_spec = importlib.util.spec_from_file_location("dp04_bootstrap_c", _FIXTURES_DIR / "bootstrap.py")
_bootstrap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bootstrap)
_STAGING_SRC = _bootstrap.ensure_importable()

from ak_trading.datapipeline import contracts as C
from ak_trading.datapipeline import ports as P
from ak_trading.datapipeline import calendar as CAL
from ak_trading.datapipeline.calendar import CalendarEvidenceDocument as Doc
from ak_trading.datapipeline.contracts import ContractError

HERE = Path(__file__).resolve().parent
FIXTURES = _FIXTURES_DIR
REPO_ROOT = HERE.parents[1]
DATASET = "futures_kline"
SOURCE_SYMBOL = "SYNTH.rb2705"
MAINLINE = "KQ.m@SYNTH.rb"
AS_OF = "2027-03-09T20:00:00+08:00"          # after every synthetic obtained_at except r2/pit docs


def nano(iso: str) -> int:
    return C.iso_instant_to_nano(iso)


def window(start_iso: str, end_iso: str) -> C.IntervalNano:
    return C.IntervalNano(nano(start_iso), nano(end_iso))


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def doc_from_fixture(name: str) -> Doc:
    return CAL.calendar_evidence_document_from_mapping(load_fixture(name))


def book_from(*names: str) -> CAL.CalendarBook:
    return CAL.CalendarBook([doc_from_fixture(name) for name in names])


def expect_error(fragment: str):
    return pytest.raises(ContractError, match=fragment)


def _find_repo_root() -> Optional[Path]:
    node = HERE
    for _ in range(12):
        if (node / "config" / "china_futures_catalog.json").is_file():
            return node
        if node.parent == node:
            return None
        node = node.parent
    return None


def _find_dp00_tree() -> Optional[Path]:
    node = HERE
    for _ in range(12):
        candidate = node / "docs" / "devplan-datapipeline-20260923-r1" / "DP00" / "r1"
        if (candidate / "baseline-manifest.json").is_file():
            return candidate
        if node.parent == node:
            return None
        node = node.parent
    return None


def _find_dp01_tree() -> Optional[Path]:
    node = HERE
    for _ in range(12):
        candidate = node / "docs" / "devplan-datapipeline-20260923-r1" / "DP01" / "r1"
        if (candidate / "dp01-freeze-manifest.json").is_file():
            return candidate
        if node.parent == node:
            return None
        node = node.parent
    return None


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ===========================================================================
# Pinned inputs: DP01 freeze + DP00 calendar anchor (BLOCKED gate)
# ===========================================================================


class TestSymbolDomainParsing:
    """Engineering glue: routing a symbol to its evidence documents.

    The parser decides NOTHING about when the market trades - it only
    derives the (exchange, product) domain used for applicability matching
    (H04.4: products differ; no natural-day/weekday derivation anywhere).
    """

    def test_reference_and_contract_shapes(self):
        assert CAL.parse_source_symbol_domains("KQ.m@SHFE.rb") == (("shfe", "rb"),)
        assert CAL.parse_source_symbol_domains("KQ.i@CFFEX.ic") == (("cffex", "ic"),)
        assert CAL.parse_source_symbol_domains("SHFE.rb2610") == (("shfe", "rb"),)
        assert CAL.parse_source_symbol_domains("CZCE.TA601") == (("czce", "ta"),)
        assert CAL.parse_source_symbol_domains("CFFEX.IC2606") == (("cffex", "ic"),)

    def test_unparseable_symbols_have_no_domain(self):
        assert CAL.parse_source_symbol_domains("garbage") == ()
        assert CAL.parse_source_symbol_domains("") == ()
        assert CAL.parse_source_symbol_domains("SHFE.rb") == (("shfe", "rb"),)

    def test_mainline_and_contract_share_product_evidence(self):
        """Product-scoped evidence answers the mainline reference symbol and
        a listed concrete contract identically (same product domain)."""
        book = book_from("synthetic_session_grid_partial.json")
        as_of = nano(AS_OF)
        win = window("2027-03-01T09:00:00+08:00", "2027-03-01T10:15:00+08:00")
        mainline = book.query(dataset=DATASET, source_symbol=MAINLINE,
                              duration_seconds=60, window=win, as_of_nano=as_of)
        contract = book.query(dataset=DATASET, source_symbol=SOURCE_SYMBOL,
                              duration_seconds=60, window=win, as_of_nano=as_of)
        assert [v.canonical() for v in mainline.subranges] == [
            v.canonical() for v in contract.subranges
        ]
        assert mainline.answer.sessions == contract.answer.sessions


class TestPinnedInputs:
    def test_dp01_frozen_sources_zero_drift(self):
        dp01 = _find_dp01_tree()
        repo = _find_repo_root()
        if dp01 is None or repo is None:
            pytest.skip("DP01 evidence tree not reachable from this layout (post-materialization)")
        manifest = json.loads((dp01 / "dp01-freeze-manifest.json").read_text(encoding="utf-8"))
        checked = 0
        for entry in manifest["files"]:
            path = repo / entry["path"]
            assert path.is_file(), f"pinned DP01 file missing: {path}"
            assert _sha256_file(path) == entry["sha256"], f"DP01 pin drift: {path}"
            checked += 1
        assert checked >= len([e for e in manifest["files"] if e["path"].endswith(".py")])

    def test_dp00_calendar_anchor_hash_matches_catalog(self):
        dp00 = _find_dp00_tree()
        if dp00 is None:
            pytest.skip("DP00 evidence tree not reachable from this layout")
        universe = json.loads((dp00 / "universe.json").read_text(encoding="utf-8"))
        anchor = universe["per_task_common"]["calendar_evidence"]
        assert anchor["type"] == "EXCHANGE_LEVEL_CLOSED_DATES_2026_ONLY"
        assert anchor["status"] == "review_required"
        repo = _find_repo_root()
        assert repo is not None, "repo root with the catalog not found"
        catalog = repo / "config" / "china_futures_catalog.json"
        assert _sha256_file(catalog) == anchor["sha256"], "2026 anchor hash drift vs DP00 pin"

    def test_dp00_anchor_is_the_only_real_calendar_evidence(self):
        """DP00 r1: no session intervals, pre-2026 has nothing (NR-10)."""
        dp00 = _find_dp00_tree()
        if dp00 is None:
            pytest.skip("DP00 evidence tree not reachable from this layout")
        universe = json.loads((dp00 / "universe.json").read_text(encoding="utf-8"))
        assert universe["per_task_common"]["calendar_evidence_status"] == "UNKNOWN"
        assert universe["per_task_common"]["generation_evidence_status"] == "UNKNOWN"


# ===========================================================================
# H04.1 envelope validation (evidence FIRST, then the read-only book)
# ===========================================================================


def _valid_grid_payload() -> dict:
    return {
        "schema_version": "datapipeline-calendar-evidence-v1",
        "evidence_version": "unit-r1",
        "dataset": DATASET,
        "source_id": "tqsdk-free-realtime-get-kline-serial",
        "evidence_kind": "SESSION_GRID",
        "data_origin": "SYNTHETIC",
        "synthetic_marker": "SYNTHETIC unit-test document",
        "applicability": {"exchanges": [], "products": ["synth.rb"], "source_symbols": []},
        "valid_from_nano": str(nano("2027-01-01T00:00:00+08:00")),
        "valid_through_nano": str(nano("2028-01-01T00:00:00+08:00")),
        "coverage": "PARTIAL_DAYS_LISTED",
        "pit_basis": "OBTAINED_AT",
        "obtained_at_nano": str(nano("2027-02-01T20:00:00+08:00")),
        "sessions": [
            {
                "trading_day": "2027-02-01",
                "segments": [
                    {"kind": "DAY", "start_nano": str(nano("2027-02-01T09:00:00+08:00")),
                     "end_nano": str(nano("2027-02-01T10:15:00+08:00"))},
                ],
            }
        ],
        "closed_dates": [],
    }


class TestEnvelopeValidation:
    def test_valid_payload_roundtrip_and_stable_hash(self):
        doc = CAL.calendar_evidence_document_from_mapping(_valid_grid_payload())
        assert doc.document_sha256() == CAL.canonical_sha256(doc.canonical())
        again = CAL.calendar_evidence_document_from_mapping(json.loads(json.dumps(doc.canonical())))
        assert again.document_sha256() == doc.document_sha256()

    def test_wrong_schema_version_rejected(self):
        payload = _valid_grid_payload()
        payload["schema_version"] = "acq_w2_calendar_evidence_v1"  # the legacy schema
        with expect_error("E_CALENDAR_EVIDENCE_SCHEMA"):
            CAL.calendar_evidence_document_from_mapping(payload)

    def test_synthetic_without_marker_rejected(self):
        payload = _valid_grid_payload()
        del payload["synthetic_marker"]
        with expect_error("E_CALENDAR_EVIDENCE_ORIGIN_MARKER"):
            CAL.calendar_evidence_document_from_mapping(payload)

    def test_scenario_without_marker_rejected(self):
        payload = _valid_grid_payload()
        payload["data_origin"] = "SCENARIO"
        del payload["synthetic_marker"]
        with expect_error("E_CALENDAR_EVIDENCE_ORIGIN_MARKER"):
            CAL.calendar_evidence_document_from_mapping(payload)

    def test_marker_forbidden_on_real_source(self):
        payload = _valid_grid_payload()
        payload["data_origin"] = "REAL_SOURCE"
        payload["evidence_reference"] = "http://example/evidence"
        payload["evidence_text_sha256"] = "0" * 64
        with expect_error("E_CALENDAR_EVIDENCE_ORIGIN_MARKER"):
            CAL.calendar_evidence_document_from_mapping(payload)

    def test_real_source_requires_provenance(self):
        payload = _valid_grid_payload()
        payload["data_origin"] = "REAL_SOURCE"
        del payload["synthetic_marker"]
        with expect_error("E_CALENDAR_EVIDENCE_PROVENANCE"):
            CAL.calendar_evidence_document_from_mapping(payload)
        payload["evidence_reference"] = "http://example/evidence"
        with expect_error("E_CALENDAR_EVIDENCE_PROVENANCE"):
            CAL.calendar_evidence_document_from_mapping(payload)
        payload["evidence_text_sha256"] = "0" * 64
        doc = CAL.calendar_evidence_document_from_mapping(payload)
        assert doc.data_origin is C.DataOrigin.REAL_SOURCE

    def test_document_hash_covers_origin_and_marker(self):
        """F16: the legacy evidence hash ignored the synthetic flag; ours
        must differ when origin/marker differ, all else equal."""
        base = CAL.calendar_evidence_document_from_mapping(_valid_grid_payload())
        real = _valid_grid_payload()
        real["data_origin"] = "REAL_SOURCE"
        real["evidence_reference"] = "http://example/evidence"
        real["evidence_text_sha256"] = "0" * 64
        del real["synthetic_marker"]
        real_doc = CAL.calendar_evidence_document_from_mapping(real)
        assert base.document_sha256() != real_doc.document_sha256()

    def test_missing_validity_domain_rejected(self):
        payload = _valid_grid_payload()
        del payload["valid_from_nano"]
        with expect_error("E_CALENDAR_EVIDENCE_VALIDITY"):
            CAL.calendar_evidence_document_from_mapping(payload)

    def test_inverted_validity_domain_rejected(self):
        payload = _valid_grid_payload()
        payload["valid_through_nano"] = payload["valid_from_nano"]
        with expect_error("E_CALENDAR_EVIDENCE_VALIDITY"):
            CAL.calendar_evidence_document_from_mapping(payload)

    def test_sessions_outside_validity_rejected(self):
        payload = _valid_grid_payload()
        payload["valid_from_nano"] = str(nano("2027-02-02T00:00:00+08:00"))
        with expect_error("E_CALENDAR_EVIDENCE_SESSION_DOMAIN"):
            CAL.calendar_evidence_document_from_mapping(payload)

    def test_unordered_or_overlapping_segments_rejected(self):
        payload = _valid_grid_payload()
        payload["sessions"][0]["segments"].insert(
            0,
            {"kind": "DAY", "start_nano": str(nano("2027-02-01T08:00:00+08:00")),
             "end_nano": str(nano("2027-02-01T09:30:00+08:00"))},
        )
        with expect_error("E_CALENDAR_EVIDENCE_SESSION_ORDER"):
            CAL.calendar_evidence_document_from_mapping(payload)

    def test_duplicate_trading_day_rejected(self):
        payload = _valid_grid_payload()
        payload["sessions"].append(dict(payload["sessions"][0]))
        with expect_error("E_CALENDAR_EVIDENCE_SESSION_ORDER"):
            CAL.calendar_evidence_document_from_mapping(payload)

    def test_empty_content_document_rejected(self):
        payload = _valid_grid_payload()
        payload["sessions"] = []
        with expect_error("E_CALENDAR_EVIDENCE_EMPTY"):
            CAL.calendar_evidence_document_from_mapping(payload)

    def test_closed_dates_document_may_not_carry_sessions(self):
        payload = _valid_grid_payload()
        payload["closed_dates"] = [
            {"exchange": "SYNTH", "date": "2027-05-01", "reason": "SYNTHETIC"}
        ]
        payload["evidence_kind"] = "EXCHANGE_CLOSED_DATES"
        with expect_error("E_CALENDAR_EVIDENCE_SCHEMA"):
            CAL.calendar_evidence_document_from_mapping(payload)

    def test_empty_applicability_rejected(self):
        payload = _valid_grid_payload()
        payload["applicability"] = {"exchanges": [], "products": [], "source_symbols": []}
        with expect_error("E_CALENDAR_EVIDENCE_APPLICABILITY"):
            CAL.calendar_evidence_document_from_mapping(payload)

    def test_unknown_enum_values_rejected(self):
        payload = _valid_grid_payload()
        payload["pit_basis"] = "SOMETIME"
        with expect_error("E_CALENDAR_EVIDENCE_ENUM"):
            CAL.calendar_evidence_document_from_mapping(payload)
        payload = _valid_grid_payload()
        payload["coverage"] = "MOSTLY"
        with expect_error("E_CALENDAR_EVIDENCE_ENUM"):
            CAL.calendar_evidence_document_from_mapping(payload)

    def test_pit_basis_obtained_requires_time(self):
        payload = _valid_grid_payload()
        del payload["obtained_at_nano"]
        with expect_error("E_CALENDAR_EVIDENCE_PROVENANCE"):
            CAL.calendar_evidence_document_from_mapping(payload)

    def test_pit_basis_unknown_forbids_time(self):
        payload = _valid_grid_payload()
        payload["pit_basis"] = "UNKNOWN"
        with expect_error("E_CALENDAR_EVIDENCE_PROVENANCE"):
            CAL.calendar_evidence_document_from_mapping(payload)
        del payload["obtained_at_nano"]
        doc = CAL.calendar_evidence_document_from_mapping(payload)
        assert doc.pit_basis is CAL.PitBasis.UNKNOWN
        assert doc.known_by(nano("2027-12-31T00:00:00+08:00")) is False


# ===========================================================================
# H04.2 path resolution + bytes->hash->object binding
# ===========================================================================


class TestPathResolutionAndLoading:
    def test_relative_path_resolves_under_first_root(self):
        resolved = CAL.resolve_evidence_path("evidence/a.json", ["r1/root", "r2/root"])
        assert resolved == "r1/root/evidence/a.json"

    def test_absolute_path_under_root_accepted(self):
        resolved = CAL.resolve_evidence_path("C:/data/evidence/a.json", ["C:/data", "D:/x"])
        assert resolved == "C:/data/evidence/a.json"

    def test_absolute_path_outside_roots_rejected(self):
        with expect_error("E_CALENDAR_EVIDENCE_PATH"):
            CAL.resolve_evidence_path("C:/other/a.json", ["C:/data"])

    def test_traversal_rejected(self):
        with expect_error("E_CALENDAR_EVIDENCE_PATH"):
            CAL.resolve_evidence_path("../escape.json", ["r1/root"])

    def test_forbidden_face_markers_rejected_before_any_fs_access(self):
        # The marker directory does not exist: a purely lexical rejection
        # must fire, not a missing-file error (checks precede file access).
        with expect_error("E_CALENDAR_EVIDENCE_PATH"):
            CAL.resolve_evidence_path("maintenance_artifacts/a.json", ["r1/root"])
        with expect_error("E_CALENDAR_EVIDENCE_PATH"):
            CAL.resolve_evidence_path("AKF260920/a.json", ["r1/root"])

    def test_credential_like_component_rejected(self):
        with expect_error("E_CALENDAR_EVIDENCE_PATH"):
            CAL.resolve_evidence_path(".env", ["r1/root"])
        with expect_error("E_CALENDAR_EVIDENCE_PATH"):
            CAL.resolve_evidence_path("secrets/cal.json", ["r1/root"])

    def test_load_chain_hash_and_document(self, tmp_path):
        root = tmp_path / "evidence-root"
        (root / "cal").mkdir(parents=True)
        payload = load_fixture("synthetic_closed_dates_2027.json")
        target = root / "cal" / "closed.json"
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        digest = _sha256_file(target)
        doc, bytes_hash = CAL.load_calendar_evidence_document(
            "cal/closed.json", allowed_roots=[str(root)], expected_sha256=digest
        )
        assert bytes_hash == digest
        assert doc.evidence_version == "synth-closed-2027-r1"
        assert doc.data_origin is C.DataOrigin.SYNTHETIC

    def test_load_missing_file_is_explicit_error(self, tmp_path):
        with expect_error("E_CALENDAR_EVIDENCE_PATH_MISSING"):
            CAL.load_calendar_evidence_document("cal/absent.json", allowed_roots=[str(tmp_path)])

    def test_load_hash_mismatch_uses_dp01_frozen_code(self, tmp_path):
        root = tmp_path
        target = root / "doc.json"
        target.write_text(json.dumps(load_fixture("synthetic_closed_dates_2027.json")), encoding="utf-8")
        with expect_error("E_ENVELOPE_HASH_MISMATCH"):
            CAL.load_calendar_evidence_document(
                "doc.json", allowed_roots=[str(root)], expected_sha256="0" * 64
            )

    def test_load_envelope_violation_is_explicit_error(self, tmp_path):
        root = tmp_path
        payload = _valid_grid_payload()
        del payload["synthetic_marker"]
        (root / "bad.json").write_text(json.dumps(payload), encoding="utf-8")
        with expect_error("E_CALENDAR_EVIDENCE_ORIGIN_MARKER"):
            CAL.load_calendar_evidence_document("bad.json", allowed_roots=[str(root)])


# ===========================================================================
# H04.1/H04.2/H04.5 book construction, version chain, binding
# ===========================================================================


class TestBookAndBinding:
    def test_identical_document_twice_is_idempotent(self):
        one = CAL.CalendarBook([doc_from_fixture("synthetic_session_grid_partial.json")])
        twice = CAL.CalendarBook(
            [doc_from_fixture("synthetic_session_grid_partial.json")] * 2
        )
        assert one.evidence_chain_sha256() == twice.evidence_chain_sha256()

    def test_chain_hash_is_order_independent_and_content_sensitive(self):
        a = doc_from_fixture("synthetic_session_grid_partial.json")
        b = doc_from_fixture("synthetic_closed_dates_2027.json")
        assert (
            CAL.CalendarBook([a, b]).evidence_chain_sha256()
            == CAL.CalendarBook([b, a]).evidence_chain_sha256()
        )
        assert CAL.CalendarBook([a]).evidence_chain_sha256() != CAL.CalendarBook([b]).evidence_chain_sha256()

    def test_same_version_different_bytes_is_hard_error(self):
        a = doc_from_fixture("synthetic_session_grid_partial.json")
        payload = load_fixture("synthetic_session_grid_partial.json")
        payload["sessions"][0]["segments"][0]["end_nano"] = str(
            int(payload["sessions"][0]["segments"][0]["end_nano"]) + 60 * 10**9
        )
        tampered = CAL.calendar_evidence_document_from_mapping(payload)
        with expect_error("E_CALENDAR_BOOK_DUPLICATE_CONFLICT"):
            CAL.CalendarBook([a, tampered])

    def test_empty_book_chain_is_none_and_queries_unknown(self):
        book = CAL.CalendarBook([])
        assert book.evidence_chain_sha256() is None
        result = book.query(
            dataset=DATASET, source_symbol=SOURCE_SYMBOL, duration_seconds=60,
            window=window("2027-03-01T09:00:00+08:00", "2027-03-01T15:00:00+08:00"),
            as_of_nano=nano(AS_OF),
        )
        assert result.answer.status is P.CalendarAnswerStatus.UNKNOWN
        assert result.answer.sessions == ()
        assert result.unknown_reasons == ("NO_CALENDAR_EVIDENCE_IN_DOMAIN",)

    def test_bind_success_and_failures(self):
        book = CAL.CalendarBook([doc_from_fixture("synthetic_session_grid_partial.json")])
        chain = book.evidence_chain_sha256()
        assert CAL.bind_calendar_evidence(chain, book.documents) is not None
        with expect_error("E_ENVELOPE_HASH_MISMATCH"):
            CAL.bind_calendar_evidence("0" * 64, book.documents)
        with expect_error("E_CALENDAR_BINDING_EMPTY"):
            CAL.bind_calendar_evidence(None, book.documents)
        # H04.2: hash "matches" but the book is EMPTY - never a success.
        with expect_error("E_CALENDAR_BINDING_EMPTY"):
            CAL.bind_calendar_evidence(None, [])

    def test_synthetic_chain_is_detectable(self):
        book = book_from("synthetic_session_grid_partial.json")
        assert book.has_synthetic_evidence() is True
        assert all(d.data_origin is C.DataOrigin.SYNTHETIC for d in book.documents)


# ===========================================================================
# V08 vectors (fixtures/DP04/vectors_calendar_v08.json)
# ===========================================================================


def _vector_ids():
    payload = load_fixture("vectors_calendar_v08.json")
    return [v["id"] for v in payload["vectors"]]


class TestV08Vectors:
    @pytest.mark.parametrize("vector_id", _vector_ids())
    def test_vector(self, vector_id):
        payload = load_fixture("vectors_calendar_v08.json")
        vector = next(v for v in payload["vectors"] if v["id"] == vector_id)
        book = book_from(*vector["fixtures"])
        if "instants" in vector:
            as_of = nano(vector["as_of_iso"])
            for case in vector["instants"]:
                got = book.trading_day_for_instant(
                    vector["dataset"], vector["source_symbol"], nano(case["iso"]), as_of
                )
                assert got == case["expected_trading_day"], case
            return
        if "trading_day" in vector:
            span = book.daily_session_span(
                vector["dataset"], vector["source_symbol"], vector["trading_day"], nano(vector["as_of_iso"])
            )
            exp = vector["expected_span"]
            assert span is not None
            assert span.start_nano == nano(exp["start_iso"])
            assert span.end_nano == nano(exp["end_iso"])
            assert (span.end_nano - span.start_nano) // 10**9 == exp["span_seconds"]
            return
        result = book.query(
            dataset=vector["dataset"],
            source_symbol=vector["source_symbol"],
            duration_seconds=vector["duration_seconds"],
            window=window(vector["window_start_iso"], vector["window_end_iso"]),
            as_of_nano=nano(vector["as_of_iso"]),
        )
        expected = vector["expected"]
        assert result.answer.status.value == expected["status"], vector["id"]
        assert len(result.answer.sessions) == expected.get("session_count", len(result.answer.sessions))
        if "verified_session_count" in expected:
            assert len(result.answer.sessions) == expected["verified_session_count"]
        if "verified_sessions_first_day" in expected:
            assert result.answer.sessions[0][0] == expected["verified_sessions_first_day"]
        for reason in expected.get("unknown_reasons_include", []):
            assert reason in result.unknown_reasons, (vector["id"], result.unknown_reasons)
        if expected.get("evidence_hash_required"):
            assert result.answer.evidence_sha256 is not None
        if expected.get("conflict_refs_required"):
            assert result.answer.conflict_refs
        if "no_trading_reason" in expected:
            assert any(
                v.reason == expected["no_trading_reason"] for v in result.subranges
            ), vector["id"]
        for fact in expected.get("subrange_facts", []):
            lo = nano(fact["start"]) if "start" in fact else None
            hi = nano(fact["end"])
            matches = [
                v for v in result.subranges
                if v.interval.end_nano == hi and (lo is None or v.interval.start_nano == lo)
                and v.status.value == fact["status"]
            ]
            assert matches, (vector["id"], fact, [(v.status.value, v.reason) for v in result.subranges])
        # H04.5 followup: a later as_of may see more - never less.
        if "followup_as_of_iso" in vector:
            follow = book.query(
                dataset=vector["dataset"],
                source_symbol=vector["source_symbol"],
                duration_seconds=vector["duration_seconds"],
                window=window(vector["window_start_iso"], vector["window_end_iso"]),
                as_of_nano=nano(vector["followup_as_of_iso"]),
            )
            fexp = vector["followup_expected"]
            assert follow.answer.status.value == fexp["status"], vector["id"]
            if "verified_session_count" in fexp:
                assert len(follow.answer.sessions) == fexp["verified_session_count"]


# ===========================================================================
# H04.3 bar classification: 60 s slots vs daily semantics
# ===========================================================================


class TestBarClassification:
    def test_minute_bar_fully_inside_session(self):
        book = book_from("synthetic_session_grid_partial.json")
        verdict, day, reason = CAL.classify_bar_calendar(
            book, dataset=DATASET, source_symbol=SOURCE_SYMBOL, duration_seconds=60,
            bar_start_nano=nano("2027-03-01T10:14:00+08:00"),
            bar_end_nano=nano("2027-03-01T10:15:00+08:00"),
            as_of_nano=nano(AS_OF),
        )
        assert (verdict, day, reason) == (CAL.BarCalendarVerdict.IN_SESSION, "2027-03-01", None)

    def test_minute_bar_crossing_nonaligned_segment_tail(self):
        """F16/H04.3: start inside the segment, tail beyond 10:15:30."""
        book = book_from("synthetic_session_grid_partial.json")
        verdict, day, _reason = CAL.classify_bar_calendar(
            book, dataset=DATASET, source_symbol=SOURCE_SYMBOL, duration_seconds=60,
            bar_start_nano=nano("2027-02-25T10:15:00+08:00"),
            bar_end_nano=nano("2027-02-25T10:16:00+08:00"),
            as_of_nano=nano(AS_OF),
        )
        assert verdict is CAL.BarCalendarVerdict.CROSSES_SESSION_BOUNDARY
        assert day == "2027-02-25"

    def test_minute_bar_crossing_into_lunch_under_complete_grid(self):
        """The same crossing rule with an evidenced break: enumerated."""
        book = book_from("synthetic_session_grid_complete_week.json")
        verdict, day, _reason = CAL.classify_bar_calendar(
            book, dataset=DATASET, source_symbol=SOURCE_SYMBOL, duration_seconds=60,
            bar_start_nano=nano("2027-03-03T10:14:00+08:00"),
            bar_end_nano=nano("2027-03-03T10:15:00+08:00"),
            as_of_nano=nano(AS_OF),
        )
        assert (verdict, day) == (CAL.BarCalendarVerdict.IN_SESSION, "2027-03-03")
        verdict, _day, reason = CAL.classify_bar_calendar(
            book, dataset=DATASET, source_symbol=SOURCE_SYMBOL, duration_seconds=60,
            bar_start_nano=nano("2027-03-03T10:15:00+08:00"),
            bar_end_nano=nano("2027-03-03T10:16:00+08:00"),
            as_of_nano=nano(AS_OF),
        )
        assert verdict is CAL.BarCalendarVerdict.OUTSIDE_SESSION_EVIDENCED
        assert reason == "NO_TRADING_EVIDENCED"

    def test_minute_bar_crossing_break_under_partial_grid_is_not_in_session(self):
        """Partial grid: the lunch gap of a LISTED trading day is evidenced
        by that day's complete segment list -> OUTSIDE_SESSION_EVIDENCED,
        still never IN_SESSION (H04.3)."""
        book = book_from("synthetic_session_grid_partial.json")
        verdict, _day, reason = CAL.classify_bar_calendar(
            book, dataset=DATASET, source_symbol=SOURCE_SYMBOL, duration_seconds=60,
            bar_start_nano=nano("2027-03-01T10:15:00+08:00"),
            bar_end_nano=nano("2027-03-01T10:16:00+08:00"),
            as_of_nano=nano(AS_OF),
        )
        assert verdict is CAL.BarCalendarVerdict.OUTSIDE_SESSION_EVIDENCED
        assert reason == "NO_TRADING_EVIDENCED"

    def test_unaligned_start_never_in_session(self):
        book = book_from("synthetic_session_grid_partial.json")
        verdict, _day, reason = CAL.classify_bar_calendar(
            book, dataset=DATASET, source_symbol=SOURCE_SYMBOL, duration_seconds=60,
            bar_start_nano=nano("2027-02-25T10:14:30+08:00"),
            bar_end_nano=nano("2027-02-25T10:15:30+08:00"),
            as_of_nano=nano(AS_OF),
        )
        assert verdict is CAL.BarCalendarVerdict.UNALIGNED_START
        assert reason == "START_OFF_MINUTE_GRID"

    def test_bar_length_mismatch_rejected(self):
        book = book_from("synthetic_session_grid_partial.json")
        with expect_error("E_CALENDAR_BAR_LENGTH"):
            CAL.classify_bar_calendar(
                book, dataset=DATASET, source_symbol=SOURCE_SYMBOL, duration_seconds=60,
                bar_start_nano=nano("2027-03-01T10:14:00+08:00"),
                bar_end_nano=nano("2027-03-01T10:16:00+08:00"),
                as_of_nano=nano(AS_OF),
            )

    def test_bar_on_conflicting_evidence(self):
        book = book_from("synthetic_session_grid_partial.json", "synthetic_conflicting_grid.json")
        verdict, _day, reason = CAL.classify_bar_calendar(
            book, dataset=DATASET, source_symbol=SOURCE_SYMBOL, duration_seconds=60,
            bar_start_nano=nano("2027-02-24T09:30:00+08:00"),
            bar_end_nano=nano("2027-02-24T09:31:00+08:00"),
            as_of_nano=nano("2027-03-10T20:00:00+08:00"),
        )
        assert verdict is CAL.BarCalendarVerdict.CALENDAR_CONFLICT

    def test_daily_bar_uses_session_span_not_natural_day(self):
        """V08: daily bar marker time != close; span end is the evidenced
        last session end (Q01/E_DAILY_86400_INFERRED guard)."""
        book = book_from("synthetic_session_grid_complete_week.json")
        verdict, day, _reason = CAL.classify_bar_calendar(
            book, dataset=DATASET, source_symbol=SOURCE_SYMBOL, duration_seconds=86400,
            bar_start_nano=nano("2027-03-02T21:00:00+08:00"),
            bar_end_nano=nano("2027-03-03T15:00:00+08:00"),
            as_of_nano=nano(AS_OF),
        )
        assert (verdict, day) == (CAL.BarCalendarVerdict.IN_SESSION, "2027-03-03")

    def test_natural_day_daily_bar_is_span_mismatch(self):
        book = book_from("synthetic_session_grid_complete_week.json")
        verdict, _day, reason = CAL.classify_bar_calendar(
            book, dataset=DATASET, source_symbol=SOURCE_SYMBOL, duration_seconds=86400,
            bar_start_nano=nano("2027-03-03T00:00:00+08:00"),
            bar_end_nano=nano("2027-03-04T00:00:00+08:00"),
            as_of_nano=nano(AS_OF),
        )
        assert verdict is CAL.BarCalendarVerdict.CROSSES_SESSION_BOUNDARY
        assert reason == "DAILY_BAR_SPAN_MISMATCH"

    def test_night_session_minute_bar_after_midnight(self):
        """Cross-midnight minute bars keep the exchange trading-day label."""
        book = book_from("synthetic_session_grid_partial.json")
        verdict, day, _reason = CAL.classify_bar_calendar(
            book, dataset=DATASET, source_symbol=SOURCE_SYMBOL, duration_seconds=60,
            bar_start_nano=nano("2027-02-27T02:29:00+08:00"),
            bar_end_nano=nano("2027-02-27T02:30:00+08:00"),
            as_of_nano=nano(AS_OF),
        )
        assert (verdict, day) == (CAL.BarCalendarVerdict.IN_SESSION, "2027-03-01")
        # 02:30-02:31 Saturday sits INSIDE trading day 2027-03-01's span
        # (Fri 21:00 -> Mon 15:00) but after the night session: the day's
        # complete segment list evidences no trading there (never UNKNOWN,
        # never IN_SESSION).
        verdict, _day, reason = CAL.classify_bar_calendar(
            book, dataset=DATASET, source_symbol=SOURCE_SYMBOL, duration_seconds=60,
            bar_start_nano=nano("2027-02-27T02:30:00+08:00"),
            bar_end_nano=nano("2027-02-27T02:31:00+08:00"),
            as_of_nano=nano(AS_OF),
        )
        assert verdict is CAL.BarCalendarVerdict.OUTSIDE_SESSION_EVIDENCED
        assert reason == "NO_TRADING_EVIDENCED"


# ===========================================================================
# H04.7 expected bar grids bound to BarGenerationEvidence
# ===========================================================================


def _fixed_slot_generation(duration: int, **overrides) -> C.BarGenerationEvidence:
    payload = dict(
        source_id="tqsdk-free-realtime-get-kline-serial",
        dataset=DATASET,
        applicable_symbols=(),
        applicable_products=("synth.rb",),
        duration_seconds=duration,
        valid_from_nano=nano("2027-01-01T00:00:00+08:00"),
        valid_through_nano=nano("2028-01-01T00:00:00+08:00"),
        emission_policy=C.EmissionPolicy.FIXED_SLOT,
        bar_time_anchor_and_close_rule=(
            "SYNTHETIC FIXED_SLOT rule: minute bars on the epoch minute grid; "
            "daily bar = [first session start, last session end)"
        ),
        boundary_short_bar_rule="SYNTHETIC: no short bars at boundaries; slots fully inside segments",
        evidence_text_sha256=hashlib.sha256(b"synthetic-generation-evidence").hexdigest(),
        evidence_reference="SYNTHETIC: DP04 unit-test generation evidence",
        obtained_at_nano=nano("2027-02-01T20:00:00+08:00"),
        data_origin=C.DataOrigin.SYNTHETIC,
    )
    payload.update(overrides)
    return C.BarGenerationEvidence(**payload)


class TestExpectedSlotGrid:
    def test_no_generation_evidence_is_expectation_unknown(self):
        book = book_from("synthetic_session_grid_partial.json")
        win = window("2027-03-01T09:00:00+08:00", "2027-03-01T15:00:00+08:00")
        grid = CAL.build_expected_slot_grid(
            book, dataset=DATASET, source_symbol=SOURCE_SYMBOL,
            window=win, as_of_nano=nano(AS_OF), generation=None,
        )
        assert grid.status is CAL.ExpectationStatus.EXPECTATION_UNKNOWN
        assert grid.slots == ()
        assert grid.unknown_ranges == (win,)
        assert "GENERATION_EVIDENCE_UNKNOWN" in grid.reasons

    def test_unknown_policy_evidence_is_expectation_unknown(self):
        book = book_from("synthetic_session_grid_partial.json")
        win = window("2027-03-01T09:00:00+08:00", "2027-03-01T10:00:00+08:00")
        unknown_gen = C.BarGenerationEvidence.unknown(
            source_id="tqsdk-free-realtime-get-kline-serial", dataset=DATASET,
            applicable_products=("synth.rb",), duration_seconds=60,
        )
        grid = CAL.build_expected_slot_grid(
            book, dataset=DATASET, source_symbol=SOURCE_SYMBOL,
            window=win, as_of_nano=nano(AS_OF), generation=unknown_gen,
        )
        assert grid.status is CAL.ExpectationStatus.EXPECTATION_UNKNOWN
        assert grid.slots == ()

    def test_trade_triggered_not_enumerable(self):
        book = book_from("synthetic_session_grid_partial.json")
        win = window("2027-03-01T09:00:00+08:00", "2027-03-01T10:00:00+08:00")
        grid = CAL.build_expected_slot_grid(
            book, dataset=DATASET, source_symbol=SOURCE_SYMBOL, window=win,
            as_of_nano=nano(AS_OF),
            generation=_fixed_slot_generation(60, emission_policy=C.EmissionPolicy.TRADE_TRIGGERED),
        )
        assert grid.status is CAL.ExpectationStatus.EXPECTATION_UNKNOWN
        assert grid.slots == ()
        assert "TRADE_TRIGGERED_NOT_ENUMERABLE" in grid.reasons

    def test_fixed_slot_day_grid_counts(self):
        """09:00-10:15 (75) + 10:30-11:30 (60) + 13:30-15:00 (90) = 225."""
        book = book_from("synthetic_session_grid_partial.json")
        grid = CAL.build_expected_slot_grid(
            book, dataset=DATASET, source_symbol=SOURCE_SYMBOL,
            window=window("2027-03-01T09:00:00+08:00", "2027-03-01T15:00:00+08:00"),
            as_of_nano=nano(AS_OF), generation=_fixed_slot_generation(60),
        )
        assert grid.status is CAL.ExpectationStatus.EXPECTED_GRID
        assert len(grid.slots) == 225
        assert grid.slots[0] == nano("2027-03-01T09:00:00+08:00")
        assert grid.slots[-1] == nano("2027-03-01T14:59:00+08:00")
        assert all(s % CAL.MINUTE_SLOT_NANO == 0 for s in grid.slots)
        assert grid.unknown_ranges == ()

    def test_fixed_slot_night_grid_crosses_midnight(self):
        """21:00 -> 02:30 is 330 minutes, last slot starts 02:29."""
        book = book_from("synthetic_session_grid_partial.json")
        grid = CAL.build_expected_slot_grid(
            book, dataset=DATASET, source_symbol=SOURCE_SYMBOL,
            window=window("2027-02-26T21:00:00+08:00", "2027-02-27T02:30:00+08:00"),
            as_of_nano=nano(AS_OF), generation=_fixed_slot_generation(60),
        )
        assert grid.status is CAL.ExpectationStatus.EXPECTED_GRID
        assert len(grid.slots) == 330
        assert grid.slots[-1] == nano("2027-02-27T02:29:00+08:00")

    def test_calendar_unknown_range_never_filled(self):
        """Window mixes an evidenced session with unevidenced time BEFORE the
        trading-day span: the grid covers only the verified part, the rest
        stays explicitly unknown (intra-span time is evidenced, pre-span
        Friday evening is not - partial grid, no weekday guessing)."""
        book = book_from("synthetic_session_grid_partial.json")
        grid = CAL.build_expected_slot_grid(
            book, dataset=DATASET, source_symbol=SOURCE_SYMBOL,
            window=window("2027-02-26T19:00:00+08:00", "2027-02-26T21:30:00+08:00"),
            as_of_nano=nano(AS_OF), generation=_fixed_slot_generation(60),
        )
        assert grid.status is CAL.ExpectationStatus.EXPECTATION_UNKNOWN
        assert len(grid.slots) == 30          # only 21:00-21:30 is verified
        assert len(grid.unknown_ranges) >= 1  # 19:00-21:00 stays unknown
        assert grid.unknown_ranges[0].start_nano == nano("2027-02-26T19:00:00+08:00")

    def test_generation_evidence_must_bind_the_task(self):
        book = book_from("synthetic_session_grid_partial.json")
        win = window("2027-03-01T09:00:00+08:00", "2027-03-01T10:00:00+08:00")
        with expect_error("E_CALENDAR_GENERATION_NOT_APPLICABLE"):
            CAL.build_expected_slot_grid(
                book, dataset=DATASET, source_symbol=SOURCE_SYMBOL, window=win,
                as_of_nano=nano(AS_OF), generation=_fixed_slot_generation(86400),
            )
        with expect_error("E_CALENDAR_GENERATION_NOT_APPLICABLE"):
            CAL.build_expected_slot_grid(
                book, dataset=DATASET, source_symbol="KQ.m@OTHER.xx", window=win,
                as_of_nano=nano(AS_OF), generation=_fixed_slot_generation(60),
            )

    def test_expected_daily_bars_use_session_spans(self):
        book = book_from("synthetic_session_grid_partial.json")
        grid = CAL.build_expected_daily_bars(
            book, dataset=DATASET, source_symbol=SOURCE_SYMBOL,
            window=window("2027-02-26T00:00:00+08:00", "2027-03-02T00:00:00+08:00"),
            as_of_nano=nano(AS_OF), generation=_fixed_slot_generation(86400),
        )
        assert grid.status is CAL.ExpectationStatus.EXPECTATION_UNKNOWN or (
            grid.status is CAL.ExpectationStatus.EXPECTED_GRID
        )
        days = dict(grid.daily_bars)
        assert "2027-03-01" in days
        span = days["2027-03-01"]
        assert span.start_nano == nano("2027-02-26T21:00:00+08:00")
        assert span.end_nano == nano("2027-03-01T15:00:00+08:00")
        assert span.end_nano - span.start_nano != 86400 * 10**9  # never natural day

    def test_expected_daily_bars_unknown_without_generation(self):
        book = book_from("synthetic_session_grid_complete_week.json")
        grid = CAL.build_expected_daily_bars(
            book, dataset=DATASET, source_symbol=SOURCE_SYMBOL,
            window=window("2027-03-03T00:00:00+08:00", "2027-03-04T00:00:00+08:00"),
            as_of_nano=nano(AS_OF), generation=None,
        )
        assert grid.status is CAL.ExpectationStatus.EXPECTATION_UNKNOWN
        assert grid.daily_bars == ()

    def test_expected_daily_bars_complete_week_enumerated(self):
        book = book_from("synthetic_session_grid_complete_week.json")
        grid = CAL.build_expected_daily_bars(
            book, dataset=DATASET, source_symbol=SOURCE_SYMBOL,
            window=window("2027-03-01T00:00:00+08:00", "2027-03-08T00:00:00+08:00"),
            as_of_nano=nano(AS_OF), generation=_fixed_slot_generation(86400),
        )
        assert grid.status is CAL.ExpectationStatus.EXPECTED_GRID
        assert [day for day, _span in grid.daily_bars] == [
            "2027-03-01", "2027-03-02", "2027-03-03", "2027-03-04", "2027-03-05",
        ]
        spans = [span for _day, span in grid.daily_bars]
        assert spans[0].start_nano == nano("2027-03-01T09:00:00+08:00")  # Monday day-only
        assert spans[1].start_nano == nano("2027-03-01T21:00:00+08:00")  # Tuesday owns Monday night


# ===========================================================================
# Port conformance and the Q05 masquerade rules
# ===========================================================================


class TestPortConformance:
    def test_sessions_signature_matches_frozen_protocol(self):
        provider_params = inspect.signature(P.CalendarProvider.sessions).parameters
        book_params = inspect.signature(CAL.CalendarBook.sessions).parameters
        assert list(book_params) == list(provider_params)
        for name, provider_param in provider_params.items():
            assert book_params[name].kind is provider_param.kind

    def test_sessions_returns_frozen_answer_type(self):
        book = book_from("synthetic_session_grid_partial.json")
        answer = book.sessions(
            dataset=DATASET, source_symbol=SOURCE_SYMBOL, duration_seconds=60,
            window=window("2027-03-01T09:00:00+08:00", "2027-03-01T15:00:00+08:00"),
            as_of_nano=nano(AS_OF),
        )
        assert isinstance(answer, P.CalendarAnswer)

    def test_unknown_answer_never_carries_sessions_or_fake_hash(self):
        book = book_from("synthetic_session_grid_partial.json")
        answer = book.sessions(
            dataset=DATASET, source_symbol=SOURCE_SYMBOL, duration_seconds=60,
            window=window("2027-03-01T09:00:00+08:00", "2027-03-01T15:00:00+08:00"),
            as_of_nano=nano("2027-03-02T20:00:00+08:00"),  # before obtained
        )
        assert answer.status is P.CalendarAnswerStatus.UNKNOWN
        assert answer.sessions == ()
        assert answer.evidence_sha256 is None

    def test_verified_answer_carries_hash_version_and_sessions(self):
        book = book_from("synthetic_session_grid_complete_week.json")
        answer = book.sessions(
            dataset=DATASET, source_symbol=SOURCE_SYMBOL, duration_seconds=60,
            window=window("2027-03-03T09:00:00+08:00", "2027-03-03T10:15:00+08:00"),
            as_of_nano=nano(AS_OF),
        )
        assert answer.status is P.CalendarAnswerStatus.VERIFIED_SESSIONS
        assert answer.evidence_sha256 == book.evidence_chain_sha256()
        assert answer.evidence_version
        assert len(answer.sessions) == 1

    def test_no_trading_answer_carries_hash_but_no_sessions(self):
        book = book_from("synthetic_closed_dates_2027.json")
        answer = book.sessions(
            dataset=DATASET, source_symbol=SOURCE_SYMBOL, duration_seconds=60,
            window=window("2027-02-16T00:00:00+08:00", "2027-02-17T00:00:00+08:00"),
            as_of_nano=nano(AS_OF),
        )
        assert answer.status is P.CalendarAnswerStatus.NO_TRADING_EVIDENCED
        assert answer.sessions == ()
        assert answer.evidence_sha256 is not None

    def test_conflict_answer_carries_conflict_refs(self):
        book = book_from(
            "synthetic_session_grid_partial.json", "synthetic_conflicting_grid.json"
        )
        answer = book.sessions(
            dataset=DATASET, source_symbol=SOURCE_SYMBOL, duration_seconds=60,
            window=window("2027-02-24T09:00:00+08:00", "2027-02-24T15:00:00+08:00"),
            as_of_nano=nano("2027-03-10T20:00:00+08:00"),
        )
        assert answer.status is P.CalendarAnswerStatus.CONFLICT
        assert len(answer.conflict_refs) == 2


# ===========================================================================
# V01/V02 calendar legs: instant identity + PIT visibility
# ===========================================================================


class TestIdentityAndPitLegs:
    def test_utc_and_shanghai_instants_same_answer(self):
        """V01 leg: the same instant in UTC vs +08:00 renders one trading day."""
        book = book_from("synthetic_session_grid_partial.json")
        utc_iso = "2027-02-26T13:00:00+00:00"          # == 2027-02-26 21:00 +08
        sh_iso = "2027-02-26T21:00:00+08:00"
        assert nano(utc_iso) == nano(sh_iso)
        as_of = nano(AS_OF)
        assert book.trading_day_for_instant(DATASET, SOURCE_SYMBOL, nano(utc_iso), as_of) == \
            book.trading_day_for_instant(DATASET, SOURCE_SYMBOL, nano(sh_iso), as_of) == "2027-03-01"

    def test_utc_and_shanghai_windows_same_partition(self):
        book = book_from("synthetic_session_grid_partial.json")
        w_local = window("2027-02-26T21:00:00+08:00", "2027-02-26T22:00:00+08:00")
        w_utc = window("2027-02-26T13:00:00+00:00", "2027-02-26T14:00:00+00:00")
        r_local = book.query(dataset=DATASET, source_symbol=SOURCE_SYMBOL,
                             duration_seconds=60, window=w_local, as_of_nano=nano(AS_OF))
        r_utc = book.query(dataset=DATASET, source_symbol=SOURCE_SYMBOL,
                           duration_seconds=60, window=w_utc, as_of_nano=nano(AS_OF))
        assert [v.canonical() for v in r_local.subranges] == [v.canonical() for v in r_utc.subranges]

    def test_evidence_later_in_time_does_not_rewrite_old_as_of(self):
        """V02/H04.5 leg: at as_of=03-19 the 03-10 day is UNKNOWN; at 03-21
        it is VERIFIED - the old UNKNOWN fact is preserved, not rewritten."""
        book = book_from("synthetic_session_grid_partial.json", "synthetic_later_version_grid.json")
        early = book.query(
            dataset=DATASET, source_symbol=SOURCE_SYMBOL, duration_seconds=60,
            window=window("2027-03-10T09:00:00+08:00", "2027-03-10T15:00:00+08:00"),
            as_of_nano=nano("2027-03-19T20:00:00+08:00"),
        )
        late = book.query(
            dataset=DATASET, source_symbol=SOURCE_SYMBOL, duration_seconds=60,
            window=window("2027-03-10T09:00:00+08:00", "2027-03-10T15:00:00+08:00"),
            as_of_nano=nano("2027-03-21T20:00:00+08:00"),
        )
        assert early.answer.status is P.CalendarAnswerStatus.UNKNOWN
        assert early.answer.sessions == ()
        assert late.answer.status is P.CalendarAnswerStatus.VERIFIED_SESSIONS
        assert late.answer.evidence_version and "synth-grid-r2" in late.answer.evidence_version

    def test_dataset_mismatch_is_unknown_not_empty_success(self):
        book = book_from("synthetic_session_grid_partial.json")
        result = book.query(
            dataset="other_dataset", source_symbol=SOURCE_SYMBOL, duration_seconds=60,
            window=window("2027-03-01T09:00:00+08:00", "2027-03-01T15:00:00+08:00"),
            as_of_nano=nano(AS_OF),
        )
        assert result.answer.status is P.CalendarAnswerStatus.UNKNOWN

    def test_symbol_outside_applicability_is_unknown(self):
        book = book_from("synthetic_session_grid_partial.json")
        result = book.query(
            dataset=DATASET, source_symbol="KQ.m@OTHER.xx", duration_seconds=60,
            window=window("2027-03-01T09:00:00+08:00", "2027-03-01T15:00:00+08:00"),
            as_of_nano=nano(AS_OF),
        )
        assert result.answer.status is P.CalendarAnswerStatus.UNKNOWN
        assert result.unknown_reasons == ("NO_CALENDAR_EVIDENCE_IN_DOMAIN",)


# ===========================================================================
# Real 2026 anchor (DP00 handoff, read-only, hash-pinned)
# ===========================================================================


def _real_anchor_documents():
    repo = _find_repo_root()
    dp00 = _find_dp00_tree()
    if repo is None or dp00 is None:
        pytest.skip("repo/DP00 tree not reachable from this layout")
    catalog_path = repo / "config" / "china_futures_catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    assert _sha256_file(catalog_path) == json.loads(
        (dp00 / "universe.json").read_text(encoding="utf-8")
    )["per_task_common"]["calendar_evidence"]["sha256"]
    observed_at = json.loads((dp00 / "universe.json").read_text(encoding="utf-8"))[
        "provenance"
    ]["products_catalog"]["observed_at"]
    return CAL.exchange_closed_dates_document_from_catalog(
        catalog,
        obtained_at_nano=nano(observed_at),
        evidence_reference="config/china_futures_catalog.json#/calendars (DP00 handoff, read-only)",
    ), catalog


class TestRealAnchor2026:
    def test_anchor_conversion_per_exchange(self):
        documents, catalog = _real_anchor_documents()
        assert len(documents) == len(catalog["calendars"])
        assert {d.applicability_exchanges[0] for d in documents} == {
            entry["exchange"] for entry in catalog["calendars"]
        }
        assert all(d.data_origin is C.DataOrigin.REAL_SOURCE for d in documents)
        assert all(d.review_status == "review_required" for d in documents)
        assert all(d.evidence_kind is CAL.EvidenceKind.EXCHANGE_CLOSED_DATES for d in documents)

    def test_anchor_holiday_window_is_no_trading_evidenced(self):
        documents, _catalog = _real_anchor_documents()
        book = CAL.CalendarBook(documents)
        shfe = next(d for d in documents if d.applicability_exchanges[0] == "SHFE")
        run_start, run_end_exclusive = _longest_closed_run(shfe)
        assert run_start is not None, "anchor must contain at least one closed date"
        result = book.query(
            dataset=DATASET, source_symbol="SHFE.rb2610", duration_seconds=60,
            window=window(f"{run_start}T00:00:00+08:00", f"{run_end_exclusive}T00:00:00+08:00"),
            as_of_nano=nano("2026-10-20T20:00:00+08:00"),
        )
        assert result.answer.status is P.CalendarAnswerStatus.NO_TRADING_EVIDENCED
        assert result.answer.sessions == ()
        assert result.answer.evidence_sha256 == book.evidence_chain_sha256()

    def test_anchor_open_day_is_unknown_not_no_trading(self):
        """Closed dates only: an open civil day carries NO session evidence
        and must answer UNKNOWN (never guessed from weekday)."""
        documents, _catalog = _real_anchor_documents()
        book = CAL.CalendarBook(documents)
        result = book.query(
            dataset=DATASET, source_symbol="SHFE.rb2610", duration_seconds=60,
            window=window("2026-09-21T09:00:00+08:00", "2026-09-21T15:00:00+08:00"),
            as_of_nano=nano("2026-10-20T20:00:00+08:00"),
        )
        assert result.answer.status is P.CalendarAnswerStatus.UNKNOWN
        assert "CLOSED_DATES_ONLY_SESSIONS_UNKNOWN" in result.unknown_reasons

    def test_anchor_weekend_not_listed_stays_unknown(self):
        """No weekday derivation: a weekend outside listed closed dates has
        no evidence and answers UNKNOWN (H04.4/D06)."""
        documents, catalog = _real_anchor_documents()
        book = CAL.CalendarBook(documents)
        shfe = next(d for d in documents if d.applicability_exchanges[0] == "SHFE")
        closed = {c.date for c in shfe.closed_dates}
        # 2026-11-14 is a Saturday, not in any closed list:
        assert "2026-11-14" not in closed
        result = book.query(
            dataset=DATASET, source_symbol="SHFE.rb2610", duration_seconds=60,
            window=window("2026-11-14T00:00:00+08:00", "2026-11-15T00:00:00+08:00"),
            as_of_nano=nano("2026-11-20T20:00:00+08:00"),
        )
        assert result.answer.status is P.CalendarAnswerStatus.UNKNOWN

    def test_anchor_pre_2026_is_unknown(self):
        documents, _catalog = _real_anchor_documents()
        book = CAL.CalendarBook(documents)
        result = book.query(
            dataset=DATASET, source_symbol="SHFE.rb2610", duration_seconds=60,
            window=window("2024-03-01T00:00:00+08:00", "2024-03-02T00:00:00+08:00"),
            as_of_nano=nano("2026-10-20T20:00:00+08:00"),
        )
        assert result.answer.status is P.CalendarAnswerStatus.UNKNOWN
        assert result.unknown_reasons == ("BEFORE_EVIDENCE_VALIDITY_PRE_2026_STYLE",)

    def test_anchor_invisible_before_catalog_observation(self):
        """H04.6: the anchor cannot inform as_of before it was possessed
        (catalog observed 2026-08-05)."""
        documents, _catalog = _real_anchor_documents()
        book = CAL.CalendarBook(documents)
        result = book.query(
            dataset=DATASET, source_symbol="SHFE.rb2610", duration_seconds=60,
            window=window("2026-10-01T00:00:00+08:00", "2026-10-02T00:00:00+08:00"),
            as_of_nano=nano("2026-01-05T20:00:00+08:00"),
        )
        assert result.answer.status is P.CalendarAnswerStatus.UNKNOWN
        assert "EVIDENCE_NOT_KNOWN_AT_AS_OF" in result.unknown_reasons


def _longest_closed_run(document) -> tuple:
    """Longest run of CONSECUTIVE closed civil dates in one anchor document.

    Returns (first_date, day_after_last_date) as ISO strings, both None when
    the document has no closed dates.  A window inside one consecutive run
    is evidenced no-trading; a run boundary (e.g. an unlisted weekend inside
    the national-day week) is NOT closed evidence and must stay UNKNOWN.
    """
    from datetime import date, timedelta

    days = sorted({c.date for c in document.closed_dates})
    if not days:
        return (None, None)
    best_start, best_end = days[0], days[0]
    run_start = days[0]
    previous = date.fromisoformat(days[0])
    for text in days[1:]:
        current = date.fromisoformat(text)
        if (current - previous).days == 1:
            previous = current
            continue
        if (previous - date.fromisoformat(run_start)).days > (
            date.fromisoformat(best_end) - date.fromisoformat(best_start)
        ).days:
            best_start, best_end = run_start, previous.isoformat()
        run_start = text
        previous = current
    if (previous - date.fromisoformat(run_start)).days > (
        date.fromisoformat(best_end) - date.fromisoformat(best_start)
    ).days:
        best_start, best_end = run_start, previous.isoformat()
    end_exclusive = (date.fromisoformat(best_end) + timedelta(days=1)).isoformat()
    return (best_start, end_exclusive)


# ===========================================================================
# Schema anti-drift: config schema vs native validator
# ===========================================================================


class TestSchemaAntiDrift:
    def _schema(self) -> dict:
        schema_path = REPO_ROOT / "config" / "datapipeline" / "calendar-evidence.schema.json"
        assert schema_path.is_file(), "calendar-evidence.schema.json must ship with the node"
        return json.loads(schema_path.read_text(encoding="utf-8"))

    def test_schema_required_fields_match_validator(self):
        schema = self._schema()
        required = set(schema["required"])
        # Fields the native validator hard-requires at envelope level:
        native_required = {
            "schema_version", "evidence_version", "dataset", "source_id",
            "evidence_kind", "data_origin", "applicability",
            "valid_from_nano", "valid_through_nano", "pit_basis",
        }
        assert required == native_required

    def test_schema_enums_match_module(self):
        schema = self._schema()
        props = schema["properties"]
        assert set(props["evidence_kind"]["enum"]) == {e.value for e in CAL.EvidenceKind}
        assert set(props["data_origin"]["enum"]) == {e.value for e in C.DataOrigin}
        assert set(props["pit_basis"]["enum"]) == {e.value for e in CAL.PitBasis}
        assert set(props["coverage"]["enum"]) == {e.value for e in CAL.CoverageDeclaration}
        assert props["schema_version"]["const"] == CAL.CALENDAR_EVIDENCE_SCHEMA_VERSION

    def test_every_fixture_has_a_synthetic_marker(self):
        for path in sorted(FIXTURES.glob("synthetic_*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            assert payload.get("data_origin") == "SYNTHETIC", path
            assert payload.get("synthetic_marker") and "SYNTHETIC" in payload["synthetic_marker"], path
            # every synthetic fixture validates through the strict envelope
            CAL.calendar_evidence_document_from_mapping(payload)


# ===========================================================================
# Import purity (H01.6-style): importing calendar performs no I/O
# ===========================================================================


class TestImportPurity:
    def test_import_performs_no_io(self, tmp_path):
        probe = tmp_path / "import_probe.py"
        probe.write_text(
            "import sys\n"
            "violations = []\n"
            "def hook(event, args):\n"
            "    if event in ('socket.connect', 'subprocess.Popen', 'urllib.Request'):\n"
            "        violations.append((event, str(args)))\n"
            "    if event == 'open':\n"
            "        path = str(args[0]) if args else ''\n"
            "        if path.endswith('.env') or 'maintenance_artifacts' in path:\n"
            "            violations.append((event, path))\n"
            "sys.addaudithook(hook)\n"
            "from ak_trading.datapipeline import calendar\n"
            "assert not violations, f'forbidden IO during import: {violations}'\n"
            "print('PURE')\n",
            encoding="utf-8",
        )
        import os

        staging_src = Path(CAL.__file__).resolve().parents[2]
        result = subprocess.run(
            [sys.executable, "-B", str(probe)],
            capture_output=True, text=True, cwd=str(tmp_path),
            env={**os.environ, "PYTHONPATH": str(staging_src)},
        )
        assert result.returncode == 0, result.stderr
        assert "PURE" in result.stdout

    def test_runtime_module_has_no_write_calls(self):
        """The DP04 runtime surface is a pure evidence consumer (02
        blueprint): the module must not even expose a write helper."""
        source = Path(CAL.__file__).read_text(encoding="utf-8")
        for forbidden in ("open(", ".write_text(", ".write_bytes(", "os.remove", "shutil."):
            assert forbidden not in source, f"runtime file-write surface: {forbidden}"
