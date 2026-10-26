"""Read-only endpoints for the web UI: investigation history, environment list and
citation inspection."""

import uuid
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.investigators.refs import ref
from src.models import (
    Environment,
    EnvironmentType,
    Evidence,
    EvidenceStance,
    EvidenceType,
    Finding,
    FindingEvidence,
    FindingStatus,
    Hypothesis,
    Investigation,
    InvestigationAssignment,
    InvestigationReport,
    ToolCall,
    ToolCategory,
    TriggerType,
)
from tests.records import add_assignment

# Far in the future so these rows sort before anything else in the shared test database
# (rows created in one transaction share now()).
FUTURE = datetime(2099, 1, 1, tzinfo=UTC)


async def _investigation(
    session: AsyncSession, title: str, created_at: datetime, environment: Environment
) -> InvestigationAssignment:
    investigation = Investigation(
        title=title, question=f"{title}?", trigger_type=TriggerType.MANUAL, created_at=created_at
    )
    assignment = await add_assignment(session, investigation=investigation, environment=environment)
    await session.flush()
    return assignment


async def test_history_lists_newest_first_with_counts_and_pages(
    client: AsyncClient, session: AsyncSession
) -> None:
    env = Environment(name=f"env-{uuid.uuid4().hex[:8]}", type=EnvironmentType.LOCAL)
    older = await _investigation(session, "older", FUTURE, env)
    newer = await _investigation(session, "newer", FUTURE + timedelta(minutes=1), env)
    session.add(
        InvestigationReport(
            investigation_id=newer.investigation_id,
            turn_id=newer.turn_id,
            version=1,
            report_json={"outcome": "inconclusive"},
        )
    )
    await session.flush()

    first = await client.get("/investigations", params={"limit": 1})
    assert first.status_code == 200
    body = first.json()
    assert [i["title"] for i in body["items"]] == ["newer"]
    assert body["next_offset"] == 1
    item = body["items"][0]
    assert item["turn_count"] == 1
    assert item["latest_report_version"] == 1
    assert item["environments"] == [
        {"id": str(env.id), "name": env.name, "type": "local", "status": "active"}
    ]
    # History rows carry no question, answers, reports or configuration.
    assert set(item) == {
        "id", "title", "status", "severity", "trigger_type", "created_at", "updated_at",
        "started_at", "completed_at", "environments", "turn_count", "latest_report_version",
    }  # fmt: skip

    second = (await client.get("/investigations", params={"limit": 1, "offset": 1})).json()
    assert second["items"][0]["id"] == str(older.investigation_id)
    assert second["items"][0]["latest_report_version"] is None


async def test_history_page_size_is_bounded(client: AsyncClient) -> None:
    assert (await client.get("/investigations", params={"limit": 101})).status_code == 422
    assert (await client.get("/investigations", params={"limit": 0})).status_code == 422
    assert (await client.get("/investigations", params={"offset": -1})).status_code == 422


async def test_environment_list_has_no_configuration(client: AsyncClient) -> None:
    name = f"env-{uuid.uuid4().hex[:8]}"
    created = await client.post(
        "/environments",
        json={"name": name, "type": "local", "metadata_json": {"signoz": {"hosts": ["vm"]}}},
    )
    assert created.status_code == 201

    listed = (await client.get("/environments")).json()
    mine = next(e for e in listed if e["name"] == name)
    assert set(mine) == {"id", "name", "type", "status"}
    assert (await client.get("/environments", params={"limit": 201})).status_code == 422


