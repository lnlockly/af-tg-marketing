# tg-engine — build contract (Ф0 skeleton)

Lightweight Telegram marketing engine. Telethon + asyncio + SQLite. Per-pod,
agent-driven. **No HTTP/FastAPI** — SQLite (`tgengine/db.py`) is the control-plane;
a few one-shot CLIs do immediate actions. `leads42` (../leads42) is the SPEC to port
logic from (do not copy GramJS/NestJS structure — reimplement light in Telethon).

Every module imports the models from `tgengine/db.py` (already written — do NOT edit
it). Match its names exactly. Sync SQLModel `Session` in CLIs; the async daemon wraps
DB calls in `asyncio.to_thread`.

## Modules to implement (one file each, no cross-writes)

### `tgengine/tgclient.py` — Telethon client + core MTProto ops
Port from leads42 `src/telegram/telegram-client-manager.service.ts` (session/proxy/
client lifecycle) and `src/telegram/telegram.service.ts` (send).
- `def parse_proxy(proxy: Proxy | None) -> tuple | None` — Telethon socks5 tuple.
- `def build_client(account, proxy) -> TelegramClient` — `StringSession(account.session)`,
  `app_id/app_hash`, proxy.
- `async def connect(account, proxy) -> TelegramClient` — connect; raise if not authorized.
- `async def whoami(client) -> dict` — id/username/phone.
- `async def resolve(client, ref: str)` — entity from @username / t.me link / id.
- `async def send_message(client, ref_or_entity, text: str)` — send; return message id.
- Tolerate `BOT_RESPONSE_TIMEOUT`/flood; raise a clear `AccountError` on hard failures.

### `tgengine/ai.py` — LLM via the AgentFlow gateway (OpenAI-compatible)
Port prompt shapes from leads42 `src/message/generator/prompt.service.ts` and
`src/analysis/analysis-*.service.ts`. Env: `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`
(default `gpt-5.5`).
- `def _client() -> OpenAI`
- `def generate_first_message(campaign, lead) -> str`
- `def generate_reply(campaign, dialog, history: list[Message]) -> str`
- `def classify_and_match(campaign, person_blurb: str) -> dict` → `{category, matched: bool}`
- `def score_interest(history: list[Message]) -> int`  (0–10; ≥ threshold = hot)

### `cli/ingest_account.py` — one-shot: register a bought account
Args: `--session <telethon-string>` (from MCP `account_session`), `--proxy socks5://u:p@host:port`,
`--country US`, `--app-id 2040`, `--app-hash …`, `--db <path>`.
Steps: upsert `Proxy` row; create `Account` (session/app/proxy/country); `connect()` to
VERIFY authorized; fill `tg_id/username/phone`; print JSON `{ok, account_id, authorized, me}`.
Reuse `tgclient.connect`. Never print the session/token.

### `cli/send_test.py` — one-shot: prove an account can send
Args: `--account-id N --to @user --text "..." --db <path>`. Load Account+Proxy, connect
via its proxy, `send_message`, print `{ok, message_id}`.

### `tgengine/engine.py` — the asyncio daemon (skeleton)
`async def main()`: `init_db()`, then run these loops concurrently with
`asyncio.gather` (each `while True: tick; await asyncio.sleep(N)`). Wrap all DB in
`asyncio.to_thread`. Log to stderr.
- `mailing_loop` (**make this one FUNCTIONAL for Ф0**): find `Dialog.status=new` whose
  campaign is `active` and it's within work hours; pick an eligible `Account`
  (is_active, warmup_phase=ready OR flag to skip warmup gate in Ф0, under
  `max_new_per_account_per_day`, not on cooldown); generate `first_message` via
  `ai.generate_first_message`; `tgclient.send_message`; set `Dialog.status=sent`,
  `last_message_at`, bump `account.dialogs_started_today`, jittered delay.
