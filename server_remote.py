"""
MSDS Chain MCP Server — Remote (HTTP SSE / Streamable HTTP)

Runs as a web server (uvicorn) instead of stdio, so external clients can
connect over HTTPS without running the server locally.

Usage:
    # Basic (uses MSDS_API_KEY for all requests):
    MSDS_API_KEY=sk-msds-xxx python server_remote.py

    # With OAuth 2.1 (for Marketplace — each user brings their own key):
    MSDS_OAUTH_ENABLED=1 python server_remote.py

    # Custom port/host:
    MSDS_MCP_PORT=8080 MSDS_MCP_HOST=0.0.0.0 python server_remote.py

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

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, Mount

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


def build_app() -> Starlette:
    """Build the combined Starlette app with MCP + OAuth + health check."""
    routes = [
        Route("/health", health, methods=["GET"]),
    ]

    if OAUTH_ENABLED:
        from oauth import oauth_routes
        routes.extend(oauth_routes)

    # Get MCP's ASGI app
    if TRANSPORT == "sse":
        mcp_asgi = mcp.sse_app()
    else:
        mcp_asgi = mcp.streamable_http_app()

    # Mount MCP at /mcp and oauth + health at root
    routes.append(Mount("/mcp", app=mcp_asgi))

    return Starlette(routes=routes)


if __name__ == "__main__":
    if TRANSPORT not in ("sse", "streamable-http"):
        print(f"Error: MSDS_MCP_TRANSPORT must be 'sse' or 'streamable-http', got '{TRANSPORT}'",
              file=sys.stderr)
        sys.exit(1)

    features = [TRANSPORT]
    if OAUTH_ENABLED:
        features.append("OAuth 2.1")

    print(f"Starting MSDS Chain MCP Server ({', '.join(features)}) on {HOST}:{PORT}",
          file=sys.stderr)

    app = build_app()
    config = uvicorn.Config(app, host=HOST, port=PORT, log_level="info")
    server = uvicorn.Server(config)

    import asyncio
    asyncio.run(server.serve())
