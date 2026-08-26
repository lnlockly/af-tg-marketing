"""
db.py — the SQLite data layer AND the control-plane. This is the shared contract
every other module imports; the agent/MCP drive the engine by writing rows here and
read results back — there is no HTTP API.

State lives in ONE SQLite file (SQLModel/SQLAlchemy), on the pod's overlay-persisted
volume, so it survives restarts. The asyncio daemon (engine.py) scans due work by
status + *_at timestamp columns — no Redis, no external queue.

Ported model shape from leads42 (prisma/schema.prisma) but trimmed to the v1 core
loop: ingest → warmup → parse → match → DM → AI reply → score → hot lead.
"""
from __future__ import annotations
import os
from datetime import datetime
from enum import Enum
from typing import Optional

from sqlmodel import SQLModel, Field, create_engine, Session

DB_PATH = os.environ.get("TGENGINE_DB", os.path.join(os.environ.get("TGENGINE_HOME", "."), "tgengine.db"))
_engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})


def init_db() -> None:
    SQLModel.metadata.create_all(_engine)


def get_session() -> Session:
    """Sync SQLModel session. CLIs use it directly; the async daemon wraps calls in
    asyncio.to_thread (SQLite writes are sub-ms at this scale)."""
    return Session(_engine)


def now() -> datetime:
    return datetime.utcnow()


# --- enums (mirror leads42 statuses, trimmed) --------------------------------
class DialogStatus(str, Enum):
    new = "new"            # matched, not yet messaged
    sent = "sent"          # first message sent, awaiting reply
    answered = "answered"  # lead replied, needs handling
    inprogress = "inprogress"
    success = "success"    # interest score >= threshold → HOT LEAD
    closed = "closed"      # dead / errored / opted out


class CampaignStatus(str, Enum):
    draft = "draft"
    active = "active"
    paused = "paused"


class TargetStatus(str, Enum):
    pending = "pending"
    parsing = "parsing"
    done = "done"
    error = "error"


class MessageFrom(str, Enum):
    account = "account"    # outbound (our userbot)
    user = "user"          # inbound (the lead)


class WarmupPhase(str, Enum):
    cold = "cold"          # freshly ingested, not warmed
    warming = "warming"
    ready = "ready"        # warm enough to run campaigns


class AccountStatus(str, Enum):
    """Health of a userbot account — the account marks ITSELF via health checks."""
    active = "active"          # healthy, usable
    spamblock = "spamblock"    # @SpamBot limited (see spamblock_until)
    cooldown = "cooldown"      # temporary flood/proxy cooldown (see cooldown_until)
    terminated = "terminated"  # session revoked / not authorized anymore
    banned = "banned"          # account banned/deactivated by Telegram
    dead = "dead"              # session invalid (bad auth_key)
    unknown = "unknown"        # not checked yet


