import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.models import (
    Environment,
    EnvironmentType,
    Evidence,
    EvidenceType,
    ImmutableRecordError,
    Investigation,
    InvestigationAssignment,
    ToolCall,
    ToolCallStatus,
    ToolCategory,
    TriggerType,
)

STARTED = datetime(2026, 10, 15, 1, 31, 2, tzinfo=UTC)


async def _assignment(session: AsyncSession) -> InvestigationAssignment:
    assignment = InvestigationAssignment(
        investigation=Investigation(
            title="API latency spike",
            question="Why did /checkout p99 latency spike?",
            trigger_type=TriggerType.MANUAL,
        ),
        environment=Environment(name=f"env-{uuid.uuid4().hex[:8]}", type=EnvironmentType.LOCAL),
        round=1,
        objective="Find the slow span in /checkout traces",
    )
    session.add(assignment)
    await session.flush()
    return assignment


def _tool_call(assignment: InvestigationAssignment, **overrides: Any) -> ToolCall:
    return ToolCall(
        assignment=assignment,
        tool_name="signoz.query_traces",
        tool_category=ToolCategory.TELEMETRY,
        **overrides,
    )


def _evidence(
    assignment: InvestigationAssignment, tool_call: ToolCall | None = None, **overrides: Any
) -> Evidence:
    fields: dict[str, Any] = {
        "evidence_type": EvidenceType.TRACE,
        "source_provider": "signoz",
        "source_reference": "trace:4bf92f3577b34da6a3ce929d0e0e4736",
        "title": "Slowest /checkout trace spent 2.1s in pg.query",
        "summary": "Trace 4bf92f35 took 2310 ms; span pg.query took 2140 ms of it.",
        "data_json": {"trace_duration_ms": 2310, "span": "pg.query", "span_duration_ms": 2140},
    }
    return Evidence(assignment=assignment, tool_call=tool_call, **(fields | overrides))


async def test_tool_call_defaults(session: AsyncSession) -> None:
    tool_call = _tool_call(await _assignment(session))
    session.add(tool_call)
    await session.flush()

    assert tool_call.status is ToolCallStatus.PENDING
    assert tool_call.attempt_number == 1
    assert tool_call.input_json == {}
    assert tool_call.output_metadata_json is None
    assert tool_call.created_at.tzinfo is not None


async def test_rejected_tool_call_needs_no_timestamps(session: AsyncSession) -> None:
    tool_call = _tool_call(
        await _assignment(session),
        status=ToolCallStatus.REJECTED,
        error_code="permission.denied",
        error_message="tool not allowed for this environment",
    )
    session.add(tool_call)
    await session.flush()

    assert tool_call.started_at is None
    assert tool_call.completed_at is None


async def test_evidence_links_tool_call_and_assignment(session: AsyncSession) -> None:
    assignment = await _assignment(session)
    tool_call = _tool_call(assignment, status=ToolCallStatus.SUCCEEDED)
    session.add(_evidence(assignment, tool_call))
    await session.flush()
    session.expunge_all()

    loaded = await session.scalar(
        select(Evidence).options(
            selectinload(Evidence.tool_call), selectinload(Evidence.assignment)
        )
    )

    assert loaded is not None
    assert loaded.assignment.id == assignment.id
    assert loaded.tool_call is not None
    assert loaded.tool_call.id == tool_call.id
    assert loaded.redaction_applied is False
    assert loaded.collected_at.tzinfo is not None


async def test_evidence_without_tool_call(session: AsyncSession) -> None:
    evidence = _evidence(await _assignment(session), tool_call=None)
    session.add(evidence)
    await session.flush()

    assert evidence.tool_call_id is None


async def test_evidence_tool_call_must_share_assignment(session: AsyncSession) -> None:
    tool_call = _tool_call(await _assignment(session))
    session.add(tool_call)
    other_assignment = await _assignment(session)
    session.add(_evidence(other_assignment, tool_call))

    with pytest.raises(IntegrityError, match="fk_evidence_tool_call_id_tool_calls"):
        await session.flush()


async def test_tool_call_cancelled_before_start(session: AsyncSession) -> None:
    tool_call = _tool_call(
        await _assignment(session), status=ToolCallStatus.CANCELLED, completed_at=STARTED
    )
    session.add(tool_call)
    await session.flush()

    assert tool_call.started_at is None


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        (
            {"started_at": STARTED, "completed_at": STARTED - timedelta(seconds=1)},
            "completed_after_started",
        ),
        ({"duration_ms": -1}, "duration_ms_nonnegative"),
        ({"attempt_number": 0}, "attempt_number_positive"),
        ({"input_json": {"query": "x" * 16_384}}, "input_json_size"),
        ({"output_metadata_json": {"rows": "x" * 65_536}}, "output_metadata_json_size"),
        ({"result_summary": "x" * 4_001}, "result_summary_length"),
        ({"error_message": "x" * 2_001}, "error_message_length"),
    ],
)
async def test_tool_call_check_constraints(
    session: AsyncSession, overrides: dict[str, Any], constraint: str
) -> None:
    session.add(_tool_call(await _assignment(session), **overrides))

    with pytest.raises(IntegrityError, match=f"ck_tool_calls_{constraint}"):
        await session.flush()


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"summary": "x" * 4_001}, "summary_length"),
        ({"data_json": {"raw": "x" * 65_536}}, "data_json_size"),
    ],
)
async def test_evidence_check_constraints(
    session: AsyncSession, overrides: dict[str, Any], constraint: str
) -> None:
    session.add(_evidence(await _assignment(session), **overrides))

    with pytest.raises(IntegrityError, match=f"ck_evidence_{constraint}"):
        await session.flush()


async def test_deleting_assignment_with_tool_calls_is_restricted(session: AsyncSession) -> None:
    assignment = await _assignment(session)
    session.add(_tool_call(assignment))
    await session.flush()

    await session.delete(assignment)
    with pytest.raises(IntegrityError, match="fk_tool_calls_assignment_id"):
        await session.flush()


async def test_deleting_assignment_with_evidence_is_restricted(session: AsyncSession) -> None:
    assignment = await _assignment(session)
    session.add(_evidence(assignment))
    await session.flush()

    await session.delete(assignment)
    with pytest.raises(IntegrityError, match="fk_evidence_assignment_id"):
        await session.flush()


async def test_deleting_tool_call_with_evidence_is_restricted(session: AsyncSession) -> None:
    assignment = await _assignment(session)
    tool_call = _tool_call(assignment)
    session.add(_evidence(assignment, tool_call))
    await session.flush()

    await session.delete(tool_call)
    with pytest.raises(IntegrityError, match="fk_evidence_tool_call_id_tool_calls"):
        await session.flush()


async def test_evidence_is_immutable(session: AsyncSession) -> None:
    evidence = _evidence(await _assignment(session))
    session.add(evidence)
    await session.flush()

    evidence.summary = "rewritten after the fact"
    with pytest.raises(ImmutableRecordError):
        await session.flush()


async def test_more_evidence_on_same_tool_call_is_allowed(session: AsyncSession) -> None:
    assignment = await _assignment(session)
    tool_call = _tool_call(assignment)
    session.add(_evidence(assignment, tool_call))
    await session.flush()

    # Appending to tool_call.evidence must insert the new record, not update the first one.
    session.add(_evidence(assignment, tool_call, source_reference="trace:a3ce929d"))
    await session.flush()
