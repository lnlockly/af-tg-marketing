#!/usr/bin/env python3
"""
cli/account_logs.py — read-only: the per-account audit trail.

Prints ActionLog rows (what each account did, when, and whether it worked) as a
compact JSON list, newest-first, with optional filters by account and/or action.
Reads state straight from the SQLite control-plane; never writes, never prints
secrets/sessions.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# Make the package importable when run as `python cli/account_logs.py` from repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# db.py binds its SQLite engine to TGENGINE_DB at import time, so honor --db BEFORE
# importing the package (a tiny pre-scan; argparse still owns real validation below).
_argv = sys.argv[1:]
for _i, _a in enumerate(_argv):
    if _a == "--db" and _i + 1 < len(_argv):
        os.environ["TGENGINE_DB"] = _argv[_i + 1]
    elif _a.startswith("--db="):
        os.environ["TGENGINE_DB"] = _a[len("--db="):]

from sqlmodel import select  # noqa: E402

from tgengine import db  # noqa: E402


def _iso(dt) -> str | None:
    return dt.isoformat() if dt is not None else None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Print the ActionLog audit trail (newest-first) as JSON."
    )
    parser.add_argument("--account-id", type=int, default=None,
                        help="filter to one Account.id")
    parser.add_argument("--action", default=None,
                        help="filter to one action type, e.g. health_check | tag | ingest")
    parser.add_argument("--limit", type=int, default=50,
                        help="max rows to return (default 50)")
    parser.add_argument("--db", help="path to the SQLite DB (sets TGENGINE_DB; "
                                     "applied at startup before the DB engine binds)")
    args = parser.parse_args()

    with db.get_session() as session:
        stmt = select(db.ActionLog)
        if args.account_id is not None:
            stmt = stmt.where(db.ActionLog.account_id == args.account_id)
        if args.action is not None:
            stmt = stmt.where(db.ActionLog.action == args.action)
        stmt = stmt.order_by(db.ActionLog.id.desc()).limit(args.limit)
        rows = session.exec(stmt).all()

    out = [
        {
            "id": r.id,
            "account_id": r.account_id,
            "action": r.action,
            "detail": r.detail,
            "ok": r.ok,
            "created_at": _iso(r.created_at),
        }
        for r in rows
    ]

    print(json.dumps(out))


if __name__ == "__main__":
    main()
