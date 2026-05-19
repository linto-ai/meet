"""Periodic Celery task that polls the recording bucket."""

import logging

from core.tasks._task import task

logger = logging.getLogger(__name__)


@task
def poll_storage_for_recordings():
    """Trigger notifications for recordings whose file exists in S3.

    Wraps the polling helper with a top-level exception handler so a
    transient bucket error never crashes the Celery beat loop.
    """
    # Imported lazily so this module can be loaded before Django apps are
    # ready (the eager import path from celery_app.py runs at worker boot).
    from core.recording.event.polling import (  # pylint: disable=import-outside-toplevel
        poll_storage_for_new_recordings,
    )

    try:
        return poll_storage_for_new_recordings()
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception("Storage polling task failed")
        return 0
