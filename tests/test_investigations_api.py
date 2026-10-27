import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import Environment, EnvironmentType, Investigation, InvestigationAssignment
from src.schemas.investigation import InvestigationCreate
from src.services import investigations
from tests.tenancy import Member


async def _create_environment(client: AsyncClient, tenant: Member, name: str = "ubuntu-vm") -> dict:
    response = await client.post(
        "/environments",
        json={"organization_id": str(tenant.organization_id), "name": name, "type": "local"},
    )
    assert response.status_code == 201
    return response.json()


async def _create_investigation(client: AsyncClient, environment_id: str) -> dict:
    response = await client.post(
        "/investigations",
        json={
            "environment_id": environment_id,
            "title": "API latency spike",
            "question": "Why did /checkout p99 latency spike?",
            "severity": "high",
        },
    )
    assert response.status_code == 201
    return response.json()


async def test_create_environment(client: AsyncClient, tenant: Member) -> None:
    response = await client.post(
        "/environments",
        json={
            "organization_id": str(tenant.organization_id),
            "name": "ubuntu-vm",
            "type": "local",
            "metadata_json": {"host": "utm"},
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "ubuntu-vm"
    assert body["type"] == "local"
    assert body["status"] == "active"
    assert body["metadata_json"] == {"host": "utm"}
    uuid.UUID(body["id"])


async def test_duplicate_environment_name_conflicts(client: AsyncClient, tenant: Member) -> None:
    await _create_environment(client, tenant)

    response = await client.post(
        "/environments",
        json={"organization_id": str(tenant.organization_id), "name": "ubuntu-vm", "type": "aws"},
    )

    assert response.status_code == 409


async def test_create_investigation(client: AsyncClient, tenant: Member) -> None:
    environment = await _create_environment(client, tenant)

    body = await _create_investigation(client, environment["id"])

    assert body["title"] == "API latency spike"
    assert body["status"] == "pending"
    assert body["severity"] == "high"
    assert body["trigger_type"] == "manual"
    assert body["current_round"] == 1
    assert body["started_at"] is None
    assert body["final_result_json"] is None


async def test_create_investigation_without_severity(client: AsyncClient, tenant: Member) -> None:
    environment = await _create_environment(client, tenant)

    response = await client.post(
        "/investigations",
        json={"environment_id": environment["id"], "title": "t", "question": "q"},
    )

    assert response.status_code == 201
    assert response.json()["severity"] is None


async def test_investigation_creates_initial_assignment(
    client: AsyncClient, session: AsyncSession, tenant: Member
) -> None:
    environment = await _create_environment(client, tenant)

    body = await _create_investigation(client, environment["id"])

    [assignment] = body["assignments"]
    assert assignment["round"] == 1
    assert assignment["status"] == "pending"
    assert assignment["objective"] == "Why did /checkout p99 latency spike?"
    assert assignment["environment"]["id"] == environment["id"]

    rows = await session.scalars(
        select(InvestigationAssignment).where(
            InvestigationAssignment.investigation_id == uuid.UUID(body["id"])
        )
    )
    assert len(rows.all()) == 1


async def test_create_investigation_with_unknown_environment(
    client: AsyncClient, session: AsyncSession, tenant: Member
) -> None:
    response = await client.post(
        "/investigations",
        json={"environment_id": str(uuid.uuid4()), "title": "t", "question": "q"},
    )

    assert response.status_code == 404
    assert await session.scalar(select(func.count()).select_from(Investigation)) == 0


async def test_get_investigation_with_assignments(client: AsyncClient, tenant: Member) -> None:
    environment = await _create_environment(client, tenant)
    created = await _create_investigation(client, environment["id"])

    response = await client.get(f"/investigations/{created['id']}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == created["id"]
    assert body["question"] == "Why did /checkout p99 latency spike?"
    [assignment] = body["assignments"]
    assert assignment["id"] == created["assignments"][0]["id"]
    assert assignment["environment"] == {
        "id": environment["id"],
        "name": "ubuntu-vm",
        "type": "local",
        "status": "active",
    }


async def test_get_unknown_investigation(client: AsyncClient, tenant: Member) -> None:
    response = await client.get(f"/investigations/{uuid.uuid4()}")

    assert response.status_code == 404


async def test_investigation_rolled_back_when_assignment_insert_fails(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = Environment(name="ubuntu-vm", type=EnvironmentType.LOCAL)
    session.add(environment)
    await session.commit()
    # round = 0 violates the assignment CHECK, failing the second insert of the transaction.
    monkeypatch.setattr(investigations, "INITIAL_ROUND", 0)

    with pytest.raises(IntegrityError, match="ck_investigation_assignments_round_positive"):
        await investigations.create_investigation(
            session,
            InvestigationCreate(environment_id=environment.id, title="t", question="q"),
        )
    await session.rollback()

    assert await session.scalar(select(func.count()).select_from(Investigation)) == 0
