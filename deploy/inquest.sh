#!/usr/bin/env bash
# Single-client Inquest stack: proxy + frontend, API, PostgreSQL and a bundled SigNoz on
# one Docker host (docs/deployment.md, D041, D044). Run from anywhere in the repository:
#
#   deploy/inquest.sh init            ask for host names and sign-in; generate every secret
#   deploy/inquest.sh up              build, provision, migrate, start SigNoz, then app + proxy
#   deploy/inquest.sh certs           Let's Encrypt certificate for both names (port 80 open)
#   deploy/inquest.sh admit EMAIL     approve the first user (D042)
#   deploy/inquest.sh bind-signoz     after that user created the organization (D037, D038)
#   deploy/inquest.sh upgrade         after git pull: rebuild, migrate, restart app + proxy
#   deploy/inquest.sh backup          pg_dump of the Inquest database
#   deploy/inquest.sh status | logs [SERVICE] | down
#
# Everything about this deployment lives in INQUEST_SECRETS_DIR (default deploy/secrets,
# git-ignored, mode 0700): stack.env (host names, not secret) and one file per process.
# Secrets are generated here or read from the terminal, never passed as arguments.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SECRETS="${INQUEST_SECRETS_DIR:-$ROOT/deploy/secrets}"
[[ "$SECRETS" = /* ]] || SECRETS="$PWD/$SECRETS"
STACK="$SECRETS/stack.env"
CERTBOT_IMAGE=certbot/certbot:v5.9.0@sha256:bc93cb710b413e6b456d622f9b0133b3e109c0a78c2ed9976af43d209b0a1805
SIGNOZ_URL=http://signoz:8080
OPERATOR="${INQUEST_OPERATOR:-$(id -un)}"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
step() { printf '\n== %s\n' "$*"; }
secret() { openssl rand -base64 "$1" | tr -d '\n'; }

load_stack() {
  [[ -f "$STACK" ]] || die "no $STACK: run 'deploy/inquest.sh init' first"
  set -a
  # shellcheck source=/dev/null
  . "$STACK"
  set +a
  export INQUEST_SECRETS_DIR="$SECRETS"
  PROJECT="${INQUEST_PROJECT:-inquest}"
  FILES=(-f "$ROOT/deploy/compose.yaml" -f "$ROOT/deploy/compose.signoz.yaml")
  # Local validation only (e.g. the e2e mock sign-in): extra compose files, colon-separated.
  if [[ -n "${INQUEST_EXTRA_COMPOSE:-}" ]]; then
    IFS=: read -r -a extra <<<"$INQUEST_EXTRA_COMPOSE"
    for f in "${extra[@]}"; do FILES+=(-f "$f"); done
  fi
}

compose() { docker compose -p "$PROJECT" "${FILES[@]}" "$@"; }

container() { compose ps -a -q "$1"; }

wait_healthy() {  # wait_healthy SERVICE SECONDS
  local deadline=$((SECONDS + $2)) status
  while ((SECONDS < deadline)); do
    status=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$(container "$1")" 2>/dev/null || true)
    [[ "$status" == healthy ]] && return 0
    sleep 5
  done
  compose logs --tail 30 "$1" >&2 || true
  die "$1 is not healthy after $2 s (last status: ${status:-none})"
}

exited_ok() {  # one-off jobs the long-running services depend on
  local state
  state=$(docker inspect -f '{{.State.Status}} {{.State.ExitCode}}' "$(container "$1")")
  [[ "$state" == "exited 0" ]] || { compose logs --tail 30 "$1" >&2; die "$1: $state"; }
}

ask() {  # ask VAR PROMPT [DEFAULT]: environment value first, then the terminal
  local value="${!1:-}"
  if [[ -z "$value" ]]; then
    read -r -p "$2${3:+ [$3]}: " value
    value="${value:-${3:-}}"
  fi
  [[ -n "$value" ]] || die "$1 is required"
  printf -v "$1" '%s' "$value"
}

lower() { tr '[:upper:]' '[:lower:]' <<<"$1"; }

check_host() {
  local re='^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$'
  [[ "$1" =~ $re ]] || die "not a host name: $1"
}

# Values that go into env files read by Docker Compose: no whitespace, quotes,
# backslashes or '$' (Compose would interpolate it).
check_value() {
  case "$2" in
    "" | *[[:space:]\"\'\$\\]*) die "$1 is empty or contains characters that cannot be stored safely" ;;
  esac
}

cmd_init() {
  [[ ! -e "$SECRETS/app.env" ]] || die "$SECRETS already holds a deployment; refusing to overwrite it"
  command -v openssl >/dev/null || die "openssl is required"

  ask INQUEST_SERVER_NAME "Inquest host name (e.g. inquest.example.com)"
  INQUEST_SERVER_NAME=$(lower "$INQUEST_SERVER_NAME")
  check_host "$INQUEST_SERVER_NAME"
  ask INQUEST_SIGNOZ_SERVER_NAME "SigNoz UI host name" "signoz.$INQUEST_SERVER_NAME"
  INQUEST_SIGNOZ_SERVER_NAME=$(lower "$INQUEST_SIGNOZ_SERVER_NAME")
  check_host "$INQUEST_SIGNOZ_SERVER_NAME"
  ask INQUEST_ACME_EMAIL "Email for Let's Encrypt and the SigNoz admin account"
  check_value INQUEST_ACME_EMAIL "$INQUEST_ACME_EMAIL"
  [[ "$INQUEST_ACME_EMAIL" == ?*@?*.?* ]] || die "not an email address"
  ask INQUEST_AUTH_ISSUER "OIDC issuer URL (e.g. https://accounts.google.com)"
  [[ "$INQUEST_AUTH_ISSUER" =~ ^https:// ]] || die "the issuer must be an https URL"
  check_value INQUEST_AUTH_ISSUER "$INQUEST_AUTH_ISSUER"
  ask INQUEST_AUTH_CLIENT_ID "OIDC client id"
  check_value INQUEST_AUTH_CLIENT_ID "$INQUEST_AUTH_CLIENT_ID"
  if [[ -z "${INQUEST_AUTH_CLIENT_SECRET:-}" ]]; then
    read -r -s -p "OIDC client secret (not shown): " INQUEST_AUTH_CLIENT_SECRET
    echo
  fi
  [[ -n "$INQUEST_AUTH_CLIENT_SECRET" ]] || die "the client secret is required"
  check_value "the client secret" "$INQUEST_AUTH_CLIENT_SECRET"

  step "generating secrets in $SECRETS"
  umask 077
  mkdir -p "$SECRETS/tls" "$SECRETS/postgres"
  chmod 700 "$SECRETS"
  cd "$SECRETS"

  # Internal CA for PostgreSQL's certificate (verify-full inside the stack) and the
  # placeholder web certificate until `certs` replaces it.
  openssl req -x509 -newkey rsa:3072 -nodes -days 3650 -subj "/CN=Inquest internal CA" \
    -keyout ca.key -out postgres/ca.pem \
    -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
  issue() {  # issue DIR CERT KEY DAYS SUBJECT_ALT_NAMES
    openssl req -newkey rsa:3072 -nodes -subj "/CN=Inquest internal" -keyout "$1/$3" -out "$1/req.csr" 2>/dev/null
    printf 'subjectAltName=%s\nextendedKeyUsage=serverAuth\nbasicConstraints=CA:FALSE\nauthorityKeyIdentifier=keyid\n' "$5" >"$1/ext.cnf"
    openssl x509 -req -in "$1/req.csr" -CA postgres/ca.pem -CAkey ca.key -CAcreateserial -days "$4" \
      -extfile "$1/ext.cnf" -out "$1/$2" 2>/dev/null
    rm "$1/req.csr" "$1/ext.cnf"
  }
  issue postgres server.crt server.key 3650 "DNS:postgres"
  issue tls fullchain.pem privkey.pem 30 "DNS:$INQUEST_SERVER_NAME,DNS:$INQUEST_SIGNOZ_SERVER_NAME"

  local admin_pw app_pw migration_pw signoz_db_pw
  admin_pw=$(secret 32) app_pw=$(secret 32) migration_pw=$(secret 32) signoz_db_pw=$(openssl rand -hex 24)

  cat >stack.env <<EOF
# Not secret. Read by deploy/inquest.sh and Docker Compose.
INQUEST_SERVER_NAME=$INQUEST_SERVER_NAME
INQUEST_SIGNOZ_SERVER_NAME=$INQUEST_SIGNOZ_SERVER_NAME
INQUEST_ACME_EMAIL=$INQUEST_ACME_EMAIL
SIGNOZ_CLICKHOUSE_MEMORY=4g
EOF
  cat >postgres.env <<EOF
POSTGRES_USER=inquest_admin
POSTGRES_PASSWORD=$admin_pw
EOF
  cat >admin.env <<EOF
POSTGRES_USER=inquest_admin
POSTGRES_PASSWORD=$admin_pw
INQUEST_DB_USER=inquest_app
INQUEST_DB_PASSWORD=$app_pw
INQUEST_DB_MIGRATION_USER=inquest_migrator
INQUEST_DB_MIGRATION_PASSWORD=$migration_pw
EOF
  cat >migrate.env <<EOF
INQUEST_DB_USER=inquest_app
INQUEST_DB_MIGRATION_USER=inquest_migrator
INQUEST_DB_MIGRATION_PASSWORD=$migration_pw
EOF
  local key_id="k$(date +%Y%m)"
  cat >app.env <<EOF
INQUEST_DB_USER=inquest_app
INQUEST_DB_PASSWORD=$app_pw
MCP_TOKEN_SECRET=$(secret 48)
INQUEST_AUTH_ISSUER=$INQUEST_AUTH_ISSUER
INQUEST_AUTH_CLIENT_ID=$INQUEST_AUTH_CLIENT_ID
INQUEST_AUTH_CLIENT_SECRET=$INQUEST_AUTH_CLIENT_SECRET
INQUEST_AUTH_PUBLIC_URL=https://$INQUEST_SERVER_NAME
INQUEST_AUTH_ADMISSION=approval
INQUEST_CREDENTIAL_KEYS={"$key_id": "$(secret 32)"}
INQUEST_CREDENTIAL_ACTIVE_KEY=$key_id
INQUEST_CREDENTIAL_FINGERPRINT_KEY=$(secret 32)
INQUEST_TELEMETRY_ALLOWED_ORIGINS=["$SIGNOZ_URL"]
EOF
  cat >proxy.env <<EOF
INQUEST_OTLP_TOKEN=$(openssl rand -hex 32)
EOF
  cat >signoz.env <<EOF
SIGNOZ_SQLSTORE_POSTGRES_DSN=postgres://signoz:$signoz_db_pw@signoz-metastore-postgres-0:5432/signoz?sslmode=disable
SIGNOZ_TOKENIZER_JWT_SECRET=$(openssl rand -hex 32)
EOF
  cat >signoz-metastore.env <<EOF
POSTGRES_PASSWORD=$signoz_db_pw
EOF
  # SigNoz's own login (the UI); the password has the classes SigNoz's policy asks for.
  cat >signoz-admin.env <<EOF
SIGNOZ_ADMIN_EMAIL=$INQUEST_ACME_EMAIL
SIGNOZ_ADMIN_PASSWORD=$(openssl rand -hex 16)Aa1!
EOF
  chmod 600 ./*.env ca.key tls/privkey.pem postgres/server.key
  # Public certificates. The application runs as an unprivileged user (uid 10001), so the
  # CA it verifies PostgreSQL with must be world-readable on the host (Linux enforces
  # this on bind mounts; Docker Desktop does not).
  chmod 644 postgres/ca.pem postgres/server.crt tls/fullchain.pem

  printf '\nDone. Next: deploy/inquest.sh up\n'
  printf 'Keep %s backed up separately from database backups (it holds the credential keys).\n' "$SECRETS"
}

# Registers SigNoz's first account (whoever registers first owns the instance, so this
# runs before the proxy exposes it) and creates a read-only service account key for
# Inquest. Runs in the app image on the private telemetry network; reads its input
# from stdin and prints the new key, if one was created, as the only stdout line.
SIGNOZ_BOOTSTRAP='
import json, sys, urllib.parse, urllib.request

cfg = json.load(sys.stdin)
base = "http://signoz:8080"

def call(method, path, body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)

if not call("GET", "/api/v1/version").get("setupCompleted"):
    call("POST", "/api/v1/register", {"name": "Inquest operator", "orgId": "", "orgName": "inquest",
                                      "email": cfg["email"], "password": cfg["password"]})
    print("registered the SigNoz admin account", file=sys.stderr)
q = urllib.parse.urlencode({"email": cfg["email"], "ref": base})
org = call("GET", "/api/v2/sessions/context?" + q)["data"]["orgs"][0]["id"]
token = call("POST", "/api/v2/sessions/email_password",
             {"email": cfg["email"], "password": cfg["password"], "orgId": org})["data"]["accessToken"]
accounts = call("GET", "/api/v1/service_accounts", token=token)["data"] or []
account = next((a for a in accounts if a["name"] == "inquest"), None)
if account is None:
    account = call("POST", "/api/v1/service_accounts", {"name": "inquest"}, token)["data"]
    roles = call("GET", "/api/v1/roles", token=token)["data"]
    viewer = next(r["id"] for r in roles if r["name"] == "signoz-viewer")
    call("POST", "/api/v1/service_account_roles", {"serviceAccountId": account["id"], "roleId": viewer}, token)
    print("created the read-only service account \"inquest\"", file=sys.stderr)
if cfg["need_key"]:
    key = call("POST", "/api/v1/service_accounts/" + account["id"] + "/keys",
               {"name": "inquest", "expiresAt": 0}, token)["data"]["key"]
    print(key)
'

bootstrap_signoz() {
  local email password need_key=true key
  email=$(sed -n 's/^SIGNOZ_ADMIN_EMAIL=//p' "$SECRETS/signoz-admin.env")
  password=$(sed -n 's/^SIGNOZ_ADMIN_PASSWORD=//p' "$SECRETS/signoz-admin.env")
  grep -q '^SIGNOZ_INQUEST_API_KEY=' "$SECRETS/signoz-admin.env" && need_key=false
  key=$(printf '{"email": "%s", "password": "%s", "need_key": %s}' "$email" "$password" "$need_key" |
    docker run --rm -i --network "${PROJECT}_telemetry" --read-only --cap-drop ALL \
      --security-opt no-new-privileges:true --entrypoint python inquest-app -c "$SIGNOZ_BOOTSTRAP")
  if [[ -n "$key" ]]; then
    check_value "the SigNoz API key" "$key"
    printf 'SIGNOZ_INQUEST_API_KEY=%s\n' "$key" >>"$SECRETS/signoz-admin.env"
  fi
}

cmd_up() {
  load_stack
  docker info >/dev/null 2>&1 || die "Docker is not reachable (installed? user in the docker group?)"

  step "build images"
  compose build --quiet app proxy

  step "database: start, provision roles, migrate"
  compose up -d postgres
  wait_healthy postgres 120
  compose run --rm provision | tail -n 1
  compose run --rm migrate 2>&1 | tail -n 1

  step "SigNoz (first start pulls images and migrates ClickHouse: a few minutes)"
  compose up -d signoz-signoz-0 ingester
  wait_healthy signoz-signoz-0 600
  exited_ok signoz-telemetrystore-clickhouse-user-scripts
  exited_ok signoz-telemetrystore-migrator
  bootstrap_signoz

  step "application and proxy"
  compose up -d app proxy
  wait_healthy app 180
  wait_healthy proxy 60

  cat <<EOF

Inquest is running.
  App:        https://$INQUEST_SERVER_NAME
  SigNoz UI:  https://$INQUEST_SIGNOZ_SERVER_NAME  (login in $SECRETS/signoz-admin.env)
  OTLP:       https://$INQUEST_SERVER_NAME:4318 (HTTP), $INQUEST_SERVER_NAME:4317 (gRPC)
              header Authorization: Bearer <INQUEST_OTLP_TOKEN in $SECRETS/proxy.env>
The certificate is a placeholder until 'deploy/inquest.sh certs'. Then:
  deploy/inquest.sh admit you@example.com
EOF
}

cmd_certs() {
  load_stack
  local tls="$SECRETS/tls" le="$SECRETS/letsencrypt"
  if openssl x509 -in "$tls/fullchain.pem" -noout -issuer 2>/dev/null | grep -qv "Inquest internal" &&
    openssl x509 -in "$tls/fullchain.pem" -noout -checkend $((30 * 86400)) >/dev/null; then
    echo "certificate valid for more than 30 days; nothing to do"
    return 0
  fi
  mkdir -p "$le"
  step "Let's Encrypt (HTTP-01 on port 80; the proxy stops for a few seconds)"
  compose stop proxy
  # Whatever happens, the proxy comes back (with the old certificate on failure).
  trap 'compose up -d proxy >/dev/null' EXIT
  docker run --rm -p 80:80 --user "$(id -u):$(id -g)" -v "$le:/le" "$CERTBOT_IMAGE" \
    certonly --standalone --non-interactive --agree-tos -m "$INQUEST_ACME_EMAIL" \
    --config-dir /le/config --work-dir /le/work --logs-dir /le/logs \
    --cert-name inquest -d "$INQUEST_SERVER_NAME" -d "$INQUEST_SIGNOZ_SERVER_NAME" \
    ${INQUEST_ACME_STAGING:+--staging}
  install -m 600 "$le/config/live/inquest/fullchain.pem" "$tls/fullchain.pem"
  install -m 600 "$le/config/live/inquest/privkey.pem" "$tls/privkey.pem"
  compose up -d proxy
  trap - EXIT
  wait_healthy proxy 60
  echo "certificate installed. Renew weekly, e.g. cron: 0 4 * * 1 $ROOT/deploy/inquest.sh certs"
}

cmd_admit() {
  [[ -n "${1:-}" ]] || die "usage: deploy/inquest.sh admit EMAIL"
  load_stack
  compose exec -T app python -m scripts.admit_user approve --email "$1" --operator "$OPERATOR" --apply
  echo "Sign in at https://$INQUEST_SERVER_NAME with that account and create the organization, then: deploy/inquest.sh bind-signoz"
}

cmd_bind_signoz() {
  load_stack
  local ids count org
  # Read-only, as the administrative role, inside the database container.
  ids=$(compose exec -T postgres sh -c \
    'PGPASSWORD="$POSTGRES_PASSWORD" psql -U "$POSTGRES_USER" -d inquest -tA -c "SELECT id FROM organizations"')
  count=$(grep -c . <<<"$ids" || true)
  ((count > 0)) || die "no organization yet: sign in and create one first"
  ((count == 1)) || die "$count organizations exist; the bundled SigNoz may belong to one only (D037)"
  org=$(tr -d '[:space:]' <<<"$ids")

  step "grant $SIGNOZ_URL to organization $org"
  local app_env="$SECRETS/app.env"
  grep -v '^INQUEST_TELEMETRY_ORIGIN_ORGANIZATIONS=' "$app_env" >"$app_env.new" || true
  printf 'INQUEST_TELEMETRY_ORIGIN_ORGANIZATIONS={"%s": ["%s"]}\n' "$SIGNOZ_URL" "$org" >>"$app_env.new"
  chmod 600 "$app_env.new"
  mv "$app_env.new" "$app_env"
  compose up -d --force-recreate app
  wait_healthy app 180

  step "verify the dedicated connection (D038)"
  compose exec -T app python -m scripts.bind_signoz_tenant verify --organization "$org" \
    --workspace-url "$SIGNOZ_URL" --operator "$OPERATOR" --apply

  cat <<EOF

In Inquest, open the environment's telemetry settings and enter:
  URL:        $SIGNOZ_URL
  API key:    SIGNOZ_INQUEST_API_KEY in $SECRETS/signoz-admin.env
  Isolation:  dedicated
EOF
}

cmd_upgrade() {
  load_stack
  step "rebuild, migrate, restart (D041 release order)"
  compose build --quiet app proxy
  compose run --rm migrate 2>&1 | tail -n 1
  compose up -d app proxy
  wait_healthy app 180
  wait_healthy proxy 60
}

cmd_backup() {
  load_stack
  local dir="${INQUEST_BACKUP_DIR:-$ROOT/deploy/backups}" file
  umask 077
  mkdir -p "$dir"
  file="$dir/inquest-$(date +%Y%m%d-%H%M%S).dump"
  compose exec -T postgres sh -c \
    'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -Fc -U "$POSTGRES_USER" inquest' >"$file"
  echo "wrote $file. Copy it off this host, encrypted; keep $SECRETS (credential keys) apart from it."
}

case "${1:-}" in
  init) cmd_init ;;
  up) cmd_up ;;
  certs) cmd_certs ;;
  admit) shift; cmd_admit "$@" ;;
  bind-signoz) cmd_bind_signoz ;;
  upgrade) cmd_upgrade ;;
  backup) cmd_backup ;;
  status) load_stack; compose ps -a ;;
  logs) shift; load_stack; compose logs --tail 100 "$@" ;;
  down) load_stack; compose down ;;
  *) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
