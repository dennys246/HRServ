"""Mint a new API key: print the plaintext once and store the argon2 hash.

The plaintext leaves this process on stdout exactly once — capture it into a
password manager immediately, because it is never recoverable from the database.

Usage:
    uv run hrserv-mint-key --label flask-frontend
    uv run hrserv-mint-key --label monitor --scope healthz
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import asyncpg

from hrserv.auth import generate_secret, hash_secret
from hrserv.config import load_settings
from hrserv.db import close_pool, create_pool, insert_api_key


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Mint a new HRServ API key and store its hash in the api_keys table."
    )
    parser.add_argument(
        "--label",
        required=True,
        help="Short identifier for the key, e.g. 'flask-frontend'. Becomes api_keys.id.",
    )
    parser.add_argument(
        "--scope",
        action="append",
        default=None,
        dest="scopes",
        help="Scope to grant; repeatable. Defaults to 'ingest'.",
    )
    return parser.parse_args(argv)


class LabelAlreadyExistsError(Exception):
    """Raised when --label collides with an existing api_keys.id."""


async def _mint(label: str, scopes: list[str]) -> str:
    """Insert a fresh key and return the plaintext to print.

    Raises LabelAlreadyExistsError if the label is taken; the caller exits
    cleanly so the operator sees a clear "rotate or pick a new label" message
    instead of an asyncpg traceback.
    """
    settings = load_settings()
    pool = await create_pool(settings.database_url)
    try:
        plaintext = generate_secret()
        try:
            await insert_api_key(
                pool,
                label=label,
                key_hash=hash_secret(plaintext),
                scopes=scopes,
            )
        except asyncpg.UniqueViolationError as e:
            raise LabelAlreadyExistsError(label) from e
        return plaintext
    finally:
        await close_pool(pool)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    scopes = args.scopes if args.scopes else ["ingest"]

    try:
        plaintext = asyncio.run(_mint(args.label, scopes))
    except LabelAlreadyExistsError:
        print(
            f"ERROR: an API key labeled {args.label!r} already exists.\n"
            f"To rotate it, first revoke the old one:\n"
            f"  UPDATE api_keys SET revoked_at = now() WHERE id = '{args.label}';\n"
            f"Then re-run with a NEW label (audit trails are easier with distinct labels).",
            file=sys.stderr,
        )
        return 1

    print(
        f"\nMinted API key for label={args.label!r} scopes={scopes}.\n"
        f"Copy the secret below into your password manager NOW — it will not be shown again:\n",
        file=sys.stderr,
    )
    print(plaintext)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
