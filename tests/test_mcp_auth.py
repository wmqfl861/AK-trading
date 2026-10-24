"""Auth middleware, audit writer and file-based reference tools."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ak_trading.mcp_server.audit import write_audit
from ak_trading.persistence.config import DatabaseSettings

ROOT = Path(__file__).resolve().parents[1]


class _StubMarket:
    """Never touched: auth tests never reach a tool."""


def _build_app(token: str):
    from ak_trading.mcp_server.server import build_mcp_app

    return build_mcp_app(_StubMarket(), token=token)  # type: ignore[arg-type]


def _client(app):
    from starlette.testclient import TestClient

    return TestClient(app)


def test_healthz_open_without_token():
    client = _client(_build_app("tok123"))
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "ak-market-data" and body["read_only"] is True


def test_mcp_requires_token():
    client = _client(_build_app("tok123"))
    assert client.get("/mcp").status_code == 401
    assert client.post("/mcp", json={}).status_code == 401


def test_mcp_rejects_wrong_token():
    client = _client(_build_app("tok123"))
    response = client.post(
        "/mcp", json={},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert response.status_code == 401


def test_mcp_accepts_correct_token():
    with _client(_build_app("tok123")) as client:
        response = client.post(
            "/mcp", json={},
            headers={
                "Authorization": "Bearer tok123",
                "Accept": "application/json, text/event-stream",
            },
        )
    assert response.status_code != 401


def test_audit_writer(tmp_path):
    write_audit("get_daily_bars", {"symbol": "SHFE.rb2610"}, rows=3,
                audit_dir=tmp_path)
    write_audit("get_daily_bars", {"symbol": "SHFE.rb2610"},
                error="rejected: bad window", audit_dir=tmp_path)
    files = sorted(tmp_path.glob("audit-*.jsonl"))
    assert len(files) == 1
    lines = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
    assert lines[0]["rows"] == 3 and lines[0]["tool"] == "get_daily_bars"
    assert lines[1]["error"].startswith("rejected")
    assert "secret" not in files[0].read_text(encoding="utf-8")


catalog = ROOT / "config" / "china_futures_catalog.json"
settlement_root = ROOT / "data" / "settlement_params"

pytestmark_ref = pytest.mark.skipif(
    not catalog.exists(), reason="catalog not present"
)


@pytest.mark.skipif(not catalog.exists(), reason="catalog not present")
def test_trading_calendar_and_products():
    from ak_trading.mcp_server import reference

    result = reference.trading_calendar("2026-09-07", "2026-09-12")
    assert result["trading_days"] == ["2026-09-07", "2026-09-08", "2026-09-09",
                                      "2026-09-10", "2026-09-11"]
    products = reference.products()
    assert products["rows"] >= 60
    assert {"exchange", "code", "name_zh"} <= set(products["products"][0])


@pytest.mark.skipif(not catalog.exists(), reason="catalog not present")
def test_trading_calendar_window_cap():
    from ak_trading.mcp_server import reference
    from ak_trading.mcp_server.queries import MarketMcpQueryError

    with pytest.raises(MarketMcpQueryError):
        reference.trading_calendar("2020-01-01", "2026-09-12")


@pytest.mark.skipif(not settlement_root.exists(), reason="settlement params not present")
def test_settlement_reference_wrapper():
    from ak_trading.mcp_server import reference
    from ak_trading.mcp_server.queries import MarketMcpQueryError

    result = reference.settlement_reference("2026-09-09", "SHFE.rb2610")
    assert result["symbol"] == "SHFE.rb2610"
    assert isinstance(result.get("available"), bool)
    assert "official" in result and "reference" in result
    with pytest.raises(MarketMcpQueryError):
        reference.settlement_reference("2026-09-09", "not a symbol")


def _build_public_app(token: str, **kwargs):
    from ak_trading.mcp_server.server import build_mcp_app

    return build_mcp_app(_StubMarket(), token=token, **kwargs)


def test_allowlist_rejects_other_clients():
    from starlette.testclient import TestClient

    app = _build_public_app("tok123", allowed_ips=("203.0.113.7",))
    with TestClient(app, client=("198.51.100.20", 50000)) as client:
        response = client.post(
            "/mcp", json={},
            headers={"Authorization": "Bearer tok123",
                     "Accept": "application/json, text/event-stream"},
        )
        assert response.status_code == 403


def test_allowlist_rejects_unparseable_client():
    from starlette.testclient import TestClient

    app = _build_public_app("tok123", allowed_ips=("203.0.113.7",))
    with TestClient(app) as client:  # default source host is the string "testclient"
        response = client.post(
            "/mcp", json={},
            headers={"Authorization": "Bearer tok123",
                     "Accept": "application/json, text/event-stream"},
        )
        assert response.status_code == 403


def test_allowlist_accepts_listed_client():
    from starlette.testclient import TestClient

    app = _build_public_app("tok123", allowed_ips=("127.0.0.1",))
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        response = client.post(
            "/mcp", json={},
            headers={"Authorization": "Bearer tok123",
                     "Accept": "application/json, text/event-stream"},
        )
        assert response.status_code != 403


def test_allowlist_accepts_cidr_client():
    from starlette.testclient import TestClient

    app = _build_public_app("tok123", allowed_ips=("10.0.75.0/24",))
    with TestClient(app, client=("10.0.75.9", 50000)) as client:
        response = client.post(
            "/mcp", json={},
            headers={"Authorization": "Bearer tok123",
                     "Accept": "application/json, text/event-stream"},
        )
        assert response.status_code != 403


def test_rate_limit_returns_429():
    app = _build_public_app("tok123", rate_per_minute=10)
    with _client(app) as client:
        codes = []
        for _ in range(14):
            response = client.post(
                "/mcp", json={},
                headers={"Authorization": "Bearer tok123",
                         "Accept": "application/json, text/event-stream"},
            )
            codes.append(response.status_code)
        assert codes.count(429) >= 1
        # healthz is never rate limited
        assert client.get("/healthz").status_code == 200


def test_rate_limit_ceiling_and_floor():
    from ak_trading.mcp_server.server import _AccessMiddleware

    class _Noop:
        async def __call__(self, scope, receive, send):
            return None

    assert _AccessMiddleware(_Noop(), token="t", rate_per_minute=5)._rate_per_minute == 10
    assert _AccessMiddleware(_Noop(), token="t", rate_per_minute=99_999)._rate_per_minute == 10_000


def test_rate_limit_applies_to_unauthorized_floods():
    app = _build_public_app("tok123", rate_per_minute=10)
    with _client(app) as client:
        codes = [
            client.post("/mcp", json={}, headers={"Authorization": "Bearer wrong"}).status_code
            for _ in range(14)
        ]
        assert codes.count(429) >= 1  # throttled even with a bad token
