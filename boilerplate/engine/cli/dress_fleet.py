#!/usr/bin/env python3
"""
cli/dress_fleet.py — batch "одёжка" across a FLEET of accounts.

Real accounts don't come dressed one at a time — you buy a batch and make each
one look like a distinct real person. This walks a set of stored Accounts and,
for each active one, rotates a persona from a pool and runs the SAME dress flow as
cli/wrap_account: a human display name + bio (generated via tgengine.ai from the
rotated persona), an optional AI avatar (--avatar-ai), and an optional own channel
(--with-channel). Accounts are dressed one at a time with a small jittered delay so
the fleet doesn't move in lockstep, and the applied fields are persisted back onto
each Account row.

Prints a JSON summary list: [{account_id, first_name, avatar, channel_username, ok}].

No HTTP — reads/writes state straight from tgengine.db, applies via tgengine.profile.
Never prints the session/token.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
from typing import Optional

# Make the package importable when run as `python cli/dress_fleet.py` from repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# db.py binds its SQLite engine to TGENGINE_DB at import time, so honor --db BEFORE
# importing the package (a tiny pre-scan; argparse still owns real validation below).
_argv = sys.argv[1:]
for _i, _a in enumerate(_argv):
    if _a == "--db" and _i + 1 < len(_argv):
        os.environ["TGENGINE_DB"] = _argv[_i + 1]
    elif _a.startswith("--db="):
        os.environ["TGENGINE_DB"] = _a[len("--db="):]

from telethon.tl import types  # noqa: E402

from tgengine import ai  # noqa: E402
from tgengine import db  # noqa: E402
from tgengine import photos as photos_mod  # noqa: E402
from tgengine import profile as profile_mod  # noqa: E402
from tgengine import tgclient  # noqa: E402

# A sensible default rotation: plausible, varied personas that look like real
# individuals (not brands/bots), spanning investor / founder / freelancer / creator.
DEFAULT_PERSONA_POOL = [
    "частный инвестор, интересуется стартапами",
    "основатель небольшого IT-стартапа",
    "фрилансер-дизайнер на удалёнке",
    "контент-криэйтор, ведёт блог о продуктивности",
    "маркетолог из агентства",
    "владелец интернет-магазина",
    "консультант по продажам",
    "разработчик, делает пет-проекты",
]

# Gentle pacing between accounts so the fleet doesn't move in lockstep.
DELAY_MIN_SECONDS = 3.0
DELAY_MAX_SECONDS = 9.0


def _parse_ids(raw: Optional[str]) -> list[int]:
    """Parse '1,2,3' (spaces tolerated) into a list of ints, preserving order."""
    ids: list[int] = []
    for chunk in (raw or "").replace(" ", "").split(","):
        if not chunk:
            continue
        try:
            ids.append(int(chunk))
        except ValueError:
            raise SystemExit(f"bad --ids value: {chunk!r}")
    return ids


def _parse_pool(raw: Optional[str]) -> list[str]:
    """Parse 'a;b;c' into a persona list; fall back to the default pool."""
    if not (raw or "").strip():
        return list(DEFAULT_PERSONA_POOL)
    pool = [p.strip() for p in raw.split(";") if p.strip()]
    return pool or list(DEFAULT_PERSONA_POOL)


def _select(ids: list[int], all_flag: bool) -> list[tuple]:
    """Load the target Accounts (+ their Proxy) as (account, proxy) tuples.

    Only active accounts are dressed. --all selects every active account; otherwise
    the explicit --ids set is used (skipping ids that are missing/inactive).
    """
    selected: list[tuple] = []
    with db.get_session() as session:
        if all_flag:
            rows = session.query(db.Account).filter(db.Account.is_active == True).all()  # noqa: E712
            accounts = list(rows)
        else:
            accounts = []
            for aid in ids:
                account = session.get(db.Account, aid)
                if account is not None and account.is_active:
                    accounts.append(account)
        for account in accounts:
            proxy = None
            if account.proxy_id is not None:
                proxy = session.get(db.Proxy, account.proxy_id)
            selected.append((account, proxy))
    return selected


async def _dress_one(account, proxy, *, persona: str, with_channel: bool,
                     photos_dir: Optional[str] = None) -> dict:
    """Connect one account and apply the dress flow: name/bio (AI text) + @username +
    optional photos + optional own channel. The engine only APPLIES photos — the agent
    sources them via its native tools and drops them in <photos_dir>/<account_id>/."""
    changed: dict = {}
    errors: dict = {}

    # Generate the human name + bio (TEXT) from the rotated persona.
    first_name, last_name = ai.generate_display_name(persona)
    about = ai.generate_profile_about(persona)
    fields: dict = {
        "first_name": first_name or None,
        "last_name": last_name or None,
        "about": about or None,
        "username": None,
    }

    client = await tgclient.connect(account, proxy)
    try:
        # 1) name / about / username.
        wrapped = await profile_mod.wrap_account(client, **fields)
        changed.update(wrapped.get("changed", {}))
        for key, value in wrapped.get("errors", {}).items():
            errors[key] = value

        # 1b) photos the agent pre-dropped for THIS account (<photos_dir>/<id>/).
        avatar_path = account.avatar_path
        pool = photos_mod.collect_local(os.path.join(photos_dir, str(account.id))) if photos_dir else []
        if pool:
            try:
                n = await profile_mod.set_photos(client, pool)
                if n > 0:
                    changed["photos_set"] = n
                    avatar_path = pool[-1]
                    changed["avatar_path"] = avatar_path
            except Exception as error:
                errors["photos"] = f"{type(error).__name__}: {error}"

        # 2b) одёжка REQUIRES a @username — auto-pick one for every fleet account.
        if "username" not in changed:
            try:
                uname = await profile_mod.ensure_username(
                    client, first_name or "", last_name or "", persona or "", seed=account.id or 0,
                )
                if uname:
                    changed["username"] = uname
                else:
                    errors["username"] = "could not secure any @username candidate"
            except Exception as error:
                errors["username"] = f"{type(error).__name__}: {error}"

        # 3) the account's OWN channel (+ photo when we have an avatar).
        if with_channel:
            # Title the channel after the person; fall back to the persona.
            title = " ".join(p for p in [first_name, last_name] if p).strip() \
                or (persona or "").strip() or "Channel"
            try:
                ch = await profile_mod.create_channel(client, title)
            except Exception as error:
                ch = None
                errors["channel"] = f"{type(error).__name__}: {error}"
            if ch:
                cid = ch.get("channel_id")
                cuser = ch.get("username")
                access_hash = ch.get("access_hash")
                if cid is not None:
                    changed["channel_id"] = cid
                input_channel = None
                if cid is not None and access_hash is not None:
                    input_channel = types.InputChannel(channel_id=cid, access_hash=access_hash)
                # Make the channel PUBLIC (@username) — part of одёжка.
                if not cuser and input_channel is not None:
                    try:
                        cuser = await profile_mod.ensure_channel_username(
                            client, input_channel, title, persona or "", seed=account.id or 0,
                        )
                    except Exception as error:
                        errors["channel_username"] = f"{type(error).__name__}: {error}"
                if cuser:
                    changed["channel_username"] = cuser
                photo_for_channel = avatar_path or account.avatar_path
                if photo_for_channel and input_channel is not None:
                    try:
                        await profile_mod.set_channel_photo(client, input_channel, photo_for_channel)
                        changed["channel_photo"] = True
                    except Exception as error:
                        errors["channel_photo"] = f"{type(error).__name__}: {error}"
    finally:
        await tgclient.disconnect(client)

    return {"changed": changed, "errors": errors}


def _persist(account_id: int, changed: dict) -> None:
    """Write the applied fields back onto the Account row (same keys as wrap_account)."""
    if not changed:
        return
    with db.get_session() as session:
        account = session.get(db.Account, account_id)
        if account is None:
            return
        if "first_name" in changed:
            account.first_name = changed["first_name"]
        if "last_name" in changed:
            account.last_name = changed["last_name"]
        if "about" in changed:
            account.about = changed["about"]
        if "username" in changed:
            account.username = changed["username"]
        if "avatar_path" in changed:
            account.avatar_path = changed["avatar_path"]
        if "channel_id" in changed:
            account.channel_id = changed["channel_id"]
        if "channel_username" in changed:
            account.channel_username = changed["channel_username"]
        session.add(account)
        session.commit()


async def _run(selected: list[tuple], pool: list[str], *, with_channel: bool,
               photos_dir: Optional[str] = None) -> list[dict]:
    summary: list[dict] = []
    for index, (account, proxy) in enumerate(selected):
        persona = pool[index % len(pool)]
        item: dict = {"account_id": account.id}
        try:
            result = await _dress_one(account, proxy, persona=persona,
                                      with_channel=with_channel, photos_dir=photos_dir)
            changed = result.get("changed", {})
            _persist(account.id, changed)
            item["first_name"] = changed.get("first_name")
            item["avatar"] = changed.get("avatar_path")
            item["channel_username"] = changed.get("channel_username")
            item["ok"] = True
            if result.get("errors"):
                item["errors"] = result["errors"]
        except tgclient.AccountError as exc:
            item["ok"] = False
            item["error"] = str(exc)
            item["reason"] = getattr(exc, "reason", None)
        except Exception as exc:  # surface any hard failure per-account, never a traceback
            item["ok"] = False
            item["error"] = f"{type(exc).__name__}: {exc}"
        summary.append(item)

        # Small jittered delay between accounts (skip after the last one).
        if index < len(selected) - 1:
            await asyncio.sleep(random.uniform(DELAY_MIN_SECONDS, DELAY_MAX_SECONDS))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Batch-dress a fleet of stored accounts (name/bio + optional avatar/channel)."
    )
    parser.add_argument("--persona-pool",
                        help="personas separated by ';' (e.g. 'инвестор;основатель;фрилансер'); "
                             "defaults to a built-in investor/founder/freelancer/creator pool")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ids", help="comma-separated Account ids to dress, e.g. 1,2,3")
    group.add_argument("--all", action="store_true", help="dress every active account")
    parser.add_argument("--photos-dir", metavar="DIR",
                        help="root of pre-sourced photos; each account uses DIR/<account_id>/*. "
                             "The AGENT fills these via its NATIVE image tools (generate/search) — "
                             "the engine only APPLIES them.")
    parser.add_argument("--with-channel", action="store_true",
                        help="create each account's own channel (photo from its avatar when available)")
    parser.add_argument("--db", help="path to the SQLite DB (sets TGENGINE_DB; "
                                     "applied at startup before the DB engine binds)")
    args = parser.parse_args()

    pool = _parse_pool(args.persona_pool)
    ids = _parse_ids(args.ids) if not args.all else []
    selected = _select(ids, args.all)

    if not selected:
        print(json.dumps([], ensure_ascii=False))
        return

    summary = asyncio.run(_run(selected, pool, with_channel=args.with_channel,
                               photos_dir=args.photos_dir))
    print(json.dumps(summary, ensure_ascii=False))
    if not any(item.get("ok") for item in summary):
        sys.exit(1)


if __name__ == "__main__":
    main()
