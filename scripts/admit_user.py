"""Operator command: decide who may use Inquest under INQUEST_AUTH_ADMISSION=approval
(D042), and disable or re-enable accounts under any policy (D043; docs/deployment.md).
Run on the backend host; there is no HTTP equivalent.

Signing in with the identity provider proves who someone is; this decides whether they may
use Inquest. Approve the first organization creator by the email their provider verifies:
their first sign-in claims the approval for their account (issuer and subject) for good.
They create an organization and invite teammates, whose invitation links admit them.

Dry run by default; --apply writes and records an audit event naming the operator.

    uv run python -m scripts.admit_user list
    uv run python -m scripts.admit_user approve --email ada@example.com --operator "Name" [--apply]
    uv run python -m scripts.admit_user approve --user USER_ID --operator "Name" [--apply]
    uv run python -m scripts.admit_user revoke --user USER_ID --operator "Name" [--apply]
    uv run python -m scripts.admit_user revoke --email ada@example.com --operator "Name" [--apply]
    uv run python -m scripts.admit_user admit-members --operator "Name" [--apply]
    uv run python -m scripts.admit_user disable --user USER_ID --operator "Name" [--apply]
    uv run python -m scripts.admit_user enable --user USER_ID --operator "Name" [--apply]

`revoke --user` withdraws admission and ends the user's sessions; memberships and
ownership are unchanged (transfer environments first if the user is leaving for good).
`disable` suspends the account under every policy (an incident, a pause): sessions and
open streams end, no sign-in works, whatever approval or invitation exists; nothing is
deleted or transferred, and environments the user owns keep running on their
credentials for the other members. `enable` lifts it; no old session comes back, and the
admission decision is as it was.
`admit-members` admits every existing member who has no decision yet: use it once when
turning approval on for a database that already has users, and only if they all should
keep access.
"""

import argparse
import asyncio
import getpass
import sys
import uuid

from src.db.session import SessionLocal, engine
from src.services import admissions


async def _list() -> None:
    async with SessionLocal() as s:
        rows = await admissions.list_admissions(s)
    if not rows:
        print("no admission decisions")
    for admission, user in rows:
        state = "withdrawn" if admission.revoked_at else "live"
        if user and user.disabled_at:
            state += ", disabled"
        who = f"user {user.id} <{user.email or '-'}>" if user else f"unclaimed <{admission.email}>"
        print(f"{admission.id}  {state:19}  {admission.source:10}  {who}")


async def _change(args: argparse.Namespace) -> None:
    actor = f"operator:{args.operator} ({getpass.getuser()})"
    user = uuid.UUID(args.user) if args.user else None
    async with SessionLocal() as s:
        try:
            if args.command == "admit-members":
                change = await admissions.admit_members(s, actor=actor)
            elif args.command == "disable":
                change = await admissions.disable_user(s, user, actor=actor)
            elif args.command == "enable":
                change = await admissions.enable_user(s, user, actor=actor)
            elif args.command == "approve":
                change = await (
                    admissions.approve_user(s, user, actor=actor)
                    if user
                    else admissions.approve_email(s, args.email, actor=actor)
                )
            else:
                change = await (
                    admissions.revoke_user(s, user, actor=actor)
                    if user
                    else admissions.revoke_email(s, args.email, actor=actor)
                )
        except admissions.AdmissionRefused as exc:
            sys.exit(f"refused: {exc}")
        if args.apply:
            await s.commit()
        else:
            await s.rollback()
    print(("applied: " if args.apply else "plan (dry run; --apply to write): ") + change.action)
    for name in ("admission_id", "user_id"):
        if getattr(change, name):
            print(f"  {name}: {getattr(change, name)}")
    if args.command in ("revoke", "disable") and user:
        print(
            f"  sessions ended: {change.sessions_ended}; unchanged: {change.memberships} "
            f"membership(s), {change.owned_environments} owned environment(s), "
            f"{change.pending_transfers} pending transfer(s)"
        )
        if change.owned_environments:
            print(
                "  their environments keep running on their credentials for the other "
                "members; see docs/deployment.md (orphaned owners)"
            )
    elif args.command == "enable":
        print(f"  sessions ended: {change.sessions_ended}")
    for member in change.users:
        print(f"  user {member}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    for name in ("approve", "revoke"):
        p = sub.add_parser(name)
        target = p.add_mutually_exclusive_group(required=True)
        target.add_argument("--email")
        target.add_argument("--user")
        p.add_argument("--operator", required=True)
        p.add_argument("--apply", action="store_true")
    p = sub.add_parser("admit-members")
    p.add_argument("--operator", required=True)
    p.add_argument("--apply", action="store_true")
    for name in ("disable", "enable"):
        p = sub.add_parser(name)
        p.add_argument("--user", required=True)
        p.add_argument("--operator", required=True)
        p.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        await (_list() if args.command == "list" else _change(args))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
