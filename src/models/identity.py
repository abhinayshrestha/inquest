"""Identity and organizations (Phase 7A, D030/D031).

Users come from an OIDC provider and are keyed by (issuer, subject); email is a mutable
attribute, never the identity. Sessions and login states store only hashes of the values
given to the browser. Organizations, memberships and invitations are the tenant model;
environments and investigations are not linked to them yet (legacy local workspace, 7B).
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base, UUIDPrimaryKeyMixin, str_enum


class OrganizationRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


class User(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("issuer", "subject", name="uq_users_issuer_subject"),)

    issuer: Mapped[str] = mapped_column(String(500))
    subject: Mapped[str] = mapped_column(String(255))
    # Lowercased; only trusted for invitations when email_verified is true.
    email: Mapped[str | None] = mapped_column(String(320), index=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    display_name: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # A disabled user's sessions stop working at once.
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserAdmission(UUIDPrimaryKeyMixin, Base):
    """Permission to use Inquest at all, separate from signing in (D042). Enforced only
    under INQUEST_AUTH_ADMISSION=approval. An operator approves a provider-verified email
    in advance (user_id empty); the first sign-in with that verified email binds the row to
    that (issuer, subject) for good, so a later account with the same address gets nothing.
    Accepting an invitation at sign-in creates a bound row. Withdrawal (revoked_at) blocks
    sign-in and sessions; memberships and environment ownership are left as they are."""

    __tablename__ = "user_admissions"
    __table_args__ = (
        CheckConstraint("email IS NOT NULL OR user_id IS NOT NULL", name="has_target"),
        CheckConstraint("source IN ('operator', 'invitation')", name="source_known"),
        # One live pre-approval per address waiting for its first sign-in.
        Index(
            "uq_user_admissions_one_pending_email",
            "email",
            unique=True,
            postgresql_where=text("user_id IS NULL AND revoked_at IS NULL"),
        ),
    )

    # Lowercased verified email the operator approved; kept after binding as a record.
    email: Mapped[str | None] = mapped_column(String(320))
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    source: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuthSession(UUIDPrimaryKeyMixin, Base):
    """A signed-in browser. The cookie holds a random token; only its SHA-256 is stored, so
    a database read does not yield usable sessions."""

    __tablename__ = "auth_sessions"
    __table_args__ = (CheckConstraint("expires_at > created_at", name="expires_after_creation"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    # Sent to the frontend (GET /auth/session) and required as X-CSRF-Token on writes.
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuthLoginState(UUIDPrimaryKeyMixin, Base):
    """One pending OIDC login: PKCE verifier, nonce and where to return. Single use, short
    lived, bound to the starting browser through a hashed pre-login cookie."""

    __tablename__ = "auth_login_states"

    state_hash: Mapped[str] = mapped_column(String(64), unique=True)
    browser_hash: Mapped[str] = mapped_column(String(64))
    code_verifier: Mapped[str] = mapped_column(String(128))
    nonce: Mapped[str] = mapped_column(String(128))
    return_to: Mapped[str] = mapped_column(String(500))
    # SHA-256 of an invitation token the sign-in was started with (D042): accepted at the
    # callback by a verified identity that is not admitted yet. Never the token itself.
    invitation_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class Organization(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "organizations"
    __table_args__ = (
        CheckConstraint("char_length(name) BETWEEN 1 AND 120", name="name_length"),
        CheckConstraint("slug ~ '^[a-z0-9][a-z0-9-]{0,59}$'", name="slug_format"),
    )

    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(60), unique=True)
    # RESTRICT: organizations outlive nothing they reference, but history needs the creator.
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OrganizationMembership(Base):
    __tablename__ = "organization_memberships"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    role: Mapped[OrganizationRole] = mapped_column(str_enum(OrganizationRole))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OrganizationInvitation(UUIDPrimaryKeyMixin, Base):
    """An invitation to one email address. The link carries a random token; only its hash
    is stored. Accepting, revoking and expiry are final: a token works at most once."""

    __tablename__ = "organization_invitations"
    __table_args__ = (
        # Invitations never grant ownership; owners are made by role changes.
        CheckConstraint("role IN ('admin', 'member')", name="role_not_owner"),
        CheckConstraint("NOT (accepted_at IS NOT NULL AND revoked_at IS NOT NULL)", name="one_end"),
        Index(
            "uq_organization_invitations_one_pending",
            "organization_id",
            "email",
            unique=True,
            postgresql_where=text("accepted_at IS NULL AND revoked_at IS NULL"),
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    email: Mapped[str] = mapped_column(String(320))
    role: Mapped[OrganizationRole] = mapped_column(str_enum(OrganizationRole))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    invited_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
