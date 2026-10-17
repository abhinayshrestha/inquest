"""MCP tests drive the real Streamable HTTP app in-process (httpx2 ASGI transport, which
the MCP SDK client uses) against the test database. SigNoz is mocked by swapping the
executor's HTTP client; everything between the MCP request and that client is real."""

import asyncio
import functools
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette

from src.mcp_server import McpSettings, build_mcp_app, mint_assignment_token
from src.mcp_server import server as mcp_server_module
from src.models import InvestigationAssignment
from src.tools import execute_tool
from tests.tools.conftest import SETTINGS, World, make_world, replies, world  # noqa: F401

SECRET = "test-secret-" + "x" * 40
MCP_SETTINGS = McpSettings(token_secret=SECRET, allowed_hosts=["testserver"])
BASE_URL = "http://testserver"


def token_for(target: World, **kwargs: Any) -> str:
    assignment = InvestigationAssignment(
        id=target.assignment_id,
        investigation_id=target.investigation_id,
        environment_id=target.environment_id,
    )
    return mint_assignment_token(assignment, MCP_SETTINGS, **kwargs)


@pytest.fixture
async def mcp_app() -> AsyncIterator[Starlette]:
    server, app = build_mcp_app(MCP_SETTINGS)
    # run() opens an anyio task group, which must be exited by the task that entered it;
    # pytest-asyncio tears fixtures down in a different task, so it gets one of its own.
    ready, stop = asyncio.Event(), asyncio.Event()

    async def serve() -> None:
        async with server.session_manager.run():
            ready.set()
            await stop.wait()

    task = asyncio.create_task(serve())
    await ready.wait()
    yield app
    stop.set()
    await task


@pytest.fixture
def signoz(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[httpx.Request]]:
    """signoz(*bodies) answers the executor's SigNoz requests in order (see replies())
    and returns the list the requests are recorded in."""

    def use(*bodies: Any, handler: Any = None) -> list[httpx.Request]:
        seen: list[httpx.Request] = []
        if handler is None:
            handler, seen = replies(*bodies)
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        monkeypatch.setattr(
            mcp_server_module,
            "execute_tool",
            functools.partial(execute_tool, signoz_settings=SETTINGS, http_client=client),
        )
        return seen

    return use


def http_client(app: Starlette, token: str | None = None, **headers: str) -> httpx2.AsyncClient:
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url=BASE_URL, headers=headers
    )


@asynccontextmanager
async def connect(
    app: Starlette, token: str | None, *, mode: str = "auto", **headers: str
) -> AsyncIterator[Client]:
    async with (
        http_client(app, token, **headers) as hc,
        Client(streamable_http_client(f"{BASE_URL}/", http_client=hc), mode=mode) as client,  # type: ignore[arg-type]
    ):
        yield client
