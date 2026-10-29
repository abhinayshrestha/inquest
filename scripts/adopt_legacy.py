"""Operator command: move ownerless legacy environments and their investigations into an
organization (D032). Run on the backend host; there is no HTTP equivalent.

Dry run by default: prints what would move and why it cannot. --apply moves it in one
transaction and records an audit event naming the operator. The owner must already be a
member of the organization (they signed in and accepted an invitation).

    uv run python -m scripts.adopt_legacy list
    uv run python -m scripts.adopt_legacy adopt --environment ENV_ID [--environment ...] \\
        --organization ORG_ID --owner EMAIL_OR_USER_ID --operator "Your Name" \\
        [--allow-process-credentials] [--apply]

--allow-process-credentials lets the adopted environments keep running investigations
with this backend's process-wide development credentials (.env) until they have their
own (Phase 7C). Without it they are configuration-required. Never grant it to an
environment whose telemetry the organization should not see.
"""

import argparse
import asyncio
import getpass
import uuid

from sqlalchemy import func, select

from src.db.session import SessionLocal, engine
from src.models import Environment, Investigation, InvestigationEnvironment, User
from src.services.adoption import AdoptionRefused, adopt, plan_adoption


async def _list() -> None:
    async with SessionLocal() as s:
        rows = await s.execute(
            select(
                Environment.id,
                Environment.name,
                func.count(InvestigationEnvironment.investigation_id),
            )
            .outerjoin(
                InvestigationEnvironment, InvestigationEnvironment.environment_id == Environment.id
            )
            .where(Environment.organization_id.is_(None))
            .group_by(Environment.id)
            .order_by(Environment.name)
        )
        ownerless = await s.scalar(
            select(func.count())
            .select_from(Investigation)
            .where(Investigation.organization_id.is_(None))
        )
    print("Ownerless (quarantined) environments:")
    for env_id, name, count in rows.all():
        print(f"  {env_id}  {name}  investigations={count}")
    print(f"Ownerless investigations: {ownerless}")


async def _owner(value: str) -> uuid.UUID:
    async with SessionLocal() as s:
        try:
            user = await s.get(User, uuid.UUID(value))
        except ValueError:
            matches = list(await s.scalars(select(User).where(User.email == value.lower())))
            user = matches[0] if len(matches) == 1 else None
    if user is None:
        raise SystemExit(f"no single user matches {value!r}")
    return user.id


async def _adopt(args: argparse.Namespace) -> None:
    owner = await _owner(args.owner)
    async with SessionLocal() as s:
        plan = await plan_adoption(
            s,
            environment_ids=args.environment,
            organization_id=args.organization,
            owner_user_id=owner,
            process_credentials=args.allow_process_credentials,
        )
        print(f"Organization: {plan.organization_id}  owner: {plan.owner_user_id}")
        for e in plan.environments:
            print(f"  environment {e.id}  {e.name}")
        print(f"  investigations: {len(plan.investigation_ids)}")
        print(f"  process-wide development credentials: {plan.process_credentials}")
        for problem in plan.problems:
            print(f"  REFUSED: {problem}")
        if not args.apply:
            print("Dry run; nothing changed. Re-run with --apply.")
            return
        try:
            event = await adopt(s, plan, actor=f"{args.operator} (os:{getpass.getuser()})")
        except AdoptionRefused:
            raise SystemExit("adoption refused; nothing changed") from None
    print(f"Adopted. Audit event {event.id}.")


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    a = sub.add_parser("adopt")
    a.add_argument("--environment", type=uuid.UUID, action="append", required=True)
    a.add_argument("--organization", type=uuid.UUID, required=True)
    a.add_argument("--owner", required=True)
    a.add_argument("--operator", required=True, help="who is running this, for the audit log")
    a.add_argument("--allow-process-credentials", action="store_true")
    a.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        await (_list() if args.command == "list" else _adopt(args))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
