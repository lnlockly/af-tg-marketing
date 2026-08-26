"""
tgclient.py — Telethon client + core MTProto ops.

Light Python/Telethon reimplementation of the leads42 session/proxy/client
lifecycle (src/telegram/telegram-client-manager.service.ts) and send path
(src/telegram/telegram.service.ts + telegram-sender.service.ts). No client
cache map, no NestJS DI — each caller builds/connects a client for one Account.

Session is a native Telethon StringSession: StringSession(account.session).
Hard failures (dead session, ban, flood, proxy) raise AccountError with a
classified `reason` so callers (engine loops / CLIs) can cooldown/deactivate.
"""
from __future__ import annotations

import asyncio
import os
import re
from typing import Optional

import json

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon import errors as tg_errors

from tgengine.db import Account, Proxy
from tgengine import fingerprints


# --- env knobs (mirror leads42 ACCOUNT_* tunables) ---------------------------
def _int_env(name: str, default: int) -> int:
    try:
        raw = int(os.environ.get(name, ""))
        return raw if raw > 0 else default
    except (TypeError, ValueError):
        return default


CONNECT_TIMEOUT = _int_env("TG_CONNECT_TIMEOUT", 15)
BOT_RESPONSE_TIMEOUT = _int_env("BOT_RESPONSE_TIMEOUT", 30)
CONNECTION_RETRIES = _int_env("TG_CONNECTION_RETRIES", 3)


# --- error classification (ported from handleAccountError switch) ------------
class AccountError(Exception):
    """A hard MTProto failure for one account, classified by `reason`:

    dead      — session invalid / revoked / user deactivated / frozen (deactivate)
    banned    — account banned / peer-flood (deactivate + ban)
    flood     — FLOOD_WAIT / "A wait of N seconds"; `retry_after` seconds set
    proxy     — proxy / network transport error (cooldown, maybe deactivate)
    unauthorized — connected but session not authorized
    error     — anything else
    """

    def __init__(self, reason: str, message: str, retry_after: Optional[int] = None):
        super().__init__(message)
        self.reason = reason
        self.retry_after = retry_after


_DEAD_RE = re.compile(
    r"AUTH_KEY_UNREGISTERED|AUTH_KEY_DUPLICATED|SESSION_REVOKED|USER_DEACTIVATED|FROZEN",
    re.I,
)
_BAN_RE = re.compile(r"\bBAN\b|DEACTIVATED|PEER_FLOOD", re.I)
_PROXY_RE = re.compile(
    r"PROXY|SOCKS|ECONNREFUSED|ECONNRESET|ETIMEDOUT|EHOSTUNREACH|ENETUNREACH|"
    r"ECONNABORTED|CONNECTION CLOSED|TIMEOUT|NOT CONNECTED|socket",
    re.I,
)
_FLOOD_RE = re.compile(r"FLOOD_WAIT|A wait of \d+ seconds is required", re.I)


def _extract_flood_seconds(message: str) -> Optional[int]:
    m = re.search(r"FLOOD_WAIT_?(\d+)", message, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"A wait of (\d+) seconds is required", message, re.I)
    if m:
        return int(m.group(1))
    return None


def _classify(error: Exception) -> AccountError:
    """Map an arbitrary Telethon/transport error to an AccountError."""
    if isinstance(error, AccountError):
        return error

    message = str(error) or error.__class__.__name__

    if isinstance(error, tg_errors.FloodWaitError):
        seconds = getattr(error, "seconds", None) or _extract_flood_seconds(message)
        return AccountError("flood", message, retry_after=seconds)
    if isinstance(error, tg_errors.PeerFloodError):
        return AccountError("banned", message)
    if isinstance(
        error,
        (
            tg_errors.AuthKeyUnregisteredError,
            tg_errors.AuthKeyDuplicatedError,
            tg_errors.SessionRevokedError,
            tg_errors.UserDeactivatedError,
        ),
    ):
        return AccountError("dead", message)
    if isinstance(error, tg_errors.UserDeactivatedBanError):
        return AccountError("banned", message)

    if _DEAD_RE.search(message):
        return AccountError("dead", message)
    if _BAN_RE.search(message):
        return AccountError("banned", message)
    if _FLOOD_RE.search(message):
        return AccountError("flood", message, retry_after=_extract_flood_seconds(message))
    if _PROXY_RE.search(message) or isinstance(error, (OSError, asyncio.TimeoutError)):
        return AccountError("proxy", message)
    return AccountError("error", message)


