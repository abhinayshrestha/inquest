#!/usr/bin/env bash
# Local security validation of the hosted topology (D041, docs/deployment.md).
#
#   deploy/e2e/run.sh            build, run every check, tear everything down
#   KEEP=1 deploy/e2e/run.sh     leave the stack running afterwards (https://localhost:18443)
#
# Builds deploy/compose.yaml with the test overlay under project "inquest-e2e" on its own
# networks (172.30.10-30.0/24) with its own volume. Generates a throwaway CA, certificates
# and random secrets in a temporary directory; touches no .env, no other container and no
# development database. Calls no real Nebius, SigNoz or identity provider.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WORK="$(mktemp -d)"
export INQUEST_SECRETS_DIR="$WORK" INQUEST_SERVER_NAME=inquest.test
export INQUEST_HTTP_PORT=127.0.0.1:18080 INQUEST_HTTPS_PORT=127.0.0.1:18443
COMPOSE=(docker compose -p inquest-e2e -f "$ROOT/deploy/compose.yaml" -f "$ROOT/deploy/e2e/compose.e2e.yaml")
FAILED=0

cleanup() {
  if [[ "${KEEP:-}" != 1 ]]; then
    "${COMPOSE[@]}" --profile e2e --profile ops down -v --remove-orphans >/dev/null 2>&1 || true
    rm -rf "$WORK"
  else
    echo "stack kept; secrets in $WORK; stop with: INQUEST_SECRETS_DIR=$WORK ${COMPOSE[*]} down -v"
  fi
}
trap cleanup EXIT

step() { printf '\n== %s\n' "$*"; }
secret() { openssl rand -base64 "$1" | tr -d '\n'; }

step "test CA, certificates and secrets in $WORK"
mkdir -p "$WORK/tls" "$WORK/postgres"
cd "$WORK"
openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj "/CN=Inquest e2e test CA" \
  -keyout ca.key -out postgres/ca.pem \
  -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
issue() {  # issue <dir> <cert> <key> <subjectAltName>
  openssl req -newkey rsa:2048 -nodes -subj "/CN=${4#DNS:}" -keyout "$1/$3" -out "$1/req.csr" 2>/dev/null
  printf 'subjectAltName=%s\nextendedKeyUsage=serverAuth\nbasicConstraints=CA:FALSE\nauthorityKeyIdentifier=keyid\n' "$4" > "$1/ext.cnf"
  openssl x509 -req -in "$1/req.csr" -CA postgres/ca.pem -CAkey ca.key -CAcreateserial -days 2 \
    -extfile "$1/ext.cnf" -out "$1/$2" 2>/dev/null
  rm "$1/req.csr" "$1/ext.cnf"
}
issue tls fullchain.pem privkey.pem "DNS:inquest.test,DNS:oidc.test"
issue postgres server.crt server.key "DNS:postgres"

