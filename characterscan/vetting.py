"""
Vetting — beoordeelt een recruit op basis van het canonieke data-dict (esi_fetch)
+ zKillboard.

De vijandenlijst ("enemy set") = dict {entity_id: naam}, primair uit de corp+alliance-
standings (automatisch), met een handmatige aanvulling en een MA-fallback.
"""

import re
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

import requests

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from .esi_fetch import get_profile, ma_character, sov_map

UA = {"User-Agent": "aa-characterscan (local eval)"}

VERDICTS = {
    "bad": {"level": "bad", "label": "VERDACHT", "css": "danger", "icon": "⛔"},
    "warn": {"level": "warn", "label": "CONTROLEER", "css": "warning", "icon": "⚠"},
    "ok": {"level": "ok", "label": "VEILIG", "css": "success", "icon": "✓"},
}

CORP_CONTACTS_SCOPE = "esi-corporations.read_contacts.v1"
ALLIANCE_CONTACTS_SCOPE = "esi-alliances.read_contacts.v1"

# Verdachte termen in mailinhoud — bewust conservatief (échte spy-signalen).
# Generieke fleet-woorden (intel, titan, supers, comms-apps) geven te veel
# false positives op normale corp-mail en zitten hier daarom NIET in.
SUSPECT_TERMS = [
    # Spionage / infiltratie (EN + NL)
    (re.compile(r"\bspy(ing|s)?\b", re.I), "spy"),
    (re.compile(r"\bspai\b", re.I), "spai"),
    (re.compile(r"\bspion", re.I), "spion"),                       # NL: spion/spionage
    (re.compile(r"spy[- ]?alt", re.I), "spy alt"),
    (re.compile(r"double[- ]?agent|dubbelspion", re.I), "double agent"),
    (re.compile(r"\binformant", re.I), "informant"),
    (re.compile(r"sleeper[- ]?agent", re.I), "sleeper agent"),
    (re.compile(r"undercover", re.I), "undercover"),
    (re.compile(r"infiltrat|\binfiltrant", re.I), "infiltrate"),
    (re.compile(r"\bmole\b|\bmol\b", re.I), "mole"),               # NL: mol
    (re.compile(r"honeypot", re.I), "honeypot"),
    # Verraad / overlopen / sabotage
    (re.compile(r"\bawox", re.I), "awox"),
    (re.compile(r"turncoat", re.I), "turncoat"),
    (re.compile(r"backstab", re.I), "backstab"),
    (re.compile(r"\bbetray", re.I), "betray"),
    (re.compile(r"verrad|verraad", re.I), "verraad"),             # NL: verrader/verraden/verraad
    (re.compile(r"\bdefector\b|\bdefect(ing|ed)?\s+to\b", re.I), "defector"),
    (re.compile(r"overlop|overgelop", re.I), "overlopen"),        # NL: overlopen/overgelopen
    (re.compile(r"sabot(age|eur|eren)", re.I), "sabotage"),
    # Diefstal / scam / omkoping
    (re.compile(r"\bheist", re.I), "heist"),
    (re.compile(r"\bthie(f|ves)\b|\btheft\b", re.I), "theft"),
    (re.compile(r"embezzl", re.I), "embezzle"),
    (re.compile(r"\bscam", re.I), "scam"),
    (re.compile(r"\bbrib(e|ing|ed|ery)", re.I), "bribe"),
    (re.compile(r"omkop|omgekocht", re.I), "omkoop"),            # NL: omkopen/omgekocht
    (re.compile(r"\bransom", re.I), "ransom"),
    # Intel lekken / hunten
    (re.compile(r"\bleak(s|ing|ed)?\b", re.I), "leak"),
    (re.compile(r"lekken|gelekt|lekte", re.I), "lekken"),        # NL: lekken/gelekt
    (re.compile(r"feed(ing)?\s+intel|intel\s+feed", re.I), "feed intel"),
    (re.compile(r"watch[- ]?list", re.I), "watchlist"),
    (re.compile(r"locator\s+agent", re.I), "locator"),
    # Cyno-hinderlaag
    (re.compile(r"hot[- ]?drop", re.I), "hotdrop"),
    (re.compile(r"cyno\s*alt", re.I), "cyno alt"),
]
URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)

