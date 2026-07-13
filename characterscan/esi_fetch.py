"""
Centrale data-ophaler voor Character Scan.

`get_profile(eve_character)` levert één canoniek dict met alle velden die de
weergave (profile.py) en de vetting (vetting.py) nodig hebben. Bron:
  1. LIVE via ESI met het door CharLink gekoppelde recruit-token  [primair]
  2. Member Audit  [tijdelijke fallback, verdwijnt als MA weg is]

Zo is Character Scan niet langer afhankelijk van Member Audit.
"""

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone as dt_timezone

import requests

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

ESI = "https://esi.evetech.net/latest"
UA = {"User-Agent": "aa-characterscan (local eval)"}
PROFILE_CACHE_SECONDS = 600  # 10 min — houd paginabezoeken snel

# Scopes die een recruit via CharLink verleent (recruit-data). Zelfde set als de
# oorspronkelijke recruiting-app.
CS_RECRUIT_SCOPES = [
    "publicData",
    "esi-wallet.read_character_wallet.v1",
    "esi-skills.read_skills.v1",
    "esi-characters.read_contacts.v1",
    "esi-characters.read_standings.v1",
    "esi-contracts.read_character_contracts.v1",
    "esi-location.read_location.v1",
    "esi-location.read_ship_type.v1",
    "esi-clones.read_clones.v1",
    "esi-mail.read_mail.v1",
]

# Skill-injector-types (vaste ESI type_ids)
LARGE_SKILL_INJECTOR = 40520
SMALL_SKILL_INJECTOR = 45635
SKILL_INJECTOR_TYPES = {
    LARGE_SKILL_INJECTOR: "Large Skill Injector",
    SMALL_SKILL_INJECTOR: "Small Skill Injector",
}

RISK_SKILL_PATTERNS = [
    ("cyno", "Cyno"),
    ("black ops", "Black Ops"),
    ("covert ops", "Covert Ops"),
    ("recon ships", "Recon"),
    ("jump drive", "Jump Drive"),
    ("jump portal", "Jump Drive"),
]


def _risk_label(name):
    low = (name or "").lower()
    for needle, label in RISK_SKILL_PATTERNS:
        if needle in low:
            return label
    return None


def _fmt_duration(days):
    """Aantal dagen → compacte NL-looptijd (bijv. '2j 3m', '5 mnd', '12 dagen')."""
    if days is None or days < 0:
        return ""
    if days < 60:
        return f"{days} dag" + ("" if days == 1 else "en")
    if days < 365:
        return f"{days // 30} mnd"
    y, rem = divmod(days, 365)
    m = rem // 30
    return f"{y}j" + (f" {m}m" if m else "")


def _age_years(birthday):
    if not birthday:
        return None
    if isinstance(birthday, str):
        try:
            birthday = datetime.fromisoformat(birthday.replace("Z", "+00:00"))
        except ValueError:
            return None
    if timezone.is_naive(birthday):
        birthday = birthday.replace(tzinfo=dt_timezone.utc)
    return round((timezone.now() - birthday).days / 365.25, 1)


# ── ESI helpers ──────────────────────────────────────────────────────────────
def _pub(path):
    try:
        r = requests.get(f"{ESI}{path}?datasource=tranquility", headers=UA, timeout=8)
        if r.ok:
            return r.json()
    except Exception:
        pass
    return None


def _auth(path, token, paged=False):
    out, page = [], 1
    while True:
        try:
            r = requests.get(
                f"{ESI}{path}?datasource=tranquility&page={page}",
                headers={**UA, "Authorization": f"Bearer {token}"}, timeout=10,
            )
        except Exception:
            break
        if not r.ok:
            return None if not out else out
        data = r.json()
        if not paged:
            return data
        out.extend(data or [])
        if page >= int(r.headers.get("X-Pages", 1) or 1) or not data:
            break
        page += 1
    return out


def _parallel(jobs, max_workers=10):
    """Voer een dict {key: callable} gelijktijdig uit → {key: resultaat}.
    Scheelt veel laadtijd t.o.v. de calls serieel afvuren."""
    if not jobs:
        return {}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(jobs))) as ex:
        futs = {k: ex.submit(fn) for k, fn in jobs.items()}
        return {k: f.result() for k, f in futs.items()}


