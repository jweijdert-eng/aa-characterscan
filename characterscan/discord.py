"""
Optionele Discord-webhook-notificaties.

Alleen actief als settings.CHARACTERSCAN_DISCORD_WEBHOOK is gezet. Wordt gebruikt
voor nieuwe aanmeldingen en (via de monitoring-taak) voor nieuwe rode vlaggen.
"""

import logging

import requests

from django.conf import settings

logger = logging.getLogger(__name__)

_COLORS = {"bad": 0xE05555, "warn": 0xF0C040, "ok": 0x3ECF6E}


def _db_settings():
    try:
        from .models import Settings
        return Settings.load()
    except Exception:  # noqa: BLE001 — DB nog niet gemigreerd o.i.d.
        return None


def webhook_url():
    """Webhook-URL: eerst uit het admin-paneel, anders uit settings.py."""
    s = _db_settings()
    if s and s.discord_webhook:
        return s.discord_webhook
    return getattr(settings, "CHARACTERSCAN_DISCORD_WEBHOOK", None)


def _post(embed):
    url = webhook_url()
    if not url:
        return False
    try:
        r = requests.post(url, json={"embeds": [embed]}, timeout=8)
        return r.ok
    except Exception as e:  # noqa: BLE001
        logger.warning("Character Scan Discord-notificatie faalde: %s", e)
        return False


def _embed(eve_character, title_prefix="", verdict=None, score=None, flags=None, note=None):
    color = _COLORS.get((verdict or {}).get("level"), 0x00B4D8)
    fields = []
    if score is not None:
        fields.append({"name": "Risico", "value": f"{score}/100", "inline": True})
    if verdict:
        fields.append({"name": "Oordeel", "value": f"{verdict.get('icon', '')} {verdict.get('label', '?')}",
                       "inline": True})
    if flags:
        sig = [f["label"] for f in flags if f.get("level") in ("bad", "warn")][:8]
        if sig:
            fields.append({"name": "Signalen", "value": ", ".join(sig)})
    if note:
        fields.append({"name": "Melding", "value": note})
    footer = eve_character.corporation_name or ""
    if eve_character.alliance_name:
        footer += f" · {eve_character.alliance_name}"
    return {
        "title": f"{title_prefix}{eve_character.character_name}".strip(),
        "url": f"https://zkillboard.com/character/{eve_character.character_id}/",
        "color": color,
        "thumbnail": {"url": f"https://images.evetech.net/characters/"
                             f"{eve_character.character_id}/portrait?size=128"},
        "fields": fields,
        "footer": {"text": footer or "Character Scan"},
    }


def notify_test():
    """Verstuur een testbericht naar de ingestelde webhook. → True bij succes."""
    if not webhook_url():
        return False
    return _post({
        "title": "✅ Character Scan — testbericht",
        "description": "De Discord-webhook werkt! Je ontvangt hier voortaan nieuwe "
                       "aanmeldingen en rode-vlag-meldingen.",
        "color": 0x00B4D8,
        "footer": {"text": "Character Scan"},
    })


def notify_application(eve_character, n_chars=1):
    """Nieuwe aanmelding gemeld op Discord (indien ingeschakeld)."""
    s = _db_settings()
    if s and not s.notify_new_application:
        return False
    prefix = "📥 Nieuwe aanmelding: "
    note = f"Account met {n_chars} character(s)" if n_chars > 1 else None
    return _post(_embed(eve_character, prefix, note=note))


def notify_flags(eve_character, verdict, score, flags, note=None):
    """Rode-vlag-/monitoring-melding op Discord (indien ingeschakeld)."""
    s = _db_settings()
    if s and not s.notify_alerts:
        return False
    return _post(_embed(eve_character, "🚩 ", verdict=verdict, score=score, flags=flags, note=note))


def notify_waiting(items, hours):
    """Herinnering op Discord aan aanmeldingen die te lang wachten.

    `items` = lijst van (character_name, character_id, wachttijd-label). → True bij succes.
    """
    if not items:
        return False
    s = _db_settings()
    if s and not s.notify_alerts:
        return False
    lines = [f"• [{name}](https://zkillboard.com/character/{cid}/) — {waited}"
             for name, cid, waited in items[:25]]
    if len(items) > 25:
        lines.append(f"… en nog {len(items) - 25}")
    return _post({
        "title": f"⏰ {len(items)} aanmelding(en) wachten al langer dan {hours} uur",
        "description": "\n".join(lines),
        "color": 0xF0C040,
        "footer": {"text": "Character Scan"},
    })