ADMIN_PASSWORD=$(secret 32) APP_PASSWORD=$(secret 32) MIGRATION_PASSWORD=$(secret 32)
cat > postgres.env <<EOF
POSTGRES_USER=inquest_admin
POSTGRES_PASSWORD=$ADMIN_PASSWORD
EOF
cat > admin.env <<EOF
POSTGRES_USER=inquest_admin
POSTGRES_PASSWORD=$ADMIN_PASSWORD
INQUEST_DB_USER=inquest_app
INQUEST_DB_PASSWORD=$APP_PASSWORD
INQUEST_DB_MIGRATION_USER=inquest_migrator
INQUEST_DB_MIGRATION_PASSWORD=$MIGRATION_PASSWORD
EOF
cat > migrate.env <<EOF
INQUEST_DB_USER=inquest_app
INQUEST_DB_MIGRATION_USER=inquest_migrator
INQUEST_DB_MIGRATION_PASSWORD=$MIGRATION_PASSWORD
EOF
cat > app.env <<EOF
INQUEST_DB_USER=inquest_app
INQUEST_DB_PASSWORD=$APP_PASSWORD
MCP_TOKEN_SECRET=$(secret 48)
INQUEST_AUTH_ISSUER=https://oidc.test/default
INQUEST_AUTH_CLIENT_ID=inquest-e2e
INQUEST_AUTH_CLIENT_SECRET=$(secret 24)
INQUEST_AUTH_PUBLIC_URL=https://inquest.test
INQUEST_CREDENTIAL_KEYS={"e2e": "$(secret 32)"}
INQUEST_CREDENTIAL_ACTIVE_KEY=e2e
INQUEST_CREDENTIAL_FINGERPRINT_KEY=$(secret 32)
INQUEST_AUTH_ADMISSION=approval
INQUEST_RATE_LIMITS={"requests.ip": "120/60"}
INQUEST_RUNNER_ENABLED=false
EOF
chmod 600 ./*.env ca.key tls/privkey.pem postgres/server.key
cd "$ROOT"

step "build images"
"${COMPOSE[@]}" --profile e2e build --quiet app proxy

step "database: start, provision roles (administrative job), migrate (migration job)"
"${COMPOSE[@]}" up -d --wait postgres
"${COMPOSE[@]}" run --rm provision | tail -n 3
"${COMPOSE[@]}" run --rm migrate 2>&1 | tail -n 2

step "hosted startup refuses unsafe or missing configuration"
refuses() {  # refuses <expected text> <docker compose run args...> (args may hold secrets)
  local expected="$1" output
  shift
  if output=$("${COMPOSE[@]}" run --rm --no-deps "$@" app 2>&1); then
    echo "FAIL  started anyway: $expected"; FAILED=1
  elif grep -q -- "$expected" <<<"$output"; then
    echo "PASS  refused: $expected"
  else
    echo "FAIL  refused for another reason (expected: $expected)"; tail -n 3 <<<"$output"; FAILED=1
  fi
}
refuses "POSTGRES_PASSWORD (administrative role) is set" -e POSTGRES_PASSWORD=x
refuses "INQUEST_DB_MIGRATION_PASSWORD is set" -e INQUEST_DB_MIGRATION_PASSWORD=x
refuses "MCP_TOKEN_SECRET must be at least" -e MCP_TOKEN_SECRET=change-me-to-at-least-32-random-characters
refuses "sign-in: INQUEST_AUTH_ISSUER" -e INQUEST_AUTH_ISSUER=
refuses "credential storage" -e INQUEST_CREDENTIAL_KEYS={}
refuses "INQUEST_AUTH_ADMISSION=open is not allowed" -e INQUEST_AUTH_ADMISSION=open
refuses "INQUEST_AUTH_ADMISSION" -e INQUEST_AUTH_ADMISSION=
refuses "INQUEST_TRUSTED_PROXIES must name the proxy" -e 'INQUEST_TRUSTED_PROXIES=["0.0.0.0/0"]'
refuses "INQUEST_TRUSTED_PROXIES is not set" -e 'INQUEST_TRUSTED_PROXIES=[]'
refuses "WEB_CONCURRENCY" -e WEB_CONCURRENCY=4
# The administrative role from the application's address: pg_hba.conf refuses it.
refuses "pg_hba.conf rejects connection" -e INQUEST_DB_USER=inquest_admin -e "INQUEST_DB_PASSWORD=$ADMIN_PASSWORD"
refuses "INQUEST_DB_USER and INQUEST_DB_PASSWORD" -e INQUEST_DB_PASSWORD=

step "start the application and the proxy"
"${COMPOSE[@]}" up -d --wait app proxy oidc
APP_ADDRESSES=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' \
  "$("${COMPOSE[@]}" ps -q app)")
echo "app addresses: $APP_ADDRESSES"
published=$("${COMPOSE[@]}" ps --format '{{.Service}} {{.Ports}}' | grep -- '->' || true)
echo "$published"
if grep -v '^proxy ' <<<"$published" | grep -q .; then
  echo "FAIL  a service other than the proxy publishes ports"; FAILED=1
else
  echo "PASS  only the proxy publishes ports (127.0.0.1:18080, 127.0.0.1:18443)"
fi

step "inside the application container"
"${COMPOSE[@]}" exec -T -e PYTHONPATH=/app app python /e2e/internal.py || FAILED=1

step "bootstrap: nobody is admitted until the operator approves the first user"
"${COMPOSE[@]}" run --rm -e CLIENT_NAME=bootstrap client-a || FAILED=1
"${COMPOSE[@]}" exec -T app python -m scripts.admit_user approve \
  --email e2e@inquest.test --operator e2e --apply
"${COMPOSE[@]}" exec -T app python -m scripts.admit_user list

step "client a (simulated internet)"
"${COMPOSE[@]}" run --rm -e "APP_ADDRESSES=$APP_ADDRESSES" client-a || FAILED=1

step "client b (another address)"
"${COMPOSE[@]}" run --rm client-b || FAILED=1

step "session lifecycle: sign out everywhere, operator disable and enable (D043)"
"${COMPOSE[@]}" run --rm -T -e CLIENT_NAME=everywhere client-b || FAILED=1
USER_ID=$("${COMPOSE[@]}" exec -T postgres sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql \
  -U "$POSTGRES_USER" -d inquest -At -c "SELECT id FROM users WHERE subject = '"'e2e-user'"'"')
"${COMPOSE[@]}" run --rm -T -e CLIENT_NAME=disabled-while-streaming client-b &
watcher=$!
sleep 8
"${COMPOSE[@]}" exec -T app python -m scripts.admit_user disable \
  --user "$USER_ID" --operator e2e --apply
wait "$watcher" || FAILED=1
"${COMPOSE[@]}" run --rm -T -e CLIENT_NAME=disabled client-b || FAILED=1
"${COMPOSE[@]}" exec -T app python -m scripts.admit_user enable \
  --user "$USER_ID" --operator e2e --apply
"${COMPOSE[@]}" run --rm -T -e CLIENT_NAME=enabled client-b || FAILED=1

step "database sessions"
sessions=$("${COMPOSE[@]}" exec -T postgres sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -U "$POSTGRES_USER" -d inquest -At -F " " -c "
  SELECT usename, ssl, count(*) FROM pg_stat_activity JOIN pg_stat_ssl USING (pid)
  WHERE datname = current_database() AND pid <> pg_backend_pid() GROUP BY 1, 2"')
echo "$sessions"
if [[ "$(awk '{print $1, $2}' <<<"$sessions" | sort -u)" == "inquest_app t" ]]; then
  echo "PASS  every application connection is inquest_app over TLS"
else
  echo "FAIL  unexpected database sessions"; FAILED=1
fi

step "graceful shutdown"
started=$(date +%s)
"${COMPOSE[@]}" stop app >/dev/null
echo "app stopped in $(( $(date +%s) - started ))s"
"${COMPOSE[@]}" logs app 2>&1 | grep -E "Shutting down|Finished server process|Application shutdown" | tail -n 3

if [[ $FAILED == 0 ]]; then step "ALL CHECKS PASSED"; else step "SOME CHECKS FAILED"; fi
exit $FAILED
