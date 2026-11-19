"""ASGI middleware: copy the inbound caller credential and User-Agent into request contextvars."""
from starlette.types import ASGIApp, Receive, Scope, Send

from request_identity import set_caller_credential, set_caller_user_agent


class IdentityMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            auth = headers.get("authorization")
            api_key = headers.get("x-api-key")
            if api_key:
                set_caller_credential(api_key)
            elif auth and auth.startswith("Bearer "):
                set_caller_credential(auth)
            else:
                set_caller_credential(None)
            set_caller_user_agent(headers.get("user-agent"))
        await self.app(scope, receive, send)
