"""Offline boundary tests for the frozen-inventory readiness inspector."""
from __future__ import annotations

import copy
import json
from dataclasses import fields, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from ak_trading.futures.data import ActionInputs
from ak_trading.futures.io import InputBundle, canonical_json_bytes
from ak_trading.futures.readiness import inspect_readiness

from ak_trading.futures.spec import ContractBar, ReadinessAction

SH = timezone(timedelta(hours=8))
AS_OF = datetime(2026, 9, 5, tzinfo=SH)
PRODUCT = "SHFE.RB"
SYMBOL = "SHFE.rb2701"
EXPIRY = "2027-01-15T15:00:00+08:00"
INSTANCE = f"{SYMBOL}|{EXPIRY}"
STRICT_FIELDS = (
    "historical_session_windows", "trading_day_source_verified",
    "parameter_available_at", "last_trade_at", "delivery_cutoff_at",
    "multiplier", "price_tick", "fee_basis_close_today",
    "margin_basis_settlement_price", "limit_bands",
)


def _bar(**changes) -> ContractBar:
    bar = ContractBar(
        symbol=SYMBOL, duration_seconds=86400, datetime_nano=1_788_451_200_000_000_000,
        ts=datetime(2026, 9, 4, tzinfo=SH), source="frozen_offline_fixture",
        trading_day=date(2026, 9, 4), trading_day_source="reference_calendar_date",
        available_at=datetime(2026, 9, 4, 16, tzinfo=SH),
        open=Decimal("3000"), high=Decimal("3010"), low=Decimal("2990"),
        close=Decimal("3005"), volume=Decimal("1000"),
    )
    return replace(bar, **changes)


def _contract(**changes) -> dict:
    payload = {
        "instance_id": INSTANCE, "raw_symbol": SYMBOL, "product": PRODUCT,
        "expiry_at": EXPIRY, "identity_status": "reconstructed_static",
        "listing_at": None, "listing_flag": "listing_history_unknown",
        "identity_issues": ["listing_at_missing"],
    }
    return {**payload, **changes}


def _bundle(*, contracts=None, bars=None, strict_status="missing_local_approved_evidence") -> InputBundle:
    return InputBundle(
        # Deliberately nonexistent: inspect_readiness must never read it.
        root=Path("/no-filesystem-access/readiness-fixture"),
        manifest={
            "schema_version": "futures_bundle_v1", "status": "complete",
            "dataset_kind": "real_export",
            "request": {
                "products": [PRODUCT], "start": "2026-09-01T00:00:00+08:00",
                "end": AS_OF.isoformat(), "as_of": AS_OF.isoformat(),
                "duration_seconds": 86400, "request_digest": "fixture-request",
            },
            "content_hashes": {"bars.csv": "declared-not-verified"},
            "source_hashes": {"universe_cache": "fixture-cache"},
        },
        contracts=tuple([_contract()] if contracts is None else contracts),
        bars=tuple([_bar()] if bars is None else bars),
        metadata={"fields": {}, "revision_history_unavailable": True},
        availability={"products": {PRODUCT: {"strict_fields": {
            name: {"status": strict_status, "reason": "no approved historical evidence"}
            for name in STRICT_FIELDS
        }}}},
    )


def _row(report: dict) -> dict:
    return report["products"][PRODUCT]["contracts"][0]


def test_frozen_inventory_is_explicitly_unavailable_but_observations_can_pass():
    report = inspect_readiness(_bundle())
    row = _row(report)
    assert report["schema_version"] == "futures_readiness_v1"
    assert report["basis"] == "frozen_inventory"
    assert report["status"] == report["execution_status"] == "unavailable"
    assert report["strict_execution_enabled"] is False
    assert report["historical_event_time"] is None
    assert report["historical_event_time_reason"]
    assert report["calendar"]["status"] == "unavailable"
    assert report["scope"]["as_of"] == AS_OF.isoformat()
    assert report["counts"] == {
        "bar_rows": 1, "contracts": 1, "products": 1, "contracts_with_bars": 1,
        "contracts_without_bars": 0, "unresolved_contracts": 0, "unassigned_bar_rows": 0,
    }
    assert row["contract_instance_id"] == INSTANCE
    assert row["actions"]["observe"]["status"] == "ready"
    assert row["actions"]["signal"]["status"] == "blocked"
    assert set(row["field_evidence"]) == {field.name for field in fields(ActionInputs)}
    for action in report["strict_actions"]:
        assert row["actions"][action]["status"] == "blocked"
        assert row["actions"][action]["missing_fields"]
        assert row["actions"][action]["reasons"]
    assert report["actions"]["observe"]["ready_count"] == 1
    strict = report["strict_action_aggregate"]
    assert strict["status"] == "blocked"
    assert strict["ready_count"] == 0
    assert strict["blocked_count"] == strict["requested_count"] == 5
    assert "observe" not in report["strict_actions"]


