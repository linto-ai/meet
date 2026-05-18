"""Periodic Celery task that polls the recording bucket."""

import logging

from core.recording.event.polling import poll_storage_for_new_recordings
from core.tasks._task import task

logger = logging.getLogger(__name__)


@task
def poll_storage_for_recordings():
    """Trigger notifications for recordings whose file exists in S3.

    Wraps the polling helper with a top-level exception handler so a
    transient bucket error never crashes the Celery beat loop.
    """
    try:
        return poll_storage_for_new_recordings()
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception("Storage polling task failed")
        return 0
