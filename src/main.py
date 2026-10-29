import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from src.api.routes import (
    auth,
    credentials,
    environments,
    health,
    investigations,
    organizations,
    overview,
    reports,
    transfers,
)
from src.api.security import (
    ClientRateLimitMiddleware,
    OriginCheckMiddleware,
    RequestLimitMiddleware,
    quota_exceeded_handler,
    rate_limited_handler,
    validation_error_handler,
)
from src.config import get_settings
from src.db.privileges import check_runtime_role
from src.db.session import SessionLocal, engine
from src.deployment import check_deployment
from src.investigators import runtime
from src.mcp_server import McpSettings, build_mcp_app
from src.mcp_server.listener import serve_mcp
from src.services import limits


def create_app() -> FastAPI:
    settings = get_settings()
    mcp_settings = McpSettings()
    _, mcp_app = build_mcp_app(mcp_settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        limits.limit_settings()  # invalid limits refuse to start rather than fail requests
        # Production refuses missing security settings (D041) and a database role beyond
        # least privilege (D040).
        check_deployment(settings, mcp_settings)
        await check_runtime_role(engine, settings)
        pruning = asyncio.create_task(limits.prune_periodically(SessionLocal))
        # MCP has its own loopback-only listener (D041); exit order stops the runtimes
        # (its only clients) before it.
        async with serve_mcp(mcp_app, mcp_settings), runtime.lifespan():
            yield
        pruning.cancel()
        with suppress(asyncio.CancelledError):
            await pruning
        await engine.dispose()

    # A hosted server publishes no API explorer: it is not a product page, and its CDN
    # assets would not load under the proxy's Content-Security-Policy anyway.
    hosted = settings.app_env == "production"
    app = FastAPI(
        title="Inquest",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None if hosted else "/docs",
        redoc_url=None if hosted else "/redoc",
        openapi_url=None if hosted else "/openapi.json",
    )
    # Outermost last: the per-address limit runs first, then the origin check, then the
    # body limits, all before routing.
    app.add_middleware(RequestLimitMiddleware)
    app.add_middleware(OriginCheckMiddleware)
    app.add_middleware(ClientRateLimitMiddleware)
    app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(limits.RateLimited, rate_limited_handler)  # type: ignore[arg-type]
    app.add_exception_handler(limits.QuotaExceeded, quota_exceeded_handler)  # type: ignore[arg-type]
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(organizations.router)
    # Every route below authorizes per organization from the session (D033).
    app.include_router(environments.router)
    app.include_router(credentials.router)
    app.include_router(transfers.router)
    app.include_router(investigations.router)
    app.include_router(overview.router)
    app.include_router(reports.router)
    return app


app = create_app()
