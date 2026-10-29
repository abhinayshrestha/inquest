"""Operator command: assign ingestion keys of a shared SigNoz Cloud (or self-hosted
Enterprise) workspace to an organization, for provider-scoped connections (D037). Run on
the backend host; there is no HTTP equivalent.

Before assigning, in SigNoz: one ingestion key per tenant, a custom role granting only
`builder_query/signoz.workspace.key.id/<that key's id>` on logs, traces and metrics, a
service account with only that role (no managed role: roles add up), and an API key for
it, which the environment owner then enters with isolation "provider_scoped". Inquest
checks at save time that the key is confined to the assigned ids.

Also verifies dedicated connections (D038): outside local development a dedicated
connection is usable only once an operator has confirmed, out of band, that the instance
or workspace at that URL belongs to that organization alone (who runs it; that no other
organization's keys work there). Inquest cannot check that: other DNS names can reach the
same backend.

`review` lists every tenant SigNoz connection with its standing under the isolation
policy (organization and environment ids, fingerprint prefix; never URLs or keys), e.g.
cross-organization reuse saved before D037, which is refused at run time. Remediate a
refused connection by one of:

- verify it for the organization it belongs to (its other users stay refused);
- assign ingestion keys to each organization (bind) and have each owner save the
  connection again as provider_scoped;
- have the owner of a refused connection replace it with their own SigNoz.

Nothing here deletes credentials or investigation history.

Dry run by default; --apply writes and records an audit event naming the operator.

    uv run python -m scripts.bind_signoz_tenant list
    uv run python -m scripts.bind_signoz_tenant review
    uv run python -m scripts.bind_signoz_tenant bind --organization ORG_ID \\
        --workspace-url https://WORKSPACE --key-id KEY_ID [--key-id ...] \\
        --operator "Your Name" [--apply]
    uv run python -m scripts.bind_signoz_tenant unbind ... (same arguments)
    uv run python -m scripts.bind_signoz_tenant verify --organization ORG_ID \\
        --workspace-url https://SIGNOZ --operator "Your Name" [--apply]
    uv run python -m scripts.bind_signoz_tenant unverify ... (same arguments)

Changes take effect at the organization's next telemetry query.
"""

import argparse
import asyncio
import getpass
import sys
import uuid

from sqlalchemy import select

from src.credentials import resolver
from src.credentials.settings import CredentialStorageUnavailable, load_credential_config
from src.db.session import SessionLocal, engine
from src.models import SigNozDedicatedConnection, SigNozTenantKey
from src.services import telemetry_keys


async def _list() -> None:
    async with SessionLocal() as s:
        rows = await s.execute(
            select(
                SigNozTenantKey.organization_id,
                SigNozTenantKey.connection_fingerprint,
                SigNozTenantKey.key_id,
            ).order_by(SigNozTenantKey.organization_id, SigNozTenantKey.key_id)
        )
        verified = await s.execute(
            select(
                SigNozDedicatedConnection.organization_id,
                SigNozDedicatedConnection.connection_fingerprint,
                SigNozDedicatedConnection.verified_by,
            ).order_by(SigNozDedicatedConnection.organization_id)
        )
    print("Assigned ingestion keys (organization, workspace fingerprint, key id):")
    for organization_id, fingerprint, key_id in rows.all():
        print(f"  {organization_id}  {fingerprint[:16]}  {key_id}")
    print("Verified dedicated connections (organization, fingerprint, verified by):")
    for organization_id, fingerprint, verified_by in verified.all():
        print(f"  {organization_id}  {fingerprint[:16]}  {verified_by}")


async def _review() -> None:
    async with SessionLocal() as s:
        reviewed = await resolver.isolation_review(s)
    print("SigNoz connections (fingerprint, organization, environment, isolation, standing):")
    for row in reviewed:
        fingerprint = (row.fingerprint or "-")[:16]
        standing = (
            "ok" if row.problem is None else f"REFUSED: {resolver.ISOLATION_PROBLEMS[row.problem]}"
        )
        print(
            f"  {fingerprint:16}  {row.organization_id}  {row.environment_id}  "
            f"{row.isolation:15}  {standing}"
        )
    refused = sum(row.problem is not None for row in reviewed)
    print(f"{refused} of {len(reviewed)} refused")


async def _dedication(args: argparse.Namespace) -> None:
    try:
        config = load_credential_config()
    except CredentialStorageUnavailable as exc:
        sys.exit(f"refused: {exc}")
    actor = f"operator:{args.operator} ({getpass.getuser()})"
    change = (
        telemetry_keys.verify_dedicated
        if args.command == "verify"
        else telemetry_keys.unverify_dedicated
    )
    async with SessionLocal() as s:
        try:
            plan = await change(
                s, config, uuid.UUID(args.organization), args.workspace_url, actor=actor,
                apply=args.apply,
            )  # fmt: skip
        except telemetry_keys.BindingRefused as exc:
            sys.exit(f"refused: {exc}")
    verb = "verified" if args.command == "verify" else "verification removed"
    print(f"{'Applied' if plan.applied else 'Dry run'}: {verb if plan.change else 'no change'}")
    if plan.others_refused:
        print(f"{plan.others_refused} stored connections of other organizations are refused")


async def _change(args: argparse.Namespace) -> None:
    try:
        config = load_credential_config()
    except CredentialStorageUnavailable as exc:
        sys.exit(f"refused: {exc}")
    actor = f"operator:{args.operator} ({getpass.getuser()})"
    change = telemetry_keys.bind if args.command == "bind" else telemetry_keys.unbind
    async with SessionLocal() as s:
        try:
            plan = await change(
                s, config, uuid.UUID(args.organization), args.workspace_url, args.key_id,
                actor=actor, apply=args.apply,
            )  # fmt: skip
        except telemetry_keys.BindingRefused as exc:
            sys.exit(f"refused: {exc}")
    changed = plan.add or plan.remove
    verb = "assigned" if args.command == "bind" else "removed"
    print(f"{'Applied' if plan.applied else 'Dry run'}: {verb} {changed or 'nothing'}")
    if plan.others_refused:
        print(
            f"{plan.others_refused} dedicated connections of other organizations to this "
            "workspace are refused while it has assigned keys (see review)"
        )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    sub.add_parser("review")
    for name in ("bind", "unbind"):
        command = sub.add_parser(name)
        command.add_argument("--organization", required=True)
        command.add_argument("--workspace-url", required=True)
        command.add_argument("--key-id", action="append", required=True)
        command.add_argument("--operator", required=True)
        command.add_argument("--apply", action="store_true")
    for name in ("verify", "unverify"):
        command = sub.add_parser(name)
        command.add_argument("--organization", required=True)
        command.add_argument("--workspace-url", required=True)
        command.add_argument("--operator", required=True)
        command.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "list":
            await _list()
        elif args.command == "review":
            await _review()
        elif args.command in ("verify", "unverify"):
            await _dedication(args)
        else:
            await _change(args)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
