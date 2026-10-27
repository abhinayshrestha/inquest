"""A fake OIDC provider and a signed-in test client, for authentication tests.

The provider is real enough to exercise the whole flow: discovery, JWKS, an RSA-signed
ID token and a token endpoint that checks the authorization code, redirect URI and PKCE
verifier. Tests can make it misbehave (wrong audience, nonce, issuer, key, algorithm).
"""

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = "https://idp.test"
CLIENT_ID = "inquest-test"
CLIENT_SECRET = "test-client-secret-value"
PUBLIC_URL = "https://app.test"


def _key() -> tuple[Any, dict[str, Any]]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key(), as_dict=True)
    return private, public


@dataclass
class FakeProvider:
    private_key: Any = None
    jwk: dict[str, Any] = field(default_factory=dict)
    # code -> (nonce, code_challenge, redirect_uri, claims)
    codes: dict[str, tuple[str, str, str, dict[str, Any]]] = field(default_factory=dict)
    token_overrides: dict[str, Any] = field(default_factory=dict)
    header_overrides: dict[str, Any] = field(default_factory=dict)
    sign_with: Any = None
    token_requests: int = 0

    def __post_init__(self) -> None:
        self.private_key, self.jwk = _key()
        self.jwk |= {"kid": "k1", "use": "sig", "alg": "RS256"}

    def authorize(self, authorize_url: str, **claims: Any) -> str:
        """What the provider does after the user signs in: issue a code for this request."""
        query = {k: v[0] for k, v in parse_qs(urlsplit(authorize_url).query).items()}
        assert query["response_type"] == "code" and query["code_challenge_method"] == "S256"
        code = secrets.token_urlsafe(16)
        self.codes[code] = (
            query["nonce"],
            query["code_challenge"],
            query["redirect_uri"],
            {"sub": "user-1", "email": "ada@example.com", "email_verified": True, "name": "Ada"}
            | claims,
        )
        return code

    def _id_token(self, nonce: str, claims: dict[str, Any]) -> str:
        now = int(time.time())
        payload = {
            "iss": ISSUER, "aud": CLIENT_ID, "iat": now, "exp": now + 300, "nonce": nonce,
        } | claims | self.token_overrides  # fmt: skip
        headers = {"kid": "k1"} | self.header_overrides
        alg = headers.pop("alg", "RS256")
        key = self.sign_with if self.sign_with is not None else self.private_key
        return jwt.encode(payload, key, algorithm=alg, headers=headers)

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == f"{ISSUER}/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/authorize",
                    "token_endpoint": f"{ISSUER}/token",
                    "jwks_uri": f"{ISSUER}/jwks",
                    "code_challenge_methods_supported": ["S256"],
                },
            )
        if url == f"{ISSUER}/jwks":
            return httpx.Response(200, json={"keys": [self.jwk]})
        if url == f"{ISSUER}/token" and request.method == "POST":
            self.token_requests += 1
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            expected = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
            if request.headers.get("authorization") != f"Basic {expected}":
                return httpx.Response(401, json={"error": "invalid_client"})
            entry = self.codes.pop(form.get("code", ""), None)
            if entry is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            nonce, challenge, redirect_uri, claims = entry
            verifier = form.get("code_verifier", "")
            digest = hashlib.sha256(verifier.encode()).digest()
            actual = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
            if actual != challenge or form.get("redirect_uri") != redirect_uri:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"id_token": self._id_token(nonce, claims)})
        return httpx.Response(404)