# --- accounts / proxies ------------------------------------------------------
class Proxy(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    kind: str = "socks5"
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None
    country: Optional[str] = None          # must equal the account's country
    created_at: datetime = Field(default_factory=now)


class Account(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    # identity — IMMUTABLE once set (leads42 rule): session is a Telethon StringSession
    session: str                            # from MCP account_session (native Telethon)
    app_id: int = 2040
    app_hash: str = "b18441a1ff607e10a989891a5462e627"
    device: Optional[str] = None            # JSON desktop fingerprint (coherent w/ app 2040); IMMUTABLE
    dc_id: Optional[int] = None
    tg_id: Optional[int] = None
    phone: Optional[str] = None
    country: Optional[str] = None
    proxy_id: Optional[int] = Field(default=None, foreign_key="proxy.id")
    # wrapping / profile
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    username: Optional[str] = None
    about: Optional[str] = None
    avatar_path: Optional[str] = None       # local file of the set profile photo
    channel_id: Optional[int] = None        # the account's own channel (одёжка: real users have one)
    channel_username: Optional[str] = None
    # organization — the owner labels accounts by purpose/group (one for X, one for Y)
    purpose: Optional[str] = None           # e.g. "marketing" | "bots" | "scout" | "warmup"
    group_name: Optional[str] = None        # a named fleet/group the account belongs to
    tags: Optional[str] = None              # JSON list of free-form labels
    note: Optional[str] = None
    # health / scheduling — the account marks ITSELF via health checks
    status: AccountStatus = AccountStatus.unknown
    spamblock_until: Optional[datetime] = None   # set from @SpamBot; None = no block
    last_health_check: Optional[datetime] = None
    is_active: bool = True
    warmup_phase: WarmupPhase = WarmupPhase.cold
    warmup_actions: int = 0
    cooldown_until: Optional[datetime] = None
    deactivated_reason: Optional[str] = None
    dialogs_started_today: int = 0
    last_send_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=now)


# --- campaigns / targets / leads / dialogs -----------------------------------
class Campaign(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    product: str                            # what we sell
    audience: str                           # who the lead is (match criteria, natural language)
    first_message_prompt: str
    reply_prompt: str
    work_start: int = 9                     # local hour
    work_end: int = 21
    max_new_per_account_per_day: int = 20
    msg_delay_min: int = 40                 # seconds between sends
    msg_delay_max: int = 180
    interest_threshold: int = 8             # score >= → hot lead
    notify_chat_id: Optional[str] = None    # where hot leads are pushed (the agent's chat)
    status: CampaignStatus = CampaignStatus.draft
    created_at: datetime = Field(default_factory=now)


class Target(SQLModel, table=True):
    """A channel/chat to parse for leads."""
    id: Optional[int] = Field(default=None, primary_key=True)
    campaign_id: int = Field(foreign_key="campaign.id")
    kind: str = "channel"                   # channel | chat
    ref: str                                # @username or invite link
    last_message_id: int = 0
    status: TargetStatus = TargetStatus.pending
    created_at: datetime = Field(default_factory=now)


class Lead(SQLModel, table=True):
    """A parsed person, classified + matched to a campaign."""
    id: Optional[int] = Field(default=None, primary_key=True)
    campaign_id: int = Field(foreign_key="campaign.id")
    tg_id: Optional[int] = None
    username: Optional[str] = None
    first_name: Optional[str] = None
    source_target_id: Optional[int] = Field(default=None, foreign_key="target.id")
    context: Optional[str] = None          # the person's blurb (comment text / bio) for the classifier
    category: Optional[str] = None
    matched: bool = False
    classified: bool = False               # match_loop has run on this lead
    created_at: datetime = Field(default_factory=now)


class Dialog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    campaign_id: int = Field(foreign_key="campaign.id")
    lead_id: int = Field(foreign_key="lead.id")
    account_id: Optional[int] = Field(default=None, foreign_key="account.id")
    status: DialogStatus = DialogStatus.new
    first_message: Optional[str] = None     # cached generated text
    approach: Optional[str] = None
    notes: Optional[str] = None
    interest_score: Optional[int] = None
    auto_mode: bool = True                  # AI auto-replies until operator takes over
    last_msg_id: int = 0                    # highest Telegram msg id processed (inbound cursor)
    next_action_at: Optional[datetime] = None
    last_message_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=now)


class Message(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    dialog_id: int = Field(foreign_key="dialog.id")
    sender: MessageFrom
    text: str
    created_at: datetime = Field(default_factory=now)


class ActionLog(SQLModel, table=True):
    """One recorded action performed BY an account, for the audit trail (what each
    account did, when, and whether it worked). Written by every tool that acts."""
    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: Optional[int] = Field(default=None, foreign_key="account.id")
    action: str                             # e.g. bot_created | group_created | scout | first_message | health_check | spamblock | ingest | dress
    detail: Optional[str] = None
    ok: bool = True
    created_at: datetime = Field(default_factory=now)


# --- shared helpers used across tools ----------------------------------------
def log_action(account_id, action: str, detail: str = "", ok: bool = True) -> None:
    """Append an ActionLog row (best-effort; never raises into a caller's flow)."""
    try:
        with get_session() as s:
            s.add(ActionLog(account_id=account_id, action=action,
                            detail=(detail or "")[:400], ok=ok))
            s.commit()
    except Exception:
        pass


if __name__ == "__main__":
    init_db()
    print(f"initialized {DB_PATH}")
