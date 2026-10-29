import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, str_enum


class CredentialKind(StrEnum):
    NEBIUS = "nebius"
    SIGNOZ = "signoz"


class TelemetryIsolation(StrEnum):
    """How a SigNoz connection keeps one organization's telemetry from another's (D037).

    dedicated: the organization's own SigNoz instance or Cloud workspace; its own
    authorization is the boundary, and Inquest refuses a connection another organization
    already uses. provider_scoped: a shared SigNoz Cloud (or self-hosted Enterprise)
    workspace whose API key is restricted to the ingestion keys an operator bound to the
    organization; every query carries those key ids and the provider rejects any other."""

    DEDICATED = "dedicated"
    PROVIDER_SCOPED = "provider_scoped"


class EnvironmentCredential(TimestampMixin, Base):
    """One provider credential of an environment, set by its owner (D026, D034).

    The secret part (API key and base URL) exists only as AES-GCM ciphertext bound to
    (environment, kind, owner, key id); its key is in the backend's environment, never in
    the database. A separate table, so loading an Environment never loads ciphertext.
    """

    __tablename__ = "environment_credentials"
    __table_args__ = (
        # Credentials belong to the owner who set them: the environment's owner can only
        # change once its credentials are deleted (they are never handed over, D029).
        ForeignKeyConstraint(
            ["environment_id", "owner_user_id"],
            ["environments.id", "environments.owner_user_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("octet_length(nonce) = 12", name="nonce_length"),
        CheckConstraint(
            "(kind = 'signoz') = (connection_fingerprint IS NOT NULL)",
            name="fingerprint_for_signoz",
        ),
        CheckConstraint("(kind = 'signoz') = (isolation IS NOT NULL)", name="isolation_for_signoz"),
    )

    environment_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    kind: Mapped[CredentialKind] = mapped_column(str_enum(CredentialKind), primary_key=True)
    owner_user_id: Mapped[uuid.UUID] = mapped_column()
    key_id: Mapped[str] = mapped_column(String(32))
    nonce: Mapped[bytes] = mapped_column(LargeBinary)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    # Non-secret configuration shown to members: Nebius model ids.
    public_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    # Keyed HMAC of the SigNoz base URL (src/credentials/crypto.fingerprint).
    connection_fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    # SigNoz only (D037).
    isolation: Mapped[TelemetryIsolation | None] = mapped_column(str_enum(TelemetryIsolation))
    updated_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )

    def __repr__(self) -> str:
        return f"EnvironmentCredential({self.environment_id}, {self.kind})"


class SigNozTenantKey(UUIDPrimaryKeyMixin, Base):
    """An ingestion key id of a shared SigNoz workspace that an operator assigned to one
    organization (D037). Provider-scoped connections query with these ids only; nobody in
    the organization chooses them. A key id belongs to at most one organization per
    workspace (connection fingerprint)."""

    __tablename__ = "signoz_tenant_keys"
    __table_args__ = (
        UniqueConstraint("connection_fingerprint", "key_id"),
        CheckConstraint("key_id ~ '^[A-Za-z0-9_-]{1,64}$'", name="key_id_format"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    connection_fingerprint: Mapped[str] = mapped_column(String(64))
    key_id: Mapped[str] = mapped_column(String(64))
    # The operator who bound it (scripts.bind_signoz_tenant), for the audit trail.
    created_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SigNozDedicatedConnection(UUIDPrimaryKeyMixin, Base):
    """An operator's verification that a SigNoz connection (fingerprint) belongs to one
    organization alone (D038). Outside local development a dedicated connection is usable
    only with one: a URL an owner typed proves nothing, since other DNS names can reach the
    same backend. One organization per connection."""

    __tablename__ = "signoz_dedicated_connections"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    connection_fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    # The operator who verified it (scripts.bind_signoz_tenant), for the audit trail.
    verified_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
