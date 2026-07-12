"""
App Models

Character Scan — recruitment/vetting plugin.
Een Recruit koppelt een EveCharacter aan een aanmeldingsstatus; de character-data
zelf wordt hergebruikt uit Member Audit (zie profile.py).
"""

# Django
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
