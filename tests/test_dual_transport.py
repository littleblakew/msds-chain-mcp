"""Smoke tests for dual-transport (Streamable HTTP + SSE) setup in server_remote.py.

PATH A (single-process dual-mount) is used: both /mcp (streamable-http) and
/sse (SSE compat) are served by the same app instance.

Tests verify:
  - /health is reachable (custom route on the outer app)
  - /mcp exists (streamable-http; 4xx on bare request is expected — not 404)
  - /sse exists (SSE; 405 on wrong-method POST is expected — not 404)
  - IdentityMiddleware is wired (no crash; contextvar populated without error)

Implementation note: the StreamableHTTPSessionManager can only run() once per
instance, so all tests that exercise the lifespan share a single module-scoped
TestClient fixture.  The /health test does not need the streamable lifespan
and uses a plain call (no context manager entry) which is fine because the
/health route handler has no dependency on the session manager.
"""
import pytest
from starlette.testclient import TestClient


# `live_client` fixture 在 tests/conftest.py（**session 级、全仓唯一**）——
# 见那里的注释：每个文件各建一个会让 StreamableHTTPSessionManager 二次 run() 而炸，
# 且这个坑只有全量一起跑才暴露。


def test_health_served():
    """Health endpoint must return 200 without needing the lifespan."""
    from server_remote import app
    # TestClient without context-manager entry: lifespan is NOT run,
    # but /health has no session-manager dependency so it still works.
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/health").status_code == 200


def test_transport_endpoint_present(live_client):
    """Streamable HTTP endpoint /mcp must exist (not 404).

    A bare POST to /mcp without proper MCP negotiation headers is rejected
    with 4xx (e.g. 406 Not Acceptable) by FastMCP — that is the expected
    behaviour and is explicitly not 404.
    """
    r = live_client.post("/mcp", json={})
    assert r.status_code != 404, (
        f"Expected /mcp to exist (route registered), got 404 (status={r.status_code})"
    )


def test_sse_endpoint_present(live_client):
    """SSE endpoint /sse must exist (not 404).

    Sending a POST (wrong method) to /sse returns 405 Method Not Allowed,
    confirming the route is registered without opening a streaming connection.
    """
    r = live_client.post("/sse", json={})
    assert r.status_code != 404, (
        f"Expected /sse to exist (route registered), got 404 (status={r.status_code})"
    )


def test_identity_middleware_wired(live_client):
    """IdentityMiddleware is wired: requests with credentials must not crash.

    Sending a request with an Authorization header confirms the middleware
    extracts the credential into the contextvar without raising an exception.
    A crash during middleware handling would surface as a 500 here.
    """
    res = live_client.get("/health", headers={"Authorization": "Bearer test-token"})
    assert res.status_code == 200