- `inbound_loop`, `match_loop`, `parse_loop`, `warmup_loop`: **STUBS** — real loop
  structure + eligibility/work-hour helpers + `# TODO Фаза N` markers. Don't implement
  the full logic yet.
- Helpers: `in_work_hours(campaign)`, `account_eligible(account, campaign)`,
  `reset_daily_counters_at_midnight`.

## Ф1 additions (parser → leads → match → dialog)

Spec source: leads42 `src/telegram-parser/telegramParser.service.ts`, `src/parser/parser.service.ts`,
`src/people/people.service.ts`, `src/analysis/analysis-contacts.service.ts` + `analysis-category.service.ts`.
`db.py` now has `Lead.context` (person blurb for the classifier) and `Lead.classified`.

### `tgengine/parser.py` — read-only MTProto parsing of a Target into people
- `async def parse_target(client, target: Target, limit: int = 200) -> tuple[list[dict], int]`
  Returns `(people, last_message_id)`. Each person dict: `{tg_id, username, first_name, context}`.
  - **channel** ref → `GetFullChannel` to find the linked discussion chat, then iterate its
    messages/comments after `target.last_message_id`; each commenter = a person, their comment
    text accumulates into `context`. Fallback: if no discussion, iterate participants (identity only).
  - **chat** ref → iterate the group's recent messages (senders + text→context) or participants.
  - De-dupe people by tg_id within the call. Tolerate private/невступлённые (resolve/join via
    `resolve`), FLOOD_WAIT (raise AccountError). Light port — no ParsedPost/ParsedMessage tables.
- `def people_to_leads(session, campaign_id, target_id, people: list[dict]) -> int`
  Upsert `Lead` rows (dedupe by (campaign_id, tg_id) — skip existing), return how many new.

### `tgengine/matcher.py` — shared classify→dialog helper (used by engine + parse_now)
- `def classify_new_leads(session, campaign, limit: int = 20) -> dict` — take up to `limit`
  `Lead` rows with `classified=false` for this campaign; for each build a blurb
  (`first_name`/`username`/`context`) → `ai.classify_and_match(campaign, blurb)` → set
  `category`/`matched`/`classified=true`; if matched, create a `Dialog(status=new)` deduped by
  (campaign_id, lead_id). Returns `{classified, matched, dialogs_created}`. Sync SQLModel.

### `tgengine/engine.py` — make `parse_loop` and `match_loop` FUNCTIONAL
- `parse_loop`: for each active campaign, take a `Target` with status=pending (set parsing) →
  pick any active account (read-only; round-robin) → `tgclient.connect` → `parser.parse_target`
  → `parser.people_to_leads` → update `Target.last_message_id`, status=done (error→status=error).
- `match_loop`: for each active campaign call `matcher.classify_new_leads(session, campaign, limit)`
  (throttle ~1/tick, gentle on LLM cost). Do NOT inline the classify logic — call matcher.

### `cli/parse_now.py` — one-shot: parse + classify a target, NO sending
Args: `--campaign N --target <@channel|link> [--account-id N] [--limit 200] --db <path>`.
Creates the `Target` row if missing, runs `parse_target` + `people_to_leads`, then calls
`matcher.classify_new_leads` over the new leads (creating `Dialog(status=new)`), and prints JSON
`{ok, parsed, new_leads, matched, dialogs_created}`. Does **not** send any messages. Pre-scan
`--db` before importing `tgengine.db` (like send_test). This is the Ф1 live-proof tool.

## Ф2 additions (inbound dialog + scoring + hot leads)

Spec source: leads42 `src/dialog/dialog.service.ts` (processDialogs / handleIncomingMessage),
`src/dialog/sender.processor.ts` (sendReplyMessage), `src/analysis/analysis-dialog.service.ts`
(interest score 0-10, threshold 8 → success + notify). `ai.generate_reply` and
`ai.score_interest` already exist.

