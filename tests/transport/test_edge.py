"""The hosted edge (`edge.py`, on with `MSDS_MCP_EDGE=1`).

The edge is what an MCP client meets before any tool runs, so the properties pinned
here are the ones a client depends on to recover by itself: a 401 that says where to
re-authorize, a discovery document that names the authorization server, OAuth paths
that reach the backend unchanged, and no credential in any log line.

Mutations (each turns at least one test red):
  · drop `error="invalid_token"` from `unauthorized_headers`   → test_dead_credential_gets_invalid_token
  · make `check_credential` return INVALID on a 5xx           → test_backend_outage_fails_open
  · stop stripping `transfer-encoding` in `_to_backend`       → test_oauth_passthrough_is_faithful
  · remove the OPTIONS exemption                              → test_cors_preflight_is_not_gated
  · log `cred` instead of `credential_fingerprint(cred)`      → test_log_line_carries_no_credential
  · record the funnel before checking the JSON-RPC result     → test_jsonrpc_error_is_not_a_first_call
"""
import asyncio
import json
import logging

import httpx
import pytest

import edge

KEY = "sk-msds-" + "k" * 32


class _FakeResp:
    def __init__(self, status=200, payload=None, content=b"", headers=None):
        self.status_code = status
        self._payload = payload
        self.content = content or (json.dumps(payload).encode() if payload is not None else b"")
        self.headers = httpx.Headers(headers or {})

    def json(self):
        return self._payload


class _Backend:
    """Stands in for `httpx.AsyncClient` inside `edge`; records every request."""

    def __init__(self):
        self.calls = []
        self.whoami = (200, {"user_id": 7})
        self.oauth = _FakeResp(200, {"ok": True}, headers={"content-type": "application/json"})

    def client(self, *a, **kw):
        backend = self

        class _C:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url, headers=None):
                backend.calls.append(("GET", url, dict(headers or {}), b""))
                status, payload = backend.whoami
                if status is None:
                    raise httpx.ConnectError("down")
                return _FakeResp(status, payload)

            async def post(self, url, json=None, headers=None):
                backend.calls.append(("POST", url, dict(headers or {}), json))
                return _FakeResp(201, {"id": 1})

            async def request(self, method, url, content=b"", headers=None):
                backend.calls.append((method, url, list(headers or []), content))
                return backend.oauth
        return _C()


@pytest.fixture
def backend(monkeypatch):
    b = _Backend()
    monkeypatch.setattr(edge, "_http", b.client)
    monkeypatch.setenv("MSDS_API_URL", "https://backend.test")
    monkeypatch.setenv("MSDS_MCP_PUBLIC_URL", "https://mcp.test")
    edge._verdicts.clear()
    edge._funnel_seen.clear()
    return b


class _Inner:
    """The wrapped MCP app: echoes the body it received, with a configurable reply."""

    def __init__(self):
        self.seen = []
        self.reply = b'{"jsonrpc":"2.0","id":1,"result":{}}'

    async def __call__(self, scope, receive, send):
        body = b""
        if scope["method"] == "POST":
            msg = await receive()
            body = msg.get("body", b"")
        self.seen.append((scope["path"], scope["method"], body))
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": self.reply})


def _client(inner):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=edge.EdgeMiddleware(inner)),
                             base_url="https://mcp.test")


def _run(coro):
    return asyncio.run(coro)


def _rpc(method, name=None):
    p = {"jsonrpc": "2.0", "id": 1, "method": method}
    if name:
        p["params"] = {"name": name}
    return json.dumps(p)


def test_no_credential_gets_401_with_discovery_pointer(backend):
    async def go():
        async with _client(_Inner()) as c:
            return await c.post("/mcp", content=_rpc("initialize"))
    r = _run(go())
    assert r.status_code == 401
    www = r.headers["www-authenticate"]
    assert 'resource_metadata="https://mcp.test/.well-known/oauth-protected-resource/mcp"' in www
    assert "invalid_token" not in www
    assert backend.calls == []   # nothing to ask the backend about


