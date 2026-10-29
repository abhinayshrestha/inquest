"""Destination policy for owner-chosen provider URLs (SSRF, D034): parsing, address
vetting at validation time and again at connection time (DNS rebinding), allowlists,
https, no redirects, no proxies."""

import socket

import httpcore
import httpx
import pytest

from src.credentials import network
from src.credentials.network import (
    DestinationNotAllowed,
    GuardedBackend,
    address_allowed,
    guarded_client,
    parse_destination,
    vet_destination,
)

PUBLIC = "93.184.216.34"
TIMEOUT = httpx.Timeout(2.0)


def resolver_of(*answers: list[str]):
    """Answers successive resolutions in turn: the first for the validation-time check,
    the next for the connection, as a rebinding DNS server would."""
    queue = list(answers)
    calls: list[tuple[str, int]] = []

    async def resolve(host: str, port: int) -> list[str]:
        calls.append((host, port))
        return queue.pop(0) if len(queue) > 1 else queue[0]

    resolve.calls = calls  # type: ignore[attr-defined]
    return resolve


async def never(host: str, port: int) -> list[str]:
    raise AssertionError("must not resolve")


class Inner(httpcore.AsyncNetworkBackend):
    """Records connections instead of opening sockets."""

    def __init__(self):
        self.connected: list[tuple[str, int]] = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):  # noqa: ASYNC109
        self.connected.append((host, port))
        raise httpcore.ConnectError("recorded")

    async def sleep(self, seconds: float) -> None:
        pass


# --- parsing ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "ftp://signoz.example.com",
        "https://",
        "https://user:pass@signoz.example.com",
        "https://token@signoz.example.com",
        "https://signoz.example.com/?next=x",
        "https://signoz.example.com/#frag",
        "https://signoz.example.com\\@127.0.0.1/",
        "https://signoz.example.com\t/",
        "https://signoz.example.com:99999/",
        "javascript:alert(1)",
    ],
)
def test_rejected_urls(url):
    with pytest.raises(DestinationNotAllowed) as exc:
        parse_destination(url)
    assert "example" not in str(exc.value)


def test_parsing_normalizes():
    d = parse_destination(" https://SigNoz.Example.COM./v1/ ")
    assert (d.host, d.port, d.path, d.origin) == (
        "signoz.example.com", 443, "/v1/", "https://signoz.example.com:443",
    )  # fmt: skip
    assert parse_destination("http://[::1]:8080").origin == "http://[::1]:8080"


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254", "100.64.0.1",
        "0.0.0.0", "224.0.0.1", "255.255.255.255", "::1", "::", "fe80::1", "fc00::1",
        "::ffff:127.0.0.1", "::ffff:169.254.169.254", "64:ff9b::7f00:1", "2002:7f00:1::",
        "2001::1", "ff02::1", "fe80::1%eth0", "not-an-ip",
    ],
)  # fmt: skip
def test_private_and_reserved_addresses_are_refused(address):
    assert not address_allowed(address)


@pytest.mark.parametrize("address", [PUBLIC, "8.8.8.8", "2606:4700:4700::1111"])
def test_public_addresses_are_allowed(address):
    assert address_allowed(address)


# --- validation-time vetting -----------------------------------------------------------------


async def test_allowlisted_origin_is_exact_and_not_resolved():
    allowed = frozenset({"http://signoz.test:8080"})
    await vet_destination(
        parse_destination("http://SIGNOZ.test:8080/"), allowed, allowlisted_only=False,
        resolver=never,
    )  # fmt: skip
    # Another port of the same host is not the allowlisted origin.
    with pytest.raises(DestinationNotAllowed):
        await vet_destination(
            parse_destination("http://signoz.test:8081"), allowed, allowlisted_only=False,
            resolver=never,
        )  # fmt: skip


async def test_allowlisted_only_refuses_everything_else():
    with pytest.raises(DestinationNotAllowed):
        await vet_destination(
            parse_destination("https://evil.example.com/v1/"),
            frozenset({"https://api.tokenfactory.nebius.com:443"}),
            allowlisted_only=True,
            resolver=never,
        )


async def test_public_endpoints_need_https():
    with pytest.raises(DestinationNotAllowed, match="https"):
        await vet_destination(
            parse_destination("http://signoz.example.com"), frozenset(), allowlisted_only=False,
            resolver=resolver_of([PUBLIC]),
        )  # fmt: skip


