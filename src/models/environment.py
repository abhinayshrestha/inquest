from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, str_enum

if TYPE_CHECKING:
    from src.models.assignment import InvestigationAssignment


class EnvironmentType(StrEnum):
    LOCAL = "local"
    AWS = "aws"


class EnvironmentStatus(StrEnum):
    ACTIVE = "active"
    # Retire environments instead of deleting them; assignments keep referencing them.
    DISABLED = "disabled"


class Environment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "environments"

    name: Mapped[str] = mapped_column(String(255), unique=True)
    type: Mapped[EnvironmentType] = mapped_column(str_enum(EnvironmentType))
    status: Mapped[EnvironmentStatus] = mapped_column(
        str_enum(EnvironmentStatus),
        default=EnvironmentStatus.ACTIVE,
        server_default=EnvironmentStatus.ACTIVE.value,
    )
    # `metadata` is reserved by SQLAlchemy Declarative.
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )

    # passive_deletes="all": let the RESTRICT FK reject the delete instead of the ORM
    # trying to NULL out a non-nullable environment_id.
    assignments: Mapped[list["InvestigationAssignment"]] = relationship(
        back_populates="environment", passive_deletes="all", lazy="raise"
    )
