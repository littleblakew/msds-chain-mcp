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
import asyncio
import json

import pytest
from starlette.testclient import TestClient

import server

# modern（2026-07-28）腿要求的信封；少了它 `prompts/list` 直接 400 `-32602`。
_MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}


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



def test_prompts_are_listed_over_the_real_transport(live_client):
    """🔴 进程内 `list_prompts()` 能用，**不等于 host 真正走的那条 HTTP 面列得出来**。

    本票的判据是「外部 host 真的调了 `prompts/list`」，那条路是 streamable HTTP
    （`server_remote.app`），不是我们在测试里直接调的那个对象。两者之间隔着一层
    协议处理 —— 而「注册了但没被通告」与「压根没注册」对 host 完全同形。
    🔴 请求必须是完整的 **modern（2026-07-28）形状**：带 `MCP-Protocol-Version` 头
    ＋ `params._meta` 信封。少了 `_meta` 会得到 **400 `-32602`**，而那个 400
    **不是产品的证据，是这条测试自己写坏了**。另外 `Mcp-Method` 头必须与 body 里的 method
    **逐字一致**，缺了它报的是 `-32020 header does not match`（**不是「缺了头」**，
    所以照报错字面去查会走偏）—— 照抄 `test_cache_hints.py`，别删。
    📌 复用 conftest 那个 **session 级** `live_client`（别在本文件自己 `TestClient(app)`：
    `StreamableHTTPSessionManager.run()` 每个实例只能调一次，第二个必然炸，
    而且**单文件跑不出来、只有全量一起跑才炸**）。
    """
    r = live_client.post(
        "/mcp",
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream",
                 "MCP-Protocol-Version": "2026-07-28",
                 "Mcp-Method": "prompts/list"},
        content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "prompts/list",
                            "params": {"_meta": _MODERN_META}}),
    )
    assert r.status_code == 200, f"prompts/list 返回 {r.status_code}: {r.text[:300]}"
    body = r.json()
    assert "result" in body, f"prompts/list 没有 result: {str(body)[:300]}"
    listed = {p["name"] for p in body["result"].get("prompts", [])}
    registered = {p.name for p in asyncio.run(server.mcp.list_prompts())}
    assert listed == registered, (
        f"传输层列出的 prompt 与注册的不一致：传输层 {sorted(listed)} vs "
        f"注册 {sorted(registered)} —— host 只看得见前者")
    assert listed, "传输层一个 prompt 都没列出来 ⇒ host 那边等于我们没做这张票"