def test_exporter_status_is_normalized_to_missing_with_original_reason():
    report = inspect_readiness(_bundle())
    evidence = _row(report)["field_evidence"]
    for field in ("sessions", "trading_day_source", "parameter_availability", "lifecycle", "fees", "margin", "settlement", "multiplier", "price_tick", "limits"):
        assert evidence[field]["status"] == "missing"
        assert "missing_local_approved_evidence" in evidence[field]["reason"]
    assert "multiplier" in _row(report)["actions"]["open_long"]["missing_fields"]
    assert report["products"][PRODUCT]["source_fields"]["multiplier"]["source_status"] == "missing_local_approved_evidence"


@pytest.mark.parametrize("status", ["ready", "approved", "fixture_valid", "bogus", "not_requested", "", None])
def test_unknown_or_non_evidence_status_cannot_bypass_gate(status):
    report = inspect_readiness(_bundle(strict_status=status))
    field = _row(report)["field_evidence"]["multiplier"]
    assert field["status"] == "invalid"
    assert "unsupported_evidence_status" in field["reason"]
    assert "multiplier" in _row(report)["actions"]["open_long"]["invalid_fields"]
    assert report["strict_action_aggregate"]["status"] == "blocked"


@pytest.mark.parametrize("provide_claimed_provenance", [False, True])
def test_fake_approved_metadata_and_statuses_never_enable_execution(provide_claimed_provenance):
    bundle = _bundle(strict_status="present_approved")
    for claim in bundle.availability["products"][PRODUCT]["strict_fields"].values():
        claim["evidence_id"] = "unverified-document"
        if provide_claimed_provenance:
            claim.update({
                "source_sha256": "a" * 64, "source_id": "claimed-authority",
                "available_at": "2026-01-01T00:00:00+08:00",
                "effective_from": "2026-01-01T00:00:00+08:00",
                "effective_through": "2027-01-01T00:00:00+08:00", "basis": "claimed",
            })
    bundle.metadata["fields"] = {
        "multiplier": {"value": "10", "approval_status": "approved"},
        "currency": {"value": "CNY", "approval_status": "approved"},
        "lot_rules": {"value": 1, "approval_status": "approved"},
    }
    report = inspect_readiness(bundle)
    evidence = _row(report)["field_evidence"]
    assert evidence["multiplier"]["status"] == "present_unapproved"
    assert "approval_claim_unverified" in evidence["multiplier"]["reason"]
    assert evidence["currency"]["status"] == "missing"
    assert "unverified-document" in _row(report)["actions"]["open_long"]["evidence_ids"]
    assert all(_row(report)["actions"][action]["status"] == "blocked" for action in report["strict_actions"])
    assert report["strict_execution_enabled"] is False


def test_unverified_not_applicable_basis_cannot_skip_required_field():
    bundle = _bundle(strict_status="not_applicable")
    bundle.availability["products"][PRODUCT]["strict_fields"]["multiplier"]["basis"] = "self-declared exemption"
    report = inspect_readiness(bundle)
    assert _row(report)["field_evidence"]["multiplier"]["status"] == "present_unapproved"
    assert "multiplier" in _row(report)["actions"]["open_long"]["missing_fields"]


