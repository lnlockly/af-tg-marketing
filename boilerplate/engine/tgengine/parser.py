"""
parser.py — read-only MTProto parsing of a Target into people (Ф1).

Light Python/Telethon reimplementation of the leads42 parser
(src/telegram-parser/telegramParser.service.ts) + person upsert
(src/people/people.service.ts). No ParsedPost/ParsedMessage tables, no client
cache map — the caller connects one Account and hands us its client.

Two entry points:
  parse_target(client, target, limit)  — async, does the MTProto reads.
  people_to_leads(session, ...)         — sync SQLModel, upserts Lead rows.

Channel targets: GetFullChannel → linked discussion chat → iterate its comments;
each commenter becomes a person whose comment text accumulates into `context`.
Fallback (no discussion / private): iterate the entity's own recent messages,
then its participants (identity only). FLOOD_WAIT surfaces as AccountError.
"""
from __future__ import annotations

from typing import Optional

from telethon import errors as tg_errors
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.types import User

from sqlmodel import Session, select

from tgengine import tgclient
from tgengine.db import Lead, Target


# How much per-person comment text we keep as the classifier blurb. Comments
# accumulate; past this we stop appending (protects prompt size / DB bloat).
CONTEXT_MAX = 1500


# --- person accumulation -----------------------------------------------------
def _add_person(people: dict, user, text: str = "") -> None:
    """De-dupe by tg_id within a call; append comment text into `context`.

    Mirrors mapApiUser + the per-sender comment accumulation from leads42's
    parseMessageChat, but keyed so repeat commenters merge into one person.
    """
    try:
        tg_id = int(user.id)
    except (TypeError, ValueError, AttributeError):
        return

    person = people.get(tg_id)
    if person is None:
        person = {
            "tg_id": tg_id,
            "username": getattr(user, "username", None),
            "first_name": getattr(user, "first_name", None),
            "context": "",
        }
        people[tg_id] = person

    text = (text or "").strip()
    if text and len(person["context"]) < CONTEXT_MAX:
        person["context"] = (person["context"] + " " + text).strip() if person["context"] else text


async def _linked_discussion(client, entity):
    """Return the linked discussion chat entity of a channel, or None.

    Ports fetchChannelMessages: GetFullChannel → ChannelFull.linkedChatId, then
    pick that chat out of the returned `chats`. Tolerates channels with no
    comments enabled (returns None so the caller falls back).
    """
    try:
        full = await client(GetFullChannelRequest(channel=entity))
    except Exception:
        return None

    linked_id = getattr(getattr(full, "full_chat", None), "linked_chat_id", None)
    if not linked_id:
        return None

    for chat in getattr(full, "chats", []) or []:
        if getattr(chat, "id", None) == linked_id:
            return chat

    # Not in the bundle — try a direct resolve as a last resort.
    try:
        return await client.get_entity(linked_id)
    except Exception:
        return None


async def _collect_messages(client, entity, limit: int, min_id: int, people: dict) -> int:
    """Iterate recent messages; each real (non-bot) sender is a person whose
    text feeds `context`. Returns the max message id seen (>= min_id).

    Ports parseMessageChat: skip senderless / empty messages, cap at `limit`.
    Using min_id makes reparses incremental (only messages after last_message_id).
    """
    last_id = min_id
    count = 0

    kwargs = {"limit": limit}
    if min_id:
        kwargs["min_id"] = min_id

    async for message in client.iter_messages(entity, **kwargs):
        if message is None:
            continue

        mid = getattr(message, "id", 0) or 0
        if mid > last_id:
            last_id = mid

        sender = getattr(message, "sender", None)
        if sender is None:
            try:
                sender = await message.get_sender()
            except tg_errors.FloodWaitError:
                raise
            except Exception:
                sender = None

        if not isinstance(sender, User) or getattr(sender, "bot", False):
            continue

        _add_person(people, sender, getattr(message, "message", "") or "")
        count += 1
        if count >= limit:
            break

    return last_id


async def _collect_participants(client, entity, limit: int, people: dict) -> None:
    """Identity-only fallback: enumerate members (GetParticipants under the
    hood). No context text. Ports getChatMembers → mapApiUser, real users only.
    Tolerates broadcast channels / restricted member lists by returning quietly.
    """
    try:
        async for user in client.iter_participants(entity, limit=limit):
            if not isinstance(user, User) or getattr(user, "bot", False):
                continue
            _add_person(people, user, "")
    except tg_errors.FloodWaitError:
        raise
    except Exception:
        return


async def parse_target(client, target: Target, limit: int = 200):
    """Read-only parse of a Target into people.

    Returns (people, last_message_id):
      people           — list of {tg_id, username, first_name, context}, de-duped.
      last_message_id  — new high-water mark to persist on the Target (unchanged
                         when only participants were read).

    channel → linked discussion comments (fallback: own messages, then members).
    chat    → recent messages (fallback: members).
    FLOOD_WAIT → tgclient.AccountError("flood"); other MTProto errors classified.
    """
    kind = (getattr(target, "kind", None) or "channel").lower()
    min_id = int(getattr(target, "last_message_id", 0) or 0)
    people: dict = {}
    last_id = min_id

    try:
        entity = await tgclient.resolve(client, target.ref)

        if kind == "channel":
            discussion = await _linked_discussion(client, entity)
            if discussion is not None:
                last_id = await _collect_messages(client, discussion, limit, min_id, people)
            else:
                # No comments chat: try the entity's own messages (megagroups),
                # then members as identity-only leads.
                last_id = await _collect_messages(client, entity, limit, min_id, people)
            if not people:
                await _collect_participants(client, entity, limit, people)
        else:  # chat / group
            last_id = await _collect_messages(client, entity, limit, min_id, people)
            if not people:
                await _collect_participants(client, entity, limit, people)

    except tg_errors.FloodWaitError as error:
        raise tgclient.AccountError("flood", str(error), retry_after=getattr(error, "seconds", None))
    except tgclient.AccountError:
        raise
    except Exception as error:
        raise tgclient._classify(error)

    return list(people.values()), last_id


# --- persistence (sync SQLModel) ---------------------------------------------
def people_to_leads(session: Session, campaign_id: int, target_id: Optional[int], people: list) -> int:
    """Upsert parsed people into Lead rows; return how many were NEW.

    De-dupes by (campaign_id, tg_id) — existing leads are skipped (never
    overwritten), matching findOrCreateParsedPeople. Commits once at the end.
    """
    new_count = 0
    seen: set = set()

    for person in people or []:
        tg_id = person.get("tg_id")
        if tg_id is None or tg_id in seen:
            continue
        seen.add(tg_id)

        existing = session.exec(
            select(Lead).where(Lead.campaign_id == campaign_id, Lead.tg_id == tg_id)
        ).first()
        if existing is not None:
            continue

        context = (person.get("context") or "").strip() or None
        session.add(
            Lead(
                campaign_id=campaign_id,
                tg_id=tg_id,
                username=person.get("username"),
                first_name=person.get("first_name"),
                source_target_id=target_id,
                context=context,
            )
        )
        new_count += 1

    if new_count:
        session.commit()

    return new_count
