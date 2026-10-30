# Bundled SigNoz

SigNoz Community for the single-client stack (`deploy/compose.signoz.yaml`,
docs/deployment.md, D044). Inquest queries it as `http://signoz:8080`; clients send OTLP
through the proxy (`deploy/nginx/signoz.conf.template`).

## Source

SigNoz no longer ships compose files (its `deploy/` was deprecated for
[Foundry](https://github.com/SigNoz/foundry)). These files are Foundry's generated
Docker Compose example, `docs/examples/docker/compose/pours/deployment/` at Foundry
`v0.4.0`:

| File | Upstream | Changed |
|---|---|---|
| `ingester/ingester.yaml`, `ingester/opamp.yaml` | verbatim | no |
| `telemetrykeeper/clickhousekeeper/keeper-0.yaml` | verbatim | no |
| `telemetrystore/clickhouse/config-0-0.yaml`, `functions.yaml` | verbatim | no |
| `compose.yaml` | `compose.yaml` | yes, below |

Images (tag and multi-arch digest, 2026-10-29):

| Service | Image |
|---|---|
| `signoz-signoz-0` | `signoz/signoz:v0.148.1` |
| `ingester`, `signoz-telemetrystore-migrator` | `signoz/signoz-otel-collector:v0.144.15` |
| ClickHouse, user scripts | `clickhouse/clickhouse-server:25.12.5` |
| keeper | `clickhouse/clickhouse-keeper:25.12.5` |
| metastore | `postgres:16` |

## Changes to `compose.yaml`

- Images pinned by tag and digest (upstream: `latest`).
- No published ports (upstream: 4317, 4318, 8080 on the host). Only the proxy publishes.
- Networks: `signoz-internal` (all components, internal), `telemetry` (SigNoz API with
  alias `signoz`, ingester with alias `signoz-ingester`; shared with the app and the proxy,
  internal), `signoz-fetch` (outbound, the user-scripts job only). Upstream: one network
  with a fixed global name.
- No `container_name` and no global volume/network names, so the stack is project-scoped
  (e2e and local stacks can run side by side).
- Metastore password and SigNoz's JWT secret come from secrets files (`signoz.env`,
  `signoz-metastore.env`) instead of upstream's literal `signoz` / default.
- `SIGNOZ_ANALYTICS_ENABLED=false`, `SIGNOZ_STATSREPORTER_ENABLED=false`; external and
  ingestion URLs set to the stack's host names.
- The user-scripts job verifies the `histogramQuantile` download against pinned
  SHA-256 sums and skips it once installed (upstream: unverified, every start).
- `no-new-privileges` on every service; ClickHouse memory limit
  (`SIGNOZ_CLICKHOUSE_MEMORY`, default 4g); the ingester waits for the migrator.

ClickHouse's `default` user has an empty password and listens on all addresses
(upstream config, unchanged): only SigNoz components may ever join `signoz-internal`.

## Behaviour to know

- **The first SigNoz account owns it.** `POST /api/v1/register` is open until the first
  user exists. Register the operator account from the private network before the proxy
  exposes `signoz.<domain>`.
- **No OTLP until then.** The ingester takes its pipeline from SigNoz over OpAMP; until an
  organization exists SigNoz answers `cannot create agent without orgId` and the ingester
  runs without receivers (the proxy answers 502). About 20 s after registration it listens
  on 4317/4318.
- Inquest needs a SigNoz API key: a service account with the `signoz-viewer` role and a
  key (SigNoz UI, Settings → Service accounts). Viewer is enough to query.

## Upgrading

Regenerate with the Foundry version that matches the new SigNoz release
(`foundryctl forge -f casting.yaml` with `flavor: compose`), diff against these files,
carry the changes above over, pin the new digests, and run the local checks in
docs/deployment.md. Recreate the whole SigNoz stack in one command (`up -d
--force-recreate`): the keeper and ClickHouse must restart together.
