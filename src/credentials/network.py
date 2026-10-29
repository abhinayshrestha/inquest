"""Destination policy for provider endpoints chosen by environment owners (SSRF defence).

An owner picks the URL the backend will call with their credentials. It is accepted only
as an exact operator-allowlisted origin or, for telemetry, as a public https endpoint.
The check runs at validation (parse, then resolve) and again on every connection: the
connection resolves the host itself, vets every address and opens the socket to a vetted
address, so DNS rebinding between check and connect cannot redirect it. TLS still
verifies the hostname. Redirects are never followed; proxy environment variables are
ignored (a proxy would resolve the host instead of us).
"""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpcore
import httpx

from src.credentials.settings import origin_of

Resolver = Callable[[str, int], Awaitable[list[str]]]

# Prefixes that embed or translate to IPv4 (NAT64, 6to4, Teredo): an address in them can
# reach a private IPv4 host even though it looks like public IPv6.
TRANSLATED_V6 = [
    ipaddress.ip_network(n) for n in ("64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32")
]


class DestinationNotAllowed(Exception):
    """The URL, or an address it resolves to, is outside the destination policy. The
    message is generic: it never echoes the URL or the resolved addresses."""


@dataclass(frozen=True)
class Destination:
    scheme: str
    host: str
    port: int
    path: str

    @property
    def origin(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.scheme}://{host}:{self.port}"


def parse_destination(url: str) -> Destination:
    url = url.strip()
    if any(c.isspace() or ord(c) < 32 or c == "\\" for c in url):
        raise DestinationNotAllowed("URL contains characters that are not allowed")
    origin = origin_of(url)
    if origin is None:
        raise DestinationNotAllowed("URL must be http(s) with a host")
    parts = urlsplit(url)
    if parts.username is not None or parts.password is not None:
        raise DestinationNotAllowed("URL must not contain credentials")
    if parts.query or parts.fragment:
        raise DestinationNotAllowed("URL must not contain a query or fragment")
    host = (parts.hostname or "").rstrip(".")
    try:
        # The HTTP client must see the same host we check; a parser disagreement is how
        # allowlist checks get bypassed.
        same = httpx.URL(url).host.rstrip(".").lower() == host
    except httpx.InvalidURL:
        same = False
    if not same:
        raise DestinationNotAllowed("URL host is ambiguous")
    port = int(origin.rsplit(":", 1)[1])
    return Destination(scheme=parts.scheme, host=host, port=port, path=parts.path)


def address_allowed(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif any(ip in network for network in TRANSLATED_V6):
            return False
    # is_global excludes private, loopback, link-local (cloud metadata), shared, reserved
    # and unspecified ranges.
    return ip.is_global and not ip.is_multicast


async def resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def _host_port(origin: str) -> tuple[str, int]:
    destination = parse_destination(origin)
    return destination.host, destination.port


def _vet(addresses: Iterable[str]) -> list[str]:
    found = list(addresses)
    # Every answer must be public: a mixed answer lets the client pick the private one.
    if not found or not all(address_allowed(a) for a in found):
        raise DestinationNotAllowed("endpoint resolves to a private or reserved address")
    return found


async def vet_destination(
    destination: Destination,
    allowed_origins: frozenset[str],
    *,
    allowlisted_only: bool,
    resolver: Resolver | None = None,
) -> None:
    """Validation-time check. Raises DestinationNotAllowed, or OSError when the host does
    not resolve. Connections are checked again by GuardedBackend."""
    if destination.origin in allowed_origins:
        return
    if allowlisted_only:
        raise DestinationNotAllowed("endpoint is not on this server's allowlist")
    if destination.scheme != "https":
        raise DestinationNotAllowed("endpoint must use https")
    _vet(await (resolver or resolve)(destination.host, destination.port))


class GuardedBackend(httpcore.AsyncNetworkBackend):
    """Connection-time enforcement (httpcore's network backend interface). Allowlisted
    host:port pairs connect as given; anything else is resolved here and connected by
    vetted address only, or refused when only allowlisted destinations are permitted."""

    def __init__(
        self,
        allowed: frozenset[tuple[str, int]],
        *,
        allowlisted_only: bool,
        resolver: Resolver | None = None,
        inner: httpcore.AsyncNetworkBackend | None = None,
    ):
        self._allowed = allowed
        self._allowlisted_only = allowlisted_only
        self._resolver = resolver or resolve
        self._inner = inner or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 (httpcore's interface)
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        if (host.strip("[]").rstrip(".").lower(), port) in self._allowed:
            return await self._inner.connect_tcp(host, port, timeout, local_address, socket_options)
        if self._allowlisted_only:
            raise DestinationNotAllowed("endpoint is not on this server's allowlist")
        try:
            async with asyncio.timeout(timeout):
                addresses = await self._resolver(host, port)
        except (OSError, TimeoutError) as exc:
            raise httpcore.ConnectError(f"resolution failed ({type(exc).__name__})") from None
        failure: Exception | None = None
        for address in _vet(addresses):
            try:
                return await self._inner.connect_tcp(
                    address, port, timeout, local_address, socket_options
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                failure = exc
        assert failure is not None
        raise failure

    async def connect_unix_socket(self, *args: Any, **kwargs: Any) -> httpcore.AsyncNetworkStream:
        raise DestinationNotAllowed("unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def guarded_client(
    allowed_origins: frozenset[str],
    *,
    allowlisted_only: bool,
    timeout: httpx.Timeout,
    resolver: Resolver | None = None,
) -> httpx.AsyncClient:
    transport = httpx.AsyncHTTPTransport(trust_env=False, retries=0)
    pool = getattr(transport, "_pool", None)
    # httpx has no public hook for the network backend. Verified with httpx 0.28 and
    # httpcore 1.0 (pinned in pyproject.toml): AsyncHTTPTransport keeps an
    # httpcore.AsyncConnectionPool in `_pool`, and the pool opens every connection through
    # `_network_backend.connect_tcp` (TLS is layered on the returned stream, so the
    # certificate is still checked against the URL's hostname). Refuse to build a client
    # at all rather than send owner credentials without the connection-time guard.
    if not isinstance(pool, httpcore.AsyncConnectionPool) or not hasattr(pool, "_network_backend"):
        raise RuntimeError("HTTP transport does not support the destination guard")
    pool._network_backend = GuardedBackend(
        frozenset(_host_port(o) for o in allowed_origins),
        allowlisted_only=allowlisted_only,
        resolver=resolver,
    )
    return httpx.AsyncClient(
        transport=transport, trust_env=False, follow_redirects=False, timeout=timeout
    )
