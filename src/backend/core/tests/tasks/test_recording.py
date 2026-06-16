"""Tests for core.tasks.recording (screen recording → Twake Drive).

Covers the idempotent/retryable refactor: the Twake upload is wrapped in
``classify_external`` (transient → retry, permanent → on_failure), every step is
checkpointed in ``linto_state`` (skipped on resume), and the public task routes
failures to the shared handler in inline mode.
"""

# pylint: disable=redefined-outer-name

from unittest import mock

import aiohttp
import pytest

from core import factories, models
from core.tasks import recording as recording_task
from core.tasks._errors import TransientError

pytestmark = pytest.mark.django_db


@pytest.fixture
def s3_download():
    """Patch the S3 storage download to return fake MP4 bytes."""
    with mock.patch.object(recording_task, "default_storage") as storage_mock:
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
    rec = factories.RecordingFactory(mode=models.RecordingModeChoices.SCREEN_RECORDING)
    factories.UserRecordingAccessFactory(
        recording=rec, user=owner_user, role=models.RoleChoices.OWNER
    )
    return rec


@pytest.fixture
def cloudery(settings):
    """Configure Twake/Cloudery so the twake step is active."""
    settings.CLOUDERY_URL = "https://cloudery"
    settings.CLOUDERY_TOKEN = "tok"
    return settings


def test_twake_upload_success_triggers_email_with_link(
    s3_download, screen_recording, cloudery
):
    """Successful Twake upload → email sent + twake step checkpointed."""
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

    screen_recording.refresh_from_db()
    assert screen_recording.linto_state["steps"]["twake"] is True
    assert screen_recording.linto_state["steps"]["email"] is True


def test_transient_twake_failure_propagates_for_retry(
    s3_download, screen_recording, cloudery
):
    """A transient Twake failure becomes TransientError (Celery retry); no email."""
    with (
        mock.patch.object(
            recording_task,
            "upload_recording_files",
            new=mock.AsyncMock(side_effect=aiohttp.ClientConnectionError("cozy down")),
        ),
        mock.patch.object(recording_task, "EmailMultiAlternatives") as mail_mock,
    ):
        with pytest.raises(TransientError):
            recording_task._process_screen_recording_sync(str(screen_recording.id))

        mail_mock.return_value.send.assert_not_called()

    screen_recording.refresh_from_db()
    assert "twake" not in (screen_recording.linto_state.get("steps") or {})


def test_permanent_twake_failure_propagates(s3_download, screen_recording, cloudery):
    """A permanent Twake failure propagates unchanged (→ on_failure), no retry wrap."""
    with (
        mock.patch.object(
            recording_task,
            "upload_recording_files",
            new=mock.AsyncMock(side_effect=RuntimeError("bad configuration")),
        ),
        mock.patch.object(recording_task, "EmailMultiAlternatives") as mail_mock,
    ):
        with pytest.raises(RuntimeError, match="bad configuration"):
            recording_task._process_screen_recording_sync(str(screen_recording.id))

        mail_mock.return_value.send.assert_not_called()


def test_twake_step_already_done_skips_upload_but_emails(
    s3_download, screen_recording, cloudery
):
    """Resume: a checkpointed twake step is not re-uploaded, but email still goes out."""
    screen_recording.linto_state = {
        "conversation_id": None,
        "steps": {"twake": True},
        "attempts": 1,
    }
    screen_recording.save(update_fields=["linto_state"])

    with (
        mock.patch.object(
            recording_task, "upload_recording_files", new=mock.AsyncMock()
        ) as upload_mock,
        mock.patch.object(recording_task, "EmailMultiAlternatives") as mail_mock,
    ):
        recording_task._process_screen_recording_sync(str(screen_recording.id))

        upload_mock.assert_not_called()
        mail_mock.return_value.send.assert_called_once()


def test_email_step_already_done_sends_nothing(s3_download, screen_recording, cloudery):
    """Resume: both twake and email done → no upload, no email."""
    screen_recording.linto_state = {
        "conversation_id": None,
        "steps": {"twake": True, "email": True},
        "attempts": 1,
    }
    screen_recording.save(update_fields=["linto_state"])

    with (
        mock.patch.object(
            recording_task, "upload_recording_files", new=mock.AsyncMock()
        ) as upload_mock,
        mock.patch.object(recording_task, "EmailMultiAlternatives") as mail_mock,
    ):
        recording_task._process_screen_recording_sync(str(screen_recording.id))

        upload_mock.assert_not_called()
        mail_mock.return_value.send.assert_not_called()


def test_no_owner_skips_everything(s3_download, cloudery):
    """Recording without owner → no upload, no mail, no crash."""
    rec = factories.RecordingFactory(mode=models.RecordingModeChoices.SCREEN_RECORDING)

    with (
        mock.patch.object(
            recording_task, "upload_recording_files", new=mock.AsyncMock()
        ) as upload_mock,
        mock.patch.object(recording_task, "EmailMultiAlternatives") as mail_mock,
    ):
        recording_task._process_screen_recording_sync(str(rec.id))

        upload_mock.assert_not_called()
        mail_mock.return_value.send.assert_not_called()


def test_public_task_routes_failure_to_handler(screen_recording):
    """Inline mode: the public @task entry must route failures to the shared
    handler (and swallow), not raise to the caller."""
    with (
        mock.patch.object(
            recording_task,
            "_process_screen_recording_sync",
            side_effect=Exception("boom"),
        ),
        mock.patch("core.tasks._base.handle_pipeline_failure") as handler,
    ):
        # Inline @task: .delay routes through the bind-aware fallback wrapper.
        recording_task.process_screen_recording_to_twake.delay(str(screen_recording.id))

    handler.assert_called_once()
    assert handler.call_args.args[0] == str(screen_recording.id)
