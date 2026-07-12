"""
Weergave-laag: leest het canonieke data-dict uit esi_fetch en markeert vijanden.
Character Scan is hiermee niet meer afhankelijk van Member Audit-modellen.
"""

from .esi_fetch import get_profile


def basic_stats(eve_character):
    """Compacte stats voor de recruiter-lijst / aanmeld-pagina."""
    p = get_profile(eve_character)
    return {
        "registered": p.get("ok", False),
        "source": p.get("source"),
        "corp_name": p.get("corp_name"),
        "alliance_name": p.get("alliance_name") or "",
        "corp_id": p.get("corp_id"),
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

    return {
        "stats": basic_stats(eve_character),
        "registered": p.get("ok", False),
        "source": p.get("source"),
        "skill_groups": p.get("skill_groups", []),
        "corp_history": p.get("corp_history", []),
        "contacts": p.get("contacts", []),
        "contracts": p.get("contracts", []),
    }
