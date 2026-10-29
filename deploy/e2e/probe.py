"""External client of the local deployment simulation (run.sh). Runs on the simulated
internet (the edge network) with only the test CA: what any outsider can reach and do.

Client "a" walks the whole public surface; client "b", from another address, then checks
that a's exhausted rate limit is a's alone. Exit status 1 on any failed check.
"""

import json
import os
import re
import socket
import ssl
import sys
import threading
import time
import uuid
from urllib.parse import parse_qs, urlsplit

import httpx

HOST = "inquest.test"
BASE = f"https://{HOST}"
CA = "/run/test-ca.pem"
# Addresses an outsider must not reach: the API listener, the private MCP listener (on
# every address of the app container), and PostgreSQL. run.sh passes the app's
# addresses, egress included.
PRIVATE = [
    *(
        (address, port)
        for address in os.environ.get("APP_ADDRESSES", "172.30.10.3 172.30.20.3").split()
        for port in (8000, 8001)
    ),
    ("172.30.20.2", 5432),
]
HEADERS = (
    "strict-transport-security",
    "content-security-policy",
    "x-content-type-options",
    "x-frame-options",
    "referrer-policy",
)
results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: object = "") -> bool:
    results.append((ok, name))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if not ok and detail else ""))
    return ok


def tls() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=CA)


def client(**kwargs) -> httpx.Client:
    return httpx.Client(verify=tls(), timeout=10, follow_redirects=False, **kwargs)


def unreachable(address: str, port: int) -> bool:
    try:
        socket.create_connection((address, port), timeout=3).close()
    except OSError:
        return True
    return False


def secure_headers(response: httpx.Response) -> bool:
    return all(h in response.headers for h in HEADERS)


def raw_exchange(payload: bytes, wait: float) -> tuple[bytes, float]:
    """Sends `payload` over TLS and returns what the proxy answers before closing."""
    start = time.monotonic()
    with socket.create_connection((HOST, 443), timeout=wait) as sock:
        with tls().wrap_socket(sock, server_hostname=HOST) as conn:
            conn.sendall(payload)
            received = b""
            try:
                while chunk := conn.recv(4096):
                    received += chunk
            except (TimeoutError, ssl.SSLError, ConnectionError):
                pass
    return received, time.monotonic() - start


# --- transport and front door -------------------------------------------------------------


def front_door() -> None:
    with httpx.Client(timeout=10, follow_redirects=False) as plain:
        redirect = plain.get(f"http://{HOST}/api/health?x=1")
        forged = plain.get(f"http://{HOST}/", headers={"host": "evil.test"})
    check(
        "HTTP redirects to HTTPS",
        redirect.status_code == 301
        and redirect.headers.get("location") == f"{BASE}/api/health?x=1",
        redirect.headers.get("location"),
    )
    check(
        "the redirect never targets the request's Host",
        forged.headers.get("location") == f"{BASE}/",
        forged.headers.get("location"),
    )

    for name, server_name in (("unknown SNI", "other.test"), ("direct IP", None)):
        try:
            with socket.create_connection((HOST, 443), timeout=5) as sock:
                tls_ctx = ssl.create_default_context(cafile=CA)
                tls_ctx.check_hostname = False
                tls_ctx.wrap_socket(sock, server_hostname=server_name).close()
            refused = False
        except (ssl.SSLError, ConnectionError):
            refused = True
        check(f"TLS handshake refused for {name}", refused)

    with client() as c:
        page = c.get(f"{BASE}/")
        route = c.get(f"{BASE}/environments/123")
        api = c.get(f"{BASE}/api/health")
        missing = c.get(f"{BASE}/api/no-such-route")
        explorer = [c.get(f"{BASE}/api/{p}").status_code for p in ("docs", "openapi.json")]
        theme = c.get(f"{BASE}/theme.js")
        script = re.search(r'src="(/assets/[^"]+\.js)"', page.text)
        asset = c.get(f"{BASE}{script.group(1)}") if script else None
        forged_host = c.get(f"{BASE}/api/health", headers={"host": "127.0.0.1:8001"})
    check("HTTPS serves the frontend", page.status_code == 200 and '<div id="root">' in page.text)
    check("client-side routes fall back to index.html", route.status_code == 200)
    check(
        "the frontend has no inline scripts (CSP script-src 'self')",
        not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", page.text),
    )
    check("theme bootstrap is served as a file", theme.status_code == 200)
    check(
        "hashed assets are served with a JavaScript type and long caching",
        asset is not None
        and asset.status_code == 200
        and "javascript" in asset.headers.get("content-type", "")
        and "max-age" in asset.headers.get("cache-control", ""),
    )
    check(
        "HTTPS routes /api to the API",
        api.status_code == 200 and api.json() == {"status": "ok", "database": "ok"},
    )
    check("hosted API publishes no explorer", explorer == [404, 404], explorer)
    check("a Host naming an internal listener is refused", forged_host.status_code == 421)
    for label, response in (("page", page), ("API", api), ("error", missing)):
        check(f"security headers on {label} responses", secure_headers(response))
    csp = page.headers.get("content-security-policy", "")
    check(
        "CSP forbids inline scripts, framing and foreign origins",
        "script-src 'self'" in csp
        and "frame-ancestors 'none'" in csp
        and "connect-src 'self'" in csp
        and "unsafe-eval" not in csp,
        csp,
    )
    check("HSTS is set", "max-age=31536000" in page.headers.get("strict-transport-security", ""))
    check("the proxy hides its version", page.headers.get("server") == "nginx")


