import base64
import binascii
import re
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict, SettingsError

KEY_BYTES = 32
KEY_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
DEFAULT_PORTS = {"http": 80, "https": 443}


class CredentialStorageUnavailable(Exception):
    """Credentials cannot be stored or decrypted on this server. Fail closed: nothing is
    saved, nothing falls back to process-wide credentials. Messages name settings only."""


class CredentialSettings(BaseSettings):
    """INQUEST_* in the environment or .env. Optional, so the app starts without them;
    credential writes then answer 503 and stored credentials read as unavailable."""

    model_config = SettingsConfigDict(
        env_prefix="INQUEST_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # JSON object {key_id: base64 of 32 random bytes}. Old keys stay listed until
    # scripts.rotate_credential_key has re-encrypted every row with the active one.
    credential_keys: dict[str, SecretStr] = {}
    credential_active_key: str = ""
    # Independent of the encryption keys, so connection fingerprints survive their rotation.
    credential_fingerprint_key: SecretStr = SecretStr("")
    # Exact origins (scheme://host[:port]). Nebius endpoints must be listed; telemetry
    # origins listed here may be http or private (operator decision, e.g. local SigNoz).
    nebius_allowed_origins: list[str] = ["https://api.tokenfactory.nebius.com"]
    telemetry_allowed_origins: list[str] = []
    # Which organizations may use each allowlisted telemetry origin outside local
    # development (D037): {origin: [organization ids]}. An allowlist entry is a network
    # decision, not tenant authorization; without a grant a hosted server refuses it.
    telemetry_origin_organizations: dict[str, list[uuid.UUID]] = {}


@dataclass(frozen=True)
class CredentialConfig:
    active_key_id: str
    keys: dict[str, bytes] = field(repr=False)
    fingerprint_key: bytes = field(repr=False)
    nebius_origins: frozenset[str]
    telemetry_origins: frozenset[str]
    # Normalized allowlisted origin -> organizations granted it (hosted servers only).
    telemetry_origin_grants: dict[str, frozenset[uuid.UUID]] = field(default_factory=dict)


def origin_of(url: str) -> str | None:
    """scheme://host:port, lowercased with the default port made explicit; None when the
    URL has no http(s) scheme or host."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in DEFAULT_PORTS or not parts.hostname:
        return None
    host = parts.hostname.rstrip(".")
    if ":" in host:
        host = f"[{host}]"
    return f"{parts.scheme}://{host}:{port or DEFAULT_PORTS[parts.scheme]}"


def _key(value: SecretStr) -> bytes | None:
    try:
        raw = base64.b64decode(value.get_secret_value(), validate=True)
    except (binascii.Error, ValueError):
        return None
    return raw if len(raw) == KEY_BYTES else None


def _origins(entries: list[str], name: str, *, https_only: bool) -> frozenset[str]:
    found = set()
    for entry in entries:
        normalized = origin_of(entry)
        parts = urlsplit(entry.strip())
        bare = not (parts.path.strip("/") or parts.query or parts.fragment or parts.username)
        if normalized is None or not bare or (https_only and parts.scheme != "https"):
            raise CredentialStorageUnavailable(
                f"{name} entries must be {'https ' if https_only else ''}origins"
            )
        found.add(normalized)
    return frozenset(found)


@lru_cache
def load_credential_config() -> CredentialConfig:
    try:
        settings = CredentialSettings()  # type: ignore[call-arg]
    except ValidationError as exc:
        # Field names only; pydantic's message would echo the provided keys.
        names = {str(e["loc"][0]).upper() for e in exc.errors() if e["loc"]}
        fields = sorted(f"INQUEST_{n}" for n in names) or ["INQUEST_CREDENTIAL_*"]
        raise CredentialStorageUnavailable(f"invalid settings: {', '.join(fields)}") from None
    except SettingsError as exc:
        # Unparseable JSON; the message names the field, the chained error is dropped.
        found = re.search(r'field "(\w+)"', str(exc))
        name = f"INQUEST_{found.group(1).upper()}" if found else "INQUEST_CREDENTIAL_*"
        raise CredentialStorageUnavailable(f"invalid settings: {name}") from None
    keys: dict[str, bytes] = {}
    for key_id, value in settings.credential_keys.items():
        key = _key(value)
        if not KEY_ID.match(key_id) or key is None:
            raise CredentialStorageUnavailable(
                "INQUEST_CREDENTIAL_KEYS must map ids [A-Za-z0-9_-] to base64 32-byte keys"
            )
        keys[key_id] = key
    if not keys:
        raise CredentialStorageUnavailable("INQUEST_CREDENTIAL_KEYS is not set")
    if settings.credential_active_key not in keys:
        raise CredentialStorageUnavailable(
            "INQUEST_CREDENTIAL_ACTIVE_KEY must name a key in INQUEST_CREDENTIAL_KEYS"
        )
    fingerprint_key = _key(settings.credential_fingerprint_key)
    if fingerprint_key is None:
        raise CredentialStorageUnavailable(
            "INQUEST_CREDENTIAL_FINGERPRINT_KEY must be a base64 32-byte key"
        )
    if fingerprint_key in keys.values():
        raise CredentialStorageUnavailable(
            "INQUEST_CREDENTIAL_FINGERPRINT_KEY must differ from every encryption key"
        )
    telemetry_origins = _origins(
        settings.telemetry_allowed_origins, "INQUEST_TELEMETRY_ALLOWED_ORIGINS", https_only=False
    )
    grants: dict[str, frozenset[uuid.UUID]] = {}
    for origin, organizations in settings.telemetry_origin_organizations.items():
        normalized = origin_of(origin)
        if normalized is None or normalized not in telemetry_origins:
            raise CredentialStorageUnavailable(
                "INQUEST_TELEMETRY_ORIGIN_ORGANIZATIONS keys must be entries of "
                "INQUEST_TELEMETRY_ALLOWED_ORIGINS"
            )
        grants[normalized] = frozenset(organizations)
    return CredentialConfig(
        active_key_id=settings.credential_active_key,
        keys=keys,
        fingerprint_key=fingerprint_key,
        nebius_origins=_origins(
            settings.nebius_allowed_origins, "INQUEST_NEBIUS_ALLOWED_ORIGINS", https_only=True
        ),
        telemetry_origins=telemetry_origins,
        telemetry_origin_grants=grants,
    )


def optional_credential_config() -> CredentialConfig | None:
    try:
        return load_credential_config()
    except CredentialStorageUnavailable:
        return None