def _names(ids):
    names, ids = {}, list({i for i in ids if i})
    for i in range(0, len(ids), 1000):
        try:
            r = requests.post(f"{ESI}/universe/names/?datasource=tranquility",
                              json=ids[i:i + 1000], headers=UA, timeout=8)
            if r.ok:
                for x in r.json():
                    names[x["id"]] = x["name"]
        except Exception:
            pass
    return names


def _skill_types(type_ids):
    """type_id → eveuniverse EveType (bulk uit DB; ontbrekende via ESI). Snel."""
    from eveuniverse.models import EveType

    types = {t.id: t for t in EveType.objects.filter(id__in=type_ids).select_related("eve_group")}
    for tid in type_ids:
        if tid not in types:
            try:
                types[tid] = EveType.objects.get_or_create_esi(id=tid)[0]
            except Exception:
                pass
    return types


def _group_skills(skills, name_map):
    """skills [{skill_id, skillpoints_in_skill, trained_skill_level}] → gegroepeerd per EVE-groep."""
    types = _skill_types([s["skill_id"] for s in skills])
    groups = {}
    for s in skills:
        t = types.get(s["skill_id"])
        gname = t.eve_group.name if (t and t.eve_group_id) else "Overig"
        sname = t.name if t else name_map.get(s["skill_id"], f"Skill {s['skill_id']}")
        g = groups.setdefault(gname, {"name": gname, "total_sp": 0, "skills": []})
        sp = s.get("skillpoints_in_skill", 0) or 0
        g["total_sp"] += sp
        g["skills"].append({"name": sname, "level": s.get("trained_skill_level", 0), "sp": sp})
    result = sorted(groups.values(), key=lambda g: g["total_sp"], reverse=True)
    for g in result:
        g["skills"].sort(key=lambda x: x["sp"], reverse=True)
        g["count"] = len(g["skills"])
    return result


# ── Token-lookup ─────────────────────────────────────────────────────────────
def ma_character(eve_character):
    """De Member Audit Character voor dit EveCharacter, of None (tijdelijke fallback)."""
    return getattr(eve_character, "memberaudit_character", None)


def recruit_token(character_id):
    """Een token voor dit character met (een deel van) de recruit-scopes, of None."""
    try:
        from esi.models import Token
        return Token.objects.filter(
            character_id=character_id,
            scopes__name="esi-skills.read_skills.v1",
        ).first()
    except Exception:
        return None


def has_recruit_token(character_id):
    return recruit_token(character_id) is not None


# ── Owner/account-info (uit AA, geen ESI) ────────────────────────────────────
def _owner_info(eve_character):
    try:
        main = eve_character.character_ownership.user.profile.main_character
        if main:
            return main.character_name, main.character_id != eve_character.character_id
    except Exception:
        pass
    return None, False


def _skill_injectors(transactions):
    """Tel gekochte skill-injectors uit de wallet-transacties → samenvatting."""
    buys = [t for t in transactions
            if t.get("is_buy") and t.get("type_id") in SKILL_INJECTOR_TYPES]
    large = sum(t.get("quantity", 0) or 0 for t in buys if t.get("type_id") == LARGE_SKILL_INJECTOR)
    small = sum(t.get("quantity", 0) or 0 for t in buys if t.get("type_id") == SMALL_SKILL_INJECTOR)
    dates = sorted(t.get("date") for t in buys if t.get("date"))
    isk = sum((t.get("quantity", 0) or 0) * (t.get("unit_price", 0) or 0) for t in buys)
    return {
        "large": large,
        "small": small,
        "total": large + small,
        "buys": len(buys),
        "first": dates[0] if dates else None,
        "last": dates[-1] if dates else None,
        "isk": isk,
        "has_transactions": bool(transactions),
    }


def _strip_html(html):
    """EVE-mail-body (HTML) → platte tekst."""
    h = re.sub(r"<br\s*/?>", "\n", html or "", flags=re.I)
    h = re.sub(r"</p>", "\n", h, flags=re.I)
    h = re.sub(r"<[^>]+>", "", h)
    for a, b in (("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"), ("&nbsp;", " ")):
        h = h.replace(a, b)
    return h.strip()


