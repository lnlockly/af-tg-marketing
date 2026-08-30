"""
engine.py — the asyncio daemon.

The control-plane is SQLite (db.py); this daemon scans due work by status + *_at
timestamps and drives the campaign loop. No HTTP, no Redis, no external queue —
just a few `while True: tick; await asyncio.sleep(N)` loops run concurrently with
`asyncio.gather`. Every DB call is sync SQLModel wrapped in `asyncio.to_thread`.

Ф0 scope: `mailing_loop` is FULLY FUNCTIONAL (new dialog → eligible account →
AI first message → send → status=sent). The other loops are clearly-marked STUBS
with real loop structure and `# TODO Фаза N` markers.

Logic ported from leads42:
  - src/dialog/services/dialog-scheduling.service.ts  (eligibility, daily caps,
    account selection, cooldown, work-hour gating)
  - src/dialog/sender.processor.ts                    (send-first-message flow,
    inactive/cooldown re-checks, terminal vs retryable failure handling)
  - src/campaign/helpers/campaign.helper.ts           (isWorkingHours semantics)
  - src/dialog/helpers/delay.ts                       (jittered send delay)
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlmodel import select

from tgengine import ai, matcher, parser, tgclient, warmup
from tgengine.db import (
    Account,
    Campaign,
    CampaignStatus,
    Dialog,
    DialogStatus,
    Lead,
    Message,
    MessageFrom,
    Proxy,
    Target,
    TargetStatus,
    WarmupPhase,
    get_session,
    init_db,
    now,
)

# --- config / logging --------------------------------------------------------
logging.basicConfig(
    stream=sys.stderr,
    level=os.environ.get("TGENGINE_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("tgengine.engine")

# Ф0 flag: skip the warmup-phase gate so a freshly-ingested account can send.
SKIP_WARMUP_GATE = os.environ.get("TGENGINE_SKIP_WARMUP", "true").lower() == "true"

MAILING_TICK_SECONDS = int(os.environ.get("TGENGINE_MAILING_TICK", "30"))
INBOUND_TICK_SECONDS = int(os.environ.get("TGENGINE_INBOUND_TICK", "20"))
MATCH_TICK_SECONDS = int(os.environ.get("TGENGINE_MATCH_TICK", "60"))
PARSE_TICK_SECONDS = int(os.environ.get("TGENGINE_PARSE_TICK", "120"))
WARMUP_TICK_SECONDS = int(os.environ.get("TGENGINE_WARMUP_TICK", "300"))

# Ф3 warmup: pace/weights preset (low|medium|high, from warmup.INTENSITY).
WARMUP_INTENSITY = os.environ.get("TGENGINE_WARMUP_INTENSITY", "medium")
# Accounts still being warmed (drive them through warmup actions to `ready`).
_WARMUP_PHASES = (WarmupPhase.cold, WarmupPhase.warming)
WARMUP_ACCOUNTS_PER_TICK = int(os.environ.get("TGENGINE_WARMUP_ACCOUNTS", "25"))

# Ф1 knobs
PARSE_LIMIT = int(os.environ.get("TGENGINE_PARSE_LIMIT", "200"))          # messages/people per target scan
PARSE_TARGETS_PER_TICK = int(os.environ.get("TGENGINE_PARSE_TARGETS", "1"))  # gentle: one target per campaign per tick
MATCH_LIMIT = int(os.environ.get("TGENGINE_MATCH_LIMIT", "10"))          # leads classified per campaign per tick (LLM cost)

# Ф2 knobs
INBOUND_DIALOGS_PER_CAMPAIGN = int(os.environ.get("TGENGINE_INBOUND_DIALOGS", "50"))  # dialogs polled/campaign/tick
INBOUND_POLL_LIMIT = int(os.environ.get("TGENGINE_INBOUND_POLL_LIMIT", "30"))         # messages scanned back per dialog
HISTORY_LIMIT = int(os.environ.get("TGENGINE_HISTORY_LIMIT", "50"))                   # messages fed to reply/score
# Cooldown seconds applied on classified AccountError (leads42 handleAccountError intent).
COOLDOWN_FLOOD_DEFAULT = int(os.environ.get("TGENGINE_COOLDOWN_FLOOD", "300"))
COOLDOWN_PROXY_SECONDS = int(os.environ.get("TGENGINE_COOLDOWN_PROXY", "120"))
COOLDOWN_DEAD_SECONDS = int(os.environ.get("TGENGINE_COOLDOWN_DEAD", "3600"))


# --- helpers (ported from leads42) -------------------------------------------
def in_work_hours(campaign: Campaign, at: Optional[datetime] = None) -> bool:
    """Port of isWorkingHours (campaign.helper.ts). Half-open window [start, end):
    hour H works if start <= H < end. start > end crosses midnight; start == end
    means round-the-clock. Uses local pod time (single-tenant pod per campaign)."""
    start = campaign.work_start
    end = campaign.work_end
    if start is None or end is None:
        return False
    if start == end:
        return True
    hour = (at or datetime.now()).hour
    if start > end:  # window crosses midnight, e.g. 22-6
        return hour >= start or hour < end
    return start <= hour < end


def account_on_cooldown(account: Account, at: Optional[datetime] = None) -> bool:
    """Port of isAccountOnCooldown."""
    if not account.cooldown_until:
        return False
    return account.cooldown_until > (at or now())


def account_eligible(account: Account, campaign: Campaign) -> bool:
    """Port of the validAccounts filter in getRandomAccountByCampaign +
    the inactive/cooldown re-checks in sender.processor.ts. An account may send if
    it is active, warm enough (unless the Ф0 warmup gate is skipped), under the
    per-day cap, and not on cooldown."""
    if not account.is_active:
        return False
    if not SKIP_WARMUP_GATE and account.warmup_phase != WarmupPhase.ready:
        return False
    if account.dialogs_started_today >= campaign.max_new_per_account_per_day:
        return False
    if account_on_cooldown(account):
        return False
    return True


def jittered_delay(campaign: Campaign) -> float:
    """Port of getDelay(min, max): random whole-second delay in [min, max]."""
    lo = campaign.msg_delay_min
    hi = max(campaign.msg_delay_max, lo)
    return float(random.randint(lo, hi))


# --- sync DB units (each is run via asyncio.to_thread) -----------------------
def _fetch_active_campaigns() -> list[Campaign]:
    with get_session() as s:
        return list(s.exec(select(Campaign).where(Campaign.status == CampaignStatus.active)).all())


def _fetch_new_dialogs(campaign_id: int, limit: int = 50) -> list[Dialog]:
    with get_session() as s:
        stmt = (
            select(Dialog)
            .where(Dialog.campaign_id == campaign_id, Dialog.status == DialogStatus.new)
            .order_by(Dialog.created_at)
            .limit(limit)
        )
        return list(s.exec(stmt).all())


def _pick_eligible_account(campaign: Campaign, preferred_id: Optional[int]) -> Optional[Account]:
    """Mirror processSingleDialog + getRandomAccountByCampaign: keep the dialog's
    assigned account if still eligible, otherwise pick a random eligible one."""
    with get_session() as s:
        if preferred_id is not None:
            acc = s.get(Account, preferred_id)
            if acc and account_eligible(acc, campaign):
                return acc
        eligible = [a for a in s.exec(select(Account)).all() if account_eligible(a, campaign)]
        if not eligible:
            return None
        return random.choice(eligible)


def _load_lead(lead_id: int) -> Optional[Lead]:
    with get_session() as s:
        return s.get(Lead, lead_id)


def _load_proxy(proxy_id: Optional[int]) -> Optional[Proxy]:
    if proxy_id is None:
        return None
    with get_session() as s:
        return s.get(Proxy, proxy_id)


def _mark_dialog_sent(dialog_id: int, account_id: int, text: str, msg_id: int = 0) -> None:
    with get_session() as s:
        dialog = s.get(Dialog, dialog_id)
        account = s.get(Account, account_id)
        if not dialog or not account:
            return
        ts = now()
        dialog.status = DialogStatus.sent
        dialog.account_id = account_id
        dialog.first_message = text
        dialog.last_message_at = ts
        if msg_id:
            dialog.last_msg_id = msg_id     # inbound cursor: only replies AFTER this count
        s.add(dialog)
        s.add(Message(dialog_id=dialog_id, sender=MessageFrom.account, text=text))
        account.dialogs_started_today += 1
        account.last_send_at = ts
        s.add(account)
        s.commit()


def _release_dialog(dialog_id: int, terminal: bool, reason: str) -> None:
    """Port of sender.processor.ts failure handling: terminal errors close the
    dialog; retryable ones return it to `new` and drop the account assignment."""
    with get_session() as s:
        dialog = s.get(Dialog, dialog_id)
        if not dialog:
            return
        if terminal:
            dialog.status = DialogStatus.closed
        else:
            dialog.status = DialogStatus.new
            dialog.account_id = None
        dialog.notes = reason[:250]
        s.add(dialog)
        s.commit()


def _reset_all_daily_counters() -> int:
    with get_session() as s:
        accounts = list(s.exec(select(Account)).all())
        for a in accounts:
            a.dialogs_started_today = 0
            s.add(a)
        s.commit()
        return len(accounts)


# --- Ф1 parse/match sync DB units --------------------------------------------
def _fetch_pending_targets(campaign_id: int, limit: int = PARSE_TARGETS_PER_TICK) -> list[Target]:
    with get_session() as s:
        stmt = (
            select(Target)
            .where(Target.campaign_id == campaign_id, Target.status == TargetStatus.pending)
            .order_by(Target.created_at)
            .limit(limit)
        )
        return list(s.exec(stmt).all())


def _claim_target(target_id: int) -> Optional[Target]:
    """Atomically move a Target pending -> parsing so a concurrent tick can't
    double-parse it. Refresh+expunge so the detached row's columns stay readable."""
    with get_session() as s:
        target = s.get(Target, target_id)
        if not target or target.status != TargetStatus.pending:
            return None
        target.status = TargetStatus.parsing
        s.add(target)
        s.commit()
        s.refresh(target)
        s.expunge(target)
        return target


