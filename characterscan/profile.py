"""
Weergave-laag: leest het canonieke data-dict uit esi_fetch en markeert vijanden.
Character Scan is hiermee niet meer afhankelijk van Member Audit-modellen.
"""

from .esi_fetch import get_profile, sov_map
from .vetting import (
    _fmt_isk,
    _location_enemy,
    _parse_date,
    _scan_text,
    mail_is_friendly,
    trusted_domains,
)


def basic_stats(eve_character, lite=False):
    """Compacte stats voor de recruiter-lijst / aanmeld-pagina."""
    p = get_profile(eve_character, lite=lite)
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
        "owner_main_id": p.get("owner_main_id"),
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

    # Clones + assets: markeer vijandelijke locaties en groepeer jump clones
    clones = p.get("clones")
    assets = p.get("assets")
    if clones or assets:
        sov = sov_map()

        def _mark(locs):
            for lc in locs or []:
                is_en, reason = _location_enemy(lc, enemy, sov)
                lc["is_enemy"], lc["enemy_reason"] = is_en, reason

        if clones:
            if clones.get("home"):
                _mark([clones["home"]])
            _mark(clones.get("locations"))
            grouped = {}
            for lc in clones.get("locations") or []:
                g = grouped.get(lc["location_id"])
                if not g:
                    g = grouped[lc["location_id"]] = {**lc, "count": 0, "max_implants": 0}
                g["count"] += 1
                g["max_implants"] = max(g["max_implants"], lc.get("implants", 0))
            clones["grouped"] = sorted(grouped.values(),
                                       key=lambda x: (x["is_enemy"], x["count"]), reverse=True)
        if assets:
            _mark(assets.get("locations"))

    # Wallet-journal → compacte transactielijst voor het wallet-blok
    cid = p.get("character_id")
    wallet_entries = []
    for j in p.get("wallet_journal", [])[:40]:
        amt = j.get("amount") or 0
        cp_id = cp_name = None
        for pid, name in ((j.get("first_party_id"), j.get("first_party_name")),
                          (j.get("second_party_id"), j.get("second_party_name"))):
            if pid and pid != cid:
                cp_id, cp_name = pid, name
                break
        wallet_entries.append({
            "date": _parse_date(j.get("date")),
            "ref_type": (j.get("ref_type") or "").replace("_", " "),
            "amount": amt,
            "amount_fmt": _fmt_isk(amt),
            "cp_id": cp_id,
            "cp_name": cp_name,
            "is_enemy": bool(cp_id) and cp_id in enemy,
        })
    journal_all = p.get("wallet_journal", [])
    income = expense = 0.0
    by_type = {}
    for j in journal_all:
        a = float(j.get("amount") or 0)
        if a > 0:
            income += a
            rt = (j.get("ref_type") or "").replace("_", " ")
            by_type[rt] = by_type.get(rt, 0.0) + a
        elif a < 0:
            expense += a
    top_sources = sorted(by_type.items(), key=lambda x: x[1], reverse=True)[:5]
    wallet = {
        "balance": p.get("wallet"),
        "entries": wallet_entries,
        "income_fmt": _fmt_isk(income),
        "expense_fmt": _fmt_isk(expense),
        "net_fmt": _fmt_isk(income + expense),
        "tx_count": len(journal_all),
        "top_sources": [{"type": t, "amount_fmt": _fmt_isk(v)} for t, v in top_sources],
    }

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
        "clones": clones,
        "assets": assets,
        "wallet": wallet,
    }
