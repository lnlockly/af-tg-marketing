"""
warmup.py — account "прогрев" (warmup) primitives.

Light Python/Telethon reimplementation of the leads42 warmup spec:
  - src/warmup/warmup.constants.ts             (INTENSITY_CONFIGS, message templates)
  - src/warmup/warmup-execution.service.ts     (per-account action execution)
  - src/warmup/warmup.helpers.ts               (cumulative-weight selector, message pick)

A freshly-ingested account is `cold`. The engine's warmup_loop drives it through
paced, human-looking actions (self-check, view a channel, join a channel, DM a
peer account) until it has done ACTIONS_TO_READY actions, at which point it is
promoted to `ready` and may run campaigns. No warmup-campaign / conversation
tables (leads42's WarmupConversation) — v1 keeps a per-account action counter on
the Account row; internal DMs are one-shot friendly pings, not multi-turn chats.

Hard MTProto failures (FLOOD_WAIT, dead session, proxy) raise tgclient.AccountError
so the caller can cooldown/deactivate the account.
"""
from __future__ import annotations

import random
from typing import Optional

from telethon.tl import functions

from tgengine import tgclient


# --- intensity configs (ported from warmup.constants.ts INTENSITY_CONFIGS) ----
# durationDays is informational (how long a full warmup campaign would run);
# delay_min/max are seconds between actions; channels_per_account caps how many
# channels a single account touches; the weights feed the cumulative selector.
INTENSITY = {
    "low": {
        "duration_days": 7,
        "delay_min": 240,
        "delay_max": 900,
        "channels_per_account": 12,
        "weights": {"self_check": 15, "channel_join": 4, "internal": 10},
    },
    "medium": {
        "duration_days": 6,
        "delay_min": 120,
        "delay_max": 600,
        "channels_per_account": 16,
        "weights": {"self_check": 12, "channel_join": 6, "internal": 25},
    },
    "high": {
        "duration_days": 5,
        "delay_min": 60,
        "delay_max": 300,
        "channels_per_account": 22,
        "weights": {"self_check": 10, "channel_join": 8, "internal": 40},
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

# Friendly warmup DM templates (ported from WARMUP_MESSAGE_TEMPLATES).
WARMUP_MESSAGE_TEMPLATES = [
    "Привет. Как у тебя сегодня день идет?",
    "Смотрю каналы и проверяю активность. Как ты?",
    "Доброе время суток. Что сейчас читаешь в Telegram?",
    "Привет. Тестирую обычный диалог, как настроение?",
    "Зашел проверить связь. У тебя все нормально?",
    "Привет. Как проходит день?",
    "Сейчас читаю несколько каналов. Что интересного видел сегодня?",
    "Привет. Какой у тебя сегодня ритм работы?",
    "Тестовый дружелюбный вопрос: как дела?",
    "Привет. Что нового у тебя сегодня?",
]

_ACTIONS = ("self_check", "channel_join", "internal_message", "channel_view")


def get_intensity(intensity: str = "medium") -> dict:
    """Return the intensity config, defaulting to medium for unknown values
    (port of getIntensityConfig)."""
    return INTENSITY.get(intensity, INTENSITY["medium"])


def build_warmup_message(seed: int) -> str:
    """Pick a friendly warmup DM template deterministically by seed
    (port of buildWarmupMessage)."""
    return WARMUP_MESSAGE_TEMPLATES[abs(seed) % len(WARMUP_MESSAGE_TEMPLATES)]


def pick_action(actions_count: int, account_id: int, intensity: str = "medium") -> str:
    """Weighted, deterministic action selector.

    Ported from warmup-execution.service.ts processWarmupAccount:
        selector = (actionsCount + accountId + campaignId) % 100
        selector < selfCheckWeight                                   -> self_check
        selector < selfCheck + channelJoin                          -> channel_join
        selector < selfCheck + channelJoin + internal               -> internal_message
        else                                                        -> channel_view

    There is no campaign in v1, so the seed is (actions_count + account_id). The
    remaining probability mass (100 - sum of weights) lands on channel_view — the
    cheapest, most common action — exactly as in leads42.
    """
    weights = get_intensity(intensity)["weights"]
    self_check = weights["self_check"]
    channel_join = weights["channel_join"]
    internal = weights["internal"]

    selector = (actions_count + account_id) % 100
    if selector < self_check:
        return "self_check"
    if selector < self_check + channel_join:
        return "channel_join"
    if selector < self_check + channel_join + internal:
        return "internal_message"
    return "channel_view"


async def do_action(client, action: str, *, channels, peers=None) -> str:
    """Execute one warmup action on a connected client; return a short detail
    string. Ported from the execute* methods in warmup-execution.service.ts.

      - self_check       -> get_me (confirms the session is alive / not frozen)
      - channel_view     -> iter_messages(channel, limit≈15) (read a channel's feed)
      - channel_join     -> channels.JoinChannelRequest (subscribe; daily cap is the
                            caller's concern)
      - internal_message -> send a friendly ping to a peer account (skip if none)

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

        if action == "internal_message":
            peer = _pick_peer(peers)
            if peer is None:
                return "internal_message skipped: no peers with username"
            username = peer.username
            text = build_warmup_message(getattr(peer, "id", 0) or 0)
            await tgclient.send_message(client, f"@{username}", text)
            return f"internal_message -> @{username}"

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


def _pick_peer(peers):
    """Choose one peer Account that has a username, or None. `peers` is a list of
    Account rows (other warmup accounts) — the internal DM target."""
    candidates = [p for p in (peers or []) if getattr(p, "username", None)]
    if not candidates:
        return None
    return random.choice(candidates)
