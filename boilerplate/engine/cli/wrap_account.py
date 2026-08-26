#!/usr/bin/env python3
"""
cli/wrap_account.py — one-shot account "одёжка" (profile dresser).

Loads an Account (+ its Proxy) from the SQLite control-plane, connects through the
account's proxy via Telethon, and applies a display name / about / username / photo
to make a freshly ingested account look like a real person before it runs campaigns.
Persists the applied name/username/about back onto the Account row and prints a
compact JSON result: {ok, changed}.

With --ai and --persona, any missing name/about is generated from the persona via
tgengine.ai (generate_display_name / generate_profile_about). Explicit flags always
win over generated values.

--avatar-ai generates a profile photo from --persona (ai.generate_avatar), sets it,
and persists avatar_path. --with-channel "Title" creates the account's OWN channel
(одёжка: real users have one); if an avatar exists it is set as the channel photo too,
and channel_id/channel_username are persisted back onto the Account row.

No HTTP — reads/writes state straight from tgengine.db, applies via tgengine.profile.
Never prints the session/token.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Optional

# Make the package importable when run as `python cli/wrap_account.py` from repo root.
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


def _load(account_id: int):
    """Load the Account and its Proxy (sync SQLModel session)."""
    with db.get_session() as session:
        account = session.get(db.Account, account_id)
        if account is None:
            raise SystemExit(f"account {account_id} not found")
        proxy = None
        if account.proxy_id is not None:
            proxy = session.get(db.Proxy, account.proxy_id)
        return account, proxy


def _resolve_fields(args) -> dict:
    """Decide which fields to apply, generating missing name/about via AI if asked."""
    first_name = args.first_name
    last_name = args.last_name
    about = args.about
    username = args.username

    if args.ai and (args.persona or "").strip():
        persona = args.persona.strip()
        if first_name is None and last_name is None:
            gen_first, gen_last = ai.generate_display_name(persona)
            first_name = gen_first or None
            last_name = gen_last or None
        if about is None:
            gen_about = ai.generate_profile_about(persona)
            about = gen_about or None

    return {
        "first_name": first_name,
        "last_name": last_name,
        "about": about,
        "username": username,
        # photos are handled as a POOL (user files / web URLs / AI), not one field
    }


async def _dress(client, account, fields: dict, *, persona: Optional[str],
                 photos_spec: Optional[str] = None,
                 with_channel: Optional[str] = None, want_username: bool = True) -> dict:
    """Apply the full 'одёжка' over a connected client: name/about/username, a PHOTO
    GALLERY (several photo FILES the agent already sourced), then an optional own channel.

    NOTE: the engine only APPLIES photos. Finding them on the web / generating them is
    the AGENT's job via its native tools; it passes ready file paths in `photos_spec`.

    Returns {"changed": {...}, "errors": {...}}. Every step is tolerant. `changed`
    carries persistable keys: first_name, last_name, about, username, avatar_path,
    photos_set, channel_id, channel_username.
    """
    changed: dict = {}
    errors: dict = {}
    fields = dict(fields)  # never mutate the caller's dict

    # 1) name / about / username (photos handled as a gallery in step 2).
    wrapped = await profile_mod.wrap_account(client, **fields)
    changed.update(wrapped.get("changed", {}))
    for key, value in wrapped.get("errors", {}).items():
        errors[key] = value

    # 2) PHOTO GALLERY — set the SEVERAL photo files the agent handed us (the last
    # becomes the current avatar). The engine does not fetch or generate — see photos.py.
    avatar_path = account.avatar_path
    pool = photos_mod.collect_local(photos_spec)
    if pool:
        try:
            n = await profile_mod.set_photos(client, pool)
            if n > 0:
                changed["photos_set"] = n
                avatar_path = pool[-1]
                changed["avatar_path"] = avatar_path
            else:
                errors["photos"] = "no photo uploaded"
        except Exception as error:
            errors["photos"] = f"{type(error).__name__}: {error}"

    # 2b) одёжка REQUIRES a @username. If none was set explicitly, auto-pick one
    # (transliterate the name + persona → try candidates until one is free).
    if want_username and "username" not in changed:
        try:
            uname = await profile_mod.ensure_username(
                client, fields.get("first_name") or "", fields.get("last_name") or "",
                persona or "", seed=account.id or 0,
            )
            if uname:
                changed["username"] = uname
            else:
                errors["username"] = "could not secure any @username candidate"
        except Exception as error:
            errors["username"] = f"{type(error).__name__}: {error}"

    # 3) the account's OWN channel (+ photo if we have an avatar).
    if with_channel:
        try:
            ch = await profile_mod.create_channel(client, with_channel)
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
            # Make the channel PUBLIC (@username) so it's openable — part of одёжка.
            if not cuser and input_channel is not None and want_username:
                try:
                    cuser = await profile_mod.ensure_channel_username(
                        client, input_channel, with_channel, persona or "", seed=account.id or 0,
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

    return {"changed": changed, "errors": errors}


async def _run(account, proxy, fields: dict, *, persona: Optional[str] = None,
               photos_spec: Optional[str] = None,
               with_channel: Optional[str] = None, want_username: bool = True) -> dict:
    client = await tgclient.connect(account, proxy)
    try:
        return await _dress(client, account, fields, persona=persona,
                            photos_spec=photos_spec,
                            with_channel=with_channel, want_username=want_username)
    finally:
        await tgclient.disconnect(client)


def _persist(account_id: int, changed: dict) -> None:
    """Write the applied name/username/about back onto the Account row."""
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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dress a stored account's profile (name/about/username/photo)."
    )
    parser.add_argument("--account-id", type=int, required=True,
                        help="Account.id in the tgengine DB to dress")
    parser.add_argument("--first-name", help="display first name")
    parser.add_argument("--last-name", help="display last name")
    parser.add_argument("--about", help="bio / about text (<=70 chars)")
    parser.add_argument("--username", help="public @username (leading @ optional)")
    parser.add_argument("--photos", metavar="FILES_OR_DIR",
                        help="photo FILES to set as the gallery: comma-separated paths OR a "
                             "directory. The AGENT sources these itself via its NATIVE tools "
                             "(image_generate for synthetic faces / a web image-search tool) — "
                             "the engine only APPLIES them. Several files = a realistic gallery.")
    parser.add_argument("--persona", help="persona for AI TEXT generation (name/bio), e.g. 'крипто-трейдер'")
    parser.add_argument("--ai", action="store_true",
                        help="generate missing name/about from --persona via the LLM (text only)")
    parser.add_argument("--with-channel", metavar="TITLE",
                        help="create the account's own channel with this title "
                             "(made public with a @username; channel photo from the avatar)")
    parser.add_argument("--no-username", action="store_true",
                        help="opt out of the default auto-@username (одёжка sets one otherwise)")
    parser.add_argument("--db", help="path to the SQLite DB (sets TGENGINE_DB; "
                                     "applied at startup before the DB engine binds)")
    args = parser.parse_args()

    fields = _resolve_fields(args)
    account, proxy = _load(args.account_id)

    try:
        result = asyncio.run(_run(account, proxy, fields, persona=args.persona,
                                  photos_spec=args.photos, with_channel=args.with_channel,
                                  want_username=not args.no_username))
        changed = result.get("changed", {})
        _persist(args.account_id, changed)
        out = {"ok": True, "changed": changed}
        if result.get("errors"):
            out["errors"] = result["errors"]
    except tgclient.AccountError as exc:
        out = {"ok": False, "error": str(exc), "reason": getattr(exc, "reason", None)}
    except Exception as exc:  # surface any hard failure as JSON, never a raw traceback
        out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    print(json.dumps(out, ensure_ascii=False))
    if not out.get("ok"):
        sys.exit(1)


if __name__ == "__main__":
    main()
