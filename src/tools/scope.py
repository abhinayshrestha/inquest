"""Environment identity scoping, the v1 isolation mechanism.

Every query is restricted to the identities (host.name or service.name values) its
environment claims, chosen per signal. This keeps investigators inside their environment
on a shared SigNoz; it is not a production multi-tenancy boundary, since it trusts that
telemetry carries honest, unique identity attributes. Anything ambiguous fails closed.
"""

from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from src.integrations.signoz.builders import required_identity
from src.integrations.signoz.schemas import MAX_SCOPE_VALUES, Identity, Name, Scope
from src.tools.policy import Rejection

SCOPE_KEY = "signoz"


class EnvironmentScope(BaseModel):
    """Environment.metadata_json["signoz"]."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    hosts: list[Name] = Field(default_factory=list, max_length=MAX_SCOPE_VALUES)
    services: list[Name] = Field(default_factory=list, max_length=MAX_SCOPE_VALUES)
    # Needed only when a service name is shared with another environment.
    deployment_environment: Name | None = None

    @model_validator(mode="after")
    def _check(self) -> "EnvironmentScope":
        for name, values in (("hosts", self.hosts), ("services", self.services)):
            if len(set(values)) != len(values):
                raise ValueError(f"duplicate {name}")
        return self

    def values(self, identity: Identity) -> list[str]:
        return self.hosts if identity == "host" else self.services


def parse_scope(metadata: Any) -> EnvironmentScope:
    raw = metadata.get(SCOPE_KEY) if isinstance(metadata, dict) else None
    if raw is None:
        raise Rejection(
            "policy.scope_missing",
            "environment has no SigNoz scope configured; telemetry queries are disabled",
        )
    try:
        return EnvironmentScope.model_validate(raw)
    except ValidationError:
        raise Rejection("policy.scope_invalid", "environment SigNoz scope is invalid") from None


def claims(metadata: Any, identity: Identity) -> tuple[set[str], str | None]:
    # Best effort on other environments: a malformed config still claims what it names,
    # so a typo elsewhere cannot make an overlapping identity look unambiguous.
    raw = metadata.get(SCOPE_KEY) if isinstance(metadata, dict) else None
    if not isinstance(raw, dict):
        return set(), None
    values = raw.get("hosts" if identity == "host" else "services")
    names = {v for v in values if isinstance(v, str)} if isinstance(values, list) else set()
    deployment = raw.get("deployment_environment")
    return names, deployment if isinstance(deployment, str) else None


def resolve_scope(req: BaseModel, own: EnvironmentScope, others: Iterable[Any]) -> Scope:
    """The mandatory restriction for this request. `others` are the metadata_json of every
    other active environment."""
    identity = required_identity(req)
    allowed = own.values(identity)
    if not allowed:
        raise Rejection(
            "policy.scope_missing_identity",
            f"environment has no authorized {identity} identities; this query cannot be "
            "isolated to it",
        )

    host, service = getattr(req, "host", None), getattr(req, "service", None)
    narrow, foreign = (host, service) if identity == "host" else (service, host)
    if foreign is not None:
        other = "service" if identity == "host" else "host"
        raise Rejection(
            "policy.identity_mismatch",
            f"this query is scoped by {identity}; narrowing by {other} is not allowed",
        )
    if narrow is not None and narrow not in allowed:
        raise Rejection(
            "policy.out_of_scope", f"{identity} {narrow!r} is not authorized for this environment"
        )

    values = (narrow,) if narrow is not None else tuple(allowed)
    deployment = own.deployment_environment if identity == "service" else None
    for metadata in others:
        claimed, their_deployment = claims(metadata, identity)
        overlap = claimed.intersection(values)
        if not overlap:
            continue
        # Same service name, told apart by deployment.environment on both sides.
        if deployment and their_deployment and deployment != their_deployment:
            continue
        raise Rejection(
            "policy.scope_ambiguous",
            f"{identity} {min(overlap)!r} is also claimed by another active environment",
        )
    return Scope(identity=identity, values=values, deployment_environment=deployment)
