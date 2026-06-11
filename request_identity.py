"""Request-scoped caller credential, forwarded to the backend on each tool call.

The gateway (msds-chain-mcp-gateway) injects the authenticated caller's credential
as an inbound header; the identity middleware copies it here per request. Tools read
it via caller_headers() instead of any global key.
"""
from contextvars import ContextVar

_caller_credential: ContextVar[str | None] = ContextVar("caller_credential", default=None)


def set_caller_credential(value: str | None) -> None:
    _caller_credential.set(value)


def get_caller_credential() -> str | None:
    return _caller_credential.get()


def caller_headers() -> dict[str, str]:
    """Build backend request headers carrying the caller's identity."""
    h = {"Content-Type": "application/json"}
    cred = _caller_credential.get()
    if not cred:
        return h
    if cred.startswith("Bearer "):
        h["Authorization"] = cred
    else:
        h["X-API-Key"] = cred
    return h
