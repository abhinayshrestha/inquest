from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.api.routes import environments, health, investigations
from src.db.session import engine


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(title="Inquest", version="0.1.0", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(environments.router)
    app.include_router(investigations.router)
    return app


app = create_app()
