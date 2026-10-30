#!/usr/bin/env bash
# Regression test for how deploy/inquest.sh installs TLS files for the proxy.
#
#   deploy/e2e/tls_permissions.sh
#
# The proxy runs as root with cap_drop ALL (deploy/compose.yaml): without CAP_DAC_OVERRIDE
# it cannot open a key the deploying user owns with mode 0600, and the stack fails to
# serve. This checks inquest.sh's own helpers against a reader with exactly the proxy's
# hardening (nginx -t on the proxy's base image): the installed files are readable by
# it, the key is root 0600 and unreadable to the deploying user, a mismatched pair is
# refused without touching the installed one, and rollback restores the previous pair.
#
# Needs a Linux Docker host and a non-root user in the docker group (the CI runner, the
# EC2 host). Docker Desktop does not enforce bind-mount permissions: there the negative
# control cannot fail and the test exits 2 (inconclusive) instead of passing.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck source=../inquest.sh
. "$ROOT/deploy/inquest.sh"

NGINX_IMAGE=nginx:1.30-alpine
WORK="$(mktemp -d)"
FAILED=0

cleanup() {
  # Root-owned files: removed the way they were written.
  tls_root 'rm -rf /w/* /w/.[!.]* 2>/dev/null || true' -v "$WORK:/w" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; FAILED=1; }

[[ "$(id -u)" != 0 ]] || { echo "run as a non-root user in the docker group"; exit 2; }

# The proxy's runtime restrictions (deploy/compose.yaml: x-hardening plus cap_add).
PROXY=(--cap-drop ALL --cap-add NET_BIND_SERVICE --cap-add CHOWN --cap-add SETUID
  --cap-add SETGID --security-opt no-new-privileges:true --network none)

cat >"$WORK/nginx.conf" <<'EOF'
server {
    listen 443 ssl;
    server_name a.test;
    ssl_certificate /etc/nginx/tls/fullchain.pem;
    ssl_certificate_key /etc/nginx/tls/privkey.pem;
}
EOF
chmod 644 "$WORK/nginx.conf"

proxy_reads() {  # proxy_reads TLS_DIR: nginx -t as the hardened proxy would run it
  docker run --rm "${PROXY[@]}" -v "$1:/etc/nginx/tls:ro" \
    -v "$WORK/nginx.conf:/etc/nginx/conf.d/default.conf:ro" "$NGINX_IMAGE" nginx -t >/dev/null 2>&1
}

pair() {  # pair DIR: self-signed chain and key, as the deploying user (key 0600)
  mkdir -p "$1"
  openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj "/CN=a.test" \
    -addext "subjectAltName=DNS:a.test,DNS:b.test" \
    -keyout "$1/privkey.pem" -out "$1/fullchain.pem" 2>/dev/null
  chmod 600 "$1/privkey.pem"
}

fingerprint() { openssl x509 -noout -fingerprint -sha256 2>/dev/null; }

modes() {  # owner:group mode of the TLS directory and both files, as root sees them
  tls_root 'stat -c "%u:%g %a" /tls /tls/fullchain.pem /tls/privkey.pem | tr "\n" " "' -v "$1:/tls:ro"
}

docker pull -q "$NGINX_IMAGE" >/dev/null
docker pull -q "$CERTBOT_IMAGE" >/dev/null
pair "$WORK/a"
pair "$WORK/b"

echo "== negative control: the deploying user's own 0600 key (the original bug)"
mkdir -p "$WORK/user-owned"
cp "$WORK/a/fullchain.pem" "$WORK/a/privkey.pem" "$WORK/user-owned/"
chmod 700 "$WORK/user-owned"
if proxy_reads "$WORK/user-owned"; then
  echo "INCONCLUSIVE: this Docker host lets the hardened proxy read another user's 0600 key"
  echo "(Docker Desktop does not enforce bind-mount permissions); run on a Linux host"
  exit 2
fi
pass "the hardened proxy cannot read a key owned by the deploying user"

echo "== install_tls"
TLS="$WORK/tls"
install_tls "$WORK/a/fullchain.pem" "$WORK/a/privkey.pem" "$TLS" a.test b.test
proxy_reads "$TLS" && pass "the hardened proxy reads the installed pair (nginx -t)" ||
  fail "the hardened proxy cannot read the installed pair"
got=$(modes "$TLS")
[[ "$got" == "0:0 700 0:0 644 0:0 600 " ]] && pass "directory root 0700, chain root 0644, key root 0600" ||
  fail "unexpected ownership or modes: $got"
if cat "$TLS/privkey.pem" >/dev/null 2>&1; then fail "the deploying user can read the key"; else
  pass "the deploying user cannot read the key"; fi
[[ "$(installed_cert "$TLS" | fingerprint)" == "$(fingerprint <"$WORK/a/fullchain.pem")" ]] &&
  pass "installed_cert returns the installed chain" || fail "installed_cert returned another chain"

echo "== refusals leave the installed pair untouched"
before=$(installed_cert "$TLS" | fingerprint)
if (install_tls "$WORK/a/fullchain.pem" "$WORK/b/privkey.pem" "$TLS" a.test b.test) 2>/dev/null; then
  fail "a mismatched certificate and key were installed"
else
  pass "a mismatched certificate and key are refused"
fi
if (install_tls "$WORK/a/fullchain.pem" "$WORK/a/privkey.pem" "$TLS" a.test other.test) 2>/dev/null; then
  fail "a certificate missing a required name was installed"
else
  pass "a certificate missing a required name is refused"
fi
[[ "$(installed_cert "$TLS" | fingerprint)" == "$before" ]] && proxy_reads "$TLS" &&
  pass "after refusals the previous pair is installed and readable" ||
  fail "a refusal changed the installed pair"

echo "== replacement and rollback"
install_tls "$WORK/b/fullchain.pem" "$WORK/b/privkey.pem" "$TLS" a.test b.test
[[ "$(installed_cert "$TLS" | fingerprint)" == "$(fingerprint <"$WORK/b/fullchain.pem")" ]] &&
  proxy_reads "$TLS" && pass "a renewal replaces the pair and stays readable" ||
  fail "the replacement pair is not installed or not readable"
got=$(tls_root 'stat -c "%u:%g %a" /tls/fullchain.pem.prev /tls/privkey.pem.prev | tr "\n" " "' -v "$TLS:/tls:ro")
[[ "$got" == "0:0 644 0:0 600 " ]] && pass "the previous pair is kept root-owned, key 0600" ||
  fail "unexpected backup ownership or modes: $got"
rollback_tls "$TLS"
[[ "$(installed_cert "$TLS" | fingerprint)" == "$(fingerprint <"$WORK/a/fullchain.pem")" ]] &&
  proxy_reads "$TLS" && [[ "$(modes "$TLS")" == "0:0 700 0:0 644 0:0 600 " ]] &&
  pass "rollback restores the previous pair, readable, same modes" || fail "rollback failed"

echo "== validity checks used by 'certs'"
cert_ok "$WORK/a/fullchain.pem" 0 a.test b.test && pass "cert_ok accepts a current certificate for both names" ||
  fail "cert_ok rejected a current certificate"
if cert_ok "$WORK/a/fullchain.pem" 30 a.test b.test; then fail "cert_ok ignored the renewal window"; else
  pass "cert_ok reports a certificate expiring within the renewal window"; fi
if cert_ok "$WORK/a/fullchain.pem" 0 a.test a.tes; then fail "cert_ok matched a name by substring"; else
  pass "cert_ok matches whole names only"; fi

exit "$FAILED"
