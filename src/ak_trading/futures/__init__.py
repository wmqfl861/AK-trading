"""Futures strategy library (first batch): data, structures, strategies.

Minimal public surface. Importing this package never touches accounts,
databases or environment loaders (finalized-plan §三). S2 adds the
portfolio, backtest and validation layers; their absence in S1 was
deliberate and their presence now is equally explicit.
"""
from ak_trading.futures.spec import (
    ClockStatus,
    ContractBar,
    ContractMetadata,
    FactorValue,
    FeeSpec,
    Fill,
    MarginSpec,
    MetadataField,
    PairSelectionState,
    PortfolioTarget,
    ProductKey,
    ResearchPolicy,
    StrategyContext,
    StrategyResult,
    StrategySpec,
    StrategyState,
    StructureFrame,
    Trade,
    parse_product_token,
)

__all__ = [
    "ClockStatus",
    "ContractBar",
    "ContractMetadata",
    "FactorValue",
    "FeeSpec",
    "Fill",
    "MarginSpec",
    "MetadataField",
    "PairSelectionState",
    "PortfolioTarget",
    "ProductKey",
    "ResearchPolicy",
    "StrategyContext",
    "StrategyResult",
    "StrategySpec",
    "StrategyState",
    "StructureFrame",
    "Trade",
    "parse_product_token",
]
