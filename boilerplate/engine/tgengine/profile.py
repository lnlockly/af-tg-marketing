"""
profile.py — account "одёжка" (profile dressing) over Telethon MTProto.

Light Python/Telethon reimplementation of leads42
src/telegram/telegram-profile.service.ts (updateProfileInfo / updateProfileUsername
/ updateProfilePhoto). Applies a display name, about/bio, username and avatar to a
connected account so a freshly bought account looks like a real person before it
runs campaigns.

GramJS `Api.account.UpdateProfile` → Telethon `functions.account.UpdateProfileRequest`
GramJS `Api.account.UpdateUsername` → `functions.account.UpdateUsernameRequest`
GramJS `Api.photos.UploadProfilePhoto` → `functions.photos.UploadProfilePhotoRequest`
(photo bytes uploaded via `client.upload_file`).

Each `set_*` op targets ONE field and may raise; `wrap_account` applies whichever
fields are provided and tolerates per-op failures so one bad field never aborts the
rest. Username collisions (USERNAME_OCCUPIED / USERNAME_NOT_MODIFIED) are treated as
non-fatal when the account already holds the requested username.
"""
from __future__ import annotations

from typing import Optional

from telethon import TelegramClient
from telethon import errors as tg_errors
from telethon.tl import functions, types
from telethon.tl.types import InputChatUploadedPhoto


async def set_name(
    client: TelegramClient,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
) -> None:
    """Update the account's display name (first and/or last).

    Ported from updateProfileInfo({firstName,lastName}). Only the provided fields
    are sent; Telegram leaves omitted fields untouched.
    """
    params: dict = {}
    if first_name is not None:
        params["first_name"] = first_name
    if last_name is not None:
        params["last_name"] = last_name
    if not params:
        return
    await client(functions.account.UpdateProfileRequest(**params))


async def set_about(client: TelegramClient, about: str) -> None:
    """Update the account's about/bio (updateProfileInfo({about}))."""
    await client(functions.account.UpdateProfileRequest(about=about or ""))


async def set_username(client: TelegramClient, username: str) -> bool:
    """Set the public @username, tolerating a name that is already taken/set.

    Ported from updateProfileUsername: strips a leading @, and when Telegram
    reports the username is unmodified or occupied-but-already-ours, treats it as a
    no-op success. Returns True if the username was set/kept, False if it was empty
    or genuinely occupied by someone else.
    """
    normalized = (username or "").strip().lstrip("@")
    if not normalized:
        return False
    try:
        await client(functions.account.UpdateUsernameRequest(username=normalized))
        return True
    except tg_errors.UsernameNotModifiedError:
        return True
    except tg_errors.UsernameOccupiedError:
        # Might already be ours — verify against the current account username.
        try:
            me = await client.get_me()
            current = (getattr(me, "username", None) or "").lower()
            if current and current == normalized.lower():
                return True
        except Exception:
            pass
        return False
    except Exception as error:
        message = str(error)
        if "USERNAME_NOT_MODIFIED" in message.upper():
            return True
        if "USERNAME_OCCUPIED" in message.upper():
            try:
                me = await client.get_me()
                current = (getattr(me, "username", None) or "").lower()
                if current and current == normalized.lower():
                    return True
            except Exception:
                pass
            return False
        raise


# --- username auto-pick (одёжка REQUIRES a @username — real accounts have one) ---
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def _translit(text: str) -> str:
    out = []
    for ch in (text or "").lower():
        if ch in _TRANSLIT:
            out.append(_TRANSLIT[ch])
        elif ch.isalnum() and ord(ch) < 128:
            out.append(ch)
    return "".join(out)


def username_candidates(first: str = "", last: str = "", persona: str = "", seed: int = 0) -> list[str]:
    """Build plausible @username candidates from a (possibly Cyrillic) name + persona.
    5-32 chars, [a-z0-9_], must start with a letter — ordered most-natural first, with
    numeric fallbacks so we almost always find a free one."""
    f, l = _translit(first), _translit(last)
    kw = ""
    for token in _translit(persona).split():
        if len(token) >= 4:
            kw = token; break
    bases = []
    if f and l:
        bases += [f"{f}_{l}", f"{f}{l}", f"{f[0]}_{l}", f"{l}_{f}"]
    if (f or l) and kw:
        bases += [f"{f or l}_{kw}", f"{kw}_{f or l}", f"{l}_{kw}"]
    if f:
        bases += [f, f"{f}_{kw}" if kw else f]
    bases = [b.strip("_") for b in bases if len(b.strip("_")) >= 5]
    cands = []
    for b in bases:
        cands.append(b[:32])
    # numeric fallbacks (deterministic by seed so re-runs are stable-ish)
    root = (bases[0] if bases else (kw or "user"))[:24].strip("_")
    for i in range(6):
        n = (seed + i * 7) % 900 + 100
        cands.append(f"{root}_{n}"[:32])
    # dedup preserve order
    seen, out = set(), []
    for c in cands:
        if c and c not in seen and c[0].isalpha():
            seen.add(c); out.append(c)
    return out


