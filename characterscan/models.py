"""
App Models

Character Scan — recruitment/vetting plugin.
Een Recruit koppelt een EveCharacter aan een aanmeldingsstatus; de character-data
zelf wordt hergebruikt uit Member Audit (zie profile.py).
"""

# Django
from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

# Alliance Auth
from allianceauth.eveonline.models import EveCharacter


class General(models.Model):
    """Meta model for app permissions"""

    class Meta:
        """Meta definitions"""

        managed = False
        default_permissions = ()
        permissions = (
            ("basic_access", _("Can access this app and apply as a recruit")),
            ("recruiter", _("Can view and manage recruitment applications")),
        )


class Settings(models.Model):
    """Eén rij met plugin-instellingen, bewerkbaar via het admin-paneel."""

    discord_webhook = models.URLField(
        max_length=500, blank=True, default="",
        verbose_name=_("Discord-webhook-URL"),
        help_text=_("Voor aanmeldingen en rode-vlag-meldingen. Leeg = notificaties uit. "
                    "(Discord: Kanaal → Integraties → Webhooks → Nieuwe webhook.)"),
    )
    notify_new_application = models.BooleanField(
        default=True, verbose_name=_("Melden bij nieuwe aanmelding"),
    )
    notify_alerts = models.BooleanField(
        default=True, verbose_name=_("Melden bij nieuwe rode vlag (monitoring)"),
    )
    show_actions_on_done = models.BooleanField(
        default=False, verbose_name=_("Actie-knoppen tonen bij afgeronde aanmeldingen"),
        help_text=_("Uit (standaard): bij afgeronde (aangenomen/afgewezen) aanmeldingen zijn de "
                    "✓/✕/⏳-knoppen verborgen. Aan: toon ze ook daar, zodat je de status kunt wijzigen."),
    )
    # Vetting-drempels (admin wint; anders settings.py-fallback)
    wallet_alert_isk = models.BigIntegerField(
        null=True, blank=True, verbose_name=_("Wallet-alarmdrempel (ISK)"),
        help_text=_("Grote/verdachte ISK-bewegingen boven dit bedrag. Leeg = standaard 1 miljard."),
    )
    injector_alert = models.PositiveSmallIntegerField(
        null=True, blank=True, verbose_name=_("Skill-injector-drempel"),
        help_text=_("Waarschuwing vanaf dit aantal gekochte Large injectors. Leeg = standaard 5."),
    )
    wait_alert_hours = models.PositiveSmallIntegerField(
        null=True, blank=True, verbose_name=_("Wachttijd-alarmdrempel (uren)"),
        help_text=_("Markeer aanmeldingen die zó lang op een beslissing wachten (nieuw/in "
                    "behandeling). Leeg = standaard 48 uur."),
    )
    trusted_link_domains = models.TextField(
        blank=True, default="", verbose_name=_("Vertrouwde link-domeinen"),
        help_text=_("Eén per regel (of komma-gescheiden). Links hierheen tellen niet als "
                    "'externe link' in de mail-scan. Bijv. auth.jouwalliance.org"),
    )
    extra_enemy_ids = models.TextField(
        blank=True, default="", verbose_name=_("Extra vijand-ids"),
        help_text=_("Handmatige aanvulling op de automatische vijandenlijst: character-, corp- "
                    "of alliance-ids (één per regel of komma-gescheiden)."),
    )

    class Meta:
        default_permissions = ()
        permissions = (("manage_settings", _("Can manage Character Scan settings")),)
        verbose_name = _("instellingen")
        verbose_name_plural = _("instellingen")

    def __str__(self) -> str:
        return "Character Scan instellingen"

    @staticmethod
    def _split(text):
        import re
        return [t.strip() for t in re.split(r"[\s,]+", text or "") if t.strip()]

    def trusted_domains_list(self):
        return [d.lower().lstrip(".") for d in self._split(self.trusted_link_domains)]

    def extra_enemy_id_list(self):
        return [int(t) for t in self._split(self.extra_enemy_ids) if t.isdigit()]

    def save(self, *args, **kwargs):
        self.pk = 1  # singleton
        super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> "Settings":
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj


class Recruit(models.Model):
    """Een aanmelding: een EveCharacter met een recruitment-status + notities."""

    class Status(models.TextChoices):
        NEW = "new", _("New")
        IN_PROGRESS = "in_progress", _("In behandeling")
        ACCEPTED = "accepted", _("Accepted")
        REJECTED = "rejected", _("Rejected")

    eve_character = models.OneToOneField(
        EveCharacter,
        on_delete=models.CASCADE,
        related_name="+",
        verbose_name=_("EVE character"),
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.NEW, db_index=True
    )
    notes = models.TextField(blank=True, default="")
    applied_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    # Recruiter die de aanmelding heeft opgepakt / verwerkt
    handled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name=_("In behandeling door"),
    )

    # Doorlopende monitoring: laatste automatische scan-uitkomst
    last_score = models.PositiveSmallIntegerField(null=True, blank=True)
    last_verdict = models.CharField(max_length=10, blank=True, default="")
    last_scanned_at = models.DateTimeField(null=True, blank=True)
    known_bad_flags = models.JSONField(default=list, blank=True)
    # Snapshot van de compacte lijst-stats, zodat afgeronde recruits niet live
    # gescand hoeven te worden om de kaart te tonen (zie recruiter_list).
    last_stats = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = _("recruit")
        verbose_name_plural = _("recruits")

    def __str__(self) -> str:
        return f"{self.eve_character.character_name} ({self.status})"

    @property
    def handled_by_name(self) -> str:
        """Main character-naam van de recruiter die 'm oppakte (of z'n username)."""
        u = self.handled_by
        if not u:
            return ""
        try:
            main = u.profile.main_character
            if main:
                return main.character_name
        except Exception:  # noqa: BLE001
            pass
        return u.username


class RecruitLogEntry(models.Model):
    """Een logregel: wie deed wat met een aanmelding (aannemen/afwijzen/notitie)."""

    class Action(models.TextChoices):
        APPLIED = "applied", _("Aangemeld")
        IN_PROGRESS = "in_progress", _("In behandeling genomen")
        ACCEPTED = "accepted", _("Aangenomen")
        REJECTED = "rejected", _("Afgewezen")
        NEW = "new", _("Heropend")
        NOTE = "note", _("Notitie")
        ALERT = "alert", _("Waarschuwing")

    recruit = models.ForeignKey(
        Recruit, on_delete=models.CASCADE, related_name="log_entries"
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    action = models.CharField(max_length=20, choices=Action.choices)
    comment = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        default_permissions = ()
        ordering = ["-created_at"]
        verbose_name = _("recruit log entry")
        verbose_name_plural = _("recruit log entries")

    def __str__(self) -> str:
        return f"{self.recruit_id} · {self.action} · {self.created_at:%Y-%m-%d %H:%M}"

    @property
    def actor_name(self) -> str:
        """De naam van wie de actie deed — main character indien mogelijk."""
        if not self.actor:
            return "systeem"
        try:
            main = self.actor.profile.main_character
            if main:
                return main.character_name
        except Exception:
            pass
        return self.actor.username
