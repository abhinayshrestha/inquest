"""Hosted entrypoint: `uv run python -m src.serve` (D041, docs/deployment.md).

Runs the public API listener with settings validated here rather than passed as uvicorn
flags, because two of them carry security weight. Forwarded client addresses (H3 rate
limits) are honoured only from INQUEST_TRUSTED_PROXIES, never from uvicorn's
FORWARDED_ALLOW_IPS default or "*". And there is exactly one worker: per-process limits
(D039) and the in-process runner (D020, D036) are documented per process, so extra workers
would silently multiply them. The MCP listener is started by the app itself (loopback
only). Development keeps `uv run poe dev`.
"""

import ipaddress
import os
import sys

import uvicorn
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.config import get_settings

MAX_HEADER_BYTES = 16 * 1024


class ServeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="INQUEST_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Loopback unless the proxy runs elsewhere; then a private address only the proxy
    # can reach (the network, not this setting, keeps everyone else out).
    listen_host: str = "127.0.0.1"
    listen_port: int = Field(default=8000, ge=1, le=65535)
    # Addresses (or networks) of the reverse proxy, which must overwrite X-Forwarded-For.
    trusted_proxies: list[str] = []
    graceful_shutdown_seconds: int = Field(default=30, ge=1, le=600)


def serve_problems(settings: ServeSettings, app_env: str) -> list[str]:
    problems = []
    for entry in settings.trusted_proxies:
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            problems.append(f"INQUEST_TRUSTED_PROXIES entry {entry[:64]!r} is not an address")
            continue
        if network.prefixlen == 0:
            problems.append("INQUEST_TRUSTED_PROXIES must name the proxy, not every address")
    if app_env == "production" and not settings.trusted_proxies:
        # A hosted server is always behind the TLS proxy; without this every client would
        # share the proxy's rate-limit budget.
        problems.append("INQUEST_TRUSTED_PROXIES is not set")
    if os.environ.get("WEB_CONCURRENCY", "1") != "1":
        problems.append("WEB_CONCURRENCY is set: run one process per container (D020)")
    return problems


def serve_config(settings: ServeSettings) -> uvicorn.Config:
    return uvicorn.Config(
        "src.main:app",
        host=settings.listen_host,
        port=settings.listen_port,
        workers=1,
        # An explicit list, so uvicorn never falls back to FORWARDED_ALLOW_IPS or its
        # loopback default. It reads X-Forwarded-For (rightmost untrusted hop) and
        # X-Forwarded-Proto from trusted peers only; Forwarded and X-Real-IP never.
        proxy_headers=bool(settings.trusted_proxies),
        forwarded_allow_ips=settings.trusted_proxies,
        server_header=False,
        h11_max_incomplete_event_size=MAX_HEADER_BYTES,
        # Shorter than the proxy's upstream keepalive would be a race; nginx keeps 4 s.
        timeout_keep_alive=5,
        timeout_graceful_shutdown=settings.graceful_shutdown_seconds,
    )


def main() -> None:
    settings = ServeSettings()
    if problems := serve_problems(settings, get_settings().app_env):
        sys.exit(f"refusing to start: {'; '.join(problems)}")
    uvicorn.Server(serve_config(settings)).run()


if __name__ == "__main__":
    main()
