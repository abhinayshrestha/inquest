"""Phase 6E read endpoints: dashboard counts, investigation filters, report explorer."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import (
    Environment,
    EnvironmentStatus,
    EnvironmentType,
    Investigation,
    InvestigationAssignment,
    InvestigationReport,
    InvestigationStatus,
    InvestigationTurn,
    TriggerType,
    TurnDecision,
    TurnKind,
    TurnStatus,
)
from tests.records import add_assignment
from tests.tenancy import Member

# Far in the future so these rows sort first in the shared test database.
FUTURE = datetime(2099, 1, 1, tzinfo=UTC)


def _env(tenant: Member, **fields) -> Environment:
    return Environment(
        name=f"env-{uuid.uuid4().hex[:8]}", type=EnvironmentType.LOCAL, **tenant.owns, **fields
    )


async def _investigation(
    session: AsyncSession,
    tenant: Member,
    title: str,
    *,
    minutes: int = 0,
    environment: Environment | None = None,
    status: InvestigationStatus = InvestigationStatus.IDLE,
) -> InvestigationAssignment:
    investigation = Investigation(
        title=title, question=f"{title}?", trigger_type=TriggerType.MANUAL, status=status,
        created_at=FUTURE + timedelta(minutes=minutes), organization_id=tenant.organization_id,
    )  # fmt: skip
    assignment = await add_assignment(
        session, investigation=investigation, environment=environment or _env(tenant)
    )
    await session.flush()
    return assignment


async def _finish(session: AsyncSession, turn_id: uuid.UUID, status: TurnStatus) -> None:
    turn = await session.get(InvestigationTurn, turn_id)
    now = datetime.now(UTC)
    turn.status, turn.started_at, turn.completed_at = status, now, now
    if status == TurnStatus.COMPLETED:
        turn.decision, turn.answer_json = TurnDecision.INVESTIGATED, {"answer_md": "ok"}
    await session.flush()


async def _follow_up(
    session: AsyncSession, investigation_id: uuid.UUID, seq: int, status: TurnStatus
) -> InvestigationTurn:
    turn = InvestigationTurn(
        investigation_id=investigation_id, seq=seq, kind=TurnKind.FOLLOW_UP,
        question=f"follow-up {seq}",
    )  # fmt: skip
    session.add(turn)
    await session.flush()
    await _finish(session, turn.id, status)
    return turn


def _report(
    assignment: InvestigationAssignment,
    turn_id: uuid.UUID,
    version: int,
    body: dict[str, Any],
    minutes: int,
) -> InvestigationReport:
    return InvestigationReport(
        investigation_id=assignment.investigation_id, turn_id=turn_id, version=version,
        report_json=body, created_at=FUTURE + timedelta(minutes=minutes),
    )  # fmt: skip


def _body(outcome: str, conclusions: int) -> dict[str, Any]:
    return {"outcome": outcome, "summary": "s", "conclusions": [{"statement": "x"}] * conclusions}


# --- dashboard -----------------------------------------------------------------------------


async def test_overview_counts_match_persisted_records(
    client: AsyncClient, session: AsyncSession, tenant: Member
):
    before = (await client.get("/overview")).json()

    answered = await _investigation(session, tenant, "answered")
    await _finish(session, answered.turn_id, TurnStatus.COMPLETED)
    failed = await _investigation(session, tenant, "failed later")
    await _finish(session, failed.turn_id, TurnStatus.COMPLETED)
    await _follow_up(session, failed.investigation_id, 2, TurnStatus.FAILED)
    await _investigation(session, tenant, "running", status=InvestigationStatus.INVESTIGATING)
    session.add(_env(tenant, status=EnvironmentStatus.DISABLED))
    session.add(_report(answered, answered.turn_id, 1, _body("inconclusive", 1), 0))
    await session.flush()

    after = (await client.get("/overview")).json()

    def delta(*path: str) -> int:
        a, b = after, before
        for key in path:
            a, b = a[key], b[key]
        return a - b

    assert delta("investigations_total") == 3
    assert delta("investigations_by_status", "idle") == 2
    assert delta("investigations_by_status", "investigating") == 1
    # The latest turn decides: one answered, one whose last follow-up failed, one pending.
    assert delta("latest_turn_by_status", "completed") == 1
    assert delta("latest_turn_by_status", "failed") == 1
    assert delta("latest_turn_by_status", "pending") == 1
    assert delta("turns_total") == 4
    assert delta("turns_by_status", "completed") == 2
    # Three new active environments from the investigations, one disabled.
    assert delta("environments_total") == 4
    assert delta("environments_active") == 3
    assert delta("reports_total") == 1
    assert delta("investigations_with_reports") == 1
    # Zero-filled: every status is always present, and nothing but stored counts is returned.
    assert set(after["investigations_by_status"]) == {s.value for s in InvestigationStatus}
    assert set(after["latest_turn_by_status"]) == {s.value for s in TurnStatus}
    assert not {"health", "uptime", "alerts", "score"} & set(after)


async def test_investigations_filter_by_status_and_latest_turn(
    client: AsyncClient, session: AsyncSession, tenant: Member
):
    answered = await _investigation(session, tenant, "answered", minutes=2)
    await _finish(session, answered.turn_id, TurnStatus.COMPLETED)
    failed = await _investigation(session, tenant, "failed later", minutes=1)
    await _finish(session, failed.turn_id, TurnStatus.COMPLETED)
    await _follow_up(session, failed.investigation_id, 2, TurnStatus.FAILED)
    running = await _investigation(
        session, tenant, "running", status=InvestigationStatus.CORRELATING
    )

    def titles(body: dict) -> list[str]:
        mine = {"answered", "failed later", "running"}
        return [i["title"] for i in body["items"] if i["title"] in mine]

    params = [("latest_turn_status", "failed"), ("latest_turn_status", "cancelled")]
    body = (await client.get("/investigations", params=params)).json()
    assert titles(body) == ["failed later"]
    item = next(i for i in body["items"] if i["title"] == "failed later")
    assert item["latest_turn_status"] == "failed"
    assert item["latest_turn_completed_at"] is not None

    body = (await client.get("/investigations", params={"status": "correlating"})).json()
    assert [i["id"] for i in body["items"] if i["title"] == "running"] == [
        str(running.investigation_id)
    ]
    body = (await client.get("/investigations", params={"latest_turn_status": "completed"})).json()
    assert titles(body) == ["answered"]
    assert (await client.get("/investigations", params={"status": "bogus"})).status_code == 422


# --- report explorer -----------------------------------------------------------------------


async def _reports(
    session: AsyncSession, tenant: Member
) -> tuple[Environment, InvestigationAssignment]:
    env = _env(tenant)
    first = await _investigation(session, tenant, "Latency on /slow", environment=env)
    await _finish(session, first.turn_id, TurnStatus.COMPLETED)
    second_turn = await _follow_up(session, first.investigation_id, 2, TurnStatus.COMPLETED)
    other = await _investigation(session, tenant, "100%_errors on /error")
    await _finish(session, other.turn_id, TurnStatus.COMPLETED)
    session.add_all(
        [
            _report(first, first.turn_id, 1, _body("root_cause_identified", 6), 1),
            _report(first, second_turn.id, 2, _body("inconclusive", 3), 2),
            # A malformed stored body must not break the listing.
            _report(other, other.turn_id, 1, {"outcome": "made_up", "conclusions": {"x": 1}}, 3),
        ]
    )
    await session.flush()
    return env, first


async def test_reports_list_versions_across_investigations(
    client: AsyncClient, session: AsyncSession, tenant: Member
):
    _, first = await _reports(session, tenant)

    body = (await client.get("/reports", params={"limit": 3})).json()

    rows = [
        (r["investigation_title"], r["version"], r["is_latest"], r["outcome"])
        for r in body["items"]
    ]
    assert rows == [
        ("100%_errors on /error", 1, True, None),
        ("Latency on /slow", 2, True, "inconclusive"),
        ("Latency on /slow", 1, False, "root_cause_identified"),
    ]
    assert body["items"][1]["turn_seq"] == 2
    assert body["items"][1]["conclusion_count"] == 3
    assert body["items"][0]["conclusion_count"] == 0
    assert body["items"][2]["investigation_id"] == str(first.investigation_id)
    page = (await client.get("/reports", params={"limit": 2})).json()
    assert page["next_offset"] == 2
    rest = (await client.get("/reports", params={"limit": 2, "offset": 2})).json()
    assert rest["items"][0]["id"] == body["items"][2]["id"]
    # Metadata only: no report body in the listing.
    assert not {"report_json", "summary", "conclusions"} & set(body["items"][0])


async def test_reports_filters_and_bounds(
    client: AsyncClient, session: AsyncSession, tenant: Member
):
    env, _ = await _reports(session, tenant)

    async def versions(**params) -> list[tuple[str, int]]:
        body = (await client.get("/reports", params={"limit": 100} | params)).json()
        mine = {"Latency on /slow", "100%_errors on /error"}
        return [
            (r["investigation_title"], r["version"])
            for r in body["items"]
            if r["investigation_title"] in mine
        ]

    assert await versions(latest_only="true") == [
        ("100%_errors on /error", 1),
        ("Latency on /slow", 2),
    ]
    assert await versions(outcome="root_cause_identified") == [("Latency on /slow", 1)]
    assert await versions(environment_id=str(env.id)) == [
        ("Latency on /slow", 2),
        ("Latency on /slow", 1),
    ]
    # The search term is literal text: "%" and "_" are not wildcards.
    assert await versions(q="100%_err") == [("100%_errors on /error", 1)]
    assert await versions(q="0%_errors on /slow") == []
    assert await versions(q="latency ON") == [("Latency on /slow", 2), ("Latency on /slow", 1)]

    for bad in ({"limit": 101}, {"offset": -1}, {"outcome": "guess"}, {"q": "x" * 101}):
        assert (await client.get("/reports", params=bad)).status_code == 422
