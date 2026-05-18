"""Test bucket polling fallback."""

# pylint: disable=redefined-outer-name

from datetime import timedelta
from unittest import mock

from django.utils import timezone

import pytest
from botocore.exceptions import ClientError

from core import factories, models
from core.recording.event import polling

pytestmark = pytest.mark.django_db


@pytest.fixture
def fake_s3_client():
    """Mock the boto3 client exposed by Django's default storage."""

    client = mock.MagicMock()
    client.head_object.return_value = {}

    with mock.patch("core.recording.event.polling.default_storage") as default_storage:
        default_storage.connection.meta.client = client
        default_storage.bucket_name = "test-bucket"
        yield client


def _missing_key_error():
    return ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")


def test_polling_notifies_savable_recording_with_existing_file(fake_s3_client):
    """A recording in ACTIVE state whose file is in the bucket gets notified."""
    recording = factories.RecordingFactory(
        status=models.RecordingStatusChoices.ACTIVE,
        mode=models.RecordingModeChoices.SCREEN_RECORDING,
    )

    with mock.patch(
        "core.recording.event.notification.NotificationService.notify_external_services",
        return_value=True,
    ) as notify:
        processed = polling.poll_storage_for_new_recordings()

    assert processed == 1
    notify.assert_called_once()
    assert notify.call_args.args[0].id == recording.id
    fake_s3_client.head_object.assert_called_once_with(
        Bucket="test-bucket", Key=recording.key
    )
    recording.refresh_from_db()
    assert recording.status == models.RecordingStatusChoices.NOTIFICATION_SUCCEEDED


def test_polling_marks_saved_when_notification_fails(fake_s3_client):
    """When notification returns False, status falls back to SAVED."""
    recording = factories.RecordingFactory(
        status=models.RecordingStatusChoices.STOPPED,
    )

    with mock.patch(
        "core.recording.event.notification.NotificationService.notify_external_services",
        return_value=False,
    ):
        polling.poll_storage_for_new_recordings()

    recording.refresh_from_db()
    assert recording.status == models.RecordingStatusChoices.SAVED


def test_polling_skips_already_saved_recording(fake_s3_client):
    """Recordings already notified are not re-processed (idempotent)."""
    factories.RecordingFactory(
        status=models.RecordingStatusChoices.NOTIFICATION_SUCCEEDED,
    )

    with mock.patch(
        "core.recording.event.notification.NotificationService.notify_external_services"
    ) as notify:
        processed = polling.poll_storage_for_new_recordings()

    assert processed == 0
    notify.assert_not_called()
    fake_s3_client.head_object.assert_not_called()


def test_polling_skips_when_file_not_yet_uploaded(fake_s3_client):
    """A savable recording whose file is not yet in the bucket is left alone."""
    recording = factories.RecordingFactory(
        status=models.RecordingStatusChoices.ACTIVE,
    )
    fake_s3_client.head_object.side_effect = _missing_key_error()

    with mock.patch(
        "core.recording.event.notification.NotificationService.notify_external_services"
    ) as notify:
        processed = polling.poll_storage_for_new_recordings()

    assert processed == 0
    notify.assert_not_called()
    recording.refresh_from_db()
    assert recording.status == models.RecordingStatusChoices.ACTIVE


def test_polling_ignores_recordings_outside_lookback_window(fake_s3_client, settings):
    """Recordings older than the lookback are not picked up."""
    settings.RECORDING_STORAGE_POLLING_LOOKBACK_HOURS = 1
    old = factories.RecordingFactory(status=models.RecordingStatusChoices.ACTIVE)
    models.Recording.objects.filter(pk=old.pk).update(
        created_at=timezone.now() - timedelta(hours=2)
    )

    with mock.patch(
        "core.recording.event.notification.NotificationService.notify_external_services"
    ) as notify:
        processed = polling.poll_storage_for_new_recordings()

    assert processed == 0
    notify.assert_not_called()
    fake_s3_client.head_object.assert_not_called()


def test_polling_continues_when_head_raises_unexpected_error(fake_s3_client):
    """A transient HEAD error for one recording does not block others."""
    factories.RecordingFactory(status=models.RecordingStatusChoices.ACTIVE)
    factories.RecordingFactory(status=models.RecordingStatusChoices.ACTIVE)

    fake_s3_client.head_object.side_effect = [
        ClientError(
            {"Error": {"Code": "InternalError", "Message": "boom"}}, "HeadObject"
        ),
        {},
    ]

    with mock.patch(
        "core.recording.event.notification.NotificationService.notify_external_services",
        return_value=True,
    ):
        processed = polling.poll_storage_for_new_recordings()

    assert processed == 1


def test_polling_no_candidates_short_circuits(fake_s3_client):
    """With nothing to look at, no S3 call is issued."""
    assert polling.poll_storage_for_new_recordings() == 0
    fake_s3_client.head_object.assert_not_called()
