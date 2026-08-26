#!/usr/bin/env python3
"""
cli/warmup_ctl.py — control & inspect account warmup (DB-only, no network).

The warmup state machine lives on the Account row (warmup_phase + warmup_actions);
the asyncio daemon's warmup_loop performs the real MTProto actions and promotes an
account to `ready` once it has done ACTIONS_TO_READY actions. This CLI never touches
Telegram — it only reads/writes those control-plane columns:

  --start   queue the selected accounts for warmup: warmup_phase=cold, warmup_actions=0
  --status  report each selected account's warmup_phase / warmup_actions vs. the
            ACTIONS_TO_READY target (imported from tgengine.warmup — single source)

Selection is either --ids 1,2,3 or --all. Prints a compact JSON result; never prints
secrets (session/app_hash/device are omitted).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# Make the package importable when run as `python cli/warmup_ctl.py` from repo root.
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
from tgengine import warmup  # noqa: E402


def _parse_ids(raw: str) -> list[int]:
    ids: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            raise SystemExit(f"invalid --ids value: {part!r} (want comma-separated ints)")
    if not ids:
        raise SystemExit("--ids was empty")
    # de-dup, preserve order
    seen: set[int] = set()
    return [i for i in ids if not (i in seen or seen.add(i))]


def _select(session, ids: list[int] | None):
    """Return the selected accounts, ordered by id. ids=None means --all."""
    from sqlmodel import select
    stmt = select(db.Account)
    if ids is not None:
        stmt = stmt.where(db.Account.id.in_(ids))
    accounts = list(session.exec(stmt.order_by(db.Account.id)))
    return accounts


def _account_view(account) -> dict:
    """Non-secret snapshot of an account's warmup state."""
    phase = account.warmup_phase
    phase = phase.value if hasattr(phase, "value") else phase
    actions = account.warmup_actions or 0
    remaining = max(0, warmup.ACTIONS_TO_READY - actions)
    return {
        "id": account.id,
        "username": account.username,
        "is_active": account.is_active,
        "warmup_phase": phase,
        "warmup_actions": actions,
        "actions_to_ready": warmup.ACTIONS_TO_READY,
        "remaining": remaining,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Control/inspect account warmup via the SQLite control-plane "
                    "(DB only, no network). The daemon performs the actual warmup.",
    )
    sel = parser.add_mutually_exclusive_group(required=True)
    sel.add_argument("--ids", help="comma-separated Account ids, e.g. 1,2,3")
    sel.add_argument("--all", action="store_true", help="select every account")

    act = parser.add_mutually_exclusive_group(required=True)
    act.add_argument("--start", action="store_true",
                     help="queue accounts for warmup: warmup_phase=cold, warmup_actions=0")
    act.add_argument("--status", action="store_true",
                     help="report warmup_phase/warmup_actions vs ACTIONS_TO_READY")

    parser.add_argument("--intensity", choices=sorted(warmup.INTENSITY.keys()),
                        default="medium",
                        help="warmup intensity the daemon should use (informational here)")
    parser.add_argument("--db", help="path to the SQLite DB (sets TGENGINE_DB; "
                                     "applied at startup before the DB engine binds)")
    args = parser.parse_args()

    ids = None if args.all else _parse_ids(args.ids)

    db.init_db()
    with db.get_session() as session:
        accounts = _select(session, ids)

        if ids is not None:
            found = {a.id for a in accounts}
            missing = [i for i in ids if i not in found]
        else:
            missing = []

        if args.start:
            for account in accounts:
                account.warmup_phase = db.WarmupPhase.cold
                account.warmup_actions = 0
                session.add(account)
            session.commit()
            for account in accounts:
                session.refresh(account)

        result = {
            "ok": True,
            "action": "start" if args.start else "status",
            "intensity": args.intensity,
            "actions_to_ready": warmup.ACTIONS_TO_READY,
            "selected": len(accounts),
            "missing": missing,
            "accounts": [_account_view(a) for a in accounts],
        }

    print(json.dumps(result))


if __name__ == "__main__":
    main()
