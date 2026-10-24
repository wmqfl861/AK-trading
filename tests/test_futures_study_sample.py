"""Offline tests for retrospective selection and raw observation preservation."""
from __future__ import annotations

import builtins
import io
import json
import os
import socket
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from ak_trading.futures.data import FIXED_UNIVERSE
from ak_trading.futures.io import BUNDLE_SCHEMA_VERSION, InputBundle
from ak_trading.futures.spec import ContractBar
from ak_trading.futures.study_sample import select_study_sample

_SH = timezone(timedelta(hours=8))
_EXPIRY = "2026-05-15T15:00:00+08:00"
_NANO = 1_700_000_000_000_000_001


def _contract(raw="SHFE.rb2605", product="SHFE.RB", expiry=_EXPIRY, **changes):
    result = {
        "instance_id": f"{raw}|{expiry}",
        "raw_symbol": raw,
        "product": product,
        "expiry_at": expiry,
        "identity_status": "reconstructed_static",
    }
    result.update(changes)
    return result


def _bars(raw="SHFE.rb2605", count=120):
    return tuple(
        ContractBar(
            symbol=raw,
            duration_seconds=86400,
            datetime_nano=_NANO + index,
            ts=datetime(2025, 1, 1, tzinfo=_SH),
            source=f"stored:{raw}",
            open=Decimal("100.00"),
            high=Decimal("102"),
            low=Decimal("99"),
            close=Decimal("101"),
            volume=Decimal("1000"),
        )
        for index in range(count)
    )


def _bundle(contracts=(), bars=(), **manifest_changes):
    manifest = {
        "dataset_kind": "real_export",
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "status": "complete",
    }
    manifest.update(manifest_changes)
    return InputBundle(
        root=Path("unused-in-memory-study-input"),
        manifest=manifest,
        bars=tuple(bars),
        contracts=tuple(contracts),
        metadata={},
        availability={},
    )


def _json(value):
    return json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":"))


def test_selects_three_per_fixed_product_by_expiry_not_outcomes_or_input_order():
    contracts = []
    bars = []
    expected = []
    raw_prefixes = ("SHFE.rb", "INE.sc", "DCE.m", "CZCE.TA", "CFFEX.IF", "GFEX.si")
    for product, prefix in zip(FIXED_UNIVERSE, raw_prefixes):
        for month in (9, 5, 3, 1):
            delivery = f"6{month:02}" if product.exchange == "CZCE" else f"26{month:02}"
            raw = prefix + delivery
            contracts.append(_contract(raw, str(product), f"2026-{month:02}-15T15:00:00+08:00"))
            # The earliest contract has invalid prices throughout. Selection
            # must still count its 120 stored observations, without filtering.
            history = _bars(raw)
            if month == 1:
                history = tuple(replace(bar, open=None, close=None) for bar in history)
            bars.extend(history)
        expected.extend(prefix + (f"6{month:02}" if product.exchange == "CZCE" else f"26{month:02}") for month in (1, 3, 5))
    sample = select_study_sample(_bundle(reversed(contracts), reversed(bars)))
    assert [contract.raw_symbol for contract in sample.selected] == expected
    assert len(sample.universe) == 24
    assert len(sample.inventory) == 24
    assert sum(row["selected"] for row in sample.inventory) == 18
    assert sample.config["products"] == [str(product) for product in FIXED_UNIVERSE]
    assert sample.config["counts"]["selected_bar_rows"] == 18 * 120
    assert sample.config["reason_counts"]["product_cap"] == 6
    assert all(counts["selected_contracts"] == 3 for counts in sample.config["per_product"].values())


def test_missing_and_short_histories_stay_in_universe_for_far_legs():
    near = _contract()
    short = _contract("SHFE.rb2609", expiry="2026-09-15T15:00:00+08:00")
    empty = _contract("SHFE.rb2701", expiry="2027-01-15T15:00:00+08:00")
    bars = _bars() + _bars(short["raw_symbol"], 119)
    sample = select_study_sample(_bundle([empty, short, near], bars))
    assert [contract.raw_symbol for contract in sample.selected] == [near["raw_symbol"]]
    assert [len(contract.bars) for contract in sample.universe] == [120, 119, 0]
    inventory = {row["raw_symbol"]: row for row in sample.inventory}
    assert inventory[short["raw_symbol"]]["reason"] == "insufficient_rows"
    assert inventory[short["raw_symbol"]]["bar_rows"] == 119
    assert inventory[empty["raw_symbol"]]["reason"] == "no_bars"
    assert inventory[empty["raw_symbol"]]["bar_rows"] == 0
    assert inventory[empty["raw_symbol"]]["selected"] is False
    assert sample.config["counts"]["universe_contracts"] == 3