### `tgengine/engine.py` — make `inbound_loop` FUNCTIONAL + add `--once`
- `inbound_loop`: for each ACTIVE campaign, for `Dialog`s with status in (sent, answered,
  inprogress) and `auto_mode=true`: connect the dialog's account → poll `iter_messages` with
  the lead since the last stored inbound → for each NEW inbound msg save `Message(sender=user)`,
  set status=answered, `last_message_at`. If there is a new inbound and `auto_mode`: build the
  history (last ~50 `Message`), `ai.generate_reply(campaign, dialog, history)` → `tgclient.send_message`
  → save `Message(sender=account)`, status=inprogress, jittered delay. Then `score = ai.score_interest(history)`
  → set `Dialog.interest_score`; if `score >= campaign.interest_threshold` set status=success
  (HOT LEAD — surfaced via cli/hot_leads; do NOT need a bot to notify in v1). Respect work hours.
  Wrap all DB in `asyncio.to_thread`; classify send/flood errors via AccountError (cooldown).
- Add a `--once` mode to `main()`: run ONE pass of parse→match→mailing→inbound (each once,
  sequentially) then exit 0. Used for controlled testing without the forever-daemon. (Default,
  no flag = the normal forever `asyncio.gather` of the loops.)

### `cli/hot_leads.py` — the agent's "show me the leads"
Args: `--campaign N` (optional), `--status success` (default; or new/sent/answered/inprogress/all),
`--limit 50`, `--db <path>`. Prints JSON: a list of `{dialog_id, lead(username/first_name),
account_id, status, interest_score, last_message, last_message_at}`. Read-only. Pre-scan `--db`.

### `cli/dialog_reply.py` — operator takeover ("взять на себя")
Args: `--dialog-id N --text "..." [--stop-auto] --db <path>`. Sets `auto_mode=false` if `--stop-auto`,
connects the dialog's account, sends the text to the lead, saves `Message(sender=account)`,
updates `last_message_at`. Prints `{ok, message_id}`. Pre-scan `--db`.

## Ф3 additions (account dressing "одёжка" + warmup)

VERY IMPORTANT part (owner). Spec source: leads42 `src/telegram/telegram-profile.service.ts`
(profile ops), `src/account/account.service.ts` (aiProfile bio), `src/warmup/warmup.service.ts` +
`warmup-execution.service.ts` + `warmup.constants.ts`. Device fingerprints already done
(tgengine/fingerprints.py + Account.device applied in tgclient.build_client — do NOT touch that).

### Profile "одёжка" — Agent P owns: `tgengine/ai.py` (edit) + `tgengine/profile.py` + `cli/wrap_account.py`
- ai.py ADD `def generate_profile_about(persona: str, lang: str = "ru") -> str` (short, human,
  ≤70 chars bio; port generateProfileAbout intent) and `def generate_display_name(persona: str,
  lang: str = "ru") -> tuple[str, str]` → (first_name, last_name). Keep the existing functions.
- `tgengine/profile.py`:
  - `async def set_name(client, first_name, last_name=None)` → `Api.account.UpdateProfileRequest`.
  - `async def set_about(client, about)` → `Api.account.UpdateProfileRequest(about=...)`.
  - `async def set_username(client, username)` → `Api.account.UpdateUsernameRequest` (tolerate taken).
  - `async def set_photo(client, path)` → `client.upload_file(path)` → `Api.photos.UploadProfilePhotoRequest`.
  - `async def wrap_account(client, *, first_name=None, last_name=None, about=None, username=None,
    photo_path=None) -> dict` — apply whichever are provided; return what changed. Tolerate per-op
    errors (one failing field must not abort the rest). Port from telegram-profile.service.ts.
- `cli/wrap_account.py`: args `--account-id N [--first-name .. --last-name .. --about .. --username ..
  --photo PATH --persona "крипто-трейдер" --ai] --db PATH`. If `--ai` and a field is missing,
  generate name/about via ai.generate_display_name/generate_profile_about from `--persona`. Load
  Account+Proxy, connect, wrap_account, persist first_name/last_name/username/about back onto the
  Account row, print `{ok, changed}`. Pre-scan --db. Never print secrets.

