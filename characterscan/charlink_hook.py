"""CharLink-integratie voor Character Scan.

Twee koppel-opties in de CharLink-pagina:
  1. "Character Scan (recruit)"  — een recruit koppelt z'n character (data-scopes);
     maakt een Recruit-record aan zodat de vetting live via ESI kan lezen.
  2. "Character Scan (standings)" — een director koppelt de corp/alliance-contacts-
     scopes → de org-vijandenlijst wordt daaruit opgebouwd.
"""

from django.contrib import messages
from django.db.models import Exists, OuterRef
from django.utils.html import format_html

from esi.models import Token

from allianceauth.eveonline.models import EveCharacter

from charlink.app_imports.utils import AppImport, LoginImport
from charlink.utils import users_with_permissions

from .esi_fetch import CS_RECRUIT_SCOPES
from .models import Recruit
from .vetting import ALLIANCE_CONTACTS_SCOPE, CORP_CONTACTS_SCOPE


# ── Recruit-koppeling ────────────────────────────────────────────────────────
def _add_recruit(request, token: Token):
    from django.core.cache import cache

    ec = EveCharacter.objects.get(character_id=token.character_id)
    Recruit.objects.get_or_create(eve_character=ec)
    cache.delete(f"cs_profile_{token.character_id}")
    messages.success(
        request,
        format_html("<strong>{}</strong> is aangemeld voor Character Scan.", ec),
    )


def _recruit_added(character: EveCharacter):
    return Recruit.objects.filter(eve_character=character).exists()


# ── Standings-koppeling (director) ───────────────────────────────────────────
def _add_standings(request, token: Token):
    from django.core.cache import cache

    from .vetting import org_enemy_ids

    cache.delete("cs_org_enemies")
    count = len(org_enemy_ids(force=True))
    messages.success(
        request,
        format_html("Corp/alliance-standings gekoppeld — {} vijandige entiteiten geladen.", count),
    )


def _standings_added(character: EveCharacter):
    return Token.objects.filter(
        character_id=character.character_id, scopes__name=CORP_CONTACTS_SCOPE
    ).exists()


app_import = AppImport("characterscan", [
    LoginImport(
        app_label="characterscan",
        unique_id="recruit",
        field_label="Character Scan (recruit)",
        add_character=_add_recruit,
        scopes=CS_RECRUIT_SCOPES,
        check_permissions=lambda user: user.has_perm("characterscan.basic_access"),
        is_character_added=_recruit_added,
        is_character_added_annotation=Exists(
            Recruit.objects.filter(eve_character_id=OuterRef("pk"))
        ),
        get_users_with_perms=lambda: users_with_permissions(["characterscan.basic_access"]),
    ),
    LoginImport(
        app_label="characterscan",
        unique_id="standings",
        field_label="Character Scan (corp/alliance standings)",
        add_character=_add_standings,
        scopes=[CORP_CONTACTS_SCOPE, ALLIANCE_CONTACTS_SCOPE],
        check_permissions=lambda user: user.has_perm("characterscan.recruiter"),
        is_character_added=_standings_added,
        is_character_added_annotation=Exists(
            Token.objects.filter(
                character_id=OuterRef("character_id"), scopes__name=CORP_CONTACTS_SCOPE
            )
        ),
        get_users_with_perms=lambda: users_with_permissions(["characterscan.recruiter"]),
    ),
])