# ── LIVE ophalen via ESI ─────────────────────────────────────────────────────
def _fetch_live(eve_character, token):
    cid = eve_character.character_id
    access = token.valid_access_token()

    # Alle onafhankelijke ESI-calls tegelijk ophalen (parallel i.p.v. serieel)
    res = _parallel({
        "info": lambda: _pub(f"/characters/{cid}/"),
        "wallet": lambda: _auth(f"/characters/{cid}/wallet/", access),
        "skills": lambda: _auth(f"/characters/{cid}/skills/", access),
        "contacts": lambda: _auth(f"/characters/{cid}/contacts/", access, paged=True),
        "contracts": lambda: _auth(f"/characters/{cid}/contracts/", access, paged=True),
        "journal": lambda: _auth(f"/characters/{cid}/wallet/journal/", access),
        "transactions": lambda: _auth(f"/characters/{cid}/wallet/transactions/", access),
        "loc": lambda: _auth(f"/characters/{cid}/location/", access),
        "ship": lambda: _auth(f"/characters/{cid}/ship/", access),
        "hist": lambda: _pub(f"/characters/{cid}/corporationhistory/"),
        "mail": lambda: _auth(f"/characters/{cid}/mail/", access),
    })
    info = res["info"]
    wallet = res["wallet"]
    skills_raw = res["skills"]
    contacts_raw = res["contacts"]
    contracts_raw = res["contracts"]
    journal_raw = res["journal"]
    transactions_raw = res["transactions"]
    loc = res["loc"]
    ship = res["ship"]
    hist_raw = res["hist"] or []
    mail_raw = res["mail"]

    skills = (skills_raw or {}).get("skills", []) if isinstance(skills_raw, dict) else []
    contacts_raw = contacts_raw if isinstance(contacts_raw, list) else []
    contracts_raw = contracts_raw if isinstance(contracts_raw, list) else []
    journal_raw = journal_raw if isinstance(journal_raw, list) else []
    transactions_raw = transactions_raw if isinstance(transactions_raw, list) else []
    recent_mails = (mail_raw if isinstance(mail_raw, list) else [])[:15]

    # Skill-injector-aankopen uit de transacties (spy-/farm-signaal)
    skill_injectors = _skill_injectors(transactions_raw)

    # Verzamel ids voor namen
    ids = set()
    if info:
        ids.update(filter(None, [info.get("corporation_id"), info.get("alliance_id")]))
    for s in skills:
        ids.add(s["skill_id"])
    for c in contacts_raw:
        ids.add(c["contact_id"])
    for h in hist_raw:
        ids.add(h["corporation_id"])
    for j in journal_raw[:200]:
        ids.update(filter(None, [j.get("first_party_id"), j.get("second_party_id")]))
    for c in contracts_raw:
        ids.update(filter(None, [c.get("issuer_id"), c.get("assignee_id"),
                                 c.get("acceptor_id"), c.get("issuer_corporation_id")]))
    for m in recent_mails:
        ids.add(m.get("from"))
        ids.update(r.get("recipient_id") for r in m.get("recipients", []))
    if loc:
        ids.update(filter(None, [loc.get("solar_system_id"), loc.get("station_id")]))
    if ship:
        ids.add(ship.get("ship_type_id"))
    ids.discard(None)
    name_map = _names(ids)

    # Mails: koppen + (per mail) de body parallel ophalen en strippen
    def _body(m):
        full = _auth(f"/characters/{cid}/mail/{m['mail_id']}/", access)
        return _strip_html(full.get("body", "")) if isinstance(full, dict) else ""

    body_map = _parallel(
        {i: (lambda mm=m: _body(mm)) for i, m in enumerate(recent_mails)}, max_workers=8
    )
    bodies = [body_map.get(i, "") for i in range(len(recent_mails))]
    mails = [{
        "subject": m.get("subject", ""),
        "from_id": m.get("from"),
        "from_name": name_map.get(m.get("from")),
        "recipient_ids": [r.get("recipient_id") for r in m.get("recipients", [])],
        "date": m.get("timestamp"),
        "body": body,
    } for m, body in zip(recent_mails, bodies)]

    # Corp-historie + alliance per corp (alliance-lookups parallel)
    from .vetting import corp_alliance, _parse_date  # hergebruik gecachte lookup
    # Einddatum van elke stint = start van de volgende corp in de VOLLEDIGE historie
    # (incl. NPC-corps), zodat de looptijd klopt ook als er NPC-corps tussen zaten.
    full_asc = sorted(hist_raw, key=lambda x: x["start_date"])
    end_by_start = {h["start_date"]: (full_asc[i + 1]["start_date"] if i + 1 < len(full_asc) else None)
                    for i, h in enumerate(full_asc)}
    player_hist = [h for h in sorted(hist_raw, key=lambda x: x["start_date"], reverse=True)
                   if h["corporation_id"] >= 98_000_000]  # NPC-corps eruit (weergave)
    alliance_map = _parallel(
        {h["corporation_id"]: (lambda c=h["corporation_id"]: corp_alliance(c)) for h in player_hist},
        max_workers=8,
    )
    now = timezone.now()
    corp_history = []
    for h in player_hist:
        start_dt = _parse_date(h["start_date"])
        end_raw = end_by_start.get(h["start_date"])
        end_dt = _parse_date(end_raw) if end_raw else None
        days = ((end_dt or now) - start_dt).days if start_dt else None
        corp_history.append({
            "corp_id": h["corporation_id"],
            "corp_name": name_map.get(h["corporation_id"], str(h["corporation_id"])),
            "alliance_id": alliance_map.get(h["corporation_id"]),
            "start": h["start_date"],
            "start_dt": start_dt,
            "end_dt": end_dt,
            "duration_days": days,
            "duration_label": _fmt_duration(days),
            "is_current": end_raw is None,
            "is_deleted": h.get("is_deleted", False),
        })

    skill_list = [{"skill_id": s["skill_id"],
                   "skillpoints_in_skill": s.get("skillpoints_in_skill", 0),
                   "trained_skill_level": s.get("trained_skill_level", 0)} for s in skills]
    skill_groups = _group_skills(skill_list, name_map)
    risk_skills = [
        {"name": name_map.get(s["skill_id"], ""), "level": s["trained_skill_level"],
         "label": _risk_label(name_map.get(s["skill_id"], ""))}
        for s in skill_list if _risk_label(name_map.get(s["skill_id"], ""))
    ]

    CONTRACT_TYPE = {"item_exchange": "item exchange", "auction": "auction",
                     "courier": "courier", "loan": "loan", "unknown": "unknown"}
    contracts = []
    for c in sorted(contracts_raw, key=lambda x: x.get("date_issued", ""), reverse=True)[:40]:
        contracts.append({
            "type": CONTRACT_TYPE.get(c.get("type"), c.get("type", "")),
            "status": (c.get("status") or "").replace("_", " "),
            "title": c.get("title") or "",
            "date": c.get("date_issued"),
            "issuer_id": c.get("issuer_id"),
            "issuer": name_map.get(c.get("issuer_id")),
            "issuer_corp_id": c.get("issuer_corporation_id"),
            "issuer_corp": name_map.get(c.get("issuer_corporation_id")),
            "assignee_id": c.get("assignee_id"),
            "assignee": name_map.get(c.get("assignee_id")),
            "assignee_cat": None,
            "acceptor_id": c.get("acceptor_id"),
            "acceptor": name_map.get(c.get("acceptor_id")),
        })

    contacts = sorted(
        [{"id": c["contact_id"], "name": name_map.get(c["contact_id"], str(c["contact_id"])),
          "standing": c["standing"]} for c in contacts_raw],
        key=lambda x: x["standing"], reverse=True,
    )[:100]

    journal = [{
        "date": j.get("date"),
        "ref_type": j.get("ref_type", ""),
        "amount": j.get("amount"),
        "first_party_id": j.get("first_party_id"),
        "first_party_name": name_map.get(j.get("first_party_id")),
        "second_party_id": j.get("second_party_id"),
        "second_party_name": name_map.get(j.get("second_party_id")),
    } for j in journal_raw[:1000]]

    owner_main, is_alt = _owner_info(eve_character)
    return {
        "ok": True,
        "source": "esi",
        "name": eve_character.character_name,
        "corp_id": info.get("corporation_id") if info else eve_character.corporation_id,
        "corp_name": name_map.get(info.get("corporation_id")) if info else eve_character.corporation_name,
        "alliance_id": info.get("alliance_id") if info else eve_character.alliance_id,
        "alliance_name": name_map.get(info.get("alliance_id")) if info and info.get("alliance_id") else eve_character.alliance_name,
        "owner_main": owner_main, "is_alt": is_alt,
        "sec": info.get("security_status") if info else None,
        "age_years": _age_years(info.get("birthday")) if info else None,
        "wallet": wallet if isinstance(wallet, (int, float)) else None,
        "total_sp": (skills_raw or {}).get("total_sp") if isinstance(skills_raw, dict) else None,
        "unallocated_sp": (skills_raw or {}).get("unallocated_sp") if isinstance(skills_raw, dict) else None,
        "skill_count": len(skill_list),
        "skill_groups": skill_groups,
        "risk_skills": risk_skills,
        "contacts": contacts,
        "contracts": contracts,
        "corp_history": corp_history,
        "wallet_journal": journal,
        "mails": mails,
        "skill_injectors": skill_injectors,
        "ship_type_id": ship.get("ship_type_id") if ship else None,
        "ship_type_name": name_map.get(ship.get("ship_type_id")) if ship else None,
        "ship_name": ship.get("ship_name") if ship else None,  # speler-gegeven naam
        "system_id": loc.get("solar_system_id") if loc else None,
        "location_name": name_map.get(loc.get("solar_system_id")) if loc else None,
        "station_name": name_map.get(loc.get("station_id")) if loc and loc.get("station_id") else None,
        "docked": bool(loc.get("station_id") or loc.get("structure_id")) if loc else False,
    }


