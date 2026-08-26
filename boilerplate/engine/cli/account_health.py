#!/usr/bin/env python3
"""
cli/account_health.py — health-check accounts and write their status back.

For each selected Account, connect through its proxy and run tgengine.health.
health_check (which classifies dead/terminated/banned/cooldown via AccountError
and otherwise probes @SpamBot for a spamblock). The result is persisted onto the
Account row (status / spamblock_until / last_health_check / is_active) and appended
to the ActionLog audit trail. Prints a compact JSON list — never a session/secret.

Accounts are checked ONE AT A TIME (gentle on the network / anti-flood). No HTTP:
reads/writes the SQLite control-plane straight, connects via tgengine.tgclient.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

# Make the package importable when run as `python cli/account_health.py` from repo root.
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

# is_active states: the account is usable (healthy or a temporary cooldown).
_ACTIVE_STATES = {db.AccountStatus.active.value, db.AccountStatus.cooldown.value}


def _iso(dt):
    return dt.isoformat() if dt is not None else None


def _parse_ids(raw: str) -> list:
    """Parse a comma/space-separated --ids list into a de-duped ordered int list."""
    ids = []
    for chunk in (raw or "").replace(",", " ").split():
        try:
            value = int(chunk)
        except ValueError:
            raise SystemExit(f"invalid account id: {chunk!r}")
        if value not in ids:
            ids.append(value)
    return ids


def _select_ids(ids_arg, all_flag: bool) -> list:
    """Resolve the set of Account ids to check (validating that they exist)."""
    with db.get_session() as session:
        if all_flag:
            rows = session.exec(select(db.Account.id).order_by(db.Account.id)).all()
            return list(rows)
        wanted = _parse_ids(ids_arg)
        present = set(session.exec(select(db.Account.id)).all())
        missing = [i for i in wanted if i not in present]
        if missing:
            raise SystemExit(f"account(s) not found: {', '.join(map(str, missing))}")
        return wanted


def _load(account_id: int):
    """Load a detached Account snapshot + its Proxy (so no session is held open
    across the network round-trip of the health check)."""
    with db.get_session() as session:
        account = session.get(db.Account, account_id)
        if account is None:
            return None, None
        proxy = None
        if account.proxy_id is not None:
            proxy = session.get(db.Proxy, account.proxy_id)
        session.expunge_all()
        return account, proxy


def _to_status(status_value) -> db.AccountStatus:
    """Coerce a health-check status (string or enum) into the AccountStatus enum,
    falling back to `unknown` for anything unexpected."""
    if isinstance(status_value, db.AccountStatus):
        return status_value
    try:
        return db.AccountStatus(status_value)
    except (ValueError, KeyError):
        return db.AccountStatus.unknown


def _persist(account_id: int, status: db.AccountStatus, spamblock_until) -> bool:
    """Write the check result back onto the Account row. Returns is_active."""
    is_active = status.value in _ACTIVE_STATES
    with db.get_session() as session:
        account = session.get(db.Account, account_id)
        if account is None:
            return is_active
        account.status = status
        account.spamblock_until = spamblock_until
        account.last_health_check = db.now()
        account.is_active = is_active
        session.add(account)
        session.commit()
    return is_active


async def _check_one(account_id: int) -> dict:
    """Run one account's health check and persist the result."""
    # Import health lazily: it's a sibling module owned separately, and keeping the
    # import out of module scope means --help / py_compile work even before it lands.
    from tgengine import health  # noqa: WPS433

    account, proxy = _load(account_id)
    if account is None:
        db.log_action(account_id, "health_check", "not_found", False)
        return {"account_id": account_id, "status": None,
                "spamblock_until": None, "detail": "account not found"}

    try:
        result = await health.health_check(account, proxy)
    except Exception as exc:  # never let one account abort the batch
        status = db.AccountStatus.unknown
        is_active = _persist(account_id, status, None)
        db.log_action(account_id, "health_check", status.value, False)
        return {"account_id": account_id, "status": status.value,
                "spamblock_until": None,
                "detail": f"{type(exc).__name__}: {exc}", "is_active": is_active}

    status = _to_status(result.get("status"))
    spamblock_until = result.get("spamblock_until")
    is_active = _persist(account_id, status, spamblock_until)
    db.log_action(account_id, "health_check", status.value, is_active)
    return {
        "account_id": account_id,
        "status": status.value,
        "spamblock_until": _iso(spamblock_until),
        "detail": result.get("detail"),
        "is_active": is_active,
    }


async def _run(ids: list) -> list:
    rows = []
    for account_id in ids:  # one at a time — gentle on the network / anti-flood
        rows.append(await _check_one(account_id))
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Health-check accounts (spamblock/dead/banned/cooldown) and "
                    "write status back to the DB."
    )
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--ids", help="comma-separated Account ids to check, e.g. 1,2,3")
    scope.add_argument("--all", action="store_true", help="check every account")
    parser.add_argument("--db", help="path to the SQLite DB (sets TGENGINE_DB; "
                                     "applied at startup before the DB engine binds)")
    args = parser.parse_args()

    ids = _select_ids(args.ids, args.all)
    rows = asyncio.run(_run(ids))
    print(json.dumps(rows))


if __name__ == "__main__":
    main()
