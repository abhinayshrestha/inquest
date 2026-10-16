"""Executor tests commit for real: the executor opens its own short transactions, so the
rolled-back `session` fixture cannot see or serialize them. Every row created here is
deleted again after the test."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, select

from src.db.session import SessionLocal
from src.integrations.signoz.settings import SigNozSettings
from src.models import (
    AssignmentStatus,
    Environment,
    EnvironmentStatus,
    EnvironmentType,
    Evidence,
    Investigation,
    InvestigationAssignment,
    InvestigationStatus,
    ToolCall,
    TriggerType,
)
from src.tools import ExecutionContext, ToolExecutionResult, execute_tool
from tests.signoz.conftest import TEST_API_KEY, load_fixture

SETTINGS = SigNozSettings(base_url="http://signoz.test", api_key=TEST_API_KEY)
EMPTY = {"status": "success", "data": {"data": {"results": []}, "meta": {}}}

Handler = Callable[[httpx.Request], Awaitable[httpx.Response] | httpx.Response]


def window(hours_ago: float = 1.0, minutes: int = 30) -> dict[str, str]:
    start = datetime.now(UTC) - timedelta(hours=hours_ago)
    return {
        "start": start.isoformat(),
        "end": (start + timedelta(minutes=minutes)).isoformat(),
    }


def trace_stats_args(**extra: Any) -> dict[str, Any]:
    return {"time_range": window(), "group_by": "operation", **extra}


def expression(request: httpx.Request, query: int = 0) -> str:
    body = json.loads(request.content)
    return body["compositeQuery"]["queries"][query]["spec"]["filter"]["expression"]


def replies(*bodies: Any) -> tuple[Handler, list[httpx.Request]]:
    """Answers requests in order: a dict is a 200 JSON body, an int a bare status, an
    exception is raised, a fixture name is loaded."""
    seen: list[httpx.Request] = []
    queue = list(bodies)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(body, BaseException):
            raise body
        if isinstance(body, int):
            return httpx.Response(body, json={})
        if isinstance(body, str):
            body = load_fixture(body)
        return httpx.Response(200, json=body)

    return handler, seen


@dataclass
class World:
    investigation_id: uuid.UUID
    environment_id: uuid.UUID
    assignment_id: uuid.UUID
    host: str
    service: str

    def context(self, key: str | None = None, **overrides: Any) -> ExecutionContext:
        fields = {
            "investigation_id": self.investigation_id,
            "assignment_id": self.assignment_id,
            "environment_id": self.environment_id,
            "idempotency_key": key,
        }
        return ExecutionContext(**(fields | overrides))

    async def run(
        self,
        tool: str,
        arguments: Any,
        handler: Handler,
        *,
        key: str | None = None,
        context: ExecutionContext | None = None,
        **kwargs: Any,
    ) -> ToolExecutionResult:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await execute_tool(
                tool,
                arguments,
                context or self.context(key),
                signoz_settings=SETTINGS,
                http_client=client,
                **kwargs,
            )

    async def tool_calls(self) -> list[ToolCall]:
        async with SessionLocal() as s:
            rows = await s.scalars(
                select(ToolCall)
                .where(ToolCall.assignment_id == self.assignment_id)
                .order_by(ToolCall.created_at, ToolCall.id)
            )
            return list(rows)

    async def evidence(self) -> list[Evidence]:
        async with SessionLocal() as s:
            rows = await s.scalars(
                select(Evidence).where(Evidence.assignment_id == self.assignment_id)
            )
            return list(rows)

    async def set_status(
        self,
        assignment: AssignmentStatus | None = None,
        investigation: InvestigationStatus | None = None,
    ) -> None:
        async with SessionLocal() as s, s.begin():
            if assignment is not None:
                (await s.get(InvestigationAssignment, self.assignment_id)).status = assignment  # type: ignore[union-attr]
            if investigation is not None:
                (await s.get(Investigation, self.investigation_id)).status = investigation  # type: ignore[union-attr]


WorldFactory = Callable[..., Awaitable[World]]


@pytest.fixture
async def make_world() -> AsyncIterator[WorldFactory]:
    created: list[World] = []

    async def make(
        *,
        signoz: dict[str, Any] | None | str = "default",
        env_type: EnvironmentType = EnvironmentType.LOCAL,
        env_status: EnvironmentStatus = EnvironmentStatus.ACTIVE,
        assignment_status: AssignmentStatus = AssignmentStatus.RUNNING,
        investigation_status: InvestigationStatus = InvestigationStatus.INVESTIGATING,
    ) -> World:
        suffix = uuid.uuid4().hex[:8]
        host, service = f"host-{suffix}", f"svc-{suffix}"
        scope = {"hosts": [host], "services": [service]} if signoz == "default" else signoz
        metadata = {} if scope is None else {"signoz": scope}
        async with SessionLocal() as s, s.begin():
            assignment = InvestigationAssignment(
                investigation=Investigation(
                    title="Checkout errors",
                    question="Why is /checkout failing?",
                    trigger_type=TriggerType.MANUAL,
                    status=investigation_status,
                ),
                environment=Environment(
                    name=f"env-{suffix}",
                    type=env_type,
                    status=env_status,
                    metadata_json=metadata,
                ),
                round=1,
                objective="Find the failing span",
                status=assignment_status,
            )
            s.add(assignment)
            await s.flush()
            world = World(
                assignment.investigation_id, assignment.environment_id, assignment.id, host, service
            )
        created.append(world)
        return world

    before = asyncio.all_tasks()
    yield make

    # A failed concurrency test can leave executor tasks running; let them finish their
    # bookkeeping first, or they would insert rows after the cleanup below.
    stray = [t for t in asyncio.all_tasks() - before if t is not asyncio.current_task()]
    for task in stray:
        task.cancel()
    await asyncio.gather(*stray, return_exceptions=True)

    async with SessionLocal() as s, s.begin():
        assignments = [w.assignment_id for w in created]
        await s.execute(delete(Evidence).where(Evidence.assignment_id.in_(assignments)))
        await s.execute(delete(ToolCall).where(ToolCall.assignment_id.in_(assignments)))
        await s.execute(
            delete(InvestigationAssignment).where(InvestigationAssignment.id.in_(assignments))
        )
        await s.execute(
            delete(Investigation).where(Investigation.id.in_([w.investigation_id for w in created]))
        )
        await s.execute(
            delete(Environment).where(Environment.id.in_([w.environment_id for w in created]))
        )


@pytest.fixture
async def world(make_world: WorldFactory) -> World:
    return await make_world()