# --- MCP and private listeners ------------------------------------------------------------


MCP_PATHS = [
    "/api/mcp",
    "/api/mcp/",
    "/api/MCP/",
    "/api/%6dcp/",
    "/api/%6Dcp",
    "/api//mcp/",
    "/api/./mcp/",
    "/api/x/../mcp/",
    "/api/%2fmcp/",
    "/api/mcp%2f",
    "/api/mcp;/",
    "/mcp",
    "/mcp/",
]


def mcp_is_private() -> None:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    headers = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        "authorization": "Bearer x",
    }
    reached = []
    with client() as c:
        for path in MCP_PATHS:
            # Raw path: httpx must not normalize the spelling being tested.
            request = c.build_request("POST", BASE, json=body, headers=headers)
            request.url = request.url.copy_with(raw_path=path.encode())
            response = c.send(request)
            bearer = "bearer" in response.headers.get("www-authenticate", "").lower()
            if response.status_code in (200, 401) and (bearer or "jsonrpc" in response.text):
                reached.append((path, response.status_code))
            elif path.startswith("/api") and response.status_code not in (400, 404, 405):
                reached.append((path, response.status_code))
    check("no spelling of /mcp reaches an MCP server through the proxy", not reached, reached)
    blocked = [f"{a}:{p}" for a, p in PRIVATE if not unreachable(a, p)]
    check("API, MCP and PostgreSQL listeners are unreachable from outside", not blocked, blocked)
    try:
        socket.getaddrinfo("postgres", 5432)
        resolved = True
    except OSError:
        resolved = False
    check("the database name does not even resolve outside", not resolved)


# --- sign-in, session cookie, CSRF and origin ---------------------------------------------


APPROVED = ("e2e-user", "e2e@inquest.test")
NOT_ADMITTED = f"{BASE}/?auth_error=not_admitted"


def provider_login(
    c: httpx.Client, authorize: str, sub: str, email: str, verified: bool = True
) -> httpx.Response:
    """The mock provider's login form (interactive mode): the username is the subject."""
    claims = {"email": email, "email_verified": verified, "name": sub}
    form = c.post(authorize, data={"username": sub, "claims": json.dumps(claims)})
    return c.get(form.headers["location"])


def login_as(
    c: httpx.Client, sub: str, email: str, verified: bool = True, invitation: str | None = None
) -> httpx.Response:
    """A whole sign-in through the proxy; returns the callback's response."""
    if invitation is None:
        authorize = c.get(f"{BASE}/api/auth/login").headers["location"]
    else:
        start = c.post(
            f"{BASE}/api/auth/login",
            json={"invitation_token": invitation, "return_to": "/invitations/accept"},
            headers={"origin": BASE},
        )
        authorize = start.json()["authorization_url"]
    return provider_login(c, authorize, sub, email, verified)