### Warmup — Agent W owns: `tgengine/warmup.py` + `tgengine/engine.py` (warmup_loop only)
- `tgengine/warmup.py` (port warmup.constants.ts + warmup-execution.service.ts):
  - `INTENSITY = {"low":{...},"medium":{...},"high":{...}}` with durationDays, delay_min/max,
    channels_per_account, weights self_check/channel_join/internal (values from warmup.constants.ts).
  - `DEFAULT_WARMUP_CHANNELS = [...]` — ~10 neutral popular PUBLIC channels (e.g. telegram, durov, tginfo…)
    to view/join. `ACTIONS_TO_READY = 25` (promote past this).
  - `def pick_action(actions_count, account_id, intensity="medium") -> str` — weighted selector
    over PASSIVE actions only (self_check|channel_view|channel_join|react). 🔴 Warmup NEVER messages.
  - `async def do_action(client, action, *, channels, peers=None) -> str` — channel_view =
    `iter_messages(channel, limit≈15)`; channel_join = `Api.channels.JoinChannelRequest` (cap handled by
    caller/day); self_check = `get_me`; react = one positive `SendReactionRequest` on a recent post
    (skip if the channel disallows it). `peers` is accepted for back-compat and IGNORED — no DMs.
    Tolerate FLOOD_WAIT → AccountError.
- `tgengine/engine.py` `warmup_loop` (replace the STUB, touch NOTHING else): for accounts with
  `warmup_phase` in (cold, warming) and not on cooldown, in a gentle paced tick: connect via proxy →
  `warmup.pick_action` → `warmup.do_action` (channels=DEFAULT, peers=other warmup accounts for internal)
  → bump `account.warmup_actions`, set phase=warming; when `warmup_actions >= ACTIONS_TO_READY` set
  phase=ready. One action per account per tick; jittered sleep by intensity delay. AccountError→cooldown.
  Add `from tgengine import warmup`. Respect a `TGENGINE_WARMUP_INTENSITY` env (default medium).

## Ф3.1 additions (avatars + own channel + fleet — owner follow-up)

db.py now has `Account.avatar_path`, `Account.channel_id`, `Account.channel_username` (already added).
Real accounts have a photo and often their OWN channel — dress the account like a real person, and
support doing it across a FLEET (several accounts), not just one.

### Agent A — `tgengine/ai.py` (edit) + `tgengine/profile.py` (edit)
- ai.py ADD `def generate_avatar(persona: str, out_path: str) -> str | None`: call the gateway's
  images endpoint `_client().images.generate(model=os.environ.get("LLM_IMAGE_MODEL","gpt-image-2"),
  prompt=<plausible neutral profile-photo prompt built from persona>, size="1024x1024")`, handle both
  `b64_json` and `url` responses (decode/download), write PNG to out_path, return the path (None on
  failure — fail-soft). Keep all existing functions.
- profile.py ADD:
  - `async def create_channel(client, title: str, about: str = "", megagroup=False) -> dict` →
    `telethon.tl.functions.channels.CreateChannelRequest(title=title, about=about, broadcast=not megagroup, megagroup=megagroup)`;
    return `{channel_id, access_hash, username?}` from the resulting channel. Tolerate errors.
  - `async def set_channel_photo(client, channel, path) -> bool` →
    `functions.channels.EditPhotoRequest(channel, photo=InputChatUploadedPhoto(await client.upload_file(path)))`.
  - keep `set_photo`/`wrap_account` — extend `wrap_account` to accept `photo_path` (already there) and
    return the change set.

### Agent B — `cli/wrap_account.py` (edit) + `cli/dress_fleet.py` (new)
- wrap_account.py: add flags `--avatar-ai` (ai.generate_avatar from --persona → set_photo → persist
  avatar_path) and `--with-channel "Title"` (profile.create_channel; if an avatar exists, set channel
  photo too; persist channel_id/channel_username). Keep existing behavior.
