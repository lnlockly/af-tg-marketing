#!/usr/bin/env node
/**
 * leads-ops — the MCP the tg-marketing agent drives its engine with. Thin wrapper:
 * every tool shells the matching one-shot CLI in the engine dir and returns its JSON.
 * The engine's control-plane is SQLite; these tools write/read rows and the daemon
 * (engine_control start) acts on them. No secrets are ever returned (the CLIs already
 * scrub session/token). Model on bot-studio's mcp-server.mjs.
 *
 * Env: TGENGINE_DIR (engine root, default /app/data/tg-engine), TGENGINE_DB
 *      (default $TGENGINE_DIR/tgengine.db), PYTHON (default python3).
 */
import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { CallToolRequestSchema, ListToolsRequestSchema } from '@modelcontextprotocol/sdk/types.js';
import { spawn } from 'node:child_process';
import { join } from 'node:path';

const DIR = process.env.TGENGINE_DIR || '/app/data/tg-engine';
const DB = process.env.TGENGINE_DB || join(DIR, 'tgengine.db');
const PY = process.env.PYTHON || 'python3';

const log = (...a) => console.error('[leads-ops]', ...a);

/** Run `python3 cli/<script> ...args --db DB` in the engine dir; return parsed JSON (or raw text). */
function runCli(script, args = []) {
  return new Promise((resolve) => {
    const full = [join('cli', script), ...args, '--db', DB];
    const child = spawn(PY, full, {
      cwd: DIR,
      env: { ...process.env, TGENGINE_HOME: DIR, TGENGINE_DB: DB },
    });
    let out = '', err = '';
    child.stdout.on('data', (d) => (out += d));
    child.stderr.on('data', (d) => (err += d));
    child.on('close', (code) => {
      let parsed = null;
      const line = out.trim().split('\n').filter(Boolean).pop() || '';
      try { parsed = JSON.parse(out.trim()); } catch { try { parsed = JSON.parse(line); } catch { parsed = null; } }
      resolve({ ok: code === 0, exit: code, json: parsed, raw: out.trim(), stderr: err.trim().split('\n').slice(-4).join('\n') });
    });
    child.on('error', (e) => resolve({ ok: false, exit: -1, json: null, raw: '', stderr: String(e) }));
  });
}

// Flatten {k:v} opts into ["--k","v"] CLI args (booleans → bare flags; arrays → csv).
function optArgs(map, spec) {
  const args = [];
  for (const [key, flag] of Object.entries(spec)) {
    const v = map[key];
    if (v === undefined || v === null || v === '') continue;
    if (v === true) args.push(flag);
    else if (v === false) continue;
    else args.push(flag, Array.isArray(v) ? v.join(',') : String(v));
  }
  return args;
}

// --- tool registry: name → {desc, schema, run} ------------------------------
const S = (props, required = []) => ({ type: 'object', properties: props, required });
const str = (d) => ({ type: 'string', description: d });
const int = (d) => ({ type: 'integer', description: d });
const bool = (d) => ({ type: 'boolean', description: d });

