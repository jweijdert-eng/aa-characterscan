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
    "esi-assets.read_assets.v1",
    "esi-universe.read_structures.v1",
    "esi-killmails.read_killmails.v1",
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
    """Namen voor entity-ids via /universe/names.

    Die endpoint geeft **404 op de HELE batch** zodra ook maar één id onresolvebaar
    is (een player-structure, een verwijderd character, e.d.) — waardoor vroeger
    álle namen op hun id terugvielen. Daarom bij een fout de batch **binair
    opsplitsen**: zo krijgen alle goede ids tóch een naam en slaan we alleen het
    rotte id over.
    """
    names, todo = {}, list({int(i) for i in ids if i})

    def resolve(batch):
        if not batch:
            return
        try:
            r = requests.post(f"{ESI}/universe/names/?datasource=tranquility",
                              json=batch, headers=UA, timeout=8)
        except Exception:
            return
        if r.ok:
            for x in r.json():
                names[x["id"]] = x["name"]
        elif len(batch) > 1:                       # onbekend id ertussen → opsplitsen
            mid = len(batch) // 2
            resolve(batch[:mid])
            resolve(batch[mid:])

    for i in range(0, len(todo), 1000):
        resolve(todo[i:i + 1000])
    return names


def sov_map():
    """{solar_system_id: alliance_id} uit de sovereignty-map (publiek, sterk gecached)."""
    key = "cs_sov_map"
    cached = cache.get(key)
    if cached is not None:
        return cached
    out = {}
    try:
        r = requests.get(f"{ESI}/sovereignty/map/?datasource=tranquility", headers=UA, timeout=15)
        if r.ok:
            for row in r.json():
                if row.get("alliance_id"):
                    out[row["system_id"]] = row["alliance_id"]
    except Exception:
        pass
    cache.set(key, out, 6 * 3600)
    return out


def resolve_location(location_id, access=None):
    """Station/structure-id → {name, system_id, owner_corp_id} (gecached). None = onbekend."""
    if not location_id:
        return None
    key = f"cs_loc_{location_id}"
    cached = cache.get(key)
    if cached is not None:
        return cached or None
    info = None
    try:
        if location_id > 1_000_000_000_000:  # player-structure (citadel)
            if access:
                r = requests.get(
                    f"{ESI}/universe/structures/{location_id}/?datasource=tranquility",
                    headers={**UA, "Authorization": f"Bearer {access}"}, timeout=8)
                if r.ok:
                    d = r.json()
                    info = {"name": d.get("name"), "system_id": d.get("solar_system_id"),
                            "owner_corp_id": d.get("owner_id"), "kind": "structure"}
        elif 60_000_000 <= location_id < 64_000_000:  # NPC-station
            r = requests.get(f"{ESI}/universe/stations/{location_id}/?datasource=tranquility",
                             headers=UA, timeout=8)
            if r.ok:
                d = r.json()
                info = {"name": d.get("name"), "system_id": d.get("system_id"),
                        "owner_corp_id": d.get("owner"), "kind": "station"}
    except Exception:
        pass
    cache.set(key, info or {}, 7 * 86400)
    return info


def _affiliations(char_ids):
    """character_id → (corporation_id, alliance_id) via één POST (publiek, geen auth)."""
    ids = list({c for c in char_ids if c})
    out = {}
    for i in range(0, len(ids), 1000):
        try:
            r = requests.post(f"{ESI}/characters/affiliation/?datasource=tranquility",
                              json=ids[i:i + 1000], headers=UA, timeout=8)
            if r.ok:
                for x in r.json():
                    out[x["character_id"]] = (x.get("corporation_id"), x.get("alliance_id"))
        except Exception:
            pass
    return out


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
            return (main.character_name,
                    main.character_id != eve_character.character_id,
                    main.character_id)
    except Exception:
        pass
    return None, False, None


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


