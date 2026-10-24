"""Pure selection of a frozen, retrospective futures observation sample."""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Mapping
from datetime import datetime

from ak_trading.futures.data import FIXED_UNIVERSE
from ak_trading.futures.io import BUNDLE_SCHEMA_VERSION, InputBundle
from ak_trading.futures.spec import ContractBar, ProductKey, parse_product_token
from ak_trading.futures.study_spec import StudyContract, StudySample

__all__ = ["select_study_sample"]

_MIN_ROWS = 120
_MAX_MIN_ROWS = 10_000
_MAX_CONTRACTS_PER_PRODUCT = 3
_MAX_INPUT_CONTRACTS = 600
_MAX_INPUT_ROWS = 200_000
_PRODUCT_ORDER = {str(product): index for index, product in enumerate(FIXED_UNIVERSE)}
_BIAS_NOTE = (
    "Retrospective coverage-convenience sample from a frozen real export; "
    "not survivorship-free. Stored history and cached contract coverage can "
    "introduce survival and coverage bias. Selection uses no price outcomes "
    "and does not establish point-in-time market membership or tradability."
)


def _bounded_integer(name: str, value: int, lower: int, upper: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
        raise ValueError(f"{name} must be an integer in {lower}..{upper}")


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _product(value: object) -> ProductKey | None:
    if not isinstance(value, (str, ProductKey)):
        return None
    try:
        return parse_product_token(str(value))
    except ValueError:
        return None


def _expiry(value: object) -> datetime | None:
    try:
        expiry = datetime.fromisoformat(value) if isinstance(value, str) else value
        if isinstance(expiry, datetime) and expiry.utcoffset() is not None:
            return expiry
    except (ValueError, TypeError, OverflowError):
        pass
    return None


def _future_symbol_matches(raw_symbol: str, product: ProductKey) -> bool:
    exchange, _, contract = raw_symbol.partition(".")
    # Preserve raw case, but require an exact exchange and a concrete delivery
    # code. This excludes continuous/index symbols, options, and spreads.
    match = re.fullmatch(r"([A-Za-z]+)([0-9]{3,4})", contract)
    if exchange != product.exchange or match is None:
        return False
    prefix, delivery = match.groups()
    return (
        prefix.upper() == product.product
        and len(delivery) == (3 if product.exchange == "CZCE" else 4)
        and 1 <= int(delivery[-2:]) <= 12
    )


def _sorted_bars(bars: tuple[ContractBar, ...]) -> dict[str, tuple[ContractBar, ...]]:
    grouped: dict[str, list[ContractBar]] = defaultdict(list)
    for bar in bars:
        if not isinstance(bar.symbol, str) or not bar.symbol:
            raise ValueError("bar symbol must be a nonempty raw symbol string")
        nano = bar.datetime_nano
        if isinstance(nano, bool) or not isinstance(nano, int):
            raise ValueError(f"datetime_nano must be an integer for {bar.symbol}")
        grouped[bar.symbol].append(bar)
    result: dict[str, tuple[ContractBar, ...]] = {}
    for symbol in sorted(grouped):
        ordered = tuple(sorted(grouped[symbol], key=lambda bar: bar.datetime_nano))
        for previous, current in zip(ordered, ordered[1:]):
            if previous.datetime_nano == current.datetime_nano:
                raise ValueError(f"duplicate datetime_nano for {symbol}: {current.datetime_nano}")
        result[symbol] = ordered
    return result


def select_study_sample(
    bundle: InputBundle,
    *,
    max_contracts_per_product: int = 3,
    min_rows: int = 120,
) -> StudySample:
    """Select up to three contracts per fixed product by expiry, then full ID.

    The caller loads and verifies the input bundle. This function performs no
    I/O and validates its in-memory manifest and contract identities. All raw
    price rows survive unchanged, ordered by integer nanoseconds. The universe
    also retains short/empty histories for possible far legs; only selected
    contracts must meet the stored-row threshold.

    Inventory ``bar_rows`` counts rows matching each declaration's raw symbol.
    Ambiguous declarations are excluded, so their repeated raw counts must not
    be summed as assigned instance histories. Config counts count input rows
    once and report ambiguous and orphan rows separately.
    """
    _bounded_integer("max_contracts_per_product", max_contracts_per_product, 1, _MAX_CONTRACTS_PER_PRODUCT)
    _bounded_integer("min_rows", min_rows, _MIN_ROWS, _MAX_MIN_ROWS)
    if not isinstance(bundle.manifest, Mapping):
        raise ValueError("study sample requires a valid input manifest")
    if bundle.manifest.get("dataset_kind") != "real_export":
        raise ValueError("study sample requires dataset_kind=real_export")
    if bundle.manifest.get("schema_version") != BUNDLE_SCHEMA_VERSION:
        raise ValueError(f"study sample requires schema_version={BUNDLE_SCHEMA_VERSION}")
    if bundle.manifest.get("status") != "complete":
        raise ValueError("study sample requires a complete input manifest")
    if len(bundle.contracts) > _MAX_INPUT_CONTRACTS:
        raise ValueError(f"study sample input exceeds {_MAX_INPUT_CONTRACTS} contracts")
    if len(bundle.bars) > _MAX_INPUT_ROWS:
        raise ValueError(f"study sample input exceeds {_MAX_INPUT_ROWS} bar rows")

    by_symbol = _sorted_bars(bundle.bars)
    raw_ids: dict[str, set[str | None]] = defaultdict(set)
    id_counts: Counter[str] = Counter()
    for metadata in bundle.contracts:
        if isinstance(metadata, Mapping):
            raw = _text(metadata.get("raw_symbol"))
            instance_id = _text(metadata.get("instance_id"))
            if raw is not None:
                raw_ids[raw].add(instance_id)
            if instance_id is not None:
                id_counts[instance_id] += 1
    ambiguous = {raw for raw, ids in raw_ids.items() if len(ids) > 1}

    inventory: list[dict[str, object]] = []
    universe: list[StudyContract] = []
    rows_by_id: dict[str, dict[str, object]] = {}
    invalid_metadata_count = 0
    for declaration in bundle.contracts:
        metadata = declaration if isinstance(declaration, Mapping) else {}
        raw = _text(metadata.get("raw_symbol"))
        instance_id = _text(metadata.get("instance_id"))
        product = _product(metadata.get("product"))
        expiry = _expiry(metadata.get("expiry_at"))
        bars = by_symbol.get(raw, ())
        row: dict[str, object] = {
            "contract_instance_id": instance_id,
            "raw_symbol": raw,
            "product": str(product) if product is not None else _text(metadata.get("product")),
            "expiry_at": expiry.isoformat() if expiry is not None else _text(metadata.get("expiry_at")),
            "bar_rows": len(bars),
            "selected": False,
            "status": "excluded",
        }
        reason = None
        if not isinstance(declaration, Mapping):
            reason = "invalid_contract_metadata"
        elif raw in ambiguous:
            reason = "ambiguous_raw_symbol_instances"
        elif instance_id is not None and id_counts[instance_id] > 1:
            reason = "duplicate_instance_id"
        elif raw is None:
            reason = "missing_raw_symbol"
        elif product is None:
            reason = "invalid_product"
        elif product not in FIXED_UNIVERSE:
            reason = "outside_fixed_universe"
        elif not _future_symbol_matches(raw, product):
            reason = "invalid_future_symbol_or_product_mismatch"
        elif expiry is None:
            reason = "invalid_or_naive_expiry"
        elif instance_id is None:
            reason = "missing_instance_id"
        elif instance_id != f"{raw}|{expiry.isoformat()}":
            reason = "instance_id_mismatch"
        if reason is not None:
            invalid_metadata_count += 1
            row["reason"] = reason
        else:
            assert raw is not None and product is not None and expiry is not None and instance_id is not None
            universe.append(StudyContract(instance_id, raw, str(product), expiry, bars))
            rows_by_id[instance_id] = row
            row["reason"] = (
                "no_bars" if not bars else "insufficient_rows" if len(bars) < min_rows else "product_cap"
            )
        inventory.append(row)

    universe.sort(key=lambda contract: (
        _PRODUCT_ORDER[contract.product], contract.expiry_at, contract.contract_instance_id,
    ))
    selected: list[StudyContract] = []
    selected_counts: Counter[str] = Counter()
    eligible_counts: Counter[str] = Counter()
    for contract in universe:
        if len(contract.bars) < min_rows:
            continue
        eligible_counts[contract.product] += 1
        if selected_counts[contract.product] >= max_contracts_per_product:
            continue
        selected_counts[contract.product] += 1
        selected.append(contract)
        rows_by_id[contract.contract_instance_id].update(
            selected=True, status="selected", reason="eligible_expiry_order",
        )
    inventory.sort(key=lambda row: (
        _PRODUCT_ORDER.get(row["product"], len(FIXED_UNIVERSE)),
        row["product"] or "", row["contract_instance_id"] or "",
        row["raw_symbol"] or "", row["expiry_at"] or "", row["reason"],
    ))
    orphan_symbols = sorted(set(by_symbol) - set(raw_ids))
    universe_counts = Counter(contract.product for contract in universe)
    declared_counts = Counter(row["product"] for row in inventory)
    config = {
        "schema_version": "futures_study_sample_v1",
        "dataset_kind": "real_export",
        "products": [str(product) for product in FIXED_UNIVERSE],
        "max_contracts_per_product": max_contracts_per_product,
        "min_rows": min_rows,
        "limits": {
            "min_rows_min": _MIN_ROWS,
            "min_rows_max": _MAX_MIN_ROWS,
            "max_contracts_per_product": _MAX_CONTRACTS_PER_PRODUCT,
            "max_input_contracts": _MAX_INPUT_CONTRACTS,
            "max_input_bar_rows": _MAX_INPUT_ROWS,
        },
        "selection_order": ["fixed_product_order", "expiry_at_ascending", "contract_instance_id_ascending"],
        "history_basis": "stored_observation_rows_including_invalid_prices",
        "inventory_bar_rows_basis": "raw_symbol_matches_not_additive_for_ambiguous_declarations",
        "universe_policy": "all_unambiguous_valid_metadata_including_short_and_empty_histories",
        "bias_note": _BIAS_NOTE,
        "status": "selected" if selected else "no_data" if not bundle.bars else "no_eligible_contracts",
        "counts": {
            "declared_contracts": len(bundle.contracts),
            "input_bar_rows": len(bundle.bars),
            "input_raw_symbols": len(by_symbol),
            "invalid_metadata_contracts": invalid_metadata_count,
            "universe_contracts": len(universe),
            "universe_bar_rows": sum(len(contract.bars) for contract in universe),
            "eligible_contracts": sum(eligible_counts.values()),
            "selected_contracts": len(selected),
            "selected_bar_rows": sum(len(contract.bars) for contract in selected),
            "excluded_contracts": len(inventory) - len(selected),
            "ambiguous_raw_symbols": len(ambiguous),
            "ambiguous_bar_rows": sum(len(by_symbol.get(raw, ())) for raw in ambiguous),
            "orphan_raw_symbols": len(orphan_symbols),
            "orphan_bar_rows": sum(len(by_symbol[raw]) for raw in orphan_symbols),
        },
        "orphan_symbols": orphan_symbols,
        "reason_counts": dict(sorted(Counter(row["reason"] for row in inventory).items())),
        "per_product": {
            str(product): {
                "declared_contracts": declared_counts[str(product)],
                "universe_contracts": universe_counts[str(product)],
                "eligible_contracts": eligible_counts[str(product)],
                "selected_contracts": selected_counts[str(product)],
            }
            for product in FIXED_UNIVERSE
        },
    }
    return StudySample(tuple(selected), tuple(universe), tuple(inventory), config)
