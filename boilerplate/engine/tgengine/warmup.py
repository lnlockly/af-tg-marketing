"""
warmup.py — account "прогрев" (warmup) primitives.

🔴 WARMUP IS READ-ONLY / PASSIVE. During warmup the account NEVER sends a message
to anyone (initiating a DM is the single fastest way Telegram's Anti-Spam v2 flags
a fresh account, and self/peer pinging looks robotic). The only actions are the
things an ordinary new user does: confirm the session, join a few public channels,
scroll their feeds, and put the occasional reaction/like on a post. When the account
has done ACTIONS_TO_READY of these it is promoted to `ready` and may run campaigns.

A freshly-ingested account is `cold`. The engine's warmup_loop drives it through
paced, human-looking actions until ready. Per-account progress is a counter on the
Account row (no warmup-conversation tables).

Hard MTProto failures (FLOOD_WAIT, dead session, proxy) raise tgclient.AccountError
so the caller can cooldown/deactivate the account.
"""
from __future__ import annotations

import random
from typing import Optional

from telethon.tl import functions, types

from tgengine import tgclient


# --- intensity configs --------------------------------------------------------
# durationDays is informational; delay_min/max are seconds between actions;
# channels_per_account caps how many channels a single account touches; the weights
# feed the cumulative selector. NO messaging weight exists — warmup never writes.
INTENSITY = {
    "low": {
        "duration_days": 7,
        "delay_min": 240,
        "delay_max": 900,
        "channels_per_account": 12,
        "weights": {"self_check": 15, "channel_join": 8, "react": 12},
    },
    "medium": {
        "duration_days": 6,
        "delay_min": 120,
        "delay_max": 600,
        "channels_per_account": 16,
        "weights": {"self_check": 12, "channel_join": 12, "react": 20},
    },
    "high": {
        "duration_days": 5,
        "delay_min": 60,
        "delay_max": 300,
        "channels_per_account": 22,
        "weights": {"self_check": 10, "channel_join": 16, "react": 30},
    },
}

# ~10 neutral, popular PUBLIC channels to view / join during warmup. Broad-interest,
# non-niche, uncontroversial — the sort of channels an ordinary new user follows.
DEFAULT_WARMUP_CHANNELS = [
    "telegram",
    "durov",
    "tginfo",
    "TelegramTips",
    "trends",
    "contest",
    "designers",
    "Tech",
    "science",
    "books",
]

# How many warmup actions promote an account cold/warming -> ready.
ACTIONS_TO_READY = 25

# Positive, common reactions an ordinary user leaves on posts. Only these — nothing
# that requires premium or looks unusual.
REACT_EMOJI = ("👍", "🔥", "❤️", "👏", "😁", "🙏")

# Passive, read-only warmup actions. NO messaging action exists by design.
_ACTIONS = ("self_check", "channel_join", "channel_view", "react")


def get_intensity(intensity: str = "medium") -> dict:
    """Return the intensity config, defaulting to medium for unknown values."""
    return INTENSITY.get(intensity, INTENSITY["medium"])


def pick_action(actions_count: int, account_id: int, intensity: str = "medium") -> str:
    """Weighted, deterministic action selector over PASSIVE actions only.

        selector = (actions_count + account_id) % 100
        selector < self_check                                  -> self_check
        selector < self_check + channel_join                  -> channel_join
        selector < self_check + channel_join + react          -> react
        else                                                  -> channel_view

    The remaining probability mass (100 - sum of weights) lands on channel_view —
    the cheapest, most common action.
    """
    weights = get_intensity(intensity)["weights"]
    self_check = weights["self_check"]
    channel_join = weights["channel_join"]
    react = weights["react"]

    selector = (actions_count + account_id) % 100
    if selector < self_check:
        return "self_check"
    if selector < self_check + channel_join:
        return "channel_join"
    if selector < self_check + channel_join + react:
        return "react"
    return "channel_view"


async def do_action(client, action: str, *, channels, peers=None) -> str:
    """Execute one PASSIVE warmup action on a connected client; return a short detail.
    `peers` is accepted for backward-compat and IGNORED — warmup never messages.

      - self_check   -> get_me (confirms the session is alive / not frozen)
      - channel_join -> channels.JoinChannelRequest (subscribe)
      - channel_view -> iter_messages(channel, limit≈15) (scroll a feed)
      - react        -> put ONE positive reaction on a recent post (like a real user)

    Any FLOOD_WAIT / dead-session / proxy failure is classified and re-raised as
    tgclient.AccountError so the caller can cooldown/deactivate.
    """
    try:
        if action == "self_check":
            me = await client.get_me()
            if me is None:
                raise tgclient.AccountError("dead", "get_me returned no user")
            uname = getattr(me, "username", None)
            return f"self_check ok{(' @' + uname) if uname else ''}"

        if action == "channel_view":
            channel = _pick_channel(channels)
            if channel is None:
                return "channel_view skipped: no channels"
            viewed = 0
            async for _ in client.iter_messages(channel, limit=15):
                viewed += 1
            return f"viewed {viewed} messages in {channel}"

        if action == "channel_join":
            channel = _pick_channel(channels)
            if channel is None:
                return "channel_join skipped: no channels"
            await client(functions.channels.JoinChannelRequest(channel))
            return f"joined channel {channel}"

        if action == "react":
            channel = _pick_channel(channels)
            if channel is None:
                return "react skipped: no channels"
            posts = [m for m in await _recent_posts(client, channel, limit=12) if getattr(m, "id", None)]
            if not posts:
                return f"react skipped: no posts in {channel}"
            target = random.choice(posts)
            emoji = random.choice(REACT_EMOJI)
            try:
                await client(functions.messages.SendReactionRequest(
                    peer=channel,
                    msg_id=target.id,
                    reaction=[types.ReactionEmoji(emoticon=emoji)],
                ))
                return f"reacted {emoji} to a post in {channel}"
            except tgclient.AccountError:
                raise
            except Exception as react_err:  # noqa: BLE001
                cls = tgclient._classify(react_err)
                # a HARD account problem must still surface; a channel that simply
                # disallows this reaction is benign — just skip.
                if getattr(cls, "kind", "") in ("flood", "dead", "banned", "proxy", "unauthorized"):
                    raise cls
                return f"react skipped: {channel} reaction unavailable"

        return f"unknown action {action}"
    except tgclient.AccountError:
        raise
    except Exception as error:  # noqa: BLE001 — classify flood/dead/proxy uniformly
        raise tgclient._classify(error)


# --- internal helpers --------------------------------------------------------
def _pick_channel(channels) -> Optional[str]:
    """Choose one channel ref from the provided list, or None if empty."""
    pool = [str(c) for c in (channels or []) if c]
    if not pool:
        return None
    return random.choice(pool)


async def _recent_posts(client, channel, limit: int = 12) -> list:
    """Fetch a few recent posts from a channel (read-only)."""
    out = []
    async for m in client.iter_messages(channel, limit=limit):
        out.append(m)
    return out
