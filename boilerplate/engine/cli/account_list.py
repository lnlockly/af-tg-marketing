#!/usr/bin/env python3
"""
cli/account_list.py — list accounts + their state (read-only).

Reads the SQLite control-plane straight (no HTTP, no Telegram connection) and prints
a JSON list summarizing each Account's identity, wrapping, and health/scheduling state.
Use it to see the fleet at a glance: who is active, warm, dressed (avatar/channel), and
who is currently on cooldown.

--active shows only is_active accounts; --all (default) shows every account.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# Make the package importable when run as `python cli/account_list.py` from repo root.
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


def _on_cooldown(account) -> bool:
    return account.cooldown_until is not None and account.cooldown_until > db.now()


def _iso(dt):
    return dt.isoformat() if dt is not None else None


def _tags(account) -> list:
    raw = account.tags
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _row(account) -> dict:
    warmup = account.warmup_phase
    status = account.status
    return {
        "account_id": account.id,
        "tg_id": account.tg_id,
        "first_name": account.first_name,
        "username": account.username,
        "country": account.country,
        "is_active": account.is_active,
        "status": status.value if hasattr(status, "value") else status,
        "spamblock_until": _iso(account.spamblock_until),
        "purpose": account.purpose,
        "group_name": account.group_name,
        "tags": _tags(account),
        "last_health_check": _iso(account.last_health_check),
        "warmup_phase": warmup.value if hasattr(warmup, "value") else warmup,
        "warmup_actions": account.warmup_actions,
        "has_avatar": bool(account.avatar_path),
        "channel_username": account.channel_username,
        "dialogs_started_today": account.dialogs_started_today,
        "on_cooldown": _on_cooldown(account),
    }


def _list(active_only: bool) -> list:
    with db.get_session() as session:
        stmt = select(db.Account)
        if active_only:
            stmt = stmt.where(db.Account.is_active == True)  # noqa: E712
        accounts = session.exec(stmt.order_by(db.Account.id)).all()
        return [_row(a) for a in accounts]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="List stored accounts and their state (read-only; no Telegram)."
    )
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--all", action="store_true",
                       help="list every account (default)")
    scope.add_argument("--active", action="store_true",
                       help="list only accounts with is_active=true")
    parser.add_argument("--db", help="path to the SQLite DB (sets TGENGINE_DB; "
                                     "applied at startup before the DB engine binds)")
    args = parser.parse_args()

    rows = _list(active_only=args.active)
    print(json.dumps(rows))


if __name__ == "__main__":
    main()
