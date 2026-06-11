"""Guard tests for the remote-server HTTP routes (server_remote.py).

These protect the endpoints the container orchestration depends on:
- /health  (container probe + status)

Note: OAuth 2.1 and /.well-known/openai-apps-challenge have moved to the
gateway layer and are no longer part of this module.
"""
import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

import server_remote


@pytest.fixture()
def client():
    # Exercise the actual handler function through real route wiring.
    app = Starlette(
        routes=[
            Route("/health", server_remote.health, methods=["GET"]),
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
    assert "oauth" not in body
