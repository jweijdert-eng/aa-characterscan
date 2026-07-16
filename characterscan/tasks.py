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
def remind_waiting_recruits():
    """Herinner op Discord aan aanmeldingen die te lang op een beslissing wachten.

    Draait periodiek via Celery-beat. Vindt nieuwe/in-behandeling recruits ouder
    dan de drempel (Settings.wait_alert_hours, standaard 48u) en post één
    samenvatting naar de Discord-webhook.
    """
    from datetime import timedelta

    from django.utils import timezone

    from .discord import notify_waiting
    from .models import Recruit, Settings

    hours = Settings.load().wait_alert_hours or 48
    cutoff = timezone.now() - timedelta(hours=hours)
    qs = (Recruit.objects
          .filter(status__in=(Recruit.Status.NEW, Recruit.Status.IN_PROGRESS),
                  applied_at__lte=cutoff)
          .select_related("eve_character").order_by("applied_at"))

    now = timezone.now()
    items = []
    for r in qs:
        waited_h = (now - r.applied_at).total_seconds() / 3600
        label = f"{int(waited_h / 24)}d" if waited_h >= 24 else f"{int(waited_h)}u"
        items.append((r.eve_character.character_name, r.eve_character.character_id, label))

    if items:
        try:
            notify_waiting(items, hours)
        except Exception:  # noqa: BLE001
            logger.warning("remind_waiting_recruits: Discord-melding faalde", exc_info=True)
    logger.info("Character Scan: %d wachtende aanmelding(en) gemeld.", len(items))
    return len(items)


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
    from .profile import basic_stats
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
        recruit.last_stats = basic_stats(recruit.eve_character)  # snapshot voor de lijst
        recruit.save(update_fields=["last_score", "last_verdict", "last_scanned_at",
                                    "known_bad_flags", "last_stats"])

    logger.info("Character Scan: leden herscand, %d nieuwe waarschuwing(en).", alerts)
    return alerts
