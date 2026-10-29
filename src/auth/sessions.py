"""Server-side sessions and pending logins.

The browser holds random tokens; the database holds their SHA-256. A session ends at
its absolute expiry, after the idle timeout, on logout, on sign-out everywhere, or when
its user is disabled (or, under approval, loses admission). Only the user's own actions
move the idle deadline (D043): writes and the activity signal the page sends on real
input. Reads, polling and event streams never do, so an unattended open tab still goes
idle.
"""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.oidc import VerifiedIdentity
from src.auth.settings import AuthSettings
from src.models import AuthLoginState, AuthSession, User, UserAdmission
from src.services import limits

LOGIN_STATE_MINUTES = 10
# last_seen_at is refreshed at most this often, so activity does not write on every request;
# the idle timeout can therefore end a session up to this much early.
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


class SessionRefused(Exception):
    """The user was disabled or lost admission while signing in."""


def admitted(user_id) -> object:
    """SQL condition: the user holds a live admission (D042, approval policy)."""
    return exists().where(UserAdmission.user_id == user_id, UserAdmission.revoked_at.is_(None))


async def create_login_state(
    session: AsyncSession,
    *,
    browser_token: str,
    verifier: str,
    nonce: str,
    return_to: str,
    invitation_token: str | None = None,
) -> str:
    """Records a pending login and returns its state value (sent to the provider).
    QuotaExceeded while the server holds the maximum of pending logins: anyone can start
    one, so their number is bounded, counted under a lock so concurrent starts cannot
    overshoot."""
    now = datetime.now(UTC)
    await limits.lock(session, "logins", "server")
    # Expired states are dead weight; clean them opportunistically.
    await session.execute(delete(AuthLoginState).where(AuthLoginState.expires_at < now))
    pending, oldest = (
        await session.execute(select(func.count(), func.min(AuthLoginState.expires_at)))
    ).one()
    if pending >= limits.quota("pending_logins"):
        await session.rollback()
        raise limits.QuotaExceeded("pending_logins", int((oldest - now).total_seconds()) + 1)
    state = secrets.token_urlsafe(32)
    session.add(
        AuthLoginState(
            state_hash=digest(state),
            browser_hash=digest(browser_token),
            code_verifier=verifier,
            nonce=nonce,
            return_to=safe_return_to(return_to),
            invitation_hash=digest(invitation_token) if invitation_token else None,
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
    """A new session; beyond `sessions_per_user` live ones, the user's oldest end, so
    repeated sign-ins cannot pile up usable sessions. Disabling, withdrawal and sign-out
    everywhere hold the same per-user lock, and the user is re-read under it: a sign-in
    racing them never leaves a session behind that a later re-enable or restore would
    bring back to life. SessionRefused then."""
    now = datetime.now(UTC)
    await limits.lock(session, "user", user.id)
    current = select(User.id).where(User.id == user.id, User.disabled_at.is_(None))
    if settings.admission == "approval":
        current = current.where(admitted(User.id))
    if await session.scalar(current) is None:
        await session.rollback()
        raise SessionRefused
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
    await session.flush()
    beyond = (
        select(AuthSession.id)
        .where(
            AuthSession.user_id == user.id,
            AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > now,
        )
        .order_by(AuthSession.created_at.desc(), AuthSession.id)
        .offset(limits.quota("sessions_per_user"))
    )
    await session.execute(
        update(AuthSession).where(AuthSession.id.in_(beyond)).values(revoked_at=now)
    )
    await session.commit()
    return token, row


async def resolve_session(
    session: AsyncSession, token: str | None, settings: AuthSettings
) -> tuple[AuthSession, User] | None:
    if not token or len(token) > 200:
        return None
    now = datetime.now(UTC)
    query = (
        select(AuthSession, User)
        .join(User, User.id == AuthSession.user_id)
        .where(AuthSession.token_hash == digest(token))
    )
    if settings.admission == "approval":
        # Withdrawing an admission ends its sessions at their next request.
        query = query.where(admitted(User.id))
    found = (await session.execute(query)).first()
    if found is None:
        return None
    auth, user = found
    idle_limit = auth.last_seen_at + timedelta(minutes=settings.idle_minutes)
    if auth.revoked_at or user.disabled_at or auth.expires_at <= now or idle_limit <= now:
        return None
    return auth, user


async def record_activity(session: AsyncSession, auth: AuthSession) -> None:
    """Moves the idle deadline: only for the user's own actions (authenticated, CSRF-checked
    writes), never for reads, polls or streams. Cannot revive an ended session."""
    now = datetime.now(UTC)
    if (now - auth.last_seen_at).total_seconds() > TOUCH_SECONDS:
        await session.execute(
            update(AuthSession)
            .where(AuthSession.id == auth.id, AuthSession.revoked_at.is_(None))
            .values(last_seen_at=now)
        )
        await session.commit()


async def revoke_session(session: AsyncSession, session_id: uuid.UUID) -> None:
    await session.execute(
        update(AuthSession)
        .where(AuthSession.id == session_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
    await session.commit()


async def revoke_user_sessions(session: AsyncSession, user_id: uuid.UUID) -> int:
    """Ends every live session of the user, in the caller's transaction, under the per-user
    lock that session creation holds: a sign-in finishing concurrently lands either before
    (and is ended) or after (a new sign-in). Returns how many were live."""
    await limits.lock(session, "user", user_id)
    now = datetime.now(UTC)
    ended = await session.execute(
        update(AuthSession)
        .where(
            AuthSession.user_id == user_id,
            AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > now,
        )
        .values(revoked_at=now)
    )
    return ended.rowcount
