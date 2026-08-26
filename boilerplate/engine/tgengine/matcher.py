"""
matcher.py — shared classify → dialog helper (used by engine.match_loop and
cli/parse_now.py).

Light port of leads42 src/analysis/analysis-category.service.ts runAnalysis():
that service walks parsed people, classifies them against a campaign's category
criteria via the LLM, and — when the person matches — creates a Dialog(status=new)
and marks the person "used". Here the taxonomy/match decision already lives in
ai.classify_and_match; this module just drives it over unclassified Lead rows and
writes the results back (category / matched / classified) plus the deduped Dialog.

Sync SQLModel Session. Imports ai + db only (no Telethon, no LLM machinery here).
"""
from __future__ import annotations

from sqlmodel import Session, select

from tgengine import ai
from tgengine.db import Campaign, Dialog, DialogStatus, Lead


def _blurb(lead: Lead) -> str:
    """Build the person blurb fed to the classifier from the lead's identity +
    accumulated context (comment text / bio). Mirrors the leads42 analysis input
    (description + messages)."""
    parts: list[str] = []
    first_name = (lead.first_name or "").strip()
    username = (lead.username or "").strip().lstrip("@")
    context = (lead.context or "").strip()
    if first_name:
        parts.append(f"Имя: {first_name}")
    if username:
        parts.append(f"Username: @{username}")
    if context:
        parts.append(f"Сообщения/описание:\n{context}")
    return "\n".join(parts).strip()


def classify_new_leads(session: Session, campaign: Campaign, limit: int = 20) -> dict:
    """Classify up to `limit` unclassified leads for `campaign` and open dialogs.

    For each Lead with classified=False belonging to this campaign:
      - build a blurb and call ai.classify_and_match(campaign, blurb)
      - persist category / matched / classified=True
      - if matched, create a Dialog(status=new) deduped by (campaign_id, lead_id)

    Returns {classified, matched, dialogs_created}.
    """
    leads = list(
        session.exec(
            select(Lead)
            .where(Lead.campaign_id == campaign.id)
            .where(Lead.classified == False)  # noqa: E712 — SQL boolean, not `is`
            .limit(limit)
        ).all()
    )

    classified = 0
    matched = 0
    dialogs_created = 0

    for lead in leads:
        result = ai.classify_and_match(campaign, _blurb(lead))
        lead.category = result.get("category")
        lead.matched = bool(result.get("matched"))
        lead.classified = True
        session.add(lead)
        classified += 1

        if lead.matched:
            matched += 1
            existing = session.exec(
                select(Dialog)
                .where(Dialog.campaign_id == campaign.id)
                .where(Dialog.lead_id == lead.id)
            ).first()
            if existing is None:
                session.add(
                    Dialog(
                        campaign_id=campaign.id,
                        lead_id=lead.id,
                        status=DialogStatus.new,
                    )
                )
                dialogs_created += 1

    session.commit()
    return {
        "classified": classified,
        "matched": matched,
        "dialogs_created": dialogs_created,
    }