def test_raw_symbol_with_multiple_instances_is_excluded_without_merging():
    raw = "CZCE.TA605"
    first = _contract(raw, "CZCE.TA")
    second = _contract(raw, "CZCE.TA", "2036-05-15T15:00:00+08:00")
    sample = select_study_sample(_bundle([second, first], _bars(raw)))
    assert sample.selected == sample.universe == ()
    assert len(sample.inventory) == 2
    assert {row["contract_instance_id"] for row in sample.inventory} == {first["instance_id"], second["instance_id"]}
    assert all(row["status"] == "excluded" and not row["selected"] for row in sample.inventory)
    assert all(row["reason"] == "ambiguous_raw_symbol_instances" for row in sample.inventory)
    assert all(row["bar_rows"] == 120 for row in sample.inventory)
    assert sample.config["counts"]["ambiguous_bar_rows"] == 120
    assert sample.config["counts"]["orphan_bar_rows"] == 0


def test_invalid_duplicate_declaration_cannot_resolve_ambiguous_raw_identity():
    valid = _contract()
    malformed = _contract(instance_id=None)
    sample = select_study_sample(_bundle([valid, malformed], _bars()))
    assert sample.universe == ()
    assert all(row["reason"] == "ambiguous_raw_symbol_instances" for row in sample.inventory)


def test_duplicate_full_instance_declarations_are_reported_separately():
    declaration = _contract()
    sample = select_study_sample(_bundle([declaration, declaration], _bars()))
    assert sample.universe == sample.selected == ()
    assert len(sample.inventory) == 2
    assert all(row["reason"] == "duplicate_instance_id" for row in sample.inventory)


def test_unordered_input_has_identical_selection_inventory_and_json_config():
    # Equal instants, different saved offsets: sort chronologically before
    # applying the full instance ID tie-break, not by timestamp text or symbol.
    first = _contract("SHFE.rb2601", expiry="2026-06-01T00:00:00+08:00")
    earlier = _contract("SHFE.rb2609", expiry="2026-05-31T15:00:00+00:00")
    tied = _contract("SHFE.rb2605", expiry="2026-05-31T16:00:00+00:00")
    metadata = [first, earlier, tied]
    bars = tuple(bar for contract in metadata for bar in _bars(contract["raw_symbol"]))
    one = select_study_sample(_bundle(metadata, bars), max_contracts_per_product=2)
    reordered = [{key: value for key, value in reversed(list(contract.items()))} for contract in reversed(metadata)]
    two = select_study_sample(_bundle(reordered, reversed(bars)), max_contracts_per_product=2)
    assert one == two
    assert [contract.raw_symbol for contract in one.selected] == [earlier["raw_symbol"], first["raw_symbol"]]
    assert _json(one.config) == _json(two.config)
    assert _json(one.inventory) == _json(two.inventory)
    note = one.config["bias_note"].lower()
    assert "retrospective" in note and "coverage-convenience" in note
    assert "not survivorship-free" in note


def test_invalid_prices_are_preserved_exactly_and_nanos_do_not_lose_precision():
    history = list(_bars())
    history[0] = replace(history[0], open=None, close=None, volume=None)
    history[1] = replace(history[1], open=Decimal("-0.00"), high=Decimal("-1"), low=Decimal("5"), close=Decimal("-2"))
    history[2] = replace(history[2], open=Decimal("NaN"), close=Decimal("Infinity"), volume=Decimal("-10"))
    history[3] = replace(history[3], close=Decimal("sNaN"), close_oi=Decimal("NaN"))
    bundle = _bundle([_contract()], reversed(history))
    original_input = bundle.bars
    sample = select_study_sample(bundle)
    ordered = sample.selected[0].bars
    assert len(ordered) == 120
    assert all(actual is original for actual, original in zip(ordered, history))
    assert [bar.datetime_nano for bar in ordered] == list(range(_NANO, _NANO + 120))
    assert ordered[1].open.as_tuple() == Decimal("-0.00").as_tuple()
    assert ordered[2].open.is_nan() and ordered[3].close.is_snan()
    assert bundle.bars is original_input
    assert _json(sample.config)


@pytest.mark.parametrize("declarations", [[], [_contract()], [_contract(), _contract(expiry="2036-05-15T15:00:00+08:00")]])
@pytest.mark.parametrize("different_source", [False, True])
def test_duplicate_nanos_raise_even_for_orphan_or_ambiguous_symbols(declarations, different_source):
    first = _bars(count=1)[0]
    duplicate = replace(first, source="another-source" if different_source else first.source, close=None)
    with pytest.raises(ValueError, match="duplicate datetime_nano.*SHFE.rb2605"):
        select_study_sample(_bundle(declarations, [first, duplicate]))


