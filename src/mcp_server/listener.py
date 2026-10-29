"""The MCP server's private listener (D041, audit M1).

MCP used to be mounted on the public API listener, so anything that reached the API could
reach token verification, and a proxy forwarding `Host: 127.0.0.1` would have passed the
Host allowlist too. It now has its own uvicorn server bound to loopback only, started by
the API process's lifespan: investigators run in that process (D010) and are its only
clients. The public listener has no /mcp route at all.
"""

import asyncio
import contextlib
import logging
import socket
from collections.abc import AsyncIterator, Generator

import uvicorn
from starlette.types import ASGIApp

from src.mcp_server.auth import LISTEN_HOST, McpSettings

logger = logging.getLogger(__name__)

# Investigators per process are bounded by D036 capacity; this only stops a runaway client.
MAX_CONNECTIONS = 64


class _EmbeddedServer(uvicorn.Server):
    """A uvicorn server inside another one: the host server owns SIGINT/SIGTERM, and its
    lifespan stops this one after the investigators that use it."""

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None]:
        yield


def bind(port: int) -> socket.socket:
    """Bound here rather than by uvicorn so a taken port raises in the lifespan (refusing
    to start) instead of uvicorn calling sys.exit inside a task."""
    sock = socket.create_server((LISTEN_HOST, port))
    sock.set_inheritable(True)
    return sock


@contextlib.asynccontextmanager
async def serve_mcp(app: ASGIApp, settings: McpSettings) -> AsyncIterator[None]:
    sock = bind(settings.port)
    config = uvicorn.Config(
        app,
        # The served app's own lifespan runs the MCP session manager.
        lifespan="on",
        log_config=None,
        # Tool calls are audited in tool_calls; tokens never appear in URLs.
        access_log=False,
        server_header=False,
        proxy_headers=False,
        limit_concurrency=MAX_CONNECTIONS,
    )
    server = _EmbeddedServer(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while not server.started:
            if task.done():
                task.result()
                raise RuntimeError("the MCP listener stopped during startup")
            await asyncio.sleep(0.01)
        logger.info("MCP listener on %s:%d (loopback only)", LISTEN_HOST, settings.port)
        yield
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError):
            await task
        sock.close()
