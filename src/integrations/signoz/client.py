import json
import re
from typing import Any

import httpx

from src.integrations.signoz.builders import tenant_scoped
from src.integrations.signoz.errors import SigNozError, SigNozErrorCode
from src.integrations.signoz.settings import SigNozSettings

QUERY_RANGE_PATH = "/api/v5/query_range"
# Without `live` SigNoz only reports that its process is up; with it, the handler also
# pings ClickHouse and returns 503 when telemetry storage is unreachable.
HEALTH_PATH = "/api/v1/health?live"
# query_range is POST but non-mutating; nothing else in SigNoz may be called.
ALLOWED_REQUESTS = {("POST", QUERY_RANGE_PATH), ("GET", HEALTH_PATH)}

API_KEY_HEADER = "SIGNOZ-API-KEY"
CONNECT_TIMEOUT_SECONDS = 3.0
MAX_ERROR_DETAIL_CHARS = 500


def sanitize(text: str, settings: SigNozSettings) -> str:
    key = settings.api_key.get_secret_value()
    if key:
        text = text.replace(key, "[redacted]")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_ERROR_DETAIL_CHARS]


def _server_error_detail(payload: Any) -> str:
    if not isinstance(payload, dict) or not isinstance(payload.get("error"), dict):
        return ""
    error = payload["error"]
    parts = [str(error.get("code") or ""), str(error.get("message") or "")]
    # Nested errors carry the filter-syntax specifics the model needs to fix its query.
    for nested in (error.get("errors") or [])[:3]:
        if isinstance(nested, dict) and nested.get("message"):
            parts.append(str(nested["message"]))
    return ": ".join(p for p in parts if p)


def _status_error(status: int, payload: Any, settings: SigNozSettings) -> SigNozError:
    detail = sanitize(_server_error_detail(payload), settings)
    if status in (401, 403):
        code = SigNozErrorCode.AUTH
        # Authorization errors name service accounts, roles and ingestion key ids.
        detail = "not authorized"
    elif status == 429 or status >= 500:
        code = SigNozErrorCode.UNAVAILABLE
    elif 300 <= status < 400:
        code = SigNozErrorCode.MALFORMED_RESPONSE
        detail = "unexpected redirect; check SIGNOZ_BASE_URL"
    else:
        code = SigNozErrorCode.INVALID_QUERY
    return SigNozError(
        code, f"HTTP {status}" + (f": {detail}" if detail else ""), http_status=status
    )


async def _read_capped(response: httpx.Response, limit: int) -> bytes:
    # Counted after decompression so a small gzip body cannot expand past the cap.
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > limit:
            raise SigNozError(
                SigNozErrorCode.RESPONSE_TOO_LARGE,
                f"response exceeded {limit} bytes; narrow the time range or filters",
                http_status=response.status_code,
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def _send(
    settings: SigNozSettings,
    client: httpx.AsyncClient,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> tuple[int, bytes, str]:
    timeout = httpx.Timeout(settings.timeout_seconds, connect=CONNECT_TIMEOUT_SECONDS)
    async with client.stream(
        method,
        settings.base_url.rstrip("/") + path,
        json=body,
        headers={API_KEY_HEADER: settings.api_key.get_secret_value(), "Accept": "application/json"},
        timeout=timeout,
        follow_redirects=False,
    ) as response:
        raw = await _read_capped(response, settings.max_response_bytes)
        return response.status_code, raw, response.headers.get("content-type", "")


async def request_json(
    settings: SigNozSettings,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Returns the parsed JSON body of a 2xx response or raises SigNozError."""
    if (method, path) not in ALLOWED_REQUESTS:
        raise SigNozError(SigNozErrorCode.CONFIG, f"{method} {path} is not an allowed request")
    if settings.tenant_key_ids and path == QUERY_RANGE_PATH:
        # Every query of a provider-scoped connection, whatever built it (D037).
        try:
            body = tenant_scoped(body or {}, settings.tenant_key_ids)
        except ValueError as exc:
            raise SigNozError(SigNozErrorCode.CONFIG, str(exc)) from None

    try:
        if client is None:
            async with httpx.AsyncClient() as owned:
                status, raw, content_type = await _send(settings, owned, method, path, body)
        else:
            status, raw, content_type = await _send(settings, client, method, path, body)
    except httpx.TimeoutException:
        raise SigNozError(
            SigNozErrorCode.TIMEOUT, f"{path} timed out after {settings.timeout_seconds}s"
        ) from None
    except (httpx.InvalidURL, httpx.UnsupportedProtocol):
        raise SigNozError(SigNozErrorCode.CONFIG, "SIGNOZ_BASE_URL is not a valid URL") from None
    except httpx.TransportError as exc:
        # Exception text can include the host; the class name is enough to diagnose.
        raise SigNozError(
            SigNozErrorCode.UNAVAILABLE, f"{path} unreachable ({type(exc).__name__})"
        ) from None

    try:
        payload = json.loads(raw)
    except ValueError:
        payload = None

    if status >= 300:
        raise _status_error(status, payload, settings)
    # Unknown paths return the SigNoz web app's HTML with 200, so JSON must be checked.
    if "json" not in content_type or not isinstance(payload, dict):
        raise SigNozError(
            SigNozErrorCode.MALFORMED_RESPONSE, f"{path} did not return JSON", http_status=status
        )
    if payload.get("status") == "error":
        detail = sanitize(_server_error_detail(payload), settings)
        raise SigNozError(SigNozErrorCode.INVALID_QUERY, detail or "error", http_status=status)
    return payload
