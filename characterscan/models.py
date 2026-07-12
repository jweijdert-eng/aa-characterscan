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


class Recruit(models.Model):
    """Een aanmelding: een EveCharacter met een recruitment-status + notities."""

    class Status(models.TextChoices):
        NEW = "new", _("New")
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

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = _("recruit")
        verbose_name_plural = _("recruits")

    def __str__(self) -> str:
        return f"{self.eve_character.character_name} ({self.status})"


class RecruitLogEntry(models.Model):
    """Een logregel: wie deed wat met een aanmelding (aannemen/afwijzen/notitie)."""

    class Action(models.TextChoices):
        APPLIED = "applied", _("Aangemeld")
        ACCEPTED = "accepted", _("Aangenomen")
        REJECTED = "rejected", _("Afgewezen")
        NEW = "new", _("Heropend")
        NOTE = "note", _("Notitie")

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
