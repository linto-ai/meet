"""Tests for core.tasks.linto extraction and Twake upload branching."""

# pylint: disable=redefined-outer-name,protected-access

from unittest import mock

import pytest

from core import factories, models
from core.recording.enums import FileExtension
from core.tasks import linto as linto_task

pytestmark = pytest.mark.django_db


def _make_recording(mode):
    rec = factories.RecordingFactory(mode=mode)
    user = factories.UserFactory(sub="owner-sub-2", email="owner2@example.com")
    factories.UserRecordingAccessFactory(
        recording=rec, user=user, role=models.RoleChoices.OWNER
    )
    return rec


def test_mp4_source_triggers_audio_extraction():
    """Recording.extension == mp4 must call extract_audio_from_video."""
    rec = _make_recording(models.RecordingModeChoices.SCREEN_RECORDING)
    assert rec.extension == FileExtension.MP4.value

    with (
        mock.patch.object(linto_task, "default_storage") as storage_mock,
        mock.patch.object(
            linto_task,
            "extract_audio_from_video",
            return_value=b"OggS extracted",
        ) as extract_mock,
        mock.patch.object(linto_task, "LinTO") as linto_cls,
    ):
        s3_client = mock.Mock()
        s3_client.get_object.return_value = {
            "Body": mock.Mock(read=mock.Mock(return_value=b"fake mp4 bytes"))
        }
        storage_mock.connection.meta.client = s3_client
        storage_mock.bucket_name = "bucket"

        transcribe_mock = mock.AsyncMock(side_effect=RuntimeError("stop"))
        linto_cls.return_value.transcribe = transcribe_mock

        with pytest.raises(RuntimeError, match="stop"):
            linto_task._process_linto_transcription_sync(str(rec.id))

    extract_mock.assert_called_once_with(b"fake mp4 bytes")
    _, kwargs = transcribe_mock.call_args
    assert kwargs["file"] == b"OggS extracted"


def test_ogg_source_skips_extraction():
    """Recording.extension == ogg must NOT call extract_audio_from_video."""
    rec = _make_recording(models.RecordingModeChoices.TRANSCRIPT)
    assert rec.extension == FileExtension.OGG.value

    with (
        mock.patch.object(linto_task, "default_storage") as storage_mock,
        mock.patch.object(linto_task, "extract_audio_from_video") as extract_mock,
        mock.patch.object(linto_task, "LinTO") as linto_cls,
    ):
        s3_client = mock.Mock()
        s3_client.get_object.return_value = {
            "Body": mock.Mock(read=mock.Mock(return_value=b"OggS raw"))
        }
        storage_mock.connection.meta.client = s3_client
        storage_mock.bucket_name = "bucket"

        transcribe_mock = mock.AsyncMock(side_effect=RuntimeError("stop"))
        linto_cls.return_value.transcribe = transcribe_mock

        with pytest.raises(RuntimeError, match="stop"):
            linto_task._process_linto_transcription_sync(str(rec.id))

    extract_mock.assert_not_called()
    _, kwargs = transcribe_mock.call_args
    assert kwargs["file"] == b"OggS raw"