async def ensure_username(client: TelegramClient, first: str = "", last: str = "",
                          persona: str = "", seed: int = 0) -> Optional[str]:
    """одёжка mandate: give the account a @username. Try candidates until one sticks;
    if it already has one, keep it. Returns the username set/kept, or None."""
    try:
        me = await client.get_me()
        if getattr(me, "username", None):
            return me.username
    except Exception:
        pass
    for cand in username_candidates(first, last, persona, seed):
        try:
            if await set_username(client, cand):
                return cand
        except Exception:
            continue
    return None


async def ensure_channel_username(client: TelegramClient, channel, title: str = "",
                                  persona: str = "", seed: int = 0) -> Optional[str]:
    """Make the account's channel PUBLIC by giving it a @username (candidates from
    the title). Returns the username set, or None."""
    for cand in username_candidates(title, "", persona, seed):
        try:
            await client(functions.channels.UpdateUsernameRequest(channel, cand))
            return cand
        except Exception:
            continue
    return None


async def set_photo(client: TelegramClient, path: str):
    """Upload a local image and set it as the account's profile photo.

    Ported from updateProfilePhoto: upload the file bytes, then
    UploadProfilePhotoRequest. `client.upload_file` accepts a path directly.
    """
    uploaded = await client.upload_file(path)
    return await client(functions.photos.UploadProfilePhotoRequest(file=uploaded))


async def set_photos(client: TelegramClient, paths: list[str]) -> int:
    """Set SEVERAL profile photos (a real account has a gallery, not one shot).

    Uploads each in order — Telegram keeps them in the account's photo history and
    the LAST one becomes the current avatar. Tolerant per-photo (one bad file does
    not abort the rest). Returns how many were set. A small pause between uploads
    avoids flood on bulk sets.
    """
    import asyncio
    ok = 0
    for p in paths:
        try:
            await set_photo(client, p)
            ok += 1
            await asyncio.sleep(1.5)
        except Exception:
            continue
    return ok


async def wrap_account(
    client: TelegramClient,
    *,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    about: Optional[str] = None,
    username: Optional[str] = None,
    photo_path: Optional[str] = None,
) -> dict:
    """Apply whichever profile fields are provided; return what changed.

    Each field is applied independently and its failure is captured (never
    aborting the remaining fields), mirroring leads42 applyDirectProfileData which
    walks name → about → username → photo as separate, tolerant steps.

    Returns: {"changed": {...applied fields...}, "errors": {field: reason}}.
    """
    changed: dict = {}
    errors: dict = {}

    if first_name is not None or last_name is not None:
        try:
            await set_name(client, first_name=first_name, last_name=last_name)
            if first_name is not None:
                changed["first_name"] = first_name
            if last_name is not None:
                changed["last_name"] = last_name
        except Exception as error:
            errors["name"] = f"{type(error).__name__}: {error}"

    if about is not None:
        try:
            await set_about(client, about)
            changed["about"] = about
        except Exception as error:
            errors["about"] = f"{type(error).__name__}: {error}"

    if username is not None:
        try:
            ok = await set_username(client, username)
            if ok:
                changed["username"] = (username or "").strip().lstrip("@")
            else:
                errors["username"] = "empty or occupied by another account"
        except Exception as error:
            errors["username"] = f"{type(error).__name__}: {error}"

    if photo_path is not None:
        try:
            await set_photo(client, photo_path)
            changed["photo"] = photo_path
        except Exception as error:
            errors["photo"] = f"{type(error).__name__}: {error}"

    return {"changed": changed, "errors": errors}


# --- own channel "одёжка" (Ф3.1) --------------------------------------------
async def create_channel(
    client: TelegramClient,
    title: str,
    about: str = "",
    megagroup: bool = False,
) -> dict:
    """Create the account's OWN channel (real users often have one).

    Ported intent from telegram-profile.service.ts channel dressing: a broadcast
    channel by default (megagroup=False), returning the new channel's identity.
    Tolerates errors (FLOOD_WAIT / restrictions) by returning {}.

    Returns: {"channel_id", "access_hash", "username"} (username may be None).
    """
    try:
        result = await client(
            functions.channels.CreateChannelRequest(
                title=title or "",
                about=about or "",
                broadcast=not megagroup,
                megagroup=megagroup,
            )
        )
    except Exception:
        return {}

    channel = None
    for chat in getattr(result, "chats", []) or []:
        if isinstance(chat, (types.Channel, types.Chat)):
            channel = chat
            break
    if channel is None:
        return {}

    return {
        "channel_id": getattr(channel, "id", None),
        "access_hash": getattr(channel, "access_hash", None),
        "username": getattr(channel, "username", None),
    }


async def set_channel_photo(client: TelegramClient, channel, path: str) -> bool:
    """Set a channel's photo from a local file. Tolerates errors (returns False).

    Ported from updateProfilePhoto for channels: upload the bytes, then
    channels.EditPhoto with InputChatUploadedPhoto. `channel` may be an entity,
    id, or username Telethon can resolve.
    """
    try:
        uploaded = await client.upload_file(path)
        await client(
            functions.channels.EditPhotoRequest(
                channel=channel,
                photo=InputChatUploadedPhoto(file=uploaded),
            )
        )
        return True
    except Exception:
        return False
