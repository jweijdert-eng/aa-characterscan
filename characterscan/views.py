"""App Views — Character Scan recruitment/vetting."""

from concurrent.futures import ThreadPoolExecutor

# Django
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.cache import cache
from django.core.handlers.wsgi import WSGIRequest
from django.db.models import Count
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _

from allianceauth.eveonline.models import EveCharacter
from esi.decorators import token_required

from django.utils import timezone

from .models import Recruit, RecruitLogEntry, Settings
from .profile import basic_stats, full_profile
from .vetting import (
    ALLIANCE_CONTACTS_SCOPE,
    CORP_CONTACTS_SCOPE,
    VERDICTS,
    assess,
    enemy_set,
    quick_verdict,
)

# Verdict-weergave voor een opgeslagen niveau (of onbekend/nog niet gescand).
_UNKNOWN_VERDICT = {"level": "", "label": "?", "css": "secondary", "icon": "·"}


def _verdict_from_level(level):
    return VERDICTS.get(level, _UNKNOWN_VERDICT)


def _persist_snapshot(recruit, stats, verdict):
    """Bewaar de compacte lijst-stats + verdict op de recruit (voor snelle weergave)."""
    recruit.last_stats = stats or {}
    recruit.last_verdict = (verdict or {}).get("level", "")
    recruit.last_scanned_at = timezone.now()
    recruit.save(update_fields=["last_stats", "last_verdict", "last_scanned_at"])


@login_required
@permission_required("characterscan.basic_access")
def index(request: WSGIRequest) -> HttpResponse:
    """Landing: recruiters gaan naar de lijst, anderen zien de aanmeld-pagina."""
    if request.user.has_perm("characterscan.recruiter"):
        return redirect("characterscan:recruiter_list")

    main = getattr(request.user.profile, "main_character", None)
    recruit = None
    stats = None
    if main:
        recruit = Recruit.objects.filter(eve_character=main).first()
        stats = basic_stats(main)
    return render(
        request,
        "characterscan/index.html",
        {"main": main, "recruit": recruit, "stats": stats},
    )


@login_required
@permission_required("characterscan.basic_access")
def apply(request: WSGIRequest) -> HttpResponse:
    """Meld het hele account aan als recruit (main + alts)."""
    if request.method != "POST":
        return redirect("characterscan:index")
    chars = list(EveCharacter.objects.filter(character_ownership__user=request.user))
    if not chars:
        main = getattr(request.user.profile, "main_character", None)
        if main:
            chars = [main]
    if not chars:
        messages.error(request, _("Je hebt geen EVE-characters gekoppeld."))
        return redirect("characterscan:index")
    created = 0
    for ec in chars:
        _, was_created = Recruit.objects.get_or_create(eve_character=ec)
        created += int(was_created)
    if created:
        main = getattr(request.user.profile, "main_character", None) or chars[0]
        try:
            from .discord import notify_application
            notify_application(main, n_chars=len(chars))
        except Exception:  # noqa: BLE001 — notificatie mag de aanmelding nooit blokkeren
            pass
        messages.success(
            request,
            _("Je aanmelding is ingediend (%(n)d character(s)). Een recruiter neemt hem in behandeling.")
            % {"n": len(chars)},
        )
    else:
        messages.info(request, _("Je had je al aangemeld — niets veranderd."))
    return redirect("characterscan:index")


