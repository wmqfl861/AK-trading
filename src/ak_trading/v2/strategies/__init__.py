"""M3B strategy library subpackage (devplan-v2.1.0 04-M3B-策略库.md).

Only version constants live here; consumers import family modules and the
shared registry/state helpers from ``ak_trading.v2.strategies.<module>``
directly, mirroring the no-reexport rule of the top-level ``v2`` package.
"""
from __future__ import annotations

M3B_NODE_VERSION = "M3B-v2.1.0"
STRATEGY_LIBRARY_VERSION = "2.1.0"

__all__ = ("M3B_NODE_VERSION", "STRATEGY_LIBRARY_VERSION")
