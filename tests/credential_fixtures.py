"""Test helpers for per-environment credentials: key rings from the process environment
(exactly as the backend reads them), recognisable sentinel secrets to search for, stored
credentials through the real service, and a local fake SigNoz server so runtime calls go
through the real destination-guarded client."""

import asyncio
import base64
import json
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.credentials import crypto
from src.credentials.settings import load_credential_config
from src.db.session import SessionLocal
from src.models import (
    CredentialKind,
    SigNozDedicatedConnection,
    SigNozTenantKey,
    TelemetryIsolation,
    User,
)
from src.services import credentials
from tests.signoz.conftest import load_fixture


def b64(seed: bytes) -> str:
    return base64.b64encode(seed * (32 // len(seed))).decode()


KEY_1 = b64(b"k1")
KEY_2 = b64(b"k2")
FINGERPRINT_KEY = b64(b"fp")
SIGNOZ_ORIGIN = "http://signoz.test:8080"
NEBIUS_URL = "https://api.tokenfactory.nebius.com/v1/"
# Distinctive values that must never show up in a response, log, audit row or plaintext
# column.
NEBIUS_SECRET = "nbk-SENTINEL-7f3a9c1e5b2d4f6a8c0e"
SIGNOZ_SECRET = "sgz-SENTINEL-2b4d6f8a0c1e3a5c"


def configure_credentials(
    monkeypatch: pytest.MonkeyPatch,
    *,
    keys: dict[str, str] | None = None,
    active: str = "k1",
    fingerprint: str = FINGERPRINT_KEY,
    telemetry: list[str] | None = None,
    nebius: list[str] | None = None,
) -> None:
    monkeypatch.setenv(
        "INQUEST_CREDENTIAL_KEYS", json.dumps({"k1": KEY_1} if keys is None else keys)
    )
    monkeypatch.setenv("INQUEST_CREDENTIAL_ACTIVE_KEY", active)
    monkeypatch.setenv("INQUEST_CREDENTIAL_FINGERPRINT_KEY", fingerprint)
    monkeypatch.setenv(
        "INQUEST_TELEMETRY_ALLOWED_ORIGINS",
        json.dumps([SIGNOZ_ORIGIN] if telemetry is None else telemetry),
    )
    monkeypatch.setenv(
        "INQUEST_NEBIUS_ALLOWED_ORIGINS",
        json.dumps(["https://api.tokenfactory.nebius.com"] if nebius is None else nebius),
    )
    load_credential_config.cache_clear()


@pytest.fixture
def credential_storage(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    configure_credentials(monkeypatch)
    yield
    load_credential_config.cache_clear()


def allow_process_credentials(monkeypatch: pytest.MonkeyPatch, allowed: bool = True) -> None:
    """The operator's opt-in that lets granted environments run on `.env` credentials."""
    monkeypatch.setenv("INQUEST_ALLOW_PROCESS_CREDENTIALS", "true" if allowed else "false")


async def store_credentials(
    session: AsyncSession,
    user_id: uuid.UUID,
    environment_id: uuid.UUID,
    *,
    tag: str = "A",
    signoz_url: str = SIGNOZ_ORIGIN,
    kinds: tuple[CredentialKind, ...] = (CredentialKind.NEBIUS, CredentialKind.SIGNOZ),
    isolation: TelemetryIsolation = TelemetryIsolation.DEDICATED,
) -> None:
    """Stores the environment's credentials as its owner would (no provider validation,
    but the same isolation admission). Secrets are the sentinels suffixed with `tag`;
    model ids are main-{tag}/local-{tag}."""
    user = await session.get(User, user_id)
    assert user is not None
    config = load_credential_config()
    for kind in kinds:
        nebius = kind is CredentialKind.NEBIUS
        await credentials.store_credential(
            session, environment_id, user, kind,
            secret={"api_key": f"{NEBIUS_SECRET if nebius else SIGNOZ_SECRET}-{tag}",
                    "base_url": NEBIUS_URL if nebius else signoz_url},
            public={"main_model": f"main-{tag}", "local_model": f"local-{tag}"} if nebius else {},
            config=config, isolation=None if nebius else isolation,
        )  # fmt: skip


async def bind_tenant_keys(
    session: AsyncSession, organization_id: uuid.UUID, url: str, *key_ids: str
) -> None:
    """What the operator's scripts.bind_signoz_tenant does: assign ingestion key ids of a
    shared workspace to an organization."""
    fingerprint = crypto.fingerprint(load_credential_config(), url)
    session.add_all(
        SigNozTenantKey(
            organization_id=organization_id,
            connection_fingerprint=fingerprint,
            key_id=key_id,
            created_by="test operator",
        )
        for key_id in key_ids
    )
    await session.flush()


async def verify_dedicated(session: AsyncSession, organization_id: uuid.UUID, url: str) -> None:
    """What the operator's scripts.bind_signoz_tenant verify does (D038)."""
    session.add(
        SigNozDedicatedConnection(
            organization_id=organization_id,
            connection_fingerprint=crypto.fingerprint(load_credential_config(), url),
            verified_by="test operator",
        )
    )
    await session.flush()


async def store_before_d037(
    session: AsyncSession, user_id: uuid.UUID, environment_id: uuid.UUID, **kwargs: Any
) -> None:
    """Stores as the API did before D037, without the isolation admission: how
    cross-organization reuse saved back then looks now."""
    with patch.object(credentials, "_admit_connection", AsyncMock()):
        await store_credentials(session, user_id, environment_id, **kwargs)


async def store_committed(user_id: uuid.UUID, environment_id: uuid.UUID, **kwargs: Any) -> None:
    async with SessionLocal() as s:
        await store_credentials(s, user_id, environment_id, **kwargs)


@asynccontextmanager
async def signoz_server(
    *replies: Any, gate: asyncio.Event | None = None
) -> AsyncIterator[tuple[str, list[dict[str, Any]]]]:
    """A real HTTP server on loopback answering in order: a fixture name or dict is a 200
    JSON body, a (status, body, headers) tuple anything else. Yields its origin (to be
    allowlisted) and the requests it received (method, path, lower-cased headers). With a
    gate, each request is recorded and then held until the gate opens (a call in flight)."""
    seen: list[dict[str, Any]] = []
    queue = list(replies)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1").split("\r\n")
        method, path, _ = head[0].split(" ", 2)
        headers = {
            k.strip().lower(): v.strip() for k, v in (h.split(":", 1) for h in head[1:] if ":" in h)
        }
        body = await reader.readexactly(int(headers.get("content-length", "0")))
        seen.append({"method": method, "path": path, "headers": headers, "body": body})
        if gate is not None:
            await gate.wait()
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if callable(item):  # a mock provider deciding per request (CloudWorkspace)
            item = (*item(seen[-1]), {})
        status, payload, extra = item if isinstance(item, tuple) else (200, item, {})
        data = json.dumps(load_fixture(payload) if isinstance(payload, str) else payload).encode()
        lines = {"Content-Type": "application/json", "Content-Length": str(len(data)),
                 "Connection": "close", **extra}  # fmt: skip
        writer.write(
            f"HTTP/1.1 {status} X\r\n".encode()
            + "".join(f"{k}: {v}\r\n" for k, v in lines.items()).encode()
            + b"\r\n"
            + data
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        yield f"http://127.0.0.1:{port}", seen


# --- a mock SigNoz Cloud workspace with ingestion-key-scoped roles (D037) -------------------

ANY_KEY = "*"  # a role on builder_query/signoz.workspace.key.id (any key): not a boundary
KEY_CLAUSE = re.compile(
    r"^signoz\.workspace\.key\.id (?:= '([A-Za-z0-9_-]+)'|IN \(((?:'[A-Za-z0-9_-]+', )*"
    r"'[A-Za-z0-9_-]+')\))(?: AND \((.*)\))?$"
)
QUERY_OK = {"status": "success", "data": {"type": "scalar", "data": {"results": []}}}
FIXTURES = {
    ("logs", "raw"): "logs_raw.json",
    ("logs", "time_series"): "log_volume.json",
    ("traces", "raw"): "spans_raw.json",
    ("traces", "scalar"): "trace_stats_scalar.json",
    ("metrics", "time_series"): "metrics_formula.json",
}
# Text in the fixtures that each tenant's copy carries its ingestion key id in, so a test
# can tell whose telemetry an answer holds.
MARKERS = ("Out of memory", "GET /api/orders")


def forbidden() -> tuple[int, dict]:
    return 403, {"status": "error", "error": {
        "type": "forbidden", "code": "authz_forbidden",
        "message": "service account svc-tenant cannot read builder_query/signoz.workspace.key.id/x",
    }}  # fmt: skip


class CloudWorkspace:
    """What the SigNoz Cloud documentation specifies for key-scoped roles: an API key's
    role lists the ingestion key ids it may read (or None: an unrestricted managed role,
    or ANY_KEY). A scoped role admits a Query Builder query only when its filter pins
    signoz.workspace.key.id at the top level (= or IN), AND-combined, with permitted ids;
    formulas are checked through their queries; PromQL and ClickHouse SQL are denied.
    Denials are 403 authz_forbidden; unknown API keys 401. Answers carry the admitted key
    ids as markers. Records what it admitted, per API key."""

    def __init__(self, roles: dict[str, set[str] | str | None]):
        self.roles = roles
        self.admitted: list[tuple[str, tuple[str, ...]]] = []
        self.denied: list[str] = []

    def _keys(self, role: set[str] | str | None, query: dict) -> tuple[str, ...] | None:
        """The ingestion keys a query reads, or None when the role denies it."""
        kind = query.get("type")
        if kind == "builder_formula":
            return ()
        if kind != "builder_query":
            return ("*",) if role is None else None
        expression = ((query.get("spec") or {}).get("filter") or {}).get("expression") or ""
        found = KEY_CLAUSE.match(expression)
        if found is None:
            return ("*",) if role is None else None
        keys = (
            (found.group(1),)
            if found.group(1)
            else tuple(k.strip("'") for k in found.group(2).split(", "))
        )
        if role is None or role == ANY_KEY or set(keys) <= role:  # type: ignore[operator]
            return keys
        return None

    def __call__(self, request: dict[str, Any]) -> tuple[int, dict]:
        api_key = request["headers"].get("signoz-api-key", "")
        if api_key not in self.roles:
            return 401, {"status": "error", "error": {"code": "unauthenticated"}}
        role = self.roles[api_key]
        body = json.loads(request["body"] or b"{}")
        queries = body.get("compositeQuery", {}).get("queries", [])
        read: set[str] = set()
        for query in queries:
            keys = self._keys(role, query)
            if keys is None:
                self.denied.append(api_key)
                return forbidden()
            read.update(keys)
        self.admitted.append((api_key, tuple(sorted(read))))
        signals = {q["spec"].get("signal") for q in queries if q.get("type") == "builder_query"}
        name = FIXTURES.get((next(iter(signals), None), body.get("requestType")))
        if name is None:
            return 200, QUERY_OK
        text = json.dumps(load_fixture(name))
        tag = ",".join(sorted(read))
        for marker in MARKERS:
            text = text.replace(marker, f"{marker} [tenant {tag}]")
        return 200, json.loads(text)

    def handler(self, request: "httpx.Request") -> "httpx.Response":
        """The same workspace behind an httpx.MockTransport (credential validation)."""
        status, payload = self({
            "headers": {k.lower(): v for k, v in request.headers.items()},
            "body": request.content,
        })  # fmt: skip
        return httpx.Response(status, json=payload)