def _pick_parse_account(target_id: int) -> Optional[Account]:
    """Round-robin an active account for read-only parsing. Ports the leads42
    selection intent: activeAccountIds[(max(entity.id,1)-1) % n] (parser.service.ts)."""
    with get_session() as s:
        accounts = list(
            s.exec(select(Account).where(Account.is_active == True).order_by(Account.id)).all()  # noqa: E712
        )
        if not accounts:
            return None
        idx = (max(target_id, 1) - 1) % len(accounts)
        return accounts[idx]


def _people_to_leads(campaign_id: int, target_id: int, people: list) -> int:
    with get_session() as s:
        n = parser.people_to_leads(s, campaign_id, target_id, people)
        s.commit()
        return n


def _finish_target(target_id: int, last_message_id: int, status: TargetStatus) -> None:
    with get_session() as s:
        target = s.get(Target, target_id)
        if not target:
            return
        if last_message_id:
            target.last_message_id = last_message_id
        target.status = status
        s.add(target)
        s.commit()


def _classify_new_leads(campaign: Campaign, limit: int = MATCH_LIMIT) -> dict:
    with get_session() as s:
        return matcher.classify_new_leads(s, campaign, limit)


def _is_terminal_send_error(err: Exception) -> bool:
    """Port of isTerminalFirstMessageError: username-gone / entity-invalid errors
    can never succeed, so close the dialog instead of retrying."""
    msg = str(err).lower()
    return (
        "username_not_occupied" in msg
        or "username invalid" in msg
        or 'no user has "' in msg
    )


