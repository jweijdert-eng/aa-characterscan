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