def test_dead_credential_gets_invalid_token(backend):
    backend.whoami = (401, {"detail": "no"})

    async def go():
        async with _client(_Inner()) as c:
            return await c.post("/mcp", content=_rpc("tools/list"),
                                headers={"Authorization": f"Bearer {KEY}"})
    r = _run(go())
    assert r.status_code == 401 and 'error="invalid_token"' in r.headers["www-authenticate"]


def test_valid_credential_reaches_the_app_with_its_body(backend):
    inner = _Inner()
    body = _rpc("tools/list")

    async def go():
        async with _client(inner) as c:
            return await c.post("/mcp", content=body, headers={"X-API-Key": KEY})
    r = _run(go())
    assert r.status_code == 200
    assert inner.seen == [("/mcp", "POST", body.encode())]
    assert backend.calls[0][2] == {"X-API-Key": KEY}


def test_backend_outage_fails_open(backend):
    for whoami in ((None, None), (503, {})):
        backend.whoami = whoami
        edge._verdicts.clear()

        async def go():
            async with _client(_Inner()) as c:
                return await c.post("/mcp", content=_rpc("tools/list"), headers={"X-API-Key": KEY})
        assert _run(go()).status_code == 200, whoami


def test_cors_preflight_is_not_gated(backend):
    inner = _Inner()

    async def go():
        async with _client(inner) as c:
            return await c.options("/mcp")
    assert _run(go()).status_code == 200 and inner.seen


def test_protected_resource_document(backend):
    async def go():
        async with _client(_Inner()) as c:
            return (await c.get("/.well-known/oauth-protected-resource/mcp"),
                    await c.get("/.well-known/oauth-protected-resource/elsewhere"))
    ok, unknown = _run(go())
    doc = ok.json()
    assert doc["resource"] == "https://mcp.test/mcp"
    assert doc["authorization_servers"] == ["https://mcp.test"]
    assert "scopes_supported" not in doc
    assert unknown.status_code == 404


def test_oauth_passthrough_is_faithful(backend, monkeypatch):
    monkeypatch.setenv("MSDS_INTERNAL_PROXY_SECRET", "s3cret")
    backend.oauth = _FakeResp(302, content=b"", headers={"location": "https://client/cb?code=x",
                                                          "content-encoding": "gzip"})

    async def go():
        async with _client(_Inner()) as c:
            return await c.post("/oauth/token?x=1", content=b"grant_type=refresh_token",
                                headers={"transfer-encoding": "chunked",
                                         "content-type": "application/x-www-form-urlencoded",
                                         "x-forwarded-for": "203.0.113.9"})
    r = _run(go())
    method, url, headers, content = backend.calls[-1]
    sent = {k.lower(): v for k, v in headers}
    assert (method, url, content) == ("POST", "https://backend.test/oauth/token?x=1",
                                      b"grant_type=refresh_token")
    assert "transfer-encoding" not in sent and "host" not in sent
    assert sent["x-internal-proxy"] == "s3cret" and sent["x-forwarded-for"] == "203.0.113.9"
    assert r.status_code == 302 and r.headers["location"] == "https://client/cb?code=x"
    assert "content-encoding" not in r.headers


def test_log_line_carries_no_credential(backend, caplog):
    caplog.set_level(logging.INFO, logger="msds_mcp.edge")

    async def go():
        async with _client(_Inner()) as c:
            await c.post("/mcp", content=_rpc("tools/list"), headers={"X-API-Key": KEY})
    _run(go())
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("mcp_client ")]
    assert lines, "no mcp_client line"
    assert KEY not in "".join(lines) and KEY[:16] not in "".join(lines)
    fields = json.loads(lines[-1][len("mcp_client "):])
    assert fields["cred_fp"] == edge.credential_fingerprint(KEY) and fields["uid"] == 7


def _funnel_steps(backend):
    return [c[3]["step"] for c in backend.calls if c[1].endswith("/mcp/funnel-event")]


def test_first_tool_call_records_funnel_once(backend):
    async def go():
        async with _client(_Inner()) as c:
            for _ in range(2):
                await c.post("/mcp", content=_rpc("tools/call", "get_storage_guidance"),
                             headers={"X-API-Key": KEY})
            await asyncio.sleep(0)
    _run(go())
    assert _funnel_steps(backend) == ["tools_listed", "first_call"]
    first = [c for c in backend.calls if c[1].endswith("/mcp/funnel-event")][-1][3]
    assert first["api_key"] == KEY and first["detail"] == "get_storage_guidance"


