"""Bucket polling fallback for S3 backends without native notifications.

Some S3-compatible providers (e.g. OVH Public Cloud Object Storage) do not
support bucket event notifications (PutBucketNotificationConfiguration).
When the storage-hook webhook cannot be wired, this polling task offers an
opt-in alternative.

Algorithm:
- Query recordings whose status is savable and that were created within the
  configured lookback window. Bounded by the number of in-flight recordings,
  not by the number of objects in the bucket.
- For each candidate, issue a single HEAD_OBJECT on the expected key.
- When the object exists, run the same notification flow as the webhook.

The cost per run scales with the number of recordings awaiting notification,
not with bucket size: a bucket holding millions of historical recordings has
the same polling cost as an empty one.

Disabled by default — enable with RECORDING_STORAGE_POLLING_ENABLED=True.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.core.files.storage import default_storage
from django.utils import timezone

from botocore.exceptions import BotoCoreError, ClientError

from core import models

from .notification import notification_service

logger = logging.getLogger(__name__)


def _object_exists(s3_client, bucket: str, key: str) -> bool:
    """Return True iff HEAD_OBJECT succeeds for the given key."""
    try:
        s3_client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        # 404 / NoSuchKey is the expected "not yet uploaded" path.
        if exc.response.get("Error", {}).get("Code") in {
            "404",
            "NoSuchKey",
            "NotFound",
        }:
            return False
        raise
    return True


def poll_storage_for_new_recordings() -> int:
    """Notify external services for savable recordings whose file exists in S3.

    Returns the number of recordings for which notification was attempted.
    Idempotent: once a recording transitions out of ACTIVE/STOPPED the query
    no longer selects it, so re-runs cannot re-trigger notifications.
    """

    lookback = timedelta(hours=settings.RECORDING_STORAGE_POLLING_LOOKBACK_HOURS)
    cutoff = timezone.now() - lookback
    batch_size = settings.RECORDING_STORAGE_POLLING_BATCH_SIZE

    candidates = list(
        models.Recording.objects.filter(
            status__in=(
                models.RecordingStatusChoices.ACTIVE,
                models.RecordingStatusChoices.STOPPED,
            ),
            created_at__gte=cutoff,
        ).order_by("created_at")[:batch_size]
    )

    if not candidates:
        return 0

    s3_client = default_storage.connection.meta.client
    bucket = default_storage.bucket_name

    processed = 0
    for recording in candidates:
        try:
            exists = _object_exists(s3_client, bucket, recording.key)
        except (BotoCoreError, ClientError):
            logger.exception(
                "Storage polling HEAD failed for recording %s", recording.id
            )
            continue

        if not exists:
            continue

        notification_service.notify_and_update_status(recording)
        processed += 1

    if processed:
        logger.info("Storage polling notified %d recording(s)", processed)

    return processed
