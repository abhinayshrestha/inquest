"""Nemotron chat models served by Nebius Token Factory (OpenAI-compatible API).

Models only reason; they never receive credentials. The API key stays in a SecretStr
and is handed to the client library directly, so it is never logged or put in prompts.
Settings come from `.env` (process-wide, legacy and granted environments) or from an
environment's own resolved credentials (nebius_settings); never a mix of the two keys.
"""

import httpx
from langchain_openai import ChatOpenAI
from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.credentials.resolver import Credentials


class NebiusSettings(BaseSettings):
    """Separate from core Settings so the app and DB tests start without Nebius configured."""

    model_config = SettingsConfigDict(
        env_prefix="NEBIUS_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    api_key: SecretStr
    base_url: str = "https://api.tokenfactory.nebius.com/v1/"
    # Optional so the smoke test can list the catalog before models are chosen.
    main_model: str | None = None
    local_model: str | None = None
    timeout_seconds: float = Field(default=120.0, gt=0)
    # Transient-error retries inside the client; graph-level retries come later.
    max_retries: int = Field(default=2, ge=0, le=5)
    max_output_tokens: int = Field(default=4096, gt=0)
    # Nebius counts reasoning toward max_tokens; Ultra's reasoning plus a full report did
    # not fit in 4096 in the first live run.
    main_max_output_tokens: int = Field(default=16_384, gt=0)


class NebiusConfigError(Exception):
    pass


def load_nebius_settings() -> NebiusSettings:
    try:
        settings = NebiusSettings()  # type: ignore[call-arg]
    except ValidationError as exc:
        # Field names only; pydantic's message would echo the provided values.
        fields = sorted({f"NEBIUS_{'_'.join(map(str, e['loc'])).upper()}" for e in exc.errors()})
        raise NebiusConfigError(f"invalid or missing settings: {', '.join(fields)}") from None
    if not settings.base_url.startswith("https://"):
        # The API key travels in a header; never send it over plain HTTP.
        raise NebiusConfigError("NEBIUS_BASE_URL must start with https://")
    return settings


def nebius_settings(credentials: Credentials) -> NebiusSettings:
    """The environment's key, endpoint and models; tuning (timeouts, retries, output
    limits) still comes from `.env`. Process-wide credentials load `.env` entirely."""
    if credentials.source == "process":
        return load_nebius_settings()
    own = credentials.nebius
    return NebiusSettings(
        api_key=SecretStr(own["api_key"]),
        base_url=own["base_url"],
        main_model=own["main_model"],
        local_model=own["local_model"],
    )


class _NoSyncRequests(httpx.BaseTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        # Runs only make async calls; a sync client would bypass the destination guard.
        raise RuntimeError("synchronous provider requests are not allowed")


def chat_model(
    model: str,
    settings: NebiusSettings,
    *,
    temperature: float = 0.0,
    max_tokens: int | None = None,
    http_async_client: httpx.AsyncClient | None = None,
) -> ChatOpenAI:
    guarded = (
        {
            "http_async_client": http_async_client,
            "http_client": httpx.Client(transport=_NoSyncRequests()),
        }
        if http_async_client is not None
        else {}
    )
    return ChatOpenAI(
        model=model,
        api_key=settings.api_key,
        base_url=settings.base_url,
        temperature=temperature,
        max_tokens=max_tokens or settings.max_output_tokens,
        timeout=settings.timeout_seconds,
        max_retries=settings.max_retries,
        # Nebius serves Chat Completions; the Responses API is OpenAI-only.
        use_responses_api=False,
        # Token usage on streamed responses too, for budgets.
        stream_usage=True,
        **guarded,
    )


def main_model(
    settings: NebiusSettings | None = None, http_async_client: httpx.AsyncClient | None = None
) -> ChatOpenAI:
    settings = settings or load_nebius_settings()
    if not settings.main_model:
        raise NebiusConfigError("NEBIUS_MAIN_MODEL is not set")
    return chat_model(
        settings.main_model,
        settings,
        max_tokens=settings.main_max_output_tokens,
        http_async_client=http_async_client,
    )


def local_model(
    settings: NebiusSettings | None = None, http_async_client: httpx.AsyncClient | None = None
) -> ChatOpenAI:
    settings = settings or load_nebius_settings()
    if not settings.local_model:
        raise NebiusConfigError("NEBIUS_LOCAL_MODEL is not set")
    return chat_model(settings.local_model, settings, http_async_client=http_async_client)
