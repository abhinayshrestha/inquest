from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.api.routes import environments, health, investigations
from src.db.session import engine
from src.mcp_server import build_mcp_app


def create_app() -> FastAPI:
    mcp_server, mcp_app = build_mcp_app()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # A mounted app's own lifespan never runs, so the MCP session manager starts here.
        async with mcp_server.session_manager.run():
            yield
        await engine.dispose()

    app = FastAPI(title="Inquest", version="0.1.0", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(environments.router)
    app.include_router(investigations.router)
    app.mount("/mcp", mcp_app)
    return app


app = create_app()
