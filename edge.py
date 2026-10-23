"""Public edge for the hosted MCP endpoint: everything a client meets before a tool runs.

Off unless `MSDS_MCP_EDGE=1`. A self-hosted or stdio server never needs it; the hosted
deployment turns it on when this process is the one facing the internet.

What it does, per request path:
  /oauth/*, /.well-known/oauth-authorization-server
        passed through unchanged to the backend, which is the authorization server
  /.well-known/oauth-protected-resource[/mcp|/sse]
        RFC 9728 document naming that authorization server
  /.well-known/openai-apps-challenge
        ownership token for the OpenAI apps directory
  /mcp, /sse, /messages
        requires a credential the backend accepts; otherwise 401 with a
        `WWW-Authenticate` that tells the client where to re-authorize. Each request
        writes one `mcp_client` log line, and first use of tools / prompts per
        credential becomes an onboarding-funnel event.

Credentials are never logged. Log lines carry only a sha256 prefix of a credential,
which identifies "the same key as last time" without being usable as one.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import sys
import time
from collections import OrderedDict

import httpx
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("msds_mcp.edge")

PRODUCT_NAME = "MSDS Chain"


def enabled() -> bool:
    return os.environ.get("MSDS_MCP_EDGE") == "1"


def _backend_url() -> str:
    return os.environ.get("MSDS_API_URL", "https://api.msdschain.lagentbot.com").rstrip("/")


def _canonical_base() -> str:
    return os.environ.get("MSDS_MCP_PUBLIC_URL", "https://mcp.lagentbot.com").rstrip("/")


_OAUTH_PREFIX = "/oauth/"
_AS_METADATA_PATH = "/.well-known/oauth-authorization-server"
# Echoes the caller's address as the backend resolved it. Passed through so the one
# question that decides whether per-IP limits work behind this edge (does the backend
# see the user, or us?) can be answered by reading it back rather than by inference.
_CLIENT_IP_PATH = "/health/client-ip"
# 🔴 Allowlist, not denylist: the backend trusts headers that only our own components may
# set (`x-internal-proxy`, `x-mcp-log-secret`, `x-bot-secret`, tenant / schema selectors).
# Forwarding whatever a caller sent would let them assert any of those; a denylist would
# miss the next one someone adds. The OAuth endpoints need nothing beyond these.
_FORWARD_HEADERS = {"content-type", "accept", "accept-language", "user-agent", "origin",
                    "referer", "x-forwarded-for", "x-forwarded-proto"}
_PRM_PATH = "/.well-known/oauth-protected-resource"
_RESOURCE_PATHS = ("/mcp", "/sse")
_CHALLENGE_PATH = "/.well-known/openai-apps-challenge"

# RFC 9110 §7.6.1: these belong to one hop. Forwarding `transfer-encoding: chunked`
# alongside a body we already read to a fixed length gives the upstream two framings for
# one request, which an ingress answers with a bare 400 and no log on either side.
_HOP_BY_HOP = {"connection", "keep-alive", "proxy-connection", "te", "trailer",
               "transfer-encoding", "upgrade"}


def _hop_by_hop(headers: Headers) -> set[str]:
    named = {t.strip().lower() for v in headers.getlist("connection") for t in v.split(",")}
    return _HOP_BY_HOP | {t for t in named if t}


def _http(**kw) -> httpx.AsyncClient:
    """Outbound client factory; one seam so tests replace the backend, not httpx."""
    return httpx.AsyncClient(**kw)


# --------------------------------------------------------------------------- credentials
def credential_fingerprint(cred: str | None) -> str:
    """Irreversible 12-hex sha256 prefix: never the credential or any prefix of it."""
    return hashlib.sha256(cred.encode()).hexdigest()[:12] if cred else "-"


def presented_credential(headers: Headers) -> tuple[str | None, str]:
    """`(credential as the core forwards it, how)`.

    `how` is `header_key` (X-API-Key), `bearer` (Authorization: Bearer …) or `none`.
    A bearer is passed through whatever it is: OAuth access tokens are `sk-msds-` keys,
    and the backend is the one that decides what it accepts.
    """
    api_key = (headers.get("x-api-key") or "").strip()
    if api_key:
        return api_key, "header_key"
    auth = headers.get("authorization") or ""
    if auth.startswith("Bearer ") and auth[7:].strip():
        return auth, "bearer"
    return None, "none"


VALID, INVALID, UNKNOWN = "valid", "invalid", "unknown"

# Verdicts are cached by an in-process HMAC of the credential (restart invalidates it,
# never comparable across processes), bounded, and negative verdicts cached briefly too:
# without that, anyone spraying junk tokens makes us call the backend once per request.
_CACHE_SECRET = secrets.token_bytes(32)
_CACHE_MAX = 2048
_VALID_TTL = 60.0
_INVALID_TTL = 10.0
# Briefly, so a backend outage costs one timeout per credential, not one per request.
_UNKNOWN_TTL = 5.0
_verdicts: "OrderedDict[str, tuple[str, int | None, float]]" = OrderedDict()


def _cache_key(cred: str) -> str:
    return hmac.new(_CACHE_SECRET, cred.encode(), hashlib.sha256).hexdigest()


async def check_credential(cred: str) -> tuple[str, int | None]:
    """`(verdict, user_id)` from the backend's identity-only endpoint.

    🔴 Fails open (`unknown`) on timeouts and 5xx: answering 401 there would send every
    connected client into re-authorization during a backend blip, which is worse than
    letting the tool call fail on its own. Only a definite 401/403 is `invalid`.
    """
    key = _cache_key(cred)
    hit = _verdicts.get(key)
    if hit and hit[2] > time.monotonic():
        _verdicts.move_to_end(key)
        return hit[0], hit[1]
    header = ({"Authorization": cred} if cred.startswith("Bearer ")
              else {"X-API-Key": cred})
    try:
        async with _http(timeout=5) as c:
            r = await c.get(f"{_backend_url()}/api/v2/whoami", headers=header)
    except httpx.HTTPError as exc:
        logger.warning("edge whoami failed: %s", type(exc).__name__)
        _remember(key, UNKNOWN, None, _UNKNOWN_TTL)
        return UNKNOWN, None
    if r.status_code == 200:
        try:
            uid = int(r.json().get("user_id"))
        except Exception:
            uid = None
        verdict, ttl = VALID, _VALID_TTL
    elif r.status_code in (401, 403):
        verdict, uid, ttl = INVALID, None, _INVALID_TTL
    else:
        logger.warning("edge whoami returned %s", r.status_code)
        _remember(key, UNKNOWN, None, _UNKNOWN_TTL)
        return UNKNOWN, None
    _remember(key, verdict, uid, ttl)
    return verdict, uid


def _remember(key: str, verdict: str, uid: int | None, ttl: float) -> None:
    _verdicts[key] = (verdict, uid, time.monotonic() + ttl)
    _verdicts.move_to_end(key)
    while len(_verdicts) > _CACHE_MAX:
        _verdicts.popitem(last=False)


# --------------------------------------------------------------------------- discovery
def protected_resource_url(resource_path: str = "") -> str:
    """RFC 9728 §3.1 path-insertion form (`…/oauth-protected-resource/mcp`): SDKs try it
    first, and one that does not fall back would otherwise never find the document."""
    suffix = resource_path.strip("/")
    return f"{_canonical_base()}{_PRM_PATH}" + (f"/{suffix}" if suffix else "")


def prm_document(resource_path: str) -> dict:
    base = _canonical_base()
    # `resource` must be the URI the client actually connects to: a client that checks
    # it against its own connection address aborts discovery on a mismatch. No
    # `scopes_supported`: omitted means unspecified, `[]` would assert "no scopes".
    return {"resource": f"{base}{resource_path}" if resource_path else base,
            "authorization_servers": [base],
            "bearer_methods_supported": ["header"]}


def unauthorized_headers(resource_path: str, presented: bool) -> list[tuple[bytes, bytes]]:
    """401 + RFC 9728 `WWW-Authenticate`. With a credential that failed, RFC 6750 §3.1
    `invalid_token`: a client that reads "no credential" retries the same dead token
    instead of refreshing or re-authorizing."""
    parts = [f'Bearer realm="{PRODUCT_NAME} MCP"']
    if presented:
        parts.append('error="invalid_token"')
        parts.append('error_description="The access token is expired or unknown"')
    parts.append(f'resource_metadata="{protected_resource_url(resource_path)}"')
    return [(b"www-authenticate", ", ".join(parts).encode()),
            (b"content-type", b"text/plain; charset=utf-8")]


# --------------------------------------------------------------------------- telemetry
def _field(value) -> str:
    """One type and a bounded length per field: a nested object or a megabyte-long
    method would break `summarize by` queries and the log line itself."""
    return "-" if value is None else str(value)[:200]


def protocol_version(headers: Headers, payload) -> str:
    """Header first (all post-initialize streamable traffic carries it there), then the
    per-request `_meta` of newer clients, then the legacy `initialize` params."""
    hdr = headers.get("mcp-protocol-version")
    if hdr:
        return _field(hdr)
    if not isinstance(payload, dict):
        return "-"
    key = "io.modelcontextprotocol/protocolVersion"
    params = payload.get("params")
    for meta in (params.get("_meta") if isinstance(params, dict) else None, payload.get("_meta")):
        if isinstance(meta, dict) and meta.get(key):
            return _field(meta[key])
    if isinstance(params, dict) and params.get("protocolVersion"):
        return _field(params["protocolVersion"])
    return "-"


def log_client(headers: Headers, payload, leg: str, auth: str, how: str,
               cred: str | None, uid: int | None) -> None:
    """One `mcp_client` line per request: who is calling, which leg, which protocol.

    🔴 `payload` may be a JSON-RPC batch (a list) or a scalar; reading `.get` on those
    would raise and drop the whole line, and old batch-sending clients are exactly the
    population this line exists to find. Never raises into the request path.
    """
    try:
        method = payload.get("method") if isinstance(payload, dict) else None
        line = json.dumps({
            "leg": leg, "ua": _field(headers.get("user-agent")),
            "proto": protocol_version(headers, payload), "method": _field(method),
            "auth": auth, "cred": how, "cred_fp": credential_fingerprint(cred), "uid": uid,
        })
        logger.info("mcp_client %s", line)
    except Exception:
        pass


_FUNNEL_STEPS = {"tools/list": "tools_listed", "tools/call": "first_call",
                 "prompts/list": "prompts_listed", "prompts/get": "prompt_used"}
_STEPS_WITH_NAME_DETAIL = {"first_call", "prompt_used"}
_FUNNEL_SEEN_MAX = 20000
_funnel_seen: "OrderedDict[tuple[str, str], None]" = OrderedDict()


_pending: set = set()


def _post_funnel(step: str, cred: str, detail: str, seen_key: tuple[str, str]) -> None:
    """Fire-and-forget `/mcp/funnel-event`. The backend resolves user and OAuth client
    from the credential, which this process has no table for.

    A failed send un-marks `seen_key` so the next request retries it: otherwise one
    backend blip would erase that credential's first call for the life of the process.
    Tasks are held in `_pending` because the loop keeps only weak references to them.
    """
    secret = os.environ.get("MCP_LOG_SECRET", "")

    async def _send() -> None:
        ok = False
        try:
            async with _http(timeout=5) as c:
                r = await c.post(f"{_backend_url()}/mcp/funnel-event",
                                 json={"step": step, "detail": detail or None, "api_key": cred},
                                 headers={"X-MCP-Log-Secret": secret} if secret else {})
            ok = r.status_code < 300
            if not ok:
                logger.warning("edge funnel post %s returned %s", step, r.status_code)
        except Exception as exc:
            logger.warning("edge funnel post %s failed: %s", step, type(exc).__name__)
        if not ok:
            _funnel_seen.pop(seen_key, None)

    try:
        task = asyncio.get_running_loop().create_task(_send())
    except RuntimeError:
        _funnel_seen.pop(seen_key, None)
        return
    _pending.add(task)
    task.add_done_callback(_pending.discard)


def record_funnel(payload, cred: str | None) -> None:
    """First `tools/list`, `tools/call`, `prompts/*` per credential (in-process dedup;
    a restart re-records once, acceptable for an onboarding funnel). A first
    `tools/call` with no `tools/list` before it back-fills `tools_listed`: SSE-leg
    clients list tools over the stream, which never reaches this POST path."""
    if not cred or not isinstance(payload, dict):
        return
    step = _FUNNEL_STEPS.get(payload.get("method"))
    if not step:
        return
    fp = credential_fingerprint(cred)
    steps = ["tools_listed", step] if step == "first_call" else [step]
    params = payload.get("params")
    for s in steps:
        if (fp, s) in _funnel_seen:
            continue
        _funnel_seen[(fp, s)] = None
        while len(_funnel_seen) > _FUNNEL_SEEN_MAX:
            _funnel_seen.popitem(last=False)
        detail = (str(params.get("name", ""))[:200]
                  if s in _STEPS_WITH_NAME_DETAIL and isinstance(params, dict) else "")
        _post_funnel(s, cred, detail, (fp, s))


def jsonrpc_succeeded(body: bytes, content_type: str) -> bool:
    """A 200 can still wrap a top-level JSON-RPC `error` that never reached a tool, so the
    status alone over-counts first calls. Tool-level failures arrive inside `result` and
    do count. Anything unparseable fails open (counts)."""
    try:
        text = body.decode("utf-8")
        if "text/event-stream" in (content_type or ""):
            for line in text.split("\n"):
                if line.startswith("data:") and line[5:].strip():
                    obj = json.loads(line[5:].strip())
                    if isinstance(obj, dict) and "error" in obj:
                        return False
            return True
        obj = json.loads(text)
        return not (isinstance(obj, dict) and "error" in obj)
    except Exception:
        return True


# --------------------------------------------------------------------------- ASGI
async def _read_body(receive: Receive, limit: int) -> bytes | None:
    """The whole request body, or None once it exceeds `limit` (we stop reading there)."""
    chunks, size = [], 0
    while True:
        msg = await receive()
        if msg["type"] == "http.disconnect":
            break
        chunk = msg.get("body", b"")
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
        if not msg.get("more_body"):
            break
    return b"".join(chunks)


_OAUTH_BODY_MAX = 64 * 1024


def _replay(body: bytes, receive: Receive) -> Receive:
    sent = False

    async def _receive() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()
    return _receive


async def _respond(send: Send, status: int, body: bytes,
                   headers: list[tuple[bytes, bytes]]) -> None:
    await send({"type": "http.response.start", "status": status,
                "headers": headers + [(b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


_CAPTURE_LIMIT = 65536


class EdgeMiddleware:
    def __init__(self, app: ASGIApp, max_body_bytes: int = 4 * 1024 * 1024) -> None:
        self.app = app
        # Same ceiling the MCP transport enforces downstream; we buffer the body to read
        # the JSON-RPC method, so without it this layer would be the unbounded one.
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]
        if path.startswith(_OAUTH_PREFIX) or path in (_AS_METADATA_PATH, _CLIENT_IP_PATH):
            return await self._to_backend(scope, receive, send)
        if path == _PRM_PATH or path.startswith(_PRM_PATH + "/"):
            rest = path[len(_PRM_PATH):].strip("/")
            resource = f"/{rest}" if rest else ""
            if resource and resource not in _RESOURCE_PATHS:
                return await _respond(send, 404, b'{"error":"unknown_resource"}',
                                      [(b"content-type", b"application/json")])
            return await _respond(send, 200, json.dumps(prm_document(resource)).encode(),
                                  [(b"content-type", b"application/json")])
        if path == _CHALLENGE_PATH:
            token = os.environ.get("MSDS_MCP_OPENAI_APPS_CHALLENGE_TOKEN", "")
            return await _respond(send, 200, token.encode(),
                                  [(b"content-type", b"text/plain; charset=utf-8")])
        # CORS preflight carries no credential by design; CORSMiddleware answers it.
        if scope["method"] != "OPTIONS" and (path in ("/mcp", "/sse") or path.startswith("/messages")):
            return await self._gated(scope, receive, send, path)
        return await self.app(scope, receive, send)

    async def _gated(self, scope: Scope, receive: Receive, send: Send, path: str) -> None:
        headers = Headers(scope=scope)
        method = scope["method"]
        # One line per long-lived SSE connection ("sse-open") vs per request elsewhere:
        # mixing them in one `leg` value would make "SSE usage vs streamable" counts lie.
        leg = ("streamable" if path == "/mcp" else
               "sse-open" if path == "/sse" else "sse-post")
        resource = "/mcp" if path == "/mcp" else "/sse"
        # 🔴 Authenticate from headers BEFORE reading any body: an unauthenticated caller
        # must not get to make us buffer anything.
        cred, how = presented_credential(headers)
        verdict, uid = (await check_credential(cred)) if cred else (INVALID, None)
        if verdict == INVALID:
            log_client(headers, None, leg, "denied",
                       "presented_invalid" if cred else "none", cred, None)
            return await _respond(send, 401, b"Unauthorized",
                                  unauthorized_headers(resource, presented=cred is not None))
        body = b""
        if method == "POST":
            body = await _read_body(receive, self.max_body_bytes)
            if body is None:
                return await _respond(send, 413, b"Payload Too Large",
                                      [(b"content-type", b"text/plain; charset=utf-8")])
        try:
            payload = json.loads(body) if body else None
        except Exception:
            payload = None
        log_client(headers, payload, leg, "ok", how, cred, uid)

        status = 0
        ctype = ""
        captured = bytearray()

        async def _send(msg: Message) -> None:
            nonlocal status, ctype
            if msg["type"] == "http.response.start":
                status = msg["status"]
                ctype = Headers(raw=msg.get("headers", [])).get("content-type", "")
            elif msg["type"] == "http.response.body" and len(captured) < _CAPTURE_LIMIT:
                captured.extend(msg.get("body", b"")[:_CAPTURE_LIMIT - len(captured)])
            await send(msg)

        await self.app(scope, _replay(body, receive) if method == "POST" else receive, _send)
        # Record only after a forward that reached tool execution: a 5xx during a deploy,
        # or a 200 wrapping a JSON-RPC error, is not a first call.
        if method == "POST" and status and status < 300 and jsonrpc_succeeded(bytes(captured), ctype):
            record_funnel(payload, cred)

    async def _to_backend(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass an OAuth request through unchanged. The browser and the MCP client see
        `mcp.lagentbot.com` as the authorization server, as published in the store
        listings; the backend serves it."""
        headers = Headers(scope=scope)
        body = await _read_body(receive, _OAUTH_BODY_MAX)
        if body is None:
            return await _respond(send, 413, b"Payload Too Large",
                                  [(b"content-type", b"text/plain; charset=utf-8")])
        fwd = [(k, v) for k, v in headers.items() if k.lower() in _FORWARD_HEADERS]
        proxy_secret = os.environ.get("MSDS_INTERNAL_PROXY_SECRET", "")
        if proxy_secret:
            fwd.append(("x-internal-proxy", proxy_secret))
        qs = scope.get("query_string", b"").decode()
        url = f"{_backend_url()}{scope['path']}" + (f"?{qs}" if qs else "")
        try:
            async with _http(timeout=30) as c:
                r = await c.request(scope["method"], url, content=body, headers=fwd)
        except httpx.HTTPError as exc:
            logger.warning("edge oauth passthrough failed: %s", type(exc).__name__)
            return await _respond(send, 502, b"Bad Gateway",
                                  [(b"content-type", b"text/plain; charset=utf-8")])
        # httpx has already decoded the body, so its encoding/length headers no longer
        # describe what we send.
        skip = _HOP_BY_HOP | {"content-length", "content-encoding"}
        out = [(k.encode(), v.encode()) for k, v in r.headers.multi_items() if k.lower() not in skip]
        await _respond(send, r.status_code, r.content, out)


def configure_logging() -> None:
    """Route this module's INFO lines to stdout (Container Apps collects stdout). Under a
    bare uvicorn the root logger has no handler, so INFO would otherwise vanish."""
    if any(getattr(h, "_edge_stdout", False) for h in logger.handlers):
        return
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    h._edge_stdout = True
    logger.addHandler(h)
    logger.setLevel(logging.INFO)
