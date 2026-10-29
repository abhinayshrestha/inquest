"""Runs inside the application container of the deployment simulation (run.sh): what the
backend itself can reach. The investigator runtimes' path to MCP still works (loopback,
signed token), nothing else in the container can bypass it, and the process talks to
PostgreSQL as the restricted runtime role over TLS. Exit status 1 on any failed check."""

import asyncio
import json
import socket
import sys
import uuid

import httpx
from sqlalchemy import text

from src.db.privileges import runtime_problems
from src.db.session import engine
from src.mcp_server import McpSettings, mint_assignment_token
from src.models import InvestigationAssignment

results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    results.append((ok, name))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if not ok and detail else ""))


def rpc(method: str, params: dict) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}


async def mcp() -> None:
    settings = McpSettings()
    # A well-formed token for an assignment that is not running: authentication passes,
    # the executor refuses the call (it re-checks the assignment under a row lock).
    assignment = InvestigationAssignment(
        id=uuid.uuid4(), investigation_id=uuid.uuid4(), environment_id=uuid.uuid4()
    )
    token = mint_assignment_token(assignment, settings, organization_id=uuid.uuid4())
    headers = {"accept": "application/json, text/event-stream"}
    bearer = {**headers, "authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=10) as client:
        anonymous = await client.post(settings.url, json=rpc("tools/list", {}), headers=headers)
        listed = await client.post(settings.url, json=rpc("tools/list", {}), headers=bearer)
        call = await client.post(
            settings.url,
            json=rpc("tools/call", {"name": "signoz_trace_stats", "arguments": {}}),
            headers=bearer,
        )
    check("MCP listener refuses requests without a token", anonymous.status_code == 401)
    tools = [t["name"] for t in listed.json().get("result", {}).get("tools", [])]
    check("authenticated investigators list tools over loopback", bool(tools))
    result = call.json().get("result", {})
    check(
        "authenticated tool calls reach the executor (which refuses a closed assignment)",
        call.status_code == 200 and result.get("isError") is True,
        result,
    )
    own = socket.gethostbyname(socket.gethostname())
    try:
        socket.create_connection((own, settings.port), timeout=3).close()
        exposed = True
    except OSError:
        exposed = False
    check("MCP listens on loopback only, not the container's network address", not exposed, own)


async def database() -> None:
    async with engine.connect() as conn:
        user = (await conn.execute(text("SELECT current_user"))).scalar()
        tls = (
            await conn.execute(text("SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()"))
        ).scalar()
        problems = await conn.run_sync(runtime_problems)
    await engine.dispose()
    check("the application connects as the restricted runtime role", user == "inquest_app", user)
    check("the database connection uses TLS", tls is True)
    check("the runtime role has no privilege beyond least privilege", problems == [], problems)


async def main() -> int:
    await mcp()
    await database()
    failed = [n for ok, n in results if not ok]
    print(json.dumps({"client": "internal", "checks": len(results), "failed": failed}))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
