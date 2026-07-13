"""
Weergave-laag: leest het canonieke data-dict uit esi_fetch en markeert vijanden.
Character Scan is hiermee niet meer afhankelijk van Member Audit-modellen.
"""

from .esi_fetch import get_profile
from .vetting import _parse_date, _scan_text, mail_is_friendly, trusted_domains


def basic_stats(eve_character):
    """Compacte stats voor de recruiter-lijst / aanmeld-pagina."""
    p = get_profile(eve_character)
    return {
        "registered": p.get("ok", False),
        "source": p.get("source"),
        "corp_name": p.get("corp_name"),
        "alliance_name": p.get("alliance_name") or "",
        "corp_id": p.get("corp_id"),
        "alliance_id": p.get("alliance_id"),
        "sec": p.get("sec"),
        "age_years": p.get("age_years"),
        "wallet": p.get("wallet"),
        "total_sp": p.get("total_sp"),
        "owner_main": p.get("owner_main"),
        "is_alt": p.get("is_alt", False),
    }


def full_profile(eve_character, enemy=None):
    """Alle secties voor de detailpagina; markeert vijanden als `enemy` is meegegeven."""
    enemy = enemy or {}
    p = get_profile(eve_character)

    for h in p.get("corp_history", []):
        h["is_enemy"] = h.get("corp_id") in enemy or (h.get("alliance_id") and h["alliance_id"] in enemy)
    for c in p.get("contacts", []):
        c["is_enemy"] = c.get("id") in enemy
    for c in p.get("contracts", []):
        c["issuer_enemy"] = (c.get("issuer_id") in enemy) or (c.get("issuer_corp_id") in enemy)
        c["assignee_enemy"] = c.get("assignee_id") in enemy
        c["acceptor_enemy"] = c.get("acceptor_id") in enemy
        c["is_enemy"] = c["issuer_enemy"] or c["assignee_enemy"] or c["acceptor_enemy"]

    standing_by_id = {c.get("id"): c.get("standing") for c in p.get("contacts", [])}
    trusted = trusted_domains()
    mails = p.get("mails", [])
    for m in mails:
        parties = [m.get("from_id")] + (m.get("recipient_ids") or [])
        m["is_enemy"] = any(pid in enemy for pid in parties if pid)
        m["date_parsed"] = _parse_date(m.get("date"))
        # Eigen standing van de recruit t.o.v. de mail-correspondenten (afzender eerst)
        m["from_standing"] = standing_by_id.get(m.get("from_id"))
        m["standing"] = m["from_standing"] if m["from_standing"] is not None else next(
            (standing_by_id.get(pid) for pid in parties if pid in standing_by_id), None)
        # Van eigen corp/alliance of blauwe afzender? → niet op termen/links scannen
        m["friendly"] = mail_is_friendly(m, p, standing_by_id)
        m["suspect"] = [] if m["friendly"] else sorted(_scan_text(
            (m.get("subject") or "") + " " + (m.get("body") or ""), trusted))

    return {
        "stats": basic_stats(eve_character),
        "registered": p.get("ok", False),
        "source": p.get("source"),
        "skill_groups": p.get("skill_groups", []),
        "corp_history": p.get("corp_history", []),
        "contacts": p.get("contacts", []),
        "contracts": p.get("contracts", []),
        "mails": mails,
        "ship": {
            "type_id": p.get("ship_type_id"),
            "type_name": p.get("ship_type_name"),
            "name": p.get("ship_name"),
        },
        "location": {
            "system_id": p.get("system_id"),
            "system_name": p.get("location_name"),
            "station_name": p.get("station_name"),
            "docked": p.get("docked", False),
        },
    }