@login_required
@permission_required("characterscan.recruiter")
def recruiter_list(request: WSGIRequest) -> HttpResponse:
    """Overzicht van alle aanmeldingen voor recruiters."""
    status = request.GET.get("status", "new")
    base = Recruit.objects.select_related("eve_character", "handled_by__profile__main_character")
    counts = {row["status"]: row["n"] for row in base.values("status").annotate(n=Count("id"))}
    # Afgerond = verwerkt (aangenomen óf afgewezen); zo blijft "Nieuw" schoon.
    counts["afgerond"] = counts.get("accepted", 0) + counts.get("rejected", 0)
    filters = [
        {"key": "new", "label": _("Nieuw"), "count": counts.get("new", 0), "color": "warning"},
        {"key": "in_progress", "label": _("In behandeling"), "count": counts.get("in_progress", 0), "color": "info"},
        {"key": "afgerond", "label": _("Afgerond"), "count": counts.get("afgerond", 0), "color": "success"},
    ]
    if status == "afgerond":
        qs = base.filter(status__in=("accepted", "rejected"))
    elif status in ("new", "in_progress", "accepted", "rejected"):
        qs = base.filter(status=status)  # directe deep-links blijven werken
    else:
        qs = base

    recruiter_char = getattr(request.user.profile, "main_character", None)
    enemy = enemy_set(recruiter_char)

    # Alleen NIEUWE recruits (en afgeronde die nog nooit gescand zijn) worden live
    # gescand. Afgeronde recruits tonen hun opgeslagen snapshot — geen ESI-calls.
    recruit_list = list(qs)
    need_scan = [r for r in recruit_list if r.status in ("new", "in_progress") or not r.last_stats]

    def _scan(r):
        from django.db import connections
        try:
            return (basic_stats(r.eve_character, lite=True),
                    quick_verdict(r.eve_character, enemy, lite=True))
        finally:
            connections.close_all()  # thread-eigen DB-connectie netjes sluiten

    live = {}
    if need_scan:
        with ThreadPoolExecutor(max_workers=min(6, len(need_scan))) as ex:
            for r, res in zip(need_scan, ex.map(_scan, need_scan)):
                live[r.pk] = res
        for r in need_scan:  # snapshot opslaan (in de hoofd-thread)
            _persist_snapshot(r, *live[r.pk])

    recruits = []
    for r in recruit_list:
        if r.pk in live:
            stats, verdict = live[r.pk]
            fresh = True
        else:
            stats, verdict = r.last_stats, _verdict_from_level(r.last_verdict)
            fresh = False
        recruits.append({"recruit": r, "stats": stats, "verdict": verdict, "fresh": fresh})

    # Blacklist-flash: markeer recruits die (character/corp/alliance) op de
    # allianceauth-blacklist staan — één bulk-query. Zonder de plugin: overslaan.
    try:
        from blacklist.models import EveNote
        all_ids = set()
        for item in recruits:
            ec = item["recruit"].eve_character
            all_ids.update(filter(None, [ec.character_id, ec.corporation_id, ec.alliance_id]))
        bl_ids = set(EveNote.objects.filter(eve_id__in=all_ids, blacklisted=True)
                     .values_list("eve_id", flat=True)) if all_ids else set()
        for item in recruits:
            ec = item["recruit"].eve_character
            item["blacklisted"] = any(i in bl_ids for i in
                                      (ec.character_id, ec.corporation_id, ec.alliance_id) if i)
    except Exception:  # noqa: BLE001 — blacklist-plugin niet geïnstalleerd
        for item in recruits:
            item["blacklisted"] = False

    from .vetting import standings_token_exists

    from .models import Settings
    return render(
        request,
        "characterscan/list.html",
        {
            "recruits": recruits,
            "filters": filters,
            "status": status,
            "needs_standings_grant": not standings_token_exists(),
            "show_actions_on_done": Settings.load().show_actions_on_done,
            "active_tab": "list",
        },
    )


@login_required
@permission_required("characterscan.recruiter")
def activity_log(request: WSGIRequest) -> HttpResponse:
    """Overzicht van beslissingen/notities (wie, wanneer, opmerking)."""
    action = request.GET.get("action", "accepted")
    qs = RecruitLogEntry.objects.select_related("recruit__eve_character", "actor").order_by("-created_at")
    if action in ("accepted", "rejected", "note", "new", "alert"):
        qs = qs.filter(action=action)
    tabs = [
        {"key": "accepted", "label": _("Aangenomen"), "color": "success"},
        {"key": "rejected", "label": _("Afgewezen"), "color": "danger"},
        {"key": "alert", "label": _("Waarschuwingen"), "color": "warning"},
        {"key": "note", "label": _("Notities"), "color": "secondary"},
        {"key": "all", "label": _("Alles"), "color": "info"},
    ]
    return render(
        request,
        "characterscan/log.html",
        {"entries": qs[:500], "action": action, "log_filters": tabs, "active_tab": "log"},
    )


@login_required
@permission_required("characterscan.recruiter")
@token_required(scopes=[CORP_CONTACTS_SCOPE, ALLIANCE_CONTACTS_SCOPE])
def grant_standings(request: WSGIRequest, token) -> HttpResponse:
    """Eenmalig een director-token koppelen; daarna verloopt alles automatisch."""
    from .vetting import org_enemy_ids

    cache.delete("cs_org_enemies")
    count = len(org_enemy_ids(force=True))  # meteen ophalen
    messages.success(
        request,
        _("Corp/alliance-standings gekoppeld — %(n)d vijandige entiteiten geladen. "
          "Dit verloopt vanaf nu automatisch.") % {"n": count},
    )
    return redirect("characterscan:recruiter_list")


@login_required
@permission_required("characterscan.recruiter")
def recruit_detail(request: WSGIRequest, pk: int) -> HttpResponse:
    """Detail + (later) vetting van één aanmelding."""
    recruit = get_object_or_404(Recruit.objects.select_related("eve_character"), pk=pk)
    recruiter_char = getattr(request.user.profile, "main_character", None)
    enemy = enemy_set(recruiter_char)
    profile = full_profile(recruit.eve_character, enemy)
    vetting = assess(recruit.eve_character, enemy)

    blacklist_available, already_blacklisted = False, False
    try:
        from blacklist.models import EveNote
        blacklist_available = True
        already_blacklisted = EveNote.objects.filter(
            eve_id=recruit.eve_character.character_id, blacklisted=True
        ).exists()
    except Exception:  # noqa: BLE001 — blacklist-app niet geïnstalleerd
        pass

    return render(
        request,
        "characterscan/detail.html",
        {
            "recruit": recruit, "profile": profile, "vetting": vetting,
            "blacklist_available": blacklist_available,
            "already_blacklisted": already_blacklisted,
            "show_actions_on_done": Settings.load().show_actions_on_done,
        },
    )