# --- proxy / client lifecycle ------------------------------------------------
def parse_proxy(proxy: Optional[Proxy]) -> Optional[tuple]:
    """Build the Telethon socks5 proxy tuple from a Proxy row, or None.

    Telethon (PySocks) tuple: (proxy_type, addr, port, rdns, username, password).
    """
    if proxy is None or not proxy.host:
        return None

    try:  # PySocks constant if available; else the string form Telethon accepts
        import socks  # type: ignore

        proxy_type = socks.SOCKS5 if (proxy.kind or "socks5") == "socks5" else socks.SOCKS4
    except Exception:
        proxy_type = proxy.kind or "socks5"

    username = proxy.username or None
    password = proxy.password or None
    return (proxy_type, proxy.host, int(proxy.port), True, username, password)


def build_client(account: Account, proxy: Optional[Proxy]) -> TelegramClient:
    """Construct (do not connect) a TelegramClient for an account.

    Uses the account's Telethon StringSession, app_id/app_hash, and proxy.
    """
    # Apply the account's STABLE desktop fingerprint (device_model/system_version/
    # app_version/lang_code/system_lang_code) so the session looks like a real
    # Telegram Desktop client, not a default library client. Coherent with app 2040.
    device = {}
    if account.device:
        try:
            device = fingerprints.as_client_kwargs(json.loads(account.device))
        except Exception:
            device = {}
    return TelegramClient(
        StringSession(account.session),
        int(account.app_id),
        account.app_hash,
        proxy=parse_proxy(proxy),
        connection_retries=CONNECTION_RETRIES,
        auto_reconnect=True,
        timeout=CONNECT_TIMEOUT,
        request_retries=CONNECTION_RETRIES,
        **device,
    )


async def connect(account: Account, proxy: Optional[Proxy]) -> TelegramClient:
    """Build + connect a client, raising AccountError if it is not authorized.

    Mirrors createClient: connect, verify connected, verify session authorized.
    On any failure the client is torn down before the error propagates.
    """
    client = build_client(account, proxy)
    try:
        await client.connect()
        if not client.is_connected():
            raise AccountError("proxy", "failed to establish connection to Telegram")
        if not await client.is_user_authorized():
            raise AccountError("dead", "session is not authorized")
        return client
    except Exception as error:
        try:
            await client.disconnect()
        except Exception:
            pass
        raise _classify(error)


# --- core ops ----------------------------------------------------------------
async def whoami(client: TelegramClient) -> dict:
    """Return {id, username, phone, first_name} for the connected account."""
    try:
        me = await client.get_me()
    except Exception as error:
        raise _classify(error)
    if me is None:
        raise AccountError("dead", "get_me returned no user (unauthorized)")
    return {
        "id": me.id,
        "username": getattr(me, "username", None),
        "phone": getattr(me, "phone", None),
        "first_name": getattr(me, "first_name", None),
    }


async def resolve(client: TelegramClient, ref: str):
    """Resolve an entity from @username, a t.me link, or a numeric id.

    Ported from resolveEntityBeforeSend: try the ref directly, then a
    normalized @username variant, then a dialogs fallback.
    """
    target = _normalize_ref(ref)
    try:
        return await client.get_entity(target)
    except Exception as first_error:
        # numeric id
        if isinstance(target, str) and target.lstrip("-").isdigit():
            try:
                return await client.get_entity(int(target))
            except Exception:
                pass
        # dialogs fallback for a username
        if isinstance(target, str):
            needle = target.lstrip("@").lower()
            try:
                async for dialog in client.iter_dialogs(limit=300):
                    entity = dialog.entity
                    if entity is None:
                        continue
                    uname = (getattr(entity, "username", None) or "").lower()
                    eid = str(getattr(entity, "id", "") or "")
                    if uname == needle or eid == needle:
                        return entity
            except Exception:
                pass
        raise _classify(first_error)


def _normalize_ref(ref: str):
    """Turn a raw reference into something get_entity understands."""
    ref = (ref or "").strip()
    m = re.search(r"(?:https?://)?t\.me/(?:joinchat/)?(@?[\w+/-]+)", ref, re.I)
    if m:
        ref = m.group(1)
    if ref.lstrip("-").isdigit():
        return int(ref)
    return ref


async def send_message(client: TelegramClient, ref_or_entity, text: str) -> int:
    """Send `text` to a username / link / id / resolved entity; return message id.

    Resolves strings via resolve(); passes entities straight through. Wraps the
    send in a timeout and classifies flood/proxy/dead failures as AccountError.
    """
    try:
        entity = ref_or_entity
        if isinstance(ref_or_entity, str):
            entity = await resolve(client, ref_or_entity)

        sent = await asyncio.wait_for(
            client.send_message(entity, text),
            timeout=BOT_RESPONSE_TIMEOUT,
        )
        return sent.id
    except Exception as error:
        raise _classify(error)


async def disconnect(client: TelegramClient) -> None:
    """Best-effort teardown (never raises)."""
    try:
        await client.disconnect()
    except Exception:
        pass
