"""Nemotron chat models served by Nebius Token Factory (OpenAI-compatible API).

Models only reason; they never receive credentials. The API key stays in a SecretStr
and is handed to the client library directly, so it is never logged or put in prompts.
"""

from langchain_openai import ChatOpenAI
from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


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


def chat_model(model: str, settings: NebiusSettings, *, temperature: float = 0.0) -> ChatOpenAI:
    return ChatOpenAI(
        model=model,
        api_key=settings.api_key,
        base_url=settings.base_url,
        temperature=temperature,
        max_tokens=settings.max_output_tokens,
        timeout=settings.timeout_seconds,
        max_retries=settings.max_retries,
        # Nebius serves Chat Completions; the Responses API is OpenAI-only.
        use_responses_api=False,
        # Token usage on streamed responses too, for budgets.
        stream_usage=True,
    )


def main_model(settings: NebiusSettings | None = None) -> ChatOpenAI:
    settings = settings or load_nebius_settings()
    if not settings.main_model:
        raise NebiusConfigError("NEBIUS_MAIN_MODEL is not set")
    return chat_model(settings.main_model, settings)


def local_model(settings: NebiusSettings | None = None) -> ChatOpenAI:
    settings = settings or load_nebius_settings()
    if not settings.local_model:
        raise NebiusConfigError("NEBIUS_LOCAL_MODEL is not set")
    return chat_model(settings.local_model, settings)
