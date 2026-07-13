"""App Tasks"""

# Standard Library
import logging

# Third Party
from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task
def refresh_enemy_standings():
    """Ververs de org-vijandenlijst (corp/alliance-standings) in de cache.

    Draait periodiek via Celery-beat, zodat de vijandenlijst automatisch actueel
    blijft zonder dat er handmatig iets gekoppeld hoeft te worden.
    """
    from .vetting import org_enemy_ids

    enemies = org_enemy_ids(force=True)
    logger.info("Character Scan: org-vijandenlijst ververst (%d entiteiten).", len(enemies))
    return len(enemies)


@shared_task
def rescan_members():
    """Herscan aangenomen leden en waarschuw bij NIEUWE rode vlaggen (loyaliteitscheck).

    Draait periodiek via Celery-beat. Voor elk aangenomen lid: run de vetting,
    vergelijk de bad-signalen met wat we vorige keer zagen, en log + Discord-melding
    bij een nieuw signaal (bv. begint met de vijand te vliegen, assets naar vijandgebied).
    """
    from django.utils import timezone

    from .discord import notify_flags
    from .esi_fetch import get_profile
    from .models import Recruit, RecruitLogEntry
    from .vetting import assess, enemy_set

    enemy = enemy_set()
    alerts = 0
    for recruit in Recruit.objects.filter(status=Recruit.Status.ACCEPTED).select_related("eve_character"):
        try:
            get_profile(recruit.eve_character, force=True)  # verse data
            result = assess(recruit.eve_character, enemy)
        except Exception as e:  # noqa: BLE001
            logger.warning("rescan_members faalde voor %s: %s", recruit.eve_character_id, e)
            continue

        bad_now = [f["label"] for f in result["flags"] if f.get("level") == "bad"]
        new_bad = [b for b in bad_now if b not in (recruit.known_bad_flags or [])]
        if new_bad:
            alerts += 1
            note = "Nieuwe rode vlag(gen): " + ", ".join(new_bad)
            RecruitLogEntry.objects.create(recruit=recruit, actor=None, action="alert", comment=note)
            try:
                notify_flags(recruit.eve_character, result["verdict"], result["score"],
                             result["flags"], note=note)
            except Exception:  # noqa: BLE001
                pass

        recruit.last_score = result["score"]
        recruit.last_verdict = result["verdict"]["level"]
        recruit.last_scanned_at = timezone.now()
        recruit.known_bad_flags = bad_now
        recruit.save(update_fields=["last_score", "last_verdict", "last_scanned_at", "known_bad_flags"])

    logger.info("Character Scan: leden herscand, %d nieuwe waarschuwing(en).", alerts)
    return alerts
