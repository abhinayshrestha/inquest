"""Development-only harness: drive the Tool Executor against the real SigNoz and PostgreSQL.

Everything goes through execute_tool(), so authorization, scoping, budgets and persistence
apply exactly as they will for investigators. Setup uses the existing services. Nothing
is deleted and no credential is ever printed.

    uv run python -m scripts.test_tool_executor check
    uv run python -m scripts.test_tool_executor setup --host HOST --service SERVICE
    uv run python -m scripts.test_tool_executor run --investigation-id I --assignment-id A \\
        --tool signoz.trace_stats --args '{"group_by": "operation"}' --last 60
    uv run python -m scripts.test_tool_executor inspect --assignment-id A
"""

import argparse
import asyncio
import json
import logging
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select, text

from src.config import get_settings
from src.db.session import SessionLocal, engine
from src.integrations.signoz import SigNozError, check_health, load_signoz_settings
from src.models import (
    Environment,
    EnvironmentStatus,
    EnvironmentType,
    Evidence,
    InvestigationAssignment,
    ToolCall,
)
from src.schemas.environment import EnvironmentCreate
from src.schemas.investigation import InvestigationCreate
from src.services import environments, investigations
from src.tools import ExecutionContext, execute_tool
from src.tools.scope import SCOPE_KEY, EnvironmentScope

SUMMARY_PREVIEW_CHARS = 300


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(2)


def emit(value: Any) -> None:
    print(json.dumps(value, indent=2, default=str))


async def check(_: argparse.Namespace) -> None:
    async with engine.connect() as conn:
        revision = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
    try:
        settings = load_signoz_settings()
    except SigNozError as exc:
        # SigNozError messages name variables, never their values.
        emit({"database": "ok", "migration": revision, "signoz": exc.message})
        return
    url = httpx.URL(settings.base_url)
    try:
        healthy: Any = await check_health(settings=settings)
    except SigNozError as exc:
        healthy = f"{exc.code}: {exc.message}"
    emit(
        {
            "database": "ok",
            "migration": revision,
            # Scheme, host and port only: a URL may embed credentials.
            "signoz_url": f"{url.scheme}://{url.host}:{url.port or ''}".rstrip(":"),
            "signoz_api_key_configured": bool(settings.api_key.get_secret_value()),
            # Health is open access; the first query is what proves the key works.
            "signoz_healthy": healthy,
        }
    )


async def setup(args: argparse.Namespace) -> None:
    scope = EnvironmentScope(hosts=[args.host], services=args.service).model_dump(mode="json")

    async with SessionLocal() as session:
        environment = await session.scalar(
            select(Environment).where(Environment.name == args.env_name)
        )
        if environment is None:
            environment = await environments.create_environment(
                session,
                EnvironmentCreate(
                    name=args.env_name,
                    type=EnvironmentType.LOCAL,
                    metadata_json={SCOPE_KEY: scope},
                ),
            )
            created = True
        else:
            # Existing records are never modified here; a mismatch needs a deliberate fix.
            if environment.metadata_json.get(SCOPE_KEY) != scope:
                fail(
                    f"environment {args.env_name!r} exists with a different SigNoz scope: "
                    f"{environment.metadata_json.get(SCOPE_KEY)!r}"
                )
            if environment.status != EnvironmentStatus.ACTIVE:
                fail(f"environment {args.env_name!r} is {environment.status}")
            created = False
        environment_id = environment.id

    async with SessionLocal() as session:
        investigation = await investigations.create_investigation(
            session,
            InvestigationCreate(
                environment_id=environment_id, title=args.title, question=args.question
            ),
        )
        investigation_id = investigation.id

    async with SessionLocal() as session:
        investigation = await investigations.start_investigation(session, investigation_id)
        (assignment,) = investigation.assignments
        emit(
            {
                "environment_id": environment_id,
                "environment_created": created,
                "signoz_scope": scope,
                "investigation_id": investigation.id,
                "investigation_status": investigation.status,
                "assignment_id": assignment.id,
                "assignment_status": assignment.status,
            }
        )


def parse_arguments(raw: str) -> Any:
    if raw.startswith("@"):
        raw = Path(raw[1:]).read_text()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(f"--args is not valid JSON: {exc.msg}")


