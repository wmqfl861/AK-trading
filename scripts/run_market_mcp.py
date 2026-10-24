"""Run the read-only market data MCP server (Streamable HTTP).

Environment:
- AK_MCP_TOKEN     required bearer token (generate: python -c "import secrets;print(secrets.token_urlsafe(32))")
- AK_MCP_HOST      bind address, default 127.0.0.1 (use a LAN IP or a
                   tunnel for remote agents; never 0.0.0.0 without a firewall rule)
- AK_MCP_PORT      default 17779
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=None, help="bind host (default AK_MCP_HOST or 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="port (default AK_MCP_PORT or 17779)")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    token = os.getenv("AK_MCP_TOKEN", "").strip()
    if not token:
        print("AK_MCP_TOKEN is required; generate one with "
              "python -c \"import secrets;print(secrets.token_urlsafe(32))\" "
              "and add it to .env", file=sys.stderr)
        return 2
    from ak_trading.persistence.config import (
        DatabaseConfigurationError, load_database_settings,
    )
    try:
        settings = load_database_settings(required=True)
    except DatabaseConfigurationError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    assert settings is not None

    from ak_trading.mcp_server.queries import ReadOnlyMarketData
    from ak_trading.mcp_server.server import build_mcp_app

    allowed_ips = tuple(
        entry.strip()
        for entry in os.getenv("AK_MCP_ALLOWED_IPS", "").split(",")
        if entry.strip()
    )
    try:
        rate_per_minute = int(os.getenv("AK_MCP_RATE_PER_MINUTE", "120"))
    except ValueError:
        rate_per_minute = 120
    rate_per_minute = max(10, min(rate_per_minute, 10_000))
    app = build_mcp_app(
        ReadOnlyMarketData(settings), token=token,
        allowed_ips=allowed_ips, rate_per_minute=rate_per_minute,
    )

    import uvicorn

    host = args.host or os.getenv("AK_MCP_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = args.port or int(os.getenv("AK_MCP_PORT", "17779") or 17779)
    uvicorn.run(app, host=host, port=port, log_level="info", access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