# ── Member Audit fallback (bouwt hetzelfde dict) ─────────────────────────────
def _fetch_memberaudit(eve_character):
    ma = getattr(eve_character, "memberaudit_character", None)
    if not ma:
        return None
    from .vetting import corp_alliance

    def one(rel):
        try:
            return getattr(ma, rel)
        except Exception:
            return None

    details = one("details")
    wallet = one("wallet_balance")
    sp = one("skillpoints")

    skill_groups, risk_skills, skill_count = [], [], None
    try:
        groups = {}
        for s in ma.skills.select_related("eve_type__eve_group").all():
            gname = getattr(s.eve_type.eve_group, "name", "Overig")
            g = groups.setdefault(gname, {"name": gname, "total_sp": 0, "skills": []})
            spv = s.skillpoints_in_skill or 0
            g["total_sp"] += spv
            g["skills"].append({"name": s.eve_type.name, "level": s.trained_skill_level, "sp": spv})
            if _risk_label(s.eve_type.name):
                risk_skills.append({"name": s.eve_type.name, "level": s.trained_skill_level,
                                    "label": _risk_label(s.eve_type.name)})
        skill_count = sum(len(g["skills"]) for g in groups.values())
        skill_groups = sorted(groups.values(), key=lambda g: g["total_sp"], reverse=True)
        for g in skill_groups:
            g["skills"].sort(key=lambda x: x["sp"], reverse=True)
            g["count"] = len(g["skills"])
    except Exception:
        pass

    corp_history = []
    try:
        full = list(ma.corporation_history.select_related("corporation").order_by("start_date"))
        end_by_start = {h.start_date: (full[i + 1].start_date if i + 1 < len(full) else None)
                        for i, h in enumerate(full)}
        now = timezone.now()
        for h in reversed(full):
            if h.corporation_id < 98_000_000:
                continue
            end_dt = end_by_start.get(h.start_date)
            days = ((end_dt or now) - h.start_date).days if h.start_date else None
            corp_history.append({
                "corp_id": h.corporation_id,
                "corp_name": getattr(h.corporation, "name", str(h.corporation_id)),
                "alliance_id": corp_alliance(h.corporation_id),
                "start": h.start_date,
                "start_dt": h.start_date,
                "end_dt": end_dt,
                "duration_days": days,
                "duration_label": _fmt_duration(days),
                "is_current": end_dt is None,
                "is_deleted": getattr(h, "is_deleted", False),
            })
    except Exception:
        pass

    contacts = []
    try:
        for c in ma.contacts.select_related("eve_entity").order_by("-standing")[:100]:
            contacts.append({"id": c.eve_entity_id,
                             "name": getattr(c.eve_entity, "name", str(c.eve_entity_id)),
                             "standing": c.standing})
    except Exception:
        pass

    contracts = []
    try:
        for c in ma.contracts.select_related("issuer", "issuer_corporation", "assignee", "acceptor").order_by("-date_issued")[:40]:
            contracts.append({
                "type": c.get_contract_type_display(), "status": c.get_status_display(),
                "title": c.title or "", "date": c.date_issued,
                "issuer_id": c.issuer_id, "issuer": getattr(c.issuer, "name", None),
                "issuer_corp_id": c.issuer_corporation_id, "issuer_corp": getattr(c.issuer_corporation, "name", None),
                "assignee_id": c.assignee_id, "assignee": getattr(c.assignee, "name", None),
                "assignee_cat": getattr(c.assignee, "category", None),
                "acceptor_id": c.acceptor_id, "acceptor": getattr(c.acceptor, "name", None),
            })
    except Exception:
        pass

    journal = []
    try:
        for j in ma.wallet_journal.select_related("first_party", "second_party").order_by("-date")[:1000]:
            journal.append({
                "date": j.date, "ref_type": j.ref_type, "amount": j.amount,
                "first_party_id": j.first_party_id, "first_party_name": getattr(j.first_party, "name", None),
                "second_party_id": j.second_party_id, "second_party_name": getattr(j.second_party, "name", None),
            })
    except Exception:
        pass

    owner_main, is_alt = _owner_info(eve_character)
    return {
        "ok": True, "source": "memberaudit",
        "name": eve_character.character_name,
        "corp_id": eve_character.corporation_id, "corp_name": eve_character.corporation_name,
        "alliance_id": eve_character.alliance_id, "alliance_name": eve_character.alliance_name or "",
        "owner_main": owner_main, "is_alt": is_alt,
        "sec": getattr(details, "security_status", None),
        "age_years": _age_years(getattr(details, "birthday", None)),
        "wallet": getattr(wallet, "total", None),
        "total_sp": getattr(sp, "total", None),
        "unallocated_sp": getattr(sp, "unallocated", None),
        "skill_count": skill_count,
        "skill_groups": skill_groups, "risk_skills": risk_skills,
        "contacts": contacts, "contracts": contracts,
        "corp_history": corp_history, "wallet_journal": journal,
        "ship_type_id": None, "ship_type_name": None, "ship_name": None,
        "system_id": None, "location_name": None, "station_name": None, "docked": False,
    }