const TOOLS = {
  account_ingest: {
    desc: 'Register a bought Telegram account from its Telethon session (+ optional country-matched proxy). Verifies it authorizes and assigns a stable desktop device fingerprint.',
    schema: S({ session: str('Telethon StringSession (from account_session)'), proxy: str('socks5://user:pass@host:port (optional)'), country: str('ISO country, e.g. US') }, ['session']),
    run: (a) => runCli('ingest_account.py', [...optArgs(a, { session: '--session', proxy: '--proxy', country: '--country' })]),
  },
  account_list: {
    desc: 'List all accounts with their dress/warmup/health state (read-only, no Telegram connection).',
    schema: S({ active_only: bool('only is_active accounts') }),
    run: (a) => runCli('account_list.py', a.active_only ? ['--active'] : ['--all']),
  },
  account_dress: {
    desc: 'Dress one account (одёжка): human name+bio (AI text from persona), an auto @username, SEVERAL photos, and an optional own channel. IMPORTANT: you SOURCE the photos yourself with your NATIVE tools (image_generate for synthetic faces / a web image-search) and pass the ready FILES in `photos` — this tool only APPLIES them. Never use a real identifiable person\'s photos.',
    schema: S({ account_id: int('Account.id'), persona: str('e.g. "частный инвестор, крипта" — drives AI name/bio + the vibe of photos you generate'), ai: bool('AI-generate missing name/bio (text)'), photos: str('photos to set: comma file paths OR a directory — files YOU already generated/downloaded'), with_channel: str('create the account a channel with this title'), first_name: str(''), last_name: str(''), about: str(''), username: str('') }, ['account_id']),
    run: (a) => runCli('wrap_account.py', ['--account-id', String(a.account_id), ...optArgs(a, { persona: '--persona', ai: '--ai', photos: '--photos', with_channel: '--with-channel', first_name: '--first-name', last_name: '--last-name', about: '--about', username: '--username' })]),
  },
  fleet_dress: {
    desc: 'Batch-dress many accounts, rotating personas (name+bio + @username + optional own channel). Photos are optional per-account: pre-drop the files YOU sourced (native tools) into photos_dir/<account_id>/ — the engine only applies them.',
    schema: S({ ids: str('comma ids e.g. 1,2,3 (omit for --all)'), all: bool('dress every active account'), persona_pool: str('semicolon personas "a;b;c"'), photos_dir: str('root dir; each account uses photos_dir/<account_id>/* (photos you pre-sourced)'), with_channel: bool('give each its own channel') }),
    run: (a) => runCli('dress_fleet.py', [...optArgs(a, { ids: '--ids', all: '--all', persona_pool: '--persona-pool', photos_dir: '--photos-dir', with_channel: '--with-channel' })]),
  },
  warmup_control: {
    desc: 'Queue accounts for warmup (--start sets them cold so the daemon warms them) or inspect warmup progress. Warmup is paced over days — do NOT rush accounts into campaigns before ready.',
    schema: S({ ids: str('comma ids (omit for all)'), all: bool(''), action: { type: 'string', enum: ['start', 'status'], description: 'start=queue for warmup; status=inspect' }, intensity: { type: 'string', enum: ['low', 'medium', 'high'] } }, ['action']),
    run: (a) => runCli('warmup_ctl.py', [...(a.ids ? ['--ids', a.ids] : ['--all']), `--${a.action}`, ...optArgs(a, { intensity: '--intensity' })]),
  },
  campaign_create: {
    desc: 'Create a campaign (draft): product, audience, first-message + reply prompts, work hours, per-account daily cap, interest threshold.',
    schema: S({ name: str(''), product: str('what we sell'), audience: str('who the lead is'), first_message_prompt: str(''), reply_prompt: str(''), work_start: int('local hour, default 9'), work_end: int('default 21'), max_per_account_per_day: int('default 20'), interest_threshold: int('hot-lead score, default 8'), notify_chat_id: str('where to surface hot leads') }, ['name', 'product', 'audience', 'first_message_prompt', 'reply_prompt']),
    run: (a) => runCli('campaign_create.py', optArgs(a, { name: '--name', product: '--product', audience: '--audience', first_message_prompt: '--first-message-prompt', reply_prompt: '--reply-prompt', work_start: '--work-start', work_end: '--work-end', max_per_account_per_day: '--max-per-account-per-day', interest_threshold: '--interest-threshold', notify_chat_id: '--notify-chat-id' })),
  },
  campaign_add_targets: {
    desc: 'Add parse targets (channels/chats) to a campaign. NOTE: public MEGAGROUPS yield people; broadcast channels usually do not (no linked discussion).',
    schema: S({ campaign: int('campaign id'), targets: str('comma refs "@a,@b,https://t.me/c"'), kind: { type: 'string', enum: ['channel', 'chat'] } }, ['campaign', 'targets']),
    run: (a) => runCli('add_targets.py', ['--campaign', String(a.campaign), '--targets', a.targets, ...optArgs(a, { kind: '--kind' })]),
  },
  campaign_control: {
    desc: 'Start / pause a campaign, or get its status + lead/dialog counts.',
    schema: S({ campaign: int(''), action: { type: 'string', enum: ['start', 'pause', 'status'] } }, ['campaign', 'action']),
    run: (a) => runCli('campaign_ctl.py', ['--campaign', String(a.campaign), `--${a.action}`]),
  },
  parse_now: {
    desc: 'Parse ONE target now (people → leads → AI classify+match → new dialogs), without sending anything. Good for a quick check of a source.',
    schema: S({ campaign: int(''), target: str('@channel / group / link'), account_id: int('parser account (optional)'), limit: int('default 200') }, ['campaign', 'target']),
    run: (a) => runCli('parse_now.py', ['--campaign', String(a.campaign), '--target', a.target, ...optArgs(a, { account_id: '--account-id', limit: '--limit' })]),
  },
  hot_leads: {
    desc: "Show leads/dialogs (default the HOT ones, interest >= threshold). The agent's inbox.",
    schema: S({ campaign: int('optional'), status: { type: 'string', enum: ['success', 'new', 'sent', 'answered', 'inprogress', 'closed', 'all'] }, limit: int('default 50') }),
    run: (a) => runCli('hot_leads.py', optArgs(a, { campaign: '--campaign', status: '--status', limit: '--limit' })),
  },
  dialog_reply: {
    desc: 'Operator takeover: send a manual message into a dialog (optionally stop the AI auto-reply).',
    schema: S({ dialog_id: int(''), text: str(''), stop_auto: bool('turn off AI auto-reply for this dialog') }, ['dialog_id', 'text']),
    run: (a) => runCli('dialog_reply.py', ['--dialog-id', String(a.dialog_id), '--text', a.text, ...optArgs(a, { stop_auto: '--stop-auto' })]),
  },
  engine_control: {
    desc: 'Start / stop / status the background daemon that runs parse, warmup, mailing and dialog loops.',
    schema: S({ action: { type: 'string', enum: ['start', 'stop', 'status'] } }, ['action']),
    run: (a) => runCli('engine_ctl.py', [`--${a.action}`]),
  },
  account_health: {
    desc: 'Health-check accounts (connect + @SpamBot probe): marks each active/spamblock/cooldown/terminated/banned/dead and records last_health_check. Pass ids OR all. One at a time (gentle).',
    schema: S({ ids: str('comma ids e.g. 1,2,3'), all: bool('check every account') }),
    run: (a) => runCli('account_health.py', [...optArgs(a, { ids: '--ids', all: '--all' })]),
  },
  account_tag: {
    desc: 'Organize an account: set its purpose, group, tags (comma list; empty clears) and a free note. Read/edit the org labels the owner uses to sort the fleet.',
    schema: S({ account_id: int('Account.id'), purpose: str('e.g. marketing | bots | scout | warmup'), group: str('named fleet/group'), tags: str('comma labels e.g. "vip,us,aged"'), note: str('free note') }, ['account_id']),
    run: (a) => runCli('account_tag.py', ['--account-id', String(a.account_id), ...optArgs(a, { purpose: '--purpose', group: '--group', tags: '--tags', note: '--note' })]),
  },
  account_logs: {
    desc: 'Read the action audit trail (what each account did, when, ok/fail), newest-first. Optionally filter by account_id and/or action. Read-only.',
    schema: S({ account_id: int('filter to one Account.id'), action: str('filter by action e.g. health_check | tag | ingest'), limit: int('default 50') }),
    run: (a) => runCli('account_logs.py', [...optArgs(a, { account_id: '--account-id', action: '--action', limit: '--limit' })]),
  },
};

