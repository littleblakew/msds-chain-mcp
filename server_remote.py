"""
MSDS Chain MCP Server — Remote (HTTP SSE / Streamable HTTP)

Runs as a web server (uvicorn) instead of stdio, so external clients can
connect over HTTPS without running the server locally.

Usage:
    MSDS_API_KEY=sk-msds-xxx python server_remote.py
    MSDS_OAUTH_ENABLED=1 python server_remote.py

Environment Variables:
    MSDS_API_KEY       - API key for authenticating to MSDS Chain backend
    MSDS_API_URL       - Backend URL (defaults to production)
    MSDS_LANG          - Response language (en/zh/ja/de/id)
    MSDS_MCP_HOST      - Host to bind (default: 0.0.0.0)
    MSDS_MCP_PORT      - Port to listen on (default: 8080)
    MSDS_MCP_TRANSPORT - "sse" or "streamable-http" (default: streamable-http)
    MSDS_OAUTH_ENABLED - Set to "1" to enable OAuth 2.1 endpoints
    MSDS_OAUTH_ISSUER  - OAuth issuer URL (default: https://mcp.lagentbot.com)
    MSDS_OAUTH_SECRET  - Secret for signing tokens (auto-generated if not set)
"""
from __future__ import annotations

import os
import sys

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

# Import everything from the main server module (all tools are registered on `mcp`)
import server as _srv  # noqa: F401 — registers tools on `mcp`
from server import mcp

HOST = os.environ.get("MSDS_MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("MSDS_MCP_PORT", "8080"))
TRANSPORT = os.environ.get("MSDS_MCP_TRANSPORT", "streamable-http")
OAUTH_ENABLED = os.environ.get("MSDS_OAUTH_ENABLED", "0") == "1"


async def health(request: Request) -> JSONResponse:
    """Health check endpoint for container orchestration."""
    return JSONResponse({"status": "ok", "tools": 18, "oauth": OAUTH_ENABLED})


# Inject custom routes into FastMCP's Starlette app via _custom_starlette_routes.
# This avoids the sub-app mount issue where lifespan (task group init) doesn't propagate.
# Inject custom routes into FastMCP's Starlette app via _custom_starlette_routes.
# This avoids the sub-app mount issue where lifespan (task group init) doesn't propagate.
mcp._custom_starlette_routes.append(Route("/health", health, methods=["GET"]))

if OAUTH_ENABLED:
    from oauth import oauth_routes
    mcp._custom_starlette_routes.extend(oauth_routes)


if __name__ == "__main__":
    if TRANSPORT not in ("sse", "streamable-http"):
        print(f"Error: MSDS_MCP_TRANSPORT must be 'sse' or 'streamable-http', got '{TRANSPORT}'",
              file=sys.stderr)
        sys.exit(1)

    features = [TRANSPORT]
    if OAUTH_ENABLED:
        features.append("OAuth 2.1")

    print(f"MSDS Chain MCP Server ({', '.join(features)}) on {HOST}:{PORT}",
          file=sys.stderr)

    mcp.settings.host = HOST
    mcp.settings.port = PORT
    mcp.run(transport=TRANSPORT)