def _decode_bio(desc):
    """EVE-character-descriptions komen soms als Python-repr uit ESI, bv.
    ``u'<font ...>...'`` mét escapes (\\n, \\') erin. Pak die uit tot de echte
    HTML zodat de bio-scan op schone tekst/links werkt."""
    desc = desc or ""
    if desc[:2] in ("u'", 'u"'):
        try:
            import ast
            val = ast.literal_eval(desc)
            if isinstance(val, str):
                return val
        except Exception:  # noqa: BLE001
            pass
    return desc


def _strip_html(html):
    """EVE-mail-body (HTML) → platte tekst."""
    h = re.sub(r"<br\s*/?>", "\n", html or "", flags=re.I)
    h = re.sub(r"</p>", "\n", h, flags=re.I)
    h = re.sub(r"<[^>]+>", "", h)
    for a, b in (("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"), ("&nbsp;", " ")):
        h = h.replace(a, b)
    return h.strip()


def _process_killmails(km_list, cid):
    """Recente killmails → lijst parsed dicts (kill/loss, victim-org, mede-aanvaller-orgs)."""
    km_list = (km_list if isinstance(km_list, list) else [])[:30]
    if not km_list:
        return []

    def one(k):
        d = _pub(f"/killmails/{k['killmail_id']}/{k['killmail_hash']}/")
        if not isinstance(d, dict):
            return None
        victim = d.get("victim") or {}
        attackers = d.get("attackers") or []
        att_chars = {a.get("character_id") for a in attackers}
        others = [a for a in attackers if a.get("character_id") != cid]
        return {
            "id": k["killmail_id"],
            "date": d.get("killmail_time"),
            "is_kill": cid in att_chars,  # recruit staat bij de aanvallers
            "victim_char": victim.get("character_id"),
            "victim_corp": victim.get("corporation_id"),
            "victim_alliance": victim.get("alliance_id"),
            "attacker_corp_ids": list({a.get("corporation_id") for a in others if a.get("corporation_id")}),
            "attacker_alliance_ids": list({a.get("alliance_id") for a in others if a.get("alliance_id")}),
        }

    results = _parallel({i: (lambda kk=k: one(kk)) for i, k in enumerate(km_list)}, max_workers=10)
    return [r for r in (results.get(i) for i in range(len(km_list))) if r]


def _process_clones(clones_raw, access):
    """Clones-endpoint → {jump_count, home, locations[]} met geresolvede locaties."""
    if not isinstance(clones_raw, dict):
        return None
    home = clones_raw.get("home_location") or {}
    jcs = clones_raw.get("jump_clones") or []
    loc_ids = {j.get("location_id") for j in jcs if j.get("location_id")}
    if home.get("location_id"):
        loc_ids.add(home["location_id"])
    resolved = _parallel(
        {lid: (lambda l=lid: resolve_location(l, access)) for lid in loc_ids}, max_workers=8
    ) if loc_ids else {}

    def loc(lid):
        r = resolved.get(lid) or {}
        return {"location_id": lid, "name": r.get("name"), "system_id": r.get("system_id"),
                "owner_corp_id": r.get("owner_corp_id"), "kind": r.get("kind")}

    result = {
        "jump_count": len(jcs),
        "home": ({**loc(home["location_id"]), "type": home.get("location_type")}
                 if home.get("location_id") else None),
        "locations": [{**loc(j.get("location_id")), "type": j.get("location_type"),
                       "implants": len(j.get("implants", []))} for j in jcs],
    }
    marks = list(result["locations"])
    if result["home"]:
        marks.append(result["home"])
    _mark_security(marks)  # sec-kleur per locatie (EVE-security van het systeem)
    return result