@login_required
@permission_required("characterscan.recruiter")
def recruit_rescan(request: WSGIRequest, pk: int) -> HttpResponse:
    """Scan één recruit nu opnieuw (verse volledige ESI-scan) en werk de snapshot bij."""
    if request.method != "POST":
        return redirect("characterscan:recruiter_list")
    recruit = get_object_or_404(Recruit.objects.select_related("eve_character"), pk=pk)
    recruiter_char = getattr(request.user.profile, "main_character", None)
    enemy = enemy_set(recruiter_char)
    ec = recruit.eve_character

    from .esi_fetch import get_profile
    get_profile(ec, force=True)  # verse volledige scan (cache verversen)
    stats = basic_stats(ec)
    result = assess(ec, enemy)

    recruit.last_stats = stats
    recruit.last_verdict = result["verdict"].get("level", "")
    recruit.last_score = result.get("score")
    recruit.last_scanned_at = timezone.now()
    recruit.known_bad_flags = [f["label"] for f in result.get("flags", []) if f.get("level") == "bad"]
    recruit.save(update_fields=["last_stats", "last_verdict", "last_score",
                                "last_scanned_at", "known_bad_flags"])
    messages.success(request, _("%(n)s opnieuw gescand.") % {"n": ec.character_name})
    return redirect(request.POST.get("next") or "characterscan:recruiter_list")


@login_required
@permission_required("characterscan.recruiter")
def blacklist_recruit(request: WSGIRequest, pk: int) -> HttpResponse:
    """Zet een recruit-character op de allianceauth-blacklist (met reden)."""
    if request.method != "POST":
        return redirect("characterscan:recruit_detail", pk=pk)
    recruit = get_object_or_404(Recruit.objects.select_related("eve_character"), pk=pk)
    if not request.user.has_perm("blacklist.add_to_blacklist"):
        messages.error(request, _("Je hebt geen recht om te blacklisten."))
        return redirect("characterscan:recruit_detail", pk=pk)
    try:
        from blacklist.models import EveNote
    except Exception:  # noqa: BLE001
        messages.error(request, _("De Blacklist-app is niet geïnstalleerd."))
        return redirect("characterscan:recruit_detail", pk=pk)

    ec = recruit.eve_character
    actor = getattr(request.user.profile, "main_character", None)
    actor_name = actor.character_name if actor else request.user.username
    reason = request.POST.get("comment", "").strip() or _("Toegevoegd via Character Scan")

    note, created = EveNote.objects.get_or_create(
        eve_id=ec.character_id, eve_catagory="character",
        defaults={
            "eve_name": ec.character_name, "added_by": actor_name, "reason": reason,
            "corporation_id": ec.corporation_id, "corporation_name": ec.corporation_name,
            "alliance_id": ec.alliance_id or None, "alliance_name": ec.alliance_name or None,
        },
    )
    note.blacklisted = True
    if not created and reason not in (note.reason or ""):
        note.reason = f"{note.reason}\n{reason}" if note.reason else reason
    note.save()

    RecruitLogEntry.objects.create(
        recruit=recruit, actor=request.user, action="alert",
        comment=_("Op de blacklist gezet — %(r)s") % {"r": reason},
    )
    messages.success(request, _("%(n)s op de blacklist gezet.") % {"n": ec.character_name})
    return redirect("characterscan:recruit_detail", pk=pk)


@login_required
@permission_required("characterscan.recruiter")
def recruit_action(request: WSGIRequest, pk: int) -> HttpResponse:
    """Status zetten (aannemen/afwijzen) of verwijderen."""
    if request.method != "POST":
        return redirect("characterscan:recruiter_list")
    recruit = get_object_or_404(Recruit, pk=pk)
    action = request.POST.get("action")
    comment = request.POST.get("comment", "").strip()
    name = recruit.eve_character.character_name

    if action == "delete":
        recruit.delete()
        messages.success(request, _("Aanmelding van %(n)s verwijderd.") % {"n": name})
        return redirect("characterscan:recruiter_list")

    if action == "note":
        if comment:
            RecruitLogEntry.objects.create(
                recruit=recruit, actor=request.user, action="note", comment=comment
            )
            messages.success(request, _("Notitie toegevoegd."))
        else:
            messages.info(request, _("Lege notitie — niets opgeslagen."))
    elif action in ("accepted", "rejected", "new", "in_progress"):
        recruit.status = action
        # Wie pakt/verwerkt de aanmelding? Bij heropenen ('new') wissen we het.
        recruit.handled_by = None if action == "new" else request.user
        recruit.save(update_fields=["status", "handled_by", "updated_at"])
        RecruitLogEntry.objects.create(
            recruit=recruit, actor=request.user, action=action, comment=comment
        )
        messages.success(request, _("Status van %(n)s bijgewerkt.") % {"n": name})

    return redirect(request.POST.get("next") or "characterscan:recruiter_list")
