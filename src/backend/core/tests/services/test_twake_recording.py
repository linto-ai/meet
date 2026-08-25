"""Tests for core.services.twake_recording upload orchestration.

This module is the only trace the screen-recording pipeline leaves in the
production logs, so the assertions below are mostly about *what gets logged*:
a silent success used to be indistinguishable from a silent skip when reading
Loki, which made a whole delivery path unauditable.
"""

# pylint: disable=redefined-outer-name

from unittest import mock

import pytest
from asgiref.sync import async_to_sync

from core import factories
from core.services import twake_recording

pytestmark = pytest.mark.django_db

FILES = [
    {"filename": "Enregistrement.mp4", "content": b"x", "content_type": "video/mp4"},
    {"filename": "Enregistrement.ogg", "content": b"y", "content_type": "audio/ogg"},
]


@pytest.fixture
def owner_access():
    """An owner access whose user carries an OIDC sub."""
    recording = factories.RecordingFactory()
    user = factories.UserFactory(sub="owner-sub")
    return factories.UserRecordingAccessFactory(recording=recording, user=user)


@pytest.fixture
def configured(settings):
    """Twake Drive credentials present, so the upload is actually attempted."""
    settings.CLOUDERY_URL = "https://cloudery.test"
    settings.CLOUDERY_TOKEN = "token"
    settings.TWAKE_DEV_INSTANCE_OVERRIDE = "instance.test"


@pytest.fixture
def drive():
    """Patch the Twake Drive HTTP layer, leaving save_file to each test."""
    with (
        mock.patch.object(
            twake_recording, "get_drive_token", new_callable=mock.AsyncMock
        ) as token,
        mock.patch.object(
            twake_recording, "ensure_meeting_directory", new_callable=mock.AsyncMock
        ) as ensure,
        mock.patch.object(
            twake_recording, "build_drive_link", return_value="https://drive.test/f"
        ),
        mock.patch.object(
            twake_recording, "save_file", new_callable=mock.AsyncMock
        ) as save,
    ):
        token.return_value = "drive-token"
        ensure.return_value = "dir-1"
        yield save


def upload(owner_access):
    """Run the coroutine under test synchronously."""
    return async_to_sync(twake_recording.upload_recording_files)(
        owner_access.recording, owner_access, FILES
    )


class TestUploadRecordingFiles:
    def test_logs_every_uploaded_file_on_success(
        self, configured, drive, owner_access, caplog
    ):
        drive.return_value = True

        with caplog.at_level("INFO", logger=twake_recording.logger.name):
            link = upload(owner_access)

        assert link == "https://drive.test/f"
        records = [r for r in caplog.records if r.levelname == "INFO"]
        assert len(records) == 1
        message = records[0].getMessage()
        assert "Uploaded 2 file(s)" in message
        assert str(owner_access.recording.id) in message
        assert "Enregistrement.mp4" in message
        assert "Enregistrement.ogg" in message

    def test_warns_when_only_some_files_made_it(
        self, configured, drive, owner_access, caplog
    ):
        # First file uploads, second one raises: the link is still returned, but
        # the operator must be able to see that the folder is incomplete.
        drive.side_effect = [True, RuntimeError("boom")]

        with caplog.at_level("INFO", logger=twake_recording.logger.name):
            link = upload(owner_access)

        assert link == "https://drive.test/f"
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        assert "Partially uploaded 1/2 file(s)" in warnings[0].getMessage()
        assert not [r for r in caplog.records if r.levelname == "INFO"]

    def test_warns_and_returns_none_when_nothing_uploaded(
        self, configured, drive, owner_access, caplog
    ):
        drive.return_value = False

        with caplog.at_level("INFO", logger=twake_recording.logger.name):
            link = upload(owner_access)

        assert link is None
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any("No file could be uploaded" in r.getMessage() for r in warnings)

    def test_skips_quietly_when_twake_is_not_configured(
        self, settings, owner_access, caplog
    ):
        settings.CLOUDERY_URL = None
        settings.CLOUDERY_TOKEN = None

        with caplog.at_level("INFO", logger=twake_recording.logger.name):
            link = upload(owner_access)

        assert link is None
        # Nothing at INFO or above: this branch is a legitimate no-op.
        assert not caplog.records

    def test_warns_when_owner_has_no_oidc_sub(self, configured, owner_access, caplog):
        owner_access.user.sub = None

        with caplog.at_level("INFO", logger=twake_recording.logger.name):
            link = upload(owner_access)

        assert link is None
        assert any(
            "Owner has no OIDC sub" in r.getMessage()
            for r in caplog.records
            if r.levelname == "WARNING"
        )
