"""Read-only market data MCP server (Streamable HTTP)."""
from ak_trading.mcp_server.queries import (
    MarketMcpQueryError, ReadOnlyMarketData,
)

__all__ = [
    "MarketMcpQueryError",
    "ReadOnlyMarketData",
    "build_mcp_app",
]


def build_mcp_app(market: ReadOnlyMarketData, *, token: str,
                  allowed_ips: tuple[str, ...] = (), rate_per_minute: int = 120):
    from ak_trading.mcp_server.server import build_mcp_app as _build

    return _build(market, token=token, allowed_ips=allowed_ips,
                  rate_per_minute=rate_per_minute)