# Vertrouwde domeinen: links hierheen tellen niet als 'externe link'.
# Uitbreidbaar via settings.CHARACTERSCAN_TRUSTED_LINK_DOMAINS.
DEFAULT_TRUSTED_DOMAINS = [
    "insidiousevil.org", "dutchlegionsdashboard.eu",
    "eveonline.com", "zkillboard.com", "evewho.com",
    "dotlan.evemaps.net", "everef.net",
]


def trusted_domains():
    extra = getattr(settings, "CHARACTERSCAN_TRUSTED_LINK_DOMAINS", None) or []
    return set(DEFAULT_TRUSTED_DOMAINS) | {str(d).lower().strip().lstrip(".") for d in extra}


def _url_host(url):
    m = re.match(r"https?://([^/\s:]+)", url, re.I)
    return m.group(1).lower() if m else ""


def _untrusted_link(txt, trusted):
    """True als er een link naar een niet-vertrouwd domein in de tekst staat."""
    for u in URL_RE.findall(txt or ""):
        host = _url_host(u)
        if host and not any(host == d or host.endswith("." + d) for d in trusted):
            return True
    return False


def _scan_text(txt, trusted=None):
    """Verdachte termen + niet-vertrouwde externe links in een tekst → set labels."""
    hits = set()
    for rx, label in SUSPECT_TERMS:
        if rx.search(txt or ""):
            hits.add(label)
    if _untrusted_link(txt, trusted if trusted is not None else trusted_domains()):
        hits.add("externe link")
    return hits


def mail_is_friendly(mail, profile, standing_by_id=None):
    """Mail van eigen corp/alliance of een blauwe (positieve standing) afzender?"""
    fid = mail.get("from_id")
    if standing_by_id is None:
        standing_by_id = {c.get("id"): c.get("standing") for c in profile.get("contacts", [])}
    if (standing_by_id.get(fid) or 0) > 0:
        return True
    rc, ra = profile.get("corp_id"), profile.get("alliance_id")
    if mail.get("from_corp_id") and mail.get("from_corp_id") == rc:
        return True
    if mail.get("from_alliance_id") and ra and mail.get("from_alliance_id") == ra:
        return True
    return False


# ── Datum/ISK-helpers ────────────────────────────────────────────────────────
def _parse_date(v):
    """datetime of ISO-string → aware datetime (of None)."""
    if not v:
        return None
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    if timezone.is_naive(v):
        v = v.replace(tzinfo=dt_timezone.utc)
    return v


def _fmt_date(v):
    d = _parse_date(v)
    return d.strftime("%d-%m-%Y") if d else "?"


def _fmt_isk(v):
    v = float(v)
    a, sign = abs(v), "-" if v < 0 else ""
    if a >= 1e12:
        return f"{sign}{a/1e12:.2f}T"
    if a >= 1e9:
        return f"{sign}{a/1e9:.2f}B"
    if a >= 1e6:
        return f"{sign}{a/1e6:.1f}M"
    if a >= 1e3:
        return f"{sign}{a/1e3:.0f}K"
    return f"{sign}{a:.0f}"


# ── Gedeelde ESI-helpers (voor de org-standings) ─────────────────────────────
def _esi_paged(path, token_str):
    out, page = [], 1
    while True:
        try:
            r = requests.get(
                f"https://esi.evetech.net/latest{path}?datasource=tranquility&page={page}",
                headers={**UA, "Authorization": f"Bearer {token_str}"}, timeout=8,
            )
        except Exception:
            break
        if not r.ok:
            break
        chunk = r.json() or []
        out.extend(chunk)
        if page >= int(r.headers.get("X-Pages", 1) or 1) or not chunk:
            break
        page += 1
    return out


