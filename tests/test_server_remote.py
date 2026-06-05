"""Guard tests for the remote-server HTTP routes (server_remote.py).

These protect the endpoints the OpenAI ChatGPT Apps listing depends on:
- /health                              (container orchestration + status)
- /.well-known/openai-apps-challenge   (OpenAI domain-ownership verification)

A regression here (broken route, dropped/altered challenge token) would
silently delist the MCP server from OpenAI, so it must fail CI loudly.
"""
import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

import server_remote


@pytest.fixture()
def client():
    # Exercise the actual handler functions through real route wiring.
    app = Starlette(
        routes=[
            Route("/health", server_remote.health, methods=["GET"]),
            Route(
                "/.well-known/openai-apps-challenge",
                server_remote.openai_apps_challenge,
                methods=["GET"],
            ),
        ]
    )
    with TestClient(app) as c:
        yield c


def test_health_returns_ok_status_and_tool_count(client):
    res = client.get("/health")

    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert isinstance(body["tools"], int) and body["tools"] > 0
    assert "oauth" in body


def test_openai_apps_challenge_returns_token_as_plain_text(client):
    res = client.get("/.well-known/openai-apps-challenge")

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/plain")
    # The domain-verification token must be a non-empty body with no markup.
    body = res.text
    assert body.strip() == server_remote.OPENAI_APPS_CHALLENGE_TOKEN
    assert body.strip() != ""
    assert "<" not in body


def test_openai_apps_challenge_token_is_env_overridable(client, monkeypatch):
    monkeypatch.setattr(server_remote, "OPENAI_APPS_CHALLENGE_TOKEN", "custom-token-xyz")

    res = client.get("/.well-known/openai-apps-challenge")

    assert res.status_code == 200
    assert res.text == "custom-token-xyz"