@pytest.mark.parametrize("changes,expected", [
    ({"instance_id": None}, "missing"),
    ({"raw_symbol": None}, "missing"),
    ({"instance_id": SYMBOL}, "invalid"),
    ({"instance_id": f"{SYMBOL}|2027-02-01T00:00:00+08:00"}, "invalid"),
    ({"expiry_at": "2027-01-15T15:00:00"}, "invalid"),
    ({"raw_symbol": "DCE.m2701"}, "invalid"),
])
def test_missing_or_malformed_identity_is_preserved_and_blocks_strict_actions(changes, expected):
    report = inspect_readiness(_bundle(contracts=[_contract(**changes)]))
    row = _row(report)
    assert report["counts"]["contracts"] == 1
    assert row["contract_instance_id"] == changes.get("instance_id", INSTANCE)
    assert row["field_evidence"]["identity"]["status"] == expected
    for action in report["strict_actions"]:
        assert row["actions"][action]["status"] == "blocked"
        assert "identity" in row["actions"][action][f"{expected}_fields"]


@pytest.mark.parametrize("product", [None, "SHFE", "NYSE.RB", "SHFE.CU"])
def test_unresolvable_product_keeps_contract_without_fabricated_identity(product):
    report = inspect_readiness(_bundle(contracts=[_contract(product=product)]))
    assert report["counts"]["contracts"] == report["counts"]["unresolved_contracts"] == 1
    row = report["unresolved_contracts"][0]
    assert row["contract_instance_id"] == INSTANCE
    assert row["product"] is None
    assert all(result["status"] == "blocked" for result in row["actions"].values())
    assert report["strict_action_aggregate"]["blocked_count"] == 5


def test_ambiguous_raw_symbol_is_not_assigned_to_multiple_contract_instances():
    second = _contract(instance_id=f"{SYMBOL}|2037-01-15T15:00:00+08:00", expiry_at="2037-01-15T15:00:00+08:00")
    report = inspect_readiness(_bundle(contracts=[_contract(), second]))
    assert report["counts"]["bar_rows"] == report["counts"]["unassigned_bar_rows"] == 1
    assert report["counts"]["contracts_without_bars"] == 2
    for row in report["products"][PRODUCT]["contracts"]:
        assert row["bar_rows"] == 0
        assert row["field_evidence"]["identity"]["status"] == "invalid"
        assert "ambiguous_raw_symbol_instances" in row["field_evidence"]["identity"]["reason"]


def test_missing_values_are_evidence_with_reasons_not_zero_defaults():
    report = inspect_readiness(_bundle(bars=[]))
    row = _row(report)
    assert row["bar_rows"] == 0  # a measured count, not invented market data
    for name in ("bar_validity", "bar_availability", "multiplier", "currency", "lot_rules", "fees"):
        assert row["field_evidence"][name]["status"] == "missing"
        assert row["field_evidence"][name]["reason"]
        assert "value" not in row["field_evidence"][name]
    assert row["listing_at"] is None
    assert row["actions"]["observe"]["status"] == "blocked"
    assert report["historical_event_time"] is None


def test_clock_presence_does_not_establish_historical_availability():
    late = _bar(available_at=AS_OF + timedelta(hours=1), session_open=datetime(2026, 9, 4, 9, tzinfo=SH), session_close=datetime(2026, 9, 4, 15, tzinfo=SH))
    report = inspect_readiness(_bundle(bars=[late]))
    assert report["clock_summary"]["available_at_after_as_of"] == 1
    assert report["clock_summary"]["reference_calendar_date_rows"] == 1
    assert report["clock_summary"]["session_close_present"] == 1
    field = _row(report)["field_evidence"]["bar_availability"]
    assert field["status"] == "unknown"
    assert "bars_available_after_request_as_of" in field["reason"]
    assert _row(report)["field_evidence"]["sessions"]["status"] == "missing"
    assert report["calendar"]["status"] == "unavailable"


def test_naive_availability_and_reversed_sessions_are_invalid():
    report = inspect_readiness(_bundle(bars=[_bar(
        available_at=datetime(2026, 9, 4),
        session_open=datetime(2026, 9, 4, 15, tzinfo=SH),
        session_close=datetime(2026, 9, 4, 9, tzinfo=SH),
    )]))
    row = _row(report)
    assert row["field_evidence"]["bar_availability"]["status"] == "invalid"
    assert row["field_evidence"]["sessions"]["status"] == "invalid"
    assert {"sessions", "bar_availability"} <= set(row["actions"]["open_long"]["invalid_fields"])


