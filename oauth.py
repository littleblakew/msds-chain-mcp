"""
OAuth 2.1 Provider for MSDS Chain MCP Server (Remote Mode)

Implements the endpoints required by Claude Marketplace for remote MCP:
- /.well-known/oauth-authorization-server  (metadata discovery)
- /oauth/register                          (Dynamic Client Registration)
- /oauth/authorize                         (authorization endpoint)
- /oauth/token                             (token exchange with PKCE)

This is a minimal implementation that wraps MSDS Chain's existing API key
system. In production, consider using Auth0 or Entra ID as the provider.

Usage:
    from oauth import oauth_app
    # Mount on your Starlette/FastAPI app alongside the MCP routes
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, HTMLResponse, RedirectResponse
from starlette.routing import Route

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ISSUER_URL = os.environ.get("MSDS_OAUTH_ISSUER", "https://mcp.lagentbot.com")
TOKEN_TTL = 3600  # 1 hour
REFRESH_TTL = 86400 * 30  # 30 days
JWT_SECRET = os.environ.get("MSDS_OAUTH_SECRET", secrets.token_hex(32))


# ---------------------------------------------------------------------------
# In-memory stores (replace with DB/Redis in production)
# ---------------------------------------------------------------------------
@dataclass
class RegisteredClient:
    client_id: str
    client_name: str
    redirect_uris: list[str]
    created_at: float = field(default_factory=time.time)


@dataclass
class AuthCode:
    code: str
    client_id: str
    redirect_uri: str
    code_challenge: str
    code_challenge_method: str
    api_key: str  # The MSDS Chain API key the user authorized with
    expires_at: float = field(default_factory=lambda: time.time() + 300)


# Stores (in-memory for now — replace with Redis/DB for production)
_clients: dict[str, RegisteredClient] = {}
_auth_codes: dict[str, AuthCode] = {}
_tokens: dict[str, dict] = {}  # access_token -> {api_key, client_id, expires_at}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _generate_token() -> str:
    return secrets.token_urlsafe(32)


def _verify_pkce(code_verifier: str, code_challenge: str, method: str) -> bool:
    if method == "S256":
        digest = hashlib.sha256(code_verifier.encode()).digest()
        import base64
        computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        return hmac.compare_digest(computed, code_challenge)
    elif method == "plain":
        return hmac.compare_digest(code_verifier, code_challenge)
    return False


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
async def metadata(request: Request) -> JSONResponse:
    """RFC 8414 — OAuth Authorization Server Metadata."""
    base = ISSUER_URL.rstrip("/")
    return JSONResponse({
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256", "plain"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["chemical-safety"],
    })


async def register(request: Request) -> JSONResponse:
    """RFC 7591 — Dynamic Client Registration."""
    body = await request.json()
    client_name = body.get("client_name", "Unknown Client")
    redirect_uris = body.get("redirect_uris", [])

    if not redirect_uris:
        return JSONResponse({"error": "invalid_client_metadata",
                             "error_description": "redirect_uris required"}, status_code=400)

    client_id = f"mcp_{secrets.token_hex(16)}"
    client = RegisteredClient(
        client_id=client_id,
        client_name=client_name,
        redirect_uris=redirect_uris,
    )
    _clients[client_id] = client

    return JSONResponse({
        "client_id": client_id,
        "client_name": client_name,
        "redirect_uris": redirect_uris,
        "token_endpoint_auth_method": "none",
    }, status_code=201)


async def authorize(request: Request) -> HTMLResponse:
    """Authorization endpoint — renders a simple login page.

    In production, redirect to MSDS Chain's login page or use SSO.
    For now, accept an API key directly (suitable for CLI flows).
    """
    client_id = request.query_params.get("client_id", "")
    redirect_uri = request.query_params.get("redirect_uri", "")
    state = request.query_params.get("state", "")
    code_challenge = request.query_params.get("code_challenge", "")
    code_challenge_method = request.query_params.get("code_challenge_method", "S256")

    # Validate client
    if client_id not in _clients:
        return HTMLResponse("<h1>Error: Unknown client</h1>", status_code=400)
    client = _clients[client_id]
    if redirect_uri not in client.redirect_uris:
        return HTMLResponse("<h1>Error: Invalid redirect_uri</h1>", status_code=400)

    # If this is a form submission (POST), process it
    if request.method == "POST":
        form = await request.form()
        api_key = form.get("api_key", "")
        if not api_key:
            return HTMLResponse("<h1>Error: API key required</h1>", status_code=400)

        # Generate authorization code
        code = secrets.token_urlsafe(32)
        _auth_codes[code] = AuthCode(
            code=code,
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            api_key=str(api_key),
        )

        # Redirect back to client with code
        params = {"code": code}
        if state:
            params["state"] = state
        return RedirectResponse(f"{redirect_uri}?{urlencode(params)}", status_code=302)

    # GET — render authorization form
    html = f"""<!DOCTYPE html>
