"""AES-256-GCM sealing of credential payloads.

The associated data binds a ciphertext to its environment, kind, owner and key id: a row
copied to another environment or kind, or kept across an ownership change, fails
authentication instead of decrypting. Nonces are random per seal (96 bits); keys are
replaced long before random-nonce limits matter for a per-environment secret store.
"""

import hashlib
import hmac
import ipaddress
import json
import os
import posixpath
import re
import socket
import uuid
from dataclasses import dataclass, field
from urllib.parse import unquote

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from src.credentials.network import parse_destination
from src.credentials.settings import CredentialConfig

NONCE_BYTES = 12
SCHEME = "inquest.credential.v1"


class CredentialUnreadable(Exception):
    """A stored credential cannot be decrypted (its key is gone, or the row was altered or
    moved). Fail closed; the message never carries key material or plaintext."""


@dataclass(frozen=True)
class Sealed:
    key_id: str
    nonce: bytes = field(repr=False)
    ciphertext: bytes = field(repr=False)


def _aad(environment_id: uuid.UUID, kind: str, owner_user_id: uuid.UUID, key_id: str) -> bytes:
    return f"{SCHEME}|{environment_id}|{kind}|{owner_user_id}|{key_id}".encode()


def seal(
    config: CredentialConfig,
    payload: dict[str, str],
    *,
    environment_id: uuid.UUID,
    kind: str,
    owner_user_id: uuid.UUID,
) -> Sealed:
    key_id = config.active_key_id
    nonce = os.urandom(NONCE_BYTES)
    plaintext = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ciphertext = AESGCM(config.keys[key_id]).encrypt(
        nonce, plaintext, _aad(environment_id, kind, owner_user_id, key_id)
    )
    return Sealed(key_id=key_id, nonce=nonce, ciphertext=ciphertext)


def unseal(
    config: CredentialConfig,
    sealed: Sealed,
    *,
    environment_id: uuid.UUID,
    kind: str,
    owner_user_id: uuid.UUID,
) -> dict[str, str]:
    """Plaintext payload. Only for deterministic code about to make the provider call
    (or re-seal it); never log, store or return the result."""
    key = config.keys.get(sealed.key_id)
    if key is None:
        raise CredentialUnreadable("encryption key is not available")
    try:
        plaintext = AESGCM(key).decrypt(
            sealed.nonce,
            sealed.ciphertext,
            _aad(environment_id, kind, owner_user_id, sealed.key_id),
        )
        payload = json.loads(plaintext)
    except (InvalidTag, ValueError):
        raise CredentialUnreadable("stored credential failed authentication") from None
    if not isinstance(payload, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in payload.items()
    ):
        raise CredentialUnreadable("stored credential has an unexpected shape")
    return payload


_NUMERIC_V4 = re.compile(r"^(0x[0-9a-f]+|[0-9]+)(\.(0x[0-9a-f]+|[0-9]+)){0,3}$")


def _canonical_host(host: str) -> str:
    """One spelling per address: decimal, hex, octal and shortened IPv4 forms become
    dotted quads; IPv6 literals are compressed and IPv4-mapped ones become IPv4."""
    host = host.lower().strip("[]")
    if _NUMERIC_V4.match(host):
        try:
            return socket.inet_ntoa(socket.inet_aton(host))
        except OSError:
            return host
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    return f"[{address.compressed}]" if address.version == 6 else str(address)


def normalize_base_url(base_url: str) -> str:
    """The form fingerprints are computed over, so equivalent spellings of one URL match:
    scheme, canonical host and explicit port, then the path with percent-escapes decoded,
    dot segments and repeated or trailing slashes removed. Different DNS names for one
    backend still differ: a fingerprint detects reuse, it never proves isolation (D037)."""
    destination = parse_destination(base_url)
    path = posixpath.normpath(unquote(destination.path)) if destination.path else ""
    path = re.sub(r"/+", "/", path).rstrip("/").removeprefix(".")
    host = _canonical_host(destination.host)
    return f"{destination.scheme}://{host}:{destination.port}{path}"


def fingerprint(config: CredentialConfig, base_url: str) -> str:
    """Keyed, stable identifier of a telemetry connection: groups environments that query
    the same backend without storing its URL in the clear. Independent of the encryption
    keys, so it does not change when they rotate."""
    message = normalize_base_url(base_url).encode()
    return hmac.new(config.fingerprint_key, message, hashlib.sha256).hexdigest()
