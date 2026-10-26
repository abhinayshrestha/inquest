from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import Environment
from src.schemas.environment import EnvironmentCreate


class EnvironmentNameTaken(Exception):
    pass


async def create_environment(session: AsyncSession, data: EnvironmentCreate) -> Environment:
    environment = Environment(**data.model_dump())
    session.add(environment)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        # Only translate the unique-name violation; anything else is a real bug.
        if exc.orig.diag.constraint_name == "uq_environments_name":
            raise EnvironmentNameTaken(data.name) from exc
        raise
    return environment


async def list_environments(session: AsyncSession, *, limit: int) -> list[Environment]:
    return list(await session.scalars(select(Environment).order_by(Environment.name).limit(limit)))