# EVE-security-kleuren (zoals in de client): per 0,1-stap; negatief → nullsec-rood.
SEC_COLORS = {
    1.0: "#2FEFEF", 0.9: "#48F0C0", 0.8: "#00EF47", 0.7: "#00F000", 0.6: "#8FEF2F",
    0.5: "#EFEF00", 0.4: "#D77700", 0.3: "#F06000", 0.2: "#F04800", 0.1: "#D73000", 0.0: "#F00000",
}


def _sec_color(sec):
    """Security-status → EVE-kleur (hex), of None."""
    if sec is None:
        return None
    step = round(min(max(sec, 0.0), 1.0), 1)  # negatief telt als 0.0 (rood)
    return SEC_COLORS.get(step, "#F00000")


def system_security(system_id):
    """Security-status van een solar system (ESI, sterk gecached). None = onbekend."""
    if not system_id:
        return None
    key = f"cs_sysec_{system_id}"
    cached = cache.get(key)
    if cached is not None:
        return None if cached == "none" else cached
    sec = None
    try:
        r = requests.get(f"{ESI}/universe/systems/{system_id}/?datasource=tranquility",
                         headers=UA, timeout=8)
        if r.ok:
            sec = r.json().get("security_status")
    except Exception:  # noqa: BLE001
        pass
    cache.set(key, "none" if sec is None else sec, 7 * 86400)
    return sec


def _mark_security(locations):
    """Voeg sec + sec_color (EVE-kleur) toe aan een lijst locatie-dicts."""
    sys_ids = {lc.get("system_id") for lc in locations if lc.get("system_id")}
    secs = _parallel({sid: (lambda s=sid: system_security(s)) for sid in sys_ids},
                     max_workers=8) if sys_ids else {}
    for lc in locations:
        sec = secs.get(lc.get("system_id"))
        lc["sec"] = round(sec, 1) if sec is not None else None
        lc["sec_color"] = _sec_color(sec)
        # Naam splitsen: deel vóór de eerste ' - ' (systeem) krijgt de sec-kleur, rest grijs.
        head, sep, tail = (lc.get("name") or "").partition(" - ")
        lc["name_head"] = head
        lc["name_tail"] = (sep + tail) if sep else ""


def _process_assets(assets_raw, access):
    """Assets → {count, location_count, locations[]} met geresolvede top-locaties."""
    if not assets_raw:
        return None
    item_ids = {a.get("item_id") for a in assets_raw}
    counts = {}
    for a in assets_raw:
        lid = a.get("location_id")
        if lid and lid not in item_ids:  # top-level locatie (niet in een container/schip)
            counts[lid] = counts.get(lid, 0) + 1
    top = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:25]
    resolved = _parallel(
        {lid: (lambda l=lid: resolve_location(l, access)) for lid, _ in top}, max_workers=10
    ) if top else {}
    locations = []
    for lid, n in top:
        r = resolved.get(lid) or {}
        locations.append({"location_id": lid, "name": r.get("name"), "system_id": r.get("system_id"),
                          "owner_corp_id": r.get("owner_corp_id"), "kind": r.get("kind"), "item_count": n})
    _mark_security(locations)  # sec-kleur per locatie
    return {"count": len(assets_raw), "location_count": len(counts), "locations": locations}


