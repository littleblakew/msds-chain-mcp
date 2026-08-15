"""
MSDS Chain MCP Server — Remote (HTTP SSE / Streamable HTTP)

Runs as a web server (uvicorn) instead of stdio, so external clients can
connect over HTTPS without running the server locally.

Transport strategy (PATH A — single-process dual-mount):
    Both transports are served concurrently from one process:
      - /mcp          → Streamable HTTP (primary; Copilot Studio, new clients)
      - /sse          → SSE (compatibility; legacy clients, Hermes MCP bridge)
      - /messages     → SSE message posting endpoint

    MSDS_MCP_TRANSPORT is kept for backward compatibility but is ignored at
    runtime — both transports are always active.

    Implementation note: routes from both MCPServer transport sub-apps are
    merged into a single outer Starlette app so that the streamable-http
    lifespan (task-group init) propagates correctly to all routes without
    the sub-app lifespan-propagation gap that appears when using Mount().

Usage:
    MSDS_API_KEY=sk-msds-xxx python server_remote.py
    uvicorn server_remote:app --host 0.0.0.0 --port 8080

Environment Variables:
    MSDS_API_KEY       - API key for authenticating to MSDS Chain backend
    MSDS_API_URL       - Backend URL (defaults to production)
    MSDS_LANG          - Response language (en/zh/ja/de/id)
    MSDS_MCP_HOST      - Host to bind (default: 0.0.0.0)
    MSDS_MCP_PORT      - Port to listen on (default: 8080)
    MSDS_MCP_TRANSPORT - Kept for backward compat; ignored (both transports active)

Note: OAuth 2.1 and well-known challenge endpoints have been moved to the
      gateway layer (msds-chain-gateway). This is the clean public MCP core.
"""
from __future__ import annotations

import os
import sys

from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcp.server.transport_security import TransportSecuritySettings

# Import everything from the main server module (all tools are registered on `mcp`)
import server as _srv  # noqa: F401 — registers tools on `mcp`
from server import mcp

from identity_middleware import IdentityMiddleware

HOST = os.environ.get("MSDS_MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("MSDS_MCP_PORT", "8080"))
TRANSPORT = os.environ.get("MSDS_MCP_TRANSPORT", "streamable-http")  # kept for compat


async def health(request: Request) -> JSONResponse:
    """Health check endpoint for container orchestration.

    Tool count is read from the live MCPServer registry (tools are registered on
    `mcp` in server.py) so it never drifts when tools are added or removed.
    """
    tools = await mcp.list_tools()
    return JSONResponse({"status": "ok", "tools": len(tools)})


# ---------------------------------------------------------------------------
# Build both transport sub-apps from the same MCPServer instance, then merge
# their routes into a single outer Starlette app.
#
# We do NOT use mcp._custom_starlette_routes (which bakes routes into one
# transport app) because we need the custom routes on the outer app, and we
# do NOT use Mount() because Starlette's Mount strips path prefixes before
# dispatching, which breaks the transport apps' internally-registered paths
# (/mcp, /sse, /messages).
#
# Instead we extract the Route/Mount objects from each transport app and
# combine them with our custom routes into one top-level router. The
# streamable-http lifespan drives startup so the task-group initialises
# before any requests arrive on either transport.
# ---------------------------------------------------------------------------
# mcp 2.x moved `transport_security` off the server ctor and onto each transport app,
# so it has to be passed here — once per transport, and it is NOT optional. Both
# builders take `host="127.0.0.1"` as default and auto-enable DNS rebinding protection
# when `transport_security is None and host in (127.0.0.1, localhost, ::1)`, pinning
# allowed_hosts to localhost only. Behind the Container Apps ingress the inbound Host
# header is our public domain, so omitting this would reject every real request.
# Auth is at the gateway, not here.
_no_dns_rebinding = TransportSecuritySettings(enable_dns_rebinding_protection=False)

# 🔴 请求体上限必须显式给，不能吃 SDK 默认的 4 MiB。
# `upload_msds_pdf` 的 schema、描述和 `_MAX_INLINE_PDF_BYTES` 都对外承诺**内联 base64
# 最大 10 MB（解码后）**，而 10 MB 的 base64 是 ~13.4 MB，早就撞穿 4 MiB ⇒ 超过 ~3 MB 的
# PDF 在传输层就被 `RequestBodyLimitMiddleware` 以裸 413 拒掉，MCP 层根本不会执行，
# 用户看到的是一句没有上下文的错误（经网关还会变成 500）。
# ⚠️ 这**不是 mcp 2.x 带来的**：`DEFAULT_MAX_REQUEST_BODY_SIZE = 4 MiB` 在 1.29 里就有
# （1.x 只是没把这个参数暴露到 `streamable_http_app()`，所以想改也改不了），实测当前
# Prod 打 5 MiB 同样失败。2.x 把它变成了可传参数，所以这次顺手把承诺兑现。
# 上限从 `_MAX_INLINE_PDF_BYTES` 推导，避免和应用层的限制各说各话；应用层那道
# `_reject_oversize_encoded` 仍在，越过传输层之后照样按 10 MB 拒。
_MAX_BODY_BYTES = (_srv._MAX_INLINE_PDF_BYTES * 4) // 3 + 262_144  # base64 膨胀 + JSON-RPC 信封

_streamable_app = mcp.streamable_http_app(
    transport_security=_no_dns_rebinding,
    max_request_body_size=_MAX_BODY_BYTES,
)   # registers /mcp
_sse_app = mcp.sse_app(transport_security=_no_dns_rebinding)   # registers /sse + /messages

_routes: list = [
    Route("/health", health, methods=["GET"]),
]

# Merge transport routes (preserves the handler references built by MCPServer).
_routes.extend(_streamable_app.router.routes)   # /mcp
_routes.extend(_sse_app.router.routes)           # /sse, /messages

# Build the combined application.
app = Starlette(
    routes=_routes,
    lifespan=_streamable_app.router.lifespan_context,
)

# IdentityMiddleware: extracts caller credentials (Bearer token / X-Api-Key)
# from inbound request headers and stores them in a request-scoped contextvar
# so all tool handlers can read the caller identity without threading state.
app.add_middleware(IdentityMiddleware)

# CORS: wildcard origin is safe because auth is via Authorization header
# (not cookies), so allow_credentials stays False.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Mcp-Session-Id"],
)


if __name__ == "__main__":
    features = ["streamable-http /mcp (primary)", "sse /sse (compat)"]

    print(f"MSDS Chain MCP Server ({', '.join(features)}) on {HOST}:{PORT}",
          file=sys.stderr)

    # mcp 2.x dropped host/port from Settings; they were never read by the SDK on this
    # path anyway — uvicorn.run() below is what actually binds.
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT, log_level=mcp.settings.log_level.lower())