def _resolve_names(ids):
    names, ids = {}, [i for i in ids if i]
    for i in range(0, len(ids), 1000):
        try:
            r = requests.post(
                "https://esi.evetech.net/latest/universe/names/?datasource=tranquility",
                json=ids[i:i + 1000], headers=UA, timeout=8,
            )
            if r.ok:
                for x in r.json():
                    names[x["id"]] = x["name"]
        except Exception:
            pass
    return names


def _is_npc_corp(corp_id):
    """Player-corps hebben een id >= 98.000.000; daaronder = NPC-corp."""
    return corp_id is None or corp_id < 98_000_000


def corp_alliance(corp_id):
    """Huidige alliance-id van een player-corp (publieke ESI, gecached). None = geen."""
    if _is_npc_corp(corp_id):
        return None
    key = f"cs_corp_alliance_{corp_id}"
    cached = cache.get(key)
    if cached is not None:
        return cached or None
    alliance_id = None
    try:
        r = requests.get(
            f"https://esi.evetech.net/latest/corporations/{corp_id}/?datasource=tranquility",
            headers=UA, timeout=6,
        )
        if r.ok:
            alliance_id = r.json().get("alliance_id")
    except Exception:
        pass
    cache.set(key, alliance_id or 0, 7 * 86400)
    return alliance_id


# ── Vijandenlijst (org corp/alliance-standings) ──────────────────────────────
def standings_token_exists():
    try:
        from esi.models import Token
        return Token.objects.filter(
            scopes__name__in=[CORP_CONTACTS_SCOPE, ALLIANCE_CONTACTS_SCOPE]
        ).exists()
    except Exception:
        return False


def org_enemy_ids(force=False):
    """Dict {id: naam} van rode entiteiten uit de CORP + ALLIANCE-standings (automatisch)."""
    key = "cs_org_enemies"
    if not force:
        cached = cache.get(key)
        if cached is not None:
            return dict(cached)

    from esi.models import Token
    from allianceauth.eveonline.models import EveCharacter

    enemies = set()
    corp_override = getattr(settings, "CHARACTERSCAN_STANDINGS_CORP_ID", None)
    ally_override = getattr(settings, "CHARACTERSCAN_STANDINGS_ALLIANCE_ID", None)

    def _corp(cid):
        ec = EveCharacter.objects.filter(character_id=cid).first()
        return ec.corporation_id if ec else None

    def _ally(cid):
        ec = EveCharacter.objects.filter(character_id=cid).first()
        return ec.alliance_id if ec else None

    for t in Token.objects.filter(scopes__name=CORP_CONTACTS_SCOPE):
        corp_id = corp_override or _corp(t.character_id)
        if not corp_id or corp_id < 98_000_000:
            continue
        try:
            rows = _esi_paged(f"/corporations/{corp_id}/contacts/", t.valid_access_token())
        except Exception:
            rows = []
        if rows:
            enemies.update(c["contact_id"] for c in rows if c.get("standing", 0) < 0)
            break

    for t in Token.objects.filter(scopes__name=ALLIANCE_CONTACTS_SCOPE):
        alliance_id = ally_override or _ally(t.character_id)
        if not alliance_id:
            continue
        try:
            rows = _esi_paged(f"/alliances/{alliance_id}/contacts/", t.valid_access_token())
        except Exception:
            rows = []
        if rows:
            enemies.update(c["contact_id"] for c in rows if c.get("standing", 0) < 0)
            break

    names = _resolve_names(enemies) if enemies else {}
    result = {eid: names.get(eid, f"#{eid}") for eid in enemies}
    cache.set(key, result, 3600)
    return result


