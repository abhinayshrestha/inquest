import uuid
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint, text
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
    __table_args__ = (
        # Names are unique per organization; ownerless legacy rows (NULL) stay unique among
        # themselves, as before.
        UniqueConstraint(
            "organization_id",
            "name",
            name="uq_environments_organization_id_name",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(
            "(organization_id IS NULL) = (owner_user_id IS NULL)", name="owner_with_organization"
        ),
    )

    name: Mapped[str] = mapped_column(String(255))
    type: Mapped[EnvironmentType] = mapped_column(str_enum(EnvironmentType))
    status: Mapped[EnvironmentStatus] = mapped_column(
        str_enum(EnvironmentStatus),
        default=EnvironmentStatus.ACTIVE,
        server_default=EnvironmentStatus.ACTIVE.value,
    )
    # NULL for ownerless records from before tenancy (D032): quarantined, never served by
    # the API, and moved only by the audited operator command (scripts.adopt_legacy).
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), index=True
    )
    # The authenticated creator (D026). Only the owner will configure credentials (7C).
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    # Granted only by an operator (scripts.adopt_legacy --allow-process-credentials): this
    # environment may run with the backend's process-wide development credentials. Tenant
    # environments never get them implicitly; without a grant (and until 7C) they cannot
    # run investigations.
    process_credentials: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    # `metadata` is reserved by SQLAlchemy Declarative.
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )

    # passive_deletes="all": let the RESTRICT FK reject the delete instead of the ORM
    # trying to NULL out a non-nullable environment_id.
    assignments: Mapped[list["InvestigationAssignment"]] = relationship(
        back_populates="environment", passive_deletes="all", lazy="raise"
    )


def can_use_process_credentials(environment: Environment) -> bool:
    """Whether runs may use the backend's process-wide development credentials (.env):
    ownerless legacy environments, reachable only by host-side tooling (D032), and
    environments an operator explicitly granted them. Never other tenant environments."""
    return environment.organization_id is None or environment.process_credentials