# --- Ф2 inbound sync DB units ------------------------------------------------
# leads42 processDialogs polls the account's active dialogs (campaign active,
# status in the automation set) and handleIncomingMessage persists inbound
# messages + flips status. We key by campaign here (single loop) instead of by
# account, but the eligibility set is the same automation statuses.
_INBOUND_STATUSES = (DialogStatus.sent, DialogStatus.answered, DialogStatus.inprogress)


def _fetch_inbound_dialogs(campaign_id: int, limit: int = INBOUND_DIALOGS_PER_CAMPAIGN) -> list[Dialog]:
    with get_session() as s:
        stmt = (
            select(Dialog)
            .where(
                Dialog.campaign_id == campaign_id,
                Dialog.auto_mode == True,  # noqa: E712
                Dialog.status.in_(_INBOUND_STATUSES),  # type: ignore[attr-defined]
                Dialog.account_id != None,  # noqa: E711 — must have a sending account
            )
            .order_by(Dialog.last_message_at)
            .limit(limit)
        )
        return list(s.exec(stmt).all())


def _load_account(account_id: Optional[int]) -> Optional[Account]:
    if account_id is None:
        return None
    with get_session() as s:
        return s.get(Account, account_id)


def _dialog_history(dialog_id: int, limit: int = HISTORY_LIMIT) -> list[Message]:
    """Last `limit` messages, chronological (ascending). Mirrors the ordered
    message load feeding generateReply / analyzeDialog in leads42."""
    with get_session() as s:
        stmt = (
            select(Message)
            .where(Message.dialog_id == dialog_id)
            .order_by(Message.created_at.desc(), Message.id.desc())  # type: ignore[attr-defined]
            .limit(limit)
        )
        rows = list(s.exec(stmt).all())
        rows.reverse()
        return rows