def enemy_set(recruiter_eve_character=None):
    """Dict {id: naam} vijandige entiteiten — automatisch uit corp/alliance-standings."""
    enemy = dict(org_enemy_ids())

    for eid in getattr(settings, "CHARACTERSCAN_ENEMY_IDS", None) or []:
        try:
            enemy.setdefault(int(eid), f"#{int(eid)}")
        except (TypeError, ValueError):
            pass

    if not enemy and recruiter_eve_character:  # fallback: persoonlijke MA-contacts
        ma = ma_character(recruiter_eve_character)
        if ma:
            try:
                for c in ma.contacts.select_related("eve_entity").filter(standing__lt=0):
                    enemy[c.eve_entity_id] = getattr(c.eve_entity, "name", f"#{c.eve_entity_id}")
            except Exception:
                pass
    return enemy


# ── Analyse op het canonieke data-dict ───────────────────────────────────────
def _location_enemy(loc, enemy, sov):
    """→ (is_vijand, reden) voor een clone-/asset-locatie (structure-eigenaar of sov)."""
    if not loc or not enemy:
        return False, None
    owner = loc.get("owner_corp_id")
    sys_id = loc.get("system_id")
    if owner and owner in enemy:
        return True, f"structure van {enemy[owner]}"
    if owner:
        aid = corp_alliance(owner)
        if aid and aid in enemy:
            return True, f"structure-alliance {enemy[aid]}"
    if sys_id and sov.get(sys_id) in enemy:
        return True, f"vijandelijke sov ({enemy[sov[sys_id]]})"
    return False, None


def enemy_hits(profile, enemy):
    """Bad-signalen tegen de vijandenlijst (corp + alliance, huidig + historisch)."""
    hits = {"current": [], "history": [], "blue": [], "contracts": []}
    if not enemy:
        return hits

    if profile.get("corp_id") in enemy:
        hits["current"].append(f"corp {enemy[profile['corp_id']]}")
    if profile.get("alliance_id") and profile["alliance_id"] in enemy:
        hits["current"].append(f"alliance {enemy[profile['alliance_id']]}")

    seen = set()
    for h in profile.get("corp_history", []):
        cid = h.get("corp_id")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        if cid in enemy:
            hits["history"].append(h.get("corp_name", str(cid)))
        elif h.get("alliance_id") and h["alliance_id"] in enemy:
            hits["history"].append(f"{h.get('corp_name', cid)} (alliance {enemy[h['alliance_id']]})")

    for c in profile.get("contacts", []):
        if c.get("standing", 0) > 0 and c.get("id") in enemy:
            hits["blue"].append(c.get("name", str(c.get("id"))))

    for c in profile.get("contracts", []):
        parties = [c.get("issuer_id"), c.get("issuer_corp_id"), c.get("assignee_id"), c.get("acceptor_id")]
        if any(p in enemy for p in parties if p):
            hits["contracts"].append(c.get("title") or c.get("type") or "contract")

    return hits