<html>
<head><title>MSDS Chain — Authorize</title>
<style>
body {{ font-family: Inter, sans-serif; max-width: 480px; margin: 60px auto; padding: 20px; background: #0F1116; color: #e0e0e0; }}
h1 {{ color: #7928CA; font-size: 1.4rem; }}
input {{ width: 100%; padding: 12px; margin: 8px 0; border: 1px solid #333; border-radius: 8px; background: #1a1d24; color: #fff; font-size: 14px; }}
button {{ width: 100%; padding: 12px; margin-top: 16px; border: none; border-radius: 8px; background: #7928CA; color: white; font-size: 16px; cursor: pointer; }}
button:hover {{ background: #6620b0; }}
.info {{ color: #888; font-size: 12px; margin-top: 8px; }}
</style>
</head>
<body>
<h1>Authorize MSDS Chain</h1>
<p><strong>{client.client_name}</strong> wants access to your chemical safety tools.</p>
<form method="POST">
  <input type="hidden" name="client_id" value="{client_id}">
  <label>MSDS Chain API Key</label>
  <input type="password" name="api_key" placeholder="sk-msds-..." required>
  <p class="info">Get your API key at <a href="https://msdschain.lagentbot.com" style="color:#7928CA">msdschain.lagentbot.com</a></p>
  <button type="submit">Authorize</button>
</form>
</body>
</html>"""
    return HTMLResponse(html)


async def token(request: Request) -> JSONResponse:
    """Token endpoint — exchange auth code for access token (PKCE verified)."""
    body = await request.form()
    grant_type = body.get("grant_type", "")

    if grant_type == "authorization_code":
        code = body.get("code", "")
        code_verifier = body.get("code_verifier", "")
        client_id = body.get("client_id", "")

        if code not in _auth_codes:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

        auth = _auth_codes.pop(code)

        if auth.expires_at < time.time():
            return JSONResponse({"error": "invalid_grant",
                                 "error_description": "code expired"}, status_code=400)
        if auth.client_id != client_id:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if not _verify_pkce(code_verifier, auth.code_challenge, auth.code_challenge_method):
            return JSONResponse({"error": "invalid_grant",
                                 "error_description": "PKCE verification failed"}, status_code=400)

        # Issue tokens
        access_token = _generate_token()
        refresh_token = _generate_token()
        expires_at = time.time() + TOKEN_TTL

        _tokens[access_token] = {
            "api_key": auth.api_key,
            "client_id": client_id,
            "expires_at": expires_at,
            "refresh_token": refresh_token,
        }
        _tokens[f"refresh:{refresh_token}"] = {
            "api_key": auth.api_key,
            "client_id": client_id,
            "expires_at": time.time() + REFRESH_TTL,
        }

        return JSONResponse({
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": TOKEN_TTL,
            "refresh_token": refresh_token,
            "scope": "chemical-safety",
        })

    elif grant_type == "refresh_token":
        refresh = body.get("refresh_token", "")
        key = f"refresh:{refresh}"
        if key not in _tokens:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        stored = _tokens.pop(key)
        if stored["expires_at"] < time.time():
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

        # Issue new tokens
        access_token = _generate_token()
        new_refresh = _generate_token()
        expires_at = time.time() + TOKEN_TTL

        _tokens[access_token] = {
            "api_key": stored["api_key"],
            "client_id": stored["client_id"],
            "expires_at": expires_at,
            "refresh_token": new_refresh,
        }
        _tokens[f"refresh:{new_refresh}"] = {
            "api_key": stored["api_key"],
            "client_id": stored["client_id"],
            "expires_at": time.time() + REFRESH_TTL,
        }

        return JSONResponse({
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": TOKEN_TTL,
            "refresh_token": new_refresh,
            "scope": "chemical-safety",
        })

    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


# ---------------------------------------------------------------------------
# Token validation helper (used by server_remote.py middleware)
# ---------------------------------------------------------------------------
def validate_bearer_token(authorization: str) -> str | None:
    """Extract and validate Bearer token, return the associated API key or None."""
    if not authorization.startswith("Bearer "):
        return None
    token_val = authorization[7:]
    stored = _tokens.get(token_val)
    if not stored:
        return None
    if stored["expires_at"] < time.time():
        del _tokens[token_val]
        return None
    return stored["api_key"]


# ---------------------------------------------------------------------------
# Starlette app (mount alongside MCP routes)
# ---------------------------------------------------------------------------
oauth_routes = [
    Route("/.well-known/oauth-authorization-server", metadata, methods=["GET"]),
    Route("/oauth/register", register, methods=["POST"]),
    Route("/oauth/authorize", authorize, methods=["GET", "POST"]),
    Route("/oauth/token", token, methods=["POST"]),
]

oauth_app = Starlette(routes=oauth_routes)
