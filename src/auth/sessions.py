"""Server-side sessions and pending logins.

The browser holds random tokens; the database holds their SHA-256. A session ends at
its absolute expiry, after the idle timeout, on logout, or when its user is disabled.
"""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.oidc import VerifiedIdentity
from src.auth.settings import AuthSettings
from src.models import AuthLoginState, AuthSession, User

LOGIN_STATE_MINUTES = 10
# last_seen_at is refreshed at most this often, so reads do not write on every request.
TOUCH_SECONDS = 300
MAX_RETURN_TO = 500


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def safe_return_to(value: str | None) -> str:
    """A same-app path to return to after sign-in; anything else becomes "/". Prevents open
    redirects ("//evil.example", "https://…", backslashes, control characters)."""
    if (
        not value
        or not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or any(ord(c) < 32 for c in value)
        or len(value) > MAX_RETURN_TO
    ):
        return "/"
    return value


async def create_login_state(
    session: AsyncSession, *, browser_token: str, verifier: str, nonce: str, return_to: str
) -> str:
    """Records a pending login and returns its state value (sent to the provider)."""
    now = datetime.now(UTC)
    # Expired states are dead weight; clean them opportunistically.
    await session.execute(delete(AuthLoginState).where(AuthLoginState.expires_at < now))
    state = secrets.token_urlsafe(32)
    session.add(
        AuthLoginState(
            state_hash=digest(state),
            browser_hash=digest(browser_token),
            code_verifier=verifier,
            nonce=nonce,
            return_to=safe_return_to(return_to),
            expires_at=now + timedelta(minutes=LOGIN_STATE_MINUTES),
        )
    )
    await session.commit()
    return state


async def take_login_state(
    session: AsyncSession, *, state: str, browser_token: str | None
) -> AuthLoginState | None:
    """The pending login for `state`, consumed (single use). None if unknown, expired, or
    started in another browser (login CSRF: someone else's callback link)."""
    row = await session.scalar(
        delete(AuthLoginState)
        .where(AuthLoginState.state_hash == digest(state))
        .returning(AuthLoginState)
    )
    await session.commit()
    if row is None or row.expires_at <= datetime.now(UTC):
        return None
    if browser_token is None or not secrets.compare_digest(row.browser_hash, digest(browser_token)):
        return None
    return row


async def upsert_user(session: AsyncSession, identity: VerifiedIdentity) -> User:
    now = datetime.now(UTC)
    user = await session.scalar(
        select(User).where(User.issuer == identity.issuer, User.subject == identity.subject)
    )
    if user is None:
        user = User(issuer=identity.issuer, subject=identity.subject)
        session.add(user)
    # Profile claims are refreshed at every sign-in; the identity is (issuer, subject).
    user.email = identity.email
    user.email_verified = identity.email_verified
    user.display_name = identity.name
    user.last_login_at = now
    await session.flush()
    return user


async def create_session(
    session: AsyncSession, user: User, settings: AuthSettings
) -> tuple[str, AuthSession]:
    now = datetime.now(UTC)
    token = secrets.token_urlsafe(32)
    row = AuthSession(
        user_id=user.id,
        token_hash=digest(token),
        csrf_token=secrets.token_urlsafe(32),
        last_seen_at=now,
        created_at=now,
        expires_at=now + timedelta(hours=settings.session_hours),
    )
    session.add(row)
    await session.commit()
    return token, row


async def resolve_session(
    session: AsyncSession, token: str | None, settings: AuthSettings
) -> tuple[AuthSession, User] | None:
    if not token or len(token) > 200:
        return None
    now = datetime.now(UTC)
    found = (
        await session.execute(
            select(AuthSession, User)
            .join(User, User.id == AuthSession.user_id)
            .where(AuthSession.token_hash == digest(token))
        )
    ).first()
    if found is None:
        return None
    auth, user = found
    idle_limit = auth.last_seen_at + timedelta(minutes=settings.idle_minutes)
    if auth.revoked_at or user.disabled_at or auth.expires_at <= now or idle_limit <= now:
        return None
    if (now - auth.last_seen_at).total_seconds() > TOUCH_SECONDS:
        await session.execute(
            update(AuthSession).where(AuthSession.id == auth.id).values(last_seen_at=now)
        )
        await session.commit()
    return auth, user


async def revoke_session(session: AsyncSession, session_id: uuid.UUID) -> None:
    await session.execute(
        update(AuthSession)
        .where(AuthSession.id == session_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
    await session.commit()
