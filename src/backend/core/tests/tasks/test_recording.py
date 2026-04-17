"""Tests for core.tasks.recording (screen recording → Twake Drive)."""

# pylint: disable=redefined-outer-name

from unittest import mock

import pytest

from core import factories, models
from core.tasks import recording as recording_task

pytestmark = pytest.mark.django_db


@pytest.fixture
def s3_download():
    """Patch MinIO download to return fake MP4 bytes."""
    with mock.patch.object(
        recording_task, "default_storage"
    ) as storage_mock:
        s3_client = mock.Mock()
        response = {"Body": mock.Mock(read=mock.Mock(return_value=b"fake mp4"))}
        s3_client.get_object.return_value = response
        storage_mock.connection.meta.client = s3_client
        storage_mock.bucket_name = "bucket"
        yield storage_mock


@pytest.fixture
def owner_user():
    """Create a user with an OIDC sub."""
    return factories.UserFactory(sub="owner-sub-1", email="owner@example.com")


@pytest.fixture
def screen_recording(owner_user):
    """Create a screen_recording with an owner access."""
    rec = factories.RecordingFactory(
        mode=models.RecordingModeChoices.SCREEN_RECORDING
    )
    factories.RecordingAccessFactory(
        recording=rec, user=owner_user, role=models.RoleChoices.OWNER
    )
    return rec


def test_twake_upload_success_triggers_email_with_link(
    s3_download, screen_recording, settings
):
    """Successful Twake upload → email with twake_drive_link set."""
    settings.CLOUDERY_URL = "https://cloudery"
    settings.CLOUDERY_TOKEN = "tok"

    with (
        mock.patch.object(
            recording_task,
            "upload_recording_files",
            new=mock.AsyncMock(return_value="https://drive.example/folder/1"),
        ) as upload_mock,
        mock.patch.object(recording_task, "EmailMultiAlternatives") as mail_mock,
    ):
        recording_task._process_screen_recording_sync(str(screen_recording.id))

        upload_mock.assert_called_once()
        args, _ = upload_mock.call_args
        files = args[2]
        assert len(files) == 1
        assert files[0]["filename"].startswith("Enregistrement_")
        assert files[0]["filename"].endswith(".mp4")
        assert files[0]["content_type"] == "video/mp4"
        mail_mock.return_value.send.assert_called_once()


def test_twake_upload_failure_falls_back_to_direct_link_email(
    s3_download, screen_recording, settings
):
    """When Twake raises, email is still sent with only the download link."""
    settings.CLOUDERY_URL = "https://cloudery"
    settings.CLOUDERY_TOKEN = "tok"

    with (
        mock.patch.object(
            recording_task,
            "upload_recording_files",
            new=mock.AsyncMock(side_effect=RuntimeError("cozy down")),
        ),
        mock.patch.object(recording_task, "EmailMultiAlternatives") as mail_mock,
    ):
        recording_task._process_screen_recording_sync(str(screen_recording.id))

        mail_mock.return_value.send.assert_called_once()


def test_no_owner_skips_everything(s3_download, settings):
    """Recording without owner → no upload, no mail, no crash."""
    settings.CLOUDERY_URL = "https://cloudery"
    settings.CLOUDERY_TOKEN = "tok"

    rec = factories.RecordingFactory(
        mode=models.RecordingModeChoices.SCREEN_RECORDING
    )

    with (
        mock.patch.object(
            recording_task, "upload_recording_files", new=mock.AsyncMock()
        ) as upload_mock,
        mock.patch.object(recording_task, "EmailMultiAlternatives") as mail_mock,
    ):
        recording_task._process_screen_recording_sync(str(rec.id))

        upload_mock.assert_not_called()
        mail_mock.return_value.send.assert_not_called()


def test_public_task_catches_exception(screen_recording):
    """The @task entry point must swallow unexpected errors and log."""
    with mock.patch.object(
        recording_task,
        "_process_screen_recording_sync",
        side_effect=Exception("boom"),
    ):
        # Should not raise
        recording_task.process_screen_recording_to_twake(str(screen_recording.id))
