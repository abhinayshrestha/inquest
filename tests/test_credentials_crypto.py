"""Credential key ring and AES-GCM sealing (D034): fail-closed configuration, tamper and
binding checks, fingerprints that survive encryption-key rotation."""

import dataclasses
import json
import uuid

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from src.credentials import crypto
from src.credentials.settings import (
    CredentialStorageUnavailable,
    load_credential_config,
    optional_credential_config,
)
from tests.credential_fixtures import (
    KEY_1,
    KEY_2,
    NEBIUS_SECRET,
    b64,
    configure_credentials,
)

ENV, OWNER = uuid.uuid4(), uuid.uuid4()
PAYLOAD = {"api_key": NEBIUS_SECRET, "base_url": "https://api.tokenfactory.nebius.com/v1/"}


@pytest.fixture(autouse=True)
def _clear_cache():
    yield
    load_credential_config.cache_clear()


def _config(monkeypatch, **kw):
    configure_credentials(monkeypatch, **kw)
    return load_credential_config()


def _seal(config, **kw):
    binding = {"environment_id": ENV, "kind": "nebius", "owner_user_id": OWNER} | kw
    return crypto.seal(config, PAYLOAD, **binding), binding


# --- key ring configuration ------------------------------------------------------------------


def test_valid_key_ring(monkeypatch):
    config = _config(monkeypatch, keys={"k1": KEY_1, "k2": KEY_2}, active="k2")
    assert config.active_key_id == "k2" and set(config.keys) == {"k1", "k2"}
    # Key material is never in repr (logs, tracebacks).
    assert KEY_1 not in repr(config) and "keys=" not in repr(config)


LEAKY = b64(b"LEAK")[:-4] + "LEAK"  # recognisable, not 32 bytes


@pytest.mark.parametrize(
    "kw, names",
    [
        ({"keys": {}}, "INQUEST_CREDENTIAL_KEYS"),
        ({"keys": {"k1": "not base64!"}}, "INQUEST_CREDENTIAL_KEYS"),
        ({"keys": {"k1": LEAKY}}, "INQUEST_CREDENTIAL_KEYS"),
        ({"keys": {"bad id": KEY_1}}, "INQUEST_CREDENTIAL_KEYS"),
        ({"active": "k9"}, "INQUEST_CREDENTIAL_ACTIVE_KEY"),
        ({"fingerprint": ""}, "INQUEST_CREDENTIAL_FINGERPRINT_KEY"),
        ({"fingerprint": LEAKY}, "INQUEST_CREDENTIAL_FINGERPRINT_KEY"),
        # Independent keys: a fingerprint key equal to an encryption key is refused.
        ({"fingerprint": KEY_1}, "INQUEST_CREDENTIAL_FINGERPRINT_KEY"),
        ({"telemetry": ["http://signoz.test:8080/path"]}, "INQUEST_TELEMETRY_ALLOWED_ORIGINS"),
        ({"telemetry": ["ftp://signoz.test"]}, "INQUEST_TELEMETRY_ALLOWED_ORIGINS"),
        ({"nebius": ["http://api.tokenfactory.nebius.com"]}, "INQUEST_NEBIUS_ALLOWED_ORIGINS"),
    ],
)
def test_invalid_configuration_fails_closed_naming_settings_only(monkeypatch, kw, names):
    configure_credentials(monkeypatch, **kw)
    with pytest.raises(CredentialStorageUnavailable) as exc:
        load_credential_config()
    assert names in str(exc.value)
    assert "LEAK" not in str(exc.value) and KEY_1 not in str(exc.value)
    assert optional_credential_config() is None


def test_malformed_json_names_setting_not_value(monkeypatch):
    configure_credentials(monkeypatch)
    monkeypatch.setenv("INQUEST_CREDENTIAL_KEYS", '{"k1": "LEAK' + "x" * 40)
    with pytest.raises(CredentialStorageUnavailable) as exc:
        load_credential_config()
    assert "INQUEST_CREDENTIAL_KEYS" in str(exc.value) and "LEAK" not in str(exc.value)


