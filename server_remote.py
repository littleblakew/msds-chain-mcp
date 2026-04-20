"""
MSDS Chain MCP Server — Remote (HTTP SSE / Streamable HTTP)

Runs as a web server (uvicorn) instead of stdio, so external clients can
connect over HTTPS without running the server locally.

Usage:
    MSDS_API_KEY=sk-msds-xxx python server_remote.py

    # Or with custom port/host:
    MSDS_MCP_PORT=8080 MSDS_MCP_HOST=0.0.0.0 python server_remote.py

Environment Variables:
    MSDS_API_KEY   - API key for authenticating to MSDS Chain backend
    MSDS_API_URL   - Backend URL (defaults to production)
    MSDS_LANG      - Response language (en/zh/ja/de/id)
    MSDS_MCP_HOST  - Host to bind (default: 0.0.0.0)
    MSDS_MCP_PORT  - Port to listen on (default: 8080)
    MSDS_MCP_TRANSPORT - "sse" or "streamable-http" (default: streamable-http)
"""
from __future__ import annotations

import os
import sys

# Import everything from the main server module (all tools are registered on `mcp`)
from server import mcp  # noqa: F401

HOST = os.environ.get("MSDS_MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("MSDS_MCP_PORT", "8080"))
TRANSPORT = os.environ.get("MSDS_MCP_TRANSPORT", "streamable-http")

if __name__ == "__main__":
    if TRANSPORT not in ("sse", "streamable-http"):
        print(f"Error: MSDS_MCP_TRANSPORT must be 'sse' or 'streamable-http', got '{TRANSPORT}'",
              file=sys.stderr)
        sys.exit(1)

    print(f"Starting MSDS Chain MCP Server ({TRANSPORT}) on {HOST}:{PORT}", file=sys.stderr)
    mcp.settings.host = HOST
    mcp.settings.port = PORT
    mcp.run(transport=TRANSPORT)