- cli/dress_fleet.py (new): batch одёжка across MANY accounts. Args `--persona-pool "a;b;c"` (or a
  default set of investor/founder/freelancer personas), `[--ids 1,2,3 | --all]`, `--avatar-ai`,
  `--with-channel`, `--db`. For each selected active Account: pick/rotate a persona, run the same
  dress flow (name+bio via ai, optional avatar, optional channel), one account at a time with a small
  delay. Print a JSON summary list `[{account_id, first_name, avatar, channel_username, ok}]`.
  Pre-scan --db; never print secrets. (Fleet internal-warmup DMs already work when 2+ accounts exist.)

## Account-manager (multi-account registry: health / spamblock / tags / logs)

The engine is the single registry of ALL userbot accounts. db.py now has: AccountStatus enum
(active/spamblock/cooldown/terminated/banned/dead/unknown); Account.{status, spamblock_until,
last_health_check, purpose, group_name, tags(JSON), note}; ActionLog(account_id, action, detail,
ok, created_at) + `db.log_action(account_id, action, detail, ok)`. The account marks ITSELF via
health checks. The MCP must expose all of this.

### `tgengine/health.py`
- `async def check_spamblock(client) -> dict` → message **@SpamBot** (`/start`), read its reply,
  return `{blocked: bool, until: datetime|None, raw: str}`. Parse the RU/EN reply: "no limits"/
  "никаких ограничений" → blocked False; "limited until <date>"/"ограничен до <date>" → blocked True
  + parse the date into `until` (best-effort; None if unparseable). Tolerant.
- `async def health_check(account, proxy) -> dict` → connect via tgclient; if it raises AccountError
  reason dead→status "dead", terminated/unauthorized→"terminated", banned→"banned", flood/proxy→
  "cooldown"; else run check_spamblock → status "spamblock"(+until) or "active". Return
  `{status, spamblock_until, detail}`. Disconnect cleanly. Import tgclient + db enums only.

### `cli/account_health.py`
Args `(--ids 1,2,3 | --all) --db`. For each selected Account: run `health.health_check`, write back
`status`, `spamblock_until`, `last_health_check=now`, and `is_active = status in (active, cooldown)`;
`db.log_action(id, "health_check", status, ok)`. Print a JSON list `[{account_id, status,
spamblock_until, detail}]`. Pre-scan --db. One at a time (gentle).

### `cli/account_tag.py`
Args `--account-id N [--purpose P --group G --tags "a,b,c" --note "..."] --db`. Set whichever are
given (tags stored as JSON list; empty string clears). `db.log_action(id, "tag", ...)`. Print the
account's org fields. Pre-scan --db.

### `cli/account_logs.py`
Args `[--account-id N] [--action X] [--limit 50] --db`. Read-only: print a JSON list of ActionLog
rows (id, account_id, action, detail, ok, created_at) newest-first, optionally filtered. Pre-scan --db.

### `cli/account_list.py` — ENRICH output
Add to each row: `status`, `spamblock_until`, `purpose`, `group_name`, `tags` (parsed list),
`last_health_check`. Keep existing fields. Still read-only.

### `mcp/server.mjs` — ADD tools + enrich
- `account_health` → account_health.py (args: ids? / all).
- `account_tag` → account_tag.py (account_id, purpose?, group?, tags?, note?).
- `account_logs` → account_logs.py (account_id?, action?, limit?).
- account_list already maps to the enriched CLI (no change needed beyond the CLI).
Keep the existing 12 tools; these make 15. Same runCli pattern, same optArgs mapping.

## Acceptance (Ф0)
- `python -m py_compile` clean on every file.
- `python -c "import tgengine.db, tgengine.tgclient, tgengine.ai, tgengine.engine"` imports.
- `python cli/ingest_account.py --help` and `python cli/send_test.py --help` work.
- (Live proof done separately in the pod: ingest a real bought account → send_test.)