def _save_inbound(dialog_id: int, texts: list[str], last_in_id: int = 0) -> None:
    """Persist new inbound messages as Message(sender=user); flip the dialog to
    `answered` + bump last_message_at (handleIncomingMessage, simplified per
    CONTRACT Ф2: inbound always lands as answered). Advance the inbound cursor to
    the highest incoming id so the same message is never reprocessed."""
    with get_session() as s:
        dialog = s.get(Dialog, dialog_id)
        if not dialog:
            return
        ts = now()
        for text in texts:
            s.add(Message(dialog_id=dialog_id, sender=MessageFrom.user, text=text))
        dialog.status = DialogStatus.answered
        dialog.last_message_at = ts
        if last_in_id:
            dialog.last_msg_id = max(dialog.last_msg_id, last_in_id)
        s.add(dialog)
        s.commit()


def _save_reply(dialog_id: int, text: str, reply_id: int = 0) -> None:
    """Persist our auto-reply as Message(sender=account); flip to `inprogress`
    (sendReplyMessage flow). Advance the cursor past our reply too."""
    with get_session() as s:
        dialog = s.get(Dialog, dialog_id)
        if not dialog:
            return
        ts = now()
        s.add(Message(dialog_id=dialog_id, sender=MessageFrom.account, text=text))
        dialog.status = DialogStatus.inprogress
        dialog.last_message_at = ts
        if reply_id:
            dialog.last_msg_id = max(dialog.last_msg_id, reply_id)
        s.add(dialog)
        s.commit()


def _set_interest(dialog_id: int, score: int, threshold: int) -> bool:
    """Store the interest score; if score >= threshold flip to `success`
    (HOT LEAD). Port of analysis-dialog.service.ts: interest >= INTEREST_THRESHOLD
    → status=success. Returns whether the dialog became hot."""
    with get_session() as s:
        dialog = s.get(Dialog, dialog_id)
        if not dialog:
            return False
        dialog.interest_score = score
        hot = score >= threshold
        if hot:
            dialog.status = DialogStatus.success
        s.add(dialog)
        s.commit()
        return hot


def _cooldown_account(account_id: int, seconds: int, reason: str, deactivate: bool) -> None:
    """Put an account on cooldown (and optionally deactivate it) after a
    classified AccountError. Port of handleAccountError's cooldown/deactivate
    branches (dead/banned deactivate; flood/proxy just cool down)."""
    with get_session() as s:
        account = s.get(Account, account_id)
        if not account:
            return
        account.cooldown_until = now() + timedelta(seconds=max(seconds, 1))
        if deactivate:
            account.is_active = False
            account.deactivated_reason = reason[:250]
        s.add(account)
        s.commit()


def _epoch(dt: Optional[datetime]) -> float:
    """UTC epoch seconds. Naive datetimes (our db.now()=utcnow) are treated as
    UTC so they compare correctly against Telethon's tz-aware message.date."""
    if dt is None:
        return 0.0
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc).timestamp()
    return dt.timestamp()


# --- loops -------------------------------------------------------------------
async def mailing_loop() -> None:
    """FUNCTIONAL (Ф0). For each active campaign inside its work hours, take its
    `new` dialogs, assign an eligible account, generate the first message via the
    LLM, send it over MTProto, and flip the dialog to `sent`. Jittered delay
    between sends to look human (getDelay)."""
    while True:
        try:
            await _mailing_pass()
        except Exception:  # keep the daemon alive across ticks
            log.exception("mailing_loop tick failed")
        await asyncio.sleep(MAILING_TICK_SECONDS)


async def _mailing_pass() -> None:
    """One mailing sweep across all active campaigns (shared by mailing_loop and
    the --once mode)."""
    campaigns = await asyncio.to_thread(_fetch_active_campaigns)
    for campaign in campaigns:
        if not in_work_hours(campaign):
            log.info("campaign %s outside work hours (%s-%s), skip",
                     campaign.id, campaign.work_start, campaign.work_end)
            continue
        dialogs = await asyncio.to_thread(_fetch_new_dialogs, campaign.id)
        for dialog in dialogs:
            await _send_first_message(campaign, dialog)
            await asyncio.sleep(jittered_delay(campaign))


