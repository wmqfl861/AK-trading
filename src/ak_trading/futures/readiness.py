"""Pure, bounded diagnostics for frozen futures inputs.

This is a frozen inventory, not G(c, d, a, t) for a historical trade. The
request's explicit as_of is only an inventory anchor. No source label,
metadata claim, synthetic fixture, or successful observation check enables
strict execution. Historical approval/sidecar ingestion is outside this API.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, fields
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Mapping, Sequence

from ak_trading.futures.data import ActionInputs, FIXED_UNIVERSE, evaluate_readiness
from ak_trading.futures.io import InputBundle
from ak_trading.futures.spec import ContractBar, FieldEvidence, ProductKey, ReadinessAction, parse_product_token

__all__ = ["inspect_readiness"]

_SCHEMA = "futures_readiness_v1"
_STRICT_ACTIONS = (
    "open_long", "open_short", "close_today", "close_history", "value",
)
# Match the existing export bounds without importing the persistence layer.
_LIMITS = {"products": 6, "contracts": 600, "bar_rows": 200_000}
_ALIASES = {
    "sessions": ("historical_session_windows",),
    "trading_day_source": ("trading_day_source_verified",),
    "parameter_availability": ("parameter_available_at",),
    "lifecycle": ("last_trade_at", "delivery_cutoff_at"),
    "multiplier": ("multiplier",),
    "price_tick": ("price_tick",),
    "fees": ("fee_basis_close_today",),
    "margin": ("margin_basis_settlement_price",),
    "settlement": ("margin_basis_settlement_price",),
    "limits": ("limit_bands",),
}
_TIME_REASON = "no_verified_historical_event_time_in_frozen_inventory"


def _mapping(value: object) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _aware(value: object) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def _datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        return None
    return result if _aware(result) else None


def _product(value: object) -> ProductKey | None:
    try:
        key = parse_product_token(value) if isinstance(value, str) else None
    except ValueError:
        return None
    return key if key in FIXED_UNIVERSE else None


def _evidence(name: str, status: str, reason: str, evidence_id: str | None = None) -> FieldEvidence:
    return FieldEvidence(name, status, reason, evidence_id)


def _claim(name: str, source_name: str, raw: object) -> FieldEvidence:
    """Normalize claims, never accepting an approval or exemption label."""
    if raw is None:
        return _evidence(name, "missing", f"{source_name}:no_frozen_field_evidence")
    if not isinstance(raw, Mapping):
        return _evidence(name, "invalid", f"{source_name}:malformed_field_evidence")
    status = raw.get("status")
    evidence_id = _text(raw.get("evidence_id"))
    original_reason = _text(raw.get("reason")) or "no_source_reason"
    if status == "missing_local_approved_evidence":
        status = "missing"
        original_reason = "missing_local_approved_evidence:" + original_reason
    if status in ("missing", "unknown", "invalid", "present_reference_only", "present_unapproved"):
        return _evidence(name, status, f"{source_name}:{original_reason}", evidence_id)
    if status == "present_approved":
        return _evidence(
            name, "present_unapproved",
            f"{source_name}:approval_claim_unverified:no_validated_provenance_and_historical_times",
            evidence_id,
        )
    if status == "not_applicable":
        return _evidence(name, "present_unapproved", f"{source_name}:exemption_basis_not_verified", evidence_id)
    # Including not_requested: it must never skip a required gate field.
    return _evidence(name, "invalid", f"{source_name}:unsupported_evidence_status:{status!r}", evidence_id)


def _product_fields(payload: object) -> tuple[dict[str, FieldEvidence], dict[str, object]]:
    strict_fields = _mapping(_mapping(payload).get("strict_fields"))
    normalized: dict[str, FieldEvidence] = {}
    sources: dict[str, object] = {}
    precedence = {"invalid": 0, "missing": 1, "unknown": 2, "present_unapproved": 3, "present_reference_only": 4}
    for name, aliases in _ALIASES.items():
        claims = [_claim(name, alias, strict_fields.get(alias)) for alias in aliases]
        worst = min(claims, key=lambda claim: precedence[claim.status])
        normalized[name] = _evidence(
            name, worst.status, ";".join(claim.reason or "unknown" for claim in claims),
            worst.evidence_id,
        )
        for alias in aliases:
            raw = _mapping(strict_fields.get(alias))
            sources[alias] = {
                "source_status": _text(raw.get("status")),
                "reason": _text(raw.get("reason")),
                "evidence_id": _text(raw.get("evidence_id")),
            }
    return normalized, sources


def _number(value: object, *, positive: bool) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return False
    try:
        number = Decimal(str(value))
        return number.is_finite() and (number > 0 if positive else number >= 0)
    except (InvalidOperation, ValueError):
        return False


def _bar_summaries(bars: Sequence[ContractBar], as_of: datetime) -> tuple[dict[str, Counter], Counter]:
    """One pass; retain counters and duplicate keys, never output raw rows."""
    by_symbol: dict[str, Counter] = {}
    total: Counter = Counter()
    seen: set[tuple[str, int, int]] = set()
    for bar in bars:
        if not isinstance(bar, ContractBar):
            raise ValueError("readiness requires ContractBar records")
        counts: Counter = Counter(bar_rows=1)
        reasons: set[str] = set()
        prices = (bar.open, bar.high, bar.low, bar.close)
        if not all(_number(price, positive=True) for price in prices):
            reasons.add("invalid_ohlc")
        elif not (bar.high >= max(bar.open, bar.close, bar.low) and bar.low <= min(bar.open, bar.close)):
            reasons.add("invalid_ohlc_order")
        if any(value is not None and not _number(value, positive=False) for value in (bar.volume, bar.open_oi, bar.close_oi)):
            reasons.add("invalid_volume_or_oi")
        if not _text(bar.symbol) or not isinstance(bar.datetime_nano, int) or isinstance(bar.datetime_nano, bool):
            reasons.add("invalid_bar_identity")
        else:
            key = (bar.symbol, bar.duration_seconds, bar.datetime_nano)
            if key in seen:
                reasons.add("duplicate_bar_key")
            seen.add(key)
        counts["invalid_bar_rows"] = int(bool(reasons))
        for reason in reasons:
            counts[reason] += 1
        counts["trading_day_present"] = int(bar.trading_day is not None)
        counts["trading_day_missing"] = int(bar.trading_day is None)
        counts["reference_calendar_date_rows"] = int(bar.trading_day_source == "reference_calendar_date")
        counts["outside_reference_calendar_rows"] = int(bar.trading_day_source == "outside_reference_calendar")
        counts["volume_missing"] = int(bar.volume is None)
        counts["session_open_present"] = int(bar.session_open is not None)
        counts["session_close_present"] = int(bar.session_close is not None)
        counts["available_at_present"] = int(bar.available_at is not None)
        counts["available_at_missing"] = int(bar.available_at is None)
        if bar.available_at is not None:
            counts["available_at_invalid"] = int(not _aware(bar.available_at))
            if _aware(bar.available_at):
                counts["available_at_after_as_of"] = int(bar.available_at > as_of)
                counts["available_at_after_raw_ts"] = int(bar.available_at > bar.ts)
        session_values = (bar.session_open, bar.session_close)
        invalid_sessions = any(value is not None and not _aware(value) for value in session_values)
        if all(_aware(value) for value in session_values):
            invalid_sessions |= bar.session_close <= bar.session_open
        counts["invalid_session_rows"] = int(invalid_sessions)
        by_symbol.setdefault(bar.symbol, Counter()).update(counts)
        total.update(counts)
    return by_symbol, total


def _counter_json(counts: Counter) -> dict[str, int]:
    return {key: int(value) for key, value in sorted(counts.items())}


def _identity(contract: Mapping, product: ProductKey | None, raw_counts: Counter, id_counts: Counter) -> FieldEvidence:
    instance_id, symbol = _text(contract.get("instance_id")), _text(contract.get("raw_symbol"))
    expiry = _datetime(contract.get("expiry_at"))
    missing = [name for name in ("instance_id", "raw_symbol", "product", "expiry_at") if not _text(contract.get(name))]
    invalid: list[str] = []
    if product is None and contract.get("product") is not None:
        invalid.append("malformed_or_unsupported_product")
    if contract.get("expiry_at") is not None and expiry is None:
        invalid.append("malformed_or_naive_expiry")
    if symbol is not None and product is not None:
        exchange, _, suffix = symbol.partition(".")
        letters = suffix.rstrip("0123456789")
        digits = suffix[len(letters):]
        if exchange != product.exchange or letters.upper() != product.product or not digits:
            invalid.append("raw_symbol_product_mismatch")
    if instance_id and symbol and expiry:
        prefix, separator, suffix = instance_id.partition("|")
        if prefix != symbol or not separator or _datetime(suffix) != expiry:
            invalid.append("instance_id_does_not_match_symbol_and_expiry")
    if symbol and raw_counts[symbol] > 1:
        invalid.append("ambiguous_raw_symbol_instances")
    if instance_id and id_counts[instance_id] > 1:
        invalid.append("duplicate_instance_id")
    if invalid:
        return _evidence("identity", "invalid", ";".join(sorted(invalid + ["missing_" + name for name in missing])))
    if missing:
        return _evidence("identity", "missing", ";".join("missing_" + name for name in missing))
    return _evidence("identity", "present_reference_only", "frozen_identity_is_not_approved_historical_listing_evidence")


def _contract_fields(contract: Mapping, identity: FieldEvidence, counts: Counter, product_fields: Mapping[str, FieldEvidence]) -> dict[str, FieldEvidence]:
    result = {
        field.name: _evidence(field.name, "unknown", "not_established_by_frozen_inventory")
        for field in fields(ActionInputs)
    }
    result.update(product_fields)
    result["identity"] = identity
    if not counts["bar_rows"]:
        result["bar_validity"] = _evidence("bar_validity", "missing", "no_unambiguously_assigned_bars")
    elif counts["invalid_bar_rows"]:
        result["bar_validity"] = _evidence("bar_validity", "invalid", "invalid_or_duplicate_raw_bars")
    else:
        # This approval is a computed local observation check, not a claim
        # accepted from the bundle, and says nothing about historical clocks.
        result["bar_validity"] = _evidence("bar_validity", "present_approved", "in_memory_ohlc_volume_identity_checks_passed")
    if _datetime(contract.get("expiry_at")):
        result["expiry"] = _evidence("expiry", "present_reference_only", "cached_expiry_has_no_approved_historical_provenance")
    else:
        result["expiry"] = _evidence("expiry", "missing" if contract.get("expiry_at") is None else "invalid", "missing_or_invalid_expiry_at")
    for name in ("currency", "lot_rules"):
        result[name] = _evidence(name, "missing", "no_validated_historical_metadata_adapter")
    for name in ("requirements", "bar_history", "pair_sync"):
        result[name] = _evidence(name, "unknown", "strategy_and_historical_decision_context_not_requested")
    result["capacity"] = _evidence("capacity", "unknown", "no_verified_execution_event_and_causal_volume_window")
    result["nav_risk"] = _evidence("nav_risk", "unknown", "portfolio_state_not_requested_in_frozen_inventory")
    if counts["available_at_invalid"]:
        result["bar_availability"] = _evidence("bar_availability", "invalid", "available_at_must_be_timezone_aware")
    elif not counts["bar_rows"] or counts["available_at_missing"]:
        result["bar_availability"] = _evidence("bar_availability", "missing", "bar_available_at_missing")
    else:
        reason = "available_at_present_but_historical_visibility_not_verified"
        if counts["available_at_after_as_of"]:
            reason += ";bars_available_after_request_as_of"
        result["bar_availability"] = _evidence("bar_availability", "unknown", reason)
    if counts["invalid_session_rows"]:
        result["sessions"] = _evidence("sessions", "invalid", "malformed_or_reversed_session_windows")
    return result


def _actions(product: ProductKey | None, instance_id: str | None, anchor: datetime, evidence: Mapping[str, FieldEvidence]) -> dict[str, dict]:
    output: dict[str, dict] = {}
    for action in ReadinessAction:
        if product is None or instance_id is None:
            # No fabricated ProductKey/contract is passed into a typed gate.
            identity = evidence["identity"]
            output[action.value] = {
                "status": "blocked", "missing_fields": ["identity"] if identity.status == "missing" else [],
                "invalid_fields": [] if identity.status == "missing" else ["identity"],
                "reasons": [identity.reason], "evidence_ids": [],
            }
            continue
        gate = evaluate_readiness(action, product, instance_id, anchor, ActionInputs(**evidence))
        output[action.value] = {
            "status": gate.status, "missing_fields": list(gate.missing_fields),
            "invalid_fields": list(gate.invalid_fields), "reasons": list(gate.reasons),
            "evidence_ids": list(gate.evidence_ids),
        }
    return output


def _aggregate(leaves: Sequence[Mapping]) -> dict[str, object]:
    """Also counts invalid identities, for which no typed GateResult exists."""
    ready = sum(leaf["status"] == "ready" for leaf in leaves)
    blocked = len(leaves) - ready
    return {
        "status": "ready" if ready and not blocked else "partial" if ready else "blocked",
        "ready_count": ready, "blocked_count": blocked,
        "requested_count": len(leaves), "denominator": "requested_contract_actions",
        "missing_fields": sorted({field for leaf in leaves for field in leaf["missing_fields"]}),
        "invalid_fields": sorted({field for leaf in leaves for field in leaf["invalid_fields"]}),
        "reasons": sorted({reason for leaf in leaves for reason in leaf["reasons"] if reason}),
    }


def _action_aggregates(contracts: Sequence[Mapping]) -> dict[str, object]:
    return {action.value: _aggregate([contract["actions"][action.value] for contract in contracts]) for action in ReadinessAction}


def _metadata_assessment(metadata: Mapping) -> dict[str, object]:
    inventory = _mapping(metadata.get("source_inventory"))
    sources = inventory.get("sources")
    loaded = (inventory.get("schema_version") == "futures_local_metadata_inventory_v1"
              and inventory.get("status") == "loaded" and isinstance(sources, list))
    return {
        "scope": "frozen_bundle_only",
        "bundle_fields_status": "present_unvalidated" if _mapping(metadata.get("fields")) else "absent",
        "source_inventory_status": ("loaded" if loaded else "declared_not_loaded"
                                    if metadata.get("local_metadata_paths") else "not_loaded"),
        "source_presence": ("present_reference_only" if sources else "not_observed_in_loaded_sources")
                           if loaded else "unassessed_not_loaded",
        "source_count": len(sources) if loaded else 0,
        "database_metadata_status": "not_assessed",
        "other_local_sources_status": "not_assessed",
        "historical_validity": "not_assessed", "approval_status": "unapproved",
        "legacy_metadata_status_interpretation": "describes_bundle_fields_only_not_database_or_local_source_absence",
        "source_hash_verification": "caller_owned_frozen_inventory_claims_not_reread",
    }


def _source_presence(payload: object, inventory_status: str) -> dict[str, object]:
    """Only copy bounded known semantic fields and content-addressed IDs."""
    import re

    raw = _mapping(payload)
    fields_by_source = _mapping(raw.get("field_sources"))
    allowed = set(_ALIASES) | {"currency", "lot_rules"}
    def source_ids(values: object) -> list[str]:
        return sorted({value for value in values if isinstance(value, str)
                       and re.fullmatch(r"local_metadata:[a-f0-9]{64}", value)}) if isinstance(values, list) else []

    ids = source_ids(raw.get("source_ids"))
    field_sources = {name: source_ids(values) for name, values in fields_by_source.items() if name in allowed}
    return {
        "status": ("present_reference_only" if ids else "not_observed_in_loaded_sources")
                  if inventory_status == "loaded" else "unassessed_not_loaded",
        "source_ids": ids if inventory_status == "loaded" else [],
        "semantic_fields": sorted(name for name, values in field_sources.items() if values)
                           if inventory_status == "loaded" else [],
        "field_sources": field_sources if inventory_status == "loaded" else {},
        "scope": "explicit_local_files_only", "approval_status": "unapproved",
        "historical_validity": "not_assessed",
    }


def _field_interpretation(evidence: Mapping[str, FieldEvidence], presence: Mapping, metadata: Mapping) -> dict[str, object]:
    output = {}
    historical = set(_ALIASES) | {"currency", "lot_rules"}
    for name, claim in evidence.items():
        observed = name in presence["semantic_fields"]
        if observed:
            category = "source_presence"
        elif name in ("currency", "lot_rules"):
            category = "implementation_limitation"
        elif claim.status == "missing":
            category = "observed_missing_bundle_field"
        elif claim.status == "invalid":
            category = "observed_invalid_bundle_field"
        elif claim.status == "unknown":
            category = "unassessed_context"
        else:
            category = "frozen_observation_or_unvalidated_claim"
        output[name] = {
            "category": category,
            "source_presence": "present_reference_only" if observed else (
                "unassessed_not_loaded" if presence["status"] == "unassessed_not_loaded"
                else "not_observed_in_loaded_sources"
            ),
            "bundle_field_claim": "present_unvalidated" if name in _mapping(metadata.get("fields")) else "not_present",
            "historical_validity": "not_assessed" if name in historical else "not_established_by_inventory",
            "approval_status": "unapproved" if name in historical else "observation_only",
            "implementation_limitation": "historical_metadata_adapter_not_implemented" if name in historical else None,
        }
    return output


def inspect_readiness(bundle: InputBundle) -> dict[str, object]:
    """Inspect a caller-loaded bundle without I/O, state changes, or strategies.

    The caller owns loading/hash verification (``read_input_bundle``) and
    publishing. Hashes here are explicitly declared input references, not
    newly verified proofs. An explicit timezone-aware request.as_of is
    required; no wall clock, bar timestamp, or created_at substitutes it.
    """
    manifest = _mapping(bundle.manifest)
    request = _mapping(manifest.get("request"))
    anchor = _datetime(request.get("as_of"))
    if anchor is None:
        raise ValueError("readiness requires explicit timezone-aware request.as_of")
    if len(bundle.bars) > _LIMITS["bar_rows"] or len(bundle.contracts) > _LIMITS["contracts"]:
        raise ValueError("readiness scope_limit: frozen input exceeds export bounds")
    availability = _mapping(_mapping(bundle.availability).get("products"))
    metadata = _mapping(bundle.metadata)
    assessment = _metadata_assessment(metadata)
    contracts = [_mapping(contract) for contract in bundle.contracts]
    raw_counts = Counter(_text(contract.get("raw_symbol")) for contract in contracts)
    id_counts = Counter(_text(contract.get("instance_id")) for contract in contracts)
    by_symbol, clock_counts = _bar_summaries(bundle.bars, anchor)
    requested = request.get("products")
    requested = requested if isinstance(requested, (list, tuple)) else []
    product_keys = {key for value in [*requested, *availability, *(c.get("product") for c in contracts)] if (key := _product(value)) is not None}
    if len(product_keys) > _LIMITS["products"]:
        raise ValueError("readiness scope_limit: too many products")
    invalid_requested = sorted({str(value) for value in requested if _product(value) is None})
    product_sources = {str(key): _product_fields(availability.get(str(key))) for key in product_keys}
    product_contracts: dict[str, list[dict]] = {str(key): [] for key in product_keys}
    all_contracts: list[dict] = []
    unresolved: list[dict] = []
    assigned_symbols: set[str] = set()
    for contract in sorted(contracts, key=lambda item: (_text(item.get("product")) or "", _text(item.get("instance_id")) or "", _text(item.get("raw_symbol")) or "")):
        product = _product(contract.get("product"))
        symbol = _text(contract.get("raw_symbol"))
        instance_id = _text(contract.get("instance_id"))
        identity = _identity(contract, product, raw_counts, id_counts)
        counts = by_symbol.get(symbol, Counter()) if symbol and raw_counts[symbol] == 1 else Counter()
        if symbol and counts["bar_rows"]:
            assigned_symbols.add(symbol)
        product_fields = product_sources[str(product)][0] if product else _product_fields(None)[0]
        evidence = _contract_fields(contract, identity, counts, product_fields)
        product_payload = _mapping(availability.get(str(product)))
        presence = _source_presence(
            _mapping(product_payload.get("contract_source_presence")).get(symbol),
            assessment["source_inventory_status"],
        )
        for name in presence["semantic_fields"]:
            if name in evidence and evidence[name].status in ("missing", "unknown", "present_unapproved"):
                evidence[name] = _evidence(
                    name, "present_reference_only",
                    "explicit_local_source_field_observed:no_historical_validity_or_approval",
                    presence["field_sources"][name][0],
                )
        row = {
            "contract_instance_id": instance_id, "raw_symbol": symbol,
            "product": str(product) if product else None,
            "identity_source_status": _text(contract.get("identity_status")),
            "listing_at": _text(contract.get("listing_at")),
            "bar_rows": counts["bar_rows"], "clock_summary": _counter_json(counts),
            "field_evidence": {name: asdict(value) for name, value in sorted(evidence.items())},
            "local_source_presence": presence,
            "field_interpretation": _field_interpretation(evidence, presence, metadata),
            "actions": _actions(product, instance_id, anchor, evidence),
        }
        all_contracts.append(row)
        if product:
            product_contracts[str(product)].append(row)
        else:
            unresolved.append(row)
    products: dict[str, object] = {}
    for name, rows in sorted(product_contracts.items()):
        products[name] = {
            "contract_count": len(rows), "bar_rows": sum(row["bar_rows"] for row in rows),
            "source_fields": product_sources[name][1],
            "local_source_presence": _source_presence(
                _mapping(availability.get(name)).get("local_source_presence"), assessment["source_inventory_status"],
            ),
            "actions": _action_aggregates(rows), "contracts": rows,
        }
    strict_leaves = [row["actions"][action] for row in all_contracts for action in _STRICT_ACTIONS]
    return {
        "schema_version": _SCHEMA, "basis": "frozen_inventory",
        "status": "unavailable", "execution_status": "unavailable",
        "strict_execution_enabled": False, "historical_event_time": None,
        "historical_event_time_reason": _TIME_REASON,
        "scope": {
            "products": sorted(product_contracts), "start": _text(request.get("start")),
            "end": _text(request.get("end")), "as_of": anchor.isoformat(),
            "inventory_anchor": "request.as_of_not_a_historical_execution_event",
        },
        "input_references": {
            "schema_version": _text(manifest.get("schema_version")),
            "dataset_kind": _text(manifest.get("dataset_kind")),
            "request_digest": _text(request.get("request_digest")),
            "declared_content_hashes": {str(key): value for key, value in sorted(_mapping(manifest.get("content_hashes")).items(), key=lambda item: str(item[0])) if isinstance(value, str)},
            "declared_source_hashes": {str(key): value for key, value in sorted(_mapping(manifest.get("source_hashes")).items(), key=lambda item: str(item[0])) if isinstance(value, str)},
            "hash_verification": "caller_owned_not_performed_by_inspector",
        },
        "counts": {
            "bar_rows": len(bundle.bars), "contracts": len(contracts), "products": len(products),
            "contracts_with_bars": sum(row["bar_rows"] > 0 for row in all_contracts),
            "contracts_without_bars": sum(row["bar_rows"] == 0 for row in all_contracts),
            "unresolved_contracts": len(unresolved),
            "unassigned_bar_rows": sum(counts["bar_rows"] for symbol, counts in by_symbol.items() if symbol not in assigned_symbols),
        },
        "clock_summary": _counter_json(clock_counts),
        "calendar": {"status": "unavailable", "reason": "verified_product_calendar_and_session_evidence_not_loaded"},
        "metadata_status": "unapproved_claims_no_historical_adapter" if _mapping(bundle.metadata).get("fields") else "no_historical_metadata_fields",
        "metadata_assessment": assessment,
        "implementation_limitations": {
            "historical_metadata_adapter": "not_implemented",
            "historical_gate_evaluation": "not_implemented_by_frozen_inventory",
            "strict_execution": "disabled_regardless_of_local_source_presence",
        },
        "actions": _action_aggregates(all_contracts),
        "strict_actions": list(_STRICT_ACTIONS),
        "strict_action_aggregate": _aggregate(strict_leaves),
        "products": products, "unresolved_contracts": unresolved,
        "invalid_requested_products": invalid_requested,
        "account_artifacts": "not_requested", "member_factor": "not_requested",
    }
