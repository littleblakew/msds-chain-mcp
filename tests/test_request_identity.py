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