async def _send_first_message(campaign: Campaign, dialog: Dialog) -> None:
    """One dialog's send flow, ported from sender.processor.ts:sendFirstMessage."""
    account = await asyncio.to_thread(_pick_eligible_account, campaign, dialog.account_id)
    if account is None:
        log.warning("no eligible account for dialog %s (campaign %s), skip",
                    dialog.id, campaign.id)
        return

    lead = await asyncio.to_thread(_load_lead, dialog.lead_id)
    if lead is None:
        await asyncio.to_thread(_release_dialog, dialog.id, True, "lead_missing")
        return

    client = None
    try:
        proxy = await asyncio.to_thread(_load_proxy, account.proxy_id)
        text = await asyncio.to_thread(ai.generate_first_message, campaign, lead)
        client = await tgclient.connect(account, proxy)
        ref = lead.username or lead.tg_id
        if ref is None:
            await asyncio.to_thread(_release_dialog, dialog.id, True, "lead_no_ref")
            return
        message_id = await tgclient.send_message(client, ref, text)
        await asyncio.to_thread(_mark_dialog_sent, dialog.id, account.id, text, message_id)
        log.info("dialog %s: first message sent via account %s (msg_id=%s)",
                 dialog.id, account.id, message_id)
    except tgclient.AccountError as err:
        terminal = _is_terminal_send_error(err)
        log.error("dialog %s: send failed (account %s, terminal=%s): %s",
                  dialog.id, account.id, terminal, err)
        await asyncio.to_thread(_release_dialog, dialog.id, terminal, f"first_message_failed: {err}")
    except Exception as err:  # noqa: BLE001 — unknown failures are retryable
        log.exception("dialog %s: unexpected send failure", dialog.id)
        await asyncio.to_thread(_release_dialog, dialog.id, False, f"first_message_failed: {err}")
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass


async def inbound_loop() -> None:
    """FUNCTIONAL (Ф2). For each active campaign inside its work hours, poll each
    auto-mode dialog (status sent/answered/inprogress) for new inbound messages
    from the lead, persist them as Message(sender=user) → status=answered, then
    auto-reply via the LLM (Message(sender=account) → status=inprogress) and score
    the conversation; interest >= threshold flips the dialog to `success`
    (HOT LEAD). Ported from leads42 processDialogs / handleIncomingMessage
    (dialog.service.ts), sendReplyMessage (sender.processor.ts) and the
    interest→success rule (analysis-dialog.service.ts)."""
    while True:
        try:
            await _inbound_pass()
        except Exception:
            log.exception("inbound_loop tick failed")
        await asyncio.sleep(INBOUND_TICK_SECONDS)


async def _inbound_pass() -> None:
    """One inbound sweep across all active campaigns (shared by inbound_loop and
    the --once mode)."""
    campaigns = await asyncio.to_thread(_fetch_active_campaigns)
    for campaign in campaigns:
        if not in_work_hours(campaign):
            log.info("campaign %s outside work hours (%s-%s), skip inbound",
                     campaign.id, campaign.work_start, campaign.work_end)
            continue
        dialogs = await asyncio.to_thread(_fetch_inbound_dialogs, campaign.id)
        for dialog in dialogs:
            await _handle_inbound_dialog(campaign, dialog)


async def _poll_new_inbound(client, entity, since_id: int) -> tuple[list[str], int]:
    """Return (texts, max_seen_id) of incoming messages with Telegram id > `since_id`,
    chronological. Keys off MESSAGE ID (like leads42's msgId cursor), NOT timestamps —
    Telegram dates are whole-second, so a reply arriving the same second as our send
    would be lost by a time cutoff. Telethon yields newest-first; stop once we reach a
    message id <= the cursor."""
    collected: list[tuple[int, str]] = []
    max_seen = since_id
    async for m in client.iter_messages(entity, limit=INBOUND_POLL_LIMIT):
        if m.id <= since_id:
            break  # reached already-seen history (newest-first ⇒ rest is older)
        if getattr(m, "out", False):
            continue  # our own outbound message (still advances nothing we act on)
        text = getattr(m, "message", None) or getattr(m, "text", None)
        if not text:
            continue  # service/media-only message
        collected.append((m.id, text))
        max_seen = max(max_seen, m.id)
    collected.sort(key=lambda item: item[0])
    return [text for _, text in collected], max_seen


