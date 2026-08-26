#!/usr/bin/env python3
"""
cli/dialog_reply.py — operator takeover ("взять на себя").

When the AI auto-dialog isn't cutting it, a human operator sends a message into
a live Dialog by hand. This one-shot loads the Dialog, its Account (+Proxy) and
its Lead from the SQLite control-plane, optionally disables auto-mode (so the
inbound_loop stops auto-replying), connects the account through its proxy via
Telethon, sends the operator's text to the lead, records it as a
Message(sender=account), bumps last_message_at, and prints {ok, message_id}.

Ported from leads42 src/dialog/sender.processor.ts (sendReplyMessage) — logic,
not structure. No HTTP: reads state straight from tgengine.db, sends via
tgengine.tgclient.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

# Make the package importable when run as `python cli/dialog_reply.py` from repo root.
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


def _load(dialog_id: int, stop_auto: bool):
    """Load the Dialog, its Account (+Proxy) and its Lead (sync SQLModel session).

    If stop_auto is set, flip Dialog.auto_mode=false and persist it here so the
    takeover sticks even if the send later fails.
    """
    with db.get_session() as session:
        dialog = session.get(db.Dialog, dialog_id)
        if dialog is None:
            raise SystemExit(f"dialog {dialog_id} not found")

        if stop_auto and dialog.auto_mode:
            dialog.auto_mode = False
            session.add(dialog)
            session.commit()
            session.refresh(dialog)

        if dialog.account_id is None:
            raise SystemExit(f"dialog {dialog_id} has no account assigned")
        account = session.get(db.Account, dialog.account_id)
        if account is None:
            raise SystemExit(f"account {dialog.account_id} not found")

        proxy = None
        if account.proxy_id is not None:
            proxy = session.get(db.Proxy, account.proxy_id)

        lead = session.get(db.Lead, dialog.lead_id)
        if lead is None:
            raise SystemExit(f"lead {dialog.lead_id} not found")

        return dialog, account, proxy, lead


def _record_reply(dialog_id: int, text: str) -> None:
    """Persist the operator's outbound Message and bump last_message_at."""
    with db.get_session() as session:
        dialog = session.get(db.Dialog, dialog_id)
        if dialog is None:
            return
        session.add(db.Message(
            dialog_id=dialog_id,
            sender=db.MessageFrom.account,
            text=text,
        ))
        dialog.last_message_at = db.now()
        session.add(dialog)
        session.commit()


async def _run(account, proxy, ref, text: str) -> int:
    client = await tgclient.connect(account, proxy)
    try:
        return await tgclient.send_message(client, ref, text)
    finally:
        await tgclient.disconnect(client)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Operator takeover: send a hand-written message into a live dialog."
    )
    parser.add_argument("--dialog-id", type=int, required=True,
                        help="Dialog.id in the tgengine DB to reply into")
    parser.add_argument("--text", required=True, help="message body to send")
    parser.add_argument("--stop-auto", action="store_true",
                        help="disable AI auto-mode on this dialog (operator takes over)")
    parser.add_argument("--db", help="path to the SQLite DB (sets TGENGINE_DB; "
                                     "applied at startup before the DB engine binds)")
    args = parser.parse_args()

    dialog, account, proxy, lead = _load(args.dialog_id, args.stop_auto)

    ref = lead.username or lead.tg_id
    if ref is None:
        print(json.dumps({"ok": False, "error": "lead has no username or tg_id to send to"}))
        sys.exit(1)

    try:
        message_id = asyncio.run(_run(account, proxy, ref, args.text))
        _record_reply(dialog.id, args.text)
        result = {"ok": True, "message_id": message_id}
    except tgclient.AccountError as exc:
        result = {"ok": False, "error": str(exc)}
    except Exception as exc:  # surface any hard failure as JSON, never a raw traceback
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    print(json.dumps(result))
    if not result.get("ok"):
        sys.exit(1)


if __name__ == "__main__":
    main()