def wallet_flags(profile, enemy):
    """Scan de wallet-journal op grote/verdachte ISK-bewegingen. → (flags, bad, warn)."""
    threshold = float(getattr(settings, "CHARACTERSCAN_WALLET_ALERT_ISK", 1_000_000_000))
    flags, bad, warn = [], 0, 0
    journal = profile.get("wallet_journal", [])
    if not journal:
        return flags, bad, warn

    def cp(j):
        # tegenpartij = de partij die niet het character zelf is
        for pid, name in ((j.get("first_party_id"), j.get("first_party_name")),
                          (j.get("second_party_id"), j.get("second_party_name"))):
            if pid and pid != profile.get("character_id"):
                return name or f"#{pid}"
        return "?"

    big_don = [j for j in journal
               if j.get("ref_type") == "player_donation" and j.get("amount") and abs(float(j["amount"])) >= threshold]
    incoming = [j for j in big_don if float(j["amount"]) > 0]
    outgoing = [j for j in big_don if float(j["amount"]) < 0]
    if incoming:
        warn += 1
        items = [f"{_fmt_isk(j['amount'])} ISK van <b>{cp(j)}</b> ({_fmt_date(j.get('date'))})" for j in incoming[:5]]
        flags.append({"level": "warn", "css": "warning", "label": "ISK-donatie (in)", "value": "; ".join(items)})
    if outgoing:
        items = [f"{_fmt_isk(abs(float(j['amount'])))} ISK naar <b>{cp(j)}</b> ({_fmt_date(j.get('date'))})" for j in outgoing[:5]]
        flags.append({"level": "info", "css": "secondary", "label": "ISK-donatie (uit)", "value": "; ".join(items)})

    if enemy:
        hits = []
        for j in journal:
            for pid, name in ((j.get("first_party_id"), j.get("first_party_name")),
                              (j.get("second_party_id"), j.get("second_party_name"))):
                if pid and pid in enemy and j.get("amount"):
                    hits.append(f"{_fmt_isk(j['amount'])} ISK ↔ <b>{enemy[pid]}</b> ({(j.get('ref_type') or '').replace('_', ' ')})")
                    break
        if hits:
            bad += 1
            flags.append({"level": "bad", "css": "danger",
                          "label": "ISK met vijand", "value": "; ".join(list(dict.fromkeys(hits))[:6])})

    biggest = max(journal, key=lambda j: abs(float(j.get("amount") or 0)))
    if biggest.get("amount") and abs(float(biggest["amount"])) >= threshold and not big_don:
        flags.append({"level": "info", "css": "secondary", "label": "Grootste transactie",
                      "value": f"{_fmt_isk(biggest['amount'])} ISK "
                               f"({(biggest.get('ref_type') or '').replace('_', ' ')}, {_fmt_date(biggest.get('date'))})"})
    return flags, bad, warn


def _zkill(character_id):
    try:
        r = requests.get(f"https://zkillboard.com/api/stats/characterID/{character_id}/",
                         headers=UA, timeout=8)
        if r.ok:
            return r.json()
    except Exception:
        pass
    return None


def quick_verdict(eve_character, enemy):
    """Lichte verdict voor de lijst (geen zKill)."""
    profile = get_profile(eve_character)
    if not profile.get("ok"):
        return {"level": "ok", "label": "?", "css": "secondary", "icon": "·"}
    if any(enemy_hits(profile, enemy).values()):
        return VERDICTS["bad"]
    if profile.get("risk_skills"):
        return VERDICTS["warn"]
    return VERDICTS["ok"]