@pytest.mark.parametrize("change", [
    {"close": None}, {"close": Decimal("NaN")}, {"volume": Decimal("-1")},
    {"open": True}, {"high": Decimal("1")},
])
def test_invalid_observation_values_block_observe(change):
    report = inspect_readiness(_bundle(bars=[_bar(**change)]))
    assert _row(report)["actions"]["observe"]["status"] == "blocked"
    assert _row(report)["field_evidence"]["bar_validity"]["status"] == "invalid"
    assert report["clock_summary"]["invalid_bar_rows"] == 1


def test_duplicate_bars_are_reported_once_per_contract_without_row_dump():
    bar = _bar()
    report = inspect_readiness(_bundle(bars=[bar, bar]))
    assert report["clock_summary"]["duplicate_bar_key"] == 1
    assert report["counts"]["bar_rows"] == 2
    assert len(report["products"][PRODUCT]["contracts"]) == 1
    assert _row(report)["actions"]["observe"]["status"] == "blocked"
    assert "bars" not in _row(report)


def test_action_matrix_remains_the_existing_action_specific_contract():
    actions = _row(inspect_readiness(_bundle()))["actions"]
    assert "margin" in actions["open_long"]["missing_fields"]
    assert "margin" not in actions["close_history"]["missing_fields"]
    assert "bar_history" not in actions["close_history"]["missing_fields"]
    assert "price_tick" not in actions["value"]["missing_fields"]
    assert "trading_day_source" not in actions["close_history"]["missing_fields"]
    assert "trading_day_source" in actions["close_today"]["missing_fields"]
    assert set(actions) == {action.value for action in ReadinessAction}


def test_json_is_deterministic_and_input_is_not_mutated(monkeypatch):
    other = _contract(instance_id="SHFE.rb2705|2027-05-15T15:00:00+08:00", raw_symbol="SHFE.rb2705", expiry_at="2027-05-15T15:00:00+08:00")
    bundle = _bundle(contracts=[other, _contract()], bars=[_bar(symbol="SHFE.rb2705"), _bar()])
    before = copy.deepcopy(bundle)

    def forbidden(*args, **kwargs):
        raise AssertionError("pure inspector attempted filesystem I/O")

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    report = inspect_readiness(bundle)
    assert bundle == before
    encoded = canonical_json_bytes(report)
    assert json.loads(json.dumps(report, allow_nan=False)) == report
    assert encoded == canonical_json_bytes(inspect_readiness(bundle))
    reordered = replace(bundle, contracts=tuple(reversed(bundle.contracts)), bars=tuple(reversed(bundle.bars)))
    assert encoded == canonical_json_bytes(inspect_readiness(reordered))
    assert str(bundle.root).encode() not in encoded
    assert report["input_references"]["hash_verification"] == "caller_owned_not_performed_by_inspector"


@pytest.mark.parametrize("as_of", [None, "2026-09-05", "not-a-date", "2026-09-05T00:00:00"])
def test_explicit_aware_as_of_is_required_without_clock_fallback(as_of):
    bundle = _bundle()
    bundle.manifest["request"]["as_of"] = as_of
    with pytest.raises(ValueError, match="explicit timezone-aware request.as_of"):
        inspect_readiness(bundle)


def test_scope_limits_fail_explicitly_instead_of_truncating():
    with pytest.raises(ValueError, match="scope_limit"):
        inspect_readiness(_bundle(contracts=[_contract()] * 601))


def test_empty_inventory_never_becomes_ready():
    report = inspect_readiness(_bundle(contracts=[], bars=[]))
    assert report["counts"]["contracts"] == report["counts"]["bar_rows"] == 0
    assert report["strict_action_aggregate"]["status"] == "blocked"
    assert report["strict_action_aggregate"]["requested_count"] == 0
    assert report["execution_status"] == "unavailable"
