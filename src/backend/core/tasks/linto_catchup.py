"""Tasks of the LinTO catch-up without live transcription (fork)."""

import logging

from core import models
from core.services import linto_catchup
from core.tasks._task import task

logger = logging.getLogger(__name__)


@task
def start_linto_catchup_capture(recording_id: str):
    """Start the catch-up capture of a recording that is still active."""
    recording = (
        models.Recording.objects.select_related("room").filter(id=recording_id).first()
    )
    if recording is None:
        return
    try:
        linto_catchup.start_capture(recording)
    except linto_catchup.CatchupError:
        logger.exception("Catch-up capture start failed for %s", recording_id)


@task
def reconcile_linto_catchup():
    """Stop orphan captures, delete unneeded segments and ended states (Celery Beat)."""
    if not linto_catchup.is_enabled():
        return None
    try:
        result = linto_catchup.reconcile()
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception("Catch-up reconciliation failed")
        return None
    if any(result.values()):
        logger.info(
            "Catch-up reconciliation: %d captures stopped, %d segment prefixes "
            "deleted, %d states discarded",
            result["stopped"],
            result["deleted"],
            result["discarded"],
        )
    return result
