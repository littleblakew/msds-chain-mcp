import asyncio
import server
from request_identity import set_caller_credential, get_caller_credential, caller_headers


def test_contextvar_roundtrip():
    async def run():
        set_caller_credential("sk-msds-abc")
        assert get_caller_credential() == "sk-msds-abc"
        assert caller_headers()["X-API-Key"] == "sk-msds-abc"
    asyncio.run(run())


def test_bearer_jwt_credential():
    async def run():
        set_caller_credential("Bearer eyJ.jwt.tok")
        h = caller_headers()
        assert h["Authorization"] == "Bearer eyJ.jwt.tok"
        assert "X-API-Key" not in h
    asyncio.run(run())


def test_missing_credential_yields_bare_headers():
    async def run():
        h = caller_headers()
        assert h == {"Content-Type": "application/json"}
    asyncio.run(run())


def test_server_headers_use_caller_credential():
    async def run():
        set_caller_credential("sk-msds-caller")
        h = server._headers()
        assert h["X-API-Key"] == "sk-msds-caller"
    asyncio.run(run())


# --- Stateful tools must gate on the per-request caller credential, not the ---
# --- global MSDS_API_KEY env (which is empty under the remote gateway model). --

class _FakeResp:
    # 🔴 `status_code` 不是可选的：真的 `httpx.Response` 必然有它，而 CI-410 之后
    # 错误路径要按状态码分岔（422 带上后端给的原因）。假响应缺这个字段＝**fixture 比
    # 生产窄**，会在与本测试无关的地方炸出 AttributeError。
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code
        self.headers: dict = {}

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _FakeClient:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **k):
        if url.endswith("/chemicals"):
            return _FakeResp({"added": [{"name": "acetone", "status": "added"}], "not_found": []})
        if url.endswith("/compatibility"):
            return _FakeResp({"matrix": [], "warnings": []})
        if url.endswith("/sessions"):
            return _FakeResp({"session_id": "sess-test"})
        return _FakeResp({})


def test_create_audit_session_gates_on_caller_credential(monkeypatch):
    """Empty MSDS_API_KEY env but a present per-request credential must NOT be
    refused (the old `if not API_KEY` guard wrongly blocked remote callers)."""
    monkeypatch.setattr(server, "API_KEY", "")          # remote model: no global env key
    monkeypatch.setattr(server.httpx, "AsyncClient", _FakeClient)

    def as_text(r):
        return r if isinstance(r, str) else r.content[0].text

    async def run():
        set_caller_credential(None)
        msg = as_text(await server.create_audit_session("exp", ["acetone"]))
        assert "requires an authenticated api key" in msg.lower()

        set_caller_credential("sk-msds-caller")
        out = as_text(await server.create_audit_session("exp", ["acetone"]))
        assert "requires an authenticated" not in out.lower()
        assert "sess-test" in out

    asyncio.run(run())
