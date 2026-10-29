"""Operator command: re-encrypt stored environment credentials with the active key (D034).
Run on the backend host; there is no HTTP equivalent.

Rotation:
  1. Add a new key to INQUEST_CREDENTIAL_KEYS (keep the old ones) and point
     INQUEST_CREDENTIAL_ACTIVE_KEY at it; restart the backend. New writes use it at once.
  2. Run this command (dry run first) to re-seal existing rows under the new key.
  3. Only after it reports nothing left to re-encrypt, remove the old key and restart.

It also recomputes connection fingerprints, which only changes anything if
INQUEST_CREDENTIAL_FINGERPRINT_KEY was replaced. All or nothing: refused while any row
cannot be decrypted with the configured keys (it names those rows, never their contents).

    uv run python -m scripts.rotate_credential_key --operator "Your Name" [--apply]
"""

import argparse
import asyncio
import getpass

from src.credentials.settings import CredentialStorageUnavailable, load_credential_config
from src.db.session import SessionLocal, engine
from src.services.credentials import rekey


async def _run(args: argparse.Namespace) -> None:
    try:
        config = load_credential_config()
    except CredentialStorageUnavailable as exc:
        raise SystemExit(f"credential storage is not configured: {exc}") from None
    async with SessionLocal() as s:
        report = await rekey(
            s,
            config,
            actor=f"{args.operator} (os:{getpass.getuser()})",
            apply=args.apply,
        )
    print(f"Active key: {config.active_key_id}")
    print(f"  stored credentials: {report.total}")
    print(f"  to re-encrypt: {report.reencrypt}")
    print(f"  fingerprints to recompute: {report.refingerprint}")
    for environment_id, kind in report.unreadable:
        print(f"  UNREADABLE: environment {environment_id} {kind}")
    if report.unreadable:
        raise SystemExit("refused: some credentials cannot be decrypted; nothing changed")
    if report.applied:
        print("Applied.")
    elif args.apply:
        print("Nothing to change.")
    else:
        print("Dry run; nothing changed. Re-run with --apply.")


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--operator", required=True, help="who is running this, for the audit log")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        await _run(args)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
