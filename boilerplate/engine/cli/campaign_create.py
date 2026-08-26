#!/usr/bin/env python3
"""
cli/campaign_create.py — create a Campaign row in the SQLite control-plane.

A Campaign is the top-level config for one outreach loop: what we sell (product),
who we target (audience), and the prompts + pacing/scoring knobs the daemon uses
to DM, reply, and score leads. This CLI just inserts one Campaign(status=draft);
targets/leads/dialogs are attached later by the parse/match/DM loops.

No HTTP — writes state straight to tgengine.db via tgengine.db (SQLModel).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# Make the package importable when run as `python cli/campaign_create.py` from repo root.
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


def _create(args) -> dict:
    db.init_db()
    campaign = db.Campaign(
        name=args.name,
        product=args.product,
        audience=args.audience,
        first_message_prompt=args.first_message_prompt,
        reply_prompt=args.reply_prompt,
        work_start=args.work_start,
        work_end=args.work_end,
        max_new_per_account_per_day=args.max_per_account_per_day,
        msg_delay_min=args.msg_delay_min,
        msg_delay_max=args.msg_delay_max,
        interest_threshold=args.interest_threshold,
        notify_chat_id=args.notify_chat_id,
        status=db.CampaignStatus.draft,
    )
    with db.get_session() as session:
        session.add(campaign)
        session.commit()
        session.refresh(campaign)
        return {"ok": True, "campaign_id": campaign.id, "name": campaign.name}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a draft Campaign row in the tgengine control-plane DB."
    )
    parser.add_argument("--name", required=True, help="human label for the campaign")
    parser.add_argument("--product", required=True, help="what we sell")
    parser.add_argument("--audience", required=True,
                        help="who the lead is (match criteria, natural language)")
    parser.add_argument("--first-message-prompt", required=True,
                        help="prompt used to generate the opening DM")
    parser.add_argument("--reply-prompt", required=True,
                        help="prompt used to generate AI auto-replies")
    parser.add_argument("--work-start", type=int, default=9,
                        help="earliest local hour to send (default 9)")
    parser.add_argument("--work-end", type=int, default=21,
                        help="latest local hour to send (default 21)")
    parser.add_argument("--max-per-account-per-day", type=int, default=20,
                        help="new dialogs per account per day (default 20)")
    parser.add_argument("--msg-delay-min", type=int, default=40,
                        help="min seconds between sends (default 40)")
    parser.add_argument("--msg-delay-max", type=int, default=180,
                        help="max seconds between sends (default 180)")
    parser.add_argument("--interest-threshold", type=int, default=8,
                        help="interest score >= this → hot lead (default 8)")
    parser.add_argument("--notify-chat-id",
                        help="chat id where hot leads are pushed (the agent's chat)")
    parser.add_argument("--db", help="path to the SQLite DB (sets TGENGINE_DB; "
                                     "applied at startup before the DB engine binds)")
    args = parser.parse_args()

    try:
        result = _create(args)
    except Exception as exc:  # surface any hard failure as JSON, never a raw traceback
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    print(json.dumps(result))
    if not result.get("ok"):
        sys.exit(1)


if __name__ == "__main__":
    main()
