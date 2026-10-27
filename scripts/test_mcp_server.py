"""Development-only harness: drive the running MCP server like an investigator would.

Mints an assignment-bound token from the assignment row (the orchestrator's job later),
keeps it in memory, and talks to /mcp over real Streamable HTTP with the MCP SDK client.
The token is never printed. Create the assignment first with
`scripts.test_tool_executor setup`; inspect rows afterwards with its `inspect` command.

    uv run python -m scripts.test_mcp_server tools --assignment-id A
    uv run python -m scripts.test_mcp_server call --assignment-id A \\
        --tool signoz_trace_stats --args '{"group_by": "operation"}' --last 60 --key manual-1
"""

import argparse
import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from scripts.test_tool_executor import emit, fail, parse_arguments
from src.config import get_settings
from src.db.session import SessionLocal, engine
from src.mcp_server import McpSettings, mint_assignment_token
from src.mcp_server.auth import TokenConfigError
from src.mcp_server.server import IDEMPOTENCY_META_KEY
from src.models import Investigation, InvestigationAssignment

# Above the executor's worst case: 20 s SigNoz deadline plus two 10 s database phases.
READ_TIMEOUT_SECONDS = 60


async def _token(assignment_id: uuid.UUID) -> str:
    async with SessionLocal() as session:
        assignment = await session.get(InvestigationAssignment, assignment_id)
        investigation = assignment and await session.get(Investigation, assignment.investigation_id)
    if assignment is None or investigation is None:
        fail(f"assignment {assignment_id} not found")
    try:
        return mint_assignment_token(
            assignment, McpSettings(), organization_id=investigation.organization_id
        )
    except TokenConfigError as exc:
        fail(str(exc))
    raise AssertionError("unreachable")


def _client(url: str, token: str) -> tuple[httpx2.AsyncClient, Client]:
    http = httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx2.Timeout(READ_TIMEOUT_SECONDS),
    )
    return http, Client(streamable_http_client(url, http_client=http))


async def tools(args: argparse.Namespace) -> None:
    http, client = _client(args.url, await _token(args.assignment_id))
    async with http, client:
        listed = (await client.list_tools()).tools
    emit(
        [
            {
                "name": t.name,
                "read_only": t.annotations.read_only_hint if t.annotations else None,
                "required": t.input_schema.get("required", []),
                "description": t.description,
            }
            for t in listed
        ]
    )


async def call(args: argparse.Namespace) -> None:
    arguments = parse_arguments(args.args)
    if args.last is not None:
        if not isinstance(arguments, dict):
            fail("--last needs --args to be a JSON object")
        end = datetime.now(UTC)
        arguments.setdefault(
            "time_range",
            {"start": (end - timedelta(minutes=args.last)).isoformat(), "end": end.isoformat()},
        )
    http, client = _client(args.url, await _token(args.assignment_id))
    async with http, client:
        result = await client.call_tool(
            args.tool,
            arguments,
            meta={IDEMPOTENCY_META_KEY: args.key} if args.key else None,
        )
    body = dict(result.structured_content or {})
    if args.no_data:
        body.pop("data", None)
    emit({"is_error": result.is_error, "result": body})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default="http://localhost:8000/mcp/")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("tools", help="tools/list as the assignment's investigator")
    p.add_argument("--assignment-id", type=uuid.UUID, required=True)

    p = sub.add_parser("call", help="tools/call as the assignment's investigator")
    p.add_argument("--assignment-id", type=uuid.UUID, required=True)
    p.add_argument("--tool", required=True, help="MCP name, e.g. signoz_trace_stats")
    p.add_argument("--args", default="{}", help="JSON object, or @path/to/file.json")
    p.add_argument(
        "--last", type=int, metavar="MINUTES", help="add time_range ending now if absent"
    )
    p.add_argument("--key", help=f"sent as _meta {IDEMPOTENCY_META_KEY!r}")
    p.add_argument("--no-data", action="store_true", help="omit the data payload")
    return parser


async def main() -> None:
    args = build_parser().parse_args()
    if get_settings().app_env == "production":
        fail("development-only script; refusing to run with APP_ENV=production")
    logging.basicConfig(level=logging.WARNING)
    try:
        await {"tools": tools, "call": call}[args.command](args)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