@pytest.mark.parametrize("nano", [True, False, 1.0, "1700000000000000001", None])
def test_nanos_require_integers_without_coercion(nano):
    bar = replace(_bars(count=1)[0], datetime_nano=nano)
    with pytest.raises(ValueError, match="datetime_nano must be an integer"):
        select_study_sample(_bundle([_contract()], [bar]))


def test_same_nano_on_different_raw_symbols_is_valid():
    other = _contract("DCE.m2605", "DCE.M")
    sample = select_study_sample(_bundle([_contract(), other], _bars() + _bars(other["raw_symbol"])))
    assert len(sample.selected) == 2


@pytest.mark.parametrize("raw", [
    "KQ.m@SHFE.rb", "KQ.i@SHFE.rb", "SHFE.rb2605C3200",
    "SHFE.rb2605-C-3200", "SHFE.rb", "SHFE.rb0000", "SHFE.rb2613",
    "SHFE.rb605", "SHFE.rb2605&SHFE.rb2609", "SHFE.rb２６０５", "DCE.rb2605",
    "SHFE.cu2605", "SHFE.rb2605 ",
])
def test_nonfuture_or_mismatched_raw_shape_is_excluded(raw):
    sample = select_study_sample(_bundle([_contract(raw)], _bars(raw)))
    assert sample.universe == sample.selected == ()
    assert sample.inventory[0]["reason"] == "invalid_future_symbol_or_product_mismatch"
    assert sample.inventory[0]["bar_rows"] == 120


@pytest.mark.parametrize(("changes", "reason"), [
    ({"raw_symbol": None}, "missing_raw_symbol"),
    ({"product": None}, "invalid_product"),
    ({"product": "SHFE.rb2605"}, "invalid_product"),
    ({"product": "SHFE.CU"}, "outside_fixed_universe"),
    ({"product": "DCE.M"}, "invalid_future_symbol_or_product_mismatch"),
    ({"expiry_at": None}, "invalid_or_naive_expiry"),
    ({"expiry_at": "2026-05-15T15:00:00"}, "invalid_or_naive_expiry"),
    ({"expiry_at": "not-an-expiry"}, "invalid_or_naive_expiry"),
    ({"instance_id": None}, "missing_instance_id"),
    ({"instance_id": "SHFE.rb2605"}, "instance_id_mismatch"),
    ({"instance_id": "SHFE.rb2605|2036-05-15T15:00:00+08:00"}, "instance_id_mismatch"),
    ({"instance_id": "DCE.m2605|2026-05-15T15:00:00+08:00"}, "instance_id_mismatch"),
])
def test_invalid_metadata_stays_in_inventory_with_reason(changes, reason):
    sample = select_study_sample(_bundle([_contract(**changes)], _bars()))
    assert sample.selected == sample.universe == ()
    assert len(sample.inventory) == 1
    assert sample.inventory[0]["reason"] == reason
    assert sample.inventory[0]["selected"] is False
    assert _json(sample.inventory) and _json(sample.config)


def test_malformed_declarations_are_counted_and_json_safe():
    sample = select_study_sample(_bundle([None, {}, {"raw_symbol": ["bad"]}]))
    assert len(sample.inventory) == 3
    assert sample.config["counts"]["invalid_metadata_contracts"] == 3
    assert _json(sample.inventory)


def test_product_keys_and_case_normalize_without_changing_raw_identity():
    contract = _contract(product="shfe.rb")
    first = select_study_sample(_bundle([contract], _bars()))
    second = select_study_sample(_bundle([_contract(product=FIXED_UNIVERSE[0])], _bars()))
    assert first == second
    assert first.selected[0].product == "SHFE.RB"
    assert first.selected[0].raw_symbol == "SHFE.rb2605"
    assert first.selected[0].contract_instance_id == contract["instance_id"]


def test_unapproved_reconstructed_identity_is_sufficient_for_retrospective_sample():
    declaration = _contract(identity_status="unapproved")
    sample = select_study_sample(_bundle([declaration], _bars()))
    assert len(sample.selected) == 1


@pytest.mark.parametrize("contracts", [[], [_contract()]])
def test_no_data_has_explicit_counts_without_replacing_fixed_products(contracts):
    sample = select_study_sample(_bundle(contracts))
    assert sample.selected == ()
    assert len(sample.universe) == len(contracts)
    assert len(sample.inventory) == len(contracts)
    assert sample.config["status"] == "no_data"
    assert sample.config["counts"]["input_bar_rows"] == 0
    assert sample.config["counts"]["selected_contracts"] == 0
    assert len(sample.config["per_product"]) == 6
    assert all(counts["selected_contracts"] == 0 for counts in sample.config["per_product"].values())