// --- MCP server -------------------------------------------------------------
const server = new Server({ name: 'leads-ops', version: '0.1.0' }, { capabilities: { tools: {} } });

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: Object.entries(TOOLS).map(([name, t]) => ({ name, description: t.desc, inputSchema: t.schema })),
}));

// NOTE: we deliberately DO NOT set `isError` on the result — some MCP clients
// (Hermes) choke on it. Errors are conveyed inside the JSON text payload (ok:false),
// which the agent reads. Return shape is just { content:[{type:'text',...}] }.
server.setRequestHandler(CallToolRequestSchema, async (req) => {
  const t = TOOLS[req.params.name];
  if (!t) return { content: [{ type: 'text', text: JSON.stringify({ ok: false, error: `unknown tool ${req.params.name}` }) }] };
  try {
    const res = await t.run(req.params.arguments || {});
    const payload = res.json ?? { ok: res.ok, raw: res.raw, stderr: res.stderr };
    return { content: [{ type: 'text', text: JSON.stringify(payload) }] };
  } catch (e) {
    return { content: [{ type: 'text', text: JSON.stringify({ ok: false, error: String(e) }) }] };
  }
});

const transport = new StdioServerTransport();
await server.connect(transport);
log(`leads-ops MCP up — ${Object.keys(TOOLS).length} tools, engine dir ${DIR}`);
