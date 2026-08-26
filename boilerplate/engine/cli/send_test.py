#!/usr/bin/env python3
"""
cli/send_test.py — one-shot: prove an account can send a message.

Loads an Account (+ its Proxy) from the SQLite control-plane, connects through the
account's proxy via Telethon, sends one message to the given target, and prints a
compact JSON result: {ok, message_id}. Used as the live proof that a freshly
ingested/warmed account can actually reach Telegram and deliver a DM.

No HTTP — reads state straight from tgengine.db, sends via tgengine.tgclient.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

# Make the package importable when run as `python cli/send_test.py` from repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# db.py binds its SQLite engine to TGENGINE_DB at import time, so honor --db BEFORE
# importing the package (a tiny pre-scan; argparse still owns real validation below).
_argv = sys.argv[1:]
for _i, _a in enumerate(_argv):
    if _a == "--db" and _i + 1 < len(_argv):
        os.environ["TGENGINE_DB"] = _argv[_i + 1]
    elif _a.startswith("--db="):
        os.environ["TGENGINE_DB"] = _a[len("--db="):]

from tgengine import db  # noqa: E402
from tgengine import tgclient  # noqa: E402


def _load(account_id: int):
    """Load the Account and its Proxy (sync SQLModel session)."""
    with db.get_session() as session:
        account = session.get(db.Account, account_id)
        if account is None:
            raise SystemExit(f"account {account_id} not found")
        proxy = None
        if account.proxy_id is not None:
            proxy = session.get(db.Proxy, account.proxy_id)
        return account, proxy


async def _run(account, proxy, to: str, text: str) -> dict:
    client = await tgclient.connect(account, proxy)
    try:
        message_id = await tgclient.send_message(client, to, text)
        return {"ok": True, "message_id": message_id}
    finally:
        await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Send one test message from a stored account to prove it can send."
    )
    parser.add_argument("--account-id", type=int, required=True,
                        help="Account.id in the tgengine DB to send from")
    parser.add_argument("--to", required=True,
                        help="target: @username, t.me link, or numeric id")
    parser.add_argument("--text", required=True, help="message body to send")
    parser.add_argument("--db", help="path to the SQLite DB (sets TGENGINE_DB; "
                                     "applied at startup before the DB engine binds)")
    args = parser.parse_args()

    account, proxy = _load(args.account_id)

    try:
        result = asyncio.run(_run(account, proxy, args.to, args.text))
    except tgclient.AccountError as exc:
        result = {"ok": False, "error": str(exc)}
    except Exception as exc:  # surface any hard failure as JSON, never a raw traceback
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    print(json.dumps(result))
    if not result.get("ok"):
        sys.exit(1)


if __name__ == "__main__":
    main()
