"""App Views — Character Scan recruitment/vetting."""

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

from .models import Recruit, RecruitLogEntry
from .profile import basic_stats, full_profile
from .vetting import (
    ALLIANCE_CONTACTS_SCOPE,
    CORP_CONTACTS_SCOPE,
    assess,
    enemy_set,
    quick_verdict,
)


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
    qs = Recruit.objects.select_related("eve_character")
    counts = {row["status"]: row["n"] for row in qs.values("status").annotate(n=Count("id"))}
    counts["all"] = sum(counts.get(s, 0) for s in ("new", "accepted", "rejected"))
    filters = [
        {"key": "new", "label": _("Nieuw"), "count": counts.get("new", 0), "color": "warning"},
        {"key": "accepted", "label": _("Aangenomen"), "count": counts.get("accepted", 0), "color": "success"},
        {"key": "rejected", "label": _("Afgewezen"), "count": counts.get("rejected", 0), "color": "danger"},
        {"key": "all", "label": _("Totaal"), "count": counts.get("all", 0), "color": "info"},
    ]
    if status in ("new", "accepted", "rejected"):
        qs = qs.filter(status=status)

    recruiter_char = getattr(request.user.profile, "main_character", None)
    enemy = enemy_set(recruiter_char)
    recruits = [
        {
            "recruit": r,
            "stats": basic_stats(r.eve_character),
            "verdict": quick_verdict(r.eve_character, enemy),
        }
        for r in qs
    ]
    from .vetting import standings_token_exists

    return render(
        request,
        "characterscan/list.html",
        {
            "recruits": recruits,
            "filters": filters,
            "status": status,
            "needs_standings_grant": not standings_token_exists(),
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
    return render(
        request,
        "characterscan/detail.html",
        {"recruit": recruit, "profile": profile, "vetting": vetting},
    )


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
    elif action in ("accepted", "rejected", "new"):
        recruit.status = action
        recruit.save(update_fields=["status", "updated_at"])
        RecruitLogEntry.objects.create(
            recruit=recruit, actor=request.user, action=action, comment=comment
        )
        messages.success(request, _("Status van %(n)s bijgewerkt.") % {"n": name})

    return redirect(request.POST.get("next") or "characterscan:recruiter_list")
