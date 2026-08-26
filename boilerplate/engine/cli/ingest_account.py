#!/usr/bin/env python3
"""
cli/ingest_account.py — one-shot CLI to register a bought Telegram account.

Given a Telethon StringSession (from the MCP `account_session`), its egress proxy,
and country, this:
  1. upserts a `Proxy` row (deduped by kind/host/port/username),
  2. creates an `Account` row (session/app/proxy/country) — identity is immutable,
  3. `connect()`s through the proxy to VERIFY the session is authorized,
  4. backfills `tg_id/username/phone` from `whoami`,
  5. prints a compact JSON result: {ok, account_id, authorized, me}.

The session string and any proxy password are secrets — they are NEVER printed.

Usage:
  python cli/ingest_account.py --session <telethon-string> \
      --proxy socks5://user:pass@host:1080 --country US \
      --app-id 2040 --app-hash <hash> --db ./tgengine.db
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from urllib.parse import unquote, urlparse

# Allow running as a plain script (python cli/ingest_account.py) by putting the
# package root (parent of this file's dir) on sys.path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# db.py binds its SQLite engine to TGENGINE_DB at import time, so honor --db BEFORE
# importing the package (argparse still validates below).
for _i, _a in enumerate(sys.argv[1:]):
    if _a == "--db" and _i + 2 <= len(sys.argv[1:]):
        os.environ["TGENGINE_DB"] = sys.argv[1:][_i + 1]
    elif _a.startswith("--db="):
        os.environ["TGENGINE_DB"] = _a[len("--db="):]

import json  # noqa: E402
from sqlmodel import select  # noqa: E402

from tgengine import db  # noqa: E402
from tgengine.db import Account, Proxy  # noqa: E402
from tgengine import tgclient  # noqa: E402
from tgengine import fingerprints  # noqa: E402


def parse_proxy_url(url: str | None) -> dict | None:
    """Parse `socks5://user:pass@host:port` into Proxy field kwargs, or None."""
    if not url:
        return None
    p = urlparse(url)
    if not p.hostname or not p.port:
        raise ValueError(f"invalid proxy url (need host:port): {url!r}")
    kind = (p.scheme or "socks5").lower()
    return {
        "kind": kind,
        "host": p.hostname,
        "port": int(p.port),
        "username": unquote(p.username) if p.username else None,
        "password": unquote(p.password) if p.password else None,
    }


def upsert_proxy(session, fields: dict | None, country: str | None) -> Proxy | None:
    """Find an existing matching proxy or create one. Returns the persisted row."""
    if fields is None:
        return None
    stmt = select(Proxy).where(
        Proxy.kind == fields["kind"],
        Proxy.host == fields["host"],
        Proxy.port == fields["port"],
        Proxy.username == fields["username"],
    )
    existing = session.exec(stmt).first()
    if existing:
        # keep country/password fresh without changing identity
        if country and existing.country != country:
            existing.country = country
        if fields.get("password") and existing.password != fields["password"]:
            existing.password = fields["password"]
        session.add(existing)
        session.commit()
        session.refresh(existing)
        return existing
    proxy = Proxy(country=country, **fields)
    session.add(proxy)
    session.commit()
    session.refresh(proxy)
    return proxy


async def run(args) -> int:
    if args.db:
        os.environ["TGENGINE_DB"] = args.db
    db.init_db()

    proxy_fields = parse_proxy_url(args.proxy)

    with db.get_session() as session:
        proxy = upsert_proxy(session, proxy_fields, args.country)

        account = Account(
            session=args.session,
            app_id=args.app_id,
            app_hash=args.app_hash,
            country=args.country,
            proxy_id=proxy.id if proxy else None,
        )
        session.add(account)
        session.commit()
        session.refresh(account)
        account_id = account.id

        # Assign a STABLE desktop fingerprint (coherent with app 2040 = Telegram
        # Desktop), seeded by the account id so it never rotates. Set BEFORE connect
        # so our very first session touch already carries the real device metadata.
        prefer = {"US": "en", "RU": "ru"}.get((args.country or "").upper())
        fp = fingerprints.pick_fingerprint(account_id, prefer_lang=prefer)
        account.device = json.dumps(fp)
        session.add(account)
        session.commit()
        session.refresh(account)

        authorized = False
        me: dict | None = None
        error: str | None = None
        client = None
        try:
            client = await tgclient.connect(account, proxy)
            authorized = True
            me = await tgclient.whoami(client)
            account.tg_id = me.get("id")
            account.username = me.get("username")
            account.phone = me.get("phone")
            session.add(account)
            session.commit()
            session.refresh(account)
        except Exception as exc:  # noqa: BLE001 — report, don't leak secrets
            error = f"{type(exc).__name__}: {exc}"
            account.is_active = False
            account.deactivated_reason = error
            session.add(account)
            session.commit()
        finally:
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:  # noqa: BLE001
                    pass

    result = {
        "ok": authorized,
        "account_id": account_id,
        "authorized": authorized,
        "me": me,
    }
    if error:
        result["error"] = error
    # me/error never contain the session or proxy password.
    print(json.dumps(result))
    return 0 if authorized else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Register a bought Telegram account and verify its session is authorized."
    )
    parser.add_argument("--session", required=True,
                        help="Telethon StringSession (secret — never printed)")
    parser.add_argument("--proxy", default=None,
                        help="socks5://user:pass@host:port (optional)")
    parser.add_argument("--country", default=None, help="ISO country code, e.g. US")
    parser.add_argument("--app-id", type=int, default=2040, dest="app_id",
                        help="Telegram api_id (default 2040)")
    parser.add_argument("--app-hash", default="b18441a1ff607e10a989891a5462e627",
                        dest="app_hash", help="Telegram api_hash")
    parser.add_argument("--db", default=None,
                        help="SQLite path (sets TGENGINE_DB for this run)")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