# --- sealing ---------------------------------------------------------------------------------


def test_round_trip_with_fresh_nonces(monkeypatch):
    config = _config(monkeypatch)
    first, binding = _seal(config)
    second, _ = _seal(config)
    assert first.key_id == "k1" and first.nonce != second.nonce
    assert NEBIUS_SECRET.encode() not in first.ciphertext
    assert NEBIUS_SECRET not in repr(first) and "nonce" not in repr(first)
    assert crypto.unseal(config, first, **binding) == PAYLOAD


@pytest.mark.parametrize(
    "change",
    [
        {"environment_id": uuid.uuid4()},
        {"kind": "signoz"},
        # Credentials of a previous owner never decrypt for the next one.
        {"owner_user_id": uuid.uuid4()},
    ],
)
def test_ciphertext_is_bound_to_environment_kind_and_owner(monkeypatch, change):
    config = _config(monkeypatch)
    sealed, binding = _seal(config)
    with pytest.raises(crypto.CredentialUnreadable):
        crypto.unseal(config, sealed, **(binding | change))


def test_relabelled_key_id_fails(monkeypatch):
    # Same key material under two ids: the id is authenticated too.
    config = _config(monkeypatch, keys={"k1": KEY_1, "k2": KEY_1})
    sealed, binding = _seal(config)
    with pytest.raises(crypto.CredentialUnreadable):
        crypto.unseal(config, dataclasses.replace(sealed, key_id="k2"), **binding)


@pytest.mark.parametrize("field", ["ciphertext", "nonce"])
def test_tampering_fails(monkeypatch, field):
    config = _config(monkeypatch)
    sealed, binding = _seal(config)
    value = bytearray(getattr(sealed, field))
    value[0] ^= 1
    with pytest.raises(crypto.CredentialUnreadable) as exc:
        crypto.unseal(config, dataclasses.replace(sealed, **{field: bytes(value)}), **binding)
    assert NEBIUS_SECRET not in str(exc.value)


def test_missing_key_is_unreadable_not_a_fallback(monkeypatch):
    old = _config(monkeypatch)
    sealed, binding = _seal(old)
    new = _config(monkeypatch, keys={"k2": KEY_2}, active="k2")
    with pytest.raises(crypto.CredentialUnreadable, match="not available"):
        crypto.unseal(new, sealed, **binding)


def test_non_object_plaintext_is_unreadable(monkeypatch):
    config = _config(monkeypatch)
    sealed, binding = _seal(config)
    aad = crypto._aad(ENV, "nebius", OWNER, "k1")
    forged = AESGCM(config.keys["k1"]).encrypt(sealed.nonce, json.dumps([1]).encode(), aad)
    with pytest.raises(crypto.CredentialUnreadable):
        crypto.unseal(config, dataclasses.replace(sealed, ciphertext=forged), **binding)


# --- fingerprints ----------------------------------------------------------------------------


def test_fingerprint_survives_encryption_key_rotation(monkeypatch):
    before = _config(monkeypatch)
    after = _config(monkeypatch, keys={"k1": KEY_1, "k2": KEY_2}, active="k2")
    url = "https://signoz.example.com/"
    assert crypto.fingerprint(before, url) == crypto.fingerprint(after, url)
    assert len(crypto.fingerprint(before, url)) == 64
    assert "signoz" not in crypto.fingerprint(before, url)


def test_fingerprint_normalizes_and_distinguishes(monkeypatch):
    config = _config(monkeypatch)
    same = [
        "https://SigNoz.Example.com",
        "https://signoz.example.com:443/",
        "https://signoz.example.com.",
    ]
    assert len({crypto.fingerprint(config, u) for u in same}) == 1
    assert crypto.fingerprint(config, "https://signoz.example.com") != crypto.fingerprint(
        config, "https://signoz.example.com:8443"
    )
    other = _config(monkeypatch, fingerprint=b64(b"f2"))
    assert crypto.fingerprint(config, same[0]) != crypto.fingerprint(other, same[0])
