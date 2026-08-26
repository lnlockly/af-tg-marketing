#!/usr/bin/env python3
"""
cli/hot_leads.py — the agent's "show me the leads" (read-only, no network).

The Ф2 surface for hot leads. Reads the SQLite control-plane straight through
tgengine.db and prints a compact JSON list of dialogs, newest/hottest first, each
joined to its Lead (who) and its last Message (what they last said):

  {dialog_id, lead, account_id, status, interest_score, last_message, last_message_at}

By default it shows `success` dialogs — those whose interest score crossed the
campaign threshold (the hot leads). `--status` narrows to any other DialogStatus
(new/sent/answered/inprogress/closed) or `all`. `--campaign` scopes to one
campaign. Read-only: it opens no Telegram client and sends nothing.

No HTTP — state is read straight from tgengine.db.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

# Make the package importable when run as `python cli/hot_leads.py` from repo root.
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

# Accepted --status values: every DialogStatus plus the "all" wildcard.
_STATUS_CHOICES = [s.value for s in db.DialogStatus] + ["all"]


def _lead_label(lead) -> Optional[str]:
    """Human-readable handle for a lead: @username, else first_name, else tg id."""
    if lead is None:
        return None
    if lead.username:
        return f"@{lead.username}"
    if lead.first_name:
        return lead.first_name
    if lead.tg_id is not None:
        return str(lead.tg_id)
    return None


def _iso(dt) -> Optional[str]:
    return dt.isoformat() if dt is not None else None


def _collect(campaign_id: Optional[int], status: str, limit: int) -> list[dict]:
    """Read Dialogs (optionally scoped by campaign/status) joined to Lead + last
    Message, hottest/newest first. Pure read via a sync SQLModel session."""
    rows: list[dict] = []
    with db.get_session() as session:
        stmt = select(db.Dialog)
        if campaign_id is not None:
            stmt = stmt.where(db.Dialog.campaign_id == campaign_id)
        if status != "all":
            stmt = stmt.where(db.Dialog.status == db.DialogStatus(status))
        # Hottest first, then most recently active, then newest dialog.
        stmt = stmt.order_by(
            db.Dialog.interest_score.desc(),
            db.Dialog.last_message_at.desc(),
            db.Dialog.id.desc(),
        ).limit(limit)

        for dialog in session.exec(stmt).all():
            lead = session.get(db.Lead, dialog.lead_id)
            last_msg = session.exec(
                select(db.Message)
                .where(db.Message.dialog_id == dialog.id)
                .order_by(db.Message.created_at.desc(), db.Message.id.desc())
                .limit(1)
            ).first()
            rows.append({
                "dialog_id": dialog.id,
                "lead": _lead_label(lead),
                "account_id": dialog.account_id,
                "status": dialog.status.value if dialog.status else None,
                "interest_score": dialog.interest_score,
                "last_message": last_msg.text if last_msg is not None else None,
                "last_message_at": _iso(dialog.last_message_at),
            })
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Show dialogs/leads from the tgengine control-plane (read-only)."
    )
    parser.add_argument("--campaign", type=int, default=None,
                        help="scope to one Campaign.id (default: all campaigns)")
    parser.add_argument("--status", default=db.DialogStatus.success.value,
                        choices=_STATUS_CHOICES,
                        help="dialog status to show, or 'all' (default: success = hot leads)")
    parser.add_argument("--limit", type=int, default=50,
                        help="max rows to return (default: 50)")
    parser.add_argument("--db", help="path to the SQLite DB (sets TGENGINE_DB; "
                                     "applied at startup before the DB engine binds)")
    args = parser.parse_args()

    rows = _collect(args.campaign, args.status, args.limit)
    print(json.dumps(rows))


if __name__ == "__main__":
    main()
