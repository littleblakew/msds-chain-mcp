r"""Each tool call gets one id that reaches the backend twice: as the `X-MCP-Call-Id` header
on every request the tool makes, and as `mcp_call_id` in the call-log POST. The backend
stores both, which is what lets an audit row be joined to the reply we logged.

The backend half (columns, header parsing, the join) lives in msds-chain
`backend/tests/test_ci548_mcp_call_id.py`.

| guard | what reverting makes it fail |
|---|---|
| `test_backend_request_and_call_log_share_one_id` | drop the header in `caller_headers`, or the `"mcp_call_id"` field in `_log_call`'s POST body |
| `test_each_call_gets_its_own_id` | mint the id once at import instead of per call |
| `test_id_does_not_outlive_the_call` | drop `reset_mcp_call_id(call_token)` |

Async cases use `asyncio.run` (no pytest-asyncio in requirements-dev.txt, see test_response_kind.py).
"""
import asyncio
import re

import server
from request_identity import caller_headers, set_caller_credential


def _fake_backend(monkeypatch) -> list[dict]:
    sent: list[dict] = []

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None, **kw):
            sent.append({"url": url, "json": json or {}, "headers": dict(headers or {})})
            return _Resp()

    monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kw: _Client())
    return sent


@server._reported
async def _probe_tool():
    """Stands in for a real tool: declares a log intent and makes one backend request."""
    server._log_intent("probe_tool", None)
    async with server.httpx.AsyncClient(timeout=1) as client:
        await client.post(f"{server.API_URL}/api/v2/compatibility/check",
                          json={}, headers=server._headers())
    return "ok"


def _split(sent: list[dict]) -> tuple[dict, dict]:
    backend = [s for s in sent if not s["url"].endswith("/mcp/call-log")]
    logs = [s for s in sent if s["url"].endswith("/mcp/call-log")]
    assert len(backend) == 1 and len(logs) == 1, sent
    return backend[0], logs[0]


def test_backend_request_and_call_log_share_one_id(monkeypatch):
    set_caller_credential("sk-msds-test")
    try:
        sent = _fake_backend(monkeypatch)
        asyncio.run(_probe_tool())
    finally:
        set_caller_credential(None)

    backend, log = _split(sent)
    header = backend["headers"].get("X-MCP-Call-Id")
    assert header and re.fullmatch(r"[0-9a-f]{32}", header), backend["headers"]
    assert log["json"]["mcp_call_id"] == header


def test_each_call_gets_its_own_id(monkeypatch):
    sent = _fake_backend(monkeypatch)
    asyncio.run(_probe_tool())
    asyncio.run(_probe_tool())

    ids = [s["json"]["mcp_call_id"] for s in sent if s["url"].endswith("/mcp/call-log")]
    assert len(ids) == 2 and ids[0] != ids[1], ids


def test_id_does_not_outlive_the_call(monkeypatch):
    _fake_backend(monkeypatch)

    async def run_then_read():
        await _probe_tool()
        return caller_headers()

    headers = asyncio.run(run_then_read())
    assert "X-MCP-Call-Id" not in headers, headers
