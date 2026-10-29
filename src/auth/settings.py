from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.config import get_settings

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class AuthNotConfigured(Exception):
    """Sign-in is unavailable. There is no fallback identity: protected routes fail closed."""


class AuthSettings(BaseSettings):
    """INQUEST_AUTH_* in the environment or .env. Optional like the SigNoz/Nebius settings,
    so the app and its tests start without a provider; sign-in then answers 503."""

    model_config = SettingsConfigDict(
        env_prefix="INQUEST_AUTH_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    issuer: str
    client_id: str = Field(min_length=1)
    client_secret: SecretStr = Field(min_length=1)
    scopes: str = "openid email profile"
    # Where the browser reaches the app (the frontend origin). Redirects after sign-in go
    # here, the OIDC redirect URI is derived from it, and unsafe requests must come from it.
    public_url: str = "http://localhost:5173"
    # Path under public_url that reaches GET /auth/callback (through the dev proxy).
    callback_path: str = "/api/auth/callback"
    session_hours: int = Field(default=12, ge=1, le=168)
    idle_minutes: int = Field(default=120, ge=5, le=1440)
    # Only for plain-http local development; refused for any other public_url.
    cookie_secure: bool = True
    # Who may use Inquest after signing in (D042). "approval": operator-approved users and
    # invited teammates only; required when hosted. "open": every verified sign-in, the
    # default only for local development and tests. Resolved by load_auth_settings.
    admission: Literal["open", "approval"] | None = None

    @field_validator("admission", mode="before")
    @classmethod
    def _empty_is_unset(cls, value: object) -> object:
        return value or None  # .env.example leaves it empty

    @property
    def origin(self) -> str:
        parts = urlsplit(self.public_url)
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def redirect_uri(self) -> str:
        return self.origin + self.callback_path


def _local(url: str) -> bool:
    return (urlsplit(url).hostname or "") in LOCAL_HOSTS


@lru_cache
def load_auth_settings() -> AuthSettings:
    try:
        settings = AuthSettings()  # type: ignore[call-arg]
    except ValidationError as exc:
        # Field names only; pydantic's message would echo values, including the secret.
        names = {"_".join(map(str, e["loc"])).upper() for e in exc.errors()}
        fields = sorted(f"INQUEST_AUTH_{n}" for n in names)
        raise AuthNotConfigured(f"missing or invalid settings: {', '.join(fields)}") from None
    for name, url in (("ISSUER", settings.issuer), ("PUBLIC_URL", settings.public_url)):
        scheme = urlsplit(url).scheme
        if scheme != "https" and not (scheme == "http" and _local(url)):
            raise AuthNotConfigured(f"INQUEST_AUTH_{name} must use https (http only for localhost)")
    if not settings.cookie_secure and not _local(settings.public_url):
        raise AuthNotConfigured("INQUEST_AUTH_COOKIE_SECURE=false is only allowed for localhost")
    if not settings.callback_path.startswith("/"):
        raise AuthNotConfigured("INQUEST_AUTH_CALLBACK_PATH must start with /")
    # Unrestricted sign-up is never what a hosted server gets by omission.
    hosted = get_settings().app_env == "production"
    if settings.admission is None and hosted:
        raise AuthNotConfigured("INQUEST_AUTH_ADMISSION must be set to approval when hosted")
    if settings.admission == "open" and hosted:
        raise AuthNotConfigured("INQUEST_AUTH_ADMISSION=open is not allowed when hosted")
    return settings.model_copy(update={"admission": settings.admission or "open"})


def get_auth_settings() -> AuthSettings:
    """FastAPI dependency; raises AuthNotConfigured (mapped to 503 by the auth routes)."""
    return load_auth_settings()