def no_session(response: httpx.Response) -> bool:
    return response.headers.get("location") == NOT_ADMITTED and not any(
        v.startswith("__Host-inquest_session=") and "Max-Age=0" not in v
        for v in response.headers.get_list("set-cookie")
    )


def bootstrap() -> None:
    """Before any operator approval: valid sign-ins, even the future first user's, get no
    session (INQUEST_AUTH_ADMISSION=approval)."""
    for label, (sub, email, verified) in {
        "an unknown identity": ("stranger", "stranger@inquest.test", True),
        "the first user before approval": (*APPROVED, True),
        "an unverified email": ("unverified", "e2e@inquest.test", False),
    }.items():
        with client() as c:
            back = login_as(c, sub, email, verified)
            state = c.get(f"{BASE}/api/auth/session").json()
            me = c.get(f"{BASE}/api/me").status_code
        check(
            f"{label} is authenticated but not admitted: no session, no access",
            no_session(back) and state["authenticated"] is False and me == 401,
            (back.headers.get("location"), state, me),
        )


def invitations(c: httpx.Client, csrf: str, organization_id: str) -> None:
    """The approved user invites a teammate; through the proxy, the link admits exactly
    the verified invited identity, once."""
    created = c.post(
        f"{BASE}/api/organizations/{organization_id}/invitations",
        json={"email": "teammate@example.com", "role": "member"},
        headers={"origin": BASE, "x-csrf-token": csrf},
    )
    if not check("an owner can invite a teammate", created.status_code == 201, created.text):
        return
    token = created.json()["accept_url"].split("#token=")[1]
    with client() as wrong:
        back = login_as(wrong, "intruder", "intruder@example.com", invitation=token)
    check("an invitation link admits nobody but the invited address", no_session(back))
    with client() as unverified:
        back = login_as(unverified, "teammate-x", "teammate@example.com", False, token)
    check("an unverified invited address is not admitted", no_session(back))
    with client() as mate:
        back = login_as(mate, "teammate", "teammate@example.com", invitation=token)
        joined = back.headers.get("location") == (
            f"{BASE}/invitations/accept?joined={organization_id}"
        )
        memberships = mate.get(f"{BASE}/api/me").json().get("memberships", [])
    check(
        "the invited teammate is admitted by signing in from the link, into that organization",
        joined
        and [m["organization_id"] for m in memberships] == [organization_id]
        and memberships[0]["role"] == "member",
        (back.headers.get("location"), memberships),
    )
    with client() as replay:
        back = login_as(replay, "teammate-2", "teammate@example.com", invitation=token)
    check("a used invitation admits no one else", no_session(back))


def sign_in(c: httpx.Client) -> str:
    start = c.get(f"{BASE}/api/auth/login")
    authorize = start.headers.get("location", "")
    query = parse_qs(urlsplit(authorize).query)
    check(
        "sign-in starts at the provider with the HTTPS redirect URI",
        start.status_code == 303
        and authorize.startswith("https://oidc.test/")
        and query.get("redirect_uri") == [f"{BASE}/api/auth/callback"],
        authorize[:120],
    )
    login_cookie = start.headers.get("set-cookie", "")
    back = provider_login(c, authorize, *APPROVED)
    cookies = back.headers.get_list("set-cookie")
    session = next((v for v in cookies if v.startswith("__Host-") and "Max-Age=0" not in v), "")
    attributes = {a.strip().lower() for a in session.split(";")[1:]}
    check(
        "the callback lands on the public origin",
        back.status_code == 303 and back.headers.get("location", "").startswith(BASE),
        back.headers.get("location"),
    )
    check(
        "cookies are __Host-, Secure, HttpOnly, SameSite=Lax, host-only",
        bool(session)
        and {"secure", "httponly", "samesite=lax", "path=/"} <= attributes
        and not any(a.startswith("domain") for a in attributes)
        and "secure" in login_cookie.lower(),
        session.split("=")[0],
    )
    state = c.get(f"{BASE}/api/auth/session").json()
    check("the session works through the proxy", state.get("authenticated") is True, state)
    return state["csrf_token"]