async def _handle_account_error(account_id: int, err: "tgclient.AccountError") -> None:
    """Apply the cooldown/deactivate policy for a classified AccountError
    (handleAccountError branches)."""
    reason = getattr(err, "reason", "error")
    if reason == "flood":
        seconds = getattr(err, "retry_after", None) or COOLDOWN_FLOOD_DEFAULT
        await asyncio.to_thread(_cooldown_account, account_id, seconds, f"flood: {err}", False)
    elif reason in ("dead", "banned"):
        await asyncio.to_thread(_cooldown_account, account_id, COOLDOWN_DEAD_SECONDS, f"{reason}: {err}", True)
    else:  # proxy / unauthorized / error — cool down, keep the account active
        await asyncio.to_thread(_cooldown_account, account_id, COOLDOWN_PROXY_SECONDS, f"{reason}: {err}", False)


async def _handle_inbound_dialog(campaign: Campaign, dialog: Dialog) -> None:
    """One dialog's inbound turn: poll → persist inbound → auto-reply → score.
    Ported from handleIncomingMessage + sendReplyMessage + analyzeDialog."""
    account = await asyncio.to_thread(_load_account, dialog.account_id)
    if account is None or not account.is_active:
        return
    if account_on_cooldown(account):
        log.info("dialog %s: account %s on cooldown, skip", dialog.id, dialog.account_id)
        return

    lead = await asyncio.to_thread(_load_lead, dialog.lead_id)
    if lead is None:
        return
    ref = lead.username or lead.tg_id
    if ref is None:
        return

    client = None
    try:
        proxy = await asyncio.to_thread(_load_proxy, account.proxy_id)
        client = await tgclient.connect(account, proxy)
        entity = await tgclient.resolve(client, str(ref))

        new_texts, max_in_id = await _poll_new_inbound(client, entity, dialog.last_msg_id)
        if not new_texts:
            return

        await asyncio.to_thread(_save_inbound, dialog.id, new_texts, max_in_id)
        log.info("dialog %s: %s new inbound message(s), status=answered",
                 dialog.id, len(new_texts))

        # Auto-reply (dialog is auto_mode by the _fetch filter). Build history
        # AFTER persisting inbound so the reply sees the latest turn.
        history = await asyncio.to_thread(_dialog_history, dialog.id, HISTORY_LIMIT)
        reply = await asyncio.to_thread(ai.generate_reply, campaign, dialog, history)
        if reply:
            reply_id = await tgclient.send_message(client, entity, reply)
            await asyncio.to_thread(_save_reply, dialog.id, reply, reply_id or 0)
            log.info("dialog %s: auto-reply sent, status=inprogress", dialog.id)
            await asyncio.sleep(jittered_delay(campaign))

        # Score interest over the full conversation → hot lead on >= threshold.
        history = await asyncio.to_thread(_dialog_history, dialog.id, HISTORY_LIMIT)
        score = await asyncio.to_thread(ai.score_interest, history)
        hot = await asyncio.to_thread(
            _set_interest, dialog.id, score, campaign.interest_threshold
        )
        if hot:
            log.info("dialog %s: HOT LEAD (interest=%s >= %s) → status=success",
                     dialog.id, score, campaign.interest_threshold)
        else:
            log.info("dialog %s: interest=%s (< %s)",
                     dialog.id, score, campaign.interest_threshold)
    except tgclient.AccountError as err:
        log.error("dialog %s: inbound failed (account %s, reason=%s): %s",
                  dialog.id, dialog.account_id, getattr(err, "reason", "?"), err)
        await _handle_account_error(account.id, err)
    except Exception:  # keep the loop alive across dialogs
        log.exception("dialog %s: unexpected inbound failure", dialog.id)
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass


async def match_loop() -> None:
    """FUNCTIONAL (Ф1). For each active campaign, hand a small batch of unclassified
    leads to the shared classifier (matcher.classify_new_leads): it runs
    ai.classify_and_match per lead and spawns a `new` Dialog for each match. Kept to
    MATCH_LIMIT leads/campaign/tick to stay gentle on LLM cost. The classify logic
    lives in matcher — this loop only schedules it."""
    while True:
        try:
            await _match_pass()
        except Exception:
            log.exception("match_loop tick failed")
        await asyncio.sleep(MATCH_TICK_SECONDS)


