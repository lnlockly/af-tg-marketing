#!/usr/bin/env python3
"""
cli/add_targets.py — attach parse Targets to a campaign WITHOUT parsing.

Takes a list of channel/chat refs (@username or t.me link) and, for each, gets-or-
creates a Target(campaign_id, kind, ref, status=pending) in the SQLite control-plane,
deduped by (campaign_id, ref). Parsing happens later — the async daemon's parse_loop
(or cli/parse_now) picks up pending Targets and turns them into Leads.

No HTTP — writes rows straight into tgengine.db; prints a compact JSON result.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# Make the package importable when run as `python cli/add_targets.py` from repo root.
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


def _parse_refs(raw: str) -> list[str]:
    """Split a comma-separated list into cleaned, de-duplicated refs (order kept)."""
    seen: set[str] = set()
    refs: list[str] = []
    for chunk in raw.split(","):
        ref = chunk.strip()
        if not ref or ref in seen:
            continue
        seen.add(ref)
        refs.append(ref)
    return refs


def _add_targets(campaign_id: int, refs: list[str], kind: str) -> dict:
    db.init_db()
    added = 0
    targets: list[dict] = []
    with db.get_session() as session:
        campaign = session.get(db.Campaign, campaign_id)
        if campaign is None:
            raise SystemExit(f"campaign {campaign_id} not found")
        for ref in refs:
            target = session.exec(
                select(db.Target).where(
                    db.Target.campaign_id == campaign_id,
                    db.Target.ref == ref,
                )
            ).first()
            created = False
            if target is None:
                target = db.Target(
                    campaign_id=campaign_id,
                    kind=kind,
                    ref=ref,
                    status=db.TargetStatus.pending,
                )
                session.add(target)
                session.commit()
                session.refresh(target)
                added += 1
                created = True
            targets.append({
                "id": target.id,
                "ref": target.ref,
                "kind": target.kind,
                "status": target.status.value if hasattr(target.status, "value") else target.status,
                "created": created,
            })
    return {"ok": True, "added": added, "targets": targets}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Attach parse Targets (channels/chats) to a campaign without parsing them."
    )
    parser.add_argument("--campaign", type=int, required=True,
                        help="Campaign.id to attach the targets to")
    parser.add_argument("--targets", required=True,
                        help='comma-separated refs, e.g. "@a,@b,https://t.me/c"')
    parser.add_argument("--kind", choices=["channel", "chat"], default="channel",
                        help="target kind for all refs in this batch (default: channel)")
    parser.add_argument("--db", help="path to the SQLite DB (sets TGENGINE_DB; "
                                     "applied at startup before the DB engine binds)")
    args = parser.parse_args()

    refs = _parse_refs(args.targets)
    if not refs:
        print(json.dumps({"ok": False, "error": "no valid target refs provided"}))
        sys.exit(1)

    try:
        result = _add_targets(args.campaign, refs, args.kind)
    except SystemExit:
        raise
    except Exception as exc:  # surface any hard failure as JSON, never a raw traceback
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    print(json.dumps(result))
    if not result.get("ok"):
        sys.exit(1)


if __name__ == "__main__":
    main()