def assess(eve_character, enemy, with_zkill=True):
    """Volledige vetting voor de detailpagina: verdict + lijst van flags."""
    profile = get_profile(eve_character)
    flags = []
    if not profile.get("ok"):
        return {"verdict": {"level": "ok", "label": "GEEN DATA", "css": "secondary", "icon": "·"},
                "flags": [{"level": "info", "css": "secondary", "text":
                           "Geen data — recruit heeft nog geen character gekoppeld via CharLink."}]}

    bad = warn = 0

    def flag(level, css, label, value):
        flags.append({"level": level, "css": css, "label": label, "value": value})

    # Risk-skills
    risk = profile.get("risk_skills", [])
    if risk:
        warn += 1
        by_label = {}
        for s in risk:
            g = by_label.setdefault(s["label"], {"max": 0, "detail": []})
            g["max"] = max(g["max"], s["level"])
            g["detail"].append(f"{s['name']} L{s['level']}")
        # compact: categorie + hoogste level, volledige skills in de tooltip
        txt = " · ".join(
            f'<b title="{", ".join(g["detail"])}">{k}</b> L{g["max"]}' for k, g in by_label.items()
        )
        flag("warn", "warning", "Risk-skills", txt)
    else:
        flag("ok", "success", "Risk-skills", "Geen cyno/covert/blops/recon/jump")

    # Leeftijd
    age = profile.get("age_years")
    if age is not None:
        if age < 0.1:
            bad += 1
            flag("bad", "danger", "Leeftijd", f"<b>Zeer jong</b> ({int(age*365)} dagen) — mogelijk wegwerp-/spy-alt")
        elif age < 0.5:
            warn += 1
            flag("warn", "warning", "Leeftijd", f"Jong ({age} jaar) — extra check waard")
        else:
            flag("ok", "success", "Leeftijd", f"{age} jaar")

    # Corp-hopping (player-corps; NPC al gefilterd in de fetch)
    hist = sorted(
        [h for h in profile.get("corp_history", []) if _parse_date(h.get("start"))],
        key=lambda h: _parse_date(h["start"]),
    )
    if hist:
        year_ago = timezone.now() - timedelta(days=365)
        recent = [h for h in hist if _parse_date(h["start"]) > year_ago]
        short = 0
        for i, h in enumerate(hist):
            end = _parse_date(hist[i + 1]["start"]) if i + 1 < len(hist) else timezone.now()
            if (end - _parse_date(h["start"])).days < 14:
                short += 1
        if len(recent) >= 5:
            warn += 1
            flag("warn", "warning", "Werkgevershistorie", f"<b>Corp-hopping</b>: {len(recent)} player-corps in 12 mnd")
        elif short >= 3:
            warn += 1
            flag("warn", "warning", "Werkgevershistorie", f"<b>Korte stints</b>: {short}× een player-corp binnen 14 dagen verlaten")
        else:
            flag("ok", "success", "Werkgevershistorie", f"Stabiel ({len(hist)} player-corps)")

    # Sec + SP
    sec = profile.get("sec")
    if sec is not None and sec < -2:
        warn += 1
        flag("warn", "warning", "Security status", f"{sec:.1f} (negatief)")
    sp = profile.get("total_sp")
    if sp is not None and sp < 5_000_000:
        flag("info", "secondary", "Skillpoints", f"{sp/1e6:.1f}M — relatief nieuw character")

    # Wallet-scan
    w_flags, w_bad, w_warn = wallet_flags(profile, enemy)
    bad += w_bad
    warn += w_warn
    flags.extend(w_flags)

    # Skill-injector-scan: veel Large Skill Injectors = snel opgekrikt (spy/farm-signaal)
    inj = profile.get("skill_injectors") or {}
    if inj.get("has_transactions"):
        threshold = int(getattr(settings, "CHARACTERSCAN_INJECTOR_ALERT", 5))
        large, small, total = inj.get("large", 0), inj.get("small", 0), inj.get("total", 0)
        span = ""
        if inj.get("first") and inj.get("last"):
            span = f", {_fmt_date(inj['first'])}–{_fmt_date(inj['last'])}"
        if large >= threshold:
            warn += 1
            flag("warn", "warning", "Skill injectors",
                 f"<b>Veel Large Skill Injectors</b>: {large}× gekocht"
                 f" (~{_fmt_isk(inj.get('isk', 0))} ISK{span}) — snel opgekrikt character")
        elif total > 0:
            parts = []
            if large:
                parts.append(f"{large}× Large")
            if small:
                parts.append(f"{small}× Small")
            flag("info", "secondary", "Skill injectors", ", ".join(parts) + f" gekocht{span}")
        else:
            flag("ok", "success", "Skill injectors", "Geen gekocht (market-transacties)")

    # Onverdeelde SP: net geïnjecteerde of in de EVE-store gekochte SP staat 'los'
    # tot je 'm toewijst — vangt injectors/store-SP die niet via de markt liepen.
    unallocated = profile.get("unallocated_sp") or 0
    if unallocated >= 500_000:  # ~1 injector of store-SP-pakket dat nog niet is verdeeld
        warn += 1
        flag("warn", "warning", "Onverdeelde SP",
             f"<b>{unallocated/1e6:.1f}M onverdeelde SP</b> — recent geïnjecteerd of in de "
             f"EVE-store gekocht, nog niet toegewezen")
    elif unallocated > 0:
        flag("info", "secondary", "Onverdeelde SP",
             f"{unallocated/1e3:.0f}k (klein — bijv. login-/event-beloningen)")

    # SP versus leeftijd: veel meer SP dan natuurlijk trainbaar → geïnjecteerd.
    # Getraind + onverdeeld samen, want beide zijn 'verkregen' SP (ook store-SP).
    sp = profile.get("total_sp")
    age = profile.get("age_years")
    if sp and age and age >= 0.05:
        effective = sp + unallocated
        max_natural = age * 24_000_000  # ~max SP/jaar (+5 implants, perfecte remaps)
        if effective > max_natural * 1.2:
            est = max(1, int((effective - max_natural) / 400_000))  # ~400k SP per Large injector
            warn += 1
            flag("warn", "warning", "SP vs. leeftijd",
                 f"<b>{effective/1e6:.0f}M SP</b> in {age} jaar — meer dan natuurlijk mogelijk "
                 f"(~{est} injectors geschat)")

    # Mail-scan: verdachte termen/externe links (van niet-blauwe afzenders)
    #            + mailcontact met vijanden
    mails = profile.get("mails", [])
    if mails:
        standing_by_id = {c.get("id"): c.get("standing") for c in profile.get("contacts", [])}
        trusted = trusted_domains()
        scanned = [m for m in mails if not mail_is_friendly(m, profile, standing_by_id)]
        all_terms = set()
        for m in scanned:
            all_terms |= _scan_text((m.get("subject", "") + " " + m.get("body", "")), trusted)
        if all_terms:
            warn += 1
            flag("warn", "warning", "Verdachte mails", ", ".join(sorted(all_terms)))
        else:
            skipped = len(mails) - len(scanned)
            extra = f" ({skipped} van eigen/blauwe afzender overgeslagen)" if skipped else ""
            flag("ok", "success", "Mails", "Geen verdachte termen of externe links" + extra)
        if enemy:
            hits = []
            for m in mails:
                parties = [m.get("from_id")] + (m.get("recipient_ids") or [])
                if any(p in enemy for p in parties if p):
                    who = m.get("from_name") or "?"
                    hits.append(f"{m.get('subject') or '(geen onderwerp)'} — {who}")
            if hits:
                bad += 1
                flag("bad", "danger", "Mail met vijand", "; ".join(list(dict.fromkeys(hits))[:5]))
            else:
                flag("ok", "success", "Mail met vijand", "Geen mailcontact met bekende vijanden")

    # Jump clones + assets in vijandelijk gebied (structure-eigenaar of sov)
    clones = profile.get("clones")
    assets = profile.get("assets")
    if clones or assets:
        sov = sov_map()
        if clones:
            home = clones.get("home")
            locs = list(clones.get("locations") or []) + ([home] if home else [])
            hits = []
            for lc in locs:
                is_en, reason = _location_enemy(lc, enemy, sov)
                if is_en:
                    hits.append(f"{lc.get('name') or lc.get('system_name') or '?'} — {reason}")
            if enemy and hits:
                bad += 1
                flag("bad", "danger", "Clone in vijandgebied", "; ".join(list(dict.fromkeys(hits))[:5]))
            else:
                flag("ok", "success", "Jump clones",
                     f"{clones.get('jump_count', 0)} clones — geen in vijandgebied")
        if assets:
            hits = []
            for lc in assets.get("locations", []):
                is_en, reason = _location_enemy(lc, enemy, sov)
                if is_en:
                    hits.append(f"{lc.get('name') or lc.get('system_name') or '?'} "
                                f"({lc.get('item_count')} items) — {reason}")
            if enemy and hits:
                bad += 1
                flag("bad", "danger", "Assets in vijandgebied", "; ".join(list(dict.fromkeys(hits))[:5]))
            else:
                flag("ok", "success", "Assets",
                     f"{assets.get('count', 0)} items in {assets.get('location_count', 0)} locaties")

    # zKillboard: stats + associates (met wie vliegt hij) + schip-/activiteitsprofiel
    if with_zkill:
        zk = _zkill(eve_character.character_id)
        if not zk:
            flag("info", "secondary", "zKillboard", "kon niet geladen worden")
        else:
            destroyed = zk.get("shipsDestroyed", 0) or 0
            lost = zk.get("shipsLost", 0) or 0
            danger = zk.get("dangerRatio", 0)
            gang = zk.get("gangRatio", 0)
            solo = zk.get("soloKills", 0) or 0
            if destroyed + lost == 0:
                flag("info", "secondary", "zKillboard", "Geen PvP-historie")
            else:
                flag("info", "secondary", "zKillboard",
                     f"<b>{destroyed}</b> kills / <b>{lost}</b> losses · danger {danger}% · gang {gang}% · {solo} solo")

            tops = {t.get("type"): (t.get("values") or []) for t in zk.get("topLists", [])}
            # Associates: top-alliances/corps op zijn killmails — vijandig?
            assoc_hits = []
            for typ, idk, namek in (("alliance", "allianceID", "allianceName"),
                                    ("corporation", "corporationID", "corporationName")):
                for v in tops.get(typ, [])[:5]:
                    if v.get(idk) in enemy:
                        assoc_hits.append(f"{v.get(namek)} ({v.get('kills')}×)")
            if enemy and assoc_hits:
                bad += 1
                flag("bad", "danger", "Vliegt met vijand", ", ".join(dict.fromkeys(assoc_hits)))
            elif tops.get("alliance") or tops.get("corporation"):
                top = (tops.get("alliance") or tops.get("corporation"))[0]
                who = top.get("allianceName") or top.get("corporationName") or "?"
                flag("ok", "success", "Vliegt met (top)", f"{who} ({top.get('kills')}×)")
            # Schipprofiel
            ships = tops.get("shipType", [])[:3]
            if ships:
                flag("info", "secondary", "Meest gevlogen schepen",
                     " · ".join(f"{v.get('shipName') or v.get('typeName')} ({v.get('kills')}×)" for v in ships))
            systems = tops.get("solarSystem", [])[:3]
            if systems:
                flag("info", "secondary", "Actiefste systemen",
                     " · ".join(f"{v.get('solarSystemName')} ({v.get('kills')}×)" for v in systems))
            # Recente activiteit / inactiviteit
            recent = ((zk.get("activepvp") or {}).get("kills") or {}).get("count", 0)
            if destroyed + lost > 0 and not recent:
                warn += 1
                flag("warn", "warning", "Activiteit", "Geen recente PvP — mogelijk inactief/slapend")
            elif recent:
                flag("ok", "success", "Recente activiteit", f"{recent} kills recent")

    # Vijand-checks
    if not enemy:
        flag("info", "secondary", "Vijandenlijst", "Niet geladen — checks overgeslagen")
    else:
        hits = enemy_hits(profile, enemy)
        if hits["current"]:
            bad += 1
            flag("bad", "danger", "Huidige corp/alliance", "Vijandig: " + ", ".join(hits["current"]))
        if hits["history"]:
            bad += 1
            flag("bad", "danger", "Vijand in historie", ", ".join(dict.fromkeys(hits["history"])))
        else:
            flag("ok", "success", "Vijand in historie", "Geen (corp + alliance gecheckt)")
        if hits["blue"]:
            bad += 1
            flag("bad", "danger", "Vijand als vriend gemarkeerd", ", ".join(dict.fromkeys(hits["blue"])))
        else:
            flag("ok", "success", "Vijand als vriend gemarkeerd", "Geen")
        if hits["contracts"]:
            bad += 1
            flag("bad", "danger", "Contracten met vijand", ", ".join(dict.fromkeys(hits["contracts"])))
        else:
            flag("ok", "success", "Contracten met vijand", "Geen")

    verdict = VERDICTS["bad"] if bad else VERDICTS["warn"] if warn else VERDICTS["ok"]
    rank = {"bad": 0, "warn": 1, "ok": 2, "info": 3}
    flags.sort(key=lambda f: rank.get(f["level"], 3))  # ernst bovenaan (stabiel)
    counts = {lvl: sum(1 for f in flags if f["level"] == lvl) for lvl in ("bad", "warn", "ok", "info")}
    return {"verdict": verdict, "flags": flags, "counts": counts}