async def _match_pass() -> None:
    """One classify sweep across all active campaigns (shared by match_loop and
    the --once mode)."""
    campaigns = await asyncio.to_thread(_fetch_active_campaigns)
    for campaign in campaigns:
        result = await asyncio.to_thread(_classify_new_leads, campaign)
        if result and result.get("classified"):
            log.info(
                "campaign %s: classified=%s matched=%s dialogs_created=%s",
                campaign.id,
                result.get("classified"),
                result.get("matched"),
                result.get("dialogs_created"),
            )


async def parse_loop() -> None:
    """FUNCTIONAL (Ф1). For each active campaign, claim a pending Target
    (pending -> parsing), pick a round-robin active account, connect read-only over
    its proxy, parse the channel/chat into people past Target.last_message_id
    (parser.parse_target), upsert them as Leads (parser.people_to_leads), then flip
    the Target to done (or error). Ported scheduling intent from
    leads42 parser.service.ts (scheduleParsing -> processEntity -> account pick)."""
    while True:
        try:
            await _parse_pass()
        except Exception:
            log.exception("parse_loop tick failed")
        await asyncio.sleep(PARSE_TICK_SECONDS)


async def _parse_pass() -> None:
    """One parse sweep across all active campaigns (shared by parse_loop and the
    --once mode)."""
    campaigns = await asyncio.to_thread(_fetch_active_campaigns)
    for campaign in campaigns:
        targets = await asyncio.to_thread(_fetch_pending_targets, campaign.id)
        for pending in targets:
            target = await asyncio.to_thread(_claim_target, pending.id)
            if target is None:  # lost the race to another tick
                continue
            await _parse_one_target(campaign, target)


async def _parse_one_target(campaign: Campaign, target: Target) -> None:
    """One Target's parse flow. Read-only: no messages are sent here."""
    account = await asyncio.to_thread(_pick_parse_account, target.id)
    if account is None:
        # No account to parse with — return the Target to pending so a later tick
        # retries once an account exists (leads42 just warns and defers).
        await asyncio.to_thread(_finish_target, target.id, 0, TargetStatus.pending)
        log.warning("no active account to parse target %s (campaign %s), deferring",
                    target.id, campaign.id)
        return

    client = None
    try:
        proxy = await asyncio.to_thread(_load_proxy, account.proxy_id)
        client = await tgclient.connect(account, proxy)
        people, last_message_id = await parser.parse_target(client, target, PARSE_LIMIT)
        new_leads = await asyncio.to_thread(
            _people_to_leads, campaign.id, target.id, people
        )
        await asyncio.to_thread(_finish_target, target.id, last_message_id, TargetStatus.done)
        log.info(
            "target %s parsed via account %s: %s people, %s new leads (last_id=%s)",
            target.id, account.id, len(people), new_leads, last_message_id,
        )
    except tgclient.AccountError as err:
        log.error("target %s: parse failed (account %s): %s", target.id, account.id, err)
        await asyncio.to_thread(_finish_target, target.id, 0, TargetStatus.error)
    except Exception as err:  # noqa: BLE001 — mark error, keep the daemon alive
        log.exception("target %s: unexpected parse failure", target.id)
        await asyncio.to_thread(_finish_target, target.id, 0, TargetStatus.error)
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass


# --- Ф3 warmup sync DB units -------------------------------------------------
def _fetch_warmup_accounts(limit: int = WARMUP_ACCOUNTS_PER_TICK) -> list[Account]:
    """Accounts still being warmed: active, phase in (cold, warming), not on
    cooldown. Coldest (fewest actions) first so laggards catch up."""
    with get_session() as s:
        stmt = (
            select(Account)
            .where(
                Account.is_active == True,  # noqa: E712
                Account.warmup_phase.in_(_WARMUP_PHASES),  # type: ignore[attr-defined]
            )
            .order_by(Account.warmup_actions)
            .limit(limit)
        )
        return [a for a in s.exec(stmt).all() if not account_on_cooldown(a)]


def _bump_warmup(account_id: int) -> tuple[int, WarmupPhase]:
    """Record one completed warmup action: increment warmup_actions, move a cold
    account to `warming`, and promote to `ready` once it clears ACTIONS_TO_READY.
    Returns (new_count, new_phase)."""
    with get_session() as s:
        account = s.get(Account, account_id)
        if not account:
            return (0, WarmupPhase.cold)
        account.warmup_actions += 1
        if account.warmup_actions >= warmup.ACTIONS_TO_READY:
            account.warmup_phase = WarmupPhase.ready
        else:
            account.warmup_phase = WarmupPhase.warming
        s.add(account)
        s.commit()
        return (account.warmup_actions, account.warmup_phase)


