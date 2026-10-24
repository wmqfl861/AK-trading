"""Shared immutable inputs for retrospective futures price-event studies."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping

from ak_trading.futures.spec import ContractBar


@dataclass(frozen=True)
class StudyContract:
    contract_instance_id: str
    raw_symbol: str
    product: str
    expiry_at: datetime
    bars: tuple[ContractBar, ...]


@dataclass(frozen=True)
class StudySignal:
    contract_instance_id: str
    raw_symbol: str
    product: str
    strategy_id: str
    index: int
    datetime_nano: int
    candidate_direction: int | None
    feature_status: str
    reason: str
    diagnostics: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class StudySample:
    selected: tuple[StudyContract, ...]
    universe: tuple[StudyContract, ...]
    inventory: tuple[Mapping[str, object], ...]
    config: Mapping[str, object]