async def _records(session: AsyncSession) -> tuple[InvestigationAssignment, Evidence, Finding]:
    assignment = await add_assignment(session)
    await session.flush()
    tool_call = ToolCall(
        assignment=assignment, tool_name="signoz.trace_stats", tool_category=ToolCategory.TELEMETRY
    )
    evidence = Evidence(
        assignment=assignment,
        tool_call=tool_call,
        evidence_type=EvidenceType.TRACE,
        source_provider="signoz",
        title="GET /slow p95 1.5 s",
        summary="2 groups, 62 spans, 0 errors. Slowest: GET /slow p95 1514 ms (31 spans).",
        data_json={
            "time_range": {"start": "2026-10-21T17:16:00Z", "end": "2026-10-21T17:46:00Z"},
            "groups": [{"operation": "GET /slow", "secret_attribute": "raw telemetry"}],
        },
    )
    finding = Finding(
        assignment=assignment,
        title="/slow is consistently slow",
        summary="p95 ≈ 1.5 s over 31 spans.",
        status=FindingStatus.CONFIRMED,
        confidence=0.8,
    )
    link = FindingEvidence(finding=finding, evidence=evidence, stance=EvidenceStance.SUPPORTS)
    # One flush: adding the link later would count as an update of immutable evidence.
    session.add_all([tool_call, evidence, finding, link])
    await session.flush()
    return assignment, evidence, finding


async def test_evidence_citation_is_summary_and_window_only(
    client: AsyncClient, session: AsyncSession
) -> None:
    assignment, evidence, _ = await _records(session)

    response = await client.get(
        f"/investigations/{assignment.investigation_id}/citations/{ref('evidence', evidence.id)}"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "evidence"
    assert body["ref"] == ref("evidence", evidence.id)
    assert body["tool_name"] == "signoz.trace_stats"
    assert body["summary"].startswith("2 groups, 62 spans")
    assert body["window_start"] == "2026-10-21T17:16:00Z"
    assert body["window_end"] == "2026-10-21T17:46:00Z"
    assert body["environment"]["id"] == str(assignment.environment_id)
    # Raw tool output never leaves the backend.
    assert "data_json" not in body
    assert "raw telemetry" not in response.text


async def test_finding_and_hypothesis_citations(client: AsyncClient, session: AsyncSession) -> None:
    assignment, evidence, finding = await _records(session)
    hypothesis = Hypothesis(assignment=assignment, statement="Handler sleeps")
    session.add(hypothesis)
    await session.flush()
    base = f"/investigations/{assignment.investigation_id}/citations"

    found = (await client.get(f"{base}/{ref('finding', finding.id)}")).json()
    assert found["kind"] == "finding"
    assert found["status"] == "confirmed"
    assert found["evidence"] == [
        {"ref": ref("evidence", evidence.id), "stance": "supports", "title": evidence.title}
    ]

    hyp = (await client.get(f"{base}/{ref('hypothesis', hypothesis.id)}")).json()
    assert hyp == hyp | {"kind": "hypothesis", "statement": "Handler sleeps", "status": "active"}


async def test_citations_resolve_only_within_their_investigation(
    client: AsyncClient, session: AsyncSession
) -> None:
    _, evidence, _ = await _records(session)
    other = await add_assignment(session)
    await session.flush()

    response = await client.get(
        f"/investigations/{other.investigation_id}/citations/{ref('evidence', evidence.id)}"
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "citation not found"


async def test_citation_refs_are_validated(client: AsyncClient, session: AsyncSession) -> None:
    assignment, _, _ = await _records(session)
    base = f"/investigations/{assignment.investigation_id}/citations"

    for bad in ("ev_123", "xx_12345678", "ev_1234567g", "ev_%25%25%25%25%25%25%25%25"):
        assert (await client.get(f"{base}/{bad}")).status_code == 422, bad
    missing = await client.get(f"/investigations/{uuid.uuid4()}/citations/ev_00000000")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "investigation not found"


async def test_ambiguous_ref_resolves_to_nothing(
    client: AsyncClient, session: AsyncSession
) -> None:
    assignment, _, _ = await _records(session)
    for suffix in ("1", "2"):
        session.add(
            Evidence(
                id=uuid.UUID(f"abcdef12-0000-4000-8000-00000000000{suffix}"),
                assignment=assignment,
                evidence_type=EvidenceType.METRIC,
                source_provider="signoz",
                title="cpu",
                summary="cpu 1%",
            )
        )
    await session.flush()

    response = await client.get(
        f"/investigations/{assignment.investigation_id}/citations/ev_abcdef12"
    )
    assert response.status_code == 404
