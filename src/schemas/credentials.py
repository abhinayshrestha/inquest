"""Credential configuration contracts (D026, D034), routed by
src/api/routes/credentials.py and mirrored by web/src/lib/api/credentials.ts.

Per-environment Nebius Token Factory and SigNoz credentials, set only by the
environment's owner, written and never read back: responses only ever carry
CredentialStatus. Secrets are SecretStr: they never appear in repr, logs or
model_dump(mode="json") output.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

MODEL_ID = r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$"


def _url(value: str, *, https_only: bool) -> str:
    value = value.strip()
    schemes = ("https://",) if https_only else ("https://", "http://")
    if not value.startswith(schemes) or len(value) > 500 or any(c.isspace() for c in value):
        raise ValueError("must be an https URL" if https_only else "must be an http(s) URL")
    if "@" in value.split("//", 1)[1].split("/", 1)[0]:
        # user:password@host would put a credential in the URL itself.
        raise ValueError("must not contain credentials")
    return value


class NebiusCredentialsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: SecretStr = Field(min_length=20, max_length=512)
    base_url: str = "https://api.tokenfactory.nebius.com/v1/"
    main_model: str = Field(pattern=MODEL_ID)
    local_model: str = Field(pattern=MODEL_ID)

    @field_validator("base_url")
    @classmethod
    def _https(cls, value: str) -> str:
        # The key travels in a header; never over plain HTTP (same rule as llm.py).
        return _url(value, https_only=True)


# dedicated: the organization's own SigNoz instance or Cloud workspace. provider_scoped: a
# shared SigNoz Cloud (or self-hosted Enterprise) workspace whose key is restricted to the
# ingestion keys an operator assigned to this organization (D037). The ingestion key ids
# are never chosen here.
TelemetryIsolationInput = Literal["dedicated", "provider_scoped"]


class SigNozCredentialsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str
    api_key: SecretStr = Field(min_length=8, max_length=512)
    isolation: TelemetryIsolationInput = "dedicated"

    @field_validator("base_url")
    @classmethod
    def _http(cls, value: str) -> str:
        return _url(value, https_only=False)


class CredentialStatus(BaseModel):
    """The only thing ever returned about a stored credential."""

    kind: Literal["nebius", "signoz"]
    # True only for "configured": stored and decryptable by this server's key ring.
    configured: bool
    # unavailable: stored, but this server cannot decrypt it (credential storage is not
    # configured or the row's key is not in the key ring). Fails closed, like not_configured.
    state: Literal["not_configured", "configured", "unavailable"]
    updated_at: datetime | None
    # The user id of who last set it; never the value or a prefix of the secret.
    updated_by: str | None
    # Nebius only: model ids are configuration, not secrets.
    main_model: str | None = None
    local_model: str | None = None
    # SigNoz only: how the connection is isolated from other organizations (D037).
    isolation: TelemetryIsolationInput | None = None
