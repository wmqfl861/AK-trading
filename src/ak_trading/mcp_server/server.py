"""MCP server surface: tools, bearer auth, health route.

The Starlette app exposes:
- POST/GET /mcp   — the Model Context Protocol endpoint (bearer token)
- GET /healthz    — unauthenticated liveness probe, no data

Every tool is a thin wrapper over ak_trading.mcp_server.queries /
.reference, with an audit line per call. Rejected requests return
``{"error": ...}`` instead of raising, so remote agents get clean feedback.
"""
from __future__ import annotations

import hmac
from typing import Any

from ak_trading.mcp_server.audit import write_audit
from ak_trading.mcp_server.queries import MarketMcpQueryError, ReadOnlyMarketData

INSTRUCTIONS = (
    "Read-only Chinese futures market data. All dates are ISO YYYY-MM-DD; "
    "symbols look like SHFE.rb2610 (contract), KQ.m@SHFE.rb (main-continuous) "
    "or CFFEX.HO2609-C-2500 (option). Bars are CST; minute windows are capped "
    "at 31 days / 6000 rows. Use get_coverage to check freshness, "
    "search_symbols for prefix search, list_products for the product "
    "directory, and get_settlement_reference for official margin/fee "
    "parameters. The service is read-only: writes are impossible."
)


def _tool_call(tool: str, args: dict[str, Any], fn) -> Any:
    error = None
    rows = None
    try:
        result = fn()
        rows = result.get("rows") if isinstance(result, dict) else (
            len(result) if isinstance(result, list) else None
        )
    except MarketMcpQueryError:
        error = "rejected:INVALID_ARGUMENT"
        result = {"error": error}
    except Exception:  # noqa: BLE001 - boundary: never leak internals
        error = "internal:ERROR"
        result = {"error": error}
    # Leave the exception handler before writing: a failed audit must not
    # acquire a rejected argument or driver exception as its visible context.
    try:
        write_audit(tool, args, rows=rows, error=error)
    except Exception:  # noqa: BLE001 - audit failures are credential-safe too
        raise RuntimeError("internal:ERROR") from None
    return result


def build_mcp_app(
    market: ReadOnlyMarketData,
    *,
    token: str,
    allowed_ips: tuple[str, ...] = (),
    rate_per_minute: int = 120,
):
    """Return a fully configured Starlette app serving the MCP endpoint."""
    from fastmcp import FastMCP

    mcp = FastMCP(name="ak-market-data", instructions=INSTRUCTIONS)

    @mcp.tool
    def search_symbols(pattern: str, limit: int = 50) -> dict:
        """Prefix-search stored symbols (>=6 chars, no wildcards)."""
        return _tool_call(
            "search_symbols", {"pattern": pattern, "limit": limit},
            lambda: market.search_symbols(pattern, limit=limit),
        )

    @mcp.tool
    def get_daily_bars(symbol: str, start: str, end: str, limit: int = 1500) -> dict:
        """Daily OHLCV+OI bars for one symbol (window <= 800 days)."""
        return _tool_call(
            "get_daily_bars", {"symbol": symbol, "start": start, "end": end, "limit": limit},
            lambda: market.daily_bars(symbol, start, end, limit=limit),
        )

    @mcp.tool
    def get_minute_bars(symbol: str, start: str, end: str, limit: int = 3000) -> dict:
        """Minute bars for one symbol (window <= 31 days, <= 6000 rows)."""
        return _tool_call(
            "get_minute_bars", {"symbol": symbol, "start": start, "end": end, "limit": limit},
            lambda: market.minute_bars(symbol, start, end, limit=limit),
        )

    @mcp.tool
    def get_coverage(symbols: list[str]) -> dict:
        """Latest stored bar date per symbol (daily + minute), max 50 symbols."""
        return _tool_call(
            "get_coverage", {"symbols": symbols},
            lambda: {"coverage": market.coverage(symbols)},
        )

    @mcp.tool
    def get_member_ranks(
        trading_day: str, exchange: str = "", product_id: str = "",
        instrument_id: str = "", limit: int = 1000,
    ) -> dict:
        """Exchange member position ranks for one trading day."""
        args = {
            "trading_day": trading_day, "exchange": exchange,
            "product_id": product_id, "instrument_id": instrument_id,
            "limit": limit,
        }
        return _tool_call(
            "get_member_ranks", args,
            lambda: market.member_ranks(
                trading_day, exchange=exchange,
                product_id=None if isinstance(product_id, str) and product_id == "" else product_id,
                instrument_id=None if isinstance(instrument_id, str) and instrument_id == "" else instrument_id,
                limit=limit,
            ),
        )

    @mcp.tool
    def get_settlement_reference(day: str, symbol: str) -> dict:
        """Official/reference margin rate and fees for one contract on a day."""
        from ak_trading.mcp_server.reference import settlement_reference

        return _tool_call(
            "get_settlement_reference", {"day": day, "symbol": symbol},
            lambda: settlement_reference(day, symbol),
        )

    @mcp.tool
    def get_trading_calendar(start: str, end: str) -> dict:
        """Chinese futures trading calendar (which days are trading days)."""
        from ak_trading.mcp_server.reference import trading_calendar

        return _tool_call(
            "get_trading_calendar", {"start": start, "end": end},
            lambda: trading_calendar(start, end),
        )

    @mcp.tool
    def list_products() -> dict:
        """All futures products (exchange, code, Chinese name)."""
        from ak_trading.mcp_server.reference import products

        return _tool_call("list_products", {}, products)

    app = mcp.http_app(path="/mcp")
    app.add_middleware(
        _AccessMiddleware, token=token,
        allowed_ips=allowed_ips, rate_per_minute=rate_per_minute,
    )

    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def health(_request):
        return JSONResponse({
            "service": "ak-market-data", "status": "ok",
            "read_only": True, "protocol": "streamable-http",
        })

    app.routes.append(Route("/healthz", health, methods=["GET"]))
    return app