def writes(c: httpx.Client, csrf: str) -> dict[str, str]:
    url = f"{BASE}/api/organizations"
    body = {"name": f"e2e {uuid.uuid4().hex[:6]}"}
    no_token = c.post(url, json=body, headers={"origin": BASE})
    foreign = c.post(url, json=body, headers={"origin": "https://evil.test", "x-csrf-token": csrf})
    cross = c.post(url, json=body, headers={"sec-fetch-site": "cross-site", "x-csrf-token": csrf})
    check("writes without the CSRF token are refused", no_token.status_code == 403)
    check("writes from another origin are refused", foreign.status_code == 403)
    check("cross-site fetches are refused", cross.status_code == 403)
    same = {"origin": BASE, "x-csrf-token": csrf}
    organization = c.post(url, json=body, headers=same)
    check("same-origin writes with the token succeed", organization.status_code == 201)
    environment = c.post(
        f"{BASE}/api/environments",
        json={"name": "e2e", "type": "local", "organization_id": organization.json()["id"]},
        headers=same,
    )
    investigation = c.post(
        f"{BASE}/api/investigations",
        json={"environment_id": environment.json()["id"], "title": "e2e", "question": "why?"},
        headers=same,
    )
    check("an investigation can be opened", investigation.status_code == 201, investigation.text)
    invitations(c, csrf, organization.json()["id"])
    return investigation.json()


# --- SSE through the proxy ----------------------------------------------------------------


def read_events(c: httpx.Client, url: str, seconds: float, **headers: str) -> tuple[list, dict]:
    ids: list[int] = []
    deadline = time.monotonic() + seconds
    with c.stream("GET", url, headers={"accept": "text/event-stream", **headers}) as stream:
        info = {"status": stream.status_code, "type": stream.headers.get("content-type", "")}
        try:
            for line in stream.iter_lines():
                if line.startswith("id: "):
                    ids.append(int(line[4:]))
                    if not headers:
                        break  # the first event arrived while the stream is open
                if time.monotonic() > deadline:
                    break
        except httpx.ReadTimeout:
            pass
        info["open"] = not stream.is_closed
    return ids, info


def streams(c: httpx.Client, investigation: dict) -> None:
    url = f"{BASE}/api/investigations/{investigation['id']}/events"
    started = time.monotonic()
    first, info = read_events(c, url, 5)
    elapsed = time.monotonic() - started
    check(
        "SSE events arrive unbuffered while the stream stays open",
        first and info["status"] == 200 and "text/event-stream" in info["type"] and elapsed < 3,
        (first, info, round(elapsed, 2)),
    )
    if first:
        quiet = httpx.Client(verify=tls(), cookies=c.cookies, timeout=httpx.Timeout(10, read=2))
        with quiet:
            replayed, _ = read_events(quiet, url, 2.5, **{"last-event-id": str(first[-1])})
        check("Last-Event-ID resumes after the last event", not replayed, replayed)


# --- session lifecycle (M3, D043) ---------------------------------------------------------

DISABLED = f"{BASE}/?auth_error=account_disabled"


def events_url(c: httpx.Client) -> str:
    organization = c.get(f"{BASE}/api/me").json()["memberships"][0]["organization_id"]
    page = c.get(f"{BASE}/api/investigations", params={"organization_id": organization}).json()
    return f"{BASE}/api/investigations/{page['items'][0]['id']}/events"


def watch(c: httpx.Client, url: str, seconds: float, into: dict) -> None:
    """Follows a stream until it reports access_revoked, closes or `seconds` pass."""
    lines: list[str] = []
    deadline = time.monotonic() + seconds
    try:
        with c.stream("GET", url, headers={"accept": "text/event-stream"}) as stream:
            into["status"] = stream.status_code
            for line in stream.iter_lines():
                lines.append(line)
                if line == "event: access_revoked" or time.monotonic() > deadline:
                    break
    except httpx.HTTPError as exc:
        lines.append(f"({type(exc).__name__})")
    into["ended_at"] = time.monotonic()
    into["revoked"] = "event: access_revoked" in lines