def test_jsonrpc_error_is_not_a_first_call(backend):
    inner = _Inner()
    inner.reply = b'{"jsonrpc":"2.0","id":1,"error":{"code":-32601,"message":"nope"}}'

    async def go():
        async with _client(inner) as c:
            await c.post("/mcp", content=_rpc("tools/call", "x"), headers={"X-API-Key": KEY})
            await asyncio.sleep(0)
    _run(go())
    assert _funnel_steps(backend) == []


def test_edge_is_off_by_default(monkeypatch):
    monkeypatch.delenv("MSDS_MCP_EDGE", raising=False)
    assert edge.enabled() is False


_PROBE = """
import json, server_remote, edge
opts = server_remote.mcp._lowlevel_server.create_initialization_options()
stack = [m.cls.__name__ for m in server_remote.app.user_middleware]
print(json.dumps({"instructions": opts.instructions, "stack": stack}))
"""


def _probe(edge_on: bool) -> dict:
    """Import server_remote in a child process: edge mode mutates module-level state
    (the served instructions), which must not leak into the rest of this test run."""
    import os
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    env.pop("MSDS_MCP_EDGE", None)
    if edge_on:
        env["MSDS_MCP_EDGE"] = "1"
    out = subprocess.run([sys.executable, "-c", _PROBE], cwd=root, env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_edge_mode_serves_hosted_instructions_and_gates():
    from pathlib import Path
    hosted = (Path(edge.__file__).with_name("hosted_instructions.txt")).read_text(encoding="utf-8")
    on, off = _probe(True), _probe(False)
    assert on["instructions"] == hosted and "EdgeMiddleware" in on["stack"]
    assert off["instructions"] != hosted and "EdgeMiddleware" not in off["stack"]


def test_oauth_passthrough_drops_caller_trust_headers(backend, monkeypatch):
    monkeypatch.delenv("MSDS_INTERNAL_PROXY_SECRET", raising=False)

    async def go():
        async with _client(_Inner()) as c:
            return await c.post("/oauth/token", content=b"x=1", headers={
                "x-internal-proxy": "forged", "x-mcp-log-secret": "forged",
                "x-bot-secret": "forged", "x-tenant-slug": "acme", "x-workspace-id": "1"})
    _run(go())
    sent = {k.lower() for k, _ in backend.calls[-1][2]}
    assert not sent & {"x-internal-proxy", "x-mcp-log-secret", "x-bot-secret",
                       "x-tenant-slug", "x-workspace-id"}, sent


def test_unauthenticated_body_is_never_read(backend):
    """401 is decided from headers alone; an oversized authenticated body gets 413."""
    async def go():
        mw = edge.EdgeMiddleware(_Inner(), max_body_bytes=10)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mw),
                                     base_url="https://mcp.test") as c:
            return (await c.post("/mcp", content=b"x" * 1000),
                    await c.post("/mcp", content=b"x" * 1000, headers={"X-API-Key": KEY}))
    anon, big = _run(go())
    assert anon.status_code == 401 and big.status_code == 413


def test_failed_funnel_send_is_retried(backend):
    posts = []

    def client(*a, **kw):
        c = backend.client()
        orig = c.post

        async def flaky(url, json=None, headers=None):
            posts.append(json["step"])
            if len(posts) <= 2:
                raise httpx.ConnectError("blip")
            return await orig(url, json=json, headers=headers)
        c.post = flaky
        return c

    edge._http = client  # restored by monkeypatch in the fixture's teardown

    async def go():
        async with _client(_Inner()) as c:
            for _ in range(2):
                await c.post("/mcp", content=_rpc("tools/call", "t"), headers={"X-API-Key": KEY})
                for _ in range(3):
                    await asyncio.sleep(0)
    _run(go())
    assert posts.count("first_call") == 2, posts


def test_client_ip_probe_is_passed_through(backend):
    async def go():
        async with _client(_Inner()) as c:
            return await c.get("/health/client-ip")
    assert _run(go()).status_code == 200
    assert backend.calls[-1][1] == "https://backend.test/health/client-ip"
