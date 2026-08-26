"""
health.py — per-account health + @SpamBot spamblock probe.

The engine is the single registry of all userbot accounts; the account marks
ITSELF via health checks. `health_check` connects the account (through its proxy),
maps a hard tgclient.AccountError to a db.AccountStatus, and — when the account is
otherwise reachable — asks @SpamBot whether it is limited.

Returns plain dicts with string status values that match db.AccountStatus, so the
CLI (cli/account_health.py) can write them straight back onto the Account row.
Everything here is tolerant: a probe failure degrades to a best-effort answer, it
never raises into the caller's flow.

Imports tgclient (connect/AccountError) + db enums only.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Optional

from tgengine import tgclient
from tgengine.db import AccountStatus


SPAMBOT = "@SpamBot"
SPAMBOT_TIMEOUT = 30


# --- reply parsing -----------------------------------------------------------
# @SpamBot answers in the account's language. We match both RU and EN phrasings.
# "Good news, no limits are currently applied to your account." / free variants.
_NO_LIMIT_RE = re.compile(
    r"no limits|not limited|good news|free to use|"
    r"никаки[ех]\s+ограничени|ограничени\w*\s+не\s+прим|"
    r"хорошие\s+новости|свободн",
    re.I,
)
# "your account is now limited until <date>." / "ограничен до <date>".
_LIMITED_RE = re.compile(
    r"is\s+limited|are\s+limited|now\s+limited|been\s+limited|"
    r"limited\s+until|restricted|"
    r"ограничен|заблокирован|ограничени\w*\s+будут\s+сняты",
    re.I,
)
# grab the date fragment after "until" / "до"
_UNTIL_RE = re.compile(r"(?:until|до)\s+(.+?)(?:[.\n]|$)", re.I)


# Month names → number (EN full/abbrev + RU nominative/genitive), for best-effort dates.
_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
    "янв": 1, "января": 1, "январь": 1, "фев": 2, "февраля": 2, "февраль": 2,
    "мар": 3, "марта": 3, "март": 3, "апр": 4, "апреля": 4, "апрель": 4,
    "мая": 5, "май": 5, "июн": 6, "июня": 6, "июнь": 6, "июл": 7, "июля": 7,
    "июль": 7, "авг": 8, "августа": 8, "август": 8, "сен": 9, "сентября": 9,
    "сентябрь": 9, "окт": 10, "октября": 10, "октябрь": 10, "ноя": 11,
    "ноября": 11, "ноябрь": 11, "дек": 12, "декабря": 12, "декабрь": 12,
}


def _parse_date(fragment: str) -> Optional[datetime]:
    """Best-effort parse of a @SpamBot date fragment into a datetime. None on fail.

    Handles forms like "12 Jan 2026, 15:00 UTC", "January 12, 2026",
    "2026-01-12 15:00:00", "12 января 2026". Time/timezone are optional.
    """
    if not fragment:
        return None
    text = fragment.strip().strip(".").strip()

    # ISO-ish: 2026-01-12[ T]15:00[:00]
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?", text)
    if m:
        try:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            hh = int(m.group(4) or 0)
            mi = int(m.group(5) or 0)
            ss = int(m.group(6) or 0)
            return datetime(y, mo, d, hh, mi, ss)
        except (ValueError, TypeError):
            pass

    # find a month name
    mon = None
    for token in re.findall(r"[A-Za-zА-Яа-яЁё]+", text):
        key = token.lower()
        if key in _MONTHS:
            mon = _MONTHS[key]
            break
    # day + year numbers
    nums = re.findall(r"\d+", text)
    day = None
    year = None
    for n in nums:
        val = int(n)
        if val > 31 and year is None:
            year = val
        elif 1 <= val <= 31 and day is None:
            day = val
    if year is None:
        # fall back to any 4-digit number
        for n in nums:
            if len(n) == 4:
                year = int(n)
                break
    # optional HH:MM
    tm = re.search(r"(\d{1,2}):(\d{2})", text)
    hh = int(tm.group(1)) if tm else 0
    mi = int(tm.group(2)) if tm else 0

    if mon and day and year:
        try:
            return datetime(year, mon, day, hh, mi)
        except (ValueError, TypeError):
            return None
    return None


# --- @SpamBot probe ----------------------------------------------------------
async def check_spamblock(client) -> dict:
    """Ask @SpamBot whether this account is limited.

    Sends "/start", reads the reply, and parses it (RU/EN). Returns
    {blocked: bool, until: datetime|None, raw: str}. Tolerant: on any error
    returns blocked False with raw carrying the error note.
    """
    raw = ""
    try:
        entity = await tgclient.resolve(client, SPAMBOT)
        await client.send_message(entity, "/start")

        # give @SpamBot a moment, then read its most recent reply
        reply = None
        deadline = asyncio.get_event_loop().time() + SPAMBOT_TIMEOUT
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(2)
            async for msg in client.iter_messages(entity, limit=5):
                text = getattr(msg, "message", None) or getattr(msg, "text", None)
                if text and not getattr(msg, "out", False):
                    reply = text
                    break
            if reply:
                break

        raw = reply or ""
    except Exception as error:  # tolerant — never raise out of the probe
        return {"blocked": False, "until": None, "raw": f"error: {error}"}

    if not raw:
        return {"blocked": False, "until": None, "raw": ""}

    # limited takes precedence over the "no limits" match when both appear
    limited = bool(_LIMITED_RE.search(raw))
    freed = bool(_NO_LIMIT_RE.search(raw))
    blocked = limited and not freed

    until = None
    if blocked:
        m = _UNTIL_RE.search(raw)
        if m:
            until = _parse_date(m.group(1))

    return {"blocked": blocked, "until": until, "raw": raw}


# --- account-level health check ---------------------------------------------
async def health_check(account, proxy) -> dict:
    """Connect the account and classify its health.

    Maps tgclient.AccountError.reason:
        dead                         -> "dead"
        unauthorized / terminated    -> "terminated"
        banned                       -> "banned"
        flood / proxy                -> "cooldown"
    Otherwise runs check_spamblock: "spamblock" (+ spamblock_until) or "active".

    Returns {status, spamblock_until, detail} with string status values matching
    db.AccountStatus. Always disconnects cleanly. Tolerant.
    """
    try:
        client = await tgclient.connect(account, proxy)
    except tgclient.AccountError as exc:
        reason = getattr(exc, "reason", "error")
        if reason == "dead":
            status = AccountStatus.dead
        elif reason in ("unauthorized", "terminated"):
            status = AccountStatus.terminated
        elif reason == "banned":
            status = AccountStatus.banned
        elif reason in ("flood", "proxy"):
            status = AccountStatus.cooldown
        else:
            status = AccountStatus.unknown
        return {"status": status.value, "spamblock_until": None, "detail": str(exc)}
    except Exception as exc:  # any non-classified failure — stay tolerant
        return {"status": AccountStatus.unknown.value,
                "spamblock_until": None,
                "detail": f"{type(exc).__name__}: {exc}"}

    try:
        probe = await check_spamblock(client)
        if probe.get("blocked"):
            return {"status": AccountStatus.spamblock.value,
                    "spamblock_until": probe.get("until"),
                    "detail": (probe.get("raw") or "")[:400]}
        return {"status": AccountStatus.active.value,
                "spamblock_until": None,
                "detail": (probe.get("raw") or "")[:400]}
    except Exception as exc:  # probe blew up but the account connected — call it active
        return {"status": AccountStatus.active.value,
                "spamblock_until": None,
                "detail": f"spamblock probe failed: {exc}"}
    finally:
        await tgclient.disconnect(client)
