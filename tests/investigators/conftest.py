"""Local Investigator tests run the real graph, the real in-process MCP app, the real Tool
Executor and real PostgreSQL checkpoints against the test database. Only the model
(scripted) and SigNoz (mocked HTTP) are fake. Rows and checkpoint threads created here are
deleted afterwards."""

import inspect
import re
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel
from sqlalchemy import delete

from src.db.session import SessionLocal
from src.investigators import policy as investigator_policy
from src.investigators.checkpoint import open_checkpointer, thread_id
from src.investigators.local_graph import LocalRunResult, run_local_investigator
from src.mcp_server import McpSettings
from src.models import Finding, FindingEvidence, Hypothesis, HypothesisEvidence
from src.tools import policy as tool_policy
from tests.mcp_server.conftest import SECRET, mcp_app, signoz  # noqa: F401
from tests.tools.conftest import World, make_world, world  # noqa: F401

# Mints tokens; the derived URL (the loopback listener) reaches the in-process app.
MCP_SETTINGS = McpSettings(token_secret=SECRET)
USAGE = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}
EVIDENCE_REF = re.compile(r"\bev_[0-9a-f]{8}\b")
HYPOTHESIS_REF = re.compile(r"\bhy_[0-9a-f]{8}\b")

Script = Any  # an item, an exception to raise, or a (possibly async) callable(messages)


class ScriptedModel:
    """Stands in for ChatOpenAI: agent replies and structured outputs come from scripts,
    in order. Records every prompt so tests can check what the model was shown."""

    def __init__(self, replies: list[Script] = (), structured: list[Script] = ()):  # type: ignore[assignment]
        self.replies = list(replies)
        self.structured = list(structured)
        self.agent_prompts: list[list[Any]] = []
        self.structured_prompts: list[list[Any]] = []
        self.tools: list[dict[str, Any]] = []
        self.methods: list[tuple[str, str]] = []

    def bind_tools(self, tools: list[dict[str, Any]]) -> "_Runnable":
        self.tools = tools
        return _Runnable(self._reply)

    def with_structured_output(
        self, schema: type, *, method: str, include_raw: bool
    ) -> "_Runnable":
        assert method in ("json_schema", "function_calling") and include_raw
        self.methods.append((schema.__name__, method))
        return _Runnable(self._structured)

    async def _next(self, queue: list[Script], messages: list[Any]) -> Any:
        assert queue, "model called more often than scripted"
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item) and not isinstance(item, BaseModel):
            item = item(messages)
            if inspect.isawaitable(item):
                item = await item
        return item

    async def _reply(self, messages: list[Any]) -> AIMessage:
        self.agent_prompts.append(messages)
        reply = await self._next(self.replies, messages)
        if isinstance(reply, AIMessage):
            return reply
        return AIMessage(content=str(reply), usage_metadata=USAGE)

    async def _structured(self, messages: list[Any]) -> dict[str, Any]:
        self.structured_prompts.append(messages)
        item = await self._next(self.structured, messages)
        if item is None:
            raw = AIMessage(content="not json", usage_metadata=USAGE)
            return {"raw": raw, "parsed": None, "parsing_error": ValueError("invalid JSON")}
        raw = AIMessage(content=item.model_dump_json(), usage_metadata=USAGE)
        return {"raw": raw, "parsed": item, "parsing_error": None}


class _Runnable:
    def __init__(self, fn: Callable[[list[Any]], Any]):
        self.fn = fn

    async def ainvoke(self, messages: list[Any]) -> Any:
        return await self.fn(messages)


def tool_call(name: str, args: dict[str, Any], call_id: str = "call_0") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
        usage_metadata=USAGE,
    )


def window(minutes: int = 30, ended_minutes_ago: int = 0) -> dict[str, str]:
    end = datetime.now(UTC) - timedelta(minutes=ended_minutes_ago)
    return {"start": (end - timedelta(minutes=minutes)).isoformat(), "end": end.isoformat()}


def refs_in(messages: list[Any], pattern: re.Pattern[str] = EVIDENCE_REF) -> list[str]:
    found: list[str] = []
    for m in messages:
        for r in pattern.findall(str(m.content)):
            if r not in found:
                found.append(r)
    return found


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tool_policy, "RETRY_BACKOFF_SECONDS", 0)


@pytest.fixture
async def checkpointer() -> AsyncIterator[Any]:
    async with open_checkpointer() as saver:
        saver.created_threads = []  # type: ignore[attr-defined]
        yield saver
        for tid in saver.created_threads:  # type: ignore[attr-defined]
            await saver.adelete_thread(tid)


@pytest.fixture
async def investigator_world(world: World) -> AsyncIterator[World]:  # noqa: F811
    yield world
    # Before make_world's cleanup: these rows reference the evidence it deletes.
    async with SessionLocal() as s, s.begin():
        await s.execute(
            delete(FindingEvidence).where(
                FindingEvidence.investigation_id == world.investigation_id
            )
        )
        await s.execute(
            delete(HypothesisEvidence).where(
                HypothesisEvidence.investigation_id == world.investigation_id
            )
        )
        await s.execute(delete(Finding).where(Finding.investigation_id == world.investigation_id))
        await s.execute(
            delete(Hypothesis).where(Hypothesis.investigation_id == world.investigation_id)
        )


@pytest.fixture
def run(mcp_app, checkpointer) -> Callable[..., Any]:  # noqa: F811
    async def go(target: World, model: ScriptedModel) -> LocalRunResult:
        checkpointer.created_threads.append(thread_id(target.assignment_id))
        return await run_local_investigator(
            target.assignment_id,
            checkpointer,
            model=model,  # type: ignore[arg-type]
            mcp_settings=MCP_SETTINGS,
            mcp_transport=httpx2.ASGITransport(app=mcp_app),
        )

    return go


@pytest.fixture
def tight(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    def set_limits(**limits: Any) -> None:
        for name, value in limits.items():
            monkeypatch.setattr(investigator_policy, name, value)

    return set_limits
