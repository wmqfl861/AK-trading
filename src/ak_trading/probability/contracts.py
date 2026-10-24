"""Shared records for reproducible daily probability research."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Mapping

from ak_trading.futures.spec import ContractBar

OUTCOMES = ("up_first", "down_first", "neither", "ambiguous")
REGIMES = ("uptrend", "downtrend", "range")
PARTITIONS = ("train", "calibration", "test")
ACTIONS = ("breakout", "mean_reversion", "pullback", "grid", "cash")


def _parameter_identity(value: float) -> str:
    number = float(value)
    return str(int(number)) if number.is_integer() else repr(number)


@dataclass(frozen=True)
class FactorSpec:
    name: str
    version: str
    lookback: int
    unit: str
    definition: str


@dataclass(frozen=True)
class FactorRow:
    event_id: str
    contract_id: str
    symbol: str
    product: str
    index: int
    timestamp_nano: int
    values: Mapping[str, float | None]
    status: str
    reason: str
    context: str
    regime: str


@dataclass(frozen=True)
class PathScenario:
    horizon: int
    upper_atr: float
    lower_atr: float

    def __post_init__(self) -> None:
        import math
        if isinstance(self.horizon, bool) or not isinstance(self.horizon, int) or not 1 <= self.horizon <= 60:
            raise ValueError("horizon must be an integer in 1..60")
        for value in (self.upper_atr, self.lower_atr):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 10:
                raise ValueError("barrier distances must be finite in (0, 10]")

    @property
    def scenario_id(self) -> str:
        return f"h{self.horizon}-u{_parameter_identity(self.upper_atr)}-d{_parameter_identity(self.lower_atr)}"


@dataclass(frozen=True)
class PathLabel:
    event_id: str
    scenario_id: str
    contract_id: str
    index: int
    horizon: int
    terminal_nano: int | None
    status: str
    reason: str
    outcome: str | None
    entry_price: float | None = None
    upper_price: float | None = None
    lower_price: float | None = None
    first_hit_step: int | None = None
    terminal_return: float | None = None
    terminal_points: float | None = None
    mfe_points: float | None = None
    mae_points: float | None = None
    terminal_regime: str | None = None


@dataclass(frozen=True)
class ActionSpec:
    name: str
    horizon: int = 5
    target_atr: float = 1.0
    stop_atr: float = 1.0
    pullback_atr: float = 0.5
    grid_step_atr: float = 0.5
    grid_levels: int = 2
    grid_stop_atr: float = 3.0

    def __post_init__(self) -> None:
        import math
        if self.name not in ACTIONS:
            raise ValueError("unsupported action")
        if isinstance(self.horizon, bool) or not isinstance(self.horizon, int) or not 1 <= self.horizon <= 60:
            raise ValueError("action horizon must be in 1..60")
        if isinstance(self.grid_levels, bool) or not isinstance(self.grid_levels, int) or not 1 <= self.grid_levels <= 5:
            raise ValueError("grid_levels must be in 1..5")
        for value in (self.target_atr, self.stop_atr, self.pullback_atr, self.grid_step_atr, self.grid_stop_atr):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 10:
                raise ValueError("action distances must be finite in (0, 10]")

    @property
    def action_id(self) -> str:
        return (
            f"{self.name}:h{self.horizon}:t{_parameter_identity(self.target_atr)}:s{_parameter_identity(self.stop_atr)}"
            f":p{_parameter_identity(self.pullback_atr)}:g{_parameter_identity(self.grid_step_atr)}:n{self.grid_levels}:x{_parameter_identity(self.grid_stop_atr)}"
        )


@dataclass(frozen=True)
class ActionOutcome:
    event_id: str
    action_id: str
    contract_id: str
    index: int
    terminal_nano: int | None
    status: str
    reason: str
    traded: bool
    direction: int
    gross_pnl: Decimal
    fees: Decimal
    slippage: Decimal
    net_pnl: Decimal
    max_margin: Decimal
    ambiguous_bars: int
    fills: tuple[Mapping[str, object], ...] = ()


@dataclass(frozen=True)
class ProbabilityPrediction:
    event_id: str
    scenario_id: str
    probabilities: Mapping[str, float]
    regime_probabilities: Mapping[str, float]
    train_count: int
    calibration_count: int
    model_status: str
    bucket: str
    temperature: float
    diagnostics: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class PayoffEstimate:
    event_id: str
    action_id: str
    count: int
    mean_net: float | None
    standard_error: float | None
    conservative_ev: float | None
    win_probability: float | None
    loss_q10: float | None
    status: str
    bucket: str
    diagnostics: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ResearchSelection:
    event_id: str
    mode: str
    action_id: str
    weights: Mapping[str, float]
    reason: str
    predicted_ev: float | None
    prediction_confidence: float | None
    random_u: float | None


@dataclass(frozen=True)
class PartitionPlan:
    train_end_nano: int
    calibration_end_nano: int
    maximum_nano: int
    fractions: tuple[float, float] = (0.6, 0.2)
    final_holdout_consumed: bool = True


def validate_bar(bar: ContractBar) -> bool:
    values = (bar.open, bar.high, bar.low, bar.close)
    if any(not isinstance(value, Decimal) or not value.is_finite() or value <= 0 for value in values):
        return False
    return bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high
