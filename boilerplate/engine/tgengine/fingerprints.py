"""
fingerprints.py — realistic DEVICE fingerprints for MTProto initConnection.

Ported from leads42 config/fingerprints.json (the desktop subset). Our bought
accounts are app_id 2040 = **Telegram Desktop**, so the device MUST be a desktop
(Windows/macOS laptop, app_version 6.6.2) — a phone device under a desktop app_id
is an obvious tell. leads42's rule: device is IDENTITY — assign once, never rotate.
So we pick a STABLE fingerprint per account (seeded by its own id/tg_id) and keep it.

Each entry maps to Telethon's TelegramClient kwargs:
  device_model, system_version, app_version, lang_code, system_lang_code
"""
from __future__ import annotations

# Desktop fingerprints (coherent with app_id 2040 / Telegram Desktop 6.6.2).
DESKTOP_FINGERPRINTS = [
    {"device_model": "ThinkPad X1 Carbon Gen 13 Aura Edition", "system_version": "Windows 11 25H2", "app_version": "6.6.2", "lang_code": "en", "system_lang_code": "en"},
    {"device_model": "ThinkPad T14 Gen 6", "system_version": "Windows 11 25H2", "app_version": "6.6.2", "lang_code": "ru", "system_lang_code": "ru"},
    {"device_model": "Dell Pro 14 Premium", "system_version": "Windows 11 25H2", "app_version": "6.6.2", "lang_code": "en", "system_lang_code": "en"},
    {"device_model": "HP EliteBook Ultra G1i 14", "system_version": "Windows 11 25H2", "app_version": "6.6.2", "lang_code": "en", "system_lang_code": "en"},
    {"device_model": "ASUS Zenbook A14 UX3407", "system_version": "Windows 11 25H2", "app_version": "6.6.2", "lang_code": "en", "system_lang_code": "en"},
    {"device_model": "ASUS Zenbook S14 UX5406", "system_version": "Windows 11 25H2", "app_version": "6.6.2", "lang_code": "ru", "system_lang_code": "ru"},
    {"device_model": "Framework Laptop 13 AMD Ryzen AI 300", "system_version": "Windows 11 25H2", "app_version": "6.6.2", "lang_code": "en", "system_lang_code": "en"},
    {"device_model": "MacBook Air M5", "system_version": "macOS Tahoe", "app_version": "6.6.2", "lang_code": "en", "system_lang_code": "en"},
    {"device_model": "MacBook Pro 14 M5", "system_version": "macOS Tahoe", "app_version": "6.6.2", "lang_code": "en", "system_lang_code": "en"},
    {"device_model": "MacBook Pro 16 M4 Max", "system_version": "macOS 15 Sequoia", "app_version": "6.6.2", "lang_code": "ru", "system_lang_code": "ru"},
]

# Telethon kwargs its TelegramClient accepts for the device metadata.
DEVICE_KEYS = ("device_model", "system_version", "app_version", "lang_code", "system_lang_code")


def pick_fingerprint(seed: int, prefer_lang: str | None = None) -> dict:
    """Deterministically pick a STABLE desktop fingerprint for an account.

    `seed` = the account's own id/tg_id so the same account always gets the same
    device (never rotate — that would flip the displayed session device). If
    `prefer_lang` is given ('ru'/'en'), bias toward that language pool.
    """
    pool = DESKTOP_FINGERPRINTS
    if prefer_lang:
        matching = [f for f in pool if f["lang_code"] == prefer_lang]
        if matching:
            pool = matching
    return dict(pool[abs(int(seed)) % len(pool)])


def as_client_kwargs(device: dict | None) -> dict:
    """Filter a stored device dict down to the keys Telethon's TelegramClient takes."""
    if not device:
        return {}
    return {k: device[k] for k in DEVICE_KEYS if device.get(k)}
