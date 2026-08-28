"""
Onboarding-status — is een (nieuw) lid netjes door het systeem gekomen?

De vraag "heeft deze recruit alles afgerond?" is een **account**-vraag, niet een
character-vraag: state, groepen, Discord en TeamSpeak hangen aan het AA-account,
niet aan één EveCharacter. Daarom levert dit module één rij per account.

Bronnen (alles soft: ontbreekt een plugin, dan vervalt die stap in plaats van te
klappen):

* **AA zelf** — main character, state, groepen
* **django-esi** — heeft elk character van het account een recruit-token, en is
  dat token nog te vernieuwen (ingetrokken tokens verliezen hun refresh_token)
* **AA-services** — Discord / TeamSpeak (alleen als de service geïnstalleerd is)
* **Onboarding Checklist-plugin** — de clone-stappen die het lid zélf op z'n
  dashboard ziet; we hergebruiken die logica in plaats van 'm na te bouwen
* **Member Audit** — character geregistreerd en geen falende update-secties

Een stap met ``done=None`` is "niet van toepassing" (plugin/service ontbreekt) en
telt niet mee in de voortgang.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

from allianceauth.eveonline.models import EveCharacter

from .esi_fetch import CS_RECRUIT_SCOPES
from .models import Recruit, Settings

# Scope waarop we "dit character is gekoppeld" afmeten. Zelfde als esi_fetch
# gebruikt om z'n profiel op te halen — is die er niet, dan kan Character Scan
# het character sowieso niet scannen.
LINK_SCOPE = "esi-skills.read_skills.v1"

CACHE_SECONDS = 600  # 10 min; de "Ververs"-knop omzeilt dit

DEFAULT_NEW_DAYS = 30

# Stappen die de Onboarding Checklist-plugin ook heeft, maar die wij zelf al
# controleren — de tokens zelfs strenger, want over álle characters van het
# account in plaats van alleen de main. Zonder deze filter krijg je dubbele
# kolommen ("Discord" naast "Link Discord account").
_OBC_SKIP = {
    "Register main character",
    "Link character (ESI)",
    "Link Discord account",
    "Link TeamSpeak",
}


def _step(key, label, done, note="", icon=""):
    return {"key": key, "label": label, "done": done, "note": note, "icon": icon}


# ── Losse checks ─────────────────────────────────────────────────────────────
def _state_step(user):
    """Staat het account op de verwachte state (standaard: iets anders dan Guest)?"""
    state = getattr(getattr(user, "profile", None), "state", None)
    name = getattr(state, "name", "") or ""
    wanted = (Settings.load().onboarding_state or "").strip()
    if wanted:
        done = name.lower() == wanted.lower()
        note = name or "geen state"
    else:
        done = bool(name) and name.lower() != "guest"
        note = name or "geen state"
    return _step("state", "State", done, note, "🏷")


def _groups_step(user):
    """Zit het account in de vereiste groep(en), of anders in minstens één groep?"""
    names = set(user.groups.values_list("name", flat=True))
    required = Settings.load().onboarding_group_list()
    if required:
        missing = [g for g in required if g not in names]
        done = not missing
        note = ", ".join(sorted(names)) if done else "mist: " + ", ".join(missing)
    else:
        done = bool(names)
        note = ", ".join(sorted(names)) if names else "geen groepen"
    return _step("groups", "Groepen", done, note, "👥")


def _token_steps(user, chars):
    """Twee stappen: is élk character gekoppeld, en zijn die tokens nog bruikbaar?

    "Bruikbaar" = er is nog een refresh_token. Trekt een speler z'n toestemming
    in op de EVE-site, dan blijft de rij staan maar verdwijnt de refresh — een
    echte call doen we hier bewust niet (te traag voor een lijst).
    """
    try:
        from esi.models import Token
    except Exception:  # noqa: BLE001 — django-esi hoort er te zijn, maar toch
        return [_step("tokens", "Characters gekoppeld", None, "django-esi ontbreekt", "🔑")]

    char_ids = [c.character_id for c in chars]
    tokens = list(
        Token.objects.filter(character_id__in=char_ids, scopes__name=LINK_SCOPE)
        .distinct()
        .only("character_id", "refresh_token")
    ) if char_ids else []

    linked_ids = {t.character_id for t in tokens}
    n_linked, n_total = len(linked_ids), len(char_ids)
    unlinked = [c.character_name for c in chars if c.character_id not in linked_ids]

    refreshable = {t.character_id for t in tokens if t.refresh_token}
    dead = sorted({
        c.character_name for c in chars
        if c.character_id in linked_ids and c.character_id not in refreshable
    })

    return [
        _step(
            "tokens", "Characters gekoppeld",
            n_total > 0 and n_linked == n_total,
            f"{n_linked}/{n_total}" + (" — mist: " + ", ".join(sorted(unlinked)) if unlinked else ""),
            "🔑",
        ),
        _step(
            "tokens_ok", "Tokens geldig",
            None if not linked_ids else not dead,
            "ingetrokken: " + ", ".join(dead) if dead else f"{len(refreshable)} bruikbaar",
            "♻",
        ),
    ]


def _service_steps(user):
    """Discord/TeamSpeak — alleen als de betreffende AA-service geïnstalleerd is."""
    steps = []
    try:
        from allianceauth.services.modules.discord.models import DiscordUser
        du = DiscordUser.objects.filter(user=user).first()
        steps.append(_step("discord", "Discord", bool(du),
                           du.username or str(du.uid) if du else "niet gekoppeld", "💬"))
    except Exception:  # noqa: BLE001 — service niet geïnstalleerd
        pass
    try:
        from allianceauth.services.modules.teamspeak3.models import Teamspeak3User
        ts = Teamspeak3User.objects.filter(user=user).first()
        steps.append(_step("teamspeak", "TeamSpeak", bool(ts),
                           ts.uid if ts else "niet gekoppeld", "🎧"))
    except Exception:  # noqa: BLE001
        pass
    return steps


def _checklist_steps(user):
    """De stappen uit de Onboarding Checklist-plugin (clones e.d.), indien aanwezig.

    We hergebruiken bewust die plugin z'n eigen logica, zodat de recruiter exact
    ziet wat het lid op z'n dashboard ziet — één waarheid, geen tweede kopie.
    """
    try:
        from onboardingchecklist.checklist import checklist
    except Exception:  # noqa: BLE001 — plugin niet geïnstalleerd
        return []
    try:
        data = checklist(user)
    except Exception:  # noqa: BLE001 — plugin-fout mag het overzicht niet slopen
        return []

    steps = []
    for s in data.get("steps", []):
        name = s.get("name") or ""
        if name in _OBC_SKIP:
            continue
        note = s.get("note") or ""
        subs = [x for x in (s.get("sub") or []) if not x.get("done")]
        if subs and not s.get("done"):
            note = note or ("mist: " + ", ".join(x["name"] for x in subs))
        steps.append(_step("obc_" + name.lower().replace(" ", "_"), name,
                           bool(s.get("done")), note, "🧬"))
    return steps


def _memberaudit_step(chars):
    """Character geregistreerd in Member Audit en zonder falende update-secties."""
    try:
        from memberaudit.models import Character as MaCharacter
        from memberaudit.models import CharacterUpdateStatus
    except Exception:  # noqa: BLE001 — MA niet (meer) geïnstalleerd
        return None

    ma_chars = list(MaCharacter.objects.filter(eve_character__in=chars)
                    .select_related("eve_character"))
    n_reg, n_total = len(ma_chars), len(chars)
    if n_reg < n_total:
        missing = {c.character_name for c in chars} - {
            m.eve_character.character_name for m in ma_chars}
        return _step("memberaudit", "Member Audit", False,
                     f"{n_reg}/{n_total} — mist: " + ", ".join(sorted(missing)), "📊")

    failing = list(
        CharacterUpdateStatus.objects
        .filter(character__in=ma_chars, is_success=False)
        .values_list("section", flat=True)
    )
    if failing:
        return _step("memberaudit", "Member Audit", False,
                     "falende secties: " + ", ".join(sorted(set(failing))), "📊")
    return _step("memberaudit", "Member Audit", True, f"{n_reg}/{n_total} compleet", "📊")


# ── Samengesteld ─────────────────────────────────────────────────────────────
def account_status(user, force=False):
    """Alle onboarding-stappen + voortgang voor één AA-account."""
    key = f"cs_onb_{user.pk}"
    if not force:
        cached = cache.get(key)
        if cached is not None:
            return cached

    profile = getattr(user, "profile", None)
    main = getattr(profile, "main_character", None)
    chars = list(EveCharacter.objects.filter(character_ownership__user=user))

    steps = [_step("main", "Main character", bool(main),
                   main.character_name if main else "geen main", "👤")]
    if main:
        steps.append(_state_step(user))
        steps.append(_groups_step(user))
        steps.extend(_token_steps(user, chars))
        steps.extend(_service_steps(user))
        steps.extend(_checklist_steps(user))
        ma = _memberaudit_step(chars)
        if ma:
            steps.append(ma)

    counted = [s for s in steps if s["done"] is not None]
    done = sum(1 for s in counted if s["done"])
    total = len(counted)
    row = {
        "user_id": user.pk,
        "username": user.username,
        "main_id": main.character_id if main else None,
        "main_name": main.character_name if main else user.username,
        "corp_name": main.corporation_name if main else "",
        "alliance_name": (main.alliance_name if main else "") or "",
        "state": getattr(getattr(profile, "state", None), "name", "") or "",
        "n_chars": len(chars),
        "steps": steps,
        "done": done,
        "total": total,
        "pct": int(round(done / total * 100)) if total else 0,
        "complete": total > 0 and done == total,
        "missing": [s["label"] for s in counted if not s["done"]],
    }
    cache.set(key, row, CACHE_SECONDS)
    return row


def _users_with_accepted_recruit(days=None):
    """Accounts met een aangenomen aanmelding → {user: datum van aannemen}."""
    qs = Recruit.objects.filter(status=Recruit.Status.ACCEPTED).select_related(
        "eve_character__character_ownership__user"
    )
    if days:
        qs = qs.filter(updated_at__gte=timezone.now() - timedelta(days=days))
    found = {}
    for r in qs:
        user = getattr(getattr(r.eve_character, "character_ownership", None), "user", None)
        if user is None:
            continue
        # Meerdere characters per account: neem de vroegste beslissing als "lid sinds".
        prev = found.get(user.pk)
        if prev is None or r.updated_at < prev[1]:
            found[user.pk] = (user, r.updated_at)
    return found


def _all_member_users():
    """Elk account met een main character (ook wie nooit door Character Scan ging)."""
    from django.contrib.auth import get_user_model

    return {
        u.pk: (u, u.date_joined)
        for u in get_user_model().objects
        .filter(profile__main_character__isnull=False)
        .select_related("profile__main_character")
    }


def overview(scope="new", force=False):
    """Rijen voor het onboarding-overzicht.

    scope: ``new`` = recent aangenomen, ``incomplete`` = iedereen die nog iets
    open heeft staan, ``all`` = elk account met een main character.
    """
    days = Settings.load().onboarding_new_days or DEFAULT_NEW_DAYS

    if scope == "all":
        found = _all_member_users()
        since_label = "lid sinds"
    elif scope == "incomplete":
        found = {**_all_member_users(), **_users_with_accepted_recruit()}
        since_label = "sinds"
    else:
        found = _users_with_accepted_recruit(days=days)
        since_label = "aangenomen"

    entries = sorted(found.values(), key=lambda x: x[1], reverse=True)

    def _one(pair):
        from django.db import connections
        user, since = pair
        try:
            row = dict(account_status(user, force=force))
            row["since"] = since
            return row
        finally:
            connections.close_all()  # thread-eigen DB-connectie netjes sluiten

    rows = []
    if entries:
        with ThreadPoolExecutor(max_workers=min(6, len(entries))) as ex:
            rows = list(ex.map(_one, entries))

    if scope == "incomplete":
        rows = [r for r in rows if not r["complete"]]

    # Kolommen = volgorde waarin de stappen voorkomen (globaal gelijk voor
    # iedereen, maar we leiden ze af uit de data zodat een ontbrekende plugin
    # de tabel niet scheeftrekt).
    columns, seen = [], set()
    for r in rows:
        for s in r["steps"]:
            if s["key"] not in seen:
                seen.add(s["key"])
                columns.append({"key": s["key"], "label": s["label"], "icon": s["icon"]})

    # Per rij de stappen in kolomvolgorde zetten (ontbrekende stap = leeg vakje).
    for r in rows:
        by_key = {s["key"]: s for s in r["steps"]}
        r["cells"] = [by_key.get(c["key"]) for c in columns]

    rows.sort(key=lambda r: (r["complete"], -r["pct"], r["main_name"].lower()))
    return {
        "rows": rows,
        "columns": columns,
        "since_label": since_label,
        "days": days,
        "n_incomplete": sum(1 for r in rows if not r["complete"]),
    }


def invalidate(user_ids=None):
    """Gooi de gecachte rijen weg (na een actie of via de Ververs-knop)."""
    if user_ids is None:
        return
    cache.delete_many([f"cs_onb_{pk}" for pk in user_ids])
