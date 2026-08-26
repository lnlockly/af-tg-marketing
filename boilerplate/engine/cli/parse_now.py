#!/usr/bin/env python3
"""
cli/parse_now.py — one-shot: parse + classify a Target, with NO sending.

The Ф1 live-proof tool. Given a campaign and a channel/chat ref, it:
  1. ensures the Target row exists (creates it if missing),
  2. connects one active account read-only over its proxy (Telethon),
  3. parses the Target into people (`parser.parse_target`),
  4. upserts them as Leads (`parser.people_to_leads`),
  5. classifies + matches the new leads, creating `Dialog(status=new)` rows for
     matches (`matcher.classify_new_leads`),
and prints a compact JSON result: {ok, parsed, new_leads, matched, dialogs_created}.

It never sends a message and never prints the session/token. No HTTP — state is
read/written straight through tgengine.db.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

# Make the package importable when run as `python cli/parse_now.py` from repo root.
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
from tgengine import tgclient  # noqa: E402
from tgengine import parser as tgparser  # noqa: E402
from tgengine import matcher  # noqa: E402


def _get_campaign(campaign_id: int):
    """Load the Campaign; hard-fail with a clear message if it is missing."""
    with db.get_session() as session:
        campaign = session.get(db.Campaign, campaign_id)
        if campaign is None:
            raise SystemExit(f"campaign {campaign_id} not found")
        return campaign


def _get_or_create_target(campaign_id: int, ref: str):
    """Find the Target for (campaign, ref); create it (status=pending) if missing.
    Returns the detached Target so the async parse can read its last_message_id."""
    with db.get_session() as session:
        target = session.exec(
            select(db.Target).where(
                db.Target.campaign_id == campaign_id,
                db.Target.ref == ref,
            )
        ).first()
        if target is None:
            target = db.Target(campaign_id=campaign_id, ref=ref)
            session.add(target)
            session.commit()
            session.refresh(target)
        return target


def _pick_account(account_id):
    """Load the account to parse with (+ its Proxy). If --account-id is given use
    that one; otherwise pick any active account. Read-only parse, so no eligibility
    gating beyond is_active."""
    with db.get_session() as session:
        if account_id is not None:
            account = session.get(db.Account, account_id)
            if account is None:
                raise SystemExit(f"account {account_id} not found")
        else:
            account = session.exec(
                select(db.Account).where(db.Account.is_active == True)  # noqa: E712
            ).first()
            if account is None:
                raise SystemExit("no active account available to parse with")
        proxy = None
        if account.proxy_id is not None:
            proxy = session.get(db.Proxy, account.proxy_id)
        return account, proxy


async def _parse(account, proxy, target, limit: int):
    """Connect read-only and parse the target into people. Returns
    (people, last_message_id)."""
    client = await tgclient.connect(account, proxy)
    try:
        people, last_message_id = await tgparser.parse_target(client, target, limit=limit)
        return people, last_message_id
    finally:
        await tgclient.disconnect(client)


def _persist_and_classify(campaign_id: int, target_id: int, people, last_message_id: int):
    """Sync tail: upsert leads, advance the target cursor, mark it done, then
    classify + match the new leads (creating `new` dialogs). Returns the counters."""
    with db.get_session() as session:
        new_leads = tgparser.people_to_leads(session, campaign_id, target_id, people)

        target = session.get(db.Target, target_id)
        if target is not None:
            if last_message_id and last_message_id > (target.last_message_id or 0):
                target.last_message_id = last_message_id
            target.status = db.TargetStatus.done
            session.add(target)
            session.commit()

        campaign = session.get(db.Campaign, campaign_id)
        # Classify at least the leads we just added (plus any leftover unclassified).
        classify_limit = max(new_leads, 1)
        result = matcher.classify_new_leads(session, campaign, limit=classify_limit)
        return new_leads, result


def _mark_target_error(target_id: int) -> None:
    """Best-effort: flip the target to error so a later run can retry it."""
    try:
        with db.get_session() as session:
            target = session.get(db.Target, target_id)
            if target is not None:
                target.status = db.TargetStatus.error
                session.add(target)
                session.commit()
    except Exception:
        pass


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Parse a Target into leads and classify/match them — no sending. "
                    "The Ф1 live-proof tool."
    )
    ap.add_argument("--campaign", type=int, required=True,
                    help="Campaign.id these leads belong to")
    ap.add_argument("--target", required=True,
                    help="channel/chat to parse: @username or invite/t.me link")
    ap.add_argument("--account-id", type=int, default=None,
                    help="Account.id to parse with (default: any active account)")
    ap.add_argument("--limit", type=int, default=200,
                    help="max messages/participants to scan (default 200)")
    ap.add_argument("--db", help="path to the SQLite DB (sets TGENGINE_DB; "
                                 "applied at startup before the DB engine binds)")
    args = ap.parse_args()

    # Resolve campaign + target + account synchronously up front.
    _get_campaign(args.campaign)
    target = _get_or_create_target(args.campaign, args.target)
    account, proxy = _pick_account(args.account_id)
    target_id = target.id

    try:
        people, last_message_id = asyncio.run(_parse(account, proxy, target, args.limit))
    except tgclient.AccountError as exc:
        _mark_target_error(target_id)
        print(json.dumps({"ok": False, "error": str(exc)}))
        sys.exit(1)
    except Exception as exc:  # surface any hard failure as JSON, never a raw traceback
        _mark_target_error(target_id)
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        sys.exit(1)

    try:
        new_leads, result = _persist_and_classify(
            args.campaign, target_id, people, last_message_id
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        sys.exit(1)

    print(json.dumps({
        "ok": True,
        "parsed": len(people),
        "new_leads": new_leads,
        "matched": int(result.get("matched", 0)),
        "dialogs_created": int(result.get("dialogs_created", 0)),
    }))


if __name__ == "__main__":
    main()