def signed_in(c: httpx.Client) -> bool:
    return c.get(f"{BASE}/api/auth/session").json()["authenticated"] is True


def streaming_client() -> httpx.Client:
    # Keep-alives come every 15 s; the default 10 s read timeout would end a quiet stream.
    return httpx.Client(verify=tls(), timeout=httpx.Timeout(10, read=30), follow_redirects=False)


def sign_out_everywhere() -> None:
    """Two browsers of the approved user; one watches an investigation, the other signs out
    everywhere through the proxy."""
    with streaming_client() as a, client() as b:
        login_as(a, *APPROVED), login_as(b, *APPROVED)
        url = events_url(a)
        watched: dict = {}
        watcher = threading.Thread(target=watch, args=(a, url, 30, watched))
        watcher.start()
        time.sleep(2)
        endpoint = f"{BASE}/api/auth/logout-everywhere"
        no_token = b.post(endpoint, headers={"origin": BASE})
        foreign = b.post(endpoint, headers={"origin": "https://evil.test", "x-csrf-token": "x"})
        check(
            "sign out everywhere needs the CSRF token and the same origin",
            no_token.status_code == 403 and foreign.status_code == 403 and signed_in(a),
        )
        csrf = b.get(f"{BASE}/api/auth/session").json()["csrf_token"]
        done = b.post(endpoint, headers={"origin": BASE, "x-csrf-token": csrf})
        signed_out_at = time.monotonic()
        watcher.join(35)
        check(
            "sign out everywhere ends the sessions of every browser",
            done.status_code == 204
            and not signed_in(a)
            and not signed_in(b)
            and a.get(f"{BASE}/api/me").status_code == 401,
            done.status_code,
        )
        delay = watched.get("ended_at", float("inf")) - signed_out_at
        check(
            "the other browser's open stream ends with access_revoked within the detection "
            "delay (5 s re-check plus a poll)",
            watched.get("status") == 200 and watched.get("revoked") and delay < 7,
            (watched, round(delay, 2)),
        )
    with client() as again:
        back = login_as(again, *APPROVED)
        check(
            "signing in again needs only the provider",
            back.status_code == 303 and signed_in(again),
            back.headers.get("location"),
        )


def disabled_while_streaming() -> None:
    """Runs while the operator disables the account (run.sh): the stream ends."""
    with streaming_client() as c:
        login_as(c, *APPROVED)
        watched: dict = {}
        watch(c, events_url(c), 60, watched)
        check(
            "operator disabling ends an open stream and the session",
            watched.get("revoked") and not signed_in(c),
            watched,
        )


def disabled() -> None:
    with client() as c:
        back = login_as(c, *APPROVED)
        check(
            "a disabled account cannot sign in, despite its approval",
            back.headers.get("location") == DISABLED and not signed_in(c),
            back.headers.get("location"),
        )


def enabled() -> None:
    with client() as c:
        back = login_as(c, *APPROVED)
        memberships = c.get(f"{BASE}/api/me").json().get("memberships", [])
        investigations = (
            c.get(
                f"{BASE}/api/investigations",
                params={"organization_id": memberships[0]["organization_id"]},
            ).json()["items"]
            if memberships
            else []
        )
        check(
            "after re-enabling, a new sign-in works and organizations and investigations are "
            "all still there",
            back.status_code == 303 and signed_in(c) and memberships and investigations,
            (back.headers.get("location"), len(memberships), len(investigations)),
        )


# --- request bounds at the proxy ----------------------------------------------------------