# ── LIVE ophalen via ESI ─────────────────────────────────────────────────────
def _fetch_live(eve_character, token, lite=False):
    """Haal het profiel live via ESI.

    `lite=True` (voor de recruiter-lijst) slaat de zwaarste calls over
    (mail-bodies, killmails, assets, clones, journal, transactions, locatie, schip)
    en haalt alleen wat de lijst nodig heeft: info, wallet, skills, contacts,
    contracts en corp-historie. Dat scheelt tientallen ESI-calls per recruit.
    """
    cid = eve_character.character_id
    access = token.valid_access_token()

    # Basis-calls: altijd nodig (ook voor de lijst)
    jobs = {
        "info": lambda: _pub(f"/characters/{cid}/"),
        "wallet": lambda: _auth(f"/characters/{cid}/wallet/", access),
        "skills": lambda: _auth(f"/characters/{cid}/skills/", access),
        "contacts": lambda: _auth(f"/characters/{cid}/contacts/", access, paged=True),
        "contracts": lambda: _auth(f"/characters/{cid}/contracts/", access, paged=True),
        "hist": lambda: _pub(f"/characters/{cid}/corporationhistory/"),
    }
    # Zware calls: alleen voor de volledige (detail-)scan
    if not lite:
        jobs.update({
            "journal": lambda: _auth(f"/characters/{cid}/wallet/journal/", access),
            "transactions": lambda: _auth(f"/characters/{cid}/wallet/transactions/", access),
            "loc": lambda: _auth(f"/characters/{cid}/location/", access),
            "ship": lambda: _auth(f"/characters/{cid}/ship/", access),
            "mail": lambda: _auth(f"/characters/{cid}/mail/", access),
            "clones": lambda: _auth(f"/characters/{cid}/clones/", access),
            "assets": lambda: _auth(f"/characters/{cid}/assets/", access, paged=True),
            "killmails": lambda: _auth(f"/characters/{cid}/killmails/recent/", access),
        })
    res = _parallel(jobs)
    info = res["info"]
    wallet = res["wallet"]
    skills_raw = res["skills"]
    contacts_raw = res["contacts"]
    contracts_raw = res["contracts"]
    journal_raw = res.get("journal")
    transactions_raw = res.get("transactions")
    loc = res.get("loc")
    ship = res.get("ship")
    hist_raw = res.get("hist") or []
    mail_raw = res.get("mail")
    clones = None if lite else _process_clones(res.get("clones"), access)
    assets = None if lite else _process_assets(
        res.get("assets") if isinstance(res.get("assets"), list) else [], access)
    killmails = [] if lite else _process_killmails(res.get("killmails"), cid)

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
    for coll in (clones, assets):
        if coll:
            locs = list(coll.get("locations") or [])
            if coll.get("home"):
                locs.append(coll["home"])
            for lc in locs:
                ids.update(filter(None, [lc.get("owner_corp_id"), lc.get("system_id")]))
    ids.discard(None)
    name_map = _names(ids)

    # Eigenaar-/systeemnamen aan de clone-/asset-locaties hangen
    for coll in (clones, assets):
        if coll:
            locs = list(coll.get("locations") or [])
            if coll.get("home"):
                locs.append(coll["home"])
            for lc in locs:
                lc["owner_name"] = name_map.get(lc.get("owner_corp_id"))
                lc["system_name"] = name_map.get(lc.get("system_id"))

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
    # Affiliatie van de afzenders (corp/alliance) — om eigen/blauwe afzenders te herkennen
    aff = _affiliations([mm["from_id"] for mm in mails])
    for mm in mails:
        c, a = aff.get(mm["from_id"], (None, None))
        mm["from_corp_id"], mm["from_alliance_id"] = c, a

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
          "standing": c["standing"], "type": c.get("contact_type")} for c in contacts_raw],
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

    # Bio (character description): repr-quirk uitpakken, dan platte tekst + de
    # entity-ids uit in-game showinfo-links (showinfo:<typeID>//<itemID>).
    bio_raw = _decode_bio(info.get("description")) if info else ""
    bio_text = _strip_html(bio_raw)
    bio_ids = [int(m) for m in re.findall(r"showinfo:\d+//(\d+)", bio_raw)]

    owner_main, is_alt, owner_main_id = _owner_info(eve_character)
    return {
        "ok": True,
        "source": "esi",
        "name": eve_character.character_name,
        "corp_id": info.get("corporation_id") if info else eve_character.corporation_id,
        "corp_name": name_map.get(info.get("corporation_id")) if info else eve_character.corporation_name,
        "alliance_id": info.get("alliance_id") if info else eve_character.alliance_id,
        "alliance_name": name_map.get(info.get("alliance_id")) if info and info.get("alliance_id") else eve_character.alliance_name,
        "owner_main": owner_main, "is_alt": is_alt, "owner_main_id": owner_main_id,
        "sec": info.get("security_status") if info else None,
        "age_years": _age_years(info.get("birthday")) if info else None,
        "bio": bio_text,
        "bio_ids": bio_ids,
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
        "clones": clones,
        "assets": assets,
        "killmails": killmails,
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
                             "standing": c.standing,
                             "type": getattr(c.eve_entity, "category", None)})
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

    owner_main, is_alt, owner_main_id = _owner_info(eve_character)
    return {
        "ok": True, "source": "memberaudit",
        "name": eve_character.character_name,
        "corp_id": eve_character.corporation_id, "corp_name": eve_character.corporation_name,
        "alliance_id": eve_character.alliance_id, "alliance_name": eve_character.alliance_name or "",
        "owner_main": owner_main, "is_alt": is_alt, "owner_main_id": owner_main_id,
        "sec": getattr(details, "security_status", None),
        "age_years": _age_years(getattr(details, "birthday", None)),
        "wallet": getattr(wallet, "total", None),
        "total_sp": getattr(sp, "total", None),
        "unallocated_sp": getattr(sp, "unallocated", None),
        "skill_count": skill_count,
        "skill_groups": skill_groups, "risk_skills": risk_skills,
        "contacts": contacts, "contracts": contracts,
        "corp_history": corp_history, "wallet_journal": journal,
        "clones": None, "assets": None, "killmails": [],
        "ship_type_id": None, "ship_type_name": None, "ship_name": None,
        "system_id": None, "location_name": None, "station_name": None, "docked": False,
    }


