"""P05 acquisition -> M5 MinuteFeed adapter: PREPARE_ONLY skeleton, no feed construction.

Work order: docs/devplan-nextphase-20260923-r1/work-orders/P05-minute-feed-prepare.md

Gate: implementation requires the P02 feed-input-contract to be frozen by Codex
(and the R8 published handover for any execution-side linkage). The current
contract draft (docs/nextphase-evidence-20260923-r1/P02/r1/feed-input-contract.*)
carries status DRAFT_NOT_FROZEN, so this revision deliberately provides only:

  * the freeze gate (ContractNotFrozen / require_frozen_contract /
    build_minute_feed) that blocks adapter construction until the gate opens;
  * prepare-only status markers consumed by the offline tests and evidence;
  * the work-order offline-acceptance scenario register (contract-independent).

Prohibited surfaces (work order "范围"): no TqApi, no database engine, no
environment loading, no native port, no order method. Production acquisition
connection/authorization stays outside the MinuteFeed protocol; the source-side
received_at_nano stays diagnostic only, M5 validation time is produced by the
independent ReceiverClock at the M5 entry (never rebuilt from import time).
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

NODE_STATUS = "PREPARE_ONLY"
FEED_READINESS = "NOT_ACCEPTED"
IMPLEMENTATION_GATE = "P02_FEED_INPUT_CONTRACT_MUST_BE_FROZEN_BY_CODEX"
R8_PENDING_MARKER = "PENDING_R8_HANDOVER"
DRAFT_SCHEMA = "ak_feed_input_contract_draft_v0"
FROZEN_STATUS = "FROZEN"
KNOWN_UNFROZEN_STATUSES = ("DRAFT_NOT_FROZEN",)

# Offline-acceptance scenarios named by the P05 work order (slug, work-order
# label). RUN_NOW scenarios are validated against the unmodified M5 consumer by
# tests/test_v2_m5_acquisition_feed.py in this revision; the other statuses
# activate only after the stated handover/freeze.
WORK_ORDER_SCENARIO_KEYS: tuple[tuple[str, str], ...] = (
    ("normal_closed_bar", "正常闭合bar"),
    ("duplicate_same_content_idempotent", "重复同内容幂等"),
    ("same_sequence_different_content_conflict", "同序不同内容冲突"),
    ("out_of_order_below_watermark", "乱序"),
    ("late_fill_flagged", "迟到"),
    ("sequence_gap_flagged", "gap"),
    ("producer_watermark_jump", "水位跳跃"),
    ("disconnect_reconnect_duplicate_resend", "断流/重连/重复补发"),
    ("trading_day_change_night_session", "换交易日/夜盘"),
    ("non_specific_contract_rejected", "非具体合约"),
    ("unfinished_bar_rejected", "未完成bar"),
)

SCENARIO_STATUS: dict[str, str] = {
    "normal_closed_bar": "RUN_NOW",
    "duplicate_same_content_idempotent": "RUN_NOW",
    "same_sequence_different_content_conflict": "RUN_NOW",
    "out_of_order_below_watermark": "RUN_NOW",
    "late_fill_flagged": "RUN_NOW",
    "sequence_gap_flagged": "RUN_NOW",
    # Producer-side watermark semantics (contract draft section 2) have no
    # consumer-protocol observable of their own; the stream-side sequence jump
    # is covered by sequence_gap_flagged until the contract freezes.
    "producer_watermark_jump": "PENDING_FREEZE",
    "disconnect_reconnect_duplicate_resend": "RUN_NOW",
    "trading_day_change_night_session": "RUN_NOW",
    "non_specific_contract_rejected": "RUN_NOW",
    "unfinished_bar_rejected": "RUN_NOW",
}

# Adapter-layer drafts beyond the work-order list; skipped with these statuses
# in tests/test_v2_m5_acquisition_feed.py until the gate opens.
ADDITIONAL_DRAFT_SCENARIOS: dict[str, str] = {
    "contract_record_ingestion": "PENDING_FREEZE",
    "watermark_restart_persistence": "PENDING_FREEZE",
    "legally_empty_window_source_rule": "PENDING_FREEZE",
    "reorder_buffer_two_second_bound": "PENDING_FREEZE",
    "execution_side_feed_consumption": "PENDING_R8_HANDOVER",
}


class ContractNotFrozen(RuntimeError):
    """The feed-input contract is not frozen; adapter construction is blocked."""


def contract_status(document: Mapping[str, Any]) -> str:
    """Return the contract document's status marker; refuse malformed input."""
    if not isinstance(document, Mapping):
        raise ValueError("contract_document_must_be_mapping")
    status = document.get("status")
    if not isinstance(status, str) or not status:
        raise ValueError("contract_status_missing")
    return status


def is_frozen_contract(document: Mapping[str, Any]) -> bool:
    """True only for a FROZEN, non-draft contract document."""
    try:
        status = contract_status(document)
    except ValueError:
        return False
    schema = document.get("schema")
    return (
        status == FROZEN_STATUS
        and isinstance(schema, str)
        and bool(schema)
        and schema != DRAFT_SCHEMA
    )


def require_frozen_contract(document: Mapping[str, Any]) -> None:
    """Block implementation unless the contract is frozen (work-order gate)."""
    if is_frozen_contract(document):
        return
    observed_status = document.get("status") if isinstance(document, Mapping) else None
    observed_schema = document.get("schema") if isinstance(document, Mapping) else None
    raise ContractNotFrozen(
        IMPLEMENTATION_GATE
        + "; observed status=" + repr(observed_status)
        + " schema=" + repr(observed_schema)
        + "; execution-side linkage " + R8_PENDING_MARKER
    )


def load_contract_document(path: Path) -> Mapping[str, Any]:
    """Read a contract document from a JSON file (no network, no environment)."""
    document = json.loads(Path(path).read_bytes())
    if not isinstance(document, dict):
        raise ValueError("contract_document_must_be_json_object")
    return document


def build_minute_feed(contract_document: Mapping[str, Any]) -> Any:
    """Blocked by design: no adapter exists in this PREPARE_ONLY revision.

    Even with a frozen contract this raises, because the implementation itself
    is gated on the freeze work order; the signature is extended there.
    """
    require_frozen_contract(contract_document)
    raise NotImplementedError("acquisition_feed_adapter_implementation_pending_freeze")