async def run(args: argparse.Namespace) -> None:
    arguments = parse_arguments(args.args)
    if args.last is not None:
        if not isinstance(arguments, dict):
            fail("--last needs --args to be a JSON object")
        end = datetime.now(UTC)
        arguments.setdefault(
            "time_range",
            {"start": (end - timedelta(minutes=args.last)).isoformat(), "end": end.isoformat()},
        )

    # The environment comes from the assignment row, never from the command line, the
    # same way the orchestrator will bind it. The investigation ID is passed through as
    # given so the executor's own context check is exercised.
    async with SessionLocal() as session:
        environment_id = await session.scalar(
            select(InvestigationAssignment.environment_id).where(
                InvestigationAssignment.id == args.assignment_id
            )
        )
    if environment_id is None:
        fail(f"assignment {args.assignment_id} not found")

    context = ExecutionContext(
        investigation_id=args.investigation_id,
        assignment_id=args.assignment_id,
        environment_id=environment_id,
        idempotency_key=args.idempotency_key,
    )
    result = await execute_tool(args.tool, arguments, context)
    # Already sanitized by the executor: bounded, redacted, secrets scrubbed.
    emit(result.model_dump(mode="json", exclude={"data"} if args.no_data else None))


async def inspect(args: argparse.Namespace) -> None:
    async with SessionLocal() as session:
        calls = (
            await session.scalars(
                select(ToolCall)
                .where(ToolCall.assignment_id == args.assignment_id)
                .order_by(ToolCall.created_at)
            )
        ).all()
        evidence = (
            await session.scalars(
                select(Evidence)
                .where(Evidence.assignment_id == args.assignment_id)
                .order_by(Evidence.collected_at)
            )
        ).all()
    meta = [c.output_metadata_json or {} for c in calls]
    emit(
        {
            "tool_calls": [
                {
                    "id": c.id,
                    "tool_name": c.tool_name,
                    "status": c.status,
                    "error_code": c.error_code,
                    "error_message": c.error_message,
                    "idempotency_key": c.idempotency_key,
                    "attempt_number": c.attempt_number,
                    "duration_ms": c.duration_ms,
                    "started_at": c.started_at,
                    "completed_at": c.completed_at,
                    "outcome": m.get("outcome"),
                    "telemetry_available": m.get("telemetry_available"),
                    "scope": m.get("scope"),
                    "evidence_ids": m.get("evidence_ids"),
                    "result_summary": (c.result_summary or "")[:SUMMARY_PREVIEW_CHARS],
                }
                for c, m in zip(calls, meta, strict=True)
            ],
            "evidence": [
                {
                    "id": e.id,
                    "tool_call_id": e.tool_call_id,
                    "investigation_id": e.investigation_id,
                    "environment_id": e.environment_id,
                    "evidence_type": e.evidence_type,
                    "title": e.title,
                    "entity": [e.entity_type, e.entity_identifier],
                    "source_reference": e.source_reference,
                    "observed_at": e.observed_at,
                    "redaction_applied": e.redaction_applied,
                    "data_json_bytes": len(json.dumps(e.data_json)),
                }
                for e in evidence
            ],
        }
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="database, migration and SigNoz reachability")

    p = sub.add_parser("setup", help="environment + fresh running investigation via services")
    p.add_argument("--env-name", default="ubuntu-vm")
    p.add_argument("--host", required=True, help="host.name exactly as SigNoz shows it")
    p.add_argument(
        "--service", action="append", required=True, help="service.name; repeat for more"
    )
    p.add_argument("--title", default="Manual tool executor test")
    p.add_argument("--question", default="Manual verification of the tool executor.")

    p = sub.add_parser("run", help="execute one tool through execute_tool()")
    p.add_argument("--investigation-id", type=uuid.UUID, required=True)
    p.add_argument("--assignment-id", type=uuid.UUID, required=True)
    p.add_argument("--tool", required=True)
    p.add_argument("--args", default="{}", help="JSON object, or @path/to/file.json")
    p.add_argument(
        "--last", type=int, metavar="MINUTES", help="add time_range ending now if absent"
    )
    p.add_argument("--idempotency-key")
    p.add_argument("--no-data", action="store_true", help="omit the data payload")

    p = sub.add_parser("inspect", help="ToolCall and Evidence rows of an assignment")
    p.add_argument("--assignment-id", type=uuid.UUID, required=True)
    return parser


async def main() -> None:
    args = build_parser().parse_args()
    if get_settings().app_env == "production":
        fail("development-only script; refusing to run with APP_ENV=production")
    logging.basicConfig(level=logging.WARNING)
    try:
        await {"check": check, "setup": setup, "run": run, "inspect": inspect}[args.command](args)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
