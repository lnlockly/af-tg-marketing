#!/usr/bin/env python3
"""
cli/campaign_ctl.py — start / pause / inspect one campaign in the control-plane.

The SQLite DB *is* the control plane (no HTTP): flipping Campaign.status here is
what the asyncio daemon reads to decide whether to run a campaign's loops. This
CLI only writes that one status field (--start → active, --pause → paused) or
reports it (--status), never touching leads/dialogs directly.

--status also rolls up Lead.matched and Dialog.status counts so an operator (or
the agent) can see a campaign's shape at a glance. Prints compact JSON; no secrets.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# Make the package importable when run as `python cli/campaign_ctl.py` from repo root.
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


def _set_status(campaign_id: int, status: "db.CampaignStatus") -> dict:
    with db.get_session() as session:
        campaign = session.get(db.Campaign, campaign_id)
        if campaign is None:
            return {"ok": False, "campaign_id": campaign_id, "error": "campaign not found"}
        campaign.status = status
        session.add(campaign)
        session.commit()
        session.refresh(campaign)
        return {"ok": True, "campaign_id": campaign_id, "status": campaign.status.value}


def _status(campaign_id: int) -> dict:
    with db.get_session() as session:
        campaign = session.get(db.Campaign, campaign_id)
        if campaign is None:
            return {"ok": False, "campaign_id": campaign_id, "error": "campaign not found"}

        leads = session.exec(
            select(db.Lead).where(db.Lead.campaign_id == campaign_id)
        ).all()
        dialogs = session.exec(
            select(db.Dialog).where(db.Dialog.campaign_id == campaign_id)
        ).all()

        lead_counts = {"total": len(leads), "matched": 0, "classified": 0}
        for lead in leads:
            if lead.matched:
                lead_counts["matched"] += 1
            if lead.classified:
                lead_counts["classified"] += 1

        dialog_counts = {"total": len(dialogs)}
        for status_enum in db.DialogStatus:
            dialog_counts[status_enum.value] = 0
        for dialog in dialogs:
            key = dialog.status.value if hasattr(dialog.status, "value") else str(dialog.status)
            dialog_counts[key] = dialog_counts.get(key, 0) + 1

        return {
            "ok": True,
            "campaign_id": campaign_id,
            "status": campaign.status.value,
            "counts": {
                "campaign": {
                    "id": campaign.id,
                    "name": campaign.name,
                    "product": campaign.product,
                },
                "leads": lead_counts,
                "dialogs": dialog_counts,
            },
        }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Start, pause, or inspect a campaign in the tgengine control-plane."
    )
    parser.add_argument("--campaign", type=int, required=True,
                        help="Campaign.id in the tgengine DB")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--start", action="store_true",
                        help="set Campaign.status = active (daemon starts running it)")
    action.add_argument("--pause", action="store_true",
                        help="set Campaign.status = paused (daemon stops running it)")
    action.add_argument("--status", action="store_true",
                        help="print the campaign plus lead/dialog counts by status")
    parser.add_argument("--db", help="path to the SQLite DB (sets TGENGINE_DB; "
                                     "applied at startup before the DB engine binds)")
    args = parser.parse_args()

    try:
        if args.start:
            result = _set_status(args.campaign, db.CampaignStatus.active)
        elif args.pause:
            result = _set_status(args.campaign, db.CampaignStatus.paused)
        else:
            result = _status(args.campaign)
    except Exception as exc:  # surface any hard failure as JSON, never a raw traceback
        result = {"ok": False, "campaign_id": args.campaign,
                  "error": f"{type(exc).__name__}: {exc}"}

    print(json.dumps(result))
    if not result.get("ok"):
        sys.exit(1)


if __name__ == "__main__":
    main()