class _AccessMiddleware:
    """Bearer check + optional client allowlist + per-IP rate limit.

    /healthz stays open. The allowlist (AK_MCP_ALLOWED_IPS) and rate limits
    matter when the service is reachable from the public internet: the token
    then stops being the only line of defence.
    """

    def __init__(
        self,
        app,
        *,
        token: str,
        allowed_ips: tuple[str, ...] = (),
        rate_per_minute: int = 120,
    ) -> None:
        import time

        self.app = app
        self._token = token.encode("utf-8")
        self._rate_per_minute = max(10, min(rate_per_minute, 10_000))
        self._window: dict[str, list[float]] = {}
        self._time = time.monotonic

        import ipaddress

        self._allowed_networks = tuple(
            ipaddress.ip_network(entry.strip(), strict=False)
            for entry in allowed_ips
            if entry.strip()
        )

    def _client_allowed(self, client_host: str) -> bool:
        if not self._allowed_networks:
            return True
        import ipaddress

        try:
            address = ipaddress.ip_address(client_host)
        except ValueError:
            return False
        return any(address in network for network in self._allowed_networks)

    def _rate_allowed(self, client_host: str) -> bool:
        now = self._time()
        window = self._window.setdefault(client_host, [])
        cutoff = now - 60.0
        while window and window[0] < cutoff:
            window.pop(0)
        if len(window) >= self._rate_per_minute:
            return False
        window.append(now)
        if len(self._window) > 10_000:  # prune idle keys
            for key, hits in list(self._window.items()):
                if not hits or hits[-1] < cutoff:
                    self._window.pop(key, None)
        return True

    async def __call__(self, scope, receive, send):
        from starlette.responses import PlainTextResponse

        if scope["type"] == "http" and scope.get("path") not in ("/healthz",):
            client_host = (scope.get("client") or (None,))[0] or ""
            if not self._client_allowed(client_host):
                response = PlainTextResponse("forbidden", status_code=403)
                await response(scope, receive, send)
                return
            # Rate limit before auth so bad-token floods are also throttled.
            if not self._rate_allowed(client_host):
                response = PlainTextResponse("rate limited", status_code=429)
                await response(scope, receive, send)
                return
            headers = {
                key.decode("latin-1").lower(): value.decode("latin-1")
                for key, value in scope.get("headers", [])
            }
            authorization = headers.get("authorization", "")
            supplied = (
                authorization[7:] if authorization.lower().startswith("bearer ") else ""
            ).encode("utf-8")
            if not supplied or not hmac.compare_digest(supplied, self._token):
                response = PlainTextResponse("unauthorized", status_code=401)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