def bounds(c: httpx.Client, csrf: str) -> None:
    big = c.post(
        f"{BASE}/api/organizations",
        content=b"{" + b" " * 200_000 + b"}",
        headers={"origin": BASE, "x-csrf-token": csrf, "content-type": "application/json"},
    )
    check(
        "oversized bodies stop at the proxy (413)",
        big.status_code == 413 and "nginx" in big.text and secure_headers(big),
        big.status_code,
    )

    def chunks():
        for _ in range(20):
            yield b" " * 10_000

    chunked = c.post(
        f"{BASE}/api/organizations",
        content=chunks(),
        headers={"origin": BASE, "x-csrf-token": csrf, "content-type": "application/json"},
    )
    check("oversized chunked bodies stop at the proxy", chunked.status_code == 413)

    request = (
        f"POST /api/organizations HTTP/1.1\r\nHost: {HOST}\r\n"
        "Content-Type: application/json\r\nContent-Length: 1000\r\n\r\n{"
    ).encode()
    answer, waited = raw_exchange(request, wait=25)
    check(
        "a stalled body is cut off by the proxy (~10 s)",
        8 <= waited <= 15 and (answer == b"" or answer.startswith(b"HTTP/1.1 408")),
        (round(waited, 1), answer[:40]),
    )
    answer, waited = raw_exchange(f"GET / HTTP/1.1\r\nHost: {HOST}\r\n".encode(), wait=25)
    check(
        "stalled headers are cut off by the proxy (~10 s)",
        8 <= waited <= 15 and not answer.startswith(b"HTTP/1.1 200"),
        (round(waited, 1), answer[:40]),
    )
    huge_header = c.get(f"{BASE}/api/health", headers={"x-filler": "a" * 20_000})
    check("oversized headers are refused", huge_header.status_code in (400, 431, 494))


# --- client addresses and the H3 limits ---------------------------------------------------


def app_limited(response: httpx.Response) -> bool:
    try:
        return response.status_code == 429 and response.json()["detail"]["code"] == "rate_limited"
    except (ValueError, KeyError, TypeError):
        return False


def exhaust_with_spoofed_headers() -> None:
    """Paced below the proxy's own limit, so a 429 here is the application's per-address
    limit, reached despite a different forged identity on every request."""
    codes = []
    with client() as c:
        for i in range(140):
            spoof = {
                "x-forwarded-for": f"198.51.100.{i}",
                "forwarded": f"for=198.51.100.{i}",
                "x-real-ip": f"198.51.100.{i}",
            }
            response = c.get(f"{BASE}/api/health", headers=spoof)
            codes.append(response.status_code)
            if app_limited(response):
                break
            time.sleep(0.07)
    check(
        "forged X-Forwarded-For/Forwarded/X-Real-IP do not escape the address limit",
        codes[-1] == 429 and len(codes) <= 121,
        f"{len(codes)} requests, last {codes[-1]}",
    )


def independent_budget() -> None:
    with client() as c:
        fresh = c.get(f"{BASE}/api/health")
        # Naming client a's address does not reach (or spend) a's bucket either.
        named = c.get(f"{BASE}/api/health", headers={"x-forwarded-for": "172.30.30.10"})
        burst = [c.get(f"{BASE}/api/auth/session") for _ in range(120)]
    check("another client keeps its own budget", fresh.status_code == 200, fresh.status_code)
    check("forging another client's address changes nothing", named.status_code == 200)
    proxy_limited = [r for r in burst if r.status_code == 429 and not app_limited(r)]
    check("the proxy's own request-rate limit answers 429 first", bool(proxy_limited))


def main() -> int:
    name = os.environ["CLIENT_NAME"]
    lifecycle = {
        "everywhere": sign_out_everywhere,
        "disabled-while-streaming": disabled_while_streaming,
        "disabled": disabled,
        "enabled": enabled,
    }
    if name == "bootstrap":
        bootstrap()
    elif name in lifecycle:
        lifecycle[name]()
    elif name == "a":
        front_door()
        mcp_is_private()
        with client() as c:
            csrf = sign_in(c)
            investigation = writes(c, csrf)
            streams(c, investigation)
            bounds(c, csrf)
        exhaust_with_spoofed_headers()
    else:
        independent_budget()
    failed = [n for ok, n in results if not ok]
    print(json.dumps({"client": name, "checks": len(results), "failed": failed}))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
