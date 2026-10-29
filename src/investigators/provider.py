"""The chat model for one Main or Local model step, built from the credentials resolved for
that step (D034).

Resolved again for every step, so a rotated credential is used from the next step on and
a deleted, unreadable or no longer allowed one blocks the next provider call. Nothing is
cached across steps or shared between environments, and the model lives only in the
step: never in graph state or checkpoints. Main resolves through its investigation's
environment; each Local Investigator through its own assignment's environment, never
Main's.
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

import httpx
from langchain_core.language_models import BaseChatModel

from src.credentials import resolver
from src.credentials.network import guarded_client
from src.credentials.resolver import CredentialRefused, Credentials
from src.db.session import SessionLocal
from src.investigators.llm import local_model, main_model, nebius_settings
from src.models import Environment


@asynccontextmanager
async def _model(
    credentials: Credentials, role: Literal["main", "local"], override: BaseChatModel | None
) -> AsyncIterator[BaseChatModel]:
    # Overrides (tests, host scripts) replace only the model, never the readiness check.
    if override is not None:
        yield override
        return
    build = main_model if role == "main" else local_model
    if credentials.source == "process":
        yield build()
        return
    assert credentials.config is not None
    settings = nebius_settings(credentials)
    # Exact allowlisted origins only, checked again when connecting.
    async with guarded_client(
        credentials.config.nebius_origins,
        allowlisted_only=True,
        timeout=httpx.Timeout(settings.timeout_seconds),
    ) as client:
        yield build(settings, http_async_client=client)


@asynccontextmanager
async def investigation_model(
    investigation_id: uuid.UUID, override: BaseChatModel | None = None
) -> AsyncIterator[BaseChatModel]:
    """Main's model. Raises CredentialRefused."""
    async with SessionLocal() as s:
        credentials = await resolver.investigation_credentials(s, investigation_id)
    async with _model(credentials, "main", override) as model:
        yield model


@asynccontextmanager
async def environment_model(
    environment_id: uuid.UUID, override: BaseChatModel | None = None
) -> AsyncIterator[BaseChatModel]:
    """A Local Investigator's model. Raises CredentialRefused."""
    async with SessionLocal() as s:
        environment = await s.get(Environment, environment_id)
        if environment is None:
            raise CredentialRefused("configuration_required")
        credentials = await resolver.environment_credentials(s, environment)
    async with _model(credentials, "local", override) as model:
        yield model
