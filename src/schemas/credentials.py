"""Planned credential configuration contracts (D026). Not routed.

Per-environment Nebius Token Factory and SigNoz credentials, set only by the
environment's owner, written once and never read back. No endpoint accepts these yet:
without authentication anyone could set or replace them, and there is no encrypted
storage (D027). The models exist so the validation rules are fixed and tested now and
mirrored by the frontend form schema (web/src/lib/api/credentials.ts).

Secrets are SecretStr: they never appear in repr, logs or model_dump(mode="json")
output, and responses only ever carry CredentialStatus.
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


class SigNozCredentialsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str
    api_key: SecretStr = Field(min_length=8, max_length=512)

    @field_validator("base_url")
    @classmethod
    def _http(cls, value: str) -> str:
        return _url(value, https_only=False)


class CredentialStatus(BaseModel):
    """The only thing ever returned about a stored credential."""

    kind: Literal["nebius", "signoz"]
    configured: bool
    updated_at: datetime | None
    # A user id once users exist; never the value or a prefix of the secret.
    updated_by: str | None
