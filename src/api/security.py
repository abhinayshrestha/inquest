"""Request-level guards that do not depend on who is signed in."""

import ipaddress

from fastapi.exceptions import RequestValidationError
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.auth.settings import AuthNotConfigured, load_auth_settings
from src.services import limits

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
DEV_ORIGINS = frozenset({"http://localhost:5173", "http://127.0.0.1:5173"})
OPEN, CLOSE, QUOTE, BACKSLASH = frozenset(b"[{"), frozenset(b"]}"), ord('"'), ord("\\")


def allowed_origins() -> frozenset[str]:
    try:
        return frozenset({load_auth_settings().origin})
    except AuthNotConfigured:
        # Unconfigured means local development: the Vite dev server's origin only.
        return DEV_ORIGINS


def client_address(scope: Scope) -> str:
    """Who to rate-limit: the peer address the ASGI server reports. Never read from
    X-Forwarded-For, Forwarded or X-Real-IP here: any client can send those. Behind the
    reverse proxy, uvicorn replaces the peer with the forwarded address only when the
    peer is in INQUEST_TRUSTED_PROXIES (src.serve), and the proxy overwrites the header
    rather than appending to it (D041). IPv6 counts per /64, which one host usually holds."""
    client = scope.get("client")
    host = client[0] if client else "unknown"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host[:100]
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


class ClientRateLimitMiddleware:
    """The first line against request floods: requests per client address per process,
    counted in memory before any body, session or database work, so refused traffic
    costs no connection. Every path counts: MCP is not served on this listener (D041)."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            retry_after = limits.allow_request(client_address(scope))
            if retry_after is not None:
                response = _rate_limited(retry_after, limits.RATE_LIMITED)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


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


def json_too_deep(body: bytes, limit: int) -> bool:
    """Nesting beyond `limit`, found by a scan that never recurses: Python's JSON parser
    recurses per level and fails (500) long before the body-size limit is reached."""
    if body.count(b"[") + body.count(b"{") <= limit:
        return False
    depth, in_string, escaped = 0, False, False
    for byte in body:
        if in_string:
            if escaped:
                escaped = False
            elif byte == BACKSLASH:
                escaped = True
            elif byte == QUOTE:
                in_string = False
        elif byte == QUOTE:
            in_string = True
        elif byte in OPEN:
            depth += 1
            if depth > limit:
                return True
        elif byte in CLOSE:
            depth -= 1
    return False


class RequestLimitMiddleware:
    """Bounds request bodies before routing, parsing or database work: 413 beyond
    INQUEST_MAX_REQUEST_BYTES (declared or actually sent, chunked included), 422 for
    JSON nested deeper than INQUEST_MAX_JSON_DEPTH. Bodies of unsafe methods are read
    here, at most the limit, and handed on unchanged; GET streams pass untouched."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        settings = limits.limit_settings()
        declared = Headers(scope=scope).get("content-length", "")
        if declared.isdigit() and int(declared) > settings.max_request_bytes:
            await _too_large()(scope, receive, send)
            return
        if scope["method"] in SAFE_METHODS:
            await self.app(scope, receive, send)
            return
        body = bytearray()
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body += message.get("body", b"")
            if len(body) > settings.max_request_bytes:
                await _too_large()(scope, receive, send)
                return
            more = message.get("more_body", False)
        if json_too_deep(bytes(body), settings.max_json_depth):
            msg = f"JSON nesting exceeds {settings.max_json_depth} levels"
            detail = [{"type": "json_too_deep", "loc": ["body"], "msg": msg}]
            await JSONResponse({"detail": detail}, status_code=422)(scope, receive, send)
            return
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if replayed:
                return await receive()
            replayed = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay, send)


def _too_large() -> JSONResponse:
    detail = {
        "code": "request_too_large",
        "message": "the request body exceeds this server's limit",
    }
    return JSONResponse({"detail": detail}, status_code=413)


def _rate_limited(retry_after: int, message: str) -> JSONResponse:
    return JSONResponse(
        {"detail": {"code": "rate_limited", "message": message}},
        status_code=429,
        headers={"Retry-After": str(retry_after)},
    )


async def rate_limited_handler(_request: Request, exc: limits.RateLimited) -> JSONResponse:
    return _rate_limited(exc.retry_after, exc.message)


async def quota_exceeded_handler(_request: Request, exc: limits.QuotaExceeded) -> JSONResponse:
    """429 with Retry-After when waiting helps (quotas per day, busy server); otherwise
    409: only fewer resources or an operator's higher limit do."""
    detail = {"code": "quota_exceeded", "quota": exc.name, "message": exc.message}
    if exc.retry_after is None:
        return JSONResponse({"detail": detail}, status_code=409)
    return JSONResponse(
        {"detail": detail}, status_code=429, headers={"Retry-After": str(exc.retry_after)}
    )


async def validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    """422 without the submitted values. FastAPI's default echoes each offending value
    ("input") and validator context; for a credential body that is the secret itself. The
    location, type and message identify the problem without it."""
    detail = []
    for error in exc.errors():
        loc = list(error.get("loc", ()))
        if error.get("type") == "extra_forbidden" and loc:
            # The unexpected key itself is caller-chosen text; it could be a pasted secret.
            loc[-1] = "(unexpected field)"
        detail.append({"type": error.get("type"), "loc": loc, "msg": error.get("msg")})
    return JSONResponse(status_code=422, content={"detail": detail})
