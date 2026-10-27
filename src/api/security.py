"""Request-level guards that do not depend on who is signed in."""

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from src.auth.settings import AuthNotConfigured, load_auth_settings

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
DEV_ORIGINS = frozenset({"http://localhost:5173", "http://127.0.0.1:5173"})


def allowed_origins() -> frozenset[str]:
    try:
        return frozenset({load_auth_settings().origin})
    except AuthNotConfigured:
        # Unconfigured means local development: the Vite dev server's origin only.
        return DEV_ORIGINS


class OriginCheckMiddleware:
    """Rejects state-changing requests that a browser sent from another site. Browsers
    send Origin on such requests; requests without it (curl, investigator runtimes) pass
    and still need whatever authentication the route requires. Plain ASGI, so streamed
    responses (SSE) pass through untouched."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] not in SAFE_METHODS:
            headers = Headers(scope=scope)
            origin = headers.get("origin")
            cross_site = headers.get("sec-fetch-site") == "cross-site"
            if (origin is not None and origin not in allowed_origins()) or cross_site:
                response = JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