def _empty(eve_character):
    owner_main, is_alt = _owner_info(eve_character)
    return {
        "ok": False, "source": None,
        "name": eve_character.character_name,
        "corp_id": eve_character.corporation_id, "corp_name": eve_character.corporation_name,
        "alliance_id": eve_character.alliance_id, "alliance_name": eve_character.alliance_name or "",
        "owner_main": owner_main, "is_alt": is_alt,
        "sec": None, "age_years": None, "wallet": None, "total_sp": None, "unallocated_sp": None, "skill_count": None,
        "skill_groups": [], "risk_skills": [], "contacts": [], "contracts": [],
        "corp_history": [], "wallet_journal": [],
        "ship_type_id": None, "ship_type_name": None, "ship_name": None,
        "system_id": None, "location_name": None, "station_name": None, "docked": False,
    }


def get_profile(eve_character, force=False):
    """Canoniek data-dict voor een character. Live via ESI-token, anders MA-fallback."""
    cid = eve_character.character_id
    key = f"cs_profile_{cid}"
    if not force:
        cached = cache.get(key)
        if cached is not None:
            return cached

    profile = None
    token = recruit_token(cid)
    if token:
        try:
            profile = _fetch_live(eve_character, token)
        except Exception as e:
            logger.warning("Character Scan live-fetch faalde voor %s: %s", cid, e)
            profile = None
    if profile is None:
        profile = _fetch_memberaudit(eve_character)  # tijdelijke fallback
    if profile is None:
        profile = _empty(eve_character)

    profile["character_id"] = cid
    cache.set(key, profile, PROFILE_CACHE_SECONDS)
    return profile