async def warmup_loop() -> None:
    """FUNCTIONAL (Ф3). Walk cold/warming accounts through paced, human-looking
    warmup actions until phase == ready. Each tick: take accounts still warming
    (active, not on cooldown), and for each connect over its proxy, pick a
    weighted PASSIVE action (warmup.pick_action) and run it (warmup.do_action)
    against the neutral default channels — join/view/react, NEVER a message — then
    bump warmup_actions (cold→warming, →ready past ACTIONS_TO_READY).
    One action per account per tick; a jittered sleep by the intensity delay keeps
    it human. A classified AccountError cools the account down (or deactivates a
    dead/banned one). Ported from warmup-execution.service.ts processWarmupAccount."""
    while True:
        try:
            await _warmup_pass()
        except Exception:  # keep the daemon alive across ticks
            log.exception("warmup_loop tick failed")
        await asyncio.sleep(WARMUP_TICK_SECONDS)


async def _warmup_pass() -> None:
    """One warmup sweep over every account still being warmed."""
    cfg = warmup.get_intensity(WARMUP_INTENSITY)
    accounts = await asyncio.to_thread(_fetch_warmup_accounts)
    if not accounts:
        return
    for account in accounts:
        await _warmup_one_account(account, cfg)
        # Jittered human pause between accounts, paced by the intensity delay.
        await asyncio.sleep(float(random.randint(cfg["delay_min"], cfg["delay_max"])))


async def _warmup_one_account(account: Account, cfg: dict) -> None:
    """One account's single warmup action for this tick."""
    action = warmup.pick_action(account.warmup_actions, account.id or 0, WARMUP_INTENSITY)
    client = None
    try:
        proxy = await asyncio.to_thread(_load_proxy, account.proxy_id)
        client = await tgclient.connect(account, proxy)
        # 🔴 warmup is PASSIVE: self_check / channel_join / channel_view / react only.
        # It NEVER sends a message (no internal DM, no self-ping) — initiating a DM is
        # the fastest way a fresh account gets flagged.
        detail = await warmup.do_action(
            client,
            action,
            channels=warmup.DEFAULT_WARMUP_CHANNELS,
        )
        count, phase = await asyncio.to_thread(_bump_warmup, account.id)
        log.info(
            "warmup account %s: %s (%s) actions=%s/%s phase=%s",
            account.id, action, detail, count, warmup.ACTIONS_TO_READY, phase.value,
        )
        if phase == WarmupPhase.ready:
            log.info("warmup account %s: READY (>= %s actions)",
                     account.id, warmup.ACTIONS_TO_READY)
    except tgclient.AccountError as err:
        log.error("warmup account %s: action %s failed (reason=%s): %s",
                  account.id, action, getattr(err, "reason", "?"), err)
        await _handle_account_error(account.id, err)
    except Exception:  # keep the loop alive across accounts
        log.exception("warmup account %s: unexpected failure", account.id)
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass


async def reset_daily_counters_at_midnight() -> None:
    """Reset every account's dialogs_started_today at local midnight so the
    per-day send cap (max_new_per_account_per_day) refills each day."""
    while True:
        now_local = datetime.now()
        tomorrow = (now_local + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        await asyncio.sleep(max((tomorrow - now_local).total_seconds(), 1.0))
        try:
            count = await asyncio.to_thread(_reset_all_daily_counters)
            log.info("reset daily counters for %s accounts", count)
        except Exception:
            log.exception("reset_daily_counters failed")


# --- entrypoint --------------------------------------------------------------
async def run_once() -> None:
    """One controlled pass of parse → match → mailing → inbound, each once and
    sequentially, for testing without the forever-daemon (CONTRACT Ф2 --once)."""
    log.info("tgengine --once pass starting (skip_warmup=%s)", SKIP_WARMUP_GATE)
    await _parse_pass()
    await _match_pass()
    await _mailing_pass()
    await _inbound_pass()
    log.info("tgengine --once pass complete")


async def main() -> None:
    await asyncio.to_thread(init_db)
    if "--once" in sys.argv[1:]:
        await run_once()
        return
    log.info("tgengine daemon starting (skip_warmup=%s)", SKIP_WARMUP_GATE)
    await asyncio.gather(
        mailing_loop(),
        inbound_loop(),
        match_loop(),
        parse_loop(),
        warmup_loop(),
        reset_daily_counters_at_midnight(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("tgengine daemon stopped")
