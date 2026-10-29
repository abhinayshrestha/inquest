# Inquest web

React frontend for Inquest. Sign-in, organizations and per-environment credentials are
handled by the backend; this app only shows what the API returns.

## Setup

Requires Node ≥ 22.22. pnpm is pinned in `package.json` (`packageManager`) and provided
by Corepack:

```sh
corepack enable pnpm   # once per machine
pnpm install
```

## Development

Start the backend first (from the repository root, see the root README), then:

```sh
pnpm dev
```

The dev server proxies `/api/*` to the backend (`/api/health` → `/health`). The target
defaults to `http://127.0.0.1:8000`; override it with `INQUEST_API_URL`. The MCP mount
(`/api/mcp`) is deliberately not proxied: it is for investigator runtimes only.

No credentials or secrets belong in this package or in `VITE_*` variables: anything
`VITE_*` is compiled into browser code.

## Checks

```sh
pnpm lint
pnpm typecheck
pnpm test
pnpm build
pnpm format:check   # pnpm format to fix
```