@pytest.mark.parametrize("answer", [["10.0.0.5"], [PUBLIC, "127.0.0.1"], []])
async def test_private_mixed_or_empty_resolution_is_refused(answer):
    with pytest.raises(DestinationNotAllowed) as exc:
        await vet_destination(
            parse_destination("https://signoz.example.com"), frozenset(), allowlisted_only=False,
            resolver=resolver_of(answer),
        )  # fmt: skip
    assert "10.0.0.5" not in str(exc.value) and "127.0.0.1" not in str(exc.value)


async def test_public_resolution_passes():
    await vet_destination(
        parse_destination("https://signoz.example.com"), frozenset(), allowlisted_only=False,
        resolver=resolver_of([PUBLIC]),
    )  # fmt: skip


async def test_unresolvable_host_is_an_os_error():
    async def fail(host, port):
        raise socket.gaierror("no such host")

    with pytest.raises(OSError):
        await vet_destination(
            parse_destination("https://nope.example.com"), frozenset(), allowlisted_only=False,
            resolver=fail,
        )  # fmt: skip


# --- connection-time enforcement -------------------------------------------------------------


async def test_rebinding_to_a_private_address_is_refused_at_connect():
    resolve = resolver_of([PUBLIC], ["127.0.0.1"])
    destination = parse_destination("https://signoz.example.com")
    await vet_destination(destination, frozenset(), allowlisted_only=False, resolver=resolve)
    inner = Inner()
    backend = GuardedBackend(frozenset(), allowlisted_only=False, resolver=resolve, inner=inner)
    with pytest.raises(DestinationNotAllowed):
        await backend.connect_tcp("signoz.example.com", 443)
    assert inner.connected == []


async def test_connects_to_the_vetted_address_not_the_hostname():
    inner = Inner()
    backend = GuardedBackend(
        frozenset(), allowlisted_only=False, resolver=resolver_of([PUBLIC, "8.8.8.8"]), inner=inner
    )
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("signoz.example.com", 443)
    # Each vetted address is tried; the hostname is never handed to a resolver again.
    assert inner.connected == [(PUBLIC, 443), ("8.8.8.8", 443)]


async def test_allowlisted_pair_connects_as_given_and_others_are_refused_when_exclusive():
    inner = Inner()
    backend = GuardedBackend(
        frozenset({("localhost", 8080)}), allowlisted_only=True, resolver=never, inner=inner
    )
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("LOCALHOST", 8080)
    assert inner.connected == [("LOCALHOST", 8080)]
    with pytest.raises(DestinationNotAllowed):
        await backend.connect_tcp("localhost", 8081)
    with pytest.raises(DestinationNotAllowed):
        await backend.connect_unix_socket("/var/run/docker.sock")


async def test_resolution_failure_at_connect_is_a_connect_error():
    async def fail(host, port):
        raise socket.gaierror("no such host")

    backend = GuardedBackend(frozenset(), allowlisted_only=False, resolver=fail, inner=Inner())
    with pytest.raises(httpcore.ConnectError) as exc:
        await backend.connect_tcp("nope.example.com", 443)
    assert "nope" not in str(exc.value)


# --- the client itself -----------------------------------------------------------------------


async def test_guarded_client_blocks_private_destinations_end_to_end(monkeypatch):
    # A proxy from the environment would resolve the host itself; it must be ignored.
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    async with guarded_client(
        frozenset(), allowlisted_only=False, timeout=TIMEOUT, resolver=resolver_of(["127.0.0.1"])
    ) as client:
        assert client.trust_env is False and client.follow_redirects is False
        with pytest.raises(DestinationNotAllowed):
            await client.get("https://signoz.example.com/api/v1/health")


async def test_guarded_client_exclusive_mode_blocks_unlisted_hosts():
    async with guarded_client(
        frozenset({"https://api.tokenfactory.nebius.com:443"}),
        allowlisted_only=True,
        timeout=TIMEOUT,
        resolver=never,
    ) as client:
        with pytest.raises(DestinationNotAllowed):
            await client.get("https://169.254.169.254/latest/meta-data/")


def test_client_is_refused_without_the_backend_hook(monkeypatch):
    class Bare:
        def __init__(self, **kwargs):
            pass

    monkeypatch.setattr(network.httpx, "AsyncHTTPTransport", Bare)
    with pytest.raises(RuntimeError, match="destination guard"):
        guarded_client(frozenset(), allowlisted_only=False, timeout=TIMEOUT)
