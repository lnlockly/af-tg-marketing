#!/usr/bin/env python3
"""
cli/account_tag.py — one-shot: label/organize a stored account.

The owner labels accounts by purpose/group/tags/note so the single registry can be
sliced ("one for X, one for Y"). This sets whichever org fields are given on an
Account row, records an ActionLog via db.log_action, and prints the account's org
fields back as compact JSON. Write-through to the SQLite control-plane; no HTTP.

Only fields explicitly passed are touched. Passing an empty string clears a field
(tags → None). Omitting a flag leaves that field unchanged. Tags are stored as a
JSON list (json.dumps) built from a comma-separated argument.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# Make the package importable when run as `python cli/account_tag.py` from repo root.
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


def _tags_to_json(raw: str) -> str | None:
    """Comma-separated string → JSON list; empty string → None (clears the field)."""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        return None
    return json.dumps(parts)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Set organization fields (purpose/group/tags/note) on a stored account."
    )
    parser.add_argument("--account-id", type=int, required=True,
                        help="Account.id in the tgengine DB to label")
    parser.add_argument("--purpose", default=None,
                        help='e.g. "marketing" | "bots" | "scout" | "warmup" (empty clears)')
    parser.add_argument("--group", default=None,
                        help="named fleet/group the account belongs to (empty clears)")
    parser.add_argument("--tags", default=None,
                        help='comma-separated labels, e.g. "us,aged,premium" (empty clears)')
    parser.add_argument("--note", default=None, help="free-form note (empty clears)")
    parser.add_argument("--db", help="path to the SQLite DB (sets TGENGINE_DB; "
                                     "applied at startup before the DB engine binds)")
    args = parser.parse_args()

    changed = []
    with db.get_session() as session:
        account = session.get(db.Account, args.account_id)
        if account is None:
            print(json.dumps({"ok": False, "error": f"account {args.account_id} not found"}))
            sys.exit(1)

        if args.purpose is not None:
            account.purpose = args.purpose or None
            changed.append("purpose")
        if args.group is not None:
            account.group_name = args.group or None
            changed.append("group_name")
        if args.tags is not None:
            account.tags = _tags_to_json(args.tags)
            changed.append("tags")
        if args.note is not None:
            account.note = args.note or None
            changed.append("note")

        session.add(account)
        session.commit()
        session.refresh(account)

        org = {
            "account_id": account.id,
            "purpose": account.purpose,
            "group_name": account.group_name,
            "tags": json.loads(account.tags) if account.tags else None,
            "note": account.note,
        }

    db.log_action(args.account_id, "tag",
                  "set " + (",".join(changed) if changed else "(none)"), True)

    print(json.dumps({"ok": True, "changed": changed, **org}))


if __name__ == "__main__":
    main()
