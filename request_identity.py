"""Request-scoped caller credential, forwarded to the backend on each tool call.

The gateway (msds-chain-mcp-gateway) injects the authenticated caller's credential
as an inbound header; the identity middleware copies it here per request. Tools read
it via caller_headers() instead of any global key.
"""
from contextvars import ContextVar

_caller_credential: ContextVar[str | None] = ContextVar("caller_credential", default=None)
# One id per tool call, minted by the `_reported` wrapper in server.py. It goes to the backend
# as `X-MCP-Call-Id` on every request the tool makes and into the call-log POST, so the
# backend's audit rows (precursor_hits) can be joined to the reply we logged for that call.
_mcp_call_id: ContextVar[str | None] = ContextVar("mcp_call_id", default=None)


def set_caller_credential(value: str | None) -> None:
    _caller_credential.set(value)


def get_caller_credential() -> str | None:
    return _caller_credential.get()


def set_mcp_call_id(value: str | None):
    """Set the current tool call's id; returns the token for `reset_mcp_call_id`."""
    return _mcp_call_id.set(value)


def reset_mcp_call_id(token) -> None:
    _mcp_call_id.reset(token)


def get_mcp_call_id() -> str | None:
    return _mcp_call_id.get()


def caller_headers() -> dict[str, str]:
    """Build backend request headers carrying the caller's identity."""
    h = {"Content-Type": "application/json"}
    call_id = _mcp_call_id.get()
    if call_id:
        h["X-MCP-Call-Id"] = call_id
    cred = _caller_credential.get()
    if not cred:
        return h
    if cred.startswith("Bearer "):
        h["Authorization"] = cred
    else:
        h["X-API-Key"] = cred
    return h