def _empty(eve_character):
    owner_main, is_alt, owner_main_id = _owner_info(eve_character)
    return {
        "ok": False, "source": None,
        "name": eve_character.character_name,
        "corp_id": eve_character.corporation_id, "corp_name": eve_character.corporation_name,
        "alliance_id": eve_character.alliance_id, "alliance_name": eve_character.alliance_name or "",
        "owner_main": owner_main, "is_alt": is_alt, "owner_main_id": owner_main_id,
        "sec": None, "age_years": None, "wallet": None, "total_sp": None, "unallocated_sp": None, "skill_count": None,
        "skill_groups": [], "risk_skills": [], "contacts": [], "contracts": [],
        "corp_history": [], "wallet_journal": [],
        "clones": None, "assets": None, "killmails": [],
        "ship_type_id": None, "ship_type_name": None, "ship_name": None,
        "system_id": None, "location_name": None, "station_name": None, "docked": False,
    }


def get_profile(eve_character, force=False, lite=False):
    """Canoniek data-dict voor een character. Live via ESI-token, anders MA-fallback.

    `lite=True` levert een lichtgewicht profiel (zonder mails/killmails/assets/clones)
    voor de recruiter-lijst. Een al gecachte volledige scan wordt daarbij hergebruikt.
    """
    cid = eve_character.character_id
    full_key = f"cs_profile_{cid}"
    lite_key = f"cs_profile_lite_{cid}"
    if not force:
        full = cache.get(full_key)
        if full is not None:
            return full  # volledige scan is een superset — prima voor de lijst
        if lite:
            cached = cache.get(lite_key)
            if cached is not None:
                return cached

    profile = None
    token = recruit_token(cid)
    if token:
        try:
            profile = _fetch_live(eve_character, token, lite=lite)
        except Exception as e:
            logger.warning("Character Scan live-fetch faalde voor %s: %s", cid, e)
            profile = None
    if profile is None:
        profile = _fetch_memberaudit(eve_character)  # tijdelijke fallback
    if profile is None:
        profile = _empty(eve_character)

    profile["character_id"] = cid
    cache.set(lite_key if lite else full_key, profile, PROFILE_CACHE_SECONDS)
    return profile