def test_orphan_rows_count_separately_without_inventory_or_universe_entries():
    orphan = "DCE.m2609"
    sample = select_study_sample(_bundle([_contract()], _bars() + _bars(orphan, 7)))
    assert len(sample.inventory) == len(sample.universe) == len(sample.selected) == 1
    assert sample.config["orphan_symbols"] == [orphan]
    assert sample.config["counts"]["orphan_raw_symbols"] == 1
    assert sample.config["counts"]["orphan_bar_rows"] == 7
    assert sample.config["counts"]["input_bar_rows"] == 127


@pytest.mark.parametrize(("manifest", "error"), [
    ({"dataset_kind": "demo"}, "real_export"),
    ({"dataset_kind": "synthetic"}, "real_export"),
    ({"dataset_kind": None}, "real_export"),
    ({"schema_version": "normalized_research_v1"}, "schema_version"),
    ({"status": "incomplete"}, "complete"),
])
def test_rejects_nonreal_or_invalid_input_manifest(manifest, error):
    with pytest.raises(ValueError, match=error):
        select_study_sample(_bundle(**manifest))


@pytest.mark.parametrize("value", [0, 4, -1, True, 1.0, "3", None])
def test_cap_must_be_integer_one_to_three(value):
    with pytest.raises(ValueError, match="max_contracts_per_product"):
        select_study_sample(_bundle(), max_contracts_per_product=value)


@pytest.mark.parametrize("value", [0, 119, 10_001, True, 120.0, "120", None])
def test_min_rows_must_be_integer_120_to_10000(value):
    with pytest.raises(ValueError, match="min_rows"):
        select_study_sample(_bundle(), min_rows=value)


@pytest.mark.parametrize("cap", [1, 2, 3])
def test_requested_cap_limits_selected_but_never_universe(cap):
    contracts = [_contract(f"SHFE.rb26{month:02}", expiry=f"2026-{month:02}-15T15:00:00+08:00") for month in (1, 3, 5, 9)]
    bars = tuple(bar for contract in contracts for bar in _bars(contract["raw_symbol"]))
    sample = select_study_sample(_bundle(contracts, bars), max_contracts_per_product=cap)
    assert len(sample.selected) == cap
    assert len(sample.universe) == 4
    assert sample.config["max_contracts_per_product"] == cap
    assert sample.config["reason_counts"]["product_cap"] == 4 - cap


def test_maximum_min_rows_threshold_counts_raw_history_without_truncation():
    bars = _bars(count=10_001)
    sample = select_study_sample(_bundle([_contract()], bars), min_rows=10_000)
    assert len(sample.selected[0].bars) == 10_001
    assert sample.config["min_rows"] == 10_000
    assert select_study_sample(_bundle([_contract()], bars[:9999]), min_rows=10_000).selected == ()


@pytest.mark.parametrize(("contract_count", "bar_count", "error"), [(601, 0, "600 contracts"), (0, 200_001, "200000 bar rows")])
def test_input_limits_apply_before_identity_or_bar_processing(contract_count, bar_count, error):
    # Deliberately malformed rows establish that the size guard runs first.
    bundle = _bundle([None] * contract_count, [None] * bar_count)
    with pytest.raises(ValueError, match=error):
        select_study_sample(bundle)


def test_input_contract_limit_is_inclusive():
    sample = select_study_sample(_bundle([None] * 600))
    assert len(sample.inventory) == 600


def test_input_bar_limit_is_inclusive_and_orphans_count_once():
    bars = _bars("undeclared-symbol", 200_000)
    sample = select_study_sample(_bundle(bars=bars))
    assert sample.selected == sample.universe == sample.inventory == ()
    assert sample.config["counts"]["orphan_bar_rows"] == 200_000
    assert sample.config["status"] == "no_eligible_contracts"


def test_selection_performs_no_filesystem_or_network_io(monkeypatch):
    bundle = _bundle([_contract()], _bars())

    def blocked(*args, **kwargs):
        raise AssertionError("selection must use only the in-memory input")

    with monkeypatch.context() as guard:
        guard.setattr(builtins, "open", blocked)
        guard.setattr(io, "open", blocked)
        guard.setattr(os, "open", blocked)
        guard.setattr(Path, "stat", blocked)
        guard.setattr(socket, "create_connection", blocked)
        guard.setattr(socket.socket, "connect", blocked)
        sample = select_study_sample(bundle)
    assert len(sample.selected) == 1
